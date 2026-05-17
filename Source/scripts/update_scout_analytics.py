#!/usr/bin/env python3
"""
Populate scout_performance_analytics from scout_findings.

This aggregates scout findings by pair, setup_type, fan_state, and session_quality
to provide detailed analytics for dashboard display.
"""

import sqlite3
from datetime import datetime
from db_connection import get_db

def update_scout_performance_analytics():
    """Populate scout_performance_analytics from scout_findings data."""

    with get_db() as conn:
        # Aggregate scout_findings into scout_performance_analytics
        conn.execute("""
            INSERT OR REPLACE INTO scout_performance_analytics (
                pair, setup_type, ema_fan_state, session_quality_range,
                total_findings, triggered_findings, successful_findings,
                trigger_rate, success_rate, avg_pips, confidence_accuracy,
                best_conditions, worst_conditions, last_updated
            )
            SELECT
                sf.pair,
                sf.setup_type,
                COALESCE(json_extract(sf.market_conditions, '$.fan_state'), 'unknown') as ema_fan_state,
                CASE
                    WHEN sf.session_quality >= 0.8 THEN 'high'
                    WHEN sf.session_quality >= 0.5 THEN 'medium'
                    ELSE 'low'
                END as session_quality_range,
                COUNT(*) as total_findings,
                SUM(CASE WHEN sf.snipe_triggered = 1 THEN 1 ELSE 0 END) as triggered_findings,
                SUM(CASE WHEN sf.outcome = 'win' THEN 1 ELSE 0 END) as successful_findings,
                CASE
                    WHEN COUNT(*) > 0
                    THEN CAST(SUM(CASE WHEN sf.snipe_triggered = 1 THEN 1 ELSE 0 END) AS REAL) / COUNT(*)
                    ELSE 0
                END as trigger_rate,
                CASE
                    WHEN SUM(CASE WHEN sf.outcome IN ('win', 'loss') THEN 1 ELSE 0 END) > 0
                    THEN CAST(SUM(CASE WHEN sf.outcome = 'win' THEN 1 ELSE 0 END) AS REAL) /
                         SUM(CASE WHEN sf.outcome IN ('win', 'loss') THEN 1 ELSE 0 END)
                    ELSE 0
                END as success_rate,
                AVG(COALESCE(sf.pips_result, 0)) as avg_pips,
                AVG(sf.scout_confidence) as confidence_accuracy,
                (
                    SELECT json_object(
                        'rsi', json_extract(market_conditions, '$.rsi'),
                        'session', market_session,
                        'hour', session_hour
                    )
                    FROM scout_findings sf2
                    WHERE sf2.pair = sf.pair
                    AND sf2.setup_type = sf.setup_type
                    AND sf2.outcome = 'win'
                    ORDER BY sf2.pips_result DESC
                    LIMIT 1
                ) as best_conditions,
                (
                    SELECT json_object(
                        'rsi', json_extract(market_conditions, '$.rsi'),
                        'session', market_session,
                        'hour', session_hour
                    )
                    FROM scout_findings sf2
                    WHERE sf2.pair = sf.pair
                    AND sf2.setup_type = sf.setup_type
                    AND sf2.outcome = 'loss'
                    ORDER BY sf2.pips_result ASC
                    LIMIT 1
                ) as worst_conditions,
                datetime('now') as last_updated
            FROM scout_findings sf
            WHERE sf.outcome IS NOT NULL
            GROUP BY
                sf.pair,
                sf.setup_type,
                ema_fan_state,
                session_quality_range
        """)

        rows_updated = conn.total_changes
        print(f"✅ Updated scout_performance_analytics: {rows_updated} rows")
        return rows_updated

if __name__ == "__main__":
    count = update_scout_performance_analytics()
    print(f"📊 Analytics updated successfully: {count} aggregations created/updated")
