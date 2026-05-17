"""
BIG LOSS PATTERN AUDIT — find the common shape across all major losses
post-2026-04-17 tune, then test earned-lock save rules across a wide grid.

Tim's complaint (2026-05-08, repeated multiple times):
"Every goddamn loss is extreme because you can't find a fucking way to save it.
The trade goes negative, stays negative or pops positive briefly, then rides
all the way back negative until the trend changes. Find the pattern."

This script:
  1. Pulls every closed scout/snipe loss in last 30 days
  2. For each, fetches M15 candles entry → exit
  3. Walks bar-by-bar to characterize the pattern
  4. Buckets the losses by shape
  5. Tests earned-lock saves across N × lock grid (NO pair exclusions)
  6. Reports per-trade saves so we can SEE which combo catches the pattern

Output:
  - Visual timeline of worst losses (printed)
  - Pattern bucket counts
  - N × lock save matrix (saves, killed winners, net pip delta)
  - Best combo recommendation
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient  # noqa: E402

DB_PATH = "~/Jarvis/Database/v2/trading_forex.db"
DAYS = 30
LOSS_THRESHOLD = 10.0  # absolute pips
N_VALUES = [3, 4, 5, 6, 8]
LOCK_VALUES = [0.0, 0.5, 1.0, 1.5]


def parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.replace("Z", "").rstrip()
    if "." in s:
        base, frac = s.split(".", 1)
        s = f"{base}.{frac[:6]}"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_et(dt: Optional[datetime]) -> str:
    if not dt:
        return "?"
    return (dt - timedelta(hours=4)).strftime("%m-%d %H:%M ET")


@dataclass
class Trade:
    id: str
    pair: str
    direction: str
    entry_price: float
    sl_price: Optional[float]
    pnl_pips: float
    source: str
    entry_time: datetime
    exit_time: datetime
    exit_trigger: str

    @property
    def pip(self) -> float:
        return 0.01 if "JPY" in self.pair else 0.0001

    @property
    def is_buy(self) -> bool:
        return self.direction == "buy"


def load_trades() -> List[Trade]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT id, pair, direction, entry_price, sl_price, source,
               entry_time, exit_time, pnl_pips,
               COALESCE(exit_trigger, '') AS exit_trigger
        FROM live_trades
        WHERE status='closed'
          AND exit_time >= datetime('now','-{DAYS} days')
          AND source IN ('scout','snipe_direct')
          AND pnl_pips IS NOT NULL
          AND ABS(pnl_pips) >= {LOSS_THRESHOLD}
          AND pnl_pips < 0
        ORDER BY exit_time DESC
        """
    ).fetchall()
    conn.close()
    out: List[Trade] = []
    for r in rows:
        et = parse_iso(r["entry_time"])
        xt = parse_iso(r["exit_time"])
        if not et or not xt:
            continue
        out.append(
            Trade(
                id=str(r["id"]),
                pair=r["pair"],
                direction=r["direction"],
                entry_price=float(r["entry_price"]),
                sl_price=float(r["sl_price"]) if r["sl_price"] else None,
                pnl_pips=float(r["pnl_pips"]),
                source=r["source"],
                entry_time=et,
                exit_time=xt,
                exit_trigger=r["exit_trigger"],
            )
        )
    return out


def load_winners_for_killcheck() -> List[Trade]:
    """Also pull recent winners — needed to compute kill rate of save rules."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT id, pair, direction, entry_price, sl_price, source,
               entry_time, exit_time, pnl_pips,
               COALESCE(exit_trigger, '') AS exit_trigger
        FROM live_trades
        WHERE status='closed'
          AND exit_time >= datetime('now','-{DAYS} days')
          AND source IN ('scout','snipe_direct')
          AND pnl_pips > 0
        ORDER BY exit_time DESC
        """
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        et = parse_iso(r["entry_time"])
        xt = parse_iso(r["exit_time"])
        if not et or not xt:
            continue
        out.append(
            Trade(
                id=str(r["id"]),
                pair=r["pair"],
                direction=r["direction"],
                entry_price=float(r["entry_price"]),
                sl_price=float(r["sl_price"]) if r["sl_price"] else None,
                pnl_pips=float(r["pnl_pips"]),
                source=r["source"],
                entry_time=et,
                exit_time=xt,
                exit_trigger=r["exit_trigger"],
            )
        )
    return out


def fetch_m15_per_pair(trades: List[Trade], oanda: OandaClient) -> Dict[str, List[Dict]]:
    pair_ranges: Dict[str, Tuple[datetime, datetime]] = {}
    for t in trades:
        lo, hi = pair_ranges.get(t.pair, (t.entry_time, t.exit_time))
        pair_ranges[t.pair] = (min(lo, t.entry_time), max(hi, t.exit_time))
    cache: Dict[str, List[Dict]] = {}
    for pair, (lo, hi) in sorted(pair_ranges.items()):
        start = lo - timedelta(hours=1)
        end = hi + timedelta(hours=1)
        try:
            candles = oanda.fetch_candles_range(
                instrument=pair, granularity="M15",
                from_time=start, to_time=end, price="M",
            )
            candles = [c for c in candles if c.get("complete", True)]
            cache[pair] = candles
            print(f"  {pair}: {len(candles)} M15 bars")
        except Exception as e:
            print(f"  {pair}: FAILED {e}")
            cache[pair] = []
    return cache


def candles_in_window(candles, entry, exit_):
    out = []
    for c in candles:
        cdt = parse_iso(c.get("time", ""))
        if not cdt:
            continue
        bar_close = cdt + timedelta(minutes=15)
        if bar_close <= entry or cdt >= exit_:
            continue
        out.append((cdt, c))
    return out


def trade_bars(t: Trade, candles_cache):
    return candles_in_window(candles_cache.get(t.pair, []), t.entry_time, t.exit_time)


def characterize(t: Trade, bars) -> Dict:
    """Walk bar-by-bar and compute pattern metrics."""
    if not bars:
        return {"n_bars": 0, "pattern": "no_data"}
    closes = []
    highs = []
    lows = []
    for _dt, c in bars:
        h = float(c["mid"]["h"]); lo = float(c["mid"]["l"]); cl = float(c["mid"]["c"])
        if t.is_buy:
            closes.append((cl - t.entry_price) / t.pip)
            highs.append((h - t.entry_price) / t.pip)
            lows.append((lo - t.entry_price) / t.pip)
        else:
            closes.append((t.entry_price - cl) / t.pip)
            highs.append((t.entry_price - lo) / t.pip)
            lows.append((t.entry_price - h) / t.pip)
    # Find first positive close
    first_pos_close_bar = next((i for i, p in enumerate(closes) if p > 0), -1)
    first_pos_high_bar = next((i for i, p in enumerate(highs) if p > 0), -1)
    # Bars negative before first positive close
    bars_neg_before_first_pos = first_pos_close_bar if first_pos_close_bar >= 0 else len(bars)
    # MFE
    mfe = max(highs) if highs else 0.0
    mfe_bar = highs.index(mfe) if highs else -1
    # MAE
    mae = min(lows) if lows else 0.0
    # After-positive trajectory
    if first_pos_close_bar >= 0 and first_pos_close_bar < len(bars) - 1:
        post_pos_closes = closes[first_pos_close_bar + 1:]
        bars_pos_run = next(
            (i for i, p in enumerate(post_pos_closes) if p < 0), len(post_pos_closes)
        )
        max_after_pos = max(closes[first_pos_close_bar:]) if first_pos_close_bar < len(closes) else 0
    else:
        bars_pos_run = 0
        max_after_pos = 0
    # Pattern bucket
    if first_pos_close_bar < 0:
        pattern = "never_positive"
    elif first_pos_close_bar == 0:
        pattern = "positive_at_entry_then_negative"
    elif bars_neg_before_first_pos >= 5:
        pattern = "long_negative_then_brief_positive_then_loss"
    else:
        pattern = "short_negative_then_brief_positive_then_loss"
    return {
        "n_bars": len(bars),
        "first_pos_close_bar": first_pos_close_bar,
        "first_pos_high_bar": first_pos_high_bar,
        "bars_neg_before_first_pos": bars_neg_before_first_pos,
        "bars_pos_run_after_first_pos": bars_pos_run,
        "mfe": round(mfe, 1),
        "mfe_bar": mfe_bar,
        "mae": round(mae, 1),
        "max_after_pos": round(max_after_pos, 1),
        "pattern": pattern,
        "closes": [round(p, 1) for p in closes],
        "highs": [round(p, 1) for p in highs],
        "lows": [round(p, 1) for p in lows],
    }


def simulate_lock(t: Trade, bars, n_neg: int, lock_pips: float) -> Dict:
    """Earned-lock simulation. Lock at entry+lock_pips when N consecutive
    negative closes have occurred AND current bar closes positive."""
    pip = t.pip
    entry = t.entry_price
    is_buy = t.is_buy
    actual = t.pnl_pips
    if not bars:
        return {"sim_pnl": actual, "fired": False, "exit_bar": None}
    consec_neg = 0
    has_earned = False
    locked = False
    lock_price = entry + lock_pips * pip if is_buy else entry - lock_pips * pip
    for idx, (_dt, c) in enumerate(bars):
        h = float(c["mid"]["h"]); lo = float(c["mid"]["l"]); cl = float(c["mid"]["c"])
        if is_buy:
            close_pips = (cl - entry) / pip
        else:
            close_pips = (entry - cl) / pip
        if not locked:
            if close_pips < 0:
                consec_neg += 1
                if consec_neg >= n_neg:
                    has_earned = True
            elif close_pips > 0:
                if has_earned:
                    locked = True
                consec_neg = 0
        else:
            if is_buy:
                hit = lo <= lock_price
            else:
                hit = h >= lock_price
            if hit:
                return {"sim_pnl": lock_pips, "fired": True, "exit_bar": idx}
    return {"sim_pnl": actual, "fired": locked, "exit_bar": None}


def main():
    print("=" * 90)
    print(f"BIG LOSS PATTERN AUDIT — last {DAYS}d, |loss| >= {LOSS_THRESHOLD}p")
    print("=" * 90)

    losses = load_trades()
    print(f"\nLoaded {len(losses)} losses ≥{LOSS_THRESHOLD}p")
    winners = load_winners_for_killcheck()
    print(f"Loaded {len(winners)} winners (for kill-rate computation)")
    all_trades = losses + winners

    print(f"\nFetching M15 history per pair ...")
    oanda = OandaClient()
    cache = fetch_m15_per_pair(all_trades, oanda)

    # Characterize each loss
    print(f"\nCharacterizing each loss ...")
    loss_data = []
    for t in losses:
        bars = trade_bars(t, cache)
        metrics = characterize(t, bars)
        loss_data.append((t, bars, metrics))

    # Pattern bucket counts
    bucket_counts = defaultdict(int)
    bucket_pips = defaultdict(float)
    for t, _bars, m in loss_data:
        bucket_counts[m["pattern"]] += 1
        bucket_pips[m["pattern"]] += t.pnl_pips
    print(f"\nPATTERN BUCKETS (across {len(losses)} big losses):")
    for bucket in sorted(bucket_counts, key=lambda b: bucket_pips[b]):
        print(f"  {bucket:<50} n={bucket_counts[bucket]:>3}  net={bucket_pips[bucket]:+8.1f}p")

    # Visual timeline of the WORST 8 losses
    print(f"\n" + "=" * 90)
    print("VISUAL TIMELINE — worst 8 losses (M15 close pips, bar by bar)")
    print("=" * 90)
    worst = sorted(loss_data, key=lambda x: x[0].pnl_pips)[:8]
    for t, bars, m in worst:
        print(f"\n  {t.id} {t.pair} {t.direction} {to_et(t.entry_time)} → {to_et(t.exit_time)}  pnl={t.pnl_pips:+.1f}p")
        print(f"    Pattern: {m['pattern']}, bars_neg_before_pos={m['bars_neg_before_first_pos']}, "
              f"MFE={m['mfe']:+.1f}p (bar {m['mfe_bar']}), MAE={m['mae']:+.1f}p")
        # Print closes as a small bar chart
        line = "    closes: "
        for i, p in enumerate(m['closes']):
            mark = '+' if p > 0 else '-'
            line += mark
        print(line + f"  [{len(m['closes'])} bars]")
        print(f"    first 12 closes: {m['closes'][:12]}")
        if m['first_pos_close_bar'] >= 0:
            print(f"    first POS close at bar {m['first_pos_close_bar']}: {m['closes'][m['first_pos_close_bar']]:+.1f}p")

    # Run save rule grid
    print(f"\n" + "=" * 90)
    print("EARNED-LOCK SAVE GRID — N × lock_pips, NO pair exclusions")
    print("=" * 90)
    print(f"\n{'N':>3}  {'lock':>5}  {'fires_loss':>11}  {'saves':>6}  {'saved_p':>9}  "
          f"{'fires_win':>10}  {'kills':>6}  {'killed_p':>10}  {'net_p':>9}")
    print("-" * 90)
    grid_results = []
    for n_neg in N_VALUES:
        for lock in LOCK_VALUES:
            saves = 0; saved_p = 0.0
            kills = 0; killed_p = 0.0
            fires_loss = 0; fires_win = 0
            saved_ids = []
            killed_ids = []
            for t, bars, _m in loss_data:
                r = simulate_lock(t, bars, n_neg, lock)
                if r["fired"] and r["exit_bar"] is not None:
                    fires_loss += 1
                    saves += 1
                    saved_p += r["sim_pnl"] - t.pnl_pips
                    saved_ids.append(t.id)
            for t in winners:
                bars = trade_bars(t, cache)
                if not bars:
                    continue
                r = simulate_lock(t, bars, n_neg, lock)
                if r["fired"] and r["exit_bar"] is not None:
                    fires_win += 1
                    if t.pnl_pips > lock + 0.01:
                        kills += 1
                        killed_p += r["sim_pnl"] - t.pnl_pips
                        killed_ids.append(t.id)
            net = saved_p + killed_p
            grid_results.append({
                "N": n_neg, "lock": lock,
                "fires_loss": fires_loss, "saves": saves, "saved_p": round(saved_p, 1),
                "fires_win": fires_win, "kills": kills, "killed_p": round(killed_p, 1),
                "net_p": round(net, 1),
                "saved_ids": saved_ids, "killed_ids": killed_ids,
            })
            print(f"{n_neg:>3}  {lock:>5.1f}  {fires_loss:>11}  {saves:>6}  {saved_p:>+9.1f}  "
                  f"{fires_win:>10}  {kills:>6}  {killed_p:>+10.1f}  {net:>+9.1f}")
    print()

    best = max(grid_results, key=lambda r: r["net_p"])
    print("=" * 90)
    print("BEST COMBO BY NET PIP DELTA")
    print("=" * 90)
    print(f"  N={best['N']} bars negative, lock={best['lock']}p")
    print(f"  Saves {best['saves']} losses for +{best['saved_p']}p")
    print(f"  Kills {best['kills']} winners for {best['killed_p']:+.1f}p")
    print(f"  NET: {best['net_p']:+.1f}p over last {DAYS} days")
    print(f"  Saved trade IDs: {best['saved_ids']}")
    print(f"  Killed trade IDs: {best['killed_ids']}")

    out = {
        "days": DAYS,
        "loss_count": len(losses),
        "winner_count": len(winners),
        "buckets": dict(bucket_counts),
        "grid": grid_results,
        "best": best,
        "loss_details": [
            {"id": t.id, "pair": t.pair, "pnl": t.pnl_pips, "metrics": m}
            for t, _b, m in loss_data
        ],
    }
    fp = os.path.join(HERE, f"big_loss_audit_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json")
    with open(fp, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nFull JSON: {fp}")


if __name__ == "__main__":
    main()
