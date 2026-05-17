import sqlite3, os, json
from datetime import datetime, timedelta

fr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flight_recorder.db")
conn = sqlite3.connect(fr_path, timeout=5)
conn.row_factory = sqlite3.Row
cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()

closes = conn.execute("SELECT COUNT(*) FROM flight_log WHERE stage = 'learning_audit' AND timestamp > ?", (cutoff,)).fetchone()[0]
print(f"trade_closes: {closes}")

wins = 0
losses = 0
audit_rows = conn.execute("SELECT data FROM flight_log WHERE stage = 'learning_audit' AND timestamp > ? AND data IS NOT NULL", (cutoff,)).fetchall()
for row in audit_rows:
    try:
        d = json.loads(row["data"]) if row["data"] else {}
        outcome = d.get("outcome", "")
        if outcome == "win":
            wins += 1
        elif outcome in ("loss", "lose"):
            losses += 1
    except:
        pass
print(f"wins: {wins}, losses: {losses}")

completes = conn.execute("SELECT COUNT(*) FROM flight_log WHERE stage = 'learning_complete' AND timestamp > ?", (cutoff,)).fetchone()[0]
print(f"learning_loops_complete: {completes}")

errs = conn.execute("SELECT COUNT(*) FROM flight_log WHERE stage LIKE 'learning_%' AND status = 'error' AND timestamp > ?", (cutoff,)).fetchone()[0]
print(f"errors: {errs}")

healthy = not (closes > 0 and completes == 0) and errs <= 2
print(f"healthy: {healthy}")

all_stages = ["learning_audit", "learning_scout", "learning_validator",
              "learning_guardian", "learning_knowledge", "learning_retro",
              "learning_drift", "learning_thesis", "learning_tuning",
              "learning_dashboard", "learning_complete"]
print("\nstage_coverage:")
for stage in all_stages:
    cnt = conn.execute("SELECT COUNT(*) FROM flight_log WHERE stage = ? AND timestamp > ?", (stage, cutoff)).fetchone()[0]
    pct = min(100, round(cnt / max(closes, 1) * 100, 1)) if closes > 0 else (100.0 if cnt > 0 else 0)
    print(f"  {stage}: {pct}%")

conn.close()
