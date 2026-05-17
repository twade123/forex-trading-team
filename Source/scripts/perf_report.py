#!/usr/bin/env python3
"""perf_report — deep on-demand performance report.

Examples:
    python perf_report.py                                   # 7d, all sections
    python perf_report.py --window 30d --focus pair,source
    python perf_report.py --since 2026-04-15
    python perf_report.py --format json > report.json
    python perf_report.py --format vault                    # writes to vault
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.context import Window
from diagnostics import (
    aggregation, profit_zone, drawdown_attr, cohort,
    snipe_analysis, scout_quality, scout_scan_health,
    regression_detector,
)


def _parse_window(args) -> Window:
    if args.since:
        return Window.since(args.since)
    if args.window.endswith("d"):
        return Window.last_days(int(args.window[:-1]))
    if args.window.endswith("h"):
        return Window.last_hours(int(args.window[:-1]))
    raise ValueError(f"Invalid window: {args.window}")


def build_report(window: Window, focus: List[str]) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": window.label,
    }
    # 1. Headline
    headline = aggregation.rollup(window, dimensions=["source"], min_trades=1)
    report["headline"] = {
        "by_source": [r.to_dict() for r in headline],
    }
    # 2. Multi-dim rollup
    report["rollup"] = [r.to_dict() for r in aggregation.rollup(window, focus, min_trades=3)]
    # 3. Profit zones
    report["profit_zones"] = [c.to_dict() for c in
                              profit_zone.top_clusters(window, focus, top_n=10)]
    # 4. Drawdowns
    report["drawdowns"] = [d.to_dict() for d in drawdown_attr.worst_drawdowns(window, top_n=5)]
    report["drawdown_attribution"] = drawdown_attr.attribute(window)
    report["losing_streaks"] = drawdown_attr.losing_streaks(window, min_length=3)
    # 5. MFE + exit distribution
    report["mfe_capture"] = profit_zone.mfe_capture(window, groupby=["source"])
    report["exit_triggers"] = aggregation.exit_trigger_distribution(window, groupby=["source"])
    # 6. Snipe funnel + origin quality
    report["snipe_funnel"] = {k: v.to_dict() for k, v in
                               snipe_analysis.watch_funnel(window, groupby=["origin_type"]).items()}
    report["snipe_quality"] = snipe_analysis.snipe_quality_by_origin(window)
    report["condition_hash_leaderboard"] = snipe_analysis.condition_hash_leaderboard(min_triggers=3)
    # 7. Scout quality
    report["scout"] = {
        "learning_loop": scout_quality.learning_loop_health(window),
        "score_calibration": scout_quality.score_calibration(window),
        "setup_catalog": scout_quality.setup_catalog_performance(window, min_trades=3),
        "pair_bias": scout_quality.pair_bias_analysis(window),
        "blockage": scout_quality.scout_blockage(window),
    }
    # 8. Confluence calibration
    report["confluence_calibration"] = aggregation.confluence_calibration(window)
    # 9. Tuning cohorts
    report["tuning_impacts"] = [c.to_dict() for c in
                                 cohort.all_recent_tuning_impacts(days=14)]
    # 10. Regressions
    report["regressions"] = [a.to_dict() for a in
                              regression_detector.detect_regressions(min_n=5, wr_delta_threshold_pp=10)]
    return report


def format_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Trade Performance Report — window={report['window']}")
    lines.append(f"*Generated {report['generated_at']}*\n")

    lines.append("## Headline (by source)\n")
    lines.append("| source | n | WR | avg pips | total pips | PF |")
    lines.append("|---|---|---|---|---|---|")
    for r in report["headline"]["by_source"]:
        lines.append(f"| {r['key']['source']} | {r['n']} | {r['win_rate']:.1%} | "
                     f"{r['avg_pips']} | {r['total_pips']} | {r['profit_factor']} |")

    lines.append("\n## Profit Zones (top 10)\n")
    for c in report["profit_zones"]:
        lines.append(f"- **#{c['rank']}** {c['key']} — {c['n']} trades, "
                     f"{c['total_pips']}p total, WR {c['win_rate']:.1%}, "
                     f"MFE capture {c['mfe_capture_ratio']:.0%}")

    lines.append("\n## Worst Drawdowns\n")
    for d in report["drawdowns"]:
        lines.append(f"- **{d['depth_pips']}p** over {d['duration_minutes']:.0f}min "
                     f"({d['start']} → {d['end']}) — common: {d['common_features']}")

    lines.append("\n## Snipe Quality by Origin\n")
    lines.append("| origin | n | WR | avg_pips | stale_triggers |")
    lines.append("|---|---|---|---|---|")
    for r in report["snipe_quality"]:
        lines.append(f"| {r['origin']} | {r['n']} | {r['win_rate']:.1%} | "
                     f"{r['avg_pips']:.1f} | {r['stale_triggers']} |")

    lines.append("\n## Scout Learning Loop\n")
    ll = report["scout"]["learning_loop"]
    lines.append(f"- {ll['message']}")
    lines.append(f"- Last populated trade: {ll['last_populated_trade']}")

    lines.append("\n## Scout Setup Catalog\n")
    lines.append("| setup | n | WR | avg_pips | flag |")
    lines.append("|---|---|---|---|---|")
    for s in report["scout"]["setup_catalog"]:
        lines.append(f"| {s['setup_code']} | {s['n']} | {s['win_rate']:.1%} | "
                     f"{s['avg_pips']:.1f} | {s['flag']} |")

    lines.append("\n## Tuning Impact (last 14d)\n")
    for c in report["tuning_impacts"]:
        lines.append(f"- **{c['param']}** {c['tuning_change']} ({c['cutover']}) — "
                     f"WR {c['before_wr']:.1%} → {c['after_wr']:.1%} "
                     f"({c['wr_delta_pp']:+.1f}pp) verdict: {c['verdict']}")

    lines.append("\n## Regressions (today vs 7d baseline)\n")
    for a in report["regressions"]:
        lines.append(f"- **{a['severity'].upper()}** {a['message']}")
    if not report["regressions"]:
        lines.append("- None")

    return "\n".join(lines)


def write_vault(report: Dict[str, Any], md: str) -> str:
    summary = f"Trade perf report — window={report['window']}, {len(report['regressions'])} regressions"
    context_lines = [f"Window: {report['window']}"]
    for r in report["headline"]["by_source"]:
        context_lines.append(f"  {r['key']['source']}: n={r['n']} WR={r['win_rate']:.1%} pips={r['total_pips']}")
    if report["regressions"]:
        context_lines.append("Regressions:")
        for a in report["regressions"][:5]:
            context_lines.append(f"  {a['message']}")
    cmd = [
        "python3", os.path.expanduser("~/Jarvis/knowledge/vault_cli.py"),
        "--agent", "claude-code",
        "--type", "note",
        "--summary", summary,
        "--context", "\n".join(context_lines),
        "--tags", "trade-perf,diagnostics,nightly",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() or result.stderr.strip()


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--window", type=str, default="7d")
    p.add_argument("--since", type=str, default=None)
    p.add_argument("--focus", type=str, default="pair,source",
                   help="Comma-separated rollup dimensions")
    p.add_argument("--format", choices=["md", "json", "vault"], default="md")
    args = p.parse_args(argv)

    window = _parse_window(args)
    focus = [d.strip() for d in args.focus.split(",")]
    report = build_report(window, focus)

    if args.format == "json":
        print(json.dumps(report, default=str, indent=2))
    elif args.format == "vault":
        md = format_markdown(report)
        print(write_vault(report, md))
        print(md)
    else:
        print(format_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
