"""Watch Manager — turns validator suggestions into scheduled market watches.

When the validator returns HOLD with re-entry suggestions, the orchestrator
can create a "watch task" that periodically checks if conditions are met.
If they are, it triggers a fresh trading cycle.

Watch tasks are:
- Short-lived (TTL = 4 hours or session end, whichever is first)
- Lightweight (pure Python indicator checks, no LLM calls)
- Graded (every suggestion is tracked: triggered vs expired, win/loss if traded)

The watch loop runs compute_sniper_score() (~0.04s) and checks specific
indicator conditions. No intelligence gathering, no LLM reasoning.
"""

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# Import connection pool for efficient database access
from db_pool import get_trading_forex, get_workspaces

def _safe_isoformat(raw: str) -> datetime:
    """Parse an ISO timestamp, truncating nanoseconds to 6 decimals.
    OANDA sends 9-decimal fractional seconds; Python's fromisoformat()
    only supports up to 6.  This mirrors oanda_client._parse_oanda_time().
    """
    s = str(raw).replace('Z', '+00:00')
    if '.' in s:
        int_part, frac_rest = s.split('.', 1)
        offset = ''
        for sep in ('+', '-'):
            if sep in frac_rest[1:]:
                idx = frac_rest.index(sep, 1)
                offset = frac_rest[idx:]
                frac_rest = frac_rest[:idx]
                break
        frac_rest = frac_rest[:6].ljust(6, '0')
        s = f"{int_part}.{frac_rest}{offset}"
    return datetime.fromisoformat(s)

try:
    from flight_recorder import flight, FlightStage
except ImportError:
    flight = None
    FlightStage = None

# Tuning config — central parameter store
try:
    from tuning_config import get as tc_get
except ImportError:
    tc_get = lambda param, fallback=None: fallback

logger = logging.getLogger("trading_bot.agents.watch_manager")

DB_PATH = Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "trading_forex.db"
_CONFIG_PATH = Path(__file__).parent.parent.parent / "Config" / "risk_config.json"


def _watch_config():
    """Load watch TTL and check interval from risk_config.json."""
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
        return {
            "ttl_hours": float(cfg.get("watch_ttl_hours", 8)),  # Default 8h — prevents zombie watches accumulating
            "check_interval_sec": int(cfg.get("watch_check_interval_min", 5)) * 60,
        }
    except Exception:
        return {"ttl_hours": 8, "check_interval_sec": 300}  # 8h default TTL


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

# Snipe criteria cap: validator can generate 10+ conditions which makes
# snipes nearly impossible to trigger. Keep between 3–7.
MAX_CONDITIONS = 7

_tables_ensured = False

def _ensure_tables():
    """Create watch_suggestions table if it doesn't exist. Runs ONCE per process."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn = get_trading_forex()
    # Cleanup: reset stale 'triggered' watches to 'watching' on startup.
    # Two cases:
    #   1. triggered_at > 30 min ago with no trade_cycle_id — never filled, reset cleanly.
    #   2. Has _last_fill_close_time in context — trade closed, reset so cooldown gate governs re-fire.
    # Without this they sit triggered forever and fire the full pipeline on every 5-min check.
    try:
        conn.execute("""
            UPDATE watch_suggestions
            SET status = 'watching',
                triggered_at = NULL,
                stale_flagged_at = NULL
            WHERE status = 'triggered'
              AND trade_cycle_id IS NULL
              AND triggered_at < datetime('now', '-30 minutes')
        """)
        conn.commit()
    except Exception:
        pass
    # Also reset triggered watches whose trade already closed (guardian stamped _last_fill_close_time).
    # These are safe to reset because the cooldown check in check_active_watches will block re-fire
    # for 15 min, and after that the snipe can legitimately re-evaluate market conditions.
    # BUT: skip watches whose trade_cycle_id matches a still-open live_trade — the close_time
    # is from a PREVIOUS fill and the current trade is still running.
    try:
        conn.execute("""
            UPDATE watch_suggestions
            SET status = 'watching',
                triggered_at = NULL,
                trade_cycle_id = NULL,
                stale_flagged_at = NULL
            WHERE status = 'triggered'
              AND json_extract(COALESCE(context, '{}'), '$._last_fill_close_time') IS NOT NULL
              AND CAST(json_extract(COALESCE(context, '{}'), '$._last_fill_close_time') AS REAL)
                  < (strftime('%s','now') - 900)
              AND (trade_cycle_id IS NULL
                   OR trade_cycle_id NOT IN (SELECT id FROM live_trades WHERE status='open'))
        """)
        conn.commit()
    except Exception:
        pass
    # Case 3: Revive watches that were wrongly mass-superseded by floor_chat.
    # floor_chat used to supersede ALL watching watches for a pair when creating
    # a new user snipe — even ones with different conditions. Snipes should
    # accumulate, so revive them (respecting max-2-per-instrument).
    try:
        _superseded = conn.execute(
            "SELECT id, instrument FROM watch_suggestions "
            "WHERE status='superseded' AND created_at >= datetime('now', '-7 days')"
        ).fetchall()
        _revived = 0
        for _sid, _sinst in _superseded:
            _active_cnt = conn.execute(
                "SELECT count(*) FROM watch_suggestions WHERE instrument=? AND status='watching'",
                (_sinst,)
            ).fetchone()[0]
            if _active_cnt < 2:
                conn.execute("UPDATE watch_suggestions SET status='watching' WHERE id=?", (_sid,))
                _revived += 1
        if _revived:
            conn.commit()
            logger.info("[watch] Startup: revived %d wrongly-superseded watches", _revived)
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watch_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id TEXT,
            instrument TEXT NOT NULL,
            suggestion_type TEXT,
            conditions TEXT,
            raw_suggestion TEXT,
            validator_verdict TEXT,
            validator_confidence REAL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_checked_at TEXT,
            check_count INTEGER DEFAULT 0,
            triggered_at TEXT,
            trade_cycle_id TEXT,
            trade_outcome TEXT,
            pips_result REAL,
            status TEXT DEFAULT 'watching',
            workspace_task_id INTEGER,
            agent_name TEXT DEFAULT 'snipe'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_watch_status 
        ON watch_suggestions(status, instrument)
    """)
    # Cross-user snipe performance leaderboard
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snipe_leaderboard (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conditions_hash TEXT NOT NULL,
            instrument TEXT NOT NULL,
            conditions TEXT,
            suggestion_type TEXT,
            times_created INTEGER DEFAULT 1,
            times_triggered INTEGER DEFAULT 0,
            times_won INTEGER DEFAULT 0,
            total_pips REAL DEFAULT 0,
            avg_pips REAL DEFAULT 0,
            win_rate REAL DEFAULT 0,
            last_triggered_at TEXT,
            last_updated_at TEXT,
            UNIQUE(conditions_hash, instrument)
        )
    """)
    conn.commit()
    # ── GAP-5/6/7 schema stubs — add columns if they don't exist yet ─────────
    # These are safe to run on every startup: SQLite raises OperationalError
    # if the column already exists, which we swallow silently.
    # Schema stubs — only need to run once. Check if columns exist first
    # to avoid failed ALTER TABLE statements that can leave implicit transactions open.
    try:
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(watch_suggestions)").fetchall()}
        _stub_cols = {
            "stale_flagged_at": "ALTER TABLE watch_suggestions ADD COLUMN stale_flagged_at TEXT",
            "criteria_hit_rate": "ALTER TABLE watch_suggestions ADD COLUMN criteria_hit_rate REAL",
            "criteria_scan_count": "ALTER TABLE watch_suggestions ADD COLUMN criteria_scan_count INTEGER DEFAULT 0",
            "criteria_met_count": "ALTER TABLE watch_suggestions ADD COLUMN criteria_met_count INTEGER DEFAULT 0",
            "last_graded_at": "ALTER TABLE watch_suggestions ADD COLUMN last_graded_at TEXT",
            # ── Pipeline lineage columns ──
            "trade_id": "ALTER TABLE watch_suggestions ADD COLUMN trade_id TEXT",
            "finding_id": "ALTER TABLE watch_suggestions ADD COLUMN finding_id INTEGER",
            # ── Kronos snipe columns ──
            "source": "ALTER TABLE watch_suggestions ADD COLUMN source TEXT",
            "expiry_time": "ALTER TABLE watch_suggestions ADD COLUMN expiry_time TEXT",
            "direction": "ALTER TABLE watch_suggestions ADD COLUMN direction TEXT",
        }
        for col_name, sql in _stub_cols.items():
            if col_name not in existing_cols:
                conn.execute(sql)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Multi-user: add user_id to snipe_leaderboard if missing ──────────
    try:
        _lb_cols = {row[1] for row in conn.execute("PRAGMA table_info(snipe_leaderboard)").fetchall()}
        if 'user_id' not in _lb_cols:
            conn.execute("ALTER TABLE snipe_leaderboard ADD COLUMN user_id INTEGER")
            conn.commit()
            logger.info("Migrated snipe_leaderboard: added user_id column")
    except Exception:
        pass

    _tables_ensured = True
    logger.info("watch_suggestions + snipe_leaderboard tables ready")


# ---------------------------------------------------------------------------
# Parse validator suggestions into measurable conditions
# ---------------------------------------------------------------------------

def parse_suggestions(validator_response: dict, instrument: str,
                      sniper_data: dict = None) -> List[Dict[str, Any]]:
    """Extract measurable conditions from validator's HOLD/CAUTION/REJECT suggestions.

    PRIORITY 1: Use structured `re_entry_conditions` from Validator LLM (preferred).
    PRIORITY 2: Regex-parse free-text reasoning (legacy fallback).

    Returns list of watch configs, each with:
        suggestion_type, conditions (list of checks), raw_text, priority
    """
    watches = []

    # 2026-04-27: Direction-aware filter for criteria. Three bugs were producing
    # unfirable watches: (1) validator writing both ema_cross_above AND
    # ema_cross_below in the same watch (mutually exclusive), (2) text-extraction
    # pulling "close above X" / "E21 crosses above E55" from validator's
    # INVALIDATION reasoning and encoding them as positive entry conditions
    # that contradict the trade direction, (3) bb_bandwidth values written as
    # percentage (0.15) instead of raw decimal (0.0015), making thresholds
    # 100x off and impossible to reach.
    _watch_dir = _normalize_direction(
        validator_response.get("re_entry_direction")
        or validator_response.get("direction")
        or ""
    )

    def _is_dir_compatible(field: str, op: str, value: Any) -> bool:
        """True if condition matches watch direction. False = drop (it's an
        invalidation level masquerading as an entry condition)."""
        if not _watch_dir:
            return True  # Unknown direction — preserve current behavior
        # SELL watch: trigger when price goes DOWN (close <), fan ordered bearish
        if _watch_dir == "sell":
            if field == "ema_cross_above":
                return False
            if field == "close" and op in (">", ">="):
                return False
            if field == "price_above":
                return False
        # BUY watch: trigger when price goes UP (close >), fan ordered bullish
        elif _watch_dir == "buy":
            if field == "ema_cross_below":
                return False
            if field == "close" and op in ("<", "<="):
                return False
            if field == "price_below":
                return False
        return True

    def _normalize_bb_bandwidth(value: Any) -> Any:
        """Validator sometimes writes 0.15 thinking percentage; bb_bandwidth is
        stored as raw decimal where realistic forex M15 values are 0.001-0.01.
        If value is clearly off-scale (>= 0.05), treat as percentage and divide."""
        try:
            v = float(value)
            return v / 100.0 if v >= 0.05 else v
        except (TypeError, ValueError):
            return value

    # ── PRIORITY 1: Structured re-entry conditions from Validator ──
    re_entry = validator_response.get("re_entry_conditions", [])
    if isinstance(re_entry, list) and len(re_entry) > 0:
        # Validate each condition has required fields
        VALID_FIELDS = {
            # Legacy indicators (still checkable)
            "rsi", "rsi_slope", "stoch_k", "stoch_d", "adx", "macd_hist",
            "bb_width", "atr", "cci", "close", "ema_21", "ema_55", "ema_100",
            "sar", "max_score", "buy_score", "sell_score", "h4_bias", "h4_rsi",
            "has_reversal_pattern", "has_pattern", "has_chart_pattern", "regime",
            "classified_setup", "session", "total_score",
            # EMA narrative
            "ema_fan_state", "ema_trend_health", "ema_velocity", "ema_reversal_risk",
            "ema_price_near_e100", "close_vs_ema",  # fishing line / retracement snipe fields
            # Market story — candle structure
            "wick_pressure", "wick_pressure_strength", "e100_interaction",
            "e100_bounces", "e100_breaks", "body_trend", "body_direction_bias",
            "range_trend", "run_state", "price_position",
            # Market story — momentum synthesis
            "momentum_state", "momentum_exhausted", "momentum_significance",
            # Market story — thesis
            "story_entry_type", "story_opportunity_score", "story_has_opportunity",
            # Bollinger bandwidth
            "bb_expanding", "bb_contracting", "bb_acceleration", "bb_width_trend",
            # Candle/momentum conditions from validator
            "momentum_candles", "has_momentum_candles", "fan_accelerating", "fan_opening",
            "rsi_recovering", "correct_side", "no_wall",
            # Price-level conditions (validator snipe specifics)
            "price_zone", "price_above", "price_below",
            "invalidation_level",
            # BB bandwidth threshold (numeric, not just boolean)
            "bb_bandwidth",
            # BB squeeze break direction
            "bb_squeeze_break",
            # Specific EMA cross ordering
            "ema_cross_below", "ema_cross_above",
        }
        VALID_OPS = {">=", ">", "<=", "<", "==", "in"}
        
        conditions = []
        raw_parts = []
        for cond in re_entry:
            if not isinstance(cond, dict):
                continue
            field = cond.get("field", "")
            op = cond.get("op", "")
            value = cond.get("value")
            reason = cond.get("reason", "")
            
            if field not in VALID_FIELDS:
                logger.warning("Validator re_entry condition has unknown field '%s' — skipping", field)
                continue
            if op not in VALID_OPS:
                logger.warning("Validator re_entry condition has invalid op '%s' — skipping", op)
                continue
            if value is None:
                continue

            # 2026-04-27 BUG1 fix: drop direction-contradicting conditions
            # (e.g. ema_cross_above on a SELL watch). Validator sometimes writes
            # criteria for BOTH directions; only the matching one is fireable.
            if not _is_dir_compatible(field, op, value):
                logger.info("[parse_suggestions] dropped direction-contradicting "
                            "%s %s %s on %s watch", field, op, value, _watch_dir)
                continue

            # 2026-04-27 BUG3 fix: normalize bb_bandwidth scale
            if field == "bb_bandwidth":
                value = _normalize_bb_bandwidth(value)

            conditions.append({
                "field": field,
                "op": op,
                "value": value,
                "source": "validator_structured",
                "desc": reason or f"{field} {op} {value}",
            })
            raw_parts.append(reason or f"{field} {op} {value}")
        
        if conditions:
            # Determine suggestion type from the conditions
            stype = "validator_structured"
            re_setup = validator_response.get("re_entry_setup", "")
            re_dir = validator_response.get("re_entry_direction", "")
            est_candles = validator_response.get("estimated_candles_to_entry")
            price_target = validator_response.get("price_target_entry")

            # Price target is context/validation only — NOT a blocking condition
            # It tells the validator "price was predicted at this level" when the cycle re-fires
            # Adding it as a hard condition would block valid setups that are 5-10 pips off target

            watches.append({
                "instrument": instrument,
                "suggestion_type": stype,
                "conditions": conditions,
                "all_conditions": conditions,
                "raw_text": "; ".join(raw_parts[:6]),
                "priority": len(conditions),
                "re_entry_setup": re_setup,
                "re_entry_direction": re_dir,
                "re_entry_regime": validator_response.get("re_entry_regime", ""),
                "estimated_candles_to_entry": est_candles,
                "price_target_entry": price_target,
            })
            # ── Merge price-level conditions from reasoning/watch_trigger text ──
            # The LLM's re_entry_conditions array often has generic indicator fields
            # but specific prices (entry zones, invalidation) only appear in the text.
            # Extract and append price conditions so snipes have complete criteria.
            _text_for_prices = (
                str(validator_response.get("reasoning", "")) + "\n"
                + str(validator_response.get("watch_trigger", "")) + "\n"
                + str(validator_response.get("watch_for", ""))
            )
            _existing_fields = {c["field"] for c in conditions}
            # Price zone
            _pz = re.search(
                r'(?:entry|zone|target)\s+(?:at\s+)?(?:approximately\s+)?(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)',
                _text_for_prices, re.IGNORECASE
            )
            if _pz and "price_zone" not in _existing_fields:
                conditions.append({
                    "field": "price_zone", "op": "in",
                    "value": f"{_pz.group(1)}-{_pz.group(2)}",
                    "source": "validator_text",
                    "desc": f"Price entry zone {_pz.group(1)}-{_pz.group(2)}",
                })
                _existing_fields.add("price_zone")
            # Invalidation
            _inv = re.search(
                r'[Ii]nvalidation[:\s]+.*?(?:above|below|closes?\s+(?:above|below))\s+(\d+\.?\d*)',
                _text_for_prices
            )
            if _inv and "invalidation_level" not in _existing_fields:
                conditions.append({
                    "field": "invalidation_level", "op": "<=",
                    "value": float(_inv.group(1)),
                    "source": "validator_text",
                    "desc": f"Invalidation at {_inv.group(1)}",
                })
                _existing_fields.add("invalidation_level")
            # EMA cross below/above — 2026-04-27 BUG1 fix: only add when
            # direction-compatible. Validator's reasoning describes BOTH
            # bullish and bearish scenarios (entry vs invalidation); pulling
            # both into the same watch makes it unfireable.
            if re.search(r'E(?:MA)?21\s+(?:to\s+)?cross(?:es|ing)?\s+(?:back\s+)?below\s+E(?:MA)?55',
                         _text_for_prices, re.IGNORECASE) and "ema_cross_below" not in _existing_fields:
                if _is_dir_compatible("ema_cross_below", "==", "ema21 < ema55"):
                    conditions.append({
                        "field": "ema_cross_below", "op": "==",
                        "value": "ema21 < ema55",
                        "source": "validator_text",
                        "desc": "E21 crosses below E55",
                    })
                    _existing_fields.add("ema_cross_below")
            if re.search(r'E(?:MA)?21\s+(?:to\s+)?cross(?:es|ing)?\s+(?:back\s+)?above\s+E(?:MA)?55',
                         _text_for_prices, re.IGNORECASE) and "ema_cross_above" not in _existing_fields:
                if _is_dir_compatible("ema_cross_above", "==", "ema21 > ema55"):
                    conditions.append({
                        "field": "ema_cross_above", "op": "==",
                        "value": "ema21 > ema55",
                        "source": "validator_text",
                        "desc": "E21 crosses above E55",
                    })
                    _existing_fields.add("ema_cross_above")
            # BB squeeze break
            if re.search(r'BB\s+squeeze\s+(?:to\s+)?(?:BREAK|break|release|expand|resolution)',
                         _text_for_prices, re.IGNORECASE) and "bb_squeeze_break" not in _existing_fields:
                conditions.append({
                    "field": "bb_squeeze_break", "op": "==",
                    "value": True,
                    "source": "validator_text",
                    "desc": "BB squeeze breaks — bands expanding",
                })
                _existing_fields.add("bb_squeeze_break")
            # BB bandwidth threshold (e.g. "bandwidth expands above 0.0055" or "BB width >= 0.0040")
            # 2026-04-27 BUG3 fix: normalize off-scale values (validator sometimes
            # writes 0.15 thinking percentage; raw decimal forex M15 BB width is
            # typically 0.001-0.01).
            _bb_bw = re.search(
                r'(?:BB\s+)?(?:band)?width\s+(?:expands?\s+)?(?:above|>=?|≥)\s*(0\.?\d+)',
                _text_for_prices, re.IGNORECASE
            )
            if _bb_bw and "bb_bandwidth" not in _existing_fields:
                _bb_value = _normalize_bb_bandwidth(float(_bb_bw.group(1)))
                conditions.append({
                    "field": "bb_bandwidth", "op": ">=",
                    "value": _bb_value,
                    "source": "validator_text",
                    "desc": f"BB bandwidth >= {_bb_value}",
                })
                _existing_fields.add("bb_bandwidth")
            # Price below/above specific level (e.g. "break and close below E100 (0.78849)")
            # 2026-04-27 BUG2 fix: only add direction-compatible close conditions.
            # Validator describes invalidation levels in reasoning ("if price closes
            # ABOVE 1.6350, bearish thesis is dead") — without the direction filter,
            # this gets pulled as a positive close > 1.6350 entry condition for a
            # SELL watch, making the watch unfireable.
            _price_below = re.search(
                r'(?:break|close|candles?)\s+(?:and\s+close\s+)?below\s+(?:E\d+\s+)?[(\[]?(\d+\.\d{3,5})',
                _text_for_prices, re.IGNORECASE
            )
            if _price_below and "price_below" not in _existing_fields and "close" not in _existing_fields:
                if _is_dir_compatible("close", "<", float(_price_below.group(1))):
                    conditions.append({
                        "field": "close", "op": "<",
                        "value": float(_price_below.group(1)),
                        "source": "validator_text",
                        "desc": f"Close below {_price_below.group(1)}",
                    })
                    _existing_fields.add("close")
            _price_above = re.search(
                r'(?:break|close|candles?)\s+(?:and\s+close\s+)?above\s+(?:E\d+\s+)?[(\[]?(\d+\.\d{3,5})',
                _text_for_prices, re.IGNORECASE
            )
            if (_price_above
                and "price_above" not in _existing_fields
                and "close" not in _existing_fields
                and _is_dir_compatible("close", ">", float(_price_above.group(1)))):
                conditions.append({
                    "field": "close", "op": ">",
                    "value": float(_price_above.group(1)),
                    "source": "validator_text",
                    "desc": f"Close above {_price_above.group(1)}",
                })
                _existing_fields.add("close")

            # ── Drop any conditions with non-checkable fields ─────────────
            # The LLM sometimes stuffs free text into watch_trigger or other
            # non-measurable fields.  Drop them so only real indicators remain.
            _NON_CHECKABLE = {"watch_trigger", "watch_for", "reasoning", "note"}
            conditions = [c for c in conditions if c.get("field") not in _NON_CHECKABLE]

            # ── Cap conditions at 3-7 ────────────────────────────────────
            # Too many criteria make snipes nearly impossible to trigger.
            # Priority: price/entry fields first, then indicators.
            # 2026-05-01: removed inner `MAX_CONDITIONS = 7` rebinding — it
            # shadowed the module-level constant, causing UnboundLocalError on
            # line ~1135 when PRIORITY 2 (text fallback) ran. Use module global.
            if len(conditions) > MAX_CONDITIONS:
                _priority_fields = {
                    "close", "price_zone", "price_above", "price_below",
                    "invalidation_level", "close_vs_ema",
                    "ema_cross_below", "ema_cross_above",
                    "buy_score", "sell_score", "max_score",
                }
                _hi = [c for c in conditions if c.get("field") in _priority_fields]
                _lo = [c for c in conditions if c.get("field") not in _priority_fields]
                conditions = (_hi + _lo)[:MAX_CONDITIONS]
                logger.info("Capped conditions from %d → %d for %s (kept price/entry fields first)",
                           len(_hi) + len(_lo), MAX_CONDITIONS, instrument)

            # Update the watch config with enriched conditions
            watches[-1]["conditions"] = conditions
            watches[-1]["all_conditions"] = conditions

            _eta = f" ETA ~{est_candles} candles" if est_candles else ""
            _pt = f" @ {price_target}" if price_target else ""
            _text_added = len(conditions) - len([c for c in conditions if c.get("source") != "validator_text"])
            logger.info("Parsed %d structured + %d text-extracted conditions for %s (setup=%s dir=%s%s%s)",
                       len(conditions) - _text_added, _text_added, instrument, re_setup, re_dir, _eta, _pt)
            return watches
    
    # ── PRIORITY 2: Legacy regex parsing (fallback) ──
    recommendation = str(validator_response.get("recommendation", ""))
    reasoning = str(validator_response.get("reasoning", ""))
    full_text = recommendation + "\n" + reasoning

    # Also check the snipe_trigger / watch_trigger text from validator
    snipe_trigger = str(validator_response.get("snipe_trigger", ""))
    if snipe_trigger:
        full_text += "\n" + snipe_trigger

    # Current indicators for context
    indicators = (sniper_data or {}).get("indicators", {})

    # Parse numbered suggestions (1. Sniper score ≥12, 2. Confluence ≥35, etc.)
    conditions = []
    suggestion_texts = []

    # ── Price zone extraction (e.g. "entry at 0.9108-0.9113" or "entry zone 111.20-111.50") ──
    price_zone_match = re.search(
        r'(?:entry|zone|target)\s+(?:at\s+)?(?:approximately\s+)?(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)',
        full_text, re.IGNORECASE
    )
    if price_zone_match:
        low, high = price_zone_match.group(1), price_zone_match.group(2)
        conditions.append({
            "field": "price_zone", "op": "in", "value": f"{low}-{high}",
            "source": "validator_text", "desc": f"Price entry zone {low}-{high}",
        })
        suggestion_texts.append(f"Price zone {low}-{high}")

    # ── Invalidation level extraction (e.g. "invalidation: price closes above 0.9130") ──
    invalidation_match = re.search(
        r'[Ii]nvalidation[:\s]+.*?(?:above|below|closes?\s+(?:above|below))\s+(\d+\.?\d*)',
        full_text
    )
    if invalidation_match:
        inv_level = invalidation_match.group(1)
        conditions.append({
            "field": "invalidation_level", "op": "<=", "value": float(inv_level),
            "source": "validator_text", "desc": f"Invalidation at {inv_level}",
        })
        suggestion_texts.append(f"Invalidation {inv_level}")

    # ── Specific EMA cross (e.g. "E21 to cross back below E55") ──
    ema_cross_below_match = re.search(
        r'E(?:MA)?21\s+(?:to\s+)?cross(?:es|ing)?\s+(?:back\s+)?below\s+E(?:MA)?55',
        full_text, re.IGNORECASE
    )
    if ema_cross_below_match:
        conditions.append({
            "field": "ema_cross_below", "op": "==", "value": "ema21 < ema55",
            "source": "validator_text", "desc": "E21 crosses below E55 (bearish fan re-establishment)",
        })
        suggestion_texts.append("E21 < E55")

    ema_cross_above_match = re.search(
        r'E(?:MA)?21\s+(?:to\s+)?cross(?:es|ing)?\s+(?:back\s+)?above\s+E(?:MA)?55',
        full_text, re.IGNORECASE
    )
    if ema_cross_above_match:
        conditions.append({
            "field": "ema_cross_above", "op": "==", "value": "ema21 > ema55",
            "source": "validator_text", "desc": "E21 crosses above E55 (bullish fan re-establishment)",
        })
        suggestion_texts.append("E21 > E55")

    # ── BB squeeze break extraction (e.g. "BB squeeze to BREAK") ──
    bb_squeeze_match = re.search(
        r'BB\s+squeeze\s+(?:to\s+)?(?:BREAK|break|release|expand)',
        full_text, re.IGNORECASE
    )
    if bb_squeeze_match:
        conditions.append({
            "field": "bb_squeeze_break", "op": "==", "value": True,
            "source": "validator_text", "desc": "BB squeeze breaks — bands expanding",
        })
        suggestion_texts.append("BB squeeze break")

    # ── BB bandwidth threshold (e.g. "bandwidth >= 0.00350") ──
    bb_bw_match = re.search(
        r'(?:bb|bandwidth|BB\s+width)\s*[≥>=]+\s*(0\.?\d+)',
        full_text, re.IGNORECASE
    )
    if bb_bw_match:
        bw_val = bb_bw_match.group(1)
        conditions.append({
            "field": "bb_bandwidth", "op": ">=", "value": float(bw_val),
            "source": "validator_text", "desc": f"BB bandwidth >= {bw_val}",
        })
        suggestion_texts.append(f"BB bandwidth >= {bw_val}")

    # ── Chart-read style patterns (added 2026-05-01) ─────────────────────
    # When the validator returns a "CHART READ:" analytical response without
    # a structured JSON block, extract conditions from the prose. Captures
    # phrases like "first cross is about to happen", "Phase 1 / Phase 2",
    # "BBs are TIGHT/COMPRESSED", "potential bearish fan flip",
    # inline EMA values "E21 (1.17464)".

    # Direction inference from "potential bullish/bearish fan flip" phrases
    # (only set if structured direction wasn't already established)
    if not _watch_dir:
        bear_flip = re.search(r'(?:potential|forming|developing)\s+bearish\s+(?:fan\s+)?(?:flip|reversal|cross)',
                              full_text, re.IGNORECASE)
        bull_flip = re.search(r'(?:potential|forming|developing)\s+bullish\s+(?:fan\s+)?(?:flip|reversal|cross)',
                              full_text, re.IGNORECASE)
        if bear_flip and not bull_flip:
            _watch_dir = "sell"
            suggestion_texts.append("dir=SELL inferred from 'bearish fan flip'")
        elif bull_flip and not bear_flip:
            _watch_dir = "buy"
            suggestion_texts.append("dir=BUY inferred from 'bullish fan flip'")

    # "First cross is about to happen" + bearish context → ema_cross_below
    first_cross = re.search(r'first\s+cross\s+(?:is\s+)?(?:about\s+to\s+happen|imminent|forming|developing)',
                            full_text, re.IGNORECASE)
    if first_cross:
        bear_context = re.search(r'bearish|sell|down|below', full_text, re.IGNORECASE)
        bull_context = re.search(r'bullish|buy|up|above', full_text, re.IGNORECASE)
        # Use direction context already inferred above if available
        if _watch_dir == "sell" or (bear_context and not bull_context):
            if not any(c.get("field") == "ema_cross_below" for c in conditions):
                conditions.append({
                    "field": "ema_cross_below", "op": "==", "value": "ema21 < ema55",
                    "source": "validator_text", "desc": "E21 must cross below E55 (first cross)",
                })
                suggestion_texts.append("E21 < E55 (first cross)")
        elif _watch_dir == "buy" or (bull_context and not bear_context):
            if not any(c.get("field") == "ema_cross_above" for c in conditions):
                conditions.append({
                    "field": "ema_cross_above", "op": "==", "value": "ema21 > ema55",
                    "source": "validator_text", "desc": "E21 must cross above E55 (first cross)",
                })
                suggestion_texts.append("E21 > E55 (first cross)")

    # BB compression ("TIGHT", "COMPRESSED", "squeeze in formation")
    # → require expansion before fire (bb_expanding == True)
    # Match either "BBs are compressed" OR "fan/price/bands [are] compressed/tight"
    # OR "TIGHT and COMPRESSED" anywhere (bare adjective form).
    if re.search(
        r'(?:BB[s]?|bands?|fan)\s+(?:are|is|currently|still|in)\s+(?:tight|compressed|squeezed|squeeze|compression)'
        r'|(?:tight\s+and\s+compressed|compressed\s+and\s+tight)'
        r'|(?:squeeze\s+(?:in\s+)?formation)',
        full_text, re.IGNORECASE):
        if not any(c.get("field") == "bb_expanding" for c in conditions):
            conditions.append({
                "field": "bb_expanding", "op": "==", "value": True,
                "source": "validator_text",
                "desc": "BBs must transition from compressed to expanding",
            })
            suggestion_texts.append("BB compress→expand transition")

    # Inline EMA price levels: "E21 (1.17464)", "E55 = 1.17448",
    # "E21 line (1.17464)", "E100 level = 1.17"
    # Allow ONE optional word (line, level, area, etc.) between EMA and value.
    inline_emas = re.finditer(
        r'E(?:MA)?(\d{2,3})(?:\s+\w+)?\s*[\(=]\s*(\d+\.\d{3,5})\s*\)?',
        full_text, re.IGNORECASE
    )
    _ema_levels = {}
    for m in inline_emas:
        try:
            _ema_levels[int(m.group(1))] = float(m.group(2))
        except (ValueError, TypeError):
            pass
    if _ema_levels and _watch_dir and not any(c.get("field") == "invalidation_level" for c in conditions):
        # SELL invalidates if price closes ABOVE E21 or E55 (whichever is higher)
        # BUY invalidates if price closes BELOW E55 or E100 (whichever is lower)
        if _watch_dir == "sell":
            ref = max([_ema_levels.get(21, 0), _ema_levels.get(55, 0)])
            if ref:
                conditions.append({
                    "field": "invalidation_level", "op": "<=", "value": ref,
                    "source": "validator_text",
                    "desc": f"Invalidation: price stays at/below {ref:.5f} (E21/E55 high)",
                })
                suggestion_texts.append(f"Invalidation @ {ref:.5f}")
        elif _watch_dir == "buy":
            valid_levels = [v for k, v in _ema_levels.items() if k in (55, 100) and v > 0]
            if valid_levels:
                ref = min(valid_levels)
                conditions.append({
                    "field": "invalidation_level", "op": ">=", "value": ref,
                    "source": "validator_text",
                    "desc": f"Invalidation: price stays at/above {ref:.5f} (E55/E100 low)",
                })
                suggestion_texts.append(f"Invalidation @ {ref:.5f}")

    # "Price below fan" / "price above fan" — direction-confirming structure
    if re.search(r'price\s+(?:is\s+)?below\s+(?:the\s+)?fan', full_text, re.IGNORECASE):
        if not any(c.get("field") == "ema_fan_state" for c in conditions):
            conditions.append({
                "field": "ema_fan_state", "op": "in",
                "value": ["bearish_expanding", "expanding", "just_crossed"],
                "source": "validator_text",
                "desc": "Fan must be bearish-ordered with price below",
            })
            suggestion_texts.append("Fan bearish (price below)")
    elif re.search(r'price\s+(?:is\s+)?above\s+(?:the\s+)?fan', full_text, re.IGNORECASE):
        if not any(c.get("field") == "ema_fan_state" for c in conditions):
            conditions.append({
                "field": "ema_fan_state", "op": "in",
                "value": ["bullish_expanding", "expanding", "just_crossed"],
                "source": "validator_text",
                "desc": "Fan must be bullish-ordered with price above",
            })
            suggestion_texts.append("Fan bullish (price above)")

    # Phase markers — "Phase 1" = early formation, "Phase 3" = expansion
    if re.search(r'[Pp]hase\s*1\b|early\s+formation', full_text):
        # Phase 1: cross hasn't happened — require it before firing
        # (already handled by first_cross block above; kept here for explicit phase signaling)
        pass
    if re.search(r'[Pp]hase\s*3\b|full\s+expansion|cascade\s+in\s+progress', full_text):
        if not any(c.get("field") == "ema_velocity" for c in conditions):
            conditions.append({
                "field": "ema_velocity", "op": ">=", "value": 0.003,
                "source": "validator_text",
                "desc": "Fan separating at moderate+ speed (Phase 3)",
            })
            suggestion_texts.append("ema_velocity ≥ 0.003 (Phase 3)")

    # Sniper threshold conditions
    sniper_match = re.findall(r'[Ss]niper\s+(?:score\s+)?[≥>=]+\s*(\d+)', full_text)
    for val in sniper_match:
        conditions.append({
            "field": "max_score", "op": ">=", "value": int(val),
            "source": "sniper", "desc": f"Sniper score ≥ {val}",
        })
        suggestion_texts.append(f"Sniper ≥{val}")
    
    # Confluence threshold
    conf_match = re.findall(r'[Cc]onfluence\s+[≥>=]+\s*(\d+)', full_text)
    for val in conf_match:
        conditions.append({
            "field": "total_score", "op": ">=", "value": int(val),
            "source": "confluence", "desc": f"Confluence ≥ {val}",
        })
        suggestion_texts.append(f"Confluence ≥{val}")
    
    # RSI slope positive/negative
    if re.search(r'RSI\s+slope.*positive', full_text, re.IGNORECASE):
        conditions.append({
            "field": "rsi_slope", "op": ">", "value": 0,
            "source": "indicator", "desc": "RSI slope positive",
        })
        suggestion_texts.append("RSI slope +")
    
    # Pullback to EMA
    ema_match = re.findall(r'pullback\s+to\s+(?:support\s+)?(?:\()?EMA(\d+)(?:\s+at\s+([\d.]+))?', full_text, re.IGNORECASE)
    for ema_period, ema_val in ema_match:
        field = f"ema_{ema_period}"
        conditions.append({
            "field": "close_vs_ema", "op": "<=", "value": float(ema_period),
            "ema_field": field,
            "source": "indicator", "desc": f"Pullback to EMA{ema_period}",
        })
        suggestion_texts.append(f"Pullback EMA{ema_period}")
    
    # Reversal patterns
    if re.search(r'reversal\s+pattern', full_text, re.IGNORECASE):
        conditions.append({
            "field": "has_reversal_pattern", "op": "==", "value": True,
            "source": "pattern", "desc": "Reversal candle pattern detected",
        })
        suggestion_texts.append("Reversal pattern")
    
    # Bullish/bearish sentiment alignment
    if re.search(r'bullish\s+(?:intelligence\s+)?sentiment', full_text, re.IGNORECASE):
        conditions.append({
            "field": "sentiment_aligned", "op": "==", "value": "bullish",
            "source": "intelligence", "desc": "Bullish sentiment alignment",
        })
        suggestion_texts.append("Bullish sentiment")
    elif re.search(r'bearish\s+(?:intelligence\s+)?sentiment', full_text, re.IGNORECASE):
        conditions.append({
            "field": "sentiment_aligned", "op": "==", "value": "bearish",
            "source": "intelligence", "desc": "Bearish sentiment alignment",
        })
        suggestion_texts.append("Bearish sentiment")
    
    # Threshold upgrade suggestion (e.g., "switch to t16")
    thresh_match = re.findall(r'(?:switch|use|try)\s+(?:to\s+)?t(\d+)', full_text, re.IGNORECASE)
    for val in thresh_match:
        conditions.append({
            "field": "max_score", "op": ">=", "value": int(val),
            "source": "sniper", "desc": f"Sniper score ≥ {val} (threshold upgrade)",
        })
        suggestion_texts.append(f"Threshold upgrade t{val}")
    
    # Stochastic exit overbought/oversold
    if re.search(r'[Ss]tochastic.*exit\s+overbought|Stoch.*K\s*<\s*80', full_text):
        conditions.append({
            "field": "stoch_k", "op": "<", "value": 80,
            "source": "indicator", "desc": "Stochastic exits overbought (<80)",
        })
        suggestion_texts.append("Stoch exit OB")
    elif re.search(r'[Ss]tochastic.*exit\s+oversold|Stoch.*K\s*>\s*20', full_text):
        conditions.append({
            "field": "stoch_k", "op": ">", "value": 20,
            "source": "indicator", "desc": "Stochastic exits oversold (>20)",
        })
        suggestion_texts.append("Stoch exit OS")
    
    # ── Extract thesis-specific conditions from validator reasoning ──
    # Instead of generic fallback conditions, parse the actual rejection reasons
    # to create snipe conditions that address what went wrong.

    # Fan state contradictions → watch for fan to change
    if re.search(r'fan\s+(?:is\s+)?(?:still\s+)?expanding|fan\s+accelerating|trend\s+(?:still\s+)?strengthening', full_text, re.IGNORECASE):
        if not any(c["field"] == "ema_fan_state" for c in conditions):
            conditions.append({
                "field": "ema_fan_state", "op": "in",
                "value": ["peaked", "decelerating", "contracting"],
                "source": "ema_narrative",
                "reason": "Fan was still expanding — wait for trend to exhaust before counter-trend entry",
            })
            suggestion_texts.append("Fan must exhaust")

    elif re.search(r'fan\s+(?:is\s+)?(?:peaked|contracting|fading)|trend\s+(?:losing|fading|dying)', full_text, re.IGNORECASE):
        # Fan peaked/contracting = RETRACEMENT SETUP — set snipe at the entry zone.
        # DO NOT wait for fan to re-expand (that's the middle of the move).
        # Watch for: price at E55 mid-retrace OR price at E100 deep retrace + reversal signal.
        if not any(c["field"] in ("ema_price_near_e100", "close_vs_ema") for c in conditions):
            conditions.append({
                "field": "ema_price_near_e100", "op": "==", "value": True,
                "source": "ema_narrative",
                "reason": "Fan peaked/contracting — watch for price to reach E100 deep retrace zone (primary entry)",
            })
            conditions.append({
                "field": "ema_fan_state", "op": "in",
                "value": ["peaked", "contracting"],
                "source": "ema_narrative",
                "reason": "Fan still ordered (peaked/contracting) = trend alive, just retracing",
            })
            suggestion_texts.append("Price at E100 retracement zone")

    # Momentum exhaustion needed
    if re.search(r'no\s+(?:momentum\s+)?exhaustion|momentum\s+(?:not\s+)?exhausted|awaiting\s+exhaustion', full_text, re.IGNORECASE):
        if not any(c["field"] == "momentum_exhausted" for c in conditions):
            conditions.append({
                "field": "momentum_exhausted", "op": "==", "value": True,
                "source": "market_story",
                "reason": "Momentum wasn't exhausted — need RSI+Stoch+MACD all confirming exhaustion",
            })
            suggestion_texts.append("Momentum exhaustion")

    # E100 interaction issues
    if re.search(r'E100\s+(?:is\s+)?broken|structural\s+level\s+lost', full_text, re.IGNORECASE):
        if not any(c["field"] == "e100_interaction" for c in conditions):
            conditions.append({
                "field": "e100_interaction", "op": "in",
                "value": ["support", "strong_support", "resistance", "strong_resistance"],
                "source": "market_story",
                "reason": "E100 was broken — wait for price to reclaim and show support/resistance",
            })
            suggestion_texts.append("E100 must hold")

    elif re.search(r'E100.*(?:not\s+tested|distant|approaching|no\s+rejection)', full_text, re.IGNORECASE):
        if not any(c["field"] == "e100_interaction" for c in conditions):
            conditions.append({
                "field": "e100_interaction", "op": "in",
                "value": ["support", "strong_support", "resistance", "strong_resistance", "testing"],
                "source": "market_story",
                "reason": "E100 wasn't being tested — wait for price to reach and interact with E100",
            })
            suggestion_texts.append("E100 interaction needed")

    # Directional conflict / chart pattern contradiction
    if re.search(r'directional\s+conflict|(?:double\s+top|head\s+and\s+shoulders).*bearish.*bullish|bearish.*(?:double\s+top|head\s+and\s+shoulders)', full_text, re.IGNORECASE):
        if not any(c["field"] == "has_reversal_pattern" for c in conditions):
            conditions.append({
                "field": "has_reversal_pattern", "op": "==", "value": True,
                "source": "pattern",
                "reason": "Bearish chart patterns contradicted bullish signal — wait for pattern to resolve or confirm",
            })
            suggestion_texts.append("Chart pattern resolution")

    # Session/timing issues
    if re.search(r'off.?hours|low\s+liquidity|asian\s+session.*risk|thin\s+market', full_text, re.IGNORECASE):
        if not any(c["field"] == "session" for c in conditions):
            conditions.append({
                "field": "session", "op": "in",
                "value": ["london", "new_york", "NY_Overlap"],
                "source": "context",
                "reason": "Off-hours or thin market — wait for liquid session",
            })
            suggestion_texts.append("Liquid session needed")

    # Velocity too low
    if re.search(r'velocity.*(?:low|slow|weak|fakeout)|slow.*velocity', full_text, re.IGNORECASE):
        if not any(c["field"] == "ema_velocity" for c in conditions):
            conditions.append({
                "field": "ema_velocity", "op": ">=", "value": 0.003,
                "source": "ema_narrative",
                "reason": "EMA velocity was too slow — wait for meaningful trend speed",
            })
            suggestion_texts.append("Velocity must pick up")

    # Timing mismatch / stale setup
    if re.search(r'timing\s+mismatch|stale|expired|bars\s+ago|too\s+late', full_text, re.IGNORECASE):
        if not any(c["field"] == "story_entry_type" for c in conditions):
            # Need a fresh thesis, not a stale one
            conditions.append({
                "field": "story_has_opportunity", "op": "==", "value": True,
                "source": "market_story",
                "reason": "Setup was stale/expired — wait for a fresh thesis from the scout",
            })
            suggestion_texts.append("Fresh thesis needed")

    # ── Derive conditions from validator checklist false items ──
    # Better than generic conditions — the checklist tells us exactly what failed.
    if not conditions:
        # Try to pull the checklist from validator_response (passed as sniper_data or outer scope)
        # This is populated as validator_response.get("checklist", {})
        pass  # handled below after this block

    if not conditions:
        # ── CHECKLIST DERIVATION: map false checklist items to measurable conditions ──
        # This fires when: (a) re_entry_conditions was empty/invalid AND (b) regex found nothing
        checklist = {}
        # checklist is on validator_response but we only have sniper_data here. 
        # We'll look for it in the full_text (validator puts checklist items in reasoning sometimes)
        # Map of checklist key → condition
        # ── Valid ema_fan_state values (canonical, from ema_separation.py) ─────
        # expanding | contracting | stable | just_crossed | peaked | forming | decelerating
        # NEVER use: bullish_expanding, bearish_expanding, bearish_accelerating, etc.
        CHECKLIST_TO_CONDITION = {
            "ema_cross": {
                "field": "ema_fan_state", "op": "in",
                "value": ["just_crossed", "expanding", "accelerating"],
                "source": "ema_narrative",
                "reason": "EMA cross not yet confirmed — wait for E21×E55 cross and fan to open",
            },
            "fan_opening": {
                "field": "ema_fan_state", "op": "in",
                "value": ["expanding", "accelerating", "just_crossed"],
                "source": "ema_narrative",
                "reason": "Fan not yet opening — wait for EMAs to begin directional separation",
            },
            "fan_accelerating": {
                "field": "ema_velocity", "op": ">=", "value": 0.003,
                "source": "ema_narrative",
                "reason": "Fan velocity too slow — need 0.003%+/bar to confirm real expansion",
            },
            "bb_expanding": {
                "field": "bb_expanding", "op": "==", "value": True,
                "source": "bollinger",
                "reason": "Bollinger Bands not yet expanding — no energy in the move",
            },
            "momentum_candles": {
                "field": "momentum_candles", "op": "==", "value": True,
                "source": "candlestick",
                "reason": "No momentum candles — need strong directional bodies before entry",
            },
            "rsi_recovering": {
                "field": "rsi", "op": ">=", "value": 35,
                "source": "indicator",
                "reason": "RSI not yet recovering from extreme — wait for momentum shift",
            },
            "candles_away": {
                # For retracement strategy: candles AT E100 is the entry, not "away from" it.
                # This condition fires when price reaches E100 zone = that's where we want to enter.
                "field": "ema_price_near_e100", "op": "==", "value": True,
                "source": "ema_narrative",
                "reason": "Waiting for price to reach E100 retracement entry zone",
            },
            "correct_side": {
                # Fan ordered + price at retracement zone = correct side for entry.
                # Do not require expansion — peaked/contracting ordered fan is the setup.
                "field": "ema_fan_state", "op": "in",
                "value": ["peaked", "contracting", "bullish_expanding", "bearish_expanding", "expanding"],
                "source": "ema_narrative",
                "reason": "Fan ordered (peaked/contracting/expanding) = structural alignment for entry",
            },
        }

        # Parse which items were mentioned as false/missing in the reasoning text
        missing_keys = []
        PATTERN_MAP = {
            "ema_cross": r'(?:no|not yet|missing)\s+(?:ema\s+)?cross|cross.*not.*confirmed|e21.*e55.*not',
            "fan_opening": r'fan.*not.*open|fan.*still.*neutral|fan.*mixed|fan.*tangles?|ema.*tangle',
            "fan_accelerating": r'velocity.*(?:low|slow|insufficient|too.*slow)|slow.*velocity|fan.*not.*accelerat',
            "bb_expanding": r'bb.*(?:flat|not.*expand|zero.*delta|no.*expand|tight)|bands.*(?:flat|not.*widen)',
            "momentum_candles": r'no.*momentum|momentum.*(?:candles?|missing|absent)|(?:doji|indecision|choppy).*candles?',
            "rsi_recovering": r'rsi.*(?:still|stuck|extreme|not.*recover)|rsi.*not.*recover',
            "candles_away": r'candles?.*(?:close|near|on).*e100|price.*(?:on|near|hugging).*e100|no.*separation',
        }
        for key, pattern in PATTERN_MAP.items():
            if re.search(pattern, full_text, re.IGNORECASE):
                missing_keys.append(key)

        # If no specific keys found from the reasoning text — do NOT guess.
        # Better to create no watch than a generic 2-condition watch that fires on everything.
        # The validator prompt requires re_entry_conditions on every non-TRADE_NOW verdict.
        # If conditions are absent, it means the validator didn't provide enough context — skip.
        if not missing_keys:
            logger.info("[WATCH] %s: no parseable conditions from validator reasoning — skipping watch creation", instrument)
            return []

        # 2026-05-03: direction-aware substitution for "ema_cross" and
        # "fan_opening" keys. Scout has pair-specific evaluators
        # (ema_cross_below/above with value="ema21 < ema55" or "ema21 > ema55")
        # and a directional fan check (ema_fan_direction). The generic
        # ema_fan_state check fires on ANY of the three EMA pairs crossing,
        # which doesn't match what the validator described. When we know the
        # watch direction, emit the direction-specific check that aligns with
        # what the validator's chart-read meant by "wait for E21×E55 cross".
        for key in missing_keys:
            if key == "ema_cross" and _watch_dir in ("buy", "sell"):
                if _watch_dir == "sell":
                    conditions.append({
                        "field": "ema_cross_below", "op": "==",
                        "value": "ema21 < ema55",
                        "source": "ema_narrative",
                        "reason": "EMA cross not yet confirmed — wait for E21 to cross below E55",
                    })
                else:  # buy
                    conditions.append({
                        "field": "ema_cross_above", "op": "==",
                        "value": "ema21 > ema55",
                        "source": "ema_narrative",
                        "reason": "EMA cross not yet confirmed — wait for E21 to cross above E55",
                    })
                suggestion_texts.append("Specific E21×E55 cross required for direction")
                continue
            if key == "fan_opening" and _watch_dir in ("buy", "sell"):
                conditions.append({
                    "field": "ema_fan_direction", "op": "==",
                    "value": "bullish" if _watch_dir == "buy" else "bearish",
                    "source": "ema_narrative",
                    "reason": f"Fan must order in {('bullish' if _watch_dir == 'buy' else 'bearish')} direction (E21>E55>E100 for buy, E21<E55<E100 for sell)",
                })
                suggestion_texts.append(f"Directional fan ordering required ({_watch_dir})")
                continue
            cond = CHECKLIST_TO_CONDITION.get(key)
            if cond:
                conditions.append(dict(cond))
                suggestion_texts.append(cond["reason"][:60])

        if conditions:
            logger.info("Derived %d conditions from checklist analysis for %s", len(conditions), instrument)
    
    if not conditions:
        logger.info("No parseable conditions from validator suggestions")
        return []
    
    # Determine suggestion type
    if any("threshold_upgrade" in c.get("desc", "").lower() for c in conditions):
        stype = "threshold_upgrade"
    elif any("pullback" in c.get("desc", "").lower() for c in conditions):
        stype = "pullback_wait"
    elif any("rsi" in c.get("desc", "").lower() for c in conditions):
        stype = "momentum_reversal"
    else:
        stype = "multi_condition"
    
    # Filter out intelligence-based conditions (can't check without LLM)
    # ── Sanitize conditions before storage ─────────────────────────────────
    # Normalise any phantom ema_fan_state values the LLM may have hallucinated.
    # ema_separation.py only ever produces the CANONICAL set below; any other
    # label is an LLM invention that will never match at check time.
    _CANONICAL_FAN_STATES = {
        'expanding', 'contracting', 'stable', 'just_crossed',
        'peaked', 'forming', 'decelerating',
    }
    _FAN_SANITIZE = {
        'bearish_expanding':    'expanding',
        'bullish_expanding':    'expanding',
        'bearish_accelerating': 'expanding',
        'bullish_accelerating': 'expanding',
        'accelerating':         'expanding',  # also not produced; map to nearest canonical
        'bearish_contracting':  'contracting',
        'bullish_contracting':  'contracting',
        'bearish_peaked':       'peaked',
        'bullish_peaked':       'peaked',
        'bearish_just_crossed': 'just_crossed',
        'bullish_just_crossed': 'just_crossed',
    }
    for _c in conditions:
        if _c.get('field') == 'ema_fan_state' and isinstance(_c.get('value'), list):
            _orig = _c['value']
            _c['value'] = [_FAN_SANITIZE.get(v, v) for v in _orig]
            _c['value'] = list(dict.fromkeys(_c['value']))  # deduplicate while preserving order
            unknown = [v for v in _c['value'] if v not in _CANONICAL_FAN_STATES]
            if unknown:
                logger.warning(
                    "[WATCH] %s: ema_fan_state condition had unrecognised values after sanitize: %s — dropping them",
                    instrument, unknown,
                )
                _c['value'] = [v for v in _c['value'] if v in _CANONICAL_FAN_STATES]
            if _orig != _c['value']:
                logger.debug("[WATCH] %s: fan_state sanitized %s → %s", instrument, _orig, _c['value'])
        # Normalise ema_velocity: negative target = bearish intent → make positive (always abs)
        if _c.get('field') == 'ema_velocity':
            _tgt = _c.get('value')
            if isinstance(_tgt, (int, float)) and _tgt < 0:
                _c['value'] = abs(_tgt)
                _c['op'] = '>='  # always use >= with positive target
                logger.debug("[WATCH] %s: ema_velocity target %s → %s >= %s", instrument, _tgt, _c['field'], _c['value'])

    checkable = [c for c in conditions if c["source"] != "intelligence"]

    # ── Cap conditions at 3-7 (same logic as structured path) ──
    if len(checkable) > MAX_CONDITIONS:
        _priority_fields = {
            "close", "price_zone", "price_above", "price_below",
            "invalidation_level", "close_vs_ema",
            "ema_cross_below", "ema_cross_above",
            "buy_score", "sell_score", "max_score",
        }
        _hi = [c for c in checkable if c.get("field") in _priority_fields]
        _lo = [c for c in checkable if c.get("field") not in _priority_fields]
        checkable = (_hi + _lo)[:MAX_CONDITIONS]
        logger.info("Legacy path: capped conditions %d → %d for %s",
                    len(_hi) + len(_lo), MAX_CONDITIONS, instrument)

    if checkable:
        # Build raw_text from condition reasons (human-readable) when available,
        # falling back to suggestion_texts (short labels) for display
        reason_texts = [c.get("reason", c.get("desc", "")) for c in checkable if c.get("reason") or c.get("desc")]
        raw_text = "; ".join(reason_texts) if reason_texts else "; ".join(suggestion_texts)

        watches.append({
            "instrument": instrument,
            "suggestion_type": stype,
            "conditions": checkable,
            "all_conditions": conditions,  # includes non-checkable for display
            "raw_text": raw_text,
            "priority": len(checkable),  # more conditions = higher priority
        })
    
    return watches


# ---------------------------------------------------------------------------
# Create watch tasks
# ---------------------------------------------------------------------------

def _dedup_result(conn, existing_id: int, instrument: str,
                  similarity: float, match_type: str) -> dict:
    """Build a dedup result dict with the existing watch's progress info.

    Returned instead of a bare int so the caller (trading_cycle) can tell the
    user the snipe already exists and how close it is to triggering.
    The dict is truthy (so ``if watch_id:`` still works) and has an ``id``
    key so ``watch_id["id"]`` or ``int(watch_id)`` patterns keep working.
    """
    row = conn.execute(
        """SELECT conditions_met_count, conditions_total_count, peak_progress,
                  check_count, created_at, status
           FROM watch_suggestions WHERE id=?""",
        (existing_id,)
    ).fetchone()
    met = row[0] or 0 if row else 0
    total = row[1] or 0 if row else 0
    peak = row[2] or 0 if row else 0
    checks = row[3] or 0 if row else 0
    created = row[4] or "" if row else ""
    pct = (met / total * 100) if total > 0 else 0

    return {
        "id": existing_id,
        "dedup": True,
        "match_type": match_type,       # "exact" or "similar"
        "similarity": round(similarity * 100),
        "instrument": instrument,
        "criteria_met": met,
        "criteria_total": total,
        "criteria_pct": round(pct, 1),
        "peak_progress": round(peak * 100, 1),
        "check_count": checks,
        "created_at": created,
    }


def _normalize_direction(d) -> str:
    """Normalize a direction string to 'buy'/'sell' or empty.

    Accepts case-insensitive synonyms used across the trading system:
        - bullish/long/buy → 'buy'
        - bearish/short/sell → 'sell'
        - anything else → ''

    Added 2026-04-27 to fix: validator/scout were emitting 'bullish'/'bearish'
    direction strings (the fan/sniper convention) but watch_suggestions.direction
    column expects 'buy'/'sell'. Without this normalization, the lower() chain
    in create_watch was filtering valid 'bullish'→empty, leaving NULL direction
    in DB. Rooted out 4 stuck watches (2200, 2204, 2205, 2208) that came in as
    WATCH verdicts but couldn't be triggered because direction column was NULL.
    """
    d = (d or "").lower().strip()
    if d in ("buy", "long", "bullish"):
        return "buy"
    if d in ("sell", "short", "bearish"):
        return "sell"
    return ""


def _infer_direction_from_conditions(conditions: list) -> str:
    """Infer 'buy' or 'sell' from structured conditions when validator didn't
    explicitly provide a direction.

    Uses unambiguous structured fields only:
        - close op </<= with a value = "close below X" confirms SELL trigger
        - close op >/>= with a value = "close above X" confirms BUY trigger
        - ema_cross_below field = bearish cross = SELL
        - ema_cross_above field = bullish cross = BUY
        - close_vs_ema op </<= = price below EMA = SELL context
        - close_vs_ema op >/>= = price above EMA = BUY context
        - rsi op <= = bearish RSI exhaustion → SELL context (weak)
        - rsi op >= = bullish RSI recovery → BUY context (weak)

    Deliberately IGNORED (ambiguous semantics across validator outputs):
        - invalidation_level — validator uses two incompatible conventions:
          some watches use op `<=` to mean "SL is at this level" (value
          constraint), others use op `>` to mean "thesis invalidated if
          price > X" (logical check). Cannot infer direction from it.

    Returns 'buy' / 'sell' / '' (undetermined).

    Added 2026-04-24 to prevent watch-direction-mislabeling bugs like watch
    2160 USD_CHF where validator prose said "bullish continuation" but rules
    were unambiguously SELL (close below, price below E100).
    """
    sell_votes = 0
    buy_votes = 0
    for c in conditions:
        try:
            field = (c.get("field") or "").lower()
            op = c.get("op") or ""
        except AttributeError:
            continue
        if field == "close":
            if op in ("<", "<="):
                sell_votes += 2
            elif op in (">", ">="):
                buy_votes += 2
        elif field == "ema_cross_below":
            sell_votes += 2
        elif field == "ema_cross_above":
            buy_votes += 2
        elif field == "close_vs_ema":
            if op in ("<", "<="):
                sell_votes += 2
            elif op in (">", ">="):
                buy_votes += 2
        elif field == "rsi":
            if op in ("<", "<="):
                sell_votes += 1
            elif op in (">", ">="):
                buy_votes += 1
    if sell_votes > buy_votes and sell_votes >= 2:
        return "sell"
    if buy_votes > sell_votes and buy_votes >= 2:
        return "buy"
    return ""


def create_watch(cycle_id: str, instrument: str, watch_config: dict,
                 validator_response: dict, workspace_task_id: int = None,
                 cycle_context: dict = None, user_id: int = None) -> Optional[int]:
    """Create a watch_suggestion row and optionally a workspace_task.

    Returns the watch ID or None on failure.
    Deduplicates: cancels existing active watches for the same pair before creating new one.
    """
    _ensure_tables()
    now = datetime.now(timezone.utc)
    
    # ── Deduplication: hash + similarity based ───────────────────────────────
    # Multiple watches on the same pair with DIFFERENT setups all coexist —
    # a BUY and a SELL, or two BUYs waiting for genuinely different setups.
    #
    # Layer 1 — Exact hash match: identical conditions → return existing ID.
    # Layer 2 — Similarity match: ≥70% Jaccard similarity on field|op sigs
    #           with same direction → refresh the most similar existing watch
    #           instead of creating a near-duplicate.
    #
    # This prevents the validator from stacking 7 copies of "BBs must expand +
    # fan must separate + RSI below 45" with slightly different wording each
    # scan cycle, while still allowing genuinely different setups to coexist.
    incoming_type = watch_config.get("suggestion_type", "unknown")
    incoming_conditions = watch_config.get("conditions", [])
    ctx_for_hash   = cycle_context or {}
    incoming_dir   = watch_config.get("re_entry_direction") or ctx_for_hash.get("direction", "unknown")
    incoming_hash  = _compute_conditions_hash(incoming_conditions, instrument, incoming_dir)
    incoming_sig   = _conditions_signature(incoming_conditions, watch_config, ctx_for_hash)

    # 0.95 Jaccard on the identity-aware sig means "literally the same trade":
    # same direction, same setup name, identical conditions with same values.
    # Anything less — different setup_name, different threshold, different
    # condition set — falls through and creates a fresh watch.
    SIMILARITY_THRESHOLD = tc_get("watch.thesis_similarity", 0.95)

    try:
        conn_dedup = get_trading_forex()
        # Find all active watches on this pair with the same suggestion_type
        dup_row = conn_dedup.execute(
            "SELECT id, context, conditions FROM watch_suggestions "
            "WHERE instrument=? AND status='watching' AND suggestion_type=?",
            (instrument, incoming_type)
        ).fetchall()

        best_sim_id = None
        best_sim_score = 0.0

        for dup_id, dup_ctx_raw, dup_conds_raw in dup_row:
            try:
                dup_ctx   = json.loads(dup_ctx_raw or "{}")
                dup_dir   = (dup_ctx.get("re_entry_direction") or dup_ctx.get("direction", "unknown")).upper()
                dup_conds = json.loads(dup_conds_raw or "[]")
                dup_hash  = _compute_conditions_hash(dup_conds, instrument, dup_dir)
            except Exception:
                dup_hash = None
                dup_dir = "unknown"
                dup_conds = []

            # Layer 1: exact hash match
            if dup_hash and dup_hash == incoming_hash:
                conn_dedup.execute(
                    "UPDATE watch_suggestions SET last_checked_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), dup_id)
                )
                conn_dedup.commit()
                logger.info("[watch] Dedup EXACT: %s hash=%s already active as #%d — skipping",
                            instrument, incoming_hash, dup_id)
                return _dedup_result(conn_dedup, dup_id, instrument, 1.0, "exact")

            # Layer 2: similarity check (same direction only)
            if dup_dir.upper() == incoming_dir.upper():
                # Pass dup_ctx as the cycle_context — that's where setup_name lives
                # for stored watches (validator persists it into the context column).
                dup_sig = _conditions_signature(dup_conds, dup_ctx, dup_ctx)
                sim = _jaccard_similarity(incoming_sig, dup_sig)
                if sim > best_sim_score:
                    best_sim_score = sim
                    best_sim_id = dup_id

        # If best similarity exceeds threshold, EITHER refresh the existing watch OR
        # supersede it if it's a stale never-developed setup (Tim's call 2026-05-07).
        # A snipe that never crossed peak_progress threshold has shown the chart never
        # moved toward its predictions — replace it with the fresher validator output
        # instead of letting it block new snipes indefinitely.
        if best_sim_id and best_sim_score >= SIMILARITY_THRESHOLD:
            _stale_peak_threshold = float(tc_get("watch.stale_replace_peak_threshold", 0.70))
            try:
                _stale_row = conn_dedup.execute(
                    "SELECT peak_progress, status FROM watch_suggestions WHERE id=?",
                    (best_sim_id,)
                ).fetchone()
            except Exception:
                _stale_row = None
            _stale_peak = float(_stale_row[0] or 0) if _stale_row else 0.0
            _stale_status = (_stale_row[1] if _stale_row else "") or ""

            if _stale_status == "watching" and _stale_peak < _stale_peak_threshold:
                # Stale similar snipe — supersede and create fresh
                conn_dedup.execute(
                    "UPDATE watch_suggestions SET status='superseded' WHERE id=?",
                    (best_sim_id,)
                )
                conn_dedup.commit()
                logger.info(
                    "[watch] Dedup STALE-REPLACE: %s #%d peak=%.0f%% (< %.0f%% threshold) — superseded, creating fresh from validator output",
                    instrument, best_sim_id, _stale_peak * 100, _stale_peak_threshold * 100
                )
                # Fall through to create new watch
            else:
                # Existing similar snipe is healthy (peak ≥ threshold) or already past 'watching' — keep it
                conn_dedup.execute(
                    "UPDATE watch_suggestions SET last_checked_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), best_sim_id)
                )
                conn_dedup.commit()
                logger.info(
                    "[watch] Dedup SIMILAR: %s new watch is %.0f%% similar to #%d (peak=%.0f%%, status=%s) — skipping create",
                    instrument, best_sim_score * 100, best_sim_id, _stale_peak * 100, _stale_status
                )
                return _dedup_result(conn_dedup, best_sim_id, instrument, best_sim_score, "similar")

        # No duplicate found — fall through to create new watch
        logger.debug("[watch] %s conditions_hash=%s is new (best_sim=%.0f%%) — creating watch",
                     instrument, incoming_hash, best_sim_score * 100)
    except Exception as e:
        logger.warning("Dedup check failed: %s", e)
    wcfg = _watch_config()
    ttl = wcfg["ttl_hours"]
    if ttl == 0:
        # User chose ∞ (infinite) via dashboard slider — no expiry
        expires = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    elif ttl < 0:
        # Invalid value — use 24h default
        expires = now + timedelta(hours=24)
    else:
        # User chose specific TTL (e.g., 8h, 24h, 168h)
        expires = now + timedelta(hours=ttl)
    
    # Build human-readable context for the dashboard
    ctx = cycle_context or {}
    validator_reasoning = ctx.get("validator_reasoning", "")

    # The setup_story should be the validator's natural language reasoning —
    # that's what reads like a trader explaining their thinking.
    # Fall back to the generic formatted story only if validator reasoning is empty.
    setup_story = validator_reasoning or ctx.get("setup_story", "")

    display_context = json.dumps({
        "setup_story": setup_story,
        "direction": ctx.get("direction", ""),
        "sniper_buy": ctx.get("sniper_buy", 0),
        "sniper_sell": ctx.get("sniper_sell", 0),
        "sniper_threshold": ctx.get("sniper_threshold", 12),
        "confluence_score": ctx.get("confluence_score", 0),
        "confluence_min": ctx.get("confluence_min", 32),
        "db_win_rate": ctx.get("db_win_rate"),
        "db_profit_factor": ctx.get("db_profit_factor"),
        "db_trade_count": ctx.get("db_trade_count"),
        "db_setup": ctx.get("db_setup", ""),
        "key_signals": ctx.get("key_signals", []),
        "validator_reasoning": validator_reasoning,
        # Structured re-entry data from Validator
        "re_entry_setup": watch_config.get("re_entry_setup", ""),
        "re_entry_direction": watch_config.get("re_entry_direction", ""),
        "re_entry_regime": watch_config.get("re_entry_regime", ""),
        # Validator forward prediction
        "estimated_candles_to_entry": watch_config.get("estimated_candles_to_entry") or ctx.get("estimated_candles_to_entry"),
        "price_target_entry": watch_config.get("price_target_entry") or ctx.get("price_target_entry"),
        # Setup classification metadata
        "setup_id": ctx.get("setup_id", ""),
        "setup_name": ctx.get("setup_name", ""),
        "classified_setups": ctx.get("classified_setups", []),
        # Scout alert linkage — persisted so record_outcome can write back
        "scout_alert_id": ctx.get("scout_alert_id"),
        # Watch manifest / fishing line data
        "watch_manifest": ctx.get("watch_manifest"),
        "watch_trigger": ctx.get("watch_trigger", ""),
        "watch_for": ctx.get("watch_for", ""),
        "confidence_trajectory": ctx.get("confidence_trajectory", "stable"),
    })

    conn = get_trading_forex()
    # Ensure context column exists
    try:
        conn.execute("ALTER TABLE watch_suggestions ADD COLUMN context TEXT DEFAULT '{}'")
    except Exception:
        pass  # column already exists

    # ── Fix 4: Max-2-per-instrument dedup with PROGRESS PROTECTION ──────────
    # Prevents zombie watch accumulation (up to 5 per pair observed in audit).
    # When a 3rd watch would be created for the same instrument, expire the oldest
    # active one (by created_at) before inserting the new one.
    #
    # PROTECTION RULES (prevent killing good snipes):
    #   1. 4-hour lock: snipes < 4 hours old cannot be expired (give them time to develop)
    #   2. High progress preservation: peak_progress >= threshold cannot be expired (close to triggering)
    #   3. Same-thesis merging: if new snipe has same direction + similar conditions,
    #      update the existing one instead of creating a new one
    try:
        _active_rows = conn.execute(
            "SELECT id, created_at, peak_progress, conditions_met_count, conditions_total_count "
            "FROM watch_suggestions WHERE instrument=? AND status='watching' ORDER BY created_at ASC",
            (instrument,)
        ).fetchall()
        if len(_active_rows) >= 2:
            # Find the best candidate to expire (lowest progress, oldest, past 4-hour lock)
            _expired_one = False
            for _ar in _active_rows:
                _ar_id = _ar[0]
                _ar_created = _ar[1]
                _ar_peak = float(_ar[2] or 0)
                _ar_met = int(_ar[3] or 0)
                _ar_total = int(_ar[4] or 1)

                # 4-hour lock: don't expire snipes less than 4 hours old
                _age_ok = True
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    if _ar_created:
                        _created_dt = _safe_isoformat(str(_ar_created))
                        if _created_dt.tzinfo is None:
                            _created_dt = _created_dt.replace(tzinfo=_tz.utc)
                        _age_hours = (_dt.now(_tz.utc) - _created_dt).total_seconds() / 3600
                        _age_ok = _age_hours >= tc_get("watch.age_check_hours", 4.0)
                except Exception:
                    _age_ok = True  # if we can't parse, allow expiry

                # High progress preservation: peak above threshold = keep it alive
                _progress_ok = _ar_peak < tc_get("watch.progress_preserve_threshold", 0.70)

                if _age_ok and _progress_ok:
                    conn.execute(
                        "UPDATE watch_suggestions SET status='expired_dedup' WHERE id=?",
                        (_ar_id,)
                    )
                    conn.commit()
                    logger.info(
                        "[watch] Dedup: %s has %d active watches — expired #%d "
                        "(peak=%.0f%%, %d/%d conditions, age_ok=%s)",
                        instrument, len(_active_rows), _ar_id,
                        _ar_peak * 100, _ar_met, _ar_total, _age_ok
                    )
                    _expired_one = True
                    break
                else:
                    logger.info(
                        "[watch] Dedup: %s watch #%d PROTECTED — peak=%.0f%% age_ok=%s "
                        "(4hr lock or 70%%+ progress)",
                        instrument, _ar_id, _ar_peak * 100, _age_ok
                    )

            if not _expired_one and len(_active_rows) >= 2:
                logger.info(
                    "[watch] Dedup: %s all %d watches protected — allowing 3rd watch as exception",
                    instrument, len(_active_rows)
                )
    except Exception as _dedup_err:
        logger.warning("[watch] Max-2 dedup check failed for %s: %s", instrument, _dedup_err)

    # Extract finding_id from cycle_context for scout→snipe linkage
    _finding_id = (cycle_context or {}).get("finding_id")

    # 2026-04-24: Populate the direction column. Previously the INSERT omitted
    # the `direction` column entirely — watch_suggestions.direction was NULL
    # for every validator-created watch, forcing downstream code to default to
    # BUY. Root-caused via watch 2160 USD_CHF (validator said "bullish" in
    # prose but conditions were SELL-pattern; system treated as BUY, gate
    # correctly blocked BUY-wrong-side, trade never fired).
    #
    # Direction precedence:
    #   1. watch_config["re_entry_direction"] (validator's structured field)
    #   2. cycle_context["direction"]          (scout/ctx direction)
    #   3. infer from structured conditions    (invalidation_level, close-op, ema_cross)
    _ctx_direction = (cycle_context or {}).get("direction", "")
    # 2026-04-27: use _normalize_direction helper to handle bullish/bearish/long/short
    # synonyms. Old chain used `.lower()` then `if _db_direction not in ("buy","sell")`
    # which silently dropped "bullish"/"bearish" — the format scout/effective_direction
    # actually emit. Result: watches 2204, 2205, 2208 were all created with NULL
    # direction despite ctx.direction being clearly set to bullish/bearish.
    _db_direction = (
        _normalize_direction(watch_config.get("re_entry_direction"))
        or _normalize_direction(_ctx_direction)
        or _infer_direction_from_conditions(watch_config.get("conditions", []))
    )
    # Sanity-check: warn if validator-declared direction contradicts inferred
    _inferred = _infer_direction_from_conditions(watch_config.get("conditions", []))
    _declared = _normalize_direction(
        watch_config.get("re_entry_direction") or _ctx_direction
    )
    if _inferred and _declared and _inferred != _declared:
        logger.warning(
            "[watch] %s direction MISMATCH: validator declared='%s' but conditions infer='%s'. "
            "Using declared. Conditions may be self-inconsistent — investigate validator output.",
            instrument, _declared, _inferred)

    cursor = conn.execute("""
        INSERT INTO watch_suggestions
        (cycle_id, instrument, direction, suggestion_type, conditions, raw_suggestion,
         validator_verdict, validator_confidence, created_at, expires_at,
         status, workspace_task_id, context, agent_name, origin_type, user_id, finding_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'watching', ?, ?, 'snipe', 'scout', ?, ?)
    """, (
        cycle_id,
        instrument,
        _db_direction,
        watch_config.get("suggestion_type", "unknown"),
        json.dumps(watch_config.get("conditions", [])),
        watch_config.get("raw_text", ""),
        validator_response.get("verdict", "HOLD"),
        validator_response.get("confidence", 0),
        now.isoformat(),
        expires.isoformat(),
        workspace_task_id,
        display_context,
        user_id,
        _finding_id,
    ))
    watch_id = cursor.lastrowid
    conn.commit()

    # Link scout finding → snipe for pipeline lineage tracing (update scout_findings.snipe_created)
    if _finding_id and watch_id:
        try:
            from scout_learning_system import link_finding_to_snipe
            link_finding_to_snipe(_finding_id, watch_id)
            logger.info("[watch] Linked scout finding #%s → snipe #%s", _finding_id, watch_id)
        except Exception as _link_err:
            logger.warning("[watch] Failed to link finding #%s → snipe #%s: %s",
                           _finding_id, watch_id, _link_err)

    # Also create a workspace_task for visibility in dashboard
    if workspace_task_id is None:
        try:
            ws_conn = get_workspaces()
            task_cursor = ws_conn.execute("""
                INSERT INTO workspace_tasks
                (workspace_id, title, description, status, priority, assigned_agent_id, metadata, created_at)
                VALUES (896, ?, ?, 'watching', 'normal', 'watch_manager', ?, ?)
            """, (
                f"Watch: {instrument} — {watch_config.get('raw_text', '')}",
                f"Validator suggested re-entry conditions. Checking every {wcfg['check_interval_sec']//60}min. "
                f"Expires {expires.strftime('%H:%M ET')}. "
                f"Conditions: {json.dumps(watch_config.get('conditions', []))}",
                json.dumps({
                    "watch_id": watch_id,
                    "instrument": instrument,
                    "suggestion_type": watch_config.get("suggestion_type"),
                    "conditions": watch_config.get("conditions", []),
                    "expires_at": expires.isoformat(),
                }),
                now.isoformat(),
            ))
            ws_task_id = task_cursor.lastrowid
            ws_conn.commit()
            conn.execute(
                "UPDATE watch_suggestions SET workspace_task_id=? WHERE id=?",
                (ws_task_id, watch_id)
            )
            conn.commit()
            logger.info("Created watch task %d (ws_task=%d) for %s: %s",
                       watch_id, ws_task_id, instrument, watch_config.get("raw_text"))
        except Exception as exc:
            logger.warning("Failed to create workspace_task for watch: %s", exc)
    
    # Don't close pooled connections
    return watch_id


# ---------------------------------------------------------------------------
# Check watch conditions against live data
# ---------------------------------------------------------------------------

def check_conditions(conditions: List[dict], sniper_result: dict,
                     confluence_score: dict = None,
                     market_picture: dict = None,
                     kronos_forecast: dict = None,
                     instrument: str = "") -> Dict[str, Any]:
    """Check if all watch conditions are met against current market data.

    Args:
        conditions: List of condition dicts from parse_suggestions
        sniper_result: Output from compute_sniper_score()
        confluence_score: Output from compute_full_confluence() (optional)
        market_picture: Output from generate_market_picture() (optional)
        kronos_forecast: Live Kronos forecast data for kronos_* fields (optional)

    Returns:
        {"met": bool, "details": [{"condition": ..., "current": ..., "met": bool}]}
    """
    indicators = sniper_result.get("indicators", {})
    patterns = sniper_result.get("detected_patterns", [])
    buy = sniper_result.get("buy_score", 0)
    sell = sniper_result.get("sell_score", 0)
    max_score = max(buy, sell)
    
    # Extract EMA data from market picture if available
    ema = market_picture.get("ema", {}) if market_picture else {}
    
    details = []
    all_met = True
    
    for cond in conditions:
        field = cond.get("field", "")
        op = cond.get("op", ">=")
        target = cond.get("value")
        source = cond.get("source", "")
        
        current = None
        met = False
        
        # ── Standard fields ──────────────────────────────────────
        if field == "sniper_score":
            current = max_score
        elif field == "max_score":
            current = max_score
        elif field == "total_score" and confluence_score:
            current = confluence_score.get("total_score", 0)
        elif field == "has_reversal_pattern":
            reversal_patterns = {"hammer", "inverted_hammer", "bullish_engulfing",
                                "bearish_engulfing", "morning_star", "evening_star",
                                "piercing_line", "dark_cloud_cover", "three_white_soldiers",
                                "three_black_crows"}
            current = bool(set(p.lower().replace(" ", "_") for p in patterns) & reversal_patterns)
        elif field == "close_vs_ema":
            ema_field = cond.get("ema_field", "ema_21")
            ema_val = indicators.get(ema_field, 0)
            close = indicators.get("close", 0)
            if ema_val and close:
                current = close - ema_val
                target = 0
        
        # ── EMA narrative fields ─────────────────────────────────
        elif field == "ema_fan_direction":
            current = ema.get("fan_direction", "unknown")
        elif field == "bb_width_pips":
            # bb_width raw is price units; pips needs conversion via instrument
            _bbw = indicators.get("bb_width", 0)
            _pip = 0.01 if "JPY" in (instrument or "") else 0.0001
            current = (_bbw / _pip) if _bbw else 0
        elif field == "ema_fan_state":
            current = ema.get("fan_state", "unknown")
            if op == "in" and isinstance(target, list):
                # ── Normalise phantom fan_state values ──────────────────────
                # ema_separation.py only ever produces: 'expanding', 'contracting',
                # 'stable', 'just_crossed', 'peaked', 'forming', 'decelerating'.
                # The validator LLM sometimes writes 'bearish_expanding',
                # 'bullish_expanding', 'bearish_accelerating' — these are valid
                # intent labels but are never returned by the system.
                # Map them to their canonical equivalents so snipe conditions work.
                _FAN_ALIAS = {
                    'bearish_expanding':    'expanding',
                    'bullish_expanding':    'expanding',
                    'bearish_accelerating': 'expanding',
                    'bullish_accelerating': 'expanding',
                    'bearish_contracting':  'contracting',
                    'bullish_contracting':  'contracting',
                    'bearish_peaked':       'peaked',
                    'bullish_peaked':       'peaked',
                }
                normalised_target = [_FAN_ALIAS.get(v, v) for v in target]
                met = current in normalised_target
                details.append({
                    "condition": cond.get("desc", f"fan_state in {target}"),
                    "current": current,
                    "target": target,
                    "met": met,
                })
                if not met:
                    all_met = False
                continue  # Skip the generic comparison below
        elif field == "ema_trend_health":
            current = ema.get("trend_health", 0)
        elif field == "ema_velocity":
            # ── ema_velocity sign convention fix ────────────────────────────
            # separation_velocity is always a positive absolute value (rate of
            # EMA spread change). The validator sometimes writes negative targets
            # to indicate "bearish acceleration" (e.g. target=-0.004). Since the
            # value is always positive, a negative target means "abs(velocity) >=
            # abs(target)" — i.e. the fan is opening fast enough, direction is
            # inferred from fan_direction not the sign of velocity.
            _raw_velocity = ema.get("separation_velocity", 0)
            if isinstance(target, (int, float)) and target < 0:
                # Negative target = bearish-acceleration intent → use abs comparison
                current = -abs(_raw_velocity)  # sign it so the <= op works correctly
            else:
                current = abs(_raw_velocity)   # positive target → keep positive
        elif field == "ema_reversal_risk":
            current = ema.get("reversal_risk", "unknown")
            if op == "in" and isinstance(target, list):
                met = current in target
                details.append({
                    "condition": cond.get("desc", f"reversal_risk in {target}"),
                    "current": current,
                    "target": target,
                    "met": met,
                })
                if not met:
                    all_met = False
                continue
        elif field == "ema_price_near_e100":
            # Check if price is within 0.08% of E100
            e100 = ema.get("current_emas", {}).get("ema100", 0)
            close = indicators.get("close", 0) or (
                float(ema.get("current_emas", {}).get("ema21", 0))  # fallback
            )
            if e100 and close:
                dist = abs(close - e100) / close * 100
                current = dist < 0.10
            else:
                current = False
        
        # ── BB expansion / contraction (derived boolean) ────────
        elif field == "bb_expanding":
            # True if BB width has grown over last 5 bars
            bb_width = indicators.get("bb_width", 0)
            bb_width_prev = indicators.get("bb_width_prev", bb_width)
            current = bb_width > bb_width_prev if bb_width and bb_width_prev else False

        elif field == "bb_contracting":
            bb_width = indicators.get("bb_width", 0)
            bb_width_prev = indicators.get("bb_width_prev", bb_width)
            current = bb_width < bb_width_prev if bb_width and bb_width_prev else False

        elif field == "bb_width_trend":
            # "expanding" or "contracting"
            bb_width = indicators.get("bb_width", 0)
            bb_width_prev = indicators.get("bb_width_prev", bb_width)
            current = "expanding" if bb_width > bb_width_prev else "contracting"

        elif field == "bb_acceleration":
            # BB width change rate (positive = accelerating expansion)
            bb_width = indicators.get("bb_width", 0)
            bb_width_prev = indicators.get("bb_width_prev", 0)
            current = round(bb_width - bb_width_prev, 6) if bb_width and bb_width_prev else 0

        # ── Momentum candles (strong directional bodies) ─────────
        elif field in ("momentum_candles", "has_momentum_candles"):
            # True if recent candle is a strong body (body > 60% of range, not a doji)
            close = indicators.get("close") or 0
            open_ = indicators.get("open") or close
            high = indicators.get("high") or close
            low = indicators.get("low") or close
            body = abs(close - open_)
            full_range = abs(high - low)
            current = bool(full_range > 0 and body / full_range >= 0.6)

        # ── Fan acceleration / opening ────────────────────────────
        elif field == "fan_accelerating":
            vel = ema.get("separation_velocity", 0)
            current = vel > tc_get("watch.ema_velocity_threshold", 0.003)  # above threshold = accelerating (lowered from 0.005 2026-03-26)

        elif field == "fan_opening":
            current = ema.get("fan_state", "") in ("bullish_expanding", "bearish_expanding")

        # ── Derived indicator fields ─────────────────────────────
        elif field == "bb_position":
            # Derive BB position string from raw indicator values
            close = indicators.get("close", 0)
            bb_upper = indicators.get("bb_upper", 0)
            bb_lower = indicators.get("bb_lower", 0)
            bb_middle = indicators.get("bb_middle", 0)
            if close and bb_upper and bb_lower and bb_middle:
                if close > bb_upper:
                    current = "Above Upper"
                elif close < bb_lower:
                    current = "Below Lower"
                elif close > bb_middle:
                    current = "Above Middle"
                else:
                    current = "Below Middle"
            else:
                current = "Unknown"

        elif field == "stoch_zone":
            stoch_k = indicators.get("stoch_k", 50)
            if stoch_k >= 80:
                current = "overbought"
            elif stoch_k <= 20:
                current = "oversold"
            else:
                current = "neutral"

        elif field == "rsi_zone":
            rsi = indicators.get("rsi", 50)
            if rsi >= 70:
                current = "overbought"
            elif rsi <= 30:
                current = "oversold"
            else:
                current = "neutral"

        # ── Two-cross confirmation: E21 × E100 (Cross 2) ───────────
        elif field == "e21_crossed_100_recently":
            # True when E21×E100 cross happened within last 10 bars — fan fully ordered
            current = bool(sniper_result.get("e21_crossed_100_recently", False))

        elif field == "e21_crossed_100_this_bar":
            current = bool(sniper_result.get("e21_crossed_100_this_bar", False))

        # ── Price-level conditions (validator snipe specifics) ──────
        elif field == "price_zone":
            # target is "low-high" string e.g. "0.9108-0.9113"
            close = indicators.get("close", 0)
            if close and isinstance(target, str) and "-" in target:
                try:
                    parts = target.split("-")
                    low = float(parts[0].strip())
                    high = float(parts[1].strip())
                    current = close
                    met = low <= close <= high
                    details.append({
                        "condition": cond.get("desc", f"price in zone {target}"),
                        "current": round(current, 5),
                        "target": target,
                        "met": met,
                    })
                    if not met:
                        all_met = False
                    continue
                except (ValueError, IndexError):
                    current = None

        elif field == "price_above":
            close = indicators.get("close", 0)
            if close and target is not None:
                current = close
                met = close > float(target)

        elif field == "price_below":
            close = indicators.get("close", 0)
            if close and target is not None:
                current = close
                met = close < float(target)

        # ── Kronos forecast fields (live re-evaluation) ────────────
        # The snipe monitor runs a fresh Kronos forecast and passes it as
        # kronos_forecast dict. These fields compare live forecast to the
        # conditions set when the snipe was created.
        elif field == "kronos_direction":
            if kronos_forecast:
                current = kronos_forecast.get("direction", "")
            else:
                current = None
        elif field == "kronos_drift_pips":
            if kronos_forecast:
                current = abs(kronos_forecast.get("drift_pips", 0))
            else:
                current = 0
        elif field == "kronos_consensus":
            if kronos_forecast:
                current = kronos_forecast.get("consensus", False)
            else:
                current = False
        elif field == "kronos_drift_atr_frac":
            if kronos_forecast:
                current = abs(kronos_forecast.get("drift_atr_frac", 0))
            else:
                current = 0
        elif field == "kronos_entry_price":
            # Check if current price has reached the predicted entry level
            close = indicators.get("close", 0)
            if close:
                current = close
            else:
                current = None

        elif field == "invalidation_level":
            # If price crosses invalidation, condition is NOT met (watch should cancel)
            close = indicators.get("close", 0)
            if close and isinstance(target, (int, float)):
                current = close
                met = False  # Invalidation means "if price reaches here, thesis is dead"
                # The watch caller checks this specially — if invalidation is hit,
                # the watch should be cancelled rather than triggered
                details.append({
                    "condition": cond.get("desc", f"invalidation at {target}"),
                    "current": round(current, 5) if current else None,
                    "target": target,
                    "met": not (close >= float(target)),  # met = price has NOT hit invalidation
                })
                if close >= float(target):
                    all_met = False
                continue

        elif field == "bb_bandwidth":
            # Numeric BB width threshold (e.g. target=0.00350 means "BB must be >= 0.00350")
            bb_width = indicators.get("bb_width", 0)
            current = bb_width

        elif field == "bb_squeeze_break":
            # True when BB was squeezing and just started expanding
            bb_width = indicators.get("bb_width", 0)
            bb_width_prev = indicators.get("bb_width_prev", bb_width)
            bb_squeeze = indicators.get("bb_squeeze", False)
            # Squeeze break = was squeezing or narrow, now expanding
            current = bool(bb_width > bb_width_prev and bb_width_prev > 0)

        elif field == "ema_cross_below":
            # Check if one EMA is below another (e.g. "E21 < E55" for bearish ordering)
            emas = ema.get("current_emas", {})
            if isinstance(target, str) and "<" in target:
                try:
                    parts = target.split("<")
                    ema_a_key = parts[0].strip().lower()
                    ema_b_key = parts[1].strip().lower()
                    # Normalize: "e21" → "ema21", "ema21" stays "ema21"
                    if ema_a_key.startswith("e") and not ema_a_key.startswith("ema"):
                        ema_a_key = "ema" + ema_a_key[1:]
                    if ema_b_key.startswith("e") and not ema_b_key.startswith("ema"):
                        ema_b_key = "ema" + ema_b_key[1:]
                    ema_a = float(emas.get(ema_a_key, 0))
                    ema_b = float(emas.get(ema_b_key, 0))
                    current = f"{ema_a_key}={ema_a:.5f} vs {ema_b_key}={ema_b:.5f}"
                    met = ema_a < ema_b if ema_a and ema_b else False
                except (ValueError, IndexError):
                    current = None
                    met = False
            else:
                current = None
                met = False
            details.append({
                "condition": cond.get("desc", f"{field}: {target}"),
                "current": current, "target": target, "met": met,
            })
            if not met:
                all_met = False
            continue

        elif field == "ema_cross_above":
            # Check if one EMA is above another (e.g. "E21 > E55" for bullish ordering)
            emas = ema.get("current_emas", {})
            if isinstance(target, str) and ">" in target:
                try:
                    parts = target.split(">")
                    ema_a_key = parts[0].strip().lower()
                    ema_b_key = parts[1].strip().lower()
                    # Normalize: "e21" → "ema21", "ema21" stays "ema21"
                    if ema_a_key.startswith("e") and not ema_a_key.startswith("ema"):
                        ema_a_key = "ema" + ema_a_key[1:]
                    if ema_b_key.startswith("e") and not ema_b_key.startswith("ema"):
                        ema_b_key = "ema" + ema_b_key[1:]
                    ema_a = float(emas.get(ema_a_key, 0))
                    ema_b = float(emas.get(ema_b_key, 0))
                    current = f"{ema_a_key}={ema_a:.5f} vs {ema_b_key}={ema_b:.5f}"
                    met = ema_a > ema_b if ema_a and ema_b else False
                except (ValueError, IndexError):
                    current = None
                    met = False
            else:
                current = None
                met = False
            details.append({
                "condition": cond.get("desc", f"{field}: {target}"),
                "current": current, "target": target, "met": met,
            })
            if not met:
                all_met = False
            continue

        # ── Indicator fields (fallback) ──────────────────────────
        elif field in indicators:
            current = indicators.get(field)
        
        # ── Generic comparison ───────────────────────────────────
        if current is not None and target is not None:
            if op == ">=":
                met = current >= target
            elif op == ">":
                met = current > target
            elif op == "<=":
                met = current <= target
            elif op == "<":
                met = current < target
            elif op == "==":
                met = current == target
            elif op == "in":
                met = current in target if isinstance(target, list) else current == target
            elif op == "!=":
                met = current != target
            elif op == "not_in":
                met = current not in target if isinstance(target, list) else current != target
        
        if not met:
            all_met = False
        
        details.append({
            "condition": cond.get("desc", f"{field} {op} {target}"),
            "current": current,
            "target": target,
            "met": met,
        })
    
    return {"met": all_met, "details": details}


# ---------------------------------------------------------------------------
# Run check loop for all active watches
# ---------------------------------------------------------------------------

def check_active_watches(user_id: int = None) -> List[Dict[str, Any]]:
    """Check all active watches against current market data.

    Args:
        user_id: Filter watches to this user. Required for multi-user.

    Returns list of watches that triggered (conditions met).
    Call this from a timer/cron every 5 minutes.
    """
    if user_id is None:
        logger.error("[WATCH] check_active_watches called without user_id — skipping (multi-user requires explicit user)")
        return []
    _ensure_tables()
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)

    # ── Fetch open OANDA positions once, up-front ─────────────────────────
    # Any pair with an open trade is fully skipped for trigger+notification.
    # Conditions are still evaluated (progress updated) but no fire occurs.
    # Fetch fails SAFE: if OANDA unreachable, assume nothing is open so we
    # don't permanently block watches — individual notify gate below still runs.
    _open_instruments: set = set()
    _oanda_prefetch_ok: bool = False  # True = OANDA API responded (even if 0 trades)
    try:
        try:
            from broker_credentials import BrokerCredentials as _BC_prefetch
        except ImportError:
            from Source.broker_credentials import BrokerCredentials as _BC_prefetch
        import requests as _rq_prefetch
        _cred_prefetch = _BC_prefetch().get_connection(user_id=user_id, broker="oanda")
        _ot_prefetch = _rq_prefetch.get(
            f"{_cred_prefetch['base_url']}/v3/accounts/{_cred_prefetch['account_id']}/openTrades",
            headers={"Authorization": f"Bearer {_cred_prefetch['api_key']}"},
            timeout=4,
        ).json().get("trades", [])
        _open_instruments = {t.get("instrument") for t in _ot_prefetch}
        _oanda_prefetch_ok = True  # API call succeeded — trust this over stale DB data
        if _open_instruments:
            logger.debug("[WATCH] Open positions pre-fetch: %s — trigger/notify blocked for these pairs",
                         _open_instruments)
        else:
            logger.debug("[WATCH] OANDA pre-fetch OK — no open positions (DB overlap guard will defer to OANDA)")
    except Exception as _prefetch_err:
        logger.warning("[WATCH] OANDA pre-fetch failed (%s) — open-trade gate will rely on per-watch check",
                       _prefetch_err)

    # Get active watches — also include triggered watches with no cycle yet
    # (so they get re-checked every 5 min until validator confirms or 6h expires)
    watches = conn.execute("""
        SELECT * FROM watch_suggestions
        WHERE user_id = ?
          AND (status = 'watching'
               OR (status = 'triggered'
                   AND trade_cycle_id IS NULL
                   AND triggered_at > datetime('now', '-6 hours')))
        ORDER BY created_at ASC
    """, (user_id,)).fetchall()
    
    if not watches:
        # Don't close pooled connections
        return []
    
    triggered = []

    # ── GAP-7: Track pair fires within this check cycle ───────────────────────
    # Prevents two watches on the same pair from both firing in one scan cycle.
    # A pair is keyed by instrument name only (any-direction block per Tim's spec).
    fired_this_cycle: set = set()  # instruments that already fired this cycle

    for watch in watches:
        instrument = watch["instrument"]
        watch_id = watch["id"]
        expires_at = _safe_isoformat(watch["expires_at"])
        
        # TTL expiry — all watches now get a proper expires_at (8h default).
        # Legacy 9999-year watches from before Fix 5 are NOT auto-expired here
        # to avoid nuking anything the user manually extended.
        if expires_at.year < 9999 and now > expires_at:
            conn.execute(
                "UPDATE watch_suggestions SET status='expired' WHERE id=?",
                (watch_id,)
            )
            # Update workspace task too
            if watch["workspace_task_id"]:
                try:
                    ws_conn = get_workspaces()
                    ws_conn.execute(
                        "UPDATE workspace_tasks SET status='expired' WHERE id=?",
                        (watch["workspace_task_id"],)
                    )
                    ws_conn.commit()
                except Exception:
                    pass
            logger.info("Watch %d expired for %s", watch_id, instrument)
            continue


        # ── Re-fire triggered watches that never got a cycle ──
        # Rule: a triggered watch must fill within 5 minutes or reset to 'watching'.
        # The watch stays alive and can re-trigger if conditions are met fresh.
        # This prevents stale entries from executing as price moves away.
        is_re_fire = (watch["status"] == "triggered" and not watch["trade_cycle_id"])
        if is_re_fire:
            triggered_at_str = watch["triggered_at"]
            if triggered_at_str:
                try:
                    triggered_dt = _safe_isoformat(triggered_at_str)
                    if triggered_dt.tzinfo is None:
                        triggered_dt = triggered_dt.replace(tzinfo=timezone.utc)
                    age_minutes = (now - triggered_dt).total_seconds() / 60
                    if age_minutes > 5:
                        # Stale trigger — reset to watching, re-evaluate fresh next tick
                        conn.execute(
                            "UPDATE watch_suggestions SET status='watching', triggered_at=NULL WHERE id=?",
                            (watch_id,)
                        )
                        conn.commit()
                        logger.info(
                            "[WATCH] %s #%d trigger expired (%.1f min old) — reset to watching",
                            instrument, watch_id, age_minutes
                        )
                        continue  # Skip this watch — will re-evaluate next 5-min tick
                except Exception:
                    pass

        # ── KRONOS PATH SNIPE — fetch live Kronos forecast for evaluation ──
        # Kronos snipes use kronos_* condition fields. The monitor runs a fresh
        # Kronos forecast and passes it to check_conditions alongside the normal
        # sniper/market data. If Kronos changed its mind, conditions won't match.
        _sug_type = watch["suggestion_type"] if "suggestion_type" in watch.keys() else ""
        _kronos_live = None
        if _sug_type == "kronos_path_snipe":
            try:
                try:
                    from kronos_runtime import get_kronos_hunter as _get_kh
                except ImportError:
                    from Source.kronos_runtime import get_kronos_hunter as _get_kh
                _kh = _get_kh()
                if _kh and hasattr(_kh, '_inference') and _kh._inference.is_ready():
                    try:
                        from kronos_runtime import _load_candles_via_oanda as _kload
                    except ImportError:
                        from Source.kronos_runtime import _load_candles_via_oanda as _kload
                    _k_candles = _kload(instrument, count=256)
                    if _k_candles is not None and len(_k_candles) >= 100:
                        _k_fr = _kh._inference.forecast(instrument, _k_candles)
                        if _k_fr:
                            # Build path plan for direction
                            try:
                                import pandas as _kpd
                                import sys as _ksys
                                from pathlib import Path as _KPath
                                _ksys.path.insert(0, str(_KPath(__file__).resolve().parent.parent.parent / "research" / "kronos"))
                                from kronos_path_walkforward import extract_path_plan as _k_epp
                                _kf_df = _kpd.DataFrame(_k_fr.forecast_path).rename(
                                    columns={"o": "open", "h": "high", "l": "low", "c": "close"}
                                ) if _k_fr.forecast_path else None
                                _k_plan = _k_epp(_kf_df, float(_k_candles["close"].iloc[-1]), instrument) if _kf_df is not None else None
                                _k_dir = _k_plan["direction"] if _k_plan else _k_fr.direction
                            except Exception:
                                _k_dir = _k_fr.direction
                            _kronos_live = {
                                "direction": _k_dir,
                                "drift_pips": _k_fr.drift_pips,
                                "drift_atr_frac": _k_fr.drift_atr_frac,
                                "confidence": _k_fr.confidence,
                                "consensus": _k_fr.consensus,
                            }
                            logger.debug("[KRONOS SNIPE] %s #%d live forecast: dir=%s drift=%.1f consensus=%s",
                                         instrument, watch_id, _k_dir, _k_fr.drift_pips, _k_fr.consensus)
                else:
                    logger.debug("[KRONOS SNIPE] %s #%d: Kronos not ready, skipping check", instrument, watch_id)
                    continue
            except Exception as _kfetch_exc:
                # 2026-04-24: upgraded — silent kronos forecast fetch failure
                # causes the snipe evaluation path to `continue` and skip this
                # snipe entirely — loses visibility into kronos eval misses.
                logger.warning("[KRONOS SNIPE] %s #%d forecast fetch FAILED: %s: %s (snipe eval skipped)",
                               instrument, watch_id, type(_kfetch_exc).__name__, _kfetch_exc)
                continue  # Can't evaluate without live forecast

        # (Kronos snipes now flow through the normal check_conditions path below
        #  with kronos_* fields evaluated against a live forecast)

        # Run lightweight indicator check
        try:
            try:
                from agents.wrappers import compute_sniper_score
                from agents.trading_cycle import _load_risk_config
            except ImportError:
                from Source.agents.wrappers import compute_sniper_score
                from Source.agents.trading_cycle import _load_risk_config
            
            # Get candles — try swarm first, fall back to direct OANDA
            candles_by_tf = {}
            
            # Try direct OANDA client first (works without swarm/team init)
            try:
                try:
                    from broker_credentials import BrokerCredentials as _BC_wm
                except ImportError:
                    from Source.broker_credentials import BrokerCredentials as _BC_wm
                _wm_conn = _BC_wm().get_connection(user_id=user_id, broker="oanda")
                _api_key  = _wm_conn.get("api_key", "")
                _wm_url   = _wm_conn.get("base_url", "https://api-fxpractice.oanda.com")
                try:
                    from Source.oanda_client import OandaClient
                except ImportError:
                    from oanda_client import OandaClient
                # 2026-05-01: cached fetch — see candle_cache.py rationale.
                # 5-min TTL matches watch evaluator cycle. Cache hit is a dict
                # lookup; miss falls through to OandaClient identical to before.
                try:
                    from candle_cache import get_cached_candles as _gcc_wm
                except ImportError:
                    from Source.candle_cache import get_cached_candles as _gcc_wm
                with OandaClient(_api_key, _wm_url) as _client:
                    for tf in ["M15", "H1", "H4"]:
                        try:
                            def _fetch_wm(_pair=instrument, _tf=tf, _cl=_client):
                                _raw = _cl.get_candles(_pair, granularity=_tf, count=250)
                                return _raw if isinstance(_raw, list) else _raw.get("candles", [])
                            candles_by_tf[tf] = _gcc_wm(_fetch_wm, instrument, tf, 250)
                        except Exception:
                            pass
            except Exception as oanda_exc:
                logger.debug("Watch %d: direct OANDA failed (%s), trying swarm", watch_id, oanda_exc)
                # Fallback to swarm — also cached. Same 5-min TTL.
                try:
                    try:
                        from agents.trading_cycle import _swarm_execute_tool
                        from candle_cache import get_cached_candles as _gcc_wm_swarm
                    except ImportError:
                        from Source.agents.trading_cycle import _swarm_execute_tool
                        from Source.candle_cache import get_cached_candles as _gcc_wm_swarm
                    for tf in ["M15", "H1", "H4"]:
                        try:
                            def _fetch_swarm(_pair=instrument, _tf=tf):
                                _result = _swarm_execute_tool(
                                    "oanda_data", "get_candles",
                                    instrument=_pair, granularity=_tf, count=250
                                )
                                _cr = _result.get("tool_result", _result)
                                return _cr.get("result", _cr).get("candles", _cr.get("candles", []))
                            candles_by_tf[tf] = _gcc_wm_swarm(_fetch_swarm, instrument, tf, 250)
                        except Exception:
                            pass
                except Exception:
                    pass
            
            if not candles_by_tf.get("M15") and not candles_by_tf.get("H1") and _sug_type != "kronos_path_snipe":
                logger.warning("Watch %d: no candles for %s (need M15 or H1)", watch_id, instrument)
                continue
            
            cfg = _load_risk_config()
            threshold = int(cfg.get("sniper", {}).get("threshold", 12))
            sniper = compute_sniper_score(candles_by_tf, instrument, threshold) if candles_by_tf else {"indicators": {}, "detected_patterns": [], "buy_score": 0, "sell_score": 0}
            
            # Always compute full market picture — snipes need the big picture
            conditions = json.loads(watch["conditions"])
            
            mkt_picture = None
            try:
                try:
                    from backtester.ema_separation import generate_market_picture
                except ImportError:
                    from Source.backtester.ema_separation import generate_market_picture

                # Normalize M15 candles for EMA module — we trade on M15, NOT H1.
                # H1 was wrong here since inception; fan_state/fan_direction were
                # computed on the wrong timeframe, causing direction sanity gates
                # to operate on stale/mismatched data (root cause of trades 2657/2669).
                _m15_raw_mp = candles_by_tf.get("M15", [])
                _m15_norm = []
                for c in _m15_raw_mp:
                    mid = c.get("mid", {})
                    _m15_norm.append({
                        "time": c.get("time", ""),
                        "open": mid.get("o", c.get("open", 0)),
                        "high": mid.get("h", c.get("high", 0)),
                        "low": mid.get("l", c.get("low", 0)),
                        "close": mid.get("c", c.get("close", 0)),
                    })
                if len(_m15_norm) >= 100:
                    mkt_picture = generate_market_picture(instrument, _m15_norm)
                else:
                    logger.warning("Watch %d %s: only %d M15 candles (need 100) — market picture unavailable",
                                   watch_id, instrument, len(_m15_norm))
            except Exception as ema_exc:
                logger.error("Watch %d %s: market picture FAILED: %s", watch_id, instrument, ema_exc)

            # ── HARD GATE: market picture is REQUIRED for snipe decisions ──────
            # Without fan_state/fan_direction the direction sanity gates have no
            # data and every snipe passes blind.  Trades 2657/2669 lost because
            # mkt_picture was None and the "fail safe = allow" philosophy let
            # 3-day-old watches fire into reversed markets.
            # If market picture is unavailable, skip this watch for THIS cycle
            # only — it will be re-evaluated in 5 minutes when data may be back.
            if mkt_picture is None and _sug_type != "kronos_path_snipe":
                logger.warning("[WATCH] %s #%d: BLOCKED — no market picture (fan data required for snipe). "
                               "Will retry next 5-min cycle.", instrument, watch_id)
                try:
                    flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                  data={"watch_id": watch_id, "gate": "no_market_picture",
                                        "suggestion_type": _sug_type},
                                  status="skip",
                                  note=f"Watch #{watch_id} blocked: no market picture")
                except Exception:
                    pass
                continue

            result = check_conditions(conditions, sniper, market_picture=mkt_picture,
                                      kronos_forecast=_kronos_live, instrument=instrument)

            # ── Phase 2: Grade A/B entry signal gate ─────────────────────────────
            # Before allowing a snipe to fire, check if the chart would show a Grade A/B
            # entry marker at the current bar (E100 bounce candle confirmation).
            # This prevents snipes from triggering mid-retracement before the bounce
            # confirms — the same moment Tim sees the entry marker on his chart.
            # Only applied to validator_structured watches (these are the ones that enter
            # during retracement and need the E100 bounce to confirm).
            _chart_entry_confirmed = True  # default: allow (don't block non-structured watches)
            if (watch["suggestion_type"] if "suggestion_type" in watch.keys() else "") == "validator_structured":
                try:
                    m15_raw = candles_by_tf.get("M15", [])
                    if len(m15_raw) >= 100:
                        # Normalise to simple dicts for format_chart_signals
                        _m15_norm = []
                        for _c in m15_raw:
                            _mid = _c.get("mid", {})
                            _m15_norm.append({
                                "time": _c.get("time", ""),
                                "open":  float(_mid.get("o", _c.get("open", 0))),
                                "high":  float(_mid.get("h", _c.get("high", 0))),
                                "low":   float(_mid.get("l", _c.get("low", 0))),
                                "close": float(_mid.get("c", _c.get("close", 0))),
                            })
                        try:
                            from backtester.ema_separation import format_chart_signals as _fcs
                        except ImportError:
                            from Source.backtester.ema_separation import format_chart_signals as _fcs

                        _markers = _fcs(_m15_norm)
                        # Check if the last 3 bars contain a Grade A or B entry signal
                        _recent_times = {_m15_norm[i]['time'] for i in range(max(0, len(_m15_norm)-3), len(_m15_norm))}
                        _entry_signals = [m for m in _markers
                                          if m.get('type') == 'entry'
                                          and m.get('time') in _recent_times
                                          and ('A' in m.get('label', '') or 'B' in m.get('label', ''))]

                        if _entry_signals:
                            _chart_entry_confirmed = True
                            logger.info("Watch %d %s: Grade %s entry signal confirmed on M15 — snipe armed",
                                        watch_id, instrument,
                                        'A' if any('A' in m.get('label','') for m in _entry_signals) else 'B')
                        else:
                            # No entry signal in last 3 bars — snipe can still fire if conditions met
                            # but log it so we can track false triggers
                            _chart_entry_confirmed = True  # don't hard-block, just log for now
                            logger.debug("Watch %d %s: no Grade A/B entry signal in last 3 M15 bars — conditions met but no chart confirmation",
                                         watch_id, instrument)
                except Exception as _fcs_err:
                    # 2026-04-24: upgraded — chart entry signal check
                    # confirms Grade A/B pattern at trigger time. Silent
                    # failure = snipe fires without chart confirmation.
                    logger.warning("Watch %d: chart entry signal check FAILED: %s: %s (chart confirm bypassed)",
                                   watch_id, type(_fcs_err).__name__, _fcs_err)

            # ── Progress tracking ──
            # ── Flat trigger threshold (restored 2026-03-31) ───────────────
            # Core+Bonus classification removed — it was allowing watches to
            # fire at as low as 29% criteria met (trade 2945 EUR_AUD) because
            # keyword-based core/bonus splitting was too loose.  The validator
            # already curated every condition as a complete package.  Honour
            # its judgment: ALL conditions must be met before the snipe fires.
            #
            # 2026-04-06: Raised 0.80 → 0.90 based on audit of 70 snipe_direct
            # trades. 5-condition watches at 80% = 4/5 needed (1 critical condition
            # missing, often RSI momentum). 7-condition watches won 73% vs 25% for
            # 5-condition. At 0.90, both 5 and 6-condition watches require 100%.
            # Previously raised to 0.90 on 2026-03-11, then rolled back to 0.80
            # during overhaul. This time: keeping it — data confirms higher threshold
            # = better outcomes. DO NOT ROLL BACK without re-auditing.
            SNIPE_TRIGGER_THRESHOLD = 0.90
            met_count = sum(1 for d in result["details"] if d.get("met"))
            total_count = len(result["details"]) or 1
            progress_pct = met_count / total_count
            old_peak = 0
            try:
                old_peak = float(watch["peak_progress"] or 0)
            except (TypeError, ValueError):
                pass
            new_peak = max(old_peak, progress_pct)
            # 2026-04-23: coerce numpy types (bool_, int64, float64) to Python
            # natives so json.dumps doesn't raise "Object of type bool_ is not
            # JSON serializable". That exception was silently caught by the
            # outer except at end of loop, preventing kronos_path_snipe watches
            # from EVER reaching the trigger branch — 0 kronos snipes ever fired.
            progress_json = json.dumps(result["details"], default=str)

            # Update progress + check count on every check
            conn.execute(
                "UPDATE watch_suggestions SET conditions_progress=?, conditions_met_count=?, "
                "conditions_total_count=?, peak_progress=?, last_checked_at=?, check_count=check_count+1 WHERE id=?",
                (progress_json, met_count, total_count, new_peak, now.isoformat(), watch_id)
            )

            # ── P1 Criteria grading — weekly hit-rate tracking ────────────────────
            # Count this scan; credit it if ≥50% of conditions are currently met.
            # After 100+ scans, flag stale if hit_rate drops below 50%.
            # Clear the flag automatically if hit_rate recovers to ≥60%.
            _criteria_credit = 1 if progress_pct >= 0.5 else 0
            _now_iso = now.isoformat()
            conn.execute(
                """UPDATE watch_suggestions
                   SET criteria_scan_count = COALESCE(criteria_scan_count, 0) + 1,
                       criteria_met_count  = COALESCE(criteria_met_count,  0) + ?,
                       criteria_hit_rate   = CAST(COALESCE(criteria_met_count, 0) + ? AS REAL)
                                             / (COALESCE(criteria_scan_count, 0) + 1),
                       last_graded_at      = ?
                   WHERE id = ?""",
                (_criteria_credit, _criteria_credit, _now_iso, watch_id)
            )
            conn.execute(
                """UPDATE watch_suggestions
                   SET stale_flagged_at = COALESCE(stale_flagged_at, ?)
                   WHERE id = ?
                     AND criteria_scan_count >= 100
                     AND criteria_hit_rate < 0.50
                     AND stale_flagged_at IS NULL""",
                (_now_iso, watch_id)
            )
            conn.execute(
                "UPDATE watch_suggestions SET stale_flagged_at = NULL "
                "WHERE id = ? AND criteria_hit_rate >= 0.60",
                (watch_id,)
            )

            # Commit progress updates for this watch immediately.
            # Without this, the implicit transaction spans the entire loop and holds
            # a RESERVED lock that blocks other writers (e.g., auto-snipe INSERT).
            conn.commit()

            # Flat threshold trigger: ALL validator conditions must be met.
            # result["met"] = all conditions met (from evaluate_conditions).
            # _flat_trigger = fallback using progress_pct >= threshold.
            _flat_trigger = len(conditions) > 0 and progress_pct >= SNIPE_TRIGGER_THRESHOLD
            should_trigger = result["met"] or _flat_trigger
            if should_trigger:
                logger.info("[WATCH] #%d %s TRIGGER: %d/%d conditions met (%.0f%%)",
                            watch_id, instrument, met_count, total_count, progress_pct * 100)

            # ── OPEN-TRADE GATE — REMOVED 2026-05-14 (Tim approved) ──
            # Previously blocked trigger+notify when any non-kronos trade was open
            # on the pair. Removed to allow snipe + scout/validator coexistence on
            # the same pair (Tim's directive). Other gates (validator_fan_alignment,
            # fan_exhaustion, conditional_exhaustion, refire_gap_exceeded) provide
            # the real filtering. The third overlap guard `overlap_fired_this_cycle`
            # below still prevents two watches on the same pair firing in the same
            # check cycle.
            #
            # Visibility preserved via flight log (note shows concurrent open count).
            if _open_instruments and instrument in _open_instruments and should_trigger:
                try:
                    flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                  data={"watch_id": watch_id, "gate": "open_trade",
                                        "met_count": met_count, "total_count": total_count,
                                        "block_removed": True},
                                  status="ok",
                                  note=f"Watch #{watch_id} {met_count}/{total_count}: concurrent open trade — block removed, proceeding")
                except Exception:
                    pass

            # ── POST-TRADE COOLDOWN (consolidated — enforced here for ALL fire paths) ──
            # Any path that fires a snipe goes through check_active_watches first.
            # Checking here means the cooldown is enforced regardless of whether the
            # snipe came from the watch timer thread or the scout's 5-min check.
            _ctx_raw = watch["context"] if "context" in watch.keys() else None
            _wctx = {}
            try:
                _wctx = json.loads(_ctx_raw or '{}')
            except Exception:
                pass
            _last_close_ts = _wctx.get("_last_fill_close_time", 0)
            if _last_close_ts:
                try:
                    _cooldown_remaining = 900 - (time.time() - float(_last_close_ts))
                    if _cooldown_remaining > 0:
                        logger.info("[WATCH] %s #%d: post-trade cooldown %.0fs remaining — skipping",
                                    instrument, watch_id, _cooldown_remaining)
                        if should_trigger:
                            try:
                                flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                              data={"watch_id": watch_id, "gate": "post_trade_cooldown",
                                                    "cooldown_remaining_s": int(_cooldown_remaining),
                                                    "met_count": met_count, "total_count": total_count},
                                              status="skip",
                                              note=f"Watch #{watch_id} blocked: cooldown {int(_cooldown_remaining)}s remaining")
                            except Exception:
                                pass
                        continue
                except Exception:
                    pass

            # Live sniper scores — always pass current values so snipe direct can infer direction
            _live_buy  = sniper.get("buy_score", 0)
            _live_sell = sniper.get("sell_score", 0)
            _live_direction = "BUY" if _live_buy > _live_sell else ("SELL" if _live_sell > _live_buy else "")
            # Pass EMA fan direction and state so snipe_direct can use them without re-fetch
            # NOTE: generate_market_picture() nests fan_direction under mkt_picture['ema'] — NOT top-level
            _live_fan_dir = ((mkt_picture or {}).get("ema", {}) or {}).get("fan_direction", "") if mkt_picture else ""
            # fan_state is also nested under mkt_picture['ema']['fan_state']
            _live_fan_state = ((mkt_picture or {}).get("ema", {}) or {}).get("fan_state", "") if mkt_picture else ""

            # ── DIRECTION SANITY GATE ─────────────────────────────────────────────
            # Before firing, verify the watch direction still agrees with current
            # market structure.  A watch created 2h ago for a BUY can become stale
            # if the fan has since crossed bearish — firing into that is the root
            # cause of today's 1686/1698/1704 losses.
            #
            # Rules (only applied when watch has an explicit re_entry_direction):
            #   BUY watch:  block if live fan_direction is 'bearish'
            #               block if sniper SELL score > BUY score by ≥ 3 points
            #               block if fan_state is 'just_crossed' with non-bullish direction
            #   SELL watch: mirror of above
            #
            # Fails SAFE: if we can't determine direction, allow the fire (don't
            # silently block valid entries due to data gaps).
            # ─────────────────────────────────────────────────────────────────────
            # 2026-04-23: _wctx.get(key, "") returns None when key exists with value None
            # (default only fires on MISSING key). Use `or ""` so None coerces to empty string
            # before .upper(). Prior bug: silently crashed watch eval every cycle for any
            # watch whose context.re_entry_direction was stored as null (EUR_GBP #2125
            # 7/7 100% never fired for hours; also watches 1727, 1936, 2034).
            # 2026-04-24: Prefer watch_suggestions.direction column (populated by
            # create_watch with conditions-based inference fallback since
            # 2026-04-24 — see _infer_direction_from_conditions). Old watches
            # predating the column fall back to context.re_entry_direction.
            # Discovery: watch 2160 USD_CHF fired BUY in a clearly-SELL setup
            # because context.re_entry_direction was stale BUY from validator
            # prose typo while DB column (after manual correction) said SELL.
            _db_dir = watch["direction"] if "direction" in watch.keys() else None
            _watch_dir = (
                (_db_dir or "")
                or (_wctx.get("re_entry_direction") or "")
            ).upper()  # BUY | SELL | ""
            _sanity_blocked = False
            _sanity_reason = ""
            # 2026-04-29: kronos_path_snipe exemption REMOVED. Backtest of 2,834 historical
            # kronos signals showed kronos was distilled on TRENDING+RETRACING bars (3x
            # oversampled) — the "predicts reversals before fan confirms" hypothesis was
            # wrong. Counter-trend kronos calls are out-of-distribution. Sanity gate now
            # applies to kronos snipes, lifting WR from 88%→94% and PF from 2.20→4.05.
            if _watch_dir in ("BUY", "SELL") and (is_re_fire or should_trigger):
                _fan = (_live_fan_dir or "").lower()      # bullish | bearish | neutral | ""
                _state = (_live_fan_state or "").lower()  # expanding | contracting | just_crossed | ...

                if _watch_dir == "BUY":
                    if _fan == "bearish":
                        _sanity_blocked = True
                        _sanity_reason = "buy_fan_bearish"
                        logger.info("[WATCH] %s #%d: SANITY GATE — BUY watch blocked, fan is bearish",
                                    instrument, watch_id)
                    elif _state == "just_crossed" and _fan not in ("bullish", ""):
                        _sanity_blocked = True
                        _sanity_reason = f"buy_just_crossed_{_fan or 'empty'}"
                        logger.info("[WATCH] %s #%d: SANITY GATE — BUY watch blocked, just_crossed+%s",
                                    instrument, watch_id, _fan)
                    elif _live_sell > _live_buy + 2:
                        _sanity_blocked = True
                        _sanity_reason = f"buy_sniper_sell_dominant_{_live_sell}v{_live_buy}"
                        logger.info("[WATCH] %s #%d: SANITY GATE — BUY watch blocked, sniper SELL(%d) >> BUY(%d)",
                                    instrument, watch_id, _live_sell, _live_buy)

                elif _watch_dir == "SELL":
                    if _fan == "bullish":
                        _sanity_blocked = True
                        _sanity_reason = "sell_fan_bullish"
                        logger.info("[WATCH] %s #%d: SANITY GATE — SELL watch blocked, fan is bullish",
                                    instrument, watch_id)
                    elif _state == "just_crossed" and _fan not in ("bearish", ""):
                        _sanity_blocked = True
                        _sanity_reason = f"sell_just_crossed_{_fan or 'empty'}"
                        logger.info("[WATCH] %s #%d: SANITY GATE — SELL watch blocked, just_crossed+%s",
                                    instrument, watch_id, _fan)
                    elif _live_buy > _live_sell + 2:
                        _sanity_blocked = True
                        _sanity_reason = f"sell_sniper_buy_dominant_{_live_buy}v{_live_sell}"
                        logger.info("[WATCH] %s #%d: SANITY GATE — SELL watch blocked, sniper BUY(%d) >> SELL(%d)",
                                    instrument, watch_id, _live_buy, _live_sell)

            if _sanity_blocked:
                try:
                    flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                  data={"watch_id": watch_id, "gate": "sanity",
                                        "reason": _sanity_reason,
                                        "watch_dir": _watch_dir, "fan_dir": _live_fan_dir,
                                        "fan_state": _live_fan_state,
                                        "sniper_buy": _live_buy, "sniper_sell": _live_sell,
                                        "met_count": met_count, "total_count": total_count},
                                  status="skip",
                                  note=f"Watch #{watch_id} {met_count}/{total_count} blocked: sanity/{_sanity_reason}")
                except Exception:
                    pass
                continue

            # ── EMA ORDERING GATE ─────────────────────────────────────────────
            # Even when fan_direction is neutral (EMAs interleaved during cross),
            # the raw EMA ordering tells us bull vs bear.  If the EMAs are fully
            # ordered AGAINST the watch direction, block the fire.
            # Trade 3015: fan was "neutral" but E21>E55>E100 (bullish) — SELL
            # watch fired into a bullish market.  This gate prevents that.
            # 2026-04-29: kronos_path_snipe exemption REMOVED. EMA ordering must match
            # snipe direction at fire time. See sanity-gate comment above for backtest
            # evidence (88%→94% WR, 2.20→4.05 PF).
            if (is_re_fire or should_trigger) and mkt_picture:
                _ema_wm = ((mkt_picture.get("ema", {}) or {}).get("current_emas", {}))
                _e21_wm = float(_ema_wm.get("ema21", 0) or 0)
                _e55_wm = float(_ema_wm.get("ema55", 0) or 0)
                _e100_wm = float(_ema_wm.get("ema100", 0) or 0)
                if _e21_wm and _e55_wm and _e100_wm:
                    _wm_emas_bull = _e21_wm > _e55_wm > _e100_wm
                    _wm_emas_bear = _e21_wm < _e55_wm < _e100_wm
                    _wm_dir = _watch_dir or _live_direction  # explicit or inferred
                    _wm_ema_conflict = (
                        (_wm_dir == "SELL" and _wm_emas_bull) or
                        (_wm_dir == "BUY"  and _wm_emas_bear)
                    )
                    if _wm_ema_conflict:
                        _wm_mkt = "bullish" if _wm_emas_bull else "bearish"
                        logger.info(
                            "[WATCH] %s #%d: EMA ORDERING GATE — %s watch blocked, EMAs ordered %s "
                            "(E21=%.5f E55=%.5f E100=%.5f)",
                            instrument, watch_id, _wm_dir, _wm_mkt, _e21_wm, _e55_wm, _e100_wm)
                        try:
                            flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                          data={"watch_id": watch_id, "gate": "ema_ordering",
                                                "watch_dir": _wm_dir, "ema_ordering": _wm_mkt,
                                                "e21": _e21_wm, "e55": _e55_wm, "e100": _e100_wm,
                                                "met_count": met_count, "total_count": total_count},
                                          status="skip",
                                          note=f"Watch #{watch_id} {met_count}/{total_count} blocked: EMAs ordered {_wm_mkt} vs {_wm_dir} watch")
                        except Exception:
                            pass
                        continue

            # ── H4 TREND ALIGNMENT GATE — REMOVED 2026-03-24 ────────────────────
            # Was blocking every triggerable snipe using stale scout H4 fan data.
            # The direction sanity gate (above) already checks live sniper scores
            # and current EMA fan — that's sufficient trend alignment.
            # Kept as a comment for audit trail.

            # ── GAP-5: Market alignment for watches without explicit direction ───────
            # The direction sanity gate above only fires when _watch_dir is explicitly
            # set. This gate catches the remaining case: if the conditions fired but the
            # current EMA fan strongly disagrees with the inferred trade direction, skip.
            # Fails SAFE — if fan direction is neutral or unknown, allow the fire.
            # 2026-04-23: skip gap-5 market-align for kronos path snipes (same
            # reason as sanity/ema_ordering gates — kronos's edge IS counter-fan).
            if not _watch_dir and (is_re_fire or should_trigger) and _sug_type != "kronos_path_snipe":
                _gap5_fan = (_live_fan_dir or "").lower()
                _gap5_dir = (_live_direction or "").upper()  # inferred from sniper scores
                if _gap5_dir == "BUY" and _gap5_fan == "bearish":
                    logger.info(
                        "[WATCH] %s #%d: MARKET ALIGN — inferred BUY direction vs bearish EMA fan — skipping",
                        instrument, watch_id,
                    )
                    try:
                        flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                      data={"watch_id": watch_id, "gate": "market_align",
                                            "inferred_dir": "BUY", "fan_dir": "bearish",
                                            "met_count": met_count, "total_count": total_count},
                                      status="skip",
                                      note=f"Watch #{watch_id} {met_count}/{total_count} blocked: inferred BUY vs bearish fan")
                    except Exception:
                        pass
                    continue
                elif _gap5_dir == "SELL" and _gap5_fan == "bullish":
                    logger.info(
                        "[WATCH] %s #%d: MARKET ALIGN — inferred SELL direction vs bullish EMA fan — skipping",
                        instrument, watch_id,
                    )
                    try:
                        flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                      data={"watch_id": watch_id, "gate": "market_align",
                                            "inferred_dir": "SELL", "fan_dir": "bullish",
                                            "met_count": met_count, "total_count": total_count},
                                      status="skip",
                                      note=f"Watch #{watch_id} {met_count}/{total_count} blocked: inferred SELL vs bullish fan")
                    except Exception:
                        pass
                    continue

            # ── GAP-7: Opposing direction / same-pair overlap guard ───────────────
            # Hard block: if ANY trade is already open on this pair (from live_trades
            # DB), do not fire another snipe — regardless of direction. This prevents
            # adding a position against your own open trade and limits double exposure.
            # Separately, fired_this_cycle prevents two watches on the same pair from
            # both firing within a single 5-minute scan cycle.
            # Fails SAFE — DB errors allow the fire (OANDA gate is the backup).
            if is_re_fire or should_trigger:
                if instrument in fired_this_cycle:
                    logger.info(
                        "[WATCH] %s #%d: OVERLAP GUARD — pair already fired this cycle — skipping",
                        instrument, watch_id,
                    )
                    try:
                        flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                      data={"watch_id": watch_id, "gate": "overlap_fired_this_cycle",
                                            "met_count": met_count, "total_count": total_count},
                                      status="skip",
                                      note=f"Watch #{watch_id} {met_count}/{total_count} blocked: another watch already fired on pair this cycle")
                    except Exception:
                        pass
                    continue
                # DB-level check: any open trade on this pair in live_trades?
                # FIX 2026-03-24: OANDA API is the source of truth for open positions.
                # When the OANDA pre-fetch succeeded and says the pair is NOT open,
                # skip the DB check entirely — the DB may contain ghost/stale records
                # from incomplete migrations. Only fall back to the DB check when
                # the OANDA pre-fetch failed (network error, timeout, etc.).
                if _oanda_prefetch_ok and instrument not in _open_instruments:
                    # OANDA confirms no open trade on this pair — trust it, skip DB check
                    logger.debug(
                        "[WATCH] %s #%d: OVERLAP GUARD — OANDA says no open position, skipping stale DB check",
                        instrument, watch_id,
                    )
                elif _oanda_prefetch_ok and instrument in _open_instruments:
                    # OANDA confirms open trade — block REMOVED 2026-05-14 (Tim approved)
                    # Same reason as the upstream `open_trade` gate: snipe + scout
                    # coexistence allowed. Visibility preserved via flight log.
                    try:
                        flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                      data={"watch_id": watch_id, "gate": "overlap_oanda_open",
                                            "met_count": met_count, "total_count": total_count,
                                            "block_removed": True},
                                      status="ok",
                                      note=f"Watch #{watch_id} {met_count}/{total_count}: OANDA shows open position — block removed, proceeding")
                    except Exception:
                        pass
                else:
                    # OANDA pre-fetch failed — fall back to DB check (fail-safe)
                    try:
                        _lt_conn = get_trading_forex()
                        _lt_open = _lt_conn.execute(
                            "SELECT COUNT(*) FROM live_trades WHERE pair=? AND status='open' AND user_id=?",
                            (instrument, user_id),
                        ).fetchone()
                        if _lt_open and _lt_open[0] > 0:
                            logger.info(
                                "[WATCH] %s #%d: OVERLAP GUARD — OANDA unavailable, DB shows open position — skipping",
                                instrument, watch_id,
                            )
                            try:
                                flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                              data={"watch_id": watch_id, "gate": "overlap_db_open",
                                                    "met_count": met_count, "total_count": total_count},
                                              status="skip",
                                              note=f"Watch #{watch_id} {met_count}/{total_count} blocked: DB shows open position (OANDA unavailable)")
                            except Exception:
                                pass
                            continue
                    except Exception as _lt_err:
                        logger.debug(
                            "[WATCH] %s: live_trades overlap check failed (%s) — allowing trigger (OANDA gate is backup)",
                            instrument, _lt_err,
                        )

            if is_re_fire:
                triggered.append({
                    "watch_id": watch_id,
                    "instrument": instrument,
                    "suggestion_type": watch["suggestion_type"],
                    "conditions_met": result["details"],
                    "raw_suggestion": watch["raw_suggestion"],
                    "trigger_type": f"re-fire ({met_count}/{total_count} conditions currently met)",
                    "live_sniper_buy": _live_buy,
                    "live_sniper_sell": _live_sell,
                    "live_direction": _live_direction,
                    "watch_direction": _watch_dir,  # 2026-04-17: watch thesis direction takes priority
                    "fan_direction": _live_fan_dir,
                    "fan_state": _live_fan_state,
                    "finding_id": watch["finding_id"],  # 2026-03-26: unified tracking fix
                })
                fired_this_cycle.add(instrument)
                logger.info("🔄 Watch %d re-firing for %s (%d/%d conditions currently met)",
                            watch_id, instrument, met_count, total_count)
            elif should_trigger:
                trigger_type = "full" if result["met"] else f"threshold ({met_count}/{total_count} = {progress_pct:.0%})"
                conn.execute(
                    "UPDATE watch_suggestions SET status='triggered', triggered_at=? WHERE id=?",
                    (now.isoformat(), watch_id)
                )
                if watch["workspace_task_id"]:
                    try:
                        ws_conn = get_workspaces()
                        ws_conn.execute(
                            "UPDATE workspace_tasks SET status='triggered' WHERE id=?",
                            (watch["workspace_task_id"],)
                        )
                        ws_conn.commit()
                    except Exception:
                        pass
                triggered.append({
                    "watch_id": watch_id,
                    "instrument": instrument,
                    "suggestion_type": watch["suggestion_type"],
                    "conditions_met": result["details"],
                    "raw_suggestion": watch["raw_suggestion"],
                    "trigger_type": trigger_type,
                    "live_sniper_buy": _live_buy,
                    "live_sniper_sell": _live_sell,
                    "live_direction": _live_direction,
                    "watch_direction": _watch_dir,  # 2026-04-17: watch thesis direction takes priority
                    "fan_direction": _live_fan_dir,
                    "fan_state": _live_fan_state,
                    "finding_id": watch["finding_id"],  # 2026-03-26: unified tracking fix
                })
                fired_this_cycle.add(instrument)
                logger.info("🎯 Watch %d TRIGGERED (%s) for %s: %s",
                           watch_id, trigger_type, instrument, watch["raw_suggestion"])

                # ── Telegram: sniper fired notification ──────────────────────
                # Gate: skip notification if a trade is already open on this pair.
                # Fails SAFE — if OANDA is unreachable we skip the notification
                # rather than flooding with duplicates.
                _notify_ok = False
                try:
                    try:
                        from broker_credentials import BrokerCredentials as _BC_wm
                    except ImportError:
                        from Source.broker_credentials import BrokerCredentials as _BC_wm
                    import requests as _rq_wm
                    _cred_wm = _BC_wm().get_connection(user_id=user_id, broker="oanda")
                    _open_wm = _rq_wm.get(
                        f"{_cred_wm['base_url']}/v3/accounts/{_cred_wm['account_id']}/openTrades",
                        headers={"Authorization": f"Bearer {_cred_wm['api_key']}"},
                        timeout=4,
                    ).json().get("trades", [])
                    if not any(t2.get("instrument") == instrument for t2 in _open_wm):
                        _notify_ok = True
                    else:
                        logger.info("[WATCH] %s #%d: suppressing notification — trade already open",
                                    instrument, watch_id)
                except Exception as _oanda_wm_err:
                    logger.warning("[WATCH] %s #%d: OANDA check failed (%s) — skipping notification (fail safe)",
                                   instrument, watch_id, _oanda_wm_err)
                    _notify_ok = False  # fail SAFE, not fail open

                if _notify_ok:
                    try:
                        import sys as _sys, os as _os
                        _src_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..')
                        if _src_dir not in _sys.path:
                            _sys.path.insert(0, _src_dir)
                        from trade_notify import notify_sniper_fired
                        notify_sniper_fired(
                            watch_id=watch_id,
                            pair=instrument,
                            conditions_met=met_count,
                            conditions_total=total_count,
                            peak_pct=new_peak * 100,
                            direction=_live_direction,
                        )
                    except Exception as _ntf_e:
                        logger.debug("Sniper fired notification failed: %s", _ntf_e)

                # Record scout-snipe correlation if this was scout-originated
                if watch["agent_name"] == "trade_scout":
                    _record_scout_snipe_correlation(watch_id, instrument)
            else:
                if met_count > 0:
                    logger.info("📊 Watch %d: %d/%d conditions met (%.0f%%) for %s — peak %.0f%%",
                               watch_id, met_count, total_count, progress_pct * 100, instrument, new_peak * 100)
                else:
                    logger.debug("Watch %d: 0/%d conditions met for %s",
                                watch_id, total_count, instrument)
        
        except Exception as exc:
            import traceback as _tb
            _trace_tail = _tb.format_exc().splitlines()
            # Grab the last 3 frames — enough to locate the crash line without bloating flight_log
            _tail = "\n".join(_trace_tail[-6:]) if _trace_tail else ""
            logger.warning("Watch %d check failed: %s\n%s", watch_id, exc, _tail)
            # 2026-04-23: flight_record every silent exception with traceback tail
            # so we can find the crash site without needing to re-run.
            try:
                flight.record(FlightStage.WATCH_EXCEPTION, pair=instrument,
                              data={"watch_id": watch_id, "error": str(exc)[:200],
                                    "error_type": type(exc).__name__,
                                    "trace_tail": _tail[:600],
                                    "suggestion_type": _sug_type if '_sug_type' in dir() else ""},
                              status="error",
                              note=f"Watch #{watch_id} {instrument} silently skipped: {type(exc).__name__}")
            except Exception:
                pass
            # CRITICAL: rollback any partial transaction from this watch's failed UPDATE.
            # Without this, an implicit BEGIN from a prior successful UPDATE holds a
            # RESERVED lock on boardroom.db that blocks ALL other writers until commit.
            try:
                conn.rollback()
            except Exception:
                pass

    conn.commit()
    # Don't close pooled connections
    return triggered


# ---------------------------------------------------------------------------
# Similarity-based dedup helpers
# ---------------------------------------------------------------------------

def _conditions_signature(conditions: list, watch_config: dict = None,
                          cycle_context: dict = None) -> frozenset:
    """Identity-aware Jaccard signature for snipe deduplication.

    Two snipes only collide when they're the same trade — same direction, same
    setup type, same conditions WITH similar threshold values. Bumped from the
    old field|op-only signature 2026-05-07 because field|op alone collided
    genuinely different setups (C4_CHART_PATTERN_BREAK vs V4_continuation) at
    71% Jaccard, suppressing fresh validator output.

    Tokens emitted:
      - cond|<field>|<op>|<value>     # one per condition (value bucketed lightly)
      - dir|<BUY|SELL>                # direction (locks in even though pre-filtered)
      - setup|<setup_name>            # C4_CHART_PATTERN_BREAK ≠ V4_continuation
      - desc|<first 60 chars>         # fallback when field is empty
    """
    sigs = []
    for c in (conditions or []):
        field = c.get("field", "")
        op = c.get("op", "")
        # Prefer 'value' (the actual condition schema), fall back to 'threshold'.
        val = c.get("value", c.get("threshold"))
        if field:
            if val is None:
                sigs.append(f"cond|{field}|{op}")
            else:
                try:
                    v = float(val)
                    # Round to 4 decimals: 1 pip on non-JPY, 0.01 pip on JPY.
                    # Tight enough that "RSI 50" and "RSI 30" differ; loose enough
                    # to swallow trailing-float noise like 50.0 vs 50.00001.
                    sigs.append(f"cond|{field}|{op}|{round(v, 4)}")
                except (TypeError, ValueError):
                    # Non-numeric (booleans, strings) — use as-is.
                    sigs.append(f"cond|{field}|{op}|{val}")
        else:
            desc = c.get("desc", "")[:60]
            if desc:
                sigs.append(f"desc|{desc}")

    # Identity tokens — direction + setup name are the strongest "same trade" signals.
    wc = watch_config or {}
    cc = cycle_context or {}
    direction = (
        wc.get("re_entry_direction")
        or cc.get("re_entry_direction")
        or cc.get("direction")
        or ""
    )
    if direction:
        sigs.append(f"dir|{str(direction).upper()}")

    setup = (
        cc.get("setup_name")
        or cc.get("setup_id")
        or cc.get("alert_type")
        or wc.get("setup_name")
        or wc.get("alert_type")
        or ""
    )
    if setup:
        sigs.append(f"setup|{setup}")

    return frozenset(sigs)


def _jaccard_similarity(sig_a: frozenset, sig_b: frozenset) -> float:
    """Jaccard similarity between two condition signature sets. 0.0–1.0."""
    if not sig_a and not sig_b:
        return 1.0
    if not sig_a or not sig_b:
        return 0.0
    intersection = sig_a & sig_b
    union = sig_a | sig_b
    return len(intersection) / len(union) if union else 0.0


# ---------------------------------------------------------------------------
# Record trade outcome for a watch suggestion
# ---------------------------------------------------------------------------

def _compute_conditions_hash(conditions: list, instrument: str, direction: str) -> str:
    """Stable hash for a conditions set — used to deduplicate and aggregate leaderboard entries.

    Fields and operators are normalised; threshold values are bucketed so minor
    numeric differences don't create separate leaderboard rows.
    """
    import hashlib
    canonical = []
    for c in sorted(conditions, key=lambda x: x.get("field", "")):
        field = c.get("field", "")
        op    = c.get("op", "")
        val   = c.get("value")
        # Bucket numeric thresholds to nearest 5 so 28 and 32 hash the same
        if isinstance(val, (int, float)):
            val = round(val / 5) * 5
        elif isinstance(val, list):
            val = sorted(str(v) for v in val)
        canonical.append(f"{field}:{op}:{val}")
    raw = f"{instrument}|{direction.upper()}|{'|'.join(canonical)}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _upsert_leaderboard(conn, watch_id: int, instrument: str,
                        conditions: list, suggestion_type: str,
                        direction: str, outcome: str, pips: float) -> None:
    """Upsert snipe_leaderboard row for this conditions fingerprint.

    Called by record_outcome — aggregates win rate / pips across all watches
    that share the same conditions pattern on the same pair.
    """
    cond_hash = _compute_conditions_hash(conditions, instrument, direction)
    now_iso   = datetime.now(timezone.utc).isoformat()
    won       = 1 if outcome == "win" else 0

    existing = conn.execute(
        "SELECT id, times_triggered, times_won, total_pips FROM snipe_leaderboard "
        "WHERE conditions_hash=? AND instrument=?",
        (cond_hash, instrument)
    ).fetchone()

    if existing:
        tid  = existing[1] + 1
        twon = existing[2] + won
        tpip = existing[3] + pips
        wr   = round(twon / tid * 100, 1) if tid else 0
        avg  = round(tpip / tid, 1)       if tid else 0
        conn.execute("""
            UPDATE snipe_leaderboard
            SET times_triggered=?, times_won=?, total_pips=?,
                avg_pips=?, win_rate=?,
                last_triggered_at=?, last_updated_at=?
            WHERE conditions_hash=? AND instrument=?
        """, (tid, twon, tpip, avg, wr, now_iso, now_iso, cond_hash, instrument))
    else:
        wr  = 100.0 if won else 0.0
        conn.execute("""
            INSERT INTO snipe_leaderboard
            (conditions_hash, instrument, conditions, suggestion_type,
             times_created, times_triggered, times_won, total_pips,
             avg_pips, win_rate, last_triggered_at, last_updated_at)
            VALUES (?,?,?,?, 1,1,?,?,?,?, ?,?)
        """, (cond_hash, instrument, json.dumps(conditions), suggestion_type,
              won, pips, round(pips, 1), wr, now_iso, now_iso))

    conn.commit()
    logger.info("[leaderboard] %s %s hash=%s → outcome=%s pips=%.1f",
                instrument, direction, cond_hash, outcome, pips)


def record_outcome(watch_id: int, trade_cycle_id: str,
                   outcome: str, pips: float = 0.0,
                   trade_id: str = None) -> None:
    """Record the result of a trade triggered by a watch suggestion.

    Also aggregates into snipe_leaderboard so win rate is tracked per
    conditions fingerprint across all watches that share that pattern.

    Args:
        trade_id: OANDA trade ID (stored in trade_id column for lineage tracking).
                  Falls back to trade_cycle_id if not provided.
    """
    conn = get_trading_forex()
    _tid = trade_id or trade_cycle_id
    conn.execute("""
        UPDATE watch_suggestions
        SET trade_cycle_id=?, trade_outcome=?, pips_result=?, status='completed',
            trade_id=COALESCE(trade_id, ?)
        WHERE id=?
    """, (trade_cycle_id, outcome, pips, _tid, watch_id))
    if conn.total_changes:
        try:
            ws_task_id = conn.execute(
                "SELECT workspace_task_id FROM watch_suggestions WHERE id=?",
                (watch_id,)
            ).fetchone()
            if ws_task_id and ws_task_id[0]:
                ws_conn = get_workspaces()
                ws_conn.execute(
                    "UPDATE workspace_tasks SET status=? WHERE id=?",
                    (f"completed_{outcome}", ws_task_id[0])
                )
                ws_conn.commit()
        except Exception:
            pass
    conn.commit()

    # ── Aggregate into leaderboard ──────────────────────────────────────────
    try:
        row = conn.execute(
            "SELECT instrument, conditions, suggestion_type, context "
            "FROM watch_suggestions WHERE id=?", (watch_id,)
        ).fetchone()
        if row:
            instrument, conds_raw, stype, ctx_raw = row
            conditions = json.loads(conds_raw or "[]")
            ctx        = json.loads(ctx_raw  or "{}")
            direction  = ctx.get("re_entry_direction") or ctx.get("direction", "unknown")
            _upsert_leaderboard(conn, watch_id, instrument, conditions,
                                stype or "unknown", direction, outcome, pips)
    except Exception as _lb_err:
        logger.warning("[leaderboard] update failed for watch #%d: %s", watch_id, _lb_err)
    # ── Write outcome back to scout_alerts if this watch came from a scout alert ──
    # This closes the full feedback loop: alert fired → snipe created → trade → outcome
    try:
        watch_row = conn.execute(
            "SELECT context FROM watch_suggestions WHERE id=?", (watch_id,)
        ).fetchone()
        if watch_row:
            ctx = json.loads(watch_row[0] or "{}")
            _scout_alert_id = ctx.get("scout_alert_id") or ctx.get("scout_context", {}).get("scout_alert_id")
            if _scout_alert_id:
                _tc = get_trading_forex()
                _tc.execute("""
                    UPDATE scout_alerts
                    SET snipe_triggered=1,
                        trade_executed=1,
                        outcome=?,
                        pips_result=?,
                        resolution_timestamp=datetime('now')
                    WHERE id=?
                """, (outcome, pips, _scout_alert_id))
                logger.info("[scout_outcome] Wrote %s %.1fp back to scout_alerts #%d",
                            outcome, pips, _scout_alert_id)
    except Exception as _so_err:
        # 2026-04-24: upgraded — scout_outcome writeback links snipes back to
        # scout alerts. Silent failure = scout_alerts.outcome drifts NULL.
        logger.warning("[scout_outcome] write-back FAILED: %s: %s (scout_alerts outcome drift)",
                       type(_so_err).__name__, _so_err)
    # Don't close pooled connections


# ---------------------------------------------------------------------------
# Get suggestion performance stats (for validator feedback)
# ---------------------------------------------------------------------------

def get_suggestion_stats() -> Dict[str, Any]:
    """Get performance stats for validator suggestions.
    
    Returns breakdown by suggestion_type: trigger rate, win rate, avg pips.
    This data feeds back into the validator prompt so it learns which
    suggestions actually work.
    """
    _ensure_tables()
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            suggestion_type,
            COUNT(*) as total,
            SUM(CASE WHEN status = 'triggered' OR status = 'completed' THEN 1 ELSE 0 END) as triggered,
            SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) as expired,
            SUM(CASE WHEN trade_outcome = 'win' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN trade_outcome = 'loss' THEN 1 ELSE 0 END) as losses,
            AVG(CASE WHEN pips_result IS NOT NULL THEN pips_result END) as avg_pips,
            AVG(check_count) as avg_checks
        FROM watch_suggestions
        GROUP BY suggestion_type
    """).fetchall()
    
    stats = {}
    for r in rows:
        total = r["total"]
        triggered = r["triggered"] or 0
        wins = r["wins"] or 0
        losses = r["losses"] or 0
        completed = wins + losses
        
        stats[r["suggestion_type"]] = {
            "total": total,
            "triggered": triggered,
            "expired": r["expired"] or 0,
            "trigger_rate": round(triggered / total * 100, 1) if total else 0,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / completed * 100, 1) if completed else None,
            "avg_pips": round(r["avg_pips"], 1) if r["avg_pips"] else None,
            "avg_checks_to_trigger": round(r["avg_checks"], 1) if r["avg_checks"] else None,
        }
    
    # Don't close pooled connections
    return stats


# ---------------------------------------------------------------------------
# Get active watches for dashboard display
# ---------------------------------------------------------------------------

def get_active_watches(user_id: int = None) -> List[Dict[str, Any]]:
    """Get active (watching) entries for dashboard display.

    Args:
        user_id: If provided, filter to this user only. None returns all users
                 (for backward compat / internal timer use).
    """
    _ensure_tables()
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row

    _base_where = """
        (status = 'watching'
           OR (status = 'triggered'
               AND triggered_at >= datetime('now', '-30 minutes'))
           OR (status = 'triggered'
               AND trade_cycle_id IS NOT NULL))
    """
    if user_id is not None:
        rows = conn.execute(f"""
            SELECT id, instrument, suggestion_type, conditions, raw_suggestion,
                   created_at, expires_at, last_checked_at, check_count, status,
                   workspace_task_id, context,
                   conditions_progress, conditions_met_count, conditions_total_count, peak_progress,
                   criteria_hit_rate, criteria_scan_count, criteria_met_count as criteria_met_count_weekly,
                   last_graded_at, stale_flagged_at
            FROM watch_suggestions
            WHERE user_id = ? AND {_base_where}
            ORDER BY
                CASE status WHEN 'watching' THEN 0 WHEN 'triggered' THEN 1 ELSE 2 END,
                created_at DESC
        """, (user_id,)).fetchall()
    else:
        rows = conn.execute(f"""
            SELECT id, instrument, suggestion_type, conditions, raw_suggestion,
                   created_at, expires_at, last_checked_at, check_count, status,
                   workspace_task_id, context,
                   conditions_progress, conditions_met_count, conditions_total_count, peak_progress,
                   criteria_hit_rate, criteria_scan_count, criteria_met_count as criteria_met_count_weekly,
                   last_graded_at, stale_flagged_at
            FROM watch_suggestions
            WHERE {_base_where}
            ORDER BY
                CASE status WHEN 'watching' THEN 0 WHEN 'triggered' THEN 1 ELSE 2 END,
                created_at DESC
        """).fetchall()
    
    watches = []
    for r in rows:
        conditions = []
        try:
            conditions = json.loads(r["conditions"])
        except (json.JSONDecodeError, TypeError):
            pass
        
        ctx = {}
        try:
            ctx = json.loads(r["context"]) if r["context"] else {}
        except (json.JSONDecodeError, TypeError):
            pass

        # Parse live condition progress
        cond_progress = []
        try:
            cond_progress = json.loads(r["conditions_progress"]) if r["conditions_progress"] else []
        except (json.JSONDecodeError, TypeError):
            pass

        # ── Staleness grade ─────────────────────────────────────────────────
        # Composite score 0–100: higher = staler / more likely worthless.
        # Components:
        #   age_score      (0–40): linearly scales from 0 at 0h to 40 at 72h+
        #   progress_score (0–30): penalty for low conditions hit rate over time
        #                          (a watch that's old AND never progressed = bad)
        #   direction_note       : flag if context direction != current fan (CRO does this)
        now_dt = datetime.now(timezone.utc)
        try:
            created_dt = _safe_isoformat(r["created_at"])
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            age_h = (now_dt - created_dt).total_seconds() / 3600
        except Exception:
            age_h = 0

        met   = r["conditions_met_count"] or 0
        total = r["conditions_total_count"] or 1
        hit_rate = met / total if total else 0

        # ── P1 Criteria grading fields ───────────────────────────────────────
        criteria_hit_rate    = r["criteria_hit_rate"]         # float or None
        criteria_scan_count  = r["criteria_scan_count"] or 0
        criteria_met_weekly  = r["criteria_met_count_weekly"] or 0
        last_graded_at       = r["last_graded_at"]
        stale_flagged_at     = r["stale_flagged_at"]

        # Staleness is purely about conditions progress — age is irrelevant.
        # A snipe waiting 3 days with 4/5 conditions is NOT stale.
        # Prefer weekly criteria_hit_rate when we have enough scans; fall back
        # to per-scan conditions ratio for new watches.
        checks = r["check_count"] or 0
        if criteria_hit_rate is not None and criteria_scan_count >= 100:
            # Use criteria hit-rate (fraction of scans with ≥50% conditions met)
            if criteria_hit_rate < 0.10:
                progress_score = 70
            elif criteria_hit_rate < 0.30:
                progress_score = 45
            elif criteria_hit_rate >= 0.60:
                progress_score = 0
            elif criteria_hit_rate >= 0.40:
                progress_score = 10
            else:
                progress_score = max(0, (1 - criteria_hit_rate) * 20)
        else:
            # Fallback: per-scan conditions ratio (existing logic)
            if checks > 50 and hit_rate == 0:
                progress_score = 70   # many checks, zero progress — genuinely stuck
            elif checks > 20 and hit_rate < 0.2:
                progress_score = 45
            elif hit_rate >= 0.6:
                progress_score = 0    # progressing well
            elif hit_rate >= 0.4:
                progress_score = 10
            else:
                progress_score = max(0, (1 - hit_rate) * 20)

        staleness_score = round(progress_score)
        staleness_label = (
            "fresh"   if staleness_score < 15 else
            "aging"   if staleness_score < 35 else
            "stale"   if staleness_score < 60 else
            "expired"
        )

        # ── Leaderboard stats for this conditions fingerprint ────────────────
        direction = ctx.get("re_entry_direction") or ctx.get("direction", "unknown")
        # Kronos snipes store conditions as a single dict, not a list —
        # normalize to list for the hash function
        _hash_conds = conditions if isinstance(conditions, list) else [conditions] if isinstance(conditions, dict) else []
        cond_hash = _compute_conditions_hash(_hash_conds, r["instrument"], direction)
        lb_stats  = None
        try:
            lb_row = conn.execute(
                "SELECT times_triggered, times_won, avg_pips, win_rate "
                "FROM snipe_leaderboard WHERE conditions_hash=? AND instrument=?",
                (cond_hash, r["instrument"])
            ).fetchone()
            if lb_row:
                lb_stats = {
                    "times_triggered": lb_row[0],
                    "times_won":       lb_row[1],
                    "avg_pips":        lb_row[2],
                    "win_rate":        lb_row[3],
                }
        except Exception:
            pass

        watches.append({
            "id": r["id"],
            "instrument": r["instrument"],
            "suggestion_type": r["suggestion_type"],
            "conditions": conditions,
            "raw_suggestion": r["raw_suggestion"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "last_checked_at": r["last_checked_at"],
            "check_count": r["check_count"],
            "status": r["status"],
            "workspace_task_id": r["workspace_task_id"],
            "context": ctx,
            "conditions_progress": cond_progress,
            "conditions_met_count": met,
            "conditions_total_count": total,
            "peak_progress": r["peak_progress"] or 0,
            # Staleness / grading fields
            "age_hours":            round(age_h, 1),
            "staleness_score":      staleness_score,
            "staleness_label":      staleness_label,
            "conditions_hash":      cond_hash,
            "direction":            direction,
            "leaderboard":          lb_stats,
            # P1 weekly criteria grading
            "criteria_hit_rate":    round(criteria_hit_rate, 3) if criteria_hit_rate is not None else None,
            "criteria_scan_count":  criteria_scan_count,
            "criteria_met_count":   criteria_met_weekly,
            "last_graded_at":       last_graded_at,
            "stale_flagged_at":     stale_flagged_at,
            "is_active":            r["status"] == "watching",
        })
    
    # Don't close pooled connections
    return watches


def _record_scout_snipe_correlation(snipe_id: int, instrument: str):
    """Record correlation between scout finding and snipe trigger."""
    try:
        import os
        scout_db_path = str(Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "trading_forex.db")
        
        # Find the most recent scout finding for this pair
        conn = get_trading_forex()
        scout_finding = conn.execute("""
            SELECT id, timestamp, setup_type, confidence_score
            FROM scout_findings
            WHERE pair = ? AND created_at > datetime('now', '-24 hours')
            ORDER BY created_at DESC LIMIT 1
        """, (instrument,)).fetchone()
        # Don't close pooled connections
        
        if scout_finding:
            # Record the correlation in trading_forex.db
            boardroom_conn = get_trading_forex()
            snipe_created_time = boardroom_conn.execute(
                "SELECT created_at FROM watch_suggestions WHERE id = ?", (snipe_id,)
            ).fetchone()
            
            if snipe_created_time:
                # Calculate time gap
                from datetime import datetime
                scout_time = _safe_isoformat(scout_finding[1])
                snipe_time = _safe_isoformat(snipe_created_time[0])
                gap_minutes = int((snipe_time - scout_time).total_seconds() / 60)
                
                boardroom_conn.execute("""
                    INSERT INTO scout_snipe_correlation 
                    (pair, scout_finding_id, snipe_id, correlation_type,
                     scout_timestamp, snipe_timestamp, time_gap_minutes, both_triggered)
                    VALUES (?, ?, ?, 'scout_to_snipe', ?, ?, ?, 1)
                """, (
                    instrument, scout_finding[0], snipe_id, 
                    scout_finding[1], snipe_created_time[0], gap_minutes
                ))
                boardroom_conn.commit()
                logger.info(f"Recorded scout-snipe correlation: scout#{scout_finding[0]} -> snipe#{snipe_id} ({gap_minutes}min gap)")
            
            # Don't close pooled connections
            
    except Exception as e:
        logger.warning(f"Failed to record scout-snipe correlation: {e}")

def get_watches_for_validator(instrument: str) -> str:
    """Return a compact read-only summary of active watches for this pair.

    Injected into the validator prompt so it can:
    - Avoid creating a duplicate watch with identical conditions
    - Note if a conditions-matched watch is already progressing well
    - Flag if a new snipe would directly contradict an existing one

    Returns an empty string if no active watches exist (clean cycle).
    The validator can READ and SUGGEST — it cannot cancel or modify watches.
    Only the user can cancel a watch.
    """
    try:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, suggestion_type, conditions, context,
                   conditions_met_count, conditions_total_count,
                   peak_progress, created_at, check_count
            FROM watch_suggestions
            WHERE instrument=? AND status='watching'
            ORDER BY created_at DESC
        """, (instrument,)).fetchall()

        if not rows:
            return ""

        now_dt = datetime.now(timezone.utc)
        lines  = [f"### Active Watches for {instrument} (read-only — only the user can cancel)"]
        lines.append("These are conditions you previously set. Check if your new analysis matches any existing watch before creating a duplicate.\n")

        for r in rows:
            try:
                created_dt = _safe_isoformat(r["created_at"])
                age_h = (now_dt - created_dt).total_seconds() / 3600
            except Exception:
                age_h = 0

            ctx = {}
            try:
                ctx = json.loads(r["context"] or "{}")
            except Exception:
                pass

            direction = ctx.get("re_entry_direction") or ctx.get("direction", "?")
            met       = r["conditions_met_count"] or 0
            total     = r["conditions_total_count"] or 1
            peak      = r["peak_progress"] or 0

            # Leaderboard stats
            conds     = json.loads(r["conditions"] or "[]")
            cond_hash = _compute_conditions_hash(conds, instrument, direction)
            lb_line   = ""
            try:
                lb = conn.execute(
                    "SELECT times_triggered, win_rate, avg_pips FROM snipe_leaderboard "
                    "WHERE conditions_hash=? AND instrument=?", (cond_hash, instrument)
                ).fetchone()
                if lb and lb[0]:
                    lb_line = f" | Historical: {lb[0]} triggers, {lb[1]:.0f}% WR, avg {lb[2]:+.1f}p"
            except Exception:
                pass

            # Staleness flag
            stale_flag = " ⚠️ STALE" if age_h > 48 else ""

            lines.append(
                f"- Watch #{r['id']} [{direction.upper()}] {r['suggestion_type']} | "
                f"Progress: {met}/{total} conditions met | Peak: {peak:.0%} | "
                f"Age: {age_h:.1f}h | Checks: {r['check_count']}{lb_line}{stale_flag}"
            )

            # Show unmet conditions so validator knows what's still missing
            try:
                cond_prog = json.loads(ctx.get("conditions_progress") or r["conditions"] or "[]")
                unmet = [c.get("field", c.get("desc", "?"))
                         for c in (cond_prog if isinstance(cond_prog, list) else [])
                         if not c.get("met", False)]
                if unmet:
                    lines.append(f"  Still waiting for: {', '.join(str(u) for u in unmet[:4])}")
            except Exception:
                pass

        lines.append(
            "\n**If your new analysis produces identical conditions to an existing watch → "
            "do NOT create a duplicate SNIPE. Instead, note the existing watch ID and confirm "
            "it is still valid.**\n"
            "**If a watch looks stale or its direction contradicts what you now see → "
            "flag it as a suggestion to the user but do NOT cancel it yourself.**\n"
        )
        return "\n".join(lines) + "\n\n"

    except Exception as e:
        # 2026-04-24: upgraded — silent failure returns empty list, validator
        # thinks there are no active watches on this pair and may re-suggest.
        logger.warning("get_watches_for_validator FAILED for %s: %s: %s (validator may re-suggest duplicate watches)",
                       instrument, type(e).__name__, e)
        return ""


def cancel_watch(watch_id: int) -> bool:
    """Cancel an active watch."""
    conn = get_trading_forex()
    conn.execute(
        "UPDATE watch_suggestions SET status='cancelled' WHERE id=? AND status IN ('watching','triggered')",
        (watch_id,)
    )
    changed = conn.total_changes > 0
    if changed:
        try:
            ws_task_id = conn.execute(
                "SELECT workspace_task_id FROM watch_suggestions WHERE id=?",
                (watch_id,)
            ).fetchone()
            if ws_task_id and ws_task_id[0]:
                ws_conn = get_workspaces()
                ws_conn.execute(
                    "UPDATE workspace_tasks SET status='cancelled' WHERE id=?",
                    (ws_task_id[0],)
                )
                ws_conn.commit()
        except Exception:
            pass
    conn.commit()
    # Don't close pooled connections
    return changed


def purge_old_watches(days: int = 7) -> int:
    """Delete cancelled/expired watches older than N days. Keeps triggered for analysis."""
    conn = get_trading_forex()
    cur = conn.execute(
        "DELETE FROM watch_suggestions WHERE status IN ('cancelled','expired') "
        "AND created_at < datetime('now', ?)",
        (f'-{days} days',)
    )
    deleted = cur.rowcount
    conn.commit()
    # Don't close pooled connections
    if deleted:
        logger.info("Purged %d old cancelled/expired watches (>%d days)", deleted, days)
    return deleted


def get_scout_snipe_correlation_analysis(lookback_hours: int = 168) -> Dict[str, Any]:
    """Analyze correlation between scout findings and snipe triggers."""
    _ensure_tables()
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    
    since_time = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    
    # Get correlation data
    correlations = conn.execute("""
        SELECT c.*, 
               sf.setup_type as scout_setup,
               sf.confidence_score as scout_confidence,
               sf.outcome as scout_outcome,
               sf.pips_result as scout_pips,
               ws.trade_outcome as snipe_outcome,
               ws.pips_result as snipe_pips
        FROM scout_snipe_correlation c
        LEFT JOIN (SELECT * FROM main.scout_findings) sf ON c.scout_finding_id = sf.id
        LEFT JOIN watch_suggestions ws ON c.snipe_id = ws.id
        WHERE c.created_at > ?
        ORDER BY c.created_at DESC
    """, (since_time,)).fetchall()
    
    analysis = {
        'total_correlations': len(correlations),
        'scout_to_snipe_success': 0,
        'both_successful': 0,
        'scout_success_snipe_fail': 0,
        'scout_fail_snipe_success': 0,
        'both_failed': 0,
        'avg_time_gap_minutes': 0,
        'pair_breakdown': {},
        'setup_performance': {},
        'insights': []
    }
    
    if not correlations:
        conn.close()
        return analysis
    
    # Analyze correlations
    time_gaps = []
    pair_stats = {}
    
    for corr in correlations:
        pair = corr['pair']
        scout_outcome = corr['scout_outcome']
        snipe_outcome = corr['snipe_outcome']
        
        if corr['time_gap_minutes'] is not None:
            time_gaps.append(corr['time_gap_minutes'])
        
        # Track by pair
        if pair not in pair_stats:
            pair_stats[pair] = {'total': 0, 'both_success': 0, 'scout_wins': 0, 'snipe_wins': 0}
        
        pair_stats[pair]['total'] += 1
        
        # Analyze outcomes
        scout_won = scout_outcome == 'win'
        snipe_won = snipe_outcome == 'win'
        
        if scout_won:
            pair_stats[pair]['scout_wins'] += 1
            analysis['scout_to_snipe_success'] += 1
        
        if snipe_won:
            pair_stats[pair]['snipe_wins'] += 1
        
        if scout_won and snipe_won:
            analysis['both_successful'] += 1
            pair_stats[pair]['both_success'] += 1
        elif scout_won and not snipe_won:
            analysis['scout_success_snipe_fail'] += 1
        elif not scout_won and snipe_won:
            analysis['scout_fail_snipe_success'] += 1
        elif scout_outcome and snipe_outcome:  # Both have outcomes
            analysis['both_failed'] += 1
    
    # Calculate statistics
    if time_gaps:
        analysis['avg_time_gap_minutes'] = sum(time_gaps) / len(time_gaps)
    
    analysis['pair_breakdown'] = pair_stats
    
    # Generate insights
    insights = []
    
    total_with_outcomes = analysis['both_successful'] + analysis['scout_success_snipe_fail'] + \
                         analysis['scout_fail_snipe_success'] + analysis['both_failed']
    
    if total_with_outcomes > 0:
        both_success_rate = analysis['both_successful'] / total_with_outcomes
        insights.append(f"When both scout and snipe trigger, success rate is {both_success_rate:.1%}")
        
        if analysis['scout_success_snipe_fail'] > analysis['scout_fail_snipe_success']:
            insights.append("Scout findings tend to be more reliable than snipe triggers")
        elif analysis['scout_fail_snipe_success'] > analysis['scout_success_snipe_fail']:
            insights.append("Snipe triggers show better follow-through than scout findings")
        else:
            insights.append("Scout and snipe systems show similar reliability")
    
    if analysis['avg_time_gap_minutes'] > 0:
        insights.append(f"Average time from scout finding to snipe trigger: {analysis['avg_time_gap_minutes']:.1f} minutes")
    
    analysis['insights'] = insights
    
    # Don't close pooled connections
    return analysis


# ---------------------------------------------------------------------------
# Scout integration — create snipes from trade_scout alerts
# ---------------------------------------------------------------------------

def create_watch_from_win(trade_data: dict) -> int:
    """Create a story-aware snipe from a winning trade.

    When a trade wins, we capture the market conditions that led to the win
    and save them as a snipe so the scout can find similar setups later.

    Expected trade_data keys:
        pair, direction, setup_name, entry_type (story thesis type),
        fan_state, fan_direction, momentum_state, momentum_significance,
        e100_interaction, wick_pressure, body_trend, opportunity_score,
        pnl_pips, pnl_usd, win_rate (setup's overall), trade_count,
        profit_factor, scout_confidence
    
    Returns the new watch ID, or -1 if skipped.
    """
    _ensure_tables()
    import hashlib
    now = datetime.now(timezone.utc)

    pair = trade_data.get('pair', '')
    direction = trade_data.get('direction', 'bullish')
    entry_type = trade_data.get('entry_type', 'unknown')
    setup_name = trade_data.get('setup_name', entry_type)
    fan_state = trade_data.get('fan_state', 'unknown')
    fan_direction = trade_data.get('fan_direction', 'unknown')
    momentum_state = trade_data.get('momentum_state', 'unknown')
    momentum_sig = trade_data.get('momentum_significance', 'unknown')
    e100_interaction = trade_data.get('e100_interaction', 'none')
    wick_pressure = trade_data.get('wick_pressure', 'unknown')
    body_trend = trade_data.get('body_trend', 'unknown')
    opp_score = trade_data.get('opportunity_score', 0)

    if not pair:
        logger.warning("create_watch_from_win: no pair — skipping")
        return -1

    # ── Build story-aware conditions ──
    # These describe the market picture that produced the win.
    # When the scout sees similar conditions again, the snipe fires.
    conditions_list = []

    # 1. Must have a story opportunity
    conditions_list.append({
        "field": "story_has_opportunity", "op": "==", "value": True,
        "source": "market_story",
        "desc": "Market story identifies a tradeable thesis",
    })

    # 2. Entry type should match (the kind of setup that won)
    if entry_type and entry_type != 'unknown':
        conditions_list.append({
            "field": "story_entry_type", "op": "==", "value": entry_type,
            "source": "market_story",
            "desc": f"Same thesis type: {entry_type}",
        })

    # 3. Minimum opportunity score (allow some flexibility: -15 from winning score)
    min_score = max(opp_score - 15, 40)
    conditions_list.append({
        "field": "story_opportunity_score", "op": ">=", "value": min_score,
        "source": "market_story",
        "desc": f"Opportunity score ≥{min_score} (won at {opp_score})",
    })

    # 4. EMA fan state — what fan state supported this win?
    if fan_state and fan_state != 'unknown':
        # Build acceptable fan states around the winning state
        fan_groups = {
            'expanding': ['expanding', 'accelerating'],
            'accelerating': ['expanding', 'accelerating'],
            'decelerating': ['decelerating', 'peaked'],
            'peaked': ['decelerating', 'peaked'],
            'contracting': ['contracting', 'peaked', 'just_crossed'],
            'just_crossed': ['just_crossed', 'contracting', 'stable'],
            'stable': ['stable', 'just_crossed', 'contracting'],
        }
        acceptable = fan_groups.get(fan_state, [fan_state])
        conditions_list.append({
            "field": "ema_fan_state", "op": "in", "value": acceptable,
            "source": "ema_narrative",
            "desc": f"EMA fan similar to win ({fan_state})",
        })

    # 5. Momentum must be meaningful
    if momentum_sig and momentum_sig != 'unknown':
        if momentum_sig == 'high':
            conditions_list.append({
                "field": "momentum_significance", "op": "in",
                "value": ["high", "moderate"],
                "source": "market_story",
                "desc": "Momentum significant (won with high)",
            })
        elif momentum_sig == 'moderate':
            conditions_list.append({
                "field": "momentum_significance", "op": "in",
                "value": ["high", "moderate"],
                "source": "market_story",
                "desc": "Momentum significant (won with moderate)",
            })

    # 6. E100 interaction (if it was a factor)
    if e100_interaction and e100_interaction not in ('none', 'unknown', 'away'):
        conditions_list.append({
            "field": "e100_interaction", "op": "in",
            "value": [e100_interaction, "bounce", "test"],
            "source": "market_story",
            "desc": f"E100 interaction similar ({e100_interaction})",
        })

    # ── Human-readable summary ──
    raw_suggestion = (
        f"🏆 Won: {setup_name} on {pair} ({direction}) | "
        f"Story: {entry_type} | Fan: {fan_direction} {fan_state} | "
        f"Score: {opp_score}/100 | "
        f"+{trade_data.get('pnl_pips', 0):.1f} pips"
    )

    cycle_id = "win_" + hashlib.md5(
        (pair + "_" + setup_name + "_" + now.isoformat()).encode()
    ).hexdigest()[:8]

    context = {
        "source": "winning_trade",
        "direction": direction,
        "entry_type": entry_type,
        "setup_name": setup_name,
        "fan_state": fan_state,
        "fan_direction": fan_direction,
        "momentum_state": momentum_state,
        "momentum_significance": momentum_sig,
        "e100_interaction": e100_interaction,
        "wick_pressure": wick_pressure,
        "body_trend": body_trend,
        "opportunity_score": opp_score,
        "pnl_pips": trade_data.get('pnl_pips', 0),
        "pnl_usd": trade_data.get('pnl_usd', 0),
        "win_rate": trade_data.get('win_rate', 0),
        "trade_count": trade_data.get('trade_count', 0),
        "profit_factor": trade_data.get('profit_factor', 0),
    }

    conn = get_trading_forex()

    # Dedup: only ONE active snipe per pair allowed
    existing = conn.execute(
        "SELECT id FROM watch_suggestions WHERE instrument=? AND status='watching' LIMIT 1",
        (pair,)
    ).fetchone()
    if existing:
        logger.info("Skipping win snipe for %s — already has active snipe #%d", pair, existing[0])
        return -1

    cursor = conn.execute("""
        INSERT INTO watch_suggestions
        (cycle_id, instrument, suggestion_type, conditions, raw_suggestion,
         validator_verdict, validator_confidence, created_at, expires_at,
         status, agent_name, context, origin_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'watching', 'winning_trade', ?, 'scout')
    """, (
        cycle_id,
        pair,
        f"replay_{entry_type}",
        json.dumps(conditions_list),
        raw_suggestion,
        direction,
        trade_data.get('scout_confidence', 0.8),
        now.isoformat(),
        '9999-12-31T23:59:59',
        json.dumps(context),
    ))
    watch_id = cursor.lastrowid
    conn.commit()

    logger.info("🏆 Created win snipe #%d for %s: %s (%s, +%.1f pips)",
                watch_id, pair, setup_name, entry_type,
                trade_data.get('pnl_pips', 0))

    if flight:
        flight.record(FlightStage.WIN_SNIPE, pair=pair, trade_id=str(trade_data.get('trade_id', '')), data={
            "watch_id": watch_id,
            "setup_name": setup_name,
            "entry_type": entry_type,
            "pnl_pips": trade_data.get('pnl_pips', 0),
            "conditions": len(conditions_list),
        }, note=f"Win snipe #{watch_id}: {setup_name} ({entry_type})")

    # ── FAN EXHAUSTION/CONTRACTION SNIPE PLACEMENT ──
    # When fan is contracting/peaked, create REVERSE snipe for opposite direction
    if fan_state in ('contracting', 'peaked') and fan_direction in ('bullish', 'bearish'):
        reverse_direction = 'sell' if direction == 'buy' else 'buy'
        reverse_fan_direction = 'bearish' if fan_direction == 'bullish' else 'bullish'
        
        # Build reverse snipe conditions
        reverse_conditions = [
            {
                "field": "story_has_opportunity", "op": "==", "value": True,
                "source": "market_story",
                "desc": "Market story identifies a tradeable thesis",
            },
            {
                "field": "fan_state", "op": "in", 
                "value": ["contracting", "peaked", "just_crossed", "expanding"],
                "source": "market_story", 
                "desc": f"Fan exhausted from {fan_direction} → reversing to {reverse_fan_direction}",
            },
            {
                "field": "bb_expanding", "op": "==", "value": True,
                "source": "market_story",
                "desc": "BB expanding in opposite direction",
            },
            {
                "field": "story_direction", "op": "==", "value": reverse_direction,
                "source": "market_story",
                "desc": f"Story suggests {reverse_direction} (reverse of winning {direction})",
            }
        ]
        
        reverse_cycle_id = "reverse_" + hashlib.md5(
            (pair + "_" + setup_name + "_reverse_" + now.isoformat()).encode()
        ).hexdigest()[:8]
        
        reverse_context = context.copy()
        reverse_context.update({
            "source": "fan_exhaustion_reverse",
            "direction": reverse_direction,
            "original_direction": direction,
            "original_fan_state": fan_state,
            "original_fan_direction": fan_direction,
            "setup_name": f"{setup_name}_reverse",
            "entry_type": "early_expansion",  # Likely to catch early reversals
        })
        
        reverse_suggestion = (
            f"⚡ Fan exhaustion reverse: {pair} {reverse_direction} | "
            f"Original {direction} won with fan {fan_direction} {fan_state} | "
            f"Now reversing to {reverse_fan_direction} expansion"
        )
        
        # Check if there's already a reverse snipe for this pair
        existing_reverse = conn.execute(
            "SELECT id FROM watch_suggestions WHERE instrument=? AND status='watching' AND agent_name='fan_exhaustion' LIMIT 1",
            (pair,)
        ).fetchone()
        
        if not existing_reverse:
            cursor = conn.execute("""
                INSERT INTO watch_suggestions
                (cycle_id, instrument, suggestion_type, conditions, raw_suggestion,
                 validator_verdict, validator_confidence, created_at, expires_at,
                 status, agent_name, context, origin_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'watching', 'fan_exhaustion', ?, 'scout')
            """, (
                reverse_cycle_id,
                pair,
                f"reverse_{entry_type}",
                json.dumps(reverse_conditions),
                reverse_suggestion,
                reverse_direction,
                trade_data.get('scout_confidence', 0.6) * 0.8,  # Slightly lower confidence for reverse
                now.isoformat(),
                (now + timedelta(hours=12)).isoformat(),  # Shorter TTL for reverse snipes
                json.dumps(reverse_context),
            ))
            reverse_watch_id = cursor.lastrowid
            conn.commit()
            
            logger.info("⚡ Created fan exhaustion reverse snipe #%d for %s: %s → %s",
                        reverse_watch_id, pair, direction, reverse_direction)
            
            if flight:
                flight.record(FlightStage.WIN_SNIPE, pair=pair, trade_id=str(trade_data.get('trade_id', '')), data={
                    "watch_id": reverse_watch_id,
                    "setup_name": f"{setup_name}_reverse", 
                    "entry_type": "fan_exhaustion_reverse",
                    "original_pnl_pips": trade_data.get('pnl_pips', 0),
                    "conditions": len(reverse_conditions),
                    "reverse_direction": reverse_direction,
                })
        else:
            logger.info("Skipping fan exhaustion reverse snipe for %s — already has reverse snipe #%d",
                        pair, existing_reverse[0])

    return watch_id


def create_kronos_snipe(
    *,
    conn=None,
    instrument: str,
    direction: str,
    entry_price: float,
    entry_bar: int,
    anchor_time,
    forecast_anchor: str,
    sl_price: float,
    tp_price: float,
    indicators: dict = None,
    fan_direction: str = "",
    fan_state: str = "",
) -> Optional[int]:
    """Create an ephemeral Kronos snipe with Kronos-native conditions.

    Conditions use kronos_* fields (direction, drift, consensus, entry_price).
    The snipe monitor runs a fresh Kronos forecast at check time and evaluates
    live forecast values against these conditions. If Kronos changes its mind,
    the snipe won't fire.

    indicators dict should contain the full Kronos forecast data:
      drift_pips, drift_atr_frac, confidence, consensus, etc.
    """
    if conn is None:
        _ensure_tables()
        conn = get_trading_forex()

    now = anchor_time if hasattr(anchor_time, 'isoformat') else datetime.now(timezone.utc)
    expiry_minutes = (entry_bar + 3) * 15
    expiry_time = now + timedelta(minutes=expiry_minutes)
    ind = indicators or {}

    # Dedup: only replace if direction changed or existing snipe expired.
    _existing = conn.execute(
        "SELECT id, direction FROM watch_suggestions "
        "WHERE instrument=? AND source='kronos_hunter' AND status='watching'",
        (instrument,)
    ).fetchone()
    if _existing:
        _ex_dir = _existing[1] or ""
        if _ex_dir == direction:
            return _existing[0]  # same direction — keep existing
        conn.execute(
            "UPDATE watch_suggestions SET status='replaced' WHERE id=?",
            (_existing[0],)
        )

    # Build conditions from Kronos's actual forecast output.
    # These use kronos_* fields — the snipe monitor runs a fresh Kronos
    # forecast at check time and evaluates live values against these.
    kronos_data = indicators or {}  # holds the full forecast data from hunter
    pip = 0.01 if "JPY" in instrument else 0.0001
    _price_fmt = 3 if pip > 0.001 else 5

    conditions = [
        {
            "field": "kronos_direction", "op": "==",
            "value": direction,
            "source": "kronos_forecast",
            "desc": f"Kronos predicts {direction.upper()} direction",
        },
        {
            "field": "kronos_entry_price", "op": "<=" if direction == "buy" else ">=",
            "value": round(entry_price, _price_fmt),
            "source": "kronos_forecast",
            "desc": f"Price reaches Kronos entry ({round(entry_price, _price_fmt)})",
        },
        {
            "field": "kronos_drift_pips", "op": ">=",
            "value": round(abs(kronos_data.get("drift_pips", 5)), 1),
            "source": "kronos_forecast",
            "desc": f"Kronos drift ≥ {round(abs(kronos_data.get('drift_pips', 5)), 1)}p",
        },
        {
            "field": "kronos_consensus", "op": "==",
            "value": True,
            "source": "kronos_forecast",
            "desc": "Kronos early + terminal bars agree on direction",
        },
    ]

    # Optional: drift_atr_frac if available (signal strength relative to volatility)
    _drift_atr = kronos_data.get("drift_atr_frac", 0)
    if _drift_atr and _drift_atr > 0:
        conditions.append({
            "field": "kronos_drift_atr_frac", "op": ">=",
            "value": round(max(_drift_atr * 0.5, 0.5), 2),  # at least half original strength
            "source": "kronos_forecast",
            "desc": f"Kronos signal strength ≥ {round(max(_drift_atr * 0.5, 0.5), 2)}× ATR",
        })

    # ── Quality-snipe structure conditions (added 2026-04-29) ───────────────
    # Backtest of 2,834 historical kronos signals showed: requiring the chart
    # structure to confirm kronos's direction at fire time lifts WR from 88% →
    # 94%, profit factor from 2.20 → 4.05, max drawdown from 214p → 97p, with
    # 6/6 walk-forward folds positive on +6pp WR delta.
    #
    # Kronos was distilled on TRENDING + RETRACING bars (3x oversampled). When
    # the live chart shows fan opposite kronos's direction at fire, the signal
    # is out-of-distribution and bleeds. These conditions enforce the model's
    # training-distribution match at trigger time.
    _expected_fan = "bullish" if direction == "buy" else "bearish"
    _bb_floor = 8 if "JPY" in instrument else 6
    # ema_fan_direction == bullish/bearish implies EMA ordering (fan_direction
    # is set from EMA order in mkt_picture), so a single gate covers both.
    conditions.append({
        "field": "ema_fan_direction", "op": "==",
        "value": _expected_fan,
        "source": "live_indicators",
        "desc": f"Fan direction must be {_expected_fan} for {direction.upper()}",
    })
    conditions.append({
        "field": "ema_fan_state", "op": "not_in",
        "value": ["peaked", "decelerating"],
        "source": "live_indicators",
        "desc": "Fan must not be exhausted (peaked/decelerating)",
    })
    conditions.append({
        "field": "stoch_zone", "op": "!=",
        "value": "overbought" if direction == "buy" else "oversold",
        "source": "live_indicators",
        "desc": f"Stoch must not be {'overbought' if direction == 'buy' else 'oversold'}",
    })
    conditions.append({
        "field": "rsi_zone", "op": "!=",
        "value": "overbought" if direction == "buy" else "oversold",
        "source": "live_indicators",
        "desc": f"RSI must not be {'overbought' if direction == 'buy' else 'oversold'}",
    })
    conditions.append({
        "field": "bb_width_pips", "op": ">=",
        "value": _bb_floor,
        "source": "live_indicators",
        "desc": f"BB width ≥ {_bb_floor}p (not dead market)",
    })

    conditions_json = json.dumps(conditions)

    # Resolve user_id
    _kronos_uid = None
    try:
        _kronos_uid = conn.execute(
            "SELECT DISTINCT user_id FROM watch_suggestions WHERE user_id IS NOT NULL LIMIT 1"
        ).fetchone()
        _kronos_uid = _kronos_uid[0] if _kronos_uid else None
    except Exception:
        pass

    # Raw suggestion text for the dashboard detail view
    raw_suggestion = (
        f"Kronos forecast predicts {direction.upper()} on {instrument}. "
        f"Path shape: price {'dips' if direction == 'buy' else 'rises'} to "
        f"{round(entry_price, 5 if pip < 0.001 else 3)} at bar {entry_bar} "
        f"({entry_bar * 15} min), then reverses. "
        f"SL: {round(sl_price, 5 if pip < 0.001 else 3)}, "
        f"TP: {round(tp_price, 5 if pip < 0.001 else 3)}. "
        f"Expires in {expiry_minutes} min."
    )

    cur = conn.execute(
        """INSERT INTO watch_suggestions
           (instrument, suggestion_type, conditions, raw_suggestion,
            source, direction, status, created_at, expires_at, expiry_time,
            agent_name, validator_verdict, validator_confidence,
            context, user_id, conditions_total_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instrument,
            "kronos_path_snipe",
            conditions_json,
            raw_suggestion,
            "kronos_hunter",
            direction,
            "watching",
            now.isoformat(),
            expiry_time.isoformat(),
            expiry_time.isoformat(),
            "kronos_hunter",
            "SNIPE",
            0.0,
            json.dumps({
                "entry_price": entry_price,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "direction": direction,
                "entry_bar": entry_bar,
                "forecast_anchor": forecast_anchor,
                "re_entry_direction": direction.upper(),
                "indicators_at_entry": ind,
                "fan_direction": fan_direction,
                "fan_state": fan_state,
            }),
            _kronos_uid,
            len(conditions),
        ),
    )
    conn.commit()
    return cur.lastrowid


def cleanup_expired_kronos(*, conn=None) -> int:
    """Delete expired Kronos snipes. Returns count deleted.

    Only touches rows with source='kronos_hunter' and expiry_time in the past.
    Regular (non-Kronos) snipes are never affected.
    """
    if conn is None:
        conn = get_trading_forex()

    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE watch_suggestions SET status='stale' "
        "WHERE source='kronos_hunter' AND status='watching' "
        "AND expiry_time IS NOT NULL AND expiry_time < ?",
        (now_iso,),
    )
    conn.commit()
    return cur.rowcount
