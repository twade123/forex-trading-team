"""
Tuning Config — centralized thresholds for scout, guardian, and story.

The trade auditor proposes changes here. Changes go through:
  1. Auditor generates recommendation
  2. Backtest proposed change against historical data
  3. Present before/after comparison to Tim for approval
  4. If approved, apply and log the change

All tunable parameters are in TUNING dict. Each has:
  - value: current active value
  - default: factory default (for reset)
  - min/max: valid range
  - description: what it does
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex

logger = logging.getLogger("trading_bot.tuning")

# ── Master Tuning Parameters ──

TUNING = {
    # Scout / Market Story thresholds
    "story.thesis_threshold": {
        "value": 40, "default": 40, "min": 20, "max": 70,
        "description": "Minimum opportunity score (0-100) to generate a story alert",
        "module": "market_story.py",
    },
    "story.momentum.rsi_oversold": {
        "value": 30, "default": 30, "min": 15, "max": 40,
        "description": "RSI level below which momentum is considered oversold",
        "module": "market_story.py",
    },
    "story.momentum.rsi_overbought": {
        "value": 70, "default": 70, "min": 60, "max": 85,
        "description": "RSI level above which momentum is considered overbought",
        "module": "market_story.py",
    },
    "story.momentum.stoch_extreme_low": {
        "value": 20, "default": 20, "min": 10, "max": 35,
        "description": "Stochastic level for oversold extreme",
        "module": "market_story.py",
    },
    "story.momentum.stoch_extreme_high": {
        "value": 80, "default": 80, "min": 65, "max": 90,
        "description": "Stochastic level for overbought extreme",
        "module": "market_story.py",
    },

    # Guardian threat layer weights (max points per layer)
    "guardian.layer1_trend_max": {
        "value": 50, "default": 50, "min": 30, "max": 60,
        "description": "Max threat points from Layer 1 (trend structure)",
        "module": "position_guardian.py",
    },
    "guardian.layer2_structure_max": {
        "value": 40, "default": 40, "min": 20, "max": 50,
        "description": "Max threat points from Layer 2 (price structure)",
        "module": "position_guardian.py",
    },
    "guardian.layer3_momentum_max": {
        "value": 15, "default": 15, "min": 5, "max": 25,
        "description": "Max threat points from Layer 3 (momentum)",
        "module": "position_guardian.py",
    },

    # Scout confidence thresholds
    "scout.min_confidence": {
        "value": 0.55, "default": 0.55, "min": 0.3, "max": 0.85,
        "description": "Minimum scout confidence to queue a cycle",
        "module": "trade_scout.py",
    },
    "scout.session_boost_max": {
        "value": 0.10, "default": 0.10, "min": 0.0, "max": 0.20,
        "description": "Max confidence boost from active trading session",
        "module": "trade_scout.py",
    },

    # Pair-level overrides (thesis restrictions)
    "pairs.restricted_thesis": {
        "value": {},  # e.g. {"GBP_USD": ["breakout"], "USD_JPY": ["counter_trend_reversal"]}
        "default": {},
        "min": None, "max": None,
        "description": "Thesis types paused per pair (from audit recommendations)",
        "module": "trade_scout.py",
    },

    # ── Guardian threat zone boundaries ──
    "guardian.zone_yellow": {"value": 31, "default": 31, "min": 20, "max": 45, "description": "Threat score boundary: GREEN to YELLOW. Above this, guardian tightens management.", "module": "position_guardian.py", "tier": 1},
    "guardian.zone_red": {"value": 61, "default": 61, "min": 45, "max": 75, "description": "Threat score boundary: YELLOW to RED. Above this, guardian actively protects profit.", "module": "position_guardian.py", "tier": 1},
    "guardian.zone_black": {"value": 81, "default": 81, "min": 70, "max": 95, "description": "Threat score boundary: RED to BLACK. Above this, guardian auto-closes trade.", "module": "position_guardian.py", "tier": 1},

    # ── Guardian profit floor locks ──
    "guardian.profit_floor_20p": {"value": 0.70, "default": 0.70, "min": 0.50, "max": 0.98, "description": "Lock ratio when peak profit >= 20 pips (lock 70%)", "module": "position_guardian.py", "tier": 1},
    "guardian.profit_floor_12p": {"value": 0.60, "default": 0.60, "min": 0.40, "max": 0.95, "description": "Lock ratio when peak profit >= 12 pips", "module": "position_guardian.py", "tier": 1},
    "guardian.profit_floor_8p": {"value": 0.50, "default": 0.50, "min": 0.30, "max": 0.90, "description": "Lock ratio when peak profit >= 8 pips", "module": "position_guardian.py", "tier": 1},
    "guardian.profit_floor_5p": {"value": 0.30, "default": 0.30, "min": 0.15, "max": 0.80, "description": "Lock ratio when peak profit >= 5 pips (ratchet threshold)", "module": "position_guardian.py", "tier": 1},

    # ── Early adverse-excursion cut (2026-04-30) ──
    # Catches fast-failing trades within first ~60 minutes.
    # 90d × 14-pair × 8-fold walk-forward: STABLE (7/8 folds positive,
    # mean +14p/fold, 83.5% precision, 7.2% winkill).
    # See scripts/loss_signature_finder.py for full validation.
    "guardian.adv_cut_enabled": {"value": True, "default": True, "description": "Enable early adverse-excursion cut for snipe/scout trades", "module": "position_guardian.py", "tier": 1},
    "guardian.adv_cut_pips": {"value": 10.0, "default": 10.0, "min": 5.0, "max": 25.0, "description": "Max adverse pips threshold to trigger early cut (within first N M15 bars)", "module": "position_guardian.py", "tier": 1},
    "guardian.adv_cut_by_bar": {"value": 4, "default": 4, "min": 2, "max": 8, "description": "M15 bar window for adverse-cut check (4 = 60 min)", "module": "position_guardian.py", "tier": 1},
    "guardian.adv_cut_excluded_pairs": {"value": ["EUR_AUD", "AUD_USD"], "default": ["EUR_AUD", "AUD_USD"], "description": "Pairs where adv-cut hurts (deep-retrace recovery archetypes per 90d backtest)", "module": "position_guardian.py", "tier": 2},
    "guardian.adv_cut_require_e55_break": {"value": True, "default": True, "description": "Structural guard: only fire adv_cut when retrace_zone is e55_retrace/e100_broken AND fan still ordered. 30-day replay 2026-05-06: drops winner-kill 10.4%→1.5%, keeps ~30% loss-save coverage. Reuses existing retrace_zone + e21_crossed_e55_against tracking.", "module": "position_guardian.py", "tier": 1},

    # ── failed_rally REWRITE (2026-05-11): exhaustion handler for failing trades ──
    # Replaces the old failed_rally_lock (disabled via override #308 on 2026-05-11).
    # Scope is strictly "trades that aren't recovering" — does NOT touch profit
    # management (profit floor, trailing SL, partial exits all handle winners).
    #
    # Classifier-gated lock at decision bar (peak + first neg M15 close).
    # Universe: brief-positive pattern AND mfe_min <= MFE < mfe_max AND
    # decision_bar <= arm_window. Logistic regression trained on 90d feature
    # matrix (RSI, ADX, BB, fan separation, MFE, MFE_bar, etc.) outputs P(loser).
    # Fires only when P >= classifier_threshold.
    # 90d backtest: 12 fires, 8 saves +85.8p, 4 kills -12.6p, NET +73.2p, 67% precision.
    # Post-tune: 2 fires, 2 saves +42.7p, 0 kills, NET +42.7p, 100% precision.
    #
    # Dry-run mode: rule evaluates and LOGS what it would do but takes NO live
    # action. Used for forward-validation before live cutover.
    #
    # Path B (never-positive hard-cut) is a separate future rule, not in this block.
    "guardian.early_exhaustion_enabled":            {"value": False, "default": False, "description": "Master switch for the failed_rally rewrite. Default False until forward-validated.", "module": "position_guardian.py", "tier": 1},
    "guardian.early_exhaustion_dry_run":            {"value": True,  "default": True,  "description": "Dry-run mode — rule evaluates and LOGS what it would do (move SL) but takes NO live action. Set to False only after forward-validation passes.", "module": "position_guardian.py", "tier": 1},
    "guardian.early_exhaustion_mfe_min_pips":       {"value": 3.0,  "default": 3.0,  "min": 1.0, "max": 8.0, "description": "Rule universe: MFE must be at least this much for the brief-positive rally to count as 'real' (below this is noise inside spread).", "module": "position_guardian.py", "tier": 2},
    "guardian.early_exhaustion_mfe_max_pips":       {"value": 10.0, "default": 10.0, "min": 5.0, "max": 15.0, "description": "Rule universe ceiling — at/above this MFE, existing profit-management logic owns the exit. Rule doesn't fire.", "module": "position_guardian.py", "tier": 2},
    "guardian.early_exhaustion_arm_window_bars":    {"value": 8,    "default": 8,    "min": 2, "max": 16, "description": "Rule only fires if decision_bar (peak + first neg) is within this many M15 bars from open. Failed-rally is an early-trade pattern.", "module": "position_guardian.py", "tier": 2},
    "guardian.early_exhaustion_classifier_threshold": {"value": 0.65, "default": 0.65, "min": 0.50, "max": 0.85, "description": "Classifier P(loser) threshold to fire. 0.65 → 67% precision / 80% recall in 90d sweep. Higher = more conservative.", "module": "position_guardian.py", "tier": 1},
    "guardian.early_exhaustion_lock_pips":          {"value": 0.5,  "default": 0.5,  "min": 0.0, "max": 3.0, "description": "Where new SL is set when rule fires — entry + this_value pips in profit direction. 0.5p covers spread, locks tiny win.", "module": "position_guardian.py", "tier": 2},

    # ── Exit-marker event-driven dual-mode rule (2026-05-14, v2) ──
    # Listens for OPPOSING ⚠ Exit (peak_sep) markers that APPEAR DURING the live
    # trade (within first N M15 bars), comparing each bar's marker set vs the
    # snapshot at trade open. New marker = retrace signal incoming.
    #
    # DUAL MODE:
    #   • pnl > 0 when marker appears  → TAKE PROFIT NOW at current bar close
    #                                      (book the top before retrace bites)
    #   • pnl <= 0 when marker appears → TIGHTEN SL to current_close - 1p
    #                                      (let recovery happen, but cap downside)
    #
    # 30d backtest (200 non-kronos trades, watch=15):
    #   snipe_direct: 30 fires, 24 helped +245p / 0 hurt — clean
    #   scout:        28 fires, 19 SL-helped +104p / 5 TP +29p / 12 hurt -28p — net +132p
    #   ALL:          60 fires, NET +373.1p
    # Original v1 (at-entry lookup, immediate close): NET +883p but cut more winners
    # and had reload-bug. v2 is cleaner: tightens SL instead of closing, lets
    # recoveries happen. Excludes kronos_hunter (separate namespace).
    "guardian.exit_marker_be_enabled":              {"value": True,  "default": True,  "description": "Enable event-driven exit-marker rule (v2 dual-mode). Listens for new opposing peak_sep markers during trade. Profitable → take profit. At/below 0 → tighten SL. Backtest 30d watch=15: NET +373p.", "module": "position_guardian.py", "tier": 1},
    "guardian.exit_marker_be_window_bars":          {"value": 15,    "default": 15,    "min": 5, "max": 30, "description": "M15 bars after entry to keep listening for new opposing peak_sep markers. >=15 bars start cannibalizing other guardian rules (watch=999 backtest dropped to +301p vs watch=15 +373p).", "module": "position_guardian.py", "tier": 2},
    "guardian.exit_marker_be_pips":                 {"value": 0.5,   "default": 0.5,   "min": 0.0, "max": 3.0, "description": "For TP mode: bar close exit accepted as-is (this is informational). Kept for legacy compatibility — not used in dual-mode logic.", "module": "position_guardian.py", "tier": 2},
    "guardian.exit_marker_neg_lock_buffer_pips":    {"value": 1.0,   "default": 1.0,   "min": 0.5, "max": 5.0, "description": "When marker appears with pnl<=0: SL placed at current_close - this many pips (adverse direction). Buffer prevents immediate-tick whipsaw exits. 1.0p validated in 30d backtest.", "module": "position_guardian.py", "tier": 2},
    "guardian.exit_marker_be_excluded_sources":     {"value": ["kronos_hunter"], "default": ["kronos_hunter"], "description": "Trade sources where this rule does NOT fire. Kronos has separate namespace (kronos.*) and was not in the validation cohort.", "module": "position_guardian.py", "tier": 2},
    "guardian.exit_marker_in_loss_action":          {"value": "tighten", "default": "tighten", "enum": ["tighten","kill"], "description": "Action on exit-marker fire when pnl<=0. 'tighten' (legacy 2026-05-14): set SL to current-1p buffer, allows recovery. 'kill' (audit 2026-05-17): close at market immediately. 30d audit n=38 in-loss fires: kill beats tighten by +50p on losers (-410p→-460p saved) and +13p on winners (winners-side cost is -140p kill vs -154p tighten). Net +63p/30d. Flip to 'kill' to enable.", "module": "position_guardian.py", "tier": 1},

    # ── Real-time loser-pattern detector (2026-05-15, claude-code, Tim approved) ──
    # Behavioral counterpart to exit_marker (which is chart-structural).
    # Catches "entered late into exhaustion, riding the retrace" — when bar 2-3
    # from entry shows MFE=0 + 3 adverse closes + RSI counter-direction by ≥5.
    # Action: SL→break-even (entry price). Trade exits flat instead of bleeding.
    # 30d backtest (259 trades): NET +298p. snipe_direct +275.8p, scout +26.4p.
    # Restricted to snipe_direct initially (where +275.8p edge is clear).
    "guardian.rt_loser_pattern_enabled":            {"value": True,  "default": False, "description": "Enable real-time loser-pattern detector. Fires on M15 bar close when MFE=0 + adv_streak≥3 + RSI moving counter-direction ≥5/3bars. Action: SL→break-even. 30d backtest: +298p NET (92% from snipes).", "module": "position_guardian.py", "tier": 1},
    "guardian.rt_loser_pattern_sources":            {"value": ["snipe_direct", "scout", "manual"], "default": ["snipe_direct", "scout", "manual"], "description": "Trade sources where this rule fires. ALL sources — late-entry pattern is the same regardless of who initiated. 30d backtest: snipe +275.8p clean, scout +26.4p marginal, manual sample too small. Tim 2026-05-15: pattern is the pattern, apply universally. Kronos still excluded (separate namespace).", "module": "position_guardian.py", "tier": 2},
    "guardian.rt_loser_pattern_mfe_max_pips":       {"value": 2.0,   "default": 2.0,   "min": 0.5, "max": 5.0, "description": "Max favorable excursion (pips) for rule to fire. ≤2p means trade essentially never went positive. Validated 2.0 on 30d sweep.", "module": "position_guardian.py", "tier": 2},
    "guardian.rt_loser_pattern_adv_streak":         {"value": 3,     "default": 3,     "min": 2, "max": 5, "description": "Min consecutive M15 closes against trade direction. 3 validated; 2 = noise, 4 = too late.", "module": "position_guardian.py", "tier": 2},
    "guardian.rt_loser_pattern_rsi_dir_min":        {"value": 5.0,   "default": 5.0,   "min": 2.0, "max": 10.0, "description": "Min RSI movement against trade direction over 3 bars. ≥5 points filters noise. Validated on 30d.", "module": "position_guardian.py", "tier": 2},
    "guardian.rt_loser_pattern_pnl_low":            {"value": -20.0, "default": -20.0, "min": -50.0, "max": -5.0, "description": "Lower bound on bar 2-3 pnl_close for rule to fire. Trades already in catastrophic drawdown (<-20p by bar 3) are too far gone — let original SL handle them.", "module": "position_guardian.py", "tier": 2},
    "guardian.rt_loser_pattern_pnl_high":           {"value": -1.0,  "default": -1.0,  "min": -5.0, "max": 0.0, "description": "Upper bound on bar 2-3 pnl_close. Trade must be at least -1p underwater to fire.", "module": "position_guardian.py", "tier": 2},

    # ── Entry-time fresh-marker detector (2026-05-15, FRONT-HALF complement to rt_loser_pattern) ──
    # Validated 2026-05-15 morning via scripts/backtest_composite_entry_block.py:
    # 30d composite check (fresh opposing peak_sep + reversal candle + retrace from extreme)
    # at entry: K=10 catches 19/258 trades, NET +101.3p (10 winners cost -28.5p / 9 losers saved +129.8p).
    # Fires on FIRST M15 bar close after entry (one bar of confirmation, not instant spawn).
    # Action: SL→entry±buffer (mirror exit_marker's tight-SL approach).
    "guardian.entry_marker_fresh_enabled":          {"value": True,  "default": False, "description": "Enable entry-time fresh-marker detector. Checks at first M15 close after entry: fresh opposing peak_sep within last K bars of entry + reversal candle + price retrace. Action: SL→entry±buffer. 30d backtest: +101.3p NET. FRONT-HALF complement to rt_loser_pattern (which is bar 2-3 BACK-HALF).", "module": "position_guardian.py", "tier": 1},
    "guardian.entry_marker_fresh_sources":          {"value": ["snipe_direct", "scout", "manual"], "default": ["snipe_direct", "scout", "manual"], "description": "Trade sources for entry-time fresh-marker check. Same scope as rt_loser_pattern (all non-kronos).", "module": "position_guardian.py", "tier": 2},
    "guardian.entry_marker_fresh_k_bars":           {"value": 10,    "default": 10,    "min": 4, "max": 20, "description": "Marker freshness: opposing peak_sep's underlying peak must be confirmed within last K M15 bars of entry. K=10 = +101p NET (best). K=6 = +61p. K=4 = +20p.", "module": "position_guardian.py", "tier": 2},
    "guardian.entry_marker_fresh_retrace_lookback": {"value": 8,     "default": 8,     "min": 4, "max": 20, "description": "Bars to scan for recent high/low when checking 'price retraced from extreme' (condition C). 8 bars matches morning backtest.", "module": "position_guardian.py", "tier": 2},
    "guardian.entry_marker_fresh_lock_buffer_pips": {"value": 2.0,   "default": 2.0,   "min": 0.5, "max": 5.0, "description": "SL placed at entry ± buffer pips (adverse direction). Buffer prevents immediate spread-trigger. 2p validated as minimum safe distance.", "module": "position_guardian.py", "tier": 2},

    # ── Guardian emergency thresholds ──
    # ── OANDA SL widening kill switch (2026-05-15, Tim approved) ──
    # Guardian was widening OANDA's SL on spawn from the planned sl_price to a
    # 3×ATR or E100+0.5×ATR catastrophic floor. Trades 15910/15972/16116/etc
    # reached planned SL distance but OANDA's widened SL was 2-5x further out,
    # so trades kept bleeding past the user's intended SL.
    #
    # 30d backtest (257 trades): widening provided NET +6p of "edge" over
    # not-widening — essentially noise. Trade-off:
    #   WITH widening: 80% WR but occasional -30 to -50p tail losses
    #   NO widening:  ~75% WR with all losses capped at planned SL distance
    # Net pip neutral; risk profile vastly cleaner without widening.
    #
    # When False (default): OANDA SL stays at the planned sl_price. Other
    # guardian rules (profit_floor, dynamic_sl_trail) still TIGHTEN as the
    # trade goes favorable, but the disaster-cap widening is disabled.
    # Kronos still has its own skip path (always disabled per existing code).
    "guardian.widen_oanda_sl_enabled":  {"value": False, "default": False, "min": False, "max": True, "description": "When True, guardian widens OANDA's SL on spawn to a 3xATR or E100+buffer catastrophic floor. Disabled 2026-05-15 because widening was turning planned 17p SLs into 90p+ blowouts on adverse-from-entry trades. 30d backtest: widening provided NET +6p (noise) vs no-widening — risk profile much cleaner without.", "module": "position_guardian.py", "tier": 1},

    "guardian.spread_spike_multiplier": {"value": 8.0, "default": 8.0, "min": 3.0, "max": 15.0, "description": "Spread must be Nx normal before emergency. Was 4x (too sensitive), now 8x.", "module": "position_guardian.py", "tier": 2},
    "guardian.margin_danger_pct": {"value": 80.0, "default": 80.0, "min": 60.0, "max": 95.0, "description": "Margin usage % that triggers emergency close", "module": "position_guardian.py", "tier": 2},

    # ── Snipe re-fire gate (2026-04-21) ──
    # 60-day backtest showed re-fires of same watch are statistically worse than
    # first fires. Fire 4+ net -108p, Fire 6+ 0/3 WR -81p. Re-fires with gap >240min
    # show 17% WR. Cap 3 + 120min gap saves +186p/14d (+282p/60d) vs baseline.
    # Winners given up: +52p (small 3-7p wins). Losers avoided: -239p (tail -71p, -38p, -35p).
    # Preserves first fires (65% WR) and genuine continuation fires (2-3 within 2h window).
    "gate.snipe_max_fires_per_watch_per_day": {"value": 3, "default": 3, "min": 1, "max": 10, "description": "Max fires per watch_id per UTC day. Fire #N+1 blocked. Backtest: Cap 3 saves +186p/14d (baseline -132p → +54p kept).", "module": "trading_cycle.py", "tier": 1},
    "gate.snipe_refire_max_gap_minutes": {"value": 120, "default": 120, "min": 30, "max": 480, "description": "Max minutes between fires on same watch. Re-fires with gap >240min show 17% WR (-58p). 120min cutoff blocks stale re-entries after trend has stalled.", "module": "trading_cycle.py", "tier": 1},

    # ── Validator-snipe fan-alignment gate (2026-04-28) ──
    # Block validator snipes when fan_sep is at/past peak AND entry candle reverses
    # color (counter-direction bar). 60d backtest on 155 validator trades:
    # baseline -608p / -$2,183 → with gate +47p / +$748 (net +$2,932 swing).
    # Blocks 51 trades (12 wins / 39 losses, 76% precision); 11 of 13 known
    # cohort losers caught. WR 56.1% → 72.1%.
    # Default OFF — flip on after observation period.
    "gate.validator_fan_alignment_enabled": {"value": False, "default": False, "min": False, "max": True, "description": "Block validator snipes when fan_sep at/past peak AND entry candle reverses (counter-direction bar). Detected via fan_at_peak / fan_post_peak / fan_reversal in last 12 M15 bars. Backtest 60d: +$2,932 net, 11×favorable ratio.", "module": "trading_cycle.py", "tier": 1},
    "gate.gate1_enabled": {"value": True, "default": True, "min": False, "max": True, "description": "Master kill-switch for the Gate 1 pre-validator block at trading_cycle.py:6589. When False, Gate 1 still runs as informational signal (orchestrator log shows pass/fail) but no longer short-circuits the cycle to GATE1_BLOCK. Set False on 2026-05-05 — Opus call cost was the original justification, moot on local 35B. Validator can absorb full scout volume now that TA Picture data is reaching it.", "module": "trading_cycle.py", "tier": 1},
    "gate.gate1_sanity_enabled": {"value": False, "default": False, "min": False, "max": True, "description": "Gate 1 SANITY hard-block at trading_cycle.py:5705. When True, blocks scout-triggered cycles where fan_direction is neutral/mixed and no qualifying alert type — kills cycle before validator runs. Set False on 2026-05-07 — was killing Phase 1 EARLY_FORMATION setups (fresh cross fired, fan not yet labeled bullish/bearish in fan_direction field) before validator could evaluate them as WATCH. Validator decides on every cycle now; it will SKIP weak setups itself.", "module": "trading_cycle.py", "tier": 1},
    "gate.tight_fan_enabled": {"value": True, "default": True, "min": False, "max": True, "description": "Tight-stale or overextended Phase 3 cascade gate (tight_fan_gate.py). Blocks TRADE_NOW entry when phase=3 AND separation_pct<0.10% AND (cross3_bars_since>=20 OR price_extension_atr>=3.4). Catches 14% of trades (61/425 over 30d). Backtest 2026-05-14: +197.7p net over 30d (saved -286.4p of losses, gave up +88.7p of small winners). Fires at place_market_order choke points for both validator path and snipe_direct path.", "module": "trading_cycle.py", "tier": 1},
    "gate.skip_ta_prefeed": {"value": False, "default": False, "min": False, "max": True, "description": "TEST FLAG. When True, skip the TA 35B call — validator runs alone against chart + scout evidence + raw indicators + patterns + intelligence. Same 35B model, just one prompt pass instead of two. Tests whether validator reasons independently or rubber-stamps TA's narrative framing. Saves ~120s per cycle.", "module": "trading_cycle.py", "tier": 1},
    "gate.validator_fan_alignment_lookback": {"value": 12, "default": 12, "min": 6, "max": 20, "description": "M15 bar lookback for fan_sep peak detection in validator-fan-alignment gate. Sweep optimal=12.", "module": "trading_cycle.py", "tier": 2},
    "gate.validator_fan_alignment_rise_n": {"value": 3, "default": 3, "min": 2, "max": 6, "description": "Bars-back used to confirm fan was rising into the current peak (current > N bars ago).", "module": "trading_cycle.py", "tier": 2},
    "gate.validator_fan_alignment_reversal_k": {"value": 6, "default": 6, "min": 3, "max": 12, "description": "Bars looked back for E21-E55 sign-flip detection (fan reversal signal).", "module": "trading_cycle.py", "tier": 2},

    # ── Snipe session-gate: Sunday blackout compliance (2026-04-20) ──
    # Snipes had a blanket bypass of the session gate. Sunday blackout (21-23 UTC
    # = 5-7PM ET) exists to give market liquidity 2h to normalize after open.
    # Trade 7572 EUR_CHF fired 21:21 UTC Sunday, ran into chop. Rule: snipes
    # respect Sunday blackout; still exempt from EUR/GBP-Asian and Friday-close.
    "gate.snipe_respects_sunday_blackout": {"value": True, "default": True, "min": False, "max": True, "description": "When True, snipes respect Sunday 5-7PM ET blackout (2h reset window after market open). Snipes still exempt from EUR/GBP deep-Asian and Friday-close session rules.", "module": "trading_cycle.py", "tier": 1},

    # ── AUD late-EU bleed window (2026-05-10 deploy) ──
    # 60d audit of AUD non-kronos closed trades by UTC hour: UTC 21 = 4 trades
    # 0% WR -82.7p; UTC 22 = 2 trades 0% WR -26.7p. Combined 0/6 WR, -109p net.
    # Phase=3 cascade signals fire textbook but fizzle without follow-through —
    # Asian-session start has insufficient AUD liquidity. Validator-cohort losses
    # 13727 (AUD_USD -30.4p) and 13743 (AUD_JPY -26.7p) both fall in this window.
    "gate.session_aud_late_eu_enabled": {"value": True, "default": True, "min": False, "max": True, "description": "When True, blocks AUD-pair trades UTC 21-22 on weekdays (excludes Sun blackout + Fri close already handled). 60d data: 0/6 WR, -109p net.", "module": "trading_cycle.py", "tier": 1},

    # ── Guardian retrace SL trail KILL SWITCH (2026-04-20) ──
    # DISABLED: Phase 3 retrace trail walks broker-side SL toward E100 during
    # retrace. During EMA compression, E100 is right at current price → trail
    # tightens SL into price noise → premature stop hit. Same fan-structure
    # signals as the scorer's false BLACKs. Re-enable after scorer/trail
    # rewrite uses candle-to-EMA position as primary signal.
    "guardian.retrace_sl_trail_enabled": {"value": False, "default": False, "min": False, "max": True, "description": "Kill switch for Phase 3 retrace SL trail (retrace_trail_e100). False=trail disabled during retrace, planned SL protects. True=trail active.", "module": "position_guardian.py", "tier": 1},

    # ── Guardian auto_close_threat90 KILL SWITCH (2026-04-20) ──
    # DISABLED: scorer over-fires on normal M15 oscillation (fan compression near
    # E100 on SELL approaching support scores as "trend structure gone" even when
    # candles respect EMAs in trade direction). Trade 7815 killed at -1.5p with
    # 93% SL unused. Re-enable only after scorer rewritten to use candle-to-EMA
    # position as primary signal.
    "guardian.auto_close_threat90_enabled": {"value": False, "default": False, "min": False, "max": True, "description": "Master kill switch for auto_close_threat90 action. False=disabled (dynamic SL trail + planned SL protect). True=enabled. Disabled 2026-04-20 due to false-positive scorer.", "module": "trading_api_routes.py", "tier": 1},
    # Deprecated (kept for config history) — sustained-ticks gate replaced by kill switch
    "guardian.auto_close_threat90_min_sustained_ticks": {"value": 5, "default": 5, "min": 2, "max": 15, "description": "DEPRECATED by kill switch. Was: min consecutive M1 ticks threat must stay >= sustained_threshold before auto_close fires.", "module": "trading_api_routes.py", "tier": 2},
    "guardian.auto_close_threat90_sustained_threshold": {"value": 80, "default": 80, "min": 60, "max": 90, "description": "DEPRECATED by kill switch. Was: threat score floor used by sustained-ticks count.", "module": "trading_api_routes.py", "tier": 2},

    # ── Guardian trailing stop & ratchet ──
    "guardian.ratchet_step_pips": {"value": 3.67, "default": 3.67, "min": 0.5, "max": 10.0, "description": "Profit floor ratchet step in pips. V2 optimizer (1000-trial) found 3.67p optimal.", "module": "position_guardian.py", "tier": 1},
    "guardian.trailing_activation_rr": {"value": 0.5, "default": 0.5, "min": 0.1, "max": 1.0, "description": "Trailing stop activates after this fraction of SL distance is profit. Optimizer found 0.2 optimal.", "module": "position_guardian.py", "tier": 1},
    "guardian.trailing_atr_mult": {"value": 1.0, "default": 1.0, "min": 0.2, "max": 2.5, "description": "Trailing distance as ATR multiplier. Optimizer found 0.3 optimal.", "module": "position_guardian.py", "tier": 1},

    # ── Guardian SL buffer ──
    "guardian.sl_buffer_pips": {"value": 3, "default": 3, "min": 1, "max": 8, "description": "Fixed SL buffer in pips. Was 1 (spread noise stops), now 3.", "module": "position_guardian.py", "tier": 1},
    "guardian.sl_buffer_atr_mult": {"value": 0.5, "default": 0.5, "min": 0.2, "max": 1.0, "description": "Dynamic SL buffer as fraction of ATR (clamped to min 3p, max 8p)", "module": "position_guardian.py", "tier": 2},

    # ── Guardian retrace detection & grace periods ──
    "guardian.retrace_candle_count": {"value": 10, "default": 10, "min": 5, "max": 25, "description": "Candles in retrace before deep-retrace logic fires. V4: was 20.", "module": "position_guardian.py", "tier": 2},
    "guardian.min_candles_retrace_exit": {"value": 15, "default": 15, "min": 8, "max": 40, "description": "Min M1 candles in trade before auto-close on deep retrace.", "module": "position_guardian.py", "tier": 2},
    "guardian.manual_grace_candles": {"value": 90, "default": 90, "min": 30, "max": 180, "description": "M1 candles (~6 M15 bars) of grace for manual trades before guardian acts", "module": "position_guardian.py", "tier": 2},
    "guardian.retrace_discount_tight": {"value": 0.80, "default": 0.80, "min": 0.50, "max": 0.95, "description": "Threat discount when EMAs nearly merged (convergence < 0.05%)", "module": "position_guardian.py", "tier": 2},
    "guardian.retrace_discount_close": {"value": 0.60, "default": 0.60, "min": 0.30, "max": 0.80, "description": "Threat discount when EMAs close (convergence < 0.10%)", "module": "position_guardian.py", "tier": 2},
    "guardian.post_retrace_cooldown_s": {"value": 300, "default": 300, "min": 60, "max": 600, "description": "Seconds of grace after retrace ends (5 min)", "module": "position_guardian.py", "tier": 3},

    # ── Entry gate thresholds (trading_cycle.py) ──
    "gate.bb_width_min_pips": {"value": 6.0, "default": 6.0, "min": 3.0, "max": 15.0, "description": "Min BB width for entry. Winners avg 10.8p, Losers avg 7.4p.", "module": "agents/trading_cycle.py", "tier": 1},
    "gate.cooldown_hours": {"value": 0.5, "default": 0.5, "min": 0.25, "max": 4.0, "description": "Post-loss cooldown on same pair. Was 2h (too aggressive), now 0.5h.", "module": "agents/trading_cycle.py", "tier": 1},
    "gate.sl_atr_mult": {"value": 2.5, "default": 2.5, "min": 1.0, "max": 4.0, "description": "SL distance in ATR multiples. Was 1.5 (too tight), now 2.5.", "module": "agents/trading_cycle.py", "tier": 1},
    "gate.tp_atr_mult": {"value": 2.0, "default": 2.0, "min": 1.0, "max": 4.0, "description": "TP distance in ATR multiples. Was 1.0 (~8p too tight), now 2.0 (~16p).", "module": "agents/trading_cycle.py", "tier": 1},
    "gate.sl_atr_mult_expanding_fan": {"value": 3.0, "default": 3.0, "min": 2.0, "max": 5.0, "description": "Max SL ATR mult for expanding fan entries", "module": "agents/trading_cycle.py", "tier": 2},
    "gate.min_rr_ratio": {"value": 1.2, "default": 1.2, "min": 0.8, "max": 2.0, "description": "Hard minimum Risk:Reward ratio. Below = blocked.", "module": "agents/trading_cycle.py", "tier": 1},
    "gate.stoch_dont_buy_above": {"value": 65, "default": 65, "min": 55, "max": 80, "description": "Dont BUY when stoch above this and falling", "module": "agents/trading_cycle.py", "tier": 1},
    "gate.stoch_dont_sell_below": {"value": 35, "default": 35, "min": 20, "max": 45, "description": "Dont SELL when stoch below this and rising", "module": "agents/trading_cycle.py", "tier": 1},
    "gate.story_score_min": {"value": 50, "default": 50, "min": 30, "max": 75, "description": "Minimum market story score for trade entry", "module": "agents/trading_cycle.py", "tier": 1},
    "gate.confidence_elite": {"value": 0.85, "default": 0.85, "min": 0.70, "max": 0.95, "description": "Scout confidence for elite setups (WR >= 90%)", "module": "agents/trading_cycle.py", "tier": 2},
    "gate.confidence_elevated": {"value": 0.70, "default": 0.70, "min": 0.55, "max": 0.85, "description": "Scout confidence for elevated setups (WR >= 85%)", "module": "agents/trading_cycle.py", "tier": 2},
    "gate.confidence_base": {"value": 0.55, "default": 0.55, "min": 0.40, "max": 0.70, "description": "Scout confidence for base setups", "module": "agents/trading_cycle.py", "tier": 2},

    # ── Bounce-trap + post-win-exhaustion gates (2026-04-14) — validated on 185 trades ──
    "gate.oscillator_freshness_enabled": {"value": True, "default": True, "min": False, "max": True, "description": "Master switch: oscillator_freshness gate (stale-drop + bounce-trap patterns). Zero false positives on 185 trades.", "module": "agents/trading_cycle.py", "tier": 1},
    "gate.bounce_trap_prev_max": {"value": 15.0, "default": 15.0, "min": 5.0, "max": 25.0, "description": "Max stoch_prev for SELL bounce-trap detection (mirror for BUY top-trap = 100-this).", "module": "agents/trading_cycle.py", "tier": 2},
    "gate.bounce_trap_jump_min": {"value": 20.0, "default": 20.0, "min": 10.0, "max": 35.0, "description": "Min stoch jump (1 bar) to trigger bounce-trap block. 5230 had jump=22.9.", "module": "agents/trading_cycle.py", "tier": 2},
    "gate.bounce_trap_top_prev_min": {"value": 85.0, "default": 85.0, "min": 70.0, "max": 95.0, "description": "Min stoch_prev for BUY top-trap detection (mirror of bounce_trap_prev_max).", "module": "agents/trading_cycle.py", "tier": 2},
    "gate.post_win_exhaustion_enabled": {"value": True, "default": True, "min": False, "max": True, "description": "Master switch: post_win_exhaustion gate. Blocks entries when M15 BB contracted >50% vs recent same-setup win.", "module": "agents/trading_cycle.py", "tier": 1},
    "gate.post_win_exhaustion_contraction_min": {"value": 0.50, "default": 0.50, "min": 0.30, "max": 0.80, "description": "Min BB contraction vs last same-setup win to trigger exhaustion block (0.5 = 50%).", "module": "agents/trading_cycle.py", "tier": 2},
    "gate.post_win_exhaustion_lookback_hours": {"value": 6, "default": 6, "min": 1, "max": 24, "description": "Hours back to look for 'last same-setup win'. Stale wins poison comparison.", "module": "agents/trading_cycle.py", "tier": 2},
    "gate.post_win_exhaustion_prior_bb_pips_min": {"value": 20.0, "default": 20.0, "min": 10.0, "max": 40.0, "description": "Min prior-win BB pips to apply exhaustion gate. Modest wins don't drain market; only big moves cause exhaustion.", "module": "agents/trading_cycle.py", "tier": 2},
    "gate.fan_exhaustion_min_pips": {"value": 4.0, "default": 4.0, "min": 1.0, "max": 12.0, "description": "Min total E21-E100 fan width (pips) below which fan is considered collapsed/exhausted. Rewrite of 2026-05-15 replaces the old label-based gate (was 66% false-positive on stable/contracting parallel-cruise fans). Now blocks only when EMAs unordered for snipe direction OR fan collapsed below this width.", "module": "agents/trading_cycle.py", "tier": 2},

    # ── Scout parameters ──
    "scout.snipe_trigger_threshold": {"value": 0.90, "default": 0.90, "min": 0.70, "max": 0.95, "description": "Watch progress % to trigger snipe. Was 0.80 (losses), raised 0.90 2026-03-11.", "module": "trade_scout.py", "tier": 1},
    "scout.win_rate_elite": {"value": 80.0, "default": 80.0, "min": 65.0, "max": 95.0, "description": "Win rate threshold for elite snipe setup selection", "module": "trade_scout.py", "tier": 2},

    # ── Watch manager parameters ──
    "watch.ema_velocity_threshold": {"value": 0.003, "default": 0.003, "min": 0.001, "max": 0.010, "description": "EMA velocity acceleration threshold. Lowered from 0.005.", "module": "agents/watch_manager.py", "tier": 2},
    "watch.thesis_similarity": {"value": 0.95, "default": 0.95, "min": 0.70, "max": 1.00, "description": "Jaccard threshold for snipe dedup on the identity-aware signature (cond|field|op|value + dir + setup_name). 0.95 = 'literally the same trade'. Raised from 0.70 → 0.95 on 2026-05-07 alongside richer signature — the old 0.70 was tuned for field|op-only and was suppressing genuinely different setups (C4_CHART_PATTERN_BREAK vs V4_continuation colliding at 71%).", "module": "agents/watch_manager.py", "tier": 2},
    "watch.progress_preserve_threshold": {"value": 0.70, "default": 0.70, "min": 0.50, "max": 0.90, "description": "Watch progress above this cannot be expired", "module": "agents/watch_manager.py", "tier": 3},
    "watch.stale_replace_peak_threshold": {"value": 0.70, "default": 0.70, "min": 0.50, "max": 0.90, "description": "When a new snipe is similar to an existing watching snipe, supersede the existing one if its peak_progress never crossed this threshold. Captures setups that never developed so fresh validator output replaces them instead of being skipped as duplicate.", "module": "agents/watch_manager.py", "tier": 2},
    "watch.age_check_hours": {"value": 4.0, "default": 4.0, "min": 1.0, "max": 12.0, "description": "Min watch age before expiration checking begins", "module": "agents/watch_manager.py", "tier": 3},

    # ── Kronos Integration — Operational ───────────────────────────────────
    "kronos.enabled": {
        "value": True, "default": True, "min": False, "max": True,
        "description": "Master Kronos kill switch (both Hunter + Filter)",
        "module": "kronos_inference.py", "tier": 1, "is_int": False,
    },
    "kronos.hunter_enabled": {
        "value": True, "default": True, "min": False, "max": True,
        "description": "Kronos Hunter on/off (independent trade discovery)",
        "module": "kronos_hunter.py", "tier": 1, "is_int": False,
    },
    "kronos.filter_enabled": {
        "value": True, "default": True, "min": False, "max": True,
        "description": "Kronos Filter on/off (pre-validator veto)",
        "module": "kronos_filter.py", "tier": 1, "is_int": False,
    },
    "kronos.shadow_mode": {
        "value": True, "default": True, "min": False, "max": True,
        "description": "If True, Hunter records signals but does NOT place orders",
        "module": "kronos_hunter.py", "tier": 1, "is_int": False,
    },
    "kronos.hunter_cadence_minutes": {
        "value": 15, "default": 15, "min": 5, "max": 60,
        "description": "How often Hunter scans all 13 pairs (M15 default)",
        "module": "kronos_hunter.py", "tier": 2, "is_int": True,
    },
    "kronos.hunter_min_drift_pips": {
        "value": 5.0, "default": 5.0, "min": 2.0, "max": 20.0,
        "description": "Minimum absolute forecast drift to trigger a signal",
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.hunter_min_drift_atr_frac": {
        "value": 0.5, "default": 0.5, "min": 0.2, "max": 2.0,
        "description": "Minimum drift as fraction of ATR",
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.hunter_loss_cooldown_count": {
        "value": 1, "default": 1, "min": 0, "max": 5,
        "description": "Pause Hunter on a pair after this many CLOSED losses inside the cooldown window. 0 disables the gate (no loss-based cooldown). Wins never trigger cooldown.",
        "module": "kronos_hunter.py", "tier": 1, "is_int": True,
    },
    "kronos.hunter_loss_cooldown_hours": {
        "value": 4, "default": 4, "min": 1, "max": 48,
        "description": "Look-back window (hours) for counting recent kronos_hunter losses on a pair.",
        "module": "kronos_hunter.py", "tier": 2, "is_int": True,
    },
    "kronos.hunter_max_concurrent_trades": {
        "value": 13, "default": 13, "min": 1, "max": 13,
        "description": "Max concurrent open Hunter trades (13 = effectively disabled; "
                       "pair-level dedup naturally caps at 13)",
        "module": "kronos_hunter.py", "tier": 1, "is_int": True,
    },
    # ── Kronos Hunter — Regime filter (2026-04-15) ────────────────────
    # Real intraday losses today showed 8 of 11 losses opened with fan
    # misaligned against trade direction (counter-trend), 5 in compression,
    # 5 with flat E21 slope. Backtest 83.9% WR was on cached data that
    # dodged the live chop. These gates reject noodling/chop entries.
    "kronos.hunter_scout_bias_gate": {
        "value": True, "default": True,
        "description": "Block Kronos trades that disagree with scout's fan direction (Gate 1.3). Backtest shows 80% WR on blocked trades — disable to let Kronos find reversals before fan confirms",
        "module": "kronos_hunter.py", "tier": 1, "is_bool": True,
    },
    "kronos.hunter_require_fan_aligned": {
        "value": True, "default": True,
        "description": "Reject Kronos entries when EMA fan is pointing against trade direction (no counter-trend)",
        "module": "kronos_hunter.py", "tier": 1, "is_bool": True,
    },
    "kronos.hunter_min_fan_sep_atr": {
        "value": 0.8, "default": 0.8, "min": 0.0, "max": 2.0,
        "description": "Reject if total E21→E100 separation < this × ATR (EMAs too compressed/noodling)",
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.hunter_min_e21_slope_pips": {
        "value": 1.0, "default": 1.0, "min": 0.0, "max": 5.0,
        "description": "Reject if |E21 slope over 5 bars| < this (flat fan = no trend)",
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.hunter_daily_kill_switch_pips": {
        "value": -50, "default": -50, "min": -200, "max": -10,
        "description": "Pause Hunter for 24h if daily pnl <= this",
        "module": "kronos_hunter.py", "tier": 1, "is_int": True,
    },
    "kronos.hunter_session_gate_enabled": {
        "value": True, "default": True, "is_bool": True,
        "description": "Block Kronos at weekend edges (Sunday 21-23 UTC, Friday 20+ UTC)",
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.hunter_sunday_block_start_utc": {
        "value": 21, "default": 21, "min": 20, "max": 23,
        "description": "Sunday block start hour UTC (inclusive)",
        "module": "kronos_hunter.py", "tier": 1, "is_int": True,
    },
    "kronos.hunter_sunday_block_end_utc": {
        "value": 23, "default": 23, "min": 22, "max": 24,
        "description": "Sunday block end hour UTC (exclusive)",
        "module": "kronos_hunter.py", "tier": 1, "is_int": True,
    },
    "kronos.hunter_friday_block_start_utc": {
        "value": 20, "default": 20, "min": 18, "max": 22,
        "description": "Friday block start hour UTC (blocks through market close)",
        "module": "kronos_hunter.py", "tier": 1, "is_int": True,
    },
    # ── Kronos bleed-hour blackout (2026-04-22) ──
    # 60d session analysis: 3 hour clusters bleed Kronos P&L regardless of day:
    #   - UTC 4-6 (ET 00-02): Tokyo→Europe overlap, 25-35% WR
    #   - UTC 16-17 (ET 12-13): London close transition, 44% WR, -$303
    #   - UTC 20-23 (ET 16-19): NY close/Sydney open, 33-57% WR, -$252
    # Default list covers all three. Can set to [] to disable bleed filter but
    # keep weekend edges.
    "kronos.hunter_session_bleed_hours_utc": {
        "value": [4, 5, 6, 16, 17, 20, 21, 22, 23],
        "default": [4, 5, 6, 16, 17, 20, 21, 22, 23],
        "description": "UTC hours where Kronos is skipped (session transitions / thin liquidity). Empty list disables.",
        "module": "kronos_hunter.py", "tier": 1,
    },
    # ── Kronos counter-momentum gate (2026-04-22) ──
    # 3-condition entry-quality filter from 7-day loss pattern analysis:
    #   C1 entry candle color confirms direction
    #   C2 prior 3-bar price extension WITH direction
    #   C3 stoch_k in direction zone AND turning further in direction
    # Losses: 64% scored 0/3. Wins: 75% scored ≥ 2/3.
    # 60-day backtest combining session+candle: -$1,378 → +$261 (+$1,639 swing).
    "kronos.hunter_counter_momentum_enabled": {
        "value": True, "default": True, "is_bool": True,
        "description": "Enable 3-indicator counter-momentum pre-entry gate for Kronos.",
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.hunter_counter_momentum_min_score": {
        "value": 2, "default": 2, "min": 1, "max": 3, "is_int": True,
        "description": "Min score (color+ext3+stoch = 0-3). Block Kronos if score < this.",
        "module": "kronos_hunter.py", "tier": 1,
    },
    # 2026-04-24: Added from trade-audit-repair session findings.
    # Grid-search optimal: [0.8, 1.1] window for confidence — kronos self-reported
    # conviction. 3-day backtest of 123 trades: baseline -216p, filtered to 16
    # kept trades at 81% WR / +21p.
    "kronos.hunter_min_signal_confidence": {
        "value": 0.8, "default": 0.8, "min": 0.0, "max": 2.0,
        "description": (
            "Min kronos confidence (|drift|/cone_pips) to fire. Below this, "
            "forecast lacks conviction. 3-day backtest: conf<0.8 has 48% WR; "
            "conf 0.8-1.0 has 85% WR. Set 0 to disable."
        ),
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.hunter_max_signal_confidence": {
        "value": 1.1, "default": 1.1, "min": 0.0, "max": 3.0,
        "description": (
            "Max kronos confidence. Above this, drift exceeds forecast cone "
            "width — mathematically inconsistent (tight predicted range but "
            "strong bias). 3-day backtest: conf>1.1 has 53% WR / -54p vs "
            "0.8-1.1 at 81% WR / +21p. Set 0 or ≥2 to disable cap."
        ),
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.hunter_max_drift_atr_ratio": {
        "value": 5.0, "default": 5.0, "min": 0.0, "max": 20.0,
        "description": (
            "Cap |drift_pips| / atr_pips. Above this, forecast is over-"
            "extrapolated (EUR-cross blind spot: avg 5.34 drift/ATR vs 1.96 "
            "non-EUR). Combined with conf [0.8, 1.1]: 92% WR / +27.5p vs 81% "
            "/ +21p without cap. Set 0 to disable."
        ),
        "module": "kronos_hunter.py", "tier": 1,
    },
    # 4-rule narrow filter thresholds (2026-04-23 filter, tunables added 2026-04-24)
    "kronos.hunter_knife_buy_stoch_max": {
        "value": 70.0, "default": 70.0, "min": 50.0, "max": 90.0,
        "description": "BUY blocked if stoch_k > this (late entry into overbought).",
        "module": "kronos_hunter.py", "tier": 2,
    },
    "kronos.hunter_knife_sell_stoch_min": {
        "value": 30.0, "default": 30.0, "min": 10.0, "max": 50.0,
        "description": "SELL blocked if stoch_k < this (late entry into oversold).",
        "module": "kronos_hunter.py", "tier": 2,
    },
    "kronos.hunter_candle_fighting_body_pct_min": {
        "value": 0.30, "default": 0.30, "min": 0.10, "max": 0.70,
        "description": "Candle-fighting rule active when body_pct > this.",
        "module": "kronos_hunter.py", "tier": 2,
    },
    "kronos.hunter_ultra_extended_atr_mult": {
        "value": 2.0, "default": 2.0, "min": 1.0, "max": 4.0,
        "description": "Block entry if |pos_e21_atr| > this (over-extended vs E21).",
        "module": "kronos_hunter.py", "tier": 2,
    },
    "kronos.hunter_ambiguous_body_pct_max": {
        "value": 0.10, "default": 0.10, "min": 0.02, "max": 0.25,
        "description": "Block ambiguous doji candles where body_pct < this.",
        "module": "kronos_hunter.py", "tier": 2,
    },
    "kronos.trigger_4rule_conservative": {
        "value": True, "default": True, "is_bool": True,
        "description": (
            "When True, block the snipe if the 4-rule trigger-time re-check "
            "can't run (missing candles or exception). Prevents silent "
            "bypass — trade 9990 EUR_JPY lost -15p at stoch=71.1 because the "
            "re-check silently skipped. Set False for permissive fallback."
        ),
        "module": "trading_cycle.py", "tier": 1,
    },
    "kronos.hunter_path_direction_override_enabled": {
        "value": False, "default": False, "is_bool": True,
        "description": (
            "Allow path_plan extraction to override early-bars direction. "
            "Disabled 2026-04-24: 36 path-override trades had 39% WR / -94p "
            "even at high confidence. Path-override also mismatches drift_pips "
            "(early-bars value kept) so signals become internally contradictory. "
            "Re-enable only if path-plan logic is rewritten to update drift too."
        ),
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.filter_min_confidence_to_reject": {
        "value": 0.7, "default": 0.7, "min": 0.5, "max": 0.95,
        "description": "Minimum Kronos confidence to veto a scout cycle (opposite direction)",
        "module": "kronos_filter.py", "tier": 1,
    },
    "kronos.model_name": {
        "value": "NeoQuasar/Kronos-base", "default": "NeoQuasar/Kronos-base",
        "min": "", "max": "",
        "description": "HuggingFace model id for Kronos-base",
        "module": "kronos_inference.py", "tier": 3, "is_int": False,
    },
    "kronos.tokenizer_path": {
        "value": "NeoQuasar/Kronos-Tokenizer-base",
        "default": "NeoQuasar/Kronos-Tokenizer-base",
        "min": None, "max": None,
        "description": "HuggingFace or local path for Kronos tokenizer (finetune changes this)",
        "module": "kronos_inference.py", "tier": 3, "is_int": False,
    },
    "kronos.use_indicator_columns": {
        "value": False, "default": False, "min": None, "max": None,
        "description": "When True, replace volume/amount with EMA sep + BB width for finetuned model",
        "module": "kronos_inference.py", "tier": 3, "is_int": False,
    },
    "kronos.lookback_bars": {
        "value": 400, "default": 400, "min": 64, "max": 512,
        "description": "M15 bars of history fed to Kronos (docs example=400, finetuning=512)",
        "module": "kronos_inference.py", "tier": 2, "is_int": True,
    },
    "kronos.pred_len_bars": {
        "value": 24, "default": 24, "min": 6, "max": 60,
        "description": "Forecast horizon in M15 bars",
        "module": "kronos_inference.py", "tier": 2, "is_int": True,
    },
    "kronos.sample_count": {
        "value": 5, "default": 5, "min": 1, "max": 20,
        "description": "Monte Carlo samples per forecast",
        "module": "kronos_inference.py", "tier": 2, "is_int": True,
    },

    # ── Kronos Integration — Guardian overrides (kronos.* namespace) ──────
    # Active for live_trades.source='kronos_hunter' via tc_get_for_trade().
    # Scout/snipe/manual trades unaffected.
    "kronos.gate.sl_atr_mult": {
        "value": 2.20, "default": 2.20, "min": 1.0, "max": 4.0,
        "description": "Kronos Hunter SL distance in ATR multiples",
        "module": "position_guardian.py", "tier": 1,
    },
    "kronos.gate.tp_atr_mult": {
        "value": 3.69, "default": 3.69, "min": 1.0, "max": 5.0,
        "description": "Kronos Hunter TP distance in ATR multiples (wider to let runners run)",
        "module": "position_guardian.py", "tier": 1,
    },
    # ── Kronos forecast-bounded SL/TP (2026-04-21) ──────────────────────
    # Hunter computes SL/TP as:
    #   sl_pips = max(atr_sl_min × ATR, min(forecast_sl_dist, atr_sl_max × ATR))
    #   tp_pips = max(atr_tp_min × ATR, min(forecast_tp_dist, atr_tp_max × ATR))
    "kronos.gate.atr_sl_min_mult": {
        "value": 1.5, "default": 1.5, "min": 0.5, "max": 3.0,
        "description": "Kronos Hunter SL floor — forecast_sl_dist clamped to at least this × ATR",
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.gate.atr_sl_max_mult": {
        "value": 3.0, "default": 3.0, "min": 1.0, "max": 5.0,
        "description": "Kronos Hunter SL cap — forecast_sl_dist clamped to at most this × ATR (tail-loss control)",
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.gate.atr_tp_min_mult": {
        "value": 1.5, "default": 1.5, "min": 0.5, "max": 3.0,
        "description": "Kronos Hunter TP floor — forecast_tp_dist clamped to at least this × ATR",
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.gate.atr_tp_max_mult": {
        "value": 5.0, "default": 5.0, "min": 1.0, "max": 8.0,
        "description": "Kronos Hunter TP cap — forecast_tp_dist clamped to at most this × ATR",
        "module": "kronos_hunter.py", "tier": 1,
    },
    "kronos.guardian.profit_floor_5p": {
        "value": 0.55, "default": 0.55, "min": 0.15, "max": 0.8,
        "description": "Kronos lock ratio at peak >= 5p (looser early floor)",
        "module": "position_guardian.py", "tier": 1,
    },
    "kronos.guardian.profit_floor_8p": {
        "value": 0.93, "default": 0.93, "min": 0.3, "max": 0.95,
        "description": "Kronos lock ratio at peak >= 8p",
        "module": "position_guardian.py", "tier": 1,
    },
    "kronos.guardian.profit_floor_12p": {
        "value": 0.93, "default": 0.93, "min": 0.4, "max": 0.98,
        "description": "Kronos lock ratio at peak >= 12p",
        "module": "position_guardian.py", "tier": 1,
    },
    "kronos.guardian.profit_floor_20p": {
        "value": 0.82, "default": 0.82, "min": 0.5, "max": 0.98,
        "description": "Kronos lock ratio at peak >= 20p (looser — let big moves breathe)",
        "module": "position_guardian.py", "tier": 1,
    },
    "kronos.guardian.ratchet_step_pips": {
        "value": 1.43, "default": 1.43, "min": 0.5, "max": 5.0,
        "description": "Kronos profit-floor ratchet step (smaller = smoother)",
        "module": "position_guardian.py", "tier": 1,
    },
    "kronos.guardian.trailing_activation_rr": {
        "value": 0.13, "default": 0.13, "min": 0.1, "max": 0.5,
        "description": "Kronos trailing stop activation (fraction of SL distance)",
        "module": "position_guardian.py", "tier": 1,
    },
    "kronos.guardian.trailing_atr_mult": {
        "value": 0.28, "default": 0.28, "min": 0.2, "max": 1.5,
        "description": "Kronos trailing distance as ATR multiple",
        "module": "position_guardian.py", "tier": 1,
    },
    "kronos.guardian.sl_buffer_pips": {
        "value": 8, "default": 8, "min": 1, "max": 15,
        "description": "Kronos SL buffer in pips (wider — reduces wick blowouts)",
        "module": "position_guardian.py", "tier": 1, "is_int": True,
    },
    # ── Kronos Threat Scorer (kronos_threat.py) ──────────────────────────
    # Tuned from indicator_profile_{backtest,live}.csv — 2,834 + 22 trades.
    # Scout's score_threat reads parallel-stable EMAs (Kronos's ideal) as
    # "fan collapsing" and kills winners. These tune how the Kronos-specific
    # scorer reads the SAME chart through Kronos's lens.
    "kronos.threat.fan_flipped_score": {
        "value": 45, "default": 45, "min": 20, "max": 70,
        "description": "Threat points when EMA fan direction flips against trade (strongest loss separator, live Δmed=-1.00)",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.fan_mixed_score": {
        "value": 20, "default": 20, "min": 5, "max": 40,
        "description": "Threat points when fan goes mixed (losing alignment, intermediate state)",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.e100_break_pips": {
        "value": 2.0, "default": 2.0, "min": 0.5, "max": 8.0,
        "description": "Pips through E100 against trade direction before scoring a break (loss min_dist_e100=-4.3p)",
        "module": "kronos_threat.py", "tier": 1,
    },
    "kronos.threat.e100_break_base_score": {
        "value": 10, "default": 10, "min": 0, "max": 30,
        "description": "Base threat points when price breaks E100 against trade",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.e100_break_pips_mult": {
        "value": 1.0, "default": 1.0, "min": 0.0, "max": 3.0,
        "description": "Additional threat points per pip through E100 (scales penalty with depth)",
        "module": "kronos_threat.py", "tier": 1,
    },
    "kronos.threat.e55_break_pips": {
        "value": 3.0, "default": 3.0, "min": 1.0, "max": 10.0,
        "description": "Pips below E55 in direction before scoring a break (win E55=+5p, loss=-1.5p)",
        "module": "kronos_threat.py", "tier": 1,
    },
    "kronos.threat.e55_break_score": {
        "value": 10, "default": 10, "min": 0, "max": 25,
        "description": "Threat points when price drops below E55 in direction",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.sep_contract_score": {
        "value": 12, "default": 12, "min": 0, "max": 30,
        "description": "Threat points when separation velocity contracts against trade",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.sep_contract_threshold": {
        "value": -0.3, "default": -0.3, "min": -2.0, "max": 0.0,
        "description": "Signed separation velocity threshold below which fan is contracting against trade",
        "module": "kronos_threat.py", "tier": 1,
    },
    "kronos.threat.bb_compression_atr": {
        "value": 1.8, "default": 1.8, "min": 1.0, "max": 3.0,
        "description": "BB width / ATR threshold below which BB is compressed (only penalized when losing)",
        "module": "kronos_threat.py", "tier": 1,
    },
    "kronos.threat.bb_compression_score": {
        "value": 8, "default": 8, "min": 0, "max": 20,
        "description": "Threat points when BB compressed AND trade losing",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.winner_cap_score": {
        "value": 30, "default": 30, "min": 10, "max": 50,
        "description": "Score cap when fan aligned + holding E55 + in profit (don't kill winners)",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.winner_e55_min_pips": {
        "value": -2.0, "default": -2.0, "min": -10.0, "max": 5.0,
        "description": "Min dist-to-E55 (signed by direction) for winner-profile cap to apply",
        "module": "kronos_threat.py", "tier": 1,
    },
    "kronos.threat.fresh_trade_cap_score": {
        "value": 40, "default": 40, "min": 20, "max": 70,
        "description": "Score cap for first N candles when fan aligned (be patient on fresh entries)",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.fresh_trade_bars": {
        "value": 3, "default": 3, "min": 1, "max": 10,
        "description": "Number of candles trade is considered 'fresh' for patience cap",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.black_threshold": {
        "value": 85, "default": 85, "min": 70, "max": 100,
        "description": "Threat score ≥ this = BLACK zone (auto-close eligible)",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.red_threshold": {
        "value": 65, "default": 65, "min": 50, "max": 85,
        "description": "Threat score ≥ this = RED zone",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.threat.yellow_threshold": {
        "value": 40, "default": 40, "min": 25, "max": 65,
        "description": "Threat score ≥ this = YELLOW zone",
        "module": "kronos_threat.py", "tier": 1, "is_int": True,
    },
    "kronos.guardian.threat_black_close_enabled": {
        "value": False, "default": False, "is_bool": True,
        "description": "When True, kronos_threat BLACK zone closes trade. "
                       "When False (default post-2026-04-21), logs would-be-close "
                       "but lets mechanical SL/TP/trailing manage the trade. "
                       "Disabled pending threat scorer rewrite per Tim 2026-04-20 directive.",
        "module": "position_guardian.py", "tier": 1,
    },
    "kronos.guardian.shadow_logging_enabled": {
        "value": True, "default": True, "is_bool": True,
        "description": "Write per-tick Kronos threat scores to kronos_shadow_scores table. "
                       "Set False to disable shadow logging without touching close behavior.",
        "module": "position_guardian.py", "tier": 2,
    },
    "kronos.guardian.auto_rollback_enabled": {
        "value": True, "default": True, "is_bool": True,
        "description": "Master switch for Kronos performance tripwire daemon",
        "module": "kronos_rollback_tripwire.py", "tier": 1,
    },
    "kronos.guardian.auto_rollback_pnl_threshold": {
        "value": -50, "default": -50, "min": -200, "max": -10,
        "description": "If rolling 4h Kronos PnL ≤ this, auto-disable kronos.enabled",
        "module": "kronos_rollback_tripwire.py", "tier": 1, "is_int": True,
    },
    "kronos.guardian.auto_rollback_window_hours": {
        "value": 4, "default": 4, "min": 1, "max": 24,
        "description": "Rolling window size (hours) for tripwire PnL calc",
        "module": "kronos_rollback_tripwire.py", "tier": 1, "is_int": True,
    },

    # ── Snipe-specific tight trailing (2026-04-21) ──────────────────────
    # Autopsy on 60 days of snipe trades showed tight trailing saves +670p
    # (535p loss reduction + 135p win increase). Snipes previously read from
    # risk_config.json (trailing_stop_*: 2.0) — way too loose. These params
    # resolve via tc_get_for_trade("guardian.<param>", "snipe_direct") which
    # now checks snipe.* namespace first. Scout/manual unchanged.
    "snipe.guardian.trailing_activation_rr": {
        "value": 0.15, "default": 0.15, "min": 0.05, "max": 1.0,
        "description": "Snipe BE-move/trail activation: peak ≥ this × SL triggers protection",
        "module": "position_guardian.py", "tier": 1,
    },
    "snipe.guardian.trailing_atr_mult": {
        "value": 0.1, "default": 0.1, "min": 0.05, "max": 1.0,
        "description": "Snipe trailing distance as ATR multiple (0.1 = 1p on 10p ATR pair)",
        "module": "position_guardian.py", "tier": 1,
    },
    # ── min_gap floor — controls "breathing room" between trail SL and current price ──
    # The default 1.0 ATR was hardcoded in position_guardian.py prior to 2026-04-22,
    # which NEUTRALIZED the snipe tight trail (0.1 ATR). Now tunable. Snipes override
    # to 0.3 so the tight trail can actually activate. 14-day backtest showed
    # 1.0 → -149.7p vs 0.3 → -32.5p (+117p improvement, WR 58.5% → 69.2%).
    "guardian.sl_min_gap_atr_mult": {
        "value": 1.0, "default": 1.0, "min": 0.1, "max": 2.0,
        "description": "Min SL distance from current price as ATR multiple. Prevents noise stops.",
        "module": "position_guardian.py", "tier": 1,
    },
    "snipe.guardian.sl_min_gap_atr_mult": {
        "value": 0.3, "default": 0.3, "min": 0.1, "max": 1.0,
        "description": "Snipe-specific min_gap (default 0.3 × ATR = ~2.4p on 8p ATR).",
        "module": "position_guardian.py", "tier": 1,
    },

    # ── snipe counter-momentum sanity gate (2026-04-22) ──
    # The sanity check for "snipe going into oversold or retracing late entry."
    # Multi-indicator pre-entry score identified from 28 never-positive snipe losses
    # over 60 days. Every never-pos loss shared this profile:
    #   seller enters during a 3-bar counter-rally, on a green candle, near E21 retest,
    #   with stoch mid-range, in BB compression.
    # 5 conditions (SELL; flipped for BUY):
    #   C1 entry candle RED, C2 prior 3-bar price dropped,
    #   C3 stoch_k ≤ 45 AND falling, C4 BB width expanding,
    #   C5 price ≤ 5 pips below E21 (extended, not at retest).
    # Block if score < min_score. 60-day backtest on 141 validator snipes:
    # net +534p saved (blocks 23/29 never-positive losses, costs 10 small wins +43p).
    # FULL-STACK (zombie retire + EUR tail + cm≥2/5):
    #   60d: -$1,899 actual → +$596 sim  (+$2,495 swing)
    #   14d:   -$828 actual → +$722 sim  (+$1,550 swing)
    # Kronos-path snipes SKIP this gate (they use forecast-path thesis, not
    # scout sniper scoring). Supersedes intent of disabled 4/09 gates:
    # momentum_trap, hard_oscillator_exhaustion, against_momentum,
    # oscillator_direction, bb_width — which were disabled as individual gates
    # but the composite signal here is what actually discriminates.
    "snipe.gate.counter_momentum_enabled": {
        "value": True, "default": True,
        "description": "Enable counter-momentum pre-entry gate for validator-origin snipes. Kronos-path snipes bypass.",
        "module": "trading_cycle.py", "tier": 1,
    },
    "snipe.gate.counter_momentum_min_score": {
        "value": 2, "default": 2, "min": 1, "max": 5,
        "description": "Min conditions aligned (1-5). Block snipe if composite score < this threshold.",
        "module": "trading_cycle.py", "tier": 1,
    },

    # ── Ghost Validator — 35B distilled model running alongside Opus (2026-04-22) ──
    # Approach I: Opus-style holistic reasoning + narrative contradiction flag.
    # Benchmark: 75% overall (TRADE_NOW 85%, SKIP 60%, WATCH 50%).
    # Runs as background thread — does NOT affect live trade decisions.
    # Logs to ghost_verdicts table for comparison tracking.
    "ghost.enabled": {
        "value": True, "default": True, "min": False, "max": True,
        "description": "Enable ghost validator (35B parallel to Opus). Ghost verdicts logged but never used for trade decisions.",
        "module": "agents/trading_cycle.py", "tier": 1,
    },
    "ghost.model_name": {
        "value": "mlx-community/Qwen3.5-35B-A3B-4bit", "default": "mlx-community/Qwen3.5-35B-A3B-4bit",
        "min": "", "max": "",
        "description": "HuggingFace model ID for ghost validator.",
        "module": "optimizer/ghost_replay.py", "tier": 2,
    },
    "ghost.adapter_path": {
        "value": "~/Jarvis/models/adapters/35b_mlx", "default": "~/Jarvis/models/adapters/35b_mlx",
        "min": "", "max": "",
        "description": "LoRA adapter path for ghost model. 35b_mlx = v2 distillation (24,711 entries).",
        "module": "optimizer/ghost_replay.py", "tier": 2,
    },
    "ghost.prompt_path": {
        "value": "~/Jarvis/Forex Trading Team/Prompts/ghost_validator_v1.md",
        "default": "~/Jarvis/Forex Trading Team/Prompts/ghost_validator_v1.md",
        "min": "", "max": "",
        "description": "System prompt file for ghost validator. v1 = Opus-style holistic + narrative flag.",
        "module": "agents/trading_cycle.py", "tier": 1,
    },
    "ghost.temperature": {
        "value": 0.7, "default": 0.7, "min": 0.1, "max": 1.5,
        "description": "Sampling temperature. Qwen3.5 needs 0.7 (temp=0 causes infinite loops).",
        "module": "optimizer/ghost_replay.py", "tier": 2,
    },
    "ghost.max_tokens": {
        "value": 4096, "default": 4096, "min": 512, "max": 8192,
        "description": "Max generation tokens for ghost validator response.",
        "module": "optimizer/ghost_replay.py", "tier": 2,
    },
    "ghost.narrative_flag_enabled": {
        "value": True, "default": True, "min": False, "max": True,
        "description": "Add warning when narrative contradicts fan_state (stalled/flat/mixed vs expanding).",
        "module": "agents/trading_cycle.py", "tier": 1,
    },
    "ghost.mode": {
        "value": "batch", "default": "batch", "min": "", "max": "",
        "description": "Ghost mode: 'batch' = nightly replay after trading (default, no memory conflict), 'realtime' = background thread during live trading (needs 35B running).",
        "module": "agents/trading_cycle.py", "tier": 1,
    },
}


def get(param: str, fallback=None):
    """Get current value of a tuning parameter."""
    p = TUNING.get(param)
    if p is None:
        return fallback
    return p["value"]


# Alias used by position_guardian.py and other callers:
#   from tuning_config import get as tc_get
# Exposing tc_get directly so tests and new callers can import it by name.
tc_get = get


def tc_get_for_trade(param: str, source: Optional[str] = None, fallback: Any = None) -> Any:
    """Resolve a TUNING param with source-aware override.

    - source='kronos_hunter' → checks 'kronos.<param>' first
    - source='snipe_direct'  → checks 'snipe.<param>' first (2026-04-21)
    - all others fall through to the global tc_get(param, fallback)

    Keeps scout/manual watchers reading the exact same global TUNING they
    always have — zero risk to their existing trades.
    """
    if source == "kronos_hunter":
        entry = TUNING.get(f"kronos.{param}")
        if entry is not None:
            return entry["value"]
    elif source == "snipe_direct":
        entry = TUNING.get(f"snipe.{param}")
        if entry is not None:
            return entry["value"]
    return tc_get(param, fallback)


def get_all() -> Dict[str, Any]:
    """Get all parameters with current values."""
    return {k: v["value"] for k, v in TUNING.items()}


def get_optimizable_params() -> Dict[str, Dict]:
    """Return params that can be optimized with their ranges.
    Only returns params with numeric min/max ranges (excludes dicts, bools, etc).
    """
    result = {}
    for name, p in TUNING.items():
        if p.get("min") is not None and p.get("max") is not None:
            if isinstance(p["min"], (int, float)) and isinstance(p["max"], (int, float)):
                result[name] = {
                    "value": p["value"],
                    "default": p["default"],
                    "min": p["min"],
                    "max": p["max"],
                    "description": p.get("description", ""),
                    "module": p.get("module", ""),
                    "tier": p.get("tier", 0),
                    "is_int": isinstance(p["value"], int) and isinstance(p["default"], int),
                }
    return result


def _load_overrides():
    """Load any saved overrides from DB on startup."""
    try:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT param, value FROM tuning_overrides WHERE active = 1
        """).fetchall()
        for r in rows:
            param = r["param"]
            if param in TUNING:
                try:
                    val = json.loads(r["value"])
                    TUNING[param]["value"] = val
                    logger.info("Loaded tuning override: %s = %s", param, val)
                except (json.JSONDecodeError, TypeError):
                    pass
    except sqlite3.OperationalError:
        pass  # Table doesn't exist yet — first run


def _ensure_tables():
    conn = get_trading_forex()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tuning_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            param TEXT NOT NULL,
            value TEXT NOT NULL,
            previous_value TEXT,
            reason TEXT,
            audit_report_id INTEGER,
            backtest_result TEXT,
            approved_by TEXT,
            approved_at TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(param, active, created_at)
        );

        CREATE TABLE IF NOT EXISTS tuning_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            param TEXT NOT NULL,
            current_value TEXT NOT NULL,
            proposed_value TEXT NOT NULL,
            reason TEXT,
            audit_report_id INTEGER,
            
            -- Backtest results
            backtest_status TEXT DEFAULT 'pending',
            backtest_before TEXT,
            backtest_after TEXT,
            backtest_improvement TEXT,
            backtest_ran_at TEXT,
            
            -- Approval
            status TEXT DEFAULT 'pending',
            approved_by TEXT,
            approved_at TEXT,
            rejected_reason TEXT,
            
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_proposals_status ON tuning_proposals(status);
        CREATE INDEX IF NOT EXISTS idx_overrides_active ON tuning_overrides(active);

        CREATE TABLE IF NOT EXISTS tuning_performance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            override_id INTEGER NOT NULL,
            param TEXT NOT NULL,
            value TEXT NOT NULL,
            previous_value TEXT,
            window_label TEXT NOT NULL,
            measured_at TEXT NOT NULL,
            before_total INTEGER DEFAULT 0,
            before_wins INTEGER DEFAULT 0,
            before_win_rate REAL DEFAULT 0,
            before_avg_pips REAL DEFAULT 0,
            before_total_pnl REAL DEFAULT 0,
            before_sl_hits INTEGER DEFAULT 0,
            after_total INTEGER DEFAULT 0,
            after_wins INTEGER DEFAULT 0,
            after_win_rate REAL DEFAULT 0,
            after_avg_pips REAL DEFAULT 0,
            after_total_pnl REAL DEFAULT 0,
            after_sl_hits INTEGER DEFAULT 0,
            win_rate_delta REAL DEFAULT 0,
            avg_pips_delta REAL DEFAULT 0,
            pnl_delta REAL DEFAULT 0,
            verdict TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(override_id, window_label)
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_param ON tuning_performance_snapshots(param);
        CREATE INDEX IF NOT EXISTS idx_snapshots_verdict ON tuning_performance_snapshots(verdict);
    """)
        conn.commit()
    except Exception:
        raise


def propose_change(
    param: str,
    proposed_value: Any,
    reason: str,
    audit_report_id: int = None,
) -> int:
    """Create a tuning proposal. Returns proposal ID."""
    if param not in TUNING:
        raise ValueError(f"Unknown parameter: {param}")

    current = TUNING[param]["value"]
    p = TUNING[param]
    
    # Validate range
    if p.get("min") is not None and isinstance(proposed_value, (int, float)):
        if proposed_value < p["min"] or proposed_value > p["max"]:
            raise ValueError(
                f"{param}: {proposed_value} out of range [{p['min']}, {p['max']}]")

    conn = get_trading_forex()
    cursor = conn.execute("""
        INSERT INTO tuning_proposals (
            param, current_value, proposed_value, reason, audit_report_id
        ) VALUES (?, ?, ?, ?, ?)
    """, (
        param, json.dumps(current), json.dumps(proposed_value),
        reason, audit_report_id,
    ))
    proposal_id = cursor.lastrowid
    conn.commit()

    logger.info("Tuning proposal #%d: %s %s → %s (%s)",
                proposal_id, param, current, proposed_value, reason)
    return proposal_id


def backtest_proposal(proposal_id: int) -> Dict[str, Any]:
    """Run backtest comparing current vs proposed value.
    
    Returns backtest results with before/after metrics.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    proposal = conn.execute(
        "SELECT * FROM tuning_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()

    if not proposal:
        return {"error": f"Proposal {proposal_id} not found"}

    param = proposal["param"]
    current_val = json.loads(proposal["current_value"])
    proposed_val = json.loads(proposal["proposed_value"])

    logger.info("Backtesting proposal #%d: %s %s → %s", proposal_id, param, current_val, proposed_val)

    # Run backtest with CURRENT value
    before_results = _run_backtest_with_value(param, current_val)

    # Run backtest with PROPOSED value
    after_results = _run_backtest_with_value(param, proposed_val)

    # Calculate improvement
    improvement = _calculate_improvement(before_results, after_results)

    # Store results
    conn = get_trading_forex()
    conn.execute("""
        UPDATE tuning_proposals SET
            backtest_status = 'complete',
            backtest_before = ?,
            backtest_after = ?,
            backtest_improvement = ?,
            backtest_ran_at = ?
        WHERE id = ?
    """, (
        json.dumps(before_results, default=str),
        json.dumps(after_results, default=str),
        json.dumps(improvement, default=str),
        datetime.now(timezone.utc).isoformat(),
        proposal_id,
    ))
    conn.commit()

    return {
        "proposal_id": proposal_id,
        "param": param,
        "current": current_val,
        "proposed": proposed_val,
        "before": before_results,
        "after": after_results,
        "improvement": improvement,
    }


def _run_backtest_with_value(param: str, value: Any) -> Dict[str, Any]:
    """Simulate a parameter change against ALL real trades in live_trades.

    For gate/filter params: counts how many real trades would have been
    BLOCKED by the new value. Reports wins lost vs losses avoided.
    For threshold params: filters trades that match the condition and
    reports performance of that subset.

    Uses live_trades (real trades) — not synthetic backtest data.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row

    results = {
        "total_trades": 0, "win_rate": 0, "profit_factor": 0,
        "total_pips": 0, "avg_pips_per_trade": 0, "total_usd": 0,
        "blocked_wins": 0, "blocked_losses": 0, "blocked_usd": 0,
    }

    # Get all closed trades with entry data
    all_trades = conn.execute("""
        SELECT pnl_pips, pnl_usd, result, bb_width, rsi, stoch_k, stoch_d,
               fan_state, story_score, source, pair, direction, confluence_score
        FROM live_trades
        WHERE exit_price IS NOT NULL AND exit_time IS NOT NULL
    """).fetchall()

    if not all_trades:
        return results

    def _bb_width_to_pips(raw_width, pair):
        """Convert stored bb_width (price difference) to pips."""
        if raw_width is None:
            return None
        pip_size = 0.01 if "JPY" in (pair or "") else 0.0001
        return raw_width / pip_size

    # Determine which trades PASS the filter at this value
    passing = []
    blocked = []

    for t in all_trades:
        would_pass = True

        if param == "gate.bb_width_min_pips":
            bb_pips = _bb_width_to_pips(t["bb_width"], t["pair"])
            if bb_pips is not None and bb_pips < value:
                would_pass = False

        elif param == "gate.stoch_dont_buy_above":
            if t["direction"] == "buy" and t["stoch_k"] is not None and t["stoch_k"] > value:
                would_pass = False

        elif param == "gate.stoch_dont_sell_below":
            if t["direction"] == "sell" and t["stoch_k"] is not None and t["stoch_k"] < value:
                would_pass = False

        elif param == "gate.story_score_min":
            ss = t["story_score"]
            if ss is not None and ss < value:
                would_pass = False

        elif param == "gate.min_rr_ratio":
            # Can't retroactively test R:R — report all trades as baseline
            pass

        elif param == "gate.cooldown_hours":
            # Can't retroactively test timing — report all trades as baseline
            pass

        elif param.startswith("gate.sl_atr_mult") or param.startswith("gate.tp_atr_mult"):
            # SL/TP changes affect outcome, not entry filtering
            # Report all trades as baseline — real impact measured by snapshots
            pass

        elif param.startswith("scout."):
            # Scout params affect which alerts fire — can't retroactively filter
            pass

        elif param.startswith("guardian."):
            # Guardian params affect exit behavior — can't retroactively filter
            pass

        else:
            # Unknown param type — report all trades as baseline
            pass

        if would_pass:
            passing.append(t)
        else:
            blocked.append(t)

    # Compute results for passing trades
    if passing:
        wins = sum(1 for t in passing if (t["pnl_usd"] or 0) > 0)
        total_pips = sum(t["pnl_pips"] or 0 for t in passing)
        total_usd = sum(t["pnl_usd"] or 0 for t in passing)
        win_pips = sum(t["pnl_pips"] or 0 for t in passing if (t["pnl_usd"] or 0) > 0)
        loss_pips = abs(sum(t["pnl_pips"] or 0 for t in passing if (t["pnl_usd"] or 0) <= 0))

        results["total_trades"] = len(passing)
        results["win_rate"] = round(wins / len(passing) * 100, 1)
        results["profit_factor"] = round(win_pips / loss_pips, 2) if loss_pips > 0 else 999
        results["total_pips"] = round(total_pips, 1)
        results["total_usd"] = round(total_usd, 2)
        results["avg_pips_per_trade"] = round(total_pips / len(passing), 2)

    # Report what got blocked
    if blocked:
        blocked_wins = sum(1 for t in blocked if (t["pnl_usd"] or 0) > 0)
        blocked_losses = sum(1 for t in blocked if (t["pnl_usd"] or 0) <= 0)
        blocked_usd = sum(t["pnl_usd"] or 0 for t in blocked)
        results["blocked_wins"] = blocked_wins
        results["blocked_losses"] = blocked_losses
        results["blocked_usd"] = round(blocked_usd, 2)

    return results


def _calculate_improvement(before: Dict, after: Dict) -> Dict:
    """Calculate improvement metrics."""
    imp = {}
    for key in ["win_rate", "profit_factor", "total_pips", "avg_pips_per_trade"]:
        b = before.get(key, 0)
        a = after.get(key, 0)
        imp[key] = {
            "before": b,
            "after": a,
            "delta": a - b,
            "pct_change": ((a - b) / b * 100) if b != 0 else 0,
        }
    # Overall: is this an improvement?
    wr_better = imp["win_rate"]["delta"] >= 0
    pf_better = imp["profit_factor"]["delta"] >= 0
    pips_better = imp["avg_pips_per_trade"]["delta"] >= 0
    imp["verdict"] = "improvement" if (wr_better and pf_better) else \
                     "mixed" if (wr_better or pf_better) else "regression"
    imp["safe_to_apply"] = imp["verdict"] in ("improvement",) and \
                           imp["win_rate"]["delta"] >= -2  # Allow tiny WR drop if PF improves
    return imp


def approve_proposal(proposal_id: int, approved_by: str = "Tim") -> Dict:
    """Approve and apply a tuning proposal."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row

    proposal = conn.execute(
        "SELECT * FROM tuning_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()

    if not proposal:
        return {"error": f"Proposal {proposal_id} not found"}

    if proposal["backtest_status"] != "complete":
        return {"error": "Proposal must be backtested before approval"}

    param = proposal["param"]
    proposed_val = json.loads(proposal["proposed_value"])
    current_val = json.loads(proposal["current_value"])

    # Deactivate any existing override for this param
    conn.execute(
        "UPDATE tuning_overrides SET active = 0 WHERE param = ? AND active = 1",
        (param,)
    )

    # Create new active override
    conn.execute("""
        INSERT INTO tuning_overrides (
            param, value, previous_value, reason, audit_report_id,
            backtest_result, approved_by, approved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        param, json.dumps(proposed_val), json.dumps(current_val),
        proposal["reason"], proposal["audit_report_id"],
        proposal["backtest_after"], approved_by,
        datetime.now(timezone.utc).isoformat(),
    ))

    # Update proposal status
    conn.execute("""
        UPDATE tuning_proposals SET status = 'approved', approved_by = ?, approved_at = ?
        WHERE id = ?
    """, (approved_by, datetime.now(timezone.utc).isoformat(), proposal_id))

    conn.commit()

    # Apply to in-memory config
    TUNING[param]["value"] = proposed_val
    logger.info("APPROVED & APPLIED: %s = %s (was %s) by %s",
                param, proposed_val, current_val, approved_by)

    return {
        "status": "approved",
        "param": param,
        "old_value": current_val,
        "new_value": proposed_val,
        "approved_by": approved_by,
    }


def reject_proposal(proposal_id: int, reason: str = "") -> Dict:
    """Reject a tuning proposal."""
    conn = get_trading_forex()
    conn.execute("""
        UPDATE tuning_proposals SET status = 'rejected', rejected_reason = ?
        WHERE id = ?
    """, (reason, proposal_id))
    conn.commit()
    return {"status": "rejected", "proposal_id": proposal_id}


def get_pending_proposals() -> List[Dict]:
    """Get all pending proposals awaiting approval."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT * FROM tuning_proposals WHERE status = 'pending'
        ORDER BY created_at DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_proposal(proposal_id: int) -> Optional[Dict]:
    """Get a specific proposal with all details."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM tuning_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row:
        r = dict(row)
        for field in ("backtest_before", "backtest_after", "backtest_improvement"):
            if r.get(field):
                try:
                    r[field] = json.loads(r[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return r
    return None


def revert_override(param: str) -> Dict:
    """Revert a parameter to its default value."""
    if param not in TUNING:
        return {"error": f"Unknown parameter: {param}"}

    default = TUNING[param]["default"]
    current = TUNING[param]["value"]

    conn = get_trading_forex()
    conn.execute(
        "UPDATE tuning_overrides SET active = 0 WHERE param = ? AND active = 1",
        (param,)
    )
    conn.commit()

    TUNING[param]["value"] = default
    logger.info("REVERTED: %s = %s (was %s)", param, default, current)
    return {"param": param, "reverted_to": default, "was": current}


def measure_change_impact(override_id: int = None) -> List[Dict]:
    """Measure tuning change impact at multiple time windows (24h/48h/7d/14d).

    For each active tuning override, computes trade performance metrics before
    vs after the change. Stores results in tuning_performance_snapshots.
    Called every 6 hours by scheduler to continuously track effectiveness.

    Returns list of new snapshot results.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row

    if override_id:
        overrides = conn.execute(
            "SELECT * FROM tuning_overrides WHERE id = ?", (override_id,)
        ).fetchall()
    else:
        overrides = conn.execute(
            "SELECT * FROM tuning_overrides WHERE active = 1 ORDER BY created_at DESC"
        ).fetchall()

    windows = {"24h": 24, "48h": 48, "7d": 168, "14d": 336}
    results = []
    now = datetime.now(timezone.utc)

    for ov in overrides:
        ov_id = ov["id"]
        param = ov["param"]
        ts = ov["created_at"]
        if not ts:
            continue

        # Source filter based on param type
        if "manual" in param:
            source_filter = "source = 'manual'"
        elif any(k in param for k in (
            "snipe", "guardian", "gate.", "watch.", "scout.", "validator.", "ghost."
        )):
            source_filter = "source IN ('snipe_direct','scout','snipe')"
        else:
            source_filter = "1=1"

        # Before metrics (48h before change — computed once, same for all windows)
        before = conn.execute(f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(AVG(pnl_pips), 2) as avg_pips,
                   ROUND(SUM(pnl_usd), 2) as total_pnl,
                   SUM(CASE WHEN exit_method = 'reconcile_inline' THEN 1 ELSE 0 END) as sl_hits
            FROM live_trades
            WHERE {source_filter}
              AND exit_price IS NOT NULL
              AND entry_time BETWEEN datetime(?, '-48 hours') AND datetime(?)
        """, (ts, ts)).fetchone()

        before_total = before["total"] or 0
        before_wins = before["wins"] or 0
        before_wr = (before_wins / before_total * 100) if before_total > 0 else 0

        for label, hours in windows.items():
            # Check if window has elapsed
            try:
                change_time = datetime.fromisoformat(
                    ts.replace("Z", "+00:00") if "Z" in ts else ts
                )
                if change_time.tzinfo is None:
                    change_time = change_time.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            elapsed_hours = (now - change_time).total_seconds() / 3600
            if elapsed_hours < hours:
                continue

            # Skip if already measured
            existing = conn.execute(
                "SELECT id FROM tuning_performance_snapshots "
                "WHERE override_id = ? AND window_label = ?",
                (ov_id, label)
            ).fetchone()
            if existing:
                continue

            # After metrics (from change to change+window hours)
            after = conn.execute(f"""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                       ROUND(AVG(pnl_pips), 2) as avg_pips,
                       ROUND(SUM(pnl_usd), 2) as total_pnl,
                       SUM(CASE WHEN exit_method = 'reconcile_inline' THEN 1 ELSE 0 END) as sl_hits
                FROM live_trades
                WHERE {source_filter}
                  AND exit_price IS NOT NULL
                  AND entry_time BETWEEN datetime(?) AND datetime(?, '+{hours} hours')
            """, (ts, ts)).fetchone()

            after_total = after["total"] or 0
            after_wins = after["wins"] or 0
            after_wr = (after_wins / after_total * 100) if after_total > 0 else 0

            wr_delta = after_wr - before_wr
            avg_pips_delta = (after["avg_pips"] or 0) - (before["avg_pips"] or 0)
            pnl_delta = (after["total_pnl"] or 0) - (before["total_pnl"] or 0)

            if after_total < 5:
                verdict = "pending"
            elif wr_delta > 3:
                verdict = "positive"
            elif wr_delta < -3:
                verdict = "negative"
            else:
                verdict = "neutral"

            conn.execute("""
                INSERT OR REPLACE INTO tuning_performance_snapshots (
                    override_id, param, value, previous_value, window_label,
                    measured_at,
                    before_total, before_wins, before_win_rate, before_avg_pips,
                    before_total_pnl, before_sl_hits,
                    after_total, after_wins, after_win_rate, after_avg_pips,
                    after_total_pnl, after_sl_hits,
                    win_rate_delta, avg_pips_delta, pnl_delta, verdict
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?)
            """, (
                ov_id, param, ov["value"], ov["previous_value"], label,
                now.isoformat(),
                before_total, before_wins, round(before_wr, 1),
                before["avg_pips"] or 0, before["total_pnl"] or 0,
                before["sl_hits"] or 0,
                after_total, after_wins, round(after_wr, 1),
                after["avg_pips"] or 0, after["total_pnl"] or 0,
                after["sl_hits"] or 0,
                round(wr_delta, 1), round(avg_pips_delta, 2),
                round(pnl_delta, 2), verdict,
            ))

            results.append({
                "override_id": ov_id, "param": param, "window": label,
                "verdict": verdict, "wr_delta": round(wr_delta, 1),
            })

            logger.info(
                "Snapshot %s/%s: WR %+.1f%% (%s)", param, label, wr_delta,
                verdict,
            )

    conn.commit()
    return results


# Initialize on import
_ensure_tables()
_load_overrides()
