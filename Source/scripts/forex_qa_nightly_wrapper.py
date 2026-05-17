#!/usr/bin/env python3
"""Nightly QA wrapper invoked by scheduler.py.

Produces:
  - Reports/qa_audit_{date}.md (data sections)
  - dashboard/qa_status.json
  - Vault entry (via perf_report --format vault)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.perf_report import build_report, format_markdown, write_vault
from diagnostics.context import Window

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "Reports")
DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "dashboard")


def main() -> int:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    window = Window.last_hours(24)
    report = build_report(window, focus=["pair", "source"])
    md = format_markdown(report)
    md_path = os.path.join(REPORTS_DIR, f"qa_audit_{date_str}.md")
    with open(md_path, "w") as f:
        f.write(md)
    status_path = os.path.join(DASHBOARD_DIR, "qa_status.json")
    status_summary = {
        "date": date_str,
        "window": window.label,
        "n_trades_by_source": {r["key"]["source"]: r["n"] for r in report["headline"]["by_source"]},
        "n_regressions": len(report["regressions"]),
        "critical_alerts": [a for a in report["regressions"] if a["severity"] == "critical"],
        "scout_learning_loop_broken": report["scout"]["learning_loop"]["broken"],
    }
    with open(status_path, "w") as f:
        json.dump(status_summary, f, indent=2, default=str)
    vault_result = write_vault(report, md)
    print(f"[OK] Nightly QA complete: md={md_path} status={status_path} vault={vault_result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
