"""oanda_chart_pattern_regen.py — Regenerate M15 charts WITH:

1. Swing-trace overlay (red/green dots + connecting line)
2. Pattern labels (from pattern_detectors.py) at the bars where each detected
   pattern fires, using the exact pattern name from pattern_library.md
3. (Optional) Daily pivot lines — kept off by default for iter 19 cleanliness;
   set INCLUDE_PIVOTS=True to enable.

Public API:
    regenerate_chart_with_patterns(pair, entry_time_iso, output_path)
        -> (str | None, list[fires])

Used by replay_iter19_patterns.py for the iter 19 pattern-detection experiment.
"""

import logging
import os
import shutil
import sys
from datetime import datetime, timezone

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

from scripts.oanda_chart_swing_regen import detect_swings
from scripts import pattern_detectors as pd_det

logger = logging.getLogger(__name__)

GRANULARITY_M15 = "M15"
CANDLE_COUNT = 250

_CHART_TEMP_DIR = "/tmp/replay_charts_pattern"
os.makedirs(_CHART_TEMP_DIR, exist_ok=True)
os.environ.setdefault("CHART_OUTPUT_DIR", _CHART_TEMP_DIR)

INCLUDE_PIVOTS = False  # iter 19 keeps pivots off; toggle to test stacking


def regenerate_chart_with_patterns(pair: str, entry_time_iso: str,
                                   output_path: str) -> tuple:
    """Returns (output_path|None, fires_list)."""
    import pandas as pd
    from oanda_client import OandaClient
    from indicators import Indicators

    ts = entry_time_iso.replace("Z", "+00:00")
    entry_dt = datetime.fromisoformat(ts)
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=timezone.utc)

    try:
        with OandaClient() as client:
            candles = client.get_candles(
                instrument=pair, granularity=GRANULARITY_M15,
                count=CANDLE_COUNT, price="M", to_time=entry_dt,
            )
    except Exception as e:
        logger.error("[%s] M15 fetch failed: %s", pair, e)
        return None, []
    if not candles or len(candles) < 30:
        return None, []

    rows = []
    for c in candles:
        mid = c.get("mid", {})
        if not mid:
            continue
        rows.append({
            "time": c["time"],
            "open": float(mid.get("o", 0) or 0),
            "high": float(mid.get("h", 0) or 0),
            "low": float(mid.get("l", 0) or 0),
            "close": float(mid.get("c", 0) or 0),
            "volume": int(c.get("volume", 0) or 0),
        })
    df = pd.DataFrame(rows)

    # Indicators for BB/EMA-dependent detectors
    engine = Indicators(candles)
    engine.compute_emas()
    ind = engine.compute_all()
    ema_df = engine.df
    ema21 = list(ema_df["ema_21"].values)
    ema55 = list(ema_df["ema_55"].values)
    ema100 = list(ema_df["ema_100"].values)
    # BB indicator returns scalars; compute 20-period 2σ Bollinger series ourselves
    # so enrich_with_context can look up BB position at arbitrary bar indexes.
    bb_period = 20
    bb_std_mult = 2.0
    bb_mid_s = df["close"].rolling(bb_period).mean()
    bb_std_s = df["close"].rolling(bb_period).std()
    bb_upper = list((bb_mid_s + bb_std_mult * bb_std_s).values)
    bb_lower = list((bb_mid_s - bb_std_mult * bb_std_s).values)
    bb_mid = list(bb_mid_s.values)

    # RSI series + fan-direction + phase for context enrichment
    rsi = ind.get("rsi", {})
    rsi_series = rsi.get("series", []) if isinstance(rsi, dict) else []
    if hasattr(rsi_series, "values"):
        rsi_series = list(rsi_series.values)
    elif isinstance(rsi_series, list) and rsi_series:
        pass  # already a list
    else:
        rsi_series = list(engine.df["rsi"].values) if "rsi" in engine.df.columns else []
    # Derive fan_direction + phase from EMAs
    e21_last, e55_last, e100_last = ema21[-1], ema55[-1], ema100[-1]
    if e21_last > e55_last > e100_last:
        fan_direction = "bullish"
    elif e21_last < e55_last < e100_last:
        fan_direction = "bearish"
    else:
        fan_direction = "mixed"
    # Simple phase derivation: if fully ordered = phase 3, partially = 2, else 1/0
    phase = 3 if fan_direction in ("bullish", "bearish") else 1

    fires = pd_det.detect_all(df, bb_upper=bb_upper, bb_lower=bb_lower,
                               bb_mid=bb_mid, ema21=ema21, ema55=ema55,
                               ema100=ema100, rsi_series=rsi_series,
                               fan_direction=fan_direction, phase=phase,
                               pair_hint=pair)
    logger.info("[%s] detected %d patterns: %s", pair, len(fires),
                [f["name"] for f in fires])

    swings = detect_swings(df["high"].values, df["low"].values)

    os.environ["CHART_OUTPUT_DIR"] = _CHART_TEMP_DIR
    try:
        import chart_generator as _cg
        if getattr(_cg, "OUTPUT_DIR", None) != _CHART_TEMP_DIR:
            import importlib
            importlib.reload(_cg)
        pivots = None
        if INCLUDE_PIVOTS:
            from scripts.oanda_chart_pivot_regen import fetch_prior_daily_pivots
            pivots = fetch_prior_daily_pivots(pair, entry_dt)
        generated_path = _cg.generate_chart(
            pair, df,
            pivot_levels=pivots,
            swing_overlay=swings,
            pattern_labels=fires,
        )
    except Exception as e:
        logger.error("[%s] generate_chart failed: %s", pair, e)
        return None, []

    if not generated_path or not os.path.exists(generated_path):
        return None, []

    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copy(generated_path, output_path)
    except Exception as e:
        logger.error("[%s] Copy failed: %s", pair, e)
        return None, []

    size = os.path.getsize(output_path)
    if size < 30_000:
        logger.warning("[%s] Chart small: %d bytes", pair, size)
        return None, []
    logger.info("[%s] pattern chart OK → %s (%d KB, %d patterns)",
                pair, output_path, size // 1024, len(fires))
    return output_path, fires


def smoke_test(pair: str = "EUR_USD",
               entry_time_iso: str = "2026-05-07T10:17:52+00:00") -> bool:
    out = "/tmp/smoke_test_pattern_chart.png"
    result, fires = regenerate_chart_with_patterns(pair, entry_time_iso, out)
    if result and os.path.exists(result):
        size = os.path.getsize(result)
        print(f"SMOKE TEST PASS: {pair} {entry_time_iso}")
        print(f"  → {result} ({size//1024}KB)")
        print(f"  → patterns detected: {[f['name'] for f in fires]}")
        return True
    print(f"SMOKE TEST FAIL: {pair} {entry_time_iso}")
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    pair = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"
    entry = sys.argv[2] if len(sys.argv) > 2 else "2026-05-07T10:17:52+00:00"
    smoke_test(pair, entry)
