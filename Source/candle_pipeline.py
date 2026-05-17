"""
Multi-timeframe candle data pipeline for the trading bot.

Fetches candlestick data from the Oanda API for all configured
instruments across their configured timeframes (M15, H1, H4).
Provides single-call ``fetch_all()`` for the trading cycle and
convenience methods for single-instrument / single-timeframe access.

Rate limiting is handled at the HTTP layer by OandaClient's
RateLimiter — no additional throttling is applied here.

Usage:
    from trading_bot.source.oanda_client import OandaClient
    from trading_bot.source.instrument_config import InstrumentConfig
    from trading_bot.source.candle_pipeline import CandlePipeline

    client = OandaClient()
    config = InstrumentConfig()
    pipeline = CandlePipeline(client, config)

    all_data = pipeline.fetch_all(count=500)
    # -> {"EUR_USD": {"M15": [...], "H1": [...], "H4": [...]}, ...}
"""

from typing import Any, Dict, List, Optional, Union

from .oanda_client import OandaClient
from .instrument_config import InstrumentConfig


class CandlePipeline:
    """Multi-timeframe candle fetching pipeline.

    Delegates to :class:`OandaClient.get_candles` for HTTP calls (which
    already has rate limiting via :class:`RateLimiter`).  Reads
    instrument / timeframe configuration from :class:`InstrumentConfig`.

    Args:
        client: An initialised :class:`OandaClient` instance.
        config: An initialised :class:`InstrumentConfig` instance.
    """

    def __init__(
        self,
        client: Optional[OandaClient] = None,
        config: Optional[InstrumentConfig] = None,
    ):
        self._client = client
        self._config = config

    # ------------------------------------------------------------------
    # Standalone candle processing (no client required)
    # ------------------------------------------------------------------

    @staticmethod
    def process(
        candles: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Filter and normalise raw Oanda candle dicts.

        Returns only complete candles with numeric OHLCV fields cast to
        float/int.  Useful for standalone processing when candles have
        already been fetched externally.

        Args:
            candles: Raw candle dicts from the Oanda API.

        Returns:
            List of processed candle dicts with guaranteed numeric fields.
        """
        processed: List[Dict[str, Any]] = []
        for c in candles:
            if not c.get("complete", False):
                continue
            mid = c.get("mid")
            if not mid:
                continue
            try:
                processed.append({
                    "time": c.get("time", ""),
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": int(c.get("volume", 0)),
                    "complete": True,
                    "mid": mid,
                })
            except (KeyError, TypeError, ValueError):
                continue
        return processed

    # ------------------------------------------------------------------
    # Single-instrument, single-timeframe
    # ------------------------------------------------------------------

    def fetch_candles(
        self,
        instrument: str,
        timeframe: str,
        count: Optional[int] = None,
        from_time=None,
        to_time=None,
        price: str = "M",
    ) -> List[Dict[str, Any]]:
        """Fetch candles for one instrument at one timeframe.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').  Must be in
                the enabled instruments list.
            timeframe: Candle granularity (e.g. 'M15', 'H1', 'H4').
                Must be in the instrument's configured timeframes.
            count: Number of candles.  Defaults to the config's
                ``default_candle_count``.
            from_time: Optional start datetime for time-range fetch.
            to_time: Optional end datetime for time-range fetch.
            price: Price component — 'M' (mid), 'B' (bid), 'A' (ask),
                or combinations like 'MBA'.  Defaults to config's
                ``default_price_component``.

        Returns:
            List of candle dicts from the Oanda API, each containing
            'time', 'volume', 'complete', and a price dict (mid/bid/ask)
            with o, h, l, c values.

        Raises:
            ValueError: If *instrument* is not enabled or *timeframe*
                is not configured for it.
        """
        if self._client is None or self._config is None:
            raise RuntimeError(
                "CandlePipeline requires client and config for fetching. "
                "Use CandlePipeline.process() for standalone candle processing."
            )

        # Validate instrument
        enabled = self._config.get_enabled_instruments()
        if instrument not in enabled:
            raise ValueError(
                f"Instrument '{instrument}' is not enabled. "
                f"Enabled: {enabled}"
            )

        # Validate timeframe
        valid_timeframes = self._config.get_timeframes(instrument)
        if timeframe not in valid_timeframes:
            raise ValueError(
                f"Timeframe '{timeframe}' not configured for "
                f"'{instrument}'. Configured: {valid_timeframes}"
            )

        if count is None:
            count = self._config.get_default_candle_count()
        if price == "M":
            price = self._config.get_default_price_component()

        return self._client.get_candles(
            instrument=instrument,
            granularity=timeframe,
            count=count,
            price=price,
            from_time=from_time,
            to_time=to_time,
        )

    # ------------------------------------------------------------------
    # Single-instrument, all timeframes
    # ------------------------------------------------------------------

    def fetch_multi_timeframe(
        self,
        instrument: str,
        count: Optional[int] = None,
        price: str = "M",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch candles for one instrument across all its configured timeframes.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').
            count: Number of candles per timeframe.  Defaults to
                config's ``default_candle_count``.
            price: Price component.  Defaults to config's
                ``default_price_component``.

        Returns:
            Dict mapping timeframe -> list of candle dicts.
            Example: ``{"M15": [...], "H1": [...], "H4": [...]}``
        """
        timeframes = self._config.get_timeframes(instrument)
        result: Dict[str, List[Dict[str, Any]]] = {}
        for tf in timeframes:
            result[tf] = self.fetch_candles(
                instrument=instrument,
                timeframe=tf,
                count=count,
                price=price,
            )
        return result

    # ------------------------------------------------------------------
    # All instruments, all timeframes
    # ------------------------------------------------------------------

    def fetch_all(
        self,
        count: Optional[int] = None,
        price: str = "M",
    ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        """Fetch candles for ALL enabled instruments across ALL their timeframes.

        This is the primary method for the trading cycle — one call
        retrieves a complete market data snapshot.

        Args:
            count: Number of candles per instrument/timeframe.  Defaults
                to config's ``default_candle_count``.
            price: Price component.  Defaults to config's
                ``default_price_component``.

        Returns:
            Nested dict: instrument -> timeframe -> list of candle dicts.
            Example::

                {
                    "EUR_USD": {"M15": [...], "H1": [...], "H4": [...]},
                    "USD_JPY": {"M15": [...], "H1": [...], "H4": [...]},
                }
        """
        instruments = self._config.get_enabled_instruments()
        result: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for inst in instruments:
            result[inst] = self.fetch_multi_timeframe(
                instrument=inst,
                count=count,
                price=price,
            )
        return result

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def fetch_latest(
        self,
        instrument: str,
        timeframe: str,
        count: int = 1,
        price: str = "M",
    ) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """Fetch the most recent candle(s) for an instrument/timeframe.

        Convenience wrapper around :meth:`fetch_candles` with a small
        default *count*.

        Args:
            instrument: Instrument name.
            timeframe: Candle granularity.
            count: Number of most-recent candles (default 1).
            price: Price component.

        Returns:
            A single candle dict when *count* is 1, otherwise a list.
        """
        candles = self.fetch_candles(
            instrument=instrument,
            timeframe=timeframe,
            count=count,
            price=price,
        )
        if count == 1 and candles:
            return candles[0]
        return candles

    # ------------------------------------------------------------------
    # Stats helper
    # ------------------------------------------------------------------

    @staticmethod
    def get_candle_stats(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compute basic statistics from a list of candles.

        Useful for data validation — checking for gaps, counting
        incomplete candles, verifying time range.

        Args:
            candles: List of candle dicts from the Oanda API.

        Returns:
            Dict with keys:
            - count: Total number of candles.
            - first_time: Timestamp of the first candle.
            - last_time: Timestamp of the last candle.
            - complete_count: Number of candles with complete=True.
            - incomplete_count: Number of candles with complete=False.
        """
        if not candles:
            return {
                "count": 0,
                "first_time": None,
                "last_time": None,
                "complete_count": 0,
                "incomplete_count": 0,
            }

        complete = sum(1 for c in candles if c.get("complete", False))
        return {
            "count": len(candles),
            "first_time": candles[0].get("time"),
            "last_time": candles[-1].get("time"),
            "complete_count": complete,
            "incomplete_count": len(candles) - complete,
        }
