# Technical Analysis — Complete Computation Reference

> **Modules:** `indicators.py`, `candlestick_patterns.py`, `chart_patterns.py`, `confluence_scorer.py`, `alignment.py`, `knowledge_store.py`
> **No external MCP** — all computation is local Python (pandas, ta-lib, numpy)
> **Primary input:** Candle data from oanda_data agent (H4/H1/M15)
> **Primary output:** Confluence score (0-100), regime, direction, patterns, alignment

---

## 1. INDICATORS (`indicators.py`)

All indicators computed from H1 candles via `Indicators(candles).compute_all()`.

### 1.1 EMA — Exponential Moving Averages

**Periods:** 21, 55, 100 (default set)

**Output:**
```json
{
  "emas": {21: [series], 55: [series], 100: [series]},
  "ema_crossovers": {
    "21_55": {"crossover": "bullish"|"bearish"|null, "bars_since": 3},
    "21_100": {"crossover": null, "bars_since": 12},
    "55_100": {"crossover": "bullish", "bars_since": 1}
  },
  "ema200_trend": {
    "above": true,
    "distance_pct": 0.35,
    "trend": "bullish"
  }
}
```

**Interpretation:**
- Price above all 3 EMAs = strong bullish
- EMAs stacked (21 > 55 > 100) = trending, trade WITH the stack direction
- EMA crossovers: 21/55 cross = short-term momentum shift, 55/100 cross = major trend change
- Distance from EMA = how extended price is. Too far = reversion risk

**Confluence weight:** 15/100 (highest tier, tied with RSI and multi-TF)

### 1.2 RSI — Relative Strength Index

**Period:** 14

**Output:**
```json
{
  "rsi": {
    "value": 65.4,
    "overbought": false,
    "oversold": false
  },
  "rsi_divergence": {
    "bullish_divergence": false,
    "bearish_divergence": true,
    "details": "Bearish: price high 1.04920 > 1.04850 but RSI 68.2 < 72.1"
  }
}
```

**Thresholds:**
- `> 70` = overbought (sell pressure building)
- `< 30` = oversold (buy pressure building)
- `50` = midline (above = bullish bias, below = bearish bias)

**Divergence:** The most powerful RSI signal. When price makes new highs but RSI doesn't → bearish divergence (momentum fading). This is what drives setup S15 (96-100% win rate in exhaustion/ranging regimes).

**Confluence weight:** 15/100 (highest tier)
- In ranging regime: weight × 1.2 (RSI more reliable in ranges)
- In trending regime: weight × 1.0 (normal)

### 1.3 MACD — Moving Average Convergence Divergence

**Parameters:** fast=12, slow=26, signal=9

**Output:**
```json
{
  "macd": {
    "macd": 0.000234,
    "signal": 0.000180,
    "histogram": 0.000054,
    "crossover": "bullish"|"bearish"|null,
    "momentum": "positive"|"negative"
  }
}
```

**Interpretation:**
- `crossover: "bullish"` = MACD just crossed above signal line (buy signal)
- `crossover: "bearish"` = MACD just crossed below signal line (sell signal)
- `histogram > 0` and growing = bullish momentum increasing
- `histogram > 0` and shrinking = bullish momentum fading (potential reversal)
- Zero-line cross (MACD crosses 0) = major trend shift

**Confluence weight:** 10/100

### 1.4 Bollinger Bands

**Parameters:** period=20, std_dev=2

**Output:**
```json
{
  "bollinger": {
    "upper": 1.05120,
    "middle": 1.04900,
    "lower": 1.04680,
    "bandwidth": 0.0042,
    "squeeze": false,
    "position": "upper"|"middle"|"lower"
  }
}
```

**Interpretation:**
- `squeeze: true` (bandwidth < 0.02) = volatility compression, breakout imminent
- `position: "upper"` = price in top 25% of band range (overbought zone in ranges)
- `position: "lower"` = price in bottom 25% (oversold zone in ranges)
- Bandwidth expanding = trend acceleration
- Bandwidth contracting = trend weakening or range forming

**Confluence weight:** 10/100
- In ranging regime: weight × 1.4 (BB is a range-trading tool)
- In trending regime: weight × 0.7 (less reliable in trends)

### 1.5 ATR — Average True Range

**Period:** 14

**Output:**
```json
{
  "atr": {
    "value": 0.00085
  }
}
```

**Not scored in confluence** — ATR is used for position sizing, not direction:
- SL distance = ATR × multiplier (typically 2.0-2.5)
- TP distance = ATR × multiplier (typically 0.3-0.5 for sniper strategy)
- Wide ATR = bigger stops needed, reduce position size
- Narrow ATR = tight stops okay, normal position size

---

## 2. ADVANCED INDICATORS (via `AdvancedIndicators`)

Computed alongside core indicators. Key outputs used by confluence scorer:

### 2.1 ADX — Average Directional Index

**Period:** 14

**Output:** `{"adx": {"value": 28.5, "adx": 28.5}}`

**THE regime classifier.** ADX doesn't tell you direction — it tells you if there IS a trend:
- `> 30` = strong trend (trending regime)
- `> 25` = moderate trend (trending regime)
- `20-25` = mixed/transitional
- `< 20` = no trend (ranging regime)
- `< 15` = dead market (squeeze regime)

**ADX declining from >30** = trend exhaustion → switch from trend setups to reversal setups

### 2.2 Stochastic Oscillator

**Parameters:** k=14, d=3, smooth=3

**Output:** `{"stochastic": {"k": 75.2, "d": 68.4, "overbought": false, "oversold": false}}`

**Thresholds:**
- `K > 80` = overbought
- `K < 20` = oversold
- K crossing above D from below 20 = buy signal (S13)
- K crossing below D from above 80 = sell signal (S13)

**Confluence weight:** 5/100
- In ranging regime: weight × 1.5 (stochastic is a range tool)
- In trending regime: weight × 0.5 (unreliable in trends — stays overbought/oversold)

### 2.3 Volume SMA

**Output:** `{"volume_sma": {"current": 1234, "average": 1100, "ratio": 1.12}}`

Volume confirms conviction. High volume on a breakout = real. Low volume = fake out.

**Confluence weight:** 5/100

---

## 3. CANDLESTICK PATTERNS (`candlestick_patterns.py`)

Scans all 61 TA-Lib CDL* functions. Returns detected patterns with priority and context filtering.

### 3.1 Priority Classification

**HIGH priority** (strongest reversal signals):
| Pattern | Candles | Signal | Direction |
|---------|---------|--------|-----------|
| Engulfing | 2 | Strong reversal | Bullish/Bearish |
| Hammer | 1 | Bottom reversal | Bullish |
| Shooting Star | 1 | Top reversal | Bearish |
| Morning Star | 3 | Bottom reversal | Bullish |
| Evening Star | 3 | Top reversal | Bearish |
| Three White Soldiers | 3 | Strong bullish continuation | Bullish |
| Three Black Crows | 3 | Strong bearish continuation | Bearish |

**MEDIUM priority** (confirmation signals):
| Pattern | Candles | Signal |
|---------|---------|--------|
| Hikkake | 2 | Trap/reversal |
| Inverted Hammer | 1 | Potential bottom |
| Hanging Man | 1 | Potential top |
| Dragonfly Doji | 1 | Indecision at support |
| Gravestone Doji | 1 | Indecision at resistance |
| Tri-Star | 3 | Strong reversal |

**LOW priority** (all other 45+ patterns): weak signals, need strong confluence from other sources.

### 3.2 Context Filtering

Raw pattern detection returns many false positives. Context filtering improves quality:

- **Trend agreement:** Pattern direction should agree with the trend (or be a reversal AT an extreme)
- **Regime scoring:** Reversal patterns score higher in ranging/exhaustion regimes
- **Lookback config:** Each pattern has a lookback window for context (recent bars)

### 3.3 Output Format

```json
{
  "candlestick_patterns": {
    "detected_count": 3,
    "filtered_patterns": [
      {
        "pattern": "CDLENGULFING",
        "name": "Engulfing",
        "direction": "bullish",
        "strength": 100,
        "priority": "high",
        "candle_count": 2,
        "bar_index": -1
      }
    ]
  }
}
```

**Confluence weight:** 10/100

---

## 4. CHART PATTERNS (`chart_patterns.py`)

Detects 14 chart patterns using swing point analysis.

### 4.1 Reversal Patterns

| Pattern | Detection | Signal |
|---------|-----------|--------|
| **Double Bottom** | Two lows within 1% of each other, neckline break | Bullish reversal |
| **Double Top** | Two highs within 1% of each other, neckline break | Bearish reversal |
| **Triple Bottom** | Three lows at similar level | Strong bullish reversal |
| **Triple Top** | Three highs at similar level | Strong bearish reversal |
| **Head & Shoulders** | Left shoulder, head (higher), right shoulder, neckline break | Bearish reversal |
| **Inverse H&S** | Mirror of H&S | Bullish reversal |

### 4.2 Continuation Patterns

| Pattern | Detection | Signal |
|---------|-----------|--------|
| **Bull Flag** | Sharp move up, tight downward consolidation, breakout | Bullish continuation |
| **Bear Flag** | Sharp move down, tight upward consolidation, breakdown | Bearish continuation |
| **Ascending Triangle** | Flat resistance, rising lows, breakout above | Bullish |
| **Descending Triangle** | Flat support, falling highs, breakdown below | Bearish |
| **Symmetrical Triangle** | Converging trendlines, breakout either way | Direction of breakout |
| **Cup & Handle** | U-shaped recovery, small pullback, breakout | Bullish |

### 4.3 Pattern Confirmation

Each detected pattern has a `confirmed` field:
- `confirmed: true` = price has broken the neckline/trendline
- `confirmed: false` = pattern is forming but not yet triggered

Only confirmed patterns should drive trade decisions. Unconfirmed patterns are "watch list" items.

### 4.4 Output Format

```json
{
  "chart_patterns": {
    "patterns": [
      {
        "type": "double_bottom",
        "direction": "bullish",
        "confirmed": true,
        "neckline": 1.0510,
        "target": 1.0555,
        "stop_level": 1.0465,
        "confidence": 0.85
      }
    ],
    "reversal_patterns": [...],
    "continuation_patterns": [...]
  }
}
```

**Confluence weight:** 10/100

---

## 5. CONFLUENCE SCORER (`confluence_scorer.py`)

The decision engine. Combines 10 signal sources into a single 0-100 score with regime awareness.

### 5.1 Weight Distribution

| Source | Max Weight | What It Measures |
|--------|-----------|------------------|
| EMA | 15 | Trend direction and strength |
| RSI | 15 | Momentum and overbought/oversold |
| Multi-TF | 15 | H4/H1/M15 directional agreement |
| MACD | 10 | Momentum and crossovers |
| Bollinger | 10 | Volatility and mean reversion |
| Candlestick | 10 | Price action patterns |
| Chart | 10 | Structural patterns |
| Volume | 5 | Conviction confirmation |
| Stochastic | 5 | Range-bound momentum |
| News | 5 | Sentiment from intelligence agent |
| **Total** | **100** | |

### 5.2 Regime Multipliers

ADX determines the regime, which adjusts weights:

**Trending (ADX > 25):**
- Boosted: EMA (×1.3), MACD (×1.3), Multi-TF (×1.2)
- Reduced: Bollinger (×0.7), Stochastic (×0.5)
- Logic: In trends, trend-following indicators are more reliable

**Ranging (ADX < 20):**
- Boosted: Bollinger (×1.4), Stochastic (×1.5), RSI (×1.2)
- Reduced: EMA (×0.6), MACD (×0.6)
- Logic: In ranges, mean-reversion indicators are more reliable

**Mixed (ADX 20-25):**
- All multipliers = 1.0 (no adjustment)

### 5.3 Trade Threshold

**Score ≥ 70 = tradeable.** Below 70, do NOT trade.

### 5.4 Output Format

```json
{
  "total_score": 78.5,
  "regime": "trending",
  "adx_value": 28.5,
  "breakdown": {
    "ema": 14.2,
    "rsi": 12.0,
    "macd": 8.5,
    "bollinger": 6.3,
    "volume": 3.5,
    "stochastic": 2.1,
    "multi_tf": 13.5,
    "candlestick": 8.0,
    "chart": 7.5,
    "news": 2.9
  },
  "direction": "bullish",
  "max_possible": 100,
  "threshold": 70
}
```

### 5.5 Direction Determination

The scorer combines directional signals from all sources:
- EMA stack direction
- RSI above/below 50
- MACD above/below zero
- Bollinger position (upper/lower)
- Pattern directions (bullish/bearish)
- Multi-TF alignment direction

Result: `"bullish"`, `"bearish"`, or `"neutral"` (conflicting signals cancel out)

---

## 6. MULTI-TIMEFRAME ALIGNMENT (`alignment.py`)

Computes indicators on ALL provided timeframes and checks directional agreement.

### 6.1 What It Does

For each timeframe (H4, H1, M15):
1. Compute core indicators (EMA, RSI, MACD, BB, ATR)
2. Compute advanced indicators (ADX, Stochastic, Volume SMA)
3. Determine directional bias per timeframe

Then compare:
- Do all 3 timeframes agree on direction? → Strong alignment
- Do H4 and H1 agree but M15 differs? → Moderate alignment (H4+H1 outweigh M15)
- Do they conflict? → Weak alignment (reduce confidence)

### 6.2 H4 Filter Edge

**Backtest proof: +4.1 percentage points** when H4 agrees with H1 direction.

This means: if H1 says "buy" and H4 also says "bullish," the historical win rate is 4.1pp higher than when H4 disagrees. This is the single most impactful filter in the system.

### 6.3 Output Format

```json
{
  "H4": {"direction": "bullish", "strength": 0.75, "indicators": {...}},
  "H1": {"direction": "bullish", "strength": 0.82, "indicators": {...}},
  "M15": {"direction": "bullish", "strength": 0.68, "indicators": {...}},
  "alignment": "strong",
  "h4_agrees": true,
  "directional_bias": {
    "direction": "bullish",
    "confidence": 0.85,
    "summary": "All timeframes bullish, strong alignment"
  }
}
```

**Confluence weight:** 15/100 (highest tier, reflecting the proven H4 edge)

---

---

## 7. REGIME CLASSIFICATION

> **Note:** The KnowledgeStore and backtest database (39,692 rows from 8.5M trades) are the **validator agent's** domain. The technical analyst detects signals from live data; the validator checks those signals against historical evidence.

### 8.1 Regime Types

| Regime | ADX Range | BB Behavior | Market Character |
|--------|-----------|-------------|------------------|
| **Strong Trend** | > 30 | Expanding bands, price riding upper/lower band | Directional moves, trend-following works |
| **Moderate Trend** | 25-30 | Normal bands | Moderate directional bias |
| **Mixed** | 20-25 | Transitional | No clear edge, reduce size |
| **Ranging** | < 20 | Stable width, price bouncing between bands | Mean reversion works, trend-following fails |
| **Squeeze** | < 15 | Extremely narrow bands (bandwidth < 0.02) | Breakout imminent, direction unknown |
| **Exhaustion** | Declining from >30 | Bands starting to contract | Divergence setups fire, trend setups fail |
| **High Volatility** | Any | ATR spiking, wide bands | Widen stops, reduce size |

### 8.2 Regime → Setup Mapping

| Regime | Best Setups | Avoid |
|--------|-------------|-------|
| Strong Trend | S7 (channel), S11 (SMA+MACD), S12 (BB breakout), S16 (SAR) | S13 (stochastic), S14 (CCI) |
| Ranging | S1 (hammer), S4 (doji), S8 (S/R break), S13 (stoch), S14 (CCI) | S11 (SMA+MACD), S16 (SAR) |
| Exhaustion | S15 (divergence), S2 (engulfing), S3 (morning/evening star) | S7 (channel), S16 (SAR) |
| Squeeze | S5/S6 (triangle breakout), S12 (BB squeeze) | All reversal setups |
| High Volatility | S19 (widen stops), reduce size | Tight stop strategies |

---

## 8. THE 20 SETUPS (S1-S20)

### Category A: Candlestick Pattern Setups
- **S1 — Hammer/Pin Bar:** Long wick rejection at support, RSI<30, Stoch<20, BB lower band
- **S2 — Engulfing:** Full body engulfment at S/R levels, volume confirmation
- **S3 — Morning/Evening Star:** Three-candle reversal, gap strengthens. Suppress when ADX<22
- **S4 — Doji at Extremes:** Doji at overbought/oversold with RSI/Stoch confirmation

### Category B: Chart Pattern Setups
- **S5 — Ascending Triangle:** Flat resistance + rising lows, buy on breakout above
- **S6 — Descending Triangle:** Flat support + falling highs, sell on breakdown below
- **S7 — Channel Trading:** Buy at lower channel line, TP at upper. Best in trends
- **S8 — S/R Break:** Ranging → breakout when support/resistance breaks with confirmation
- **S9 — Head & Shoulders:** Neckline break entry, target = head-to-neckline distance
- **S10 — Double Top/Bottom:** Enter on neckline break, target = pattern height

### Category C: Indicator-Based Setups
- **S11 — SMA 50/100 + MACD:** Buy when price above both SMAs + MACD positive. Trend-only
- **S12 — BB Squeeze Breakout:** Squeeze (bandwidth < 0.02) → breakout in trend direction
- **S13 — Slow Stochastic:** Buy when K crosses up from <20, sell when crosses down from >80. Range-only
- **S14 — CCI Extremes:** Overbought >+100, oversold <-100. Reversal from extremes
- **S15 — Divergence:** Price makes new high/low, oscillator doesn't. THE king setup. 96%+ win in ranging
- **S16 — Parabolic SAR:** Dots flip = entry. Always-in strategy. Trend-only (ADX>25)

### Category D: Structural Setups
- **S17 — Pivot Points:** PP/S1-S3/R1-R3 as targets and stop levels
- **S18 — Fibonacci in Channels:** 50% retracement + channel line confluence

### Category E: Volatility Setups
- **S19 — ATR/StdDev Volatility:** Wide stops in high-vol, reduce size. StdDev spike = trend change
- **S20 — Multi-Pair Correlation:** Correlated pairs confirm each other. Reduce size if both open

---

## 9. WRAPPER FUNCTION

The trading cycle calls one function: `run_full_analysis(candles_by_tf, news_score, instrument)`

This wrapper executes in order:
1. `Indicators(h1_candles).compute_all()` → core indicators
2. `CandlestickPatterns(h1_candles).get_detected_patterns()` → candle patterns
3. `ChartPatterns(h1_candles).scan_all()` → chart patterns
4. `ConfluenceScorer().compute_score(indicators, patterns)` → confluence score
5. `MultiTimeframeAlignment(candles_by_tf).analyze()` → multi-TF alignment

Returns consolidated dict with all results for the validator.
