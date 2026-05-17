"""
Instrument profiling with live Oanda market data.

Computes spread, ATR, volatility, and per-session range analysis for
any tradeable instrument using live candlestick data from the Oanda API.
Profiles are persisted as JSON for offline access and historical reference.

Usage:
    from trading_bot.source.oanda_client import OandaClient
    from trading_bot.source.account_manager import AccountManager
    from trading_bot.source.instrument_profile import InstrumentProfile

    client = OandaClient()
    am = AccountManager(client)
    am.initialize()

    profile = InstrumentProfile("EUR_USD", client, am)
    profile.compute_profile()
    profile.save_profile()
    print(profile.spread_pips, profile.atr_14_pips, profile.best_session)
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from .oanda_client import OandaClient
    from .account_manager import AccountManager
except ImportError:
    from oanda_client import OandaClient
    from account_manager import AccountManager

# Session windows in Eastern Time (ET) hours.
# Each session is (start_hour, end_hour, crosses_midnight).
_SESSIONS = {
    "Sydney": (17, 2, True),
    "Tokyo": (19, 4, True),
    "London": (3, 12, False),
    "New_York": (8, 17, False),
    "London_NY_Overlap": (8, 12, False),
}

# Base directory for per-instrument data
_DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "Data"
)


class InstrumentProfile:
    """Computes and persists market profile data for a single instrument.

    Fetches H1 candlestick data from the Oanda API to compute:
    - Spread statistics (average, min, max, std) in pips
    - ATR (14 and 50 period) in pips
    - Daily range in pips
    - Volatility classification (low / moderate / high)
    - Per-session average range to identify best trading sessions

    Args:
        instrument: Oanda instrument name (e.g. 'EUR_USD').
        client: Authenticated OandaClient instance.
        account_manager: Initialized AccountManager with cached instrument specs.
    """

    def __init__(
        self,
        instrument: str,
        client: OandaClient,
        account_manager: AccountManager,
    ):
        self.instrument = instrument
        self.client = client
        self.account_manager = account_manager
        self._profile: Dict[str, Any] = {}

        # Per-instrument data directory
        self._profile_dir = os.path.join(_DATA_DIR, instrument)
        self._profile_path = os.path.join(self._profile_dir, "profile.json")

    # ------------------------------------------------------------------
    # Profile computation
    # ------------------------------------------------------------------

    def compute_profile(self, candle_count: int = 200) -> Dict[str, Any]:
        """Compute full instrument profile from live Oanda data.

        Fetches H1 candles with bid+ask (for spread) and mid (for ATR/
        volatility/session analysis), then computes all profile metrics.

        Args:
            candle_count: Number of H1 candles to fetch (default 200).

        Returns:
            The computed profile dict.
        """
        # Get instrument spec from AccountManager cache
        spec = self.account_manager.get_instrument_spec(self.instrument)
        if spec is None:
            raise ValueError(
                f"Instrument '{self.instrument}' not found in "
                f"AccountManager instrument cache. Call am.initialize() first."
            )

        pip_location = int(spec["pipLocation"])
        pip_size = 10 ** pip_location

        # Fetch candles: bid+ask for spread, mid for ATR/volatility
        candles_ba = self.client.get_candles(
            self.instrument,
            granularity="H1",
            count=candle_count,
            price="BA",
        )
        candles_mid = self.client.get_candles(
            self.instrument,
            granularity="H1",
            count=candle_count,
            price="M",
        )

        # Filter to complete candles only
        candles_ba = [c for c in candles_ba if c.get("complete", False)]
        candles_mid = [c for c in candles_mid if c.get("complete", False)]

        # Compute spread stats
        spread_data = self._compute_spread(candles_ba, pip_size)

        # Compute ATR and daily range
        atr_data = self._compute_atr(candles_mid, pip_size)

        # Compute volatility classification
        volatility_data = self._compute_volatility(candles_mid)

        # Compute per-session range analysis
        session_data = self._compute_sessions(candles_mid, pip_size)

        # Assemble profile
        self._profile = {
            "instrument": self.instrument,
            "pip_location": pip_location,
            "display_precision": int(spec.get("displayPrecision", 5)),
            "margin_rate": spec.get("marginRate", "N/A"),
            "trade_units_precision": int(
                spec.get("tradeUnitsPrecision", 0)
            ),
            "spread": spread_data,
            "atr": atr_data,
            "volatility": volatility_data,
            "sessions": session_data,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "candle_count_used": len(candles_mid),
        }

        return self._profile

    def _compute_spread(
        self,
        candles_ba: List[Dict[str, Any]],
        pip_size: float,
    ) -> Dict[str, float]:
        """Compute spread statistics from bid/ask candles.

        Args:
            candles_ba: List of candle dicts with 'bid' and 'ask' keys.
            pip_size: Size of one pip (e.g. 0.0001 for EUR_USD).

        Returns:
            Dict with average_pips, min_pips, max_pips, std_pips.
        """
        spreads = []
        for c in candles_ba:
            if "bid" in c and "ask" in c:
                bid_close = float(c["bid"]["c"])
                ask_close = float(c["ask"]["c"])
                spread_pips = (ask_close - bid_close) / pip_size
                spreads.append(spread_pips)

        if not spreads:
            return {
                "average_pips": 0.0,
                "min_pips": 0.0,
                "max_pips": 0.0,
                "std_pips": 0.0,
            }

        avg = sum(spreads) / len(spreads)
        min_s = min(spreads)
        max_s = max(spreads)
        variance = sum((s - avg) ** 2 for s in spreads) / len(spreads)
        std = variance ** 0.5

        return {
            "average_pips": round(avg, 4),
            "min_pips": round(min_s, 4),
            "max_pips": round(max_s, 4),
            "std_pips": round(std, 4),
        }

    def _compute_atr(
        self,
        candles_mid: List[Dict[str, Any]],
        pip_size: float,
    ) -> Dict[str, float]:
        """Compute Average True Range (ATR) from mid candles.

        ATR is calculated manually:
        true_range = max(high-low, abs(high-prev_close), abs(low-prev_close))
        ATR(N) = simple moving average of last N true ranges.

        Args:
            candles_mid: List of candle dicts with 'mid' key.
            pip_size: Size of one pip.

        Returns:
            Dict with atr_14, atr_50, daily_range_pips (all in pips).
        """
        true_ranges: List[float] = []
        daily_ranges: List[float] = []
        prev_close: Optional[float] = None

        for c in candles_mid:
            mid = c["mid"]
            h = float(mid["h"])
            l = float(mid["l"])
            close = float(mid["c"])

            daily_ranges.append(h - l)

            if prev_close is not None:
                tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
                true_ranges.append(tr)

            prev_close = close

        # ATR-14: mean of last 14 true ranges, in pips
        atr_14 = 0.0
        if len(true_ranges) >= 14:
            atr_14 = sum(true_ranges[-14:]) / 14 / pip_size

        # ATR-50: mean of last 50 true ranges, in pips
        atr_50 = 0.0
        if len(true_ranges) >= 50:
            atr_50 = sum(true_ranges[-50:]) / 50 / pip_size
        elif true_ranges:
            atr_50 = sum(true_ranges) / len(true_ranges) / pip_size

        # Average daily (hourly) range
        avg_range = 0.0
        if daily_ranges:
            avg_range = sum(daily_ranges) / len(daily_ranges) / pip_size

        return {
            "atr_14": round(atr_14, 4),
            "atr_50": round(atr_50, 4),
            "daily_range_pips": round(avg_range, 4),
        }

    def _compute_volatility(
        self,
        candles_mid: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Compute volatility classification from hourly returns.

        Classification thresholds (hourly returns std dev):
        - low: < 0.0003
        - moderate: 0.0003 - 0.0008
        - high: > 0.0008

        Args:
            candles_mid: List of candle dicts with 'mid' key.

        Returns:
            Dict with hourly_returns_std and classification.
        """
        returns: List[float] = []
        prev_close: Optional[float] = None

        for c in candles_mid:
            close = float(c["mid"]["c"])
            if prev_close is not None and prev_close != 0:
                ret = (close - prev_close) / prev_close
                returns.append(ret)
            prev_close = close

        if not returns:
            return {
                "hourly_returns_std": 0.0,
                "classification": "low",
            }

        avg_ret = sum(returns) / len(returns)
        variance = sum((r - avg_ret) ** 2 for r in returns) / len(returns)
        std = variance ** 0.5

        if std < 0.0003:
            classification = "low"
        elif std <= 0.0008:
            classification = "moderate"
        else:
            classification = "high"

        return {
            "hourly_returns_std": round(std, 8),
            "classification": classification,
        }

    def _compute_sessions(
        self,
        candles_mid: List[Dict[str, Any]],
        pip_size: float,
    ) -> Dict[str, Any]:
        """Compute average hourly range per trading session.

        Assigns each candle to sessions based on its timestamp hour (ET),
        then computes the mean high-low range per session in pips.

        Args:
            candles_mid: List of candle dicts with 'mid' and 'time' keys.
            pip_size: Size of one pip.

        Returns:
            Dict with per-session avg_range_pips and best_session.
        """
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")

        # Accumulate ranges per session
        session_ranges: Dict[str, List[float]] = {
            name: [] for name in _SESSIONS
        }

        for c in candles_mid:
            mid = c["mid"]
            h = float(mid["h"])
            l = float(mid["l"])
            candle_range = h - l

            # Parse candle time and convert to ET
            time_str = c.get("time", "")
            try:
                # Oanda RFC3339 format: 2024-01-15T12:00:00.000000000Z
                dt = datetime.fromisoformat(
                    time_str.replace("Z", "+00:00").split(".")[0]
                    + "+00:00"
                )
                dt_et = dt.astimezone(et)
            except (ValueError, AttributeError):
                continue

            hour = dt_et.hour

            # Check each session window
            for session_name, (start, end, crosses_midnight) in _SESSIONS.items():
                if crosses_midnight:
                    # Session spans midnight (e.g., 17:00 - 02:00)
                    if hour >= start or hour < end:
                        session_ranges[session_name].append(candle_range)
                else:
                    # Session within same day (e.g., 08:00 - 17:00)
                    if start <= hour < end:
                        session_ranges[session_name].append(candle_range)

        # Compute averages and find best session
        result: Dict[str, Any] = {}
        best_session = "London_NY_Overlap"
        best_avg = 0.0

        for session_name in _SESSIONS:
            ranges = session_ranges[session_name]
            if ranges:
                avg = sum(ranges) / len(ranges) / pip_size
            else:
                avg = 0.0

            key = f"{session_name.lower()}_avg_range_pips"
            result[key] = round(avg, 4)

            if avg > best_avg:
                best_avg = avg
                best_session = session_name

        result["best_session"] = best_session

        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_profile(self) -> None:
        """Write the computed profile to JSON file.

        Creates the per-instrument data directory if it does not exist.
        File location: Forex Trading Team/Data/{instrument}/profile.json
        """
        if not self._profile:
            raise ValueError(
                "No profile to save. Call compute_profile() first."
            )

        os.makedirs(self._profile_dir, exist_ok=True)

        with open(self._profile_path, "w") as f:
            json.dump(self._profile, f, indent=2)
            f.write("\n")

    def load_profile(self) -> Optional[Dict[str, Any]]:
        """Load a previously saved profile from JSON file.

        Returns:
            Profile dict if file exists, None otherwise.
        """
        try:
            with open(self._profile_path, "r") as f:
                self._profile = json.load(f)
            return self._profile
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            return None

    def get_profile(self) -> Dict[str, Any]:
        """Get the current profile, computing it if empty.

        Returns:
            The profile dict with all computed metrics.
        """
        if not self._profile:
            self.compute_profile()
        return self._profile

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def spread_pips(self) -> float:
        """Average spread in pips."""
        return self._profile.get("spread", {}).get("average_pips", 0.0)

    @property
    def atr_14_pips(self) -> float:
        """ATR(14) in pips."""
        return self._profile.get("atr", {}).get("atr_14", 0.0)

    @property
    def best_session(self) -> str:
        """Session with highest average range."""
        return self._profile.get("sessions", {}).get(
            "best_session", "Unknown"
        )

    @property
    def volatility_class(self) -> str:
        """Volatility classification: low, moderate, or high."""
        return self._profile.get("volatility", {}).get(
            "classification", "unknown"
        )

    @property
    def margin_rate(self) -> str:
        """Instrument margin rate as string."""
        return self._profile.get("margin_rate", "N/A")
