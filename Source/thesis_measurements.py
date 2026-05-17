"""
Single source of truth for chart-structural thesis measurements.

Computes fan/BB deltas, cross detection, RSI/stoch state, divergences,
retracement classification, and the 10-point checklist from a single
indicator-loaded DataFrame. Scout, trading_cycle, and full_confluence_scorer
all call this so they see the same numbers for the same chart.

Pure compute — no logger calls, no flight.record. Any field whose underlying
data is missing returns None (not 0.0), so callers can distinguish "no data"
from a real-zero reading via ``is not None``.
"""

from typing import Optional, Dict, Any
import pandas as pd


def compute_thesis_measurements(
    df: pd.DataFrame,
    pip_size: float,
    fan_state: str = "unknown",
    fan_direction: str = "neutral",
) -> Dict[str, Any]:
    """
    Compute all chart-structural thesis measurements from M15 candle data.

    Args:
        df: DataFrame indexed by bar (oldest → newest). Required columns:
            ``ema_21``, ``ema_55``, ``ema_100``, ``open``, ``high``, ``low``,
            ``close``, ``bb_upper``, ``bb_lower``. Optional columns:
            ``rsi`` or ``RSI``, ``stoch_k``, ``stoch_d``,
            ``rsi_bull_div``, ``rsi_bear_div``,
            ``rsi_hidden_bull_div``, ``rsi_hidden_bear_div``.
        pip_size: 0.01 for JPY pairs, 0.0001 otherwise.
        fan_state: pre-computed fan kinetic state from ``ema_signal``
            (``expanding`` / ``contracting`` / ``stable`` / ``just_crossed`` etc).
        fan_direction: pre-computed fan ordering from ``ema_signal``
            (``bullish`` / ``bearish`` / ``neutral``).

    Returns:
        dict with keys (all may be None when underlying data unavailable):
          fan_delta_5bar, fan_delta_20bar, fan_expanding, fan_accelerating,
          fan_width_now, bb_delta_5bar, bb_delta_20bar, bb_expanding,
          bb_width_now, candles_moving_away, e100_dist_pips,
          separation_accelerating, e100_dist_history,
          recent_cross, cross_bars_ago, cross1_direction,
          cross2_detected, cross2_bars_ago, cross2_direction,
          dual_cross_cascade, cascade_direction,
          e55_dist_pips,
          rsi_now, rsi_recovery_ok, rsi_was_extreme, rsi_extreme_val, rsi_healthy,
          stoch_k_now, stoch_d_now, stoch_bull_cross, stoch_bear_cross,
          rsi_bull_divergence, rsi_bear_divergence,
          momentum_candles, candles_correct_side,
          reversal_candle_at_ema, reversal_candle_ema_level,
          reversal_candle_direction,
          is_retracement, is_retracement_forming, retracement_type,
          was_expanding_recently, peak_fan_width, candles_holding,
          bb_re_expanding, tested_e55, tested_e100,
          fan_flip_detected, fan_flip_direction,
          checklist, checklist_score
    """
    # Initialize all outputs to None (unavailable). Compute blocks below
    # overwrite as data becomes available; failure paths leave None in place.
    out: Dict[str, Any] = {
        "fan_delta_5bar": None, "fan_delta_20bar": None,
        "fan_expanding": None, "fan_accelerating": None, "fan_width_now": None,
        "bb_delta_5bar": None, "bb_delta_20bar": None,
        "bb_expanding": None, "bb_width_now": None,
        "candles_moving_away": None, "e100_dist_pips": None,
        "separation_accelerating": None, "e100_dist_history": None,
        "recent_cross": None, "cross_bars_ago": None, "cross1_direction": None,
        "cross2_detected": None, "cross2_bars_ago": None, "cross2_direction": None,
        "dual_cross_cascade": None, "cascade_direction": None,
        "e55_dist_pips": None,
        "rsi_now": None, "rsi_recovery_ok": None, "rsi_was_extreme": None,
        "rsi_extreme_val": None, "rsi_healthy": None,
        "stoch_k_now": None, "stoch_d_now": None,
        "stoch_bull_cross": None, "stoch_bear_cross": None,
        "rsi_bull_divergence": None, "rsi_bear_divergence": None,
        "momentum_candles": None, "candles_correct_side": None,
        "reversal_candle_at_ema": None, "reversal_candle_ema_level": None,
        "reversal_candle_direction": None,
        "is_retracement": None, "is_retracement_forming": None,
        "retracement_type": None, "was_expanding_recently": None,
        "peak_fan_width": None, "candles_holding": None,
        "bb_re_expanding": None, "tested_e55": None, "tested_e100": None,
        "fan_flip_detected": None, "fan_flip_direction": None,
        "checklist": None, "checklist_score": None,
    }

    if df is None or len(df) == 0:
        return out

    latest_row = df.iloc[-1]

    # ──────────────────────────────────────────────────────────────────────
    # BB expansion — dual window (5-bar fast + 20-bar context)
    # NOTE: DataFrame does NOT have a 'bb_width' column — compute from bb_upper - bb_lower
    # Source: trade_scout.py:1335-1351
    # ──────────────────────────────────────────────────────────────────────
    _bb_delta_5bar = 0.0
    _bb_delta_20bar = 0.0
    _bb_expanding = False
    _bb_width_now = 0.0
    _bb_5_ok = False
    _bb_20_ok = False
    try:
        _has_bb_cols = "bb_upper" in df.columns and "bb_lower" in df.columns
        if _has_bb_cols and len(df) >= 6:
            _bb_width_now = float(df.iloc[-1]["bb_upper"]) - float(df.iloc[-1]["bb_lower"])
            _bb_width_5ago = float(df.iloc[-6]["bb_upper"]) - float(df.iloc[-6]["bb_lower"])
            _bb_delta_5bar = _bb_width_now - _bb_width_5ago
            _bb_5_ok = True
        if _has_bb_cols and len(df) >= 21:
            _bb_width_20ago = float(df.iloc[-21]["bb_upper"]) - float(df.iloc[-21]["bb_lower"])
            _bb_delta_20bar = _bb_width_now - _bb_width_20ago
            _bb_20_ok = True
        # Expanding if either fast signal is positive, or context shows clear expansion
        _bb_expanding = (_bb_delta_5bar > 0.0004) or (
            _bb_delta_20bar > 0.0008 and _bb_delta_5bar > -0.0002
        )
    except (ValueError, TypeError, IndexError, KeyError):
        pass
    if _bb_5_ok:
        out["bb_delta_5bar"] = _bb_delta_5bar
        out["bb_width_now"] = _bb_width_now
        out["bb_expanding"] = _bb_expanding
    if _bb_20_ok:
        out["bb_delta_20bar"] = _bb_delta_20bar

    # ──────────────────────────────────────────────────────────────────────
    # Fan width and expansion
    # Source: trade_scout.py:1353-1403
    # ──────────────────────────────────────────────────────────────────────
    _fan_width_now = 0.0
    _fan_delta_5bar = 0.0
    _fan_delta_20bar = 0.0
    _fan_expanding = False
    _fan_accelerating = False
    _fan_5_ok = False
    _fan_20_ok = False
    _fan_acc_ok = False
    _price_now = 0.0
    try:
        if len(df) >= 6:
            _e21_now = float(df.iloc[-1].get("ema_21", 0))
            _e55_now = float(df.iloc[-1].get("ema_55", 0))
            _e100_now = float(df.iloc[-1].get("ema_100", 0))
            _price_now = float(df.iloc[-1].get("close", 0))
            _e21_5ago = float(df.iloc[-6].get("ema_21", 0))
            _e100_5ago = float(df.iloc[-6].get("ema_100", 0))
            _price_5ago = float(df.iloc[-6].get("close", 0))
            if _price_now > 0 and _price_5ago > 0:
                _fan_width_now = abs(_e21_now - _e100_now) / _price_now * 100
                _fan_width_5ago = abs(_e21_5ago - _e100_5ago) / _price_5ago * 100
                _fan_delta_5bar = _fan_width_now - _fan_width_5ago
                _fan_5_ok = True
        if len(df) >= 21 and _price_now > 0:
            _e21_20ago = float(df.iloc[-21].get("ema_21", 0))
            _e100_20ago = float(df.iloc[-21].get("ema_100", 0))
            _price_20ago = float(df.iloc[-21].get("close", _price_now))
            if _price_20ago > 0:
                _fan_width_20ago = abs(_e21_20ago - _e100_20ago) / _price_20ago * 100
                _fan_delta_20bar = _fan_width_now - _fan_width_20ago
                _fan_20_ok = True
        # Expanding only if the 5-bar delta is positive — fan must be growing RIGHT NOW.
        _fan_expanding = _fan_delta_5bar > 0

        # Fan accelerating: last 3 bars all growing, rate increasing
        if len(df) >= 4:
            _fan_deltas = []
            for _fi in range(-3, 0):
                _e21_i = float(df.iloc[_fi].get("ema_21", 0))
                _e100_i = float(df.iloc[_fi].get("ema_100", 0))
                _e21_p = float(df.iloc[_fi - 1].get("ema_21", 0))
                _e100_p = float(df.iloc[_fi - 1].get("ema_100", 0))
                _p_i = float(df.iloc[_fi].get("close", 1))
                _p_p = float(df.iloc[_fi - 1].get("close", 1))
                if _p_i > 0 and _p_p > 0:
                    _fw_i = abs(_e21_i - _e100_i) / _p_i
                    _fw_p = abs(_e21_p - _e100_p) / _p_p
                    _fan_deltas.append(_fw_i - _fw_p)
            _fan_accelerating = (
                len(_fan_deltas) >= 3 and all(_d > 0 for _d in _fan_deltas)
            )
            _fan_acc_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _fan_5_ok:
        out["fan_delta_5bar"] = _fan_delta_5bar
        out["fan_width_now"] = _fan_width_now
        out["fan_expanding"] = _fan_expanding
    if _fan_20_ok:
        out["fan_delta_20bar"] = _fan_delta_20bar
    if _fan_acc_ok:
        out["fan_accelerating"] = _fan_accelerating

    # ──────────────────────────────────────────────────────────────────────
    # Candles moving away from E100
    # Source: trade_scout.py:1405-1419
    # ──────────────────────────────────────────────────────────────────────
    _candles_moving_away = False
    _e100_dist_pips = 0.0
    _price_val = 0.0
    _moving_ok = False
    try:
        _price_val = float(latest_row.get("close", 0))
        _e100_val = float(df.iloc[-1].get("ema_100", 0)) if len(df) > 0 else 0
        _e100_dist_pips = abs(_price_val - _e100_val) / pip_size if _e100_val > 0 else 0
        if len(df) >= 2:
            _p_prev = float(df.iloc[-2].get("close", 0))
            _e100_prev = float(df.iloc[-2].get("ema_100", 0))
            if _e100_val > 0 and _e100_prev > 0:
                _candles_moving_away = abs(_price_val - _e100_val) > abs(_p_prev - _e100_prev)
                _moving_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _moving_ok:
        out["candles_moving_away"] = _candles_moving_away
    out["e100_dist_pips"] = _e100_dist_pips if _e100_dist_pips else None

    # ──────────────────────────────────────────────────────────────────────
    # 3-bar separation velocity: each of last 3 bars progressively farther from E100
    # Source: trade_scout.py:1421-1438
    # ──────────────────────────────────────────────────────────────────────
    _separation_accelerating = False
    _e100_dist_history = []
    _sep_ok = False
    try:
        if len(df) >= 4:
            for _svi in range(-4, 0):
                _sv_p = float(df.iloc[_svi].get("close", 0))
                _sv_e100 = float(df.iloc[_svi].get("ema_100", 0))
                if _sv_e100 > 0:
                    _e100_dist_history.append(abs(_sv_p - _sv_e100) / pip_size)
            if len(_e100_dist_history) >= 4:
                _separation_accelerating = (
                    _e100_dist_history[-1] > _e100_dist_history[-2] > _e100_dist_history[-3]
                    and _e100_dist_history[-1] >= 2.0  # at least 2 pips away
                )
                _sep_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _sep_ok:
        out["separation_accelerating"] = _separation_accelerating
        out["e100_dist_history"] = _e100_dist_history

    # ──────────────────────────────────────────────────────────────────────
    # DUAL-CROSS CASCADE DETECTION
    # Cross 1: E21 × E55 within last 30 bars
    # Source: trade_scout.py:1440-1467
    # ──────────────────────────────────────────────────────────────────────
    _recent_cross = False
    _cross_bars_ago: Optional[int] = None
    _cross1_direction: Optional[str] = None  # 'bullish' or 'bearish'
    _cross1_ok = False
    try:
        if len(df) >= 16:
            for _ci in range(max(len(df) - 30, 1), len(df)):
                _e21_c = float(df.iloc[_ci].get("ema_21", 0))
                _e55_c = float(df.iloc[_ci].get("ema_55", 0))
                _e21_p = float(df.iloc[_ci - 1].get("ema_21", 0))
                _e55_p = float(df.iloc[_ci - 1].get("ema_55", 0))
                if _e21_c > _e55_c and _e21_p <= _e55_p:
                    _recent_cross = True
                    _cross_bars_ago = len(df) - _ci
                    _cross1_direction = "bullish"
                    break
                elif _e21_c < _e55_c and _e21_p >= _e55_p:
                    _recent_cross = True
                    _cross_bars_ago = len(df) - _ci
                    _cross1_direction = "bearish"
                    break
            _cross1_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _cross1_ok:
        out["recent_cross"] = _recent_cross
        out["cross_bars_ago"] = _cross_bars_ago
        out["cross1_direction"] = _cross1_direction

    # Cross 2: E21 × E100 within last 30 bars
    # Source: trade_scout.py:1469-1491
    _cross2_detected = False
    _cross2_bars_ago: Optional[int] = None
    _cross2_direction: Optional[str] = None
    _cross2_ok = False
    try:
        if len(df) >= 16:
            for _ci in range(max(len(df) - 30, 1), len(df)):
                _e21_c = float(df.iloc[_ci].get("ema_21", 0))
                _e100_c = float(df.iloc[_ci].get("ema_100", 0))
                _e21_p = float(df.iloc[_ci - 1].get("ema_21", 0))
                _e100_p = float(df.iloc[_ci - 1].get("ema_100", 0))
                if _e21_c > _e100_c and _e21_p <= _e100_p:
                    _cross2_detected = True
                    _cross2_bars_ago = len(df) - _ci
                    _cross2_direction = "bullish"
                    break
                elif _e21_c < _e100_c and _e21_p >= _e100_p:
                    _cross2_detected = True
                    _cross2_bars_ago = len(df) - _ci
                    _cross2_direction = "bearish"
                    break
            _cross2_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _cross2_ok:
        out["cross2_detected"] = _cross2_detected
        out["cross2_bars_ago"] = _cross2_bars_ago
        out["cross2_direction"] = _cross2_direction

    # Dual-cross cascade: both crosses in same direction, cross1 before cross2
    # Source: trade_scout.py:1493-1502
    _dual_cross_cascade = False
    _cascade_direction: Optional[str] = None
    if (
        _recent_cross
        and _cross2_detected
        and _cross1_direction == _cross2_direction
        and _cross_bars_ago is not None
        and _cross2_bars_ago is not None
        and _cross_bars_ago >= _cross2_bars_ago  # cross1 happened before cross2
        and _cross2_bars_ago <= 5  # tightened: entry is candle 2-3 after cross2
    ):
        _dual_cross_cascade = True
        _cascade_direction = _cross2_direction
    if _cross1_ok and _cross2_ok:
        out["dual_cross_cascade"] = _dual_cross_cascade
        out["cascade_direction"] = _cascade_direction

    # ──────────────────────────────────────────────────────────────────────
    # E55 distance (for retracement type detection)
    # Source: trade_scout.py:1518-1525
    # ──────────────────────────────────────────────────────────────────────
    _e55_dist_pips = 0.0
    _e55_dist_ok = False
    try:
        _e55_val = float(df.iloc[-1].get("ema_55", 0))
        if _e55_val > 0:
            _e55_dist_pips = abs(_price_val - _e55_val) / pip_size
            _e55_dist_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _e55_dist_ok:
        out["e55_dist_pips"] = _e55_dist_pips

    # ──────────────────────────────────────────────────────────────────────
    # RSI state
    # Source: trade_scout.py:1527-1548
    # ──────────────────────────────────────────────────────────────────────
    _rsi_now = float(latest_row.get("rsi", latest_row.get("RSI", 50)))
    _rsi_recovery_ok = True
    _rsi_was_extreme = False
    _rsi_extreme_val: Optional[float] = None
    _rsi_healthy = 25 < _rsi_now < 75
    try:
        if len(df) >= 11:
            _rsi_window = [
                float(df.iloc[_ri].get("rsi", df.iloc[_ri].get("RSI", 50)))
                for _ri in range(max(len(df) - 11, 0), len(df))
            ]
            _rsi_min_10 = min(_rsi_window)
            _rsi_max_10 = max(_rsi_window)
            if _rsi_min_10 < 30:
                _rsi_was_extreme = True
                _rsi_extreme_val = _rsi_min_10
                _rsi_recovery_ok = _rsi_now > 25
            elif _rsi_max_10 > 70:
                _rsi_was_extreme = True
                _rsi_extreme_val = _rsi_max_10
                _rsi_recovery_ok = _rsi_now < 75
    except (ValueError, TypeError, IndexError):
        pass
    out["rsi_now"] = _rsi_now
    out["rsi_recovery_ok"] = _rsi_recovery_ok
    out["rsi_was_extreme"] = _rsi_was_extreme
    out["rsi_extreme_val"] = _rsi_extreme_val
    out["rsi_healthy"] = _rsi_healthy

    # ──────────────────────────────────────────────────────────────────────
    # STOCHASTIC CROSS DETECTION
    # Bull cross: %K crossing above %D from oversold
    # Bear cross: %K crossing below %D from overbought
    # Source: trade_scout.py:1550-1578
    # ──────────────────────────────────────────────────────────────────────
    _stoch_k_now = float(latest_row.get("stoch_k", 50))
    _stoch_d_now = float(latest_row.get("stoch_d", 50))
    _stoch_bull_cross = False
    _stoch_bear_cross = False
    try:
        if len(df) >= 2:
            _stoch_k_prev = float(df.iloc[-2].get("stoch_k", 50))
            _stoch_d_prev = float(df.iloc[-2].get("stoch_d", 50))
            # Bull cross: %K was below %D, now above, and in oversold zone (<35)
            _stoch_bull_cross = (
                _stoch_k_prev <= _stoch_d_prev
                and _stoch_k_now > _stoch_d_now
                and _stoch_k_now < 35
            )
            # Bear cross: %K was above %D, now below, and in overbought zone (>65)
            _stoch_bear_cross = (
                _stoch_k_prev >= _stoch_d_prev
                and _stoch_k_now < _stoch_d_now
                and _stoch_k_now > 65
            )
    except (ValueError, TypeError, IndexError):
        pass
    out["stoch_k_now"] = _stoch_k_now
    out["stoch_d_now"] = _stoch_d_now
    out["stoch_bull_cross"] = _stoch_bull_cross
    out["stoch_bear_cross"] = _stoch_bear_cross

    # ──────────────────────────────────────────────────────────────────────
    # RSI DIVERGENCE FOR RETRACEMENT
    # Source: trade_scout.py:1580-1588 — reads pre-computed flags from row
    # ──────────────────────────────────────────────────────────────────────
    _rsi_bull_divergence = bool(
        latest_row.get("rsi_bull_div", False) or latest_row.get("rsi_hidden_bull_div", False)
    )
    _rsi_bear_divergence = bool(
        latest_row.get("rsi_bear_div", False) or latest_row.get("rsi_hidden_bear_div", False)
    )
    out["rsi_bull_divergence"] = _rsi_bull_divergence
    out["rsi_bear_divergence"] = _rsi_bear_divergence

    # ──────────────────────────────────────────────────────────────────────
    # Momentum candles (last 3 bars: strong bodies in same direction)
    # Source: trade_scout.py:1590-1611
    # ──────────────────────────────────────────────────────────────────────
    _momentum_candles = False
    _momentum_ok = False
    try:
        if len(df) >= 3:
            _body_ratios = []
            _directions = []
            for _mi in range(-3, 0):
                _o = float(df.iloc[_mi]["open"])
                _c = float(df.iloc[_mi]["close"])
                _h = float(df.iloc[_mi]["high"])
                _l = float(df.iloc[_mi]["low"])
                _body = abs(_c - _o)
                _total = _h - _l if _h > _l else 0.0001
                _body_ratios.append(_body / _total)
                _directions.append("bull" if _c > _o else "bear")
            _avg_body = sum(_body_ratios) / len(_body_ratios)
            _momentum_candles = _avg_body > 0.6 and (
                all(_d == "bull" for _d in _directions)
                or all(_d == "bear" for _d in _directions)
            )
            _momentum_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _momentum_ok:
        out["momentum_candles"] = _momentum_candles

    # ──────────────────────────────────────────────────────────────────────
    # Candles correct side of all 3 EMAs
    # Source: trade_scout.py:1613-1622
    # ──────────────────────────────────────────────────────────────────────
    _candles_correct_side = False
    _correct_ok = False
    try:
        _p = float(latest_row.get("close", 0))
        _e21 = float(df.iloc[-1].get("ema_21", 0))
        _e55 = float(df.iloc[-1].get("ema_55", 0))
        _e100 = float(df.iloc[-1].get("ema_100", 0))
        _candles_correct_side = (_p > _e21 > _e55 > _e100) or (_p < _e21 < _e55 < _e100)
        _correct_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _correct_ok:
        out["candles_correct_side"] = _candles_correct_side

    # ──────────────────────────────────────────────────────────────────────
    # REVERSAL CANDLE AT EMA LEVEL
    # The re-entry signal is a reversal candle (hammer, pin bar, engulfing)
    # forming AT E55 or E100. Wick pokes through the EMA but body closes
    # back on the correct side.
    # Source: trade_scout.py:1624-1678
    # ──────────────────────────────────────────────────────────────────────
    _reversal_candle_at_ema = False
    _reversal_candle_ema_level: Optional[str] = None  # 'e55' or 'e100'
    _reversal_candle_direction: Optional[str] = None  # 'bullish' or 'bearish'
    _reversal_ok = False
    try:
        if len(df) >= 2:
            _rc_o = float(df.iloc[-1]["open"])
            _rc_c = float(df.iloc[-1]["close"])
            _rc_h = float(df.iloc[-1]["high"])
            _rc_l = float(df.iloc[-1]["low"])
            _rc_body = abs(_rc_c - _rc_o)
            _rc_total = _rc_h - _rc_l if _rc_h > _rc_l else 0.0001
            _rc_body_ratio = _rc_body / _rc_total
            _rc_e55 = float(df.iloc[-1].get("ema_55", 0))
            _rc_e100 = float(df.iloc[-1].get("ema_100", 0))

            _lower_wick = min(_rc_o, _rc_c) - _rc_l
            _upper_wick = _rc_h - max(_rc_o, _rc_c)
            _is_bull_reversal = (
                _rc_body_ratio < 0.45
                and _lower_wick > _rc_body * 1.5
                and _rc_c > _rc_o
            )
            _is_bear_reversal = (
                _rc_body_ratio < 0.45
                and _upper_wick > _rc_body * 1.5
                and _rc_c < _rc_o
            )

            for _ema_name, _ema_lvl in [("e55", _rc_e55), ("e100", _rc_e100)]:
                if _ema_lvl <= 0:
                    continue
                _dist_to_ema = abs(min(_rc_o, _rc_c) - _ema_lvl) / pip_size
                if _dist_to_ema < 5:
                    if _is_bull_reversal and _rc_l <= _ema_lvl + 2 * pip_size:
                        _reversal_candle_at_ema = True
                        _reversal_candle_ema_level = _ema_name
                        _reversal_candle_direction = "bullish"
                        break
                    elif _is_bear_reversal and _rc_h >= _ema_lvl - 2 * pip_size:
                        _reversal_candle_at_ema = True
                        _reversal_candle_ema_level = _ema_name
                        _reversal_candle_direction = "bearish"
                        break
            _reversal_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _reversal_ok:
        out["reversal_candle_at_ema"] = _reversal_candle_at_ema
        out["reversal_candle_ema_level"] = _reversal_candle_ema_level
        out["reversal_candle_direction"] = _reversal_candle_direction

    # ──────────────────────────────────────────────────────────────────────
    # RETRACEMENT DETECTION
    # PATH A (FORMING): Fan ordered, price at E55/E100, reversal candle / stoch /
    #                   divergence — entry BEFORE re-expansion (fishing line).
    # PATH B (CONFIRMED): Fan ordered, price tested E55/E100, BBs re-expanding
    #                     for 3+ bars — confirms move resumed.
    # Source: trade_scout.py:1680-1871 (logger.info calls excluded)
    # ──────────────────────────────────────────────────────────────────────
    _is_retracement = False
    _is_retracement_forming = False
    _retracement_type: Optional[str] = None
    _was_expanding_recently = False
    _candles_holding = False
    _peak_fan_width = 0.0
    _bb_re_expanding = False
    _tested_e55 = False
    _tested_e100 = False
    _retrace_ok = False
    try:
        if len(df) >= 20:
            _peak_fan = 0
            _peak_idx = -1
            for _pi in range(max(len(df) - 50, 0), len(df)):
                _e21_pi = float(df.iloc[_pi].get("ema_21", 0))
                _e100_pi = float(df.iloc[_pi].get("ema_100", 0))
                _p_pi = float(df.iloc[_pi].get("close", 1))
                if _p_pi > 0:
                    _fw_pi = abs(_e21_pi - _e100_pi) / _p_pi * 100
                    if _fw_pi > _peak_fan:
                        _peak_fan = _fw_pi
                        _peak_idx = _pi
            _peak_fan_width = _peak_fan
            _bars_since_peak = len(df) - _peak_idx if _peak_idx >= 0 else 999
            _was_expanding_recently = (
                _peak_fan > _fan_width_now * 1.3
                and 3 < _bars_since_peak < 40
                and _peak_fan > 0.02
            )

            if _was_expanding_recently and _peak_idx >= 0:
                _e21_pk = float(df.iloc[_peak_idx].get("ema_21", 0))
                _e100_pk = float(df.iloc[_peak_idx].get("ema_100", 0))
                _was_bullish = _e21_pk > _e100_pk

                _held_above_e100 = True
                _crossed_e100 = False
                _touched_e55 = False
                _bb_min_after_peak = 999.0
                for _hi in range(_peak_idx, len(df)):
                    _p_h = float(df.iloc[_hi].get("close", 0))
                    _e55_h = float(df.iloc[_hi].get("ema_55", 0))
                    _e100_h = float(df.iloc[_hi].get("ema_100", 0))
                    _bb_h = float(df.iloc[_hi].get("bb_upper", 0)) - float(
                        df.iloc[_hi].get("bb_lower", 0)
                    )

                    if _bb_h > 0 and _bb_h < _bb_min_after_peak:
                        _bb_min_after_peak = _bb_h

                    if _e55_h > 0:
                        _e55_dist_h = abs(_p_h - _e55_h) / pip_size
                        if _e55_dist_h < 3:
                            _touched_e55 = True

                    if _was_bullish and _p_h < _e100_h:
                        _held_above_e100 = False
                        _crossed_e100 = True
                    elif not _was_bullish and _p_h > _e100_h:
                        _held_above_e100 = False
                        _crossed_e100 = True

                _candles_holding = _held_above_e100
                _tested_e55 = _touched_e55
                _tested_e100 = _crossed_e100

                if _bb_min_after_peak < 999.0 and _bb_width_now > 0:
                    _bb_re_expanding = (
                        _bb_width_now > _bb_min_after_peak * 1.2
                        and _bb_delta_5bar > 0
                    )

                # Guardian-style reexpansion counter
                _reexpansion_count = 0
                _bb_min_idx = _peak_idx
                for _ri in range(_peak_idx, len(df)):
                    _bb_ri = float(df.iloc[_ri].get("bb_upper", 0)) - float(
                        df.iloc[_ri].get("bb_lower", 0)
                    )
                    if _bb_ri > 0 and _bb_ri <= _bb_min_after_peak * 1.05:
                        _bb_min_idx = _ri
                if _bb_min_idx < len(df) - 1:
                    for _rei in range(_bb_min_idx + 1, len(df)):
                        _re_e21 = float(df.iloc[_rei].get("ema_21", 0))
                        _re_e100 = float(df.iloc[_rei].get("ema_100", 0))
                        _re_e21_p = float(df.iloc[_rei - 1].get("ema_21", 0))
                        _re_e100_p = float(df.iloc[_rei - 1].get("ema_100", 0))
                        _re_bb = float(df.iloc[_rei].get("bb_upper", 0)) - float(
                            df.iloc[_rei].get("bb_lower", 0)
                        )
                        _re_bb_p = float(df.iloc[_rei - 1].get("bb_upper", 0)) - float(
                            df.iloc[_rei - 1].get("bb_lower", 0)
                        )
                        _re_fan_exp = abs(_re_e21 - _re_e100) > abs(_re_e21_p - _re_e100_p)
                        _re_bb_exp = _re_bb > _re_bb_p if _re_bb_p > 0 else False
                        if _re_fan_exp and _re_bb_exp:
                            _reexpansion_count += 1
                        else:
                            _reexpansion_count = 0
                _confirmed_reexpansion = _reexpansion_count >= 3

                # E55 SHALLOW RETRACEMENT
                _shallow_retrace = (
                    _candles_holding
                    and _tested_e55
                    and fan_direction in ("bullish", "bearish")
                    and _confirmed_reexpansion
                    and (_bb_re_expanding or _bb_expanding)
                )

                # E100 DEEP RETRACEMENT
                _deep_retrace = (
                    _crossed_e100
                    and fan_direction in ("bullish", "bearish")
                    and _bars_since_peak < 40
                    and _confirmed_reexpansion
                    and (_bb_re_expanding or _bb_expanding)
                )

                _is_retracement = _shallow_retrace or _deep_retrace
                if _shallow_retrace:
                    _retracement_type = "e55_shallow"
                elif _deep_retrace:
                    _retracement_type = "e100_deep"

                # PATH A: RETRACEMENT FORMING (before re-expansion)
                if not _is_retracement and _was_expanding_recently:
                    _fan_ordered_retrace = fan_direction in ("bullish", "bearish")
                    _price_at_e55 = _tested_e55 and _e55_dist_pips < 5
                    _price_at_e100 = _e100_dist_pips < 5

                    _stoch_supports = (
                        (fan_direction == "bullish" and _stoch_bull_cross)
                        or (fan_direction == "bearish" and _stoch_bear_cross)
                    )
                    _div_supports = (
                        (fan_direction == "bullish" and _rsi_bull_divergence)
                        or (fan_direction == "bearish" and _rsi_bear_divergence)
                    )
                    _candle_supports = (
                        _reversal_candle_at_ema
                        and _reversal_candle_direction == fan_direction
                    )

                    _forming_signals = sum([_stoch_supports, _div_supports, _candle_supports])

                    if (
                        _fan_ordered_retrace
                        and (_price_at_e55 or _price_at_e100)
                        and _forming_signals >= 1
                    ):
                        _is_retracement_forming = True
                        _ema_level = "e100" if _price_at_e100 else "e55"
                        _retracement_type = f"{_ema_level}_forming"
            _retrace_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _retrace_ok:
        out["is_retracement"] = _is_retracement
        out["is_retracement_forming"] = _is_retracement_forming
        out["retracement_type"] = _retracement_type
        out["was_expanding_recently"] = _was_expanding_recently
        out["peak_fan_width"] = _peak_fan_width
        out["candles_holding"] = _candles_holding
        out["bb_re_expanding"] = _bb_re_expanding
        out["tested_e55"] = _tested_e55
        out["tested_e100"] = _tested_e100

    # ──────────────────────────────────────────────────────────────────────
    # FAN-FLIP DETECTION
    # Catches trend reversals: fan was ordered in direction A, now re-ordering
    # in direction B. Not a retracement — a new trend starting.
    # Source: trade_scout.py:1894-1933
    # ──────────────────────────────────────────────────────────────────────
    _fan_flip_detected = False
    _fan_flip_direction: Optional[str] = None
    _flip_ok = False
    try:
        if len(df) >= 25:
            _curr_e21 = float(df.iloc[-1].get("ema_21", 0))
            _curr_e55 = float(df.iloc[-1].get("ema_55", 0))
            _curr_e100 = float(df.iloc[-1].get("ema_100", 0))
            _now_bullish = _curr_e21 > _curr_e55 > _curr_e100
            _now_bearish = _curr_e21 < _curr_e55 < _curr_e100

            _prev_bullish_count = 0
            _prev_bearish_count = 0
            for _fli in range(max(len(df) - 25, 0), max(len(df) - 5, 0)):
                _e21_fl = float(df.iloc[_fli].get("ema_21", 0))
                _e55_fl = float(df.iloc[_fli].get("ema_55", 0))
                _e100_fl = float(df.iloc[_fli].get("ema_100", 0))
                if _e21_fl > _e55_fl > _e100_fl:
                    _prev_bullish_count += 1
                elif _e21_fl < _e55_fl < _e100_fl:
                    _prev_bearish_count += 1

            _prev_dominant_bullish = _prev_bullish_count >= 5
            _prev_dominant_bearish = _prev_bearish_count >= 5
            if _now_bearish and _prev_dominant_bullish and _fan_expanding:
                _fan_flip_detected = True
                _fan_flip_direction = "bearish"
            elif _now_bullish and _prev_dominant_bearish and _fan_expanding:
                _fan_flip_detected = True
                _fan_flip_direction = "bullish"
            _flip_ok = True
    except (ValueError, TypeError, IndexError):
        pass
    if _flip_ok:
        out["fan_flip_detected"] = _fan_flip_detected
        out["fan_flip_direction"] = _fan_flip_direction

    # ──────────────────────────────────────────────────────────────────────
    # CHECKLIST SCORE (mirrors validator's 10-point system)
    # Source: trade_scout.py:1935-1952
    # ──────────────────────────────────────────────────────────────────────
    _checklist = {
        "ema_cross": _recent_cross,
        "dual_cross": _dual_cross_cascade,
        "candles_away": _candles_moving_away,
        "fan_opening": _fan_expanding,
        "fan_accelerating": _fan_accelerating,
        "bb_expanding": _bb_expanding,
        "bb_fan_parallel": _bb_expanding and _fan_expanding,
        "rsi_recovering": _rsi_recovery_ok and _rsi_healthy,
        "momentum_candles": _momentum_candles,
        "correct_side": _candles_correct_side,
        "no_wall": True,  # Can't detect from data — validator judges visually
        "stoch_cross": _stoch_bull_cross or _stoch_bear_cross,
        "rsi_divergence": _rsi_bull_divergence or _rsi_bear_divergence,
        "reversal_candle_at_ema": _reversal_candle_at_ema,
    }
    _checklist_score = sum(1 for v in _checklist.values() if v)
    out["checklist"] = _checklist
    out["checklist_score"] = _checklist_score

    return out
