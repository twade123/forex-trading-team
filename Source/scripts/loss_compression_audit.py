"""
LOSS COMPRESSION AUDIT — look for multi-indicator confluence around losses.

For each post-tune loss:
  - 5 bars BEFORE entry  (precursor context)
  - Entry bar
  - Bars 1-3 of trade (the brief positive moment if any)
  - First positive close bar (if exists)
  - Bar after first positive (the reversal)
  - Bar at exit

At each bar, dump:
  - EMA21, E55, E100 + their separations (fan width, fan compression %)
  - BB upper/mid/lower + width + width-trend (5-bar % change)
  - ATR + 5-bar trend
  - RSI + slope
  - Stoch K, D
  - Candle range (high-low)
  - Body size (|close-open|)
  - Body % of range
  - Wick directions

Run on: 13 collapse losses + 8 never-positive losses for comparison.
"""
from __future__ import annotations
import sys, os, sqlite3, json, statistics
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)
from oanda_client import OandaClient

DB = '~/Jarvis/Database/v2/trading_forex.db'
SINCE = '2026-04-17'


def parse_iso(s):
    s = s.replace('Z','').rstrip()
    if '.' in s:
        b, f = s.split('.', 1); s = f"{b}.{f[:6]}"
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

def to_et(dt): return (dt - timedelta(hours=4)).strftime('%m-%d %H:%M')


def ema_series(closes, period):
    if len(closes) < period: return [None]*len(closes)
    out = [None]*(period-1)
    e = sum(closes[:period])/period
    out.append(e)
    k = 2/(period+1)
    for v in closes[period:]:
        e = v*k + e*(1-k)
        out.append(e)
    return out


def bb_series(closes, period=20, std=2):
    upper=[None]*len(closes); lower=[None]*len(closes); mid=[None]*len(closes)
    for i in range(period-1, len(closes)):
        w = closes[i-period+1:i+1]
        m = sum(w)/period
        sd = statistics.pstdev(w)
        mid[i]=m; upper[i]=m+std*sd; lower[i]=m-std*sd
    return upper, mid, lower


def atr_series(highs, lows, closes, period=14):
    out=[None]*len(closes)
    if len(closes) < period+1: return out
    trs=[]
    for i in range(1,len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    a = sum(trs[:period])/period
    out[period]=a
    for i in range(period+1,len(closes)):
        a = (a*(period-1)+trs[i-1])/period
        out[i]=a
    return out


def rsi_series(closes, period=14):
    if len(closes) < period+1: return [None]*len(closes)
    out=[None]*period
    gains=[max(0,closes[i]-closes[i-1]) for i in range(1,period+1)]
    losses=[max(0,closes[i-1]-closes[i]) for i in range(1,period+1)]
    avg_g=sum(gains)/period; avg_l=sum(losses)/period
    rs = avg_g/avg_l if avg_l>0 else 100
    out.append(100-100/(1+rs))
    for i in range(period+1,len(closes)):
        g=max(0,closes[i]-closes[i-1]); l=max(0,closes[i-1]-closes[i])
        avg_g=(avg_g*(period-1)+g)/period; avg_l=(avg_l*(period-1)+l)/period
        rs = avg_g/avg_l if avg_l>0 else 100
        out.append(100-100/(1+rs))
    return out


def stoch_series(highs, lows, closes, k_period=14, d_period=3):
    out_k=[None]*len(closes); out_d=[None]*len(closes)
    for i in range(k_period-1,len(closes)):
        hi=max(highs[i-k_period+1:i+1]); lo=min(lows[i-k_period+1:i+1])
        out_k[i] = (closes[i]-lo)/(hi-lo)*100 if hi>lo else 50
    for i in range(k_period+d_period-2,len(closes)):
        out_d[i] = sum(out_k[i-d_period+1:i+1])/d_period
    return out_k, out_d


def adx_series(highs, lows, closes, period=14):
    """Wilder's ADX."""
    if len(closes) < period*2: return [None]*len(closes)
    out=[None]*len(closes)
    plus_dm = []; minus_dm = []; tr_list = []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        plus = up if up > dn and up > 0 else 0
        minus = dn if dn > up and dn > 0 else 0
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        plus_dm.append(plus); minus_dm.append(minus); tr_list.append(tr)
    # Wilder smoothing
    if len(tr_list) < period: return out
    pdm_s = sum(plus_dm[:period]); mdm_s = sum(minus_dm[:period]); tr_s = sum(tr_list[:period])
    dxs = []
    for i in range(period, len(tr_list)):
        pdi = 100*pdm_s/tr_s if tr_s>0 else 0
        mdi = 100*mdm_s/tr_s if tr_s>0 else 0
        dx = 100*abs(pdi-mdi)/(pdi+mdi) if (pdi+mdi)>0 else 0
        dxs.append(dx)
        # update
        pdm_s = pdm_s - pdm_s/period + plus_dm[i]
        mdm_s = mdm_s - mdm_s/period + minus_dm[i]
        tr_s = tr_s - tr_s/period + tr_list[i]
    # ADX = period-MA of DX
    if len(dxs) < period: return out
    adx = sum(dxs[:period])/period
    base_idx = period*2  # offset in original closes
    out[base_idx] = adx
    for i in range(period, len(dxs)):
        adx = (adx*(period-1) + dxs[i])/period
        out[base_idx + i - period + 1] = adx
    return out


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


def fan_separation_pips(e21, e55, e100, pip):
    if None in (e21, e55, e100): return None
    return (max(e21, e55, e100) - min(e21, e55, e100)) / pip


def main():
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

    oanda = OandaClient()
    cache = {}
    by_pair = defaultdict(list)
    for r in losses: by_pair[r['pair']].append(r)
    for pair, trs in by_pair.items():
        earliest = min(parse_iso(t['entry_time']) for t in trs)
        latest = max(parse_iso(t['exit_time']) for t in trs)
        candles = oanda.fetch_candles_range(
            instrument=pair, granularity='M15',
            from_time=earliest - timedelta(hours=40),
            to_time=latest + timedelta(hours=2),
            price='M',
        )
        cache[pair] = [c for c in candles if c.get('complete', True)]

    rows_collapse = []  # 13 close-positive then collapse
    rows_never = []     # 5+ never-positive

    def analyze_trade(r):
        pair = r['pair']; pip = 0.01 if 'JPY' in pair else 0.0001
        entry = r['entry_price']; is_buy = r['direction']=='buy'
        entry_dt = parse_iso(r['entry_time']); exit_dt = parse_iso(r['exit_time'])
        candles = cache[pair]
        opens = [float(c['mid']['o']) for c in candles]
        highs = [float(c['mid']['h']) for c in candles]
        lows = [float(c['mid']['l']) for c in candles]
        closes = [float(c['mid']['c']) for c in candles]
        e21s = ema_series(closes, 21)
        e55s = ema_series(closes, 55)
        e100s = ema_series(closes, 100)
        bbu, bbm, bbl = bb_series(closes, 20, 2)
        atrs = atr_series(highs, lows, closes, 14)
        rsis = rsi_series(closes, 14)
        sk, sd = stoch_series(highs, lows, closes)
        adxs = adx_series(highs, lows, closes, 14)

        # Find entry idx
        entry_idx = None
        for i, c in enumerate(candles):
            cdt = parse_iso(c['time'])
            if cdt + timedelta(minutes=15) > entry_dt and cdt < exit_dt:
                entry_idx = i; break
        if entry_idx is None: return None

        # First positive close
        first_pos = None; last_idx = entry_idx
        for i in range(entry_idx, len(candles)):
            cdt = parse_iso(candles[i]['time'])
            if cdt >= exit_dt: break
            last_idx = i
            cp = (closes[i]-entry)/pip if is_buy else (entry-closes[i])/pip
            if cp > 0 and first_pos is None:
                first_pos = i

        def snap(i, label):
            if i is None or i<0 or i>=len(candles): return None
            cl = closes[i]; o = opens[i]; h = highs[i]; l = lows[i]
            cp = (cl-entry)/pip if is_buy else (entry-cl)/pip
            e21=e21s[i]; e55=e55s[i]; e100=e100s[i]
            atrv=atrs[i]; rsi=rsis[i]; ks=sk[i]; ds=sd[i]; adx=adxs[i]
            fan = fan_state(e21,e55,e100,is_buy)
            fan_sep_p = fan_separation_pips(e21,e55,e100,pip)
            fan_sep_atr = (fan_sep_p / (atrv/pip)) if (fan_sep_p and atrv) else None
            bbw_p = ((bbu[i]-bbl[i])/pip) if (bbu[i] and bbl[i]) else None
            # 5-bar BB width trend
            bbw_5_ago = ((bbu[i-5]-bbl[i-5])/pip) if (i >= 5 and bbu[i-5] and bbl[i-5]) else None
            bbw_change_pct = ((bbw_p-bbw_5_ago)/bbw_5_ago*100) if (bbw_p and bbw_5_ago) else None
            # 5-bar ATR trend
            atr_5_ago = atrs[i-5] if i>=5 and atrs[i-5] else None
            atr_change_pct = ((atrv-atr_5_ago)/atr_5_ago*100) if (atrv and atr_5_ago) else None
            # Candle structure
            rng = h - l
            body = abs(cl - o)
            body_pct = (body/rng*100) if rng > 0 else 0
            return {
                'label': label, 'bar_offset': i - entry_idx,
                'time_et': to_et(parse_iso(candles[i]['time'])),
                'close_pips': round(cp,1),
                'fan': fan,
                'fan_sep_p': round(fan_sep_p,1) if fan_sep_p else None,
                'fan_sep_atr': round(fan_sep_atr,2) if fan_sep_atr else None,
                'bbw_p': round(bbw_p,1) if bbw_p else None,
                'bbw_5b_chg_pct': round(bbw_change_pct,0) if bbw_change_pct else None,
                'atr_p': round(atrv/pip,1) if atrv else None,
                'atr_5b_chg_pct': round(atr_change_pct,0) if atr_change_pct else None,
                'rsi': round(rsi,1) if rsi else None,
                'stoch_k': round(ks,0) if ks is not None else None,
                'stoch_d': round(ds,0) if ds is not None else None,
                'adx': round(adx,1) if adx else None,
                'candle_range_p': round(rng/pip,1),
                'body_pct': round(body_pct,0),
            }

        # Phase snaps:
        snaps = {
            'pre_5':  snap(entry_idx-5, 'pre-5'),
            'pre_3':  snap(entry_idx-3, 'pre-3'),
            'pre_1':  snap(entry_idx-1, 'pre-1'),
            'entry':  snap(entry_idx, 'entry'),
            'first_pos': snap(first_pos, 'first_pos') if first_pos is not None else None,
            'after_pos': snap(first_pos+1, 'after_pos') if first_pos is not None and first_pos+1 < len(candles) else None,
            'exit':   snap(last_idx, 'exit'),
        }
        return {
            'id': r['id'], 'pair': pair, 'dir': r['direction'], 'pnl': r['pnl_pips'],
            'first_pos_offset': (first_pos - entry_idx) if first_pos else None,
            'snaps': snaps,
        }

    for r in losses:
        result = analyze_trade(r)
        if not result: continue
        if result['snaps']['first_pos']:
            rows_collapse.append(result)
        else:
            rows_never.append(result)

    print('='*120)
    print(f'COLLAPSE LOSSES (n={len(rows_collapse)}) — bar-by-bar phase indicators')
    print('='*120)

    for r in rows_collapse:
        print(f"\n  {r['id']} {r['pair']} {r['dir'].upper()} | actual {r['pnl']:+.1f}p | first_pos at bar {r['first_pos_offset']}")
        for label in ['pre_5','pre_3','pre_1','entry','first_pos','after_pos','exit']:
            s = r['snaps'].get(label)
            if not s: continue
            print(f"    {label:<10} bar={s['bar_offset']:>3} {s['time_et']:<12} cls={s['close_pips']:>+5.1f}p "
                  f"fan={s['fan']:<8} sep={s['fan_sep_p']}p({s['fan_sep_atr']}ATR) "
                  f"bbw={s['bbw_p']}p({s['bbw_5b_chg_pct']}%) atr={s['atr_p']}p({s['atr_5b_chg_pct']}%) "
                  f"rsi={s['rsi']} stoch={s['stoch_k']}/{s['stoch_d']} adx={s['adx']} "
                  f"rng={s['candle_range_p']}p body={s['body_pct']}%")
    print()
    print('='*120)
    print('AGGREGATE — collapse losses, indicator state at FIRST_POS_CLOSE')
    print('='*120)
    if rows_collapse:
        # collect features
        def collect(field, phase='first_pos'):
            return [r['snaps'][phase][field] for r in rows_collapse
                    if r['snaps'].get(phase) and r['snaps'][phase].get(field) is not None]
        for ph in ['pre_3','pre_1','entry','first_pos','after_pos']:
            print(f"\n  Phase {ph}:")
            for fld in ['fan_sep_p','fan_sep_atr','bbw_p','bbw_5b_chg_pct','atr_p','atr_5b_chg_pct','rsi','stoch_k','adx','candle_range_p','body_pct']:
                vals = collect(fld, ph)
                if not vals: continue
                avg = sum(vals)/len(vals)
                print(f"    {fld:<20} avg={avg:>7.1f}  range=[{min(vals):>6.1f}, {max(vals):>6.1f}]  n={len(vals)}")
            fans = [r['snaps'][ph]['fan'] for r in rows_collapse if r['snaps'].get(ph)]
            from collections import Counter
            print(f"    fan_state distribution: {dict(Counter(fans))}")

    out_path = os.path.join(HERE, f"compression_audit_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json")
    with open(out_path,'w') as f:
        json.dump({'collapse': rows_collapse, 'never_pos': rows_never}, f, indent=2, default=str)
    print(f"\nFull JSON: {out_path}")


if __name__=='__main__':
    main()
