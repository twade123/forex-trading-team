"""
Re-analysis: take the same 181 fan_exhaustion-blocked snipes and bucket them
by FAN GEOMETRY features (not pair), to find which feature actually separates
winners from losers within the "stable" / "contracting" buckets.

Features pulled per cycle from the surrounding flight_log stages:
  - fan_direction vs snipe direction (aligned?)
  - sep_peak vs sep_now ratio (fan at peak = mature, fresh = young)
  - E21-E55-E100 full-fan separation in pips
  - price distance from E21 in ATRs
  - bb_width_pips
  - bars_since_cross (if available)
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

def pip_size(p): return 0.01 if p.endswith("_JPY") else 0.0001


def fetch_candles(pair, t_from, t_to, gran="M15"):
    try:
        r = requests.get(f"{BASE}/v3/instruments/{pair}/candles", headers=H, params={
            "granularity": gran,
            "from": t_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to":   t_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price": "M",
        }, timeout=15)
        return [c for c in r.json().get("candles", []) if c.get("complete")]
    except Exception:
        return []


def compute_atr_pips(candles, pair):
    pip = pip_size(pair)
    if len(candles) < ATR_PERIOD + 1: return None
    trs, pc = [], float(candles[0]["mid"]["c"])
    for c in candles[1:]:
        h, l, cl = float(c["mid"]["h"]), float(c["mid"]["l"]), float(c["mid"]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        pc = cl
    return mean(trs[-ATR_PERIOD:]) / pip if trs else None


def simulate(entry_candle, fwd, direction, atr_pips, pair):
    pip = pip_size(pair)
    e = float(entry_candle["mid"]["c"])
    sl_d, tp_d = atr_pips * SL_ATR_MULT * pip, atr_pips * TP_ATR_MULT * pip
    if direction == "SELL":
        sl, tp = e + sl_d, e - tp_d
    else:
        sl, tp = e - sl_d, e + tp_d
    for i, c in enumerate(fwd):
        h, l = float(c["mid"]["h"]), float(c["mid"]["l"])
        if direction == "SELL":
            if h >= sl: return "loss", -atr_pips * SL_ATR_MULT
            if l <= tp: return "win", atr_pips * TP_ATR_MULT
        else:
            if l <= sl: return "loss", -atr_pips * SL_ATR_MULT
            if h >= tp: return "win", atr_pips * TP_ATR_MULT
    if fwd:
        last = float(fwd[-1]["mid"]["c"])
        return "timeout", (e - last) / pip if direction == "SELL" else (last - e) / pip
    return "nodata", 0.0


def get_pair(wid, _c={}):
    if wid in _c: return _c[wid]
    with sqlite3.connect(WATCH_DB, timeout=5) as wc:
        r = wc.execute("SELECT instrument FROM watch_suggestions WHERE id=?", (wid,)).fetchone()
    _c[wid] = r[0] if r else None
    return _c[wid]


def gather_cycle_context(fc, watch_id, block_ts):
    """Pull all SNIPE_GATE_PASSED / SNIPE_DIRECT_START stages within 30 sec before this block."""
    cutoff = (datetime.fromisoformat(block_ts.replace("Z", "+00:00")) - timedelta(seconds=30)).isoformat()
    rows = fc.execute(
        "SELECT stage, data FROM flight_log WHERE timestamp >= ? AND timestamp <= ? "
        "AND (stage='SNIPE_DIRECT_START' OR stage='SNIPE_GATE_PASSED') "
        "AND data LIKE ? ORDER BY timestamp",
        (cutoff, block_ts, f'%"watch_id": {watch_id}%')
    ).fetchall()
    ctx = {}
    for stage, raw in rows:
        try: d = json.loads(raw)
        except Exception: continue
        gate = d.get("gate", "")
        if stage == "SNIPE_DIRECT_START":
            ctx["direction"] = d.get("direction")
            ctx["fan_state_start"] = d.get("fan_state")
            ctx["fan_dir_start"] = d.get("fan_direction")
        elif gate == "direction_aligned":
            ctx["fan_dir"] = d.get("fan_dir")
            ctx["fan_state"] = d.get("fan_state")
        elif gate == "ema_ordering":
            ctx["e21"] = d.get("e21"); ctx["e55"] = d.get("e55"); ctx["e100"] = d.get("e100")
        elif gate == "validator_fan_alignment":
            ctx["sep_peak"] = d.get("fan_sep_peak"); ctx["sep_now"] = d.get("fan_sep_now")
        elif gate == "ema21_position":
            ctx["price"] = d.get("price"); ctx["ema21"] = d.get("ema21"); ctx["dist_atr_e21"] = d.get("dist_atr")
        elif gate == "bb_width":
            ctx["bb_width_pips"] = d.get("bb_width_pips")
        elif gate == "momentum_trap":
            ctx["rsi"] = d.get("rsi"); ctx["stoch"] = d.get("stoch")
        elif gate == "oscillator_freshness":
            ctx["stoch_jump"] = d.get("stoch_jump")
    return ctx


def main():
    print(f"Re-analyzing fan_exhaustion blocks by FAN GEOMETRY (not pair)")
    print(f"Window: last {WINDOW_DAYS}d, fwd {LOOK_FORWARD_HOURS}h, SL/TP {SL_ATR_MULT}/{TP_ATR_MULT}*ATR")
    print("=" * 90)

    trades = []
    with sqlite3.connect(FLIGHT_DB, timeout=5) as fc:
        blocks = fc.execute(
            "SELECT timestamp, data FROM flight_log "
            "WHERE stage='SNIPE_GATE_BLOCKED' AND data LIKE '%fan_exhaust%' "
            "AND timestamp > datetime('now', ?) ORDER BY timestamp",
            (f'-{WINDOW_DAYS} days',)
        ).fetchall()

        for block_ts, raw in blocks:
            try: d = json.loads(raw)
            except Exception: continue
            wid = d.get("watch_id"); fan_state = d.get("fan_state")
            if not wid: continue
            pair = get_pair(wid)
            if not pair: continue
            ctx = gather_cycle_context(fc, wid, block_ts)
            direction = ctx.get("direction")
            if not direction: continue

            t = datetime.fromisoformat(block_ts.replace("Z", "+00:00"))
            back = fetch_candles(pair, t - timedelta(hours=12), t)
            if len(back) < ATR_PERIOD + 2: continue
            atr = compute_atr_pips(back, pair)
            if not atr: continue
            fwd = fetch_candles(pair, t, t + timedelta(hours=LOOK_FORWARD_HOURS))
            if not fwd: continue

            outcome, pips = simulate(back[-1], fwd, direction, atr, pair)

            # Derived geometry
            pip = pip_size(pair)
            full_fan_pips = None
            e21, e55, e100 = ctx.get("e21"), ctx.get("e55"), ctx.get("e100")
            if e21 and e55 and e100:
                full_fan_pips = max(e21, e55, e100) - min(e21, e55, e100)
                full_fan_pips /= pip
            fan_dir_aligned = None
            if ctx.get("fan_dir"):
                snipe_dir = direction
                fan_dir_aligned = (snipe_dir == "BUY" and ctx["fan_dir"] == "bullish") or \
                                  (snipe_dir == "SELL" and ctx["fan_dir"] == "bearish")
            sep_decay = None
            sp, sn = ctx.get("sep_peak"), ctx.get("sep_now")
            if sp and sn and sp > 0:
                sep_decay = sn / sp  # 1.0 = at peak, lower = fan shrinking from peak

            trades.append({
                "ts": block_ts, "pair": pair, "dir": direction, "fan_state": fan_state,
                "outcome": outcome, "pips": round(pips, 1),
                "fan_dir_aligned": fan_dir_aligned,
                "full_fan_pips": round(full_fan_pips, 1) if full_fan_pips else None,
                "dist_atr_e21": ctx.get("dist_atr_e21"),
                "bb_width_pips": ctx.get("bb_width_pips"),
                "sep_decay": round(sep_decay, 2) if sep_decay else None,
                "rsi": ctx.get("rsi"), "stoch": ctx.get("stoch"),
                "atr_pips": round(atr, 1),
            })

    print(f"\nTotal replayed with geometry: {len(trades)}")
    stable = [t for t in trades if t["fan_state"] == "stable"]
    contracting = [t for t in trades if t["fan_state"] == "contracting"]

    def bucket_summary(name, ts, group_key, get_label):
        if not ts: return
        print(f"\n{name} — bucketed by {group_key}:")
        buckets = defaultdict(list)
        for t in ts:
            buckets[get_label(t)].append(t)
        print(f"{'BUCKET':<28}{'N':>4}{'WIN%':>7}{'NET_PIPS':>10}{'AVG_PIP':>9}")
        for k in sorted(buckets.keys(), key=lambda x: -len(buckets[x])):
            grp = buckets[k]
            wins = sum(1 for t in grp if t["outcome"] == "win")
            net = sum(t["pips"] for t in grp)
            wr = 100 * wins / len(grp)
            print(f"{str(k):<28}{len(grp):>4}{wr:>6.1f}%{net:>10.1f}{net/len(grp):>9.1f}")

    # 1. Fan direction alignment with snipe direction
    bucket_summary("STABLE", stable, "fan_dir_aligned",
        lambda t: "aligned" if t["fan_dir_aligned"] is True else ("OPPOSITE" if t["fan_dir_aligned"] is False else "mixed/unknown"))

    # 2. Full fan separation magnitude (wide cruise vs narrow / forming)
    def fan_width_bucket(t):
        f = t["full_fan_pips"]
        if f is None: return "?"
        if f < 6: return "narrow_<6p"
        if f < 12: return "mid_6-12p"
        if f < 20: return "wide_12-20p"
        return "very_wide_>=20p"
    bucket_summary("STABLE", stable, "full_fan_separation", fan_width_bucket)

    # 3. sep_decay — is fan at peak or already off peak
    def sep_decay_bucket(t):
        s = t["sep_decay"]
        if s is None: return "?"
        if s >= 0.97: return "at_peak_>=97%"
        if s >= 0.85: return "near_peak_85-97%"
        if s >= 0.70: return "decaying_70-85%"
        return "well_off_peak_<70%"
    bucket_summary("STABLE", stable, "sep_decay_from_peak", sep_decay_bucket)

    # 4. Price distance from E21 in ATRs (retest depth)
    def dist_bucket(t):
        d = t["dist_atr_e21"]
        if d is None: return "?"
        if d < 0.5: return "at_e21_<0.5atr"
        if d < 1.5: return "near_e21_0.5-1.5"
        if d < 2.5: return "stretched_1.5-2.5"
        return "far_>=2.5atr"
    bucket_summary("STABLE", stable, "dist_from_e21_in_ATR", dist_bucket)

    # 5. BB width
    def bb_bucket(t):
        b = t["bb_width_pips"]
        if b is None: return "?"
        if b < 8: return "compressed_<8p"
        if b < 15: return "normal_8-15p"
        if b < 25: return "wide_15-25p"
        return "very_wide_>=25p"
    bucket_summary("STABLE", stable, "bb_width", bb_bucket)

    # 6. Combined: fan_aligned + wide fan (the "open and continuing" pattern Tim described)
    print("\n" + "=" * 90)
    print("COMBINED FILTER: stable + aligned + full_fan>=12p (Tim's 'open and continuing' theory):")
    combo = [t for t in stable if t["fan_dir_aligned"] is True and t["full_fan_pips"] and t["full_fan_pips"] >= 12]
    rest = [t for t in stable if not (t["fan_dir_aligned"] is True and t["full_fan_pips"] and t["full_fan_pips"] >= 12)]
    if combo:
        wins = sum(1 for t in combo if t["outcome"] == "win")
        net = sum(t["pips"] for t in combo)
        print(f"  MATCH (aligned, wide fan):  N={len(combo)}  WR={100*wins/len(combo):.1f}%  net={net:+.1f}p  avg={net/len(combo):+.1f}p")
    if rest:
        wins = sum(1 for t in rest if t["outcome"] == "win")
        net = sum(t["pips"] for t in rest)
        print(f"  REST:                       N={len(rest)}  WR={100*wins/len(rest):.1f}%  net={net:+.1f}p  avg={net/len(rest):+.1f}p")

    # 7. Pure aligned-vs-not within stable (Tim's primary hypothesis)
    print("\n" + "=" * 90)
    print("PURE ALIGNMENT TEST within stable:")
    aligned = [t for t in stable if t["fan_dir_aligned"] is True]
    opp = [t for t in stable if t["fan_dir_aligned"] is False]
    if aligned:
        wins = sum(1 for t in aligned if t["outcome"] == "win")
        net = sum(t["pips"] for t in aligned)
        print(f"  fan ALIGNED with snipe dir:  N={len(aligned)}  WR={100*wins/len(aligned):.1f}%  net={net:+.1f}p")
    if opp:
        wins = sum(1 for t in opp if t["outcome"] == "win")
        net = sum(t["pips"] for t in opp)
        print(f"  fan OPPOSITE snipe dir:      N={len(opp)}  WR={100*wins/len(opp):.1f}%  net={net:+.1f}p")

    out = "/tmp/fan_geometry_replay.json"
    with open(out, "w") as f:
        json.dump(trades, f, indent=2, default=str)
    print(f"\nRaw geometry-tagged trades: {out}")


if __name__ == "__main__":
    main()
