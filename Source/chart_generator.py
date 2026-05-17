#!/usr/bin/env python3
"""
Generate candlestick charts with EMA overlays and Bollinger Bands
for specified forex pairs using live OANDA data.
"""

import logging
import sys
import os

# Ensure Source dir is on path for config/oanda_client imports
_src = os.path.dirname(os.path.abspath(__file__))
if _src not in sys.path:
    sys.path.insert(0, _src)

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from datetime import datetime, timezone

from oanda_client import OandaClient

logger = logging.getLogger(__name__)

OUTPUT_DIR = os.environ.get(
    "CHART_OUTPUT_DIR",
    os.path.join(os.path.expanduser("~"), "Documents", "Cowork Files", "outputs"),
)
os.makedirs(OUTPUT_DIR, exist_ok=True)

PAIRS = ["GBP_JPY", "USD_CAD", "EUR_GBP"]
GRANULARITY = "M15"
COUNT = 120  # extra candles for EMA warm-up, we'll display ~100


def fetch_candles(client, instrument, granularity, count):
    """Fetch candle data from OANDA."""
    candles = client.get_candles(
        instrument=instrument,
        granularity=granularity,
        count=count,
        price="M",
    )
    # Parse into arrays
    times, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for c in candles:
        if not c.get("complete", True) and c != candles[-1]:
            continue  # skip incomplete except most recent
        t = c["time"].replace("Z", "+00:00")
        # Truncate nanoseconds
        if "." in t:
            base, frac = t.split(".", 1)
            offset = ""
            for sep in ("+", "-"):
                if sep in frac[1:]:
                    idx = frac.index(sep, 1)
                    offset = frac[idx:]
                    frac = frac[:idx]
                    break
            if not offset:
                offset = "+00:00"
            frac = frac[:6].ljust(6, "0")
            t = f"{base}.{frac}{offset}"
        else:
            t = t + "+00:00" if "+" not in t and "-" not in t[1:] else t
        
        dt = datetime.fromisoformat(t)
        mid = c.get("mid", {})
        times.append(dt)
        opens.append(float(mid["o"]))
        highs.append(float(mid["h"]))
        lows.append(float(mid["l"]))
        closes.append(float(mid["c"]))
        volumes.append(int(c.get("volume", 0)))
    
    return {
        "time": np.array(times),
        "open": np.array(opens),
        "high": np.array(highs),
        "low": np.array(lows),
        "close": np.array(closes),
        "volume": np.array(volumes),
    }


def calc_ema(data, period):
    """Calculate Exponential Moving Average."""
    ema = np.zeros_like(data)
    multiplier = 2.0 / (period + 1)
    ema[0] = data[0]
    for i in range(1, len(data)):
        ema[i] = (data[i] - ema[i-1]) * multiplier + ema[i-1]
    return ema


def calc_bollinger(data, period=20, num_std=2):
    """Calculate Bollinger Bands."""
    sma = np.convolve(data, np.ones(period)/period, mode='valid')
    # Pad the beginning with NaN
    pad = np.full(period - 1, np.nan)
    sma = np.concatenate([pad, sma])
    
    std = np.full_like(data, np.nan, dtype=float)
    for i in range(period - 1, len(data)):
        std[i] = np.std(data[i-period+1:i+1])
    
    upper = sma + num_std * std
    lower = sma - num_std * std
    return sma, upper, lower


def plot_chart(candle_data, instrument, output_path):
    """Generate a professional candlestick chart with indicators."""
    times = candle_data["time"]
    o = candle_data["open"]
    h = candle_data["high"]
    l = candle_data["low"]
    c = candle_data["close"]
    
    # Display last 200 candles but use all for EMA calculation.
    # 2026-04-27: increased from 100 → 200 (~50 hours of M15 context) so the
    # TA agent and validator can see prior swing highs/lows, multi-day S/R
    # levels, and the lead-in to the current setup. Candles are already
    # fetched at count=250 by trading_cycle.py so no extra OANDA cost.
    display_start = max(0, len(c) - 200)
    
    # Calculate indicators on full data
    ema21 = calc_ema(c, 21)
    ema55 = calc_ema(c, 55)
    ema100 = calc_ema(c, 100)
    bb_mid, bb_upper, bb_lower = calc_bollinger(c, 20, 2)
    
    # Slice for display
    t = times[display_start:]
    o_d = o[display_start:]
    h_d = h[display_start:]
    l_d = l[display_start:]
    c_d = c[display_start:]
    ema21_d = ema21[display_start:]
    ema55_d = ema55[display_start:]
    ema100_d = ema100[display_start:]
    bb_mid_d = bb_mid[display_start:]
    bb_upper_d = bb_upper[display_start:]
    bb_lower_d = bb_lower[display_start:]
    
    # Convert times to matplotlib format
    t_num = mdates.date2num(t)
    
    # --- Styling ---
    bg_color = "#1a1a2e"
    panel_color = "#16213e"
    grid_color = "#2a2a4a"
    text_color = "#e0e0e0"
    bull_color = "#00e676"
    bear_color = "#ff1744"
    ema21_color = "#ffeb3b"
    ema55_color = "#ff9800"
    ema100_color = "#f44336"
    bb_color = "#7c4dff"
    bb_fill = "#7c4dff"
    
    fig, ax = plt.subplots(1, 1, figsize=(16, 9), facecolor=bg_color)
    ax.set_facecolor(panel_color)
    
    # Candlesticks
    width = 0.004  # width in date units for M15
    for i in range(len(t)):
        color = bull_color if c_d[i] >= o_d[i] else bear_color
        # Wick
        ax.plot([t_num[i], t_num[i]], [l_d[i], h_d[i]], color=color, linewidth=0.8)
        # Body
        body_low = min(o_d[i], c_d[i])
        body_high = max(o_d[i], c_d[i])
        body_height = body_high - body_low
        if body_height < 1e-10:
            body_height = (h_d[i] - l_d[i]) * 0.01 or 0.0001
        rect = plt.Rectangle((t_num[i] - width/2, body_low), width, body_height,
                              facecolor=color, edgecolor=color, linewidth=0.5,
                              alpha=0.9)
        ax.add_patch(rect)
    
    # EMAs
    ax.plot(t_num, ema21_d, color=ema21_color, linewidth=1.5, label="EMA 21", alpha=0.9)
    ax.plot(t_num, ema55_d, color=ema55_color, linewidth=1.5, label="EMA 55", alpha=0.9)
    ax.plot(t_num, ema100_d, color=ema100_color, linewidth=1.5, label="EMA 100", alpha=0.9)
    
    # Bollinger Bands
    valid = ~np.isnan(bb_upper_d)
    ax.plot(t_num[valid], bb_upper_d[valid], color=bb_color, linewidth=1.0, linestyle="--", alpha=0.7, label="BB Upper")
    ax.plot(t_num[valid], bb_lower_d[valid], color=bb_color, linewidth=1.0, linestyle="--", alpha=0.7, label="BB Lower")
    ax.plot(t_num[valid], bb_mid_d[valid], color=bb_color, linewidth=0.7, linestyle=":", alpha=0.5)
    ax.fill_between(t_num[valid], bb_lower_d[valid], bb_upper_d[valid],
                     color=bb_fill, alpha=0.08)
    
    # Current price line
    current_price = c_d[-1]
    ax.axhline(y=current_price, color="#00bcd4", linewidth=1.0, linestyle="-.", alpha=0.8)
    ax.annotate(f"  {current_price:.5f}", xy=(t_num[-1], current_price),
                fontsize=10, color="#00bcd4", fontweight="bold",
                va="center", ha="left",
                bbox=dict(boxstyle="round,pad=0.2", facecolor=bg_color, edgecolor="#00bcd4", alpha=0.8))
    
    # Formatting
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M", tz=timezone.utc))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=8, color=text_color)
    plt.setp(ax.get_yticklabels(), fontsize=9, color=text_color)
    
    ax.grid(True, alpha=0.15, color=grid_color, linewidth=0.5)
    ax.tick_params(colors=text_color, which="both")
    for spine in ax.spines.values():
        spine.set_color(grid_color)
    
    # Title
    pair_display = instrument.replace("_", "/")
    last_time = t[-1].strftime("%Y-%m-%d %H:%M UTC")
    
    # EMA fan analysis
    ema_order = ""
    if ema21_d[-1] < ema55_d[-1] < ema100_d[-1]:
        ema_order = "BEARISH FAN"
    elif ema21_d[-1] > ema55_d[-1] > ema100_d[-1]:
        ema_order = "BULLISH FAN"
    else:
        ema_order = "MIXED/TRANSITIONING"
    
    # EMA separation
    spread_21_55 = abs(ema21_d[-1] - ema55_d[-1])
    spread_21_55_prev = abs(ema21_d[-10] - ema55_d[-10]) if len(ema21_d) > 10 else spread_21_55
    ema_sep = "SEPARATING" if spread_21_55 > spread_21_55_prev else "COMPRESSING"
    
    # BB width analysis
    bb_widths = bb_upper_d[valid] - bb_lower_d[valid]
    if len(bb_widths) >= 10:
        bb_trend = "EXPANDING" if bb_widths[-1] > bb_widths[-10] else "CONTRACTING"
    else:
        bb_trend = "N/A"
    
    title = f"{pair_display}  M15  |  {last_time}\nEMAs: {ema_order} ({ema_sep})  |  BBs: {bb_trend}  |  Price: {current_price:.5f}"
    ax.set_title(title, fontsize=13, fontweight="bold", color=text_color, pad=15)
    
    # Legend
    legend = ax.legend(loc="upper left", fontsize=8, facecolor=panel_color, edgecolor=grid_color,
                       labelcolor=text_color, framealpha=0.9)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, facecolor=bg_color, edgecolor="none", bbox_inches="tight")
    plt.close(fig)
    
    logger.info(
        "%s M15 | Price=%.5f EMA21=%.5f EMA55=%.5f EMA100=%.5f "
        "Fan=%s Trend=%s BB_upper=%.5f BB_lower=%.5f BB_width=%.5f BB_trend=%s | saved=%s",
        pair_display, current_price,
        ema21_d[-1], ema55_d[-1], ema100_d[-1],
        ema_order, ema_sep,
        bb_upper_d[valid][-1], bb_lower_d[valid][-1], bb_widths[-1], bb_trend,
        output_path,
    )
    
    return {
        "price": current_price,
        "ema21": ema21_d[-1],
        "ema55": ema55_d[-1],
        "ema100": ema100_d[-1],
        "ema_fan": ema_order,
        "ema_trend": ema_sep,
        "bb_trend": bb_trend,
        "bb_width": bb_widths[-1] if len(bb_widths) > 0 else 0,
    }


def generate_chart(pair: str, df, snipe_levels=None, user_annotations=None,
                   pivot_levels=None, swing_overlay=None, pattern_labels=None) -> str:
    """Generate a candlestick chart from a DataFrame and return the saved file path.

    This is the entry point called by trading_cycle.py during live analysis.

    Args:
        pair: Instrument name e.g. "AUD_JPY"
        df: pandas DataFrame with columns: time (ISO string), open, high, low, close, volume
        snipe_levels: optional list of dicts with at least a "price" key — drawn as horizontal lines
        user_annotations: optional list of annotation dicts (currently unused in rendering)
        pivot_levels: optional list of dicts with "label" and "price" keys — drawn as faint
            dashed horizontal lines labeled at the left edge. Default None = not drawn (no
            change to production output).
        swing_overlay: optional list of dicts with "bar_idx" (int), "price" (float), "type"
            ("high"|"low"), in time order. Drawn as small dots + thin connecting line so the
            geometric pattern (W bottoms, M tops, wedges) appears as a literal traced shape.
            Default None = not drawn (no change to production output).

    Returns:
        Absolute path to the saved PNG, e.g. "/Users/.../outputs/AUD_JPY_M15_chart.png"
    """
    from datetime import datetime as _dt, timezone as _tz
    import pandas as _pd

    # Parse time strings to aware datetime objects
    times = []
    for t in df["time"]:
        if isinstance(t, str):
            try:
                ts = t.replace("Z", "+00:00")
                if "." in ts:
                    base, frac = ts.split(".", 1)
                    offset = ""
                    for sep in ("+", "-"):
                        if sep in frac[1:]:
                            idx = frac.index(sep, 1)
                            offset = frac[idx:]
                            frac = frac[:idx]
                            break
                    if not offset:
                        offset = "+00:00"
                    frac = frac[:6].ljust(6, "0")
                    ts = f"{base}.{frac}{offset}"
                times.append(_dt.fromisoformat(ts))
            except Exception:
                times.append(_dt.now(_tz.utc))
        else:
            times.append(t)

    candle_data = {
        "time": times,
        "open":   list(df["open"]),
        "high":   list(df["high"]),
        "low":    list(df["low"]),
        "close":  list(df["close"]),
    }

    output_path = os.path.join(OUTPUT_DIR, f"{pair}_M15_chart.png")

    # --- Replicate plot_chart logic with snipe_level support ---
    t_all = candle_data["time"]
    o_all = candle_data["open"]
    h_all = candle_data["high"]
    l_all = candle_data["low"]
    c_all = candle_data["close"]

    c_arr = np.array(c_all, dtype=float)
    # 2026-04-27: 100 → 200 (see plot_chart comment above for rationale).
    display_start = max(0, len(c_arr) - 200)

    ema21  = calc_ema(c_arr, 21)
    ema55  = calc_ema(c_arr, 55)
    ema100 = calc_ema(c_arr, 100)
    bb_mid, bb_upper, bb_lower = calc_bollinger(c_arr, 20, 2)

    # RSI (14-period)
    _delta = np.diff(c_arr)
    _gain = np.where(_delta > 0, _delta, 0.0)
    _loss = np.where(_delta < 0, -_delta, 0.0)
    _rsi_arr = np.full(len(c_arr), 50.0)
    if len(_gain) >= 14:
        _avg_gain = np.convolve(_gain, np.ones(14)/14, mode='valid')
        _avg_loss = np.convolve(_loss, np.ones(14)/14, mode='valid')
        _avg_loss = np.where(_avg_loss == 0, 1e-10, _avg_loss)
        _rs = _avg_gain / _avg_loss
        _rsi_vals = 100.0 - (100.0 / (1.0 + _rs))
        _rsi_arr[14:14+len(_rsi_vals)] = _rsi_vals
        if len(_rsi_vals) < len(c_arr) - 14:
            _rsi_arr[14+len(_rsi_vals):] = _rsi_vals[-1]

    # MACD (12, 26, 9)
    _ema12 = calc_ema(c_arr, 12)
    _ema26 = calc_ema(c_arr, 26)
    _macd_line = _ema12 - _ema26
    _macd_signal = calc_ema(_macd_line, 9)
    _macd_hist = _macd_line - _macd_signal

    t       = t_all[display_start:]
    o_d     = np.array(o_all[display_start:], dtype=float)
    h_d     = np.array(h_all[display_start:], dtype=float)
    l_d     = np.array(l_all[display_start:], dtype=float)
    c_d     = c_arr[display_start:]
    ema21_d  = ema21[display_start:]
    ema55_d  = ema55[display_start:]
    ema100_d = ema100[display_start:]
    bb_mid_d   = bb_mid[display_start:]
    bb_upper_d = bb_upper[display_start:]
    bb_lower_d = bb_lower[display_start:]
    rsi_d      = _rsi_arr[display_start:]
    macd_line_d = _macd_line[display_start:]
    macd_sig_d  = _macd_signal[display_start:]
    macd_hist_d = _macd_hist[display_start:]

    t_num = mdates.date2num(t)

    bg_color    = "#1a1a2e"
    panel_color = "#16213e"
    grid_color  = "#2a2a4a"
    text_color  = "#e0e0e0"
    bull_color  = "#00e676"
    bear_color  = "#ff1744"
    ema21_color  = "#ffeb3b"
    ema55_color  = "#ff9800"
    ema100_color = "#f44336"
    bb_color    = "#7c4dff"
    rsi_color   = "#26c6da"
    macd_color  = "#ab47bc"
    macd_sig_color = "#ef6c00"

    fig, (ax, ax_rsi, ax_macd) = plt.subplots(
        3, 1, figsize=(16, 12), facecolor=bg_color,
        gridspec_kw={"height_ratios": [3, 1, 1]},
        sharex=True,
    )
    fig.subplots_adjust(hspace=0.05)
    ax.set_facecolor(panel_color)
    ax_rsi.set_facecolor(panel_color)
    ax_macd.set_facecolor(panel_color)

    # 2026-04-27: Weekend annotation. Forex closes Fri 5pm ET → Sun 5pm ET (~48h).
    # When the chart spans a weekend, that gap appears as a flat segment between
    # the last Friday candle and the first Sunday candle. Without an explicit
    # marker the agent has to guess what the flat region means; with the band
    # + label it can read "Friday close vs Sunday open" structurally.
    _gap_threshold_hours = 12  # M15 normal gap is 15 min; >12h is a session boundary
    _gaps_drawn = 0
    for _gi in range(1, len(t)):
        try:
            _delta = (t[_gi] - t[_gi - 1]).total_seconds() / 3600.0
        except Exception:
            continue
        if _delta < _gap_threshold_hours:
            continue
        _gx0, _gx1 = t_num[_gi - 1], t_num[_gi]
        for _gax in (ax, ax_rsi, ax_macd):
            _gax.axvspan(_gx0, _gx1, color="#475569", alpha=0.18, zorder=0)
        # Label only on the price panel
        try:
            _gap_hours = round(_delta)
            # transform=get_xaxis_transform() → x in data coords, y in axes coords
            # y=0.5 centers the label vertically in the price panel.
            ax.text((_gx0 + _gx1) / 2.0, 0.5,
                    f"WEEKEND\n{_gap_hours}h closed",
                    fontsize=9, color="#94a3b8", fontweight="bold",
                    ha="center", va="center",
                    transform=ax.get_xaxis_transform(),
                    bbox=dict(boxstyle="round,pad=0.3", facecolor=panel_color,
                              edgecolor="#475569", alpha=0.85))
            _gaps_drawn += 1
        except Exception:
            pass

    width = 0.004
    for i in range(len(t)):
        color = bull_color if c_d[i] >= o_d[i] else bear_color
        ax.plot([t_num[i], t_num[i]], [l_d[i], h_d[i]], color=color, linewidth=0.8)
        body_low  = min(o_d[i], c_d[i])
        body_high = max(o_d[i], c_d[i])
        body_height = body_high - body_low
        if body_height < 1e-10:
            body_height = (h_d[i] - l_d[i]) * 0.01 or 0.0001
        rect = plt.Rectangle((t_num[i] - width / 2, body_low), width, body_height,
                              facecolor=color, edgecolor=color, linewidth=0.5, alpha=0.9)
        ax.add_patch(rect)

    ax.plot(t_num, ema21_d,  color=ema21_color,  linewidth=1.5, label="EMA 21",  alpha=0.9)
    ax.plot(t_num, ema55_d,  color=ema55_color,  linewidth=1.5, label="EMA 55",  alpha=0.9)
    ax.plot(t_num, ema100_d, color=ema100_color, linewidth=1.5, label="EMA 100", alpha=0.9)

    valid = ~np.isnan(bb_upper_d)
    ax.plot(t_num[valid], bb_upper_d[valid], color=bb_color, linewidth=1.0, linestyle="--", alpha=0.7, label="BB Upper")
    ax.plot(t_num[valid], bb_lower_d[valid], color=bb_color, linewidth=1.0, linestyle="--", alpha=0.7, label="BB Lower")
    ax.plot(t_num[valid], bb_mid_d[valid],   color=bb_color, linewidth=0.7, linestyle=":",  alpha=0.5)
    ax.fill_between(t_num[valid], bb_lower_d[valid], bb_upper_d[valid], color=bb_color, alpha=0.08)

    current_price = c_d[-1]
    ax.axhline(y=current_price, color="#00bcd4", linewidth=1.0, linestyle="-.", alpha=0.8)
    ax.annotate(f"  {current_price:.5f}", xy=(t_num[-1], current_price),
                fontsize=10, color="#00bcd4", fontweight="bold",
                va="center", ha="left",
                bbox=dict(boxstyle="round,pad=0.2", facecolor=bg_color, edgecolor="#00bcd4", alpha=0.8))

    # Draw snipe levels as dashed magenta horizontal lines
    if snipe_levels:
        for sl in snipe_levels:
            price = sl.get("price")
            if price is not None:
                ax.axhline(y=float(price), color="#ff4081", linewidth=1.2, linestyle="--", alpha=0.85)
                ax.annotate(f"  SNIPE {float(price):.5f}", xy=(t_num[0], float(price)),
                            fontsize=8, color="#ff4081", fontweight="bold", va="bottom")

    # Daily pivot levels — faint dashed amber horizontal lines, labeled at left edge
    if pivot_levels:
        for pv in pivot_levels:
            price = pv.get("price")
            label = pv.get("label", "")
            if price is not None:
                ax.axhline(y=float(price), color="#ffb74d", linewidth=0.9,
                           linestyle="--", alpha=0.55)
                ax.annotate(f"  {label} {float(price):.5f}", xy=(t_num[0], float(price)),
                            fontsize=8, color="#ffb74d", fontweight="bold", va="center")

    # Pattern labels — text annotations at specific bars when a detector fired.
    # Each entry is a dict with bar_idx, name, color. The chart shows a small
    # text label at the bar's price location so the named pattern from
    # pattern_library.md is visible to the validator alongside the candles.
    #
    # 2026-05-13: bar_idx is FULL-FRAME (0..len(c_arr)-1). The display arrays
    # t_num/h_d/l_d are sliced to the last 200 bars (display_start onwards),
    # so we must translate: display_idx = bar_idx - display_start. Prior to
    # this fix, labels at full-frame bar_idx ≥ 200 (i.e., the last 50 bars
    # of the 250-bar fetch — the bars MOST relevant for entry timing) were
    # silently dropped by the old `bi < len(t_num)` check. Recent pattern
    # fires + Exit markers landing near the entry edge are now visible.
    if pattern_labels:
        for pl in pattern_labels:
            bi_raw = pl.get("bar_idx")
            name = pl.get("name", "")
            pcolor = pl.get("color", "#94a3b8")
            if bi_raw is None:
                continue
            bi = bi_raw - display_start
            if not (0 <= bi < len(t_num)):
                continue
            # Place label above the candle's high (for bullish-color labels) or
            # below the low (for bearish-color labels) so labels don't collide.
            y_anchor = h_d[bi] if pcolor != "#ef5350" else l_d[bi]
            va = "bottom" if pcolor != "#ef5350" else "top"
            offset = 0.0008 * (1 if va == "bottom" else -1)
            ax.annotate(name, xy=(t_num[bi], y_anchor + offset),
                        fontsize=7, color=pcolor, fontweight="bold",
                        ha="center", va=va,
                        bbox=dict(boxstyle="round,pad=0.18", facecolor=bg_color,
                                  edgecolor=pcolor, alpha=0.85, linewidth=0.5))
            # Small marker at the bar
            ax.scatter([t_num[bi]], [y_anchor],
                       c=pcolor, s=18, marker="v" if va == "top" else "^",
                       alpha=0.9, zorder=4, edgecolors="#0d1117", linewidths=0.5)

    # ── EMA-signal markers from the shared format_chart_signals() ────────
    # 2026-05-13: Same function the dashboard backend (trading_api_routes.py)
    # calls to populate the dashboard chart Tim sees. Calling it here makes
    # the validator's chart and Tim's chart show the same markers — one
    # source of truth for: ⚠ Exit↓/↑ (peak_sep), ⚡ Close↑/↓ (decel),
    # ◼ CL/CS (return_exit), 🛡 E100 (ema100_test), ▶ entries, EMA Cross.
    #
    # Renders only signals whose timestamp falls inside the display window
    # (last 200 bars). Errors are non-fatal — chart still renders without
    # these markers if the signal compute fails.
    try:
        from backtester.ema_separation import format_chart_signals as _fcs

        def _canon_time(t):
            if isinstance(t, str):
                return t
            return t.isoformat() if hasattr(t, "isoformat") else str(t)

        _candles_flat = []
        _time_to_idx = {}
        for _i, _rawt in enumerate(df["time"]):
            _candles_flat.append({
                "time": _rawt,
                "open":  float(o_all[_i]),
                "high":  float(h_all[_i]),
                "low":   float(l_all[_i]),
                "close": float(c_all[_i]),
            })
            _time_to_idx[_canon_time(_rawt)] = _i

        _ema_signals = _fcs(_candles_flat) or []

        # 2026-05-14: Filter to ONLY the most recent peak_sep (⚠ Exit↓/↑) marker.
        # Earlier integration rendered every Exit/Close/CL/CS/E100/entry/EMA-Cross
        # signal across the whole 250-bar window — the validator read this wall of
        # labels as cautionary signals on every chart, flat-lining confidence to
        # ~5/10 even on clean continuation winners. Tim's directive: show only the
        # most current Exit marker, drop the rest, keep the live-version core
        # indicators (EMAs, BBs, RSI, MACD, candles, swing-trace, pattern_labels).
        _peak_seps = [s for s in _ema_signals if s.get("type") == "peak_sep"]
        if _peak_seps:
            def _sig_bar_idx(s):
                bi = _time_to_idx.get(_canon_time(s.get("time")))
                return bi if bi is not None else -1
            _peak_seps.sort(key=_sig_bar_idx)
            _ema_signals = [_peak_seps[-1]]
        else:
            _ema_signals = []

        # type → direction → (label_text, color, position)
        _SIG_STYLE = {
            "peak_sep":    {"sell": ("⚠ Exit↓",  "#d29922", "above"),
                            "buy":  ("⚠ Exit↑",  "#d29922", "below")},
            "decel":       {"sell": ("⚡ Close↓", "#ff6b00", "above"),
                            "buy":  ("⚡ Close↑", "#ff6b00", "below")},
            "return_exit": {"sell": ("◼ CL",     "#58a6ff", "above"),
                            "buy":  ("◼ CS",     "#58a6ff", "below")},
            "ema100_test": {"sell": ("🛡 E100",  "#26c6da", "above"),
                            "buy":  ("🛡 E100",  "#26c6da", "below")},
            "entry":       {"sell": (None,       "#ef5350", "above"),
                            "buy":  (None,       "#66bb6a", "below")},
            "crossover":   {"bullish": ("EMA Cross↑", "#a855f7", "below"),
                            "bearish": ("EMA Cross↓", "#a855f7", "above")},
        }

        for _sig in _ema_signals:
            _stype = _sig.get("type")
            _sdir = _sig.get("direction")
            _sty = _SIG_STYLE.get(_stype, {}).get(_sdir)
            if not _sty:
                continue
            _label_default, _scolor, _pos = _sty
            # Entry markers use the format_chart_signals-built label
            # ("▶ A engulf↑" etc.); other types use our short label.
            _label = _sig.get("label", _label_default) if _stype == "entry" else _label_default
            if not _label:
                continue
            _bar_full = _time_to_idx.get(_canon_time(_sig.get("time")))
            if _bar_full is None:
                continue
            _bar_disp = _bar_full - display_start
            if not (0 <= _bar_disp < len(t_num)):
                continue
            if _pos == "above":
                _y = h_d[_bar_disp]
                _va = "bottom"
                _yoff = 0.0008
            else:
                _y = l_d[_bar_disp]
                _va = "top"
                _yoff = -0.0008
            ax.annotate(_label, xy=(t_num[_bar_disp], _y + _yoff),
                        fontsize=7, color=_scolor, fontweight="bold",
                        ha="center", va=_va,
                        bbox=dict(boxstyle="round,pad=0.18",
                                  facecolor=bg_color,
                                  edgecolor=_scolor, alpha=0.85,
                                  linewidth=0.5))
            ax.scatter([t_num[_bar_disp]], [_y],
                       c=_scolor, s=18,
                       marker="v" if _va == "bottom" else "^",
                       alpha=0.9, zorder=4,
                       edgecolors="#0d1117", linewidths=0.5)
    except Exception as _fcs_err:
        logger.debug("format_chart_signals integration skipped: %s", _fcs_err)

    # Swing-trace overlay — small dots at swing highs/lows + thin connecting line
    # so geometric patterns (W, M, wedges) appear as a literal traced shape.
    # No labels — pure geometric annotation.
    # 2026-05-13: bar_idx is full-frame (same convention as pattern_labels).
    # Translate to display frame; previously points in last 50 bars were dropped.
    if swing_overlay:
        sw_pts = []
        for pt in swing_overlay:
            bi_raw = pt.get("bar_idx")
            if bi_raw is None:
                continue
            bi = bi_raw - display_start
            if not (0 <= bi < len(t_num)):
                continue
            sw_pts.append({**pt, "bar_idx": bi})
        sw_pts.sort(key=lambda pt: pt["bar_idx"])
        if sw_pts:
            xs = [t_num[pt["bar_idx"]] for pt in sw_pts]
            ys = [float(pt["price"]) for pt in sw_pts]
            colors = ["#ef5350" if pt.get("type") == "high" else "#66bb6a" for pt in sw_pts]
            # Connecting line in muted grey — traces W/M/wedge geometry
            ax.plot(xs, ys, color="#cbd5e1", linewidth=0.9, alpha=0.55, zorder=2)
            # Dots at each swing point (red for highs, green for lows)
            ax.scatter(xs, ys, c=colors, s=22, alpha=0.85, zorder=3,
                       edgecolors="#0d1117", linewidths=0.6)

    plt.setp(ax.get_yticklabels(), fontsize=9, color=text_color)
    ax.grid(True, alpha=0.15, color=grid_color, linewidth=0.5)
    ax.tick_params(colors=text_color, which="both")
    for spine in ax.spines.values():
        spine.set_color(grid_color)

    pair_display = pair.replace("_", "/")
    last_time = t[-1].strftime("%Y-%m-%d %H:%M UTC") if hasattr(t[-1], "strftime") else str(t[-1])

    ema_order = (
        "BEARISH FAN" if ema21_d[-1] < ema55_d[-1] < ema100_d[-1]
        else "BULLISH FAN" if ema21_d[-1] > ema55_d[-1] > ema100_d[-1]
        else "MIXED"
    )
    spread_now  = abs(ema21_d[-1] - ema55_d[-1])
    spread_prev = abs(ema21_d[-10] - ema55_d[-10]) if len(ema21_d) > 10 else spread_now
    ema_sep = "SEPARATING" if spread_now > spread_prev else "COMPRESSING"

    bb_widths = bb_upper_d[valid] - bb_lower_d[valid]
    bb_trend = ("EXPANDING" if bb_widths[-1] > bb_widths[-10] else "CONTRACTING") if len(bb_widths) >= 10 else "N/A"

    snipe_note = f"  |  {len(snipe_levels)} snipe level(s)" if snipe_levels else ""
    title = (
        f"{pair_display}  M15  |  {last_time}{snipe_note}\n"
        f"EMAs: {ema_order} ({ema_sep})  |  BBs: {bb_trend}  |  Price: {current_price:.5f}"
    )
    ax.set_title(title, fontsize=13, fontweight="bold", color=text_color, pad=15)
    ax.legend(loc="upper left", fontsize=8, facecolor=panel_color, edgecolor=grid_color,
              labelcolor=text_color, framealpha=0.9)

    # ── RSI subplot ──────────────────────────────────────────
    ax_rsi.plot(t_num, rsi_d, color=rsi_color, linewidth=1.2, label="RSI(14)")
    ax_rsi.axhline(y=70, color="#ef5350", linewidth=0.7, linestyle="--", alpha=0.6)
    ax_rsi.axhline(y=30, color="#66bb6a", linewidth=0.7, linestyle="--", alpha=0.6)
    ax_rsi.axhline(y=50, color=grid_color, linewidth=0.5, linestyle=":", alpha=0.4)
    ax_rsi.fill_between(t_num, 70, 100, color="#ef5350", alpha=0.05)
    ax_rsi.fill_between(t_num, 0, 30, color="#66bb6a", alpha=0.05)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI", fontsize=9, color=text_color)
    ax_rsi.grid(True, alpha=0.15, color=grid_color, linewidth=0.5)
    ax_rsi.tick_params(colors=text_color, which="both")
    plt.setp(ax_rsi.get_yticklabels(), fontsize=8, color=text_color)
    for spine in ax_rsi.spines.values():
        spine.set_color(grid_color)
    ax_rsi.legend(loc="upper left", fontsize=7, facecolor=panel_color, edgecolor=grid_color,
                  labelcolor=text_color, framealpha=0.8)

    # ── MACD subplot ─────────────────────────────────────────
    ax_macd.plot(t_num, macd_line_d, color=macd_color, linewidth=1.0, label="MACD")
    ax_macd.plot(t_num, macd_sig_d, color=macd_sig_color, linewidth=1.0, label="Signal")
    _macd_pos = np.where(macd_hist_d >= 0, macd_hist_d, 0)
    _macd_neg = np.where(macd_hist_d < 0, macd_hist_d, 0)
    ax_macd.bar(t_num, _macd_pos, width=width, color="#66bb6a", alpha=0.6)
    ax_macd.bar(t_num, _macd_neg, width=width, color="#ef5350", alpha=0.6)
    ax_macd.axhline(y=0, color=grid_color, linewidth=0.5, alpha=0.5)
    ax_macd.set_ylabel("MACD", fontsize=9, color=text_color)
    ax_macd.grid(True, alpha=0.15, color=grid_color, linewidth=0.5)
    ax_macd.tick_params(colors=text_color, which="both")
    plt.setp(ax_macd.get_yticklabels(), fontsize=8, color=text_color)
    for spine in ax_macd.spines.values():
        spine.set_color(grid_color)
    ax_macd.legend(loc="upper left", fontsize=7, facecolor=panel_color, edgecolor=grid_color,
                   labelcolor=text_color, framealpha=0.8)

    # Move x-axis labels to MACD (bottom) panel
    ax.tick_params(labelbottom=False)
    ax_rsi.tick_params(labelbottom=False)
    ax_macd.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M", tz=_tz.utc))
    ax_macd.xaxis.set_major_locator(mdates.HourLocator(interval=4))
    plt.setp(ax_macd.get_xticklabels(), rotation=45, ha="right", fontsize=8, color=text_color)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, facecolor=bg_color, edgecolor="none", bbox_inches="tight")
    plt.close(fig)

    return output_path


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("Connecting to OANDA...")
    with OandaClient() as client:
        # Verify connection
        summary = client.get_account_summary()
        print(f"Connected: Account {summary.get('account', {}).get('id', 'unknown')}")
        
        results = {}
        for pair in PAIRS:
            print(f"\nFetching {pair} M15 candles...")
            candle_data = fetch_candles(client, pair, GRANULARITY, COUNT)
            print(f"  Got {len(candle_data['time'])} candles")
            
            output_path = os.path.join(OUTPUT_DIR, f"{pair}_M15_chart.png")
            results[pair] = plot_chart(candle_data, pair, output_path)
        
        print("\n\nALL CHARTS GENERATED SUCCESSFULLY")
        for pair, r in results.items():
            print(f"  {pair}: {r['ema_fan']} | {r['ema_trend']} | BB {r['bb_trend']}")


if __name__ == "__main__":
    main()
