#!/usr/bin/env python3
"""Retroactive Setup Classifier for Trades.

Reads trades from live_trades (unified table in trading_forex.db), classifies
each using setup_classifier.py, and updates setup_trades/setup_revenue/user_snipe_list.

Usage:
    python classify_manual_trades.py              # Classify all unclassified trades
    python classify_manual_trades.py --all        # Re-classify all trades
    python classify_manual_trades.py --trade 324  # Classify specific trade
"""

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

# Setup paths
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_SOURCE_DIR = os.path.dirname(SOURCE_DIR)  # Source/
DATA_DIR = os.path.join(os.path.dirname(SOURCE_DIR), 'Data')
DB_DIR = os.path.join(os.path.dirname(os.path.dirname(SOURCE_DIR)), 'Database')

TRADE_LOG_DB = os.path.join(DATA_DIR, 'trade_log.db')
# Revenue tables now live in trade_log.db
TREVOR_DB = os.path.join(DATA_DIR, 'trade_log.db')

# Add source to path for imports (both scripts/ and Source/)
import sys
sys.path.insert(0, SOURCE_DIR)
sys.path.insert(0, PARENT_SOURCE_DIR)

from setup_classifier import classify_setups, get_best_setups
from db_pool import get_trading_forex

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def _determine_regime(indicators: Dict) -> str:
    """Determine market regime from indicators."""
    adx = indicators.get('adx', 25)
    bb_width = indicators.get('bb_width', 0)
    rsi = indicators.get('rsi', 50)
    
    if bb_width > 0 and bb_width < 0.003:
        return 'squeeze'
    if adx > 25:
        if rsi > 70 or rsi < 30:
            return 'exhaustion'
        return 'strong_trend'
    if adx < 20:
        return 'ranging'
    # 20-25 ADX = transitional
    return 'ranging'


def _parse_indicators(trade: Dict) -> Dict:
    """Parse stored indicators JSON + top-level fields into classifier-ready dict."""
    ind = {}
    
    # Parse JSON indicators column
    raw = trade.get('indicators')
    if raw and isinstance(raw, str):
        try:
            ind = json.loads(raw)
        except json.JSONDecodeError:
            pass
    elif isinstance(raw, dict):
        ind = raw
    
    # Map field names to what classify_setups expects
    mapped = {
        'rsi': float(ind.get('rsi', trade.get('rsi', 50))),
        'stoch_k': float(ind.get('stoch_k', trade.get('stoch_k', 50))),
        'stoch_d': float(ind.get('stoch_d', 50)),
        'adx': float(ind.get('adx', 25)),
        'macd_value': float(ind.get('macd_line', ind.get('macd_value', 0))),
        'macd_signal': float(ind.get('macd_signal', 0)),
        'macd_hist': float(ind.get('macd_histogram', ind.get('macd_hist', 0))),
        'bb_upper': float(ind.get('bb_upper', 0)),
        'bb_lower': float(ind.get('bb_lower', 0)),
        'bb_mid': float(ind.get('bb_middle', ind.get('bb_mid', 0))),
        'bb_width': float(ind.get('bb_width', 0)),
        'close': float(ind.get('close', trade.get('entry_price', 0))),
        'sma50': float(ind.get('sma_50', ind.get('sma50', 0))),
        'sma100': float(ind.get('sma_100', ind.get('sma100', 0))),
        'sar': float(ind.get('parabolic_sar', ind.get('sar', 0))),
        'cci': float(ind.get('cci', 0)),
        'ema_21': float(ind.get('ema_21', ind.get('ema21', 0))),
        'ema_55': float(ind.get('ema_55', ind.get('ema55', 0))),
        'ema_100': float(ind.get('ema_100', ind.get('ema100', 0))),
        'atr': float(ind.get('atr', 0)),
    }
    return mapped


def classify_single_trade(trade: Dict) -> List[Dict]:
    """Classify a single trade and return matching setups."""
    indicators = _parse_indicators(trade)
    regime = _determine_regime(indicators)
    
    # We don't have candle patterns or chart patterns stored, pass empty
    setups = classify_setups(
        indicators=indicators,
        candle_patterns={},
        chart_patterns=[],
        regime=regime,
    )
    return setups


def classify_and_update(reclassify_all: bool = False, specific_trade: str = None, user_id: int = None):
    """Main function: classify manual trades and update databases."""
    # Resolve user_id from arg or TRADING_USER_ID env (set by serve_ui.py)
    if not user_id:
        _env = os.environ.get("TRADING_USER_ID")
        user_id = int(_env) if _env else None

    # Read trades from unified live_trades table
    _lt_conn = get_trading_forex()
    _lt_conn.row_factory = sqlite3.Row
    if specific_trade:
        rows = _lt_conn.execute(
            "SELECT *, id as trade_id FROM live_trades WHERE id = ? OR oanda_trade_id = ?",
            (specific_trade, specific_trade)
        ).fetchall()
    elif reclassify_all:
        rows = _lt_conn.execute("SELECT *, id as trade_id FROM live_trades").fetchall()
    else:
        rows = _lt_conn.execute(
            "SELECT *, id as trade_id FROM live_trades WHERE classified_setup IS NULL"
        ).fetchall()
    
    if not rows:
        logger.info("No trades to classify")
        return
    
    logger.info(f"Classifying {len(rows)} manual trades...")
    
    results = []
    for row in rows:
        trade = dict(row)
        trade_id = trade['trade_id']
        pair = trade['pair']
        direction = trade['direction']
        result = trade.get('result')
        pips = trade.get('pips')
        realized_pl = trade.get('realized_pl')
        
        # Skip test trades
        if pair == 'TEST_USD':
            logger.info(f"  Skipping test trade {trade_id}")
            continue
        
        setups = classify_single_trade(trade)
        best = get_best_setups(setups, min_confidence=0.3, max_results=3)
        
        # Pick the best matching setup in the trade's direction
        matched = None
        for s in (best or setups):
            if s['direction'] == direction or s['direction'] == 'neutral':
                matched = s
                break
        
        # If no directional match, take best overall
        if not matched and setups:
            matched = setups[0]
        
        setup_name = matched['setup'] if matched else 'unclassified'
        setup_full = f"{matched['setup']} {matched['name']}" if matched else 'unclassified'
        confidence = matched['confidence'] if matched else 0
        regime_valid = matched['regime_valid'] if matched else False
        
        outcome = 'win' if result == 'win' else ('loss' if result == 'loss' else None)
        
        all_setups_str = [f"{s['setup']}({s['direction']},{s['confidence']:.0%})" for s in setups[:5]]
        
        results.append({
            'trade_id': trade_id,
            'pair': pair,
            'direction': direction,
            'setup_name': setup_name,
            'setup_full': setup_full,
            'confidence': confidence,
            'regime_valid': regime_valid,
            'result': result,
            'pips': pips,
            'realized_pl': realized_pl,
            'outcome': outcome,
            'all_setups': all_setups_str,
            'entry_price': trade.get('entry_price'),
            'exit_price': trade.get('exit_price'),
            'entry_time': trade.get('entry_time'),
            'exit_time': trade.get('exit_time'),
            'units': trade.get('units'),
        })
        
        status = f"{'✅' if regime_valid else '⚠️'} {setup_name}"
        print(f"  Trade {trade_id:>6} {pair:<10} {direction:<4} → {status:<20} "
              f"conf={confidence:.0%} | {result or 'open':>4} {pips or 0:>+7.1f} pips "
              f"| all: {', '.join(all_setups_str[:3])}")
    
    if not results:
        logger.info("No trades classified")
        return
    
    # ── Update live_trades: classified_setup column ──
    _lt_upd = get_trading_forex()
    for r in results:
        _lt_upd.execute(
            "UPDATE live_trades SET classified_setup = ? WHERE id = ? OR oanda_trade_id = ?",
            (r['setup_full'], r['trade_id'], r['trade_id'])
        )
    _lt_upd.commit()
    logger.info(f"Updated {len(results)} trades in live_trades.classified_setup")
    
    # ── Update v2/trading_forex.db: setup_trades ──
    def _trevor_conn():
        c = sqlite3.connect(TREVOR_DB, timeout=60)
        c.execute("PRAGMA journal_mode=DELETE")
        c.execute("PRAGMA busy_timeout=60000")
        return c
    
    for r in results:
        if r['outcome'] is None:
            continue
        
        duration_mins = 0
        if r.get('entry_time') and r.get('exit_time'):
            try:
                from dateutil import parser as dtparser
                t1 = dtparser.parse(r['entry_time'])
                t2 = dtparser.parse(r['exit_time'])
                duration_mins = int((t2 - t1).total_seconds() / 60)
            except Exception:
                pass
        
        for attempt in range(5):
            try:
                conn = _trevor_conn()
                existing = conn.execute(
                    "SELECT id FROM setup_trades WHERE trade_id = ? AND user_id = ?",
                    (r['trade_id'], user_id)
                ).fetchone()

                if existing:
                    conn.execute("""
                        UPDATE setup_trades SET setup_name = ?, pnl_pips = ?, pnl_usd = ?,
                        outcome = ?, source = 'manual_classified'
                        WHERE trade_id = ? AND user_id = ?
                    """, (r['setup_name'], r['pips'], r['realized_pl'],
                          r['outcome'], r['trade_id'], user_id))
                else:
                    conn.execute("""
                        INSERT OR REPLACE INTO setup_trades
                        (trade_id, setup_name, pair, direction, entry_price, exit_price,
                         units, pnl_pips, pnl_usd, outcome, duration_minutes, source,
                         scout_confidence, opened_at, closed_at, user_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual_classified', ?, ?, ?, ?)
                    """, (r['trade_id'], r['setup_name'], r['pair'], r['direction'],
                          r['entry_price'], r['exit_price'], r['units'],
                          r['pips'], r['realized_pl'], r['outcome'], duration_mins,
                          r['confidence'], r['entry_time'], r['exit_time'], user_id))
                
                conn.commit()
                conn.close()
                break
            except sqlite3.OperationalError as e:
                if 'locked' in str(e) and attempt < 4:
                    import time
                    time.sleep(2)
                    continue
                raise
    
    logger.info("Updated setup_trades in v2/trading_forex.db")
    
    # ── Update setup_revenue and check for promotions ──
    _update_revenue_and_promotions(results, user_id)
    
    # ── Summary ──
    print("\n" + "="*70)
    print("CLASSIFICATION SUMMARY")
    print("="*70)
    setup_counts = {}
    for r in results:
        sn = r['setup_name']
        if sn not in setup_counts:
            setup_counts[sn] = {'total': 0, 'wins': 0, 'losses': 0, 'pnl': 0}
        setup_counts[sn]['total'] += 1
        if r['outcome'] == 'win':
            setup_counts[sn]['wins'] += 1
        elif r['outcome'] == 'loss':
            setup_counts[sn]['losses'] += 1
        setup_counts[sn]['pnl'] += r['realized_pl'] or 0
    
    for sn, stats in sorted(setup_counts.items()):
        wr = stats['wins'] / max(stats['total'], 1) * 100
        print(f"  {sn:<15} {stats['total']:>3} trades | {stats['wins']}W {stats['losses']}L | "
              f"WR={wr:.0f}% | PnL=${stats['pnl']:+.2f}")
    print("="*70)


def _update_revenue_and_promotions(results: List[Dict], user_id: int = None):
    """Update setup_revenue table and promote qualifying setups to snipe list."""
    # Resolve user_id from arg or TRADING_USER_ID env (set by serve_ui.py)
    if not user_id:
        _env = os.environ.get("TRADING_USER_ID")
        user_id = int(_env) if _env else None

    # Group closed trades by (setup_name, pair)
    groups = {}
    for r in results:
        if r['outcome'] is None:
            continue
        key = (r['setup_name'], r['pair'])
        groups.setdefault(key, []).append(r)
    
    with sqlite3.connect(TREVOR_DB, timeout=60) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA busy_timeout=60000")
        now = datetime.now(timezone.utc).isoformat()
        
        for (setup_name, pair), trades in groups.items():
            wins = sum(1 for t in trades if t['outcome'] == 'win')
            losses = sum(1 for t in trades if t['outcome'] == 'loss')
            total = wins + losses
            total_pips = sum(t['pips'] or 0 for t in trades)
            total_usd = sum(t['realized_pl'] or 0 for t in trades)
            win_rate = wins / total if total > 0 else 0  # Decimal 0-1 (matches setup_revenue.py)
            best_usd = max((t['realized_pl'] or 0) for t in trades)
            worst_usd = min((t['realized_pl'] or 0) for t in trades)
            
            # Upsert setup_revenue
            existing = conn.execute(
                "SELECT id, total_trades, wins, losses, total_pips, total_usd FROM setup_revenue "
                "WHERE setup_name = ? AND pair = ? AND user_id = ?",
                (setup_name, pair, user_id)
            ).fetchone()

            if existing:
                # Merge with existing
                new_total = existing[1] + total
                new_wins = existing[2] + wins
                new_losses = existing[3] + losses
                new_pips = existing[4] + total_pips
                new_usd = existing[5] + total_usd
                new_wr = new_wins / new_total * 100 if new_total > 0 else 0

                conn.execute("""
                    UPDATE setup_revenue SET total_trades=?, wins=?, losses=?,
                    total_pips=?, total_usd=?, win_rate=?,
                    best_trade_usd=MAX(best_trade_usd, ?),
                    worst_trade_usd=MIN(worst_trade_usd, ?),
                    last_trade_at=? WHERE id=?
                """, (new_total, new_wins, new_losses, new_pips, new_usd, new_wr,
                      best_usd, worst_usd, now, existing[0]))
            else:
                conn.execute("""
                    INSERT INTO setup_revenue
                    (setup_name, pair, total_trades, wins, losses, total_pips, total_usd,
                     best_trade_usd, worst_trade_usd, win_rate, first_trade_at, last_trade_at, user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (setup_name, pair, total, wins, losses, total_pips, total_usd,
                      best_usd, worst_usd, win_rate, now, now, user_id))

            # Check promotion criteria: 3+ wins, ≥70% WR, positive $
            # Re-read the merged totals
            rev = conn.execute(
                "SELECT total_trades, wins, total_usd, win_rate, promoted FROM setup_revenue "
                "WHERE setup_name = ? AND pair = ? AND user_id = ?",
                (setup_name, pair, user_id)
            ).fetchone()

            if rev and rev[1] >= 3 and rev[3] >= 70 and rev[2] > 0 and not rev[4]:
                # Promote!
                conn.execute(
                    "UPDATE setup_revenue SET promoted = 1, promoted_at = ? "
                    "WHERE setup_name = ? AND pair = ? AND user_id = ?",
                    (now, setup_name, pair, user_id)
                )

                # Add to snipe list
                conn.execute("""
                    INSERT OR REPLACE INTO user_snipe_list
                    (setup_name, pair, direction, source, lifetime_pnl_usd,
                     lifetime_trades, lifetime_win_rate, is_active, promoted_at, user_id)
                    VALUES (?, ?, NULL, 'auto_promoted', ?, ?, ?, 1, ?, ?)
                """, (setup_name, pair, rev[2], rev[0], rev[3], now, user_id))
                
                logger.info(f"🎯 PROMOTED: {setup_name} on {pair} → snipe list "
                           f"({rev[1]}W/{rev[0]}T, {rev[3]:.0f}% WR, ${rev[2]:+.2f})")
                
                # Also update live_trades promoted_to_snipe
                _lt_promo = get_trading_forex()
                for t in trades:
                    if t['outcome'] == 'win':
                        _lt_promo.execute(
                            "UPDATE live_trades SET promoted_to_snipe = 1 WHERE id = ? OR oanda_trade_id = ?",
                            (t['trade_id'], t['trade_id'])
                        )
                _lt_promo.commit()
        
        conn.commit()
        logger.info("Updated setup_revenue and checked promotions")


def classify_single_trade_at_close(trade_id: str):
    """Called when a trade closes — classifies it immediately.

    This is the hook for the live flow.
    """
    try:
        _lt_cls = get_trading_forex()
        _lt_cls.row_factory = sqlite3.Row
        row = _lt_cls.execute(
            "SELECT *, id as trade_id FROM live_trades WHERE id = ? OR oanda_trade_id = ?",
            (trade_id, trade_id)
        ).fetchone()

        if not row:
            logger.debug(f"Trade {trade_id} not found in live_trades")
            return

        trade = dict(row)
        if trade.get('result') is None:
            logger.debug(f"Trade {trade_id} not yet closed")
            return

        classify_and_update(specific_trade=trade_id)
        logger.info(f"✅ Classified trade {trade_id} at close")
    except Exception as e:
        logger.error(f"Failed to classify trade {trade_id}: {e}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Classify manual trades')
    parser.add_argument('--all', action='store_true', help='Re-classify all trades')
    parser.add_argument('--trade', type=str, help='Classify specific trade ID')
    parser.add_argument('--user-id', type=int, default=None,
                        help='User ID to classify for (default: TRADING_USER_ID env or 2)')
    args = parser.parse_args()

    classify_and_update(reclassify_all=args.all, specific_trade=args.trade, user_id=args.user_id)
