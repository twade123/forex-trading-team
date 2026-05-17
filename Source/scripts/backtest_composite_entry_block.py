"""backtest_composite_entry_block.py — Composite entry-block signal.

Rule (Tim's spec 2026-05-15):
  Block entry when ALL of:
    A. Opposing ⚠ Exit marker confirmed within last K M15 bars at entry time
       (peak_sep marker from format_chart_signals — same as chart)
    B. Last 1-2 candles show reversal character:
       - For Exit Long opposing BUY:  last close < open OR cum 2-bar close < open
       - For Exit Short opposing SELL: last close > open OR cum 2-bar close > open
    C. Price has retraced from the peak:
       - BUY block: current_close < highest_high(last 5 bars)
       - SELL block: current_close > lowest_low(last 5 bars)

Tested on every snipe/scout/manual trade in last 30 days. Outputs:
  - Block rate
  - Winner-kill rate
  - Loser-catch rate
  - Per-source net pip
  - Per-trade classification (every loser shown)

Sweep K (freshness): 2, 4, 6 bars
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
RETRACE_LOOKBACK = 5  # bars to compute peak high/low against current

def get_pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def fetch_up_to_entry(pair, entry_time):
    oc = OandaClient()
    try:
        ft = isoparse(entry_time)
        ft_lookback = ft - timedelta(minutes=15 * LOOKBACK_BARS)
        candles = oc.get_candles(pair, granularity="M15",
                                  from_time=ft_lookback, to_time=ft + timedelta(minutes=5))
    except Exception:
        return None, None
    if not candles: return None, None
    flat = []
    for c in candles:
        t = c.get("time", "")
        if not t: continue
        mid = c.get("mid", {})
        close = float(mid.get("c", 0))
        if not close: continue
        flat.append({"time": t,
                     "open":  float(mid.get("o", 0)),
                     "high":  float(mid.get("h", 0)),
                     "low":   float(mid.get("l", 0)),
                     "close": close})
    try: et = isoparse(entry_time)
    except: return None, None
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


def composite_block_check(candles, entry_idx, trade_dir, K):
    """Return (block: bool, reason_dict)"""
    if not candles or entry_idx is None or len(candles) < 110:
        return False, {"reason": "insufficient_data"}
    sub = candles[: entry_idx + 1]
    signals = format_chart_signals(sub) or []
    is_long = trade_dir.lower() in ("buy", "long")
    oppose_dir = "sell" if is_long else "buy"  # Exit Long has dir='sell', Exit Short has dir='buy'

    # A. Find opposing peak_sep marker — need its UNDERLYING peak to be confirmed within K bars
    # A peak at index P is confirmed when we have bar P+3. Marker labeled at P-3.
    # At entry (bar = entry_idx), peaks at entry_idx-3 or earlier are confirmed.
    # "Confirmed within last K bars" means peak at entry_idx-3 down to entry_idx-3-K+1
    # = peak at entry_idx-3..entry_idx-K-2
    # = marker label at peak-3 = entry_idx-6..entry_idx-K-5
    # = offset from entry: -6..-(K+5)
    t2i = {c["time"]: i for i, c in enumerate(sub)}
    fresh_markers = []
    for s in signals:
        if s.get("type") != "peak_sep": continue
        if s.get("direction") != oppose_dir: continue
        idx = t2i.get(s.get("time"))
        if idx is None: continue
        offset = idx - entry_idx  # negative = before entry
        # The peak that triggered this marker is at offset+3 from entry
        # "Fresh" = peak confirmed within last K bars = peak at offset_peak in [-3-K+1, -3]
        peak_offset = offset + 3
        if -(3 + K - 1) <= peak_offset <= -3:
            fresh_markers.append({"offset": offset, "peak_offset": peak_offset, "time": s.get("time")})
    if not fresh_markers:
        return False, {"reason": "no_fresh_marker", "all_marker_offsets": []}

    # B. Reversal candle character — last bar before entry
    last = sub[-1]
    second_last = sub[-2] if len(sub) >= 2 else None
    last_red  = last["close"] < last["open"]
    last_green = last["close"] > last["open"]
    cum2_red  = second_last and (last["close"] + second_last["close"]) < (last["open"] + second_last["open"])
    cum2_green = second_last and (last["close"] + second_last["close"]) > (last["open"] + second_last["open"])
    if is_long:
        # Block BUY when reversal is bearish (against the BUY)
        reversal = last_red or cum2_red
    else:
        # Block SELL when reversal is bullish
        reversal = last_green or cum2_green
    if not reversal:
        return False, {"reason": "no_reversal_candles", "fresh_markers": fresh_markers}

    # C. Price retraced from peak
    last_n = sub[-RETRACE_LOOKBACK:]
    current = last["close"]
    if is_long:
        recent_high = max(b["high"] for b in last_n)
        retraced = current < recent_high
    else:
        recent_low = min(b["low"] for b in last_n)
        retraced = current > recent_low
    if not retraced:
        return False, {"reason": "no_retrace_yet", "fresh_markers": fresh_markers}

    return True, {
        "reason": "BLOCK",
        "fresh_marker_offsets": [m["offset"] for m in fresh_markers],
        "last_bar_red_or_green": "red" if last_red else "green" if last_green else "doji",
        "current_vs_extreme": f"{current:.5f}",
    }


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, source, entry_price, entry_time, pnl_pips,
               max_favorable_excursion_pips as mfe, max_adverse_excursion_pips as mae
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= datetime('now','-30 days')
          AND pnl_pips IS NOT NULL
          AND entry_price IS NOT NULL
        ORDER BY entry_time DESC
    """).fetchall()
    print(f"Composite entry-block backtest on {len(trades)} trades (last 30d)")
    print()

    # Cache candles per trade once
    print("Fetching candles per trade...")
    cache = {}
    for i, t in enumerate(trades):
        if i % 25 == 0: print(f"  {i}/{len(trades)}...")
        candles, entry_idx = fetch_up_to_entry(t["pair"], t["entry_time"])
        cache[t["id"]] = (candles, entry_idx) if candles and entry_idx is not None else None
    usable = sum(1 for v in cache.values() if v)
    print(f"  Done — {usable}/{len(trades)} usable.\n")

    for K in (2, 4, 6, 10):
        print(f"═══════════ FRESHNESS K = {K} bars (peak confirmed within last {K} bars) ═══════════")
        blocked_winners = []
        blocked_losers = []
        allowed_winners = []
        allowed_losers = []
        block_reasons = {}
        for t in trades:
            cd = cache.get(t["id"])
            if not cd:
                # Unknown — treat as allowed
                if float(t["pnl_pips"]) > 0: allowed_winners.append(t)
                else: allowed_losers.append(t)
                continue
            candles, entry_idx = cd
            block, info = composite_block_check(candles, entry_idx, t["direction"], K)
            if block:
                if float(t["pnl_pips"]) > 0:
                    blocked_winners.append((t, info))
                else:
                    blocked_losers.append((t, info))
            else:
                if float(t["pnl_pips"]) > 0: allowed_winners.append(t)
                else: allowed_losers.append(t)
                block_reasons[info.get("reason", "?")] = block_reasons.get(info.get("reason", "?"), 0) + 1

        # Per source breakdown
        all_blocked = blocked_winners + blocked_losers
        for src in ("snipe_direct", "scout", "manual", "ALL"):
            bw = [(t,i) for t,i in blocked_winners if src=="ALL" or t["source"]==src]
            bl = [(t,i) for t,i in blocked_losers  if src=="ALL" or t["source"]==src]
            aw = [t for t in allowed_winners if src=="ALL" or t["source"]==src]
            al = [t for t in allowed_losers  if src=="ALL" or t["source"]==src]
            total = len(bw)+len(bl)+len(aw)+len(al)
            if total == 0: continue
            pip_killed_w = sum(float(t["pnl_pips"]) for t,_ in bw)
            pip_saved_l = sum(-float(t["pnl_pips"]) for t,_ in bl)
            new_wr = 100 * len(aw) / max(len(aw)+len(al), 1)
            print(f"  [{src:14s}] n={total:3d}  blocked={len(bw)+len(bl):3d} "
                  f"({len(bw)}W -{pip_killed_w:.1f}p / {len(bl)}L +{pip_saved_l:.1f}p)  "
                  f"NET={pip_saved_l - pip_killed_w:+7.1f}p  new_WR={new_wr:.1f}%")
        print()

        if K == 4:  # detailed list at our likely sweet spot
            print(f"  ── ALL BLOCKED LOSERS (K=4) ──")
            for t, info in sorted(blocked_losers, key=lambda x: float(x[0]["pnl_pips"])):
                print(f"    #{t['id']:<6s} {t['pair']:<8s} {t['source']:<14s} {t['direction']:<4s} "
                      f"pnl={float(t['pnl_pips']):+6.1f}p  marker_offsets={info.get('fresh_marker_offsets')}")
            print(f"  ── ALL BLOCKED WINNERS (K=4) ── (these would have been killed)")
            for t, info in sorted(blocked_winners, key=lambda x: -float(x[0]["pnl_pips"])):
                print(f"    #{t['id']:<6s} {t['pair']:<8s} {t['source']:<14s} {t['direction']:<4s} "
                      f"pnl={float(t['pnl_pips']):+6.1f}p  marker_offsets={info.get('fresh_marker_offsets')}")
            print()


if __name__ == "__main__":
    main()
