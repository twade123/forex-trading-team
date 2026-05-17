#!/usr/bin/env python3
"""
Scout Learning System - Tracks scout finding outcomes and improves performance

Creates feedback loop where:
1. Scout findings are recorded with entry context
2. When trades close, outcomes are backfilled
3. Performance analytics feed back to improve scout weighting/filtering
4. Scout gets smarter over time by learning what actually works

Tables created:
- scout_findings: All scout alerts with outcome tracking
- scout_performance_metrics: Aggregated performance by setup/pair/session
"""

import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

_JARVIS_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = str(_JARVIS_ROOT / "Database" / "v2" / "trading_forex.db")

def ensure_scout_learning_tables():
    """Create scout learning tables if they don't exist."""
    from db_connection import get_db
    with get_db() as conn:
        # Main findings table - tracks ALL scout alerts with outcome resolution.
        # Collective by design: all users share this knowledge pool (data flywheel).
        # user_id is PROVENANCE ONLY — do NOT add read-side filtering on this column.
        # Decision locked: Tim 2026-05-09.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scout_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                pair TEXT NOT NULL,
                setup_type TEXT NOT NULL,
                setup_name TEXT,
                direction TEXT NOT NULL,
                entry_price REAL,
                scout_confidence REAL,
                session_quality REAL,
                market_conditions TEXT,
                sniper_score REAL,
                historical_win_rate REAL,
                reasoning TEXT,
                alert_type TEXT,
                snipe_created BOOLEAN DEFAULT FALSE,
                snipe_id INTEGER,
                snipe_triggered BOOLEAN DEFAULT FALSE,
                snipe_trigger_time TEXT,
                trade_executed BOOLEAN DEFAULT FALSE,
                trade_id TEXT,
                trade_entry_price REAL,
                trade_direction TEXT,
                outcome TEXT,
                pips_result REAL,
                hold_time_hours REAL,
                exit_reason TEXT,
                resolution_timestamp TEXT,
                session_hour INTEGER,
                market_session TEXT,
                user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Performance aggregation table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scout_performance_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric_period TEXT NOT NULL,
                period_start TEXT NOT NULL,
                pair TEXT,
                setup_type TEXT, 
                market_session TEXT,
                alert_type TEXT,
                total_findings INTEGER DEFAULT 0,
                snipes_created INTEGER DEFAULT 0,
                snipes_triggered INTEGER DEFAULT 0,
                trades_executed INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                breakevens INTEGER DEFAULT 0,
                total_pips REAL DEFAULT 0,
                snipe_creation_rate REAL,
                trigger_rate REAL,
                execution_rate REAL,
                win_rate REAL,
                avg_pips REAL,
                roi_score REAL,
                precision_score REAL,
                recall_score REAL,
                f1_score REAL,
                last_updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(metric_period, period_start, pair, setup_type, market_session, alert_type)
            )
        """)
        
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scout_findings_outcome ON scout_findings(outcome, pair, setup_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scout_findings_session ON scout_findings(market_session, session_hour)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scout_findings_timestamp ON scout_findings(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scout_performance_period ON scout_performance_metrics(metric_period, period_start)")
        # Composite index for time-range + outcome queries (most common access pattern)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scout_findings_ts_outcome ON scout_findings(timestamp DESC, outcome)")
    logger.info("Scout learning tables created successfully")

def record_scout_finding(alert: Dict[str, Any], session_quality: float = None,
                         user_id: int = None) -> int:
    """Record a scout finding for outcome tracking.

    scout_findings is collective by design — the table is a shared knowledge pool
    (data flywheel) across all PMA users.  user_id is PROVENANCE ONLY; no
    read-side filtering should ever be added.  Decision locked: Tim 2026-05-09.

    Args:
        alert: Scout alert dictionary
        session_quality: Current session quality score (0-1)
        user_id: ID of the user whose scout session produced this alert.
                 Stored for provenance; does not affect read access.

    Returns:
        finding_id: ID of the created finding record
    """
    ensure_scout_learning_tables()
    
    # Extract session info
    now = datetime.now()
    session_hour = now.hour
    
    # Determine market session
    market_session = _determine_market_session(now)
    
    # Parse market conditions from alert
    market_conditions = {
        'rsi': alert.get('current_rsi'),
        'stoch_k': alert.get('current_stoch_k'),
        'bb_position': alert.get('bb_position'),
        'candle_pattern': alert.get('candle_pattern'),
        'h4_bias': alert.get('h4_bias'),
        'fan_state': alert.get('fan_state'),
        'confluence_score': alert.get('confluence_score'),
    }
    
    from db_connection import get_db
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO scout_findings (
                timestamp, pair, setup_type, setup_name, direction,
                scout_confidence, session_quality, market_conditions,
                sniper_score, historical_win_rate, reasoning, alert_type,
                session_hour, market_session, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            alert.get('timestamp'),
            alert.get('pair'),
            alert.get('setup_name', alert.get('reason', 'unknown')),
            alert.get('setup_name'),
            alert.get('direction') or 'neutral',
            alert.get('score', 0) / 100.0,  # Convert to 0-1 scale
            session_quality,
            json.dumps(market_conditions),
            alert.get('score', 0),
            alert.get('historical_win_rate', 0),
            alert.get('reasoning', ''),
            alert.get('type', 'scout_alert'),
            session_hour,
            market_session,
            user_id,  # provenance only — collective table, no read filtering
        ))
        finding_id = cursor.lastrowid

    logger.info(f"Recorded scout finding #{finding_id} for {alert.get('pair')} {alert.get('setup_name')}")
    return finding_id

def link_finding_to_snipe(finding_id: int, snipe_id: int):
    """Link a scout finding to its created snipe."""
    from db_connection import get_db
    with get_db() as conn:
        conn.execute("""
            UPDATE scout_findings 
            SET snipe_created=TRUE, snipe_id=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (snipe_id, finding_id))
    
def record_snipe_trigger(finding_id: int = None, snipe_id: int = None):
    """Record that a snipe was triggered."""
    if not finding_id and not snipe_id:
        return
        
    from db_connection import get_db
    with get_db() as conn:
        if finding_id:
            conn.execute("""
                UPDATE scout_findings 
                SET snipe_triggered=TRUE, snipe_trigger_time=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (datetime.now().isoformat(), finding_id))
        elif snipe_id:
            conn.execute("""
                UPDATE scout_findings 
                SET snipe_triggered=TRUE, snipe_trigger_time=?, updated_at=CURRENT_TIMESTAMP
                WHERE snipe_id=?
            """, (datetime.now().isoformat(), snipe_id))

def record_trade_execution(trade_id: str, entry_price: float, direction: str, 
                          finding_id: int = None, snipe_id: int = None):
    """Record that a trade was executed from a finding/snipe."""
    if not finding_id and not snipe_id:
        return
        
    from db_connection import get_db
    with get_db() as conn:
        where_clause = "id=?" if finding_id else "snipe_id=?"
        where_value = finding_id if finding_id else snipe_id
        
        conn.execute(f"""
            UPDATE scout_findings 
            SET trade_executed=TRUE, trade_id=?, trade_entry_price=?, 
                trade_direction=?, updated_at=CURRENT_TIMESTAMP
            WHERE {where_clause}
        """, (trade_id, entry_price, direction, where_value))

def record_trade_outcome(trade_id: str, outcome: str, pips_result: float, 
                        exit_reason: str = None, hold_time_hours: float = None):
    """Record the final outcome of a trade that originated from scout finding."""
    from db_connection import get_db
    with get_db() as conn:
        conn.execute("""
            UPDATE scout_findings 
            SET outcome=?, pips_result=?, hold_time_hours=?, exit_reason=?,
                resolution_timestamp=?, updated_at=CURRENT_TIMESTAMP
            WHERE trade_id=?
        """, (
            outcome, pips_result, hold_time_hours, exit_reason,
            datetime.now().isoformat(), trade_id
        ))
        
        if conn.total_changes:
            logger.info(f"Recorded trade outcome for {trade_id}: {outcome} ({pips_result:.1f} pips)")
            _update_performance_metrics()

            # ── Learning Integration: check for scout drift ──
            try:
                # Look up the pair and setup_type for this trade
                conn.row_factory = lambda cursor, row: {
                    col[0]: row[idx] for idx, col in enumerate(cursor.description)
                }
                row = conn.execute(
                    "SELECT pair, setup_type FROM scout_findings WHERE trade_id=?",
                    (trade_id,)
                ).fetchone()
                if row:
                    from learning_integrator import LearningIntegrator
                    integrator = LearningIntegrator()
                    integrator.check_scout_drift(
                        pair=row["pair"],
                        setup_type=row["setup_type"],
                        outcome=outcome,
                        pips_result=pips_result,
                    )
            except Exception as e:
                logger.warning("Scout drift check failed (non-fatal): %s", e)

def get_scout_performance_summary(days_back: int = 30) -> Dict[str, Any]:
    """Get scout performance summary for dashboard display."""
    ensure_scout_learning_tables()
    
    cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
    
    from db_connection import get_db
    with get_db() as conn:
        overall = conn.execute("""
            SELECT 
                COUNT(*) as total_findings,
                SUM(CASE WHEN snipe_created THEN 1 ELSE 0 END) as snipes_created,
                SUM(CASE WHEN snipe_triggered THEN 1 ELSE 0 END) as snipes_triggered,
                SUM(CASE WHEN trade_executed THEN 1 ELSE 0 END) as trades_executed,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                AVG(CASE WHEN outcome IN ('win','loss') THEN pips_result END) as avg_pips,
                SUM(CASE WHEN outcome IN ('win','loss') THEN pips_result ELSE 0 END) as total_pips
            FROM scout_findings WHERE timestamp > ?
        """, (cutoff,)).fetchone()
        
        by_setup = conn.execute("""
            SELECT setup_type, COUNT(*) as findings,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                AVG(CASE WHEN outcome IN ('win','loss') THEN pips_result END) as avg_pips
            FROM scout_findings WHERE timestamp > ? AND outcome IN ('win','loss')
            GROUP BY setup_type ORDER BY avg_pips DESC
        """, (cutoff,)).fetchall()
        
        by_session = conn.execute("""
            SELECT market_session, COUNT(*) as findings,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                AVG(CASE WHEN outcome IN ('win','loss') THEN pips_result END) as avg_pips
            FROM scout_findings WHERE timestamp > ? AND outcome IN ('win','loss')
            GROUP BY market_session ORDER BY avg_pips DESC
        """, (cutoff,)).fetchall()
    
    # Calculate rates
    total_findings = overall['total_findings'] or 1
    trades = overall['trades_executed'] or 0
    
    return {
        'period_days': days_back,
        'total_findings': overall['total_findings'],
        'conversion_rates': {
            'finding_to_snipe': (overall['snipes_created'] or 0) / total_findings * 100,
            'snipe_to_trigger': (overall['snipes_triggered'] or 0) / max(overall['snipes_created'], 1) * 100,
            'trigger_to_trade': trades / max(overall['snipes_triggered'], 1) * 100,
        },
        'trade_performance': {
            'total_trades': trades,
            'wins': overall['wins'] or 0,
            'losses': overall['losses'] or 0,
            'win_rate': (overall['wins'] or 0) / max(trades, 1) * 100,
            'avg_pips': overall['avg_pips'] or 0,
            'total_pips': overall['total_pips'] or 0,
        },
        'best_setups': [dict(row) for row in by_setup],
        'best_sessions': [dict(row) for row in by_session],
    }

def get_scout_learning_recommendations() -> List[Dict[str, Any]]:
    """Get recommendations for improving scout performance based on learning data."""
    ensure_scout_learning_tables()
    
    recommendations = []
    
    from db_connection import get_db
    with get_db() as conn:
        low_trigger = conn.execute("""
            SELECT setup_type, COUNT(*) as snipes_created,
                   SUM(CASE WHEN snipe_triggered THEN 1 ELSE 0 END) as triggered
            FROM scout_findings WHERE snipe_created=TRUE
            GROUP BY setup_type
            HAVING triggered * 1.0 / snipes_created < 0.3
            ORDER BY snipes_created DESC
        """).fetchall()
        
        for row in low_trigger:
            recommendations.append({
                'type': 'low_trigger_rate',
                'setup': row['setup_type'],
                'issue': f"Creates {row['snipes_created']} snipes but only {row['triggered']} trigger",
                'suggestion': 'Review snipe conditions - may be too restrictive',
                'priority': 'high'
            })
        
        poor_performers = conn.execute("""
            SELECT setup_type, 
                   SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                   COUNT(*) as total_trades
            FROM scout_findings WHERE outcome IN ('win','loss')
            GROUP BY setup_type
            HAVING total_trades >= 5 AND wins * 1.0 / (wins + losses) < 0.6
            ORDER BY wins * 1.0 / (wins + losses) ASC
        """).fetchall()
        
        for row in poor_performers:
            win_rate = row['wins'] / (row['wins'] + row['losses']) * 100
            recommendations.append({
                'type': 'poor_performance',
                'setup': row['setup_type'],
                'issue': f"Win rate {win_rate:.1f}% over {row['total_trades']} trades",
                'suggestion': 'Consider adjusting filters or removing this setup',
                'priority': 'medium'
            })
        
        hourly_performance = conn.execute("""
            SELECT session_hour,
                   AVG(CASE WHEN outcome IN ('win','loss') THEN pips_result END) as avg_pips,
                   COUNT(*) as findings
            FROM scout_findings WHERE outcome IN ('win','loss')
            GROUP BY session_hour HAVING findings >= 3
            ORDER BY avg_pips DESC
        """).fetchall()
        
        if len(hourly_performance) > 5:
            best_hours = [row['session_hour'] for row in hourly_performance[:3]]
            worst_hours = [row['session_hour'] for row in hourly_performance[-3:]]
            
            recommendations.append({
                'type': 'time_optimization',
                'setup': 'all',
                'issue': f"Performance varies significantly by hour",
                'suggestion': f'Focus scanning on hours {best_hours}, reduce activity during {worst_hours}',
                'priority': 'low',
                'data': {'best_hours': best_hours, 'worst_hours': worst_hours}
            })
    
    return recommendations

def _determine_market_session(dt: datetime) -> str:
    """Determine which market session is most active at given time."""
    hour = dt.hour
    
    # London: 3AM-12PM ET
    if 3 <= hour <= 12:
        # London-NY overlap: 8AM-12PM ET  
        if 8 <= hour <= 12:
            return 'London-NY'
        return 'London'
    # NY: 8AM-5PM ET
    elif 8 <= hour <= 17:
        return 'NY'
    # Tokyo: 7PM-4AM ET (next day)
    elif hour >= 19 or hour <= 4:
        # Sydney-Tokyo overlap: 7PM-2AM ET
        if hour >= 19 or hour <= 2:
            return 'Tokyo-Sydney'
        return 'Tokyo'
    # Sydney: 5PM-2AM ET (next day)
    elif 17 <= hour <= 23 or 0 <= hour <= 2:
        return 'Sydney'
    else:
        return 'dead_zone'

def aggregate_performance():
    """Update aggregated performance metrics from scout_findings data.
    
    Aggregates scout_findings with resolved outcomes into scout_performance_metrics
    for daily, weekly, and monthly periods by setup/pair/session dimensions.
    """
    from db_connection import get_db
    
    try:
        with get_db() as conn:
            # Get current time for period calculations
            now = datetime.now()
            
            # Define periods to aggregate
            periods = {
                'daily': now.date().isoformat(),
                'weekly': (now - timedelta(days=now.weekday())).date().isoformat(),  # Start of week
                'monthly': now.replace(day=1).date().isoformat(),  # Start of month
            }
            
            for period_type, period_start in periods.items():
                # Clear existing metrics for this period to recalculate
                conn.execute("""
                    DELETE FROM scout_performance_metrics 
                    WHERE metric_period = ? AND period_start = ?
                """, (period_type, period_start))
                
                # Calculate period boundaries
                if period_type == 'daily':
                    start_time = f"{period_start}T00:00:00"
                    end_time = f"{period_start}T23:59:59"
                elif period_type == 'weekly':
                    week_end = (datetime.fromisoformat(period_start) + timedelta(days=6)).date()
                    start_time = f"{period_start}T00:00:00"
                    end_time = f"{week_end}T23:59:59"
                else:  # monthly
                    month_start = datetime.fromisoformat(period_start)
                    if month_start.month == 12:
                        month_end = month_start.replace(year=month_start.year + 1, month=1) - timedelta(days=1)
                    else:
                        month_end = month_start.replace(month=month_start.month + 1) - timedelta(days=1)
                    start_time = f"{period_start}T00:00:00"
                    end_time = f"{month_end.date()}T23:59:59"
                
                # Aggregate scout_findings for this period
                aggregation_query = """
                    INSERT INTO scout_performance_metrics (
                        metric_period, period_start, pair, setup_type, market_session, alert_type,
                        total_findings, snipes_created, snipes_triggered, trades_executed,
                        wins, losses, breakevens, total_pips, win_rate,
                        snipe_creation_rate, trigger_rate, execution_rate, avg_pips, last_updated
                    )
                    SELECT 
                        ? as metric_period,
                        ? as period_start,
                        pair,
                        setup_type,
                        market_session,
                        alert_type,
                        COUNT(*) as total_findings,
                        SUM(CASE WHEN snipe_created = 1 THEN 1 ELSE 0 END) as snipes_created,
                        SUM(CASE WHEN snipe_triggered = 1 THEN 1 ELSE 0 END) as snipes_triggered,
                        SUM(CASE WHEN trade_executed = 1 THEN 1 ELSE 0 END) as trades_executed,
                        SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                        SUM(CASE WHEN outcome = 'breakeven' THEN 1 ELSE 0 END) as breakevens,
                        SUM(COALESCE(pips_result, 0)) as total_pips,
                        CASE WHEN COUNT(CASE WHEN outcome IN ('win', 'loss') THEN 1 END) > 0 
                             THEN CAST(SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS REAL) / 
                                  COUNT(CASE WHEN outcome IN ('win', 'loss') THEN 1 END)
                             ELSE 0 END as win_rate,
                        CASE WHEN COUNT(*) > 0 THEN CAST(SUM(CASE WHEN snipe_created=1 THEN 1 ELSE 0 END) AS REAL) / COUNT(*) ELSE 0 END as snipe_creation_rate,
                        CASE WHEN SUM(CASE WHEN snipe_created=1 THEN 1 ELSE 0 END) > 0 THEN CAST(SUM(CASE WHEN snipe_triggered=1 THEN 1 ELSE 0 END) AS REAL) / SUM(CASE WHEN snipe_created=1 THEN 1 ELSE 0 END) ELSE 0 END as trigger_rate,
                        CASE WHEN COUNT(*) > 0 THEN CAST(SUM(CASE WHEN trade_executed=1 THEN 1 ELSE 0 END) AS REAL) / COUNT(*) ELSE 0 END as execution_rate,
                        AVG(COALESCE(pips_result, 0)) as avg_pips,
                        CURRENT_TIMESTAMP as last_updated
                    FROM scout_findings
                    WHERE timestamp >= ? AND timestamp <= ?
                      AND outcome IS NOT NULL
                      AND user_id = ?
                    GROUP BY pair, setup_type, market_session, alert_type
                    HAVING COUNT(*) >= 1  -- At least 1 finding to create a metric
                """
                
                conn.execute(aggregation_query, (
                    period_type, period_start, start_time, end_time, 2
                ))
            
            # Log aggregation completion
            total_metrics = conn.execute("SELECT COUNT(*) FROM scout_performance_metrics").fetchone()[0]
            logger.info("✅ Scout performance aggregated: %d total metrics across all periods", total_metrics)
            
    except Exception as e:
        logger.error("Scout performance aggregation failed: %s", e)
        raise

def _update_performance_metrics():
    """Wrapper for backward compatibility - calls aggregate_performance."""
    aggregate_performance()
    # Also update scout_performance_analytics for detailed analytics
    update_scout_performance_analytics()


def update_scout_performance_analytics():
    """Populate scout_performance_analytics from scout_findings data.

    Aggregates scout findings by pair, setup_type, fan_state, and session_quality
    to provide detailed analytics for dashboard display.
    """
    from db_connection import get_db

    try:
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
            if rows_updated > 0:
                logger.info(f"Updated scout_performance_analytics: {rows_updated} aggregations")
    except Exception as e:
        logger.warning(f"Failed to update scout_performance_analytics: {e}")

# Integration hooks for existing systems
def integrate_with_scout():
    """Show how to integrate with existing trade scout."""
    example_code = """
    # In trade_scout.py _scan_pair method, after creating alert:
    from scout_learning_system import record_scout_finding
    
    for alert in alerts:
        # Record finding for learning
        finding_id = record_scout_finding(alert, get_session_quality(pair))
        alert['finding_id'] = finding_id  # Store for later linking
        
        self._store_alert(alert)
        await self._broadcast_alert(alert)
    """
    print(example_code)

def integrate_with_watch_manager():
    """Show how to integrate with watch manager."""
    example_code = """
    # In watch_manager.py create_watch_from_scout:
    from scout_learning_system import link_finding_to_snipe
    
    watch_id = cursor.lastrowid
    finding_id = alert.get('finding_id')
    if finding_id:
        link_finding_to_snipe(finding_id, watch_id)
    
    # In check_active_watches when snipe triggers:
    from scout_learning_system import record_snipe_trigger
    
    if result["met"]:
        # Mark as triggered in database
        conn.execute("UPDATE watch_suggestions SET status='triggered'...")
        
        # Record in learning system
        record_snipe_trigger(snipe_id=watch_id)
        
        # *** CRITICAL FIX: LAUNCH TRADING CYCLE ***
        from Source.agents.trading_cycle import TradingCycle
        cycle = TradingCycle()
        result = cycle.run_cycle(instrument)
        
        # Record trade execution if cycle executed
        if result.get('trade_executed'):
            record_trade_execution(
                result['trade_id'], 
                result['entry_price'],
                result['direction'],
                snipe_id=watch_id
            )
    """
    print(example_code)

if __name__ == "__main__":
    # Initialize the learning system
    ensure_scout_learning_tables()
    
    # Show performance summary
    summary = get_scout_performance_summary()
    print("Scout Performance Summary:")
    print(json.dumps(summary, indent=2))
    
    # Show recommendations
    recommendations = get_scout_learning_recommendations()
    if recommendations:
        print("\nImprovement Recommendations:")
        for rec in recommendations:
            print(f"- {rec['type']}: {rec['suggestion']}")