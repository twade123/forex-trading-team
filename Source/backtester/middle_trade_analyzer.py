#!/usr/bin/env python3
"""
Middle Trade Analyzer — Extract exact entry rules for non-extreme setups.
Runs pure SQL/Python against backtest DB. Zero LLM tokens.

Usage: python middle_trade_analyzer.py
Output: middle_trade_playbook.json + middle_trade_report.md
"""

import sqlite3
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime

DB_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'Database', 'v2/trading_forex.db'))
if not os.path.exists(DB_PATH):
    # Fallback
    DB_PATH = os.path.expanduser('~/jarvis/Database/v2/trading_forex.db')

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       '..', '..', '.openclaw', 'workspace', 'notes')
os.makedirs(OUT_DIR, exist_ok=True)

MIDDLE_FILTER = """
    rsi BETWEEN 35 AND 65
    AND stoch_k BETWEEN 25 AND 75
    AND stoch_d BETWEEN 25 AND 75
    AND entry_price > bb_lower
    AND entry_price < bb_upper
"""

TARGET_SETUPS = [
    ('S15', 'ranging'),
    ('S8', 'strong_trend'),
    ('S15', 'exhaustion'),
    ('S10', 'strong_trend'),
    ('S18', 'strong_trend'),
    ('S15', 'squeeze'),
]

PAIRS = ['EUR_USD','GBP_USD','USD_JPY','AUD_USD','NZD_USD','EUR_GBP',
         'EUR_JPY','GBP_JPY','AUD_JPY','USD_CHF','EUR_CHF','EUR_AUD','USD_CAD']


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def analyze_setup(db, base_setup, regime):
    """Full deep-dive on one setup+regime combo in the middle zone."""
    print(f"\n{'='*60}")
    print(f"  Analyzing {base_setup} / {regime} — middle zone")
    print(f"{'='*60}")

    base_where = f"""
        base_setup = '{base_setup}' AND regime = '{regime}'
        AND {MIDDLE_FILTER}
    """

    result = {}

    # ── 1. Overall stats ──
    row = db.execute(f"""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) as losses,
               AVG(CASE WHEN result='win' THEN pips END) as avg_win_pips,
               AVG(CASE WHEN result='loss' THEN ABS(pips) END) as avg_loss_pips,
               AVG(pips) as avg_pips,
               AVG(max_favorable_pips) as avg_mfe,
               AVG(max_adverse_pips) as avg_mae,
               AVG(candles_to_exit) as avg_candles,
               AVG(atr) as avg_atr
        FROM backtest_trades WHERE {base_where}
    """).fetchone()

    total = row['total']
    if total < 50:
        print(f"  ⚠️  Only {total} trades — skipping")
        return None

    wins = row['wins'] or 0
    losses = row['losses'] or 0
    wr = wins / total * 100
    avg_win = row['avg_win_pips'] or 0
    avg_loss = row['avg_loss_pips'] or 0
    pf = (wins * avg_win) / (losses * avg_loss) if losses and avg_loss else 99

    result['overview'] = {
        'total_trades': total, 'wins': wins, 'losses': losses,
        'win_rate': round(wr, 2), 'profit_factor': round(pf, 2),
        'avg_win_pips': round(avg_win, 2), 'avg_loss_pips': round(avg_loss, 2),
        'avg_pips': round(row['avg_pips'] or 0, 2),
        'avg_mfe': round(row['avg_mfe'] or 0, 2),
        'avg_mae': round(row['avg_mae'] or 0, 2),
        'avg_candles_held': round(row['avg_candles'] or 0, 1),
        'avg_atr': round(row['avg_atr'] or 0, 5),
    }
    print(f"  Total: {total} | WR: {wr:.1f}% | PF: {pf:.1f} | Avg win: {avg_win:.1f} pips | Avg loss: {avg_loss:.1f} pips")
    print(f"  MFE: {result['overview']['avg_mfe']:.1f} pips | MAE: {result['overview']['avg_mae']:.1f} pips | Candles: {result['overview']['avg_candles_held']:.0f}")

    # ── 2. RSI sweet spots (5-point bins) ──
    print("\n  📊 RSI Sweet Spots:")
    rsi_bins = db.execute(f"""
        SELECT CAST(rsi/5 AS INT)*5 as rsi_bin,
               COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr
        FROM backtest_trades WHERE {base_where}
        GROUP BY rsi_bin ORDER BY rsi_bin
    """).fetchall()
    result['rsi_bins'] = []
    for r in rsi_bins:
        bin_label = f"{r['rsi_bin']}-{r['rsi_bin']+5}"
        result['rsi_bins'].append({
            'range': bin_label, 'total': r['total'], 'win_rate': r['wr']
        })
        marker = " ★" if r['wr'] and r['wr'] >= wr + 2 else ""
        print(f"    RSI {bin_label:>6}: {r['total']:>5} trades, {r['wr']:.1f}% WR{marker}")

    # ── 3. Stoch K sweet spots ──
    print("\n  📊 Stoch K Sweet Spots:")
    stoch_bins = db.execute(f"""
        SELECT CAST(stoch_k/10 AS INT)*10 as sk_bin,
               COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr
        FROM backtest_trades WHERE {base_where}
        GROUP BY sk_bin ORDER BY sk_bin
    """).fetchall()
    result['stoch_bins'] = []
    for r in stoch_bins:
        bin_label = f"{r['sk_bin']}-{r['sk_bin']+10}"
        result['stoch_bins'].append({
            'range': bin_label, 'total': r['total'], 'win_rate': r['wr']
        })
        marker = " ★" if r['wr'] and r['wr'] >= wr + 2 else ""
        print(f"    Stoch {bin_label:>6}: {r['total']:>5} trades, {r['wr']:.1f}% WR{marker}")

    # ── 4. ADX sweet spots ──
    print("\n  📊 ADX Sweet Spots:")
    adx_bins = db.execute(f"""
        SELECT CAST(adx/5 AS INT)*5 as adx_bin,
               COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr
        FROM backtest_trades WHERE {base_where}
        GROUP BY adx_bin ORDER BY adx_bin
    """).fetchall()
    result['adx_bins'] = []
    for r in adx_bins:
        bin_label = f"{r['adx_bin']}-{r['adx_bin']+5}"
        result['adx_bins'].append({
            'range': bin_label, 'total': r['total'], 'win_rate': r['wr']
        })
        marker = " ★" if r['wr'] and r['wr'] >= wr + 2 else ""
        print(f"    ADX {bin_label:>6}: {r['total']:>5} trades, {r['wr']:.1f}% WR{marker}")

    # ── 5. BB position (where within the bands) ──
    print("\n  📊 BB Position (0=lower band, 1=upper band):")
    bb_bins = db.execute(f"""
        SELECT CAST(((entry_price - bb_lower) / NULLIF(bb_upper - bb_lower, 0)) * 10 AS INT) as bb_decile,
               COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr
        FROM backtest_trades WHERE {base_where}
            AND bb_upper != bb_lower
        GROUP BY bb_decile ORDER BY bb_decile
    """).fetchall()
    result['bb_position'] = []
    for r in bb_bins:
        if r['bb_decile'] is None: continue
        pct = r['bb_decile'] * 10
        result['bb_position'].append({
            'percentile': f"{pct}-{pct+10}%", 'total': r['total'], 'win_rate': r['wr']
        })
        bar = "█" * int((r['wr'] or 0) / 5)
        print(f"    {pct:>3}-{pct+10:<3}%: {r['total']:>5} trades, {r['wr']:.1f}% WR {bar}")

    # ── 6. Entry candle patterns ──
    print("\n  🕯️ Entry Candle Patterns:")
    candles = db.execute(f"""
        SELECT entry_candle_pattern,
               COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr,
               AVG(CASE WHEN result='win' THEN pips END) as avg_win
        FROM backtest_trades WHERE {base_where}
        GROUP BY entry_candle_pattern
        HAVING COUNT(*) >= 20
        ORDER BY wr DESC
    """).fetchall()
    result['candle_patterns'] = []
    for r in candles:
        result['candle_patterns'].append({
            'pattern': r['entry_candle_pattern'] or 'none',
            'total': r['total'], 'win_rate': r['wr'],
            'avg_win_pips': round(r['avg_win'] or 0, 2)
        })
        print(f"    {(r['entry_candle_pattern'] or 'none'):>20}: {r['total']:>5} trades, {r['wr']:.1f}% WR, {r['avg_win'] or 0:.1f} avg pips")

    # ── 7. Trigger reasons ──
    print("\n  🎯 Entry Triggers (top 15):")
    triggers = db.execute(f"""
        SELECT 
            CASE 
                WHEN trigger_reason LIKE '%engulfing%' THEN 'engulfing_pattern'
                WHEN trigger_reason LIKE '%hammer%' THEN 'hammer'
                WHEN trigger_reason LIKE '%shooting_star%' THEN 'shooting_star'
                WHEN trigger_reason LIKE '%doji%' THEN 'doji'
                WHEN trigger_reason LIKE '%Stoch%cross%' THEN 'stoch_cross'
                WHEN trigger_reason LIKE '%RSI%divergence%' THEN 'rsi_divergence'
                WHEN trigger_reason LIKE '%RSI%' THEN 'rsi_signal'
                WHEN trigger_reason LIKE '%CCI%' THEN 'cci_reversal'
                WHEN trigger_reason LIKE '%MACD%' THEN 'macd_signal'
                WHEN trigger_reason LIKE '%BB%' OR trigger_reason LIKE '%Bollinger%' THEN 'bb_signal'
                WHEN trigger_reason LIKE '%SAR%' THEN 'sar_flip'
                WHEN trigger_reason LIKE '%morning_star%' OR trigger_reason LIKE '%evening_star%' THEN 'star_pattern'
                ELSE 'other'
            END as trigger_type,
            COUNT(*) as total,
            SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr,
            AVG(CASE WHEN result='win' THEN pips END) as avg_win
        FROM backtest_trades WHERE {base_where}
        GROUP BY trigger_type
        HAVING COUNT(*) >= 10
        ORDER BY total DESC LIMIT 15
    """).fetchall()
    result['triggers'] = []
    for r in triggers:
        result['triggers'].append({
            'trigger': r['trigger_type'], 'total': r['total'],
            'win_rate': r['wr'], 'avg_win_pips': round(r['avg_win'] or 0, 2)
        })
        print(f"    {r['trigger_type']:>20}: {r['total']:>5} trades, {r['wr']:.1f}% WR, {r['avg_win'] or 0:.1f} avg pips")

    # ── 8. Direction analysis ──
    print("\n  ↕️ Direction:")
    dirs = db.execute(f"""
        SELECT direction,
               COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr
        FROM backtest_trades WHERE {base_where}
        GROUP BY direction
    """).fetchall()
    result['direction'] = []
    for r in dirs:
        result['direction'].append({
            'direction': r['direction'], 'total': r['total'], 'win_rate': r['wr']
        })
        print(f"    {r['direction']:>6}: {r['total']:>5} trades, {r['wr']:.1f}% WR")

    # ── 9. H4 alignment ──
    print("\n  📐 H4 Agreement:")
    h4 = db.execute(f"""
        SELECT h4_agrees,
               COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr
        FROM backtest_trades WHERE {base_where}
        GROUP BY h4_agrees
    """).fetchall()
    result['h4_alignment'] = []
    for r in h4:
        result['h4_alignment'].append({
            'h4_agrees': r['h4_agrees'], 'total': r['total'], 'win_rate': r['wr']
        })
        print(f"    H4 {r['h4_agrees'] or '?':>6}: {r['total']:>5} trades, {r['wr']:.1f}% WR")

    # ── 10. Session (time of day) ──
    print("\n  🕐 Trading Session:")
    sessions = db.execute(f"""
        SELECT session,
               COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr,
               AVG(CASE WHEN result='win' THEN pips END) as avg_win
        FROM backtest_trades WHERE {base_where}
        GROUP BY session ORDER BY total DESC
    """).fetchall()
    result['sessions'] = []
    for r in sessions:
        result['sessions'].append({
            'session': r['session'] or 'unknown', 'total': r['total'],
            'win_rate': r['wr'], 'avg_win_pips': round(r['avg_win'] or 0, 2)
        })
        print(f"    {(r['session'] or '?'):>12}: {r['total']:>5} trades, {r['wr']:.1f}% WR, {r['avg_win'] or 0:.1f} avg pips")

    # ── 11. Per-pair stats ──
    print("\n  💱 Per-Pair Performance:")
    pair_stats = db.execute(f"""
        SELECT pair,
               COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr,
               AVG(CASE WHEN result='win' THEN pips END) as avg_win,
               AVG(max_favorable_pips) as avg_mfe
        FROM backtest_trades WHERE {base_where}
        GROUP BY pair ORDER BY wr DESC
    """).fetchall()
    result['pairs'] = []
    for r in pair_stats:
        result['pairs'].append({
            'pair': r['pair'], 'total': r['total'], 'win_rate': r['wr'],
            'avg_win_pips': round(r['avg_win'] or 0, 2),
            'avg_mfe': round(r['avg_mfe'] or 0, 2)
        })
        print(f"    {r['pair']:>10}: {r['total']:>5} trades, {r['wr']:.1f}% WR, {r['avg_win'] or 0:.1f} avg win, MFE={r['avg_mfe'] or 0:.1f}")

    # ── 12. MFE/MAE analysis for optimal exits ──
    print("\n  📏 Exit Optimization (MFE/MAE in ATR multiples):")
    exit_data = db.execute(f"""
        SELECT result,
               AVG(max_favorable_pips / NULLIF(atr * 10000, 0)) as mfe_atr,
               AVG(max_adverse_pips / NULLIF(atr * 10000, 0)) as mae_atr,
               AVG(pips / NULLIF(atr * 10000, 0)) as pips_atr,
               AVG(candles_to_exit) as avg_candles,
               MIN(candles_to_exit) as min_candles,
               MAX(candles_to_exit) as max_candles
        FROM backtest_trades WHERE {base_where}
            AND atr > 0
        GROUP BY result
    """).fetchall()
    result['exit_analysis'] = []
    for r in exit_data:
        result['exit_analysis'].append({
            'result': r['result'],
            'mfe_atr': round(r['mfe_atr'] or 0, 2),
            'mae_atr': round(r['mae_atr'] or 0, 2),
            'pips_atr': round(r['pips_atr'] or 0, 2),
            'avg_candles': round(r['avg_candles'] or 0, 1),
        })
        print(f"    {r['result']:>5}: MFE={r['mfe_atr'] or 0:.2f}×ATR, MAE={r['mae_atr'] or 0:.2f}×ATR, "
              f"pips={r['pips_atr'] or 0:.2f}×ATR, candles={r['avg_candles'] or 0:.0f}")

    # ── 13. RR/SL multiplier sweet spots ──
    print("\n  🎰 Best RR/SL Combinations:")
    rr_sl = db.execute(f"""
        SELECT rr_mult, sl_mult,
               COUNT(*) as total,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr,
               AVG(pips) as avg_pips
        FROM backtest_trades WHERE {base_where}
        GROUP BY rr_mult, sl_mult
        HAVING COUNT(*) >= 50
        ORDER BY wr DESC LIMIT 10
    """).fetchall()
    result['rr_sl_combos'] = []
    for r in rr_sl:
        result['rr_sl_combos'].append({
            'rr_mult': r['rr_mult'], 'sl_mult': r['sl_mult'],
            'total': r['total'], 'win_rate': r['wr'],
            'avg_pips': round(r['avg_pips'] or 0, 2)
        })
        print(f"    TP={r['rr_mult']}×ATR SL={r['sl_mult']}×ATR: {r['total']:>5} trades, {r['wr']:.1f}% WR, {r['avg_pips'] or 0:.1f} avg pips")

    # ── 14. Loser analysis — what's different about losses? ──
    print("\n  ❌ Loser Profile (what to AVOID):")
    loser_profile = db.execute(f"""
        SELECT AVG(rsi) as avg_rsi,
               AVG(stoch_k) as avg_stoch,
               AVG(adx) as avg_adx,
               AVG(bb_width) as avg_bbw,
               AVG(max_adverse_pips) as avg_mae,
               AVG(candles_to_exit) as avg_candles
        FROM backtest_trades
        WHERE {base_where} AND result='loss'
    """).fetchone()
    winner_profile = db.execute(f"""
        SELECT AVG(rsi) as avg_rsi,
               AVG(stoch_k) as avg_stoch,
               AVG(adx) as avg_adx,
               AVG(bb_width) as avg_bbw,
               AVG(max_adverse_pips) as avg_mae,
               AVG(candles_to_exit) as avg_candles
        FROM backtest_trades
        WHERE {base_where} AND result='win'
    """).fetchone()
    result['loser_vs_winner'] = {
        'loser': {k: round(loser_profile[k] or 0, 3) for k in loser_profile.keys()},
        'winner': {k: round(winner_profile[k] or 0, 3) for k in winner_profile.keys()},
    }
    print(f"    {'':>15} {'WINNERS':>10} {'LOSERS':>10} {'DELTA':>10}")
    for k in ['avg_rsi', 'avg_stoch', 'avg_adx', 'avg_bbw', 'avg_mae', 'avg_candles']:
        w = winner_profile[k] or 0
        l = loser_profile[k] or 0
        d = l - w
        print(f"    {k:>15}: {w:>10.2f} {l:>10.2f} {d:>+10.2f}")

    # ── 15. Price vs SMA position ──
    print("\n  📍 Price vs SMA:")
    sma_pos = db.execute(f"""
        SELECT price_vs_sma50, price_vs_sma100,
               COUNT(*) as total,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr
        FROM backtest_trades WHERE {base_where}
        GROUP BY price_vs_sma50, price_vs_sma100
        HAVING COUNT(*) >= 20
        ORDER BY wr DESC
    """).fetchall()
    result['sma_position'] = []
    for r in sma_pos:
        result['sma_position'].append({
            'vs_sma50': r['price_vs_sma50'], 'vs_sma100': r['price_vs_sma100'],
            'total': r['total'], 'win_rate': r['wr']
        })
        print(f"    SMA50={r['price_vs_sma50'] or '?':>6} SMA100={r['price_vs_sma100'] or '?':>6}: {r['total']:>5} trades, {r['wr']:.1f}% WR")

    # ── 16. MACD histogram direction ──
    print("\n  📈 MACD Histogram:")
    macd_bins = db.execute(f"""
        SELECT CASE WHEN macd_hist > 0 THEN 'positive'
                    WHEN macd_hist < 0 THEN 'negative'
                    ELSE 'zero' END as macd_dir,
               COUNT(*) as total,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr
        FROM backtest_trades WHERE {base_where}
        GROUP BY macd_dir ORDER BY total DESC
    """).fetchall()
    result['macd_histogram'] = []
    for r in macd_bins:
        result['macd_histogram'].append({
            'direction': r['macd_dir'], 'total': r['total'], 'win_rate': r['wr']
        })
        print(f"    MACD hist {r['macd_dir']:>10}: {r['total']:>5} trades, {r['wr']:.1f}% WR")

    # ── 17. Loss streak analysis ──
    print("\n  📉 Entry After Loss Streaks:")
    streaks = db.execute(f"""
        SELECT loss_streak_at_entry,
               COUNT(*) as total,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr
        FROM backtest_trades WHERE {base_where}
        GROUP BY loss_streak_at_entry
        HAVING COUNT(*) >= 20
        ORDER BY loss_streak_at_entry
    """).fetchall()
    result['loss_streaks'] = []
    for r in streaks:
        result['loss_streaks'].append({
            'streak': r['loss_streak_at_entry'], 'total': r['total'], 'win_rate': r['wr']
        })
        flag = " ⚠️ AVOID" if r['wr'] and r['wr'] < wr - 5 else ""
        print(f"    After {r['loss_streak_at_entry']} losses: {r['total']:>5} trades, {r['wr']:.1f}% WR{flag}")

    # ── 18. Pivot proximity ──
    print("\n  📌 Pivot Proximity (ATR distance):")
    pivot_bins = db.execute(f"""
        SELECT CASE WHEN dist_to_daily_pivot_atr < 0.5 THEN 'near (<0.5 ATR)'
                    WHEN dist_to_daily_pivot_atr < 1.0 THEN 'medium (0.5-1 ATR)'
                    WHEN dist_to_daily_pivot_atr < 2.0 THEN 'far (1-2 ATR)'
                    ELSE 'very far (>2 ATR)' END as prox,
               COUNT(*) as total,
               ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as wr
        FROM backtest_trades WHERE {base_where}
            AND dist_to_daily_pivot_atr IS NOT NULL
        GROUP BY prox ORDER BY total DESC
    """).fetchall()
    result['pivot_proximity'] = []
    for r in pivot_bins:
        result['pivot_proximity'].append({
            'proximity': r['prox'], 'total': r['total'], 'win_rate': r['wr']
        })
        print(f"    {r['prox']:>20}: {r['total']:>5} trades, {r['wr']:.1f}% WR")

    return result


def build_playbook_entry(setup, regime, data):
    """Convert analysis data into a scout-compatible playbook entry."""
    if not data:
        return None

    ov = data['overview']

    # Find best RSI range
    best_rsi = max(data['rsi_bins'], key=lambda x: x['win_rate'] if x['total'] >= 50 else 0) if data['rsi_bins'] else None
    # Find best stoch range
    best_stoch = max(data['stoch_bins'], key=lambda x: x['win_rate'] if x['total'] >= 50 else 0) if data['stoch_bins'] else None
    # Find best ADX range
    best_adx = max(data['adx_bins'], key=lambda x: x['win_rate'] if x['total'] >= 50 else 0) if data['adx_bins'] else None
    # Find best candle pattern
    best_candle = max(data['candle_patterns'], key=lambda x: x['win_rate'] if x['total'] >= 30 else 0) if data['candle_patterns'] else None
    # Find best trigger
    best_trigger = max(data['triggers'], key=lambda x: x['win_rate'] if x['total'] >= 30 else 0) if data['triggers'] else None
    # Find best RR/SL
    best_rr = max(data['rr_sl_combos'], key=lambda x: x['win_rate'] if x['total'] >= 50 else 0) if data['rr_sl_combos'] else None
    # Find best session
    best_session = max(data['sessions'], key=lambda x: x['win_rate'] if x['total'] >= 30 else 0) if data['sessions'] else None
    # Top pairs (WR >= overall WR and >= 50 trades)
    top_pairs = [p for p in data['pairs'] if p['win_rate'] and p['win_rate'] >= ov['win_rate'] and p['total'] >= 50]

    entry = {
        'id': f"MID_{setup}_{regime.upper()}",
        'name': f"Middle Zone {setup} {regime.replace('_', ' ').title()}",
        'base_setup': setup,
        'regime': regime,
        'category': 'middle_zone',
        'stats': ov,
        'entry_conditions': {
            'rsi_range': best_rsi['range'] if best_rsi else '35-65',
            'stoch_range': best_stoch['range'] if best_stoch else '25-75',
            'adx_range': best_adx['range'] if best_adx else 'any',
            'bb_position': 'inside_bands',
            'preferred_candle': best_candle['pattern'] if best_candle else 'any',
            'preferred_trigger': best_trigger['trigger'] if best_trigger else 'any',
        },
        'exit_rules': {
            'tp_atr': best_rr['rr_mult'] if best_rr else 2.0,
            'sl_atr': best_rr['sl_mult'] if best_rr else 2.5,
            'max_candles': int(ov['avg_candles_held'] * 2),
        },
        'pair_filter': [p['pair'] for p in top_pairs[:6]],
        'best_session': best_session['session'] if best_session else 'any',
        'h4_alignment': data['h4_alignment'],
        'direction_stats': data['direction'],
        'anti_patterns': {
            'loser_profile': data['loser_vs_winner']['loser'],
            'loss_streak_limit': next(
                (s['streak'] for s in data['loss_streaks']
                 if s['win_rate'] and s['win_rate'] < ov['win_rate'] - 10),
                None
            ),
        },
        'full_analysis': data,
    }
    return entry


def generate_report(playbook):
    """Generate markdown report from playbook entries."""
    lines = [
        "# Middle Trade Deep Dive — Scout Playbook",
        f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"\nAnalyzed {len(playbook)} patterns from backtest DB ({DB_PATH})\n",
    ]

    for entry in playbook:
        if not entry:
            continue
        ov = entry['stats']
        lines.append(f"\n## {entry['id']}: {entry['name']}")
        lines.append(f"**{ov['win_rate']}% WR | {ov['total_trades']} trades | PF {ov['profit_factor']} | "
                     f"Avg win {ov['avg_win_pips']} pips | MFE {ov['avg_mfe']} pips**\n")

        lines.append("### Entry Conditions")
        ec = entry['entry_conditions']
        lines.append(f"- RSI: {ec['rsi_range']}")
        lines.append(f"- Stoch K: {ec['stoch_range']}")
        lines.append(f"- ADX: {ec['adx_range']}")
        lines.append(f"- BB: Inside bands")
        lines.append(f"- Preferred candle: {ec['preferred_candle']}")
        lines.append(f"- Preferred trigger: {ec['preferred_trigger']}")

        lines.append("\n### Exit Rules")
        ex = entry['exit_rules']
        lines.append(f"- TP: {ex['tp_atr']}×ATR")
        lines.append(f"- SL: {ex['sl_atr']}×ATR")
        lines.append(f"- Max hold: {ex['max_candles']} candles")
        lines.append(f"- Avg candles to exit: {ov['avg_candles_held']}")

        lines.append(f"\n### Best Pairs")
        for p in entry['pair_filter']:
            pair_data = next((x for x in entry['full_analysis']['pairs'] if x['pair'] == p), {})
            lines.append(f"- {p}: {pair_data.get('win_rate', '?')}% WR, {pair_data.get('total', '?')} trades")

        lines.append(f"\n### Best Session: {entry['best_session']}")

        lines.append("\n### Direction")
        for d in entry['direction_stats']:
            lines.append(f"- {d['direction']}: {d['total']} trades, {d['win_rate']}% WR")

        lines.append("\n### H4 Alignment Impact")
        for h in entry['h4_alignment']:
            lines.append(f"- H4 {h['h4_agrees']}: {h['total']} trades, {h['win_rate']}% WR")

        # Best RR/SL combos
        lines.append("\n### Best TP/SL Combinations")
        for combo in entry['full_analysis']['rr_sl_combos'][:5]:
            lines.append(f"- TP={combo['rr_mult']}×ATR SL={combo['sl_mult']}×ATR: "
                        f"{combo['total']} trades, {combo['win_rate']}% WR, {combo['avg_pips']} avg pips")

        # Loser profile
        ap = entry['anti_patterns']
        lines.append("\n### Anti-Patterns (AVOID)")
        lp = ap['loser_profile']
        wp = entry['full_analysis']['loser_vs_winner']['winner']
        lines.append(f"- Losers avg RSI: {lp['avg_rsi']:.1f} vs Winners: {wp['avg_rsi']:.1f}")
        lines.append(f"- Losers avg Stoch: {lp['avg_stoch']:.1f} vs Winners: {wp['avg_stoch']:.1f}")
        lines.append(f"- Losers avg ADX: {lp['avg_adx']:.1f} vs Winners: {wp['avg_adx']:.1f}")
        lines.append(f"- Losers avg BB width: {lp['avg_bbw']:.4f} vs Winners: {wp['avg_bbw']:.4f}")
        if ap['loss_streak_limit'] is not None:
            lines.append(f"- ⚠️ Stop trading after {ap['loss_streak_limit']} consecutive losses")

        lines.append("\n---")

    return "\n".join(lines)


def main():
    print(f"🔍 Middle Trade Analyzer")
    print(f"   DB: {DB_PATH}")
    print(f"   Target setups: {len(TARGET_SETUPS)}")

    db = get_db()

    # Quick sanity check
    total = db.execute("SELECT COUNT(*) FROM backtest_trades").fetchone()[0]
    middle = db.execute(f"SELECT COUNT(*) FROM backtest_trades WHERE {MIDDLE_FILTER}").fetchone()[0]
    print(f"\n   Total trades: {total:,}")
    print(f"   Middle zone: {middle:,} ({middle/total*100:.1f}%)")

    playbook = []
    full_data = {}

    for setup, regime in TARGET_SETUPS:
        data = analyze_setup(db, setup, regime)
        if data:
            entry = build_playbook_entry(setup, regime, data)
            playbook.append(entry)
            full_data[f"{setup}_{regime}"] = data

    db.close()

    # ── Write playbook JSON (scout-compatible) ──
    # Strip full_analysis from JSON to keep it lean
    lean_playbook = []
    for e in playbook:
        if not e:
            continue
        lean = {k: v for k, v in e.items() if k != 'full_analysis'}
        lean_playbook.append(lean)

    json_path = os.path.join(OUT_DIR, 'middle-trade-playbook.json')
    with open(json_path, 'w') as f:
        json.dump(lean_playbook, f, indent=2)
    print(f"\n✅ Playbook JSON: {json_path}")

    # Also write to Forex Trading Team config for scout to load
    scout_json_path = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', '..', 'Config', 'middle_zone_playbook.json'))
    with open(scout_json_path, 'w') as f:
        json.dump(lean_playbook, f, indent=2)
    print(f"✅ Scout config JSON: {scout_json_path}")

    # ── Write report ──
    report_path = os.path.join(OUT_DIR, 'middle-trade-deep-dive.md')
    report = generate_report(playbook)
    with open(report_path, 'w') as f:
        f.write(report)
    print(f"✅ Report: {report_path}")

    print(f"\n{'='*60}")
    print(f"  SUMMARY: {len(playbook)} viable middle-zone patterns")
    for e in playbook:
        if not e:
            continue
        print(f"    {e['id']:>30}: {e['stats']['win_rate']}% WR, {e['stats']['total_trades']} trades, "
              f"pairs={len(e['pair_filter'])}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
