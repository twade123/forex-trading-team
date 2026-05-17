"""60-day snipe signal backtest. Temporary analysis script.
Tests multiple candle-based rules for their ability to discriminate wins vs losses.
Scope: all snipe_direct and scout trades in last 60 days across all pairs.
"""
import config, requests, sqlite3, json
from datetime import datetime, timedelta, timezone

config._load_from_db()
H = config.get_default_headers()


def fetch(pair, gran, t, look_back_min=180, look_forward_min=5):
    from_iso = (t - timedelta(minutes=look_back_min)).strftime('%Y-%m-%dT%H:%M:%SZ')
    to_iso = (t + timedelta(minutes=look_forward_min)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        r = requests.get(f'{config.BASE_URL}/v3/instruments/{pair}/candles', headers=H,
            params={'granularity': gran, 'from': from_iso, 'to': to_iso, 'price': 'M'}, timeout=10)
        return r.json().get('candles', [])
    except Exception:
        return []


def candle_info(c):
    o, h, l, cl = float(c['mid']['o']), float(c['mid']['h']), float(c['mid']['l']), float(c['mid']['c'])
    body = abs(cl - o); rng = h - l
    return {
        'o': o, 'h': h, 'l': l, 'c': cl,
        'dir': 'bull' if cl > o else 'bear' if cl < o else 'doji',
        'body_pct': body / rng if rng > 0 else 0,
        'body': body,
        'rng': rng,
        'lw_pct': (min(o, cl) - l) / rng if rng > 0 else 0,
        'uw_pct': (h - max(o, cl)) / rng if rng > 0 else 0,
    }


def compute_signals(m15_candles, h1_candles, entry_dt, is_sell):
    entry_idx = None
    for i, c in enumerate(m15_candles):
        ct = datetime.fromisoformat(c['time'][:19] + '+00:00')
        if ct <= entry_dt < ct + timedelta(minutes=15):
            entry_idx = i; break
    if entry_idx is None or entry_idx < 3:
        return None
    in_dir = 'bear' if is_sell else 'bull'

    entry = candle_info(m15_candles[entry_idx])
    prior = candle_info(m15_candles[entry_idx - 1])
    prior2 = candle_info(m15_candles[entry_idx - 2])
    prior3 = candle_info(m15_candles[entry_idx - 3])

    # Signal A: entry candle closes in trade direction
    sA = entry['dir'] == in_dir

    # Signal B: rejection wick against direction (hammer for SELL, shooting star for BUY)
    if is_sell:
        sB_rejection = entry['lw_pct'] > 0.50 and entry['body_pct'] < 0.40
    else:
        sB_rejection = entry['uw_pct'] > 0.50 and entry['body_pct'] < 0.40

    # Signal C: 2+ of last 3 closed candles in direction
    last3_dirs = [prior3['dir'], prior2['dir'], prior['dir']]
    sC_prior_confirm = sum(1 for d in last3_dirs if d == in_dir) >= 2

    # Signal D: body progression — bodies growing in direction (momentum)
    bodies_in_dir = []
    for c in [prior3, prior2, prior]:
        bodies_in_dir.append(c['body'] if c['dir'] == in_dir else -c['body'])
    # Positive and growing sum = momentum building
    sD_momentum = bodies_in_dir[-1] > 0 and sum(bodies_in_dir) > 0

    # Signal E: NO shrinking bodies over last 3 (would indicate exhaustion)
    recent_bodies = [prior3['body'], prior2['body'], prior['body']]
    sE_not_exhausting = not (recent_bodies[0] > recent_bodies[1] > recent_bodies[2])

    # Signal F: H1 candle direction aligned
    sF_h1_aligned = False
    if h1_candles:
        h1_idx = None
        for i, c in enumerate(h1_candles):
            ct = datetime.fromisoformat(c['time'][:19] + '+00:00')
            if ct <= entry_dt < ct + timedelta(hours=1):
                h1_idx = i; break
        if h1_idx is not None:
            h1_cur = candle_info(h1_candles[h1_idx])
            sF_h1_aligned = h1_cur['dir'] == in_dir

    # Signal G: prior closed M15 candle strongly in direction (body > 50% of range)
    sG_prior_strong = prior['dir'] == in_dir and prior['body_pct'] > 0.5

    # Signal H: no hammer/shooting-star in last 2 candles (preceding reversal warning)
    preceding_rejection = False
    for c in [prior, prior2]:
        if is_sell:
            if c['lw_pct'] > 0.50 and c['body_pct'] < 0.40:
                preceding_rejection = True
        else:
            if c['uw_pct'] > 0.50 and c['body_pct'] < 0.40:
                preceding_rejection = True
    sH_no_preceding_rejection = not preceding_rejection

    return {
        'A_candle_dir': sA,
        'B_no_rejection_wick': not sB_rejection,
        'C_prior_confirm': sC_prior_confirm,
        'D_momentum': sD_momentum,
        'E_not_exhausting': sE_not_exhausting,
        'F_h1_aligned': sF_h1_aligned,
        'G_prior_strong': sG_prior_strong,
        'H_no_preceding_rejection': sH_no_preceding_rejection,
    }


def main():
    con = sqlite3.connect('~/Jarvis/Database/v2/trading_forex.db')
    trades = con.execute("""
        SELECT id, entry_time, outcome, pnl_pips, direction, pair, source
        FROM live_trades
        WHERE source IN ('snipe_direct','scout')
          AND entry_time >= datetime('now', '-60 days')
          AND outcome IN ('win','loss')
        ORDER BY entry_time
    """).fetchall()
    print(f'Loaded {len(trades)} trades for analysis')

    from collections import defaultdict
    cutoff_14 = datetime.now(timezone.utc) - timedelta(days=14)
    b_all = defaultdict(lambda: {'w': 0, 'l': 0, 'pip': 0})
    b_14 = defaultdict(lambda: {'w': 0, 'l': 0, 'pip': 0})
    per_rule_all = defaultdict(lambda: {'pass': {'w': 0, 'l': 0, 'pip': 0},
                                         'fail': {'w': 0, 'l': 0, 'pip': 0}})

    rules = ['A_candle_dir', 'B_no_rejection_wick', 'C_prior_confirm', 'D_momentum',
             'E_not_exhausting', 'F_h1_aligned', 'G_prior_strong', 'H_no_preceding_rejection']

    proc = 0
    for i, (tid, entry_iso, outcome, pnl, direction, pair, source) in enumerate(trades):
        if i % 30 == 0:
            print(f'  {i}/{len(trades)} (processed={proc})', flush=True)
        entry_dt = datetime.fromisoformat(entry_iso.replace('+00:00', '+00:00'))
        m15 = fetch(pair, 'M15', entry_dt)
        h1 = fetch(pair, 'H1', entry_dt, look_back_min=60 * 10)
        if len(m15) < 5:
            continue
        is_sell = direction in ('sell', 'short')
        s = compute_signals(m15, h1, entry_dt, is_sell)
        if s is None:
            continue
        proc += 1

        for rule in rules:
            side = 'pass' if s[rule] else 'fail'
            per_rule_all[rule][side]['w' if outcome == 'win' else 'l'] += 1
            per_rule_all[rule][side]['pip'] += pnl

        # Combined rules
        combos = {
            'A_only': s['A_candle_dir'],
            'A_AND_B': s['A_candle_dir'] and s['B_no_rejection_wick'],
            'A_AND_C': s['A_candle_dir'] and s['C_prior_confirm'],
            'A_AND_D': s['A_candle_dir'] and s['D_momentum'],
            'A_AND_F': s['A_candle_dir'] and s['F_h1_aligned'],
            'A_AND_H': s['A_candle_dir'] and s['H_no_preceding_rejection'],
            'ALL_A_B_C': s['A_candle_dir'] and s['B_no_rejection_wick'] and s['C_prior_confirm'],
            'A_B_F': s['A_candle_dir'] and s['B_no_rejection_wick'] and s['F_h1_aligned'],
            'A_B_H': s['A_candle_dir'] and s['B_no_rejection_wick'] and s['H_no_preceding_rejection'],
        }
        for name, pasres in combos.items():
            key = (name, 'WITH' if pasres else 'AGAINST')
            b_all[key][outcome[0]] += 1
            b_all[key]['pip'] += pnl
            if entry_dt >= cutoff_14:
                b_14[key][outcome[0]] += 1
                b_14[key]['pip'] += pnl

    con.close()

    def report(bmap, title):
        print(f'\n=== {title} ===')
        print('{:<25} {:>4} {:>4} {:>4} {:>8}'.format('Rule / side', 'W', 'L', 'WR%', 'NetPips'))
        print('-' * 55)
        names = sorted({k[0] for k in bmap})
        for name in names:
            for side in ('WITH', 'AGAINST'):
                d = bmap.get((name, side), {'w': 0, 'l': 0, 'pip': 0})
                t = d['w'] + d['l']
                if t == 0:
                    continue
                wr = 100 * d['w'] / t
                print('{} {} {:>4} {:>4} {:>3.0f}%  {:+7.1f}'.format(
                    name.ljust(20), side.ljust(3), d['w'], d['l'], wr, d['pip']))

    print(f'\nProcessed {proc} trades across all rules\n')
    print('=== INDIVIDUAL RULES (pass=signal met, fail=signal not met) — 60 days ===')
    print('{:<30} {:>4} {:>4} {:>4} {:>8}'.format('Rule / side', 'W', 'L', 'WR%', 'NetPips'))
    print('-' * 65)
    for rule in rules:
        for side in ('pass', 'fail'):
            d = per_rule_all[rule][side]
            t = d['w'] + d['l']
            if t == 0:
                continue
            wr = 100 * d['w'] / t
            print('{} {} {:>4} {:>4} {:>3.0f}%  {:+7.1f}'.format(
                rule.ljust(25), side.ljust(4), d['w'], d['l'], wr, d['pip']))

    report(b_all, f'COMBINED RULES 60-DAY')
    report(b_14, 'COMBINED RULES LAST 14 DAYS')


if __name__ == '__main__':
    main()
