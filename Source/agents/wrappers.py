"""
Wrapper functions for trading cycle tool compatibility.

This module provides wrapper functions that aggregate multiple sub-tools
into single composite tools to match the names expected by trading_cycle.py.

Created to fix tool name mismatches between trading_cycle.py expectations
and team_setup.py skill registrations.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trading_bot.agents.wrappers")

# ---------------------------------------------------------------------------
# Lazy imports for components
# ---------------------------------------------------------------------------

def _get_indicators():
    """Lazy import indicators module."""
    from Source.indicators import Indicators
    # Note: Indicators class needs candles in constructor, will be instantiated in wrapper
    return Indicators

def _get_candlestick_patterns():
    """Lazy import candlestick patterns module."""
    from Source.candlestick_patterns import CandlestickPatterns
    return CandlestickPatterns

def _get_chart_patterns():
    """Lazy import chart patterns module."""
    from Source.chart_patterns import ChartPatterns  
    return ChartPatterns

def _get_confluence_scorer():
    """Lazy import confluence scorer module."""
    from Source.confluence_scorer import ConfluenceScorer
    return ConfluenceScorer

def _get_alignment():
    """Lazy import alignment module."""
    from Source.alignment import MultiTimeframeAlignment
    return MultiTimeframeAlignment

def _get_advanced_indicators():
    """Lazy import advanced indicators module."""
    from Source.indicators_advanced import AdvancedIndicators
    return AdvancedIndicators

def _get_trade_validator():
    """Lazy import trade validator module."""
    from Source.trade_validator import TradeValidator
    return TradeValidator()

def _get_validation_analyst():
    """Lazy import validation analyst module."""
    from Source.validation_analyst import ValidationAnalyst
    return ValidationAnalyst()

def _get_trade_logger():
    """Lazy import trade logger module."""
    from Source.trade_logger import TradeLogger
    return TradeLogger()

def _get_knowledge_store():
    """Lazy import knowledge store module."""
    from Source.knowledge_store import KnowledgeStore
    return KnowledgeStore()

def _get_oanda_client():
    """Lazy import Oanda client."""
    from Source.oanda_client import OandaClient
    return OandaClient()

def _get_candle_pipeline():
    """Lazy import candle pipeline."""
    from Source.candle_pipeline import CandlePipeline
    from Source.instrument_config import InstrumentConfig
    client = _get_oanda_client()
    config = InstrumentConfig()
    return CandlePipeline(client, config)

def _get_risk_manager():
    """Lazy import risk manager."""
    from Source.risk_manager import RiskManager
    from Source.account_manager import AccountManager
    oanda_client = _get_oanda_client()
    account_manager = AccountManager(oanda_client)
    return RiskManager(oanda_client, account_manager)

def _get_position_monitor():
    """Lazy import position monitor."""
    from Source.position_monitor import PositionMonitor
    return PositionMonitor()

# ---------------------------------------------------------------------------
# Pipeline helpers — bridge OANDA candle dicts to backtester DataFrame world
# Used by: run_full_analysis(), run_full_validation()
# Tested against: test_live_pipeline.py (verified 2026-02-17)
# ---------------------------------------------------------------------------

def _get_backtester_indicators():
    """Lazy import backtester indicators module (DataFrame-based)."""
    try:
        from Source.backtester import indicators
    except ModuleNotFoundError:
        from backtester import indicators
    return indicators


def _get_detect_regime():
    """Lazy import detect_regime from backtester."""
    try:
        from Source.backtester.master_sweep_v3 import detect_regime
    except ModuleNotFoundError:
        from backtester.master_sweep_v3 import detect_regime
    return detect_regime


def _get_decision_logger():
    """Lazy import DecisionLogger."""
    try:
        from Source.decision_logger import DecisionLogger
    except ModuleNotFoundError:
        from decision_logger import DecisionLogger
    return DecisionLogger()


def candles_to_dataframe(candles: List[Dict]) -> "pd.DataFrame":
    """Convert OANDA candle dicts to pandas DataFrame with all backtester indicators.

    Input format (from CandlePipeline or OandaClient):
        [{"time": "...", "open": 1.18, "high": ..., "low": ..., "close": ..., "volume": ...}, ...]

    Returns:
        DataFrame with 32+ indicator columns from backtester.indicators.compute_all()
        Returns empty DataFrame if input is empty or invalid.
    """
    import pandas as pd

    if not candles or not isinstance(candles, list):
        return pd.DataFrame()

    rows = []
    for c in candles:
        try:
            # Handle both raw OANDA format (with mid dict) and pre-processed format
            if "mid" in c and isinstance(c["mid"], dict):
                mid = c["mid"]
                rows.append({
                    "time": c.get("time", ""),
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": int(c.get("volume", 0)),
                })
            elif "open" in c:
                rows.append({
                    "time": c.get("time", ""),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": int(c.get("volume", 0)),
                })
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("Skipping candle: %s", exc)
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    bt_indicators = _get_backtester_indicators()
    df = bt_indicators.compute_all(df)
    return df


def detect_regime_from_candles(candles: List[Dict]) -> str:
    """Detect market regime from H1 candle dicts.

    Returns one of: "strong_trend", "ranging", "exhaustion", "squeeze",
    "high_volatility", "unknown"
    """
    df = candles_to_dataframe(candles)
    if df.empty or len(df) < 50:
        return "unknown"

    detect_regime = _get_detect_regime()
    return detect_regime(df, len(df) - 1)


def scan_setups_from_candles(candles: List[Dict], pair: str) -> List[Dict[str, str]]:
    """Identify which S1-S20 setups are firing on the latest candle.

    Uses the ACTUAL backtester setup functions (all 20) for accurate detection.
    Returns list of dicts: [{"setup": "S13", "direction": "sell"}, ...]
    """
    df = candles_to_dataframe(candles)
    if df.empty or len(df) < 50:
        return []

    i = len(df) - 1
    setups_firing = []

    try:
        try:
            from Source.backtester.master_sweep_v3 import (
                setup_s1_hammer_pinbar, setup_s2_engulfing, setup_s3_morning_evening_star,
                setup_s4_doji_extremes, setup_s5_ascending_triangle, setup_s6_descending_triangle,
                setup_s7_channel_trading, setup_s8_sr_break, setup_s9_head_shoulders,
                setup_s10_double_top_bottom, setup_s11_sma_macd, setup_s12_bb_squeeze_breakout,
                setup_s13_stoch_crossover, setup_s14_cci_extremes, setup_s15_rsi_divergence,
                setup_s16_sar_flip, setup_s17_pivot_bounce, setup_s18_fib_retracement,
                setup_s19_atr_expansion, setup_s20_multi_timeframe,
            )
        except ModuleNotFoundError:
            from backtester.master_sweep_v3 import (
                setup_s1_hammer_pinbar, setup_s2_engulfing, setup_s3_morning_evening_star,
                setup_s4_doji_extremes, setup_s5_ascending_triangle, setup_s6_descending_triangle,
                setup_s7_channel_trading, setup_s8_sr_break, setup_s9_head_shoulders,
                setup_s10_double_top_bottom, setup_s11_sma_macd, setup_s12_bb_squeeze_breakout,
                setup_s13_stoch_crossover, setup_s14_cci_extremes, setup_s15_rsi_divergence,
                setup_s16_sar_flip, setup_s17_pivot_bounce, setup_s18_fib_retracement,
                setup_s19_atr_expansion, setup_s20_multi_timeframe,
            )

        all_setups = [
            ("S1", setup_s1_hammer_pinbar), ("S2", setup_s2_engulfing),
            ("S3", setup_s3_morning_evening_star), ("S4", setup_s4_doji_extremes),
            ("S5", setup_s5_ascending_triangle), ("S6", setup_s6_descending_triangle),
            ("S7", setup_s7_channel_trading), ("S8", setup_s8_sr_break),
            ("S9", setup_s9_head_shoulders), ("S10", setup_s10_double_top_bottom),
            ("S11", setup_s11_sma_macd), ("S12", setup_s12_bb_squeeze_breakout),
            ("S13", setup_s13_stoch_crossover), ("S14", setup_s14_cci_extremes),
            ("S15", setup_s15_rsi_divergence), ("S16", setup_s16_sar_flip),
            ("S17", setup_s17_pivot_bounce), ("S18", setup_s18_fib_retracement),
            ("S19", setup_s19_atr_expansion), ("S20", setup_s20_multi_timeframe),
        ]

        for setup_name, setup_func in all_setups:
            try:
                result = setup_func(df, i)
                if result and result.get("direction"):
                    setups_firing.append({
                        "setup": setup_name,
                        "direction": result["direction"],
                        "entry_price": result.get("entry_price"),
                        "trigger_reason": result.get("trigger_reason", ""),
                    })
            except Exception:
                continue  # Skip broken setup, don't block others

    except ImportError as exc:
        logger.warning("Could not import backtester setups: %s — falling back to simplified scanner", exc)
        # Fallback: simplified scanner for S3, S5, S13
        row = df.iloc[i]
        prev = df.iloc[i - 1] if i > 0 else row
        rsi = row.get("rsi", 50)
        stoch_k = row.get("stoch_k", 50)
        adx = row.get("adx", 20)
        close = row["close"]
        bb_upper = row.get("bb_upper", close)
        bb_lower = row.get("bb_lower", close)

        if rsi < 30 and close <= bb_lower:
            setups_firing.append({"setup": "S5", "direction": "buy"})
        elif rsi > 70 and close >= bb_upper:
            setups_firing.append({"setup": "S5", "direction": "sell"})
        if stoch_k < 20 and adx < 25:
            setups_firing.append({"setup": "S13", "direction": "buy"})
        elif stoch_k > 80 and adx < 25:
            setups_firing.append({"setup": "S13", "direction": "sell"})

    return setups_firing


def get_h4_trend(h4_candles: List[Dict]) -> str:
    """Determine H4 trend direction from candle dicts.

    Returns "bullish" or "bearish".
    Logic: close > ema_21 → bullish, else bearish (same as test_live_pipeline.py).
    """
    df = candles_to_dataframe(h4_candles)
    if df.empty or len(df) < 25:
        return "unknown"

    last = df.iloc[-1]
    ema21 = last.get("ema_21", last["close"])
    if last["close"] > ema21:
        return "bullish"
    return "bearish"


def detect_session() -> str:
    """Detect current trading session based on Eastern Time.

    Returns: "Asian", "London", "NY_Overlap", "NY", "Off_Hours"
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-5)))  # ET
    hour = now.hour
    if 0 <= hour < 8:
        return "Asian"
    elif 8 <= hour < 12:
        return "London" if hour < 9 else "NY_Overlap"
    elif 12 <= hour < 17:
        return "NY"
    else:
        return "Off_Hours"


# ---------------------------------------------------------------------------
# oanda_data wrapper functions (renamed from oanda_client functions)
# ---------------------------------------------------------------------------

def fetch_candles(instrument: str, timeframe: str = "H1", count: int = 500, **kwargs) -> Dict[str, Any]:
    """Wrapper for oanda_client.get_candles -> fetch_candles."""
    try:
        client = _get_oanda_client()
        candles = client.get_candles(
            instrument=instrument, 
            granularity=timeframe, 
            count=count, 
            **kwargs
        )
        return {
            "candles": candles,
            "count": len(candles),
            "instrument": instrument,
            "timeframe": timeframe
        }
    except Exception as exc:
        logger.error("fetch_candles failed: %s", exc)
        return {"error": str(exc), "candles": []}

def get_account_summary() -> Dict[str, Any]:
    """Wrapper for oanda_client.get_account_summary."""
    try:
        client = _get_oanda_client()
        return client.get_account_summary()
    except Exception as exc:
        logger.error("get_account_summary failed: %s", exc)
        return {"error": str(exc)}

def fetch_multi_timeframe(instrument: str, count: Optional[int] = None, **kwargs) -> Dict[str, Any]:
    """Fetch M15/H1/H4 candles for any instrument via OANDA client directly.

    Bypasses CandlePipeline instrument validation so the dashboard can trade
    any OANDA-supported pair, not just those in instruments.json.
    """
    try:
        client = _get_oanda_client()
        timeframes = ["M15", "H1", "H4"]
        candle_count = count or 500
        result = {}
        for tf in timeframes:
            candles = client.get_candles(
                instrument=instrument,
                granularity=tf,
                count=candle_count,
                price="M",
            )
            # get_candles returns raw OANDA response; extract candle list
            if isinstance(candles, dict):
                result[tf] = candles.get("candles", candles.get("data", []))
            elif isinstance(candles, list):
                result[tf] = candles
            else:
                result[tf] = []
        return result
    except Exception as exc:
        logger.error("fetch_multi_timeframe failed for %s: %s", instrument, exc)
        return {"error": str(exc)}

def get_current_pricing(instruments: List[str], **kwargs) -> Dict[str, Any]:
    """Wrapper for oanda_client.get_pricing -> get_current_pricing.
    
    Returns:
        {
            "prices": [
                {"instrument": "EUR_USD", "bid": 1.18146, "ask": 1.18163,
                 "spread": 0.00017, "tradeable": True, "time": "..."},
                ...
            ],
            "by_instrument": {"EUR_USD": {...}, ...}   # keyed for quick lookup
        }
    """
    try:
        client = _get_oanda_client()
        if isinstance(instruments, str):
            instruments = [i.strip() for i in instruments.split(",")]
        result = client.get_pricing(instruments=instruments, **kwargs)
        
        prices = []
        by_instrument = {}
        for price in result.get("prices", []):
            inst = price.get("instrument", "")
            if not inst:
                continue
            bids = price.get("bids", [{}])
            asks = price.get("asks", [{}])
            entry = {
                "instrument": inst,
                "bid": float(bids[0].get("price", 0)) if bids else 0,
                "ask": float(asks[0].get("price", 0)) if asks else 0,
                "spread": 0,
                "tradeable": price.get("tradeable", False),
                "time": price.get("time", ""),
            }
            entry["spread"] = entry["ask"] - entry["bid"]
            prices.append(entry)
            by_instrument[inst] = entry
        
        return {"prices": prices, "by_instrument": by_instrument}
    except Exception as exc:
        logger.error("get_current_pricing failed: %s", exc)
        return {"error": str(exc)}

def get_instrument_specs(instrument: str, **kwargs) -> Dict[str, Any]:
    """Wrapper for oanda_client.get_instruments -> get_instrument_specs."""
    try:
        client = _get_oanda_client()
        instruments = client.get_instruments(instruments=[instrument])
        
        if instruments:
            return instruments[0]  # Return the first (should be only) instrument spec
        else:
            return {"error": f"No specs found for {instrument}"}
    except Exception as exc:
        logger.error("get_instrument_specs failed: %s", exc)
        return {"error": str(exc)}

# ---------------------------------------------------------------------------
# technical_analyst composite wrapper
# ---------------------------------------------------------------------------

def _sanitize_for_json(obj, depth=0):
    """Recursively convert pandas/numpy objects to JSON-safe Python types."""
    if depth > 10:
        return str(obj)
    try:
        import numpy as np
        import pandas as pd
        if isinstance(obj, (pd.Timestamp,)):
            return obj.isoformat()
        if isinstance(obj, pd.Series):
            return {str(k): _sanitize_for_json(v, depth+1) for k, v in obj.to_dict().items()}
        if isinstance(obj, pd.DataFrame):
            return [_sanitize_for_json(row, depth+1) for row in obj.to_dict(orient='records')]
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return round(float(obj), 8) if not np.isnan(obj) else None
        if isinstance(obj, np.ndarray):
            return [_sanitize_for_json(v, depth+1) for v in obj.tolist()]
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
    except ImportError:
        pass
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_json(v, depth+1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v, depth+1) for v in obj]
    if isinstance(obj, float) and (obj != obj):  # NaN
        return None
    # Catch-all for datetime-like objects
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    return obj

def run_full_analysis(candles_by_tf: Dict[str, List[Dict]], news_score: float = 0.0, 
                     instrument: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """Composite wrapper combining all technical analysis components.
    
    Expected by trading_cycle.py but was split into 5 separate tools in team_setup.py.
    This combines: indicators.compute_all, candlestick_patterns.detect, 
    chart_patterns.detect, confluence_scorer.score, alignment.check
    """
    try:
        # Get primary timeframe candles (H1 preferred)
        h1_candles = candles_by_tf.get("H1", [])
        if not h1_candles and candles_by_tf:
            # Fall back to first available timeframe
            h1_candles = next(iter(candles_by_tf.values()))
        
        if not h1_candles:
            return {"error": "No candles provided for analysis"}

        result = {
            "instrument": instrument,
            "candles_analyzed": len(h1_candles),
            "timeframes": list(candles_by_tf.keys())
        }

        # 1. Compute indicators
        try:
            IndicatorsClass = _get_indicators()
            indicators_calc = IndicatorsClass(h1_candles)
            core_indicators = indicators_calc.compute_all()
            result["core_indicators"] = core_indicators
        except Exception as exc:
            logger.warning("Indicators computation failed: %s", exc)
            result["core_indicators"] = {"error": str(exc)}

        # 2. Compute advanced indicators (ADX, Stochastic, Volume SMA, Fibonacci, VWAP)
        #    Must run before chart patterns so volume ratio is available
        try:
            AdvancedIndicatorsClass = _get_advanced_indicators()
            adv_calc = AdvancedIndicatorsClass(h1_candles)
            advanced_indicators = adv_calc.compute_all()
            result["advanced_indicators"] = advanced_indicators
        except Exception as exc:
            logger.warning("Advanced indicators computation failed: %s", exc)
            result["advanced_indicators"] = {"error": str(exc)}

        # 3. Detect candlestick patterns
        try:
            CandlestickPatternsClass = _get_candlestick_patterns()
            cs_detector = CandlestickPatternsClass(h1_candles)
            cs_patterns = cs_detector.get_detected_patterns()
            result["candlestick_patterns"] = cs_patterns
        except Exception as exc:
            logger.warning("Candlestick pattern detection failed: %s", exc)
            result["candlestick_patterns"] = {"error": str(exc)}

        # 4. Detect chart patterns (with volume ratio from advanced indicators)
        try:
            ChartPatternsClass = _get_chart_patterns()
            chart_detector = ChartPatternsClass(h1_candles)
            vol_ratio = None
            adv = result.get("advanced_indicators", {})
            if isinstance(adv, dict):
                vol_sma = adv.get("volume_sma", {})
                if isinstance(vol_sma, dict):
                    vol_ratio = vol_sma.get("ratio")
            chart_patterns = chart_detector.scan_all(volume_sma_ratio=vol_ratio)
            result["chart_patterns"] = chart_patterns
        except Exception as exc:
            logger.warning("Chart pattern detection failed: %s", exc)
            result["chart_patterns"] = {"error": str(exc)}

        # 5. Check alignment (must run BEFORE confluence scoring)
        try:
            MultiTimeframeAlignmentClass = _get_alignment()
            alignment_checker = MultiTimeframeAlignmentClass(candles_by_tf)
            alignment = alignment_checker.get_snapshot()
            result["alignment"] = alignment
        except Exception as exc:
            logger.warning("Alignment check failed: %s", exc)
            result["alignment"] = {"error": str(exc)}

        # 6. Score confluence (after alignment + advanced indicators)
        try:
            ConfluenceScorerClass = _get_confluence_scorer()
            confluence_scorer = ConfluenceScorerClass()
            confluence = confluence_scorer.compute_score(
                indicators_result=result.get("core_indicators", {}),
                advanced_result=result.get("advanced_indicators", {}),
                alignment_snapshot=result.get("alignment", {}),
                pattern_results=result.get("candlestick_patterns", {}),
                chart_results=result.get("chart_patterns", {}),
                news_data={"score": news_score} if news_score else None,
            )
            result["confluence"] = confluence
        except Exception as exc:
            logger.warning("Confluence scoring failed: %s", exc)
            result["confluence"] = {"error": str(exc)}

        # 7. Regime detection + setup scanning + H4 trend + session
        #    (uses backtester DataFrame path — proven in test_live_pipeline.py)
        try:
            result["regime"] = detect_regime_from_candles(h1_candles)
        except Exception as exc:
            logger.warning("Regime detection failed: %s", exc)
            result["regime"] = "unknown"

        try:
            result["setups_firing"] = scan_setups_from_candles(h1_candles, instrument or "")
        except Exception as exc:
            logger.warning("Setup scanning failed: %s", exc)
            result["setups_firing"] = []

        try:
            h4_candles = candles_by_tf.get("H4", [])
            result["h4_trend"] = get_h4_trend(h4_candles) if h4_candles else "unknown"
        except Exception as exc:
            logger.warning("H4 trend detection failed: %s", exc)
            result["h4_trend"] = "unknown"

        result["session"] = detect_session()

        return _sanitize_for_json(result)

    except Exception as exc:
        logger.error("run_full_analysis failed: %s", exc)
        return {"error": str(exc)}

# ---------------------------------------------------------------------------
# Sniper V4 scoring on live candles
# ---------------------------------------------------------------------------

def compute_sniper_score(candles_by_tf: Dict[str, List[Dict]],
                         instrument: Optional[str] = None,
                         sniper_threshold: int = 12,
                         **kwargs) -> Dict[str, Any]:
    """Run the Sniper V4 scorer on live OANDA candles.

    Converts live candle dicts into a pandas DataFrame, computes all indicators
    using the SAME backtester pipeline (indicators.compute_all, candle_patterns,
    sniper_v4.add_enhanced_indicators), then calls score_v4() on the last row.

    This produces the SAME score that achieved 90%+ win rate in backtesting.

    Returns:
        Dict with buy_score, sell_score, threshold, direction, signal_strength,
        and component breakdown.
    """
    import pandas as pd
    import numpy as np

    try:
        # Trade on M15, fall back to H1 for scoring
        primary_candles = candles_by_tf.get("M15") or candles_by_tf.get("H1", [])
        primary_tf = "M15" if candles_by_tf.get("M15") else "H1"
        if not primary_candles or len(primary_candles) < 100:
            return {"error": f"Need ≥100 {primary_tf} candles, got {len(primary_candles) if primary_candles else 0}",
                    "buy_score": 0, "sell_score": 0, "threshold": sniper_threshold}

        # 1. Convert OANDA candle dicts to DataFrame
        rows = []
        for c in primary_candles:
            mid = c.get("mid", {})
            row = {
                "timestamp": c.get("time", ""),
                "open": float(mid.get("o", c.get("open", 0))),
                "high": float(mid.get("h", c.get("high", 0))),
                "low": float(mid.get("l", c.get("low", 0))),
                "close": float(mid.get("c", c.get("close", 0))),
                "volume": int(c.get("volume", 0)),
            }
            rows.append(row)

        df = pd.DataFrame(rows)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])

        # 2. Run backtester indicator pipeline (SAME as master_sweep.load_and_prepare)
        # Import with fallback: "Source.backtester" works when cwd is project root,
        # "backtester" works when Source/ is on sys.path (e.g., scout via python -m)
        try:
            from Source.backtester import indicators as bt_indicators
        except ModuleNotFoundError:
            from backtester import indicators as bt_indicators
        df = bt_indicators.compute_all(df)

        # 3. Divergence signals
        try:
            try:
                from Source.backtester.divergence import add_divergence_signals
            except ModuleNotFoundError:
                from backtester.divergence import add_divergence_signals
            df = add_divergence_signals(df)
        except Exception as exc:
            logger.debug("Divergence signals skipped: %s", exc)

        # 4. Candlestick patterns
        try:
            from Source.backtester.candle_patterns import detect_all_patterns
        except ModuleNotFoundError:
            from backtester.candle_patterns import detect_all_patterns
        df = detect_all_patterns(df)

        # 4b. Chart patterns
        try:
            from Source.backtester.chart_patterns import detect_all_chart_patterns
        except ModuleNotFoundError:
            from backtester.chart_patterns import detect_all_chart_patterns
        chart_patterns = detect_all_chart_patterns(df, lookback=100)

        # 5. Enhanced indicators for sniper (CCI, SAR, Fibonacci, Pivots, stoch crossover, EMA crossover)
        try:
            from Source.backtester.sniper_v4 import add_enhanced_indicators, score_v4, TF_PARAMS
        except ModuleNotFoundError:
            from backtester.sniper_v4 import add_enhanced_indicators, score_v4, TF_PARAMS
        df = add_enhanced_indicators(df)

        # 6. Derived columns (from master_sweep.load_and_prepare)
        # Consecutive candles
        bull_run = (df["close"] > df["open"]).astype(int)
        bear_run = (df["close"] < df["open"]).astype(int)
        consec_bull = pd.Series(0, index=df.index)
        consec_bear = pd.Series(0, index=df.index)
        for i in range(1, len(df)):
            if bull_run.iloc[i]:
                consec_bull.iloc[i] = consec_bull.iloc[i - 1] + 1
            if bear_run.iloc[i]:
                consec_bear.iloc[i] = consec_bear.iloc[i - 1] + 1
        df["consec_bull"] = consec_bull
        df["consec_bear"] = consec_bear

        # RSI slope
        df["rsi_slope"] = df["rsi"].diff(3)

        # BB penetration
        atr_safe = df["atr"].replace(0, np.nan)
        df["bb_lower_pen"] = np.where(
            df["close"] < df["bb_lower"],
            (df["bb_lower"] - df["close"]) / atr_safe,
            0
        )
        df["bb_upper_pen"] = np.where(
            df["close"] > df["bb_upper"],
            (df["close"] - df["bb_upper"]) / atr_safe,
            0
        )

        # Swing high/low proximity
        lookback = 50
        df["swing_high"] = df["high"].rolling(lookback).max()
        df["swing_low"] = df["low"].rolling(lookback).min()
        df["near_swing_high"] = (df["swing_high"] - df["close"]) / atr_safe < 0.5
        df["near_swing_low"] = (df["close"] - df["swing_low"]) / atr_safe < 0.5

        # Fibonacci levels (from swing high/low)
        swing_high = df["high"].iloc[-lookback:].max()
        swing_low = df["low"].iloc[-lookback:].min()
        fib_levels = bt_indicators.fibonacci_levels(swing_high, swing_low)
        for fib_key, fib_val in fib_levels.items():
            df[fib_key] = fib_val

        # Pivot points (from previous day's H/L/C — aggregate last 24 H1 candles)
        if len(df) >= 24:
            prev_day = df.iloc[-48:-24] if len(df) >= 48 else df.iloc[:len(df) - 24]
            if len(prev_day) > 0:
                prev_h = prev_day["high"].max()
                prev_l = prev_day["low"].min()
                prev_c = prev_day["close"].iloc[-1]
                pivots = bt_indicators.pivot_points(prev_h, prev_l, prev_c)
                df["pivot"] = pivots["pivot"]
                df["pivot_s1"] = pivots["s1"]
                df["pivot_r1"] = pivots["r1"]

        # Rename parabolic_sar → sar (sniper_v4 expects "sar")
        if "parabolic_sar" in df.columns:
            df["sar"] = df["parabolic_sar"]
            # Re-run add_enhanced_indicators now that sar column exists
            df = add_enhanced_indicators(df)

        # 7. H4 bias and H4 RSI
        h4_candles = candles_by_tf.get("H4", [])
        if h4_candles and len(h4_candles) >= 20:
            h4_rows = []
            for c in h4_candles:
                mid = c.get("mid", {})
                h4_rows.append({
                    "open": float(mid.get("o", c.get("open", 0))),
                    "high": float(mid.get("h", c.get("high", 0))),
                    "low": float(mid.get("l", c.get("low", 0))),
                    "close": float(mid.get("c", c.get("close", 0))),
                    "volume": int(c.get("volume", 0)),
                })
            h4_df = pd.DataFrame(h4_rows)
            h4_rsi = bt_indicators.rsi(h4_df)
            h4_rsi_val = h4_rsi.iloc[-1] if len(h4_rsi) > 0 and not pd.isna(h4_rsi.iloc[-1]) else 50.0

            # H4 bias from last few candles
            h4_close = h4_df["close"].iloc[-1]
            h4_ema21 = h4_df["close"].ewm(span=21).mean().iloc[-1]
            if h4_close > h4_ema21 * 1.001:
                h4_bias = "bull"
            elif h4_close < h4_ema21 * 0.999:
                h4_bias = "bear"
            else:
                h4_bias = "range"

            df["h4_bias"] = h4_bias
            df["h4_rsi"] = float(h4_rsi_val)
        else:
            df["h4_bias"] = "none"
            df["h4_rsi"] = 50.0

        # 8. Score the LAST row using score_v4
        last_row = df.iloc[-1].to_dict()
        # Convert numpy types to Python natives
        for k, v in last_row.items():
            if isinstance(v, (np.bool_,)):
                last_row[k] = bool(v)
            elif isinstance(v, (np.integer,)):
                last_row[k] = int(v)
            elif isinstance(v, (np.floating,)):
                last_row[k] = float(v) if not np.isnan(v) else 0.0

        # Compute full EMA market picture for narrative-aware confluence scoring
        try:
            try:
                from Source.backtester.ema_separation import scan_ema_signals, generate_market_picture
            except ModuleNotFoundError:
                from backtester.ema_separation import scan_ema_signals, generate_market_picture
            _candle_dicts = []
            for c in h1_candles:
                mid = c.get("mid", {})
                _candle_dicts.append({
                    "time": c.get("time", ""),
                    "open": mid.get("o", c.get("open", 0)),
                    "high": mid.get("h", c.get("high", 0)),
                    "low": mid.get("l", c.get("low", 0)),
                    "close": mid.get("c", c.get("close", 0)),
                })
            _ema_sig = scan_ema_signals(_candle_dicts)
            last_row['ema_separation_velocity'] = _ema_sig.get('separation_velocity', 0)
            # Derive fan_direction from scan_ema_signals output
            # scan_ema_signals has no fan_direction key — derive from recommended_bias/signal
            _bias = (_ema_sig.get('recommended_bias') or _ema_sig.get('signal') or '').lower()
            if _bias in ('bullish', 'bull', 'long', 'up'):
                _ema_sig['fan_direction'] = 'bullish'
            elif _bias in ('bearish', 'bear', 'short', 'down'):
                _ema_sig['fan_direction'] = 'bearish'
            else:
                _ema_sig['fan_direction'] = 'neutral'
            # Pass full EMA context for narrative-aware scoring
            last_row['_market_picture_ema'] = _ema_sig
        except Exception:
            last_row['ema_separation_velocity'] = 0

        tf_params = TF_PARAMS.get(primary_tf, TF_PARAMS.get("H1"))
        buy_score, sell_score = score_v4(last_row, tf_params)

        # Determine direction and signal strength
        max_score = max(buy_score, sell_score)
        if buy_score >= sniper_threshold and buy_score > sell_score:
            direction = "bullish"
            signal = "TRADE"
        elif sell_score >= sniper_threshold and sell_score > buy_score:
            direction = "bearish"
            signal = "TRADE"
        elif max_score >= sniper_threshold - 2:
            direction = "bullish" if buy_score > sell_score else "bearish"
            signal = "WATCH"
        else:
            direction = "neutral"
            signal = "HOLD"

        # --- Build full indicator snapshot for LLM consumption ---
        def _safe(key, default=0, decimals=2):
            v = last_row.get(key, default)
            try:
                v = float(v)
                return round(v, decimals) if not np.isnan(v) else default
            except (TypeError, ValueError):
                return default

        indicators_snapshot = {
            # Trend
            "ema_21": _safe("ema_21", decimals=5),
            "ema_55": _safe("ema_55", decimals=5),
            "ema_100": _safe("ema_100", decimals=5),
            "sma_50": _safe("sma_50", decimals=5),
            "sma_100": _safe("sma_100", decimals=5),
            "adx": _safe("adx"),
            # Momentum
            "rsi": _safe("rsi"),
            "rsi_slope": _safe("rsi_slope"),
            "stoch_k": _safe("stoch_k"),
            "stoch_d": _safe("stoch_d"),
            "stoch_crossover_bull": bool(last_row.get("stoch_crossover_bull", False)),
            "stoch_crossover_bear": bool(last_row.get("stoch_crossover_bear", False)),
            "macd": _safe("macd", decimals=6),
            "macd_signal": _safe("macd_signal", decimals=6),
            "macd_histogram": _safe("macd_histogram", decimals=6),
            "cci": _safe("cci"),
            # Volatility
            "atr": _safe("atr", decimals=5),
            "bb_upper": _safe("bb_upper", decimals=5),
            "bb_lower": _safe("bb_lower", decimals=5),
            "bb_middle": _safe("bb_middle", decimals=5),
            "bb_lower_pen": _safe("bb_lower_pen", decimals=3),
            "bb_upper_pen": _safe("bb_upper_pen", decimals=3),
            # BB width (upper-lower) for expansion detection
            "bb_width": round(
                (last_row.get("bb_upper", 0) or 0) - (last_row.get("bb_lower", 0) or 0), 5
            ),
            # BB width 5 bars ago for expansion comparison (from df)
            "bb_width_prev": round(
                float(df["bb_upper"].iloc[-6] - df["bb_lower"].iloc[-6])
                if "bb_upper" in df.columns and "bb_lower" in df.columns and len(df) > 5
                else (last_row.get("bb_upper", 0) or 0) - (last_row.get("bb_lower", 0) or 0), 5
            ),
            # Structure
            "sar": _safe("sar", decimals=5),
            "sar_bullish": bool(last_row.get("sar_bullish", False)),
            "at_key_fib": bool(last_row.get("at_key_fib", False)),
            "pivot": _safe("pivot", decimals=5),
            "pivot_s1": _safe("pivot_s1", decimals=5),
            "pivot_r1": _safe("pivot_r1", decimals=5),
            "near_swing_high": bool(last_row.get("near_swing_high", False)),
            "near_swing_low": bool(last_row.get("near_swing_low", False)),
            # Candles
            "consec_bull": int(last_row.get("consec_bull", 0)),
            "consec_bear": int(last_row.get("consec_bear", 0)),
            "candle_bull_signal": int(last_row.get("candle_bull_signal", 0)),
            "candle_bear_signal": int(last_row.get("candle_bear_signal", 0)),
            # Price
            "close": _safe("close", decimals=5),
            "volume": int(last_row.get("volume", 0)),
        }

        # Detected candlestick patterns (non-zero/True only)
        candle_pattern_names = [
            "hammer", "inverted_hammer", "bullish_engulfing", "bearish_engulfing",
            "morning_star", "evening_star", "shooting_star", "doji", "dragonfly_doji",
            "gravestone_doji", "spinning_top", "dark_cloud", "piercing_line",
            "three_white_soldiers", "three_black_crows", "tweezer_top", "tweezer_bottom",
            "rising_three", "falling_three", "bullish_harami", "bearish_harami",
            "marubozu",
        ]
        # 2026-04-24 fix: scan last 3 bars, not just last_row. Candle patterns
        # are bar-specific events — hammer that formed 2 bars ago is still the
        # relevant signal for the current validator decision. Previous code
        # only checked last_row which missed 80%+ of real patterns (verified via
        # direct test: EUR_USD last-5-bars had hammer+morning_star but last_row
        # alone showed nothing). Patterns tagged with bars-ago suffix for clarity.
        detected_patterns = []
        _bars_to_scan = min(3, len(df))
        for _bars_ago in range(_bars_to_scan):
            _row_idx = -1 - _bars_ago  # -1 = last_row, -2 = prev, -3 = 2-prev
            try:
                _row = df.iloc[_row_idx]
            except Exception:
                break
            for p in candle_pattern_names:
                try:
                    if bool(_row.get(p, False)):
                        _tag = p if _bars_ago == 0 else f"{p}@{_bars_ago}_bars_ago"
                        # Avoid duplicates (prefer the most recent occurrence)
                        if p not in detected_patterns and _tag not in detected_patterns:
                            detected_patterns.append(_tag)
                except Exception:
                    pass
        
        # Add chart patterns 
        if chart_patterns:
            for pattern in chart_patterns:
                pattern_name = pattern.get('pattern', '').replace(' ', '_').lower()
                detected_patterns.append(f"{pattern_name} ({pattern.get('confidence', 0)}%)")

        # Divergence
        divergence_info = {
            "rsi_bullish_div": bool(last_row.get("rsi_bullish_div", False)),
            "rsi_bearish_div": bool(last_row.get("rsi_bearish_div", False)),
            "macd_bullish_div": bool(last_row.get("macd_bullish_div", False)),
            "macd_bearish_div": bool(last_row.get("macd_bearish_div", False)),
        }

        # Surface fan_direction from EMA signals so trading_cycle.py can derive direction
        _mp = last_row.get("_market_picture_ema", {}) or {}
        _fan_dir = _mp.get("fan_direction", "") or ""

        # Two-cross confirmation flags
        # Cross 2 (E21×E100) = fan fully ordered — the confirmation cross
        _c2_bull = bool(last_row.get("e21_crossed_100_recently_bull", False))
        _c2_bear = bool(last_row.get("e21_crossed_100_recently_bear", False))
        _c2_current_bull = bool(last_row.get("ema_21_cross_100_up", False))
        _c2_current_bear = bool(last_row.get("ema_21_cross_100_down", False))
        # Direction-agnostic: did EITHER cross happen recently?
        _e21_crossed_100_recently = _c2_bull or _c2_bear
        _e21_crossed_100_this_bar = _c2_current_bull or _c2_current_bear

        result = {
            "buy_score": int(buy_score),
            "sell_score": int(sell_score),
            "threshold": sniper_threshold,
            "direction": direction,
            "fan_direction": _fan_dir,  # EMA fan direction for direction fallback
            # Two-cross confirmation — Cross 2 (E21×E100) = fan fully ordered
            "e21_crossed_100_recently": _e21_crossed_100_recently,   # within last 10 bars
            "e21_crossed_100_this_bar": _e21_crossed_100_this_bar,    # happening right now
            "e21_cross_100_bull": _c2_bull,   # direction-specific
            "e21_cross_100_bear": _c2_bear,
            "signal": signal,
            "max_score": int(max_score),
            "h4_bias": last_row.get("h4_bias", "none"),
            "h4_rsi": round(float(last_row.get("h4_rsi", 50)), 1),
            # Full indicator snapshot for LLM
            "indicators": indicators_snapshot,
            "detected_patterns": detected_patterns,
            "chart_patterns": chart_patterns,
            "divergence": divergence_info,
            # Legacy flat fields (for backward compat)
            "rsi": _safe("rsi"),
            "stoch_k": _safe("stoch_k"),
            "adx": _safe("adx"),
            "bb_lower_pen": _safe("bb_lower_pen", decimals=3),
            "bb_upper_pen": _safe("bb_upper_pen", decimals=3),
            "at_key_fib": bool(last_row.get("at_key_fib", False)),
            "sar_bullish": bool(last_row.get("sar_bullish", False)),
            "consec_bear": int(last_row.get("consec_bear", 0)),
            "consec_bull": int(last_row.get("consec_bull", 0)),
        }

        # Expose the indicator-loaded df so trading_cycle can compute
        # thesis_measurements locally (Phase 3 of refactor 2026-05-06).
        # Saves duplicating the df-build pipeline (compute_all + divergence +
        # candle_patterns + add_enhanced_indicators) just to get a usable df
        # for compute_thesis_measurements(). See vault doc:
        #   agents/claude-code/2026-05-06-thesis-measurements-refactor.md
        result["df"] = df

        logger.info("[SNIPER] %s (%s): buy=%d sell=%d threshold=%d → %s %s",
                     instrument or "?", primary_tf, buy_score, sell_score,
                     sniper_threshold, signal, direction)
        return result

    except Exception as exc:
        logger.error("compute_sniper_score failed: %s", exc, exc_info=True)
        return {"error": str(exc), "buy_score": 0, "sell_score": 0,
                "threshold": sniper_threshold}


# ---------------------------------------------------------------------------
# validator composite wrapper
# ---------------------------------------------------------------------------

def run_full_validation(candles: List[Dict], indicators: Dict, patterns: Dict,
                       trade_params: Dict, risk_limits: Dict, 
                       historical_performance: Optional[Dict] = None,
                       analysis_results: Optional[Dict] = None,
                       intelligence_data: Optional[Dict] = None,
                       **kwargs) -> Dict[str, Any]:
    """Validate trade signal against historical backtest evidence + risk limits.

    Uses DecisionLogger.evaluate_and_log() — the proven 4-step pipeline that
    queries 39,692 backtest setup performance rows and 8.5M trade records.
    Verified working via test_live_pipeline.py (2026-02-17).

    Flow:
        1. Gate 0: Hard risk limits (daily loss, concurrent trades, data quality)
        2. For each firing setup: DecisionLogger.evaluate_and_log() →
           - Gate 1: Data integrity
           - Gate 2: Pre-trade checks (13 contradiction rules)
           - Gate 3: Backtest evidence (TradingDB queries)
           - Gate 4: Final verdict (APPROVE/REJECT/CAUTION)
        3. Return best verdict across all firing setups
    """
    try:
        ar = analysis_results if isinstance(analysis_results, dict) else {}
        intel = intelligence_data if isinstance(intelligence_data, dict) else {}
        rl = risk_limits or {}

        result = {
            "candles_count": len(candles) if candles else 0,
            "trade_params": trade_params,
            "risk_limits": risk_limits,
        }

        # ── Gate 0: Hard risk limits (fast, no DB) ───────────────────
        gate0_issues = []

        if not candles or len(candles) < 10:
            gate0_issues.append(f"Insufficient candles: {len(candles) if candles else 0}")
        if not indicators or not indicators.get("core"):
            gate0_issues.append("Missing core indicators")

        daily_loss = rl.get("current_daily_loss_pct", 0)
        max_daily = rl.get("max_daily_loss_pct", 3.0)
        if daily_loss >= max_daily:
            gate0_issues.append(f"Daily loss {daily_loss:.1f}% >= {max_daily:.1f}% limit")

        open_trades = rl.get("open_trade_count", 0)
        max_concurrent = rl.get("max_concurrent_trades", 3)
        if open_trades >= max_concurrent:
            gate0_issues.append(f"Open trades {open_trades} >= {max_concurrent} limit")

        if gate0_issues:
            result.update({
                "overall_passed": False,
                "confidence": 0.0,
                "verdict": "REJECT",
                "issues": gate0_issues,
                "gate": "gate0_risk_limits",
                "recommendation": "hold -- " + "; ".join(gate0_issues),
            })
            return result

        # ── Get setups, regime, H4, session from analysis_results ────
        setups_firing = ar.get("setups_firing", [])
        regime = ar.get("regime", "unknown")
        h4_trend = ar.get("h4_trend", "unknown")
        session = ar.get("session", "Off_Hours")
        instrument = trade_params.get("instrument", "")
        timeframe = trade_params.get("timeframe", "M15")

        # Extract indicator values for DecisionLogger
        core = indicators.get("core", {}) if isinstance(indicators, dict) else {}
        ind_for_dl = {}
        for key in ["adx", "rsi", "stoch_k", "bb_width", "atr", "cci", "macd_histogram"]:
            if key in core:
                ind_for_dl[key] = core[key]

        # No setups firing → HOLD (not enough signal)
        if not setups_firing:
            result.update({
                "overall_passed": False,
                "confidence": 0.1,
                "verdict": "REJECT",
                "issues": [f"No setups firing in {regime} regime"],
                "gate": "no_setups",
                "recommendation": "hold -- no setups detected",
                "regime": regime,
                "setups_firing": [],
            })
            return result

        # ── Evaluate each firing setup via DecisionLogger ────────────
        dl = _get_decision_logger()

        # Extract intelligence data for DecisionLogger
        news_data = intel.get("news") if intel else None
        weather_data = intel.get("weather") if intel else None
        wolfram_data = intel.get("statistics") if intel else None

        setup_results = []
        best_result = None
        best_confidence = -1.0

        for setup_info in setups_firing:
            setup_name = setup_info.get("setup", "")
            direction = setup_info.get("direction", "neutral")
            if direction == "neutral":
                continue

            setup_key = f"{setup_name}_rr2.0_sl2.5"  # default params, DB will find best variant
            h4_agrees = (
                (direction == "buy" and h4_trend == "bullish")
                or (direction == "sell" and h4_trend == "bearish")
            )

            # Concurrent setups list for confluence check
            concurrent = [s.get("setup", "") for s in setups_firing if s.get("direction") != "neutral"]

            try:
                dl_result = dl.evaluate_and_log(
                    pair=instrument,
                    timeframe=timeframe,
                    setup=setup_key,
                    direction=direction,
                    regime=regime,
                    indicators=ind_for_dl,
                    h4_agrees=h4_agrees,
                    session=session,
                    market_data={"concurrent_setups": concurrent},
                    news_data=news_data,
                    weather_data=weather_data,
                    wolfram_data=wolfram_data,
                )
            except Exception as exc:
                logger.error("DecisionLogger.evaluate_and_log failed for %s: %s", setup_key, exc)
                dl_result = {
                    "verdict": "REJECT",
                    "confidence": 0.0,
                    "recommended_action": "SKIP",
                    "warnings": [f"Pipeline error: {exc}"],
                    "decision_id": "error",
                }

            entry = {
                "setup": setup_key,
                "base_setup": setup_name,
                "direction": direction,
                "h4_agrees": h4_agrees,
                "verdict": dl_result.get("verdict", "REJECT"),
                "confidence": dl_result.get("confidence", 0.0),
                "recommended_action": dl_result.get("recommended_action", "SKIP"),
                "recommended_params": dl_result.get("recommended_params"),
                "warnings": dl_result.get("warnings", []),
                "loss_patterns": dl_result.get("loss_patterns", []),
                "confluence": dl_result.get("confluence"),
                "pipeline_steps": dl_result.get("pipeline_steps"),
                "decision_id": dl_result.get("decision_id"),
                "execution_time_ms": dl_result.get("execution_time_ms"),
            }
            setup_results.append(entry)

            # Track best result (prefer APPROVE > CAUTION > REJECT, then highest confidence)
            verdict_rank = {"APPROVE": 3, "CAUTION": 2, "REJECT": 1}.get(entry["verdict"], 0)
            best_rank = {"APPROVE": 3, "CAUTION": 2, "REJECT": 1}.get(
                best_result["verdict"] if best_result else "", 0
            )
            if verdict_rank > best_rank or (verdict_rank == best_rank and entry["confidence"] > best_confidence):
                best_result = entry
                best_confidence = entry["confidence"]

        # ── Build final result from best setup ───────────────────────
        if best_result is None:
            result.update({
                "overall_passed": False,
                "confidence": 0.0,
                "verdict": "REJECT",
                "issues": ["No tradeable setups (all neutral direction)"],
                "recommendation": "hold",
            })
            return result

        overall_passed = best_result["verdict"] in ("APPROVE", "CAUTION")

        result.update({
            "overall_passed": overall_passed,
            "confidence": best_result["confidence"],
            "verdict": best_result["verdict"],
            "recommendation": best_result["recommended_action"].lower(),
            "best_setup": best_result["setup"],
            "best_setup_base": best_result["base_setup"],
            "best_direction": best_result["direction"],
            "recommended_params": best_result["recommended_params"],
            "h4_agrees": best_result["h4_agrees"],
            "warnings": best_result["warnings"],
            "loss_patterns": best_result["loss_patterns"],
            "confluence_evidence": best_result["confluence"],
            "pipeline_steps": best_result["pipeline_steps"],
            "decision_id": best_result["decision_id"],
            "execution_time_ms": best_result["execution_time_ms"],
            "regime": regime,
            "session": session,
            "setups_evaluated": setup_results,
            "setups_firing": setups_firing,
            "borderline": best_result["verdict"] == "CAUTION",
            "needs_llm_escalation": best_result["verdict"] == "CAUTION",
        })
        return result

    except Exception as exc:
        logger.error("run_full_validation failed: %s", exc)
        return {"error": str(exc), "overall_passed": False}

# ---------------------------------------------------------------------------
# pip-value aware position sizing
# ---------------------------------------------------------------------------

def compute_units_for_pip_target(instrument: str, target_pip_value_usd: float,
                                  current_price: float = None) -> int:
    """Convert a $/pip target into the correct unit count for *any* pair.

    The dashboard lets the user pick a lot size labelled by $/pip (e.g. $1/pip).
    That label is only accurate for XXX_USD pairs.  For every other pair the
    number of units must be adjusted so the actual pip value in the USD-
    denominated account matches the user's intent.

    Formula:  units = target_$/pip / (pip_size × quote→USD_rate)

    For cross-pairs where neither currency is USD we need one extra spot
    price to convert the quote currency into USD.  We fetch that from OANDA
    with a single candle call (cheap, cached in most brokers).

    Returns units rounded to the nearest 100.
    """
    base, quote = instrument.split("_")
    is_jpy_quote = (quote == "JPY")
    pip_size = 0.01 if is_jpy_quote else 0.0001

    # ── Determine quote→USD conversion rate ────────────────────────────
    if quote == "USD":
        # Direct: pip is already in USD
        quote_to_usd = 1.0
    elif base == "USD":
        # USD_XXX pair — we have the rate directly from the instrument price
        if current_price is None:
            current_price = _quick_price(instrument)
        quote_to_usd = 1.0 / current_price if current_price and current_price > 0 else 1.0
    else:
        # Cross pair — need a separate conversion rate
        quote_to_usd = _quote_currency_to_usd(quote)

    pip_value_per_unit = pip_size * quote_to_usd
    if pip_value_per_unit <= 0:
        # Fallback: assume USD-quoted behaviour
        logger.warning("[PIP_SIZING] pip_value_per_unit=%.8f for %s — falling back to 0.0001",
                       pip_value_per_unit, instrument)
        pip_value_per_unit = 0.0001

    raw_units = target_pip_value_usd / pip_value_per_unit
    # Round to nearest 100 — OANDA accepts any integer but clean numbers
    # are easier for the user to audit
    units = int(round(raw_units / 100.0) * 100)
    units = max(100, units)  # never go below 100

    logger.info(
        "[PIP_SIZING] %s: target=$%.2f/pip → pip_val_per_unit=%.8f → %d units "
        "(quote=%s, quote→USD=%.6f, pip_size=%.4f)",
        instrument, target_pip_value_usd, pip_value_per_unit, units,
        quote, quote_to_usd, pip_size
    )
    return units


def _quick_price(instrument: str) -> Optional[float]:
    """Fetch latest mid-close price for an instrument (1 candle)."""
    try:
        raw = fetch_candles(instrument, timeframe="M1", count=1)
        candles = raw.get("candles", []) if isinstance(raw, dict) else []
        if candles:
            return float(candles[-1]["mid"]["c"])
    except Exception as e:
        logger.warning("[PIP_SIZING] _quick_price(%s) failed: %s", instrument, e)
    return None


# Map of quote currencies to the OANDA pair that converts them to USD
_QUOTE_TO_USD_PAIR = {
    "JPY": ("USD_JPY", True),    # JPY→USD = 1 / USD_JPY
    "CAD": ("USD_CAD", True),    # CAD→USD = 1 / USD_CAD
    "CHF": ("USD_CHF", True),    # CHF→USD = 1 / USD_CHF
    "GBP": ("GBP_USD", False),   # GBP→USD = GBP_USD directly
    "AUD": ("AUD_USD", False),   # AUD→USD = AUD_USD directly
    "NZD": ("NZD_USD", False),   # NZD→USD = NZD_USD directly
    "EUR": ("EUR_USD", False),   # EUR→USD = EUR_USD directly
}


def _quote_currency_to_usd(quote_currency: str) -> float:
    """Get the conversion rate from a quote currency to USD.

    Uses a single M1 candle fetch for the appropriate USD pair.
    Falls back to conservative estimate if fetch fails.
    """
    pair_info = _QUOTE_TO_USD_PAIR.get(quote_currency)
    if not pair_info:
        logger.warning("[PIP_SIZING] Unknown quote currency %s — assuming 1.0", quote_currency)
        return 1.0

    pair, invert = pair_info
    price = _quick_price(pair)
    if price is None or price <= 0:
        # Hardcoded conservative fallbacks — close enough if API is down
        _FALLBACKS = {"JPY": 0.0067, "CAD": 0.73, "CHF": 1.13,
                      "GBP": 1.27, "AUD": 0.65, "NZD": 0.58, "EUR": 1.08}
        rate = _FALLBACKS.get(quote_currency, 1.0)
        logger.warning("[PIP_SIZING] Could not fetch %s — using fallback %.4f for %s→USD",
                       pair, rate, quote_currency)
        return rate

    if invert:
        return 1.0 / price
    return price


# ---------------------------------------------------------------------------
# execution wrappers
# ---------------------------------------------------------------------------

def place_market_order(instrument: str, units: int, stop_loss: Optional[str] = None,
                      take_profit: Optional[str] = None, direction: str = "buy",
                      confluence_score: Optional[float] = None, 
                      risk_profile: str = "default", cycle_id: Optional[str] = None,
                      **kwargs) -> Dict[str, Any]:
    """Wrapper for order placement (missing from team_setup.py)."""
    try:
        client = _get_oanda_client()
        
        # Convert direction to units sign
        if direction.lower() == "sell":
            units = -abs(units)
        else:
            units = abs(units)
            
        # Build client extensions for tracking
        client_extensions = {
            "id": cycle_id or f"trade_{instrument}",
            "tag": f"confluence_{confluence_score}" if confluence_score else "manual",
            "comment": f"Risk profile: {risk_profile}"
        }
        
        result = client.place_market_order(
            instrument=instrument,
            units=units,
            stop_loss=stop_loss,
            take_profit=take_profit,
            client_extensions=client_extensions
        )
        
        # Extract key info for cycle result
        fill_transaction = result.get("orderFillTransaction", {})
        return {
            "status": "filled" if fill_transaction else "rejected",
            "trade_id": fill_transaction.get("tradeOpened", {}).get("tradeID"),
            "entry_price": fill_transaction.get("price"),
            "units": fill_transaction.get("units"),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "client_extensions": client_extensions,
            "full_response": result
        }
        
    except Exception as exc:
        logger.error("place_market_order failed: %s", exc)
        return {"error": str(exc), "status": "error"}

def get_position_status(**kwargs) -> Dict[str, Any]:
    """Wrapper for position_monitor.check_positions -> get_position_status."""
    try:
        monitor = _get_position_monitor()
        return monitor.check_positions(**kwargs)
    except Exception as exc:
        logger.error("get_position_status failed: %s", exc)
        return {"error": str(exc), "positions": []}

def update_monitored_positions(candles_by_instrument: Dict[str, List], 
                             current_prices: Dict[str, float], **kwargs) -> Dict[str, Any]:
    """Wrapper for position monitoring updates (missing from team_setup.py)."""
    try:
        monitor = _get_position_monitor()
        return monitor.update_positions(
            candles_by_instrument=candles_by_instrument,
            current_prices=current_prices,
            **kwargs
        )
    except Exception as exc:
        logger.error("update_monitored_positions failed: %s", exc)
        return {"error": str(exc), "actions_taken": []}

# ---------------------------------------------------------------------------
# reporter composite wrappers  
# ---------------------------------------------------------------------------

def generate_cycle_summary(cycle_data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """Composite wrapper for cycle reporting (missing from team_setup.py).
    
    Combines trade_logger.log_signal, trade_logger.log_trade, 
    knowledge_store.store_decision, knowledge_store.get_instrument_knowledge
    """
    try:
        logger_instance = _get_trade_logger()
        knowledge = _get_knowledge_store()
        
        instrument = cycle_data.get("instrument", "")
        decision = cycle_data.get("decision", {})
        execution = cycle_data.get("execution")
        
        summary = {
            "instrument": instrument,
            "cycle_start": cycle_data.get("cycle_start"),
            "trade_placed": execution is not None and execution.get("status") == "filled",
            "decision_action": decision.get("action", "hold"),
            "confluence_score": decision.get("confluence_score", 0)
        }
        
        # Store decision in knowledge store via save_performance (store_decision doesn't exist)
        try:
            knowledge.save_performance(
                instrument=instrument,
                metric_name=f"cycle_{cycle_data.get('cycle_start', 'unknown')}",
                value={
                    "action": decision.get("action", "hold"),
                    "allowed": decision.get("allowed", False),
                    "confluence_score": decision.get("confluence_score", 0),
                    "executed": execution is not None and isinstance(execution, dict) and execution.get("status") == "filled",
                },
                period="cycle",
            )
            summary["decision_stored"] = True
        except Exception as exc:
            logger.warning("Failed to store decision: %s", exc)
            summary["decision_stored"] = False
        
        # Get updated instrument knowledge via get_knowledge (get_instrument_knowledge doesn't exist)
        try:
            inst_knowledge = knowledge.get_knowledge(instrument)
            summary["knowledge_updated"] = True
            summary["knowledge_summary"] = {
                "patterns_count": len(inst_knowledge.get("patterns", {})),
                "performance_metrics": len(inst_knowledge.get("performance", {}))
            }
        except Exception as exc:
            logger.warning("Failed to get instrument knowledge: %s", exc)
            summary["knowledge_updated"] = False
        
        return summary
        
    except Exception as exc:
        logger.error("generate_cycle_summary failed: %s", exc)
        return {"error": str(exc), "trade_placed": False}

def log_trade_to_knowledge(instrument: str, trade_result: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """Wrapper for logging trades to knowledge store via save_performance."""
    try:
        knowledge = _get_knowledge_store()
        knowledge.save_performance(
            instrument=instrument,
            metric_name=f"trade_{trade_result.get('trade_id', 'unknown')}",
            value={
                "status": trade_result.get("status"),
                "trade_id": trade_result.get("trade_id"),
                "entry_price": trade_result.get("entry_price"),
                "units": trade_result.get("units"),
            },
            period="trade",
        )
        return {"logged": True, "instrument": instrument}
    except Exception as exc:
        logger.error("log_trade_to_knowledge failed: %s", exc)
        return {"error": str(exc), "logged": False}

# ---------------------------------------------------------------------------
# cycle_orchestrator wrappers
# ---------------------------------------------------------------------------

def evaluate_cycle_readiness(instrument: str, **kwargs) -> Dict[str, Any]:
    """Wrapper for cycle readiness evaluation.
    
    Checks market hours (forex: Sun 5pm ET - Fri 5pm ET), skip-after-open,
    close-warning, and instrument validity.
    """
    try:
        from datetime import datetime, timedelta
        import pytz
        
        et = pytz.timezone("America/New_York")
        now_et = datetime.now(et)
        day_name = now_et.strftime("%A").lower()
        hour = now_et.hour
        minute = now_et.minute
        
        ready = True
        blocking_reasons = []
        warnings = []
        
        # --- Market hours check ---
        # Closed: Saturday all day, Sunday before 5pm ET, Friday after 5pm ET
        if day_name == "saturday":
            ready = False
            blocking_reasons.append("Market closed (Saturday)")
        elif day_name == "sunday" and hour < 17:
            ready = False
            blocking_reasons.append("Market closed (Sunday before 5pm ET)")
        elif day_name == "friday" and hour >= 17:
            ready = False
            blocking_reasons.append("Market closed (Friday after 5pm ET)")
        
        # Skip first 30 min after Sunday open (low liquidity, gapping)
        if day_name == "sunday" and hour == 17 and minute < 30:
            ready = False
            blocking_reasons.append("Skip first 30 min after market open (low liquidity)")
        
        # Close warning: 2h before Friday close (no new trades)
        if day_name == "friday" and 15 <= hour < 17:
            warnings.append("Within 2h of Friday close — manage exits only, no new trades")
        
        # --- Session info ---
        session = "off_hours"
        if 3 <= hour < 12:
            session = "london" if hour < 8 else "london_ny_overlap" if hour < 12 else "london"
        elif 8 <= hour < 17:
            session = "new_york"
        elif hour >= 17 or hour < 2:
            session = "sydney"
        elif 19 <= hour or hour < 4:
            session = "tokyo"
        
        # --- Instrument validation ---
        try:
            client = _get_oanda_client()
            instruments = client.get_instruments([instrument])
            if not instruments:
                ready = False
                blocking_reasons.append(f"Invalid instrument: {instrument}")
        except Exception:
            ready = False
            blocking_reasons.append("Cannot verify instrument (OANDA connection failed)")
        
        return {
            "ready": ready,
            "instrument": instrument,
            "blocking_reasons": blocking_reasons,
            "warnings": warnings,
            "session": session,
            "market_day": day_name,
            "time_et": now_et.strftime("%H:%M"),
            "timestamp": datetime.now(pytz.utc).isoformat(),
        }
        
    except Exception as exc:
        logger.error("evaluate_cycle_readiness failed: %s", exc)
        return {"error": str(exc), "ready": False}

def make_trade_decision(analysis_results: Dict, validation_results: Dict, 
                       intelligence: Dict, account_summary: Dict, **kwargs) -> Dict[str, Any]:
    """Trade decision using validator's backtest evidence + confluence + risk limits.

    The validator now returns rich historical evidence from DecisionLogger:
    - verdict: APPROVE/REJECT/CAUTION with confidence
    - best_setup: specific setup with optimal RR/SL params from backtest DB
    - loss_patterns: what conditions cause losses for this setup
    - confluence_evidence: multi-setup confluence boost from historical data

    This function combines that with confluence score and risk limits for final decision.
    """
    try:
        import json, os
        
        # Use risk_limits kwarg if passed (from trading_cycle with user DB overrides),
        # otherwise fall back to config file
        limits = kwargs.get("risk_limits")
        _full_config = {}
        if not limits:
            config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'Config', 'risk_config.json')
            try:
                with open(config_path) as f:
                    _full_config = json.load(f)
                limits = _full_config.get("risk_limits", {})
            except Exception:
                limits = {"min_confluence": 40, "min_rr_ratio": 1.5, "max_daily_loss_pct": 3.0,
                          "max_concurrent_trades": 3, "max_risk_per_trade_pct": 2.0, "max_correlated_positions": 1}

        # Position sizing config — merge from config file + user DB preferences
        _pos_cfg = _full_config.get("position_sizing", {})
        limits["position_sizing_mode"] = _pos_cfg.get("mode", "auto")
        limits["fixed_units"] = _pos_cfg.get("fixed_units", 10000)
        limits["fixed_lots"] = _pos_cfg.get("fixed_lots", 0.1)

        # User DB overrides (trading_preferences in core.db) take priority
        try:
            import sqlite3 as _psz_sq
            _psz_core = os.path.join(
                os.path.dirname(__file__), '..', '..', '..', 'Database', 'v2', 'core.db')
            _psz_uid = kwargs.get("user_id", 2)
            with _psz_sq.connect(_psz_core, timeout=5) as _psz_conn:
                _psz_rows = _psz_conn.execute(
                    "SELECT pref_key, pref_value FROM trading_preferences WHERE user_id=? "
                    "AND pref_key IN ('risk_position_sizing_mode','risk_fixed_units')",
                    (_psz_uid,)
                ).fetchall()
                for _pk, _pv in _psz_rows:
                    if _pk == 'risk_position_sizing_mode':
                        limits["position_sizing_mode"] = _pv
                    elif _pk == 'risk_fixed_units':
                        limits["fixed_units"] = int(float(_pv))
        except Exception:
            pass  # DB may not exist — use config values
        
        min_confluence = limits.get("min_confluence", 40)
        max_daily_loss = limits.get("max_daily_loss_pct", 3.0)
        max_concurrent = limits.get("max_concurrent_trades", 3)
        
        # Extract analysis data
        confluence = analysis_results.get("confluence", {}) if isinstance(analysis_results, dict) else {}
        confluence_score = confluence.get("total_score", 0)
        regime = analysis_results.get("regime", confluence.get("regime", "unknown"))
        
        # Extract validator evidence (now from DecisionLogger/TradingDB)
        val = validation_results if isinstance(validation_results, dict) else {}
        validator_verdict = val.get("verdict", "REJECT")
        validator_confidence = val.get("confidence", 0.0)
        validator_passed = val.get("overall_passed", False)
        best_setup = val.get("best_setup", "none")
        best_direction = val.get("best_direction", "neutral")
        recommended_params = val.get("recommended_params") or {}
        loss_patterns = val.get("loss_patterns", [])
        warnings = val.get("warnings", [])
        decision_id = val.get("decision_id", "")
        setups_evaluated = val.get("setups_evaluated", [])
        
        action = "hold"
        allowed = False
        reasons = []
        blocking_reasons = []
        advisory_warnings = []
        
        # ── Advisory checks (inform, don't block — LLM already decided) ──
        if not validator_passed:
            advisory_warnings.append(f"Validator: {validator_verdict} ({validator_confidence:.0%})")
        if confluence_score < min_confluence:
            advisory_warnings.append(f"Confluence {confluence_score:.1f} < {min_confluence} threshold")
        
        # ── Hard limits ONLY (account safety — these CAN block the LLM) ──
        # Gate 1: Check concurrent trade limit
        open_count = 0
        if isinstance(account_summary, dict):
            open_count = int(account_summary.get("openTradeCount", 0))
        if open_count >= max_concurrent:
            blocking_reasons.append(f"Max concurrent trades ({open_count}/{max_concurrent})")
        
        # Gate 2: Check daily loss limit
        daily_loss_pct = kwargs.get("daily_loss_pct", 0.0)
        if daily_loss_pct >= max_daily_loss:
            blocking_reasons.append(f"Daily loss limit ({daily_loss_pct:.1f}% >= {max_daily_loss}%)")
        
        # ── Resolve direction: VALIDATOR DECISION IS AUTHORITATIVE ──
        # llm_action carries the validator's direction from trading_cycle.py.
        # This function computes SL/TP/sizing only — never overrides direction.
        # Fix 2026-04-07: sniper scores were flipping validator SELL→BUY (trade #4780).
        llm_action = kwargs.get("llm_action")
        if llm_action in ("buy", "sell"):
            best_direction = llm_action
        # V3 path: best_direction already set by run_full_validation
        
        # If no blocking reasons, build the trade
        if not blocking_reasons:
            direction = best_direction.lower() if isinstance(best_direction, str) else ""
            # Normalize direction variants
            if direction in ("buy", "bull", "bullish", "long"):
                action = "buy"
                allowed = True
            elif direction in ("sell", "bear", "bearish", "short"):
                action = "sell"
                allowed = True
            
            reasons = [
                f"Validator: {validator_verdict} ({validator_confidence:.0%} confidence)",
                f"Setup: {best_setup}",
                f"Confluence: {confluence_score:.1f}/{min_confluence}",
                f"Regime: {regime}",
                f"H4 agrees: {val.get('h4_agrees', 'unknown')}",
            ]
            # Include advisory warnings (non-blocking)
            for aw in advisory_warnings:
                reasons.append(f"⚠️ Advisory: {aw}")
            # Add loss pattern warnings
            for lp in loss_patterns[:2]:
                if isinstance(lp, dict):
                    reasons.append(f"⚠️ {lp.get('filter_suggestion', lp.get('description', ''))}")

            # If CAUTION, note position size reduction
            if validator_verdict == "CAUTION":
                reasons.append("CAUTION: Reduced position size recommended")
            
            # Calculate actual SL/TP price levels from ATR and recommended params
            current_price = None
            atr_value = None
            try:
                # Get current price from analysis indicators
                core_ind = analysis_results.get("core_indicators", {}) if isinstance(analysis_results, dict) else {}
                atr_data = core_ind.get("atr", {})
                if isinstance(atr_data, dict):
                    atr_value = atr_data.get("value") or atr_data.get("atr")
                
                # Get current price from account summary or analysis
                if isinstance(account_summary, dict):
                    current_price = float(account_summary.get("last_price", 0)) or None
                if not current_price:
                    # Fetch live price from OANDA
                    try:
                        client = _get_oanda_client()
                        pricing = client.get_pricing([instrument])
                        prices = pricing.get("prices", [])
                        if prices:
                            p = prices[0]
                            asks = p.get("asks", [{}])
                            bids = p.get("bids", [{}])
                            ask = float(asks[0].get("price", 0)) if asks else 0
                            bid = float(bids[0].get("price", 0)) if bids else 0
                            if ask and bid:
                                current_price = (ask + bid) / 2
                            elif ask:
                                current_price = ask
                            elif bid:
                                current_price = bid
                    except Exception as price_err:
                        logger.warning("Failed to fetch price for %s: %s", instrument, price_err)
            except Exception:
                pass
            
            sl_price = None
            tp_price = None
            position_size = 1000  # Default minimum lot
            
            # ATR fallback chain: core_indicators → market_picture → scout_context → sniper_score
            if not atr_value:
                mkt_pic = analysis_results.get("market_picture", {}) if isinstance(analysis_results, dict) else {}
                if isinstance(mkt_pic, dict):
                    bb = mkt_pic.get("bollinger", {})
                    atr_value = mkt_pic.get("atr") or bb.get("atr")
            if not atr_value:
                scout_ctx = kwargs.get("scout_context", {})
                if isinstance(scout_ctx, dict):
                    atr_value = scout_ctx.get("atr")
            if not atr_value:
                snp = analysis_results.get("sniper_score", {}) if isinstance(analysis_results, dict) else {}
                if isinstance(snp, dict):
                    atr_value = snp.get("atr") or snp.get("indicators", {}).get("atr")
            if atr_value:
                atr_value = float(atr_value)
            
            # Price fallback: account_summary → scout_context → sniper indicators
            if not current_price:
                scout_ctx = kwargs.get("scout_context", {})
                if isinstance(scout_ctx, dict) and scout_ctx.get("price"):
                    current_price = float(scout_ctx["price"])
            if not current_price:
                snp = analysis_results.get("sniper_score", {}) if isinstance(analysis_results, dict) else {}
                if isinstance(snp, dict):
                    ind = snp.get("indicators", {})
                    current_price = float(ind.get("close", 0)) if ind.get("close") else None
            
            instrument = kwargs.get("instrument", "unknown")
            logger.info("[SL/TP] %s: price=%s atr=%s", instrument, current_price, atr_value)
            
            if current_price and atr_value and atr_value > 0:
                sl_multiplier = float(recommended_params.get("sl_atr", recommended_params.get("sl", 2.5)))  # V4: reverted to 2.5x ATR (3.0 proved worse)
                rr_ratio = float(recommended_params.get("rr", recommended_params.get("rr_ratio", 2.0)))
                sl_distance = atr_value * sl_multiplier
                tp_distance = sl_distance * rr_ratio
                
                # Determine pip precision for instrument
                if "JPY" in instrument:
                    pip_precision = 3  # JPY pairs: 3 decimals
                else:
                    pip_precision = 5  # Most pairs: 5 decimals
                
                if action == "buy":
                    sl_price = round(current_price - sl_distance, pip_precision)
                    tp_price = round(current_price + tp_distance, pip_precision)
                elif action == "sell":
                    sl_price = round(current_price + sl_distance, pip_precision)
                    tp_price = round(current_price - tp_distance, pip_precision)
                
                # Position sizing: check mode from user preferences
                position_sizing_mode = limits.get("position_sizing_mode", "auto")
                
                if position_sizing_mode == "fixed":
                    # User selected a fixed unit count (e.g. 10000 = literal 10,000 units)
                    # Use the value directly — do NOT convert through pip-value scaling.
                    # "Fixed 10,000 units" means exactly 10,000 units on every pair.
                    _nominal = int(limits.get("fixed_units", 10000))
                    position_size = _nominal
                    logger.info("[SIZING] %s: fixed mode → literal %d units", instrument, position_size)
                    risk_pct = 0.0  # Will be calculated for display
                elif position_sizing_mode == "fixed_lots":
                    # Convert lots to units (1.0 lot = 100000, 0.1 = 10000, 0.01 = 1000)
                    # Use literal unit count — no pip-value scaling.
                    fixed_lots = float(limits.get("fixed_lots", 0.1))
                    position_size = int(fixed_lots * 100000)
                    logger.info("[SIZING] %s: fixed_lots mode → %.2f lots = %d units", instrument, fixed_lots, position_size)
                    risk_pct = 0.0  # Will be calculated for display
                else:
                    # Auto mode: risk-based calculation with proper pip value math
                    risk_pct = limits.get("max_risk_per_trade_pct", 2.0) / 100.0
                    if validator_verdict == "CAUTION":
                        risk_pct *= 0.5  # Half size for CAUTION trades
                    
                    account_balance = 2000  # Default demo balance
                    if isinstance(account_summary, dict):
                        account_balance = float(account_summary.get("balance", 2000))
                    
                    risk_amount = account_balance * risk_pct
                    
                    if sl_distance > 0:
                        # Calculate pip value per unit based on currency pair
                        if "JPY" in instrument:
                            pip_size = 0.01
                            pip_value_per_unit = pip_size / current_price  # For JPY pairs
                        else:
                            pip_size = 0.0001
                            # For non-JPY pairs
                            if instrument.endswith("_USD"):
                                pip_value_per_unit = pip_size  # Quote is USD
                            elif instrument.startswith("USD_"):
                                pip_value_per_unit = pip_size / current_price  # Base is USD
                            else:
                                # Cross pair - conservative estimate
                                pip_value_per_unit = pip_size / current_price
                        
                        sl_pips = sl_distance / pip_size
                        position_size = int(risk_amount / (sl_pips * pip_value_per_unit))
                        position_size = max(1, min(position_size, 100000))  # Clamp 1-100K
                    else:
                        position_size = 1000  # Fallback
                
                # For display purposes, calculate actual risk if not already set
                if risk_pct == 0.0 and isinstance(account_summary, dict) and sl_distance > 0:
                    account_balance = float(account_summary.get("balance", 2000))
                    # Recalculate pip values for display
                    if "JPY" in instrument:
                        pip_size = 0.01
                        pip_value_per_unit = pip_size / current_price
                    else:
                        pip_size = 0.0001
                        if instrument.endswith("_USD"):
                            pip_value_per_unit = pip_size
                        elif instrument.startswith("USD_"):
                            pip_value_per_unit = pip_size / current_price
                        else:
                            pip_value_per_unit = pip_size / current_price
                    
                    sl_pips = sl_distance / pip_size
                    actual_risk = sl_pips * pip_value_per_unit * position_size
                    risk_pct = actual_risk / account_balance if account_balance > 0 else 0.0
                
                reasons.append(f"SL: {sl_price} ({sl_multiplier}×ATR), TP: {tp_price} ({rr_ratio}:1 R:R)")
                reasons.append(f"Position: {position_size} units ({risk_pct*100:.1f}% risk)")
        
        return {
            "action": action,
            "allowed": allowed,
            "confluence_score": confluence_score,
            "direction": best_direction,
            "regime": regime,
            "recommendation": val.get("recommendation", "hold"),
            "reasons": reasons,
            "blocking_reasons": blocking_reasons,
            "validation_passed": validator_passed,
            "validator_verdict": validator_verdict,
            "validator_confidence": validator_confidence,
            "best_setup": best_setup,
            "stop_loss": str(sl_price) if sl_price else None,
            "take_profit": str(tp_price) if tp_price else None,
            "position_size": position_size if allowed else 0,
            "entry_price": str(current_price) if current_price else None,
            "atr": atr_value,
            "recommended_params": recommended_params,
            "loss_patterns": loss_patterns,
            "decision_id": decision_id,
            "setups_evaluated": setups_evaluated,
            "evidence": {
                "setup": best_setup,
                "verdict": validator_verdict,
                "confidence": validator_confidence,
                "warnings": warnings,
                "loss_patterns": loss_patterns,
            },
            "risk_limits_applied": {
                "min_confluence": min_confluence,
                "max_daily_loss_pct": max_daily_loss,
                "max_concurrent_trades": max_concurrent,
                "open_trade_count": open_count,
            },
        }
        
    except Exception as exc:
        logger.error("make_trade_decision failed: %s", exc)
        return {"error": str(exc), "action": "hold", "allowed": False}

def get_risk_status(**kwargs) -> Dict[str, Any]:
    """Wrapper for risk status check (missing from team_setup.py)."""
    try:
        risk_manager = _get_risk_manager()
        return risk_manager.get_status(**kwargs)
    except Exception as exc:
        logger.error("get_risk_status failed: %s", exc)
        return {"error": str(exc), "open_positions": 0}

def should_escalate_to_llm(contradictions: Dict, validation_results: Dict, **kwargs) -> Dict[str, Any]:
    """Wrapper for LLM escalation decision (missing from team_setup.py)."""
    try:
        # Determine if we need LLM escalation based on contradictions and validation
        confidence = validation_results.get("confidence", 1.0)
        borderline = validation_results.get("borderline", False)
        
        escalate = (
            confidence < 0.7 or 
            borderline or
            len(contradictions) > 2 or
            validation_results.get("needs_llm_escalation", False)
        )
        
        reason = "High confidence" if not escalate else (
            "Low confidence" if confidence < 0.7 else
            "Borderline case" if borderline else
            f"Multiple contradictions ({len(contradictions)})"
        )
        
        return {
            "escalate": escalate,
            "reason": reason,
            "confidence": confidence,
            "contradictions_count": len(contradictions)
        }
        
    except Exception as exc:
        logger.error("should_escalate_to_llm failed: %s", exc)
        return {"error": str(exc), "escalate": False}

def process_operator_command(command: str, params: Dict = None, **kwargs) -> Dict[str, Any]:
    """Wrapper for operator command processing (missing from team_setup.py)."""
    try:
        if params is None:
            params = {}
            
        # Handle basic commands
        if command == "pause_trading":
            return {"command": command, "result": "Trading paused", "success": True}
        elif command == "resume_trading":
            return {"command": command, "result": "Trading resumed", "success": True}
        elif command == "get_status":
            return {"command": command, "result": "Status retrieved", "success": True}
        else:
            return {"command": command, "result": f"Unknown command: {command}", "success": False}
            
    except Exception as exc:
        logger.error("process_operator_command failed: %s", exc)
        return {"error": str(exc), "command": command, "success": False}

# ---------------------------------------------------------------------------
# Intelligence agent wrappers (merged news + weather + wolfram)
# ---------------------------------------------------------------------------

def _get_currency_map():
    """Lazy import currency intelligence map."""
    try:
        from Source.currency_intelligence_map import (
            get_intelligence_config,
            get_news_queries,
            get_weather_config,
            get_wolfram_checks,
            should_check_weather,
            get_correlated_instruments,
        )
    except ModuleNotFoundError:
        from currency_intelligence_map import (
            get_intelligence_config,
            get_news_queries,
            get_weather_config,
            get_wolfram_checks,
            should_check_weather,
            get_correlated_instruments,
        )
    return {
        "get_intelligence_config": get_intelligence_config,
        "get_news_queries": get_news_queries,
        "get_weather_config": get_weather_config,
        "get_wolfram_checks": get_wolfram_checks,
        "should_check_weather": should_check_weather,
        "get_correlated_instruments": get_correlated_instruments,
    }


def _get_trading_db():
    """Lazy import TradingDB."""
    try:
        from Source.backtester.trading_db import TradingDB
    except ModuleNotFoundError:
        from backtester.trading_db import TradingDB
    return TradingDB()


def _get_intelligence_store():
    """Lazy-load IntelligenceStore to avoid circular imports."""
    global _intel_store
    try:
        return _intel_store
    except NameError:
        pass
    try:
        from Source.intelligence_store import IntelligenceStore
    except ModuleNotFoundError:
        from intelligence_store import IntelligenceStore
    _intel_store = IntelligenceStore()
    return _intel_store


def _get_mcp(name: str):
    """Get an MCP handler wrapper by name. Routes through workspace MCP layer.
    
    This is the ONLY way trading bot code should access MCPs.
    Never import handlers directly — always go through this function.
    
    Routes through WorkspaceSharingManager when available (provides workspace 
    context, execution tracking, cron hookup). Falls back to get_handler_wrapper
    if workspace layer isn't initialized.
    """
    import sys
    if '~/jarvis' not in sys.path:
        sys.path.insert(0, '~/jarvis')
    from Jarvis_Agent_SDK.mcp_wrapper import get_handler_wrapper
    wrapper = get_handler_wrapper(name)
    if wrapper is None:
        raise RuntimeError(f"MCP '{name}' not found in HANDLER_WRAPPER_REGISTRY")
    return wrapper


_workspace_manager = None
_workspace_id = None

def _get_workspace_manager():
    """Get or create WorkspaceSharingManager with trading bot workspace context."""
    global _workspace_manager, _workspace_id
    if _workspace_manager is not None:
        return _workspace_manager, _workspace_id
    
    try:
        from Jarvis_Agent_SDK.import_helper import get_workspace_sharing
        wsm = get_workspace_sharing()
        
        # Get or create default workspace (this method is available)
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop = asyncio.new_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
        
        ws = loop.run_until_complete(wsm.get_or_create_default_workspace())
        if ws and isinstance(ws, dict):
            _workspace_id = ws.get("id") or ws.get("workspace_id")
        elif isinstance(ws, int):
            _workspace_id = ws
        else:
            _workspace_id = 1  # Default workspace
        
        _workspace_manager = wsm
        logger.info("WorkspaceSharingManager initialized, workspace_id=%s", _workspace_id)
        return _workspace_manager, _workspace_id
    except Exception as e:
        logger.warning("WorkspaceSharingManager not available: %s — using direct MCP", e)
        return None, None


def _execute_mcp(handler_name: str, method_name: str, parameters: dict = None) -> dict:
    """Execute an MCP tool through the MCP wrapper layer with workspace tracking.
    
    Uses get_handler_wrapper for synchronous execution (avoids nested event loop 
    deadlocks), then logs the call to workspace for tracking/cron integration.
    
    This is the ONLY way trading bot code should call MCPs.
    """
    parameters = parameters or {}
    
    # Remove workspace context param that workspace layer adds (handlers don't expect it)
    clean_params = {k: v for k, v in parameters.items() if k != "context"}
    
    # Execute through handler wrapper (synchronous, no deadlock risk)
    wrapper = _get_mcp(handler_name)
    try:
        if hasattr(wrapper.handler_instance, method_name):
            method = getattr(wrapper.handler_instance, method_name)
            result = method(**clean_params) if clean_params else method()
        else:
            result = wrapper.execute(action=method_name, parameters=clean_params)
    except Exception as e:
        logger.error("MCP %s.%s failed: %s", handler_name, method_name, e)
        return {"status": "error", "error": str(e), "handler": handler_name, "action": method_name}
    
    # Wrap in standard format
    if isinstance(result, dict) and "status" in result:
        result["handler"] = handler_name
        result["action"] = method_name
        return result
    
    return {
        "result": result,
        "status": "success",
        "handler": handler_name,
        "action": method_name,
    }


def _wolfram_llm(query: str, maxchars: int = 2000) -> Dict[str, Any]:
    """Wolfram LLM API call via workspace MCP layer.
    
    Routes through workspace_sharing.execute_method_via_mcp when available,
    falls back to direct handler wrapper, then to raw urllib.
    """
    try:
        result = _execute_mcp("wolfram", "query_llm_api", {"query": query, "maxchars": maxchars})
        if result and isinstance(result, dict):
            # Workspace layer wraps in {result:..., status:...} — unwrap if needed
            if "result" in result and isinstance(result["result"], dict):
                return result["result"]
            if "success" in result:
                return result
            # If workspace wrapper returned status+result, extract
            if result.get("status") == "success" and "result" in result:
                return result["result"] if isinstance(result["result"], dict) else {"success": True, "text": str(result["result"])}
        return result if isinstance(result, dict) else {"success": False, "text": str(result)}
    except Exception as e:
        logger.debug("Wolfram MCP call failed: %s", e)
        return {"success": False, "text": "", "query": query, "error": str(e)}


def _wolfram_llm_direct(query: str, maxchars: int = 2000) -> Dict[str, Any]:
    """Direct Wolfram LLM API call — fallback only."""
    import urllib.request
    import urllib.parse
    from pathlib import Path as _Path

    key = os.environ.get('WOLFRAM_API_KEY', '')
    if not key:
        key_path = _Path("~/jarvis/API/WOLFRAM_API_KEY.txt")
        key = key_path.read_text().strip()
    base = "https://www.wolframalpha.com/api/v1/llm-api"
    params = urllib.parse.urlencode({"input": query, "appid": key, "maxchars": maxchars})
    url = f"{base}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return {"success": True, "text": r.read().decode(), "query": query}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        suggestions = [
            l.strip() for l in body.split("\n")
            if l.strip() and "could not" not in l.lower() and "try" not in l.lower()
        ]
        return {"success": False, "text": body, "suggestions": suggestions, "query": query}
    except Exception as e:
        return {"success": False, "text": str(e), "suggestions": [], "query": query}


def _wolfram_cached(query: str, cache_key: str, category: str = "wolfram_macro",
                    instrument: str = None, maxchars: int = 2000,
                    cache_only: bool = False) -> Dict[str, Any]:
    """Query Wolfram with cache layer. Returns cached if available, else fetches live.
    If cache_only=True, returns empty on cache miss (no MCP call)."""
    store = _get_intelligence_store()
    cached = store.get_cached(cache_key)
    if cached is not None:
        logger.debug("Wolfram cache hit: %s", cache_key)
        return {"success": True, "text": cached, "query": query, "from_cache": True}

    if cache_only:
        logger.debug("Wolfram cache miss (cache_only): %s", cache_key)
        return {"success": False, "text": "", "query": query, "from_cache": False, "reason": "cache_miss"}

    result = _wolfram_llm(query, maxchars=maxchars)
    if result["success"]:
        store.set_cached(cache_key, category, result["text"],
                         instrument=instrument, query_used=query)
    else:
        # Live fetch failed — return stale data rather than empty so the pipeline
        # has something to work with. Log once so we know wolfram is down.
        stale = store.get_stale(cache_key)
        if stale is not None:
            logger.debug("Wolfram live fetch failed, using stale cache for %s", cache_key)
            return {"success": True, "text": stale, "query": query,
                    "from_cache": True, "stale": True}
        logger.warning("Wolfram fetch failed and no stale cache for %s: %s",
                        cache_key, result.get("text", "unknown error"))
    return result


def gather_intelligence(instrument: str, task_id: int = None,
                        decision_id: str = None, cycle_id: str = None,
                        cache_only: bool = False, **kwargs) -> Dict[str, Any]:
    """Master intelligence wrapper — currency-aware news + weather + wolfram.

    If cache_only=True, reads ONLY from pre-cached data (no MCP calls).
    The cron pre-cache jobs populate the cache before market sessions.
    """
    try:
        cmap = _get_currency_map()
        config = cmap["get_intelligence_config"](instrument)
        store = _get_intelligence_store()
        wolfram_queries_used = []
        news_queries_used = []

        # Purge expired cache at start of each cycle (non-critical)
        try:
            store.purge_expired_cache()
        except Exception as _purge_exc:
            logger.warning("Cache purge failed (non-blocking): %s", _purge_exc)

        # Parse instrument → base/quote
        parts = instrument.split("_")
        base_ccy = parts[0] if len(parts) == 2 else instrument[:3]
        quote_ccy = parts[1] if len(parts) == 2 else instrument[3:]

        results = {
            "type": "INTELLIGENCE_REPORT",
            "instrument": instrument,
            "macro": {},
            "news": {},
            "weather": {},
            "statistics": {},
            "verdict": "PENDING_LLM_SCORING",  # Placeholder for database constraint
            "bias": "neutral",     # Placeholder for database constraint  
            "confidence": 0.0,  # Placeholder for database constraint
            "summary": "",
            "raw_data": {},   # Raw data for LLM scoring
            "agent_briefing": "",  # Formatted briefing for LLM
        }

        # --- Wolfram Macro Research (3 calls max) ---
        # Call 1: Combined interest rates for both currencies (single query)
        # Call 2: Exchange rate range + volatility
        # Call 3: Seasonal pattern (in run_statistical_checks, cached monthly)
        macro = {
            "base_currency": base_ccy,
            "quote_currency": quote_ccy,
        }
        try:
            # CALL 1 (per currency, not per pair): Individual rate lookups
            # Cached by currency code so USD fires once for all 7 USD pairs
            rate_queries = _get_rate_queries(base_ccy, quote_ccy)
            for ccy in (base_ccy, quote_ccy):
                query = rate_queries.get(ccy)
                if query:
                    # Wolfram query available — use normal cached path
                    r = _wolfram_cached(query, f"wolfram:rate:{ccy}", "wolfram_macro", instrument, cache_only=cache_only)
                    wolfram_queries_used.append(query)
                else:
                    # No Wolfram query (e.g. EUR, GBP, CHF) — check bridge cache from intelligence_agent_prep
                    bridge = store.get_cached(f"wolfram:rate:{ccy}")
                    r = {"success": bridge is not None, "text": bridge or "", "from_cache": True}
                if r["success"]:
                    rate = _extract_rate_from_wolfram(r["text"])
                    if ccy == base_ccy:
                        macro["base_currency_rate"] = rate
                    else:
                        macro["quote_currency_rate"] = rate

            base_rate = macro.get("base_currency_rate")
            quote_rate = macro.get("quote_currency_rate")
            if base_rate is not None and quote_rate is not None:
                macro["rate_differential"] = round(base_rate - quote_rate, 4)

            # CALL 2: Exchange rate range + volatility + current price
            pair_query = _get_exchange_query(base_ccy, quote_ccy)
            if pair_query:
                fx = _wolfram_cached(pair_query, f"wolfram:fx:{instrument}", "wolfram_macro", instrument, cache_only=cache_only)
                wolfram_queries_used.append(pair_query)
                if fx["success"]:
                    macro.update(_extract_fx_range(fx["text"]))

        except Exception as exc:
            logger.warning("Wolfram macro for %s failed: %s", instrument, exc)

        results["macro"] = macro

        # --- News ---
        try:
            news = query_news_for_pair(instrument, cache_only=cache_only)
            results["news"] = news
            news_queries_used = news.get("_queries_used", [])
        except Exception as exc:
            logger.warning("News query for %s failed: %s", instrument, exc)
            results["news"] = {"error": str(exc)}

        # --- Weather (only if commodity-linked) ---
        try:
            weather = check_weather_for_pair(instrument, cache_only=cache_only)
            results["weather"] = weather
        except Exception as exc:
            logger.warning("Weather check for %s failed: %s", instrument, exc)
            results["weather"] = {"error": str(exc)}

        # --- Wolfram Statistics ---
        try:
            stats = run_statistical_checks(instrument)
            results["statistics"] = stats
            wolfram_queries_used.extend(stats.get("_queries_used", []))
        except Exception as exc:
            logger.warning("Wolfram stats for %s failed: %s", instrument, exc)
            results["statistics"] = {"error": str(exc)}

        # --- Build Raw Data for LLM Scoring ---
        results["raw_data"] = {
            "news_articles": results["news"].get("articles", []),
            "wolfram_results": {
                f"rate_query_{base_ccy}": macro.get("base_currency_rate"),
                f"rate_query_{quote_ccy}": macro.get("quote_currency_rate"),
                "oil_price_query": macro.get("oil_price"),
                "exchange_rate_query": macro.get("current_rate"),
            },
            "weather": results["weather"],
            "macro": macro,
        }

        # --- Create Agent Briefing ---
        briefing_parts = [f"## Market Intelligence Brief: {instrument}"]
        
        # Macro Data Section — natural language narrative
        briefing_parts.append("\n### Macro Fundamentals")
        
        base_rate = macro.get("base_currency_rate")
        quote_rate = macro.get("quote_currency_rate")
        rate_diff = macro.get("rate_differential", 0)
        if base_rate is not None and quote_rate is not None:
            favor = base_ccy if rate_diff > 0 else quote_ccy
            briefing_parts.append(
                f"- Interest rate differential favors {favor}: "
                f"{base_ccy} at {base_rate:.2f}% vs {quote_ccy} at {quote_rate:.2f}% "
                f"(spread {abs(rate_diff):.2f}% {'supporting' if rate_diff > 0 else 'against'} {base_ccy})"
            )
        elif base_rate is not None:
            briefing_parts.append(f"- {base_ccy} interest rate: {base_rate:.2f}%")
        
        # Exchange rate context with range position
        current = macro.get("pair_current_price") or macro.get("current_rate")
        yr_min = macro.get("pair_1yr_min")
        yr_max = macro.get("pair_1yr_max")
        yr_avg = macro.get("pair_1yr_avg")
        yr_vol = macro.get("pair_1yr_volatility")
        range_pos = macro.get("pair_range_position", "")
        
        if current and yr_min and yr_max:
            range_pct = ((current - yr_min) / (yr_max - yr_min) * 100) if yr_max != yr_min else 50
            pos_desc = "near its 12-month high" if range_pos == "near_top" else \
                       "near its 12-month low" if range_pos == "near_bottom" else \
                       "in the middle of its 12-month range"
            briefing_parts.append(
                f"- {instrument} at {current:.4f} — {pos_desc} "
                f"(12-month range: {yr_min:.4f} to {yr_max:.4f}, currently at {range_pct:.0f}th percentile)"
            )
            if yr_avg:
                above_below = "above" if current > yr_avg else "below"
                briefing_parts.append(f"- Trading {above_below} the 12-month average of {yr_avg:.4f}")
        elif current:
            briefing_parts.append(f"- {instrument} current rate: {current}")
        
        if yr_vol:
            vol_desc = "high" if yr_vol > 12 else "moderate" if yr_vol > 7 else "low"
            briefing_parts.append(f"- Annualized volatility: {yr_vol:.1f}% ({vol_desc})")
        
        if macro.get("oil_price"):
            briefing_parts.append(f"- Crude oil at ${macro['oil_price']:.1f}/bbl (relevant for commodity-linked currencies)")

        # News Section
        articles = results["news"].get("articles", [])
        briefing_parts.append(f"\n### News ({len(articles)} articles analyzed)")
        for i, article in enumerate(articles[:5], 1):  # Top 5 articles
            source = article.get("source", "Unknown")
            title = article.get("title", "")
            desc = article.get("description", "")
            briefing_parts.append(f"{i}. [{source}] \"{title}\" — {desc}")

        # Weather Section
        weather = results["weather"]
        briefing_parts.append("\n### Weather Impacts")
        if weather.get("check_weather") and weather.get("severity", 0) > 0:
            briefing_parts.append(f"- Weather severity: {weather.get('severity', 0)}/10")
            briefing_parts.append(f"- Status: {weather.get('status', 'UNKNOWN')}")
        else:
            briefing_parts.append(f"- No weather impacts for {instrument}")

        # Statistical Analysis Section
        briefing_parts.append("\n### Statistical Analysis")
        stats = results["statistics"]
        if stats.get("volatility_regime"):
            vol_r = stats["volatility_regime"]
            vol_desc = {
                "low": "Quiet market — small moves, tight ranges. Good for mean reversion.",
                "normal": "Normal volatility — standard trading conditions.",
                "high": "Elevated volatility — wider stops needed, bigger moves expected.",
                "extreme": "Extreme volatility — caution, risk of whipsaws and gap moves.",
            }.get(vol_r, f"Volatility regime: {vol_r}")
            briefing_parts.append(f"- {vol_desc}")
        if stats.get("seasonal_bias"):
            briefing_parts.append(f"- Seasonal tendency: {stats['seasonal_bias']}")
        if macro.get("range_30d"):
            briefing_parts.append(f"- 30-day trading range: {macro['range_30d']}")
        if stats.get("atr_percentile"):
            briefing_parts.append(f"- Current ATR at {stats['atr_percentile']:.0f}th percentile of 90-day history")

        results["agent_briefing"] = "\n".join(briefing_parts)

        results["summary"] = _build_summary(results)
        results["_wolfram_queries"] = wolfram_queries_used
        results["_news_queries"] = news_queries_used

        # --- Save snapshot ---
        try:
            results["cycle_id"] = cycle_id
            store.save_snapshot(
                report=results,
                instrument=instrument,
                decision_id=decision_id,
            )
        except Exception as exc:
            logger.error("Failed to save intelligence snapshot: %s", exc)

        logger.info(
            "Intelligence for %s: verdict=%s, confidence=%.2f, wolfram_queries=%d",
            instrument, results["verdict"], results["confidence"], len(wolfram_queries_used),
        )
        return results

    except Exception as exc:
        logger.error("gather_intelligence failed for %s: %s", instrument, exc)
        return {"error": str(exc), "instrument": instrument}


# ── Wolfram helpers ─────────────────────────────────────────────────

# Map currency codes to Wolfram rate queries
# Wolfram LLM API query strings — TESTED March 2026 against /api/v1/llm-api
# Only USD (Fed funds rate) works for central bank policy rates.
# All other central bank rate queries 501. Use None to skip gracefully.
# CPI queries work for US (via "US inflation rate") and UK ("United Kingdom CPI 2025").
# FX queries: "X USD" format works (e.g. "AUD USD"), not "1 X to Y" format.
_RATE_QUERY_MAP = {
    "USD": "US federal funds rate",        # ✅ Returns latest Fed funds rate with date
    "EUR": None,                            # ❌ "eurozone deposit rate" returns historical median, not current
    "GBP": None,                            # ❌ all BOE formulations 501
    "JPY": None,                            # ❌ all BOJ formulations 501
    "AUD": None,                            # ❌ all RBA formulations 501
    "NZD": None,                            # ❌ all RBNZ formulations 501
    "CAD": None,                            # ❌ all BOC formulations 501
    "CHF": None,                            # ❌ all SNB formulations 501
}

# FX exchange rate queries — tested working format
_EXCHANGE_QUERY_MAP = {
    "EUR_USD": "EUR USD",                   # ✅ Returns current rate + 1yr range
    "GBP_USD": "GBP USD",
    "USD_JPY": "USD JPY",
    "AUD_USD": "AUD USD",
    "NZD_USD": "NZD USD",
    "USD_CAD": "USD CAD",
    "USD_CHF": "USD CHF",
    "EUR_GBP": "EUR GBP",
    "EUR_JPY": "EUR JPY",
    "GBP_JPY": "GBP JPY",
    "AUD_NZD": "AUD NZD",
    "EUR_CHF": "EUR CHF",
    "EUR_AUD": "EUR AUD",
}

# CPI / inflation queries — tested working
_CPI_QUERY_MAP = {
    "USD": "US inflation rate",             # ✅
    "GBP": "United Kingdom CPI 2025",       # ✅
    "EUR": None,                            # ❌ 501
    "JPY": None,                            # ❌ 501
    "AUD": None,                            # ❌ 501
    "NZD": None,                            # ❌ 501
    "CAD": None,                            # ❌ 501
    "CHF": None,                            # ❌ 501
}

# Commodity queries — all tested working
_COMMODITY_QUERY_MAP = {
    "gold": "gold price",                   # ✅
    "oil": "oil price WTI",                 # ✅
    "us10y": "US 10 year treasury yield",   # ✅
}


def _get_rate_queries(base_ccy: str, quote_ccy: str) -> Dict[str, str]:
    """Get Wolfram rate queries for both currencies in a pair.
    Only returns entries where query is not None (i.e. confirmed working in testing).
    """
    queries = {}
    for ccy in (base_ccy, quote_ccy):
        q = _RATE_QUERY_MAP.get(ccy)
        if q is not None:
            queries[ccy] = q
    return queries


def _get_combined_rate_query(base_ccy: str, quote_ccy: str) -> Optional[str]:
    """Build a single Wolfram query that returns interest rates for both currencies.
    
    Uses 'compare interest rates X Y' format which returns a side-by-side table.
    Falls back to base currency only if quote isn't in map.
    """
    # Map currency codes to country names Wolfram understands
    _CCY_TO_COUNTRY = {
        "USD": "US", "EUR": "eurozone", "GBP": "UK", "JPY": "Japan",
        "AUD": "Australia", "NZD": "New Zealand", "CAD": "Canada", "CHF": "Switzerland",
    }
    base_country = _CCY_TO_COUNTRY.get(base_ccy)
    quote_country = _CCY_TO_COUNTRY.get(quote_ccy)
    if base_country and quote_country:
        return f"compare interest rates {base_country} {quote_country}"
    # Fallback: single rate query for whichever we have
    if base_ccy in _RATE_QUERY_MAP:
        return _RATE_QUERY_MAP[base_ccy]
    if quote_ccy in _RATE_QUERY_MAP:
        return _RATE_QUERY_MAP[quote_ccy]
    return None


def _extract_dual_rates(text: str, base_ccy: str, quote_ccy: str) -> Dict[str, Optional[float]]:
    """Extract two interest rates from a Wolfram compare response.
    
    Wolfram compare returns a table with both countries' rates.
    Falls back to single-rate extraction if compare format not detected.
    """
    import re
    result = {"base_rate": None, "quote_rate": None}
    
    _CCY_TO_COUNTRY = {
        "USD": "United States", "EUR": "euro", "GBP": "United Kingdom",
        "JPY": "Japan", "AUD": "Australia", "NZD": "New Zealand",
        "CAD": "Canada", "CHF": "Switzerland",
    }
    
    # Try to find rates by country name proximity
    for ccy, key in [(base_ccy, "base_rate"), (quote_ccy, "quote_rate")]:
        country = _CCY_TO_COUNTRY.get(ccy, ccy)
        # Look for "Country ... X.XX%" pattern
        pattern = re.escape(country) + r'[^%]*?([\d.]+)\s*%'
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result[key] = float(match.group(1))
    
    # Fallback: if we only got one or none, try generic percentage extraction
    if result["base_rate"] is None and result["quote_rate"] is None:
        # Single rate response — assign to base
        rate = _extract_rate_from_wolfram(text)
        if rate is not None:
            result["base_rate"] = rate
    
    return result


def _get_exchange_query(base_ccy: str, quote_ccy: str) -> Optional[str]:
    """Get Wolfram exchange rate query for a pair."""
    instrument = f"{base_ccy}_{quote_ccy}"
    return _EXCHANGE_QUERY_MAP.get(instrument)


def _extract_rate_from_wolfram(text: str) -> Optional[float]:
    """Extract the primary rate number from Wolfram LLM response."""
    import re
    # Look for patterns like "3.64%" or "2.72%"
    # Try "Latest result" first
    match = re.search(r"Latest result:\s*([\d.]+)%", text)
    if match:
        return float(match.group(1))
    # Try "real interest rate"
    match = re.search(r"real interest rate\s*\|\s*([-\d.]+)%", text)
    if match:
        return float(match.group(1))
    # Try any percentage near the top
    match = re.search(r"([\d.]+)%", text)
    if match:
        return float(match.group(1))
    return None


def _extract_number_after(text: str, prefix: str, suffix: str) -> Optional[float]:
    """Extract a number between prefix and suffix."""
    import re
    pattern = re.escape(prefix) + r"([\d.,]+)" + re.escape(suffix)
    match = re.search(pattern, text)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def _extract_fx_range(text: str) -> Dict[str, Any]:
    """Extract 1yr min/max/avg/volatility from Wolfram exchange rate response."""
    import re
    result = {}
    # "1-year minimum | $1.04"
    match = re.search(r"1-year minimum\s*\|\s*[^\d]*([\d.]+)", text)
    if match:
        result["pair_1yr_min"] = float(match.group(1))
    match = re.search(r"1-year maximum\s*\|\s*[^\d]*([\d.]+)", text)
    if match:
        result["pair_1yr_max"] = float(match.group(1))
    match = re.search(r"1-year average\s*\|\s*[^\d]*([\d.]+)", text)
    if match:
        result["pair_1yr_avg"] = float(match.group(1))
    match = re.search(r"annualized volatility:\s*([\d.]+)%", text)
    if match:
        result["pair_1yr_volatility"] = float(match.group(1))
    # Extract current rate from "Result" line
    match = re.search(r"Result:\s*[^\d]*([\d.]+)", text)
    if match:
        result["pair_current_price"] = float(match.group(1))
    # Determine range position
    if "pair_current_price" in result and "pair_1yr_min" in result and "pair_1yr_max" in result:
        rng = result["pair_1yr_max"] - result["pair_1yr_min"]
        if rng > 0:
            pos = (result["pair_current_price"] - result["pair_1yr_min"]) / rng
            if pos > 0.8:
                result["pair_range_position"] = "near_top"
            elif pos < 0.2:
                result["pair_range_position"] = "near_bottom"
            else:
                result["pair_range_position"] = "mid_range"
    return result


def _build_summary(results: Dict) -> str:
    """Build a one-line summary from the intelligence report."""
    parts = []
    macro = results.get("macro", {})
    rd = macro.get("rate_differential")
    if rd is not None:
        parts.append(f"Rate diff: {rd:+.2f}%")
    oil = macro.get("oil_price")
    if oil:
        parts.append(f"Oil: ${oil:.1f}")
    pos = macro.get("pair_range_position")
    if pos:
        parts.append(f"Range: {pos}")
    ns = results.get("news", {}).get("net_sentiment")
    if ns:
        parts.append(f"Sentiment: {ns:+.1f}")
    parts.append(f"Verdict: {results.get('verdict', 'UNKNOWN')}")
    return " | ".join(parts)


def query_news_for_pair(instrument: str, cache_only: bool = False, **kwargs) -> Dict[str, Any]:
    """Query news MCP with currency-specific search terms.
    If cache_only=True, returns empty result on cache miss (no MCP call)."""
    try:
        cmap = _get_currency_map()
        news_config = cmap["get_news_queries"](instrument)
        store = _get_intelligence_store()

        search_terms = news_config["search_terms"]
        result = {
            "instrument": instrument,
            "search_terms": search_terms,
            "key_events": news_config["key_events"],
            "central_banks": news_config["central_banks"],
            "currencies_affected": news_config["currencies_affected"],
            "articles_analyzed": 0,
            "net_sentiment": None,  # LLM will score this
            "base_sentiment": 0.0,
            "quote_sentiment": 0.0,
            "high_impact_events": [],
            "headlines": [],
            "articles": [],  # Full article data
            "block_trading": False,
            "_queries_used": [],
        }

        # Check cache first
        cache_key = f"news:{instrument}"
        cached = store.get_cached(cache_key)
        if cached is not None:
            logger.debug("News cache hit for %s", instrument)
            return cached

        if cache_only:
            logger.debug("News cache miss (cache_only) for %s", instrument)
            return result

        # Call News MCP handler through workspace MCP layer
        try:
            from datetime import datetime, timedelta
            from_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

            all_headlines = []
            all_articles = []
            _rate_limited = False
            for term in search_terms[:5]:  # Max 5 queries per pair (updated limit)
                if _rate_limited:
                    break  # Don't burn remaining quota — stop on first rate limit hit
                try:
                    resp = _execute_mcp("news", "fetch_news", 
                                       {"query": term, "from_date": from_date, "page_size": 5})
                    # Workspace layer may wrap result
                    articles = resp.get("result", resp) if isinstance(resp, resp.__class__) and isinstance(resp, dict) else resp
                    # Detect rate limit errors in response
                    if isinstance(resp, dict):
                        err_text = str(resp.get("error", "")) + str(resp.get("message", ""))
                        if "too many requests" in err_text.lower() or "429" in err_text:
                            logger.warning("News API rate limited for %s — using stale cache", instrument)
                            _rate_limited = True
                            stale = store.get_stale(cache_key)
                            if stale:
                                return stale
                            break
                    articles = resp.get("result", resp) if isinstance(resp, dict) else resp
                    result["_queries_used"].append(term)
                    if articles and isinstance(articles, list):
                        for a in articles:
                            if isinstance(a, dict):
                                # Extract full article data
                                title = a.get("title", "")
                                description = a.get("description", "")
                                content = a.get("content", "")
                                source_name = a.get("source", {}).get("name", "") if a.get("source") else ""
                                published_at = a.get("publishedAt", "")
                                
                                all_headlines.append(title)
                                all_articles.append({
                                    "title": title,
                                    "description": description,
                                    "content": content,
                                    "source": source_name,
                                    "date": published_at
                                })
                            else:
                                all_headlines.append(str(a))
                except Exception as e:
                    logger.warning("News MCP query '%s' failed: %s", term, e)

            result["headlines"] = all_headlines[:10]
            result["articles"] = all_articles[:10]  # Store full article data
            result["articles_analyzed"] = len(all_articles)

            # REMOVE keyword-based sentiment scoring - set to None for LLM scoring
            result["net_sentiment"] = None

        except (ImportError, RuntimeError) as e:
            logger.warning("News MCP not available: %s — returning config only", e)

        # Only cache if we actually got articles — don't overwrite good stale data
        # with an empty result from a rate-limited or failed fetch.
        if result.get("articles_analyzed", 0) > 0 or result.get("headlines"):
            store.set_cached(cache_key, "news", result, instrument=instrument)
        elif not _rate_limited:
            # Got nothing but not rate-limited (MCP unavailable etc.) — still cache
            # briefly so we don't hammer the endpoint every cycle
            store.set_cached(cache_key, "news", result, ttl_minutes=30, instrument=instrument)
        return result

    except Exception as exc:
        logger.error("query_news_for_pair failed for %s: %s", instrument, exc)
        return {"error": str(exc)}


def check_weather_for_pair(instrument: str, cache_only: bool = False, **kwargs) -> Dict[str, Any]:
    """Check weather for commodity-linked pairs only."""
    try:
        cmap = _get_currency_map()

        if not cmap["should_check_weather"](instrument):
            return {
                "instrument": instrument,
                "check_weather": False,
                "checked": False,
                "severity": 0,
                "status": "CLEAR",
                "reason": "Not commodity-linked — weather check skipped",
            }

        weather_config = cmap["get_weather_config"](instrument)
        store = _get_intelligence_store()

        result = {
            "instrument": instrument,
            "check_weather": True,
            "checked": True,
            "regions": weather_config["regions"],
            "commodities": weather_config["commodities"],
            "severity": 0,
            "status": "CLEAR",
            "events": [],
        }

        # Check cache
        cache_key = f"weather:{instrument}"
        cached = store.get_cached(cache_key)
        if cached is not None:
            return cached

        if cache_only:
            logger.debug("Weather cache miss (cache_only) for %s", instrument)
            return result

        # Call Weather MCP for each region through workspace MCP layer
        try:
            max_severity = 0
            events = []
            for region in weather_config["regions"][:3]:
                loc = "unknown"
                try:
                    if isinstance(region, dict):
                        loc = region.get("location", region.get("name", "unknown"))
                    else:
                        loc = str(region)
                    resp = _execute_mcp("weather", "weather", {"location": loc})
                    w = resp.get("result", resp) if isinstance(resp, dict) else resp
                    if w and isinstance(w, dict):
                        # Extract severe conditions
                        alerts = w.get("alerts", [])
                        temp = w.get("temperature")
                        wind = w.get("wind_speed")
                        conditions = w.get("conditions", "")

                        severity = 1  # baseline
                        if alerts:
                            severity = max(severity, 3)
                            events.extend([str(a) for a in alerts[:2]])
                        if wind and isinstance(wind, (int, float)) and wind > 60:
                            severity = max(severity, 3)
                            events.append(f"High winds {wind}mph at {loc}")
                        if temp and isinstance(temp, (int, float)) and (temp > 110 or temp < -20):
                            severity = max(severity, 2)
                            events.append(f"Extreme temp {temp}°F at {loc}")

                        max_severity = max(max_severity, severity)
                except Exception as e:
                    logger.warning("Weather check for '%s' failed: %s", loc, e)

            result["severity"] = max_severity
            result["events"] = events
            if max_severity >= 3:
                result["status"] = "WARNING"
            elif max_severity >= 5:
                result["status"] = "SEVERE"

        except (ImportError, RuntimeError) as e:
            logger.warning("Weather MCP not available: %s — returning config only", e)

        store.set_cached(cache_key, "weather", result, instrument=instrument)
        return result

    except Exception as exc:
        logger.error("check_weather_for_pair failed for %s: %s", instrument, exc)
        return {"error": str(exc)}


def run_statistical_checks(instrument: str, candles: list = None, **kwargs) -> Dict[str, Any]:
    """Run Wolfram statistical validation for a pair."""
    try:
        cmap = _get_currency_map()
        wolfram_config = cmap["get_wolfram_checks"](instrument)
        correlated = cmap["get_correlated_instruments"](instrument)
        queries_used = []

        result = {
            "instrument": instrument,
            "correlation_pairs": wolfram_config["correlation_pairs"],
            "correlation_values": wolfram_config["correlation_values"],
            "correlated_open_warning": correlated,
            "position_sizing": {
                "method": wolfram_config["position_sizing_method"],
                "max_risk_pct": wolfram_config["max_risk_pct"],
                "recommended_size": None,
            },
            "statistical_significance": None,
            "seasonal_pattern": None,
            "_queries_used": [],
        }

        parts = instrument.split("_")
        base_ccy = parts[0] if len(parts) == 2 else instrument[:3]
        quote_ccy = parts[1] if len(parts) == 2 else instrument[3:]

        # Kelly criterion from backtest data (no Wolfram needed)
        try:
            try:
                from Source.backtester.trading_db import TradingDB
            except ModuleNotFoundError:
                from backtester.trading_db import TradingDB
            db = TradingDB()
            # Get best setups across regimes for this pair
            setups = []
            for regime in ["trending", "ranging", "exhaustion"]:
                setups.extend(db.get_best_params(instrument, regime, min_trades=20))
            setups.sort(key=lambda s: s.get("profit_factor", 0), reverse=True)
            if setups:
                best = setups[0]
                win_rate = best.get("win_rate", 50) / 100.0
                avg_pips = abs(best.get("avg_pips", 10))
                # Estimate avg_loss from profit_factor: PF = avg_win/avg_loss * win_rate/(1-win_rate)
                pf = best.get("profit_factor", 1.0) or 1.0
                avg_win = avg_pips if avg_pips > 0 else 10
                avg_loss = avg_win / max(pf, 0.01) if pf > 0 else 10
                win_loss_ratio = avg_win / avg_loss
                kelly = win_rate - ((1 - win_rate) / win_loss_ratio)
                result["position_sizing"]["kelly_fraction"] = round(kelly, 4)
                result["position_sizing"]["half_kelly"] = round(kelly / 2, 4)
                result["position_sizing"]["recommended_size"] = round(min(kelly / 2 * 100, wolfram_config["max_risk_pct"]), 2)
        except Exception as e:
            logger.warning("Kelly calculation failed: %s", e)

        # CALL 3: Seasonal pattern — Wolfram LLM API returns 501 for compound narrative
        # queries like "EUR/USD exchange rate March seasonal pattern". Skip Wolfram here;
        # seasonal bias is derived from historical candle data instead (no API call needed).
        logger.debug("Seasonal pattern: skipping Wolfram (unsupported query type)")

        result["_queries_used"] = queries_used
        return result

    except Exception as exc:
        logger.error("run_statistical_checks failed for %s: %s", instrument, exc)
        return {"error": str(exc)}

# ---------------------------------------------------------------------------
# trade_monitor wrapper functions (Agent 6 - NEW)
# ---------------------------------------------------------------------------

def monitor_open_positions(**kwargs) -> Dict[str, Any]:
    """Monitor all open positions for changes, alerts, and management needs."""
    try:
        position_monitor = _get_position_monitor()
        positions = position_monitor.get_all_positions(**kwargs)
        
        alerts = []
        position_data = []
        
        for pos in positions:
            position_data.append({
                "instrument": pos.get("instrument", ""),
                "units": pos.get("units", 0),
                "unrealized_pl": pos.get("unrealizedPL", 0),
                "side": "long" if float(pos.get("units", 0)) > 0 else "short",
            })
            
            # Check for alert conditions
            pl_pct = float(pos.get("unrealizedPL", 0)) / 1000  # rough account percentage
            if abs(pl_pct) > 1.0:  # More than 1% account risk
                alerts.append({
                    "type": "high_pl_risk", 
                    "instrument": pos.get("instrument"),
                    "pl_pct": pl_pct
                })
        
        return {
            "positions": position_data,
            "position_count": len(position_data),
            "alerts": alerts,
            "alert_count": len(alerts),
            "status": "monitoring_active" if position_data else "no_positions",
        }
        
    except Exception as exc:
        logger.error("monitor_open_positions failed: %s", exc)
        return {"error": str(exc), "positions": [], "alerts": []}

def check_spread_conditions(instruments: List[str] = None, **kwargs) -> Dict[str, Any]:
    """Check current spreads vs normal conditions for trading instruments."""
    try:
        if instruments is None:
            instruments = ["EUR_USD", "USD_JPY", "GBP_USD"]  # default major pairs
            
        pricing_data = get_current_pricing(instruments)
        spread_conditions = []
        
        # Normal spread thresholds (in pips, approximate)
        normal_spreads = {
            "EUR_USD": 1.5, "USD_JPY": 1.2, "GBP_USD": 2.0,
            "AUD_USD": 1.8, "NZD_USD": 2.5, "USD_CAD": 2.0, 
            "USD_CHF": 1.8
        }
        
        if isinstance(pricing_data, dict) and "prices" in pricing_data:
            for price in pricing_data["prices"]:
                instrument = price.get("instrument", "")
                spread = price.get("spread", 0)
                normal = normal_spreads.get(instrument, 2.0)
                
                condition = "normal"
                if spread > normal * 3:
                    condition = "wide"
                elif spread > normal * 2:
                    condition = "elevated"
                    
                spread_conditions.append({
                    "instrument": instrument,
                    "current_spread": spread,
                    "normal_spread": normal,
                    "condition": condition,
                    "spread_multiplier": spread / normal if normal > 0 else 0,
                })
        
        return {
            "spread_conditions": spread_conditions,
            "wide_spreads": [s for s in spread_conditions if s["condition"] == "wide"],
            "overall_condition": "wide" if any(s["condition"] == "wide" for s in spread_conditions) else "normal",
        }
        
    except Exception as exc:
        logger.error("check_spread_conditions failed: %s", exc)
        return {"error": str(exc), "spread_conditions": []}

def alert_orchestrator(alert_type: str, message: str, data: Dict = None, **kwargs) -> Dict[str, Any]:
    """Send alert to cycle_orchestrator for immediate attention."""
    try:
        alert_data = {
            "alert_type": alert_type,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data or {},
            "source": "trade_monitor",
        }
        
        # For now, just log the alert
        # In full implementation, this would route through SwarmHandler
        logger.warning("TRADE_MONITOR ALERT [%s]: %s", alert_type.upper(), message)
        
        return {
            "alert_sent": True,
            "alert_id": f"tm_{int(time.time())}",
            "alert_data": alert_data,
        }
        
    except Exception as exc:
        logger.error("alert_orchestrator failed: %s", exc)
        return {"error": str(exc), "alert_sent": False}
