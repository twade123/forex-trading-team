#!/usr/bin/env python3
"""
Clean up the 31 bad learning loop records that ran on incomplete data.

What happened: The learning loop ran on trades that had 0% scout accuracy and
0/31 validator correct because the data was split between manual_trades and
live_trades. Now that tables are unified, these bad records should be removed
so the next learning run starts fresh with complete data.

Cleanup targets:
1. trade_audits: 31 records where scout_signal_accuracy=0 AND entry_type is empty
2. flight_log: ~223 learning_* entries from the same bad batch (all from 2026-03-25T10:25-10:26)
"""
import sqlite3
import json

# ── 1. Clean trade_audits ──
print('=' * 70)
print('CLEANING trade_audits')
print('=' * 70)

db = '~/Jarvis/Database/v2/trading_forex.db'
conn = sqlite3.connect(db, timeout=10)

before = conn.execute('SELECT COUNT(*) FROM trade_audits').fetchone()[0]
print('Before: %d records' % before)

# Target: all 31 bad records from the 2026-03-25 batch
# They ALL have scout_signal_accuracy=0.0 AND entry_type is empty
bad_ids = conn.execute('''
    SELECT id FROM trade_audits 
    WHERE scout_signal_accuracy = 0.0 
      AND (entry_type IS NULL OR entry_type = '')
''').fetchall()
print('Bad records found: %d' % len(bad_ids))

if bad_ids:
    conn.execute('''
        DELETE FROM trade_audits 
        WHERE scout_signal_accuracy = 0.0 
          AND (entry_type IS NULL OR entry_type = '')
    ''')
    conn.commit()

after = conn.execute('SELECT COUNT(*) FROM trade_audits').fetchone()[0]
print('After: %d records' % after)
print('Deleted: %d' % (before - after))
conn.close()

# ── 2. Clean flight_log learning entries ──
print()
print('=' * 70)
print('CLEANING flight_log learning entries')
print('=' * 70)

fr_db = '<repo_root>/Source/flight_recorder.db'
fr = sqlite3.connect(fr_db, timeout=10)

before_fl = fr.execute("SELECT COUNT(*) FROM flight_log WHERE stage LIKE 'learning_%%'").fetchone()[0]
print('Before: %d learning entries' % before_fl)

# The bad batch all ran on 2026-03-25 between 10:25 and 10:27
# They correspond to cycle_ids starting with 'recon_'
bad_learning = fr.execute('''
    SELECT COUNT(*) FROM flight_log 
    WHERE stage LIKE 'learning_%%'
      AND timestamp >= '2026-03-25T10:25:00'
      AND timestamp <= '2026-03-25T10:27:00'
''').fetchone()[0]
print('Bad learning entries (2026-03-25 10:25-10:27): %d' % bad_learning)

if bad_learning > 0:
    fr.execute('''
        DELETE FROM flight_log 
        WHERE stage LIKE 'learning_%%'
          AND timestamp >= '2026-03-25T10:25:00'
          AND timestamp <= '2026-03-25T10:27:00'
    ''')
    fr.commit()

after_fl = fr.execute("SELECT COUNT(*) FROM flight_log WHERE stage LIKE 'learning_%%'").fetchone()[0]
print('After: %d learning entries' % after_fl)
print('Deleted: %d' % (before_fl - after_fl))

fr.close()

print()
print('=' * 70)
print('CLEANUP COMPLETE')
print('=' * 70)
print('- trade_audits: %d -> %d (-%d bad records)' % (before, after, before - after))
print('- flight_log learning: %d -> %d (-%d bad entries)' % (before_fl, after_fl, before_fl - after_fl))
print()
print('The knowledge.json live_performance sections were already cleaned in the previous session.')
print('Next learning loop run will start fresh with complete unified data from live_trades.')
