#!/usr/bin/env python3
"""Trading Database Query Layer — connects the data validator to backtest evidence.

Provides fast, purpose-built queries for the trading pipeline:
  - validate_trade_setup() — should we take this trade?
  - get_loss_patterns() — what do losses look like for this setup?
  - check_confluence() — how do concurrent setups affect win rate?
  - check_performance_drift() — is live performance diverging from backtest?
  - get_best_params() — what RR/SL works best for this setup+pair+regime?
  - log_decision() — record a trade decision with full audit trail
  - log_live_trade() — record a live/paper trade
  - log_news_event() — record a news event
  - log_weather_event() — record a weather event
  - log_wolfram_analysis() — record a Wolfram analysis
  - log_market_snapshot() — record a market state snapshot

Usage:
    from Source.backtester.trading_db import TradingDB
    db = TradingDB()
    result = db.validate_trade_setup("EUR_USD", "ranging", "S15", "buy", indicators={...})
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_connection import DB_PATH

logger = logging.getLogger("TradingDB")


class TradingDB:
    """Query layer for the trading backtest + live database."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.execute("PRAGMA query_only=FALSE")
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ================================================================
    # PHASE 2.1: validate_trade_setup — the core question
    # ================================================================

    def validate_trade_setup(self, pair: str, regime: str, setup: str,
                              direction: str = None,
                              indicators: Dict = None,
                              h4_agrees: bool = None,
                              session: str = None) -> Dict[str, Any]:
        """
        Should we take this trade? Query backtest evidence.

        Returns:
            {
                "verdict": "APPROVE" | "REJECT" | "CAUTION",
                "confidence": 0.0-1.0,
                "historical_stats": {...},
                "best_params": {...},
                "warnings": [...],
                "loss_patterns": [...],
                "h4_impact": {...},
                "session_impact": {...}
            }
        """
        t0 = time.time()
        result = {
            "verdict": "REJECT",
            "confidence": 0.0,
            "warnings": [],
            "historical_stats": None,
            "best_params": None,
            "loss_patterns": [],
            "h4_impact": None,
            "session_impact": None,
        }

        # --- Get historical performance for this setup+pair+regime ---
        # Match any param variant of this base setup
        setup_pattern = f"{setup}%" if not setup.startswith("S") or "_rr" not in setup else setup

        rows = self.conn.execute("""
            SELECT setup, trade_count, win_count, win_rate, total_pips, avg_pips,
                   profit_factor, h4_agrees_win_rate, avg_risk_reward,
                   max_favorable, max_adverse, avg_hold_time
            FROM backtest_setup_performance
            WHERE pair=? AND regime=? AND setup LIKE ?
            ORDER BY profit_factor DESC
        """, (pair, regime, setup_pattern)).fetchall()

        # Fallback 1: SNP setups were stored with regime='mixed' — try that
        if not rows and setup.startswith("SNP"):
            rows = self.conn.execute("""
                SELECT setup, trade_count, win_count, win_rate, total_pips, avg_pips,
                       profit_factor, h4_agrees_win_rate, avg_risk_reward,
                       max_favorable, max_adverse, avg_hold_time
                FROM backtest_setup_performance
                WHERE pair=? AND regime='mixed' AND setup LIKE ?
                ORDER BY profit_factor DESC
            """, (pair, setup_pattern)).fetchall()
            if rows:
                result["warnings"].append(f"No regime-specific data for {regime}; using regime=mixed (sniper sweep data)")

        # Fallback 2: broaden to any SNP setup for this pair (different threshold)
        if not rows and setup.startswith("SNP"):
            rows = self.conn.execute("""
                SELECT setup, trade_count, win_count, win_rate, total_pips, avg_pips,
                       profit_factor, h4_agrees_win_rate, avg_risk_reward,
                       max_favorable, max_adverse, avg_hold_time
                FROM backtest_setup_performance
                WHERE pair=? AND setup LIKE 'SNP%'
                ORDER BY profit_factor DESC
            """, (pair,)).fetchall()
            if rows:
                result["warnings"].append(f"No data for {setup} on {pair}; showing all SNP setups for this pair")

        if not rows:
            result["warnings"].append(f"No backtest data for {setup} on {pair} in {regime}")
            result["verdict"] = "REJECT"
            result["confidence"] = 0.1
            return result

        # ── DIRECTION-AWARE CROSS-CHECK (Feb 27 2026) ──
        # backtest_setup_performance has NO direction column. Some setups are
        # direction-locked: S5=buy-only, S6=sell-only, S9=sell-only.
        # If the trade direction doesn't match the setup's backtest direction,
        # the WR is meaningless. Query backtest_trades directly for direction split.
        if direction:
            try:
                dir_check = self.conn.execute("""
                    SELECT direction, COUNT(*) as cnt,
                           ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END)*100.0/COUNT(*), 1) as wr,
                           ROUND(SUM(pips), 1) as total_pips
                    FROM backtest_trades
                    WHERE base_setup=? AND regime=? AND pair=?
                    GROUP BY direction
                """, (setup_pattern.rstrip('%'), regime, pair)).fetchall()

                if dir_check:
                    dir_map = {r["direction"]: {"cnt": r["cnt"], "wr": r["wr"], "pips": r["total_pips"]} for r in dir_check}
                    result["direction_split"] = dir_map

                    trade_dir = direction.lower().replace('bullish', 'buy').replace('bearish', 'sell')
                    if trade_dir in dir_map:
                        # Direction has data — use direction-specific WR
                        dir_data = dir_map[trade_dir]
                        result["direction_wr"] = dir_data["wr"]
                        result["direction_trades"] = dir_data["cnt"]
                        if dir_data["cnt"] < 50:
                            result["warnings"].append(
                                f"Only {dir_data['cnt']} {trade_dir} trades in backtest (low sample)")
                    elif len(dir_map) == 1:
                        # Setup is one-direction only and we're trading the OTHER direction
                        only_dir = list(dir_map.keys())[0]
                        result["warnings"].append(
                            f"⚠️ DIRECTION MISMATCH: {setup} is {only_dir.upper()}-ONLY in backtest "
                            f"({dir_map[only_dir]['cnt']:,} trades) but trade is {trade_dir.upper()}. "
                            f"WR data does NOT apply to this direction!")
                        result["direction_mismatch"] = True
                        result["direction_wr"] = None  # No valid WR for this direction
                        result["direction_trades"] = 0
            except Exception as e:
                # 2026-04-24: upgraded — silent = validator misses direction mismatch signal.
                logger.warning("Direction cross-check FAILED: %s: %s (validator sees no direction mismatch)",
                               type(e).__name__, e)

        # Aggregate across all param variants
        total_trades = sum(r["trade_count"] for r in rows)
        total_wins = sum(r["win_count"] for r in rows)
        total_pips = sum(r["total_pips"] or 0 for r in rows)
        overall_wr = round(100.0 * total_wins / total_trades, 1) if total_trades > 0 else 0

        # Best param variant — cap PF=9999 sentinel (means zero losses, not infinite edge)
        PF_CAP = 50.0  # Realistic max; 9999 is a sentinel for zero losses

        def _effective_pf(r):
            pf = r["profit_factor"] or 0
            return min(pf, PF_CAP)

        best = rows[0]
        viable = [r for r in rows if r["trade_count"] >= 10 and r["profit_factor"] > 1.0]

        # Sort viable by statistical significance: prefer larger samples with real edge
        # Score = trade_count * min(PF, cap) — rewards both volume AND profitability
        viable.sort(key=lambda r: r["trade_count"] * _effective_pf(r), reverse=True)

        # Use best viable row for decisions (not tiny samples with PF=9999)
        # Fall back to raw best if no viable rows
        decision_row = viable[0] if viable else best

        result["historical_stats"] = {
            "total_trades": total_trades,
            "total_wins": total_wins,
            "overall_win_rate": overall_wr,
            "total_pips": round(total_pips, 1),
            "param_variants_tested": len(rows),
            "viable_variants": len(viable),
            "best_setup": decision_row["setup"],
            "best_win_rate": decision_row["win_rate"],
            "best_profit_factor": min(decision_row["profit_factor"] or 0, PF_CAP),
            "best_trade_count": decision_row["trade_count"],
            "best_total_pips": decision_row["total_pips"],
        }

        result["best_params"] = self._extract_params(decision_row["setup"])

        # --- H4 Impact ---
        if h4_agrees is not None and decision_row["h4_agrees_win_rate"] is not None:
            result["h4_impact"] = {
                "h4_agrees_win_rate": decision_row["h4_agrees_win_rate"],
                "current_h4_agrees": h4_agrees,
            }
            if not h4_agrees:
                result["warnings"].append(
                    f"H4 trend disagrees — win rate drops to ~{decision_row['win_rate'] - 4:.0f}% historically"
                )

        # --- Session Impact ---
        if session:
            sess_row = self.conn.execute("""
                SELECT session,
                       COUNT(*) as cnt,
                       ROUND(100.0 * SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) / COUNT(*), 1) as wr
                FROM backtest_trades
                WHERE pair=? AND regime=? AND setup=? AND session=?
            """, (pair, regime, decision_row["setup"], session)).fetchone()
            if sess_row and sess_row["cnt"] and sess_row["cnt"] >= 5:
                result["session_impact"] = {
                    "session": session,
                    "win_rate": sess_row["wr"],
                    "trades": sess_row["cnt"],
                }

        # --- Loss Patterns ---
        result["loss_patterns"] = self.get_loss_patterns(pair, decision_row["setup"], regime, limit=5)

        # --- Indicator Warnings ---
        if indicators:
            result["warnings"].extend(
                self._check_indicator_warnings(pair, decision_row["setup"], regime, indicators)
            )

        # --- Direction mismatch override ---
        # If the setup is one-direction-only and we're trading the other direction,
        # the WR data is invalid. Override historical_stats with direction-specific data.
        if result.get("direction_mismatch"):
            result["historical_stats"]["direction_warning"] = (
                f"Setup {setup} has NO {direction} trades in backtest. "
                f"Reported WR is for the OPPOSITE direction only."
            )
            # Downgrade confidence significantly — we have no valid backtest evidence
            result["verdict"] = "CAUTION"
            result["confidence"] = 0.3
            result["warnings"].append(
                "Direction mismatch: backtest WR does not apply to this trade direction. "
                "Consider using the direction-equivalent setup instead."
            )
        elif result.get("direction_wr") is not None:
            # We have direction-specific WR — add it to historical stats
            result["historical_stats"]["direction_win_rate"] = result["direction_wr"]
            result["historical_stats"]["direction_trade_count"] = result["direction_trades"]

        # --- Verdict (using decision_row = best viable, not raw best) ---
        if result.get("direction_mismatch"):
            # Already set to CAUTION above — don't override with APPROVE
            pass
        elif not viable:
            result["verdict"] = "REJECT"
            result["confidence"] = 0.2
            result["warnings"].append("No param variant is profitable (PF > 1.0) with ≥10 trades")
        elif decision_row["profit_factor"] >= 5.0 and decision_row["trade_count"] >= 20:
            result["verdict"] = "APPROVE"
            result["confidence"] = min(0.95, 0.5 + (decision_row["trade_count"] / 200))
        elif decision_row["profit_factor"] >= 2.0 and decision_row["trade_count"] >= 15:
            result["verdict"] = "APPROVE"
            result["confidence"] = min(0.85, 0.4 + (decision_row["trade_count"] / 200))
        elif decision_row["profit_factor"] >= 1.3 and decision_row["trade_count"] >= 20:
            result["verdict"] = "CAUTION"
            result["confidence"] = min(0.65, 0.3 + (decision_row["trade_count"] / 300))
            result["warnings"].append(f"Marginal edge — PF={decision_row['profit_factor']:.2f}, consider reducing size")
        else:
            result["verdict"] = "REJECT"
            result["confidence"] = 0.3
            result["warnings"].append(f"Insufficient edge — PF={decision_row['profit_factor']:.2f}")

        # Downgrade if H4 disagrees
        if h4_agrees is False and result["verdict"] == "APPROVE":
            result["verdict"] = "CAUTION"
            result["warnings"].append("Downgraded from APPROVE due to H4 disagreement")

        result["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        return result

    # ================================================================
    # PHASE 2.2: get_loss_patterns
    # ================================================================

    def get_loss_patterns(self, pair: str, setup: str, regime: str,
                          limit: int = 10) -> List[Dict]:
        """
        Analyze losing trades to find common indicator patterns.
        Returns clusters of indicator values that appear in losses.
        """
        rows = self.conn.execute("""
            SELECT adx, rsi, bb_width, atr, stoch_k, session,
                   h4_agrees, near_daily_resistance, near_daily_support,
                   loss_streak_at_entry, entry_candle_pattern,
                   price_vs_sma50, price_vs_sma100
            FROM backtest_trades
            WHERE pair=? AND setup=? AND regime=? AND result='loss'
            LIMIT 5000
        """, (pair, setup, regime)).fetchall()

        # Fallback: if no losses with exact setup (e.g. SNP setups not in trades table),
        # try pair+regime only to still provide loss pattern analysis
        if len(rows) < 5:
            rows = self.conn.execute("""
                SELECT adx, rsi, bb_width, atr, stoch_k, session,
                       h4_agrees, near_daily_resistance, near_daily_support,
                       loss_streak_at_entry, entry_candle_pattern,
                       price_vs_sma50, price_vs_sma100
                FROM backtest_trades
                WHERE pair=? AND regime=? AND result='loss'
                LIMIT 5000
            """, (pair, regime)).fetchall()

        # Final fallback: pair only (ignore regime)
        if len(rows) < 5:
            rows = self.conn.execute("""
                SELECT adx, rsi, bb_width, atr, stoch_k, session,
                       h4_agrees, near_daily_resistance, near_daily_support,
                       loss_streak_at_entry, entry_candle_pattern,
                       price_vs_sma50, price_vs_sma100
                FROM backtest_trades
                WHERE pair=? AND result='loss'
                LIMIT 5000
            """, (pair,)).fetchall()

        if len(rows) < 5:
            return [{"note": f"Only {len(rows)} losses — insufficient data for pattern analysis"}]

        patterns = []

        # ADX clustering
        adx_vals = [r["adx"] for r in rows if r["adx"] is not None]
        if adx_vals:
            low_adx_losses = len([v for v in adx_vals if v < 25])
            if low_adx_losses / len(adx_vals) > 0.6:
                patterns.append({
                    "pattern": "low_adx",
                    "description": f"{round(100*low_adx_losses/len(adx_vals))}% of losses had ADX < 25 (weak trend)",
                    "filter_suggestion": "Consider requiring ADX > 25"
                })

        # Near resistance (for buys)
        near_res = [r for r in rows if r["near_daily_resistance"] in ('True', '1', 'true')]
        if len(near_res) / max(len(rows), 1) > 0.3:
            patterns.append({
                "pattern": "near_resistance",
                "description": f"{round(100*len(near_res)/len(rows))}% of losses were near daily resistance",
                "filter_suggestion": "Avoid entries near daily resistance levels"
            })

        # H4 disagrees
        h4_disagree = [r for r in rows if r["h4_agrees"] in ('False', '0', 'false')]
        if len(h4_disagree) / max(len(rows), 1) > 0.6:
            patterns.append({
                "pattern": "h4_disagrees",
                "description": f"{round(100*len(h4_disagree)/len(rows))}% of losses had H4 trend disagreeing",
                "filter_suggestion": "Only trade when H4 trend agrees"
            })

        # BB width (low volatility)
        bb_vals = [r["bb_width"] for r in rows if r["bb_width"] is not None]
        if bb_vals:
            median_bb = sorted(bb_vals)[len(bb_vals)//2]
            low_bb = len([v for v in bb_vals if v < median_bb * 0.5])
            if low_bb / len(bb_vals) > 0.4:
                patterns.append({
                    "pattern": "low_volatility",
                    "description": f"{round(100*low_bb/len(bb_vals))}% of losses had very narrow Bollinger Bands",
                    "filter_suggestion": "Avoid low-volatility environments for this setup"
                })

        # Session clustering
        session_counts = {}
        for r in rows:
            s = r["session"] or "unknown"
            session_counts[s] = session_counts.get(s, 0) + 1
        worst_session = max(session_counts, key=session_counts.get) if session_counts else None
        if worst_session and session_counts[worst_session] / len(rows) > 0.4:
            patterns.append({
                "pattern": "session_clustering",
                "description": f"{round(100*session_counts[worst_session]/len(rows))}% of losses occurred during {worst_session}",
                "filter_suggestion": f"Avoid trading during {worst_session}"
            })

        # Loss streak
        streak_vals = [r["loss_streak_at_entry"] for r in rows if r["loss_streak_at_entry"] is not None]
        if streak_vals:
            high_streak = len([v for v in streak_vals if v >= 3])
            if high_streak / len(streak_vals) > 0.2:
                patterns.append({
                    "pattern": "loss_streak",
                    "description": f"{round(100*high_streak/len(streak_vals))}% of losses came after 3+ consecutive losses",
                    "filter_suggestion": "Pause trading after 3 consecutive losses"
                })

        return patterns[:limit]

    # ================================================================
    # PHASE 2.3: check_confluence
    # ================================================================

    def check_confluence(self, pair: str, setups_firing: List[str],
                          regime: str = None) -> Dict[str, Any]:
        """
        When multiple setups fire simultaneously, what's the historical win rate?
        """
        if not setups_firing or len(setups_firing) < 2:
            return {"note": "Need at least 2 setups to check confluence"}

        # Check each pair of setups
        results = []
        for setup in setups_firing:
            # Find trades where this setup fired AND the other setups were concurrent
            other_setups = [s for s in setups_firing if s != setup]
            for other in other_setups:
                query = """
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                           ROUND(SUM(COALESCE(combined_pips, pips)), 1) as pips
                    FROM backtest_trades
                    WHERE pair=? AND setup LIKE ? AND concurrent_setups LIKE ?
                """
                params = [pair, f"{setup}%", f"%{other}%"]
                if regime:
                    query += " AND regime=?"
                    params.append(regime)

                row = self.conn.execute(query, params).fetchone()
                if row and row["total"] and row["total"] >= 5:
                    wr = round(100.0 * row["wins"] / row["total"], 1)
                    results.append({
                        "setup": setup,
                        "concurrent_with": other,
                        "trades": row["total"],
                        "win_rate": wr,
                        "pips": row["pips"],
                    })

        # Also get solo performance for comparison
        solo_stats = {}
        for setup in setups_firing:
            query = "SELECT win_rate, trade_count FROM backtest_setup_performance WHERE pair=? AND setup LIKE ?"
            params = [pair, f"{setup}%"]
            if regime:
                query += " AND regime=?"
                params.append(regime)
            query += " ORDER BY profit_factor DESC LIMIT 1"
            row = self.conn.execute(query, params).fetchone()
            if row:
                solo_stats[setup] = {"win_rate": row["win_rate"], "trades": row["trade_count"]}

        confluence_boost = None
        if results and solo_stats:
            avg_confluence_wr = sum(r["win_rate"] for r in results) / len(results)
            avg_solo_wr = sum(s["win_rate"] for s in solo_stats.values()) / len(solo_stats)
            confluence_boost = round(avg_confluence_wr - avg_solo_wr, 1)

        return {
            "confluence_results": results,
            "solo_performance": solo_stats,
            "confluence_boost_pp": confluence_boost,
            "recommendation": "Confluence improves edge" if confluence_boost and confluence_boost > 3 else "Minimal confluence benefit"
        }

    # ================================================================
    # PHASE 2.4: check_performance_drift
    # ================================================================

    def check_performance_drift(self, pair: str, setup: str,
                                 regime: str = None) -> Dict[str, Any]:
        """
        Compare live trade performance against backtest baseline.
        Alerts if live is underperforming.
        """
        # Backtest baseline
        query = """
            SELECT win_rate, profit_factor, trade_count, total_pips, avg_pips
            FROM backtest_setup_performance
            WHERE pair=? AND setup=?
        """
        params = [pair, setup]
        if regime:
            query += " AND regime=?"
            params.append(regime)
        query += " ORDER BY trade_count DESC LIMIT 1"
        bt_row = self.conn.execute(query, params).fetchone()

        # Live performance
        query = """
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(COALESCE(combined_pips, pips)), 1) as pips
            FROM live_trades
            WHERE pair=? AND setup=?
        """
        params = [pair, setup]
        if regime:
            query += " AND regime=?"
            params.append(regime)
        live_row = self.conn.execute(query, params).fetchone()

        result = {"backtest": None, "live": None, "drift": None, "alert": False}

        if bt_row:
            result["backtest"] = {
                "win_rate": bt_row["win_rate"],
                "profit_factor": bt_row["profit_factor"],
                "trades": bt_row["trade_count"],
                "pips": bt_row["total_pips"],
            }

        if live_row and live_row["total"] and live_row["total"] > 0:
            live_wr = round(100.0 * live_row["wins"] / live_row["total"], 1)
            result["live"] = {
                "win_rate": live_wr,
                "trades": live_row["total"],
                "pips": live_row["pips"],
            }

            if bt_row and live_row["total"] >= 10:
                wr_diff = live_wr - bt_row["win_rate"]
                result["drift"] = {
                    "win_rate_diff": round(wr_diff, 1),
                    "direction": "improving" if wr_diff > 0 else "degrading",
                }
                if wr_diff < -10:
                    result["alert"] = True
                    result["alert_message"] = (
                        f"DRIFT ALERT: {setup} on {pair} live win rate ({live_wr}%) is "
                        f"{abs(wr_diff):.1f}pp below backtest ({bt_row['win_rate']}%). "
                        f"Consider pausing this setup."
                    )
        else:
            result["live"] = {"note": "No live trades yet"}

        return result

    # ================================================================
    # PHASE 2.5: get_best_params
    # ================================================================

    def get_best_params(self, pair: str, regime: str,
                         base_setup: str = None,
                         min_trades: int = 10) -> List[Dict]:
        """Get the best performing parameter variants for a pair+regime."""
        query = """
            SELECT setup, trade_count, win_rate, profit_factor, total_pips, avg_pips
            FROM backtest_setup_performance
            WHERE pair=? AND regime=? AND trade_count>=? AND profit_factor > 1.0
        """
        params = [pair, regime, min_trades]
        if base_setup:
            query += " AND setup LIKE ?"
            params.append(f"{base_setup}%")
        query += " ORDER BY profit_factor DESC LIMIT 20"

        rows = self.conn.execute(query, params).fetchall()
        
        # Fallback: SNP setups stored with regime='mixed'
        if not rows and (not base_setup or base_setup.startswith("SNP")):
            query2 = """
                SELECT setup, trade_count, win_rate, profit_factor, total_pips, avg_pips
                FROM backtest_setup_performance
                WHERE pair=? AND setup LIKE 'SNP%' AND trade_count>=? AND profit_factor > 1.0
                ORDER BY profit_factor DESC LIMIT 20
            """
            rows = self.conn.execute(query2, (pair, min_trades)).fetchall()

        return [
            {
                "setup": r["setup"],
                "trades": r["trade_count"],
                "win_rate": r["win_rate"],
                "profit_factor": r["profit_factor"],
                "total_pips": r["total_pips"],
                "avg_pips": r["avg_pips"],
                **self._extract_params(r["setup"]),
            }
            for r in rows
        ]

    # ================================================================
    # LOGGING METHODS — Phase 3 prep
    # ================================================================

    def log_decision(self, pair: str, timeframe: str, setup: str,
                      direction: str, regime: str,
                      market_data: Dict = None, news_data: Dict = None,
                      weather_data: Dict = None, wolfram_data: Dict = None,
                      verdict: str = "REJECT", confidence: float = 0.0,
                      reasoning: str = "", db_evidence: Dict = None,
                      loss_patterns: List = None, confluence: Dict = None,
                      recommended_rr: float = None, recommended_sl: float = None,
                      recommended_size: float = None, size_reason: str = None,
                      final_action: str = "SKIP", action_reason: str = "",
                      execution_time_ms: int = None,
                      user_id: int = None) -> str:
        """Log a trade decision. Returns decision_id."""
        # Resolve user_id from env if not provided
        if user_id is None:
            _env_uid = os.environ.get('TRADING_USER_ID')
            if _env_uid:
                try:
                    user_id = int(_env_uid)
                except (ValueError, TypeError):
                    pass

        # Ensure user_id column exists (migration for existing DBs)
        try:
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trade_decisions)").fetchall()}
            if 'user_id' not in cols:
                self.conn.execute("ALTER TABLE trade_decisions ADD COLUMN user_id INTEGER")
                self.conn.commit()
                logger.info("Migrated trade_decisions: added user_id column")
        except Exception as e:
            logger.debug("user_id migration check for trade_decisions: %s", e)

        decision_id = str(uuid.uuid4())[:8]
        self.conn.execute("""
            INSERT INTO trade_decisions (
                decision_id, pair, timeframe, setup, direction, regime,
                market_agent_data, news_agent_data, weather_agent_data, wolfram_agent_data,
                validator_verdict, validator_confidence, validator_reasoning,
                validator_db_evidence, validator_loss_patterns, validator_confluence,
                recommended_rr, recommended_sl, recommended_size, recommended_size_reason,
                final_action, final_action_reason, execution_time_ms, user_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            decision_id, pair, timeframe, setup, direction, regime,
            json.dumps(market_data) if market_data else None,
            json.dumps(news_data) if news_data else None,
            json.dumps(weather_data) if weather_data else None,
            json.dumps(wolfram_data) if wolfram_data else None,
            verdict, confidence, reasoning,
            json.dumps(db_evidence) if db_evidence else None,
            json.dumps(loss_patterns) if loss_patterns else None,
            json.dumps(confluence) if confluence else None,
            recommended_rr, recommended_sl, recommended_size, size_reason,
            final_action, action_reason, execution_time_ms, user_id
        ))
        self.conn.commit()
        return decision_id

    def log_live_trade(self, trade_data: Dict[str, Any]) -> str:
        """Log a live/paper trade. trade_data should match live_trades schema.
        Unknown columns are silently dropped to avoid INSERT errors."""
        trade_id = trade_data.get("trade_id", str(uuid.uuid4())[:8])
        trade_data = dict(trade_data)  # copy to avoid mutating caller's dict
        trade_data["trade_id"] = trade_id

        # Get valid column names from the table
        valid_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(live_trades)").fetchall()}
        # Filter to only valid columns
        trade_data = {k: v for k, v in trade_data.items() if k in valid_cols}

        # Ensure NOT NULL columns have defaults
        trade_data.setdefault("source", "paper")
        trade_data.setdefault("pair", "")
        # user_id must be provided by caller — no hardcoded default
        if "user_id" not in trade_data:
            trade_data["user_id"] = None
        trade_data.setdefault("timeframe", "M15")
        trade_data.setdefault("setup", "unknown")
        trade_data.setdefault("direction", "neutral")
        trade_data.setdefault("entry_time", datetime.now(timezone.utc).isoformat())
        trade_data.setdefault("entry_price", 0.0)
        trade_data.setdefault("sl_price", 0.0)
        trade_data.setdefault("tp_price", 0.0)

        columns = list(trade_data.keys())
        placeholders = ",".join(["?" for _ in columns])
        col_names = ",".join([f'"{c}"' for c in columns])

        self.conn.execute(
            f"INSERT INTO live_trades ({col_names}) VALUES ({placeholders})",
            list(trade_data.values())
        )
        self.conn.commit()
        return trade_id

    def update_trade_outcome(self, trade_id: str = None, decision_id: str = None,
                              outcome: str = None, pips: float = None):
        """Update outcome after a trade closes."""
        if trade_id:
            self.conn.execute("""
                UPDATE live_trades SET result=?, pips=?, updated_at=datetime('now')
                WHERE trade_id=?
            """, (outcome, pips, trade_id))
        if decision_id:
            self.conn.execute("""
                UPDATE trade_decisions SET outcome=?, outcome_pips=?,
                    live_trade_id=?
                WHERE decision_id=?
            """, (outcome, pips, trade_id, decision_id))
        self.conn.commit()

    def log_news_event(self, category: str, impact_level: str, headline: str,
                        currencies_affected: str = None, pairs_affected: str = None,
                        sentiment_score: float = None, direction_bias: str = None,
                        event_time: str = None, is_upcoming: bool = False,
                        source: str = None, url: str = None, summary: str = None) -> int:
        """Log a news event. Returns event_id."""
        cursor = self.conn.execute("""
            INSERT INTO news_events (
                category, impact_level, headline, currencies_affected, pairs_affected,
                sentiment_score, direction_bias, event_time, is_upcoming,
                source, url, summary
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (category, impact_level, headline, currencies_affected, pairs_affected,
              sentiment_score, direction_bias, event_time, 1 if is_upcoming else 0,
              source, url, summary))
        self.conn.commit()
        return cursor.lastrowid

    def log_weather_event(self, region: str, country: str, event_type: str,
                           severity: int, description: str = None,
                           currencies_affected: str = None, pairs_affected: str = None,
                           estimated_impact: str = None, impact_direction: str = None) -> int:
        """Log an extreme weather event. Returns event_id."""
        cursor = self.conn.execute("""
            INSERT INTO weather_events (
                region, country, event_type, severity, description,
                currencies_affected, pairs_affected, estimated_impact, impact_direction
            ) VALUES (?,?,?,?,?,?,?,?,?)
        """, (region, country, event_type, severity, description,
              currencies_affected, pairs_affected, estimated_impact, impact_direction))
        self.conn.commit()
        return cursor.lastrowid

    def log_wolfram_analysis(self, query_type: str, query_text: str,
                              result_summary: str, result_data: Dict = None,
                              result_value: float = None, decision_id: str = None,
                              pair: str = None) -> int:
        """Log a Wolfram analysis. Returns analysis_id."""
        cursor = self.conn.execute("""
            INSERT INTO wolfram_analyses (
                query_type, query_text, result_summary, result_data,
                result_value, decision_id, pair
            ) VALUES (?,?,?,?,?,?,?)
        """, (query_type, query_text, result_summary,
              json.dumps(result_data) if result_data else None,
              result_value, decision_id, pair))
        self.conn.commit()
        return cursor.lastrowid

    def log_market_snapshot(self, snapshot: Dict[str, Any]) -> int:
        """Log a market snapshot. snapshot should match market_snapshots schema."""
        columns = list(snapshot.keys())
        placeholders = ",".join(["?" for _ in columns])
        col_names = ",".join([f'"{c}"' for c in columns])
        cursor = self.conn.execute(
            f"INSERT OR REPLACE INTO market_snapshots ({col_names}) VALUES ({placeholders})",
            list(snapshot.values())
        )
        self.conn.commit()
        return cursor.lastrowid

    # ================================================================
    # UTILITY: check upcoming high-impact news
    # ================================================================

    def get_upcoming_high_impact_news(self, currencies: List[str] = None,
                                       hours_ahead: int = 24) -> List[Dict]:
        """Check for upcoming high-impact events that could affect trading."""
        query = """
            SELECT headline, event_time, currencies_affected, impact_level, category
            FROM news_events
            WHERE is_upcoming=1 AND is_active=1 AND impact_level='high'
        """
        if currencies:
            currency_clauses = " OR ".join([f"currencies_affected LIKE '%{c}%'" for c in currencies])
            query += f" AND ({currency_clauses})"
        query += " ORDER BY event_time"

        return [dict(r) for r in self.conn.execute(query).fetchall()]

    def get_active_weather_warnings(self, currencies: List[str] = None) -> List[Dict]:
        """Check for active extreme weather events."""
        query = "SELECT * FROM weather_events WHERE is_active=1 AND severity >= 3"
        if currencies:
            clauses = " OR ".join([f"currencies_affected LIKE '%{c}%'" for c in currencies])
            query += f" AND ({clauses})"
        return [dict(r) for r in self.conn.execute(query).fetchall()]

    # ================================================================
    # HELPERS
    # ================================================================

    def _extract_params(self, setup_name: str) -> Dict:
        """Extract rr_mult and sl_mult from setup name like 'S15_rr2.0_sl2.5'."""
        result = {"rr_mult": None, "sl_mult": None}
        if "_rr" in setup_name and "_sl" in setup_name:
            try:
                parts = setup_name.split("_")
                for p in parts:
                    if p.startswith("rr"):
                        result["rr_mult"] = float(p[2:])
                    elif p.startswith("sl"):
                        result["sl_mult"] = float(p[2:])
            except (ValueError, IndexError):
                pass
        return result

    def _check_indicator_warnings(self, pair: str, setup: str, regime: str,
                                    indicators: Dict) -> List[str]:
        """Check if current indicators match loss patterns."""
        warnings = []

        # Get average indicator values for losses
        loss_avgs = self.conn.execute("""
            SELECT AVG(adx) as avg_adx, AVG(rsi) as avg_rsi, AVG(bb_width) as avg_bb
            FROM backtest_trades
            WHERE pair=? AND setup=? AND regime=? AND result='loss'
        """, (pair, setup, regime)).fetchone()

        if loss_avgs and loss_avgs["avg_adx"]:
            current_adx = indicators.get("adx")
            if current_adx and current_adx < loss_avgs["avg_adx"] * 0.8:
                warnings.append(
                    f"ADX ({current_adx:.1f}) is well below loss average ({loss_avgs['avg_adx']:.1f}) — "
                    f"weak trend conditions historically produce losses"
                )

            current_bb = indicators.get("bb_width")
            if current_bb and loss_avgs["avg_bb"] and current_bb < loss_avgs["avg_bb"] * 0.5:
                warnings.append(
                    f"BB width ({current_bb:.5f}) very narrow — low volatility historically produces losses"
                )

        return warnings
