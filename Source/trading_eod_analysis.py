#!/usr/bin/env python3
"""
EOD trading analysis — called by the Trading EOD Summary cron.
Returns a structured analysis to be included in the Telegram summary.
"""
import sqlite3, json, os, logging
from datetime import datetime, timezone, timedelta

from db_connection import get_db
from db_pool import get_workspaces

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FLIGHT_DB = os.path.join(_SCRIPT_DIR, "flight_recorder.db")

def run():
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')

    try:
      with get_db(FLIGHT_DB) as fc:

        # ── Session P&L and trade outcomes ──────────────────────────────
        trades = fc.execute("""
            SELECT note, data FROM flight_log
            WHERE stage='trade_close' AND timestamp >= datetime('now','-24 hours')
        """).fetchall()

        wins = 0
        losses = 0
        total_pips = 0.0
        total_usd = 0.0
        best_trade = None
        worst_trade = None
        for t in trades:
            try:
                d=json.loads(t['data'] or '{}')
                pips=float(d.get('pnl_pips',0) or 0)
                usd=float(d.get('pnl_usd',0) or 0)
                total_pips+=pips; total_usd+=usd
                if d.get('outcome')=='win': wins+=1
                else: losses+=1
                if best_trade is None or pips > float(json.loads(best_trade['data'] or '{}').get('pnl_pips',0) or 0):
                    best_trade=t
                if worst_trade is None or pips < float(json.loads(worst_trade['data'] or '{}').get('pnl_pips',0) or 0):
                    worst_trade=t
            except Exception as e:
                logging.warning("[EOD] Failed to parse trade data: %s", e)

        # ── Pipeline funnel ───────────────────────────────────────────────
        alerts = fc.execute("SELECT COUNT(*) FROM flight_log WHERE stage='scout_alert' AND timestamp >= datetime('now','-24 hours')").fetchone()[0]
        cycles = fc.execute("SELECT COUNT(*) FROM flight_log WHERE stage='cycle_start' AND timestamp >= datetime('now','-24 hours')").fetchone()[0]
        confirms = fc.execute("SELECT COUNT(*) FROM flight_log WHERE stage='validator_verdict' AND (note LIKE '%CONFIRM%' OR note LIKE '%TRADE_NOW%') AND timestamp >= datetime('now','-24 hours')").fetchone()[0]
        watches = fc.execute("SELECT COUNT(*) FROM flight_log WHERE stage='validator_verdict' AND note LIKE '%WATCH%' AND timestamp >= datetime('now','-24 hours')").fetchone()[0]
        rejects = fc.execute("SELECT COUNT(*) FROM flight_log WHERE stage='validator_verdict' AND (note LIKE '%REJECT%' OR note LIKE '%SKIP%') AND timestamp >= datetime('now','-24 hours')").fetchone()[0]
        exec_fails = fc.execute("SELECT COUNT(*) FROM flight_log WHERE stage='execution' AND (note LIKE '%execution_failed%' OR note LIKE '%No trade_id%') AND timestamp >= datetime('now','-24 hours')").fetchone()[0]

        # ── Guardian actions ──────────────────────────────────────────────
        guardian_actions = fc.execute("""
            SELECT json_extract(data,'$.action') as rule, COUNT(*) as cnt
            FROM flight_log WHERE stage='guardian_action' AND timestamp >= datetime('now','-24 hours')
            GROUP BY rule ORDER BY cnt DESC LIMIT 5
        """).fetchall()

        # ── Cascade phase transitions today ───────────────────────────────
        try:
            phases = fc.execute("""
                SELECT phase, COUNT(*) as cnt, ROUND(AVG(pnl_pips),1) as avg_p
                FROM trade_phases WHERE timestamp >= datetime('now','-24 hours')
                GROUP BY phase
            """).fetchall()
        except Exception as e:
            logging.warning("[EOD] Failed to query trade_phases: %s", e)
            phases=[]

        # ── What to tune — compare thresholds to outcomes ─────────────────
        tune_notes = []
        if len(trades) > 0:
            wr = wins / len(trades)
            if wr < 0.40:
                tune_notes.append(f"WR={wr:.0%} — below target. Check: direction gate too loose? SL too tight?")
            if exec_fails > 0:
                tune_notes.append(f"{exec_fails} execution failure(s) — order didn't place after CONFIRM")
            if confirms > 0 and len(trades) < confirms * 0.5:
                tune_notes.append(f"Only {len(trades)} trades from {confirms} confirms — snipe conditions may be too strict")
        if not phases:
            tune_notes.append("No cascade phase data yet — need live trade with retrace to populate")
        
        # ── Snipe leaderboard changes ──────────────────────────────────────
        try:
            bc = get_workspaces()
            bc.row_factory = sqlite3.Row
            lb = bc.execute("""
                SELECT instrument, win_rate, avg_pips, times_triggered
                FROM snipe_leaderboard WHERE times_triggered >= 2
                ORDER BY win_rate DESC LIMIT 5
            """).fetchall()
        except Exception as e:
            logging.warning("[EOD] Failed to query snipe_leaderboard: %s", e)
            lb=[]

        # ── Format the analysis block ─────────────────────────────────────
        total = wins + losses
        wr_str = f"{wins/(total)*100:.0f}%" if total else "—"
        pl_str = f"{'+'if total_usd>=0 else ''}${total_usd:.2f}"
        pip_str = f"{'+'if total_pips>=0 else ''}{total_pips:.1f}p"

        lines = [
            f"\n📊 *Trading Analysis — {today}*",
            f"",
            f"*Session:* {total} trades | {wins}W/{losses}L | WR {wr_str} | {pl_str} ({pip_str})",
        ]

        if best_trade:
            try:
                bd=json.loads(best_trade['data'] or '{}')
                lines.append(f"Best: {best_trade['note'][:40]}")
            except Exception as e:
                logging.warning("[EOD] Failed to parse best_trade data: %s", e)
        if worst_trade and worst_trade != best_trade:
            try:
                wd=json.loads(worst_trade['data'] or '{}')
                lines.append(f"Worst: {worst_trade['note'][:40]}")
            except Exception as e:
                logging.warning("[EOD] Failed to parse worst_trade data: %s", e)

        lines += [
            f"",
            f"*Pipeline:* {alerts} alerts → {cycles} cycles → {confirms+watches} decisions → {total} trades",
            f"  ✅ Confirms: {confirms} | 👁 Watches: {watches} | ❌ Rejects: {rejects}",
        ]
        if exec_fails:
            lines.append(f"  ⚠️ Execution failures: {exec_fails} (order didn't place after CONFIRM)")

        if phases:
            lines.append(f"\n*Cascade phases today:*")
            for p in phases:
                lines.append(f"  {p['phase']}: {p['cnt']}× avg {p['avg_p']:+.1f}p")

        if guardian_actions:
            lines.append(f"\n*Guardian rules fired:*")
            for g in guardian_actions:
                if g['rule']:
                    lines.append(f"  {g['rule']}: {g['cnt']}×")

        if lb:
            lines.append(f"\n*Top snipe patterns:*")
            for l in lb:
                lines.append(f"  {l['instrument'].replace('_','/')}: {l['win_rate']:.0f}%WR avg {l['avg_pips']:+.1f}p ({l['times_triggered']}t)")

        if tune_notes:
            lines.append(f"\n*What to tune tomorrow:*")
            for n in tune_notes:
                lines.append(f"  • {n}")
        else:
            lines.append(f"\n✓ No tuning flags — system performing as expected")

        print('\n'.join(lines))

    except Exception as e:
        print(f"EOD analysis error: {e}")

if __name__ == '__main__':
    run()


def write_session_metrics():
    """Write today's session metrics to flight_recorder for cross-session comparison."""
    import json
    from datetime import datetime, timezone

    _FLIGHT_DB = os.path.join(_SCRIPT_DIR, "flight_recorder.db")
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    try:
      with get_db(_FLIGHT_DB) as fc:

        # Core metrics
        day = fc.execute("""
            SELECT
              COUNT(CASE WHEN stage='trade_close' THEN 1 END) as trades,
              SUM(CASE WHEN stage='trade_close' AND note LIKE '%win%' THEN 1 ELSE 0 END) as wins,
              ROUND(SUM(CASE WHEN stage='trade_close' THEN CAST(json_extract(data,'$.pnl_usd') AS REAL) ELSE 0 END),2) as total_usd,
              ROUND(AVG(CASE WHEN stage='trade_close' THEN CAST(json_extract(data,'$.pnl_pips') AS REAL) END),1) as avg_pips,
              COUNT(CASE WHEN stage='scout_alert' THEN 1 END) as scout_alerts,
              COUNT(CASE WHEN stage='cycle_start' THEN 1 END) as cycles
            FROM flight_log WHERE timestamp >= datetime('now','-24 hours')
        """).fetchone()

        trades = day[0] or 0
        wins   = day[1] or 0

        exec_fails = fc.execute("""
            SELECT COUNT(*) FROM flight_log
            WHERE stage='execution' AND note LIKE '%execution_failed%'
              AND timestamp >= datetime('now','-24 hours')
        """).fetchone()[0] or 0

        try:
            p3 = fc.execute("SELECT COUNT(*) FROM trade_phases WHERE phase='retracing' AND timestamp >= datetime('now','-24 hours')").fetchone()[0] or 0
            p5 = fc.execute("SELECT COUNT(*) FROM trade_phases WHERE phase='exhaustion' AND timestamp >= datetime('now','-24 hours')").fetchone()[0] or 0
            # Phase 3 survival = how many retracing transitions were followed by 'continuing'
            p3_survived = fc.execute("""
                SELECT COUNT(*) FROM trade_phases t1
                WHERE t1.phase='retracing' AND t1.timestamp >= datetime('now','-24 hours')
                  AND EXISTS (SELECT 1 FROM trade_phases t2 WHERE t2.trade_id=t1.trade_id AND t2.phase='continuing')
            """).fetchone()[0] or 0
            p3_survival_rate = round(p3_survived / p3 * 100, 1) if p3 else 0
        except: p3=p5=p3_survival_rate=0

        fc.execute("""
            INSERT OR REPLACE INTO session_metrics
            (session_date, trades, wins, losses, win_rate, total_usd, avg_pips,
             scout_alerts, cycles_run, exec_failures, phase3_count, phase5_count, phase3_survival_rate)
            VALUES (?,?,?,?,?,?,?, ?,?,?,?,?,?)
        """, (
            today, trades, wins, trades-wins,
            round(wins/trades*100,1) if trades else 0,
            day[2] or 0, day[3] or 0,
            day[4] or 0, day[5] or 0, exec_fails,
            p3, p5, p3_survival_rate
        ))
        print(f"Session metrics written for {today}: {trades}t {wins}W {trades-wins}L")
    except Exception as e:
        print(f"session_metrics write failed: {e}")


if __name__ == '__main__':
    run()
    write_session_metrics()
