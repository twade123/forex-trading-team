"""audit_48h_combo_rule.py — Find the multi-feature loser-signature rule.

Reads /tmp/audit_48h_tip_summary.csv from the prior audit. Applies direction-aware
transforms (RSI/Stoch/slope sign is flipped for SELL trades so 'adverse' = positive).
Then sweeps combos of 2-4 conditions to find the rule with best loser-catch /
winner-kill ratio.

Output: ranked list of rules + per-loser/per-winner snapshot table so Tim can
eyeball the pattern.
"""
import csv, itertools

SUMMARY = "/tmp/audit_48h_tip_summary.csv"


def is_short(d):
    return d.lower() in ("sell", "short")


def transform(row):
    """Add direction-aware adverse-direction features."""
    d = row["direction"]
    short = is_short(d)
    # RSI direction (3-bar): for SELL adverse = positive (rising); for BUY adverse = negative
    rsi_dir = float(row["rsi_dir3"]) if row.get("rsi_dir3") not in (None, "") else None
    row["rsi_adverse_dir"] = (rsi_dir if short else -rsi_dir) if rsi_dir is not None else None
    # E21 slope: for SELL adverse = positive slope; for BUY adverse = negative slope
    slope = float(row["slope_e21_p3"]) if row.get("slope_e21_p3") not in (None, "") else None
    row["slope_e21_adverse"] = (slope if short else -slope) if slope is not None else None
    slope = float(row["slope_e55_p3"]) if row.get("slope_e55_p3") not in (None, "") else None
    row["slope_e55_adverse"] = (slope if short else -slope) if slope is not None else None
    # Stoch position relative to adverse zone
    # For SELL: adverse = stoch rising from OS (low values rising toward mid)
    # For BUY: adverse = stoch falling from OB
    k = float(row["stoch_k"]) if row.get("stoch_k") not in (None, "") else None
    # Simpler: adverse stoch = stoch in "wrong half" (>50 for SELL, <50 for BUY)
    row["stoch_in_adverse_half"] = (1 if (short and k is not None and k > 50) or
                                       (not short and k is not None and k < 50) else 0)
    # Body adverse: for SELL adverse=green bar (body>0); for BUY adverse=red bar (body<0)
    br = float(row["body_ratio"]) if row.get("body_ratio") not in (None, "") else None
    row["body_adverse"] = (br if short else -br) if br is not None else None
    return row


def num(row, k):
    v = row.get(k)
    if v in (None, ""): return None
    try: return float(v)
    except: return None


def eval_rule(rows, rule):
    """rule: list of (col, op, threshold) — ALL must be true."""
    matched = []
    for r in rows:
        ok = True
        for col, op, T in rule:
            v = num(r, col)
            if v is None:
                ok = False; break
            if op == ">=" and v < T: ok = False; break
            if op == "<=" and v > T: ok = False; break
            if op == ">" and v <= T: ok = False; break
            if op == "<" and v >= T: ok = False; break
            if op == "==" and v != T: ok = False; break
        if ok: matched.append(r)
    return matched


def main():
    rows = list(csv.DictReader(open(SUMMARY)))
    for r in rows: transform(r)

    losers = [r for r in rows if r["outcome_class"] in ("large_loser", "small_loser")]
    winners = [r for r in rows if r["outcome_class"] in ("small_winner", "big_winner")]
    opens = [r for r in rows if r["outcome_class"] == "open"]

    print(f"Cohort: {len(losers)} losers, {len(winners)} winners, {len(opens)} open")
    print()

    # Print per-loser tip-bar signature so the pattern is visible
    print("="*120)
    print("LOSER TIP-BAR SIGNATURES")
    print("="*120)
    cols_to_show = [("bar_off","bar"),("pnl_close","pnl"),("mfe","mfe"),("mae","mae"),
                    ("adv_streak","adv"),("rsi","rsi"),("rsi_adverse_dir","rsi_adv"),
                    ("stoch_k","stK"),("stoch_in_adverse_half","stADV"),
                    ("adx","adx"),("adx_dir3","adxD"),
                    ("slope_e21_adverse","s21adv"),("slope_e55_adverse","s55adv"),
                    ("d_e21_atr","dE21"),("d_e100_atr","dE100"),
                    ("bb_width_atr","bbATR"),("body_adverse","bodyADV"),
                    ("fan_state","fan")]
    header = "  " + " ".join(f"{lbl:>7s}" for _,lbl in cols_to_show)
    print(f"  {'id':<6s} {'pair':<8s} {'dir':<4s} {'pnl_final':>9s}   " + header)
    for r in sorted(losers, key=lambda x: float(x["outcome_pnl"])):
        line = f"  {r['trade_id']:<6s} {r['pair']:<8s} {r['direction']:<4s} {float(r['outcome_pnl']):+9.1f}p  "
        for col, lbl in cols_to_show:
            v = r.get(col, "")
            if v in (None, ""): line += "    -   "
            else:
                try:
                    fv = float(v)
                    line += f" {fv:+7.2f}" if abs(fv) < 1000 else f" {fv:+7.0f}"
                except:
                    line += f" {str(v)[:7]:>7s}"
        print(line)
    print()
    print("WINNER SAMPLE (early-bar):")
    for r in sorted(winners, key=lambda x: -float(x["outcome_pnl"]))[:10]:
        line = f"  {r['trade_id']:<6s} {r['pair']:<8s} {r['direction']:<4s} {float(r['outcome_pnl']):+9.1f}p  "
        for col, lbl in cols_to_show:
            v = r.get(col, "")
            if v in (None, ""): line += "    -   "
            else:
                try:
                    fv = float(v)
                    line += f" {fv:+7.2f}" if abs(fv) < 1000 else f" {fv:+7.0f}"
                except:
                    line += f" {str(v)[:7]:>7s}"
        print(line)
    print()

    # === Define candidate single conditions ==================================
    candidates = [
        ("mfe", "<=", 2.0),
        ("mfe", "<=", 1.0),
        ("mfe", "<=", 0.5),
        ("adv_streak", ">=", 3),
        ("adv_streak", ">=", 2),
        ("mae", ">=", 5),
        ("mae", ">=", 7),
        ("mae", ">=", 10),
        ("pnl_close", "<=", -3),
        ("pnl_close", "<=", -5),
        ("rsi_adverse_dir", ">=", 0),
        ("rsi_adverse_dir", ">=", 2),
        ("rsi_adverse_dir", ">=", 5),
        ("stoch_in_adverse_half", "==", 1),
        ("adx_dir3", "<=", 1.5),
        ("adx_dir3", "<=", 0),
        ("slope_e21_adverse", ">=", -1.0),
        ("slope_e21_adverse", ">=", 0),
        ("slope_e55_adverse", ">=", -1.0),
        ("body_adverse", ">=", 0),
        ("bb_width_atr", "<=", 4.5),
        ("bb_width_atr", "<=", 5.0),
    ]

    # === Sweep all combos of 2 and 3 conditions ==============================
    print("="*120)
    print("COMBO RULE SWEEP — 2-condition combos (top 30 by loser_catch - 2*winner_kill)")
    print("="*120)
    print(f"  {'cond1':<35s} {'cond2':<35s} L_catch  W_kill   Score")
    results = []
    for c1, c2 in itertools.combinations(candidates, 2):
        rule = [c1, c2]
        mloss = eval_rule(losers, rule)
        mwin  = eval_rule(winners, rule)
        lc = len(mloss); wc = len(mwin)
        if lc < 12: continue  # require catching at least 12/22 losers
        score = lc/len(losers) - 2*(wc/len(winners))
        results.append((score, lc, wc, rule))
    results.sort(reverse=True)
    for score, lc, wc, rule in results[:30]:
        s1 = f"{rule[0][0]} {rule[0][1]} {rule[0][2]}"
        s2 = f"{rule[1][0]} {rule[1][1]} {rule[1][2]}"
        print(f"  {s1:<35s} {s2:<35s} {lc}/{len(losers)} ({lc/len(losers)*100:.0f}%)  {wc}/{len(winners)} ({wc/len(winners)*100:.0f}%)  {score:+.3f}")

    print()
    print("="*120)
    print("COMBO RULE SWEEP — 3-condition combos (top 30)")
    print("="*120)
    print(f"  {'cond1':<35s} {'cond2':<35s} {'cond3':<35s} L_catch  W_kill   Score")
    results3 = []
    for c1, c2, c3 in itertools.combinations(candidates, 3):
        rule = [c1, c2, c3]
        mloss = eval_rule(losers, rule)
        mwin  = eval_rule(winners, rule)
        lc = len(mloss); wc = len(mwin)
        if lc < 12: continue
        score = lc/len(losers) - 2*(wc/len(winners))
        results3.append((score, lc, wc, rule))
    results3.sort(reverse=True)
    for score, lc, wc, rule in results3[:30]:
        s1 = f"{rule[0][0]} {rule[0][1]} {rule[0][2]}"
        s2 = f"{rule[1][0]} {rule[1][1]} {rule[1][2]}"
        s3 = f"{rule[2][0]} {rule[2][1]} {rule[2][2]}"
        print(f"  {s1:<35s} {s2:<35s} {s3:<35s} {lc}/{len(losers)} ({lc/len(losers)*100:.0f}%)  {wc}/{len(winners)} ({wc/len(winners)*100:.0f}%)  {score:+.3f}")

    # === Check open trades against best rule =================================
    print()
    print("="*120)
    print("OPEN-TRADE ALERT — does it match the best rule?")
    print("="*120)
    if not results3:
        print("  No combos. Skipping.")
        return
    best = results3[0][3]
    rule_str = " AND ".join(f"{c[0]} {c[1]} {c[2]}" for c in best)
    print(f"  Best 3-combo rule: {rule_str}")
    for r in opens:
        match = len(eval_rule([r], best)) == 1
        marker = "🚨 MATCHES — RULE WOULD FIRE" if match else "(safe — rule wouldn't fire)"
        print(f"  #{r['trade_id']:<6s} {r['pair']:<8s} {r['direction']:<4s} {marker}")
        for col, op, T in best:
            v = num(r, col)
            print(f"     {col:<25s} {op} {T}  →  actual {v}")


if __name__ == "__main__":
    main()
