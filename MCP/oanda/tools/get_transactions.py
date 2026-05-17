"""MCP tool: get_transactions -- unified transaction query (3 modes)."""

from datetime import datetime
from typing import Optional

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the get_transactions tool."""

    @mcp.tool()
    def get_transactions(
        mode: str = "since",
        since_id: Optional[str] = None,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        transaction_type: Optional[str] = None,
    ) -> dict:
        """Query transaction history using one of three modes.

        Modes:
        - "since": Incremental poll since a transaction ID.
            Requires since_id.
        - "time": Query by time range (returns page URLs).
            Requires from_time, optional to_time.
        - "idrange": Query by transaction ID range.
            Requires from_id, to_id.

        Args:
            mode: Query mode ('since', 'time', or 'idrange').
            since_id: Transaction ID for incremental since-poll.
            from_time: Start time as ISO 8601 string (mode='time').
            to_time: End time as ISO 8601 string (mode='time').
            from_id: Starting transaction ID (mode='idrange').
            to_id: Ending transaction ID (mode='idrange').
            transaction_type: Optional CSV of transaction types to filter
                (e.g. 'ORDER_FILL,TRADE_CLOSE').

        Returns dict with transactions list (since/idrange) or page
        URLs and metadata (time mode).
        """
        client = get_client()

        try:
            if mode == "since":
                if since_id is None:
                    return {
                        "error": "mode='since' requires since_id",
                        "required": ["since_id"],
                    }
                txns = client.get_transactions_since(
                    since_id=since_id,
                    transaction_type=transaction_type,
                )
                return {"transactions": txns}

            elif mode == "time":
                if from_time is None:
                    return {
                        "error": "mode='time' requires from_time",
                        "required": ["from_time"],
                    }
                dt_from = datetime.fromisoformat(from_time)
                dt_to = (
                    datetime.fromisoformat(to_time) if to_time else None
                )
                return client.get_transactions_by_time(
                    from_time=dt_from,
                    to_time=dt_to,
                    transaction_type=transaction_type,
                )

            elif mode == "idrange":
                if from_id is None or to_id is None:
                    return {
                        "error": "mode='idrange' requires from_id and to_id",
                        "required": ["from_id", "to_id"],
                    }
                txns = client.get_transactions_idrange(
                    from_id=from_id,
                    to_id=to_id,
                    transaction_type=transaction_type,
                )
                return {"transactions": txns}

            else:
                return {
                    "error": f"Unknown mode: {mode}",
                    "valid_modes": ["since", "time", "idrange"],
                }
        except OandaAPIError as e:
            return {"error": str(e)}
