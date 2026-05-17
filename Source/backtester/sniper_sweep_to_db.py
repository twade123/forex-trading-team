#!/usr/bin/env python3
"""Run master_sweep sniper-only configs and store results into backtest_setup_performance DB.

Usage:
    cd ~/jarvis/Trading\ Bot
    source ~/myenv/bin/activate
    python -m Source.backtester.sniper_sweep_to_db [--pair EUR_USD] [--tf H1]
"""

import argparse
import gc
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
os.environ['PYTHONUNBUFFERED'] = '1'

import numpy as np
import pandas as pd

TRADING_BOT = Path(__file__).resolve().parent.parent.parent
JARVIS_ROOT = TRADING_BOT.parent
sys.path.insert(0, str(JARVIS_ROOT))
sys.path.insert(0, str(TRADING_BOT))

from Source.backtester import indicators, divergence, rule_engine
from Source.backtester.candle_patterns import detect_all_patterns
from Source.backtester.sniper_v4 import add_enhanced_indicators, score_v4, TF_PARAMS
from Source.backtester.master_sweep import (
    load_and_prepare, precompute_signals, run_backtest_fast, _compute_stats,
    ALL_PAIRS, JPY_PAIRS, SNIPER_THRESHOLDS, SNIPER_TP_ATR,
    get_data_path,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
logger = logging.getLogger("sniper_sweep_db")
logger.setLevel(logging.INFO)

DB_PATH = JARVIS_ROOT / "Database" / "v2/trading_forex.db"
DATA_DIR = TRADING_BOT / "Data"

# Sniper-only parameter grid
def generate_sniper_configs():
    configs = []
    # Sniper with R:R 
    for threshold, rr, sl, gate in product(
        SNIPER_THRESHOLDS,            # [8, 10, 12, 14, 16]
        [1.5, 2.0, 2.5, 3.0],        # risk_reward
        [1.0, 1.5, 2.0, 2.5],        # sl_atr_mult
        [False, True],                # sell_candle_gate
    ):
        configs.append({
            "engine": "sniper",
            "threshold": threshold,
            "risk_reward": rr,
            "sl_atr_mult": sl,
            "sell_candle_gate": gate,
            "tp_atr_frac": None,
        })
    # Sniper with TP as ATR fraction
    for threshold, tp_atr, sl, gate in product(
        SNIPER_THRESHOLDS,
        [0.3, 0.5, 0.8],
        [1.5, 2.0, 2.5],
        [False, True],
    ):
        configs.append({
            "engine": "sniper",
            "threshold": threshold,
            "risk_reward": 0,
            "sl_atr_mult": sl,
            "sell_candle_gate": gate,
            "tp_atr_frac": tp_atr,
        })
    return configs


def setup_name(cfg):
    """Generate setup name like SNP_t12_rr2.0_sl2.5 or SNP_t12_tpA0.5_sl2.5"""
    t = cfg["threshold"]
    sl = cfg["sl_atr_mult"]
    gate = "_gate" if cfg["sell_candle_gate"] else ""
    if cfg.get("tp_atr_frac") is not None:
        return f"SNP_t{t}_tpA{cfg['tp_atr_frac']}_sl{sl}{gate}"
    else:
        return f"SNP_t{t}_rr{cfg['risk_reward']}_sl{sl}{gate}"


def detect_regime_from_signals(signals, idx):
    """Simple regime detection from pre-computed data."""
    # Use ADX-like heuristic from price action
    # Since we don't have regime in signals, default to "mixed"
    return "mixed"


def store_to_db(results, pair, timeframe):
    """Insert/replace sniper results into backtest_setup_performance."""
    if not results:
        return 0
    
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    cursor = conn.cursor()
    inserted = 0
    
    for r in results:
        if r.get("total_trades", 0) < 5:
            continue
        
        setup = setup_name(r)
        regime = "mixed"  # Sniper doesn't filter by regime — one row per setup/pair/tf
        
        cursor.execute("""
            INSERT OR REPLACE INTO backtest_setup_performance 
            (setup, pair, timeframe, regime,
             trade_count, win_count, win_rate, total_pips, avg_pips,
             profit_factor, avg_risk_reward, max_favorable, max_adverse,
             avg_hold_time, h4_agrees_count, h4_agrees_win_rate,
             best_session, best_session_win_rate)
            VALUES (?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?)
        """, (
            setup, pair, timeframe, regime,
            r["total_trades"], r["total_wins"], r["win_rate"],
            r["total_pips"], round(r["total_pips"] / max(r["total_trades"], 1), 1),
            r["profit_factor"], r.get("risk_reward", 0),
            r.get("avg_win", 0),   # max_favorable → avg_win as proxy
            r.get("avg_loss", 0),  # max_adverse → avg_loss as proxy
            0,   # avg_hold_time — not tracked in fast backtest
            0,   # h4_agrees_count — not tracked
            0.0, # h4_agrees_win_rate
            "all", 0.0,  # best_session — sniper doesn't filter by session
        ))
        inserted += 1
    
    conn.commit()
    conn.close()
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Sniper V4 Sweep → DB")
    parser.add_argument("--pair", type=str, help="Single pair (e.g. EUR_USD)")
    parser.add_argument("--tf", type=str, help="Single timeframe (e.g. H1)")
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else ALL_PAIRS
    timeframes = [args.tf] if args.tf else ["H1", "H4"]  # Focus on H1 + H4

    configs = generate_sniper_configs()
    rules = rule_engine.load_rules()

    # Find available data
    pair_tf_combos = []
    for pair in pairs:
        for tf in timeframes:
            if get_data_path(pair, tf).exists():
                pair_tf_combos.append((pair, tf))

    total_runs = len(pair_tf_combos) * len(configs)
    logger.info("=" * 70)
    logger.info("SNIPER V4 SWEEP → DB")
    logger.info("  Pairs: %d | TFs: %d | Combos: %d | Configs: %d | Total: %d",
                len(pairs), len(timeframes), len(pair_tf_combos), len(configs), total_runs)
    logger.info("  DB: %s", DB_PATH)
    logger.info("=" * 70)

    total_inserted = 0
    completed = 0
    start_time = time.time()

    for combo_idx, (pair, tf) in enumerate(pair_tf_combos):
        csv_path = get_data_path(pair, tf)
        is_jpy = pair in JPY_PAIRS

        logger.info("\n[%d/%d] %s/%s ...", combo_idx + 1, len(pair_tf_combos), pair, tf)
        t0 = time.time()

        try:
            df = load_and_prepare(str(csv_path), tf)
        except Exception as e:
            logger.error("  Failed: %s", e)
            completed += len(configs)
            continue

        logger.info("  %d candles in %.1fs. Pre-computing signals...", len(df), time.time() - t0)
        
        try:
            signals = precompute_signals(df, rules, tf)
        except Exception as e:
            logger.error("  Signal precompute failed: %s", e)
            completed += len(configs)
            del df; gc.collect()
            continue

        combo_results = []
        best_pf = 0
        best_cfg = None

        for cfg_idx, cfg in enumerate(configs):
            try:
                stats = run_backtest_fast(signals, cfg, is_jpy)
            except Exception:
                completed += 1
                continue

            completed += 1
            if stats.get("total_trades", 0) > 0:
                stats.update(cfg)
                stats["pair"] = pair
                stats["timeframe"] = tf
                combo_results.append(stats)

                if stats["total_trades"] >= 10 and stats["profit_factor"] > best_pf:
                    best_pf = stats["profit_factor"]
                    best_cfg = stats

            if (cfg_idx + 1) % 100 == 0:
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (total_runs - completed) / rate if rate > 0 else 0
                print(f"\r  [{cfg_idx+1}/{len(configs)}] {completed}/{total_runs} | "
                      f"{rate:.0f}/sec | ETA {eta/60:.0f}m", end="", flush=True)

        print()
        combo_time = time.time() - t0

        # Store to DB
        n_inserted = store_to_db(combo_results, pair, tf)
        total_inserted += n_inserted

        if best_cfg:
            logger.info("  ✓ %s/%s %.0fs — %d stored, best PF=%.2f (%s, %d trades, %.0f%% win)",
                        pair, tf, combo_time, n_inserted, best_pf,
                        setup_name(best_cfg), best_cfg["total_trades"], best_cfg["win_rate"])
        else:
            logger.info("  ✗ %s/%s %.0fs — no viable trades", pair, tf, combo_time)

        del df, signals
        gc.collect()

    total_time = time.time() - start_time
    logger.info("\n" + "=" * 70)
    logger.info("DONE — %.1f minutes, %d sniper setups stored to DB", total_time / 60, total_inserted)
    logger.info("=" * 70)

    # Verify
    conn = sqlite3.connect(str(DB_PATH))
    count = conn.execute("SELECT COUNT(*) FROM backtest_setup_performance WHERE setup LIKE 'SNP_%'").fetchone()[0]
    sample = conn.execute("""
        SELECT setup, pair, timeframe, trade_count, win_rate, profit_factor 
        FROM backtest_setup_performance 
        WHERE setup LIKE 'SNP_%' AND profit_factor > 1.0
        ORDER BY profit_factor DESC LIMIT 10
    """).fetchall()
    conn.close()
    
    logger.info("\nTotal SNP setups in DB: %d", count)
    logger.info("Top 10 by profit factor:")
    for s in sample:
        logger.info("  %s %s/%s — %d trades, %.1f%% win, PF=%.2f", *s)


if __name__ == "__main__":
    main()
