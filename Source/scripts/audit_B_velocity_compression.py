"""
AUDIT B: For the 11 Quick-detection losers (<15 min, -3.4p avg loss),
compare against the 27 Late-detection losers (60+ min, -21p avg loss).

Question: did fan-velocity / BB-compression signals fire BEFORE the
position-based retrace_zone transition? If yes, those signals are
candidates for earlier-detection logic.

Pulls flight_log guardian_action / guardian_state_restore events that
contain BB-width, fan_state, and any velocity-indicating fields.
"""
import json
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

DB_FLIGHT = "~/Jarvis/Database/v2/flight_recorder.db"
AUDIT_FILE = "/tmp/ghost_v2/retrace_audit_losers.json"
OUT = "/tmp/ghost_v2/audit_B_velocity_compression.json"


def fetch_indicator_events(trade_id: str):
    """Pull all events with indicator-rich data fields for this trade."""
    conn = sqlite3.connect(DB_FLIGHT)
    rows = conn.execute("""
        SELECT timestamp, stage, data
        FROM flight_log
        WHERE trade_id = ?
          AND (data LIKE '%fan_state%' OR data LIKE '%bb_width%'
               OR data LIKE '%fan_velocity%' OR data LIKE '%bb_expanding%'
               OR data LIKE '%retrace_zone%' OR data LIKE '%e55%' OR data LIKE '%e21%')
        ORDER BY timestamp
    """, (str(trade_id),)).fetchall()
    conn.close()
    return rows


def extract_signals(rows):
    """Return time-series of (ts, fan_state, bb_width, retrace_zone, pnl) for each event."""
    signals = []
    for ts, stage, data_raw in rows:
        try:
            d = json.loads(data_raw) if data_raw else {}
        except Exception:
            continue
        sig = {
            "ts": ts,
            "stage": stage,
            "fan_state": d.get("fan_state"),
            "bb_width": d.get("bb_width"),
            "bb_expanding": d.get("bb_expanding"),
            "retrace_zone": d.get("retrace_zone"),
            "retrace_state": d.get("retrace_state"),
            "pnl_pips": d.get("pnl_pips"),
            "e55": d.get("e55"),
            "e100": d.get("e100"),
            "anchor": d.get("anchor"),
        }
        # Drop empty entries
        if any(v is not None for k, v in sig.items() if k not in ("ts", "stage")):
            signals.append(sig)
    return signals


def main():
    audit = json.load(open(AUDIT_FILE))
    valid = [d for d in audit if 'error' not in d]
    detected = [d for d in valid if d.get('first_retrace_detection') and d.get('detection_minutes_after_entry') is not None]
    quick = [d for d in detected if d['detection_minutes_after_entry'] < 15]
    late = [d for d in detected if d['detection_minutes_after_entry'] >= 60]

    print(f"Quick-detection losers (<15 min): {len(quick)} (avg loss -3.4p)")
    print(f"Late-detection losers (60+ min): {len(late)} (avg loss -21p)")

    # For each cohort, sample indicator events
    cohorts = {"QUICK": quick, "LATE": late}
    output = {}
    for label, cohort in cohorts.items():
        print(f"\n=== {label} cohort ({len(cohort)}) ===")
        cohort_data = []
        velocity_present_count = 0
        bb_present_count = 0
        bb_contracting_count = 0
        fan_peaked_count = 0
        for d in cohort[:20]:  # sample up to 20
            tid = d['trade_id']
            rows = fetch_indicator_events(tid)
            sigs = extract_signals(rows)
            # Count features present
            has_velocity = any(s.get('fan_state') for s in sigs)
            has_bb_data = any(s.get('bb_width') is not None or s.get('bb_expanding') is not None for s in sigs)
            bb_contracting = any(s.get('bb_expanding') is False for s in sigs)
            fan_peaked = any(s.get('fan_state') in ('peaked','contracting','converging') for s in sigs)
            if has_velocity: velocity_present_count += 1
            if has_bb_data: bb_present_count += 1
            if bb_contracting: bb_contracting_count += 1
            if fan_peaked: fan_peaked_count += 1
            cohort_data.append({
                "trade_id": tid, "pair": d['pair'], "outcome_pips": d['outcome_pips'],
                "detection_min": d['detection_minutes_after_entry'],
                "fan_states_seen": list(set(s['fan_state'] for s in sigs if s.get('fan_state'))),
                "bb_expanding_states_seen": list(set(s['bb_expanding'] for s in sigs if s.get('bb_expanding') is not None)),
                "n_indicator_events": len(sigs),
                "first_bb_contracting_ts": next((s['ts'] for s in sigs if s.get('bb_expanding') is False), None),
                "first_fan_peaked_ts": next((s['ts'] for s in sigs if s.get('fan_state') in ('peaked','contracting','converging')), None),
            })
        sample_n = min(20, len(cohort))
        print(f"  Sampled {sample_n} of {len(cohort)}")
        print(f"  fan_state data present: {velocity_present_count}/{sample_n}")
        print(f"  BB data present:        {bb_present_count}/{sample_n}")
        print(f"  BB CONTRACTING observed: {bb_contracting_count}/{sample_n}")
        print(f"  Fan peaked/contracting/converging observed: {fan_peaked_count}/{sample_n}")
        output[label] = cohort_data

    with open(OUT, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nDetailed per-trade indicator timeline: {OUT}")


if __name__ == "__main__":
    main()
