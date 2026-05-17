"""audit_48h_full_indicators.py — Bar-by-bar full indicator audit for last 48h.

Mission: find the indicator combo that's present in LOSERS at the moment they tip
into doom, but absent in WINNERS at the same relative bar. Output drives a
real-time guardian rule that fires SL→BE the moment that combo emerges.

For every trade in last 48h (losses + winners + open):
  Walk M15 bars from entry → exit (or +20 bars cap)
  At each bar compute the FULL indicator panel:
    - EMA fan: E21, E55, E100, separations (pips + ATR), slopes (3-bar)
    - Distance to each EMA in pips + ATR units
    - RSI(14) + direction
    - Stoch %K, %D
    - MACD line, signal, hist + hist direction
    - ATR(14) in pips
    - BB width (pips + % price + ATR)
    - ADX(14) + DI+/-
    - Candle: body/range, color
    - Per-trade: bars_since_entry, mfe, mae, pnl_close, adv_streak

Mark TIP BAR per loser:
  First bar where MAE accelerates (>=2p jump) AND MFE has plateaued (no growth in 2 bars).

For each winner, snapshot the indicator panel at "bar 2-3" (the comparable early window
where doom-vs-survival decisions are made).

Output:
  /tmp/audit_48h_per_bar.csv          — all bars all trades, full indicator panel
  /tmp/audit_48h_tip_summary.csv      — one row per trade with tip-bar snapshot
  stdout                              — distributional analysis: loser median/avg vs winner
                                        for each indicator at tip vs same-bar
"""
import os, sqlite3, sys, csv, json
SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)
from oanda_client import OandaClient
from dateutil.parser import isoparse
from datetime import timedelta, datetime, timezone

DB = "~/Jarvis/Database/v2/trading_forex.db"

def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


# === TA helpers (no external deps) ============================================
def ema(values, period):
    if not values: return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def sma(values, period):
    out = []
    for i in range(len(values)):
        if i < period - 1:
            out.append(None)
        else:
            out.append(sum(values[i-period+1:i+1]) / period)
    return out

def stdev(values, period):
    out = []
    for i in range(len(values)):
        if i < period - 1:
            out.append(None); continue
        w = values[i-period+1:i+1]
        m = sum(w)/period
        out.append((sum((x-m)**2 for x in w)/period) ** 0.5)
    return out

def atr(highs, lows, closes, period=14):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    # Wilder smoothing
    out = [None]*len(trs)
    if len(trs) >= period:
        out[period-1] = sum(trs[:period]) / period
        for i in range(period, len(trs)):
            out[i] = (out[i-1] * (period-1) + trs[i]) / period
    return out

def rsi(closes, period=14):
    if len(closes) < period+1: return [None]*len(closes)
    gains = [0.0]; losses = [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    out = [None]*len(closes)
    avg_g = sum(gains[1:period+1]) / period
    avg_l = sum(losses[1:period+1]) / period
    if avg_l == 0: out[period] = 100.0
    else:
        rs = avg_g/avg_l
        out[period] = 100 - 100/(1+rs)
    for i in range(period+1, len(closes)):
        avg_g = (avg_g * (period-1) + gains[i]) / period
        avg_l = (avg_l * (period-1) + losses[i]) / period
        if avg_l == 0: out[i] = 100.0
        else:
            rs = avg_g/avg_l
            out[i] = 100 - 100/(1+rs)
    return out

def stoch(highs, lows, closes, k_period=14, d_period=3, smooth_k=3):
    k_raw = [None]*len(closes)
    for i in range(k_period-1, len(closes)):
        hh = max(highs[i-k_period+1:i+1])
        ll = min(lows[i-k_period+1:i+1])
        if hh == ll: k_raw[i] = 50.0
        else: k_raw[i] = 100 * (closes[i] - ll) / (hh - ll)
    # Smooth K
    k = [None]*len(closes)
    for i in range(k_period+smooth_k-2, len(closes)):
        w = [x for x in k_raw[i-smooth_k+1:i+1] if x is not None]
        if len(w) == smooth_k: k[i] = sum(w)/smooth_k
    # D = SMA of K
    d = [None]*len(closes)
    for i in range(k_period+smooth_k+d_period-3, len(closes)):
        w = [x for x in k[i-d_period+1:i+1] if x is not None]
        if len(w) == d_period: d[i] = sum(w)/d_period
    return k, d

def macd(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig: return [None]*len(closes), [None]*len(closes), [None]*len(closes)
    ef = ema(closes, fast); es = ema(closes, slow)
    line = [ef[i] - es[i] for i in range(len(closes))]
    signal = ema(line, sig)
    hist = [line[i] - signal[i] for i in range(len(closes))]
    return line, signal, hist

def adx(highs, lows, closes, period=14):
    """Standard ADX with DI+/DI-."""
    n = len(closes)
    if n < period*2: return [None]*n, [None]*n, [None]*n
    plus_dm = [0.0]; minus_dm = [0.0]; trs = [highs[0]-lows[0]]
    for i in range(1, n):
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    # Wilder smooth
    smp = [None]*n; smm = [None]*n; smt = [None]*n
    smp[period-1] = sum(plus_dm[:period])
    smm[period-1] = sum(minus_dm[:period])
    smt[period-1] = sum(trs[:period])
    for i in range(period, n):
        smp[i] = smp[i-1] - smp[i-1]/period + plus_dm[i]
        smm[i] = smm[i-1] - smm[i-1]/period + minus_dm[i]
        smt[i] = smt[i-1] - smt[i-1]/period + trs[i]
    di_p = [None]*n; di_m = [None]*n; dx = [None]*n
    for i in range(period-1, n):
        if smt[i] and smt[i] > 0:
            di_p[i] = 100 * smp[i] / smt[i]
            di_m[i] = 100 * smm[i] / smt[i]
            s = di_p[i] + di_m[i]
            dx[i] = 100 * abs(di_p[i] - di_m[i]) / s if s else 0.0
    adx_out = [None]*n
    if n >= period*2:
        adx_out[period*2-2] = sum(d for d in dx[period-1:period*2-1] if d is not None) / period
        for i in range(period*2-1, n):
            if adx_out[i-1] is not None and dx[i] is not None:
                adx_out[i] = (adx_out[i-1] * (period-1) + dx[i]) / period
    return adx_out, di_p, di_m


# === Data fetch ==============================================================
def fetch_bars(pair, entry_time, exit_time):
    """Fetch M15 bars: 140 bars BEFORE entry (for indicator warmup) + all bars to exit."""
    oc = OandaClient()
    try:
        ft = isoparse(entry_time)
        now_utc = datetime.now(timezone.utc)
        if exit_time:
            tt = isoparse(exit_time) + timedelta(minutes=15*20)
        else:
            tt = now_utc
        # Never request future bars (OANDA rejects)
        if tt > now_utc:
            tt = now_utc
        candles = oc.get_candles(pair, "M15",
                                 from_time=ft - timedelta(minutes=15*140),
                                 to_time=tt, count=500)
    except Exception as e:
        return None, None, str(e)
    if not candles: return None, None, "no candles"
    flat = []
    for c in candles:
        if not c.get("complete", True): continue
        mid = c.get("mid", {})
        cl = float(mid.get("c", 0))
        if not cl: continue
        flat.append({"time": c.get("time"),
                     "open": float(mid.get("o", 0)),
                     "high": float(mid.get("h", 0)),
                     "low":  float(mid.get("l", 0)),
                     "close": cl})
    try: et = isoparse(entry_time)
    except: return None, None, "bad entry_time"
    entry_idx = None
    for i, c in enumerate(flat):
        try: ct = isoparse(c["time"])
        except: continue
        if ct >= et:
            entry_idx = i; break
    if entry_idx is None: return None, None, "entry_idx not found"
    return flat, entry_idx, None


# === Indicator panel at bar i ================================================
def compute_panel(flat, i, pip):
    """Return dict of every indicator value at bar i. Requires precomputed series."""
    return None  # populated inline below


def slope_pips(series, i, lookback, pip):
    if i - lookback < 0 or series[i] is None or series[i-lookback] is None: return None
    return (series[i] - series[i-lookback]) / pip


def fan_state(e21, e55, e100, i, pip):
    """Determine fan state at bar i."""
    if i < 5 or any(x[i] is None for x in (e21, e55, e100)): return "unknown"
    s21_55_now = abs(e21[i] - e55[i]) / pip
    s21_55_prev = abs(e21[i-2] - e55[i-2]) / pip
    s55_100_now = abs(e55[i] - e100[i]) / pip
    s55_100_prev = abs(e55[i-2] - e100[i-2]) / pip
    expanding = s21_55_now > s21_55_prev * 1.05 and s55_100_now > s55_100_prev * 1.0
    contracting = s21_55_now < s21_55_prev * 0.95
    # Ordering
    if e21[i] > e55[i] > e100[i]: ordering = "bull_ordered"
    elif e21[i] < e55[i] < e100[i]: ordering = "bear_ordered"
    else: ordering = "tangled"
    if expanding: return f"expanding_{ordering}"
    if contracting: return f"contracting_{ordering}"
    return f"stable_{ordering}"


# === Per-trade analysis ======================================================
def analyze_trade(t, writer_bars, writer_summary):
    pair = t['pair']; pip = pip_size(pair)
    is_long = t['direction'].lower() in ("buy","long")
    ep = float(t['entry_price'])

    bars, entry_idx, err = fetch_bars(pair, t['entry_time'], t['exit_time'])
    if not bars or entry_idx is None:
        print(f"  #{t['id']:<6s} {pair:<8s} {t['direction']:<4s} — FETCH FAIL: {err}")
        return None

    closes = [b["close"] for b in bars]
    highs  = [b["high"]  for b in bars]
    lows   = [b["low"]   for b in bars]
    opens  = [b["open"]  for b in bars]

    # Precompute indicators
    e21  = ema(closes, 21)
    e55  = ema(closes, 55)
    e100 = ema(closes, 100)
    atr14 = atr(highs, lows, closes, 14)
    rsi14 = rsi(closes, 14)
    sk, sd = stoch(highs, lows, closes, 14, 3, 3)
    ml, ms, mh = macd(closes, 12, 26, 9)
    bb_mid = sma(closes, 20)
    bb_std = stdev(closes, 20)
    adx14, dip, dim = adx(highs, lows, closes, 14)

    # Walk from entry to (exit or +20 bars)
    end_idx = min(entry_idx + 20, len(bars) - 1)
    mfe = 0.0; mae = 0.0
    adv_streak = 0; fav_streak = 0
    bar_records = []  # for CSV write
    tip_bar_offset = None  # offset from entry where doom tips

    for i in range(entry_idx, end_idx + 1):
        b = bars[i]
        bar_off = i - entry_idx
        cl_pnl = ((b["close"] - ep) if is_long else (ep - b["close"])) / pip
        hi_pnl = ((b["high"] - ep) if is_long else (ep - b["low"])) / pip
        lo_pnl = ((b["low"] - ep) if is_long else (ep - b["high"])) / pip
        mfe_prev = mfe
        mfe = max(mfe, hi_pnl)
        mae_prev = mae
        mae = max(mae, -lo_pnl)
        if cl_pnl < 0: adv_streak += 1; fav_streak = 0
        else: fav_streak += 1; adv_streak = 0

        rng = max(b["high"] - b["low"], pip*0.1)
        body = (b["close"] - b["open"])
        body_ratio = body / rng  # positive=bullish bar
        color = "green" if body > 0 else "red" if body < 0 else "doji"
        # adverse-direction bar from trade perspective
        adv_bar = (body < 0) if is_long else (body > 0)

        atr_pip = (atr14[i] / pip) if atr14[i] else None
        bb_w_pip = (4 * bb_std[i] / pip) if bb_std[i] else None  # full BB width upper-lower
        bb_w_atr = (bb_w_pip / atr_pip) if (bb_w_pip and atr_pip) else None

        # EMA separations
        sep_21_55_pip = ((e21[i] - e55[i]) / pip) if (e21[i] and e55[i]) else None
        sep_55_100_pip = ((e55[i] - e100[i]) / pip) if (e55[i] and e100[i]) else None
        sep_21_55_atr = (abs(sep_21_55_pip) / atr_pip) if (sep_21_55_pip is not None and atr_pip) else None
        sep_55_100_atr = (abs(sep_55_100_pip) / atr_pip) if (sep_55_100_pip is not None and atr_pip) else None
        # Slopes (3-bar)
        slope_e21 = slope_pips(e21, i, 3, pip)
        slope_e55 = slope_pips(e55, i, 3, pip)
        slope_e100 = slope_pips(e100, i, 3, pip)
        # Distance to EMAs in ATR units (signed: +=above EMA)
        d_e21_atr = ((b["close"] - e21[i]) / pip / atr_pip) if (e21[i] and atr_pip) else None
        d_e55_atr = ((b["close"] - e55[i]) / pip / atr_pip) if (e55[i] and atr_pip) else None
        d_e100_atr = ((b["close"] - e100[i]) / pip / atr_pip) if (e100[i] and atr_pip) else None
        # RSI direction (3-bar diff)
        rsi_now = rsi14[i]
        rsi_dir = (rsi14[i] - rsi14[i-3]) if (rsi14[i] is not None and i>=3 and rsi14[i-3] is not None) else None
        # MACD hist direction
        macd_hist_now = mh[i] if mh[i] is not None else None
        macd_hist_prev = mh[i-1] if (i>0 and mh[i-1] is not None) else None
        macd_hist_dir = (macd_hist_now - macd_hist_prev) if (macd_hist_now is not None and macd_hist_prev is not None) else None
        # ADX direction
        adx_now = adx14[i] if adx14[i] is not None else None
        adx_dir = (adx14[i] - adx14[i-3]) if (adx14[i] is not None and i>=3 and adx14[i-3] is not None) else None

        fan = fan_state(e21, e55, e100, i, pip)

        rec = {
            "trade_id": t['id'], "pair": pair, "direction": t['direction'],
            "source": t['source'], "outcome_pnl": t['pnl_pips'],
            "outcome_class": classify_outcome(t),
            "bar_off": bar_off, "time": b["time"],
            "close": round(b["close"], 5),
            "pnl_close": round(cl_pnl, 1),
            "mfe": round(mfe, 1), "mae": round(mae, 1),
            "adv_streak": adv_streak, "fav_streak": fav_streak,
            "bar_color": color, "body_ratio": round(body_ratio, 2),
            "adv_bar": int(adv_bar),
            "atr_pips": round(atr_pip, 2) if atr_pip else None,
            "rsi": round(rsi_now, 1) if rsi_now is not None else None,
            "rsi_dir3": round(rsi_dir, 1) if rsi_dir is not None else None,
            "stoch_k": round(sk[i], 1) if sk[i] is not None else None,
            "stoch_d": round(sd[i], 1) if sd[i] is not None else None,
            "macd_hist": round(macd_hist_now * 1e5, 2) if macd_hist_now is not None else None,
            "macd_hist_dir": round(macd_hist_dir * 1e5, 2) if macd_hist_dir is not None else None,
            "adx": round(adx_now, 1) if adx_now is not None else None,
            "adx_dir3": round(adx_dir, 1) if adx_dir is not None else None,
            "bb_width_atr": round(bb_w_atr, 2) if bb_w_atr is not None else None,
            "sep_21_55_atr": round(sep_21_55_atr, 2) if sep_21_55_atr is not None else None,
            "sep_55_100_atr": round(sep_55_100_atr, 2) if sep_55_100_atr is not None else None,
            "slope_e21_p3": round(slope_e21, 2) if slope_e21 is not None else None,
            "slope_e55_p3": round(slope_e55, 2) if slope_e55 is not None else None,
            "slope_e100_p3": round(slope_e100, 2) if slope_e100 is not None else None,
            "d_e21_atr": round(d_e21_atr, 2) if d_e21_atr is not None else None,
            "d_e55_atr": round(d_e55_atr, 2) if d_e55_atr is not None else None,
            "d_e100_atr": round(d_e100_atr, 2) if d_e100_atr is not None else None,
            "fan_state": fan,
        }
        bar_records.append(rec)
        writer_bars.writerow(rec)

        # Tip detection (losers only)
        is_loser = classify_outcome(t) == "loser"
        if is_loser and tip_bar_offset is None and bar_off >= 1:
            mae_jump = mae - mae_prev
            # Tip = first bar where: MAE jumped >=2p AND MFE didn't grow for 2 bars
            if mae_jump >= 2.0 and mfe == mfe_prev and bar_off >= 1:
                tip_bar_offset = bar_off
            # Fallback definition: MFE=0 and bar 2+ with MAE>=5
            elif mfe == 0 and mae >= 5 and bar_off >= 2:
                tip_bar_offset = bar_off

    # Summary row — write the bar of interest:
    #   losers: tip bar (or bar 3 fallback)
    #   winners/open: bar 2 (corresponding early-window state)
    cls = classify_outcome(t)
    if cls == "loser":
        target_off = tip_bar_offset if tip_bar_offset is not None else min(3, len(bar_records)-1)
    else:
        target_off = min(2, len(bar_records)-1)
    if target_off < len(bar_records):
        snap = dict(bar_records[target_off])
        snap["tip_bar"] = tip_bar_offset if tip_bar_offset is not None else ""
        writer_summary.writerow(snap)

    return bar_records


def classify_outcome(t):
    p = t['pnl_pips']
    if p is None: return "open"
    p = float(p)
    if p <= -10: return "large_loser"
    if p < 0: return "small_loser"  # also a "loser"
    if p < 5: return "small_winner"
    return "big_winner"


# === Main =====================================================================
def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, source, entry_price, sl_price, tp_price,
               entry_time, exit_time, exit_price, pnl_pips,
               max_favorable_excursion_pips, max_adverse_excursion_pips, status
        FROM live_trades
        WHERE entry_time >= datetime('now','-48 hours')
          AND source IN ('snipe_direct','scout','manual')
          AND entry_price IS NOT NULL
        ORDER BY entry_time ASC
    """).fetchall()
    print(f"Auditing {len(trades)} trades in last 48h")

    # Classify
    counts = {"large_loser": 0, "small_loser": 0, "small_winner": 0, "big_winner": 0, "open": 0}
    for t in trades:
        counts[classify_outcome(dict(t))] += 1
    print(f"  Cohorts: {counts}")
    print()

    # Output files
    bars_csv = "/tmp/audit_48h_per_bar.csv"
    summary_csv = "/tmp/audit_48h_tip_summary.csv"
    headers = ["trade_id","pair","direction","source","outcome_pnl","outcome_class",
               "bar_off","time","close","pnl_close","mfe","mae","adv_streak","fav_streak",
               "bar_color","body_ratio","adv_bar","atr_pips",
               "rsi","rsi_dir3","stoch_k","stoch_d","macd_hist","macd_hist_dir",
               "adx","adx_dir3","bb_width_atr","sep_21_55_atr","sep_55_100_atr",
               "slope_e21_p3","slope_e55_p3","slope_e100_p3",
               "d_e21_atr","d_e55_atr","d_e100_atr","fan_state"]
    summary_headers = headers + ["tip_bar"]

    with open(bars_csv, "w", newline="") as fb, open(summary_csv, "w", newline="") as fs:
        wb = csv.DictWriter(fb, fieldnames=headers); wb.writeheader()
        ws = csv.DictWriter(fs, fieldnames=summary_headers); ws.writeheader()
        for i, t in enumerate(trades):
            if i % 10 == 0: print(f"  {i}/{len(trades)}...")
            analyze_trade(dict(t), wb, ws)
    print(f"\nWrote {bars_csv} and {summary_csv}")

    # === Distribution analysis ===============================================
    print("\n" + "="*100)
    print("INDICATOR DISTRIBUTION: losers vs winners at tip / early-window bar")
    print("="*100)

    rows = list(csv.DictReader(open(summary_csv)))
    losers = [r for r in rows if r["outcome_class"] in ("large_loser","small_loser")]
    winners = [r for r in rows if r["outcome_class"] in ("small_winner","big_winner")]
    opens   = [r for r in rows if r["outcome_class"] == "open"]
    print(f"  Snapshot rows: {len(losers)} losers, {len(winners)} winners, {len(opens)} open")

    numeric_cols = ["pnl_close","mfe","mae","adv_streak","body_ratio","atr_pips",
                    "rsi","rsi_dir3","stoch_k","stoch_d","macd_hist","macd_hist_dir",
                    "adx","adx_dir3","bb_width_atr","sep_21_55_atr","sep_55_100_atr",
                    "slope_e21_p3","slope_e55_p3","slope_e100_p3",
                    "d_e21_atr","d_e55_atr","d_e100_atr"]

    def stats(rows, col):
        vals = []
        for r in rows:
            v = r.get(col, "")
            try:
                if v == "" or v is None: continue
                vals.append(float(v))
            except: continue
        if not vals: return None
        vals.sort()
        med = vals[len(vals)//2]
        avg = sum(vals)/len(vals)
        return {"n": len(vals), "med": med, "avg": avg,
                "min": vals[0], "max": vals[-1]}

    print(f"\n  {'indicator':<20s} {'LOSER':<35s} {'WINNER':<35s}  delta(med)")
    print(f"  {'':20s} {'med   avg    [min, max]':<35s} {'med   avg    [min, max]':<35s}")
    print("  " + "-"*110)
    for col in numeric_cols:
        sl = stats(losers, col); sw = stats(winners, col)
        if sl is None or sw is None:
            print(f"  {col:<20s} (no data)")
            continue
        def fmt(s):
            return f"{s['med']:+6.2f} {s['avg']:+6.2f}  [{s['min']:+6.2f}, {s['max']:+6.2f}]"
        delta = sl["med"] - sw["med"]
        print(f"  {col:<20s} {fmt(sl):<35s} {fmt(sw):<35s}  {delta:+6.2f}")

    # === Single-feature classifier sweep ====================================
    print("\n" + "="*100)
    print("SINGLE-FEATURE CLASSIFIER — block-if-metric-{>=/<=}-T")
    print("="*100)
    print("  Looking for thresholds that catch >=60% losers with <30% winner-kill")
    print()
    for col in numeric_cols:
        vals_l = [float(r[col]) for r in losers if r.get(col) not in (None, "")]
        vals_w = [float(r[col]) for r in winners if r.get(col) not in (None, "")]
        if not vals_l or not vals_w: continue
        # Try >=T direction
        best_ge = None; best_le = None
        # Try thresholds from union of values
        all_vals = sorted(set(vals_l + vals_w))
        for T in all_vals:
            l_caught = sum(1 for v in vals_l if v >= T)
            w_caught = sum(1 for v in vals_w if v >= T)
            l_pct = l_caught / max(len(vals_l), 1)
            w_pct = w_caught / max(len(vals_w), 1)
            if l_pct >= 0.6 and w_pct <= 0.3:
                if best_ge is None or l_pct - w_pct > best_ge[2]:
                    best_ge = (T, l_caught, l_pct - w_pct, w_caught, l_pct, w_pct)
            l_caught_l = sum(1 for v in vals_l if v <= T)
            w_caught_l = sum(1 for v in vals_w if v <= T)
            l_pct_l = l_caught_l / max(len(vals_l), 1)
            w_pct_l = w_caught_l / max(len(vals_w), 1)
            if l_pct_l >= 0.6 and w_pct_l <= 0.3:
                if best_le is None or l_pct_l - w_pct_l > best_le[2]:
                    best_le = (T, l_caught_l, l_pct_l - w_pct_l, w_caught_l, l_pct_l, w_pct_l)
        if best_ge:
            T,lc,_,wc,lp,wp = best_ge
            print(f"  [{col}] >= {T:+8.3f}  catches {lc}/{len(vals_l)} losers ({lp*100:.0f}%) AND only {wc}/{len(vals_w)} winners ({wp*100:.0f}%)")
        if best_le:
            T,lc,_,wc,lp,wp = best_le
            print(f"  [{col}] <= {T:+8.3f}  catches {lc}/{len(vals_l)} losers ({lp*100:.0f}%) AND only {wc}/{len(vals_w)} winners ({wp*100:.0f}%)")

    # === Open trades alert ==================================================
    if opens:
        print("\n" + "="*100)
        print("OPEN TRADES — current early-bar indicator snapshot")
        print("="*100)
        for r in opens:
            print(f"  #{r['trade_id']:<6s} {r['pair']:<8s} {r['direction']:<4s} {r['source']:<14s}")
            for col in numeric_cols:
                v = r.get(col, "")
                if v == "" or v is None: continue
                print(f"    {col:<20s} = {v}")


if __name__ == "__main__":
    main()
