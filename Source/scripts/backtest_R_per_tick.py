"""
Per-tick (M1 candle) backtest of candidate R (3p threshold) vs LIVE baseline (4.5p threshold).

Uses optimizer/replay.py's candle_walk_replay logic with M1 candles + 1-bar reaction delay
(matches real guardian's 60s tick rate). Properly handles intra-bar wicks.

Compare against the M15 simulator's coarse estimate.
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from oanda_client import OandaClient, _parse_oanda_time  # noqa

DB = "~/Jarvis/Database/v2/trading_forex.db"
OUT_DIR = Path("/tmp/ghost_v2")

JPY_PIP = 0.01
NON_JPY_PIP = 0.0001


def pip_size(pair):
    return JPY_PIP if pair.endswith("_JPY") else NON_JPY_PIP


def parse_dt(s):
    s = s.replace(" ", "T")
    if not s.endswith("Z") and "+" not in s.split("T", 1)[-1]:
        s = s + "Z"
    return _parse_oanda_time(s)


def candle_walk(trade, candles, floor_tiers, reaction_delay_bars=1):
    """
    Walk candles bar-by-bar applying ratchet floor + reaction-delay.
    floor_tiers: list of (mfe_trigger_pips, lock_ratio) — same as M15 sim semantics
        - 0 < lock_ratio <= 1 → lock_pips = trigger * lock_ratio
        - lock_ratio == 0 → BE
        - lock_ratio < 0 (e.g., -2.0) → entry-2p (loss-side cushion)
    """
    pair = trade["pair"]
    direction = trade["direction"].lower()
    entry = float(trade["entry_price"])
    psize = pip_size(pair)
    is_long = direction in ("buy", "long")

    if not candles:
        return {"sim_pips": trade["outcome_pips"], "exit_reason": "no_candles", "peak": 0}

    sign = 1 if is_long else -1
    peak_pips = 0.0
    floor_pips = None  # current locked floor (pips from entry, sign-aware)
    breach_bar = None  # first bar where close breached floor

    for i, c in enumerate(candles):
        if "mid" not in c:
            continue
        h = float(c["mid"]["h"])
        l = float(c["mid"]["l"])
        cl = float(c["mid"]["c"])

        if is_long:
            best_pips = (h - entry) / psize
            worst_pips = (l - entry) / psize
            close_pips = (cl - entry) / psize
        else:
            best_pips = (entry - l) / psize
            worst_pips = (entry - h) / psize
            close_pips = (entry - cl) / psize

        peak_pips = max(peak_pips, best_pips)

        # Determine floor for current peak
        triggered = [(t, r) for (t, r) in floor_tiers if peak_pips >= t]
        if triggered:
            t, r = max(triggered, key=lambda x: x[0])
            if 0 < r <= 1:
                new_floor = t * r
            else:
                new_floor = float(r)
            if floor_pips is None or new_floor > floor_pips:
                floor_pips = new_floor

        # Reaction-delay breach check: only exit if close breaches floor AND
        # the breach persists for reaction_delay_bars consecutive bars
        if floor_pips is not None:
            if close_pips < floor_pips:
                if breach_bar is None:
                    breach_bar = i
                elif (i - breach_bar) >= reaction_delay_bars:
                    # Exit at floor (with small slippage realistic for live)
                    exit_pips = max(close_pips, floor_pips - 0.5)
                    return {
                        "sim_pips": round(exit_pips, 1),
                        "exit_reason": "floor_breach",
                        "peak": round(peak_pips, 1),
                        "floor": round(floor_pips, 1),
                        "bars": i,
                    }
            else:
                breach_bar = None

    # Ran out of candles — exit at actual trade exit
    return {
        "sim_pips": round(float(trade["outcome_pips"]), 1),
        "exit_reason": "actual_exit",
        "peak": round(peak_pips, 1),
        "floor": round(floor_pips, 1) if floor_pips else None,
        "bars": len(candles),
    }


def fetch_trades():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, pair, direction, entry_price, entry_time, exit_time,
               outcome, outcome_pips, source
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= '2026-04-16' AND entry_time < '2026-05-16'
          AND status='closed' AND outcome IN ('win','loss')
          AND exit_time IS NOT NULL AND entry_price IS NOT NULL
        ORDER BY entry_time
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


CONFIGS = {
    "LIVE_4p5_M1": [
        (4.5, 0.80),
        (8, 0.85),
        (12, 0.90),
        (20, 0.95),
    ],
    "R_3p_M1": [
        (3, 0.80),       # the change: 4.5 → 3
        (8, 0.85),
        (12, 0.90),
        (20, 0.95),
    ],
    "R_plus_M_high_end": [
        (3, 0.80),
        (8, 0.85),
        (12, 0.92),
        (20, 0.97),
        (30, 0.95),
        (50, 0.95),
    ],
}


def main():
    trades = fetch_trades()
    print(f"Trades to backtest (M1 per-tick): {len(trades)}")
    client = OandaClient()

    # Pre-fetch M1 candles for each trade once (reused across all configs)
    # Cache to disk so we don't re-fetch on subsequent runs
    cache_path = OUT_DIR / "m1_candle_cache.json"
    if cache_path.exists():
        print(f"Loading M1 candle cache from {cache_path}")
        candle_cache = json.load(open(cache_path))
    else:
        print("Pre-fetching M1 candles for all trades...")
        candle_cache = {}
        for i, t in enumerate(trades, 1):
            try:
                et = parse_dt(t["entry_time"])
                xt = parse_dt(t["exit_time"])
                candles = client.fetch_candles_range(
                    instrument=t["pair"], granularity="M1",
                    from_time=et, to_time=xt, price="M",
                )
                candle_cache[str(t["id"])] = candles
                if i % 20 == 0:
                    print(f"  [{i}/{len(trades)}] cached")
            except Exception as e:
                candle_cache[str(t["id"])] = None
                print(f"  [{i}/{len(trades)}] {t['id']} ERROR: {e}")
        with open(cache_path, "w") as f:
            json.dump(candle_cache, f)
        print(f"Cache written: {cache_path}")

    summary = {}
    for name, tiers in CONFIGS.items():
        print(f"\n--- {name} ---")
        results = []
        actual_total = 0.0
        sim_total = 0.0
        L_actual = L_sim = W_actual = W_sim = 0.0
        for t in trades:
            if t["outcome_pips"] is None:
                continue
            actual = float(t["outcome_pips"])
            cands = candle_cache.get(str(t["id"]))
            r = candle_walk(t, cands, tiers, reaction_delay_bars=1)
            sim = r["sim_pips"]
            results.append({**t, **r, "actual_pips": actual, "sim_pips": sim})
            actual_total += actual
            sim_total += sim
            if t["outcome"] == "loss":
                L_actual += actual
                L_sim += sim
            else:
                W_actual += actual
                W_sim += sim
        floor_exits = sum(1 for r in results if r.get("exit_reason") == "floor_breach")
        delta = sim_total - actual_total
        print(f"  Total: sim {sim_total:+.1f}p (actual {actual_total:+.1f}p, delta {delta:+.1f}p)")
        print(f"  Losers: actual {L_actual:+.1f}p → sim {L_sim:+.1f}p (delta {L_sim-L_actual:+.1f}p)")
        print(f"  Winners: actual {W_actual:+.1f}p → sim {W_sim:+.1f}p (delta {W_sim-W_actual:+.1f}p)")
        print(f"  Floor-breach exits: {floor_exits}")
        summary[name] = {
            "total_sim": round(sim_total, 1),
            "delta": round(delta, 1),
            "L_save": round(L_sim - L_actual, 1),
            "W_lost": round(W_sim - W_actual, 1),
            "floor_exits": floor_exits,
        }
        with open(OUT_DIR / f"per_tick_{name}.json", "w") as f:
            json.dump(results, f, indent=2)

    with open(OUT_DIR / "per_tick_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nSummary: {json.dumps(summary, indent=2)}")


if __name__ == "__main__":
    main()
