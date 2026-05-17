"""Phase 4 — candle-walk forward test on top variant (V_clf65).

For each selected trade:
  1. Show entry context (pair, direction, time, MFE/MAE outcome)
  2. Print bar-by-bar trace: close pip, high pip, low pip, key indicators
  3. Mark MFE-peak bar and decision bar
  4. Show classifier features at decision bar + P(loser)
  5. Show rule decision (FIRE or HOLD) and counterfactual sim outcome

Selection: top 10 by interest from Phase 3 fires_detail —
   - 3 V_clf65 saves (rule worked)
   - 3 V_clf65 kills (rule fired but trade was winner)
   - 4 V_clf65 misses (loser that rule didn't save)

Also walks today's 5 fires explicitly.

Output: human-readable trace per trade.
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
from failed_rally_phase3_sweep import (
    make_classifier_variant, simulate_lock_outcome,
)


def to_et(dt):
    if isinstance(dt, str):
        dt = parse_iso(dt)
    return (dt - timedelta(hours=4)).strftime('%m-%d %H:%M ET')


def walk_one_trade(t, df, classifier_fn, clf_payload, P_FIRE=0.65):
    """Print full candle walk for one trade with rule state at each bar."""
    pip = 0.01 if 'JPY' in t['pair'] else 0.0001
    is_buy = t['direction'] in ('buy', 'long')
    entry = t['entry_price']

    print()
    print('─' * 100)
    print(f"TRADE {t['id']}  {t['pair']:8s} {t['direction']:<4s}  "
          f"entry={entry:.5f}  ENT={to_et(t['entry_time'])}  EXIT={to_et(t['exit_time'])}  "
          f"actual_pnl={t['pnl_pips']:+.1f}p")
    print('─' * 100)

    ent_idx = find_entry_index(df, t['entry_time'])
    exit_idx = find_exit_index(df, t['exit_time'])
    if ent_idx < 0 or exit_idx <= ent_idx:
        print('  No bars.')
        return

    m = measure_trade(t, df)
    if not m:
        print('  No measurement.')
        return

    print(f"  Pattern={m['pattern']}  MFE={m['mfe']:+.1f}p @ bar {m['mfe_bar']}  "
          f"MAE={m['mae']:+.1f}p @ bar {m['mae_bar']}  "
          f"decision_bar={m['decision_bar']}")

    snap = m.get('snap_decision') or {}
    fired = classifier_fn(m, t)
    # Compute P(loser) manually for inspection
    coefs = np.array(list(clf_payload['classifier_coefs'].values()))
    intercept = clf_payload['classifier_intercept']
    sc_mean = np.array(clf_payload['classifier_scaler_mean'])
    sc_scale = np.array(clf_payload['classifier_scaler_scale'])
    feature_keys = [
        'rsi', 'stoch_k', 'adx', 'macd_hist', 'bb_pos', 'bb_width_ratio',
        'fan_ordered', 'fan_inverted', 'e21_e55_pips', 'e55_e100_pips',
        'fan_velocity', 'candle_vs_e21', 'counter_color_count', 'is_reversal_candle',
    ]
    derived_keys = ['mfe', 'mfe_bar', 'mae_at_peak', 'decision_bar']
    if snap and m['pattern'] in ('long_neg_then_brief_pos', 'short_neg_then_brief_pos') \
       and m['mfe'] >= 3.0 and m['decision_bar'] <= 8:
        x = np.array([snap.get(k, 0) for k in feature_keys] + [m[k] for k in derived_keys],
                     dtype=float)
        x_scaled = (x - sc_mean) / sc_scale
        logit = float(np.dot(x_scaled, coefs)) + intercept
        p_loser = 1 / (1 + math.exp(-logit))
    else:
        p_loser = None

    print(f"  Classifier P(loser) = {p_loser:.3f}" if p_loser is not None else "  Classifier: out of universe (no MFE>=3 or wrong pattern)")
    print(f"  V_clf65 verdict: {'🔥 FIRE (lock SL to BE+0.5)' if fired else '· HOLD (no action)'}")

    if snap:
        print(f"  Decision-bar indicators:")
        print(f"    RSI={snap.get('rsi'):.1f}  stoch_K={snap.get('stoch_k'):.1f}  "
              f"ADX={snap.get('adx'):.1f}  fan_ordered={snap.get('fan_ordered')}")
        print(f"    E21-E55={snap.get('e21_e55_pips'):.1f}p  "
              f"E55-E100={snap.get('e55_e100_pips'):.1f}p  "
              f"fan_velocity={snap.get('fan_velocity'):+.1f}")
        print(f"    counter_color={snap.get('counter_color_count')}  "
              f"is_reversal={snap.get('is_reversal_candle')}  "
              f"candle_type={snap.get('candle_type')}")

    # Bar-by-bar trace
    print()
    print(f"  {'bar':>4}{'time_ET':>10}{'close':>9}{'high':>8}{'low':>8}"
          f"{'rsi':>7}{'fan_ord':>9}{'tag':>6}")
    seg = df.iloc[ent_idx:exit_idx + 1]
    for i, (_, row) in enumerate(seg.iterrows()):
        h = float(row['high']); lo = float(row['low']); cl = float(row['close'])
        if is_buy:
            cp = (cl - entry) / pip
            hp = (h - entry) / pip
            lp = (lo - entry) / pip
        else:
            cp = (entry - cl) / pip
            hp = (entry - lo) / pip
            lp = (entry - h) / pip
        tag = ''
        if i == m['mfe_bar']: tag += 'MFE'
        if i == m['decision_bar']: tag += '↘DEC'
        rsi_v = row.get('rsi', 0)
        e21 = row.get('ema_21', 0); e55 = row.get('ema_55', 0); e100 = row.get('ema_100', 0)
        fan_o = 1 if (is_buy and e21 > e55 > e100) or (not is_buy and e21 < e55 < e100) else 0
        bartime = pd.to_datetime(row['time'].replace('Z', '')) - timedelta(hours=4)
        print(f"  {i:>4}{bartime.strftime('%m-%d %H:%M'):>10}"
              f"{cp:>+9.1f}{hp:>+8.1f}{lp:>+8.1f}{rsi_v:>+7.1f}{fan_o:>9}{tag:>6}")

    # Counterfactual simulation
    sim_pnl, hit_bar = simulate_lock_outcome({**t, 'm': m}, df)
    delta = sim_pnl - t['pnl_pips']
    if fired:
        if t['pnl_pips'] < 0:
            print(f"\n  ✓ SAVE: actual={t['pnl_pips']:+.1f}p → sim={sim_pnl:+.1f}p  "
                  f"DELTA={delta:+.1f}p")
        else:
            print(f"\n  ✗ KILL: actual={t['pnl_pips']:+.1f}p → sim={sim_pnl:+.1f}p  "
                  f"DELTA={delta:+.1f}p (winner cut to BE)")


def main():
    # Load Phase 2 classifier + Phase 3 results
    p2_files = sorted([f for f in os.listdir(HERE) if f.startswith('failed_rally_phase2_') and f.endswith('.json')])
    p3_files = sorted([f for f in os.listdir(HERE) if f.startswith('failed_rally_phase3_') and f.endswith('.json')])
    if not p2_files or not p3_files:
        print('ERROR: missing Phase 2 or Phase 3 JSON.')
        return
    with open(os.path.join(HERE, p2_files[-1])) as f:
        clf_payload = json.load(f)
    with open(os.path.join(HERE, p3_files[-1])) as f:
        p3_results = json.load(f)

    # Build classifier function
    classifier_fn = make_classifier_variant(
        np.array(list(clf_payload['classifier_coefs'].values())),
        clf_payload['classifier_intercept'],
        clf_payload['classifier_scaler_mean'],
        clf_payload['classifier_scaler_scale'],
        clf_payload['feature_names'],
        0.65,
    )

    # Pull selection of trades to walk
    # 1. V_clf65 saves from 90d
    # 2. V_clf65 kills from 90d
    # 3. Today's 5 fires
    saves_90d = p3_results.get('90d', {}).get('variants', {}).get('V_clf65', {}).get('save_ids', [])
    kills_90d = p3_results.get('90d', {}).get('variants', {}).get('V_clf65', {}).get('kill_ids', [])

    today_ids = ['13843', '13809', '13944', '13964', '14062']

    selected_ids = (set(saves_90d[:3]) | set(kills_90d[:3]) | set(today_ids))
    print(f'\nWalking {len(selected_ids)} trades:')
    print(f'  V_clf65 saves: {saves_90d[:3]}')
    print(f'  V_clf65 kills: {kills_90d[:3]}')
    print(f'  Today fires:   {today_ids}')

    # Load 90d cohort + today
    trades = load_trades('2026-02-11', '2026-05-12')
    trades_by_id = {t['id']: t for t in trades}
    target_trades = [trades_by_id[tid] for tid in selected_ids if tid in trades_by_id]
    print(f'  Found in DB:   {len(target_trades)}')

    oanda = OandaClient()
    cache = fetch_pair_candles(target_trades, oanda)
    pair_dfs = {}
    for pair, candles in cache.items():
        df = candles_to_df(candles)
        if df.empty:
            continue
        pair_dfs[pair] = compute_all(df)

    print()
    print('=' * 100)
    print('CANDLE-WALK — V_clf65 verdict on representative trades')
    print('=' * 100)

    # Order: saves first, then kills, then today
    order = []
    for tid in saves_90d[:3]:
        if tid in trades_by_id:
            order.append(('SAVE', trades_by_id[tid]))
    for tid in kills_90d[:3]:
        if tid in trades_by_id:
            order.append(('KILL', trades_by_id[tid]))
    for tid in today_ids:
        if tid in trades_by_id:
            order.append(('TODAY', trades_by_id[tid]))

    for label, t in order:
        df = pair_dfs.get(t['pair'])
        if df is None:
            continue
        print(f'\n\n>>> CATEGORY: {label} <<<')
        walk_one_trade(t, df, classifier_fn, clf_payload, P_FIRE=0.65)

    print()
    print('=' * 100)
    print('SUMMARY')
    print('=' * 100)
    s = p3_results.get('90d', {}).get('variants', {}).get('V_clf65', {})
    print(f'  V_clf65 over 90d: {s["fires"]} fires, {s["saves"]} saves '
          f'(+{s["saved_p"]}p), {s["kills"]} kills ({s["killed_p"]}p), '
          f'NET={s["net_p"]:+.1f}p')
    pt = p3_results.get('post_tune', {}).get('variants', {}).get('V_clf65', {})
    print(f'  V_clf65 post-tune: {pt["fires"]} fires, {pt["saves"]} saves '
          f'(+{pt["saved_p"]}p), {pt["kills"]} kills ({pt["killed_p"]}p), '
          f'NET={pt["net_p"]:+.1f}p')


if __name__ == '__main__':
    main()
