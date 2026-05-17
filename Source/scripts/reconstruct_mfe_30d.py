"""
Reconstruct MFE for 30d non-kronos cohort from M15 candles.

Reads live_trades for 2026-04-16 to 2026-05-16, fetches M15 candles between
entry_time and exit_time via OandaClient, computes max favorable excursion
in pips, writes JSON output. Does NOT touch the DB unless --backfill is set.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from oanda_client import OandaClient, _parse_oanda_time  # noqa: E402

DB = "~/Jarvis/Database/v2/trading_forex.db"
OUT = "/tmp/ghost_v2/mfe_reconstructed_30d.json"

JPY_PIP = 0.01
NON_JPY_PIP = 0.0001


def pip_size(pair: str) -> float:
    return JPY_PIP if pair.endswith("_JPY") else NON_JPY_PIP


def parse_dt(s: str) -> datetime:
    s = s.replace(" ", "T")
    if not s.endswith("Z") and "+" not in s.split("T", 1)[-1]:
        s = s + "Z"
    return _parse_oanda_time(s)


def fetch_trades(only_missing: bool = True) -> list:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    where_mfe = "AND max_favorable_excursion_pips IS NULL" if only_missing else ""
    rows = conn.execute(f"""
        SELECT id, pair, direction, entry_price, entry_time, exit_time,
               outcome, outcome_pips, max_favorable_excursion_pips, exit_trigger, source
        FROM live_trades
        WHERE source IN ('snipe_direct','scout','manual')
          AND entry_time >= '2026-04-16'
          AND entry_time < '2026-05-16'
          AND status = 'closed'
          AND outcome IN ('win','loss')
          AND exit_time IS NOT NULL
          {where_mfe}
        ORDER BY entry_time
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def compute_mfe(client: OandaClient, trade: dict) -> dict:
    pair = trade["pair"]
    direction = trade["direction"].lower()
    entry = float(trade["entry_price"])
    et = parse_dt(trade["entry_time"])
    xt = parse_dt(trade["exit_time"])
    psize = pip_size(pair)

    candles = client.fetch_candles_range(
        instrument=pair,
        granularity="M15",
        from_time=et,
        to_time=xt,
        price="M",
    )

    if not candles:
        return {"id": trade["id"], "mfe_pips": None, "candle_count": 0, "error": "no_candles"}

    # Favorable = high for BUY, low for SELL
    if direction == "buy":
        peak = max(float(c["mid"]["h"]) for c in candles if "mid" in c)
        mfe_price = peak - entry
    else:
        trough = min(float(c["mid"]["l"]) for c in candles if "mid" in c)
        mfe_price = entry - trough

    mfe_pips = round(mfe_price / psize, 1)
    return {
        "id": trade["id"],
        "pair": pair,
        "direction": direction,
        "outcome": trade["outcome"],
        "realized_pips": round(float(trade["outcome_pips"]), 1) if trade["outcome_pips"] is not None else None,
        "mfe_pips": mfe_pips,
        "giveback_pips": round(mfe_pips - float(trade["outcome_pips"]), 1) if trade["outcome_pips"] is not None else None,
        "candle_count": len(candles),
        "exit_trigger": trade["exit_trigger"],
        "source": trade["source"],
    }


def backfill_db(results: list) -> int:
    """Write reconstructed MFE back to live_trades. Only sets where current is NULL."""
    conn = sqlite3.connect(DB)
    n_updated = 0
    for r in results:
        if r.get("mfe_pips") is None:
            continue
        cur = conn.execute(
            "SELECT max_favorable_excursion_pips FROM live_trades WHERE id = ?",
            (r["id"],),
        ).fetchone()
        if cur is None:
            continue
        existing = cur[0]
        if existing is not None:
            continue  # don't overwrite existing values
        conn.execute(
            "UPDATE live_trades SET max_favorable_excursion_pips = ? WHERE id = ?",
            (r["mfe_pips"], r["id"]),
        )
        n_updated += 1
    conn.commit()
    conn.close()
    return n_updated


def main():
    only_missing = "--all" not in sys.argv
    do_backfill = "--backfill" in sys.argv
    trades = fetch_trades(only_missing=only_missing)
    print(f"Trades to process: {len(trades)} ({'missing MFE only' if only_missing else 'all'})")
    if do_backfill:
        print("BACKFILL MODE: will write reconstructed MFE to live_trades where currently NULL")

    Path(OUT).parent.mkdir(parents=True, exist_ok=True)
    results = []
    client = OandaClient()
    for i, t in enumerate(trades, 1):
        try:
            r = compute_mfe(client, t)
            results.append(r)
            if i % 10 == 0 or i == len(trades):
                print(f"[{i}/{len(trades)}] {t['pair']} {t['direction']} → MFE {r.get('mfe_pips')}p")
        except Exception as e:
            results.append({"id": t["id"], "error": str(e)})
            print(f"[{i}/{len(trades)}] {t['id']} ERROR: {e}")

    with open(OUT, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWritten: {OUT}")

    if do_backfill:
        n = backfill_db(results)
        print(f"BACKFILL: wrote MFE to {n} rows in live_trades (NULL → reconstructed)")

    # Summary
    valid = [r for r in results if r.get("mfe_pips") is not None]
    losers = [r for r in valid if r.get("outcome") == "loss"]
    winners = [r for r in valid if r.get("outcome") == "win"]

    print(f"\n=== SUMMARY ({len(valid)} valid) ===")
    losers_with_pnl = [r for r in losers if r.get("realized_pips") is not None]
    print(f"LOSERS ({len(losers)}):")
    for thresh in [1, 2, 3, 5, 8, 10]:
        n = sum(1 for r in losers_with_pnl if r["mfe_pips"] >= thresh)
        rescuable_mfe = sum(r["mfe_pips"] for r in losers_with_pnl if r["mfe_pips"] >= thresh)
        avoided_loss = sum(r["realized_pips"] for r in losers_with_pnl if r["mfe_pips"] >= thresh)
        print(f"  MFE>={thresh}p: {n} trades, MFE sum {rescuable_mfe:+.1f}p, loss avoided if BE'd: {-avoided_loss:+.1f}p")
    winners_with_pnl = [r for r in winners if r.get("realized_pips") is not None]
    if winners_with_pnl:
        gb = [r["giveback_pips"] for r in winners_with_pnl if r["giveback_pips"] is not None]
        print(f"\nWINNERS ({len(winners_with_pnl)} with realized pnl):")
        print(f"  Avg MFE: {sum(r['mfe_pips'] for r in winners_with_pnl)/len(winners_with_pnl):.1f}p")
        print(f"  Avg realized: {sum(r['realized_pips'] for r in winners_with_pnl)/len(winners_with_pnl):.1f}p")
        print(f"  Avg giveback: {sum(gb)/len(gb):.1f}p, total giveback: {sum(gb):+.1f}p")
        # By peak bucket
        print("  Winners by MFE bucket:")
        for lo, hi in [(0, 3), (3, 5), (5, 8), (8, 12), (12, 20), (20, 999)]:
            sub = [r for r in winners_with_pnl if lo <= r["mfe_pips"] < hi]
            if sub:
                avg_mfe = sum(r["mfe_pips"] for r in sub) / len(sub)
                avg_real = sum(r["realized_pips"] for r in sub) / len(sub)
                avg_gb = sum(r["giveback_pips"] for r in sub) / len(sub)
                print(f"    MFE {lo:>2}-{hi:>2}p: n={len(sub):>3}, avg MFE {avg_mfe:>5.1f}p, avg realized {avg_real:>5.1f}p, avg giveback {avg_gb:>5.1f}p")


if __name__ == "__main__":
    main()
