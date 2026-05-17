"""early_exhaustion_evaluator — pure-function evaluator for the failed_rally rewrite.

Standalone (no guardian code touched). Given:
  - trade metadata (pair, direction, entry_price, entry_time)
  - M15 candle list since entry (with full warmup history for indicators)

Returns:
  {
    "would_fire":   bool,
    "reason":       str,                   # human-readable explanation
    "mfe":          float, "mfe_bar": int,
    "decision_bar": int,
    "p_loser":      float | None,          # classifier output if in universe
    "features":     dict | None,           # snapshot features at decision bar
    "target_sl_pips": float | None,        # entry + lock_pips in profit direction
  }

Tuning is read at call time via tuning_config.get(). Classifier coefficients
are loaded from <repo_root>/Source/early_exhaustion_classifier.json
on import (cached for process lifetime).

Used by:
  - scripts/exhaustion_handler_shadow.py — background dry-run poller
  - (future) position_guardian.py — once dry-run validates and Tim approves live cutover

Scope: ONLY acts on trades that aren't recovering (brief-positive pattern,
classifier signals exhaustion). Does NOT touch profit management.
"""
from __future__ import annotations
import os
import json
import math
import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
CLASSIFIER_PATH = os.path.join(_HERE, "early_exhaustion_classifier.json")

# Cache (process-lifetime). Reload by calling reload_classifier().
_CLF: Dict[str, Any] = {}


def _load_classifier() -> Dict[str, Any]:
    global _CLF
    if _CLF:
        return _CLF
    if not os.path.exists(CLASSIFIER_PATH):
        logger.warning("early_exhaustion: classifier file missing at %s", CLASSIFIER_PATH)
        return {}
    with open(CLASSIFIER_PATH) as f:
        data = json.load(f)
    _CLF = {
        "coefs":        np.array(list(data["classifier_coefs"].values()), dtype=float),
        "intercept":    float(data["classifier_intercept"]),
        "scaler_mean":  np.array(data["classifier_scaler_mean"], dtype=float),
        "scaler_scale": np.array(data["classifier_scaler_scale"], dtype=float),
        "feature_names": data["feature_names"],
    }
    return _CLF


def reload_classifier() -> None:
    global _CLF
    _CLF = {}
    _load_classifier()


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _stoch_k(df: pd.DataFrame, period: int = 14) -> pd.Series:
    low_min = df["low"].rolling(window=period).min()
    high_max = df["high"].rolling(window=period).max()
    return 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr_v = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr_v)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr_v)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _macd_hist(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    fast_e = closes.ewm(span=fast, adjust=False).mean()
    slow_e = closes.ewm(span=slow, adjust=False).mean()
    macd_line = fast_e - slow_e
    sig = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - sig


def _bollinger(closes: pd.Series, period: int = 20, std: float = 2.0):
    mid = closes.rolling(window=period).mean()
    sd = closes.rolling(window=period).std()
    upper = mid + std * sd
    lower = mid - std * sd
    width = (2 * std * sd) / mid
    return upper, mid, lower, width


def _candles_to_df(candles: List[Dict[str, Any]]) -> pd.DataFrame:
    """Accept OANDA candle list and return indicator-ready DataFrame."""
    rows = []
    for c in candles:
        mid = c.get("mid") or {}
        rows.append({
            "time":   c.get("time", ""),
            "open":   float(mid.get("o", c.get("open", 0))),
            "high":   float(mid.get("h", c.get("high", 0))),
            "low":    float(mid.get("l", c.get("low", 0))),
            "close":  float(mid.get("c", c.get("close", 0))),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["time_dt"] = pd.to_datetime(df["time"].str.replace("Z", "", regex=False),
                                    format="mixed", utc=True)
    df["ema_21"]  = _ema(df["close"], 21)
    df["ema_55"]  = _ema(df["close"], 55)
    df["ema_100"] = _ema(df["close"], 100)
    df["rsi"]     = _rsi(df["close"])
    df["stoch_k"] = _stoch_k(df)
    df["adx"]     = _adx(df)
    df["macd_histogram"] = _macd_hist(df["close"])
    bb_u, bb_m, bb_l, bb_w = _bollinger(df["close"])
    df["bb_upper"]  = bb_u
    df["bb_middle"] = bb_m
    df["bb_lower"]  = bb_l
    df["bb_width"]  = bb_w
    return df


def _safe(v: Any, default: float = 0.0) -> float:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _candle_color(row) -> str:
    o = float(row["open"]); c = float(row["close"])
    rng = float(row["high"]) - float(row["low"])
    if rng <= 0:
        return "doji"
    body = abs(c - o)
    if body / rng < 0.15:
        return "doji"
    return "green" if c > o else "red"


def _candle_type(row) -> str:
    o = float(row["open"]); c = float(row["close"])
    h = float(row["high"]); l = float(row["low"])
    rng = h - l
    if rng <= 0:
        return "doji"
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_frac = body / rng if rng > 0 else 0
    if body_frac < 0.15:
        return "doji"
    if upper_wick > 2 * body and lower_wick < body * 0.5:
        return "shooting_star" if c < o else "exhaustion_top_wick"
    if lower_wick > 2 * body and upper_wick < body * 0.5:
        return "hammer" if c > o else "exhaustion_bot_wick"
    return "normal"


def _snapshot(df: pd.DataFrame, idx: int, is_buy: bool, entry: float, pip: float) -> Optional[Dict[str, Any]]:
    if idx < 0 or idx >= len(df):
        return None
    row = df.iloc[idx]
    e21 = _safe(row.get("ema_21"))
    e55 = _safe(row.get("ema_55"))
    e100 = _safe(row.get("ema_100"))
    close = float(row["close"])

    if is_buy:
        fan_ordered = 1 if (e21 > e55 > e100) else 0
        fan_inverted = 1 if (e21 < e55 < e100) else 0
    else:
        fan_ordered = 1 if (e21 < e55 < e100) else 0
        fan_inverted = 1 if (e21 > e55 > e100) else 0

    e21_e55_pips = (e21 - e55) / pip if is_buy else (e55 - e21) / pip
    e55_e100_pips = (e55 - e100) / pip if is_buy else (e100 - e55) / pip

    fan_velocity = 0.0
    if idx >= 5:
        prev = df.iloc[idx - 5]
        pe21 = _safe(prev.get("ema_21")); pe55 = _safe(prev.get("ema_55"))
        if pe21 and pe55:
            prev_gap = (pe21 - pe55) / pip if is_buy else (pe55 - pe21) / pip
            fan_velocity = e21_e55_pips - prev_gap

    bb_upper = _safe(row.get("bb_upper")); bb_lower = _safe(row.get("bb_lower"))
    bb_width = _safe(row.get("bb_width"))
    if bb_upper and bb_lower:
        bb_pos = 1 if close >= bb_upper else (-1 if close <= bb_lower else 0)
    else:
        bb_pos = 0
    bb_width_ratio = 1.0
    if idx >= 20:
        prev_widths = df["bb_width"].iloc[max(0, idx - 20):idx]
        mean = prev_widths.mean()
        if mean and not math.isnan(mean) and mean > 0:
            bb_width_ratio = bb_width / mean

    candle_vs_e21 = 0
    if e21:
        if is_buy:
            candle_vs_e21 = 1 if close > e21 else (-1 if close < e21 else 0)
        else:
            candle_vs_e21 = 1 if close < e21 else (-1 if close > e21 else 0)

    # Color streak last 5 (relative to trade direction)
    streak = []
    for i in range(max(0, idx - 4), idx + 1):
        streak.append(_candle_color(df.iloc[i]))
    in_trend = "green" if is_buy else "red"
    counter_color_count = sum(1 for c in streak if c != in_trend and c != "doji")

    ctype = _candle_type(row)
    if is_buy:
        is_reversal = 1 if ctype in ("shooting_star", "exhaustion_top_wick", "doji") else 0
    else:
        is_reversal = 1 if ctype in ("hammer", "exhaustion_bot_wick", "doji") else 0

    return {
        "rsi": _safe(row.get("rsi")),
        "stoch_k": _safe(row.get("stoch_k")),
        "adx": _safe(row.get("adx")),
        "macd_hist": _safe(row.get("macd_histogram")),
        "bb_pos": bb_pos,
        "bb_width_ratio": bb_width_ratio,
        "fan_ordered": fan_ordered,
        "fan_inverted": fan_inverted,
        "e21_e55_pips": e21_e55_pips,
        "e55_e100_pips": e55_e100_pips,
        "fan_velocity": fan_velocity,
        "candle_vs_e21": candle_vs_e21,
        "counter_color_count": counter_color_count,
        "is_reversal_candle": is_reversal,
        "candle_type": ctype,
    }


def _classifier_probability(features: Dict[str, Any], mfe: float, mfe_bar: int,
                             mae_at_peak: float, decision_bar: int) -> Optional[float]:
    clf = _load_classifier()
    if not clf:
        return None
    feature_keys = [
        "rsi", "stoch_k", "adx", "macd_hist",
        "bb_pos", "bb_width_ratio",
        "fan_ordered", "fan_inverted",
        "e21_e55_pips", "e55_e100_pips", "fan_velocity",
        "candle_vs_e21", "counter_color_count", "is_reversal_candle",
    ]
    x = np.array([features.get(k, 0) for k in feature_keys] +
                 [mfe, mfe_bar, mae_at_peak, decision_bar], dtype=float)
    x_scaled = (x - clf["scaler_mean"]) / clf["scaler_scale"]
    logit = float(np.dot(x_scaled, clf["coefs"])) + clf["intercept"]
    return 1.0 / (1.0 + math.exp(-logit))


def evaluate_trade(
    pair: str,
    direction: str,
    entry_price: float,
    entry_time_iso: str,
    m15_candles_since_entry_with_warmup: List[Dict[str, Any]],
    *,
    mfe_min_pips: float = 3.0,
    mfe_max_pips: float = 10.0,
    arm_window_bars: int = 8,
    classifier_threshold: float = 0.65,
    lock_pips: float = 0.5,
) -> Dict[str, Any]:
    """Pure-function evaluator. See module docstring."""
    is_buy = direction.lower() in ("buy", "long")
    pip = 0.01 if "JPY" in pair.upper() else 0.0001
    out = {
        "would_fire": False,
        "reason": "",
        "mfe": 0.0, "mfe_bar": -1,
        "mae_at_peak": 0.0,
        "decision_bar": -1,
        "p_loser": None,
        "features": None,
        "target_sl_pips": None,
        "target_sl_price": None,
    }

    df = _candles_to_df(m15_candles_since_entry_with_warmup)
    if df.empty:
        out["reason"] = "no candles"
        return out

    # Find first candle whose close time > entry_time
    entry_time = pd.to_datetime(entry_time_iso.replace("Z", ""), utc=True)
    bar_close = df["time_dt"] + pd.Timedelta(minutes=15)
    mask = bar_close > entry_time
    if not mask.any():
        out["reason"] = "entry after last candle"
        return out
    ent_idx = int(mask.idxmax())

    # Walk segment from entry → end of df
    seg_indices = list(range(ent_idx, len(df)))
    closes_pips: List[float] = []
    highs_pips: List[float] = []
    lows_pips: List[float] = []
    for i in seg_indices:
        row = df.iloc[i]
        h = float(row["high"]); lo = float(row["low"]); cl = float(row["close"])
        if is_buy:
            closes_pips.append((cl - entry_price) / pip)
            highs_pips.append((h - entry_price) / pip)
            lows_pips.append((lo - entry_price) / pip)
        else:
            closes_pips.append((entry_price - cl) / pip)
            highs_pips.append((entry_price - lo) / pip)
            lows_pips.append((entry_price - h) / pip)

    if not closes_pips:
        out["reason"] = "no bars after entry"
        return out

    mfe = max(highs_pips); mfe_bar = highs_pips.index(mfe)
    mae = min(lows_pips); mae_bar = lows_pips.index(mae)
    mae_at_peak = min(lows_pips[:mfe_bar + 1])
    out["mfe"] = round(mfe, 1)
    out["mfe_bar"] = mfe_bar
    out["mae_at_peak"] = round(mae_at_peak, 1)

    # Pattern check
    first_pos = next((i for i, p in enumerate(closes_pips) if p > 0), -1)
    if first_pos < 0:
        out["reason"] = f"never positive (MFE close-side ≤ 0); rule does not handle this case"
        return out
    if first_pos == 0 and mfe > 0:
        pattern = "positive_at_entry"
    elif first_pos >= 5:
        pattern = "long_neg_then_brief_pos"
    else:
        pattern = "short_neg_then_brief_pos"
    if pattern not in ("long_neg_then_brief_pos", "short_neg_then_brief_pos"):
        out["reason"] = f"pattern={pattern}, not brief-positive (rule does not apply)"
        return out

    # Decision bar: first negative close after MFE bar
    decision_bar = -1
    for i in range(mfe_bar + 1, len(closes_pips)):
        if closes_pips[i] < 0:
            decision_bar = i
            break
    if decision_bar < 0:
        out["reason"] = "no negative close after MFE peak yet — rule not armed"
        return out
    out["decision_bar"] = decision_bar

    # Universe gates
    if mfe < mfe_min_pips:
        out["reason"] = f"MFE {mfe:.1f}p < min {mfe_min_pips}p (rally too small)"
        return out
    if mfe >= mfe_max_pips:
        out["reason"] = f"MFE {mfe:.1f}p ≥ max {mfe_max_pips}p (above rule ceiling — profit management owns this)"
        return out
    if decision_bar > arm_window_bars:
        out["reason"] = f"decision_bar {decision_bar} > arm_window {arm_window_bars}"
        return out

    # Snapshot features at decision bar
    decision_idx = ent_idx + decision_bar
    snap = _snapshot(df, decision_idx, is_buy, entry_price, pip)
    if snap is None:
        out["reason"] = "snapshot failed"
        return out
    out["features"] = snap

    # Classifier
    p_loser = _classifier_probability(snap, mfe, mfe_bar, mae_at_peak, decision_bar)
    out["p_loser"] = round(p_loser, 3) if p_loser is not None else None
    if p_loser is None:
        out["reason"] = "classifier unavailable"
        return out
    if p_loser < classifier_threshold:
        out["reason"] = f"P(loser)={p_loser:.3f} < threshold {classifier_threshold}"
        return out

    # FIRE
    out["would_fire"] = True
    out["target_sl_pips"] = lock_pips
    out["target_sl_price"] = entry_price + lock_pips * pip if is_buy else entry_price - lock_pips * pip
    out["reason"] = (f"FIRE — MFE={mfe:.1f}p @ bar {mfe_bar}, decision_bar={decision_bar}, "
                     f"P(loser)={p_loser:.3f} ≥ {classifier_threshold}")
    return out
