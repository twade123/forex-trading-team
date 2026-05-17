"""SL-widen recovery analysis — would losses have recovered if we held longer?

For each closed losing trade in the last 90 days:
  1. Fetch M15 candles from entry_time forward through exit + 60 bars
  2. At exit (SL hit), check: was fan still ORDERED?
     (E21<E55<E100 for sells, E21>E55>E100 for buys — meaning trend not invalidated)
  3. Walk forward up to 60 bars after exit:
     - Did price exceed entry_price by N pips in trade direction? (RECOVERY = WIN)
     - Did fan invalidate (E21 cross E55) before recovery? (TRUE LOSS)
     - Neither? (TIMEOUT — would still be holding)
  4. Compute aggregate stats:
     - % of fan-intact-at-SL losses that would have recovered
     - Pips recovered (counterfactual)
     - Time-to-recovery distribution

Usage: python -m scripts.sl_widen_recovery_analysis [--days 90] [--target-pips 3.0]
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtester.data_fetcher import fetch_candles
from backtester.indicators import compute_all

DB_PATH = "~/Jarvis/Database/v2/trading_forex.db"


def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def _parse_iso(s: str) -> datetime:
    """Parse ISO timestamp, tolerating nanosecond precision (truncates to micro)."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # Truncate fractional seconds to 6 digits (microseconds) — Python's
    # fromisoformat doesn't handle nanos.
    if "." in s:
        head, rest = s.split(".", 1)
        # rest looks like "901507624+00:00" — split at the +/- timezone marker
        if "+" in rest:
            frac, tz = rest.split("+", 1)
            frac = frac[:6]
            s = f"{head}.{frac}+{tz}"
        elif "-" in rest:
            frac, tz = rest.split("-", 1)
            frac = frac[:6]
            s = f"{head}.{frac}-{tz}"
        else:
            s = f"{head}.{rest[:6]}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def candles_to_df(candles: list) -> pd.DataFrame:
    rows = []
    for c in candles:
        if not c.get("complete", True):
            continue
        m = c["mid"]
        rows.append({
            "time": c["time"],
            "open": float(m["o"]),
            "high": float(m["h"]),
            "low": float(m["l"]),
            "close": float(m["c"]),
            "volume": int(c.get("volume", 0)),
        })
    return pd.DataFrame(rows)


def fan_ordered_at_idx(df: pd.DataFrame, i: int, direction: str) -> Optional[bool]:
    """True if fan ordered in trade direction at index i."""
    if i < 0 or i >= len(df):
        return None
    r = df.iloc[i]
    e21, e55, e100 = r.get("ema_21"), r.get("ema_55"), r.get("ema_100")
    if any(pd.isna(x) for x in (e21, e55, e100)):
        return None
    is_buy = direction.lower() in ("buy", "long")
    if is_buy:
        return e21 > e55 > e100
    else:
        return e21 < e55 < e100


def find_index_at_time(df: pd.DataFrame, target_time: str) -> Optional[int]:
    """Find the bar index whose time is closest to target_time (forward)."""
    if df.empty:
        return None
    target_dt = _parse_iso(target_time)
    for i in range(len(df)):
        bar_time = df.iloc[i]["time"]
        bar_dt = _parse_iso(bar_time)
        if bar_dt >= target_dt:
            return i
    return len(df) - 1


def analyze_trade(trade: dict, candles_df: pd.DataFrame, target_pips: float = 5.0,
                  forward_bars: int = 60, max_sl_mult: float = 2.0) -> dict:
    """Analyze one trade — full policy P&L if we'd held past SL until fan invalidation
    or recovery target."""
    pair = trade["pair"]
    direction = trade["direction"]
    entry_price = trade["entry_price"]
    sl_price = trade["sl_price"]
    exit_time = trade["exit_time"]
    pnl_pips = trade["pnl_pips"]
    pip = pip_size(pair)
    is_buy = direction.lower() in ("buy", "long")

    if not exit_time:
        return {"status": "no_exit_time"}
    exit_idx = find_index_at_time(candles_df, exit_time)
    if exit_idx is None:
        return {"status": "no_exit_idx"}

    # Fan ordering at exit (use 1 bar before exit to capture pre-exit state)
    check_idx = max(0, exit_idx - 1)
    fan_intact = fan_ordered_at_idx(candles_df, check_idx, direction)
    if fan_intact is None:
        return {"status": "no_fan_data"}

    # Compute hard SL cap distance (cut-bait)
    # Original SL distance from entry, then multiplied
    orig_sl_dist_pips = abs(entry_price - sl_price) / pip if sl_price else abs(pnl_pips)
    hard_cap_pips = orig_sl_dist_pips * max_sl_mult  # e.g., 2× original SL

    # Walk forward from exit_idx+1
    end_idx = min(exit_idx + 1 + forward_bars, len(candles_df))
    target_price = entry_price + (target_pips * pip if is_buy else -target_pips * pip)
    recovered_at = None
    fan_invalidated_at = None
    fan_invalidated_price = None
    hard_cap_hit_at = None
    hard_cap_hit_price = None
    max_favorable_after_exit = 0.0
    max_adverse_after_exit = 0.0
    bars_to_recovery = None

    for j in range(exit_idx + 1, end_idx):
        bar = candles_df.iloc[j]
        # Check recovery (price reaches target in trade direction)
        if is_buy:
            if bar["high"] >= target_price and recovered_at is None:
                recovered_at = j
                bars_to_recovery = j - exit_idx
            mfav = (bar["high"] - entry_price) / pip
            madv = (entry_price - bar["low"]) / pip
        else:
            if bar["low"] <= target_price and recovered_at is None:
                recovered_at = j
                bars_to_recovery = j - exit_idx
            mfav = (entry_price - bar["low"]) / pip
            madv = (bar["high"] - entry_price) / pip
        if mfav > max_favorable_after_exit:
            max_favorable_after_exit = mfav
        if madv > max_adverse_after_exit:
            max_adverse_after_exit = madv

        # Check hard SL cap hit (cut-bait — price ran against by max_sl_mult × orig_sl)
        if hard_cap_hit_at is None and madv >= hard_cap_pips:
            hard_cap_hit_at = j
            # Exit price is the cap level (price reached the cap)
            if is_buy:
                hard_cap_hit_price = entry_price - hard_cap_pips * pip
            else:
                hard_cap_hit_price = entry_price + hard_cap_pips * pip

        # Check fan invalidation
        if fan_invalidated_at is None:
            cur_fan = fan_ordered_at_idx(candles_df, j, direction)
            if cur_fan is False:
                fan_invalidated_at = j
                fan_invalidated_price = float(bar["close"])

        # Stop walking once we've hit any terminal condition
        if recovered_at is not None or hard_cap_hit_at is not None:
            break
        if recovered_at is not None and fan_invalidated_at is not None:
            break

    # ── Policy P&L ──
    # New policy: ride fan-intact losses with wide SL until either:
    #   (a) recovery target reached → take +target_pips win
    #   (b) fan invalidates → exit at the fan-cross close price
    #   (c) timeout (60 bars without either) → exit at last close
    new_policy_pnl_pips = None
    new_policy_exit_reason = None

    if not fan_intact:
        # Fan was already broken at SL — keep original loss, do not widen
        new_policy_pnl_pips = pnl_pips
        new_policy_exit_reason = "kept_original_sl_fan_broken"
    else:
        # Fan intact at SL — apply wide-SL policy with cut-bait cap
        # Priority order of exits (whichever fires first in time):
        events = []
        if recovered_at is not None:
            events.append((recovered_at, "recovery_target", target_pips))
        if hard_cap_hit_at is not None:
            events.append((hard_cap_hit_at, "hard_cap_cut_bait", -hard_cap_pips))
        if fan_invalidated_at is not None:
            fan_pnl = ((fan_invalidated_price - entry_price) / pip) if is_buy \
                     else ((entry_price - fan_invalidated_price) / pip)
            events.append((fan_invalidated_at, "fan_invalidation", fan_pnl))
        if events:
            events.sort(key=lambda x: x[0])
            _, new_policy_exit_reason, new_policy_pnl_pips = events[0]
        else:
            # Timeout — no exit triggered in 60 bars
            last_close = float(candles_df.iloc[end_idx - 1]["close"])
            new_policy_pnl_pips = ((last_close - entry_price) / pip) if is_buy \
                                  else ((entry_price - last_close) / pip)
            new_policy_exit_reason = "timeout_60bars"

    return {
        "status": "ok",
        "trade_id": trade["id"],
        "pair": pair,
        "direction": direction,
        "actual_loss_pips": pnl_pips,
        "fan_intact_at_sl": fan_intact,
        "recovered": recovered_at is not None,
        "bars_to_recovery": bars_to_recovery,
        "fan_invalidated_after_sl": fan_invalidated_at is not None,
        "bars_to_fan_invalid": (fan_invalidated_at - exit_idx) if fan_invalidated_at else None,
        "recovered_before_invalidation": (
            recovered_at is not None
            and (fan_invalidated_at is None or recovered_at < fan_invalidated_at)
        ),
        "max_favorable_after_sl_pips": round(max_favorable_after_exit, 1),
        "max_adverse_after_sl_pips": round(max_adverse_after_exit, 1),
        "orig_sl_dist_pips": round(orig_sl_dist_pips, 1),
        "hard_cap_pips": round(hard_cap_pips, 1),
        "new_policy_pnl_pips": round(new_policy_pnl_pips, 1) if new_policy_pnl_pips is not None else None,
        "new_policy_exit_reason": new_policy_exit_reason,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--target-pips", type=float, default=5.0,
                        help="How many pips past entry counts as 'recovered'")
    parser.add_argument("--forward-bars", type=int, default=60,
                        help="How many M15 bars to walk forward past exit")
    parser.add_argument("--max-sl-mult", type=float, default=2.0,
                        help="Hard cap on widened SL as multiple of original SL distance (cut-bait)")
    args = parser.parse_args()

    print(f"SL-widen recovery analysis: last {args.days}d | recovery >= {args.target_pips}p | walk-forward {args.forward_bars} bars | hard cap = {args.max_sl_mult}× orig SL (cut-bait)")
    print()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    losses = conn.execute(f"""
        SELECT id, pair, direction, entry_price, sl_price, tp_price, exit_price,
               entry_time, exit_time, pnl_pips, fan_state, fan_direction, source
        FROM live_trades
        WHERE outcome='loss' AND status='closed'
          AND entry_time >= datetime('now','-{args.days} days')
          AND source = 'snipe_direct'  -- validator-driven scout snipes only (no kronos, no manual, no raw scout)
          AND exit_time IS NOT NULL
          AND entry_price IS NOT NULL
          AND sl_price IS NOT NULL
        ORDER BY pair, entry_time
    """).fetchall()
    conn.close()

    print(f"Loaded {len(losses)} closed losses across {args.days} days")

    # Group by pair and fetch candles per pair (efficient — one fetch covers many trades)
    by_pair = defaultdict(list)
    for t in losses:
        by_pair[t["pair"]].append(dict(t))

    all_results = []
    _pair_dfs = {}  # cache for sweep re-use
    for pair, trades in by_pair.items():
        if not trades:
            continue
        # Get earliest entry and latest exit for this pair
        earliest_entry = min(t["entry_time"] for t in trades)
        latest_exit = max(t["exit_time"] for t in trades)
        # Pad: 30 bars before earliest_entry (warm up indicators) and 60 bars after latest_exit
        from_dt = _parse_iso(earliest_entry) - timedelta(hours=8)  # 30 bars warm-up
        to_dt = _parse_iso(latest_exit) + timedelta(minutes=15 * args.forward_bars + 60)
        from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        print(f"  [{pair}] fetching {(to_dt - from_dt).days}d of M15 candles for {len(trades)} losses...", flush=True)
        try:
            candles = fetch_candles(pair, "M15", from_str, to_str)
            df = candles_to_df(candles)
            if len(df) < 50:
                print(f"    {pair}: insufficient candles ({len(df)})")
                continue
            df = compute_all(df)
            _pair_dfs[pair] = df  # cache for sweep
        except Exception as e:
            print(f"    {pair}: fetch failed — {e}")
            continue

        for t in trades:
            try:
                result = analyze_trade(t, df, args.target_pips, args.forward_bars, args.max_sl_mult)
                if result["status"] == "ok":
                    all_results.append(result)
            except Exception as e:
                print(f"    trade {t['id']}: {e}")

    if not all_results:
        print("No analyzable trades.")
        return

    print()
    print("=" * 80)
    print(f"AGGREGATE RESULTS — {len(all_results)} losses analyzed")
    print("=" * 80)

    # Split by fan_intact_at_sl
    intact = [r for r in all_results if r["fan_intact_at_sl"]]
    broken = [r for r in all_results if not r["fan_intact_at_sl"]]

    def summarize(group, label):
        if not group:
            print(f"\n{label}: 0 trades")
            return
        n = len(group)
        recovered = sum(1 for r in group if r["recovered"])
        recovered_clean = sum(1 for r in group if r["recovered_before_invalidation"])
        avg_actual_loss = sum(r["actual_loss_pips"] for r in group) / n
        avg_max_fav_after = sum(r["max_favorable_after_sl_pips"] for r in group) / n
        bars_recoveries = [r["bars_to_recovery"] for r in group if r["bars_to_recovery"]]
        avg_bars = sum(bars_recoveries) / len(bars_recoveries) if bars_recoveries else 0
        median_bars = sorted(bars_recoveries)[len(bars_recoveries)//2] if bars_recoveries else 0
        print(f"\n{label}: {n} trades")
        print(f"  Recovered (price reached entry+{n}p):           {recovered}/{n} = {100*recovered/n:.1f}%")
        print(f"  Recovered BEFORE fan invalidation (clean win):  {recovered_clean}/{n} = {100*recovered_clean/n:.1f}%")
        print(f"  Avg actual loss:                                {avg_actual_loss:+.1f}p")
        print(f"  Avg max favorable AFTER SL hit:                 {avg_max_fav_after:+.1f}p")
        print(f"  Avg bars to recovery (when recovered):          {avg_bars:.1f} bars (~{avg_bars*15/60:.1f}h)")
        print(f"  Median bars to recovery:                        {median_bars} bars")

    summarize(intact, "FAN-INTACT AT SL HIT (the addressable population)")
    summarize(broken, "FAN-BROKEN AT SL HIT (true setup failure)")

    # ── POLICY P&L COMPARISON ──
    print()
    print("=" * 80)
    print("POLICY COMPARISON — old (tight SL) vs new (wide SL + fan-cross exit)")
    print("=" * 80)
    old_total = sum(r["actual_loss_pips"] for r in all_results)
    new_total = sum(r["new_policy_pnl_pips"] for r in all_results if r["new_policy_pnl_pips"] is not None)
    intact_old = sum(r["actual_loss_pips"] for r in intact)
    intact_new = sum(r["new_policy_pnl_pips"] for r in intact if r["new_policy_pnl_pips"] is not None)
    n = len(all_results)
    print(f"  All {n} losses, OLD policy total:  {old_total:+.1f}p")
    print(f"  All {n} losses, NEW policy total:  {new_total:+.1f}p")
    print(f"  NET POLICY SWING (positive = better): {new_total - old_total:+.1f}p")
    print(f"     = {(new_total - old_total)/n:+.1f}p per trade × {n} trades")
    print(f"     = {(new_total - old_total)/(args.days):+.1f}p per day over {args.days} days")
    print()
    print(f"  Fan-intact subset only ({len(intact)} trades):")
    print(f"    OLD policy: {intact_old:+.1f}p   NEW policy: {intact_new:+.1f}p")
    print(f"    Subset swing: {intact_new - intact_old:+.1f}p")
    print()
    # Exit reason breakdown
    by_exit = defaultdict(list)
    for r in all_results:
        if r["new_policy_pnl_pips"] is not None:
            by_exit[r["new_policy_exit_reason"]].append(r)
    print(f"  New-policy exit reason breakdown:")
    for reason in sorted(by_exit.keys()):
        group = by_exit[reason]
        n_g = len(group)
        avg_pnl = sum(r["new_policy_pnl_pips"] for r in group) / n_g
        tot_pnl = sum(r["new_policy_pnl_pips"] for r in group)
        print(f"    {reason:<32} n={n_g:>3}  avg={avg_pnl:>+5.1f}p  total={tot_pnl:>+7.1f}p")

    # ── RECOVERY TARGET SWEEP ──
    # Re-evaluate the policy at different recovery targets (re-using already-walked data)
    print()
    print("=" * 80)
    print("RECOVERY-TARGET SWEEP — what target_pips maximizes net policy P&L?")
    print("(Re-runs the trade walks at each target; fan-invalidation logic unchanged)")
    print("=" * 80)
    # Need to re-run analyze_trade with different targets, but we have raw candles per pair
    # already cached in _pair_dfs. Quick re-analysis.
    print(f"  {'Target':>7} {'Recoveries':>11} {'Rec%':>6} {'NewPolicy Total':>16} {'Swing vs Old':>14}")
    for target in [1.0, 2.0, 3.0, 5.0, 8.0, 12.0]:
        sweep_results = []
        for pair, trades in by_pair.items():
            if pair not in _pair_dfs:
                continue
            df = _pair_dfs[pair]
            for t in trades:
                try:
                    r = analyze_trade(t, df, target, args.forward_bars, args.max_sl_mult)
                    if r["status"] == "ok":
                        sweep_results.append(r)
                except Exception:
                    pass
        if not sweep_results:
            continue
        intact_sw = [r for r in sweep_results if r["fan_intact_at_sl"]]
        rec_sw = sum(1 for r in intact_sw if r["recovered_before_invalidation"])
        new_tot = sum(r["new_policy_pnl_pips"] for r in sweep_results)
        old_tot = sum(r["actual_loss_pips"] for r in sweep_results)
        swing = new_tot - old_tot
        print(f"  {target:>+5.1f}p {rec_sw:>10}/{len(intact_sw):<3} {100*rec_sw/len(intact_sw):>5.1f}% {new_tot:>+15.1f}p {swing:>+13.1f}p")

    # Counterfactual: if we'd widened SL on fan-intact trades and they recovered
    cf_recovered = [r for r in intact if r["recovered_before_invalidation"]]
    if cf_recovered:
        actual_loss_total = sum(r["actual_loss_pips"] for r in cf_recovered)
        cf_gain_total = len(cf_recovered) * args.target_pips  # what they'd have made if recovered
        print()
        print("=" * 80)
        print("COUNTERFACTUAL — if we'd held fan-intact losses through to recovery")
        print("=" * 80)
        print(f"  Trades that would have recovered:    {len(cf_recovered)}")
        print(f"  Actual pips lost on those trades:    {actual_loss_total:+.1f}")
        print(f"  Hypothetical pips at +{args.target_pips}p target:    +{cf_gain_total:.1f}")
        print(f"  Net swing (would-be win - actual):   {cf_gain_total - actual_loss_total:+.1f}p")
        print(f"  Per-trade swing:                     {(cf_gain_total - actual_loss_total)/len(cf_recovered):+.1f}p/trade")

    # Per-pair breakdown
    print()
    print("=" * 80)
    print("PER-PAIR BREAKDOWN (fan-intact recoveries)")
    print("=" * 80)
    by_pair_results = defaultdict(list)
    for r in intact:
        by_pair_results[r["pair"]].append(r)
    print(f"  {'Pair':<10} {'N':>4} {'Recovered':>10} {'Rec%':>6} {'Avg Loss':>9} {'Avg MFE':>8}")
    for pair in sorted(by_pair_results.keys()):
        group = by_pair_results[pair]
        n = len(group)
        rec = sum(1 for r in group if r["recovered_before_invalidation"])
        avg_loss = sum(r["actual_loss_pips"] for r in group) / n
        avg_mfe = sum(r["max_favorable_after_sl_pips"] for r in group) / n
        print(f"  {pair:<10} {n:>4} {rec:>10} {100*rec/n:>5.1f}% {avg_loss:>+8.1f}p {avg_mfe:>+7.1f}p")


if __name__ == "__main__":
    main()
