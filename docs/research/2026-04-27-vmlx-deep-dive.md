# vMLX Deep Dive — Architecture, Adapter Integration, Distillation Pipeline

**Date:** 2026-04-27
**Researcher:** Claude (main session, after sub-agent permissions block)
**Subject:** Read vMLX 1.3.11 source on disk, document what we'd need to do to migrate
**Status:** Research only — no code modified

All file paths under `~/myenv/lib/python3.10/site-packages/vmlx_engine/` unless noted.

---

## TL;DR Decision Matrix

| Path | Effort | Risk | What we keep | What we lose | Recommendation |
|---|---|---|---|---|---|
| **A — Add `--adapter-path` to vMLX (small fork)** | ~1 day | Low | Cache, batching, our adapter, tool calling | Maintenance of fork | **High — this is the right answer** |
| **B — Manual fuse pipeline** | ~3-4 hrs initial + ~5 min per retrain | Medium (re-quantization is lossy) | Cache, batching, vMLX upstream | LoRA flexibility (always re-fuse to test changes) | Medium — viable backup |
| **C — Port vMLX cache into our wrapper** | 1-2 weeks | Medium-high (it's 14K lines of cache+scheduler+batch_generator) | Our wrapper, tool calling | Continuous batching (would also have to port) | Low — too much code |
| **D — Wait for upstream LoRA support** | 0 (just monitoring) | Indefinite | Everything if it lands | Real benefit timeline unclear | Low — passive |

**Headline:** Path A is one line of model loading code plus CLI plumbing. The adapter loading machinery already exists in `mlx_vlm.utils.load()` — vMLX just doesn't expose the parameter.

---

## Section 1 — Cache architecture (the prize we're trying to inherit)

### The 4-tier cache hierarchy

```
┌─ L0 (vision):  VisionEmbeddingCache    — projected image features by pixel hash
├─ L1 (paged):   PagedCacheManager       — KV blocks in unified memory, content-addressed
├─ L2 (disk):    DiskPromptCache         — SQLite-indexed safetensors snapshots on disk
└─ Mamba state:  Per-layer checkpoint    — Mamba/DeltaNet recurrent state (no trim, only restore)
```

### L1: PagedCacheManager (`paged_cache.py`)

- **Block size:** 64 tokens default (configurable via `--paged-cache-block-size`).
- **Content-addressed:** Each block has a `BlockHash` chained from its parent (`paged_cache.py:43-51` — "Chain hashing for prefix caching... hash depends on parent block"). Two requests sharing a system prompt share *identical* block-hash chain for those tokens, so the cache hits at the block boundary.
- **Allocation:** Free list is a doubly-linked list, O(1) LRU eviction (`paged_cache.py:13-18`).
- **Reference counting:** Blocks have `ref_count` (`paged_cache.py:117`) — multiple in-flight requests can share read-only blocks. Copy-on-Write when one needs to extend.

This is exactly the prefix-cache primitive we wanted, generalized to the block level so it's robust to multi-request and continuous-batching scenarios.

### L2: DiskPromptCache (`disk_cache.py`)

- Built on **mlx-lm's `save_prompt_cache` / `load_prompt_cache`** (`disk_cache.py:5-6, 232-233`).
- SQLite index keyed by `_hash_tokens(tokens)` (line 41).
- Background writer thread — `store()` is non-blocking (`disk_cache.py:14, 95`).
- **Survives server restart.** This is "warm validator on cold boot" — the system prompt KV stays on disk between sessions.

When we tested vMLX with `--enable-disk-cache --disk-cache-dir /tmp/vmlx-disk-cache`, the log line `Disk cache initialized: dir=/tmp/vmlx-disk-cache/Qwen3.5-35B-A3B-4bit_fc94ed7ca25d` confirmed it created a per-model directory. Restart-survival is real.

### L0: VisionEmbeddingCache (`mllm_batch_generator.py:863-972`)

- Two-level cache: `max_pixel_entries` (raw pixel hash → resized tensor) + `max_encoding_entries` (encoded image embeddings).
- Vision tower runs ONCE per unique image — subsequent requests with the same chart skip the vision encoder entirely.
- Default size = 100 entries.
- Hashing: by image **pixels** (not file bytes), so cropped/resized variants of the same chart can share cache.

**For our use case:** during a 15-min M15 cycle, the same chart may be passed to multiple agents (TA narrator, validator, scout). Vision cache means only the first call pays the encoder cost.

### Hybrid model handling — the key insight

`mllm_scheduler.py:80-89` is explicit:
> "Required for hybrid models (auto-switches from memory-aware)... others use MambaCache/ArraysCache (SSM/linear attention). This creates a [problem]... `_truncate_hybrid_cache()` to trim generation tokens, [but Mamba state can't be trimmed]"

vMLX's solution: paged cache uses **block-level checkpoints** of *all* per-layer state — including Mamba — and restores rather than trims. The scheduler at line 409 auto-detects hybrid and forces paged mode. Our test logs showed exactly this:
```
Auto-switching VLM to paged cache for hybrid model
(memory-aware cache can't truncate MambaCache)
```

This is what makes prefix caching architecturally correct for Qwen3.5 — and confirms the agent's research from earlier today that trim-style caching (mlx-lm's default) silently breaks on our model.

**Recommendation:** This is the architecture we want. Migrating to vMLX inherits 14k+ lines of well-considered cache infrastructure for free.

---

## Section 2 — Adapter integration paths (the blocker)

### Where the model is loaded — exactly

`vmlx_engine/models/mllm.py:722-770` is the entry point. The relevant bit:

```python
def load(self) -> None:
    ...
    from mlx_vlm import load                                       # line 748
    from mlx_vlm.utils import load_config
    ...
    self.model, self.processor = load(self.model_name, lazy=_lazy)  # line 762
    self.config = load_config(self.model_name)
    self._loaded = True
```

That call to `mlx_vlm.load()` already accepts `adapter_path` as a parameter (we verified — it's the same function our existing `mlx_vlm_server_with_tools.py` uses). **vMLX simply doesn't pass it.**

### The minimal patch (Path A)

Three changes, ~10-15 lines total:

1. **`vmlx_engine/cli.py`** — add `--adapter-path` argument (~3 lines).
2. **`vmlx_engine/server.py`** — propagate `adapter_path` from CLI args into the MLLM config (~3 lines around line 814 `load_model()`).
3. **`vmlx_engine/models/mllm.py:762`** — change to `load(self.model_name, adapter_path=self.adapter_path, lazy=_lazy)` (~2 lines including the `__init__` accept).

That's it. The mlx-vlm side already does `apply_lora_layers(model, adapter_path)` internally (`mlx_vlm.utils.load`).

### What about the LoRA stub at `worker.py:222`?

```python
# LoRA methods (not yet supported on MLX)
def add_lora(self, lora_request) -> bool:
    logger.warning("LoRA not yet supported on MLX backend")
    return False
```

This is for vLLM's **multi-LoRA-at-inference** API (different LoRA per request). We don't need that — we use **one** static adapter loaded at server boot. The patch above bypasses this entirely; we'd never call `add_lora()`.

### Cache-key concern

If the cache hashed only token content, two servers with different adapters would silently share blocks → wrong outputs. Inspecting `paged_cache.py:43`'s `compute_block_hash` function would confirm whether adapter_id is in the key. **Open item — read the function before deploying.** Most likely OK because each vMLX process loads exactly one model+adapter set; cross-contamination only matters in multi-tenant LoRA-hot-swap, which we don't have.

**Recommendation: Path A — fork vMLX, add the flag, ship.**

---

## Section 3 — Fuse pipeline (Path B fallback)

Our LoRA shape from `mlx_vlm/trainer/lora.py`:

```python
class LoRaLayer(nn.Module):
    self.original_layer  # QuantizedLinear (weight, scales, biases) — 4-bit, group_size=64
    self.A               # (input_dims, rank=32)  — fp16
    self.B               # (rank=32, output_dims) — fp16
    self.alpha = 64.0    # scale
    # Forward: y = original_layer(x) + alpha * (x @ A) @ B
```

### 3a — Manual quantized fuse (~50 lines, lossy)

```python
def fuse_lora_into_quantized(layer):
    qlin = layer.original_layer
    # 1. Dequantize: returns fp16 weight matrix
    W_fp = mx.dequantize(qlin.weight, qlin.scales, qlin.biases,
                         group_size=64, bits=4)
    # 2. Add LoRA delta. Forward is y += alpha * (x @ A) @ B
    #    = x @ (alpha * A @ B), so weight delta is (alpha * A @ B).T
    delta = (layer.alpha * (layer.A @ layer.B)).T
    W_fused = W_fp + delta
    # 3. Re-quantize
    new_w, new_scales, new_biases = mx.quantize(W_fused, group_size=64, bits=4)
    # 4. Replace
    new_layer = nn.QuantizedLinear(...)
    new_layer.weight = new_w; new_layer.scales = new_scales; new_layer.biases = new_biases
    return new_layer
```

Walk all LoRaLayers, replace each with the fused QuantizedLinear, save_weights.

**Risk:** re-quantization rounds twice — once when the base was quantized originally, once after fusion. Per-tensor MSE drift is small but cumulative across 40 layers × ~7 LoRA-bearing modules each = ~280 fused tensors. Could shift validator behavior in unpredictable ways. Need a parity test against the runtime-adapter version on a regression set before shipping.

### 3b — Dequantized fuse (lossless but huge)

Same as 3a but skip step 3. Output is fp16 weights → ~70 GB on disk and in memory. **Won't fit in 64 GB unified memory for serving on M1 Max.** Mac Studio M3 Ultra 256 GB could host it. Trade-off: lossless quality but kills your concurrency budget.

### 3c — Mixed (not recommended)

Serve quantized base + small fp16 "delta" model that runs alongside. Architecturally messy, not standard.

**Recommendation: Path B is a viable backup if Path A blocks. The re-quantization risk needs a parity test gate.**

---

## Section 4 — Distillation → serving pipeline (the future-Tim concern)

### Monthly retrain workflow with Path A (vMLX with adapter flag)

```
1. Train new LoRA → produces new adapters.safetensors + adapter_config.json (~64 MB)
2. Stage: copy adapter into models/adapters/35b_mlx_v{N+1}/
3. Validate on regression set (parity vs current adapter on N golden charts)
4. SIGTERM vMLX → it flushes disk cache for the OLD adapter
5. Restart vMLX with --adapter-path .../35b_mlx_v{N+1}/
6. Disk cache rebuilds for the new adapter (first call is slow, subsequent fast)
7. Watchdog detects vMLX is up → trading resumes
```

Total downtime: ~30-60 seconds (process restart + model+adapter load).

### Hot-swap question

`server.py:131` — `_cli_args: dict = {}  # Saved CLI args for model reload on wake`. There IS a `def load_model()` at line 814. This implies vMLX can reload a model in-place (used for vLLM-compatible "sleep/wake" mode). **Could potentially be re-purposed for adapter hot-swap** — call load_model() with new adapter_path. **Open item — read the load_model() signature before relying on this.**

If hot-swap works, downtime drops to ~5-10 seconds (just the model+adapter reload, no process restart).

### Monthly retrain workflow with Path B (fuse pipeline)

```
1. Train new LoRA
2. Run fuse script (~5 min): produces models/qwen35b-trading-fused-v{N+1}/
3. Parity check on regression set
4. Restart vMLX pointed at new fused model
5. Disk cache rebuilds (different model = different cache namespace)
```

Same downtime as Path A. Slightly more steps. Re-quantization risk recurs every retrain.

**Recommendation: Path A's pipeline is cleaner. Re-fusing every month is operationally fine but adds a quality-drift surface.**

---

## Section 5 — What we lose by retiring `mlx_vlm_server_with_tools.py`

Our wrapper is 318 lines. Audit:

| Wrapper feature | vMLX equivalent | Gap? |
|---|---|---|
| `--adapter-path` flag | NONE (the blocker) | **Yes — Path A fixes** |
| `--max-kv-size` | Implicit via paged cache config | None |
| Pin model+adapter at launch | Same with adapter flag added | None |
| OpenAI-compat `/v1/chat/completions` | Native | None — vMLX has it |
| OpenAI-compat `/v1/models` | Native | None |
| Qwen3 `<tool_call>` parsing | `vmlx_engine/api/tool_calling.py` + `--tool-call-parser qwen3` | None — already supported |
| `gc.collect() + mx.metal.clear_cache()` after each request | vMLX has its own cache hygiene (paged manager handles eviction) | Probably better off without — that wrapper code was fighting mlx_vlm's lack of a real cache |
| Tool list injection in Qwen3.5 system prompt format | `--enable-auto-tool-choice` + parser | None |
| Anthropic protocol adapter | `vmlx_engine/api/anthropic_adapter.py` | Bonus — Open Claw could hit Anthropic-shaped endpoint |
| Ollama protocol adapter | `vmlx_engine/api/ollama_adapter.py` | Bonus |

**Net:** vMLX is a strict superset of our wrapper *once Path A adds adapter loading.* We retire 318 lines, gain 14K of well-engineered serving code, get the cache + batching for free.

---

## Open Items (read before shipping)

1. **`paged_cache.py:43-90` `compute_block_hash` definition** — does it include adapter_id in the key? If yes, no concern. If no, adding adapter support upstream needs to also update the hash to prevent cross-contamination in multi-LoRA scenarios (we don't need it, but a clean upstream PR should).
2. **`server.py:814 def load_model(...)` signature** — does it accept arbitrary kwargs we could pass adapter_path through? Confirms whether hot-swap is feasible without a process restart.
3. **`mllm_batch_generator.py:863-1000` exact vision cache hit/miss semantics** — does it cache the vision tower output before or after the language-model embedding projection? Affects whether re-running a chart with a different question hits cache (we observed it does, in our test).
4. **vMLX `--api-key` and rate limiting** — we may want the gateway to be authoritative on tenant priorities; vMLX rate limiting could conflict.
5. **Memory under sustained load** — our test peaked at 23 GB but only ran 4 requests. Need a soak test (50+ requests with vision) before declaring the budget safe.

---

## ⚠️ ADDENDUM 2 — 2026-04-27 late evening — drift check results

**The 4-bit re-fuse path is empirically unsafe for our adapter.** Ran `Source/serving/drift_check.py` against all 390 LoRaLayers. Each layer measured: drift between runtime fp16 LoRA output vs fused-and-requantized 4-bit output, on random fp16 inputs.

```
Layers checked: 390
Mean rel L2 drift:    3.47%   (target was <0.5%)
Max rel L2 drift:    18.73%
p50 / p95 / p99:      3.45% / 3.90% / 5.97%

Worst:
  shared_expert_gate    mean 3.60%   single-layer max  36.17%   ← MoE expert routing
  in_proj_a             mean 3.59%   single-layer max   6.14%
  up_proj               mean 3.48%   single-layer max   3.83%
```

**Mean ~7× the safety threshold.** A single MoE-router layer at 36% drift would reroute tokens to wrong experts — exactly the kind of perturbation that wrecks the multi-domain distillation.

This is fundamental quantization arithmetic, not a vMLX bug. RunPod with re-quantize step would produce the same number. **No 4-bit-out fuse is viable for this adapter.**

## Decision: pivot to full fine-tune for v3

Given:
- 4-bit re-fuse: drift too high (this addendum)
- fp16 deploy 70 GB: violates Tim's 4-bit footprint constraint (`feedback_keep_4bit_footprint`)
- Runtime adapter overlay: incompatible with vMLX continuous batching (`Stream(gpu, 3)` bug)
- Upstream vMLX fix: indefinite timeline

The path that actually unlocks vMLX cache + batching for our distillation is to **change the next training run's strategy from LoRA to full fine-tune**:

```
v2 (current):                          v3 (proposed):
  base_4bit + LoRA on top                full FT of base_fp16
  → adapter overlay at runtime           → single trained model
  → can't fuse cleanly (drift)           → quantize ONCE to 4-bit (clean)
  → vMLX batched broken                  → vMLX serves natively
```

v3 also lets us bundle the trading-bias reweight (TRADE_NOW/WATCH/SKIP → 40/10/50 + wins×3) per `feedback_distillation_reweight`. Two problems, one training run.

### v3 hardware reality

```
Memory: ~430 GB total (fp16 weights + AdamW + gradients + activations)
M1 Max 64 GB: insufficient
Mac Studio 256 GB: insufficient (still need GPU sharding)
RunPod 4× H100 80GB: viable, ~6-12h training, ~$100-300
RunPod 8× A100 80GB: viable, ~8-16h training, ~$100-250
End-to-end (data prep + setup + training + iteration + quant + validate): 2-3 days
```

Until v3 lands, **stay on current production** (`mlx_vlm_server_with_tools.py` + adapter at runtime). The vMLX migration unlocks once we have a v3 single-artifact model.

### Path A (vmlx_with_adapter.py wrapper) — what to do with it

**Reference artifact only — not deployable as a stop-gap.** Subsequent test (2026-04-27 ~21:30) revealed the Metal stream affinity bug ALSO fires in **Simple mode when the request includes an image**:

```
Simple + adapter + text-only:           ✅ works (validated earlier)
Simple + adapter + image (validator):   ❌ Stream(gpu, 1) — same class of bug
Continuous batching + adapter + ANY:    ❌ Stream(gpu, 3)
Either mode + base model only:          ✅ works
```

Our production workload is image-bearing (chart in every validator call). All four LoRA-bearing cells are broken. **vMLX cannot serve our adapter at all today; only base-model traffic works.**

The wrapper is kept as a reference for when:
1. Upstream vMLX fixes the threading bug (mlx_vlm.generate.wired_limit's `mx.synchronize(s)` failing under asyncio.to_thread), OR
2. v3 lands as a full-FT single-artifact model that doesn't need adapter loading at all

---

## ⚠️ ADDENDUM 2026-04-27 evening — live test results

**Path A (adapter loading) was implemented and tested via monkey-patch wrapper at `Source/serving/vmlx_with_adapter.py`. Findings:**

- ✅ Monkey-patch successfully injects `adapter_path` into `mlx_vlm.load()` without forking vMLX (lazy import in `models/mllm.py:748` makes this clean)
- ✅ Simple mode + adapter generates real distilled-validator output (23.9s, references our trained terminology like "fishing line", "BB squeeze breakout")
- ❌ **Continuous batching + adapter fails every prefill:** `"There is no Stream(gpu, 3) in current thread"` — content=null returned. Root cause: vMLX's batched paged-cache generator routes Metal state on a specific stream; LoRaLayer's `__call__` produces intermediate tensors on a different stream, and cache state submission breaks.

**Conclusion:** Path A alone gives us the adapter but loses continuous batching + prefix cache. Path A + Path B (fuse) is the only combination that delivers all three: distilled validator + cache + batching. **Path B (fuse pipeline) is now the critical path.**

The Path A wrapper is still useful as a **fallback** (Simple mode + adapter is a marginal improvement over current production), and as a reference artifact for the eventual upstream vMLX bug report (LoRaLayer + continuous-batching incompatibility).

---

## Original recommendation (revised)

**Was:** Path A — patch vMLX to accept `--adapter-path`. *(Insufficient on its own, see addendum.)*

**Now:** **Path B (manual quantized fuse) is the right answer.** Three files, ~10-15 lines, ~1 day of work including testing.

This gives us:
- Our distilled adapter on every request (no quality regression vs current)
- 180× prefix-cache speedup we measured today, but on full validator workload
- Continuous batching for cross-tenant parallelism (Open Claw + trading + boardroom can all be in flight simultaneously up to `max_num_seqs`)
- Vision feature cache (re-using charts within a cycle is free)
- Disk L2 (validator stays warm across restarts)
- Tool calling, Anthropic protocol, Ollama protocol — all free

The fork burden is small because vMLX's adapter integration is genuinely a missing-feature-not-architectural-mismatch. We can also upstream the patch — single-adapter-at-load-time is a plausibly accepted PR.

Path B (manual fuse) is the fallback if Path A reveals a problem. Path C (port cache to our wrapper) is over-engineering. Path D (wait) is passive.

**Next session's plan:** prototype Path A in a vMLX fork, run our 5-chart parity test against the current adapter-runtime production. If parity holds and the cache works, we have a migration path to plan.
