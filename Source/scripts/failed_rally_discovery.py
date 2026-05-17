"""Phase 1 discovery for failed_rally_lock rewrite.

Goal: MEASURE the pattern from historical data. No design decisions yet.

For every post-tune closed trade (scout + snipe_direct, exclude kronos):
  1. Pull M15 bars during the trade window.
  2. Compute MFE, MFE_bar, MAE, MAE_bar, pattern bucket (reuses
     big_loss_pattern_audit.characterize logic).
  3. Compute fan state at MFE-peak bar and at exit bar via
     backtester.indicators.compute_all (E21/E55/E100 ordering).
  4. Compute fan-break bar (first bar where E21 crossed E55 against the
     trade direction).

Aggregate distributions:
  - For losers in each pattern bucket (never_positive, failed_rally_long,
    failed_rally_short): MFE histogram, MFE_bar histogram, MAE histogram,
    fan-broken-at-peak %, bars-from-peak-to-break.
  - For winners: same metrics.

Output candidate values for:
  rally_min_pips         (MFE threshold to call something a "real rally")
  arm_window_bars        (bars from open when rule may arm)
  mae_at_peak_threshold  (MAE at MFE-peak — how deep was trade when it peaked?)
  hard_cut_mae_pips      (MAE for never_positive losers — early-cut threshold)

Two cohorts: 90-day full + 3-week post-tune clean window (2026-04-17 → 2026-05-04).

Outputs:
  - console summary with histograms + candidate-value table
  - JSON with full per-trade metrics for downstream variant testing
"""
from __future__ import annotations
import sys
import os
import sqlite3
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient

import pandas as pd

# Use the backtester indicators package for EMA21/55/100.
from backtester.indicators import ema as _ema_fn

DB = '~/Jarvis/Database/v2/trading_forex.db'

# Two cohorts. Post-tune was 2026-04-17 per failed_rally_test.py constant.
# Clean window per memory notes: Apr 29 - May 5 was the post-tune-clean band,
# but for failed-rally study we want the broader post-tune window minus the
# May 5+ regression contamination — Apr 17 to May 4 is the right slice.
COHORTS = {
    '90d':       {'start': '2026-02-11', 'end': '2026-05-11'},
    'post_tune': {'start': '2026-04-17', 'end': '2026-05-04'},
}


def parse_iso(s):
    if not s:
        return None
    s = s.replace('Z', '').rstrip()
    if '.' in s:
        b, f = s.split('.', 1)
        s = f"{b}.{f[:6]}"
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def to_et(dt):
    if dt is None:
        return ''
    return (dt - timedelta(hours=4)).strftime('%m-%d %H:%M')


def load_trades(start: str, end: str):
    """Pull closed scout + snipe_direct trades in [start, end). Exclude kronos."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, pair, direction, entry_price, sl_price, tp_price,
               source, entry_type, entry_time, exit_time, pnl_pips
        FROM live_trades
        WHERE status='closed'
          AND exit_time >= ? AND exit_time < ?
          AND source IN ('scout','snipe_direct')
          AND (entry_type IS NULL OR entry_type NOT LIKE '%kronos%')
          AND source NOT LIKE '%kronos%'
          AND pnl_pips IS NOT NULL
        ORDER BY exit_time
        """,
        (start, end),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        et = parse_iso(r['entry_time'])
        xt = parse_iso(r['exit_time'])
        if not et or not xt:
            continue
        out.append({
            'id': str(r['id']),
            'pair': r['pair'],
            'direction': r['direction'],
            'entry_price': float(r['entry_price']),
            'sl_price': float(r['sl_price']) if r['sl_price'] else None,
            'tp_price': float(r['tp_price']) if r['tp_price'] else None,
            'source': r['source'],
            'entry_type': r['entry_type'],
            'entry_time': et,
            'exit_time': xt,
            'pnl_pips': float(r['pnl_pips']),
        })
    return out


def fetch_pair_candles(trades, oanda: OandaClient, granularity='M15'):
    """One fetch per pair covering all trade windows. Buffer 4h either side
    so EMAs have warmup history available before each trade entry."""
    cache = {}
    by_pair = defaultdict(list)
    for t in trades:
        by_pair[t['pair']].append(t)
    for pair, trs in by_pair.items():
        earliest = min(t['entry_time'] for t in trs)
        latest = max(t['exit_time'] for t in trs)
        # 4-hour warmup before earliest entry so EMA100 has values at trade start.
        candles = oanda.fetch_candles_range(
            instrument=pair, granularity=granularity,
            from_time=earliest - timedelta(hours=30),
            to_time=latest + timedelta(hours=1), price='M',
        )
        cache[pair] = [c for c in candles if c.get('complete', True)]
    return cache


def candles_to_df(candles):
    """Convert OANDA candle list to indicator-ready DataFrame."""
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


def add_emas(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df['e21']  = _ema_fn(df, 21)
    df['e55']  = _ema_fn(df, 55)
    df['e100'] = _ema_fn(df, 100)
    return df


def find_entry_index(df: pd.DataFrame, entry_time: datetime) -> int:
    """First bar whose close-time > entry_time. (bar at index i represents
    candle that closes at df['time_dt'].iloc[i] + 15min.)"""
    if df.empty:
        return -1
    bar_close = df['time_dt'] + pd.Timedelta(minutes=15)
    mask = bar_close > entry_time
    if not mask.any():
        return -1
    return int(mask.idxmax())


def fan_state(row, is_buy: bool) -> str:
    """Return 'ordered' | 'inverted' | 'transition'."""
    e21, e55, e100 = row.get('e21'), row.get('e55'), row.get('e100')
    if pd.isna(e21) or pd.isna(e55) or pd.isna(e100):
        return 'unknown'
    if is_buy:
        if e21 > e55 > e100:
            return 'ordered'
        if e21 < e55 < e100:
            return 'inverted'
        return 'transition'
    else:
        if e21 < e55 < e100:
            return 'ordered'
        if e21 > e55 > e100:
            return 'inverted'
        return 'transition'


def measure_trade(t, df: pd.DataFrame) -> dict:
    """Full per-trade measurement."""
    pip = 0.01 if 'JPY' in t['pair'] else 0.0001
    is_buy = t['direction'] in ('buy', 'long')
    entry = t['entry_price']
    exit_t = t['exit_time']

    ent_idx = find_entry_index(df, t['entry_time'])
    if ent_idx < 0:
        return {'n_bars': 0, 'error': 'no_entry_index'}

    # Bars from ent_idx until the first bar whose close-time >= exit_time.
    bar_close = df['time_dt'] + pd.Timedelta(minutes=15)
    exit_mask = bar_close >= exit_t
    if exit_mask.any():
        exit_idx = int(exit_mask.idxmax())
    else:
        exit_idx = len(df) - 1
    if exit_idx <= ent_idx:
        return {'n_bars': 0, 'error': 'exit_before_entry'}

    seg = df.iloc[ent_idx:exit_idx + 1].copy()
    closes, highs, lows = [], [], []
    fan_states = []
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
        fan_states.append(fan_state(row, is_buy))

    if not closes:
        return {'n_bars': 0, 'error': 'empty_seg'}

    # Pattern bucketing
    first_pos_close_bar = next((i for i, p in enumerate(closes) if p > 0), -1)
    bars_neg_before_pos = first_pos_close_bar if first_pos_close_bar >= 0 else len(closes)
    mfe = max(highs)
    mfe_bar = highs.index(mfe)
    mae = min(lows)
    mae_bar = lows.index(mae)
    # MAE at MFE-peak (how deep was the trade in red when it peaked?)
    mae_at_peak = min(lows[:mfe_bar + 1]) if mfe_bar >= 0 else 0

    if first_pos_close_bar < 0:
        pattern = 'never_positive'
    elif first_pos_close_bar == 0 and mfe > 0:
        pattern = 'positive_at_entry'
    elif bars_neg_before_pos >= 5:
        pattern = 'long_neg_then_brief_pos'
    else:
        pattern = 'short_neg_then_brief_pos'

    # Fan-break: first bar where state went from 'ordered' to 'transition' or
    # 'inverted' after entry.
    fan_at_entry = fan_states[0] if fan_states else 'unknown'
    fan_break_bar = -1
    for i, s in enumerate(fan_states):
        if fan_at_entry == 'ordered' and s != 'ordered':
            fan_break_bar = i
            break
    fan_at_peak = fan_states[mfe_bar] if 0 <= mfe_bar < len(fan_states) else 'unknown'
    fan_at_exit = fan_states[-1] if fan_states else 'unknown'

    return {
        'n_bars': len(closes),
        'pattern': pattern,
        'mfe': round(mfe, 1),
        'mfe_bar': mfe_bar,
        'mae': round(mae, 1),
        'mae_bar': mae_bar,
        'mae_at_peak': round(mae_at_peak, 1),
        'first_pos_close_bar': first_pos_close_bar,
        'bars_neg_before_pos': bars_neg_before_pos,
        'fan_at_entry': fan_at_entry,
        'fan_at_peak': fan_at_peak,
        'fan_at_exit': fan_at_exit,
        'fan_break_bar': fan_break_bar,
        'closes_head': [round(c, 1) for c in closes[:12]],
    }


def percentile(values, p):
    """0..100 percentile."""
    if not values:
        return None
    vs = sorted(values)
    k = (len(vs) - 1) * (p / 100.0)
    f = int(k); c = min(f + 1, len(vs) - 1)
    if f == c:
        return vs[f]
    return vs[f] + (vs[c] - vs[f]) * (k - f)


def summarize_pattern(label, trades):
    """Print histogram + percentile summary for a trade subset."""
    if not trades:
        print(f"  {label:35s} (n=0)")
        return {}
    mfes = [t['m']['mfe'] for t in trades if 'mfe' in t['m']]
    mfe_bars = [t['m']['mfe_bar'] for t in trades if 'mfe_bar' in t['m']]
    maes = [t['m']['mae'] for t in trades if 'mae' in t['m']]
    mae_at_peaks = [t['m']['mae_at_peak'] for t in trades if 'mae_at_peak' in t['m']]
    fan_broken_at_peak = sum(1 for t in trades if t['m'].get('fan_at_peak') in ('transition', 'inverted'))
    fan_break_bars = [t['m']['fan_break_bar'] for t in trades
                      if t['m'].get('fan_break_bar', -1) >= 0]
    stats = {
        'n': len(trades),
        'mfe_p25': percentile(mfes, 25),
        'mfe_p50': percentile(mfes, 50),
        'mfe_p75': percentile(mfes, 75),
        'mfe_bar_p50': percentile(mfe_bars, 50),
        'mfe_bar_p90': percentile(mfe_bars, 90),
        'mae_p25': percentile(maes, 25),
        'mae_p50': percentile(maes, 50),
        'mae_p75': percentile(maes, 75),
        'mae_at_peak_p50': percentile(mae_at_peaks, 50),
        'fan_broken_at_peak_pct': round(100 * fan_broken_at_peak / len(trades), 1),
        'fan_break_bar_p50': percentile(fan_break_bars, 50),
    }
    print(f"  {label:35s} n={stats['n']:>3}  "
          f"MFE p25/50/75={stats['mfe_p25']:>5.1f}/{stats['mfe_p50']:>5.1f}/{stats['mfe_p75']:>5.1f}  "
          f"MFE_bar p50/90={int(stats['mfe_bar_p50'] or 0):>2}/{int(stats['mfe_bar_p90'] or 0):>2}  "
          f"MAE p25/50/75={stats['mae_p25']:>6.1f}/{stats['mae_p50']:>6.1f}/{stats['mae_p75']:>6.1f}  "
          f"fan_brk@peak={stats['fan_broken_at_peak_pct']:>4.1f}%")
    return stats


def run_cohort(label, start, end, oanda):
    print()
    print('=' * 100)
    print(f'COHORT: {label}   window=[{start}, {end})')
    print('=' * 100)
    trades = load_trades(start, end)
    if not trades:
        print('  No trades.')
        return None
    winners = [t for t in trades if t['pnl_pips'] > 0]
    losers = [t for t in trades if t['pnl_pips'] < 0]
    print(f'  Total: {len(trades)}  Winners: {len(winners)}  Losers: {len(losers)}')

    cache = fetch_pair_candles(trades, oanda)
    pair_dfs = {p: add_emas(candles_to_df(c)) for p, c in cache.items()}

    measured = []
    for t in trades:
        df = pair_dfs.get(t['pair'])
        if df is None or df.empty:
            continue
        m = measure_trade(t, df)
        if 'error' in m:
            continue
        measured.append({**t, 'm': m})

    losers_m = [t for t in measured if t['pnl_pips'] < 0]
    winners_m = [t for t in measured if t['pnl_pips'] > 0]
    print(f'  Measured: {len(measured)} (losers {len(losers_m)}, winners {len(winners_m)})')

    print()
    print('  PATTERN BUCKETS (losers):')
    by_pat_l = defaultdict(list)
    for t in losers_m:
        by_pat_l[t['m']['pattern']].append(t)
    pat_summaries = {}
    for pat in ('never_positive', 'long_neg_then_brief_pos',
                'short_neg_then_brief_pos', 'positive_at_entry'):
        pat_summaries[f'loser_{pat}'] = summarize_pattern(f'L:{pat}', by_pat_l.get(pat, []))

    print()
    print('  PATTERN BUCKETS (winners):')
    by_pat_w = defaultdict(list)
    for t in winners_m:
        by_pat_w[t['m']['pattern']].append(t)
    for pat in ('never_positive', 'long_neg_then_brief_pos',
                'short_neg_then_brief_pos', 'positive_at_entry'):
        pat_summaries[f'winner_{pat}'] = summarize_pattern(f'W:{pat}', by_pat_w.get(pat, []))

    return {
        'cohort': label,
        'window': [start, end],
        'n_total': len(measured),
        'n_winners': len(winners_m),
        'n_losers': len(losers_m),
        'pattern_summaries': pat_summaries,
        'trades': [{**{k: v for k, v in t.items() if k not in ('entry_time', 'exit_time')},
                    'entry_time': t['entry_time'].isoformat(),
                    'exit_time': t['exit_time'].isoformat()}
                   for t in measured],
    }


def recommend_thresholds(cohort_results):
    """Use the post_tune cohort failed-rally losers to recommend candidate values."""
    print()
    print('=' * 100)
    print('DATA-DRIVEN CANDIDATE VALUES (from post_tune cohort, failed-rally-pattern losers)')
    print('=' * 100)
    pt = cohort_results.get('post_tune', {})
    psum = pt.get('pattern_summaries', {})

    # Combine the two brief-positive failed-rally buckets — both are "had a rally, lost"
    long_pos = psum.get('loser_long_neg_then_brief_pos') or {}
    short_pos = psum.get('loser_short_neg_then_brief_pos') or {}
    np_loser = psum.get('loser_never_positive') or {}

    rally_min_p25 = []
    for s in (long_pos, short_pos):
        if s.get('mfe_p25') is not None:
            rally_min_p25.append(s['mfe_p25'])
    rally_min_candidate = min(rally_min_p25) if rally_min_p25 else None

    mfe_bar_p90s = []
    for s in (long_pos, short_pos):
        if s.get('mfe_bar_p90') is not None:
            mfe_bar_p90s.append(s['mfe_bar_p90'])
    arm_window_candidate = max(mfe_bar_p90s) if mfe_bar_p90s else None

    np_mae_p25 = np_loser.get('mae_p25')

    print(f'  rally_min_pips     candidate = {rally_min_candidate}    '
          f'(P25 MFE across both failed-rally buckets — "real rally" floor)')
    print(f'  arm_window_bars    candidate = {arm_window_candidate}   '
          f'(P90 MFE_bar — covers when rallies actually peak)')
    print(f'  hard_cut_mae_pips  candidate = {np_mae_p25}     '
          f'(P25 MAE of never-positive losers — early hard-cut threshold)')
    print()
    print('  Winner-kill risk check:')
    for k in ('winner_long_neg_then_brief_pos', 'winner_short_neg_then_brief_pos'):
        s = psum.get(k) or {}
        if s.get('n'):
            print(f'    {k}: n={s["n"]}, MFE p50={s.get("mfe_p50")} — '
                  f'these are winners that ALSO had brief-positive pattern. '
                  f'Threshold must exclude them.')

    return {
        'rally_min_pips': rally_min_candidate,
        'arm_window_bars': arm_window_candidate,
        'hard_cut_mae_pips': np_mae_p25,
    }


def main():
    oanda = OandaClient()
    results = {}
    for label, win in COHORTS.items():
        results[label] = run_cohort(label, win['start'], win['end'], oanda)

    recommended = recommend_thresholds(results)

    out = os.path.join(HERE,
                       f'failed_rally_discovery_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.json')
    with open(out, 'w') as f:
        json.dump({'cohorts': results, 'recommended': recommended},
                  f, indent=2, default=str)
    print()
    print(f'Full JSON: {out}')


if __name__ == '__main__':
    main()
