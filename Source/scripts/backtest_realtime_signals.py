"""Real-time signal backtest — signals available AT FIRE MOMENT (no candle-close wait).
Tests M1-based micro-momentum and structural signals that can gate snipes in real-time.

Core hypothesis: a move-exhaustion signal exists in the M1 micro-structure
(last 3-10 M1 bars) that predicts whether the snipe is catching a continuation
or a bounce.
"""
import config, requests, sqlite3, json
from datetime import datetime, timedelta, timezone

config._load_from_db()
H = config.get_default_headers()


def fetch(pair, gran, t, look_back_min=120, look_forward_min=2):
    from_iso = (t - timedelta(minutes=look_back_min)).strftime('%Y-%m-%dT%H:%M:%SZ')
    to_iso = (t + timedelta(minutes=look_forward_min)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        r = requests.get(f'{config.BASE_URL}/v3/instruments/{pair}/candles', headers=H,
            params={'granularity': gran, 'from': from_iso, 'to': to_iso, 'price': 'M'}, timeout=15)
        return r.json().get('candles', [])
    except Exception:
        return []


def compute_rsi(closes, period=14):
    if len(closes) <= period:
        return None
    gains = losses = 0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0: gains += diff
        else: losses -= diff
    avg_gain = gains / period; avg_loss = losses / period
    vals = []
    for i in range(period, len(closes)):
        diff = closes[i] - closes[i - 1]
        g = max(diff, 0); l = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        vals.append(100 - 100 / (1 + rs))
    return vals


def compute_signals_realtime(m1, m15, entry_dt, entry_price, is_sell):
    """Signals available at fire time. Uses CLOSED M1 bars BEFORE entry_dt."""
    # M1 bars fully closed before fire moment
    closed_m1 = []
    for c in m1:
        ct = datetime.fromisoformat(c['time'][:19] + '+00:00')
        if ct + timedelta(minutes=1) <= entry_dt:
            closed_m1.append(c)
    if len(closed_m1) < 20:
        return None
    recent = closed_m1[-10:]  # last 10 closed M1 bars before fire

    in_dir = 'bear' if is_sell else 'bull'
    m1_closes = [float(c['mid']['c']) for c in closed_m1]
    m1_highs = [float(c['mid']['h']) for c in closed_m1]
    m1_lows = [float(c['mid']['l']) for c in closed_m1]
    m1_opens = [float(c['mid']['o']) for c in closed_m1]

    # Direction count of last 3 M1 bars
    last3 = closed_m1[-3:]
    last3_dirs = []
    for c in last3:
        o, cl = float(c['mid']['o']), float(c['mid']['c'])
        if cl > o: last3_dirs.append('bull')
        elif cl < o: last3_dirs.append('bear')
        else: last3_dirs.append('flat')
    dir_count_3 = sum(1 for d in last3_dirs if d == in_dir)

    # Last 5 bars direction count
    last5 = closed_m1[-5:]
    last5_dirs = []
    for c in last5:
        o, cl = float(c['mid']['o']), float(c['mid']['c'])
        if cl > o: last5_dirs.append('bull')
        elif cl < o: last5_dirs.append('bear')
        else: last5_dirs.append('flat')
    dir_count_5 = sum(1 for d in last5_dirs if d == in_dir)

    # Body progression (growing in direction = momentum; shrinking = exhaustion)
    bodies_signed = []
    for c in closed_m1[-5:]:
        o, cl = float(c['mid']['o']), float(c['mid']['c'])
        body = abs(cl - o)
        signed = body if ((cl > o and not is_sell) or (cl < o and is_sell)) else -body
        bodies_signed.append(signed)
    momentum_sum = sum(bodies_signed)

    # Last-tick velocity (last 5 min price change)
    px_5min_ago = m1_closes[-6] if len(m1_closes) >= 6 else m1_closes[0]
    px_now = m1_closes[-1]
    vel_5min = px_now - px_5min_ago  # positive = up
    # For SELL we want negative velocity (price going down)
    vel_in_dir = -vel_5min if is_sell else vel_5min

    # Price vs recent 10-min extreme
    recent_high = max(m1_highs[-10:])
    recent_low = min(m1_lows[-10:])
    rng = recent_high - recent_low
    pos_in_range = (px_now - recent_low) / rng if rng > 0 else 0.5
    # For SELL: low position = at recent low = late entry
    #          high position = at recent high = early
    if is_sell:
        pos_for_dir = pos_in_range  # 0 = at low, 1 = at high
    else:
        pos_for_dir = 1 - pos_in_range

    # RSI at fire time + slope
    rsi_vals = compute_rsi(m1_closes, 14)
    rsi_now = rsi_vals[-1] if rsi_vals and len(rsi_vals) >= 1 else None
    rsi_5_ago = rsi_vals[-6] if rsi_vals and len(rsi_vals) >= 6 else None
    if rsi_now is not None and rsi_5_ago is not None:
        rsi_slope = rsi_now - rsi_5_ago
        # For SELL: we want RSI falling (slope negative). Slope positive = bounce.
        rsi_slope_in_dir = -rsi_slope if is_sell else rsi_slope
    else:
        rsi_slope_in_dir = 0
        rsi_now = 50

    # New-extreme check: did the last M1 bar make a new low (SELL) / new high (BUY) vs prior 5 bars?
    prior5_low = min(m1_lows[-6:-1]) if len(m1_lows) >= 6 else m1_lows[0]
    prior5_high = max(m1_highs[-6:-1]) if len(m1_highs) >= 6 else m1_highs[0]
    last_low = m1_lows[-1]; last_high = m1_highs[-1]
    if is_sell:
        new_extreme = last_low < prior5_low  # new low made = continuation
    else:
        new_extreme = last_high > prior5_high

    # ATR (mini) via TR of last 10 M1 bars
    trs = []
    for i in range(1, min(11, len(closed_m1))):
        prev_c = float(closed_m1[-i - 1]['mid']['c'])
        h = float(closed_m1[-i]['mid']['h']); l = float(closed_m1[-i]['mid']['l'])
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    atr_m1 = sum(trs) / len(trs) if trs else 0
    # ATR 5 bars back
    trs_old = []
    for i in range(6, min(16, len(closed_m1))):
        prev_c = float(closed_m1[-i - 1]['mid']['c'])
        h = float(closed_m1[-i]['mid']['h']); l = float(closed_m1[-i]['mid']['l'])
        trs_old.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    atr_m1_old = sum(trs_old) / len(trs_old) if trs_old else atr_m1
    atr_ratio = atr_m1 / atr_m1_old if atr_m1_old > 0 else 1

    # Last M1 candle shape (bar that just closed before fire)
    last_c = closed_m1[-1]
    lo, lh, ll, lcl = float(last_c['mid']['o']), float(last_c['mid']['h']), float(last_c['mid']['l']), float(last_c['mid']['c'])
    lbody = abs(lcl - lo); lrng = lh - ll
    lbody_pct = lbody / lrng if lrng > 0 else 0
    last_bullish = lcl > lo
    # For SELL: last M1 bullish body = warning sign
    last_against = (last_bullish and is_sell) or (not last_bullish and not is_sell)

    return {
        'dir_3of3': dir_count_3 == 3,           # all 3 last M1 in direction
        'dir_2of3': dir_count_3 >= 2,           # at least 2 of 3
        'dir_3of5': dir_count_5 >= 3,           # at least 3 of 5
        'dir_4of5': dir_count_5 >= 4,           # strong continuation
        'momentum_positive': momentum_sum > 0,    # net body progression in direction
        'vel_in_direction': vel_in_dir > 0,      # price moving in direction last 5 min
        'new_extreme_made': new_extreme,          # last bar made new low/high in direction
        'last_m1_in_direction': not last_against, # last closed M1 bar in direction
        'pos_top_half_range': pos_for_dir > 0.5, # entering in top half of recent range (early)
        'rsi_slope_positive': rsi_slope_in_dir > 0, # RSI moving in direction
        'rsi_not_extreme': (rsi_now > 25 and rsi_now < 75),  # not pinned at extreme
        'atr_expanding': atr_ratio > 1.0,        # volatility growing
    }


def main():
    con = sqlite3.connect('~/Jarvis/Database/v2/trading_forex.db')
    trades = con.execute("""
        SELECT id, entry_time, entry_price, outcome, pnl_pips, direction, pair, source
        FROM live_trades
        WHERE source IN ('snipe_direct','scout')
          AND entry_time >= datetime('now', '-60 days')
          AND outcome IN ('win','loss')
        ORDER BY entry_time
    """).fetchall()
    print(f'Loaded {len(trades)} trades')

    from collections import defaultdict
    cutoff_14 = datetime.now(timezone.utc) - timedelta(days=14)
    stats = defaultdict(lambda: {'14d': {'pass': {'w': 0, 'l': 0, 'pip': 0}, 'fail': {'w': 0, 'l': 0, 'pip': 0}},
                                  '60d': {'pass': {'w': 0, 'l': 0, 'pip': 0}, 'fail': {'w': 0, 'l': 0, 'pip': 0}}})

    rules = ['dir_3of3', 'dir_2of3', 'dir_3of5', 'dir_4of5', 'momentum_positive',
             'vel_in_direction', 'new_extreme_made', 'last_m1_in_direction',
             'pos_top_half_range', 'rsi_slope_positive', 'rsi_not_extreme', 'atr_expanding']

    proc = 0
    for i, (tid, entry_iso, e_px, outcome, pnl, direction, pair, source) in enumerate(trades):
        if i % 20 == 0:
            print(f'  {i}/{len(trades)} (proc={proc})', flush=True)
        entry_dt = datetime.fromisoformat(entry_iso.replace('+00:00', '+00:00'))
        m1 = fetch(pair, 'M1', entry_dt, look_back_min=30)
        m15 = fetch(pair, 'M15', entry_dt, look_back_min=60)
        if len(m1) < 25 or len(m15) < 3:
            continue
        is_sell = direction in ('sell', 'short')
        s = compute_signals_realtime(m1, m15, entry_dt, e_px, is_sell)
        if s is None:
            continue
        proc += 1
        for rule in rules:
            passes = s[rule]
            side = 'pass' if passes else 'fail'
            for window, cutoff_check in [('60d', True), ('14d', entry_dt >= cutoff_14)]:
                if not cutoff_check and window == '14d':
                    continue
                stats[rule][window][side]['w' if outcome == 'win' else 'l'] += 1
                stats[rule][window][side]['pip'] += pnl

    con.close()
    print(f'\nProcessed {proc} trades')

    def print_rule_table(window, title):
        print(f'\n=== {title} ===')
        print('{:<22} {:>4} {:>4} {:>4} {:>8} | {:>4} {:>4} {:>4} {:>8}  discrim'.format(
            'Rule', 'W+', 'L+', 'WR%', 'Pips+', 'W-', 'L-', 'WR%', 'Pips-'))
        print('-' * 95)
        # Sort by discriminating power (pass WR minus fail WR)
        ranked = []
        for rule in rules:
            d = stats[rule][window]
            p = d['pass']; f = d['fail']
            pt = p['w'] + p['l']; ft = f['w'] + f['l']
            if pt == 0 or ft == 0:
                continue
            pwr = 100 * p['w'] / pt; fwr = 100 * f['w'] / ft
            ranked.append((pwr - fwr, rule, p, f, pwr, fwr))
        ranked.sort(reverse=True)
        for discrim, rule, p, f, pwr, fwr in ranked:
            print('{} {:>4} {:>4} {:>3.0f}%  {:+7.1f} | {:>4} {:>4} {:>3.0f}%  {:+7.1f}   {:+.0f}%'.format(
                rule.ljust(20), p['w'], p['l'], pwr, p['pip'], f['w'], f['l'], fwr, f['pip'], discrim))

    print_rule_table('60d', 'REAL-TIME SIGNALS — 60 DAYS (sorted by discrimination)')
    print_rule_table('14d', 'REAL-TIME SIGNALS — LAST 14 DAYS')


if __name__ == '__main__':
    main()
