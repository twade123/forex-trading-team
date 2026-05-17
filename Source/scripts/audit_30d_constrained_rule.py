"""audit_30d_constrained_rule.py — Run the constrained-rule sweep on 30d data."""
import csv, sys, os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_48h_constrained_rule import sig_A, sig_B, transform, num

BARS = "/tmp/audit_30d_per_bar.csv"
SUMMARY = "/tmp/audit_30d_tip_summary.csv"


def main():
    bar_rows = defaultdict(list)
    for r in csv.DictReader(open(BARS)):
        bar_rows[r["trade_id"]].append(r)
    for tid in bar_rows: bar_rows[tid].sort(key=lambda x: int(x["bar_off"]))

    summary = list(csv.DictReader(open(SUMMARY)))
    for r in summary: transform(r)

    results = []
    for pnl_low in (-6, -8, -10, -12, -15, -20):
        for pnl_high in (-1, -2, -3, -4):
            if pnl_low >= pnl_high: continue
            for action in ("close", "tighten1p", "tighten3p", "BE"):
                for signame in ("A", "B", "AB"):
                    sig = {"A": sig_A, "B": sig_B, "AB": lambda r: sig_A(r) or sig_B(r)}[signame]

                    loser_save = 0; loser_count = 0
                    winner_cost = 0; winner_count = 0
                    winner_survived = 0
                    fires_total = 0

                    for snap in summary:
                        if not sig(snap): continue
                        tid = snap["trade_id"]
                        outcome = snap["outcome_class"]
                        if outcome == "open" or snap["outcome_pnl"] in (None, ""): continue
                        bars = bar_rows.get(tid, [])
                        if len(bars) < 3: continue
                        bar2 = bars[2]
                        bar2_pnl = float(bar2["pnl_close"])
                        bar2_mae = float(bar2["mae"])
                        if not (pnl_low <= bar2_pnl <= pnl_high): continue
                        actual_pnl = float(snap["outcome_pnl"])
                        fires_total += 1

                        if action == "close":
                            sim_pnl = bar2_pnl
                        elif action == "tighten1p":
                            sl_pips_from_entry = -(bar2_pnl - 1)
                            breach = False
                            for fb in bars[3:]:
                                if float(fb["mae"]) >= sl_pips_from_entry: breach = True; break
                            sim_pnl = -sl_pips_from_entry if breach else actual_pnl
                        elif action == "tighten3p":
                            sl_pips_from_entry = -(bar2_pnl - 3)
                            breach = False
                            for fb in bars[3:]:
                                if float(fb["mae"]) >= sl_pips_from_entry: breach = True; break
                            sim_pnl = -sl_pips_from_entry if breach else actual_pnl
                        elif action == "BE":
                            cur_mfe = float(bar2["mfe"])
                            future_max_mfe = max([float(fb["mfe"]) for fb in bars[3:]] + [cur_mfe])
                            if future_max_mfe > 0:
                                sim_pnl = 0.0
                            else:
                                sim_pnl = actual_pnl

                        delta = sim_pnl - actual_pnl
                        if outcome in ("large_loser", "small_loser"):
                            loser_save += delta
                            loser_count += 1
                        else:
                            if sim_pnl < actual_pnl:
                                winner_cost += actual_pnl - sim_pnl
                            else:
                                winner_survived += 1
                            winner_count += 1

                    net = loser_save - winner_cost
                    if loser_count + winner_count == 0: continue
                    results.append((net, signame, pnl_low, pnl_high, action,
                                    loser_count, winner_count, winner_survived,
                                    loser_save, winner_cost))

    results.sort(reverse=True)
    print(f"{'sig':<3s} {'pnl_low':<8s} {'pnl_high':<8s} {'action':<10s} {'L':<4s} {'W':<4s} {'Wsurv':<6s} {'L_save':<8s} {'W_cost':<8s} NET")
    for net, signame, pl, ph, act, lc, wc, ws, ls, wc_p in results[:40]:
        print(f"{signame:<3s} {pl:<8.0f} {ph:<8.0f} {act:<10s} {lc:<4d} {wc:<4d} {ws:<6d} +{ls:6.1f}p -{wc_p:6.1f}p  {net:+7.1f}p")

    # === Best rule deep-dive ==================================================
    if not results: return
    best = results[0]
    net, signame, pl, ph, act, lc, wc, ws, ls, wc_p = best
    print()
    print("="*100)
    print(f"BEST RULE on 30d: sig={signame}, pnl_range=[{pl},{ph}], action={act}")
    print(f"  Losers caught: {lc}/98 ({lc/98*100:.0f}%) — saved +{ls:.1f}p")
    print(f"  Winners flagged: {wc}/161 ({wc/161*100:.0f}%) — cost -{wc_p:.1f}p")
    print(f"    of those, {ws} survived (SL never hit)")
    print(f"  NET 30d: {net:+.1f}p = {net/30:+.1f}p/day = ~{net*30/30:+.0f}p/month run-rate")
    print("="*100)

    # Per-source breakdown
    print()
    print(f"Best rule applied — per-source breakdown:")
    sig_fn = {"A": sig_A, "B": sig_B, "AB": lambda r: sig_A(r) or sig_B(r)}[signame]
    by_source = defaultdict(lambda: {"L_caught": 0, "L_save": 0, "W_flagged": 0, "W_cost": 0,
                                     "L_total": 0, "W_total": 0})
    for snap in summary:
        src = snap["source"]
        outcome = snap["outcome_class"]
        if outcome == "open" or snap["outcome_pnl"] in (None, ""): continue
        is_loser = outcome in ("large_loser", "small_loser")
        is_winner = outcome in ("small_winner", "big_winner")
        if is_loser: by_source[src]["L_total"] += 1
        if is_winner: by_source[src]["W_total"] += 1

        if not sig_fn(snap): continue
        tid = snap["trade_id"]
        bars = bar_rows.get(tid, [])
        if len(bars) < 3: continue
        bar2 = bars[2]
        bar2_pnl = float(bar2["pnl_close"])
        bar2_mae = float(bar2["mae"])
        if not (pl <= bar2_pnl <= ph): continue
        actual_pnl = float(snap["outcome_pnl"])

        cur_mfe = float(bar2["mfe"])
        future_max_mfe = max([float(fb["mfe"]) for fb in bars[3:]] + [cur_mfe])
        sim_pnl = 0.0 if future_max_mfe > 0 else actual_pnl
        delta = sim_pnl - actual_pnl
        if is_loser:
            by_source[src]["L_caught"] += 1
            by_source[src]["L_save"] += delta
        elif is_winner and sim_pnl < actual_pnl:
            by_source[src]["W_flagged"] += 1
            by_source[src]["W_cost"] += actual_pnl - sim_pnl

    for src, st in sorted(by_source.items()):
        net_src = st["L_save"] - st["W_cost"]
        print(f"  {src:<14s}  L={st['L_caught']}/{st['L_total']:<3d} W={st['W_flagged']}/{st['W_total']:<3d}  "
              f"save=+{st['L_save']:6.1f}p  cost=-{st['W_cost']:6.1f}p  NET={net_src:+7.1f}p")


if __name__ == "__main__":
    main()
