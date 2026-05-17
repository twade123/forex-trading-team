"""
Robustness testing for optimized parameters.

Three tests:
1. Parameter Perturbation — perturb each param ±5/10/20%, flag if WR changes >3pp
2. Bootstrap Confidence Interval — resample 1000x, report 5th/50th/95th percentile WR
3. Monte Carlo Permutation — shuffle outcomes 500x, test if real WR beats random
"""

from __future__ import annotations

import copy
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from optimizer.replay import TradeSnapshot, replay_all_trades

logger = logging.getLogger("optimizer.robustness")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MonteCarloResult:
    """Monte Carlo permutation test results."""
    real_wr: float
    mean_random_wr: float
    percentile_rank: float
    p_value: float
    is_significant: bool
    n_permutations: int
    random_distribution: List[float]


@dataclass
class RobustnessReport:
    """Complete robustness test results."""
    perturbation_results: Dict[str, List[Dict]]
    flagged_params: List[str]
    bootstrap_ci: Dict[str, float]
    monte_carlo: Optional[MonteCarloResult]
    is_robust: bool


# ---------------------------------------------------------------------------
# Parameter perturbation
# ---------------------------------------------------------------------------

def parameter_perturbation(
    best_params: Dict[str, Any],
    trades: List[TradeSnapshot],
    param_meta: Dict[str, Dict],
    candles_by_trade: Optional[dict] = None,
    perturbation_pcts: List[float] = None,
    threshold_pp: float = 3.0,
) -> Tuple[Dict[str, List[Dict]], List[str]]:
    """Perturb each param by +/- pct, flag if WR changes > threshold_pp.

    Returns (per_param_results, flagged_param_names).
    """
    if perturbation_pcts is None:
        perturbation_pcts = [0.05, 0.10, 0.20]

    # Baseline WR
    baseline = replay_all_trades(trades, best_params, candles_by_trade=candles_by_trade)
    baseline_wr = baseline.get("win_rate", 0.0)

    results = {}
    flagged = set()
    total_evals = len(best_params) * len(perturbation_pcts) * 2
    eval_count = 0

    for name, val in best_params.items():
        param_results = []
        meta = param_meta.get(name, {})
        lo = float(meta.get("min", val * 0.5))
        hi = float(meta.get("max", val * 2.0))

        for pct in perturbation_pcts:
            for sign in [1, -1]:
                perturbed = dict(best_params)
                new_val = val * (1 + sign * pct)
                # Clamp to valid range
                new_val = max(lo, min(hi, new_val))
                if meta.get("is_int", False):
                    new_val = int(round(new_val))
                perturbed[name] = new_val

                result = replay_all_trades(trades, perturbed, candles_by_trade=candles_by_trade)
                wr = result.get("win_rate", 0.0)
                wr_delta = wr - baseline_wr

                param_results.append({
                    "pct": sign * pct,
                    "original": val,
                    "perturbed": new_val,
                    "wr": round(wr, 2),
                    "wr_delta": round(wr_delta, 2),
                    "flagged": abs(wr_delta) > threshold_pp,
                })

                if abs(wr_delta) > threshold_pp:
                    flagged.add(name)

                eval_count += 1
                if eval_count % 50 == 0:
                    logger.info("[PERTURBATION] %d/%d evals", eval_count, total_evals)

        results[name] = param_results

    flagged_list = sorted(flagged)
    logger.info(
        "[PERTURBATION] Done: %d params tested, %d flagged (threshold=%.1fpp)",
        len(best_params), len(flagged_list), threshold_pp,
    )
    return results, flagged_list


# ---------------------------------------------------------------------------
# Bootstrap confidence interval
# ---------------------------------------------------------------------------

def bootstrap_confidence_interval(
    best_params: Dict[str, Any],
    trades: List[TradeSnapshot],
    candles_by_trade: Optional[dict] = None,
    n_bootstrap: int = 1000,
    seed: int = 42,
) -> Dict[str, float]:
    """Resample trades with replacement, report WR percentiles."""
    rng = random.Random(seed)
    wrs = []

    for i in range(n_bootstrap):
        sample = rng.choices(trades, k=len(trades))
        # Build candle subset for sampled trades
        sample_candles = None
        if candles_by_trade:
            sample_candles = {t.id: candles_by_trade[t.id] for t in sample if t.id in candles_by_trade}
            if not sample_candles:
                sample_candles = None

        result = replay_all_trades(sample, best_params, candles_by_trade=sample_candles)
        wrs.append(result.get("win_rate", 0.0))

        if (i + 1) % 200 == 0:
            logger.info("[BOOTSTRAP] %d/%d samples", i + 1, n_bootstrap)

    wrs.sort()
    n = len(wrs)

    ci = {
        "p5": round(wrs[int(0.05 * n)], 2),
        "p50": round(wrs[n // 2], 2),
        "p95": round(wrs[int(0.95 * n)], 2),
        "mean": round(sum(wrs) / n, 2),
    }

    logger.info(
        "[BOOTSTRAP] Done: mean=%.1f%%, 5th=%.1f%%, 50th=%.1f%%, 95th=%.1f%%",
        ci["mean"], ci["p5"], ci["p50"], ci["p95"],
    )
    return ci


# ---------------------------------------------------------------------------
# Monte Carlo permutation test
# ---------------------------------------------------------------------------

def monte_carlo_permutation(
    trades: List[TradeSnapshot],
    params: Dict[str, Any],
    n_permutations: int = 500,
    candles_by_trade: Optional[dict] = None,
    seed: int = 42,
) -> MonteCarloResult:
    """Shuffle trade outcomes and test if optimized WR beats random.

    Keeps same trade features but randomly shuffles which are wins vs losses.
    If real WR doesn't exceed 95th percentile of shuffled WRs, the optimizer
    may be fitting noise rather than signal.
    """
    rng = random.Random(seed)

    # Real result
    real_result = replay_all_trades(trades, params, candles_by_trade=candles_by_trade)
    real_wr = real_result.get("win_rate", 0.0)

    # Collect outcomes + pnls for shuffling
    outcomes = [t.outcome for t in trades]
    pnls = [t.pnl_pips for t in trades]

    random_wrs = []
    for i in range(n_permutations):
        # Shuffle outcomes + pnls together
        combined = list(zip(outcomes, pnls))
        rng.shuffle(combined)
        shuffled_outcomes, shuffled_pnls = zip(*combined) if combined else ([], [])

        # Create modified snapshots with shuffled outcomes
        shuffled_trades = []
        for t, new_outcome, new_pnl in zip(trades, shuffled_outcomes, shuffled_pnls):
            st = copy.copy(t)
            st.outcome = new_outcome
            st.pnl_pips = new_pnl
            shuffled_trades.append(st)

        result = replay_all_trades(shuffled_trades, params, candles_by_trade=candles_by_trade)
        random_wrs.append(result.get("win_rate", 0.0))

        if (i + 1) % 100 == 0:
            logger.info("[MONTE_CARLO] %d/%d permutations", i + 1, n_permutations)

    random_wrs.sort()
    n = len(random_wrs)
    mean_random = sum(random_wrs) / n

    below_count = sum(1 for rw in random_wrs if rw < real_wr)
    percentile = below_count / n * 100.0
    p_value = 1.0 - (below_count / n)

    logger.info(
        "[MONTE_CARLO] Real WR=%.1f%%, Random mean=%.1f%%, Percentile=%.1fth, p=%.4f",
        real_wr, mean_random, percentile, p_value,
    )

    return MonteCarloResult(
        real_wr=round(real_wr, 2),
        mean_random_wr=round(mean_random, 2),
        percentile_rank=round(percentile, 1),
        p_value=round(p_value, 4),
        is_significant=p_value < 0.05,
        n_permutations=n_permutations,
        random_distribution=random_wrs,
    )


# ---------------------------------------------------------------------------
# Full robustness suite
# ---------------------------------------------------------------------------

def run_robustness_check(
    best_params: Dict[str, Any],
    trades: List[TradeSnapshot],
    param_meta: Dict[str, Dict],
    candles_by_trade: Optional[dict] = None,
    n_bootstrap: int = 1000,
    n_permutations: int = 500,
    seed: int = 42,
) -> RobustnessReport:
    """Run all three robustness tests and return unified report."""
    logger.info("[ROBUSTNESS] Starting full suite: perturbation + bootstrap + Monte Carlo")

    # 1. Perturbation
    perturbation_results, flagged = parameter_perturbation(
        best_params, trades, param_meta, candles_by_trade=candles_by_trade,
    )

    # 2. Bootstrap
    bootstrap_ci = bootstrap_confidence_interval(
        best_params, trades, candles_by_trade=candles_by_trade,
        n_bootstrap=n_bootstrap, seed=seed,
    )

    # 3. Monte Carlo
    mc = monte_carlo_permutation(
        trades, best_params, n_permutations=n_permutations,
        candles_by_trade=candles_by_trade, seed=seed,
    )

    # Overall robustness assessment
    is_robust = (
        len(flagged) < 8
        and bootstrap_ci["p5"] > 52.0
        and mc.is_significant
    )

    logger.info(
        "[ROBUSTNESS] DONE: flagged=%d, bootstrap_5th=%.1f%%, MC p=%.4f, robust=%s",
        len(flagged), bootstrap_ci["p5"], mc.p_value, is_robust,
    )

    return RobustnessReport(
        perturbation_results=perturbation_results,
        flagged_params=flagged,
        bootstrap_ci=bootstrap_ci,
        monte_carlo=mc,
        is_robust=is_robust,
    )
