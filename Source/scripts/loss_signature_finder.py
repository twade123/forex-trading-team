"""Loss signature finder — find a pattern that's high-precision on losses.

Tim's constraint: identify the loss signature without losing the edge.
That means the rule must fire on most losses but NOT on winners.

Approach:
  1. Pull ALL closed validator-snipe trades (wins AND losses) in 90 days
  2. For each, measure post-entry candle behavior in first N bars:
     - counter_color_count (candles opposite to trade direction)
     - moved_toward_e21_count
     - tagged_e21
     - tagged_e55
     - tagged_e100
     - max_adverse_pips_in_first_N
  3. Sweep candidate rules:
     - "≥3 of first 3 bars counter-color"
     - "≥3 of first 4 bars counter-color"
     - "≥4 of first 5 bars counter-color"
     - "≥3 counter + tagged E21 within N bars"
     - etc.
  4. For each rule:
     - precision = wins-that-fire-rule vs losses-that-fire-rule
     - recall on losses = % of losses caught
     - simulated exit P&L = sum of pnl_at_bar_N for all trades that fire
  5. Pick rule with highest (precision × recall × net_pnl_savings)

Usage: python -m scripts.loss_signature_finder [--days 90] [--bars-after 5]
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


def measure_post_entry(trade: dict, df: pd.DataFrame, bars_after: int = 5) -> Optional[dict]:
    """Measure post-entry candle behavior for any closed trade (win or loss)."""
    pair = trade["pair"]
    direction = trade["direction"]
    entry_price = float(trade["entry_price"])
    entry_time = trade["entry_time"]
    pnl_pips = float(trade["pnl_pips"])
    outcome = trade["outcome"]
    pip = pip_size(pair)
    is_buy = direction.lower() in ("buy", "long")

    entry_idx = find_index_at_time(df, entry_time)
    if entry_idx is None or entry_idx + bars_after >= len(df):
        return None

    # Per-bar measurements
    bars = []
    counter_color_count = 0
    same_color_count = 0
    moved_toward_e21_count = 0
    tagged_e21_at_bar = None
    tagged_e55_at_bar = None
    max_adverse_in_window = 0.0
    max_favorable_in_window = 0.0

    for k in range(1, bars_after + 1):
        if entry_idx + k >= len(df):
            break
        bar = df.iloc[entry_idx + k]
        bar_open, bar_close = float(bar["open"]), float(bar["close"])
        bar_high, bar_low = float(bar["high"]), float(bar["low"])
        e21 = bar.get("ema_21")
        e55 = bar.get("ema_55")

        is_green = bar_close > bar_open
        is_red = bar_close < bar_open

        # Counter-color = candle direction OPPOSITE to trade direction
        # SELL: trade direction = down, counter-color = green (up close)
        # BUY:  trade direction = up,   counter-color = red (down close)
        if (is_buy and is_red) or (not is_buy and is_green):
            counter_color_count += 1
        elif (is_buy and is_green) or (not is_buy and is_red):
            same_color_count += 1

        # Adverse / favorable
        if is_buy:
            adv = (entry_price - bar_low) / pip
            fav = (bar_high - entry_price) / pip
        else:
            adv = (bar_high - entry_price) / pip
            fav = (entry_price - bar_low) / pip
        if adv > max_adverse_in_window:
            max_adverse_in_window = adv
        if fav > max_favorable_in_window:
            max_favorable_in_window = fav

        # E21 tag (price reaches E21 from trade direction side)
        if tagged_e21_at_bar is None and not pd.isna(e21):
            if not is_buy and bar_high >= e21:
                tagged_e21_at_bar = k
            elif is_buy and bar_low <= e21:
                tagged_e21_at_bar = k

        # E55 tag
        if tagged_e55_at_bar is None and not pd.isna(e55):
            if not is_buy and bar_high >= e55:
                tagged_e55_at_bar = k
            elif is_buy and bar_low <= e55:
                tagged_e55_at_bar = k

        # Did this bar move toward E21 vs prev bar?
        if not pd.isna(e21):
            prev_close = float(df.iloc[entry_idx + k - 1]["close"])
            if abs(bar_close - e21) < abs(prev_close - e21):
                moved_toward_e21_count += 1

        # P&L at close of this bar (for early-cut simulation)
        pnl_at_bar = ((bar_close - entry_price) / pip) if is_buy else ((entry_price - bar_close) / pip)

        # EMA structure: distance E21-E55 (in pips), positive = ordered with trade direction
        e21_e55_dist_pips = 0.0
        if not pd.isna(e21) and not pd.isna(e55):
            # For SELL: ordered means E21 < E55, so dist = (E55 - E21) / pip = positive
            # For BUY: ordered means E21 > E55, so dist = (E21 - E55) / pip = positive
            raw_dist = (e55 - e21) if not is_buy else (e21 - e55)
            e21_e55_dist_pips = raw_dist / pip

        # BB width
        bb_u = bar.get("bb_upper")
        bb_l = bar.get("bb_lower")
        bb_w = ((bb_u - bb_l) / pip) if (not pd.isna(bb_u) and not pd.isna(bb_l)) else 0

        # RSI
        rsi = bar.get("rsi", 50)

        bars.append({
            "k": k,
            "color": "G" if is_green else ("R" if is_red else "D"),
            "is_counter": (is_buy and is_red) or (not is_buy and is_green),
            "pnl_at_close": round(pnl_at_bar, 1),
            "e21_e55_dist": round(e21_e55_dist_pips, 1),
            "bb_width_pips": round(bb_w, 1),
            "rsi": round(float(rsi) if not pd.isna(rsi) else 50, 1),
        })

    return {
        "trade_id": trade["id"],
        "pair": pair,
        "direction": direction,
        "outcome": outcome,
        "actual_pnl_pips": pnl_pips,
        "counter_color_count": counter_color_count,
        "same_color_count": same_color_count,
        "moved_toward_e21": moved_toward_e21_count,
        "tagged_e21_at_bar": tagged_e21_at_bar,
        "tagged_e55_at_bar": tagged_e55_at_bar,
        "max_adverse_pips": round(max_adverse_in_window, 1),
        "max_favorable_pips": round(max_favorable_in_window, 1),
        "bars": bars,
    }


def evaluate_rule(results: List[dict], rule_fn, cut_bar: int) -> dict:
    """Evaluate a candidate rule against all measurements.

    rule_fn(measurement) -> bool — returns True if rule fires
    cut_bar — at which bar to exit if rule fires (use bar.pnl_at_close)
    """
    fires_loss = []
    fires_win = []
    misses_loss = []
    misses_win = []
    for r in results:
        triggered = rule_fn(r)
        if triggered:
            if r["outcome"] == "loss":
                fires_loss.append(r)
            else:
                fires_win.append(r)
        else:
            if r["outcome"] == "loss":
                misses_loss.append(r)
            else:
                misses_win.append(r)

    # Simulated exit P&L at cut_bar for triggering trades
    # Use pnl_at_close of bar `cut_bar` (1-indexed, so bars[cut_bar-1])
    def exit_pnl(r):
        idx = cut_bar - 1
        if idx < 0 or idx >= len(r["bars"]):
            return r["actual_pnl_pips"]
        return r["bars"][idx]["pnl_at_close"]

    fires_loss_simulated = [exit_pnl(r) for r in fires_loss]
    fires_win_simulated = [exit_pnl(r) for r in fires_win]

    n_fire = len(fires_loss) + len(fires_win)
    n_loss_total = len(fires_loss) + len(misses_loss)
    n_win_total = len(fires_win) + len(misses_win)

    return {
        "fire_count": n_fire,
        "fires_loss": len(fires_loss),
        "fires_win": len(fires_win),
        "precision_loss": (len(fires_loss) / n_fire) if n_fire else 0,
        "recall_loss": (len(fires_loss) / n_loss_total) if n_loss_total else 0,
        "winner_kill_rate": (len(fires_win) / n_win_total) if n_win_total else 0,
        # P&L impact:
        # - Trades that triggered the rule: actual_pnl gets replaced with exit_pnl
        # - For losses: savings = (actual loss) - (exit pnl). Negative actual_pnl, exit_pnl typically less negative or positive.
        # - For wins: cost = (actual win) - (exit pnl). Positive actual, exit_pnl typically smaller.
        "loss_pnl_actual": sum(r["actual_pnl_pips"] for r in fires_loss),
        "loss_pnl_simulated": sum(fires_loss_simulated),
        "loss_savings": sum(r["actual_pnl_pips"] for r in fires_loss) - sum(fires_loss_simulated),
        # Wait — savings is (actual - sim). For a loss: actual = -16, sim = -3 → savings = -13 (means we kept 13p).
        # That sign is confusing. Let me redefine: savings = sim - actual (positive = better outcome)
        "win_pnl_actual": sum(r["actual_pnl_pips"] for r in fires_win),
        "win_pnl_simulated": sum(fires_win_simulated),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--bars-after", type=int, default=5)
    parser.add_argument("--source", type=str, default="snipe_direct")
    args = parser.parse_args()

    print(f"Loss signature finder: last {args.days}d | {args.bars_after} bars after entry | source={args.source}")
    print()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    trades = conn.execute(f"""
        SELECT id, pair, direction, entry_price, entry_time, exit_time,
               pnl_pips, outcome, source
        FROM live_trades
        WHERE outcome IN ('win','loss') AND status='closed'
          AND entry_time >= datetime('now','-{args.days} days')
          AND source = '{args.source}'
          AND entry_price IS NOT NULL
        ORDER BY pair, entry_time
    """).fetchall()
    conn.close()

    n_total = len(trades)
    n_loss = sum(1 for t in trades if t["outcome"] == "loss")
    n_win = sum(1 for t in trades if t["outcome"] == "win")
    print(f"Loaded {n_total} closed trades: {n_win} wins, {n_loss} losses")

    by_pair = defaultdict(list)
    for t in trades:
        by_pair[t["pair"]].append(dict(t))

    all_results = []
    for pair, pair_trades in by_pair.items():
        if not pair_trades:
            continue
        earliest = min(t["entry_time"] for t in pair_trades)
        latest = max(t["entry_time"] for t in pair_trades)
        from_dt = _parse_iso(earliest) - timedelta(hours=8)
        to_dt = _parse_iso(latest) + timedelta(minutes=15 * (args.bars_after + 5) + 60)
        from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"  [{pair}] fetching candles for {len(pair_trades)} trades...", flush=True)
        try:
            candles = fetch_candles(pair, "M15", from_str, to_str)
            df = candles_to_df(candles)
            if len(df) < 30:
                continue
            df = compute_all(df)
        except Exception as e:
            print(f"    {pair}: fetch failed — {e}")
            continue

        for t in pair_trades:
            r = measure_post_entry(t, df, args.bars_after)
            if r:
                all_results.append(r)

    if not all_results:
        print("No results.")
        return

    n_loss_a = sum(1 for r in all_results if r["outcome"] == "loss")
    n_win_a = sum(1 for r in all_results if r["outcome"] == "win")
    print()
    print(f"Analyzed {len(all_results)} trades: {n_win_a} wins, {n_loss_a} losses")

    # ── Define candidate rules ──
    # Each rule takes a measurement dict and returns True if it fires.
    rules = {}

    def make_color_rule(min_counter: int, in_first_n: int):
        def rule(r):
            counter_in_window = sum(1 for b in r["bars"][:in_first_n] if b["is_counter"])
            return counter_in_window >= min_counter
        return rule

    def make_color_plus_e21(min_counter: int, in_first_n: int, e21_by_bar: int):
        def rule(r):
            counter_in_window = sum(1 for b in r["bars"][:in_first_n] if b["is_counter"])
            tag = r["tagged_e21_at_bar"]
            return counter_in_window >= min_counter and tag is not None and tag <= e21_by_bar
        return rule

    def make_adverse_rule(min_adv_pips: float, by_bar: int):
        def rule(r):
            # Find max adverse in first `by_bar` bars
            max_adv = 0
            for b in r["bars"][:by_bar]:
                pnl = b["pnl_at_close"]
                # pnl_at_close negative = trade is in loss at that bar
                if -pnl > max_adv:
                    max_adv = -pnl
            return max_adv >= min_adv_pips
        return rule

    rules["3of3 counter"] = (make_color_rule(3, 3), 3)
    rules["3of4 counter"] = (make_color_rule(3, 4), 4)
    rules["4of5 counter"] = (make_color_rule(4, 5), 5)
    rules["3of5 counter"] = (make_color_rule(3, 5), 5)
    rules["3of3 + E21<=3"] = (make_color_plus_e21(3, 3, 3), 3)
    rules["3of4 + E21<=4"] = (make_color_plus_e21(3, 4, 4), 4)
    rules["adv>=10p by bar 3"] = (make_adverse_rule(10.0, 3), 3)
    rules["adv>=15p by bar 5"] = (make_adverse_rule(15.0, 5), 5)
    rules["adv>=10p + 3of4 counter"] = ((lambda r: make_adverse_rule(10.0, 4)(r) and make_color_rule(3, 4)(r)), 4)

    # ── Evaluate each rule ──
    print()
    print("=" * 110)
    print("CANDIDATE RULE EVALUATION — looking for high precision on losses + low winner-kill rate")
    print("=" * 110)
    print(f"  {'Rule':<28} {'Cut@':>5} {'Fires':>6} {'L hit':>6} {'W hit':>6} {'Prec':>6} {'Recall':>7} {'WinKill':>8} {'Sim P&L':>9} {'NetSwing':>10}")
    print("-" * 110)

    for name, (rule_fn, cut_bar) in rules.items():
        ev = evaluate_rule(all_results, rule_fn, cut_bar)
        # Net swing: change in total P&L if we replace actual outcomes for triggering trades with simulated exit
        # = (sum sim) - (sum actual) for triggers
        actual_total = ev["loss_pnl_actual"] + ev["win_pnl_actual"]
        sim_total = ev["loss_pnl_simulated"] + ev["win_pnl_simulated"]
        net_swing = sim_total - actual_total  # positive = better
        print(f"  {name:<28} {cut_bar:>5} {ev['fire_count']:>6} {ev['fires_loss']:>6} {ev['fires_win']:>6} "
              f"{100*ev['precision_loss']:>5.1f}% {100*ev['recall_loss']:>6.1f}% {100*ev['winner_kill_rate']:>7.1f}% "
              f"{sim_total:>+8.1f}p {net_swing:>+9.1f}p")

    print("-" * 110)
    print()
    print("Glossary:")
    print("  L hit / W hit = losses / wins where rule fires")
    print("  Prec  = precision = % of fires that are losses (high = rule mostly catches losses)")
    print("  Recall = % of all losses caught by the rule")
    print("  WinKill = % of all wins killed by the rule (LOW IS BETTER — preserves edge)")
    print("  Sim P&L = total simulated P&L for triggers (sum of exit-at-bar-N pnl)")
    print("  NetSwing = (sim P&L) - (actual P&L of triggers); +ve = rule helps overall")

    # ── 1. THRESHOLD SWEEP — adverse excursion at bar 3 ──
    print()
    print("=" * 110)
    print("1. THRESHOLD SWEEP — adv>=Xp by bar 3, vary X")
    print("=" * 110)
    print(f"  {'Threshold':>10} {'Fires':>6} {'L hit':>6} {'W hit':>6} {'Prec':>6} {'Recall':>7} {'WinKill':>8} {'NetSwing':>10}")
    print("-" * 110)
    for thr in [4, 6, 8, 10, 12, 15, 20]:
        rule_fn = make_adverse_rule(thr, 3)
        ev = evaluate_rule(all_results, rule_fn, 3)
        actual_total = ev["loss_pnl_actual"] + ev["win_pnl_actual"]
        sim_total = ev["loss_pnl_simulated"] + ev["win_pnl_simulated"]
        net_swing = sim_total - actual_total
        print(f"  adv>={thr:>3}p   {ev['fire_count']:>6} {ev['fires_loss']:>6} {ev['fires_win']:>6} "
              f"{100*ev['precision_loss']:>5.1f}% {100*ev['recall_loss']:>6.1f}% {100*ev['winner_kill_rate']:>7.1f}% "
              f"{net_swing:>+9.1f}p")

    # ── 2. BAR SWEEP — adv>=10p by bar X, vary X ──
    print()
    print("=" * 110)
    print("2. BAR SWEEP — adv>=10p by bar X, vary X")
    print("=" * 110)
    print(f"  {'BarN':>5} {'Fires':>6} {'L hit':>6} {'W hit':>6} {'Prec':>6} {'Recall':>7} {'WinKill':>8} {'NetSwing':>10}")
    print("-" * 110)
    for bn in [2, 3, 4, 5]:
        rule_fn = make_adverse_rule(10, bn)
        ev = evaluate_rule(all_results, rule_fn, bn)
        actual_total = ev["loss_pnl_actual"] + ev["win_pnl_actual"]
        sim_total = ev["loss_pnl_simulated"] + ev["win_pnl_simulated"]
        net_swing = sim_total - actual_total
        print(f"  bar{bn:>2}   {ev['fire_count']:>6} {ev['fires_loss']:>6} {ev['fires_win']:>6} "
              f"{100*ev['precision_loss']:>5.1f}% {100*ev['recall_loss']:>6.1f}% {100*ev['winner_kill_rate']:>7.1f}% "
              f"{net_swing:>+9.1f}p")

    # ── 3. PAIR-STRATIFY — apply adv>=10p by bar 3 per pair ──
    print()
    print("=" * 110)
    print("3. PAIR-STRATIFY — adv>=10p by bar 3, per-pair performance")
    print("=" * 110)
    print(f"  {'Pair':<10} {'Trades':>7} {'Fires':>6} {'L hit':>6} {'W hit':>6} {'Prec':>6} {'WinKill':>8} {'NetSwing':>10}")
    print("-" * 110)
    by_pair_results = defaultdict(list)
    for r in all_results:
        by_pair_results[r["pair"]].append(r)
    rule_fn = make_adverse_rule(10, 3)
    for pair in sorted(by_pair_results.keys()):
        group = by_pair_results[pair]
        ev = evaluate_rule(group, rule_fn, 3)
        actual_total = ev["loss_pnl_actual"] + ev["win_pnl_actual"]
        sim_total = ev["loss_pnl_simulated"] + ev["win_pnl_simulated"]
        net_swing = sim_total - actual_total
        n_loss_p = sum(1 for r in group if r["outcome"] == "loss")
        n_win_p = sum(1 for r in group if r["outcome"] == "win")
        prec = (100 * ev["precision_loss"]) if ev["fire_count"] else 0
        wk = (100 * ev["winner_kill_rate"]) if n_win_p else 0
        print(f"  {pair:<10} {len(group):>7} {ev['fire_count']:>6} {ev['fires_loss']:>6} {ev['fires_win']:>6} "
              f"{prec:>5.1f}% {wk:>7.1f}% {net_swing:>+9.1f}p")

    # ── 4. KILLED-WINS INSPECTION — what's special about the 11 wins killed? ──
    print()
    print("=" * 110)
    print("4. KILLED WINS — wins that adv>=10p by bar 3 would have wrongly cut")
    print("=" * 110)
    rule_fn = make_adverse_rule(10, 3)
    killed_wins = [r for r in all_results if r["outcome"] == "win" and rule_fn(r)]
    if not killed_wins:
        print("  (none)")
    else:
        print(f"  {len(killed_wins)} wins would be cut. Profile:")
        avg_actual_win = sum(r["actual_pnl_pips"] for r in killed_wins) / len(killed_wins)
        avg_max_adv = sum(r["max_adverse_pips"] for r in killed_wins) / len(killed_wins)
        avg_max_fav = sum(r["max_favorable_pips"] for r in killed_wins) / len(killed_wins)
        print(f"    avg actual pnl when held to TP: {avg_actual_win:+.1f}p")
        print(f"    avg max adverse in first 5 bars: {avg_max_adv:+.1f}p")
        print(f"    avg max favorable in first 5 bars: {avg_max_fav:+.1f}p")
        print()
        print(f"  {'TradeID':<8} {'Pair':<10} {'Dir':<5} {'Actual':>7} {'MaxAdv':>7} {'MaxFav':>7} {'BarN+counter':<14} {'BarN+pnl':<22}")
        print("  " + "-" * 95)
        for r in sorted(killed_wins, key=lambda x: -x["actual_pnl_pips"]):
            counter_str = "/".join("C" if b["is_counter"] else "S" for b in r["bars"][:5])
            pnl_str = " ".join(f"{b['pnl_at_close']:+.0f}" for b in r["bars"][:5])
            print(f"  {r['trade_id']:<8} {r['pair']:<10} {r['direction']:<5} "
                  f"{r['actual_pnl_pips']:>+6.1f}p {r['max_adverse_pips']:>+6.1f}p {r['max_favorable_pips']:>+6.1f}p "
                  f"{counter_str:<14} {pnl_str:<22}")
        print()
        print("  Pattern in killed wins (look for): did they go fav first, then adv? Or straight adv?")
        # Simple heuristic: did first bar pnl_at_close hit favorable before going adverse?
        went_fav_first = 0
        went_adv_first = 0
        for r in killed_wins:
            first_bars_pnl = [b["pnl_at_close"] for b in r["bars"][:3]]
            if any(p >= 2.0 for p in first_bars_pnl):
                went_fav_first += 1
            else:
                went_adv_first += 1
        print(f"  Went FAV first (≥+2p in any of first 3 bars): {went_fav_first}/{len(killed_wins)}")
        print(f"  Went ADV first (never +2p in first 3 bars):    {went_adv_first}/{len(killed_wins)}")
        if went_fav_first > 0:
            print(f"  → CANDIDATE EXEMPTION: 'don't cut if max_favorable_pips >= 2 by current bar'")

    # ── 5. EMA-STRUCTURE RULES — Tim's full mental model ──
    # Path A: ride retrace if EMAs don't cross, exit at break-even if they do
    print()
    print("=" * 110)
    print("5. EMA-STRUCTURE RULES — fan-cross + multi-signal logic (Tim's actual mental model)")
    print("=" * 110)

    # Rule: E21 crossed E55 against trade direction by bar N (fan-cross exit)
    def make_fan_cross_rule(by_bar: int):
        def rule(r):
            for b in r["bars"][:by_bar]:
                if b["e21_e55_dist"] < 0:  # negative = E21 crossed E55 against direction
                    return True
            return False
        return rule

    # Rule: E21 within X pips of E55 (warning, fan-cross imminent)
    def make_fan_compress_rule(max_dist: float, by_bar: int):
        def rule(r):
            for b in r["bars"][:by_bar]:
                if b["e21_e55_dist"] < max_dist:  # E21 dangerously close to E55
                    return True
            return False
        return rule

    # Rule: adv≥Xp AND fan compressed (multi-signal)
    def make_adv_plus_compress(adv_pips: float, max_fan_dist: float, by_bar: int):
        def rule(r):
            adv_hit = False
            compress_hit = False
            for b in r["bars"][:by_bar]:
                if -b["pnl_at_close"] >= adv_pips:
                    adv_hit = True
                if b["e21_e55_dist"] < max_fan_dist:
                    compress_hit = True
            return adv_hit and compress_hit
        return rule

    # Healthy-retrace EXEMPTION signal — when this fires, the trade is in a
    # Path A retrace (fan compressing + RSI hit counter-extreme + fan ordered).
    # Used as an OVERRIDE: "yes the rule says cut, but exempt because it's a
    # healthy retrace that's likely to recover."
    def healthy_retrace_signal(r, by_bar: int = 5) -> bool:
        if len(r["bars"]) < 2:
            return False
        is_buy_local = r["direction"].lower() in ("buy", "long")
        bb_width_at_entry = r["bars"][0]["bb_width_pips"]
        bb_constricted = False
        rsi_hit_counter_extreme = False
        fan_still_ordered = True
        for k in range(min(by_bar, len(r["bars"]))):
            b = r["bars"][k]
            # BB constricting: width contracted ≥30% from bar 0
            if bb_width_at_entry > 0 and b["bb_width_pips"] < bb_width_at_entry * 0.7:
                bb_constricted = True
            # RSI hit counter-extreme during retrace
            # SELL trade: retrace pushes price UP → RSI rises → counter-extreme = OVERBOUGHT
            # BUY trade: retrace pulls price DOWN → RSI falls → counter-extreme = OVERSOLD
            if not is_buy_local and b["rsi"] >= 65:
                rsi_hit_counter_extreme = True
            if is_buy_local and b["rsi"] <= 35:
                rsi_hit_counter_extreme = True
            # Fan still ordered (E21 on correct side of E55)
            if b["e21_e55_dist"] < 0:  # E21 crossed E55 against direction
                fan_still_ordered = False
                break
        return bb_constricted and rsi_hit_counter_extreme and fan_still_ordered

    # Rule: BB constricting + RSI counter-extreme = HEALTHY RETRACE (HOLD signal)
    # This is checking presence of the signal across all trades — should be
    # MORE common in winners than losers.
    def make_bb_rsi_signal_check(by_bar: int):
        def rule(r):
            return healthy_retrace_signal(r, by_bar)
        return rule

    ema_rules = {
        "fan-cross by bar 3": (make_fan_cross_rule(3), 3),
        "fan-cross by bar 4": (make_fan_cross_rule(4), 4),
        "fan-cross by bar 5": (make_fan_cross_rule(5), 5),
        "fan dist<2p by bar 4": (make_fan_compress_rule(2.0, 4), 4),
        "fan dist<5p by bar 4": (make_fan_compress_rule(5.0, 4), 4),
        "adv>=10p AND fan<5p by 4": (make_adv_plus_compress(10, 5.0, 4), 4),
        "adv>=10p AND fan<3p by 4": (make_adv_plus_compress(10, 3.0, 4), 4),
        "healthy-retrace signal by 5": (make_bb_rsi_signal_check(5), 5),
    }

    print(f"  {'Rule':<30} {'Cut@':>5} {'Fires':>6} {'L hit':>6} {'W hit':>6} {'Prec':>6} {'Recall':>7} {'WinKill':>8} {'NetSwing':>10}")
    print("-" * 110)
    for name, (rule_fn, cut_bar) in ema_rules.items():
        ev = evaluate_rule(all_results, rule_fn, cut_bar)
        actual_total = ev["loss_pnl_actual"] + ev["win_pnl_actual"]
        sim_total = ev["loss_pnl_simulated"] + ev["win_pnl_simulated"]
        net_swing = sim_total - actual_total
        print(f"  {name:<30} {cut_bar:>5} {ev['fire_count']:>6} {ev['fires_loss']:>6} {ev['fires_win']:>6} "
              f"{100*ev['precision_loss']:>5.1f}% {100*ev['recall_loss']:>6.1f}% {100*ev['winner_kill_rate']:>7.1f}% "
              f"{net_swing:>+9.1f}p")

    # ── 6. CONFIRMATION: bar 4 + pair exclusion vs EMA-structure baseline ──
    print()
    print("=" * 110)
    print("6. CONFIRMATION — adv>=10p by bar 4 with hostile pairs excluded")
    print("=" * 110)
    excluded_pairs = {"EUR_AUD", "AUD_USD"}
    filtered = [r for r in all_results if r["pair"] not in excluded_pairs]
    n_excluded = len(all_results) - len(filtered)
    print(f"  Excluded {n_excluded} trades from {sorted(excluded_pairs)} ({len(filtered)} remain)")

    rule_fn = make_adverse_rule(10, 4)
    ev = evaluate_rule(filtered, rule_fn, 4)
    actual_total = ev["loss_pnl_actual"] + ev["win_pnl_actual"]
    sim_total = ev["loss_pnl_simulated"] + ev["win_pnl_simulated"]
    net_swing = sim_total - actual_total
    print()
    print(f"  Rule: adv>=10p by bar 4")
    print(f"  Fires: {ev['fire_count']} ({ev['fires_loss']} L hit, {ev['fires_win']} W hit)")
    print(f"  Precision: {100*ev['precision_loss']:.1f}%   Recall: {100*ev['recall_loss']:.1f}%   WinKill: {100*ev['winner_kill_rate']:.1f}%")
    print(f"  NetSwing: {net_swing:+.1f}p over 90 days")

    # ── 7. ITERATIONS — combinations + healthy-retrace exemption ──
    print()
    print("=" * 110)
    print("7. RULE ITERATIONS — combos + healthy-retrace EXEMPTION")
    print("=" * 110)
    print(f"  {'Rule':<55} {'Cut@':>5} {'Fires':>6} {'L hit':>6} {'W hit':>6} {'Prec':>6} {'Recall':>7} {'WinKill':>8} {'NetSwing':>10}")
    print("-" * 110)

    # First — does the healthy-retrace signal correlate with WIN outcome?
    n_winners_with_signal = sum(1 for r in all_results if r["outcome"] == "win" and healthy_retrace_signal(r, 5))
    n_losers_with_signal = sum(1 for r in all_results if r["outcome"] == "loss" and healthy_retrace_signal(r, 5))
    n_winners_total = sum(1 for r in all_results if r["outcome"] == "win")
    n_losers_total = sum(1 for r in all_results if r["outcome"] == "loss")
    print(f"  HEALTHY-RETRACE SIGNAL DISTRIBUTION (separator check):")
    print(f"    {n_winners_with_signal}/{n_winners_total} winners ({100*n_winners_with_signal/max(n_winners_total,1):.1f}%) show signal")
    print(f"    {n_losers_with_signal}/{n_losers_total} losers ({100*n_losers_with_signal/max(n_losers_total,1):.1f}%) show signal")
    print()

    # Compound rules
    def make_compound(rule_fn, exempt_fn):
        """Wraps a base rule with an exemption — if exemption signal fires, skip the cut."""
        def compound(r):
            if rule_fn(r) and not exempt_fn(r):
                return True
            return False
        return compound

    def make_or_rule(rule_a, rule_b):
        def comb(r):
            return rule_a(r) or rule_b(r)
        return comb

    # Build the rules
    fan_cross_3 = make_fan_cross_rule(3)
    adv10_bar4 = make_adverse_rule(10, 4)
    healthy_signal = lambda r: healthy_retrace_signal(r, 5)

    iteration_rules = {
        "(A) adv>=10p bar 4 (baseline)":                          (adv10_bar4, 4),
        "(B) fan-cross by 3 (Tim's mental model)":                (fan_cross_3, 3),
        "(C) adv>=10p OR fan-cross (union)":                      (make_or_rule(adv10_bar4, fan_cross_3), 4),
        "(D) adv>=10p WITH healthy-retrace exempt":               (make_compound(adv10_bar4, healthy_signal), 4),
        "(E) fan-cross WITH healthy-retrace exempt":              (make_compound(fan_cross_3, healthy_signal), 3),
        "(F) (adv OR fan-cross) WITH healthy-retrace exempt":     (make_compound(make_or_rule(adv10_bar4, fan_cross_3), healthy_signal), 4),
    }

    for name, (rule_fn, cut_bar) in iteration_rules.items():
        ev = evaluate_rule(all_results, rule_fn, cut_bar)
        actual_total = ev["loss_pnl_actual"] + ev["win_pnl_actual"]
        sim_total = ev["loss_pnl_simulated"] + ev["win_pnl_simulated"]
        net_swing = sim_total - actual_total
        print(f"  {name:<55} {cut_bar:>5} {ev['fire_count']:>6} {ev['fires_loss']:>6} {ev['fires_win']:>6} "
              f"{100*ev['precision_loss']:>5.1f}% {100*ev['recall_loss']:>6.1f}% {100*ev['winner_kill_rate']:>7.1f}% "
              f"{net_swing:>+9.1f}p")

    # Same but on filtered (excluded EUR_AUD/AUD_USD)
    print()
    print("  --- WITH EUR_AUD + AUD_USD EXCLUDED (the hostile pairs) ---")
    print()
    for name, (rule_fn, cut_bar) in iteration_rules.items():
        ev = evaluate_rule(filtered, rule_fn, cut_bar)
        actual_total = ev["loss_pnl_actual"] + ev["win_pnl_actual"]
        sim_total = ev["loss_pnl_simulated"] + ev["win_pnl_simulated"]
        net_swing = sim_total - actual_total
        print(f"  {name:<55} {cut_bar:>5} {ev['fire_count']:>6} {ev['fires_loss']:>6} {ev['fires_win']:>6} "
              f"{100*ev['precision_loss']:>5.1f}% {100*ev['recall_loss']:>6.1f}% {100*ev['winner_kill_rate']:>7.1f}% "
              f"{net_swing:>+9.1f}p")

    # ── 8. WALK-FORWARD STABILITY CHECK on Rule A ──
    # Split 90-day window into 8 folds (~11 days each), evaluate Rule A per fold.
    # Rule A: adv>=10p by bar 4, with EUR_AUD + AUD_USD excluded.
    print()
    print("=" * 110)
    print("8. WALK-FORWARD STABILITY — Rule A across 8 folds (~11 days each)")
    print("   Rule: adv>=10p by bar 4, exclude EUR_AUD + AUD_USD")
    print("=" * 110)

    # Sort filtered trades by entry_time (need entry_time on results — pull from trade dict)
    # We don't have entry_time on the result dict; let me bring it through.
    # Quick fix: re-query the trades with entry_time and join by trade_id.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    id_to_entry = {}
    for row in conn.execute(f"""
        SELECT id, entry_time FROM live_trades
        WHERE outcome IN ('win','loss') AND status='closed'
          AND entry_time >= datetime('now','-{args.days} days')
          AND source = '{args.source}'
    """).fetchall():
        id_to_entry[str(row["id"])] = row["entry_time"]
    conn.close()

    # Annotate filtered with entry_time
    filtered_dated = []
    for r in filtered:
        et = id_to_entry.get(str(r["trade_id"]))
        if et:
            r2 = dict(r)
            r2["entry_time"] = et
            filtered_dated.append(r2)

    # Sort by entry_time
    filtered_dated.sort(key=lambda x: x["entry_time"])
    n_folds = 8
    fold_size = len(filtered_dated) / n_folds

    rule_fn = make_adverse_rule(10, 4)
    print(f"  {'Fold':<6} {'Days':<22} {'N':>5} {'L':>4} {'W':>4} {'Fires':>6} {'L hit':>6} {'W hit':>6} {'Prec':>6} {'WinKill':>8} {'NetSwing':>10}")
    print("-" * 110)
    fold_swings = []
    fold_precisions = []
    fold_winkills = []
    for fold in range(n_folds):
        start = int(fold * fold_size)
        end = int((fold + 1) * fold_size) if fold < n_folds - 1 else len(filtered_dated)
        fold_data = filtered_dated[start:end]
        if not fold_data:
            continue
        first_date = fold_data[0]["entry_time"][:10]
        last_date = fold_data[-1]["entry_time"][:10]
        n_loss_f = sum(1 for r in fold_data if r["outcome"] == "loss")
        n_win_f = sum(1 for r in fold_data if r["outcome"] == "win")
        ev = evaluate_rule(fold_data, rule_fn, 4)
        actual_total = ev["loss_pnl_actual"] + ev["win_pnl_actual"]
        sim_total = ev["loss_pnl_simulated"] + ev["win_pnl_simulated"]
        net_swing = sim_total - actual_total
        prec = 100*ev["precision_loss"] if ev["fire_count"] else 0
        wk = 100*ev["winner_kill_rate"] if n_win_f else 0
        date_str = f"{first_date[5:]}→{last_date[5:]}"
        print(f"  F{fold:<5} {date_str:<22} {len(fold_data):>5} {n_loss_f:>4} {n_win_f:>4} "
              f"{ev['fire_count']:>6} {ev['fires_loss']:>6} {ev['fires_win']:>6} "
              f"{prec:>5.1f}% {wk:>7.1f}% {net_swing:>+9.1f}p")
        fold_swings.append(net_swing)
        fold_precisions.append(prec)
        fold_winkills.append(wk)

    # Aggregate stability stats
    if fold_swings:
        n_folds_pos = sum(1 for s in fold_swings if s > 0)
        n_folds_neg = sum(1 for s in fold_swings if s < 0)
        mean_swing = sum(fold_swings) / len(fold_swings)
        sd_swing = (sum((s - mean_swing)**2 for s in fold_swings) / len(fold_swings)) ** 0.5
        mean_prec = sum(fold_precisions) / len(fold_precisions)
        mean_wk = sum(fold_winkills) / len(fold_winkills)
        print("-" * 110)
        print(f"\n  STABILITY VERDICT:")
        print(f"    Folds positive: {n_folds_pos}/{len(fold_swings)}")
        print(f"    Folds negative: {n_folds_neg}/{len(fold_swings)}")
        print(f"    Mean NetSwing:  {mean_swing:+.1f}p / fold (sd={sd_swing:.1f}p)")
        print(f"    Mean precision: {mean_prec:.1f}%")
        print(f"    Mean winkill:   {mean_wk:.1f}%")
        # Stability gates
        stable = (
            n_folds_neg <= 1
            and mean_swing > 0
            and sd_swing <= abs(mean_swing) * 1.5
        )
        verdict = "STABLE — safe to ship" if stable else "UNSTABLE — investigate variance"
        print(f"    VERDICT: {verdict}")


if __name__ == "__main__":
    main()
