"""
Audit: for each loser in the 30d cohort, did guardian's retrace_zone detection
fire? If so, at what pnl_pips? Did it act on the detection?

Pulls flight_log for each loser. Extracts retrace_zone transitions, peak pnl
before retrace, time-to-detection from entry, time-to-exit from detection.

Goal: identify whether guardian's existing retrace detection is too LATE,
and whether earlier detection (fan-velocity, bb-compression, bar character)
could save losers without adding new gates.
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
from oanda_client import _parse_oanda_time  # noqa

DB_TRADES = "~/Jarvis/Database/v2/trading_forex.db"
DB_FLIGHT = "~/Jarvis/Database/v2/flight_recorder.db"
OUT = "/tmp/ghost_v2/retrace_audit_losers.json"


def parse_dt(s: str):
    if not s:
        return None
    s2 = s.replace(" ", "T")
    if not s2.endswith("Z") and "+" not in s2.split("T", 1)[-1]:
        s2 = s2 + "Z"
    return _parse_oanda_time(s2)


def fetch_losers() -> list:
    conn = sqlite3.connect(DB_TRADES)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, pair, direction, entry_time, exit_time, outcome_pips,
               max_favorable_excursion_pips, max_adverse_excursion_pips,
               exit_trigger, cycle_id, source
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= '2026-04-16' AND entry_time < '2026-05-16'
          AND status='closed' AND outcome='loss'
          AND exit_time IS NOT NULL
        ORDER BY entry_time
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_flight_for_trade(trade_id: str, entry_time: str, exit_time: str) -> list:
    """Pull events for this trade between entry and exit, matched via the
    flight_log.trade_id column (top-level, indexed)."""
    conn = sqlite3.connect(DB_FLIGHT)
    rows = conn.execute("""
        SELECT timestamp, stage, status, data
        FROM flight_log
        WHERE timestamp >= ? AND timestamp <= ?
          AND trade_id = ?
        ORDER BY timestamp
    """, (entry_time, exit_time, str(trade_id))).fetchall()
    conn.close()
    return rows


def fetch_flight_by_cycle(cycle_id: str, entry_time: str, exit_time: str) -> list:
    """Fallback: pull by cycle_id. Less precise (other trades may share cycle window)."""
    conn = sqlite3.connect(DB_FLIGHT)
    rows = conn.execute("""
        SELECT timestamp, stage, status, data
        FROM flight_log
        WHERE timestamp >= ? AND timestamp <= ?
          AND cycle_id = ?
        ORDER BY timestamp
    """, (entry_time, exit_time, cycle_id)).fetchall()
    conn.close()
    return rows


def audit_one(trade: dict) -> dict:
    """Audit one loser. Returns retrace-detection summary."""
    trade_id = str(trade["id"])
    entry_time = trade["entry_time"]
    exit_time = trade["exit_time"]

    # Try trade_id-tagged events first (most precise)
    events = fetch_flight_for_trade(trade_id, entry_time, exit_time)

    # Filter to guardian_action events
    guardian_events = []
    for ts, stage, status, data_raw in events:
        if stage in ("guardian_action", "guardian_threat", "guardian_spawn", "guardian_state_restore"):
            try:
                data = json.loads(data_raw) if data_raw else {}
            except Exception:
                data = {}
            guardian_events.append({"ts": ts, "stage": stage, "data": data})

    # Extract retrace_zone transitions
    zone_transitions = []
    last_zone = None
    for e in guardian_events:
        zone = e["data"].get("retrace_zone") or e["data"].get("retrace_state")
        pnl = e["data"].get("pnl_pips")
        if zone and zone != last_zone:
            zone_transitions.append({
                "ts": e["ts"], "zone": zone, "pnl_pips": pnl, "stage": e["stage"]
            })
            last_zone = zone

    # First non-trending transition (retrace detection)
    first_retrace = None
    for t in zone_transitions:
        if t["zone"] not in ("trending", None):
            first_retrace = t
            break

    # Last guardian action
    last_action = None
    for e in reversed(guardian_events):
        if e["stage"] == "guardian_action":
            last_action = e
            break

    # Time from entry to first retrace detection
    et = parse_dt(entry_time)
    xt = parse_dt(exit_time)
    duration_min = (xt - et).total_seconds() / 60 if et and xt else None

    detection_min = None
    if first_retrace:
        det_t = parse_dt(first_retrace["ts"])
        if et and det_t:
            detection_min = (det_t - et).total_seconds() / 60

    actions = [e for e in guardian_events if e["stage"] == "guardian_action"]
    action_types = set()
    for a in actions:
        action_types.add(a["data"].get("action", "?"))

    return {
        "trade_id": trade_id,
        "pair": trade["pair"],
        "direction": trade["direction"],
        "source": trade["source"],
        "entry_time": entry_time,
        "exit_time": exit_time,
        "duration_min": round(duration_min, 1) if duration_min else None,
        "outcome_pips": round(float(trade["outcome_pips"]), 1) if trade["outcome_pips"] else None,
        "mfe_pips": round(float(trade["max_favorable_excursion_pips"]), 1) if trade["max_favorable_excursion_pips"] else None,
        "mae_pips": round(float(trade["max_adverse_excursion_pips"]), 1) if trade["max_adverse_excursion_pips"] else None,
        "exit_trigger": trade["exit_trigger"],
        "guardian_events_count": len(guardian_events),
        "guardian_action_count": len(actions),
        "guardian_action_types": sorted(action_types),
        "zone_transitions": zone_transitions,
        "first_retrace_detection": first_retrace,
        "detection_minutes_after_entry": round(detection_min, 1) if detection_min else None,
        "last_action": last_action,
    }


def main():
    losers = fetch_losers()
    print(f"Auditing {len(losers)} losers...")
    results = []
    for i, t in enumerate(losers, 1):
        try:
            audit = audit_one(t)
            results.append(audit)
            if i % 20 == 0:
                print(f"  [{i}/{len(losers)}] {t['pair']} {t['id']} — {audit['guardian_action_count']} guardian actions, {len(audit['zone_transitions'])} zone transitions")
        except Exception as e:
            print(f"  [{i}/{len(losers)}] {t['id']} ERROR: {e}")
            results.append({"trade_id": str(t["id"]), "error": str(e)})

    with open(OUT, "w") as f:
        json.dump(results, f, indent=2)

    # Summary
    valid = [r for r in results if "error" not in r]
    no_actions = [r for r in valid if r["guardian_action_count"] == 0]
    detected_retrace = [r for r in valid if r.get("first_retrace_detection")]
    no_detection = [r for r in valid if not r.get("first_retrace_detection") and r["guardian_action_count"] > 0]

    print(f"\n=== SUMMARY ({len(valid)} losers audited) ===")
    print(f"Losers with ZERO guardian actions: {len(no_actions)} (logging miss or no guardian attached)")
    print(f"Losers where retrace detected: {len(detected_retrace)}")
    print(f"Losers with guardian actions but no retrace detection: {len(no_detection)}")

    if detected_retrace:
        # How early was detection?
        with_timing = [r for r in detected_retrace if r.get("detection_minutes_after_entry") is not None]
        if with_timing:
            times = [r["detection_minutes_after_entry"] for r in with_timing]
            print(f"\nDetection timing on {len(with_timing)} losers:")
            print(f"  Median time to detection: {sorted(times)[len(times)//2]:.1f} min after entry")
            print(f"  Mean time to detection: {sum(times)/len(times):.1f} min")
            print(f"  <5 min: {sum(1 for t in times if t<5)}")
            print(f"  5-15 min: {sum(1 for t in times if 5<=t<15)}")
            print(f"  15-30 min: {sum(1 for t in times if 15<=t<30)}")
            print(f"  >30 min: {sum(1 for t in times if t>=30)}")

        # First zone breached
        first_zones = [r["first_retrace_detection"]["zone"] for r in detected_retrace if r.get("first_retrace_detection")]
        from collections import Counter
        print(f"\nFirst zone breached: {dict(Counter(first_zones))}")

        # pnl at detection
        pnls_at_detect = [r["first_retrace_detection"].get("pnl_pips") for r in detected_retrace
                          if r.get("first_retrace_detection", {}).get("pnl_pips") is not None]
        if pnls_at_detect:
            print(f"\npnl_pips at first detection: mean {sum(pnls_at_detect)/len(pnls_at_detect):+.1f}p")
            print(f"  Negative (already losing): {sum(1 for p in pnls_at_detect if p<0)}/{len(pnls_at_detect)}")
            print(f"  Positive (still up): {sum(1 for p in pnls_at_detect if p>0)}/{len(pnls_at_detect)}")
            print(f"  At/near 0: {sum(1 for p in pnls_at_detect if -1<=p<=1)}/{len(pnls_at_detect)}")

    print(f"\nResults: {OUT}")


if __name__ == "__main__":
    main()
