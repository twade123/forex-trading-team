"""
Results pipeline for the forex parameter optimizer.

Connects OptimizerEngine output to the tuning_config.propose_change() flow
and generates markdown reports for the dashboard.

Usage:
    python -m optimizer.results [--n-calls 500] [--tier 1] [--params rsi_min stoch_min]
                                [--no-proposals]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path setup — ensure Source/ is on sys.path when run as __main__
# ---------------------------------------------------------------------------

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


# ---------------------------------------------------------------------------
# Trade snapshot loader
# ---------------------------------------------------------------------------

def load_trade_snapshots(days_back: Optional[int] = None):
    """Load closed trades from live_trades and return as TradeSnapshot objects.

    Queries live_trades for closed trades (exit_time IS NOT NULL) with
    valid entry_price and pnl_pips.  The ``outcome`` field is derived from
    pnl_pips when the stored value is NULL.

    Args:
        days_back: If set, only load trades whose exit_time is within the
            last N days. Used to focus the optimizer on a recent system
            state (e.g. after a major fix landed). Pass None for all-time.

    Returns:
        List[TradeSnapshot]
    """
    from db_pool import get_trading_forex
    from optimizer.replay import TradeSnapshot

    conn = get_trading_forex()
    conn.row_factory = __import__("sqlite3").Row
    try:
        if days_back is not None:
            sql = """
                SELECT id, pair, direction, outcome, pnl_pips, realized_pl,
                       fan_state, bb_width, rsi, stoch_k, stoch_d, story_score,
                       atr, adx, confidence, entry_price, sl_price, tp_price,
                       max_favorable_excursion_pips, max_adverse_excursion_pips,
                       session, source, trend_health, fan_direction, fan_ordered,
                       momentum_state, entry_time
                FROM live_trades
                WHERE exit_time IS NOT NULL
                  AND entry_price IS NOT NULL
                  AND entry_price > 0
                  AND pnl_pips IS NOT NULL
                  AND exit_time >= datetime('now', ?)
                ORDER BY entry_time
            """
            rows = conn.execute(sql, (f"-{int(days_back)} days",)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, pair, direction, outcome, pnl_pips, realized_pl,
                       fan_state, bb_width, rsi, stoch_k, stoch_d, story_score,
                       atr, adx, confidence, entry_price, sl_price, tp_price,
                       max_favorable_excursion_pips, max_adverse_excursion_pips,
                       session, source, trend_health, fan_direction, fan_ordered,
                       momentum_state, entry_time
                FROM live_trades
                WHERE exit_time IS NOT NULL
                  AND entry_price IS NOT NULL
                  AND entry_price > 0
                  AND pnl_pips IS NOT NULL
                ORDER BY entry_time
            """).fetchall()
    finally:
        conn.close()

    snapshots: List[TradeSnapshot] = []
    for r in rows:
        pnl = r["pnl_pips"]
        outcome = r["outcome"]
        if outcome is None:
            outcome = "win" if (pnl is not None and pnl > 0) else "loss"

        snapshots.append(TradeSnapshot(
            id=str(r["id"]),
            pair=r["pair"] or "",
            direction=r["direction"] or "buy",
            outcome=outcome,
            pnl_pips=float(pnl),
            realized_pl=float(r["realized_pl"]) if r["realized_pl"] is not None else 0.0,
            fan_state=r["fan_state"] or "stable",
            bb_width=r["bb_width"],
            rsi=r["rsi"],
            stoch_k=r["stoch_k"],
            stoch_d=r["stoch_d"],
            story_score=r["story_score"],
            atr=r["atr"],
            adx=r["adx"],
            confidence=r["confidence"],
            entry_price=float(r["entry_price"]),
            sl_price=r["sl_price"],
            tp_price=r["tp_price"],
            mfe=r["max_favorable_excursion_pips"],
            mae=r["max_adverse_excursion_pips"],
            session=r["session"],
            source=r["source"],
            trend_health=r["trend_health"],
            fan_direction=r["fan_direction"],
            fan_ordered=bool(r["fan_ordered"]) if r["fan_ordered"] is not None else None,
            momentum_state=r["momentum_state"],
            entry_time=r["entry_time"],
        ))

    logger.info("[OPTIMIZER] Loaded %d trade snapshots", len(snapshots))
    return snapshots


# ---------------------------------------------------------------------------
# Main optimization runner
# ---------------------------------------------------------------------------

def run_optimization(
    n_calls: int = 500,
    tier_filter: Optional[int] = None,
    param_filter: Optional[List[str]] = None,
    use_candle_walk: bool = False,
    engine_version: str = "v2",
    apply_spread: bool = False,
    use_time_decay: bool = False,
    days_back: Optional[int] = None,
):
    """Run parameter optimization over historical trades.

    Args:
        n_calls:        Number of optimizer evaluations.
        tier_filter:    If set, only optimize params with tier <= tier_filter.
        param_filter:   If set, only optimize these named params.
        use_candle_walk: Fetch M15 candles for high-fidelity candle-walk replay.
        engine_version: "v1" for GP (skopt), "v2" for Optuna TPE.
        apply_spread:   Deduct session-aware spread costs from simulated P&L.
        use_time_decay: Weight recent trades more heavily (half-life ~69 days).

    Returns:
        OptimizationResult

    Raises:
        ValueError: Fewer than 30 closed trades available.
    """
    from tuning_config import get_optimizable_params

    snapshots = load_trade_snapshots(days_back=days_back)
    snapshots = [
        t for t in snapshots
        if not (t.pnl_pips == 0 and (t.mfe or 0) > 20)
    ]
    if len(snapshots) < 30:
        raise ValueError(
            f"Need at least 30 closed trades to optimize; only {len(snapshots)} "
            f"available{f' in last {days_back} days' if days_back else ''}."
        )
    if days_back is not None:
        logger.info("[OPTIMIZER] Filtered to last %d days: %d trades", days_back, len(snapshots))

    all_params = get_optimizable_params()

    params = {}
    for name, meta in all_params.items():
        if tier_filter is not None and meta.get("tier", 0) > tier_filter:
            continue
        if param_filter and name not in param_filter:
            continue
        params[name] = meta

    if not params:
        raise ValueError("No optimizable params remain after applying filters.")

    candles_by_trade = None
    if use_candle_walk:
        from optimizer.replay import load_candles_for_trades
        logger.info("[OPTIMIZER] Loading M15 candles for candle-walk replay...")
        candles_by_trade = load_candles_for_trades(snapshots)
        logger.info("[OPTIMIZER] Loaded candles for %d / %d trades",
                    len(candles_by_trade), len(snapshots))

    logger.info(
        "[OPTIMIZER] Starting optimization: %d snapshots, %d params, %d calls, "
        "engine=%s, candle_walk=%s, spread=%s, time_decay=%s",
        len(snapshots), len(params), n_calls, engine_version,
        use_candle_walk, apply_spread, use_time_decay,
    )

    if engine_version == "v2":
        from optimizer.engine_v2 import OptunaEngine
        engine = OptunaEngine(
            snapshots, params, n_trials=n_calls,
            candles_by_trade=candles_by_trade,
            apply_spread=apply_spread,
            use_time_decay=use_time_decay,
        )
    else:
        from optimizer.engine import OptimizerEngine
        engine = OptimizerEngine(
            snapshots, params, n_calls=n_calls,
            candles_by_trade=candles_by_trade,
        )

    result = engine.run()
    return result, snapshots, params, candles_by_trade


# ---------------------------------------------------------------------------
# Proposal creation
# ---------------------------------------------------------------------------

def create_proposals_from_result(result, min_improvement: float = 2.0) -> List[int]:
    """Create tuning proposals for params that improved meaningfully.

    Args:
        result:          OptimizationResult from run_optimization().
        min_improvement: Minimum win-rate improvement (percentage points) needed
                         before any proposals are created.

    Returns:
        List of proposal IDs (empty list if improvement below threshold).
    """
    from tuning_config import propose_change, get_optimizable_params, TUNING

    improvement = result.best_win_rate - result.baseline_win_rate
    if improvement < min_improvement:
        logger.info(
            "[OPTIMIZER] Win-rate improvement %.2f%% below threshold %.2f%% — no proposals",
            improvement, min_improvement,
        )
        return []

    params = get_optimizable_params()
    proposal_ids: List[int] = []

    for param, optimal_val in result.best_params.items():
        if param not in params:
            continue
        current_val = params[param]["value"]

        # Only propose if the change is meaningful (>5% of current or absolute change).
        # Some TUNING entries hold non-numeric current values (strings, lists, descriptive
        # placeholders like "absent — no rule existed"). Skip those — the optimizer only
        # tunes scalar numeric params, so a non-numeric current means this param isn't
        # genuinely optimizable here. Without this guard the run crashes mid-loop and the
        # markdown report never gets written (2026-05-06 incident).
        if not isinstance(current_val, (int, float)) or isinstance(current_val, bool):
            logger.debug("[OPTIMIZER] Skipping non-numeric param %s (current=%r)", param, current_val)
            continue
        if not isinstance(optimal_val, (int, float)) or isinstance(optimal_val, bool):
            logger.debug("[OPTIMIZER] Skipping non-numeric optimal for %s (optimal=%r)", param, optimal_val)
            continue
        if current_val == 0:
            changed = optimal_val != 0
        else:
            changed = abs(optimal_val - current_val) / abs(current_val) > 0.05

        if not changed:
            continue

        reason = (
            f"Bayesian optimizer: {param} {current_val} → {optimal_val} "
            f"(+{improvement:.1f}pp win rate over {result.evaluations} evals, "
            f"baseline {result.baseline_win_rate:.1f}% → optimized {result.best_win_rate:.1f}%)"
        )
        try:
            pid = propose_change(param, optimal_val, reason)
            proposal_ids.append(pid)
            logger.info("[OPTIMIZER] Created proposal #%d: %s %s → %s", pid, param, current_val, optimal_val)
        except Exception as e:
            logger.warning("[OPTIMIZER] Failed to create proposal for %s: %s", param, e)

    return proposal_ids


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def save_optimization_report(result, proposals: List[int]) -> str:
    """Write a markdown optimizer report and return the file path.

    Args:
        result:    OptimizationResult
        proposals: List of proposal IDs created from this result.

    Returns:
        Absolute path to the written .md file.
    """
    date_str = datetime.now().strftime("%Y-%m-%d")

    # Resolve Reports directory (sibling of Source/)
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proj_dir = os.path.dirname(src_dir)
    reports_dir = os.path.join(proj_dir, "Reports")
    os.makedirs(reports_dir, exist_ok=True)

    report_path = os.path.join(reports_dir, f"optimizer_report_{date_str}.md")

    improvement = result.best_win_rate - result.baseline_win_rate
    elapsed_min = result.elapsed_seconds / 60.0

    lines = [
        f"# Optimizer Report — {date_str}",
        "",
        "## Summary",
        "",
        f"- **Baseline win rate:** {result.baseline_win_rate:.2f}%",
        f"- **Optimized win rate:** {result.best_win_rate:.2f}%",
        f"- **Improvement:** {improvement:+.2f}pp",
        f"- **Evaluations:** {result.evaluations}",
        f"- **Elapsed:** {elapsed_min:.1f} min",
        f"- **Best score:** {result.best_score:.4f}",
        f"- **Best avg pips:** {result.best_avg_pips:.2f}",
        f"- **Trades retained:** {result.best_remaining_trades}",
        f"- **Proposals created:** {len(proposals)}",
        "",
        "## Optimal Parameters",
        "",
        "| Parameter | Current | Optimal | Change |",
        "|-----------|---------|---------|--------|",
    ]

    try:
        from tuning_config import get_optimizable_params
        params_meta = get_optimizable_params()
    except Exception:
        params_meta = {}

    for param, optimal in sorted(result.best_params.items()):
        current = params_meta.get(param, {}).get("value", "?")
        if current != "?" and isinstance(current, float):
            change = f"{optimal - current:+.4f}"
        elif current != "?" and isinstance(current, (int, float)):
            change = f"{optimal - current:+g}"
        else:
            change = "—"
        lines.append(f"| {param} | {current} | {optimal} | {change} |")

    lines += [
        "",
        "## Parameter Importance",
        "",
        "| Parameter | Importance |",
        "|-----------|------------|",
    ]
    for param, importance in sorted(
        result.param_importance.items(), key=lambda x: x[1], reverse=True
    ):
        lines.append(f"| {param} | {importance:.4f} |")

    lines += [
        "",
        "## Top 10 Evaluations",
        "",
        "| # | Win Rate | Avg Pips | Score | Trades |",
        "|---|----------|----------|-------|--------|",
    ]
    top = sorted(result.history, key=lambda x: x.get("score", 0), reverse=True)[:10]
    for i, ev in enumerate(top, 1):
        lines.append(
            f"| {i} | {ev.get('win_rate', 0):.1f}% | {ev.get('avg_pips', 0):.2f} "
            f"| {ev.get('score', 0):.4f} | {ev.get('remaining', 0)} |"
        )

    if proposals:
        lines += [
            "",
            "## Proposals Created",
            "",
        ]
        for pid in proposals:
            lines.append(f"- Proposal #{pid} (pending Tim review in QA Audit panel)")

    lines += ["", f"*Generated {datetime.now().isoformat()}*", ""]

    with open(report_path, "w") as f:
        f.write("\n".join(lines))

    logger.info("[OPTIMIZER] Report saved: %s", report_path)
    return report_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Run the forex parameter optimizer and create tuning proposals."
    )
    parser.add_argument("--n-calls", type=int, default=500,
                        help="Number of optimizer evaluations (default: 500)")
    parser.add_argument("--tier", type=int, default=None,
                        help="Only optimize params at or below this tier")
    parser.add_argument("--params", nargs="+", default=None,
                        help="Specific param names to optimize")
    parser.add_argument("--no-proposals", action="store_true",
                        help="Skip creating tuning proposals")
    parser.add_argument("--candle-walk", action="store_true",
                        help="Use M15 candle-walk replay (high fidelity)")
    parser.add_argument("--engine", choices=["v1", "v2"], default="v2",
                        help="v1=GP (skopt), v2=Optuna TPE (default)")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Run walk-forward cross-validation after optimization")
    parser.add_argument("--wf-folds", type=int, default=8,
                        help="Number of walk-forward folds (default: 8)")
    parser.add_argument("--robustness", action="store_true",
                        help="Run perturbation + bootstrap + Monte Carlo tests")
    parser.add_argument("--time-decay", action="store_true",
                        help="Weight recent trades more (half-life ~69 days)")
    parser.add_argument("--apply-spread", action="store_true",
                        help="Deduct session-aware spread costs")
    parser.add_argument("--days-back", type=int, default=None,
                        help="Only optimize against trades from the last N days. "
                             "Use this to focus on a specific system state (e.g. "
                             "after a major fix). Default: all-time.")
    args = parser.parse_args()

    try:
        print("Loading trade snapshots...")
        result, snapshots, params, candles_by_trade = run_optimization(
            n_calls=args.n_calls,
            tier_filter=args.tier,
            param_filter=args.params,
            use_candle_walk=args.candle_walk,
            engine_version=args.engine,
            apply_spread=args.apply_spread,
            use_time_decay=args.time_decay,
            days_back=args.days_back,
        )

        improvement = result.best_win_rate - result.baseline_win_rate
        print(f"\n=== Optimization Complete ({args.engine} engine) ===")
        print(f"  Evaluations : {result.evaluations}")
        print(f"  Elapsed     : {result.elapsed_seconds:.1f}s")
        print(f"  Baseline WR : {result.baseline_win_rate:.2f}%")
        print(f"  Optimized WR: {result.best_win_rate:.2f}%")
        print(f"  Improvement : {improvement:+.2f}pp")
        print(f"  Best score  : {result.best_score:.4f}")
        print(f"  Trades kept : {result.best_remaining_trades}")

        # Walk-forward cross-validation
        wf_result = None
        if args.walk_forward:
            from optimizer.walk_forward import run_walk_forward
            print(f"\n--- Walk-Forward CV ({args.wf_folds} folds) ---")
            wf_result = run_walk_forward(
                snapshots, params, candles_by_trade,
                n_folds=args.wf_folds,
                n_trials_per_fold=min(args.n_calls, 200),
            )
            print(f"  Mean OOS WR:       {wf_result.mean_oos_wr:.1f}% (std={wf_result.std_oos_wr:.1f}pp)")
            print(f"  Overfitting ratio: {wf_result.overfitting_ratio:.2f}")
            print(f"  PBO:               {wf_result.pbo:.2f}")
            print(f"  Mean degradation:  {wf_result.mean_degradation_pp:.1f}pp")

            if wf_result.unstable_params:
                print(f"\n  UNSTABLE PARAMS ({len(wf_result.unstable_params)}):")
                for name in wf_result.unstable_params[:10]:
                    s = wf_result.param_stability[name]
                    print(f"    {name}: CV={s['cv']:.2f} values={s['values_across_folds']}")

            print(f"\n  Per-fold breakdown:")
            for f in wf_result.fold_results:
                print(f"    Fold {f['fold_id']}: IS={f['in_sample_wr']:.1f}% → OOS={f['oos_wr']:.1f}% "
                      f"(n_train={f['n_train']}, n_test={f['n_test']})")

        # Robustness checks
        robustness = None
        if args.robustness:
            from optimizer.robustness import run_robustness_check
            print(f"\n--- Robustness Suite ---")
            robustness = run_robustness_check(
                result.best_params, snapshots, params,
                candles_by_trade=candles_by_trade,
            )
            print(f"\n  Perturbation: {len(robustness.flagged_params)} fragile params")
            if robustness.flagged_params:
                print(f"    Flagged: {', '.join(robustness.flagged_params[:10])}")

            ci = robustness.bootstrap_ci
            print(f"  Bootstrap CI: 5th={ci['p5']:.1f}% 50th={ci['p50']:.1f}% 95th={ci['p95']:.1f}%")

            mc = robustness.monte_carlo
            if mc:
                print(f"  Monte Carlo:  real={mc.real_wr:.1f}% random_mean={mc.mean_random_wr:.1f}% "
                      f"p={mc.p_value:.4f} {'SIGNIFICANT' if mc.is_significant else 'NOT significant'}")

            print(f"\n  Overall robust: {'YES' if robustness.is_robust else 'NO'}")

        # Proposals
        proposals: List[int] = []
        if not args.no_proposals:
            proposals = create_proposals_from_result(result)
            if proposals:
                print(f"\n  Created {len(proposals)} proposal(s): {proposals}")
            else:
                print("\n  No proposals created (below threshold)")
        else:
            print("\n  --no-proposals: skipping")

        report_path = save_optimization_report(result, proposals)
        print(f"\n  Report: {report_path}")

    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logger.exception("Optimizer failed")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
