---
title: Forex M15 Pattern Library — All 46 Teaching Images
type: reference
workspace: forex-trading-team
agent: validator
version: 2.0 (2026-04-24, complete coverage)
source_images: <repo_root>/Data/charts/teaching/
description: 1:1 mapping from every teaching image to structured description. Used by the distilled 35B validator as pattern vocabulary + reference. Contains all candlestick patterns, chart patterns, and annotated real-trade examples.
---

# Forex M15 Pattern Library — Complete Coverage

This file documents **every teaching image** used to train the 35B validator. 46 images total across three directories:

- `/teaching/` root: 19 images (4 d6_trade, 9 tim_teach, 6 trade outcome examples)
- `/teaching/patterns/pattern_NN_*.png`: 17 named pattern library images
- `/teaching/patterns/chart_N.png`: 10 generic educational reference charts

The 35B was distilled with knowledge from all of these. This file is the canonical text mapping so agents use consistent vocabulary and the setups stay tied to the images Tim curated.

**Fishing line overlay** (Tim's core mental model): every setup has a rod-tension state — **loading** (pre-move), **bending** (move developing), **at max tension** (entry zone), **snapping** (thesis broken). Use this to decide WHERE the line sits.

---

# SECTION A — NAMED PATTERN LIBRARY (17 images)

## pattern_01_hammer_pin_bar.png — Hammer / Pin Bar
**Type**: single candle reversal (bullish or bearish mirror)
**Bullish hammer ASCII**:
```
   ┃         <- small body at TOP
   █
   ┃
   ┃         <- lower wick ≥ 2× body
```
**Bearish shooting star ASCII**:
```
   ┃         <- upper wick ≥ 2× body
   ┃
   ┃
   █         <- small body at BOTTOM
```
**Detection**: wick ≥ 2× body on one side; opposite wick < body; at swing extreme or near E55/E100.
**Bias**: bullish (hammer at support) / bearish (star at resistance).
**Entry**: next candle close past the body in reversal direction.
**Invalidation**: close back through the wick extreme.
**Fishing line**: at max tension on prior move; reversal candle is the release.
**Python detector**: `pattern=hammer|shooting_star`.
**Reliability**: HIGH at key S/R, MEDIUM random.

## pattern_02_engulfing_bullish.png — Bullish Engulfing
**Type**: 2-candle reversal.
**Structure**: small red candle → large green candle whose body wraps the red body entirely.
**Detection**: `open_now ≤ close_prev AND close_now ≥ open_prev`. Second candle green, first red.
**Entry**: close of engulfing candle.
**Invalidation**: below engulfing low.
**Target**: prior swing high or BB upper.
**Reliability**: HIGH — one of the most reliable reversal patterns.
**Python detector**: `pattern=bullish_engulfing bias=bullish`.

## pattern_03_engulfing_bearish.png — Bearish Engulfing
**Type**: 2-candle reversal (mirror).
**Structure**: small green → large red engulfing the green body.
**Detection**: `open_now ≥ close_prev AND close_now ≤ open_prev`. Second candle red, first green.
**Entry**: close of engulfing candle.
**Invalidation**: above engulfing high.
**Python detector**: `pattern=bearish_engulfing bias=bearish`.

## pattern_04_morning_evening_star.png — Morning / Evening Star
**Type**: 3-candle reversal.
**Morning star (bullish)**: large red → small body/doji → large green closing above midpoint of first.
**Evening star (bearish)**: large green → small body/doji → large red closing below midpoint of first.
**Detection**: 3-bar window with the size pattern big-small-big.
**Entry**: close of third candle.
**Invalidation**: beyond the star's extreme (low for morning, high for evening).
**Reliability**: HIGH — strongest 3-candle reversal.
**Python detector**: `pattern=morning_star|evening_star`.

## pattern_05_doji_extreme.png — Doji at Extreme
**Type**: indecision / reversal pending.
**Structure**: body ≤ 10% of total range. Variants:
- **Dragonfly** (long lower wick, no upper): bullish at support
- **Gravestone** (long upper wick, no lower): bearish at resistance
- **Long-legged** (shadows both sides): extreme indecision, big move coming
- **Standard**: pure indecision; needs confirmation candle
**Context matters**: doji at BB extreme / RSI extreme / swing high-low = real signal. Doji mid-range = noise.
**Python detector**: `pattern=doji_at_top|doji_at_bottom|doji`.

## pattern_06_ascending_triangle.png — Ascending Triangle
**Type**: continuation pattern (bullish).
**Structure** (ASCII):
```
─────────────────    <- flat horizontal top (resistance)
     /|    /|
    / |   / |
   /  |  /  |
  /   | /   |       <- higher lows rising into flat top
```
**Detection**: flat resistance (3+ tests), higher lows compressing into it.
**Confirmation**: decisive close above the flat top.
**Target**: triangle height projected up from breakout.
**Fishing line**: rod loading during compression; snap on upside break.
**Bias**: bullish continuation (or breakout after range).

## pattern_07_descending_triangle.png — Descending Triangle
**Type**: continuation pattern (bearish, mirror of ascending).
**Structure**: flat horizontal bottom (support), lower highs compressing down to it.
**Confirmation**: decisive close below the flat bottom.
**Target**: triangle height projected down.
**Bias**: bearish continuation.

## pattern_08_channel_trading.png — Channel Trading
**Type**: range / parallel channel.
**Structure**: price oscillating between parallel support + resistance lines.
**Trade**: fade extremes with confirmation candle; avoid middle; take profit near opposite band.
**Invalidation**: decisive break of either bound (channel breakout).
**Best use**: M15 channel within larger H1/H4 trend — fade the counter-trend side, trade the with-trend side heavier.

## pattern_09_support_resistance_break.png — S/R Break
**Type**: breakout.
**Structure**: horizontal S/R tested multiple times, then decisively pierced.
**Confirmation**: close beyond the level by ≥ 0.3 ATR, with volume / BB expansion.
**False break**: close immediately returns inside = fake-out; reverse the bias.
**Trade direction**: with the break. **Invalidation**: return back through the level within 3 bars.

## pattern_10_bb_squeeze_breakout.png — BB Squeeze Breakout (Tim's #1 setup)
**Type**: volatility expansion trade.
**Structure**: Bollinger Bands compress to tight bandwidth for ≥10 M15 bars, then price decisively pierces one band.
**Detection**:
- `bandwidth < 50% of 20-bar average` sustained ≥10 bars
- Price closes beyond band by ≥ 0.5 × current bandwidth
- EMA fan aligned with break direction
**Bias**: directional (follows the break direction).
**Entry**: on break confirmation close.
**Invalidation**: close back inside bands within 3 bars.
**Target**: 2-3× bandwidth projected from break.
**Fishing line**: squeeze = rod at max tension loading; break = the snap.
**Tim's note**: bearish version often preceded by double top at E100 + E21 crossing below E55.

## pattern_11_momentum_divergence.png — RSI / MACD Divergence
**Type**: leading reversal (regular) or continuation (hidden) signal.
**Regular bearish**: price makes HIGHER high, indicator LOWER high → reversal down.
**Regular bullish**: price LOWER low, indicator HIGHER low → reversal up.
**Hidden bullish**: price HIGHER low, indicator LOWER low → uptrend continues.
**Hidden bearish**: price LOWER high, indicator HIGHER high → downtrend continues.
**Detection rule**: need 2 swing points on price + matching 2 points on indicator.
**Trade**: wait for price-level confirmation (break of prior swing) before entering.
**Reliability**: HIGH — #1 leading reversal signal per validator encyclopedia.

## pattern_12_fibonacci_channel.png — Fibonacci Retracement
**Type**: retracement entry in established trend.
**Levels**: 38.2%, 50%, 61.8% (and 78.6% for deep).
**Use**: in trend, wait for pullback to a fib + confluence signal (hammer/engulfing/doji/RSI cross).
**Best confluence**: fib + E55/E100 + reversal candle.
**Invalidation**: break of the next deeper fib level (e.g., if entering at 38.2%, stop below 50%).

## pattern_13_multi_pair_correlation.png — Multi-Pair Correlation
**Type**: confluence confirmation signal (not a direct setup).
**Rule**: USD-pairs move opposite to each other when USD dominates (e.g., EUR/USD up → USD/CHF down, USD/JPY down).
**Use**: if your pair's setup conflicts with correlated pair behavior, LOWER confidence by ~20%.
**Common pairs**: EUR/USD ↔ USD/CHF (inverse ~85%), EUR/USD ↔ AUD/USD (direct ~70%), USD/JPY ↔ EUR/JPY (complex).

## pattern_14_volatility_atr.png — Volatility / ATR Regime
**Type**: regime indicator (shapes risk/SL sizing, not direct entry).
**Rule**: ATR tells you the regime. Low ATR = ranging, high ATR = trending/breakout.
**SL sizing**: width scales with ATR. Use 1.5-2.5× ATR for SL distance.
**Entry filter**: prefer setups when current ATR > 60% of 20-period average (volatility expanding).
**Warning**: entering when ATR is contracting = often chop ahead.

## pattern_15_sma_macd_win.png — SMA + MACD Confluence WIN Example
**Type**: winning confluence setup.
**What it shows**: MACD histogram cross aligned with EMA cross AND BB expanding.
**Lesson**: MACD cross alone is weak. Require BB expansion OR EMA fan alignment for a valid trade.
**Entry**: on the confluence bar (all three conditions true simultaneously).

## pattern_16_sma_macd_loss.png — SMA + MACD FAIL Example
**Type**: losing setup to AVOID.
**What it shows**: MACD cross WITHOUT BB expansion → false signal, chop / whipsaw.
**Lesson**: the rule from pattern_15 enforced — if BBs are flat, MACD crosses are noise.
**Action**: SKIP if you see MACD cross but BB flat/contracting.

## pattern_17_pivot_points.png — Daily Pivot Points
**Type**: static S/R reference.
**Use**: daily pivots act as S/R on M15. Price near R1/S1 = possible reversal. Break of R2/S2 = breakout confirmation.
**Formula**: PP = (H+L+C)/3; R1 = 2×PP − L; S1 = 2×PP − H; R2 = PP + (H−L); S2 = PP − (H−L).
**Trade**: fade R1/S1 with reversal signal, ride break through R2/S2 with momentum.

---

# SECTION B — TIM'S ANNOTATED TEACHING CHARTS (9 images)

## tim_teach_1.png — AUD_USD bullish fan expansion (TRADE_NOW example)
**What it shows**: green zone — fan opening wide, BBs expanding. Clean unmistakable bullish expansion.
**Lesson**: this is what "TRADE_NOW" looks like. Fan ordered bullish (E21>E55>E100) and separating widely; BB bandwidth growing; candles riding upper band.
**Use this as a visual template** for bullish TRADE_NOW verdicts.

## tim_teach_2.png — GBP_USD bearish fan expansion (TRADE_NOW example)
**What it shows**: clear downward expansion after fan cross. EMAs separating in order (E100>E55>E21 bearish), BBs widening, strong red candles.
**Lesson**: mirror of tim_teach_1 for bearish TRADE_NOW.

## tim_teach_3.png — EUR_CHF TANGLED fan (SKIP example — critical)
**What it shows**: EMAs fully converged and CROSSING each other with no consistent order. Red boxes mark no-trade zones.
**KEY DISTINCTION**: this is NOT a peaked/retrace setup. It's pure chop.
- Ordered+contracting fan (E21 still above E55) = retrace SETUP → WATCH/TRADE.
- Tangled/disordered fan (E21 crossing E55 multiple times) = chop → SKIP.
**Lesson**: skip tangled fans even if individual candles look directional.

## tim_teach_4.png — EUR_USD peaked fan + BBs contracting (RETRACEMENT SETUP)
**What it shows**: fan peaked, BBs contracting. If E21 still ordered above E55, this is the setup FORMING.
**Lesson**: watch for price to hit E55 (mid-retrace) or E100 (deep retrace) for entry. BB tightening during retrace is EXPECTED (the rod bending).
**Skip only if**: E21 has crossed BELOW E55 (fan failed), OR price still at peak (nothing to retrace into).
**Fishing line**: rod at max bend during this state.

## tim_teach_stage1_fan_entry.png — Phase 2.5 E21×E55 Entry (EUR/AUD LONG)
**What it shows**: E21 just crossed E55 (first cross, circled). E21 has NOT YET crossed E100 — candles show yellow-highlighted space from E100. Full fan (E21>E55>E100) forms AFTER entry.
**Lesson**: do NOT wait for the full fan. The E21×E55 cross with opening gap IS the entry. Earlier = better R/R.
**Entry**: on the cross bar or next close in direction.
**Invalidation**: E21 crosses back below E55 within 3 bars.

## tim_teach_euraud_phase25_e100_retest.png — E100 Retest BUY (EUR/AUD)
**What it shows**: price at E100, fan peaked/contracting but still ORDERED (E21>E55>E100). Yellow circles mark the BUY zone.
**CRITICAL rule**: double top signals that fired at E100 here were ACCUMULATION candles, not distribution — fan ordering overrides candle-level signals.
**Entry**: BUY at E100 zone if fan still ordered.
**Invalidation**: E21 crosses below E55 (fan fails).
**Common mistakes**:
- Rejecting because BBs contracting (they're SUPPOSED to contract in retrace)
- Rejecting because fan velocity negative (expected during retrace)
- Taking the double-top signal literally (context overrides)
**Fishing line**: line at maximum tension — THIS IS YOUR ENTRY.

## tim_teach_euraud_annotated_bullish.png — Annotated Bullish Setup
**What it shows**: Tim's annotations overlaid on a bullish setup — entry/SL/TP marked, with commentary on which signals confirmed the thesis.
**Lesson**: shows the full thought process for entering a bullish setup with annotations, not just the candles.

## tim_teach_eurchf_bearish_fan_flip.png — THE Bearish Fan Flip (EUR/CHF SHORT)
**THE CANONICAL BEARISH SETUP**. Sequence of events:
1. Bollinger squeeze for 10+ hours (very tight bands)
2. Double top forms at E100 (95% confidence — annotated in image)
3. E21 crosses BELOW E55 and E100 — fan flips bearish
4. Explosive 100+ pip breakdown
**Lesson**: E21 crossing BELOW E55 is THE fan failure / reversal trigger.
**Key caveat on RSI**: RSI hit 18.8 AFTER the move — this is NORMAL for a strong trend. Do NOT skip a bearish fan flip because RSI looks "oversold."
**This is the #1 bearish setup in Tim's playbook.**

## tim_teach_eurchf_annotated_short_snipe.png — Annotated Short Snipe
**What it shows**: Tim's annotations on the EUR/CHF short setup — entry zone, SL placement, TP projection, and the specific candle/structural signals that triggered the snipe.
**Lesson**: how to annotate a chart when building a short snipe thesis with clear entry/SL/TP levels.

---

# SECTION C — REAL TRADE OUTCOME EXAMPLES (6 images in /teaching)

## trade_103_AUD_JPY_SHORT_LOSS_-34p.png — LOSS -34p (SKIP example)
**What it shows**: AUD_JPY short trade taken that lost -34 pips.
**Why it lost**: CHOPPY setup. E100 too close to price. No clear EMA separation. Fan Width didn't show sustained growth.
**Lesson**: SKIP this type of setup. Short taken against insufficient structure = loss.
**Validator rule**: if fan is not clearly separating AND BBs not expanding, SKIP the short signal.

## trade_311_EUR_JPY_LONG_WIN_+93p.png — WIN +93p (TRADE_NOW example)
**What it shows**: EUR_JPY long trade that won +93 pips.
**Why it won**: bullish expansion. EMAs separating upward, BBs confirming expansion. Entry when expansion was visually clear.
**Lesson**: this is a textbook TRADE_NOW long.

## trade_338_GBP_JPY_SHORT_LOSS_-74p.png — LOSS -74p (bigger SKIP lesson)
**What it shows**: GBP_JPY short that lost -74p.
**Why it lost**: fan never expanded. Entered on a cross but EMAs stayed tangled. Fan Width showed short inconsistent bars.
**Lesson**: a cross without follow-through is a fake setup. Wait for fan actually separating before entering.
**Validator rule**: "cross only" entries = high failure rate. Require cross + separation.

## trade_364_USD_JPY_SHORT_WIN_+190p.png — BIG WIN +190p (elite TRADE_NOW)
**What it shows**: USD_JPY short that won +190 pips.
**Why it won**: perfect bearish expansion. Fan opens wide, BBs expand, candles drop cleanly riding lower band. Fan Width bars grow tall and green.
**Lesson**: when you see THIS on a chart, it's a max-conviction TRADE_NOW.
**Use as gold-standard reference** for bearish big-win setup.

## trade_633_EUR_AUD_BUY_LOSS_-3p.png — blank/corrupt PNG (FILE ISSUE)
**What it shows**: PNG file is transparent/empty (0 usable pixels). Corrupt or never rendered.
**Action**: regenerate from historical OHLC, OR exclude from training data.
**Expected content**: small EUR_AUD buy loss example (-3 pips per filename).

## trade_641_EUR_AUD_BUY_LOSS_-5p.png — blank/corrupt PNG (FILE ISSUE)
**Same as above** — transparent/empty. Regenerate or exclude. Expected -5p EUR_AUD buy loss example.

---

# SECTION D — D6 CURATED TRADES (4 images)

The d6 series is a curated set of clean entry examples numbered from an original 20-trade study. Only 4 were saved to the teaching directory.

## d6_trade_01_EUR_USD_long_WIN.png — EUR/USD LONG WIN
**What it shows**: bullish fan setup on EUR/USD with clean long entry that won.
**Lesson**: classic long — fan ordered bullish, entry at BB mid or E21 pullback with bullish continuation candle.

## d6_trade_03_EUR_USD_short_WIN.png — EUR/USD SHORT WIN
**What it shows**: bearish fan setup on EUR/USD, clean short that won.
**Lesson**: mirror of d6_01 — fan ordered bearish, entry at BB mid or E21 touch with bearish continuation.

## d6_trade_06_GBP_JPY_long_WIN.png — GBP/JPY LONG WIN
**What it shows**: GBP/JPY bullish continuation winning trade.
**Lesson**: JPY pairs move faster; look for BB expansion with strong momentum bars before entering.

## d6_trade_16_GBP_JPY_short_WIN.png — GBP/JPY SHORT WIN
**What it shows**: GBP/JPY bearish continuation winning trade.
**Lesson**: bearish fan flip or continuation on the JPY cross — move was decisive once confirmed.

---

# SECTION E — GENERIC REFERENCE CHARTS (10 images in /patterns)

The `chart_1.png` through `chart_10.png` are educational reference images (likely sourced from TradingView screenshots or a forex course). They were included in the 35B training set as generic examples of chart reading, not tied to specific named patterns.

## chart_1.png — generic reference (1920×979, 16:9 landscape)
## chart_2.png — generic reference (1920×884)
## chart_3.png — generic reference (1920×978)
## chart_4.png — generic reference (1920×915)
## chart_5.png — generic reference (1772×1190, near-4:3)
## chart_6.png — generic reference (1794×1192)
## chart_7.png — generic reference (1746×1174)
## chart_8.png — generic reference (1782×1190)
## chart_9.png — generic reference (1784×1168)
## chart_10.png — generic reference (1764×1188)

**Role in training**: these charts were shown to the model during distillation as examples of various market states — trends, ranges, reversals. They help the model generalize "what a forex chart looks like" beyond the specific tim_teach annotated setups.

**Usage today**: not actively referenced by name in the validator prompt, but present in the training signal. If specific educational content from any of these proves important, this section should be updated with the specific pattern each depicts.

---

# SECTION F — CONTEXT RULES (when patterns are reliable)

### Location matters most
1. At key S/R level → pattern reliability doubles
2. At E55 or E100 → strong confluence with thesis framework
3. At Bollinger Band → volatility-informed reversal zone
4. In empty space → pattern reliability drops significantly

### Trend context
1. Reversal pattern WITH higher-timeframe trend → most reliable
2. Reversal pattern AGAINST higher-timeframe trend → less reliable, needs more confirmation
3. Continuation pattern WITH the trend → high reliability
4. Pattern in consolidation/range → low reliability unless at range boundary

### Volume / BB width confirmation
- Reversal patterns should show INCREASING BB width on the reversal bar
- Continuation patterns work on normal BB width
- Patterns with contracting BB width = noise, skip them

### Multiple pattern confluence
- Two patterns at same level = very strong (e.g., tweezer bottom + bullish engulfing)
- Pattern + indicator signal = strong (e.g., hammer + RSI oversold + at E100)
- Three+ signals = high-conviction trade

### Candle size
- Patterns with candles ≥ 1× ATR are significant
- Hammer with 2× ATR shadow is much stronger than 0.5× ATR
- Marubozu covering ≥ 1.5× ATR is a very strong momentum signal

---

# SECTION G — VERDICT DECISION MATRIX

| Situation | Verdict | Direction |
|---|---|---|
| Fan ordered + expanding + BBs expanding | TRADE_NOW | with fan |
| Fan ordered + peaked + price at E55/E100 + retrace intact | TRADE_NOW | with fan (retrace entry) |
| Fan ordered + BB squeeze forming | WATCH | with fan, snipe at break |
| Fan flipped (E21 crosses opposite after trend) | TRADE_NOW | new direction (after confirmation) |
| Fan tangled/mixed (tim_teach_3 style) | SKIP | — |
| Chop/range with no break | SKIP | — |
| Named pattern at S/R with confluence | TRADE_NOW | pattern's direction |
| Named pattern at S/R counter-trend | WATCH | wait for confirmation |
| Named pattern in empty space | SKIP | — |

---

# SECTION H — HOW THE VALIDATOR USES THIS FILE

1. **Read chart left-to-right** → identify phase (consolidating / expanding / peaked / retracing).
2. **Name the dominant pattern** from sections A-D. Your CHART READ should say the pattern name.
3. **Check fishing-line state** — where is the rod? (loading / bending / max tension / snapped)
4. **Ground thresholds in current readings** — no absolute numbers from memory (see `ghost_validator_v1.md` GROUNDING RULE).
5. **Write snipe conditions** using the pattern's geometry:
   - W → neckline break at `max(close between lows)`
   - M → neckline break at `min(close between highs)`
   - BB squeeze → band pierce ≥ 0.5× current bandwidth
   - E100 retest → price within 0.08% of E100
6. **Confirm with candle signal** from `candle_patterns.py` (pre-computed field) — if provided, weight as microstructure confirmation.

If NONE of the patterns from sections A-D match what you see → **SKIP**. Better to skip than invent a thesis.

---

# META

**Image file inventory**: 46 total (19 teaching root + 17 numbered patterns + 10 generic charts)
**Source of descriptions**:
- `knowledge/collective/trading-knowledge/education/chart_patterns.md`
- `knowledge/collective/trading-knowledge/education/candlestick_patterns.md`
- `knowledge/collective/trading-knowledge/setup_knowledge.md`
- `Forex Trading Team/Prompts/validator_v4.md` (teaching narrative)
- Image filename self-labels (WIN/LOSS/pair/direction)

**Related files**:
- `Source/candle_patterns.py` — deterministic Python detector for candlestick patterns
- `Prompts/ghost_validator_v1.md` — the active 35B prompt (references this library by name)
- `Prompts/validator_v4.md` — Opus-path legacy prompt with full teaching narrative

**Known issues**:
- `trade_633_EUR_AUD_BUY_LOSS_-3p.png` is blank/transparent — regenerate
- `trade_641_EUR_AUD_BUY_LOSS_-5p.png` is blank/transparent — regenerate
- `chart_1.png` – `chart_10.png` specific pattern tagging is TODO (currently marked generic reference)
