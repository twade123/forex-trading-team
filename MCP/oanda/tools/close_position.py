"""MCP tool: close_position -- close long/short side of a position."""

from typing import Optional

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the close_position tool."""

    @mcp.tool()
    def close_position(
        instrument: str,
        long_units: Optional[str] = None,
        short_units: Optional[str] = None,
    ) -> dict:
        """Close the long and/or short side of a position by instrument.

        At least one of long_units or short_units must be provided.
        Use "ALL" to close the entire side, or a specific unit count
        for a partial close.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').
            long_units: Units to close on the long side ("ALL" or count).
            short_units: Units to close on the short side ("ALL" or count).

        Returns dict with position close transactions.
        """
        if long_units is None and short_units is None:
            return {
                "error": "At least one of long_units or short_units required",
                "required": ["long_units or short_units"],
                "provided": {
                    "long_units": long_units,
                    "short_units": short_units,
                },
            }

        try:
            return get_client().close_position(
                instrument=instrument,
                long_units=long_units,
                short_units=short_units,
            )
        except OandaAPIError as e:
            return {"error": str(e)}
