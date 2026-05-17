"""Param sensitivity sweeps via optimizer.replay."""
from __future__ import annotations

from typing import Any, Dict, List

from optimizer.replay import replay_all_trades
from optimizer.results import load_trade_snapshots
from tuning_config import TUNING, get as tc_get

from diagnostics.context import Window


def sweep_param(param: str, values: List[float], window: Window) -> List[Dict[str, Any]]:
    """Replay live trades (in window) against each param value. Return scored results."""
    if param not in TUNING:
        raise ValueError(f"Unknown param: {param}")
    # Filter snapshots to window
    snaps = [s for s in load_trade_snapshots()
             if s.entry_time and s.entry_time >= window.start.isoformat()
             and s.entry_time < window.end.isoformat()]
    base_params = {k: v["value"] for k, v in TUNING.items()}
    out: List[Dict[str, Any]] = []
    for val in values:
        p = dict(base_params, **{param: val})
        res = replay_all_trades(snaps, p)
        out.append({
            "value": val,
            "score": res.get("win_rate", 0) * 0.7 + res.get("avg_pips", 0) * 0.3,
            "win_rate": res.get("win_rate", 0),
            "avg_pips": res.get("avg_pips", 0),
            "total_pips": res.get("total_pips", 0),
            "remaining": res.get("remaining", 0),
        })
    return out


def local_sensitivity(param: str, window: Window, delta_pct: float = 0.20) -> Dict[str, Any]:
    """Sweep current +/- delta%, +/- 2*delta% — is current a local optimum?"""
    current = tc_get(param)
    if not isinstance(current, (int, float)):
        raise ValueError(f"Param {param} is not numeric (got {type(current)})")
    values = sorted({
        current * (1 - 2 * delta_pct),
        current * (1 - delta_pct),
        current,
        current * (1 + delta_pct),
        current * (1 + 2 * delta_pct),
    })
    curve = sweep_param(param, values, window)
    current_score = next(c["score"] for c in curve if abs(c["value"] - current) < 1e-9)
    is_optimum = all(current_score >= c["score"] for c in curve)
    return {
        "param": param,
        "current_value": current,
        "curve": curve,
        "current_score": current_score,
        "is_local_optimum": is_optimum,
        "best_value": max(curve, key=lambda c: c["score"])["value"],
    }
