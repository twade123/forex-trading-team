"""trace_48h_losers_per_bar.py — Walk every large loss in last 48h bar-by-bar
and find the MOMENT each one tipped into "this is going to lose big" territory.

For each loser:
  - Walk M15 bars from entry
  - At each bar compute: pnl_close, mfe, mae, last_bar_color, adverse_drift_count,
    fan velocity proxy (E21-E55 distance change), bb_width
  - Highlight the bar where pattern shifts to "doomed"

Goal: identify the EARLIEST consistent moment across all losers where a real-time
rule could fire AND save material loss.
"""
import os, sqlite3, sys
SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)
from oanda_client import OandaClient
from backtester.ema_separation import calculate_ema
from dateutil.parser import isoparse
from datetime import timedelta

DB = "~/Jarvis/Database/v2/trading_forex.db"

def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def fetch_full(pair, entry_time, exit_time):
    oc = OandaClient()
    try:
        ft = isoparse(entry_time); tt = isoparse(exit_time)
        # 140 bars before entry for fan context, all trade bars after
        candles = oc.get_candles(pair, "M15", from_time=ft - timedelta(minutes=15*140),
                                  to_time=tt, count=400)
    except Exception:
        return None, None
    if not candles: return None, None
    flat = []
    for c in candles:
        mid = c.get("mid", {})
        cl = float(mid.get("c", 0))
        if not cl: continue
        flat.append({"time": c.get("time"),
                     "open": float(mid.get("o",0)),
                     "high": float(mid.get("h",0)),
                     "low":  float(mid.get("l",0)),
                     "close": cl})
    # Find entry idx
    try: et = isoparse(entry_time)
    except: return None, None
    entry_idx = None
    for i, c in enumerate(flat):
        try: ct = isoparse(c["time"])
        except: continue
        if ct >= et:
            entry_idx = i; break
    if entry_idx is None: return None, None
    return flat, entry_idx


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    losers = conn.execute("""
        SELECT id, pair, direction, source, entry_price, entry_time, exit_time, pnl_pips,
               max_favorable_excursion_pips as mfe_db,
               max_adverse_excursion_pips as mae_db, sl_price
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= datetime('now','-48 hours')
          AND pnl_pips <= -10
        ORDER BY pnl_pips ASC
    """).fetchall()
    print(f"Tracing {len(losers)} large losses (≤-10p) in last 48 hours")
    print()

    for t in losers:
        print('='*120)
        print(f"#{t['id']} {t['pair']} {t['direction']} {t['source']}  "
              f"final_pnl={t['pnl_pips']}p MFE={t['mfe_db']} MAE={t['mae_db']}")
        flat, entry_idx = fetch_full(t['pair'], t['entry_time'], t['exit_time'])
        if not flat or entry_idx is None:
            print("  (no candle data)"); continue
        pip = pip_size(t['pair'])
        ep = float(t['entry_price'])
        is_long = t['direction'].lower() in ("buy","long")

        # EMAs for fan context
        closes = [c["close"] for c in flat]
        e21 = calculate_ema(closes, 21)
        e55 = calculate_ema(closes, 55)
        e100 = calculate_ema(closes, 100)

        # Walk from entry to (exit or +20 bars)
        end_idx = min(entry_idx + 20, len(flat) - 1)
        mfe = 0.0; mae = 0.0
        consec_adverse = 0
        print(f"  bar  | close       | pnl_close | mfe   | mae   | candle | bar_color | adv_streak | E21-E55_sep | bb_width")
        print(f"  -----+-------------+-----------+-------+-------+--------+-----------+------------+-------------+--------")
        for i in range(entry_idx, end_idx + 1):
            b = flat[i]
            cl_pnl = ((b["close"] - ep) if is_long else (ep - b["close"])) / pip
            hi_pnl = ((b["high"] - ep) if is_long else (ep - b["low"])) / pip
            lo_pnl = ((b["low"] - ep) if is_long else (ep - b["high"])) / pip
            mfe = max(mfe, hi_pnl)
            mae = max(mae, -lo_pnl)
            color = "GREEN" if b["close"] > b["open"] else "RED" if b["close"] < b["open"] else "DOJI"
            # adverse from THIS trade's perspective
            adverse_close = (b["close"] < b["open"]) if is_long else (b["close"] > b["open"])
            if cl_pnl < 0:
                consec_adverse += 1
            else:
                consec_adverse = 0
            sep_pct = (e21[i] - e55[i]) / e55[i] * 100 if e55[i] else 0
            # BB width (simple range over 20 bars)
            if i >= 20:
                hh = max(flat[j]["high"] for j in range(i-19, i+1))
                ll = min(flat[j]["low"]  for j in range(i-19, i+1))
                bb_w = (hh - ll) / e55[i] * 100 if e55[i] else 0
            else: bb_w = 0
            bar_label = "entry" if i == entry_idx else f"+{i - entry_idx}"
            mark = ""
            if i == entry_idx: mark = " ← entry"
            if mfe == 0 and mae >= 5 and i - entry_idx >= 2: mark += " [LOSER_SIG]"
            print(f"  {bar_label:<5s}| {b['close']:.5f} | {cl_pnl:+7.1f}p | {mfe:5.1f} | {mae:5.1f} | "
                  f"{color:<6s} | {('ADVERSE' if adverse_close else 'FAVOR'):<9s} | {consec_adverse:<10d} | "
                  f"{sep_pct:+6.3f}%    | {bb_w:5.2f}%{mark}")


if __name__ == "__main__":
    main()
