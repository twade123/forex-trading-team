"""MCP tool: Detect candlestick patterns with optional context filtering."""


def register(mcp):
    """Register the detect_candlestick_patterns tool on the MCP server."""

    @mcp.tool()
    def detect_candlestick_patterns(
        candles: list,
        indicators_result: dict = None,
        advanced_result: dict = None,
    ) -> dict:
        """Detect candlestick patterns using TA-Lib with optional context filtering.

        Runs all 61 TA-Lib CDL* pattern recognition functions on the provided
        candle data. When indicator context is supplied, applies trend,
        support/resistance proximity, volume, and ADX regime checks to
        produce confidence-scored results.

        Args:
            candles: List of Oanda candle dicts (min 50 for reliable
                detection) with time, mid (o/h/l/c), volume, complete fields.
            indicators_result: Optional output from core indicators
                (EMA, RSI, MACD, Bollinger, ATR) for context filtering.
            advanced_result: Optional output from advanced indicators
                (ADX, Stochastic, Volume, Fibonacci, VWAP) for regime context.

        Returns:
            Dict with: detected (list of raw patterns at last bar),
            context_filtered (list with confidence scores when context
            provided), summary (total count).
        """
        try:
            from Source.candlestick_patterns import CandlestickPatterns

            cp = CandlestickPatterns(candles)
            raw = cp.get_detected_patterns()

            if indicators_result or advanced_result:
                filtered = cp.get_context_filtered(
                    indicators_result=indicators_result,
                    advanced_result=advanced_result,
                )
            else:
                filtered = raw

            return {
                "detected": raw,
                "context_filtered": filtered,
                "summary": {
                    "total_detected": len(raw),
                    "total_filtered": len(filtered),
                    "high_priority": len(
                        [p for p in raw if p.get("priority") == "high"]
                    ),
                },
            }
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 50,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"Candlestick pattern detection failed: {exc}"}
