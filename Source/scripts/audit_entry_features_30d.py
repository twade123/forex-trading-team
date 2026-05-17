"""
PHASE 1: M15 entry-time feature extraction for asymmetric-loss audit.

For each 30d validator-family trade (snipe_direct + scout + manual):
  - Pull M15 candles ending at entry bar (250-bar history)
  - Compute entry-bar features using the SAME helpers scout/validator use:
      * fan state, fan direction, separation pips, fan width %
      * EMA cascade phase (0..4) via E100 position + closes-through-E100
      * RSI(14), Stoch %K(14,3,3), BB width, BB position
      * ATR(14), distance from E100 in ATR units
      * Opposing peak_sep marker within last 3/5/10 bars (the "exhaustion at entry" signal)
      * Distance to recent daily H/L (M15-derivable; no daily API)
      * Distance to prior-session H/L (M15-derivable)
      * Session tag (Asian/London/NY) from entry UTC hour
      * Bar-of-day, minutes-into-session

Output: /tmp/ghost_v2/entry_features_30d.json — one row per trade.

Usage:
  python audit_entry_features_30d.py
  python audit_entry_features_30d.py --limit 5   # smoke test
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from oanda_client import OandaClient, _parse_oanda_time
from backtester.ema_separation import (
    format_chart_signals, scan_ema_signals,
    _compute_rsi, _compute_stochastic, _compute_bollinger, calculate_ema,
)

DB_TRADES = "~/Jarvis/Database/v2/trading_forex.db"
OUT = "/tmp/ghost_v2/entry_features_30d.json"

JPY_PIP = 0.01
NON_JPY_PIP = 0.0001
PAD_BARS = 250  # bars of M15 history before entry (62.5h, covers weekend)


def pip_size(pair):
    return JPY_PIP if pair.endswith("_JPY") else NON_JPY_PIP


def parse_dt(s):
    if not s:
        return None
    s2 = s.replace(" ", "T")
    if not s2.endswith("Z") and "+" not in s2.split("T", 1)[-1]:
        s2 = s2 + "Z"
    return _parse_oanda_time(s2)


def session_tag(et_utc):
    """Asian/London/NY based on ET. ET = UTC-5 (winter) / UTC-4 (summer).
    Simple approximation using fixed UTC->ET-5 offset; the rough buckets are
    Asian 19:00-02:30 ET, London 02:30-09:00 ET, NY 08:00-17:00 ET (overlap).
    Returns: 'asian' | 'london' | 'london_ny_overlap' | 'ny' | 'after_hours'.
    """
    et_hour = (et_utc.hour - 4) % 24  # ET = UTC-4 (EDT during summer)
    if 19 <= et_hour or et_hour < 2.5:
        return "asian"
    if 2.5 <= et_hour < 8:
        return "london"
    if 8 <= et_hour < 12:
        return "london_ny_overlap"
    if 12 <= et_hour < 17:
        return "ny"
    return "after_hours"


def fetch_trades():
    conn = sqlite3.connect(DB_TRADES)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, pair, direction, source, outcome, outcome_pips, pnl_pips,
               max_favorable_excursion_pips, max_adverse_excursion_pips,
               entry_time, exit_time, entry_price,
               setup, base_setup, story_score, story_entry_type
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND status='closed' AND outcome IN ('win','loss')
          AND entry_time >= datetime('now', '-30 days')
          AND exit_time IS NOT NULL AND entry_price IS NOT NULL
        ORDER BY entry_time
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _bars_to_cnd(candles):
    """OANDA mid candles -> list[dict] format the helpers expect."""
    out = []
    for c in candles:
        if "mid" not in c:
            continue
        m = c["mid"]
        out.append({
            "time": c["time"],
            "open": float(m["o"]), "high": float(m["h"]),
            "low": float(m["l"]), "close": float(m["c"]),
        })
    return out


def _atr(candles, period=14):
    """Simple ATR over the last `period` bars (price units, not pips)."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(len(candles) - period, len(candles)):
        h = candles[i]["high"]; l = candles[i]["low"]
        pc = candles[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


def _cascade_phase(ema21, ema55, ema100, closes, is_long):
    """Approximate cascade phase 0-4 based on EMA order and closes-through-E100.

    0 = unordered or against bias
    1 = ordered but EMAs converging / weak
    2 = ordered, EMAs separating, some closes through E100
    3 = ordered, full separation, 6+ recent closes beyond E100
    4 = peaked / decelerating (separation contracting after expansion)
    """
    if len(ema21) < 30 or len(ema55) < 30 or len(ema100) < 30:
        return 0

    # Current EMA order check
    e21 = ema21[-1]; e55 = ema55[-1]; e100 = ema100[-1]
    if is_long:
        ordered = e21 > e55 > e100
    else:
        ordered = e21 < e55 < e100
    if not ordered:
        return 0

    # Recent closes beyond E100 in trade direction
    last10 = closes[-10:]
    beyond = sum(1 for c in last10 if (c > e100) == is_long)

    # Separation now vs 5 bars ago — peaked?
    sep_now = abs(e21 - e55) + abs(e55 - e100)
    e21_5 = ema21[-6]; e55_5 = ema55[-6]; e100_5 = ema100[-6]
    sep_5_ago = abs(e21_5 - e55_5) + abs(e55_5 - e100_5)
    peaked = sep_now < sep_5_ago * 0.9  # 10% contraction

    if peaked:
        return 4
    if beyond >= 6 and sep_now > sep_5_ago * 1.1:
        return 3
    if beyond >= 3:
        return 2
    return 1


def _recent_marker(candles, oppose_dir, look_back_bars):
    """Was there an opposing peak_sep marker within the last N bars (excl. last bar)?"""
    if len(candles) < look_back_bars + 2:
        return False, None
    sigs = format_chart_signals(candles) or []
    if not sigs:
        return False, None
    # Find latest opposing peak_sep
    opp = [s for s in sigs if s.get("type") == "peak_sep" and s.get("direction") == oppose_dir]
    if not opp:
        return False, None
    # Compare time of latest opposing marker to N bars ago
    last_marker_time = parse_dt(opp[-1].get("time"))
    cutoff_time = parse_dt(candles[-look_back_bars]["time"])
    if last_marker_time and cutoff_time and last_marker_time >= cutoff_time:
        # Compute bars-ago
        target_t = last_marker_time
        bars_ago = None
        for i, c in enumerate(candles):
            if parse_dt(c["time"]) == target_t:
                bars_ago = len(candles) - 1 - i
                break
        return True, bars_ago
    return False, None


def _price_range_position(candles, lookback=24):
    """Where is current close in the last-N-bar range (0=low, 100=high)."""
    if len(candles) < lookback:
        return None
    hi = max(c["high"] for c in candles[-lookback:])
    lo = min(c["low"] for c in candles[-lookback:])
    rng = hi - lo
    if rng <= 0:
        return None
    cur = candles[-1]["close"]
    return round((cur - lo) / rng * 100, 1)


def _prior_session_hl_distance(candles, entry_close, psize, session_bars=32):
    """Distance from entry close to prior-session high and low in pips.
    Approximates a 'prior session' as the previous 32 M15 bars (8 hours).
    Returns dict with pips_to_prev_hi, pips_to_prev_lo.
    """
    if len(candles) < session_bars * 2:
        return {"pips_to_prev_hi": None, "pips_to_prev_lo": None}
    prev = candles[-session_bars * 2 : -session_bars]
    hi = max(c["high"] for c in prev)
    lo = min(c["low"] for c in prev)
    return {
        "pips_to_prev_hi": round((hi - entry_close) / psize, 1),
        "pips_to_prev_lo": round((entry_close - lo) / psize, 1),
    }


def analyze_trade(client, trade):
    tid = str(trade["id"])
    pair = trade["pair"]
    direction = (trade["direction"] or "").lower()
    is_long = direction in ("buy", "long")
    psize = pip_size(pair)
    entry = float(trade["entry_price"])

    et = parse_dt(trade["entry_time"])
    xt = parse_dt(trade["exit_time"])
    if not et:
        return {"trade_id": tid, "error": "bad_entry_time"}

    # Pull M15 candles: PAD_BARS before entry through entry. We don't need exit window.
    pad_start = et - timedelta(minutes=15 * PAD_BARS)
    pad_end = et + timedelta(minutes=20)  # small forward buffer to ensure entry bar present
    candles = client.fetch_candles_range(
        instrument=pair, granularity="M15",
        from_time=pad_start, to_time=pad_end, price="M",
    )
    if not candles:
        return {"trade_id": tid, "error": "no_candles"}
    cnd = _bars_to_cnd(candles)

    # Find entry bar (first M15 close >= entry_time)
    entry_idx = None
    for i, c in enumerate(cnd):
        if parse_dt(c["time"]) >= et:
            entry_idx = i
            break
    if entry_idx is None or entry_idx < 100:
        return {"trade_id": tid, "error": "no_entry_bar",
                "entry_idx": entry_idx, "total": len(cnd)}

    pre_entry = cnd[: entry_idx + 1]  # candles up to AND INCLUDING entry bar
    closes = [c["close"] for c in pre_entry]
    ema21 = calculate_ema(closes, 21)
    ema55 = calculate_ema(closes, 55)
    ema100 = calculate_ema(closes, 100)

    # Fan state via existing scout helper
    fan = scan_ema_signals(pre_entry) or {}
    fan_state = fan.get("fan_state")
    fan_dir = fan.get("fan_direction")
    fan_sep_pips = round((fan.get("current_separation_pips") or 0), 1)
    fan_width_pct = fan.get("fan_width_pct")
    velocity_trend = fan.get("velocity_trend")

    # Cascade phase 0-4
    phase = _cascade_phase(ema21, ema55, ema100, closes, is_long)

    # Recent closes beyond E100
    e100_last = ema100[-1] if ema100 else None
    if e100_last is not None:
        beyond_e100 = sum(1 for c in closes[-10:] if (c > e100_last) == is_long)
    else:
        beyond_e100 = None

    # Distance from current price to E100 in ATR units
    atr = _atr(pre_entry, period=14)
    dist_to_e100_atr = None
    if e100_last is not None and atr:
        dist_to_e100_atr = round((closes[-1] - e100_last) / atr, 2)

    # RSI / Stoch / BB
    rsi_val = _compute_rsi(closes, 14)
    stoch = _compute_stochastic(pre_entry, k_period=14, k_smooth=3, d_smooth=3) or {}
    bb = _compute_bollinger(closes, period=20, std_mult=2.0) or {}
    bb_width_pips = None
    bb_position_pct = None
    if bb.get("upper") and bb.get("lower"):
        bb_width_pips = round((bb["upper"] - bb["lower"]) / psize, 1)
        rng = bb["upper"] - bb["lower"]
        if rng > 0:
            bb_position_pct = round((closes[-1] - bb["lower"]) / rng * 100, 1)

    # Opposing peak_sep markers within last 3, 5, 10 bars
    oppose = "sell" if is_long else "buy"
    mkr3, ba3 = _recent_marker(pre_entry, oppose, 3)
    mkr5, ba5 = _recent_marker(pre_entry, oppose, 5)
    mkr10, ba10 = _recent_marker(pre_entry, oppose, 10)

    # Range/session features
    range_pos = _price_range_position(pre_entry, lookback=24)  # 6h window
    prev_session = _prior_session_hl_distance(pre_entry, closes[-1], psize, session_bars=32)
    sess = session_tag(et)
    et_hour = (et.hour - 4) % 24
    minutes_into_hour = et.minute

    # Outcome — fallback outcome_pips → pnl_pips
    outcome_pips = trade.get("outcome_pips")
    if outcome_pips is None:
        outcome_pips = trade.get("pnl_pips")

    return {
        "trade_id": tid,
        "pair": pair,
        "direction": direction,
        "source": trade["source"],
        "outcome": trade["outcome"],
        "outcome_pips": round(float(outcome_pips), 1) if outcome_pips is not None else None,
        "mfe_pips": round(float(trade["max_favorable_excursion_pips"]), 1) if trade["max_favorable_excursion_pips"] is not None else None,
        "mae_pips": round(float(trade["max_adverse_excursion_pips"]), 1) if trade["max_adverse_excursion_pips"] is not None else None,
        "setup": trade.get("setup"),
        "base_setup": trade.get("base_setup"),
        "db_story_score": trade.get("story_score"),
        # M15 chart features at entry
        "fan_state": fan_state,
        "fan_direction": fan_dir,
        "fan_sep_pips": fan_sep_pips,
        "fan_width_pct": fan_width_pct,
        "velocity_trend": velocity_trend,
        "cascade_phase": phase,
        "closes_beyond_e100": beyond_e100,
        "dist_to_e100_atr": dist_to_e100_atr,
        "rsi": round(rsi_val, 1) if rsi_val is not None else None,
        "stoch_k": round(stoch.get("k"), 1) if stoch.get("k") is not None else None,
        "stoch_d": round(stoch.get("d"), 1) if stoch.get("d") is not None else None,
        "bb_width_pips": bb_width_pips,
        "bb_position_pct": bb_position_pct,
        "atr_pips": round(atr / psize, 1) if atr else None,
        # Exhaustion-at-entry signals (the key hypothesis)
        "oppo_marker_within_3bars": mkr3,
        "oppo_marker_within_5bars": mkr5,
        "oppo_marker_within_10bars": mkr10,
        "oppo_marker_bars_ago": ba10,
        # Range / session
        "range_position_24bar_pct": range_pos,
        "pips_to_prev_session_hi": prev_session["pips_to_prev_hi"],
        "pips_to_prev_session_lo": prev_session["pips_to_prev_lo"],
        "session": sess,
        "entry_hour_et": et_hour,
        "entry_minute": minutes_into_hour,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Only process first N trades (smoke test)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    trades = fetch_trades()
    if args.limit:
        trades = trades[: args.limit]
    print(f"Extracting entry features for {len(trades)} trades ...")
    client = OandaClient()
    results = []
    for i, t in enumerate(trades, 1):
        try:
            r = analyze_trade(client, t)
            results.append(r)
            if i % 20 == 0 or i == len(trades):
                ok = sum(1 for x in results if "error" not in x)
                er = sum(1 for x in results if "error" in x)
                print(f"  [{i}/{len(trades)}] ok={ok} err={er}")
        except Exception as e:
            results.append({"trade_id": str(t["id"]), "error": f"exc:{e}"})

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults: {args.out}")
    print(f"OK rows: {sum(1 for x in results if 'error' not in x)} / {len(results)}")


if __name__ == "__main__":
    main()
