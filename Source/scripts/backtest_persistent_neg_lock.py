"""backtest_persistent_neg_lock.py — Test a guardian rule that closes positions
which go negative immediately and stay negative.

Rule:
  If pnl has been continuously negative since entry (never went positive)
  AND pnl reaches -X pips threshold
  → close at current pnl (~-X pips), saving the rest of the SL distance

Tunable thresholds:
  - lock_pip_threshold: how negative before we cut (3, 4, 5, 6, 7 tested)

For each snipe in last 30 days:
  1. Fetch M15 candles from entry through exit
  2. At each bar close, compute pnl_pips from entry_price
  3. Walk forward — if pnl never positive and crosses below -X → exit there
  4. Compare to actual exit pip outcome
"""
import os
import sqlite3
import sys
from pathlib import Path

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient

DB = "~/Jarvis/Database/v2/trading_forex.db"


def get_pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def compute_pnl_path(pair, entry_time, exit_time, entry_price, direction, max_bars=200):
    """Fetch M15 candles between entry and exit, return pnl_pips at each bar close."""
    from datetime import datetime
    oc = OandaClient()
    pip = get_pip_size(pair)
    try:
        # Parse to datetime objects
        from dateutil.parser import isoparse
        ft = isoparse(entry_time)
        tt = isoparse(exit_time)
        candles = oc.get_candles(pair, granularity="M15", from_time=ft, to_time=tt, count=max_bars)
    except Exception as e:
        return None

    if not candles:
        return None

    is_long = direction.lower() in ("buy", "long")
    pnl_path = []
    for c in candles:
        t = c.get("time", "")
        close_px = float(c.get("mid", {}).get("c", c.get("close", 0)))
        if not close_px:
            continue
        diff = (close_px - entry_price) if is_long else (entry_price - close_px)
        pnl_pips = diff / pip
        pnl_path.append({"time": t, "close": close_px, "pnl_pips": pnl_pips})
    return pnl_path


def apply_lock(pnl_path, lock_pip):
    """Walk the pnl path. If never positive and pnl crosses below -lock_pip,
    return the bar index + pnl_pips at lock time. Otherwise return None."""
    ever_positive = False
    for i, bar in enumerate(pnl_path):
        if bar["pnl_pips"] > 0:
            ever_positive = True
            return None  # locked rule only fires if NEVER positive
        if not ever_positive and bar["pnl_pips"] <= -lock_pip:
            return {"bar_idx": i, "lock_pnl": bar["pnl_pips"], "lock_time": bar["time"]}
    return None


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, entry_price, entry_time, exit_time, pnl_pips, source
        FROM live_trades
        WHERE source='snipe_direct'
        AND entry_time >= datetime('now','-30 days')
        AND pnl_pips IS NOT NULL
        AND entry_price IS NOT NULL
        ORDER BY entry_time ASC
    """).fetchall()

    print(f"Backtesting persistent-negative-lock on {len(trades)} snipes (last 30 days)")
    print()

    # Sweep thresholds
    THRESHOLDS = [3, 4, 5, 6, 7, 8, 10]

    # For each trade, compute pnl_path once, then test all thresholds
    print("Fetching candle paths for all trades...")
    paths = {}
    for i, t in enumerate(trades):
        if i % 25 == 0:
            print(f"  {i}/{len(trades)}...")
        path = compute_pnl_path(t["pair"], t["entry_time"], t["exit_time"],
                                 float(t["entry_price"]), t["direction"])
        if path:
            paths[t["id"]] = path
    print(f"  Done — {len(paths)}/{len(trades)} trades have candle data")
    print()

    print(f"=== THRESHOLD SWEEP ===")
    print(f"{'thresh':<8} {'fires':<6} {'win_cut':<8} {'lose_cut':<9} {'pip_saved':<11} {'pip_lost_winners':<17} {'net_pip':<10}")
    print('-'*90)

    for thresh in THRESHOLDS:
        fires = 0
        saved = 0.0
        lost_w = 0.0
        wins_cut = 0
        losses_cut = 0
        for t in trades:
            path = paths.get(t["id"])
            if not path: continue
            lock_result = apply_lock(path, thresh)
            if lock_result is None: continue
            fires += 1
            actual = t["pnl_pips"]
            cut_at = lock_result["lock_pnl"]
            if actual > 0:
                # We cut a winner — lost actual pips
                lost_w += actual
                wins_cut += 1
            else:
                # We cut a loser — saved (actual - cut_at) pips of damage
                saved += (cut_at - actual)
                losses_cut += 1
        net = saved - lost_w
        print(f"  -{thresh}p{'':<3} {fires:<6} {wins_cut:<8} {losses_cut:<9} {saved:<+10.1f}p {lost_w:<+16.1f}p {net:<+9.1f}p")


if __name__ == "__main__":
    main()
