"""One-off: replay today's closed trades through V0 (old failed_rally_lock)
and V_clf65 (rewrite) to answer 'would these have caught losers or killed winners?'

Window: 2026-05-14 00:00 ET → now (04:00 UTC → now UTC).
"""
from __future__ import annotations
import sys
import os
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)
sys.path.insert(0, HERE)

from oanda_client import OandaClient
from backtester.indicators import compute_all
from failed_rally_phase2 import (
    load_trades, fetch_pair_candles, candles_to_df, measure_trade,
)
from failed_rally_phase3_sweep import (
    v0_current, make_classifier_variant, simulate_lock_outcome,
)

START = '2026-05-14T04:00:00+00:00'  # 12am ET = 04:00 UTC
END   = '2026-05-15T00:00:00+00:00'

CLASSIFIER_JSON = os.path.join(SOURCE_DIR, 'early_exhaustion_classifier.json')


def main():
    print(f"Window: {START} → {END}  (12am ET 2026-05-14 → end of day UTC)")
    trades = load_trades(START, END)
    print(f"Loaded {len(trades)} trades (scout+snipe_direct, kronos-excluded)")
    if not trades:
        return

    oanda = OandaClient()
    pair_candles = fetch_pair_candles(trades, oanda)
    pair_dfs = {}
    for pair, cs in pair_candles.items():
        df = candles_to_df(cs)
        if not df.empty:
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
    print(f"Measured {len(measured)} trades\n")

    # Load classifier
    with open(CLASSIFIER_JSON) as f:
        clf = json.load(f)
    v_clf65 = make_classifier_variant(
        np.array(list(clf['classifier_coefs'].values())),
        clf['classifier_intercept'],
        clf['classifier_scaler_mean'],
        clf['classifier_scaler_scale'],
        clf['feature_names'],
        0.65,
    )
    v_clf70 = make_classifier_variant(
        np.array(list(clf['classifier_coefs'].values())),
        clf['classifier_intercept'],
        clf['classifier_scaler_mean'],
        clf['classifier_scaler_scale'],
        clf['feature_names'],
        0.70,
    )

    # Per-trade table
    print(f"{'id':>6} {'pair':<8} {'dir':<5} {'pnl':>7} {'mfe':>6} {'mfe_b':>6} "
          f"{'pat':<32} {'V0':<8} {'V0_sim':>8} {'V0_Δ':>7} {'C65':<8} {'C65_sim':>8} {'C65_Δ':>7}")
    print('-' * 130)
    v0_fires, c65_fires = [], []
    for t in measured:
        m = t['m']
        v0 = v0_current(m, t)
        c65 = v_clf65(m, t)
        pair = t['pair']
        df = pair_dfs[pair]

        v0_sim, _ = simulate_lock_outcome(t, df) if v0 else (t['pnl_pips'], None)
        c65_sim, _ = simulate_lock_outcome(t, df) if c65 else (t['pnl_pips'], None)

        v0_delta = (v0_sim - t['pnl_pips']) if v0 else 0
        c65_delta = (c65_sim - t['pnl_pips']) if c65 else 0

        if v0:
            v0_fires.append({'id': t['id'], 'pair': pair, 'pnl': t['pnl_pips'],
                             'sim': v0_sim, 'delta': v0_delta, 'is_loser': t['pnl_pips'] < 0})
        if c65:
            c65_fires.append({'id': t['id'], 'pair': pair, 'pnl': t['pnl_pips'],
                              'sim': c65_sim, 'delta': c65_delta, 'is_loser': t['pnl_pips'] < 0})

        pat_short = (m.get('pattern') or '')[:30]
        print(f"{t['id']:>6} {pair:<8} {t['direction']:<5} "
              f"{t['pnl_pips']:>+7.1f} {m['mfe']:>+6.1f} {m['mfe_bar']:>6} "
              f"{pat_short:<32} "
              f"{'FIRE' if v0 else '-':<8} "
              f"{v0_sim:>+8.1f} {v0_delta:>+7.1f} "
              f"{'FIRE' if c65 else '-':<8} "
              f"{c65_sim:>+8.1f} {c65_delta:>+7.1f}")

    print()
    for label, fires in (('V0 (old rule)', v0_fires), ('V_clf65 (rewrite)', c65_fires)):
        saves = [f for f in fires if f['is_loser']]
        kills = [f for f in fires if not f['is_loser']]
        saved_p = sum(f['delta'] for f in saves)
        killed_p = sum(f['delta'] for f in kills)
        print(f"  {label}:  fires={len(fires)}  saves={len(saves)} (+{saved_p:.1f}p)  "
              f"kills={len(kills)} ({killed_p:+.1f}p)  net={saved_p+killed_p:+.1f}p")
        for f in fires:
            tag = 'SAVE' if f['is_loser'] else 'KILL'
            print(f"    {tag}  {f['id']:>6} {f['pair']:<8}  actual={f['pnl']:+.1f}p  "
                  f"sim={f['sim']:+.1f}p  Δ={f['delta']:+.1f}p")
        print()


if __name__ == '__main__':
    main()
