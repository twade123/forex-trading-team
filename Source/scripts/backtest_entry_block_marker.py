"""backtest_entry_block_marker.py — Backtest the missing piece: ENTRY BLOCK
when an opposing peak_sep marker exists at/before entry.

Already shipped (v2): during-trade marker rule (markers appearing +1..+15 after entry).
This backtest: would we be better off ALSO blocking entries that open while an
opposing marker is already on the chart?

For each trade (last 30d, snipe+scout+manual, NO kronos):
  1. Fetch candles up to entry bar
  2. Compute peak_sep markers via format_chart_signals
  3. Find opposing markers (direction != trade direction)
  4. If any opposing marker has offset in [-WINDOW, 0] → ENTRY BLOCKED
  5. If blocked: pnl = 0 (no trade taken). If allowed: actual pnl.

Sweep windows: 3, 5, 7, 10 bars.

For each window: report
  - # winners blocked (lost profit)
  - # losers blocked (saved loss)
  - net pip impact
  - WR before vs after
"""
import os, sqlite3, sys
SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient
from backtester.ema_separation import format_chart_signals
from dateutil.parser import isoparse
from datetime import timedelta

DB = "~/Jarvis/Database/v2/trading_forex.db"
LOOKBACK_BARS = 140
WINDOWS = [3, 5, 7, 10]


def get_pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def fetch_window_up_to_entry(pair, entry_time):
    """Fetch candles ending AT entry bar (not after)."""
    oc = OandaClient()
    try:
        ft = isoparse(entry_time)
        ft_lookback = ft - timedelta(minutes=15 * LOOKBACK_BARS)
        # Fetch up through entry time (inclusive)
        candles = oc.get_candles(pair, granularity="M15",
                                 from_time=ft_lookback, to_time=ft + timedelta(minutes=5),
                                 count=200)
    except Exception:
        return None, None
    if not candles:
        return None, None
    flat = []
    for c in candles:
        t = c.get("time", "")
        if not t: continue
        mid = c.get("mid", {})
        close = float(mid.get("c", 0))
        if not close: continue
        flat.append({
            "time": t,
            "open":  float(mid.get("o", 0)),
            "high":  float(mid.get("h", 0)),
            "low":   float(mid.get("l", 0)),
            "close": close,
        })
    try:
        et = isoparse(entry_time)
    except Exception:
        return None, None
    # Find entry bar idx — first bar whose time >= entry_time
    entry_idx = None
    for i, c in enumerate(flat):
        try: ct = isoparse(c["time"])
        except: continue
        if ct >= et:
            entry_idx = i
            break
    if entry_idx is None and flat:
        entry_idx = len(flat) - 1
    return flat, entry_idx


def has_opposing_marker_in_window(candles, entry_idx, trade_dir, window):
    """Check if opposing peak_sep marker exists at offset [-window, 0] from entry."""
    if not candles or entry_idx is None or len(candles) < 110:
        return False, None
    sub = candles[: entry_idx + 1]  # candles up to and including entry bar
    signals = format_chart_signals(sub) or []
    peak_seps = [s for s in signals if s.get("type") == "peak_sep"]
    if not peak_seps: return False, None
    t2i = {c["time"]: i for i, c in enumerate(sub)}
    is_long = trade_dir.lower() in ("buy", "long")
    oppose = "sell" if is_long else "buy"
    for s in peak_seps:
        if s.get("direction") != oppose: continue
        idx = t2i.get(s.get("time"))
        if idx is None: continue
        offset = idx - entry_idx
        if -window <= offset <= 0:
            return True, offset
    return False, None


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, source, entry_price, entry_time, pnl_pips,
               max_favorable_excursion_pips as mfe
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= datetime('now','-30 days')
          AND pnl_pips IS NOT NULL
          AND entry_price IS NOT NULL
        ORDER BY entry_time DESC
    """).fetchall()
    print(f"Entry-block backtest on {len(trades)} trades (last 30d)")
    print()

    # Pre-compute marker positions once per trade (window doesn't change candle data)
    print("Fetching candles and computing markers per trade...")
    marker_info = {}
    for i, t in enumerate(trades):
        if i % 25 == 0: print(f"  {i}/{len(trades)}...")
        candles, entry_idx = fetch_window_up_to_entry(t["pair"], t["entry_time"])
        if not candles or entry_idx is None or len(candles) < 110:
            marker_info[t["id"]] = None
            continue
        # Find ALL opposing marker offsets up to entry
        sub = candles[: entry_idx + 1]
        signals = format_chart_signals(sub) or []
        t2i = {c["time"]: i for i, c in enumerate(sub)}
        is_long = t["direction"].lower() in ("buy", "long")
        oppose = "sell" if is_long else "buy"
        offsets = []
        for s in signals:
            if s.get("type") != "peak_sep": continue
            if s.get("direction") != oppose: continue
            idx = t2i.get(s.get("time"))
            if idx is not None:
                offsets.append(idx - entry_idx)
        marker_info[t["id"]] = offsets
    usable = sum(1 for v in marker_info.values() if v is not None)
    print(f"  Done — {usable}/{len(trades)} usable.\n")

    # Aggregate baseline
    baseline_total = sum(float(t["pnl_pips"]) for t in trades if marker_info[t["id"]] is not None)
    baseline_wins = sum(1 for t in trades if marker_info[t["id"]] is not None and float(t["pnl_pips"]) > 0)
    baseline_losses = sum(1 for t in trades if marker_info[t["id"]] is not None and float(t["pnl_pips"]) <= 0)
    baseline_wr = 100 * baseline_wins / max(baseline_wins + baseline_losses, 1)
    print(f"BASELINE (no entry block): n={baseline_wins + baseline_losses}  "
          f"wins={baseline_wins} losses={baseline_losses} WR={baseline_wr:.1f}%  "
          f"total_pnl={baseline_total:+.1f}p\n")

    # Sweep windows
    for window in WINDOWS:
        print(f"═══════════ WINDOW = -{window}..0 bars from entry ═══════════")
        blocked_winners = []
        blocked_losers = []
        allowed = []
        for t in trades:
            offsets = marker_info.get(t["id"])
            if offsets is None:
                allowed.append(t)
                continue
            has_marker = any(-window <= o <= 0 for o in offsets)
            if has_marker:
                if float(t["pnl_pips"]) > 0:
                    blocked_winners.append(t)
                else:
                    blocked_losers.append(t)
            else:
                allowed.append(t)

        pip_lost_blocking_winners = sum(float(t["pnl_pips"]) for t in blocked_winners)
        pip_saved_blocking_losers = sum(-float(t["pnl_pips"]) for t in blocked_losers)
        net = pip_saved_blocking_losers - pip_lost_blocking_winners

        # WR after
        new_wins = sum(1 for t in allowed if float(t["pnl_pips"]) > 0)
        new_losses = sum(1 for t in allowed if float(t["pnl_pips"]) <= 0)
        new_wr = 100 * new_wins / max(new_wins + new_losses, 1)

        # Per source
        for src in ("snipe_direct", "scout", "manual", "ALL"):
            sw = [t for t in blocked_winners if (src == "ALL" or t["source"] == src)]
            sl = [t for t in blocked_losers  if (src == "ALL" or t["source"] == src)]
            sa = [t for t in allowed         if (src == "ALL" or t["source"] == src)]
            if not (sw or sl or sa): continue
            pl_w = sum(float(t["pnl_pips"]) for t in sw)
            pl_s = sum(-float(t["pnl_pips"]) for t in sl)
            n_blocked = len(sw) + len(sl)
            n_total = n_blocked + len(sa)
            print(f"  [{src:14s}] blocked={n_blocked}/{n_total} "
                  f"({len(sw)} winners -{pl_w:.1f}p / {len(sl)} losers +{pl_s:.1f}p)  "
                  f"NET={(pl_s - pl_w):+7.1f}p")
        print(f"  WR: baseline={baseline_wr:.1f}% → after-block={new_wr:.1f}%")
        print(f"  Total pip impact: NET {net:+.1f}p")
        print()


if __name__ == "__main__":
    main()
