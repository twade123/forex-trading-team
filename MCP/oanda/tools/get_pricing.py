"""MCP tool: get_pricing."""

from typing import List

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the get_pricing tool."""

    @mcp.tool()
    def get_pricing(instruments: List[str]) -> dict:
        """Get live bid/ask pricing with liquidity depth for instruments.

        Args:
            instruments: List of instrument names
                (e.g. ['EUR_USD', 'USD_JPY']).

        Returns dict with prices list (each containing bid/ask arrays
        with liquidity buckets, closeoutBid/Ask, instrument, status,
        time) and a time field for next-poll since parameter.
        """
        try:
            return get_client().get_pricing(instruments)
        except OandaAPIError as e:
            return {"error": str(e)}
