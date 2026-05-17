"""tight_fan_gate.py — pre-entry gate that blocks tight-stale or overextended
Phase 3 cascade entries that backtest shows are net-losing.

Backtested 2026-05-14 against 425 live trades over 30 days:
  - Net pip impact: +197.7p (saved -286.4p of losses, gave up +88.7p of small winners)
  - Catches 61 trades (14% of all entries)
  - Blocked-bucket WR: 52.5% (caught losers are 4x bigger than blocked winners)

Gate rule:
  Phase == 3 AND separation_pct < 0.10% AND (cross3_bars_since >= 20 OR price_extension_atr >= 3.4)

Two firing conditions:
  - mature_stall: fan ordered 20+ bars without widening → stalled cascade, not real impulse
  - overextended: price 3.4+ ATR from 20-bar mean → late entry to fading move

Public API:
  check_tight_fan_gate(candles, direction) -> {
      'block': bool,
      'reason': str,
      'data': {phase, separation_pct, cross3_bars_since, price_extension_atr, fan_state}
  }
"""
import pandas as pd

from scripts.build_cohort_indicators import (
    derive_cross_state,
    derive_fan_state,
    derive_cascade_phase,
    derive_exhaustion,
)
from indicators import Indicators

TIGHT_SEP_MAX = 0.10
MATURE_C3_MIN = 20
OVEREXT_ATR_MIN = 3.4


def check_tight_fan_gate(candles: list, direction: str) -> dict:
    """Evaluate the tight-fan gate against a candle window.

    Returns a dict with 'block' (bool), 'reason' (str), 'data' (dict).
    Fails open on any error — never raises, always returns a dict.
    """
    if not candles or len(candles) < 100:
        return {"block": False, "reason": "insufficient_candles", "data": {}}

    try:
        engine = Indicators(candles)
        engine.compute_emas()
        crosses = derive_cross_state(engine.df)
        fan = derive_fan_state(engine.df)
        phase = derive_cascade_phase(crosses, fan["fan_ordered"])
        ind = engine.compute_all()
        # strip pandas Series so the dict is JSON-safe for logging
        for k, v in list(ind.items()):
            if isinstance(v, dict):
                ind[k] = {sk: sv for sk, sv in v.items() if not isinstance(sv, pd.Series)}
        exhaustion = derive_exhaustion(direction, ind, engine.df)

        sep = float(fan["separation_pct"])
        c3 = crosses["cross3"]["bars_since_last_flip"] or 999
        ext = float(exhaustion["price_extension_atr"])

        is_p3_tight = (phase == 3) and (sep < TIGHT_SEP_MAX)
        is_mature_stall = is_p3_tight and (c3 >= MATURE_C3_MIN)
        is_overextended = is_p3_tight and (ext >= OVEREXT_ATR_MIN)
        block = is_mature_stall or is_overextended

        reasons = []
        if is_mature_stall:
            reasons.append(f"mature_stall(c3={c3})")
        if is_overextended:
            reasons.append(f"overextended(ext={ext})")

        return {
            "block": block,
            "reason": "|".join(reasons) if block else "passed",
            "data": {
                "phase": phase,
                "separation_pct": sep,
                "cross3_bars_since": c3,
                "price_extension_atr": ext,
                "fan_state": fan["fan_state"],
            },
        }
    except Exception as e:
        return {"block": False, "reason": f"gate_error: {type(e).__name__}: {e}", "data": {}}
