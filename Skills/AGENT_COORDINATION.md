# Agent Coordination — How the 7 Agents Work Together

## Cycle Flow
```
┌─────────────────┐
│ cycle_orchestrator│ ← Controls everything
└────────┬────────┘
         │
    Phase 1: PRE-CHECK
         │ Check market hours, account health, existing positions
         │ Uses: OANDA (get_account_summary, list_open_trades)
         │ Uses: DATA_VALIDATOR (validate_pre_trade)
         │
    Phase 2: DATA COLLECTION
         │
    ┌────▼────┐
    │oanda_data│ → Fetch candles (H1, H4, M15), pricing, account state
    └────┬────┘   Posts: CANDLE_DATA to task thread
         │
    Phase 3: INTELLIGENCE (parallel-ready but runs sequential)
         │
    ┌────▼───────┐
    │intelligence │ → News + Weather + Wolfram (one agent, 3 MCPs)
    └────┬───────┘   Reads: currency_intelligence_map for pair-specific queries
         │           Posts: NEWS_IMPACT, WEATHER_SEVERITY, STATISTICAL_CHECKS
         │           @mentions: technical_analyst, validator
         │
    Phase 4: TECHNICAL ANALYSIS
         │
    ┌────▼──────────────┐
    │technical_analyst    │ → Indicators, candle patterns, chart patterns, confluence
    └────┬──────────────┘   Reads: oanda_data candles from thread
         │                   Reads: intelligence news/weather from thread
         │                   Reads: DATA_VALIDATOR (validate_trade_setup, get_loss_patterns)
         │                   Posts: SIGNAL with setup_id, direction, confidence, indicators
         │
    Phase 5: VALIDATION
         │
    ┌────▼─────┐
    │validator  │ → 4-step pipeline: Gate1 → Gate2 → DB evidence → verdict
    └────┬─────┘   Reads: ALL prior thread posts (data + intelligence + technical)
         │         Uses: DATA_VALIDATOR (evaluate_trade) ← main action
         │         Posts: VERDICT (APPROVE/CAUTION/REJECT) with confidence + reasoning
         │
    Phase 6: DECISION
         │
    ┌────▼────────────┐
    │cycle_orchestrator │ → Weighs technical score + validator verdict + intelligence risk
    └────┬────────────┘   Decision: TRADE / REDUCE / SKIP
         │                 If TRADE or REDUCE → Phase 7
         │                 If SKIP → Phase 8
         │
    Phase 7: EXECUTION (only if trading)
         │
    ┌────▼─────┐
    │execution  │ → Place order with SL/TP, manage existing positions
    └────┬─────┘   Uses: OANDA (place_market_order, set_trade_dependent_orders)
         │         Uses: DATA_VALIDATOR (check_positions) for exit rules
         │         Uses: WOLFRAM (Kelly criterion) for position sizing
         │         Posts: EXECUTION_RESULT with trade_id, fill price, SL/TP levels
         │
    Phase 8: REPORTING (always)
         │
    ┌────▼────┐
    │reporter  │ → Log trade, update knowledge store, generate summary
    └─────────┘   Uses: DATA_VALIDATOR (log_live_trade, log_decision)
                  Uses: knowledge_store (store_decision, update patterns)
                  Posts: CYCLE_SUMMARY to dashboard (cycle_data.json)
```

## Data Flow Between Agents

### What Each Agent WRITES to the Task Thread
| Agent | Post Type | Content |
|---|---|---|
| oanda_data | DATA_DELIVERY | {candles_h1, candles_h4, candles_m15, pricing, account_summary, spread} |
| intelligence | DATA_DELIVERY | {news_impact, weather_severity, statistical_checks, sentiment_score, overall_recommendation} |
| technical_analyst | SIGNAL | {setup_id, direction, confidence, indicators, regime, confluence_score, h4_agrees} |
| validator | VERDICT | {verdict, confidence, evidence, gate_results, reasoning, decision_id} |
| cycle_orchestrator | DECISION | {action: TRADE/REDUCE/SKIP, reasoning} |
| execution | EXECUTION_RESULT | {trade_id, fill_price, units, sl_price, tp_price, status} |
| reporter | CYCLE_SUMMARY | {phases_completed, decision, outcome, timing} |

### What Each Agent READS from the Task Thread
| Agent | Reads From | What It Needs |
|---|---|---|
| intelligence | oanda_data | Current price for context, spread |
| technical_analyst | oanda_data | All candle data for indicator calculation |
| technical_analyst | intelligence | News sentiment, upcoming events (avoid trading before HIGH impact) |
| validator | oanda_data + intelligence + technical_analyst | ALL data — validates the complete picture |
| cycle_orchestrator | validator | Verdict and confidence for final decision |
| execution | cycle_orchestrator + validator + technical_analyst | Trade direction, sizing, SL/TP levels |
| reporter | ALL | Everything — logs the complete cycle |

## MCP Assignment
| Agent | MCPs Used | Why |
|---|---|---|
| oanda_data | handler_oanda | Fetch market data, account state |
| intelligence | handler_news_info, handler_weather, handler_wolfram | 3 data sources, one agent runs them sequentially |
| technical_analyst | (none — pure Python) | Indicators, patterns all computed locally |
| validator | handler_data_validator | Historical evidence from 8.5M backtest trades |
| execution | handler_oanda, handler_data_validator | Place orders + check exit rules |
| reporter | handler_data_validator | Log trades and decisions |
| cycle_orchestrator | (none — coordination only) | Orchestrates other agents |

## Conflict Resolution
When agents disagree:
1. **technical_analyst says BUY, validator says REJECT** → REJECT wins (validator has veto)
2. **intelligence says CAUTION, technical says HIGH confidence** → REDUCE size (compromise)
3. **Multiple setups fire** → validator checks confluence, pick highest evidence one
4. **Validator CAUTION + orchestrator low confidence** → SKIP (conservative)

## Sequential Execution Rules
- One agent at a time (memory safety)
- Each agent MUST complete before next starts
- If any agent fails: log error, skip that phase, continue
- Total cycle target: <30 seconds
- If cycle exceeds 60 seconds: log warning, complete but flag for investigation
