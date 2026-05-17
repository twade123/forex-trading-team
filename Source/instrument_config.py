"""
Instrument configuration loader for the trading bot.

Loads instrument selections and parameters from instruments.json,
validates configuration, and provides access to instrument settings.
Adding new instruments requires only editing instruments.json — no
code changes needed.

Usage:
    from trading_bot.source.instrument_config import InstrumentConfig

    ic = InstrumentConfig()
    for inst in ic.get_enabled_instruments():
        print(inst, ic.get_timeframes(inst))
"""

import json
import os
from typing import Any, Dict, List

try:
    from . import config
except ImportError:
    import config

# Path to instruments.json relative to this Source directory
_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "Config"
)
_DEFAULT_CONFIG_PATH = os.path.join(_CONFIG_DIR, "instruments.json")

# Valid timeframes (from config.GRANULARITIES)
_VALID_TIMEFRAMES = set(config.GRANULARITIES)


class InstrumentConfigError(Exception):
    """Raised when instruments.json is invalid or missing."""


class InstrumentConfig:
    """Loads and validates instrument configuration from instruments.json.

    Provides read access to instrument selections, timeframes, and
    default parameters. Supports programmatic addition of new
    instruments with automatic JSON persistence.

    Args:
        config_path: Path to instruments.json. Defaults to
            'Forex Trading Team/Config/instruments.json'.
    """

    def __init__(self, config_path: str = _DEFAULT_CONFIG_PATH):
        self._config_path = os.path.abspath(config_path)
        self._data: Dict[str, Any] = {}
        self._load()
        self._validate()

    # ------------------------------------------------------------------
    # Loading and validation
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load instruments.json from disk."""
        try:
            with open(self._config_path, "r") as f:
                self._data = json.load(f)
        except FileNotFoundError:
            raise InstrumentConfigError(
                f"Instrument config not found: {self._config_path}"
            )
        except json.JSONDecodeError as e:
            raise InstrumentConfigError(
                f"Invalid JSON in {self._config_path}: {e}"
            )

    def _validate(self) -> None:
        """Validate the loaded configuration.

        Checks:
        - All instruments in selected_instruments exist in instruments dict.
        - All instruments have an 'enabled' field (bool).
        - All timeframes are valid granularities from config.GRANULARITIES.

        Raises:
            InstrumentConfigError: If validation fails.
        """
        selected = self._data.get("selected_instruments", [])
        instruments = self._data.get("instruments", {})

        if not isinstance(selected, list):
            raise InstrumentConfigError(
                "selected_instruments must be a list"
            )
        if not isinstance(instruments, dict):
            raise InstrumentConfigError(
                "instruments must be a dict"
            )

        for name in selected:
            if name not in instruments:
                raise InstrumentConfigError(
                    f"Instrument '{name}' in selected_instruments "
                    f"but not in instruments dict"
                )

        for name, inst_config in instruments.items():
            if "enabled" not in inst_config:
                raise InstrumentConfigError(
                    f"Instrument '{name}' missing 'enabled' field"
                )
            if not isinstance(inst_config["enabled"], bool):
                raise InstrumentConfigError(
                    f"Instrument '{name}' enabled must be bool, "
                    f"got {type(inst_config['enabled']).__name__}"
                )

            # Validate timeframes (instrument-level or defaults)
            timeframes = inst_config.get(
                "timeframes",
                self._data.get("defaults", {}).get("timeframes", []),
            )
            for tf in timeframes:
                if tf not in _VALID_TIMEFRAMES:
                    raise InstrumentConfigError(
                        f"Instrument '{name}' has invalid timeframe "
                        f"'{tf}'. Valid: {sorted(_VALID_TIMEFRAMES)}"
                    )

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get_enabled_instruments(self) -> List[str]:
        """Get list of enabled instrument names.

        Returns instruments from selected_instruments where
        enabled=True in their config.

        Returns:
            List of instrument name strings (e.g. ['EUR_USD', 'USD_JPY']).
        """
        selected = self._data.get("selected_instruments", [])
        instruments = self._data.get("instruments", {})
        return [
            name for name in selected
            if instruments.get(name, {}).get("enabled", False)
        ]

    def get_instrument_config(self, instrument: str) -> Dict[str, Any]:
        """Get the full configuration dict for an instrument.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').

        Returns:
            Dict with timeframes, notes, selection_rationale, enabled, etc.

        Raises:
            KeyError: If instrument not found in config.
        """
        instruments = self._data.get("instruments", {})
        if instrument not in instruments:
            raise KeyError(
                f"Instrument '{instrument}' not in config. "
                f"Available: {list(instruments.keys())}"
            )
        return instruments[instrument]

    def get_timeframes(self, instrument: str) -> List[str]:
        """Get timeframes for an instrument, falling back to defaults.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').

        Returns:
            List of timeframe strings (e.g. ['M15', 'H1', 'H4']).
        """
        inst_config = self.get_instrument_config(instrument)
        return inst_config.get(
            "timeframes",
            self._data.get("defaults", {}).get("timeframes", []),
        )

    def get_default_candle_count(self) -> int:
        """Get the default candle count from config defaults.

        Returns:
            Integer candle count (e.g. 500).
        """
        return self._data.get("defaults", {}).get(
            "candle_count", config.DEFAULT_CANDLE_COUNT
        )

    def get_default_price_component(self) -> str:
        """Get the default price component from config defaults.

        Returns:
            Price component string (e.g. 'M' for mid).
        """
        return self._data.get("defaults", {}).get("price_component", "M")

    # ------------------------------------------------------------------
    # Mutation (for programmatic instrument addition)
    # ------------------------------------------------------------------

    def add_instrument(
        self, name: str, instrument_config: Dict[str, Any]
    ) -> None:
        """Add a new instrument to the configuration and persist.

        If the instrument already exists, updates its configuration.
        Adds to selected_instruments if not already present.

        Args:
            name: Instrument name (e.g. 'AUD_USD').
            instrument_config: Config dict with at minimum 'enabled' (bool).
                Optional: 'timeframes', 'notes', 'selection_rationale'.

        Raises:
            InstrumentConfigError: If config is invalid after addition.
        """
        instruments = self._data.setdefault("instruments", {})
        instruments[name] = instrument_config

        selected = self._data.setdefault("selected_instruments", [])
        if name not in selected:
            selected.append(name)

        # Re-validate after addition
        self._validate()

        # Persist to disk
        self._save()

    def _save(self) -> None:
        """Write current configuration back to instruments.json."""
        with open(self._config_path, "w") as f:
            json.dump(self._data, f, indent=2)
            f.write("\n")
