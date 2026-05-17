"""Compare today's rollup to trailing 7d baseline; emit alerts on significant drops.

Pool-managed connections (used indirectly via diagnostics.aggregation and
diagnostics.live_health) are thread-local and cached; we do NOT close them.
Lifecycle is owned by the pool. Matches the pattern established in
diagnostics.context (A1).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from diagnostics.aggregation import rollup
from diagnostics.context import Window
from diagnostics.live_health import check_all


@dataclass
class RegressionAlert:
    dimension: str
    key: Any
    severity: str
    baseline_wr: float
    recent_wr: float
    delta_pp: float
    n_recent: int
    n_baseline: int
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in [
            "dimension", "key", "severity", "baseline_wr", "recent_wr",
            "delta_pp", "n_recent", "n_baseline", "message",
        ]}


def detect_regressions(
    min_n: int = 10,
    wr_delta_threshold_pp: float = 10,
) -> List[RegressionAlert]:
    """Flag dimensions whose recent (24h) WR has dropped vs trailing 7d baseline.

    Rules:
        - Baseline requires >= min_n trades to avoid noise.
        - Recent requires >= min_n // 3 trades (lower bar — 24h is thin).
        - delta_pp <= -wr_delta_threshold_pp → warn.
        - delta_pp <= -2 * wr_delta_threshold_pp → critical.
        - Also pulls critical daemon failures from live_health.check_all().

    Sorted critical → warn, then by delta_pp (biggest drop first).
    """
    today = Window.last_hours(24)
    baseline = Window.last_days(7)
    dims_to_check = [["pair"], ["source"], ["session"], ["setup_code"]]
    alerts: List[RegressionAlert] = []
    for dims in dims_to_check:
        today_rows = {r.key: r for r in rollup(today, dims, min_trades=1)}
        base_rows = {r.key: r for r in rollup(baseline, dims, min_trades=min_n)}
        for key, br in base_rows.items():
            tr = today_rows.get(key)
            if not tr or tr.n < min_n // 3:
                continue
            delta_pp = (tr.win_rate - br.win_rate) * 100
            if delta_pp <= -wr_delta_threshold_pp:
                sev = "critical" if delta_pp <= -2 * wr_delta_threshold_pp else "warn"
                alerts.append(RegressionAlert(
                    dimension=",".join(dims),
                    key=list(key),
                    severity=sev,
                    baseline_wr=br.win_rate,
                    recent_wr=tr.win_rate,
                    delta_pp=delta_pp,
                    n_recent=tr.n,
                    n_baseline=br.n,
                    message=(
                        f"{','.join(dims)}={key}: WR {br.win_rate:.1%} → {tr.win_rate:.1%} "
                        f"({delta_pp:+.1f}pp) on {tr.n} recent / {br.n} baseline"
                    ),
                ))
    # Also pull daemon failures as critical regressions
    for s in check_all():
        if s.severity == "critical":
            alerts.append(RegressionAlert(
                dimension="daemon",
                key=s.component,
                severity="critical",
                baseline_wr=0,
                recent_wr=0,
                delta_pp=0,
                n_recent=0,
                n_baseline=0,
                message=f"DAEMON FAILURE: {s.component} — {s.message}",
            ))
    order = {"critical": 0, "warn": 1}
    alerts.sort(key=lambda a: (order[a.severity], a.delta_pp))
    return alerts
