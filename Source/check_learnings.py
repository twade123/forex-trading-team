import sys, sqlite3, os, json
sys.path.insert(0, '<repo_root>/Source')
from db_pool import get_trading_forex

# 1. Check trade dates - are last night's trades in there?
print("=" * 60)
print("TRADE DATES (most recent first)")
print("=" * 60)
conn = get_trading_forex()
conn.row_factory = sqlite3.Row
trades = conn.execute("""
    SELECT id, pair, direction, outcome, pnl_pips, entry_time, exit_time
    FROM live_trades
    WHERE outcome IS NOT NULL AND outcome != ''
    ORDER BY entry_time DESC
    LIMIT 15
""").fetchall()
for t in trades:
    print(f"  {t['entry_time'][:16]} | {t['pair']:8s} {t['direction']:5s} | {t['outcome']:4s} | {t['pnl_pips']:+.1f}p")
conn.close()

# 2. What did the learning pipeline actually produce?
print("\n" + "=" * 60)
print("LEARNING PIPELINE OUTPUTS (from flight_log)")
print("=" * 60)
fr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flight_recorder.db")
fr = sqlite3.connect(fr_path)
fr.row_factory = sqlite3.Row

# Learning audit data - what did it find?
audits = fr.execute("""
    SELECT data, note FROM flight_log
    WHERE stage = 'learning_audit' AND data IS NOT NULL
    ORDER BY timestamp DESC LIMIT 5
""").fetchall()
print("\nSample learning_audit entries:")
for a in audits:
    try:
        d = json.loads(a['data'])
        print(f"  {d.get('pair','?')} | outcome={d.get('outcome','?')} | "
              f"scout_accuracy={d.get('scout_signal_accuracy','?')} | "
              f"entry_timing={d.get('entry_timing_score','?')} | "
              f"exit_quality={d.get('exit_quality_score','?')}")
    except:
        print(f"  note: {a['note']}")

# Scout learnings
print("\nScout learnings:")
scouts = fr.execute("""
    SELECT data, note FROM flight_log
    WHERE stage = 'learning_scout' AND data IS NOT NULL
    ORDER BY timestamp DESC LIMIT 5
""").fetchall()
for s in scouts:
    try:
        d = json.loads(s['data'])
        print(f"  {json.dumps(d, indent=2)[:200]}")
    except:
        print(f"  {s['note']}")

# Validator learnings
print("\nValidator learnings:")
vals = fr.execute("""
    SELECT data, note FROM flight_log
    WHERE stage = 'learning_validator' AND data IS NOT NULL
    ORDER BY timestamp DESC LIMIT 5
""").fetchall()
for v in vals:
    try:
        d = json.loads(v['data'])
        print(f"  {json.dumps(d, indent=2)[:300]}")
    except:
        print(f"  {v['note']}")

# Knowledge vault writes
print("\nKnowledge vault writes:")
kvs = fr.execute("""
    SELECT data, note FROM flight_log
    WHERE stage = 'learning_knowledge' AND data IS NOT NULL
    ORDER BY timestamp DESC LIMIT 5
""").fetchall()
for k in kvs:
    try:
        d = json.loads(k['data'])
        print(f"  {json.dumps(d, indent=2)[:300]}")
    except:
        print(f"  {k['note']}")

# Drift detections
print("\nDrift alerts:")
drifts = fr.execute("""
    SELECT data, note FROM flight_log
    WHERE stage = 'learning_drift' AND data IS NOT NULL
    ORDER BY timestamp DESC
""").fetchall()
for dr in drifts:
    try:
        d = json.loads(dr['data'])
        print(f"  {json.dumps(d, indent=2)[:300]}")
    except:
        print(f"  {dr['note']}")

# Learning complete summaries
print("\nLearning complete summaries (last 5):")
completes = fr.execute("""
    SELECT data, note FROM flight_log
    WHERE stage = 'learning_complete' AND data IS NOT NULL
    ORDER BY timestamp DESC LIMIT 5
""").fetchall()
for c in completes:
    try:
        d = json.loads(c['data'])
        print(f"  learnings={d.get('learnings_count','?')} | dur={d.get('duration_ms','?')}ms | {c['note']}")
    except:
        print(f"  {c['note']}")

# 3. Check trade_audits for the analysis quality
print("\n" + "=" * 60)
print("TRADE AUDIT QUALITY (from trade_audits table)")
print("=" * 60)
conn2 = get_trading_forex()
conn2.row_factory = sqlite3.Row
audits2 = conn2.execute("""
    SELECT pair, direction, outcome, pnl_pips, setup_name,
           scout_signal_accuracy, scout_thesis_correct,
           entry_timing_score, exit_quality_score,
           max_favorable_pips, max_adverse_pips,
           guardian_accuracy, validator_verdict, validator_correct,
           accuracy_trend
    FROM trade_audits
    ORDER BY audited_at DESC LIMIT 10
""").fetchall()
for a in audits2:
    print(f"  {a['pair']:8s} {a['direction']:5s} | {a['outcome']:4s} {a['pnl_pips']:+6.1f}p | "
          f"scout_acc={a['scout_signal_accuracy']:.0f}% thesis={'Y' if a['scout_thesis_correct'] else 'N'} | "
          f"entry={a['entry_timing_score']:.0f} exit={a['exit_quality_score']:.0f} | "
          f"MFE={a['max_favorable_pips']:.1f} MAE={a['max_adverse_pips']:.1f} | "
          f"trend={a['accuracy_trend']}")

# Aggregate stats
stats = conn2.execute("""
    SELECT
        COUNT(*) as total,
        AVG(scout_signal_accuracy) as avg_scout_acc,
        AVG(entry_timing_score) as avg_entry,
        AVG(exit_quality_score) as avg_exit,
        AVG(max_favorable_pips) as avg_mfe,
        AVG(max_adverse_pips) as avg_mae,
        SUM(CASE WHEN scout_thesis_correct = 1 THEN 1 ELSE 0 END) as thesis_correct,
        SUM(CASE WHEN validator_correct = 1 THEN 1 ELSE 0 END) as val_correct
    FROM trade_audits
""").fetchone()
print(f"\nAGGREGATE: {stats['total']} audits")
print(f"  Avg scout accuracy: {stats['avg_scout_acc']:.1f}%")
print(f"  Scout thesis correct: {stats['thesis_correct']}/{stats['total']}")
print(f"  Avg entry timing: {stats['avg_entry']:.1f}/100")
print(f"  Avg exit quality: {stats['avg_exit']:.1f}/100")
print(f"  Avg MFE: {stats['avg_mfe']:.1f} pips")
print(f"  Avg MAE: {stats['avg_mae']:.1f} pips")
print(f"  Validator correct: {stats['val_correct']}/{stats['total']}")

conn2.close()
fr.close()
