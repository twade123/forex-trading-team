"""MCP tool: poll_account_changes."""

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the poll_account_changes tool."""

    @mcp.tool()
    def poll_account_changes(since_transaction_id: str) -> dict:
        """Get incremental account state changes since a transaction ID.

        Used for efficient sync: poll with the last known transaction ID
        to receive only what changed (orders, trades, positions, state).

        Args:
            since_transaction_id: The last known transaction ID. Response
                includes changes that occurred after this ID.

        Returns dict with: changes, state, lastTransactionID.
        """
        try:
            return get_client().get_account_changes(since_transaction_id)
        except OandaAPIError as e:
            return {"error": str(e)}
