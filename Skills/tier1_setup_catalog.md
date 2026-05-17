# Tier 1 Setup Catalog — Validator Reference

**Purpose:** When scout fires an alert with `alert_type` in (`C1_STOCH_EXTREME_BB`, `C3_RSI_DIV_GOLDEN`, `C4_CHART_PATTERN_BREAK`, `C5_FIB_REACTION`, `C8_TRIANGLE_BREAKOUT`, `C9_BEAR_EXP_PULLBACK`, `C11_BIG_MOVE`), this catalog tells you what the detector saw and how to judge the setup.

**Validator role unchanged:** You judge chart structure independently. Gates handle pair/time/news downstream — do not duplicate that work. Use this catalog to understand the detector's *thesis*, not as a replacement for your own judgment.

**Direction is set by the detector** (unlike V4 which leaves direction null). The detector tells you BUY or SELL. Your job: confirm the structural pattern is real.

---

## Catalog Format

Each setup has 4 sections:
- **REQUIRED** — what the detector saw (trigger conditions). All must be true for the alert to fire.
- **BONUS** — confirmations that lift confidence (boost score by 1 each, max +3).
- **ANTI-PATTERNS** — specific failure modes. If you see these, override toward SKIP/CAUTION even if REQUIRED looks good.
- **PERF** — backtest performance (90d × 14 pairs × production guardian exits). Pre-gate / post-gate WR.

---

## C1_STOCH_EXTREME_BB — Stochastic mean-reversion at BB band

**Thesis:** In a non-trending market, price has poked the BB outer band with stochastic at extreme. Now stoch is turning back. This is mean reversion, not trend continuation.

**Direction:** BUY at lower band oversold, SELL at upper band overbought.

**REQUIRED:**
- Prior bar: stoch_k ≥80 (sell) or ≤20 (buy)
- Prior bar: high poked BB upper (sell) or low poked BB lower (buy) — within 0.1% of band
- Current bar: stoch_k turning AWAY from extreme (lower than prior on sell, higher on buy)
- Current bar: candle color confirms direction (red on sell, green on buy)
- ADX < 22 (genuinely ranging — NOT a trend setup)
- BB width ≥ 3× ATR (BBs have real width, not flat)

**BONUS:**
- RSI also at extreme (>70 sell / <30 buy) and turning
- Price closed back inside the BB on the current bar
- Volume on the rejection bar elevated vs prior 5

**ANTI-PATTERNS:**
- Strong directional fan (E21/E55/E100 ordered + separating) — this is a trend, not a range. Mean reversion fails into trends.
- ADX rising — momentum building, not ranging
- Higher timeframe (H1/H4) shows breakout in the OPPOSITE direction of this trade
- News event within 30 minutes (volatility breaks ranges)

**PERF:**
- Backtest 90d: Pre-gate 84% WR, +1.4 avg pip. Post-gate **87.1% WR, PF 2.17, +2.20 avg pip** (gates filter half the false-positives, the survivors are elite). Most fires get blocked by `fan_exhaustion` or `ema_ordering_conflict` — when those gates pass, this is one of our highest-quality setups.
<!-- LIVE_PERF_START:C1_STOCH_EXTREME_BB -->
- Live 30d: pending — no closed trades yet
<!-- LIVE_PERF_END:C1_STOCH_EXTREME_BB -->
---

## C3_RSI_DIV_GOLDEN — RSI divergence at exhaustion

**Thesis:** Trend has been strong (ADX > 25) but is now weakening (ADX declining). Price made a new high/low but RSI didn't confirm. The trend is exhausting and reversing.

**Direction:** SELL at new high with RSI lower than prior peak. BUY at new low with RSI higher than prior trough.

**REQUIRED:**
- ADX declining and was > 25 within last 3 bars (trend was strong, now weakening)
- Within last 10 bars: price made a NEW high (sell) or NEW low (buy)
- RSI at this new extreme is LOWER than RSI at the prior peak (bearish div) — or HIGHER than prior trough (bullish div)
- Price near BB upper (sell) or lower (buy) band

**BONUS:**
- Stoch also showing divergence
- Reversal candlestick at the new extreme (shooting star, hammer, evening/morning star)
- Bearish/bullish engulfing on the new extreme bar
- Round number / prior swing level acting as resistance/support

**ANTI-PATTERNS:**
- Fan still actively expanding in trend direction — divergence at peak expansion often fails (trend hasn't truly exhausted)
- RSI already extreme but not yet diverged — wait for confirmation
- ADX still rising or just rising again — trend may be re-accelerating
- Choppy/sideways structure for last 20+ bars (no real trend to reverse)

**PERF:**
- Backtest 90d: Pre-gate 84% WR, +2.4 avg pip. Post-gate **91.7% WR, PF 3.68, +4.92 avg pip on the few that survive (n=24/1172)**. Most fires get blocked by `fan_exhaustion` (the gate doesn't yet understand exhaustion entries). When fully cleared, **this is our highest-quality setup**.
<!-- LIVE_PERF_START:C3_RSI_DIV_GOLDEN -->
- Live 30d: pending — no closed trades yet
<!-- LIVE_PERF_END:C3_RSI_DIV_GOLDEN -->

**Special note for validator:** This is a counter-fan setup. Production gates assume "fan not expanding = no trade", which is the OPPOSITE of this thesis. If you see a C3 alert that gets blocked by fan_exhaustion in flight log, that's expected — the gate works, but our backtest shows the survivors are elite. Treat C3 alerts that DO reach you with extra weight.

---

## C4_CHART_PATTERN_BREAK — Double-top / double-bottom break

**Thesis:** Price formed two peaks (or troughs) at approximately the same level within the last 30 bars. The neckline (trough between two peaks, or peak between two troughs) has now broken. Classical reversal pattern.

**Direction:** SELL on double-top break (close below the trough). BUY on double-bottom break (close above the peak).

**REQUIRED:**
- Within last 30 bars: two peaks within 0.3×ATR of each other, at least 5 bars apart (sell)
- OR two troughs within 0.3×ATR of each other, at least 5 bars apart (buy)
- Current bar closes BELOW the lowest point between the two peaks (sell)
- OR closes ABOVE the highest point between the two troughs (buy)

**BONUS:**
- Volume spike on the breakout bar
- Strong body candle (body ≥ 70% of range) on breakout
- The neckline coincides with a prior swing high/low (multi-timeframe support)
- After the break, price retests the broken neckline as resistance/support and respects it

**ANTI-PATTERNS:**
- The pattern is inside a strong opposing trend (double-top forming inside an uptrend that's still expanding) — likely a continuation pause, not a reversal
- The "second peak" was actually a higher high (real new high, not a double-top)
- Breakout bar is a doji or small body — weak conviction
- News-driven spike that doesn't follow through on the next bar

**PERF:**
- Backtest 90d: Pre-gate 83.4% WR, +2.4 avg pip. Post-gate **83.1% WR, PF 1.74, +2.60 avg pip (n=903 survivors)**. ~33% gate-pass rate. Quality holds well after gates.
<!-- LIVE_PERF_START:C4_CHART_PATTERN_BREAK -->
- Live 30d: **65.0% WR** (13W/7L), -0.3p, +$81.42, streak +1
<!-- LIVE_PERF_END:C4_CHART_PATTERN_BREAK -->
---

## C5_FIB_REACTION — Fib retracement entry

**Thesis:** After a swing in one direction, price has pulled back to a Fibonacci retracement level (38.2%, 50%, or 61.8%) and shown a reversal candle. Continuation entry.

**Direction:** BUY if EMA21 > EMA100 (uptrend bias) and bullish reversal at fib. SELL if EMA21 < EMA100 (downtrend) and bearish reversal at fib.

**REQUIRED:**
- 30-bar swing: difference between high and low > 0
- Current price within 0.5×ATR of fib 38.2 / 50 / 61.8 of that swing
- Reversal candle: close > prior bar's HIGH (bull reversal) or close < prior bar's LOW (bear reversal)
- EMA21 > EMA100 for buy entry, or EMA21 < EMA100 for sell entry (trend bias matches direction)

**BONUS:**
- Multi-fib confluence (multiple fib levels stacked at the same price)
- Fib 61.8 (the golden ratio) — strongest level historically
- Volume on the reversal candle elevated
- The fib level coincides with E55 or E100

**ANTI-PATTERNS:**
- Price has already broken below 78.6% retracement — likely full reversal, not pullback
- Reversal candle has small body and large opposing wick — weak rejection
- ADX is rising in the OPPOSITE direction of the planned trade — momentum reversing
- Inside a sideways range (no clear swing to retrace)

**PERF:**
- Backtest 90d: Pre-gate 84.3% WR, +2.3 avg pip. Post-gate **84.6% WR, PF 1.64, +2.24 avg pip (n=583 survivors)**. ~18% gate-pass rate. Most fires blocked by `fan_exhaustion` (fib retracements happen during pullbacks when fan is stable, not expanding). When gates pass, quality holds.
<!-- LIVE_PERF_START:C5_FIB_REACTION -->
- Live 30d: **85.7% WR** (6W/1L), -19.2p, $-150.79, streak +5
<!-- LIVE_PERF_END:C5_FIB_REACTION -->
---

## C8_TRIANGLE_BREAKOUT — Consolidation breakout

**Thesis:** Price has been consolidating in a tightening range (later bars have less range than earlier bars). Now it has broken out beyond the consolidation high or low. Classic squeeze-to-breakout.

**Direction:** BUY on close above 20-bar high. SELL on close below 20-bar low.

**REQUIRED:**
- 20-bar range ≤ 6×ATR (true consolidation, not just a normal range)
- Late 10 bars range < 0.85 × early 10 bars range (range is actively tightening)
- Current close BREAKS above the 20-bar high (buy) or below the 20-bar low (sell)

**BONUS:**
- Strong-body candle on breakout (body ≥ 70% of range)
- BB squeeze (BB width contracted, then expanding on the breakout)
- Volume spike on breakout bar
- Multi-timeframe: H1 also showing similar squeeze pattern

**ANTI-PATTERNS:**
- The "breakout" is a single-bar spike that closes back inside the range — false breakout
- High-impact news caused the spike (likely to reverse on the next bar)
- The triangle apex is far away (consolidation hasn't fully tightened) — premature breakout
- Breakout direction conflicts with the prior dominant trend (less reliable than continuation breakouts)

**PERF:**
- Backtest 90d: Pre-gate 82.4% WR, +1.8 avg pip. Post-gate **82.6% WR, PF 1.47, +1.75 avg pip (n=889 survivors)**. ~52% gate-pass rate.
<!-- LIVE_PERF_START:C8_TRIANGLE_BREAKOUT -->
- Live 30d: **0.0% WR** (0W/3L), -44.8p, $-211.71, streak -3
<!-- LIVE_PERF_END:C8_TRIANGLE_BREAKOUT -->
---

## C9_BEAR_EXP_PULLBACK — Pullback in trending fan

**Thesis:** Fan is fully ordered and trending. A small counter-direction pullback bar happened (e.g., a small green bar in a bear trend). Now the next bar is closing back in trend direction, BELOW E21 (or above E21 for buy). The pullback failed, trend resumes.

**Direction:** SELL when E21 < E55 < E100 (bearish fan) and trend resumes after pullback. BUY mirrors for E21 > E55 > E100.

**REQUIRED:**
- For SELL: E21 < E55 < E100 (ordered bearish fan)
- Prior bar: bullish green candle BUT body < 60% of range (small counter-pullback, not strong)
- Prior bar's high ≤ E21 × 1.001 (the pullback did NOT break E21 from below)
- Current bar: red, close < E21 (trend resuming)
- For BUY: mirror — E21 > E55 > E100, prior bar small bear pullback, current closes above E21 in green

**BONUS:**
- ADX > 25 (trend strength confirmed)
- Fan separating (Δ5bar in trend direction)
- E55 directly below current price holding as nearest support (sell) or resistance (buy)
- Stoch oversold (sell) — momentum extreme suggests pullback exhausted

**ANTI-PATTERNS:**
- Pullback bar broke E21 by more than 1.001× — pullback may turn into reversal
- Pullback bar had body > 60% of range — strong counter-move, not a normal pullback
- ADX falling — trend losing energy, pullback may continue
- Multiple consecutive pullback bars — trend may be exhausting

**PERF:**
- Backtest 90d: Pre-gate 83.4% WR, +2.2 avg pip. Post-gate **83.0% WR, PF 1.60, +2.19 avg pip (n=2389 survivors)**. ~53% gate-pass rate. **Highest absolute net pips in the catalog (+5,231 over 90 days × 14 pairs).** This is the dominant live-winner archetype.
<!-- LIVE_PERF_START:C9_BEAR_EXP_PULLBACK -->
- Live 30d: **56.7% WR** (17W/13L), -53.1p, $-288.36, streak -2
<!-- LIVE_PERF_END:C9_BEAR_EXP_PULLBACK -->
---

## C11_BIG_MOVE — Trending continuation with momentum

**Thesis:** Strong, ordered fan with high ADX. MACD histogram aligned with trend. Current bar continues trend direction with body closing beyond E21. Pure momentum continuation.

**Direction:** BUY when E21 > E55 > E100, ADX ≥ 28, MACD hist > 0, green candle closing above E21. SELL is the mirror.

**REQUIRED:**
- ADX ≥ 28 (strong trend, not weak)
- For BUY: E21 > E55 > E100 (bullish fan), MACD histogram > 0, current bar green, close > E21
- For SELL: E21 < E55 < E100 (bearish fan), MACD histogram < 0, current bar red, close < E21

**BONUS:**
- ADX rising (strengthening trend)
- Fan separation widening (Δ5bar in trend direction)
- MACD histogram bars getting larger
- BB expanding in trend direction
- Higher timeframe (H1/H4) trending same direction

**ANTI-PATTERNS:**
- Fan starting to compress (Δ5bar near 0 or going negative) — trend stalling
- Price extended >1.5× ATR away from E21 — late entry, exhaustion risk
- RSI at extreme (>80 buy / <20 sell) without prior pullback — too late
- BB starting to contract in middle of move — trend losing energy

**PERF:**
- Backtest 90d: Pre-gate 81.9% WR, +2.0 avg pip. Post-gate **82.0% WR, PF 1.48, +1.98 avg pip (n=1454 survivors)**. ~89% gate-pass rate (highest in the catalog). Almost no false positives at the gate stage — this is fan-aligned trend continuation, exactly what gates expect.
<!-- LIVE_PERF_START:C11_BIG_MOVE -->
- Live 30d: **66.7% WR** (6W/3L), -23.9p, $-325.69, streak +4
<!-- LIVE_PERF_END:C11_BIG_MOVE -->
---

## How to Use This Catalog in Your Verdict

When you receive a scout alert with one of the Tier 1 alert_types:

1. **Read the alert_type** — that's the detector that fired. Direction is set.
2. **Pull the matching section above** — understand what the detector SAW.
3. **Verify on the chart**: Are the REQUIRED conditions visible in the live chart now? (Scout fired ~30-90s ago — chart may have moved.)
4. **Score**:
   - All REQUIRED visible + matching the section → start at confidence 7
   - Each BONUS visible → +1 (max +3 → confidence 10)
   - Any ANTI-PATTERN visible → -2 or SKIP entirely
5. **Issue verdict**:
   - 8-10 → TRADE_NOW (with snipe_entry/invalidation/target as usual)
   - 6-7 → WATCH (set re-entry conditions on the missing REQUIRED items)
   - ≤5 → SKIP (clear thesis breakdown)

**Important:** The 10-point V4 thesis still applies to `V4_CRITERIA_MET` / `V4_EARLY_WARNING` alerts. This catalog is *supplementary* — only use it when alert_type is one of the 7 Tier 1 names above.

---

## Summary Table

| Alert | Direction logic | Best regime | Post-gate WR | Post-gate PF | Notes |
|---|---|---|---|---|---|
| C1_STOCH_EXTREME_BB | Stoch + BB cross-reversal | Ranging (ADX<22) | 87.1% | 2.17 | Highest PF after gates |
| C3_RSI_DIV_GOLDEN | RSI divergence at peak | Trend exhausting | **91.7%** | **3.68** | Highest WR — but most blocked by gates |
| C4_CHART_PATTERN_BREAK | DT/DB neckline break | Reversal | 83.1% | 1.74 | Classical pattern |
| C5_FIB_REACTION | Fib + reversal candle + EMA bias | Pullback in trend | 84.6% | 1.64 | Most fires blocked, survivors solid |
| C8_TRIANGLE_BREAKOUT | Range tightening + breakout | Consolidation→trend | 82.6% | 1.47 | Watch for false breakouts |
| C9_BEAR_EXP_PULLBACK | Failed pullback in fan | Trending | 83.0% | 1.60 | Highest net pips contributor |
| C11_BIG_MOVE | ADX≥28 + fan + MACD aligned | Strong trend | 82.0% | 1.48 | Cleanest gate-pass (89%) |

All performance numbers from 90-day × 14-pair backtest using production guardian exit params, walk-forward 8-fold validation. See `agents/claude-code/learnings.md` and `Source/scripts/setup_signal_backtest.py` for full evidence.
