"""backtest_marker_appears_during_trade.py — Event-driven dual-mode rule.

Tim's spec (2026-05-14):
  "Rule centered around the exit indicator activity. Apply ALL THE TIME as a
   safety net. If trade is profitable, take profit at the top. If trade is at
   or below 0, tighten SL as close to BE as possible — let it recover or take
   a small loss, but never eat the big loss."

DUAL-MODE rule:
  • Marker appears while pnl > 0   → TAKE PROFIT NOW at current bar close
                                      (rule outcome = arm_pnl, immediate exit)
  • Marker appears while pnl <= 0  → TIGHTEN SL to current_close - 1p buffer
                                      (walk forward; either SL hits or trade
                                      recovers to natural exit)

Algorithm:
  1. At trade open, snapshot opposing peak_sep markers up to entry bar = BASELINE
  2. On each subsequent M15 bar close (until exit or watch_bars limit):
     - Recompute markers up to that bar; diff vs baseline
     - If NEW opposing marker:
         * pnl > 0  → exit at this bar's close (book the top)
         * pnl <= 0 → arm virtual SL, walk forward to either SL hit or natural exit
"""
import os
import sqlite3
import sys
from collections import Counter

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient
from backtester.ema_separation import format_chart_signals
from dateutil.parser import isoparse
from datetime import timedelta

DB = "~/Jarvis/Database/v2/trading_forex.db"

LOOKBACK_BARS = 140        # M15 bars BEFORE entry (gives E100 valid runway)
WATCH_BARS_SWEEP = [5, 8, 15, 999]   # 999 = "all the time, no cutoff"
NEG_LOCK_BUFFER_PIPS = 1.0   # for trades at <= 0: SL = current_close - 1p adverse buffer


def get_pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def fetch_window(pair, entry_time, exit_time, lookback_bars=LOOKBACK_BARS):
    oc = OandaClient()
    try:
        ft = isoparse(entry_time)
        tt = isoparse(exit_time)
        ft_lookback = ft - timedelta(minutes=15 * lookback_bars)
        candles = oc.get_candles(pair, granularity="M15", from_time=ft_lookback, to_time=tt, count=500)
    except Exception:
        return None, None
    if not candles:
        return None, None

    flat = []
    for c in candles:
        t = c.get("time", "")
        if not t:
            continue
        bar_close_px = float(c.get("mid", {}).get("c", c.get("close", 0)))
        if not bar_close_px:
            continue
        flat.append({
            "time": t,
            "open":  float(c.get("mid", {}).get("o", c.get("open", 0))),
            "high":  float(c.get("mid", {}).get("h", c.get("high", 0))),
            "low":   float(c.get("mid", {}).get("l", c.get("low", 0))),
            "close": bar_close_px,
        })
    # Find entry bar idx
    try:
        et = isoparse(entry_time)
    except Exception:
        return None, None
    entry_idx = None
    for i, c in enumerate(flat):
        try:
            ct = isoparse(c["time"])
        except Exception:
            continue
        if ct >= et:
            entry_idx = i
            break
    if entry_idx is None and flat:
        entry_idx = len(flat) - 1
    return flat, entry_idx


def opposing_peak_seps_up_to(flat_candles, end_idx, oppose_dir):
    """Return list of (time, idx) for all opposing peak_sep markers in flat_candles[:end_idx+1]."""
    if end_idx < 100:
        return []
    sub = flat_candles[: end_idx + 1]
    signals = format_chart_signals(sub) or []
    t2i = {c["time"]: i for i, c in enumerate(sub)}
    out = []
    for s in signals:
        if s.get("type") != "peak_sep":
            continue
        if s.get("direction") != oppose_dir:
            continue
        idx = t2i.get(s.get("time"))
        if idx is not None:
            out.append((s.get("time"), idx))
    return out


def simulate_trade(flat_candles, entry_idx, entry_price, trade_dir, pip, watch_bars):
    """Simulate the dual-mode marker rule. Returns dict:
        {'rule_fired': bool, 'fire_bar': int|None, 'rule_pnl': float|None,
         'arm_pnl': float|None, 'exit_kind': str|None}

    Logic:
      1. Snapshot opposing peak_sep markers at entry → baseline
      2. Walk bars 1..watch_bars: detect NEW opposing marker
      3. On detection — DUAL MODE:
          - arm_pnl > 0:  take profit NOW at current bar close (exit_kind='take_profit')
          - arm_pnl <= 0: arm virtual SL at bar_close - 1p adverse,
                           walk forward to either SL hit (exit_kind='sl_hit')
                           or natural exit (exit_kind='no_sl_hit')
    """
    is_long = trade_dir.lower() in ("buy", "long")
    oppose_dir = "sell" if is_long else "buy"

    baseline = opposing_peak_seps_up_to(flat_candles, entry_idx, oppose_dir)
    baseline_times = {t for (t, _) in baseline}

    arm_bar = None
    arm_pnl = None
    sl_price = None
    max_check = min(entry_idx + watch_bars, len(flat_candles) - 1)
    for bar in range(entry_idx + 1, max_check + 1):
        current = opposing_peak_seps_up_to(flat_candles, bar, oppose_dir)
        if any(t not in baseline_times for (t, _) in current):
            bar_close = flat_candles[bar]["close"]
            diff = (bar_close - entry_price) if is_long else (entry_price - bar_close)
            arm_pnl = diff / pip
            arm_bar = bar
            if arm_pnl > 0:
                # PROFITABLE: take profit immediately at current bar close
                return {"rule_fired": True, "fire_bar": arm_bar - entry_idx,
                        "rule_pnl": arm_pnl, "arm_pnl": arm_pnl,
                        "exit_kind": "take_profit"}
            # AT/BELOW 0: tighten SL, walk forward
            sl_price = bar_close - (NEG_LOCK_BUFFER_PIPS * pip if is_long else -NEG_LOCK_BUFFER_PIPS * pip)
            break

    if arm_bar is None:
        return {"rule_fired": False, "fire_bar": None, "rule_pnl": None,
                "arm_pnl": None, "exit_kind": None}

    # Walk forward looking for SL hit
    for bar in range(arm_bar + 1, len(flat_candles)):
        bar_high = flat_candles[bar]["high"]
        bar_low = flat_candles[bar]["low"]
        hit = (bar_low <= sl_price) if is_long else (bar_high >= sl_price)
        if hit:
            sl_diff = (sl_price - entry_price) if is_long else (entry_price - sl_price)
            rule_pnl = sl_diff / pip
            return {"rule_fired": True, "fire_bar": arm_bar - entry_idx,
                    "rule_pnl": rule_pnl, "arm_pnl": arm_pnl,
                    "exit_kind": "sl_hit"}

    # No SL hit → trade runs to natural exit
    return {"rule_fired": True, "fire_bar": arm_bar - entry_idx,
            "rule_pnl": None, "arm_pnl": arm_pnl,
            "exit_kind": "no_sl_hit"}


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, entry_price, entry_time, exit_time, pnl_pips, source
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
        AND entry_time >= datetime('now','-30 days')
        AND pnl_pips IS NOT NULL
        AND entry_price IS NOT NULL
        ORDER BY entry_time ASC
    """).fetchall()
    print(f"Event-driven marker backtest on {len(trades)} trades (snipe+scout+manual)")
    print()

    # Pre-fetch all candle windows once
    print("Fetching candle windows...")
    windows = {}
    for i, t in enumerate(trades):
        if i % 25 == 0:
            print(f"  {i}/{len(trades)}...")
        flat, entry_idx = fetch_window(t["pair"], t["entry_time"], t["exit_time"])
        if flat and entry_idx is not None and len(flat) >= 110:
            windows[t["id"]] = (flat, entry_idx)
    print(f"  Done — {len(windows)} usable windows")
    print()

    # Sweep
    for watch in WATCH_BARS_SWEEP:
        print(f"═══════════ WATCH = {watch} M15 bars after entry ═══════════")
        results = []
        for t in trades:
            w = windows.get(t["id"])
            if not w:
                continue
            flat, entry_idx = w
            pip = get_pip_size(t["pair"])
            actual = float(t["pnl_pips"])
            sim = simulate_trade(flat, entry_idx, float(t["entry_price"]),
                                  t["direction"], pip, watch)
            # If rule fired but SL never hit, effective_pnl is the actual exit
            if sim["rule_fired"]:
                effective_pnl = sim["rule_pnl"] if sim["rule_pnl"] is not None else actual
            else:
                effective_pnl = actual
            results.append({
                "id": t["id"],
                "source": t["source"],
                "actual_pnl": actual,
                "is_winner": actual > 0,
                "rule_fired": sim["rule_fired"],
                "fire_bar": sim.get("fire_bar"),
                "arm_pnl": sim.get("arm_pnl"),
                "rule_pnl": sim.get("rule_pnl"),
                "effective_pnl": effective_pnl,
                "exit_kind": sim.get("exit_kind"),
                "delta": effective_pnl - actual,        # >0 = rule HELPED, <0 = rule HURT
            })

        # Per-source impact split by mode
        for src in ("snipe_direct", "scout", "manual", "ALL"):
            grp = results if src == "ALL" else [r for r in results if r["source"] == src]
            if not grp:
                continue
            fires = [r for r in grp if r["rule_fired"]]
            tp_mode = [r for r in fires if r["exit_kind"] == "take_profit"]
            sl_mode = [r for r in fires if r["exit_kind"] == "sl_hit"]
            no_hit  = [r for r in fires if r["exit_kind"] == "no_sl_hit"]
            # Deltas
            tp_delta = sum(r["delta"] for r in tp_mode)   # take-profit-mode delta sum
            sl_delta = sum(r["delta"] for r in sl_mode)   # sl-tighten-mode delta sum (only counts hits)
            net = tp_delta + sl_delta
            print(f"  [{src:14s}] n={len(grp):3d}  fires={len(fires):3d} "
                  f"(TP-mode={len(tp_mode)} Δ{tp_delta:+6.1f}p | "
                  f"SL-hit={len(sl_mode)} Δ{sl_delta:+6.1f}p | "
                  f"no-hit={len(no_hit)})  NET={net:+7.1f}p")
        print()

        # TP-mode breakdown — when we book the top, what did we leave on the table?
        all_fires = [r for r in results if r["rule_fired"]]
        if all_fires:
            tp_fires = [r for r in all_fires if r["exit_kind"] == "take_profit"]
            sl_fires = [r for r in all_fires if r["exit_kind"] == "sl_hit"]
            no_hits = [r for r in all_fires if r["exit_kind"] == "no_sl_hit"]
            print(f"  Fires: {len(all_fires)} total → "
                  f"{len(tp_fires)} take-profit, {len(sl_fires)} SL-tighten-hit, "
                  f"{len(no_hits)} SL-tighten-recovered-to-natural-exit")
            if tp_fires:
                tp_helped = [r for r in tp_fires if r["delta"] > 0]
                tp_hurt   = [r for r in tp_fires if r["delta"] < 0]
                print(f"    TP-mode: helped={len(tp_helped)} (+{sum(r['delta'] for r in tp_helped):.1f}p)  "
                      f"hurt={len(tp_hurt)} ({sum(r['delta'] for r in tp_hurt):.1f}p)")
            if sl_fires:
                sl_helped = [r for r in sl_fires if r["delta"] > 0]
                sl_hurt   = [r for r in sl_fires if r["delta"] < 0]
                print(f"    SL-mode: helped={len(sl_helped)} (+{sum(r['delta'] for r in sl_helped):.1f}p)  "
                      f"hurt={len(sl_hurt)} ({sum(r['delta'] for r in sl_hurt):.1f}p)")
        print()


if __name__ == "__main__":
    main()
