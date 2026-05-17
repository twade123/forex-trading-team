"""analyze_zero_mfe_losers.py — Find the entry-time signature of "wrong from minute 1"
trades (MFE = 0, never went positive, lost meaningfully).

For each MFE=0 loser in last 30 days, compute REAL-TIME features that were available
at entry decision (from candle history up to entry bar):
  - Fan separation pct (E21-E100 / E100 * 100)
  - Fan velocity (separation change last 3 bars)
  - Fan acceleration (delta of velocity)
  - Price extension in ATR (distance from 20-bar mean / ATR)
  - Stoch %K (14,3,3)
  - RSI (14)
  - ADX (14)
  - Last 3 candle bodies & wicks
  - High of last 5 bars vs current close
  - Distance from current close to E21 / E55 / E100 in ATR

Then compute same for winners > +10p over same period for comparison.

Output: per-feature comparison — what's STATISTICALLY DIFFERENT between zero-MFE losers and big winners?
"""
import os, sqlite3, sys, math
from collections import defaultdict
SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient
from backtester.ema_separation import calculate_ema
from dateutil.parser import isoparse
from datetime import timedelta

DB = "~/Jarvis/Database/v2/trading_forex.db"


def get_pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def fetch_up_to_entry(pair, entry_time):
    oc = OandaClient()
    try:
        ft = isoparse(entry_time)
        ft_lookback = ft - timedelta(minutes=15 * 200)
        candles = oc.get_candles(pair, granularity="M15",
                                  from_time=ft_lookback, to_time=ft)
    except Exception:
        return None
    if not candles: return None
    out = []
    for c in candles:
        mid = c.get("mid", {})
        close = float(mid.get("c", 0))
        if not close: continue
        out.append({
            "time": c.get("time"),
            "open":  float(mid.get("o", 0)),
            "high":  float(mid.get("h", 0)),
            "low":   float(mid.get("l", 0)),
            "close": close,
        })
    return out if len(out) >= 100 else None


def compute_features(candles, trade_dir, pip):
    """Compute real-time features from candle history (excludes entry bar's future)."""
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    opens  = [c["open"]  for c in candles]
    if len(closes) < 100: return None
    e21 = calculate_ema(closes, 21)
    e55 = calculate_ema(closes, 55)
    e100 = calculate_ema(closes, 100)
    # Last bar values
    last = candles[-1]
    last_close = last["close"]
    is_long = trade_dir.lower() in ("buy", "long")

    # Fan separation (E21 - E100 normalized)
    sep_pct = (e21[-1] - e100[-1]) / e100[-1] * 100  # signed: positive=bullish
    # Last 3 bars: was separation expanding, peaking, or compressing?
    sep_t = [(e21[-i] - e100[-i]) / e100[-i] * 100 for i in (1, 2, 3, 4)]
    velocity = sep_t[0] - sep_t[1]  # most recent change
    accel    = velocity - (sep_t[1] - sep_t[2])  # change in change

    # ATR (simple range avg last 14 bars)
    ranges = [highs[-i] - lows[-i] for i in range(1, 15)]
    atr = sum(ranges) / len(ranges)
    atr_pips = atr / pip

    # Price extension from 20-bar mean
    mean20 = sum(closes[-20:]) / 20
    ext_atr = abs(last_close - mean20) / atr if atr > 0 else 0

    # Distance from current close to E21/E55/E100 in ATR
    d_e21  = (last_close - e21[-1])  / atr if atr > 0 else 0
    d_e55  = (last_close - e55[-1])  / atr if atr > 0 else 0
    d_e100 = (last_close - e100[-1]) / atr if atr > 0 else 0

    # Stoch %K (14,3,3) — simple
    win = 14
    if len(highs) >= win:
        h14 = max(highs[-win:])
        l14 = min(lows[-win:])
        if h14 > l14:
            stoch_raw = 100 * (last_close - l14) / (h14 - l14)
        else:
            stoch_raw = 50.0
    else:
        stoch_raw = 50.0

    # RSI (14)
    gains, losses = [], []
    for i in range(1, min(15, len(closes))):
        d = closes[-i] - closes[-i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if losses and sum(losses) > 0:
        rs = (sum(gains)/len(gains)) / (sum(losses)/len(losses))
        rsi = 100 - 100/(1+rs)
    else:
        rsi = 100.0

    # Last 3 candle body+wick analysis
    last_body = abs(last_close - last["open"])
    last_range = max(last["high"] - last["low"], 1e-9)
    last_body_pct = last_body / last_range
    last_upper_wick = (last["high"] - max(last_close, last["open"])) / last_range
    last_lower_wick = (min(last_close, last["open"]) - last["low"]) / last_range
    last_red = last_close < last["open"]
    last_green = last_close > last["open"]

    # 5-bar high/low — is current close at the extreme?
    high5 = max(highs[-5:])
    low5 = min(lows[-5:])
    dist_from_high5_p = (high5 - last_close) / pip
    dist_from_low5_p = (last_close - low5) / pip

    return {
        "sep_pct": sep_pct,
        "fan_velocity": velocity,
        "fan_accel": accel,
        "atr_pips": atr_pips,
        "ext_atr": ext_atr,
        "d_e21_atr": d_e21,
        "d_e55_atr": d_e55,
        "d_e100_atr": d_e100,
        "stoch_raw": stoch_raw,
        "rsi": rsi,
        "last_body_pct": last_body_pct,
        "last_upper_wick_pct": last_upper_wick,
        "last_lower_wick_pct": last_lower_wick,
        "last_red": int(last_red),
        "last_green": int(last_green),
        "dist_from_high5_p": dist_from_high5_p,
        "dist_from_low5_p": dist_from_low5_p,
    }


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    # All trades last 30d
    trades = conn.execute("""
        SELECT id, pair, direction, source, entry_time, pnl_pips,
               max_favorable_excursion_pips as mfe,
               max_adverse_excursion_pips as mae, setup
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= datetime('now','-30 days')
          AND pnl_pips IS NOT NULL
          AND entry_price IS NOT NULL
        ORDER BY entry_time DESC
    """).fetchall()
    print(f"Computing real-time features for {len(trades)} trades...")
    print()

    # Classify trades:
    # ZERO_MFE_LOSERS: pnl < -10 AND mfe is None or <= 0.5
    # BIG_WINNERS: pnl > 10
    # OTHER
    bins = {"ZERO_MFE_LOSER": [], "BIG_WINNER": [], "OTHER": []}
    feature_acc = {"ZERO_MFE_LOSER": defaultdict(list), "BIG_WINNER": defaultdict(list)}

    for i, t in enumerate(trades):
        if i % 25 == 0: print(f"  {i}/{len(trades)}...")
        mfe = t["mfe"]
        pnl = float(t["pnl_pips"])
        if pnl < -10 and (mfe is None or float(mfe) <= 0.5):
            cohort = "ZERO_MFE_LOSER"
        elif pnl > 10:
            cohort = "BIG_WINNER"
        else:
            cohort = "OTHER"
        candles = fetch_up_to_entry(t["pair"], t["entry_time"])
        if not candles: continue
        pip = get_pip_size(t["pair"])
        feats = compute_features(candles, t["direction"], pip)
        if not feats: continue
        bins[cohort].append({"id": t["id"], "pair": t["pair"], "dir": t["direction"],
                              "src": t["source"], "pnl": pnl, "mfe": mfe, "mae": t["mae"],
                              "setup": t["setup"], **feats})
        if cohort in feature_acc:
            for k, v in feats.items():
                feature_acc[cohort][k].append(v)

    print()
    print(f"COHORT SIZES: ZERO_MFE_LOSER={len(bins['ZERO_MFE_LOSER'])} "
          f"BIG_WINNER={len(bins['BIG_WINNER'])} OTHER={len(bins['OTHER'])}")
    print()

    # Compare medians for each feature
    print("=== FEATURE COMPARISON: ZERO_MFE_LOSER vs BIG_WINNER ===")
    print(f"  {'feature':<25s} {'loser_median':<14s} {'winner_median':<14s} {'loser_avg':<12s} {'winner_avg':<12s}")
    print('  ' + '-'*90)
    def median(xs):
        if not xs: return None
        s = sorted(xs); n = len(s)
        return s[n//2] if n % 2 else (s[n//2-1] + s[n//2])/2
    feature_keys = list(feature_acc["ZERO_MFE_LOSER"].keys())
    for k in feature_keys:
        lvals = feature_acc["ZERO_MFE_LOSER"][k]
        wvals = feature_acc["BIG_WINNER"][k]
        if not lvals or not wvals: continue
        lmed = median(lvals); wmed = median(wvals)
        lavg = sum(lvals)/len(lvals); wavg = sum(wvals)/len(wvals)
        diff_marker = " ←" if abs(lmed - wmed) / (abs(lmed) + abs(wmed) + 0.01) > 0.3 else ""
        print(f"  {k:<25s} {lmed:<14.3f} {wmed:<14.3f} {lavg:<12.3f} {wavg:<12.3f}{diff_marker}")

    # Show today's 3 losers' specific features
    print()
    print("=== TODAY'S 3 LOSERS — entry features ===")
    today_ids = {'15910','15972','16116'}
    for t in bins["ZERO_MFE_LOSER"] + bins["OTHER"]:
        if t["id"] in today_ids:
            print(f"\n#{t['id']} {t['pair']} {t['dir']} {t['src']} pnl={t['pnl']:+.1f}p MFE={t['mfe']}")
            print(f"  setup={t['setup']}")
            print(f"  sep_pct={t['sep_pct']:.3f}  fan_velocity={t['fan_velocity']:+.4f}  fan_accel={t['fan_accel']:+.4f}")
            print(f"  ext_atr={t['ext_atr']:.2f}  d_e21_atr={t['d_e21_atr']:+.2f}  d_e55_atr={t['d_e55_atr']:+.2f}  d_e100_atr={t['d_e100_atr']:+.2f}")
            print(f"  stoch={t['stoch_raw']:.1f}  rsi={t['rsi']:.1f}  atr_pips={t['atr_pips']:.1f}")
            print(f"  last_body_pct={t['last_body_pct']:.2f}  upper_wick={t['last_upper_wick_pct']:.2f}  lower_wick={t['last_lower_wick_pct']:.2f}")
            print(f"  dist_from_high5={t['dist_from_high5_p']:.1f}p  dist_from_low5={t['dist_from_low5_p']:.1f}p")


if __name__ == "__main__":
    main()
