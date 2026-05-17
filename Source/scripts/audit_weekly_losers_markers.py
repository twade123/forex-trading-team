"""audit_weekly_losers_markers.py — Deep audit of every losing trade this week.

For each loser:
  1. Fetch M15 candles from (entry - 140 bars) through exit
  2. Compute peak_sep markers via format_chart_signals (same logic the chart uses)
  3. Find opposing-direction markers (opposite to trade direction)
  4. For each marker, compute offset from entry bar (negative = before entry, positive = after)
  5. Classify the loser:
     a) PRE-ENTRY marker (offset -3 to 0):  trade should have been BLOCKED at entry
     b) POST-ENTRY marker (offset +1 to +5): trade should have had SL → break-even
     c) LATE marker (offset >5):              caught by exit_marker_sl rule (working as designed)
     d) NO MARKER:                            not catchable by exit marker rule — different cause

Output: per-loser table showing exact marker timing, current outcome, prescriptive action.
"""
import os, sqlite3, sys
from collections import Counter, defaultdict
SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient
from backtester.ema_separation import format_chart_signals
from dateutil.parser import isoparse
from datetime import timedelta

DB = "~/Jarvis/Database/v2/trading_forex.db"
LOOKBACK_BARS = 140


def get_pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def fetch_window(pair, entry_time, exit_time):
    oc = OandaClient()
    try:
        ft = isoparse(entry_time)
        tt = isoparse(exit_time)
        ft_lookback = ft - timedelta(minutes=15 * LOOKBACK_BARS)
        candles = oc.get_candles(pair, granularity="M15", from_time=ft_lookback, to_time=tt, count=500)
    except Exception:
        return None, None
    if not candles:
        return None, None
    flat = []
    for c in candles:
        t = c.get("time", "")
        if not t: continue
        bar_close_px = float(c.get("mid", {}).get("c", c.get("close", 0)))
        if not bar_close_px: continue
        flat.append({
            "time": t,
            "open":  float(c.get("mid", {}).get("o", c.get("open", 0))),
            "high":  float(c.get("mid", {}).get("h", c.get("high", 0))),
            "low":   float(c.get("mid", {}).get("l", c.get("low", 0))),
            "close": bar_close_px,
        })
    try:
        et = isoparse(entry_time)
    except Exception:
        return None, None
    entry_idx = None
    for i, c in enumerate(flat):
        try:
            ct = isoparse(c["time"])
        except Exception: continue
        if ct >= et:
            entry_idx = i
            break
    if entry_idx is None and flat:
        entry_idx = len(flat) - 1
    return flat, entry_idx


def find_opposing_markers(candles, entry_idx, trade_dir):
    if not candles or entry_idx is None: return []
    signals = format_chart_signals(candles) or []
    peak_seps = [s for s in signals if s.get("type") == "peak_sep"]
    if not peak_seps: return []
    t2i = {c["time"]: i for i, c in enumerate(candles)}
    is_long = trade_dir.lower() in ("buy", "long")
    oppose = "sell" if is_long else "buy"
    out = []
    for s in peak_seps:
        if s.get("direction") != oppose: continue
        idx = t2i.get(s.get("time"))
        if idx is not None:
            out.append({"time": s.get("time"), "offset": idx - entry_idx})
    out.sort(key=lambda x: x["offset"])
    return out


def classify(markers):
    """Return classification tag and the relevant marker (nearest applicable)."""
    if not markers:
        return "NO_MARKER", None
    # Find markers in interesting windows
    # PRE-ENTRY: -3..0 (the "should have been blocked" zone)
    pre = [m for m in markers if -3 <= m["offset"] <= 0]
    if pre:
        # Take the closest to 0 (most recent before entry)
        m = max(pre, key=lambda x: x["offset"])
        return "PRE_ENTRY_BLOCK", m
    # POST-ENTRY: +1..+5 (the "should have BE'd" zone)
    post = [m for m in markers if 1 <= m["offset"] <= 5]
    if post:
        m = min(post, key=lambda x: x["offset"])
        return "POST_ENTRY_BE", m
    # Older PRE (-4..-10): not actionable as "block at entry" but exists
    far_pre = [m for m in markers if -10 <= m["offset"] < -3]
    if far_pre:
        m = max(far_pre, key=lambda x: x["offset"])
        return "FAR_PRE", m
    # LATE (+6..+15): exit_marker rule's territory
    late = [m for m in markers if 6 <= m["offset"] <= 15]
    if late:
        m = min(late, key=lambda x: x["offset"])
        return "LATE_EXIT_MARKER", m
    # Anything else
    return "OUT_OF_WINDOW", markers[0]


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, source, entry_price, sl_price, entry_time, exit_time,
               pnl_pips, max_adverse_excursion_pips as mae, max_favorable_excursion_pips as mfe
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= datetime('now','-7 days')
          AND pnl_pips IS NOT NULL
          AND pnl_pips < 0
          AND entry_price IS NOT NULL
        ORDER BY entry_time DESC
    """).fetchall()
    print(f"Auditing {len(trades)} losing trades from last 7 days (snipe+scout+manual)")
    print()

    rows = []
    for i, t in enumerate(trades):
        if i % 10 == 0: print(f"  {i}/{len(trades)}...")
        candles, entry_idx = fetch_window(t["pair"], t["entry_time"], t["exit_time"])
        if not candles or entry_idx is None or len(candles) < 110:
            rows.append({**dict(t), "tag": "NO_DATA", "marker_offset": None, "marker_time": None})
            continue
        markers = find_opposing_markers(candles, entry_idx, t["direction"])
        tag, m = classify(markers)
        rows.append({
            **dict(t),
            "tag": tag,
            "marker_offset": m["offset"] if m else None,
            "marker_time": m["time"] if m else None,
            "all_marker_offsets": [mk["offset"] for mk in markers if -15 <= mk["offset"] <= 20],
        })
    print(f"  Done — {len(rows)} losers analyzed.")
    print()

    # ── Per-tag summary ──
    print("="*100)
    print("CATEGORIZATION SUMMARY")
    print("="*100)
    tag_groups = defaultdict(list)
    for r in rows:
        tag_groups[r["tag"]].append(r)

    tag_order = ["PRE_ENTRY_BLOCK", "POST_ENTRY_BE", "FAR_PRE", "LATE_EXIT_MARKER", "OUT_OF_WINDOW", "NO_MARKER", "NO_DATA"]
    tag_explain = {
        "PRE_ENTRY_BLOCK": "Marker fired -3..0 bars BEFORE entry → trade should have been BLOCKED at entry",
        "POST_ENTRY_BE":   "Marker fired +1..+5 bars AFTER entry → SL should have moved to BE",
        "FAR_PRE":         "Marker fired -10..-4 bars before entry → too old to block, but contextually bearish at entry",
        "LATE_EXIT_MARKER":"Marker fired +6..+15 bars into trade → exit_marker rule's territory (working as designed if it fired)",
        "OUT_OF_WINDOW":   "Marker exists but outside any actionable window",
        "NO_MARKER":       "NO opposing marker found in candle window — different failure cause",
        "NO_DATA":         "Could not fetch candles or insufficient history",
    }
    for tag in tag_order:
        grp = tag_groups[tag]
        if not grp: continue
        total_pip = sum(float(r["pnl_pips"]) for r in grp)
        avg_pip = total_pip / len(grp)
        print(f"\n  [{tag}]  n={len(grp)}  total_pnl={total_pip:.1f}p  avg={avg_pip:.1f}p")
        print(f"    {tag_explain[tag]}")
    print()

    # ── Detailed table per loser ──
    print("="*150)
    print("DETAILED LOSER AUDIT — chronological (most recent first)")
    print("="*150)
    print(f"  {'id':<6s} {'pair':<8s} {'src':<14s} {'dir':<4s} {'entry_time':<19s} {'pnl':<7s} "
          f"{'MAE':<6s} {'MFE':<6s} {'SLp':<6s} {'tag':<18s} {'mk_off':<8s} marker_offsets_in_window")
    print('-'*150)
    for r in rows:
        pip = get_pip_size(r["pair"])
        sl_dist = abs(float(r["entry_price"]) - float(r["sl_price"])) / pip if r.get("sl_price") else 0
        et = r["entry_time"][:19] if r["entry_time"] else ""
        mo = r.get("marker_offset")
        all_mo = r.get("all_marker_offsets") or []
        all_mo_str = "[" + ",".join(f"{x:+d}" for x in all_mo[:8]) + "]"
        print(f"  {r['id']:<6s} {r['pair']:<8s} {r['source']:<14s} {r['direction']:<4s} "
              f"{et:<19s} {float(r['pnl_pips']):+7.1f} "
              f"{(r['mae'] or 0):<6.1f} {(r['mfe'] or 0):<6.1f} {sl_dist:<6.1f} "
              f"{r['tag']:<18s} {('—' if mo is None else f'{mo:+d}'):<8s} {all_mo_str}")


if __name__ == "__main__":
    main()
