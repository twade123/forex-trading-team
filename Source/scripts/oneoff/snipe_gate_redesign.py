"""
Snipe gate redesign — test bench.

Goal: take EVERY snipe attempt over the last 14 days (~1,163), regardless of
which gate blocked them, simulate forward outcomes with realistic snipe trail,
then compare three policies:

  POLICY A — current actual decisions (live behavior)
  POLICY B — fan_exhaustion gate disabled (let everything fan-blocked through)
  POLICY C — proposed geometry-based gate (multi-factor)

For each policy, compute: N_traded, WR, total net pips.

The candidate POLICY C blocks snipes when ANY of:
  - sep_decay zone in [0.70, 0.97]    (declining-from-peak danger zone)
  - fan_width >= 12 pips AND aligned   (mature/extended trends in direction)
  - dist_atr_e21 >= 2.5                 (price too far from E21, no retest)
otherwise allows through.

Realistic exit: SL = 2.5*ATR, TP = 1.0*ATR, trail activation at MFE>=0.15*ATR,
trail distance 0.1*ATR behind peak. Approximates the live snipe.* trail config.
"""
import os, sys, json, sqlite3
from datetime import datetime, timedelta
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
TRAIL_ACTIVATION_RR = 0.15      # activate when MFE >= 0.15 * TP distance
TRAIL_ATR_MULT = 0.1            # trail at 0.1 * ATR behind peak
ATR_PERIOD = 14

# Candidate Policy C thresholds
DECAY_BLOCK_LOW = 0.70
DECAY_BLOCK_HIGH = 0.97
WIDE_FAN_PIPS = 12.0
FAR_FROM_E21_ATR = 2.5

def pip_size(p): return 0.01 if p.endswith("_JPY") else 0.0001


def fetch(pair, t_from, t_to, gran="M15"):
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


def simulate_with_trail(entry_candle, fwd, direction, atr_pips, pair):
    """SL=2.5*ATR, TP=1.0*ATR, activate trail at MFE>=0.15*ATR, trail at 0.1*ATR.
    Returns (outcome, pips). outcome ∈ {win, loss, trail_stop, timeout}."""
    pip = pip_size(pair)
    e = float(entry_candle["mid"]["c"])
    sl_init = atr_pips * SL_ATR_MULT * pip
    tp_dist = atr_pips * TP_ATR_MULT * pip
    activation = atr_pips * TP_ATR_MULT * TRAIL_ACTIVATION_RR * pip   # 0.15 * 1.0 * ATR
    trail_dist = atr_pips * TRAIL_ATR_MULT * pip                       # 0.1 * ATR

    if direction == "SELL":
        sl, tp = e + sl_init, e - tp_dist
    else:
        sl, tp = e - sl_init, e + tp_dist

    peak_favorable = 0.0    # in pips favorable
    trail_active = False
    for c in fwd:
        h, l = float(c["mid"]["h"]), float(c["mid"]["l"])
        # Did SL hit first?
        if direction == "SELL":
            if h >= sl:
                kind = "trail_stop" if trail_active else "loss"
                pips = (e - sl) / pip
                return kind, pips
            if l <= tp:
                return "win", atr_pips * TP_ATR_MULT
            # Update peak favorable
            fav = (e - l) / pip
            if fav > peak_favorable:
                peak_favorable = fav
                if peak_favorable * pip >= activation:
                    trail_active = True
                    new_sl = l + trail_dist
                    if new_sl < sl:
                        sl = new_sl
        else:
            if l <= sl:
                kind = "trail_stop" if trail_active else "loss"
                pips = (sl - e) / pip
                return kind, pips
            if h >= tp:
                return "win", atr_pips * TP_ATR_MULT
            fav = (h - e) / pip
            if fav > peak_favorable:
                peak_favorable = fav
                if peak_favorable * pip >= activation:
                    trail_active = True
                    new_sl = h - trail_dist
                    if new_sl > sl:
                        sl = new_sl
    # Timeout — mark to last close
    if fwd:
        last = float(fwd[-1]["mid"]["c"])
        pnl = (e - last) / pip if direction == "SELL" else (last - e) / pip
        return "timeout", pnl
    return "nodata", 0.0


def policy_c_block(t):
    """Candidate gate: returns (block:bool, reason:str)."""
    sd = t.get("sep_decay")
    fw = t.get("full_fan_pips")
    aligned = t.get("fan_dir_aligned")
    dist = t.get("dist_atr_e21")
    if sd is not None and DECAY_BLOCK_LOW <= sd < DECAY_BLOCK_HIGH:
        return True, f"decay_zone({sd:.2f})"
    if fw is not None and fw >= WIDE_FAN_PIPS and aligned is True:
        return True, f"wide_aligned({fw:.1f}p)"
    if dist is not None and dist >= FAR_FROM_E21_ATR:
        return True, f"far_from_e21({dist:.1f}atr)"
    return False, ""


_pair_cache = {}
def get_pair(wid):
    if wid in _pair_cache: return _pair_cache[wid]
    with sqlite3.connect(WATCH_DB, timeout=5) as wc:
        r = wc.execute("SELECT instrument FROM watch_suggestions WHERE id=?", (wid,)).fetchone()
    _pair_cache[wid] = r[0] if r else None
    return _pair_cache[wid]


def collect_attempts():
    """Return list of dicts — one per snipe attempt — with all available geometry + actual outcome."""
    attempts = []
    with sqlite3.connect(FLIGHT_DB, timeout=5) as fc:
        starts = fc.execute(
            "SELECT timestamp, data FROM flight_log WHERE stage='SNIPE_DIRECT_START' "
            "AND timestamp > datetime('now', ?) ORDER BY timestamp",
            (f'-{WINDOW_DAYS} days',)
        ).fetchall()

        for ts, raw in starts:
            try: d = json.loads(raw)
            except Exception: continue
            wid = d.get("watch_id"); direction = d.get("direction")
            if not wid or not direction: continue
            pair = get_pair(wid)
            if not pair: continue

            # Gather all SNIPE stages for this watch within 60 seconds after start
            t_start = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            t_end = (t_start + timedelta(seconds=60)).isoformat()
            stages = fc.execute(
                "SELECT stage, data FROM flight_log WHERE timestamp >= ? AND timestamp <= ? "
                "AND data LIKE ? ORDER BY timestamp",
                (ts, t_end, f'%\"watch_id\": {wid}%')
            ).fetchall()

            ctx = {"actual_outcome": "no_decision"}
            for stage, raw2 in stages:
                try: dd = json.loads(raw2)
                except Exception: continue
                gate = dd.get("gate", "")
                if stage == "SNIPE_GATE_PASSED":
                    if gate == "direction_aligned":
                        ctx["fan_dir"] = dd.get("fan_dir"); ctx["fan_state"] = dd.get("fan_state")
                    elif gate == "ema_ordering":
                        ctx["e21"] = dd.get("e21"); ctx["e55"] = dd.get("e55"); ctx["e100"] = dd.get("e100")
                    elif gate == "validator_fan_alignment":
                        ctx["sep_peak"] = dd.get("fan_sep_peak"); ctx["sep_now"] = dd.get("fan_sep_now")
                    elif gate == "ema21_position":
                        ctx["dist_atr_e21"] = dd.get("dist_atr")
                    elif gate == "bb_width":
                        ctx["bb_width_pips"] = dd.get("bb_width_pips")
                    elif gate == "momentum_trap":
                        ctx["rsi"] = dd.get("rsi"); ctx["stoch"] = dd.get("stoch")
                elif stage == "SNIPE_GATE_BLOCKED":
                    ctx["actual_outcome"] = f"blocked_{gate}"
                elif stage == "SNIPE_ALL_GATES_PASSED":
                    ctx["actual_outcome"] = "passed_all"
                    ctx["atr_pips_live"] = dd.get("atr_pips")

            # Derive geometry
            pip = pip_size(pair)
            full_fan_pips = None
            if ctx.get("e21") and ctx.get("e55") and ctx.get("e100"):
                full_fan_pips = (max(ctx["e21"], ctx["e55"], ctx["e100"]) - min(ctx["e21"], ctx["e55"], ctx["e100"])) / pip
            fan_dir_aligned = None
            if ctx.get("fan_dir"):
                fan_dir_aligned = (direction == "BUY" and ctx["fan_dir"] == "bullish") or \
                                  (direction == "SELL" and ctx["fan_dir"] == "bearish")
            sep_decay = None
            if ctx.get("sep_peak") and ctx.get("sep_now") and ctx["sep_peak"] > 0:
                sep_decay = ctx["sep_now"] / ctx["sep_peak"]

            attempts.append({
                "ts": ts, "watch_id": wid, "pair": pair, "direction": direction,
                "actual_outcome": ctx["actual_outcome"],
                "fan_state": ctx.get("fan_state"),
                "fan_dir": ctx.get("fan_dir"),
                "fan_dir_aligned": fan_dir_aligned,
                "full_fan_pips": round(full_fan_pips, 1) if full_fan_pips else None,
                "sep_decay": round(sep_decay, 3) if sep_decay else None,
                "dist_atr_e21": ctx.get("dist_atr_e21"),
                "bb_width_pips": ctx.get("bb_width_pips"),
                "rsi": ctx.get("rsi"), "stoch": ctx.get("stoch"),
                "atr_pips_live": ctx.get("atr_pips_live"),
            })
    return attempts


def main():
    print(f"Snipe gate redesign — full-cohort test bench")
    print(f"Window: last {WINDOW_DAYS}d  |  fwd: {LOOK_FORWARD_HOURS}h  |  trail: act {TRAIL_ACTIVATION_RR}rr, dist {TRAIL_ATR_MULT}*ATR")
    print("=" * 100)

    attempts = collect_attempts()
    print(f"Total snipe attempts collected: {len(attempts)}")

    # Action distribution
    by_outcome = defaultdict(int)
    for a in attempts: by_outcome[a["actual_outcome"]] += 1
    print("\nActual outcome distribution:")
    for k, v in sorted(by_outcome.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {k:<40}{v}")

    # Simulate each — only those with enough data
    print("\nSimulating forward outcomes for all attempts (this takes ~5-15 min)...")
    sim_results = []
    failed = defaultdict(int)
    for i, a in enumerate(attempts):
        t = datetime.fromisoformat(a["ts"].replace("Z", "+00:00"))
        back = fetch(a["pair"], t - timedelta(hours=12), t)
        if len(back) < ATR_PERIOD + 2:
            failed["no_history"] += 1; continue
        atr = compute_atr_pips(back, a["pair"])
        if not atr:
            failed["bad_atr"] += 1; continue
        fwd = fetch(a["pair"], t, t + timedelta(hours=LOOK_FORWARD_HOURS))
        if not fwd:
            failed["no_forward"] += 1; continue
        outcome, pips = simulate_with_trail(back[-1], fwd, a["direction"], atr, a["pair"])
        a["sim_outcome"] = outcome
        a["sim_pips"] = round(pips, 1)
        a["atr_pips"] = round(atr, 1)
        a["policy_c_block"], a["policy_c_reason"] = policy_c_block(a)
        sim_results.append(a)
        if (i + 1) % 100 == 0:
            print(f"  ... {i+1}/{len(attempts)} processed, {len(sim_results)} simulated")

    print(f"\nSimulated: {len(sim_results)}/{len(attempts)}, failures:")
    for k, v in failed.items(): print(f"  {k}: {v}")

    # =========================================================================
    # POLICY COMPARISONS
    # =========================================================================
    def aggregate(trades, label):
        if not trades:
            return {"label": label, "n": 0, "wr": 0, "net": 0, "avg": 0}
        wins = sum(1 for t in trades if t["sim_outcome"] == "win")
        # In trailing mode, trail_stop can be slightly positive too
        positive = sum(1 for t in trades if t["sim_pips"] > 0)
        net = sum(t["sim_pips"] for t in trades)
        return {"label": label, "n": len(trades), "wr": 100*wins/len(trades),
                "pos_pct": 100*positive/len(trades),
                "net": net, "avg": net/len(trades)}

    def show(rows):
        print(f"\n{'POLICY':<40}{'N':>5}{'WR%':>7}{'POS%':>7}{'NET_PIPS':>10}{'AVG':>8}")
        for r in rows:
            print(f"{r['label']:<40}{r['n']:>5}{r['wr']:>6.1f}%{r.get('pos_pct',0):>6.1f}%{r['net']:>10.1f}{r['avg']:>8.1f}")

    # Policy A: actual live behavior
    policy_a = [t for t in sim_results if t["actual_outcome"] == "passed_all"]
    # Policy B: disable fan_exhaustion only — let through those + already-passed
    policy_b = [t for t in sim_results if t["actual_outcome"] in ("passed_all", "blocked_fan_exhaustion")]
    # Policy C: apply candidate gate — start from passed_all + fan_blocked, remove those policy_c would block
    policy_c_input = [t for t in sim_results if t["actual_outcome"] in ("passed_all", "blocked_fan_exhaustion")]
    policy_c = [t for t in policy_c_input if not t["policy_c_block"]]

    print("\n" + "=" * 100)
    print("POLICY OUTCOMES (simulated pips for those allowed to trade):")
    show([
        aggregate(policy_a, "A — current live (passed_all only)"),
        aggregate(policy_b, "B — fan_exhaustion disabled"),
        aggregate(policy_c, "C — proposed multi-factor gate"),
    ])

    # Show what policy C blocked that policy B let through
    c_blocked_subset = [t for t in policy_c_input if t["policy_c_block"]]
    c_blocked_agg = aggregate(c_blocked_subset, "C blocks (rescued from B)")
    # Show what was blocked by current actual fan_exhaustion gate
    fan_blocked = [t for t in sim_results if t["actual_outcome"] == "blocked_fan_exhaustion"]
    print("\nDetail:")
    print(f"  Snipes currently fan_exhaustion-blocked: N={len(fan_blocked)}  sim_net={sum(t['sim_pips'] for t in fan_blocked):+.1f}p")
    print(f"  Of those, policy C would still block:  N={len([t for t in fan_blocked if t['policy_c_block']])}")
    print(f"  Of those, policy C would let through:  N={len([t for t in fan_blocked if not t['policy_c_block']])} "
          f"net={sum(t['sim_pips'] for t in fan_blocked if not t['policy_c_block']):+.1f}p")

    # Also: how does policy C handle currently-passed snipes (does it kill any winners?)
    passed_blocked_by_c = [t for t in sim_results if t["actual_outcome"] == "passed_all" and t["policy_c_block"]]
    print(f"  Of currently passed snipes, policy C would NEWLY block: N={len(passed_blocked_by_c)} "
          f"net_pips_those={sum(t['sim_pips'] for t in passed_blocked_by_c):+.1f}p")

    # Block-reason histogram for policy C
    reasons = defaultdict(list)
    for t in c_blocked_subset: reasons[t["policy_c_reason"]].append(t)
    print("\nPolicy C block reasons (within rescued pool):")
    for r, ts in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        net = sum(t["sim_pips"] for t in ts)
        wins = sum(1 for t in ts if t["sim_outcome"] == "win")
        print(f"  {r:<28}N={len(ts):>3}  WR={100*wins/len(ts):>5.1f}%  net={net:+7.1f}p (good if NEGATIVE)")

    # Dump
    out = "/tmp/snipe_gate_redesign.json"
    with open(out, "w") as f:
        json.dump(sim_results, f, indent=2, default=str)
    print(f"\nRaw simulated trades: {out}")


if __name__ == "__main__":
    main()
