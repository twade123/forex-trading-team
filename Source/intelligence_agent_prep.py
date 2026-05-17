#!/usr/bin/env python3
"""Intelligence Agent Briefing System — 3x/day proactive data gathering + AI synthesis.

Gathers Wolfram macro data, news, weather, and statistics for all trading pairs,
then uses Haiku to synthesize a proper analyst-quality briefing per pair.

Architecture:
  - Called by intelligence_scheduler (background thread in trading_api_routes.py)
  - Runs 3x/day: morning (6 AM ET), midday (12 PM ET), evening (5 PM ET)
  - Also callable standalone: python intelligence_agent_prep.py --all
  - Cache stored in intelligence_cache table (intelligence.db)
  - Trading cycle reads cache_only=True — gets pre-built AI briefing instantly

Usage:
    python intelligence_agent_prep.py --session asia
    python intelligence_agent_prep.py --session london
    python intelligence_agent_prep.py --session ny
    python intelligence_agent_prep.py --all
    python intelligence_agent_prep.py --pairs EUR_USD GBP_USD
"""

import sys
import os
import argparse
import logging
import time
import json
from datetime import datetime, timezone
from typing import List, Dict, Any
from pathlib import Path

# Add paths for imports — computed from __file__ so direct runs and scheduler both work
_source_dir = os.path.dirname(os.path.abspath(__file__))   # .../Forex Trading Team/Source
_team_dir   = os.path.dirname(_source_dir)                 # .../Forex Trading Team
jarvis_dir  = os.path.dirname(_team_dir)                   # .../Jarvis
for _p in [_source_dir, _team_dir, jarvis_dir]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# All 13 trading pairs
ALL_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "NZD_USD", "USD_CAD",
    "USD_CHF", "EUR_GBP", "EUR_JPY", "GBP_JPY", "AUD_JPY", "EUR_AUD", "EUR_CHF"
]

SESSION_PAIRS = {
    "asia":   ["AUD_USD", "NZD_USD", "AUD_JPY", "USD_JPY", "EUR_JPY", "GBP_JPY"],
    "london": ["EUR_USD", "GBP_USD", "USD_CHF", "EUR_GBP", "EUR_AUD", "EUR_CHF"],
    "ny":     ["USD_CAD", "EUR_USD", "GBP_USD", "USD_JPY"],
    "all":    ALL_PAIRS,
}

# Session quality metadata — best liquidity windows, spread conditions, low-liquidity warnings
_SESSION_QUALITY = {
    "EUR_USD": {
        "best_session": "London/NY overlap (13:00-17:00 UTC)",
        "spread_conditions": "Tightest spreads at London open (08:00 UTC); widens in Asia",
        "avoid_windows": "22:00-00:00 UTC (end-of-day/pre-Asia); major news blackouts",
    },
    "GBP_USD": {
        "best_session": "London session (08:00-16:00 UTC)",
        "spread_conditions": "Volatile at London open, tightens mid-session; spreads widen sharply in Asia",
        "avoid_windows": "22:00-07:00 UTC (Asia session); pre-BOE/Fed announcement windows",
    },
    "USD_JPY": {
        "best_session": "Tokyo session (00:00-06:00 UTC) and London/NY overlap",
        "spread_conditions": "Good liquidity across all sessions; tightest at Tokyo open",
        "avoid_windows": "Pre-Tokyo (20:00-22:00 UTC) — thin and gap-prone",
    },
    "AUD_USD": {
        "best_session": "Sydney/Tokyo session (22:00-06:00 UTC)",
        "spread_conditions": "Best during Asia; widens considerably in NY afternoon",
        "avoid_windows": "NY afternoon (19:00-22:00 UTC); pre-RBA events",
    },
    "NZD_USD": {
        "best_session": "Sydney/Tokyo session (22:00-06:00 UTC)",
        "spread_conditions": "Thinner than AUD/USD; tightest in Asia; significant spread widening in European close",
        "avoid_windows": "London close to NY close (15:00-22:00 UTC)",
    },
    "USD_CAD": {
        "best_session": "NY session (13:00-21:00 UTC)",
        "spread_conditions": "Best liquidity when NY and Toronto both active; widens in Asia",
        "avoid_windows": "22:00-12:00 UTC (pre-NY); avoid around US/Canada data releases",
    },
    "USD_CHF": {
        "best_session": "London/NY overlap (13:00-17:00 UTC)",
        "spread_conditions": "Reasonable London hours; thin in Asia — SNB intervention risk any session",
        "avoid_windows": "22:00-07:00 UTC (Asia); SNB events cause gap risk",
    },
    "EUR_GBP": {
        "best_session": "London session (08:00-17:00 UTC)",
        "spread_conditions": "Best during European hours only; very thin outside Frankfurt/London",
        "avoid_windows": "NY afternoon through Asia (17:00-08:00 UTC)",
    },
    "EUR_JPY": {
        "best_session": "Tokyo/London overlap (07:00-09:00 UTC)",
        "spread_conditions": "Good during Tokyo and London; thinner in NY afternoon",
        "avoid_windows": "NY close to Tokyo open (21:00-00:00 UTC)",
    },
    "GBP_JPY": {
        "best_session": "London session (08:00-16:00 UTC)",
        "spread_conditions": "High-volatility pair — spreads spike on news; best mid-London session",
        "avoid_windows": "Asia session (22:00-07:00 UTC) — very thin with large spreads",
    },
    "AUD_JPY": {
        "best_session": "Tokyo/Sydney session (00:00-06:00 UTC)",
        "spread_conditions": "Best during Asia overlap; thin in European afternoon",
        "avoid_windows": "London close through NY close (15:00-22:00 UTC)",
    },
    "EUR_AUD": {
        "best_session": "Sydney/London transition (06:00-10:00 UTC)",
        "spread_conditions": "Moderate liquidity; tightest during Asia-to-London handoff",
        "avoid_windows": "NY session (13:00-22:00 UTC) — one of the thinner crosses",
    },
    "EUR_CHF": {
        "best_session": "London/Frankfurt open (07:00-12:00 UTC)",
        "spread_conditions": "Thin pair; gap risk on SNB news; only trade during European hours",
        "avoid_windows": "All non-European hours — Asia and NY afternoon especially",
    },
}


# ── Expanded Wolfram macro query dictionaries (~47 queries) ──────────────────
# These run in _fetch_wolfram_macro_expanded() via the existing _wolfram_cached helper.
# Cache key pattern: wolfram_expanded:{key} | source: wolfram_macro | TTL: 24h
#
# QUERY FORMAT RULES (Wolfram LLM API):
#   ✓ Short, specific, data-oriented: "US GDP growth rate"
#   ✗ Compound narrative: "United States real GDP growth rate latest quarter annualized"
#   ✗ Contextual phrases: "seasonal pattern", "year-over-year", "latest month"
#   Wolfram resolves "latest" implicitly — no need to specify recency qualifiers.

WOLFRAM_GDP_QUERIES = {
    "gdp_us":          "US GDP growth rate",
    "gdp_eurozone":    "Eurozone GDP growth rate",
    "gdp_uk":          "UK GDP growth rate",
    "gdp_japan":       "Japan GDP growth rate",
    "gdp_australia":   "Australia GDP growth rate",
    "gdp_nz":          "New Zealand GDP growth rate",
    "gdp_canada":      "Canada GDP growth rate",
    "gdp_switzerland": "Switzerland GDP growth rate",
}

WOLFRAM_TRADE_BALANCE_QUERIES = {
    "trade_us":          "US trade balance",
    "trade_eurozone":    "Eurozone trade balance",
    "trade_uk":          "UK trade balance",
    "trade_japan":       "Japan trade balance",
    "trade_australia":   "Australia trade balance",
    "trade_nz":          "New Zealand trade balance",
    "trade_canada":      "Canada trade balance",
    "trade_switzerland": "Switzerland trade balance",
}

WOLFRAM_PMI_QUERIES = {
    "pmi_mfg_us":        "US ISM Manufacturing PMI",
    "pmi_svc_us":        "US ISM Services PMI",
    "pmi_mfg_eurozone":  "Eurozone Manufacturing PMI",
    "pmi_svc_eurozone":  "Eurozone Services PMI",
    "pmi_mfg_uk":        "UK Manufacturing PMI",
    "pmi_svc_uk":        "UK Services PMI",
    "pmi_mfg_japan":     "Japan Manufacturing PMI",
    "pmi_svc_japan":     "Japan Services PMI",
    "pmi_mfg_australia": "Australia Manufacturing PMI",
    "pmi_mfg_canada":    "Canada Ivey PMI",
}

WOLFRAM_RETAIL_QUERIES = {
    "retail_us":        "US retail sales",
    "retail_uk":        "UK retail sales",
    "retail_eurozone":  "Eurozone retail sales",
    "retail_australia": "Australia retail sales",
    "retail_canada":    "Canada retail sales",
    "retail_japan":     "Japan retail sales",
}

WOLFRAM_CONFIDENCE_QUERIES = {
    "conf_us":        "US consumer confidence index",
    "conf_eurozone":  "Eurozone consumer confidence",
    "conf_uk":        "UK consumer confidence",
    "conf_japan":     "Japan consumer confidence",
    "conf_australia": "Australia consumer confidence",
}

WOLFRAM_HOUSING_QUERIES = {
    "housing_us_starts":     "US housing starts",
    "housing_us_exist":      "US existing home sales",
    "housing_us_permits":    "US building permits",
    "housing_uk_prices":     "UK house prices",
    "housing_aus_approvals": "Australia building approvals",
    "housing_can_starts":    "Canada housing starts",
}

WOLFRAM_WAGE_QUERIES = {
    "wages_us_eci":    "US Employment Cost Index",
    "wages_us_ahw":    "US average hourly earnings",
    "wages_uk":        "UK average weekly earnings",
    "wages_eurozone":  "Eurozone wage growth",
    "wages_australia": "Australia wage price index",
    "wages_canada":    "Canada average hourly wages",
}

# Combined — all 47 expanded queries (run by _fetch_wolfram_macro_expanded)
WOLFRAM_EXPANDED_ALL: dict = {}
WOLFRAM_EXPANDED_ALL.update(WOLFRAM_GDP_QUERIES)
WOLFRAM_EXPANDED_ALL.update(WOLFRAM_TRADE_BALANCE_QUERIES)
WOLFRAM_EXPANDED_ALL.update(WOLFRAM_PMI_QUERIES)
WOLFRAM_EXPANDED_ALL.update(WOLFRAM_RETAIL_QUERIES)
WOLFRAM_EXPANDED_ALL.update(WOLFRAM_CONFIDENCE_QUERIES)
WOLFRAM_EXPANDED_ALL.update(WOLFRAM_HOUSING_QUERIES)
WOLFRAM_EXPANDED_ALL.update(WOLFRAM_WAGE_QUERIES)


def _fetch_wolfram_macro_expanded() -> dict:
    """
    Run all 47 expanded Wolfram macro queries and return results dict.
    Uses the existing _wolfram_cached() helper with 24h TTL.
    Returns: {key: text_result} — None for failed queries.
    """
    try:
        from agents.wrappers import _wolfram_cached
    except ImportError:
        from Source.agents.wrappers import _wolfram_cached

    results = {}
    for key, query in WOLFRAM_EXPANDED_ALL.items():
        try:
            r = _wolfram_cached(query, f"wolfram_expanded:{key}", "wolfram_macro", "global")
            results[key] = r["text"][:500] if r.get("success") else None
        except Exception as e:
            logger.debug(f"Wolfram expanded [{key}] error: {e}")
            results[key] = None

    success_count = sum(1 for v in results.values() if v is not None)
    logger.info(f"Wolfram expanded macro: {success_count}/{len(results)} queries succeeded")
    return results


def _get_anthropic_client():
    """Get Anthropic client with API key."""
    from anthropic import Anthropic
    key_path = Path(jarvis_dir) / "API" / "CLAUDE_API_KEY.txt"
    api_key = key_path.read_text().strip() if key_path.exists() else os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("No Anthropic API key found")
    return Anthropic(api_key=api_key)


def _get_local_client():
    """Get OpenAI-compatible client for local MLX 35B agent (port 11502 — agent fleet)."""
    from openai import OpenAI
    return OpenAI(base_url="http://localhost:11503/v1", api_key="mlx-local")  # serving gateway → MLX 35B


def _synthesize_briefing(pair: str, raw_data: Dict[str, Any]) -> str:
    """Use local 35B (CSO) to synthesize raw intelligence data into an analyst-quality briefing.
    
    Gate: if no real Wolfram macro data exists, returns None — no hallucinated output.
    No Haiku fallback — we either have real data to synthesize or we don't.
    """
    macro = raw_data.get("macro", {})
    news = raw_data.get("news", {})
    weather = raw_data.get("weather", {})
    stats = raw_data.get("statistics", {})

    base_ccy, quote_ccy = pair.split("_")

    # Build data payload for the LLM
    data_text = f"Pair: {pair} ({base_ccy}/{quote_ccy})\n\n"
    
    # Macro
    data_text += "MACRO DATA:\n"
    if macro.get("base_currency_rate") is not None:
        data_text += f"- {base_ccy} rate: {macro['base_currency_rate']}%\n"
    if macro.get("quote_currency_rate") is not None:
        data_text += f"- {quote_ccy} rate: {macro['quote_currency_rate']}%\n"
    if macro.get("rate_differential") is not None:
        data_text += f"- Rate differential: {macro['rate_differential']}%\n"
    if macro.get("pair_current_price"):
        data_text += f"- Current price: {macro['pair_current_price']}\n"
    if macro.get("pair_1yr_min"):
        data_text += f"- 1yr range: {macro['pair_1yr_min']} - {macro['pair_1yr_max']}\n"
        data_text += f"- Range position: {macro.get('pair_range_position', 'unknown')}\n"
    if macro.get("oil_price"):
        data_text += f"- Oil: ${macro['oil_price']}/bbl\n"
    
    # Deep macro (inflation, GDP, unemployment, trade balance, commodities, bonds)
    deep = macro.get("deep_macro", {})
    if deep:
        data_text += "\nECONOMIC INDICATORS:\n"
        label_map = {
            "inflation": "Inflation", "gdp_growth": "GDP Growth",
            "unemployment": "Unemployment", "trade_balance": "Trade Balance",
            "bond_yield": "10yr Bond Yield",
        }
        for prefix, ccy_label in [("base", base_ccy), ("quote", quote_ccy)]:
            ccy_data = []
            for suffix, label in label_map.items():
                key = f"{prefix}_{suffix}"
                if key in deep:
                    ccy_data.append(f"  - {label}: {deep[key][:200]}")
            if ccy_data:
                data_text += f"\n{ccy_label} Economy:\n" + "\n".join(ccy_data) + "\n"
        
        # Commodities
        commodity_lines = []
        for k, v in deep.items():
            if "commodity" in k:
                commodity_lines.append(f"  - {v[:200]}")
        if commodity_lines:
            data_text += "\nCOMMODITIES:\n" + "\n".join(commodity_lines) + "\n"
    
    # News
    articles = news.get("articles", []) if isinstance(news, dict) else []
    if articles:
        data_text += f"\nNEWS ({len(articles)} articles):\n"
        for i, art in enumerate(articles[:8], 1):
            if isinstance(art, dict):
                data_text += f"  {i}. [{art.get('source', '?')}] {art.get('title', '?')}\n"
                if art.get("summary"):
                    data_text += f"     {art['summary'][:200]}\n"
    else:
        data_text += "\nNEWS: No recent articles found.\n"
    
    # Weather
    if isinstance(weather, dict) and weather.get("check_weather"):
        data_text += f"\nWEATHER: Severity {weather.get('severity', 0)}/10 — {weather.get('status', 'unknown')}\n"
    
    # Statistics
    if isinstance(stats, dict):
        if stats.get("seasonal_pattern"):
            data_text += f"\nSEASONAL: {stats['seasonal_pattern'][:300]}\n"
        if stats.get("position_sizing", {}).get("kelly_fraction"):
            data_text += f"KELLY: {stats['position_sizing']['kelly_fraction']:.3f} (half-Kelly: {stats['position_sizing'].get('half_kelly', 0):.3f})\n"
        corr = stats.get("correlation_pairs", [])
        corr_vals = stats.get("correlation_values", {})
        if corr:
            corr_parts = []
            for c in corr[:6]:
                r = corr_vals.get(c)
                corr_parts.append(f"{c} (r={r:.2f})" if r is not None else str(c))
            data_text += f"CORRELATIONS: {', '.join(corr_parts)}\n"

    # Session quality
    sq = _SESSION_QUALITY.get(pair, {})
    if sq:
        data_text += (
            f"\nSESSION QUALITY:\n"
            f"- Best liquidity: {sq['best_session']}\n"
            f"- Spread conditions: {sq['spread_conditions']}\n"
            f"- Low-liquidity windows to avoid: {sq['avoid_windows']}\n"
        )

    synthesis_prompt = (
        "/no_think\n"  # Disable Qwen3 extended thinking — we need output, not rumination
        "You are a senior forex macro analyst at a hedge fund. Synthesize ALL the provided data into a comprehensive "
        "intelligence briefing for a forex trader. You have access to interest rates, inflation, GDP, unemployment, "
        "trade balance, bond yields, commodity prices, news, weather, and seasonal patterns.\n\n"
        "Structure your briefing:\n"
        "1. **MACRO PICTURE**: Rate differential + inflation trajectory + GDP momentum + employment. Which economy is stronger?\n"
        "2. **BOND SPREAD**: Yield differential direction — widening or narrowing? This drives capital flows.\n"
        "3. **COMMODITIES**: Any commodity moves that directly affect this pair (oil→CAD, iron ore→AUD, gold→CHF/AUD, energy→JPY)\n"
        "4. **RANGE & VALUE**: Where is price in its 1yr range? Cheap, expensive, or fair value?\n"
        "5. **NEWS CATALYSTS**: Anything that could move this pair in the next 24h\n"
        "6. **RISK FACTORS**: Correlations with r-values (e.g. GBP/USD r=0.87 means near-perfect co-movement), seasonality, weather, upcoming data releases\n"
        "7. **SESSION QUALITY**: Best session to trade this pair (liquidity, spreads), any low-liquidity windows to avoid, expected spread conditions today\n\n"
        "End with:\n"
        "**BIAS: BULLISH/BEARISH/NEUTRAL (HIGH/MEDIUM/LOW confidence)**\n"
        "**KEY LEVEL**: [significant price level to watch]\n"
        "**CATALYST**: [the one thing most likely to move this pair next]\n\n"
        "Be direct. No filler. Every sentence should be actionable intelligence."
    )

    import re as _re

    # Use MLX 35B agent (port 11502) — single shared agent fleet, free, always running, no paid API fallback
    result_text = ""
    try:
        client = _get_local_client()
        response = client.chat.completions.create(
            model="mlx-community/Qwen3.5-35B-A3B-4bit",
            messages=[
                {"role": "system", "content": synthesis_prompt},
                {"role": "user", "content": data_text},
            ],
            max_tokens=4000,
            temperature=0.3,
            extra_headers={"X-Jarvis-Tenant": "trading"},
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        result_text = response.choices[0].message.content or ""
        # Strip Qwen3 thinking tags (closed or unclosed)
        result_text = _re.sub(r'<think>.*?</think>\s*', '', result_text, flags=_re.DOTALL)
        if '<think>' in result_text:
            result_text = result_text.split('<think>')[0]
        result_text = result_text.strip()
    except Exception as local_err:
        logger.error(f"[{pair}] MLX 35B agent (port 11502) failed: {local_err} — NO paid API fallback")

    if result_text and len(result_text) > 50:
        logger.info(f"[{pair}] Briefing synthesized by MLX 35B agent ({len(result_text)} chars)")
        return result_text

    logger.warning(f"[{pair}] MLX 35B agent returned empty — no briefing stored")
    return None


def gather_and_synthesize(pair: str) -> Dict[str, Any]:
    """Gather all intelligence data for a pair, then synthesize with AI."""
    start = time.time()
    result = {
        "pair": pair, "cached_components": [], "errors": [],
        "wolfram_calls": 0, "news_calls": 0, "weather_calls": 0,
        "total_time": 0, "briefing": "",
    }
    
    try:
        from agents.wrappers import (
            _wolfram_cached, _get_rate_queries, _get_exchange_query,
            query_news_for_pair, check_weather_for_pair, run_statistical_checks,
            _extract_rate_from_wolfram,
        )
    except ImportError:
        from Source.agents.wrappers import (
            _wolfram_cached, _get_rate_queries, _get_exchange_query,
            query_news_for_pair, check_weather_for_pair, run_statistical_checks,
            _extract_rate_from_wolfram,
        )

    base_ccy, quote_ccy = pair.split("_")
    macro = {"base_currency": base_ccy, "quote_currency": quote_ccy}
    news_data = {}
    weather_data = {}
    stats_data = {}

    # 1. Economic data — DUAL SOURCE: free APIs (fast/reliable) + Wolfram LLM (rich narrative)
    #
    # economic_data_fetcher: FRED + Yahoo Finance + OANDA
    #   → interest rates (all currencies), FX 1yr range, commodity prices (quantitative)
    # Wolfram LLM API: rich narrative data with context, history, charts
    #   → US macro (fed funds, treasury curve), commodities (gold/oil with trend)
    #   → Wolfram excels at these; fails on non-USD central bank rates → fall back to FRED

    # 1a. Free APIs (always runs — fast, no quota concerns)
    try:
        try:
            from economic_data_fetcher import fetch_all_for_pair as _fetch_econ
        except ImportError:
            from Source.economic_data_fetcher import fetch_all_for_pair as _fetch_econ

        econ = _fetch_econ(pair)
        for k, v in econ.items():
            if k not in ("data_source", "fetched_at"):
                macro[k] = v
        # Map commodity keys to what _synthesize_macro_briefing looks for
        if "wti_oil_price" in macro and "oil_price" not in macro:
            macro["oil_price"] = macro["wti_oil_price"]
        if "brent_oil_price" in macro and "oil_price" not in macro:
            macro["oil_price"] = macro["brent_oil_price"]
        result["cached_components"].append("macro_free_apis")
        logger.info(f"  [{pair}] Free API data: rates={macro.get('base_currency_rate')}/{macro.get('quote_currency_rate')}, price={macro.get('pair_current_price')}")
    except Exception as e:
        result["errors"].append(f"Free API fetch: {e}")
        logger.warning(f"  [{pair}] Free API fetch failed: {e}")

    # 1b. Wolfram LLM API — rich narrative for what it handles well
    # These queries are confirmed working with the current AppID.
    # Wolfram fails on non-USD central bank rates → we already have those from FRED above.
    _WOLFRAM_DEEP_QUERIES = {
        # US macro — excellent Wolfram coverage
        "base_fed_funds":    ("US federal funds rate",         "USD"),
        "base_treasury":     ("US 10 year treasury yield",     "USD"),
        # Commodities — "current ... spot price" forces real-time data via LLM API
        "commodity_gold":    ("current gold spot price",                 None),
        "commodity_oil":     ("current WTI crude oil spot price",        None),
    }
    # CCY-specific commodity adds
    _CCY_WOLFRAM_EXTRAS = {
        "AUD": {"commodity_iron":   ("current iron ore spot price",  None)},
        "CAD": {"commodity_natgas": ("current natural gas price",    None)},
        "NZD": {"commodity_dairy":  ("New Zealand dairy price index", None)},
        "GBP": {"base_uk_cpi":      ("United Kingdom CPI 2025",      "GBP")},
    }
    for ccy in [base_ccy, quote_ccy]:
        extras = _CCY_WOLFRAM_EXTRAS.get(ccy, {})
        _WOLFRAM_DEEP_QUERIES.update(extras)

    deep_macro = {}
    try:
        for key, (query, ccy_filter) in _WOLFRAM_DEEP_QUERIES.items():
            # Skip if ccy_filter set and neither pair currency matches
            if ccy_filter and ccy_filter not in (base_ccy, quote_ccy):
                continue
            try:
                r = _wolfram_cached(query, f"wolfram:deep:{key}", "wolfram_deep", pair)
                if not r.get("from_cache", False):
                    result["wolfram_calls"] += 1
                if r.get("success"):
                    deep_macro[key] = r["text"][:400]
                    logger.debug(f"  [{pair}] Wolfram {key}: OK")
                else:
                    logger.debug(f"  [{pair}] Wolfram {key}: {r.get('text','fail')[:80]}")
            except Exception as e:
                logger.debug(f"  [{pair}] Wolfram {key} error: {e}")

        # Bond yields from free APIs (FRED — more complete than Wolfram for non-USD)
        for prefix, ccy in [("base", base_ccy), ("quote", quote_ccy)]:
            bond_key = f"{ccy.lower()}_bond_yield_10yr"
            if bond_key in macro:
                deep_macro[f"{prefix}_bond_yield_10yr"] = f"{macro[bond_key]:.2f}%"

        if deep_macro:
            macro["deep_macro"] = deep_macro
            result["cached_components"].append("wolfram_deep")
            logger.info(f"  [{pair}] Wolfram deep macro: {list(deep_macro.keys())}")
    except Exception as e:
        result["errors"].append(f"Wolfram deep: {e}")
        logger.warning(f"  [{pair}] Wolfram deep failed: {e}")

    # 2. News
    try:
        news_data = query_news_for_pair(pair)
        if "error" not in news_data:
            result["cached_components"].append("news")
            result["news_calls"] = len(news_data.get("_queries_used", []))
    except Exception as e:
        result["errors"].append(f"News: {e}")
    
    # 3. Weather
    try:
        weather_data = check_weather_for_pair(pair)
        if "error" not in weather_data:
            result["cached_components"].append("weather")
    except Exception as e:
        result["errors"].append(f"Weather: {e}")
    
    # 4. Statistics (seasonal + kelly)
    try:
        stats_data = run_statistical_checks(pair)
        if "error" not in stats_data:
            result["cached_components"].append("statistics")
            result["wolfram_calls"] += len(stats_data.get("_queries_used", []))
    except Exception as e:
        result["errors"].append(f"Stats: {e}")
    
    # 5. AI Synthesis
    raw = {"macro": macro, "news": news_data, "weather": weather_data, "statistics": stats_data}
    try:
        briefing = _synthesize_briefing(pair, raw)
        if briefing:
            result["briefing"] = briefing
            result["cached_components"].append("ai_briefing")
        else:
            logger.info(f"  [{pair}] AI briefing skipped — no real macro data to synthesize")
    except Exception as e:
        result["errors"].append(f"AI synthesis: {e}")
        briefing = None

    # 6. Cache writes — bridge keys are independent of AI synthesis success
    try:
        from intelligence_store import IntelligenceStore
        store = IntelligenceStore()

        # AI briefing (only if synthesis succeeded)
        if briefing:
            store.set_cached(
                f"briefing:ai:{pair}", "ai_briefing",
                {"briefing": briefing, "macro": macro, "generated_at": datetime.now(timezone.utc).isoformat()},
                instrument=pair, query_used="ai_synthesis"
            )

        # ── Bridge keys: write regardless of AI synthesis ──
        # gather_intelligence() in wrappers.py looks for wolfram:rate:{CCY} and
        # wolfram:fx:{PAIR} under category wolfram_macro with cache_only=True.
        parts = pair.split("_")
        _base = parts[0] if len(parts) == 2 else pair[:3]
        _quote = parts[1] if len(parts) == 2 else pair[3:]

        # Interest rates → wolfram:rate:{CCY}
        # Store as plain text — _extract_rate_from_wolfram() parses with regex
        for _ccy, _rate_key in [(_base, "base_rate"), (_quote, "quote_rate")]:
            _rate_val = macro.get(f"{'base' if _ccy == _base else 'quote'}_currency_rate")
            if _rate_val is not None:
                store.set_cached(
                    f"wolfram:rate:{_ccy}", "wolfram_macro",
                    f"{_ccy} interest rate: {_rate_val}%",
                    instrument=pair, query_used=f"bridge_from_deep:{pair}"
                )

        # FX range → wolfram:fx:{PAIR}
        # Store as Wolfram-formatted text — _extract_fx_range() parses with regex
        if macro.get("pair_current_price"):
            store.set_cached(
                f"wolfram:fx:{pair}", "wolfram_macro",
                (f"Result: {macro.get('pair_current_price')}\n"
                 f"1-year minimum | {macro.get('pair_1yr_min', '?')}\n"
                 f"1-year maximum | {macro.get('pair_1yr_max', '?')}\n"
                 f"1-year average | {macro.get('pair_1yr_avg', '?')}"),
                instrument=pair, query_used=f"bridge_from_deep:{pair}"
            )

        # News bridge → news:{PAIR}
        # query_news_for_pair() reads this key in cache_only mode
        if news_data and isinstance(news_data, dict) and "error" not in news_data:
            store.set_cached(
                f"news:{pair}", "news",
                news_data,
                instrument=pair, query_used=f"bridge_from_deep:{pair}"
            )

        logger.debug(f"  [{pair}] Bridge keys written (rate:{_base}, rate:{_quote}, fx:{pair}, news:{pair})")
        store.close()
    except Exception as e:
        logger.warning(f"  [{pair}] Failed to write cache/bridge keys: {e}")
        logger.warning(f"  [{pair}] AI synthesis: {e}")
    
    result["total_time"] = time.time() - start
    logger.info(f"  [{pair}] Done in {result['total_time']:.1f}s — {', '.join(result['cached_components'])} | {result['wolfram_calls']}W {result['news_calls']}N calls")
    return result


def run_refresh(pairs: List[str] = None, session_name: str = "all") -> Dict[str, Any]:
    """Run a full intelligence refresh for the given pairs. Returns summary dict.
    
    This is the main entry point — called by the scheduler thread and by CLI.
    """
    if pairs is None:
        pairs = ALL_PAIRS
    
    logger.info(f"=== Intelligence Refresh: {session_name} ({len(pairs)} pairs) ===")
    start = time.time()
    
    # Flush stale cache for these pairs so we get fresh data
    try:
        from intelligence_store import IntelligenceStore
        store = IntelligenceStore()
        flushed = store.flush_cache(instruments=pairs)
        store.close()
        logger.info(f"  Flushed {flushed} cached entries for {len(pairs)} pairs")
    except Exception as e:
        logger.warning(f"  Cache flush failed: {e}")
    
    results = []
    total_w = total_n = 0
    
    for pair in pairs:
        try:
            r = gather_and_synthesize(pair)
            results.append(r)
            total_w += r["wolfram_calls"]
            total_n += r["news_calls"]
        except Exception as e:
            logger.error(f"Failed on {pair}: {e}")
            results.append({"pair": pair, "errors": [str(e)], "cached_components": [], "total_time": 0})
    
    elapsed = time.time() - start
    ok = [r for r in results if r.get("cached_components")]
    fail = [r for r in results if r.get("errors") and not r.get("cached_components")]
    
    summary = {
        "session": session_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pairs_total": len(pairs),
        "pairs_ok": len(ok),
        "pairs_failed": len(fail),
        "wolfram_calls": total_w,
        "news_calls": total_n,
        "elapsed_seconds": round(elapsed, 1),
        "results": results,
    }
    
    logger.info(f"=== Refresh complete: {len(ok)}/{len(pairs)} OK, {total_w} Wolfram + {total_n} News calls, {elapsed:.1f}s ===")

    # Build and persist intelligence package(s) to intelligence_packages table
    try:
        from intelligence_package_builder import build_and_save
        windows_to_build = (
            ["asia", "london", "ny"] if session_name in ("all", "custom")
            else [session_name] if session_name in ("asia", "london", "ny")
            else []
        )
        for win in windows_to_build:
            try:
                pkg = build_and_save(win, save=True)
                logger.info(f"  Intelligence package saved: id={pkg.id} window={win} "
                            f"sources={pkg.data_sources_used} time={pkg.assembly_time_ms}ms")
                summary[f"package_id_{win}"] = pkg.id
            except Exception as e:
                logger.warning(f"  Package build failed for window={win}: {e}")
    except ImportError as e:
        logger.warning(f"  intelligence_package_builder not available: {e}")

    # Save summary to file for dashboard access
    try:
        _source_dir = os.path.dirname(os.path.abspath(__file__))
        summary_path = os.path.join(os.path.dirname(_source_dir), "dashboard", "intelligence_status.json")
        with open(summary_path, "w") as f:
            # Write a slim version (no full results) for the dashboard
            slim = {k: v for k, v in summary.items() if k != "results"}
            slim["pair_status"] = {r["pair"]: {"ok": bool(r.get("cached_components")), "components": r.get("cached_components", []), "time": r.get("total_time", 0)} for r in results}
            json.dump(slim, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write status file: {e}")
    
    return summary


def main():
    parser = argparse.ArgumentParser(description="Intelligence briefing refresh")
    parser.add_argument("--session", choices=["asia", "london", "ny", "all"])
    parser.add_argument("--pairs", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    
    if not args.session and not args.pairs:
        parser.error("Must specify --session or --pairs")
    
    pairs = args.pairs if args.pairs else SESSION_PAIRS.get(args.session, ALL_PAIRS)
    session_name = args.session or "custom"
    
    if args.dry_run:
        logger.info(f"DRY RUN — would refresh: {', '.join(pairs)}")
        return
    
    summary = run_refresh(pairs, session_name)
    
    if summary["pairs_failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
