"""
Comparative backtest analysis for strategy optimization.

Broker-agnostic: accepts any :class:`CandleProvider` (Oanda, Coinbase,
futures brokers).  Runs multiple backtest configurations and ranks results.
Includes EMA period comparison, candlestick and chart pattern success-rate
analysis, and cross-config performance ranking.

Usage:
    from Source.oanda_client import OandaClient
    from Source.backtest_analysis import BacktestAnalysis

    client = OandaClient()
    analysis = BacktestAnalysis(client, market_type='forex')
    report = analysis.run_full_analysis('EUR_USD', from_time, to_time)
    print(analysis.format_comparison_report(report))
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("trading.backtest_analysis")


class BacktestAnalysis:
    """Comparative backtest analysis for strategy optimization.

    Broker-agnostic: accepts any CandleProvider (Oanda, Coinbase,
    futures brokers). Runs multiple backtest configurations and ranks
    results. Includes EMA period comparison, pattern success rates,
    and cross-config performance ranking.

    Args:
        provider: Any CandleProvider implementation.
        market_type: Market type for BacktestConfig ('forex', 'crypto', 'futures').
        cache_dir: Optional cache directory for HistoricalDataFetcher.
    """

    # Default EMA period sets to compare (BKTS-05)
    _DEFAULT_EMA_SETS: List[Tuple[int, int, int]] = [
        (9, 21, 50),
        (21, 55, 100),
        (9, 21, 100),
    ]

    def __init__(
        self,
        provider,
        market_type: str = "forex",
        cache_dir: Optional[str] = None,
    ) -> None:
        from Source.historical_data import HistoricalDataFetcher

        self._provider = provider
        self._market_type = market_type
        self._fetcher = HistoricalDataFetcher(provider, cache_dir)

    # ------------------------------------------------------------------
    # EMA period comparison (BKTS-05)
    # ------------------------------------------------------------------

    def compare_ema_periods(
        self,
        instrument: str,
        from_time: datetime,
        to_time: datetime,
        period_sets: Optional[List[Tuple]] = None,
    ) -> Dict[str, Any]:
        """Compare multiple EMA period sets via walk-forward optimization.

        For each period set, creates a BacktestConfig, runs WalkForwardOptimizer,
        and collects train/test metrics.  Results are ranked by test-set Sharpe.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').
            from_time: Start of historical range.
            to_time: End of historical range.
            period_sets: List of (fast, mid, slow) EMA period tuples.
                Defaults to [(9,21,50), (21,55,100), (9,21,100)].

        Returns:
            Dict with instrument, market_type, period, results list,
            ranking, and recommendation.
        """
        from Source.backtester import BacktestConfig, Backtester, WalkForwardOptimizer

        if period_sets is None:
            period_sets = list(self._DEFAULT_EMA_SETS)

        # Fetch data once for all runs
        candle_data = self._fetch_all_timeframes(instrument, from_time, to_time)

        results: List[Dict[str, Any]] = []
        for ema_set in period_sets:
            config = BacktestConfig(
                market_type=self._market_type,
                ema_periods=ema_set,
            )
            bt = Backtester(config)
            wfo = WalkForwardOptimizer()
            wfo_result = wfo.run(candle_data, instrument, bt)

            results.append({
                "ema_set": ema_set,
                "train_metrics": wfo_result.get("train", {}).get("metrics", {}),
                "test_metrics": wfo_result.get("test", {}).get("metrics", {}),
                "overfit_check": wfo_result.get("overfit_check", {}),
            })

        # Rank by test-set Sharpe ratio descending
        ranked = sorted(
            results,
            key=lambda r: r["test_metrics"].get("sharpe_ratio", 0.0),
            reverse=True,
        )
        ranking = [r["ema_set"] for r in ranked]

        # Build recommendation
        best = ranked[0] if ranked else None
        if best:
            ema_str = "/".join(str(p) for p in best["ema_set"])
            sharpe = best["test_metrics"].get("sharpe_ratio", 0.0)
            dd = best["test_metrics"].get("max_drawdown", 0.0)
            recommendation = (
                f"{ema_str} -- best test Sharpe ({sharpe:.2f}) "
                f"with {dd * 100:.1f}% max drawdown"
            )
        else:
            recommendation = "No results available"

        return {
            "instrument": instrument,
            "market_type": self._market_type,
            "period": f"{from_time.isoformat()} to {to_time.isoformat()}",
            "results": ranked,
            "ranking": ranking,
            "recommendation": recommendation,
        }

    # ------------------------------------------------------------------
    # Candlestick pattern success rates (BKTS-08)
    # ------------------------------------------------------------------

    def analyze_candlestick_patterns(
        self,
        instrument: str,
        from_time: datetime,
        to_time: datetime,
        granularity: str = "H1",
        forward_bars: int = 20,
    ) -> Dict[str, Any]:
        """Calculate per-pattern success rates for candlestick patterns.

        Scans historical candles for TA-Lib pattern signals, then measures
        the forward outcome over *forward_bars* to classify each occurrence
        as win, loss, or neutral.

        Args:
            instrument: Instrument name.
            from_time: Start of range.
            to_time: End of range.
            granularity: Candle granularity (default 'H1').
            forward_bars: Bars to look ahead for outcome (default 20).

        Returns:
            Dict with instrument, market_type, granularity, and per-pattern
            statistics sorted by win_rate descending.
        """
        from Source.candlestick_patterns import CandlestickPatterns

        candles = self._fetcher.fetch(instrument, granularity, from_time, to_time)
        if len(candles) < 50:
            return {
                "instrument": instrument,
                "market_type": self._market_type,
                "granularity": granularity,
                "patterns": {},
                "error": "Insufficient candle data",
            }

        cp = CandlestickPatterns(candles)
        scan = cp.scan_all()

        # Compute simple ATR for outcome measurement
        atr = self._compute_simple_atr(candles)

        pattern_stats: Dict[str, Dict[str, Any]] = {}

        for func_name, output in scan.items():
            occurrences = 0
            wins = 0
            losses = 0
            neutral = 0
            total_move = 0.0

            for idx in range(len(output)):
                val = int(output[idx])
                if val == 0:
                    continue

                direction = "bullish" if val > 0 else "bearish"
                occurrences += 1

                # Measure forward outcome
                end_idx = min(idx + forward_bars, len(candles) - 1)
                if end_idx <= idx:
                    neutral += 1
                    continue

                entry_price = float(
                    candles[idx].get("mid", {}).get("c", 0)
                )
                exit_price = float(
                    candles[end_idx].get("mid", {}).get("c", 0)
                )

                if entry_price == 0 or exit_price == 0:
                    neutral += 1
                    continue

                move = exit_price - entry_price
                if direction == "bearish":
                    move = -move

                total_move += abs(exit_price - entry_price)

                if atr > 0 and move >= atr:
                    wins += 1
                elif atr > 0 and move <= -atr:
                    losses += 1
                else:
                    neutral += 1

            if occurrences > 0:
                human_name = CandlestickPatterns.PATTERN_NAMES.get(
                    func_name, func_name
                )
                pip_size = 0.01 if "_JPY" in instrument else 0.0001
                avg_move_pips = (total_move / occurrences) / pip_size

                pattern_stats[human_name] = {
                    "talib_name": func_name,
                    "occurrences": occurrences,
                    "wins": wins,
                    "losses": losses,
                    "neutral": neutral,
                    "win_rate": wins / occurrences,
                    "avg_move_pips": round(avg_move_pips, 1),
                }

        # Sort by win_rate descending
        sorted_patterns = dict(
            sorted(
                pattern_stats.items(),
                key=lambda item: item[1]["win_rate"],
                reverse=True,
            )
        )

        return {
            "instrument": instrument,
            "market_type": self._market_type,
            "granularity": granularity,
            "total_bars": len(candles),
            "forward_bars": forward_bars,
            "patterns": sorted_patterns,
        }

    # ------------------------------------------------------------------
    # Chart pattern success rates (BKTS-09)
    # ------------------------------------------------------------------

    def analyze_chart_patterns(
        self,
        instrument: str,
        from_time: datetime,
        to_time: datetime,
        granularity: str = "H1",
        forward_bars: int = 40,
    ) -> Dict[str, Any]:
        """Calculate per-pattern success rates for chart patterns.

        Runs chart pattern detection on sliding windows of historical data,
        then measures forward outcome over *forward_bars*.

        Args:
            instrument: Instrument name.
            from_time: Start of range.
            to_time: End of range.
            granularity: Candle granularity (default 'H1').
            forward_bars: Bars to look ahead for outcome (default 40).

        Returns:
            Dict with instrument, market_type, and per-pattern statistics
            sorted by win_rate descending.
        """
        from Source.chart_patterns import ChartPatterns

        candles = self._fetcher.fetch(instrument, granularity, from_time, to_time)
        if len(candles) < 100:
            return {
                "instrument": instrument,
                "market_type": self._market_type,
                "granularity": granularity,
                "patterns": {},
                "error": "Insufficient candle data",
            }

        atr = self._compute_simple_atr(candles)
        pattern_stats: Dict[str, Dict[str, Any]] = {}
        window_size = 200
        step = 20  # Slide by 20 bars each iteration

        for start_idx in range(0, len(candles) - window_size - forward_bars, step):
            window = candles[start_idx: start_idx + window_size]

            try:
                cp = ChartPatterns(window)
                scan_result = cp.scan_all()
            except (ValueError, Exception):
                continue

            # Combine all detected patterns
            all_detected = (
                scan_result.get("reversals", [])
                + scan_result.get("continuations", [])
            )

            for pattern in all_detected:
                ptype = pattern.get("type", "unknown")
                direction = pattern.get("direction", "neutral")

                # Forward outcome from end of window
                outcome_idx = start_idx + window_size + forward_bars
                if outcome_idx >= len(candles):
                    continue

                entry_price = float(
                    candles[start_idx + window_size - 1]
                    .get("mid", {})
                    .get("c", 0)
                )
                exit_price = float(
                    candles[outcome_idx].get("mid", {}).get("c", 0)
                )

                if entry_price == 0 or exit_price == 0:
                    continue

                move = exit_price - entry_price
                if direction == "bearish":
                    move = -move

                if ptype not in pattern_stats:
                    pattern_stats[ptype] = {
                        "occurrences": 0,
                        "wins": 0,
                        "losses": 0,
                        "neutral": 0,
                        "win_rate": 0.0,
                    }

                stats = pattern_stats[ptype]
                stats["occurrences"] += 1

                if atr > 0 and move >= atr * 1.5:
                    stats["wins"] += 1
                elif atr > 0 and move <= -atr:
                    stats["losses"] += 1
                else:
                    stats["neutral"] += 1

        # Calculate win rates
        for stats in pattern_stats.values():
            occ = stats["occurrences"]
            stats["win_rate"] = stats["wins"] / occ if occ > 0 else 0.0

        # Sort by win_rate descending
        sorted_patterns = dict(
            sorted(
                pattern_stats.items(),
                key=lambda item: item[1]["win_rate"],
                reverse=True,
            )
        )

        return {
            "instrument": instrument,
            "market_type": self._market_type,
            "granularity": granularity,
            "total_bars": len(candles),
            "forward_bars": forward_bars,
            "patterns": sorted_patterns,
        }

    # ------------------------------------------------------------------
    # Generic config comparison (BKTS-07)
    # ------------------------------------------------------------------

    def compare_configs(
        self,
        instrument: str,
        from_time: datetime,
        to_time: datetime,
        configs: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Compare arbitrary BacktestConfig variations via walk-forward.

        Each config dict specifies BacktestConfig parameters to vary.
        All configs use ``self._market_type``.

        Args:
            instrument: Instrument name.
            from_time: Start of range.
            to_time: End of range.
            configs: List of dicts with BacktestConfig parameter overrides.

        Returns:
            Dict with ranked results across all configurations.
        """
        from Source.backtester import BacktestConfig, Backtester, WalkForwardOptimizer

        candle_data = self._fetch_all_timeframes(instrument, from_time, to_time)

        results: List[Dict[str, Any]] = []
        for idx, cfg_overrides in enumerate(configs):
            # Build config with overrides
            cfg_params = {"market_type": self._market_type}
            cfg_params.update(cfg_overrides)
            config = BacktestConfig(**cfg_params)

            bt = Backtester(config)
            wfo = WalkForwardOptimizer()
            wfo_result = wfo.run(candle_data, instrument, bt)

            results.append({
                "config_index": idx,
                "config_params": cfg_overrides,
                "train_metrics": wfo_result.get("train", {}).get("metrics", {}),
                "test_metrics": wfo_result.get("test", {}).get("metrics", {}),
                "overfit_check": wfo_result.get("overfit_check", {}),
                "combined_metrics": wfo_result.get("combined_metrics", {}),
            })

        # Rank by test Sharpe
        ranked = sorted(
            results,
            key=lambda r: r["test_metrics"].get("sharpe_ratio", 0.0),
            reverse=True,
        )

        return {
            "instrument": instrument,
            "market_type": self._market_type,
            "period": f"{from_time.isoformat()} to {to_time.isoformat()}",
            "config_count": len(configs),
            "results": ranked,
        }

    # ------------------------------------------------------------------
    # Full analysis (convenience)
    # ------------------------------------------------------------------

    def run_full_analysis(
        self,
        instrument: str,
        from_time: datetime,
        to_time: datetime,
    ) -> Dict[str, Any]:
        """Run all analyses: EMA comparison, pattern success rates, default WFO.

        Args:
            instrument: Instrument name.
            from_time: Start of range.
            to_time: End of range.

        Returns:
            Combined report dict with all analysis results and market_type.
        """
        from Source.backtester import BacktestConfig, Backtester, WalkForwardOptimizer

        logger.info(
            "Running full analysis for %s (%s) [%s -> %s]",
            instrument,
            self._market_type,
            from_time,
            to_time,
        )

        # 1. EMA period comparison
        ema_results = self.compare_ema_periods(instrument, from_time, to_time)

        # 2. Candlestick pattern success rates
        candlestick_results = self.analyze_candlestick_patterns(
            instrument, from_time, to_time
        )

        # 3. Chart pattern success rates
        chart_results = self.analyze_chart_patterns(
            instrument, from_time, to_time
        )

        # 4. Default walk-forward with current strategy settings
        candle_data = self._fetch_all_timeframes(instrument, from_time, to_time)
        config = BacktestConfig(market_type=self._market_type)
        bt = Backtester(config)
        wfo = WalkForwardOptimizer()
        default_wfo = wfo.run(candle_data, instrument, bt)

        return {
            "instrument": instrument,
            "market_type": self._market_type,
            "period": f"{from_time.isoformat()} to {to_time.isoformat()}",
            "ema_comparison": ema_results,
            "candlestick_patterns": candlestick_results,
            "chart_patterns": chart_results,
            "default_walkforward": default_wfo,
        }

    # ------------------------------------------------------------------
    # Report formatting
    # ------------------------------------------------------------------

    def format_comparison_report(self, results: Dict[str, Any]) -> str:
        """Format analysis results as a Tim-facing plain text report.

        Args:
            results: Output from :meth:`run_full_analysis`.

        Returns:
            Formatted report string with ranked tables and go/no-go.
        """
        mt = results.get("market_type", self._market_type)
        instrument = results.get("instrument", "?")
        period = results.get("period", "?")

        lines = [
            f"=== Full Analysis Report: {instrument} ({mt}) ===",
            f"Period: {period}",
            "",
        ]

        # EMA comparison section
        ema = results.get("ema_comparison", {})
        if ema:
            lines.append("--- EMA Period Comparison ---")
            for r in ema.get("results", []):
                ema_str = "/".join(str(p) for p in r["ema_set"])
                test_m = r.get("test_metrics", {})
                overfit = r.get("overfit_check", {})
                lines.append(
                    f"  {ema_str}:  "
                    f"Sharpe {test_m.get('sharpe_ratio', 0):.3f}  "
                    f"DD {test_m.get('max_drawdown_pct', '?')}  "
                    f"PF {test_m.get('profit_factor', 0):.2f}  "
                    f"[{overfit.get('verdict', '?')}]"
                )
            lines.append(f"  Recommendation: {ema.get('recommendation', 'n/a')}")
            lines.append("")

        # Top 5 candlestick patterns
        cdl = results.get("candlestick_patterns", {})
        cdl_patterns = cdl.get("patterns", {})
        if cdl_patterns:
            lines.append("--- Top 5 Candlestick Patterns ---")
            for i, (name, stats) in enumerate(cdl_patterns.items()):
                if i >= 5:
                    break
                lines.append(
                    f"  {name}: {stats['win_rate']:.1%} win rate "
                    f"({stats['occurrences']} occurrences, "
                    f"avg {stats.get('avg_move_pips', 0):.1f} pips)"
                )
            lines.append("")

        # Top 5 chart patterns
        chart = results.get("chart_patterns", {})
        chart_patterns = chart.get("patterns", {})
        if chart_patterns:
            lines.append("--- Top 5 Chart Patterns ---")
            for i, (name, stats) in enumerate(chart_patterns.items()):
                if i >= 5:
                    break
                lines.append(
                    f"  {name}: {stats['win_rate']:.1%} win rate "
                    f"({stats['occurrences']} occurrences)"
                )
            lines.append("")

        # Default walk-forward summary
        wfo = results.get("default_walkforward", {})
        test_m = wfo.get("test", {}).get("metrics", {})
        overfit = wfo.get("overfit_check", {})
        if test_m:
            lines.append("--- Default Strategy Walk-Forward ---")
            lines.append(f"  Test Sharpe:    {test_m.get('sharpe_ratio', 0):.4f}")
            lines.append(f"  Test Drawdown:  {test_m.get('max_drawdown_pct', '?')}")
            lines.append(f"  Test PF:        {test_m.get('profit_factor', 0):.4f}")
            lines.append(f"  Overfit:        {overfit.get('verdict', '?')}")
            lines.append("")

            # Go/no-go
            sharpe = test_m.get("sharpe_ratio", 0.0)
            dd = test_m.get("max_drawdown", 0.0)
            pf = test_m.get("profit_factor", 0.0)
            verdict_str = overfit.get("verdict", "unknown")
            all_pass = (
                sharpe > 1.5
                and dd < 0.15
                and pf > 1.5
                and verdict_str != "overfit"
            )
            go_str = "GO - Deploy to live" if all_pass else "NO-GO - Needs tuning"
            lines.append(f"=== VERDICT: {go_str} ===")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_all_timeframes(
        self,
        instrument: str,
        from_time: datetime,
        to_time: datetime,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch candle data for all configured timeframes.

        Returns:
            Dict mapping timeframe -> list of candle dicts.
        """
        timeframes = ("M15", "H1", "H4")
        candle_data: Dict[str, List[Dict[str, Any]]] = {}

        for tf in timeframes:
            try:
                candles = self._fetcher.fetch(instrument, tf, from_time, to_time)
                candle_data[tf] = candles
                logger.info(
                    "Fetched %d %s candles for %s",
                    len(candles),
                    tf,
                    instrument,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch %s candles for %s: %s",
                    tf,
                    instrument,
                    exc,
                )
                candle_data[tf] = []

        return candle_data

    @staticmethod
    def _compute_simple_atr(
        candles: List[Dict[str, Any]],
        period: int = 14,
    ) -> float:
        """Compute a simple ATR from the last *period* candles.

        Args:
            candles: List of candle dicts.
            period: ATR period (default 14).

        Returns:
            ATR value, or 0.0 if insufficient data.
        """
        if len(candles) < period + 1:
            return 0.0

        recent = candles[-(period + 1):]
        true_ranges = []

        for i in range(1, len(recent)):
            high = float(recent[i].get("mid", {}).get("h", 0))
            low = float(recent[i].get("mid", {}).get("l", 0))
            prev_close = float(recent[i - 1].get("mid", {}).get("c", 0))

            if high == 0 or low == 0 or prev_close == 0:
                continue

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)

        if not true_ranges:
            return 0.0

        return sum(true_ranges) / len(true_ranges)
