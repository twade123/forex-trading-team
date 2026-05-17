"""Kronos-tuned threat scorer for position_guardian.

Replaces scout's score_threat() for trades where source='kronos_hunter'.

Tuned from indicator_profile_{backtest,live}.csv — 2,834 backtest trades
+ 22 live trades. All parameters live in tuning_config under the
kronos.threat.* namespace so they can be swept by Optuna and live-tuned
via tuning_overrides without a code change.

Loss separators (these drive threat UP):
  - fan_direction flipped against trade              (live Δmed = -1.00)
  - price broken through E100 against direction      (live Δ = +9.3p)
  - price below E55 against direction                (live Δ = +6.6p)
  - separation velocity contracting                  (live Δ = +1.9)

Win profile (these CAP threat so we don't kill winners):
  - fan aligned + price holding E55 + in profit      → score ≤ winner_cap
  - fresh trade (< fresh_trade_bars) + fan aligned   → score ≤ fresh_cap
"""

from __future__ import annotations

from typing import Any, Dict, List

from tuning_config import tc_get_for_trade


def _p(name: str) -> float:
    """Resolve kronos.threat.<name> tunable (source='kronos_hunter')."""
    return tc_get_for_trade(f"threat.{name}", "kronos_hunter")


def score_threat_kronos(
    trade: Dict[str, Any],
    market: Dict[str, Any],
    candles_m1: List[Dict],
    spread_normal: float,
    margin_pct: float = 0.0,
) -> Dict[str, Any]:
    """Kronos threat scorer. Returns dict matching score_threat() contract."""
    reasons: List[str] = []

    direction = trade.get("direction", "buy")
    is_long = direction == "buy"
    pnl_pips = trade.get("pnl_pips", 0.0)
    candles_in = trade.get("candles_in_trade", 0)

    ema = market.get("ema", {})
    emas = ema.get("current_emas", {})
    e21 = emas.get("ema21", 0.0) or 0.0
    e55 = emas.get("ema55", 0.0) or 0.0
    e100 = emas.get("ema100", 0.0) or 0.0
    fan_dir = ema.get("fan_direction", "neutral")
    velocity = ema.get("separation_velocity", 0.0) or 0.0

    price = candles_m1[-1]["close"] if candles_m1 else trade.get("entry_price", 0.0)
    pip = 0.01 if "JPY" in trade.get("pair", "") else 0.0001

    sign = 1.0 if is_long else -1.0
    dist_e55 = sign * (price - e55) / pip if e55 else 0.0
    dist_e100 = sign * (price - e100) / pip if e100 else 0.0

    fan_aligned = (is_long and fan_dir == "bullish") or (not is_long and fan_dir == "bearish")
    fan_flipped = (is_long and fan_dir == "bearish") or (not is_long and fan_dir == "bullish")

    sep_vel_signed = sign * velocity / pip if velocity else 0.0

    bb = market.get("bollinger", {})
    bb_w = (bb.get("upper", 0) - bb.get("lower", 0)) / pip if bb.get("upper") else 0.0
    atr_p = (market.get("atr", {}).get("value", 0) or 0) / pip
    bb_w_atr = bb_w / atr_p if atr_p > 0 else 2.0

    score = 0

    # (1) Fan flipped against trade direction
    if fan_flipped:
        score += int(_p("fan_flipped_score"))
        reasons.append(f"fan flipped {fan_dir} vs {direction}")
    elif fan_dir == "mixed":
        score += int(_p("fan_mixed_score"))
        reasons.append("fan mixed (losing alignment)")

    # (2) Price broken through E100 against trade
    e100_break_pips = _p("e100_break_pips")
    if e100 > 0 and dist_e100 < -e100_break_pips:
        depth = max(0.0, min(abs(dist_e100), 30.0))
        score += int(_p("e100_break_base_score") + _p("e100_break_pips_mult") * depth)
        reasons.append(f"price {dist_e100:.1f}p through E100 against {direction}")

    # (3) Price below E55 in direction
    e55_break_pips = _p("e55_break_pips")
    if e55 > 0 and dist_e55 < -e55_break_pips:
        score += int(_p("e55_break_score"))
        reasons.append(f"price {dist_e55:.1f}p below E55 against {direction}")

    # (4) Separation velocity against trade
    sep_threshold = _p("sep_contract_threshold")
    if sep_vel_signed < sep_threshold:
        score += int(_p("sep_contract_score"))
        reasons.append(f"fan contracting against {direction} ({sep_vel_signed:.2f})")

    # (5) BB compression while losing
    if bb_w_atr < _p("bb_compression_atr") and pnl_pips < 0:
        score += int(_p("bb_compression_score"))
        reasons.append(f"BB compressed ({bb_w_atr:.2f}× ATR) while losing")

    # Check for emergency conditions (applied AFTER winner cap so winners can't
    # suppress a real emergency).
    emergency_flag = False
    spread = market.get("spread", {}).get("current", spread_normal) or spread_normal
    if spread_normal > 0 and spread > spread_normal * 4:
        emergency_flag = True
        reasons.append(f"spread {spread:.1f} = {spread / spread_normal:.1f}× normal")

    # margin_pct is PERCENT (0-100), matching scout's convention at position_guardian
    # line 732. Kronos trades are thesis-driven, so they get the same 95% snipe grace
    # rather than the stricter 80% default (below that is normal healthy margin use).
    if margin_pct > 95.0:
        emergency_flag = True
        reasons.append(f"MARGIN CRITICAL: {margin_pct:.1f}%")

    # Winner protection — cap when trade matches WIN profile
    winner_cap = int(_p("winner_cap_score"))
    winner_e55_min = _p("winner_e55_min_pips")
    if fan_aligned and dist_e55 >= winner_e55_min and pnl_pips >= 0:
        if score > winner_cap:
            score = winner_cap
            reasons.append("winner profile — fan aligned, holding E55, in profit")

    # Fresh-trade patience cap
    fresh_bars = int(_p("fresh_trade_bars"))
    fresh_cap = int(_p("fresh_trade_cap_score"))
    if fan_aligned and candles_in < fresh_bars and not fan_flipped:
        if score > fresh_cap:
            score = fresh_cap

    # Emergency override — applied LAST so winner/fresh caps cannot suppress
    # a real spread-spike or margin-critical condition.
    if emergency_flag:
        score = max(score, 85)

    score = max(0, min(100, int(score)))

    black = int(_p("black_threshold"))
    red = int(_p("red_threshold"))
    yellow = int(_p("yellow_threshold"))
    # Zone in UPPERCASE to match scout's contract (downstream plumbing checks 'BLACK'/'RED'/'YELLOW'/'GREEN')
    if score >= black:
        zone = "BLACK"
    elif score >= red:
        zone = "RED"
    elif score >= yellow:
        zone = "YELLOW"
    else:
        zone = "GREEN"

    return {
        # Scout-compatible keys (required by position_guardian downstream)
        "threat_level": score,
        "zone": zone,
        "reasons": reasons,
        "reasoning": "; ".join(reasons) if reasons else "kronos: nominal",
        "emergency": emergency_flag,
        # Kronos-specific keys
        "score": score,
        "scorer": "kronos",
        "layer_scores": {
            "trend": int(_p("fan_flipped_score")) if fan_flipped else (int(_p("fan_mixed_score")) if fan_dir == "mixed" else 0),
            "structure": int(max(0.0, -dist_e100) * _p("e100_break_pips_mult")) if dist_e100 < 0 else 0,
            "momentum": int(_p("sep_contract_score")) if sep_vel_signed < sep_threshold else 0,
            "emergency": 85 if emergency_flag else 0,
        },
    }
