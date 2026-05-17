"""MCP tool: get_account_summary."""

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the get_account_summary tool."""

    @mcp.tool()
    def get_account_summary() -> dict:
        """Get account balance, NAV, margin, P&L, and trade counts.

        Returns dict with: balance, NAV, currency, marginAvailable,
        marginUsed, unrealizedPL, pl, openTradeCount, openPositionCount.
        """
        try:
            return get_client().get_account_summary()
        except OandaAPIError as e:
            return {"error": str(e)}
