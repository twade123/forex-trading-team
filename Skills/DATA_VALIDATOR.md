# Data Validator — Complete Tool Reference

> **MCP Handler:** `handler_data_validator` (DataValidatorHandler)
> **Database:** `~/jarvis/Database/trevor_database.db` (backtest_setup_performance: 39,692 rows from 8.5M trades)
> **Python modules:** `trade_validator.py` (heuristic gates), `decision_logger.py` (4-step pipeline), `validation_analyst.py` (LLM analysis)
> **LLM:** Claude Sonnet via Anthropic SDK (lazy-loaded, graceful degradation if unavailable)

---

## 1. PRIMARY ACTIONS

### 1.1 `evaluate_trade` ⭐ MAIN ENTRY POINT

Runs the full 4-step decision pipeline and logs to `trade_decisions` table. This is the single call the orchestrator makes for trade validation.

**Parameters:**
```json
{
  "pair": "EUR_USD",           // required
  "timeframe": "H1",          // required
  "setup": "S15",             // required — setup ID or "S15_rr2.0_sl2.5" with params
  "direction": "buy",         // required — "buy" or "sell"
  "regime": "ranging",        // required — from ADX classification
  "indicators": {},           // optional — full indicator snapshot
  "h4_agrees": true,          // optional — from alignment
  "session": "London",        // optional — "Asian", "London", "NY_Overlap", "NY"
  "candles": [],              // optional — raw H1 candles for Gate 1
  "market_data": {},          // optional — pricing, spread, concurrent setups
  "news_data": {},            // optional — intelligence agent output
  "weather_data": {},         // optional — weather severity
  "wolfram_data": {},         // optional — macro data
  "confluence_output": {}     // optional — confluence scorer output
}
```

**Pipeline steps:**
1. **Gate 1 — Data Integrity** (TradeValidator heuristic): validates candles OHLC consistency, indicator completeness, required fields
2. **Gate 2 — Pre-Trade Check** (TradeValidator heuristic): confluence ≥ threshold, R:R ratio, daily loss limit, max concurrent trades, high-impact news proximity, 13 contradiction rules
3. **Backtest Evidence** (TradingDB query): queries `backtest_setup_performance` for this setup+pair+regime combo, gets win rate, profit factor, trade count, best parameters, loss patterns, H4 impact, session impact
4. **Final Verdict**: combines all evidence. Gate failures override. News/weather can downgrade APPROVE→CAUTION.

**Response:**
```json
{
  "decision_id": "dec_123",
  "verdict": "APPROVE",              // "APPROVE" | "CAUTION" | "REJECT"
  "confidence": 0.85,               // 0.0 - 1.0
  "recommended_action": "EXECUTE",   // "EXECUTE" | "REDUCE" | "SKIP"
  "recommended_params": {            // best historical parameters
    "rr_mult": 2.0,
    "sl_mult": 2.5,
    "threshold": 14
  },
  "warnings": ["High-impact news in 2 hours"],
  "loss_patterns": [
    {"indicator": "rsi", "range_low": 45, "range_high": 55, "loss_rate": 0.65}
  ],
  "confluence": null,                // multi-setup confluence if applicable
  "pipeline_steps": {
    "step1_data_integrity": {"passed": true, "confidence": 1.0, "issues": [], "elapsed_ms": 3.2},
    "step2_pre_trade": {"passed": true, "confidence": 1.0, "issues": [], "needs_llm_escalation": false},
    "step3_backtest_evidence": {
      "verdict": "APPROVE",
      "confidence": 0.85,
      "historical_stats": {
        "total_trades": 847,
        "total_wins": 813,
        "overall_win_rate": 96.0,
        "best_setup": "S15_rr2.0_sl2.5",
        "best_win_rate": 96.0,
        "best_profit_factor": 1.56
      },
      "best_params": {"rr_mult": 2.0, "sl_mult": 2.5, "threshold": 14},
      "h4_impact": {"h4_agrees_wr": 97.2, "h4_disagrees_wr": 91.5, "edge": 5.7},
      "session_impact": {"best_session": "London", "worst_session": "Asian"}
    },
    "step4_final": {"verdict": "APPROVE", "confidence": 0.85}
  },
  "execution_time_ms": 212
}
```

### 1.2 `validate_trade_pipeline` ⭐ FULL PIPELINE WITH LLM

Same as `evaluate_trade` but ALWAYS runs the LLM after heuristics. The LLM gets:
- Heuristic results (Gate 1 + Gate 2 + contradictions)
- Historical knowledge (KnowledgeStore patterns, win rates, learned parameters)
- Winning trade examples (positive_patterns.json from backtesting)
- Instrument profile (spread, ATR, volatility, session behavior)
- Recent trade snapshots (chart state from recent validated signals)
- Current market data (all raw indicators, candles, patterns, news)

**Response adds:**
```json
{
  "heuristic": {"overall_passed": true, "gate_1": {...}, "gate_2": {...}},
  "llm_judgment": {
    "passed": true,
    "confidence": 0.88,
    "recommendation": "proceed",
    "reasoning": "S15 divergence in ranging matches 96% historical win rate...",
    "heuristic_agreement": true,
    "historical_match": "Strong — matches top-performing setup pattern",
    "data_quality_note": "All indicators complete, spread normal",
    "issues_found": []
  },
  "final_passed": true,
  "final_confidence": 0.88,
  "llm_recommendation": "proceed",
  "llm_reasoning": "..."
}
```

### 1.3 `check_positions` ⭐ POSITION MANAGEMENT

Runs all 12 exit rules against open positions.

**Parameters:**
```json
{
  "positions": [
    {
      "trade_id": "12345",
      "instrument": "EUR_USD",
      "direction": "buy",
      "entry_price": 1.0488,
      "current_price": 1.0502,
      "stop_loss": 1.0465,
      "take_profit": 1.0520,
      "unrealized_pl": 14.0,
      "entry_time": "2026-02-17T10:30:00Z",
      "atr": 0.00085,
      "current_regime": "ranging",
      "current_spread": 1.2
    }
  ],
  "market_state": {
    "prices": {},
    "regimes": {},
    "sessions": {},
    "upcoming_news": [],
    "candle_data": {}
  }
}
```

**Response:**
```json
{
  "actions": [
    {
      "trade_id": "12345",
      "action": "TIGHTEN_SL",
      "reason": "Trade at 1.6× initial risk — tighten to breakeven",
      "rule": "trailing_stop",
      "urgency": "next_check",
      "new_sl": 1.0488,
      "close_fraction": null,
      "details": {}
    }
  ],
  "position_count": 1,
  "actions_count": 1
}
```

**12 Exit Rules:**
1. **TP hit** — take profit price reached
2. **SL hit** — stop loss price reached
3. **Trailing stop** — activated after 1:1 RR, trails at 1.5×ATR
4. **Partial exit** — close 50% at 1:1 RR, move SL to breakeven
5. **Max hold time** — 48 hours max per trade
6. **Regime change** — market regime shifted from entry regime
7. **News event** — HIGH impact event within 30 minutes
8. **Correlation breach** — correlated position opened
9. **Performance drift** — live results significantly worse than backtest
10. **Session end** — close before illiquid session if not in profit
11. **Spread widening** — spread > 3× normal
12. **Manual override** — operator command

---

## 2. BACKTEST DATABASE QUERIES

These query `backtest_setup_performance` (39,692 rows, 308 patterns per pair for EUR_USD).

### 2.1 `validate_trade_setup`

Query historical performance for a specific setup+pair+regime.

**Parameters:**
```json
{
  "pair": "EUR_USD",
  "regime": "ranging",
  "setup": "S15",
  "direction": "buy",       // optional
  "indicators": {},          // optional — checks against loss patterns
  "h4_agrees": true,         // optional — queries H4 impact
  "session": "London"        // optional — queries session impact
}
```

**Response:**
```json
{
  "verdict": "APPROVE",
  "confidence": 0.85,
  "historical_stats": {
    "total_trades": 847,
    "total_wins": 813,
    "overall_win_rate": 96.0,
    "total_pips": 1234.5,
    "param_variants_tested": 12,
    "viable_variants": 8,
    "best_setup": "S15_rr2.0_sl2.5",
    "best_win_rate": 96.0,
    "best_profit_factor": 1.56,
    "best_trade_count": 214,
    "best_total_pips": 456.7
  },
  "best_params": {"rr_mult": 2.0, "sl_mult": 2.5, "threshold": 14},
  "h4_impact": {
    "h4_agrees_wr": 97.2,
    "h4_disagrees_wr": 91.5,
    "edge": 5.7
  },
  "session_impact": {
    "best_session": "London",
    "worst_session": "Asian"
  },
  "warnings": [],
  "loss_patterns": [...]
}
```

**Verdict logic:**
- `total_trades < 10` → REJECT (insufficient data)
- `win_rate < 50%` → REJECT
- `win_rate 50-60%` AND `profit_factor < 1.0` → REJECT
- `win_rate 60-75%` → CAUTION
- `win_rate > 75%` AND `profit_factor > 1.0` → APPROVE
- `win_rate > 90%` AND `trade_count > 50` → APPROVE (high confidence)

### 2.2 `get_loss_patterns`

Find common indicator ranges where a setup loses.

**Parameters:** `{pair, setup, regime, limit}`

**Response:**
```json
{
  "patterns": [
    {"indicator": "rsi", "range_low": 45, "range_high": 55, "loss_rate": 0.65, "trade_count": 32},
    {"indicator": "adx", "range_low": 20, "range_high": 25, "loss_rate": 0.58, "trade_count": 48}
  ]
}
```

### 2.3 `check_confluence`

Check how concurrent setups affect win rate.

**Parameters:** `{pair, setups_firing: ["S1", "S15"], regime}`

**Response:**
```json
{
  "combined_win_rate": 97.5,
  "individual_rates": {"S1": 78.0, "S15": 96.0},
  "synergy_score": 1.5,
  "trade_count": 23
}
```

### 2.4 `get_best_params`

Get optimal TP/SL/threshold for a setup+pair+regime.

**Parameters:** `{pair, regime, base_setup, min_trades}`

**Response:**
```json
{
  "best_params": [
    {"setup": "S15_rr2.0_sl2.5", "win_rate": 96.0, "profit_factor": 1.56, "trade_count": 214},
    {"setup": "S15_rr1.5_sl2.0", "win_rate": 93.2, "profit_factor": 1.42, "trade_count": 198}
  ]
}
```

### 2.5 `check_performance_drift`

Compare recent live performance to backtest baseline.

**Parameters:** `{pair, setup, regime}`

**Response:**
```json
{
  "live_win_rate": 88.0,
  "backtest_win_rate": 96.0,
  "drift": -8.0,
  "significant": true,
  "recommendation": "REDUCE size — live performance below backtest by 8pp"
}
```

---

## 3. HEURISTIC VALIDATION (TradeValidator)

### 3.1 Gate 1 — Data Integrity

Validates 8 data types against YAML rules (`Config/validation_rules.yaml`):

| Data Type | Required Keys | Checks |
|-----------|--------------|--------|
| candles | time, mid (o/h/l/c), volume, complete | OHLC consistency, timestamp continuity |
| indicators_core | emas, rsi, macd, bollinger, atr | All required keys present, values numeric |
| indicators_advanced | adx, stochastic, volume_sma | Keys present |
| candlestick_patterns | detected_count, filtered_patterns | Array structure |
| chart_patterns | patterns, reversal_patterns, continuation_patterns | Array structure |
| alignment | alignment, per_timeframe | Structure check |
| news_intelligence | sentiment, events | Structure check |
| intelligence_aggregator | overall_sentiment, recommendation | Structure check |

**Consecutive failure tracking:** 3+ failures in a row triggers CRITICAL alert (possible data feed issue).

### 3.2 Gate 2 — Pre-Trade Checks

10 checks + 13 contradiction rules:

**Checks:**
1. Action = hold → auto-pass
2. Confluence score ≥ min_confluence (default 70)
3. RSI gate passed
4. Stop loss is numeric
5. R:R ratio ≥ min_rr_ratio (default 2.0)
6. Daily loss < max_daily_loss_pct (default 5.0%)
7. Open trades < max_concurrent_trades (default 3)
8. No high-impact event within 30 minutes
9. `tradeable` flag is True
10. Run 13 contradiction rules

**13 Contradiction Rules:**

| # | Severity | Rule |
|---|----------|------|
| 1 | Warning | Buying into overbought RSI (>70) |
| 2 | Warning | Selling into oversold RSI (<30) |
| 3 | Warning | EMA200 trend vs MACD crossover disagree |
| 4 | Warning | ADX trending (>25) but Bollinger squeeze |
| 5 | Warning | Bullish divergence with RSI ≥ 50 |
| 6 | Warning | Bearish divergence with RSI ≤ 50 |
| 7 | Warning | Price at upper BB but RSI oversold |
| 8 | Warning | Price at lower BB but RSI overbought |
| 9 | Warning | Stochastic and RSI disagree on extremes |
| 10 | Warning | Price above VWAP but bearish signal |
| 11 | Critical | H4 direction opposes M15 direction |
| 12 | Critical | High score (>70) with 3+ warning contradictions |
| 13 | Critical | Trading signal during closed market |

### 3.3 Gate 3 — Historical Performance (in full pipeline)

Adjusts confidence based on backtest performance:
- Strategy win rate < 40% → confidence × 0.8
- Profit factor < 1.0 → confidence × 0.7
- Instrument-specific win rate < 35% → additional warning

---

## 4. LLM VALIDATION

### 4.1 `_validate_trading_with_llm` (internal)

Called by `validate_trade_pipeline` and individual validation handlers. Assembles full context and sends to Claude.

**Context assembled (`_assemble_trading_context`):**
1. **Knowledge Store** — per-instrument patterns, win rates, learned parameters from `knowledge_store.py`
2. **Positive Patterns KB** — winning trade examples from `Trading Bot/Data/{instrument}/knowledge_base/positive_patterns.json`
3. **Recent Trade Snapshots** — chart state from `Trading Bot/Data/{instrument}/snapshots/` (last 3 days, 5 per day)
4. **Instrument Profile** — from `Trading Bot/Data/{instrument}/profile.json` (spread, ATR, volatility, session data)

**LLM receives:**
- Validation type (data_integrity / pre_trade / full_pipeline / contradiction_analysis)
- Heuristic engine results (gates, contradictions, confidence)
- Historical knowledge from Knowledge Store
- Winning trade examples
- Instrument profile
- Recent trade snapshots (outcomes if available)
- Current market data (indicators, candles, patterns, news — truncated for context window)

**LLM returns:**
```json
{
  "passed": true,
  "confidence": 0.88,
  "recommendation": "proceed",
  "reasoning": "S15 divergence in ranging regime matches historical pattern...",
  "heuristic_agreement": true,
  "historical_match": "Strong",
  "data_quality_note": "All indicators complete",
  "issues_found": []
}
```

**Graceful degradation:** If Anthropic SDK unavailable or API call fails, falls back to heuristic result.

### 4.2 `ValidationAnalyst` (validation_analyst.py)

Separate LLM analysis module with three modes:

**On-demand** (`analyze_on_demand`): Called for gray-zone decisions. Receives trade decision + indicators + contradictions. Returns proceed/hold/reduce_size with reasoning.

**Hourly** (`analyze_hourly`): Batch analysis of accumulated validation results. Looks for systematic issues, regime changes, parameter drift.

**Daily** (`analyze_daily`): Parameter effectiveness review with tuning recommendations.

### 4.3 Hourly Batch Analysis (`run_trade_hourly_analysis`)

Accumulated heuristic+LLM results are batch-analyzed hourly:
- Systematic data quality issues (repeated failures in same type)
- Regime changes (heuristics becoming less reliable)
- Parameter drift (thresholds needing adjustment)
- LLM disagreement patterns (when LLM overrides heuristics, was it right?)

---

## 5. LOGGING ACTIONS

### 5.1 `log_decision`
Log a trade decision to `trade_decisions` table.
```json
{"pair": "EUR_USD", "timeframe": "H1", "setup": "S15", "direction": "buy",
 "regime": "ranging", "verdict": "APPROVE", "confidence": 0.85,
 "reasoning": "...", "db_evidence": {...}}
```

### 5.2 `log_live_trade`
Log a completed trade to `live_trades` table (67 columns matching backtest schema).
```json
{"decision_id": "dec_123", "instrument": "EUR_USD", "direction": "buy",
 "entry_price": 1.0488, "exit_price": 1.0502, "pips": 14.0, "result": "win", ...}
```

### 5.3 `get_upcoming_news`
Check for high-impact economic events.
```json
{"currencies": ["EUR", "USD"], "hours_ahead": 24}
```

---

## 6. GENERAL VALIDATION

### 6.1 `validate_trade_data`
Gate 1 heuristics → LLM judgment on any data payload.

### 6.2 `validate_pre_trade`
Gate 2 heuristics → LLM judgment on trade decision.

### 6.3 `detect_contradictions`
13-rule heuristic detection → LLM interpretation of contradictions in market context.

### 6.4 `get_trade_metrics`
TradeValidator performance metrics + LLM validation stats.

---

## 7. CONFIGURATION

### validation_rules.yaml
Located at `Trading Bot/Config/validation_rules.yaml`. Defines schema, range, enum, and cross-field rules for all data types.

### risk_config.json
Located at `Trading Bot/Config/risk_config.json`. Defines:
- `min_confluence`: 70 (trade threshold)
- `min_rr_ratio`: 1.5
- `max_daily_loss_pct`: 3.0
- `max_concurrent_trades`: 3
- Plus: data_collection, trading_hours, instruments, position_management, kelly_sizing

### LLM Model
Default: `claude-sonnet-4-5-20250929` (ValidationAnalyst)
Handler uses `_call_anthropic()` with lazy Anthropic client.

---

## 8. WATCH MANIFEST TOOLS

These three query methods power the validator's `watch_manifest.trigger_conditions[].progress_pct` fields. The validator calls them when issuing a WATCH to populate evidence-based estimates rather than guessing.

### 8.1 `query_watch_trajectory(setup_name, pair, current_conditions)`

Queries `backtest_setup_performance` for how often this setup successfully triggered from conditions that look like the current partial state.

**Parameters:**
```json
{
  "setup_name": "S15",
  "pair": "EUR_USD",
  "current_conditions": {
    "ema_cross": true,
    "fan_opening": true,
    "bb_expanding": false,
    "momentum_candles": false,
    "checklist_score": 5
  }
}
```

**Response:**
```json
{
  "trigger_rate_from_here": 0.68,
  "median_candles_to_trigger": 4,
  "typical_progress_pct_per_field": {
    "bb_expanding": 35,
    "momentum_candles": 20
  },
  "sample_size": 143
}
```

Use `typical_progress_pct_per_field` to populate `progress_pct` on each `trigger_conditions` entry. This gives the validator calibrated estimates instead of manual guesses.

### 8.2 `get_typical_candle_countdown(setup_name, pair)`

Returns the average number of M15 bars from "partial setup conditions met" to "full trigger" across the 8.4M trade backtest. Feeds `watch_manifest.trajectory_assessment.expected_trigger_candles`.

**Parameters:** `{setup_name, pair, regime, session}`

**Response:**
```json
{
  "median_candles": 5,
  "p25_candles": 3,
  "p75_candles": 9,
  "note": "Expansion setups typically take longer than sniper setups"
}
```

Use `median_candles` as the default `expected_trigger_candles`. Use `p75_candles` as `time_limit_candles` for expansion setups.

### 8.3 `get_watch_invalidation_rate(setup_name, pair)`

Returns what percentage of WATCHes on this setup historically became CONFIRMs vs REJECTs. Helps calibrate `time_limit_candles` — if 80% of WATCHes trigger within 5 candles, set limit to 6. If only 40% trigger within 10 candles, the setup is unreliable and the default limit is fine.

**Parameters:** `{setup_name, pair, regime}`

**Response:**
```json
{
  "watch_to_confirm_pct": 0.58,
  "watch_to_reject_pct": 0.42,
  "median_confirm_candles": 4,
  "median_reject_candles": 9,
  "recommended_time_limit": 7
}
```

The validator uses `recommended_time_limit` directly as `fishing_line.time_limit_candles` when available.
