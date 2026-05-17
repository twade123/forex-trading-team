"""
Audit: when validator_fan_alignment gate fires, is it actually right?

Gate fires when (structural) AND (candle_warns) BOTH true:
  structural = at_peak | post_peak (1-10b ago, decayed below peak) | fan_reversed (sign flip in last 6b)
  candle_warns = SELL+green or BUY+red entry candle

Gate already logs: reason, fan_sep_peak, fan_sep_now, bars_since_peak, candle_warns.

Audit slices:
  1. Block reasons distribution (at_peak / post_peak / fan_reversed / combos)
  2. For post_peak: how trivial is the "decay"? bars_since_peak & decay_pips
  3. Cross-ref with ema_ordering gate output to confirm fan was still ordered/wide
  4. Sample blocks — generate visual evidence for Tim
"""
import os, sys, json, sqlite3
from datetime import datetime, timedelta
from collections import defaultdict
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

FLIGHT_DB = "<repo_root>/Source/flight_recorder.db"
WATCH_DB  = "~/Jarvis/Database/v2/trading_forex.db"
WINDOW_DAYS = 14
MIN_FAN_PIPS = 4.0

def pip_size(p): return 0.01 if p.endswith("_JPY") else 0.0001

_cache = {}
def get_pair(wid):
    if wid in _cache: return _cache[wid]
    with sqlite3.connect(WATCH_DB, timeout=5) as wc:
        r = wc.execute("SELECT instrument FROM watch_suggestions WHERE id=?", (wid,)).fetchone()
    _cache[wid] = r[0] if r else None
    return _cache[wid]


def gather_ema_geom(fc, watch_id, block_ts):
    """Pull e21/e55/e100 from ema_ordering gate within 30s before this block."""
    cutoff = (datetime.fromisoformat(block_ts.replace("Z", "+00:00")) - timedelta(seconds=30)).isoformat()
    rows = fc.execute(
        "SELECT data FROM flight_log WHERE timestamp >= ? AND timestamp <= ? "
        "AND stage='SNIPE_GATE_PASSED' AND data LIKE ? AND data LIKE '%ema_ordering%' "
        "ORDER BY timestamp DESC LIMIT 1",
        (cutoff, block_ts, f'%\"watch_id\": {watch_id}%')
    ).fetchone()
    if not rows: return None
    try:
        d = json.loads(rows[0])
        return {"e21": d.get("e21"), "e55": d.get("e55"), "e100": d.get("e100"),
                "snipe_dir": d.get("snipe_dir")}
    except Exception:
        return None


def parse_post_peak(reason):
    """Extract bars_since_peak and decay_pips from 'post_peak(Nb,Xp)' string."""
    if "post_peak(" not in reason: return None, None
    try:
        inside = reason.split("post_peak(")[1].split(")")[0]
        parts = inside.split(",")
        bars = int(parts[0].replace("b", ""))
        pips = float(parts[1].replace("p", ""))
        return bars, pips
    except Exception:
        return None, None


def main():
    print(f"Audit: validator_fan_alignment gate — is it firing correctly?")
    print(f"Window: last {WINDOW_DAYS}d")
    print("=" * 88)

    blocks = []
    with sqlite3.connect(FLIGHT_DB, timeout=5) as fc:
        rows = fc.execute(
            "SELECT timestamp, data FROM flight_log "
            "WHERE stage='SNIPE_GATE_BLOCKED' AND data LIKE '%validator_fan_alignment%' "
            "AND timestamp > datetime('now', ?) ORDER BY timestamp DESC",
            (f'-{WINDOW_DAYS} days',)
        ).fetchall()

        for ts, raw in rows:
            try: d = json.loads(raw)
            except Exception: continue
            wid = d.get("watch_id")
            if not wid: continue
            pair = get_pair(wid)
            reason = d.get("reason", "")
            bars, decay = parse_post_peak(reason)
            geom = gather_ema_geom(fc, wid, ts)

            # Reality check: was fan actually still ordered and wide?
            fan_pips = None; fan_aligned = None
            if geom and all([geom.get("e21"), geom.get("e55"), geom.get("e100"), geom.get("snipe_dir")]):
                e21, e55, e100 = geom["e21"], geom["e55"], geom["e100"]
                pip = pip_size(pair) if pair else 0.0001
                fan_pips = (max(e21, e55, e100) - min(e21, e55, e100)) / pip
                sd = geom["snipe_dir"]
                fan_aligned = (sd == "BUY" and e21 > e55 > e100) or (sd == "SELL" and e100 > e55 > e21)

            blocks.append({
                "ts": ts, "watch_id": wid, "pair": pair, "reason": reason,
                "fan_sep_peak": d.get("fan_sep_peak"), "fan_sep_now": d.get("fan_sep_now"),
                "bars_since_peak": d.get("bars_since_peak"),
                "post_peak_bars": bars, "post_peak_decay": decay,
                "ema_fan_pips": round(fan_pips, 1) if fan_pips else None,
                "ema_fan_aligned": fan_aligned,
                "snipe_dir": geom.get("snipe_dir") if geom else None,
            })

    print(f"\nTotal validator_fan_alignment blocks: {len(blocks)}")

    # 1. Reason distribution
    print("\n" + "-" * 88)
    print("REASON DISTRIBUTION:")
    reason_hist = defaultdict(int)
    for b in blocks:
        # Bucket multi-reason combos
        r = b["reason"]
        if "+" in r: r = "+".join(sorted(p.split("(")[0] for p in r.split("+")))
        elif "(" in r: r = r.split("(")[0]
        reason_hist[r] += 1
    for r, n in sorted(reason_hist.items(), key=lambda kv: -kv[1]):
        pct = 100 * n / len(blocks)
        print(f"  {r:<35}{n:>5}  ({pct:.1f}%)")

    # 2. post_peak severity — how trivial vs substantial?
    print("\n" + "-" * 88)
    print("POST_PEAK SEVERITY (the most common reason):")
    pp_blocks = [b for b in blocks if b["post_peak_bars"] is not None]
    if pp_blocks:
        print(f"  Total post_peak blocks: {len(pp_blocks)}")
        # Bucket by bars_since_peak
        bars_hist = defaultdict(int)
        decay_hist = defaultdict(int)
        for b in pp_blocks:
            bn = b["post_peak_bars"]
            if bn == 1: bars_hist["1 bar"] += 1
            elif bn == 2: bars_hist["2 bars"] += 1
            elif bn <= 4: bars_hist["3-4 bars"] += 1
            elif bn <= 7: bars_hist["5-7 bars"] += 1
            else: bars_hist["8+ bars"] += 1
            d = b["post_peak_decay"]
            if d < 0.5: decay_hist["<0.5 pips"] += 1
            elif d < 1.0: decay_hist["0.5-1.0 pips"] += 1
            elif d < 2.0: decay_hist["1-2 pips"] += 1
            elif d < 5.0: decay_hist["2-5 pips"] += 1
            else: decay_hist["5+ pips"] += 1
        print("\n  By bars_since_peak:")
        for k, n in sorted(bars_hist.items()):
            pct = 100 * n / len(pp_blocks)
            print(f"    {k:<15}{n:>5}  ({pct:.1f}%)")
        print("\n  By decay magnitude (peak - now):")
        for k, n in sorted(decay_hist.items()):
            pct = 100 * n / len(pp_blocks)
            print(f"    {k:<15}{n:>5}  ({pct:.1f}%)")

        # The "trivial" bucket: 1-2 bars AND <1p decay = barely off peak
        trivial = [b for b in pp_blocks if b["post_peak_bars"] <= 2 and b["post_peak_decay"] < 1.0]
        substantial = [b for b in pp_blocks if not (b["post_peak_bars"] <= 2 and b["post_peak_decay"] < 1.0)]
        print(f"\n  TRIVIAL (1-2 bars + <1p decay):   {len(trivial)} ({100*len(trivial)/len(pp_blocks):.1f}%)")
        print(f"  SUBSTANTIAL:                       {len(substantial)} ({100*len(substantial)/len(pp_blocks):.1f}%)")

    # 3. Cross-reference: was the underlying fan still ordered & wide at block time?
    print("\n" + "-" * 88)
    print("REALITY CHECK: was the actual EMA fan still ordered & wide when the gate fired?")
    decided = [b for b in blocks if b["ema_fan_aligned"] is not None]
    if decided:
        aligned_wide = [b for b in decided if b["ema_fan_aligned"] and b["ema_fan_pips"] and b["ema_fan_pips"] >= MIN_FAN_PIPS]
        aligned_narrow = [b for b in decided if b["ema_fan_aligned"] and b["ema_fan_pips"] and b["ema_fan_pips"] < MIN_FAN_PIPS]
        unordered = [b for b in decided if not b["ema_fan_aligned"]]
        print(f"  Decided cases:                                    {len(decided)}")
        print(f"  EMAs still ORDERED for snipe dir + WIDE (>={MIN_FAN_PIPS}p):  {len(aligned_wide)} ({100*len(aligned_wide)/len(decided):.1f}%)")
        print(f"  EMAs still ORDERED but narrow (<{MIN_FAN_PIPS}p):           {len(aligned_narrow)} ({100*len(aligned_narrow)/len(decided):.1f}%)")
        print(f"  EMAs UNORDERED for snipe dir (true reversal):     {len(unordered)} ({100*len(unordered)/len(decided):.1f}%)")
        print("\n  Interpretation:")
        print(f"    - UNORDERED ({len(unordered)}) = gate genuinely caught a fan that flipped — correct block")
        print(f"    - ORDERED+NARROW ({len(aligned_narrow)}) = fan collapsing — defensible")
        print(f"    - ORDERED+WIDE ({len(aligned_wide)}) = healthy fan, gate fired on micro fan_sep wiggle = likely false positive")

    # 4. Cross-tab: reason × reality
    print("\n" + "-" * 88)
    print("REASON × REALITY:")
    print(f"{'REASON':<20}{'WIDE+ORDERED':>15}{'NARROW':>10}{'UNORDERED':>12}{'NO_DATA':>10}")
    by_reason = defaultdict(lambda: {"wide": 0, "narrow": 0, "unord": 0, "nodata": 0})
    for b in blocks:
        r = b["reason"]
        if "+" in r: rcat = "+".join(sorted(p.split("(")[0] for p in r.split("+")))
        elif "(" in r: rcat = r.split("(")[0]
        else: rcat = r
        if b["ema_fan_aligned"] is None: by_reason[rcat]["nodata"] += 1
        elif not b["ema_fan_aligned"]: by_reason[rcat]["unord"] += 1
        elif b["ema_fan_pips"] and b["ema_fan_pips"] >= MIN_FAN_PIPS: by_reason[rcat]["wide"] += 1
        else: by_reason[rcat]["narrow"] += 1
    for r in sorted(by_reason.keys(), key=lambda k: -(by_reason[k]['wide']+by_reason[k]['narrow']+by_reason[k]['unord'])):
        v = by_reason[r]
        print(f"  {r:<18}{v['wide']:>13}{v['narrow']:>10}{v['unord']:>12}{v['nodata']:>10}")

    # 5. Sample blocks — show the most suspect (post_peak, trivial, ordered+wide)
    print("\n" + "=" * 88)
    print("SAMPLE SUSPECT BLOCKS (post_peak trivial + fan still ordered & wide):")
    suspects = [b for b in blocks
                if b["post_peak_bars"] is not None
                and b["post_peak_bars"] <= 3 and b["post_peak_decay"] < 1.5
                and b["ema_fan_aligned"] is True
                and (b["ema_fan_pips"] or 0) >= MIN_FAN_PIPS]
    print(f"  Total suspect: {len(suspects)}")
    print(f"\n  {'ET TIME':<14}{'PAIR':<10}{'DIR':<5}{'REASON':<28}{'FAN_PIPS':>10}{'WATCH':>7}")
    for b in suspects[:15]:
        et = (datetime.fromisoformat(b["ts"].replace("Z","+00:00")) - timedelta(hours=4)).strftime("%m-%d %H:%M")
        print(f"  {et:<14}{(b['pair'] or '?'):<10}{(b['snipe_dir'] or '?'):<5}"
              f"{b['reason'][:26]:<28}{(b['ema_fan_pips'] or 0):>9.1f}p{str(b['watch_id'])[-5:]:>7}")

    out = "/tmp/validator_fan_alignment_audit.json"
    with open(out, "w") as f: json.dump(blocks, f, indent=2, default=str)
    print(f"\nFull raw audit: {out}")


if __name__ == "__main__":
    main()
