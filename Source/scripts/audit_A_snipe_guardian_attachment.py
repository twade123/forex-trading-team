"""
AUDIT A: For the 21 losers with ZERO guardian_action events, check whether
guardian_spawn fired at all. If guardian never spawned → attachment bug.
If guardian spawned but never logged action → silent logging gap.

Focuses on the 14 snipe_direct losers in the zero-action bucket.
"""
import json
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

DB_FLIGHT = "~/Jarvis/Database/v2/flight_recorder.db"
AUDIT_FILE = "/tmp/ghost_v2/retrace_audit_losers.json"
OUT = "/tmp/ghost_v2/audit_A_snipe_attachment.json"


def main():
    audit = json.load(open(AUDIT_FILE))
    zero_action = [d for d in audit if 'error' not in d and d['guardian_action_count'] == 0]
    print(f"Losers with ZERO guardian_action events: {len(zero_action)}")

    conn = sqlite3.connect(DB_FLIGHT)
    results = []
    for d in zero_action:
        tid = d['trade_id']
        # Pull ALL flight_log events for this trade_id
        rows = conn.execute("""
            SELECT timestamp, stage, status, substr(data,1,300) as data
            FROM flight_log
            WHERE trade_id = ?
            ORDER BY timestamp
        """, (tid,)).fetchall()

        stages = [r[1] for r in rows]
        has_spawn = 'guardian_spawn' in stages
        has_restore = 'guardian_state_restore' in stages
        has_action = 'guardian_action' in stages
        has_threat = 'guardian_threat' in stages

        results.append({
            "trade_id": tid,
            "pair": d['pair'],
            "source": d['source'],
            "outcome_pips": d['outcome_pips'],
            "duration_min": d['duration_min'],
            "event_count": len(rows),
            "stages_seen": list(set(stages)),
            "has_guardian_spawn": has_spawn,
            "has_guardian_state_restore": has_restore,
            "has_guardian_action": has_action,
            "has_guardian_threat": has_threat,
        })
    conn.close()

    # Summary
    print(f"\n=== AUDIT A SUMMARY ===")
    no_spawn = [r for r in results if not r['has_guardian_spawn']]
    spawn_no_action = [r for r in results if r['has_guardian_spawn'] and not r['has_guardian_action']]
    print(f"  Guardian NEVER spawned: {len(no_spawn)} ({len(no_spawn)*100/len(results):.0f}%)")
    print(f"  Guardian spawned but NO actions: {len(spawn_no_action)}")
    print()
    print(f"By source — guardian never spawned:")
    from collections import Counter
    no_spawn_src = Counter(r['source'] for r in no_spawn)
    print(f"  {dict(no_spawn_src)}")
    print()
    print(f"Lost pips on the no-spawn cohort: {sum(r['outcome_pips'] for r in no_spawn if r['outcome_pips']):+.1f}p across {len(no_spawn)} trades")

    # Show the actual stages observed (helps diagnose)
    all_stages = Counter()
    for r in results:
        for s in r['stages_seen']:
            all_stages[s] += 1
    print(f"\nStages observed across zero-action losers (top):")
    for s, c in all_stages.most_common(15):
        print(f"  {s}: {c}")

    with open(OUT, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults: {OUT}")


if __name__ == "__main__":
    main()
