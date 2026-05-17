"""
Earned-Breakeven-Lock backtest — Tim's proposed rule.

When a trade has been net-negative on M15 close for N consecutive bars,
the next M15 close that crosses positive triggers an SL lock at
entry + lock_pips. From that point forward, if any subsequent M15 bar's
adverse extreme (low for buy, high for sell) crosses the lock price,
the trade exits at +lock_pips. Otherwise the trade's actual outcome stands.

Replays last 90 days of closed scout/snipe trades against the rule using
real OANDA M15 candle data. Reports save vs winnerkill counts and net
pip swing across a sweep of (N, lock_pips) parameter combos.

Usage:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python scripts/backtest_earned_lock.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

# Project root resolution — script lives in Source/scripts/
HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient  # noqa: E402

DB_PATH = "~/Jarvis/Database/v2/trading_forex.db"
DAYS_BACK = 90
N_VALUES = [3, 5, 8, 12]
LOCK_VALUES = [0.5, 1.0]


def parse_iso_utc(s: str) -> Optional[datetime]:
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


def to_et(dt: datetime) -> str:
    return (dt - timedelta(hours=4)).strftime("%m-%d %H:%M")


@dataclass
class Trade:
    id: str
    pair: str
    direction: str
    entry_price: float
    sl_price: Optional[float]
    exit_price: Optional[float]
    pnl_pips: float
    source: str
    entry_time: datetime
    exit_time: datetime
    exit_trigger: str
    pip_size: float = field(init=False)

    def __post_init__(self):
        self.pip_size = 0.01 if "JPY" in self.pair else 0.0001


@dataclass
class SimResult:
    simulated_pnl: float
    lock_fired: bool
    lock_bar: Optional[int]
    exit_bar: Optional[int]
    actual_pnl: float

    @property
    def delta(self) -> float:
        return self.simulated_pnl - self.actual_pnl


def load_trades() -> List[Trade]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT id, pair, direction, entry_price, sl_price, exit_price,
               source, entry_time, exit_time, pnl_pips,
               COALESCE(exit_trigger, 'unknown') as exit_trigger
        FROM live_trades
        WHERE status='closed'
          AND exit_time >= datetime('now','-{DAYS_BACK} days')
          AND source IN ('scout','snipe_direct')
          AND pnl_pips IS NOT NULL
        ORDER BY exit_time ASC
        """
    ).fetchall()
    conn.close()
    out: List[Trade] = []
    for r in rows:
        et = parse_iso_utc(r["entry_time"])
        xt = parse_iso_utc(r["exit_time"])
        if not et or not xt:
            continue
        out.append(
            Trade(
                id=str(r["id"]),
                pair=r["pair"],
                direction=r["direction"],
                entry_price=float(r["entry_price"]),
                sl_price=float(r["sl_price"]) if r["sl_price"] else None,
                exit_price=float(r["exit_price"]) if r["exit_price"] else None,
                pnl_pips=float(r["pnl_pips"]),
                source=r["source"],
                entry_time=et,
                exit_time=xt,
                exit_trigger=r["exit_trigger"],
            )
        )
    return out


def fetch_m15_per_pair(
    trades: List[Trade], oanda: OandaClient
) -> Dict[str, List[Dict]]:
    """Fetch full M15 history for each unique pair covering all trade windows.
    Cached per pair — one fetch covers every trade for that pair."""
    pair_ranges: Dict[str, Tuple[datetime, datetime]] = {}
    for t in trades:
        lo, hi = pair_ranges.get(t.pair, (t.entry_time, t.exit_time))
        pair_ranges[t.pair] = (
            min(lo, t.entry_time),
            max(hi, t.exit_time),
        )
    cache: Dict[str, List[Dict]] = {}
    for pair, (lo, hi) in sorted(pair_ranges.items()):
        # Pad 1h on each side so we always have at least 1 candle before entry
        # and 1 candle after exit for proper bar boundary handling
        start = lo - timedelta(hours=1)
        end = hi + timedelta(hours=1)
        print(
            f"  Fetching {pair} M15 from {to_et(start)} ET to {to_et(end)} ET ...",
            end=" ",
            flush=True,
        )
        try:
            candles = oanda.fetch_candles_range(
                instrument=pair,
                granularity="M15",
                from_time=start,
                to_time=end,
                price="M",
            )
        except Exception as e:
            print(f"FAILED: {e}")
            cache[pair] = []
            continue
        # Keep only complete candles
        candles = [c for c in candles if c.get("complete", True)]
        cache[pair] = candles
        print(f"{len(candles)} bars")
    return cache


def candles_in_window(
    candles: List[Dict], entry: datetime, exit_: datetime
) -> List[Tuple[datetime, Dict]]:
    """Return (datetime, candle) pairs whose M15 OPEN time falls inside
    [entry, exit_]. Each M15 bar represents the 15 minutes starting at its
    'time' field. So bar.time = 10:30 covers 10:30 to 10:45."""
    out = []
    for c in candles:
        cdt = parse_iso_utc(c.get("time", ""))
        if not cdt:
            continue
        # Bar closes 15 min after its time. Include if any portion falls in
        # the trade window. Entry-bar: bar.time <= entry < bar.time+15min
        # is the bar containing entry. Anything at or after entry is in.
        bar_close = cdt + timedelta(minutes=15)
        if bar_close <= entry:
            continue
        if cdt >= exit_:
            continue
        out.append((cdt, c))
    return out


def simulate(
    trade: Trade,
    bars: List[Tuple[datetime, Dict]],
    n_neg: int,
    lock_pips: float,
) -> SimResult:
    """Simulate the earned-lock rule on a single trade.

    Returns SimResult with simulated_pnl. If lock fires and is hit by a later
    bar's adverse extreme, simulated_pnl = +lock_pips. Otherwise actual_pnl
    stands. (If lock fires but never hits, trade exits at actual outcome.)
    """
    pip = trade.pip_size
    entry = trade.entry_price
    is_buy = trade.direction == "buy"
    actual = trade.pnl_pips

    if not bars:
        return SimResult(actual, False, None, None, actual)

    consec_neg = 0
    has_earned = False
    locked = False
    lock_bar: Optional[int] = None
    lock_price = (
        entry + lock_pips * pip if is_buy else entry - lock_pips * pip
    )

    for idx, (_dt, c) in enumerate(bars):
        h = float(c["mid"]["h"])
        lo = float(c["mid"]["l"])
        cl = float(c["mid"]["c"])
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
                    lock_bar = idx
                    # Lock activated at this bar's close; subsequent bars
                    # are checked for adverse cross. Don't check current
                    # bar's high/low — lock is set AT close, future-only.
                consec_neg = 0
            # close_pips == 0: neutral — don't break streak, don't trigger
        else:
            # Lock active — check if this bar's adverse extreme hits lock
            if is_buy:
                adverse = lo
                hit = adverse <= lock_price
            else:
                adverse = h
                hit = adverse >= lock_price
            if hit:
                return SimResult(
                    simulated_pnl=lock_pips,
                    lock_fired=True,
                    lock_bar=lock_bar,
                    exit_bar=idx,
                    actual_pnl=actual,
                )

    return SimResult(
        simulated_pnl=actual,
        lock_fired=locked,
        lock_bar=lock_bar,
        exit_bar=None,
        actual_pnl=actual,
    )


def run_backtest():
    print("=" * 80)
    print("EARNED-LOCK BACKTEST — 90 DAYS, scout + snipe_direct")
    print("=" * 80)
    print()
    print("Loading trades from DB ...")
    trades = load_trades()
    print(f"  {len(trades)} closed trades")
    wins = sum(1 for t in trades if t.pnl_pips > 0)
    losses = sum(1 for t in trades if t.pnl_pips < 0)
    print(
        f"  Baseline: {wins} wins, {losses} losses, "
        f"net {sum(t.pnl_pips for t in trades):+.1f}p"
    )
    print()
    print("Fetching M15 history per pair (cached) ...")
    oanda = OandaClient()
    cache = fetch_m15_per_pair(trades, oanda)
    print()

    # Build per-trade bar lists once (reusable across all param combos)
    print("Building per-trade bar windows ...")
    per_trade_bars: Dict[str, List] = {}
    no_data = 0
    for t in trades:
        bars = candles_in_window(cache.get(t.pair, []), t.entry_time, t.exit_time)
        per_trade_bars[t.id] = bars
        if not bars:
            no_data += 1
    print(f"  {no_data} trades had no M15 data (skipped)")
    print()

    # Sweep
    rows = []
    for n_neg in N_VALUES:
        for lock in LOCK_VALUES:
            saves = 0
            saves_pips = 0.0
            kills = 0
            kills_pips = 0.0
            unchanged_fired = 0
            net_delta = 0.0
            losses_saved_actual_pnls = []
            winners_killed_actual_pnls = []
            per_pair_save = defaultdict(int)
            per_pair_kill = defaultdict(int)
            for t in trades:
                bars = per_trade_bars[t.id]
                if not bars:
                    continue
                res = simulate(t, bars, n_neg, lock)
                if res.lock_fired and res.exit_bar is not None:
                    # Lock hit. Compare simulated vs actual.
                    if res.actual_pnl < 0:
                        # Loss saved
                        saves += 1
                        saves_pips += res.delta
                        losses_saved_actual_pnls.append(res.actual_pnl)
                        per_pair_save[t.pair] += 1
                    elif res.actual_pnl > lock + 0.01:
                        # Winner killed (capped at lock instead of running)
                        kills += 1
                        kills_pips += res.delta  # negative number
                        winners_killed_actual_pnls.append(res.actual_pnl)
                        per_pair_kill[t.pair] += 1
                    else:
                        unchanged_fired += 1
                    net_delta += res.delta
            rows.append(
                {
                    "N": n_neg,
                    "lock": lock,
                    "saves": saves,
                    "saves_pips": round(saves_pips, 1),
                    "kills": kills,
                    "kills_pips": round(kills_pips, 1),
                    "unchanged": unchanged_fired,
                    "net_delta_p": round(net_delta, 1),
                    "avg_loss_saved": round(
                        sum(losses_saved_actual_pnls) / len(losses_saved_actual_pnls)
                        if losses_saved_actual_pnls
                        else 0,
                        1,
                    ),
                    "avg_winner_killed_at": round(
                        sum(winners_killed_actual_pnls)
                        / len(winners_killed_actual_pnls)
                        if winners_killed_actual_pnls
                        else 0,
                        1,
                    ),
                    "save_kill_ratio": round(
                        saves / max(kills, 1) if saves else 0, 2
                    ),
                    "per_pair_save": dict(per_pair_save),
                    "per_pair_kill": dict(per_pair_kill),
                }
            )

    # Print sweep table
    print("=" * 110)
    print("PARAMETER SWEEP")
    print("=" * 110)
    print(
        f"{'N_neg':>5} {'lock':>5} {'saves':>6} {'saved_p':>9} {'kills':>6} "
        f"{'killed_p':>10} {'net_p':>9} {'avg_loss_saved':>15} "
        f"{'avg_winner_at':>14} {'save:kill':>10}"
    )
    print("-" * 110)
    for r in rows:
        print(
            f"{r['N']:>5} {r['lock']:>5.1f} {r['saves']:>6} "
            f"{r['saves_pips']:>+9.1f} {r['kills']:>6} {r['kills_pips']:>+10.1f} "
            f"{r['net_delta_p']:>+9.1f} {r['avg_loss_saved']:>+15.1f} "
            f"{r['avg_winner_killed_at']:>+14.1f} {r['save_kill_ratio']:>10.2f}"
        )
    print()

    # Recommend best by net delta
    best = max(rows, key=lambda r: r["net_delta_p"])
    print("=" * 80)
    print("RECOMMENDATION")
    print("=" * 80)
    print(
        f"Best net pip swing: N={best['N']} bars, lock={best['lock']}p "
        f"→ {best['net_delta_p']:+.1f}p over 90 days"
    )
    print(
        f"  Saves {best['saves']} losses ({best['saves_pips']:+.1f}p), "
        f"kills {best['kills']} winners ({best['kills_pips']:+.1f}p)"
    )
    print(f"  Save:Kill ratio: {best['save_kill_ratio']:.2f}")
    print()
    if best["per_pair_save"]:
        print("  Per-pair saves:", best["per_pair_save"])
    if best["per_pair_kill"]:
        print("  Per-pair kills:", best["per_pair_kill"])
    print()

    # Persist results
    out_path = os.path.join(
        os.path.dirname(__file__),
        f"earned_lock_backtest_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json",
    )
    with open(out_path, "w") as f:
        json.dump(
            {
                "days_back": DAYS_BACK,
                "n_trades": len(trades),
                "wins_baseline": wins,
                "losses_baseline": losses,
                "no_m15_data": no_data,
                "sweep": rows,
                "best": best,
            },
            f,
            indent=2,
        )
    print(f"Detailed results saved to: {out_path}")
    return rows, best


if __name__ == "__main__":
    run_backtest()
