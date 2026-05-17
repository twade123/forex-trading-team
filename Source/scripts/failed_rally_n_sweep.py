"""FAILED-RALLY LOCK — N-CONSEC-NEG SWEEP over 90 days."""
from __future__ import annotations
import sys, os, sqlite3, json
from datetime import datetime, timedelta, timezone
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)
from oanda_client import OandaClient

DB = '~/Jarvis/Database/v2/trading_forex.db'
DAYS_BACK = 90


def parse_iso(s):
    s = s.replace('Z','').rstrip()
    if '.' in s:
        b, f = s.split('.', 1); s = f"{b}.{f[:6]}"
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def to_et(dt): return (dt - timedelta(hours=4)).strftime('%m-%d %H:%M')


def simulate_n(t, bars, n_required):
    pip = 0.01 if 'JPY' in t['pair'] else 0.0001
    is_buy = t['direction']=='buy'
    entry = t['entry_price']
    actual = t['pnl_pips']
    if not bars: return {'sim': actual, 'fired': False, 'exit_bar': None}
    state = 'normal'
    consec_neg = 0
    lock_price = entry
    for i, (_dt, c) in enumerate(bars):
        h = float(c['mid']['h']); lo = float(c['mid']['l']); cl = float(c['mid']['c'])
        cp = (cl-entry)/pip if is_buy else (entry-cl)/pip
        if state == 'locked':
            hit = (lo <= lock_price) if is_buy else (h >= lock_price)
            if hit:
                return {'sim': 0.0, 'fired': True, 'exit_bar': i}
            continue
        if cp < 0:
            consec_neg += 1
            if state == 'normal' and consec_neg >= n_required:
                state = 'earned'
            elif state == 'pos_seen':
                state = 'locked'
                hit = (lo <= lock_price) if is_buy else (h >= lock_price)
                if hit:
                    return {'sim': 0.0, 'fired': True, 'exit_bar': i}
        elif cp > 0:
            consec_neg = 0
            if state == 'earned':
                state = 'pos_seen'
    return {'sim': actual, 'fired': state == 'locked', 'exit_bar': None}


def main():
    cutoff = (datetime.utcnow() - timedelta(days=DAYS_BACK)).strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    rows = conn.execute(f"""
        SELECT id, pair, direction, entry_price, source, entry_time, exit_time, pnl_pips
        FROM live_trades
        WHERE status='closed' AND exit_time >= '{cutoff}'
          AND source IN ('scout','snipe_direct')
          AND pnl_pips IS NOT NULL
        ORDER BY exit_time
    """).fetchall()
    conn.close()
    trades = [dict(r) for r in rows]
    print(f'Trades since {cutoff}: {len(trades)} (winners {sum(1 for t in trades if t["pnl_pips"]>0)}, losers {sum(1 for t in trades if t["pnl_pips"]<0)})')

    oanda = OandaClient()
    cache = {}
    by_pair = defaultdict(list)
    for t in trades: by_pair[t['pair']].append(t)
    for pair, trs in by_pair.items():
        earliest = min(parse_iso(t['entry_time']) for t in trs)
        latest = max(parse_iso(t['exit_time']) for t in trs)
        candles = oanda.fetch_candles_range(
            instrument=pair, granularity='M15',
            from_time=earliest - timedelta(hours=1),
            to_time=latest + timedelta(hours=1), price='M',
        )
        cache[pair] = [c for c in candles if c.get('complete', True)]

    def bars_in_window(t):
        out = []
        ent = parse_iso(t['entry_time']); ex = parse_iso(t['exit_time'])
        for c in cache.get(t['pair'], []):
            cdt = parse_iso(c['time']); bc = cdt + timedelta(minutes=15)
            if bc <= ent or cdt >= ex: continue
            out.append((cdt, c))
        return out

    print()
    print('='*100)
    print(f'FAILED-RALLY LOCK — N-CONSEC-NEG SWEEP over last {DAYS_BACK} days')
    print('='*100)
    print(f'\n{"N_min":>5}  {"saves":>6}  {"saved_p":>9}  {"avg_save":>9}  {"kills":>6}  {"killed_p":>10}  {"avg_kill":>9}  {"net_p":>9}  {"S:K":>6}  {"pip_ratio":>10}')
    print('-'*100)
    sweep = []
    big_kills_by_n = {}
    for n in [1, 2, 3, 4, 5]:
        saves = kills = 0
        saved_p = killed_p = 0.0
        big_kills = []
        save_ids = []
        kill_ids = []
        for t in trades:
            bars = bars_in_window(t)
            r = simulate_n(t, bars, n)
            if r['fired'] and r['exit_bar'] is not None:
                if t['pnl_pips'] < 0:
                    saves += 1
                    saved_p += (r['sim'] - t['pnl_pips'])
                    save_ids.append(t['id'])
                elif t['pnl_pips'] > 0:
                    kills += 1
                    killed_p += (r['sim'] - t['pnl_pips'])
                    kill_ids.append(t['id'])
                    if t['pnl_pips'] >= 5:
                        big_kills.append({'id': t['id'], 'pair': t['pair'], 'actual': t['pnl_pips']})
        net = saved_p + killed_p
        sk = saves / max(kills, 1)
        pr = abs(saved_p) / max(abs(killed_p), 0.1)
        avg_save = saved_p / saves if saves else 0
        avg_kill = killed_p / kills if kills else 0
        sweep.append({
            'N': n, 'saves': saves, 'saved_p': round(saved_p, 1), 'avg_save': round(avg_save, 1),
            'kills': kills, 'killed_p': round(killed_p, 1), 'avg_kill': round(avg_kill, 1),
            'net': round(net, 1), 's_to_k': round(sk, 2), 'pip_ratio': round(pr, 2),
            'big_kills': big_kills, 'save_ids': save_ids, 'kill_ids': kill_ids,
        })
        big_kills_by_n[n] = big_kills
        print(f'{n:>5}  {saves:>6}  {saved_p:>+9.1f}  {avg_save:>+9.1f}  {kills:>6}  {killed_p:>+10.1f}  {avg_kill:>+9.1f}  {net:>+9.1f}  {sk:>6.2f}  {pr:>10.2f}')

    print()
    print('='*100)
    print('Big winner kills (>=5p) by N')
    print('='*100)
    for n, bk in big_kills_by_n.items():
        print(f'  N={n}: {len(bk)} winners >=5p killed')
        for k in sorted(bk, key=lambda x: -x['actual']):
            print(f"    {k['id']:<7} {k['pair']:<10} actual=+{k['actual']:.1f}p")

    print()
    print('Best by net pip:')
    best = max(sweep, key=lambda r: r['net'])
    print(f'  N={best["N"]}: saves={best["saves"]} ({best["saved_p"]}p), kills={best["kills"]} ({best["killed_p"]}p), NET={best["net"]}p')

    out = os.path.join(HERE, f'failed_rally_n_sweep_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.json')
    with open(out, 'w') as f:
        json.dump({'days': DAYS_BACK, 'cutoff': cutoff, 'sweep': sweep}, f, indent=2, default=str)
    print(f'\nFull JSON: {out}')


if __name__ == '__main__':
    main()
