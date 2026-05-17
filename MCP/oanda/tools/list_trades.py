"""MCP tool: list_trades -- query trades with optional open trades."""

from typing import Optional

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the list_trades tool."""

    @mcp.tool()
    def list_trades(
        instrument: Optional[str] = None,
        state: str = "ALL",
        include_open: bool = True,
    ) -> dict:
        """List trades for the account, optionally including open trades.

        Args:
            instrument: Optional instrument name to filter by.
            state: Trade state filter (OPEN, CLOSED, CLOSE_WHEN_TRADEABLE,
                ALL). Default 'ALL'.
            include_open: Also fetch dedicated open trades list.
                Default True.

        Returns dict with 'trades' list and optionally 'open_trades' list.
        """
        try:
            client = get_client()
            trades = client.get_trades(instrument=instrument, state=state)
            result = {"trades": trades}
            if include_open:
                result["open_trades"] = client.get_open_trades()
            return result
        except OandaAPIError as e:
            return {"error": str(e)}
