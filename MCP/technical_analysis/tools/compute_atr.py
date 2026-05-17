"""MCP tool: Compute Average True Range."""


def register(mcp):
    """Register the compute_atr tool on the MCP server."""

    @mcp.tool()
    def compute_atr(candles: list) -> dict:
        """Compute Average True Range for volatility measurement.

        Args:
            candles: List of Oanda candle dicts with time, mid (o/h/l/c),
                volume, and complete fields. Minimum 20 candles recommended.

        Returns:
            Dict with: value (float, latest ATR reading).
        """
        try:
            from Source.indicators import Indicators

            ind = Indicators(candles)
            result = ind.compute_all()

            atr_data = result.get("atr", {})
            # Strip non-serialisable series
            return {k: v for k, v in atr_data.items() if k != "series"}
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 20,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"ATR computation failed: {exc}"}
