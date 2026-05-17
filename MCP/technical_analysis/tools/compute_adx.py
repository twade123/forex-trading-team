"""MCP tool: Compute ADX value and market regime classification."""


def register(mcp):
    """Register the compute_adx tool on the MCP server."""

    @mcp.tool()
    def compute_adx(candles: list) -> dict:
        """Compute ADX value and classify market regime.

        Args:
            candles: List of Oanda candle dicts with time, mid (o/h/l/c),
                volume, and complete fields. Minimum 30 candles recommended.

        Returns:
            Dict with: adx (float), plus_di (float), minus_di (float),
            regime (trending/ranging/mixed), trend_direction
            (bullish/bearish/neutral).
        """
        try:
            from Source.indicators_advanced import AdvancedIndicators

            adv = AdvancedIndicators(candles)
            result = adv.compute_all()
            return result.get("adx", {})
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 30,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"ADX computation failed: {exc}"}
