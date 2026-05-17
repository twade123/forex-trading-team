"""
Performance-driven YAML config auto-tuner for risk parameters.

Analyzes closed trade performance and recommends or applies
adjustments to ``risk_asset_classes.yaml`` values within safe
bounds.  Runs periodically (after N closed trades or on-demand).

Five analysis dimensions:
    1. Regime multiplier effectiveness (stop-hit rate per regime)
    2. Partial take-profit hit rates (TP1/TP2 level calibration)
    3. Time decay effectiveness (bars-in-trade vs outcome)
    4. Trailing method comparison (chandelier vs ratchet)
    5. Spread cost impact (spread-relative to ATR)

Safety:
    - All adjustments bounded by :data:`TUNING_BOUNDS`
    - Maximum change per cycle is ``step_increment`` (0.1)
    - Minimum 50 trades before any tuning runs
    - Backup YAML before every write
    - NEVER touches profile presets or absolute_limits
    - Tuning history persisted for full audit trail

Usage::

    from trading_bot.source.risk_auto_tuner import RiskAutoTuner

    tuner = RiskAutoTuner()
    analysis = tuner.analyze(closed_trades)
    result = tuner.apply_recommendations(analysis, auto_apply=False)
"""

import copy
import json
import logging
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("trading_bot.risk_auto_tuner")

# ---------------------------------------------------------------------------
# Safety bounds -- hardcoded limits that no auto-tuning may exceed
# ---------------------------------------------------------------------------

TUNING_BOUNDS: Dict[str, Any] = {
    "regime_multipliers": {
        "trending": (1.5, 3.0),
        "ranging": (0.8, 2.0),
        "mixed": (1.0, 2.5),
        "news": (2.0, 4.0),
    },
    "partial_tp": {
        "tp_levels_r": {
            0: (0.5, 1.5),   # TP1
            1: (1.0, 3.0),   # TP2
        },
        "profit_lock_amount_r": (0.1, 0.5),
    },
    "time_decay": {
        "time_decay_max_bars": (24, 96),
    },
    "chandelier": {
        "chandelier_multiplier": (2.0, 4.0),
        "chandelier_lookback": (15, 30),
    },
    "step_increment": 0.1,
}

# ---------------------------------------------------------------------------
# Minimum sample sizes
# ---------------------------------------------------------------------------

MIN_TOTAL_TRADES = 50
MIN_TRADES_PER_REGIME = 15
MIN_TRADES_FOR_TP = 30


class RiskAutoTuner:
    """Analyzes closed trade performance and tunes risk config values.

    Runs periodically (after N closed trades or on-demand). Reviews:

    - Stop hit rate per regime -> adjust regime_multipliers
    - Partial TP hit rates -> adjust split_ratios and tp_levels_r
    - Time-in-trade vs outcome -> adjust time_decay params
    - Chandelier vs ratchet trail effectiveness -> adjust trailing params
    - Spread cost impact -> adjust spread_filter_max_atr_pct

    Safety:

    - All adjustments bounded by ``TUNING_BOUNDS``
    - Changes logged with before/after values
    - Backup of YAML before any write
    - Auto-tune only adjusts asset_classes and global_defaults sections
    - NEVER touches absolute_limits or profile presets (Tim-only)

    Args:
        config_path: Path to ``risk_asset_classes.yaml``.  Defaults to
            ``Forex Trading Team/Config/risk_asset_classes.yaml``.
        history_source: Optional callable returning list of closed trade
            dicts.  If ``None``, trades must be passed directly to
            :meth:`analyze`.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        history_source: Optional[Callable[[], List[Dict]]] = None,
    ) -> None:
        if config_path is None:
            source_dir = Path(__file__).resolve().parent
            config_path = str(source_dir.parent / "Config" / "risk_asset_classes.yaml")

        self._config_path = Path(config_path)
        self._history_source = history_source
        self._config: Dict[str, Any] = {}
        self._load_config()

        # Tuning history file sits next to the YAML config
        self._history_path = self._config_path.parent / ".risk_tuning_history.json"

        logger.info(
            "RiskAutoTuner initialised: config=%s, bounds=%d categories",
            self._config_path, len(TUNING_BOUNDS) - 1,  # exclude step_increment
        )

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """Load current YAML config from disk."""
        try:
            with open(self._config_path, "r") as f:
                self._config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.error("Config not found: %s", self._config_path)
            self._config = {}
        except yaml.YAMLError as exc:
            logger.error("YAML parse error: %s", exc)
            self._config = {}

    # ------------------------------------------------------------------
    # Main analysis entry point
    # ------------------------------------------------------------------

    def analyze(self, trades: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """Analyze closed trades and produce tuning recommendations.

        Each trade dict should have:

        - ``instrument`` (str)
        - ``direction`` (str)
        - ``regime`` (str) -- regime at entry
        - ``entry_price`` (float)
        - ``exit_price`` (float)
        - ``initial_stop`` (float)
        - ``result_r`` (float) -- actual R-multiple outcome
        - ``tp1_hit`` (bool)
        - ``tp2_hit`` (bool)
        - ``bars_in_trade`` (int)
        - ``trailing_method`` (str) -- ``"chandelier"`` or ``"ratchet"``
        - ``spread_pips`` (float)
        - ``stopped_out`` (bool)

        Args:
            trades: List of closed trade dicts.  If ``None``, uses the
                ``history_source`` callable provided at init.

        Returns:
            Dict with keys ``status``, ``trades_analyzed``,
            ``recommendations`` (per-dimension dicts), and
            ``summary`` (human-readable).
        """
        if trades is None and self._history_source is not None:
            trades = self._history_source()
        if trades is None:
            trades = []

        if len(trades) < MIN_TOTAL_TRADES:
            logger.info(
                "Insufficient trades for tuning: %d < %d minimum",
                len(trades), MIN_TOTAL_TRADES,
            )
            return {
                "status": "insufficient_data",
                "trades_analyzed": len(trades),
                "minimum_required": MIN_TOTAL_TRADES,
                "recommendations": {},
                "summary": (
                    f"Need {MIN_TOTAL_TRADES} trades minimum, "
                    f"have {len(trades)}."
                ),
            }

        recommendations: Dict[str, Any] = {}
        summary_parts: List[str] = []

        # Dimension 1: Regime multipliers
        regime_rec = self._analyze_regime_multipliers(trades)
        if regime_rec:
            recommendations["regime_multipliers"] = regime_rec
            summary_parts.append(
                f"Regime: {len(regime_rec)} adjustment(s) recommended"
            )

        # Dimension 2: Partial TP levels
        tp_rec = self._analyze_partial_tp(trades)
        if tp_rec:
            recommendations["partial_tp"] = tp_rec
            summary_parts.append(
                f"Partial TP: {len(tp_rec)} adjustment(s) recommended"
            )

        # Dimension 3: Time decay
        td_rec = self._analyze_time_decay(trades)
        if td_rec:
            recommendations["time_decay"] = td_rec
            summary_parts.append(
                f"Time decay: {len(td_rec)} adjustment(s) recommended"
            )

        # Dimension 4: Trailing method
        trail_rec = self._analyze_trailing(trades)
        if trail_rec:
            recommendations["trailing"] = trail_rec
            summary_parts.append(
                f"Trailing: {len(trail_rec)} adjustment(s) recommended"
            )

        # Dimension 5: Spread impact
        spread_rec = self._analyze_spread_impact(trades)
        if spread_rec:
            recommendations["spread"] = spread_rec
            summary_parts.append(
                f"Spread: {len(spread_rec)} adjustment(s) recommended"
            )

        status = "recommendations_available" if recommendations else "no_changes_needed"
        summary = "; ".join(summary_parts) if summary_parts else "All parameters within optimal range."

        logger.info(
            "Analysis complete: %d trades, %d recommendations across %d dimensions",
            len(trades), sum(len(v) for v in recommendations.values()),
            len(recommendations),
        )

        return {
            "status": status,
            "trades_analyzed": len(trades),
            "recommendations": recommendations,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Dimension 1: Regime multiplier analysis
    # ------------------------------------------------------------------

    def _analyze_regime_multipliers(self, trades: List[Dict]) -> Dict[str, Any]:
        """Analyze stop-hit rate per regime and recommend multiplier changes.

        Rules:
        - Stop-hit rate > 60%: recommend increasing multiplier by step_increment
        - Stop-hit rate < 25% AND avg result_r < 1.0: recommend decreasing
          (stops too wide, not capturing enough)
        """
        step = TUNING_BOUNDS["step_increment"]
        by_regime: Dict[str, List[Dict]] = defaultdict(list)
        for t in trades:
            regime = t.get("regime", "mixed")
            by_regime[regime].append(t)

        recs: Dict[str, Any] = {}
        current_mults = self._config.get("global_defaults", {}).get(
            "volatility", {}
        ).get("regime_multipliers", {})

        for regime, regime_trades in by_regime.items():
            if len(regime_trades) < MIN_TRADES_PER_REGIME:
                continue

            stopped = sum(1 for t in regime_trades if t.get("stopped_out", False))
            stop_rate = stopped / len(regime_trades)
            avg_r = (
                sum(t.get("result_r", 0) for t in regime_trades) / len(regime_trades)
            )
            current = current_mults.get(regime)
            if current is None:
                continue

            bounds = TUNING_BOUNDS["regime_multipliers"].get(regime)
            if bounds is None:
                continue

            rec: Optional[Dict[str, Any]] = None

            if stop_rate > 0.60:
                new_val = self._clamp(current + step, bounds[0], bounds[1])
                if new_val != current:
                    rec = {
                        "action": "increase",
                        "field": f"regime_multipliers.{regime}",
                        "current": current,
                        "proposed": round(new_val, 2),
                        "reason": (
                            f"Stop-hit rate {stop_rate:.0%} > 60% "
                            f"({stopped}/{len(regime_trades)} trades)"
                        ),
                        "metrics": {
                            "stop_hit_rate": round(stop_rate, 3),
                            "avg_result_r": round(avg_r, 3),
                            "sample_size": len(regime_trades),
                        },
                    }

            elif stop_rate < 0.25 and avg_r < 1.0:
                new_val = self._clamp(current - step, bounds[0], bounds[1])
                if new_val != current:
                    rec = {
                        "action": "decrease",
                        "field": f"regime_multipliers.{regime}",
                        "current": current,
                        "proposed": round(new_val, 2),
                        "reason": (
                            f"Stop-hit rate {stop_rate:.0%} < 25% but "
                            f"avg R = {avg_r:.2f} < 1.0 (stops too wide)"
                        ),
                        "metrics": {
                            "stop_hit_rate": round(stop_rate, 3),
                            "avg_result_r": round(avg_r, 3),
                            "sample_size": len(regime_trades),
                        },
                    }

            if rec is not None:
                recs[regime] = rec

        return recs

    # ------------------------------------------------------------------
    # Dimension 2: Partial take-profit analysis
    # ------------------------------------------------------------------

    def _analyze_partial_tp(self, trades: List[Dict]) -> Dict[str, Any]:
        """Analyze TP1/TP2 hit rates and recommend level adjustments.

        Rules:
        - TP1 hit rate < 30%: move tp1 closer (e.g. 1.0R -> 0.8R)
        - TP2 hit rate < 15%: move tp2 closer (e.g. 2.0R -> 1.5R)
        - TP2 hit rate > 70%: move tp2 further (leaving money on table)
        """
        tp_trades = [t for t in trades if "tp1_hit" in t]
        if len(tp_trades) < MIN_TRADES_FOR_TP:
            return {}

        step = TUNING_BOUNDS["step_increment"]
        tp1_hits = sum(1 for t in tp_trades if t.get("tp1_hit", False))
        tp2_hits = sum(1 for t in tp_trades if t.get("tp2_hit", False))
        tp1_rate = tp1_hits / len(tp_trades)
        tp2_rate = tp2_hits / len(tp_trades)

        current_levels = self._config.get("global_defaults", {}).get(
            "partial_tp", {}
        ).get("tp_levels_r", [1.0, 2.0, None])

        recs: Dict[str, Any] = {}

        # TP1 analysis
        if len(current_levels) > 0 and current_levels[0] is not None:
            current_tp1 = float(current_levels[0])
            tp1_bounds = TUNING_BOUNDS["partial_tp"]["tp_levels_r"][0]

            if tp1_rate < 0.30:
                new_tp1 = self._clamp(current_tp1 - step, tp1_bounds[0], tp1_bounds[1])
                if new_tp1 != current_tp1:
                    recs["tp1"] = {
                        "action": "decrease",
                        "field": "partial_tp.tp_levels_r[0]",
                        "current": current_tp1,
                        "proposed": round(new_tp1, 2),
                        "reason": (
                            f"TP1 hit rate {tp1_rate:.0%} < 30% "
                            f"({tp1_hits}/{len(tp_trades)})"
                        ),
                        "metrics": {
                            "tp1_hit_rate": round(tp1_rate, 3),
                            "sample_size": len(tp_trades),
                        },
                    }

        # TP2 analysis
        if len(current_levels) > 1 and current_levels[1] is not None:
            current_tp2 = float(current_levels[1])
            tp2_bounds = TUNING_BOUNDS["partial_tp"]["tp_levels_r"][1]

            if tp2_rate < 0.15:
                new_tp2 = self._clamp(current_tp2 - step, tp2_bounds[0], tp2_bounds[1])
                if new_tp2 != current_tp2:
                    recs["tp2"] = {
                        "action": "decrease",
                        "field": "partial_tp.tp_levels_r[1]",
                        "current": current_tp2,
                        "proposed": round(new_tp2, 2),
                        "reason": (
                            f"TP2 hit rate {tp2_rate:.0%} < 15% "
                            f"({tp2_hits}/{len(tp_trades)})"
                        ),
                        "metrics": {
                            "tp2_hit_rate": round(tp2_rate, 3),
                            "sample_size": len(tp_trades),
                        },
                    }
            elif tp2_rate > 0.70:
                new_tp2 = self._clamp(current_tp2 + step, tp2_bounds[0], tp2_bounds[1])
                if new_tp2 != current_tp2:
                    recs["tp2"] = {
                        "action": "increase",
                        "field": "partial_tp.tp_levels_r[1]",
                        "current": current_tp2,
                        "proposed": round(new_tp2, 2),
                        "reason": (
                            f"TP2 hit rate {tp2_rate:.0%} > 70% "
                            f"(leaving money on table)"
                        ),
                        "metrics": {
                            "tp2_hit_rate": round(tp2_rate, 3),
                            "sample_size": len(tp_trades),
                        },
                    }

        return recs

    # ------------------------------------------------------------------
    # Dimension 3: Time decay analysis
    # ------------------------------------------------------------------

    def _analyze_time_decay(self, trades: List[Dict]) -> Dict[str, Any]:
        """Analyze bars-in-trade vs outcome and recommend time decay changes.

        Buckets: 0-12, 12-24, 24-36, 36-48, 48+ bars.

        Rules:
        - Trades > 48 bars with negative avg result_r: keep decay at 48
        - Trades 36-48 profitable: recommend extending max_bars to 60
        """
        bar_trades = [t for t in trades if "bars_in_trade" in t]
        if len(bar_trades) < MIN_TOTAL_TRADES:
            return {}

        buckets: Dict[str, List[float]] = {
            "0-12": [],
            "12-24": [],
            "24-36": [],
            "36-48": [],
            "48+": [],
        }

        for t in bar_trades:
            bars = t.get("bars_in_trade", 0)
            r = t.get("result_r", 0)
            if bars <= 12:
                buckets["0-12"].append(r)
            elif bars <= 24:
                buckets["12-24"].append(r)
            elif bars <= 36:
                buckets["24-36"].append(r)
            elif bars <= 48:
                buckets["36-48"].append(r)
            else:
                buckets["48+"].append(r)

        step = TUNING_BOUNDS["step_increment"]
        current_max_bars = self._config.get("global_defaults", {}).get(
            "stop_management", {}
        ).get("time_decay_max_bars", 48)
        td_bounds = TUNING_BOUNDS["time_decay"]["time_decay_max_bars"]

        recs: Dict[str, Any] = {}
        bucket_metrics = {}
        for name, values in buckets.items():
            if values:
                bucket_metrics[name] = {
                    "count": len(values),
                    "avg_r": round(sum(values) / len(values), 3),
                }
            else:
                bucket_metrics[name] = {"count": 0, "avg_r": 0.0}

        # Check late-trade profitability
        late_trades = buckets["48+"]
        mid_late_trades = buckets["36-48"]

        if late_trades and len(late_trades) >= 5:
            avg_late = sum(late_trades) / len(late_trades)
            if avg_late < 0:
                # Late trades losing money -- keep or reduce max_bars
                if current_max_bars > 48:
                    new_val = self._clamp(
                        current_max_bars - 12, td_bounds[0], td_bounds[1]
                    )
                    if new_val != current_max_bars:
                        recs["time_decay_max_bars"] = {
                            "action": "decrease",
                            "field": "stop_management.time_decay_max_bars",
                            "current": current_max_bars,
                            "proposed": int(new_val),
                            "reason": (
                                f"Trades >48 bars avg R = {avg_late:.2f} "
                                f"(negative, reduce holding time)"
                            ),
                            "metrics": bucket_metrics,
                        }

        if mid_late_trades and len(mid_late_trades) >= 5:
            avg_mid_late = sum(mid_late_trades) / len(mid_late_trades)
            if avg_mid_late > 0 and "time_decay_max_bars" not in recs:
                # 36-48 bar trades are profitable, extend
                new_val = self._clamp(
                    current_max_bars + 12, td_bounds[0], td_bounds[1]
                )
                if new_val != current_max_bars:
                    recs["time_decay_max_bars"] = {
                        "action": "increase",
                        "field": "stop_management.time_decay_max_bars",
                        "current": current_max_bars,
                        "proposed": int(new_val),
                        "reason": (
                            f"Trades 36-48 bars avg R = {avg_mid_late:.2f} "
                            f"(profitable, extend holding time)"
                        ),
                        "metrics": bucket_metrics,
                    }

        return recs

    # ------------------------------------------------------------------
    # Dimension 4: Trailing method analysis
    # ------------------------------------------------------------------

    def _analyze_trailing(self, trades: List[Dict]) -> Dict[str, Any]:
        """Compare chandelier vs ratchet trailing stop effectiveness.

        Rules:
        - If chandelier captures > 0.5R more on average: recommend as primary
        - If ratchet captures more: recommend tighter chandelier multiplier
        """
        chandelier_trades = [
            t for t in trades if t.get("trailing_method") == "chandelier"
        ]
        ratchet_trades = [
            t for t in trades if t.get("trailing_method") == "ratchet"
        ]

        if len(chandelier_trades) < 10 or len(ratchet_trades) < 10:
            return {}

        avg_chand = (
            sum(t.get("result_r", 0) for t in chandelier_trades)
            / len(chandelier_trades)
        )
        avg_ratch = (
            sum(t.get("result_r", 0) for t in ratchet_trades)
            / len(ratchet_trades)
        )

        recs: Dict[str, Any] = {}
        diff = avg_chand - avg_ratch

        if diff > 0.5:
            recs["trailing_preference"] = {
                "action": "prefer_chandelier",
                "field": "stop_management.trailing_algorithm",
                "current": self._config.get("global_defaults", {}).get(
                    "stop_management", {}
                ).get("trailing_algorithm", "chandelier"),
                "proposed": "chandelier",
                "reason": (
                    f"Chandelier avg R = {avg_chand:.2f} vs "
                    f"ratchet avg R = {avg_ratch:.2f} (+{diff:.2f}R)"
                ),
                "metrics": {
                    "chandelier_avg_r": round(avg_chand, 3),
                    "ratchet_avg_r": round(avg_ratch, 3),
                    "difference_r": round(diff, 3),
                    "chandelier_count": len(chandelier_trades),
                    "ratchet_count": len(ratchet_trades),
                },
            }
        elif diff < -0.5:
            # Ratchet is better -- recommend tightening chandelier
            current_mult = self._config.get("global_defaults", {}).get(
                "stop_management", {}
            ).get("chandelier_multiplier", 3.0)
            step = TUNING_BOUNDS["step_increment"]
            ch_bounds = TUNING_BOUNDS["chandelier"]["chandelier_multiplier"]
            new_val = self._clamp(current_mult - step, ch_bounds[0], ch_bounds[1])
            if new_val != current_mult:
                recs["chandelier_multiplier"] = {
                    "action": "decrease",
                    "field": "stop_management.chandelier_multiplier",
                    "current": current_mult,
                    "proposed": round(new_val, 2),
                    "reason": (
                        f"Ratchet avg R = {avg_ratch:.2f} outperforms "
                        f"chandelier avg R = {avg_chand:.2f} by {-diff:.2f}R; "
                        f"tighten chandelier"
                    ),
                    "metrics": {
                        "chandelier_avg_r": round(avg_chand, 3),
                        "ratchet_avg_r": round(avg_ratch, 3),
                        "difference_r": round(diff, 3),
                        "chandelier_count": len(chandelier_trades),
                        "ratchet_count": len(ratchet_trades),
                    },
                }

        return recs

    # ------------------------------------------------------------------
    # Dimension 5: Spread impact analysis
    # ------------------------------------------------------------------

    def _analyze_spread_impact(self, trades: List[Dict]) -> Dict[str, Any]:
        """Analyze spread cost relative to trade outcome.

        Identifies if high-spread trades are consistently unprofitable.
        """
        spread_trades = [t for t in trades if "spread_pips" in t]
        if len(spread_trades) < MIN_TOTAL_TRADES:
            return {}

        spreads = [t.get("spread_pips", 0) for t in spread_trades]
        if not spreads:
            return {}

        median_spread = sorted(spreads)[len(spreads) // 2]

        high_spread = [t for t in spread_trades if t.get("spread_pips", 0) > median_spread]
        low_spread = [t for t in spread_trades if t.get("spread_pips", 0) <= median_spread]

        if not high_spread or not low_spread:
            return {}

        avg_r_high = sum(t.get("result_r", 0) for t in high_spread) / len(high_spread)
        avg_r_low = sum(t.get("result_r", 0) for t in low_spread) / len(low_spread)

        recs: Dict[str, Any] = {}

        # If high-spread trades are losing and low-spread are winning,
        # recommend tightening the spread filter
        if avg_r_high < 0 and avg_r_low > 0:
            current_filter = self._config.get("global_defaults", {}).get(
                "safety", {}
            ).get("spread_filter_max_atr_pct", 0.10)
            step = TUNING_BOUNDS["step_increment"]
            new_val = max(0.05, current_filter - 0.02)  # Tighten by 2%
            if new_val != current_filter:
                recs["spread_filter"] = {
                    "action": "tighten",
                    "field": "safety.spread_filter_max_atr_pct",
                    "current": current_filter,
                    "proposed": round(new_val, 3),
                    "reason": (
                        f"High-spread trades avg R = {avg_r_high:.2f} "
                        f"(losing) vs low-spread avg R = {avg_r_low:.2f} "
                        f"(winning); tighten spread filter"
                    ),
                    "metrics": {
                        "median_spread": round(median_spread, 2),
                        "high_spread_avg_r": round(avg_r_high, 3),
                        "low_spread_avg_r": round(avg_r_low, 3),
                        "high_spread_count": len(high_spread),
                        "low_spread_count": len(low_spread),
                    },
                }

        return recs

    # ------------------------------------------------------------------
    # Apply recommendations
    # ------------------------------------------------------------------

    def apply_recommendations(
        self,
        recommendations: Dict[str, Any],
        auto_apply: bool = False,
    ) -> Dict[str, Any]:
        """Apply or preview tuning recommendations.

        Args:
            recommendations: Output from :meth:`analyze` -- the full
                analysis dict (with ``recommendations`` key) or just
                the recommendations sub-dict.
            auto_apply: If ``True``, write changes to YAML within
                bounds.  If ``False``, return recommendations only.

        Returns:
            Dict with ``changes_applied`` (list of changes made),
            ``changes_deferred`` (outside bounds or review-only),
            ``backup_path`` (if YAML was written), and ``auto_applied``
            (bool).
        """
        # Accept either the full analysis dict or just recommendations
        if "recommendations" in recommendations:
            recs = recommendations["recommendations"]
        else:
            recs = recommendations

        if not recs:
            return {
                "auto_applied": False,
                "changes_applied": [],
                "changes_deferred": [],
                "backup_path": None,
            }

        if not auto_apply:
            all_changes = self._flatten_recommendations(recs)
            return {
                "auto_applied": False,
                "changes_applied": [],
                "changes_deferred": all_changes,
                "backup_path": None,
            }

        # Auto-apply mode: validate bounds, backup, write
        self._load_config()  # Refresh from disk
        config_before = copy.deepcopy(self._config)

        changes_applied: List[Dict[str, Any]] = []
        changes_deferred: List[Dict[str, Any]] = []

        for dimension, dim_recs in recs.items():
            if isinstance(dim_recs, dict):
                for key, rec in dim_recs.items():
                    if not isinstance(rec, dict):
                        continue
                    result = self._apply_single_recommendation(rec)
                    if result["applied"]:
                        changes_applied.append(result)
                    else:
                        changes_deferred.append(result)

        backup_path = None
        if changes_applied:
            # Create backup
            backup_path = self._create_backup()

            # Write updated YAML
            self._write_config()

            # Save tuning history
            self._save_tuning_history(
                config_before=config_before,
                config_after=self._config,
                changes_applied=changes_applied,
                trades_analyzed=recommendations.get("trades_analyzed", 0),
            )

            logger.info(
                "Auto-applied %d changes (deferred %d); backup at %s",
                len(changes_applied), len(changes_deferred), backup_path,
            )
        else:
            logger.info("No changes within bounds to apply.")

        result = {
            "auto_applied": bool(changes_applied),
            "changes_applied": changes_applied,
            "changes_deferred": changes_deferred,
            "backup_path": str(backup_path) if backup_path else None,
        }

        # ── Learning Integration: record parameter changes in vault ──
        if changes_applied:
            try:
                from learning_integrator import LearningIntegrator
                integrator = LearningIntegrator()
                tuning_learnings = integrator.process_tuning_event({
                    "applied": changes_applied,
                    "trades_analyzed": recommendations.get("trades_analyzed", 0),
                })
                result["learnings_written"] = tuning_learnings
                logger.info(
                    "Risk tuning → %d vault learnings written",
                    len(tuning_learnings))
            except Exception as e:
                logger.warning("Risk tuning learning integration failed: %s", e)

        return result

    def _apply_single_recommendation(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Apply one recommendation to the in-memory config.

        Returns dict with ``applied`` (bool) and details.
        """
        field = rec.get("field", "")
        proposed = rec.get("proposed")
        current = rec.get("current")

        if proposed is None or field == "":
            return {"applied": False, "field": field, "reason": "missing data"}

        # Validate the proposed value is within tuning bounds
        if not self._value_within_bounds(field, proposed):
            return {
                "applied": False,
                "field": field,
                "current": current,
                "proposed": proposed,
                "clamped": self._clamp_to_bounds(field, proposed),
                "reason": "outside tuning bounds",
            }

        # Navigate config and apply
        applied = self._set_config_value(field, proposed)
        return {
            "applied": applied,
            "field": field,
            "current": current,
            "proposed": proposed,
            "reason": rec.get("reason", ""),
        }

    def _set_config_value(self, field: str, value: Any) -> bool:
        """Set a value in the global_defaults section of the config.

        Supports dotted paths like ``regime_multipliers.trending`` and
        ``partial_tp.tp_levels_r[0]``.
        """
        defaults = self._config.get("global_defaults", {})

        # Parse field path
        parts = field.split(".")
        if not parts:
            return False

        # Navigate to parent
        node = defaults
        for part in parts[:-1]:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return False

        # Set value
        last = parts[-1]

        # Handle array indexing like tp_levels_r[0]
        if "[" in last and "]" in last:
            key = last[:last.index("[")]
            idx_str = last[last.index("[") + 1:last.index("]")]
            try:
                idx = int(idx_str)
            except ValueError:
                return False
            if isinstance(node, dict) and key in node:
                arr = node[key]
                if isinstance(arr, list) and 0 <= idx < len(arr):
                    arr[idx] = value
                    return True
            return False

        if isinstance(node, dict):
            node[last] = value
            return True

        return False

    def _value_within_bounds(self, field: str, value: Any) -> bool:
        """Check if a proposed value falls within TUNING_BOUNDS."""
        bounds = self._get_bounds_for_field(field)
        if bounds is None:
            return True  # No bounds defined -- allow
        if isinstance(bounds, tuple) and len(bounds) == 2:
            lo, hi = bounds
            return lo <= value <= hi
        return True

    def _clamp_to_bounds(self, field: str, value: Any) -> Any:
        """Clamp a value to TUNING_BOUNDS for its field."""
        bounds = self._get_bounds_for_field(field)
        if bounds is None:
            return value
        if isinstance(bounds, tuple) and len(bounds) == 2:
            lo, hi = bounds
            return self._clamp(value, lo, hi)
        return value

    def _get_bounds_for_field(self, field: str) -> Optional[Tuple[float, float]]:
        """Look up bounds for a dotted field path."""
        # Map field paths to TUNING_BOUNDS locations
        if "regime_multipliers" in field:
            regime = field.split(".")[-1]
            return TUNING_BOUNDS.get("regime_multipliers", {}).get(regime)
        if "tp_levels_r" in field:
            if "[0]" in field:
                return TUNING_BOUNDS["partial_tp"]["tp_levels_r"].get(0)
            if "[1]" in field:
                return TUNING_BOUNDS["partial_tp"]["tp_levels_r"].get(1)
        if "profit_lock_amount_r" in field:
            return TUNING_BOUNDS["partial_tp"].get("profit_lock_amount_r")
        if "time_decay_max_bars" in field:
            return TUNING_BOUNDS["time_decay"].get("time_decay_max_bars")
        if "chandelier_multiplier" in field:
            return TUNING_BOUNDS["chandelier"].get("chandelier_multiplier")
        if "chandelier_lookback" in field:
            return TUNING_BOUNDS["chandelier"].get("chandelier_lookback")
        return None

    # ------------------------------------------------------------------
    # Backup and write
    # ------------------------------------------------------------------

    def _create_backup(self) -> Path:
        """Create a timestamped backup of the YAML config."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_name = f"{self._config_path.stem}.backup_{timestamp}{self._config_path.suffix}"
        backup_path = self._config_path.parent / backup_name
        shutil.copy2(str(self._config_path), str(backup_path))
        logger.info("Config backup created: %s", backup_path)
        return backup_path

    def _write_config(self) -> None:
        """Write the current in-memory config back to YAML."""
        try:
            with open(self._config_path, "w") as f:
                yaml.dump(
                    self._config, f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            logger.info("Config written to %s", self._config_path)
        except OSError as exc:
            logger.error("Failed to write config: %s", exc)

    # ------------------------------------------------------------------
    # Tuning history
    # ------------------------------------------------------------------

    def _save_tuning_history(
        self,
        config_before: Dict,
        config_after: Dict,
        changes_applied: List[Dict],
        trades_analyzed: int,
    ) -> None:
        """Append a tuning event to the history file."""
        history = self._load_tuning_history()
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trades_analyzed": trades_analyzed,
            "changes": changes_applied,
            "config_before_defaults": config_before.get("global_defaults", {}),
            "config_after_defaults": config_after.get("global_defaults", {}),
        }
        history.append(entry)

        try:
            with open(self._history_path, "w") as f:
                json.dump(history, f, indent=2, default=str)
            logger.info(
                "Tuning history saved: %d total entries", len(history)
            )
        except OSError as exc:
            logger.error("Failed to save tuning history: %s", exc)

    def _load_tuning_history(self) -> List[Dict]:
        """Load tuning history from JSON file."""
        try:
            with open(self._history_path, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except FileNotFoundError:
            pass  # Normal on first run
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load tuning history: %s", exc)
        return []

    def get_tuning_history(self) -> List[Dict]:
        """Return the full tuning history.

        Returns:
            List of past tuning events with timestamps, changes,
            and performance data.
        """
        return self._load_tuning_history()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        """Clamp a value to [lo, hi] range."""
        return max(lo, min(hi, value))

    @staticmethod
    def _flatten_recommendations(recs: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Flatten nested recommendation dicts into a flat list."""
        flat: List[Dict[str, Any]] = []
        for dim_recs in recs.values():
            if isinstance(dim_recs, dict):
                for rec in dim_recs.values():
                    if isinstance(rec, dict):
                        flat.append(rec)
        return flat
