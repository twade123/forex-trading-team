"""oanda_chart_swing_regen.py — Regenerate M15 charts WITH swing-trace overlay.

Detects swing highs and swing lows on the last ~100 M15 bars using a windowed
local-extremum filter, then calls chart_generator.generate_chart() with the
new swing_overlay kwarg so the geometric pattern (W bottoms, M tops, wedges,
trendlines) is drawn as a traced shape over the candles.

Public API:
    regenerate_chart_with_swings(pair, entry_time_iso, output_path) -> str | None

Same shape as oanda_chart_pivot_regen.py — only difference is what gets overlaid.
"""

import logging
import os
import shutil
import sys
from datetime import datetime, timezone

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

logger = logging.getLogger(__name__)

GRANULARITY_M15 = "M15"
CANDLE_COUNT = 250

_CHART_TEMP_DIR = "/tmp/replay_charts_swing"
os.makedirs(_CHART_TEMP_DIR, exist_ok=True)
os.environ.setdefault("CHART_OUTPUT_DIR", _CHART_TEMP_DIR)

# Swing detection window — a bar is a swing high if its high > neighbors within ±WIN bars
# Window 4 = 4 bars left + 4 bars right = swing prominence over a ~2-hour range on M15
SWING_WINDOW = 4
# Only annotate swings within the last LAST_N bars (so the overlay shows the relevant
# recent geometry, not 60+ bars of history clutter)
LAST_N_BARS = 100


def detect_swings(highs, lows, window: int = SWING_WINDOW, last_n: int = LAST_N_BARS):
    """Return list of {bar_idx, price, type} dicts in time order.

    A bar i is a swing HIGH if highs[i] is strictly greater than highs[i-window..i+window]
    (excluding i itself), and similarly for swing LOW with lows.

    Args:
        highs, lows: array-like of float, one per bar.
        window: ±N-bar prominence window. Larger = sparser, more significant swings.
        last_n: only return swings whose bar_idx is within the last N bars of the input.

    Returns:
        Sorted list (by bar_idx) of dicts: {bar_idx: int, price: float, type: 'high'|'low'}.
    """
    n = len(highs)
    if n != len(lows) or n == 0:
        return []
    points = []
    start = max(window, n - last_n)
    end = n - window
    for i in range(start, end):
        h = highs[i]
        if all(h > highs[i - k] for k in range(1, window + 1)) and \
           all(h > highs[i + k] for k in range(1, window + 1)):
            points.append({"bar_idx": i, "price": float(h), "type": "high"})
            continue
        lo = lows[i]
        if all(lo < lows[i - k] for k in range(1, window + 1)) and \
           all(lo < lows[i + k] for k in range(1, window + 1)):
            points.append({"bar_idx": i, "price": float(lo), "type": "low"})
    return points


def regenerate_chart_with_swings(pair: str, entry_time_iso: str,
                                 output_path: str) -> str | None:
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

    swings = detect_swings(df["high"].values, df["low"].values)
    logger.info("[%s] detected %d swings (%d highs, %d lows) over last %d bars",
                pair, len(swings),
                sum(1 for s in swings if s["type"] == "high"),
                sum(1 for s in swings if s["type"] == "low"),
                LAST_N_BARS)

    os.environ["CHART_OUTPUT_DIR"] = _CHART_TEMP_DIR
    try:
        import chart_generator as _cg
        if getattr(_cg, "OUTPUT_DIR", None) != _CHART_TEMP_DIR:
            import importlib
            importlib.reload(_cg)
        generated_path = _cg.generate_chart(pair, df, swing_overlay=swings)
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
    logger.info("[%s] swing chart OK → %s (%d KB, %d swing pts)",
                pair, output_path, size // 1024, len(swings))
    return output_path


def smoke_test(pair: str = "EUR_USD",
               entry_time_iso: str = "2026-05-07T10:17:52+00:00") -> bool:
    out = "/tmp/smoke_test_swing_chart.png"
    result = regenerate_chart_with_swings(pair, entry_time_iso, out)
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
