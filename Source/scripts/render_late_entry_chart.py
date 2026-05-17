"""render_late_entry_chart.py — Clean chart renderer for teaching the validator
what late entry / exhaustion looks like. Three layers only:

  1. Candles (standard bull/bear)
  2. EMAs (E21 / E55 / E100) with CURVE ARCS highlighting where the line
     physically forms a U (upward bend) or n (downward bend).
  3. BB upper/lower lines with SQUEEZE markers — thin yellow horizontal bars
     above and below where the bands physically constrict (width < 0.7× recent mean)
     for 3+ consecutive bars.

The validator's eye reads the picture: EMA curve = bend happening, BB squeeze =
momentum about to die, candle behavior visible plainly. No "STRETCHED" text,
no per-bar colored segments, no fill_between gradients. Just clean indicators
with the right things emphasized.
"""

from __future__ import annotations

import os
from datetime import datetime as _dt
from typing import Iterable

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# ── INDICATORS ───────────────────────────────────────────────────────

def _ema(x: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.full(len(x), np.nan)
    if len(x) == 0:
        return out
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def _bollinger(c: np.ndarray, period: int = 20, std_mult: float = 2.0):
    n = len(c)
    mid = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for i in range(period - 1, n):
        w = c[i - period + 1:i + 1]
        m = float(w.mean())
        s = float(w.std(ddof=0))
        mid[i] = m
        upper[i] = m + std_mult * s
        lower[i] = m - std_mult * s
    return mid, upper, lower


# ── EMA CURVE DETECTION ──────────────────────────────────────────────

def detect_ema_curve_zones(ema: np.ndarray, look_left: int = 4, look_right: int = 4,
                           min_prominence_pips: float = 1.0,
                           pair_pip: float = 0.0001) -> list[dict]:
    """Find local maxima (peak n-bend) and local minima (U-bend) where BOTH
    sides of the curve have completed. Used for HISTORICAL curves in the
    chart background — shows the validator what completed curves look like.

    For curve-IN-FORMATION at the right edge (entry time), see
    detect_curve_onset() — that's the late-entry signal.
    """
    n = len(ema)
    zones = []
    for i in range(look_left, n - look_right):
        if np.isnan(ema[i]):
            continue
        window = ema[i - look_left : i + look_right + 1]
        if np.any(np.isnan(window)):
            continue
        if ema[i] == window.max() and ema[i] > ema[i - look_left] and ema[i] > ema[i + look_right]:
            prom = min(ema[i] - ema[i - look_left], ema[i] - ema[i + look_right]) / pair_pip
            if prom >= min_prominence_pips:
                zones.append({"bar_idx": i, "type": "peak", "ema_value": float(ema[i]),
                              "start_idx": i - look_left, "end_idx": i + look_right,
                              "prominence_pips": round(prom, 2)})
        elif ema[i] == window.min() and ema[i] < ema[i - look_left] and ema[i] < ema[i + look_right]:
            prom = min(ema[i - look_left] - ema[i], ema[i + look_right] - ema[i]) / pair_pip
            if prom >= min_prominence_pips:
                zones.append({"bar_idx": i, "type": "trough", "ema_value": float(ema[i]),
                              "start_idx": i - look_left, "end_idx": i + look_right,
                              "prominence_pips": round(prom, 2)})
    return zones


def detect_curve_onset(ema: np.ndarray, pair_pip: float,
                       strong_lookback: int = 8, decel_lookback: int = 3,
                       strong_pips_per_bar: float = 0.3,
                       decel_ratio: float = 0.5) -> list[dict]:
    """Find bars where the EMA's slope HAS BEEN strong for the past
    `strong_lookback` bars, but in the most recent `decel_lookback` bars
    has slowed to less than `decel_ratio` of the prior strong pace.

    This is "curve in formation" — the EMA is starting to roll over but
    hasn't completed the curve yet. The trade entered here loses because
    the move is exhausting.

    Returns list of dicts with:
        {"bar_idx": int, "type": "peak_forming"|"trough_forming",
         "prior_slope": float, "current_slope": float}
    """
    n = len(ema)
    zones = []
    for i in range(strong_lookback + decel_lookback, n):
        if np.isnan(ema[i]) or np.isnan(ema[i - strong_lookback]):
            continue
        # Prior strong segment: bars (i-strong-decel) to (i-decel)
        prior_start = i - strong_lookback - decel_lookback
        prior_end = i - decel_lookback
        if prior_start < 0 or np.isnan(ema[prior_start]):
            continue
        prior_slope = (ema[prior_end] - ema[prior_start]) / strong_lookback / pair_pip
        # Recent decel segment: last decel_lookback bars
        cur_slope = (ema[i] - ema[i - decel_lookback]) / decel_lookback / pair_pip
        prior_strong_up = prior_slope > strong_pips_per_bar
        prior_strong_down = prior_slope < -strong_pips_per_bar

        if prior_strong_up:
            # Decelerating: cur_slope < decel_ratio * prior_slope
            if cur_slope < prior_slope * decel_ratio:
                zones.append({"bar_idx": i, "type": "peak_forming",
                              "prior_slope": round(prior_slope, 3),
                              "current_slope": round(cur_slope, 3)})
        elif prior_strong_down:
            # For downward trend: cur_slope > decel_ratio * prior_slope
            # (where both are negative, decel_ratio < 1 means cur is less negative)
            if cur_slope > prior_slope * decel_ratio:
                zones.append({"bar_idx": i, "type": "trough_forming",
                              "prior_slope": round(prior_slope, 3),
                              "current_slope": round(cur_slope, 3)})
    return zones


# ── BB SQUEEZE DETECTION ─────────────────────────────────────────────

def detect_bb_squeeze_zones(bb_upper: np.ndarray, bb_lower: np.ndarray,
                            min_run: int = 3,
                            squeeze_ratio: float = 0.70,
                            recent_window: int = 20) -> list[dict]:
    """Historical completed squeeze zones (full squeeze episodes). For
    in-formation squeeze at the right edge use detect_squeeze_forming()."""
    n = len(bb_upper)
    width = bb_upper - bb_lower
    zones = []
    in_run = False
    run_start = None
    for i in range(recent_window, n):
        if np.isnan(width[i]):
            continue
        recent_mean = float(np.nanmean(width[max(0, i - recent_window):i]))
        is_squeezed = width[i] < squeeze_ratio * recent_mean
        if is_squeezed and not in_run:
            in_run = True
            run_start = i
        elif (not is_squeezed) and in_run:
            run_end = i
            if (run_end - run_start) >= min_run:
                u_mean = float(np.nanmean(bb_upper[run_start:run_end]))
                l_mean = float(np.nanmean(bb_lower[run_start:run_end]))
                zones.append({"start_idx": run_start, "end_idx": run_end - 1,
                              "upper_mean": u_mean, "lower_mean": l_mean,
                              "length_bars": run_end - run_start})
            in_run = False
            run_start = None
    if in_run and run_start is not None:
        run_end = n
        if (run_end - run_start) >= min_run:
            u_mean = float(np.nanmean(bb_upper[run_start:run_end]))
            l_mean = float(np.nanmean(bb_lower[run_start:run_end]))
            zones.append({"start_idx": run_start, "end_idx": run_end - 1,
                          "upper_mean": u_mean, "lower_mean": l_mean,
                          "length_bars": run_end - run_start})
    return zones


def detect_squeeze_forming(bb_upper: np.ndarray, bb_lower: np.ndarray,
                           min_shrink_bars: int = 2,
                           min_shrink_pct: float = 0.05) -> list[int]:
    """Bars where BB width has been STRICTLY decreasing for the last
    `min_shrink_bars` bars AND total shrink ≥ `min_shrink_pct`.

    This catches squeezes IN FORMATION (bands converging) — earlier than
    detect_bb_squeeze_zones which requires width below recent-mean threshold.
    """
    n = len(bb_upper)
    width = bb_upper - bb_lower
    flags = []
    for i in range(min_shrink_bars + 1, n):
        if any(np.isnan(width[i - k]) for k in range(min_shrink_bars + 1)):
            continue
        shrinking = all(width[i - k] < width[i - k - 1] for k in range(min_shrink_bars))
        if shrinking:
            total_shrink = (width[i - min_shrink_bars] - width[i]) / max(width[i - min_shrink_bars], 1e-9)
            if total_shrink >= min_shrink_pct:
                flags.append(i)
    return flags


# ── RENDERER ─────────────────────────────────────────────────────────

def render_chart(pair: str, df: pd.DataFrame, out_path: str,
                 pair_pip: float = 0.0001, title_extra: str = ""):
    if len(df) < 60:
        raise ValueError(f"Not enough candles ({len(df)})")

    # Parse times
    times = []
    for t in df["time"]:
        if isinstance(t, str):
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
        else:
            times.append(t)

    t_num = mdates.date2num(times)
    o = np.array(df["open"], dtype=float)
    h = np.array(df["high"], dtype=float)
    l = np.array(df["low"], dtype=float)
    c = np.array(df["close"], dtype=float)

    ema21 = _ema(c, 21)
    ema55 = _ema(c, 55)
    ema100 = _ema(c, 100)
    bb_mid, bb_upper, bb_lower = _bollinger(c, 20, 2.0)

    # Historical completed curves — only MAJOR ones (high prominence). These
    # provide background context; over-firing creates noise.
    curve_min_prom_pips = 4.0 if pair_pip == 0.01 else 5.0  # JPY ~4p, others ~5p
    e21_curves = detect_ema_curve_zones(ema21, look_left=5, look_right=5,
                                        min_prominence_pips=curve_min_prom_pips,
                                        pair_pip=pair_pip)
    e55_curves = detect_ema_curve_zones(ema55, look_left=6, look_right=6,
                                        min_prominence_pips=curve_min_prom_pips * 0.7,
                                        pair_pip=pair_pip)
    bb_squeezes = detect_bb_squeeze_zones(bb_upper, bb_lower, min_run=4,
                                          squeeze_ratio=0.60)

    # CURVES IN FORMATION at entry edge — the actual late-entry signal.
    # These are bars where EMA slope has decelerated from strong to flat,
    # i.e. the curve is JUST STARTING.
    e21_onset = detect_curve_onset(ema21, pair_pip)
    e55_onset = detect_curve_onset(ema55, pair_pip,
                                   strong_pips_per_bar=0.2)
    bb_forming = detect_squeeze_forming(bb_upper, bb_lower)

    # Display window — last 200 bars (matches live chart)
    display_start = max(0, len(c) - 200)

    def trim(arr):
        return arr[display_start:]

    t_d = trim(t_num)
    o_d, h_d, l_d, c_d = trim(o), trim(h), trim(l), trim(c)
    e21_d = trim(ema21)
    e55_d = trim(ema55)
    e100_d = trim(ema100)
    bbu_d = trim(bb_upper)
    bbl_d = trim(bb_lower)
    bbm_d = trim(bb_mid)

    # Filter zones to those falling in display window
    def shift_zone(z):
        return {**z,
                "start_idx": z["start_idx"] - display_start,
                "end_idx": z["end_idx"] - display_start,
                "bar_idx": z.get("bar_idx", z["start_idx"]) - display_start}

    e21_curves_d = [shift_zone(z) for z in e21_curves
                    if z["start_idx"] >= display_start]
    e55_curves_d = [shift_zone(z) for z in e55_curves
                    if z["start_idx"] >= display_start]
    bb_squeezes_d = [shift_zone(z) for z in bb_squeezes
                     if z["end_idx"] >= display_start]
    e21_onset_d = [{"bar_idx": z["bar_idx"] - display_start, **z}
                   for z in e21_onset if z["bar_idx"] >= display_start]
    e55_onset_d = [{"bar_idx": z["bar_idx"] - display_start, **z}
                   for z in e55_onset if z["bar_idx"] >= display_start]
    bb_forming_d = [i - display_start for i in bb_forming if i >= display_start]

    # ── Plot ─────────────────────────────────────────────────────────
    bg = "#0d1117"
    fg = "#c9d1d9"
    grid = "#21262d"
    bull = "#26a69a"
    bear = "#ef5350"
    e21_color = "#58a6ff"   # light blue
    e55_color = "#d29922"   # amber
    e100_color = "#a371f7"  # purple
    bb_color = "#7d8590"    # neutral grey
    squeeze_color = "#ffd54f"  # yellow (matches Tim's hand-drawn marks)

    fig, ax = plt.subplots(figsize=(14, 7), facecolor=bg)
    ax.set_facecolor(bg)

    if len(t_d) > 1:
        width = (t_d[-1] - t_d[0]) / max(len(t_d) - 1, 1) * 0.7
    else:
        width = 0.01

    # Candles
    for i in range(len(t_d)):
        col = bull if c_d[i] >= o_d[i] else bear
        ax.plot([t_d[i], t_d[i]], [l_d[i], h_d[i]], color=col, linewidth=0.8)
        body_low = min(o_d[i], c_d[i])
        body_high = max(o_d[i], c_d[i])
        body_height = body_high - body_low
        if body_height < 1e-10:
            body_height = (h_d[i] - l_d[i]) * 0.01 or 0.0001
        rect = plt.Rectangle((t_d[i] - width / 2, body_low), width, body_height,
                             facecolor=col, edgecolor=col, linewidth=0.5, alpha=0.9)
        ax.add_patch(rect)

    # EMAs (plain lines)
    ax.plot(t_d, e21_d, color=e21_color, linewidth=1.5, alpha=0.95, label="EMA 21")
    ax.plot(t_d, e55_d, color=e55_color, linewidth=1.5, alpha=0.90, label="EMA 55")
    ax.plot(t_d, e100_d, color=e100_color, linewidth=1.5, alpha=0.85, label="EMA 100")

    # BB lines — base layer (thin dashed grey). Squeeze zones will overlay
    # bold yellow on the actual BB lines themselves where constriction is active.
    valid = ~np.isnan(bbu_d)
    ax.plot(t_d[valid], bbu_d[valid], color=bb_color, linewidth=0.9,
            linestyle="--", alpha=0.55)
    ax.plot(t_d[valid], bbl_d[valid], color=bb_color, linewidth=0.9,
            linestyle="--", alpha=0.55)

    # ── BB SQUEEZE: the BB lines THEMSELVES bold up + glow yellow during
    # the constriction window. No separate horizontal bars.
    for z in bb_squeezes_d:
        s = max(0, z["start_idx"])
        e = min(len(t_d) - 1, z["end_idx"])
        if e < s:
            continue
        # Use actual BB values across the squeeze window
        xs = t_d[s:e + 1]
        ys_u = bbu_d[s:e + 1]
        ys_l = bbl_d[s:e + 1]
        valid_seg = ~(np.isnan(ys_u) | np.isnan(ys_l))
        if not valid_seg.any():
            continue
        # Glow: thicker semi-transparent yellow line UNDER
        ax.plot(xs[valid_seg], ys_u[valid_seg], color=squeeze_color,
                linewidth=6.5, alpha=0.35, zorder=4, solid_capstyle="round")
        ax.plot(xs[valid_seg], ys_l[valid_seg], color=squeeze_color,
                linewidth=6.5, alpha=0.35, zorder=4, solid_capstyle="round")
        # Bold: solid yellow line on top
        ax.plot(xs[valid_seg], ys_u[valid_seg], color=squeeze_color,
                linewidth=2.8, alpha=0.95, zorder=5, solid_capstyle="round")
        ax.plot(xs[valid_seg], ys_l[valid_seg], color=squeeze_color,
                linewidth=2.8, alpha=0.95, zorder=5, solid_capstyle="round")

    # ── HISTORICAL COMPLETED CURVES — the EMA21 line itself glows red/green
    # in the bend zone. Glow = wide translucent layer underneath, sharp line on top.
    for z in e21_curves_d:
        s = max(0, z["start_idx"])
        e = min(len(t_d) - 1, z["end_idx"])
        if e <= s:
            continue
        xs = t_d[s:e + 1]
        ys = e21_d[s:e + 1]
        col = "#ef5350" if z["type"] == "peak" else "#3fb950"
        # Glow layer
        ax.plot(xs, ys, color=col, linewidth=9.0, alpha=0.30, zorder=5,
                solid_capstyle="round")
        # Sharp layer on top
        ax.plot(xs, ys, color=col, linewidth=3.0, alpha=0.95, zorder=6,
                solid_capstyle="round")

    for z in e55_curves_d:
        s = max(0, z["start_idx"])
        e = min(len(t_d) - 1, z["end_idx"])
        if e <= s:
            continue
        xs = t_d[s:e + 1]
        ys = e55_d[s:e + 1]
        col = "#ef5350" if z["type"] == "peak" else "#3fb950"
        ax.plot(xs, ys, color=col, linewidth=6.0, alpha=0.22, zorder=3,
                solid_capstyle="round")
        ax.plot(xs, ys, color=col, linewidth=2.0, alpha=0.80, zorder=4,
                solid_capstyle="round")

    # ── ENTRY-EDGE ONSET (only the MOST RECENT onset, if it landed in last
    # 5 bars). One short colored EMA segment at the deceleration tail —
    # nothing else. Captures "curve just starting" without flooding the chart.
    edge_window = 5
    last_edge_idx = len(t_d) - 1
    most_recent_e21_onset = None
    for z in e21_onset_d:
        bi = z["bar_idx"]
        if bi >= last_edge_idx - edge_window and bi <= last_edge_idx:
            already_covered = any(c["start_idx"] <= bi <= c["end_idx"]
                                  for c in e21_curves_d)
            if not already_covered:
                most_recent_e21_onset = z  # keep last one (most recent bar)
    if most_recent_e21_onset is not None:
        bi = most_recent_e21_onset["bar_idx"]
        decel_lookback = 3
        s = max(0, bi - decel_lookback)
        xs = t_d[s:bi + 1]
        ys = e21_d[s:bi + 1]
        col = "#ef5350" if most_recent_e21_onset["type"] == "peak_forming" else "#3fb950"
        # Glow + sharp on EMA itself at the forming segment
        ax.plot(xs, ys, color=col, linewidth=8.0, alpha=0.30, zorder=6,
                solid_capstyle="round")
        ax.plot(xs, ys, color=col, linewidth=2.8, alpha=0.90, zorder=7,
                solid_capstyle="round")

    # BB SQUEEZE FORMING at right edge — only flag if forming bars exist in
    # last edge_window AND no completed squeeze already covers them. Just
    # color the BB lines yellow at those bars (subtle).
    edge_forming = [bi for bi in bb_forming_d
                    if last_edge_idx - edge_window <= bi <= last_edge_idx and
                    not any(z["start_idx"] <= bi <= z["end_idx"]
                            for z in bb_squeezes_d)]
    if edge_forming:
        runs = []
        run = [edge_forming[0]]
        for k in edge_forming[1:]:
            if k == run[-1] + 1:
                run.append(k)
            else:
                runs.append(run)
                run = [k]
        runs.append(run)
        for run in runs:
            if len(run) < 1:
                continue
            s, e = run[0], run[-1]
            xs = t_d[s:e + 1]
            ys_u = bbu_d[s:e + 1]
            ys_l = bbl_d[s:e + 1]
            valid_run = ~(np.isnan(ys_u) | np.isnan(ys_l))
            if not valid_run.any():
                continue
            # Glow + bold on the BB lines themselves
            ax.plot(xs[valid_run], ys_u[valid_run], color=squeeze_color,
                    linewidth=5.5, alpha=0.30, zorder=4, solid_capstyle="round")
            ax.plot(xs[valid_run], ys_l[valid_run], color=squeeze_color,
                    linewidth=5.5, alpha=0.30, zorder=4, solid_capstyle="round")
            ax.plot(xs[valid_run], ys_u[valid_run], color=squeeze_color,
                    linewidth=2.4, alpha=0.95, zorder=5, solid_capstyle="round")
            ax.plot(xs[valid_run], ys_l[valid_run], color=squeeze_color,
                    linewidth=2.4, alpha=0.95, zorder=5, solid_capstyle="round")

    # Current price line
    current = c_d[-1]
    ax.axhline(y=current, color="#00bcd4", linewidth=0.9,
               linestyle="-.", alpha=0.7)
    ax.annotate(f"  {current:.5f}", xy=(t_d[-1], current), fontsize=9,
                color="#00bcd4", fontweight="bold", va="center")

    # Title
    pair_disp = pair.replace("_", "/")
    ax.set_title(f"{pair_disp}  M15  {title_extra}",
                 color=fg, fontsize=12, pad=10)

    plt.setp(ax.get_yticklabels(), fontsize=9, color=fg)
    plt.setp(ax.get_xticklabels(), fontsize=8, color=fg)
    ax.grid(True, alpha=0.12, color=grid, linewidth=0.5)
    ax.tick_params(colors=fg)
    for spine in ax.spines.values():
        spine.set_color(grid)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d %H:%M"))

    # Minimal legend
    legend_text = ("EMA curve: RED arc = downward bend (peak) | "
                   "GREEN arc = upward bend (trough)  ·  "
                   "Yellow BB bars = squeeze (constriction)")
    ax.text(0.01, 0.985, legend_text, transform=ax.transAxes,
            fontsize=8, color=fg, va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.3", facecolor=bg,
                      edgecolor=grid, alpha=0.85))

    plt.tight_layout()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    plt.savefig(out_path, dpi=110, facecolor=bg, bbox_inches="tight")
    plt.close(fig)
    return out_path


def summary_last_n_bars(df: pd.DataFrame, pair_pip: float, n: int = 5) -> dict:
    """Summarize the structural state at the right edge of the chart (entry).

    Detects:
      - E21 curve IN FORMATION (peak_forming = decel from strong rising,
        trough_forming = decel from strong falling) anywhere in last N bars
      - BB squeeze FORMING (width strictly shrinking 2+ bars) in last N bars
      - Historical completed curves / squeezes ended within last N bars
    """
    c = np.array(df["close"], dtype=float)
    ema21 = _ema(c, 21)
    ema55 = _ema(c, 55)
    _, bb_upper, bb_lower = _bollinger(c, 20, 2.0)
    last_idx = len(c) - 1
    curve_min_prom_pips = 1.5 if pair_pip == 0.01 else 2.0

    e21_curves = detect_ema_curve_zones(ema21, min_prominence_pips=curve_min_prom_pips,
                                        pair_pip=pair_pip)
    bb_squeezes = detect_bb_squeeze_zones(bb_upper, bb_lower)
    e21_onset = detect_curve_onset(ema21, pair_pip)
    bb_forming = detect_squeeze_forming(bb_upper, bb_lower)

    recent_peak_completed = any(z["type"] == "peak" and z["end_idx"] >= last_idx - n
                                for z in e21_curves)
    recent_trough_completed = any(z["type"] == "trough" and z["end_idx"] >= last_idx - n
                                  for z in e21_curves)
    recent_squeeze_completed = any(z["end_idx"] >= last_idx - n for z in bb_squeezes)
    onset_peak = any(z["type"] == "peak_forming" and z["bar_idx"] >= last_idx - n
                     for z in e21_onset)
    onset_trough = any(z["type"] == "trough_forming" and z["bar_idx"] >= last_idx - n
                       for z in e21_onset)
    squeeze_forming = any(i >= last_idx - n for i in bb_forming)

    any_late = (recent_peak_completed or recent_trough_completed
                or recent_squeeze_completed or onset_peak or onset_trough
                or squeeze_forming)

    return {
        "onset_peak_forming": onset_peak,
        "onset_trough_forming": onset_trough,
        "squeeze_forming": squeeze_forming,
        "completed_peak_recent": recent_peak_completed,
        "completed_trough_recent": recent_trough_completed,
        "completed_squeeze_recent": recent_squeeze_completed,
        "any_late_signal": any_late,
    }
