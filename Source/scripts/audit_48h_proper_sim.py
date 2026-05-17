"""audit_48h_proper_sim.py — Proper simulation: rule fires at bar 2 ⇒ tightens
SL to (current bar's adverse pnl + 1p worse). Trade only closes if a FUTURE bar
breaches that SL. If trade recovers before breach, it continues to its actual outcome.

This reflects the real exit_marker v2 dual-mode rule, not naive market-close.
"""
import csv
from collections import defaultdict

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
    # Load per-bar data grouped by trade
    bar_rows = defaultdict(list)
    for r in csv.DictReader(open(BARS)):
        bar_rows[r["trade_id"]].append(r)
    for tid in bar_rows: bar_rows[tid].sort(key=lambda x: int(x["bar_off"]))

    # Tip summary (bar 2 snapshot)
    summary = list(csv.DictReader(open(SUMMARY)))
    for r in summary: transform(r)
    by_id = {r["trade_id"]: r for r in summary}

    def fire_rule(r):  # A OR B
        return sig_A(r) or sig_B(r)

    print("PROPER SIM: rule fires at bar 2 → SL tightens to (bar2_mae + 1p worse).")
    print("Trade closes ONLY if a future bar's max-adverse-excursion breaches that SL.")
    print()

    losers = []; winners = []
    for tid, snap in by_id.items():
        if not fire_rule(snap): continue
        bars = bar_rows.get(tid, [])
        if len(bars) < 3: continue
        bar2 = bars[2]
        bar2_pnl_close = float(bar2["pnl_close"])
        bar2_mae = float(bar2["mae"])
        sl_pips = bar2_mae + 1.0
        outcome_class = snap["outcome_class"]
        if outcome_class == "open" or snap["outcome_pnl"] in (None, ""): continue
        actual_pnl = float(snap["outcome_pnl"])

        # Did a future bar breach SL?
        breach_bar = None
        breach_pnl = None
        for fb in bars[3:]:
            fmae = float(fb["mae"])
            if fmae >= sl_pips:
                breach_bar = int(fb["bar_off"])
                # If breached, trade closes at -sl_pips
                breach_pnl = -sl_pips
                break

        if breach_bar is None:
            # SL never breached — trade reaches its actual outcome
            sim_pnl = actual_pnl
            delta = 0
        else:
            sim_pnl = breach_pnl
            delta = sim_pnl - actual_pnl  # negative for losers actually = saving; for winners cut short = loss

        record = (tid, snap["pair"], snap["direction"], outcome_class, actual_pnl, sim_pnl, delta, breach_bar)
        if outcome_class in ("large_loser", "small_loser"):
            losers.append(record)
        else:
            winners.append(record)

    print("LOSERS caught:")
    print(f"  {'id':<6s} {'pair':<8s} {'dir':<4s} {'actual':>8s} {'sim':>8s} {'delta':>8s} breach")
    total_actual_l = 0; total_sim_l = 0; total_delta_l = 0
    for tid, pair, d, _, ap, sp, dl, b in sorted(losers, key=lambda x: x[4]):
        total_actual_l += ap; total_sim_l += sp; total_delta_l += dl
        b_str = f"bar+{b}" if b is not None else "no breach"
        print(f"  {tid:<6s} {pair:<8s} {d:<4s} {ap:+8.1f} {sp:+8.1f} {dl:+8.1f} {b_str}")
    print(f"  TOTAL: actual {total_actual_l:+.1f}p → sim {total_sim_l:+.1f}p → saved {total_actual_l - total_sim_l:+.1f}p")
    print()

    print("WINNERS flagged:")
    print(f"  {'id':<6s} {'pair':<8s} {'dir':<4s} {'actual':>8s} {'sim':>8s} {'delta':>8s} breach")
    total_actual_w = 0; total_sim_w = 0; total_delta_w = 0
    survived = 0
    for tid, pair, d, _, ap, sp, dl, b in sorted(winners, key=lambda x: -x[4]):
        total_actual_w += ap; total_sim_w += sp; total_delta_w += dl
        if b is None: survived += 1
        b_str = f"bar+{b}" if b is not None else "SL NEVER HIT — winner survives"
        print(f"  {tid:<6s} {pair:<8s} {d:<4s} {ap:+8.1f} {sp:+8.1f} {dl:+8.1f} {b_str}")
    print(f"  TOTAL: actual {total_actual_w:+.1f}p → sim {total_sim_w:+.1f}p → cost {total_actual_w - total_sim_w:+.1f}p")
    print(f"  Survived (tightened SL never breached): {survived}/{len(winners)}")
    print()

    net_save = (total_actual_l - total_sim_l) - (total_actual_w - total_sim_w)
    print(f"NET IMPACT across 48h:")
    print(f"  Losers cut shorter:    {total_actual_l - total_sim_l:+.1f}p saved (across {len(losers)} losers)")
    print(f"  Winners cut shorter:   {total_actual_w - total_sim_w:+.1f}p lost (across {len(winners)} winners; only {len(winners)-survived} actually got hit)")
    print(f"  NET:                   {net_save:+.1f}p in 48h")


if __name__ == "__main__":
    main()
