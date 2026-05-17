#!/usr/bin/env python3
"""perf_live — real-time trade diagnostic.

Examples:
    python perf_live.py                                 # status page
    python perf_live.py --check kronos,guardian,daemons
    python perf_live.py --check watches                 # stale + near-trigger
    python perf_live.py --check scout                   # learning loop + cadence + bias
    python perf_live.py --symptom "snipe fired EUR_USD SELL then lost 8p"
    python perf_live.py --trade 1523
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.context import Window
from diagnostics import (
    live_health, watch_health, scout_quality, scout_scan_health,
    vault_matcher, snipe_analysis,
)


def _print_section(title: str) -> None:
    print(f"\n{'='*60}\n{title}\n{'='*60}")


def status_page() -> int:
    _print_section("LIVE HEALTH")
    critical = 0
    for s in live_health.check_all():
        tag = {"critical": "🔴", "warn": "🟡", "ok": "🟢"}[s.severity]
        print(f"{tag} {s.component:30} {s.message}")
        if s.severity == "critical":
            critical += 1
    _print_section("WATCHES NEAR TRIGGER")
    near = live_health.check_watches_near_trigger(progress_min=0.80)
    for w in near:
        print(f"  {w['pair']:8} {(w.get('direction') or ''):4} progress={w['peak_progress']:.0%} origin={w['origin_type']}")
    if not near:
        print("  (none)")
    return 2 if critical else 0


def check_kronos() -> None:
    _print_section("KRONOS")
    s = live_health.check_kronos()
    print(f"  alive={s.alive}  staleness={s.staleness_seconds:.0f}s  → {s.message}")
    stages = live_health.check_pipeline_stages()
    for name in ["kronos_hunter_scan_start", "kronos_hunter_signal", "kronos_filter_check"]:
        if name in stages:
            st = stages[name]
            print(f"  {name:32} last={st.staleness_seconds:.0f}s  {st.severity}")


def check_guardian() -> None:
    _print_section("GUARDIAN")
    s = live_health.check_guardian()
    print(f"  {s.message}  severity={s.severity}  details={s.details}")


def check_daemons() -> None:
    _print_section("DAEMONS")
    for name, s in live_health.check_daemons().items():
        tag = {"critical": "🔴", "warn": "🟡", "ok": "🟢"}[s.severity]
        print(f"  {tag} {name:20} {s.message}")


def check_watches() -> None:
    _print_section("STALE WATCHES")
    for v in watch_health.scan_active_watches():
        if v.recommendation != "keep":
            print(f"  [{v.recommendation:12}] #{v.watch_id} {v.pair} {v.direction} "
                  f"age={v.age_hours:.1f}h → {v.reason}")
    _print_section("NEAR TRIGGER")
    for w in live_health.check_watches_near_trigger():
        print(f"  {w['pair']:8} progress={w['peak_progress']:.0%}")


def check_scout() -> None:
    _print_section("SCOUT LEARNING LOOP")
    hl = scout_quality.learning_loop_health(Window.last_days(7))
    print(f"  {hl['message']}  last_populated={hl['last_populated_trade']}")
    _print_section("SCOUT SCAN CADENCE (last 24h)")
    c = scout_scan_health.scan_cadence(Window.last_hours(24))
    print(f"  scans={c['n_scans']} rate={c['scans_per_hour']:.1f}/h "
          f"(expected ~{c['expected_per_hour']:.0f}/h) gaps>15m={c['gaps_over_15min']}")
    _print_section("PAIR BIAS (last 7d)")
    pb = scout_quality.pair_bias_analysis(Window.last_days(7))
    for p in pb["biased_pairs"]:
        info = pb["pairs"][p]
        print(f"  {p} {info['n']} findings ({info['pct_of_total']:.0%}) — over-represented")


def symptom_search(desc: str) -> None:
    _print_section(f"VAULT MATCHES for: {desc}")
    for m in vault_matcher.match_symptom(desc, limit=5):
        print(f"\n  {m['path']}\n  {'-'*40}\n  {m['snippet'][:300]}...")


def trade_dive(trade_id: int) -> None:
    from diagnostics.context import load_flight_log
    _print_section(f"FLIGHT LOG for trade {trade_id}")
    for r in load_flight_log(trade_id=str(trade_id)):
        print(f"  {r.timestamp}  {r.stage:28} {r.status:10} {r.note or ''}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Real-time trade diagnostics")
    p.add_argument("--check", type=str, default=None,
                   help="Comma-separated: kronos,guardian,daemons,watches,scout")
    p.add_argument("--symptom", type=str, default=None)
    p.add_argument("--trade", type=int, default=None)
    args = p.parse_args(argv)
    if args.trade:
        trade_dive(args.trade); return 0
    if args.symptom:
        symptom_search(args.symptom); return 0
    if args.check:
        for c in args.check.split(","):
            c = c.strip()
            fn = {"kronos": check_kronos, "guardian": check_guardian,
                  "daemons": check_daemons, "watches": check_watches,
                  "scout": check_scout}.get(c)
            if fn:
                fn()
            else:
                print(f"Unknown check: {c}", file=sys.stderr)
        return 0
    return status_page()


if __name__ == "__main__":
    sys.exit(main())
