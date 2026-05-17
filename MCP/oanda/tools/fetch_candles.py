"""MCP tool: fetch_candles."""

from datetime import datetime
from typing import Optional

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the fetch_candles tool."""

    @mcp.tool()
    def fetch_candles(
        instrument: str,
        granularity: str = "H1",
        count: int = 500,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
    ) -> dict:
        """Fetch OHLCV candlestick data for an instrument.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').
            granularity: Candle granularity (S5, M1, M5, M15, H1, H4,
                D, W, M). Default 'H1'.
            count: Number of candles to return (max 5000). Ignored when
                both from_time and to_time are provided.
            from_time: Start of time range as ISO 8601 string.
            to_time: End of time range as ISO 8601 string.

        Returns dict with candles list, each containing time, volume,
        complete flag, and price dicts (mid/bid/ask with o, h, l, c).
        """
        try:
            dt_from = (
                datetime.fromisoformat(from_time) if from_time else None
            )
            dt_to = (
                datetime.fromisoformat(to_time) if to_time else None
            )

            kwargs = {
                "instrument": instrument,
                "granularity": granularity,
                "price": "M",
            }

            if dt_from is not None and dt_to is not None:
                kwargs["from_time"] = dt_from
                kwargs["to_time"] = dt_to
            elif dt_from is not None:
                kwargs["from_time"] = dt_from
                kwargs["count"] = count
            else:
                kwargs["count"] = count
                if dt_to is not None:
                    kwargs["to_time"] = dt_to

            candles = get_client().get_candles(**kwargs)
            return {"candles": candles}
        except OandaAPIError as e:
            return {"error": str(e)}
