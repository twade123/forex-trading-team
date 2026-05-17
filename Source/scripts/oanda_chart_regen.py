"""oanda_chart_regen.py — Regenerate historical M15 charts from OANDA candles.

Fetches 250 M15 candles ending at a specific historical timestamp and renders
them through the existing chart_generator, bypassing the need for saved PNGs.

Public API:
    regenerate_chart_at(pair, entry_time_iso, output_path) -> str | None

Used by replay_60trade_oanda.py for the Task 2d large-cohort replay.
"""

import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

logger = logging.getLogger(__name__)

GRANULARITY = "M15"
CANDLE_COUNT = 250  # 250 M15 candles = ~62.5h of context; first ~50 used for EMA warm-up

# Shared temp dir for chart_generator output; we copy to trade-specific paths
_CHART_TEMP_DIR = "/tmp/replay_charts_regen"
os.makedirs(_CHART_TEMP_DIR, exist_ok=True)

# Patch chart_generator to write to our temp dir
os.environ.setdefault("CHART_OUTPUT_DIR", _CHART_TEMP_DIR)


def regenerate_chart_at(pair: str, entry_time_iso: str, output_path: str) -> str | None:
    """Fetch OANDA historical M15 candles ending at entry_time and render to output_path.

    Args:
        pair: OANDA instrument name (e.g. "EUR_USD").
        entry_time_iso: ISO8601 string for the trade entry time (UTC).
            Candles ending at or just before this time are fetched.
        output_path: Destination path for the generated PNG.

    Returns:
        output_path on success, None on failure.
    """
    try:
        import pandas as pd
    except ImportError:
        logger.error("pandas not installed — needed for chart regen")
        return None

    try:
        from oanda_client import OandaClient
    except ImportError:
        logger.error("oanda_client not importable from SOURCE_DIR=%s", SOURCE_DIR)
        return None

    # Parse entry time
    try:
        ts = entry_time_iso.replace("Z", "+00:00")
        entry_dt = datetime.fromisoformat(ts)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
    except Exception as e:
        logger.error("Cannot parse entry_time_iso=%r: %s", entry_time_iso, e)
        return None

    # Fetch candles from OANDA
    try:
        with OandaClient() as client:
            candles = client.get_candles(
                instrument=pair,
                granularity=GRANULARITY,
                count=CANDLE_COUNT,
                price="M",
                to_time=entry_dt,
            )
    except Exception as e:
        logger.error("[%s] OANDA fetch failed for %s at %s: %s", pair, pair, entry_time_iso, e)
        return None

    if not candles:
        logger.warning("[%s] OANDA returned 0 candles for %s at %s", pair, pair, entry_time_iso)
        return None

    # Parse into DataFrame
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

    if len(rows) < 30:
        logger.warning("[%s] Too few rows (%d) after parsing candles", pair, len(rows))
        return None

    df = pd.DataFrame(rows)

    # Ensure output dir for chart_generator uses our temp dir
    os.environ["CHART_OUTPUT_DIR"] = _CHART_TEMP_DIR

    try:
        # Import must come AFTER os.environ is set — chart_generator reads it at module level,
        # but OUTPUT_DIR is computed at import time. Use importlib to reload if needed.
        try:
            import chart_generator as _cg
            if _cg.OUTPUT_DIR != _CHART_TEMP_DIR:
                import importlib
                importlib.reload(_cg)
        except ImportError:
            import importlib
            import chart_generator as _cg

        generated_path = _cg.generate_chart(pair, df)
    except Exception as e:
        logger.error("[%s] chart_generator.generate_chart failed: %s", pair, e)
        return None

    if not generated_path or not os.path.exists(generated_path):
        logger.error("[%s] generate_chart returned %r but file doesn't exist", pair, generated_path)
        return None

    # Copy from the shared name (USD_CAD_M15_chart.png) to trade-specific output_path
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copy(generated_path, output_path)
    except Exception as e:
        logger.error("[%s] Copy %s → %s failed: %s", pair, generated_path, output_path, e)
        return None

    file_size = os.path.getsize(output_path)
    if file_size < 30_000:
        logger.warning("[%s] Generated chart is suspiciously small: %d bytes", pair, file_size)
        return None

    logger.info("[%s] chart regen OK → %s (%d KB)", pair, output_path, file_size // 1024)
    return output_path


def smoke_test(pair: str = "EUR_USD", entry_time_iso: str = "2026-05-07T10:17:52+00:00") -> bool:
    """Quick sanity check — fetch 1 chart and confirm size > 50KB."""
    out = "/tmp/smoke_test_chart.png"
    result = regenerate_chart_at(pair, entry_time_iso, out)
    if result and os.path.exists(result):
        size = os.path.getsize(result)
        print(f"SMOKE TEST PASS: {pair} {entry_time_iso} → {result} ({size//1024}KB)")
        return True
    else:
        print(f"SMOKE TEST FAIL: {pair} {entry_time_iso} → returned {result}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import sys
    pair = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"
    entry = sys.argv[2] if len(sys.argv) > 2 else "2026-05-07T10:17:52+00:00"
    smoke_test(pair, entry)
