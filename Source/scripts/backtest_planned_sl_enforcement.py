"""backtest_planned_sl_enforcement.py — Simulate a new guardian rule that
enforces the original (DB-recorded) sl_price as a hard stop.

Context: position_guardian.py widens OANDA's SL on spawn from the planned
sl_price to a catastrophic floor (3×ATR or E100+0.5×ATR). For trades that go
straight adverse from minute 1, none of guardian's dynamic-management rules
fire (they all require some prior favorable state). Result: trades bleed past
the user's intended SL until they hit OANDA's wide catastrophic SL — or until
my exit_marker rule fires later.

This backtest simulates: at each M15 bar after entry, if pnl_pips reaches
-planned_sl_distance, exit at that price.

Compares actual outcome vs simulated outcome per source.
"""
import os, sqlite3, sys
from collections import Counter
SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient
from dateutil.parser import isoparse
from datetime import timedelta

DB = "~/Jarvis/Database/v2/trading_forex.db"


def get_pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def fetch_path(pair, entry_time, exit_time):
    oc = OandaClient()
    try:
        ft = isoparse(entry_time)
        tt = isoparse(exit_time)
        candles = oc.get_candles(pair, granularity="M15", from_time=ft, to_time=tt, count=200)
    except Exception:
        return None
    if not candles:
        return None
    bars = []
    for c in candles:
        try:
            close = float(c.get("mid", {}).get("c", c.get("close", 0)))
            high = float(c.get("mid", {}).get("h", c.get("high", 0)))
            low = float(c.get("mid", {}).get("l", c.get("low", 0)))
            if not close: continue
            bars.append({"time": c.get("time", ""), "close": close, "high": high, "low": low})
        except Exception:
            continue
    return bars


def simulate(bars, entry_price, sl_price, direction, pip, mode="wick"):
    """Walk bars. Mode determines trigger:
       - 'wick': fire on M15 adverse extreme touching sl_price (original)
       - 'close': fire only on M15 close past sl_price
       - 'close_mfe_gate': fire on M15 close past sl_price AND peak_pnl < 3p
    Returns: {'fired': bool, 'fire_bar': int|None, 'exit_pnl_pips': float|None}"""
    is_long = direction.lower() in ("buy", "long")
    if not sl_price:
        return {"fired": False, "fire_bar": None, "exit_pnl_pips": None}
    peak_pnl = -999
    for i, b in enumerate(bars):
        close_pnl = ((b["close"] - entry_price) if is_long else (entry_price - b["close"])) / pip
        peak_pnl = max(peak_pnl, close_pnl)

        if mode == "wick":
            hit = (b["low"] <= sl_price) if is_long else (b["high"] >= sl_price)
            exit_at = sl_price
        elif mode == "close":
            # Close-based: fire only if bar CLOSE is past sl_price (no wick triggers)
            hit = (b["close"] <= sl_price) if is_long else (b["close"] >= sl_price)
            exit_at = b["close"]  # exit at actual close, not at SL price
        elif mode == "close_mfe_gate":
            hit = ((b["close"] <= sl_price) if is_long else (b["close"] >= sl_price)) and peak_pnl < 3.0
            exit_at = b["close"]
        else:
            raise ValueError(mode)

        if hit:
            sl_pnl = ((exit_at - entry_price) if is_long else (entry_price - exit_at)) / pip
            return {"fired": True, "fire_bar": i, "exit_pnl_pips": sl_pnl, "peak_pnl_at_fire": peak_pnl}
    return {"fired": False, "fire_bar": None, "exit_pnl_pips": None, "peak_pnl_at_fire": peak_pnl}


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, entry_price, sl_price, entry_time, exit_time, pnl_pips, source,
               max_favorable_excursion_pips as mfe, max_adverse_excursion_pips as mae
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= datetime('now','-30 days')
          AND pnl_pips IS NOT NULL
          AND entry_price IS NOT NULL
          AND sl_price IS NOT NULL
        ORDER BY entry_time ASC
    """).fetchall()
    print(f"Backtest planned_sl_enforcement on {len(trades)} trades (snipe+scout+manual)")
    print()

    results = []
    for i, t in enumerate(trades):
        if i % 25 == 0: print(f"  {i}/{len(trades)}...")
        bars = fetch_path(t["pair"], t["entry_time"], t["exit_time"])
        if not bars: continue
        pip = get_pip_size(t["pair"])
        sim = simulate(bars, float(t["entry_price"]), float(t["sl_price"]),
                       t["direction"], pip)
        actual = float(t["pnl_pips"])
        # If rule fires, effective_pnl = exit_pnl_pips (which equals -planned_sl_distance)
        # Otherwise = actual
        effective = sim["exit_pnl_pips"] if sim["fired"] else actual
        results.append({
            "id": t["id"], "source": t["source"], "pair": t["pair"],
            "actual": actual, "effective": effective,
            "rule_fired": sim["fired"], "fire_bar": sim.get("fire_bar"),
            "mfe": t["mfe"], "mae": t["mae"],
            "delta": effective - actual,
            "is_winner": actual > 0,
            "planned_sl_distance_p": abs(float(t["entry_price"]) - float(t["sl_price"])) / pip,
        })
    print(f"  Done — {len(results)} usable.")
    print()

    # Aggregate by source
    print("=== Planned SL enforcement rule — per source ===")
    print(f"  Assumes: on each M15 bar, if adverse extreme touches DB.sl_price, exit at sl_price.")
    print()
    for src in ("snipe_direct", "scout", "manual", "ALL"):
        grp = results if src == "ALL" else [r for r in results if r["source"] == src]
        if not grp: continue
        fires = [r for r in grp if r["rule_fired"]]
        winners = [r for r in grp if r["is_winner"]]
        losers = [r for r in grp if not r["is_winner"]]
        helped = [r for r in fires if r["delta"] > 0]
        hurt = [r for r in fires if r["delta"] < 0]
        pip_saved = sum(r["delta"] for r in helped)
        pip_lost = sum(-r["delta"] for r in hurt)
        net = pip_saved - pip_lost
        print(f"  [{src:14s}] n={len(grp):3d}  fires={len(fires):3d}  "
              f"helped={len(helped):3d} (+{pip_saved:6.1f}p)  hurt={len(hurt):3d} (-{pip_lost:5.1f}p)  "
              f"NET={net:+7.1f}p")

    # Per-trade winner-kill analysis
    print()
    print("=== Winners killed by the rule (would have hit planned SL while ultimately a winner) ===")
    killed_winners = sorted([r for r in results if r["rule_fired"] and r["is_winner"]],
                            key=lambda r: -r["actual"])
    for r in killed_winners[:15]:
        print(f"  #{r['id']:6s} {r['pair']:8s} {r['source']:14s} "
              f"sl_dist={r['planned_sl_distance_p']:5.1f}p  actual={r['actual']:+6.1f}p → "
              f"would_close_at={r['effective']:+6.1f}p (delta={r['delta']:+6.1f}p)  MFE={r['mfe']}p  MAE={r['mae']}p")
    print(f"  ...{max(0,len(killed_winners)-15)} more...")

    # Top losses prevented
    print()
    print("=== Top losses prevented ===")
    saves = sorted([r for r in results if r["rule_fired"] and not r["is_winner"]],
                   key=lambda r: r["actual"])
    for r in saves[:15]:
        print(f"  #{r['id']:6s} {r['pair']:8s} {r['source']:14s} "
              f"sl_dist={r['planned_sl_distance_p']:5.1f}p  actual={r['actual']:+6.1f}p → "
              f"would_close_at={r['effective']:+6.1f}p (saved={r['delta']:+6.1f}p)  MFE={r['mfe']}p  MAE={r['mae']}p")


if __name__ == "__main__":
    main()
