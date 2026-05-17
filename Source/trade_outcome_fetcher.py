#!/usr/bin/env python3
"""
trade_outcome_fetcher.py — Recent closed trade outcomes for the intelligence package.

Source: v2/trading_forex.db (live_trades table)
No caching — always pulls latest from DB.
"""

import logging
import sqlite3
from typing import Dict, List

from db_pool import get_trading_forex

logger = logging.getLogger(__name__)


def _classify_result(pips: float) -> str:
    if pips > 2:  return "W"
    if pips < -2: return "L"
    return "BE"


def _calculate_streak(trades: List[Dict]) -> str:
    if not trades:
        return "none"
    streak_type = trades[0]["result"]
    count = 0
    for t in trades:
        if t["result"] == streak_type:
            count += 1
        else:
            break
    return f"{streak_type}{count}"


def fetch_recent_trades(pair: str, user_id: int = 1, limit: int = 5) -> Dict:
    """
    Pull last N closed trades for a pair from v2/trading_forex.db.
    Returns structured trade outcomes for the intelligence package.

    Column mapping (v2 schema):
      direction: 'long' / 'short'
      pnl_pips:  pips result
      exit_trigger / exit_method: close reason
    """
    try:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT id, pair, direction, entry_price, exit_price,
                   pnl_pips, setup_code, confluence_score,
                   exit_trigger, exit_method, exit_time, outcome
            FROM live_trades
            WHERE pair = ? AND status = 'closed'
            ORDER BY exit_time DESC LIMIT ?
        """, (pair, limit)).fetchall()
    except Exception as e:
        logger.warning(f"[{pair}] fetch_recent_trades DB error: {e}")
        return {"pair": pair, "recent_trades": [], "summary": _empty_summary()}

    trades = []
    for r in rows:
        pips = r["pnl_pips"] or 0.0
        # Normalize direction: long→buy, short→sell
        direction = "buy" if r["direction"] == "long" else "sell"
        close_reason = r["exit_trigger"] or r["exit_method"] or r["outcome"] or "unknown"
        trades.append({
            "trade_id":            r["id"],
            "direction":           direction,
            "result":              _classify_result(pips),
            "pips":                round(pips, 1),
            "setup_code":          r["setup_code"],
            "confidence_at_entry": r["confluence_score"],
            "close_reason":        close_reason,
            "closed_at":           r["exit_time"],
        })

    wins   = sum(1 for t in trades if t["result"] == "W")
    losses = sum(1 for t in trades if t["result"] == "L")
    total_pips = sum(t["pips"] for t in trades)

    return {
        "pair": pair,
        "recent_trades": trades,
        "summary": {
            "count":      len(trades),
            "wins":       wins,
            "losses":     losses,
            "breakeven":  len(trades) - wins - losses,
            "total_pips": round(total_pips, 1),
            "win_rate":   round(wins / len(trades) * 100, 1) if trades else 0.0,
            "streak":     _calculate_streak(trades),
        },
    }


def _empty_summary() -> Dict:
    return {"count": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "total_pips": 0.0, "win_rate": 0.0, "streak": "none"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for pair in ["EUR_USD", "GBP_USD", "USD_JPY"]:
        result = fetch_recent_trades(pair)
        s = result["summary"]
        print(f"{pair}: {s['wins']}W/{s['losses']}L | {s['total_pips']:+.1f} pips | WR {s['win_rate']:.0f}% | streak {s['streak']}")
