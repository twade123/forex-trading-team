"""Kronos Filter — pre-validator veto.

Hooked into trading_cycle between cycle_start and validator_call. When
scout fires a cycle, we consult Kronos: if it says OPPOSITE direction with
high confidence, we cancel the cycle before any Anthropic API spend.

Fail-open: any Kronos error/degradation -> PASS (never block scout because
Kronos broke).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional

import pandas as pd

from kronos_inference import KronosInferenceService

logger = logging.getLogger("trading_bot.kronos_filter")


class FilterOutcome(str, Enum):
    PASS = "pass"
    REJECT = "reject"


@dataclass
class FilterDecision:
    outcome: FilterOutcome
    reason: str
    kronos_direction: Optional[str] = None
    kronos_confidence: Optional[float] = None
    used_cache: bool = False


class KronosFilter:
    def __init__(
        self,
        *,
        inference: KronosInferenceService,
        signals_db,
        candle_loader: Callable[[str], pd.DataFrame],
        params_fn: Callable[[], Dict[str, Any]],
        cache_window_minutes: int = 15,
    ):
        self._inference = inference
        self._signals_db = signals_db
        self._candle_loader = candle_loader
        self._params_fn = params_fn
        self._cache_window_minutes = cache_window_minutes

    def check(self, *, pair: str, scout_direction: str) -> FilterDecision:
        if not self._inference.is_ready():
            return FilterDecision(FilterOutcome.PASS, "kronos not ready")
        params = self._params_fn()
        threshold = params["filter_min_confidence_to_reject"]

        # Use recent cached signal when available
        cached = self._signals_db.recent_for_pair(
            pair, within_minutes=self._cache_window_minutes
        )
        if cached:
            kdir = cached.get("direction")
            kconf = cached.get("confidence") or 0.0
            used_cache = True
        else:
            try:
                candles = self._candle_loader(pair)
                fr = self._inference.forecast(
                    pair, candles,
                    pred_len=int(params["pred_len_bars"]),
                    sample_count=int(params["sample_count"]),
                )
            except Exception as exc:
                logger.warning("kronos filter error on %s: %s — fail open", pair, exc)
                return FilterDecision(FilterOutcome.PASS, f"inference error: {exc}")
            if fr is None:
                return FilterDecision(FilterOutcome.PASS, "inference returned None")
            kdir, kconf = fr.direction, fr.confidence
            used_cache = False

        opposite = kdir and kdir != scout_direction
        if opposite and kconf >= threshold:
            return FilterDecision(
                FilterOutcome.REJECT,
                f"kronos opposite ({kdir}) with conf {kconf:.2f} >= {threshold}",
                kronos_direction=kdir, kronos_confidence=kconf, used_cache=used_cache,
            )
        return FilterDecision(
            FilterOutcome.PASS,
            f"kronos dir={kdir} conf={kconf:.2f} (scout={scout_direction})",
            kronos_direction=kdir, kronos_confidence=kconf, used_cache=used_cache,
        )
