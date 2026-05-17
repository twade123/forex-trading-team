"""Strictly real-time signal backtest — NO LOOK-AHEAD BIAS.
Only uses data that would be available at fire moment:
- PREVIOUS closed M15 bar (finished before fire)
- Current M15 bar: open + current price (= entry price)
- PREVIOUS closed H1 bar
- Current H1 bar: open + current price
- No M1 noise
"""
import config, requests, sqlite3
from datetime import datetime, timedelta, timezone

config._load_from_db()
H = config.get_default_headers()


def fetch(pair, gran, t, look_back_min=120, look_forward_min=0):
    """Fetch candles. look_forward_min=0 ensures no candles after fire time."""
    from_iso = (t - timedelta(minutes=look_back_min)).strftime('%Y-%m-%dT%H:%M:%SZ')
    to_iso = t.strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        r = requests.get(f'{config.BASE_URL}/v3/instruments/{pair}/candles', headers=H,
            params={'granularity': gran, 'from': from_iso, 'to': to_iso, 'price': 'M'}, timeout=15)
        return r.json().get('candles', [])
    except Exception:
        return []


def candle_dir(c, is_sell):
    o, cl = float(c['mid']['o']), float(c['mid']['c'])
    if cl > o: return 'WITH' if not is_sell else 'AGAINST'
    if cl < o: return 'AGAINST' if not is_sell else 'WITH'
    return 'DOJI'


def compute_signals(m15, h1, entry_dt, entry_price, is_sell, pair):
    """Strictly real-time signals."""
    # Find CURRENT M15 bar (entry lives inside this bar, may still be open)
    pip = 0.01 if 'JPY' in pair else 0.0001
    current_m15 = None
    for c in m15:
        ct = datetime.fromisoformat(c['time'][:19] + '+00:00')
        if ct <= entry_dt < ct + timedelta(minutes=15):
            current_m15 = c; break
    if current_m15 is None: return None

    # Find PREVIOUS closed M15 bar (fully closed at fire time)
    prev_m15 = None
    for c in m15:
        ct = datetime.fromisoformat(c['time'][:19] + '+00:00')
        if ct + timedelta(minutes=15) <= entry_dt:
            prev_m15 = c  # keep updating; last one wins
    if prev_m15 is None: return None

    # Previous 2 M15 bars
    prev2_m15 = None
    _idx = None
    for i, c in enumerate(m15):
        ct = datetime.fromisoformat(c['time'][:19] + '+00:00')
        if ct + timedelta(minutes=15) <= entry_dt:
            _idx = i
    if _idx is not None and _idx >= 1:
        prev2_m15 = m15[_idx - 1]

    # Current H1
    current_h1 = None
    for c in h1:
        ct = datetime.fromisoformat(c['time'][:19] + '+00:00')
        if ct <= entry_dt < ct + timedelta(hours=1):
            current_h1 = c; break

    # Previous H1
    prev_h1 = None
    for c in h1:
        ct = datetime.fromisoformat(c['time'][:19] + '+00:00')
        if ct + timedelta(hours=1) <= entry_dt:
            prev_h1 = c

    # Signal 1: previous closed M15 direction matches trade direction
    prev_m15_dir_match = candle_dir(prev_m15, is_sell) == 'WITH'

    # Signal 2: 2 previous M15 bars both in direction
    prev2_both_match = False
    if prev2_m15 is not None:
        prev2_both_match = (candle_dir(prev_m15, is_sell) == 'WITH'
                            and candle_dir(prev2_m15, is_sell) == 'WITH')

    # Signal 3: current M15 intra-bar — price beyond open in trade direction
    curr_open = float(current_m15['mid']['o'])
    curr_intra_in_dir = (entry_price < curr_open) if is_sell else (entry_price > curr_open)

    # Signal 4: current M15 intra-bar strong — price >5p beyond open
    curr_intra_pips = abs(entry_price - curr_open) / pip
    curr_intra_strong = curr_intra_in_dir and curr_intra_pips >= 5

    # Signal 5: previous H1 direction matches
    prev_h1_match = False
    if prev_h1 is not None:
        prev_h1_match = candle_dir(prev_h1, is_sell) == 'WITH'

    # Signal 6: current H1 intra-bar in direction
    curr_h1_intra_in_dir = False
    if current_h1 is not None:
        h1_open = float(current_h1['mid']['o'])
        curr_h1_intra_in_dir = (entry_price < h1_open) if is_sell else (entry_price > h1_open)

    # Signal 7: combo — prev_m15 + prev_h1 both match (multi-TF closed-candle alignment)
    combo_prev_m15_h1 = prev_m15_dir_match and prev_h1_match

    # Signal 8: combo — prev_m15 + curr_m15 intra (both scales agree in dir)
    combo_prev_curr_m15 = prev_m15_dir_match and curr_intra_in_dir

    # Signal 9: combo — prev_m15 + curr_h1 intra
    combo_prev_m15_curr_h1 = prev_m15_dir_match and curr_h1_intra_in_dir

    # Signal 10: combo — curr M15 intra + curr H1 intra
    combo_curr_m15_h1_intra = curr_intra_in_dir and curr_h1_intra_in_dir

    # Signal 11: TRIPLE combo — all of above (multi-scale confirmation)
    triple = prev_m15_dir_match and curr_intra_in_dir and curr_h1_intra_in_dir

    # Signal 12: prev_m15 body size (conviction) + direction
    prev_o = float(prev_m15['mid']['o']); prev_c = float(prev_m15['mid']['c'])
    prev_h = float(prev_m15['mid']['h']); prev_l = float(prev_m15['mid']['l'])
    prev_body = abs(prev_c - prev_o); prev_rng = prev_h - prev_l
    prev_body_pct = prev_body / prev_rng if prev_rng > 0 else 0
    prev_strong_dir = prev_m15_dir_match and prev_body_pct > 0.5

    # Signal 13: prev M15 no rejection wick in trade direction
    prev_lw = (min(prev_o, prev_c) - prev_l) / prev_rng if prev_rng > 0 else 0
    prev_uw = (prev_h - max(prev_o, prev_c)) / prev_rng if prev_rng > 0 else 0
    if is_sell:
        prev_no_rejection = prev_lw < 0.50  # for SELL, long lower wick on prev bar = bounce already started
    else:
        prev_no_rejection = prev_uw < 0.50

    return {
        'prev_m15_dir': prev_m15_dir_match,
        'prev2_m15_both': prev2_both_match,
        'curr_m15_intra': curr_intra_in_dir,
        'curr_m15_intra_strong_5p': curr_intra_strong,
        'prev_h1_dir': prev_h1_match,
        'curr_h1_intra': curr_h1_intra_in_dir,
        'combo_prev_m15_h1': combo_prev_m15_h1,
        'combo_prev_curr_m15': combo_prev_curr_m15,
        'combo_prev_m15_curr_h1': combo_prev_m15_curr_h1,
        'combo_curr_m15_h1_intra': combo_curr_m15_h1_intra,
        'triple_confirm': triple,
        'prev_strong_dir': prev_strong_dir,
        'prev_no_rejection': prev_no_rejection,
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
    stats = {'60d': defaultdict(lambda: {'pass': {'w': 0, 'l': 0, 'pip': 0},
                                          'fail': {'w': 0, 'l': 0, 'pip': 0}}),
             '14d': defaultdict(lambda: {'pass': {'w': 0, 'l': 0, 'pip': 0},
                                          'fail': {'w': 0, 'l': 0, 'pip': 0}})}

    rules = ['prev_m15_dir', 'prev2_m15_both', 'curr_m15_intra', 'curr_m15_intra_strong_5p',
             'prev_h1_dir', 'curr_h1_intra',
             'combo_prev_m15_h1', 'combo_prev_curr_m15', 'combo_prev_m15_curr_h1',
             'combo_curr_m15_h1_intra', 'triple_confirm',
             'prev_strong_dir', 'prev_no_rejection']

    proc = 0
    for i, (tid, entry_iso, e_px, outcome, pnl, direction, pair, source) in enumerate(trades):
        if i % 20 == 0:
            print(f'  {i}/{len(trades)} proc={proc}', flush=True)
        entry_dt = datetime.fromisoformat(entry_iso.replace('+00:00', '+00:00'))
        m15 = fetch(pair, 'M15', entry_dt, look_back_min=120)
        h1 = fetch(pair, 'H1', entry_dt, look_back_min=60*8)
        if len(m15) < 5 or len(h1) < 2:
            continue
        is_sell = direction in ('sell', 'short')
        s = compute_signals(m15, h1, entry_dt, e_px, is_sell, pair)
        if s is None:
            continue
        proc += 1
        for rule in rules:
            side = 'pass' if s[rule] else 'fail'
            for window, matches in [('60d', True), ('14d', entry_dt >= cutoff_14)]:
                if not matches: continue
                stats[window][rule][side]['w' if outcome == 'win' else 'l'] += 1
                stats[window][rule][side]['pip'] += pnl

    con.close()
    print(f'\nProcessed {proc} trades (strictly real-time signals)\n')

    for window in ('60d', '14d'):
        print(f'=== {window.upper()} — sorted by discrimination ===')
        print('{:<30} {:>4} {:>4} {:>4} {:>8} | {:>4} {:>4} {:>4} {:>8}  disc'.format(
            'Rule', 'W+', 'L+', 'WR%', 'Pips+', 'W-', 'L-', 'WR%', 'Pips-'))
        print('-' * 100)
        ranked = []
        for rule in rules:
            d = stats[window][rule]
            p = d['pass']; f = d['fail']
            pt = p['w'] + p['l']; ft = f['w'] + f['l']
            if pt == 0 or ft == 0:
                continue
            pwr = 100 * p['w'] / pt; fwr = 100 * f['w'] / ft
            ranked.append((pwr - fwr, rule, p, f, pwr, fwr, pt, ft))
        ranked.sort(reverse=True)
        for discrim, rule, p, f, pwr, fwr, pt, ft in ranked:
            print('{} {:>4} {:>4} {:>3.0f}%  {:+7.1f} | {:>4} {:>4} {:>3.0f}%  {:+7.1f}   {:+.0f}%'.format(
                rule.ljust(28), p['w'], p['l'], pwr, p['pip'], f['w'], f['l'], fwr, f['pip'], discrim))
        print()


if __name__ == '__main__':
    main()
