"""
Market-type-agnostic backtester with walk-forward optimization.

Provides :class:`BacktestConfig` (market-aware configuration),
:class:`TradeResult` (single simulated trade), :class:`PerformanceMetrics`
(all required performance statistics), :class:`Backtester` (event-driven
simulation), and :class:`WalkForwardOptimizer` (70/30 train/test split
with overfit detection).

The backtester works across forex, crypto, and futures by delegating
market-specific behaviour (session hours, pip calculations, trading
windows) to :class:`MarketProfile`.

Usage:
    from Source.backtester import Backtester, BacktestConfig, WalkForwardOptimizer

    config = BacktestConfig(market_type='forex')
    bt = Backtester(config)
    candle_data = {'H1': [...], 'H4': [...], 'M15': [...]}
    trades = bt.run(candle_data, 'EUR_USD')

    wfo = WalkForwardOptimizer()
    results = wfo.run(candle_data, 'EUR_USD', bt)
    print(wfo.format_report(results))
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("trading.backtester")


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class BacktestConfig:
    """Configuration for a backtest run.

    Uses MarketProfile for market-type-specific behavior (session hours,
    pip calculations, trading windows). Works across forex, crypto, futures.

    Args:
        market_type: Market type name matching a MarketProfile YAML
            (e.g. 'forex', 'crypto', 'futures').
        initial_balance: Starting account balance (default $500).
        risk_per_trade: Fraction of balance risked per trade (default 0.02).
        timeframes: Timeframes to use (default ('M15', 'H1', 'H4')).
        primary_timeframe: Entry signal timeframe (default 'H1').
        ema_periods: Tuple of 3 EMA periods (default (9, 21, 50)).
        score_threshold: Minimum confluence score to enter (default 70).
        use_session_filter: Whether to apply session-based scoring (default True).
        use_market_hours: Whether to restrict to market open hours (default True).
            True for forex/futures, False for crypto (24/7).
    """

    market_type: str = "forex"
    initial_balance: float = 500.0
    risk_per_trade: float = 0.02
    timeframes: tuple = ("M15", "H1", "H4")
    primary_timeframe: str = "H1"
    ema_periods: tuple = (9, 21, 50)
    score_threshold: float = 70.0
    use_session_filter: bool = True
    use_market_hours: bool = True


@dataclass
class TradeResult:
    """Single simulated trade result."""

    instrument: str
    direction: str          # "buy" or "sell"
    entry_time: str         # RFC3339
    exit_time: str          # RFC3339
    entry_price: float
    exit_price: float
    units: int
    pnl: float              # realized P&L in account currency
    pnl_pips: float         # P&L in pips
    stop_loss: float
    take_profit: float
    confluence_score: float  # score at entry
    exit_reason: str        # "tp_hit", "sl_hit", "time_exit", "signal_flip"
    market_type: str = "forex"  # tag for result separation


# ------------------------------------------------------------------
# PerformanceMetrics
# ------------------------------------------------------------------

class PerformanceMetrics:
    """Static metrics calculator for backtested trades.

    All methods are classmethods -- no instance state.  Computes Sharpe,
    Sortino, Calmar, profit factor, max drawdown, win rate, and more.
    Annualization adjusts by market type: 252 days for forex/futures,
    365 for crypto.
    """

    # Market types that trade 365 days/year
    _CONTINUOUS_MARKETS = {"crypto"}

    @classmethod
    def compute(
        cls,
        trades: List[TradeResult],
        initial_balance: float = 500.0,
    ) -> Dict[str, Any]:
        """Compute all performance metrics from a list of trades.

        Args:
            trades: List of :class:`TradeResult` from a backtest run.
            initial_balance: Starting balance for equity curve.

        Returns:
            Dict with all required metrics (see module docstring).
        """
        if not trades:
            return cls._empty_metrics(initial_balance)

        # Determine market type from first trade
        market_type = trades[0].market_type
        periods_per_year = 365 if market_type in cls._CONTINUOUS_MARKETS else 252

        # Basic counts
        winning = [t for t in trades if t.pnl > 0]
        losing = [t for t in trades if t.pnl <= 0]
        total = len(trades)

        win_rate = len(winning) / total if total > 0 else 0.0
        gross_profit = sum(t.pnl for t in winning)
        gross_loss = abs(sum(t.pnl for t in losing))
        net_profit = gross_profit - gross_loss

        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else float("inf")
        )
        avg_win = gross_profit / len(winning) if winning else 0.0
        avg_loss = gross_loss / len(losing) if losing else 0.0
        avg_rr = avg_win / avg_loss if avg_loss > 0 else float("inf")

        # Equity curve
        equity_curve: List[Tuple[str, float]] = []
        balance = initial_balance
        for t in trades:
            balance += t.pnl
            equity_curve.append((t.exit_time, balance))

        # Returns series
        returns = [t.pnl / initial_balance for t in trades]

        # Max drawdown
        max_dd = cls._compute_drawdown(equity_curve, initial_balance)

        # Sharpe, Sortino, Calmar
        sharpe = cls._compute_sharpe(returns, periods_per_year)
        sortino = cls._compute_sortino(returns, periods_per_year)

        final_balance = equity_curve[-1][1] if equity_curve else initial_balance
        total_return = (final_balance - initial_balance) / initial_balance

        # Annualized return (approximate from trade count)
        if total > 0 and periods_per_year > 0:
            annualized_return = (1 + total_return) ** (
                periods_per_year / max(total, 1)
            ) - 1
        else:
            annualized_return = 0.0

        calmar = cls._compute_calmar(annualized_return, max_dd)

        # Consecutive streaks
        max_consec_wins, max_consec_losses = cls._compute_streaks(trades)

        # Average trade duration
        avg_duration = cls._compute_avg_duration(trades)

        return {
            "total_trades": total,
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "win_rate": win_rate,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "net_profit": round(net_profit, 2),
            "profit_factor": round(profit_factor, 4),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "avg_rr": round(avg_rr, 4),
            "max_drawdown": round(max_dd, 6),
            "max_drawdown_pct": f"{max_dd * 100:.1f}%",
            "sharpe_ratio": round(sharpe, 4),
            "sortino_ratio": round(sortino, 4),
            "calmar_ratio": round(calmar, 4),
            "equity_curve": equity_curve,
            "return_pct": round(total_return, 6),
            "annualized_return": round(annualized_return, 6),
            "max_consecutive_wins": max_consec_wins,
            "max_consecutive_losses": max_consec_losses,
            "avg_trade_duration": avg_duration,
            "market_type": market_type,
        }

    @classmethod
    def _compute_drawdown(
        cls,
        equity_curve: List[Tuple[str, float]],
        initial_balance: float,
    ) -> float:
        """Compute maximum drawdown as a decimal fraction.

        Args:
            equity_curve: List of (time, balance) tuples.
            initial_balance: Starting balance.

        Returns:
            Max drawdown as decimal (e.g. 0.15 = 15%).
        """
        if not equity_curve:
            return 0.0

        peak = initial_balance
        max_dd = 0.0

        for _, balance in equity_curve:
            if balance > peak:
                peak = balance
            dd = (peak - balance) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        return max_dd

    @classmethod
    def _compute_sharpe(
        cls,
        returns: List[float],
        periods_per_year: int = 252,
    ) -> float:
        """Compute annualized Sharpe ratio.

        Args:
            returns: List of per-trade returns.
            periods_per_year: 252 for forex/futures, 365 for crypto.

        Returns:
            Annualized Sharpe ratio (0.0 if insufficient data).
        """
        if len(returns) < 2:
            return 0.0

        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(variance) if variance > 0 else 0.0

        if std_r == 0:
            return 0.0

        return (mean_r / std_r) * math.sqrt(periods_per_year)

    @classmethod
    def _compute_sortino(
        cls,
        returns: List[float],
        periods_per_year: int = 252,
    ) -> float:
        """Compute annualized Sortino ratio (downside deviation only).

        Args:
            returns: List of per-trade returns.
            periods_per_year: 252 for forex/futures, 365 for crypto.

        Returns:
            Annualized Sortino ratio (0.0 if no downside deviation).
        """
        if len(returns) < 2:
            return 0.0

        mean_r = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]

        if not downside:
            return float("inf") if mean_r > 0 else 0.0

        downside_var = sum(r ** 2 for r in downside) / len(downside)
        downside_std = math.sqrt(downside_var)

        if downside_std == 0:
            return 0.0

        return (mean_r / downside_std) * math.sqrt(periods_per_year)

    @classmethod
    def _compute_calmar(
        cls,
        annualized_return: float,
        max_drawdown: float,
    ) -> float:
        """Compute Calmar ratio (annualized return / max drawdown).

        Args:
            annualized_return: Compound annual return.
            max_drawdown: Maximum drawdown as decimal.

        Returns:
            Calmar ratio (0.0 if no drawdown).
        """
        if max_drawdown == 0:
            return float("inf") if annualized_return > 0 else 0.0
        return annualized_return / max_drawdown

    @classmethod
    def _compute_streaks(
        cls,
        trades: List[TradeResult],
    ) -> Tuple[int, int]:
        """Compute max consecutive wins and losses.

        Returns:
            Tuple of (max_consecutive_wins, max_consecutive_losses).
        """
        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0

        for t in trades:
            if t.pnl > 0:
                current_wins += 1
                current_losses = 0
                max_wins = max(max_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_losses = max(max_losses, current_losses)

        return max_wins, max_losses

    @classmethod
    def _compute_avg_duration(cls, trades: List[TradeResult]) -> str:
        """Compute average trade holding time as a human-readable string.

        Returns:
            String like '4h 30m' or 'n/a'.
        """
        if not trades:
            return "n/a"

        total_seconds = 0
        parsed_count = 0

        for t in trades:
            try:
                entry = datetime.fromisoformat(
                    t.entry_time.replace("Z", "+00:00")
                )
                exit_ = datetime.fromisoformat(
                    t.exit_time.replace("Z", "+00:00")
                )
                total_seconds += (exit_ - entry).total_seconds()
                parsed_count += 1
            except (ValueError, TypeError):
                continue

        if parsed_count == 0:
            return "n/a"

        avg_secs = total_seconds / parsed_count
        hours = int(avg_secs // 3600)
        minutes = int((avg_secs % 3600) // 60)
        return f"{hours}h {minutes}m"

    @classmethod
    def _empty_metrics(cls, initial_balance: float) -> Dict[str, Any]:
        """Return a zero-trade metrics dict."""
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "net_profit": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_rr": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": "0.0%",
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "calmar_ratio": 0.0,
            "equity_curve": [],
            "return_pct": 0.0,
            "annualized_return": 0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "avg_trade_duration": "n/a",
            "market_type": "forex",
        }

    @classmethod
    def format_report(cls, metrics: Dict[str, Any]) -> str:
        """Format metrics as a Tim-facing plain text report.

        Includes pass/fail indicators against target thresholds:
        Sharpe > 1.5, max drawdown < 15%, profit factor > 1.5.

        Args:
            metrics: Dict from :meth:`compute`.

        Returns:
            Formatted report string.
        """
        mt = metrics.get("market_type", "forex")
        sharpe = metrics.get("sharpe_ratio", 0.0)
        max_dd = metrics.get("max_drawdown", 0.0)
        pf = metrics.get("profit_factor", 0.0)

        sharpe_pass = "PASS" if sharpe > 1.5 else "FAIL"
        dd_pass = "PASS" if max_dd < 0.15 else "FAIL"
        pf_pass = "PASS" if pf > 1.5 else "FAIL"

        all_pass = sharpe > 1.5 and max_dd < 0.15 and pf > 1.5
        verdict = "GO" if all_pass else "NO-GO"

        lines = [
            f"=== Backtest Report ({mt}) ===",
            f"Total Trades:         {metrics.get('total_trades', 0)}",
            f"Win Rate:             {metrics.get('win_rate', 0.0):.1%}",
            f"Net Profit:           ${metrics.get('net_profit', 0.0):.2f}",
            f"Return:               {metrics.get('return_pct', 0.0):.2%}",
            "",
            "--- Key Metrics (threshold check) ---",
            f"Sharpe Ratio:         {sharpe:.4f}  (> 1.5: {sharpe_pass})",
            f"Max Drawdown:         {metrics.get('max_drawdown_pct', '0.0%')}  (< 15%: {dd_pass})",
            f"Profit Factor:        {pf:.4f}  (> 1.5: {pf_pass})",
            "",
            "--- Extended Metrics ---",
            f"Sortino Ratio:        {metrics.get('sortino_ratio', 0.0):.4f}",
            f"Calmar Ratio:         {metrics.get('calmar_ratio', 0.0):.4f}",
            f"Avg Win:              ${metrics.get('avg_win', 0.0):.2f}",
            f"Avg Loss:             ${metrics.get('avg_loss', 0.0):.2f}",
            f"Avg R:R:              {metrics.get('avg_rr', 0.0):.2f}",
            f"Max Consec Wins:      {metrics.get('max_consecutive_wins', 0)}",
            f"Max Consec Losses:    {metrics.get('max_consecutive_losses', 0)}",
            f"Avg Trade Duration:   {metrics.get('avg_trade_duration', 'n/a')}",
            "",
            f"=== VERDICT: {verdict} ===",
        ]
        return "\n".join(lines)


# ------------------------------------------------------------------
# Backtester engine
# ------------------------------------------------------------------

class Backtester:
    """Market-type-agnostic event-driven strategy backtester.

    Steps through historical candles, computes indicators on a rolling
    window, generates confluence signals, and simulates trades with
    realistic position sizing and stop management.

    Uses MarketProfile for market-specific behavior:
    - Session hours filtering (forex: London-NY, crypto: 24/7, futures: RTH)
    - Pip calculations appropriate to the market
    - Trading window restrictions

    Args:
        config: BacktestConfig with market-type-aware parameters.
    """

    # Rolling window size for indicators (EMA 200 needs ~200 bars)
    _LOOKBACK = 200

    def __init__(self, config: Optional[BacktestConfig] = None) -> None:
        self._config = config or BacktestConfig()
        self._profile = None  # Lazy loaded
        self._balance: float = self._config.initial_balance
        self._initial_balance: float = self._config.initial_balance
        self._equity_curve: List[Tuple[str, float]] = []
        self._trades: List[TradeResult] = []
        self._open_position: Optional[Dict[str, Any]] = None

    def _get_profile(self):
        """Lazy-load MarketProfile from config.market_type."""
        if self._profile is None:
            try:
                from Source.market_profile import MarketProfile
                self._profile = MarketProfile.from_market_type(
                    self._config.market_type
                )
            except Exception as exc:
                logger.warning(
                    "Could not load MarketProfile for '%s': %s. "
                    "Market hours filtering disabled.",
                    self._config.market_type,
                    exc,
                )
        return self._profile

    def run(
        self,
        candle_data: Dict[str, List[Dict[str, Any]]],
        instrument: str,
        instrument_config: Optional[Dict[str, Any]] = None,
    ) -> List[TradeResult]:
        """Run the backtest simulation on historical candle data.

        Steps through the primary timeframe candles one bar at a time,
        computing indicators on a rolling window and simulating trades.

        Args:
            candle_data: Dict mapping timeframe -> list of candle dicts.
            instrument: Instrument name (e.g. 'EUR_USD').
            instrument_config: Optional instrument spec dict with
                pip_size, margin_rate, etc.

        Returns:
            List of completed TradeResult instances.
        """
        self.reset()
        primary_tf = self._config.primary_timeframe
        primary_candles = candle_data.get(primary_tf, [])

        if not primary_candles:
            logger.warning("No candles for primary timeframe %s", primary_tf)
            return []

        # Filter to complete candles
        primary_candles = [
            c for c in primary_candles if c.get("complete", True)
        ]

        profile = self._get_profile()

        # Derive pip_size from instrument_config or default
        pip_size = 0.0001  # EUR_USD default
        if instrument_config:
            pip_size = instrument_config.get("pip_size", pip_size)
        elif "_JPY" in instrument:
            pip_size = 0.01

        logger.info(
            "Starting backtest: %s %s (%s), %d candles",
            instrument,
            self._config.market_type,
            primary_tf,
            len(primary_candles),
        )

        for idx in range(self._LOOKBACK, len(primary_candles)):
            candle = primary_candles[idx]
            bar_time_str = candle.get("time", "")

            # 1. Market hours check
            if self._config.use_market_hours and profile is not None:
                try:
                    bar_time = self._parse_time(bar_time_str)
                    if not profile.is_market_open(bar_time):
                        continue
                except (ValueError, TypeError):
                    pass

            # 2. Build rolling window
            window = primary_candles[idx - self._LOOKBACK: idx + 1]

            # 3. Compute signals
            signals = self._compute_signals(
                window, instrument, candle_data, idx
            )

            if signals is None:
                continue

            # 4. Get current prices
            mid = candle.get("mid", {})
            current_close = float(mid.get("c", 0))
            current_high = float(mid.get("h", 0))
            current_low = float(mid.get("l", 0))

            if current_close == 0:
                continue

            # 5. Check exit if position is open
            if self._open_position is not None:
                exit_reason = self._check_exit(
                    self._open_position, candle, signals
                )
                if exit_reason is not None:
                    # Determine exit price based on reason
                    if exit_reason == "sl_hit":
                        exit_price = self._open_position["stop_loss"]
                    elif exit_reason == "tp_hit":
                        exit_price = self._open_position["take_profit"]
                    else:
                        exit_price = current_close

                    self._record_trade(
                        self._open_position,
                        exit_price,
                        bar_time_str,
                        exit_reason,
                        pip_size,
                    )
                    self._open_position = None

            # 6. Check entry if no position
            if self._open_position is None:
                entry = self._check_entry(signals, candle, pip_size)
                if entry is not None:
                    units = self._calculate_position_size(
                        entry["price"],
                        entry["stop_loss"],
                        instrument,
                        pip_size,
                    )
                    self._open_position = {
                        "instrument": instrument,
                        "direction": entry["direction"],
                        "entry_time": bar_time_str,
                        "entry_price": entry["price"],
                        "stop_loss": entry["stop_loss"],
                        "take_profit": entry["take_profit"],
                        "units": units,
                        "confluence_score": signals.get("score", 0),
                    }

        # Close any remaining open position at last candle
        if self._open_position is not None and primary_candles:
            last = primary_candles[-1]
            last_mid = last.get("mid", {})
            last_close = float(last_mid.get("c", 0))
            if last_close > 0:
                self._record_trade(
                    self._open_position,
                    last_close,
                    last.get("time", ""),
                    "time_exit",
                    pip_size,
                )
            self._open_position = None

        logger.info(
            "Backtest complete: %d trades, final balance $%.2f",
            len(self._trades),
            self._balance,
        )
        return list(self._trades)

    def _compute_signals(
        self,
        candles_window: List[Dict[str, Any]],
        instrument: str,
        all_timeframe_data: Dict[str, List[Dict[str, Any]]],
        current_idx: int,
    ) -> Optional[Dict[str, Any]]:
        """Compute indicator signals from a candle window.

        Lazy imports Indicators, AdvancedIndicators, MultiTimeframeAlignment,
        and ConfluenceScorer.

        Args:
            candles_window: Rolling window of candles for primary TF.
            instrument: Instrument name.
            all_timeframe_data: Full candle data across timeframes.
            current_idx: Current bar index in primary candles.

        Returns:
            Dict with score, direction, signals, regime, indicators.
            None if computation fails.
        """
        try:
            from Source.indicators import Indicators
            from Source.indicators_advanced import AdvancedIndicators
            from Source.confluence_scorer import ConfluenceScorer

            # Core indicators on primary timeframe window
            ind = Indicators(candles_window)
            ind_result = ind.compute_all()

            # Advanced indicators
            adv = AdvancedIndicators(candles_window)
            adv_result = adv.compute_all()

            # Multi-timeframe alignment (use H4 candles up to current time)
            alignment_snapshot = {}
            try:
                from Source.alignment import MultiTimeframeAlignment

                # Build MTF data: use available candles up to proportional idx
                mtf_data = {}
                primary_tf = self._config.primary_timeframe
                for tf, candles in all_timeframe_data.items():
                    if tf == primary_tf:
                        mtf_data[tf] = candles_window
                    else:
                        # Approximate proportional position
                        proportion = current_idx / max(
                            len(all_timeframe_data.get(primary_tf, [1])), 1
                        )
                        end_idx = max(
                            self._LOOKBACK,
                            int(proportion * len(candles)),
                        )
                        start_idx = max(0, end_idx - self._LOOKBACK)
                        mtf_data[tf] = candles[start_idx:end_idx]

                if len(mtf_data) >= 2:
                    mta = MultiTimeframeAlignment(mtf_data)
                    alignment_snapshot = mta.get_snapshot()
            except Exception:
                pass  # Graceful degradation

            # Score via ConfluenceScorer
            scorer = ConfluenceScorer()
            score_result = scorer.compute_score(
                indicators_result=ind_result,
                advanced_result=adv_result,
                alignment_snapshot=alignment_snapshot,
            )

            # Extract RSI and ATR for entry/exit checks
            rsi_data = ind_result.get("rsi", {})
            atr_data = ind_result.get("atr", {})

            return {
                "score": score_result.get("total_score", 0),
                "direction": score_result.get("direction", "neutral"),
                "regime": score_result.get("regime", "mixed"),
                "rsi_value": rsi_data.get("value"),
                "atr_value": atr_data.get("value"),
                "indicators": ind_result,
                "advanced": adv_result,
                "confluence": score_result,
            }

        except Exception as exc:
            logger.debug("Signal computation failed at idx %d: %s", current_idx, exc)
            return None

    def _check_entry(
        self,
        signals: Dict[str, Any],
        current_candle: Dict[str, Any],
        pip_size: float,
    ) -> Optional[Dict[str, Any]]:
        """Check if entry conditions are met.

        Applies RSI gate, score threshold, and direction check.

        Args:
            signals: Output from _compute_signals.
            current_candle: Current candle dict.
            pip_size: Pip size for stop/target calculation.

        Returns:
            Entry dict with direction, price, stop, target; or None.
        """
        score = signals.get("score", 0)
        direction = signals.get("direction", "neutral")
        rsi = signals.get("rsi_value")
        atr = signals.get("atr_value")

        # Score threshold (STRT-02: > 70)
        if score < self._config.score_threshold:
            return None

        # Direction must be clear (> 0.3 threshold from 05-01)
        if direction == "neutral":
            return None

        # RSI gate (STRT-04)
        if rsi is not None:
            if direction == "bullish" and rsi > 70:
                return None
            if direction == "bearish" and rsi < 30:
                return None

        # Get entry price
        mid = current_candle.get("mid", {})
        entry_price = float(mid.get("c", 0))
        if entry_price == 0:
            return None

        # ATR-based stop (2x ATR from entry)
        if atr is None or atr <= 0:
            return None

        stop_distance = atr * 2.0

        # Minimum stop distance: 2x spread (use 2 pips default estimate)
        min_stop = pip_size * 4  # 2x a ~2 pip spread
        stop_distance = max(stop_distance, min_stop)

        if direction == "bullish":
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + stop_distance * 2.0  # 2:1 R:R
            trade_dir = "buy"
        else:
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - stop_distance * 2.0  # 2:1 R:R
            trade_dir = "sell"

        return {
            "direction": trade_dir,
            "price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }

    def _check_exit(
        self,
        position: Dict[str, Any],
        current_candle: Dict[str, Any],
        signals: Dict[str, Any],
    ) -> Optional[str]:
        """Check if exit conditions are met for an open position.

        Args:
            position: Open position dict.
            current_candle: Current candle dict.
            signals: Current signals dict.

        Returns:
            Exit reason string or None.
        """
        mid = current_candle.get("mid", {})
        high = float(mid.get("h", 0))
        low = float(mid.get("l", 0))
        close = float(mid.get("c", 0))

        direction = position["direction"]
        sl = position["stop_loss"]
        tp = position["take_profit"]

        # Check SL hit
        if direction == "buy" and low <= sl:
            return "sl_hit"
        if direction == "sell" and high >= sl:
            return "sl_hit"

        # Check TP hit
        if direction == "buy" and high >= tp:
            return "tp_hit"
        if direction == "sell" and low <= tp:
            return "tp_hit"

        # Signal flip exit: direction reversed with score > 60
        sig_direction = signals.get("direction", "neutral")
        sig_score = signals.get("score", 0)

        if sig_score > 60:
            if direction == "buy" and sig_direction == "bearish":
                return "signal_flip"
            if direction == "sell" and sig_direction == "bullish":
                return "signal_flip"

        return None

    def _calculate_position_size(
        self,
        entry_price: float,
        stop_price: float,
        instrument: str,
        pip_size: float,
    ) -> int:
        """Calculate position size in units.

        Uses risk_per_trade * balance / risk_per_unit, consistent
        with PositionSizer logic from Phase 8.

        Args:
            entry_price: Entry price.
            stop_price: Stop loss price.
            instrument: Instrument name.
            pip_size: Pip size for the instrument.

        Returns:
            Number of units (minimum 1).
        """
        risk_amount = self._balance * self._config.risk_per_trade
        stop_distance = abs(entry_price - stop_price)

        if stop_distance <= 0:
            return 1

        # Simple pip value calculation
        pip_value = pip_size
        parts = instrument.split("_")
        if len(parts) == 2 and parts[0] == "USD" and entry_price > 0:
            pip_value = pip_size / entry_price

        stop_pips = stop_distance / pip_size
        risk_per_unit = stop_pips * pip_value

        if risk_per_unit <= 0:
            return 1

        units = int(risk_amount / risk_per_unit)

        # 10:1 leverage cap
        max_units = int(self._balance * 10 / entry_price) if entry_price > 0 else 1
        units = min(units, max(1, max_units))

        return max(1, units)

    def _record_trade(
        self,
        position: Dict[str, Any],
        exit_price: float,
        exit_time: str,
        exit_reason: str,
        pip_size: float,
    ) -> None:
        """Record a completed trade.

        Creates a TradeResult, updates balance and equity curve.

        Args:
            position: Open position dict.
            exit_price: Price at which the trade exited.
            exit_time: RFC3339 exit time.
            exit_reason: Reason for exit.
            pip_size: Pip size for pnl_pips calculation.
        """
        direction = position["direction"]
        entry_price = position["entry_price"]
        units = position["units"]

        # P&L calculation
        if direction == "buy":
            price_diff = exit_price - entry_price
        else:
            price_diff = entry_price - exit_price

        pnl_pips = price_diff / pip_size if pip_size > 0 else 0.0

        # Simple P&L in account currency (pip_value * pips * units)
        pip_value = pip_size
        instrument = position["instrument"]
        parts = instrument.split("_")
        if len(parts) == 2 and parts[0] == "USD" and entry_price > 0:
            pip_value = pip_size / entry_price

        pnl = pnl_pips * pip_value * units

        trade = TradeResult(
            instrument=position["instrument"],
            direction=direction,
            entry_time=position["entry_time"],
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            units=units,
            pnl=pnl,
            pnl_pips=pnl_pips,
            stop_loss=position["stop_loss"],
            take_profit=position["take_profit"],
            confluence_score=position["confluence_score"],
            exit_reason=exit_reason,
            market_type=self._config.market_type,
        )

        self._trades.append(trade)
        self._balance += pnl
        self._equity_curve.append((exit_time, self._balance))

    def reset(self) -> None:
        """Reset backtester state for a fresh run."""
        self._balance = self._config.initial_balance
        self._initial_balance = self._config.initial_balance
        self._equity_curve = []
        self._trades = []
        self._open_position = None
        self._profile = None

    @staticmethod
    def _parse_time(time_str: str) -> datetime:
        """Parse RFC3339 time string to UTC datetime."""
        s = time_str.replace("Z", "")
        if "." in s:
            integer_part, frac = s.split(".", 1)
            offset = ""
            for sep in ("+", "-"):
                if sep in frac:
                    idx = frac.index(sep)
                    offset = frac[idx:]
                    frac = frac[:idx]
                    break
            frac = frac[:6].ljust(6, "0")
            s = f"{integer_part}.{frac}{offset}"
        return datetime.fromisoformat(s + "+00:00")


# ------------------------------------------------------------------
# Walk-forward optimizer
# ------------------------------------------------------------------

class WalkForwardOptimizer:
    """Walk-forward optimization with configurable train/test split.

    Works with any market type -- split logic is time-based, not
    market-specific.

    Args:
        train_ratio: Fraction of data used for training (default 0.7).
    """

    def __init__(self, train_ratio: float = 0.7) -> None:
        self._train_ratio = train_ratio

    def run(
        self,
        candle_data: Dict[str, List[Dict[str, Any]]],
        instrument: str,
        backtester: Backtester,
    ) -> Dict[str, Any]:
        """Run walk-forward optimization: train on first N%, test on rest.

        Args:
            candle_data: Dict mapping timeframe -> list of candle dicts.
            instrument: Instrument name.
            backtester: Configured Backtester instance.

        Returns:
            Dict with train/test results, overfit check, combined metrics.
        """
        # Split data
        train_data, test_data, split_point = self._split_candles(
            candle_data, self._train_ratio
        )

        # Run on training set
        backtester.reset()
        train_trades = backtester.run(train_data, instrument)
        train_metrics = PerformanceMetrics.compute(
            train_trades, backtester._config.initial_balance
        )

        # Run on test set
        backtester.reset()
        test_trades = backtester.run(test_data, instrument)
        test_metrics = PerformanceMetrics.compute(
            test_trades, backtester._config.initial_balance
        )

        # Combined metrics
        all_trades = train_trades + test_trades
        combined_metrics = PerformanceMetrics.compute(
            all_trades, backtester._config.initial_balance
        )

        # Overfit check
        train_sharpe = train_metrics.get("sharpe_ratio", 0.0)
        test_sharpe = test_metrics.get("sharpe_ratio", 0.0)
        train_dd = train_metrics.get("max_drawdown", 0.0)
        test_dd = test_metrics.get("max_drawdown", 0.0)
        train_pf = train_metrics.get("profit_factor", 0.0)
        test_pf = test_metrics.get("profit_factor", 0.0)

        sharpe_deg = train_sharpe - test_sharpe
        dd_increase = test_dd - train_dd
        pf_deg = train_pf - test_pf

        # Verdict logic
        if sharpe_deg > 1.0 or dd_increase > 0.10 or pf_deg > 1.0:
            verdict = "overfit"
        elif sharpe_deg > 0.5 or dd_increase > 0.05 or pf_deg > 0.5:
            verdict = "caution"
        else:
            verdict = "pass"

        return {
            "market_type": backtester._config.market_type,
            "instrument": instrument,
            "train": {"trades": train_trades, "metrics": train_metrics},
            "test": {"trades": test_trades, "metrics": test_metrics},
            "overfit_check": {
                "sharpe_degradation": round(sharpe_deg, 4),
                "drawdown_increase": round(dd_increase, 6),
                "profit_factor_degradation": round(pf_deg, 4),
                "verdict": verdict,
            },
            "combined_metrics": combined_metrics,
            "split_point": split_point,
        }

    def _split_candles(
        self,
        candle_data: Dict[str, List[Dict[str, Any]]],
        ratio: float,
    ) -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]], str]:
        """Split candle data by time into train and test sets.

        Args:
            candle_data: Full candle data across timeframes.
            ratio: Fraction for training set (e.g. 0.7).

        Returns:
            Tuple of (train_data, test_data, split_point_str).
        """
        train_data: Dict[str, List[Dict]] = {}
        test_data: Dict[str, List[Dict]] = {}
        split_point = ""

        for tf, candles in candle_data.items():
            if not candles:
                train_data[tf] = []
                test_data[tf] = []
                continue

            split_idx = int(len(candles) * ratio)
            split_idx = max(1, min(split_idx, len(candles) - 1))

            train_data[tf] = candles[:split_idx]
            test_data[tf] = candles[split_idx:]

            # Use primary TF split point for reporting
            if not split_point and split_idx < len(candles):
                split_point = candles[split_idx].get("time", "")

        return train_data, test_data, split_point

    def format_report(self, results: Dict[str, Any]) -> str:
        """Format walk-forward results as a Tim-facing report.

        Args:
            results: Output from :meth:`run`.

        Returns:
            Formatted report string with train vs test comparison.
        """
        mt = results.get("market_type", "forex")
        instrument = results.get("instrument", "?")
        train_m = results.get("train", {}).get("metrics", {})
        test_m = results.get("test", {}).get("metrics", {})
        overfit = results.get("overfit_check", {})
        combined = results.get("combined_metrics", {})

        verdict = overfit.get("verdict", "unknown").upper()

        lines = [
            f"=== Walk-Forward Report: {instrument} ({mt}) ===",
            f"Split: {self._train_ratio:.0%} train / {1 - self._train_ratio:.0%} test",
            f"Split Point: {results.get('split_point', 'n/a')}",
            "",
            "--- Training Set ---",
            f"  Trades:         {train_m.get('total_trades', 0)}",
            f"  Win Rate:       {train_m.get('win_rate', 0.0):.1%}",
            f"  Sharpe:         {train_m.get('sharpe_ratio', 0.0):.4f}",
            f"  Max Drawdown:   {train_m.get('max_drawdown_pct', '0.0%')}",
            f"  Profit Factor:  {train_m.get('profit_factor', 0.0):.4f}",
            "",
            "--- Test Set ---",
            f"  Trades:         {test_m.get('total_trades', 0)}",
            f"  Win Rate:       {test_m.get('win_rate', 0.0):.1%}",
            f"  Sharpe:         {test_m.get('sharpe_ratio', 0.0):.4f}",
            f"  Max Drawdown:   {test_m.get('max_drawdown_pct', '0.0%')}",
            f"  Profit Factor:  {test_m.get('profit_factor', 0.0):.4f}",
            "",
            "--- Overfit Detection ---",
            f"  Sharpe Degradation:  {overfit.get('sharpe_degradation', 0):.4f}",
            f"  Drawdown Increase:   {overfit.get('drawdown_increase', 0):.4f}",
            f"  PF Degradation:      {overfit.get('profit_factor_degradation', 0):.4f}",
            f"  Verdict:             {verdict}",
            "",
            "--- Combined ---",
            f"  Total Trades:   {combined.get('total_trades', 0)}",
            f"  Net Profit:     ${combined.get('net_profit', 0.0):.2f}",
            f"  Sharpe:         {combined.get('sharpe_ratio', 0.0):.4f}",
            "",
        ]

        # Go/no-go recommendation
        test_sharpe = test_m.get("sharpe_ratio", 0.0)
        test_dd = test_m.get("max_drawdown", 0.0)
        test_pf = test_m.get("profit_factor", 0.0)
        all_pass = (
            test_sharpe > 1.5
            and test_dd < 0.15
            and test_pf > 1.5
            and verdict != "overfit"
        )

        recommendation = "GO - Deploy to live" if all_pass else "NO-GO - Needs tuning"
        lines.append(f"=== RECOMMENDATION: {recommendation} ===")

        return "\n".join(lines)
