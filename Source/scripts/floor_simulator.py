"""
M15 floor simulator: walk each trade's M15 candles, apply a candidate
profit-floor / BE-trail rule, compute hypothetical realized P&L.

Rule structure (in pips, from entry, in trade direction):
    Tiers: list of (mfe_trigger_pips, lock_ratio_or_pips)
        - mfe_trigger_pips: when running MFE crosses this, raise floor.
        - lock_ratio_or_pips:
            * if float 0<x<=1: lock = trigger * x (e.g., 0.5 = lock half of trigger)
            * if int/float >1: lock pips above entry directly (e.g., 1.0 = +1p above entry)
            * if 0: BE (entry)
            * if negative: -X pips below entry (loss buffer)

The simulator walks M15 candles between entry_time and exit_time.
On each candle:
  1. Update running MFE from candle high (BUY) / low (SELL)
  2. Determine highest tier triggered → corresponding floor price
  3. Check next-candle's adverse extreme: if it crosses floor, exit at floor price
  4. Otherwise continue
At final candle (or actual exit_time), exit at actual exit_price.
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from oanda_client import OandaClient, _parse_oanda_time  # noqa: E402

DB = "~/Jarvis/Database/v2/trading_forex.db"
OUT_DIR = Path("/tmp/ghost_v2")
OUT_DIR.mkdir(parents=True, exist_ok=True)

JPY_PIP = 0.01
NON_JPY_PIP = 0.0001


def pip_size(pair: str) -> float:
    return JPY_PIP if pair.endswith("_JPY") else NON_JPY_PIP


def parse_dt(s: str) -> datetime:
    s = s.replace(" ", "T")
    if not s.endswith("Z") and "+" not in s.split("T", 1)[-1]:
        s = s + "Z"
    return _parse_oanda_time(s)


def fetch_trades() -> list:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, pair, direction, entry_price, exit_price,
               entry_time, exit_time, outcome, outcome_pips, source
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= '2026-04-16' AND entry_time < '2026-05-16'
          AND status='closed' AND outcome IN ('win','loss')
          AND exit_time IS NOT NULL AND entry_price IS NOT NULL
        ORDER BY entry_time
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def floor_price_from_tiers(entry: float, direction: str, psize: float,
                           mfe_pips: float, tiers: list) -> float | None:
    """Return floor price for current running MFE, or None if no tier triggered."""
    sign = 1 if direction == "buy" else -1
    triggered = [(t, lock) for (t, lock) in tiers if mfe_pips >= t]
    if not triggered:
        return None
    # Use highest triggered tier
    trigger, lock = max(triggered, key=lambda x: x[0])
    if isinstance(lock, float) and 0 < lock <= 1:
        lock_pips = trigger * lock
    else:
        lock_pips = float(lock)
    return entry + sign * lock_pips * psize


def simulate_trade(client, trade: dict, tiers: list) -> dict:
    pair = trade["pair"]
    direction = trade["direction"].lower()
    entry = float(trade["entry_price"])
    psize = pip_size(pair)
    sign = 1 if direction == "buy" else -1

    et = parse_dt(trade["entry_time"])
    xt = parse_dt(trade["exit_time"])

    candles = client.fetch_candles_range(
        instrument=pair, granularity="M15",
        from_time=et, to_time=xt, price="M",
    )
    if not candles:
        return {"id": trade["id"], "error": "no_candles", "actual_pips": trade["outcome_pips"]}

    running_mfe_pips = 0.0
    floor_price = None  # current ratchet floor (None = no floor yet)

    for c in candles:
        if "mid" not in c:
            continue
        h = float(c["mid"]["h"])
        l = float(c["mid"]["l"])

        # 1) If a floor was set by a PREVIOUS candle, check if THIS candle's
        #    adverse extreme touches it. If so, exit at the floor.
        if floor_price is not None:
            if direction == "buy" and l <= floor_price:
                exit_p = floor_price
                pips = (exit_p - entry) / psize * sign
                return {
                    "id": trade["id"], "pair": pair, "direction": direction,
                    "actual_pips": round(float(trade["outcome_pips"]), 1),
                    "sim_pips": round(pips, 1),
                    "exit_method": "floor_hit",
                    "floor_at_exit_pips": round((floor_price - entry) / psize * sign, 1),
                    "candle_count": len(candles),
                    "outcome": trade["outcome"],
                    "source": trade["source"],
                }
            if direction == "sell" and h >= floor_price:
                exit_p = floor_price
                pips = (entry - exit_p) / psize
                return {
                    "id": trade["id"], "pair": pair, "direction": direction,
                    "actual_pips": round(float(trade["outcome_pips"]), 1),
                    "sim_pips": round(pips, 1),
                    "exit_method": "floor_hit",
                    "floor_at_exit_pips": round((entry - floor_price) / psize, 1),
                    "candle_count": len(candles),
                    "outcome": trade["outcome"],
                    "source": trade["source"],
                }

        # 2) Update MFE based on THIS candle's favorable extreme
        if direction == "buy":
            cand_mfe = (h - entry) / psize
        else:
            cand_mfe = (entry - l) / psize
        if cand_mfe > running_mfe_pips:
            running_mfe_pips = cand_mfe
            # 3) Recompute floor based on new MFE
            new_floor = floor_price_from_tiers(entry, direction, psize, running_mfe_pips, tiers)
            if new_floor is not None:
                if floor_price is None:
                    floor_price = new_floor
                else:
                    # Ratchet UP only (favorable direction)
                    if direction == "buy" and new_floor > floor_price:
                        floor_price = new_floor
                    elif direction == "sell" and new_floor < floor_price:
                        floor_price = new_floor

    # Walked all candles, no floor hit — exit at actual exit (real trade outcome)
    return {
        "id": trade["id"], "pair": pair, "direction": direction,
        "actual_pips": round(float(trade["outcome_pips"]), 1),
        "sim_pips": round(float(trade["outcome_pips"]), 1),
        "exit_method": "actual_exit",
        "floor_at_exit_pips": round((floor_price - entry) / psize * sign, 1) if floor_price else None,
        "running_mfe_pips": round(running_mfe_pips, 1),
        "candle_count": len(candles),
        "outcome": trade["outcome"],
        "source": trade["source"],
    }


CANDIDATES = {
    # tier = (mfe_trigger_pips, lock — float 0-1 = ratio of trigger | int >1 or 0 = absolute pips above entry | negative = below entry)
    "A_mfe_unconditional": [
        (5, 0.5),
        (8, 0.7),
        (12, 0.8),
        (20, 0.9),
        (30, 0.92),
    ],
    "B_be_then_ratchet": [
        (3, 0.0),
        (8, 0.5),
        (12, 0.7),
        (20, 0.85),
        (30, 0.9),
    ],
    "C_loose_BE_then_tight": [
        (5, 0.0),
        (10, 0.6),
        (15, 0.75),
        (25, 0.9),
    ],
    "D_tight_loser_focused": [
        (3, -1.0),
        (5, 0.0),
        (8, 0.6),
        (15, 0.8),
        (25, 0.9),
    ],
    # NEW — adverse-buffer variants (give wick room before locking BE)
    "E_adv_buffer_2p": [
        (5, -2.0),   # MFE 5p → SL at entry-2p (give 2p wick room)
        (10, 0.0),   # MFE 10p → BE
        (15, 0.5),
        (25, 0.8),
    ],
    "F_adv_buffer_3p_conservative": [
        (5, -3.0),   # 3p adverse cushion
        (10, 0.0),
        (15, 0.7),
        (25, 0.9),
    ],
    "G_loose_late_lock": [
        (8, -2.0),   # only fire at MFE 8p, with 2p cushion
        (12, 0.0),   # BE at MFE 12p
        (18, 0.6),
        (30, 0.85),
    ],
    "H_higher_trigger_no_cushion": [
        # Skip early BE entirely (no MFE<8 action) — analog of "wait for confirmation"
        (8, 0.0),
        (12, 0.5),
        (20, 0.8),
        (30, 0.9),
    ],
    "I_monster_only": [
        # Don't touch anything until trade is genuinely big
        (15, 0.0),
        (25, 0.7),
        (40, 0.9),
    ],
    "J_super_tight": [
        # Extreme: tight to test ceiling
        (2, -1.0),
        (5, 0.5),
        (10, 0.7),
        (15, 0.85),
        (25, 0.92),
    ],
    "K_LIVE_post_may13": [
        # Current live rule per position_guardian.py line 2293+ (deployed 2026-05-13)
        (4.5, 0.80),   # 4.5p → 80% = +3.6p
        (8, 0.85),     # 8p → 85% = +6.8p
        (12, 0.90),    # 12p → 90% = +10.8p
        (20, 0.95),    # 20p → 95% = +19p
    ],
    "L_live_plus_small_capture": [
        # Live rule + earlier trigger for small winners
        (3, -1.0),     # 3p → SL at entry-1p (small loser-rescue cushion)
        (4.5, 0.80),
        (8, 0.85),
        (12, 0.90),
        (20, 0.95),
    ],
    "M_tighter_high_end": [
        # Live rule but tighter on big winners (capture monster giveback)
        (4.5, 0.80),
        (8, 0.85),
        (12, 0.92),    # +11p instead of +10.8p
        (20, 0.97),    # +19.4p instead of +19p
        (30, 0.95),    # 30p → +28.5p (catches the 13310/13322 class)
        (50, 0.95),    # 50p → +47.5p
    ],
    "N_live_with_be_floor": [
        # Live rule + BE@5p (between current 4.5 lock and giveback)
        (5, 0.0),      # 5p → BE (replaces 4.5p tier — provide BE as floor)
        (8, 0.85),
        (12, 0.90),
        (20, 0.95),
    ],
    # NEW — 3p trigger variants. Tim's hypothesis: catch the 42 losers that peaked +3p.
    "O_3p_full_cushion": [
        # 3p → SL at entry-3p (max cushion — only saves losers that gave back ≥6p)
        (3, -3.0),
        (4.5, 0.80),
        (8, 0.85),
        (12, 0.90),
        (20, 0.95),
    ],
    "P_3p_med_cushion": [
        # 3p → SL at entry-2p (medium cushion)
        (3, -2.0),
        (4.5, 0.80),
        (8, 0.85),
        (12, 0.90),
        (20, 0.95),
    ],
    "Q_3p_gentle_lock": [
        # 3p → lock +1p (lock 33% = +1p — minimal lock, lots of room)
        (3, 0.33),
        (4.5, 0.80),
        (8, 0.85),
        (12, 0.90),
        (20, 0.95),
    ],
    "R_3p_replace_45_tier": [
        # Lower threshold from 4.5p to 3p, same 80% lock (= +2.4p)
        (3, 0.80),
        (8, 0.85),
        (12, 0.90),
        (20, 0.95),
    ],
    "S_3p_split_loser_winner": [
        # 3p → SL at entry-2.5p (split between cushion sizes)
        (3, -2.5),
        (4.5, 0.80),
        (8, 0.85),
        (12, 0.90),
        (20, 0.95),
        # M's high-end tighten
        (30, 0.95),
        (50, 0.95),
    ],
}


def summarize(name: str, results: list, actual_total: float):
    sim_total = sum(r["sim_pips"] for r in results if r.get("sim_pips") is not None)
    delta = sim_total - actual_total
    changed = [r for r in results if r.get("exit_method") == "floor_hit"]
    print(f"\n=== {name} ===")
    print(f"  Total sim pips: {sim_total:+.1f}p (actual {actual_total:+.1f}p, delta {delta:+.1f}p)")
    print(f"  Floor-exits: {len(changed)} of {len(results)}")

    # Bucket sim vs actual by outcome
    losers = [r for r in results if r.get("outcome") == "loss"]
    winners = [r for r in results if r.get("outcome") == "win"]
    L_actual = sum(r["actual_pips"] for r in losers if r["actual_pips"] is not None)
    L_sim = sum(r["sim_pips"] for r in losers if r["sim_pips"] is not None)
    W_actual = sum(r["actual_pips"] for r in winners if r["actual_pips"] is not None)
    W_sim = sum(r["sim_pips"] for r in winners if r["sim_pips"] is not None)
    print(f"  Losers ({len(losers)}): actual {L_actual:+.1f}p → sim {L_sim:+.1f}p (delta {L_sim - L_actual:+.1f}p)")
    print(f"  Winners ({len(winners)}): actual {W_actual:+.1f}p → sim {W_sim:+.1f}p (delta {W_sim - W_actual:+.1f}p)")


def main():
    trades = fetch_trades()
    print(f"Trades to simulate: {len(trades)}")
    client = OandaClient()
    actual_total = sum(float(t["outcome_pips"]) for t in trades if t["outcome_pips"] is not None)

    all_results = {}
    for name, tiers in CANDIDATES.items():
        print(f"\n--- Running {name} ---")
        results = []
        for i, t in enumerate(trades, 1):
            try:
                r = simulate_trade(client, t, tiers)
                results.append(r)
            except Exception as e:
                results.append({"id": t["id"], "error": str(e)})
            if i % 30 == 0 or i == len(trades):
                print(f"  [{i}/{len(trades)}]")
        all_results[name] = results
        summarize(name, results, actual_total)
        with open(OUT_DIR / f"floor_sim_{name}.json", "w") as f:
            json.dump(results, f, indent=2)

    print(f"\nResults written to {OUT_DIR}/floor_sim_*.json")


if __name__ == "__main__":
    main()
