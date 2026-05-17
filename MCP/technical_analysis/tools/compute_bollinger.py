"""MCP tool: Compute Bollinger Bands position, squeeze, and bandwidth."""


def register(mcp):
    """Register the compute_bollinger tool on the MCP server."""

    @mcp.tool()
    def compute_bollinger(candles: list) -> dict:
        """Compute Bollinger Bands position, squeeze detection, and bandwidth.

        Args:
            candles: List of Oanda candle dicts with time, mid (o/h/l/c),
                volume, and complete fields. Minimum 25 candles recommended.

        Returns:
            Dict with: upper (float), middle (float), lower (float),
            bandwidth (float), squeeze (bool), position (upper/middle/lower).
        """
        try:
            from Source.indicators import Indicators

            ind = Indicators(candles)
            result = ind.compute_all()
            return result.get("bollinger", {})
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 25,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"Bollinger computation failed: {exc}"}
