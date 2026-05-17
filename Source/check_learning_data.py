#!/usr/bin/env python3
"""Check what the 31 learning loops actually adjusted."""
import sqlite3
import json

fr_db = '<repo_root>/Source/flight_recorder.db'
fr = sqlite3.connect(fr_db, timeout=10)
fr.row_factory = sqlite3.Row

print('=' * 70)
print('LEARNING_KNOWLEDGE entries (what was written to knowledge.json):')
print('=' * 70)
know = fr.execute('''
    SELECT data, note, pair, timestamp FROM flight_log
    WHERE stage = 'learning_knowledge'
    ORDER BY timestamp DESC LIMIT 5
''').fetchall()
for i, e in enumerate(know):
    print('[%d] pair=%s time=%s' % (i+1, e['pair'], e['timestamp']))
    print('    note: %s' % (str(e['note'])[:200] if e['note'] else 'NULL'))
    try:
        d = json.loads(e['data']) if e['data'] else {}
        print('    data: %s' % json.dumps(d)[:300])
    except:
        print('    data: %s' % str(e['data'])[:200])
    print()

print('=' * 70)
print('LEARNING_SCOUT entries (scout signal adjustments):')
print('=' * 70)
scout = fr.execute('''
    SELECT data, note, pair, timestamp FROM flight_log
    WHERE stage = 'learning_scout'
    ORDER BY timestamp DESC LIMIT 5
''').fetchall()
for i, e in enumerate(scout):
    print('[%d] pair=%s time=%s' % (i+1, e['pair'], e['timestamp']))
    print('    note: %s' % (str(e['note'])[:200] if e['note'] else 'NULL'))
    try:
        d = json.loads(e['data']) if e['data'] else {}
        print('    data: %s' % json.dumps(d)[:300])
    except:
        print('    data: %s' % str(e['data'])[:200])
    print()

print('=' * 70)
print('LEARNING_DRIFT entries (parameter adjustments):')
print('=' * 70)
drift = fr.execute('''
    SELECT data, note, pair, timestamp FROM flight_log
    WHERE stage = 'learning_drift'
    ORDER BY timestamp DESC LIMIT 5
''').fetchall()
for i, e in enumerate(drift):
    print('[%d] pair=%s time=%s' % (i+1, e['pair'], e['timestamp']))
    print('    note: %s' % (str(e['note'])[:200] if e['note'] else 'NULL'))
    try:
        d = json.loads(e['data']) if e['data'] else {}
        print('    data: %s' % json.dumps(d)[:300])
    except:
        print('    data: %s' % str(e['data'])[:200])
    print()

print('=' * 70)
print('LEARNING_VALIDATOR entries:')
print('=' * 70)
val = fr.execute('''
    SELECT data, note, pair, timestamp FROM flight_log
    WHERE stage = 'learning_validator'
    ORDER BY timestamp DESC LIMIT 5
''').fetchall()
for i, e in enumerate(val):
    print('[%d] pair=%s time=%s' % (i+1, e['pair'], e['timestamp']))
    print('    note: %s' % (str(e['note'])[:200] if e['note'] else 'NULL'))
    try:
        d = json.loads(e['data']) if e['data'] else {}
        print('    data: %s' % json.dumps(d)[:300])
    except:
        print('    data: %s' % str(e['data'])[:200])
    print()

fr.close()
