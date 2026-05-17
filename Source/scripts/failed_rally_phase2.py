"""Phase 2 — multi-indicator fingerprint study for failed_rally_lock rewrite.

For every post-tune closed trade in the 90d cohort that's a candidate for
the rule (MFE >= 3p AND brief-positive pattern AND peak within arm window),
capture indicator snapshots at the decision bar, then fit a classifier to
identify what discriminates failed-rally losers from brief-positive winners.

Decision bar = first negative M15 close AFTER MFE-peak bar. This is the
moment Path A would fire — the question is whether to move SL to BE+ here
or leave the trade alone.

Pipeline:
  1. Load 90d cohort, exclude kronos.
  2. Fetch M15 candles per pair with 30h warmup buffer for indicator stability.
  3. compute_all indicators on each pair's DataFrame.
  4. Per trade: identify entry / MFE-peak / decision / exit bar indices.
  5. Snapshot 12 features at decision bar.
  6. Filter to RULE UNIVERSE: trades that would have triggered V1 (MFE >= 3p,
     brief-positive pattern, decision_bar <= 8).
  7. Label: 1=loser (pnl < 0), 0=winner (pnl > 0).
  8. Fit logistic regression with cross-validation, output:
       - Feature importance (coefficients)
       - ROC AUC
       - Classification report
       - Per-feature distribution by class
       - K-means cluster analysis on losers
  9. Write feature matrix to JSON for downstream variant sweep.
"""
from __future__ import annotations
import sys
import os
import sqlite3
import json
import math
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.metrics import roc_auc_score, classification_report, confusion_matrix
from sklearn.cluster import KMeans

from oanda_client import OandaClient
from backtester.indicators import compute_all

DB = '~/Jarvis/Database/v2/trading_forex.db'
COHORT_START = '2026-02-11'
COHORT_END = '2026-05-11'

# Rule candidate universe parameters (locked: rally_min_pips=3, arm_window=8)
RALLY_MIN_PIPS = 3.0
ARM_WINDOW_BARS = 8


def parse_iso(s):
    if not s:
        return None
    s = s.replace('Z', '').rstrip()
    if '.' in s:
        b, f = s.split('.', 1); s = f"{b}.{f[:6]}"
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def load_trades(start, end):
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, pair, direction, entry_price, sl_price, source, entry_type,
               entry_time, exit_time, pnl_pips
        FROM live_trades
        WHERE status='closed'
          AND exit_time >= ? AND exit_time < ?
          AND source IN ('scout','snipe_direct')
          AND (entry_type IS NULL OR entry_type NOT LIKE '%kronos%')
          AND source NOT LIKE '%kronos%'
          AND pnl_pips IS NOT NULL
        ORDER BY exit_time
        """, (start, end)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        et = parse_iso(r['entry_time']); xt = parse_iso(r['exit_time'])
        if not et or not xt:
            continue
        out.append({
            'id': str(r['id']), 'pair': r['pair'], 'direction': r['direction'],
            'entry_price': float(r['entry_price']),
            'sl_price': float(r['sl_price']) if r['sl_price'] else None,
            'source': r['source'], 'entry_type': r['entry_type'],
            'entry_time': et, 'exit_time': xt,
            'pnl_pips': float(r['pnl_pips']),
        })
    return out


def fetch_pair_candles(trades, oanda, granularity='M15'):
    cache = {}
    by_pair = defaultdict(list)
    for t in trades:
        by_pair[t['pair']].append(t)
    for pair, trs in by_pair.items():
        earliest = min(t['entry_time'] for t in trs)
        latest = max(t['exit_time'] for t in trs)
        candles = oanda.fetch_candles_range(
            instrument=pair, granularity=granularity,
            from_time=earliest - timedelta(hours=30),
            to_time=latest + timedelta(hours=1), price='M',
        )
        cache[pair] = [c for c in candles if c.get('complete', True)]
    return cache


def candles_to_df(candles):
    rows = []
    for c in candles:
        mid = c.get('mid', {})
        rows.append({
            'time': c['time'],
            'open':  float(mid.get('o', 0)),
            'high':  float(mid.get('h', 0)),
            'low':   float(mid.get('l', 0)),
            'close': float(mid.get('c', 0)),
            'volume': c.get('volume', 0),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df['time_dt'] = pd.to_datetime(df['time'].str.replace('Z', '', regex=False),
                                    format='mixed', utc=True)
    return df


def find_entry_index(df, entry_time):
    if df.empty:
        return -1
    bar_close = df['time_dt'] + pd.Timedelta(minutes=15)
    mask = bar_close > entry_time
    if not mask.any():
        return -1
    return int(mask.idxmax())


def find_exit_index(df, exit_time):
    if df.empty:
        return -1
    bar_close = df['time_dt'] + pd.Timedelta(minutes=15)
    mask = bar_close >= exit_time
    if mask.any():
        return int(mask.idxmax())
    return len(df) - 1


def _candle_color(row, prev_row=None):
    """Return 'green' if close > open, 'red' if close < open, 'doji' if tiny body."""
    o = row['open']; c = row['close']
    body = abs(c - o)
    rng = row['high'] - row['low']
    if rng <= 0:
        return 'doji'
    if body / rng < 0.15:
        return 'doji'
    return 'green' if c > o else 'red'


def _candle_type(row):
    """Classify candle: engulfing_bull, engulfing_bear, hammer, shooting_star,
    doji, exhaustion_top_wick, exhaustion_bot_wick, or 'normal'."""
    o = row['open']; c = row['close']; h = row['high']; l = row['low']
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return 'doji'
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_frac = body / rng if rng > 0 else 0

    if body_frac < 0.15:
        return 'doji'
    if upper_wick > 2 * body and lower_wick < body * 0.5:
        return 'shooting_star' if c < o else 'exhaustion_top_wick'
    if lower_wick > 2 * body and upper_wick < body * 0.5:
        return 'hammer' if c > o else 'exhaustion_bot_wick'
    return 'normal'


def _color_streak(df, idx, n=5):
    """Count counter-color bars in last n bars (relative to trade direction).
    Used externally — this just returns the raw color sequence."""
    if idx < 0:
        return []
    start = max(0, idx - n + 1)
    return [_candle_color(df.iloc[i]) for i in range(start, idx + 1)]


def snapshot_at_bar(df, idx, is_buy, entry_price, pip):
    """Capture all features at bar `idx` of df."""
    if idx < 0 or idx >= len(df):
        return None
    row = df.iloc[idx]

    def safe(v, default=0.0):
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            return default
        return float(v)

    e21 = safe(row.get('ema_21'))
    e55 = safe(row.get('ema_55'))
    e100 = safe(row.get('ema_100'))
    close = float(row['close'])

    # Fan state vs direction
    if is_buy:
        fan_ordered = 1 if (e21 > e55 > e100) else 0
        fan_inverted = 1 if (e21 < e55 < e100) else 0
    else:
        fan_ordered = 1 if (e21 < e55 < e100) else 0
        fan_inverted = 1 if (e21 > e55 > e100) else 0

    # Fan separation in pips (E21 to E55, E55 to E100)
    e21_e55_pips = (e21 - e55) / pip if is_buy else (e55 - e21) / pip
    e55_e100_pips = (e55 - e100) / pip if is_buy else (e100 - e55) / pip

    # Fan velocity: change in E21-E55 gap vs 5 bars ago
    fan_velocity = 0.0
    if idx >= 5:
        prev = df.iloc[idx - 5]
        prev_e21 = safe(prev.get('ema_21'))
        prev_e55 = safe(prev.get('ema_55'))
        if prev_e21 and prev_e55:
            prev_gap = (prev_e21 - prev_e55) / pip if is_buy else (prev_e55 - prev_e21) / pip
            fan_velocity = e21_e55_pips - prev_gap

    # BB state
    bb_upper = safe(row.get('bb_upper'))
    bb_lower = safe(row.get('bb_lower'))
    bb_mid = safe(row.get('bb_middle'))
    bb_width = safe(row.get('bb_width'))

    # BB width ratio vs 20-bar mean
    bb_width_ratio = 1.0
    if idx >= 20:
        prev_widths = df['bb_width'].iloc[max(0, idx - 20):idx]
        prev_mean = prev_widths.mean()
        if prev_mean and not math.isnan(prev_mean):
            bb_width_ratio = bb_width / prev_mean if prev_mean > 0 else 1.0

    # BB position: 1=above upper, -1=below lower, 0=inside
    if bb_upper and bb_lower:
        if close >= bb_upper:
            bb_pos = 1
        elif close <= bb_lower:
            bb_pos = -1
        else:
            bb_pos = 0
    else:
        bb_pos = 0

    # Candle position vs E21
    candle_vs_e21 = 0
    if e21:
        if is_buy:
            candle_vs_e21 = 1 if close > e21 else (-1 if close < e21 else 0)
        else:
            candle_vs_e21 = 1 if close < e21 else (-1 if close > e21 else 0)

    # Color streak: count counter-color bars in last 5 (relative to trade direction)
    streak = _color_streak(df, idx, n=5)
    in_trend_color = 'green' if is_buy else 'red'
    counter_color_count = sum(1 for c in streak if c != in_trend_color and c != 'doji')

    # Candle type at this bar
    ctype = _candle_type(row)
    # Encode reversal-against-direction
    if is_buy:
        is_reversal_candle = 1 if ctype in ('shooting_star', 'exhaustion_top_wick', 'doji') else 0
    else:
        is_reversal_candle = 1 if ctype in ('hammer', 'exhaustion_bot_wick', 'doji') else 0

    return {
        'rsi': safe(row.get('rsi')),
        'stoch_k': safe(row.get('stoch_k')),
        'adx': safe(row.get('adx')),
        'macd_hist': safe(row.get('macd_histogram')),
        'bb_pos': bb_pos,
        'bb_width_ratio': bb_width_ratio,
        'fan_ordered': fan_ordered,
        'fan_inverted': fan_inverted,
        'e21_e55_pips': e21_e55_pips,
        'e55_e100_pips': e55_e100_pips,
        'fan_velocity': fan_velocity,
        'candle_vs_e21': candle_vs_e21,
        'counter_color_count': counter_color_count,
        'is_reversal_candle': is_reversal_candle,
        'candle_type': ctype,
    }


def measure_trade(t, df):
    """Walk trade window, find decision points, snapshot indicators."""
    pip = 0.01 if 'JPY' in t['pair'] else 0.0001
    is_buy = t['direction'] in ('buy', 'long')
    entry = t['entry_price']

    ent_idx = find_entry_index(df, t['entry_time'])
    exit_idx = find_exit_index(df, t['exit_time'])
    if ent_idx < 0 or exit_idx < 0 or exit_idx <= ent_idx:
        return None

    seg = df.iloc[ent_idx:exit_idx + 1]
    closes, highs, lows = [], [], []
    for _, row in seg.iterrows():
        h = float(row['high']); lo = float(row['low']); cl = float(row['close'])
        if is_buy:
            closes.append((cl - entry) / pip)
            highs.append((h - entry) / pip)
            lows.append((lo - entry) / pip)
        else:
            closes.append((entry - cl) / pip)
            highs.append((entry - lo) / pip)
            lows.append((entry - h) / pip)

    if not closes:
        return None

    mfe = max(highs); mfe_bar_rel = highs.index(mfe)
    mae = min(lows); mae_bar_rel = lows.index(mae)
    mae_at_peak = min(lows[:mfe_bar_rel + 1])

    # Pattern bucket (same as Phase 1)
    first_pos_close_bar = next((i for i, p in enumerate(closes) if p > 0), -1)
    if first_pos_close_bar < 0:
        pattern = 'never_positive'
    elif first_pos_close_bar == 0 and mfe > 0:
        pattern = 'positive_at_entry'
    elif first_pos_close_bar >= 5:
        pattern = 'long_neg_then_brief_pos'
    else:
        pattern = 'short_neg_then_brief_pos'

    # Decision bar: first negative close AFTER mfe_bar_rel (Path A fires here)
    decision_bar_rel = -1
    for i in range(mfe_bar_rel + 1, len(closes)):
        if closes[i] < 0:
            decision_bar_rel = i
            break
    # If no post-peak negative found, decision_bar = mfe_bar (rule wouldn't fire)
    if decision_bar_rel < 0:
        decision_bar_rel = mfe_bar_rel

    decision_idx_abs = ent_idx + decision_bar_rel
    mfe_idx_abs = ent_idx + mfe_bar_rel

    # Snapshot at entry and decision bar (most important for rule firing)
    snap_entry = snapshot_at_bar(df, ent_idx, is_buy, entry, pip)
    snap_peak = snapshot_at_bar(df, mfe_idx_abs, is_buy, entry, pip)
    snap_decision = snapshot_at_bar(df, decision_idx_abs, is_buy, entry, pip)

    return {
        'pattern': pattern,
        'mfe': round(mfe, 1), 'mfe_bar': mfe_bar_rel,
        'mae': round(mae, 1), 'mae_bar': mae_bar_rel,
        'mae_at_peak': round(mae_at_peak, 1),
        'first_pos_close_bar': first_pos_close_bar,
        'decision_bar': decision_bar_rel,
        'n_bars': len(closes),
        'snap_entry': snap_entry,
        'snap_peak': snap_peak,
        'snap_decision': snap_decision,
        'closes_head': [round(c, 1) for c in closes[:15]],
    }


def is_rule_candidate(m):
    """V1 universe: rule would have considered firing on this trade."""
    if not m:
        return False
    if m['mfe'] < RALLY_MIN_PIPS:
        return False
    if m['pattern'] not in ('long_neg_then_brief_pos', 'short_neg_then_brief_pos'):
        return False
    if m['decision_bar'] > ARM_WINDOW_BARS:
        return False
    return True


def main():
    print(f'Loading 90d cohort [{COHORT_START} → {COHORT_END})...')
    trades = load_trades(COHORT_START, COHORT_END)
    print(f'  {len(trades)} trades')

    oanda = OandaClient()
    print('Fetching candles...')
    cache = fetch_pair_candles(trades, oanda)
    print(f'  {len(cache)} pairs, total bars {sum(len(c) for c in cache.values())}')

    print('Computing indicators...')
    pair_dfs = {}
    for pair, candles in cache.items():
        df = candles_to_df(candles)
        if df.empty:
            continue
        df = compute_all(df)
        pair_dfs[pair] = df

    print('Measuring trades...')
    measured = []
    for t in trades:
        df = pair_dfs.get(t['pair'])
        if df is None or df.empty:
            continue
        m = measure_trade(t, df)
        if m is None:
            continue
        m['is_candidate'] = is_rule_candidate(m)
        measured.append({**t, 'm': m})

    candidates = [t for t in measured if t['m']['is_candidate']]
    print(f'  {len(measured)} measured, {len(candidates)} are V1-rule candidates')

    winners = [t for t in candidates if t['pnl_pips'] > 0]
    losers  = [t for t in candidates if t['pnl_pips'] < 0]
    print(f'  Candidates: {len(winners)} winners (would be KILLED), {len(losers)} losers (would be SAVED)')

    if len(losers) < 5 or len(winners) < 5:
        print('  Insufficient candidates for classifier. Lower filters or extend cohort.')
        return

    # ─── Build feature matrix ───
    feature_keys = [
        'rsi', 'stoch_k', 'adx', 'macd_hist',
        'bb_pos', 'bb_width_ratio',
        'fan_ordered', 'fan_inverted',
        'e21_e55_pips', 'e55_e100_pips', 'fan_velocity',
        'candle_vs_e21', 'counter_color_count', 'is_reversal_candle',
    ]
    # Also include trade-derived features
    derived_keys = ['mfe', 'mfe_bar', 'mae_at_peak', 'decision_bar']

    rows = []
    labels = []
    ids = []
    for t in candidates:
        snap = t['m']['snap_decision']
        if not snap:
            continue
        r = [snap.get(k, 0) for k in feature_keys]
        r += [t['m'][k] for k in derived_keys]
        rows.append(r)
        labels.append(1 if t['pnl_pips'] < 0 else 0)  # 1 = loser
        ids.append(t['id'])

    X = np.array(rows)
    y = np.array(labels)
    print(f'  Feature matrix: {X.shape}, label balance: {Counter(labels)}')

    # ─── Fit logistic regression ───
    print()
    print('=' * 100)
    print('LOGISTIC REGRESSION — predict failed-rally loser at decision bar')
    print('=' * 100)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    clf = LogisticRegression(max_iter=2000, class_weight='balanced',
                              C=1.0, random_state=42)
    clf.fit(X_scaled, y)

    feature_names = feature_keys + derived_keys
    coefs = list(zip(feature_names, clf.coef_[0]))
    coefs_sorted = sorted(coefs, key=lambda x: -abs(x[1]))
    print('\n  Feature importance (coef magnitude, scaled features):')
    print(f"  {'feature':<25}{'coef':>10}{'direction':>12}")
    for name, coef in coefs_sorted:
        direction = 'loser+' if coef > 0 else 'winner+'
        print(f"  {name:<25}{coef:>+10.3f}{direction:>12}")

    # Cross-validated ROC AUC
    try:
        cv = StratifiedKFold(n_splits=min(5, len(losers)), shuffle=True, random_state=42)
        aucs = cross_val_score(clf, X_scaled, y, cv=cv, scoring='roc_auc')
        print(f'\n  Cross-val ROC AUC: {aucs.mean():.3f} ± {aucs.std():.3f} '
              f'({len(aucs)} folds)')
    except Exception as e:
        print(f'\n  Cross-val failed: {e}')

    # Per-trade probabilities for outside-the-box inspection
    probs = clf.predict_proba(X_scaled)[:, 1]

    # ─── Per-feature distribution by class ───
    print()
    print('=' * 100)
    print('FEATURE DISTRIBUTIONS — loser vs winner medians at decision bar')
    print('=' * 100)
    print(f"  {'feature':<25}{'loser_p50':>12}{'winner_p50':>12}{'separation':>12}")
    for k in feature_keys + derived_keys:
        lv = [X[i, feature_names.index(k)] for i in range(len(y)) if y[i] == 1]
        wv = [X[i, feature_names.index(k)] for i in range(len(y)) if y[i] == 0]
        lp = float(np.median(lv)) if lv else 0
        wp = float(np.median(wv)) if wv else 0
        sep = lp - wp
        print(f"  {k:<25}{lp:>+12.2f}{wp:>+12.2f}{sep:>+12.2f}")

    # ─── K-means on losers only ───
    print()
    print('=' * 100)
    print(f'K-MEANS on {len(losers)} failed-rally losers — are they one type or multiple?')
    print('=' * 100)
    loser_rows = np.array([rows[i] for i in range(len(y)) if y[i] == 1])
    if len(loser_rows) >= 6:
        # Try k=2 and k=3
        for k in (2, 3):
            km = KMeans(n_clusters=k, random_state=42, n_init=10)
            labels_k = km.fit_predict(StandardScaler().fit_transform(loser_rows))
            cnt = Counter(labels_k)
            print(f'\n  k={k}: cluster sizes = {dict(cnt)}')
            # Cluster centroid summary
            for c in sorted(set(labels_k)):
                idxs = [i for i, lab in enumerate(labels_k) if lab == c]
                centroid = loser_rows[idxs].mean(axis=0)
                top3 = sorted(zip(feature_names, centroid),
                              key=lambda x: -abs(x[1]))[:4]
                print(f'    cluster {c} (n={cnt[c]}): top features = '
                      + ', '.join(f'{n}={v:+.1f}' for n, v in top3))

    # ─── Outside-the-box: threshold table from sklearn ───
    print()
    print('=' * 100)
    print('THRESHOLD TABLE — classifier probability cutoff vs save/kill trade-off')
    print('=' * 100)
    print(f"  {'P_cut':>6}{'fires':>7}{'TP(sv)':>8}{'FP(kl)':>8}{'precision':>11}{'recall':>9}")
    for thresh in (0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75):
        fires = (probs >= thresh).astype(int)
        tp = int(((fires == 1) & (y == 1)).sum())   # saved
        fp = int(((fires == 1) & (y == 0)).sum())   # killed winner
        fn = int(((fires == 0) & (y == 1)).sum())   # missed loser
        prec = tp / max(tp + fp, 1)
        rec = tp / max(tp + fn, 1)
        print(f'  {thresh:>6.2f}{int(fires.sum()):>7}{tp:>8}{fp:>8}{prec:>11.2f}{rec:>9.2f}')

    # ─── Dump feature matrix for downstream variant sweep ───
    out = os.path.join(HERE, f'failed_rally_phase2_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.json')
    payload = {
        'cohort': [COHORT_START, COHORT_END],
        'rally_min_pips': RALLY_MIN_PIPS,
        'arm_window_bars': ARM_WINDOW_BARS,
        'feature_names': feature_names,
        'n_candidates': len(candidates),
        'n_winners_in_candidates': len(winners),
        'n_losers_in_candidates': len(losers),
        'classifier_coefs': dict(zip(feature_names, [float(c) for c in clf.coef_[0]])),
        'classifier_intercept': float(clf.intercept_[0]),
        'classifier_scaler_mean': scaler.mean_.tolist(),
        'classifier_scaler_scale': scaler.scale_.tolist(),
        'trade_records': [
            {
                'id': t['id'],
                'pair': t['pair'],
                'direction': t['direction'],
                'pnl_pips': t['pnl_pips'],
                'is_loser': t['pnl_pips'] < 0,
                'entry_time': t['entry_time'].isoformat(),
                'exit_time': t['exit_time'].isoformat(),
                'mfe': t['m']['mfe'],
                'mfe_bar': t['m']['mfe_bar'],
                'mae': t['m']['mae'],
                'mae_at_peak': t['m']['mae_at_peak'],
                'decision_bar': t['m']['decision_bar'],
                'pattern': t['m']['pattern'],
                'snap_decision': t['m']['snap_decision'],
                'closes_head': t['m']['closes_head'],
            }
            for t in candidates
        ],
    }
    with open(out, 'w') as f:
        json.dump(payload, f, indent=2, default=str)
    print(f'\nFull JSON: {out}')


if __name__ == '__main__':
    main()
