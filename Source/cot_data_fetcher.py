#!/usr/bin/env python3
"""
cot_data_fetcher.py — CFTC Commitment of Traders positioning data.

Source: CFTC Financial Futures Weekly report (FinFutWk.txt)
  URL: https://www.cftc.gov/dea/newcot/FinFutWk.txt
  Published: Every Tuesday at 3:30 PM ET for the prior Tuesday's positions.

Tracks Leveraged Money (speculator) + Asset Manager positions for 7 FX contracts.
Stores history in v2/intelligence.db (cot_positioning table) for percentile calculation.
Cache TTL: 7 days (COT updates weekly).
"""

import csv
import io
import logging
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional

from db_pool import get_intelligence

logger = logging.getLogger(__name__)

CFTC_FINANCIAL_FUTURES_URL = "https://www.cftc.gov/dea/newcot/FinFutWk.txt"

# CFTC contract names for FX futures (partial match, case-insensitive)
COT_FX_CONTRACTS = {
    "EUR": "EURO FX",
    "GBP": "BRITISH POUND",
    "JPY": "JAPANESE YEN",
    "AUD": "AUSTRALIAN DOLLAR",
    "CAD": "CANADIAN DOLLAR",
    "CHF": "SWISS FRANC",
    # NZD is not reported in the CFTC Financial Futures weekly file
}

EXTREME_LONG_PERCENTILE  = 90
EXTREME_SHORT_PERCENTILE = 10


def _get_db() -> sqlite3.Connection:
    """Return pooled v2/intelligence.db connection. Table init is idempotent."""
    conn = get_intelligence()
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cot_positioning (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            currency TEXT NOT NULL,
            spec_long INTEGER,
            spec_short INTEGER,
            spec_net INTEGER,
            asset_mgr_long INTEGER,
            asset_mgr_short INTEGER,
            asset_mgr_net INTEGER,
            combined_net INTEGER,
            combined_net_change INTEGER DEFAULT 0,
            percentile REAL,
            positioning_signal TEXT,
            squeeze_risk TEXT,
            fetched_at TEXT NOT NULL,
            UNIQUE(report_date, currency)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cot_date ON cot_positioning(report_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cot_currency ON cot_positioning(currency)")
    return conn


def _col_idx(header: List[str], col_name: str) -> int:
    """Find column index by partial case-insensitive match."""
    col_lower = col_name.lower()
    for i, h in enumerate(header):
        if col_lower in h.strip().lower():
            return i
    raise ValueError(f"Column '{col_name}' not found in header")


def _safe_int(val: str) -> int:
    """Parse integer, stripping commas and whitespace."""
    try:
        return int(val.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return 0


def _find_contract_row(reader: List[List[str]], contract_name: str) -> Optional[List[str]]:
    """Find the row matching a contract name (partial, case-insensitive)."""
    name_lower = contract_name.lower()
    for row in reader:
        if row and name_lower in row[0].lower():
            return row
    return None


def _calculate_percentile(value: int, history: List[int]) -> float:
    """Calculate percentile of value within historical series."""
    if not history:
        return 50.0
    below = sum(1 for h in history if h < value)
    return round(below / len(history) * 100, 1)


def _load_cot_history(conn: sqlite3.Connection, currency: str, limit: int = 156) -> List[int]:
    """Load up to ~3 years of combined_net history for a currency."""
    rows = conn.execute(
        "SELECT combined_net FROM cot_positioning WHERE currency = ? "
        "ORDER BY report_date DESC LIMIT ?",
        (currency, limit)
    ).fetchall()
    return [r["combined_net"] for r in rows if r["combined_net"] is not None]


def _load_previous_cot(conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Load most recent saved COT row per currency for change calculation."""
    result = {}
    for ccy in COT_FX_CONTRACTS:
        row = conn.execute(
            "SELECT spec_net, combined_net FROM cot_positioning "
            "WHERE currency = ? ORDER BY report_date DESC LIMIT 1",
            (ccy,)
        ).fetchone()
        if row:
            result[ccy] = {"spec_net": row["spec_net"], "combined_net": row["combined_net"]}
    return result


def fetch_cot_data() -> Dict[str, Dict]:
    """
    Fetch and parse current COT report for all 7 FX contracts.
    Stores results in v2/intelligence.db (cot_positioning).
    Returns dict keyed by currency code.

    On failure returns empty dict — caller should handle gracefully.
    """
    try:
        req = urllib.request.Request(
            CFTC_FINANCIAL_FUTURES_URL,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw_text = resp.read().decode("latin-1", errors="replace")
    except Exception as e:
        logger.error(f"COT fetch from CFTC failed: {e}")
        return {}

    try:
        reader = list(csv.reader(io.StringIO(raw_text)))
        if not reader:
            logger.error("COT file is empty")
            return {}

        # FinFutWk.txt has NO header row — it starts with data rows directly.
        # CFTC Traders in Financial Futures disaggregated format column indices:
        #   0  Market name
        #   1  Date compact (YYMMDD)
        #   2  Date (YYYY-MM-DD)           ← report date
        #   7  Open Interest
        #   8  Dealer Long All
        #   9  Dealer Short All
        #  10  Dealer Spread All
        #  11  Asset Manager Long All      ← am_long
        #  12  Asset Manager Short All     ← am_short
        #  13  Asset Manager Spread All
        #  14  Leveraged Money Long All    ← lev_long (speculators)
        #  15  Leveraged Money Short All   ← lev_short
        idx_date      = 2
        idx_am_long   = 11
        idx_am_short  = 12
        idx_lev_long  = 14
        idx_lev_short = 15

        data_rows = reader  # All rows are data rows

        conn = _get_db()
        previous = _load_previous_cot(conn)
        fetched_at = datetime.now(timezone.utc).isoformat()
        results: Dict[str, Dict] = {}

        for currency, contract_name in COT_FX_CONTRACTS.items():
            row = _find_contract_row(data_rows, contract_name)
            if not row or len(row) <= max(idx_lev_long, idx_lev_short, idx_am_long, idx_am_short):
                logger.warning(f"COT row not found for {currency} ({contract_name})")
                continue

            report_date = row[idx_date].strip() if len(row) > idx_date else "unknown"
            spec_long   = _safe_int(row[idx_lev_long])
            spec_short  = _safe_int(row[idx_lev_short])
            am_long     = _safe_int(row[idx_am_long])
            am_short    = _safe_int(row[idx_am_short])

            spec_net     = spec_long - spec_short
            am_net       = am_long - am_short
            combined_net = spec_net + am_net

            # Change vs previous week
            prev = previous.get(currency, {})
            combined_net_change = combined_net - prev.get("combined_net", combined_net)

            # Historical percentile
            history = _load_cot_history(conn, currency)
            percentile = _calculate_percentile(combined_net, history) if history else 50.0

            extreme_long  = percentile > EXTREME_LONG_PERCENTILE
            extreme_short = percentile < EXTREME_SHORT_PERCENTILE

            positioning_signal = (
                "extreme_long"  if extreme_long  else
                "extreme_short" if extreme_short else
                "neutral"
            )
            squeeze_risk = "high" if (extreme_long or extreme_short) else "low"

            entry = {
                "currency":           currency,
                "report_date":        report_date,
                "spec_long":          spec_long,
                "spec_short":         spec_short,
                "spec_net":           spec_net,
                "asset_mgr_long":     am_long,
                "asset_mgr_short":    am_short,
                "asset_mgr_net":      am_net,
                "combined_net":       combined_net,
                "combined_net_change": combined_net_change,
                "percentile":         percentile,
                "positioning_signal": positioning_signal,
                "squeeze_risk":       squeeze_risk,
            }
            results[currency] = entry

            # Persist to DB (upsert)
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO cot_positioning
                    (report_date, currency, spec_long, spec_short, spec_net,
                     asset_mgr_long, asset_mgr_short, asset_mgr_net,
                     combined_net, combined_net_change, percentile,
                     positioning_signal, squeeze_risk, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    report_date, currency,
                    spec_long, spec_short, spec_net,
                    am_long, am_short, am_net,
                    combined_net, combined_net_change, percentile,
                    positioning_signal, squeeze_risk, fetched_at,
                ))
            except Exception as db_err:
                logger.warning(f"COT DB insert failed for {currency}: {db_err}")

        conn.commit()

        logger.info(f"COT data fetched: {list(results.keys())}")
        return results

    except Exception as e:
        logger.error(f"COT parse error: {e}")
        return {}


def get_latest_cot() -> Dict[str, Dict]:
    """
    Return the most recent COT row per currency from the local DB cache.
    Used when the live fetch fails or when TTL hasn't expired.
    """
    try:
        conn = _get_db()
        results = {}
        for ccy in COT_FX_CONTRACTS:
            row = conn.execute(
                "SELECT * FROM cot_positioning WHERE currency = ? "
                "ORDER BY report_date DESC LIMIT 1",
                (ccy,)
            ).fetchone()
            if row:
                results[ccy] = dict(row)
        return results
    except Exception as e:
        logger.error(f"get_latest_cot DB error: {e}")
        return {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = fetch_cot_data()
    for ccy, d in data.items():
        print(f"{ccy}: net={d['combined_net']:+,} pctile={d['percentile']:.0f}% signal={d['positioning_signal']} squeeze={d['squeeze_risk']}")
