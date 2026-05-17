# 35B Validator — Pattern Library Audit (Design)

**Date:** 2026-04-24
**Author:** claude-code
**Status:** pending approval

## Purpose

Verify that the 35B local validator (Qwen3.5-35B-A3B-4bit + `35b_mlx` adapter) can read charts as well as or better than Opus, using the newly wired-in `pattern_library.md` as its pattern vocabulary.

Two recent changes are already live (serve_ui restarted 2026-04-24 10:45 ET):

1. **Pattern library wired in** (`e833c802`) — `Skills/pattern_library.md` (411 lines) added to `skill_files_local` for the validator config in `team_setup.py:317`.
2. **Candle detection fixed** (`00940133`) — `agents/wrappers.py:919` now scans last 3 M15 bars instead of only the current bar, so hammers/engulfings/stars from 1-2 bars ago show up in the `_v4_patterns_text` field.

We need to know whether the model actually uses the library + vision, not just whether the files are loaded.

## Non-goals

- Opus parity head-to-head. Skipped — we already have sufficient Opus benchmark data from prior sessions.
- Tuning the `MIN_CONFLUENCE` or any scout parameters. Pure model-quality audit.
- Measuring live watch P&L. That's a downstream metric once we know the model can read charts.

## Test plan

### Test 1 — Library ablation

**Question answered:** Does `pattern_library.md` change the model's output?

- **Inputs:** 10 teaching images under `Data/charts/teaching/patterns/` (`pattern_01_hammer_pin_bar.png` through `pattern_17_*.png` — a subset of 10 covering named patterns).
- **Procedure:** Call the 35B validator endpoint on each image twice: once with the current config (library loaded), once with the library removed from `skill_files_local` via an environment override (no restart needed if we can pass a per-call skill list — otherwise a temporary config swap + mini-restart).
- **Grade per image:**
  - `LIB_HIT` — verdict's `pattern` field uses a term that appears in `pattern_library.md`
  - `DIR_CORRECT` — direction matches the pattern's bias (hammer → BUY, shooting star → SELL, etc.)
- **Expected signal:** If library-OFF score >= library-ON score, the file is dead weight. Rip it out or compact.

### Test 2 — Real-trade WIN/LOSS replay (50 charts)

**Question answered:** Across many real historical trades, does the 35B with current stack correctly take wins and avoid losses?

- **Source:** `Data/charts/labeled/` (417 files, filename format `PAIR_direction_OUTCOME_pnlp_time.png`)
- **Sample:** 50 charts total. Stratified:
  - 25 WIN charts, 25 LOSS charts
  - Diversified across pairs (at least 4 pairs represented on each side)
  - Balanced BUY vs SELL (at least 10 of each direction on each side)
  - Seed = 42 for reproducibility
- **Procedure:** Each chart runs once through the current 35B stack via the same entry path `compute_sniper_score`-equivalent validator call. Output captured to `/tmp/stack_audit_2026-04-24_test2.log`.
- **Grade per chart:**
  - WIN chart → **PASS** if verdict ∈ {TRADE_NOW, WATCH} AND direction matches truth
  - LOSS chart → **PASS** if verdict == SKIP OR direction opposite to truth (would have avoided the loss)
- **Baseline:** Prior 8-chart bench scored 6/8 = 75%. Need this 50-chart bench to be within ±5pp of that or better.
- **Additional telemetry collected per chart:**
  - Latency
  - Pattern name returned
  - Confidence
  - Does the pattern name appear in `pattern_library.md`? (boolean)

### Test order

Test 1 first (cheap, fast, decides whether the library is load-bearing). If library-OFF beats library-ON by a meaningful margin (>10%), stop, report, and revisit design before running Test 2 — no point stress-testing a stack we already know is suboptimal.

## Deliverables

1. `/tmp/stack_audit_2026-04-24.md` — combined results with:
   - Test 1: 10 patterns × 2 runs grid (LIB_HIT / DIR_CORRECT / verdict)
   - Test 2: 50-chart grade table, pair breakdown, confusion matrix, pattern-naming stats
   - Summary: does the stack meet "good as Opus" bar?
2. One vault write to `knowledge/agents/claude-code/` with the session's findings
3. A follow-up audit pass using the `/trade-audit-repair` skill on the findings — proposes any prompt/guardrail changes. No changes ship without the skill-mandated backtest gate.

## Risks and edge cases

- **Heavy prompt regression** — if Test 1 shows the library has hurt accuracy (like the earlier v2/v3 benchmark regression from 75% → 50% when the benchmark harness had stripped USER input), we need to examine: is the regression because the harness is unrealistic, or because the prompt really is too heavy for the distilled 35B? If it's real regression in production, compact the library to ~80 lines covering the most common 12 patterns.
- **Pattern labeling uncertainty in `labeled/` filenames** — filename pnl is the eventual trade outcome, not a guarantee the setup at chart-time was a "should trade" moment. A LOSS chart might still be a legitimate TRADE_NOW setup that got stopped out for reasons unrelated to chart quality. This noise is acceptable at n=50.
- **Concurrency with live trading** — MLX server is busy with live validator calls; bench calls need to queue behind them. Not a blocker, just adds latency.
- **Skill list hot-swap** — if the validator config can't be swapped per-call for Test 1, we'll need to restart serve_ui with library removed, run the OFF pass, then restart with library re-added. Adds ~3 min overhead.

## Exit criteria

- Test 1 complete: clear answer to "does library help?"
- Test 2 complete: WIN/LOSS replay score across 50 real trades
- Audit report written
- `/trade-audit-repair` invoked on the findings — any proposed prompt/guardrail change has a backtest plan attached before code is touched
