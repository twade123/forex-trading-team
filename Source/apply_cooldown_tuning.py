#!/usr/bin/env python3
"""One-shot script to insert tuning_overrides for the 2026-04-02 cooldown reduction.
Run once after server restart, then delete this file.

Usage:
    source ~/myenv/bin/activate && python apply_cooldown_tuning.py
"""
import sqlite3, json, os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'Database', 'v2', 'trading_forex.db')
DB_PATH = os.path.abspath(DB_PATH)

def main():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('PRAGMA busy_timeout = 10000')
    now = datetime.now(timezone.utc).isoformat()
    batch = '2026-04-02 Pair Cooldown Reduction + Dynamic SL Fixes'

    # Check if already applied
    existing = conn.execute(
        "SELECT COUNT(*) FROM tuning_overrides WHERE batch_label = ?", (batch,)
    ).fetchone()[0]
    if existing > 0:
        print(f"Already applied ({existing} records with batch '{batch}'). Skipping.")
        conn.close()
        return

    records = [
        {
            'param': 'snipe_direct.pair_cooldown_hours',
            'value': '0.5 (30 minutes)',
            'previous_value': '2.0 (2 hours)',
            'reason': (
                'EUR_AUD snipe was blocked for 2h after trade #4305 loss — a loss caused by '
                'Dynamic SL bug, not bad analysis. 2h cooldown prevented the system from '
                're-entering a valid setup. 30min still prevents rapid revenge-trading '
                '(S16 churning pattern) but does not lock out pairs for half a session. '
                'Daily per-pair limit already removed on 2026-04-01.'
            ),
            'backtest_result': json.dumps({
                'affected_trades': ['EUR_AUD watch #1790 blocked 6+ consecutive cycles over 15min'],
                'original_rationale': 'S16 churned EUR_USD 9x in one day (19-39min gaps, 4W/5L = -26.8p)',
                'new_rationale': '30min still blocks sub-30min re-entry, graduated cooldown may be added later'
            }),
            'change_type': 'bug_fix'
        },
        {
            'param': 'dynamic_sl.ema_convergence_check',
            'value': 'E55 anchor + 10-15p buffer when EMA gap < 0.15%',
            'previous_value': 'Always E100 anchor + 8p buffer',
            'reason': (
                'During retrace E55 and E100 converge (<0.15% gap). Anchoring SL to E100 '
                'with 8p buffer placed the stop within 1-2p of entry, choking trades. '
                'EUR_AUD #4293 SL moved from 39p to 1.7p from entry. Now uses E55 with '
                'wider buffer when EMAs converged, plus 50% minimum distance safeguard '
                'from original SL, plus pnl_pips > 0 guard.'
            ),
            'backtest_result': json.dumps({
                'affected_trades': ['EUR_AUD #4293 (-$36, SL choked)', 'EUR_AUD #4305 (-$122, SL tightened while losing)'],
                'three_fixes': [
                    'EMA convergence check on anchor selection',
                    '50% minimum distance from original SL',
                    'Current PnL must be positive to tighten'
                ]
            }),
            'change_type': 'bug_fix'
        },
        {
            'param': 'profit_protection.price_variable_fix',
            'value': 'current_price (function parameter)',
            'previous_value': 'price (undefined — NameError every tick)',
            'reason': (
                '_check_profit_protection() receives current_price as parameter but '
                'ratchet validation used bare "price". Crashed every tick for all trades. '
                'Ratchet activated correctly but floor SL was never moved to OANDA. '
                'Every trade with peak >= 3p had zero profit floor protection.'
            ),
            'backtest_result': json.dumps({
                'bug': 'name "price" is not defined — NameError on every guardian tick',
                'impact': 'Ratchet profit floor completely non-functional for ALL trades',
                'fix': 'price → current_price on lines 1901-1902'
            }),
            'change_type': 'bug_fix'
        },
    ]

    for rec in records:
        conn.execute("""
            INSERT INTO tuning_overrides
            (param, value, previous_value, reason, backtest_result, change_type, batch_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec['param'], rec['value'], rec['previous_value'],
            rec['reason'], rec['backtest_result'], rec['change_type'],
            batch, now
        ))

    conn.commit()
    print(f"Inserted {len(records)} tuning_overrides records (batch: {batch})")
    conn.close()

if __name__ == '__main__':
    main()
