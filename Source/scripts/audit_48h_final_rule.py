"""audit_48h_final_rule.py — Combine Signatures A and B with OR, measure full impact."""
import csv

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
    """Stalled trend: EMAs not moving + current bar adverse."""
    s21 = num(r, "slope_e21_adverse"); s55 = num(r, "slope_e55_adverse"); body = num(r, "body_adverse")
    return s21 is not None and s55 is not None and body is not None and s21 >= -1.0 and s55 >= -1.0 and body >= 0


def sig_B(r):
    """Bad entry timing: MFE plateau + adv streak + RSI fighting direction."""
    mfe = num(r, "mfe"); adv = num(r, "adv_streak"); rdir = num(r, "rsi_adverse_dir")
    return mfe is not None and adv is not None and rdir is not None and mfe <= 2.0 and adv >= 3 and rdir >= 5


def sig_C(r):
    """Stronger entry-timing variant: MFE plateau + adv streak + already 3p underwater."""
    mfe = num(r, "mfe"); adv = num(r, "adv_streak"); pnl = num(r, "pnl_close")
    return mfe is not None and adv is not None and pnl is not None and mfe <= 2.0 and adv >= 3 and pnl <= -3.0


def main():
    rows = list(csv.DictReader(open(SUMMARY)))
    for r in rows: transform(r)

    losers = [r for r in rows if r["outcome_class"] in ("large_loser", "small_loser")]
    winners = [r for r in rows if r["outcome_class"] in ("small_winner", "big_winner")]
    opens = [r for r in rows if r["outcome_class"] == "open"]

    # Per-trade pnl lookup
    def pnl(r): return float(r["outcome_pnl"])

    rules = {
        "A: slope_e21_adv>=-1 AND slope_e55_adv>=-1 AND body_adv>=0": sig_A,
        "B: mfe<=2 AND adv_streak>=3 AND rsi_adv_dir>=5":              sig_B,
        "C: mfe<=2 AND adv_streak>=3 AND pnl_close<=-3":               sig_C,
        "A OR B":                                                       lambda r: sig_A(r) or sig_B(r),
        "A OR C":                                                       lambda r: sig_A(r) or sig_C(r),
        "B OR C":                                                       lambda r: sig_B(r) or sig_C(r),
        "A OR B OR C":                                                  lambda r: sig_A(r) or sig_B(r) or sig_C(r),
    }

    # Estimate net pip impact:
    # if loser caught at bar 2 with pnl_close, action = SL→BE (close at -3 to 0).
    # Conservatively model: fire_pnl = pnl_close at the snapshot bar (best case for now).
    # Real action would tighten SL to current price-1p, but we use bar_close as proxy.

    print(f"{'rule':<55s} L_catch              W_kill            sim_save  sim_loss  NET")
    for name, fn in rules.items():
        L_hit = [r for r in losers if fn(r)]
        W_hit = [r for r in winners if fn(r)]
        # Estimate save per loser = actual_pnl - fire_pnl(bar2_pnl_close)
        # In practice: fire = SL→BE so trade closes at ~bar2 mark or breakeven if profitable
        sim_save = 0.0
        for r in L_hit:
            fire_pnl = num(r, "pnl_close") or 0
            actual = pnl(r)
            sim_save += max(0, fire_pnl - actual)  # saved = fire_pnl - actual (both negative; fire less neg = save)
        # Per winner killed: cost = actual_pnl - fire_pnl(bar2)
        sim_loss = 0.0
        for r in W_hit:
            fire_pnl = num(r, "pnl_close") or 0
            actual = pnl(r)
            sim_loss += max(0, actual - fire_pnl)  # winner cut shorter = lost diff
        net = sim_save - sim_loss
        print(f"{name:<55s} {len(L_hit):2d}/{len(losers)} ({len(L_hit)/len(losers)*100:3.0f}%)  "
              f"{len(W_hit):2d}/{len(winners)} ({len(W_hit)/len(winners)*100:3.0f}%)  "
              f"+{sim_save:6.1f}p  -{sim_loss:6.1f}p  {net:+7.1f}p")

    # Per-loser detail: which signatures fired
    print()
    print("="*120)
    print("PER-LOSER detection breakdown (which signatures fired)")
    print("="*120)
    print(f"  {'id':<6s} {'pair':<8s} {'dir':<4s} {'actual':<8s} {'A':<3s} {'B':<3s} {'C':<3s} {'AorBorC':<8s}")
    for r in sorted(losers, key=lambda x: pnl(x)):
        a = sig_A(r); b = sig_B(r); c = sig_C(r); any_ = a or b or c
        print(f"  {r['trade_id']:<6s} {r['pair']:<8s} {r['direction']:<4s} {pnl(r):+8.1f} "
              f"{'YES' if a else '   ':<3s} {'YES' if b else '   ':<3s} {'YES' if c else '   ':<3s} "
              f"{'CAUGHT' if any_ else 'MISS':<8s}")

    print()
    print("="*120)
    print("WINNERS killed by A OR B OR C")
    print("="*120)
    print(f"  {'id':<6s} {'pair':<8s} {'dir':<4s} {'actual':<8s} {'bar2pnl':<8s} {'fired':<10s}")
    for r in sorted(winners, key=lambda x: -pnl(x)):
        a = sig_A(r); b = sig_B(r); c = sig_C(r)
        if a or b or c:
            fired = ("A" if a else "") + ("B" if b else "") + ("C" if c else "")
            print(f"  {r['trade_id']:<6s} {r['pair']:<8s} {r['direction']:<4s} {pnl(r):+8.1f} "
                  f"{num(r,'pnl_close'):+8.1f} {fired:<10s}")

    print()
    print("="*120)
    print("OPEN-TRADE ALERT")
    print("="*120)
    for r in opens:
        a = sig_A(r); b = sig_B(r); c = sig_C(r)
        any_ = a or b or c
        print(f"  #{r['trade_id']:<6s} {r['pair']:<8s} {r['direction']:<4s} bar={r['bar_off']} pnl={r['pnl_close']}")
        print(f"    A (stalled trend):       {'FIRES' if a else 'no'}  (slope_e21_adv={num(r,'slope_e21_adverse'):.2f}, slope_e55_adv={num(r,'slope_e55_adverse'):.2f}, body_adv={num(r,'body_adverse'):.2f})")
        print(f"    B (bad entry timing):    {'FIRES' if b else 'no'}  (mfe={num(r,'mfe'):.1f}, adv_streak={num(r,'adv_streak'):.0f}, rsi_adv_dir={num(r,'rsi_adverse_dir'):.1f})")
        print(f"    C (mfe plateau + neg):   {'FIRES' if c else 'no'}  (mfe={num(r,'mfe'):.1f}, adv_streak={num(r,'adv_streak'):.0f}, pnl_close={num(r,'pnl_close'):.1f})")
        print(f"    OVERALL: {'🚨 FIRE GUARDIAN — close to BE' if any_ else '(safe)'}")


if __name__ == "__main__":
    main()
