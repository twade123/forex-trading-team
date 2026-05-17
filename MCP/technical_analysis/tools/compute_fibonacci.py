"""MCP tool: Compute Fibonacci retracement and extension levels."""


def register(mcp):
    """Register the compute_fibonacci tool on the MCP server."""

    @mcp.tool()
    def compute_fibonacci(candles: list) -> dict:
        """Compute Fibonacci retracement and extension levels.

        Detects swing high/low within the lookback window, determines
        trend direction, and calculates retracement (0-100%) and
        extension (127.2%, 161.8%) levels.

        Args:
            candles: List of Oanda candle dicts with time, mid (o/h/l/c),
                volume, and complete fields. Minimum 50 candles recommended.

        Returns:
            Dict with: swing_high, swing_low, trend (up/down),
            retracement_levels (dict), extension_levels (dict),
            nearest_level (level, price, distance_pct).
        """
        try:
            from Source.indicators_advanced import AdvancedIndicators

            adv = AdvancedIndicators(candles)
            result = adv.compute_all()
            return result.get("fibonacci", {})
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 50,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"Fibonacci computation failed: {exc}"}
