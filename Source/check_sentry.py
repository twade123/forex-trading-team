import sys, sqlite3, os, json
sys.path.insert(0, '<repo_root>/Source')

fr_path = '<repo_root>/Source/flight_recorder.db'
if not os.path.exists(fr_path):
    print(f'flight_recorder.db NOT FOUND at {fr_path}')
    import glob
    for f in glob.glob('~/Jarvis/**/flight_recorder.db', recursive=True):
        print(f'  Found at: {f}')
    sys.exit(1)

conn = sqlite3.connect(fr_path)
conn.row_factory = sqlite3.Row

total = conn.execute('SELECT COUNT(*) FROM flight_log').fetchone()[0]
print(f'flight_log total rows: {total}')

from datetime import datetime, timedelta
cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
recent = conn.execute('SELECT COUNT(*) FROM flight_log WHERE timestamp > ?', (cutoff,)).fetchone()[0]
print(f'Last 24h: {recent}')

stages = conn.execute('SELECT stage, COUNT(*) as cnt FROM flight_log GROUP BY stage ORDER BY cnt DESC').fetchall()
print('\nStage breakdown:')
for s in stages:
    print(f'  {s["stage"]}: {s["cnt"]}')

learning = conn.execute("SELECT stage, COUNT(*) as cnt FROM flight_log WHERE stage LIKE 'learning_%' GROUP BY stage ORDER BY cnt DESC").fetchall()
print('\nLearning stages:')
for s in learning:
    print(f'  {s["stage"]}: {s["cnt"]}')

recent_learning = conn.execute("SELECT stage, status, timestamp, note FROM flight_log WHERE stage LIKE 'learning_%' ORDER BY timestamp DESC LIMIT 5").fetchall()
print('\nMost recent learning entries:')
for r in recent_learning:
    print(f'  {r["timestamp"]} | {r["stage"]} | {r["status"]} | {(r["note"] or "")[:80]}')

conn.close()
