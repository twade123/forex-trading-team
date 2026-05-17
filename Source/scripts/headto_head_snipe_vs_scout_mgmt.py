"""
Head-to-head: replay every snipe trade with TWO management param sets.
- Set A: current snipe.* overrides (tight trail, tight min_gap, ratchet TP)
- Set B: scout/global guardian.* defaults (no snipe overrides)

Reuses the existing candle-walk engine in optimizer/replay.py. No new
simulation logic. Each trade is replayed candle-by-candle under both
param sets and the simulated PnLs are compared.

Question being answered (from Tim, 2026-05-07):
"If we change the snipes to work like the scout trades do we really pick
up profit or do they lose? Candle-walk this forward based on the types
of snipe setups that won."

Usage:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python scripts/headto_head_snipe_vs_scout_mgmt.py [--days 30]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from typing import Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)

from optimizer.replay import (  # noqa: E402
    candle_walk_replay,
    load_candles_for_trades,
)
from optimizer.results import load_trade_snapshots  # noqa: E402
from tuning_config import tc_get_for_trade  # noqa: E402


PARAMS_TO_TEST = [
    "guardian.trailing_activation_rr",
    "guardian.trailing_atr_mult",
    "guardian.sl_min_gap_atr_mult",
    "guardian.sl_buffer_pips",
    "guardian.ratchet_step_pips",
    "guardian.profit_floor_5p",
    "guardian.profit_floor_8p",
    "guardian.profit_floor_12p",
    "guardian.profit_floor_20p",
    "gate.sl_atr_mult",
    "gate.tp_atr_mult",
]


def build_param_set(source: str) -> Dict[str, float]:
    """Resolve all PARAMS_TO_TEST under the given source. snipe_direct
    pulls snipe.* overrides; scout pulls global guardian.*/gate.* defaults."""
    out: Dict[str, float] = {}
    for p in PARAMS_TO_TEST:
        v = tc_get_for_trade(p, source, fallback=None)
        if v is not None:
            out[p] = v
    return out


def fmt_pct(x: float) -> str:
    return f"{x*100:+.0f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30,
                    help="how far back to pull snipe trades")
    ap.add_argument("--source", default="snipe_direct",
                    help="trade source to replay (snipe_direct)")
    args = ap.parse_args()

    snipe_params = build_param_set("snipe_direct")
    scout_params = build_param_set("scout")

    print("=" * 80)
    print(f"HEAD-TO-HEAD: snipe management vs scout management")
    print(f"Window: last {args.days} days, source = {args.source}")
    print("=" * 80)
    print()
    print("Param differences (snipe_direct → scout):")
    all_keys = sorted(set(snipe_params) | set(scout_params))
    for k in all_keys:
        s = snipe_params.get(k)
        c = scout_params.get(k)
        marker = " ←DIFF" if s != c else ""
        print(f"  {k:<40} snipe={s!s:>12}  scout={c!s:>12}{marker}")
    print()

    snapshots = load_trade_snapshots(days_back=args.days)
    snapshots = [s for s in snapshots if s.source == args.source]
    print(f"Loaded {len(snapshots)} {args.source} trades from last {args.days}d")
    if not snapshots:
        print("No trades to replay. Bye.")
        return

    candles_by_trade = load_candles_for_trades(snapshots)
    have_candles = sum(1 for s in snapshots if candles_by_trade.get(s.id) is not None)
    print(f"  M15 candles available for {have_candles} / {len(snapshots)} trades")
    print()

    # Replay each trade under both param sets
    rows = []
    for s in snapshots:
        candles = candles_by_trade.get(s.id)
        if candles is None or len(candles) < 2:
            continue
        a = candle_walk_replay(s, candles, snipe_params)
        b = candle_walk_replay(s, candles, scout_params)
        rows.append({
            "id": s.id,
            "pair": s.pair,
            "dir": s.direction,
            "actual_pnl": s.pnl_pips,
            "actual_outcome": s.outcome,
            "snipe_pnl": a["simulated_pnl"],
            "snipe_exit": a.get("exit_reason"),
            "scout_pnl": b["simulated_pnl"],
            "scout_exit": b.get("exit_reason"),
            "delta": b["simulated_pnl"] - a["simulated_pnl"],
        })

    if not rows:
        print("No replayable trades.")
        return

    # Aggregate
    n = len(rows)
    sum_actual = sum(r["actual_pnl"] for r in rows)
    sum_snipe_sim = sum(r["snipe_pnl"] for r in rows)
    sum_scout_sim = sum(r["scout_pnl"] for r in rows)
    delta_total = sum_scout_sim - sum_snipe_sim

    # Splits by actual outcome
    actual_winners = [r for r in rows if r["actual_pnl"] > 0]
    actual_losers = [r for r in rows if r["actual_pnl"] < 0]
    win_delta = sum(r["delta"] for r in actual_winners)
    loss_delta = sum(r["delta"] for r in actual_losers)

    print("=" * 80)
    print("AGGREGATE RESULT")
    print("=" * 80)
    print(f"  Trades replayed                 : {n}")
    print(f"  Sum ACTUAL pnl (live)           : {sum_actual:+.1f}p")
    print(f"  Sum SIM under snipe params      : {sum_snipe_sim:+.1f}p")
    print(f"  Sum SIM under scout params      : {sum_scout_sim:+.1f}p")
    print(f"  Δ (scout - snipe), aggregate    : {delta_total:+.1f}p")
    print()
    print(f"  Actual winners ({len(actual_winners)}): "
          f"under scout params they net {win_delta:+.1f}p more "
          f"({'better' if win_delta>=0 else 'worse'})")
    print(f"  Actual losers  ({len(actual_losers)}): "
          f"under scout params they net {loss_delta:+.1f}p more "
          f"({'better' if loss_delta>=0 else 'worse'})")
    print()

    # Per-pair breakdown
    by_pair: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"n": 0, "snipe": 0.0, "scout": 0.0, "delta": 0.0}
    )
    for r in rows:
        b = by_pair[r["pair"]]
        b["n"] += 1
        b["snipe"] += r["snipe_pnl"]
        b["scout"] += r["scout_pnl"]
        b["delta"] += r["delta"]
    print("Per-pair (Δ = scout - snipe):")
    print(f"  {'pair':<10}{'n':>4}{'snipe_sum':>11}{'scout_sum':>11}{'Δ':>10}")
    for pair in sorted(by_pair, key=lambda p: by_pair[p]["delta"]):
        b = by_pair[pair]
        print(f"  {pair:<10}{int(b['n']):>4}"
              f"{b['snipe']:>+11.1f}{b['scout']:>+11.1f}{b['delta']:>+10.1f}")
    print()

    # Show top 10 individual trades by delta (both directions)
    rows_sorted = sorted(rows, key=lambda r: r["delta"])
    print("Worst 10 (scout management hurts most):")
    print(f"  {'id':<7}{'pair':<10}{'actual':>8}{'snipe':>8}{'scout':>8}{'Δ':>8}  scout_exit")
    for r in rows_sorted[:10]:
        print(f"  {r['id']:<7}{r['pair']:<10}"
              f"{r['actual_pnl']:>+8.1f}{r['snipe_pnl']:>+8.1f}"
              f"{r['scout_pnl']:>+8.1f}{r['delta']:>+8.1f}  {r['scout_exit']}")
    print()
    print("Best 10 (scout management helps most):")
    for r in rows_sorted[-10:]:
        print(f"  {r['id']:<7}{r['pair']:<10}"
              f"{r['actual_pnl']:>+8.1f}{r['snipe_pnl']:>+8.1f}"
              f"{r['scout_pnl']:>+8.1f}{r['delta']:>+8.1f}  {r['scout_exit']}")
    print()

    # Save full result
    import json, datetime as _dt
    out_path = os.path.join(
        HERE,
        f"snipe_vs_scout_mgmt_{_dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    )
    with open(out_path, "w") as f:
        json.dump({
            "days": args.days,
            "n": n,
            "params_snipe": snipe_params,
            "params_scout": scout_params,
            "actual_total": sum_actual,
            "snipe_sim_total": sum_snipe_sim,
            "scout_sim_total": sum_scout_sim,
            "delta_total": delta_total,
            "rows": rows,
        }, f, indent=2)
    print(f"Full per-trade results: {out_path}")


if __name__ == "__main__":
    main()
