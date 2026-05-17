"""
Chart rendering module for the trading bot.

Produces publication-quality trade charts with:
- OHLC candlestick chart with entry/exit price markers
- EMA overlays (21, 55, 100)
- RSI subplot with overbought/oversold bands
- MACD subplot with histogram
- Pattern annotations showing which patterns triggered
- Confluence score and trade rationale text box

Designed to integrate with :class:`TradeSnapshot` -- call
:func:`render_trade_chart` as a drop-in replacement for the
snapshot's internal ``_render_chart`` method, or use standalone.

Usage::

    from Source.chart_renderer import render_trade_chart

    render_trade_chart(
        candles=candles,
        instrument="EUR_USD",
        save_path="chart.png",
        indicators_result=ind.compute_all(),
        entry_price=1.0850,
        exit_price=1.0900,
        direction="buy",
        confluence_score=82.5,
        trade_rationale="Strong EMA alignment + bullish engulfing at 0.618 fib",
        pattern_results=pi.scan(),
    )
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger("trading_bot.chart_renderer")

# EMA periods for overlays
_EMA_PERIODS = [21, 55, 100]
_EMA_COLORS = {21: "#2196F3", 55: "#FF9800", 100: "#9C27B0"}


# ------------------------------------------------------------------
# Candle parsing
# ------------------------------------------------------------------

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
# Drawing helpers
# ------------------------------------------------------------------

def _draw_candlesticks(ax: Any, ohlc: Dict[str, List], x: List[int]) -> None:
    """Draw OHLC candlestick bodies and wicks."""
    for i in range(len(x)):
        o = ohlc["open"][i]
        h = ohlc["high"][i]
        l_ = ohlc["low"][i]
        c = ohlc["close"][i]
        color = "#26a69a" if c >= o else "#ef5350"
        ax.plot([x[i], x[i]], [l_, h], color=color, linewidth=0.8)
        body_bottom = min(o, c)
        body_height = abs(c - o) or (h - l_) * 0.01
        ax.bar(
            x[i], body_height, bottom=body_bottom, width=0.6,
            color=color, edgecolor=color, linewidth=0.5,
        )


def _overlay_emas(
    ax: Any, indicators: Dict[str, Any], n: int
) -> None:
    """Overlay EMA 21/55/100 lines on the main chart."""
    emas = indicators.get("emas", {})

    for period in _EMA_PERIODS:
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
            vals = [
                float(v) if not (isinstance(v, float) and np.isnan(v)) else None
                for v in vals
            ]
            xs = list(range(len(vals)))
            plot_x = [xi for xi, v in zip(xs, vals) if v is not None]
            plot_v = [v for v in vals if v is not None]
            if plot_v:
                ax.plot(
                    plot_x, plot_v, linewidth=1, alpha=0.8,
                    color=_EMA_COLORS[period], label=f"EMA {period}",
                )
        except Exception:
            pass


def _draw_entry_exit(
    ax: Any,
    ohlc: Dict[str, List],
    entry_price: Optional[float],
    exit_price: Optional[float],
    direction: Optional[str],
) -> None:
    """Draw horizontal entry/exit markers with direction arrows."""
    n = len(ohlc["close"])
    if entry_price is not None:
        color = "#26a69a" if direction == "buy" else "#ef5350"
        ax.axhline(
            y=entry_price, color=color, linestyle="-", linewidth=1.2, alpha=0.8,
        )
        marker = "^" if direction == "buy" else "v"
        ax.plot(
            n - 1, entry_price, marker=marker, color=color,
            markersize=10, zorder=5, label=f"Entry ({direction})",
        )
    if exit_price is not None:
        ax.axhline(
            y=exit_price, color="#FFC107", linestyle="--", linewidth=1.0, alpha=0.7,
        )
        ax.plot(
            n - 1, exit_price, marker="x", color="#FFC107",
            markersize=10, zorder=5, label="Exit",
        )


def _annotate_patterns(
    ax: Any, pattern_results: Dict[str, Any], ohlc: Dict[str, List]
) -> None:
    """Annotate detected patterns with small labels."""
    patterns = pattern_results.get("candlestick_patterns", [])
    if not isinstance(patterns, list):
        return

    n = len(ohlc["close"])
    for p in patterns[:5]:
        if not isinstance(p, dict):
            continue
        name = p.get("name", p.get("pattern", ""))
        pat_dir = p.get("direction", "")
        idx = p.get("bar_index")

        if idx is not None and 0 <= idx < n:
            price = (
                ohlc["high"][idx] if pat_dir == "bullish"
                else ohlc["low"][idx]
            )
            color = "#26a69a" if pat_dir == "bullish" else "#ef5350"
            ax.annotate(
                name[:15], xy=(idx, price),
                xytext=(idx, price), fontsize=5,
                color=color, ha="center",
                arrowprops=dict(arrowstyle="->", color=color, lw=0.5),
            )


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
        xs = list(range(len(vals)))
        valid_x = [
            xi for xi, v in zip(xs, vals)
            if not (isinstance(v, float) and np.isnan(v))
        ]
        valid_v = [
            float(v) for v in vals
            if not (isinstance(v, float) and np.isnan(v))
        ]
        if valid_v:
            ax.plot(valid_x, valid_v, color="#2196F3", linewidth=1, label="RSI(14)")
            ax.legend(loc="upper left", fontsize=6)


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

    macd_val = macd_data.get("macd")
    signal_val = macd_data.get("signal")
    hist_val = macd_data.get("histogram")

    if macd_val is not None and signal_val is not None:
        ax.bar(
            [n - 1], [float(hist_val or 0)], width=0.6,
            color="#26a69a" if (hist_val or 0) >= 0 else "#ef5350",
            alpha=0.6, label="Histogram",
        )
        ax.plot(
            [n - 1], [float(macd_val)], "o", color="#2196F3",
            markersize=3, label="MACD",
        )
        ax.plot(
            [n - 1], [float(signal_val)], "o", color="#FF9800",
            markersize=3, label="Signal",
        )
        ax.legend(loc="upper left", fontsize=6)


def _add_info_box(
    fig: Any,
    confluence_score: Optional[float],
    trade_rationale: Optional[str],
) -> None:
    """Add a text box with confluence score and trade rationale."""
    lines = []
    if confluence_score is not None:
        lines.append(f"Confluence Score: {confluence_score:.1f} / 100")
    if trade_rationale:
        lines.append(f"Rationale: {trade_rationale}")
    if not lines:
        return

    text = "\n".join(lines)
    fig.text(
        0.01, 0.01, text, fontsize=7,
        verticalalignment="bottom",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="#ECEFF1", alpha=0.8),
    )


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def render_trade_chart(
    candles: List[Dict[str, Any]],
    instrument: str,
    save_path: str,
    timeframe: str = "H1",
    indicators_result: Optional[Dict[str, Any]] = None,
    advanced_result: Optional[Dict[str, Any]] = None,
    pattern_results: Optional[Dict[str, Any]] = None,
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    direction: Optional[str] = None,
    confluence_score: Optional[float] = None,
    trade_rationale: Optional[str] = None,
    action: Optional[str] = None,
    timestamp_label: Optional[str] = None,
    tail: int = 50,
) -> str:
    """Render a full trade chart and save as PNG.

    Args:
        candles: Raw Oanda candle dicts.
        instrument: Oanda instrument name (e.g. ``'EUR_USD'``).
        save_path: Destination file path for the PNG.
        timeframe: Candle timeframe label (default ``'H1'``).
        indicators_result: Output from ``Indicators.compute_all()``.
        advanced_result: Output from ``AdvancedIndicators.compute_all()``.
        pattern_results: Output from ``PatternIntegration.scan()``.
        entry_price: Trade entry price for marker.
        exit_price: Trade exit/TP price for marker.
        direction: ``'buy'`` or ``'sell'`` for entry arrow direction.
        confluence_score: 0-100 confluence score to display.
        trade_rationale: Human-readable trade rationale string.
        action: Action label for chart title (e.g. ``'buy'``).
        timestamp_label: ISO timestamp for chart title.
        tail: Number of candles to display (default 50).

    Returns:
        The *save_path* string on success.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ohlc = _parse_candles(candles, tail=tail)
    if ohlc is None or len(ohlc["close"]) == 0:
        logger.warning("No candle data to render for %s", instrument)
        fig, ax = plt.subplots(figsize=(14, 9))
        ax.text(
            0.5, 0.5, "No candle data", ha="center", va="center",
            fontsize=16, transform=ax.transAxes,
        )
        fig.savefig(save_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return save_path

    n = len(ohlc["close"])
    x = list(range(n))

    # 3-panel layout: main (60%), RSI (20%), MACD (20%)
    fig, (ax_main, ax_rsi, ax_macd) = plt.subplots(
        3, 1, figsize=(14, 9),
        gridspec_kw={"height_ratios": [3, 1, 1]},
        sharex=True,
    )
    fig.subplots_adjust(hspace=0.05, bottom=0.10)

    # --- Main panel: candlesticks ---
    _draw_candlesticks(ax_main, ohlc, x)

    # --- EMA overlays (21/55/100) ---
    if indicators_result and isinstance(indicators_result, dict):
        _overlay_emas(ax_main, indicators_result, n)

    # --- Entry / exit markers ---
    _draw_entry_exit(ax_main, ohlc, entry_price, exit_price, direction)

    # --- Pattern annotations ---
    if pattern_results and isinstance(pattern_results, dict):
        _annotate_patterns(ax_main, pattern_results, ohlc)

    # Title
    score_str = f" | Score: {confluence_score:.1f}" if confluence_score is not None else ""
    action_str = action or direction or "snapshot"
    ts_str = f" @ {timestamp_label[:19]}" if timestamp_label else ""
    ax_main.set_title(
        f"{instrument} {timeframe} -- {action_str}{ts_str}{score_str}",
        fontsize=10,
    )
    ax_main.set_ylabel("Price")
    ax_main.grid(True, alpha=0.3)
    ax_main.legend(loc="upper left", fontsize=7, ncol=3)

    # --- RSI subplot ---
    _draw_rsi(ax_rsi, indicators_result, n)

    # --- MACD subplot ---
    _draw_macd(ax_macd, indicators_result, n)

    # X-axis time labels
    if ohlc["time"]:
        tick_indices = list(range(0, n, max(1, n // 6)))
        ax_macd.set_xticks(tick_indices)
        ax_macd.set_xticklabels(
            [
                ohlc["time"][i][:16] if i < len(ohlc["time"]) else ""
                for i in tick_indices
            ],
            rotation=45, fontsize=7,
        )

    # --- Info box with confluence score + rationale ---
    _add_info_box(fig, confluence_score, trade_rationale)

    fig.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close(fig)

    logger.info("Chart rendered: %s (%s)", save_path, instrument)
    return save_path
