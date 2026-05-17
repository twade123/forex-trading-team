#!/usr/bin/env python3
"""One-shot script to insert tuning_overrides for the 2026-04-02 retrace fix.
Run once after server restart, then delete this file.

Usage:
    source ~/myenv/bin/activate && python apply_retrace_tuning.py
"""
import sqlite3, json, os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'Database', 'v2', 'trading_forex.db')
DB_PATH = os.path.abspath(DB_PATH)

def main():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('PRAGMA busy_timeout = 10000')
    now = datetime.now(timezone.utc).isoformat()
    batch = '2026-04-02 Retrace Awareness + M15 EMA Fix'

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
            'param': 'score_threat.ema_source',
            'value': 'M15 (broadcast to M1 alignment)',
            'previous_value': 'M1 (EMA100 ≈ 100min ≈ M15 EMA7)',
            'reason': (
                'candle_structure was using M1 EMAs for E100 interaction detection. '
                'M1 EMA100 is only ~100min of data, equivalent to ~EMA7 on M15. '
                'AUD/USD #4271 scored "E100 BROKEN" against this meaningless M1 level. '
                'EUR/USD scored BLACK(90) while dashboard showed TREND RESUMING. '
                'Now computes M15 EMA21/55/100 and broadcasts to M1 candle alignment. '
                'BB also upgraded to M15.'
            ),
            'backtest_result': json.dumps({
                'affected_trades': ['AUD/USD #4271 (-$166)', 'EUR/USD BLACK(90) contradicting TREND RESUMING'],
                'bug': 'M1 EMA100 ≈ M15 EMA7 — completely different structural level',
                'fix': 'candle_structure + BB now use M15 EMAs/BB, M1 fallback if insufficient data'
            }),
            'change_type': 'bug_fix'
        },
        {
            'param': 'score_threat.retrace_awareness',
            'value': 'enabled (proximity discount + candle-E55 scoring + fan collapse suppression)',
            'previous_value': 'none (score_threat had zero retrace context)',
            'reason': (
                'score_threat() received no retrace_state from TradeWatcher. During retrace, '
                'natural EMA compression caused false E100 proximity/broken signals. '
                'Now passes retrace_state, retrace_depth, e100_tests_in_retrace, peak_fan_width, '
                'reexpansion_count. Discounts proximity 20-80% when price at E55, suppresses fan '
                'collapse during retrace, evaluates candle conviction at E55 as primary retrace signal.'
            ),
            'backtest_result': json.dumps({
                'affected_signals': [
                    'proximity_risk: 90→18 (80% discount when EMAs within 0.05%)',
                    'fan_collapse: +20→0 (suppressed during retrace)',
                    'E100_broken: 40→10 (small bodies = retrace noise)',
                    'proximity_add: halved during retrace',
                    'NEW: candle-E55 bounce detection reduces structure threat by 10'
                ],
                'net_effect': 'BLACK(90-100) → YELLOW(35-45) during retrace',
                'retrace_model': 'E21(safe) → E55(midway/healthy) → E100(oh-shit territory)'
            }),
            'change_type': 'bug_fix'
        },
        {
            'param': 'build_market_state.bb_source',
            'value': 'M15 (when buffer >= 30 candles)',
            'previous_value': 'M1',
            'reason': (
                'M1 Bollinger Bands are micro-noise. M15 BB shows the actual volatility '
                'envelope visible on the trading chart. Retrace depth calculations using M1 BB '
                'width measured meaningless micro-contraction.'
            ),
            'backtest_result': json.dumps({
                'fix': 'BB upper/lower computed from M15 candles, M1 fallback if M15 < 30 bars'
            }),
            'change_type': 'bug_fix'
        }
    ]

    for rec in records:
        conn.execute('''
            INSERT INTO tuning_overrides
                (param, value, previous_value, reason, backtest_result,
                 approved_by, approved_at, active, batch_label, change_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ''', (
            rec['param'], rec['value'], rec['previous_value'], rec['reason'],
            rec['backtest_result'], 'Tim (user)', now, batch, rec['change_type']
        ))

    conn.commit()
    print(f"✓ Inserted {len(records)} tuning records (batch: '{batch}')")

    # Verify
    rows = conn.execute(
        'SELECT id, param, change_type FROM tuning_overrides WHERE batch_label = ? ORDER BY id',
        (batch,)
    ).fetchall()
    for r in rows:
        print(f"  id={r[0]}: {r[1]} ({r[2]})")

    conn.close()


if __name__ == '__main__':
    main()
