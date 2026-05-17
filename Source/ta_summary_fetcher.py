#!/usr/bin/env python3
"""
ta_summary_fetcher.py — Pull TA summary from flight_recorder for a pair.

Sources:
  - flight_recorder.db (TA_COMPUTE, TA_LLM, VALIDATOR_CALL stages)
  - v2/trading_forex.db (user_chart_annotations, watch_suggestions)

No caching — always pulls latest data from live DB.
"""

import json
import logging
import os
import sqlite3
from typing import Dict, List, Optional

from db_pool import get_trading_forex
from db_connection import get_db

logger = logging.getLogger(__name__)

_source_dir   = os.path.dirname(os.path.abspath(__file__))
_team_dir     = os.path.dirname(_source_dir)
_jarvis_dir   = os.path.dirname(_team_dir)

FLIGHT_DB_PATH  = os.path.join(_source_dir, "flight_recorder.db")


def _classify_rsi(rsi: Optional[float]) -> str:
    if rsi is None: return "unknown"
    if rsi > 70:    return "overbought"
    if rsi > 60:    return "bullish"
    if rsi < 30:    return "oversold"
    if rsi < 40:    return "bearish"
    return "neutral"


def _classify_adx(adx: Optional[float]) -> str:
    if adx is None: return "unknown"
    if adx > 50:    return "very_strong"
    if adx > 25:    return "strong"
    if adx > 20:    return "developing"
    return "weak"


def _check_ema_alignment(data: dict) -> str:
    e20, e50, e200 = data.get("ema_20"), data.get("ema_50"), data.get("ema_200")
    if None in (e20, e50, e200): return "unknown"
    if e20 > e50 > e200:  return "bullish_aligned"
    if e20 < e50 < e200:  return "bearish_aligned"
    return "mixed"


def _get_latest_indicators(conn: sqlite3.Connection, pair: str) -> Dict:
    """Pull last TA_COMPUTE stage payload for the pair."""
    try:
        row = conn.execute("""
            SELECT data, timestamp FROM flight_log
            WHERE instrument = ? AND stage = 'TA_COMPUTE' AND status = 'ok'
            ORDER BY timestamp DESC LIMIT 1
        """, (pair,)).fetchone()

        if not row or not row["data"]:
            return {}

        data = json.loads(row["data"])
        return {
            "timestamp":        row["timestamp"],
            "rsi_14":           data.get("rsi_14"),
            "rsi_signal":       _classify_rsi(data.get("rsi_14")),
            "macd_histogram":   data.get("macd_histogram"),
            "macd_signal":      "bullish" if (data.get("macd_histogram") or 0) > 0 else "bearish",
            "adx_14":           data.get("adx_14"),
            "adx_trend_strength": _classify_adx(data.get("adx_14")),
            "bb_position":      data.get("bb_percent_b"),
            "ema_20":           data.get("ema_20"),
            "ema_50":           data.get("ema_50"),
            "ema_200":          data.get("ema_200"),
            "ema_alignment":    _check_ema_alignment(data),
            "stochastic_k":     data.get("stoch_k"),
            "stochastic_d":     data.get("stoch_d"),
            "atr_14":           data.get("atr_14"),
        }
    except Exception as e:
        logger.debug(f"[{pair}] TA indicators fetch error: {e}")
        return {}


def _get_latest_patterns(conn: sqlite3.Connection, pair: str) -> Dict:
    """Pull last TA_LLM stage — LLM pattern recognition output."""
    try:
        row = conn.execute("""
            SELECT data, timestamp FROM flight_log
            WHERE instrument = ? AND stage = 'TA_LLM' AND status = 'ok'
            ORDER BY timestamp DESC LIMIT 1
        """, (pair,)).fetchone()

        if not row or not row["data"]:
            return {}

        data = json.loads(row["data"])
        return {
            "timestamp":        row["timestamp"],
            "active_patterns":  data.get("patterns", []),
            "trend_direction":  data.get("trend", "unknown"),
            "key_observation":  data.get("summary", ""),
        }
    except Exception as e:
        logger.debug(f"[{pair}] TA patterns fetch error: {e}")
        return {}


def _get_key_levels(conn: sqlite3.Connection, pair: str) -> Dict:
    """Pull S/R levels from latest TA_LLM or VALIDATOR_CALL data."""
    try:
        row = conn.execute("""
            SELECT data FROM flight_log
            WHERE instrument = ? AND stage IN ('TA_LLM', 'VALIDATOR_CALL') AND status = 'ok'
            ORDER BY timestamp DESC LIMIT 1
        """, (pair,)).fetchone()

        if not row or not row["data"]:
            return {}

        data = json.loads(row["data"])
        return {
            "resistance_levels": data.get("resistance", []),
            "support_levels":    data.get("support", []),
            "pivot_point":       data.get("pivot"),
        }
    except Exception as e:
        logger.debug(f"[{pair}] Key levels fetch error: {e}")
        return {}


def _get_chart_annotations(pair: str, user_id: int = 1) -> List[Dict]:
    """Pull user's active chart annotations from v2/trading_forex.db."""
    try:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT annotation_type, note, price, timeframe, created_at
            FROM user_chart_annotations
            WHERE user_id = ? AND pair = ?
              AND active = 1
              AND (expires_at IS NULL OR expires_at > datetime('now'))
            ORDER BY created_at DESC LIMIT 5
        """, (user_id, pair)).fetchall()
        return [
            {
                "type":        r["annotation_type"],
                "content":     r["note"],
                "price_level": r["price"],
                "timeframe":   r["timeframe"],
                "created_at":  r["created_at"],
            }
            for r in rows
        ]
    except Exception as e:
        logger.debug(f"[{pair}] Chart annotations fetch error: {e}")
        return []


def _get_active_watches(pair: str, user_id: int = 1) -> List[Dict]:
    """Pull active watch conditions from v2/trading_forex.db watch_suggestions."""
    try:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT suggestion_type, conditions, raw_suggestion,
                   validator_confidence, created_at, user_thesis
            FROM watch_suggestions
            WHERE instrument = ? AND status = 'watching'
              AND (expires_at IS NULL OR expires_at > datetime('now'))
            ORDER BY created_at DESC LIMIT 5
        """, (pair,)).fetchall()
        return [
            {
                "type":       r["suggestion_type"],
                "conditions": r["conditions"],
                "summary":    r["raw_suggestion"],
                "confidence": r["validator_confidence"],
                "thesis":     r["user_thesis"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    except Exception as e:
        logger.debug(f"[{pair}] Active watches fetch error: {e}")
        return []


def fetch_ta_summary(pair: str, user_id: int = 1) -> Dict:
    """
    Pull last completed TA cycle for a pair from flight_recorder.
    Returns structured TA summary for the intelligence package.
    """
    try:
        with get_db(FLIGHT_DB_PATH, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            indicators = _get_latest_indicators(conn, pair)
            patterns   = _get_latest_patterns(conn, pair)
            key_levels = _get_key_levels(conn, pair)
    except Exception as e:
        logger.warning(f"[{pair}] flight_recorder open error: {e}")
        indicators, patterns, key_levels = {}, {}, {}

    annotations   = _get_chart_annotations(pair, user_id)
    active_watches = _get_active_watches(pair, user_id)

    return {
        "pair":             pair,
        "indicators":       indicators,
        "patterns":         patterns,
        "key_levels":       key_levels,
        "trend_direction":  patterns.get("trend_direction"),
        "chart_annotations": annotations,
        "active_watches":   active_watches,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for pair in ["EUR_USD", "GBP_USD", "USD_JPY"]:
        summary = fetch_ta_summary(pair)
        ind = summary["indicators"]
        print(f"{pair}: rsi={ind.get('rsi_14')} ema_align={ind.get('ema_alignment')} trend={summary['trend_direction']}")
