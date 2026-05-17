---
name: validator-tools
description: Tool capability for the forex validator — the nine MCP tools, when to call each, mandatory calls, DO-NOT-CALL list, and error handling. Use while reasoning about a verdict when data not in the task context would make the analysis more precise.
---

# Validator Tools — How to Swing the Hammer

Your task context already includes: chart image, indicators, scout evidence, intelligence report, patterns, account state, trader annotations. Tools fetch data NOT in that context. **Call a tool when it makes your analysis PRECISE — not to re-confirm data you already have.**

## The nine tools

| Tool | When to call | Example |
|------|---|---|
| `get_live_price(pair)` | Setting a SPECIFIC snipe entry zone — want exact bid/ask/spread right now | `get_live_price(pair="EUR_USD")` → {bid, ask, spread} |
| `get_upcoming_news(currencies)` | **MANDATORY before any TRADE_NOW**. Also call before WATCH if you see a big session boundary approaching | `get_upcoming_news(currencies=["EUR","USD"])` → events in next 24h |
| `get_recent_candles(pair, count)` | Indicator values look stale or missing from context — want fresh OHLC | `get_recent_candles(pair="EUR_USD", count=10)` |
| `validate_trade_setup(pair, setup, direction)` | Check historical backtest win rate for the specific setup you've identified — evidence-based confidence boost | `validate_trade_setup(pair="NZD_USD", setup="S14", direction="sell")` → win_rate, profit_factor, trade_count |
| `get_loss_patterns(pair)` | Check what conditions have led to losses on THIS pair historically — identify reversion risk | `get_loss_patterns(pair="EUR_USD")` |
| `check_confluence(pair, setups)` | Multiple setups firing at once — check if the combination has a real edge | `check_confluence(pair="EUR_USD", setups=["S5","S14"])` |
| `get_trade_history(pair)` | Want to know if this pair has been winning or losing recently | `get_trade_history(pair="EUR_USD")` → last 10 trades |
| `get_account_summary()` | Before TRADE_NOW — check balance, open positions, correlation exposure | `get_account_summary()` |
| `wolfram_calculate(query)` | Math/stats: Fibonacci levels, correlation, regression, Kelly sizing, live macro prices | `wolfram_calculate(query="0.5900 - 0.618 * (0.5900 - 0.5780)")` → exact retracement |

## When NOT to call tools

- **WATCH and SKIP verdicts** rarely need tools — the chart + indicators tell the story. Skip tools unless you're setting a specific snipe price or checking news.
- **Indicator data already in context** — don't call `get_recent_candles` if RSI/MACD/BB values are already provided in the task.
- **Scout evidence already shows historical stats** — don't redundantly call `validate_trade_setup` if scout already gave you win_rate and PF for the same setup.

## DO NOT CALL — recursive or misleading

- **`validate_full`** — this IS the validation pipeline you're currently in. Calling it recurses. Never call it.
- **`run_full_validation`** — same issue, recursive wrapper.

## Error handling — CRITICAL

If a tool returns a result containing `"error"`, `"no such table"`, empty data, or `null`:

1. Note it briefly in your reasoning (e.g. "backtest data unavailable for this pair — proceeding with chart + scout evidence")
2. **DO NOT retry the same tool** — it will fail the same way. The data isn't there.
3. **DO NOT call the tool with different args to "fix it"** — the underlying data doesn't exist.
4. Proceed with your verdict using the context you already have.

## Mandatory tool calls

- **Before TRADE_NOW**: ALWAYS call `get_upcoming_news` to check for high-impact events within 30 minutes. High-impact in 30 min = SKIP regardless of chart.
- **Before setting a SPECIFIC snipe entry zone**: call `get_live_price` for exact bid/ask. Do not guess prices from the chart image.

Tools make your analysis PRECISE. A snipe entry based on `get_live_price` beats one estimated from the chart. A confidence boost from `validate_trade_setup` showing 75% win rate on 500+ trades is REAL evidence. Use them — with intent, not reflexively.
