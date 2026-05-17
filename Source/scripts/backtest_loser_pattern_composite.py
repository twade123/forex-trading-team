"""backtest_loser_pattern_composite.py — Iterate on MFE=0 with adverse-confirming
signals layered on. Goal: catch losers while reducing winner-kill rate.

For each trade, walk bars and at each bar evaluate:

  CORE:    MFE so far == 0 AND MAE >= MAE_THRESH AND bar_index < MAX_BARS

  ADVERSE-CONFIRMING (need at least N of these true):
    1. monotonic_adverse: last 2 M15 closes both worse than entry (no recovery yet)
    2. adverse_body:     latest M15 bar has |body|/|range| >= 0.5 AND closed adverse
    3. mae_growing:      MAE in last 2 bars > 30% larger than 1 bar ago (still expanding)

Sweeps:
  MAE_THRESH: 5, 7, 10
  MAX_BARS:   3, 4
  N_CONFIRM:  0 (MFE=0 alone), 1, 2, 3
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
        cl = float(mid.get("c", 0))
        if not cl: continue
        out.append({"open": float(mid.get("o",0)), "high": float(mid.get("h",0)),
                     "low": float(mid.get("l",0)), "close": cl})
    return out if out else None


def simulate(bars, entry_price, direction, pip, mae_thresh, max_bars, n_confirm):
    is_long = direction.lower() in ("buy", "long")
    mfe = 0.0
    mae_history = [0.0]  # MAE per bar end
    for i, b in enumerate(bars):
        # MFE/MAE update
        if is_long:
            high_pnl = (b["high"] - entry_price) / pip
            low_pnl = (b["low"] - entry_price) / pip
        else:
            high_pnl = (entry_price - b["low"]) / pip
            low_pnl = (entry_price - b["high"]) / pip
        mfe = max(mfe, high_pnl)
        mae = max(mae_history[-1], -low_pnl)
        mae_history.append(mae)

        # CORE conditions
        if i >= max_bars: break
        if mfe > 0 or mae < mae_thresh: continue

        # ADVERSE-CONFIRMING signals
        n_confirmed = 0

        # 1. monotonic_adverse: last 2 M15 closes both worse than entry
        if i >= 1:
            prev_close_pnl = ((bars[i-1]["close"] - entry_price) if is_long else (entry_price - bars[i-1]["close"])) / pip
            curr_close_pnl = ((b["close"] - entry_price) if is_long else (entry_price - b["close"])) / pip
            if prev_close_pnl < 0 and curr_close_pnl < 0 and curr_close_pnl <= prev_close_pnl:
                n_confirmed += 1

        # 2. adverse_body: latest bar has |body|/|range| >= 0.5 AND closed adverse
        rng = max(b["high"] - b["low"], pip * 0.1)
        body = abs(b["close"] - b["open"])
        bar_adverse = (b["close"] < b["open"] and is_long) or (b["close"] > b["open"] and not is_long)
        if body/rng >= 0.5 and bar_adverse:
            n_confirmed += 1

        # 3. mae_growing: MAE this bar 30%+ bigger than two bars ago
        if i >= 2 and mae_history[i-1] > 0 and mae > mae_history[i-1] * 1.30:
            n_confirmed += 1

        if n_confirmed >= n_confirm:
            close_pnl = ((b["close"] - entry_price) if is_long else (entry_price - b["close"])) / pip
            return {"fired": True, "fire_bar": i, "fire_pnl": close_pnl,
                    "mfe_at_fire": mfe, "mae_at_fire": mae, "n_confirmed": n_confirmed}

    return {"fired": False, "fire_bar": None, "fire_pnl": None,
            "mfe_at_fire": mfe, "mae_at_fire": mae_history[-1], "n_confirmed": 0}


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, pair, direction, source, entry_price, entry_time, exit_time, pnl_pips
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= datetime('now','-30 days')
          AND pnl_pips IS NOT NULL
          AND entry_price IS NOT NULL
        ORDER BY entry_time DESC
    """).fetchall()
    print(f"Composite loser-pattern backtest on {len(trades)} trades (30d)")
    print()

    print("Fetching candle paths...")
    cache = {}
    for i, t in enumerate(trades):
        if i % 25 == 0: print(f"  {i}/{len(trades)}...")
        bars = fetch_path(t["pair"], t["entry_time"], t["exit_time"])
        if bars: cache[t["id"]] = bars
    print(f"  Done — {len(cache)}/{len(trades)} usable.\n")

    print(f"{'MAE':<5s} {'BAR':<4s} {'CONF':<5s} {'fires':<6s} {'helped':<7s} {'hurt':<6s} {'wins_kld':<10s} {'NET':<9s} {'ratio'}")
    print('-'*80)
    for mae_thresh in (5, 7, 10):
        for max_bars in (3, 4):
            for n_confirm in (0, 1, 2, 3):
                fires = 0; helped = []; hurt = []; wkills = 0
                for t in trades:
                    bars = cache.get(t["id"])
                    if not bars: continue
                    pip = pip_size(t["pair"])
                    actual = float(t["pnl_pips"])
                    sim = simulate(bars, float(t["entry_price"]), t["direction"], pip,
                                    mae_thresh, max_bars, n_confirm)
                    if not sim["fired"]: continue
                    fires += 1
                    delta = sim["fire_pnl"] - actual
                    if actual > 0: wkills += 1
                    if delta > 0: helped.append((t, sim, actual, delta))
                    else:         hurt.append((t, sim, actual, delta))
                pip_saved = sum(d for *_, d in helped)
                pip_lost  = sum(-d for *_, d in hurt)
                net = pip_saved - pip_lost
                # ratio: ideal is high helped / low hurt
                ratio = pip_saved / max(pip_lost, 0.1)
                print(f"{mae_thresh:<5d} {max_bars:<4d} {n_confirm:<5d} {fires:<6d} "
                      f"{len(helped):3d}(+{pip_saved:5.1f}) {len(hurt):2d}(-{pip_lost:5.1f}) "
                      f"{wkills:<10d} {net:+7.1f}p  {ratio:.2f}x")

    # Detail at best
    print()
    print("═══ DETAIL: best candidate MAE=5p BARS=3 CONFIRM=2 ═══")
    helped_list = []; hurt_list = []
    for t in trades:
        bars = cache.get(t["id"])
        if not bars: continue
        pip = pip_size(t["pair"])
        actual = float(t["pnl_pips"])
        sim = simulate(bars, float(t["entry_price"]), t["direction"], pip, 5, 3, 2)
        if sim["fired"]:
            delta = sim["fire_pnl"] - actual
            entry = (t, sim, actual, delta)
            (helped_list if delta > 0 else hurt_list).append(entry)
    print(f"\n  helped (loser cut shorter — {len(helped_list)} trades):")
    for t, sim, actual, delta in sorted(helped_list, key=lambda x: x[2]):
        print(f"    #{t['id']:<6s} {t['pair']:<8s} {t['direction']:<4s} actual={actual:+6.1f}p → "
              f"fire@bar={sim['fire_bar']} fire_pnl={sim['fire_pnl']:+6.1f}p saved={delta:+5.1f}p")
    print(f"\n  hurt ({len(hurt_list)} trades — these are the false positives):")
    for t, sim, actual, delta in sorted(hurt_list, key=lambda x: x[3]):
        win_marker = " ← WAS A WINNER" if actual > 0 else ""
        print(f"    #{t['id']:<6s} {t['pair']:<8s} {t['direction']:<4s} actual={actual:+6.1f}p → "
              f"fire@bar={sim['fire_bar']} fire_pnl={sim['fire_pnl']:+6.1f}p delta={delta:+5.1f}p{win_marker}")


if __name__ == "__main__":
    main()
