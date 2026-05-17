# Trading Team Timing System — Dynamic Scheduling by Pair

> **Primary timeframe:** M15 (15-minute candles)
> **Higher timeframes:** H1 (alignment), H4 (trend bias)
> **Market hours:** Sunday 5:00 PM ET → Friday 5:00 PM ET
> **Config:** `Config/schedule_config.json` (static), `Config/pair_schedule.json` (dynamic, per-pair)

---

## 1. THE M15 HEARTBEAT

Everything revolves around the M15 candle. Candles close at :00, :15, :30, :45 every hour. The cycle starts immediately after candle close.

### M15 Candle Timeline (per cycle)

```
:00.00  ──── M15 candle closes ────
:00.01  Cycle START
:00.02  │ oanda_data: fetch M15/H1/H4 candles + pricing + account (2-3s)
:00.05  │ intelligence: read cache, fetch if expired (1-5s)
:00.10  │ technical_analyst: run indicators + patterns + confluence (2-3s)
:00.15  │ validator: heuristic gates + LLM validation (3-5s)
:00.20  │ orchestrator: decision (1-2s)
:00.25  │ execution: place order if signal (1-2s)
:00.30  │ reporter: log everything (1-2s)
:00.35  Cycle END (~35 seconds total)

:05.00  trade_monitor check #1 (if trades open)
:10.00  trade_monitor check #2 (if trades open)

:15.00  ──── Next M15 candle closes ────
:15.01  Next cycle START
```

### Why M15?

- **Fast enough** to catch intraday moves and mean reversion signals
- **Slow enough** that each candle is statistically meaningful (not noise)
- **4 cycles per hour** = maximum 96 cycles per trading day
- **Aligns with news** — most economic releases happen on the hour or half-hour, so the M15 candle right after the release captures the initial reaction

---

## 2. PAIR-AWARE SCHEDULING

Different pairs are active at different times. Trading EUR_USD during Tokyo session is like fishing in an empty pond. The schedule adapts to which pairs the user has selected.

### Session-Pair Matrix

| Pair | Sydney (5pm-2am) | Tokyo (7pm-4am) | London (3am-12pm) | NY (8am-5pm) | Best Window |
|------|:-:|:-:|:-:|:-:|---|
| EUR_USD | ○ | ○ | ● | ● | **London-NY overlap (8am-12pm)** |
| GBP_USD | ○ | ○ | ● | ● | **London-NY overlap (8am-12pm)** |
| USD_JPY | ○ | ● | ● | ● | **Tokyo-London overlap (3am-4am)**, NY |
| EUR_JPY | ○ | ● | ● | ○ | **Tokyo + London** |
| GBP_JPY | ○ | ● | ● | ○ | **London** |
| AUD_USD | ● | ● | ○ | ● | **Sydney-Tokyo overlap**, NY |
| NZD_USD | ● | ● | ○ | ○ | **Sydney-Tokyo overlap** |
| AUD_NZD | ● | ● | ○ | ○ | **Sydney-Tokyo** |
| USD_CAD | ○ | ○ | ○ | ● | **NY session (8am-5pm)** |
| USD_CHF | ○ | ○ | ● | ● | **London-NY overlap** |
| EUR_GBP | ○ | ○ | ● | ○ | **London (3am-12pm)** |
| EUR_CHF | ○ | ○ | ● | ○ | **London** |
| EUR_AUD | ● | ● | ● | ○ | **London + Sydney** |

● = Active (good liquidity, tight spreads) | ○ = Quiet (wide spreads, choppy)

### Dynamic Schedule Generation

When the user selects pairs to trade, the system generates a pair-specific schedule:

```python
def generate_pair_schedule(selected_pairs: list) -> dict:
    """Generate cron schedule based on selected pairs."""
    schedule = {}
    
    for pair in selected_pairs:
        pair_config = PAIR_SESSIONS[pair]
        schedule[pair] = {
            "active_sessions": pair_config["active_sessions"],
            "best_window": pair_config["best_window"],
            "cycle_priority": pair_config["priority_sessions"],  # run first in these sessions
            "skip_sessions": pair_config["skip_sessions"],       # don't trade in these
            "intelligence_queries": get_pair_queries(pair),       # what to search for
            "news_keywords": get_pair_news_keywords(pair),        # news search terms
            "macro_currencies": get_pair_currencies(pair),        # which currencies to track
            "weather_locations": get_pair_weather(pair),           # weather check cities
        }
    
    return schedule
```

---

## 3. INTELLIGENCE TIMING — LOOKBACK & FORECAST

### Wolfram Macro Data — 8-Day Lookback + Forward Context

Macro data moves slowly but the CONTEXT of when data was released matters.

| Data Type | Refresh | Lookback | Forward | Cache TTL | Why |
|-----------|---------|----------|---------|-----------|-----|
| Interest rates | Daily 6am ET | Current + last change date | Next meeting date | 24h | Rates change 8x/year per central bank |
| Inflation (CPI) | Daily 6am ET | Last 3 readings (3 months) | Next release date | 24h | Trend matters more than single reading |
| Employment (NFP) | Daily 6am ET | Last 2 readings | Next release date | 24h | MoM trend shows direction |
| GDP | Weekly (Mon 6am) | Last 2 quarters | Next release date | 7 days | Quarterly data, slow-moving |
| Trade balance | Weekly (Mon 6am) | Last quarter | — | 7 days | Quarterly, structural |
| Oil/energy | Every 4h | Current + 1-week trend | — | 4h | Moves intraday, matters for CAD |
| DXY / Gold | Every 1h | Current + 1-week trend | — | 1h | Broad market context |
| Exchange rate range | Daily 6am ET | 1-year min/max/avg | — | 24h | Where is price relative to range? |

**8-day lookback strategy:**
When intelligence queries Wolfram for macro data, it should capture:
1. **Current value** — what's the rate/number right now?
2. **When it last changed** — was there a recent shift? (within 8 days)
3. **Trend direction** — is it going up, down, or flat over the last 3 readings?
4. **Next event date** — when is the next release/meeting that could change this?

This gives the orchestrator: "US rates at 3.64%, last changed Jan 29 (cut 25bp), next meeting Mar 18. Trend: cutting cycle, 3 cuts in 6 months."

### News Data — Event Calendar + Lookback

| Data Type | Refresh | Lookback | Forward | Cache TTL |
|-----------|---------|----------|---------|-----------|
| Breaking news | Every 15 min | Last 4 hours | — | 15 min |
| Economic calendar | Every 15 min | — | Next 8 days | 15 min |
| Sentiment analysis | Every 15 min | Last 24h articles | — | 15 min |
| Central bank speeches | Every 15 min | Last 7 days | Next 7 days | 15 min |

**Forward calendar (8 days) is critical:**
Intelligence needs to know: "NFP is in 3 days. ECB meeting in 5 days. US CPI in 7 days." The orchestrator uses this to:
- Avoid opening multi-day trades before high-impact events
- Tighten stops on existing trades as events approach
- Know when to expect volatility spikes

### Weather Data — 5-7 Day Forecast

| Data Type | Refresh | Lookback | Forward | Cache TTL | Pairs Affected |
|-----------|---------|----------|---------|-----------|---------------|
| Severe weather | Every 30 min | Current | 5-7 day forecast | 30 min | AUD, NZD, CAD |
| Natural disasters | Every 30 min | Last 48h | — | 30 min | All (if near financial center) |

**Only query for commodity currencies + financial centers:**
- AUD/NZD: Sydney, Melbourne, Auckland, Wellington
- CAD: Toronto, Calgary (oil region)
- JPY: Tokyo (earthquake zone)
- USD: NYC, Washington DC (if hurricane season)
- GBP: London (if extreme event)

**Skip weather entirely for:** EUR_CHF, EUR_GBP (no commodity exposure, no weather-sensitive geography)

---

## 4. PER-PAIR INTELLIGENCE QUERIES

When the user selects a pair, intelligence knows exactly what to search for.

### EUR_USD

```json
{
  "pair": "EUR_USD",
  "currencies": ["USD", "EUR"],
  "wolfram_macro": {
    "daily": [
      {"query": "US federal funds rate", "cache_key": "wolfram:rate:USD"},
      {"query": "eurozone interest rate", "cache_key": "wolfram:rate:EUR"},
      {"query": "US inflation rate", "cache_key": "wolfram:inflation:USD"},
      {"query": "eurozone inflation rate", "cache_key": "wolfram:inflation:EUR"},
      {"query": "US unemployment rate", "cache_key": "wolfram:unemployment:USD"},
      {"query": "1 euro to US dollars", "cache_key": "wolfram:fx:EUR_USD"}
    ],
    "weekly": [
      {"query": "US GDP growth rate", "cache_key": "wolfram:gdp:USD"},
      {"query": "eurozone GDP growth rate", "cache_key": "wolfram:gdp:EUR"},
      {"query": "US trade deficit", "cache_key": "wolfram:trade:USD"}
    ]
  },
  "news_keywords": [
    "Federal Reserve AND (interest rate OR monetary policy OR inflation)",
    "ECB AND (interest rate OR monetary policy OR inflation)",
    "US nonfarm payrolls OR US employment",
    "US CPI OR US inflation OR consumer price",
    "eurozone CPI OR eurozone inflation"
  ],
  "weather_locations": [],
  "active_sessions": ["london", "new_york"],
  "best_window": {"start_et": "08:00", "end_et": "12:00"},
  "skip_sessions": ["sydney"]
}
```

### USD_JPY

```json
{
  "pair": "USD_JPY",
  "currencies": ["USD", "JPY"],
  "wolfram_macro": {
    "daily": [
      {"query": "US federal funds rate", "cache_key": "wolfram:rate:USD"},
      {"query": "Japan interest rate", "cache_key": "wolfram:rate:JPY"},
      {"query": "US inflation rate", "cache_key": "wolfram:inflation:USD"},
      {"query": "Japan inflation rate", "cache_key": "wolfram:inflation:JPY"},
      {"query": "Japan unemployment rate", "cache_key": "wolfram:unemployment:JPY"},
      {"query": "1 US dollar to Japanese yen", "cache_key": "wolfram:fx:USD_JPY"}
    ],
    "weekly": [
      {"query": "Japan GDP", "cache_key": "wolfram:gdp:JPY"},
      {"query": "Japan trade balance", "cache_key": "wolfram:trade:JPY"}
    ]
  },
  "news_keywords": [
    "Federal Reserve AND (interest rate OR monetary policy)",
    "Bank of Japan AND (interest rate OR monetary policy OR yield curve)",
    "Japan intervention OR yen intervention",
    "US nonfarm payrolls OR US employment"
  ],
  "weather_locations": ["Tokyo"],
  "active_sessions": ["tokyo", "london", "new_york"],
  "best_window": {"start_et": "08:00", "end_et": "12:00"},
  "skip_sessions": []
}
```

### AUD_USD (Commodity Pair)

```json
{
  "pair": "AUD_USD",
  "currencies": ["AUD", "USD"],
  "wolfram_macro": {
    "daily": [
      {"query": "Australia interest rate", "cache_key": "wolfram:rate:AUD"},
      {"query": "US federal funds rate", "cache_key": "wolfram:rate:USD"},
      {"query": "Australia inflation rate", "cache_key": "wolfram:inflation:AUD"},
      {"query": "Australia unemployment rate", "cache_key": "wolfram:unemployment:AUD"},
      {"query": "price iron", "cache_key": "wolfram:iron_ore"},
      {"query": "China GDP growth", "cache_key": "wolfram:gdp:CNY"}
    ],
    "weekly": [
      {"query": "Australia GDP growth rate", "cache_key": "wolfram:gdp:AUD"},
      {"query": "exports Australia", "cache_key": "wolfram:trade:AUD"}
    ]
  },
  "news_keywords": [
    "Reserve Bank of Australia AND (interest rate OR monetary policy)",
    "Federal Reserve AND (interest rate OR monetary policy)",
    "China economy OR China trade OR China manufacturing PMI",
    "iron ore price OR Australian commodities"
  ],
  "weather_locations": ["Sydney", "Melbourne"],
  "active_sessions": ["sydney", "tokyo", "new_york"],
  "best_window": {"start_et": "19:00", "end_et": "02:00"},
  "skip_sessions": ["london"]
}
```

### Currency → Query Mapping (All 8 Currencies)

```json
{
  "USD": {
    "country": "US",
    "central_bank": "Federal Reserve",
    "rate_query": "US federal funds rate",
    "inflation_query": "US inflation rate",
    "employment_query": "US unemployment rate",
    "gdp_query": "US GDP growth rate",
    "trade_query": "US trade deficit",
    "news_bank": "Federal Reserve AND (interest rate OR monetary policy OR inflation)",
    "news_jobs": "US nonfarm payrolls OR US employment",
    "news_prices": "US CPI OR US inflation OR consumer price"
  },
  "EUR": {
    "country": "eurozone",
    "central_bank": "ECB",
    "rate_query": "eurozone interest rate",
    "inflation_query": "eurozone inflation rate",
    "employment_query": "eurozone unemployment rate",
    "gdp_query": "eurozone GDP growth rate",
    "trade_query": "eurozone trade balance",
    "news_bank": "ECB AND (interest rate OR monetary policy OR inflation)",
    "news_prices": "eurozone CPI OR eurozone inflation"
  },
  "GBP": {
    "country": "UK",
    "central_bank": "Bank of England",
    "rate_query": "UK interest rate",
    "inflation_query": "UK inflation rate",
    "employment_query": "UK unemployment rate",
    "gdp_query": "UK GDP growth rate",
    "trade_query": "UK trade balance",
    "news_bank": "Bank of England AND (interest rate OR monetary policy)",
    "news_prices": "UK CPI OR UK inflation"
  },
  "JPY": {
    "country": "Japan",
    "central_bank": "Bank of Japan",
    "rate_query": "Japan interest rate",
    "inflation_query": "Japan inflation rate",
    "employment_query": "Japan unemployment rate",
    "gdp_query": "Japan GDP",
    "trade_query": "Japan trade balance",
    "news_bank": "Bank of Japan AND (interest rate OR monetary policy OR yield curve)",
    "news_extra": "Japan intervention OR yen intervention"
  },
  "AUD": {
    "country": "Australia",
    "central_bank": "RBA",
    "rate_query": "Australia interest rate",
    "inflation_query": "Australia inflation rate",
    "employment_query": "Australia unemployment rate",
    "gdp_query": "Australia GDP growth rate",
    "trade_query": "exports Australia",
    "news_bank": "Reserve Bank of Australia AND (interest rate OR monetary policy)",
    "commodity_query": "price iron",
    "news_extra": "China economy OR China trade OR iron ore price"
  },
  "NZD": {
    "country": "New Zealand",
    "central_bank": "RBNZ",
    "rate_query": "New Zealand interest rate",
    "inflation_query": "New Zealand inflation rate",
    "employment_query": "New Zealand unemployment rate",
    "gdp_query": "New Zealand GDP growth rate",
    "trade_query": "exports New Zealand",
    "news_bank": "RBNZ AND (interest rate OR monetary policy)",
    "commodity_query": "dairy price index",
    "news_extra": "China economy OR New Zealand dairy"
  },
  "CAD": {
    "country": "Canada",
    "central_bank": "Bank of Canada",
    "rate_query": "Canada interest rate",
    "inflation_query": "Canada inflation rate",
    "employment_query": "Canada unemployment rate",
    "gdp_query": "Canada GDP growth rate",
    "trade_query": "Canada trade balance",
    "news_bank": "Bank of Canada AND (interest rate OR monetary policy)",
    "commodity_query": "crude oil price",
    "news_extra": "Canada oil production OR OPEC"
  },
  "CHF": {
    "country": "Switzerland",
    "central_bank": "SNB",
    "rate_query": "Switzerland interest rate",
    "inflation_query": "Switzerland inflation rate",
    "employment_query": "Switzerland unemployment rate",
    "gdp_query": "Switzerland GDP growth rate",
    "trade_query": "Switzerland trade balance",
    "news_bank": "Swiss National Bank AND (interest rate OR monetary policy)",
    "news_extra": "Swiss franc safe haven OR SNB intervention"
  }
}
```

---

## 5. FULL TEAM SCHEDULE — DAILY TIMELINE

### Pre-Market (Before Sunday 5pm ET)

```
Saturday (Market Closed):
  Nothing runs. No API calls. No checks.

Sunday:
  04:00 ET  Knowledge maintenance (purge expired cache, refresh stats)
  05:00 ET  Intelligence: weekly macro refresh (GDP, trade balance — 7-day TTL)
  06:00 ET  Intelligence: daily macro refresh (rates, inflation, employment — 24h TTL)
            → 32 Wolfram queries (4 per currency × 8 currencies)
            → Cached for the entire day
  16:30 ET  Intelligence: pre-market news scan for all selected pairs
            → Check what happened over the weekend
            → Economic calendar for the week ahead (8 days forward)
  17:00 ET  ──── MARKET OPENS ────
  17:00 ET  Skip first 30 min (low liquidity, Sunday gap risk)
  17:30 ET  First M15 cycle eligible to run
```

### Active Trading Day (Mon-Thu)

```
Recurring Jobs:
┌─────────────────────────────────────────────────────────────┐
│ Every 15 min  │ Trading Cycle (full 8-agent sequence)       │
│ Every 15 min  │ News refresh (matches cycle, 15-min TTL)    │
│ Every 5 min   │ Trade monitor (only when trades open)       │
│ Every 30 min  │ Weather refresh (commodity pairs only)      │
│ Every 1 hour  │ Cross rates: DXY + gold (broad context)    │
│ Every 4 hours │ Oil/energy refresh (CAD, risk sentiment)    │
└─────────────────────────────────────────────────────────────┘

Daily Jobs:
  05:00 ET  Knowledge maintenance
  06:00 ET  Daily macro refresh (intelligence)
  17:30 ET  Daily summary (reporter)

Candle-Aligned Jobs:
  :00, :15, :30, :45  M15 cycle runs (after candle close)
  :00                 H1 candle also closed — extra significance
                      → Technical analyst has fresh H1 data
                      → Intelligence checks hourly cross rates
  Every 4th H1 (:00 every 4 hours, aligned to trading day)
                      H4 candle closed — major significance
                      → Technical analyst recalculates H4 alignment
                      → Orchestrator reassesses trend bias
```

### Friday (Close Day)

```
  ...normal trading until...
  15:00 ET  Close warning starts (2h before close)
            → Orchestrator: no new trades
            → Orchestrator: evaluate open trades for exit
            → Trade monitor: increase check frequency to every 2 min
  16:30 ET  Last new M15 cycle (but no new entries)
  16:45 ET  Final position check — close any trades you don't want to hold over weekend
  17:00 ET  ──── MARKET CLOSES ────
  17:30 ET  Daily summary (reporter)
  18:00 ET  Weekly summary (reporter)
            → Week's P&L, setup performance vs backtest
            → Intelligence accuracy review
            → Drift detection report
            → Recommendations for next week
```

---

## 6. PER-AGENT TASK SCHEDULE

### oanda_data — Tasks & Timing

| Task | When | Trigger | What |
|------|------|---------|------|
| Fetch M15 candles | Every 15 min | Cycle start | 100 bars for each selected pair |
| Fetch H1 candles | Every 15 min | Cycle start | 250 bars (includes fresh H1 at :00) |
| Fetch H4 candles | Every 15 min | Cycle start | 50 bars (includes fresh H4 when aligned) |
| Fetch pricing | Every 15 min | Cycle start | Bid/ask/spread for current pair + any open positions |
| Fetch account | Every 15 min | Cycle start | Balance, margin, P&L, positions |
| Supplemental pricing | Every 15 min | If open trades on OTHER pairs | Latest candle + spread for position management |
| Daily P&L calc | Every 15 min | If any trades closed today | `list_trades(state="CLOSED")` filtered to today |

**Pre-candle check (new):** 5 seconds before candle close (:14:55, :29:55, :44:55, :59:55), fetch latest tick to have the most current price for the candle that's about to close. This ensures the cycle starts with the freshest possible data.

---

### intelligence — Tasks & Timing

| Task | When | Trigger | What |
|------|------|---------|------|
| Daily macro | 6:00 AM ET | Cron | All rates, inflation, employment for currencies in selected pairs |
| Weekly macro | Mon 6:00 AM ET | Cron | GDP, trade balance for all currencies |
| News scan | Every 15 min | Cycle | Breaking news + calendar for BOTH currencies in the pair |
| 8-day calendar | Every 15 min | Cycle | High-impact events in next 8 days (with countdown) |
| Weather check | Every 30 min | Cron | Only for commodity pairs (AUD, NZD, CAD) + JPY (earthquake) |
| Oil/energy | Every 4h | Cron | Crude oil price (affects CAD, risk sentiment) |
| Cross rates | Every 1h | Cron | DXY + gold (broad context) |
| Kelly calculation | Per cycle | When trade signal exists | Fresh computation, no cache |
| Correlation check | Per cycle | When opening new trade with existing positions | Fresh computation, no cache |
| Pre-market scan | Sun 4:30 PM ET | Cron | Weekend news, week-ahead calendar |
| Lookback reference | Per cycle | When cached data > 4h old | Check if prior query results still valid in current context |

**Lookback reference strategy:**
When the orchestrator needs intelligence context and the cache is hours old, intelligence doesn't just return stale data. It:
1. Returns the cached data with timestamp
2. Flags: "Macro data from 6:00 AM, 4.5 hours old. No known events since that would change rates."
3. OR: "Macro data from 6:00 AM, but ECB spoke at 9:00 AM — recommend fresh query."

This avoids unnecessary API calls while ensuring stale data is flagged when context has changed.

---

### technical_analyst — Tasks & Timing

| Task | When | Trigger | What |
|------|------|---------|------|
| Full analysis | Every 15 min | Cycle (after oanda_data) | All indicators, patterns, confluence, regime, alignment |
| Regime check | On demand | Orchestrator request (via trade_monitor alert) | Quick ADX + volatility check on latest candles |
| H4 alignment update | When H4 candle closes | Part of full analysis at H4-aligned times | Recalculate H4 trend direction |
| Pre-candle pattern check | :14:50, :29:50, :44:50, :59:50 | 10 sec before close | Check if forming candle matches a pattern (early signal) |

**H1 and H4 candle significance:**
- When M15 cycle runs at :00 → H1 just closed. Technical analyst has 4 fresh M15 candles summarized into 1 H1. This is a higher-confidence cycle.
- When M15 cycle runs at a time aligned with H4 close → H4 just closed. This is the highest-confidence cycle for trend analysis.
- Orchestrator should weight :00 cycles slightly higher for new trade entries.

---

### validator — Tasks & Timing

| Task | When | Trigger | What |
|------|------|---------|------|
| Full validation | Per cycle | Orchestrator sends data | Heuristic gates + LLM validation |
| Quick check | On demand | Orchestrator request (mid-trade) | DB evidence query only (no full LLM) |

No recurring cron. Validator is purely reactive — called by orchestrator.

---

### execution — Tasks & Timing

| Task | When | Trigger | What |
|------|------|---------|------|
| Place order | Per cycle | Orchestrator trade plan | Market/limit/stop order |
| Modify trade | On demand | Orchestrator instruction | Move SL/TP, switch to trailing |
| Close trade | On demand | Orchestrator instruction | Full or partial close |
| Position status | On demand | Orchestrator request | Query open trades |

No recurring cron. Execution is purely reactive.

---

### trade_monitor — Tasks & Timing

| Task | When | Trigger | What |
|------|------|---------|------|
| Position check | Every 5 min | Cron (when trades open) | All open trade status + spreads + account |
| News proximity | Every 5 min | Part of position check | Check cached news for events < 30 min away |
| Recently closed | Every 5 min | Part of position check | Detect SL/TP fills since last check |
| Friday close prep | Every 2 min (Fri 3-5pm) | Cron override | More frequent checks before close |

**Activation/deactivation:**
- Orchestrator opens first trade → enable 5-min cron
- Last trade closes → disable cron (save API calls)
- Friday 3pm → switch to 2-min checks

---

### reporter — Tasks & Timing

| Task | When | Trigger | What |
|------|------|---------|------|
| Cycle log | Every 15 min | End of cycle | Signal, decision, validation, trade (if any) |
| Trade exit log | On event | Trade closes (SL/TP/manual) | Log exit, link outcome to intelligence |
| Daily summary | 5:30 PM ET | Cron | Day's P&L, trades, win rate, holds, API budget |
| Weekly summary | Fri 6:00 PM ET | Cron | Week performance, drift, setup analysis |
| Performance drift | Daily 5:00 PM ET | Part of daily summary | Live vs backtest comparison per setup |

---

### cycle_orchestrator — Tasks & Timing

| Task | When | Trigger | What |
|------|------|---------|------|
| Run cycle | Every 15 min | M15 candle close | Full 8-step sequence via trading_cycle.py |
| React to alerts | Any time | Trade monitor report | Call agents on demand, adjust trades |
| User commands | Any time | User message | Answer questions, take direction |
| Risk check | Every cycle | Part of decision step | Daily loss, concurrent trades, correlation |
| Pair rotation | Every cycle | If multiple pairs selected | Round-robin or priority-based pair selection |
| Session awareness | Every cycle | Pre-check | Am I in the best window for this pair? Adjust confidence. |

---

## 7. PAIR ROTATION — MULTI-PAIR SCHEDULING

When the user selects multiple pairs, the orchestrator rotates through them. But not randomly — priority based on session.

### Priority System

```
For each M15 cycle:
  1. Which pairs are in their BEST window right now?
     → These get priority (highest liquidity, tightest spreads)
  2. Which pairs are in an ACTIVE session?
     → These are eligible
  3. Which pairs are in a QUIET session?
     → Skip these (or reduce size if forced to trade)
```

### Example: 3 Pairs Selected (EUR_USD, USD_JPY, AUD_USD)

```
8:00 AM ET cycle:  EUR_USD (London-NY overlap — best window)
8:15 AM ET cycle:  USD_JPY (NY session — active)
8:30 AM ET cycle:  EUR_USD (still in best window — gets more cycles)
8:45 AM ET cycle:  USD_JPY
...
7:00 PM ET cycle:  AUD_USD (Sydney opening — becomes active)
7:15 PM ET cycle:  USD_JPY (Tokyo opening — becomes active)
7:30 PM ET cycle:  AUD_USD
```

The orchestrator assigns more cycles to pairs in their best window.

---

## 8. DASHBOARD — USER CONTROLS

### Pair Selector Section

```
┌─ Active Trading Pairs ──────────────────────────┐
│                                                   │
│  ☑ EUR_USD   ☑ GBP_USD   ☐ USD_JPY              │
│  ☐ AUD_USD   ☐ NZD_USD   ☐ USD_CAD              │
│  ☐ USD_CHF   ☐ EUR_GBP   ☐ EUR_JPY              │
│  ☐ GBP_JPY   ☐ AUD_NZD   ☐ EUR_CHF              │
│  ☐ EUR_AUD                                        │
│                                                   │
│  [Apply Changes]                                  │
└───────────────────────────────────────────────────┘
```

When the user changes pairs:
1. `Config/pair_schedule.json` is updated
2. Intelligence agent's query list updates (new currencies, new news keywords)
3. Cron jobs update (session-appropriate timing for new pairs)
4. Orchestrator's rotation table updates
5. All agents receive the updated pair configuration

### OANDA API Key Section

```
┌─ OANDA Connection ──────────────────────────────┐
│                                                   │
│  API Key: ●●●●●●●●●●●●●●●●●●●●  [Change]       │
│  Account: 101-001-24637237-001                    │
│  Environment: ○ Practice  ● Live                  │
│  Status: ✅ Connected (last check: 2 min ago)     │
│                                                   │
└───────────────────────────────────────────────────┘
```

- API key stored under user ID, tokenized (never displayed in full)
- Validated on entry (ping OANDA, verify account access)
- Environment toggle (practice vs live) updates all URLs

### Risk Settings Section

```
┌─ Risk Management ───────────────────────────────┐
│                                                   │
│  Min Confluence Score:  [70]  (50-100)           │
│  Min Risk:Reward Ratio: [1.5] (1.0-5.0)         │
│  Max Daily Loss %:      [3.0] (1.0-10.0)        │
│  Max Concurrent Trades: [3]   (1-10)             │
│  Max Risk Per Trade %:  [2.0] (0.5-5.0)         │
│  Max Correlated Pos:    [1]   (1-3)              │
│                                                   │
│  Position Sizing:                                 │
│  ○ Fixed %   ● Half-Kelly   ○ Full Kelly         │
│  Max Position %:        [2.0] (0.5-5.0)          │
│                                                   │
│  [Save Changes]                                   │
└───────────────────────────────────────────────────┘
```

- User adjusts within defined min/max ranges
- Changes write to `Config/risk_config.json` under their user workspace
- Orchestrator loads config at each cycle start (always fresh)
- User has NO access to: agent prompts, system code, database, API keys of other users

### Schedule Visibility

```
┌─ Agent Schedule ────────────────────────────────┐
│                                                   │
│  Trading Cycle:   Every 15 min  [Active]         │
│  Trade Monitor:   Every 5 min   [Idle - no trades]│
│  News Refresh:    Every 15 min  [Active]         │
│  Weather Check:   Every 30 min  [Active]         │
│  Cross Rates:     Every 1 hour  [Active]         │
│  Oil/Energy:      Every 4 hours [Active]         │
│  Macro Refresh:   Daily 6:00 AM [Next: 18h]     │
│  Daily Summary:   Daily 5:30 PM [Next: 7h]      │
│                                                   │
│  Current Session: London-NY Overlap (Best)        │
│  Next Session:    New York (in 0h)                │
│                                                   │
└───────────────────────────────────────────────────┘
```

Read-only. User sees timing but can't change it (timing is derived from pair selection).

---

## 9. API BUDGET — DYNAMIC BY PAIR COUNT

API usage scales with how many pairs are selected.

### Wolfram Budget (2,000/month ≈ 67/day)

| Job | Queries/Pair | ×Pairs | ×Frequency | Daily Total |
|-----|-------------|--------|-----------|-------------|
| Daily macro | 4-6 | per currency (deduplicated) | 1/day | 8-16 |
| Weekly macro | 2-3 | per currency | 1/week ÷ 5 | 2-3 |
| Oil/energy | 1 | 1 (shared) | 3/day | 3 |
| Cross rates | 2 | 1 (shared) | 12/day | 24 |
| Kelly calc | 1 | per trade signal | ~4/day | 4 |
| Correlation | 1 | per new trade | ~2/day | 2 |
| **Total** | | | | **~45-50/day** |

At 50/day × 30 = 1,500/month. Within 2,000 budget.

**Key optimization:** Currency queries are deduplicated. Trading EUR_USD + GBP_USD = 3 currencies (USD, EUR, GBP), not 4 queries × 2 pairs. The cache key is per-currency, so USD rate is fetched once and used by both pairs.

### News Budget (100/day estimated)

| Job | Queries/Pair | ×Pairs | ×Frequency | Daily Total |
|-----|-------------|--------|-----------|-------------|
| News scan | 2-4 keywords | per pair | 96/day (every 15 min) | High if uncached |
| **With caching** | 2-4 | per pair | ~6/day (15-min TTL, cache hits) | **12-24** |

### OANDA Budget (100 req/sec — virtually unlimited)

| Job | Calls/Cycle | ×Cycles/Day | Daily Total |
|-----|------------|-------------|-------------|
| Candles (3 TF) | 3 | 96 | 288 |
| Pricing | 1 | 96 | 96 |
| Account | 1 | 96 | 96 |
| Trade monitor | 3 | 288 (5-min) | 864 |
| **Total** | | | **~1,344/day** |

At 100/sec, this is 13 seconds worth. No concern.

---

## 10. WHEN PAIRS CHANGE — UPDATE PROPAGATION

When the user selects/deselects pairs through the dashboard:

```
User clicks [Apply Changes]
    │
    ▼
1. Update Config/pair_schedule.json
    │
    ▼
2. Intelligence agent receives new pair config
    ├── New currencies → add to daily macro refresh list
    ├── New news keywords → update search queries
    ├── New weather locations → add/remove weather checks
    └── Deduplicate: if USD already tracked for EUR_USD, don't add again for GBP_USD
    │
    ▼
3. Orchestrator receives new pair rotation
    ├── Update session priority matrix
    ├── Update correlation tracking (new pair combos)
    └── Recalculate round-robin schedule
    │
    ▼
4. Technical analyst receives new instruments
    └── Will analyze these pairs when cycled by orchestrator
    │
    ▼
5. Validator receives new pair config
    └── Loads DB evidence for new pairs (backtest_setup_performance)
    │
    ▼
6. Execution/trade_monitor: no change needed
    └── They work with whatever pair the orchestrator tells them
    │
    ▼
7. Reporter: no change needed
    └── Logs whatever instruments come through
```

The key: intelligence and orchestrator need the pair config. Everyone else just follows orders.

---

## 11. TODO — TIMING INVESTIGATION PER AGENT

### oanda_data
- [ ] Implement pre-candle tick fetch (5 sec before close) for freshest data
- [ ] Test M15 candle alignment — verify OANDA returns complete candles at :00/:15/:30/:45
- [ ] Measure candle fetch latency for 3 timeframes (target: < 3 seconds total)
- [ ] Handle H4 candle alignment (which hours align with H4 close for OANDA?)

### intelligence
- [ ] Build `pair_schedule.json` generator — takes selected pairs, outputs per-pair query config
- [ ] Implement 8-day forward calendar query (News MCP)
- [ ] Implement lookback reference check — "is cached data still valid given recent events?"
- [ ] Test Wolfram deduplication — same currency queried once regardless of pair count
- [ ] Implement weekly macro refresh (GDP, trade balance) with 7-day TTL
- [ ] Add "last changed" and "next event" tracking to macro cache entries
- [ ] Build pre-market Sunday scan (weekend news + week-ahead calendar)
- [ ] Test all 8 currency query phrasings against Wolfram (some fail — need fallbacks)
- [ ] Measure actual API response times per source (Wolfram, News, Weather)

### technical_analyst
- [ ] Test H1/H4 candle close detection — is the cycle at :00 getting a complete H1?
- [ ] Implement "quick regime check" (lighter than full analysis) for on-demand orchestrator calls
- [ ] Weight :00 cycles higher (H1 just closed — more data significance)
- [ ] Weight H4-aligned cycles highest (H4 close — strongest trend signal)
- [ ] Test pre-candle pattern detection feasibility (10 sec before close)

### validator
- [ ] Build "quick DB check" mode (no LLM, just historical evidence) for mid-trade checks
- [ ] Test DB query performance for new pairs (do all 13 have backtest data?)
- [ ] Measure full validation pipeline latency (target: < 5 seconds)

### execution
- [ ] Measure order placement latency (OANDA API → fill confirmation)
- [ ] Test partial close timing — does OANDA process partial closes instantly?
- [ ] Implement order tracking — map cycle_id → OANDA trade_id for audit

### trade_monitor
- [ ] Implement activation/deactivation cron (orchestrator controls)
- [ ] Implement Friday close 2-min override
- [ ] Test news proximity check from cache (does 5-min interval catch 15-min news events?)
- [ ] Measure per-check API latency (target: < 2 seconds for full check)

### reporter
- [ ] Implement end-of-day summary trigger (5:30 PM ET cron)
- [ ] Implement weekly summary trigger (Fri 6:00 PM ET cron)
- [ ] Build intelligence accuracy tracking (did verdict match outcome?)
- [ ] Test drift detection formula with real backtest vs live data

### cycle_orchestrator
- [ ] Implement pair rotation with session-based priority
- [ ] Implement session awareness in decision confidence (best window = higher confidence)
- [ ] Build the `pair_schedule.json` → agent config propagation pipeline
- [ ] Replace `make_trade_decision()` with LLM-powered reasoning
- [ ] Implement user command handling ("what's happening?", "close EUR_USD", "be more aggressive")
- [ ] Test full cycle end-to-end with real OANDA data (paper account)

### System / Infrastructure
- [ ] Build `Config/pair_schedule.json` — dynamic per-pair config
- [ ] Build scheduler process (checks jobs every 30 sec, runs what's due)
- [ ] Build pair change propagation (dashboard → config → agents)
- [ ] Build dashboard pair selector UI (read existing `TODO-dashboard-multiuser.md`)
- [ ] Build dashboard risk settings UI with validation ranges
- [ ] Build API key management (tokenized storage per user)
- [ ] Test full day simulation — 96 cycles with all jobs firing
