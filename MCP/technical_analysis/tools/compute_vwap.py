"""MCP tool: Compute VWAP and intraday directional bias."""


def register(mcp):
    """Register the compute_vwap tool on the MCP server."""

    @mcp.tool()
    def compute_vwap(candles: list) -> dict:
        """Compute Volume-Weighted Average Price and intraday bias.

        VWAP resets daily (standard for intraday bias assessment).

        Args:
            candles: List of Oanda candle dicts with time, mid (o/h/l/c),
                volume, and complete fields. Minimum 20 candles recommended.

        Returns:
            Dict with: vwap (float), price_vs_vwap (above/below),
            distance_pct (float).
        """
        try:
            from Source.indicators_advanced import AdvancedIndicators

            adv = AdvancedIndicators(candles)
            result = adv.compute_all()
            return result.get("vwap", {})
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 20,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"VWAP computation failed: {exc}"}
