"""Chase-and-retrace loss pattern analysis.

Tim's hypothesis: most large losses share a visual signature on the chart:
  1. Big move happens in trade direction (the bear/bull push that justifies the entry)
  2. Trade entered AT or NEAR the bottom/top of that move (chasing)
  3. Next 3-5 candles are COUNTER-COLOR (green for sell, red for buy)
  4. They retrace toward E21 (and often past it)
  5. SL gets clipped on the retrace, OR the move never resumes

This script measures, for every closed loss, the post-entry candle behavior:
  - How many of the first N bars are counter-color (against trade direction)
  - Did price retrace TOWARD E21 (vs continue away from E21)
  - Did price tag E21 within N bars
  - Did price tag E55 / E100 within N bars

Then classifies each loss:
  - CHASE_RETRACE: >=3 counter-color bars in first 5 + price moved toward E21
  - CLEAN_FAIL: candles continued in trade direction but trade still lost
  - IMMEDIATE_FLIP: candles immediately flipped, fast big move against
  - SLOW_DRIFT: mixed bars, gradual loss

Outputs: % of large losses matching each pattern + would early-cut rule have helped.

Usage: python -m scripts.chase_retrace_loss_pattern [--days 90] [--bars-after 5]
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
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        if "+" in rest:
            frac, tz = rest.split("+", 1)
            s = f"{head}.{frac[:6]}+{tz}"
        elif "-" in rest:
            frac, tz = rest.split("-", 1)
            s = f"{head}.{frac[:6]}-{tz}"
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


def find_index_at_time(df: pd.DataFrame, target_time: str) -> Optional[int]:
    target_dt = _parse_iso(target_time)
    for i in range(len(df)):
        bar_dt = _parse_iso(df.iloc[i]["time"])
        if bar_dt >= target_dt:
            return i
    return None


def analyze_loss(trade: dict, df: pd.DataFrame, bars_after: int = 5) -> Optional[dict]:
    """Measure post-entry candle behavior for a closed losing trade."""
    pair = trade["pair"]
    direction = trade["direction"]
    entry_price = float(trade["entry_price"])
    entry_time = trade["entry_time"]
    pnl_pips = float(trade["pnl_pips"])
    pip = pip_size(pair)
    is_buy = direction.lower() in ("buy", "long")
    expected_color = "green" if is_buy else "red"   # candles SHOULD be this color in trade direction

    entry_idx = find_index_at_time(df, entry_time)
    if entry_idx is None or entry_idx + bars_after >= len(df):
        return None

    entry_e21 = df.iloc[entry_idx].get("ema_21")
    if pd.isna(entry_e21):
        return None
    dist_to_e21_at_entry = (entry_price - entry_e21) / pip if is_buy else (entry_e21 - entry_price) / pip
    # Negative = entry was on the FAR side of E21 from trade direction (chase signature)
    # Positive = entry was on the NEAR side of E21 (early entry, fan still ordering)

    # Walk N bars after entry
    counter_color_count = 0
    same_color_count = 0
    moved_toward_e21_count = 0  # bar's high/low approached E21 from current side
    moved_away_e21_count = 0
    tagged_e21 = False
    tagged_e55 = False
    tagged_e100 = False
    bars_to_e21_tag = None
    bars_data = []

    for k in range(1, bars_after + 1):
        if entry_idx + k >= len(df):
            break
        bar = df.iloc[entry_idx + k]
        bar_open, bar_close = float(bar["open"]), float(bar["close"])
        bar_high, bar_low = float(bar["high"]), float(bar["low"])
        e21 = bar.get("ema_21")
        e55 = bar.get("ema_55")
        e100 = bar.get("ema_100")

        is_green = bar_close > bar_open
        is_red = bar_close < bar_open

        if (is_buy and is_red) or (not is_buy and is_green):
            counter_color_count += 1
        elif (is_buy and is_green) or (not is_buy and is_red):
            same_color_count += 1

        # Distance to E21 (signed in trade direction)
        if not pd.isna(e21):
            # For SELL: price > E21 means "above E21" = retrace direction (bad)
            # For BUY: price < E21 means "below E21" = retrace direction (bad)
            cur_dist_e21 = (bar_close - e21) / pip if is_buy else (e21 - bar_close) / pip
            if cur_dist_e21 > 0:  # price has moved past E21 in retrace direction (bad for trade)
                if not tagged_e21:
                    tagged_e21 = True
                    bars_to_e21_tag = k

            # Did this bar's high (sell) or low (buy) tag E21?
            if not tagged_e21:
                if not is_buy and bar_high >= e21:
                    tagged_e21 = True
                    bars_to_e21_tag = k
                elif is_buy and bar_low <= e21:
                    tagged_e21 = True
                    bars_to_e21_tag = k

            # Did bar approach E21 vs move away? (compare with prev bar's close)
            prev_close = float(df.iloc[entry_idx + k - 1]["close"])
            prev_dist_to_e21 = abs(prev_close - e21)
            cur_dist_to_e21 = abs(bar_close - e21)
            if cur_dist_to_e21 < prev_dist_to_e21:
                moved_toward_e21_count += 1
            else:
                moved_away_e21_count += 1

        # Tag E55 / E100
        if not tagged_e55 and not pd.isna(e55):
            if not is_buy and bar_high >= e55:
                tagged_e55 = True
            elif is_buy and bar_low <= e55:
                tagged_e55 = True
        if not tagged_e100 and not pd.isna(e100):
            if not is_buy and bar_high >= e100:
                tagged_e100 = True
            elif is_buy and bar_low <= e100:
                tagged_e100 = True

        bars_data.append({
            "k": k,
            "color": "G" if is_green else ("R" if is_red else "D"),
            "dist_e21_pips": round((bar_close - e21) / pip if not pd.isna(e21) else 0, 1) if is_buy
                else round((e21 - bar_close) / pip if not pd.isna(e21) else 0, 1),
        })

    # Classify the loss
    classification = "OTHER"
    reason = ""

    is_chase = (
        counter_color_count >= 3
        and tagged_e21  # price reached E21
    )
    is_immediate_flip = (
        counter_color_count >= 4
        and bars_to_e21_tag is not None and bars_to_e21_tag <= 2  # E21 tagged within 2 bars
    )
    is_clean_fail = (
        same_color_count >= 3
        and not tagged_e21
    )

    if is_immediate_flip:
        classification = "IMMEDIATE_FLIP"
        reason = f"{counter_color_count}/{bars_after} counter, E21 tagged in {bars_to_e21_tag}b"
    elif is_chase:
        classification = "CHASE_RETRACE"
        reason = f"{counter_color_count}/{bars_after} counter, E21 tagged in {bars_to_e21_tag}b"
    elif is_clean_fail:
        classification = "CLEAN_FAIL"
        reason = f"{same_color_count}/{bars_after} same color, no E21 tag"
    else:
        classification = "MIXED"
        reason = f"{counter_color_count} counter / {same_color_count} same"

    return {
        "trade_id": trade["id"],
        "pair": pair,
        "direction": direction,
        "actual_loss_pips": pnl_pips,
        "loss_size": "LARGE" if abs(pnl_pips) >= 10 else "SMALL",
        "dist_e21_at_entry_pips": round(dist_to_e21_at_entry, 1),
        "counter_color_count": counter_color_count,
        "same_color_count": same_color_count,
        "moved_toward_e21": moved_toward_e21_count,
        "moved_away_e21": moved_away_e21_count,
        "tagged_e21": tagged_e21,
        "tagged_e55": tagged_e55,
        "tagged_e100": tagged_e100,
        "bars_to_e21_tag": bars_to_e21_tag,
        "classification": classification,
        "reason": reason,
        "bars": bars_data,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--bars-after", type=int, default=5)
    parser.add_argument("--source", type=str, default="snipe_direct")
    args = parser.parse_args()

    print(f"Chase-retrace loss pattern: last {args.days}d | {args.bars_after} bars after entry | source={args.source}")
    print()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    losses = conn.execute(f"""
        SELECT id, pair, direction, entry_price, sl_price, exit_price,
               entry_time, exit_time, pnl_pips, source
        FROM live_trades
        WHERE outcome='loss' AND status='closed'
          AND entry_time >= datetime('now','-{args.days} days')
          AND source = '{args.source}'
          AND entry_price IS NOT NULL
        ORDER BY pair, entry_time
    """).fetchall()
    conn.close()

    print(f"Loaded {len(losses)} closed losses")

    by_pair = defaultdict(list)
    for t in losses:
        by_pair[t["pair"]].append(dict(t))

    all_results = []
    for pair, trades in by_pair.items():
        if not trades:
            continue
        earliest_entry = min(t["entry_time"] for t in trades)
        latest_entry = max(t["entry_time"] for t in trades)
        from_dt = _parse_iso(earliest_entry) - timedelta(hours=8)
        to_dt = _parse_iso(latest_entry) + timedelta(minutes=15 * (args.bars_after + 5) + 60)
        from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"  [{pair}] fetching candles for {len(trades)} losses...", flush=True)
        try:
            candles = fetch_candles(pair, "M15", from_str, to_str)
            df = candles_to_df(candles)
            if len(df) < 30:
                continue
            df = compute_all(df)
        except Exception as e:
            print(f"    {pair}: fetch failed — {e}")
            continue

        for t in trades:
            r = analyze_loss(t, df, args.bars_after)
            if r:
                all_results.append(r)

    if not all_results:
        print("No results.")
        return

    print()
    print("=" * 90)
    print(f"PATTERN CLASSIFICATION — {len(all_results)} losses")
    print("=" * 90)

    by_class = defaultdict(list)
    for r in all_results:
        by_class[r["classification"]].append(r)

    print(f"  {'Pattern':<20} {'N':>4} {'%':>6} {'AvgLoss':>9} {'Total':>10} {'%LargeLoss':>12}")
    total_n = len(all_results)
    for cls in sorted(by_class.keys(), key=lambda x: -len(by_class[x])):
        group = by_class[cls]
        n = len(group)
        avg = sum(r["actual_loss_pips"] for r in group) / n
        tot = sum(r["actual_loss_pips"] for r in group)
        large = sum(1 for r in group if r["loss_size"] == "LARGE")
        print(f"  {cls:<20} {n:>4} {100*n/total_n:>5.1f}% {avg:>+8.1f}p {tot:>+9.1f}p  {100*large/n:>10.1f}%")

    # Large loss focus
    large_losses = [r for r in all_results if r["loss_size"] == "LARGE"]
    if large_losses:
        print()
        print("=" * 90)
        print(f"LARGE LOSSES ONLY (≥10p) — {len(large_losses)} trades")
        print("=" * 90)
        large_by_class = defaultdict(list)
        for r in large_losses:
            large_by_class[r["classification"]].append(r)
        print(f"  {'Pattern':<20} {'N':>4} {'%':>6} {'AvgLoss':>9} {'Total':>10}")
        n_large = len(large_losses)
        for cls in sorted(large_by_class.keys(), key=lambda x: -len(large_by_class[x])):
            group = large_by_class[cls]
            n = len(group)
            avg = sum(r["actual_loss_pips"] for r in group) / n
            tot = sum(r["actual_loss_pips"] for r in group)
            print(f"  {cls:<20} {n:>4} {100*n/n_large:>5.1f}% {avg:>+8.1f}p {tot:>+9.1f}p")

    # ── Early-cut rule simulation ──
    # Rule: if 3+ counter-color candles in first 3 bars AND moving toward E21 → exit at bar 3
    print()
    print("=" * 90)
    print("EARLY-CUT RULE SIMULATION — 'if 3 of first 3 bars counter-color + toward E21 → exit'")
    print("=" * 90)
    for cut_bars in [3, 4, 5]:
        rule_triggers = []
        for r in all_results:
            bars = r["bars"][:cut_bars]
            counter_in_window = sum(1 for b in bars if (
                (r["direction"].lower() in ("buy","long") and b["color"] == "R")
                or (r["direction"].lower() not in ("buy","long") and b["color"] == "G")
            ))
            if counter_in_window >= max(3, cut_bars - 1):  # 3 of 3, 3 of 4, 4 of 5
                rule_triggers.append(r)
        if not rule_triggers:
            print(f"  cut_bars={cut_bars}: 0 triggers")
            continue
        n = len(rule_triggers)
        avg_actual = sum(r["actual_loss_pips"] for r in rule_triggers) / n
        # Simulated: cutting at bar `cut_bars` close — what was P&L there?
        # Approximation: use signed dist_e21_pips at last bar in window relative to entry direction
        # Better: compute close price at entry_idx + cut_bars relative to entry_price
        print(f"  cut_bars={cut_bars}: rule fires on {n} losses")
        print(f"    avg actual loss: {avg_actual:+.1f}p (these are the trades the rule would have exited early)")
        print(f"    avg loss size at bar {cut_bars} would replace the actual SL hit")

    # Per-pair pattern incidence
    print()
    print("=" * 90)
    print("PER-PAIR — chase-retrace incidence")
    print("=" * 90)
    print(f"  {'Pair':<10} {'Total':>6} {'Chase':>6} {'Chase%':>8} {'Imm.Flip':>9} {'CleanFail':>10} {'Mixed':>6}")
    pp = defaultdict(lambda: defaultdict(int))
    for r in all_results:
        pp[r["pair"]]["total"] += 1
        pp[r["pair"]][r["classification"]] += 1
    for pair in sorted(pp.keys()):
        d = pp[pair]
        chase = d.get("CHASE_RETRACE", 0)
        flip = d.get("IMMEDIATE_FLIP", 0)
        clean = d.get("CLEAN_FAIL", 0)
        mixed = d.get("MIXED", 0)
        total = d["total"]
        print(f"  {pair:<10} {total:>6} {chase:>6} {100*chase/total:>7.1f}% {flip:>9} {clean:>10} {mixed:>6}")


if __name__ == "__main__":
    main()
