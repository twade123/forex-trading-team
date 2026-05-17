# Wolfram MCP Skill — handler_wolfram (Wolfram|Alpha API)

Complete reference for the Wolfram MCP. Covers everything this tool can do, for any use case.

---

## Connection
- **Full Results API**: `https://api.wolframalpha.com/v2/query` (structured pods, XML → parsed to dict)
- **LLM API**: `https://www.wolframalpha.com/api/v1/llm-api` (pre-formatted text for AI consumption) ⭐ **PREFERRED**
- **Key**: `~/jarvis/API/WOLFRAM_API_KEY.txt`
- **Free tier**: 2,000 queries/month (~67/day)
- **Same AppID works for both APIs**

---

## Two APIs — When to Use Which

| API | Best For | Output | Speed |
|-----|----------|--------|-------|
| **LLM API** ⭐ | Economic data, country research, financial lookups, general research | Clean pre-formatted text + image URLs | Fast |
| **Full Results API** | Structured data extraction (p-values, specific numbers), pod filtering | Dict of pods with subpods | Medium |

**Rule: Default to LLM API.** Only use Full Results API when you need structured pod data for programmatic extraction.

---

## LLM API — `query_llm_api(query, maxchars)` ⭐ PRIMARY

Pre-formatted text response optimized for AI/LLM consumption. Includes chart image URLs and Wolfram Language code.

```python
# handler_wolfram.query_llm_api(query, maxchars=6800)
# OR direct: GET https://www.wolframalpha.com/api/v1/llm-api?input={query}&appid={key}&maxchars={n}

params: {
    query: str,       # REQUIRED. Natural language or math expression.
    maxchars: int     # Max response length. Default: 6800. Use 500 for quick lookups.
}

returns: {
    'success': bool,
    'text': str,          # Full formatted response (on success)
    'suggestions': list,  # Alternative queries (on 501 failure)
    'query': str          # Original query
}
```

**Key behaviors:**
- Returns clean text with labeled sections (no XML/JSON parsing needed)
- Includes `image: https://...` URLs for charts and plots
- Includes `Wolfram Language code:` for reproducibility
- On 501 failure, returns **suggested alternative queries** (not just an error)
- Simplified keyword queries work best (e.g. `"US CPI"` not `"what is the consumer price index"`)

**Sample response:**
```
Query: "US federal funds rate"

Input interpretation:
United States | effective federal funds rate | weekly

Latest result:
3.64% (February 11, 2026)

Effective federal funds rate history:
image: https://public6.wolframalpha.com/files/PNG_xxx.png

Long-term interest rates:
10-year treasury note | 4.26%
30-year treasury bond | 4.88%
Moody's Aaa bonds | 5.4%
Moody's Baa bonds | 5.9%
conventional mortgage rate | 6.1%

Short-term interest rates:
federal funds rate | 3.64%
3-month treasury bill | 3.68%
bank prime rate | 6.75%
```

---

## Full Results API — `query_wolfram_alpha(query, format_type, include_pods, ...)`

Structured pod-based results. Returns dict keyed by pod title.

```python
# handler_wolfram.query_wolfram_alpha(query, format_type, include_pods, exclude_pods, timeout, max_width)

params: {
    query: str,              # REQUIRED.
    format_type: str,        # "plaintext" | "image" | "mathml". Default: "plaintext"
    include_pods: [str],     # Optional. Filter to specific pods.
    exclude_pods: [str],     # Optional. Exclude specific pods.
    timeout: int,            # Default: 30
    max_width: int           # Default: 500 (for images)
}

returns: {
    "Pod Title": {
        "id": "PodID",
        "subpods": [{"plaintext": "result text", "img": {"src": "url", ...}}]
    },
    ...
}
```

**Extracting data:** `result["Result"]["subpods"][0]["plaintext"]`

### Other Full Results API Functions
- `get_simple_answer(query)` — Returns just the primary answer as a string (or None)
- `get_enhanced_result(query)` — Auto-predicts relevant pods, filters results
- `get_optimized_result(query)` — Classifies intent, maps to optimal pods
- `get_pod_titles(query)` — Lists available pod titles (for discovery/debugging)
- `classify_query_intent(query)` — Returns intent category + confidence

### Handler Actions (via `handle()`)
```python
handler.handle("llm_query", query="US CPI", maxchars=2000)     # LLM API
handler.handle("query", query="Pearson correlation of ...")      # Full Results API
handler.handle("simple", query="(0.65*1.5-0.35)/1.5")          # Simple answer
handler.handle("enhanced", query="Japan GDP")                    # Auto-filtered
handler.handle("optimized", query="solve x^2-4=0")             # Intent-optimized
handler.handle("pod_titles", query="US inflation rate")          # Pod discovery
handler.handle("classify", query="100 miles to km")              # Intent classification
```

---

## What Works — Confirmed Live (Feb 2026, Free Tier)

### Economic Indicators (LLM API)
| Query | Returns | Freshness |
|-------|---------|-----------|
| `"US federal funds rate"` | 3.64% + ALL short/long-term rates (10yr, 30yr, Aaa, Baa, mortgage, prime) | Weekly |
| `"US inflation rate"` | 2.386%/yr + core, food, energy breakdown | Monthly |
| `"US CPI"` | 325.3 index + MoM/YoY change + component breakdown | Monthly |
| `"US unemployment rate"` | 4.3% + nonfarm payrolls 158.6M + labor force 171.9M | Monthly |
| `"US nonfarm payrolls"` | Full employment breakdown by industry sector | Monthly |
| `"US trade deficit"` | -$846.4B current account balance | Annual |
| `"UK inflation rate"` | 3.27%/yr + GDP deflator + wholesale | Annual |
| `"UK interest rate"` | Real rate, lending, deposit, spread, risk premium | Annual |

### Country Economic Profiles
| Query | Returns |
|-------|---------|
| `"Japan GDP"` | $4.028T + GDP at parity + sector breakdown (agriculture/industry/manufacturing) + currency conversions |
| `"Japan trade balance"` | $157.7B surplus (world rank 3rd) |
| `"Japan unemployment rate"` | 2.5% + by education + long-term + labor force breakdown |
| `"Australia GDP growth rate"` | 1.373%/yr + full economic properties (GDP at exchange, parity, per capita) |
| `"Australia unemployment rate"` | 3.94% + by education + long-term |
| `"Canada oil production"` | 4.074M bbl/day + natural gas + coal production |
| `"Canada unemployment rate"` | Full breakdown with education + labor force |
| `"Switzerland trade balance"` | $59.16B surplus |
| `"eurozone interest rate"` | Median + range across member states |
| `"China GDP growth"` | Growth rate + full economic properties |
| `"exports New Zealand"` | Export commodities: dairy, fish, machinery, meat, wood |
| `"US trade balance vs China"` | Side-by-side: US -$846.4B vs China +$317.3B |
| `"compare GDP US Japan eurozone"` | Side-by-side: US $31.1T, Japan $4.0T, Eurozone $16.5T |

### Commodities & Currencies
| Query | Returns |
|-------|---------|
| `"crude oil price"` / `"oil price WTI"` | $60.04/bbl + MoM/YoY change + chart | Monthly |
| `"gold price per ounce"` | $4,548 + multi-currency conversions + 1yr min/max/avg + volatility | Live |
| `"1 euro to US dollars"` | $1.19 + 1yr min ($1.04) / max ($1.20) / avg ($1.15) + volatility 7.4% | Live |
| `"US 10 year treasury yield"` | Yield curve data | Current |
| `"treasury yield curve"` | Full yield curve | Current |

### Options & Derivatives
| Query | Returns |
|-------|---------|
| `"option pricing formula"` | Full Black-Scholes: value ($1.66), delta, gamma, vega, theta, rho + option ladder + delta hedging table |

### Statistical Computation (Full Results API best)
| Query | Returns |
|-------|---------|
| `"Pearson correlation of {data1} and {data2}"` | Correlation coefficient + p-value + degrees of freedom + plot | Computed |
| `"standard deviation of {data}"` | Exact value | Computed |
| Math expressions: `"(0.65*1.5-0.35)/1.5"` | 0.416667 + percentage + rational approximation | Computed |

---

## What DOESN'T Work (or Needs Rephrasing)

| Fails | Use Instead | Why |
|-------|-------------|-----|
| `"ECB interest rate"` | `"eurozone interest rate"` | Wolfram doesn't know "ECB" abbreviation |
| `"Bank of England interest rate"` | `"UK interest rate"` or `"England interest rate"` | Interprets BoE as a building |
| `"EUR/USD exchange rate"` | `"1 euro to US dollars"` | Doesn't parse forex pair notation |
| `"iron ore price"` | `"price iron"` or `"Australia iron ore exports"` | Query phrasing matters |
| `"New Zealand dairy exports"` | `"exports New Zealand"` | Flip the word order |
| `"Kelly criterion with win rate 0.65"` | `"(0.65*1.5-0.35)/1.5"` | NL descriptions → 501 error |
| `"Black-Scholes S=100 K=105..."` | `"option pricing formula"` | Use generic, let Wolfram provide defaults |

**Key rule:** When a query fails (501), the LLM API returns suggestions. Try those first before rephrasing manually.

---

## Rate Limits & Best Practices
- **2,000 queries/month** (~67/day) on free tier
- **LLM API is the default** — cleaner output, suggestions on failure
- **Full Results API only** when you need structured pod extraction (p-values, specific numbers)
- **`maxchars=500-1000`** for quick lookups, **`maxchars=6800`** for full research
- **Cache results** — economic data doesn't change intra-day (rates are weekly/monthly)
- **Simplify queries** — `"US CPI"` works better than `"what is the US consumer price index"`
- **Handle 501 gracefully** — try suggestions, rephrase as simpler keywords or math

---

## Error Handling
- **HTTP 501** (LLM API): Query not understood — response body contains suggestions
- **HTTP 501** (Full Results): Returns `success: false` in queryresult
- **HTTP 403**: Invalid API key or quota exceeded
- **HTTP 400**: Missing input parameter
- **Empty results**: Query parsed but no computable answer — rephrase
