"""
LOSS PHASE INDICATOR AUDIT.

For every post-tune loss (since 2026-04-17, scout + snipe_direct), walk bar-by-bar
and identify three phase transitions:
    PHASE A — Entry
    PHASE B — First positive close (the brief positive)
    PHASE C — Reversal back to negative (close goes back below entry after B)
    PHASE D — Exit

At each phase, compute and report:
    Price relative to EMA21/E55/E100 (in pips)
    Distance from EMA21 (in ATR units)
    Fan ordering (in trade direction / against / mixed)
    BB width (pips), BB expanding/contracting
    ATR (pips)
    RSI, Stoch K/D
    Bars-since-entry counter
    M15 candle close vs entry (signed pips)

NO RULE PROPOSALS. Just structured data so we can see the pattern.
"""
from __future__ import annotations

import sys, os, sqlite3, json
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)
from oanda_client import OandaClient

DB = '~/Jarvis/Database/v2/trading_forex.db'
SINCE = '2026-04-17'  # post-tune cutoff


def parse_iso(s):
    s = s.replace('Z','').rstrip()
    if '.' in s:
        b, f = s.split('.', 1); s = f"{b}.{f[:6]}"
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def to_et(dt): return (dt - timedelta(hours=4)).strftime('%m-%d %H:%M')


def ema_series(closes, period):
    if len(closes) < period:
        return [None]*len(closes)
    out = [None]*(period-1)
    e = sum(closes[:period])/period
    out.append(e)
    k = 2/(period+1)
    for v in closes[period:]:
        e = v*k + e*(1-k)
        out.append(e)
    return out


def rsi_series(closes, period=14):
    if len(closes) < period+1:
        return [None]*len(closes)
    out = [None]*period
    gains = [max(0, closes[i]-closes[i-1]) for i in range(1, period+1)]
    losses = [max(0, closes[i-1]-closes[i]) for i in range(1, period+1)]
    avg_g = sum(gains)/period; avg_l = sum(losses)/period
    rs = avg_g/avg_l if avg_l > 0 else 100
    out.append(100 - 100/(1+rs))
    for i in range(period+1, len(closes)):
        g = max(0, closes[i]-closes[i-1])
        l = max(0, closes[i-1]-closes[i])
        avg_g = (avg_g*(period-1) + g)/period
        avg_l = (avg_l*(period-1) + l)/period
        rs = avg_g/avg_l if avg_l > 0 else 100
        out.append(100 - 100/(1+rs))
    return out


def stoch_series(highs, lows, closes, k_period=14, d_period=3):
    out_k = [None]*len(closes)
    out_d = [None]*len(closes)
    for i in range(k_period-1, len(closes)):
        hi = max(highs[i-k_period+1:i+1])
        lo = min(lows[i-k_period+1:i+1])
        out_k[i] = (closes[i]-lo)/(hi-lo)*100 if hi > lo else 50
    for i in range(k_period+d_period-2, len(closes)):
        out_d[i] = sum(out_k[i-d_period+1:i+1])/d_period
    return out_k, out_d


def atr_series(highs, lows, closes, period=14):
    out = [None]*len(closes)
    if len(closes) < period+1: return out
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    a = sum(trs[:period])/period
    out[period] = a
    for i in range(period+1, len(closes)):
        a = (a*(period-1) + trs[i-1])/period
        out[i] = a
    return out


def bb_series(closes, period=20, std=2):
    import statistics
    upper = [None]*len(closes)
    lower = [None]*len(closes)
    mid = [None]*len(closes)
    for i in range(period-1, len(closes)):
        window = closes[i-period+1:i+1]
        m = sum(window)/period
        sd = statistics.pstdev(window)
        mid[i] = m; upper[i] = m + std*sd; lower[i] = m - std*sd
    return upper, mid, lower


def fan_state(e21, e55, e100, is_buy):
    if e21 is None or e55 is None or e100 is None: return '?'
    if is_buy:
        if e21 > e55 > e100: return 'WITH'
        if e21 < e55 < e100: return 'AGAINST'
        return 'MIXED'
    else:
        if e21 < e55 < e100: return 'WITH'
        if e21 > e55 > e100: return 'AGAINST'
        return 'MIXED'


def main():
    print("="*100)
    print(f"LOSS PHASE INDICATOR AUDIT — all post-tune losses since {SINCE}")
    print("="*100)

    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    losses = conn.execute(f"""
        SELECT id, pair, direction, entry_price, source, entry_time, exit_time,
               pnl_pips, COALESCE(exit_trigger,'') exit_trigger
        FROM live_trades
        WHERE status='closed' AND exit_time >= '{SINCE}'
          AND source IN ('scout','snipe_direct')
          AND pnl_pips < 0
        ORDER BY exit_time
    """).fetchall()
    conn.close()
    print(f"\n{len(losses)} post-tune losses\n")

    oanda = OandaClient()
    cache: Dict[str, List[dict]] = {}

    # Pre-fetch enough M15 history per pair for EMA100 + buffer
    by_pair: Dict[str, List] = defaultdict(list)
    for r in losses:
        by_pair[r['pair']].append(r)
    for pair, trs in by_pair.items():
        earliest = min(parse_iso(t['entry_time']) for t in trs)
        latest = max(parse_iso(t['exit_time']) for t in trs)
        candles = oanda.fetch_candles_range(
            instrument=pair, granularity='M15',
            from_time=earliest - timedelta(hours=40),  # ~160 bars history for EMA100
            to_time=latest + timedelta(hours=2),
            price='M',
        )
        cache[pair] = [c for c in candles if c.get('complete', True)]

    rows_out = []
    for r in losses:
        pair = r['pair']; pip = 0.01 if 'JPY' in pair else 0.0001
        entry = r['entry_price']; is_buy = r['direction']=='buy'
        entry_dt = parse_iso(r['entry_time']); exit_dt = parse_iso(r['exit_time'])
        candles = cache[pair]
        closes = [float(c['mid']['c']) for c in candles]
        highs = [float(c['mid']['h']) for c in candles]
        lows = [float(c['mid']['l']) for c in candles]
        e21s = ema_series(closes, 21)
        e55s = ema_series(closes, 55)
        e100s = ema_series(closes, 100)
        atrs = atr_series(highs, lows, closes, 14)
        rsis = rsi_series(closes, 14)
        sk, sd = stoch_series(highs, lows, closes)
        bbu, bbm, bbl = bb_series(closes, 20, 2)

        # Find phase indices
        entry_idx = None
        for i, c in enumerate(candles):
            cdt = parse_iso(c['time'])
            if cdt + timedelta(minutes=15) > entry_dt and cdt < exit_dt:
                entry_idx = i; break
        if entry_idx is None: continue
        first_pos_idx = None
        reversal_idx = None  # bar where close goes back negative AFTER first_pos
        last_idx = entry_idx
        for i in range(entry_idx, len(candles)):
            cdt = parse_iso(candles[i]['time'])
            if cdt >= exit_dt: break
            last_idx = i
            cp = (closes[i]-entry)/pip if is_buy else (entry-closes[i])/pip
            if cp > 0 and first_pos_idx is None:
                first_pos_idx = i
            if first_pos_idx is not None and i > first_pos_idx and cp < 0 and reversal_idx is None:
                reversal_idx = i

        bars_neg_before_first_pos = (first_pos_idx - entry_idx) if first_pos_idx is not None else None
        had_pos_then_collapse = (first_pos_idx is not None and reversal_idx is not None
                                  and (last_idx - reversal_idx) >= 1)

        def snap(i, label):
            if i is None or i >= len(candles): return None
            cl = closes[i]
            cp = (cl-entry)/pip if is_buy else (entry-cl)/pip
            e21 = e21s[i]; e55 = e55s[i]; e100 = e100s[i]
            atrv = atrs[i]; rsi = rsis[i]; ks = sk[i]; ds = sd[i]
            bbw_p = ((bbu[i]-bbl[i])/pip) if (bbu[i] and bbl[i]) else None
            de21 = ((cl-e21)/pip if is_buy else (e21-cl)/pip) if e21 else None
            de21_atr = (de21 / (atrv/pip)) if (de21 is not None and atrv) else None
            return {
                'label': label, 'bar': i - entry_idx, 'time_et': to_et(parse_iso(candles[i]['time'])),
                'close_pips': round(cp, 1),
                'de21_p': round(de21, 1) if de21 is not None else None,
                'de21_atr': round(de21_atr, 2) if de21_atr is not None else None,
                'fan': fan_state(e21, e55, e100, is_buy),
                'atr_p': round(atrv/pip, 1) if atrv else None,
                'rsi': round(rsi, 1) if rsi else None,
                'stoch_k': round(ks, 0) if ks is not None else None,
                'stoch_d': round(ds, 0) if ds is not None else None,
                'bb_width_p': round(bbw_p, 1) if bbw_p else None,
            }

        snapshots = {
            'A_entry': snap(entry_idx, 'A_entry'),
            'B_first_pos': snap(first_pos_idx, 'B_first_pos'),
            'C_reversal': snap(reversal_idx, 'C_reversal'),
            'D_exit': snap(last_idx, 'D_exit'),
        }

        rows_out.append({
            'id': r['id'], 'pair': pair, 'dir': r['direction'],
            'pnl': r['pnl_pips'],
            'entry_time_et': to_et(entry_dt),
            'bars_neg_before_first_pos': bars_neg_before_first_pos,
            'first_pos_idx_offset': (first_pos_idx - entry_idx) if first_pos_idx else None,
            'had_pos_then_collapse': had_pos_then_collapse,
            'snapshots': snapshots,
        })

    # Print summary
    print(f"{'TID':<7}{'PAIR':<10}{'DIR':<5}{'PnL':>7}{'NegB4Pos':>10}{'AB→C?':>7}  PHASE-A→B→C→D snapshots")
    print('-'*100)
    for r in rows_out:
        if r['bars_neg_before_first_pos'] is None:
            print(f"  {r['id']:<7}{r['pair']:<10}{r['dir']:<5}{r['pnl']:>+7.1f}  NEVER closed positive")
            continue
        col_collapse = 'YES' if r['had_pos_then_collapse'] else 'no'
        print(f"  {r['id']:<7}{r['pair']:<10}{r['dir']:<5}{r['pnl']:>+7.1f}{r['bars_neg_before_first_pos']:>10}{col_collapse:>7}")
        for label in ['A_entry','B_first_pos','C_reversal','D_exit']:
            s = r['snapshots'][label]
            if s is None: continue
            print(f"      {s['label']:<14} bar={s['bar']:>3} {s['time_et']:<13} "
                  f"close={s['close_pips']:>+5.1f}p  dE21={s['de21_p'] if s['de21_p'] is not None else '?'}p "
                  f"({s['de21_atr'] if s['de21_atr'] is not None else '?'}ATR)  fan={s['fan']:<8} "
                  f"atr={s['atr_p']}p  rsi={s['rsi']}  stoch={s['stoch_k']}/{s['stoch_d']}  bbw={s['bb_width_p']}p")

    # Aggregate stats — focus on losses that had positive close then collapse
    print()
    print("="*100)
    print("AGGREGATE PATTERN — losses that crossed positive then collapsed back")
    print("="*100)
    crossed = [r for r in rows_out if r['had_pos_then_collapse']]
    print(f"\n{len(crossed)} of {len(rows_out)} losses (={100*len(crossed)/max(len(rows_out),1):.0f}%) closed positive then collapsed")
    if crossed:
        # Distribution of bars_neg_before_first_pos
        vals = [r['bars_neg_before_first_pos'] for r in crossed]
        print(f"\nBars negative BEFORE first positive close — distribution across {len(vals)} trades:")
        for low, high, label in [(0,0,'0 bars'),(1,1,'1 bar'),(2,2,'2 bars'),(3,3,'3 bars'),
                                  (4,5,'4-5 bars'),(6,8,'6-8 bars'),(9,15,'9-15 bars'),(16,99,'16+ bars')]:
            n = sum(1 for v in vals if low <= v <= high)
            if n: print(f"  {label:<12}: {'■'*n} ({n})")

        # Indicator snapshot at first_pos_close — common features
        print(f"\nAt the moment of first positive close (Phase B):")
        de21s = [r['snapshots']['B_first_pos']['de21_p'] for r in crossed if r['snapshots']['B_first_pos']['de21_p'] is not None]
        if de21s:
            avg = sum(de21s)/len(de21s)
            print(f"  Distance from EMA21 (signed for trade direction): avg {avg:+.1f}p, range {min(de21s):+.1f} to {max(de21s):+.1f}")
            against21 = sum(1 for v in de21s if v < 0)
            print(f"  Price still ON THE WRONG SIDE of EMA21: {against21}/{len(de21s)} ({100*against21/len(de21s):.0f}%)")
        fans_b = [r['snapshots']['B_first_pos']['fan'] for r in crossed]
        from collections import Counter
        print(f"  Fan ordering at first_pos_close: {dict(Counter(fans_b))}")
        rsis_b = [r['snapshots']['B_first_pos']['rsi'] for r in crossed if r['snapshots']['B_first_pos']['rsi'] is not None]
        if rsis_b:
            print(f"  RSI at first_pos_close: avg {sum(rsis_b)/len(rsis_b):.1f}, range {min(rsis_b):.1f}-{max(rsis_b):.1f}")
        stoch_b = [r['snapshots']['B_first_pos']['stoch_k'] for r in crossed if r['snapshots']['B_first_pos']['stoch_k'] is not None]
        if stoch_b:
            print(f"  Stoch K at first_pos_close: avg {sum(stoch_b)/len(stoch_b):.0f}, range {min(stoch_b):.0f}-{max(stoch_b):.0f}")

    out = os.path.join(HERE, f"loss_phase_audit_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json")
    with open(out,'w') as f:
        json.dump(rows_out, f, indent=2, default=str)
    print(f"\nFull JSON: {out}")


if __name__ == "__main__":
    main()
