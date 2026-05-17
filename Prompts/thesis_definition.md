# THE EMA FAN EXPANSION THESIS
## Canonical Definition — All Agents Must Use This

This is the **single source of truth** for how the thesis works. Every agent in the pipeline reads this the same way.

---

### TWO ENTRY PATHS

**PATH A — Sniper (Mean Reversion)**
- Finds extreme RSI/Stochastic readings at key levels
- Works AGAINST the current trend (counter-trend)
- Thesis CONFIRMS by checking: is the trend actually dying? (fan peaked/decelerating, momentum exhausted)

**PATH B — Thesis (EMA Fan Expansion / Trend Entry)**
- Finds fresh trend formation via EMA cross + fan development
- Works WITH the developing trend
- The EMA system IS the thesis — there is no separate "PATH C"

---

### THE THESIS — How a Trend Entry Develops (PATH B)

This is a **developing story**, not a snapshot. You watch it unfold bar by bar:

#### Step 1: TRIGGER — E21 crosses E55
- A bullish cross: E21 crosses ABOVE E55
- A bearish cross: E21 crosses BELOW E55
- This is the starting gun. Nothing happens until this fires.

#### Step 2: FAN ORDERING — All three EMAs stack correctly
- **BUY signal**: price > E21 > E55 > E100 (E100 on the bottom/outside)
- **SELL signal**: price < E21 < E55 < E100 (E100 on the top/outside)
- If the fan is NOT fully ordered, the thesis is NOT confirmed yet. Wait.

#### Step 3: E100 POSITIONING — E100 becomes support/resistance
- For BUY: E100 is BELOW price, acting as a support floor
- For SELL: E100 is ABOVE price, acting as a resistance ceiling
- E100 is the outer boundary of the fan. It defines the "wall" the trend pushes away from.
- If price breaks back through E100, the thesis is DEAD — abandon.

#### Step 4: FAN SEPARATION — Total fan width (E21 to E100) is GROWING
- **Fan width = abs(E21 - E100)** — the TOTAL distance across all three EMAs
- NOT just E21-E55 gap. The full fan from inside (E21) to outside (E100).
- This must be INCREASING bar over bar (fan is expanding/accelerating)
- If fan width starts shrinking, the move is fading

#### Step 5: BB EXPANSION — Bollinger Bands expanding SIMULTANEOUSLY
- BB width must be growing at the same time the fan is expanding
- This confirms that volatility is supporting the move, not just EMA drift
- BB expanding + fan expanding = real move
- BB contracting + fan expanding = suspicious, likely fakeout

#### ENTRY: When Steps 2-5 all align simultaneously
- Fan is ordered → E100 on the right side → fan width growing → BB expanding
- Minimum 10 bars after the cross (earlier entries are noise — backtested)
- Sweet spot: 15-25 bars after cross (highest win rate in backtesting)

#### ABANDON conditions:
- Price breaks through E100 (support/resistance broke)
- Fan width shrinks for 3+ consecutive bars (move is fading)
- Fan state goes to "contracting" or "peaked" (trend exhausted before entry)
- 30+ bars without all conditions aligning (stale cross, move over)

---

### WHAT EACH AGENT DOES WITH THE THESIS

**Scout**: Detects the E21/E55 cross trigger, checks if fan is developing toward full ordering. Creates alert when story score ≥40. Does NOT need all 5 steps confirmed — it's an early warning.

**TA (Technical Analyst)**: Reads the current state of the thesis story. Reports WHERE in the 5-step sequence we are. "Cross happened 12 bars ago. Fan is ordered. E100 at 1.0485 acting as support, 15 pips below price. Fan width 0.08% and growing. BB expanding with acceleration 0.015. Thesis is CONFIRMED — all conditions met."

**Validator**: Confirms or rejects the thesis. For PATH B entries:
- All 5 steps aligned → CONFIRM
- Fan ordered but BB not expanding → WATCH (wait for BB)
- Fan NOT ordered or E100 on wrong side → REJECT
- Strong DB evidence (85%+ WR, 500+ trades) can reinforce a borderline thesis

**Orchestrator**: Receives the verdict and sizes the trade. For PATH B entries, the sniper score is INFORMATIONAL ONLY — do not let sniper disagreement block a confirmed thesis entry.

---

### KEY METRICS FROM MARKET PICTURE DATA

The `generate_market_picture()` function provides these fields:
- `fan_ordered` — boolean, true when EMAs are in correct sequence
- `fan_state` — expanding / accelerating / stable / decelerating / peaked / contracting / just_crossed
- `fan_direction` — bullish / bearish / mixed
- `current_emas.ema21`, `.ema55`, `.ema100` — raw values for computing fan width
- `gap_21_55`, `gap_55_100` — individual gaps (fan width = sum of both for same-direction, or use abs(e21-e100))
- `ema100_role` — support / resistance / neutral
- `separation_velocity` — rate of fan expansion (%/bar)
- `fan_velocity_trend` — accelerating / stable / decelerating
- `trend_health` — 0-100 composite score

BB fields:
- `bb_expanding` — boolean
- `bb_acceleration` — rate of width change
- `bb_width` — current bandwidth

---

### WHAT THIS IS NOT

- This is NOT indicator stacking (checking RSI + MACD + Stoch independently)
- This is NOT a snapshot score (checking conditions at one moment)
- This IS a developing narrative — cross happens, then you WATCH the fan build
- The sniper (PATH A) and thesis (PATH B) are INDEPENDENT strategies that answer different questions
- During a fresh bullish EMA cross, the sniper WILL show overbought/sell — that's NORMAL, not a contradiction
