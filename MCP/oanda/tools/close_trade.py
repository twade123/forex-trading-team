"""MCP tool: close_trade -- fully or partially close an open trade."""

from typing import Optional

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the close_trade tool."""

    @mcp.tool()
    def close_trade(
        trade_id: str,
        units: Optional[str] = None,
    ) -> dict:
        """Close an open trade, fully or partially.

        Args:
            trade_id: The trade identifier to close.
            units: Number of units to close (as string). If None,
                closes the entire trade.

        Returns dict with order creation and fill transactions.
        """
        try:
            return get_client().close_trade(
                trade_id=trade_id,
                units=units,
            )
        except OandaAPIError as e:
            return {"error": str(e)}
