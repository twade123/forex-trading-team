"""MCP tool: Compute Volume SMA ratio and confirmation signal."""


def register(mcp):
    """Register the compute_volume_analysis tool on the MCP server."""

    @mcp.tool()
    def compute_volume_analysis(candles: list) -> dict:
        """Compute Volume SMA ratio and move confirmation signal.

        Uses tick volume (standard for forex) to assess whether the
        current move has conviction relative to the 20-period SMA.

        Args:
            candles: List of Oanda candle dicts with time, mid (o/h/l/c),
                volume, and complete fields. Minimum 25 candles recommended.

        Returns:
            Dict with: current_volume (int), sma (float), ratio (float),
            confirmation (high/low).
        """
        try:
            from Source.indicators_advanced import AdvancedIndicators

            adv = AdvancedIndicators(candles)
            result = adv.compute_all()
            return result.get("volume_sma", {})
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 25,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"Volume analysis failed: {exc}"}
