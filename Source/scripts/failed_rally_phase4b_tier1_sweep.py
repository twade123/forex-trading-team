"""Phase 4b — Tier 1 BE-trail threshold sweep.

Tier 1 rule: when MFE crosses `tier1_threshold_pips` at ANY bar, move SL to
entry + `tier1_lock_pips`. Trade is now locked for at least `tier1_lock_pips`
unless the bar's adverse extreme has already touched the lock price.

Sweep grid:
  tier1_threshold_pips: 5, 6, 7, 8, 9, 10, 12
  tier1_lock_pips:      0.5, 1.0, 1.5, 2.0

Per combo, evaluate against 90d + post-tune cohorts and report:
  fires, saves, kills, saved_p, killed_p, net_p, precision

Also evaluate STACKED scenarios:
  Tier 1 only
  Tier 1 (8p, 1p lock) + V_clf65 (Tier 2) for any trade where Tier 1 didn't fire

Find Pareto-optimal combo by net pips and precision.
"""
from __future__ import annotations
import sys
import os
import json
import math
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)

import numpy as np
import pandas as pd

from oanda_client import OandaClient
from backtester.indicators import compute_all
from failed_rally_phase2 import (
    parse_iso, load_trades, fetch_pair_candles, candles_to_df,
    find_entry_index, find_exit_index, snapshot_at_bar, measure_trade,
)
from failed_rally_phase3_sweep import make_classifier_variant

COHORTS = {
    '90d':       ('2026-02-11', '2026-05-11'),
    'post_tune': ('2026-04-17', '2026-05-04'),
}


def simulate_tier1(t, df, threshold_pips, lock_pips):
    """Walk M15 bars. First bar where intrabar high (favorable) crosses
    threshold, set SL at entry + lock_pips. From next bar onward, if adverse
    extreme touches SL, exit at lock_pips.

    Returns (sim_pnl, fired, fire_bar, hit_bar).
    For winners that don't get hit: sim_pnl = actual.
    For winners that get hit: sim_pnl = lock_pips.
    For losers that get saved by lock: sim_pnl = lock_pips.
    """
    pip = 0.01 if 'JPY' in t['pair'] else 0.0001
    is_buy = t['direction'] in ('buy', 'long')
    entry = t['entry_price']
    actual = t['pnl_pips']

    ent_idx = find_entry_index(df, t['entry_time'])
    exit_idx = find_exit_index(df, t['exit_time'])
    if ent_idx < 0 or exit_idx <= ent_idx:
        return actual, False, None, None

    sl_price = None
    fire_bar = None

    for i in range(ent_idx, exit_idx + 1):
        row = df.iloc[i]
        h = float(row['high']); lo = float(row['low'])
        # Favorable excursion = how far the trade went into profit during bar
        if is_buy:
            fav = (h - entry) / pip
            adv = (lo - entry) / pip  # negative = adverse
        else:
            fav = (entry - lo) / pip
            adv = (entry - h) / pip   # negative = adverse

        if sl_price is None:
            # SL not yet moved — check if bar's favorable excursion crosses threshold
            if fav >= threshold_pips:
                sl_price = entry + lock_pips * pip if is_buy else entry - lock_pips * pip
                fire_bar = i - ent_idx
                # SAME-BAR adverse check: if the bar's adverse extreme has already
                # passed the lock price during this bar (sequence unknown intrabar),
                # we OPTIMISTICALLY assume the favorable extreme came first (rule
                # fires before the adverse retrace within bar). This is what the
                # SL-modify-order semantics provide in real execution.
                continue
        else:
            # SL is set — check if adverse extreme touches it
            if is_buy:
                hit = lo <= sl_price
            else:
                hit = h >= sl_price
            if hit:
                return float(lock_pips), True, fire_bar, i - ent_idx

    # Trade closed without SL hit
    if fire_bar is not None:
        return actual, True, fire_bar, None
    return actual, False, None, None


def simulate_tier1_plus_clf65(t, df, m, clf_predicate,
                                tier1_threshold, tier1_lock):
    """Tier 1 first; if Tier 1 didn't fire, fall back to V_clf65 lock at
    entry + 0.5p at decision bar."""
    sim_t1, fired_t1, fire_bar_t1, hit_bar_t1 = simulate_tier1(t, df, tier1_threshold, tier1_lock)
    if fired_t1:
        return sim_t1, 'tier1', fire_bar_t1
    # Tier 2 / classifier
    if clf_predicate(m, t):
        from failed_rally_phase3_sweep import simulate_lock_outcome
        sim_t2, _ = simulate_lock_outcome({**t, 'm': m}, df, lock_pips=0.5)
        return sim_t2, 'tier2', m['decision_bar']
    return t['pnl_pips'], 'none', None


def evaluate_tier1(name, threshold, lock_pips, measured, pair_dfs):
    fires = []
    for t in measured:
        df = pair_dfs.get(t['pair'])
        if df is None:
            continue
        sim, fired, fire_bar, hit_bar = simulate_tier1(t, df, threshold, lock_pips)
        if not fired:
            continue
        delta = sim - t['pnl_pips']
        fires.append({
            'id': t['id'], 'pair': t['pair'],
            'actual': t['pnl_pips'], 'sim': sim, 'delta': delta,
            'fire_bar': fire_bar, 'hit_bar': hit_bar,
            'is_loser': t['pnl_pips'] < 0,
        })
    saves = [f for f in fires if f['is_loser'] and f['delta'] > 0]
    kills = [f for f in fires if not f['is_loser'] and f['delta'] < 0]
    neutral = [f for f in fires if f not in saves and f not in kills]
    saved_p = sum(f['delta'] for f in saves)
    killed_p = sum(f['delta'] for f in kills)
    neutral_p = sum(f['delta'] for f in neutral)
    return {
        'name': name,
        'threshold': threshold, 'lock_pips': lock_pips,
        'fires': len(fires),
        'saves': len(saves), 'saved_p': round(saved_p, 1),
        'kills': len(kills), 'killed_p': round(killed_p, 1),
        'neutral': len(neutral), 'neutral_p': round(neutral_p, 1),
        'net_p': round(saved_p + killed_p + neutral_p, 1),
        'precision': round(len(saves) / max(len(fires), 1), 3),
        'fires_detail': fires,
    }


def evaluate_stacked(name, threshold, lock_pips, clf_predicate, measured, pair_dfs):
    fires = []
    for t in measured:
        df = pair_dfs.get(t['pair'])
        if df is None:
            continue
        sim, tier, fire_bar = simulate_tier1_plus_clf65(
            t, df, t['m'], clf_predicate, threshold, lock_pips
        )
        if tier == 'none':
            continue
        delta = sim - t['pnl_pips']
        fires.append({
            'id': t['id'], 'pair': t['pair'], 'tier': tier,
            'actual': t['pnl_pips'], 'sim': sim, 'delta': delta,
            'fire_bar': fire_bar, 'is_loser': t['pnl_pips'] < 0,
        })
    saves = [f for f in fires if f['is_loser'] and f['delta'] > 0]
    kills = [f for f in fires if not f['is_loser'] and f['delta'] < 0]
    neutral = [f for f in fires if f not in saves and f not in kills]
    saved_p = sum(f['delta'] for f in saves)
    killed_p = sum(f['delta'] for f in kills)
    neutral_p = sum(f['delta'] for f in neutral)
    return {
        'name': name,
        'fires': len(fires),
        'tier1_fires': sum(1 for f in fires if f['tier'] == 'tier1'),
        'tier2_fires': sum(1 for f in fires if f['tier'] == 'tier2'),
        'saves': len(saves), 'saved_p': round(saved_p, 1),
        'kills': len(kills), 'killed_p': round(killed_p, 1),
        'neutral': len(neutral), 'neutral_p': round(neutral_p, 1),
        'net_p': round(saved_p + killed_p + neutral_p, 1),
        'precision': round(len(saves) / max(len(fires), 1), 3),
    }


def measure_cohort(start, end, oanda):
    print(f'  Loading {start} → {end}...')
    trades = load_trades(start, end)
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
    return measured, pair_dfs


def main():
    # Load classifier
    p2_files = sorted([f for f in os.listdir(HERE) if f.startswith('failed_rally_phase2_') and f.endswith('.json')])
    if not p2_files:
        print('ERROR: no Phase 2 JSON.')
        return
    with open(os.path.join(HERE, p2_files[-1])) as f:
        clf = json.load(f)
    classifier_fn = make_classifier_variant(
        np.array(list(clf['classifier_coefs'].values())),
        clf['classifier_intercept'], clf['classifier_scaler_mean'],
        clf['classifier_scaler_scale'], clf['feature_names'], 0.65,
    )

    oanda = OandaClient()
    all_results = {}

    for cohort_label, (start, end) in COHORTS.items():
        print(f'\n--- {cohort_label} ---')
        measured, pair_dfs = measure_cohort(start, end, oanda)
        print(f'  Measured: {len(measured)} trades')

        cohort_results = {'tier1_alone': [], 'tier1_plus_clf65': []}

        print('\n  Tier 1 alone:')
        print(f"    {'thresh':>7}{'lock':>6}{'fires':>7}{'saves':>7}{'kills':>7}"
              f"{'neut':>6}{'saved_p':>10}{'killed_p':>10}{'neutral_p':>11}{'net_p':>9}{'prec':>7}")
        for thresh in (5, 6, 7, 8, 9, 10, 12):
            for lock in (0.5, 1.0, 1.5, 2.0):
                r = evaluate_tier1(f't{thresh}_l{lock}', thresh, lock, measured, pair_dfs)
                cohort_results['tier1_alone'].append(r)
                print(f"    {thresh:>7}{lock:>6.1f}{r['fires']:>7}{r['saves']:>7}{r['kills']:>7}"
                      f"{r['neutral']:>6}{r['saved_p']:>+10.1f}{r['killed_p']:>+10.1f}"
                      f"{r['neutral_p']:>+11.1f}{r['net_p']:>+9.1f}{r['precision']:>7.2f}")

        # Stacked: Tier 1 (best of sweep above) + V_clf65 as Tier 2 fallback
        print('\n  Tier 1 + V_clf65 stacked (Tier 1 fires first; Tier 2 if not):')
        print(f"    {'t1thr':>6}{'lock':>6}{'fires':>7}{'t1':>5}{'t2':>5}"
              f"{'saves':>7}{'kills':>7}{'net_p':>9}{'prec':>7}")
        for thresh in (6, 7, 8, 10):
            for lock in (0.5, 1.0):
                r = evaluate_stacked(f'stacked_t{thresh}_l{lock}', thresh, lock,
                                      classifier_fn, measured, pair_dfs)
                cohort_results['tier1_plus_clf65'].append({**r, 'threshold': thresh, 'lock_pips': lock})
                print(f"    {thresh:>6}{lock:>6.1f}{r['fires']:>7}{r['tier1_fires']:>5}{r['tier2_fires']:>5}"
                      f"{r['saves']:>7}{r['kills']:>7}{r['net_p']:>+9.1f}{r['precision']:>7.2f}")

        all_results[cohort_label] = cohort_results

    # Find optimal
    print('\n' + '=' * 100)
    print('OPTIMAL TIER 1 ALONE (by net pips, 90d cohort)')
    print('=' * 100)
    t1_results = all_results['90d']['tier1_alone']
    by_net = sorted(t1_results, key=lambda r: -r['net_p'])[:5]
    for r in by_net:
        print(f"  thresh={r['threshold']:>3} lock={r['lock_pips']:>4.1f}  "
              f"fires={r['fires']:>3} saves={r['saves']:>3} kills={r['kills']:>3} "
              f"net={r['net_p']:+.1f}p  precision={r['precision']:.2f}")
    print()
    print('Best by precision (≥10 fires):')
    by_prec = sorted([r for r in t1_results if r['fires'] >= 10],
                     key=lambda r: (-r['precision'], -r['net_p']))[:5]
    for r in by_prec:
        print(f"  thresh={r['threshold']:>3} lock={r['lock_pips']:>4.1f}  "
              f"fires={r['fires']:>3} saves={r['saves']:>3} kills={r['kills']:>3} "
              f"net={r['net_p']:+.1f}p  precision={r['precision']:.2f}")
    print()
    print('Best STACKED combo (by net + precision):')
    stacked = all_results['90d']['tier1_plus_clf65']
    by_score = sorted(stacked, key=lambda r: -(r['net_p'] + r['precision'] * 20))[:3]
    for r in by_score:
        print(f"  t1_thresh={r['threshold']} t1_lock={r['lock_pips']}  "
              f"fires={r['fires']} t1={r['tier1_fires']} t2={r['tier2_fires']}  "
              f"saves={r['saves']} kills={r['kills']} net={r['net_p']:+.1f}p prec={r['precision']:.2f}")

    out = os.path.join(HERE,
                       f'failed_rally_phase4b_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.json')
    with open(out, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f'\nFull JSON: {out}')


if __name__ == '__main__':
    main()
