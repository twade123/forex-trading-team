"""
Risk asset-class registry with config-driven routing.

Routes instruments to asset-class-specific risk rulesets via regex-based
detection and 3-layer config merging (global_defaults <- asset_class <-
instrument_override).

Mirrors the :class:`TradeValidationRegistry` pattern from
``trade_validator.py``: loads config from YAML, auto-detects asset class
from instrument name, resolves a fully merged ruleset dict, and caches
the classification for repeated lookups.

Usage:
    from trading_bot.source.risk_registry import RiskRegistry

    registry = RiskRegistry()
    asset_class = registry.classify("EUR_USD")       # "forex"
    ruleset = registry.get_ruleset("EUR_USD")         # fully merged dict
    sizing = registry.get_position_sizing_config("EUR_USD")
"""

import copy
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("trading_bot.risk_registry")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict.

    - If both values for a key are dicts the merge recurses.
    - Otherwise the *override* value replaces the *base* value.
    - Neither input is mutated.
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ------------------------------------------------------------------
# RiskRegistry
# ------------------------------------------------------------------

class RiskRegistry:
    """Routes instruments to asset-class-specific risk rulesets.

    Mirrors :class:`TradeValidationRegistry` pattern from ``trade_validator.py``:

    - Loads config from YAML (``risk_asset_classes.yaml``).
    - Auto-detects asset class from instrument name via regex.
    - Resolves risk parameters via 3-layer merging:
      ``global_defaults <- asset_classes.{class} <- instrument_overrides.{instrument}``
    - Caches classification per instrument (stable, no need to re-classify).

    Adding a new asset class requires only YAML changes -- no code modifications.
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        if config_path is None:
            config_path = self._default_config_path()

        self._config_path = config_path
        self._raw: Dict[str, Any] = {}
        self._detection_rules: List[Dict[str, Any]] = []
        self._classification_cache: Dict[str, str] = {}

        self._load_config(config_path)

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    @staticmethod
    def _default_config_path() -> str:
        """Resolve the default config path relative to this file."""
        source_dir = Path(__file__).resolve().parent
        return str(source_dir.parent / "Config" / "risk_asset_classes.yaml")

    def _load_config(self, config_path: str) -> None:
        """Parse YAML config and pre-compile detection regex patterns."""
        try:
            with open(config_path, "r") as f:
                self._raw = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.error("Risk config not found: %s", config_path)
            return
        except yaml.YAMLError as exc:
            logger.error("YAML parse error in %s: %s", config_path, exc)
            return

        # Pre-compile detection patterns sorted by priority
        detection = self._raw.get("asset_class_detection", {})
        rules = []
        for class_name, info in detection.items():
            compiled = [re.compile(p) for p in info.get("patterns", [])]
            rules.append({
                "class_name": class_name,
                "priority": info.get("priority", 999),
                "patterns": compiled,
            })
        rules.sort(key=lambda r: r["priority"])
        self._detection_rules = rules

        logger.info(
            "Loaded risk config from %s: %d asset classes, %d instrument overrides",
            config_path,
            len(self._raw.get("asset_classes", {})),
            len(self._raw.get("instrument_overrides", {})),
        )

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def classify(self, instrument: str) -> str:
        """Auto-detect asset class for *instrument* via regex patterns.

        Results are cached -- an instrument's asset class never changes at
        runtime.

        Args:
            instrument: Oanda-style instrument name (e.g. ``"EUR_USD"``).

        Returns:
            Asset class name (e.g. ``"forex"``, ``"crypto"``).
        """
        if instrument in self._classification_cache:
            logger.debug("classify('%s') -> cache hit: %s",
                         instrument, self._classification_cache[instrument])
            return self._classification_cache[instrument]

        for rule in self._detection_rules:
            for pattern in rule["patterns"]:
                if pattern.search(instrument):
                    self._classification_cache[instrument] = rule["class_name"]
                    logger.debug(
                        "classify('%s') -> %s (pattern %s, priority %d)",
                        instrument, rule["class_name"],
                        pattern.pattern, rule["priority"],
                    )
                    return rule["class_name"]

        # Fallback to forex with a warning
        logger.warning(
            "No asset class matched '%s', defaulting to forex", instrument
        )
        self._classification_cache[instrument] = "forex"
        return "forex"

    # ------------------------------------------------------------------
    # Ruleset resolution
    # ------------------------------------------------------------------

    def get_ruleset(self, instrument: str) -> dict:
        """Return the fully resolved risk ruleset for *instrument*.

        Resolution order (3-layer merge):
            1. ``global_defaults``  (base)
            2. ``asset_classes.{detected_class}``  (overlay)
            3. ``instrument_overrides.{instrument}``  (overlay, if present)

        Returns:
            Merged dict with all applicable risk parameters.
        """
        asset_class = self.classify(instrument)

        # Layer 1: global defaults
        base = copy.deepcopy(self._raw.get("global_defaults", {}))

        # Layer 2: asset class overrides
        class_cfg = self._raw.get("asset_classes", {}).get(asset_class, {})
        merged = deep_merge(base, class_cfg)

        # Layer 3: instrument-specific overrides
        inst_cfg = self._raw.get("instrument_overrides", {}).get(instrument, {})
        if inst_cfg:
            merged = deep_merge(merged, inst_cfg)

        return merged

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def get_position_sizing_config(self, instrument: str) -> dict:
        """Return the ``position_sizing`` section for *instrument*."""
        return self.get_ruleset(instrument).get("position_sizing", {})

    def get_volatility_config(self, instrument: str) -> dict:
        """Return the ``volatility`` section for *instrument*."""
        return self.get_ruleset(instrument).get("volatility", {})

    def get_stop_config(self, instrument: str) -> dict:
        """Return the ``stop_management`` section for *instrument*."""
        return self.get_ruleset(instrument).get("stop_management", {})

    def get_correlation_group(self, instrument: str) -> Optional[str]:
        """Return the correlation group name that *instrument* belongs to.

        Scans all ``correlation_groups`` in the resolved ruleset.  Returns
        ``None`` if the instrument is not in any group.
        """
        ruleset = self.get_ruleset(instrument)
        groups = ruleset.get("correlation_groups", {})
        for group_name, members in groups.items():
            if instrument in members:
                return group_name
        return None

    def list_asset_classes(self) -> list:
        """Return the list of configured asset class names."""
        return list(self._raw.get("asset_classes", {}).keys())
