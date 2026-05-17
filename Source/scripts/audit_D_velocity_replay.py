"""
AUDIT D: For each loser with detected retrace, fetch M15 candles AROUND the
detection time. Compute fan-velocity (EMA convergence rate) and BB-width
contraction at -3, -2, -1 M15 bars BEFORE the position-based retrace_zone
fired. Did velocity / compression signals predict the turn earlier?

Hypothesis: if E55-E21 separation was already contracting 3 bars before
price crossed E21, we could detect the turn 45 min earlier.
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from oanda_client import OandaClient, _parse_oanda_time

AUDIT_FILE = "/tmp/ghost_v2/retrace_audit_losers.json"
OUT = "/tmp/ghost_v2/audit_D_velocity_replay.json"

JPY_PIP = 0.01
NON_JPY_PIP = 0.0001


def parse_dt(s: str):
    if not s:
        return None
    s2 = s.replace(" ", "T")
    if not s2.endswith("Z") and "+" not in s2.split("T", 1)[-1]:
        s2 = s2 + "Z"
    return _parse_oanda_time(s2)


def ema(prices, period):
    """Compute EMA series."""
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(prices[:period]) / period]
    for p in prices[period:]:
        out.append(p * k + out[-1] * (1 - k))
    return out


def bb_width(prices, period=20, mult=2.0):
    """Compute BB width at end of series."""
    if len(prices) < period:
        return None
    recent = prices[-period:]
    mean = sum(recent) / period
    var = sum((p - mean) ** 2 for p in recent) / period
    std = var ** 0.5
    return mult * std * 2  # upper - lower


def fetch_loser_with_detection(audit_file):
    """Return losers that had detection (so we have a time to anchor on)."""
    audit = json.load(open(audit_file))
    return [d for d in audit
            if 'error' not in d
            and d.get('first_retrace_detection')
            and d.get('detection_minutes_after_entry') is not None]


def analyze_one(client, loser):
    """For one loser, fetch candles up to detection time + analyze signals before that."""
    pair = loser['pair']
    entry_t = parse_dt(loser['entry_time'])
    detection_t = parse_dt(loser['first_retrace_detection']['ts'])
    if not entry_t or not detection_t:
        return {"trade_id": loser['trade_id'], "error": "bad_timestamps"}

    # Fetch M15 candles from entry to detection
    # Pad: 25 bars before entry to compute proper EMA + BB
    pad_start = entry_t - timedelta(minutes=15 * 30)
    candles = client.fetch_candles_range(
        instrument=pair, granularity="M15",
        from_time=pad_start, to_time=detection_t, price="M",
    )

    if not candles or len(candles) < 25:
        return {"trade_id": loser['trade_id'], "error": "insufficient_candles", "n": len(candles)}

    closes = [float(c["mid"]["c"]) for c in candles if "mid" in c]
    times = [c["time"] for c in candles if "mid" in c]

    # Find candle index at entry
    entry_idx = None
    for i, t in enumerate(times):
        if parse_dt(t) >= entry_t:
            entry_idx = i
            break
    if entry_idx is None or entry_idx < 22:
        return {"trade_id": loser['trade_id'], "error": "no_entry_bar"}

    # Walk forward from entry. For each bar, compute:
    #   - fan separation (E21-E55, E55-E100)
    #   - fan-velocity (rate of change of separation)
    #   - bb_width and contraction rate
    samples = []
    for i in range(entry_idx, min(len(closes) - 1, entry_idx + 30)):
        if i < 22:  # need enough history
            continue
        window = closes[: i + 1]
        e21_s = ema(window, 21)
        e55_s = ema(window, 55) if len(window) >= 55 else None
        e100_s = ema(window, 100) if len(window) >= 100 else None
        if not e21_s:
            continue
        e21 = e21_s[-1]
        e55 = e55_s[-1] if e55_s else None
        e100 = e100_s[-1] if e100_s else None
        sep_21_55 = abs(e21 - e55) if e55 else None
        bbw = bb_width(window, 20)
        # Velocity = change in separation over last 3 bars (if available)
        sep_velocity = None
        if e55_s and len(e21_s) >= 4 and len(e55_s) >= 4:
            prev_sep = abs(e21_s[-4] - e55_s[-4])
            sep_velocity = (sep_21_55 - prev_sep) if sep_21_55 else None
        bb_contracting = None
        if i >= 22:
            prev_window = closes[: i - 2]
            prev_bbw = bb_width(prev_window, 20)
            if prev_bbw and bbw:
                bb_contracting = bbw < prev_bbw  # contracting in last 3 bars

        samples.append({
            "bar_offset": i - entry_idx,  # 0 = entry bar
            "time": times[i],
            "close": closes[i],
            "e21": e21, "e55": e55, "e100": e100,
            "sep_21_55_pips": round(sep_21_55 / (JPY_PIP if pair.endswith("_JPY") else NON_JPY_PIP), 2) if sep_21_55 else None,
            "sep_velocity": round(sep_velocity / (JPY_PIP if pair.endswith("_JPY") else NON_JPY_PIP), 2) if sep_velocity is not None else None,
            "bb_width": bbw,
            "bb_contracting_last3": bb_contracting,
        })

    # Detection bar index in samples list
    detection_bar = None
    for s in samples:
        if parse_dt(s['time']) >= detection_t:
            detection_bar = s['bar_offset']
            break

    # Did velocity/contraction signal fire BEFORE detection?
    pre_detection_samples = [s for s in samples if detection_bar is not None and s['bar_offset'] < detection_bar]
    contraction_before_detection = any(s.get('bb_contracting_last3') is True for s in pre_detection_samples)
    velocity_neg_before = any(s.get('sep_velocity') is not None and s['sep_velocity'] < 0 for s in pre_detection_samples)
    # When did velocity first turn negative?
    first_neg_vel = next((s for s in samples if s.get('sep_velocity') is not None and s['sep_velocity'] < 0), None)
    first_contract = next((s for s in samples if s.get('bb_contracting_last3') is True), None)

    bars_velocity_earlier = None
    if first_neg_vel and detection_bar is not None:
        bars_velocity_earlier = detection_bar - first_neg_vel['bar_offset']
    bars_bb_earlier = None
    if first_contract and detection_bar is not None:
        bars_bb_earlier = detection_bar - first_contract['bar_offset']

    return {
        "trade_id": loser['trade_id'],
        "pair": pair,
        "outcome_pips": loser['outcome_pips'],
        "detection_minutes_after_entry": loser['detection_minutes_after_entry'],
        "detection_bar_offset": detection_bar,
        "n_samples": len(samples),
        "velocity_negative_before_detection": velocity_neg_before,
        "bb_contracting_before_detection": contraction_before_detection,
        "bars_velocity_earlier_than_detection": bars_velocity_earlier,
        "bars_bb_earlier_than_detection": bars_bb_earlier,
        "samples": samples,
    }


def main():
    audit = json.load(open(AUDIT_FILE))
    detected_losers = [d for d in audit
                       if 'error' not in d
                       and d.get('first_retrace_detection')
                       and d.get('detection_minutes_after_entry') is not None]
    print(f"Analyzing {len(detected_losers)} detected losers...")
    client = OandaClient()

    results = []
    for i, d in enumerate(detected_losers, 1):
        try:
            r = analyze_one(client, d)
            results.append(r)
            if i % 10 == 0:
                print(f"  [{i}/{len(detected_losers)}]")
        except Exception as e:
            results.append({"trade_id": d['trade_id'], "error": str(e)})
            print(f"  [{i}/{len(detected_losers)}] {d['trade_id']} ERROR: {e}")

    with open(OUT, "w") as f:
        json.dump(results, f, indent=2)

    # Summary
    valid = [r for r in results if 'samples' in r]
    vel_earlier = [r for r in valid if r.get('bars_velocity_earlier_than_detection') is not None
                   and r['bars_velocity_earlier_than_detection'] > 0]
    bb_earlier = [r for r in valid if r.get('bars_bb_earlier_than_detection') is not None
                  and r['bars_bb_earlier_than_detection'] > 0]

    print(f"\n=== AUDIT D SUMMARY ({len(valid)} analyzed) ===")
    print(f"Velocity (sep_21_55 negative) fired BEFORE position-based detection: {len(vel_earlier)}/{len(valid)}")
    if vel_earlier:
        bars = [r['bars_velocity_earlier_than_detection'] for r in vel_earlier]
        print(f"  Median bars earlier: {sorted(bars)[len(bars)//2]} bars ({sorted(bars)[len(bars)//2] * 15} min)")
        print(f"  Mean bars earlier: {sum(bars)/len(bars):.1f}")
    print(f"\nBB-width contracting fired BEFORE position-based detection: {len(bb_earlier)}/{len(valid)}")
    if bb_earlier:
        bars = [r['bars_bb_earlier_than_detection'] for r in bb_earlier]
        print(f"  Median bars earlier: {sorted(bars)[len(bars)//2]} bars ({sorted(bars)[len(bars)//2] * 15} min)")

    print(f"\nResults: {OUT}")


if __name__ == "__main__":
    main()
