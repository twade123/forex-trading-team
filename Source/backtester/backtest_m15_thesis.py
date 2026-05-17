#!/usr/bin/env python3
"""
M15 Thesis Backtester — the COMPLETE thesis as Tim described it.

THE EMA FAN EXPANSION THESIS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. TRIGGER: E21 crosses E55
2. E100 BECOMES SUPPORT/RESISTANCE:
   - BUY: E100 below candles = support floor
   - SELL: E100 above candles = resistance ceiling
3. FULL FAN ORDER:
   - BUY: price > E21 > E55 > E100 (E100 on bottom/outside)
   - SELL: price < E21 < E55 < E100 (E100 on top/outside)
4. FAN SEPARATION = E100 to E21 (the total width of ALL three EMAs)
   This must be GROWING bar over bar.
5. BB EXPANSION happening SIMULTANEOUSLY with EMA fan separation.

Entry: When 2-5 all align. The fan is ordered, E100 is on the right side,
separation is growing, and BBs are expanding with it.

THE COUNTER-TREND REVERSAL:
━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Fan was ordered but now collapsing (peaked/contracting)
2. Momentum exhausted (RSI+Stoch extreme opposite trend)
3. Structure confirms (wicks rejecting, bodies shrinking)

Usage:
    python -m backtester.backtest_m15_thesis --pairs EUR_USD
    python -m backtester.backtest_m15_thesis  # all 13 pairs
"""

import sys
import json
import argparse
import logging
import time as _time
from pathlib import Path
from collections import defaultdict

import pandas as pd

_source_dir = str(Path(__file__).resolve().parent.parent)
_trading_bot_dir = str(Path(__file__).resolve().parent.parent.parent)
_backtester_dir = str(Path(__file__).resolve().parent)
for p in [_source_dir, _trading_bot_dir, _backtester_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from backtester.data_fetcher import fetch_candles
from backtester.ema_separation import generate_market_picture, calculate_ema
from backtester.candle_structure import analyze_candle_structure
from backtester.indicators import atr as compute_atr_series

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

ALL_PAIRS = [
    'EUR_USD', 'GBP_USD', 'USD_JPY', 'EUR_JPY', 'GBP_JPY',
    'AUD_USD', 'NZD_USD', 'USD_CAD', 'USD_CHF', 'EUR_GBP',
    'EUR_CHF', 'EUR_AUD', 'AUD_JPY',
]
JPY_PAIRS = {'USD_JPY', 'EUR_JPY', 'GBP_JPY', 'AUD_JPY'}


def normalize_candle(c):
    mid = c.get('mid', c)
    if isinstance(mid, dict):
        return {
            'time': c.get('time', ''),
            'open': float(mid.get('o', mid.get('open', 0))),
            'high': float(mid.get('h', mid.get('high', 0))),
            'low': float(mid.get('l', mid.get('low', 0))),
            'close': float(mid.get('c', mid.get('close', 0))),
            'volume': int(c.get('volume', 0)),
        }
    return {
        'time': c.get('time', ''),
        'open': float(c.get('open', 0)),
        'high': float(c.get('high', 0)),
        'low': float(c.get('low', 0)),
        'close': float(c.get('close', 0)),
        'volume': int(c.get('volume', 0)),
    }


def price_to_pips(diff, pair):
    return diff * 100 if pair in JPY_PAIRS else diff * 10000


def get_atr(candles_flat, period=14):
    df = pd.DataFrame(candles_flat)
    if len(df) < period + 1:
        return 0
    s = compute_atr_series(df, period)
    v = s.iloc[-1]
    return 0 if pd.isna(v) else float(v)


def simulate_trade(future, direction, atr_val, pair, tp_mult, sl_mult, max_hold=40):
    if len(future) < 2 or atr_val <= 0:
        return None
    entry = future[0]['open']
    tp_d, sl_d = atr_val * tp_mult, atr_val * sl_mult
    tp_p = entry + tp_d if direction == 'buy' else entry - tp_d
    sl_p = entry - sl_d if direction == 'buy' else entry + sl_d
    mfe, mae = 0.0, 0.0

    for i, c in enumerate(future[:max_hold]):
        h, l = c['high'], c['low']
        if direction == 'buy':
            fav, adv = h - entry, entry - l
            hit_tp, hit_sl = h >= tp_p, l <= sl_p
        else:
            fav, adv = entry - l, h - entry
            hit_tp, hit_sl = l <= tp_p, h >= sl_p
        mfe, mae = max(mfe, fav), max(mae, adv)
        if hit_tp and hit_sl:
            hit_tp = (c['close'] > entry) if direction == 'buy' else (c['close'] < entry)
            hit_sl = not hit_tp
        if hit_tp:
            return {'result': 'win', 'pips': price_to_pips(tp_d, pair), 'candles': i+1, 'exit': 'tp',
                    'mfe': price_to_pips(mfe, pair), 'mae': price_to_pips(mae, pair)}
        if hit_sl:
            return {'result': 'loss', 'pips': -price_to_pips(sl_d, pair), 'candles': i+1, 'exit': 'sl',
                    'mfe': price_to_pips(mfe, pair), 'mae': price_to_pips(mae, pair)}

    ex = future[min(max_hold-1, len(future)-1)]['close']
    p = price_to_pips((ex - entry) if direction == 'buy' else (entry - ex), pair)
    return {'result': 'win' if p > 0 else 'loss', 'pips': round(p, 2), 'candles': max_hold,
            'exit': 'timeout', 'mfe': price_to_pips(mfe, pair), 'mae': price_to_pips(mae, pair)}


# ═══════════════════════════════════════════════════════════════════════
# CROSS WATCHER — tracks the developing fan story
# ═══════════════════════════════════════════════════════════════════════

class CrossWatcher:
    """
    Watches a detected EMA cross and tracks the FULL fan development:
    - E100 positioning (support for buy / resistance for sell)
    - Full fan order (price > E21 > E55 > E100 for buy, reverse for sell)
    - Fan width = abs(E21 - E100) growing bar over bar
    - BB expanding simultaneously
    """
    
    def __init__(self, cross_idx, direction):
        self.cross_idx = cross_idx
        self.direction = direction  # 'buy' or 'sell'
        self.bars = 0
        
        # Fan width history (E21 to E100, the FULL fan)
        self.fan_width_history = []
        self.bb_expand_history = []
        self.fan_ordered_history = []
        self.e100_correct_history = []
        
    def update(self, ema_data, bb_data, current_candle):
        """Feed one bar. Returns ('ready', reason) | ('watching', None) | ('abandon', reason)."""
        self.bars += 1
        
        emas = ema_data.get('current_emas', {})
        e21 = emas.get('ema21', 0)
        e55 = emas.get('ema55', 0)
        e100 = emas.get('ema100', 0)
        close = current_candle['close']
        
        fan_state = ema_data.get('fan_state', 'unknown')
        
        bb_expanding = bb_data.get('bb_expanding', False)
        bb_accel = bb_data.get('bb_acceleration', 0)
        
        if not (e21 and e55 and e100):
            return ('watching', None)
        
        # ── Compute fan metrics ──
        
        # Full fan width: distance from E21 (inside) to E100 (outside)
        fan_width = abs(e21 - e100)
        fan_width_pct = fan_width / close * 100 if close > 0 else 0
        self.fan_width_history.append(fan_width_pct)
        
        # E100 on correct side?
        if self.direction == 'buy':
            e100_correct = e100 < close  # E100 below price = support
            fan_ordered = close > e21 > e55 > e100  # Full stack
        else:
            e100_correct = e100 > close  # E100 above price = resistance
            fan_ordered = close < e21 < e55 < e100  # Full stack reversed
        
        self.e100_correct_history.append(e100_correct)
        self.fan_ordered_history.append(fan_ordered)
        self.bb_expand_history.append(bb_expanding)
        
        # ── ABANDON conditions ──
        
        # Fan collapsed / trend reversed
        if fan_state in ('contracting', 'peaked') and self.bars > 5:
            return ('abandon', f'fan_{fan_state} at bar {self.bars}')
        
        # Price crossed back through E100 (our support/resistance broke)
        if self.direction == 'buy' and close < e100:
            return ('abandon', f'price_below_E100 at bar {self.bars}')
        elif self.direction == 'sell' and close > e100:
            return ('abandon', f'price_above_E100 at bar {self.bars}')
        
        # Too long without full fan forming — backtested: 15+ bars WR drops sharply
        if self.bars >= 20:
            return ('abandon', f'stale after {self.bars} bars')
        
        # Fan width shrinking after initial expansion (fakeout)
        if len(self.fan_width_history) >= 5:
            recent_3 = self.fan_width_history[-3:]
            if all(recent_3[j] < recent_3[j-1] for j in range(1, len(recent_3))):
                # 3 consecutive bars of fan narrowing
                return ('abandon', f'fan_narrowing 3 bars at bar {self.bars}')
        
        # ── ENTRY conditions — need minimum history ──
        # Data shows 10-15 bar confirms at PF 1.48 vs 6-8 bars at PF 0.81
        if self.bars < 10:
            return ('watching', None)
        
        # THE FULL THESIS CHECK:
        
        # 1. Fan is ORDERED right now (price > E21 > E55 > E100 for buy)
        if not fan_ordered:
            return ('watching', None)
        
        # 2. E100 on correct side (support for buy, resistance for sell)
        if not e100_correct:
            return ('watching', None)
        
        # 3. Fan width GROWING — check last 3 bars are expanding AND minimum width
        if len(self.fan_width_history) < 3:
            return ('watching', None)
        
        # Minimum fan width — backtested: 0.10%+ has PF 1.17-1.86, below 0.10% is PF 1.04 (noise)
        if fan_width_pct < 0.10:
            return ('watching', None)
        
        recent_widths = self.fan_width_history[-3:]
        fan_growing = all(recent_widths[j] > recent_widths[j-1] for j in range(1, len(recent_widths)))
        
        if not fan_growing:
            return ('watching', None)
        
        # 4. BB expanding — must have been expanding for 2+ of last 3 bars
        #    (simultaneous with fan separation, not catching up later)
        if len(self.bb_expand_history) < 3:
            return ('watching', None)
        recent_bb = self.bb_expand_history[-3:]
        if sum(recent_bb) < 2:
            return ('watching', None)
        
        # 5. Fan has been ordered for at least 2 of the last 3 bars (stability)
        recent_ordered = self.fan_ordered_history[-3:]
        if sum(recent_ordered) < 2:
            return ('watching', None)
        
        # ALL CONDITIONS MET — the thesis is confirmed
        reason = (f"THESIS bars={self.bars} fan_width={fan_width_pct:.4f}% "
                 f"E100={'support' if self.direction=='buy' else 'resistance'} "
                 f"ordered={sum(self.fan_ordered_history[-3:])}/3 "
                 f"bb_accel={bb_accel:.3f} "
                 f"widths_last3=[{','.join(f'{w:.4f}' for w in recent_widths)}]")
        
        return ('ready', reason)


def run_backtest(pair, raw_candles, tp_mult=1.5, sl_mult=1.5, max_hold=40):
    candles = [normalize_candle(c) for c in raw_candles]
    window = 200
    trades = []
    stats = {
        'crosses_detected': 0,
        'efx_entered': 0,
        'efx_abandoned_fan_state': 0,
        'efx_abandoned_e100_broke': 0,
        'efx_abandoned_stale': 0,
        'efx_abandoned_fan_narrow': 0,
        'ctr_entered': 0,
        'total_candles': len(candles),
    }
    
    last_trade_idx = -20
    cooldown = 16  # 4 hours M15
    
    watcher = None
    prev_fan_state = None
    
    t0 = _time.time()
    
    for i in range(window, len(candles) - max_hold):
        win = candles[i - window:i]
        
        try:
            mkt = generate_market_picture(pair, win)
        except Exception:
            continue
        
        ema_data = mkt.get('ema', {})
        bb_data = mkt.get('bollinger', {})
        rsi_data = mkt.get('rsi', {})
        stoch_data = mkt.get('stochastic', {})
        
        fan_state = ema_data.get('fan_state', 'unknown')
        fan_dir = ema_data.get('fan_direction', 'mixed')
        
        # ── THESIS A: EMA FAN EXPANSION ──────────────────────────
        
        # Detect NEW cross
        if fan_state == 'just_crossed' and prev_fan_state != 'just_crossed' and watcher is None:
            cross_dir = 'none'
            crossovers = ema_data.get('crossovers', [])
            if crossovers:
                cross_dir = 'buy' if crossovers[-1].get('direction') == 'bullish' else 'sell'
            if cross_dir == 'none':
                emas = ema_data.get('current_emas', {})
                e21, e55 = emas.get('ema21', 0), emas.get('ema55', 0)
                if e21 and e55:
                    cross_dir = 'buy' if e21 > e55 else 'sell'
            
            if cross_dir != 'none':
                watcher = CrossWatcher(i, cross_dir)
                stats['crosses_detected'] += 1
        
        # Update watcher
        if watcher is not None:
            status, reason = watcher.update(ema_data, bb_data, candles[i-1])
            
            if status == 'ready' and i - last_trade_idx >= cooldown:
                atr_val = get_atr(win[-20:])
                if atr_val > 0:
                    future = candles[i:i + max_hold + 1]
                    result = simulate_trade(future, watcher.direction, atr_val, pair,
                                          tp_mult, sl_mult, max_hold)
                    if result:
                        result['entry_type'] = 'ema_fan_expansion'
                        result['direction'] = watcher.direction
                        result['time'] = candles[i]['time']
                        result['confirm_bars'] = watcher.bars
                        result['reason'] = reason
                        result['fan_width'] = watcher.fan_width_history[-1] if watcher.fan_width_history else 0
                        trades.append(result)
                        last_trade_idx = i
                        stats['efx_entered'] += 1
                
                watcher = None
            
            elif status == 'abandon':
                if 'fan_state' in reason or 'fan_' in reason.split()[0]:
                    if 'narrow' in reason:
                        stats['efx_abandoned_fan_narrow'] += 1
                    else:
                        stats['efx_abandoned_fan_state'] += 1
                elif 'E100' in reason:
                    stats['efx_abandoned_e100_broke'] += 1
                elif 'stale' in reason:
                    stats['efx_abandoned_stale'] += 1
                else:
                    stats['efx_abandoned_fan_state'] += 1
                watcher = None
        
        # ── THESIS B: COUNTER-TREND REVERSAL ─────────────────────
        
        if watcher is None and i - last_trade_idx >= cooldown:
            if fan_state in ('peaked', 'decelerating', 'contracting') and fan_dir in ('bullish', 'bearish'):
                rsi_val = rsi_data.get('value', 50)
                stoch_k = stoch_data.get('k', 50)
                trend_health = ema_data.get('trend_health', 0)
                
                ctr_dir = 'buy' if fan_dir == 'bearish' else 'sell'
                
                # Momentum exhaustion
                if fan_dir == 'bullish':
                    mom_exhausted = (rsi_val >= 68 and stoch_k >= 72) or rsi_val >= 75
                else:
                    mom_exhausted = (rsi_val <= 32 and stoch_k <= 28) or rsi_val <= 25
                
                # E100 role check — for CTR, price should be near E100
                emas = ema_data.get('current_emas', {})
                e100 = emas.get('ema100', 0)
                close = candles[i-1]['close']
                near_e100 = abs(close - e100) / close * 100 < 0.15 if e100 > 0 and close > 0 else False
                
                # Structure: wick pressure + body exhaustion
                try:
                    closes = [c['close'] for c in win]
                    ema_21 = calculate_ema(closes, 21)
                    ema_55 = calculate_ema(closes, 55)
                    ema_100 = calculate_ema(closes, 100)
                    cstruct = analyze_candle_structure(win, ema_21, ema_55, ema_100)
                except Exception:
                    cstruct = {}
                
                wick_pressure = cstruct.get('wick_analysis', {}).get('dominant_pressure', 'balanced')
                body_trend = cstruct.get('body_progression', {}).get('body_trend', 'unknown')
                
                structure_ok = False
                if ctr_dir == 'buy' and wick_pressure == 'buying' and body_trend in ('shrinking', 'steady'):
                    structure_ok = True
                elif ctr_dir == 'sell' and wick_pressure == 'selling' and body_trend in ('shrinking', 'steady'):
                    structure_ok = True
                
                trend_dying = (fan_state in ('peaked', 'contracting') or 
                              (fan_state == 'decelerating' and trend_health < 35))
                
                if mom_exhausted and structure_ok and trend_dying:
                    atr_val = get_atr(win[-20:])
                    if atr_val > 0:
                        future = candles[i:i + max_hold + 1]
                        result = simulate_trade(future, ctr_dir, atr_val, pair,
                                              tp_mult, sl_mult, max_hold)
                        if result:
                            result['entry_type'] = 'counter_trend_reversal'
                            result['direction'] = ctr_dir
                            result['time'] = candles[i]['time']
                            result['rsi'] = rsi_val
                            result['stoch_k'] = stoch_k
                            result['trend_health'] = trend_health
                            result['near_e100'] = near_e100
                            result['reason'] = (f"fan_{fan_state} health={trend_health} "
                                              f"rsi={rsi_val:.0f} stoch={stoch_k:.0f} "
                                              f"wick={wick_pressure} body={body_trend} "
                                              f"near_e100={near_e100}")
                            trades.append(result)
                            last_trade_idx = i
                            stats['ctr_entered'] += 1
        
        prev_fan_state = fan_state
        
        # Progress
        done = i - window
        total = len(candles) - window - max_hold
        if done % 5000 == 0 and done > 0:
            elapsed = _time.time() - t0
            logger.info(f"  {pair}: {done}/{total} candles, {len(trades)} trades, {elapsed:.0f}s")
    
    elapsed = _time.time() - t0
    logger.info(f"  {pair}: done — {len(trades)} trades in {elapsed:.1f}s")
    logger.info(f"    {stats}")
    
    return trades, stats


def print_results(all_results, tp_mult, sl_mult):
    print("\n" + "=" * 120)
    print(f"M15 FULL FAN THESIS BACKTEST — 3 YEARS — TP={tp_mult}×ATR, SL={sl_mult}×ATR")
    print(f"Entry: cross → E100 support/resistance → fan ordered (E21>E55>E100) → fan width growing → BB expanding")
    print("=" * 120)
    
    grand_trades = []
    grand_stats = defaultdict(int)
    
    for pair in ALL_PAIRS:
        if pair not in all_results:
            continue
        trades, stats = all_results[pair]
        for k, v in stats.items():
            grand_stats[k] += v
        
        if not trades:
            print(f"\n{pair}: 0 trades | {stats}")
            continue
        
        by_type = defaultdict(list)
        for t in trades:
            by_type[t['entry_type']].append(t)
        
        wins = [t for t in trades if t['result'] == 'win']
        wr = len(wins) / len(trades) * 100
        total_pips = sum(t['pips'] for t in trades)
        gw = sum(t['pips'] for t in wins) if wins else 0
        gl = abs(sum(t['pips'] for t in trades if t['result'] == 'loss')) or 0.01
        pf = gw / gl
        
        print(f"\n{'─' * 120}")
        print(f"{pair}: {len(trades)} trades | WR {wr:.1f}% | PF {pf:.2f} | {total_pips:+.1f} pips ({total_pips/len(trades):+.2f}/trade)")
        
        det = stats['crosses_detected']
        entered = stats['efx_entered']
        ab_fs = stats['efx_abandoned_fan_state']
        ab_e100 = stats['efx_abandoned_e100_broke']
        ab_stale = stats['efx_abandoned_stale']
        ab_narrow = stats['efx_abandoned_fan_narrow']
        print(f"  Cross funnel: {det} detected → {entered} entered "
              f"| abandoned: fan_state={ab_fs} e100_broke={ab_e100} stale={ab_stale} fan_narrow={ab_narrow}")
        print(f"  CTR: {stats['ctr_entered']} entered")
        
        for etype, etrades in sorted(by_type.items(), key=lambda x: -len(x[1])):
            ew = [t for t in etrades if t['result'] == 'win']
            ewr = len(ew) / len(etrades) * 100
            ep = sum(t['pips'] for t in etrades)
            egw = sum(t['pips'] for t in ew) if ew else 0
            egl = abs(sum(t['pips'] for t in etrades if t['result'] == 'loss')) or 0.01
            epf = egw / egl
            avg_mfe = sum(t['mfe'] for t in etrades) / len(etrades)
            avg_mae = sum(t['mae'] for t in etrades) / len(etrades)
            avg_hold = sum(t['candles'] for t in etrades) / len(etrades)
            exits = defaultdict(int)
            for t in etrades:
                exits[t['exit']] += 1
            exit_str = ' '.join(f"{k}={v}" for k, v in sorted(exits.items()))
            
            if etype == 'ema_fan_expansion':
                avg_confirm = sum(t.get('confirm_bars', 0) for t in etrades) / len(etrades)
                avg_fw = sum(t.get('fan_width', 0) for t in etrades) / len(etrades)
                print(f"  {etype:28s} {len(etrades):5d}t | WR {ewr:5.1f}% | PF {epf:5.2f} | {ep:+9.1f}p | "
                      f"MFE={avg_mfe:.1f} MAE={avg_mae:.1f} hold={avg_hold:.0f} | "
                      f"confirm={avg_confirm:.1f}bars fan_w={avg_fw:.4f}% | {exit_str}")
            else:
                print(f"  {etype:28s} {len(etrades):5d}t | WR {ewr:5.1f}% | PF {epf:5.2f} | {ep:+9.1f}p | "
                      f"MFE={avg_mfe:.1f} MAE={avg_mae:.1f} hold={avg_hold:.0f} | {exit_str}")
        
        # Sample trades
        for etype in ['ema_fan_expansion', 'counter_trend_reversal']:
            et = by_type.get(etype, [])
            if et:
                sample = ([t for t in et if t['result'] == 'win'][:3] + 
                          [t for t in et if t['result'] == 'loss'][:2])
                if sample:
                    print(f"  Samples ({etype}):")
                    for t in sample:
                        print(f"    {t['time'][:16]} {t['direction']:4s} → {t['result']:4s} {t['pips']:+6.1f}p "
                              f"hold={t['candles']} | {t.get('reason','')[:100]}")
        
        grand_trades.extend(trades)
    
    # Grand totals
    if grand_trades:
        print(f"\n{'=' * 120}")
        wins = [t for t in grand_trades if t['result'] == 'win']
        wr = len(wins) / len(grand_trades) * 100
        total_pips = sum(t['pips'] for t in grand_trades)
        gw = sum(t['pips'] for t in wins) if wins else 0
        gl = abs(sum(t['pips'] for t in grand_trades if t['result'] == 'loss')) or 0.01
        pf = gw / gl
        
        print(f"GRAND TOTAL: {len(grand_trades)} trades | WR {wr:.1f}% | PF {pf:.2f} | {total_pips:+.1f} pips")
        print(f"Stats: {dict(grand_stats)}")
        
        by_type = defaultdict(list)
        for t in grand_trades:
            by_type[t['entry_type']].append(t)
        
        print("\nBy thesis type:")
        for etype, etrades in sorted(by_type.items(), key=lambda x: -len(x[1])):
            ew = [t for t in etrades if t['result'] == 'win']
            ewr = len(ew) / len(etrades) * 100
            ep = sum(t['pips'] for t in etrades)
            egw = sum(t['pips'] for t in ew) if ew else 0
            egl = abs(sum(t['pips'] for t in etrades if t['result'] == 'loss')) or 0.01
            epf = egw / egl
            print(f"  {etype:28s} {len(etrades):5d} trades | WR {ewr:5.1f}% | PF {epf:5.2f} | {ep:+10.1f} pips")
        
        exits = defaultdict(int)
        for t in grand_trades:
            exits[t['exit']] += 1
        print(f"\nExits: {dict(exits)}")
        
        # Confirm bars distribution
        efx = [t for t in grand_trades if t['entry_type'] == 'ema_fan_expansion']
        if efx:
            print("\nEMA fan expansion — confirm bars:")
            for lo, hi in [(6,8), (8,10), (10,15), (15,20), (20,30)]:
                bt = [t for t in efx if lo <= t.get('confirm_bars', 0) < hi]
                if bt:
                    bwr = sum(1 for t in bt if t['result'] == 'win') / len(bt) * 100
                    bp = sum(t['pips'] for t in bt)
                    bgw = sum(t['pips'] for t in bt if t['result'] == 'win') or 0
                    bgl = abs(sum(t['pips'] for t in bt if t['result'] == 'loss')) or 0.01
                    bpf = bgw / bgl
                    print(f"  bars {lo:2d}-{hi:2d}: {len(bt):4d} trades | WR {bwr:.1f}% | PF {bpf:.2f} | {bp:+.1f} pips")
        
        # Fan width at entry distribution
        if efx:
            print("\nEMA fan expansion — fan width at entry:")
            for lo, hi in [(0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 1.0)]:
                bt = [t for t in efx if lo <= t.get('fan_width', 0) < hi]
                if bt:
                    bwr = sum(1 for t in bt if t['result'] == 'win') / len(bt) * 100
                    bp = sum(t['pips'] for t in bt)
                    bgw = sum(t['pips'] for t in bt if t['result'] == 'win') or 0
                    bgl = abs(sum(t['pips'] for t in bt if t['result'] == 'loss')) or 0.01
                    bpf = bgw / bgl
                    print(f"  width {lo:.2f}-{hi:.2f}%: {len(bt):4d} trades | WR {bwr:.1f}% | PF {bpf:.2f} | {bp:+.1f} pips")
    else:
        print(f"\nNO TRADES. Stats: {dict(grand_stats)}")


def main():
    parser = argparse.ArgumentParser(description="M15 Full Fan Thesis Backtester")
    parser.add_argument('--pairs', nargs='+', default=ALL_PAIRS)
    parser.add_argument('--tp', type=float, default=1.5)
    parser.add_argument('--sl', type=float, default=1.5)
    parser.add_argument('--max-hold', type=int, default=40)
    parser.add_argument('--from-date', default='2023-02-01')
    parser.add_argument('--save', default=None)
    args = parser.parse_args()
    
    from_time = f"{args.from_date}T00:00:00Z"
    
    pair_candles = {}
    for pair in args.pairs:
        logger.info(f"Fetching {pair} M15 from {args.from_date}...")
        try:
            raw = fetch_candles(pair, 'M15', from_time)
            logger.info(f"  {pair}: {len(raw)} candles")
            pair_candles[pair] = raw
        except Exception as e:
            logger.error(f"  {pair}: {e}")
    
    logger.info(f"\nFetched {len(pair_candles)} pairs. Running backtests...\n")
    
    all_results = {}
    for pair in args.pairs:
        if pair not in pair_candles or len(pair_candles[pair]) < 250:
            continue
        trades, stats = run_backtest(pair, pair_candles[pair], args.tp, args.sl, args.max_hold)
        all_results[pair] = (trades, stats)
    
    print_results(all_results, args.tp, args.sl)
    
    if args.save:
        save_data = {}
        for pair, (trades, stats) in all_results.items():
            clean = [{k: v for k, v in t.items()} for t in trades]
            save_data[pair] = {'trades': clean, 'stats': stats}
        with open(args.save, 'w') as f:
            json.dump(save_data, f, indent=2, default=str)
        logger.info(f"Saved to {args.save}")


if __name__ == '__main__':
    main()
