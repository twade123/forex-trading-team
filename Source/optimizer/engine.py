"""
Bayesian Optimization Engine for the forex parameter optimizer.

Uses scikit-optimize's gp_minimize to search the parameter space efficiently.
The objective function replays all trades with candidate params and returns
a composite score.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from optimizer.replay import TradeSnapshot, replay_all_trades


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class OptimizationResult:
    """Full result of one optimization run."""

    best_params: Dict[str, Any]
    best_score: float
    best_win_rate: float
    best_avg_pips: float
    best_total_pips: float
    best_remaining_trades: int
    baseline_score: float
    baseline_win_rate: float
    baseline_avg_pips: float
    evaluations: int
    elapsed_seconds: float
    history: List[Dict[str, Any]]   # Each eval's params + score
    param_importance: Dict[str, float]  # Relative importance of each param


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

def _composite_score(replay_result: dict) -> float:
    """Score = win_rate * 0.7 + avg_pips_normalized * 0.2 + trade_retention * 0.1

    avg_pips_norm: clamp avg_pips to [-20, +20], map to 0-100.
    retention_score: remaining/total, penalize blocking >40% (60% retention = full score).
    Hard floor: if remaining < 20 trades, return 0.0.
    """
    total = replay_result.get("total", 0)
    remaining = replay_result.get("remaining", 0)

    if remaining < 20:
        return 0.0

    win_rate = replay_result.get("win_rate", 0.0)

    avg_pips = replay_result.get("avg_pips", 0.0)
    clamped = max(-20.0, min(20.0, avg_pips))
    avg_pips_norm = (clamped + 20.0) / 40.0 * 100.0  # 0-100

    retention_ratio = remaining / total if total > 0 else 0.0
    if retention_ratio >= 0.60:
        retention_score = 1.0
    else:
        retention_score = retention_ratio / 0.60

    score = win_rate * 0.7 + (avg_pips_norm / 100.0) * 0.2 + retention_score * 0.1
    return score


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class OptimizerEngine:
    """Bayesian optimization engine wrapping scikit-optimize gp_minimize."""

    def __init__(
        self,
        trades: List[TradeSnapshot],
        params: Dict[str, Dict],
        n_calls: int = 500,
        n_initial_points: int = 50,
        random_state: int = 42,
        candles_by_trade: dict = None,
    ):
        """
        Args:
            trades: List of TradeSnapshot objects to replay.
            params: Dict of param_name -> {"min", "max", "value", "is_int", ...}
            n_calls: Total number of objective evaluations.
            n_initial_points: Random explorations before Gaussian Process kicks in.
            random_state: Seed for reproducibility.
            candles_by_trade: Optional dict {trade_id: DataFrame} for candle-walk replay.
                When provided, uses high-fidelity M15 candle walk instead of MFE approximation.
        """
        self.trades = trades
        self.params = params
        self.n_calls = n_calls
        self.n_initial_points = n_initial_points
        self.random_state = random_state
        self.candles_by_trade = candles_by_trade

        # Deterministic ordering is critical so param_values list maps correctly
        self.param_names = sorted(params.keys())
        self.history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_search_space(self) -> list:
        """Build skopt dimension list: Real for floats, Integer for ints."""
        from skopt.space import Integer, Real

        dims = []
        for name in self.param_names:
            p = self.params[name]
            lo, hi = float(p["min"]), float(p["max"])
            if p.get("is_int", False):
                dims.append(Integer(int(lo), int(hi), name=name))
            else:
                dims.append(Real(lo, hi, name=name))
        return dims

    def _build_param_dict(self, param_values: list) -> dict:
        """Map ordered values back to a named dict."""
        return dict(zip(self.param_names, param_values))

    def _objective(self, param_values: list) -> float:
        """Replay all trades with *param_values*, compute composite score.

        Returns NEGATIVE score because skopt minimizes.
        Records each evaluation to self.history.
        Logs progress every 10 evaluations.
        """
        param_dict = self._build_param_dict(param_values)
        result = replay_all_trades(self.trades, param_dict, candles_by_trade=self.candles_by_trade)
        score = _composite_score(result)

        self.history.append({
            "params": dict(param_dict),
            "score": score,
            "win_rate": result.get("win_rate", 0.0),
            "avg_pips": result.get("avg_pips", 0.0),
            "total_pips": result.get("total_pips", 0.0),
            "remaining": result.get("remaining", 0),
        })

        # Progress logging
        n = len(self.history)
        if n % 10 == 0 or n <= 3:
            best = max(self.history, key=lambda h: h["score"])
            import logging
            logging.getLogger("optimizer.engine").info(
                "[PROGRESS] eval %d/%d — score=%.2f WR=%.1f%% avg=%.2f (best: %.2f WR=%.1f%%)",
                n, self.n_calls, score, result.get("win_rate", 0),
                result.get("avg_pips", 0), best["score"], best["win_rate"],
            )

        return -score  # skopt minimizes

    def _compute_baseline(self) -> Tuple[float, float, float]:
        """Replay with current default values; return (score, win_rate, avg_pips)."""
        default_dict = {name: self.params[name]["value"] for name in self.param_names}
        result = replay_all_trades(self.trades, default_dict)
        score = _composite_score(result)
        return score, result.get("win_rate", 0.0), result.get("avg_pips", 0.0)

    def _compute_param_importance(self, opt_result) -> Dict[str, float]:
        """Correlate each param's values across history with scores.

        Uses absolute Pearson-like correlation as a proxy for importance.
        Normalizes so importances sum to 1.
        """
        if len(self.history) < 2:
            return {name: 1.0 / len(self.param_names) for name in self.param_names}

        scores = [h["score"] for h in self.history]
        score_mean = sum(scores) / len(scores)
        score_var = sum((s - score_mean) ** 2 for s in scores)

        importances: Dict[str, float] = {}
        for name in self.param_names:
            values = [h["params"][name] for h in self.history]
            val_mean = sum(values) / len(values)
            val_var = sum((v - val_mean) ** 2 for v in values)

            if score_var == 0 or val_var == 0:
                importances[name] = 0.0
            else:
                cov = sum(
                    (values[i] - val_mean) * (scores[i] - score_mean)
                    for i in range(len(scores))
                )
                importances[name] = abs(cov) / ((score_var * val_var) ** 0.5)

        total = sum(importances.values())
        if total == 0:
            n = len(self.param_names)
            return {name: 1.0 / n for name in self.param_names}

        return {name: v / total for name, v in importances.items()}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> OptimizationResult:
        """Run Bayesian optimization and return a full OptimizationResult."""
        from skopt import gp_minimize

        t0 = time.time()
        self.history = []

        baseline_score, baseline_wr, baseline_avg_pips = self._compute_baseline()
        dimensions = self._build_search_space()

        opt = gp_minimize(
            func=self._objective,
            dimensions=dimensions,
            n_calls=self.n_calls,
            n_initial_points=self.n_initial_points,
            random_state=self.random_state,
            noise=1e-10,
        )

        best_params = self._build_param_dict(opt.x)

        # Pull metrics from history entry that matches best score
        best_neg_score = opt.fun
        best_score = -best_neg_score

        # Find the matching history entry for best
        best_entry = max(self.history, key=lambda h: h["score"])

        param_importance = self._compute_param_importance(opt)

        # Replay best params to get full stats
        best_replay = replay_all_trades(self.trades, best_params)

        return OptimizationResult(
            best_params=best_params,
            best_score=best_score,
            best_win_rate=best_replay.get("win_rate", 0.0),
            best_avg_pips=best_replay.get("avg_pips", 0.0),
            best_total_pips=best_replay.get("total_pips", 0.0),
            best_remaining_trades=best_replay.get("remaining", 0),
            baseline_score=baseline_score,
            baseline_win_rate=baseline_wr,
            baseline_avg_pips=baseline_avg_pips,
            evaluations=len(self.history),
            elapsed_seconds=time.time() - t0,
            history=list(self.history),
            param_importance=param_importance,
        )
