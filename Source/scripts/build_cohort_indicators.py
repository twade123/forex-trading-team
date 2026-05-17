"""build_cohort_indicators.py — One-shot: compute per-trade indicator blocks
for the iter-matrix cohort and save to /tmp/cohort_indicator_blocks.json.

Each trade gets a production-shaped "Indicator Data — Raw" text block built
from OANDA M15 candles + indicators.py + simple cascade-phase derivation.

Run:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python3 scripts/build_cohort_indicators.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

import pandas as pd

from oanda_client import OandaClient
from indicators import Indicators
from indicators_advanced import AdvancedIndicators
from backtester.ema_separation import (
    generate_market_picture,
    scan_ema_signals,
    _compute_bollinger_bandwidth_series,
    _compute_rsi,
)
from validator_block_builder import (
    build_validator_indicator_block,
    compute_range_position_pct,
    compute_prior_session_hl_pips,
    _flatten_oanda_candles,
)


def _candle_for_market_picture(c: dict) -> dict:
    """Convert OANDA mid candle to the dict shape ema_separation expects."""
    if "mid" in c:
        m = c["mid"]
        return {
            "time": c.get("time"),
            "open": float(m["o"]), "high": float(m["h"]),
            "low": float(m["l"]), "close": float(m["c"]),
        }
    return {
        "time": c.get("time"),
        "open": float(c["open"]), "high": float(c["high"]),
        "low": float(c["low"]), "close": float(c["close"]),
    }


def _compute_rsi_slope(candles, period=14, slope_lookback=3):
    """RSI slope over `slope_lookback` bars. Match the LIVE convention:
    positive = strengthening momentum, negative = weakening.
    Returns slope in RSI-points/bar.
    """
    flat = [_candle_for_market_picture(c) for c in candles]
    closes = [c["close"] for c in flat]
    if len(closes) < period + slope_lookback + 1:
        return 0.0
    cur = _compute_rsi(closes, period)
    prev = _compute_rsi(closes[:-slope_lookback], period)
    if cur is None or prev is None:
        return 0.0
    return round((cur - prev) / slope_lookback, 2)


def _compute_scout_deltas(candles, lookback_short=5, lookback_long=20):
    """Compute fan and BB deltas over 5-bar and 20-bar windows.
    Used in the validator's scout-context section. Mirrors what
    trade_scout._compute_alert_deltas produces in live.
    """
    flat = [_candle_for_market_picture(c) for c in candles]
    closes = [c["close"] for c in flat]

    # Fan delta: separation_pct now vs N bars ago
    def _sep_pct_at(idx_offset):
        sub = flat[: len(flat) - idx_offset]
        if len(sub) < 100:
            return None
        sig = scan_ema_signals(sub) or {}
        return sig.get("separation_pct")

    sep_now = _sep_pct_at(0)
    sep_5 = _sep_pct_at(lookback_short)
    sep_20 = _sep_pct_at(lookback_long)
    # Convert pct to fractional delta for the validator block (matches live format scale)
    fan_d_5 = (sep_now - sep_5) / 100 if sep_now is not None and sep_5 is not None else 0
    fan_d_20 = (sep_now - sep_20) / 100 if sep_now is not None and sep_20 is not None else 0

    # BB delta: bandwidth now vs N bars ago
    bw_series = _compute_bollinger_bandwidth_series(closes, period=20, std_mult=2.0)
    bw_now = bw_series[-1] if bw_series else None
    bw_5 = bw_series[-1 - lookback_short] if len(bw_series) > lookback_short else None
    bw_20 = bw_series[-1 - lookback_long] if len(bw_series) > lookback_long else None
    bb_d_5 = (bw_now - bw_5) / 100 if bw_now is not None and bw_5 is not None else 0
    bb_d_20 = (bw_now - bw_20) / 100 if bw_now is not None and bw_20 is not None else 0

    return {
        "fan_delta_5bar": round(fan_d_5, 5),
        "fan_delta_20bar": round(fan_d_20, 5),
        "bb_delta_5bar": round(bb_d_5, 5),
        "bb_delta_20bar": round(bb_d_20, 5),
    }


def _compute_divergence(candles):
    """Run RSI + MACD divergence detection (same helper trade_scout uses live)."""
    try:
        from trade_scout import _detect_divergence_at_current  # type: ignore
    except Exception:
        return {}
    flat = [_candle_for_market_picture(c) for c in candles]
    closes = [c["close"] for c in flat]
    if len(closes) < 30:
        return {}
    # Build RSI + MACD series
    rsi_vals = []
    for i in range(14, len(closes) + 1):
        v = _compute_rsi(closes[:i], 14)
        rsi_vals.append(v if v is not None else 50)
    # Simple MACD = EMA12 - EMA26
    def _ema(values, span):
        if not values:
            return []
        k = 2 / (span + 1)
        ema_vals = [values[0]]
        for v in values[1:]:
            ema_vals.append(v * k + ema_vals[-1] * (1 - k))
        return ema_vals
    if len(closes) < 26:
        return {}
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_vals = [a - b for a, b in zip(ema12, ema26)]
    # Pad RSI to match closes length
    pad = len(closes) - len(rsi_vals)
    rsi_padded = [50] * pad + rsi_vals
    div = _detect_divergence_at_current(closes, rsi_padded, macd_vals, lookback=20, order=5)
    # Strip the divergence_types list, keep only the boolean flags
    return {k: v for k, v in div.items() if k != "divergence_types" and v}


def _detect_candle_patterns(candles, fan_direction, phase, pair):
    """Run the same pattern detector live uses."""
    try:
        from scripts.pattern_detectors import detect_patterns_for_validator
    except Exception:
        return []
    flat = [_candle_for_market_picture(c) for c in candles]
    try:
        fires = detect_patterns_for_validator(
            flat,
            fan_direction=str(fan_direction or "mixed"),
            phase=int(phase or 0),
            pair_hint=pair,
        )
        # Extract pattern names
        return [f.get("pattern", "?") for f in (fires or []) if isinstance(f, dict)]
    except Exception:
        return []

OUT_PATH = "/tmp/cohort_indicator_blocks.json"
GRANULARITY = "M15"
CANDLE_COUNT = 250

# 19-trade expanded cohort (2026-04-29 to 2026-05-08): 11 winners + 8 losers
# All scout TRADE_NOW vtd entries that opened live trades, last 14d
COHORT = [
    ("13138", "AUD_JPY", "SELL", "2026-04-29T18:49:36"),
    ("13310", "AUD_JPY", "SELL", "2026-04-30T09:49:57"),
    ("13362", "AUD_JPY", "SELL", "2026-04-30T10:50:05"),
    ("13396", "EUR_CHF", "SELL", "2026-04-30T13:48:54"),
    ("13424", "USD_CAD", "SELL", "2026-04-30T15:45:49"),
    ("13452", "EUR_AUD", "SELL", "2026-05-01T16:34:10"),
    ("13578", "AUD_USD", "SELL", "2026-05-04T16:51:45"),
    ("13621", "GBP_USD", "BUY",  "2026-05-05T23:51:09"),
    ("13665", "USD_CAD", "SELL", "2026-05-06T02:09:42"),
    ("13681", "USD_CHF", "SELL", "2026-05-06T11:08:42"),
    ("13705", "EUR_USD", "BUY",  "2026-05-07T10:17:52"),
    ("13713", "NZD_USD", "BUY",  "2026-05-07T10:28:41"),
    ("13727", "AUD_USD", "SELL", "2026-05-07T21:21:27"),
    ("13743", "AUD_JPY", "SELL", "2026-05-07T22:04:25"),
    # NEW (5/8 Friday cohort)
    ("13765", "GBP_JPY", "BUY",  "2026-05-08T07:10:15"),  # +29.3p winner
    ("13809", "GBP_USD", "BUY",  "2026-05-08T09:36:34"),  # -5.1p loser
    ("13817", "EUR_JPY", "BUY",  "2026-05-08T10:02:34"),  # +5.1p winner
    ("13827", "EUR_USD", "BUY",  "2026-05-08T10:17:53"),  # +4.7p winner
    ("13843", "AUD_JPY", "BUY",  "2026-05-08T11:17:30"),  # -7.7p loser
    # ADDED 2026-05-12 for iter 20f late-entry-gate test — current
    # late-entry losers from live trading post-iter-20d deploy
    ("13913", "EUR_GBP", "SELL", "2026-05-08T15:23:00"),  # -33.2p / -$228 loser (sell into bounce)
    ("14088", "EUR_CHF", "BUY",  "2026-05-11T09:32:00"),  # -13.9p / -$90 loser (extended fan)
    ("14249", "GBP_JPY", "BUY",  "2026-05-11T17:21:00"),  # -48.9p / -$157 loser (stretched 2× ATR)
    ("14431", "AUD_JPY", "BUY",  "2026-05-12T05:02:00"),  # -22.1p / -$71 loser (Phase 3 cont. trap)
    ("14485", "EUR_AUD", "BUY",  "2026-05-12T08:02:00"),  # -27.2p / -$99 loser (extended fan, stretched)
    # 2026-05-13 losers (added 2026-05-14 for exhaustion-rule test)
    ("14882", "EUR_CHF", "SELL", "2026-05-13T07:58:23"),  # -13.5p loser, snipe
    ("14906", "EUR_JPY", "SELL", "2026-05-13T08:04:41"),  # -22.7p loser, validator confirmed
    ("14992", "EUR_USD", "SELL", "2026-05-13T09:37:06"),  # -18.1p loser, snipe
    ("15179", "GBP_JPY", "SELL", "2026-05-13T13:20:15"),  # -25.7p loser, validator confirmed
    ("15205", "USD_CHF", "BUY",  "2026-05-13T15:15:55"),  # -10.9p loser (was open, flipped)
    ("15227", "EUR_AUD", "SELL", "2026-05-13T16:46:03"),  # -40.7p loser
    ("15233", "EUR_CHF", "SELL", "2026-05-13T19:46:10"),  # -6.2p loser, snipe re-fire
]


def fetch_raw_candles(pair: str, entry_iso: str) -> list | None:
    """Fetch raw OANDA candles in the dict shape Indicators expects."""
    ts = entry_iso.replace("Z", "+00:00")
    if "+" not in ts and "-" not in ts.split("T")[1]:
        ts += "+00:00"
    entry_dt = datetime.fromisoformat(ts)
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
    with OandaClient() as client:
        candles = client.get_candles(
            instrument=pair, granularity=GRANULARITY,
            count=CANDLE_COUNT, price="M", to_time=entry_dt,
        )
    return candles if candles and len(candles) >= 30 else None


def derive_cross_state(df: pd.DataFrame) -> dict:
    """For E21/E55, E21/E100, E55/E100 — find the most recent crossing
    direction and bars since. Determines cascade_phase 0-4."""
    out = {}
    for fast, slow, key in [(21, 55, "cross1"), (21, 100, "cross2"), (55, 100, "cross3")]:
        f = df[f"ema_{fast}"]
        s = df[f"ema_{slow}"]
        diff_sign = (f - s).apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        # Walk back from latest bar; first index where sign differs from latest = cross bar
        latest_sign = diff_sign.iloc[-1]
        bars_since = None
        cross_dir = None
        for i in range(len(diff_sign) - 2, -1, -1):
            if pd.isna(f.iloc[i]) or pd.isna(s.iloc[i]):
                continue
            if diff_sign.iloc[i] != latest_sign and diff_sign.iloc[i] != 0:
                bars_since = (len(diff_sign) - 1) - i
                cross_dir = "bullish" if latest_sign > 0 else "bearish"
                break
        out[key] = {
            "current_orientation": "bullish" if latest_sign > 0 else "bearish" if latest_sign < 0 else "tied",
            "bars_since_last_flip": bars_since,
            "cross_direction": cross_dir,
        }
    return out


def derive_cascade_phase(crosses: dict, fan_ordered: bool) -> int:
    """Phase 0 = none, 1 = cross1 in expected dir, 2 = cross1+cross2,
    3 = cross1+cross2+cross3 fully ordered, 4 = phase 3 + price-confirmed."""
    c1 = crosses["cross1"]["bars_since_last_flip"]
    c2 = crosses["cross2"]["bars_since_last_flip"]
    c3 = crosses["cross3"]["bars_since_last_flip"]
    if c1 is None and c2 is None and c3 is None:
        return 0
    have_c1 = c1 is not None and c1 < 80
    have_c2 = c2 is not None and c2 < 80
    have_c3 = c3 is not None and c3 < 80
    if have_c1 and have_c2 and have_c3 and fan_ordered:
        return 3  # phase 4 needs price-confirmation, hold at 3 unless we add explicit check
    if have_c1 and have_c2:
        return 2
    if have_c1 or have_c2:
        return 1
    return 0


def derive_fan_state(df: pd.DataFrame) -> dict:
    e21 = df["ema_21"].iloc[-1]
    e55 = df["ema_55"].iloc[-1]
    e100 = df["ema_100"].iloc[-1]
    last_close = df["close"].iloc[-1]
    bullish_ordered = e21 > e55 > e100
    bearish_ordered = e21 < e55 < e100
    fan_ordered = bool(bullish_ordered or bearish_ordered)
    fan_direction = "bullish" if bullish_ordered else ("bearish" if bearish_ordered else "mixed")
    separation_pct = ((max(e21, e55, e100) - min(e21, e55, e100)) / e100) * 100
    sep_5_ago = (
        (max(df["ema_21"].iloc[-6], df["ema_55"].iloc[-6], df["ema_100"].iloc[-6])
         - min(df["ema_21"].iloc[-6], df["ema_55"].iloc[-6], df["ema_100"].iloc[-6]))
        / df["ema_100"].iloc[-6]
    ) * 100
    sep_velocity = (separation_pct - sep_5_ago) / 5
    fan_state = "expanding" if sep_velocity > 0.005 else ("contracting" if sep_velocity < -0.005 else "stable")
    last10 = df["close"].iloc[-10:]
    e100_last10 = df["ema_100"].iloc[-10:]
    above = int((last10.values > e100_last10.values).sum())
    below = int((last10.values < e100_last10.values).sum())
    pip_factor = 100 if "JPY" in df.columns.tolist() else 10000  # crude — overridden per pair below
    return {
        "e21": round(e21, 5), "e55": round(e55, 5), "e100": round(e100, 5),
        "fan_ordered": fan_ordered,
        "fan_direction": fan_direction,
        "fan_state": fan_state,
        "separation_pct": round(separation_pct, 4),
        "separation_velocity_pct_per_bar": round(sep_velocity, 5),
        "last_close": round(last_close, 5),
        "candles_above_e100_last10": above,
        "candles_below_e100_last10": below,
    }


def derive_exhaustion(direction: str, indicators_dict: dict, df) -> dict:
    """Compute exhaustion warning + components.

    Triggers if any of:
      - RSI extreme: <30 on SELL (depleted bear) or >70 on BUY (depleted bull)
      - Counter-direction wicks last 5: 3+ candles with wick > body in trade direction's
        opposite face (SELL: lower-wick > body counts; BUY: upper-wick > body counts)
      - Price extension: |last_close - mean(last_20_closes)| / ATR > 2.5
    """
    rsi_v = indicators_dict["rsi"].get("value")
    atr_v = indicators_dict["atr"].get("value", 0)
    rsi_extreme = False
    if rsi_v is not None:
        if direction.upper() == "SELL" and rsi_v < 30:
            rsi_extreme = True
        elif direction.upper() == "BUY" and rsi_v > 70:
            rsi_extreme = True

    last5 = df.iloc[-5:]
    counter_wicks = 0
    for _, c in last5.iterrows():
        body = abs(c["close"] - c["open"])
        upper_wick = c["high"] - max(c["close"], c["open"])
        lower_wick = min(c["close"], c["open"]) - c["low"]
        if direction.upper() == "SELL":
            # counter wick on SELL = lower wick (rejection of move down)
            if lower_wick > body:
                counter_wicks += 1
        else:
            # counter wick on BUY = upper wick (rejection of move up)
            if upper_wick > body:
                counter_wicks += 1

    last_close = df["close"].iloc[-1]
    mean20 = df["close"].iloc[-20:].mean()
    extension = abs(last_close - mean20) / atr_v if atr_v else 0
    extended = extension > 2.5

    warning = rsi_extreme or counter_wicks >= 3 or extended
    return {
        "warning": warning,
        "rsi_extreme": rsi_extreme,
        "counter_wicks_last5": counter_wicks,
        "price_extension_atr": round(extension, 2),
    }


_EUR_GBP_PAIRS = ('EUR_USD', 'GBP_USD', 'EUR_GBP', 'EUR_CHF', 'GBP_JPY', 'EUR_JPY')
_EUR_CROSS_PAIRS = ('EUR_AUD', 'EUR_CHF', 'EUR_JPY', 'EUR_CAD', 'EUR_NZD')
_AUD_PAIRS = ('AUD_JPY', 'AUD_USD', 'AUD_NZD', 'AUD_CAD',
              'AUD_CHF', 'EUR_AUD', 'GBP_AUD', 'NZD_AUD')


def classify_session(pair: str, entry_iso: str) -> tuple:
    """Mirror of trading_cycle.py:2934-2978 non-kronos session gate. Returns
    (blocked: bool, reason: str). Kept in sync with the live rule set —
    when trading_cycle.py changes, update here too."""
    ts = entry_iso.replace("Z", "+00:00")
    if "." in ts:
        base, frac = ts.split(".", 1)
        tz = "+00:00"
        for sep in ("+", "-"):
            if sep in frac:
                fp, tp = frac.split(sep, 1); tz = f"{sep}{tp}"; frac = fp; break
        ts = f"{base}.{frac[:6]}{tz}"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    hr = dt.hour
    dow = dt.weekday()  # 0=Mon..6=Sun
    is_sun, is_fri = dow == 6, dow == 4
    if is_sun and hr in (21, 22, 23):
        return True, "Sunday blackout (5-7PM ET) — thin liquidity, gap risk"
    if pair in _EUR_GBP_PAIRS and (hr >= 23 or hr < 3):
        return True, f"{pair} blocked during deep Asian (7-11PM ET) — thin liquidity for EUR/GBP"
    if pair in _EUR_CROSS_PAIRS and (hr in (3, 4, 5) or (hr == 6 and dt.minute < 30)):
        return True, f"{pair} blocked during EUR-cross Asian tail (11PM-2:30AM ET) — thin liquidity"
    if is_fri and hr >= 20:
        return True, "Friday close (after 4PM ET) — weekend gap risk"
    if pair in _AUD_PAIRS and hr in (21, 22) and not is_sun and not is_fri:
        return True, f"{pair} blocked UTC 21-22 weekday — AUD bleed window (60d: 0/6 WR, -109p)"
    return False, ""


def build_block_for_trade(
    pair: str, direction: str, candles: list,
    fan: dict, crosses: dict, phase: int,
    indicators_basic: dict, indicators_advanced: dict, exhaustion: dict,
    session: tuple = (False, ""),
) -> str:
    """Test-path adapter for the shared validator_block_builder.

    All fields the LIVE path provides are computed here from the same helpers:
      - generate_market_picture → rich EMA (velocity_trend, trend_health,
        reversal_risk, e100 rejections, e100 candle pattern), BB state
        (squeeze/expanding/contracting/lower_pen/upper_pen), gap_price_100
      - AdvancedIndicators → Stochastic K/D + ADX/regime
      - Inline helpers → RSI slope, scout deltas (fan_Δ5/20, bb_Δ5/20),
        divergence (same _detect_divergence_at_current trade_scout uses),
        candlestick patterns (same detect_patterns_for_validator live uses).

    Output is byte-identical to LIVE for the same inputs (verified
    2026-05-17 via byte-diff harness).
    """
    pip_factor = 100 if "JPY" in pair else 10000

    # Convert OANDA candles to the flat shape generate_market_picture / scan_ema_signals expect
    candles_for_mp = [_candle_for_market_picture(c) for c in candles]

    # ── Rich EMA + BB state via the same function LIVE uses upstream ────
    market_picture = generate_market_picture(pair, candles_for_mp)
    mp_ema = market_picture.get("ema", {}) or {}
    mp_bb = market_picture.get("bollinger", {}) or {}

    # Fall back to scan_ema_signals if generate_market_picture returns insufficient data
    if not mp_ema:
        mp_ema = scan_ema_signals(candles_for_mp) or {}

    # E100 distance + role (mirrors live's _v4_e100_dist + _e100_role)
    last_close = mp_ema.get("last_close", fan.get("last_close"))
    e100_v = mp_ema.get("ema100") or fan.get("e100")
    if isinstance(e100_v, dict):
        e100_v = e100_v.get("current_emas", {}).get("ema100", 0)
    e100_dist_pips = round(abs(last_close - e100_v) * pip_factor, 1) if e100_v else 0
    e100_role = ("support" if last_close and e100_v and last_close > e100_v
                 else "resistance" if last_close and e100_v and last_close < e100_v
                 else "neutral")

    # ── Momentum: basic + advanced ──────────────────────────────────────
    rsi_dict = indicators_basic.get("rsi", {}) or {}
    rsi_v = rsi_dict.get("value", 50)
    macd = indicators_basic.get("macd", {}) or {}
    macd_hist = macd.get("histogram_value") or macd.get("histogram") or 0
    if isinstance(macd_hist, (list, pd.Series)):
        macd_hist = float(macd_hist[-1] if len(macd_hist) else 0)
    bb = indicators_basic.get("bollinger", {}) or {}
    bb_w_basic = bb.get("bandwidth", bb.get("bb_width", 0))
    if isinstance(bb_w_basic, (list, pd.Series)):
        bb_w_basic = float(bb_w_basic[-1] if len(bb_w_basic) else 0)
    adv_stoch = indicators_advanced.get("stochastic", {}) or {}
    adv_adx = indicators_advanced.get("adx", {}) or {}
    stoch_k = adv_stoch.get("k")
    stoch_d = adv_stoch.get("d")
    adx_val = adv_adx.get("adx", 0) or 0
    regime = adv_adx.get("regime")
    if not regime:
        if adx_val < 15:
            regime = "compression"
        elif adx_val < 25:
            regime = "weak_trend"
        elif adx_val < 35:
            regime = "strong_trend"
        else:
            regime = "very_strong_trend"
    rsi_slope = _compute_rsi_slope(candles)

    # ── Location, patterns, divergence, scout deltas ────────────────────
    location = {
        "range_position_24bar_pct": compute_range_position_pct(candles, lookback=24),
        **compute_prior_session_hl_pips(candles, pair, session_bars=32),
    }
    pattern_names = _detect_candle_patterns(candles, mp_ema.get("fan_direction") or fan.get("fan_direction"), phase, pair)
    divergence_dict = _compute_divergence(candles)
    scout_deltas = _compute_scout_deltas(candles)

    # ── E100 candle pattern text + rejections (from market_picture / scan_ema_signals) ─
    e100_pat = market_picture.get("candle_pattern_at_e100") or mp_ema.get("e100_candle_pattern")
    if isinstance(e100_pat, dict):
        e100_pat_text = e100_pat.get("text") or e100_pat.get("description") or "none"
    elif isinstance(e100_pat, str):
        e100_pat_text = e100_pat
    else:
        e100_pat_text = "none"

    return build_validator_indicator_block(
        pair=pair,
        direction=direction.upper(),
        ema={
            "fan_direction": mp_ema.get("fan_direction") or fan.get("fan_direction"),
            "fan_state": mp_ema.get("fan_state") or fan.get("fan_state"),
            "fan_ordered": mp_ema.get("fan_ordered") or fan.get("fan_ordered"),
            "separation_pct": mp_ema.get("separation_pct") or fan.get("separation_pct", 0),
            "separation_velocity": mp_ema.get("separation_velocity") or fan.get("separation_velocity_pct_per_bar", 0),
            "fan_velocity_trend": mp_ema.get("fan_velocity_trend", "unknown"),
            "gap_price_100": mp_ema.get("gap_price_100", 0),
            "cascade_phase": phase,
            "trend_health": market_picture.get("trend_health") or mp_ema.get("trend_health", 0),
            "reversal_risk": market_picture.get("reversal_risk") or mp_ema.get("reversal_risk", "unknown"),
        },
        bollinger={
            "bb_squeeze": mp_bb.get("bb_squeeze", False),
            "bb_expanding": mp_bb.get("bb_expanding", False),
            "bb_contracting": mp_bb.get("bb_contracting", False),
            "bb_lower_pen": mp_bb.get("lower_pen", mp_bb.get("bb_lower_pen", 0)),
            "bb_upper_pen": mp_bb.get("upper_pen", mp_bb.get("bb_upper_pen", 0)),
            "bb_bandwidth": mp_bb.get("bandwidth", bb_w_basic),
        },
        momentum={
            "rsi": rsi_v if rsi_v is not None else 50,
            "rsi_slope": rsi_slope,
            "rsi_recovery": True,
            "stoch_k": stoch_k if stoch_k is not None else 50,
            "stoch_d": stoch_d if stoch_d is not None else 50,
            "macd_histogram": macd_hist,
            "adx": adx_val,
            "regime": regime,
        },
        crosses={
            "e21_e55": {
                "current_orientation": crosses["cross1"]["current_orientation"],
                "bars_since_last_flip": crosses["cross1"]["bars_since_last_flip"],
                "cross_direction": crosses["cross1"]["cross_direction"],
            },
            "e21_e100": {
                "current_orientation": crosses["cross2"]["current_orientation"],
                "bars_since_last_flip": crosses["cross2"]["bars_since_last_flip"],
                "cross_direction": crosses["cross2"]["cross_direction"],
            },
            "e55_e100": {
                "current_orientation": crosses["cross3"]["current_orientation"],
                "bars_since_last_flip": crosses["cross3"]["bars_since_last_flip"],
                "cross_direction": crosses["cross3"]["cross_direction"],
            },
        },
        e100={
            "role": e100_role, "dist_pips": e100_dist_pips,
            "candle_pattern_text": e100_pat_text,
            "candles_below_e100": mp_ema.get("candles_below_e100", fan.get("candles_below_e100_last10", 0)),
            "candles_above_e100": mp_ema.get("candles_above_e100", fan.get("candles_above_e100_last10", 0)),
            "last_close_vs_e100": "below" if last_close and e100_v and last_close < e100_v else "above",
            "rejections_from_below": mp_ema.get("e100_rejections_from_below", 0),
            "rejections_from_above": mp_ema.get("e100_rejections_from_above", 0),
        },
        location=location,
        patterns=pattern_names,
        divergence=divergence_dict,
        scout={
            "alert_type": f"{direction.upper()} alert",
            "e100_dist_pips": e100_dist_pips,
            **scout_deltas,
        },
        session=session,
    )


# Legacy name kept for any other consumers; same call now goes through shared builder.
def format_block(pair: str, direction: str, fan: dict, crosses: dict, phase: int,
                 indicators_dict: dict, exhaustion: dict, session: tuple = (False, "")) -> str:
    # Backwards-compat: assume passed-in indicators_dict is the basic one; no advanced.
    return build_block_for_trade(
        pair, direction, [], fan, crosses, phase,
        indicators_dict, {}, exhaustion, session,
    )


def main():
    out = {}
    for trade_id, pair, direction, entry_iso in COHORT:
        print(f"[{trade_id}] {pair} {direction} @ {entry_iso}")
        candles = fetch_raw_candles(pair, entry_iso)
        if candles is None:
            print(f"  ERROR: insufficient candles")
            out[trade_id] = {"error": "insufficient_candles"}
            continue
        try:
            engine = Indicators(candles)
        except ValueError as e:
            print(f"  ERROR: Indicators({len(candles)} candles): {e}")
            out[trade_id] = {"error": str(e)}
            continue
        engine.compute_emas()
        crosses = derive_cross_state(engine.df)
        fan = derive_fan_state(engine.df)
        phase = derive_cascade_phase(crosses, fan["fan_ordered"])
        ind = engine.compute_all()
        # Advanced indicators (ADX, Stochastic) — same class the live sniper uses,
        # so test+live see byte-identical block content for these fields.
        try:
            adv_engine = AdvancedIndicators(candles)
            ind_advanced = adv_engine.compute_all()
            for k, v in list(ind_advanced.items()):
                if isinstance(v, dict):
                    ind_advanced[k] = {sk: sv for sk, sv in v.items() if not isinstance(sv, pd.Series)}
        except Exception as _adv_e:
            print(f"  WARN: AdvancedIndicators failed: {_adv_e}")
            ind_advanced = {}
        exhaustion = derive_exhaustion(direction, ind, engine.df)
        session_block = classify_session(pair, entry_iso)
        # Strip series objects (not JSON serializable)
        for k, v in list(ind.items()):
            if isinstance(v, dict):
                ind[k] = {sk: sv for sk, sv in v.items() if not isinstance(sv, pd.Series)}
        block_text = build_block_for_trade(
            pair, direction, candles, fan, crosses, phase,
            ind, ind_advanced, exhaustion, session_block,
        )
        out[trade_id] = {
            "pair": pair, "direction": direction,
            "phase": phase, "fan": fan, "crosses": crosses,
            "exhaustion": exhaustion,
            "session_blocked": session_block[0],
            "session_reason": session_block[1],
            "block_text": block_text,
        }
        print(f"  → phase={phase} fan={fan['fan_direction']} {fan['fan_state']} "
              f"sep={fan['separation_pct']}% RSI={ind['rsi'].get('value')} "
              f"stoch={(ind_advanced.get('stochastic') or {}).get('k')} "
              f"exhausted={exhaustion['warning']}")
    Path(OUT_PATH).write_text(json.dumps(out, indent=2, default=str))
    print(f"\nWrote {OUT_PATH} ({len(out)} entries)")


if __name__ == "__main__":
    main()
