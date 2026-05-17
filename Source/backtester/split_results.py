#!/usr/bin/env python3
"""Split V3 sweep results into per-pair files for the data validator.

Reads the massive v3_all_trades.csv in chunks, splits into:
  Results/v3_by_pair/{PAIR}/
    trades.csv              — all trades for this pair
    best_setups.json        — top setup+param+regime combos (quick lookup)
    setup_matrix.csv        — setup × regime × params performance grid

Also creates:
  Results/v3_by_pair/index.json — master index with pair list + global stats

Usage:
    cd ~/jarvis/Trading\ Bot
    source ~/myenv/bin/activate
    python -u -m Source.backtester.split_results
"""

import csv
import gc
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

TRADING_BOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = TRADING_BOT / "Results"
OUTPUT_DIR = RESULTS_DIR / "v3_by_pair"
TRADES_CSV = RESULTS_DIR / "v3_all_trades.csv"
SETUP_SUMMARY = RESULTS_DIR / "v3_setup_summary.csv"

CHUNK_SIZE = 500_000  # rows per chunk


def split_trades():
    """Stream CSV and write per-pair trade files."""
    print("=" * 70)
    print("SPLITTING V3 RESULTS BY PAIR")
    print("=" * 70)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # First pass: count rows per pair (fast scan)
    print("\n📊 Counting trades per pair...", flush=True)
    pair_counts = defaultdict(int)
    total = 0
    with open(TRADES_CSV, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)
        pair_idx = header.index('pair')
        for row in reader:
            pair_counts[row[pair_idx]] += 1
            total += 1
            if total % 1_000_000 == 0:
                print(f"  Scanned {total:,} rows...", flush=True)

    print(f"\n✅ Total trades: {total:,}")
    for pair, count in sorted(pair_counts.items()):
        print(f"  {pair}: {count:,} trades")

    # Second pass: stream and split into per-pair CSVs
    print("\n📁 Writing per-pair trade files...", flush=True)
    pair_writers = {}
    pair_files = {}

    with open(TRADES_CSV, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)
        pair_idx = header.index('pair')

        for pair in pair_counts:
            pair_dir = OUTPUT_DIR / pair
            pair_dir.mkdir(parents=True, exist_ok=True)
            fh = open(pair_dir / "trades.csv", 'w', newline='')
            writer = csv.writer(fh)
            writer.writerow(header)
            pair_writers[pair] = writer
            pair_files[pair] = fh

        written = 0
        for row in reader:
            pair = row[pair_idx]
            pair_writers[pair].writerow(row)
            written += 1
            if written % 1_000_000 == 0:
                print(f"  Written {written:,}/{total:,} ({written*100//total}%)", flush=True)

    for fh in pair_files.values():
        fh.close()

    print(f"✅ Split complete — {len(pair_counts)} pair directories created")
    return list(pair_counts.keys()), header


def build_best_setups(pairs):
    """For each pair, analyze trades and build the quick-lookup best_setups.json."""
    print("\n" + "=" * 70)
    print("BUILDING BEST SETUPS PER PAIR")
    print("=" * 70)

    index = {"pairs": {}, "generated": time.strftime("%Y-%m-%d %H:%M:%S")}

    for pair in sorted(pairs):
        pair_dir = OUTPUT_DIR / pair
        print(f"\n🔍 {pair}...", end=" ", flush=True)

        df = pd.read_csv(pair_dir / "trades.csv", low_memory=False)
        total_trades = len(df)

        # --- Setup Matrix (setup × regime × timeframe) ---
        matrix_rows = []
        for (setup, regime, tf), grp in df.groupby(['setup', 'regime', 'timeframe']):
            n = len(grp)
            if n < 5:
                continue
            wins = (grp['result'] == 'win').sum()
            wr = round(wins / n * 100, 1)

            # Use combined_pips if available, fall back to pips
            if 'combined_pips' in grp.columns:
                pips_col = pd.to_numeric(grp['combined_pips'], errors='coerce')
                pips_col = pips_col.fillna(pd.to_numeric(grp['pips'], errors='coerce').fillna(0))
            else:
                pips_col = pd.to_numeric(grp['pips'], errors='coerce').fillna(0)

            total_pips = round(pips_col.sum(), 1)
            avg_pips = round(pips_col.mean(), 2)

            win_pips = pips_col[pips_col > 0].sum()
            loss_pips = abs(pips_col[pips_col <= 0].sum())
            pf = round(win_pips / max(loss_pips, 0.01), 2)

            # H4 agreement rate
            h4_agrees_rate = None
            if 'h4_agrees' in grp.columns:
                h4_vals = grp['h4_agrees'].astype(str).str.lower()
                h4_true = ((h4_vals == 'true') | (h4_vals == '1')).sum()
                h4_agrees_rate = round(h4_true / n * 100, 1)

            # Best session
            best_session = None
            if 'session' in grp.columns:
                sess_wr = {}
                for sess, sg in grp.groupby('session'):
                    if len(sg) >= 3:
                        sess_wr[sess] = round((sg['result'] == 'win').sum() / len(sg) * 100, 1)
                if sess_wr:
                    best_session = max(sess_wr, key=sess_wr.get)

            matrix_rows.append({
                'setup': setup, 'regime': regime, 'timeframe': tf,
                'trades': n, 'wins': wins, 'win_rate': wr,
                'total_pips': total_pips, 'avg_pips': avg_pips,
                'profit_factor': pf,
                'h4_agrees_pct': h4_agrees_rate,
                'best_session': best_session,
            })

        matrix_df = pd.DataFrame(matrix_rows)
        matrix_df.to_csv(pair_dir / "setup_matrix.csv", index=False)

        # --- Best Setups by Regime ---
        best = {}
        viable = [r for r in matrix_rows if r['trades'] >= 10 and r['profit_factor'] > 1.0]
        for regime in ['strong_trend', 'ranging', 'exhaustion', 'squeeze', 'high_volatility']:
            regime_setups = [r for r in viable if r['regime'] == regime]
            regime_setups.sort(key=lambda x: (-x['profit_factor'], -x['total_pips']))
            best[regime] = regime_setups[:10]  # top 10 per regime

        # Overall best (any regime)
        viable.sort(key=lambda x: (-x['total_pips']))
        best['overall_by_pips'] = viable[:10]
        viable.sort(key=lambda x: (-x['profit_factor']))
        best['overall_by_pf'] = viable[:10]

        # Quick stats
        pair_stats = {
            'total_trades': total_trades,
            'total_viable_combos': len(viable),
            'regimes_found': list(df['regime'].unique()) if 'regime' in df.columns else [],
            'timeframes': list(df['timeframe'].unique()),
            'setups_tested': list(df['setup'].unique()),
        }

        best_data = {'pair': pair, 'stats': pair_stats, 'best_setups': best}

        with open(pair_dir / "best_setups.json", 'w') as f:
            json.dump(best_data, f, indent=2, default=str)

        n_viable = len(viable)
        print(f"{total_trades:,} trades, {len(matrix_rows)} combos, {n_viable} viable", flush=True)

        index['pairs'][pair] = pair_stats

        del df
        gc.collect()

    # --- Master Index ---
    total_all = sum(p['total_trades'] for p in index['pairs'].values())
    index['total_trades'] = total_all
    index['total_pairs'] = len(index['pairs'])

    with open(OUTPUT_DIR / "index.json", 'w') as f:
        json.dump(index, f, indent=2, default=str)

    print(f"\n✅ Index saved to {OUTPUT_DIR / 'index.json'}")
    return index


def print_summary(index):
    """Print the key findings."""
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total trades: {index['total_trades']:,}")
    print(f"Pairs: {index['total_pairs']}")
    print()

    # Load best setups and show top performers
    print("🏆 TOP SETUPS PER PAIR (by profit factor, ≥10 trades):")
    print(f"{'PAIR':<12} {'REGIME':<16} {'SETUP':<22} {'TF':<4} {'PF':>6} {'WR%':>5} {'PIPS':>8} {'TRADES':>6}")
    print("-" * 80)

    for pair in sorted(index['pairs']):
        pair_dir = OUTPUT_DIR / pair
        with open(pair_dir / "best_setups.json") as f:
            data = json.load(f)
        top = data['best_setups'].get('overall_by_pf', [])
        if top:
            r = top[0]
            print(f"{pair:<12} {r['regime']:<16} {r['setup']:<22} {r['timeframe']:<4} "
                  f"{r['profit_factor']:>6.2f} {r['win_rate']:>4.1f}% {r['total_pips']:>7.0f} {r['trades']:>6}")


def main():
    t0 = time.time()

    if not TRADES_CSV.exists():
        print(f"❌ {TRADES_CSV} not found!")
        sys.exit(1)

    pairs, header = split_trades()
    index = build_best_setups(pairs)
    print_summary(index)

    elapsed = time.time() - t0
    print(f"\n⏱️  Total time: {elapsed/60:.1f} minutes")
    print(f"📁 Output: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
