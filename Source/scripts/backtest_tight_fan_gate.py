"""backtest_tight_fan_gate.py — retroactively test the tight-stale-fan gate
against all live trades from the last 30 days.

Gate rule:
  Phase == 3 AND separation_pct < 0.10% AND (cross3_bars_since >= 20 OR price_extension_atr >= 3.4)
  → BLOCK (would have been WATCH instead of TRADE_NOW)

Reuses computation from build_cohort_indicators.py.
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from scripts.build_cohort_indicators import (
    fetch_raw_candles,
    derive_cross_state,
    derive_fan_state,
    derive_cascade_phase,
    derive_exhaustion,
)
from indicators import Indicators

DB_PATH = "~/Jarvis/Database/v2/trading_forex.db"
OUT_RESULTS = "/tmp/gate_backtest_30d.json"
PROGRESS_LOG = "/tmp/gate_backtest_30d_progress.log"


def evaluate(trade_id, pair, direction, entry_time):
    try:
        candles = fetch_raw_candles(pair, entry_time)
        if not candles:
            return {"trade_id": trade_id, "error": "insufficient_candles"}
        engine = Indicators(candles)
        engine.compute_emas()
        crosses = derive_cross_state(engine.df)
        fan = derive_fan_state(engine.df)
        phase = derive_cascade_phase(crosses, fan["fan_ordered"])
        ind = engine.compute_all()
        # Strip series for safety
        for k, v in list(ind.items()):
            if isinstance(v, dict):
                ind[k] = {sk: sv for sk, sv in v.items() if not isinstance(sv, pd.Series)}
        exhaustion = derive_exhaustion(direction, ind, engine.df)

        sep_pct = fan["separation_pct"]
        c3_bars = crosses["cross3"]["bars_since_last_flip"] or 999
        ext_atr = exhaustion["price_extension_atr"]
        is_p3 = phase == 3
        is_tight = sep_pct < 0.10
        is_mature_stall = c3_bars >= 20
        is_overextended = ext_atr >= 3.4
        gate_fires = is_p3 and is_tight and (is_mature_stall or is_overextended)

        reasons = []
        if gate_fires:
            if is_mature_stall:
                reasons.append(f"mature_stall(c3={c3_bars})")
            if is_overextended:
                reasons.append(f"overextended(ext={ext_atr})")

        return {
            "trade_id": trade_id,
            "pair": pair, "direction": direction,
            "entry_time": entry_time,
            "phase": phase,
            "separation_pct": sep_pct,
            "cross3_bars_since": c3_bars,
            "price_extension_atr": ext_atr,
            "fan_state": fan["fan_state"],
            "gate_fires": gate_fires,
            "fire_reason": "|".join(reasons),
        }
    except Exception as e:
        return {"trade_id": trade_id, "error": str(e)}


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, entry_time, pnl_pips, source, base_setup
        FROM live_trades
        WHERE entry_time >= datetime('now','-30 days')
        AND pnl_pips IS NOT NULL
        ORDER BY entry_time DESC
    """).fetchall()
    print(f"Backtesting {len(trades)} trades — last 30 days")

    results = []
    Path(PROGRESS_LOG).write_text(f"[{datetime.now()}] start\n")
    t0 = time.time()
    for i, t in enumerate(trades):
        evald = evaluate(t["id"], t["pair"], t["direction"], t["entry_time"])
        if "error" not in evald:
            evald["actual_pips"] = t["pnl_pips"]
            evald["source"] = t["source"]
            evald["base_setup"] = t["base_setup"]
        results.append(evald)
        if (i + 1) % 25 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta_min = (len(trades) - i - 1) / rate / 60 if rate > 0 else 0
            line = f"[{datetime.now()}] {i+1}/{len(trades)} ({elapsed:.0f}s, ETA {eta_min:.1f} min)"
            print(line)
            with open(PROGRESS_LOG, "a") as f:
                f.write(line + "\n")
            Path(OUT_RESULTS).write_text(json.dumps(results, indent=2, default=str))

    Path(OUT_RESULTS).write_text(json.dumps(results, indent=2, default=str))
    elapsed = time.time() - t0
    print(f"\nDONE — {len(results)} trades, {elapsed/60:.1f} min")

    # Summary
    valid = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]
    blocked = [r for r in valid if r.get("gate_fires")]
    preserved = [r for r in valid if not r.get("gate_fires")]
    blocked_w = [r for r in blocked if r["actual_pips"] > 0]
    blocked_l = [r for r in blocked if r["actual_pips"] < 0]
    pres_w = [r for r in preserved if r["actual_pips"] > 0]
    pres_l = [r for r in preserved if r["actual_pips"] < 0]

    print(f"\n=== GATE BACKTEST SUMMARY (last 30 days, {len(results)} trades) ===")
    print(f"Errors (data fetch fails): {len(errors)}")
    print(f"\nGATE WOULD BLOCK: {len(blocked)}")
    if blocked:
        print(f"  → blocked WINNERS (false pos): {len(blocked_w)} | pips lost: {sum(r['actual_pips'] for r in blocked_w):+.1f}")
        print(f"  → blocked LOSERS (true pos):   {len(blocked_l)} | pips saved: {-sum(r['actual_pips'] for r in blocked_l):+.1f}")
        wr_block = len(blocked_w) / len(blocked)
        print(f"  Blocked-bucket WR: {wr_block:.1%}  (lower = gate is precise)")
    print(f"\nGATE WOULD PRESERVE: {len(preserved)}")
    if preserved:
        print(f"  → preserved WINNERS (correct): {len(pres_w)} | pips: {sum(r['actual_pips'] for r in pres_w):+.1f}")
        print(f"  → preserved LOSERS (missed):   {len(pres_l)} | pips: {sum(r['actual_pips'] for r in pres_l):+.1f}")
        wr_pres = len(pres_w) / len(preserved)
        print(f"  Preserved-bucket WR: {wr_pres:.1%}  (higher = gate doesn't hurt winners)")
    pip_net = -sum(r['actual_pips'] for r in blocked_l) - sum(r['actual_pips'] for r in blocked_w)
    print(f"\nNET PIP IMPACT IF GATE ACTIVE: {pip_net:+.1f}p over 30 days")


if __name__ == "__main__":
    main()
