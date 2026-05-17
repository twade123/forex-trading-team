#!/usr/bin/env python3
"""Fast parameter sweep: focus on high-impact params, skip redundant combos."""

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
from Source.backtester import rule_engine

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("sweep")
logger.setLevel(logging.INFO)

DATA_DIR = TRADING_BOT / "Data"
RESULTS_DIR = TRADING_BOT / "Results"

JPY_PAIRS = ["USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"]

# Focus on top pairs + most promising timeframes
SWEEP_CONFIGS = [
    # (pair, timeframe) — M15 for volume, H1 for quality, H4 for swing
    ("EUR_USD", "M15"), ("EUR_USD", "M30"), ("EUR_USD", "H1"),
    ("GBP_USD", "M15"), ("GBP_USD", "M30"), ("GBP_USD", "H1"),
    ("AUD_JPY", "M15"), ("AUD_JPY", "H1"),
    ("NZD_USD", "M15"), ("NZD_USD", "H1"),
    ("USD_CHF", "M15"), ("USD_CHF", "H1"),
    ("EUR_AUD", "M15"), ("EUR_AUD", "H1"),
    ("AUD_USD", "M15"), ("AUD_USD", "H1"),
    ("USD_CAD", "M15"), ("USD_CAD", "H1"),
    ("USD_JPY", "M15"), ("USD_JPY", "H1"),
    ("EUR_JPY", "M15"),
    ("GBP_JPY", "M15"),
    ("EUR_GBP", "M15"),
]

# Focused grid — 4 × 3 × 2 = 24 configs per pair/tf (vs 144 before)
THRESHOLDS = [20, 30, 40, 50]
RISK_REWARDS = [1.5, 2.0, 3.0]
SL_MULTS = [1.0, 2.0]  # Tight vs wide stops


def ensure_data(instrument: str, granularity: str) -> Path:
    tag = f"{instrument.lower()}_{granularity.lower()}_3yr"
    csv_path = DATA_DIR / f"{tag}.csv"
    if csv_path.exists():
        import os
        if (time.time() - os.path.getmtime(csv_path)) / 3600 < 24:
            return csv_path
    logger.info("Fetching %s %s...", instrument, granularity)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fetch_and_save(instrument=instrument, granularity=granularity,
                          from_time="2023-02-13T00:00:00Z", to_time=now, data_dir=DATA_DIR)


def main():
    logger.info("FAST PARAMETER SWEEP — %d pair/tf combos × %d configs = %d total",
                len(SWEEP_CONFIGS),
                len(THRESHOLDS) * len(RISK_REWARDS) * len(SL_MULTS),
                len(SWEEP_CONFIGS) * len(THRESHOLDS) * len(RISK_REWARDS) * len(SL_MULTS))

    rules = rule_engine.load_rules()
    all_results = []
    total_time = time.time()

    for pair, tf in SWEEP_CONFIGS:
        is_jpy = pair in JPY_PAIRS

        try:
            csv_path = ensure_data(pair, tf)
        except Exception as e:
            logger.error("Failed %s %s: %s", pair, tf, e)
            continue

        # Prepare data once
        bt_prep = Backtester()
        t0 = time.time()
        df_prepared = bt_prep.prepare_data(str(csv_path))
        prep_time = time.time() - t0

        combo_results = []
        for threshold in THRESHOLDS:
            for rr in RISK_REWARDS:
                for sl_mult in SL_MULTS:
                    bt = Backtester(confluence_threshold=threshold, risk_reward=rr, max_positions=2)
                    bt.sl_atr_mult = sl_mult
                    bt.is_jpy = is_jpy
                    bt.rules = rules

                    t0 = time.time()
                    stats = bt.run(df_prepared.copy())
                    run_time = time.time() - t0

                    t = stats.get("total_trades", 0)
                    if t > 0:
                        result = {
                            "pair": pair, "timeframe": tf,
                            "threshold": threshold, "risk_reward": rr,
                            "sl_atr_mult": sl_mult,
                            "trades": t,
                            "win_rate": stats.get("win_rate", 0),
                            "total_pips": stats.get("total_pips", 0),
                            "profit_factor": stats.get("profit_factor", 0),
                            "max_drawdown": stats.get("max_drawdown_pips", 0),
                            "avg_win": stats.get("avg_win_pips", 0),
                            "avg_loss": stats.get("avg_loss_pips", 0),
                            "trades_per_day": round(t / (365 * 3), 2),
                        }
                        all_results.append(result)
                        combo_results.append(result)

        # Quick summary for this pair/tf
        if combo_results:
            best = max(combo_results, key=lambda x: x["profit_factor"] if x["trades"] >= 10 else 0)
            logger.info("%s/%s: %d candles, prep=%.1fs, best PF=%.2f (%d trades, %.0f pips, t=%d rr=%.1f sl=%.1f)",
                       pair, tf, len(df_prepared), prep_time,
                       best["profit_factor"], best["trades"], best["total_pips"],
                       best["threshold"], best["risk_reward"], best["sl_atr_mult"])
        else:
            logger.info("%s/%s: %d candles — no trades generated", pair, tf, len(df_prepared))

    elapsed = time.time() - total_time
    logger.info("Total sweep time: %.0fs", elapsed)

    if not all_results:
        print("No trades generated!")
        return

    # === OUTPUT ===
    viable = [r for r in all_results if r["trades"] >= 15 and r["profit_factor"] > 1.0]
    viable.sort(key=lambda x: x["profit_factor"], reverse=True)

    print(f"\n{'='*130}")
    print(f"TOP 40 CONFIGS (min 15 trades, PF > 1.0) — {len(viable)} viable out of {len(all_results)} with trades")
    print(f"{'='*130}")
    print(f"{'PAIR':<10} {'TF':<4} {'THRESH':>6} {'R:R':>5} {'SL':>4} {'TRADES':>7} {'WIN%':>6} {'PIPS':>10} {'PF':>6} {'DD':>8} {'AVG_W':>7} {'AVG_L':>7} {'T/DAY':>6}")
    print(f"{'-'*130}")

    for r in viable[:40]:
        print(f"{r['pair']:<10} {r['timeframe']:<4} {r['threshold']:>6} {r['risk_reward']:>5.1f} {r['sl_atr_mult']:>4.1f} {r['trades']:>7} {r['win_rate']:>5.1f}% {r['total_pips']:>9.0f} {r['profit_factor']:>6.2f} {r['max_drawdown']:>7.0f} {r['avg_win']:>6.1f} {r['avg_loss']:>6.1f} {r['trades_per_day']:>6.2f}")

    # High frequency profitable
    high_freq = [r for r in all_results if r["trades_per_day"] >= 0.3 and r["profit_factor"] > 1.0 and r["trades"] >= 30]
    high_freq.sort(key=lambda x: x["total_pips"], reverse=True)

    print(f"\n{'='*130}")
    print(f"TOP 20 HIGH-FREQUENCY PROFITABLE (≥0.3/day, PF>1.0, ≥30 trades)")
    print(f"{'='*130}")
    print(f"{'PAIR':<10} {'TF':<4} {'THRESH':>6} {'R:R':>5} {'SL':>4} {'TRADES':>7} {'WIN%':>6} {'PIPS':>10} {'PF':>6} {'DD':>8} {'T/DAY':>6}")
    print(f"{'-'*130}")

    for r in high_freq[:20]:
        print(f"{r['pair']:<10} {r['timeframe']:<4} {r['threshold']:>6} {r['risk_reward']:>5.1f} {r['sl_atr_mult']:>4.1f} {r['trades']:>7} {r['win_rate']:>5.1f}% {r['total_pips']:>9.0f} {r['profit_factor']:>6.2f} {r['max_drawdown']:>7.0f} {r['trades_per_day']:>6.2f}")

    # Portfolio: best per pair (across all TFs)
    print(f"\n{'='*80}")
    print("OPTIMAL PORTFOLIO (best profitable config per pair):")
    print(f"{'='*80}")

    best_per_pair = {}
    for r in sorted(viable, key=lambda x: -x["total_pips"]):
        pair = r["pair"]
        if pair not in best_per_pair:
            best_per_pair[pair] = r

    total_daily_pips = 0
    total_daily_trades = 0
    for pair, r in sorted(best_per_pair.items()):
        daily_pips = r["total_pips"] / (365 * 3)
        total_daily_pips += daily_pips
        total_daily_trades += r["trades_per_day"]
        print(f"  {pair:<10} {r['timeframe']:<4} t={r['threshold']} rr={r['risk_reward']} sl={r['sl_atr_mult']} → PF={r['profit_factor']:.2f}, {r['win_rate']:.0f}% win, {r['trades_per_day']:.2f}/day, ~{daily_pips:.1f} pips/day")

    print(f"\n  COMBINED: {total_daily_trades:.1f} trades/day, ~{total_daily_pips:.0f} pips/day")
    print(f"  At mini lot (0.1, $1/pip): ~${total_daily_pips:.0f}/day")
    print(f"  At $5/pip:                  ~${total_daily_pips * 5:.0f}/day")
    print(f"  At $10/pip:                 ~${total_daily_pips * 10:.0f}/day")

    # Save
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_DIR / "sweep_results.json", "w") as f:
        json.dump({
            "all_results": all_results,
            "viable": viable[:50],
            "high_freq": high_freq[:30],
            "best_per_pair": best_per_pair,
            "metadata": {
                "configs_with_trades": len(all_results),
                "viable_count": len(viable),
                "elapsed_seconds": round(elapsed),
                "run_time": datetime.now(timezone.utc).isoformat(),
            }
        }, f, indent=2, default=str)

    logger.info("Results saved to %s", RESULTS_DIR / "sweep_results.json")


if __name__ == "__main__":
    main()
