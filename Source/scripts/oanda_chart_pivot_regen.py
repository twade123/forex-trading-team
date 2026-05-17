"""oanda_chart_pivot_regen.py — Regenerate M15 charts WITH daily pivot overlay.

Same as oanda_chart_regen.py but additionally fetches the prior D1 candle,
computes classic floor pivots (PP/R1/S1/R2/S2), and passes them to
chart_generator.generate_chart() via the new pivot_levels kwarg.

Used by replay_iter18_pivots.py for the iter 18 pivot-visual experiment.

Public API:
    regenerate_chart_with_pivots(pair, entry_time_iso, output_path) -> str | None
"""

import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

logger = logging.getLogger(__name__)

GRANULARITY_M15 = "M15"
GRANULARITY_D = "D"
CANDLE_COUNT = 250

_CHART_TEMP_DIR = "/tmp/replay_charts_pivot"
os.makedirs(_CHART_TEMP_DIR, exist_ok=True)
os.environ.setdefault("CHART_OUTPUT_DIR", _CHART_TEMP_DIR)


def compute_floor_pivots(high: float, low: float, close: float) -> list:
    """Classic floor pivots from prior daily H/L/C. Returns list of {label, price} dicts."""
    pp = (high + low + close) / 3.0
    r1 = 2 * pp - low
    s1 = 2 * pp - high
    r2 = pp + (high - low)
    s2 = pp - (high - low)
    return [
        {"label": "R2", "price": r2},
        {"label": "R1", "price": r1},
        {"label": "PP", "price": pp},
        {"label": "S1", "price": s1},
        {"label": "S2", "price": s2},
    ]


def fetch_prior_daily_pivots(pair: str, entry_dt: datetime) -> list | None:
    """Fetch the daily candle for the trading day BEFORE entry_dt and compute pivots.

    Uses 2 daily candles to_time=entry_dt to guarantee we have at least one
    complete prior day regardless of where entry_dt falls in the current day.
    """
    from oanda_client import OandaClient
    try:
        with OandaClient() as client:
            candles = client.get_candles(
                instrument=pair, granularity=GRANULARITY_D,
                count=2, price="M", to_time=entry_dt,
            )
    except Exception as e:
        logger.error("[%s] D1 fetch failed at %s: %s", pair, entry_dt, e)
        return None
    if not candles:
        logger.warning("[%s] D1 returned 0 candles", pair)
        return None
    # Use the most recent COMPLETE daily candle prior to entry.
    # OANDA returns oldest→newest; the last one may be the current (incomplete) day.
    def _parse_oanda_time(s: str) -> datetime:
        s = s.replace("Z", "+00:00")
        if "." in s:
            base, frac = s.split(".", 1)
            tz = "+00:00"
            for sep in ("+", "-"):
                if sep in frac:
                    frac_part, tz_part = frac.split(sep, 1)
                    tz = f"{sep}{tz_part}"
                    frac = frac_part
                    break
            frac = frac[:6]
            s = f"{base}.{frac}{tz}"
        return datetime.fromisoformat(s)

    prior = None
    for c in reversed(candles):
        c_time = _parse_oanda_time(c["time"])
        if c_time.date() < entry_dt.date() and c.get("complete", True):
            prior = c
            break
    if prior is None:
        # Fall back to the oldest of the two
        prior = candles[0]
    mid = prior.get("mid", {})
    h, l, cl = float(mid["h"]), float(mid["l"]), float(mid["c"])
    pivots = compute_floor_pivots(h, l, cl)
    logger.info("[%s] prior D1 H=%.5f L=%.5f C=%.5f → PP=%.5f",
                pair, h, l, cl, pivots[2]["price"])
    return pivots


def regenerate_chart_with_pivots(pair: str, entry_time_iso: str,
                                 output_path: str) -> str | None:
    """Fetch M15 candles + prior D1 pivots, render chart with pivot overlay."""
    import pandas as pd
    from oanda_client import OandaClient

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
        return None
    if not candles or len(candles) < 30:
        return None

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

    pivots = fetch_prior_daily_pivots(pair, entry_dt)
    if pivots is None:
        logger.warning("[%s] No pivots — rendering chart WITHOUT pivot overlay", pair)

    os.environ["CHART_OUTPUT_DIR"] = _CHART_TEMP_DIR
    try:
        import chart_generator as _cg
        if getattr(_cg, "OUTPUT_DIR", None) != _CHART_TEMP_DIR:
            import importlib
            importlib.reload(_cg)
        generated_path = _cg.generate_chart(pair, df, pivot_levels=pivots)
    except Exception as e:
        logger.error("[%s] generate_chart failed: %s", pair, e)
        return None

    if not generated_path or not os.path.exists(generated_path):
        return None

    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copy(generated_path, output_path)
    except Exception as e:
        logger.error("[%s] Copy %s → %s failed: %s", pair, generated_path, output_path, e)
        return None

    size = os.path.getsize(output_path)
    if size < 30_000:
        logger.warning("[%s] Chart suspiciously small: %d bytes", pair, size)
        return None
    logger.info("[%s] pivot chart OK → %s (%d KB)", pair, output_path, size // 1024)
    return output_path


def smoke_test(pair: str = "EUR_USD",
               entry_time_iso: str = "2026-05-07T10:17:52+00:00") -> bool:
    out = "/tmp/smoke_test_pivot_chart.png"
    result = regenerate_chart_with_pivots(pair, entry_time_iso, out)
    if result and os.path.exists(result):
        size = os.path.getsize(result)
        print(f"SMOKE TEST PASS: {pair} {entry_time_iso} → {result} ({size//1024}KB)")
        return True
    print(f"SMOKE TEST FAIL: {pair} {entry_time_iso}")
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    pair = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"
    entry = sys.argv[2] if len(sys.argv) > 2 else "2026-05-07T10:17:52+00:00"
    smoke_test(pair, entry)
