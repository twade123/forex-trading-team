#!/usr/bin/env python3
"""Check the 31 bad audit records and learning loop entries."""
import sqlite3
import json
import os

db = '~/Jarvis/Database/v2/trading_forex.db'
conn = sqlite3.connect(db, timeout=10)
conn.row_factory = sqlite3.Row

print('=' * 70)
print('TRADE AUDITS - BAD DATA ANALYSIS')
print('=' * 70)

total = conn.execute('SELECT COUNT(*) FROM trade_audits').fetchone()[0]
bad_scout = conn.execute('SELECT COUNT(*) FROM trade_audits WHERE scout_signal_accuracy = 0.0').fetchone()[0]
bad_val = conn.execute('SELECT COUNT(*) FROM trade_audits WHERE validator_correct = 0').fetchone()[0]
bad_entry = conn.execute('SELECT COUNT(*) FROM trade_audits WHERE entry_type IS NULL OR entry_type = ""').fetchone()[0]

print('Total audit records: %d' % total)
print('Scout accuracy=0.0: %d/%d (ALL are zero = bad data)' % (bad_scout, total))
print('Validator correct=0: %d/%d (ALL are zero = bad data)' % (bad_val, total))
print('Missing entry_type: %d/%d' % (bad_entry, total))

outcomes = conn.execute('SELECT outcome, COUNT(*) FROM trade_audits GROUP BY outcome').fetchall()
print('Outcomes: %s' % ', '.join('%s=%d' % (r[0], r[1]) for r in outcomes))
print()
conn.close()

# ── Flight log ──
print('=' * 70)
print('FLIGHT_LOG - LEARNING PIPELINE ENTRIES')
print('=' * 70)

# Try multiple possible paths
fr_paths = [
    '<repo_root>/Source/flight_recorder.db',
    '<repo_root>/Source/Data/flight_recorder.db',
    '~/Jarvis/Database/v2/flight_recorder.db',
]

fr = None
for p in fr_paths:
    if os.path.exists(p) and os.path.getsize(p) > 0:
        try:
            fr = sqlite3.connect(p, timeout=10)
            fr.row_factory = sqlite3.Row
            # verify it has flight_log
            fr.execute('SELECT COUNT(*) FROM flight_log')
            print('Using: %s' % p)
            break
        except:
            fr = None

if fr is None:
    print('Could not find flight_recorder.db with flight_log table')
    for p in fr_paths:
        exists = os.path.exists(p)
        size = os.path.getsize(p) if exists else 0
        print('  %s exists=%s size=%d' % (p, exists, size))
else:
    stages = fr.execute('''
        SELECT stage, COUNT(*) as cnt FROM flight_log
        WHERE stage LIKE 'learning_%%'
        GROUP BY stage ORDER BY stage
    ''').fetchall()
    print('Learning stages:')
    for s in stages:
        print('  %s: %d' % (s['stage'], s['cnt']))
    total_learning = sum(s['cnt'] for s in stages)
    print('Total learning entries: %d' % total_learning)

    # Check learning_knowledge payloads
    print()
    print('LEARNING_KNOWLEDGE PAYLOADS (what was written to knowledge.json):')
    know = fr.execute('''
        SELECT payload, timestamp FROM flight_log
        WHERE stage = 'learning_knowledge'
        ORDER BY timestamp DESC LIMIT 3
    ''').fetchall()
    for i, e in enumerate(know):
        try:
            p = json.loads(e['payload']) if e['payload'] else {}
            print('  [%d] %s | %s' % (i+1, e['timestamp'], json.dumps(p)[:250]))
        except:
            print('  [%d] %s | raw: %s' % (i+1, e['timestamp'], str(e['payload'])[:200]))

    # Check learning_drift payloads - these show parameter adjustments
    print()
    print('LEARNING_DRIFT PAYLOADS (parameter drift/adjustments):')
    drift = fr.execute('''
        SELECT payload, timestamp FROM flight_log
        WHERE stage = 'learning_drift'
        ORDER BY timestamp DESC LIMIT 3
    ''').fetchall()
    for i, e in enumerate(drift):
        try:
            p = json.loads(e['payload']) if e['payload'] else {}
            print('  [%d] %s | %s' % (i+1, e['timestamp'], json.dumps(p)[:250]))
        except:
            print('  [%d] %s | raw: %s' % (i+1, e['timestamp'], str(e['payload'])[:200]))

    fr.close()
