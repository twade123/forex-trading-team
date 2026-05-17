"""
AUDIT C: Compare entry-time signals (fan_state, fan_direction, fan_width_pct,
fan_ordered, momentum_state) across:
  - Quick-detection losers (<15 min, -3.4p avg)
  - Late-detection losers (60+ min, -21p avg)
  - Winners (the trades that worked)

Question: did losers enter with already-weak fan signals? If so, an entry-time
weak-signal check (still NOT a new gate per Tim — could be a "watch-the-trade-closer" tag) is the lever.
"""
import json
import sqlite3
import sys
from collections import Counter, defaultdict

DB_TRADES = "~/Jarvis/Database/v2/trading_forex.db"
AUDIT_FILE = "/tmp/ghost_v2/retrace_audit_losers.json"
OUT = "/tmp/ghost_v2/audit_C_entry_signals.json"


def fetch_signals(trade_ids: list, label: str):
    """Fetch entry-time signals for a list of trade IDs."""
    if not trade_ids:
        return []
    conn = sqlite3.connect(DB_TRADES)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in trade_ids)
    rows = conn.execute(f"""
        SELECT id, pair, direction, source, outcome_pips,
               max_favorable_excursion_pips, max_adverse_excursion_pips,
               fan_state, fan_direction, fan_ordered, fan_width_pct,
               e100_role, momentum_state, dual_cross_cascade, cascade_direction,
               retracement_type, confluence_score, session
        FROM live_trades WHERE id IN ({placeholders})
    """, [str(t) for t in trade_ids]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def main():
    audit = json.load(open(AUDIT_FILE))
    valid = [d for d in audit if 'error' not in d]

    quick_ids = [d['trade_id'] for d in valid
                 if d.get('detection_minutes_after_entry') is not None
                 and d['detection_minutes_after_entry'] < 15
                 and d.get('first_retrace_detection')]
    late_ids = [d['trade_id'] for d in valid
                if d.get('detection_minutes_after_entry') is not None
                and d['detection_minutes_after_entry'] >= 60
                and d.get('first_retrace_detection')]

    # Pull winners as control
    conn = sqlite3.connect(DB_TRADES)
    conn.row_factory = sqlite3.Row
    winners = [dict(r) for r in conn.execute("""
        SELECT id, pair, direction, source, outcome_pips, fan_state, fan_direction,
               fan_ordered, fan_width_pct, e100_role, momentum_state,
               dual_cross_cascade, cascade_direction, retracement_type,
               confluence_score, session
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= '2026-04-16' AND entry_time < '2026-05-16'
          AND status='closed' AND outcome='win'
    """).fetchall()]
    conn.close()

    quick = fetch_signals(quick_ids, "QUICK")
    late = fetch_signals(late_ids, "LATE")

    print(f"\n=== AUDIT C: ENTRY SIGNALS ===")
    print(f"QUICK losers (<15min detect, -3.4p avg): {len(quick)}")
    print(f"LATE  losers (60+min detect, -21p avg): {len(late)}")
    print(f"WINNERS (control):                       {len(winners)}")
    print()

    for label, cohort in [("QUICK losers", quick), ("LATE losers", late), ("WINNERS", winners)]:
        print(f"\n--- {label} (n={len(cohort)}) ---")
        if not cohort:
            continue
        fan_states = Counter(c['fan_state'] for c in cohort if c['fan_state'])
        fan_dirs = Counter(c['fan_direction'] for c in cohort if c['fan_direction'])
        fan_ordered = Counter(c['fan_ordered'] for c in cohort if c['fan_ordered'] is not None)
        momentum = Counter(c['momentum_state'] for c in cohort if c['momentum_state'])
        e100_role = Counter(c['e100_role'] for c in cohort if c['e100_role'])
        cascades = Counter(c['dual_cross_cascade'] for c in cohort if c['dual_cross_cascade'] is not None)

        widths = [c['fan_width_pct'] for c in cohort if c['fan_width_pct'] is not None]
        confs = [c['confluence_score'] for c in cohort if c['confluence_score'] is not None]

        print(f"  fan_state:        {dict(fan_states)}")
        print(f"  fan_direction:    {dict(fan_dirs)}")
        print(f"  fan_ordered:      {dict(fan_ordered)}")
        print(f"  momentum_state:   {dict(momentum)}")
        print(f"  e100_role:        {dict(e100_role)}")
        print(f"  dual_cross_cascade: {dict(cascades)}")
        if widths:
            print(f"  fan_width_pct: median {sorted(widths)[len(widths)//2]:.3f}, mean {sum(widths)/len(widths):.3f}")
        if confs:
            print(f"  confluence_score: median {sorted(confs)[len(confs)//2]:.1f}, mean {sum(confs)/len(confs):.1f}")

    # Smoking-gun check: are there signals that DIFFERENTIATE late losers from winners at entry?
    print(f"\n=== DIFFERENTIATORS: LATE LOSERS vs WINNERS ===")
    fields = ['fan_state', 'fan_direction', 'fan_ordered', 'momentum_state', 'e100_role']
    for f in fields:
        late_vals = Counter(c[f] for c in late if c[f])
        win_vals = Counter(c[f] for c in winners if c[f])
        late_total = sum(late_vals.values()) or 1
        win_total = sum(win_vals.values()) or 1
        diffs = []
        for v in set(late_vals.keys()) | set(win_vals.keys()):
            late_pct = late_vals[v] / late_total * 100
            win_pct = win_vals[v] / win_total * 100
            if abs(late_pct - win_pct) >= 10:
                diffs.append((v, late_pct, win_pct, late_pct - win_pct))
        if diffs:
            print(f"\n  {f} differentials >=10pp:")
            for v, lp, wp, d in sorted(diffs, key=lambda x: abs(x[3]), reverse=True):
                print(f"    {v}: LATE-loser {lp:.0f}% vs WINNER {wp:.0f}%  (Δ {d:+.0f}pp)")

    output = {
        "quick_losers": quick, "late_losers": late, "winners": winners,
    }
    with open(OUT, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults: {OUT}")


if __name__ == "__main__":
    main()
