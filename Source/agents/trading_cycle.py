"""
Trading Cycle -- swarm-driven trading cycle engine.

Orchestrates a complete trading cycle through SwarmHandler, routing all
agent operations via execute_tool(), distribute_tasks(), and
coordinate_parallel() instead of direct Python function calls.

Cycle phases (8-agent architecture):
    1. Pre-check (cycle_orchestrator via execute_tool)
    2. Data collection (oanda_data via execute_tool)
    3. Intelligence gathering (intelligence via execute_tool)
    4. Technical analysis (technical_analyst via execute_tool)
    5. Master Decision (cycle_orchestrator receives all data, calls validator as resource, produces trade plan)
    6. Conditional execution (execution via execute_tool)
    7. Trade monitoring activation (trade_monitor integration when trades open)
    8. Post-trade reporting (reporter via execute_tool)

Each cycle creates a task (via CommentProtocol), runs agents through
SwarmHandler, and produces a complete audit trail as task comments.

Usage::

    from Source.agents.trading_cycle import TradingCycle
    from Source.agents.team_setup import TradingTeamSetup
    from Source.agents.comment_protocol import CommentProtocol

    team = TradingTeamSetup()
    protocol = CommentProtocol()
    cycle = TradingCycle(team, protocol)
    result = cycle.run_cycle("EUR_USD")
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .comment_protocol import CommentProtocol, MessageType
from .team_setup import TradingTeamSetup

# Import db pool with absolute import to avoid relative import issues
import sys
sys.path.append(str(Path(__file__).parent.parent))
from db_pool import get_core, get_trading_forex
# Single source of truth for thesis measurements (fan/BB deltas, cross detection,
# RSI/stoch state, retracement classification). Used by scout, trading_cycle, and
# full_confluence_scorer so manual + scout-driven cycles produce identical inputs.
# See vault: agents/claude-code/2026-05-06-thesis-measurements-refactor.md
try:
    from thesis_measurements import compute_thesis_measurements
except ImportError:
    from Source.thesis_measurements import compute_thesis_measurements

try:
    from tuning_config import get as tc_get
except ImportError:
    tc_get = lambda param, fallback=None: fallback

# Module-level profile engine singleton — set by trading_api_routes at startup
_shared_profile_engine = None

try:
    from flight_recorder import flight, FlightStage
except ImportError:
    try:
        from Source.flight_recorder import flight, FlightStage
    except ImportError:
        flight = None
        FlightStage = None

logger = logging.getLogger("trading_bot.agents.trading_cycle")

_JARVIS_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_V2_DB_DIR = _JARVIS_ROOT / "Database" / "v2"
_TRADING_FOREX_DB = str(_V2_DB_DIR / "trading_forex.db")
_CORE_DB = str(_V2_DB_DIR / "core.db")
_FOREX_DATA_DIR = _JARVIS_ROOT / "Forex Trading Team" / "Data"

# ── Concurrency gate for Anthropic API calls ──────────────────────────────────
# Limits simultaneous Claude calls across all trading cycles running in parallel.
# 5 concurrent calls keeps us well under rate limits (claude-sonnet: 50 RPM).
# threading.Semaphore because trading_cycle runs in threads, not an asyncio loop.
import threading as _threading
_CLAUDE_SEMAPHORE = _threading.Semaphore(5)

# Unified validator singleton (lazy-loaded)
_unified_validator = None


def _compute_session_window(instrument: str, tc_get_fn=None, now_utc=None) -> dict:
    """Evaluate session-window state for a pair at a given moment.

    Returns dict:
      state:           'BLOCKED' (legacy hard gate — block snipe entries) |
                       'CAUTION' (owning session asleep / known chop window — validator
                                  should write WATCH-with-snipe only) |
                       'OPEN'    (normal judgment — model decides) |
                       'PRIME'   (owning session active — model can commit on 6+ checklist)
      reason:          human-readable explanation
      owning_session:  'Tokyo' | 'London' | 'NY' | 'Overlap' | None
      next_open_utc:   'HH:MM UTC (Session)' when owning session re-opens (CAUTION/BLOCKED only)

    Used by:
      - Snipe gate at run_cycle (~line 2932) via _compute_session_gate back-compat wrapper.
      - Validator section build (~line 6620) so the local 35B sees session state and
        applies iter 20e's PRIME-commit / CAUTION-downgrade / BLOCKED-skip rules.

    Session definitions (UTC):
      Tokyo  00-09  — owns JPY pairs and AUD/NZD pairs
      London 07-16  — owns EUR, GBP, CHF, EUR-crosses
      NY     12-21  — owns USD pairs
      Overlap 12-16 — peak liquidity for all pairs

    Data-justified rules combine general session reasoning with the 60d/clean-window
    audit findings. Hard BLOCKS preserve existing data-backed gates; CAUTION/PRIME
    extends with new windows where research + clean-period data both agree.
    """
    from datetime import datetime as _sg_dt, timezone as _sg_tz
    now = now_utc if now_utc is not None else _sg_dt.now(_sg_tz.utc)
    h = now.hour
    m = now.minute
    dow = now.weekday()  # 0=Mon, 6=Sun
    is_sunday = (dow == 6)
    is_friday = (dow == 4)

    eur_gbp_pairs = ('EUR_USD', 'GBP_USD', 'EUR_GBP', 'EUR_CHF', 'GBP_JPY', 'EUR_JPY', 'USD_CHF')
    jpy_pairs     = ('USD_JPY', 'EUR_JPY', 'GBP_JPY', 'AUD_JPY', 'NZD_JPY', 'CAD_JPY', 'CHF_JPY')
    aud_nzd_pairs = ('AUD_USD', 'NZD_USD', 'AUD_JPY', 'AUD_NZD', 'AUD_CAD', 'AUD_CHF', 'NZD_JPY')
    cross_pairs   = ('EUR_AUD', 'AUD_JPY', 'GBP_JPY', 'EUR_JPY', 'EUR_NZD', 'EUR_CAD',
                     'AUD_NZD', 'AUD_CAD', 'AUD_CHF', 'GBP_AUD', 'NZD_AUD')

    def _flag(key: str, default: bool = True) -> bool:
        if tc_get_fn is None:
            return default
        try:
            return bool(tc_get_fn(key, default))
        except Exception:
            return default

    def _result(state, reason, owning=None, next_open=None):
        return {"state": state, "reason": reason,
                "owning_session": owning, "next_open_utc": next_open}

    # ─── HARD BLOCKS (legacy, data-justified — preserved) ───
    if is_sunday and h in (21, 22, 23):
        return _result("BLOCKED", "Sunday blackout (5-7PM ET) — thin liquidity, gap risk",
                       next_open="00:00 UTC (Tokyo)")

    if instrument in eur_gbp_pairs and (h >= 23 or h < 3):
        return _result("BLOCKED",
                       f"{instrument} deep Asian (23-03 UTC) — EUR/GBP have no real liquidity here",
                       next_open="07:00 UTC (London)")

    if (
        _flag("gate.session_eur_cross_tail_enabled")
        and instrument in ('EUR_AUD', 'EUR_CHF', 'EUR_JPY', 'EUR_CAD', 'EUR_NZD')
        and (h in (3, 4, 5) or (h == 6 and m < 30))
    ):
        return _result("BLOCKED",
                       f"{instrument} EUR-cross Asian tail (03-06:30 UTC) — backtested 2W/4L -$126",
                       next_open="07:00 UTC (London)")

    if is_friday and h >= 20:
        return _result("BLOCKED", "Friday close (after 4PM ET) — weekend gap risk",
                       next_open="Monday 22:00 UTC")

    if (
        _flag("gate.session_aud_late_eu_enabled")
        and instrument in ('AUD_JPY', 'AUD_USD', 'AUD_NZD', 'AUD_CAD',
                           'AUD_CHF', 'EUR_AUD', 'GBP_AUD', 'NZD_AUD')
        and h in (21, 22) and not is_sunday and not is_friday
    ):
        return _result("BLOCKED",
                       f"{instrument} UTC 21-22 weekday — AUD bleed window (60d: 0/6 WR, -109p)",
                       next_open="00:00 UTC (Tokyo)")

    # ─── PRIME windows (owning session active — trust structural read) ───
    # London-NY overlap 12-16 UTC = peak liquidity for all pairs.
    # Clean-period data (Apr 29-May 5): 100% WR, +32p across pairs.
    if 12 <= h < 16:
        return _result("PRIME", "London-NY overlap (12-16 UTC) — peak liquidity, prime structural follow-through",
                       owning="Overlap")

    # London early 08-12 UTC = owning session for EUR/GBP/CHF/EUR-crosses.
    # Clean-period data: 89% WR, +100p over 9 trades.
    if instrument in eur_gbp_pairs and 8 <= h < 12:
        return _result("PRIME", "London session (08-12 UTC) — owning market for EUR/GBP/CHF",
                       owning="London")

    # Tokyo 00-06 UTC = owning session for JPY pairs.
    if instrument in jpy_pairs and 0 <= h < 6:
        return _result("PRIME", "Tokyo session (00-06 UTC) — owning market for JPY pairs",
                       owning="Tokyo")

    # Sydney + Tokyo 22-06 UTC = owning session for AUD/NZD pairs.
    if instrument in aud_nzd_pairs and (h >= 22 or h < 6):
        return _result("PRIME", "Sydney/Tokyo (22-06 UTC) — owning market for AUD/NZD pairs",
                       owning="Tokyo")

    # ─── CAUTION windows (owning session asleep — WATCH-with-snipe only) ───

    # Pre-London chop 04-08 UTC for EUR/GBP/CHF/USD pairs.
    # Research + clean-data: thin liquidity, known stop-hunt window (cable specifically).
    # Today's GBP_USD watch 2573 failure validated this (-18.7p, fired 05:39 UTC).
    if instrument in eur_gbp_pairs and 4 <= h < 8:
        return _result("CAUTION",
                       f"Pre-London (04-08 UTC) — thin liquidity, known stop-hunt window for {instrument}",
                       next_open="08:00 UTC (London)")

    # Asian session 00-04 UTC for non-JPY EUR/GBP/USD pairs.
    # Research: EUR/GBP barely move in Tokyo. 90d EUR_USD: 33% WR, -24p.
    if instrument in eur_gbp_pairs and 0 <= h < 4 and instrument not in jpy_pairs:
        return _result("CAUTION",
                       f"Asian session — {instrument} (EUR/GBP/CHF) is asleep here",
                       next_open="08:00 UTC (London)")

    # Post-London NY-only 16-20 UTC for AUD/NZD pairs.
    # Their owning sessions have closed; NY traders are working USD majors.
    # Clean-period data: NY-only -42p over 5 trades; 90d AUD_JPY NY: 38% WR, -80p.
    if instrument in aud_nzd_pairs and 16 <= h < 20:
        return _result("CAUTION",
                       f"NY-only (16-20 UTC, post-London) — {instrument} owning sessions closed",
                       next_open="22:00 UTC (Sydney)")

    # NY close 20-24 UTC for cross pairs (Friday already blocked above).
    # 90d EUR_AUD NY close: 13% WR, -82p.
    if instrument in cross_pairs and 20 <= h < 24:
        return _result("CAUTION",
                       f"NY close (20-24 UTC) — {instrument} cross pair, liquidity transitioning",
                       next_open="00:00 UTC (Tokyo)")

    # ─── OPEN (normal judgment) ───
    return _result("OPEN", "")


def _compute_session_gate(instrument: str, tc_get_fn=None, now_utc=None) -> tuple[bool, str]:
    """Back-compat shim for callers that only need (blocked, reason).

    The snipe-direct gate at run_cycle (~line 2932) uses this. The richer state
    (PRIME/CAUTION/OPEN) is exposed only to the validator section build.
    """
    w = _compute_session_window(instrument, tc_get_fn, now_utc)
    return (w["state"] == "BLOCKED", w["reason"])


def _call_unified_validator(params: dict) -> dict:
    """Call the unified handler_data_validator with full context.

    Loads the handler once (singleton), then calls evaluate_with_full_context.
    Returns result in the same format the old _agent_task("validator", ...) returned:
    {"response": json_string, "tool_calls": []}
    """
    global _unified_validator
    if _unified_validator is None:
        try:
            _jarvis_root = str(Path(__file__).parent.parent.parent.parent)
            if _jarvis_root not in sys.path:
                sys.path.insert(0, _jarvis_root)
            from Handler.handler_data_validator import DataValidatorHandler
            _unified_validator = DataValidatorHandler(
                workspace_id=params.get("workspace_id", "forex-trading-team")
            )
            logger.info("[UNIFIED_VALIDATOR] Handler initialized")
        except Exception as e:
            logger.error(f"[UNIFIED_VALIDATOR] Failed to initialize: {e}")
            return {
                "response": json.dumps({
                    "verdict": "SKIP", "confidence": 0.0, "direction": None,
                    "reasoning": f"Unified validator initialization failed: {e}",
                    "re_entry_conditions": [],
                }),
                "tool_calls": [],
            }

    try:
        import asyncio
        # Run the async method synchronously (trading_cycle is sync)
        try:
            loop = asyncio.get_running_loop()
            # Already in an event loop — use nest_asyncio or run in thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    lambda: asyncio.run(_unified_validator.evaluate_with_full_context(params))
                ).result(timeout=180)
        except RuntimeError:
            # No event loop running — safe to create one
            result = asyncio.run(_unified_validator.evaluate_with_full_context(params))

        # Wrap in the expected format: {"response": json_string, "tool_calls": []}
        return {
            "response": json.dumps(result, default=str),
            "tool_calls": [],
        }
    except Exception as e:
        logger.error(f"[UNIFIED_VALIDATOR] Call failed: {e}", exc_info=True)
        return {
            "response": json.dumps({
                "verdict": "SKIP", "confidence": 0.0, "direction": None,
                "reasoning": f"Unified validator call failed: {e}",
                "re_entry_conditions": [],
            }),
            "tool_calls": [],
        }

# ---------------------------------------------------------------------------
# Risk config loader
# ---------------------------------------------------------------------------

_risk_config = None


def _load_risk_config() -> dict:
    """Load risk config from Config/risk_config.json. Cached after first load."""
    global _risk_config
    if _risk_config is not None:
        return _risk_config
    config_path = Path(__file__).parent.parent.parent / "Config" / "risk_config.json"
    try:
        with open(config_path) as f:
            _risk_config = json.load(f)
            logger.info("Loaded risk config from %s", config_path)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("Failed to load risk_config.json (%s), using defaults", e)
        _risk_config = {}
    return _risk_config


def _get_risk_limits(account_summary: dict = None, daily_loss_pct: float = 0.0, user_id: int = None) -> dict:
    """Build risk_limits dict from config + user DB overrides + live account data."""
    cfg = _load_risk_config().get("risk_limits", {})
    open_trades = 0
    if isinstance(account_summary, dict):
        open_trades = account_summary.get("openTradeCount",
                      account_summary.get("open_trade_count", 0))
    
    sniper_cfg = _load_risk_config().get("sniper", {})
    pos_cfg = _load_risk_config().get("position_sizing", {})
    base = {
        "min_confluence": cfg.get("min_confluence", 40),
        "min_rr_ratio": cfg.get("min_rr_ratio", 1.5),
        "max_daily_loss_pct": cfg.get("max_daily_loss_pct", 3.0),
        "max_concurrent_trades": cfg.get("max_concurrent_trades", 3),
        "current_daily_loss_pct": daily_loss_pct,
        "current_open_trades": open_trades,
        "sniper_threshold": int(sniper_cfg.get("threshold", 12)),
        "sniper_tp_atr": float(sniper_cfg.get("tp_atr", 0.5)),
        "sniper_sl_atr": float(sniper_cfg.get("sl_atr", 2.5)),  # V4: reverted to 2.5x ATR (3.0 proved worse)
        "position_sizing_mode": pos_cfg.get("mode", "auto"),
        "fixed_units": pos_cfg.get("fixed_units", 10000),
        "fixed_lots": pos_cfg.get("fixed_lots", 0.1),
        "auto_profit": "on",
    }
    
    # Apply user overrides from trading_preferences DB
    if user_id is not None:
        try:
            conn = get_core()
            rows = conn.execute(
                "SELECT pref_key, pref_value FROM trading_preferences WHERE user_id = ? AND pref_key LIKE 'risk_%'",
                (user_id,)
            ).fetchall()
            # Don't close pooled connections
            STRING_KEYS = {"position_sizing_mode", "auto_profit"}
            for pref_key, pref_value in rows:
                setting_key = pref_key[5:]  # Strip 'risk_' prefix
                if setting_key in base and setting_key not in ("current_daily_loss_pct", "current_open_trades"):
                    if setting_key in STRING_KEYS:
                        base[setting_key] = str(pref_value)
                    else:
                        try:
                            base[setting_key] = float(pref_value)
                        except (ValueError, TypeError):
                            pass
        except Exception as exc:
            logger.warning("Failed to load user risk overrides: %s", exc)
    
    return base

# ---------------------------------------------------------------------------
# Sniper score → 0-100 confluence-equivalent
# ---------------------------------------------------------------------------

def sniper_to_confluence(sniper_result: dict) -> dict:
    """Map Sniper V4 output to a 0-100 confluence-equivalent score.
    
    This lets all downstream code that expects confluence_score (0-100) work
    with sniper data. The mapping is based on the sniper component structure:
    
    Sniper max theoretical ≈ 30-35 (all components firing).
    Practical range: 0-25. Scores > 20 are extremely rare.
    Threshold default = 12 (maps to ~60/100 — minimum trade signal).
    
    Mapping: (sniper_score / 25) * 80, capped at 100, with bonuses for:
    - H4 alignment: +8
    - Candlestick pattern: +6  
    - Divergence: +6
    """
    if not sniper_result or sniper_result.get("error"):
        return {"total_score": 0, "direction": "neutral", "regime": "unknown"}
    
    buy = sniper_result.get("buy_score", 0)
    sell = sniper_result.get("sell_score", 0)
    max_score = max(buy, sell)
    
    # Base: linear map from sniper scale to 0-80
    base = min(80, (max_score / 25.0) * 80)
    
    # Bonuses
    h4_bias = sniper_result.get("h4_bias", "none")
    direction = sniper_result.get("direction", "neutral")
    h4_agrees = (
        (h4_bias == "bull" and direction == "bullish") or
        (h4_bias == "bear" and direction == "bearish")
    )
    if h4_agrees:
        base += 8
    
    patterns = sniper_result.get("detected_patterns", [])
    if patterns:
        base += 6
    
    div = sniper_result.get("divergence", {})
    if any(div.values()):
        base += 6
    
    score = min(100, round(base, 1))
    
    # Regime from ADX
    ind = sniper_result.get("indicators", {})
    adx = ind.get("adx", 25)
    if adx > 25:
        regime = "trending"
    elif adx < 20:
        regime = "ranging"
    else:
        regime = "mixed"
    
    return {
        "total_score": score,
        "direction": direction,
        "regime": regime,
        "sniper_buy": buy,
        "sniper_sell": sell,
        "h4_agrees": h4_agrees,
    }


# ---------------------------------------------------------------------------
# KnowledgeStore lazy singleton
# ---------------------------------------------------------------------------

_knowledge_store = None


def _detect_regime(sniper_data: dict) -> str:
    """Map current indicators to valid DB regime names.
    Valid DB regimes: exhaustion, high_volatility, ranging, squeeze, strong_trend
    NOTE: 'mixed' does NOT exist in backtest DB — always map to a real regime."""
    ind = sniper_data.get("indicators", {}) if isinstance(sniper_data, dict) else {}
    adx = ind.get("adx", 25)
    atr = ind.get("atr", 0)
    bb_width = ind.get("bb_width", 0)
    rsi = ind.get("rsi", 50)
    
    # BB squeeze detection
    if bb_width and bb_width < 0.003:
        return "squeeze"
    # Strong trend
    if adx > 35:
        return "strong_trend"
    # Exhaustion: trending but RSI extreme
    if adx > 25 and (rsi > 70 or rsi < 30):
        return "exhaustion"
    # High volatility
    if adx > 20 and atr and bb_width and bb_width > 0.01:
        return "high_volatility"
    # Ranging (ADX < 25 — "mixed" doesn't exist in backtest DB)
    # ADX 20-25 is transitional — closer to ranging than trending
    if adx < 25:
        return "ranging"
    # ADX 25-35 without other signals — mild trend
    return "strong_trend"


def _compute_story_score(indicators: dict, ema_result: dict, scout_context: dict) -> int:
    """Quantify how compelling the trade thesis is (0-100).

    Components (100 total):
      EMA alignment with direction     30
      Fan velocity (momentum exists)   25
      Oscillator in trend zone         20
      BB expanding in trade direction   25

    Returns int 0-100.
    """
    # indicators may be {"core": {...}, "advanced": {...}} or flat dict
    _raw_ind = indicators if isinstance(indicators, dict) else {}
    ind = _raw_ind.get("core", _raw_ind) if isinstance(_raw_ind.get("core"), dict) else _raw_ind
    ema = ema_result if isinstance(ema_result, dict) else {}
    scout = scout_context if isinstance(scout_context, dict) else {}
    score = 0

    # --- 1. EMA alignment (30 pts) ---
    # EMA values live under ema_result["current_emas"] with keys ema21/ema55/ema100
    _cur_emas = ema.get("current_emas", {}) if isinstance(ema.get("current_emas"), dict) else {}
    e21 = float(_cur_emas.get("ema21", 0) or ind.get("ema_21", 0) or 0)
    e55 = float(_cur_emas.get("ema55", 0) or ind.get("ema_55", 0) or 0)
    e100 = float(_cur_emas.get("ema100", 0) or ind.get("ema_100", 0) or 0)

    # Also check fan_ordered flag from scan_ema_signals as shortcut
    fan_ordered = ema.get("fan_ordered", False)
    if fan_ordered or (e21 > e55 > e100 > 0) or (0 < e21 < e55 < e100):
        score += 30  # fully ordered fan
    elif e21 and e55 and e100:
        # partial order: 2 of 3 relationships correct
        if (e21 > e55 and e55 < e100) or (e21 < e55 and e55 > e100):
            score += 18

    # --- 2. Fan velocity / momentum (25 pts) ---
    velocity = abs(float(ema.get("separation_velocity", 0) or 0))
    if velocity > 0.002:
        score += 25
    elif velocity > 0.001:
        score += 15
    elif velocity > 0.0005:
        score += 8

    # --- 3. Oscillator in trend zone (20 pts) ---
    # RSI/stoch are merged into ema_result at top level, also check ind (sniper)
    rsi = float(ema.get("rsi", 0) or ind.get("rsi", 50) or 50)
    stoch_k = float(ema.get("stoch_k", 0) or ind.get("stoch_k", 50) or 50)
    rsi_trend_zone = 35 <= rsi <= 65
    rsi_ok = 25 <= rsi <= 75
    stoch_extreme = stoch_k > 90 or stoch_k < 10
    if rsi_trend_zone and not stoch_extreme:
        score += 20
    elif rsi_ok and not stoch_extreme:
        score += 12
    elif rsi_ok:
        score += 6

    # --- 4. BB expanding in trade direction (25 pts) ---
    # bb_expanding and bb_acceleration are merged into ema_result from bollinger sub-dict
    bb_accel = float(ema.get("bb_acceleration", 0) or 0)
    bb_expanding = ema.get("bb_expanding", False) or scout.get("bb_expanding", False)
    bb_width = float(ema.get("width_pct", 0) or ind.get("bb_width", 0) or 0)
    if bb_expanding and bb_accel > 0:
        score += 25
    elif bb_expanding or bb_accel > 0:
        score += 12
    elif bb_width > 0.005:
        score += 6

    return min(score, 100)


def _build_thesis_from_ta(
    pair: str,
    ta_narrative: str,
    ta_full: dict,
    indicators: dict,
    ema_result: dict,
    scout_context: dict,
    story_score: int,
) -> str:
    """Build a conviction-style thesis from the TA agent's analysis.

    Mirrors how Tim frames his chart submissions — direction, EMA story,
    oscillator state, BB context, and phase assessment — so the validator
    evaluates a thesis rather than raw data.
    """
    # indicators may be {"core": {...}, "advanced": {...}} or flat dict
    _raw_ind = indicators if isinstance(indicators, dict) else {}
    ind = _raw_ind.get("core", _raw_ind) if isinstance(_raw_ind.get("core"), dict) else _raw_ind
    ema = ema_result if isinstance(ema_result, dict) else {}
    scout = scout_context if isinstance(scout_context, dict) else {}
    ta = ta_full if isinstance(ta_full, dict) else {}

    # --- Direction from fan ordering ---
    # EMA values are under ema_result["current_emas"] with keys ema21/ema55/ema100
    _cur_emas = ema.get("current_emas", {}) if isinstance(ema.get("current_emas"), dict) else {}
    e21 = float(_cur_emas.get("ema21", 0) or ind.get("ema_21", 0) or 0)
    e55 = float(_cur_emas.get("ema55", 0) or ind.get("ema_55", 0) or 0)
    e100 = float(_cur_emas.get("ema100", 0) or ind.get("ema_100", 0) or 0)

    # Also check fan_ordered + fan_direction flags from scan_ema_signals
    fan_ordered = ema.get("fan_ordered", False)
    fan_dir = (ema.get("fan_direction", "") or "").lower()

    if fan_ordered and fan_dir == "bullish" or (e21 > e55 > e100 > 0):
        direction = "BUY"
        fan_desc = "Bullish EMA fan ordered and separating (E21 > E55 > E100)"
    elif fan_ordered and fan_dir == "bearish" or (0 < e21 < e55 < e100):
        direction = "SELL"
        fan_desc = "Bearish EMA fan ordered and separating (E100 > E55 > E21)"
    else:
        # mixed — use scout direction or best guess
        scout_dir = (scout.get("direction") or "").upper()
        direction = scout_dir if scout_dir in ("BUY", "SELL") else "NEUTRAL"
        fan_desc = "EMA fan mixed — partial ordering"

    # Fan velocity
    velocity = float(ema.get("separation_velocity", 0) or 0)
    if velocity:
        fan_desc += f" (velocity {velocity:+.4f}%/bar)"

    # --- Oscillator state ---
    rsi = float(ema.get("rsi", 0) or ind.get("rsi", 50) or 50)
    stoch_k = float(ema.get("stoch_k", 0) or ind.get("stoch_k", 50) or 50)
    stoch_d = float(ema.get("stoch_d", 0) or ind.get("stoch_d", 50) or 50)

    osc_parts = []
    if rsi:
        if rsi > 70:
            osc_parts.append(f"RSI at {rsi:.0f} — overbought zone, watch for exhaustion")
        elif rsi < 30:
            osc_parts.append(f"RSI at {rsi:.0f} — oversold zone, watch for exhaustion")
        elif 45 <= rsi <= 55:
            osc_parts.append(f"RSI at {rsi:.0f} — neutral, momentum not yet established")
        else:
            osc_parts.append(f"RSI at {rsi:.0f} — momentum in trend zone")
    if stoch_k:
        if stoch_k > 80:
            osc_parts.append(f"Stoch K/D at {stoch_k:.0f}/{stoch_d:.0f} — elevated")
        elif stoch_k < 20:
            osc_parts.append(f"Stoch K/D at {stoch_k:.0f}/{stoch_d:.0f} — depressed")
        else:
            osc_parts.append(f"Stoch K/D at {stoch_k:.0f}/{stoch_d:.0f}")
    osc_text = ". ".join(osc_parts) if osc_parts else "Oscillator data unavailable"

    # --- BB context ---
    # bb_expanding and bb_acceleration are merged into ema_result from bollinger sub-dict
    bb_accel = float(ema.get("bb_acceleration", 0) or 0)
    bb_expanding = ema.get("bb_expanding", False) or scout.get("bb_expanding", False)
    bb_width = float(ema.get("width_pct", 0) or ind.get("bb_width", 0) or 0)
    if bb_expanding and bb_accel > 0:
        bb_text = f"BBs expanding (accel {bb_accel:+.4f}, width {bb_width:.4f}) — volatility increasing"
    elif bb_expanding:
        bb_text = f"BBs expanding (width {bb_width:.4f})"
    elif ema.get("squeeze", False) or bb_width < 0.003:
        bb_text = f"BBs in squeeze (width {bb_width:.4f}) — energy coiling for breakout"
    else:
        bb_text = f"BBs flat/contracting (width {bb_width:.4f})"

    # --- Phase from TA ---
    phase = (ta.get("cascade_phase", "") or ta.get("phase_assessment", "") or "")[:200]
    phase_text = f"Phase assessment: {phase}" if phase else ""

    # --- TA narratives (use if available) ---
    ema_story = (ta.get("ema_state", "") or ta.get("ema_story", "") or "")[:300]
    candle_story = (ta.get("candle_tests", "") or ta.get("candle_story", "") or "")[:200]

    # --- Build the thesis ---
    _pair_display = pair.replace("_", "/")
    quality = "Strong" if story_score >= 70 else "Moderate" if story_score >= 50 else "Weak"

    parts = [
        f"**{_pair_display} {direction}** — {fan_desc}.",
        osc_text + ".",
        bb_text + ".",
    ]
    if phase_text:
        parts.append(phase_text)
    if ema_story and ema_story not in fan_desc:
        parts.append(f"TA reads: {ema_story}")
    if candle_story:
        parts.append(f"Candles: {candle_story}")
    parts.append(f"Story score: {story_score}/100 ({quality}).")

    return " ".join(parts)


def _get_knowledge_store():
    """Lazy-initialise and return the shared KnowledgeStore."""
    global _knowledge_store
    if _knowledge_store is None:
        try:
            from Source.knowledge_store import KnowledgeStore
            _knowledge_store = KnowledgeStore()
        except ImportError:
            logger.warning("KnowledgeStore not available -- running headless")
    return _knowledge_store


def _get_active_pair_from_db() -> str:
    """Read active trading pair from users database.
    
    Returns:
        str: Active pair like "EUR_USD", defaults to "EUR_USD" if not found.
    """
    try:
        conn = get_core()
        cursor = conn.cursor()
        
        # Get user_id from broker_credentials (assuming first row for now)
        cursor.execute("SELECT user_id FROM broker_credentials LIMIT 1")
        user_result = cursor.fetchone()
        
        if not user_result:
            logger.warning("No broker credentials found, using default EUR_USD")
            return "EUR_USD"
            
        user_id = user_result[0]
        
        # Get active_pair preference
        cursor.execute("""
            SELECT pref_value FROM trading_preferences 
            WHERE user_id = ? AND pref_key = 'active_pair'
        """, (user_id,))
        
        pair_result = cursor.fetchone()
        
        if pair_result:
            return pair_result[0]
        else:
            logger.info("No active_pair preference found, using default EUR_USD")
            return "EUR_USD"
                
    except Exception as exc:
        logger.warning("Failed to read active pair from database: %s", exc)
        return "EUR_USD"

# ---------------------------------------------------------------------------
# SwarmHandler lazy singleton
# ---------------------------------------------------------------------------

_swarm = None


def _get_swarm(tracker=None):
    """Lazy-initialise and return the shared SwarmHandler."""
    global _swarm
    if _swarm is None:
        try:
            from Handler.handler_swarm import SwarmHandler
            _swarm = SwarmHandler(tracker=tracker)
        except ImportError:
            logger.warning("SwarmHandler not available -- running headless")
    return _swarm


# ---------------------------------------------------------------------------
# TradeLogger lazy singleton
# ---------------------------------------------------------------------------

_trade_logger = None


def _get_trade_logger():
    """Lazy-initialise and return the shared TradeLogger."""
    global _trade_logger
    if _trade_logger is None:
        from Source.trade_logger import TradeLogger
        _trade_logger = TradeLogger()
    return _trade_logger


# ---------------------------------------------------------------------------
# AgentRegistry lazy singleton
# ---------------------------------------------------------------------------

_agent_registry = None


def _get_agent_registry():
    """Lazy-initialise and return the shared AgentRegistryHandler."""
    global _agent_registry
    if _agent_registry is None:
        try:
            from Handler.handler_agent_registry import AgentRegistryHandler
            _agent_registry = AgentRegistryHandler()
        except ImportError:
            logger.warning("AgentRegistryHandler not available -- running headless")
    return _agent_registry


def _report_agent_performance(agent_name: str, success: bool,
                              response_time: float,
                              quality_score: float = 0.5) -> None:
    """Report agent performance metrics directly to agents.db agent_registry.

    Updates success_count, failure_count, avg_response_time, total_requests,
    and last_request_at for the agent. Matches by agent_name (not agent_id).
    """
    conn = None
    try:
        import sqlite3 as _sql3
        db_path = str(Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "agents.db")
        conn = _sql3.connect(db_path, timeout=30, isolation_level=None)
        
        row = conn.execute(
            "SELECT success_count, failure_count, avg_response_time, total_requests "
            "FROM agent_registry WHERE agent_name = ? AND team_id IS NOT NULL LIMIT 1",
            (agent_name,)
        ).fetchone()
        
        if not row:
            return
        
        sc, fc, avg_rt, total = row
        total = (total or 0) + 1
        if success:
            sc = (sc or 0) + 1
        else:
            fc = (fc or 0) + 1
        
        # Weighted running average
        if total > 1 and avg_rt:
            avg_rt = ((avg_rt * (total - 1)) + response_time) / total
        else:
            avg_rt = response_time
        
        conn.execute(
            "UPDATE agent_registry SET success_count=?, failure_count=?, "
            "avg_response_time=?, total_requests=?, last_request_at=? "
            "WHERE agent_name=? AND team_id IS NOT NULL",
            (sc, fc, round(avg_rt, 3), total,
             datetime.now(timezone.utc).isoformat(), agent_name)
        )
        conn.commit()
    except Exception as exc:
        logger.debug("Performance report for %s failed: %s", agent_name, exc)
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Swarm execution helpers
# ---------------------------------------------------------------------------

# Module-level trading pause flag (resets on restart, not persisted)
_trading_paused = False


def _run_swarm(action: str, parameters: dict, timeout: float = 30.0) -> dict:
    """Execute a SwarmHandler action synchronously with timeout.

    Wraps the async SwarmHandler.handle() in asyncio.run() for use from
    synchronous cycle code.  Returns the result data dict on success,
    or raises on failure. Times out after the specified timeout.
    """
    swarm = _get_swarm()
    if swarm is None:
        raise RuntimeError("SwarmHandler unavailable")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    coro = swarm.handle({"action": action, "parameters": parameters})
    
    # Wrap with timeout
    async def run_with_timeout():
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"SwarmHandler {action} timed out after {timeout}s")
    
    coro_with_timeout = run_with_timeout()

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(asyncio.run, coro_with_timeout).result()
    else:
        result = asyncio.run(coro_with_timeout)

    # HandlerResult has .data and .success
    if hasattr(result, "data"):
        if hasattr(result, "success") and not result.success:
            # Error message may be in result.error (HandlerResult) or result.data
            err_msg = getattr(result, "error", None) or result.data or "unknown error"
            raise RuntimeError(
                f"SwarmHandler {action} failed: {err_msg}"
            )
        return result.data or {}
    if isinstance(result, dict):
        return result
    return {}


def _swarm_execute_tool(agent_name: str, tool_name: str, **kwargs) -> dict:
    """Shortcut: swarm.execute_tool(agent_name, tool_name, **kwargs).
    LEGACY — kept for fallback. Prefer _agent_task() for LLM agent execution."""
    return _run_swarm("execute_tool", {
        "agent_name": agent_name,
        "tool_name": tool_name,
        **kwargs,
    })


# ── V4 Vision: Teaching image cache (loaded once per process) ──
_V4_TEACHING_IMAGES_CACHE: list = []
_V4_TEACHING_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "Data", "charts", "teaching"
)

# Teaching image manifest — each entry is a reference example for the validator
_V4_TEACHING_MANIFEST = [
    # Tim's annotated TRADE examples — his personal markups with annotations/circles
    {"file": "tim_teach_stage1_fan_entry.png", "description": "PHASE 2.5 ENTRY EXAMPLE — EUR/AUD LONG: E21 crossed E55 (circled, marked 'CS Bull'). E21 has NOT yet crossed E100 — candles have clear yellow-highlighted space from E100. This IS a valid entry. The full fan (E21>E55>E100) forms AFTER entry. Tim opened a profitable BUY trade at this exact moment. DO NOT skip setups because E21 hasn't crossed E100 yet. The E21×E55 cross with opening gap is the entry signal."},
    {"file": "tim_teach_euraud_phase25_e100_retest.png", "description": "RETRACEMENT ENTRY LESSON — PRICE ON E100/E55 = BUY ZONE, NOT REJECTION. When a BULLISH fan has peaked and is contracting but STILL ORDERED (E21 > E55 > E100), price pulling back to E100 or E55 is the BEST ENTRY ZONE — not a skip. The fan breathing and contracting is NORMAL RETRACEMENT. Yellow circles mark where price tested E100/E55 from above — this looks like 'the trend is dying' but it is ACCUMULATION before the next leg up. KEY RULE: if E21 is still above E55 (bull) or E21 still below E55 (bear), the fan has NOT failed — it is RETRACING. BUY when price hits E100 from above in an ordered bullish fan. DO NOT reject because BBs are contracting or fan velocity is negative — contraction during a retrace is EXPECTED. Set SNIPE for price reaching E55 or E100 in an ordered fan. The setup only FAILS when E21 crosses BELOW E55 (bull) — that is the exit signal, not the entry block."},
    {"file": "tim_teach_eurchf_bearish_fan_flip.png", "description": "TRADE EXAMPLE — EUR/CHF SHORT: Bearish EMA fan flip after Bollinger squeeze. Chart shows: 10+ hours of tight BB consolidation (bands contracted), then double top pattern (95% confidence, circled) at E100 resistance, then E21 crossed BELOW E55 and E100 creating bearish fan. The breakdown was explosive — 100+ pips. KEY LESSON: When BBs squeeze for extended period + double top forms at E100 + E21 crosses below E55 = SHORT ENTRY. This is the bearish mirror of Phase 2.5. The validator correctly called WATCH at first cycle but missed the entry due to a blank chart image. RSI oversold (18.8) and Stoch (2.1) at the close confirm the move was real. These indicators being extreme AFTER entry is normal for a strong trend — do not use oversold as a reason to skip a bearish fan flip."},
    {"file": "tim_teach_1.png", "description": "TRADE EXAMPLE — Fan opening wide, BBs expanding. Clean unmistakable expansion. THIS is a valid entry."},
    {"file": "tim_teach_2.png", "description": "TRADE EXAMPLE — Clear downward expansion after cross. EMAs separating in order, BBs widening. Obvious trend."},
    # Tim's annotated SKIP examples
    {"file": "tim_teach_3.png", "description": "SKIP EXAMPLE — TANGLED FAN (not retracement). EMAs are fully converged and crossing each other in all directions — E21 is not consistently above or below E55. BBs are tight with no directional separation. This is CHOP, not retracement. DO NOT trade when EMAs have no structure. KEY DISTINCTION: this is different from a peaked/contracting ordered fan where E21>E55>E100 (that is a RETRACEMENT — see other examples). This is DISORDERED — no clear relationship between EMAs. The red boxes show 'no trade zones' because EMAs are tangled, not because the fan is contracting."},
    {"file": "tim_teach_4.png", "description": "RETRACEMENT SETUP — PEAKED FAN + BBs CONTRACTING = WATCH FOR ENTRY ZONE. This shows a bullish fan that has peaked (maximum separation) and the BBs are tightening. IMPORTANT: if the fan is still ORDERED (E21 > E55 > E100), this is NOT a skip — this is the RETRACEMENT PHASE where price is pulling back toward E55 or E100 before the trend continues. The rod tip is bending (fishing line theory). ENTRY TIMING: (A) if price pulls to E55 = mid-retrace entry zone, (B) if price pulls all the way to E100 = deep retracement best entry. ONLY skip if E21 has crossed BELOW E55 (fan structure failed) or if price is still at the PEAK with nothing to retrace to yet. BBs tightening during contraction = expected. BBs re-expanding after hitting E55/E100 = confirmation the trend is resuming. Set SNIPE for price reaching E55 or E100 with reversal candle."},
    # One backtest WIN + one LOSS for contrast (unannotated but clean examples)
    {"file": "trade_364_USD_JPY_SHORT_WIN_+190p.png", "description": "TRADE EXAMPLE — USD_JPY SHORT +190 pips: Perfect expansion. Fan opens wide, BBs expand, candles drop cleanly."},
    {"file": "trade_103_AUD_JPY_SHORT_LOSS_-34p.png", "description": "SKIP EXAMPLE — AUD_JPY SHORT -34 pips LOSS: Choppy. E100 too close. No clear separation."},
    # Real LOSS trades — NOTE: trade_641 and trade_633 files are blank placeholders (13KB white images)
    # Excluded from manifest until real chart images are available
    # {"file": "trade_641_EUR_AUD_BUY_LOSS_-5p.png", ...},
    # {"file": "trade_633_EUR_AUD_BUY_LOSS_-3p.png", ...},
    # Removed: 4 D6 examples (redundant with above), 2 extra backtest, 5 pattern refs (validator knows these)
]

# ─── FISHING LINE THEORY (text knowledge — no image needed) ──────────────────
# This is injected into every validator prompt alongside the teaching images.
# It explains the core retracement strategy Tim trades that the images do NOT show.
_FISHING_LINE_THEORY = """
## FISHING LINE THEORY — THE CORE RETRACEMENT STRATEGY

Tim's primary entry is a RETRACEMENT CONTINUATION, not an expansion entry.
Think of the EMA fan as a fishing rod:
- When the fan is EXPANDING = the rod is straight and casting
- When the fan PEAKS then CONTRACTS = the rod tip is BENDING (a fish is pulling)
- When the rod bends the MOST (price at E55 or E100) = SET THE HOOK (this is the entry)
- When the rod STRAIGHTENS again (fan re-expands) = the fish is running = trade in profit

### THE FIVE-PHASE CASCADE — THE FULL TRADE LIFECYCLE

Every trend move cycles through these phases. The guardian tracks each one in real time
using three composite signals: EMA fan separation (E21-E100 distance), Bollinger Band width,
and their interaction (both expanding vs both contracting).

**Phase 1 — TRENDING (BOTH EXPAND)**
Both fan separation AND BB width increasing simultaneously.
Signal: `both_expanding = True` (bb_contracting_count=0 AND ema_velocity>0)
Action: Ride the trend. SL trails E55 + buffer. No fixed TP — the move is alive.
What it looks like on chart: E21 pulling strongly away from E55 and E100. BBs widening.
Candles well clear of E55.

**Phase 2 — PEAK SIGNAL (fan velocity → 0 while BBs still wide)**
Fan separation stops growing (velocity turns near-zero or slightly negative) but BBs are
still at their widest. This is the TOP of the leg — 1-2 bars before the retrace starts.
Signal: `ema_velocity ≤ 0` while `bb_width ≥ 80% of peak_bb_width`
Action: Lock profit floor (SL moves to entry + 70% of current pips). Trade still open.
The guardian's profit-lock rule fires here: "peaked against trade" signal.
What it looks like: Fan still ordered but separation not growing. Price extended.

**Phase 3 — RETRACING (BOTH CONTRACT)**
Fan separation AND BB width both shrinking simultaneously. The retrace is underway.
Signal: `both_contracting = True` (bb_contracting_count>0 AND ema_velocity<0)
THIS IS NORMAL. The fan is still ordered (E21 still on correct side of E55). The structure
is intact. Do NOT panic-exit. Do NOT mistake this for a trend reversal.
Guardian state: `retrace_state = 'retracing'`
SL now trails toward E100 gradually (30% of distance per tick) — the danger zone anchor.
Price heading toward E55 (mid-retrace) or E100 (deep retrace).
What it looks like: E21 and E55 converging, BBs squeezing, price pulling back.

**Phase 4 — RESUMPTION (BOTH EXPAND after retrace)**
Both signals re-expand after the contraction. The second leg is starting.
Signal: `reexpansion_count ≥ 2` (both_expanding for 2+ consecutive bars after retrace)
Guardian state: `retrace_state = 'continuing'`
This is the second entry point. If still in trade: TP resets based on current price projection.
SL resets back to E55 anchor (Phase 1 rules resume).
If NOT in trade: this is the validator's cue to set a second-leg re-entry snipe.
What it looks like: BBs starting to widen again from compressed state. Fan separation
increasing. Price bounced off E55 or E100 and is heading back in trade direction.

**Phase 5 — EXHAUSTION EXIT (composite peak detection)**
All three signals converging: fan velocity turning negative, RSI in extreme zone,
BB width at or near session peak. The move is done. Take profit now.
Signal: `ema_velocity ≤ 0` AND `rsi > 70 (bull) or < 30 (bear)` AND `bb_width ≥ peak * 0.9`
Action: Close 50% of position at this signal. Trail the remaining 50% with tight SL
(set to entry + 70% of peak pips). The trailing half either gets stopped at profit floor
or catches an extension if the move has one more leg.
What it looks like: Fan peaked, BBs at widest, RSI extreme, candles showing rejection
wicks at the top/bottom. Price extended far past E55.

### THE RETRACEMENT CYCLE (complete picture):
1. EMA fan expands — E21 pulls away from E55 and E100 (PHASE 1 / BOTH EXPAND)
2. Fan velocity → 0, BBs still wide (PHASE 2 / PEAK — lock profit floor)
3. BBs CONTRACT, EMA velocity negative, candles pull back toward EMAs (PHASE 3 / RETRACE)
4. Price reaches E55 (mid-retrace) or E100 (deep retracement) — ENTRY ZONE
5. Reversal candle forms at E55 or E100 (hammer, engulfing, pin bar)
6. BBs start re-expanding — fan starts breathing again (PHASE 4 / RESUMPTION)
7. Fan re-accelerates — E21 pulls away from E55 again (back to PHASE 1)
8. Repeat until PHASE 5 exhaustion

### CRITICAL DISTINCTIONS:
- ORDERED FAN CONTRACTING (E21>E55>E100 bull) = RETRACEMENT = NORMAL = HOLD
- DISORDERED FAN (E21 below E55, EMAs tangled) = REVERSAL or CHOP = EXIT/SKIP
- The ONLY true structural exit signal is E21 crossing BELOW E55 (bull) or ABOVE E55 (bear)
- BOTH CONTRACT during an ordered fan = Phase 3 retrace — DO NOT exit
- BOTH EXPAND after retrace = Phase 4 resumption — this is the second-leg entry

### WHAT THE GUARDIAN IS TRACKING IN REAL TIME (every M1 bar):
The guardian monitors these continuously for every live trade:
- `both_contracting`: BB width shrinking AND fan separation shrinking simultaneously
- `both_expanding`: both increasing
- `retrace_state`: 'trending' | 'retracing' | 'continuing'
- `_peak_bb_width`: highest BB width seen since trade opened
- `_peak_fan_width`: highest E21-E100 separation seen since trade opened
- `_reexpansion_count`: consecutive bars of both_expanding after a retrace
- `_e100_tests_in_retrace`: how many times price has tested E100 during retrace
- `_retrace_depth`: how deep the BB has compressed (% of peak width used)

### YOUR ROLE AS VALIDATOR FOR SECOND-LEG RE-ENTRY:
When a cycle is triggered with `reentry_context.is_reentry = True` and
`reentry_context.prior_exit_reason = 'guardian_retrace_partial'` or similar:
- A prior trade ran its first leg and the guardian is now watching for Phase 4 resumption
- You are being asked to confirm the second-leg entry conditions
- LOOK AT: where price bounced (E55 or E100?), is the fan still ordered, are BBs beginning to widen?
- SET SNIPE CONDITIONS that fire when the resumption is confirmed — not when it's just starting

### SECOND-LEG SNIPE CONDITIONS (set these when you see Phase 4 starting):
- Scenario A (E55 bounce continuation — mid-retrace held):
  `ema_fan_state in ['contracting','peaked']` (still retracing or just peaked)
  + `ema_velocity > 0` (velocity just turned positive — fan breathing again)
  + `bb_expanding == True` (BBs widening from compressed state)
  + `close_vs_ema ema_55 between [-3, +8]` (price near E55, not already past it)

- Scenario B (E100 bounce continuation — deep retrace):
  `ema_price_near_e100 == True` (price at the deep zone)
  + `rsi_zone in ['oversold','neutral']` (momentum spent)
  + `bb_width < 60% of peak_bb_width` (still compressed — early in resumption)

- Scenario C (resumption confirmed — safer but later entry):
  `both_expanding == True` for 2+ bars (reexpansion_count ≥ 2)
  + `ema_fan_state in ['expanding']` (fan confirmed moving again)
  + Note: this is a later, lower-risk entry — catches the middle of Phase 4, not the bottom

### WHAT TO LOOK FOR IN THE CHART:
- Is E21 still above E55? (bull) → Fan ordered → retracement setup alive
- Where is price? Near E55 (mid) or E100 (deep)? → These are the entry zones
- Are BBs starting to flicker wider? → First sign retrace ending (Phase 4 beginning)
- Is there a reversal candle at E55/E100? → Entry trigger
- Is RSI extreme? + Fan velocity turning? → Phase 5 exhaustion approaching (take profit)

### SNIPE CONDITIONS FOR INITIAL RETRACEMENT ENTRY:
- Scenario A (E55 retest): `close_vs_ema ema_55 <= 5` + `has_reversal_pattern == true` + `stoch_zone in [oversold, neutral]`
- Scenario B (E100 deep retest): `ema_price_near_e100 == true` + `ema_fan_state in [peaked, contracting]` + `rsi_zone in [oversold, neutral]`
- Scenario C (re-acceleration): `ema_velocity > 0` + `bb_acceleration > 0.0001` (early, still great entry)

### WHAT NOT TO DO:
- DO NOT wait for `ema_fan_state = bullish_expanding` as the ONLY trigger — that is Phase 1, not Phase 4
- DO NOT skip because BBs are contracting — contraction IS the retracement phase (Phase 3)
- DO NOT skip because fan velocity is negative — that means price is in the retracement zone
- DO NOT skip because price is near E55 or E100 — those are the TARGET entry levels
- DO NOT set snipe conditions that require `both_expanding` from the start — you'll always miss the entry
- DO NOT confuse Phase 3 (ordered fan contracting = NORMAL) with a trend reversal (disordered fan)
"""

# ── Pattern Knowledge Base ─────────────────────────────────────────────────────
# Maps detected pattern names → image file + trading context text
# Source: Research/complete_visual_knowledge_base.md (all 55 screenshots)
# Images injected dynamically only when that pattern is detected in the cycle.
_V4_PATTERN_DIR = os.path.join(_V4_TEACHING_DIR, "patterns")

_V4_PATTERN_MAP = {
    # S1 — Hammer/Pin Bar Reversal
    "hammer":          {"file": "pattern_01_hammer_pin_bar.png", "setup": "S1"},
    "inverted_hammer": {"file": "pattern_01_hammer_pin_bar.png", "setup": "S1"},
    "shooting_star":   {"file": "pattern_01_hammer_pin_bar.png", "setup": "S1"},
    # S2 — Engulfing
    "bullish_engulfing": {"file": "pattern_02_engulfing_bullish.png", "setup": "S2"},
    "bearish_engulfing": {"file": "pattern_03_engulfing_bearish.png", "setup": "S2"},
    # S3 — Morning/Evening Star
    "morning_star": {"file": "pattern_04_morning_evening_star.png", "setup": "S3"},
    "evening_star":  {"file": "pattern_04_morning_evening_star.png", "setup": "S3"},
    # S4 — Doji
    "doji":           {"file": "pattern_05_doji_extreme.png", "setup": "S4"},
    "dragonfly_doji": {"file": "pattern_05_doji_extreme.png", "setup": "S4"},
    "gravestone_doji":{"file": "pattern_05_doji_extreme.png", "setup": "S4"},
    "spinning_top":   {"file": "pattern_05_doji_extreme.png", "setup": "S4"},
    # S5/S6 — Triangles
    "ascending_triangle":  {"file": "pattern_06_ascending_triangle.png", "setup": "S5"},
    "descending_triangle": {"file": "pattern_07_descending_triangle.png", "setup": "S6"},
    # S7 — Channel
    "channel": {"file": "pattern_08_channel_trading.png", "setup": "S7"},
    # S8 — S/R Break
    "support_resistance_break": {"file": "pattern_09_support_resistance_break.png", "setup": "S8"},
    "at_support":    {"file": "pattern_09_support_resistance_break.png", "setup": "S8"},
    "at_resistance": {"file": "pattern_09_support_resistance_break.png", "setup": "S8"},
    # S12 — BB Squeeze
    "bb_squeeze":   {"file": "pattern_10_bb_squeeze_breakout.png", "setup": "S12"},
    "bb_expanding": {"file": "pattern_10_bb_squeeze_breakout.png", "setup": "S12"},
    # S15 — Divergence (keyed separately, matched via divergence dict)
    "divergence":   {"file": "pattern_11_momentum_divergence.png", "setup": "S15"},
    "divergence_rsi":   {"file": "pattern_11_momentum_divergence.png", "setup": "S15"},
    "divergence_stoch": {"file": "pattern_11_momentum_divergence.png", "setup": "S15"},
    # S17 — Fibonacci
    "fibonacci": {"file": "pattern_12_fibonacci_channel.png", "setup": "S18"},
    # Three soldiers/crows (use engulfing images as closest match)
    "three_white_soldiers": {"file": "pattern_02_engulfing_bullish.png", "setup": "S2"},
    "three_black_crows":    {"file": "pattern_03_engulfing_bearish.png", "setup": "S2"},
    # Dark cloud / piercing (use engulfing as closest match)
    "dark_cloud":    {"file": "pattern_03_engulfing_bearish.png", "setup": "S2"},
    "piercing_line": {"file": "pattern_02_engulfing_bullish.png", "setup": "S2"},
    # Tweezer tops/bottoms (S/R at extremes)
    "tweezer_top":    {"file": "pattern_09_support_resistance_break.png", "setup": "S8"},
    "tweezer_bottom": {"file": "pattern_09_support_resistance_break.png", "setup": "S8"},
    # Double top/bottom (chart_1-10 series)
    "double_top":    {"file": "chart_1.png", "setup": "S10"},
    "double_bottom": {"file": "chart_2.png", "setup": "S10"},
}

# Text descriptions for all setups — full detail from Tim's research docs
# Source: complete_visual_knowledge_base.md + visual_pattern_analysis.md (Feb 13 2026)
# Injected as text when that pattern is detected. Image injected separately if available.
_V4_SETUP_TEXT = {
    "S1": (
        "S1 — Hammer/Pin Bar Reversal (shots 1-5, 8, 10): Long wick rejection at support "
        "(BB lower band, S/R level, or pivot). Body small relative to wick (2:1+ wick:body ratio). "
        "Confirmation: next candle closes above hammer high. Best in ranging/oversold markets. "
        "Indicators: RSI <30, Stochastic <20, price at BB lower band. "
        "Shooting star = bearish mirror at resistance. "
        "PHASE 2.5 CONTEXT: Hammer at E100 during bullish fan retest = ACCUMULATION not reversal — do NOT reject."
    ),
    "S2": (
        "S2 — Engulfing Pattern (shots 2, 4, 7, 9): Bullish engulfing = bearish candle fully engulfed "
        "by next bullish candle. Bearish engulfing = opposite. Stronger at key S/R levels. "
        "Bigger body = more conviction. Three white soldiers (3 consecutive bullish) = strong continuation. "
        "Three black crows (3 consecutive bearish) = strong continuation down. "
        "Dark cloud cover = bearish reversal at resistance. Piercing line = bullish reversal at support. "
        "CRITICAL: In Phase 2.5 bullish fan, bullish engulfing at E100 = ACCUMULATION not distribution — "
        "do NOT call it a double top. Bearish engulfing at E100 during bearish fan = continuation short entry."
    ),
    "S3": (
        "S3 — Morning/Evening Star (shots 3, 5): Three-candle reversal. "
        "Morning star: big bearish → small body (doji/spinning top) → big bullish. "
        "Evening star: big bullish → small body → big bearish. "
        "Gap between candles strengthens signal. At BB bands = highest conviction. "
        "Confirms: trend exhaustion + reversal. Combine with RSI/Stoch extreme for entry."
    ),
    "S4": (
        "S4 — Doji at Extremes (shots 1, 6, 8): Open ≈ close = indecision → potential reversal. "
        "Must combine with RSI/Stochastic extreme. "
        "Dragonfly doji at support = bullish (long lower wick, tiny body at top). "
        "Gravestone doji at resistance = bearish (long upper wick, tiny body at bottom). "
        "Spinning top = indecision in both directions. "
        "In trending fan context, doji = pause not reversal unless at extreme RSI/Stoch + BB band."
    ),
    "S5": (
        "S5 — Ascending Triangle Breakout (shot 40): Flat resistance + rising higher lows. "
        "BUY on break above resistance with volume. SL below last higher low. "
        "Target: height of triangle projected from breakout. "
        "In Phase 2.5/3 fan context: ascending triangle within bullish fan = coiling before continuation — "
        "BUY the breakout, not the rejection."
    ),
    "S6": (
        "S6 — Descending Triangle Breakdown (shot 41): Flat support + falling lower highs. "
        "SELL on break below support. SL above last lower high. "
        "Target: height of triangle projected downward. "
        "In bearish fan context: descending triangle = coiling before continuation — SELL the breakdown."
    ),
    "S7": (
        "S7 — Channel Trading (shots 37, 38): Uptrend channel = buy at lower channel line, TP at upper. "
        "Inner waves (secondary trends) form within the channel. "
        "Fibonacci retracements at 50% within each wave. "
        "Entry: price touches lower channel + Fib 50% + bullish candle. SL below channel line. "
        "In EMA fan context: channel is the fan structure itself — lower channel = E100 support."
    ),
    "S8": (
        "S8 — Support/Resistance Break (shot 39): Ranging market between S/R lines. "
        "SELL when support breaks (close below), retest of broken support fails. "
        "SL1 (tight): just above broken support. SL2 (wide): above the range. "
        "Confirmation: bearish candle closes below support, next candle confirms. "
        "CRITICAL AT E100: Price sitting ON E100 from above in a bullish fan = SUPPORT zone, "
        "not breakdown. Price sitting on E100 from BELOW in bearish fan = RESISTANCE zone. "
        "Context from fan direction is MANDATORY for S/R interpretation."
    ),
    "S9": (
        "S9 — Head & Shoulders: Classic reversal — left shoulder, head (highest high), right shoulder. "
        "Neckline break = entry. Target: distance from head to neckline projected down from break. "
        "In trending fan: H&S at the end of long expansion = fan peaking. High reversal risk."
    ),
    "S10": (
        "S10 — Double Top/Bottom (chart series): Two touches at same resistance/support level = reversal zone. "
        "Detection: price makes two highs/lows within 0.1% of each other. "
        "Enter on neckline break. Target: height of pattern. "
        "PHASE 2.5 EXCEPTION: Double tops appearing at E100 during BULLISH fan retest = accumulation, NOT distribution. "
        "The fan direction overrides the pattern signal. Only treat double top as real reversal "
        "when fan is peaked/contracting OR when pattern forms at significant resistance above the fan."
    ),
    "S12": (
        "S12 — Bollinger Band Squeeze → Breakout (shot 51): BB narrows = compression, breakout imminent. "
        "'Only take signals in direction of overall trend.' "
        "If trend (fan) is bullish, only take BB expansions to the upside. "
        "If trend (fan) is bearish, only take BB expansions downward. "
        "BB squeeze + EMA fan cross = HIGHEST CONVICTION setup — the squeeze IS the setup, expansion IS the entry. "
        "EUR/CHF example: 10+ hours BB squeeze → double top at E100 → E21 crosses below E55 → explosive 100+ pip breakdown. "
        "ADX filter: confirm with ADX for direction."
    ),
    "S13": (
        "S13 — Slow Stochastic Oscillator (shot 53): "
        "BUY: Stochastic crosses UP from below 20 (oversold). "
        "SELL: Stochastic crosses DOWN from above 80 (overbought). "
        "Works best in RANGING markets (low ADX). In trending markets, use only for timing pullback entries. "
        "In EMA fan: Stochastic oversold during bullish fan expansion = buy the dip, not fade the trend."
    ),
    "S15": (
        "S15 — Momentum Divergence (shots 52, chart3, chart4, shot26): "
        "BEARISH: Price makes higher highs, RSI/Stoch makes LOWER highs = trend exhaustion. "
        "Detection: swing high in price > previous swing high, but RSI at that high < RSI at previous high by >5 pts. "
        "RSI should be in overbought territory (>60 min, ideally >70). "
        "Confirmation needed: bearish candle pattern (engulfing, shooting star, evening star). "
        "BULLISH (hidden): Price makes higher lows, indicator makes lower lows = continuation signal. "
        "CRITICAL RULE: Divergence AGAINST thesis direction = SKIP or reduce size. "
        "Divergence WITH thesis direction = add conviction. "
        "This is the #1 sell signal from all 55 screenshots. "
        "MACD version: MACD histogram crosses from positive to negative within last 5 bars + RSI >70 or declining from >70. "
        "Multi-timeframe: H4 divergence used to validate H1 entries."
    ),
    "S18": (
        "S18 — Fibonacci Retracement in Channels (shot 38): "
        "Within uptrend channel, each pullback retraces ~50% of prior leg before continuing. "
        "50% Fib is the key level — retracements to 32.8% = strong trend, 61.8% = golden ratio, >78.6% = potential reversal. "
        "Entry: 50% Fib + lower channel line confluence + bullish reversal candle. "
        "Stop below channel; TP at upper channel. "
        "In EMA fan: E100 often sits at the 50% Fib level during Phase 2.5 — this is WHY E100 retest = buy zone."
    ),
    "S19": (
        "S19 — ATR Volatility Context (shot 45): High ATR = wide stops needed, big moves possible. "
        "StdDev spike = trend change likely. Use ATR for dynamic SL/TP sizing. "
        "At 5-20 pip scale: 2 pips slippage = 20% of trade. Size positions accordingly."
    ),
    "S20": (
        "S20 — Multi-Pair Correlation (shots 43, 44): EUR/USD and GBP/USD move together. "
        "If trading correlated pairs in same direction, reduce position size (same directional risk). "
        "Use one as confirmation for the other — if EUR/USD bullish fan + GBP/USD bullish fan = higher conviction. "
        "If they diverge, treat as conflicting signal."
    ),
}

_V4_PATTERN_IMAGE_CACHE: dict = {}  # filename → base64 dict

def _detect_image_media_type(raw_bytes: bytes) -> str:
    """Detect actual image format from magic bytes. Returns 'image/png' or 'image/jpeg'.

    Critical: always send bytes with the matching media_type or Anthropic returns HTTP 400.
    PNG magic: \\x89PNG (bytes 0-3)
    JPEG magic: \\xff\\xd8\\xff (bytes 0-2)
    GIF magic: GIF8 (bytes 0-3) — not supported by Anthropic vision; convert to PNG instead
    """
    if raw_bytes[:4] == b'\x89PNG':
        return "image/png"
    if raw_bytes[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    if raw_bytes[:4] in (b'GIF8', b'RIFF'):
        return "image/png"  # caller should convert; flag as PNG so it's re-encoded
    return "image/png"  # safe default


def _load_pattern_image(filename: str) -> dict | None:
    """Lazy-load a single pattern image, cached by filename."""
    global _V4_PATTERN_IMAGE_CACHE
    if filename in _V4_PATTERN_IMAGE_CACHE:
        return _V4_PATTERN_IMAGE_CACHE[filename]

    fpath = os.path.join(_V4_PATTERN_DIR, filename)
    if not os.path.exists(fpath):
        logger.warning("[V4] Pattern image missing: %s", fpath)
        return None

    import base64 as _b64
    from PIL import Image as _PIL_Image
    import io as _io

    with open(fpath, "rb") as f:
        raw = f.read()
    try:
        img = _PIL_Image.open(_io.BytesIO(raw))
        w, h = img.size
        if max(w, h) > 1920:
            if w >= h:
                new_w, new_h = 1920, int(h * 1920 / w)
            else:
                new_h, new_w = 1920, int(w * 1920 / h)
            img = img.resize((new_w, new_h), _PIL_Image.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            raw = buf.getvalue()
        else:
            # No resize — normalize to PNG to guarantee media_type consistency
            buf = _io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            raw = buf.getvalue()
    except Exception:
        pass

    media_type = _detect_image_media_type(raw)
    entry = {
        "b64": _b64.standard_b64encode(raw).decode("utf-8"),
        "media_type": media_type,
        "description": f"Pattern reference: {filename}",
    }
    # Cap cache at 50 entries — there are only 27 pattern files so this is defense-in-depth
    if len(_V4_PATTERN_IMAGE_CACHE) >= 50:
        _keys = list(_V4_PATTERN_IMAGE_CACHE.keys())
        for _k in _keys[:25]:
            del _V4_PATTERN_IMAGE_CACHE[_k]
        logger.debug("[V4] Pattern cache evicted 25 oldest entries (size was ≥50)")
    _V4_PATTERN_IMAGE_CACHE[filename] = entry
    return entry


def _get_pattern_context(detected_patterns: list, chart_patterns: list, divergence: dict) -> tuple:
    """Return (images_list, text_block) for detected patterns.

    Images: unique pattern images for each detected pattern (max 3 to stay lean).
    Text: compact setup descriptions for all matched setups.
    """
    matched_setups = set()
    images_to_add = []
    seen_files = set()

    # Candlestick patterns
    for p in (detected_patterns or []):
        entry = _V4_PATTERN_MAP.get(p)
        if entry:
            matched_setups.add(entry["setup"])
            if entry["file"] not in seen_files:
                img = _load_pattern_image(entry["file"])
                if img:
                    img = dict(img)  # copy
                    img["description"] = f"Pattern ref ({p}): {_V4_SETUP_TEXT.get(entry['setup'], '')[:120]}"
                    images_to_add.append(img)
                    seen_files.add(entry["file"])

    # Chart patterns
    for cp in (chart_patterns or []):
        cp_type = cp.get("type", cp) if isinstance(cp, dict) else cp
        entry = _V4_PATTERN_MAP.get(str(cp_type).lower().replace(" ", "_"))
        if entry and entry["file"] not in seen_files:
            matched_setups.add(entry["setup"])
            img = _load_pattern_image(entry["file"])
            if img:
                img = dict(img)
                img["description"] = f"Pattern ref ({cp_type}): {_V4_SETUP_TEXT.get(entry['setup'], '')[:120]}"
                images_to_add.append(img)
                seen_files.add(entry["file"])

    # Divergence
    if isinstance(divergence, dict) and any(divergence.values()):
        matched_setups.add("S15")
        if "pattern_11_momentum_divergence.png" not in seen_files:
            img = _load_pattern_image("pattern_11_momentum_divergence.png")
            if img:
                img = dict(img)
                img["description"] = f"Pattern ref (divergence): {_V4_SETUP_TEXT['S15'][:120]}"
                images_to_add.append(img)
                seen_files.add("pattern_11_momentum_divergence.png")

    # Cap at 3 pattern images max
    images_to_add = images_to_add[:3]

    # Build text block
    text_lines = []
    if matched_setups:
        text_lines.append("### Pattern Reference (detected this cycle)")
        for setup_id in sorted(matched_setups):
            desc = _V4_SETUP_TEXT.get(setup_id)
            if desc:
                text_lines.append(f"- **{setup_id}**: {desc}")
        text_lines.append("")

    return images_to_add, "\n".join(text_lines)


def _load_v4_teaching_images() -> list:
    """Load and cache teaching images as base64 for vision-enabled validator.
    
    Returns list of dicts: [{"b64": "...", "media_type": "image/png", "description": "..."}]
    Loaded once per process, cached in _V4_TEACHING_IMAGES_CACHE.
    """
    global _V4_TEACHING_IMAGES_CACHE
    if _V4_TEACHING_IMAGES_CACHE:
        return _V4_TEACHING_IMAGES_CACHE

    import base64 as _b64
    from PIL import Image as _PIL_Image
    import io as _io
    loaded = []
    MAX_IMAGE_DIM = 1920  # Anthropic many-image limit is 2000px; use 1920 as safe margin
    for entry in _V4_TEACHING_MANIFEST:
        fpath = os.path.join(_V4_TEACHING_DIR, entry["file"])
        if not os.path.exists(fpath):
            logger.warning("[V4] Teaching image missing: %s", fpath)
            continue
        with open(fpath, "rb") as f:
            raw = f.read()
        # Auto-resize if either dimension exceeds limit; always normalize to PNG
        try:
            img = _PIL_Image.open(_io.BytesIO(raw))
            w, h = img.size
            if max(w, h) > MAX_IMAGE_DIM:
                if w >= h:
                    new_w, new_h = MAX_IMAGE_DIM, int(h * MAX_IMAGE_DIM / w)
                else:
                    new_h, new_w = MAX_IMAGE_DIM, int(w * MAX_IMAGE_DIM / h)
                img = img.resize((new_w, new_h), _PIL_Image.LANCZOS)
                logger.warning("[V4] Auto-resized teaching image %s: %dx%d → %dx%d",
                               entry["file"], w, h, new_w, new_h)
            # Always save as PNG — guarantees media_type matches bytes sent to Anthropic.
            # (Original file may be JPEG; hardcoded "image/png" caused HTTP 400 errors.)
            buf = _io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            raw = buf.getvalue()
        except Exception as _resize_err:
            logger.debug("[V4] Could not normalise image %s: %s", entry["file"], _resize_err)
        b64_data = _b64.standard_b64encode(raw).decode("utf-8")
        loaded.append({
            "b64": b64_data,
            "media_type": "image/png",  # guaranteed PNG after save above
            "description": entry["description"],
        })

    logger.info("[V4] Loaded %d/%d teaching images from %s", len(loaded), len(_V4_TEACHING_MANIFEST), _V4_TEACHING_DIR)
    _V4_TEACHING_IMAGES_CACHE = loaded
    return loaded


_LOCAL_VALIDATOR_IMAGES_CACHE = None

_LOCAL_VALIDATOR_MANIFEST = [
    {"file": "tim_teach_eurchf_annotated_short_snipe.png",
     "description": "REFERENCE TRADE — EUR/CHF SHORT SNIPE: Annotated chart showing EMA cross, EMA fan, Bollinger expansion, and short snipe entry. Full thesis with entry zone marked — the mirror pattern applies to bullish setups."},
]


def _load_local_validator_images() -> list:
    """Load 1 annotated teaching image for the local 35B validator.

    The distilled 35B already knows the setup patterns from training — it just
    needs ONE visual anchor showing how an annotated entry zone is marked on a
    chart. Dropped from 2→1 teaching image 2026-04-23 to cut prefill budget
    (~1-2K tokens saved). The bullish mirror is inferred by the model.
    """
    global _LOCAL_VALIDATOR_IMAGES_CACHE
    if _LOCAL_VALIDATOR_IMAGES_CACHE is not None:
        return _LOCAL_VALIDATOR_IMAGES_CACHE

    import base64 as _b64
    loaded = []
    for entry in _LOCAL_VALIDATOR_MANIFEST:
        fpath = os.path.join(_V4_TEACHING_DIR, entry["file"])
        if not os.path.exists(fpath):
            logger.warning("[V4-LOCAL] Teaching image missing: %s", fpath)
            continue
        with open(fpath, "rb") as f:
            raw = f.read()
        media_type = _detect_image_media_type(raw)
        loaded.append({
            "b64": _b64.standard_b64encode(raw).decode("utf-8"),
            "media_type": media_type,
            "description": entry["description"],
        })
    logger.info("[V4-LOCAL] Loaded %d/%d annotated teaching images", len(loaded), len(_LOCAL_VALIDATOR_MANIFEST))
    _LOCAL_VALIDATOR_IMAGES_CACHE = loaded
    return loaded


def _load_v4_chart_image(chart_path: str) -> dict:
    """Load a single chart image as base64 for the vision call.
    
    Returns: {"b64": "...", "media_type": "image/png", "description": "Current M15 chart"}
    Returns None if path is missing, file doesn't exist, or file is too small (< 5KB = blank/corrupt).
    """
    import base64 as _b64
    if not chart_path or not os.path.exists(chart_path):
        return None
    file_size = os.path.getsize(chart_path)
    if file_size < 50000:  # < 50KB = sparse/broken chart (normal full charts are 108-120KB)
        logger.warning("[V4] Chart image too small (%d bytes) — likely broken/incomplete, skipping: %s", file_size, chart_path)
        return None
    with open(chart_path, "rb") as f:
        raw = f.read()
    # Detect actual format — chart_generator may save JPEG despite .png extension
    media_type = _detect_image_media_type(raw)
    return {
        "b64": _b64.standard_b64encode(raw).decode("utf-8"),
        "media_type": media_type,
        "description": "CURRENT SETUP — Evaluate this chart against the teaching examples above.",
    }


def _direct_ta_call(task: str, max_tokens: int = 800) -> dict:
    """Direct local MLX call for TA — 35B (CSO model).

    2026-04-27: Trading team flipped from 9B (port 11500) to 35B (port 11502)
    on 2026-04-26. Direct call now goes through the gateway at 11503 which
    forwards to 35B with trading-tenant priority queue. Port 11500 is dead.
    
    TA is a camera (describe numbers) not a reasoner. 9B handles this well.
    Falls back to swarm (team_setup model assignments) if local model is unavailable.
    """
    import urllib.request as _urlreq
    import json as _json

    _TA_PORT = 11503  # gateway → forwards to MLX 35B at 11502 with trading priority
    _TA_URL = f"http://127.0.0.1:{_TA_PORT}/v1/chat/completions"
    # 2026-04-27: System prompt now LOADED from technical_analyst_v4.md (the
    # canonical TA prompt the swarm path also uses). Single source of truth so
    # edits to v4.md flow into both direct and swarm-fallback paths.
    try:
        with open("<repo_root>/Prompts/technical_analyst_v4.md") as _tap:
            _SYSTEM = _tap.read()
    except Exception:
        _SYSTEM = (
            "You are a technical analyst. Your ONLY job is to describe what indicator data shows. "
            "No trading recommendations. No decisions. Return valid JSON only — no markdown, no preamble."
        )

    payload = _json.dumps({
        "model": "mlx-community/Qwen3.5-35B-A3B-4bit",
        "tenant": "trading",  # priority 0 in the gateway queue
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": task},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        # Disable Qwen3 extended thinking — the TA is a data camera, not a reasoner.
        # Without this, Qwen3.5-9B generates 10-16K tokens of <think> reasoning before
        # answering, taking 38-66 seconds per call instead of 8-12 seconds.
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()

    try:
        req = _urlreq.Request(_TA_URL, data=payload,
                              headers={
                                  "Content-Type": "application/json",
                                  "X-Jarvis-Tenant": "trading",
                              },
                              method="POST")
        # 180s timeout: covers the physical queue wait when scout fires 5 pairs concurrently
        # (cycle 5's TA waits up to 100s behind cycles 1-4 at ~25s each + own 25s run).
        # Prior 45s timed out during parallel cycles 2026-04-23.
        with _urlreq.urlopen(req, timeout=180) as resp:
            result = _json.loads(resp.read())
        text = result["choices"][0]["message"]["content"]
        # Minimum length check — empty/trivial response triggers swarm fallback same as an exception
        if not text or len(text.strip()) < 50:
            raise ValueError(f"MLX returned trivial response ({len(text.strip())} chars): {repr(text[:40])}")
        logger.info("[TA] Local 9B response: %d chars, model=mlx/CRO", len(text))
        return {
            "response": text,
            "model": "mlx/CRO-9B",
        }
    except Exception as _local_err:
        logger.error("[TA] Local 9B failed (%s) — routing to swarm (no Haiku fallback)", _local_err)
        # 300s covers physical queue wait when multiple TAs hit the 9B after direct-path failure
        return _agent_task("technical_analyst", task, max_tokens=max_tokens, timeout=300.0)


def _agent_task(agent_name: str, task: str, context: dict = None,
                max_tokens: int = 4096, timeout: float = 120.0,
                max_tool_rounds: int = 10, images: list = None) -> dict:
    """Execute a task using an agent's LLM with MCP tool access.

    This is the primary agent execution path. The agent:
    1. Receives the task + context
    2. Reasons about what MCP tools to call
    3. Calls tools, interprets results
    4. Returns structured response

    Parameters
    ----------
    images : list, optional
        List of image dicts for vision-enabled agents. Each dict:
        {"b64": "<base64 data>", "media_type": "image/png", "description": "text label"}
        Images are interleaved with text in the user message content.

    Returns the full result dict with 'response', 'tool_calls', etc.
    """
    params = {
        "agent_name": agent_name,
        "task": task,
        "context": context or {},
        "max_tokens": max_tokens,
        "max_tool_rounds": max_tool_rounds,
    }
    if images:
        params["images"] = images
    # Acquire semaphore before making the Claude API call to bound concurrency
    with _CLAUDE_SEMAPHORE:
        result = _run_swarm("execute_agent_task", params, timeout=timeout)
    # execute_agent_task returns HandlerResult; extract data dict
    if not isinstance(result, dict):
        result = {"response": str(result)}

    # Ghost validator: DISABLED during live trading (35B uses too much memory with vision).
    # Ghost comparison runs at end of day via batch replay instead.
    # To re-enable: change False to True below.
    _GHOST_ENABLED = False
    if _GHOST_ENABLED and agent_name == "validator" and result.get("response"):
        try:
            import threading
            _ghost_task = task
            _ghost_images = images
            _ghost_anthropic_response = result.get("response", "")
            _ghost_pair = (context or {}).get("instrument", "UNKNOWN")

            def _run_ghost_validator():
                try:
                    from openai import OpenAI
                    import json, re, sqlite3, os
                    from datetime import datetime, timezone

                    client = OpenAI(base_url="http://localhost:11503", api_key="mlx-local")  # serving gateway → MLX 35B

                    # Build messages matching what Anthropic got
                    user_content = []
                    if _ghost_images:
                        for img in _ghost_images:
                            if img.get("description"):
                                user_content.append({"type": "text", "text": img["description"]})
                            if img.get("b64"):
                                media = img.get("media_type", "image/png")
                                user_content.append({
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{media};base64,{img['b64']}"},
                                })
                    # Add task + explicit JSON instruction for local model
                    user_content.append({"type": "text", "text": _ghost_task})
                    user_content.append({"type": "text", "text": (
                        "\n\nIMPORTANT: You MUST respond with a JSON object. "
                        "Start your response with ```json and end with ```. "
                        "Required fields: verdict (TRADE_NOW, WATCH, or SKIP), "
                        "direction (BUY or SELL), confidence (0.0-1.0), "
                        "reasoning (string explaining your decision). Example:\n"
                        '```json\n{"verdict": "WATCH", "direction": "BUY", '
                        '"confidence": 0.7, "reasoning": "Fan expanding but RSI overbought"}\n```'
                    )})

                    resp = client.chat.completions.create(
                        model="mlx-community/Qwen3.5-35B-A3B-4bit",
                        messages=[
                            {"role": "user", "content": user_content},
                        ],
                        max_tokens=2500,
                        temperature=0,
                        extra_headers={"X-Jarvis-Tenant": "background"},
                    )
                    local_response = resp.choices[0].message.content or ""
                    # Strip Qwen thinking tags
                    local_response = re.sub(r"<think>[\s\S]*?</think>", "", local_response).strip()

                    # Parse verdicts from both — responses are prose with embedded JSON
                    def _extract(raw):
                        text = raw.strip()
                        # Method 1: find ```json ... ``` code blocks
                        code_blocks = re.findall(r'```(?:json)?\s*\n([\s\S]*?)```', text)
                        for block in code_blocks:
                            try:
                                p = json.loads(block.strip())
                                if "verdict" in p:
                                    return _build_result(p)
                            except Exception:
                                continue
                        # Method 2: find balanced braces containing "verdict"
                        # Walk through text finding { ... } pairs
                        for i, ch in enumerate(text):
                            if ch == '{':
                                depth = 1
                                j = i + 1
                                while j < len(text) and depth > 0:
                                    if text[j] == '{': depth += 1
                                    elif text[j] == '}': depth -= 1
                                    j += 1
                                candidate = text[i:j]
                                if '"verdict"' in candidate:
                                    try:
                                        p = json.loads(candidate)
                                        return _build_result(p)
                                    except Exception:
                                        continue
                        return {"verdict": "PARSE_ERROR", "direction": None,
                                "confidence": 0.0, "reasoning": ""}

                    def _build_result(p):
                        return {
                            "verdict": str(p.get("verdict", "PARSE_ERROR")).upper(),
                            "direction": str(p.get("direction", "")).upper() or None,
                            "confidence": float(p.get("confidence", 0)),
                            "reasoning": str(p.get("reasoning", "")),
                        }

                    anth = _extract(_ghost_anthropic_response)
                    loc = _extract(local_response)
                    v_match = anth["verdict"] == loc["verdict"]
                    d_match = anth["direction"] == loc["direction"]
                    c_delta = abs(anth["confidence"] - loc["confidence"])

                    logger.info(
                        "[GHOST] %s: Anthropic=%s/%s(%.2f) vs Local=%s/%s(%.2f) — verdict %s, direction %s",
                        _ghost_pair, anth["verdict"], anth["direction"], anth["confidence"],
                        loc["verdict"], loc["direction"], loc["confidence"],
                        "MATCH" if v_match else "MISMATCH",
                        "MATCH" if d_match else "MISMATCH",
                    )

                    db_path = os.path.expanduser(
                        "~/Jarvis/Database/v2/trading_forex.db"
                    )
                    conn = sqlite3.connect(db_path, timeout=10)
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS ghost_verdicts (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            pair TEXT NOT NULL,
                            anthropic_verdict TEXT,
                            anthropic_direction TEXT,
                            anthropic_confidence REAL,
                            anthropic_reasoning TEXT,
                            anthropic_raw_response TEXT,
                            local_verdict TEXT,
                            local_direction TEXT,
                            local_confidence REAL,
                            local_reasoning TEXT,
                            local_raw_response TEXT,
                            verdict_match BOOLEAN,
                            direction_match BOOLEAN,
                            confidence_delta REAL,
                            local_model TEXT
                        )
                    """)
                    conn.execute("""
                        INSERT INTO ghost_verdicts (
                            timestamp, pair,
                            anthropic_verdict, anthropic_direction, anthropic_confidence, anthropic_reasoning,
                            anthropic_raw_response,
                            local_verdict, local_direction, local_confidence, local_reasoning,
                            local_raw_response,
                            verdict_match, direction_match, confidence_delta, local_model
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        datetime.now(timezone.utc).isoformat(), _ghost_pair,
                        anth["verdict"], anth["direction"], anth["confidence"], anth["reasoning"],
                        _ghost_anthropic_response,
                        loc["verdict"], loc["direction"], loc["confidence"], loc["reasoning"],
                        local_response,
                        v_match, d_match, c_delta,
                        "mlx-community/Qwen3.5-35B-A3B-4bit",
                    ))
                    conn.commit()
                    conn.close()
                except Exception as ge:
                    logger.warning("[GHOST] Local model comparison failed: %s", ge)

            threading.Thread(target=_run_ghost_validator, daemon=True).start()
        except Exception as e:
            logger.debug("[GHOST] Failed to launch ghost thread: %s", e)

    # Log agent's work to comms table so dashboard can show it
    tool_calls = result.get("tool_calls", [])
    response_preview = result.get("response", "")[:500]
    tokens_in = result.get("input_tokens", 0)
    tokens_out = result.get("output_tokens", 0)
    rounds = result.get("rounds", 0)
    
    # Build detailed activity summary
    tool_summary = ", ".join(tc.get("tool", "?") for tc in tool_calls[:5])
    if len(tool_calls) > 5:
        tool_summary += f" (+{len(tool_calls)-5} more)"
    
    activity = f"[{agent_name}] {len(tool_calls)} tool calls"
    if tool_summary:
        activity += f": {tool_summary}"
    activity += f" | {rounds} rounds | {tokens_in}in/{tokens_out}out tokens"
    
    _swarm_send_message(agent_name, "cycle_orchestrator", activity)
    
    # Also log the agent's response summary
    if response_preview:
        _swarm_send_message(agent_name, "cycle_orchestrator",
            f"[{agent_name} response] {response_preview[:300]}")
    
    return result


def _swarm_distribute_tasks(tasks: list, strategy: str = "round_robin") -> dict:
    """Shortcut: swarm.distribute_tasks(tasks, strategy)."""
    return _run_swarm("distribute_tasks", {
        "tasks": tasks,
        "strategy": strategy,
    })


def _swarm_coordinate_parallel(tasks: list, timeout: float = 30.0) -> dict:
    """Shortcut: swarm.coordinate_parallel(tasks, timeout)."""
    return _run_swarm("coordinate_parallel", {
        "tasks": tasks,
        "timeout": timeout,
    })


def _swarm_send_message(from_agent: str, to_agent: str, message: str) -> dict:
    """Log inter-agent communication directly to conversations.db (thread-safe)."""
    conn = None
    try:
        import sqlite3 as _sql3, hashlib
        db_path = str(Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "conversations.db")
        msg_id = f"msg_{int(time.time())}_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"
        ts = datetime.now(timezone.utc).isoformat()
        conn = _sql3.connect(db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """INSERT INTO agent_communications (
                id, from_agent_id, to_agent_id, workspace_id,
                message, context, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, from_agent, to_agent, "default",
             message, '{}', ts),
        )
        conn.commit()
        logger.info("Agent Communication: %s -> %s, Content: %s", from_agent, to_agent, message[:60])
        return {"sent": True, "from": from_agent, "to": to_agent}
    except Exception as exc:
        logger.warning("send_message %s->%s failed: %s", from_agent, to_agent, exc)
        return {"sent": False, "error": str(exc)}
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# LLM-powered orchestrator decision
# ---------------------------------------------------------------------------

_orchestrator_prompt = None

def _load_orchestrator_prompt() -> str:
    """Load orchestrator prompt from vault (canonical), fallback to legacy path."""
    global _orchestrator_prompt
    if _orchestrator_prompt is None:
        # Vault is canonical source
        jarvis_root = Path(__file__).parent.parent.parent.parent
        vault_path  = jarvis_root / "knowledge" / "agents" / "cycle_orchestrator" / "prompt.md"
        legacy_path = Path(__file__).parent.parent.parent / "Prompts" / "cycle_orchestrator_v4.md"

        if vault_path.exists():
            _orchestrator_prompt = vault_path.read_text()
            logger.info("Loaded cycle_orchestrator prompt from vault")
        elif legacy_path.exists():
            _orchestrator_prompt = legacy_path.read_text()
            logger.warning("cycle_orchestrator: using legacy Prompts/ path (migrate to vault)")
        else:
            _orchestrator_prompt = "You are the head trader. Make a trade decision based on all available data."
            logger.warning("cycle_orchestrator prompt not found in vault or legacy path")
    return _orchestrator_prompt



# V3: Orchestrator LLM decision function removed (364 lines). Validator is sole authority.
# Backup: trading_cycle.py.backup_v3 | Design: notes/pipeline-v3-design-2026-02-28.md


class TradingCycle:
    """Orchestrates a complete trading cycle through SwarmHandler.

    All agent operations route through SwarmHandler (execute_tool,
    distribute_tasks, coordinate_parallel) instead of direct Python calls.
    """

    def __init__(
        self,
        team_setup: TradingTeamSetup,
        comment_protocol: CommentProtocol,
        user_id: int = None,
    ):
        """Initialise with team infrastructure and communication protocol.

        Parameters
        ----------
        team_setup : TradingTeamSetup
            Provides workspace IDs and agent registration state.
        comment_protocol : CommentProtocol
            Handles task creation and inter-agent comment posting.
        """
        self._team = team_setup
        self._protocol = comment_protocol
        self.user_id = user_id
        self._cycle_count = 0
        self._last_cycle_time: Optional[str] = None
        self._logger_instance = None
        self.live_cycle_result: Optional[Dict[str, Any]] = None  # Exposed for live progress

        # Resolve orchestrator model from team_setup AGENT_SPECS
        # V3: Orchestrator is the cycle coordinator (not the trade decision maker)
        from .team_setup import AGENT_SPECS
        self._orchestrator_model = "mlx/CRO-9B"  # default — overridden by AGENT_SPECS if cycle_orchestrator defined
        for spec in AGENT_SPECS:
            if spec.get("name") == "cycle_orchestrator":
                self._orchestrator_model = spec.get("model", self._orchestrator_model)
                break

        # Share the SwarmHandler that team_setup populated with agents
        # so _get_swarm() returns the instance with registered agents/tools
        global _swarm
        if hasattr(team_setup, 'swarm') and team_setup.swarm is not None:
            _swarm = team_setup.swarm

    @staticmethod
    @staticmethod
    def _format_classified_setups(sc: dict) -> str:
        """Format S1-S20 classified setups for agent prompts."""
        if not sc or not sc.get('market_snapshot'):
            return "No S1-S20 setup classification available (no Scout context)."
        
        setups = sc.get('market_snapshot', {}).get('classified_setups', [])
        if not setups:
            return "No S1-S20 setups detected in current conditions."
        
        lines = []
        for s in setups:
            regime_tag = "✅" if s.get('regime_valid') else "⚠️ REGIME MISMATCH"
            candle_tag = " +candle confirmation" if s.get('candle_confirmation') else ""
            lines.append(
                f"- {regime_tag} **{s['setup']}** {s['name']} → {s['direction'].upper()} "
                f"(confidence={s['confidence']:.0%}{candle_tag})"
            )
        return "\n".join(lines)

    def _format_scout_context_for_ta(self, sc: dict) -> str:
        """Format scout context as a comprehensive section for the TA agent prompt."""
        if not sc:
            return ""
        snap = sc.get('market_snapshot', {}) or {}

        # ── Staleness tracking ──
        queued_at = sc.get('queued_at', 0)
        staleness_sec = int(time.time() - queued_at) if queued_at else 0
        staleness_min = staleness_sec / 60

        lines = [
            "## 6. SCOUT CONTEXT (what triggered this cycle)\n",
        ]

        # Staleness warning — always show so TA knows the time gap
        if staleness_sec > 0:
            lines.append(f"⏱️ **STALENESS: Scout detected this signal {staleness_sec}s ({staleness_min:.1f} min) ago.**")
            if staleness_min > 2:
                lines.append(f"⚠️ **SIGNIFICANT DELAY** — {staleness_min:.0f} minutes have passed since detection. "
                            f"Indicators have likely shifted. The entry window Scout identified may have CLOSED. "
                            f"Your fresh data is the ground truth — Scout's snapshot is historical context only.")
            else:
                lines.append(f"Moderate delay. Your fresh analysis takes priority over Scout's snapshot values. "
                            f"Compare key indicators (RSI, Stoch, price vs BB) for drift.")
            lines.append("")

        # Show scout's trigger values so TA can compare
        if snap and staleness_sec > 0:
            lines.append("### 📊 DRIFT CHECK — Scout's values at detection vs your fresh data")
            lines.append("Compare these to your current readings. If key triggers have reverted, the window may be closed:")
            _drift_items = []
            if snap.get('rsi') is not None:
                _drift_items.append(f"RSI was {snap['rsi']:.1f} ({snap.get('rsi_zone', '?')})")
            if snap.get('stoch_k') is not None:
                _drift_items.append(f"Stoch K/D was {snap['stoch_k']:.1f}/{snap.get('stoch_d', 0):.1f} ({snap.get('stoch_zone', '?')})")
            if snap.get('price') is not None:
                _drift_items.append(f"Price was {snap['price']}")
            if snap.get('bb_position') is not None:
                _drift_items.append(f"BB position was {snap['bb_position']}")
            if snap.get('adx') is not None:
                _drift_items.append(f"ADX was {snap['adx']:.1f}")
            for item in _drift_items:
                lines.append(f"  - {item}")
            lines.append("")
            lines.append("**YOUR TASK**: In your summary, include a `signal_drift` field rating the drift as "
                        "`MINIMAL` (triggers still valid), `MODERATE` (weakened but arguable), or `EXPIRED` "
                        "(key triggers reverted, entry window closed). If EXPIRED, set setup_quality to NONE.")
            lines.append("")

        # 2026-05-10: scout now injects (setup, pair) lifetime track record via
        # trade_scout._lookup_setup_track_record. Show full economic context so
        # the validator/TA weight setups by their historical edge, not just by
        # current indicator alignment.
        _setup_n = sc.get('setup_name', '?')
        _dir_s = sc.get('direction', '?').upper()
        _wr = sc.get('win_rate', 0)
        _tc = sc.get('trade_count', 0)
        _gross_usd = sc.get('gross_revenue', 0)
        _gross_pips = sc.get('gross_revenue_pips', 0)
        _wins = sc.get('wins', 0)
        _losses = sc.get('losses', 0)
        _promoted = sc.get('promoted', False)
        _pf = sc.get('profit_factor')
        _pf_str = '?' if _pf is None else ('∞' if _pf == float('inf') else f"{_pf:.2f}")
        _promoted_tag = "🎯 AUTO-PROMOTED" if _promoted else ""
        if _tc > 0:
            _history_line = (
                f"- Track Record on this pair: {_wins}W/{_losses}L ({_wr:.1f}% WR over {_tc} trades) | "
                f"Gross: ${_gross_usd:+.0f} / {_gross_pips:+.1f}p | PF={_pf_str} {_promoted_tag}".rstrip()
            )
        else:
            _history_line = "- Track Record on this pair: no prior trades (new setup × pair combo)"

        lines.extend([
            f"Trade Scout detected: **{_setup_n}** → {_dir_s}",
            _history_line,
            f"- Sniper Score: {sc.get('score', '?')} | Scout Confidence: {sc.get('scout_confidence', '?')} ({sc.get('confidence_tier', '?')})",
        ])
        if sc.get('reasoning'):
            lines.append(f"- Scout Reasoning: {sc['reasoning']}")
        if sc.get('candle_pattern') and sc['candle_pattern'] != 'None':
            lines.append(f"- Candle Patterns: {sc['candle_pattern']}")

        if snap:
            lines.append("\n### Scout's Indicator Snapshot (at detection time)")
            lines.append(f"- Price: {snap.get('price', '?')}")
            lines.append(f"- EMA Fan: {snap.get('fan_direction', '?')} {snap.get('fan_state', '?')} "
                        f"(ordered={snap.get('fan_ordered', '?')}, health={snap.get('trend_health', '?')}/100)")
            lines.append(f"- Separation: {snap.get('separation_pct', 0):.4f}% | "
                        f"Velocity: {snap.get('separation_velocity', 0):.6f}%/bar ({snap.get('velocity_trend', '?')})")
            lines.append(f"- Reversal Risk: {snap.get('reversal_risk', '?')} | Bias: {snap.get('recommended_bias', '?')}")
            lines.append(f"- RSI: {snap.get('rsi', '?')} ({snap.get('rsi_zone', '?')}) | "
                        f"Stoch: {snap.get('stoch_k', '?')}/{snap.get('stoch_d', '?')} ({snap.get('stoch_zone', '?')})")
            lines.append(f"- MACD: {snap.get('macd', '?')} sig={snap.get('macd_signal', '?')} hist={snap.get('macd_histogram', '?')}")
            lines.append(f"- BB: {snap.get('bb_position', '?')} | ADX: {snap.get('adx', '?')} → regime={snap.get('regime', '?')}")
            lines.append(f"- ATR: {snap.get('atr', '?')} | SAR: {'bullish' if snap.get('sar_bullish') else 'bearish'}")
            lines.append(f"- Consec candles: bull={snap.get('consec_bull', 0)} bear={snap.get('consec_bear', 0)}")
            lines.append(f"- H4 Bias: {snap.get('h4_bias', '?')}" + (f" | H4 RSI: {snap.get('h4_rsi', '?')}" if snap.get('h4_rsi') else ""))
            lines.append(f"- Bull/Bear scores: {snap.get('bull_score', '?')}/{snap.get('bear_score', '?')}")
            lines.append(f"- Session: {', '.join(snap.get('active_sessions', []))} (quality={snap.get('session_quality', '?')}, prime={snap.get('is_prime_time', '?')})")
            if snap.get('candle_patterns'):
                lines.append(f"- Detected patterns: {', '.join(snap['candle_patterns'])}")
            if snap.get('profile_confidence'):
                lines.append(f"- Profile engine: confidence={snap['profile_confidence']:.3f}, "
                            f"hist_wr={snap.get('profile_historical_wr', '?')}%, "
                            f"TP={snap.get('profile_suggested_tp', '?')} SL={snap.get('profile_suggested_sl', '?')} pips")
            if snap.get('confluence_narrative'):
                lines.append(f"- Confluence: {snap['confluence_narrative'][:300]}")
            if snap.get('recent_candles'):
                lines.append(f"\n### Last {len(snap['recent_candles'])} M15 candles (OHLCV)")
                for i, c in enumerate(snap['recent_candles']):
                    body = "▲" if c['close'] >= c['open'] else "▼"
                    lines.append(f"  {body} O={c['open']:.5f} H={c['high']:.5f} L={c['low']:.5f} C={c['close']:.5f}")

        # Snipe-in-thesis context from combined playbook
        stc = sc.get('snipe_thesis_context')
        if stc and stc.get('has_context'):
            lines.append(f"\n### 🎯 COMBINED PLAYBOOK MATCH: {stc['play_id']}")
            lines.append(f"- Play Type: **{stc['play_type'].upper().replace('_', ' ')}**")
            lines.append(f"- Thesis direction: {stc.get('thesis_direction', '?')} | Snipe phase: {stc.get('snipe_phase', '?')} | Alignment: {stc.get('snipe_alignment', '?')}")
            lines.append(f"- Backtested: {stc['snipe_wr']}% WR, PF {stc['snipe_pf']} over {stc.get('snipe_trades', '?')} trades")
            if stc.get('snipe_setups'):
                lines.append(f"- Best S-codes in this context: {', '.join(stc['snipe_setups'])}")
            lines.append(f"- Confidence boost applied: +{stc['boost']}%")
            lines.append(f"\n**This snipe is firing during a backtested thesis window. The combined playbook says this is a {stc['play_type']} setup. Weight this context heavily.**\n")

        lines.append("\n**COMPARE** your fresh analysis against Scout's snapshot. Confirm or refute the setup. "
                     "Note any changes since Scout's detection time.\n\n")
        return "\n".join(lines)

    @staticmethod
    def _format_snipe_context_for_ta(sc: dict) -> str:
        """Format snipe (validator HOLD) context as a section for the TA agent prompt.
        
        Unlike Scout context which is a proactive scan, snipe context contains
        the Validator's SPECIFIC thesis about what needs to happen for this trade
        to become viable. The TA must validate this thesis against the current market.
        """
        if not sc:
            return ""
        
        lines = [
            "## 6. SNIPE TRIGGER — VALIDATOR'S ORIGINAL THESIS\n",
            "**This cycle was triggered by a snipe (Validator HOLD condition that just hit).**",
            "The Validator previously analyzed this pair and said 'not yet, but watch for these conditions.'",
            "Those conditions just triggered. Your job: run your full analysis and confirm whether",
            "the Validator's original thesis still holds NOW.\n",
        ]
        
        # Original thesis
        if sc.get('setup_story'):
            lines.append(f"### Validator's Story")
            lines.append(f"{sc['setup_story']}\n")
        
        direction = sc.get('direction', 'unknown')
        lines.append(f"### Snipe Details")
        lines.append(f"- **Direction**: {direction.upper()}")
        lines.append(f"- **Story Score**: {sc.get('opportunity_score', sc.get('story_score', '?'))}/100 | Type: {sc.get('story_entry_type', sc.get('entry_type', '?'))}")
        
        if sc.get('db_win_rate'):
            lines.append(f"- **DB Evidence**: {sc['db_win_rate']}% WR, PF={sc.get('db_profit_factor', '?')}, {sc.get('db_trade_count', '?')} trades")
        if sc.get('db_setup'):
            lines.append(f"- **Setup**: {sc['db_setup']}")
        if sc.get('setup_name'):
            lines.append(f"- **Setup Name**: {sc['setup_name']}")
        if sc.get('confluence_score'):
            lines.append(f"- **Original Confluence**: {sc['confluence_score']}/{sc.get('confluence_min', 50)} (at time of HOLD)")
        if sc.get('key_signals'):
            lines.append(f"- **Key Signals**: {', '.join(sc['key_signals']) if isinstance(sc['key_signals'], list) else sc['key_signals']}")
        if sc.get('validator_reasoning'):
            lines.append(f"- **Validator Reasoning**: {sc['validator_reasoning']}")
        
        # What conditions just triggered
        conditions_met = sc.get('conditions_met', [])
        if conditions_met:
            lines.append(f"\n### Conditions That Just Triggered")
            for cm in conditions_met:
                status = "✅" if cm.get('met', True) else "❌"
                lines.append(f"  {status} {cm.get('field', '?')}: current={cm.get('current', '?')} (target: {cm.get('target', '?')})")
        
        # Current Scout-computed data if available
        if sc.get('market_snapshot'):
            lines.append(f"\n### Current Market (from Scout's scan)")
        
        snap = sc.get('market_snapshot', {})
        if snap:
            lines.append(f"- EMA Fan: {snap.get('fan_direction', '?')} {snap.get('fan_state', '?')} (health={snap.get('trend_health', '?')})")
            lines.append(f"- RSI: {snap.get('rsi', '?')} | Stoch: {snap.get('stoch_k', '?')}/{snap.get('stoch_d', '?')}")
            lines.append(f"- BB: {snap.get('bb_position', '?')} | ADX: {snap.get('adx', '?')}")
            lines.append(f"- Session: {', '.join(snap.get('active_sessions', []))}")
            if snap.get('profile_confidence'):
                lines.append(f"- Profile: confidence={snap['profile_confidence']:.3f}, hist_wr={snap.get('profile_historical_wr', '?')}%")
        
        lines.append(f"\n**VALIDATE**: Does your fresh M15 analysis support the Validator's {direction.upper()} thesis?")
        lines.append(f"The conditions the Validator was waiting for have been met — but has the broader market context changed?")
        lines.append(f"If your analysis confirms the direction, this is a high-confidence entry (pre-validated + conditions met).")
        lines.append(f"If your analysis contradicts the direction, flag it — conditions met doesn't mean the trade is still valid.\n\n")
        
        return "\n".join(lines)

    def _get_logger(self):
        """Return the shared TradeLogger (lazy)."""
        if self._logger_instance is None:
            self._logger_instance = _get_trade_logger()
        return self._logger_instance

    def _write_dashboard_data(self, cycle_num: int, instrument: str, decision: dict, 
                              phase_timings: dict, summary: dict) -> None:
        """Write cycle data to dashboard/cycle_data.json for real-time dashboard."""
        try:
            import json
            from pathlib import Path
            
            dashboard_dir = Path(__file__).parent.parent.parent / "dashboard"
            dashboard_dir.mkdir(exist_ok=True)
            
            dashboard_data = {
                "cycle_number": cycle_num,
                "instrument": instrument,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "decision": {
                    "action": decision.get("action", "hold") if isinstance(decision, dict) else "hold",
                    "allowed": decision.get("allowed", False) if isinstance(decision, dict) else False,
                    "confidence": decision.get("confidence", 0.0) if isinstance(decision, dict) else 0.0,
                    "reasoning": decision.get("reasoning", "") if isinstance(decision, dict) else "",
                },
                "timing_breakdown": phase_timings,
                "agent_actions": {
                    "data_collection": bool(self._cycle_count > 0),
                    "intelligence_gathered": True,
                    "technical_analysis": True,
                    "decision_made": True,
                    "execution_attempted": decision.get("action") not in ["hold", None] if isinstance(decision, dict) else False,
                    "reporting_completed": True,
                },
                "position_summary": {
                    "trade_placed": summary.get("trade_placed", False) if isinstance(summary, dict) else False,
                    "direction": decision.get("action") if isinstance(decision, dict) else None,
                    "size": decision.get("position_size", 0) if isinstance(decision, dict) else 0,
                },
                "performance": {
                    "total_time_seconds": sum(phase_timings.values()),
                    "phases_completed": len(phase_timings),
                }
            }
            
            dashboard_file = dashboard_dir / "cycle_data.json"
            with open(dashboard_file, 'w') as f:
                json.dump(dashboard_data, f, indent=2)
                
            logger.info("Updated dashboard data: cycle #%d, %s %s", 
                       cycle_num, instrument, decision.get("action", "hold") if isinstance(decision, dict) else "hold")
                       
        except Exception as exc:
            logger.warning("Failed to write dashboard data: %s", exc)

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def run_cycle(self, instrument: str = None, timeframe: str = "M15", scout_context: dict = None, test_mode: bool = False) -> dict:
        """Run one complete trading cycle for an instrument.

        All agent calls route through SwarmHandler (execute_tool,
        distribute_tasks, coordinate_parallel).

        Parameters
        ----------
        instrument : str
            Instrument to trade (e.g. ``"EUR_USD"``).
        timeframe : str
            Primary timeframe (default ``"M15"``).
        scout_context : dict, optional
            Context from Trade Scout: setup_name, direction, win_rate,
            trade_count, profit_factor, reasoning, score, fan_state, etc.

        Returns
        -------
        dict
            Complete cycle result including all step outputs, decision,
            execution (if any), and summary.
        """
        # Use active pair from database if no instrument specified
        if instrument is None:
            instrument = _get_active_pair_from_db()
            logger.info("Using active pair from database: %s", instrument)
        
        cycle_start = datetime.now(timezone.utc).isoformat()
        cycle_clock = time.time()
        self._cycle_count += 1
        cycle_num = self._cycle_count

        logger.info(
            "=== Trading Cycle #%d: %s %s ===",
            cycle_num, instrument, timeframe,
        )

        # Phase timing tracker
        phase_timings: Dict[str, float] = {}

        # Market hours check BEFORE pre-check step
        def is_forex_market_open() -> bool:
            """Check if forex market is open (Sun 5pm ET - Fri 5pm ET)"""
            now_utc = datetime.now(timezone.utc)
            # Convert to ET (approximate)
            import zoneinfo
            try:
                et_tz = zoneinfo.ZoneInfo("America/New_York")
                now_et = now_utc.astimezone(et_tz)
            except Exception:
                # Fallback: approximate ET as UTC-5 (EST) or UTC-4 (EDT)
                # For simplicity, we'll use a basic approximation
                import time
                is_dst = time.daylight and time.localtime().tm_isdst
                offset = -4 if is_dst else -5  # EDT vs EST
                et_offset = offset * 3600
                now_et = datetime.fromtimestamp(now_utc.timestamp() + et_offset)
            
            day_of_week = now_et.weekday()  # 0=Monday, 6=Sunday
            hour = now_et.hour
            
            # Convert to Sunday=0 format for easier logic
            sunday_week_day = (day_of_week + 1) % 7  # 0=Sunday, 6=Saturday
            
            if sunday_week_day == 0:  # Sunday
                return hour >= 17  # 5pm ET or later
            elif 1 <= sunday_week_day <= 4:  # Monday-Thursday
                return True  # Market open all day
            elif sunday_week_day == 5:  # Friday
                return hour < 17  # Before 5pm ET
            else:  # Saturday
                return False

        # ── FRIDAY CLOSE-OUT PROTECTION ──────────────────────────────
        # Prevents opening new trades near weekend and force-closes
        # any open positions before market close. Added after AUD_USD
        # weekend gap loss of -$201 on 2026-02-27.
        def _get_friday_status() -> dict:
            """Check Friday close-out rules.
            Returns dict with 'action': 'normal'|'no_new_trades'|'close_all'
            """
            now_utc = datetime.now(timezone.utc)
            import zoneinfo
            try:
                et_tz = zoneinfo.ZoneInfo("America/New_York")
                now_et = now_utc.astimezone(et_tz)
            except Exception:
                import time as _time
                is_dst = _time.daylight and _time.localtime().tm_isdst
                offset = -4 if is_dst else -5
                now_et = datetime.fromtimestamp(now_utc.timestamp() + offset * 3600)

            if now_et.weekday() != 4:  # Not Friday
                return {"action": "normal", "reason": "Not Friday"}

            current_minutes = now_et.hour * 60 + now_et.minute
            minutes_until_close = 17 * 60 - current_minutes

            # Friday 4:30 PM ET+ → CLOSE ALL open trades immediately
            if current_minutes >= 16 * 60 + 30:
                logger.critical(
                    "FRIDAY CLOSE-OUT: %02d:%02d ET — past 16:30, "
                    "closing ALL positions before weekend",
                    now_et.hour, now_et.minute,
                )
                # Force close all open trades via OANDA
                try:
                    close_result = _swarm_execute_tool(
                        "oanda_data", "close_all_positions"
                    )
                    logger.critical(
                        "FRIDAY CLOSE-OUT: close_all_positions result: %s",
                        close_result,
                    )
                except Exception as e:
                    logger.error("FRIDAY CLOSE-OUT: Failed to close positions: %s", e)
                    # Try individual close as fallback
                    try:
                        open_trades = _swarm_execute_tool(
                            "oanda_data", "get_open_trades"
                        )
                        trades = open_trades.get("trades", []) if isinstance(open_trades, dict) else []
                        for t in trades:
                            tid = t.get("id") or t.get("trade_id")
                            if tid:
                                _swarm_execute_tool(
                                    "oanda_data", "close_trade",
                                    trade_id=str(tid),
                                )
                                logger.critical("FRIDAY CLOSE-OUT: Closed trade %s", tid)
                    except Exception as e2:
                        logger.error("FRIDAY CLOSE-OUT: Fallback close failed: %s", e2)

                return {
                    "action": "close_all",
                    "reason": f"Friday {now_et.hour}:{now_et.minute:02d} ET — "
                              f"past 16:30, all positions closed before weekend",
                    "minutes_until_close": max(minutes_until_close, 0),
                }

            # Friday 4:00 PM ET+ → NO NEW TRADES (but existing ones stay)
            if current_minutes >= 16 * 60:
                logger.warning(
                    "FRIDAY NO-NEW-TRADES: %02d:%02d ET — within 60min of "
                    "market close, blocking new entries",
                    now_et.hour, now_et.minute,
                )
                return {
                    "action": "no_new_trades",
                    "reason": f"Friday {now_et.hour}:{now_et.minute:02d} ET — "
                              f"within 60min of close, no new trades",
                    "minutes_until_close": minutes_until_close,
                }

            # Friday 3:00 PM ET+ → WARNING (trades allowed but with caution)
            if current_minutes >= 15 * 60:
                logger.info(
                    "FRIDAY WARNING: %02d:%02d ET — %d min until market close",
                    now_et.hour, now_et.minute, minutes_until_close,
                )
                return {
                    "action": "warn",
                    "reason": f"Friday {now_et.hour}:{now_et.minute:02d} ET — "
                              f"{minutes_until_close}min until close",
                    "minutes_until_close": minutes_until_close,
                }

            return {"action": "normal", "reason": "Friday, market hours normal"}

        if not test_mode:
            friday_status = _get_friday_status()
            if friday_status["action"] == "close_all":
                return {
                    "cycle_number": cycle_num,
                    "instrument": instrument,
                    "timeframe": timeframe,
                    "cycle_start": cycle_start,
                    "status": "friday_closeout",
                    "message": friday_status["reason"],
                    "steps_completed": ["friday_closeout"],
                    "timing": {
                        "total": time.time() - cycle_clock,
                        "phases": {"friday_closeout": time.time() - cycle_clock},
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    "error": None,
                }
            elif friday_status["action"] == "no_new_trades":
                return {
                    "cycle_number": cycle_num,
                    "instrument": instrument,
                    "timeframe": timeframe,
                    "cycle_start": cycle_start,
                    "status": "friday_no_new_trades",
                    "message": friday_status["reason"],
                    "steps_completed": ["friday_no_new_trades"],
                    "timing": {
                        "total": time.time() - cycle_clock,
                        "phases": {"friday_no_new_trades": time.time() - cycle_clock},
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    "error": None,
                }
        # ── END FRIDAY CLOSE-OUT ──────────────────────────────────────

        if not is_forex_market_open() and not test_mode:
            logger.info("Forex market is closed - returning early (reopens Sun 5pm ET)")
            market_closed_result = {
                "cycle_number": cycle_num,
                "instrument": instrument,
                "timeframe": timeframe,
                "cycle_start": cycle_start,
                "status": "market_closed",
                "message": "Forex market closed (reopens Sun 5pm ET)",
                "steps_completed": ["market_hours_check"],
                "timing": {
                    "total": 0.01,
                    "phases": {"market_hours_check": 0.01},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                "error": None,
            }
            
            # Still write to dashboard JSON so it shows market status
            try:
                dashboard_path = Path(__file__).parent.parent.parent / "dashboard" / "cycle_data.json"
                dashboard_path.parent.mkdir(exist_ok=True)
                
                dashboard_data = dict(market_closed_result)
                dashboard_data.update({
                    "agent_list": [spec["name"] for spec in [
                        {"name": "oanda_data"}, {"name": "technical_analyst"}, {"name": "intelligence"},
                        {"name": "validator"},
                        {"name": "execution"}, {"name": "reporter"}, {"name": "cycle_orchestrator"}
                    ]],
                    "comment_protocol_messages": [],
                    "export_timestamp": datetime.now(timezone.utc).isoformat(),
                })
                
                tmp_path = dashboard_path.with_suffix('.json.tmp')
                with open(tmp_path, 'w') as f:
                    json.dump(dashboard_data, f, indent=2, default=str)
                tmp_path.rename(dashboard_path)
                
            except Exception as exc:
                logger.warning("Failed to export market closed data to dashboard: %s", exc)
            
            return market_closed_result

        cycle_result: Dict[str, Any] = {
            "cycle_number": cycle_num,
            "instrument": instrument,
            "timeframe": timeframe,
            "cycle_start": cycle_start,
            "status": "running",
            "steps_completed": [],
            "phases": [],       # Agent activity log for dashboard
            "decisions": [],    # Decision log for dashboard
            "error": None,
            "scout_context": scout_context,
        }
        self.live_cycle_result = cycle_result  # Expose for live dashboard progress
        
        # PROBLEM 1 FIX: Record scout snipe trigger if cycle started from scout
        if scout_context and scout_context.get('finding_id'):
            try:
                from scout_learning_system import record_snipe_trigger
                record_snipe_trigger(finding_id=scout_context.get('finding_id'))
                logger.info(f"Recorded snipe trigger for scout finding #{scout_context.get('finding_id')}")
            except Exception as e:
                logger.warning(f"Failed to record snipe trigger: {e}")

        _cycle_id = f"cycle_{cycle_num}_{cycle_start}"
        if flight:
            flight.record(FlightStage.CYCLE_START, pair=instrument, cycle_id=_cycle_id, data={
                "timeframe": timeframe, "scout_context": bool(scout_context),
                "test_mode": test_mode,
            }, note=f"Cycle #{cycle_num} for {instrument}")

        def _log_phase(agent: str, description: str, duration: float, status: str = "ok"):
            """Append a phase entry for the dashboard orchestrator comms panel."""
            cycle_result["phases"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": agent,
                "description": description,
                "duration": duration,
                "status": status,
            })

        # ── SNIPE DIRECT EXECUTION ─────────────────────────────────────────────
        # When a snipe trigger fires, skip the ENTIRE trading pipeline.
        # No data collection, no intelligence, no TA, no validator, no decision.
        # The snipe conditions ARE the signal. Go straight to execution.
        # Full pipeline: 136 seconds. This path: ~2 seconds.
        _is_re_entry = bool((scout_context or {}).get("_prev_snipe_filled"))
        if _is_re_entry:
            # Re-entry = prior snipe filled AND that trade is STILL OPEN.
            # Running the full pipeline just to HOLD wastes Haiku + Sonnet calls.
            # Block immediately — the trade is already open, nothing to enter.
            logger.info("⚡ [SNIPE] %s: re-entry blocked — prior fill trade is still open, no stacking",
                        instrument)
            cycle_result["status"] = "skipped"
            cycle_result["skip_reason"] = "snipe_reentry_trade_open"
            cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
            return cycle_result

        # Snipe triggered → direct execution. No full pipeline.
        if scout_context and scout_context.get("triggered_by") == "snipe" and not _is_re_entry:
            _snipe_start = time.time()
            _watch_id_fr = scout_context.get("watch_id") or scout_context.get("_watch_id", "")

            # ── Flight recorder: log snipe direct start ──────────────
            def _fr_snipe(stage, status="ok", **kw):
                """Helper to record snipe flight events."""
                if flight:
                    try:
                        flight.record(stage, pair=instrument, status=status,
                                      data={"watch_id": _watch_id_fr, **kw})
                    except Exception:
                        pass

            _fr_snipe("SNIPE_DIRECT_START",
                      direction=(scout_context.get("direction") or ""),
                      fan_direction=(scout_context.get("fan_direction") or ""),
                      fan_state=(scout_context.get("fan_state") or ""),
                      sniper_buy=scout_context.get("sniper_buy", 0),
                      sniper_sell=scout_context.get("sniper_sell", 0))

            # ── Kronos snipe detection — skip scout-thesis gates ──────────
            # Kronos snipes (source='kronos_hunter', suggestion_type='kronos_path_snipe')
            # use the Kronos forecast model for entry thesis, not scout's fan/EMA structure.
            # Scout-thesis gates (ema21_position wrong_side, fan_exhaustion, oscillator
            # freshness, refire cap) kill valid Kronos reversal/forecast setups because
            # they enforce scout's trend-continuation rules. Cooldown gate is KEPT for
            # Kronos to prevent same-pair revenge sniping.
            # Kill-switch tunable: gate.kronos_bypass_scout_gates (default True).
            _is_kronos_snipe = (scout_context or {}).get("suggestion_type") == "kronos_path_snipe"
            if not _is_kronos_snipe and _watch_id_fr:
                # M1-fast path strips suggestion_type from snipe_context; fall back to DB lookup.
                try:
                    _k_conn = get_trading_forex()
                    _k_row = _k_conn.execute(
                        "SELECT suggestion_type, source FROM watch_suggestions WHERE id = ?",
                        (_watch_id_fr,),
                    ).fetchone()
                    if _k_row:
                        _is_kronos_snipe = (
                            _k_row[0] == "kronos_path_snipe" or _k_row[1] == "kronos_hunter"
                        )
                except Exception:
                    pass
            _is_kronos_snipe = _is_kronos_snipe and bool(
                tc_get("gate.kronos_bypass_scout_gates", True)
            )
            if _is_kronos_snipe:
                _fr_snipe("SNIPE_KRONOS_DETECTED", bypass="scout_thesis_gates")

            # Normalize direction — watch context stores "bullish"/"bearish" or "BUY"/"SELL"
            _raw_dir = (scout_context.get("direction") or "").upper()
            _snipe_dir = "BUY" if _raw_dir in ("BUY", "BULLISH", "LONG", "BULL") else \
                         "SELL" if _raw_dir in ("SELL", "BEARISH", "SHORT", "BEAR") else ""
            _watch_id    = scout_context.get("watch_id") or scout_context.get("_watch_id")
            _snipe_score = scout_context.get("sniper_buy" if _snipe_dir == "BUY" else "sniper_sell", 0)
            _snipe_thresh = scout_context.get("sniper_threshold", 12)

            # If direction not set, infer from current sniper buy/sell scores
            if not _snipe_dir:
                _infer_buy  = scout_context.get("sniper_buy", 0) or 0
                _infer_sell = scout_context.get("sniper_sell", 0) or 0
                if _infer_buy > _infer_sell and _infer_buy > 0:
                    _snipe_dir = "BUY"
                    logger.info("⚡ [SNIPE DIRECT] %s: direction inferred BUY from sniper scores (buy=%s > sell=%s)",
                                instrument, _infer_buy, _infer_sell)
                elif _infer_sell > _infer_buy and _infer_sell > 0:
                    _snipe_dir = "SELL"
                    logger.info("⚡ [SNIPE DIRECT] %s: direction inferred SELL from sniper scores (sell=%s > buy=%s)",
                                instrument, _infer_sell, _infer_buy)
                else:
                    # Still no clear direction — try live sniper score now
                    try:
                        from Source.backtester.sniper_v4 import score_v4 as _sv4
                        from Source.backtester.ema_separation import generate_market_picture as _gmp
                        _mp = _gmp(instrument, "M15")
                        if _mp:
                            _live = _sv4(_mp)
                            _lb, _ls = _live.get("buy", 0), _live.get("sell", 0)
                            if _lb > _ls:
                                _snipe_dir = "BUY"
                                logger.info("⚡ [SNIPE DIRECT] %s: direction inferred BUY from live sniper (buy=%s > sell=%s)",
                                            instrument, _lb, _ls)
                            elif _ls > _lb:
                                _snipe_dir = "SELL"
                                logger.info("⚡ [SNIPE DIRECT] %s: direction inferred SELL from live sniper (sell=%s > buy=%s)",
                                            instrument, _ls, _lb)
                    except Exception as _infer_err:
                        logger.debug("Direction inference via live sniper failed: %s", _infer_err)

            # Block only if direction still undetermined after inference
            if not _snipe_dir:
                logger.info("⚡ [SNIPE DIRECT] %s BLOCKED: direction='%s' and scores tied/zero — no trade",
                            instrument, _raw_dir)
                _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="no_direction", raw_dir=_raw_dir)
                cycle_result["status"] = "skipped"
                cycle_result["skip_reason"] = "snipe_no_direction"
                cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                return cycle_result

            # ── Market direction alignment check ─────────────────────────────
            # Snipe direction must agree with the current EMA fan direction.
            # A BUY snipe into a bearish fan (or SELL into bullish) is a
            # counter-trend entry — block it here before any execution.
            # Use scout_context fan_direction (from watch_manager). If empty,
            # re-fetch live — never trade blind (trades 2657/2669 root cause).
            try:
                _fan_dir_align = (
                    scout_context.get("fan_direction") or
                    scout_context.get("live_direction") or
                    ""
                ).lower()

                # ── SAFETY NET: if fan data is missing, re-fetch from M15 ────
                # watch_manager should always provide this now, but if something
                # upstream fails, we compute it here rather than trading blind.
                _live_fan_state = (scout_context.get("fan_state") or "").lower()
                if not _fan_dir_align or _fan_dir_align in ("", "neutral", "mixed", "unknown"):
                    try:
                        from Source.backtester.ema_separation import generate_market_picture as _gmp_refetch
                    except ImportError:
                        from backtester.ema_separation import generate_market_picture as _gmp_refetch
                    try:
                        from Source.oanda_client import OandaClient as _OC_refetch
                    except ImportError:
                        from oanda_client import OandaClient as _OC_refetch
                    try:
                        from Source.broker_credentials import BrokerCredentials as _BC_refetch
                    except ImportError:
                        from broker_credentials import BrokerCredentials as _BC_refetch
                    try:
                        # 2026-05-01: cached fetch — reuses M15 candles fetched
                        # by validator earlier in this cycle (within 5 min TTL).
                        try:
                            from candle_cache import get_cached_candles as _gcc_rf
                        except ImportError:
                            from Source.candle_cache import get_cached_candles as _gcc_rf
                        _bc_r = _BC_refetch().get_connection(user_id=getattr(self, 'user_id', None), broker="oanda")
                        with _OC_refetch(_bc_r["api_key"], _bc_r["base_url"]) as _cl_r:
                            def _fetch_rf(_pair=instrument, _cl=_cl_r):
                                _raw = _cl.get_candles(_pair, granularity="M15", count=250)
                                return _raw if isinstance(_raw, list) else _raw.get("candles", [])
                            _clist = _gcc_rf(_fetch_rf, instrument, "M15", 250)
                        _m15n = []
                        for _cr in _clist:
                            _midr = _cr.get("mid", {})
                            _m15n.append({
                                "time": _cr.get("time", ""),
                                "open": _midr.get("o", _cr.get("open", 0)),
                                "high": _midr.get("h", _cr.get("high", 0)),
                                "low": _midr.get("l", _cr.get("low", 0)),
                                "close": _midr.get("c", _cr.get("close", 0)),
                            })
                        if len(_m15n) >= 100:
                            _refetched_mp = _gmp_refetch(instrument, _m15n)
                            if _refetched_mp:
                                _ema_r = _refetched_mp.get("ema", {})
                                _fan_dir_align = (_ema_r.get("fan_direction", "") or "").lower()
                                _live_fan_state = (_ema_r.get("fan_state", "") or "").lower()
                                logger.info(
                                    "⚡ [SNIPE DIRECT] %s: fan data was empty — re-fetched M15: fan=%s state=%s",
                                    instrument, _fan_dir_align, _live_fan_state
                                )
                                # Update scout_context so downstream (live_trades INSERT) gets correct values
                                if scout_context:
                                    scout_context["fan_direction"] = _fan_dir_align
                                    scout_context["fan_state"] = _live_fan_state
                    except Exception as _refetch_err:
                        logger.error(
                            "⚡ [SNIPE DIRECT] %s: fan re-fetch FAILED (%s) — BLOCKING snipe (no blind trades)",
                            instrument, _refetch_err
                        )
                        _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="fan_refetch_failed")
                        cycle_result["status"] = "skipped"
                        cycle_result["skip_reason"] = "fan_data_unavailable"
                        cycle_result["skip_detail"] = f"Fan data missing and re-fetch failed: {_refetch_err}"
                        cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                        return cycle_result
                # fan_direction from watch_manager is "BUY"/"SELL" — normalize to "bullish"/"bearish"
                if _fan_dir_align in ("buy", "bullish"):   _fan_dir_align = "bullish"
                elif _fan_dir_align in ("sell", "bearish"): _fan_dir_align = "bearish"

                # "just_crossed" + neutral/empty direction means the fan is at the
                # crossover point with no confirmed direction — treat as conflict.
                _just_crossed_neutral = (
                    _live_fan_state in ("just_crossed", "forming") and
                    _fan_dir_align not in ("bullish", "bearish")  # neutral / empty
                )

                _bullish_fan = _fan_dir_align == "bullish"
                _bearish_fan = _fan_dir_align == "bearish"
                _dir_conflict = (
                    (_snipe_dir == "BUY"  and _bearish_fan) or
                    (_snipe_dir == "SELL" and _bullish_fan) or
                    # New: block into just_crossed-neutral — fan has no confirmed direction.
                    # The watch was set in a prior trending regime; that regime is now gone.
                    _just_crossed_neutral
                )
                # 2026-04-23: kronos path snipes bypass this validator-style check.
                # Kronos's edge is predicting reversals — fan will always disagree at
                # trigger time by design. Kronos's own filters (hunter_gate: session/
                # drift/consensus/chop/counter_momentum/scout_bias) already ran at
                # snipe creation. Same philosophy as watch_manager sanity gate bypass.
                if _dir_conflict and _is_kronos_snipe:
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s kronos path snipe — BYPASSING direction_conflict "
                        "gate (snipe=%s vs fan=%s) — kronos predicts reversals",
                        instrument, _snipe_dir, _fan_dir_align
                    )
                    _fr_snipe("SNIPE_GATE_PASSED", gate="direction_conflict_kronos_bypass",
                              snipe_dir=_snipe_dir, fan_dir=_fan_dir_align,
                              fan_state=_live_fan_state)
                    _dir_conflict = False
                if _dir_conflict:
                    _conflict_reason = (
                        f"just_crossed+neutral fan (regime shift — original BUY thesis invalidated)"
                        if _just_crossed_neutral else
                        f"fan_direction={_fan_dir_align} opposes snipe dir={_snipe_dir}"
                    )
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s BLOCKED: %s",
                        instrument, _conflict_reason
                    )
                    _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="direction_conflict",
                              snipe_dir=_snipe_dir, fan_dir=_fan_dir_align, fan_state=_live_fan_state)
                    cycle_result["status"] = "skipped"
                    cycle_result["skip_reason"] = "snipe_direction_conflict"
                    cycle_result["skip_detail"] = _conflict_reason
                    cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                    return cycle_result
                else:
                    _fr_snipe("SNIPE_GATE_PASSED", gate="direction_aligned",
                              snipe_dir=_snipe_dir, fan_dir=_fan_dir_align, fan_state=_live_fan_state)
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s direction aligned: snipe=%s fan=%s fan_state=%s — proceeding",
                        instrument, _snipe_dir, _fan_dir_align, _live_fan_state
                    )

                # ── EMA ORDERING GATE ─────────────────────────────────────────
                # When fan_direction is neutral (EMAs interleaved, not fully ordered),
                # check raw EMA positions.  If E21 > E55 > E100 the market is bullish-
                # ordered — block SELL snipes.  Mirror for bearish.
                # This catches trade 3015: fan was "neutral" but E21>E55>E100
                # (bullish ordering) — sold into a bullish market.
                # NOTE: Only fires when EMAs are FULLY ordered against the snipe.
                # Interleaved EMAs (mid-cross) won't trigger — valid post-cross
                # snipes are not affected.
                _ema_vals = {}
                if '_refetched_mp' in dir() and _refetched_mp:
                    _ema_vals = (_refetched_mp.get("ema", {}) or {}).get("current_emas", {})
                if not _ema_vals and scout_context.get("market_picture"):
                    _mp_sc = scout_context["market_picture"]
                    if isinstance(_mp_sc, dict):
                        _ema_vals = (_mp_sc.get("ema", {}) or {}).get("current_emas", {})

                if _ema_vals and not _is_kronos_snipe:
                    # 2026-04-23: kronos path snipes bypass EMA-ordering conflict gate.
                    # Briefly re-enabled earlier this afternoon as a 9897 NZD_USD catch,
                    # but combined with 5-rule filter was too aggressive (only 3/79 trades
                    # survived in backtest). Reverted pending deep-dive loser analysis —
                    # see memory/project_kronos_filter_deploy_pending.md.
                    _e21 = float(_ema_vals.get("ema21", 0) or 0)
                    _e55 = float(_ema_vals.get("ema55", 0) or 0)
                    _e100 = float(_ema_vals.get("ema100", 0) or 0)
                    if _e21 and _e55 and _e100:
                        _emas_bullish = _e21 > _e55 > _e100
                        _emas_bearish = _e21 < _e55 < _e100
                        _ema_order_conflict = (
                            (_snipe_dir == "SELL" and _emas_bullish) or
                            (_snipe_dir == "BUY"  and _emas_bearish)
                        )
                        if _ema_order_conflict:
                            _ema_mkt = "bullish" if _emas_bullish else "bearish"
                            _ema_reason = (
                                f"EMA ordering conflict: E21={_e21:.5f} {'>' if _emas_bullish else '<'} "
                                f"E55={_e55:.5f} {'>' if _emas_bullish else '<'} E100={_e100:.5f} "
                                f"({_ema_mkt}) but snipe is {_snipe_dir}"
                            )
                            logger.info("⚡ [SNIPE DIRECT] %s BLOCKED: %s", instrument, _ema_reason)
                            _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked",
                                      gate="ema_ordering_conflict",
                                      snipe_dir=_snipe_dir, e21=_e21, e55=_e55, e100=_e100)
                            cycle_result["status"] = "skipped"
                            cycle_result["skip_reason"] = "ema_ordering_conflict"
                            cycle_result["skip_detail"] = _ema_reason
                            cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                            return cycle_result
                        else:
                            _fr_snipe("SNIPE_GATE_PASSED", gate="ema_ordering",
                                      snipe_dir=_snipe_dir, e21=round(_e21, 5),
                                      e55=round(_e55, 5), e100=round(_e100, 5))

            except Exception as _align_err:
                # Direction check failed entirely — BLOCK the snipe. No blind trades.
                # (Old behavior: "allow on infra error" — caused trades 2657/2669 losses)
                logger.error("⚡ [SNIPE DIRECT] %s: direction alignment check FAILED (%s) — BLOCKING snipe",
                             instrument, _align_err)
                _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="alignment_check_error",
                          error=str(_align_err))
                cycle_result["status"] = "skipped"
                cycle_result["skip_reason"] = "direction_check_failed"
                cycle_result["skip_detail"] = f"Alignment check exception: {_align_err}"
                cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                return cycle_result

            logger.info("⚡ [SNIPE DIRECT] %s dir=%s score=%s/%s watch_id=%s — skipping full pipeline",
                        instrument, _snipe_dir, _snipe_score, _snipe_thresh, _watch_id)
            cycle_result["steps_completed"].append("snipe_direct")

            # ── 4-rule narrow kronos filter at TRIGGER time (2026-04-23) ──
            # Path snipes drift between creation (valid setup) and trigger (setup
            # evaporated). Re-check the 4 narrow patterns from the loser deep-dive.
            # Only applies to kronos_path_snipe (validator snipes have their own
            # quality filters that already ran).
            # 2026-04-24: Conservative mode (default). On any data/exception
            # issue, BLOCK the snipe rather than silently allowing. Trade 9990
            # EUR_JPY (lost -15p at 21:18 UTC) triggered with stoch=71.1 — knife
            # territory — but the re-check produced no flight_log entry, so it
            # silently bypassed. Prefer to miss an edge than enter a bad setup.
            # Tunable: kronos.trigger_4rule_conservative (default True).
            _trig_conservative = bool(tc_get("kronos.trigger_4rule_conservative", True))
            if _is_kronos_snipe:
                try:
                    # 2026-04-27: Re-applied Friday fix lost in 35B agent
                    # collapse refactor. Two issues at this site:
                    # (1) fetch_candles is locally-imported below at ~line 2735,
                    #     making it a function-scope local. Without binding it
                    #     here first, line 2617 raises UnboundLocalError.
                    # (2) wrappers.fetch_candles returns a dict
                    #     {"candles": [...], "count": N, ...} — not a list.
                    #     Iterating it yields keys (strings), then c.get(...)
                    #     fails with "'str' object has no attribute 'get'".
                    # Other fetch_candles sites in this file (~3027, 3298, 3493)
                    # already use the same unwrap pattern.
                    from Source.agents.wrappers import fetch_candles
                    import numpy as _np4
                    _raw_cs4 = fetch_candles(instrument, "M15", 150)
                    _cs4 = _raw_cs4.get("candles", []) if isinstance(_raw_cs4, dict) else (_raw_cs4 or [])
                    if _cs4:
                        _cs4 = [c for c in _cs4 if c.get('complete', True)]
                    if not _cs4 or len(_cs4) < 25:
                        _nodata_reason = (
                            f"insufficient candles for 4-rule re-check "
                            f"(got {len(_cs4) if _cs4 else 0}, need ≥25)"
                        )
                        if _trig_conservative:
                            logger.warning("⚡ [SNIPE DIRECT] %s BLOCKED: %s",
                                           instrument, _nodata_reason)
                            _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked",
                                      gate="kronos_4rule_trigger_nodata",
                                      reason=_nodata_reason, watch_id=_watch_id)
                            cycle_result["status"] = "skipped"
                            cycle_result["skip_reason"] = "kronos_4rule_trigger_nodata"
                            cycle_result["skip_detail"] = _nodata_reason
                            cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                            return cycle_result
                        else:
                            logger.debug("[SNIPE DIRECT] %s: 4-rule re-check skipped (%s) — permissive mode",
                                         instrument, _nodata_reason)
                    elif _cs4 and len(_cs4) >= 25:
                        _closes4 = _np4.array([float(c['mid']['c']) for c in _cs4])
                        _opens4  = _np4.array([float(c['mid']['o']) for c in _cs4])
                        _highs4  = _np4.array([float(c['mid']['h']) for c in _cs4])
                        _lows4   = _np4.array([float(c['mid']['l']) for c in _cs4])
                        # Pip size + ATR14
                        _pip4 = 0.01 if "JPY" in instrument else 0.0001
                        _trs = [_np4.maximum(_highs4[i]-_lows4[i],
                                             _np4.maximum(abs(_highs4[i]-_closes4[i-1]),
                                                          abs(_lows4[i]-_closes4[i-1])))
                                for i in range(1, len(_cs4))]
                        _atr_pips4 = (float(_np4.mean(_trs[-14:])) / _pip4) if len(_trs) >= 14 else 0
                        # EMA21
                        _alpha4 = 2.0/22
                        _e21_4 = _closes4[0]
                        for _x in _closes4[1:]:
                            _e21_4 = _alpha4*_x + (1-_alpha4)*_e21_4
                        # Stoch %K(14)
                        _stk4 = None
                        if len(_cs4) >= 14:
                            _h14 = _np4.max(_highs4[-14:]); _l14 = _np4.min(_lows4[-14:])
                            _rng14 = _h14 - _l14
                            _stk4 = 100.0 * (_closes4[-1] - _l14) / _rng14 if _rng14 > 0 else 50.0
                        # Last candle color + body %
                        _last_o4 = float(_opens4[-1]); _last_c4 = float(_closes4[-1])
                        _color4 = "GREEN" if _last_c4 > _last_o4 else ("RED" if _last_c4 < _last_o4 else "DOJI")
                        _body4 = abs(_last_c4 - _last_o4)
                        _rng4 = float(_highs4[-1]) - float(_lows4[-1])
                        _body_pct4 = (_body4 / _rng4) if _rng4 > 0 else 0.0
                        # Pos vs E21 in ATR
                        _pos_pips4 = (_closes4[-1] - _e21_4) / _pip4
                        _pos_atr4 = (_pos_pips4 / _atr_pips4) if _atr_pips4 > 0 else 0.0
                        _d4 = _snipe_dir.lower()
                        # Thresholds — all tunable (2026-04-24). Keep in sync with
                        # kronos_hunter.py Gate 1.4 (re-check at trigger time for path snipes).
                        _knife_buy_max = float(tc_get("kronos.hunter_knife_buy_stoch_max", 70.0))
                        _knife_sell_min = float(tc_get("kronos.hunter_knife_sell_stoch_min", 30.0))
                        _fight_body_min = float(tc_get("kronos.hunter_candle_fighting_body_pct_min", 0.30))
                        _extended_atr = float(tc_get("kronos.hunter_ultra_extended_atr_mult", 2.0))
                        _ambiguous_body_max = float(tc_get("kronos.hunter_ambiguous_body_pct_max", 0.10))
                        # ── Apply rules ──
                        _reason4 = None
                        if _stk4 is not None and _stk4 > 0:
                            if _d4 == "buy" and _stk4 > _knife_buy_max:
                                _reason4 = f"kronos_knife_buy_overbought stoch={_stk4:.1f}"
                            elif _d4 == "sell" and _stk4 < _knife_sell_min:
                                _reason4 = f"kronos_knife_sell_oversold stoch={_stk4:.1f}"
                        if not _reason4 and _body_pct4 > _fight_body_min:
                            if _d4 == "buy" and _color4 == "RED":
                                _reason4 = f"kronos_candle_fighting BUY+RED body={_body_pct4*100:.0f}%"
                            elif _d4 == "sell" and _color4 == "GREEN":
                                _reason4 = f"kronos_candle_fighting SELL+GREEN body={_body_pct4*100:.0f}%"
                        if not _reason4:
                            if _d4 == "buy" and _pos_atr4 > _extended_atr:
                                _reason4 = f"kronos_ultra_extended BUY pos={_pos_atr4:+.2f}×ATR"
                            elif _d4 == "sell" and _pos_atr4 < -_extended_atr:
                                _reason4 = f"kronos_ultra_extended SELL pos={_pos_atr4:+.2f}×ATR"
                        if not _reason4 and _body_pct4 < _ambiguous_body_max:
                            _reason4 = f"kronos_ambiguous_candle body={_body_pct4*100:.0f}%<{_ambiguous_body_max*100:.0f}%"

                        if _reason4:
                            logger.info("⚡ [SNIPE DIRECT] %s BLOCKED: %s", instrument, _reason4)
                            _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked",
                                      gate="kronos_4rule_trigger",
                                      reason=_reason4, watch_id=_watch_id,
                                      stoch=round(_stk4, 1) if _stk4 else None,
                                      candle_color=_color4, body_pct=round(_body_pct4, 3),
                                      pos_e21_atr=round(_pos_atr4, 2))
                            cycle_result["status"] = "skipped"
                            cycle_result["skip_reason"] = "kronos_4rule_trigger"
                            cycle_result["skip_detail"] = _reason4
                            cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                            return cycle_result
                        else:
                            _fr_snipe("SNIPE_GATE_PASSED", gate="kronos_4rule_trigger",
                                      stoch=round(_stk4, 1) if _stk4 else None,
                                      candle_color=_color4, body_pct=round(_body_pct4, 3),
                                      pos_e21_atr=round(_pos_atr4, 2))
                except Exception as _k4_err:
                    if _trig_conservative:
                        logger.warning("⚡ [SNIPE DIRECT] %s BLOCKED: 4-rule re-check exception — %s",
                                       instrument, _k4_err)
                        _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked",
                                  gate="kronos_4rule_trigger_error",
                                  reason=f"exception: {_k4_err}",
                                  watch_id=_watch_id)
                        cycle_result["status"] = "skipped"
                        cycle_result["skip_reason"] = "kronos_4rule_trigger_error"
                        cycle_result["skip_detail"] = f"4-rule re-check raised: {_k4_err}"
                        cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                        return cycle_result
                    else:
                        logger.debug("[SNIPE DIRECT] kronos 4-rule filter check failed: %s — allowing (permissive mode)", _k4_err)

            try:
                from Source.agents.wrappers import (
                    fetch_candles, get_account_summary, place_market_order
                )
                import numpy as np

                # ── Sniper score re-validation (re-landed 2026-04-22 from Mar-31 worktree) ──
                # The watch was valid at creation, but market conditions may have shifted
                # by trigger time. Re-fetch the live M15 market picture and score with
                # score_v4. If current score in the intended direction has dropped below
                # the threshold (typically 12), block the fire. Watch stays active and
                # can re-fire next cycle if conditions recover.
                # Kronos-path snipes skip this — they use forecast-path thesis, not
                # scout sniper scores.
                _is_kronos_snipe_rv = (
                    (scout_context or {}).get("suggestion_type") == "kronos_path_snipe"
                )
                if (
                    not _is_kronos_snipe_rv
                    and tc_get("gate.sniper_revalidation", True)
                ):
                    try:
                        from Source.backtester.sniper_v4 import score_v4 as _sv4_rv
                        from Source.backtester.ema_separation import generate_market_picture as _gmp_rv
                        _rv_mp = _gmp_rv(instrument, "M15")
                        if _rv_mp:
                            _rv_live = _sv4_rv(_rv_mp)
                            _rv_current_score = _rv_live.get(
                                "buy" if _snipe_dir == "BUY" else "sell", 0
                            )
                            if _rv_current_score < _snipe_thresh:
                                logger.info(
                                    "⚡ [SNIPE DIRECT] %s BLOCKED: sniper revalidation — "
                                    "current %s score=%s below threshold=%s (original=%s at watch creation)",
                                    instrument, _snipe_dir, _rv_current_score, _snipe_thresh, _snipe_score
                                )
                                _fr_snipe(
                                    "SNIPE_GATE_BLOCKED",
                                    status="blocked",
                                    gate="sniper_revalidation",
                                    skip_reason="sniper_score_stale",
                                    direction=_snipe_dir,
                                    original_sniper_score=_snipe_score,
                                    current_sniper_score=_rv_current_score,
                                    threshold=_snipe_thresh,
                                )
                                cycle_result["status"] = "skipped"
                                cycle_result["skip_reason"] = "sniper_score_stale"
                                cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                                return cycle_result
                            else:
                                _fr_snipe(
                                    "SNIPE_GATE_PASSED",
                                    gate="sniper_revalidation",
                                    direction=_snipe_dir,
                                    current_sniper_score=_rv_current_score,
                                    threshold=_snipe_thresh,
                                )
                    except Exception as _rv_err:
                        logger.warning(
                            "⚡ [SNIPE DIRECT] %s: sniper revalidation failed (%s) — proceeding without recheck",
                            instrument, _rv_err
                        )

                # ── 0. Open-trade visibility (NO BLOCK — 2026-05-14, Tim approved) ──
                # Previously: blocked snipes when any non-kronos trade was open on the
                # same pair. Removed 2026-05-14 — Tim's call. Reason: snipe + scout +
                # validator should be able to coexist on the same pair. The other gates
                # (validator_fan_alignment, fan_exhaustion, conditional_exhaustion,
                # tight_fan, refire_gap_exceeded) already provide the right filtering.
                # Kept the OANDA query so we still record "n trades open when snipe fired"
                # in flight log for audit, but the cycle is allowed through regardless.
                try:
                    import requests as _req
                    from broker_credentials import BrokerCredentials as _BC
                    _bc_conn = _BC().get_connection(user_id=self.user_id, broker="oanda")
                    _oanda_key = _bc_conn.get("api_key", "")
                    _base_url  = _bc_conn.get("base_url", "https://api-fxpractice.oanda.com")
                    _acct_id   = _bc_conn.get("account_id", "")
                    _ot = _req.get(
                        f"{_base_url}/v3/accounts/{_acct_id}/openTrades",
                        headers={"Authorization": f"Bearer {_oanda_key}"}, timeout=5
                    ).json().get("trades", [])
                    _existing = [t for t in _ot if t.get("instrument") == instrument]
                    if _existing:
                        logger.info("⚡ [SNIPE DIRECT] %s: %d concurrent open trade(s) — allowed (open_trade_guard removed)",
                                    instrument, len(_existing))
                    _fr_snipe("SNIPE_GATE_PASSED", gate="open_trade_guard",
                              note=f"{len(_existing)} concurrent open trade(s) — block removed")
                except Exception as _oe:
                    logger.debug("⚡ [SNIPE DIRECT] open-trade check failed: %s — proceeding", _oe)
                    _fr_snipe("SNIPE_GATE_PASSED", gate="open_trade_guard", note="check failed, proceeding")

                # ── SESSION GATE (2026-04-06) ─────────────────
                # Backtest of 71 trades: medium gate blocks 4 losses ($509 saved)
                # and 3 small wins ($60 lost) = net +$449.
                # 2026-05-11: rule logic moved to _compute_session_gate (module-
                # level) so the validator section build can also call it and
                # surface BLOCKED state to scout-driven cycles. Without that,
                # scout cycles never traversed this snipe-path block and the
                # validator always saw "Session gate: OPEN" — defeating iter
                # 20d's BLOCKED → WATCH downgrade rule.
                from datetime import timezone as _sg_tz
                _sg_now = datetime.now(_sg_tz.utc)
                _sg_is_sunday = (_sg_now.weekday() == 6)
                _sg_hour = _sg_now.hour
                _session_blocked, _session_reason = _compute_session_gate(
                    instrument, tc_get_fn=tc_get, now_utc=_sg_now,
                )

                if _session_blocked:
                    # Snipes at 100% (triggered_by=snipe) bypass session gate for
                    # EUR/GBP-Asian and Friday-close rules — validator confirmed,
                    # all conditions met. BUT snipes do NOT bypass the Sunday-open
                    # blackout (21-23 UTC) — market needs 2h to reset before snipes
                    # can fire (2026-04-20 audit: Sunday-open snipes ran into chop
                    # before liquidity normalized).
                    _is_snipe = (scout_context or {}).get("triggered_by") in ("snipe", "cascade_reentry")
                    _sunday_blackout = _sg_is_sunday and _sg_hour in (21, 22, 23)
                    _eur_cross_tail_active = (
                        instrument in ('EUR_AUD', 'EUR_CHF', 'EUR_JPY', 'EUR_CAD', 'EUR_NZD')
                        and (_sg_hour in (3, 4, 5) or (_sg_hour == 6 and _sg_now.minute < 30))
                    )
                    try:
                        _snipe_respects_sunday = bool(tc_get("gate.snipe_respects_sunday_blackout", True))
                    except Exception:
                        _snipe_respects_sunday = True
                    try:
                        _snipe_respects_eur_tail = bool(tc_get("gate.snipe_respects_eur_cross_tail", True))
                    except Exception:
                        _snipe_respects_eur_tail = True
                    _snipe_exempt = _is_snipe and not (
                        (_sunday_blackout and _snipe_respects_sunday)
                        or (_eur_cross_tail_active and _snipe_respects_eur_tail)
                    )
                    if _snipe_exempt:
                        logger.info("⚡ [SNIPE DIRECT] %s: session gate would block (%s) but SNIPE EXEMPT — proceeding",
                                    instrument, _session_reason)
                        _fr_snipe("SNIPE_GATE_PASSED", gate="session_gate",
                                  note=f"snipe exempt: {_session_reason}")
                    else:
                        _block_note = _session_reason
                        if _sunday_blackout and _is_snipe:
                            _block_note = f"Sunday blackout — snipes respect 2h reset window ({_session_reason})"
                        elif _eur_cross_tail_active and _is_snipe:
                            _block_note = f"EUR-cross Asian tail — snipes respect 11PM-2:30AM ET window ({_session_reason})"
                        logger.info("⚡ [SNIPE DIRECT] %s BLOCKED by session gate: %s", instrument, _block_note)
                        _fr_snipe("SNIPE_GATE_BLOCKED", gate="session_gate", reason=_block_note)
                        cycle_result["status"] = "skipped"
                        cycle_result["skip_reason"] = "session_gate"
                        cycle_result["skip_detail"] = _block_note
                        return cycle_result
                else:
                    _fr_snipe("SNIPE_GATE_PASSED", gate="session_gate")

                # ── Per-pair cooldown after loss (2h) ─────────
                # S16 churned EUR_USD 9 times (19-39min gaps, 4W/5L = -26.8p).
                # After a loss on a pair: 2h cooldown before sniping same pair again.
                # 2026-03-26 deep audit finding.
                # 2026-04-01: Per-pair daily limit (max 3/day) REMOVED — upstream
                # scout/validator criteria handle quality filtering now.
                # The 2h post-loss cooldown is kept to prevent revenge-trading same pair.
                _cooldown_hours = tc_get("gate.cooldown_hours", 0.5)   # 30 min (was 2h — too aggressive, blocked valid re-entries)
                try:
                    _cd_conn = get_trading_forex()
                    # Check for recent loss on this pair
                    _last_loss = _cd_conn.execute("""
                        SELECT exit_time FROM live_trades
                        WHERE pair = ? AND result = 'loss'
                        AND date(entry_time) = date('now')
                        ORDER BY exit_time DESC LIMIT 1
                    """, (instrument,)).fetchone()
                    if _last_loss and _last_loss[0]:
                        from datetime import timezone as _cd_tz
                        # FIX: truncate nanoseconds before fromisoformat (OANDA sends 9 decimals, Python supports 6)
                        _raw_ts = str(_last_loss[0]).replace('Z', '+00:00')
                        if '.' in _raw_ts:
                            _int_part, _frac_rest = _raw_ts.split('.', 1)
                            _offset = ''
                            for _sep in ('+', '-'):
                                if _sep in _frac_rest[1:]:
                                    _idx = _frac_rest.index(_sep, 1)
                                    _offset = _frac_rest[_idx:]
                                    _frac_rest = _frac_rest[:_idx]
                                    break
                            _frac_rest = _frac_rest[:6].ljust(6, '0')
                            _raw_ts = f"{_int_part}.{_frac_rest}{_offset}"
                        _loss_time = datetime.fromisoformat(_raw_ts.split('+')[0].split('Z')[0])
                        _hours_since = (datetime.now(timezone.utc).replace(tzinfo=None) - _loss_time).total_seconds() / 3600
                        if _hours_since < _cooldown_hours:
                            logger.info(
                                "⚡ [SNIPE DIRECT] %s BLOCKED: loss %.1fh ago (cooldown=%.1fh) — pair cooling off",
                                instrument, _hours_since, _cooldown_hours
                            )
                            _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="pair_cooldown",
                                      hours_since_loss=round(_hours_since, 1), cooldown_hours=_cooldown_hours)
                            cycle_result["status"] = "skipped"
                            cycle_result["skip_reason"] = "snipe_pair_cooldown"
                            cycle_result["skip_detail"] = f"Loss {_hours_since:.1f}h ago, cooldown requires {_cooldown_hours:.1f}h"
                            cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                            return cycle_result
                    _fr_snipe("SNIPE_GATE_PASSED", gate="cooldown_check",
                              note="2h post-loss cooldown passed")
                except Exception as _cd_err:
                    logger.warning("⚡ [SNIPE DIRECT] cooldown check failed: %s — proceeding", _cd_err)
                    _fr_snipe("SNIPE_GATE_PASSED", gate="cooldown_check", note="check failed, proceeding")

                # ── 1. News check — query news_events DB directly ─────────
                _news_clear = True
                try:
                    import sqlite3 as _ndb
                    _ncur = _ndb.connect(
                        _TRADING_FOREX_DB, timeout=3
                    ).execute(
                        """SELECT COUNT(*) FROM news_events
                           WHERE impact_level IN ('high','HIGH','red','RED')
                           AND is_upcoming = 1
                           AND (currencies_affected LIKE ? OR pairs_affected LIKE ?)
                           AND datetime(event_time) BETWEEN datetime('now','-15 minutes')
                                                        AND datetime('now','+60 minutes')""",
                        (f"%{instrument[:3]}%", f"%{instrument}%")
                    )
                    _news_count = _ncur.fetchone()[0]
                    _news_clear = (_news_count == 0)
                    if not _news_clear:
                        logger.info("⚡ [SNIPE DIRECT] %s: %d high-impact news in next 60m — BLOCKED",
                                    instrument, _news_count)
                except Exception:
                    pass  # DB unavailable — assume clear, don't block a good setup
                if not _news_clear:
                    logger.info("⚡ [SNIPE DIRECT] %s BLOCKED: high-impact news imminent", instrument)
                    _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="news_check",
                              news_count=_news_count if '_news_count' in dir() else 0)
                    cycle_result["status"] = "skipped"
                    cycle_result["skip_reason"] = "snipe_news_block"
                    cycle_result["skip_detail"] = "High-impact news imminent"
                    return cycle_result
                _fr_snipe("SNIPE_GATE_PASSED", gate="news_check")

                # ── 2. Fetch M15 candles (price, ATR, momentum check) ────
                # 2026-04-16: bumped 50→150 so E100 EMA gate has enough data.
                # 50 candles meant len(_closes)<100 and e100_late_entry silently skipped.
                _raw = fetch_candles(instrument, timeframe="M15", count=150)
                _candles = _raw.get("candles", []) if isinstance(_raw, dict) else []
                if len(_candles) < 20:
                    # Retry once — first call after server start can fail while OANDA client inits
                    import time as _tslp
                    logger.warning("⚡ [SNIPE DIRECT] %s: only %d candles on first try (raw keys=%s) — retrying in 2s",
                                   instrument, len(_candles), list(_raw.keys()) if isinstance(_raw, dict) else type(_raw).__name__)
                    _tslp.sleep(2)
                    _raw = fetch_candles(instrument, timeframe="M15", count=50)
                    _candles = _raw.get("candles", []) if isinstance(_raw, dict) else []
                if len(_candles) < 20:
                    # Still failing — skip, don't error, watch stays eligible to re-fire
                    logger.error("⚡ [SNIPE DIRECT] %s: candle fetch failed (%d candles) — skipping this tick",
                                 instrument, len(_candles))
                    _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="candle_fetch",
                              candle_count=len(_candles), required=20)
                    cycle_result["status"] = "skipped"
                    cycle_result["skip_reason"] = "candle_fetch_failed"
                    cycle_result["skip_detail"] = f"Only {len(_candles)} candles fetched (need 20)"
                    cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                    return cycle_result
                _fr_snipe("SNIPE_GATE_PASSED", gate="candle_fetch", candle_count=len(_candles))

                _closes = np.array([float(c["mid"]["c"]) for c in _candles if c.get("complete", True)])
                _highs  = np.array([float(c["mid"]["h"]) for c in _candles if c.get("complete", True)])
                _lows   = np.array([float(c["mid"]["l"]) for c in _candles if c.get("complete", True)])
                _current_price = float(_candles[-1]["mid"]["c"])

                # ── Validator-snipe fan-alignment gate (2026-04-28) ────────
                # Block when fan_sep at/past peak AND entry candle reverses color.
                # Skip kronos (kronos is forecast-reversal by design — bypasses).
                # Backtest 60d: net +$2,932, WR 56.1% → 72.1%, 11×favorable ratio.
                # Default OFF; flip via gate.validator_fan_alignment_enabled.
                if (not _is_kronos_snipe
                        and bool(tc_get("gate.validator_fan_alignment_enabled", False))
                        and len(_closes) >= 60):
                    _opens_arr = np.array([float(c["mid"]["o"]) for c in _candles if c.get("complete", True)])
                    _LB = int(tc_get("gate.validator_fan_alignment_lookback", 12))
                    _RISE_N = int(tc_get("gate.validator_fan_alignment_rise_n", 3))
                    _REV_K = int(tc_get("gate.validator_fan_alignment_reversal_k", 6))

                    # EMA21 / EMA55 over the candle history
                    def _ema(values, period):
                        out = np.full_like(values, np.nan, dtype=float)
                        if len(values) < period:
                            return out
                        k = 2.0 / (period + 1)
                        out[period - 1] = values[:period].mean()
                        for _i in range(period, len(values)):
                            out[_i] = values[_i] * k + out[_i - 1] * (1 - k)
                        return out

                    _e21 = _ema(_closes, 21)
                    _e55 = _ema(_closes, 55)
                    _ps = 0.01 if "JPY" in instrument else 0.0001
                    if not (np.isnan(_e21[-1]) or np.isnan(_e55[-1])):
                        _fan_signed = (_e21 - _e55) / _ps
                        _fan_sep = np.abs(_fan_signed)

                        if len(_fan_sep) >= _LB and not np.any(np.isnan(_fan_sep[-_LB:])):
                            _window = _fan_sep[-_LB:]
                            _peak_idx = int(np.argmax(_window))
                            _peak_val = float(_window[_peak_idx])
                            _cur_val = float(_window[-1])
                            _bars_since_peak = (_LB - 1) - _peak_idx

                            _rising = (len(_window) > _RISE_N
                                       and _window[-1] > _window[-1 - _RISE_N])
                            _at_peak_now = (_bars_since_peak == 0) and _rising
                            _post_peak = (1 <= _bars_since_peak <= _LB - 2) and (_cur_val < _peak_val)

                            _reversed = False
                            if len(_fan_signed) >= _REV_K + 1:
                                _signs = np.sign(_fan_signed[-_REV_K - 1:])
                                _reversed = bool(np.any(_signs[:-1] * _signs[-1] < 0))

                            _structural = _at_peak_now or _post_peak or _reversed

                            _entry_green = _closes[-1] > _opens_arr[-1]
                            _entry_red = _closes[-1] < _opens_arr[-1]
                            _candle_warns = ((_snipe_dir == "SELL" and _entry_green)
                                             or (_snipe_dir == "BUY" and _entry_red))

                            if _structural and _candle_warns:
                                _reasons = []
                                if _at_peak_now: _reasons.append("at_peak")
                                if _post_peak: _reasons.append(f"post_peak({_bars_since_peak}b,{round(_peak_val - _cur_val, 1)}p)")
                                if _reversed: _reasons.append("fan_reversed")
                                _why = "+".join(_reasons)
                                logger.warning(
                                    "🚫 [SNIPE GATE] %s %s: validator_fan_alignment block — %s + candle warns "
                                    "(fan_sep peak=%.1f→now=%.1f bars_since_peak=%d)",
                                    instrument, _snipe_dir, _why, _peak_val, _cur_val, _bars_since_peak,
                                )
                                _fr_snipe(
                                    "SNIPE_GATE_BLOCKED", status="blocked",
                                    gate="validator_fan_alignment",
                                    reason=_why,
                                    fan_sep_peak=round(_peak_val, 1),
                                    fan_sep_now=round(_cur_val, 1),
                                    bars_since_peak=_bars_since_peak,
                                    candle_warns=True,
                                )
                                cycle_result["status"] = "skipped"
                                cycle_result["skip_reason"] = "validator_fan_alignment"
                                cycle_result["skip_detail"] = (
                                    f"Fan {_why} + entry candle reversed direction. "
                                    f"peak={_peak_val:.1f}p now={_cur_val:.1f}p"
                                )
                                cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                                return cycle_result
                            else:
                                _fr_snipe(
                                    "SNIPE_GATE_PASSED", gate="validator_fan_alignment",
                                    structural=_structural, candle_warns=_candle_warns,
                                    fan_sep_peak=round(_peak_val, 1),
                                    fan_sep_now=round(_cur_val, 1),
                                )

                # ATR (14-period)
                _tr = np.maximum(
                    _highs[1:] - _lows[1:],
                    np.maximum(abs(_highs[1:] - _closes[:-1]), abs(_lows[1:] - _closes[:-1]))
                )
                _atr = float(np.mean(_tr[-14:])) if len(_tr) >= 14 else float(np.mean(_tr))

                # Momentum trap: both RSI AND Stoch at extreme = buying exhaustion
                _delta = np.diff(_closes)
                _gain  = np.where(_delta > 0, _delta, 0)
                _loss  = np.where(_delta < 0, -_delta, 0)
                # Wilder RSI (exponential smoothing) — standard method matching TradingView/dashboard
                # Fix 2026-04-07: was using simple average which produced values 5-10 points more extreme
                _period = 14
                _avg_gain = float(np.mean(_gain[:_period])) if len(_gain) >= _period else float(np.mean(_gain))
                _avg_loss = float(np.mean(_loss[:_period])) if len(_loss) >= _period else float(np.mean(_loss))
                for _i in range(_period, len(_gain)):
                    _avg_gain = (_avg_gain * (_period - 1) + float(_gain[_i])) / _period
                    _avg_loss = (_avg_loss * (_period - 1) + float(_loss[_i])) / _period
                _rs    = (_avg_gain / _avg_loss) if _avg_loss > 0 else 100
                _rsi   = 100 - (100 / (1 + _rs))
                _low14 = np.min(_lows[-14:]); _high14 = np.max(_highs[-14:])
                _stoch_raw = ((_current_price - _low14) / (_high14 - _low14) * 100) if _high14 > _low14 else 50
                _stoch = max(0.0, min(100.0, _stoch_raw))  # clamp 0-100 — new lows produce negative without this
                # Also compute stochastic on prior bar to detect direction of stoch movement
                _low14_prev = np.min(_lows[-15:-1]); _high14_prev = np.max(_highs[-15:-1])
                _prev_price = float(_closes[-2]) if len(_closes) >= 2 else _current_price
                _stoch_prev_raw = ((_prev_price - _low14_prev) / (_high14_prev - _low14_prev) * 100) \
                              if _high14_prev > _low14_prev else 50
                _stoch_prev = max(0.0, min(100.0, _stoch_prev_raw))  # clamp 0-100

                # ── EXISTING: momentum trap (extreme exhaustion) ─────────────
                # 2026-04-09: DISABLED — gates proven to block more winners than losers.
                # V1 optimizer (Apr 6) + V2 Optuna (Apr 8) both confirmed gates hurt revenue.
                # 300 trials with gates ON scored 0.0 (blocked to <20 trades). Gates stay OFF.
                # Vault: "Gates remain disabled at minimum values" — see collective/patterns/2026-04-08.md
                # Keeping computation + logging for monitoring only (no return/block).
                _trap  = (_snipe_dir == "BUY"  and _rsi > 78 and _stoch > 90) or \
                         (_snipe_dir == "SELL" and _rsi < 22 and _stoch < 10)
                if _trap:
                    logger.info("⚡ [SNIPE DIRECT] %s WOULD_BLOCK (disabled): momentum trap RSI=%.0f Stoch=%.0f",
                                instrument, _rsi, _stoch)
                    _fr_snipe("SNIPE_GATE_LOGGED", status="would_block", gate="momentum_trap",
                              rsi=round(_rsi, 1), stoch=round(_stoch, 1))
                _fr_snipe("SNIPE_GATE_PASSED", gate="momentum_trap",
                          rsi=round(_rsi, 1), stoch=round(_stoch, 1))

                # ── NEW (2026-03-29): Hard oscillator exhaustion gates ────────
                # Trade #2583 lost -12.5p selling into RSI 23 / Stoch 5.6 (fully exhausted).
                # These gates reject entries when oscillators show the move is DONE.
                # 2026-04-07: Removed stoch from this gate. Stoch stays pinned at
                # extremes during strong trends (EUR_AUD watch #1816 blocked 10+ times
                # at stoch=0-13 while RSI=42 and fan expanding — valid trend continuation).
                # RSI is the better exhaustion signal: it ranges wider and doesn't pin.
                # SELL rejected if RSI < 30 (oversold)
                # BUY  rejected if RSI > 70 (overbought)
                # 2026-04-09: DISABLED — see momentum_trap comment above. Gates OFF.
                _hard_osc_block = (
                    (_snipe_dir == "SELL" and _rsi < 30) or
                    (_snipe_dir == "BUY"  and _rsi > 70)
                )
                if _hard_osc_block:
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s WOULD_BLOCK (disabled): hard oscillator gate "
                        "— %s with RSI=%.1f Stoch=%.1f (move exhausted)",
                        instrument, _snipe_dir, _rsi, _stoch
                    )
                    _fr_snipe("SNIPE_GATE_LOGGED", status="would_block", gate="hard_oscillator_exhaustion",
                              rsi=round(_rsi, 1), stoch=round(_stoch, 1), direction=_snipe_dir)
                _fr_snipe("SNIPE_GATE_PASSED", gate="hard_oscillator_exhaustion",
                          rsi=round(_rsi, 1), stoch=round(_stoch, 1))

                # ── NEW (2026-03-30): Selling-into-strength / buying-into-weakness gate ──
                # Trade #2669 lost -4.4p: SELL with stoch_k=70.1 (near overbought).
                # Oscillators screaming price is going UP and we sold into it.
                # Existing gates only catch selling into oversold (exhaustion).
                # This gate catches the OPPOSITE: entering against live momentum.
                # SELL blocked if stoch > 65 (price has upward momentum)
                # BUY  blocked if stoch < 35 (price has downward momentum)
                # 2026-04-09: DISABLED — see momentum_trap comment above. Gates OFF.
                _against_momentum = (
                    (_snipe_dir == "SELL" and _stoch > tc_get("gate.stoch_dont_buy_above", 65)) or
                    (_snipe_dir == "BUY"  and _stoch < tc_get("gate.stoch_dont_sell_below", 35))
                )
                if _against_momentum:
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s WOULD_BLOCK (disabled): against-momentum gate "
                        "— %s with Stoch=%.1f (trading against live momentum)",
                        instrument, _snipe_dir, _stoch
                    )
                    _fr_snipe("SNIPE_GATE_LOGGED", status="would_block", gate="against_momentum",
                              stoch=round(_stoch, 1), direction=_snipe_dir)
                _fr_snipe("SNIPE_GATE_PASSED", gate="against_momentum",
                          stoch=round(_stoch, 1))

                # ── oscillator_freshness gate — 4 reversal-window patterns ────
                # Stale (original): stoch already retreated from extreme, reversal window passed.
                # Bounce trap (2026-04-14): stoch jumped from oversold/overbought = bounce started,
                # entering against the bounce = catching a falling/rising knife.
                # Validated on 185 trades (Mar 15 – Apr 14): catches 2 losses (5230 -35.3p,
                # pre-Apr-9 loss +6.7p) with ZERO false positives. Precision 100%.
                _bounce_jump_min = tc_get("gate.bounce_trap_jump_min", 20.0)
                _bounce_prev_max = tc_get("gate.bounce_trap_prev_max", 15.0)
                _top_trap_prev_min = tc_get("gate.bounce_trap_top_prev_min", 85.0)
                _stoch_jump = _stoch - _stoch_prev

                _osc_stale_drop = (
                    (_snipe_dir == "SELL" and _stoch_prev > 70 and _stoch < 50) or
                    (_snipe_dir == "BUY"  and _stoch_prev < 30 and _stoch > 50)
                )
                _osc_bounce_trap = (
                    (_snipe_dir == "SELL" and _stoch_prev < _bounce_prev_max and _stoch_jump > _bounce_jump_min) or
                    (_snipe_dir == "BUY"  and _stoch_prev > _top_trap_prev_min and (-_stoch_jump) > _bounce_jump_min)
                )
                _osc_freshness_fires = _osc_stale_drop or _osc_bounce_trap
                _osc_pattern = "bounce_trap" if _osc_bounce_trap else ("stale_drop" if _osc_stale_drop else "")

                # 2026-04-17: DISABLED — only fan_exhaustion gate active for entry filtering.
                # Bounce_trap blocked valid entries in waterfall trends (stoch pinned at
                # extremes = trend strength, not exhaustion). 132-trade backtest: stoch
                # gates 0.2-0.5:1 ratio = kill more winners than losers.
                # 2026-04-22: also bypassed for Kronos snipes (forecast-based thesis).
                if _osc_freshness_fires and tc_get("gate.oscillator_freshness_enabled", False) and not _is_kronos_snipe:
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s BLOCKED: oscillator_freshness/%s "
                        "— %s stoch %.0f→%.0f (jump=%+.0f)",
                        instrument, _osc_pattern, _snipe_dir, _stoch_prev, _stoch, _stoch_jump
                    )
                    _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="oscillator_freshness",
                              pattern=_osc_pattern,
                              stoch=round(_stoch, 1), stoch_prev=round(_stoch_prev, 1),
                              stoch_jump=round(_stoch_jump, 1), direction=_snipe_dir)
                    cycle_result["status"] = "skipped"
                    cycle_result["skip_reason"] = f"oscillator_freshness_{_osc_pattern}"
                    cycle_result["skip_detail"] = (
                        f"Stoch {_stoch_prev:.0f}→{_stoch:.0f} (jump {_stoch_jump:+.0f}) — "
                        f"{_osc_pattern} signals {_snipe_dir} entry is against the bounce "
                        f"(reversal window passed or bounce already started)"
                    )
                    return cycle_result
                _fr_snipe("SNIPE_GATE_PASSED", gate="oscillator_freshness",
                          stoch=round(_stoch, 1), stoch_prev=round(_stoch_prev, 1),
                          stoch_jump=round(_stoch_jump, 1))

                # ── NEW (2026-03-29): Candle position sanity check vs EMA 21 ──
                # The teaching charts show the cascade starts at the 2nd/3rd candle
                # AFTER the EMA 21/100 cross — price should be near EMA 21, not
                # far away.  If current close is too far from EMA 21, we're late.
                # Compute EMA 21 from closes and check distance in ATR multiples.
                if len(_closes) >= 21:
                    _ema21_val = float(_closes[0])
                    _ema21_k = 2.0 / (21 + 1)
                    for _cv in _closes[1:]:
                        _ema21_val = float(_cv) * _ema21_k + _ema21_val * (1 - _ema21_k)
                    _dist_from_ema21 = abs(_current_price - _ema21_val)
                    _dist_atr_mult = _dist_from_ema21 / _atr if _atr > 0 else 0
                    # SELL: price should be near or below EMA 21 (within 1.5 ATR)
                    # BUY:  price should be near or above EMA 21 (within 1.5 ATR)
                    _ema21_wrong_side = (
                        (_snipe_dir == "SELL" and _current_price > _ema21_val + 1.0 * _atr) or
                        (_snipe_dir == "BUY"  and _current_price < _ema21_val - 1.0 * _atr)
                    )
                    # 2026-04-07: DISABLED overextended check. EUR_AUD watch #1816 at
                    # 100% conditions blocked 23+ times. Price moves far from E21 during
                    # valid E100 breakdowns/continuations. The wrong_side check (price on
                    # opposite side of E21 from trade direction) still protects against
                    # truly invalid entries. If a snipe hits 100% of its watch conditions,
                    # the setup is valid — distance from E21 is the wrong filter.
                    # Keeping wrong_side only: SELL blocked if price ABOVE E21+1ATR,
                    # BUY blocked if price BELOW E21-1ATR.
                    _ema21_overextended = False  # disabled — see comment above
                    # 2026-04-22: Kronos forecasts reversal setups where price is far from
                    # EMA21 (that's the entry). This scout-thesis gate kills them. Bypass
                    # for Kronos; scout snipes still enforced.
                    if (_ema21_wrong_side or _ema21_overextended) and not _is_kronos_snipe:
                        _block_type = "overextended" if _ema21_overextended else "wrong_side"
                        logger.info(
                            "⚡ [SNIPE DIRECT] %s BLOCKED: candle position check "
                            "— %s but price=%.5f is %.1f ATR from EMA21=%.5f (%s)",
                            instrument, _snipe_dir, _current_price, _dist_atr_mult, _ema21_val, _block_type
                        )
                        _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="ema21_position",
                                  price=_current_price, ema21=round(_ema21_val, 5),
                                  dist_atr=round(_dist_atr_mult, 1), direction=_snipe_dir,
                                  block_type=_block_type)
                        cycle_result["status"] = "skipped"
                        cycle_result["skip_reason"] = f"candle_position_{_block_type}"
                        cycle_result["skip_detail"] = (
                            f"Price {_current_price:.5f} is {_dist_atr_mult:.1f}x ATR from "
                            f"EMA21 {_ema21_val:.5f} — {'chasing extended move' if _ema21_overextended else 'late entry, cascade already underway'}"
                        )
                        return cycle_result
                    _fr_snipe("SNIPE_GATE_PASSED", gate="ema21_position",
                              price=_current_price, ema21=round(_ema21_val, 5),
                              dist_atr=round(_dist_atr_mult, 1))

                # ── NEW: oscillator direction gate ───────────────────────────
                # Block entries where stochastic is moving AGAINST the trade direction
                # through the 20–80 zone. This catches "recovering from oversold" (don't short)
                # and "rolling from overbought" (don't buy) conditions.
                # Trade #1429 EUR_AUD: selling while stoch was rising from <20 (recovering)
                # Trade #1479 AUD_JPY: buying while stoch was falling from >80 (rolling over)
                # Only apply when stoch is in the 20-80 range (if it's already at extreme,
                # the existing momentum_trap above handles it).
                _stoch_rising = _stoch > _stoch_prev + 3  # stoch moving up meaningfully
                _stoch_falling = _stoch < _stoch_prev - 3  # stoch moving down meaningfully
                # 2026-04-07: _is_confirmed_snipe was never defined — crashed every
                # snipe that reached this gate. Snipes at 100% watch conditions are
                # already confirmed by the watch system. This gate should still apply
                # to non-watch snipes but not block confirmed watches.
                # 2026-04-09: DISABLED — see momentum_trap comment above. Gates OFF.
                _is_confirmed_snipe = (scout_context or {}).get('watch_id') is not None
                _osc_gate = not _is_confirmed_snipe and (
                    (_snipe_dir == "SELL" and _stoch < tc_get("gate.stoch_dont_sell_below", 35) and _stoch_rising) or   # recovering = don't short
                    (_snipe_dir == "BUY"  and _stoch > tc_get("gate.stoch_dont_buy_above", 65) and _stoch_falling)      # rolling = don't buy
                )
                if _osc_gate:
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s WOULD_BLOCK (disabled): oscillator direction gate "
                        "— %s with stoch=%.0f (prev=%.0f, %s through zone)",
                        instrument, _snipe_dir, _stoch, _stoch_prev,
                        "rising" if _stoch_rising else "falling"
                    )
                    _fr_snipe("SNIPE_GATE_LOGGED", status="would_block", gate="oscillator_direction",
                              stoch=round(_stoch, 1), stoch_prev=round(_stoch_prev, 1),
                              direction=_snipe_dir, stoch_trend="rising" if _stoch_rising else "falling")
                _fr_snipe("SNIPE_GATE_PASSED", gate="oscillator_direction",
                          stoch=round(_stoch, 1), stoch_prev=round(_stoch_prev, 1))

                # ── NEW: BB width gate (M1 Bollinger Band width) ──────────────
                # Validator writes bb_expanding==True (boolean) but that's not
                # selective enough (52% WR). M1 BB width is the real predictor:
                # Winners avg 10.8p, Losers avg 7.4p. Gate at 6 pips minimum.
                # 2026-03-26 deep audit: fan!=AGAINST + bb>=8 → 86% WR (+20.4p)
                _bb_width_min_pips = tc_get("gate.bb_width_min_pips", 6.0)  # Conservative start — tune upward if needed
                _bb_gate_passed = True
                _bb_width_pips = None
                _bb_expanding_live = None
                _bb_upper_val = None
                _bb_lower_val = None
                _bb_mid_val = None
                _pip_bb = 0.01 if "JPY" in instrument else 0.0001
                try:
                    _m1_raw = fetch_candles(instrument, timeframe="M1", count=25)
                    _m1_candles = _m1_raw.get("candles", []) if isinstance(_m1_raw, dict) else []
                    if len(_m1_candles) >= 20:
                        _m1_closes = np.array([float(c["mid"]["c"]) for c in _m1_candles if c.get("complete", True)])
                        if len(_m1_closes) >= 20:
                            _bb_period = 20
                            _bb_sma = np.mean(_m1_closes[-_bb_period:])
                            _bb_std = np.std(_m1_closes[-_bb_period:])
                            _bb_upper_val = float(_bb_sma + 2 * _bb_std)
                            _bb_lower_val = float(_bb_sma - 2 * _bb_std)
                            _bb_mid_val = float(_bb_sma)
                            _bb_width_raw = _bb_upper_val - _bb_lower_val
                            _bb_width_pips = _bb_width_raw / _pip_bb

                            # Check if expanding vs 5 bars ago
                            if len(_m1_closes) >= _bb_period + 5:
                                _prev_sma = np.mean(_m1_closes[-_bb_period - 5:-5])
                                _prev_std = np.std(_m1_closes[-_bb_period - 5:-5])
                                _prev_width = (_prev_sma + 2 * _prev_std) - (_prev_sma - 2 * _prev_std)
                                _bb_expanding_live = bool(_bb_width_raw > _prev_width)

                            if _bb_width_pips < _bb_width_min_pips:
                                _bb_gate_passed = False
                                logger.info(
                                    "⚡ [SNIPE DIRECT] %s BLOCKED: BB width %.1f pips < %.1f minimum "
                                    "(expanding=%s) — dead market, no energy for directional move",
                                    instrument, _bb_width_pips, _bb_width_min_pips, _bb_expanding_live
                                )
                            else:
                                logger.info(
                                    "⚡ [SNIPE DIRECT] %s BB width OK: %.1f pips >= %.1f min (expanding=%s)",
                                    instrument, _bb_width_pips, _bb_width_min_pips, _bb_expanding_live
                                )
                    else:
                        logger.warning("⚡ [SNIPE DIRECT] %s: only %d M1 candles — BB gate skipped",
                                       instrument, len(_m1_candles))
                except Exception as _bb_err:
                    logger.warning("⚡ [SNIPE DIRECT] %s: BB width check failed (%s) — proceeding without gate",
                                   instrument, _bb_err)

                # 2026-04-09: DISABLED — see momentum_trap comment above. Gates OFF.
                if not _bb_gate_passed:
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s WOULD_BLOCK (disabled): BB width %.1f < %.1f min",
                        instrument, _bb_width_pips or 0, _bb_width_min_pips
                    )
                    _fr_snipe("SNIPE_GATE_LOGGED", status="would_block", gate="bb_width",
                              bb_width_pips=round(_bb_width_pips, 1) if _bb_width_pips else 0,
                              bb_min_pips=_bb_width_min_pips, bb_expanding=_bb_expanding_live)
                _fr_snipe("SNIPE_GATE_PASSED", gate="bb_width",
                          bb_width_pips=round(_bb_width_pips, 1) if _bb_width_pips else None,
                          bb_expanding=_bb_expanding_live)

                # ── snipe_counter_momentum gate (2026-04-22) ─────────────────────
                # "Sanity check for snipes going into oversold or retracing late entry."
                # Multi-indicator pre-entry filter identified from 28 never-positive
                # snipe losses over 60 days. Each loss fit the same signature:
                # seller enters during a 3-bar counter-rally, on a green candle, near
                # E21 retest zone, with stoch mid-range, in BB compression.
                #
                # 5 conditions (for SELL; flipped for BUY):
                #   C1: entry candle color aligned (SELL=RED / BUY=GREEN)
                #   C2: prior 3-bar price moved WITH direction
                #   C3: stoch_k ≤ 45 AND turning lower (SELL) / ≥ 55 AND turning higher (BUY)
                #   C4: BB width expanding over last 3 bars
                #   C5: price ≥ 5 pips beyond E21 in direction (not in retest zone)
                #
                # Block if score < min_score (default 2). 60-day backtest on 141
                # validator snipes: blocks 23/29 never-positive losses (-577p saved)
                # at cost of 10 small wins (+43p) = net +534p.
                # Kronos-path snipes SKIP — they use forecast-path thesis.
                _cm_enabled = tc_get("snipe.gate.counter_momentum_enabled", True)
                _cm_min_score = int(tc_get("snipe.gate.counter_momentum_min_score", 2))
                if _cm_enabled and not _is_kronos_snipe:
                    try:
                        _ec = _candles[-1]
                        _eopen_cm = float(_ec["mid"]["o"])
                        _eclose_cm = float(_ec["mid"]["c"])
                        _is_long_cm = _snipe_dir == "BUY"

                        # C1 candle color aligned with direction
                        if _eclose_cm > _eopen_cm:
                            _c_color = "GREEN"
                        elif _eclose_cm < _eopen_cm:
                            _c_color = "RED"
                        else:
                            _c_color = "DOJI"
                        _c1 = (_is_long_cm and _c_color == "GREEN") or \
                              (not _is_long_cm and _c_color == "RED")

                        # C2 3-bar price extension aligned
                        if len(_closes) >= 4:
                            _ext_3bar = float(_closes[-1]) - float(_closes[-4])
                            _c2 = (_is_long_cm and _ext_3bar > 0) or \
                                  (not _is_long_cm and _ext_3bar < 0)
                        else:
                            _c2 = False

                        # C3 stoch aligned + turning further in direction
                        if _is_long_cm:
                            _c3 = _stoch >= 55 and _stoch >= _stoch_prev
                        else:
                            _c3 = _stoch <= 45 and _stoch <= _stoch_prev

                        # C4 BB width expanding over last 3 bars
                        _c4 = False
                        _bb_period = 20
                        if len(_closes) >= _bb_period + 2:
                            def _bb_width_at(end_idx):
                                _slc = _closes[end_idx - _bb_period:end_idx]
                                _std = float(np.std(_slc, ddof=0))
                                return 4.0 * _std  # upper-lower = 2 std above + 2 std below
                            _bw_now = _bb_width_at(len(_closes))
                            _bw_p1 = _bb_width_at(len(_closes) - 1)
                            _bw_p2 = _bb_width_at(len(_closes) - 2)
                            _c4 = _bw_now > _bw_p1 and _bw_p1 > _bw_p2 * 0.98

                        # C5 price extended ≥5 pips from E21 in direction
                        _c5 = False
                        if len(_closes) >= 21:
                            _alpha_cm = 2.0 / (21 + 1)
                            _e21_cm = float(_closes[0])
                            for _px in _closes[1:]:
                                _e21_cm = _alpha_cm * float(_px) + (1 - _alpha_cm) * _e21_cm
                            _pip_cm = 0.01 if "JPY" in instrument.upper() else 0.0001
                            _pos_e21 = (_current_price - _e21_cm) / _pip_cm
                            _c5 = (_is_long_cm and _pos_e21 >= 5.0) or \
                                  (not _is_long_cm and _pos_e21 <= -5.0)

                        _cm_score = int(_c1) + int(_c2) + int(_c3) + int(_c4) + int(_c5)

                        if _cm_score < _cm_min_score:
                            logger.info(
                                "⚡ [SNIPE DIRECT] %s BLOCKED: counter_momentum score=%d/5 < min=%d "
                                "(c1_color=%s c2_ext=%s c3_stoch=%s c4_bb_exp=%s c5_pos_e21=%s)",
                                instrument, _cm_score, _cm_min_score,
                                _c1, _c2, _c3, _c4, _c5,
                            )
                            _fr_snipe(
                                "SNIPE_GATE_BLOCKED",
                                status="blocked",
                                gate="counter_momentum",
                                score=_cm_score,
                                min_score=_cm_min_score,
                                c1_candle_color=bool(_c1),
                                c2_3bar_extension=bool(_c2),
                                c3_stoch_direction=bool(_c3),
                                c4_bb_expanding=bool(_c4),
                                c5_pos_vs_e21=bool(_c5),
                            )
                            cycle_result["status"] = "skipped"
                            cycle_result["skip_reason"] = "counter_momentum"
                            cycle_result["skip_detail"] = (
                                f"score {_cm_score}/5 below min {_cm_min_score} — "
                                f"counter-momentum/retest-zone entry"
                            )
                            cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                            return cycle_result

                        _fr_snipe(
                            "SNIPE_GATE_PASSED",
                            gate="counter_momentum",
                            score=_cm_score,
                            min_score=_cm_min_score,
                        )
                    except Exception as _cm_err:
                        logger.warning(
                            "⚡ [SNIPE DIRECT] %s: counter_momentum check failed (%s) — proceeding",
                            instrument, _cm_err,
                        )

                # ── post_win_exhaustion gate (2026-04-14) ───────────────────────
                # Trades #5484 EUR_AUD -26.3p and #5581 USD_CHF -5.3p both snipe-entered
                # right after BIG-BB wins on same pair+setup+direction — move already
                # exhausted. Candle-walk on 185 trades:
                #   - Blocks when M15 BB contracted >50% vs last same-setup+direction win
                #     in last 6h, AND prior-win BB was > 20 pips (exhaustion-worthy move)
                #   - Post-Apr-9 (29 trades): catches 5484 + 5581, zero false positives
                #   - Pre-Apr-9 (156 trades): catches 1 loss, 2 false positives
                #   - Net: +70p across 185 trades
                # 2026-04-17: DISABLED — fan_exhaustion gate replaces this.
                _exh_enabled = tc_get("gate.post_win_exhaustion_enabled", False)
                _exh_contraction_min = tc_get("gate.post_win_exhaustion_contraction_min", 0.50)
                _exh_lookback_h = tc_get("gate.post_win_exhaustion_lookback_hours", 6)
                _exh_prior_bb_pips_min = tc_get("gate.post_win_exhaustion_prior_bb_pips_min", 20.0)

                _exh_blocks = False
                _exh_current_bb_m15 = None
                _exh_last_win_bb = None
                _exh_last_win_id = None
                _exh_contraction = None

                if _exh_enabled:
                    try:
                        # Compute current M15 BB width
                        _m15_raw = fetch_candles(instrument, timeframe="M15", count=25)
                        _m15_candles = _m15_raw.get("candles", []) if isinstance(_m15_raw, dict) else []
                        if len(_m15_candles) >= 20:
                            _m15_closes = np.array([float(c["mid"]["c"]) for c in _m15_candles if c.get("complete", True)])
                            if len(_m15_closes) >= 20:
                                _m15_sma = np.mean(_m15_closes[-20:])
                                _m15_std = np.std(_m15_closes[-20:])
                                _exh_current_bb_m15 = float((_m15_sma + 2*_m15_std) - (_m15_sma - 2*_m15_std))

                                # Identify setup for matching
                                _exh_setup = (scout_context or {}).get("setup") or \
                                             (scout_context or {}).get("setup_code") or "unknown"
                                _norm_dir = 'sell' if _snipe_dir == 'SELL' else 'buy'

                                import sqlite3 as _exh_sqlite
                                from datetime import datetime as _exh_dt, timezone as _exh_tz, timedelta as _exh_td
                                _exh_cutoff = (_exh_dt.now(_exh_tz.utc) - _exh_td(hours=_exh_lookback_h)).isoformat()

                                with _exh_sqlite.connect(_TRADING_FOREX_DB, timeout=5) as _exh_conn:
                                    _exh_row = _exh_conn.execute(
                                        "SELECT id, bb_width FROM live_trades "
                                        "WHERE pair=? AND direction=? AND setup=? AND outcome='win' "
                                        "  AND entry_time >= ? AND bb_width IS NOT NULL AND bb_width > 0 "
                                        "ORDER BY entry_time DESC LIMIT 1",
                                        (instrument, _norm_dir, _exh_setup, _exh_cutoff)
                                    ).fetchone()

                                if _exh_row:
                                    _exh_last_win_id, _exh_last_win_bb = _exh_row
                                    _exh_prior_bb_pips = _exh_last_win_bb / _pip_bb
                                    # Only apply gate when prior win BB was substantial (>20p)
                                    if _exh_prior_bb_pips > _exh_prior_bb_pips_min and _exh_last_win_bb > 0:
                                        _exh_contraction = 1.0 - (_exh_current_bb_m15 / _exh_last_win_bb)
                                        if _exh_contraction > _exh_contraction_min:
                                            _exh_blocks = True
                    except Exception as _exh_err:
                        logger.warning("⚡ [SNIPE DIRECT] %s: post_win_exhaustion check failed (%s) — proceeding",
                                       instrument, _exh_err)

                if _exh_blocks:
                    _exh_pct = (_exh_contraction or 0) * 100
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s BLOCKED: post_win_exhaustion "
                        "— M15 BB %.0f%% smaller than last same-setup win #%s (within %dh)",
                        instrument, _exh_pct, _exh_last_win_id, _exh_lookback_h
                    )
                    _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="post_win_exhaustion",
                              current_bb_m15=round(_exh_current_bb_m15, 6),
                              last_win_bb=round(_exh_last_win_bb, 6),
                              last_win_id=_exh_last_win_id,
                              contraction_pct=round(_exh_pct, 1),
                              lookback_hours=_exh_lookback_h)
                    cycle_result["status"] = "skipped"
                    cycle_result["skip_reason"] = "post_win_exhaustion"
                    cycle_result["skip_detail"] = (
                        f"M15 BB width {_exh_current_bb_m15:.6f} is {_exh_pct:.0f}% smaller "
                        f"than last {instrument} {_snipe_dir} win #{_exh_last_win_id} "
                        f"(within {_exh_lookback_h}h) — move exhausted, no energy left"
                    )
                    return cycle_result
                _fr_snipe("SNIPE_GATE_PASSED", gate="post_win_exhaustion",
                          current_bb_m15=round(_exh_current_bb_m15, 6) if _exh_current_bb_m15 else None,
                          last_win_bb=round(_exh_last_win_bb, 6) if _exh_last_win_bb else None,
                          last_win_id=_exh_last_win_id,
                          contraction_pct=round((_exh_contraction or 0) * 100, 1) if _exh_contraction else None)

                # ── Fan exhaustion gate (rewritten 2026-05-15) ──────────────
                # Original (2026-04-17) read the classifier's fan_state label
                # ("stable"/"contracting"/"peaked"/"decelerating") and blocked
                # anything not in {expanding, accelerating, just_crossed}.
                # 14-day audit (255 blocks): the LABEL was wrong 66% of the
                # time — EMAs still ordered, fan still wide, snipe direction
                # aligned with fan, but the 5-bar E21-E55 delta classifier
                # labeled it "stable" or "contracting" and the gate fired
                # "exhausted." Classic healthy retracement misread as dying
                # trend. The classifier conflated "no growth in 5 bars" with
                # "trend dying" — wrong for parallel cruising fans and for
                # E21-pullback retest entries (exactly the snipe setup).
                #
                # Direct geometric check: fan is exhausted ONLY when EMAs
                # actually disagree with the snipe (collapsed or unordered or
                # reversed direction). Healthy ordered fan = snipe proceeds.
                _fan_exhaust_enabled = tc_get("gate.fan_exhaustion_enabled", True)
                _fan_exhaust_blocks = False
                _fan_exhaust_reason = ""

                # Re-fetch EMAs safely (same source the ema_ordering gate uses)
                _fe_ema_vals = {}
                if '_refetched_mp' in dir() and _refetched_mp:
                    _fe_ema_vals = (_refetched_mp.get("ema", {}) or {}).get("current_emas", {})
                if not _fe_ema_vals and (scout_context or {}).get("market_picture"):
                    _fe_mp_sc = scout_context["market_picture"]
                    if isinstance(_fe_mp_sc, dict):
                        _fe_ema_vals = (_fe_mp_sc.get("ema", {}) or {}).get("current_emas", {})

                _fe_e21 = float(_fe_ema_vals.get("ema21", 0) or 0)
                _fe_e55 = float(_fe_ema_vals.get("ema55", 0) or 0)
                _fe_e100 = float(_fe_ema_vals.get("ema100", 0) or 0)

                if _fan_exhaust_enabled and _fe_e21 and _fe_e55 and _fe_e100:
                    _fe_fan_pips = (max(_fe_e21, _fe_e55, _fe_e100) - min(_fe_e21, _fe_e55, _fe_e100)) / _pip_bb
                    _fe_bullish_ordered = _fe_e21 > _fe_e55 > _fe_e100
                    _fe_bearish_ordered = _fe_e100 > _fe_e55 > _fe_e21
                    _fe_aligned = (
                        (_snipe_dir == "BUY"  and _fe_bullish_ordered) or
                        (_snipe_dir == "SELL" and _fe_bearish_ordered)
                    )
                    _fe_min_pips = float(tc_get("gate.fan_exhaustion_min_pips", 4.0))

                    if not _fe_aligned:
                        _fan_exhaust_blocks = True
                        _fe_implied = "bullish" if _fe_bullish_ordered else ("bearish" if _fe_bearish_ordered else "mixed")
                        _fan_exhaust_reason = f"EMAs not ordered for {_snipe_dir} (implied={_fe_implied})"
                    elif _fe_fan_pips < _fe_min_pips:
                        _fan_exhaust_blocks = True
                        _fan_exhaust_reason = f"fan width collapsed ({_fe_fan_pips:.1f}p < {_fe_min_pips:.1f}p min)"

                # 2026-04-22: Kronos uses forecast-path thesis. Bypass.
                if _fan_exhaust_blocks and not _is_kronos_snipe:
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s BLOCKED: fan_exhaustion — %s",
                        instrument, _fan_exhaust_reason
                    )
                    _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked", gate="fan_exhaustion",
                              reason=_fan_exhaust_reason,
                              e21=round(_fe_e21, 5), e55=round(_fe_e55, 5), e100=round(_fe_e100, 5),
                              fan_pips=round((max(_fe_e21, _fe_e55, _fe_e100) - min(_fe_e21, _fe_e55, _fe_e100)) / _pip_bb, 1)
                                       if (_fe_e21 and _fe_e55 and _fe_e100) else None)
                    cycle_result["status"] = "skipped"
                    cycle_result["skip_reason"] = "fan_exhaustion"
                    cycle_result["skip_detail"] = _fan_exhaust_reason
                    return cycle_result
                _fr_snipe("SNIPE_GATE_PASSED", gate="fan_exhaustion",
                          e21=round(_fe_e21, 5) if _fe_e21 else None,
                          e55=round(_fe_e55, 5) if _fe_e55 else None,
                          e100=round(_fe_e100, 5) if _fe_e100 else None,
                          fan_pips=round((max(_fe_e21, _fe_e55, _fe_e100) - min(_fe_e21, _fe_e55, _fe_e100)) / _pip_bb, 1)
                                   if (_fe_e21 and _fe_e55 and _fe_e100) else None)

                # ── Re-fire cap & stale-gap gate (2026-04-21, rewritten 2026-04-23) ──
                # SPEC (Tim's design intent): the cap exists to prevent a LOSING watch
                # from bleeding pnl by firing repeatedly into a dying setup. A watch
                # that's WINNING should keep firing — the cap is not a hard ceiling,
                # it's a loss-protection mechanism.
                #
                # Rules:
                #   1. Day boundary = ET midnight (04:00 UTC in EDT / 05:00 in EST),
                #      NOT UTC midnight. A trade closed 23:03 ET yesterday no longer
                #      counts against today's budget.
                #   2. Only LOSING fires count toward the cap. Wins don't penalize the
                #      watch — a setup that keeps paying out should keep getting taken.
                #   3. Still honor the refire-gap (stale fire detection).
                #
                # Prior spec was: UTC day + count ALL fires. That locked out watch 1939
                # AUD_USD today despite 3 winning fires yesterday ET. Observed 2026-04-23.
                # 2026-04-22: Kronos snipes don't re-fire on the same watch (Kronos creates
                # ephemeral snipes per forecast cycle, old ones expire). Bypass the cap.
                _watch_id_for_cap = (scout_context or {}).get("watch_id")
                if _watch_id_for_cap and not _is_kronos_snipe:
                    try:
                        _max_losing_fires = int(tc_get("gate.snipe_max_fires_per_watch_per_day", 3))
                        _max_gap_min = int(tc_get("gate.snipe_refire_max_gap_minutes", 120))
                        from flight_recorder import DB_PATH as _REFIRE_FL_DB
                        import sqlite3 as _refire_sq
                        _now_utc = datetime.now(timezone.utc)
                        # ET day start — default 4h offset for EDT. DST adjustment is
                        # a future enhancement; for now rely on EDT (Apr-Nov).
                        _et_offset_hours = int(tc_get("gate.snipe_day_et_offset_hours", 4))
                        _now_et = _now_utc - timedelta(hours=_et_offset_hours)
                        _et_day_start_naive = _now_et.replace(hour=0, minute=0, second=0, microsecond=0)
                        _day_start_utc = (_et_day_start_naive + timedelta(hours=_et_offset_hours)).replace(tzinfo=timezone.utc)
                        _day_start = _day_start_utc.isoformat()
                        with _refire_sq.connect(str(_REFIRE_FL_DB), timeout=3) as _fc:
                            _fc.row_factory = _refire_sq.Row
                            # Prior fires on this watch since ET midnight
                            _prior = _fc.execute(
                                "SELECT timestamp, data FROM flight_log "
                                "WHERE stage='SNIPE_OPENED' "
                                "AND json_extract(data, '$.watch_id') = ? "
                                "AND timestamp >= ? AND timestamp < ? "
                                "ORDER BY timestamp DESC",
                                (int(_watch_id_for_cap), _day_start, _now_utc.isoformat())
                            ).fetchall()
                        # Count only LOSING fires (look up each trade's pnl in live_trades)
                        _fires_today = len(_prior)
                        _losing_fires = 0
                        _winning_fires = 0
                        if _prior:
                            try:
                                import json as _j_lt
                                from db_pool import get_trading_forex as _gtf_cap
                                _lt_cap = _gtf_cap()
                                for _p in _prior:
                                    try:
                                        _pd = _j_lt.loads(_p['data']) if _p['data'] else {}
                                    except Exception:
                                        continue
                                    _ptid = _pd.get('trade_id')
                                    if not _ptid:
                                        continue
                                    _pnl_row = _lt_cap.execute(
                                        "SELECT pnl_pips, status FROM live_trades WHERE id=? LIMIT 1",
                                        (str(_ptid),)
                                    ).fetchone()
                                    if not _pnl_row:
                                        continue
                                    # Only closed trades count toward cap — open ones are pending
                                    if _pnl_row[1] != 'closed':
                                        continue
                                    _pnl = _pnl_row[0] or 0
                                    if _pnl > 0:
                                        _winning_fires += 1
                                    else:
                                        _losing_fires += 1
                            except Exception as _lt_err:
                                logger.debug("[SNIPE DIRECT] cap pnl lookup failed: %s — treating all fires as losing (fail safe)",
                                             _lt_err)
                                _losing_fires = _fires_today  # conservative
                        _fire_n = _fires_today + 1
                        _gap_min = None
                        if _prior:
                            _last_ts = datetime.fromisoformat(_prior[0]['timestamp'].replace('Z','+00:00'))
                            _gap_min = (_now_utc - _last_ts).total_seconds() / 60

                        if _losing_fires >= _max_losing_fires:
                            _reason = (f"losing_fires={_losing_fires} >= cap {_max_losing_fires} "
                                       f"(wins={_winning_fires}, total_today_ET={_fires_today})")
                            logger.info("⚡ [SNIPE DIRECT] %s BLOCKED: fire_count_cap — %s (watch_id=%s)",
                                        instrument, _reason, _watch_id_for_cap)
                            _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked",
                                      gate="fire_count_cap",
                                      reason=_reason, watch_id=_watch_id_for_cap,
                                      losing_fires=_losing_fires, winning_fires=_winning_fires,
                                      fire_n=_fire_n, max_losing_fires=_max_losing_fires)
                            cycle_result["status"] = "skipped"
                            cycle_result["skip_reason"] = "fire_count_cap"
                            cycle_result["skip_detail"] = _reason
                            return cycle_result

                        if _gap_min is not None and _gap_min > _max_gap_min:
                            _reason = f"refire gap {_gap_min:.0f}min exceeds max ({_max_gap_min})"
                            logger.info("⚡ [SNIPE DIRECT] %s BLOCKED: %s (watch_id=%s, fire#%d)",
                                        instrument, _reason, _watch_id_for_cap, _fire_n)
                            _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked",
                                      gate="refire_gap_exceeded",
                                      reason=_reason, watch_id=_watch_id_for_cap,
                                      fire_n=_fire_n, gap_min=round(_gap_min, 1),
                                      max_gap_min=_max_gap_min)
                            cycle_result["status"] = "skipped"
                            cycle_result["skip_reason"] = "refire_gap_exceeded"
                            cycle_result["skip_detail"] = _reason
                            return cycle_result

                        _fr_snipe("SNIPE_GATE_PASSED", gate="refire_cap",
                                  watch_id=_watch_id_for_cap, fire_n=_fire_n,
                                  gap_min=round(_gap_min, 1) if _gap_min is not None else None)
                    except Exception as _re_err:
                        logger.debug("[SNIPE DIRECT] refire gate check failed: %s — proceeding", _re_err)
                        _fr_snipe("SNIPE_GATE_PASSED", gate="refire_cap",
                                  note=f"check failed, proceeding: {_re_err}")

                # ── Conditional exhaustion gate (2026-04-22) ──────────────────
                # Only fires AFTER 2+ wins on the same pair/direction today.
                # First entries at low RSI are valid (fresh breakdown). Re-entries
                # into an exhausted move are not. 14-day backtest: blocks 8 losses
                # saving -145.9p while letting first entries through.
                try:
                    from db_pool import get_trading_forex as _exh_gtf
                    _exh_conn = _exh_gtf()
                    _exh_today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).isoformat()
                    _prior_wins = _exh_conn.execute(
                        "SELECT COUNT(*) FROM live_trades "
                        "WHERE pair=? AND direction=? AND result='win' "
                        "AND source='snipe_direct' AND entry_time >= ?",
                        (instrument, _snipe_dir.lower(), _exh_today)
                    ).fetchone()[0]

                    if _prior_wins >= 2:
                        _exh_sell_blocked = _snipe_dir == "SELL" and _rsi < 35
                        _exh_buy_blocked = _snipe_dir == "BUY" and _rsi > 65
                        if _exh_sell_blocked or _exh_buy_blocked:
                            _exh_reason = (
                                f"exhaustion after {_prior_wins} wins today: "
                                f"RSI={_rsi:.1f} Stoch={_stoch:.1f} — move exhausted"
                            )
                            logger.info("⚡ [SNIPE DIRECT] %s BLOCKED: %s", instrument, _exh_reason)
                            _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked",
                                      gate="conditional_exhaustion",
                                      rsi=round(_rsi, 1), stoch=round(_stoch, 1),
                                      prior_wins=_prior_wins, direction=_snipe_dir)
                            cycle_result["status"] = "skipped"
                            cycle_result["skip_reason"] = "conditional_exhaustion"
                            cycle_result["skip_detail"] = _exh_reason
                            return cycle_result
                        else:
                            logger.debug("⚡ [SNIPE DIRECT] %s: %d prior wins but RSI=%.1f OK — proceeding",
                                         instrument, _prior_wins, _rsi)
                    _fr_snipe("SNIPE_GATE_PASSED", gate="conditional_exhaustion",
                              prior_wins=_prior_wins, rsi=round(_rsi, 1))
                except Exception as _exh_err:
                    logger.debug("[SNIPE DIRECT] exhaustion gate check failed: %s — proceeding", _exh_err)

                # ── 3. ATR / pip prep (SL/TP prices calculated after user settings loaded) ──
                _pip = 0.01 if "JPY" in instrument else 0.0001
                _atr_pips = _atr / _pip
                # Defaults — overridden by user's dashboard settings below
                # 2026-04-01: SL raised to 2.5×ATR (was 1.5×). Backtest of 48hrs snipes:
                # 3 trades hit original SL (1.5×) that would have survived at 2.5×, saving $54.
                # Net improvement +$97 even after accounting for smaller position sizes.
                # Guardian ratcheting profit floor + retrace trail manage the trade once open;
                # the SL just needs to survive the initial retracement.
                _sl_atr_mult = tc_get("gate.sl_atr_mult", 2.5)   # was 1.5 — too tight, retracements hitting SL before guardian can act
                _tp_atr_mult = tc_get("gate.tp_atr_mult", 2.0)   # wider TP:  ~16p (was 1.0 =  ~8p) → 0.8 R:R baseline, guardian extends

                # ── 4. Position size — pip-value aware sizing ──────────────
                # The dashboard dropdown labels lots by $/pip (e.g. 10000 = "$1/pip")
                # but that only holds for XXX_USD pairs. For JPY pairs, cross pairs
                # etc. we must adjust units so the ACTUAL pip value matches the
                # user's intent.  Read the nominal units from config, derive the
                # $/pip target, then compute correct units for THIS pair.
                _nominal_units = 10000  # fallback: "$1/pip" intent
                try:
                    import json as _jmod
                    _rcfg_path = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "Config", "risk_config.json"
                    )
                    with open(_rcfg_path) as _rcf:
                        _rcfg = _jmod.load(_rcf)
                    _nominal_units = int(_rcfg.get("position_sizing", {}).get("fixed_units", 10000))
                    # SL/TP multipliers from sniper section (user can tune in dashboard)
                    _sniper_cfg  = _rcfg.get("sniper", {})
                    _sl_atr_mult = float(_sniper_cfg.get("sl_atr", 2.5))
                    _tp_atr_mult = float(_sniper_cfg.get("tp_atr", 1.0))
                except Exception as _rcfg_err:
                    logger.warning("[SNIPE DIRECT] Could not load risk_config: %s — using defaults", _rcfg_err)
                # User DB overrides (trading_preferences table in core.db)
                try:
                    import sqlite3 as _pdb
                    with _pdb.connect(_CORE_DB,
                                      timeout=5) as _pc:
                        _prows = _pc.execute(
                            "SELECT pref_key, pref_value FROM trading_preferences WHERE user_id=? "
                            "AND pref_key IN ('risk_fixed_units','risk_sniper_sl_atr','risk_sniper_tp_atr')",
                            (self.user_id,)
                        ).fetchall()
                        for _pk, _pv in _prows:
                            if _pk == 'risk_fixed_units':
                                _nominal_units = int(float(_pv))
                            elif _pk == 'risk_sniper_sl_atr':
                                _sl_atr_mult = float(_pv)
                            elif _pk == 'risk_sniper_tp_atr':
                                _tp_atr_mult = float(_pv)
                except Exception:
                    pass  # table may not exist yet — use config values

                # Fixed mode = literal units.  User said "10,000 units" = exactly 10,000.
                # Do NOT convert through pip-value scaling — that produced different
                # unit counts per pair (e.g. 7,950 for GBP_JPY) which confused the user.
                _units = _nominal_units

                logger.info("[SNIPE DIRECT] %s: fixed mode → literal %d units, "
                            "SL=%.1f×ATR, TP=%.1f×ATR",
                            instrument, _units,
                            _sl_atr_mult, _tp_atr_mult)

                # ── 5. Compute SL / TP prices from user's ATR settings ───
                # JPY pairs use 3 decimal places; all others use 5
                _price_precision = 3 if "JPY" in instrument else 5

                # ── Fan-state aware SL cap ───────────────────────────────────
                # 2026-04-01: Raised caps — old 1.5/2.0 was too tight, causing SL hits
                # on normal retracements. New baseline is 2.5×ATR for all fan states.
                # Expanding fans get no extra room since 2.5× already provides adequate buffer.
                _snipe_fan_state = (scout_context or {}).get("fan_state", "") or ""
                _fan_is_expanding = _snipe_fan_state in ("expanding", "just_crossed", "accelerating")
                if not _fan_is_expanding and _sl_atr_mult > 2.5:
                    _original_sl_mult = _sl_atr_mult
                    _sl_atr_mult = 2.5  # non-expanding fan: cap at 2.5×ATR
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s: fan_state='%s' (non-expanding) → SL capped %.1f→%.1f×ATR (~%.0fp max)",
                        instrument, _snipe_fan_state, _original_sl_mult, _sl_atr_mult, _atr_pips * _sl_atr_mult
                    )
                elif _fan_is_expanding and _sl_atr_mult > tc_get("gate.sl_atr_mult_expanding_fan", 3.0):
                    _sl_atr_mult = tc_get("gate.sl_atr_mult_expanding_fan", 3.0)  # expanding fan: allow up to 3.0×ATR

                _sl_dist = _atr * _sl_atr_mult
                _tp_dist = _atr * _tp_atr_mult
                if _snipe_dir == "BUY":
                    _sl_price = round(_current_price - _sl_dist, _price_precision)
                    _tp_price = round(_current_price + _tp_dist, _price_precision)
                else:
                    _sl_price = round(_current_price + _sl_dist, _price_precision)
                    _tp_price = round(_current_price - _tp_dist, _price_precision)

                # ── R:R gate — never place a trade below 1.2 R:R ─────────────
                # This is the root cause of our 0.37 R:R problem. Hard-block sub-1.2 trades.
                _actual_rr = _tp_atr_mult / _sl_atr_mult if _sl_atr_mult > 0 else 0
                _min_rr = tc_get("gate.min_rr_ratio", 1.2)
                if _actual_rr < _min_rr:
                    logger.warning(
                        "⛔ [SNIPE DIRECT] %s: R:R %.2f below minimum %.1f (SL=%.1f×ATR TP=%.1f×ATR) — "
                        "adjusting TP to enforce minimum R:R",
                        instrument, _actual_rr, _min_rr, _sl_atr_mult, _tp_atr_mult
                    )
                    # Widen TP to meet minimum rather than aborting — direction was good
                    _tp_atr_mult = _sl_atr_mult * _min_rr
                    _tp_dist = _atr * _tp_atr_mult
                    if _snipe_dir == "BUY":
                        _tp_price = round(_current_price + _tp_dist, _price_precision)
                    else:
                        _tp_price = round(_current_price - _tp_dist, _price_precision)
                    logger.info(
                        "⚡ [SNIPE DIRECT] %s: TP adjusted → %.1f×ATR (~%.0fp) to reach %.1f R:R",
                        instrument, _tp_atr_mult, _atr_pips * _tp_atr_mult, _min_rr
                    )

                logger.info(
                    "⚡ [SNIPE DIRECT] %s %s: price=%.5f ATR=%.1fpips SL=%.5f(%.0fp) TP=%.5f(%.0fp) "
                    "R:R=%.2f units=%d RSI=%.0f Stoch=%.0f",
                    instrument, _snipe_dir, _current_price, _atr_pips,
                    _sl_price, _atr_pips * _sl_atr_mult,
                    _tp_price, _atr_pips * _tp_atr_mult,
                    _tp_atr_mult / _sl_atr_mult if _sl_atr_mult > 0 else 0,
                    _units, _rsi, _stoch
                )

                # ── 5. Register thesis with guardian BEFORE order ────────
                try:
                    _guardian_instance = globals().get("_guardian_instance") or \
                                         getattr(self, "_guardian", None)
                    if _guardian_instance:
                        # Determine fan state at entry — critical for guardian retracement detection
                        _thesis_fan_state = (scout_context or {}).get('fan_state', '')
                        _thesis_fan_dir = (scout_context or {}).get('fan_direction', '')
                        # Detect if this is a retracement entry based on fan state + conditions
                        _thesis_is_retrace = _thesis_fan_state in (
                            'peaked', 'contracting', 'compressed', 'just_crossed'
                        )
                        # Extract invalidation level from watch conditions if available
                        _thesis_invalidation = None
                        _conditions_met = (scout_context or {}).get('conditions_met', [])
                        if isinstance(_conditions_met, list):
                            for _cond in _conditions_met:
                                if isinstance(_cond, dict) and 'invalidation' in str(_cond.get('condition', '')).lower():
                                    try:
                                        _thesis_invalidation = float(_cond.get('target', 0))
                                    except (ValueError, TypeError):
                                        pass
                        _guardian_instance.register_thesis(instrument, {
                            'entry_type': 'snipe_direct',
                            'thesis': scout_context.get('user_thesis', '') or
                                      scout_context.get('validator_reasoning', ''),
                            'direction': _snipe_dir,
                            'watch_id': _watch_id,
                            'opportunity_score': scout_context.get('confluence_score', 0),
                            'fan_state_at_entry': _thesis_fan_state,
                            'fan_direction_at_entry': _thesis_fan_dir,
                            'is_retracement_entry': _thesis_is_retrace,
                            'invalidation_level': _thesis_invalidation,
                            'setup_name': (scout_context or {}).get('setup_name', ''),
                            'regime': (scout_context or {}).get('re_entry_regime', ''),
                        })
                except Exception:
                    pass

                # ── 5a. Setup ID extraction (logging only, no blocking) ──
                # Setup classification can produce various IDs (V4_CRITERIA_MET, S16, etc.)
                # or be empty for older watches. This is used for analytics/tracking only —
                # NOT as an execution gate. The snipe already passed scout + validator + 80%+ conditions.
                _setup_id = (scout_context.get('setup_id') or '').strip() if scout_context else ''
                if not _setup_id:
                    _setup_id = f"snipe_watch_{_watch_id}"
                    logger.info("[SNIPE DIRECT] %s: no setup_id in context, using '%s' for tracking",
                                instrument, _setup_id)
                else:
                    logger.info("✅ [SNIPE DIRECT] %s: setup_id='%s'", instrument, _setup_id)

                # ── 5b. REMOVED: Pre-trade guardian gate ──────────────────────────────
                # The guardian's job is to MANAGE OPEN TRADES, not gatekeep entries.
                # The snipe already passed: scout detection → validator confirmation →
                # 80%+ conditions met. The guardian has an 8-minute development grace
                # period built in for new trades — it lets them breathe after opening.
                # Running score_threat() before the trade opens was blocking valid
                # setups on mild E100 proximity / velocity noise (threat 31-36).
                # Removed 2026-03-26. Guardian starts monitoring AFTER trade opens.
                # ──────────────────────────────────────────────────────────────────────

                # ── 5b. REMOVED: Snipe list enforcement gate ─────────────────────────
                # The snipe list (user_snipe_list) was blocking valid trades because
                # setup_id names don't stay in sync:
                #   - V4_CRITERIA_MET vs CRITERIA_MET (naming mismatch)
                #   - S16 not in list (new setup type never added)
                #   - Empty setup_id → looked up as snipe_watch_{id}
                # The snipe already passed: scout → validator → 80%+ conditions.
                # The snipe list should TRACK what works, not gate execution.
                # Removed 2026-03-26. Snipe list remains for analytics/reporting only.
                # ──────────────────────────────────────────────────────────────────────
                _fr_snipe("SNIPE_ALL_GATES_PASSED", gate="all",
                          rsi=round(_rsi, 1), stoch=round(_stoch, 1),
                          atr_pips=round(_atr_pips, 1), units=_units,
                          sl_atr=_sl_atr_mult, tp_atr=_tp_atr_mult,
                          setup_id=_setup_id)
                logger.info("✅ [SNIPE DIRECT] %s %s: setup='%s' — proceeding to order",
                            instrument, _snipe_dir, _setup_id)

                # ── 5b. DOA safety check — verify price hasn't blown past the SL ──
                # Trade #4427 lost -$160 in 56ms because price was already at the SL
                # when the order was placed. Check current price vs SL before sending.
                try:
                    from Source.oanda_client import OandaClient as _OC_doa
                    _oc_doa = _OC_doa()
                    _live_price = _oc_doa.get_pricing(instrument)
                    if _live_price:
                        _ask_now = float(_live_price.get('asks', [{'price': 0}])[0].get('price', 0))
                        _bid_now = float(_live_price.get('bids', [{'price': 0}])[0].get('price', 0))
                        # For SELL: if ask is already >= SL, trade is DOA
                        # For BUY: if bid is already <= SL, trade is DOA
                        _doa = False
                        if _snipe_dir == "SELL" and _ask_now > 0 and _ask_now >= _sl_price:
                            _doa = True
                            logger.error("🛑 [SNIPE DOA] %s SELL blocked — ask %.5f already >= SL %.5f (would lose instantly)",
                                        instrument, _ask_now, _sl_price)
                        elif _snipe_dir == "BUY" and _bid_now > 0 and _bid_now <= _sl_price:
                            _doa = True
                            logger.error("🛑 [SNIPE DOA] %s BUY blocked — bid %.5f already <= SL %.5f (would lose instantly)",
                                        instrument, _bid_now, _sl_price)
                        if _doa:
                            _fr_snipe("SNIPE_DOA_BLOCKED", status="blocked",
                                      reason=f"Price already past SL: ask={_ask_now} bid={_bid_now} sl={_sl_price}")
                            cycle_result["status"] = "blocked"
                            cycle_result["skip_reason"] = "doa_price_past_sl"
                except Exception as _doa_err:
                    logger.debug("[SNIPE DOA] Price check failed (non-fatal): %s", _doa_err)

                if cycle_result.get("skip_reason") == "doa_price_past_sl":
                    return cycle_result  # DOA — exit without placing order

                # ── Tight-fan gate (2026-05-14): block tight-stale or overextended Phase 3 ──
                # Backtest 425 trades / 30d: +197.7p net. Catches mature-stalled cascades
                # (sep<0.10% for 20+ bars) and overextended fresh entries (ext>=3.4 ATR).
                # Tunable via gate.tight_fan_enabled. Fail-open on any error.
                try:
                    if tc_get("gate.tight_fan_enabled", True):
                        from tight_fan_gate import check_tight_fan_gate
                        _tf_raw = fetch_candles(instrument, "M15", 150)
                        _tf_candles = _tf_raw.get("candles", []) if isinstance(_tf_raw, dict) else (_tf_raw or [])
                        _tf_result = check_tight_fan_gate(_tf_candles, _snipe_dir)
                        if _tf_result["block"]:
                            logger.info("[TIGHT_FAN_GATE BLOCK] %s %s: %s | data=%s",
                                        instrument, _snipe_dir, _tf_result["reason"], _tf_result["data"])
                            _fr_snipe("SNIPE_GATE_BLOCKED", status="blocked",
                                      gate="tight_fan", reason=_tf_result["reason"],
                                      **_tf_result["data"])
                            cycle_result["status"] = "skipped"
                            cycle_result["skip_reason"] = f"tight_fan_gate: {_tf_result['reason']}"
                            return cycle_result
                except Exception as _tf_exc:
                    logger.warning("[TIGHT_FAN_GATE] snipe fail-open: %s", _tf_exc)
                # ──────────────────────────────────────────────────────────

                # ── 6. Place the order ────────────────────────────────────
                _fill = place_market_order(
                    instrument=instrument,
                    units=_units,
                    direction=_snipe_dir.lower(),
                    stop_loss=str(_sl_price),
                    take_profit=str(_tp_price),
                    confluence_score=scout_context.get("confluence_score"),
                    cycle_id=_cycle_id,
                )
                _elapsed = time.time() - _snipe_start

                if _fill.get("status") == "error":
                    _err_msg = _fill.get("error", "OANDA API error")
                    logger.error("⚡ [SNIPE DIRECT] %s order ERROR: %s", instrument, _err_msg)
                    _fr_snipe("SNIPE_ORDER_ERROR", status="error", error=_err_msg)
                    cycle_result["status"] = "error"
                    cycle_result["error"] = f"Snipe direct execution error: {_err_msg}"
                elif not _fill.get("trade_id"):
                    _rej_reason = _fill.get("reject_reason") or _fill.get("full_response", {}).get("orderRejectTransaction", {}).get("rejectReason", "OANDA rejected order")
                    logger.error("⚡ [SNIPE DIRECT] %s order REJECTED: %s | SL=%.5f TP=%.5f dir=%s",
                                 instrument, _rej_reason, _sl_price, _tp_price, _snipe_dir)
                    _fr_snipe("SNIPE_ORDER_REJECTED", status="rejected", reason=str(_rej_reason),
                              sl=_sl_price, tp=_tp_price, direction=_snipe_dir)
                    cycle_result["status"] = "error"
                    cycle_result["error"] = f"Order rejected: {_rej_reason}"
                elif _fill.get("trade_id"):
                    # ── Recalculate SL/TP from ACTUAL fill price ─────────────────
                    # BUY fills at ASK (bid + spread). Pre-order SL/TP used bid price
                    # so the TP could end up only 1 pip from fill if spread is wide.
                    # Fix: amend orders to match correct distances from actual entry.
                    _fill_price = float(_fill.get("entry_price") or _current_price)
                    if abs(_fill_price - _current_price) > 0.00005:  # meaningful spread difference
                        _old_tp = _tp_price
                        _old_sl = _sl_price
                        if _snipe_dir == "BUY":
                            _sl_price = round(_fill_price - _sl_dist, _price_precision)
                            _tp_price = round(_fill_price + _tp_dist, _price_precision)
                        else:
                            _sl_price = round(_fill_price + _sl_dist, _price_precision)
                            _tp_price = round(_fill_price - _tp_dist, _price_precision)
                        logger.info(
                            "⚡ [SNIPE DIRECT] %s: fill=%.5f (calc was %.5f, spread=%.1fpips) — "
                            "amending TP %.5f→%.5f SL %.5f→%.5f",
                            instrument, _fill_price, _current_price,
                            abs(_fill_price - _current_price) / _pip,
                            _old_tp, _tp_price, _old_sl, _sl_price
                        )
                        try:
                            from Source.oanda_client import OandaClient as _OC
                            from Source.broker_credentials import BrokerCredentials as _BC
                            _bc_conn = _BC().get_connection(user_id=self.user_id, broker="oanda")
                            with _OC(_bc_conn["api_key"], _bc_conn.get("account_id", "")) as _oc:
                                _amend_result = _oc.set_trade_orders(
                                    trade_id=str(_fill["trade_id"]),
                                    take_profit={"price": str(_tp_price), "timeInForce": "GTC"},
                                    stop_loss={"price": str(_sl_price), "timeInForce": "GTC"},
                                )
                            logger.info("⚡ [SNIPE DIRECT] %s TP/SL amended OK — new TP=%.5f SL=%.5f", instrument, _tp_price, _sl_price)
                        except Exception as _amend_err:
                            logger.warning("⚡ [SNIPE DIRECT] %s TP/SL amend failed: %s — orders left at original prices", instrument, _amend_err)
                    # ─────────────────────────────────────────────────────────────

                    logger.info(
                        "⚡ [SNIPE DIRECT] ✅ %s FILLED trade_id=%s entry=%.5f SL=%.5f TP=%.5f "
                        "units=%d | took %.2fs (vs 136s full pipeline)",
                        instrument, _fill["trade_id"], _fill_price,
                        _sl_price, _tp_price, _units, _elapsed
                    )
                    _fr_snipe("SNIPE_ORDER_FILLED", status="filled",
                              trade_id=str(_fill["trade_id"]), entry_price=_fill_price,
                              sl=_sl_price, tp=_tp_price, units=_units,
                              elapsed_sec=round(_elapsed, 2),
                              rsi=round(_rsi, 1), stoch=round(_stoch, 1),
                              atr_pips=round(_atr_pips, 1),
                              bb_width=round(_bb_width_pips, 1) if _bb_width_pips else None,
                              setup_id=_setup_id)

                    # ── 6a. Record trade in live_trades ──────────────────────────
                    # Without this row the guardian UPDATE on close matches 0 rows,
                    # so P&L, outcome, and exit data are never persisted. The dashboard
                    # then shows stale numbers because it reads from live_trades.
                    try:
                        from datetime import timezone as _tz2
                        _lt_now = datetime.now(_tz2.utc).isoformat()
                        _lt_c = get_trading_forex()
                        # 2026-04-23: distinguish kronos path snipes from validator snipes
                        # so UI can show 🔮 vs 🎯. Entry_type used by dashboard icon logic.
                        # Try scout_context first; fall back to DB lookup by watch_id
                        # because scout's path doesn't always include suggestion_type in
                        # the POST payload (observed trade 9697 2026-04-23).
                        _sugg_in_ctx = (scout_context or {}).get("suggestion_type")
                        _entry_type_col = "snipe_direct"
                        if _sugg_in_ctx == "kronos_path_snipe":
                            _entry_type_col = "kronos_snipe"
                        else:
                            _wid_for_et = (scout_context or {}).get("watch_id") or (scout_context or {}).get("_watch_id")
                            if _wid_for_et:
                                try:
                                    _et_row = _lt_c.execute(
                                        "SELECT suggestion_type FROM watch_suggestions WHERE id=?",
                                        (_wid_for_et,)
                                    ).fetchone()
                                    if _et_row and _et_row[0] == "kronos_path_snipe":
                                        _entry_type_col = "kronos_snipe"
                                except Exception:
                                    pass
                        _lt_c.execute("""
                            INSERT OR IGNORE INTO live_trades (
                                id, source, oanda_trade_id, pair, timeframe, setup, base_setup,
                                direction, entry_time, entry_price, sl_price, tp_price,
                                status, user_id, units, cycle_id, entry_type,
                                fan_state, fan_direction, story_score, story_entry_type,
                                bb_width, bb_expanding, bb_upper, bb_lower, bb_mid,
                                rsi, atr, stoch_k, confidence, finding_id
                            ) VALUES (
                                ?, 'snipe_direct', ?, ?, 'M15', ?, ?,
                                ?, ?, ?, ?, ?,
                                'open', ?, ?, ?, ?,
                                ?, ?, ?, ?,
                                ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?
                            )
                        """, (
                            str(_fill["trade_id"]),
                            str(_fill["trade_id"]),
                            instrument,
                            _setup_id,
                            _setup_id,
                            _snipe_dir.lower(),
                            _lt_now,
                            _fill_price,
                            _sl_price,
                            _tp_price,
                            getattr(self, 'user_id', None),
                            _units,
                            _cycle_id,
                            _entry_type_col,
                            scout_context.get('fan_state', '') if scout_context else '',
                            scout_context.get('fan_direction', '') if scout_context else '',
                            scout_context.get('opportunity_score', scout_context.get('story_score', 0)) if scout_context else 0,
                            scout_context.get('entry_type', '') if scout_context else '',
                            # Indicator snapshot (2026-03-26 deep audit fix)
                            (_bb_width_raw if _bb_width_pips is not None else None),
                            (1 if _bb_expanding_live else (0 if _bb_expanding_live is not None else None)),
                            _bb_upper_val,
                            _bb_lower_val,
                            _bb_mid_val,
                            round(_rsi, 1),
                            _atr,
                            round(_stoch, 1),
                            scout_context.get('confluence_score', 0) if scout_context else 0,
                            scout_context.get('finding_id') if scout_context else None,
                        ))
                        logger.info("[LT] Created live_trades row for %s trade %s",
                                    instrument, _fill["trade_id"])
                        # ── Origin tracking (2026-04-22) — populate metadata JSON ──
                        # Enables post-hoc audit of which watch fired this trade and
                        # what the validator thesis was at watch creation. Previously
                        # live_trades.metadata was always '{}' for snipe_direct.
                        try:
                            import json as _mj
                            _origin = {
                                "watch_id": _watch_id,
                                "entry_type": "snipe_direct",
                                "suggestion_type": (scout_context or {}).get("suggestion_type"),
                                "triggered_by": (scout_context or {}).get("triggered_by"),
                                "watch_created_at": (scout_context or {}).get("watch_created_at"),
                                "original_sniper_score": _snipe_score,
                                "current_sniper_score": (scout_context or {}).get("live_sniper_buy" if _snipe_dir == "BUY" else "live_sniper_sell"),
                            }
                            _lt_c.execute(
                                "UPDATE live_trades SET metadata=? WHERE id=?",
                                (_mj.dumps({k: v for k, v in _origin.items() if v is not None}),
                                 str(_fill["trade_id"])),
                            )
                        except Exception as _meta_err:
                            logger.debug("[LT] metadata update failed for %s: %s",
                                         _fill.get("trade_id"), _meta_err)
                    except Exception as _lt_ins_err:
                        logger.warning("[LT] Failed to create live_trades for %s: %s",
                                       instrument, _lt_ins_err)
                    # ─────────────────────────────────────────────────────────────

                    # Write trade_cycle_id + _snipe_filled flag back to watch
                    # trade_cycle_id → stops re-fire while trade is open (cleared on close by guardian/monitor)
                    # _snipe_filled in context → routes re-entry to validation instead of direct exec
                    # 2026-04-06: Was failing silently on 70/71 trades due to DB lock contention.
                    # Raw sqlite3 connect with 5s timeout lost to DELETE journal mode contention.
                    # Fixed: use db_connection.get_db() with proper timeout + 3 retries.
                    if _watch_id:
                        _link_ok = False
                        for _retry in range(3):
                            try:
                                import json as _wjson
                                from db_connection import get_db as _get_watch_db
                                with _get_watch_db(timeout=10) as _wc:
                                    _wrow = _wc.execute("SELECT context FROM watch_suggestions WHERE id=?",
                                                        (_watch_id,)).fetchone()
                                    _wctx2 = {}
                                    try: _wctx2 = _wjson.loads(_wrow[0] or '{}') if _wrow else {}
                                    except Exception as e: logger.warning("[CYCLE] Failed to parse watch context: %s", e)
                                    _wctx2["_snipe_filled"] = True
                                    _wctx2["_snipe_fill_trade_id"] = str(_fill["trade_id"])
                                    _wc.execute(
                                        "UPDATE watch_suggestions SET trade_cycle_id=?, status='triggered', context=? "
                                        "WHERE id=?",
                                        (str(_fill["trade_id"]), _wjson.dumps(_wctx2), _watch_id)
                                    )
                                    _wc.commit()
                                logger.info("⚡ [SNIPE DIRECT] 🔗 Watch #%s → trade %s (re-fire suppressed; re-entry will validate)",
                                            _watch_id, _fill["trade_id"])
                                _link_ok = True
                                break
                            except Exception as _we:
                                if _retry < 2:
                                    import time as _wt
                                    _wt.sleep(0.5)
                                    logger.debug("Retry %d/3 writing trade_cycle_id for watch %s: %s", _retry+1, _watch_id, _we)
                                else:
                                    logger.error("FAILED to link watch #%s → trade %s after 3 retries: %s",
                                                 _watch_id, _fill["trade_id"], _we)
                        if not _link_ok:
                            # Flight recorder audit trail for the broken link
                            if flight:
                                flight.record(FlightStage.GUARDIAN_ACTION, pair=instrument,
                                              trade_id=str(_fill["trade_id"]), data={
                                    "action": "watch_link_failed",
                                    "watch_id": _watch_id,
                                }, status="error", note=f"Watch #{_watch_id} → trade link FAILED")

                    cycle_result["status"] = "complete"
                    cycle_result["execution"] = _fill
                    cycle_result["steps_completed"].append("execution")
                    phase_timings["snipe_direct"] = _elapsed
                # (error/reject cases handled above in the if-elif-elif chain)

            except Exception as _sde:
                logger.error("⚡ [SNIPE DIRECT] %s exception: %s", instrument, _sde, exc_info=True)
                _fr_snipe("SNIPE_EXCEPTION", status="error", error=str(_sde))
                cycle_result["status"] = "error"
                cycle_result["error"] = str(_sde)

            # Always return here — never fall through to the full pipeline
            cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
            cycle_result["phase_timings"] = phase_timings
            return cycle_result
        # ── END SNIPE DIRECT ───────────────────────────────────────────────

        # Step 1: Pre-check — always ready (orchestrator decides later with full data)
        phase_start = time.time()
        phase_elapsed = time.time() - phase_start
        phase_timings["pre_check"] = phase_elapsed
        _log_phase("cycle_orchestrator", f"Pre-check: starting cycle for {instrument}", phase_elapsed)
        cycle_result["readiness"] = {"ready": True}
        cycle_result["steps_completed"].append("pre_check")

        # Step 2: Create cycle task for audit trail
        task_id = None
        try:
            team_status = self._team.get_team_status()
            workspace_ids = team_status.get("workspace_ids", {})
            # Use single workspace_id (new) or fall back to _parent (legacy)
            parent_ws = workspace_ids.get("_workspace", workspace_ids.get("_parent", 0))
            task_id = self._protocol.create_cycle_task(
                workspace_id=parent_ws,
                instrument=instrument,
                timeframe=timeframe,
            )
            cycle_result["task_id"] = task_id
        except Exception as exc:
            logger.warning("Failed to create cycle task: %s (continuing)", exc)

        # Step 3: Data collection via LLM agents
        phase_start = time.time()
        candles_by_tf: Dict[str, Any] = {}
        intelligence_data: Dict[str, Any] = {}
        account_summary: Dict[str, Any] = {}
        pricing_data: Dict[str, Any] = {}
        instrument_specs: Dict[str, Any] = {}

        try:
            # 3a: oanda_data — direct MCP calls, wrapped in process-wide candle
            # cache (5-min TTL). Cache hit when same pair/TF was fetched within
            # the last 5 min by ANY caller (validator, watch_manager, kronos).
            try:
                from candle_cache import get_cached_candles as _gcc_tc
            except ImportError:
                from Source.candle_cache import get_cached_candles as _gcc_tc

            def _fetch_tc(tf, _inst=instrument):
                _r = _swarm_execute_tool("oanda_data", "get_candles", instrument=_inst, granularity=tf, count=250)
                _cr_inner = _r.get("tool_result", _r)
                return _cr_inner.get("result", _cr_inner).get("candles", _cr_inner.get("candles", []))

            candles_by_tf["M15"] = _gcc_tc(lambda: _fetch_tc("M15"), instrument, "M15", 250)
            candles_by_tf["H1"] = _gcc_tc(lambda: _fetch_tc("H1"), instrument, "H1", 250)
            candles_by_tf["H4"] = _gcc_tc(lambda: _fetch_tc("H4"), instrument, "H4", 250)

            acct_result = _swarm_execute_tool("oanda_data", "get_account_summary")
            ar = acct_result.get("tool_result", acct_result)
            account_summary = ar.get("result", ar).get("account", ar.get("account", ar))

            price_result = _swarm_execute_tool("oanda_data", "get_pricing", instruments=instrument)
            pr = price_result.get("tool_result", price_result)
            pricing_data = pr.get("result", pr)

            for tf, data in candles_by_tf.items():
                logger.info("[CANDLES] %s: %d candles", tf, len(data) if isinstance(data, list) else 0)

            self._post_result(
                task_id, "oanda_data", MessageType.DATA_DELIVERY,
                f"oanda_data collected data for {instrument}: "
                f"M15={len(candles_by_tf.get('M15', []))} H1={len(candles_by_tf.get('H1', []))} "
                f"H4={len(candles_by_tf.get('H4', []))} candles, "
                f"balance={account_summary.get('balance', '?')}",
                {
                    "instrument": instrument,
                    "candles": {tf: len(d) if isinstance(d, list) else 0 for tf, d in candles_by_tf.items()},
                    "account": account_summary if isinstance(account_summary, dict) else {},
                    "pricing": pricing_data if isinstance(pricing_data, dict) else {},
                },
            )

            phase_elapsed = time.time() - phase_start
            phase_timings["data_collection"] = phase_elapsed
            _report_agent_performance("oanda_data", True, phase_elapsed)
            _log_phase("oanda_data", f"Collected {instrument} market data via MCP", phase_elapsed)

            if flight:
                flight.record(FlightStage.DATA_OANDA, pair=instrument, cycle_id=_cycle_id, data={
                    "m15_candles": len(candles_by_tf.get('M15', [])) if isinstance(candles_by_tf.get('M15'), list) else 0,
                    "h1_candles": len(candles_by_tf.get('H1', [])) if isinstance(candles_by_tf.get('H1'), list) else 0,
                    "h4_candles": len(candles_by_tf.get('H4', [])) if isinstance(candles_by_tf.get('H4'), list) else 0,
                    "balance": account_summary.get('balance') if isinstance(account_summary, dict) else None,
                    "open_trades": account_summary.get('openTradeCount', 0) if isinstance(account_summary, dict) else 0,
                }, duration_ms=phase_elapsed * 1000)

            # 3b: Intelligence — read cached data, then LLM agent synthesizes and delivers
            # Step 1: Read pre-cached Wolfram/News/Weather data (fast, no API calls)
            # Step 2: Give cached data to Intelligence Agent LLM to analyze and summarize
            # Step 3: Agent delivers report to Orchestrator through normal communication
            intel_start = time.time()
            intel_tool_calls = []
            intel_report = {}
            try:
                try:
                    from agents.wrappers import gather_intelligence
                except ImportError:
                    from Source.agents.wrappers import gather_intelligence
                intel_report = gather_intelligence(
                    instrument=instrument,
                    task_id=task_id,
                    cycle_id=cycle_result.get("cycle_id"),
                    cache_only=True,  # Read from pre-cached cron data only — no MCP calls
                )
                logger.info("Intelligence gathered for %s in %.2fs — macro keys: %s",
                           instrument, time.time() - intel_start, list(intel_report.get("macro", {}).keys()))
            except Exception as intel_exc:
                logger.warning("Intelligence gather failed: %s", intel_exc, exc_info=True)
                intel_report = {}

            # Build a data summary for the intelligence agent to analyze
            _macro = intel_report.get("macro", {}) if intel_report else {}
            _news = intel_report.get("news", {}) if intel_report else {}
            _weather = intel_report.get("weather", {}) if intel_report else {}
            _stats = intel_report.get("statistics", {}) if intel_report else {}
            _briefing = intel_report.get("agent_briefing", "") if intel_report else ""

            # Parse instrument for context
            parts = instrument.split("_")
            base_ccy = parts[0] if len(parts) == 2 else instrument[:3]
            quote_ccy = parts[1] if len(parts) == 2 else instrument[3:]

            # Format cached data as a readable briefing for the LLM agent
            cached_data_summary = f"## Pre-Cached Intelligence Data for {instrument} ({base_ccy}/{quote_ccy})\n\n"
            cached_data_summary += "### Macro Data (from Wolfram cache)\n"
            if _macro.get("base_currency_rate") is not None:
                cached_data_summary += f"- {base_ccy} interest rate: {_macro.get('base_currency_rate', 'N/A')}%\n"
                cached_data_summary += f"- {quote_ccy} interest rate: {_macro.get('quote_currency_rate', 'N/A')}%\n"
                cached_data_summary += f"- Rate differential: {_macro.get('rate_differential', 'N/A')}%\n"
            if _macro.get("pair_current_price"):
                cached_data_summary += f"- Current price: {_macro.get('pair_current_price')}\n"
                cached_data_summary += f"- 1yr range: {_macro.get('pair_1yr_min', '?')} - {_macro.get('pair_1yr_max', '?')}\n"
                cached_data_summary += f"- Range position: {_macro.get('pair_range_position', 'unknown')}\n"
            if _macro.get("oil_price"):
                cached_data_summary += f"- Oil price: ${_macro.get('oil_price')}/bbl\n"

            cached_data_summary += "\n### News Data (from cache)\n"
            if isinstance(_news, dict):
                articles = _news.get("articles", [])
                cached_data_summary += f"- {len(articles)} articles analyzed\n"
                for i, art in enumerate(articles[:5], 1):
                    if isinstance(art, dict):
                        cached_data_summary += f"  {i}. [{art.get('source', '?')}] {art.get('title', '?')}\n"

            cached_data_summary += "\n### Weather Data (from cache)\n"
            if isinstance(_weather, dict) and _weather.get("check_weather"):
                cached_data_summary += f"- Severity: {_weather.get('severity', 0)}/10\n"
                cached_data_summary += f"- Status: {_weather.get('status', 'unknown')}\n"
            else:
                cached_data_summary += "- No weather impact for this pair\n"

            if _briefing:
                cached_data_summary += f"\n### Pre-Built Briefing\n{_briefing}\n"

            # Try AI-synthesized briefing first (from intelligence_agent_prep 3x/day refresh)
            _ai_briefing = None
            try:
                from intelligence_store import IntelligenceStore
                _ai_store = IntelligenceStore()
                _ai_cached = _ai_store.get_cached(f"briefing:ai:{instrument}")
                _ai_store.close()
                if _ai_cached:
                    import json as _json
                    _ai_data = _json.loads(_ai_cached) if isinstance(_ai_cached, str) else _ai_cached
                    _ai_briefing = _ai_data.get("briefing") if isinstance(_ai_data, dict) else _ai_cached
            except Exception as _ai_exc:
                logger.debug("AI briefing cache miss for %s: %s", instrument, _ai_exc)

            # Priority: AI briefing > agent_briefing > mechanical summary
            intel_response = _ai_briefing if _ai_briefing else (_briefing if _briefing else cached_data_summary)
            intel_tool_calls = []
            
            intel_elapsed = time.time() - intel_start
            phase_timings["intelligence"] = intel_elapsed
            _report_agent_performance("intelligence", True, intel_elapsed)

            # Build intelligence_data from cached data
            # Derive verdict/bias from macro data (rate differential + range position)
            _net_sent = 0.0
            _recommendation = "NEUTRAL"
            _rate_diff = _macro.get("rate_differential", 0) or 0
            _range_pos = _macro.get("pair_range_position", "")
            _news_sent = 0.0
            if isinstance(_news, dict):
                _news_sent = _news.get("base_sentiment", _news.get("sentiment", 0)) or 0

            # Simple rule-based bias from macro data
            _intel_bias = "neutral"
            _intel_verdict = "NEUTRAL"
            _intel_confidence = 0.35
            _bias_signals = 0
            if _rate_diff > 1.0: _bias_signals += 1  # rate diff favors base
            elif _rate_diff < -1.0: _bias_signals -= 1
            if _range_pos == "near_bottom": _bias_signals += 1  # mean reversion potential
            elif _range_pos == "near_top": _bias_signals -= 1
            if _news_sent > 0.3: _bias_signals += 1
            elif _news_sent < -0.3: _bias_signals -= 1

            if _bias_signals >= 2:
                _intel_bias = "buy"
                _intel_verdict = "BULLISH"
                _intel_confidence = 0.55
            elif _bias_signals <= -2:
                _intel_bias = "sell"
                _intel_verdict = "BEARISH"
                _intel_confidence = 0.55
            elif _bias_signals == 1:
                _intel_bias = "buy"
                _intel_verdict = "SLIGHTLY_BULLISH"
                _intel_confidence = 0.40
            elif _bias_signals == -1:
                _intel_bias = "sell"
                _intel_verdict = "SLIGHTLY_BEARISH"
                _intel_confidence = 0.40
            intelligence_data = {
                "agent_briefing": intel_response,
                "overall_sentiment": _news_sent,
                "risk_events_upcoming": [],
                "recommendation": _recommendation,
                "sources_available": ["wolfram_cache", "news_cache", "weather_cache"],
                "tool_calls": intel_tool_calls,
                "news": _news if isinstance(_news, dict) else {},
                "weather": _weather if isinstance(_weather, dict) else {},
                "wolfram": {
                    "queries": intel_report.get("_wolfram_queries", []),
                    "count": len(intel_report.get("_wolfram_queries", [])),
                    "rate_differential": _macro.get("rate_differential"),
                    "base_rate": _macro.get("base_currency_rate"),
                    "quote_rate": _macro.get("quote_currency_rate"),
                },
                "macro": _macro,
                "statistics": _stats if isinstance(_stats, dict) else {},
                "verdict": _intel_verdict,
                "bias": _intel_bias,
                "confidence": _intel_confidence,
                "summary": intel_report.get("summary", ""),
            }

            logger.info("Intelligence verdict: %s bias=%s conf=%.2f (rule-based from macro data)",
                       _intel_verdict, _intel_bias, _intel_confidence)
            _log_phase("intelligence",
                f"Intelligence for {instrument}: {_intel_verdict} bias={_intel_bias} "
                f"conf={_intel_confidence:.0%} | rates={'yes' if _macro.get('base_currency_rate') else 'no'} "
                f"| briefing={'yes' if _ai_briefing else 'no'} ({intel_elapsed:.1f}s)",
                intel_elapsed)

            if flight:
                flight.record(FlightStage.DATA_INTELLIGENCE, pair=instrument, cycle_id=_cycle_id, data={
                    "verdict": intelligence_data.get("verdict", "PENDING"),
                    "bias": intelligence_data.get("bias", "unknown"),
                    "confidence": intelligence_data.get("confidence", 0),
                    "recommendation": intelligence_data.get("recommendation", "NEUTRAL"),
                    "sources": intelligence_data.get("sources_available", []),
                }, duration_ms=intel_elapsed * 1000)

            self._post_result(
                task_id, "intelligence", MessageType.DATA_DELIVERY,
                f"Intelligence agent briefing for {instrument}: {len(intel_tool_calls)} tool calls",
                intelligence_data,
            )

            cycle_result["data_collection"] = {
                "candles_by_tf": {
                    tf: {"count": len(data) if isinstance(data, list) else 0}
                    for tf, data in candles_by_tf.items()
                },
                "intelligence": intelligence_data,
                "account": account_summary if isinstance(account_summary, dict) else {},
                "pricing": pricing_data if isinstance(pricing_data, dict) else {},
            }
            cycle_result["steps_completed"].append("data_collection")
            # Store intelligence at top level for dashboard/card hydration
            cycle_result["intelligence_data"] = intelligence_data

        except Exception as exc:
            phase_elapsed = time.time() - phase_start
            phase_timings["data_collection"] = phase_elapsed
            _report_agent_performance("oanda_data", False, phase_elapsed)
            _log_phase("oanda_data", f"Data collection failed: {exc}", phase_elapsed, status="error")
            logger.error("Cycle #%d data collection failed: %s", cycle_num, exc)
            self._post_error(task_id, "data_collection", str(exc))
            # For user_watch/user_chat cycles, OANDA data is optional — Tim's submitted chart
            # IS the signal. A 502 or other OANDA failure must not kill the validator.
            # All vars (candles_by_tf, account_summary, etc.) are already empty dicts — just continue.
            _is_snipe_cycle = (scout_context or {}).get("triggered_by") in ("snipe", "cascade_reentry")
            if _is_snipe_cycle:
                logger.warning(
                    "Cycle #%d: OANDA data collection failed for user_watch — continuing with "
                    "empty candles so validator can run on submitted chart. Error: %s",
                    cycle_num, exc,
                )
                cycle_result["steps_completed"].append("data_collection_degraded")
                cycle_result["oanda_degraded"] = True
            else:
                cycle_result["error"] = f"Data collection failed: {exc}"
                cycle_result["aborted"] = True
                cycle_result["abort_reason"] = ["Data collection failure"]
                return cycle_result

        # Step 4: Technical analysis — Python computes, LLM interprets
        # 1. run_full_analysis() computes all indicators/patterns (fast, ~0.2s)
        # 2. LLM agent reads the results and applies reasoning (sniper strategy,
        #    contradiction detection, regime-appropriate signal selection)
        phase_start = time.time()

        # Resolve user ID early so TA phase can use risk overrides
        _cycle_user_id = None
        _uconn = None
        try:
            import sqlite3 as _sql3
            _udb = Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "core.db"
            _uconn = _sql3.connect(str(_udb), isolation_level=None)
            _ures = _uconn.execute("SELECT user_id FROM broker_credentials LIMIT 1").fetchone()
            if _ures:
                _cycle_user_id = _ures[0]
        except Exception:
            pass
        finally:
            if _uconn:
                _uconn.close()

        analysis_results: Dict[str, Any] = {}
        try:
            # Extract candle lists from fetch results
            candles_for_ta = {}
            if isinstance(candles_by_tf, dict):
                for tf, tf_data in candles_by_tf.items():
                    if isinstance(tf_data, list):
                        candles_for_ta[tf] = tf_data
                    elif isinstance(tf_data, dict):
                        candles_for_ta[tf] = tf_data.get("candles", [])

            news_score = 0.0
            news = intelligence_data.get("news", {})
            if isinstance(news, dict):
                raw_sentiment = news.get("sentiment", 0.0)
                if raw_sentiment:
                    news_score = abs(float(raw_sentiment))
            if news_score == 0.0:
                overall = intelligence_data.get("overall_sentiment", 0.0)
                if overall:
                    news_score = abs(float(overall))

            _swarm_send_message(
                "cycle_orchestrator", "technical_analyst",
                f"Analyze {instrument} with {len(candles_for_ta)} timeframes",
            )

            # --- Phase A: Sniper V4 as PRIMARY computation ---
            from Source.agents.wrappers import _sanitize_for_json, compute_sniper_score
            # Use user-aware risk limits (includes slider overrides from dashboard)
            _early_risk = _get_risk_limits(user_id=_cycle_user_id)
            sniper_threshold = int(_early_risk.get("sniper_threshold", 12))
            sniper_result = compute_sniper_score(
                candles_by_tf=candles_for_ta,
                instrument=instrument,
                sniper_threshold=sniper_threshold,
            )

            # Sniper is now the primary analysis — it computes ALL indicators
            # using the SAME pipeline that backtested at 90%+ win rate
            
            # --- EMA Market Picture ---
            # Compute full EMA narrative + RSI/Stoch/BB market picture
            ema_result = {}
            market_picture = {}
            try:
                try:
                    from backtester.ema_separation import scan_ema_signals, generate_market_picture
                except ImportError:
                    try:
                        from Source.backtester.ema_separation import scan_ema_signals, generate_market_picture
                    except ImportError:
                        from ..backtester.ema_separation import scan_ema_signals, generate_market_picture
                
                h1_candles = candles_for_ta.get("H1", [])
                m15_candles = candles_for_ta.get("M15", [])
                if h1_candles:
                    # Normalize candle format for EMA module
                    _h1 = []
                    for c in h1_candles:
                        mid = c.get("mid", {})
                        _h1.append({
                            "time": c.get("time", ""),
                            "open": mid.get("o", c.get("open", 0)),
                            "high": mid.get("h", c.get("high", 0)),
                            "low": mid.get("l", c.get("low", 0)),
                            "close": mid.get("c", c.get("close", 0)),
                        })
                    _m15 = []
                    for c in (m15_candles or []):
                        mid = c.get("mid", {})
                        _m15.append({
                            "time": c.get("time", ""),
                            "open": mid.get("o", c.get("open", 0)),
                            "high": mid.get("h", c.get("high", 0)),
                            "low": mid.get("l", c.get("low", 0)),
                            "close": mid.get("c", c.get("close", 0)),
                        })
                    # M15 is the trading timeframe — primary for EMA/BB analysis
                    # H1 is supplemental (higher-resolution velocity comparison)
                    market_picture = generate_market_picture(instrument, _m15 if _m15 else _h1, _h1 if _m15 else None)
                    _ema_sub  = market_picture.get('ema', {})
                    _bb_sub   = market_picture.get('bollinger', {})
                    _rsi_sub  = market_picture.get('rsi', {})
                    _stoch_sub = market_picture.get('stochastic', {})
                    # Convert gap_price_100 (%) to pips for confluence scorer
                    _gap_pct  = float(_ema_sub.get('gap_price_100', 0) or 0)
                    _cur_emas = _ema_sub.get('current_emas', {})
                    _e100_price = float(_cur_emas.get('ema100', 0) or 0)
                    _pip_size = 0.01 if 'JPY' in instrument else 0.0001
                    _e100_pips = abs(_gap_pct / 100 * _e100_price) / _pip_size if _e100_price else 0
                    ema_result = {
                        **_ema_sub,
                        # BB fields — merge without duplicate prefix
                        **{k: v for k, v in _bb_sub.items()},
                        "rsi": _rsi_sub.get('value', 50),
                        "stoch_k": _stoch_sub.get('k', 50),
                        "stoch_d": _stoch_sub.get('d', 50),
                        # Derived pip distance for Gate2/3
                        "e100_distance_pips": round(_e100_pips, 1),
                    }
            except Exception as exc:
                logger.warning("EMA market picture failed: %s", exc)
                ema_result = {"error": str(exc)}

            # Compute thesis measurements from the indicator-loaded df scout uses.
            # Single source of truth in thesis_measurements.py so manual cycles and
            # scout-driven cycles produce identical TA + validator inputs.
            _thesis = {}
            try:
                _df = sniper_result.get("df") if isinstance(sniper_result, dict) else None
                if _df is not None and len(_df) > 0:
                    _pip_size = 0.01 if 'JPY' in instrument else 0.0001
                    _fan_state = (ema_result or {}).get("fan_state", "unknown") if isinstance(ema_result, dict) else "unknown"
                    _fan_direction = (ema_result or {}).get("fan_direction", "neutral") if isinstance(ema_result, dict) else "neutral"
                    _thesis = compute_thesis_measurements(
                        _df, _pip_size,
                        fan_state=_fan_state,
                        fan_direction=_fan_direction,
                    ) or {}
            except Exception as exc:
                logger.warning("thesis_measurements compute failed: %s", exc)

            raw_analysis = {
                "sniper_score": sniper_result,
                "ema_signals": ema_result,
                "market_picture": market_picture,
                "instrument": instrument,
            }

            python_elapsed = time.time() - phase_start
            logger.info("[TIMING] TA sniper computation: %.2fs (buy=%d sell=%d)",
                       python_elapsed,
                       sniper_result.get("buy_score", 0),
                       sniper_result.get("sell_score", 0))

            if flight:
                flight.record(FlightStage.TA_COMPUTE, pair=instrument, cycle_id=_cycle_id, data={
                    "buy_score": sniper_result.get("buy_score", 0),
                    "sell_score": sniper_result.get("sell_score", 0),
                    "fan_state": ema_result.get("fan_state", "unknown") if isinstance(ema_result, dict) else "error",
                    "fan_direction": ema_result.get("fan_direction", "unknown") if isinstance(ema_result, dict) else "error",
                    "trend_health": ema_result.get("trend_health", 0) if isinstance(ema_result, dict) else 0,
                    "reversal_risk": ema_result.get("reversal_risk", "unknown") if isinstance(ema_result, dict) else "error",
                    "regime": regime if 'regime' in dir() else "unknown",
                }, duration_ms=python_elapsed * 1000, note=f"sniper {sniper_result.get('buy_score',0)}B/{sniper_result.get('sell_score',0)}S")

            # --- Phase B: LLM interpretation of sniper output ---
            indicators = sniper_result.get("indicators", {})
            detected_patterns = sniper_result.get("detected_patterns", [])
            divergence = sniper_result.get("divergence", {})

            # Run setup classifier if no scout context provided it
            if not scout_context or not scout_context.get('market_snapshot', {}).get('classified_setups'):
                try:
                    from setup_classifier import classify_setups
                    from backtester.chart_patterns import detect_all_chart_patterns
                    
                    # Get chart patterns from sniper result
                    _chart_pats = sniper_result.get("chart_patterns", [])
                    if isinstance(_chart_pats, dict):
                        _chart_pats = _chart_pats.get("patterns", [])
                    
                    _candle_dict = {}
                    for p in detected_patterns:
                        _candle_dict[p.lower().replace(' ', '_')] = True
                    
                    _regime_for_cls = _detect_regime(sniper_result)
                    _classified = classify_setups(
                        indicators=indicators.get('core', indicators),
                        candle_patterns=_candle_dict,
                        chart_patterns=_chart_pats,
                        regime=_regime_for_cls,
                    )
                    if _classified:
                        # Store in scout_context so it flows to prompts
                        if not scout_context:
                            scout_context = {}
                        if 'market_snapshot' not in scout_context:
                            scout_context['market_snapshot'] = {}
                        scout_context['market_snapshot']['classified_setups'] = [
                            {'setup': s['setup'], 'name': s['name'], 'direction': s['direction'],
                             'confidence': s['confidence'], 'regime_valid': s['regime_valid'],
                             'candle_confirmation': s.get('candle_confirmation', False)}
                            for s in _classified[:5]
                        ]
                        logger.info("Classified %d S1-S20 setups for %s (cycle-computed)", len(_classified), instrument)
                except Exception as e:
                    logger.debug("Setup classifier error in cycle: %s", e)

            # Recent price action (last 5 H1 candles)
            h1_candles = candles_for_ta.get("H1", [])
            recent_candles = []
            for c in (h1_candles[-5:] if h1_candles else []):
                mid = c.get("mid", {})
                recent_candles.append({
                    "time": c.get("time", "")[:19],
                    "O": mid.get("o", c.get("open")),
                    "H": mid.get("h", c.get("high")),
                    "L": mid.get("l", c.get("low")),
                    "C": mid.get("c", c.get("close")),
                    "vol": c.get("volume"),
                })

            # Intelligence context
            intel_summary = (
                f"Sentiment: {intelligence_data.get('overall_sentiment', 0)}, "
                f"Recommendation: {intelligence_data.get('recommendation', 'NEUTRAL')}, "
                f"News Score: {news_score:.2f}"
            )
            risk_events = intelligence_data.get("risk_events_upcoming", [])
            if risk_events:
                intel_summary += f", Risk Events: {json.dumps(risk_events[:3])}"

            # Determine regime from ADX
            adx_val = indicators.get("adx", 25)
            if adx_val > 25:
                regime = "trending"
            elif adx_val < 20:
                regime = "ranging"
            else:
                regime = "mixed"

            # Build confluence narrative from market picture if available
            _confluence_narr = ""
            if market_picture and market_picture.get("confluence_narrative"):
                _confluence_narr = market_picture["confluence_narrative"]

            # E100 pattern info
            _e100_pat = ema_result.get('e100_candle_pattern') if ema_result else None
            _e100_text = f"⚡ {_e100_pat['name']} ({_e100_pat['direction']}) at E100" if _e100_pat else "None"

            # ══════════════════════════════════════════════════════════════
            # V4 TECHNICAL ANALYST — Describe the market + generate chart
            #
            # TA does NOT decide direction or trade quality.
            # TA describes what it sees and generates a chart image.
            # The Validator is the brain — TA is the eyes.
            # ══════════════════════════════════════════════════════════════

            # ── Generate chart image for validator ──
            _v4_chart_path = None
            try:
                import pandas as _pd
                from chart_generator import generate_chart

                # chart_generator computes EMAs + BBs from raw candles inline
                _m15_raw = candles_for_ta.get("M15", [])
                if _m15_raw and len(_m15_raw) >= 60:
                    _rows = []
                    for _c in _m15_raw:
                        _mid = _c.get("mid", {})
                        _rows.append({
                            "time": _c.get("time", ""),
                            "open": float(_mid.get("o", _c.get("open", 0))),
                            "high": float(_mid.get("h", _c.get("high", 0))),
                            "low": float(_mid.get("l", _c.get("low", 0))),
                            "close": float(_mid.get("c", _c.get("close", 0))),
                            "volume": int(_c.get("volume", 0)),
                        })
                    _chart_df = _pd.DataFrame(_rows)
                    # ── Load active watch snipe levels for this pair ──
                    _snipe_levels = []
                    try:
                        import sqlite3 as _sqlite3, json as _json_sl
                        _watch_db = str(Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "trading_forex.db")
                        with _sqlite3.connect(_watch_db, isolation_level=None) as _wc:
                            _wrows = _wc.execute(
                                "SELECT context FROM watch_suggestions WHERE instrument=? AND status='watching' ORDER BY id DESC LIMIT 3",
                                (instrument,)
                            ).fetchall()
                        for _wr in _wrows:
                            try:
                                _ctx = _json_sl.loads(_wr[0] or '{}')
                                _pt = _ctx.get("price_target_entry")
                                _dir = _ctx.get("direction", "")
                                if _pt:
                                    _snipe_levels.append({
                                        "price": float(_pt),
                                        "label": f"SNIPE {'BUY' if 'bull' in _dir else 'SELL' if 'bear' in _dir else 'ENTRY'}",
                                        "color": "#00FF7F" if 'bull' in _dir else "#FF4444",
                                    })
                            except Exception: pass
                    except Exception as _sl_err:
                        logger.debug(f"[V4] Snipe level lookup failed: {_sl_err}")
                    # ── Pull Tim's active annotations for this pair ──
                    # These render ON the chart image so the validator SEES them visually,
                    # exactly like the annotated teaching images it's already trained on.
                    _user_annotations = []
                    _chart_watch_id = (scout_context or {}).get("watch_id") or \
                                      (scout_context or {}).get("_watch_id")
                    try:
                        import sqlite3 as _ann_sq
                        _ann_db = _TRADING_FOREX_DB
                        with _ann_sq.connect(_ann_db, timeout=5) as _ann_conn:
                            _ann_conn.row_factory = _ann_sq.Row
                            # Auto-expire annotations older than 48 hours
                            _ann_conn.execute(
                                "UPDATE user_chart_annotations SET active=0 "
                                "WHERE active=1 AND expires_at IS NULL "
                                "AND created_at < datetime('now', '-48 hours')"
                            )
                            _ann_conn.commit()

                            if _chart_watch_id:
                                # Snipe-triggered cycle: prefer annotations scoped to this watch,
                                # fall back to recent pair annotations if none are snipe-scoped.
                                _ann_rows = _ann_conn.execute(
                                    "SELECT annotation_type, price, direction, note, ema_cross, bar_time "
                                    "FROM user_chart_annotations "
                                    "WHERE pair=? AND active=1 "
                                    "  AND (timeframe='M15' OR timeframe IS NULL) "
                                    "  AND (expires_at IS NULL OR expires_at > datetime('now')) "
                                    "  AND created_at > datetime('now', '-48 hours') "
                                    "  AND (snipe_id=? OR snipe_id IS NULL) "
                                    "ORDER BY (snipe_id IS NOT NULL) DESC, created_at DESC LIMIT 15",
                                    (instrument, _chart_watch_id)
                                ).fetchall()
                            else:
                                # Clean scout cycle: no annotations on chart — clean read
                                _ann_rows = []

                            _user_annotations = [dict(r) for r in _ann_rows]
                        if _user_annotations:
                            logger.info("[V4] Loaded %d trader annotations for %s chart (watch_id=%s)",
                                        len(_user_annotations), instrument, _chart_watch_id)
                        else:
                            logger.debug("[V4] No annotations for %s chart (clean cycle or no snipe scope)", instrument)
                    except Exception as _ann_err:
                        logger.debug("Could not load annotations for chart: %s", _ann_err)

                    # 2026-05-11 iter-20d wire-up FIX: run pattern detection ONCE
                    # here, BEFORE chart generation, so fires can be drawn on the
                    # chart image AND reused for the Detected Patterns text section
                    # later (line ~6680). Today's audit revealed pattern labels
                    # were missing from 57% of live charts because pattern_labels
                    # was never passed to generate_chart. Iter 20d testing had
                    # labels on every chart — without them, the model can't apply
                    # the pattern-conflict veto rule.
                    _v4_pattern_fires = []
                    try:
                        from scripts.pattern_detectors import detect_patterns_for_validator
                        _v4_pattern_fires = detect_patterns_for_validator(
                            _m15_raw,
                            fan_direction=str(ema_result.get("fan_direction", "mixed")),
                            phase=int(ema_result.get("cascade_phase", 0) or 0),
                            pair_hint=instrument,
                        )
                    except Exception as _pd_exc:
                        logger.warning("[V4] Pattern detection failed: %s", _pd_exc)
                        _v4_pattern_fires = []

                    _v4_chart_path = generate_chart(
                        pair=instrument, df=_chart_df,
                        snipe_levels=_snipe_levels or None,
                        user_annotations=_user_annotations or None,
                        pattern_labels=_v4_pattern_fires or None,
                    )
                    if _snipe_levels:
                        logger.info(f"[V4] Chart generated with {len(_snipe_levels)} snipe level(s): {[s['price'] for s in _snipe_levels]}")
                    elif _user_annotations:
                        logger.info(f"[V4] Chart generated with {len(_user_annotations)} trader annotation(s)")
                    else:
                        logger.info(f"[V4] Chart generated: {_v4_chart_path}")
                else:
                    logger.warning(f"[V4] Not enough M15 candles ({len(_m15_raw)}) for chart")
            except Exception as _cg_err:
                logger.warning(f"[V4] Chart generation failed: {_cg_err}")

            # ── V4 thesis measurements ──
            _v4_fan_delta = _thesis.get('fan_delta_5bar') or 0
            _v4_fan_delta_20 = _thesis.get('fan_delta_20bar') or 0
            _v4_bb_delta = _thesis.get('bb_delta_5bar') or 0
            _v4_bb_delta_20 = _thesis.get('bb_delta_20bar') or 0
            _v4_bb_expanding = bool(_thesis.get('bb_expanding'))
            _v4_fan_expanding = bool(_thesis.get('fan_expanding'))
            # e100_distance_pips: prefer ema_result's deterministic compute, fall back
            # to thesis value, then scout_context for legacy snipe paths that still
            # populate it.
            _v4_e100_dist = (
                ema_result.get('e100_distance_pips', 0)
                or _thesis.get('e100_dist_pips')
                or (scout_context.get('e100_distance_pips', 0) if scout_context else 0)
            )
            _v4_rsi_recovery = (
                _thesis.get('rsi_recovery_ok')
                if _thesis.get('rsi_recovery_ok') is not None
                else (scout_context.get('rsi_recovery_ok', True) if scout_context else True)
            )
            _v4_alert_type = scout_context.get('alert_type', 'UNKNOWN') if scout_context else 'UNKNOWN'

            # ── E100 pattern info ──
            _e100_text_v4 = f"⚡ {_e100_pat['name']} ({_e100_pat['direction']}) at E100" if _e100_pat else "None"

            # ── Pre-compute delta lines for the TA task (always real values now —
            # no scout-supplied vs unavailable branching) ──
            _fan_delta_line = (
                f"Δ5bar: {_v4_fan_delta:+.5f} | Δ20bar: {_v4_fan_delta_20:+.5f} "
                f"({'expanding' if _v4_fan_expanding else 'not expanding'})\n"
            )
            _bb_delta_line = (
                f"- BB width Δ5bar: {_v4_bb_delta:+.5f} | Δ20bar: {_v4_bb_delta_20:+.5f} "
                f"({'expanding' if _v4_bb_expanding else 'not expanding'})\n"
            )

            # ── Build thesis step checklist for TA to evaluate ──
            _bars_since_cross = ema_result.get("bars_since_crossover")
            _cross_happened = (
                _bars_since_cross is not None and
                isinstance(_bars_since_cross, (int, float)) and
                _bars_since_cross <= 50
            )
            _fan_ordered_str = ema_result.get("fan_ordered", False)
            _e100_role = ema_result.get("ema100_role", "unknown")
            _e100_above = ("support" in str(_e100_role).lower() or ema_result.get("gap_price_100", 0) > 0)

            ta_task = (
                f"You are the Technical Analyst for {instrument} M15.\n"
                f"DESCRIBE what the market is doing. No decisions, no recommendations — pure facts.\n\n"
                f"## Market Data\n\n"
                f"**EMA Structure:**\n"
                f"- Fan: {ema_result.get('fan_direction', '?')} {ema_result.get('fan_state', '?')} "
                f"(ordered: {ema_result.get('fan_ordered', '?')})\n"
                f"- Fan width (E21→E100): {ema_result.get('separation_pct', 0):.4f}% | {_fan_delta_line}"
                f"- Velocity: {ema_result.get('separation_velocity', 0):.6f}%/bar "
                f"({ema_result.get('fan_velocity_trend', '?')})\n"
                f"- E100 role: {_e100_role} | distance: {_v4_e100_dist:.1f} pips | "
                f"gap_price_100: {ema_result.get('gap_price_100', 0):.4f}%\n"
                f"- E100 candle pattern: {_e100_text_v4}\n"
                f"- Bars since last E21/E55 cross: "
                f"{'never or >50 bars' if not _cross_happened else str(int(_bars_since_cross)) + ' bars ago'}\n"
                # 2026-04-27: cascade fields — cross sequence + price-vs-E100 confirmation.
                # Without these the TA reads tightly-clustered EMAs as "tangled" even when
                # there's an active cross sequence. cascade_phase 0..4 summarizes the
                # trend-formation state.
                f"- Bars since last E21/E100 cross (Cross 2): "
                f"{ema_result.get('bars_since_cross2') if ema_result.get('bars_since_cross2') is not None else 'never or >100 bars'}\n"
                f"- Bars since last E55/E100 cross (Cross 3): "
                f"{ema_result.get('bars_since_cross3') if ema_result.get('bars_since_cross3') is not None else 'never or >100 bars'}"
                f"{' (' + str(ema_result.get('cross3_direction')) + ')' if ema_result.get('cross3_direction') else ''}\n"
                f"- Cascade phase: {ema_result.get('cascade_phase', 0)}/4 "
                f"(0=none, 1=cross1 only, 2=+cross2, 3=+cross3 fully ordered, 4=phase 3 confirmed by price)\n"
                f"- Last 10 closes vs E100: {ema_result.get('candles_below_e100', 0)} below / "
                f"{ema_result.get('candles_above_e100', 0)} above. Last close: "
                f"{ema_result.get('last_close_vs_e100', 'unknown')} E100\n"
                f"- E100 rejections (last 20 bars): "
                f"{ema_result.get('e100_rejections_from_below', 0)} from below (E100=resistance), "
                f"{ema_result.get('e100_rejections_from_above', 0)} from above (E100=support)\n"
                f"- Trend Health: {ema_result.get('trend_health', 0)}/100 | "
                f"Reversal Risk: {ema_result.get('reversal_risk', '?')}\n\n"
                f"**Bollinger Bands:**\n"
                f"{_bb_delta_line}"
                f"- BB squeeze: {ema_result.get('bb_squeeze', False)} | "
                f"BB expanding: {ema_result.get('bb_expanding', False)} | "
                f"BB contracting: {ema_result.get('bb_contracting', False)}\n"
                f"- BB position: lower_pen={indicators.get('bb_lower_pen', 0):.4f}, "
                f"upper_pen={indicators.get('bb_upper_pen', 0):.4f}\n\n"
                f"**Momentum:**\n"
                f"- RSI: {indicators.get('rsi', 50):.1f} (slope: {indicators.get('rsi_slope', 0):.2f}) "
                f"{'⚠️ STUCK AT EXTREME' if not _v4_rsi_recovery else ''}\n"
                f"- Stoch K/D: {indicators.get('stoch_k', 50):.1f}/{indicators.get('stoch_d', 50):.1f}\n"
                f"- MACD hist: {indicators.get('macd_histogram', 0):.5f}\n"
                f"- ADX: {adx_val:.1f} → regime={regime}\n\n"
                f"**Patterns & Divergence:**\n"
                f"- Candlestick: {', '.join(detected_patterns) if detected_patterns else 'None'}\n"
                f"- Divergence: {json.dumps({k:v for k,v in divergence.items() if v}, default=str) if any(divergence.values()) else 'None'}\n\n"
                f"**Scout:** {_v4_alert_type} | "
                f"E100 dist={_v4_e100_dist:.1f}p | "
                f"fan_Δ5={_v4_fan_delta:+.5f} | fan_Δ20={_v4_fan_delta_20:+.5f} | "
                f"bb_Δ5={_v4_bb_delta:+.5f} | bb_Δ20={_v4_bb_delta_20:+.5f}\n\n"
                f"## Your Output — Annotated Chart Picture\n\n"
                f"Narrate each section as if talking a trader through what they see on the chart. "
                f"Specific numbers only. Do NOT make directional calls or trade recommendations.\n\n"
                f"Return JSON only — no markdown fences, no preamble:\n"
                f"{{\n"
                f'  "narrative": "2-3 sentence summary of the overall M15 picture in plain terms",\n'
                f'  "ema_state": "Fan open or closed. Direction (bullish=E21>E55>E100, bearish=E100>E55>E21, neutral=tangled). Price X pips above/below E55 and Y pips above/below E100. E21 crossed E55 N bars ago (or no recent cross). Fan separation trend: Δ5 and Δ20 both positive=growing, both negative=shrinking, mixed=stalled.",\n'
                f'  "bb_state": "Expanding, contracting, or squeezing. Price position relative to bands (above upper/walking upper/middle/walking lower/below lower). BB width Δ5 rate. Whether BB aligns with or contradicts the fan.",\n'
                f'  "candle_tests": "How last 3-5 candles interact with E55 and E100: wicks testing these levels, closes above/below them, E100 acting as support or resistance, bouncing or breaking. Include detected candlestick patterns and wick pressure direction.",\n'
                f'  "rsi_state": "RSI value and direction (rising/falling, toward extreme or recovering). Stoch K/D values and cross status. ADX value and slope (rising/flat/falling). Any divergence: price new high/low but RSI not confirming. Whether momentum aligns with or contradicts price.",\n'
                f'  "retracement_status": "Is price in a retracement (pulling back against fan direction)? If yes: how deep (approaching E55 or E100?), where pullback terminated, which EMA held as support/resistance, has recovery started. If no retracement: how many bars has price been moving directionally.",\n'
                f'  "cascade_phase": "Current phase — Phase 2.5: E21 just crossed E55 (fan opening). Phase 3: full fan ordered and expanding. Phase 4: fan peaked/contracting (retracement forming). Phase 5: fan re-accelerating after retracement. Fan separation pct, velocity %/bar, and exhaustion signals if any (velocity declining, fan narrowing, consecutive wicks against direction).",\n'
                f'  "conflicting_signals": ["Concrete conflicts only — e.g. fan expanding but BB contracting, RSI diverging from price, ADX disagrees with fan state"],\n'
                f'  "clarity": "CLEAR|MIXED|UNCLEAR"\n'
                f"}}"
            )

            # NOTE: User chart annotations are intentionally NOT passed to the TA.
            # The TA is a pure data collector — no awareness of the trader's thesis.
            # Annotations are passed directly to the Validator, which is the only agent
            # authorised to reason about the trader's intent and thesis.

            # 2026-04-27: max_tokens raised 1800 → 3000. The 7-field JSON output (narrative,
            # ema_state, bb_state, candle_tests, rsi_state, retracement_status, cascade_phase,
            # conflicting_signals, clarity) on 35B was being truncated mid-output around field
            # #4-5 because 35B is more verbose than 9B was. Truncated JSON → parse failure →
            # empty ta_interpretation → all fields surface as None downstream.
            #
            # Test flag: when gate.skip_ta_prefeed is True, the TA pass is skipped entirely.
            # Validator then runs alone with chart + scout evidence + raw indicators + patterns
            # + intelligence (TA Picture section is naturally omitted because the existing
            # builder at line ~6334 guards on `_v4_ta_full or _v4_ta_narrative` — both empty
            # when ta_interpretation is {}).
            _skip_ta_prefeed = bool(tc_get("gate.skip_ta_prefeed", False))
            if _skip_ta_prefeed:
                logger.info("[TA] %s: skipped (gate.skip_ta_prefeed=True) — validator will reason independently from raw inputs", instrument)
                ta_result = {"response": "", "rounds": 0, "tool_calls": [], "skipped_for_test": True}
            else:
                ta_result = _direct_ta_call(ta_task, max_tokens=3000)

            # Parse LLM response
            ta_response = ta_result.get("response", "")
            logger.debug("[TA_RAW] %s: %s", instrument, ta_response[:300])

            ta_interpretation = {}
            try:
                import re as _re
                # Try code block first (```json ... ```) — use greedy match for full object
                json_match = _re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', ta_response, _re.DOTALL)
                if json_match:
                    ta_interpretation = json.loads(json_match.group(1))
                else:
                    # Fallback: find outermost { ... } using depth tracking
                    brace_start = ta_response.find('{')
                    if brace_start >= 0:
                        depth = 0
                        for i in range(brace_start, len(ta_response)):
                            if ta_response[i] == '{':
                                depth += 1
                            elif ta_response[i] == '}':
                                depth -= 1
                                if depth == 0:
                                    ta_interpretation = json.loads(ta_response[brace_start:i+1])
                                    break
                # Ensure required fields always present
                if ta_interpretation:
                    if "clarity" not in ta_interpretation:
                        # Derive clarity from conflicting_signals count
                        conflicts = ta_interpretation.get("conflicting_signals", [])
                        ta_interpretation["clarity"] = "UNCLEAR" if len(conflicts) > 2 else ("MIXED" if conflicts else "CLEAR")
                    if "narrative" not in ta_interpretation:
                        ta_interpretation["narrative"] = ta_response[:400]
                else:
                    # 2026-04-27: salvage parser — when complete JSON parse fails (most often
                    # because the model's output was truncated mid-emit and depth never
                    # returned to 0), regex-extract whatever string fields landed before the
                    # cut. Better partial coverage than nothing.
                    _salvage = {}
                    for _field in (
                        "narrative", "ema_state", "ema_story", "bb_state", "bb_story",
                        "candle_tests", "candle_story", "rsi_state", "momentum_story",
                        "retracement_status", "cascade_phase", "phase_assessment", "clarity",
                    ):
                        _m = _re.search(rf'"{_field}"\s*:\s*"((?:[^"\\]|\\.)*)"', ta_response, _re.DOTALL)
                        if _m:
                            _salvage[_field] = _m.group(1)
                    # conflicting_signals is an array — pull contents
                    _cs = _re.search(r'"conflicting_signals"\s*:\s*\[([^\]]*)\]', ta_response, _re.DOTALL)
                    if _cs:
                        _salvage["conflicting_signals"] = [
                            s.strip().strip('"') for s in _re.findall(r'"((?:[^"\\]|\\.)*)"', _cs.group(1))
                        ]
                    if _salvage:
                        ta_interpretation = _salvage
                        if "narrative" not in ta_interpretation:
                            ta_interpretation["narrative"] = ta_response[:400]
                        if "clarity" not in ta_interpretation:
                            ta_interpretation["clarity"] = "UNCLEAR"
                        logger.warning(
                            "[TA] JSON depth-parse failed for %s — salvaged %d fields via regex: %s",
                            instrument, len(_salvage), sorted(_salvage.keys()),
                        )
                    else:
                        ta_interpretation = {
                            "narrative": ta_response[:400] if ta_response else "TA parse error",
                            "clarity": "UNCLEAR",
                            "conflicting_signals": ["JSON parse failed"],
                        }
                        logger.warning("[TA] JSON parse failed for %s — raw: %s", instrument, ta_response[:200])
            except (json.JSONDecodeError, Exception) as parse_exc:
                logger.warning("Failed to parse TA LLM JSON for %s: %s | raw: %s",
                               instrument, parse_exc, ta_response[:200])

            # When TA is skipped for the test, force ta_interpretation truly empty so the
            # downstream "Indicator Data — TA Picture" section is not added to the validator
            # task (the builder at ~line 6334 short-circuits on empty narrative + empty fields).
            # The Python-computed thesis_progress is still added below — it's deterministic
            # and not part of the TA prefeed.
            if _skip_ta_prefeed:
                ta_interpretation = {}
                ta_response = ""

            # ── Compute thesis_progress from Python (deterministic — no LLM hallucination) ──
            _fan_dir_lc = (ema_result.get("fan_direction", "") or "").lower()
            _gap_p100 = ema_result.get("gap_price_100", 0) or 0
            _s1_met = bool(_cross_happened)
            _s2_met = bool(ema_result.get("fan_ordered", False))
            _s3_met = ((_fan_dir_lc in ("bullish", "bull") and _gap_p100 > 0) or
                       (_fan_dir_lc in ("bearish", "bear") and _gap_p100 < 0) or
                       (_fan_dir_lc not in ("bullish", "bull", "bearish", "bear"))) and _v4_e100_dist >= 5.0
            # Dual-window: step met if 5-bar positive OR (20-bar positive with only minor 5-bar pullback)
            _s4_met = (_v4_fan_delta > 0) or (_v4_fan_delta_20 > 0.002 and _v4_fan_delta > -0.001)
            _s5_met = (_v4_bb_delta > 0.0004) or (_v4_bb_delta_20 > 0.0008 and _v4_bb_delta > -0.0002)
            _tp_steps = [_s1_met, _s2_met, _s3_met, _s4_met, _s5_met]
            _tp_names = ["step1_cross", "step2_fan_ordered", "step3_e100_clear", "step4_fan_growing", "step5_bb_expanding"]
            _python_thesis = {
                "step1_cross":       {"met": _s1_met, "detail": f"{int(_bars_since_cross)} bars ago" if _s1_met else "no recent cross (>50 bars or never)"},
                "step2_fan_ordered": {"met": _s2_met, "detail": f"ordered {_fan_dir_lc}" if _s2_met else f"tangled (fan_state={ema_result.get('fan_state','?')})"},
                "step3_e100_clear":  {"met": _s3_met, "detail": f"{_v4_e100_dist:.1f}p {'above' if _gap_p100 > 0 else 'below'} E100" if _s3_met else f"only {_v4_e100_dist:.1f}p from E100 (need ≥5p)"},
                "step4_fan_growing": {"met": _s4_met, "detail": f"Δ5={_v4_fan_delta:+.5f} Δ20={_v4_fan_delta_20:+.5f} ({'expanding' if _s4_met else 'not expanding — both windows negative'})"},
                "step5_bb_expanding":{"met": _s5_met, "detail": f"Δ5={_v4_bb_delta:+.5f} Δ20={_v4_bb_delta_20:+.5f} ({'expanding' if _s5_met else 'not expanding — both windows negative'})"},
                "steps_confirmed": sum(_tp_steps),
                "steps_missing": [n for n, met in zip(_tp_names, _tp_steps) if not met],
            }
            # Always override with Python-computed value — reliable and free
            if ta_interpretation is None:
                ta_interpretation = {}
            ta_interpretation["thesis_progress"] = _python_thesis
            logger.info("[TA] Python thesis: %d/5 steps confirmed — missing: %s",
                        _python_thesis["steps_confirmed"], _python_thesis["steps_missing"])

            # Merge: raw_analysis has the data, ta_interpretation has the reasoning
            analysis_results = raw_analysis  # full raw data for downstream agents
            analysis_results["ta_interpretation"] = ta_interpretation
            analysis_results["agent_response"] = ta_response
            analysis_results["agent_rounds"] = ta_result.get("rounds", 0)
            analysis_results["tool_calls"] = ta_result.get("tool_calls", [])

            # V4: Pass chart path and narrative through to validator
            analysis_results["v4_chart_path"] = _v4_chart_path
            analysis_results["v4_narrative"] = ta_interpretation.get("narrative", ta_response[:500]) if ta_interpretation else ta_response[:500]
            analysis_results["v4_clarity"] = ta_interpretation.get("clarity", "UNKNOWN") if ta_interpretation else "UNKNOWN"

            # Extract LLM's assessment (V4: TA no longer provides direction/recommendation)
            sniper_score_val = sniper_result.get("max_score", 0)
            if ta_interpretation:
                # V4 TA doesn't provide direction — derive from EMA fan if neutral
                llm_direction = ta_interpretation.get("direction", "neutral")

                # If direction is neutral, derive from fan_direction (sniper or ema_result fallback)
                if llm_direction == "neutral":
                    fan_dir = ""
                    if sniper_result:
                        fan_dir = (sniper_result.get("fan_direction", "") or "").lower()
                    # Second fallback: ema_result computed later in the cycle
                    if not fan_dir and ema_result:
                        fan_dir = (ema_result.get("fan_direction", "") or "").lower()
                    if fan_dir in ("bullish", "bull", "up"):
                        llm_direction = "bullish"
                    elif fan_dir in ("bearish", "bear", "down"):
                        llm_direction = "bearish"
                    # Keep neutral only if fan is genuinely neutral/unknown

                llm_rec = ta_interpretation.get("recommendation", "HOLD")
                llm_confidence = ta_interpretation.get("confidence", 0)
                analysis_results["llm_direction"] = llm_direction
                analysis_results["llm_recommendation"] = llm_rec
                analysis_results["llm_confidence"] = llm_confidence
                analysis_results["llm_confluence_score"] = llm_confidence

            # Generate confluence-equivalent score (0-100) from sniper data
            confluence_equiv = sniper_to_confluence(sniper_result)
            analysis_results["confluence"] = confluence_equiv
            analysis_results["confluence_score"] = confluence_equiv["total_score"]

            # Sanitize
            try:
                analysis_results = _sanitize_for_json(analysis_results)
            except Exception:
                pass

            final_score = sniper_score_val
            # Derive final_dir from fan if neutral
            final_dir = ta_interpretation.get("direction", sniper_result.get("direction", "neutral")) if ta_interpretation else sniper_result.get("direction", "neutral")
            if final_dir == "neutral" and sniper_result:
                fan_dir = (sniper_result.get("fan_direction", "") or "").lower()
                if fan_dir in ("bullish", "bull", "up"):
                    final_dir = "bullish"
                elif fan_dir in ("bearish", "bear", "down"):
                    final_dir = "bearish"

            market_state = ta_interpretation.get("market_state", "") if ta_interpretation else ""

            self._post_result(
                task_id, "technical_analysis", MessageType.ANALYSIS_RESULT,
                f"TA: direction={final_dir}, regime={regime}, "
                f"state={market_state[:100]}",
                analysis_results,
            )

            if flight:
                _llm_elapsed = time.time() - phase_start - python_elapsed
                _ta_thesis = (ta_interpretation or {}).get("thesis_progress", {})
                # 2026-04-27: storage limits raised so flight_recorder shows full content —
                # narrative/ema_state 200→500, cascade_phase 150→300. Also added bb_state,
                # candle_tests, rsi_state, retracement_status to flight storage so the
                # full TA picture is queryable (was: only 4 of 9 TA fields were stored).
                flight.record(FlightStage.TA_LLM, pair=instrument, cycle_id=_cycle_id, data={
                    "clarity": (ta_interpretation or {}).get("clarity", "UNCLEAR"),
                    "steps_confirmed": _ta_thesis.get("steps_confirmed", 0),
                    "steps_missing": _ta_thesis.get("steps_missing", []),
                    "narrative": ((ta_interpretation or {}).get("narrative", "") or "")[:500],
                    "ema_state": ((ta_interpretation or {}).get("ema_state", (ta_interpretation or {}).get("ema_story", "")) or "")[:500],
                    "bb_state": ((ta_interpretation or {}).get("bb_state", (ta_interpretation or {}).get("bb_story", "")) or "")[:400],
                    "candle_tests": ((ta_interpretation or {}).get("candle_tests", (ta_interpretation or {}).get("candle_story", "")) or "")[:400],
                    "rsi_state": ((ta_interpretation or {}).get("rsi_state", (ta_interpretation or {}).get("momentum_story", "")) or "")[:400],
                    "retracement_status": ((ta_interpretation or {}).get("retracement_status", "") or "")[:400],
                    "cascade_phase": ((ta_interpretation or {}).get("cascade_phase", (ta_interpretation or {}).get("phase_assessment", "")) or "")[:300],
                    "conflicting_signals": (ta_interpretation or {}).get("conflicting_signals", [])[:5],
                    "ta_field_count": len(ta_interpretation or {}),
                }, duration_ms=_llm_elapsed * 1000)

            phase_elapsed = time.time() - phase_start
            phase_timings["analysis"] = phase_elapsed
            logger.info("[TIMING] analysis: %.2fs (python=%.2fs, LLM=%.2fs)", 
                       phase_elapsed, python_elapsed, phase_elapsed - python_elapsed)
            _report_agent_performance("technical_analyst", True, phase_elapsed)
            # V4: Build TA summary for dashboard
            _ta_narrative = ta_interpretation.get("narrative", "") if ta_interpretation else ""
            _ta_clarity = ta_interpretation.get("clarity", "?") if ta_interpretation else "?"
            _ta_conflicts = ta_interpretation.get("conflicting_signals", []) if ta_interpretation else []
            _ta_ema_story = ta_interpretation.get("ema_story", "") if ta_interpretation else ""
            _ta_thesis_prog = ta_interpretation.get("thesis_progress", {}) if ta_interpretation else {}
            _ta_steps_done = _ta_thesis_prog.get("steps_confirmed", "?")
            _ta_parts = [f"thesis={_ta_steps_done}/5 | clarity={_ta_clarity}"]
            if _ta_narrative:
                _ta_parts.append(f"| {_ta_narrative[:150]}")
            if _ta_conflicts:
                _ta_parts.append(f"| ⚠ {', '.join(str(c) for c in _ta_conflicts[:2])}")
            if _v4_chart_path:
                _ta_parts.append(f"| chart={os.path.basename(_v4_chart_path)}")
            _log_phase("technical_analyst", " ".join(_ta_parts), phase_elapsed)

            cycle_result["analysis"] = analysis_results
            _sniper_tmp = analysis_results.get("sniper_score", {}) if isinstance(analysis_results, dict) else {}
            cycle_result["indicators"] = _sniper_tmp.get("indicators", {}) if isinstance(_sniper_tmp, dict) else {}
            cycle_result["sniper"] = _sniper_tmp
            # V4: Surface TA explanation for dashboard lightbox
            cycle_result["ta_explanation"] = {
                "narrative": ta_interpretation.get("narrative", "") if ta_interpretation else "",
                "ema_state": ta_interpretation.get("ema_state", ta_interpretation.get("ema_story", "")) if ta_interpretation else "",
                "bb_state": ta_interpretation.get("bb_state", ta_interpretation.get("bb_story", "")) if ta_interpretation else "",
                "candle_tests": ta_interpretation.get("candle_tests", ta_interpretation.get("candle_story", "")) if ta_interpretation else "",
                "rsi_state": ta_interpretation.get("rsi_state", ta_interpretation.get("momentum_story", "")) if ta_interpretation else "",
                "retracement_status": ta_interpretation.get("retracement_status", "") if ta_interpretation else "",
                "cascade_phase": ta_interpretation.get("cascade_phase", ta_interpretation.get("phase_assessment", "")) if ta_interpretation else "",
                "conflicting_signals": ta_interpretation.get("conflicting_signals", []) if ta_interpretation else [],
                "clarity": ta_interpretation.get("clarity", "UNKNOWN") if ta_interpretation else "UNKNOWN",
                "thesis_progress": ta_interpretation.get("thesis_progress", {}) if ta_interpretation else {},
                "chart_path": _v4_chart_path,
            }
            cycle_result["steps_completed"].append("analysis")

        except Exception as exc:
            phase_elapsed = time.time() - phase_start
            phase_timings["analysis"] = phase_elapsed
            logger.info("[TIMING] analysis: %.2fs (failed)", phase_elapsed)
            _report_agent_performance("technical_analyst", False, phase_elapsed)
            logger.error("Cycle #%d analysis failed: %s", cycle_num, exc, exc_info=True)
            self._post_error(task_id, "analysis", str(exc))
            # Log to dashboard timeline so failures are VISIBLE
            _log_phase("technical_analyst",
                       f"⚠️ FAILED: {str(exc)[:200]}",
                       phase_elapsed, status="error")
            _swarm_send_message(
                "technical_analyst", "cycle_orchestrator",
                f"[ERROR] Analysis failed: {str(exc)[:300]}")
            cycle_result["error"] = f"Analysis failed: {exc}"
            # Preserve sniper_score from raw_analysis so confluence scorer still has data
            analysis_results = {
                "error": str(exc),
                "sniper_score": raw_analysis.get("sniper_score", {}) if 'raw_analysis' in dir() else {},
                "ema_signals": raw_analysis.get("ema_signals", {}) if 'raw_analysis' in dir() else {},
                "market_picture": raw_analysis.get("market_picture", {}) if 'raw_analysis' in dir() else {},
            }

        # Step 5: Master Decision (orchestrator receives ALL data, calls validator as resource, makes trade plan)
        phase_start = time.time()
        # Read prior TA results from workspace comments (inter-agent communication)
        prior_ta_results: List[Dict[str, Any]] = []
        if task_id is not None:
            try:
                prior_ta_results = self._protocol.get_agent_results(
                    task_id=task_id,
                    agent_name="technical_analysis",
                )
                if prior_ta_results:
                    logger.info(
                        "Cycle #%d: Validation phase read %d prior TA results",
                        cycle_num, len(prior_ta_results),
                    )
            except Exception as exc:
                logger.warning("Failed to read prior TA results: %s", exc)

        # Load instrument knowledge for informed decision-making (historical learning)
        instrument_knowledge: Dict[str, Any] = {}
        try:
            ks = _get_knowledge_store()
            if ks is not None:
                instrument_knowledge = ks.get_knowledge(instrument)
                if instrument_knowledge:
                    logger.info(
                        "Cycle #%d: Loaded instrument knowledge for %s "
                        "(patterns=%d, performance=%d)",
                        cycle_num, instrument,
                        len(instrument_knowledge.get("patterns", {})),
                        len(instrument_knowledge.get("performance", {})),
                    )
        except Exception as exc:
            logger.warning("Failed to load instrument knowledge: %s", exc)

        # Load historical backtest performance (optional, graceful degradation)
        historical_performance: Optional[Dict[str, Any]] = None
        try:
            from Source.backtester import Backtester, PerformanceMetrics
            # Check KnowledgeStore for cached backtest metrics
            if instrument_knowledge:
                perf = instrument_knowledge.get("performance", {})
                # Look for backtest_results in performance metrics
                backtest_data = perf.get("backtest_results")
                if isinstance(backtest_data, dict):
                    val = backtest_data.get("value", {})
                    if isinstance(val, dict):
                        historical_performance = val
                        logger.info(
                            "Cycle #%d: Loaded historical backtest performance for %s "
                            "(win_rate=%.2f, pf=%.2f)",
                            cycle_num, instrument,
                            val.get("win_rate", 0.0),
                            val.get("profit_factor", 0.0),
                        )
        except ImportError:
            logger.debug("Backtester not available -- skipping historical performance")
        except Exception as exc:
            logger.warning("Failed to load backtest performance: %s", exc)

        validation_results: Dict[str, Any] = {}
        try:
            h1_candles = candles_for_ta.get("H1", []) if isinstance(candles_for_ta, dict) else []

            # Build indicator inputs for validator from sniper pipeline
            sniper_data = analysis_results.get("sniper_score", {}) if isinstance(analysis_results, dict) else {}
            sniper_ind = sniper_data.get("indicators", {})
            indicators = {
                "core": sniper_ind,  # sniper computes all indicators
                "advanced": sniper_ind,
            }
            patterns = {
                "candlestick_patterns": sniper_data.get("detected_patterns", []),
                "chart_patterns": [],
            }

            # Build trade_params from sniper + TA interpretation
            ta_interp = analysis_results.get("ta_interpretation", {}) if isinstance(analysis_results, dict) else {}
            
            # Sniper is the EARLY WARNING SYSTEM only — it triggers the cycle but does NOT set direction.
            # Direction comes from market structure: EMA fan expansion + BB expansion + price position.
            # Priority: (1) Scout thesis (2) EMA fan direction (3) neutral fallback
            # Sniper buy_score vs sell_score is NOT used for direction — it's just "something is happening here"
            sniper_buy = sniper_data.get("buy_score", 0)
            sniper_sell = sniper_data.get("sell_score", 0)

            # Get structural direction from EMA fan (computed in ema_result / market_picture)
            # generate_market_picture() nests fan_direction under ema{} — check both levels
            _ema_sub = ema_result.get("ema", {}) if isinstance(ema_result, dict) else {}
            _fan_direction = (
                ema_result.get("fan_direction", "") or
                _ema_sub.get("fan_direction", "") or   # ← nested under ema{} in generate_market_picture
                (scout_context or {}).get("fan_direction", "") or
                sniper_data.get("fan_direction", "") or
                ""
            ).lower()

            # Scout thesis is PRIMARY (scout read the market story: EMA fan + BB + price structure)
            if scout_context and scout_context.get("direction"):
                _scout_dir = scout_context["direction"].lower()
                if _scout_dir in ("buy", "long", "bullish"):
                    effective_direction = "bullish"
                elif _scout_dir in ("sell", "short", "bearish"):
                    effective_direction = "bearish"
                else:
                    # Scout direction unclear — fall back to EMA fan
                    effective_direction = _fan_direction if _fan_direction in ("bullish", "bearish") else "neutral"
            else:
                # No scout context — derive purely from EMA fan structure
                if _fan_direction in ("bullish", "bearish"):
                    effective_direction = _fan_direction
                else:
                    effective_direction = "neutral"

            logger.info("[DIRECTION] %s: EMA fan=%s | Scout=%s | Effective=%s",
                        instrument, _fan_direction,
                        scout_context.get("direction", "none") if scout_context else "none",
                        effective_direction)
            ta_market_state = ta_interp.get("market_state", "") if ta_interp else ""
            confidence = ta_interp.get("confidence", 0) if ta_interp else 0

            # Risk limits from config + live account data
            daily_loss_pct = 0.0
            if isinstance(cycle_result.get("data_collection"), dict):
                daily_perf = cycle_result["data_collection"].get("daily_performance", {})
                if isinstance(daily_perf, dict):
                    daily_loss_pct = daily_perf.get("daily_loss_pct", 0.0)
            # _cycle_user_id already resolved at top of TA phase
            risk_limits = _get_risk_limits(account_summary, daily_loss_pct, user_id=_cycle_user_id)

            # Compute PARTIAL confluence (everything except DB evidence — that comes from validator)
            pre_validator_confluence = {"total_score": 0}
            try:
                from Source.full_confluence_scorer import compute_full_confluence
                # Get profile engine — try module var first, then Flask app config
                _pe = _shared_profile_engine
                if _pe is None:
                    try:
                        from flask import current_app
                        _pe = current_app.config.get('_profile_engine')
                    except Exception:
                        pass
                if isinstance(sniper_data, dict) and "instrument" not in sniper_data:
                    sniper_data["instrument"] = instrument
                # Inject fan direction as trade direction if not explicitly set —
                # the confluence scorer uses 'direction' for is_with_trend; sniper_data
                # often has no 'direction' field causing gate2 to score 3 instead of 10.
                # generate_market_picture nests fan_direction under ema{} — check both levels.
                if isinstance(sniper_data, dict) and sniper_data.get("direction", "neutral") in ("neutral", "none", "", None):
                    _ema_nested = (ema_result or {}).get("ema", {}) if isinstance(ema_result, dict) else {}
                    _fan_dir_for_confluence = (
                        (ema_result or {}).get("fan_direction", "") or
                        _ema_nested.get("fan_direction", "") or
                        (scout_context or {}).get("fan_direction", "") or
                        "neutral"
                    )
                    if _fan_dir_for_confluence not in ("neutral", "mixed", ""):
                        sniper_data["direction"] = _fan_dir_for_confluence
                pre_validator_confluence = compute_full_confluence(
                    sniper_result=sniper_data,
                    intelligence_data=intelligence_data,
                    db_evidence=None,
                    account_state=account_summary,
                    min_confluence=int(risk_limits.get("min_confluence", 40)),
                    market_picture=ema_result,
                    # Merge thesis values into scout_context so the Gate 1 scorer (which
                    # reads fan_delta_5bar / bb_delta_5bar from this dict) gets real
                    # numbers on manual cycles where scout_context is otherwise empty.
                    scout_context={
                        **(scout_context or {}),
                        **{k: v for k, v in (_thesis or {}).items() if v is not None},
                    },
                    profile_engine=_pe,
                    pair=instrument,
                )
                _pvc_tradeable = pre_validator_confluence.get("tradeable", False)
                _pvc_summary = pre_validator_confluence.get("summary", "")
                logger.info("[PRE-VALIDATOR CONFLUENCE] %s: %d/75 tradeable=%s — %s",
                           instrument, pre_validator_confluence["total_score"],
                           _pvc_tradeable, _pvc_summary[:80])
            except Exception as pvc_exc:
                logger.warning("Pre-validator confluence failed: %s", pvc_exc)

            trade_params = {
                "sniper_buy_score": sniper_buy,
                "sniper_sell_score": sniper_sell,
                "sniper_threshold": sniper_data.get("threshold", 12),
                "sniper_signal": sniper_data.get("signal", "HOLD"),
                "confluence_score": pre_validator_confluence.get("total_score", 0),
                "direction": effective_direction,
                "action": "buy" if effective_direction == "bullish" else (
                    "sell" if effective_direction == "bearish" else "hold"
                ),
                "instrument": instrument,
                "ta_market_state": ta_market_state,
                "ta_key_signals": ta_interp.get("key_signals", []) if ta_interp else [],
                "ta_contradictions": ta_interp.get("contradictions", []) if ta_interp else [],
                "ta_reasoning": ta_interp.get("reasoning", "") if ta_interp else "",
                "sniper_score": sniper_data,
            }

            # Step 5: Validator AGENT — skip if Gate 1 failed (no signal found)
            # ── Gate 1 — lightweight sanity check only ───────────────────────
            # Scout pre-filters before cycles run (quality gate, pair filter, weak pair
            # EARLY_WARNING block, direction filter, cooldown). By the time a cycle reaches
            # here, it's already been qualified. The old expansion-scoring Gate 1 was
            # incorrectly blocking valid retracement setups (contracting fan = 0/75 score).
            # Only block truly unsalvageable signals:
            # Use freshly-computed ema_result as primary source (most reliable),
            # then fall back to scout_context fields.
            _ema_nested = (ema_result or {}).get("ema", {}) if isinstance(ema_result, dict) else {}
            _fan_dir_raw = (
                (ema_result or {}).get("fan_direction", "") or
                _ema_nested.get("fan_direction", "") or
                (scout_context or {}).get("fan_direction", "") or
                (scout_context or {}).get("market_snapshot", {}).get("fan_direction", "") or
                (scout_context or {}).get("ema_data", {}).get("fan_direction", "")
            )
            _fan_state_raw = (
                (ema_result or {}).get("fan_state", "") or
                _ema_nested.get("fan_state", "") or
                (scout_context or {}).get("fan_state", "") or
                (scout_context or {}).get("market_snapshot", {}).get("fan_state", "")
            )
            _cross_bars = (scout_context or {}).get("cross_bars_ago", 0) or 0

            _hard_block = False
            _hard_block_reason = ""
            # Manual run = no scout alert triggered this cycle. scout_context may be non-None
            # if the setup classifier created it mid-cycle (line ~2971), but that doesn't
            # make it a scout-triggered run. Check for scout-specific fields instead.
            _is_manual_run = not (scout_context or {}).get("alert_type") and \
                             not (scout_context or {}).get("triggered_by") and \
                             not (scout_context or {}).get("scout_alert_id")
            # Block 1: genuinely no direction at all (not a retracement, not a snipe)
            # Manual runs always reach the validator — Gate 1 hard-block is for automated scout cycles only.
            # 2026-05-07: gated behind gate.gate1_sanity_enabled (default False). Tim's call: was
            # killing Phase 1 early-formation cycles before validator could evaluate them as WATCH.
            # Validator will SKIP weak setups itself; let it decide.
            _gate1_sanity_enabled = bool(tc_get("gate.gate1_sanity_enabled", False))
            if _gate1_sanity_enabled and \
               not _is_manual_run and \
               _fan_dir_raw in ("neutral", "mixed", "") and \
               (scout_context or {}).get("alert_type") not in ("RETRACEMENT", "CRITERIA_MET") and \
               (scout_context or {}).get("triggered_by") not in ("snipe", "cascade_reentry"):
                _hard_block = True
                _hard_block_reason = f"fan_direction='{_fan_dir_raw}' with no qualifying alert type"

            if _hard_block:
                logger.info("[GATE1 SANITY BLOCK] %s: %s", instrument, _hard_block_reason)
                _gate1_msg = f"Gate 1 SANITY FAIL: {_hard_block_reason}"
                _log_phase("validator_verdict", _gate1_msg, 0, status="skip")
                cycle_result["action"] = "hold"
                cycle_result["reason"] = _gate1_msg
                cycle_result["validator_verdict"] = "SKIP"
                cycle_result["steps_completed"].append("validation")
                cycle_result["steps_completed"].append("hold_decision")
                try:
                    _push_dashboard(instrument, cycle_result)
                except Exception:
                    pass
                return cycle_result
            # All other cycles — let the validator decide
            _gate1_passed = True  # Gate 1 replaced by scout pre-filtering
            # ── Build market story section from scout context ──
            _snap = (scout_context.get('market_snapshot', {}) or {}) if scout_context else {}
            _story_section = ""
            if _snap.get('story_thesis'):
                _cs = _snap.get('candle_structure', {}) or {}
                _wp = _cs.get('wick_pressure', {}) if isinstance(_cs, dict) else {}
                _bt = _cs.get('body_trend', {}) if isinstance(_cs, dict) else {}
                _e100 = _cs.get('e100_interaction', {}) if isinstance(_cs, dict) else {}
                _story_section = (
                    f"### MARKET STORY (3-Layer Read from Scout)\n"
                    f"**Thesis**: {_snap.get('story_entry_type', 'unknown')} — {_snap.get('story_thesis', 'N/A')}\n"
                    f"**Opportunity Score**: {_snap.get('story_opportunity_score', 0)}/100 | "
                    f"Confidence: {_snap.get('story_confidence', 0):.2f}\n"
                    f"**Narrative**: {_snap.get('story_narrative', 'N/A')}\n"
                    f"**Warnings**: {json.dumps(_snap.get('story_warnings', []))}\n\n"
                    f"**Layer 1 — Trend**: Fan {_snap.get('fan_direction', '?')} {_snap.get('fan_state', '?')} | "
                    f"Velocity: {_snap.get('separation_velocity', 0):.6f}%/bar ({_snap.get('velocity_trend', '?')}) | "
                    f"Health: {_snap.get('trend_health', 0)}/100 | Reversal Risk: {_snap.get('reversal_risk', '?')}\n"
                    f"**Layer 2 — Structure**: "
                    f"Wick pressure: {_wp.get('dominant_pressure', '?')} ({_wp.get('pressure_strength', '?')}) | "
                    f"Body trend: {_bt.get('body_trend', '?')} ({_bt.get('direction_bias', '?')}) | "
                    f"E100: {_e100.get('type', _snap.get('ema100_role', '?'))} "
                    f"(bounces={_e100.get('bounces', '?')}, breaks={_e100.get('breaks', '?')}) | "
                    f"E100 candle: {_snap.get('e100_candle_pattern') or 'None'}\n"
                    f"**Layer 3 — Momentum**: State={_snap.get('momentum_state', '?')} | "
                    f"Significance={_snap.get('momentum_significance', '?')} | "
                    f"Exhausted={_snap.get('momentum_exhausted', False)}\n"
                    f"Momentum narrative: {_snap.get('momentum_narrative', 'N/A')}\n\n"
                    f"**VALIDATE THIS THESIS** against the evidence. Does Layer 1 support the entry type? "
                    f"Does Layer 2 show the right price action? Does Layer 3 confirm or contradict?\n\n"
                )
            
            # ── Determine entry type: expansion, counter-trend, with-trend, or EMA cross ──
            _fan_dir = (ema_result.get('fan_direction', 'neutral') or 'neutral').lower() if ema_result else 'neutral'
            _trade_dir = effective_direction.lower() if effective_direction else 'neutral'
            _fan_state = (ema_result.get('fan_state', 'unknown') or 'unknown').lower() if ema_result else 'unknown'

            # Check if scout resolved direction via expansion logic
            _opp_source = scout_context.get('opportunity_source', '') if isinstance(scout_context, dict) else ''
            _is_expansion_entry = _opp_source in ('expansion_thesis', 'expansion_thesis_mixed')
            _raw_sniper_dir = scout_context.get('raw_sniper_direction', '') if isinstance(scout_context, dict) else ''

            _is_ema_cross = _opp_source == 'ema_cross_trend'
            _is_thesis_confirmed = _opp_source in ('thesis_confirmed', 'thesis_elite')
            _is_counter_trend = (
                not _is_ema_cross and not _is_expansion_entry and not _is_thesis_confirmed and (
                    (_trade_dir == 'bullish' and _fan_dir == 'bearish') or
                    (_trade_dir == 'bearish' and _fan_dir == 'bullish')
                )
            )
            _is_snipe_trigger = (
                isinstance(scout_context, dict) and
                scout_context.get("triggered_by") in ("snipe", "cascade_reentry")
            )
            if _is_expansion_entry:
                _entry_type_label = "EXPANSION ENTRY (EMA fan + BB expanding)"
            elif _is_thesis_confirmed:
                _entry_type_label = "THESIS CONFIRMED (direction confirmed — fan aligned + EMA separating + BB expanding + candles past E100)"
            elif _is_ema_cross:
                _entry_type_label = "EMA CROSS TREND ENTRY (independent of sniper)"
            elif _is_counter_trend:
                _entry_type_label = "COUNTER-TREND (mean reversion)"
            else:
                _entry_type_label = "WITH-TREND (momentum)"

            # Build entry type explanation for validator
            if _is_expansion_entry:
                _v4_note = ""
                if _raw_sniper_dir and _raw_sniper_dir != scout_context.get('direction', '').lower():
                    _v4_note = (
                        f"\n\n**NOTE: The raw v4 sniper scored {_raw_sniper_dir.upper()} (mean reversion), "
                        f"but during expansion the EMA fan direction is authoritative. "
                        f"v4 opposite to fan = CONFIRMATION that trend has momentum, NOT a conflict. "
                        f"87% WR on live trades with this pattern.**"
                    )
                # Include expansion quality scoring if available
                _eq = scout_context.get('expansion_quality')
                _eq_note = ""
                if _eq and isinstance(_eq, dict):
                    _eq_note = (
                        f"\n\n**EXPANSION QUALITY: {_eq.get('score', '?')}/14 ({_eq.get('label', '?')})**\n"
                        f"Timing breakdown (from 8.5M backtest trades):\n"
                    )
                    for k, v in (_eq.get('details') or {}).items():
                        _eq_note += f"  - {k}: {v}\n"
                    _eq_note += (
                        f"\nInterpretation: Higher score = earlier in the cross = better timing. "
                        f"10+ = ELITE entry. 7-9 = solid. 4-6 = OK. <4 = late/weak.\n"
                        f"DO NOT penalize 'indicator conflicts' — during expansion, MACD opposing = AT the crossover = 87.7% WR. "
                        f"RSI in sweet spot (30-50 sell/50-70 buy) = 82-98% WR."
                    )
                
                _rsi_q = scout_context.get('rsi_quality', '')
                _rsi_q_note = f"\nRSI quality: **{_rsi_q}** (from backtest bucket analysis)" if _rsi_q else ""
                
                _entry_explanation = (
                    '**This is an EXPANSION ENTRY. The EMA fan is expanding/just_crossed AND Bollinger Bands are expanding. '
                    'This means a directional move is underway. '
                    'Direction comes from the EMA fan structure, NOT from v4 sniper scores. '
                    'The v4 sniper is a mean-reversion system — during expansion, its directional signal is INVERTED '
                    '(it sees oversold and says BUY, but oversold during bearish expansion means sell momentum is strong). '
                    'Validate based on: (1) Is the EMA fan truly expanding? (2) Is BB expanding? '
                    '(3) Is E100 acting as support/resistance in the trade direction? '
                    '(4) Is RSI directionally aligned with the move (low for sells, high for buys)? '
                    'Do NOT reject because v4 sniper disagrees on direction — that disagreement is EXPECTED and CONFIRMS the trade.**'
                    + _v4_note + _eq_note + _rsi_q_note
                )
            elif _is_ema_cross:
                _entry_explanation = (
                    '**This is an EMA CROSS TREND ENTRY. The EMAs have recently crossed with expanding separation and BB expansion. '
                    'This is a TREND entry — completely independent of the sniper system. '
                    'The sniper is mean reversion and will show conflicting signals (overbought/oversold) during fresh trends — IGNORE sniper scores entirely for this entry type. '
                    'Validate based on: (1) Is the EMA cross real? (2) Is separation building? (3) Is BB expanding? (4) Does H4 bias support? '
                    'Do NOT reject because sniper disagrees — sniper is irrelevant for trend entries.**'
                )
            elif _is_counter_trend:
                _entry_explanation = (
                    '**This is a COUNTER-TREND entry. The sniper is a mean reversion system (84.6% WR). '
                    'It INTENTIONALLY fires opposite to the current trend direction. '
                    'Sniper BUY in a bearish market = buying the reversal. '
                    'Sniper SELL in a bullish market = selling the reversal. '
                    'Direction opposition IS the strategy — it is NOT a conflict. '
                    'Only reject counter-trend if the fan is EXPANDING/ACCELERATING (healthy trend, no exhaustion yet).**'
                )
            else:
                _entry_explanation = '**This is a WITH-TREND entry. Sniper direction aligns with fan direction.**'

            # --- Learning data integration (feeds validator live performance context) ---
            _learning_sections = ""
            try:
                from db_connection import get_db
                _setup_id = scout_context.get('setup_id', '') if scout_context else ''
                _pair = instrument

                # Exit learning: historical MFE/hold times for this setup+pair
                with get_db() as _lconn:
                    _exit = _lconn.execute(
                        "SELECT AVG(max_favorable_excursion_pips), AVG(duration_minutes), primary_exit_signal, COUNT(*) "
                        "FROM exit_learning WHERE setup_name=? AND pair=? ORDER BY created_at DESC LIMIT 100",
                        (_setup_id, _pair)).fetchone()
                    if _exit and _exit[3] and _exit[3] > 0:
                        _learning_sections += (
                            f"### 📈 Exit Learning ({_exit[3]} past trades, {_setup_id}/{_pair})\n"
                            f"- Avg MFE: {_exit[0]:.1f} pips | Avg hold: {_exit[1]:.0f} min | Best exit: {_exit[2] or 'Dynamic'}\n"
                            f"Use this to calibrate exit expectations.\n\n")

                # Setup revenue: live P&L tracking from v2/trading_forex.db
                # (setup_revenue lives in the trading forex pool,
                #  NOT in trevor_database.db — using get_db() would query wrong DB)
                _rev_conn = None
                try:
                    _rev_conn = get_trading_forex()

                    # Per-setup revenue for this pair
                    _rev = _rev_conn.execute(
                        "SELECT wins, losses, total_pips, total_usd FROM setup_revenue WHERE setup_name=? AND pair=?",
                        (_setup_id, _pair)).fetchone()
                    if _rev and (_rev[0] + _rev[1]) > 0:
                        _live_wr = _rev[0] / (_rev[0] + _rev[1]) * 100
                        _perf_tag = "**CONSISTENT PERFORMER**" if _live_wr > 75 else (
                            "**⚠️ UNDERPERFORMER** — reduce confidence" if _live_wr < 50 and (_rev[0]+_rev[1]) >= 5 else "")
                        _learning_sections += (
                            f"### 💰 Live Performance ({_setup_id}/{_pair})\n"
                            f"- {_rev[0]}W/{_rev[1]}L ({_live_wr:.0f}% WR) | {_rev[2]:+.1f} pips | ${_rev[3]:+.0f} PnL\n"
                            f"{_perf_tag}\n\n")

                    # ALL setups that have won on this pair (regardless of current setup_id)
                    # This tells the validator "this pair has these winning patterns"
                    _pair_winners = _rev_conn.execute(
                        "SELECT setup_name, wins, losses, total_pips, total_usd, win_rate "
                        "FROM setup_revenue WHERE pair=? AND wins >= 1 ORDER BY total_usd DESC LIMIT 5",
                        (_pair,)).fetchall()
                    if _pair_winners:
                        _learning_sections += f"### 🏆 Winning Setups on {_pair} (lifetime)\n"
                        for _pw in _pair_winners:
                            _pw_wr = float(_pw[5] or 0) * 100
                            _pw_tag = " ⭐" if _pw_wr >= 70 and _pw[1] >= 2 else ""
                            _learning_sections += (
                                f"- **{_pw[0]}**: {_pw[1]}W/{_pw[2]}L ({_pw_wr:.0f}% WR) | "
                                f"{_pw[3]:+.1f} pips | ${_pw[4]:+.2f}{_pw_tag}\n")
                        _learning_sections += "\n"

                    # Cross-pair winners (setups that win on 2+ pairs = proven patterns)
                    _cross = _rev_conn.execute(
                        "SELECT setup_name, COUNT(DISTINCT pair), SUM(wins), SUM(total_usd) "
                        "FROM setup_revenue WHERE wins >= 1 "
                        "GROUP BY setup_name HAVING COUNT(DISTINCT pair) >= 2 "
                        "ORDER BY SUM(total_usd) DESC LIMIT 3").fetchall()
                    if _cross:
                        _learning_sections += "### 🌐 Proven Cross-Pair Setups\n"
                        for _cp in _cross:
                            _learning_sections += (
                                f"- **{_cp[0]}**: wins on {_cp[1]} pairs | {_cp[2]} total wins | "
                                f"${float(_cp[3] or 0):+.2f} gross\n")
                        _learning_sections += "\n"
                except Exception as _rev_err:
                    logger.debug(f"Setup revenue learning context unavailable: {_rev_err}")

                # Scout findings: 30-day success rate
                with get_db() as _lconn:
                    _sf = _lconn.execute(
                        "SELECT COUNT(*), SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END), AVG(pips_result) "
                        "FROM scout_findings WHERE (setup_name=? OR setup_name LIKE ? || '%') AND pair=? AND outcome IS NOT NULL "
                        "AND timestamp > datetime('now','-30 days')", (_setup_id, _setup_id, _pair)).fetchone()
                    if _sf and _sf[0] and _sf[0] > 0:
                        _sf_wr = (_sf[1] or 0) / _sf[0] * 100
                        _learning_sections += (
                            f"### 🔍 Scout Alert History (30d, {_setup_id}/{_pair})\n"
                            f"- {_sf[0]} alerts, {_sf[1] or 0} wins ({_sf_wr:.0f}%), avg {_sf[2]:.1f} pips\n"
                            f"{'**RELIABLE PATTERN**' if _sf_wr > 70 else ''}\n\n")

                # Recent trades on this pair
                with get_db() as _lconn:
                    _recent = _lconn.execute(
                        "SELECT direction, result, pips, exit_reason "
                        "FROM live_trades WHERE pair=? ORDER BY entry_time DESC LIMIT 3",
                        (_pair,)).fetchall()
                    if _recent:
                        _learning_sections += f"### 📊 Recent Trades ({_pair})\n"
                        for _rt in _recent:
                            _learning_sections += f"- {_rt[0]} → {_rt[1]} ({_rt[2]:+.1f} pips, {_rt[3]})\n"
                        if sum(1 for _rt in _recent if _rt[1] and 'loss' in str(_rt[1]).lower()) >= 2:
                            _learning_sections += "**⚠️ LOSS STREAK — elevated risk on this pair**\n"
                        _learning_sections += "\n"

            except Exception as _le:
                logger.debug(f"Learning data unavailable (non-critical): {_le}")

            # Build snipe trigger context if this cycle was triggered by a snipe
            # ── User watch context: inject chart + prior validator analysis ──────
            _user_watch_section = ""
            _user_chart_image = None
            _triggered_by = (scout_context or {}).get("triggered_by", "")
            if _triggered_by in ("snipe", "cascade_reentry"):
                _uw_ctx = scout_context or {}
                _uw_val_analysis = _uw_ctx.get("validator_full_analysis") or _uw_ctx.get("validator_context", "")
                _uw_thesis = _uw_ctx.get("user_thesis", "")
                _uw_convo = _uw_ctx.get("conversation_context", [])
                _uw_chart_path = _uw_ctx.get("user_chart_path", "")

                # Load user's submitted chart image if it exists
                if _uw_chart_path:
                    try:
                        _user_chart_image = _load_v4_chart_image(_uw_chart_path)
                    except Exception:
                        pass
                # Also check disk for latest saved chart
                if not _user_chart_image:
                    try:
                        _uw_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                               "dashboard", "user_charts")
                        _uw_path = os.path.join(_uw_dir, f"{instrument}_latest.png")
                        if os.path.exists(_uw_path):
                            _user_chart_image = _load_v4_chart_image(_uw_path)
                    except Exception:
                        pass

                _convo_lines = ""
                for _c in (_uw_convo or [])[-5:]:
                    if isinstance(_c, dict) and _c.get("text"):
                        _convo_lines += f"  [{_c.get('role','?')}]: {_c['text'][:200]}\n"

                if _uw_thesis or _uw_val_analysis or _uw_chart_path:
                    if _triggered_by == "cascade_reentry":
                        # Fast validator cycle fired by guardian Phase 4 resumption
                        _user_watch_section = (
                            f"## ⚡ CASCADE PHASE 4 — SECOND LEG RE-ENTRY EVALUATION\n\n"
                            f"**The guardian detected Phase 4 RESUMPTION on {instrument}.**\n"
                            f"The prior trade ran its first leg in profit, the market retraced (Phase 3),\n"
                            f"and now BOTH the EMA fan separation AND Bollinger Bands are re-expanding.\n\n"
                            + (f"**Guardian context:** {_uw_thesis}\n\n" if _uw_thesis else "")
                            + f"**Your task:** Look at the live chart and evaluate the second-leg re-entry.\n"
                            + f"1. Is the fan still ORDERED? (E21 on correct side of E55)\n"
                            + f"2. Did price bounce off E55 (mid-retrace) or E100 (deep retrace)?\n"
                            + f"3. Are BBs genuinely widening from compressed state, or is this a false start?\n"
                            + f"4. Is RSI recovering from extreme (not still pinned)?\n\n"
                            + f"If YES to the above → TRADE_NOW with direction, OR set a tight SNIPE for the\n"
                            + f"exact conditions that confirm the second leg (prefer Scenario A or B from\n"
                            + f"the fishing line theory — conditions that fire AT the retrace bottom).\n"
                            + f"If NO (fan disordered, price past E55 into reversal territory) → SKIP.\n\n"
                        )
                        logger.info("[V4] %s: cascade_reentry context loaded — phase4 resumption evaluation",
                                    instrument)
                    else:
                        _user_watch_section = (
                            f"## 📸 TRADER CONTEXT — USER-REQUESTED WATCH\n\n"
                            + (f"**Trader's thesis:** {_uw_thesis}\n\n" if _uw_thesis else "")
                            + (f"**Prior validator analysis (what you said before):**\n{_uw_val_analysis[:600]}\n\n"
                               if _uw_val_analysis else "")
                            + (f"**Prior conversation:**\n{_convo_lines}\n" if _convo_lines else "")
                            + (f"**Trader submitted an annotated chart (see image below).**\n\n"
                               if _user_chart_image else "")
                            + f"Your job: evaluate whether the setup the trader described is NOW ready to trade.\n"
                            + f"You have full context of what they showed you — use it.\n\n"
                        )
                        logger.info("[V4] %s: user_watch context loaded — thesis=%s chart=%s prior_val=%d chars",
                                    instrument, bool(_uw_thesis), bool(_user_chart_image), len(_uw_val_analysis))

            _snipe_section = ""
            if _is_snipe_trigger:
                _snipe_wid = (scout_context or {}).get("watch_id", "?")
                _snipe_orig_verdict = (scout_context or {}).get("validator_verdict", "WATCH")
                _snipe_orig_reason = (scout_context or {}).get("validator_reasoning", 
                                     (scout_context or {}).get("raw_suggestion", ""))
                _snipe_conditions = (scout_context or {}).get("conditions_met", [])
                _snipe_cond_summary = ""
                for _sc in (_snipe_conditions if isinstance(_snipe_conditions, list) else []):
                    if isinstance(_sc, dict) and _sc.get("met"):
                        _snipe_cond_summary += f"  - ✅ {_sc.get('field', '?')} = {_sc.get('current', '?')} (target: {_sc.get('target', '?')})\n"
                
                _snipe_section = (
                    f"## 🎯 SNIPE TRIGGER — YOUR PREVIOUS WATCH CONDITIONS WERE MET\n\n"
                    f"**You previously analyzed this pair and issued a {_snipe_orig_verdict} (watch #{_snipe_wid}).**\n"
                    f"Your reasoning: {str(_snipe_orig_reason)[:300]}\n\n"
                    f"**The conditions you specified have now been met:**\n{_snipe_cond_summary}\n"
                    f"**Your job now: Re-evaluate with FRESH market data.**\n"
                    f"- Has the thesis you were waiting for actually materialized?\n"
                    f"- Is the EMA fan now aligned with the trade direction?\n"
                    f"- Are BBs expanding (thesis confirmed) or still contracting (not ready)?\n"
                    f"- Are candles past E100 on the correct side?\n"
                    f"- If ALL thesis conditions are met → **TRADE_NOW with direction**\n"
                    f"- If conditions met but market has moved past the entry → **SKIP** (missed it)\n"
                    f"- If conditions met but new risk appeared → **SKIP** with reason\n\n"
                )

            # ══════════════════════════════════════════════════════════════
            # V4 VALIDATOR — Vision-enabled, THE trading brain
            #
            # Gets: teaching images (cached) + live chart + TA narrative
            # Decides: TRADE_NOW / SNIPE / SKIP + direction
            # Still has MCP tools for DB queries and news
            # ══════════════════════════════════════════════════════════════

            # ── Load vision images ──
            # Local 35B validator: 1 annotated teaching image (distilled model already
            # has the patterns from training — needs one visual anchor for entry-zone
            # markup). Trimmed from 2→1 on 2026-04-23 to cut prefill budget.
            # Anthropic: full 9-image teaching set (model has no prior training).
            try:
                from agents.team_setup import AGENT_SPECS as _agent_specs
                _validator_model = next((a.get("model", "") for a in _agent_specs if a.get("name") == "validator"), "")
            except Exception:
                _validator_model = ""
            _is_local_validator = "mlx" in str(_validator_model).lower()
            if _is_local_validator:
                _v4_images = _load_local_validator_images()  # 1 annotated image
                logger.info("[V4] Local 35B validator — using 1 annotated teaching image")
            else:
                _v4_images = _load_v4_teaching_images()  # full 9-image set for Anthropic
            _v4_live_chart = _load_v4_chart_image(analysis_results.get("v4_chart_path", "")) if isinstance(analysis_results, dict) else None
            _v4_chart_missing = False
            if _v4_live_chart:
                _v4_images_for_call = _v4_images + [_v4_live_chart]
            else:
                _v4_images_for_call = _v4_images
                _v4_chart_missing = True
                logger.warning("[V4] No live chart available — validator running without current chart image")

            # ── V4 TA narrative + key indicators ──
            _v4_ta_narrative = analysis_results.get("v4_narrative", "") if isinstance(analysis_results, dict) else ""
            _v4_ta_clarity = analysis_results.get("v4_clarity", "UNKNOWN") if isinstance(analysis_results, dict) else "UNKNOWN"
            _v4_ta_full = analysis_results.get("ta_interpretation", {}) if isinstance(analysis_results, dict) else {}
            _v4_ta_ema_story = (_v4_ta_full.get("ema_state", "") or _v4_ta_full.get("ema_story", "") or "")[:500]
            _v4_ta_bb_story = (_v4_ta_full.get("bb_state", "") or _v4_ta_full.get("bb_story", "") or "")[:400]
            _v4_ta_candle_tests = (_v4_ta_full.get("candle_tests", "") or _v4_ta_full.get("candle_story", "") or "")[:400]
            _v4_ta_rsi_state = (_v4_ta_full.get("rsi_state", "") or _v4_ta_full.get("momentum_story", "") or "")[:400]
            _v4_ta_retracement = (_v4_ta_full.get("retracement_status", "") or "")[:400]
            _v4_ta_phase = (_v4_ta_full.get("cascade_phase", "") or _v4_ta_full.get("phase_assessment", "") or "")[:300]
            _v4_ta_thesis = _v4_ta_full.get("thesis_progress", {}) or {}
            _v4_ta_conflicts = _v4_ta_full.get("conflicting_signals", []) or []

            # ── Key indicator summary (compact — chart is primary) ──
            _v4_ind_raw = indicators if isinstance(indicators, dict) else {}
            _v4_ind = _v4_ind_raw.get("core", _v4_ind_raw)  # flatten: indicators may be {"core": {...}} or flat
            _v4_ema = ema_result if isinstance(ema_result, dict) and not ema_result.get('error') else {}
            _v4_scout = scout_context if isinstance(scout_context, dict) else {}

            # ── Build intelligence summary ──
            _v4_intel = intelligence_data if isinstance(intelligence_data, dict) else {}
            _v4_intel_text = (
                f"### Intelligence\n"
                f"- Sentiment: {_v4_intel.get('overall_sentiment', 0)} | "
                f"Recommendation: {_v4_intel.get('recommendation', 'NEUTRAL')}\n"
                f"- Risk Events: {json.dumps(_v4_intel.get('risk_events_upcoming', [])[:3])}\n"
                f"- Briefing: {_v4_intel.get('agent_briefing', 'N/A')[:300]}\n\n"
            )

            # ── Build scout evidence section ──
            _v4_scout_evidence = ""
            if _v4_scout:
                # Compute how old the scout's readings are so validator knows how stale they are
                _scout_ts_str = _v4_scout.get("timestamp", "")
                _scout_age_s = None
                if _scout_ts_str:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        _scout_ts = _dt.fromisoformat(_scout_ts_str.replace("Z", "+00:00"))
                        _scout_age_s = int((datetime.now(timezone.utc) - _scout_ts).total_seconds())
                    except Exception:
                        pass
                _scout_age_label = (
                    f"{_scout_age_s}s ago" if _scout_age_s is not None and _scout_age_s < 300
                    else f"{_scout_age_s//60}m ago" if _scout_age_s is not None
                    else "timing unknown"
                )

                _v4_scout_evidence = (
                    f"### Scout Evidence\n"
                    f"⚠️ **Scout scanned {_scout_age_label} — chart below is the current reality**\n"
                    f"- Alert type: **{_v4_scout.get('alert_type', 'UNKNOWN')}**"
                    f"{' — All thesis conditions met' if _v4_scout.get('alert_type') == 'CRITERIA_MET' else ''}"
                    f"{' — Extreme detected but thesis NOT yet confirmed' if _v4_scout.get('alert_type') == 'EARLY_WARNING' else ''}\n"
                    f"- Setup ID: {_v4_scout.get('setup_id') or _v4_scout.get('setup_name', 'N/A')} | "
                    f"Win Rate: {_v4_scout.get('win_rate', 'N/A')}% | "
                    f"Trades: {_v4_scout.get('trade_count', 'N/A')} | PF: {_v4_scout.get('profit_factor', 'N/A')}\n"
                    f"- Scout confidence: {_v4_scout.get('scout_confidence', 'N/A')} ({_v4_scout.get('confidence_tier', 'N/A')})\n"
                    f"- Detected regime: {_detect_regime(sniper_data)}\n"
                    f"- Thesis measurements: fan_Δ5bar={(_thesis.get('fan_delta_5bar') or 0):+.5f} | fan_Δ20bar={(_thesis.get('fan_delta_20bar') or 0):+.5f}, "
                    f"bb_Δ5bar={(_thesis.get('bb_delta_5bar') or 0):+.5f} | bb_Δ20bar={(_thesis.get('bb_delta_20bar') or 0):+.5f}, "
                    f"candles_moving={'yes' if _thesis.get('candles_moving_away') else 'no'}, "
                    f"recent_cross={'yes' if _thesis.get('recent_cross') else 'no'}\n\n"
                )

            # ── Build patterns section + inject dynamic pattern images ──
            _chart_patterns = analysis_results.get("chart_patterns", []) if isinstance(analysis_results, dict) else []
            _pattern_images, _pattern_ref_text = _get_pattern_context(
                detected_patterns, _chart_patterns, divergence
            )
            # Pattern image injection removed 2026-04-28: caused 0-char silent
            # responses on the local 35B path (3+ images → think-loop fills max_tokens
            # with stripped <think> blocks → empty content → JSON parse fails twice
            # → GATE1_BLOCK). Validator runs on 1 teaching + 1 live chart only.
            # _pattern_ref_text below still feeds into the patterns text block.
            # Inject user's submitted chart for user_watch cycles (appended after live chart)
            if _user_chart_image:
                _v4_images_for_call = _v4_images_for_call + [_user_chart_image]
                logger.info("[V4] %s: appended user-submitted chart (user_watch context)", instrument)
            _v4_patterns_text = (
                f"### Patterns Detected This Cycle\n"
                f"- Candlestick: {', '.join(detected_patterns) if detected_patterns else 'None'}\n"
                f"- Divergence: {json.dumps({k:v for k,v in divergence.items() if v}, default=str) if isinstance(divergence, dict) and any(divergence.values()) else 'None'}\n\n"
            ) + _pattern_ref_text

            # ── Build re-entry context ──
            _v4_reentry = ""
            if isinstance(scout_context, dict) and scout_context.get('reentry_context', {}).get('is_reentry'):
                _rc = scout_context['reentry_context']
                _phase = _rc.get('cascade_phase', '')
                _retrace_depth = _rc.get('retrace_depth_pct', 0)
                _peak_bb = _rc.get('peak_bb_width', 0)
                _curr_bb = _rc.get('current_bb_width', 0)
                _e100_tests = _rc.get('e100_tests_in_retrace', 0)
                _reexpansion = _rc.get('reexpansion_count', 0)
                _is_second_leg = _phase in ('resumption', 'continuing', 'second_leg')

                _v4_reentry = (
                    f"### Re-Entry Context — {'SECOND LEG OPPORTUNITY' if _is_second_leg else 'Prior Trade Context'}\n"
                    f"Prior trade: {_rc.get('prior_direction', '?')} → {_rc.get('prior_pnl_pips', 0):+.1f} pips | "
                    f"Exit reason: {_rc.get('prior_exit_reason', '?')} | "
                    f"Fan at close: {_rc.get('fan_state_at_close', '?')}\n"
                )
                if _is_second_leg:
                    _v4_reentry += (
                        f"\n**GUARDIAN PHASE STATE AT CYCLE TRIGGER:**\n"
                        f"- Cascade phase: **{_phase.upper()}** — the retrace completed, resumption is beginning\n"
                        f"- Retrace depth: {_retrace_depth:.0f}% of peak BB width (BB compressed to {_curr_bb:.3f}% from peak {_peak_bb:.3f}%)\n"
                        f"- E100 tests during retrace: {_e100_tests} (0-1 = healthy retrace, 2+ = structural risk)\n"
                        f"- Re-expansion count: {_reexpansion} bars of both_expanding confirmed\n"
                        f"\n**THIS IS A SECOND-LEG ENTRY EVALUATION.**\n"
                        f"The prior trade captured the first leg. The market retraced (Phase 3: BOTH CONTRACT).\n"
                        f"Now both fan separation AND Bollinger Bands are beginning to re-expand (Phase 4: RESUMPTION).\n"
                        f"Your job: confirm the second leg is real, not a false re-expansion, and set re-entry conditions.\n"
                        f"\nLook for:\n"
                        f"1. Fan still ORDERED (E21 on correct side of E55) — if not, the trend reversed, SKIP\n"
                        f"2. Price bounced off E55 or E100 (not floating in mid-air)\n"
                        f"3. BBs starting to widen from compressed state (early signal = best entry)\n"
                        f"4. RSI recovering from extreme (not still pinned at oversold/overbought)\n"
                        f"\nSet SNIPE conditions for the BOTTOM of the retrace / start of resumption.\n"
                        f"Do NOT require `ema_fan_state=expanding` — by then you've missed half the move.\n"
                        f"Target: conditions that fire when price is near E55/E100 AND BBs just starting to widen.\n\n"
                    )
                else:
                    _v4_reentry += "\n"

            # ── Active watch context — injected into validator (read-only) ────
            # Validator sees existing watches for this pair so it can avoid
            # creating duplicates and flag stale/contradicting ones to the user.
            # It can suggest cancellation but CANNOT cancel watches itself.
            _validator_watch_text = ""
            _triggering_watch_id  = (scout_context or {}).get("watch_id") or \
                                    (scout_context or {}).get("_watch_id")
            # Only inject watch list for clean scout cycles (not when a snipe is already firing).
            # When a snipe fires, the validator already gets the snipe context block above.
            _is_snipe_cycle = bool(_triggering_watch_id)
            if not _is_snipe_cycle:
                try:
                    from agents.watch_manager import get_watches_for_validator
                    _validator_watch_text = get_watches_for_validator(instrument)
                except Exception as _wv_err:
                    logger.debug("Could not load watch context for validator: %s", _wv_err)

            # ── Trader annotations — scoped to triggering snipe only ─────────
            # Clean scout cycles get NO annotations — validator must read the chart fresh.
            # When a user snipe fires, only annotations linked to THAT watch are shown.
            # This prevents stale multi-day annotations from contaminating unrelated cycles.
            _validator_ann_text = ""
            try:
                import sqlite3 as _vann_sq
                _vann_db = _TRADING_FOREX_DB
                with _vann_sq.connect(_vann_db, timeout=5) as _vann_conn:
                    _vann_conn.row_factory = _vann_sq.Row
                    if _triggering_watch_id:
                        # Snipe cycle: annotations scoped to this watch, or recent pair annotations
                        # (snipe_id FK doesn't exist yet — fall back to recent+pair scoped)
                        _vann_rows = _vann_conn.execute(
                            "SELECT annotation_type, price, direction, note, ema_cross "
                            "FROM user_chart_annotations WHERE pair=? AND active=1 "
                            "  AND (expires_at IS NULL OR expires_at > datetime('now')) "
                            "  AND created_at > datetime('now', '-48 hours') "
                            "ORDER BY created_at DESC LIMIT 10",
                            (instrument,)
                        ).fetchall()
                    else:
                        # Clean scout cycle: no annotations — fresh read only
                        _vann_rows = []
                if _vann_rows:
                    _vann_lines = ["### Trader's Chart Notes (from your saved annotations)"]
                    _vann_lines.append("These are your own markings on this pair. "
                                       "They reflect your thesis — use as context alongside the live chart.")
                    for _a in _vann_rows:
                        _parts = [f"- [{_a['annotation_type'].upper()}]"]
                        if _a['price']:     _parts.append(f"@ {_a['price']}")
                        if _a['direction']: _parts.append(f"({_a['direction']})")
                        if _a['ema_cross']: _parts.append(f"cross={_a['ema_cross']}")
                        if _a['note']:      _parts.append(f"— {_a['note']}")
                        _vann_lines.append(" ".join(_parts))
                    _validator_ann_text = "\n".join(_vann_lines) + "\n\n"
            except Exception as _vann_err:
                logger.debug(f"Could not load annotations for validator: {_vann_err}")

            _chart_status_warning = (
                "⚠️ **NO LIVE CHART AVAILABLE** — The chart image could not be loaded this cycle. "
                "You have the teaching examples and full TA data below. "
                "Do NOT invent or describe chart details you cannot see. "
                "Evaluate this setup using ONLY the technical indicator values, confluence score, "
                "and TA narrative provided. Make your TRADE/WATCH/SKIP determination from the numbers "
                "— the chart is a visual aid, not a requirement.\n\n"
                if _v4_chart_missing else
                f"✅ Chart received: {len(_v4_images)} teaching examples + 1 live chart. "
                f"**The LAST image is ALWAYS the live {instrument} M15 chart.** "
                f"Teaching images come first. The live chart is always final. "
                f"It shows 3 panels: (1) Candlesticks with EMA 21/55/100 + Bollinger Bands, "
                f"(2) RSI subplot, (3) MACD subplot. 100 bars (~25 hours). Read ALL panels.\n\n"
            )
            # ── COMPUTED FACTUAL DATA for validator ──
            # NO LLM interpretation — just exact indicator values.
            # The validator reads the CHART IMAGE + these facts to form its own thesis.
            _e21_val = float(_v4_ind.get('ema_21', 0) or 0)
            _e55_val = float(_v4_ind.get('ema_55', 0) or 0)
            _e100_val = float(_v4_ind.get('ema_100', 0) or 0)
            _close_val = float(_v4_ind.get('close', 0) or 0)
            _pip_size = 0.01 if 'JPY' in instrument else 0.0001

            # Fan ordering — computed from actual values
            if _e21_val > _e55_val > _e100_val:
                _fan_order_str = "BULLISH ORDERED (E21 > E55 > E100)"
            elif _e100_val > _e55_val > _e21_val:
                _fan_order_str = "BEARISH ORDERED (E100 > E55 > E21)"
            else:
                _parts = []
                if _e21_val > _e55_val:
                    _parts.append("E21 above E55")
                else:
                    _parts.append("E21 below E55")
                if _e55_val > _e100_val:
                    _parts.append("E55 above E100")
                else:
                    _parts.append("E55 below E100")
                _fan_order_str = f"MIXED ({', '.join(_parts)})"

            # Distances in pips
            _e21_dist = (_close_val - _e21_val) / _pip_size if _close_val and _e21_val else 0
            _e55_dist = (_close_val - _e55_val) / _pip_size if _close_val and _e55_val else 0
            _e100_dist = (_close_val - _e100_val) / _pip_size if _close_val and _e100_val else 0

            # 2026-05-05: Removed parallel "Indicator Data: PAIR M15 (computed — no
            # interpretation)" section. It read from _v4_ema and _v4_scout dicts that
            # had silent zero-defaults (separation_velocity, bb_delta_5bar, bb_expanding
            # not consistently populated) — every validator SKIP today cited
            # "Δ5bar=0.00000" as primary disqualifier. Validator now consumes TA's
            # interpretation via the "Indicator Data — TA Picture" section inserted
            # below, which gives the same data (velocity, BB state, cascade phase)
            # in narrative form sourced directly from TA's structured output.
            # Tim's mental model: validator works off TA + chart, not parallel dicts.
            _validator_sections = []
            # 2026-04-29: Scout Evidence section — was dead code (built into
            # _v4_scout_evidence at line ~6087 but never appended anywhere).
            # Inserting at position 0 so validator sees alert_type FIRST in the
            # task — lets it route to the right mental model (Tier 1 catalog vs
            # fan 10-point checklist) before reading indicator data.
            if _v4_scout_evidence:
                _scout_content = _v4_scout_evidence.replace("### Scout Evidence\n", "", 1)
                _validator_sections.insert(0, {
                    "heading": "Scout Evidence",
                    "content": _scout_content,
                })
            # 2026-05-07: TASK ANCHOR — mirrors floor_chat.py:740-759 preamble used
            # for user-submitted annotated charts. Auto-cycles previously got data
            # dumps with no "Your job" instruction; model fell back on prompt's
            # line-9 example or "tim_teach_*" training references. The annotated-chart
            # path consistently produces clean snipe output because of this preamble
            # — bringing the same focused-task framing to scout/manual cycles.
            _scout_alert_type = (scout_context or {}).get("alert_type", "") if scout_context else ""
            _scout_setup_name = ""
            try:
                _scout_setup_name = (
                    (scout_context or {}).get("setup_name", "") or
                    (scout_context or {}).get("setup_id", "") or ""
                )
            except Exception:
                pass
            _scout_direction_hint = ""
            try:
                _scout_direction_hint = (
                    (scout_context or {}).get("direction", "") or
                    (scout_context or {}).get("market_snapshot", {}).get("direction", "") or ""
                )
            except Exception:
                pass
            _scout_thesis_hint = ""
            try:
                _scout_thesis_hint = (
                    (scout_context or {}).get("story_thesis", "") or
                    (scout_context or {}).get("scout_reasoning", "") or
                    str(_auto_thesis)[:200] if '_auto_thesis' in dir() else ""
                )
            except Exception:
                pass
            _scout_pattern_hint = ""
            try:
                _detected_patterns_for_anchor = (analysis_results or {}).get("sniper_score", {}).get("chart_patterns", []) if isinstance(analysis_results, dict) else []
                if isinstance(_detected_patterns_for_anchor, dict):
                    _detected_patterns_for_anchor = _detected_patterns_for_anchor.get("patterns", [])
                if _detected_patterns_for_anchor:
                    _first_pattern = _detected_patterns_for_anchor[0]
                    if isinstance(_first_pattern, dict):
                        _scout_pattern_hint = _first_pattern.get("name") or _first_pattern.get("type") or ""
                    elif isinstance(_first_pattern, str):
                        _scout_pattern_hint = _first_pattern
            except Exception:
                pass

            if _scout_alert_type:
                _task_anchor_content = (
                    f"**Detected setup:** {_scout_alert_type}"
                    + (f" — {_scout_setup_name}" if _scout_setup_name else "")
                    + "\n"
                    + (f"**Direction signal:** {_scout_direction_hint}\n" if _scout_direction_hint else "")
                    + (f"**Scout's read:** {_scout_thesis_hint}\n" if _scout_thesis_hint else "")
                    + (f"**Pattern flagged:** {_scout_pattern_hint}\n" if _scout_pattern_hint else "")
                    + "\n"
                    + "**Your job — find the opportunity:**\n"
                    + "1. Look at the LIVE chart. What do you actually see — fan ordering, recent crosses, BB state, slope direction, candles at the right edge?\n"
                    + "2. Walk the 10-point checklist. Which items are CONFIRMED and which are still FORMING?\n"
                    + "3. Apply fishing line theory: rod loading (SKIP), bending (WATCH the entry zone), at MAX TENSION and primed (TRADE_NOW immediately), or snapped (thesis dead).\n"
                    + "4. If 6+ checklist items are CONFIRMED RIGHT NOW → verdict TRADE_NOW with confidence 6+. Don't downgrade a ready setup to a SNIPE just because re_entry_conditions feel safer to write — the count binds the verdict.\n"
                    + "5. If a setup is FORMING but not yet 6+ confirmed → give a SNIPE with specific entry zone, invalidation, target, and 5+ re_entry_conditions that must flip true for the trade to fire.\n"
                    + "6. If the scout's read is wrong (chart shows different) → say so clearly and SKIP, or flip direction with reasoning.\n\n"
                    + "SKIP only if there is genuinely no thesis at all — no crosses, no fan structure, no patterns forming. A neutral fan with a recent cross or developing structure is WATCH territory, not SKIP."
                )
            else:
                _task_anchor_content = (
                    f"**Pair:** {instrument} M15\n\n"
                    + "**Your job — find the opportunity:**\n"
                    + "1. Look at the LIVE chart. What do you actually see — fan ordering, recent crosses, BB state, slope direction, candles at the right edge?\n"
                    + "2. Walk the 10-point checklist. Which items are CONFIRMED and which are still FORMING?\n"
                    + "3. Apply fishing line theory: rod loading (SKIP), bending (WATCH the entry zone), at MAX TENSION and primed (TRADE_NOW immediately), or snapped (thesis dead).\n"
                    + "4. If 6+ checklist items are CONFIRMED RIGHT NOW → verdict TRADE_NOW with confidence 6+. Don't downgrade a ready setup to a SNIPE just because re_entry_conditions feel safer to write — the count binds the verdict.\n"
                    + "5. If a setup is FORMING but not yet 6+ confirmed → give a SNIPE with specific entry zone, invalidation, target, and 5+ re_entry_conditions that must flip true for the trade to fire.\n\n"
                    + "SKIP only if there is genuinely no thesis at all — no crosses, no fan structure, no patterns forming. A neutral fan with a recent cross or developing structure is WATCH territory, not SKIP."
                )
            _validator_sections.insert(0, {
                "heading": "Your Job — Find the Opportunity",
                "content": _task_anchor_content,
            })
            # 2026-05-05: TA Picture section — wires TA's structured output through
            # to the local validator path. Heading contains "indicator" so it survives
            # the _local_keep filter at line ~6482. Validator's prompt
            # (ghost_validator_v1.md) explicitly asks for cascade_phase, ema_state,
            # bb_state — they were being filtered out before. With this section,
            # validator gets TA's narrative interpretation (velocity, BB state,
            # cascade_phase) instead of zero'd numbers from _v4_ema/_v4_scout dicts.
            #
            # 2026-05-07: Mutually exclusive with the Raw Indicators fallback
            # below — when TA ran, validator gets TA's narrative interpretation.
            # When TA was bypassed (gate.skip_ta_prefeed=true → ta_interpretation={}),
            # validator gets the raw numerical block instead. Same heading prefix
            # ("Indicator Data —") so the _local_keep filter catches both via
            # the existing "indicator" keyword. Token usage stays the same; we
            # just choose narrative-form OR raw-form, never both.
            _indicator_idx = 1 if _v4_scout_evidence else 0
            if _v4_ta_full or _v4_ta_narrative:
                _ta_picture_parts = []
                if _v4_ta_narrative:
                    _ta_picture_parts.append(f"**Narrative:** {_v4_ta_narrative}")
                if _v4_ta_phase:
                    _ta_picture_parts.append(f"**Cascade Phase:** {_v4_ta_phase}")
                if _v4_ta_ema_story:
                    _ta_picture_parts.append(f"**EMA State:** {_v4_ta_ema_story}")
                if _v4_ta_bb_story:
                    _ta_picture_parts.append(f"**BB State:** {_v4_ta_bb_story}")
                if _v4_ta_candle_tests:
                    _ta_picture_parts.append(f"**Candle Tests:** {_v4_ta_candle_tests}")
                if _v4_ta_rsi_state:
                    _ta_picture_parts.append(f"**RSI/Momentum:** {_v4_ta_rsi_state}")
                if _v4_ta_retracement:
                    _ta_picture_parts.append(f"**Retracement:** {_v4_ta_retracement}")
                if _v4_ta_conflicts:
                    _ta_picture_parts.append(
                        f"**Conflicting Signals:** "
                        f"{'; '.join(_v4_ta_conflicts) if isinstance(_v4_ta_conflicts, list) else _v4_ta_conflicts}"
                    )
                if _v4_ta_clarity and _v4_ta_clarity != "UNKNOWN":
                    _ta_picture_parts.append(f"**Clarity:** {_v4_ta_clarity}")
                if _ta_picture_parts:
                    _ta_picture_content = "\n\n".join(_ta_picture_parts)
                    _validator_sections.insert(_indicator_idx, {
                        "heading": "Indicator Data — TA Picture",
                        "content": _ta_picture_content,
                    })
            else:
                # TA was bypassed (gate.skip_ta_prefeed=true) or returned empty.
                # 2026-05-17 refactor: this block-building logic was extracted into
                # validator_block_builder.build_validator_indicator_block so the
                # ghost-replay test path produces IDENTICAL output. Adding a new
                # field = ONE edit in validator_block_builder.py.
                from validator_block_builder import (
                    build_validator_indicator_block,
                    compute_range_position_pct,
                    compute_prior_session_hl_pips,
                )
                _m15_candles_for_loc = (
                    candles_for_ta.get("M15", []) if isinstance(candles_for_ta, dict) else []
                )
                _location = {
                    "range_position_24bar_pct": compute_range_position_pct(_m15_candles_for_loc, lookback=24),
                    **compute_prior_session_hl_pips(_m15_candles_for_loc, instrument, session_bars=32),
                }
                _crosses_dict = {
                    "e21_e55": {
                        "current_orientation": ema_result.get("fan_direction", "?"),
                        "bars_since_last_flip": int(_bars_since_cross) if _cross_happened else None,
                        "cross_direction": ema_result.get("fan_direction"),
                    },
                    "e21_e100": {
                        "current_orientation": ema_result.get("fan_direction", "?"),
                        "bars_since_last_flip": ema_result.get("bars_since_cross2"),
                        "cross_direction": None,
                    },
                    "e55_e100": {
                        "current_orientation": ema_result.get("fan_direction", "?"),
                        "bars_since_last_flip": ema_result.get("bars_since_cross3"),
                        "cross_direction": ema_result.get("cross3_direction"),
                    },
                }
                _e100_dict = {
                    "role": _e100_role,
                    "dist_pips": _v4_e100_dist,
                    "candle_pattern_text": _e100_text_v4,
                    "candles_below_e100": ema_result.get("candles_below_e100", 0),
                    "candles_above_e100": ema_result.get("candles_above_e100", 0),
                    "last_close_vs_e100": ema_result.get("last_close_vs_e100", "unknown"),
                    "rejections_from_below": ema_result.get("e100_rejections_from_below", 0),
                    "rejections_from_above": ema_result.get("e100_rejections_from_above", 0),
                }
                _raw_indicator_content = build_validator_indicator_block(
                    pair=instrument,
                    direction=str(effective_direction or "").upper(),
                    ema={
                        "fan_direction": ema_result.get("fan_direction"),
                        "fan_state": ema_result.get("fan_state"),
                        "fan_ordered": ema_result.get("fan_ordered"),
                        "separation_pct": ema_result.get("separation_pct", 0),
                        "separation_velocity": ema_result.get("separation_velocity", 0),
                        "fan_velocity_trend": ema_result.get("fan_velocity_trend"),
                        "gap_price_100": ema_result.get("gap_price_100", 0),
                        "cascade_phase": ema_result.get("cascade_phase", 0),
                        "trend_health": ema_result.get("trend_health", 0),
                        "reversal_risk": ema_result.get("reversal_risk"),
                    },
                    bollinger={
                        "bb_squeeze": ema_result.get("bb_squeeze", False),
                        "bb_expanding": ema_result.get("bb_expanding", False),
                        "bb_contracting": ema_result.get("bb_contracting", False),
                        "bb_lower_pen": indicators.get("bb_lower_pen", 0),
                        "bb_upper_pen": indicators.get("bb_upper_pen", 0),
                        "bb_bandwidth": indicators.get("bb_bandwidth"),
                    },
                    momentum={
                        "rsi": indicators.get("rsi", 50),
                        "rsi_slope": indicators.get("rsi_slope", 0),
                        "rsi_recovery": _v4_rsi_recovery,
                        "stoch_k": indicators.get("stoch_k", 50),
                        "stoch_d": indicators.get("stoch_d", 50),
                        "macd_histogram": indicators.get("macd_histogram", 0),
                        "adx": adx_val,
                        "regime": regime,
                    },
                    crosses=_crosses_dict,
                    e100=_e100_dict,
                    location=_location,
                    patterns=detected_patterns,
                    divergence=divergence,
                    scout={
                        "alert_type": _v4_alert_type,
                        "e100_dist_pips": _v4_e100_dist,
                        "fan_delta_5bar": _v4_fan_delta,
                        "fan_delta_20bar": _v4_fan_delta_20,
                        "bb_delta_5bar": _v4_bb_delta,
                        "bb_delta_20bar": _v4_bb_delta_20,
                    },
                    session=(False, ""),
                )
                _validator_sections.insert(_indicator_idx, {
                    "heading": "Indicator Data — Raw",
                    "content": _raw_indicator_content,
                })
            if _v4_intel_text:
                _validator_sections.append({"heading": "Intelligence", "content": _v4_intel_text})
            # 2026-05-10 iter-20d wire-up: replace the old _v4_patterns_text
            # section with the detector pipeline that scored 19/19 on the
            # 19-trade cohort. detect_patterns_for_validator runs the 11
            # tunable detectors + mutual-exclusion + confirmation/invalidation
            # filtering, returns enriched fires. build_pattern_section renders
            # each fire with verbatim pattern_library.md quotes + per-fire
            # indicator-context evidence. body_only=True lets the validator-
            # framework wrap with the section heading the prompt expects.
            try:
                from scripts.pattern_library_quotes import build_pattern_section
                # 2026-05-11 iter-20d wire-up FIX: reuse the fires already computed
                # at chart-generation time (line ~5194) instead of re-detecting.
                # Falls back to local detection if for some reason fires weren't
                # computed (e.g., chart generation was skipped).
                _live_pattern_fires = locals().get("_v4_pattern_fires") or []
                if not _live_pattern_fires:
                    try:
                        from scripts.pattern_detectors import detect_patterns_for_validator
                        _live_pattern_fires = detect_patterns_for_validator(
                            candles_for_ta.get("M15", []),
                            fan_direction=str(ema_result.get("fan_direction", "mixed")),
                            phase=int(ema_result.get("cascade_phase", 0) or 0),
                            pair_hint=instrument,
                        )
                    except Exception:
                        _live_pattern_fires = []
                _live_pattern_section = build_pattern_section(_live_pattern_fires, body_only=True)
                # 2026-05-11: ALWAYS append the section so the validator knows the
                # pattern check ran. Previously this dropped the section when no
                # patterns fired (~57% of calls in today's audit), so the model
                # had no signal that the 11-detector pipeline was even consulted.
                # Empty-detection still gets a stub so the pattern-conflict-veto
                # rule in the prompt has explicit input either way.
                _validator_sections.append({
                    "heading": "Detected Patterns On This Chart",
                    "content": _live_pattern_section if _live_pattern_section else (
                        "No programmatic patterns detected on the most recent bars. "
                        "(11 detectors checked: hammer/pin, bullish engulfing, bearish "
                        "engulfing, morning/evening star, doji-at-extreme, ascending "
                        "triangle, descending triangle, channel, BB-squeeze breakout, "
                        "RSI/MACD divergence, plus mutual-exclusion + confirmation/"
                        "invalidation filters.) Pattern-conflict veto does not apply — "
                        "read structure visually from the chart."
                    ),
                })
            except Exception as _pat_exc:
                logger.warning("Live pattern detection failed: %s", _pat_exc)
                # Even on error, append a stub so prompt-rules referencing this
                # section don't reference a missing block.
                _validator_sections.append({
                    "heading": "Detected Patterns On This Chart",
                    "content": "Pattern detector failed for this cycle — read structure visually.",
                })

            # 2026-05-10 iter-20d wire-up: scout history block — as-of-now
            # win-rate aggregation for this setup × pair from closed live_trades.
            # n≥5 threshold for 🎯/⚠️ badges; smaller samples render as neutral
            # per iter 20a/20d calibration.
            try:
                from scripts.build_scout_history import fetch_as_of_history, format_scout_section
                from datetime import datetime as _now_dt, timezone as _now_tz
                _scout_dir = (_scout_direction_hint or "").upper() or None
                if _scout_setup_name and _scout_dir:
                    _scout_history = fetch_as_of_history(
                        _scout_setup_name,
                        instrument,
                        _now_dt.now(_now_tz.utc).isoformat(),
                    )
                    _scout_history_text = format_scout_section(_scout_history, _scout_dir)
                    if _scout_history_text:
                        _validator_sections.append({
                            "heading": "Scout History",
                            "content": _scout_history_text,
                        })
            except Exception as _sh_exc:
                logger.warning("Live scout history failed: %s", _sh_exc)

            # 2026-05-11 iter-20e wire-up: session window state — evaluated FRESH
            # via _compute_session_window so the validator sees PRIME/CAUTION/
            # OPEN/BLOCKED + owning_session + next_open_utc. Validator prompt
            # rules (ghost_validator_v1.md "SESSION-AWARE TRADING"):
            #   PRIME    → trust structural read, commit on 6+ checklist
            #   CAUTION  → downgrade TRADE_NOW to WATCH-with-snipe for owning open
            #   BLOCKED  → never TRADE_NOW, always WATCH-with-snipe
            #   OPEN     → normal phase-based judgment
            _sess = _compute_session_window(instrument, tc_get_fn=tc_get)
            _sess_line = f"Session gate: {_sess['state']}"
            if _sess.get('reason'):
                _sess_line = f"{_sess_line} — {_sess['reason']}"
            if _sess.get('owning_session'):
                _sess_line += f"\nOwning session: {_sess['owning_session']}"
            if _sess.get('next_open_utc'):
                _sess_line += f"\nNext owning-session open: {_sess['next_open_utc']}"
            _validator_sections.append({"heading": "Session Gate", "content": _sess_line})

            if _v4_reentry:
                _validator_sections.append({"heading": "Re-Entry Context", "content": _v4_reentry})
            _validator_sections.append({"heading": "Account", "content": (
                f"- Balance: {account_summary.get('balance', 'N/A')} | "
                f"Open trades: {account_summary.get('openTradeCount', 0)} | "
                f"Daily loss: {daily_loss_pct:.2f}%"
            )})
            if _learning_sections:
                _validator_sections.append({"heading": "Learning Context", "content": _learning_sections})
            if _validator_watch_text:
                _validator_sections.append({"heading": "Active Watches", "content": _validator_watch_text})
            if _validator_ann_text:
                _validator_sections.append({"heading": "Trader Annotations", "content": _validator_ann_text})

            # Build the params dict for unified validator
            # Include full indicator set (RSI, Stoch, ADX, MACD, BB, ATR) — not just EMA narrative
            _full_indicators = {**(_v4_ema if isinstance(_v4_ema, dict) else {})}
            _computed_ind = analysis_results.get("indicators", {}) if isinstance(analysis_results, dict) else {}
            for _ind_key in ("rsi", "stoch_k", "stoch_d", "adx", "macd_hist",
                             "bb_width", "bb_upper", "bb_lower", "bb_middle",
                             "atr", "close", "open", "high", "low",
                             "bb_width_prev", "bb_squeeze"):
                if _ind_key in _computed_ind and _computed_ind[_ind_key] is not None:
                    _full_indicators[_ind_key] = _computed_ind[_ind_key]

            # ── Compute story score + build thesis ──────────────────────
            # Quantify how compelling this setup is and frame it as a thesis
            # so the validator evaluates conviction, not raw data.
            _story_score = _compute_story_score(_v4_ind, _v4_ema, _v4_scout)

            # ── NEW (2026-03-29): Story score gating ──────────────────────
            # <50 = weak setup, block entirely (saves validator call cost)
            # ≥70 = strong setup, validator sees high conviction score
            # 50-69 = moderate, proceed normally
            #
            # EXCEPTION: Retracement entries get a pass on story_score.
            # During retracement, fan velocity near 0 and BB contracting
            # are EXPECTED. The score formula punishes both.
            # Tim's March 26 manual retracement trades scored 0-35 but
            # went 3W/1L (+11.1p). Detect retracement context by:
            #   1. Fan is ordered (E21>E55>E100 or reverse) but
            #   2. Fan state is peaked/contracting/stable (not expanding)
            #   3. Trend structure intact, just pulling back
            _fan_state_raw = (_v4_ema or {}).get('fan_state', '')
            _fan_ordered_raw = (_v4_ema or {}).get('fan_ordered', False)
            _is_retracement_context = (
                _fan_ordered_raw and
                _fan_state_raw in ('peaked', 'contracting', 'stable', 'decelerating')
            )
            _story_min = tc_get("gate.story_score_min", 50)
            if _story_score < _story_min and not _is_retracement_context:
                logger.info(
                    '⚡ [SNIPE DIRECT] %s BLOCKED: story_score=%d < %d — setup too weak to trade',
                    instrument, _story_score, _story_min
                )
                cycle_result['status'] = 'skipped'
                cycle_result['skip_reason'] = 'story_score_too_low'
                cycle_result['skip_detail'] = f'story_score={_story_score} < {_story_min} — weak setup'
                return cycle_result
            elif _story_score < _story_min and _is_retracement_context:
                logger.info(
                    '\u26a1 [SNIPE DIRECT] %s ALLOWED: story_score=%d < 50 but retracement '
                    'context (fan=%s ordered=%s) \u2014 passing to validator',
                    instrument, _story_score, _fan_state_raw, _fan_ordered_raw
                )

            _unified_params = {
                "pair": instrument,
                "chart_path": _v4_chart_path or (analysis_results.get("v4_chart_path", "") if isinstance(analysis_results, dict) else ""),
                "user_chart_path": (scout_context or {}).get("user_chart_path") if _user_chart_image else None,
                "indicators": _full_indicators,
                "data_sections": _validator_sections,
                "workspace_id": "forex-trading-team",
                "story_score": _story_score,
            }
            if isinstance(scout_context, dict) and scout_context.get("trader_annotations"):
                _unified_params["trader_annotations"] = scout_context["trader_annotations"]
            _auto_thesis = _build_thesis_from_ta(
                pair=instrument,
                ta_narrative=_v4_ta_narrative,
                ta_full=_v4_ta_full,
                indicators=_v4_ind,
                ema_result=_v4_ema,
                scout_context=_v4_scout,
                story_score=_story_score,
            )
            logger.info(
                "[STORY] %s: story_score=%d thesis=%s",
                instrument, _story_score, _auto_thesis[:120]
            )

            # ── Build task string for SwarmHandler validator agent ──
            # Frame as THESIS EVALUATION (same as Tim's chart submissions)
            # so the validator confirms/denies a conviction rather than
            # building one from scratch with raw data.
            _pair_display = instrument.replace("_", "/")

            # 2026-04-26 ship: BARE task body for the LOCAL 35B path.
            # Audit (audit_v1_canonical.py on 14 historical Opus-TRADE_NOW charts):
            #   v1-prompt + canonical 1 image + bare task = 12/14 actionable (86%).
            #   Same combo with rich Opus thesis-challenge framing dropped to 2/14.
            # The "Your job: does the structure SUPPORT this thesis?" framing
            # primes the model to evaluate-vs-commit and leak to WATCH/SKIP. The
            # bare framing lets the 35B form its own thesis from the chart, which
            # matches how it was distilled. Anthropic/Opus path keeps rich body —
            # only the local 35B branch gets the lean version.
            if _is_local_validator:
                _val_preamble = (
                    f"M15 chart — {_pair_display}. Read it fresh and form YOUR OWN "
                    f"thesis from the structure you see (story_score={_story_score} "
                    f"is informational only, not a directive).\n\n"
                    f"Return ONLY a ```json code block with: verdict (TRADE_NOW/WATCH/SKIP), "
                    f"direction (BUY/SELL), confidence (0-10), reasoning (start with CHART READ:), "
                    f"re_entry_conditions (list of {{field, op, value, reason}} dicts), "
                    f"re_entry_direction, re_entry_setup, watch_trigger (SPECIFIC prices: "
                    f"entry zone, invalidation, target), watch_for, snipe_entry_zone, "
                    f"snipe_invalidation, snipe_target, estimated_candles_to_entry, "
                    f"price_target_entry, watch_manifest (MANDATORY for WATCH).\n\n"
                )
            else:
                _val_preamble = (
                    f"📊 **TRADE THESIS — {_pair_display}**\n\n"
                    f"The technical analysis team has identified this setup:\n\n"
                    f"**THESIS:** {_auto_thesis}\n\n"
                    f"**Your job:**\n"
                    f"1. Look at the chart. Does the structure SUPPORT this thesis?\n"
                    f"2. Run the 10-point checklist — which items CONFIRM the thesis?\n"
                    f"3. If the thesis is right, give a SNIPE with entry conditions.\n"
                    f"4. If the thesis is wrong, explain what the chart ACTUALLY shows.\n"
                    f"5. Use fishing line theory — what phase is this, and what comes next?\n\n"
                    f"Return structured JSON with: verdict (TRADE_NOW/WATCH/SKIP), direction, confidence (1-10), "
                    f"story_score ({_story_score}), "
                    f"checklist (dict of all 10 items), reasoning (detailed — start with CHART READ: "
                    f"describe what you see, then evaluate the thesis using fishing line theory), "
                    f"re_entry_conditions (list of {{field, op, value, reason}} dicts), "
                    f"re_entry_direction, re_entry_setup, watch_trigger (SPECIFIC prices: "
                    f"entry zone like 212.45-212.60, invalidation like below 212.20, target), "
                    f"watch_for (plain english trigger summary with prices), "
                    f"estimated_candles_to_entry (integer), price_target_entry (price level), "
                    f"watch_manifest (MANDATORY for WATCH — include fishing_line with entry_zone_pips, "
                    f"direction, time_limit_candles; trigger_conditions with progress_pct for each; "
                    f"invalidation_conditions; trajectory_assessment with velocity and death_flags).\n\n"
                )

            _val_task_parts = [_chart_status_warning, _val_preamble]
            # Supporting data — TA report, intelligence, indicators.
            # Local 35B path skips the heavier sections (TA narrative, intelligence) —
            # those are what biased it toward verify-vs-commit. Indicators-only stays
            # so the model has structured numbers alongside the chart vision.
            if _is_local_validator:
                # 2026-04-27: Use singular "indicator" — section heading is
                # "Indicator Data: PAIR M15 (computed — ...)" without an 's'.
                # Plural "indicators" silently failed substring match for months,
                # leaving the local validator with NO numerical EMA values and
                # forcing it to read tightly-clustered EMAs from the chart image
                # alone — which led to "tangled" mislabels on stalled-fan charts.
                # 2026-04-29: added "scout" so Scout Evidence section reaches the
                # local validator. Without it, validator can't see alert_type and the
                # tier1_setup_catalog.md (loaded as a skill file) is dead weight —
                # validator wouldn't know which catalog entry to consult on Tier 1
                # alerts (C1/C3/C4/C5/C8/C9/C11). Scout Evidence is mostly facts
                # (alert_type, setup_id, WR, PF), not the heavy thesis prose that
                # caused the prior verify-vs-commit bias.
                # 2026-05-10: added "detected patterns" and "session" so the
                # iter-20d wire-up sections reach the local 35B path. The
                # pattern section feeds the pattern-conflict veto inside the
                # CONTINUATION composite; the session gate section feeds the
                # BLOCKED→WATCH downgrade rule. "scout" already in set covers
                # the Scout History section (heading contains "scout").
                _local_keep = {
                    "indicator", "live indicator", "live_indicator",
                    "scout", "your job", "find the opportunity",
                    "detected patterns", "session",
                }
                for _sec in _validator_sections:
                    _heading = (_sec.get("heading") or "").lower()
                    if any(k in _heading for k in _local_keep):
                        _val_task_parts.append(f"## {_sec.get('heading', 'Data')}\n{_sec.get('content', '')}")
            else:
                for _sec in _validator_sections:
                    _val_task_parts.append(f"## {_sec.get('heading', 'Data')}\n{_sec.get('content', '')}")
            # JSON format reminder at the end
            _val_task_parts.append(
                "\n---\n"
                "After using your tools and analyzing the chart, respond with ONLY a ```json code block. "
                "No prose outside the JSON."
            )
            _val_task_string = "\n\n".join(_val_task_parts)

            _validator_start = time.time()
            # ── Gate 1 block: skip expensive validator call if no signal ──
            # User requests and qualified scout alerts bypass Gate1.
            # Only EARLY_WARNING and unqualified alerts get gated.
            _triggered_by = (scout_context or {}).get("triggered_by", "")
            _user_requested = _triggered_by in ("snipe",) or _is_manual_run
            _alert_type = (scout_context or {}).get("alert_type", "")
            _scout_qualified = _alert_type in ("CRITERIA_MET", "WATCH", "TRADE_NOW")
            _gate1_passed = pre_validator_confluence.get("breakdown", {}).get("gate1_sniper", {}).get("pass", False)

            # ── Kronos Filter: pre-validator veto ────────────────────────────────
            # Fail-open wrap — any exception here must never block a scout cycle.
            try:
                from tuning_config import TUNING
                from flight_recorder import flight as _kf_flight, FlightStage as _kf_FlightStage
                if TUNING["kronos.enabled"]["value"] and TUNING["kronos.filter_enabled"]["value"]:
                    from kronos_runtime import get_kronos_filter  # singleton provider — Task 10
                    kflt = get_kronos_filter()
                    if kflt is not None:
                        # Kronos emits 'buy'/'sell'; scout uses 'bullish'/'bearish'/'neutral'
                        _norm_map = {"bullish": "buy", "bearish": "sell"}
                        _norm_scout_dir = _norm_map.get(effective_direction)
                        if _kf_flight:
                            _kf_flight.record(_kf_FlightStage.KRONOS_FILTER_CHECK,
                                              pair=instrument, cycle_id=_cycle_id,
                                              data={"pair": instrument,
                                                    "scout_direction": effective_direction,
                                                    "normalized_direction": _norm_scout_dir})
                        if _norm_scout_dir is None:
                            # neutral/unknown direction — no opinion to compare against, skip veto
                            if _kf_flight:
                                _kf_flight.record(
                                    _kf_FlightStage.KRONOS_FILTER_PASS,
                                    pair=instrument, cycle_id=_cycle_id,
                                    data={"pair": instrument,
                                          "reason": f"scout_direction={effective_direction!r} not buy/sell — skipping veto"},
                                )
                        else:
                            kd = kflt.check(pair=instrument, scout_direction=_norm_scout_dir)
                            if kd.outcome.value == "reject":
                                if _kf_flight:
                                    _kf_flight.record(_kf_FlightStage.KRONOS_FILTER_REJECT,
                                                      pair=instrument, cycle_id=_cycle_id,
                                                      data={"pair": instrument,
                                                            "reason": kd.reason,
                                                            "kronos_direction": kd.kronos_direction,
                                                            "kronos_confidence": kd.kronos_confidence})
                                logger.info("[KRONOS_FILTER] cycle rejected: %s", kd.reason)
                                cycle_result["status"] = "skipped"
                                cycle_result["skip_reason"] = f"kronos_filter: {kd.reason}"
                                cycle_result["cycle_end"] = datetime.now(timezone.utc).isoformat()
                                return cycle_result
                            if _kf_flight:
                                _kf_flight.record(_kf_FlightStage.KRONOS_FILTER_PASS,
                                                  pair=instrument, cycle_id=_cycle_id,
                                                  data={"pair": instrument, "reason": kd.reason})
            except Exception as kronos_exc:  # never block scout because Kronos misbehaved
                try:
                    logger.warning("[KRONOS_FILTER] exception, failing open: %s", kronos_exc)
                except Exception:
                    pass
            # ─────────────────────────────────────────────────────────────────────

            # 2026-05-05: Gate 1 made tunable. Tim's call: kill it. Gate 1 was a cost
            # filter for Opus calls; on local 35B every call is free. With the TA Picture
            # section now reaching the validator, the validator can correctly SKIP
            # weak setups itself — Gate 1 was rejecting valid mature trends with
            # directional bias as "no active setup" (e.g. fan=contracting bullish,
            # cross=40bars, story=50/100). Set gate.gate1_enabled=False via
            # tuning_overrides to disable.
            _gate1_killswitch_enabled = bool(tc_get("gate.gate1_enabled", True))
            if _gate1_killswitch_enabled and not _gate1_passed and not _user_requested and not _scout_qualified:
                logger.info("[GATE1 BLOCK] %s: Gate1 fail — skipping validator. Reason: %s",
                            instrument, pre_validator_confluence.get("summary", "no signal")[:100])
                validator_result = {
                    "response": json.dumps({
                        "verdict": "GATE1_BLOCK", "confidence": 0.0,
                        "direction": None, "overall_passed": False,
                        "reasoning": (
                            f"Gate 1 failed — no market structure found "
                            f"(fan direction mixed/flat, story score below 30). "
                            f"Validator not called. "
                            f"{pre_validator_confluence.get('summary','')}"
                        ),
                        "re_entry_conditions": [],
                    }),
                    "tool_calls": [],
                }

            # ── SNIPE FAST PATH ──────────────────────────────────────────────
            # When a snipe fires with strong score + Gate1 pass → enter immediately.
            # Skip the 90-second vision validator — the snipe conditions ARE the
            # technical validation. 86% of snipes in backtest were winning trades.
            # Only blocked by: Gate1 fail (stale setup) | news | momentum trap.
            elif _is_snipe_trigger and _gate1_passed:
                _snipe_buy  = sniper_result.get("buy_score", 0)  if isinstance(sniper_result, dict) else 0
                _snipe_sell = sniper_result.get("sell_score", 0) if isinstance(sniper_result, dict) else 0
                _snipe_thresh = sniper_result.get("threshold", 12) if isinstance(sniper_result, dict) else 12
                _snipe_dir  = sniper_result.get("direction", "") if isinstance(sniper_result, dict) else ""
                _snipe_h4   = sniper_result.get("h4_bias", "")  if isinstance(sniper_result, dict) else ""
                _fp_score   = max(_snipe_buy, _snipe_sell)
                _fp_dir     = "BUY" if _snipe_buy >= _snipe_sell else "SELL"

                # Check news (quick DB query — same as full validator)
                _fp_news_clear = True
                try:
                    import sqlite3 as _fpndb
                    _fpncur = _fpndb.connect(
                        _TRADING_FOREX_DB, timeout=3
                    ).execute(
                        """SELECT COUNT(*) FROM news_events
                           WHERE impact_level IN ('high','HIGH','red','RED')
                           AND is_upcoming = 1
                           AND (currencies_affected LIKE ? OR pairs_affected LIKE ?)
                           AND datetime(event_time) BETWEEN datetime('now','-15 minutes')
                                                        AND datetime('now','+60 minutes')""",
                        (f"%{instrument[:3]}%", f"%{instrument}%")
                    )
                    _fp_news_clear = (_fpncur.fetchone()[0] == 0)
                except Exception:
                    pass

                # Momentum trap: both RSI AND Stoch at extreme = buying exhaustion
                _fp_ind = indicators if isinstance(indicators, dict) else {}
                _fp_rsi   = float(_fp_ind.get("rsi", 50))
                _fp_stoch = float(_fp_ind.get("stoch_k", 50))
                _fp_trap  = (_fp_dir == "BUY"  and _fp_rsi > 78 and _fp_stoch > 90) or \
                            (_fp_dir == "SELL" and _fp_rsi < 22 and _fp_stoch < 10)

                # H4 opposing: snipe BUY but H4 strongly bearish = skip
                _fp_h4_ok = not (_fp_dir == "BUY" and _snipe_h4 in ("bear", "strongly_bearish")) and \
                            not (_fp_dir == "SELL" and _snipe_h4 in ("bull", "strongly_bullish"))

                _fp_passes = (
                    _fp_score >= _snipe_thresh and
                    _fp_news_clear and
                    not _fp_trap and
                    _fp_h4_ok
                )

                if _fp_passes:
                    logger.info(
                        "⚡ [SNIPE FAST PATH] %s: score=%d/%d dir=%s Gate1=PASS news=%s trap=%s h4=%s → CONFIRM",
                        instrument, _fp_score, _snipe_thresh, _fp_dir,
                        "clear" if _fp_news_clear else "BLOCKED",
                        "BLOCKED" if _fp_trap else "ok",
                        _snipe_h4
                    )
                    validator_result = {
                        "response": json.dumps({
                            "verdict": "TRADE_NOW",
                            "direction": _fp_dir,
                            "confidence": round(min(1.0, _fp_score / 20.0), 2),
                            "overall_passed": True,
                            "reasoning": (
                                f"SNIPE FAST PATH: Sniper {_fp_dir} score={_fp_score} (threshold={_snipe_thresh}). "
                                f"Gate1 PASS. News clear. No momentum trap (RSI={_fp_rsi:.0f}, Stoch={_fp_stoch:.0f}). "
                                f"H4={_snipe_h4}. Entering at snipe conditions."
                            ),
                            "sl_atr": 2.5,
                            "re_entry_conditions": [],
                            "watch_for": "",
                        }),
                        "tool_calls": [],
                    }
                else:
                    _fp_block_reason = []
                    if _fp_score < _snipe_thresh: _fp_block_reason.append(f"score {_fp_score}<{_snipe_thresh}")
                    if not _fp_news_clear: _fp_block_reason.append("news risk")
                    if _fp_trap: _fp_block_reason.append(f"momentum trap RSI={_fp_rsi:.0f} Stoch={_fp_stoch:.0f}")
                    if not _fp_h4_ok: _fp_block_reason.append(f"H4 opposing ({_snipe_h4})")
                    logger.info(
                        "⚡ [SNIPE FAST PATH] %s blocked: %s — falling through to full validator",
                        instrument, ", ".join(_fp_block_reason)
                    )
                    # Fall through to full unified validator below
                    if flight:
                        flight.record(FlightStage.VALIDATOR_CALL, pair=instrument, cycle_id=_cycle_id,
                                      note="Snipe fast path blocked — running unified validator")
                    validator_result = _agent_task(
                        "validator", _val_task_string,
                        context={"instrument": instrument, "from_cycle": True},
                        # max_tool_rounds=0: single-shot vision (no tools) for automated cycles.
                        # Pre-built prompt has intelligence/indicators/scout/annotations/patterns;
                        # tools stay defined in team_setup for floor-chat invocations.
                        max_tokens=4096, timeout=900.0, max_tool_rounds=0,
                        images=_v4_images_for_call,
                    )
            # ─────────────────────────────────────────────────────────────────

            else:
              if flight:
                _chart_note = "live chart" if not _v4_chart_missing else "NO CHART (chart generation failed)"
                # Log the full data package summary so we can audit what the validator received
                _pkg_summary = {
                    "chart": _chart_note,
                    "data_sections": len(_validator_sections),
                    "section_headings": [s.get("heading", "?")[:40] for s in _validator_sections],
                    "has_intelligence": any("Intelligence" in s.get("heading", "") for s in _validator_sections),
                    "has_indicators": any("Indicator Data" in s.get("heading", "") for s in _validator_sections),
                    "has_scout": any("Scout" in s.get("heading", "") for s in _validator_sections),
                    "has_annotations": any("Annotation" in s.get("heading", "") for s in _validator_sections),
                    "has_patterns": any("Pattern" in s.get("heading", "") for s in _validator_sections),
                    "workspace": _unified_params.get("workspace_id"),
                }
                flight.record(FlightStage.VALIDATOR_CALL, pair=instrument, cycle_id=_cycle_id,
                              data=_pkg_summary,
                              note=f"Unified validator: {len(_validator_sections)} sections, {_chart_note}")
              # timeout=900 covers physical queue wait when scout fires 5 pairs concurrently
              # on the local 35B (each ~120s, cycle-5 sees ~625s wall time). Prior 180s killed cycles 3-5.
              validator_result = _agent_task(
                  "validator", _val_task_string,
                  context={"instrument": instrument, "from_cycle": True},
                  # max_tool_rounds=0: single-shot vision for automated cycles.
                  max_tokens=2500, timeout=900.0, max_tool_rounds=0,
                  images=_v4_images_for_call,
              )

            # Log validator's tool calls individually (what it queried from DB)
            for tc in validator_result.get("tool_calls", []):
                tool_name = tc.get("tool", "?")
                output = tc.get("output_preview", tc.get("output", ""))
                # Parse output for key info
                output_summary = ""
                try:
                    parsed = json.loads(output) if isinstance(output, str) else output
                    if isinstance(parsed, dict):
                        if parsed.get("verdict"):
                            output_summary = f"verdict={parsed['verdict']}, confidence={parsed.get('confidence', '?')}"
                            hs = parsed.get("historical_stats", {})
                            if hs:
                                output_summary += f", win_rate={hs.get('overall_win_rate', '?')}%, PF={hs.get('best_profit_factor', '?')}, trades={hs.get('total_trades', '?')}"
                            warnings = parsed.get("warnings", [])
                            if warnings:
                                output_summary += f", warnings: {'; '.join(str(w) for w in warnings[:3])}"
                        elif parsed.get("best_params") is not None:
                            bp = parsed["best_params"]
                            output_summary = f"{len(bp)} params found" + (f", best: {bp[0].get('setup', '?')} ({bp[0].get('win_rate', '?')}% win, PF={bp[0].get('profit_factor', '?')})" if bp else "")
                        elif parsed.get("patterns") is not None:
                            output_summary = f"{len(parsed['patterns'])} loss patterns"
                        else:
                            output_summary = str(output)[:200]
                except Exception:
                    output_summary = str(output)[:200]
                _swarm_send_message(
                    "validator", "cycle_orchestrator",
                    f"[DB QUERY] {tool_name} → {output_summary}",
                )
            
            # Flight: validator DB queries
            if flight:
                _val_tool_calls = validator_result.get("tool_calls", [])
                flight.record(FlightStage.VALIDATOR_DB, pair=instrument, cycle_id=_cycle_id, data={
                    "tool_calls": len(_val_tool_calls),
                    "tools_used": list(set(tc.get("tool", "?") for tc in _val_tool_calls)),
                }, duration_ms=(time.time() - _validator_start) * 1000,
                note=f"{len(_val_tool_calls)} DB queries")

            # Log the full verdict
            _swarm_send_message(
                "validator", "cycle_orchestrator",
                f"[VERDICT] {validator_result.get('response', '')[:800]}",
            )
            
            # Parse validator response
            # Default is SKIP (not HOLD) — HOLD only appears as a valid mapped output, never as a silent fallback.
            # If JSON parse fails entirely below, SKIP is the correct conservative default.
            validation_results = {
                "verdict": "SKIP",
                "confidence": 0.2,
                "reasoning": "JSON parse failed — validator response could not be decoded. Defaulting to SKIP.",
                "overall_passed": False,
                "recommendation": "skip",
                "tool_calls": validator_result.get("tool_calls", []),
                "agent_response": validator_result.get("response", ""),
            }
            # Try to extract structured JSON from response.
            #
            # Bug fixed 2026-04-27: parse-success was inferred from heuristics on the
            # placeholder values, AFTER the V4→V3 verdict mapping had already mutated
            # the placeholder "SKIP" to "WATCH". When both primary parse and retry
            # failed, validation_results was left with verdict="WATCH", confidence=0.2,
            # direction=None — which slipped past the watch-creation gate at line ~8020
            # and produced garbage watches with NULL direction. Now: parse success is
            # tracked explicitly, V4 mapping runs only after a successful parse, and
            # retry failure resets verdict to GATE1_BLOCK so downstream skips the watch.
            import re as _re

            _V4_TO_V3_VERDICT = {
                "TRADE_NOW": "CONFIRM",
                "CONFIRM":   "CONFIRM", # pass-through (V3 legacy)
                "WATCH":     "WATCH",
                "SNIPE":     "WATCH",   # legacy alias
                # 2026-05-03: SKIP/REJECT no longer remap to WATCH. The original
                # design assumed validator would always populate re_entry_conditions
                # even on SKIP/REJECT. The current ghost_validator_v1 prompt teaches
                # the model "if you can't reach 5 conditions, return SKIP with empty
                # conditions" — when that legitimate SKIP got remapped to WATCH, a
                # garbage snipe was built from sparse regex-extracted prose. Now
                # SKIP and REJECT are honored — no snipe gets created.
                "SKIP":      "SKIP",    # honor model's SKIP — no snipe
                "HOLD":      "WATCH",   # legacy V3 alias kept for backwards compat
                "REJECT":    "REJECT",  # honor explicit reject — no snipe
                "GATE1_BLOCK": "REJECT",  # pre-validator block — no validator ran, no snipe
                "VALIDATOR_PARSE_FAIL": "REJECT",  # validator ran but output unparseable — no snipe
            }

            def _try_extract_json(text: str):
                """Return parsed dict or None. Tries direct → code-block → bracket-count."""
                if not text:
                    return None
                # Direct parse
                try:
                    p = json.loads(text)
                    if isinstance(p, dict):
                        return p
                except (json.JSONDecodeError, TypeError):
                    pass
                # Code-block fallback (greedy to capture nested braces)
                cb = _re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, _re.DOTALL)
                if cb:
                    try:
                        p = json.loads(cb.group(1))
                        if isinstance(p, dict):
                            return p
                    except json.JSONDecodeError:
                        pass
                # Bracket-counting: anchor on "verdict", walk back to {, depth-count
                vi = text.find('"verdict"')
                if vi != -1:
                    bp = text.rfind('{', 0, vi)
                    if bp != -1:
                        depth = 0
                        for i in range(bp, len(text)):
                            if text[i] == '{':
                                depth += 1
                            elif text[i] == '}':
                                depth -= 1
                                if depth == 0:
                                    try:
                                        p = json.loads(text[bp:i + 1])
                                        if isinstance(p, dict):
                                            return p
                                    except json.JSONDecodeError:
                                        pass
                                    break
                return None

            def _apply_v4_post_processing():
                """V4→V3 verdict mapping, direction propagation, numeric defaults, conf
                normalization. Idempotent — call after any successful parse."""
                _v4_verdict = (validation_results.get("verdict") or "").upper()
                if _v4_verdict in _V4_TO_V3_VERDICT:
                    validation_results["v4_verdict"] = _v4_verdict  # preserve original
                    validation_results["verdict"] = _V4_TO_V3_VERDICT[_v4_verdict]
                    logger.info("[V4] Verdict mapped: %s → %s", _v4_verdict, validation_results["verdict"])

                # V4: validator provides direction — propagate to trade_params
                _v4_direction = validation_results.get("direction")
                if _v4_direction and _v4_direction.upper() in ("BUY", "SELL"):
                    validation_results["v4_direction"] = _v4_direction.upper()
                    trade_params["direction"] = _v4_direction.lower()
                    logger.info("[V4] Direction from validator: %s", _v4_direction.upper())

                # Sanitize numeric fields the LLM may return as null
                for _nk, _nv in (
                    ("confidence", 0.5), ("historical_win_rate", 0),
                    ("historical_profit_factor", 0), ("historical_trade_count", 0),
                ):
                    if validation_results.get(_nk) is None:
                        validation_results[_nk] = _nv

                # Confidence scale normalization (0-10 int → 0.0-1.0).
                # Both validator prompts spec confidence as 0-10 int = count of
                # confirmed checklist items. Downstream gates expect 0.0-1.0.
                _raw_conf = validation_results.get("confidence", 0.5)
                if isinstance(_raw_conf, (int, float)) and _raw_conf > 1.0:
                    _normalized_conf = round(float(_raw_conf) / 10.0, 3)
                    validation_results["confidence"] = _normalized_conf
                    logger.info("[CONF NORMALIZE] confidence %s → %.3f (0-10 scale → 0.0-1.0)",
                                _raw_conf, _normalized_conf)

                _db_ev = validation_results.get("db_evidence")
                if isinstance(_db_ev, dict):
                    for _dk in ("best_win_rate", "best_profit_factor", "best_trade_count", "total_pips"):
                        if _db_ev.get(_dk) is None:
                            _db_ev[_dk] = 0

            # ── Primary parse ────────────────────────────────────────────────
            _parsed = _try_extract_json(validator_result.get("response", ""))

            # ── Retry on parse failure ───────────────────────────────────────
            # Validator LLM occasionally returns truncated or malformed JSON.
            # One retry recovers most cases without the full-pipeline cost.
            if _parsed is None:
                logger.warning("Validator JSON parse failed for %s — retrying once", instrument)
                try:
                    import time as _retry_time
                    _retry_time.sleep(3)
                    _retry_result = _agent_task(
                        "validator", _val_task_string,
                        context={"instrument": instrument, "from_cycle": True},
                        # max_tool_rounds=0: single-shot vision retry on JSON parse failure.
                        max_tokens=4096, timeout=900.0, max_tool_rounds=0,
                        images=_v4_images_for_call,
                    )
                    _parsed = _try_extract_json(_retry_result.get("response", ""))
                    if _parsed is not None:
                        logger.info("Validator JSON retry succeeded for %s", instrument)
                except Exception as _retry_exc:
                    logger.warning("Validator retry call failed for %s: %s", instrument, _retry_exc)

            # ── Apply parsed data, OR fall back to GATE1_BLOCK on total failure ──
            if _parsed is not None:
                validation_results.update(_parsed)
                logger.info("Validator parsed keys: %s", list(
                    k for k in validation_results.keys()
                    if k not in ("tool_calls", "agent_response", "reasoning")
                ))
                _apply_v4_post_processing()
            else:
                # Both attempts failed. Cannot create a watch from unparseable output —
                # we have no direction, no conditions, no real verdict. Use a distinct
                # verdict label (VALIDATOR_PARSE_FAIL) so dashboards/queries can
                # distinguish this real silent-failure mode from the legitimate
                # pre-validator GATE1_BLOCK (line 6566) — both block downstream snipe
                # creation but mean different things.
                validation_results["verdict"] = "VALIDATOR_PARSE_FAIL"
                validation_results["confidence"] = 0.0
                validation_results["reasoning"] = (
                    "JSON parse failed twice — validator output unparseable. "
                    "No watch created (would have NULL direction)."
                )
                logger.warning(
                    "[VALIDATOR PARSE FAIL] %s: both attempts unparseable — "
                    "verdict=VALIDATOR_PARSE_FAIL to suppress garbage watch", instrument)

                # 2026-05-05: capture raw responses on parse-fail to a persistent
                # location so we can root-cause the failure mode. /tmp dumps get
                # wiped; this writes to the logs dir which survives reboots.
                # Pattern: prose without ```json``` wrapper, truncated JSON, or
                # malformed JSON all hit this path.
                try:
                    import os as _os, time as _ts_pf, datetime as _dt_pf
                    _pf_dir = _os.path.join(
                        _os.path.dirname(_os.path.abspath(__file__)),
                        "..", "..", "Source", "logs", "validator_parse_fails"
                    )
                    _pf_dir = _os.path.abspath(_pf_dir)
                    _os.makedirs(_pf_dir, exist_ok=True)
                    _ts_str = _dt_pf.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
                    _pf_path = _os.path.join(
                        _pf_dir, f"{instrument}_{_ts_str}.txt"
                    )
                    _raw_resp = (validator_result.get("response") or "")
                    _retry_resp = ""
                    try:
                        _retry_resp = (_retry_result.get("response") or "")  # noqa
                    except NameError:
                        pass
                    with open(_pf_path, "w") as _pf_f:
                        _pf_f.write(f"=== VALIDATOR PARSE FAIL ===\n")
                        _pf_f.write(f"timestamp: {_ts_str}\n")
                        _pf_f.write(f"pair: {instrument}\n")
                        _pf_f.write(f"primary_response_len: {len(_raw_resp)}\n")
                        _pf_f.write(f"retry_response_len: {len(_retry_resp)}\n")
                        _pf_f.write(f"\n=== PRIMARY RESPONSE ===\n{_raw_resp}\n")
                        if _retry_resp:
                            _pf_f.write(f"\n=== RETRY RESPONSE ===\n{_retry_resp}\n")
                    logger.warning(
                        "[VALIDATOR PARSE FAIL DUMP] %s wrote raw to %s "
                        "(primary=%d chars, retry=%d chars)",
                        instrument, _pf_path, len(_raw_resp), len(_retry_resp)
                    )
                except Exception as _pf_exc:
                    logger.warning("[VALIDATOR PARSE FAIL DUMP] failed: %s", _pf_exc)

            # Extract raw DB evidence from validate_full tool results
            # This is authoritative — bypasses LLM repackaging which drops fields
            _val_tool_calls = validator_result.get("tool_calls", [])
            for _tc in _val_tool_calls:
                if _tc.get("tool") == "validate_full":
                    try:
                        _raw = _tc.get("output", _tc.get("result", ""))
                        _raw_parsed = json.loads(_raw) if isinstance(_raw, str) else _raw
                        if isinstance(_raw_parsed, dict):
                            _v = _raw_parsed.get("validation", {})
                            if isinstance(_v, dict):
                                _hs = _v.get("historical_stats", {})
                                if isinstance(_hs, dict) and _hs.get("total_trades", 0) > 0:
                                    _raw_db_ev = {
                                        "win_rate": _hs.get("overall_win_rate", 0),
                                        "profit_factor": _hs.get("best_profit_factor", 0),
                                        "trade_count": _hs.get("total_trades", 0),
                                        "loss_patterns": [p.get("pattern", str(p)) for p in (_raw_parsed.get("loss_patterns", {}).get("patterns", []))] if isinstance(_raw_parsed.get("loss_patterns"), dict) else [],
                                    }
                                    validation_results["_raw_db_evidence"] = _raw_db_ev
                                    logger.info("[DB_EVIDENCE_RAW] From validate_full tool: WR=%.1f PF=%.2f TC=%d",
                                               float(_raw_db_ev["win_rate"]), float(_raw_db_ev["profit_factor"]), int(_raw_db_ev["trade_count"]))
                    except Exception as _raw_exc:
                        logger.debug("Could not extract raw DB evidence: %s", _raw_exc)

            # ── V4: Confidence floor removed ──
            # Old system used DB backtest evidence to enforce minimum confidence.
            # V4: Validator's vision confidence IS the authority — no override.
            # The validator sees the chart like a human. Trust what it sees.

            # ── SETUP LEARNER: Evaluate conditions against backtest data ──
            # Runs on every cycle (not just SNIPER_DIRECT) to discover hidden edge
            try:
                from Source.setup_learner import evaluate_conditions as _eval_conds
                _sc_snap = scout_context or {}
                _learn_regime = _detect_regime(sniper_data)
                _learn_dir = "buy" if _sc_snap.get("direction", "").upper() in ("BUY", "BULL", "BULLISH") else "sell"
                _learn_ind = {
                    "rsi": sniper_data.get("indicators", {}).get("rsi", 50),
                    "stoch_k": sniper_data.get("indicators", {}).get("stoch_k", 50),
                    "adx": sniper_data.get("indicators", {}).get("adx", 25),
                    "bb_width": sniper_data.get("indicators", {}).get("bb_width", 0.005),
                }
                _learn_result = _eval_conds(instrument, _learn_regime, _learn_ind, _learn_dir,
                                           sniper_score=sniper_data.get("indicators", {}).get("v4_max_score", 0))
                if _learn_result and _learn_result.get("status") == "validated":
                    _swarm_send_message("cycle_orchestrator", "reporter",
                        f"[SETUP LEARNER] ✅ Conditions for {instrument} ({_learn_dir}) validated: "
                        f"{_learn_result['backtest']['win_rate']}% WR across {_learn_result['backtest']['trades']} trades")
            except Exception as _learn_exc:
                logger.debug("Setup learner: %s", _learn_exc)

            # Flight: validator verdict
            if flight:
                _vr = validation_results if isinstance(validation_results, dict) else {}
                flight.record(FlightStage.VALIDATOR_VERDICT, pair=instrument, cycle_id=_cycle_id, data={
                    "verdict": _vr.get("verdict", "HOLD"),
                    "confidence": _vr.get("confidence", 0),
                    "direction": _vr.get("direction"),
                    "reasoning": str(_vr.get("reasoning", ""))[:500],
                    "chart_read": str(_vr.get("chart_read", ""))[:300],
                    "setup_identified": _vr.get("setup_identified", ""),
                    "checklist_score": _vr.get("checklist_score"),
                    "two_pass": _vr.get("two_pass", False),
                    "vault_education_used": _vr.get("vault_education_used", False),
                    "teaching_images_count": _vr.get("teaching_images_count", 0),
                    "re_entry_count": len(_vr.get("re_entry_conditions", []) or []),
                    "missing_items": _vr.get("missing_items", [])[:5],
                    "re_entry_conditions": (_vr.get("re_entry_conditions", []) or [])[:5],
                    "flags": _vr.get("flags", [])[:3],
                    "best_setup": _vr.get("best_setup"),
                    "win_rate": _vr.get("historical_win_rate", 0),
                    "education_reference": str(_vr.get("education_reference", ""))[:200],
                    "elapsed_seconds": _vr.get("elapsed_seconds"),
                }, duration_ms=(time.time() - _validator_start) * 1000,
                note=f"{_vr.get('verdict', '?')} dir={_vr.get('direction','?')} conf={_vr.get('confidence',0)} "
                     f"setup={_vr.get('setup_identified', '?')} 2pass={_vr.get('two_pass', False)}")

            self._post_result(
                task_id, "data_validator", MessageType.VALIDATION_RESULT,
                f"Validation: verdict={validation_results.get('verdict', 'N/A') if isinstance(validation_results, dict) else 'N/A'}, "
                f"setup={validation_results.get('best_setup', 'none') if isinstance(validation_results, dict) else 'none'}, "
                f"confidence={validation_results.get('confidence', 0) if isinstance(validation_results, dict) else 0:.0%}",
                validation_results if isinstance(validation_results, dict) else {},
            )

            # ── Ghost Validator: 35B parallel comparison (non-blocking) ──
            # Fires in background thread — does NOT affect trade decisions.
            # Logs to ghost_verdicts for tracking 35B vs Opus match rate.
            try:
                from tuning_config import get as _tc_get
                if _tc_get("ghost.enabled", False) and _tc_get("ghost.mode", "batch") == "realtime":
                    import threading as _ghost_threading

                    def _run_ghost_validator(
                        _g_instrument, _g_chart_path, _g_input_prompt,
                        _g_anthropic_verdict, _g_vtd_id, _g_narrative,
                    ):
                        try:
                            import urllib.request, urllib.error
                            # Check if 35B server is running on port 11502
                            try:
                                urllib.request.urlopen("http://127.0.0.1:11502/v1/models", timeout=2)
                            except Exception:
                                return  # 35B not running, skip silently

                            # Load ghost prompt
                            import os
                            _g_prompt_path = os.path.expanduser(
                                _tc_get("ghost.prompt_path",
                                        "~/Jarvis/Forex Trading Team/Prompts/ghost_validator_v1.md"))
                            try:
                                with open(_g_prompt_path) as _gf:
                                    _g_system_prompt = _gf.read()
                            except FileNotFoundError:
                                return

                            # Build task text from input_prompt
                            from optimizer.ghost_replay import (
                                _build_task_string_from_input,
                                _load_image_as_b64,
                                _extract_verdict,
                                log_ghost_verdict,
                            )
                            _g_task = _build_task_string_from_input(_g_input_prompt)

                            # Narrative contradiction flag
                            if _tc_get("ghost.narrative_flag_enabled", True):
                                _g_narr = (_g_narrative or "").lower()
                                _g_fan = _g_input_prompt.get("fan_state", "")
                                if _g_fan in ("expanding", "accelerating"):
                                    _contras = [w for w in ["stalled", "zero expansion", "zero separation",
                                                            "not expanding", "flat", "no recent cross", "mixed"]
                                                if w in _g_narr]
                                    if _contras:
                                        _g_task = (f"\n⚠️ NOTE: Fan labeled '{_g_fan}' but narrative "
                                                   f"contains: {', '.join(_contras)}. Verify visually.\n"
                                                   + _g_task)

                            # Load chart
                            _g_chart_b64 = _load_image_as_b64(_g_chart_path)
                            if not _g_chart_b64:
                                return

                            # Build OpenAI request
                            _g_messages = [
                                {"role": "system", "content": _g_system_prompt},
                                {"role": "user", "content": [
                                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_g_chart_b64}"}},
                                    {"type": "text", "text": "LIVE CHART — read this chart like a senior trader."},
                                    {"type": "text", "text": _g_task},
                                ]},
                            ]
                            _g_payload = json.dumps({
                                "model": _tc_get("ghost.model_name", "mlx-community/Qwen3.5-35B-A3B-4bit"),
                                "messages": _g_messages,
                                "temperature": _tc_get("ghost.temperature", 0.7),
                                "top_p": 0.8,
                                "max_tokens": int(_tc_get("ghost.max_tokens", 4096)),
                                "stream": False,
                            }).encode()
                            _g_req = urllib.request.Request(
                                "http://127.0.0.1:11503/v1/chat/completions",  # serving gateway → MLX 35B
                                data=_g_payload,
                                headers={
                                    "Content-Type": "application/json",
                                    "X-Jarvis-Tenant": "background",
                                },
                            )
                            _g_resp = urllib.request.urlopen(_g_req, timeout=300)
                            _g_data = json.loads(_g_resp.read())
                            _g_raw = _g_data["choices"][0]["message"].get("content", "")

                            import re as _g_re
                            _g_raw = _g_re.sub(r"<think>.*?</think>", "", _g_raw, flags=_g_re.DOTALL).strip()

                            _g_verdict = _extract_verdict(_g_raw)
                            log_ghost_verdict(
                                _g_instrument, _g_anthropic_verdict, _g_verdict,
                                _g_vtd_id or 0, _g_chart_path or "", _g_raw,
                            )
                            logger.info("[GHOST] %s: Opus=%s 35B=%s dir=%s conf=%s",
                                        _g_instrument,
                                        _g_anthropic_verdict.get("verdict", "?"),
                                        _g_verdict.get("verdict", "?"),
                                        _g_verdict.get("direction", "?"),
                                        _g_verdict.get("confidence", 0))
                        except Exception as _ghost_exc:
                            logger.debug("[GHOST] Error: %s", _ghost_exc)

                    # Gather inputs for ghost thread
                    _g_ip = {}
                    try:
                        _g_ip = json.loads(sniper_data.get("input_prompt", "{}")) if isinstance(
                            sniper_data.get("input_prompt"), str) else sniper_data.get("input_prompt", {})
                    except Exception:
                        _g_ip = {"pair": instrument, "narrative": sniper_data.get("narrative", ""),
                                 "fan_state": sniper_data.get("fan_state", ""),
                                 "bb_expanding": sniper_data.get("bb_expanding", False),
                                 "indicators": sniper_data.get("indicators", {})}

                    _ghost_threading.Thread(
                        target=_run_ghost_validator,
                        args=(
                            instrument,
                            _chart_path if '_chart_path' in dir() else "",
                            _g_ip,
                            validation_results if isinstance(validation_results, dict) else {},
                            0,  # vtd_id — filled if vision_training_data row exists
                            sniper_data.get("narrative", ""),
                        ),
                        daemon=True,
                        name=f"ghost-validator-{instrument}",
                    ).start()
            except Exception as _ghost_setup_exc:
                logger.debug("[GHOST] Setup error: %s", _ghost_setup_exc)

            # ── Record REJECT/WATCH for scout rejection cooldown ──
            _val_verdict = validation_results.get("verdict", "SKIP") if isinstance(validation_results, dict) else "SKIP"
            if _val_verdict in ("REJECT", "WATCH"):
                try:
                    _rej_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                             '..', '..', 'dashboard', 'rejection_cooldowns.json')
                    _rej_path = os.path.normpath(_rej_path)
                    _rej_data = {}
                    if os.path.exists(_rej_path):
                        with open(_rej_path) as _rf:
                            _rej_data = json.load(_rf)
                    _rej_setup = validation_results.get("best_setup", "unknown") if isinstance(validation_results, dict) else "unknown"
                    _rej_key = f"{instrument}::{_rej_setup}"
                    _rej_data[_rej_key] = time.time() + 1800  # 30 min cooldown (2 candles)
                    with open(_rej_path, 'w') as _wf:
                        json.dump(_rej_data, _wf)
                    logger.info("[REJECT_COOLDOWN] %s → cooldown 1h", _rej_key)
                except Exception as _rej_exc:
                    logger.debug("Failed to write rejection cooldown: %s", _rej_exc)

            # Check if LLM escalation needed (borderline signal)
            val_confidence = validation_results.get("confidence", 0.5) if isinstance(validation_results, dict) else 0.5
            val_borderline = validation_results.get("borderline", False) if isinstance(validation_results, dict) else False
            needs_escalation = (
                isinstance(validation_results, dict)
                and (validation_results.get("needs_llm_escalation", False)
                     or val_confidence < 0.7
                     or val_borderline)
            )

            if needs_escalation:
                contradictions = validation_results.get("contradictions", {})

                # ValidationAnalyst (paid Sonnet call) DISABLED 2026-03-31.
                # Redundant with vision_validator which already makes the same decision
                # with MORE context (chart images). Was burning ~$5-10/day in extra API calls.
                # The vision validator's verdict is the sole trade decision maker.
                logger.info("LLM escalation skipped — vision validator is sole decision maker")

                # Also check via cycle_orchestrator (existing escalation path)
                try:
                    escalation_result = _swarm_execute_tool(
                        "cycle_orchestrator", "should_escalate_to_llm",
                        contradictions=contradictions,
                        validation_results=validation_results,
                    )
                    escalation = escalation_result.get("tool_result", escalation_result)
                    validation_results["llm_escalation"] = escalation
                    logger.info(
                        "LLM escalation check: %s (%s)",
                        escalation.get("escalate") if isinstance(escalation, dict) else "unknown",
                        escalation.get("reason") if isinstance(escalation, dict) else "unknown",
                    )
                except Exception as esc_exc:
                    logger.warning("Orchestrator escalation check failed: %s", esc_exc)

            phase_elapsed = time.time() - phase_start
            phase_timings["validation"] = phase_elapsed
            logger.info("[TIMING] validation: %.2fs", phase_elapsed)
            _report_agent_performance("validator", True, phase_elapsed)
            # Build rich validator summary for dashboard
            _v_verdict = (validation_results.get("verdict", "—") or "—").upper() if isinstance(validation_results, dict) else "—"
            _v_conf = validation_results.get("confidence", "?") if isinstance(validation_results, dict) else "?"
            _v_reason = validation_results.get("reasoning", "") if isinstance(validation_results, dict) else ""
            _v_dir = validation_results.get("direction", "") if isinstance(validation_results, dict) else ""
            _v_re_entry = validation_results.get("re_entry_conditions", "") if isinstance(validation_results, dict) else ""
            _v_parts = [f"{_v_verdict}"]
            if _v_dir:
                _v_parts.append(f"{_v_dir}")
            _v_parts.append(f"(confidence: {_v_conf * 100:.0f}%)" if isinstance(_v_conf, (int, float)) else f"(confidence: {_v_conf})")
            if _v_reason:
                _v_parts.append(f"| {_v_reason}")
            if _v_verdict == "WATCH" and _v_re_entry:
                _v_parts.append(f"| Snipe: {str(_v_re_entry)[:100]}")
            _log_phase("validator", " ".join(_v_parts), phase_elapsed)

            cycle_result["validation"] = validation_results
            cycle_result["steps_completed"].append("validation")

            # Log validation result (LOGS-03)
            try:
                self._get_logger().log_validation(
                    cycle_id=f"cycle_{cycle_num}_{cycle_start}",
                    instrument=instrument,
                    validation_results=validation_results if isinstance(validation_results, dict) else {},
                )
            except Exception as exc:
                logger.warning("Validation logging failed: %s", exc)

            # ── V4: Save chart + verdict to vision_training_data for model training ──
            # Every chart the validator sees gets saved — wins AND losses.
            # When the trade closes, we update with the outcome.
            _v4_entry_chart = analysis_results.get("v4_chart_path", "") if isinstance(analysis_results, dict) else ""
            if _v4_entry_chart and os.path.exists(_v4_entry_chart):
                try:
                    import shutil
                    _v4_verdict_str = validation_results.get("v4_verdict", validation_results.get("verdict", "UNKNOWN")) if isinstance(validation_results, dict) else "UNKNOWN"
                    _v4_dir_str = validation_results.get("v4_direction", "") if isinstance(validation_results, dict) else ""
                    _v4_conf_val = validation_results.get("confidence", 0) if isinstance(validation_results, dict) else 0
                    # 2026-04-29: raised cap from 500 → 5000 so we can audit
                    # whether the model is walking the 10-point checklist.
                    # Reasoning JSON-encoded is at most a few KB; trivial to store.
                    _v4_reasoning = validation_results.get("reasoning", "")[:5000] if isinstance(validation_results, dict) else ""

                    # Copy chart to persistent training directory (named by pair + timestamp)
                    _v4_train_dir = str(_FOREX_DATA_DIR / "charts" / "training")
                    os.makedirs(_v4_train_dir, exist_ok=True)
                    _v4_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    _v4_saved_name = f"{instrument}_{_v4_verdict_str}_{_v4_dir_str}_{_v4_ts}.png"
                    _v4_saved_path = os.path.join(_v4_train_dir, _v4_saved_name)
                    shutil.copy2(_v4_entry_chart, _v4_saved_path)

                    # Log to v2/trading_forex.db
                    _v4_conn = get_trading_forex()
                    _v4_conn.execute("""
                        INSERT INTO vision_training_data
                        (timestamp, agent, chart_path, input_prompt, output_response, verdict, model_used)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        datetime.now(timezone.utc).isoformat(),
                        "validator",
                        _v4_saved_path,
                        json.dumps({"pair": instrument, "alert_type": _v4_scout.get('alert_type', ''),
                                    "indicators": {k: _v4_ind.get('core', _v4_ind).get(k) for k in ['rsi', 'stoch_k', 'stoch_d', 'adx']},
                                    "fan_state": _v4_ema.get('fan_state', ''),
                                    "bb_expanding": _v4_scout.get('bb_expanding', False),
                                    "narrative": _v4_ta_narrative[:200]}, default=str),
                        json.dumps({"verdict": _v4_verdict_str, "direction": _v4_dir_str,
                                    "confidence": _v4_conf_val, "reasoning": _v4_reasoning}, default=str),
                        _v4_verdict_str,
                        self._team.get_agent("validator", {}).get("model", "claude-sonnet-4-6") if hasattr(self, '_team') and hasattr(self._team, 'get_agent') else "unknown",
                    ))

                    # Also store chart path in cycle_result so trade_monitor can link outcome later
                    cycle_result["v4_entry_chart_path"] = _v4_saved_path
                    logger.info("[V4] Training data saved: %s (verdict=%s)", _v4_saved_name, _v4_verdict_str)
                except Exception as _v4_save_err:
                    logger.warning("[V4] Failed to save training data: %s", _v4_save_err)

        except Exception as exc:
            phase_elapsed = time.time() - phase_start
            phase_timings["validation"] = phase_elapsed
            logger.info("[TIMING] validation: %.2fs (failed)", phase_elapsed)
            _report_agent_performance("validator", False, phase_elapsed)
            _log_phase("validator", f"Validation failed: {exc}", phase_elapsed, status="error")
            logger.error("Cycle #%d validation failed: %s", cycle_num, exc)
            self._post_error(task_id, "validation", str(exc))
            # Default to SKIP on validation failure (connection error, timeout, etc.)
            # HOLD should never be a silent default — SKIP is the correct conservative fallback.
            validation_results = {
                "verdict": "SKIP",
                "v4_verdict": "SKIP",
                "overall_passed": False,
                "confidence": 0.1,
                "reasoning": f"Validator connection error — skipping cycle: {exc}",
                "recommendation": "skip -- validation error",
            }

        # ── Compute Full Confluence Score (ALL cycle data → single 0-100 score) ──
        try:
            from Source.full_confluence_scorer import compute_full_confluence
            sniper_for_confluence = analysis_results.get("sniper_score", {}) if isinstance(analysis_results, dict) else {}
            if isinstance(sniper_for_confluence, dict) and "instrument" not in sniper_for_confluence:
                sniper_for_confluence["instrument"] = instrument  # ensure pair is available for candle bonus
            
            # V4: Pass validator's vision confidence as evidence for confluence scoring
            # No more DB historical evidence — the validator's visual assessment IS the evidence
            db_evidence_for_confluence = {}
            if isinstance(validation_results, dict):
                _v4_conf = validation_results.get("confidence", 0)
                db_evidence_for_confluence = {
                    "v4_confidence": _v4_conf,
                    "confidence": _v4_conf,
                    "source": "v4_vision",
                }
                logger.info("[V4] Vision confidence for confluence: %.0f%%", float(_v4_conf) * 100 if _v4_conf <= 1 else float(_v4_conf))
            
            # Get profile engine — try module var first, then Flask app config
            _pe2 = _shared_profile_engine
            if _pe2 is None:
                try:
                    from flask import current_app
                    _pe2 = current_app.config.get('_profile_engine')
                except Exception:
                    pass
            # Use the validator's direction for post-validator confluence scoring.
            # The validator may disagree with the fan direction (e.g., SELL into contracting bullish fan).
            # Without this, reversal setups always score 0/75 because the confluence scorer
            # sees "counter-trend" when the validator is actually identifying the NEXT move.
            _v4_dir = (validation_results.get("v4_direction", "") or "").lower() if isinstance(validation_results, dict) else ""
            if _v4_dir in ("buy", "sell"):
                _dir_mapped = "bullish" if _v4_dir == "buy" else "bearish"
                sniper_for_confluence = {**sniper_for_confluence, "direction": _dir_mapped}

            full_confluence = compute_full_confluence(
                sniper_result=sniper_for_confluence,
                intelligence_data=intelligence_data,
                db_evidence=db_evidence_for_confluence,
                account_state=account_summary,
                min_confluence=int(risk_limits.get("min_confluence", 40)),
                market_picture=ema_result,
                # Merge thesis values into scout_context so Gate 1 (which reads
                # fan_delta_5bar / bb_delta_5bar from this dict) gets real numbers
                # on manual cycles. Mirrors the merge in the pre-validator call.
                scout_context={
                    **(scout_context or {}),
                    **{k: v for k, v in (_thesis or {}).items() if v is not None},
                },
                profile_engine=_pe2,
                pair=instrument,
            )
            
            logger.info("[CONFLUENCE] %s: %d/75 — %s",
                       instrument, full_confluence["total_score"], full_confluence["summary"])
            if flight:
                _bd = full_confluence.get("breakdown", {})
                _sniper_raw = analysis_results.get("sniper_score", {}) if isinstance(analysis_results, dict) else {}
                flight.record(FlightStage.CONFLUENCE_SCORE, pair=instrument, cycle_id=_cycle_id, data={
                    "total_score": full_confluence["total_score"],
                    "tradeable": full_confluence.get("tradeable", False),
                    "sniper": _sniper_raw.get("max_score") or max(_sniper_raw.get("buy_score", 0), _sniper_raw.get("sell_score", 0)),
                    "sniper_buy": _sniper_raw.get("buy_score", 0),
                    "sniper_sell": _sniper_raw.get("sell_score", 0),
                    "gate1": _bd.get("gate1_sniper", {}).get("pass", False),
                    "ema": _bd.get("ema_narrative", {}).get("points", 0),
                    "intel": _bd.get("intelligence", {}).get("points", 0),
                    "db": _bd.get("db_evidence", {}).get("points", 0),
                    "session": _bd.get("session_regime", {}).get("points", 0),
                })
            _log_phase("cycle_orchestrator",
                       f"Full confluence: {full_confluence['total_score']}/{full_confluence.get('max_possible', 75)} "
                       f"({full_confluence['summary']})",
                       0.0)
            
            # Store in cycle results for dashboard
            cycle_result["full_confluence"] = full_confluence
            if isinstance(analysis_results, dict):
                analysis_results["full_confluence"] = full_confluence
        except Exception as exc:
            logger.warning("Full confluence computation failed: %s", exc)
            full_confluence = {"total_score": 0, "tradeable": False, "breakdown": {}, "summary": "error"}

        # ── USER WATCH EARLY EXIT ─────────────────────────────────────────────
        # ── USER CHART EARLY EXIT ─────────────────────────────────────────────
        # When Tim submits a chart (user_watch OR user_chat with a chart),
        # the validator's job is to set the snipe/watch conditions ("the fishing line").
        # Once that's done, STOP. The trading cycle only fires when the scout sees
        # those conditions met. NEVER run the orchestrator or execution agents on
        # a user chart submission cycle.
        #
        # This covers BOTH triggered_by values:
        #   user_watch  — set by trading_api_routes legacy SET_WATCH handler
        #   user_chat   — set by floor_chat run_cycle action (should not happen after
        #                 the __SUBMIT_CHART__ dispatcher fix, but guard it here too)
        _triggered_by_val = (scout_context or {}).get("triggered_by", "") if isinstance(scout_context, dict) else ""
        # A snipe is a snipe — user_watch and user_chat cycles execute the same as
        # scout_snipe. triggered_by only affects data collection fallback and context
        # injection (above). It does NOT block execution. Removed all USER_CHART_EXIT gates.
        _triggered_by_val = _triggered_by_val  # keep for logging only
        # ─────────────────────────────────────────────────────────────────────

        # Step 5: Master Decision via cycle_orchestrator (LLM-powered reasoning)
        phase_start = time.time()
        
        # Orchestrator receives ALL data from collection phases
        all_cycle_data = {
            "instrument": instrument,
            "timeframe": timeframe,
            "data_collection": cycle_result.get("data_collection", {}),
            "intelligence": intelligence_data,
            "technical_analysis": analysis_results,
            "account_summary": account_summary,
            "full_confluence": full_confluence,
            "validation": validation_results,
        }

        decision: Dict[str, Any] = {}
        try:
            _swarm_send_message(
                "cycle_orchestrator", "execution",
                f"Master trader analyzing complete market picture for {instrument}",
            )
            # Log what the orchestrator is working with
            val_verdict = validation_results.get("verdict", "?") if isinstance(validation_results, dict) else "?"
            val_conf = validation_results.get("confidence", 0) if isinstance(validation_results, dict) else 0
            _swarm_send_message(
                "validator", "cycle_orchestrator",
                f"[TO ORCHESTRATOR] Full Confluence: {full_confluence.get('total_score', 0)}/100 "
                f"(tradeable={full_confluence.get('tradeable', False)}) | "
                f"Validator: {val_verdict} ({val_conf:.0%} confidence) | "
                f"{full_confluence.get('summary', '')}",
            )
            
            # === MOMENTUM TRAP PRE-CHECK (hard code gate — no LLM override) ===
            # Catches: BUY when RSI>78+Stoch>90 (chasing OB) or SELL when RSI<22+Stoch<10 (chasing OS)
            # Also: counter-trend + ADX>30 + BB expanding = fighting a strong accelerating trend
            _momentum_trap_block = False
            _momentum_trap_reason = ""
            try:
                _sc_dir = (scout_context or {}).get("direction", "").lower()
                _snap = (scout_context or {}).get("market_snapshot", {}) or {}
                _sc_rsi = _snap.get("rsi") or (analysis_results or {}).get("sniper_score", {}).get("indicators", {}).get("rsi")
                _sc_stoch = _snap.get("stoch_k") or (analysis_results or {}).get("sniper_score", {}).get("indicators", {}).get("stoch_k")
                _sc_adx = _snap.get("adx") or (analysis_results or {}).get("sniper_score", {}).get("indicators", {}).get("adx", 25)
                _sc_fan = _snap.get("fan_state", "")
                _bb_expanding = _snap.get("bb_expanding", False) or (ema_result or {}).get("fan_state") == "expanding"

                if _sc_rsi is not None and _sc_stoch is not None:
                    _sc_rsi = float(_sc_rsi)
                    _sc_stoch = float(_sc_stoch)
                    _sc_adx = float(_sc_adx) if _sc_adx else 25

                    # BUY into overbought = chasing
                    # Two triggers: (1) RSI>78 + Stoch>90, (2) RSI>75 + Stoch>85 + ADX>30
                    # NOTE: Stoch alone is NOT sufficient — it hits 95/100 in healthy trends
                    # where RSI stays moderate (30-70). Those are GOOD trades. Require BOTH.
                    if _sc_dir == "buy" and (
                        (_sc_rsi > 78 and _sc_stoch > 90) or 
                        (_sc_rsi > 75 and _sc_stoch > 85 and _sc_adx > 30)
                    ):
                        _momentum_trap_block = True
                        _momentum_trap_reason = f"MOMENTUM TRAP: BUY with RSI={_sc_rsi:.0f} Stoch={_sc_stoch:.0f} ADX={_sc_adx:.0f} — chasing overbought, not reversing"

                    # SELL into oversold = chasing
                    elif _sc_dir == "sell" and (
                        (_sc_rsi < 22 and _sc_stoch < 10) or 
                        (_sc_rsi < 25 and _sc_stoch < 15 and _sc_adx > 30)
                    ):
                        _momentum_trap_block = True
                        _momentum_trap_reason = f"MOMENTUM TRAP: SELL with RSI={_sc_rsi:.0f} Stoch={_sc_stoch:.0f} ADX={_sc_adx:.0f} — chasing oversold, not reversing"

                    # Counter-trend + strong accelerating trend (ADX>30 + expanding against)
                    if not _momentum_trap_block and _sc_adx > 30 and _sc_fan == "expanding":
                        _fan_dir = _snap.get("fan_direction", "") or (ema_result or {}).get("fan_direction", "")
                        _counter = (_sc_dir == "buy" and _fan_dir == "bearish") or (_sc_dir == "sell" and _fan_dir == "bullish")
                        if _counter:
                            _momentum_trap_block = True
                            _momentum_trap_reason = f"MOMENTUM TRAP: {_sc_dir.upper()} against {_fan_dir} expanding fan (ADX={_sc_adx:.0f}) — trend accelerating"
            except Exception as _mt_err:
                logger.debug("Momentum trap check error: %s", _mt_err)

            if _momentum_trap_block:
                logger.warning("🛑 %s on %s — HARD BLOCK", _momentum_trap_reason, instrument)
                if flight:
                    flight.record(FlightStage.ORCHESTRATOR_LLM, pair=instrument, cycle_id=_cycle_id,
                                  data={"action": "hold", "momentum_trap": True, "reason": _momentum_trap_reason},
                                  duration_ms=(time.time() - phase_start) * 1000, status="warn",
                                  note=_momentum_trap_reason)

            # === PIPELINE V3: VALIDATOR IS THE TRADING AUTHORITY ===
            # The validator already received ALL data and made the trade decision.
            # No orchestrator LLM call needed. Validator CONFIRM = trade, REJECT/WATCH = hold.
            # The orchestrator is now the team coordinator (mlx/CRO, status updates only).
            
            _val_verdict = (validation_results.get("verdict", "") or "").upper().strip() if isinstance(validation_results, dict) else ""
            
            # 2026-05-08: Momentum trap is log-only on validator-CONFIRM path.
            # The snipe_direct path already disabled this rule on 2026-04-09 after
            # V1/V2 optimizers proved gates ON scored 0.0 (blocked more winners
            # than losers — see line ~3223 comment + collective/patterns/2026-04-08).
            # The validator-CONFIRM path was missed in that cleanup. 2026-05-07
            # data confirmed: 3 GBP_JPY/EUR_JPY/GBP_USD CONFIRMs blocked at RSI
            # 75-81 + Stoch 98-100 all moved +4-5 pips in trade direction within
            # 15 min. Validator was correct, gate was wrong. Tim's directive:
            # "trade_now means trade_now — let it through. Guardian handles
            # post-entry safety, not pre-entry overrides." Detection + warning
            # logging preserved at lines ~7739-7745 so we keep visibility.
            if _momentum_trap_block and _val_verdict == "CONFIRM":
                logger.info("⚡ Momentum trap detected on %s but NOT blocking — "
                            "validator CONFIRM passed through (log-only mode)", instrument)
            _val_trade_plan = validation_results.get("trade_plan", {}) if isinstance(validation_results, dict) else {}
            _val_confidence = validation_results.get("confidence", 0) if isinstance(validation_results, dict) else 0
            _val_reasoning = validation_results.get("reasoning", "") if isinstance(validation_results, dict) else ""

            # ── Live training pair collection (non-blocking) ──
            # Save every validator decision to build 35B training dataset.
            # Full market context captured here — this is what the 35B must learn to read.
            try:
                from Source.validator_training_extractor import collect_live_pair
                # market_picture nests EMA data under 'ema' key and BB under 'bollinger'
                _mp = market_picture or {}
                _mp_ema = _mp.get('ema', _mp)   # scan_ema_signals output (fan_state etc)
                _mp_bb  = _mp.get('bollinger', {})
                # indicators was overwritten to {"core":..,"advanced":..} — use sniper_result directly
                _ind = (sniper_result.get("indicators", {}) if isinstance(sniper_result, dict) else
                        (analysis_results or {}).get("sniper_score", {}).get("indicators", {})
                        if isinstance(analysis_results, dict) else {})
                _ta = ta_interpretation if isinstance(ta_interpretation, dict) else {}
                _mkt_ctx = (
                    f"Pair: {instrument} | Direction bias: {sniper_result.get('direction','?') if isinstance(sniper_result,dict) else '?'}\n"
                    f"EMA fan: {_mp_ema.get('fan_state','?')} {_mp_ema.get('fan_direction','?')} "
                    f"(ordered: {_mp_ema.get('fan_ordered','?')})\n"
                    f"Fan width: {_mp_ema.get('separation_pct',0):.4f}% | "
                    f"Velocity: {_mp_ema.get('separation_velocity',0):.5f}%/bar | "
                    f"Trend health: {_mp.get('trend_health', _mp_ema.get('trend_health',0))}/100\n"
                    f"BB expanding: {_mp_bb.get('bb_expanding','?')} | "
                    f"BB width%: {_mp_bb.get('width_pct',0):.3f} | "
                    f"Squeeze: {_mp_bb.get('squeeze','?')}\n"
                    f"RSI: {_ind.get('rsi',0):.1f} | Stoch K/D: {_ind.get('stoch_k',0):.1f}/{_ind.get('stoch_d',0):.1f} | "
                    f"ADX: {_ind.get('adx',0):.1f} | MACD hist: {_ind.get('macd_histogram',0):.5f}\n"
                    f"Sniper: buy={sniper_result.get('buy_score',0) if isinstance(sniper_result,dict) else 0} "
                    f"sell={sniper_result.get('sell_score',0) if isinstance(sniper_result,dict) else 0} "
                    f"threshold={sniper_result.get('threshold',12) if isinstance(sniper_result,dict) else 12}\n"
                    f"TA narrative: {_ta.get('narrative','')[:300]}\n"
                    f"TA clarity: {_ta.get('clarity','?')} | Phase: {_ta.get('phase_assessment','?')}"
                )
                collect_live_pair(
                    cycle_id=_cycle_id,
                    instrument=instrument,
                    verdict=_val_verdict,
                    reasoning=_val_reasoning,
                    market_context=_mkt_ctx,
                    confidence=float(_val_confidence) if _val_confidence else 0.0,
                )
            except Exception as _te:
                pass  # Never block a cycle for training collection

            # Track snipe trigger for logging (but validator is ALWAYS authoritative)
            _is_snipe_trigger = (
                isinstance(scout_context, dict) and
                scout_context.get("triggered_by") in ("snipe", "cascade_reentry")
            )
            
            # Build the decision from validator verdict — validator is SOLE authority
            if _val_verdict == "CONFIRM":
                # V4: direction is at top level (BUY/SELL). V3: in trade_plan dict.
                _trade_dir = (
                    validation_results.get("v4_direction", "").lower()  # V4 vision validator
                    or _val_trade_plan.get("direction", "")             # V3 legacy
                    or (scout_context or {}).get("direction", "neutral") # fallback
                )
                llm_decision = {
                    "action": _trade_dir if _trade_dir in ("buy", "sell") else "hold",
                    "allowed": _trade_dir in ("buy", "sell"),
                    "reasoning": _val_reasoning,
                    "confluence_score": full_confluence.get("total_score", 0),
                    "direction": _trade_dir,
                    "regime": _val_trade_plan.get("regime", "unknown"),
                    "stop_loss_atr_mult": _val_trade_plan.get("sl_atr_mult", risk_limits.get("sniper_sl_atr", 2.5)),  # V4: 2.5x ATR (3.0 proved worse)
                    "take_profit_atr_mult": _val_trade_plan.get("tp_atr_mult", risk_limits.get("sniper_tp_atr", 0.5)),
                    "position_size_pct": _val_trade_plan.get("risk_pct", risk_limits.get("max_risk_per_trade_pct", 2.0)),
                    "hold_reasons": [],
                    "source": "validator_v4_vision",
                }
                logger.info("[V3] Validator CONFIRMED %s on %s (%.0f%% confidence) — executing trade plan",
                           _trade_dir, instrument, _val_confidence * 100 if _val_confidence <= 1 else _val_confidence)
            else:
                # Validator says REJECT/WATCH/HOLD — no trade
                # Use effective_direction (EMA fan / scout thesis) — sniper does NOT set direction
                _hold_dir = effective_direction if effective_direction and effective_direction != "neutral" else "neutral"
                # Build hold_reasons from: loss_patterns (if any) + validator reasoning summary
                # FIX: hold_reasons was always [] — dashboard showed blank. Now includes
                # the validator's actual reasoning so Tim can see WHY it's holding.
                _loss_patterns = validation_results.get("loss_patterns", []) if isinstance(validation_results, dict) else []
                _hold_reasons = list(_loss_patterns) if _loss_patterns else []
                if _val_reasoning and not _hold_reasons:
                    # Use first sentence of validator reasoning as the hold reason
                    _first_sentence = _val_reasoning.split(".")[0].strip()
                    if _first_sentence:
                        _hold_reasons = [f"{_val_verdict}: {_first_sentence}"]
                llm_decision = {
                    "action": "hold",
                    "allowed": False,
                    "reasoning": _val_reasoning,
                    "hold_reasons": _hold_reasons,
                    "confluence_score": full_confluence.get("total_score", 0),
                    "direction": _hold_dir,
                    "source": "validator_v4_vision",
                }
                logger.info("[V3] Validator %s on %s — no trade. Reason: %s",
                           _val_verdict, instrument, _val_reasoning[:200])
                if flight:
                    flight.record(FlightStage.ORCHESTRATOR_LLM, pair=instrument, cycle_id=_cycle_id,
                                  data={"action": "hold", "validator_verdict": _val_verdict,
                                        "confidence": _val_confidence, "source": "validator_v4_vision"},
                                  duration_ms=(time.time() - phase_start) * 1000,
                                  note=f"V3: Validator {_val_verdict} → HOLD")

            # ── Orchestrator LLM narration (mlx/CRO 9B local, ~$0/call) ──
            # The orchestrator is the team voice on the trading floor — synthesizes what the
            # team found and communicates it to Tim in plain language.
            try:
                import urllib.request as _orch_req
                import json as _orch_json

                _ta_phase = (ta_interpretation or {}).get("phase_assessment", "") if isinstance(ta_interpretation, dict) else ""
                _ta_narrative = (ta_interpretation or {}).get("narrative", "") if isinstance(ta_interpretation, dict) else ""
                _thesis_steps = (_python_thesis.get("steps_confirmed", 0) if isinstance(_python_thesis, dict) else
                                 (ta_interpretation or {}).get("thesis_progress", {}).get("steps_confirmed", 0) if isinstance(ta_interpretation, dict) else 0)

                _orch_system = (
                    "You are the cycle orchestrator for a forex trading team. "
                    "You manage the team and communicate results to the trader (Tim). "
                    "Be direct and specific — no filler words, no 'I'. "
                    "2-4 sentences max. Speak as a team captain reporting to the trader.\n"
                    "CRITICAL: The VALIDATOR is the sole authority. DO NOT mention sniper score "
                    "as a reason to hold or trade — it is background data only. "
                    "Summarize what the validator SAW on the chart, not raw indicator numbers."
                )
                _orch_prompt = (
                    f"Pair: {instrument} | Validator verdict: {_val_verdict} ({_val_confidence:.0%} confidence)\n"
                    f"Thesis progress: {_thesis_steps}/5 steps confirmed\n"
                    f"Phase: {_ta_phase or 'unknown'}\n"
                    f"Validator saw: {_val_reasoning[:300] if _val_reasoning else 'No reasoning'}\n"
                    + (f"Action: {llm_decision.get('action','').upper()} "
                       f"SL {llm_decision.get('stop_loss_atr_mult','?')}×ATR "
                       f"TP {llm_decision.get('take_profit_atr_mult','?')}×ATR\n"
                       if _val_verdict == 'CONFIRM' else "")
                    + "\nReport to Tim what the validator found on the chart and what happens next. "
                    "Do NOT mention sniper score."
                )
                _orch_payload = _orch_json.dumps({
                    "model": "mlx-community/Qwen3.5-9B-4bit",
                    "messages": [
                        {"role": "system", "content": _orch_system},
                        {"role": "user", "content": _orch_prompt},
                    ],
                    "max_tokens": 256,
                    "temperature": 0.3,
                    # Disable extended thinking — orchestrator narration needs speed, not reasoning
                    "chat_template_kwargs": {"enable_thinking": False},
                }).encode()
                _orch_request = _orch_req.Request(
                    "http://127.0.0.1:11500/chat/completions",
                    data=_orch_payload,
                    headers={"Content-Type": "application/json"},
                )
                with _orch_req.urlopen(_orch_request, timeout=15) as _orch_resp:
                    _orch_result = _orch_json.loads(_orch_resp.read())
                _orch_text = _orch_result["choices"][0]["message"]["content"].strip()
                # Strip <think> blocks if model includes them
                import re as _re_orch
                _orch_text = _re_orch.sub(r'<think>.*?</think>', '', _orch_text, flags=_re_orch.DOTALL).strip()

                logger.info("[ORCH] %s narration: %s", instrument, _orch_text[:120])
                _swarm_send_message("cycle_orchestrator", "reporter", _orch_text)
                _log_phase("cycle_orchestrator", _orch_text, 0)
                cycle_result["orchestrator_narration"] = _orch_text

            except Exception as _orch_err:
                # Fallback to template narration if MLX unavailable
                logger.warning("[ORCH] LLM narration failed (%s) — using template", _orch_err)
                _tmpl = (
                    f"{'✅' if _val_verdict == 'CONFIRM' else '⏸️' if _val_verdict == 'WATCH' else '❌'} "
                    f"Validator {_val_verdict} on {instrument} "
                    f"({_val_confidence:.0%} confidence). "
                    f"{_val_reasoning[:150] if _val_reasoning else 'No reasoning.'}"
                )
                _swarm_send_message("cycle_orchestrator", "reporter", _tmpl)
                _log_phase("cycle_orchestrator", _tmpl, 0)
                cycle_result["orchestrator_narration"] = _tmpl

            # V3: Snipe trigger logging — validator is authoritative, no bypass
            if _is_snipe_trigger:
                _snipe_wid = scout_context.get("watch_id", "?")
                if _val_verdict == "CONFIRM":
                    logger.info("🎯 SNIPE CONFIRMED: watch #%s conditions met + validator CONFIRM on %s",
                               _snipe_wid, instrument)
                else:
                    logger.info("🎯 SNIPE DENIED: watch #%s conditions met but validator %s on %s — no trade",
                               _snipe_wid, _val_verdict, instrument)
                if flight:
                    flight.record(FlightStage.ORCHESTRATOR_LLM, pair=instrument, cycle_id=_cycle_id,
                                  data={"snipe_trigger": True, "watch_id": _snipe_wid, 
                                        "validator_verdict": _val_verdict,
                                        "action": llm_decision.get("action", "hold")},
                                  note=f"SNIPE {'CONFIRMED' if _val_verdict == 'CONFIRM' else 'DENIED'}: "
                                       f"watch #{_snipe_wid} → validator {_val_verdict}")

            # If LLM says trade, use make_trade_decision for SL/TP/sizing math
            if llm_decision.get("allowed") and llm_decision.get("action") in ("buy", "sell"):
                # Run the heuristic to calculate exact prices + check hard limits
                heuristic_result = _swarm_execute_tool(
                    "cycle_orchestrator", "make_trade_decision",
                    all_cycle_data=all_cycle_data,
                    analysis_results=analysis_results if isinstance(analysis_results, dict) else {},
                    validation_results=validation_results if isinstance(validation_results, dict) else {},
                    intelligence=intelligence_data,
                    account_summary=account_summary if isinstance(account_summary, dict) else {},
                    instrument=instrument,
                    timeframe=timeframe,
                    risk_limits=risk_limits,
                    llm_action=llm_decision.get("action"),  # Pass LLM decision for direction fallback
                    scout_context=scout_context if isinstance(scout_context, dict) else {},
                )
                heuristic = heuristic_result.get("tool_result", heuristic_result)
                
                # Merge: LLM reasoning + heuristic SL/TP/sizing
                decision = heuristic if isinstance(heuristic, dict) else {}
                decision["llm_reasoning"] = llm_decision.get("reasoning", "")
                decision["llm_action"] = llm_decision.get("action")
                decision["llm_allowed"] = llm_decision.get("allowed")
                # ENFORCE validator direction — make_trade_decision computes SL/TP only
                # Fix 2026-04-07: heuristic was overriding validator SELL→BUY via sniper scores
                if llm_decision.get("action") in ("buy", "sell"):
                    decision["action"] = llm_decision["action"]
                    decision["direction"] = llm_decision.get("direction", llm_decision["action"])
                
                # If heuristic blocked it (hard risk limits), override LLM
                if not decision.get("allowed"):
                    decision["llm_overridden"] = True
                    decision["reasons"] = decision.get("blocking_reasons", []) + [
                        f"LLM wanted {llm_decision['action']} but hard limits blocked"
                    ]
            else:
                # LLM says hold — use its reasoning
                decision = {
                    "action": "hold",
                    "allowed": False,
                    "reasoning": llm_decision.get("reasoning", "Orchestrator decided to hold"),
                    "hold_reasons": llm_decision.get("hold_reasons", []),
                    "llm_reasoning": llm_decision.get("reasoning", ""),
                    "confluence_score": llm_decision.get("confluence_score", 0),
                    "regime": llm_decision.get("regime", "unknown"),
                    "direction": llm_decision.get("direction", "neutral"),
                    "reasons": llm_decision.get("hold_reasons", [llm_decision.get("reasoning", "Hold")]),
                    "blocking_reasons": llm_decision.get("hold_reasons", []),
                }

            # Flight: orchestrator decision
            if flight and isinstance(decision, dict):
                flight.record(FlightStage.ORCHESTRATOR_LLM, pair=instrument, cycle_id=_cycle_id, data={
                    "action": decision.get("action", "hold"),
                    "allowed": decision.get("allowed", False),
                    "direction": decision.get("direction", "neutral"),
                    "reasoning": str(decision.get("llm_reasoning", decision.get("reasoning", "")))[:200],
                }, duration_ms=(time.time() - phase_start) * 1000,
                note=f"{decision.get('action', 'hold').upper()} allowed={decision.get('allowed', False)}")

                if decision.get("allowed") and decision.get("stop_loss"):
                    flight.record(FlightStage.ORCHESTRATOR_MATH, pair=instrument, cycle_id=_cycle_id, data={
                        "sl": decision.get("stop_loss"),
                        "tp": decision.get("take_profit"),
                        "units": decision.get("position_size"),
                        "rr": decision.get("rr_mult"),
                    })

            self._post_result(
                task_id, "trading_orchestrator", MessageType.TRADE_DECISION,
                f"Decision: {decision.get('action', 'hold') if isinstance(decision, dict) else 'hold'} {instrument} "
                f"(allowed={decision.get('allowed', False) if isinstance(decision, dict) else False})",
                decision if isinstance(decision, dict) else {},
            )

            phase_elapsed = time.time() - phase_start
            phase_timings["decision"] = phase_elapsed
            logger.info("[TIMING] decision: %.2fs", phase_elapsed)
            _report_agent_performance("cycle_orchestrator", True, phase_elapsed)
            cycle_result["decision"] = decision
            action_str = decision.get("action", "hold").upper() if isinstance(decision, dict) else "HOLD"
            _log_phase("cycle_orchestrator", f"Trade decision: {action_str}", phase_elapsed)
            cycle_result["steps_completed"].append("decision")

            # Add to decisions array for dashboard
            action = decision.get("action", "hold") if isinstance(decision, dict) else str(decision)
            reasons = decision.get("reasons", []) if isinstance(decision, dict) else []

            # ── Humanize hold reasons for dashboard ──
            _REASON_LABELS = {
                "confluence_below_threshold": "Confluence score too low — not enough signals agree",
                "ema_fan_contracting_weak_velocity": "EMA fan is contracting with weak momentum — trend losing steam",
                "ema_fan_contracting": "EMA fan is contracting — trend may be reversing",
                "ema_weak_velocity": "EMA velocity too low — market not committed to direction",
                "validator_caution_h4_disagreement": "Higher timeframe (H4) doesn't confirm the direction",
                "trend_health_critically_low": "Trend structure is critically weak — no reliable direction",
                "mixed_regime_high_reversal_risk": "Mixed regime with high reversal risk — too uncertain",
                "db_evidence_insufficient": "Not enough historical evidence to support this setup",
                "spread_too_wide": "Spread is too wide — poor entry conditions",
                "session_quality_low": "Trading session quality too low (off-hours)",
                "max_positions_reached": "Maximum open positions already reached",
                "correlation_risk": "Correlated position already open — too much exposure",
                "recent_loss_cooldown": "Recent loss on this pair — cooling down before re-entry",
                "news_risk": "High-impact news event approaching — staying out",
                "rsi_overbought": "RSI overbought — momentum stretched too far",
                "rsi_oversold": "RSI oversold — momentum stretched too far",
                "stochastic_extreme": "Stochastic at extreme — likely to snap back",
                "no_clear_signal": "No clear trading signal — sitting this one out",
            }

            def _humanize_reasons(raw_reasons):
                if not raw_reasons:
                    return f"Holding on {instrument} — no actionable setup right now"
                human = []
                for r in raw_reasons:
                    r_str = str(r).strip()
                    # Check exact match first, then partial matches
                    if r_str in _REASON_LABELS:
                        human.append(_REASON_LABELS[r_str])
                    else:
                        matched = False
                        for key, label in _REASON_LABELS.items():
                            if key in r_str.lower().replace(" ", "_"):
                                human.append(label)
                                matched = True
                                break
                        if not matched:
                            # Already human-readable (from LLM reasoning), pass through
                            human.append(r_str.replace("_", " ").capitalize() if "_" in r_str else r_str)
                return "; ".join(human) if human else f"Holding on {instrument}"

            # Also build a plain-English summary from LLM reasoning if available
            _llm_reasoning = decision.get("llm_reasoning", "") if isinstance(decision, dict) else ""
            _human_result = _llm_reasoning if _llm_reasoning and len(_llm_reasoning) > 20 else _humanize_reasons(reasons)

            cycle_result["decisions"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": "cycle_orchestrator",
                "action": action.upper(),
                "result": _human_result,
            })

            # Log signal (LOGS-01) -- after decision so we have full context
            try:
                # Build EMA snapshot for signal log (trade AND hold decisions)
                _signal_ema = {}
                if ema_result and not ema_result.get("error"):
                    _signal_ema = {
                        "fan_direction": ema_result.get("fan_direction"),
                        "fan_state": ema_result.get("fan_state"),
                        "separation_pct": ema_result.get("separation_pct"),
                        "separation_velocity": ema_result.get("separation_velocity"),
                        "fan_velocity_trend": ema_result.get("fan_velocity_trend"),
                        "trend_health": ema_result.get("trend_health"),
                        "reversal_risk": ema_result.get("reversal_risk"),
                        "recommended_bias": ema_result.get("recommended_bias"),
                        "ema100_role": ema_result.get("ema100_role"),
                        "narrative": ema_result.get("narrative"),
                    }
                self._get_logger().log_signal(
                    cycle_id=f"cycle_{cycle_num}_{cycle_start}",
                    instrument=instrument,
                    timeframe=timeframe,
                    analysis_results=analysis_results if isinstance(analysis_results, dict) else {},
                    decision=decision if isinstance(decision, dict) else {},
                    intelligence_data=intelligence_data,
                    ema_snapshot=_signal_ema,
                )
            except Exception as exc:
                logger.warning("Signal logging failed: %s", exc)

        except Exception as exc:
            phase_elapsed = time.time() - phase_start
            phase_timings["decision"] = phase_elapsed
            logger.info("[TIMING] decision: %.2fs (failed)", phase_elapsed)
            _report_agent_performance("cycle_orchestrator", False, phase_elapsed)
            _log_phase("cycle_orchestrator", f"Decision failed: {exc}", phase_elapsed, status="error")
            logger.error("Cycle #%d decision failed: %s", cycle_num, exc)
            self._post_error(task_id, "decision", str(exc))
            decision = {"action": "hold", "allowed": False}
            cycle_result["decision"] = decision

        # Step 7: Conditional execution via execute_tool
        phase_start = time.time()
        execution_result: Optional[Dict[str, Any]] = None
        try:
            if isinstance(decision, dict) and decision.get("allowed") and decision.get("action") in ("buy", "sell"):
                # ── FRIDAY EXECUTION GATE (belt-and-suspenders) ──
                if not test_mode:
                    _fri = _get_friday_status()
                    if _fri["action"] in ("close_all", "no_new_trades"):
                        logger.critical(
                            "FRIDAY GATE: Blocked trade execution for %s — %s",
                            instrument, _fri["reason"],
                        )
                        decision["allowed"] = False
                        decision["action"] = "hold"
                        decision["friday_blocked"] = True
                        cycle_result["decision"] = decision
                # ── END FRIDAY EXECUTION GATE ──

                # Pre-register thesis with guardian BEFORE order placement
                # Guardian's OANDA reconcile may catch the trade before we return
                try:
                    from Source.trading_api_routes import _guardian_instance
                    if _guardian_instance and scout_context:
                        _guardian_instance.register_thesis(instrument, {
                            'entry_type': scout_context.get('story_entry_type', scout_context.get('entry_type', 'unknown')),
                            'thesis': scout_context.get('story_thesis', scout_context.get('reasoning', '')),
                            'direction': scout_context.get('direction', ''),
                            'fan_state_at_entry': scout_context.get('market_snapshot', {}).get('fan_state', ''),
                            'opportunity_score': scout_context.get('opportunity_score', 0),
                            'setup_id': scout_context.get('setup_id', ''),
                        })
                except Exception:
                    pass  # Guardian not running yet — thesis will register after fill

                sl = decision.get("stop_loss")
                tp = decision.get("take_profit")
                if not sl or not tp:
                    raise ValueError(f"Missing SL ({sl}) or TP ({tp}) — cannot place order without risk management")

                units     = decision.get("position_size", 1000)
                direction = decision["action"]

                # ── FIFO_VIOLATION_SAFEGUARD pre-flight (2026-05-07, fixed 2026-05-08) ──
                # OANDA US accounts allow multiple trades on the same pair-direction,
                # but they must close FIFO (oldest first). The safeguard rejects a
                # new trade whose SL/TP would trigger BEFORE an existing trade's
                # SL/TP — that would force the newer trade to close out of order.
                # Fix: when an open same-direction trade exists on this pair, copy
                # its CURRENT live SL/TP onto the new order. The live values are
                # fetched from OANDA directly (~160ms), because guardian widens
                # the SL at trade open and trails it as the trade runs — DB only
                # stores the entry SL, not the live one. Reading the DB caused
                # 5+ rejections on 5/7-5/8 even with override active (DB SL was
                # tighter than live → still FIFO violation). Fallback: if OANDA
                # call fails, use DB values (better than nothing).
                try:
                    _fifo_conn = get_trading_forex()
                    _existing = _fifo_conn.execute(
                        "SELECT id, oanda_trade_id, sl_price, tp_price FROM live_trades "
                        "WHERE pair=? AND direction=? AND status='open' "
                        "AND oanda_trade_id IS NOT NULL "
                        "AND source != 'kronos_hunter' "
                        "ORDER BY entry_time ASC LIMIT 1",
                        (instrument, direction)
                    ).fetchone()
                    if _existing:
                        _ext_id, _oanda_id, _db_sl, _db_tp = _existing
                        _live_sl, _live_tp, _src = None, None, "db"
                        try:
                            from Source.oanda_client import OandaClient as _OC
                        except ImportError:
                            from oanda_client import OandaClient as _OC
                        try:
                            _oanda_trade = _OC().get_trade(str(_oanda_id))
                            _slo = (_oanda_trade.get("stopLossOrder") or {}).get("price")
                            _tpo = (_oanda_trade.get("takeProfitOrder") or {}).get("price")
                            if _slo and _tpo:
                                _live_sl, _live_tp, _src = _slo, _tpo, "oanda_live"
                        except Exception as _api_err:
                            logger.warning(
                                "[EXEC] %s FIFO live SL/TP fetch failed for trade %s: %s — falling back to DB",
                                instrument, _oanda_id, _api_err
                            )
                        if _live_sl is None and _db_sl and _db_tp:
                            _live_sl, _live_tp = str(_db_sl), str(_db_tp)
                        if _live_sl and _live_tp:
                            # 2026-05-08: OANDA's FIFO_VIOLATION_SAFEGUARD rejects
                            # exits that are EQUAL to the existing trade's exits —
                            # it requires strictly wider. Add a 0.5-pip buffer in
                            # the wider direction so the new trade is unambiguously
                            # ordered after the existing one in OANDA's accounting.
                            _is_jpy = "JPY" in instrument
                            _pip = 0.01 if _is_jpy else 0.0001
                            _prec = 3 if _is_jpy else 5
                            _buf = 0.5 * _pip
                            _live_sl_f = float(_live_sl)
                            _live_tp_f = float(_live_tp)
                            if direction == "buy":
                                _new_sl = round(_live_sl_f - _buf, _prec)
                                _new_tp = round(_live_tp_f + _buf, _prec)
                            else:
                                _new_sl = round(_live_sl_f + _buf, _prec)
                                _new_tp = round(_live_tp_f - _buf, _prec)
                            _orig_sl, _orig_tp = sl, tp
                            sl = f"{_new_sl:.{_prec}f}"
                            tp = f"{_new_tp:.{_prec}f}"
                            logger.info(
                                "[EXEC] %s %s: FIFO override (%s+0.5pip) — existing trade %s same-direction open. "
                                "live SL=%s TP=%s → new SL=%s TP=%s (validator wanted SL=%s TP=%s)",
                                instrument, direction.upper(), _src, _ext_id,
                                _live_sl, _live_tp, sl, tp, _orig_sl, _orig_tp
                            )
                except Exception as _fifo_err:
                    logger.warning(
                        "[EXEC] %s FIFO pre-flight check failed: %s — proceeding with original SL/TP",
                        instrument, _fifo_err
                    )

                # ── Direct Python execution — same path as snipe_direct ────────
                # The execution agent LLM adds zero value here: direction, units,
                # SL, and TP are all already computed. Routing through an LLM
                # costs ~60s and can silently fail (returns prose instead of tool call).
                # Direct place_market_order is deterministic, <1s, and identical to
                # the snipe_direct path that handles all watch-triggered trades.
                logger.info("[EXEC] %s %s: direct Python execution (bypassing LLM agent) "
                            "units=%d SL=%s TP=%s", instrument, direction.upper(), units, sl, tp)
                try:
                    from Source.agents.wrappers import (
                        fetch_candles as _fc2, get_account_summary as _gas2,
                        place_market_order as _pmo2,
                    )
                except ImportError:
                    from agents.wrappers import place_market_order as _pmo2

                # ── Tight-fan gate (2026-05-14): also block validator/exec path ──
                try:
                    if tc_get("gate.tight_fan_enabled", True):
                        from tight_fan_gate import check_tight_fan_gate
                        _tf_raw2 = fetch_candles(instrument, "M15", 150)
                        _tf_candles2 = _tf_raw2.get("candles", []) if isinstance(_tf_raw2, dict) else (_tf_raw2 or [])
                        _tf_result2 = check_tight_fan_gate(_tf_candles2, direction)
                        if _tf_result2["block"]:
                            logger.info("[TIGHT_FAN_GATE BLOCK] %s %s (exec path): %s | data=%s",
                                        instrument, direction, _tf_result2["reason"], _tf_result2["data"])
                            if flight:
                                try:
                                    flight.record(FlightStage.WATCH_GATE_BLOCKED, pair=instrument,
                                                  cycle_id=_cycle_id, status="blocked",
                                                  data={"gate": "tight_fan",
                                                        "reason": _tf_result2["reason"],
                                                        "path": "validator_exec",
                                                        **_tf_result2["data"]})
                                except Exception:
                                    pass
                            cycle_result["status"] = "skipped"
                            cycle_result["skip_reason"] = f"tight_fan_gate: {_tf_result2['reason']}"
                            return cycle_result
                except Exception as _tf_exc2:
                    logger.warning("[TIGHT_FAN_GATE] exec fail-open: %s", _tf_exc2)
                # ──────────────────────────────────────────────────────────────────

                _direct_fill = _pmo2(
                    instrument=instrument,
                    units=units,
                    direction=direction,
                    stop_loss=str(sl),
                    take_profit=str(tp),
                    confluence_score=decision.get("confluence_score"),
                    cycle_id=_cycle_id,
                )

                # Normalise fill into execution_result — same shape as snipe_direct
                execution_result = {"status": "unknown"}
                if isinstance(_direct_fill, dict):
                    _tid = (_direct_fill.get("trade_id") or
                            _direct_fill.get("tradeId") or
                            _direct_fill.get("tradeOpened", {}).get("tradeID"))
                    if _tid:
                        execution_result = {
                            "status":      "filled",
                            "trade_id":    str(_tid),
                            "entry_price": float(_direct_fill.get("entry_price") or _direct_fill.get("price") or _direct_fill.get("fullVWAP") or 0),
                            "units":       int(_direct_fill.get("units") or units),
                            "stop_loss":   str(sl),
                            "take_profit": str(tp),
                        }
                        logger.info("[EXEC] %s direct fill OK: trade_id=%s entry=%.5f",
                                    instrument, _tid, execution_result["entry_price"])
                    elif _direct_fill.get("status") == "error":
                        execution_result = {
                            "status": "rejected",
                            "error":  _direct_fill.get("error", "OANDA rejected order"),
                        }
                        logger.warning("[EXEC] %s direct fill REJECTED: %s",
                                       instrument, execution_result["error"])
                    else:
                        execution_result = {
                            "status": "execution_failed",
                            "error":  f"place_market_order returned no trade_id: {_direct_fill}",
                        }
                        logger.error("[EXEC] %s direct fill: no trade_id in response: %s",
                                     instrument, _direct_fill)
                else:
                    execution_result = {
                        "status": "execution_failed",
                        "error":  f"place_market_order returned unexpected type: {type(_direct_fill)}",
                    }

                # Build exec_agent_result stub so downstream code that reads
                # .get("tool_calls") etc. doesn't break
                exec_agent_result = {
                    "response":      f"Direct Python execution: {execution_result.get('status')}",
                    "tool_calls":    [{"tool": "place_market_order", "output": str(_direct_fill)}],
                    "input_tokens":  0,
                    "output_tokens": 0,
                    "rounds":        1,
                }

                # execution_result already fully populated by direct Python call above.
                # No LLM parsing needed — trade_id comes directly from OANDA response.

                self._post_result(
                    task_id, "execution", MessageType.EXECUTION_REPORT,
                    f"Order placed: {decision['action']} {instrument} "
                    f"(status={execution_result.get('status', 'unknown') if isinstance(execution_result, dict) else 'unknown'})",
                    execution_result if isinstance(execution_result, dict) else {},
                )

                cycle_result["execution"] = execution_result
                cycle_result["steps_completed"].append("execution")

                # Flight: execution
                if flight:
                    _exec_status = execution_result.get("status", "unknown") if isinstance(execution_result, dict) else "unknown"
                    flight.record(FlightStage.EXECUTION, pair=instrument, cycle_id=_cycle_id,
                                  trade_id=str(execution_result.get("trade_id", "")) if isinstance(execution_result, dict) else "",
                                  data={
                        "status": _exec_status,
                        "trade_id": execution_result.get("trade_id") if isinstance(execution_result, dict) else None,
                        "entry_price": execution_result.get("entry_price") if isinstance(execution_result, dict) else None,
                        "units": execution_result.get("units") if isinstance(execution_result, dict) else None,
                    }, duration_ms=(time.time() - phase_start) * 1000,
                    note=f"Order {_exec_status}")

                # Log trade (LOGS-02)
                if isinstance(execution_result, dict) and execution_result.get("status") == "filled":
                    try:
                        # Build EMA snapshot for trade record
                        _ema_snapshot = {}
                        if ema_result and not ema_result.get("error"):
                            _ema_snapshot = {
                                "fan_direction": ema_result.get("fan_direction"),
                                "fan_state": ema_result.get("fan_state"),
                                "fan_ordered": ema_result.get("fan_ordered"),
                                "separation_pct": ema_result.get("separation_pct"),
                                "separation_velocity": ema_result.get("separation_velocity"),
                                "fan_velocity_trend": ema_result.get("fan_velocity_trend"),
                                "trend_health": ema_result.get("trend_health"),
                                "reversal_risk": ema_result.get("reversal_risk"),
                                "recommended_bias": ema_result.get("recommended_bias"),
                                "ema100_role": ema_result.get("ema100_role"),
                                "gap_21_55": ema_result.get("gap_21_55"),
                                "gap_55_100": ema_result.get("gap_55_100"),
                                "gap_price_100": ema_result.get("gap_price_100"),
                                "e100_candle_pattern": ema_result.get("e100_candle_pattern"),
                                "narrative": ema_result.get("narrative"),
                            }
                        
                        # Build market picture snapshot
                        _mkt_snapshot = {}
                        if market_picture:
                            _mkt_snapshot = {
                                "rsi": market_picture.get("rsi", {}),
                                "stochastic": market_picture.get("stochastic", {}),
                                "bollinger": market_picture.get("bollinger", {}),
                                "confluence_narrative": market_picture.get("confluence_narrative", ""),
                            }
                        
                        self._get_logger().log_trade(
                            cycle_id=f"cycle_{cycle_num}_{cycle_start}",
                            trade_id=execution_result.get("trade_id", ""),
                            instrument=instrument,
                            direction=decision.get("action", ""),
                            entry_price=execution_result.get("entry_price", 0),
                            units=execution_result.get("units", 0),
                            stop_loss=execution_result.get("stop_loss", ""),
                            take_profit=execution_result.get("take_profit", ""),
                            risk_profile=decision.get("profile", "default"),
                            confluence_score=decision.get("confluence_score", 0),
                            patterns_triggered=(
                                analysis_results.get("candlestick_patterns", {}).get("filtered_patterns", [])
                                if isinstance(analysis_results, dict) else []
                            ),
                            mcp_data_used=intelligence_data,
                            client_extensions=execution_result.get("client_extensions", {}),
                            ema_snapshot=_ema_snapshot,
                            market_picture_snapshot=_mkt_snapshot,
                        )
                    except Exception as exc:
                        logger.warning("Trade logging failed: %s", exc)

                    # ── 6b. Record in live_trades (cycle/scout trades) ──────
                    # The snipe_direct path has its own INSERT (line ~3049).
                    # This covers all NON-snipe cycle trades (scout cascade,
                    # reentry, manual cycle runs) so they show on the dashboard.
                    try:
                        from db_pool import get_trading_forex as _gtf_cycle
                        _lt_cycle = _gtf_cycle()
                        _ct_tid = str(execution_result.get('trade_id', ''))
                        _ct_dir = decision.get('action', '').lower()
                        _ct_entry = float(execution_result.get('entry_price', 0))
                        _ct_sl = float(execution_result.get('stop_loss', 0) or 0)
                        _ct_tp = float(execution_result.get('take_profit', 0) or 0)
                        _ct_units = abs(int(execution_result.get('units', 0)))
                        _ct_src = 'scout' if (scout_context and not scout_context.get('_snipe_filled')) else 'cycle'
                        _ct_now = datetime.now(timezone.utc).isoformat()
                        _ct_fan_dir = ''
                        _ct_fan_state = ''
                        _ct_story = 0
                        if scout_context:
                            _ct_fan_dir = scout_context.get('fan_direction', '')
                            _ct_fan_state = scout_context.get('fan_state', '')
                            _ct_story = scout_context.get('opportunity_score', scout_context.get('story_score', 0))
                        _lt_cycle.execute("""
                            INSERT OR IGNORE INTO live_trades (
                                id, source, oanda_trade_id, pair, timeframe,
                                direction, entry_time, entry_price, sl_price, tp_price,
                                status, user_id, units, cycle_id, entry_type,
                                fan_state, fan_direction, story_score,
                                setup, base_setup
                            ) VALUES (
                                ?, ?, ?, ?, 'M15',
                                ?, ?, ?, ?, ?,
                                'open', ?, ?, ?, ?,
                                ?, ?, ?,
                                ?, ?
                            )
                        """, (
                            _ct_tid, _ct_src, _ct_tid, instrument,
                            _ct_dir, _ct_now, _ct_entry, _ct_sl, _ct_tp,
                            getattr(self, 'user_id', None), _ct_units,
                            _cycle_id, _ct_src,
                            _ct_fan_state, _ct_fan_dir, _ct_story,
                            # 2026-04-16: setup_id lives in scout_context (from classify_setups),
                            # NOT in decision. decision.get('setup_id') was always 'unknown'.
                            (scout_context or {}).get('setup_id', 'unknown'),
                            (scout_context or {}).get('setup_id', 'unknown'),
                        ))
                        _lt_cycle.commit()
                        logger.info("[LT] Created live_trades row for %s %s trade %s (source=%s)",
                                    instrument, _ct_dir, _ct_tid, _ct_src)
                    except Exception as _lt_cyc_err:
                        logger.warning("[LT] Failed to create live_trades for cycle trade %s: %s",
                                       instrument, _lt_cyc_err)

                    # ── Telegram: trade opened notification ──────────────────
                    try:
                        from trade_notify import notify_trade_opened
                        _snipe_id = (scout_context or {}).get("watch_id") or \
                                    (scout_context or {}).get("snipe_id")
                        _src = "sniper" if _snipe_id else "trading_team"
                        notify_trade_opened(
                            trade_id=str(execution_result.get("trade_id", "?")),
                            pair=instrument,
                            direction=decision.get("action", ""),
                            units=abs(int(execution_result.get("units", 0))),
                            entry_price=float(execution_result.get("entry_price", 0)),
                            sl_price=float(execution_result.get("stop_loss", 0) or 0),
                            tp_price=float(execution_result.get("take_profit", 0) or 0),
                            source=_src,
                            snipe_id=str(_snipe_id) if _snipe_id else None,
                        )
                    except Exception as _ntf_e:
                        logger.debug("Trade open notification failed: %s", _ntf_e)

                    # Notify Position Guardian to start watching this trade immediately
                    # Guardian reconciles with OANDA every 15s anyway, but this is instant.
                    try:
                        from Source.trading_api_routes import _trigger_guardian_reconcile, _guardian_instance
                        # Register thesis context so guardian knows the trade's story
                        if _guardian_instance and scout_context:
                            _guardian_instance.register_thesis(instrument, {
                                'entry_type': scout_context.get('story_entry_type', scout_context.get('entry_type', 'unknown')),
                                'thesis': scout_context.get('story_thesis', scout_context.get('reasoning', '')),
                                'direction': scout_context.get('direction', ''),
                                'fan_state_at_entry': scout_context.get('market_snapshot', {}).get('fan_state', ''),
                                'opportunity_score': scout_context.get('opportunity_score', 0),
                                'setup_id': scout_context.get('setup_id', ''),
                            })
                        _trigger_guardian_reconcile()
                        logger.info("Position Guardian notified — trade %s will be watched (thesis: %s)",
                                   execution_result.get("trade_id"),
                                   scout_context.get('story_entry_type', 'unknown') if scout_context else 'none')
                    except Exception as exc:
                        logger.debug("Guardian notification deferred (reconcile will catch it): %s", exc)

            else:
                # Post hold decision
                hold_reason = (
                    decision.get("blocking_reasons", decision.get("reasons", []))
                    if isinstance(decision, dict) else []
                )
                self._post_result(
                    task_id, "trading_orchestrator", MessageType.STATUS_UPDATE,
                    f"Hold: {hold_reason}",
                    {"action": "hold", "reasons": hold_reason},
                )
                cycle_result["steps_completed"].append("hold_decision")
                # Flight: hold decision
                if flight:
                    flight.record(FlightStage.EXECUTION, pair=instrument, cycle_id=_cycle_id, data={
                        "status": "hold",
                    }, note=f"HOLD: {'; '.join(hold_reason[:2]) if hold_reason else 'no signal'}")
                cycle_result["decisions"].append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "agent": "cycle_orchestrator",
                    "action": "HOLD",
                    "result": "; ".join(hold_reason) if hold_reason else "No entry signal — holding",
                })

                # ── Watch Manager: parse validator suggestions into scheduled snipes ──
                # If the validator ran (not GATE1_BLOCK), always create a snipe.
                # The validator analyzed the chart — its conditions are actionable.
                _snipe_verdict = (validation_results.get("verdict", "") or "").upper() if isinstance(validation_results, dict) else ""
                if _snipe_verdict in ("REJECT", "GATE1_BLOCK", "VALIDATOR_PARSE_FAIL", "SKIP"):
                    # 2026-05-03: SKIP added — validator legitimately said "no setup",
                    # don't create a garbage snipe from sparse prose-regex extraction.
                    # Pre-fix, SKIP was remapped to WATCH at line ~6783 and slipped past
                    # this gate even though validator's re_entry_conditions was empty.
                    logger.info("[SNIPE GATE] %s: verdict=%s — validator did not endorse entry, skipping snipe", instrument, _snipe_verdict)
                elif (validation_results.get("confidence", 0) or 0) < 0.05 and (validation_results.get("confidence", 0) or 0) != 0:
                    logger.info("[SNIPE GATE] %s: confidence too low (%.2f) — not creating snipe", instrument, validation_results.get("confidence", 0))
                else:
                 try:
                    from Source.agents.watch_manager import parse_suggestions, create_watch
                    sniper_data = analysis_results.get("sniper_score", {}) if isinstance(analysis_results, dict) else {}
                    watch_configs = parse_suggestions(
                        validation_results if isinstance(validation_results, dict) else {},
                        instrument,
                        sniper_data=sniper_data,
                    )
                    # Build rich context for dashboard display
                    _val = validation_results if isinstance(validation_results, dict) else {}
                    _db_ev = _val.get("db_evidence", {}) if isinstance(_val.get("db_evidence"), dict) else {}
                    _fc = full_confluence if isinstance(full_confluence, dict) else {}
                    _snp = sniper_data if isinstance(sniper_data, dict) else {}
                    _ind = _snp.get("indicators", {})
                    _key_sigs = (ta_interp.get("key_signals", []) if isinstance(ta_interp, dict) else [])[:5]

                    # Build plain-English story
                    _dir = effective_direction or "neutral"
                    _buy = _snp.get("buy_score", 0)
                    _sell = _snp.get("sell_score", 0)
                    _thresh = _snp.get("threshold", 12)
                    _signal = _snp.get("signal", "HOLD")
                    _rsi = _ind.get("rsi", 50)
                    _adx = _ind.get("adx", 25)
                    _story_parts = [
                        f"{instrument} shows a {_dir} bias (Sniper {_buy}B/{_sell}S, needs {_thresh} to trigger).",
                    ]
                    if _rsi < 35:
                        _story_parts.append(f"RSI is oversold at {_rsi:.0f} — watching for a bounce.")
                    elif _rsi > 65:
                        _story_parts.append(f"RSI is overbought at {_rsi:.0f} — watching for a pullback.")
                    if _db_ev.get("best_win_rate"):
                        _story_parts.append(
                            f"Backtest shows {_db_ev['best_win_rate'] or 0:.0f}% win rate "
                            f"(PF {_db_ev.get('best_profit_factor') or 0:.2f}) "
                            f"across {_db_ev.get('best_trade_count') or 0} trades on similar setups."
                        )
                    _story_parts.append("Monitoring until entry conditions align.")

                    # Build thesis progress from TA output for the watch lightbox
                    _ta_exp = cycle_result.get("ta_explanation", {})
                    _ta_thesis_ctx = (_ta_exp.get("thesis_progress") or
                                      (ta_interpretation or {}).get("thesis_progress") or {})

                    # 2026-04-27: normalize direction to 'buy'/'sell' to match
                    # watch_suggestions.direction column. Without this, _dir was
                    # 'bullish'/'bearish' (the effective_direction format) and
                    # watch_manager.create_watch's normalization filtered it to
                    # NULL. Watches 2204, 2205, 2208 were created with NULL direction
                    # because of this. Use the same normalize helper as watch_manager.
                    from Source.agents.watch_manager import _normalize_direction
                    _watch_dir_normalized = (
                        _normalize_direction(_val.get("re_entry_direction"))
                        or _normalize_direction(_dir)
                    )
                    _watch_context = {
                        "setup_story": " ".join(_story_parts),
                        "story_score": _story_score,
                        "auto_thesis": _auto_thesis[:500],
                        # Fix: store the INTENDED trade direction (re_entry_direction),
                        # not the current fan direction (_dir). Reversal setups were
                        # storing "bullish" fan state as direction, then executing BUY
                        # when conditions fired, even though the trade was a SELL setup.
                        # 2026-04-27: now normalized to 'buy'/'sell' (was 'bullish'/'bearish').
                        "direction": _watch_dir_normalized or _val.get("re_entry_direction") or _dir,
                        "thesis_progress": _ta_thesis_ctx,
                        "confluence_score": _fc.get("total_score", 0),
                        "confluence_min": int(risk_limits.get("min_confluence", 30)),
                        "db_win_rate": _db_ev.get("best_win_rate"),
                        "db_profit_factor": _db_ev.get("best_profit_factor"),
                        "db_trade_count": _db_ev.get("best_trade_count"),
                        "db_setup": _val.get("best_setup", ""),
                        "key_signals": _key_sigs,
                        "validator_reasoning": str(_val.get("reasoning", ""))[:500],
                        "re_entry_setup": _val.get("re_entry_setup", ""),
                        "re_entry_direction": _val.get("re_entry_direction", ""),
                        "re_entry_regime": _val.get("re_entry_regime", ""),
                        "estimated_candles_to_entry": _val.get("estimated_candles_to_entry"),
                        "price_target_entry": _val.get("price_target_entry"),
                        # Watch manifest — built from the validator's analysis
                        "watch_manifest": {
                            "fishing_line": {
                                "entry_zone_pips": _val.get("price_target_entry", ""),
                                "direction": _val.get("re_entry_direction", ""),
                                "time_limit_candles": _val.get("watch_check_candles", 8),
                            },
                            "trigger_conditions": [
                                {"indicator": c.get("field", ""), "required": str(c.get("value", "")),
                                 "reason": c.get("reason", c.get("desc", "")),
                                 "current": "", "progress_pct": 0}
                                for c in (_val.get("re_entry_conditions") or [])
                            ],
                            "confidence_at_cast": _val.get("confidence", 0),
                            "confidence_trend": _val.get("confidence_trajectory", "stable") or "stable",
                        } if _val.get("re_entry_conditions") else None,
                        "watch_trigger": (
                            (f"Entry at {_val.get('price_target_entry')}. " if _val.get("price_target_entry") else "")
                            + "; ".join(c.get("reason", c.get("desc", "")) for c in (_val.get("re_entry_conditions") or [])[:3])
                        ),
                        "watch_for": str(_val.get("reasoning", ""))[:300],
                        "confidence_trajectory": _val.get("confidence_trajectory", "stable") or "stable",
                        # Dual-window expansion data (available for watch conditions)
                        "fan_delta_5bar": _v4_fan_delta,
                        "fan_delta_20bar": _v4_fan_delta_20,
                        "bb_delta_5bar": _v4_bb_delta,
                        "bb_delta_20bar": _v4_bb_delta_20,
                        # Scout alert linkage — lets record_outcome write back to scout_alerts
                        "scout_alert_id": (scout_context or {}).get("scout_alert_id"),
                        # Scout finding linkage — lets pipeline lineage trace scout→snipe
                        "finding_id": (scout_context or {}).get("finding_id"),
                        # Setup ID — flows from scout classifier through to Gate 5a when watch triggers
                        "setup_id": (scout_context or {}).get("setup_id") or _val.get("best_setup", ""),
                        "setup_name": (scout_context or {}).get("setup_name") or f"V4_{_val.get('re_entry_setup', '')}",
                        # Classified setups from scout — preserved for snipe card context
                        "classified_setups": (scout_context or {}).get("classified_setups", []),
                    }

                    # Create watches for all non-TRADE_NOW verdicts.
                    # REJECT = dead chart right now, but we still watch for thesis to form.
                    # The only time we skip watch creation is if parse_suggestions returned nothing
                    # (which now falls back to checklist-derived conditions, not generic story checks).
                    # Log whether conditions came from structured validator output or fallback.
                    _has_structured = any(
                        wc.get("suggestion_type") == "validator_structured"
                        for wc in watch_configs
                    )
                    if _has_structured:
                        logger.info("[WATCH] %s: using validator-structured conditions (%d watches)",
                                    instrument, len(watch_configs))
                    elif watch_configs:
                        logger.info("[WATCH] %s: using checklist-derived fallback conditions (%d watches)",
                                    instrument, len(watch_configs))
                    else:
                        # 2026-04-28: Empty-conditions guard. Validator sometimes returns
                        # verdict=WATCH with re_entry_conditions=[] (model violating its own
                        # min-5 rule). Pre-fix this was silently dropped — no flight_log,
                        # no SKIP recorded, just nothing. Now: emit visible event and
                        # explicitly mark the cycle as skipped so the rate is monitorable.
                        # Per Tim's principle "watches and snipes are the same thing" —
                        # validator saying WATCH but producing no snipe violates that.
                        _empty_re_count = len(_val.get("re_entry_conditions") or [])
                        logger.warning(
                            "[WATCH GUARD] %s: validator returned %s with %d re_entry_conditions "
                            "and no parseable fallback — promoting to SKIP. The validator violated "
                            "its own min-5-conditions rule. (verdict was %s, conf=%s)",
                            instrument, _snipe_verdict, _empty_re_count,
                            _val.get("verdict"), _val.get("confidence")
                        )
                        if flight:
                            try:
                                flight.record(
                                    "watch_dropped_no_conditions",
                                    pair=instrument,
                                    cycle_id=_cycle_id,
                                    status="skip",
                                    data={
                                        "verdict": _val.get("verdict"),
                                        "confidence": _val.get("confidence"),
                                        "re_entry_count": _empty_re_count,
                                        "reasoning_excerpt": str(_val.get("reasoning",""))[:200],
                                    },
                                    note=f"validator returned {_val.get('verdict')} with {_empty_re_count} conditions — dropped",
                                )
                            except Exception:
                                logger.exception("[WATCH GUARD] flight.record failed")
                        # Treat as SKIP — do not create a watch, do not fall through
                        # to the create_watch loop (watch_configs is empty anyway).
                        cycle_result["skip_reason"] = "watch_dropped_no_conditions"
                        cycle_result["skip_detail"] = (
                            f"Validator returned {_val.get('verdict')} but no actionable "
                            f"conditions; promoted to SKIP per snipe-must-be-snipe rule."
                        )

                    _watch_wr = _db_ev.get("best_win_rate", 0) or 0
                    if _watch_wr > 0:
                        logger.info("[WATCH] %s: backtest WR %.1f%% — creating watch", instrument, _watch_wr)
                    else:
                        logger.info("[WATCH] %s: no backtest data — creating watch (validator authority)", instrument)

                    for wc in watch_configs:
                        watch_id = create_watch(
                            cycle_id=f"cycle_{cycle_num}_{cycle_start}",
                            instrument=instrument,
                            watch_config=wc,
                            validator_response=_val,
                            cycle_context=_watch_context,
                            user_id=self.user_id,
                        )
                        if watch_id:
                            # Dedup: create_watch() returns a dict when an existing
                            # similar watch was found, or an int for a new watch.
                            _is_dedup = isinstance(watch_id, dict) and watch_id.get("dedup")
                            _wid = watch_id["id"] if _is_dedup else watch_id

                            if _is_dedup:
                                _pct = watch_id.get("criteria_pct", 0)
                                _met = watch_id.get("criteria_met", 0)
                                _total = watch_id.get("criteria_total", 0)
                                _peak = watch_id.get("peak_progress", 0)
                                _sim = watch_id.get("similarity", 0)
                                _swarm_send_message(
                                    "cycle_orchestrator", "watch_manager",
                                    f"[SNIPE EXISTS] #{_wid} for {instrument} — "
                                    f"already tracking a {_sim}% similar snipe. "
                                    f"Currently {_met}/{_total} criteria met ({_pct:.0f}%), "
                                    f"peak {_peak:.0f}%. No new snipe created.",
                                )
                                _log_phase("validator",
                                          f"Snipe exists #{_wid} ({_pct:.0f}% progress, "
                                          f"{_sim}% similar) — skipped duplicate",
                                          0.0)
                                cycle_result.setdefault("watches_deduped", []).append({
                                    "existing_watch_id": _wid,
                                    "instrument": instrument,
                                    "similarity": _sim,
                                    "criteria_pct": _pct,
                                    "peak_progress": _peak,
                                })
                            else:
                                _swarm_send_message(
                                    "cycle_orchestrator", "watch_manager",
                                    f"[WATCH CREATED] #{_wid} for {instrument}: {wc.get('raw_text', '')} "
                                    f"(checking every 5min, expires in 4h)",
                                )
                                _log_phase("validator",
                                          f"Snipe #{_wid}: {wc.get('raw_text', '')}",
                                          0.0)
                                cycle_result.setdefault("watches_created", []).append({
                                    "watch_id": _wid,
                                    "instrument": instrument,
                                    "type": wc.get("suggestion_type"),
                                    "conditions": wc.get("raw_text"),
                                })
                    # Store watch context for dashboard display
                    if watch_configs and _watch_context:
                        cycle_result["watch_context"] = _watch_context

                    # Flight: watch/snipe creation
                    if flight and cycle_result.get("watches_created"):
                        flight.record(FlightStage.WATCH_CREATE, pair=instrument, cycle_id=_cycle_id, data={
                            "watches": len(cycle_result["watches_created"]),
                            "types": [w.get("type") for w in cycle_result["watches_created"]],
                        }, note=f"{len(cycle_result['watches_created'])} watches created")
                 except Exception as watch_exc:
                    logger.warning("Watch manager failed: %s", watch_exc)

            phase_elapsed = time.time() - phase_start
            phase_timings["execution"] = phase_elapsed
            logger.info("[TIMING] execution: %.2fs", phase_elapsed)
            _report_agent_performance("execution", True, phase_elapsed)
            _log_phase("execution", "Order execution phase complete", phase_elapsed)

        except Exception as exc:
            phase_elapsed = time.time() - phase_start
            phase_timings["execution"] = phase_elapsed
            logger.info("[TIMING] execution: %.2fs (failed)", phase_elapsed)
            _report_agent_performance("execution", False, phase_elapsed)
            logger.error("Cycle #%d execution failed: %s", cycle_num, exc)
            self._post_error(task_id, "execution", str(exc))
            cycle_result["execution_error"] = str(exc)

        # Step 7: Trade Monitor Integration — notify trade_monitor that a trade is live
        if isinstance(execution_result, dict) and execution_result.get("status") == "filled":
            try:
                _tm_trade_id = execution_result.get("trade_id", "?")
                _tm_dir      = execution_result.get("direction", "?")
                _tm_entry    = execution_result.get("fill_price", "?")
                _tm_sl       = execution_result.get("sl_price", "?")
                _tm_tp       = execution_result.get("tp_price", "?")
                _tm_task = (
                    f"Trade {_tm_trade_id} is now live: {instrument} {_tm_dir.upper()} "
                    f"@ {_tm_entry} | SL {_tm_sl} | TP {_tm_tp}. "
                    f"Monitor this position. Check guardian threat levels every 5 minutes. "
                    f"Alert orchestrator if: threat > 60, unrealized loss > 1% account, "
                    f"high-impact news within 30min. Report status."
                )
                _agent_task("trade_monitor", _tm_task, max_tokens=256, timeout=20.0)
                logger.info("Trade placed — trade_monitor notified for %s %s", instrument, _tm_trade_id)
            except Exception as _tm_err:
                logger.debug("trade_monitor notification failed (non-critical): %s", _tm_err)
        
        # Step 8: Post-Trade Reporting and Dashboard Update
        phase_start = time.time()
        try:
            cycle_data = {
                "instrument": instrument,
                "data_collection": cycle_result.get("data_collection"),
                "analysis": analysis_results if isinstance(analysis_results, dict) else {},
                "intelligence": intelligence_data,
                "validation": validation_results if isinstance(validation_results, dict) else {},
                "decision": decision if isinstance(decision, dict) else {},
                "execution": execution_result,
                "cycle_start": cycle_start,
            }

            _swarm_send_message(
                "cycle_orchestrator", "reporter",
                f"Generate cycle #{cycle_num} summary for {instrument}",
            )
            report_result = _swarm_execute_tool(
                "reporter", "generate_cycle_summary",
                cycle_data=cycle_data,
            )
            summary = report_result.get("tool_result", report_result)

            self._post_result(
                task_id, "reporting", MessageType.CYCLE_SUMMARY,
                f"Cycle #{cycle_num} complete: traded={summary.get('trade_placed', False) if isinstance(summary, dict) else False}",
                summary if isinstance(summary, dict) else {},
            )

            # Log trade to knowledge if we traded
            if isinstance(execution_result, dict) and execution_result.get("status") == "filled":
                _swarm_execute_tool(
                    "reporter", "log_trade_to_knowledge",
                    instrument=instrument,
                    trade_result=execution_result,
                )

            cycle_result["summary"] = summary
            cycle_result["steps_completed"].append("reporting")

            # Write cycle_data.json for dashboard (Step 5 requirement)
            self._write_dashboard_data(cycle_num, instrument, decision, phase_timings, summary)

            # ── Health Check: post-cycle pipeline sanity scan ──
            try:
                from Source.cycle_health_check import run_health_check
                health_findings = run_health_check(cycle_result, instrument)
                if health_findings:
                    cycle_result["health_findings"] = health_findings
                    critical = [f for f in health_findings if f["severity"] == "CRITICAL"]
                    warnings = [f for f in health_findings if f["severity"] == "WARNING"]
                    info = [f for f in health_findings if f["severity"] == "INFO"]
                    _log_phase("reporter",
                              f"Health check: {len(critical)} critical, {len(warnings)} warnings, {len(info)} info",
                              0.0)
                    if critical:
                        logger.warning("CRITICAL health findings for %s: %s",
                                      instrument, "; ".join(f["message"][:80] for f in critical))
                    if flight:
                        flight.record(FlightStage.DASHBOARD_PUSH, pair=instrument, cycle_id=_cycle_id, data={
                            "health_check": True,
                            "critical": len(critical),
                            "warnings": len(warnings),
                            "info": len(info),
                            "findings": [f["message"][:120] for f in health_findings[:5]],
                        }, note=f"health: {len(critical)}C {len(warnings)}W {len(info)}I")
            except Exception as hc_exc:
                logger.warning("Health check failed (non-blocking): %s", hc_exc)

            phase_elapsed = time.time() - phase_start
            phase_timings["reporting"] = phase_elapsed
            logger.info("[TIMING] reporting: %.2fs", phase_elapsed)
            _report_agent_performance("reporter", True, phase_elapsed)
            _log_phase("reporter", "Generated cycle report and logged to knowledge base", phase_elapsed)

        except Exception as exc:
            phase_elapsed = time.time() - phase_start
            phase_timings["reporting"] = phase_elapsed
            logger.info("[TIMING] reporting: %.2fs (failed)", phase_elapsed)
            _report_agent_performance("reporter", False, phase_elapsed)
            logger.error("Cycle #%d reporting failed: %s", cycle_num, exc)
            # Reporting failure doesn't affect trade outcome

        # Step 9: Collect full cycle thread and store decision to knowledge
        # Collect full conversation from workspace comments for audit trail
        if task_id is not None:
            try:
                full_thread = self._protocol.get_cycle_thread(task_id=task_id)
                cycle_result["full_thread"] = full_thread
                logger.info(
                    "Cycle #%d: Collected full thread with %d comments",
                    cycle_num, len(full_thread),
                )
            except Exception as exc:
                logger.warning("Failed to collect cycle thread: %s", exc)

        # Store decision to knowledge store (learning loop)
        try:
            ks = _get_knowledge_store()
            if ks is not None:
                ta_summary = {}
                if isinstance(analysis_results, dict):
                    confluence = analysis_results.get("confluence", {})
                    _snp = analysis_results.get("sniper_score", {}) if isinstance(analysis_results, dict) else {}
                    ta_summary = {
                        "sniper_buy": _snp.get("buy_score", 0),
                        "sniper_sell": _snp.get("sell_score", 0),
                        "direction": _snp.get("direction", "neutral"),
                        "signal": _snp.get("signal", "HOLD"),
                    }
                intel_summary = {
                    "sources": intelligence_data.get("sources_available", []),
                    "news_sentiment": (
                        intelligence_data.get("news", {}).get("sentiment")
                        if isinstance(intelligence_data.get("news"), dict)
                        else None
                    ),
                }
                decision_action = (
                    decision.get("action", "hold")
                    if isinstance(decision, dict) else "hold"
                )
                ks.save_performance(
                    instrument=instrument,
                    metric_name=f"cycle_{cycle_num}_{decision_action}",
                    value={
                        "action": decision_action,
                        "allowed": decision.get("allowed", False) if isinstance(decision, dict) else False,
                        "ta_summary": ta_summary,
                        "intelligence": intel_summary,
                        "executed": execution_result is not None,
                        "timestamp": cycle_start,
                    },
                    period="cycle",
                )
        except Exception as exc:
            logger.warning("Failed to store decision to knowledge: %s", exc)

        self._last_cycle_time = datetime.now(timezone.utc).isoformat()
        cycle_result["end_time"] = self._last_cycle_time
        cycle_result["status"] = "completed"

        # Finalize timing
        total_elapsed = time.time() - cycle_clock
        phase_timings["total"] = total_elapsed
        cycle_result["timing"] = {
            "total": total_elapsed,
            "phases": phase_timings,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "[TIMING] Full cycle: %.2fs | Phases: %s",
            total_elapsed, phase_timings,
        )

        logger.info(
            "=== Cycle #%d complete: %s | steps=%s ===",
            cycle_num, instrument, cycle_result["steps_completed"],
        )

        if flight:
            flight.record(FlightStage.CYCLE_END, pair=instrument, cycle_id=_cycle_id, data={
                "total_time_s": total_elapsed,
                "steps_completed": cycle_result["steps_completed"],
                "action": decision.get("action", "hold") if isinstance(decision, dict) else "error",
                "status": cycle_result.get("status", "unknown"),
                "confluence": full_confluence.get("total_score", 0) if isinstance(full_confluence, dict) else 0,
            }, duration_ms=total_elapsed * 1000,
            note=f"{decision.get('action', 'hold').upper() if isinstance(decision, dict) else 'ERROR'} in {total_elapsed:.1f}s")

        # Write cycle data to dashboard JSON
        try:
            # Compute dashboard path relative to trading_cycle.py
            dashboard_path = Path(__file__).parent.parent.parent / "dashboard" / "cycle_data.json"
            dashboard_path.parent.mkdir(exist_ok=True)
            
            # Include additional data for dashboard
            dashboard_data = dict(cycle_result)  # Copy all cycle result data
            dashboard_data.update({
                "agent_list": [spec["name"] for spec in [
                    {"name": "oanda_data"}, {"name": "technical_analyst"}, {"name": "intelligence"},
                    {"name": "validator"},
                    {"name": "execution"}, {"name": "reporter"}, {"name": "cycle_orchestrator"}
                ]],
                "comment_protocol_messages": cycle_result.get("full_thread", []),
                "export_timestamp": datetime.now(timezone.utc).isoformat(),
            })
            
            # Write atomically (write to .tmp then rename)
            tmp_path = dashboard_path.with_suffix('.json.tmp')
            with open(tmp_path, 'w') as f:
                json.dump(dashboard_data, f, indent=2, default=str)
            tmp_path.rename(dashboard_path)
            
            logger.info("Exported cycle #%d data to dashboard: %s", cycle_num, dashboard_path)
            if flight:
                flight.record(FlightStage.DASHBOARD_PUSH, pair=instrument, cycle_id=_cycle_id, data={
                    "wrote_file": True,
                })
        except Exception as exc:
            logger.warning("Failed to export dashboard data: %s", exc)

        # ── Persist decision to trade_decisions table ──
        _conn = None
        try:
            import sqlite3 as _sq3
            import os as _os
            _db_path = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))),
                "Database", "v2", "trading_forex.db",
            )
            _dec = cycle_result.get("decision", {})
            _val = cycle_result.get("validation", {})
            if isinstance(_val, str):
                _val = {}
            _intel = cycle_result.get("intelligence", cycle_result.get("intelligence_data", {}))
            if isinstance(_intel, str):
                _intel = {}
            
            _conn = _sq3.connect(_db_path, timeout=30, isolation_level=None)
            _conn.execute("PRAGMA journal_mode=DELETE")
            _conn.execute("PRAGMA busy_timeout=30000")
            # Build market story snapshot from scout context for revenue tracking
            _story_snapshot = None
            _sc = scout_context if scout_context else {}
            _ms = _sc.get("market_snapshot", {})
            # Scout sends story fields at top level of scout_context AND in market_snapshot
            # Merge both levels: top-level scout_context fields take priority
            _merged = {**_ms, **_sc} if _ms else _sc
            if _merged and (_merged.get("entry_type") or _merged.get("fan_state") or _merged.get("story_thesis")):
                _cs = _merged.get("candle_structure", {}) if isinstance(_merged.get("candle_structure"), dict) else {}
                _story_snapshot = json.dumps({
                    "entry_type": _merged.get("entry_type", _merged.get("story_entry_type", "")),
                    "story_thesis": _merged.get("story_thesis", ""),
                    "opportunity_score": _merged.get("opportunity_score", _merged.get("story_opportunity_score", _merged.get("score", 0))),
                    "confidence": _merged.get("story_confidence", _merged.get("confidence", 0)),
                    "fan_state": _merged.get("fan_state", ""),
                    "fan_direction": _merged.get("fan_direction", ""),
                    "velocity_trend": _merged.get("velocity_trend", ""),
                    "trend_health": _merged.get("trend_health", 0),
                    "momentum_state": _merged.get("momentum_state", ""),
                    "momentum_significance": _merged.get("momentum_significance", ""),
                    "momentum_exhausted": _merged.get("momentum_exhausted", False),
                    "e100_interaction": _cs.get("e100_interaction", "none"),
                    "wick_pressure": _cs.get("wick_pressure", ""),
                    "body_trend": _cs.get("body_trend", ""),
                    "recommended_bias": _merged.get("recommended_bias", ""),
                }, default=str)

            _conn.execute("""INSERT INTO trade_decisions (
                timestamp, pair, timeframe, setup, direction, regime,
                market_agent_data, news_agent_data, weather_agent_data, wolfram_agent_data,
                validator_verdict, validator_confidence, validator_reasoning,
                validator_db_evidence, validator_loss_patterns, validator_confluence,
                recommended_rr, recommended_sl,
                final_action, final_action_reason,
                execution_time_ms, created_at, market_story_snapshot, user_id
            ) VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?, ?,?, ?,?, ?,?, ?,?)""", (
                datetime.now(timezone.utc).isoformat(),
                instrument,
                timeframe,
                _dec.get("setup_name", _dec.get("setup", "")),
                # direction must be 'long' or 'short' (CHECK constraint).
                # Cascade: decision → scout_context → fan_direction → market_story → 'long' fallback.
                # Previous bug: skipped fan_direction, so all WATCH/REJECT defaulted to 'long'.
                (lambda d: {"buy": "long", "sell": "short", "long": "long", "short": "short",
                            "bullish": "long", "bearish": "short", "bull": "long", "bear": "short"}.get(
                    (d or "").lower(), "long"
                ))(_dec.get("direction")
                   or (scout_context or {}).get("direction")
                   or _dec.get("bias")
                   or _merged.get("fan_direction", "")
                   or _merged.get("recommended_bias", "")
                   or (cycle_result.get("analysis", {}) or {}).get("fan_direction", "")
                ),
                _dec.get("regime", cycle_result.get("regime", "")),
                json.dumps(_intel.get("market", ""), default=str) if _intel else "",
                json.dumps(_intel.get("news", ""), default=str) if _intel else "",
                json.dumps(_intel.get("weather", ""), default=str) if _intel else "",
                json.dumps(_intel.get("wolfram", _intel.get("macro", "")), default=str) if _intel else "",
                _val.get("verdict", "") if isinstance(_val, dict) else "",
                _val.get("confidence", 0) if isinstance(_val, dict) else 0,
                _val.get("reasoning", "") if isinstance(_val, dict) else "",
                json.dumps(_val.get("db_evidence", {}), default=str) if isinstance(_val, dict) else "{}",
                json.dumps(_val.get("loss_patterns", []), default=str) if isinstance(_val, dict) else "[]",
                cycle_result.get("full_confluence", {}).get("total_score", 0),
                _dec.get("rr_mult", None),
                _dec.get("sl_mult", None),
                # final_action must be in (trade,skip,watchlist,defer). Map hold/watch → skip/watchlist.
                {"trade": "trade", "skip": "skip", "watchlist": "watchlist", "defer": "defer",
                 "hold": "watchlist", "watch": "watchlist"}.get(
                    _dec.get("action", _dec.get("llm_action", "skip")), "skip"),
                _dec.get("reason", _dec.get("reasoning", "")),
                int(cycle_result.get("total_time_ms", 0)),
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                _story_snapshot,
                _cycle_user_id,
            ))
            # Link the OANDA trade_id so guardian can find this decision at close
            _oanda_tid = None
            _exec = cycle_result.get("execution", {})
            if isinstance(_exec, dict):
                _oanda_tid = _exec.get("trade_id") or _exec.get("tradeId")
            if _oanda_tid:
                _conn.execute(
                    "UPDATE trade_decisions SET live_trade_id = ? WHERE rowid = last_insert_rowid()",
                    (str(_oanda_tid),)
                )

            _conn.commit()
            logger.info("Persisted decision for %s to trade_decisions (trade_id=%s)", instrument, _oanda_tid)
            
            # PROBLEM 1 FIX: Record trade execution in scout learning system
            if scout_context and scout_context.get('finding_id') and _oanda_tid:
                try:
                    from scout_learning_system import record_trade_execution
                    _entry_price = _exec.get("entry_price", 0)
                    _direction = _dec.get("action", "").lower()
                    record_trade_execution(str(_oanda_tid), _entry_price, _direction,
                                         finding_id=scout_context.get('finding_id'))
                    logger.info(f"Recorded trade execution #{_oanda_tid} for scout finding #{scout_context.get('finding_id')}")
                except Exception as e:
                    logger.warning(f"Failed to record trade execution: {e}")

            # Collect training pairs for MLX fine-tuning (happens when trade closes with win)
            # This just registers the cycle_id — actual collection happens in position_guardian
            # when the trade closes and we know the outcome
            if _oanda_tid:
                try:
                    # Store cycle_id mapping for later collection
                    # Position guardian will use this when trade closes
                    pass  # Collection happens at trade close in position_guardian
                except Exception as _tc_err:
                    logger.debug("Training cycle prep failed (non-critical): %s", _tc_err)
        except Exception as exc:
            logger.warning("Failed to persist trade_decisions: %s", exc)
        finally:
            if _conn:
                _conn.close()

        return cycle_result

    # ------------------------------------------------------------------
    # Position monitoring (between cycles)
    # ------------------------------------------------------------------

    def run_position_update(self, instruments: List[str]) -> dict:
        """Between-cycle position monitoring (AGNT-14).

        For each instrument with open trades, fetches latest candles
        and pricing via SwarmHandler, then runs the position monitor.

        Parameters
        ----------
        instruments : list[str]
            Instruments to check for open positions.

        Returns
        -------
        dict
            instruments_checked, actions_taken.
        """
        all_actions: List[Dict[str, Any]] = []
        instruments_checked = 0

        try:
            # Get open trades via execution agent
            pos_result = _swarm_execute_tool(
                "execution", "get_position_status",
            )
            positions_data = pos_result.get("tool_result", pos_result)
            positions = positions_data.get("positions", []) if isinstance(positions_data, dict) else []

            if not positions:
                return {"instruments_checked": 0, "actions_taken": []}

            # Build instrument-to-trades map
            inst_trades: Dict[str, List[str]] = {}
            for pos in positions:
                inst = pos.get("instrument", "")
                if inst in instruments:
                    inst_trades.setdefault(inst, []).append(pos.get("trade_id", ""))

            # For each instrument with positions, fetch data and update
            candles_by_instrument: Dict[str, Any] = {}
            current_prices: Dict[str, float] = {}

            for inst in inst_trades:
                try:
                    # Fetch H1 candles for ATR calculation
                    candle_result = _swarm_execute_tool(
                        "oanda_data", "fetch_candles",
                        instrument=inst, timeframe="H1", count=50,
                    )
                    candle_data = candle_result.get("tool_result", candle_result)
                    candles_by_instrument[inst] = candle_data.get("candles", []) if isinstance(candle_data, dict) else []

                    # Fetch current price
                    pricing_result = _swarm_execute_tool(
                        "oanda_data", "get_current_pricing",
                        instruments=[inst],
                    )
                    pricing = pricing_result.get("tool_result", pricing_result)
                    inst_pricing = pricing.get(inst, {}) if isinstance(pricing, dict) else {}
                    bid = inst_pricing.get("bid", 0)
                    ask = inst_pricing.get("ask", 0)
                    mid = (bid + ask) / 2 if bid and ask else 0
                    current_prices[inst] = mid

                    instruments_checked += 1
                except Exception as exc:
                    logger.error(
                        "Position update data fetch failed for %s: %s", inst, exc,
                    )

            # ── EMA Market Narrative for open positions ──────────────
            # Compute EMA state per instrument so position monitor knows
            # whether to hold, tighten, or exit based on fan state.
            ema_context_by_inst: Dict[str, Dict] = {}
            try:
                try:
                    from backtester.ema_separation import scan_ema_signals
                except ImportError:
                    try:
                        from Source.backtester.ema_separation import scan_ema_signals
                    except ImportError:
                        from ..backtester.ema_separation import scan_ema_signals

                for inst in inst_trades:
                    # Fetch M15 candles for real-time EMA tracking
                    # (position monitor needs faster reads than H1)
                    try:
                        m15_result = _swarm_execute_tool(
                            "oanda_data", "fetch_candles",
                            instrument=inst, timeframe="M15", count=200,
                        )
                        m15_data = m15_result.get("tool_result", m15_result)
                        m15_raw = m15_data.get("candles", []) if isinstance(m15_data, dict) else []
                    except Exception:
                        m15_raw = []

                    # Use M15 if we have enough bars, fall back to H1
                    raw_candles = m15_raw if len(m15_raw) >= 100 else candles_by_instrument.get(inst, [])
                    if not raw_candles:
                        continue
                    _norm = []
                    for c in raw_candles:
                        mid = c.get("mid", {})
                        _norm.append({
                            "time": c.get("time", ""),
                            "open": mid.get("o", c.get("open", 0)),
                            "high": mid.get("h", c.get("high", 0)),
                            "low": mid.get("l", c.get("low", 0)),
                            "close": mid.get("c", c.get("close", 0)),
                        })
                    if len(_norm) >= 100:
                        ema_ctx = scan_ema_signals(_norm)
                        ema_context_by_inst[inst] = ema_ctx

                        # ── EMA-driven position actions ──────────────
                        fan_state = ema_ctx.get('fan_state', 'unknown')
                        reversal_risk = ema_ctx.get('reversal_risk', 'low')
                        vel_trend = ema_ctx.get('fan_velocity_trend', 'unknown')

                        for trade_id in inst_trades.get(inst, []):
                            ema_action = None
                            ema_reason = ''

                            if fan_state == 'peaked':
                                ema_action = 'CLOSE'
                                ema_reason = (
                                    f"EMA fan PEAKED — separation at maximum ({ema_ctx.get('separation_pct', 0):.3f}%). "
                                    f"Move is exhausting. Close position."
                                )
                            elif fan_state == 'contracting':
                                ema_action = 'CLOSE'
                                ema_reason = (
                                    f"EMA fan CONTRACTING — separation shrinking. "
                                    f"Trend reversing. Close position."
                                )
                            elif fan_state == 'decelerating':
                                ema_action = 'TIGHTEN'
                                ema_reason = (
                                    f"EMA fan DECELERATING — momentum fading "
                                    f"(velocity trend: {vel_trend}). Tighten stop to breakeven."
                                )
                            elif reversal_risk == 'high' and fan_state != 'expanding':
                                ema_action = 'TIGHTEN'
                                ema_reason = (
                                    f"EMA reversal risk HIGH (fan {fan_state}, "
                                    f"velocity {vel_trend}). Tighten stop."
                                )

                            if ema_action:
                                all_actions.append({
                                    'instrument': inst,
                                    'trade_id': trade_id,
                                    'action': ema_action,
                                    'source': 'ema_narrative',
                                    'reason': ema_reason,
                                    'fan_state': fan_state,
                                    'reversal_risk': reversal_risk,
                                    'trend_health': ema_ctx.get('trend_health', 0),
                                    'separation_pct': ema_ctx.get('separation_pct', 0),
                                    'narrative': ema_ctx.get('narrative', ''),
                                })
                                logger.info(
                                    "[EMA POSITION MONITOR] %s trade %s: %s — %s",
                                    inst, trade_id, ema_action, ema_reason,
                                )
            except Exception as ema_exc:
                logger.warning("EMA position monitor failed: %s", ema_exc)

            # Run standard position monitor via execution agent
            if candles_by_instrument and current_prices:
                update_result = _swarm_execute_tool(
                    "execution", "update_monitored_positions",
                    candles_by_instrument=candles_by_instrument,
                    current_prices=current_prices,
                )
                update_data = update_result.get("tool_result", update_result)
                all_actions.extend(
                    update_data.get("actions_taken", []) if isinstance(update_data, dict) else []
                )

        except Exception as exc:
            logger.error("run_position_update failed: %s", exc)

        return {
            "instruments_checked": instruments_checked,
            "actions_taken": all_actions,
        }

    # ------------------------------------------------------------------
    # Operator command routing
    # ------------------------------------------------------------------

    def handle_operator_command(self, command: str, params: dict = None) -> dict:
        """Route Tim's commands/messages to the cycle_orchestrator agent.

        Simple commands (pause/resume/status) are fast-pathed.
        Free-form messages (market perspective, questions) go to the
        orchestrator LLM which can relay to the validator or other agents.
        """
        if params is None:
            params = {}

        # Fast-path simple commands
        cmd_lower = command.strip().lower()
        if cmd_lower in ("pause", "pause_trading"):
            global _trading_paused
            _trading_paused = True
            return {"command": "pause_trading", "result": "Trading paused", "success": True}
        if cmd_lower in ("resume", "resume_trading"):
            _trading_paused = False
            return {"command": "resume_trading", "result": "Trading resumed", "success": True}

        # Everything else → orchestrator LLM (can call validator, TA, etc.)
        task = (
            f"OPERATOR MESSAGE FROM TIM (the human trader):\n"
            f"{command}\n\n"
            f"Additional params: {json.dumps(params) if params else 'none'}\n\n"
            f"You are the cycle orchestrator. Tim is sharing his market perspective or asking a question.\n"
            f"If Tim is describing a market setup or thesis, use your tools to:\n"
            f"1. Pull current data for the pair(s) mentioned (get_candles, get_pricing)\n"
            f"2. Run TA analysis if needed\n"
            f"3. Ask the validator to evaluate Tim's thesis against current charts\n"
            f"4. Report back what the team sees — agree, disagree, or add nuance\n\n"
            f"Be direct and conversational. Tim is your boss and an experienced trader."
        )
        try:
            result = _agent_task(
                "cycle_orchestrator", task,
                context={"operator_command": True, "message": command},
                max_tokens=4096, timeout=90.0,
            )
            response_text = result.get("response", "")
            return {
                "command": command,
                "result": response_text,
                "success": True,
                "agent": "cycle_orchestrator",
                "tool_calls": result.get("tool_calls", []),
            }
        except Exception as exc:
            logger.error("Operator command via LLM failed: %s", exc, exc_info=True)
            # Fallback to tool call
            result = _swarm_execute_tool(
                "cycle_orchestrator", "process_operator_command",
                command=command, params=params,
            )
            return result.get("tool_result", result)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_cycle_status(self) -> dict:
        """Return current cycle engine status.

        Returns
        -------
        dict
            cycles_run, last_cycle_time, trading_active, open_positions,
            risk_status.
        """
        risk_status = {}
        open_positions = 0
        try:
            risk_result = _swarm_execute_tool(
                "cycle_orchestrator", "get_risk_status",
            )
            risk_status = risk_result.get("tool_result", risk_result)
            if isinstance(risk_status, dict):
                open_positions = risk_status.get("open_positions", 0)
        except Exception:
            pass

        return {
            "cycles_run": self._cycle_count,
            "last_cycle_time": self._last_cycle_time,
            "trading_active": not _trading_paused,
            "open_positions": open_positions,
            "risk_status": risk_status,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post_result(
        self,
        task_id: Optional[int],
        agent_name: str,
        message_type: str,
        summary: str,
        details: dict,
    ) -> None:
        """Post an agent result to the cycle task (best-effort)."""
        if task_id is None:
            return
        try:
            self._protocol.post_agent_result(
                task_id=task_id,
                agent_name=agent_name,
                message_type=message_type,
                content_summary=summary,
                technical_details=details,
            )
        except Exception as exc:
            logger.warning(
                "Failed to post %s result for %s: %s",
                message_type, agent_name, exc,
            )

    def _post_error(
        self,
        task_id: Optional[int],
        step_name: str,
        error_msg: str,
    ) -> None:
        """Post an error message to the cycle task (best-effort)."""
        if task_id is None:
            return
        # Strip HTML from error messages (e.g. OANDA 502 Cloudflare pages) before
        # they reach the Trading Floor where they render as white-screen HTML blobs.
        import re as _re_err
        _clean_msg = error_msg
        if '<' in error_msg and '>' in error_msg:
            # Contains HTML tags — extract just the status line, drop the body
            _clean_msg = _re_err.sub(r'<[^>]+>', '', error_msg)[:200].strip()
            _clean_msg = ' '.join(_clean_msg.split())  # collapse whitespace
            if not _clean_msg:
                _clean_msg = "API error (HTML response — likely 502 Cloudflare)"
        try:
            self._protocol.post_agent_result(
                task_id=task_id,
                agent_name="trading_cycle",
                message_type=MessageType.ERROR,
                content_summary=f"ERROR in {step_name}: {_clean_msg}",
                technical_details={"step": step_name, "error": _clean_msg},
            )
        except Exception as exc:
            logger.warning("Failed to post error for %s: %s", step_name, exc)
