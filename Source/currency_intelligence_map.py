"""
Currency Intelligence Map — maps each traded pair to specific news, weather,
and statistical targets so the intelligence agent knows exactly what to search.

Used by:
    - intelligence agent wrappers (gather_intelligence, query_news_for_pair, etc.)
    - Will migrate to skills/intelligence/references/currency-map.md later

13 pairs × 3 intelligence sources = targeted, currency-aware queries.
"""

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Currency metadata
# ---------------------------------------------------------------------------

CURRENCIES = {
    "EUR": {
        "name": "Euro",
        "central_bank": "ECB",
        "country": "Eurozone",
        "commodity_linked": False,
        "key_events": ["ECB rate decision", "Eurozone PMI", "Eurozone CPI", "ECB press conference", "German IFO"],
        "news_terms": ["ECB interest rate", "eurozone GDP", "euro currency", "european economy"],
        "weather_regions": [],  # Not commodity-linked
    },
    "USD": {
        "name": "US Dollar",
        "central_bank": "Federal Reserve",
        "country": "United States",
        "commodity_linked": False,
        "key_events": ["US NFP", "US CPI", "Fed rate decision", "FOMC minutes", "US GDP", "US retail sales", "US ISM PMI"],
        "news_terms": ["federal reserve rate", "US economy", "dollar currency", "US jobs report"],
        "weather_regions": [],
    },
    "GBP": {
        "name": "British Pound",
        "central_bank": "BoE",
        "country": "United Kingdom",
        "commodity_linked": False,
        "key_events": ["BoE rate decision", "UK CPI", "UK employment", "UK GDP", "UK PMI"],
        "news_terms": ["BoE interest rate", "UK economy", "british pound currency", "sterling GBP"],
        "weather_regions": [],
    },
    "JPY": {
        "name": "Japanese Yen",
        "central_bank": "BoJ",
        "country": "Japan",
        "commodity_linked": False,
        "key_events": ["BoJ rate decision", "Japan CPI", "Japan GDP", "Tankan survey", "BoJ intervention"],
        "news_terms": ["BoJ interest rate", "japan economy", "yen currency", "japanese monetary policy"],
        "weather_regions": [],
    },
    "AUD": {
        "name": "Australian Dollar",
        "central_bank": "RBA",
        "country": "Australia",
        "commodity_linked": True,
        "commodities": ["iron ore", "coal", "LNG"],
        "key_events": ["RBA rate decision", "Australia employment", "Australia CPI", "China PMI", "iron ore prices"],
        "news_terms": ["RBA interest rate", "australia economy", "australian dollar currency", "iron ore commodity"],
        "weather_regions": ["Queensland", "Western Australia", "New South Wales"],
        "weather_impacts": ["drought", "flooding", "cyclone", "bushfire"],
    },
    "NZD": {
        "name": "New Zealand Dollar",
        "central_bank": "RBNZ",
        "country": "New Zealand",
        "commodity_linked": True,
        "commodities": ["dairy", "agriculture", "forestry"],
        "key_events": ["RBNZ rate decision", "NZ GDP", "NZ CPI", "Global Dairy Trade auction"],
        "news_terms": ["RBNZ interest rate", "new zealand economy", "NZD currency", "dairy commodity"],
        "weather_regions": ["Canterbury", "Waikato", "Southland"],
        "weather_impacts": ["drought", "flooding", "frost"],
    },
    "CAD": {
        "name": "Canadian Dollar",
        "central_bank": "BoC",
        "country": "Canada",
        "commodity_linked": True,
        "commodities": ["crude oil", "natural gas", "lumber"],
        "key_events": ["BoC rate decision", "Canada employment", "Canada CPI", "crude oil inventory", "OPEC"],
        "news_terms": ["BoC interest rate", "canada economy", "canadian dollar currency", "crude oil commodity"],
        "weather_regions": ["Alberta", "Saskatchewan", "Gulf of Mexico"],
        "weather_impacts": ["extreme cold", "hurricane", "pipeline disruption"],
    },
    "CHF": {
        "name": "Swiss Franc",
        "central_bank": "SNB",
        "country": "Switzerland",
        "commodity_linked": False,
        "key_events": ["SNB rate decision", "Swiss CPI", "SNB intervention", "Swiss GDP"],
        "news_terms": ["SNB interest rate", "switzerland economy", "swiss franc currency", "CHF safe haven"],
        "weather_regions": [],
    },
}

# ---------------------------------------------------------------------------
# Pair correlation data (from backtest analysis)
# ---------------------------------------------------------------------------

PAIR_CORRELATIONS = {
    "EUR_USD": {"GBP_USD": 0.87, "USD_CHF": -0.85, "EUR_GBP": 0.35},
    "GBP_USD": {"EUR_USD": 0.87, "EUR_GBP": -0.52, "GBP_JPY": 0.78},
    "USD_JPY": {"EUR_JPY": 0.68, "GBP_JPY": 0.72, "USD_CHF": 0.45},
    "AUD_USD": {"NZD_USD": 0.92, "EUR_AUD": -0.65, "AUD_NZD": 0.30},
    "NZD_USD": {"AUD_USD": 0.92, "AUD_NZD": -0.40},
    "USD_CAD": {"AUD_USD": -0.55, "EUR_USD": -0.60},
    "USD_CHF": {"EUR_USD": -0.85, "USD_JPY": 0.45},
    "EUR_GBP": {"EUR_USD": 0.35, "GBP_USD": -0.52},
    "EUR_JPY": {"USD_JPY": 0.68, "EUR_USD": 0.55},
    "GBP_JPY": {"USD_JPY": 0.72, "GBP_USD": 0.78},
    "AUD_NZD": {"AUD_USD": 0.30, "NZD_USD": -0.40},
    "EUR_CHF": {"EUR_USD": 0.60, "USD_CHF": -0.55},
    "EUR_AUD": {"AUD_USD": -0.65, "EUR_USD": 0.50},
}

# Pairs that should NOT be open simultaneously (|correlation| > 0.80)
CORRELATED_GROUPS = [
    {"EUR_USD", "GBP_USD"},       # r=0.87
    {"AUD_USD", "NZD_USD"},       # r=0.92
    {"EUR_USD", "USD_CHF"},       # r=-0.85 (inverse)
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_pair_currencies(instrument: str) -> tuple:
    """Split 'EUR_USD' into ('EUR', 'USD')."""
    parts = instrument.split("_")
    if len(parts) != 2:
        raise ValueError(f"Invalid instrument: {instrument}")
    return parts[0], parts[1]


def get_intelligence_config(instrument: str) -> Dict[str, Any]:
    """Get full intelligence config for a pair.

    Returns dict with news, weather, wolfram configs plus correlation data.
    """
    base, quote = get_pair_currencies(instrument)
    base_info = CURRENCIES.get(base, {})
    quote_info = CURRENCIES.get(quote, {})

    return {
        "instrument": instrument,
        "base": {"currency": base, **base_info},
        "quote": {"currency": quote, **quote_info},
        "news": get_news_queries(instrument),
        "weather": get_weather_config(instrument),
        "wolfram": get_wolfram_checks(instrument),
        "correlations": PAIR_CORRELATIONS.get(instrument, {}),
        "correlated_groups": [
            g for g in CORRELATED_GROUPS if instrument in g
        ],
    }


def get_news_queries(instrument: str) -> Dict[str, Any]:
    """Get targeted news search config for a pair.

    Combines both currencies' key events and search terms.
    """
    base, quote = get_pair_currencies(instrument)
    base_info = CURRENCIES.get(base, {})
    quote_info = CURRENCIES.get(quote, {})

    # Combine search terms from both currencies (max 5 best ones)
    search_terms = []
    search_terms.extend(base_info.get("news_terms", []))
    search_terms.extend(quote_info.get("news_terms", []))
    # Add forex as one query
    search_terms.append("forex")
    # Limit to max 5 best terms
    search_terms = search_terms[:5]

    # Combine key events
    key_events = []
    key_events.extend(base_info.get("key_events", []))
    key_events.extend(quote_info.get("key_events", []))

    # Central banks
    central_banks = []
    if base_info.get("central_bank"):
        central_banks.append(base_info["central_bank"])
    if quote_info.get("central_bank"):
        central_banks.append(quote_info["central_bank"])

    return {
        "search_terms": search_terms,
        "key_events": key_events,
        "central_banks": central_banks,
        "currencies_affected": [base, quote],
    }


def get_weather_config(instrument: str) -> Dict[str, Any]:
    """Get weather check config for a pair.

    Returns check_weather=True only if at least one currency is commodity-linked.
    Includes regions and impact types to check.
    """
    base, quote = get_pair_currencies(instrument)
    base_info = CURRENCIES.get(base, {})
    quote_info = CURRENCIES.get(quote, {})

    check_weather = base_info.get("commodity_linked", False) or quote_info.get("commodity_linked", False)

    regions = []
    commodities = []
    weather_impacts = []

    for info in [base_info, quote_info]:
        if info.get("commodity_linked"):
            regions.extend(info.get("weather_regions", []))
            commodities.extend(info.get("commodities", []))
            weather_impacts.extend(info.get("weather_impacts", []))

    return {
        "check_weather": check_weather,
        "regions": regions,
        "commodities": commodities,
        "weather_impacts": weather_impacts,
        "severity_threshold": 3,  # Only report severity >= 3
    }


def get_wolfram_checks(instrument: str) -> Dict[str, Any]:
    """Get statistical checks config for a pair."""
    base, quote = get_pair_currencies(instrument)
    correlations = PAIR_CORRELATIONS.get(instrument, {})

    return {
        "correlation_pairs": list(correlations.keys()),
        "correlation_values": correlations,
        "check_seasonal_patterns": True,
        "check_volatility_regime": True,
        "position_sizing_method": "kelly_criterion",
        "max_risk_pct": 2.0,  # Max 2% account risk per trade
    }


def get_correlated_instruments(instrument: str) -> List[str]:
    """Get instruments that are highly correlated with this one.

    Used by execution agent to prevent overexposure.
    """
    result = []
    for group in CORRELATED_GROUPS:
        if instrument in group:
            result.extend(g for g in group if g != instrument)
    return result


def should_check_weather(instrument: str) -> bool:
    """Quick check if this pair needs weather analysis."""
    base, quote = get_pair_currencies(instrument)
    return (
        CURRENCIES.get(base, {}).get("commodity_linked", False)
        or CURRENCIES.get(quote, {}).get("commodity_linked", False)
    )


# ---------------------------------------------------------------------------
# All traded instruments
# ---------------------------------------------------------------------------

ALL_INSTRUMENTS = [
    "EUR_USD", "USD_JPY", "GBP_USD", "AUD_USD", "NZD_USD",
    "USD_CAD", "USD_CHF", "EUR_GBP", "EUR_JPY", "GBP_JPY",
    "AUD_NZD", "EUR_CHF", "EUR_AUD",
]
