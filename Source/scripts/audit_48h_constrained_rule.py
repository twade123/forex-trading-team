"""audit_48h_constrained_rule.py — Iterate on the rule:
  Signature B variant fires only when bar2_pnl is in "early warning zone".
  Action options:
    1. Tighten SL to bar2_close - 1p (gentle)
    2. Market close at bar2_close (aggressive)
  Both simulated honestly using actual future-bar data.

Goal: find the {pnl_range × action} combo with best NET pip impact.
"""
import csv
from collections import defaultdict
import itertools

BARS = "/tmp/audit_48h_per_bar.csv"
SUMMARY = "/tmp/audit_48h_tip_summary.csv"


def is_short(d): return d.lower() in ("sell", "short")

def transform(row):
    short = is_short(row["direction"])
    def f(k):
        v = row.get(k)
        try: return float(v) if v not in (None, "") else None
        except: return None
    rd = f("rsi_dir3");      row["rsi_adverse_dir"] = (rd if short else -rd) if rd is not None else None
    s21 = f("slope_e21_p3"); row["slope_e21_adverse"] = (s21 if short else -s21) if s21 is not None else None
    s55 = f("slope_e55_p3"); row["slope_e55_adverse"] = (s55 if short else -s55) if s55 is not None else None
    br = f("body_ratio");    row["body_adverse"] = (br if short else -br) if br is not None else None
    return row

def num(row, k):
    v = row.get(k)
    if v in (None, ""): return None
    try: return float(v)
    except: return None


def sig_A(r):
    s21 = num(r, "slope_e21_adverse"); s55 = num(r, "slope_e55_adverse"); body = num(r, "body_adverse")
    return s21 is not None and s55 is not None and body is not None and s21 >= -1.0 and s55 >= -1.0 and body >= 0


def sig_B(r):
    mfe = num(r, "mfe"); adv = num(r, "adv_streak"); rdir = num(r, "rsi_adverse_dir")
    return mfe is not None and adv is not None and rdir is not None and mfe <= 2.0 and adv >= 3 and rdir >= 5


def main():
    bar_rows = defaultdict(list)
    for r in csv.DictReader(open(BARS)):
        bar_rows[r["trade_id"]].append(r)
    for tid in bar_rows: bar_rows[tid].sort(key=lambda x: int(x["bar_off"]))

    summary = list(csv.DictReader(open(SUMMARY)))
    for r in summary: transform(r)

    # Sweep: pnl_low, pnl_high, action (close|tighten), signal (A|B|AB)
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

                        if action == "close":
                            sim_pnl = bar2_pnl
                        elif action == "tighten1p":
                            # SL set to (bar2_pnl - 1). Closes if future mae exceeds (bar2_mae + ... ).
                            # Actually SL is at -|bar2_pnl + 1| pips from entry.
                            sl_pips_from_entry = -(bar2_pnl - 1)  # if bar2_pnl=-9, SL at -10p loss
                            breach = False
                            for fb in bars[3:]:
                                fmae = float(fb["mae"])
                                if fmae >= sl_pips_from_entry:
                                    breach = True; break
                            sim_pnl = -sl_pips_from_entry if breach else actual_pnl
                        elif action == "tighten3p":
                            sl_pips_from_entry = -(bar2_pnl - 3)
                            breach = False
                            for fb in bars[3:]:
                                if float(fb["mae"]) >= sl_pips_from_entry: breach = True; break
                            sim_pnl = -sl_pips_from_entry if breach else actual_pnl
                        elif action == "BE":
                            # SL moves to break-even (0). Trade closes at 0 if/when price returns to entry.
                            # Otherwise continues to actual outcome.
                            sl_pips_from_entry = 0
                            # Future-bar check: does any future bar have MFE growing? If favorable bar appears
                            # before MAE reaches infinity, trade can close at BE (need fav move).
                            # Simplification: if trade later went positive at any point, exit at 0; else actual.
                            # MFE in future = max future mfe vs current mfe.
                            cur_mfe = float(bar2["mfe"])
                            future_max_mfe = max([float(fb["mfe"]) for fb in bars[3:]] + [cur_mfe])
                            if future_max_mfe > 0:
                                # If trade ever showed favorable move, BE was hit → exit at 0
                                sim_pnl = 0.0
                            else:
                                # Never recovered to break-even, continued to actual
                                sim_pnl = actual_pnl

                        delta = sim_pnl - actual_pnl
                        if outcome in ("large_loser", "small_loser"):
                            loser_save += delta  # positive when sim better than actual
                            loser_count += 1
                        else:
                            if sim_pnl < actual_pnl:
                                winner_cost += actual_pnl - sim_pnl
                            else:
                                winner_survived += 1
                            winner_count += 1

                    net = loser_save - winner_cost
                    if loser_count == 0 and winner_count == 0: continue
                    results.append((net, signame, pnl_low, pnl_high, action,
                                    loser_count, winner_count, winner_survived,
                                    loser_save, winner_cost))

    results.sort(reverse=True)
    print(f"{'sig':<3s} {'pnl_low':<8s} {'pnl_high':<8s} {'action':<10s} {'L':<4s} {'W':<4s} {'Wsurv':<6s} {'L_save':<8s} {'W_cost':<8s} NET")
    for net, signame, pl, ph, act, lc, wc, ws, ls, wc_p in results[:50]:
        print(f"{signame:<3s} {pl:<8.0f} {ph:<8.0f} {act:<10s} {lc:<4d} {wc:<4d} {ws:<6d} +{ls:6.1f}p -{wc_p:6.1f}p  {net:+7.1f}p")


if __name__ == "__main__":
    main()
