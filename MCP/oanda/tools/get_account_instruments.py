"""MCP tool: get_account_instruments."""

from typing import List, Optional

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the get_account_instruments tool."""

    @mcp.tool()
    def get_account_instruments(
        instruments: Optional[List[str]] = None,
    ) -> dict:
        """Get tradeable instrument specs for the account.

        Args:
            instruments: Optional list of instrument names to filter
                (e.g. ['EUR_USD', 'GBP_USD']). If None, returns all.

        Returns dict list with: name, type, displayName, pipLocation,
        displayPrecision, tradeUnitsPrecision, marginRate.
        """
        try:
            return {"instruments": get_client().get_instruments(instruments)}
        except OandaAPIError as e:
            return {"error": str(e)}
