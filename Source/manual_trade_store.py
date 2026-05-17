"""
Manual Trade Store — DEPRECATED: now reads from unified live_trades table.

Writes are handled by trading_api_routes.py directly into live_trades.
This class is kept for backward compatibility of read methods (analyze_patterns,
get_stats, get_trade_by_oanda_id, get_open_manual_trades).

record_entry() and record_exit() are now no-ops that log deprecation warnings.
"""

import os
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Optional, List
from db_pool import get_trading_forex

logger = logging.getLogger("trading_bot.manual_trade_store")

_DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Data")
_DB_PATH = os.path.join(_DB_DIR, "trade_log.db")


def _bucket(value, thresholds):
    """Bucket a numeric value into a label."""
    for label, lo, hi in thresholds:
        if lo <= value < hi:
            return label
    return "unknown"


def _rsi_bucket(rsi):
    return _bucket(rsi, [("oversold", 0, 30), ("neutral", 30, 70), ("overbought", 70, 101)])


def _stoch_bucket(k):
    return _bucket(k, [("oversold", 0, 20), ("neutral", 20, 80), ("overbought", 80, 101)])


class ManualTradeStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or _DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = get_trading_forex()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS manual_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE,
                    user_id INTEGER NOT NULL,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    units INTEGER,
                    entry_price REAL,
                    entry_time TEXT,

                    -- Exit data (filled on close)
                    exit_price REAL,
                    exit_time TEXT,
                    result TEXT,
                    pips REAL,
                    realized_pl REAL,
                    exit_reason TEXT,
                    hold_bars INTEGER,
                    mfe_pips REAL,
                    mae_pips REAL,

                    -- Full market snapshots (JSON)
                    market_picture JSON,
                    market_story JSON,
                    sniper_scores JSON,
                    candle_structure JSON,
                    indicators JSON,

                    -- Derived fields for fast queries
                    fan_state TEXT,
                    fan_direction TEXT,
                    fan_ordered INTEGER,
                    e100_role TEXT,
                    fan_width_pct REAL,
                    bb_expanding INTEGER,
                    rsi REAL,
                    stoch_k REAL,
                    momentum_state TEXT,
                    trend_health REAL,
                    story_score REAL,
                    story_entry_type TEXT,

                    -- Exit snapshot
                    exit_market_picture JSON,

                    -- Cascade / retracement pattern (Scout revamp fields)
                    dual_cross_cascade INTEGER DEFAULT 0,
                    cascade_direction TEXT,
                    retracement_type TEXT,
                    bb_re_expanding INTEGER DEFAULT 0,
                    tested_e55 INTEGER DEFAULT 0,
                    tested_e100 INTEGER DEFAULT 0,
                    entry_setup_type TEXT,

                    -- Learning
                    pattern_fingerprint TEXT,
                    promoted_to_snipe INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_manual_pair ON manual_trades(pair);
                CREATE INDEX IF NOT EXISTS idx_manual_result ON manual_trades(result);
                CREATE INDEX IF NOT EXISTS idx_manual_fingerprint ON manual_trades(pattern_fingerprint);
                CREATE INDEX IF NOT EXISTS idx_manual_trade_id ON manual_trades(trade_id);
            """)

            # Migration: add cascade/retracement columns to existing databases
            _new_cols = [
                ("dual_cross_cascade", "INTEGER DEFAULT 0"),
                ("cascade_direction", "TEXT"),
                ("retracement_type", "TEXT"),
                ("bb_re_expanding", "INTEGER DEFAULT 0"),
                ("tested_e55", "INTEGER DEFAULT 0"),
                ("tested_e100", "INTEGER DEFAULT 0"),
                ("entry_setup_type", "TEXT"),
            ]
            for _col_name, _col_type in _new_cols:
                try:
                    conn.execute(f"ALTER TABLE manual_trades ADD COLUMN {_col_name} {_col_type}")
                except Exception:
                    pass  # column already exists
        finally:
            pass  # pooled connection, do not close

    def _generate_fingerprint(self, fan_state, fan_direction, fan_ordered,
                               e100_role, bb_expanding, momentum_state,
                               rsi, stoch_k, direction,
                               cascade_direction=None, retracement_type=None):
        """Create a hashable pattern fingerprint from key conditions.

        Now includes cascade/retracement state so winning patterns with
        dual-cross cascades can be distinguished from generic setups.
        """
        parts = [
            fan_state or "unknown",
            fan_direction or "unknown",
            "ordered" if fan_ordered else "unordered",
            e100_role or "unknown",
            "bb_exp" if bb_expanding else "bb_flat",
            momentum_state or "unknown",
            _rsi_bucket(rsi or 50),
            _stoch_bucket(stoch_k or 50),
            direction,
            f"cascade_{cascade_direction}" if cascade_direction else "no_cascade",
            f"retrace_{retracement_type}" if retracement_type else "no_retrace",
        ]
        return "|".join(parts)

    def record_entry(self, pair: str, direction: str, units: int,
                     entry_price: float, trade_id: str,
                     market_picture: Dict = None,
                     market_story: Dict = None,
                     sniper_scores: Dict = None,
                     candle_structure: Dict = None,
                     indicators: Dict = None,
                     user_id: int = None,
                     scout_alert: Dict = None) -> int:
        """DEPRECATED: Writes now go directly to live_trades via trading_api_routes.py.
        This method is a no-op that returns 0. Kept for backward compatibility."""
        logger.info(f"[DEPRECATED] ManualTradeStore.record_entry() called for {trade_id} — "
                     "writes now go to live_trades directly")
        return 0

    def _record_entry_legacy(self, pair: str, direction: str, units: int,
                     entry_price: float, trade_id: str,
                     market_picture: Dict = None,
                     market_story: Dict = None,
                     sniper_scores: Dict = None,
                     candle_structure: Dict = None,
                     indicators: Dict = None,
                     user_id: int = None,
                     scout_alert: Dict = None) -> int:
        """Legacy record_entry — kept for reference. No longer called."""

        # Extract derived fields from market picture
        ema = (market_picture or {}).get('ema', {})
        bb = (market_picture or {}).get('bollinger', {})
        emas = ema.get('current_emas', {})
        e21 = emas.get('ema_21') or emas.get('ema21', 0)
        e55 = emas.get('ema_55') or emas.get('ema55', 0)
        e100 = emas.get('ema_100') or emas.get('ema100', 0)

        fan_state = ema.get('fan_state', 'unknown')
        fan_direction = ema.get('fan_direction', 'unknown')
        e100_role = ema.get('ema100_role', 'unknown')
        trend_health = ema.get('trend_health', 0)

        # Fan ordering check
        if direction == 'buy':
            fan_ordered = 1 if (entry_price > e21 > e55 > e100 and e21 > 0) else 0
        else:
            fan_ordered = 1 if (entry_price < e21 < e55 < e100 and e21 > 0) else 0

        fan_width_pct = abs(e21 - e100) / entry_price * 100 if entry_price > 0 and e21 > 0 else 0
        bb_expanding = 1 if bb.get('bb_expanding', False) else 0

        # Momentum from story
        mom = (market_story or {}).get('layers', {}).get('momentum', {})
        momentum_state = mom.get('state', 'unknown')
        rsi = mom.get('rsi', 50)
        stoch_k = mom.get('stoch_k', 50)

        story_score = (market_story or {}).get('opportunity_score', 0)
        story_entry_type = (market_story or {}).get('entry_type', 'none')

        # Extract cascade/retracement fields from scout_alert if available
        _sa = scout_alert or {}
        dual_cross_cascade = 1 if _sa.get('dual_cross_cascade') else 0
        cascade_direction = _sa.get('cascade_direction')
        retracement_type = _sa.get('retracement_type')
        bb_re_expanding = 1 if _sa.get('bb_re_expanding') else 0
        tested_e55 = 1 if _sa.get('tested_e55') else 0
        tested_e100 = 1 if _sa.get('tested_e100') else 0
        entry_setup_type = _sa.get('alert_type', 'unknown')

        fingerprint = self._generate_fingerprint(
            fan_state, fan_direction, fan_ordered, e100_role,
            bb_expanding, momentum_state, rsi, stoch_k, direction,
            cascade_direction=cascade_direction, retracement_type=retracement_type
        )

        # Serialize JSON safely
        def _json(obj):
            if obj is None:
                return None
            try:
                return json.dumps(obj, default=str)
            except Exception:
                return json.dumps({"error": "serialization_failed"})

        # Serialize indicators — convert numpy/pandas types
        ind_dict = None
        if indicators is not None:
            try:
                ind_dict = {}
                for k, v in (indicators if isinstance(indicators, dict) else indicators.to_dict()).items():
                    try:
                        ind_dict[k] = float(v) if hasattr(v, '__float__') else str(v)
                    except (ValueError, TypeError):
                        ind_dict[k] = str(v)
            except Exception:
                ind_dict = {"error": "conversion_failed"}

        entry_time = datetime.now(timezone.utc).isoformat()

        conn = get_trading_forex()
        try:
            conn.execute("BEGIN")
            cur = conn.execute("""
                INSERT INTO manual_trades (
                    trade_id, pair, direction, units, entry_price, entry_time,
                    market_picture, market_story, sniper_scores, candle_structure, indicators,
                    fan_state, fan_direction, fan_ordered, e100_role, fan_width_pct,
                    bb_expanding, rsi, stoch_k, momentum_state, trend_health,
                    story_score, story_entry_type,
                    dual_cross_cascade, cascade_direction, retracement_type,
                    bb_re_expanding, tested_e55, tested_e100, entry_setup_type,
                    pattern_fingerprint, user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, pair, direction, units, entry_price, entry_time,
                _json(market_picture), _json(market_story), _json(sniper_scores),
                _json(candle_structure), _json(ind_dict),
                fan_state, fan_direction, fan_ordered, e100_role, round(fan_width_pct, 4),
                bb_expanding, round(rsi, 1), round(stoch_k, 1), momentum_state,
                round(trend_health, 1), round(story_score, 1), story_entry_type,
                dual_cross_cascade, cascade_direction, retracement_type,
                bb_re_expanding, tested_e55, tested_e100, entry_setup_type,
                fingerprint, user_id
            ))
            row_id = cur.lastrowid
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            pass  # pooled connection, do not close

        logger.info(
            f"📝 Manual trade recorded: {trade_id} {direction.upper()} {pair} "
            f"@ {entry_price} | fan={fan_state} ordered={fan_ordered} "
            f"width={fan_width_pct:.3f}% bb_exp={bb_expanding} "
            f"cascade={'Y' if dual_cross_cascade else 'N'} cascade_dir={cascade_direction} "
            f"retrace={retracement_type} setup={entry_setup_type} | fingerprint={fingerprint}"
        )
        return row_id

    def record_exit(self, trade_id: str, exit_price: float, realized_pl: float,
                    exit_reason: str, exit_market_picture: Dict = None,
                    hold_bars: int = None, mfe_pips: float = None,
                    mae_pips: float = None, user_id: int = None):
        """DEPRECATED: Exit updates now go directly to live_trades via trading_api_routes.py.
        This method is a no-op. Kept for backward compatibility."""
        logger.info(f"[DEPRECATED] ManualTradeStore.record_exit() called for {trade_id} — "
                     "updates now go to live_trades directly")
        return

    def _record_exit_legacy(self, trade_id: str, exit_price: float, realized_pl: float,
                    exit_reason: str, exit_market_picture: Dict = None,
                    hold_bars: int = None, mfe_pips: float = None,
                    mae_pips: float = None, user_id: int = None):
        """Legacy record_exit — kept for reference. No longer called."""

        # Calculate pips from entry
        conn = get_trading_forex()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT entry_price, direction, pair FROM live_trades WHERE id = ? OR oanda_trade_id = ?",
                (trade_id, trade_id)
            ).fetchone()

            if not row:
                logger.warning(f"Manual trade {trade_id} not found for exit update")
                return

            entry_price = row['entry_price']
            direction = row['direction']
            pair = row['pair']

            jpy = pair in ('USD_JPY', 'EUR_JPY', 'GBP_JPY', 'AUD_JPY')
            multiplier = 100 if jpy else 10000

            if direction == 'buy':
                pips = (exit_price - entry_price) * multiplier
            else:
                pips = (entry_price - exit_price) * multiplier

            result = 'win' if realized_pl > 0 else ('loss' if realized_pl < 0 else 'breakeven')
            exit_time = datetime.now(timezone.utc).isoformat()

            def _json(obj):
                if obj is None:
                    return None
                try:
                    return json.dumps(obj, default=str)
                except Exception:
                    return None

            conn.execute("BEGIN")
            if user_id is not None:
                conn.execute("""
                    UPDATE manual_trades SET
                        exit_price = ?, exit_time = ?, result = ?, pips = ?,
                        realized_pl = ?, exit_reason = ?, hold_bars = ?,
                        mfe_pips = ?, mae_pips = ?, exit_market_picture = ?
                    WHERE trade_id = ? AND user_id = ?
                """, (
                    exit_price, exit_time, result, round(pips, 2),
                    round(realized_pl, 4), exit_reason, hold_bars,
                    mfe_pips, mae_pips, _json(exit_market_picture),
                    trade_id, user_id
                ))
            else:
                conn.execute("""
                    UPDATE manual_trades SET
                        exit_price = ?, exit_time = ?, result = ?, pips = ?,
                        realized_pl = ?, exit_reason = ?, hold_bars = ?,
                        mfe_pips = ?, mae_pips = ?, exit_market_picture = ?
                    WHERE trade_id = ?
                """, (
                    exit_price, exit_time, result, round(pips, 2),
                    round(realized_pl, 4), exit_reason, hold_bars,
                    mfe_pips, mae_pips, _json(exit_market_picture),
                    trade_id
                ))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            pass  # pooled connection, do not close

        logger.info(
            f"📝 Manual trade closed: {trade_id} {result.upper()} "
            f"{pips:+.1f} pips (${realized_pl:+.2f}) | reason={exit_reason}"
        )

        # ── Feed closed trade into SetupRevenueTracker for lifetime P&L tracking ──
        # This powers the winning trade → Scout feedback loop:
        # setup_revenue aggregates gross revenue per setup+pair, and auto-promotes
        # winners (3+ wins, 70%+ WR) to user_snipe_list which Scout watches for.
        try:
            from setup_revenue import SetupRevenueTracker
            _rev_conn = get_trading_forex()
            try:
                _rev_conn.row_factory = sqlite3.Row
                _full_trade = _rev_conn.execute(
                    "SELECT *, id as trade_id FROM live_trades WHERE id = ? OR oanda_trade_id = ?", (trade_id, trade_id)
                ).fetchone()
                if _full_trade and _full_trade['result'] in ('win', 'loss'):
                    _ft = dict(_full_trade)
                    _setup_name = _ft.get('classified_setup') or _ft.get('entry_setup_type') or 'unknown'
                    _user = _ft.get('user_id') or user_id
                    tracker = SetupRevenueTracker(db_path=self.db_path)
                    tracker.record_trade(
                        trade_id=trade_id,
                        setup_name=_setup_name,
                        pair=pair,
                        direction=direction,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        pnl_pips=round(pips, 2),
                        pnl_usd=round(realized_pl, 4),
                        source='manual',
                        user_id=_user,
                    )
                    logger.info(
                        f"📊 Revenue tracked: {_setup_name} on {pair} → {result} "
                        f"{pips:+.1f}p ${realized_pl:+.2f}"
                    )
            finally:
                pass  # pooled connection, do not close
        except Exception as _rev_err:
            logger.warning(f"Setup revenue tracking failed: {_rev_err}")

    def get_open_manual_trades(self, user_id: int) -> List[Dict]:
        """Get open trades for a single user from unified live_trades table.

        Args:
            user_id: Required. Tenant scope — only trades belonging to this user
                     are returned. Raises ValueError if None.
        """
        if user_id is None:
            raise ValueError("user_id is required for get_open_manual_trades")
        conn = get_trading_forex()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT *, id as trade_id FROM live_trades "
                "WHERE result IS NULL AND user_id = ? "
                "ORDER BY entry_time DESC",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            pass  # pooled connection, do not close

    def get_trade_by_oanda_id(self, trade_id: str) -> Optional[Dict]:
        """Look up a trade by OANDA trade ID from unified live_trades table."""
        conn = get_trading_forex()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT *, id as trade_id FROM live_trades WHERE oanda_trade_id = ? OR id = ?",
                (trade_id, trade_id)
            ).fetchone()
            return dict(row) if row else None
        finally:
            pass  # pooled connection, do not close

    def analyze_patterns(self, pair: str = None, min_trades: int = 5) -> List[Dict]:
        """Find recurring patterns in closed trades (reads from unified live_trades)."""
        query = """
            SELECT pattern_fingerprint, direction, pair,
                   COUNT(*) as total,
                   SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins,
                   SUM(pips) as total_pips,
                   AVG(pips) as avg_pips,
                   SUM(CASE WHEN result = 'win' THEN pips ELSE 0 END) as gross_win,
                   ABS(SUM(CASE WHEN result = 'loss' THEN pips ELSE 0 END)) as gross_loss
            FROM live_trades
            WHERE result IS NOT NULL AND pattern_fingerprint IS NOT NULL
        """
        params = []
        if pair:
            query += " AND pair = ?"
            params.append(pair)

        query += " GROUP BY pattern_fingerprint, direction, pair HAVING COUNT(*) >= ?"
        params.append(min_trades)
        query += " ORDER BY COUNT(*) DESC"

        conn = get_trading_forex()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        finally:
            pass  # pooled connection, do not close

        patterns = []
        for r in rows:
            wr = r['wins'] / r['total'] * 100 if r['total'] > 0 else 0
            pf = r['gross_win'] / r['gross_loss'] if r['gross_loss'] > 0 else 99.0
            patterns.append({
                'fingerprint': r['pattern_fingerprint'],
                'direction': r['direction'],
                'pair': r['pair'],
                'total_trades': r['total'],
                'wins': r['wins'],
                'win_rate': round(wr, 1),
                'profit_factor': round(pf, 2),
                'total_pips': round(r['total_pips'], 1),
                'avg_pips': round(r['avg_pips'], 2),
                'promotable': wr >= 65 and pf >= 1.2 and r['total'] >= 8,
            })
        return patterns

    def get_stats(self, user_id: int, pair: str = None) -> Dict:
        """Get summary stats for a single user's trades (reads from unified live_trades).

        Args:
            user_id: Required. Tenant scope — only trades belonging to this user
                     are aggregated. Raises ValueError if None.
            pair:    Optional pair filter (e.g. "EUR_USD").
        """
        if user_id is None:
            raise ValueError("user_id is required for get_stats")
        query = "SELECT *, id as trade_id FROM live_trades WHERE result IS NOT NULL AND user_id = ?"
        params: list = [user_id]
        if pair:
            query += " AND pair = ?"
            params.append(pair)

        conn = get_trading_forex()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        finally:
            pass  # pooled connection, do not close

        if not rows:
            return {'total': 0, 'wins': 0, 'losses': 0, 'win_rate': 0,
                    'total_pips': 0, 'total_pl': 0, 'profit_factor': 0}

        wins = [r for r in rows if r['result'] == 'win']
        losses = [r for r in rows if r['result'] == 'loss']
        gw = sum(r['pips'] for r in wins) if wins else 0
        gl = abs(sum(r['pips'] for r in losses)) if losses else 0
        gl = gl if gl > 0 else 0.01  # avoid divide-by-zero when losses have 0 pips

        return {
            'total': len(rows),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': round(len(wins) / len(rows) * 100, 1),
            'total_pips': round(sum(r['pips'] for r in rows), 1),
            'total_pl': round(sum(r['realized_pl'] for r in rows if r['realized_pl']), 2),
            'profit_factor': round(gw / gl, 2),
        }
