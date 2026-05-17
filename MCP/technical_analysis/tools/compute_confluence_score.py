"""MCP tool: Compute weighted confluence score (0-100) from all signal sources."""


def register(mcp):
    """Register the compute_confluence_score tool on the MCP server."""

    @mcp.tool()
    def compute_confluence_score(
        indicators_result: dict = None,
        advanced_result: dict = None,
        alignment_snapshot: dict = None,
        pattern_results: dict = None,
        news_data: dict = None,
    ) -> dict:
        """Compute weighted confluence score (0-100) from all signal sources.

        Combines 10 signal sources (EMA, RSI, MACD, Bollinger, Volume,
        Stochastic, Multi-TF, Candlestick, Chart, News) into a single
        0-100 score. ADX regime determines weight amplification.

        Args:
            indicators_result: Output from core indicators (EMA, RSI,
                MACD, Bollinger, ATR).
            advanced_result: Output from advanced indicators (ADX,
                Stochastic, Volume, Fibonacci, VWAP).
            alignment_snapshot: Output from multi-timeframe alignment.
            pattern_results: Output from pattern integration scan.
            news_data: Optional news/sentiment data for news scoring
                component (Phase 6 integration).

        Returns:
            Dict with: total_score (0-100), regime (trending/ranging/mixed),
            adx_value (float), direction (bullish/bearish/neutral),
            breakdown (per-source scores), threshold (70), max_possible (100).
        """
        try:
            from Source.confluence_scorer import ConfluenceScorer

            cs = ConfluenceScorer()
            return cs.compute_score(
                indicators_result=indicators_result,
                advanced_result=advanced_result,
                alignment_snapshot=alignment_snapshot,
                pattern_results=pattern_results,
                news_data=news_data,
            )
        except Exception as exc:
            return {"error": f"Confluence scoring failed: {exc}"}
