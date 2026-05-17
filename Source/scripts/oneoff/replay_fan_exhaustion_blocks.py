"""
One-off replay: simulate the snipes that the fan_exhaustion gate blocked over
the last 14 days. For each block, fetch M15 candles forward and check whether
SL or TP would have been hit first under the live snipe defaults
(SL=2.5*ATR, TP=1.0*ATR). Bucket results by fan_state at block time.

Pure analysis script — no trading, no DB writes, no code changes.

Usage:
  cd ~/Jarvis/"Forex Trading Team"/Source
  source ~/myenv/bin/activate
  python3 scripts/oneoff/replay_fan_exhaustion_blocks.py
"""
import os, sys, json, sqlite3
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config, requests

config._load_from_db()
H = config.get_default_headers()
BASE = config.BASE_URL

FLIGHT_DB = "<repo_root>/Source/flight_recorder.db"
WATCH_DB  = "~/Jarvis/Database/v2/trading_forex.db"

WINDOW_DAYS = 14
LOOK_FORWARD_HOURS = 8
SL_ATR_MULT = 2.5
TP_ATR_MULT = 1.0
ATR_PERIOD = 14
JPY_PIP = 0.01
DEFAULT_PIP = 0.0001

def pip_size(pair):
    return JPY_PIP if pair.endswith("_JPY") else DEFAULT_PIP


def fetch_candles(pair, t_from, t_to, gran="M15"):
    params = {
        "granularity": gran,
        "from": t_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to":   t_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": "M",
    }
    try:
        r = requests.get(f"{BASE}/v3/instruments/{pair}/candles", headers=H, params=params, timeout=15)
        return [c for c in r.json().get("candles", []) if c.get("complete")]
    except Exception as e:
        return []


def compute_atr_pips(candles, pair):
    pip = pip_size(pair)
    if len(candles) < ATR_PERIOD + 1:
        return None
    trs = []
    prev_close = float(candles[0]["mid"]["c"])
    for c in candles[1:]:
        h, l, cl = float(c["mid"]["h"]), float(c["mid"]["l"]), float(c["mid"]["c"])
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = cl
    if not trs:
        return None
    atr = mean(trs[-ATR_PERIOD:])
    return atr / pip


def simulate_outcome(entry_candle, fwd_candles, direction, atr_pips, pair):
    """Open at entry-candle close, SL=2.5*ATR, TP=1.0*ATR. Walk forward.
    Returns (outcome, pips, bars_to_exit) where outcome in {win, loss, timeout}.
    """
    pip = pip_size(pair)
    entry_price = float(entry_candle["mid"]["c"])
    sl_dist = atr_pips * SL_ATR_MULT * pip
    tp_dist = atr_pips * TP_ATR_MULT * pip
    if direction == "SELL":
        sl = entry_price + sl_dist
        tp = entry_price - tp_dist
    else:
        sl = entry_price - sl_dist
        tp = entry_price + tp_dist

    for i, c in enumerate(fwd_candles):
        h, l = float(c["mid"]["h"]), float(c["mid"]["l"])
        if direction == "SELL":
            # Pessimistic: assume SL hit first if both hit on same bar
            if h >= sl and l <= tp:
                return ("loss", -atr_pips * SL_ATR_MULT, i + 1)
            if h >= sl:
                return ("loss", -atr_pips * SL_ATR_MULT, i + 1)
            if l <= tp:
                return ("win", atr_pips * TP_ATR_MULT, i + 1)
        else:
            if h >= tp and l <= sl:
                return ("loss", -atr_pips * SL_ATR_MULT, i + 1)
            if l <= sl:
                return ("loss", -atr_pips * SL_ATR_MULT, i + 1)
            if h >= tp:
                return ("win", atr_pips * TP_ATR_MULT, i + 1)
    # Timed out — mark to market at last close
    if fwd_candles:
        last = float(fwd_candles[-1]["mid"]["c"])
        pnl_pips = (entry_price - last) / pip if direction == "SELL" else (last - entry_price) / pip
        return ("timeout", pnl_pips, len(fwd_candles))
    return ("nodata", 0.0, 0)


def get_pair_for_watch(watch_id, _cache={}):
    if watch_id in _cache:
        return _cache[watch_id]
    with sqlite3.connect(WATCH_DB, timeout=5) as wc:
        row = wc.execute("SELECT instrument FROM watch_suggestions WHERE id=?", (watch_id,)).fetchone()
    pair = row[0] if row else None
    _cache[watch_id] = pair
    return pair


def get_direction_for_cycle(fc, watch_id, block_ts):
    """Find the direction from SNIPE_DIRECT_START for this watch within last 5 min."""
    cutoff = (datetime.fromisoformat(block_ts.replace("Z", "+00:00")) - timedelta(minutes=5)).isoformat()
    row = fc.execute(
        "SELECT data FROM flight_log WHERE stage='SNIPE_DIRECT_START' "
        "AND data LIKE ? AND timestamp >= ? AND timestamp <= ? "
        "ORDER BY timestamp DESC LIMIT 1",
        (f'%"watch_id": {watch_id}%', cutoff, block_ts)
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0]).get("direction")
    except Exception:
        return None


def main():
    print(f"Replay window: last {WINDOW_DAYS} days, forward {LOOK_FORWARD_HOURS}h per block")
    print(f"Trade defaults: SL={SL_ATR_MULT}*ATR, TP={TP_ATR_MULT}*ATR (live snipe config)")
    print("=" * 78)

    results = defaultdict(list)
    failed = defaultdict(int)
    n_total = 0

    with sqlite3.connect(FLIGHT_DB, timeout=5) as fc:
        blocks = fc.execute(
            "SELECT timestamp, data FROM flight_log "
            "WHERE stage='SNIPE_GATE_BLOCKED' AND data LIKE '%fan_exhaust%' "
            "AND timestamp > datetime('now', ?) ORDER BY timestamp",
            (f'-{WINDOW_DAYS} days',)
        ).fetchall()

        print(f"Found {len(blocks)} fan_exhaustion blocks")
        for block_ts, raw in blocks:
            n_total += 1
            try:
                d = json.loads(raw)
            except Exception:
                failed["parse"] += 1; continue
            watch_id = d.get("watch_id")
            fan_state = d.get("fan_state", "unknown")
            if not watch_id:
                failed["no_watch_id"] += 1; continue

            pair = get_pair_for_watch(watch_id)
            if not pair:
                failed["no_pair"] += 1; continue

            direction = get_direction_for_cycle(fc, watch_id, block_ts)
            if not direction:
                failed["no_direction"] += 1; continue

            t_block = datetime.fromisoformat(block_ts.replace("Z", "+00:00"))
            t_lookback = t_block - timedelta(hours=12)
            t_forward  = t_block + timedelta(hours=LOOK_FORWARD_HOURS)

            backward_candles = fetch_candles(pair, t_lookback, t_block)
            if len(backward_candles) < ATR_PERIOD + 2:
                failed["no_history"] += 1; continue
            atr_pips = compute_atr_pips(backward_candles, pair)
            if not atr_pips or atr_pips <= 0:
                failed["bad_atr"] += 1; continue

            forward_candles = fetch_candles(pair, t_block, t_forward)
            if not forward_candles:
                failed["no_forward"] += 1; continue
            # The entry candle is the one whose close happens AT or just before t_block.
            entry_candle = backward_candles[-1]

            outcome, pips, bars = simulate_outcome(entry_candle, forward_candles, direction, atr_pips, pair)
            results[fan_state].append({
                "ts": block_ts, "watch_id": watch_id, "pair": pair, "dir": direction,
                "atr_pips": round(atr_pips, 1), "outcome": outcome, "pips": round(pips, 1), "bars": bars,
            })

    print("\nFailures:")
    for k, v in failed.items():
        print(f"  {k}: {v}")
    print(f"\nReplayed: {sum(len(v) for v in results.values())} / {n_total}")

    print("\n" + "=" * 78)
    print(f"{'FAN STATE':<14}{'N':>4}{'WIN%':>7}{'AVG_PIP':>9}{'WIN_AVG':>9}{'LOSS_AVG':>10}{'NET_PIPS':>10}{'TIMEOUT%':>10}")
    print("-" * 78)
    grand_net = 0.0
    grand_n = 0
    for fan_state in sorted(results.keys(), key=lambda k: -len(results[k])):
        trades = results[fan_state]
        if not trades: continue
        wins = [t for t in trades if t["outcome"] == "win"]
        losses = [t for t in trades if t["outcome"] == "loss"]
        timeouts = [t for t in trades if t["outcome"] == "timeout"]
        wr = 100.0 * len(wins) / len(trades)
        avg = mean(t["pips"] for t in trades)
        win_avg = mean(t["pips"] for t in wins) if wins else 0
        loss_avg = mean(t["pips"] for t in losses) if losses else 0
        net = sum(t["pips"] for t in trades)
        to_pct = 100.0 * len(timeouts) / len(trades)
        grand_net += net
        grand_n += len(trades)
        print(f"{fan_state:<14}{len(trades):>4}{wr:>6.1f}%{avg:>9.1f}{win_avg:>9.1f}{loss_avg:>10.1f}{net:>10.1f}{to_pct:>9.1f}%")
    print("-" * 78)
    if grand_n:
        print(f"{'TOTAL':<14}{grand_n:>4}{'':>7}{'':>9}{'':>9}{'':>10}{grand_net:>10.1f}")

    # Per-pair drill down for the biggest bucket
    if results.get("stable"):
        print("\nStable bucket — per-pair breakdown:")
        by_pair = defaultdict(list)
        for t in results["stable"]:
            by_pair[t["pair"]].append(t)
        print(f"{'PAIR':<10}{'N':>4}{'WIN%':>7}{'NET_PIPS':>10}")
        for p, ts in sorted(by_pair.items(), key=lambda kv: -sum(t["pips"] for t in kv[1])):
            wins = sum(1 for t in ts if t["outcome"] == "win")
            net = sum(t["pips"] for t in ts)
            wr = 100.0 * wins / len(ts)
            print(f"{p:<10}{len(ts):>4}{wr:>6.1f}%{net:>10.1f}")

    # Dump raw to JSON for follow-up analysis
    out_path = "/tmp/fan_exhaustion_replay.json"
    with open(out_path, "w") as f:
        json.dump({k: v for k, v in results.items()}, f, indent=2, default=str)
    print(f"\nRaw results: {out_path}")


if __name__ == "__main__":
    main()
