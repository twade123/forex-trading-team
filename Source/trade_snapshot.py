"""
Trade snapshot capture system for the trading bot.

Captures a candlestick chart screenshot with indicator overlays (EMA,
Bollinger, Fibonacci) plus RSI and MACD subplots, alongside the full
indicator/decision state as a JSON sidecar.  Every trade gets a visual
record paired with the data that produced the decision.

The ``outcome`` field in the JSON is null at capture time -- Phase 8
(risk management) or Phase 12 (logging) will update it with win/loss
and P/L data once the trade resolves.

Primary entry point is :meth:`TradeSnapshot.capture`.

Usage::

    from Source.trade_snapshot import TradeSnapshot

    snap = TradeSnapshot()
    result = snap.capture(
        instrument="EUR_USD",
        candles=candles,
        indicators_result=ind.compute_all(),
        advanced_result=adv.compute_all(),
        confluence_output=cs.compute_score(...),
        trade_decision=se.evaluate(...),
    )
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("trading_bot.trade_snapshot")


class TradeSnapshot:
    """Captures chart screenshot + full indicator state at every trade.

    Renders a multi-panel matplotlib chart (candlestick OHLC with
    indicator overlays, RSI subplot, MACD subplot) and saves it as
    PNG alongside a JSON file containing the complete data state.

    Args:
        base_dir: Root directory for snapshot storage.  Snapshots are
            saved under ``{base_dir}/{instrument}/snapshots/{YYYYMMDD}/``.
    """

    def __init__(self, base_dir: str = "Forex Trading Team/Data") -> None:
        self.base_dir = Path(base_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def capture(
        self,
        instrument: str,
        candles: List[Dict[str, Any]],
        indicators_result: Optional[Dict[str, Any]] = None,
        advanced_result: Optional[Dict[str, Any]] = None,
        confluence_output: Optional[Dict[str, Any]] = None,
        trade_decision: Optional[Dict[str, Any]] = None,
        pattern_results: Optional[Dict[str, Any]] = None,
        news_data: Optional[Dict[str, Any]] = None,
        timeframe: str = "M15",
    ) -> Dict[str, Any]:
        """Capture full trade snapshot: chart image + data JSON.

        Args:
            instrument: Oanda instrument name (e.g. ``'EUR_USD'``).
            candles: Raw Oanda candle dicts.
            indicators_result: Output from ``Indicators.compute_all()``.
            advanced_result: Output from ``AdvancedIndicators.compute_all()``.
            confluence_output: Output from ``ConfluenceScorer.compute_score()``.
            trade_decision: Output from ``StrategyEngine.evaluate()``.
            pattern_results: Combined pattern scan output.
            news_data: News intelligence snapshot.
            timeframe: Candle timeframe label (default ``'M15'``).

        Returns:
            Dict with keys:

            - ``snapshot_id``: str -- timestamp-based unique ID.
            - ``image_path``: str -- path to saved PNG.
            - ``data_path``: str -- path to saved JSON.
            - ``instrument``: str
            - ``timestamp``: str -- ISO 8601.
            - ``action``: str -- from trade_decision or ``'snapshot'``.
        """
        now = datetime.now(timezone.utc)
        snapshot_id = self._make_snapshot_id(now, instrument)
        timestamp_iso = now.isoformat()

        action = "snapshot"
        if trade_decision and isinstance(trade_decision, dict):
            action = trade_decision.get("action", "snapshot")

        score = None
        if confluence_output and isinstance(confluence_output, dict):
            score = confluence_output.get("total_score")
        if score is None and trade_decision and isinstance(trade_decision, dict):
            conf = trade_decision.get("confluence", {})
            if isinstance(conf, dict):
                score = conf.get("total_score")

        # Build output directory
        date_str = now.strftime("%Y%m%d")
        snap_dir = self.base_dir / instrument / "snapshots" / date_str
        snap_dir.mkdir(parents=True, exist_ok=True)

        image_path = snap_dir / f"{snapshot_id}.png"
        data_path = snap_dir / f"{snapshot_id}.json"

        # Render chart
        self._render_chart(
            candles=candles,
            instrument=instrument,
            timeframe=timeframe,
            action=action,
            timestamp_iso=timestamp_iso,
            score=score,
            indicators_result=indicators_result,
            advanced_result=advanced_result,
            pattern_results=pattern_results,
            save_path=str(image_path),
        )

        # Save data JSON
        snapshot_data = {
            "snapshot_id": snapshot_id,
            "instrument": instrument,
            "timeframe": timeframe,
            "timestamp": timestamp_iso,
            "action": action,
            "image_path": str(image_path),
            "trade_decision": self._serialise(trade_decision),
            "confluence": self._serialise(confluence_output),
            "indicators_core": self._serialise(indicators_result),
            "indicators_advanced": self._serialise(advanced_result),
            "patterns": self._serialise(pattern_results),
            "news": self._serialise(news_data),
            "outcome": None,
        }

        with open(data_path, "w") as f:
            json.dump(snapshot_data, f, indent=2, default=str)

        logger.info(
            "Snapshot captured: %s (%s %s)", snapshot_id, instrument, action
        )

        return {
            "snapshot_id": snapshot_id,
            "image_path": str(image_path),
            "data_path": str(data_path),
            "instrument": instrument,
            "timestamp": timestamp_iso,
            "action": action,
        }

    # ------------------------------------------------------------------
    # Snapshot ID
    # ------------------------------------------------------------------

    @staticmethod
    def _make_snapshot_id(dt: datetime, instrument: str) -> str:
        """Create a timestamp-based unique snapshot ID.

        Format: ``YYYYMMDD_HHMMSS_INSTRUMENT``.
        """
        return f"{dt.strftime('%Y%m%d_%H%M%S')}_{instrument}"

    # ------------------------------------------------------------------
    # Chart rendering
    # ------------------------------------------------------------------

    def _render_chart(
        self,
        candles: List[Dict[str, Any]],
        instrument: str,
        timeframe: str,
        action: str,
        timestamp_iso: str,
        score: Optional[float],
        indicators_result: Optional[Dict[str, Any]],
        advanced_result: Optional[Dict[str, Any]],
        pattern_results: Optional[Dict[str, Any]],
        save_path: str,
    ) -> None:
        """Render a multi-panel chart and save as PNG.

        Panel layout:
        1. Main chart: OHLC candlesticks + EMA + Bollinger + Fibonacci
        2. Subplot 1: RSI with overbought/oversold lines
        3. Subplot 2: MACD line + signal + histogram
        """
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        # Parse candles into arrays (last 50 for readability)
        ohlc = self._parse_candles(candles, tail=50)
        if ohlc is None or len(ohlc["close"]) == 0:
            logger.warning("No candle data to render for %s", instrument)
            # Create a minimal placeholder
            fig, ax = plt.subplots(figsize=(12, 8))
            ax.text(0.5, 0.5, "No candle data", ha="center", va="center",
                    fontsize=16, transform=ax.transAxes)
            fig.savefig(save_path, dpi=100, bbox_inches="tight")
            plt.close(fig)
            return

        n = len(ohlc["close"])
        x = list(range(n))

        # Build figure with 3 panels: main (60%), RSI (20%), MACD (20%)
        fig, (ax_main, ax_rsi, ax_macd) = plt.subplots(
            3, 1, figsize=(12, 8),
            gridspec_kw={"height_ratios": [3, 1, 1]},
            sharex=True,
        )
        fig.subplots_adjust(hspace=0.05)

        # --- Main panel: candlesticks ---
        self._draw_candlesticks(ax_main, ohlc, x)

        # --- Indicator overlays on main ---
        if indicators_result and isinstance(indicators_result, dict):
            self._overlay_emas(ax_main, indicators_result, n)
            self._overlay_bollinger(ax_main, indicators_result, n)

        if advanced_result and isinstance(advanced_result, dict):
            self._overlay_fibonacci(ax_main, advanced_result, n)

        # --- Pattern annotations ---
        if pattern_results and isinstance(pattern_results, dict):
            self._annotate_patterns(ax_main, pattern_results, ohlc)

        # Title
        score_str = f" -- Score: {score:.1f}" if score is not None else ""
        ax_main.set_title(
            f"{instrument} {timeframe} -- {action} @ {timestamp_iso[:19]}{score_str}",
            fontsize=10,
        )
        ax_main.set_ylabel("Price")
        ax_main.grid(True, alpha=0.3)
        ax_main.legend(loc="upper left", fontsize=7, ncol=3)

        # --- RSI subplot ---
        self._draw_rsi(ax_rsi, indicators_result, n)

        # --- MACD subplot ---
        self._draw_macd(ax_macd, indicators_result, n)

        # X-axis labels: show a few time labels
        if ohlc["time"]:
            tick_indices = list(range(0, n, max(1, n // 6)))
            ax_macd.set_xticks(tick_indices)
            ax_macd.set_xticklabels(
                [ohlc["time"][i][:16] if i < len(ohlc["time"]) else ""
                 for i in tick_indices],
                rotation=45, fontsize=7,
            )

        fig.savefig(save_path, dpi=100, bbox_inches="tight")
        plt.close(fig)

    # ------------------------------------------------------------------
    # Candle parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_candles(
        candles: List[Dict[str, Any]], tail: int = 50
    ) -> Optional[Dict[str, List]]:
        """Extract OHLCV arrays from Oanda candle dicts.

        Returns the last *tail* complete candles as parallel lists.
        """
        complete = [c for c in candles if c.get("complete", True)]
        if not complete:
            return None

        complete = complete[-tail:]

        opens, highs, lows, closes, volumes, times = [], [], [], [], [], []
        for c in complete:
            mid = c.get("mid", {})
            if not mid:
                continue
            try:
                opens.append(float(mid.get("o", 0)))
                highs.append(float(mid.get("h", 0)))
                lows.append(float(mid.get("l", 0)))
                closes.append(float(mid.get("c", 0)))
                volumes.append(int(c.get("volume", 0)))
                times.append(str(c.get("time", "")))
            except (TypeError, ValueError):
                continue

        if not closes:
            return None

        return {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "time": times,
        }

    # ------------------------------------------------------------------
    # Candlestick drawing
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_candlesticks(
        ax: Any, ohlc: Dict[str, List], x: List[int]
    ) -> None:
        """Draw OHLC candlesticks using matplotlib rectangles."""
        for i in range(len(x)):
            o = ohlc["open"][i]
            h = ohlc["high"][i]
            l_ = ohlc["low"][i]
            c = ohlc["close"][i]

            color = "#26a69a" if c >= o else "#ef5350"  # green / red

            # Wick (high-low line)
            ax.plot([x[i], x[i]], [l_, h], color=color, linewidth=0.8)

            # Body (open-close rectangle)
            body_bottom = min(o, c)
            body_height = abs(c - o) or (h - l_) * 0.01  # min visible
            ax.bar(
                x[i], body_height, bottom=body_bottom, width=0.6,
                color=color, edgecolor=color, linewidth=0.5,
            )

    # ------------------------------------------------------------------
    # Indicator overlays
    # ------------------------------------------------------------------

    @staticmethod
    def _overlay_emas(
        ax: Any, indicators: Dict[str, Any], n: int
    ) -> None:
        """Overlay EMA lines on the main chart."""
        emas = indicators.get("emas", {})
        colors = {9: "#2196F3", 21: "#FF9800", 50: "#9C27B0"}

        for period, color in colors.items():
            series = emas.get(period)
            if series is None:
                continue
            try:
                if hasattr(series, "values"):
                    vals = series.values[-n:]
                elif isinstance(series, (list, np.ndarray)):
                    vals = np.array(series)[-n:]
                else:
                    continue
                vals = [float(v) if not (isinstance(v, float) and np.isnan(v))
                        else None for v in vals]
                x = list(range(len(vals)))
                # Filter out None for plotting
                plot_x = [xi for xi, v in zip(x, vals) if v is not None]
                plot_v = [v for v in vals if v is not None]
                if plot_v:
                    ax.plot(plot_x, plot_v, linewidth=1, alpha=0.8,
                            color=color, label=f"EMA {period}")
            except Exception:
                pass

    @staticmethod
    def _overlay_bollinger(
        ax: Any, indicators: Dict[str, Any], n: int
    ) -> None:
        """Overlay Bollinger Band lines on the main chart."""
        bb = indicators.get("bollinger", {})
        if not isinstance(bb, dict):
            return

        upper = bb.get("upper")
        middle = bb.get("middle")
        lower = bb.get("lower")

        # These are typically scalar last values; draw as horizontal lines
        if upper is not None and lower is not None and middle is not None:
            try:
                ax.axhline(y=float(upper), color="gray", linestyle="--",
                           linewidth=0.7, alpha=0.5, label="BB Upper")
                ax.axhline(y=float(middle), color="gray", linestyle="-.",
                           linewidth=0.5, alpha=0.4)
                ax.axhline(y=float(lower), color="gray", linestyle="--",
                           linewidth=0.7, alpha=0.5, label="BB Lower")
            except (TypeError, ValueError):
                pass

    @staticmethod
    def _overlay_fibonacci(
        ax: Any, advanced: Dict[str, Any], n: int
    ) -> None:
        """Overlay Fibonacci retracement levels on the main chart."""
        fib = advanced.get("fibonacci", {})
        if not isinstance(fib, dict):
            return

        levels = fib.get("retracement_levels", {})
        if not isinstance(levels, dict):
            return

        fib_colors = {
            0.236: "#B39DDB", 0.382: "#9575CD",
            0.5: "#7E57C2", 0.618: "#673AB7", 0.786: "#512DA8",
        }
        for level, price in levels.items():
            try:
                lvl = float(level)
                px = float(price)
                if lvl in (0.0, 1.0):
                    continue  # Skip swing high/low (clutters chart)
                color = fib_colors.get(lvl, "#9E9E9E")
                ax.axhline(y=px, color=color, linestyle=":",
                           linewidth=0.6, alpha=0.6)
                ax.text(n - 1, px, f"  {lvl:.3f}", fontsize=6,
                        va="center", color=color, alpha=0.8)
            except (TypeError, ValueError):
                continue

    # ------------------------------------------------------------------
    # Pattern annotations
    # ------------------------------------------------------------------

    @staticmethod
    def _annotate_patterns(
        ax: Any, pattern_results: Dict[str, Any],
        ohlc: Dict[str, List],
    ) -> None:
        """Annotate detected patterns with arrows/labels."""
        patterns = pattern_results.get("candlestick_patterns", [])
        if not isinstance(patterns, list):
            return

        n = len(ohlc["close"])
        for p in patterns[:5]:  # Limit to 5 annotations
            if not isinstance(p, dict):
                continue
            name = p.get("name", p.get("pattern", ""))
            direction = p.get("direction", "")
            idx = p.get("bar_index")

            if idx is not None and 0 <= idx < n:
                price = ohlc["high"][idx] if direction == "bullish" else ohlc["low"][idx]
                arrow_dir = "bullish" if direction == "bullish" else "bearish"
                color = "#26a69a" if arrow_dir == "bullish" else "#ef5350"
                offset = 10 if arrow_dir == "bullish" else -10
                ax.annotate(
                    name[:15], xy=(idx, price),
                    xytext=(idx, price), fontsize=5,
                    color=color, ha="center",
                    arrowprops=dict(arrowstyle="->", color=color, lw=0.5),
                )

    # ------------------------------------------------------------------
    # RSI subplot
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_rsi(
        ax: Any, indicators: Optional[Dict[str, Any]], n: int
    ) -> None:
        """Draw RSI line with overbought/oversold reference lines."""
        ax.axhline(y=70, color="red", linestyle="--", linewidth=0.5, alpha=0.5)
        ax.axhline(y=30, color="green", linestyle="--", linewidth=0.5, alpha=0.5)
        ax.axhline(y=50, color="gray", linestyle="-.", linewidth=0.3, alpha=0.3)
        ax.set_ylabel("RSI", fontsize=8)
        ax.set_ylim(0, 100)
        ax.grid(True, alpha=0.2)

        if indicators is None or not isinstance(indicators, dict):
            return

        rsi_data = indicators.get("rsi", {})
        if not isinstance(rsi_data, dict):
            return

        series = rsi_data.get("series")
        if series is not None and hasattr(series, "values"):
            vals = series.values[-n:]
            x = list(range(len(vals)))
            valid_x = [xi for xi, v in zip(x, vals)
                       if not (isinstance(v, float) and np.isnan(v))]
            valid_v = [float(v) for v in vals
                       if not (isinstance(v, float) and np.isnan(v))]
            if valid_v:
                ax.plot(valid_x, valid_v, color="#2196F3", linewidth=1,
                        label="RSI(14)")
                ax.legend(loc="upper left", fontsize=6)

    # ------------------------------------------------------------------
    # MACD subplot
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_macd(
        ax: Any, indicators: Optional[Dict[str, Any]], n: int
    ) -> None:
        """Draw MACD line, signal line, and histogram bars."""
        ax.set_ylabel("MACD", fontsize=8)
        ax.axhline(y=0, color="gray", linewidth=0.3)
        ax.grid(True, alpha=0.2)

        if indicators is None or not isinstance(indicators, dict):
            return

        macd_data = indicators.get("macd", {})
        if not isinstance(macd_data, dict):
            return

        # For scalar values (last-bar only), draw as horizontal reference
        macd_val = macd_data.get("macd")
        signal_val = macd_data.get("signal")
        hist_val = macd_data.get("histogram")

        if macd_val is not None and signal_val is not None:
            # Draw a single-bar representation
            ax.bar([n - 1], [float(hist_val or 0)], width=0.6,
                   color="#26a69a" if (hist_val or 0) >= 0 else "#ef5350",
                   alpha=0.6, label="Histogram")
            ax.plot([n - 1], [float(macd_val)], "o", color="#2196F3",
                    markersize=3, label="MACD")
            ax.plot([n - 1], [float(signal_val)], "o", color="#FF9800",
                    markersize=3, label="Signal")
            ax.legend(loc="upper left", fontsize=6)

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @classmethod
    def _serialise(cls, obj: Any) -> Any:
        """Convert complex types to JSON-safe Python primitives.

        Handles pandas Series/DataFrame, numpy arrays/scalars, datetime,
        and None values.
        """
        if obj is None:
            return None

        if isinstance(obj, dict):
            return {str(k): cls._serialise(v) for k, v in obj.items()}

        if isinstance(obj, list):
            return [cls._serialise(item) for item in obj]

        if isinstance(obj, pd.Series):
            return float(obj.iloc[-1]) if len(obj) > 0 and not obj.empty else None

        if isinstance(obj, pd.DataFrame):
            return obj.to_dict(orient="list")

        if isinstance(obj, np.ndarray):
            return obj.tolist()

        if isinstance(obj, (np.integer,)):
            return int(obj)

        if isinstance(obj, (np.floating,)):
            val = float(obj)
            if np.isnan(val) or np.isinf(val):
                return None
            return val

        if isinstance(obj, (np.bool_,)):
            return bool(obj)

        if isinstance(obj, datetime):
            return obj.isoformat()

        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()

        # Catch remaining numpy types
        if hasattr(obj, "item"):
            try:
                return obj.item()
            except Exception:
                pass

        return obj
