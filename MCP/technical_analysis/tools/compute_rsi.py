"""MCP tool: Compute RSI value, overbought/oversold condition, and divergence."""


def register(mcp):
    """Register the compute_rsi tool on the MCP server."""

    @mcp.tool()
    def compute_rsi(candles: list) -> dict:
        """Compute RSI value, condition, and divergence detection.

        Args:
            candles: List of Oanda candle dicts with time, mid (o/h/l/c),
                volume, and complete fields. Minimum 30 candles recommended.

        Returns:
            Dict with: value (float 0-100), overbought (bool),
            oversold (bool), divergence (bullish_divergence,
            bearish_divergence, details).
        """
        try:
            from Source.indicators import Indicators

            ind = Indicators(candles)
            result = ind.compute_all()

            rsi_data = result.get("rsi", {})
            divergence = result.get("rsi_divergence", {})

            # Strip non-serialisable series
            rsi_output = {
                k: v for k, v in rsi_data.items() if k != "series"
            }
            rsi_output["divergence"] = divergence

            return rsi_output
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 30,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"RSI computation failed: {exc}"}
