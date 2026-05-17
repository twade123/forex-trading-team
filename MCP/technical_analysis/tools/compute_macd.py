"""MCP tool: Compute MACD histogram, crossover, and signal line."""


def register(mcp):
    """Register the compute_macd tool on the MCP server."""

    @mcp.tool()
    def compute_macd(candles: list) -> dict:
        """Compute MACD histogram, signal line crossover, and momentum.

        Args:
            candles: List of Oanda candle dicts with time, mid (o/h/l/c),
                volume, and complete fields. Minimum 35 candles recommended
                (26 slow + 9 signal).

        Returns:
            Dict with: macd (float), signal (float), histogram (float),
            crossover (bullish/bearish/None), momentum (positive/negative).
        """
        try:
            from Source.indicators import Indicators

            ind = Indicators(candles)
            result = ind.compute_all()
            return result.get("macd", {})
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 35,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"MACD computation failed: {exc}"}
