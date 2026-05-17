"""MCP tool: Compute multi-timeframe directional alignment score."""


def register(mcp):
    """Register the compute_multi_timeframe_alignment tool on the MCP server."""

    @mcp.tool()
    def compute_multi_timeframe_alignment(candle_data: dict) -> dict:
        """Compute multi-timeframe directional alignment score.

        Runs all 12 indicators across three timeframes (M15, H1, H4) and
        produces a weighted directional alignment score following the
        TECH-13 hierarchy: H4=0.45, H1=0.35, M15=0.20.

        Args:
            candle_data: Dict mapping timeframe to candle list:
                {"H4": [...candles], "H1": [...candles], "M15": [...candles]}.
                Each candle list should contain Oanda candle dicts with time,
                mid (o/h/l/c), volume, complete fields.

        Returns:
            Dict with: alignment (score, alignment label, per_timeframe
            breakdown), indicators (per-timeframe indicator results),
            summary (human-readable string).
        """
        try:
            from Source.alignment import MultiTimeframeAlignment

            mta = MultiTimeframeAlignment(candle_data)
            mta.analyze()
            return mta.get_snapshot()
        except ValueError as exc:
            return {
                "error": str(exc),
                "timeframes_provided": list(candle_data.keys()),
            }
        except Exception as exc:
            return {"error": f"Multi-timeframe alignment failed: {exc}"}
