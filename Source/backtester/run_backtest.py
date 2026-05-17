#!/usr/bin/env python3
"""Runner: fetch data for multiple pairs, run backtest, save results."""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TRADING_BOT = Path(__file__).resolve().parent.parent.parent
JARVIS_ROOT = TRADING_BOT.parent
sys.path.insert(0, str(JARVIS_ROOT))
sys.path.insert(0, str(TRADING_BOT))

from Source.backtester.data_fetcher import fetch_and_save
from Source.backtester.backtester import Backtester

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("backtest.runner")

DATA_DIR = TRADING_BOT / "Data"
RESULTS_DIR = TRADING_BOT / "Results"

# Major pairs + key crosses — liquid, tight spreads
INSTRUMENTS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF",
    "AUD_USD", "NZD_USD", "USD_CAD",
    "EUR_GBP", "EUR_JPY", "GBP_JPY",
    "EUR_AUD", "EUR_CHF", "AUD_JPY",
]


def ensure_data(instrument: str) -> Path:
    """Fetch data for one instrument if not cached/fresh."""
    csv_name = f"{instrument.lower()}_h1_3yr.csv"
    csv_path = DATA_DIR / csv_name

    if csv_path.exists():
        import os
        age_hours = (time.time() - os.path.getmtime(csv_path)) / 3600
        if age_hours < 24:
            return csv_path

    logger.info("Fetching 3yr H1 data for %s...", instrument)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fetch_and_save(
        instrument=instrument,
        granularity="H1",
        from_time="2023-02-13T00:00:00Z",
        to_time=now,
        data_dir=DATA_DIR,
    )


def run_single(csv_path: Path, threshold: int) -> dict:
    bt = Backtester(confluence_threshold=threshold)
    df = bt.prepare_data(str(csv_path))
    return bt.run(df)


def main():
    logger.info("=" * 60)
    logger.info("MULTI-PAIR OANDA H1 BACKTESTER")
    logger.info("=" * 60)

    thresholds = [20, 30, 40, 50, 60]
    all_results = {}

    for instrument in INSTRUMENTS:
        logger.info("\n>>> Fetching data for %s", instrument)
        try:
            csv_path = ensure_data(instrument)
        except Exception as e:
            logger.error("Failed to fetch %s: %s", instrument, e)
            continue

        for threshold in thresholds:
            key = f"{instrument}_t{threshold}"
            try:
                stats = run_single(csv_path, threshold)
                stats["instrument"] = instrument
                all_results[key] = stats
            except Exception as e:
                logger.error("Backtest failed %s t=%d: %s", instrument, threshold, e)

    # === Summary ===
    print(f"\n{'='*80}")
    print(f"{'INSTRUMENT':<12} {'THRESH':>6} {'TRADES':>7} {'WIN%':>6} {'PIPS':>8} {'PF':>6} {'MAXDD':>7} {'AVG_W':>7} {'AVG_L':>7}")
    print(f"{'='*80}")

    best_by_pair = {}

    for key in sorted(all_results.keys()):
        r = all_results[key]
        t = r.get("total_trades", 0)
        inst = r.get("instrument", "?")
        # Extract threshold from key
        thresh = key.split("_t")[-1]

        if t > 0:
            print(f"{inst:<12} {thresh:>6} {t:>7} {r['win_rate']:>5.1f}% {r['total_pips']:>7.1f} {r['profit_factor']:>6.2f} {r['max_drawdown_pips']:>6.1f} {r.get('avg_win_pips',0):>6.1f} {r.get('avg_loss_pips',0):>6.1f}")

            # Track best threshold per pair (by profit factor, min 10 trades)
            if t >= 10:
                pf = r.get("profit_factor", 0)
                if inst not in best_by_pair or pf > best_by_pair[inst]["pf"]:
                    best_by_pair[inst] = {"pf": pf, "threshold": thresh, "trades": t, "pips": r["total_pips"], "win_rate": r["win_rate"]}

    # Best per pair
    print(f"\n{'='*60}")
    print("OPTIMAL THRESHOLD PER PAIR (min 10 trades):")
    print(f"{'='*60}")
    total_daily_trades = 0
    for inst, info in sorted(best_by_pair.items(), key=lambda x: -x[1]["pf"]):
        daily = info["trades"] / (365 * 3)  # rough daily average
        total_daily_trades += daily
        print(f"  {inst}: threshold={info['threshold']}, PF={info['pf']:.2f}, {info['trades']} trades ({daily:.1f}/day), {info['win_rate']}% win, {info['pips']:.0f} pips")

    print(f"\n  Combined estimated trades/day: {total_daily_trades:.1f}")

    # Save results (without trade logs)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    save_results = {}
    for key, stats in all_results.items():
        save_results[key] = {k: v for k, v in stats.items() if k != "trade_log"}

    save_results["metadata"] = {
        "instruments": INSTRUMENTS,
        "timeframe": "H1",
        "data_range": "2023-02-13 to 2026-02-13",
        "run_time": datetime.now(timezone.utc).isoformat(),
        "best_by_pair": best_by_pair,
    }

    results_path = RESULTS_DIR / "backtest_multi_pair.json"
    with open(results_path, "w") as f:
        json.dump(save_results, f, indent=2, default=str)

    logger.info("Results saved to %s", results_path)


if __name__ == "__main__":
    main()
