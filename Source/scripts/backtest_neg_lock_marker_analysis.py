"""backtest_neg_lock_marker_analysis.py — For each snipe in last 30 days, check
whether an opposing ⚠ Exit (peak_sep) marker appears in a tight window around entry.

Goal: Test Tim's hypothesis — losers go negative immediately because they enter
right as a peak_sep marker fires. If true, that gives us a FASTER trigger than
"wait for -5p drift": detect marker-near-entry and immediately move SL to BE.

For each trade:
  1. Fetch candles from (entry - 60 M15 bars) through exit
  2. Run format_chart_signals on the full window
  3. Find OPPOSING peak_sep markers (marker dir vs trade dir)
  4. Record nearest opposing marker's offset from entry bar (-N = before, +N = after)
  5. Also walk pnl_path with -5p never-positive rule to flag caught trades

Cross-tabs:
  - All trades: marker-near-entry frequency by outcome (won / lost)
  - Caught-by-rule subset (losers): how many had a marker in window
  - Untouched-by-rule subset: do winners also have these markers?

If marker presence cleanly separates losers from winners → ship the marker rule
instead of (or alongside) the -5p drift rule.
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

LOOKBACK_BARS = 140     # M15 bars to fetch BEFORE entry — needs to be >100 for E100 to be valid at entry bar
NEAR_ENTRY_WIN = 5      # |bars_from_entry| <= this counts as "near entry"
LOCK_THRESH = 5         # the -5p threshold from the prior backtest


def get_pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def fetch_window(pair, entry_time, exit_time, lookback_bars=LOOKBACK_BARS):
    """Fetch candles from entry-lookback through exit. Returns (candles, entry_idx)."""
    oc = OandaClient()
    try:
        ft = isoparse(entry_time)
        tt = isoparse(exit_time)
        # Back up lookback*15 minutes from entry
        ft_lookback = ft - timedelta(minutes=15 * lookback_bars)
        candles = oc.get_candles(pair, granularity="M15", from_time=ft_lookback, to_time=tt, count=500)
    except Exception:
        return None, None
    if not candles:
        return None, None

    # Find entry bar idx — first bar with time >= entry_time
    flat = []
    entry_idx = None
    for c in candles:
        t = c.get("time", "")
        if not t:
            continue
        try:
            ct = isoparse(t)
        except Exception:
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
            "_dt":   ct,
        })
    # Locate entry index
    try:
        et = isoparse(entry_time)
    except Exception:
        return None, None
    for i, c in enumerate(flat):
        if c["_dt"] >= et:
            entry_idx = i
            break
    if entry_idx is None and flat:
        entry_idx = len(flat) - 1
    # Strip _dt before returning (format_chart_signals doesn't need it)
    for c in flat:
        c.pop("_dt", None)
    return flat, entry_idx


def find_nearest_opposing_peak_sep(candles, entry_idx, trade_dir):
    """Return offset of nearest opposing peak_sep marker from entry_idx, or None.
    Opposing means marker.direction != trade direction (long ↔ short)."""
    if not candles or entry_idx is None:
        return None
    signals = format_chart_signals(candles) or []
    peak_seps = [s for s in signals if s.get("type") == "peak_sep"]
    if not peak_seps:
        return None

    # Map time -> idx
    t2i = {c["time"]: i for i, c in enumerate(candles)}

    is_long = trade_dir.lower() in ("buy", "long")
    oppose_dir = "sell" if is_long else "buy"

    offsets = []
    for s in peak_seps:
        if s.get("direction") != oppose_dir:
            continue
        idx = t2i.get(s.get("time"))
        if idx is None:
            continue
        offsets.append(idx - entry_idx)
    if not offsets:
        return None
    # Nearest by absolute distance
    offsets.sort(key=lambda x: abs(x))
    return offsets[0]


def pnl_at_each_bar(candles, entry_idx, entry_price, trade_dir, pip):
    """Compute pnl_pips at each post-entry bar close. Returns list starting at entry_idx."""
    is_long = trade_dir.lower() in ("buy", "long")
    out = []
    for c in candles[entry_idx:]:
        diff = (c["close"] - entry_price) if is_long else (entry_price - c["close"])
        out.append(diff / pip)
    return out


def walk_lock(pnl_series, lock_pip):
    """Same rule as backtest: never positive + crosses -lock_pip → fires.
    Returns (fired_bool, bar_idx, lock_pnl) or (False, None, None)."""
    ever_pos = False
    for i, p in enumerate(pnl_series):
        if p > 0:
            return (False, None, None)
        if p <= -lock_pip:
            return (True, i, p)
    return (False, None, None)


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
    print(f"Analyzing {len(trades)} trades (snipe + scout + manual, NO kronos) for marker-near-entry pattern")
    print()

    # Collect per-trade data
    rows = []
    for i, t in enumerate(trades):
        if i % 25 == 0:
            print(f"  {i}/{len(trades)}...")
        candles, entry_idx = fetch_window(t["pair"], t["entry_time"], t["exit_time"])
        if not candles or entry_idx is None or len(candles) < 110:
            continue
        offset = find_nearest_opposing_peak_sep(candles, entry_idx, t["direction"])
        pip = get_pip_size(t["pair"])
        pnl_series = pnl_at_each_bar(candles, entry_idx, float(t["entry_price"]), t["direction"], pip)
        rule_fired, _, _ = walk_lock(pnl_series, LOCK_THRESH)
        rows.append({
            "id": t["id"],
            "pair": t["pair"],
            "dir": t["direction"],
            "source": t["source"],
            "actual_pnl": float(t["pnl_pips"]),
            "is_winner": float(t["pnl_pips"]) > 0,
            "marker_offset": offset,                              # None / negative=before / 0=at / positive=after
            "marker_in_window": offset is not None and abs(offset) <= NEAR_ENTRY_WIN,
            "lock_rule_fires": rule_fired,
        })
    print(f"  Done — {len(rows)} trades with usable data")
    print()

    # ── Slice 1: marker-in-window frequency by outcome, PER SOURCE ────────────
    print("=== MARKER-NEAR-ENTRY FREQUENCY BY OUTCOME (per source) ===")
    print(f"  Window: |offset| <= {NEAR_ENTRY_WIN} bars from entry, opposing direction only")
    print()
    for src in ("snipe_direct", "scout", "manual", "ALL"):
        if src == "ALL":
            group = rows
        else:
            group = [r for r in rows if r["source"] == src]
        if not group:
            continue
        winners = [r for r in group if r["is_winner"]]
        losers  = [r for r in group if not r["is_winner"]]
        w_marker = sum(1 for r in winners if r["marker_in_window"])
        l_marker = sum(1 for r in losers  if r["marker_in_window"])
        wpct = 100*w_marker/max(len(winners),1)
        lpct = 100*l_marker/max(len(losers),1)
        ratio = lpct / max(wpct, 0.01)
        print(f"  [{src}]  n={len(group)} ({len(winners)}W / {len(losers)}L)")
        print(f"    Winners w/ marker: {w_marker}/{len(winners)} ({wpct:.1f}%)")
        print(f"    Losers  w/ marker: {l_marker}/{len(losers)}  ({lpct:.1f}%)   ← S/N ratio: {ratio:.1f}x")
        print()

    # Use ALL rows for downstream slices (3, 4) — already done; keep winners/losers global
    winners = [r for r in rows if r["is_winner"]]
    losers  = [r for r in rows if not r["is_winner"]]

    # ── Slice 2: marker offset distribution (losers only) ─────────────────────
    print("=== MARKER OFFSET DISTRIBUTION (LOSERS WITH MARKERS) ===")
    losers_with_markers = [r for r in losers if r["marker_offset"] is not None]
    offset_buckets = Counter()
    for r in losers_with_markers:
        o = r["marker_offset"]
        if o < -10:    offset_buckets["<-10 (very old)"] += 1
        elif -10 <= o <= -6:  offset_buckets["-10..-6"] += 1
        elif -5 <= o <= -3:   offset_buckets["-5..-3 (just before)"] += 1
        elif -2 <= o <= -1:   offset_buckets["-2..-1 (right before)"] += 1
        elif o == 0:          offset_buckets["0 (at entry bar)"] += 1
        elif 1 <= o <= 2:     offset_buckets["+1..+2 (right after)"] += 1
        elif 3 <= o <= 5:     offset_buckets["+3..+5 (just after)"] += 1
        else:                  offset_buckets[">+5 (later)"] += 1
    for k in ["<-10 (very old)", "-10..-6", "-5..-3 (just before)", "-2..-1 (right before)",
              "0 (at entry bar)", "+1..+2 (right after)", "+3..+5 (just after)", ">+5 (later)"]:
        if offset_buckets.get(k, 0):
            print(f"  {k:30s} {offset_buckets[k]}")
    print()

    # ── Slice 3: cross-tab with the -5p drift rule ────────────────────────────
    print(f"=== MARKER vs -{LOCK_THRESH}p DRIFT RULE CROSS-TAB ===")
    for outcome_name, group in [("LOSERS", losers), ("WINNERS", winners)]:
        both     = sum(1 for r in group if r["marker_in_window"] and r["lock_rule_fires"])
        marker_only = sum(1 for r in group if r["marker_in_window"] and not r["lock_rule_fires"])
        lock_only   = sum(1 for r in group if not r["marker_in_window"] and r["lock_rule_fires"])
        neither     = sum(1 for r in group if not r["marker_in_window"] and not r["lock_rule_fires"])
        total = len(group)
        print(f"  {outcome_name} (n={total})")
        print(f"    BOTH signals fire:           {both}")
        print(f"    MARKER only (not -{LOCK_THRESH}p):    {marker_only}")
        print(f"    -{LOCK_THRESH}p only (no marker):    {lock_only}")
        print(f"    NEITHER:                     {neither}")
    print()

    # ── Slice 4: BE move impact, PER SOURCE ──────────────────────────────────
    print("=== HYPOTHETICAL: 'MARKER IN WINDOW → MOVE TO BREAKEVEN' (per source) ===")
    print("  Assumes BE = 0p (entry price). Trade exits at 0 if rule fires.")
    print()
    for src in ("snipe_direct", "scout", "manual", "ALL"):
        if src == "ALL":
            group = rows
        else:
            group = [r for r in rows if r["source"] == src]
        if not group:
            continue
        s_winners = [r for r in group if r["is_winner"]]
        s_losers  = [r for r in group if not r["is_winner"]]
        killed = [r for r in s_winners if r["marker_in_window"]]
        saved  = [r for r in s_losers  if r["marker_in_window"]]
        pip_lost_w = sum(r["actual_pnl"] for r in killed)
        pip_saved  = sum(-r["actual_pnl"] for r in saved)
        print(f"  [{src}]  Winners killed: {len(killed)} (-{pip_lost_w:.1f}p)   Losers saved: {len(saved)} (+{pip_saved:.1f}p)   NET {pip_saved - pip_lost_w:+.1f}p")
    print()

    # Save raw rows for further analysis if needed
    import json
    out_path = "/tmp/marker_analysis_rows.json"
    with open(out_path, "w") as f:
        json.dump(rows, f, default=str)
    print(f"  Raw rows → {out_path}")


if __name__ == "__main__":
    main()
