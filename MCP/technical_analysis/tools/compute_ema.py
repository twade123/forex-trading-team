"""MCP tool: Compute EMA crossovers, trend direction, and 200 EMA position."""


def register(mcp):
    """Register the compute_ema tool on the MCP server."""

    @mcp.tool()
    def compute_ema(candles: list) -> dict:
        """Compute EMA crossovers, trend direction, and 200 EMA position.

        Args:
            candles: List of Oanda candle dicts with time, mid (o/h/l/c),
                volume, and complete fields. Minimum 200 candles recommended
                for EMA 200.

        Returns:
            Dict with: ema_crossovers (set_1 and set_2 crossover signals),
            ema200_trend (direction, distance_pct), emas (period-to-value
            mapping for all computed EMAs).
        """
        try:
            from Source.indicators import Indicators

            ind = Indicators(candles)
            result = ind.compute_all()

            # Build serialisable EMA values (last value per period)
            ema_values = {}
            raw_emas = result.get("emas", {})
            for period, series in raw_emas.items():
                last = series.iloc[-1] if len(series) > 0 else None
                import pandas as pd

                if last is not None and not pd.isna(last):
                    ema_values[int(period)] = round(float(last), 6)
                else:
                    ema_values[int(period)] = None

            return {
                "ema_crossovers": result.get("ema_crossovers", {}),
                "ema200_trend": result.get("ema200_trend", {}),
                "emas": ema_values,
            }
        except ValueError as exc:
            return {
                "error": str(exc),
                "required": 200,
                "provided": len(candles),
            }
        except Exception as exc:
            return {"error": f"EMA computation failed: {exc}"}
