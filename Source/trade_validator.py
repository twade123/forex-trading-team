"""
Trade-specific validation engine for the trading bot.

Loads validation rulesets from ``Forex Trading Team/Config/validation_rules.yaml``
and provides auto-routing by discriminator keys so callers do not need to
specify the data type explicitly.  Every data structure flowing through the
pipeline (candles, indicators, patterns, news, trade decisions, orders) has
a registered ruleset with schema checks, range validation, enum checks,
and cross-field rules.

Primary entry points:
- :meth:`TradeValidator.validate` -- validate data and get a
  :class:`ValidationResult`.
- :meth:`TradeTradeValidationRegistry.identify` -- auto-detect data type.
- :meth:`TradeTradeValidationRegistry.list_rulesets` -- list all registered types.

Usage::

    from Source.trade_validator import TradeValidator

    tv = TradeValidator()
    result = tv.validate(candle_data)
    # result.passed, result.issues, result.data_type, etc.
"""

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("trading_bot.trade_validator")

# ======================================================================
# ValidationResult
# ======================================================================


@dataclass
class ValidationResult:
    """Outcome of a single validation run."""

    gate: str  # "gate_1", "gate_2", or "on_demand"
    passed: bool
    issues: List[str]
    elapsed_ms: float
    timestamp: str  # ISO 8601
    data_type: str  # ruleset name (e.g. "candles")
    confidence: float  # 0.0-1.0 (1.0 = clear pass/fail, 0.5 = gray)
    needs_llm_escalation: bool = False  # True when gray-zone heuristic pass


# ======================================================================
# Cross-field rule functions
# ======================================================================


def ohlc_consistency(data: Any) -> List[str]:
    """Validate high >= max(open, close) and low <= min(open, close).

    Accepts a single candle dict or a list of candle dicts.
    """
    issues: List[str] = []
    items = data if isinstance(data, list) else [data]
    for idx, candle in enumerate(items):
        mid = candle.get("mid") if isinstance(candle, dict) else None
        if mid is None:
            continue
        try:
            o = float(mid.get("o", 0))
            h = float(mid.get("h", 0))
            l_ = float(mid.get("l", 0))
            c = float(mid.get("c", 0))
        except (TypeError, ValueError):
            issues.append(f"candle[{idx}]: non-numeric OHLC values")
            continue
        if h < max(o, c):
            issues.append(
                f"candle[{idx}]: high {h} < max(open {o}, close {c})"
            )
        if l_ > min(o, c):
            issues.append(
                f"candle[{idx}]: low {l_} > min(open {o}, close {c})"
            )
    return issues


def timestamp_continuity(data: Any) -> List[str]:
    """Check for gaps > 2x interval, skipping Fri 17:00 ET - Sun 17:00 ET."""
    issues: List[str] = []
    if not isinstance(data, list) or len(data) < 2:
        return issues

    # Extract timestamps
    timestamps = []
    for candle in data:
        t = candle.get("time") if isinstance(candle, dict) else None
        if t is None:
            continue
        try:
            import pandas as pd

            ts = pd.Timestamp(t)
            timestamps.append(ts)
        except Exception:
            continue

    if len(timestamps) < 2:
        return issues

    timestamps.sort()

    # Estimate interval from median of differences
    diffs = [
        (timestamps[i + 1] - timestamps[i]).total_seconds()
        for i in range(len(timestamps) - 1)
        if (timestamps[i + 1] - timestamps[i]).total_seconds() > 0
    ]
    if not diffs:
        return issues

    diffs.sort()
    median_interval = diffs[len(diffs) // 2]

    try:
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
    except ImportError:
        return issues  # Cannot check without timezone support

    for i in range(len(timestamps) - 1):
        gap = (timestamps[i + 1] - timestamps[i]).total_seconds()
        if gap <= 0:
            continue

        # Skip weekend gap check
        try:
            t1_et = timestamps[i].tz_convert(et) if timestamps[i].tzinfo else timestamps[i].tz_localize("UTC").tz_convert(et)
            t2_et = timestamps[i + 1].tz_convert(et) if timestamps[i + 1].tzinfo else timestamps[i + 1].tz_localize("UTC").tz_convert(et)

            # Friday 17:00 ET to Sunday 17:00 ET is the weekend gap
            if t1_et.weekday() == 4 and t1_et.hour >= 17:
                if t2_et.weekday() == 6 and t2_et.hour >= 17:
                    continue
                if t2_et.weekday() == 0:  # Monday
                    continue
        except Exception:
            pass

        if gap > 2 * median_interval:
            issues.append(
                f"timestamp gap {gap:.0f}s at index {i} "
                f"(expected ~{median_interval:.0f}s)"
            )
    return issues


def bollinger_ordering(data: Any) -> List[str]:
    """Validate upper >= middle >= lower for Bollinger Bands."""
    issues: List[str] = []
    bb = data.get("bollinger") if isinstance(data, dict) else None
    if bb is None:
        return issues
    upper = bb.get("upper")
    middle = bb.get("middle")
    lower = bb.get("lower")
    if upper is not None and middle is not None and lower is not None:
        if upper < middle:
            issues.append(
                f"bollinger: upper {upper} < middle {middle}"
            )
        if middle < lower:
            issues.append(
                f"bollinger: middle {middle} < lower {lower}"
            )
    return issues


def fibonacci_ordering(data: Any) -> List[str]:
    """Validate Fibonacci retracement levels are in correct order."""
    issues: List[str] = []
    fib = data.get("fibonacci") if isinstance(data, dict) else None
    if fib is None:
        return issues

    levels_dict = fib.get("retracement_levels") or fib.get("levels")
    if not isinstance(levels_dict, dict):
        return issues

    # Standard retracement level keys (ascending)
    expected_order = [0.0, 0.236, 0.328, 0.382, 0.5, 0.618, 0.786, 1.0]
    prices = []
    for level in expected_order:
        # Keys might be float or string
        price = levels_dict.get(level) or levels_dict.get(str(level))
        if price is not None:
            prices.append((level, float(price)))

    # In an uptrend, prices descend from level 0.0 (swing high) to 1.0
    # In a downtrend, prices ascend from level 0.0 (swing low) to 1.0
    # Check that prices are monotonically ordered (either all ascending
    # or all descending).
    if len(prices) >= 2:
        ascending = all(
            prices[i][1] <= prices[i + 1][1]
            for i in range(len(prices) - 1)
        )
        descending = all(
            prices[i][1] >= prices[i + 1][1]
            for i in range(len(prices) - 1)
        )
        if not ascending and not descending:
            issues.append(
                "fibonacci: retracement levels are not monotonically ordered"
            )
    return issues


def weights_sum_to_one(data: Any) -> List[str]:
    """Validate H4+H1+M15 weights sum to approximately 1.0."""
    issues: List[str] = []
    alignment = data.get("alignment") if isinstance(data, dict) else None
    if alignment is None:
        return issues

    per_tf = alignment.get("per_timeframe")
    if not isinstance(per_tf, dict):
        return issues

    total_weight = 0.0
    for tf in ("H4", "H1", "M15"):
        tf_data = per_tf.get(tf)
        if isinstance(tf_data, dict):
            total_weight += tf_data.get("weight", 0.0)

    if abs(total_weight - 1.0) > 0.01:
        issues.append(
            f"alignment weights sum to {total_weight:.4f}, "
            f"expected ~1.0 (tolerance 0.01)"
        )
    return issues


def action_tradeable_consistency(data: Any) -> List[str]:
    """Validate action != 'hold' requires tradeable=True."""
    issues: List[str] = []
    if not isinstance(data, dict):
        return issues
    action = data.get("action")
    tradeable = data.get("tradeable")
    if action is not None and action != "hold" and tradeable is False:
        issues.append(
            f"action is '{action}' but tradeable is False "
            "(must be True for non-hold actions)"
        )
    return issues


def stop_loss_present(data: Any) -> List[str]:
    """Validate MARKET orders have stopLossOnFill."""
    issues: List[str] = []
    if not isinstance(data, dict):
        return issues
    order_type = data.get("type")
    if order_type == "MARKET" and "stopLossOnFill" not in data:
        issues.append(
            "MARKET order missing stopLossOnFill "
            "(risk: unlimited downside)"
        )
    return issues


# Registry of named cross-field rule functions.
_CROSS_FIELD_FUNCTIONS: Dict[str, Callable[[Any], List[str]]] = {
    "ohlc_consistency": ohlc_consistency,
    "timestamp_continuity": timestamp_continuity,
    "bollinger_ordering": bollinger_ordering,
    "fibonacci_ordering": fibonacci_ordering,
    "weights_sum_to_one": weights_sum_to_one,
    "action_tradeable_consistency": action_tradeable_consistency,
    "stop_loss_present": stop_loss_present,
}


# ======================================================================
# Helper: dot-notation path lookup
# ======================================================================


def _resolve_path(data: Any, path: str) -> Any:
    """Resolve a dot-notation path against nested dicts.

    Returns the value at the path or ``None`` if any key is missing.
    Handles list items by applying the path to each element when the
    current node is a list.
    """
    parts = path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            # Return first element's value (for validation context)
            if len(current) > 0 and isinstance(current[0], dict):
                current = current[0].get(part)
            else:
                return None
        else:
            return None
        if current is None:
            return None
    return current


# ======================================================================
# TradeValidationRuleset
# ======================================================================


class TradeValidationRuleset:
    """Single ruleset loaded from YAML config.

    Encapsulates schema, range, enum, and cross-field checks for one
    data type.  The :meth:`matches` method tests discriminator keys
    against incoming data for auto-routing.

    Args:
        name: Ruleset name (e.g. ``'candles'``).
        config_dict: Parsed YAML dict for this ruleset.
    """

    def __init__(self, name: str, config_dict: Dict[str, Any]) -> None:
        self.name = name
        self._config = config_dict
        self._description = config_dict.get("description", "")

        # Parse sub-sections
        self._discriminator = config_dict.get("discriminator", {})
        self._schema = config_dict.get("schema", {})
        self._ranges = config_dict.get("ranges", {}) or {}
        self._enums = config_dict.get("enums", {}) or {}
        self._cross_field_rules = config_dict.get("cross_field_rules", []) or []
        self._is_list_type = config_dict.get("list_type", False) or self._schema.get("list_type", False)

    def matches(self, data: Any) -> bool:
        """Check if *data* matches this ruleset's discriminator.

        Returns:
            True if the data's top-level keys match the discriminator
            definition.
        """
        if not self._discriminator:
            return False

        # List discriminator: list where items have specific keys
        list_keys = self._discriminator.get("list_with_keys")
        if list_keys:
            if not isinstance(data, list):
                return False
            if len(data) == 0:
                return False
            first = data[0]
            if not isinstance(first, dict):
                return False
            return all(k in first for k in list_keys)

        # Dict discriminator: required top-level keys
        req_keys = self._discriminator.get("required_keys", [])
        if req_keys:
            # For list-type rulesets, check the first item's keys
            if self._is_list_type and isinstance(data, list):
                if len(data) == 0:
                    return False
                first = data[0]
                if not isinstance(first, dict):
                    return False
                return all(k in first for k in req_keys)

            if not isinstance(data, dict):
                return False
            if not all(k in data for k in req_keys):
                return False

            # Optional nested discriminator check
            nested_check = self._discriminator.get("nested_check", {})
            for parent_key, child_keys in nested_check.items():
                parent_val = data.get(parent_key)
                if not isinstance(parent_val, dict):
                    return False
                if not all(ck in parent_val for ck in child_keys):
                    return False

            return True

        return False

    def validate(self, data: Any) -> Tuple[List[str], float]:
        """Run all checks against *data*.

        Returns:
            Tuple of (issues list, confidence score 0.0-1.0).
        """
        issues: List[str] = []

        # --- Schema checks ---
        issues.extend(self._check_schema(data))

        # --- Range checks ---
        issues.extend(self._check_ranges(data))

        # --- Enum checks ---
        issues.extend(self._check_enums(data))

        # --- Cross-field rules ---
        issues.extend(self._check_cross_field(data))

        # Confidence: 1.0 when clearly passing or failing,
        # lower when there are borderline values.
        if len(issues) == 0:
            confidence = 1.0
        elif len(issues) <= 2:
            confidence = 0.7
        else:
            confidence = 0.4

        return issues, confidence

    # --- Schema validation ---

    def _check_schema(self, data: Any) -> List[str]:
        """Check required keys and types."""
        issues: List[str] = []

        # Handle list-type schemas (e.g. candlestick_patterns)
        if self._is_list_type:
            if not isinstance(data, list):
                issues.append(
                    f"expected list, got {type(data).__name__}"
                )
                return issues
            item_keys = self._schema.get("item_keys", [])
            for idx, item in enumerate(data):
                if not isinstance(item, dict):
                    issues.append(f"item[{idx}]: expected dict")
                    continue
                for key in item_keys:
                    if key not in item:
                        issues.append(f"item[{idx}]: missing key '{key}'")
            return issues

        if not isinstance(data, dict):
            issues.append(f"expected dict, got {type(data).__name__}")
            return issues

        # Required top-level keys
        req_keys = self._schema.get("required_keys", [])
        for key in req_keys:
            if key not in data:
                issues.append(f"missing required key '{key}'")

        # Nested required keys
        nested = self._schema.get("nested", {})
        for parent_key, child_schema in nested.items():
            parent_val = data.get(parent_key)
            if parent_val is None:
                continue  # Already caught by required_keys check
            if not isinstance(parent_val, dict):
                issues.append(
                    f"'{parent_key}' should be dict, "
                    f"got {type(parent_val).__name__}"
                )
                continue
            child_req = child_schema.get("required_keys", [])
            for ck in child_req:
                if ck not in parent_val:
                    issues.append(
                        f"'{parent_key}' missing required key '{ck}'"
                    )

        # Type checks
        types = self._schema.get("types", {})
        for key, expected_type_str in types.items():
            val = data.get(key)
            if val is None:
                continue
            expected = {
                "int": int, "float": float, "str": str,
                "bool": bool, "list": list, "dict": dict,
            }.get(expected_type_str)
            if expected and not isinstance(val, expected):
                issues.append(
                    f"'{key}' should be {expected_type_str}, "
                    f"got {type(val).__name__}"
                )

        return issues

    # --- Range validation ---

    def _check_ranges(self, data: Any) -> List[str]:
        """Check numerical fields against min/max bounds."""
        issues: List[str] = []

        for path, bounds in self._ranges.items():
            if not isinstance(bounds, dict):
                continue

            # Handle wildcard paths for list-of-dict fields
            if path.startswith("*."):
                actual_path = path[2:]
                items_to_check = self._collect_wildcard_items(data)
                for item_label, item_data in items_to_check:
                    val = _resolve_path(item_data, actual_path)
                    if val is not None:
                        issue = self._check_single_range(
                            f"{item_label}.{actual_path}", val, bounds
                        )
                        if issue:
                            issues.append(issue)
                continue

            # Handle list-type data -- check each item
            if isinstance(data, list):
                for idx, item in enumerate(data):
                    val = _resolve_path(item, path)
                    if val is not None:
                        issue = self._check_single_range(
                            f"item[{idx}].{path}", val, bounds
                        )
                        if issue:
                            issues.append(issue)
                continue

            # Standard dict path
            val = _resolve_path(data, path)
            if val is None:
                continue
            issue = self._check_single_range(path, val, bounds)
            if issue:
                issues.append(issue)

        return issues

    @staticmethod
    def _check_single_range(
        path: str, val: Any, bounds: Dict[str, Any]
    ) -> Optional[str]:
        """Check a single value against range bounds."""
        try:
            num_val = float(val)
        except (TypeError, ValueError):
            return None  # Non-numeric silently passes range check

        min_val = bounds.get("min")
        max_val = bounds.get("max")
        exclusive_min = bounds.get("exclusive_min", False)

        if min_val is not None:
            if exclusive_min:
                if num_val <= float(min_val):
                    return f"'{path}' = {num_val} <= {min_val} (must be >)"
            else:
                if num_val < float(min_val):
                    return f"'{path}' = {num_val} < {min_val}"

        if max_val is not None:
            if num_val > float(max_val):
                return f"'{path}' = {num_val} > {max_val}"

        return None

    def _collect_wildcard_items(
        self, data: Any
    ) -> List[Tuple[str, Any]]:
        """Collect items from list-valued fields for wildcard range checks."""
        results: List[Tuple[str, Any]] = []
        if isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, list):
                    for idx, item in enumerate(val):
                        if isinstance(item, dict):
                            results.append((f"{key}[{idx}]", item))
        return results

    # --- Enum validation ---

    def _check_enums(self, data: Any) -> List[str]:
        """Check string fields against allowed value sets."""
        issues: List[str] = []

        for path, allowed in self._enums.items():
            if not isinstance(allowed, list):
                continue

            # Handle wildcard paths
            if path.startswith("*."):
                actual_path = path[2:]
                items_to_check = self._collect_wildcard_items(data)
                for item_label, item_data in items_to_check:
                    val = _resolve_path(item_data, actual_path)
                    if val is not None:
                        issue = self._check_single_enum(
                            f"{item_label}.{actual_path}", val, allowed
                        )
                        if issue:
                            issues.append(issue)
                continue

            # Handle list-type data
            if isinstance(data, list):
                for idx, item in enumerate(data):
                    val = _resolve_path(item, path)
                    if val is not None:
                        issue = self._check_single_enum(
                            f"item[{idx}].{path}", val, allowed
                        )
                        if issue:
                            issues.append(issue)
                continue

            val = _resolve_path(data, path)
            if val is None:
                # None/null is acceptable if "null" is in allowed list
                if "null" not in allowed:
                    continue
                else:
                    continue  # None matches "null"
            issue = self._check_single_enum(path, val, allowed)
            if issue:
                issues.append(issue)

        return issues

    @staticmethod
    def _check_single_enum(
        path: str, val: Any, allowed: List[Any]
    ) -> Optional[str]:
        """Check a single value against allowed enum values."""
        # Convert None to "null" for comparison
        compare_val = val if val is not None else "null"
        str_val = str(compare_val) if not isinstance(compare_val, str) else compare_val
        str_allowed = [str(a) for a in allowed]
        if str_val not in str_allowed and compare_val not in allowed:
            return (
                f"'{path}' = '{val}' not in allowed values {allowed}"
            )
        return None

    # --- Cross-field rules ---

    def _check_cross_field(self, data: Any) -> List[str]:
        """Run registered cross-field validation functions."""
        issues: List[str] = []
        for rule_name in self._cross_field_rules:
            func = _CROSS_FIELD_FUNCTIONS.get(rule_name)
            if func is None:
                logger.warning(
                    "Cross-field rule '%s' not registered", rule_name
                )
                continue
            try:
                rule_issues = func(data)
                issues.extend(rule_issues)
            except Exception as exc:
                logger.warning(
                    "Cross-field rule '%s' raised %s: %s",
                    rule_name, type(exc).__name__, exc,
                )
        return issues


# ======================================================================
# TradeValidationRegistry
# ======================================================================


class TradeValidationRegistry:
    """Config-driven registry of validation rulesets.

    Loads ``validation_rules.yaml`` and creates a
    :class:`TradeValidationRuleset` for each entry.  Supports runtime
    registration of additional rulesets and auto-detection of data
    types via discriminator matching.

    Args:
        config_path: Path to the YAML config file.  If ``None``,
            defaults to ``Forex Trading Team/Config/validation_rules.yaml``
            relative to the project root.
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._rulesets: Dict[str, TradeValidationRuleset] = {}

        if config_path is None:
            config_path = self._default_config_path()

        self._config_path = config_path
        self._load_config(config_path)

    @staticmethod
    def _default_config_path() -> str:
        """Resolve the default config path."""
        # Walk up from this file to find the Config directory
        source_dir = Path(__file__).resolve().parent
        config_path = source_dir.parent / "Config" / "validation_rules.yaml"
        return str(config_path)

    def _load_config(self, config_path: str) -> None:
        """Parse the YAML config and create rulesets."""
        try:
            with open(config_path, "r") as f:
                raw = yaml.safe_load(f)
        except FileNotFoundError:
            logger.error("Config file not found: %s", config_path)
            return
        except yaml.YAMLError as exc:
            logger.error("YAML parse error in %s: %s", config_path, exc)
            return

        if not isinstance(raw, dict):
            logger.error("Config must be a YAML mapping, got %s", type(raw))
            return

        for name, ruleset_config in raw.items():
            if isinstance(ruleset_config, dict):
                self._rulesets[name] = TradeValidationRuleset(name, ruleset_config)

        logger.info(
            "Loaded %d validation rulesets from %s",
            len(self._rulesets), config_path,
        )

    def register(self, name: str, ruleset: "TradeValidationRuleset") -> None:
        """Add or replace a ruleset at runtime.

        Args:
            name: Ruleset name.
            ruleset: A :class:`TradeValidationRuleset` instance.
        """
        self._rulesets[name] = ruleset
        logger.info("Registered ruleset '%s'", name)

    def identify(self, data: Any) -> Optional[str]:
        """Auto-detect data type by checking discriminators.

        Iterates all registered rulesets and returns the name of the
        first whose discriminator matches the data.

        Args:
            data: Data to identify.

        Returns:
            Ruleset name or ``None`` if no match.
        """
        for name, ruleset in self._rulesets.items():
            try:
                if ruleset.matches(data):
                    return name
            except Exception:
                continue
        return None

    def validate(
        self, data: Any, data_type: Optional[str] = None
    ) -> "ValidationResult":
        """Validate data against the matching ruleset.

        If *data_type* is not provided, auto-detects via
        :meth:`identify`.

        Args:
            data: Data to validate.
            data_type: Explicit ruleset name, or ``None`` for auto-detect.

        Returns:
            A :class:`ValidationResult`.
        """
        start = time.monotonic()

        if data_type is None:
            data_type = self.identify(data)

        if data_type is None:
            elapsed = (time.monotonic() - start) * 1000
            return ValidationResult(
                gate="on_demand",
                passed=False,
                issues=["Unable to identify data type"],
                elapsed_ms=round(elapsed, 3),
                timestamp=datetime.now(timezone.utc).isoformat(),
                data_type="unknown",
                confidence=0.0,
            )

        ruleset = self._rulesets.get(data_type)
        if ruleset is None:
            elapsed = (time.monotonic() - start) * 1000
            return ValidationResult(
                gate="on_demand",
                passed=False,
                issues=[f"No ruleset registered for '{data_type}'"],
                elapsed_ms=round(elapsed, 3),
                timestamp=datetime.now(timezone.utc).isoformat(),
                data_type=data_type,
                confidence=0.0,
            )

        issues, confidence = ruleset.validate(data)
        elapsed = (time.monotonic() - start) * 1000

        return ValidationResult(
            gate="on_demand",
            passed=len(issues) == 0,
            issues=issues,
            elapsed_ms=round(elapsed, 3),
            timestamp=datetime.now(timezone.utc).isoformat(),
            data_type=data_type,
            confidence=confidence,
        )

    def list_rulesets(self) -> List[str]:
        """Return names of all registered rulesets."""
        return list(self._rulesets.keys())


# ======================================================================
# TradeValidator
# ======================================================================


class TradeValidator:
    """Main validator with registry, gates, and performance metrics.

    Wraps :class:`TradeValidationRegistry` with per-type tracking of pass/fail
    counts, average validation times, and consecutive failure counting
    for circuit-breaker patterns.

    Args:
        config_path: Path to the YAML config file.  If ``None``,
            uses the default Forex Trading Team/Config/validation_rules.yaml.
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        self.registry = TradeValidationRegistry(config_path)
        self._metrics: Dict[str, Any] = {
            "total_validations": 0,
            "total_passed": 0,
            "total_failed": 0,
            "avg_gate1_ms": 0.0,
            "avg_gate2_ms": 0.0,
            "last_10_times": deque(maxlen=10),
            "by_data_type": {},
        }
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Gate 1: Data Integrity Validation
    # ------------------------------------------------------------------

    def validate_data_integrity(
        self,
        candles: List[Any],
        indicators_result: Optional[Dict[str, Any]] = None,
        advanced_result: Optional[Dict[str, Any]] = None,
        pattern_results: Optional[Dict[str, Any]] = None,
        alignment_snapshot: Optional[Dict[str, Any]] = None,
        news_data: Optional[Dict[str, Any]] = None,
        aggregator_output: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """Gate 1: validate all incoming data integrity via the registry.

        Runs each input through its matching registry ruleset and
        aggregates all issues.  Does **not** short-circuit -- every input
        is validated even if earlier ones fail.

        Consecutive failure tracking: if Gate 1 fails 3+ times in a row,
        a CRITICAL log is emitted (possible data feed issue).

        Args:
            candles: List of Oanda candle dicts.
            indicators_result: Output from ``Indicators.compute_all()``.
            advanced_result: Output from ``AdvancedIndicators.compute_all()``.
            pattern_results: Combined pattern scan output (dict with
                ``candlestick_patterns`` and ``chart_patterns`` keys).
            alignment_snapshot: Output from
                ``MultiTimeframeAlignment.get_snapshot()``.
            news_data: Output from ``NewsIntelligence.monitor()``.
            aggregator_output: Output from
                ``IntelligenceAggregator.gather()``.

        Returns:
            A :class:`ValidationResult` with ``gate='gate_1'``.
        """
        start = time.monotonic()
        all_issues: List[str] = []
        confidences: List[float] = []

        # 1. Candles -- validate each candle individually, plus cross-field
        if candles:
            result = self.registry.validate(candles, "candles")
            all_issues.extend(result.issues)
            confidences.append(result.confidence)

        # 2. Indicators core
        if indicators_result is not None:
            result = self.registry.validate(indicators_result, "indicators_core")
            all_issues.extend(result.issues)
            confidences.append(result.confidence)

        # 3. Indicators advanced
        if advanced_result is not None:
            result = self.registry.validate(advanced_result, "indicators_advanced")
            all_issues.extend(result.issues)
            confidences.append(result.confidence)

        # 4. Candlestick patterns (list inside pattern_results)
        if pattern_results is not None:
            cs_patterns = pattern_results.get("candlestick_patterns")
            if cs_patterns is not None:
                result = self.registry.validate(cs_patterns, "candlestick_patterns")
                all_issues.extend(result.issues)
                confidences.append(result.confidence)

        # 5. Chart patterns (dict inside pattern_results)
        if pattern_results is not None:
            ch_patterns = pattern_results.get("chart_patterns")
            if ch_patterns is not None:
                result = self.registry.validate(ch_patterns, "chart_patterns")
                all_issues.extend(result.issues)
                confidences.append(result.confidence)

        # 6. Alignment
        if alignment_snapshot is not None:
            result = self.registry.validate(alignment_snapshot, "alignment")
            all_issues.extend(result.issues)
            confidences.append(result.confidence)

        # 7. News intelligence
        if news_data is not None:
            result = self.registry.validate(news_data, "news_intelligence")
            all_issues.extend(result.issues)
            confidences.append(result.confidence)

        # 8. Intelligence aggregator
        if aggregator_output is not None:
            result = self.registry.validate(aggregator_output, "intelligence_aggregator")
            all_issues.extend(result.issues)
            confidences.append(result.confidence)

        # Overall confidence = weakest link
        overall_confidence = min(confidences) if confidences else 1.0
        passed = len(all_issues) == 0

        elapsed = (time.monotonic() - start) * 1000

        gate_result = ValidationResult(
            gate="gate_1",
            passed=passed,
            issues=all_issues,
            elapsed_ms=round(elapsed, 3),
            timestamp=datetime.now(timezone.utc).isoformat(),
            data_type="pipeline",
            confidence=overall_confidence,
        )

        # Consecutive failure tracking
        if passed:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                logger.critical(
                    "ALERT: %d consecutive Gate 1 failures "
                    "-- possible data feed issue",
                    self._consecutive_failures,
                )

        # Update metrics (skip_consecutive: we already handled above)
        self._update_metrics(gate_result, skip_consecutive=True)

        return gate_result

    # ------------------------------------------------------------------
    # Gate 2: Pre-trade Validation
    # ------------------------------------------------------------------

    # Default risk limits applied when caller does not override.
    DEFAULT_RISK_LIMITS: Dict[str, Any] = {
        "min_confluence": 30,
        "min_rr_ratio": 2.0,
        "max_daily_loss_pct": 5.0,
        "max_concurrent_trades": 3,
        "current_daily_loss_pct": 0.0,
        "current_open_trades": 0,
    }

    def detect_contradictions(
        self,
        indicators_result: Optional[Dict[str, Any]] = None,
        advanced_result: Optional[Dict[str, Any]] = None,
        confluence_output: Optional[Dict[str, Any]] = None,
        market_story: Optional[Dict[str, Any]] = None,
        direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Detect story-aware contradictions in the market read.

        Contradictions are **thesis-relative** — the same indicator values
        mean different things depending on the market story.  RSI 75 in an
        expanding fan is normal; RSI 75 in a peaked fan is exhaustion.

        Returns a dict with ``contradictions`` (list of dicts), each
        containing ``rule``, ``description``, and ``severity``
        (``'warning'`` or ``'critical'``).  Also includes
        ``has_critical`` (bool) and ``reasoning_summary`` (str).

        Args:
            indicators_result: Core indicators output.
            advanced_result: Advanced indicators output.
            confluence_output: Confluence scorer output.
            market_story: The 3-layer market story from ``read_market_story()``.
                Contains ``entry_type``, ``layers`` (trend, structure, momentum),
                ``opportunity_score``, ``has_opportunity``.
            direction: Trade direction (``'buy'`` or ``'sell'``).

        Returns:
            Dict with ``contradictions``, ``has_critical``, and
            ``reasoning_summary``.
        """
        contradictions: List[Dict[str, str]] = []
        story = market_story or {}
        layers = story.get("layers", {})
        trend = layers.get("trend", {})
        structure = layers.get("structure", {})
        momentum = layers.get("momentum", {})
        entry_type = story.get("entry_type", "none")

        fan_state = trend.get("fan_state", "unknown")
        fan_dir = trend.get("fan_direction", "mixed")
        velocity = trend.get("velocity", 0)
        trend_health = trend.get("trend_health", 50)
        reversal_risk = trend.get("reversal_risk", "moderate")

        mom_state = momentum.get("state", "neutral")
        mom_exhausted = momentum.get("exhausted", False)
        mom_significance = momentum.get("significance", "low")

        e100_int = structure.get("e100_interaction", {}).get("interaction", "distant") \
            if isinstance(structure.get("e100_interaction"), dict) else "distant"
        wick_pressure = structure.get("wick_pressure", {}).get("dominant_pressure", "balanced") \
            if isinstance(structure.get("wick_pressure"), dict) else "balanced"
        body_trend = structure.get("body_trend", {}).get("body_trend", "steady") \
            if isinstance(structure.get("body_trend"), dict) else "steady"
        run_state = structure.get("consecutive", {}).get("run_state", "neutral") \
            if isinstance(structure.get("consecutive"), dict) else "neutral"

        conf = confluence_output or {}
        ind = indicators_result or {}
        adv = advanced_result or {}

        # H4 bias from confluence alignment
        alignment_data = conf.get("alignment", {})
        if not isinstance(alignment_data, dict):
            alignment_data = {}
        per_tf = alignment_data.get("per_timeframe", {})
        h4_dir = None
        if isinstance(per_tf, dict):
            h4_dir = (per_tf.get("H4", {}) or {}).get("direction")

        # ================================================================
        # THESIS-RELATIVE CONTRADICTIONS
        # ================================================================

        # ---- Counter-trend reversal checks ----
        if entry_type == "counter_trend_reversal":
            # CRITICAL: fan must be exhausting, NOT strengthening
            if fan_state in ("expanding", "accelerating"):
                contradictions.append({
                    "rule": "CTR-1", "severity": "critical",
                    "description": (
                        f"Counter-trend thesis but fan is {fan_state} — "
                        f"trend still strengthening, reversal is high risk"
                    ),
                })
            # Momentum should show exhaustion
            if not mom_exhausted and mom_significance != "critical":
                if mom_state not in ("exhausted_bull", "exhausted_bear",
                                     "overbought", "oversold"):
                    contradictions.append({
                        "rule": "CTR-2", "severity": "warning",
                        "description": (
                            f"Counter-trend thesis but momentum is '{mom_state}' "
                            f"with no exhaustion — reversal unconfirmed"
                        ),
                    })
            # Wick pressure should support reversal direction
            expected_pressure = "buying" if direction == "buy" else "selling"
            if direction and wick_pressure != expected_pressure and wick_pressure != "balanced":
                contradictions.append({
                    "rule": "CTR-3", "severity": "warning",
                    "description": (
                        f"Counter-trend {direction} but wick pressure is "
                        f"'{wick_pressure}' — structure doesn't confirm reversal"
                    ),
                })

        # ---- Trend continuation checks ----
        elif entry_type == "trend_continuation":
            expected_fan_dir = "bullish" if direction == "buy" else "bearish"
            # Fan should be healthy, not dying
            if fan_state in ("peaked", "contracting"):
                contradictions.append({
                    "rule": "TC-1", "severity": "critical",
                    "description": (
                        f"Trend continuation thesis but fan is {fan_state} — "
                        f"trend is fading, late for continuation entry"
                    ),
                })
            # Fan direction should match trade direction
            if direction and fan_dir != expected_fan_dir and fan_dir != "mixed":
                contradictions.append({
                    "rule": "TC-2", "severity": "critical",
                    "description": (
                        f"Continuation {direction} but fan direction is "
                        f"'{fan_dir}' — trading against the trend"
                    ),
                })
            # Momentum stretched = reduced upside
            if mom_state == "stretched_with_trend":
                contradictions.append({
                    "rule": "TC-3", "severity": "warning",
                    "description": (
                        "Momentum stretched with trend — reduced upside "
                        "potential, consider tighter TP"
                    ),
                })
            # Low velocity = possible fakeout
            if isinstance(velocity, (int, float)) and velocity < 0.003:
                contradictions.append({
                    "rule": "TC-4", "severity": "warning",
                    "description": (
                        f"Trend velocity {velocity:.5f}%/bar is slow (<0.003) — "
                        f"possible fakeout or weak continuation"
                    ),
                })

        # ---- E100 bounce checks ----
        elif entry_type == "e100_bounce":
            # E100 must not be broken
            if e100_int == "broken":
                contradictions.append({
                    "rule": "E100-1", "severity": "critical",
                    "description": (
                        "E100 bounce thesis but E100 is broken — "
                        "structural level lost, thesis invalid"
                    ),
                })
            # E100 should show support/resistance, not just distant
            if e100_int in ("distant", "approaching"):
                contradictions.append({
                    "rule": "E100-2", "severity": "warning",
                    "description": (
                        f"E100 bounce thesis but interaction is '{e100_int}' — "
                        f"price hasn't reached the level yet"
                    ),
                })
            # Wick pressure should confirm bounce direction
            expected_pressure = "buying" if direction == "buy" else "selling"
            if direction and wick_pressure != expected_pressure and wick_pressure != "balanced":
                contradictions.append({
                    "rule": "E100-3", "severity": "warning",
                    "description": (
                        f"E100 bounce {direction} but wick pressure is "
                        f"'{wick_pressure}' — no structural rejection yet"
                    ),
                })

        # ---- Breakout checks ----
        elif entry_type == "breakout":
            range_trend = structure.get("consecutive", {}).get("range_trend", "steady") \
                if isinstance(structure.get("consecutive"), dict) else "steady"
            # Need compression or fresh cross
            if range_trend != "compressing" and fan_state != "just_crossed":
                contradictions.append({
                    "rule": "BRK-1", "severity": "warning",
                    "description": (
                        "Breakout thesis but no range compression or fresh "
                        "EMA cross detected — breakout may be premature"
                    ),
                })
            # Bodies should be growing (conviction)
            if body_trend == "shrinking":
                contradictions.append({
                    "rule": "BRK-2", "severity": "warning",
                    "description": (
                        "Breakout thesis but candle bodies are shrinking — "
                        "weak conviction, false breakout risk"
                    ),
                })

        # ================================================================
        # CONTEXT-INDEPENDENT CHECKS (always flag regardless of thesis)
        # ================================================================

        # H4 strongly opposes trade direction
        if h4_dir and direction:
            opposing = (
                (h4_dir == "bullish" and direction == "sell") or
                (h4_dir == "bearish" and direction == "buy")
            )
            # Only critical if it's not a counter-trend thesis (where opposing H4 is expected)
            if opposing and entry_type != "counter_trend_reversal":
                contradictions.append({
                    "rule": "CTX-1", "severity": "critical",
                    "description": (
                        f"H4 bias is {h4_dir} but trading {direction} — "
                        f"higher timeframe opposes this trade"
                    ),
                })
            elif opposing and entry_type == "counter_trend_reversal":
                # For counter-trend, H4 opposition is expected but worth noting
                contradictions.append({
                    "rule": "CTX-1b", "severity": "warning",
                    "description": (
                        f"Counter-trend against H4 {h4_dir} bias — "
                        f"normal for reversal thesis but limits upside"
                    ),
                })

        # Momentum diverging from trend direction on a with-trend trade
        if mom_state == "diverging_from_trend" and entry_type == "trend_continuation":
            contradictions.append({
                "rule": "CTX-2", "severity": "warning",
                "description": (
                    "Momentum diverging from trend on a continuation trade — "
                    "trend may be losing internal strength"
                ),
            })

        # Exhaustion risk from consecutive candle runs
        if direction == "buy" and run_state == "bull_exhaustion_risk":
            contradictions.append({
                "rule": "CTX-3", "severity": "warning",
                "description": (
                    "5+ consecutive bullish candles — exhaustion risk, "
                    "late entry for a long position"
                ),
            })
        elif direction == "sell" and run_state == "bear_exhaustion_risk":
            contradictions.append({
                "rule": "CTX-3", "severity": "warning",
                "description": (
                    "5+ consecutive bearish candles — exhaustion risk, "
                    "late entry for a short position"
                ),
            })

        # High-impact news (checked from confluence/news data if available)
        news_data = conf.get("news", {}) or {}
        if news_data.get("high_impact_within_30min"):
            contradictions.append({
                "rule": "CTX-4", "severity": "critical",
                "description": "High-impact news event within 30 minutes",
            })

        # Market closed
        session_data = conf.get("session", {}) or {}
        if isinstance(session_data, dict) and session_data.get("market_open") is False:
            contradictions.append({
                "rule": "CTX-5", "severity": "critical",
                "description": "Market is closed",
            })

        # ================================================================
        # Summary
        # ================================================================
        has_critical = any(c["severity"] == "critical" for c in contradictions)

        if not contradictions:
            summary = "No contradictions — thesis is consistent with market data"
        else:
            parts = [
                f"[{c['severity'].upper()}] {c['rule']}: {c['description']}"
                for c in contradictions
            ]
            summary = "; ".join(parts)

        return {
            "contradictions": contradictions,
            "has_critical": has_critical,
            "reasoning_summary": summary,
        }

    def validate_pre_trade(
        self,
        trade_decision: Dict[str, Any],
        news_data: Optional[Dict[str, Any]] = None,
        risk_limits: Optional[Dict[str, Any]] = None,
        indicators_result: Optional[Dict[str, Any]] = None,
        advanced_result: Optional[Dict[str, Any]] = None,
        confluence_output: Optional[Dict[str, Any]] = None,
        market_story: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """Gate 2: pre-trade validation with story-aware contradiction detection.

        Runs risk limit checks and story-aware contradiction detection.
        Contradictions are thesis-relative — the same indicator values mean
        different things depending on the market story context.

        Args:
            trade_decision: Output from ``StrategyEngine.evaluate()``.
            news_data: Output from ``NewsIntelligence.monitor()``.
            risk_limits: Override for :attr:`DEFAULT_RISK_LIMITS`.
            indicators_result: Core indicators for contradiction detection.
            advanced_result: Advanced indicators for contradiction detection.
            confluence_output: Confluence scorer output.
            market_story: The 3-layer market story for thesis-aware checks.

        Returns:
            A :class:`ValidationResult` with ``gate='gate_2'``.
        """
        start = time.monotonic()
        issues: List[str] = []
        limits = {**self.DEFAULT_RISK_LIMITS, **(risk_limits or {})}

        action = trade_decision.get("action", "hold")

        # 1. If hold, nothing to validate
        if action == "hold":
            elapsed = (time.monotonic() - start) * 1000
            result = ValidationResult(
                gate="gate_2",
                passed=True,
                issues=[],
                elapsed_ms=round(elapsed, 3),
                timestamp=datetime.now(timezone.utc).isoformat(),
                data_type="trade_decision",
                confidence=1.0,
                needs_llm_escalation=False,
            )
            self._update_metrics(result)
            return result

        # 2. adjusted_score >= min_confluence
        min_conf = limits["min_confluence"]
        adj_score = trade_decision.get("adjusted_score", 0)
        if adj_score < min_conf:
            issues.append(
                f"adjusted_score {adj_score} < min_confluence {min_conf}"
            )

        # 3. rsi_gate.passed is True
        rsi_gate = trade_decision.get("rsi_gate", {})
        if isinstance(rsi_gate, dict) and not rsi_gate.get("passed", True):
            issues.append(
                f"RSI gate blocked: {rsi_gate.get('reason', 'unknown')}"
            )

        # 4. stop_loss_price is numeric if present
        slp = trade_decision.get("stop_loss_price")
        if slp is not None:
            try:
                float(slp)
            except (TypeError, ValueError):
                issues.append(
                    f"stop_loss_price '{slp}' is not numeric"
                )

        # 5. R:R ratio check
        slp = trade_decision.get("stop_loss_price")
        tpp = trade_decision.get("take_profit_price")
        if slp is not None and tpp is not None:
            try:
                sl = float(slp)
                tp = float(tpp)
                entry = float(trade_decision.get("entry_price", trade_decision.get("adjusted_score", 0)))
                # Try to compute R:R from entry or from the prices directly
                entry_price = trade_decision.get("entry_price")
                if entry_price is not None:
                    entry = float(entry_price)
                    risk = abs(entry - sl)
                    reward = abs(tp - entry)
                    if risk > 0:
                        rr = reward / risk
                        min_rr = limits["min_rr_ratio"]
                        if rr < min_rr:
                            issues.append(
                                f"R:R ratio {rr:.2f} < min {min_rr}"
                            )
            except (TypeError, ValueError):
                pass  # Already caught in check 4

        # 6. Daily loss check
        cur_loss = limits.get("current_daily_loss_pct", 0.0)
        max_loss = limits["max_daily_loss_pct"]
        if cur_loss >= max_loss:
            issues.append(
                f"daily loss {cur_loss}% >= max {max_loss}%"
            )

        # 7. Max concurrent trades
        cur_trades = limits.get("current_open_trades", 0)
        max_trades = limits["max_concurrent_trades"]
        if cur_trades >= max_trades:
            issues.append(
                f"open trades {cur_trades} >= max {max_trades}"
            )

        # 8. High-impact event check
        if news_data is not None:
            events = news_data.get("events", {})
            if isinstance(events, dict) and events.get("high_impact_within_30min", False):
                issues.append(
                    "high-impact event within 30 minutes -- trade blocked"
                )

        # 9. tradeable flag must be True
        if not trade_decision.get("tradeable", False):
            issues.append("tradeable flag is False")

        # 10. Run story-aware contradiction detection
        # Extract direction from trade_decision for thesis-relative checks
        _trade_direction = None
        if action in ("buy", "sell"):
            _trade_direction = action
        elif trade_decision.get("direction") in ("buy", "sell"):
            _trade_direction = trade_decision["direction"]

        contradiction_result = self.detect_contradictions(
            indicators_result=indicators_result,
            advanced_result=advanced_result,
            confluence_output=confluence_output,
            market_story=market_story,
            direction=_trade_direction,
        )

        if contradiction_result["has_critical"]:
            issues.append(
                f"critical contradiction: {contradiction_result['reasoning_summary']}"
            )

        # Compute Gate 2 confidence and LLM escalation
        passed = len(issues) == 0

        # Determine confidence and escalation
        needs_escalation = False
        if not passed:
            # Clearly failed -- no escalation needed
            confidence = 1.0
        else:
            # Passed heuristics -- check for gray zone
            warning_count = sum(
                1 for c in contradiction_result["contradictions"]
                if c["severity"] == "warning"
            )
            near_threshold = 65 <= adj_score <= 75
            weak_direction = (
                conf_direction := (confluence_output or {}).get("direction", ""),
                conf_direction == "neutral" and adj_score > 70,
            )[-1]

            if near_threshold or (warning_count >= 2 and not contradiction_result["has_critical"]) or weak_direction:
                confidence = 0.5
                needs_escalation = True
            else:
                confidence = 1.0

        elapsed = (time.monotonic() - start) * 1000

        gate_result = ValidationResult(
            gate="gate_2",
            passed=passed,
            issues=issues,
            elapsed_ms=round(elapsed, 3),
            timestamp=datetime.now(timezone.utc).isoformat(),
            data_type="trade_decision",
            confidence=confidence,
            needs_llm_escalation=needs_escalation,
        )

        self._update_metrics(gate_result)
        return gate_result

    # ------------------------------------------------------------------
    # Full Pipeline Validation
    # ------------------------------------------------------------------

    def validate_full_pipeline(
        self,
        candles: List[Any],
        indicators_result: Optional[Dict[str, Any]],
        advanced_result: Optional[Dict[str, Any]],
        pattern_results: Optional[Dict[str, Any]],
        trade_decision: Dict[str, Any],
        news_data: Optional[Dict[str, Any]] = None,
        risk_limits: Optional[Dict[str, Any]] = None,
        confluence_output: Optional[Dict[str, Any]] = None,
        alignment_snapshot: Optional[Dict[str, Any]] = None,
        aggregator_output: Optional[Dict[str, Any]] = None,
        historical_performance: Optional[Dict[str, Any]] = None,
        market_story: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Orchestrate Gate 1 + Gate 2 + Gate 3 (historical) with early exit.

        1. Run Gate 1 on all data inputs.
        2. If Gate 1 fails, return immediately (gate_2 = None).
        3. Run Gate 2 on trade decision with all context.
        4. Run Gate 3 (optional) historical backtest performance check.
        5. Compute overall_passed = gate_1.passed AND gate_2.passed.

        Args:
            candles: Raw candle list.
            indicators_result: Core indicators.
            advanced_result: Advanced indicators.
            pattern_results: Pattern scan output.
            trade_decision: Strategy engine output.
            news_data: News intelligence snapshot.
            risk_limits: Risk limit overrides.
            confluence_output: Confluence scorer output.
            alignment_snapshot: Multi-TF alignment.
            aggregator_output: Intelligence aggregator output.
            historical_performance: Optional backtest results dict with
                ``win_rate``, ``profit_factor``, and ``instrument_stats``.
                When provided, Gate 3 adjusts confidence based on
                historical strategy performance.

        Returns:
            Dict with ``gate_1``, ``gate_2``, ``contradictions``,
            ``overall_passed``, ``needs_llm_escalation``,
            ``confidence``, ``borderline``, ``historical_adjustment``,
            and ``total_elapsed_ms``.
        """
        pipeline_start = time.monotonic()

        # Gate 1
        gate_1 = self.validate_data_integrity(
            candles=candles,
            indicators_result=indicators_result,
            advanced_result=advanced_result,
            pattern_results=pattern_results,
            alignment_snapshot=alignment_snapshot,
            news_data=news_data,
            aggregator_output=aggregator_output,
        )

        if not gate_1.passed:
            total_elapsed = (time.monotonic() - pipeline_start) * 1000
            return {
                "gate_1": gate_1,
                "gate_2": None,
                "contradictions": None,
                "overall_passed": False,
                "needs_llm_escalation": False,
                "total_elapsed_ms": round(total_elapsed, 3),
            }

        # Gate 2
        gate_2 = self.validate_pre_trade(
            trade_decision=trade_decision,
            news_data=news_data,
            risk_limits=risk_limits,
            indicators_result=indicators_result,
            advanced_result=advanced_result,
            confluence_output=confluence_output,
            market_story=market_story,
        )

        # Extract contradictions (story-aware)
        _direction = trade_decision.get("action") if trade_decision.get("action") in ("buy", "sell") else \
            trade_decision.get("direction")
        contradictions = self.detect_contradictions(
            indicators_result=indicators_result,
            advanced_result=advanced_result,
            confluence_output=confluence_output,
            market_story=market_story,
            direction=_direction,
        )

        overall_passed = gate_1.passed and gate_2.passed
        total_elapsed = (time.monotonic() - pipeline_start) * 1000

        # Expose confidence and borderline flag for upstream consumers
        gate2_confidence = gate_2.confidence if gate_2 else 1.0

        # Gate 3: Historical performance check (optional, never blocks)
        historical_adjustment: Dict[str, Any] = {}
        if historical_performance and isinstance(historical_performance, dict):
            hist_warnings: List[str] = []
            confidence_multiplier = 1.0

            strategy_winrate = historical_performance.get("win_rate", 0.5)
            strategy_pf = historical_performance.get("profit_factor", 1.0)
            instrument_stats = historical_performance.get("instrument_stats", {})

            if strategy_winrate < 0.4:
                hist_warnings.append(
                    f"Strategy win rate below threshold: {strategy_winrate:.1%}"
                )
                confidence_multiplier *= 0.8

            if strategy_pf < 1.0:
                hist_warnings.append(
                    f"Strategy profit factor below 1.0: {strategy_pf:.2f}"
                )
                confidence_multiplier *= 0.7

            # Instrument-specific stats
            if isinstance(instrument_stats, dict):
                inst_wr = instrument_stats.get("win_rate")
                if inst_wr is not None and inst_wr < 0.35:
                    hist_warnings.append(
                        f"Instrument win rate very low: {inst_wr:.1%}"
                    )
                    confidence_multiplier *= 0.85

            gate2_confidence *= confidence_multiplier

            historical_adjustment = {
                "applied": True,
                "strategy_winrate": strategy_winrate,
                "strategy_profit_factor": strategy_pf,
                "confidence_multiplier": round(confidence_multiplier, 4),
                "warnings": hist_warnings,
            }
            if hist_warnings:
                logger.info(
                    "Gate 3 historical check: %d warnings, confidence adjusted by %.2f",
                    len(hist_warnings), confidence_multiplier,
                )
        else:
            historical_adjustment = {"applied": False}

        borderline = gate2_confidence < 0.7 and gate2_confidence >= 0.5

        return {
            "gate_1": gate_1,
            "gate_2": gate_2,
            "contradictions": contradictions,
            "overall_passed": overall_passed,
            "needs_llm_escalation": gate_2.needs_llm_escalation,
            "confidence": gate2_confidence,
            "borderline": borderline,
            "historical_adjustment": historical_adjustment,
            "total_elapsed_ms": round(total_elapsed, 3),
        }

    # ------------------------------------------------------------------
    # Public validate (on-demand / general purpose)
    # ------------------------------------------------------------------

    def validate(
        self,
        data: Any,
        data_type: Optional[str] = None,
        gate: str = "on_demand",
    ) -> ValidationResult:
        """Validate data and update metrics.

        Args:
            data: Data to validate.
            data_type: Explicit data type, or ``None`` for auto-detect.
            gate: Gate label (e.g. ``'gate_1'``, ``'gate_2'``).

        Returns:
            A :class:`ValidationResult`.
        """
        result = self.registry.validate(data, data_type)
        # Override gate label
        result = ValidationResult(
            gate=gate,
            passed=result.passed,
            issues=result.issues,
            elapsed_ms=result.elapsed_ms,
            timestamp=result.timestamp,
            data_type=result.data_type,
            confidence=result.confidence,
        )
        self._update_metrics(result)
        return result

    def _update_metrics(
        self, result: ValidationResult, *, skip_consecutive: bool = False
    ) -> None:
        """Update internal performance metrics from a validation result.

        Args:
            result: The validation result to record.
            skip_consecutive: When *True*, do not touch the consecutive
                failure counter (caller already handled it).
        """
        m = self._metrics
        m["total_validations"] += 1
        if result.passed:
            m["total_passed"] += 1
            if not skip_consecutive:
                self._consecutive_failures = 0
        else:
            m["total_failed"] += 1
            if not skip_consecutive:
                self._consecutive_failures += 1

        m["last_10_times"].append(result.elapsed_ms)

        # Update per-gate averages
        if result.gate == "gate_1":
            prev = m["avg_gate1_ms"]
            n = m["total_validations"]
            m["avg_gate1_ms"] = prev + (result.elapsed_ms - prev) / n
        elif result.gate == "gate_2":
            prev = m["avg_gate2_ms"]
            n = m["total_validations"]
            m["avg_gate2_ms"] = prev + (result.elapsed_ms - prev) / n

        # Per data-type stats
        dt = result.data_type
        if dt not in m["by_data_type"]:
            m["by_data_type"][dt] = {
                "passed": 0, "failed": 0, "total": 0,
            }
        dt_stats = m["by_data_type"][dt]
        dt_stats["total"] += 1
        if result.passed:
            dt_stats["passed"] += 1
        else:
            dt_stats["failed"] += 1

    def get_metrics(self) -> dict:
        """Return a copy of performance metrics."""
        m = dict(self._metrics)
        m["last_10_times"] = list(self._metrics["last_10_times"])
        m["consecutive_failures"] = self._consecutive_failures
        return m


# ======================================================================
# Backward-compatible aliases (deprecated -- remove in Phase 12)
# ======================================================================

DataValidator = TradeValidator
ValidationRegistry = TradeValidationRegistry
ValidationRuleset = TradeValidationRuleset
