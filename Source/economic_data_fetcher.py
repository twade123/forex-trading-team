"""
economic_data_fetcher.py — Free API replacement for Wolfram economic data.

Sources:
  - FRED (Federal Reserve Economic Data) — interest rates, bond yields, CPI, unemployment
  - Yahoo Finance — gold, oil, commodity prices, FX ranges
  - Exchange rate via OANDA API (already authenticated)

All results are cached with 4h TTL so they don't fire during every trade cycle.
"""

import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path

logger = logging.getLogger(__name__)

# ── FRED Series IDs ────────────────────────────────────────────────
_FRED_SERIES = {
    # US
    "USD_rate":         "FEDFUNDS",
    "USD_10yr":         "DGS10",
    "USD_2yr":          "DGS2",
    "USD_inflation":    "CPIAUCSL",
    "USD_unemployment": "UNRATE",
    # UK
    "GBP_10yr":         "IRLTLT01GBM156N",
    # EUR / Germany
    "EUR_10yr":         "IRLTLT01DEM156N",
    # Japan
    "JPY_10yr":         "IRLTLT01JPM156N",
    # Canada
    "CAD_10yr":         "IRLTLT01CAM156N",
    # Australia (10yr)
    "AUD_10yr":         "IRLTLT01AUM156N",
}

# ── Yahoo Finance tickers ──────────────────────────────────────────
_YAHOO_TICKERS = {
    "gold":         "GC=F",
    "wti_oil":      "CL=F",
    "brent_oil":    "BZ=F",
    "natural_gas":  "NG=F",
    "iron_ore":     "SCOA.L",    # proxy
    # FX 1yr range
    "EUR_USD":      "EURUSD=X",
    "GBP_USD":      "GBPUSD=X",
    "USD_JPY":      "USDJPY=X",
    "AUD_USD":      "AUDUSD=X",
    "NZD_USD":      "NZDUSD=X",
    "USD_CAD":      "USDCAD=X",
    "USD_CHF":      "USDCHF=X",
    "EUR_GBP":      "EURGBP=X",
    "EUR_JPY":      "EURJPY=X",
    "GBP_JPY":      "GBPJPY=X",
    "AUD_NZD":      "AUDNZD=X",
    "EUR_CHF":      "EURCHF=X",
    "EUR_AUD":      "EURAUD=X",
}

# Central bank policy rates (manually maintained — these change rarely)
_POLICY_RATES = {
    "USD": 4.25,   # Fed Funds (upper bound)
    "EUR": 2.65,   # ECB deposit rate
    "GBP": 4.50,   # Bank of England
    "JPY": 0.50,   # Bank of Japan
    "AUD": 4.10,   # RBA
    "NZD": 3.75,   # RBNZ
    "CAD": 3.00,   # Bank of Canada
    "CHF": 0.25,   # SNB
}


def _fred_fetch(series_id: str, timeout: int = 8) -> Optional[float]:
    """Fetch latest value from FRED CSV endpoint. Returns float or None."""
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            lines = r.read().decode().strip().split('\n')
            # Find last non-empty, non-"." value
            for line in reversed(lines[1:]):  # skip header
                parts = line.split(',')
                if len(parts) >= 2 and parts[1].strip() not in ('', '.'):
                    return float(parts[1].strip())
    except Exception as e:
        logger.debug("FRED %s failed: %s", series_id, e)
    return None


def _yahoo_fetch(ticker: str, timeout: int = 8) -> Optional[Dict]:
    """Fetch price data from Yahoo Finance. Returns dict with price, 52w_high, 52w_low or None."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?interval=1d&range=252d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        result = d['chart']['result'][0]
        meta = result['meta']
        closes = result['indicators']['quote'][0].get('close', [])
        closes = [c for c in closes if c is not None]
        return {
            "price": meta.get('regularMarketPrice'),
            "52w_high": max(closes) if closes else None,
            "52w_low": min(closes) if closes else None,
            "52w_avg": round(sum(closes) / len(closes), 5) if closes else None,
        }
    except Exception as e:
        logger.debug("Yahoo %s failed: %s", ticker, e)
    return None


def get_interest_rates(base_ccy: str, quote_ccy: str) -> Dict[str, Any]:
    """Get policy rates and 10yr bond yields for both currencies."""
    result = {}
    for ccy in [base_ccy, quote_ccy]:
        prefix = "base" if ccy == base_ccy else "quote"
        # Policy rate (fast static lookup)
        result[f"{prefix}_currency_rate"] = _POLICY_RATES.get(ccy)
        # 10yr bond yield from FRED
        fred_key = f"{ccy}_10yr"
        if fred_key in _FRED_SERIES:
            val = _fred_fetch(_FRED_SERIES[fred_key])
            if val is not None:
                result[f"{prefix}_bond_yield_10yr"] = val

    # Rate differential
    base_r = result.get("base_currency_rate")
    quote_r = result.get("quote_currency_rate")
    if base_r is not None and quote_r is not None:
        result["rate_differential"] = round(base_r - quote_r, 4)

    return result


def get_us_macro() -> Dict[str, Any]:
    """Get US macro indicators from FRED (cached across all USD pairs)."""
    result = {}
    for key, series_id in [
        ("usd_fed_funds",   "FEDFUNDS"),
        ("usd_10yr",        "DGS10"),
        ("usd_unemployment","UNRATE"),
    ]:
        val = _fred_fetch(series_id)
        if val is not None:
            result[key] = val
    return result


def get_commodity_prices() -> Dict[str, Any]:
    """Get live commodity prices from Yahoo Finance."""
    result = {}
    for label, ticker in [("gold", "GC=F"), ("wti_oil", "CL=F"), ("brent_oil", "BZ=F")]:
        data = _yahoo_fetch(ticker, timeout=6)
        if data and data.get("price"):
            result[label] = data["price"]
    return result


def get_fx_range(instrument: str) -> Dict[str, Any]:
    """Get 1yr FX range and current price from Yahoo Finance."""
    ticker = _YAHOO_TICKERS.get(instrument)
    if not ticker:
        return {}
    data = _yahoo_fetch(ticker, timeout=6)
    if not data:
        return {}
    result = {}
    if data.get("price"):
        result["pair_current_price"] = data["price"]
    if data.get("52w_high"):
        result["pair_1yr_max"] = data["52w_high"]
    if data.get("52w_low"):
        result["pair_1yr_min"] = data["52w_low"]
    if data.get("52w_avg"):
        result["pair_1yr_avg"] = data["52w_avg"]
    # Range position 0-100%
    if result.get("pair_1yr_max") and result.get("pair_1yr_min") and result.get("pair_current_price"):
        rng = result["pair_1yr_max"] - result["pair_1yr_min"]
        if rng > 0:
            result["pair_range_position"] = round(
                (result["pair_current_price"] - result["pair_1yr_min"]) / rng * 100, 1
            )
    return result


def fetch_all_for_pair(instrument: str) -> Dict[str, Any]:
    """
    Fetch all economic data for a pair. Used by intelligence_agent_prep.py.
    Returns flat dict ready to merge into macro data.
    """
    parts = instrument.split("_")
    base = parts[0] if len(parts) == 2 else instrument[:3]
    quote = parts[1] if len(parts) == 2 else instrument[3:]

    result = {
        "data_source": "free_apis",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # Rates + bond yields (fast)
    try:
        rates = get_interest_rates(base, quote)
        result.update(rates)
    except Exception as e:
        logger.warning("Rates fetch failed for %s: %s", instrument, e)

    # FX range (1 Yahoo call)
    try:
        fx = get_fx_range(instrument)
        result.update(fx)
    except Exception as e:
        logger.warning("FX range fetch failed for %s: %s", instrument, e)

    # Commodity prices (relevant to pair)
    _CCY_COMMODITIES = {
        "AUD": ["gold", "brent_oil"],
        "CAD": ["wti_oil"],
        "NOK": ["brent_oil"],
        "USD": ["wti_oil", "gold"],
        "JPY": ["wti_oil"],
        "EUR": ["brent_oil"],
        "GBP": ["brent_oil"],
        "CHF": ["gold"],
    }
    relevant_commodities = set(
        _CCY_COMMODITIES.get(base, []) + _CCY_COMMODITIES.get(quote, [])
    )
    if relevant_commodities:
        try:
            commodities = get_commodity_prices()
            for c in relevant_commodities:
                if c in commodities:
                    result[c + "_price"] = commodities[c]
        except Exception as e:
            logger.warning("Commodity fetch failed: %s", e)

    return result


# ── Cross-asset tickers (for intelligence package) ──────────────────────────
CROSS_ASSET_TICKERS = {
    "vix":    "^VIX",
    "dxy":    "DX-Y.NYB",
    "sp500":  "^GSPC",
    "nasdaq": "^IXIC",
    "tlt":    "TLT",
    "btc":    "BTC-USD",
    "xlf":    "XLF",
    "xle":    "XLE",
}


def _classify_vix(level: float) -> str:
    """Classify VIX level into human-readable tier."""
    if level is None: return "unknown"
    if level < 15:   return "low"
    if level < 20:   return "normal"
    if level < 25:   return "elevated"
    if level < 30:   return "high"
    return "extreme"


def fetch_cross_asset_data() -> Dict[str, Any]:
    """
    Fetch cross-asset data (VIX, DXY, equities, bonds, crypto, sectors).
    No TTL caching — fetched fresh on each intelligence window run.
    Returns dict keyed by short name (vix, dxy, sp500, etc.)
    """
    results: Dict[str, Any] = {}
    for key, ticker in CROSS_ASSET_TICKERS.items():
        data = _yahoo_fetch(ticker, timeout=8)
        if not data:
            results[key] = {"ticker": ticker, "error": "fetch_failed"}
            continue

        price = data.get("price")
        hi52  = data.get("52w_high")
        lo52  = data.get("52w_low")

        entry: Dict[str, Any] = {
            "ticker":        ticker,
            "current_price": price,
            "52w_high":      hi52,
            "52w_low":       lo52,
        }

        if key == "vix" and price is not None:
            entry["level"] = _classify_vix(price)

        if key == "dxy" and price is not None and hi52 is not None and lo52 is not None:
            rng = hi52 - lo52
            pos = (price - lo52) / rng * 100 if rng > 0 else 50
            entry["52w_position_pct"] = round(pos, 1)
            entry["trend"] = "strong" if pos > 70 else "weak" if pos < 30 else "neutral"

        results[key] = entry

    return results


def _yahoo_fetch_closes(ticker: str, days: int = 20, timeout: int = 10) -> Optional[List[float]]:
    """Fetch recent daily close prices from Yahoo Finance. Returns list of floats or None."""
    try:
        range_str = f"{days + 5}d"  # Fetch slightly more to cover weekends/holidays
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(ticker)}?interval=1d&range={range_str}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        closes = d['chart']['result'][0]['indicators']['quote'][0].get('close', [])
        closes = [c for c in closes if c is not None]
        return closes[-days:] if len(closes) >= days else closes
    except Exception as e:
        logger.debug("Yahoo closes %s failed: %s", ticker, e)
    return None


def _pearson(x: List[float], y: List[float]) -> Optional[float]:
    """Compute Pearson correlation coefficient for two series of equal length."""
    n = min(len(x), len(y))
    if n < 5:
        return None
    x, y = x[:n], y[:n]
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    dx = sum((xi - mx) ** 2 for xi in x)
    dy = sum((yi - my) ** 2 for yi in y)
    denom = (dx * dy) ** 0.5
    return round(num / denom, 3) if denom > 0 else None


# Pairs in display order for correlation matrix
_CORR_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "NZD_USD",
    "USD_CAD", "USD_CHF", "EUR_GBP", "EUR_JPY", "GBP_JPY",
    "AUD_JPY", "EUR_AUD", "EUR_CHF",
]

_CORR_TICKERS = {
    "EUR_USD": "EURUSD=X", "GBP_USD": "GBPUSD=X", "USD_JPY": "USDJPY=X",
    "AUD_USD": "AUDUSD=X", "NZD_USD": "NZDUSD=X", "USD_CAD": "USDCAD=X",
    "USD_CHF": "USDCHF=X", "EUR_GBP": "EURGBP=X", "EUR_JPY": "EURJPY=X",
    "GBP_JPY": "GBPJPY=X", "AUD_JPY": "AUDJPY=X", "EUR_AUD": "EURAUD=X",
    "EUR_CHF": "EURCHF=X",
}

# Minimum |r| to include in strong-pairs summary
_CORR_STRONG_THRESHOLD = 0.70


def fetch_pair_correlations(days: int = 20) -> Dict[str, Any]:
    """
    Compute 20-day rolling pairwise price correlations for all 13 FX pairs.
    Uses Yahoo Finance for close price data.

    Returns:
        {
          "matrix":   {pair: {other_pair: r_value}},   # full correlation matrix
          "strong":   [(pair_a, pair_b, r), ...],       # |r| >= 0.70, sorted desc
          "clusters": {"risk_on": [...], "risk_off": [...], "commodity": [...]},
          "days":     int,
        }
    """
    # Fetch closes for each pair
    closes: Dict[str, List[float]] = {}
    for pair, ticker in _CORR_TICKERS.items():
        c = _yahoo_fetch_closes(ticker, days=days)
        if c and len(c) >= 5:
            closes[pair] = c

    if not closes:
        logger.warning("fetch_pair_correlations: no price data available")
        return {}

    # Build pairwise matrix
    pairs = [p for p in _CORR_PAIRS if p in closes]
    matrix: Dict[str, Dict[str, float]] = {}
    strong: list = []

    for i, pa in enumerate(pairs):
        matrix[pa] = {}
        for j, pb in enumerate(pairs):
            if pa == pb:
                matrix[pa][pb] = 1.0
            elif j > i:
                r = _pearson(closes[pa], closes[pb])
                if r is not None:
                    matrix[pa][pb] = r
                    matrix.setdefault(pb, {})[pa] = r
                    if abs(r) >= _CORR_STRONG_THRESHOLD:
                        strong.append((pa, pb, r))

    strong.sort(key=lambda x: abs(x[2]), reverse=True)

    return {
        "matrix":  matrix,
        "strong":  strong[:20],  # top 20 pairs
        "days":    days,
        "pairs_computed": len(pairs),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Testing economic_data_fetcher...")
    for pair in ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD"]:
        data = fetch_all_for_pair(pair)
        print(f"\n{pair}:")
        for k, v in data.items():
            if k not in ("data_source", "fetched_at"):
                print(f"  {k}: {v}")
