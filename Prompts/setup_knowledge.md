# Trading Setups Reference — All 20 Setups (S1-S20)
## Agent Quick Reference for Setup Identification and Validation

---

## CANDLESTICK PATTERN SETUPS

### S1 — Hammer/Pin Bar Reversal
- **Entry**: Long wick rejection at support (BB lower, S/R, pivot), body ≤50% of total range
- **Confirmation**: Next candle closes above hammer high
- **Regime**: Ranging/oversold (ADX <25, RSI <30, Stochastic <20)
- **Stop**: Below hammer low | **TP**: 2:1 R/R or next resistance

### S2 — Engulfing Pattern  
- **Entry**: Current candle completely engulfs previous opposite-colored candle
- **Context**: Stronger at key S/R levels, volume confirmation preferred
- **Regime**: All regimes, best at trend exhaustion
- **Stop**: Beyond engulfed candle | **TP**: Next major S/R level

### S3 — Morning/Evening Star
- **Entry**: 3-candle reversal (large→small→large opposite direction)
- **Confirmation**: Third candle closes beyond 50% of first candle body
- **Regime**: Trend reversal at extremes
- **Stop**: Beyond pattern range | **TP**: Measured move equal to pattern height

### S4 — Doji at Extremes
- **Entry**: Open ≈ close at RSI/Stochastic extreme + confirmation candle
- **Types**: Dragonfly (bullish at support), Gravestone (bearish at resistance)  
- **Regime**: Ranging markets, trend exhaustion
- **Stop**: Beyond doji range | **TP**: Next S/R level

---

## INDICATOR-BASED SETUPS

### S5 — BB Upper + Stochastic Overbought (SELL)
- **Entry**: Price touches/exceeds BB upper + Stochastic >80 crossing down
- **Regime**: Ranging only (ADX <25)
- **Mirror**: S6 — BB lower + Stochastic <20 crossing up (BUY)
- **Stop**: Above BB upper | **TP**: BB middle or lower

### S7 — MACD Crossover + RSI Extreme
- **Entry**: MACD histogram crosses zero + RSI >70 (sell) or <30 (buy)
- **Regime**: All regimes — momentum reversal
- **Stop**: Recent swing high/low | **TP**: Next momentum divergence

### S8 — Parabolic SAR Flip + EMA Cross
- **Entry**: SAR dots flip sides + EMA(10) crosses EMA(20) same direction
- **Regime**: Trending (ADX >20) — catches reversals
- **Stop**: Opposite SAR flip | **TP**: Trend continuation until next SAR flip

### S9 — EMA Trend + MACD Confirmation
- **Entry**: EMA(10) crosses EMA(25) + MACD histogram aligns
- **Filter**: Skip if EMAs tangled (No Man's Land)
- **Regime**: Trending markets only
- **Stop**: EMA cross reversal | **TP**: Major S/R level

### S10 — Fibonacci Retracement Entry
- **Entry**: Price reaches 38.2%, 50%, or 61.8% fib + reversal signal
- **Context**: In established trend, entry at fib level pullback
- **Regime**: Trending — continuation trade
- **Stop**: Beyond 100% fib level | **TP**: Next swing high/low

### S11 — SMA 50/100 + MACD (CRITICAL: ADX >25)
- **Entry**: Price above/below BOTH SMA 50 & 100 + MACD turns positive/negative
- **Partial Exit**: Half at 1:1 R/R, move stop to BE, let rest run
- **Regime**: STRONG trending only (ADX >25) — FAILS in ranging/low volatility
- **Stop**: Back inside SMA range | **TP**: Staged exits

### S12 — BB Squeeze → Breakout
- **Entry**: BB narrows (squeeze) → breakout in trend direction
- **Rule**: "Only take signals in direction of overall trend"
- **Regime**: Compression → trending (ADX rising from <15)
- **Stop**: Back inside squeeze | **TP**: BB outer band

### S13 — Stochastic Oscillator Range Trading
- **Entry**: Stochastic crosses up from <20 (buy) or down from >80 (sell)
- **Best**: Ranging markets with clear alternating signals
- **Regime**: Ranging (ADX <20) — terrible in trends
- **Stop**: Opposite stochastic extreme | **TP**: Range boundary

### S14 — Momentum Divergence (PRIORITY SIGNAL)
- **Entry**: Price makes higher high but oscillator makes lower high (bearish)
- **Indicators**: RSI, Stochastic, Momentum, MACD
- **Signal**: Precedes major trend reversals
- **Regime**: Trend exhaustion → reversal
- **Stop**: Beyond divergence swing | **TP**: Trend reversal target

### S15 — Hidden Divergence (Continuation)
- **Entry**: Price higher low + oscillator lower low = bullish continuation
- **Opposite**: Price lower high + oscillator higher high = bearish continuation
- **Context**: Confirms trend strength, not reversal
- **Regime**: Strong trending | **TP**: Next trend leg

### S16 — Parabolic SAR Stop-and-Reverse
- **Rule**: Always in market — when stopped out of long → go short
- **Entry**: SAR dot flip + ADX >25 confirmation
- **Regime**: Strong trending only — terrible in ranges
- **Stop**: Next SAR flip | **TP**: Ride trend until SAR reversal

---

## CHART PATTERN SETUPS

### S17 — Ascending/Descending Triangle
- **Ascending**: Flat resistance + rising lows → buy breakout above resistance
- **Descending**: Flat support + falling highs → sell breakdown below support  
- **Stop**: Beyond triangle boundary | **TP**: Triangle height projected

### S18 — Head & Shoulders / Inverse H&S
- **Entry**: Neckline break with volume confirmation
- **Target**: Distance from head to neckline projected from break
- **Regime**: Major reversal pattern at trend exhaustion
- **Stop**: Return above/below neckline

### S19 — Double Top/Bottom
- **Entry**: Neckline break after second touch of resistance/support
- **Confirmation**: Volume on breakout, failure to make new high/low
- **Target**: Pattern height projected from neckline
- **Stop**: Back above/below neckline

### S20 — Bull/Bear Flag
- **Entry**: Breakout from flag in direction of flagpole
- **Pattern**: Sharp move (pole) → consolidation channel (flag) → continuation
- **Target**: Flagpole length projected from breakout
- **Stop**: Opposite side of flag channel

---

## REGIME → SETUP MAPPING (CRITICAL)

| **Regime** | **Detection** | **Primary Setups** | **Avoid** |
|------------|---------------|-------------------|-----------|
| **Strong Trend** | ADX >30, price beyond SMAs | S11, S16, S8, S12, S17-20 | S13, S5/S6 |
| **Weak Trend** | ADX 20-30 | S7, S9, S10, S8 | S16, S13 |
| **Ranging** | ADX <20, price between S/R | S1-S6, S13 | S11, S16, S8 |
| **Trend Exhaustion** | ADX declining from >30, divergence | S14, S2, S3, S18, S19 | S11, S16 |
| **Compression** | BB squeeze, ADX <15 | S12, S17 | S13, S16 |

---

## KEY TRADING RULES

1. **Trade with trend** — only take BB/range signals against trend in ranging regime
2. **Partial exits** — S11 style: exit ½ at 1:1, move stop to BE, let rest run
3. **Divergence priority** — S14 is the #1 reversal signal, overrides everything
4. **ADX is king** — S11 FAILS without ADX >25, S16 needs trending regime
5. **Confluence wins** — Multiple setups + fib levels + S/R = highest probability
6. **Fibonacci 50%** — most reliable retracement level across all timeframes
7. **Volume confirms** — patterns without volume are low-confidence
8. **Context matters** — hammer at support ≠ hammer in middle of nowhere

**FAILURE MODES:**
- S11: Fails on low-volatility pairs (EUR/GBP documented failure)
- S16: Whipsaws badly in ranges
- S13: Gets chopped up in trends
- All setups: Reduced effectiveness around major news events