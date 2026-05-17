# Data Validator MCP Skill — handler_data_validator

## Overview
Trade validation engine backed by 8.5M backtest trades in SQLite. Provides historical evidence for trade decisions, position management, and performance tracking.
Database: `~/jarvis/Database/trevor_database.db`

All actions dispatched via: `execute_action("handler_data_validator", {"action": "<action>", "parameters": {...}})`

---

## Actions — Full Pipeline (Use These First)

### evaluate_trade ⭐ PRIMARY
Run the complete 4-step validation pipeline and log the decision.
```
Parameters: {
  instrument: str (required, e.g. "EUR_USD"),
  direction: str (required, "BUY" | "SELL"),
  setup_id: str (required, e.g. "S15"),
  regime: str (required, e.g. "ranging", "strong_trend", "exhaustion"),
  timeframe: str (required, e.g. "H1"),
  entry_price: float (required),
  stop_loss: float (required),
  take_profit: float (required),
  indicators: dict (required, full indicator snapshot),
  confluence_score: float (required, 0-100),
  session: str (optional, "london", "new_york", "asian", "overlap"),
  news_sentiment: float (optional, -1.0 to +1.0),
  weather_severity: int (optional, 1-5),
  agent_recommendations: dict (optional, what each agent recommended)
}

Response: {
  verdict: "APPROVE" | "CAUTION" | "REJECT",
  confidence: float (0-1),
  gate1_result: {passed: bool, issues: [...]},
  gate2_result: {passed: bool, issues: [...]},
  evidence: {
    win_rate: float,
    profit_factor: float,
    trade_count: int,
    best_session: str,
    loss_patterns: [...]
  },
  reasoning: str,
  decision_id: str (logged to trade_decisions table)
}
```
**Use when:** This is the validator agent's main action. Call once per trade signal.

### check_positions ⭐ PRIMARY
Run all 12 exit rules against current open positions.
```
Parameters: {
  positions: list (required, each with: {
    trade_id: str,
    instrument: str,
    direction: str,
    entry_price: float,
    current_price: float,
    stop_loss: float,
    take_profit: float,
    unrealized_pl: float,
    entry_time: str (ISO),
    atr: float,
    current_regime: str,
    current_spread: float
  })
}

Response: {
  actions: [{
    trade_id: str,
    action: "HOLD" | "CLOSE" | "PARTIAL_EXIT" | "TIGHTEN_SL" | "MOVE_TO_BE",
    reason: str,
    rule: str (which of 12 rules triggered),
    urgency: "immediate" | "next_check" | "monitor"
  }, ...]
}
```
**Use when:** Every cycle after data collection, check all open positions.

---

## Actions — Individual Validation Steps

### validate_trade_setup
Query historical performance for a specific setup+pair+regime combo.
```
Parameters: {
  instrument: str, setup_id: str, regime: str, timeframe: str, session: str (optional)
}
Response: {
  win_rate: float, profit_factor: float, trade_count: int,
  avg_win_pips: float, avg_loss_pips: float, best_tp_atr: float, best_sl_atr: float,
  best_session: str, worst_session: str
}
```
**Use when:** Technical analyst wants to check if a detected setup has historical backing.

### get_loss_patterns
Find common indicator ranges where a setup loses.
```
Parameters: {instrument: str, setup_id: str, regime: str (optional)}
Response: {
  patterns: [{
    indicator: str, range_low: float, range_high: float,
    loss_rate: float, trade_count: int
  }, ...]
}
```
**Use when:** Suppressing signals that match known loss patterns.

### check_confluence
Check historical combined win rate when multiple setups fire together.
```
Parameters: {instrument: str, setup_ids: list[str], regime: str}
Response: {
  combined_win_rate: float, individual_rates: dict, synergy_score: float
}
```

### get_best_params
Get optimal TP/SL/threshold parameters for a setup.
```
Parameters: {instrument: str, setup_id: str, regime: str}
Response: {
  best_tp_atr: float, best_sl_atr: float, best_threshold: int,
  win_rate: float, profit_factor: float
}
```
**Use when:** Before execution, get the historically optimal entry parameters.

### check_performance_drift
Compare recent live performance to backtest baseline.
```
Parameters: {instrument: str, setup_id: str, window: int (number of recent trades)}
Response: {
  live_win_rate: float, backtest_win_rate: float, drift: float,
  significant: bool (p < 0.05), recommendation: str
}
```
**Use when:** Reporter agent tracks drift; validator checks before approval.

---

## Actions — Logging

### log_decision
Log a trade decision to trade_decisions table.
```
Parameters: {
  instrument: str, direction: str, setup_id: str, verdict: str,
  confidence: float, reasoning: str, agent_recommendations: dict,
  indicators: dict, gate1_passed: bool, gate2_passed: bool
}
Response: {decision_id: str}
```

### log_live_trade
Log a completed live trade to live_trades table (67 columns matching backtest schema).
```
Parameters: {
  decision_id: str (links to trade_decisions),
  instrument: str, direction: str, entry_price: float, exit_price: float,
  entry_time: str, exit_time: str, pips: float, result: str ("win"|"loss"|"breakeven"),
  setup_id: str, regime: str, timeframe: str,
  ... (full indicator snapshot at entry)
}
```

### get_upcoming_news
Check for upcoming high-impact economic events.
```
Parameters: {instrument: str (optional), hours_ahead: int (default 24)}
Response: {events: [{name, currency, impact, datetime, ...}, ...]}
```

---

## Actions — General Validation

### validate_trade_data
Validate a data payload against trading data quality rules.
```
Parameters: {data: dict (candle data, indicator values, etc.), rules: dict (optional custom rules)}
Response: {valid: bool, errors: [...], warnings: [...]}
```

### validate_pre_trade
Pre-trade checklist: data freshness, spread, indicators complete, session timing.
```
Parameters: {instrument: str, candles: list, indicators: dict, spread: float}
Response: {ready: bool, issues: [...]}
```

### detect_contradictions
Find contradictory signals in agent recommendations.
```
Parameters: {recommendations: dict (agent_name → recommendation)}
Response: {contradictions: [...], severity: str}
```

---

## 12 Exit Rules (check_positions)
1. **TP hit** — take profit reached
2. **SL hit** — stop loss reached
3. **Trailing stop** — activated after 1:1 RR, trails at 1.5×ATR
4. **Partial exit** — 50% at 1:1 RR, move SL to breakeven
5. **Max hold time** — 48 hours max per trade
6. **Regime change** — market regime shifted from entry regime
7. **News event** — HIGH impact event within 30 minutes
8. **Correlation breach** — correlated position opened by another system
9. **Performance drift** — live results significantly worse than backtest
10. **Session end** — close before illiquid session if not in profit
11. **Spread widening** — spread > 3× normal
12. **Manual override** — human operator command

## Coordination
- **validator** calls `evaluate_trade` — main decision pipeline
- **technical_analyst** calls `validate_trade_setup` and `get_loss_patterns` — historical context
- **execution** calls `check_positions` every cycle — position management
- **reporter** calls `log_live_trade` and `check_performance_drift` — tracking
- **orchestrator** calls `validate_pre_trade` — cycle pre-check
