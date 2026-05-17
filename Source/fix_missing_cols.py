"""Fix columns lost during table rebuild and re-run backfill."""
import sys, sqlite3, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_pool import get_trading_forex

conn = get_trading_forex()

# Get current columns
current = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)").fetchall()}
print(f"Current columns: {len(current)}")

# All columns that SHOULD exist (from both original schema + ALTERs + manual_trades merge)
REQUIRED_COLUMNS = {
    # Original live_trades columns that may have been lost in rebuild
    'result': 'TEXT',
    'pips': 'REAL',
    'combined_pips': 'REAL',
    'risk_reward_actual': 'REAL',
    'h4_trend': 'TEXT',
    'h4_agrees': 'TEXT',
    'h4_info': 'TEXT',
    'sl_moved_to_be': 'TEXT',
    'be_candle': 'REAL',
    'partial_exit_hit': 'TEXT',
    'partial_exit_pips': 'REAL',
    'second_half_result': 'TEXT',
    'second_half_pips': 'REAL',
    'nearest_daily_pivot': 'TEXT',
    'dist_to_daily_pivot_atr': 'REAL',
    'near_daily_resistance': 'TEXT',
    'near_daily_support': 'TEXT',
    'loss_streak_at_entry': 'INTEGER DEFAULT 0',
    'max_loss_streak': 'INTEGER DEFAULT 0',
    'adx': 'REAL',
    'adx_slope': 'REAL',
    'rsi': 'REAL',
    'macd_value': 'REAL',
    'macd_signal': 'REAL',
    'macd_hist': 'REAL',
    'stoch_k': 'REAL',
    'stoch_d': 'REAL',
    'cci': 'REAL',
    'bb_upper': 'REAL',
    'bb_mid': 'REAL',
    'bb_lower': 'REAL',
    'bb_width': 'REAL',
    'bb_expanding': 'INTEGER',
    'sma50': 'REAL',
    'sma100': 'REAL',
    'atr': 'REAL',
    'sar': 'REAL',
    'price_vs_sma50': 'TEXT',
    'price_vs_sma100': 'TEXT',
    'entry_candle_pattern': 'TEXT',
    'prev_3_candle_patterns': 'TEXT',
    'nearest_support': 'REAL',
    'nearest_resistance': 'REAL',
    'pivot_pp': 'REAL',
    'pivot_r1': 'REAL',
    'pivot_s1': 'REAL',
    'max_favorable_pips': 'REAL',
    'max_adverse_pips': 'REAL',
    'candles_to_exit': 'INTEGER',
    'trigger_reason': 'TEXT',
    'confidence': 'REAL',
    'concurrent_setups': 'TEXT',
    'concurrent_directions': 'TEXT',
    # Columns added via ALTER TABLE in previous sessions
    'setup_code': 'TEXT',
    'regime': 'TEXT',
    'metadata': "TEXT DEFAULT '{}'",
    'source': "TEXT DEFAULT 'paper'",
    'account_id': 'TEXT',
    'oanda_trade_id': 'TEXT',
    'decision_id': 'TEXT',
    'timeframe': 'TEXT',
    'setup': 'TEXT',
    'base_setup': 'TEXT',
    'rr_mult': 'REAL',
    'sl_mult': 'REAL',
    'session': 'TEXT',
    'spread_at_entry': 'REAL',
    'outcome': 'TEXT',
    'outcome_pips': 'REAL',
    'outcome_r': 'REAL',
    'outcome_usd': 'REAL',
    'partial_close_price': 'REAL',
    'partial_close_pips': 'REAL',
    'max_favorable_excursion_pips': 'REAL',
    'max_adverse_excursion_pips': 'REAL',
    'confluence_score': 'REAL',
    'risk_profile': 'TEXT',
    'position_size': 'REAL',
    'risk_amount_usd': 'REAL',
    'exit_trigger': 'TEXT',
    'exit_method': 'TEXT',
    'user_id': 'INTEGER DEFAULT 2',
    'finding_id': 'TEXT',
    'entry_type': 'TEXT',
    'validator_verdict': 'TEXT',
    'validator_confidence': 'REAL',
    'cycle_id': 'TEXT',
    # manual_trades merge columns
    'market_picture': 'JSON',
    'market_story': 'JSON',
    'sniper_scores': 'JSON',
    'candle_structure': 'JSON',
    'indicators': 'JSON',
    'exit_market_picture': 'JSON',
    'fan_state': 'TEXT',
    'fan_direction': 'TEXT',
    'fan_ordered': 'INTEGER',
    'e100_role': 'TEXT',
    'fan_width_pct': 'REAL',
    'momentum_state': 'TEXT',
    'trend_health': 'REAL',
    'story_score': 'REAL',
    'story_entry_type': 'TEXT',
    'dual_cross_cascade': 'INTEGER DEFAULT 0',
    'cascade_direction': 'TEXT',
    'retracement_type': 'TEXT',
    'bb_re_expanding': 'INTEGER DEFAULT 0',
    'tested_e55': 'INTEGER DEFAULT 0',
    'tested_e100': 'INTEGER DEFAULT 0',
    'entry_setup_type': 'TEXT',
    'pattern_fingerprint': 'TEXT',
    'promoted_to_snipe': 'INTEGER DEFAULT 0',
    'classified_setup': 'TEXT',
    'units': 'INTEGER',
    'hold_bars': 'INTEGER',
    'realized_pl': 'REAL',
}

added = 0
for col, col_type in sorted(REQUIRED_COLUMNS.items()):
    if col not in current:
        try:
            conn.execute(f"ALTER TABLE live_trades ADD COLUMN {col} {col_type}")
            print(f"  + {col} ({col_type})")
            added += 1
        except sqlite3.OperationalError as e:
            if 'duplicate' not in str(e).lower():
                print(f"  ERROR {col}: {e}")

conn.commit()
print(f"\nAdded {added} missing columns")

# Verify
final = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)").fetchall()}
print(f"Final column count: {len(final)}")

# Now re-run the backfill UPDATE for existing trades
print("\n── Re-running backfill UPDATEs ──")
mt_rows = conn.execute("SELECT * FROM manual_trades ORDER BY created_at").fetchall()
mt_cols_list = [desc[0] for desc in conn.execute("SELECT * FROM manual_trades LIMIT 0").description]
valid_cols = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)").fetchall()}

updated = 0
for mt in mt_rows:
    mt_dict = dict(zip(mt_cols_list, mt))
    tid = str(mt_dict.get('trade_id', ''))
    if not tid:
        continue

    update_fields = {}
    for col in ['market_picture', 'market_story', 'sniper_scores',
                'candle_structure', 'indicators', 'exit_market_picture',
                'fan_state', 'fan_direction', 'fan_ordered', 'e100_role',
                'fan_width_pct', 'bb_expanding', 'momentum_state',
                'trend_health', 'story_score', 'story_entry_type',
                'dual_cross_cascade', 'cascade_direction', 'retracement_type',
                'bb_re_expanding', 'tested_e55', 'tested_e100',
                'entry_setup_type', 'pattern_fingerprint', 'promoted_to_snipe',
                'classified_setup', 'units', 'hold_bars', 'realized_pl',
                'rsi', 'stoch_k']:
        val = mt_dict.get(col)
        if val is not None and col in valid_cols:
            update_fields[col] = val

    # Map result/pips/exit data
    if mt_dict.get('result') and mt_dict['result'] in ('win', 'loss'):
        update_fields['result'] = mt_dict['result']
        update_fields['outcome'] = mt_dict['result']
        for fld in ['pips', 'exit_price', 'exit_time', 'exit_reason',
                     'mfe_pips', 'mae_pips']:
            if mt_dict.get(fld) is not None and fld in valid_cols:
                update_fields[fld] = mt_dict[fld]
        if mt_dict.get('realized_pl') is not None:
            update_fields['realized_pl'] = mt_dict['realized_pl']
            update_fields['pnl_usd'] = mt_dict['realized_pl']
        if mt_dict.get('pips') is not None:
            update_fields['outcome_pips'] = mt_dict['pips']

    if update_fields:
        set_clause = ", ".join(f'"{k}" = ?' for k in update_fields.keys())
        vals = list(update_fields.values()) + [tid, tid]
        try:
            conn.execute(
                f'UPDATE live_trades SET {set_clause} WHERE id = ? OR oanda_trade_id = ?',
                vals
            )
            updated += 1
        except Exception as e:
            print(f"  UPDATE {tid} failed: {e}")

conn.commit()
print(f"Updated {updated} trades with rich data")

# Final verification
lt_total = conn.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0]
lt_with_fp = conn.execute("SELECT COUNT(*) FROM live_trades WHERE pattern_fingerprint IS NOT NULL").fetchone()[0]
lt_with_mp = conn.execute("SELECT COUNT(*) FROM live_trades WHERE market_picture IS NOT NULL").fetchone()[0]
lt_with_outcome = conn.execute("SELECT COUNT(*) FROM live_trades WHERE outcome IS NOT NULL AND outcome != ''").fetchone()[0]
lt_with_result = conn.execute("SELECT COUNT(*) FROM live_trades WHERE result IS NOT NULL AND result != ''").fetchone()[0]

print(f"\n── Final State ──")
print(f"  live_trades total: {lt_total}")
print(f"  with pattern_fingerprint: {lt_with_fp}")
print(f"  with market_picture: {lt_with_mp}")
print(f"  with outcome: {lt_with_outcome}")
print(f"  with result: {lt_with_result}")

dirs = conn.execute("SELECT direction, COUNT(*) FROM live_trades GROUP BY direction").fetchall()
print(f"  Directions: {dict(dirs)}")

# Sample a fully unified row
sample = conn.execute("""
    SELECT id, pair, direction, outcome, pnl_pips, fan_state, pattern_fingerprint,
           rsi, bb_expanding, trend_health, story_score, market_picture IS NOT NULL as has_mp
    FROM live_trades
    WHERE pattern_fingerprint IS NOT NULL AND outcome IS NOT NULL
    LIMIT 3
""").fetchall()
print(f"\n  Sample unified rows:")
for s in sample:
    print(f"    #{s[0]} {s[1]} {s[2]} | {s[3]} {s[4] or 0:+.1f}p | fan={s[5]} rsi={s[6]} bb_exp={s[7]} health={s[8]} story={s[9]} mp={s[10]}")

conn.close()
