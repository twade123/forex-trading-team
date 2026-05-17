"""
ONE question: when the fan_exhaustion gate fires, is the fan actually exhausted?

No simulation. No outcomes. No SL/TP. Just read the geometry at block time and
ask: by any reasonable definition of "exhausted fan," does the fan qualify?

Exhausted = trend energy spent. Means at least ONE of:
  - EMAs no longer ordered (E21/E55/E100 not in fan-direction order)
  - Fan width collapsed (< 4 pips total E21-E100 spread)
  - Fan direction does NOT agree with the intended snipe direction
    (snipe trying to BUY a bearish fan, or SELL a bullish fan)

NOT exhausted = healthy trend, snipe should be allowed:
  - EMAs still ordered in fan direction
  - Fan width still substantial (>= 4 pips)
  - Fan direction agrees with snipe direction
"""
import os, sys, json, sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

FLIGHT_DB = "<repo_root>/Source/flight_recorder.db"
WATCH_DB  = "~/Jarvis/Database/v2/trading_forex.db"
WINDOW_DAYS = 14
MIN_FAN_PIPS = 4.0

def pip_size(p): return 0.01 if p.endswith("_JPY") else 0.0001

_cache = {}
def get_pair(wid):
    if wid in _cache: return _cache[wid]
    with sqlite3.connect(WATCH_DB, timeout=5) as wc:
        r = wc.execute("SELECT instrument FROM watch_suggestions WHERE id=?", (wid,)).fetchone()
    _cache[wid] = r[0] if r else None
    return _cache[wid]


def gather_geom(fc, watch_id, block_ts):
    cutoff = (datetime.fromisoformat(block_ts.replace("Z", "+00:00")) - timedelta(seconds=30)).isoformat()
    rows = fc.execute(
        "SELECT stage, data FROM flight_log WHERE timestamp >= ? AND timestamp <= ? "
        "AND data LIKE ? ORDER BY timestamp",
        (cutoff, block_ts, f'%\"watch_id\": {watch_id}%')
    ).fetchall()
    ctx = {}
    for stage, raw in rows:
        try: d = json.loads(raw)
        except Exception: continue
        gate = d.get("gate", "")
        if stage == "SNIPE_DIRECT_START":
            ctx["direction"] = d.get("direction")
        elif gate == "direction_aligned":
            ctx["fan_dir"] = d.get("fan_dir")
        elif gate == "ema_ordering":
            ctx["e21"] = d.get("e21"); ctx["e55"] = d.get("e55"); ctx["e100"] = d.get("e100")
    return ctx


def diagnose(t):
    """Return ('truly_exhausted' | 'NOT_exhausted' | 'unknown', reasons)."""
    e21, e55, e100 = t.get("e21"), t.get("e55"), t.get("e100")
    direction = t.get("direction")
    fan_dir = t.get("fan_dir")
    pair = t.get("pair")

    if not all([e21, e55, e100, direction, pair]):
        return "unknown", ["missing_data"]

    pip = pip_size(pair)
    fan_pips = (max(e21, e55, e100) - min(e21, e55, e100)) / pip
    bullish_ordered = e21 > e55 > e100
    bearish_ordered = e100 > e55 > e21
    ordered = bullish_ordered or bearish_ordered

    # Direction the fan implies
    implied_fan_dir = "bullish" if bullish_ordered else ("bearish" if bearish_ordered else "mixed")

    # Direction the snipe wants
    snipe_wants = "bullish" if direction == "BUY" else "bearish"

    reasons = []
    truly_exhausted = False

    if not ordered:
        truly_exhausted = True
        reasons.append("EMAs_not_ordered")
    if fan_pips < MIN_FAN_PIPS:
        truly_exhausted = True
        reasons.append(f"fan_collapsed_{fan_pips:.1f}p")
    if ordered and implied_fan_dir != snipe_wants:
        truly_exhausted = True
        reasons.append(f"snipe_against_fan_{snipe_wants}_vs_{implied_fan_dir}")

    if truly_exhausted:
        return "truly_exhausted", reasons
    return "NOT_exhausted", [f"ordered_{implied_fan_dir}_fan_{fan_pips:.1f}p_aligned_with_{direction}"]


def main():
    print(f"Auditing fan_exhaustion blocks: was the fan ACTUALLY exhausted?")
    print(f"Window: last {WINDOW_DAYS}d")
    print("=" * 88)

    results = []
    with sqlite3.connect(FLIGHT_DB, timeout=5) as fc:
        blocks = fc.execute(
            "SELECT timestamp, data FROM flight_log "
            "WHERE stage='SNIPE_GATE_BLOCKED' AND data LIKE '%fan_exhaust%' "
            "AND timestamp > datetime('now', ?) ORDER BY timestamp",
            (f'-{WINDOW_DAYS} days',)
        ).fetchall()

        for ts, raw in blocks:
            try: d = json.loads(raw)
            except Exception: continue
            wid = d.get("watch_id")
            fan_state = d.get("fan_state")
            if not wid: continue
            pair = get_pair(wid)
            if not pair: continue
            geom = gather_geom(fc, wid, ts)
            geom["pair"] = pair
            verdict, reasons = diagnose(geom)
            results.append({
                "ts": ts, "watch_id": wid, "pair": pair, "fan_state_label": fan_state,
                "direction": geom.get("direction"),
                "fan_dir": geom.get("fan_dir"),
                "e21": geom.get("e21"), "e55": geom.get("e55"), "e100": geom.get("e100"),
                "verdict": verdict, "reasons": reasons,
            })

    total = len(results)
    by_verdict = defaultdict(int)
    for r in results: by_verdict[r["verdict"]] += 1

    print(f"\nTotal fan_exhaustion blocks: {total}")
    print(f"\nVerdict on whether the fan was ACTUALLY exhausted at the moment of block:")
    for v, n in sorted(by_verdict.items(), key=lambda kv: -kv[1]):
        pct = 100 * n / total if total else 0
        print(f"  {v:<22}{n:>5}  ({pct:.1f}%)")

    # The "NOT_exhausted" group is the false-positive count
    fp = [r for r in results if r["verdict"] == "NOT_exhausted"]
    correct = [r for r in results if r["verdict"] == "truly_exhausted"]
    unknown = [r for r in results if r["verdict"] == "unknown"]

    print("\n" + "-" * 88)
    print(f"VERDICT BREAKDOWN (excluding {len(unknown)} unknowns):")
    if (len(fp) + len(correct)) > 0:
        fp_pct = 100 * len(fp) / (len(fp) + len(correct))
        print(f"  Gate was WRONG (fan was NOT exhausted):    {len(fp):>4}  ({fp_pct:.1f}%)")
        print(f"  Gate was RIGHT (fan was truly exhausted):  {len(correct):>4}  ({100-fp_pct:.1f}%)")

    print("\nBy classifier label (what fan_state did the classifier return when it blocked?):")
    by_label = defaultdict(lambda: {"NOT_exhausted": 0, "truly_exhausted": 0, "unknown": 0})
    for r in results: by_label[r["fan_state_label"]][r["verdict"]] += 1
    print(f"{'LABEL':<14}{'TRULY_EXH':>12}{'NOT_EXH':>10}{'UNKNOWN':>10}{'FALSE_POS%':>12}")
    for label, counts in sorted(by_label.items(), key=lambda kv: -(kv[1]['NOT_exhausted']+kv[1]['truly_exhausted'])):
        decided = counts["NOT_exhausted"] + counts["truly_exhausted"]
        fp_pct = 100 * counts["NOT_exhausted"] / decided if decided else 0
        print(f"  {label:<12}{counts['truly_exhausted']:>10}{counts['NOT_exhausted']:>10}"
              f"{counts['unknown']:>10}{fp_pct:>11.1f}%")

    # Reason histogram for "truly_exhausted" — was it E21 cross / collapse / counter-trend?
    print("\nWhen the gate was RIGHT, the reason was:")
    reason_hist = defaultdict(int)
    for r in correct:
        for reason in r["reasons"]:
            # Normalize fan-collapse pip values
            if reason.startswith("fan_collapsed_"): reason = "fan_collapsed"
            if reason.startswith("snipe_against_fan_"): reason = "snipe_against_fan"
            reason_hist[reason] += 1
    for reason, n in sorted(reason_hist.items(), key=lambda kv: -kv[1]):
        print(f"  {reason:<30}{n}")

    # Examples of false positives
    print("\nSample false positives (fan healthy, gate fired):")
    for r in fp[:8]:
        et = (datetime.fromisoformat(r["ts"].replace("Z","+00:00")) - timedelta(hours=4)).strftime("%m-%d %H:%M")
        print(f"  {et} ET  {r['pair']:<8} {r['direction']:<4} label={r['fan_state_label']:<11} "
              f"{r['reasons'][0]}")

    out = "/tmp/fan_exhaustion_actually_exhausted.json"
    with open(out, "w") as f: json.dump(results, f, indent=2, default=str)
    print(f"\nFull data: {out}")


if __name__ == "__main__":
    main()
