"""MCP tool: list_positions -- query open or all positions."""

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the list_positions tool."""

    @mcp.tool()
    def list_positions(open_only: bool = True) -> dict:
        """List positions for the account.

        Args:
            open_only: If True (default), return only positions with
                non-zero units. If False, return all positions including
                those with zero units.

        Returns dict with 'positions' list.
        """
        try:
            client = get_client()
            if open_only:
                positions = client.get_open_positions()
            else:
                positions = client.get_positions()
            return {"positions": positions}
        except OandaAPIError as e:
            return {"error": str(e)}
