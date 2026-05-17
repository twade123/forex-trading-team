"""MCP tool: fetch_latest_candles (dancing bears)."""

from typing import List

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the fetch_latest_candles tool."""

    @mcp.tool()
    def fetch_latest_candles(candle_specifications: List[str]) -> dict:
        """Get the latest completed candles for multiple instruments at once.

        Also known as "dancing bears". Fetches the most recently completed
        candle for each specification in a single API call.

        Args:
            candle_specifications: List of specs in the format
                '{instrument}:{granularity}:{price_component}'
                e.g. ['EUR_USD:H1:M', 'USD_JPY:M15:M'].

        Returns dict with latestCandles list (one entry per spec).
        """
        try:
            candles = get_client().get_latest_candles(candle_specifications)
            return {"latestCandles": candles}
        except OandaAPIError as e:
            return {"error": str(e)}
