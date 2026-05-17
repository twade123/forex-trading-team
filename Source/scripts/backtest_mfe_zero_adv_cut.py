"""backtest_mfe_zero_adv_cut.py — MFE=0 real-time guardian rule.

Rule (Tim's spec 2026-05-15):
  Guardian rule that fires by bar N (1..MAX_BARS) when ALL of:
    A. MFE so far == 0 (trade has NEVER gone positive)
    B. MAE so far >= MAE_THRESHOLD pips (real adverse move)

Action: close at market (current pnl).

KEY INSIGHT: every WINNER has MFE > 0 by definition. So this rule CANNOT kill
a winner — it's mathematically restricted to trades that lost. The only question
is whether it catches losers EARLIER than they would have otherwise lost.

For each trade (last 30 days, snipe+scout+manual):
  1. Fetch candles from entry to exit
  2. Walk bar-by-bar tracking MFE and MAE
  3. If by bar N <= MAX_BARS: MFE == 0 AND MAE >= MAE_THRESHOLD → simulate exit
  4. Compare exit pnl to actual outcome

Sweeps:
  MAE_THRESHOLD: 5, 7, 10, 12 pips
  MAX_BARS:      3, 4, 6, 8
"""
import os, sqlite3, sys
SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient
from dateutil.parser import isoparse
from datetime import timedelta

DB = "~/Jarvis/Database/v2/trading_forex.db"

def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def fetch_path(pair, entry_time, exit_time):
    oc = OandaClient()
    try:
        ft = isoparse(entry_time); tt = isoparse(exit_time)
        candles = oc.get_candles(pair, "M15", from_time=ft, to_time=tt, count=200)
    except Exception:
        return None
    if not candles: return None
    out = []
    for c in candles:
        mid = c.get("mid", {})
        close = float(mid.get("c", 0))
        if not close: continue
        out.append({"high": float(mid.get("h",0)), "low": float(mid.get("l",0)), "close": close})
    return out if out else None


def simulate(bars, entry_price, direction, pip, mae_thresh, max_bars):
    """Walk M15 bars, track MFE/MAE. If by bar <= max_bars MFE=0 + MAE>=thresh, fire."""
    is_long = direction.lower() in ("buy", "long")
    mfe = 0.0  # max favorable (in pips, always positive)
    mae = 0.0  # max adverse (in pips, always positive)
    for i, b in enumerate(bars):
        # Compute bar's high/low pnl for tracking MFE/MAE
        if is_long:
            high_pnl = (b["high"] - entry_price) / pip   # favorable
            low_pnl = (b["low"] - entry_price) / pip     # adverse (negative when below entry)
            mfe = max(mfe, high_pnl)
            mae = max(mae, -low_pnl)
        else:
            high_pnl = (entry_price - b["low"]) / pip
            low_pnl = (entry_price - b["high"]) / pip
            mfe = max(mfe, high_pnl)
            mae = max(mae, -low_pnl)

        # Check rule
        if i < max_bars and mfe <= 0.0 and mae >= mae_thresh:
            # Fire — exit at current bar's close pnl
            close_pnl = ((b["close"] - entry_price) if is_long else (entry_price - b["close"])) / pip
            return {"fired": True, "fire_bar": i, "fire_pnl": close_pnl, "mfe_at_fire": mfe, "mae_at_fire": mae}

    return {"fired": False, "fire_bar": None, "fire_pnl": None, "mfe_at_fire": mfe, "mae_at_fire": mae}


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, source, entry_price, entry_time, exit_time, pnl_pips,
               max_favorable_excursion_pips as mfe_db,
               max_adverse_excursion_pips as mae_db
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= datetime('now','-30 days')
          AND pnl_pips IS NOT NULL
          AND entry_price IS NOT NULL
        ORDER BY entry_time DESC
    """).fetchall()
    print(f"Backtest MFE=0 adv-cut rule on {len(trades)} trades (last 30d)")
    print()

    # Cache paths
    print("Fetching candle paths per trade...")
    cache = {}
    for i, t in enumerate(trades):
        if i % 25 == 0: print(f"  {i}/{len(trades)}...")
        bars = fetch_path(t["pair"], t["entry_time"], t["exit_time"])
        if bars: cache[t["id"]] = bars
    print(f"  Done — {len(cache)}/{len(trades)} usable.")
    print()

    # Sweep
    for max_bars in (3, 4, 6, 8):
        for mae_thresh in (5, 7, 10, 12):
            print(f"═══ MAE_THRESHOLD={mae_thresh}p  MAX_BARS={max_bars} ═══")
            fires = 0; helped = []; hurt = []; winners_killed = 0
            for t in trades:
                bars = cache.get(t["id"])
                if not bars: continue
                pip = pip_size(t["pair"])
                actual = float(t["pnl_pips"])
                sim = simulate(bars, float(t["entry_price"]), t["direction"], pip, mae_thresh, max_bars)
                if sim["fired"]:
                    fires += 1
                    fire_pnl = sim["fire_pnl"]
                    delta = fire_pnl - actual
                    if actual > 0:
                        winners_killed += 1
                        # Shouldn't happen given MFE=0 gate, but track for sanity
                        hurt.append((t, sim, actual, fire_pnl, delta))
                    elif delta > 0:
                        helped.append((t, sim, actual, fire_pnl, delta))
                    else:
                        hurt.append((t, sim, actual, fire_pnl, delta))

            pip_saved = sum(d for *_, d in helped)
            pip_lost = sum(-d for *_, d in hurt)
            net = pip_saved - pip_lost
            print(f"  fires={fires}  helped={len(helped)} (+{pip_saved:.1f}p)  hurt={len(hurt)} (-{pip_lost:.1f}p)  "
                  f"winners_killed={winners_killed}  NET={net:+.1f}p")

    # Detailed list at the most promising setting
    print()
    print("═══ DETAIL: MAE=10p MAX_BARS=4 ═══")
    helped_list = []
    for t in trades:
        bars = cache.get(t["id"])
        if not bars: continue
        pip = pip_size(t["pair"])
        sim = simulate(bars, float(t["entry_price"]), t["direction"], pip, 10, 4)
        if sim["fired"]:
            actual = float(t["pnl_pips"])
            helped_list.append((t, sim, actual))
    print(f"Caught {len(helped_list)} trades:")
    for t, sim, actual in sorted(helped_list, key=lambda x: x[2]):
        savings = sim["fire_pnl"] - actual
        print(f"  #{t['id']:<6s} {t['pair']:<8s} {t['direction']:<4s} {t['source']:<14s} "
              f"actual={actual:+6.1f}p  fire_at_bar={sim['fire_bar']}  fire_pnl={sim['fire_pnl']:+6.1f}p  "
              f"saved={savings:+6.1f}p  MAE@fire={sim['mae_at_fire']:.1f}p")

if __name__ == "__main__":
    main()
