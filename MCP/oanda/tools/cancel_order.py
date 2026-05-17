"""MCP tool: cancel_order -- cancel a pending order."""

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the cancel_order tool."""

    @mcp.tool()
    def cancel_order(order_id: str) -> dict:
        """Cancel a pending order.

        Args:
            order_id: The order identifier to cancel.

        Returns dict with orderCancelTransaction.
        """
        try:
            return get_client().cancel_order(order_id=order_id)
        except OandaAPIError as e:
            return {"error": str(e)}
