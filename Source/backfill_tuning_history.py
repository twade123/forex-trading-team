"""
Backfill tuning_overrides from tuning_log.md history.

Run once to populate the dashboard with all historical tuning changes
so the performance endpoint can measure before/after impact for every change.

Usage:
    source ~/myenv/bin/activate && python backfill_tuning_history.py
"""

import json
import os
import sqlite3
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
_TRADING_BOT_DIR = os.path.dirname(_DIR)
_JARVIS_ROOT = os.path.dirname(_TRADING_BOT_DIR)
_DB = os.path.join(_JARVIS_ROOT, "Database", "v2", "trading_forex.db")


def _ensure_columns(conn):
    """Add batch_label and change_type columns if missing."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tuning_overrides)").fetchall()}
    if "batch_label" not in cols:
        conn.execute("ALTER TABLE tuning_overrides ADD COLUMN batch_label TEXT DEFAULT ''")
        print("  + Added batch_label column")
    if "change_type" not in cols:
        conn.execute("ALTER TABLE tuning_overrides ADD COLUMN change_type TEXT DEFAULT 'param_change'")
        print("  + Added change_type column")
    conn.commit()


# All historical tuning changes extracted from tuning_log.md
# Each entry: (date, param, value, prev_value, reason, batch_label, change_type, active)
HISTORY = [
    # ── 2026-03-24 ──────────────────────────────────────────────────
    ("2026-03-24T12:00:00", "watch.trigger_threshold", "0.80", "0.90",
     "90% condition match too strict. Watches seeing valid setups but requiring near-perfect alignment, causing missed entries.",
     "", "param_change", 0),  # active=0 because later changed again

    # ── 2026-03-26 ──────────────────────────────────────────────────
    ("2026-03-26T10:00:00", "watch.ema_velocity_threshold", "0.003", "0.005",
     "EMA velocity typically sits 0.002-0.004. Old 0.005 threshold meant velocity condition almost never passed. Watches scanning hundreds of cycles without triggering.",
     "2026-03-26 Gate Additions", "param_change", 1),

    ("2026-03-26T10:00:00", "gate.bb_width_min_pips_m1", "6", "0",
     "NEW GATE: Prevent entries in tight BB squeezes with no room. 6 pips M1 filters tightest squeezes while allowing normal volatility.",
     "2026-03-26 Gate Additions", "new_gate", 1),

    ("2026-03-26T10:00:00", "gate.per_pair_cooldown_hours", "2", "0",
     "NEW GATE: 2-hour cooldown after loss on same pair. Prevents revenge trading and rapid-fire entries.",
     "2026-03-26 Gate Additions", "new_gate", 1),

    ("2026-03-26T10:00:00", "gate.per_pair_daily_max", "3", "0",
     "NEW GATE: Max 3 trades per day per pair. Prevents overtrading single instrument.",
     "2026-03-26 Gate Additions", "new_gate", 1),

    # ── 2026-03-27 ──────────────────────────────────────────────────
    ("2026-03-27T10:00:00", "watch.trigger_threshold", "0.70", "0.80",
     "Dropped to 70% — watches still triggering too late at 80%. EUR_JPY valid trade missed, AUD_USD never triggered. REVERTED same day — all 5 trades at 70% entered during retrace into E100.",
     "2026-03-27 Threshold Experiment", "param_change", 0),  # reverted

    ("2026-03-27T15:00:00", "watch.trigger_threshold", "0.80", "0.70",
     "REVERT: All 5 trades at 70% entered during retrace into E100. GBP_JPY #2375 -30.7p, GBP_USD #2385 -26.1p, NZD_USD #2395 -15.6p. Root cause: 70% lets snipes fire when 'setup exists' but 'entry timing' conditions not required.",
     "2026-03-27 Threshold Revert", "revert", 1),

    ("2026-03-27T14:00:00", "validator.thesis_framing", "enabled", "disabled",
     "Automated cycles now frame data as thesis for validator (like Tim's chart submissions) instead of raw numbers. Story score 0-100 computed from EMA alignment, velocity, oscillator zone, BB expansion.",
     "2026-03-27 Validator + Guardian", "new_feature", 1),

    ("2026-03-27T14:00:00", "guardian.rule6_bad_entry_exit", "enabled", "disabled",
     "NEW RULE: Closes when peak < 3p, current < -10p, 10+ candles, E100 tested. Catches bad entries at ~-10p instead of full SL (-25 to -30p). Saves ~15-20 pips per bad entry. Added after retrace audit found GBP_JPY #2375 (-30.7p, never positive).",
     "2026-03-27 Validator + Guardian", "new_rule", 1),

    # ── 2026-03-29 — Post-Audit Overhaul ───────────────────────────
    ("2026-03-29T10:00:00", "fix.cooldown_timestamp_nanosecond", "truncate_to_6", "raw_9_decimal",
     "BUG FIX: Python fromisoformat() fails on OANDA 9-decimal timestamps, causing per-pair 2h cooldown to fail-open silently.",
     "2026-03-29 Post-Audit Overhaul", "bug_fix", 1),

    ("2026-03-29T10:00:00", "gate.oscillator_exhaustion_rsi_sell", "30", "none",
     "NEW GATE: Block SELL if RSI < 30. March 27 losses entered at oscillator extremes where move was exhausted.",
     "2026-03-29 Post-Audit Overhaul", "new_gate", 1),

    ("2026-03-29T10:00:00", "gate.oscillator_exhaustion_stoch_sell", "10", "none",
     "NEW GATE: Block SELL if Stoch < 10. Belt-and-suspenders with RSI gate.",
     "2026-03-29 Post-Audit Overhaul", "new_gate", 1),

    ("2026-03-29T10:00:00", "gate.oscillator_freshness", "enabled", "disabled",
     "NEW GATE: Block if stoch crossed through reversal zone (was >70 now <50 for SELL). Catches clearly stale signals where sell reversal window has passed.",
     "2026-03-29 Post-Audit Overhaul", "new_gate", 1),

    ("2026-03-29T10:00:00", "gate.candle_vs_ema21_max_atr", "1.5", "none",
     "NEW GATE: Block if price >1.5 ATR from EMA 21. Teaching charts show cascade starts at 2nd/3rd candle AFTER cross — far from E21 means entry is too late.",
     "2026-03-29 Post-Audit Overhaul", "new_gate", 1),

    ("2026-03-29T10:00:00", "gate.story_score_min", "50", "0",
     "NEW GATE: Block if story_score < 50 before validator call. Saves LLM cost on weak setups. Score = EMA alignment 30pts + fan velocity 25pts + oscillator zone 20pts + BB expanding 25pts.",
     "2026-03-29 Post-Audit Overhaul", "new_gate", 1),

    ("2026-03-29T10:00:00", "fix.fromisoformat_7_locations", "safe_isoformat", "raw_fromisoformat",
     "BUG FIX: All 7 datetime.fromisoformat() calls in watch_manager.py replaced with _safe_isoformat() helper for nanosecond timestamps. Affected watch expiry, staleness scoring, scout-snipe timing.",
     "2026-03-29 Post-Audit Overhaul", "bug_fix", 1),

    ("2026-03-29T10:00:00", "watch.48h_stale_rule", "removed", "enabled",
     "REMOVED: 48h stale watch expiry mass-expired all 25 watches on Sunday (markets close Friday 5pm). Direction Sanity Gate already validates market state before firing — time-based expiry unnecessary and dangerous.",
     "2026-03-29 Post-Audit Overhaul", "removal", 1),

    ("2026-03-29T10:00:00", "watch.core_bonus_criteria", "core_all+bonus_50pct", "flat_80pct",
     "Replaced flat 80% threshold with two-pass: CORE conditions (BB, candle position, EMA crosses, retracement zones) ALL must pass. BONUS conditions (fan, ADX, RSI, stoch) ≥50% must pass. Validated against 3 real watches from DB.",
     "2026-03-29 Post-Audit Overhaul", "new_feature", 1),

    # ── 2026-03-29 — Session 2: Chart Study + Scout ────────────────
    ("2026-03-29T16:00:00", "validator.phase_pattern_rules", "5_phases_codified", "generic",
     "Added CASCADE START, PEAK, RETRACEMENT, RE-ENTRY, REGIME CHANGE behavioral rules from studying all 20 teaching charts. Validator now evaluates against recognizable phase patterns.",
     "2026-03-29 Chart Study + Scout Audit", "new_feature", 1),

    ("2026-03-29T16:00:00", "scout.stoch_cross_detection", "enabled", "disabled",
     "NEW: %K/%D crossover detection. Bull cross in oversold (<35), bear cross in overbought (>65). Single strongest signal across all 20 teaching charts.",
     "2026-03-29 Chart Study + Scout Audit", "new_feature", 1),

    ("2026-03-29T16:00:00", "scout.rsi_divergence", "wired", "computed_but_unused",
     "RSI divergence was already computed by TA but scout never used it. Now wired into retracement logic.",
     "2026-03-29 Chart Study + Scout Audit", "bug_fix", 1),

    ("2026-03-29T16:00:00", "scout.reversal_candle_at_ema", "enabled", "disabled",
     "NEW: Detects hammer/pin bar shapes (small body <45%, long wick >1.5x body) within 5 pips of E55/E100. Every teaching chart showed this as the entry signal.",
     "2026-03-29 Chart Study + Scout Audit", "new_feature", 1),

    ("2026-03-29T16:00:00", "scout.consolidation_filter_fix", "exempt_ordered_fans", "blocks_all_low_adx",
     "BUG FIX: Ordered fans no longer blocked by consolidation filter when ADX<20 and BB tight. Retracement phases naturally have low ADX but fan is still ordered.",
     "2026-03-29 Chart Study + Scout Audit", "bug_fix", 1),

    ("2026-03-29T16:00:00", "scout.path_a_retracement_forming", "enabled", "disabled",
     "NEW: Detection fires BEFORE BB re-expansion confirmed. Requires fan ordered + price at E55/E100 + ≥1 signal (stoch cross, RSI div, reversal candle). Priority 5a RETRACEMENT FORMING.",
     "2026-03-29 Chart Study + Scout Audit", "new_feature", 1),

    ("2026-03-29T16:00:00", "scout.dead_move_chop_exemptions", "exempt_retracement", "blocks_all",
     "Dead move and chop filters now exempt retracement forming setups. Indicators naturally look subdued during pullbacks.",
     "2026-03-29 Chart Study + Scout Audit", "bug_fix", 1),

    # ── 2026-03-30 ──────────────────────────────────────────────────
    ("2026-03-30T12:00:00", "gate.oscillator_direction_stoch_sell", "45", "35",
     "Widened from <35 to <45. Trade #2795 AUD_USD SELL slipped through at stoch=53.4 — gate only caught early phase of recovery. Mid-range (35-45) is most dangerous for bounce entries. Saved -$16.50.",
     "2026-03-30 Oscillator Updates", "param_change", 1),

    ("2026-03-30T12:00:00", "gate.oscillator_direction_stoch_buy", "55", "65",
     "Widened from >65 to >55 (symmetrical with SELL change).",
     "2026-03-30 Oscillator Updates", "param_change", 1),

    ("2026-03-30T12:00:00", "gate.stoch_velocity_bounce", "25", "none",
     "NEW GATE: Block when stoch jumps >25pts in one bar from extreme zone. SELL blocked if prev<25 AND jump>25. Trade #2795: prev=6.1, velocity=47.3 — would have been caught. A 25pt stoch jump from oversold is almost always a bounce.",
     "2026-03-30 Oscillator Updates", "new_gate", 1),

    # ── 2026-03-31 ──────────────────────────────────────────────────
    ("2026-03-31T12:00:00", "gate.sniper_revalidation", "enabled", "disabled",
     "NEW GATE: Re-checks live sniper score when watch triggers. If score dropped below 12, blocks entry. Audit found scores of 6-8 on entries that should have been blocked — original score valid but never re-checked.",
     "2026-03-31 Revalidation + Fix", "new_gate", 1),

    ("2026-03-31T12:00:00", "fix.story_score_persistence", "opportunity_score_key", "story_score_key",
     "BUG FIX: Two-part key mismatch. trading_api_routes.py never forwarded opportunity_score to snipe_ctx. trading_cycle.py used wrong key. Story score was logging as 0 on all snipe-direct trades since ~March 27.",
     "2026-03-31 Revalidation + Fix", "bug_fix", 1),
]


def backfill():
    conn = sqlite3.connect(_DB, timeout=10)
    _ensure_columns(conn)

    # Check which params already exist to avoid duplicates
    existing = set()
    for r in conn.execute("SELECT param, created_at FROM tuning_overrides").fetchall():
        existing.add((r[0], r[1][:10] if r[1] else ""))

    inserted = 0
    skipped = 0
    for ts, param, value, prev, reason, batch, ctype, active in HISTORY:
        date_key = ts[:10]
        if (param, date_key) in existing:
            skipped += 1
            continue

        conn.execute(
            """INSERT INTO tuning_overrides
               (param, value, previous_value, reason, backtest_result,
                approved_by, approved_at, active, created_at, batch_label, change_type)
               VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
            (param, value, prev, reason,
             "Tim (post-mortem)", ts, active, ts, batch, ctype),
        )
        inserted += 1
        print(f"  + {ts[:10]} {param}: {prev} → {value}")

    # Also tag the existing 2026-04-01 records with batch_label
    conn.execute("""
        UPDATE tuning_overrides
        SET batch_label = '2026-04-01 SL Widening',
            change_type = 'param_change'
        WHERE created_at LIKE '2026-04-01%'
          AND (batch_label IS NULL OR batch_label = '')
    """)

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM tuning_overrides").fetchone()[0]
    conn.close()
    print(f"\nDone: {inserted} inserted, {skipped} skipped (already exist), {total} total records")


if __name__ == "__main__":
    print(f"Backfilling tuning history to {_DB} ...")
    backfill()
    print("Complete.")
