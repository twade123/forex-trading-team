"""MCP tool: Detect chart patterns (double top/bottom, H&S, flags, triangles)."""


def register(mcp):
    """Register the detect_chart_patterns tool on the MCP server."""

    @mcp.tool()
    def detect_chart_patterns(
        candles: list,
        volume_sma_ratio: float = 1.2,
    ) -> dict:
        """Detect geometric chart patterns with breakout confirmation.

        Detects reversal patterns (double top/bottom, triple top/bottom,
        head & shoulders) and continuation patterns (bull/bear flags,
        ascending/descending/symmetrical triangles, cup & handle).

        Args:
            candles: List of Oanda candle dicts (min 100 for pattern
                detection) with time, mid (o/h/l/c), volume, complete fields.
            volume_sma_ratio: Volume threshold for breakout confirmation
                (default 1.2).

        Returns:
            Dict with: reversals (list), continuations (list),
            confirmed (list of breakout-confirmed patterns),
            unconfirmed (list).
        """
        try:
            from Source.chart_patterns import ChartPatterns

            cp = ChartPatterns(candles)
            return cp.scan_all(volume_sma_ratio=volume_sma_ratio)
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 100,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"Chart pattern detection failed: {exc}"}
