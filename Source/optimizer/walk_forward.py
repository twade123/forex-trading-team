"""
Walk-Forward Cross-Validation for the forex parameter optimizer.

Splits trades chronologically into folds, optimizes on train set per fold,
evaluates on held-out test set. Reports true out-of-sample WR, overfitting
ratio, Probability of Backtest Overfitting (PBO), and parameter stability.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import optuna

from optimizer.engine import OptimizationResult
from optimizer.engine_v2 import OptunaEngine, composite_score_v2
from optimizer.replay import TradeSnapshot, replay_all_trades

logger = logging.getLogger("optimizer.walk_forward")

MIN_TEST_TRADES = 15
MIN_TRAIN_TRADES = 30


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class WalkForwardFold:
    fold_id: int
    train_trades: List[TradeSnapshot]
    test_trades: List[TradeSnapshot]
    train_candles: Optional[dict]
    test_candles: Optional[dict]
    purge_count: int


@dataclass
class WalkForwardResult:
    fold_results: List[Dict[str, Any]]
    mean_oos_wr: float
    std_oos_wr: float
    mean_oos_score: float
    best_consensus_params: Dict[str, Any]
    overfitting_ratio: float
    pbo: float
    mean_degradation_pp: float
    param_stability: Dict[str, Dict]
    unstable_params: List[str]


# ---------------------------------------------------------------------------
# Time parsing helper
# ---------------------------------------------------------------------------

def _parse_time(time_str: str) -> datetime:
    """Parse entry_time string to naive UTC datetime."""
    import pandas as pd
    dt = pd.Timestamp(time_str).to_pydatetime()
    # Strip timezone to avoid naive/aware comparison errors
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# Fold creation with time-based purge
# ---------------------------------------------------------------------------

def create_walk_forward_folds(
    trades: List[TradeSnapshot],
    candles_by_trade: Optional[dict],
    n_folds: int = 8,
    purge_hours: float = 4.0,
) -> List[WalkForwardFold]:
    """Split trades chronologically with time-based purging.

    Uses anchored walk-forward: train on all folds before test fold.
    Purge: remove training trades within purge_hours of first test
    trade's entry_time to prevent label leakage.
    """
    fold_size = len(trades) // n_folds
    chunks = []
    for i in range(n_folds):
        start = i * fold_size
        end = start + fold_size if i < n_folds - 1 else len(trades)
        chunks.append(trades[start:end])

    results = []
    for test_idx in range(1, n_folds):
        # Anchored: train on all chunks before test chunk
        train_trades = []
        for fi in range(test_idx):
            train_trades.extend(chunks[fi])

        test_trades = chunks[test_idx]

        if not test_trades or not test_trades[0].entry_time:
            # No time data — skip purge
            train_candles = _subset_candles(candles_by_trade, train_trades)
            test_candles = _subset_candles(candles_by_trade, test_trades)
            results.append(WalkForwardFold(
                fold_id=test_idx,
                train_trades=train_trades,
                test_trades=test_trades,
                train_candles=train_candles,
                test_candles=test_candles,
                purge_count=0,
            ))
            continue

        # TIME-BASED PURGE
        test_start = _parse_time(test_trades[0].entry_time)
        purge_cutoff = test_start - timedelta(hours=purge_hours)

        original_len = len(train_trades)
        train_trades = [
            t for t in train_trades
            if t.entry_time is None or _parse_time(t.entry_time) < purge_cutoff
        ]
        purge_count = original_len - len(train_trades)

        if purge_count > 0:
            logger.info("[WF-CV] Fold %d: purged %d boundary trades", test_idx, purge_count)

        # Size warnings
        if len(test_trades) < MIN_TEST_TRADES:
            logger.warning(
                "[WF-CV] Fold %d has only %d test trades (min %d). "
                "Consider fewer folds.", test_idx, len(test_trades), MIN_TEST_TRADES,
            )
        if len(train_trades) < MIN_TRAIN_TRADES:
            logger.warning(
                "[WF-CV] Fold %d has only %d train trades (min %d). "
                "Results may be unreliable.", test_idx, len(train_trades), MIN_TRAIN_TRADES,
            )

        train_candles = _subset_candles(candles_by_trade, train_trades)
        test_candles = _subset_candles(candles_by_trade, test_trades)

        results.append(WalkForwardFold(
            fold_id=test_idx,
            train_trades=train_trades,
            test_trades=test_trades,
            train_candles=train_candles,
            test_candles=test_candles,
            purge_count=purge_count,
        ))

    return results


def _subset_candles(candles_by_trade: Optional[dict], trades: List[TradeSnapshot]) -> Optional[dict]:
    """Build candle subset for trades. Returns None if no candles available."""
    if not candles_by_trade:
        return None
    subset = {t.id: candles_by_trade[t.id] for t in trades if t.id in candles_by_trade}
    return subset if subset else None


# ---------------------------------------------------------------------------
# Parameter stability
# ---------------------------------------------------------------------------

def compute_param_stability(fold_results: List[Dict]) -> Dict[str, Dict]:
    """Measure parameter stability across walk-forward folds.

    Returns per-param: mean, std, cv (coefficient of variation), stable (CV < 0.30).
    """
    if len(fold_results) < 2:
        return {}

    param_names = list(fold_results[0]["best_params"].keys())
    stability = {}

    for name in param_names:
        values = [
            f["best_params"].get(name) for f in fold_results
            if name in f.get("best_params", {})
        ]
        values = [v for v in values if v is not None]

        if len(values) < 2:
            stability[name] = {"mean": values[0] if values else 0, "std": 0, "cv": 0, "stable": True}
            continue

        mean = statistics.mean(values)
        std = statistics.stdev(values)
        cv = std / abs(mean) if mean != 0 else float("inf")

        stability[name] = {
            "mean": round(mean, 4),
            "std": round(std, 4),
            "cv": round(cv, 4),
            "stable": cv < 0.30,
            "values_across_folds": [round(v, 4) for v in values],
        }

    return stability


# ---------------------------------------------------------------------------
# Main walk-forward runner
# ---------------------------------------------------------------------------

def run_walk_forward(
    trades: List[TradeSnapshot],
    params: Dict[str, Dict],
    candles_by_trade: Optional[dict] = None,
    n_folds: int = 8,
    n_trials_per_fold: int = 200,
    purge_hours: float = 4.0,
    seed: int = 42,
) -> WalkForwardResult:
    """Run walk-forward cross-validation.

    For each fold: optimize on train set, evaluate best params on test set.
    Reports mean OOS WR, overfitting ratio, PBO, and param stability.
    """
    folds = create_walk_forward_folds(trades, candles_by_trade, n_folds, purge_hours)

    if not folds:
        raise ValueError("No folds created — need at least 2 folds")

    # Suppress Optuna logging during nested optimization
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    fold_results = []
    for fold in folds:
        logger.info(
            "[WF-CV] Fold %d: train=%d test=%d purged=%d",
            fold.fold_id, len(fold.train_trades), len(fold.test_trades), fold.purge_count,
        )

        fold_seed = seed + fold.fold_id

        engine = OptunaEngine(
            trades=fold.train_trades,
            params=params,
            n_trials=n_trials_per_fold,
            n_startup_trials=min(30, n_trials_per_fold // 3),
            seed=fold_seed,
            candles_by_trade=fold.train_candles,
            study_name=f"wf_fold_{fold.fold_id}_{seed}",
        )
        result = engine.run()

        # Evaluate best params on held-out test set
        test_replay = replay_all_trades(
            fold.test_trades, result.best_params,
            candles_by_trade=fold.test_candles,
        )
        test_score = composite_score_v2(test_replay)

        fold_results.append({
            "fold_id": fold.fold_id,
            "in_sample_wr": result.best_win_rate,
            "oos_wr": test_replay.get("win_rate", 0.0),
            "in_sample_score": result.best_score,
            "oos_score": test_score,
            "best_params": result.best_params,
            "n_train": len(fold.train_trades),
            "n_test": len(fold.test_trades),
            "purge_count": fold.purge_count,
        })

        logger.info(
            "[WF-CV] Fold %d: IS WR=%.1f%% → OOS WR=%.1f%% (degradation: %.1fpp)",
            fold.fold_id,
            result.best_win_rate,
            test_replay.get("win_rate", 0.0),
            result.best_win_rate - test_replay.get("win_rate", 0.0),
        )

    # Compute aggregate metrics
    oos_wrs = [f["oos_wr"] for f in fold_results]
    is_wrs = [f["in_sample_wr"] for f in fold_results]

    mean_oos = statistics.mean(oos_wrs)
    std_oos = statistics.stdev(oos_wrs) if len(oos_wrs) > 1 else 0.0
    mean_oos_score = statistics.mean([f["oos_score"] for f in fold_results])
    mean_is = statistics.mean(is_wrs)

    # Overfitting ratio: IS / OOS (1.0 = no overfit, >1.5 = severe)
    overfitting_ratio = mean_is / mean_oos if mean_oos > 0 else float("inf")

    # PBO: fraction of folds where OOS < (median IS × 0.5)
    median_is = sorted(is_wrs)[len(is_wrs) // 2]
    pbo = sum(1 for oos in oos_wrs if oos < median_is * 0.5) / len(oos_wrs)

    # Mean degradation
    degradation = [is_wr - oos_wr for is_wr, oos_wr in zip(is_wrs, oos_wrs)]
    mean_degradation = statistics.mean(degradation)

    # Consensus params: median of each param across folds
    consensus = _median_params(fold_results)

    # Parameter stability
    stability = compute_param_stability(fold_results)
    unstable = [name for name, s in stability.items() if not s.get("stable", True)]

    logger.info(
        "[WF-CV] DONE: Mean OOS WR=%.1f%% (std=%.1fpp), Overfit ratio=%.2f, PBO=%.2f, "
        "Unstable params=%d/%d",
        mean_oos, std_oos, overfitting_ratio, pbo, len(unstable), len(stability),
    )

    return WalkForwardResult(
        fold_results=fold_results,
        mean_oos_wr=round(mean_oos, 2),
        std_oos_wr=round(std_oos, 2),
        mean_oos_score=round(mean_oos_score, 2),
        best_consensus_params=consensus,
        overfitting_ratio=round(overfitting_ratio, 3),
        pbo=round(pbo, 3),
        mean_degradation_pp=round(mean_degradation, 2),
        param_stability=stability,
        unstable_params=unstable,
    )


def _median_params(fold_results: List[Dict]) -> Dict[str, Any]:
    """Compute median of each parameter across folds."""
    if not fold_results:
        return {}

    param_names = list(fold_results[0]["best_params"].keys())
    consensus = {}

    for name in param_names:
        values = [f["best_params"][name] for f in fold_results if name in f["best_params"]]
        if not values:
            continue
        consensus[name] = statistics.median(values)

    return consensus
