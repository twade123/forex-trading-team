"""Rebuild /tmp/cohort_indicator_blocks.json for the full 30d validator-family
cohort, using the shared validator_block_builder.

Pulls every closed trade from live_trades in the 30d window (scout / snipe_direct
/ manual), fetches M15 candles ending at each entry, computes the same fields
the LIVE trading_cycle path computes via AdvancedIndicators + Indicators +
generate_market_picture, and writes the canonical block string built by
validator_block_builder.build_validator_indicator_block().

Output JSON is what ghost_replay_v2.py reads. After this rebuild, every ghost
replay sees the SAME block content the live validator gets — adding stoch K/D,
ADX, range_position, and prior-session H/L distance that were missing before.

Usage:
    python3 scripts/rebuild_cohort_indicators_30d.py [--limit N]
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)
sys.path.insert(0, os.path.join(SOURCE_DIR, "scripts"))

import pandas as pd

from indicators import Indicators
from indicators_advanced import AdvancedIndicators
from scripts.build_cohort_indicators import (
    fetch_raw_candles,
    derive_cross_state,
    derive_fan_state,
    derive_cascade_phase,
    derive_exhaustion,
    classify_session,
    build_block_for_trade,
)

OUT_PATH = "/tmp/cohort_indicator_blocks.json"
DB = "~/Jarvis/Database/v2/trading_forex.db"


def fetch_30d_cohort():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, pair, direction, entry_time
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND status='closed' AND outcome IN ('win','loss')
          AND entry_time >= datetime('now','-30 days')
          AND entry_price IS NOT NULL
        ORDER BY entry_time
    """).fetchall()
    con.close()
    out = []
    for r in rows:
        et = r["entry_time"]
        et_iso = et.replace("+00:00", "").replace("Z", "").replace(" ", "T").split(".")[0]
        out.append((str(r["id"]), r["pair"], r["direction"].upper(), et_iso))
    return out


def _strip_pd_series(d):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = {sk: sv for sk, sv in v.items() if not isinstance(sv, pd.Series)}
        else:
            out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Only first N trades")
    args = ap.parse_args()

    cohort = fetch_30d_cohort()
    if args.limit:
        cohort = cohort[: args.limit]
    print(f"Rebuilding {len(cohort)} cohort blocks with the shared validator_block_builder ...")

    out = {}
    ok = err = 0
    for i, (trade_id, pair, direction, entry_iso) in enumerate(cohort, 1):
        try:
            candles = fetch_raw_candles(pair, entry_iso)
            if candles is None:
                out[trade_id] = {"error": "insufficient_candles", "pair": pair, "direction": direction}
                err += 1
                continue
            engine = Indicators(candles)
            engine.compute_emas()
            crosses = derive_cross_state(engine.df)
            fan = derive_fan_state(engine.df)
            phase = derive_cascade_phase(crosses, fan["fan_ordered"])
            ind = _strip_pd_series(engine.compute_all())
            try:
                ind_advanced = _strip_pd_series(AdvancedIndicators(candles).compute_all())
            except Exception:
                ind_advanced = {}
            exhaustion = derive_exhaustion(direction, ind, engine.df)
            session_block = classify_session(pair, entry_iso)
            block_text = build_block_for_trade(
                pair, direction, candles, fan, crosses, phase,
                ind, ind_advanced, exhaustion, session_block,
            )
            out[trade_id] = {
                "pair": pair, "direction": direction,
                "phase": phase, "fan": fan, "crosses": crosses,
                "exhaustion": exhaustion,
                "session_blocked": session_block[0],
                "session_reason": session_block[1],
                "block_text": block_text,
            }
            ok += 1
        except Exception as e:
            out[trade_id] = {"error": str(e), "pair": pair, "direction": direction}
            err += 1
        if i % 25 == 0 or i == len(cohort):
            print(f"  [{i}/{len(cohort)}] ok={ok} err={err}")

    Path(OUT_PATH).write_text(json.dumps(out, indent=2, default=str))
    print(f"\nWrote {OUT_PATH} ({len(out)} entries; ok={ok}, err={err})")


if __name__ == "__main__":
    main()
