#!/usr/bin/env python3
"""Sweep TP/SL multipliers on cached EUR_USD M15 data."""
import sys, json
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtester.backtest_m15_thesis import normalize_candle, simulate_trade, get_atr, read_market_story

# We'll load candles once, run market_story once, then sweep TP/SL on the same signals
from backtester.data_fetcher import fetch_candles

PAIR = 'EUR_USD'
FROM = '2023-02-01T00:00:00Z'
WINDOW = 200
STEP = 4
COOLDOWN = 8
MAX_HOLD = 40
THRESHOLD = 40

print(f"Fetching {PAIR} M15...")
raw = fetch_candles(PAIR, 'M15', FROM)
candles = [normalize_candle(c) for c in raw]
print(f"  {len(candles)} candles")

# Collect all signals with their ATR and future candles
print("Scanning for thesis signals...")
signals = []
for i in range(WINDOW, len(candles) - MAX_HOLD, STEP):
    win = candles[i - WINDOW:i]
    try:
        story = read_market_story(PAIR, win)
    except Exception:
        continue
    if not story.get('has_opportunity') or story['opportunity_score'] < THRESHOLD:
        continue
    if story['direction'] not in ('buy', 'sell'):
        continue

    atr_val = get_atr(win[-20:])
    if atr_val <= 0:
        continue

    signals.append({
        'idx': i,
        'direction': story['direction'],
        'entry_type': story['entry_type'],
        'score': story['opportunity_score'],
        'atr': atr_val,
        'future': candles[i:i + MAX_HOLD + 1],
    })

print(f"  {len(signals)} raw signals found")

# Sweep
tp_range = [0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 2.5, 3.0]
sl_range = [1.0, 1.5, 2.0, 2.5, 3.0]
thresholds = [40, 50, 60]

print(f"\n{'TP':>5} {'SL':>5} {'THR':>4} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Pips':>10} {'Pips/T':>8} {'CTR_WR':>7} {'BRK_WR':>7} {'EFX_cnt':>7}")
print("─" * 95)

best = {'pf': 0, 'combo': ''}

for thresh in thresholds:
    for tp in tp_range:
        for sl in sl_range:
            trades = []
            last_idx = -COOLDOWN
            for sig in signals:
                if sig['score'] < thresh:
                    continue
                if sig['idx'] - last_idx < COOLDOWN:
                    continue
                r = simulate_trade(sig['future'], sig['direction'], sig['atr'], PAIR, tp, sl, MAX_HOLD)
                if r:
                    r['entry_type'] = sig['entry_type']
                    trades.append(r)
                    last_idx = sig['idx']

            if len(trades) < 20:
                continue

            wins = [t for t in trades if t['result'] == 'win']
            wr = len(wins) / len(trades) * 100
            gw = sum(t['pips'] for t in wins) if wins else 0
            gl = abs(sum(t['pips'] for t in trades if t['result'] == 'loss')) or 0.01
            pf = gw / gl
            total = sum(t['pips'] for t in trades)
            avg = total / len(trades)

            by_type = defaultdict(list)
            for t in trades:
                by_type[t['entry_type']].append(t)
            ctr = by_type.get('counter_trend_reversal', [])
            brk = by_type.get('breakout', [])
            efx = by_type.get('ema_fan_expansion', [])
            ctr_wr = (sum(1 for t in ctr if t['result'] == 'win') / len(ctr) * 100) if ctr else 0
            brk_wr = (sum(1 for t in brk if t['result'] == 'win') / len(brk) * 100) if brk else 0

            marker = ""
            if pf > best['pf'] and len(trades) >= 50:
                best = {'pf': pf, 'combo': f"TP={tp} SL={sl} THR={thresh}"}
                marker = " ★"

            print(f"{tp:5.1f} {sl:5.1f} {thresh:4d} {len(trades):7d} {wr:6.1f}% {pf:7.2f} {total:+10.1f} {avg:+8.2f} {ctr_wr:6.1f}% {brk_wr:6.1f}% {len(efx):7d}{marker}")

print(f"\n★ Best PF (≥50 trades): {best['combo']} → PF {best['pf']:.2f}")
