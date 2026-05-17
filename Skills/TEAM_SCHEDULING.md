# Team Scheduling — Cron Jobs, Market Hours & Cache Strategy

> **Config file:** `Config/schedule_config.json`
> **Cache store:** `IntelligenceStore` (`Source/intelligence_store.py`)
> **Market hours:** Sunday 5:00 PM ET → Friday 5:00 PM ET

---

## 1. MARKET HOURS

Forex trades 24/5. The bot respects open/close and trading sessions.

### Weekly Schedule (all times ET)

```
Sunday    17:00  Market opens (skip first 30 min — low liquidity, gapping)
Sunday    17:30  Bot starts accepting trades
Monday    ────── Normal trading ──────
Tuesday   ────── Normal trading ──────
Wednesday ────── Normal trading ──────
Thursday  ────── Normal trading ──────
Friday    15:00  Close warning (2h before close) — no new trades, manage exits
Friday    17:00  Market closes — all jobs stop
Saturday  ────── Market closed ──────
```

### Four Sessions (ET)

| Session | Open ET | Close ET | Key Pairs | Character |
|---------|---------|----------|-----------|-----------|
| Sydney | 17:00 | 02:00 | AUD, NZD | Low volume, range-bound |
| Tokyo | 19:00 | 04:00 | JPY pairs | Moderate, yen-driven |
| London | 03:00 | 12:00 | EUR, GBP, CHF | High volume, trends start |
| New York | 08:00 | 17:00 | EUR/USD, USD/CAD, USD/JPY | Highest volume |
| **London-NY overlap** | **08:00** | **12:00** | **All majors** | **Best window — tightest spreads, strongest moves** |

### Market Hours Check Logic

Every job checks market hours before running:

```python
def is_market_open():
    now = datetime.now(ET)
    day = now.strftime("%A").lower()
    
    if day == "saturday":
        return False
    if day == "sunday" and now.hour < 17:
        return False
    if day == "friday" and now.hour >= 17:
        return False
    
    # Skip first 30 min after open
    if day == "sunday" and now.hour == 17 and now.minute < 30:
        return False
    
    return True

def is_close_warning():
    """True if within 2 hours of Friday close."""
    now = datetime.now(ET)
    if now.strftime("%A").lower() == "friday" and now.hour >= 15:
        return True
    return False

def current_session():
    """Returns active session(s) — can be multiple during overlaps."""
    ...
```

---

## 2. SCHEDULED JOBS — THE FULL TEAM

### Job Overview

| Job | Interval | Active When | Agent(s) | Purpose |
|-----|----------|-------------|----------|---------|
| **Trading Cycle** | 15 min | Market hours | All 8 | Full analysis → decision → execute |
| **Trade Monitor** | 5 min | Trades open | trade_monitor | Check positions, spreads, news proximity |
| **News Scan** | 15 min | Market hours | intelligence | Refresh news cache |
| **Weather Check** | 30 min | Market hours | intelligence | Refresh weather cache |
| **Cross Rates** | 1 hour | Market hours | intelligence | DXY, gold via Wolfram |
| **Oil/Energy** | 4 hours | Market hours | intelligence | Crude oil via Wolfram |
| **Macro Refresh** | Daily | 06:00 ET | intelligence | All interest rates, inflation, GDP, employment |
| **Knowledge Maintenance** | Daily | 05:00 ET | system | Purge cache, refresh stats, update bad sessions |
| **Daily Summary** | Daily | 17:30 ET (Mon-Fri) | reporter | Day's P&L, trades, lessons learned |
| **Weekly Summary** | Weekly | 18:00 ET (Fri) | reporter | Weekly performance review |

### Timeline — Typical Trading Day (ET)

```
05:00  Knowledge maintenance runs (purge, refresh stats)
06:00  Macro refresh (intelligence fetches rates, inflation, GDP for all 8 currencies)
       → Cache warm for the day (TTL: 24h)

08:00  London-NY overlap starts — best trading window
08:00  Trading cycle #1 of the day (cycles run every 15 min)
       → oanda_data fetches candles
       → intelligence reads macro from cache (warm), fetches fresh news (15-min TTL)
       → technical_analyst runs indicators + patterns
       → validator checks evidence
       → orchestrator decides
       → execution places trade (if signal)
       → trade_monitor starts (if trade opened)

08:05  Trade monitor check #1 (if trades open)
08:10  Trade monitor check #2
08:15  Trading cycle #2 + news refresh (news cache expired after 15 min)
       → intelligence fetches new news, reads macro from cache (still warm)
       → technical_analyst runs on new M15 candle

...repeat every 15 min...

10:00  Cross rates refresh (DXY, gold — 1h TTL)
12:00  London-NY overlap ends. Oil/energy refresh (4h TTL)

15:00  Friday close warning — no new trades opened
17:00  Friday market close — all periodic jobs stop
17:30  Daily summary (reporter)
18:00  Weekly summary (reporter, Fridays only)
```

---

## 3. CACHE STRATEGY — WHO WRITES, WHO READS

### The Rule

**Only the intelligence agent writes to cache. Everyone else reads.**

This prevents cache conflicts and ensures data consistency. The intelligence agent is the single source of truth for all external data.

### Cache Flow

```
External APIs                Intelligence Agent              Other Agents
─────────────               ──────────────────              ────────────
Wolfram MCP   ───────────►  gather_intelligence()  ──────►  IntelligenceStore
News MCP      ───────────►       │                           (SQLite cache)
Weather MCP   ───────────►       │                               │
                                 │                               ▼
                                 │                          get_cached()
                                 │                               │
                                 │                    ┌──────────┼──────────┐
                                 │                    ▼          ▼          ▼
                                 │               validator  orchestrator  trade_monitor
                                 │
                                 ▼
                            set_cached(key, category, data, ttl)
```

### Cache Categories & TTLs

| Category | TTL | Writer | Readers | Refresh Job |
|----------|-----|--------|---------|-------------|
| `wolfram_macro` | 24h (1440 min) | intelligence | validator, orchestrator | Daily 06:00 ET |
| `wolfram_stats` | 0 (no cache) | intelligence | validator | Per-cycle (always fresh) |
| `news` | 15 min | intelligence | trade_monitor, validator, orchestrator | Every 15 min |
| `weather` | 30 min | intelligence | validator | Every 30 min |
| `oil_energy` | 4h (240 min) | intelligence | orchestrator | Every 4h |
| `cross_rates` | 1h (60 min) | intelligence | orchestrator | Every 1h |

### How It Works In Practice

**Trading cycle starts:**
1. Intelligence agent calls `gather_intelligence(instrument)` 
2. For macro data: `get_cached("wolfram:rate:USD")` → **cache hit** (refreshed at 06:00) → no API call
3. For news: `get_cached("news:EUR_USD")` → if < 15 min old → **cache hit** → no API call. If expired → fetches fresh from News MCP → `set_cached()`
4. For weather: same pattern, 30 min TTL
5. Returns combined intelligence report to orchestrator

**Trade monitor check (between cycles):**
1. Needs to know if news is approaching → `get_cached("news:EUR_USD")` → reads what intelligence last wrote
2. Does NOT call News MCP itself — reads from cache only
3. If cache is expired (shouldn't be if news refresh runs on time), flags `news_data_stale` in report

**Why this matters:**
- No duplicate API calls (two agents hitting Wolfram for the same data)
- Consistent data across the team (everyone sees the same interest rates)
- Predictable API usage (you know exactly how many Wolfram/News calls per day)

---

## 4. INTELLIGENCE REFRESH JOBS — DETAIL

### 4.1 Macro Refresh (Daily, 06:00 ET)

Runs once before trading starts. Populates the entire macro picture.

**Per currency (8 currencies = 8 × 4 = 32 Wolfram queries):**

| Query Template | Cache Key | Example |
|---------------|-----------|---------|
| `{country} interest rate` | `wolfram:rate:{ccy}` | `wolfram:rate:USD` |
| `{country} inflation rate` | `wolfram:inflation:{ccy}` | `wolfram:inflation:EUR` |
| `{country} unemployment rate` | `wolfram:unemployment:{ccy}` | `wolfram:unemployment:GBP` |
| `{country} GDP` | `wolfram:gdp:{ccy}` | `wolfram:gdp:JPY` |

**Country mapping:**
```json
{"USD": "US", "EUR": "eurozone", "GBP": "UK", "JPY": "Japan",
 "AUD": "Australia", "NZD": "New Zealand", "CAD": "Canada", "CHF": "Switzerland"}
```

**After refresh, these cache keys are warm for 24h.** Every trading cycle that day reads from cache — zero Wolfram calls during cycles for macro data.

### 4.2 News Scan (Every 15 min)

Matches the cycle interval. Intelligence fetches news for all instruments in the active trading list (`Config/risk_config.json → instruments`).

- Runs during market hours only
- Each instrument gets its own cache key: `news:EUR_USD`, `news:GBP_USD`, etc.
- 15-min TTL means trade_monitor always has news data < 15 min old

### 4.3 Weather Check (Every 30 min)

Only matters for extreme events. Cache key per instrument: `weather:EUR_USD`.

### 4.4 Oil/Energy (Every 4h)

Single Wolfram query: `"crude oil price"`. Cache key: `wolfram:oil_price`.

Matters for: USD_CAD (Canada = oil exporter), general risk sentiment.

### 4.5 Cross Rates (Every 1h)

Two Wolfram queries: `"US dollar index"`, `"gold price"`. Cache keys: `wolfram:dxy`, `wolfram:gold`.

DXY tells you if dollar is strengthening across the board. Gold = risk-off indicator.

---

## 5. TRADE MONITOR — DETAILED FLOW

### Start Condition
Orchestrator opens a trade → enables trade_monitor cron (every 5 min)

### Each 5-Min Check

```
1. is_market_open()?
   └─ No → skip, go back to sleep

2. list_open_trades()
   └─ Empty → disable trade_monitor cron, report "no_open_trades"

3. For each open trade:
   a. get_pricing(instrument) → current bid/ask/spread
   b. Calculate: pips_in_favor, pips_to_sl, pips_to_tp, spread_status
   c. Check spread thresholds (2× warning, 4× critical)

4. list_trades(state="CLOSED", count=10) → check for SL/TP fills since last check

5. get_cached("news:{instrument}") → check for high-impact events within 30 min
   └─ Cache expired? → flag "news_data_stale" (don't fetch — that's intelligence's job)

6. get_account_summary() → margin usage, total unrealized P&L

7. Package report → send to orchestrator

8. Go back to sleep for 5 min
```

### Stop Condition
All trades closed → orchestrator disables trade_monitor cron

---

## 6. ORCHESTRATOR RESPONSES TO MONITOR ALERTS

When trade_monitor sends an alert, the orchestrator may spin up other agents:

| Alert | Orchestrator Response |
|-------|--------------------|
| `spread_critical` | → Call execution: tighten SL or close |
| `news_imminent` | → Call intelligence: get latest news detail → decide hold/close |
| `rapid_adverse_move` | → Call technical_analyst: regime check on latest candles → decide |
| `trade_closed_sl` | → Call reporter: log loss, update knowledge store |
| `trade_closed_tp` | → Call reporter: log win, update knowledge store |
| `news_data_stale` | → Call intelligence: force news refresh |
| `all_normal` | → Do nothing, wait for next cycle |

This is how the team works between cycles. Trade_monitor is the trigger, orchestrator is the brain, other agents are called on demand.

---

## 7. IMPLEMENTATION — CRON JOB SETUP

All jobs are registered in `schedule_config.json` and managed by the orchestrator (or a scheduler process). Each job follows this pattern:

```python
class ScheduledJob:
    name: str           # "trade_monitor", "news_scan", etc.
    interval: timedelta # How often
    active_when: str    # "market_hours", "trades_open", "always"
    last_run: datetime  # Track for skipping if already ran
    agent: str          # Which agent runs it
    
    def should_run(self, now: datetime) -> bool:
        if self.active_when == "market_hours" and not is_market_open():
            return False
        if self.active_when == "trades_open" and not has_open_trades():
            return False
        if now - self.last_run < self.interval:
            return False
        return True
```

### Job Registration

```python
SCHEDULED_JOBS = [
    ScheduledJob("trading_cycle",     interval=timedelta(minutes=15), active_when="market_hours",  agent="cycle_orchestrator"),
    ScheduledJob("trade_monitor",     interval=timedelta(minutes=5),  active_when="trades_open",   agent="trade_monitor"),
    ScheduledJob("news_scan",         interval=timedelta(minutes=15), active_when="market_hours",  agent="intelligence"),
    ScheduledJob("weather_check",     interval=timedelta(minutes=30), active_when="market_hours",  agent="intelligence"),
    ScheduledJob("cross_rates",       interval=timedelta(hours=1),    active_when="market_hours",  agent="intelligence"),
    ScheduledJob("oil_energy",        interval=timedelta(hours=4),    active_when="market_hours",  agent="intelligence"),
    ScheduledJob("macro_refresh",     interval=timedelta(hours=24),   run_at="06:00",              agent="intelligence"),
    ScheduledJob("knowledge_maint",   interval=timedelta(hours=24),   run_at="05:00",              agent="system"),
    ScheduledJob("daily_summary",     interval=timedelta(hours=24),   run_at="17:30",              agent="reporter"),
    ScheduledJob("weekly_summary",    interval=timedelta(weeks=1),    run_at="18:00", run_on="friday", agent="reporter"),
]
```

### Scheduler Loop

```python
async def scheduler_loop():
    """Main scheduler — runs forever, checks jobs every 30 seconds."""
    while True:
        now = datetime.now(ET)
        for job in SCHEDULED_JOBS:
            if job.should_run(now):
                await run_job(job)
                job.last_run = now
        await asyncio.sleep(30)  # Check every 30 seconds
```

---

## 8. API BUDGET — DAILY COST ESTIMATE

Knowing the schedule, we can predict daily API usage:

### Wolfram MCP
| Job | Queries/Run | Runs/Day | Total Queries |
|-----|------------|----------|---------------|
| Macro refresh | 32 | 1 | 32 |
| Oil/energy | 1 | 3 | 3 |
| Cross rates | 2 | 12 | 24 |
| Per-cycle stats | ~2 | 48 | 96 |
| **Total** | | | **~155 queries/day** |

### News MCP
| Job | Queries/Run | Runs/Day | Total Queries |
|-----|------------|----------|---------------|
| News scan | 13 (per instrument) | 48 | 624 |
| _With cache hits_ | ~3 (only expired) | 48 | ~144 |
| **Total (effective)** | | | **~144 queries/day** |

### Weather MCP
| Job | Queries/Run | Runs/Day | Total Queries |
|-----|------------|----------|---------------|
| Weather check | 13 | 24 | 312 |
| _With cache hits_ | ~3 | 24 | ~72 |
| **Total (effective)** | | | **~72 queries/day** |

### OANDA API
| Job | Calls/Run | Runs/Day | Total Calls |
|-----|-----------|----------|-------------|
| Trading cycle | ~10 | 48 | 480 |
| Trade monitor | ~5 | up to 288 | up to 1440 |
| **Total** | | | **~1920 calls/day** |

OANDA allows 100 req/sec — well within limits.
