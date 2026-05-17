"""MCP tool: replace_order -- cancel and recreate a pending order."""

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the replace_order tool."""

    @mcp.tool()
    def replace_order(order_id: str, new_order: dict) -> dict:
        """Replace (cancel + recreate) a pending order atomically.

        Args:
            order_id: The order identifier to replace.
            new_order: New order specification dict (same format as the
                'order' key in a create-order request body, e.g.
                {"type": "LIMIT", "instrument": "EUR_USD", ...}).

        Returns dict with orderCancelTransaction and orderCreateTransaction.
        """
        try:
            return get_client().replace_order(
                order_id=order_id,
                new_order_body={"order": new_order},
            )
        except OandaAPIError as e:
            return {"error": str(e)}
