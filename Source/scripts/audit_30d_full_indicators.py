"""audit_30d_full_indicators.py — Extend 48h audit to 30 days for proper validation.

Same indicator panel and tip-bar logic. Output to /tmp/audit_30d_*.csv.

Runs against last 30 days of trades. Expect ~250-400 trades. Takes ~10 min for
the OANDA candle pulls.
"""
import os, sys
SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

# Reuse 48h script's machinery
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_48h_full_indicators import (
    pip_size, ema, sma, stdev, atr, rsi, stoch, macd, adx,
    fetch_bars, slope_pips, fan_state, classify_outcome, analyze_trade,
    DB
)
import sqlite3, csv


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, source, entry_price, sl_price, tp_price,
               entry_time, exit_time, exit_price, pnl_pips,
               max_favorable_excursion_pips, max_adverse_excursion_pips, status
        FROM live_trades
        WHERE entry_time >= datetime('now','-30 days')
          AND source IN ('snipe_direct','scout','manual')
          AND entry_price IS NOT NULL
          AND status = 'closed'
        ORDER BY entry_time ASC
    """).fetchall()
    print(f"Auditing {len(trades)} closed trades in last 30 days")
    counts = {}
    for t in trades:
        c = classify_outcome(dict(t))
        counts[c] = counts.get(c, 0) + 1
    print(f"  Cohorts: {counts}")
    print()

    bars_csv = "/tmp/audit_30d_per_bar.csv"
    summary_csv = "/tmp/audit_30d_tip_summary.csv"
    headers = ["trade_id","pair","direction","source","outcome_pnl","outcome_class",
               "bar_off","time","close","pnl_close","mfe","mae","adv_streak","fav_streak",
               "bar_color","body_ratio","adv_bar","atr_pips",
               "rsi","rsi_dir3","stoch_k","stoch_d","macd_hist","macd_hist_dir",
               "adx","adx_dir3","bb_width_atr","sep_21_55_atr","sep_55_100_atr",
               "slope_e21_p3","slope_e55_p3","slope_e100_p3",
               "d_e21_atr","d_e55_atr","d_e100_atr","fan_state"]
    summary_headers = headers + ["tip_bar"]

    fail = 0
    with open(bars_csv, "w", newline="") as fb, open(summary_csv, "w", newline="") as fs:
        wb = csv.DictWriter(fb, fieldnames=headers); wb.writeheader()
        ws = csv.DictWriter(fs, fieldnames=summary_headers); ws.writeheader()
        for i, t in enumerate(trades):
            if i % 25 == 0: print(f"  {i}/{len(trades)}...")
            result = analyze_trade(dict(t), wb, ws)
            if result is None: fail += 1
    print(f"\nWrote {bars_csv} and {summary_csv}. {fail} fetch failures of {len(trades)}.")


if __name__ == "__main__":
    main()
