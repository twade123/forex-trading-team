"""MCP tool: list_orders -- query orders with optional pending orders."""

from typing import Optional

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the list_orders tool."""

    @mcp.tool()
    def list_orders(
        instrument: Optional[str] = None,
        state: str = "PENDING",
        include_pending: bool = True,
    ) -> dict:
        """List orders for the account, optionally including pending orders.

        Args:
            instrument: Optional instrument name to filter by.
            state: Order state filter (PENDING, FILLED, TRIGGERED,
                CANCELLED, ALL). Default 'PENDING'.
            include_pending: Also fetch dedicated pending orders list.
                Default True.

        Returns dict with 'orders' list and optionally 'pending_orders' list.
        """
        try:
            client = get_client()
            orders = client.get_orders(instrument=instrument, state=state)
            result = {"orders": orders}
            if include_pending:
                result["pending_orders"] = client.get_pending_orders()
            return result
        except OandaAPIError as e:
            return {"error": str(e)}
