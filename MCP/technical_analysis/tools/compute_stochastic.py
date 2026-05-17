"""MCP tool: Compute Stochastic Oscillator with crossover detection."""


def register(mcp):
    """Register the compute_stochastic tool on the MCP server."""

    @mcp.tool()
    def compute_stochastic(candles: list) -> dict:
        """Compute Stochastic Oscillator %K, %D with crossover detection.

        Args:
            candles: List of Oanda candle dicts with time, mid (o/h/l/c),
                volume, and complete fields. Minimum 20 candles recommended.

        Returns:
            Dict with: k (float 0-100), d (float 0-100),
            overbought (bool, k > 80), oversold (bool, k < 20),
            crossover (bullish/bearish/None).
        """
        try:
            from Source.indicators_advanced import AdvancedIndicators

            adv = AdvancedIndicators(candles)
            result = adv.compute_all()
            return result.get("stochastic", {})
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 20,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"Stochastic computation failed: {exc}"}
