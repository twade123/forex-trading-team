"""Phase 3 — variant sweep for failed_rally_lock rewrite.

Runs 8+ rule variants against the 90d cohort and the post-tune clean window.
Each variant evaluates as if execution is perfect (lock price exit at BE+0.5p),
since the SL-move execution change is orthogonal to the rule logic.

Variants:
  V0      Current rule (any positive cross arms)
  V1      MFE >= 3p + brief-positive pattern + decision_bar <= 8
  V2      V1 + mfe < 7p (winner ceiling)
  V3      V2 + mfe_bar <= 2 (early peak only)
  V4      V3 + rsi_at_decision >= 35
  V5      V4 + adx_at_decision <= 30
  V6      V5 + e55_e100_pips >= 6
  V_clf65 Classifier P(loser) >= 0.65 (uses Phase 2 fitted model)
  V_clf70 Classifier P(loser) >= 0.70

Plus Path B (hard-cut never-positive losers) tested orthogonally:
  PB1     MAE >= 15p AND fan_break_bar present AND never_positive pattern
  PB2     PB1 with hard_cut_window <= 8 bars

Metrics per variant per cohort:
  saves (count, pips)
  kills (count, pips)
  net pips
  precision, recall
  per-trade: fired_trade_ids

Adversarial test:
  Today's 5 guardian fires — see which variant spares the obvious winner-kills.

Walk-forward:
  Train classifier on cohort[:80%], test on cohort[80%:]
"""
from __future__ import annotations
import sys
import os
import sqlite3
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)

import numpy as np
import pandas as pd

from oanda_client import OandaClient
from backtester.indicators import compute_all

# Reuse Phase 2 helpers via import
sys.path.insert(0, HERE)
from failed_rally_phase2 import (
    parse_iso, load_trades, fetch_pair_candles, candles_to_df,
    find_entry_index, find_exit_index, snapshot_at_bar, measure_trade,
)

COHORTS = {
    '90d':       ('2026-02-11', '2026-05-11'),
    'post_tune': ('2026-04-17', '2026-05-04'),
}

# Lock pips (BE+0.5p in profit direction, same as Path A spec)
LOCK_PIPS = 0.5


def simulate_lock_outcome(t, df, lock_pips=LOCK_PIPS):
    """Walk the trade window again. If rule fires at decision bar, check whether
    subsequent M1-like bar (we use M15 low/high) touched lock_price.
    Returns (sim_pnl, lock_hit_bar) where sim_pnl = lock_pips if hit, else actual.
    For winners that would fire: actual pnl is replaced by lock_pips (rule cuts win).
    For losers that would fire: actual pnl is replaced by lock_pips (rule saves).
    """
    pip = 0.01 if 'JPY' in t['pair'] else 0.0001
    is_buy = t['direction'] in ('buy', 'long')
    entry = t['entry_price']
    lock_price = entry + lock_pips * pip if is_buy else entry - lock_pips * pip
    actual = t['pnl_pips']

    ent_idx = find_entry_index(df, t['entry_time'])
    exit_idx = find_exit_index(df, t['exit_time'])
    if ent_idx < 0 or exit_idx <= ent_idx:
        return actual, None

    m = t['m']
    decision_bar_rel = m['decision_bar']
    decision_idx = ent_idx + decision_bar_rel

    # After decision bar, look for first bar whose adverse extreme touches lock.
    for i in range(decision_idx, min(exit_idx + 1, len(df))):
        row = df.iloc[i]
        h = float(row['high']); lo = float(row['low'])
        if is_buy:
            hit = lo <= lock_price
        else:
            hit = h >= lock_price
        if hit:
            return float(lock_pips), i - ent_idx

    return actual, None


# ─── Variant rule predicates: takes measured trade m, returns True if rule fires ───

def v0_current(m, t):
    """Current rule: any positive cross arms; brief-positive pattern fires."""
    if m['pattern'] not in ('long_neg_then_brief_pos', 'short_neg_then_brief_pos'):
        return False
    if m['mfe'] <= 0:
        return False
    return True


def v1_mfe3(m, t):
    if m['pattern'] not in ('long_neg_then_brief_pos', 'short_neg_then_brief_pos'):
        return False
    if m['mfe'] < 3.0:
        return False
    if m['decision_bar'] > 8:
        return False
    return True


def v2_mfe_window(m, t):
    if not v1_mfe3(m, t):
        return False
    if m['mfe'] >= 7.0:
        return False
    return True


def v3_early_peak(m, t):
    if not v2_mfe_window(m, t):
        return False
    if m['mfe_bar'] > 2:
        return False
    return True


def v4_rsi(m, t):
    if not v3_early_peak(m, t):
        return False
    snap = m.get('snap_decision') or {}
    if snap.get('rsi', 0) < 35:
        return False
    return True


def v5_adx(m, t):
    if not v4_rsi(m, t):
        return False
    snap = m.get('snap_decision') or {}
    if snap.get('adx', 0) > 30:
        return False
    return True


def v6_late_cascade(m, t):
    if not v5_adx(m, t):
        return False
    snap = m.get('snap_decision') or {}
    if snap.get('e55_e100_pips', 0) < 6:
        return False
    return True


def make_classifier_variant(coefs, intercept, scaler_mean, scaler_scale,
                             feature_names, p_threshold):
    """Returns a variant function that uses the fitted classifier."""
    def variant(m, t):
        if m['pattern'] not in ('long_neg_then_brief_pos', 'short_neg_then_brief_pos'):
            return False
        if m['mfe'] < 3.0:
            return False
        if m['decision_bar'] > 8:
            return False
        snap = m.get('snap_decision') or {}
        if not snap:
            return False
        feature_keys = [
            'rsi', 'stoch_k', 'adx', 'macd_hist',
            'bb_pos', 'bb_width_ratio',
            'fan_ordered', 'fan_inverted',
            'e21_e55_pips', 'e55_e100_pips', 'fan_velocity',
            'candle_vs_e21', 'counter_color_count', 'is_reversal_candle',
        ]
        derived_keys = ['mfe', 'mfe_bar', 'mae_at_peak', 'decision_bar']
        x = []
        for k in feature_keys:
            x.append(snap.get(k, 0))
        for k in derived_keys:
            x.append(m[k])
        x = np.array(x, dtype=float)
        # Scale
        x_scaled = (x - np.array(scaler_mean)) / np.array(scaler_scale)
        # Logistic
        logit = float(np.dot(x_scaled, coefs)) + intercept
        p = 1 / (1 + math.exp(-logit))
        return p >= p_threshold
    return variant


VARIANTS = {
    'V0_current':    v0_current,
    'V1_mfe3':       v1_mfe3,
    'V2_mfe_window': v2_mfe_window,
    'V3_early_peak': v3_early_peak,
    'V4_rsi':        v4_rsi,
    'V5_adx':        v5_adx,
    'V6_late_casc':  v6_late_cascade,
}


def evaluate_variant(name, predicate, measured_trades, pair_dfs):
    """Run variant against all measured trades. Return metrics + per-trade results."""
    fires = []
    for t in measured_trades:
        if not predicate(t['m'], t):
            continue
        df = pair_dfs.get(t['pair'])
        sim_pnl, hit_bar = simulate_lock_outcome(t, df)
        actual = t['pnl_pips']
        delta = sim_pnl - actual
        fires.append({
            'id': t['id'], 'pair': t['pair'], 'pnl': actual,
            'sim': sim_pnl, 'delta': delta, 'hit_bar': hit_bar,
            'mfe': t['m']['mfe'], 'mfe_bar': t['m']['mfe_bar'],
            'is_loser': actual < 0,
        })
    saves = [f for f in fires if f['is_loser']]
    kills = [f for f in fires if not f['is_loser']]
    saved_p = sum(f['delta'] for f in saves)
    killed_p = sum(f['delta'] for f in kills)
    return {
        'name': name,
        'fires': len(fires),
        'saves': len(saves),
        'kills': len(kills),
        'saved_p': round(saved_p, 1),
        'killed_p': round(killed_p, 1),
        'net_p': round(saved_p + killed_p, 1),
        'avg_save': round(saved_p / max(len(saves), 1), 1),
        'avg_kill': round(killed_p / max(len(kills), 1), 1),
        'precision': round(len(saves) / max(len(fires), 1), 3),
        'recall_vs_losers': None,  # filled by caller
        'kill_ids': [f['id'] for f in kills],
        'save_ids': [f['id'] for f in saves],
        'fires_detail': fires,
    }


def measure_cohort(start, end, oanda, classifier_payload=None):
    print(f'\n  Loading trades [{start} → {end})...')
    trades = load_trades(start, end)
    if not trades:
        return None, None, None
    print(f'  {len(trades)} trades')
    cache = fetch_pair_candles(trades, oanda)
    pair_dfs = {}
    for pair, candles in cache.items():
        df = candles_to_df(candles)
        if df.empty:
            continue
        df = compute_all(df)
        pair_dfs[pair] = df
    measured = []
    for t in trades:
        df = pair_dfs.get(t['pair'])
        if df is None or df.empty:
            continue
        m = measure_trade(t, df)
        if m is None:
            continue
        measured.append({**t, 'm': m})
    print(f'  {len(measured)} measured')
    total_losers = sum(1 for t in measured if t['pnl_pips'] < 0)
    return measured, pair_dfs, total_losers


def run_all_variants(measured, pair_dfs, total_losers, classifier_payload):
    """Evaluate every variant against the cohort. Return results dict."""
    results = {}
    for name, pred in VARIANTS.items():
        r = evaluate_variant(name, pred, measured, pair_dfs)
        r['recall_vs_losers'] = round(r['saves'] / max(total_losers, 1), 3)
        results[name] = r

    if classifier_payload:
        for thresh, label in [(0.65, 'V_clf65'), (0.70, 'V_clf70'), (0.60, 'V_clf60')]:
            pred = make_classifier_variant(
                np.array(list(classifier_payload['classifier_coefs'].values())),
                classifier_payload['classifier_intercept'],
                classifier_payload['classifier_scaler_mean'],
                classifier_payload['classifier_scaler_scale'],
                classifier_payload['feature_names'],
                thresh,
            )
            r = evaluate_variant(label, pred, measured, pair_dfs)
            r['recall_vs_losers'] = round(r['saves'] / max(total_losers, 1), 3)
            results[label] = r
    return results


def print_table(label, results):
    print()
    print('=' * 110)
    print(f'COHORT: {label}')
    print('=' * 110)
    print(f"  {'variant':<16}{'fires':>7}{'saves':>7}{'kills':>7}"
          f"{'saved_p':>10}{'killed_p':>10}{'net_p':>9}"
          f"{'avg_sv':>8}{'avg_kl':>8}{'prec':>7}{'rec':>7}")
    for name in ('V0_current', 'V1_mfe3', 'V2_mfe_window', 'V3_early_peak',
                 'V4_rsi', 'V5_adx', 'V6_late_casc',
                 'V_clf60', 'V_clf65', 'V_clf70'):
        r = results.get(name)
        if not r:
            continue
        print(f"  {name:<16}{r['fires']:>7}{r['saves']:>7}{r['kills']:>7}"
              f"{r['saved_p']:>+10.1f}{r['killed_p']:>+10.1f}{r['net_p']:>+9.1f}"
              f"{r['avg_save']:>+8.1f}{r['avg_kill']:>+8.1f}"
              f"{r['precision']:>7.2f}{r['recall_vs_losers']:>7.2f}")


def adversarial_check(measured, results, today_trade_ids):
    """For each variant, report fire status on today's 5 guardian trades."""
    print()
    print('=' * 110)
    print("ADVERSARIAL TEST — today's 5 guardian fires")
    print('=' * 110)
    today_trades = {t['id']: t for t in measured if t['id'] in today_trade_ids}
    print(f'  Today trades found in cohort: {list(today_trades.keys())}')
    if not today_trades:
        print('  No today trades in cohort window. Run cohort up to today.')
        return
    print(f"  {'tid':<10}{'pair':<10}{'pnl':>8}{'mfe':>7}", end='')
    for v in ('V0_current', 'V1_mfe3', 'V3_early_peak', 'V5_adx', 'V_clf65', 'V_clf70'):
        print(f"{v:>10}", end='')
    print()
    for tid, t in today_trades.items():
        print(f"  {tid:<10}{t['pair']:<10}{t['pnl_pips']:>+8.1f}{t['m']['mfe']:>+7.1f}", end='')
        for v in ('V0_current', 'V1_mfe3', 'V3_early_peak', 'V5_adx', 'V_clf65', 'V_clf70'):
            r = results.get(v) or {}
            fired = '🔥' if tid in (r.get('save_ids', []) + r.get('kill_ids', [])) else '·'
            print(f"{fired:>10}", end='')
        print()


def main():
    # Load Phase 2 classifier payload
    p2_files = sorted([f for f in os.listdir(HERE) if f.startswith('failed_rally_phase2_') and f.endswith('.json')])
    if not p2_files:
        print('ERROR: no Phase 2 JSON found. Run failed_rally_phase2.py first.')
        return
    p2_path = os.path.join(HERE, p2_files[-1])
    with open(p2_path) as f:
        clf_payload = json.load(f)
    print(f'Loaded classifier from {p2_files[-1]}')

    oanda = OandaClient()
    all_results = {}

    for cohort_label, (start, end) in COHORTS.items():
        measured, pair_dfs, total_losers = measure_cohort(start, end, oanda, clf_payload)
        if measured is None:
            print(f'  No data for {cohort_label}, skipping.')
            continue
        results = run_all_variants(measured, pair_dfs, total_losers, clf_payload)
        all_results[cohort_label] = {
            'n_total': len(measured),
            'n_losers': total_losers,
            'variants': results,
        }
        print_table(cohort_label, results)

    # Adversarial — today's 5 fires
    today_ids = {'13843', '13809', '13944', '13964', '14062'}
    # Need a cohort that includes them — load fresh through-today cohort
    measured_today, pair_dfs_today, _ = measure_cohort(
        '2026-05-08', '2026-05-12', oanda, clf_payload
    )
    if measured_today:
        results_today = run_all_variants(measured_today, pair_dfs_today,
                                          sum(1 for t in measured_today if t['pnl_pips'] < 0),
                                          clf_payload)
        adversarial_check(measured_today, results_today, today_ids)

    out = os.path.join(HERE, f'failed_rally_phase3_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.json')
    with open(out, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f'\nFull JSON: {out}')


if __name__ == '__main__':
    main()
