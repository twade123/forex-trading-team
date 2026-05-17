"""
Optuna TPE Optimization Engine for the forex parameter optimizer.

V2 engine replacing scikit-optimize GP with Optuna's Tree-structured Parzen
Estimator (TPE). Handles 49-dimensional parameter spaces efficiently.
Uses fANOVA for nonlinear parameter importance estimation.

Composite score v2 adds: profit factor, Calmar ratio, max drawdown penalty.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import optuna

from optimizer.engine import OptimizationResult
from optimizer.replay import TradeSnapshot, replay_all_trades

logger = logging.getLogger("optimizer.engine_v2")


# ---------------------------------------------------------------------------
# Composite scoring v2
# ---------------------------------------------------------------------------

def composite_score_v2(replay_result: dict) -> float:
    """V2 composite score with risk metrics.

    Components:
      WR:        (win_rate / 100)                    x 0.30
      PF:        min(profit_factor, 3.0) / 3.0       x 0.25
      Calmar:    clamp(calmar, 0, 5.0) / 5.0         x 0.20
      MDD:       max(0, 1.0 - mdd / 50.0)            x 0.15
      Retention: min(remaining/total / 0.60, 1.0)     x 0.10

    Hard floors:
      remaining < 20 -> 0.0
      max_drawdown > 80p -> 0.0

    Returns score on 0-100 scale.
    """
    total = replay_result.get("total", 0)
    remaining = replay_result.get("remaining", 0)

    if remaining < 20:
        return 0.0

    max_dd = replay_result.get("max_drawdown_pips", 0.0)
    if max_dd > 80.0:
        return 0.0

    wr = replay_result.get("win_rate", 0.0) / 100.0

    pf = replay_result.get("profit_factor", 1.0)
    pf_score = min(pf, 3.0) / 3.0

    total_pips = replay_result.get("total_pips", 0.0)
    calmar = total_pips / max(max_dd, 1.0) if total_pips > 0 else 0.0
    calmar_score = min(max(calmar, 0.0), 5.0) / 5.0

    mdd_penalty = max(0.0, 1.0 - max_dd / 50.0)

    retention_ratio = remaining / total if total > 0 else 0.0
    retention_score = min(retention_ratio / 0.60, 1.0)

    raw = (
        wr * 0.30
        + pf_score * 0.25
        + calmar_score * 0.20
        + mdd_penalty * 0.15
        + retention_score * 0.10
    )
    return round(raw * 100.0, 2)


# ---------------------------------------------------------------------------
# Progress callback
# ---------------------------------------------------------------------------

def _progress_callback(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
    """Log progress every 10 trials."""
    n = trial.number + 1
    if n % 10 == 0 or n <= 3:
        best = study.best_trial
        logger.info(
            "[PROGRESS] trial %d — score=%.2f WR=%.1f%% (best: trial %d score=%.2f WR=%.1f%%)",
            n,
            trial.value or 0.0,
            trial.user_attrs.get("win_rate", 0.0),
            best.number + 1,
            best.value or 0.0,
            best.user_attrs.get("win_rate", 0.0),
        )


# ---------------------------------------------------------------------------
# Optuna Engine
# ---------------------------------------------------------------------------

class OptunaEngine:
    """Optuna TPE optimization engine.

    Drop-in replacement for OptimizerEngine that returns the same
    OptimizationResult dataclass. Uses TPE sampler which handles
    high-dimensional (49+) spaces much better than GP.
    """

    def __init__(
        self,
        trades: List[TradeSnapshot],
        params: Dict[str, Dict],
        n_trials: int = 500,
        n_startup_trials: int = 50,
        seed: int = 42,
        candles_by_trade: Optional[dict] = None,
        study_name: Optional[str] = None,
        storage_path: Optional[str] = None,
        apply_spread: bool = False,
        use_time_decay: bool = False,
    ):
        self.trades = trades
        self.params = params
        self.n_trials = n_trials
        self.n_startup_trials = n_startup_trials
        self.seed = seed
        self.candles_by_trade = candles_by_trade
        self.apply_spread = apply_spread
        self.trade_weights = self._compute_time_decay_weights() if use_time_decay else None

        self.param_names = sorted(params.keys())

        # Storage defaults to optimizer dir
        if storage_path is None:
            optimizer_dir = os.path.dirname(os.path.abspath(__file__))
            storage_path = os.path.join(optimizer_dir, "optuna_studies.db")
        self.storage_path = storage_path

        from datetime import datetime
        self.study_name = study_name or f"optimizer_{datetime.now():%Y%m%d_%H%M%S}"

        self.study: Optional[optuna.Study] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_time_decay_weights(self) -> Optional[Dict[str, float]]:
        """Compute time-decay weights: exp(-0.01 * days_old), half-life ~69 days."""
        if not any(t.entry_time for t in self.trades):
            return None
        now = datetime.utcnow()
        weights = {}
        for t in self.trades:
            if t.entry_time:
                try:
                    from optimizer.walk_forward import _parse_time
                    dt = _parse_time(t.entry_time)
                    days_old = (now - dt).total_seconds() / 86400
                    weights[t.id] = math.exp(-0.01 * max(days_old, 0))
                except Exception:
                    weights[t.id] = 1.0
            else:
                weights[t.id] = 1.0
        return weights

    def _suggest_params(self, trial: optuna.Trial) -> Dict[str, Any]:
        """Use trial.suggest_int/suggest_float for each param."""
        param_dict = {}
        for name in self.param_names:
            p = self.params[name]
            if p.get("is_int", False):
                param_dict[name] = trial.suggest_int(name, int(p["min"]), int(p["max"]))
            else:
                param_dict[name] = trial.suggest_float(name, float(p["min"]), float(p["max"]))
        return param_dict

    def _objective(self, trial: optuna.Trial) -> float:
        """Optuna MAXIMIZES when direction='maximize'."""
        param_dict = self._suggest_params(trial)
        result = replay_all_trades(
            self.trades, param_dict, candles_by_trade=self.candles_by_trade,
            apply_spread=self.apply_spread, trade_weights=self.trade_weights,
        )
        score = composite_score_v2(result)

        # Store user attrs for analysis
        trial.set_user_attr("win_rate", result.get("win_rate", 0.0))
        trial.set_user_attr("avg_pips", result.get("avg_pips", 0.0))
        trial.set_user_attr("total_pips", result.get("total_pips", 0.0))
        trial.set_user_attr("remaining", result.get("remaining", 0))
        trial.set_user_attr("max_drawdown", result.get("max_drawdown_pips", 0.0))
        trial.set_user_attr("profit_factor", result.get("profit_factor", 0.0))

        return score

    def _compute_baseline(self) -> Tuple[float, float, float]:
        """Replay with current default values; return (score, win_rate, avg_pips)."""
        default_dict = {name: self.params[name]["value"] for name in self.param_names}
        result = replay_all_trades(
            self.trades, default_dict, candles_by_trade=self.candles_by_trade,
            apply_spread=self.apply_spread, trade_weights=self.trade_weights,
        )
        score = composite_score_v2(result)
        return score, result.get("win_rate", 0.0), result.get("avg_pips", 0.0)

    def _get_param_importance(self) -> Dict[str, float]:
        """Use Optuna's fANOVA-based importance (nonlinear)."""
        try:
            return optuna.importance.get_param_importances(self.study)
        except Exception as e:
            logger.warning("[IMPORTANCE] fANOVA failed: %s — falling back to uniform", e)
            n = len(self.param_names)
            return {name: 1.0 / n for name in self.param_names}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> OptimizationResult:
        """Run Optuna TPE optimization and return OptimizationResult."""
        t0 = time.time()

        baseline_score, baseline_wr, baseline_avg_pips = self._compute_baseline()
        logger.info(
            "[BASELINE] score=%.2f WR=%.1f%% avg_pips=%.2f",
            baseline_score, baseline_wr, baseline_avg_pips,
        )

        sampler = optuna.samplers.TPESampler(
            n_startup_trials=self.n_startup_trials,
            seed=self.seed,
        )

        storage = f"sqlite:///{self.storage_path}"
        self.study = optuna.create_study(
            study_name=self.study_name,
            sampler=sampler,
            direction="maximize",
            storage=storage,
            load_if_exists=True,
        )

        # Suppress Optuna's own trial-by-trial logging
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        self.study.optimize(
            self._objective,
            n_trials=self.n_trials,
            callbacks=[_progress_callback],
        )

        # Extract best params
        best_trial = self.study.best_trial
        best_params = {name: best_trial.params[name] for name in self.param_names}

        # fANOVA importance
        param_importance = self._get_param_importance()

        # Build history from all trials
        history = []
        for trial in self.study.trials:
            if trial.state == optuna.trial.TrialState.COMPLETE:
                history.append({
                    "params": {name: trial.params.get(name) for name in self.param_names},
                    "score": trial.value or 0.0,
                    "win_rate": trial.user_attrs.get("win_rate", 0.0),
                    "avg_pips": trial.user_attrs.get("avg_pips", 0.0),
                    "total_pips": trial.user_attrs.get("total_pips", 0.0),
                    "remaining": trial.user_attrs.get("remaining", 0),
                })

        # Final replay of best params for full stats
        best_replay = replay_all_trades(
            self.trades, best_params, candles_by_trade=self.candles_by_trade
        )

        elapsed = time.time() - t0
        logger.info(
            "[DONE] %d trials in %.1fs — best score=%.2f WR=%.1f%% PF=%.2f MDD=%.1fp",
            len(history), elapsed, best_trial.value or 0.0,
            best_replay.get("win_rate", 0.0),
            best_replay.get("profit_factor", 0.0),
            best_replay.get("max_drawdown_pips", 0.0),
        )

        return OptimizationResult(
            best_params=best_params,
            best_score=best_trial.value or 0.0,
            best_win_rate=best_replay.get("win_rate", 0.0),
            best_avg_pips=best_replay.get("avg_pips", 0.0),
            best_total_pips=best_replay.get("total_pips", 0.0),
            best_remaining_trades=best_replay.get("remaining", 0),
            baseline_score=baseline_score,
            baseline_win_rate=baseline_wr,
            baseline_avg_pips=baseline_avg_pips,
            evaluations=len(history),
            elapsed_seconds=elapsed,
            history=history,
            param_importance=param_importance,
        )
