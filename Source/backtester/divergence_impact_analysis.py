#!/usr/bin/env python3
"""
Divergence Impact Analysis on Sniper V4 Trades
================================================
Answers: How much edge are we leaving on the table by not scoring divergence?

Run:
  source ~/myenv/bin/activate
  cd ~/jarvis/Trading\ Bot
  python Source/backtester/divergence_impact_analysis.py
"""

import sys, os, time
import sqlite3

DB_PATH = '~/jarvis/Database/v2/trading_forex.db'

def pct(wins, total):
    return 100 * wins / total if total > 0 else 0

def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ── 1. Setup-level divergence analysis ──
    print("=" * 70)
    print("SECTION 1: SETUP-LEVEL — S15 (RSI Divergence Setup) PERFORMANCE")
    print("=" * 70)

    cur.execute("""
        SELECT setup, regime, trade_count, win_rate, avg_pips, profit_factor
        FROM backtest_setup_performance 
        WHERE setup LIKE '%S15%'
        ORDER BY trade_count DESC
        LIMIT 30
    """)
    rows = cur.fetchall()
    if rows:
        print(f"\n{'Setup':<30} {'Regime':<15} {'Trades':>7} {'WR%':>7} {'AvgPip':>8} {'PF':>6}")
        print("-" * 80)
        for r in rows:
            print(f"{r['setup']:<30} {r['regime']:<15} {r['trade_count']:>7} "
                  f"{r['win_rate']:>6.1f}% {r['avg_pips']:>7.1f} {r['profit_factor']:>6.2f}")
    else:
        print("   No S15 rows in backtest_setup_performance")

    # ── 2. Trigger reason analysis ──
    print("\n" + "=" * 70)
    print("SECTION 2: DIVERGENCE IN TRIGGER REASON — WIN RATE COMPARISON")
    print("=" * 70)

    # Total
    cur.execute("SELECT COUNT(*) as cnt FROM backtest_trades")
    total = cur.fetchone()['cnt']

    # WITH divergence
    cur.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               AVG(pips) as avg_pips,
               AVG(CASE WHEN result='win' THEN pips ELSE NULL END) as avg_win,
               AVG(CASE WHEN result='loss' THEN pips ELSE NULL END) as avg_loss
        FROM backtest_trades 
        WHERE trigger_reason LIKE '%divergence%' OR trigger_reason LIKE '%div %'
    """)
    dv = cur.fetchone()

    # WITHOUT divergence
    cur.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               AVG(pips) as avg_pips,
               AVG(CASE WHEN result='win' THEN pips ELSE NULL END) as avg_win,
               AVG(CASE WHEN result='loss' THEN pips ELSE NULL END) as avg_loss
        FROM backtest_trades 
        WHERE trigger_reason NOT LIKE '%divergence%' AND trigger_reason NOT LIKE '%div %'
    """)
    nd = cur.fetchone()

    print(f"\n   Total trades in DB: {total:,}")
    print(f"   Trades WITH divergence in trigger: {dv['total']:,} ({100*dv['total']/total:.1f}%)")
    print(f"   Trades WITHOUT divergence:         {nd['total']:,} ({100*nd['total']/total:.1f}%)")

    if dv['total'] > 0:
        dv_wr = pct(dv['wins'], dv['total'])
        nd_wr = pct(nd['wins'], nd['total'])
        print(f"\n   {'Metric':<25} {'WITH Divergence':>18} {'WITHOUT':>18} {'Delta':>10}")
        print("   " + "-" * 75)
        print(f"   {'Win Rate':<25} {dv_wr:>17.1f}% {nd_wr:>17.1f}% {dv_wr-nd_wr:>+9.1f}%")
        print(f"   {'Avg Pips':<25} {dv['avg_pips']:>17.2f}  {nd['avg_pips']:>17.2f}  {dv['avg_pips']-nd['avg_pips']:>+9.2f}")
        if dv['avg_win']: print(f"   {'Avg Win (pips)':<25} {dv['avg_win']:>17.2f}  {nd['avg_win']:>17.2f}")
        if dv['avg_loss']: print(f"   {'Avg Loss (pips)':<25} {dv['avg_loss']:>17.2f}  {nd['avg_loss']:>17.2f}")

    # Breakdown by type
    print(f"\n   By divergence type:")
    for pattern, label in [
        ('%Bullish%divergence%', 'Bullish RSI Div'),
        ('%Bearish%divergence%', 'Bearish RSI Div'),
        ('%bullish%div%',       'bullish div (lower)'),
        ('%bearish%div%',       'bearish div (lower)'),
        ('%MACD%div%',          'MACD Divergence'),
        ('%hidden%div%',        'Hidden Divergence'),
        ('%RSI%div%',           'RSI Divergence (any)'),
    ]:
        cur.execute(f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                   AVG(pips) as avg_pips
            FROM backtest_trades WHERE trigger_reason LIKE '{pattern}'
        """)
        r = cur.fetchone()
        if r['total'] > 0:
            wr = pct(r['wins'], r['total'])
            print(f"   {label:<25} {r['total']:>8,} trades  {wr:>5.1f}% WR  {r['avg_pips']:>+7.2f} avg pips")

    # Sample some trigger_reason values with "div" to see exact format
    print(f"\n   Sample trigger_reason values containing 'div':")
    cur.execute("""
        SELECT DISTINCT trigger_reason FROM backtest_trades 
        WHERE trigger_reason LIKE '%div%' LIMIT 15
    """)
    for r in cur.fetchall():
        print(f"     → {r['trigger_reason'][:100]}")

    # ── 3. Sniper score band overlap ──
    print("\n" + "=" * 70)
    print("SECTION 3: SNIPER SCORE BAND × DIVERGENCE")
    print("=" * 70)

    # Check what score-like columns exist
    cur.execute("PRAGMA table_info(backtest_trades)")
    cols = [r['name'] for r in cur.fetchall()]
    score_col = next((c for c in ['confidence', 'max_score'] if c in cols), None)

    if score_col:
        for label, where in [
            ("WITH divergence", "trigger_reason LIKE '%divergence%' OR trigger_reason LIKE '%div %'"),
            ("WITHOUT divergence", "trigger_reason NOT LIKE '%divergence%' AND trigger_reason NOT LIKE '%div %'"),
        ]:
            cur.execute(f"""
                SELECT 
                    CASE 
                        WHEN {score_col} >= 16 THEN '16+'
                        WHEN {score_col} >= 14 THEN '14-15'
                        WHEN {score_col} >= 12 THEN '12-13'
                        WHEN {score_col} >= 10 THEN '10-11'
                        WHEN {score_col} >= 8  THEN '08-09'
                        ELSE '<8'
                    END as band,
                    COUNT(*) as total,
                    SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                    AVG(pips) as avg_pips
                FROM backtest_trades
                WHERE ({where}) AND {score_col} IS NOT NULL
                GROUP BY band ORDER BY band
            """)
            rows = cur.fetchall()
            if rows:
                print(f"\n   {label} (score col: {score_col}):")
                print(f"   {'Band':<10} {'Trades':>8} {'WR%':>7} {'Avg Pips':>10}")
                print("   " + "-" * 40)
                for r in rows:
                    print(f"   {r['band']:<10} {r['total']:>8,} {pct(r['wins'],r['total']):>6.1f}% {r['avg_pips']:>+9.2f}")
    else:
        print(f"   No score column found. Available: {', '.join(sorted(cols))}")

    # ── 4. Per-pair divergence edge ──
    print("\n" + "=" * 70)
    print("SECTION 4: PER-PAIR DIVERGENCE EDGE")
    print("=" * 70)

    cur.execute("""
        SELECT pair,
            SUM(CASE WHEN (trigger_reason LIKE '%divergence%' OR trigger_reason LIKE '%div %') AND result='win' THEN 1 ELSE 0 END) as dw,
            SUM(CASE WHEN (trigger_reason LIKE '%divergence%' OR trigger_reason LIKE '%div %') THEN 1 ELSE 0 END) as dt,
            SUM(CASE WHEN (trigger_reason NOT LIKE '%divergence%' AND trigger_reason NOT LIKE '%div %') AND result='win' THEN 1 ELSE 0 END) as nw,
            SUM(CASE WHEN (trigger_reason NOT LIKE '%divergence%' AND trigger_reason NOT LIKE '%div %') THEN 1 ELSE 0 END) as nt
        FROM backtest_trades GROUP BY pair ORDER BY pair
    """)
    rows = cur.fetchall()
    print(f"\n   {'Pair':<12} {'DivTrades':>9} {'DivWR%':>7} {'NoDivTrades':>11} {'NoDivWR%':>9} {'Edge':>7}")
    print("   " + "-" * 60)
    for r in rows:
        dwr = pct(r['dw'], r['dt'])
        nwr = pct(r['nw'], r['nt'])
        edge = dwr - nwr
        flag = " 🔥" if edge > 5 else (" ⚠️" if edge < -5 else "")
        print(f"   {r['pair']:<12} {r['dt']:>9,} {dwr:>6.1f}% {r['nt']:>11,} {nwr:>8.1f}% {edge:>+6.1f}%{flag}")

    # ── 5. By regime ──
    print("\n" + "=" * 70)
    print("SECTION 5: DIVERGENCE EDGE BY REGIME")
    print("=" * 70)

    cur.execute("""
        SELECT regime,
            SUM(CASE WHEN (trigger_reason LIKE '%divergence%' OR trigger_reason LIKE '%div %') AND result='win' THEN 1 ELSE 0 END) as dw,
            SUM(CASE WHEN (trigger_reason LIKE '%divergence%' OR trigger_reason LIKE '%div %') THEN 1 ELSE 0 END) as dt,
            SUM(CASE WHEN (trigger_reason NOT LIKE '%divergence%' AND trigger_reason NOT LIKE '%div %') AND result='win' THEN 1 ELSE 0 END) as nw,
            SUM(CASE WHEN (trigger_reason NOT LIKE '%divergence%' AND trigger_reason NOT LIKE '%div %') THEN 1 ELSE 0 END) as nt
        FROM backtest_trades GROUP BY regime ORDER BY dt DESC
    """)
    rows = cur.fetchall()
    print(f"\n   {'Regime':<18} {'DivTrades':>9} {'DivWR%':>7} {'NoDivTrades':>11} {'NoDivWR%':>9} {'Edge':>7}")
    print("   " + "-" * 65)
    for r in rows:
        dwr = pct(r['dw'], r['dt'])
        nwr = pct(r['nw'], r['nt'])
        edge = dwr - nwr
        flag = " 🔥" if edge > 5 else ""
        print(f"   {r['regime']:<18} {r['dt']:>9,} {dwr:>6.1f}% {r['nt']:>11,} {nwr:>8.1f}% {edge:>+6.1f}%{flag}")

    # ── 6. Setup × divergence cross-tab ──
    print("\n" + "=" * 70)
    print("SECTION 6: TOP SETUPS × DIVERGENCE CROSS-TAB")
    print("=" * 70)

    cur.execute("""
        SELECT setup,
            SUM(CASE WHEN (trigger_reason LIKE '%divergence%' OR trigger_reason LIKE '%div %') AND result='win' THEN 1 ELSE 0 END) as dw,
            SUM(CASE WHEN (trigger_reason LIKE '%divergence%' OR trigger_reason LIKE '%div %') THEN 1 ELSE 0 END) as dt,
            SUM(CASE WHEN (trigger_reason NOT LIKE '%divergence%' AND trigger_reason NOT LIKE '%div %') AND result='win' THEN 1 ELSE 0 END) as nw,
            SUM(CASE WHEN (trigger_reason NOT LIKE '%divergence%' AND trigger_reason NOT LIKE '%div %') THEN 1 ELSE 0 END) as nt
        FROM backtest_trades GROUP BY setup ORDER BY dt DESC LIMIT 25
    """)
    rows = cur.fetchall()
    print(f"\n   {'Setup':<25} {'DivTrades':>9} {'DivWR%':>7} {'NoDivTrades':>11} {'NoDivWR%':>9} {'Edge':>7}")
    print("   " + "-" * 72)
    for r in rows:
        dwr = pct(r['dw'], r['dt'])
        nwr = pct(r['nw'], r['nt'])
        edge = dwr - nwr
        flag = " 🔥" if edge > 5 else ""
        print(f"   {str(r['setup']):<25} {r['dt']:>9,} {dwr:>6.1f}% {r['nt']:>11,} {nwr:>8.1f}% {edge:>+6.1f}%{flag}")

    # ── 7. High-score trades: what % had divergence? ──
    print("\n" + "=" * 70)
    print("SECTION 7: HIGH-CONFIDENCE TRADES — DIVERGENCE PREVALENCE")
    print("=" * 70)

    if 'confidence' in cols:
        for lo, hi, label in [(80, 100, '80-100'), (60, 80, '60-80'), (40, 60, '40-60'), (20, 40, '20-40'), (0, 20, '0-20')]:
            cur.execute(f"""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN trigger_reason LIKE '%divergence%' OR trigger_reason LIKE '%div %' THEN 1 ELSE 0 END) as div_count,
                       SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins
                FROM backtest_trades WHERE confidence >= {lo} AND confidence < {hi}
            """)
            r = cur.fetchone()
            if r['total'] > 0:
                print(f"   Confidence {label}: {r['total']:>9,} trades | {pct(r['div_count'],r['total']):>5.1f}% had divergence | {pct(r['wins'],r['total']):>5.1f}% WR")

    print("\n" + "=" * 70)
    print("RECOMMENDATION")
    print("=" * 70)
    print("""
    If divergence shows >3-5% WR edge, add to score_v4():

    # RSI Regular Divergence (reversal signal — strongest)
    if row.get("rsi_bull_div", False):  sb += 4
    if row.get("rsi_bear_div", False):  ss += 4

    # RSI Hidden Divergence (continuation)
    if row.get("rsi_hidden_bull_div", False):  sb += 2
    if row.get("rsi_hidden_bear_div", False):  ss += 2

    # MACD Divergence (confirmation)
    if row.get("macd_bull_div", False):  sb += 3
    if row.get("macd_bear_div", False):  ss += 3

    For SCOUT live: port divergence.py swing detection into scan loop.
    """)

    elapsed = time.time() - t0
    print(f"⏱  Done in {elapsed:.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
