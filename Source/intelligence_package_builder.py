#!/usr/bin/env python3
"""
intelligence_package_builder.py — Intelligence Package Builder v2.0

Assembles a comprehensive intelligence package for each trading window (3x daily).
Sources: FRED, Yahoo Finance, Wolfram expanded macro, NewsAPI, economic calendar,
         COT positioning, TA summary (flight recorder), recent trades, news sentiment.

Stores assembled package in Database/v2/intelligence.db (intelligence_packages table).
Validator reads the cached package at trade time — zero latency at trade time.

Usage:
    builder = IntelligencePackageBuilder()
    package = builder.build(window="london")
    package.save()

    # Or as async:
    package = await builder.build_async(window="london")
"""

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from db_pool import get_intelligence

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
ALL_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "NZD_USD", "USD_CAD",
    "USD_CHF", "EUR_GBP", "EUR_JPY", "GBP_JPY", "AUD_JPY", "EUR_AUD", "EUR_CHF"
]

SESSION_PAIRS = {
    "asia":   ["AUD_USD", "NZD_USD", "AUD_JPY", "USD_JPY", "EUR_JPY", "GBP_JPY"],
    "london": ["EUR_USD", "GBP_USD", "USD_CHF", "EUR_GBP", "EUR_AUD", "EUR_CHF"],
    "ny":     ["USD_CAD", "EUR_USD", "GBP_USD", "USD_JPY"],
}

INTELLIGENCE_WINDOWS = {
    "asia":   {"label": "Asia Session",   "et_hour": "6 AM"},
    "london": {"label": "London Session", "et_hour": "12 PM"},
    "ny":     {"label": "NY Session",     "et_hour": "5 PM"},
}

# Currency → economy label
_ECONOMY_LABELS = {
    "USD": "US (Fed)",    "EUR": "Eurozone (ECB)", "GBP": "UK (BoE)",
    "JPY": "Japan (BoJ)", "AUD": "Australia (RBA)", "NZD": "NZ (RBNZ)",
    "CAD": "Canada (BoC)", "CHF": "Switzerland (SNB)",
}

# Central bank policy rates (static fallback — keep current)
_POLICY_RATES = {
    "USD": 4.25, "EUR": 2.65, "GBP": 4.50, "JPY": 0.50,
    "AUD": 4.10, "NZD": 3.75, "CAD": 3.00, "CHF": 0.25,
}

# VIX position-sizing rules
_VIX_SIZE_RULES = {
    20: ("standard", "Standard position sizing"),
    25: ("reduced_25pct", "Reduce position size 25%"),
    float("inf"): ("reduced_50pct", "Reduce position size 50%"),
}


# ── DB Init ───────────────────────────────────────────────────────────────────

def _init_db(conn: sqlite3.Connection):
    """Create intelligence_packages table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            package_version TEXT DEFAULT '2.0',
            macro_data TEXT,
            cross_asset_data TEXT,
            cot_data TEXT,
            calendar_data TEXT,
            per_pair_data TEXT,
            correlation_data TEXT,
            risk_factors TEXT,
            package_text TEXT NOT NULL,
            data_sources_used TEXT,
            data_sources_failed TEXT,
            assembly_time_ms INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_packages_window "
        "ON intelligence_packages(window, generated_at)"
    )
    conn.commit()


def _get_intel_db() -> sqlite3.Connection:
    """Return pooled v2/intelligence.db connection. Table init is idempotent."""
    conn = get_intelligence()
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class IntelligencePackage:
    """Complete intelligence package for one window."""
    window: str
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    macro_data:      Dict = field(default_factory=dict)
    cross_asset_data: Dict = field(default_factory=dict)
    cot_data:        Dict = field(default_factory=dict)
    calendar_data:   Dict = field(default_factory=dict)
    per_pair_data:   Dict = field(default_factory=dict)
    correlation_data: Dict = field(default_factory=dict)
    risk_factors:    Dict = field(default_factory=dict)

    data_sources_used:   List[str] = field(default_factory=list)
    data_sources_failed: List[str] = field(default_factory=list)
    assembly_time_ms: int = 0
    id: Optional[int] = None

    def to_dict(self) -> Dict:
        return {
            "window":            self.window,
            "generated_at":      self.generated_at.isoformat(),
            "package_version":   "2.0",
            "macro_data":        self.macro_data,
            "cross_asset_data":  self.cross_asset_data,
            "cot_data":          self.cot_data,
            "calendar_data":     self.calendar_data,
            "per_pair_data":     self.per_pair_data,
            "correlation_data":  self.correlation_data,
            "risk_factors":      self.risk_factors,
            "data_sources_used": self.data_sources_used,
            "data_sources_failed": self.data_sources_failed,
            "assembly_time_ms":  self.assembly_time_ms,
        }

    def to_markdown(self) -> str:
        """Assemble the full intelligence package as a Markdown document."""
        return _render_markdown(self)

    def save(self) -> int:
        """Save package to intelligence_packages table in v2/intelligence.db."""
        conn = _get_intel_db()
        package_text = self.to_markdown()
        cur = conn.execute("""
            INSERT INTO intelligence_packages
            (window, generated_at, package_version,
             macro_data, cross_asset_data, cot_data, calendar_data,
             per_pair_data, correlation_data, risk_factors,
             package_text, data_sources_used, data_sources_failed,
             assembly_time_ms)
            VALUES (?, ?, '2.0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            self.window,
            self.generated_at.isoformat(),
            json.dumps(self.macro_data),
            json.dumps(self.cross_asset_data),
            json.dumps(self.cot_data),
            json.dumps(self.calendar_data),
            json.dumps(self.per_pair_data),
            json.dumps(self.correlation_data),
            json.dumps(self.risk_factors),
            package_text,
            json.dumps(self.data_sources_used),
            json.dumps(self.data_sources_failed),
            self.assembly_time_ms,
        ))
        conn.commit()
        self.id = cur.lastrowid
        logger.info(f"Intelligence package saved: id={self.id} window={self.window} "
                    f"sources={self.data_sources_used} failed={self.data_sources_failed} "
                    f"assembly={self.assembly_time_ms}ms")
        return self.id


# ── Markdown Renderer ─────────────────────────────────────────────────────────

def _render_markdown(pkg: "IntelligencePackage") -> str:
    """Render the full intelligence package as a Markdown document."""
    win_label = INTELLIGENCE_WINDOWS.get(pkg.window, {}).get("label", pkg.window.upper())
    date_str  = pkg.generated_at.strftime("%Y-%m-%d")
    ts_str    = pkg.generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        f"---",
        f'package_version: "2.0"',
        f'window: "{pkg.window}"',
        f'generated_at: "{ts_str}"',
        f'session_label: "{win_label}"',
        f'pairs_count: {len(ALL_PAIRS)}',
        f'data_sources:',
    ]
    for src in (pkg.data_sources_used or []):
        lines.append(f'  - {src}')
    lines.append("---\n")
    lines.append(f"# Intelligence Package — {win_label} — {date_str}\n")

    # Section 1: Global Macro Snapshot
    lines.append("## 1. GLOBAL MACRO SNAPSHOT\n")
    lines += _render_macro_section(pkg.macro_data)

    # Section 2: Cross-Asset Dashboard
    lines.append("## 2. CROSS-ASSET DASHBOARD\n")
    lines += _render_cross_asset_section(pkg.cross_asset_data)

    # Section 3: COT Positioning
    lines.append("## 3. COT POSITIONING\n")
    lines += _render_cot_section(pkg.cot_data)

    # Section 4: Economic Calendar
    lines.append("## 4. ECONOMIC CALENDAR (Next 24 Hours)\n")
    lines += _render_calendar_section(pkg.calendar_data)

    # Section 5: Per-Pair Analysis
    lines.append("## 5. PER-PAIR ANALYSIS\n")
    for i, pair in enumerate(ALL_PAIRS, 1):
        pair_data = pkg.per_pair_data.get(pair, {})
        lines += _render_pair_section(i, pair, pair_data, pkg)

    # Section 6: Cross-Pair Correlations
    lines.append("## 6. CROSS-PAIR CORRELATIONS (20-Day)\n")
    lines += _render_correlation_section(pkg.correlation_data, pkg.cross_asset_data)

    # Section 7: Risk Factors
    lines.append("## 7. RISK FACTORS\n")
    lines += _render_risk_section(pkg.risk_factors, pkg.cross_asset_data)

    lines.append("---")
    lines.append(f"*End of Intelligence Package — v2.0*")
    lines.append(f"*Generated at: {ts_str}*")
    lines.append(f"*Sources: {', '.join(pkg.data_sources_used or ['none'])}*")
    lines.append(f"*Assembly time: {pkg.assembly_time_ms}ms*")

    return "\n".join(lines)


def _render_macro_section(macro: dict) -> List[str]:
    lines = []

    # 1.1 Interest Rates
    lines.append("### 1.1 Interest Rates & Monetary Policy")
    lines.append("| Economy | Policy Rate | 10yr Yield | Direction |")
    lines.append("|---------|-------------|------------|-----------|")
    rates    = macro.get("rates", {})
    bonds    = macro.get("bond_yields", {})
    for ccy, label in _ECONOMY_LABELS.items():
        rate     = rates.get(ccy, _POLICY_RATES.get(ccy, "—"))
        bond     = bonds.get(ccy, "—")
        rate_str = f"{rate:.2f}%" if isinstance(rate, float) else str(rate)
        bond_str = f"{bond:.2f}%" if isinstance(bond, float) else str(bond)
        lines.append(f"| {label} | {rate_str} | {bond_str} | — |")
    lines.append("")

    # 1.2 GDP
    lines.append("### 1.2 GDP Growth")
    lines.append("| Economy | Latest | Trend |")
    lines.append("|---------|--------|-------|")
    gdp = macro.get("expanded", {})
    for ccy, label in _ECONOMY_LABELS.items():
        econ_key = ccy.lower()
        val = gdp.get(f"gdp_{econ_key}") or gdp.get(f"gdp_{_ECON_KEY_MAP.get(ccy, econ_key)}")
        val_str = _truncate(val, 80) if val else "—"
        lines.append(f"| {label} | {val_str} | — |")
    lines.append("")

    # 1.3 PMI
    lines.append("### 1.3 PMI Readings")
    lines.append("| Economy | Mfg PMI | Svc PMI |")
    lines.append("|---------|---------|---------|")
    for ccy, label in _ECONOMY_LABELS.items():
        econ_key = _ECON_KEY_MAP.get(ccy, ccy.lower())
        mfg = _truncate(gdp.get(f"pmi_mfg_{econ_key}"), 30) or "—"
        svc = _truncate(gdp.get(f"pmi_svc_{econ_key}"), 30) or "—"
        lines.append(f"| {label} | {mfg} | {svc} |")
    lines.append("")

    # 1.4 Trade Balance & Confidence
    lines.append("### 1.4 Trade Balance & Consumer Confidence")
    lines.append("| Economy | Trade Balance | Consumer Confidence | Retail Sales |")
    lines.append("|---------|--------------|---------------------|--------------|")
    for ccy, label in _ECONOMY_LABELS.items():
        econ_key = _ECON_KEY_MAP.get(ccy, ccy.lower())
        trade = _truncate(gdp.get(f"trade_{econ_key}"), 40) or "—"
        conf  = _truncate(gdp.get(f"conf_{econ_key}"), 40) or "—"
        retail = _truncate(gdp.get(f"retail_{econ_key}"), 40) or "—"
        lines.append(f"| {label} | {trade} | {conf} | {retail} |")
    lines.append("")

    return lines


# Economy key mapping: currency → wolfram query suffix
_ECON_KEY_MAP = {
    "USD": "us",
    "EUR": "eurozone",
    "GBP": "uk",
    "JPY": "japan",
    "AUD": "australia",
    "NZD": "nz",
    "CAD": "canada",
    "CHF": "switzerland",
}


def _render_cross_asset_section(ca: dict) -> List[str]:
    lines = []
    lines.append("### 2.1 Risk Gauges")
    lines.append("| Indicator | Value | Signal |")
    lines.append("|-----------|-------|--------|")

    for key, label in [("vix", "VIX"), ("dxy", "DXY"), ("sp500", "S&P 500"),
                        ("nasdaq", "Nasdaq"), ("tlt", "TLT (Bonds)"), ("btc", "BTC-USD")]:
        d     = ca.get(key, {})
        price = d.get("current_price")
        if price is None:
            lines.append(f"| {label} | — | — |")
            continue
        val_str = f"{price:,.1f}" if price > 100 else f"{price:.2f}"
        signal  = d.get("level") or d.get("trend") or "—"
        lines.append(f"| {label} | {val_str} | {signal} |")
    lines.append("")

    lines.append("### 2.2 Sector Performance")
    lines.append("| Sector | ETF | Price |")
    lines.append("|--------|-----|-------|")
    for key, label in [("xlf", "Financials (XLF)"), ("xle", "Energy (XLE)")]:
        d     = ca.get(key, {})
        price = d.get("current_price")
        try:
            price_str = f"{float(price):.2f}" if price is not None else "—"
        except (TypeError, ValueError):
            price_str = "—"
        lines.append(f"| {label} | {key.upper()} | {price_str} |")
    lines.append("")

    return lines


def _render_cot_section(cot: dict) -> List[str]:
    if not cot:
        return ["*COT data not available.*\n"]
    lines = []
    lines.append("| Currency | Spec Net | Chg | Asset Mgr Net | Combined | Pctile | Signal | Squeeze |")
    lines.append("|----------|----------|-----|---------------|----------|--------|--------|---------|")
    for ccy in ["EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"]:
        d = cot.get(ccy, {})
        if not d:
            continue
        spec_net = d.get("spec_net", 0)
        chg      = d.get("combined_net_change", 0)
        am_net   = d.get("asset_mgr_net", 0)
        comb     = d.get("combined_net", 0)
        pct      = d.get("percentile", 50)
        signal   = d.get("positioning_signal", "neutral")
        squeeze  = d.get("squeeze_risk", "low").upper()
        lines.append(
            f"| {ccy} | {spec_net:+,} | {chg:+,} | {am_net:+,} | {comb:+,} "
            f"| {pct:.0f}% | {signal} | {squeeze} |"
        )
    lines.append("")

    # Squeeze flags
    extremes = [ccy for ccy, d in cot.items() if d.get("squeeze_risk") == "high"]
    if extremes:
        for ccy in extremes:
            signal = cot[ccy].get("positioning_signal", "extreme")
            lines.append(f"**Squeeze risk: {ccy} {signal} — position squeeze risk elevated.**")
        lines.append("")

    return lines


def _render_calendar_section(cal: dict) -> List[str]:
    if not cal or not cal.get("events"):
        return ["*Economic calendar not available.*\n"]
    lines = []
    events = cal.get("events", [])
    high   = [e for e in events if e.get("impact") == "high"]
    medium = [e for e in events if e.get("impact") == "medium"]

    if high:
        lines.append("### High Impact")
        lines.append("| Time (UTC) | Event | Currency | Expected | Previous |")
        lines.append("|------------|-------|----------|----------|----------|")
        for e in high[:10]:
            t_str = e.get("time_utc", "")[:16].replace("T", " ")
            lines.append(
                f"| {t_str} | {e['event_name']} | {e['currency']} "
                f"| {e.get('expected') or '—'} | {e.get('previous') or '—'} |"
            )
        lines.append("")

    if medium:
        lines.append("### Medium Impact")
        lines.append("| Time (UTC) | Event | Currency | Expected | Previous |")
        lines.append("|------------|-------|----------|----------|----------|")
        for e in medium[:10]:
            t_str = e.get("time_utc", "")[:16].replace("T", " ")
            lines.append(
                f"| {t_str} | {e['event_name']} | {e['currency']} "
                f"| {e.get('expected') or '—'} | {e.get('previous') or '—'} |"
            )
        lines.append("")

    nhi = cal.get("next_high_impact")
    if nhi:
        lines.append(f"**Next high-impact event: {nhi['event']} in {nhi.get('hours_away', '?')}h.**")
        lines.append("")

    return lines


def _render_pair_section(idx: int, pair: str, pair_data: dict, pkg: "IntelligencePackage") -> List[str]:
    lines = [f"### 5.{idx} {pair.replace('_', '/')}\n"]
    base_ccy, quote_ccy = pair.split("_")

    # Macro profile
    lines.append("#### Macro Profile")
    base_rate  = _POLICY_RATES.get(base_ccy, "?")
    quote_rate = _POLICY_RATES.get(quote_ccy, "?")
    diff       = round(base_rate - quote_rate, 2) if isinstance(base_rate, float) and isinstance(quote_rate, float) else "?"
    lines.append(f"- **Rate differential:** {base_ccy} {base_rate}% vs {quote_ccy} {quote_rate}% = {diff:+.2f}bp" if isinstance(diff, float) else f"- **Rate differential:** {diff}")

    # FX range from existing macro_data
    pair_macro = pkg.macro_data.get("fx_ranges", {}).get(pair, {})
    if pair_macro:
        cur  = pair_macro.get("pair_current_price")
        lo   = pair_macro.get("pair_1yr_min")
        hi   = pair_macro.get("pair_1yr_max")
        pos  = pair_macro.get("pair_range_position")
        if cur:
            lines.append(f"- **1yr range:** {lo} – {hi} | Current: {cur} | Position: {pos}%")
    lines.append("")

    # TA Summary
    ta = pair_data.get("ta_summary", {})
    ind = ta.get("indicators", {})
    if ind:
        lines.append("#### Technical Summary")
        trend = ta.get("trend_direction") or ta.get("patterns", {}).get("trend_direction") or "—"
        lines.append(f"- **Trend:** {trend}")
        if ind.get("rsi_14"):
            lines.append(f"- **RSI(14):** {ind['rsi_14']:.1f} ({ind.get('rsi_signal', '—')})")
        if ind.get("macd_signal"):
            lines.append(f"- **MACD:** {ind['macd_signal']}")
        if ind.get("adx_14"):
            lines.append(f"- **ADX:** {ind['adx_14']:.1f} ({ind.get('adx_trend_strength', '—')})")
        if ind.get("ema_alignment"):
            lines.append(f"- **EMA Alignment:** {ind['ema_alignment']}")
        kl = ta.get("key_levels", {})
        if kl.get("resistance_levels"):
            lines.append(f"- **Resistance:** {', '.join(str(r) for r in kl['resistance_levels'][:3])}")
        if kl.get("support_levels"):
            lines.append(f"- **Support:** {', '.join(str(s) for s in kl['support_levels'][:3])}")
        lines.append("")

    # News Sentiment
    news = pair_data.get("news", {})
    agg  = news.get("aggregate_sentiment", {})
    if agg:
        lines.append("#### News Sentiment")
        lines.append(f"- **Score:** {agg.get('score', 0):+.3f} ({agg.get('label', 'neutral')})")
        articles = news.get("articles", [])
        if articles:
            lines.append("- **Key headlines:**")
            for a in articles[:3]:
                src      = a.get("source", "?")
                title    = a.get("title", "")[:80]
                senti    = a.get("sentiment", "neutral")
                impact   = a.get("impact_level", "low")
                lines.append(f"  - [{src}] {title} — {senti}, {impact} impact")
        lines.append("")

    # COT Context
    cot = pkg.cot_data
    if cot:
        lines.append("#### COT Context")
        for ccy in [base_ccy, quote_ccy]:
            d = cot.get(ccy, {})
            if d:
                net    = d.get("combined_net", 0)
                pct    = d.get("percentile", 50)
                signal = d.get("positioning_signal", "neutral")
                lines.append(f"- {ccy}: net {net:+,} ({pct:.0f}th pctile) — {signal}")
        lines.append("")

    # Recent Trades
    rt = pair_data.get("recent_trades", {})
    trades = rt.get("recent_trades", [])
    if trades:
        lines.append("#### Recent Trades")
        lines.append("| # | Dir | Result | Pips | Setup | Reason |")
        lines.append("|---|-----|--------|------|-------|--------|")
        for i, t in enumerate(trades[:5], 1):
            lines.append(
                f"| {i} | {t.get('direction','?').upper()} | {t.get('result','?')} "
                f"| {t.get('pips', 0):+.1f} | {t.get('setup_code') or '—'} "
                f"| {t.get('close_reason') or '—'} |"
            )
        s = rt.get("summary", {})
        if s:
            lines.append(
                f"- **Summary:** {s.get('wins', 0)}W/{s.get('losses', 0)}L/{s.get('breakeven', 0)}BE "
                f"| {s.get('total_pips', 0):+.1f} pips | WR {s.get('win_rate', 0):.0f}% "
                f"| Streak: {s.get('streak', 'none')}"
            )
        lines.append("")

    lines.append("---\n")
    return lines


def _render_risk_section(risk: dict, cross_asset: dict) -> List[str]:
    lines = []
    immediate = risk.get("immediate", [])
    medium    = risk.get("medium_term", [])

    if immediate:
        lines.append("### Immediate (Next 24h)")
        for i, r in enumerate(immediate, 1):
            rtype  = r.get("type", "unknown")
            desc   = r.get("action") or r.get("currency") or ""
            lines.append(f"{i}. **{rtype}** — {desc}")
        lines.append("")

    if medium:
        lines.append("### Medium-Term (1-2 Weeks)")
        for i, r in enumerate(medium, 1):
            rtype = r.get("type", "unknown")
            desc  = r.get("action") or r.get("description") or ""
            lines.append(f"{i}. **{rtype}** — {desc}")
        lines.append("")

    # VIX position sizing guide
    vix_d  = cross_asset.get("vix", {})
    vix_val = vix_d.get("current_price")
    if vix_val:
        lines.append("### Position Size Adjustment")
        if vix_val < 20:
            lines.append(f"- VIX {vix_val:.1f} — standard sizing")
        elif vix_val < 25:
            lines.append(f"- VIX {vix_val:.1f} — **reduce 25%**")
        else:
            lines.append(f"- VIX {vix_val:.1f} — **reduce 50%** (high volatility)")
        lines.append("")

    return lines


def _render_correlation_section(corr: dict, cross_asset: dict) -> List[str]:
    if not corr:
        return ["*Correlation data not available.*\n"]
    lines = []
    strong = corr.get("strong", [])
    days   = corr.get("days", 20)

    if strong:
        lines.append(f"*Based on {days}-day rolling close prices.*\n")
        lines.append("### Strong Correlations (|r| ≥ 0.70)\n")
        lines.append("| Pair A | Pair B | r | Direction |")
        lines.append("|--------|--------|---|-----------|")
        for pa, pb, r in strong[:15]:
            direction = "co-move" if r > 0 else "inverse"
            lines.append(f"| {pa.replace('_','/')} | {pb.replace('_','/')} | {r:+.3f} | {direction} |")
        lines.append("")

        # Cluster warnings: if trading two highly correlated pairs, flag doubled exposure
        lines.append("### Correlation Risk")
        warned = set()
        for pa, pb, r in strong:
            if abs(r) >= 0.85 and (pa, pb) not in warned:
                warned.add((pa, pb))
                direction_str = "same direction" if r > 0 else "opposite direction"
                lines.append(f"- **{pa.replace('_','/')}/{pb.replace('_','/')}** r={r:+.3f} — "
                              f"high correlation ({direction_str}): avoid simultaneous positions, "
                              f"effectively doubles exposure")
        if not warned:
            lines.append("- No extreme correlation warnings (|r| < 0.85)")
        lines.append("")
    else:
        lines.append("*No strong correlations detected (|r| < 0.70).*\n")

    # Live spread table
    live_spreads = cross_asset.get("live_spreads", {})
    if live_spreads:
        lines.append("### Live Spreads (OANDA)\n")
        lines.append("| Pair | Bid | Ask | Spread (pips) |")
        lines.append("|------|-----|-----|---------------|")
        for pair in ALL_PAIRS:
            s = live_spreads.get(pair)
            if s:
                flag = " ⚠" if s["spread_pips"] > 3.0 else ""
                lines.append(
                    f"| {pair.replace('_','/')} | {s['bid']:.5f} | {s['ask']:.5f} "
                    f"| {s['spread_pips']:.1f}{flag} |"
                )
        lines.append("")

    return lines


def _truncate(val: Optional[str], max_len: int = 60) -> Optional[str]:
    if val is None:
        return None
    val = str(val).strip()
    return val[:max_len] + "…" if len(val) > max_len else val


# ── Builder ───────────────────────────────────────────────────────────────────

class IntelligencePackageBuilder:
    """Builds intelligence packages from all data sources."""

    def __init__(self, user_id: int = 1):
        self.user_id = user_id

    def build(self, window: str) -> "IntelligencePackage":
        """
        Build a complete intelligence package synchronously.
        Runs all data fetchers, assembles package, returns IntelligencePackage.
        """
        start_ms = int(time.time() * 1000)
        pkg = IntelligencePackage(window=window)

        # ── Phase 1: Independent sources ──────────────────────────────────
        # Each source is independent; failures are logged and skipped.

        # 1a. Existing FRED + Yahoo FX rates
        try:
            existing = self._fetch_existing_macro()
            pkg.macro_data.update(existing)
            pkg.data_sources_used.append("fred_yahoo")
        except Exception as e:
            logger.error(f"fred_yahoo failed: {e}")
            pkg.data_sources_failed.append("fred_yahoo")

        # 1b. Yahoo cross-asset
        try:
            from economic_data_fetcher import fetch_cross_asset_data
            pkg.cross_asset_data = fetch_cross_asset_data()
            pkg.data_sources_used.append("yahoo_cross_asset")
        except Exception as e:
            logger.error(f"yahoo_cross_asset failed: {e}")
            pkg.data_sources_failed.append("yahoo_cross_asset")

        # 1c. Wolfram expanded macro
        try:
            from intelligence_agent_prep import _fetch_wolfram_macro_expanded
            expanded = _fetch_wolfram_macro_expanded()
            pkg.macro_data["expanded"] = expanded
            pkg.data_sources_used.append("wolfram_expanded")
        except Exception as e:
            logger.error(f"wolfram_expanded failed: {e}")
            pkg.data_sources_failed.append("wolfram_expanded")

        # 1d. COT data
        try:
            from cot_data_fetcher import fetch_cot_data, get_latest_cot
            cot = fetch_cot_data()
            if not cot:
                cot = get_latest_cot()
                if cot:
                    logger.info("COT: using cached data (live fetch returned empty)")
            if cot:
                pkg.cot_data = cot
                pkg.data_sources_used.append("cot_cftc")
        except Exception as e:
            logger.error(f"cot_cftc failed: {e}")
            pkg.data_sources_failed.append("cot_cftc")

        # 1e. Economic calendar
        try:
            from economic_calendar_fetcher import fetch_economic_calendar
            pkg.calendar_data = fetch_economic_calendar(24)
            pkg.data_sources_used.append("economic_calendar")
        except Exception as e:
            logger.error(f"economic_calendar failed: {e}")
            pkg.data_sources_failed.append("economic_calendar")

        # ── Phase 2: Per-pair data ─────────────────────────────────────────
        for pair in ALL_PAIRS:
            pair_data = self._build_pair_analysis(pair)
            pkg.per_pair_data[pair] = pair_data

        if any(pkg.per_pair_data):
            pkg.data_sources_used.append("per_pair_analysis")

        # ── Phase 3: Cross-pair correlations ──────────────────────────────
        try:
            from economic_data_fetcher import fetch_pair_correlations
            pkg.correlation_data = fetch_pair_correlations(days=20)
            if pkg.correlation_data:
                pkg.data_sources_used.append("cross_pair_correlations")
        except Exception as e:
            logger.error(f"cross_pair_correlations failed: {e}")
            pkg.data_sources_failed.append("cross_pair_correlations")

        # ── Phase 4: Live spreads ──────────────────────────────────────────
        try:
            spread_data = self._fetch_live_spreads()
            if spread_data:
                pkg.cross_asset_data["live_spreads"] = spread_data
                pkg.data_sources_used.append("live_spreads")
        except Exception as e:
            logger.error(f"live_spreads failed: {e}")
            pkg.data_sources_failed.append("live_spreads")

        # ── Phase 5: Risk factors ──────────────────────────────────────────
        pkg.risk_factors = self._assess_risk_factors(pkg)

        pkg.assembly_time_ms = int(time.time() * 1000) - start_ms
        logger.info(
            f"Package assembled: window={window} "
            f"sources={pkg.data_sources_used} failed={pkg.data_sources_failed} "
            f"time={pkg.assembly_time_ms}ms"
        )
        return pkg

    def _fetch_existing_macro(self) -> dict:
        """Fetch FRED rates + Yahoo FX ranges for all pairs."""
        try:
            from economic_data_fetcher import get_interest_rates, get_fx_range, _POLICY_RATES
        except ImportError:
            from Source.economic_data_fetcher import get_interest_rates, get_fx_range, _POLICY_RATES

        result: dict = {
            "rates":       dict(_POLICY_RATES),
            "bond_yields": {},
            "fx_ranges":   {},
        }

        # Bond yields via FRED
        try:
            sample = get_interest_rates("USD", "EUR")
            for k, v in sample.items():
                if "bond_yield" in k:
                    ccy_prefix = "USD" if "base" in k else "EUR"
                    result["bond_yields"][ccy_prefix] = v
        except Exception:
            pass

        # FX 1yr ranges for all 13 pairs
        for pair in ALL_PAIRS:
            try:
                fx = get_fx_range(pair)
                if fx:
                    result["fx_ranges"][pair] = fx
            except Exception:
                pass

        return result

    def _build_pair_analysis(self, pair: str) -> dict:
        """Build complete analysis for a single pair."""
        result: dict = {"pair": pair}

        # TA summary
        try:
            from ta_summary_fetcher import fetch_ta_summary
            result["ta_summary"] = fetch_ta_summary(pair, self.user_id)
        except Exception as e:
            logger.debug(f"[{pair}] TA summary error: {e}")
            result["ta_summary"] = {}

        # Recent trades
        try:
            from trade_outcome_fetcher import fetch_recent_trades
            result["recent_trades"] = fetch_recent_trades(pair, self.user_id)
        except Exception as e:
            logger.debug(f"[{pair}] Recent trades error: {e}")
            result["recent_trades"] = {}

        # News + sentiment scoring
        try:
            result["news"] = self._fetch_and_score_news(pair)
        except Exception as e:
            logger.debug(f"[{pair}] News/sentiment error: {e}")
            result["news"] = {}

        return result

    def _fetch_and_score_news(self, pair: str) -> dict:
        """Fetch news articles and score sentiment for a pair."""
        try:
            try:
                from agents.wrappers import query_news_for_pair
            except ImportError:
                from Source.agents.wrappers import query_news_for_pair
            news_raw = query_news_for_pair(pair)
            articles = news_raw.get("articles", []) if isinstance(news_raw, dict) else []
        except Exception:
            articles = []

        try:
            from news_sentiment_scorer import score_news_batch, aggregate_sentiment
            scored    = score_news_batch(articles, pair)
            aggregate = aggregate_sentiment(scored)
        except Exception:
            scored    = articles
            aggregate = {"score": 0.0, "label": "neutral", "article_count": len(articles), "high_impact_count": 0}

        return {"articles": scored, "aggregate_sentiment": aggregate}

    def _fetch_live_spreads(self) -> dict:
        """Fetch live bid/ask spreads for all 13 pairs from OANDA."""
        try:
            from oanda_client import OandaClient
            client = OandaClient()  # uses config.py API_KEY / ACCOUNT_ID
            pricing = client.get_pricing(ALL_PAIRS)
            prices = pricing.get("prices", [])
            spreads = {}
            for p in prices:
                instrument = p.get("instrument", "")
                bids = p.get("bids", [])
                asks = p.get("asks", [])
                if not bids or not asks:
                    continue
                bid = float(bids[0].get("price", 0))
                ask = float(asks[0].get("price", 0))
                raw_spread = ask - bid
                # Convert to pips (JPY pairs: 2 decimal places, others: 4)
                multiplier = 100 if "JPY" in instrument else 10000
                spread_pips = round(raw_spread * multiplier, 1)
                spreads[instrument] = {
                    "bid":         round(bid, 5),
                    "ask":         round(ask, 5),
                    "spread_pips": spread_pips,
                    "tradeable":   p.get("tradeable", True),
                }
            return spreads
        except Exception as e:
            logger.debug(f"Live spread fetch failed: {e}")
            return {}

    def _assess_risk_factors(self, pkg: "IntelligencePackage") -> dict:
        """Identify immediate and medium-term risk factors from all data."""
        factors: dict = {"immediate": [], "medium_term": []}

        # VIX spike
        vix = pkg.cross_asset_data.get("vix", {})
        vix_level = vix.get("current_price")
        if vix_level and vix_level > 25:
            factors["immediate"].append({
                "type":   "vix_spike",
                "level":  vix_level,
                "action": "reduce_position_size_50pct",
            })
        elif vix_level and vix_level > 20:
            factors["immediate"].append({
                "type":   "vix_elevated",
                "level":  vix_level,
                "action": "reduce_position_size_25pct",
            })

        # COT extremes
        for ccy, data in (pkg.cot_data or {}).items():
            if data.get("squeeze_risk") == "high":
                factors["immediate"].append({
                    "type":         "cot_extreme",
                    "currency":     ccy,
                    "positioning":  data.get("positioning_signal"),
                    "action":       "flag_squeeze_risk",
                })

        # High-impact calendar events within 2h
        cal = pkg.calendar_data or {}
        events = cal.get("events", [])
        now_iso = datetime.now(timezone.utc).isoformat()
        for evt in events:
            if evt.get("impact") == "high" and evt.get("time_utc", "") >= now_iso:
                try:
                    from datetime import timedelta
                    evt_t = datetime.fromisoformat(evt["time_utc"].replace("Z", "+00:00"))
                    hrs   = (evt_t - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hrs <= 2:
                        factors["immediate"].append({
                            "type":     "imminent_high_impact_event",
                            "event":    evt["event_name"],
                            "currency": evt["currency"],
                            "hours_away": round(hrs, 1),
                            "action":   "avoid_new_entries_on_affected_pairs",
                        })
                except Exception:
                    pass

        return factors


# ── Standalone runner ──────────────────────────────────────────────────────────

def build_and_save(window: str, save: bool = True) -> IntelligencePackage:
    """
    Build and optionally save an intelligence package for a window.
    Used by intelligence_agent_prep.py and the scheduler.
    """
    builder = IntelligencePackageBuilder()
    pkg = builder.build(window=window)
    if save:
        pkg.save()
    return pkg


def get_latest_package(window: Optional[str] = None) -> Optional[Dict]:
    """
    Retrieve the most recently assembled package from v2/intelligence.db.
    Used by the validator at trade time (zero latency).
    """
    try:
        conn = _get_intel_db()
        if window:
            row = conn.execute(
                "SELECT id, window, generated_at, package_text "
                "FROM intelligence_packages WHERE window = ? "
                "ORDER BY generated_at DESC LIMIT 1",
                (window,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, window, generated_at, package_text "
                "FROM intelligence_packages ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_latest_package failed: {e}")
        return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    win = sys.argv[1] if len(sys.argv) > 1 else "london"
    logger.info(f"Building intelligence package for window: {win}")
    pkg = build_and_save(win, save=True)
    print(f"\nPackage ID: {pkg.id}")
    print(f"Window: {pkg.window}")
    print(f"Sources: {pkg.data_sources_used}")
    print(f"Failed: {pkg.data_sources_failed}")
    print(f"Assembly time: {pkg.assembly_time_ms}ms")
    print(f"\n--- PACKAGE PREVIEW (first 500 chars) ---")
    print(pkg.to_markdown()[:500])
