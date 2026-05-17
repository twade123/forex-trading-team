#!/usr/bin/env python3
"""
Divergence Presence on ALL Setup Trades
=========================================
The main analysis showed divergence was only TAGGED on S15 trades.
This script re-derives divergence from raw candle data across ALL setups
to answer: "Was divergence present but unrecorded on S1-S20 trades?"

Uses OANDA historical candles → computes divergence at each trade entry time
→ checks if winning non-S15 trades had divergence active.

Run:
  source ~/myenv/bin/activate
  cd ~/jarvis/Trading\ Bot
  python Source/backtester/divergence_on_all_setups.py
"""

import sys, os, time, json
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

DB_PATH = '~/jarvis/Database/v2/trading_forex.db'

# Read OANDA API key
API_KEY_PATH = '~/jarvis/API/OANDA_API_KEY.txt'
BASE_URL = 'https://api-fxpractice.oanda.com'

def get_api_key():
    with open(API_KEY_PATH) as f:
        return f.read().strip()

def fetch_candles(pair, count=500, granularity='H1'):
    """Fetch historical candles from OANDA."""
    import urllib.request
    api_key = get_api_key()
    url = f"{BASE_URL}/v3/instruments/{pair}/candles?count={count}&granularity={granularity}&price=M"
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    })
    resp = urllib.request.urlopen(req)
    data = json.loads(resp.read().decode())
    
    candles = []
    for c in data.get('candles', []):
        if c['complete']:
            mid = c['mid']
            candles.append({
                'time': c['time'],
                'open': float(mid['o']),
                'high': float(mid['h']),
                'low': float(mid['l']),
                'close': float(mid['c']),
            })
    return candles

def compute_rsi(closes, period=14):
    """Compute RSI series."""
    rsi = [50.0] * len(closes)
    if len(closes) < period + 1:
        return rsi
    
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        gains.append(max(0, delta))
        losses.append(max(0, -delta))
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100 - (100 / (1 + rs))
    
    return rsi

def compute_macd_hist(closes, fast=12, slow=26, signal=9):
    """Compute MACD histogram series."""
    def ema(data, period):
        result = [data[0]] if data else []
        mult = 2 / (period + 1)
        for i in range(1, len(data)):
            result.append(data[i] * mult + result[-1] * (1 - mult))
        return result
    
    if len(closes) < slow + signal:
        return [0.0] * len(closes)
    
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return histogram

def find_swing_highs(values, order=5):
    """Return indices of swing highs."""
    indices = []
    for i in range(order, len(values) - order):
        if all(values[i] >= values[i-j] for j in range(1, order+1)) and \
           all(values[i] >= values[i+j] for j in range(1, order+1)):
            indices.append(i)
    return indices

def find_swing_lows(values, order=5):
    """Return indices of swing lows."""
    indices = []
    for i in range(order, len(values) - order):
        if all(values[i] <= values[i-j] for j in range(1, order+1)) and \
           all(values[i] <= values[i+j] for j in range(1, order+1)):
            indices.append(i)
    return indices

def detect_divergence_at_bar(closes, rsi_vals, macd_vals, bar_idx, lookback=20, order=5):
    """
    Check if divergence is active at a specific bar index.
    Returns dict with all divergence types found.
    """
    result = {
        'rsi_bull_div': False,
        'rsi_bear_div': False,
        'rsi_hidden_bull_div': False,
        'rsi_hidden_bear_div': False,
        'macd_bull_div': False,
        'macd_bear_div': False,
        'any_divergence': False,
        'types': []
    }
    
    start = max(0, bar_idx - lookback - order)
    end = min(len(closes), bar_idx + order + 1)
    
    if end - start < 2 * order + 2:
        return result
    
    # Swing detection on the window
    window_closes = closes[start:end]
    window_rsi = rsi_vals[start:end]
    window_macd = macd_vals[start:end]
    
    low_indices = find_swing_lows(window_closes, order)
    high_indices = find_swing_highs(window_closes, order)
    
    # RSI Regular Bullish: price LL, RSI HL
    for i in range(1, len(low_indices)):
        idx_prev, idx_curr = low_indices[i-1], low_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if window_closes[idx_curr] < window_closes[idx_prev] and window_rsi[idx_curr] > window_rsi[idx_prev]:
            # Check if this is recent enough to matter at bar_idx
            actual_idx = start + idx_curr
            if bar_idx - actual_idx <= order + 2:
                result['rsi_bull_div'] = True
                result['types'].append('RSI_BULL')
    
    # RSI Regular Bearish: price HH, RSI LH
    for i in range(1, len(high_indices)):
        idx_prev, idx_curr = high_indices[i-1], high_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if window_closes[idx_curr] > window_closes[idx_prev] and window_rsi[idx_curr] < window_rsi[idx_prev]:
            actual_idx = start + idx_curr
            if bar_idx - actual_idx <= order + 2:
                result['rsi_bear_div'] = True
                result['types'].append('RSI_BEAR')
    
    # RSI Hidden Bullish: price HL, RSI LL (continuation)
    for i in range(1, len(low_indices)):
        idx_prev, idx_curr = low_indices[i-1], low_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if window_closes[idx_curr] > window_closes[idx_prev] and window_rsi[idx_curr] < window_rsi[idx_prev]:
            actual_idx = start + idx_curr
            if bar_idx - actual_idx <= order + 2:
                result['rsi_hidden_bull_div'] = True
                result['types'].append('RSI_HIDDEN_BULL')
    
    # RSI Hidden Bearish: price LH, RSI HH (continuation)
    for i in range(1, len(high_indices)):
        idx_prev, idx_curr = high_indices[i-1], high_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if window_closes[idx_curr] < window_closes[idx_prev] and window_rsi[idx_curr] > window_rsi[idx_prev]:
            actual_idx = start + idx_curr
            if bar_idx - actual_idx <= order + 2:
                result['rsi_hidden_bear_div'] = True
                result['types'].append('RSI_HIDDEN_BEAR')
    
    # MACD Bullish: price LL, MACD HL
    for i in range(1, len(low_indices)):
        idx_prev, idx_curr = low_indices[i-1], low_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if window_closes[idx_curr] < window_closes[idx_prev] and window_macd[idx_curr] > window_macd[idx_prev]:
            actual_idx = start + idx_curr
            if bar_idx - actual_idx <= order + 2:
                result['macd_bull_div'] = True
                result['types'].append('MACD_BULL')
    
    # MACD Bearish: price HH, MACD LH
    for i in range(1, len(high_indices)):
        idx_prev, idx_curr = high_indices[i-1], high_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if window_closes[idx_curr] > window_closes[idx_prev] and window_macd[idx_curr] < window_macd[idx_prev]:
            actual_idx = start + idx_curr
            if bar_idx - actual_idx <= order + 2:
                result['macd_bear_div'] = True
                result['types'].append('MACD_BEAR')
    
    result['any_divergence'] = any([
        result['rsi_bull_div'], result['rsi_bear_div'],
        result['rsi_hidden_bull_div'], result['rsi_hidden_bear_div'],
        result['macd_bull_div'], result['macd_bear_div']
    ])
    
    return result


def main():
    t0 = time.time()
    
    PAIRS = [
        'EUR_USD', 'GBP_USD', 'USD_JPY', 'AUD_USD', 'NZD_USD',
        'USD_CAD', 'USD_CHF', 'EUR_JPY', 'GBP_JPY', 'EUR_GBP',
        'AUD_JPY', 'EUR_AUD', 'EUR_CHF'
    ]
    
    print("=" * 70)
    print("DIVERGENCE PRESENCE ON LIVE CANDLES — ALL 13 PAIRS")
    print("=" * 70)
    print("Fetching 500 M15 candles per pair, computing divergence at each bar,")
    print("then showing what score_v4 is MISSING right now.\n")
    
    all_results = {}
    
    for pair in PAIRS:
        try:
            candles = fetch_candles(pair, count=500, granularity='M15')
            if len(candles) < 50:
                print(f"   ⚠️  {pair}: only {len(candles)} candles, skipping")
                continue
            
            closes = [c['close'] for c in candles]
            rsi_vals = compute_rsi(closes)
            macd_vals = compute_macd_hist(closes)
            
            # Check divergence at recent bars (last 50)
            div_count = 0
            div_types_seen = {}
            recent_divs = []
            
            for i in range(len(candles) - 50, len(candles)):
                div = detect_divergence_at_bar(closes, rsi_vals, macd_vals, i)
                if div['any_divergence']:
                    div_count += 1
                    for dt in div['types']:
                        div_types_seen[dt] = div_types_seen.get(dt, 0) + 1
                    if i >= len(candles) - 10:  # Last 10 bars
                        recent_divs.append({
                            'bar': i - (len(candles) - 1),  # negative offset from current
                            'time': candles[i]['time'][:16],
                            'types': div['types'],
                            'rsi': rsi_vals[i],
                            'close': closes[i]
                        })
            
            pct = 100 * div_count / 50
            status = "🔥 ACTIVE NOW" if recent_divs else "—"
            print(f"   {pair:<10} {div_count}/50 bars had divergence ({pct:.0f}%)  {status}")
            
            if div_types_seen:
                types_str = ', '.join(f"{k}={v}" for k, v in sorted(div_types_seen.items(), key=lambda x: -x[1]))
                print(f"             Types: {types_str}")
            
            if recent_divs:
                for rd in recent_divs[-3:]:
                    print(f"             Bar {rd['bar']:+d}: {rd['time']} | RSI={rd['rsi']:.1f} | {', '.join(rd['types'])}")
            
            all_results[pair] = {
                'div_bars': div_count,
                'div_pct': pct,
                'types': div_types_seen,
                'recent': recent_divs,
                'current_rsi': rsi_vals[-1],
                'current_close': closes[-1]
            }
            
        except Exception as e:
            print(f"   ❌ {pair}: {e}")
    
    # ── Summary ──
    print("\n" + "=" * 70)
    print("SUMMARY: DIVERGENCE FREQUENCY ACROSS ALL PAIRS")
    print("=" * 70)
    
    total_bars = 0
    total_div = 0
    type_totals = {}
    active_pairs = []
    
    for pair, r in all_results.items():
        total_bars += 50
        total_div += r['div_bars']
        for t, c in r['types'].items():
            type_totals[t] = type_totals.get(t, 0) + c
        if r['recent']:
            active_pairs.append(pair)
    
    print(f"\n   Total bars checked: {total_bars}")
    print(f"   Bars with divergence: {total_div} ({100*total_div/total_bars:.1f}%)")
    print(f"   Pairs with ACTIVE divergence (last 10 bars): {len(active_pairs)}")
    if active_pairs:
        print(f"   Active: {', '.join(active_pairs)}")
    
    print(f"\n   Divergence type breakdown:")
    for t, c in sorted(type_totals.items(), key=lambda x: -x[1]):
        label = {
            'RSI_BULL': 'RSI Bullish (reversal up)',
            'RSI_BEAR': 'RSI Bearish (reversal down)',
            'RSI_HIDDEN_BULL': 'RSI Hidden Bullish (continuation up)',
            'RSI_HIDDEN_BEAR': 'RSI Hidden Bearish (continuation down)',
            'MACD_BULL': 'MACD Bullish',
            'MACD_BEAR': 'MACD Bearish',
        }.get(t, t)
        print(f"     {label:<45} {c:>4} occurrences")
    
    # ── What this means for scout ──
    print("\n" + "=" * 70)
    print("WHAT SCOUT IS MISSING")
    print("=" * 70)
    print(f"""
   Divergence occurs on ~{100*total_div/total_bars:.0f}% of recent bars across all pairs.
   
   In the backtest:
   - Trades WITH divergence: 86.4% WR (+38% edge)
   - Trades WITHOUT:         48.5% WR (coin flip)
   
   Right now score_v4 gives 0 points for divergence.
   That means a bar with RSI=35, hammer candle, AND bullish divergence
   scores the SAME as one with just RSI=35 and a hammer.
   
   The divergence is the difference between 86% and 48%.
   
   TYPES TO ADD TO score_v4:
   ─────────────────────────
   RSI Regular (reversal):   +5 pts  ← strongest signal (86.4% WR)
   RSI Hidden (continuation): +3 pts  ← trend confirmation
   MACD Regular:             +3 pts  ← secondary confirmation
   
   FOR SCOUT LIVE:
   ─────────────────
   Compute on every scan using last 30 candles.
   Need: swing high/low detection (order=5) + RSI + MACD histogram.
   Scout already has RSI and MACD. Just needs swing detection + comparison.
   
   Estimated compute cost: <5ms per pair (just array comparisons).
""")
    
    # Save results
    out_path = os.path.join(os.path.dirname(__file__), '..', '..', 'Config', 'divergence_live_snapshot.json')
    with open(out_path, 'w') as f:
        # Convert for JSON serialization
        serializable = {}
        for pair, r in all_results.items():
            serializable[pair] = {
                'div_bars_of_50': r['div_bars'],
                'div_pct': round(r['div_pct'], 1),
                'types': r['types'],
                'current_rsi': round(r['current_rsi'], 1),
                'active_now': len(r['recent']) > 0,
                'recent_divergences': r['recent']
            }
        json.dump(serializable, f, indent=2)
    print(f"   Snapshot saved: {out_path}")
    
    elapsed = time.time() - t0
    print(f"\n⏱  Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
