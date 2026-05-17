"""
For each trade in a chosen cohort (losers / winners / all), walk M15 candles
forward from entry and detect NEW opposing peak_sep markers using the SAME
format_chart_signals() function the live exit_marker rule uses
(backtester/ema_separation.py).

For each marker fire, record:
  - bar_offset (1, 2, 3, ...)
  - pnl at marker fire
  - pnl progression at +1, +3, +5, +10, +20 bars after fire
  - whether the trade ever recovered to flat (>=0) between marker and exit
  - max adverse excursion from marker to exit

Quantifies kill-at-market vs current tighten counterfactual on real outcomes.

Usage:
  python audit_exit_marker_losers.py [--cohort losers|winners|all] [--out PATH]
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from oanda_client import OandaClient, _parse_oanda_time
from backtester.ema_separation import format_chart_signals  # live function

DB_TRADES = "~/Jarvis/Database/v2/trading_forex.db"
DEFAULT_OUT = "/tmp/ghost_v2/audit_exit_marker_{cohort}.json"

# Post-marker progression sample points (bars after first marker fire)
POST_OFFSETS = [1, 3, 5, 10, 20]

JPY_PIP = 0.01
NON_JPY_PIP = 0.0001


def pip_size(pair):
    return JPY_PIP if pair.endswith("_JPY") else NON_JPY_PIP


def parse_dt(s):
    if not s:
        return None
    s2 = s.replace(" ", "T")
    if not s2.endswith("Z") and "+" not in s2.split("T", 1)[-1]:
        s2 = s2 + "Z"
    return _parse_oanda_time(s2)


def fetch_trades(cohort):
    """cohort: 'losers' | 'winners' | 'all'"""
    if cohort == "losers":
        outcome_clause = "AND outcome='loss'"
    elif cohort == "winners":
        outcome_clause = "AND outcome='win'"
    else:
        outcome_clause = ""
    conn = sqlite3.connect(DB_TRADES)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"""
        SELECT id, pair, direction, entry_price, entry_time, exit_time,
               outcome_pips, pnl_pips, max_favorable_excursion_pips,
               max_adverse_excursion_pips, source, outcome
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= '2026-04-16' AND entry_time < '2026-05-16'
          AND status='closed'
          {outcome_clause}
          AND exit_time IS NOT NULL AND entry_price IS NOT NULL
        ORDER BY entry_time
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Backwards-compat alias
def fetch_losers():
    return fetch_trades("losers")


def _pnl_at(close_price, entry, is_long, psize):
    if is_long:
        return (close_price - entry) / psize
    return (entry - close_price) / psize


def analyze_trade(client, trade):
    pair = trade["pair"]
    direction = trade["direction"].lower()
    entry = float(trade["entry_price"])
    is_long = direction in ("buy", "long")
    psize = pip_size(pair)

    et = parse_dt(trade["entry_time"])
    xt = parse_dt(trade["exit_time"])
    if not et or not xt:
        return {"trade_id": str(trade["id"]), "error": "bad_timestamps"}

    # Pad 250 M15 bars (62.5h) before entry — safely covers weekend gap.
    pad_start = et - timedelta(minutes=15 * 250)
    candles = client.fetch_candles_range(
        instrument=pair, granularity="M15",
        from_time=pad_start, to_time=xt, price="M",
    )
    if not candles:
        return {"trade_id": str(trade["id"]), "error": "no_candles"}

    # Build candle list as format_chart_signals expects (open/high/low/close/time)
    cnd = []
    for c in candles:
        if "mid" not in c:
            continue
        m = c["mid"]
        cnd.append({
            "time": c["time"],
            "open": float(m["o"]), "high": float(m["h"]),
            "low": float(m["l"]), "close": float(m["c"]),
        })

    # Find entry bar
    entry_idx = None
    for i, c in enumerate(cnd):
        if parse_dt(c["time"]) >= et:
            entry_idx = i
            break
    # Require 100 bars BEFORE entry for E100 runway (regardless of total length).
    if entry_idx is None or entry_idx < 100:
        return {"trade_id": str(trade["id"]), "error": "no_entry_bar",
                "entry_idx": entry_idx, "total": len(cnd)}

    # Opposing direction = direction OPPOSITE to trade
    oppose = "sell" if is_long else "buy"

    # Baseline markers up to AND INCLUDING entry bar
    baseline_sub = cnd[: entry_idx + 1]
    baseline_sigs = format_chart_signals(baseline_sub) or []
    baseline_times = {
        s.get("time") for s in baseline_sigs
        if s.get("type") == "peak_sep" and s.get("direction") == oppose
    }

    # Walk forward from entry, recompute markers, find first new opposing peak_sep
    marker_bar = None
    for bar in range(1, len(cnd) - entry_idx):
        sub = cnd[: entry_idx + bar + 1]
        sigs = format_chart_signals(sub) or []
        current_oppose = {
            s.get("time") for s in sigs
            if s.get("type") == "peak_sep" and s.get("direction") == oppose
        }
        new_set = current_oppose - baseline_times
        if new_set:
            marker_bar = bar
            break  # only need first

    # Build first-marker payload + post-marker progression
    first_marker = None
    if marker_bar is not None:
        close_price = cnd[entry_idx + marker_bar]["close"]
        pnl_at_mkr = _pnl_at(close_price, entry, is_long, psize)
        bar_time = parse_dt(cnd[entry_idx + marker_bar]["time"])
        min_after_entry = (bar_time - et).total_seconds() / 60

        # Post-marker bar-by-bar progression
        post = {}
        for off in POST_OFFSETS:
            tgt = entry_idx + marker_bar + off
            if tgt < len(cnd):
                p = _pnl_at(cnd[tgt]["close"], entry, is_long, psize)
                post[f"bar_plus_{off}"] = round(p, 1)
            else:
                post[f"bar_plus_{off}"] = None

        # Did the trade ever recover to >=0 between marker and exit?
        ever_recovered_flat = False
        max_adverse_after = pnl_at_mkr  # tracks worst pnl
        max_favorable_after = pnl_at_mkr  # tracks best pnl after marker
        for i in range(entry_idx + marker_bar, len(cnd)):
            # check both high and low (intra-bar)
            hi = cnd[i]["high"]
            lo = cnd[i]["low"]
            best_pnl = _pnl_at(hi if is_long else lo, entry, is_long, psize)
            worst_pnl = _pnl_at(lo if is_long else hi, entry, is_long, psize)
            if best_pnl >= 0:
                ever_recovered_flat = True
            if worst_pnl < max_adverse_after:
                max_adverse_after = worst_pnl
            if best_pnl > max_favorable_after:
                max_favorable_after = best_pnl

        first_marker = {
            "bar_offset": marker_bar,
            "minutes_after_entry": round(min_after_entry, 1),
            "pnl_pips_at_marker": round(pnl_at_mkr, 1),
            "post_marker_pnl": post,
            "ever_recovered_to_flat_after_marker": ever_recovered_flat,
            "max_adverse_after_marker_pips": round(max_adverse_after, 1),
            "max_favorable_after_marker_pips": round(max_favorable_after, 1),
        }

    # Final outcome: prefer outcome_pips, fallback to pnl_pips (DB has nulls on either)
    outcome = trade.get("outcome_pips")
    if outcome is None:
        outcome = trade.get("pnl_pips")
    outcome_f = round(float(outcome), 1) if outcome is not None else None

    mfe = trade.get("max_favorable_excursion_pips")
    mae = trade.get("max_adverse_excursion_pips")

    return {
        "trade_id": str(trade["id"]),
        "pair": pair,
        "direction": direction,
        "source": trade["source"],
        "outcome": trade.get("outcome"),
        "actual_outcome_pips": outcome_f,
        "actual_mfe_pips": round(float(mfe), 1) if mfe is not None else None,
        "actual_mae_pips": round(float(mae), 1) if mae is not None else None,
        "baseline_opposing_markers": len(baseline_times),
        "entry_bar_idx": entry_idx,
        "total_bars_in_trade": len(cnd) - entry_idx - 1,
        "first_new_opposing_marker": first_marker,
    }


# Backwards-compat alias
def analyze_loser(client, loser):
    return analyze_trade(client, loser)


def summarize(results, cohort_label):
    valid = [r for r in results if "first_new_opposing_marker" in r]
    fired = [r for r in valid if r["first_new_opposing_marker"]]
    never_fired = [r for r in valid if not r["first_new_opposing_marker"]]
    errors = [r for r in results if "error" in r]

    print(f"\n=== EXIT-MARKER AUDIT ON {len(valid)} {cohort_label.upper()} (errors: {len(errors)}) ===")
    if not valid:
        return
    pct_fire = len(fired) * 100 / len(valid)
    print(f"Opposing peak_sep marker appeared after entry: {len(fired)} ({pct_fire:.0f}%)")
    print(f"No marker fired during trade:                  {len(never_fired)}")

    if not fired:
        return

    # ---- State-at-fire ------------------------------------------------
    profitable = [r for r in fired if r["first_new_opposing_marker"]["pnl_pips_at_marker"] > 2]
    near_be = [r for r in fired if -3 <= r["first_new_opposing_marker"]["pnl_pips_at_marker"] <= 2]
    deep_loss = [r for r in fired if r["first_new_opposing_marker"]["pnl_pips_at_marker"] < -3]

    def _avg_final(sub):
        outs = [r["actual_outcome_pips"] for r in sub if r["actual_outcome_pips"] is not None]
        return sum(outs) / len(outs) if outs else 0.0

    print(f"\nState at marker fire:")
    print(f"  Profitable (>+2p):  {len(profitable):>3}  avg final {_avg_final(profitable):+.1f}p")
    print(f"  Near BE (-3..+2p):  {len(near_be):>3}  avg final {_avg_final(near_be):+.1f}p")
    print(f"  Deep loss (<-3p):   {len(deep_loss):>3}  avg final {_avg_final(deep_loss):+.1f}p")

    # ---- Recovery after marker ----------------------------------------
    recov = [r for r in fired if r["first_new_opposing_marker"].get("ever_recovered_to_flat_after_marker")]
    print(f"\nRecovered to flat (>=0) any time AFTER marker fire: {len(recov)} / {len(fired)} ({100*len(recov)/len(fired):.0f}%)")
    if recov:
        # Of those that recovered, what was their final outcome?
        final_after_recov = _avg_final(recov)
        print(f"  -> of those that touched flat, avg final outcome: {final_after_recov:+.1f}p")

    # ---- Post-marker progression --------------------------------------
    print(f"\nPost-marker price progression (avg pnl across all fires, n={len(fired)}):")
    print(f"  {'@marker':>10}  {'+1bar':>8}  {'+3bar':>8}  {'+5bar':>8}  {'+10bar':>8}  {'+20bar':>8}  {'final':>8}")
    avg_at = sum(r["first_new_opposing_marker"]["pnl_pips_at_marker"] for r in fired) / len(fired)
    def _avg_post(off):
        vals = [r["first_new_opposing_marker"]["post_marker_pnl"].get(f"bar_plus_{off}") for r in fired]
        vals = [v for v in vals if v is not None]
        return sum(vals)/len(vals) if vals else 0.0
    avg_finals = _avg_final(fired)
    print(f"  {avg_at:>+9.1f}p  {_avg_post(1):>+7.1f}p  {_avg_post(3):>+7.1f}p  {_avg_post(5):>+7.1f}p  {_avg_post(10):>+7.1f}p  {_avg_post(20):>+7.1f}p  {avg_finals:>+7.1f}p")

    # ---- Kill vs tighten counterfactual -------------------------------
    # Kill-at-market: trade closes at pnl_at_marker
    # Current tighten: SL tightened to current-1p, trade closes at pnl_at_marker - 1p (approx)
    # Compare both to actual final
    delta_kill = 0.0
    delta_tighten = 0.0
    for r in fired:
        mk = r["first_new_opposing_marker"]
        if mk["pnl_pips_at_marker"] >= 0:
            # in-profit fire: leave current rule alone (we only change in-loss)
            continue
        if r["actual_outcome_pips"] is None:
            continue
        actual = r["actual_outcome_pips"]
        kill_outcome = mk["pnl_pips_at_marker"]  # close at market when marker fires
        tighten_outcome = mk["pnl_pips_at_marker"] - 1.0  # tighten to current-1p
        delta_kill += kill_outcome - actual
        delta_tighten += tighten_outcome - actual

    print(f"\nCounterfactual on IN-LOSS marker fires (vs actual outcomes):")
    print(f"  Kill-at-market: {delta_kill:+.1f}p net delta")
    print(f"  Tighten -1p:    {delta_tighten:+.1f}p net delta")
    if delta_kill > delta_tighten:
        print(f"  -> Kill wins by {delta_kill - delta_tighten:+.1f}p")
    else:
        print(f"  -> Tighten wins by {delta_tighten - delta_kill:+.1f}p")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", choices=["losers", "winners", "all"], default="losers")
    ap.add_argument("--out", default=None, help="Output JSON path")
    args = ap.parse_args()

    out_path = args.out or DEFAULT_OUT.format(cohort=args.cohort)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    trades = fetch_trades(args.cohort)
    print(f"Analyzing {len(trades)} {args.cohort} for opposing peak_sep markers...")
    client = OandaClient()
    results = []
    for i, t in enumerate(trades, 1):
        try:
            r = analyze_trade(client, t)
            results.append(r)
            if i % 10 == 0 or i == len(trades):
                fired = sum(1 for x in results if x.get("first_new_opposing_marker"))
                errs = sum(1 for x in results if "error" in x)
                print(f"  [{i}/{len(trades)}] markers found: {fired}, errors: {errs}")
        except Exception as e:
            results.append({"trade_id": str(t["id"]), "error": str(e)})

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    summarize(results, args.cohort)
    print(f"\nResults: {out_path}")


if __name__ == "__main__":
    main()
