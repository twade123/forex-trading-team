"""Process-wide Kronos lifecycle. One model, shared by Hunter + Filter."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("trading_bot.kronos_runtime")

_lock = threading.Lock()
_inference = None
_filter = None
_hunter = None
_cached_user_id: Optional[int] = None


def _get_trading_user_id() -> int:
    """Resolve the active trading user_id dynamically.

    Priority:
      1. TRADING_USER_ID environment variable (set by serve_ui.py at boot)
      2. Admin user lookup in core.db (first user with is_admin=1)
      3. Raises if neither resolves

    Cached after first success to avoid repeated DB hits.
    """
    global _cached_user_id
    if _cached_user_id is not None:
        return _cached_user_id

    _env = os.environ.get("TRADING_USER_ID")
    if _env:
        _cached_user_id = int(_env)
        return _cached_user_id

    _core_db = str(Path(__file__).resolve().parent.parent.parent
                   / "Database" / "v2" / "core.db")
    if Path(_core_db).exists():
        try:
            with sqlite3.connect(_core_db, timeout=3) as _c:
                _row = _c.execute(
                    "SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
                ).fetchone()
                if _row:
                    _cached_user_id = int(_row[0])
                    os.environ["TRADING_USER_ID"] = str(_cached_user_id)  # cache for siblings
                    return _cached_user_id
        except Exception as exc:
            logger.warning("kronos: core.db user lookup failed: %s", exc)

    raise RuntimeError(
        "kronos: cannot resolve trading user_id — set TRADING_USER_ID env "
        "or ensure core.db has an admin user"
    )


def _build_inference():
    from kronos_inference import KronosInferenceService
    return KronosInferenceService()


def _load_candles_via_oanda(pair: str, count: int = 256):
    """Fetch last `count` M15 bars from OANDA and return as DataFrame.

    Returns DataFrame with columns: time (UTC tz-aware), open, high, low, close, volume.
    Used by both Hunter and Filter (different `count`).

    2026-05-01: candles fetched through process-wide cache (5-min TTL).
    """
    import pandas as pd
    from oanda_client import OandaClient
    try:
        from candle_cache import get_cached_candles as _gcc_kr
    except ImportError:
        from Source.candle_cache import get_cached_candles as _gcc_kr
    client = OandaClient()
    def _fetch_kr(_pair=pair, _count=count, _cl=client):
        return _cl.get_candles(instrument=_pair, granularity="M15", count=_count, price="M")
    raw = _gcc_kr(_fetch_kr, pair, "M15", count)
    rows = []
    for c in raw:
        if not c.get("complete", True):
            continue  # skip incomplete current bar
        m = c.get("mid") or {}
        if not m:
            continue
        rows.append({
            "time": pd.to_datetime(c["time"], utc=True),
            "open": float(m["o"]),
            "high": float(m["h"]),
            "low": float(m["l"]),
            "close": float(m["c"]),
            "volume": int(c.get("volume", 0)),
        })
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    return df


def _load_candles_for_filter(pair: str):
    """Load last 256 M15 bars for a single pair via OANDA (filter-sized).
    Filter is latency-tolerant so a fresh fetch is fine."""
    return _load_candles_via_oanda(pair, count=256)


def _build_filter(inference):
    from tuning_config import TUNING
    from kronos_filter import KronosFilter
    from kronos_signals_db import KronosSignalsDB

    signals_db = KronosSignalsDB()

    def params_fn():
        # Resolve via tc_get_for_trade so dashboard tuning takes effect live.
        _src = "kronos_hunter"
        return {
            "filter_min_confidence_to_reject": tc_get_for_trade("filter_min_confidence_to_reject", _src),
            "pred_len_bars": tc_get_for_trade("pred_len_bars", _src),
            "sample_count": tc_get_for_trade("sample_count", _src),
        }

    return KronosFilter(
        inference=inference,
        signals_db=signals_db,
        candle_loader=_load_candles_for_filter,
        params_fn=params_fn,
    )


def get_kronos_filter() -> Optional[object]:
    """Returns the process-wide KronosFilter, or None when disabled/unavailable."""
    global _inference, _filter
    from tuning_config import TUNING
    if not TUNING["kronos.enabled"]["value"] or not TUNING["kronos.filter_enabled"]["value"]:
        return None
    with _lock:
        if _inference is None:
            _inference = _build_inference()
        if not _inference.is_ready():
            return None
        if _filter is None:
            _filter = _build_filter(_inference)
    return _filter


def get_kronos_hunter() -> Optional[object]:
    """Build/return the process-wide KronosHunter. Wires live collaborators."""
    global _inference, _hunter
    from tuning_config import TUNING, tc_get_for_trade
    if not TUNING["kronos.enabled"]["value"] or not TUNING["kronos.hunter_enabled"]["value"]:
        return None
    with _lock:
        if _inference is None:
            _inference = _build_inference()
        if not _inference.is_ready():
            return None
        if _hunter is None:
            from kronos_hunter import KronosHunter
            from kronos_signals_db import KronosSignalsDB
            from db_pool import get_trading_forex

            # Live order placement: size via PositionSizer @ 1% risk, fire
            # OANDA market order with structural SL/TP, INSERT a minimal
            # live_trades row with source='kronos_hunter' so the guardian's
            # _reconcile picks it up and routes via tc_get_for_trade().
            def _place(*, pair, direction, entry_price, sl_price, tp_price, source, **kwargs):
                import sqlite3
                import uuid
                from datetime import datetime, timezone
                from oanda_client import OandaClient
                client = OandaClient()
                pip = 0.01 if "JPY" in pair else 0.0001

                # 1) Position size — match snipe_direct's fixed-units path.
                # User's risk_fixed_units pref ($10/pip = 100,000 units) overrides
                # the risk_config.json default (10,000 = $1/pip).
                _nominal_units = 10000  # last-resort fallback
                try:
                    import json as _jmod
                    import os as _os
                    _rcfg_path = _os.path.join(
                        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                        "Config", "risk_config.json"
                    )
                    with open(_rcfg_path) as _rcf:
                        _rcfg = _jmod.load(_rcf)
                    _nominal_units = int(_rcfg.get("position_sizing", {}).get("fixed_units", 10000))
                except Exception as exc:
                    logger.warning("kronos: risk_config.json read failed (%s) — using %d", exc, _nominal_units)

                # User DB override (trading_preferences.risk_fixed_units)
                try:
                    _uid = _get_trading_user_id()
                    _core_db = str(Path(__file__).resolve().parent.parent.parent
                                   / "Database" / "v2" / "core.db")
                    with sqlite3.connect(_core_db, timeout=5) as _pc:
                        _row = _pc.execute(
                            "SELECT pref_value FROM trading_preferences "
                            "WHERE user_id=? AND pref_key='risk_fixed_units'",
                            (_uid,),
                        ).fetchone()
                        if _row:
                            _nominal_units = int(float(_row[0]))
                except Exception as exc:
                    logger.warning("kronos: trading_preferences lookup failed (%s) — using config default %d",
                                   exc, _nominal_units)

                # Fixed mode: literal units, signed by direction
                units = _nominal_units if direction.lower() == "buy" else -_nominal_units
                logger.info("kronos: %s %s → %d units (fixed-mode, snipe-equivalent)",
                            pair, direction, units)

                # 3) OANDA market order
                price_str = lambda p: f"{p:.3f}" if "JPY" in pair else f"{p:.5f}"
                fill = client.place_market_order(
                    instrument=pair,
                    units=units,
                    stop_loss=price_str(sl_price),
                    take_profit=price_str(tp_price),
                    client_extensions={
                        "id": f"kronos_{uuid.uuid4().hex[:10]}",
                        "tag": "kronos_hunter",
                        "comment": f"kronos hunter {direction}",
                    },
                )
                # OANDA returns one of:
                #   orderFillTransaction     — order filled, new position opened
                #   orderCancelTransaction   — order rejected (INSUFFICIENT_MARGIN,
                #                              MARKET_HALTED, INVALID_PRICE, ...)
                # If cancelled, surface the actual reason — otherwise the caller
                # gets a useless "missing tradeID" log. Map margin rejects to a
                # distinct exception type so kronos_signals.action_taken can
                # record `hunter_trade_rejected_margin` rather than generic fail.
                cancelTx = (fill or {}).get("orderCancelTransaction")
                if cancelTx:
                    cancel_reason = cancelTx.get("reason", "UNKNOWN")
                    raise RuntimeError(
                        f"kronos: OANDA rejected {pair} {direction} "
                        f"units={units} reason={cancel_reason}"
                    )

                fillTx = (fill or {}).get("orderFillTransaction") or {}
                trade_opened = fillTx.get("tradeOpened") or {}
                oanda_trade_id = trade_opened.get("tradeID")
                fill_price = float(fillTx.get("price") or entry_price)
                if not oanda_trade_id:
                    # No cancel, no fill — check for tradeReduced (opposite-direction
                    # nettout that closed an existing position instead of opening).
                    trade_reduced = fillTx.get("tradeReduced")
                    trades_closed = fillTx.get("tradesClosed")
                    if trade_reduced or trades_closed:
                        raise RuntimeError(
                            f"kronos: order netted against existing position on {pair} "
                            f"(reduced={bool(trade_reduced)} closed={bool(trades_closed)}) — "
                            f"dedup gate should have caught this"
                        )
                    raise RuntimeError(f"kronos: OANDA fill missing tradeID for {pair}: {fill}")

                # 4) INSERT live_trades row — user_id resolved dynamically.
                # CRITICAL: use oanda_trade_id AS the live_trades.id so the
                # guardian's reconcile (which only has the OANDA id) can find
                # our row via `WHERE id = ?` and pick up source='kronos_hunter'
                # → routes TUNING through kronos.* namespace. Matches the
                # convention used by snipe_direct.
                trade_id = str(oanda_trade_id)
                now = datetime.now(timezone.utc).isoformat()
                _trade_uid = _get_trading_user_id()
                # 2026-04-16: capture setup context for learning feedback loop
                _fan_state = kwargs.get('fan_state', '')
                _fan_dir = kwargs.get('fan_direction', '')
                _story_sc = kwargs.get('story_score', 0)
                _setup = kwargs.get('setup', 'kronos_unknown')
                _bb_width = kwargs.get('bb_width')
                _rsi = kwargs.get('rsi')
                _stoch_k = kwargs.get('stoch_k')
                _adx = kwargs.get('adx')
                _atr = kwargs.get('atr')
                conn = get_trading_forex()
                conn.execute("""
                    INSERT INTO live_trades (
                        id, pair, direction, entry_price, entry_time, status,
                        oanda_trade_id, source, entry_type, units,
                        sl_price, tp_price, user_id,
                        fan_state, fan_direction, story_score,
                        setup, base_setup,
                        bb_width, rsi, stoch_k, atr
                    ) VALUES (?,?,?,?,?, 'open', ?,?,?,?,?,?,?, ?,?,?, ?,?, ?,?,?,?)
                """, (
                    trade_id, pair, direction, fill_price, now,
                    oanda_trade_id, source, "kronos_hunter", abs(units),
                    sl_price, tp_price, _trade_uid,
                    _fan_state, _fan_dir, _story_sc,
                    _setup, _setup,
                    _bb_width, _rsi, _stoch_k, _atr,
                ))
                conn.commit()
                return {"trade_id": trade_id, "oanda_trade_id": oanda_trade_id,
                        "fill_price": fill_price, "units": units}

            signals_db = KronosSignalsDB()

            def candle_loader(pair: str):
                return _load_candles_via_oanda(
                    pair, count=TUNING["kronos.lookback_bars"]["value"]
                )

            def open_trade_checker(pair: str) -> bool:
                """Only block Kronos if another KRONOS trade is open on this pair.
                Scout/snipe/manual trades on the same pair do NOT block Kronos."""
                conn = get_trading_forex()
                row = conn.execute(
                    "SELECT COUNT(*) FROM live_trades "
                    "WHERE pair=? AND status='open' AND source='kronos_hunter'", (pair,)
                ).fetchone()
                return bool(row[0])

            def concurrent_counter() -> int:
                conn = get_trading_forex()
                row = conn.execute(
                    "SELECT COUNT(*) FROM live_trades "
                    "WHERE status='open' AND source='kronos_hunter'"
                ).fetchone()
                return int(row[0] or 0)

            def daily_pnl_fn() -> float:
                from datetime import datetime, timezone
                today = datetime.now(timezone.utc).date().isoformat()
                conn = get_trading_forex()
                row = conn.execute(
                    "SELECT COALESCE(SUM(pnl_pips), 0) FROM live_trades "
                    "WHERE source='kronos_hunter' "
                    "AND DATE(entry_time) = ? AND status='closed'",
                    (today,),
                ).fetchone()
                return float(row[0] or 0.0)

            def params_fn():
                # Resolve every tunable via tc_get_for_trade so dashboard
                # overrides in tuning_overrides table take effect on the NEXT
                # scan without a process restart. Previously several params
                # read TUNING dict directly, bypassing the override layer.
                _src = "kronos_hunter"
                return {
                    "hunter_min_drift_pips": tc_get_for_trade("hunter_min_drift_pips", _src),
                    "hunter_min_drift_atr_frac": tc_get_for_trade("hunter_min_drift_atr_frac", _src),
                    "hunter_loss_cooldown_count": tc_get_for_trade("hunter_loss_cooldown_count", _src),
                    "hunter_loss_cooldown_hours": tc_get_for_trade("hunter_loss_cooldown_hours", _src),
                    "hunter_max_concurrent_trades": tc_get_for_trade("hunter_max_concurrent_trades", _src),
                    "hunter_daily_kill_switch_pips": tc_get_for_trade("hunter_daily_kill_switch_pips", _src),
                    "lookback_bars": tc_get_for_trade("lookback_bars", _src),
                    "pred_len_bars": tc_get_for_trade("pred_len_bars", _src),
                    "sample_count": tc_get_for_trade("sample_count", _src),
                    "sl_atr_mult": tc_get_for_trade("gate.sl_atr_mult", _src),
                    "tp_atr_mult": tc_get_for_trade("gate.tp_atr_mult", _src),
                    # Forecast-bounded SL/TP bounds (tunable, added 2026-04-21)
                    "gate.atr_sl_min_mult": tc_get_for_trade("gate.atr_sl_min_mult", _src),
                    "gate.atr_sl_max_mult": tc_get_for_trade("gate.atr_sl_max_mult", _src),
                    "gate.atr_tp_min_mult": tc_get_for_trade("gate.atr_tp_min_mult", _src),
                    "gate.atr_tp_max_mult": tc_get_for_trade("gate.atr_tp_max_mult", _src),
                    # Regime gate params (new 2026-04-15) — chop entry filter
                    "hunter_require_fan_aligned": tc_get_for_trade("hunter_require_fan_aligned", _src),
                    "hunter_min_fan_sep_atr": tc_get_for_trade("hunter_min_fan_sep_atr", _src),
                    "hunter_min_e21_slope_pips": tc_get_for_trade("hunter_min_e21_slope_pips", _src),
                    # Scout bias gate (2026-04-20) — toggle via dashboard
                    "hunter_scout_bias_gate": tc_get_for_trade("hunter_scout_bias_gate", _src),
                    # Session gate + bleed-hour blackout (2026-04-22)
                    "hunter_session_gate_enabled": tc_get_for_trade("hunter_session_gate_enabled", _src),
                    "hunter_sunday_block_start_utc": tc_get_for_trade("hunter_sunday_block_start_utc", _src),
                    "hunter_sunday_block_end_utc": tc_get_for_trade("hunter_sunday_block_end_utc", _src),
                    "hunter_friday_block_start_utc": tc_get_for_trade("hunter_friday_block_start_utc", _src),
                    "hunter_session_bleed_hours_utc": tc_get_for_trade("hunter_session_bleed_hours_utc", _src),
                    # Counter-momentum pre-entry gate (2026-04-22)
                    "hunter_counter_momentum_enabled": tc_get_for_trade("hunter_counter_momentum_enabled", _src),
                    "hunter_counter_momentum_min_score": tc_get_for_trade("hunter_counter_momentum_min_score", _src),
                    # Confidence band + drift/ATR cap (2026-04-24, A2 + A3)
                    "hunter_min_signal_confidence": tc_get_for_trade("hunter_min_signal_confidence", _src),
                    "hunter_max_signal_confidence": tc_get_for_trade("hunter_max_signal_confidence", _src),
                    "hunter_max_drift_atr_ratio": tc_get_for_trade("hunter_max_drift_atr_ratio", _src),
                    # Path-plan direction override toggle (2026-04-24)
                    "hunter_path_direction_override_enabled": tc_get_for_trade(
                        "hunter_path_direction_override_enabled", _src),
                    # 4-rule filter enable + tunable thresholds (2026-04-24)
                    "hunter_4rule_filter_enabled": tc_get_for_trade("hunter_4rule_filter_enabled", _src),
                    "hunter_knife_buy_stoch_max": tc_get_for_trade("hunter_knife_buy_stoch_max", _src),
                    "hunter_knife_sell_stoch_min": tc_get_for_trade("hunter_knife_sell_stoch_min", _src),
                    "hunter_candle_fighting_body_pct_min": tc_get_for_trade(
                        "hunter_candle_fighting_body_pct_min", _src),
                    "hunter_ultra_extended_atr_mult": tc_get_for_trade("hunter_ultra_extended_atr_mult", _src),
                    "hunter_ambiguous_body_pct_max": tc_get_for_trade("hunter_ambiguous_body_pct_max", _src),
                }

            def loss_counter(pair: str, hours: float) -> int:
                """Count CLOSED kronos_hunter losses on `pair` within last `hours`."""
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).isoformat()
                conn = get_trading_forex()
                row = conn.execute(
                    "SELECT COUNT(*) FROM live_trades "
                    "WHERE source='kronos_hunter' AND pair=? "
                    "AND status='closed' AND outcome='loss' "
                    "AND COALESCE(exit_time, entry_time) >= ?",
                    (pair, cutoff),
                ).fetchone()
                return int(row[0] or 0)

            pairs = [
                "AUD_JPY", "AUD_USD", "EUR_AUD", "EUR_CHF", "EUR_GBP",
                "EUR_JPY", "EUR_USD", "GBP_JPY", "GBP_USD", "NZD_USD",
                "USD_CAD", "USD_CHF", "USD_JPY",
            ]

            _hunter = KronosHunter(
                inference=_inference,
                signals_db=signals_db,
                candle_loader=candle_loader,
                open_trade_checker=open_trade_checker,
                concurrent_counter=concurrent_counter,
                daily_pnl_fn=daily_pnl_fn,
                order_placer=_place,
                pairs=pairs,
                params_fn=params_fn,
                shadow_mode_fn=lambda: tc_get_for_trade("shadow_mode", "kronos_hunter"),
                loss_counter=loss_counter,
            )
    return _hunter


def shutdown() -> None:
    """Reset singletons (mainly for tests)."""
    global _inference, _filter, _hunter
    with _lock:
        _inference = None
        _filter = None
        _hunter = None
