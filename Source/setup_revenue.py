"""
Setup Revenue Tracker — tracks lifetime P&L per setup+pair from live trades.

Stores every closed trade with its setup name, pair, direction, P&L (pips + $),
and aggregates lifetime stats. Setups that perform well get auto-promoted to
the user's permanent snipe list.

Tables:
  setup_trades     — individual trade records (one row per closed trade)
  setup_revenue    — aggregated lifetime stats per setup+pair
  user_snipe_list  — promoted setups that auto-trigger via Scout

Auto-promotion rules:
  - 3+ wins AND win_rate >= 70% AND total_revenue > $0 → promoted
  - Demoted if win_rate drops below 50% over last 10 trades
"""

import logging
import sqlite3
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from db_connection import DB_PATH
from db_pool import get_trading_forex

# Database path — setup_trades, setup_revenue, user_snipe_list live alongside manual_trades
# in trade_log.db (same DB that stores the raw trade data).
# Previously pointed at Database/v2/trading_forex.db which was always empty.
_V2_DB_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "Data", "trade_log.db"
))

logger = logging.getLogger("trading_bot.setup_revenue")

# Auto-promotion thresholds
MIN_WINS_TO_PROMOTE = 3
MIN_WIN_RATE_TO_PROMOTE = 0.70
MIN_WINS_TO_DEMOTE_CHECK = 10
MIN_WIN_RATE_TO_KEEP = 0.50


class SetupRevenueTracker:
    """Track and aggregate live trade P&L per setup+pair."""

    def __init__(self, db_path: str = None):
        self._db_path = db_path or _V2_DB_PATH
        self._ensure_tables()

    def _conn(self) -> sqlite3.Connection:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        return conn  # Pooled connection — do NOT close()

    def _ensure_tables(self):
        conn = self._conn()
        conn.execute("BEGIN")
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS setup_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL,
                    setup_name TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL,
                    exit_price REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    units REAL,
                    pnl_pips REAL NOT NULL,
                    pnl_usd REAL NOT NULL,
                    r_multiple REAL,
                    outcome TEXT NOT NULL,  -- 'win' or 'loss'
                    duration_minutes INTEGER,
                    source TEXT,  -- 'scout', 'snipe', 'manual'
                    scout_confidence REAL,
                    threat_zone_at_close TEXT,
                    close_reason TEXT,
                    opened_at TEXT,
                    closed_at TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    watch_id INTEGER,
                    UNIQUE(trade_id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS setup_revenue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    setup_name TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_pips REAL DEFAULT 0,
                    total_usd REAL DEFAULT 0,
                    best_trade_usd REAL DEFAULT 0,
                    worst_trade_usd REAL DEFAULT 0,
                    avg_r_multiple REAL DEFAULT 0,
                    avg_duration_minutes REAL DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    streak_current INTEGER DEFAULT 0,  -- positive = wins, negative = losses
                    streak_best INTEGER DEFAULT 0,
                    last_trade_at TEXT,
                    first_trade_at TEXT,
                    promoted INTEGER DEFAULT 0,  -- 1 = on user's snipe list
                    promoted_at TEXT,
                    user_id INTEGER NOT NULL,
                    UNIQUE(setup_name, pair, user_id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_snipe_list (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    setup_name TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    direction TEXT,  -- NULL = both directions
                    source TEXT DEFAULT 'auto_promoted',  -- 'auto_promoted', 'manual'
                    lifetime_pnl_usd REAL DEFAULT 0,
                    lifetime_trades INTEGER DEFAULT 0,
                    lifetime_win_rate REAL DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    promoted_at TEXT,
                    demoted_at TEXT,
                    notes TEXT,
                    user_id INTEGER NOT NULL,
                    UNIQUE(setup_name, pair, user_id)
                )
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_setup_trades_setup ON setup_trades(setup_name, pair)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_setup_trades_closed ON setup_trades(closed_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_setup_revenue_pair ON setup_revenue(pair)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user_snipe_active ON user_snipe_list(is_active, user_id)")

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def record_trade(
        self,
        trade_id: str,
        setup_name: str,
        pair: str,
        direction: str,
        pnl_pips: float,
        pnl_usd: float,
        entry_price: float = 0,
        exit_price: float = 0,
        stop_loss: float = 0,
        take_profit: float = 0,
        units: float = 0,
        r_multiple: float = 0,
        duration_minutes: int = 0,
        source: str = 'scout',
        scout_confidence: float = 0,
        threat_zone_at_close: str = '',
        close_reason: str = '',
        opened_at: str = '',
        user_id: int = None,
        watch_id: int = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Record a closed trade and update aggregated revenue.

        Returns dict with updated revenue stats and promotion status.
        """
        outcome = 'win' if pnl_pips > 0 else 'loss'
        now = datetime.now(timezone.utc).isoformat()

        conn = self._conn()
        conn.execute("BEGIN")
        try:
            # Insert or update trade record (reclassification updates setup_name)
            conn.execute("""
                INSERT OR REPLACE INTO setup_trades (
                    trade_id, setup_name, pair, direction, entry_price, exit_price,
                    stop_loss, take_profit, units, pnl_pips, pnl_usd, r_multiple,
                    outcome, duration_minutes, source, scout_confidence,
                    threat_zone_at_close, close_reason, opened_at, closed_at, user_id, watch_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, setup_name, pair, direction, entry_price, exit_price,
                stop_loss, take_profit, units, pnl_pips, pnl_usd, r_multiple,
                outcome, duration_minutes, source, scout_confidence,
                threat_zone_at_close, close_reason, opened_at, now, user_id, watch_id,
            ))

            # Update aggregated revenue
            existing = conn.execute(
                "SELECT * FROM setup_revenue WHERE setup_name = ? AND pair = ? AND user_id = ?",
                (setup_name, pair, user_id)
            ).fetchone()

            if existing:
                total_trades = existing['total_trades'] + 1
                wins = existing['wins'] + (1 if outcome == 'win' else 0)
                losses = existing['losses'] + (1 if outcome == 'loss' else 0)
                total_pips = existing['total_pips'] + pnl_pips
                total_usd = existing['total_usd'] + pnl_usd
                best = max(existing['best_trade_usd'], pnl_usd)
                worst = min(existing['worst_trade_usd'], pnl_usd)
                win_rate = wins / total_trades if total_trades > 0 else 0

                # Running average R-multiple
                prev_avg_r = existing['avg_r_multiple'] or 0
                avg_r = ((prev_avg_r * existing['total_trades']) + r_multiple) / total_trades

                # Running average duration
                prev_avg_dur = existing['avg_duration_minutes'] or 0
                avg_dur = ((prev_avg_dur * existing['total_trades']) + duration_minutes) / total_trades

                # Streak
                prev_streak = existing['streak_current']
                if outcome == 'win':
                    streak = prev_streak + 1 if prev_streak > 0 else 1
                else:
                    streak = prev_streak - 1 if prev_streak < 0 else -1
                best_streak = max(existing['streak_best'], streak)

                conn.execute("""
                    UPDATE setup_revenue SET
                        total_trades = ?, wins = ?, losses = ?, total_pips = ?,
                        total_usd = ?, best_trade_usd = ?, worst_trade_usd = ?,
                        avg_r_multiple = ?, avg_duration_minutes = ?, win_rate = ?,
                        streak_current = ?, streak_best = ?, last_trade_at = ?
                    WHERE setup_name = ? AND pair = ? AND user_id = ?
                """, (
                    total_trades, wins, losses, total_pips,
                    total_usd, best, worst, avg_r, avg_dur, win_rate,
                    streak, best_streak, now,
                    setup_name, pair, user_id,
                ))
            else:
                wins = 1 if outcome == 'win' else 0
                losses = 1 - wins
                win_rate = float(wins)
                conn.execute("""
                    INSERT INTO setup_revenue (
                        setup_name, pair, total_trades, wins, losses, total_pips,
                        total_usd, best_trade_usd, worst_trade_usd, avg_r_multiple,
                        avg_duration_minutes, win_rate, streak_current, streak_best,
                        last_trade_at, first_trade_at, user_id
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    setup_name, pair, wins, losses, pnl_pips,
                    pnl_usd, max(0, pnl_usd), min(0, pnl_usd), r_multiple,
                    duration_minutes, win_rate,
                    1 if outcome == 'win' else -1,
                    1 if outcome == 'win' else 0,
                    now, now, user_id,
                ))
                total_trades = 1
                total_usd = pnl_usd

            conn.execute("COMMIT")

            # Check auto-promotion
            promotion_result = self._check_promotion(conn, setup_name, pair, user_id)

            # Create a story-aware snipe from winning trades
            if outcome == 'win':
                try:
                    try:
                        from agents.watch_manager import create_watch_from_win
                    except ImportError:
                        from Source.agents.watch_manager import create_watch_from_win
                    win_snipe_data = {
                        'pair': pair,
                        'direction': direction,
                        'setup_name': setup_name,
                        'entry_type': kwargs.get('entry_type', 'unknown'),
                        'fan_state': kwargs.get('fan_state', 'unknown'),
                        'fan_direction': kwargs.get('fan_direction', 'unknown'),
                        'momentum_state': kwargs.get('momentum_state', 'unknown'),
                        'momentum_significance': kwargs.get('momentum_significance', 'unknown'),
                        'e100_interaction': kwargs.get('e100_interaction', 'none'),
                        'wick_pressure': kwargs.get('wick_pressure', 'unknown'),
                        'body_trend': kwargs.get('body_trend', 'unknown'),
                        'opportunity_score': kwargs.get('opportunity_score', 0),
                        'pnl_pips': pnl_pips,
                        'pnl_usd': pnl_usd,
                        'win_rate': win_rate if existing else 1.0,
                        'trade_count': total_trades if existing else 1,
                        'profit_factor': kwargs.get('profit_factor', 0),
                        'scout_confidence': scout_confidence,
                    }
                    create_watch_from_win(win_snipe_data)

                    # Flight recorder: WIN_SNIPE
                    try:
                        from flight_recorder import get_flight_recorder, FlightStage
                        flight = get_flight_recorder()
                        if flight:
                            flight.record(FlightStage.WIN_SNIPE, pair=pair,
                                          trade_id=trade_id, data={
                                "setup": setup_name,
                                "entry_type": kwargs.get('entry_type', 'unknown'),
                                "pnl_pips": pnl_pips,
                                "pnl_usd": pnl_usd,
                                "win_rate": win_rate if existing else 1.0,
                                "total_trades": total_trades if existing else 1,
                                "promoted": promotion_result.get('action', 'none'),
                            }, note=f"Win snipe: {setup_name} +{pnl_pips:.1f} pips")
                    except Exception:
                        pass  # Flight recorder optional
                except Exception as e:
                    logger.warning("Failed to create win snipe for %s %s: %s", pair, setup_name, e)

            return {
                'setup_name': setup_name,
                'pair': pair,
                'outcome': outcome,
                'pnl_usd': pnl_usd,
                'total_trades': total_trades if existing else 1,
                'total_usd': total_usd if existing else pnl_usd,
                'win_rate': win_rate,
                'promoted': promotion_result.get('promoted', False),
                'promotion_action': promotion_result.get('action', 'none'),
            }

        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _check_promotion(self, conn: sqlite3.Connection, setup_name: str, pair: str, user_id: int) -> Dict:
        """Check if setup should be promoted/demoted on user's snipe list.

        Uses the existing connection (which is inside a transaction in record_trade).
        """
        rev = conn.execute(
            "SELECT * FROM setup_revenue WHERE setup_name = ? AND pair = ? AND user_id = ?",
            (setup_name, pair, user_id)
        ).fetchone()

        if not rev:
            return {'action': 'none', 'promoted': False}

        now = datetime.now(timezone.utc).isoformat()
        existing_snipe = conn.execute(
            "SELECT * FROM user_snipe_list WHERE setup_name = ? AND pair = ? AND user_id = ?",
            (setup_name, pair, user_id)
        ).fetchone()

        # Promotion check
        if (rev['wins'] >= MIN_WINS_TO_PROMOTE and
            rev['win_rate'] >= MIN_WIN_RATE_TO_PROMOTE and
            rev['total_usd'] > 0):

            if not existing_snipe:
                conn.execute("""
                    INSERT INTO user_snipe_list (
                        setup_name, pair, source, lifetime_pnl_usd, lifetime_trades,
                        lifetime_win_rate, is_active, promoted_at, user_id
                    ) VALUES (?, ?, 'auto_promoted', ?, ?, ?, 1, ?, ?)
                """, (setup_name, pair, rev['total_usd'], rev['total_trades'],
                      rev['win_rate'], now, user_id))
                conn.execute(
                    "UPDATE setup_revenue SET promoted = 1, promoted_at = ? WHERE setup_name = ? AND pair = ? AND user_id = ?",
                    (now, setup_name, pair, user_id))
                logger.info("AUTO-PROMOTED: %s on %s — %d wins, %.0f%% WR, $%.2f lifetime",
                           setup_name, pair, rev['wins'], rev['win_rate'] * 100, rev['total_usd'])
                return {'action': 'promoted', 'promoted': True}
            elif not existing_snipe['is_active']:
                # Re-activate
                conn.execute("""
                    UPDATE user_snipe_list SET is_active = 1, promoted_at = ?,
                        lifetime_pnl_usd = ?, lifetime_trades = ?, lifetime_win_rate = ?
                    WHERE setup_name = ? AND pair = ? AND user_id = ?
                """, (now, rev['total_usd'], rev['total_trades'], rev['win_rate'],
                      setup_name, pair, user_id))
                return {'action': 're_activated', 'promoted': True}
            else:
                # Already active — update stats
                conn.execute("""
                    UPDATE user_snipe_list SET
                        lifetime_pnl_usd = ?, lifetime_trades = ?, lifetime_win_rate = ?
                    WHERE setup_name = ? AND pair = ? AND user_id = ?
                """, (rev['total_usd'], rev['total_trades'], rev['win_rate'],
                      setup_name, pair, user_id))
                return {'action': 'updated', 'promoted': True}

        # Demotion check
        if (existing_snipe and existing_snipe['is_active'] and
            rev['total_trades'] >= MIN_WINS_TO_DEMOTE_CHECK and
            rev['win_rate'] < MIN_WIN_RATE_TO_KEEP):

            conn.execute("""
                UPDATE user_snipe_list SET is_active = 0, demoted_at = ?
                WHERE setup_name = ? AND pair = ? AND user_id = ?
            """, (now, setup_name, pair, user_id))
            conn.execute(
                "UPDATE setup_revenue SET promoted = 0 WHERE setup_name = ? AND pair = ? AND user_id = ?",
                (setup_name, pair, user_id))
            logger.warning("DEMOTED: %s on %s — %.0f%% WR over %d trades",
                          setup_name, pair, rev['win_rate'] * 100, rev['total_trades'])
            return {'action': 'demoted', 'promoted': False}

        return {'action': 'none', 'promoted': bool(existing_snipe and existing_snipe['is_active'])}

    # ── Query methods for dashboard ──

    def get_revenue_by_pair(self, pair: str, user_id: int = None) -> List[Dict]:
        """Get all setup revenue records for a pair."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM setup_revenue WHERE pair = ? AND user_id = ? ORDER BY total_usd DESC",
            (pair, user_id)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_revenue(self, user_id: int = None) -> List[Dict]:
        """Get all setup revenue records for a user."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM setup_revenue WHERE user_id = ? ORDER BY total_usd DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_snipe_list(self, user_id: int = None, active_only: bool = True) -> List[Dict]:
        """Get user's promoted snipe list."""
        conn = self._conn()
        if active_only:
            rows = conn.execute(
                "SELECT * FROM user_snipe_list WHERE user_id = ? AND is_active = 1 ORDER BY lifetime_pnl_usd DESC",
                (user_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM user_snipe_list WHERE user_id = ? ORDER BY is_active DESC, lifetime_pnl_usd DESC",
                (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_trades(self, pair: str = None, setup_name: str = None,
                          limit: int = 20, user_id: int = None) -> List[Dict]:
        """Get recent trade records, optionally filtered."""
        conn = self._conn()
        query = "SELECT * FROM setup_trades WHERE user_id = ?"
        params: list = [user_id]
        if pair:
            query += " AND pair = ?"
            params.append(pair)
        if setup_name:
            query += " AND setup_name = ?"
            params.append(setup_name)
        query += " ORDER BY closed_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_top_setups(self, min_trades: int = 2, user_id: int = None) -> List[Dict]:
        """Get top performing setups across all pairs."""
        conn = self._conn()
        rows = conn.execute("""
            SELECT * FROM setup_revenue
            WHERE user_id = ? AND total_trades >= ?
            ORDER BY total_usd DESC LIMIT 20
        """, (user_id, min_trades)).fetchall()
        return [dict(r) for r in rows]

    def get_pair_summary(self, user_id: int = None) -> Dict[str, Dict]:
        """Get per-pair P&L summary."""
        conn = self._conn()
        rows = conn.execute("""
            SELECT pair,
                   SUM(total_trades) as trades,
                   SUM(wins) as wins,
                   SUM(total_pips) as pips,
                   SUM(total_usd) as usd
            FROM setup_revenue WHERE user_id = ?
            GROUP BY pair ORDER BY usd DESC
        """, (user_id,)).fetchall()
        return {r['pair']: dict(r) for r in rows}

    def manual_add_snipe(self, setup_name: str, pair: str, direction: str = None,
                         notes: str = '', user_id: int = None) -> bool:
        """Manually add a setup to the snipe list."""
        conn = self._conn()
        conn.execute("BEGIN")
        try:
            conn.execute("""
                INSERT OR REPLACE INTO user_snipe_list (
                    setup_name, pair, direction, source, is_active, promoted_at, notes, user_id
                ) VALUES (?, ?, ?, 'manual', 1, ?, ?, ?)
            """, (setup_name, pair, direction,
                  datetime.now(timezone.utc).isoformat(), notes, user_id))
            conn.execute("COMMIT")
            return True
        except Exception as e:
            conn.execute("ROLLBACK")
            logger.error("Failed to add snipe: %s", e)
            return False
