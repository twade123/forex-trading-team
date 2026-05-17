# MLX VLM Caching — Research & Options
**Date:** 2026-04-27
**Researcher:** Claude (research-agent)
**Subject:** Cross-request prompt cache for Qwen3.5-VL-35B-A3B-4bit on M1 Max
**Status:** Research only — no code changes

---

## TL;DR

- The 40 s prefill-per-call cost is real. `mlx_vlm.server` (≤ 0.4.4) has **no cross-request prompt cache**. It only caches *vision features by image hash* (LRU=8) — text KV state is rebuilt every call. The "warmer" approach is dead because `generate.py` and our wrapper actively call `mx.clear_cache()` after each generation.
- A working text-prompt cache **landed in mlx-vlm PR #995** ("feat: prompt prefix caching with TTL eviction and TurboQuant support", opened 2026-04-09 by `eloe`) but is **still open**, not yet in 0.4.4. ([PR list](https://github.com/Blaizzy/mlx-vlm/pulls), [issue #344](https://github.com/Blaizzy/mlx-vlm) tracking).
- **Major architectural blocker for our model**: Qwen3.5 uses a hybrid GDN/Mamba + attention design. mlx-lm's prefix cache *silently falls back to full recompute* on any model with Mamba/SSM/sliding-window layers ([mlx-lm #980](https://github.com/ml-explore/mlx-lm/issues/980)). The standard `--prompt-cache-size` LRU trim approach **will not work** for Qwen3.5-VL-MoE without a checkpoint/restore implementation.
- Two third-party servers already solve our problem on Apple Silicon: **vMLX** ([jjang-ai/vmlx](https://github.com/jjang-ai/vmlx)) — explicitly the "only MLX engine where VLs work with the full caching stack: prefix cache + paged KV + KV quant + continuous batching + persistent disk L2"; and **vllm-mlx** ([waybarrios/vllm-mlx](https://github.com/waybarrios/vllm-mlx)) — content-hash prefix cache reports 28× speedup on repeated images, TTFT 28 s → 0.3 s on cache hit.
- The cleanest in-tree path is to **port LM Studio's `VisionCacheWrapper`** ([AirRunner/mlx-engine `feat/vlm-image-kv-cache`](https://github.com/lmstudio-ai/mlx-engine/issues/287)) — it uses **checkpoint/restore** (not trim), which is the only approach that works for hybrid-attention VLMs.

---

## Findings

### 1. mlx-vlm changelog & in-flight work
- Latest stable on PyPI: **0.4.4** (released ~2026-04-04). Headline change is `VisionFeatureCache` — LRU(8) over **projected vision features keyed by image path/hash**, so the vision tower runs once per unique image. **It does not cache the LM's text KV state.** Used automatically by chat UIs and the server. ([releases](https://github.com/Blaizzy/mlx-vlm/releases), [v0.4.4 notes](https://github.com/Blaizzy/mlx-vlm))
- 0.3.10 → 0.4.x adds Jina VLM, Molmo2, Gemma 4, qwen3_vl_moe kwargs fix.
- **Tracking issue [#344](https://github.com/Blaizzy/mlx-vlm)**: persistent prompt cache for mlx-vlm — open, intermittent activity.
- **PR [#995](https://github.com/Blaizzy/mlx-vlm/pulls)** (2026-04-09, `eloe`): "feat: prompt prefix caching with TTL eviction and TurboQuant support". **Open / unmerged as of today.** This is the closest thing to a native fix shipping upstream. Maintainer's stance: receptive but not yet merged.
- **Issue [#832](https://github.com/Blaizzy/mlx-vlm/issues/832)** (cross-turn image KV cache for Qwen3.5): the vision tower re-runs every turn; needs `image_end_index` and `get_partial_input_embeddings` plumbed through `qwen3_5_vl_moe.py`. Open.
- **Confirmed:** `generate.py` calls `mx.clear_cache()` 8+ times per generate; our wrapper adds `gc.collect() + mx.metal.clear_cache()`. No KV state survives even within a single warmer process. (Our own inspection.)

### 2. Open issues / PRs around caching, batching, perf
- [#344](https://github.com/Blaizzy/mlx-vlm) prompt cache, [#832](https://github.com/Blaizzy/mlx-vlm/issues/832) cross-turn image KV, [#995](https://github.com/Blaizzy/mlx-vlm/pulls) prefix cache PR — all listed above.
- Downstream fallout: [opencode #21419](https://github.com/anomalyco/opencode/issues/21419) — exact same complaint as ours, system prompt re-prefilled every call against `mlx_vlm.server`. Confirms this is a known broken behaviour, not a misconfiguration on our end.
- Maintainer (Blaizzy) is one person; throughput on PRs is slow. Don't bet on #995 landing on a known timeline.

### 3. mlx-lm `--prompt-cache-size` — is it portable?
- mlx-lm's server (`mlx_lm.server`) keeps an `LRUPromptCache` keyed by prompt-token prefix. On a hit it **trims** the saved cache to the longest common prefix and prefills only the suffix. Refactored in PR #1019; now lives near `mlx_lm/models/cache.py` with `make_prompt_cache`, `cache_history`, `trim_prompt_cache`. ([repo](https://github.com/ml-explore/mlx-lm), [HTTP server doc](https://deepwiki.com/ml-explore/mlx-lm/3.3-http-server))
- The primitives **are reusable in principle** — `make_prompt_cache(model)` walks the model's layer list and creates per-layer cache objects. It does not assume "language-only" — it assumes the model exposes `model.layers` with attention modules.
- **Critical gotcha for our case ([mlx-lm #980](https://github.com/ml-explore/mlx-lm/issues/980)):**
  > "Prompt prefix caching only works for pure full-attention models. Any model using sliding window attention, Mamba/SSM layers, or mixed attention types silently falls back to full recomputation."
  - Sliding-window layers use `RotatingKVCache` (circular buffer) — can't be meaningfully trimmed.
  - Mamba/SSM layers have recurrent state that integrates all history — **not trimmable at an arbitrary token boundary**.
  - **Qwen3.5-A3B uses Gated DeltaNet (a Mamba variant) interleaved with attention layers**, per the Qwen team's own [model card](https://huggingface.co/Qwen/Qwen3.5-35B-A3B) and [vLLM recipes](https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3.5.html).
  - **Implication:** even if we manually wired `make_prompt_cache` into `mlx_vlm.server`, on Qwen3.5-VL it would either silently full-recompute, or worse, return wrong logits.
- The path that **does** work is **checkpoint/restore** instead of trim — see #4 / #8 below.

### 4. Existing wrappers & alternative servers
| Project | Repo | What it gives us | Status |
|---|---|---|---|
| **vMLX** | [jjang-ai/vmlx](https://github.com/jjang-ai/vmlx) (`pip install vmlx`) | "Only MLX engine where VL works with full caching stack: prefix cache + paged KV + KV quant + continuous batching + persistent disk L2." Live-verified on Holo3-35B-A3B (same A3B class as ours). | Active, on PyPI |
| **vllm-mlx** | [waybarrios/vllm-mlx](https://github.com/waybarrios/vllm-mlx) | OpenAI + Anthropic API, vLLM-style continuous batching, content-hash prefix cache for VLs. Reports **TTFT 28 s → 0.3 s on cache hit** on 33 K-token contexts; 28× speedup on repeated images; 5.8× on shared text prefixes. | Active, on PyPI |
| **mlx-engine** (LM Studio) | [lmstudio-ai/mlx-engine](https://github.com/lmstudio-ai/mlx-engine) + fork [AirRunner/mlx-engine `feat/vlm-image-kv-cache`](https://github.com/lmstudio-ai/mlx-engine/issues/287) | `VisionCacheWrapper`: LRU of **checkpoint** snapshots taken right after the image token block; restore + prefill only the new text suffix. Architecturally compatible with hybrid models. | LM Studio uses production; fork PR pending |
| **cubist38/mlx-openai-server** | [repo](https://github.com/cubist38/mlx-openai-server) | OpenAI-compatible FastAPI over mlx + mlx-vlm. No special cache work — same prefill cost as us. | Drop-in alternative, doesn't solve our problem |
| **SharpAI/SwiftLM** | [repo](https://github.com/SharpAI/SwiftLM) | Native Swift; TurboQuant KV cache compression. LLM-only, no VL. | Not applicable |
| **jundot/omlx** | [repo](https://github.com/jundot/omlx) | SSD-paged cache; LLM-only at the moment. | Not applicable |

### 5. Continuous batching on MLX
- vLLM-style continuous batching with PagedAttention exists on Apple Silicon today via **vllm-mlx** and **vmlx** (both ship paged-cache + continuous-batching primitives in pure MLX). vllm-mlx benchmarks: 1.5–3× throughput at 5 concurrent, 4.3× at 16 concurrent.
- Apple's own `ml-explore/mlx` has **no native PagedAttention** yet. Tracked in [mlx #2955](https://github.com/ml-explore/mlx/issues/2955) and the [vllm-metal RFC #188](https://github.com/vllm-project/vllm-metal/issues/188). Both are open.
- For our gateway use case (one validator at a time, queue depth ≥ 1 occasionally), continuous batching is a *nice-to-have* — the dominant cost is the 13–17 K prefill, not concurrency.

### 6. MLX core 0.31.x serving features
- 0.31.0/0.31.1/0.31.2 (currently installed): "Fix precision in Metal fused attention", "[CUDA] Attention sinks in cuDNN SDPA", various unified-memory and quantisation tweaks. ([mlx releases](https://github.com/ml-explore/mlx/releases))
- **No paged-attention primitive, no native KV-cache manager, no batched-prefill kernel** at the core level. Everything serving-shaped lives one layer up (in mlx-lm / mlx-vlm / third-party).
- [Discussion #3203](https://github.com/ml-explore/mlx/discussions/3203) ("oMLX — paged SSD caching for coding agents") is the most active recent thread on serving-shaped features. Apple is *aware*; nothing committed.

### 7. Other Mac-native VLM serving options
- **LM Studio** (uses `mlx-engine` under the hood): ships `VisionFeatureCache` for images **plus** `VisionCacheWrapper` checkpoint cache for text prefixes against VLs. ([Unified MLX engine blog](https://lmstudio.ai/blog/unified-mlx-engine), [issue #287](https://github.com/lmstudio-ai/mlx-engine/issues/287)). Caveat: KV cache reported broken on a recent LM Studio build ([bug #1319](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1319)) — verify before adopting.
- **Ollama**: caches KV state per loaded model and reuses on byte-identical prefix, but only while the model is in memory. Default `keep_alive=5m` will dump it; set `keep_alive=-1`. **Vision support exists** but KV-quant on vision is flagged experimental and quality-degrading. ([Ollama FAQ](https://docs.ollama.com/faq), [multimodal blog](https://ollama.com/blog/multimodal-models))
- **llama.cpp + Metal**: continuous batching (`-cb`) works for text, but [the docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md) and Hacker News confirm vision multimodal does **not** participate in continuous batching, and prefix cache reuse for VLs is not first-class. Would also require a GGUF Qwen3.5-VL-35B-A3B that may not exist or perform on par.
- **None** of these is a *zero-code* drop-in for the MoE-A3B 4-bit Qwen3.5-VL we're running. vMLX is closest because it lists Holo3-35B-A3B (same class) as live-verified.

### 8. Architectural feasibility for our specific case
**Question:** Can we cache the LM's KV state after the system prompt and only prefill (image tokens + new user text) per request?

**Answer:** Yes in principle, no with a trim-style cache, **yes with a checkpoint-style cache** — and someone has already implemented exactly that.

**Why trim fails on Qwen3.5-VL:** the LM has interleaved GDN (Mamba-like) and full-attention layers. Mamba state is recurrent — you can't trim back to "after the system prompt" because the state at any later position has integrated everything since. mlx-lm currently doesn't even detect this and silently full-recomputes ([#980](https://github.com/ml-explore/mlx-lm/issues/980)).

**Why checkpoint works:** instead of trimming, you take a *full snapshot* of every layer's cache (KV tensors **and** Mamba states) at a known offset (end of system prompt, or end of system+image block). On the next request you `restore()` that exact snapshot and feed only the new text suffix forward. State is consistent because you never partially-updated it. This is what:
- LM Studio's `VisionCacheWrapper` does (commit `feat/vlm-image-kv-cache` in [AirRunner/mlx-engine](https://github.com/lmstudio-ai/mlx-engine/issues/287)).
- vLLM does for LLaVA / Qwen-VL — [HF discussion](https://discuss.huggingface.co/t/multimodal-prefix-caching-with-qwen3-vl/170849).
- vMLX claims to do for VLs end-to-end ([README](https://github.com/jjang-ai/vmlx)).

For our case the checkpoint boundary is slightly subtle: our system prompt is *before* the image, so the system-prompt-only checkpoint is image-independent and image-prompt-independent — we can pre-compute it once at server startup and reuse forever. Image tokens + user text are then the only per-request prefill. Expected gain: ~80–90 % of the 40 s prefill eliminated, since the system prompt is the bulk of the 13–17 K tokens.

---

## Revised Options Table

| # | Path | Effort | Risk | Expected gain | Dependencies / unknowns |
|---|---|---|---|---|---|
| **A** | **Switch to vMLX** (`pip install vmlx`, point gateway at `vmlx serve mlx-community/Qwen3.5-VL-35B-A3B-...`) | **S** (1–2 days incl. validator regression) | M (newer project; verify our exact 4-bit MoE quant loads + matches outputs) | High — full caching stack, paged KV, continuous batch, persistent L2 | Verify A3B-4bit runs at our quality bar; verify token-perfect parity vs current model |
| **B** | **Switch to vllm-mlx** | **S** (similar to A) | M (Anthropic-API surface differs from ours; need shim) | High — published 28 s→0.3 s TTFT on 33 K context (very close to our scenario) | Same model-load verification; check Qwen3.5-VL-MoE explicitly listed |
| **C** | **Port `VisionCacheWrapper` (checkpoint cache) into `mlx_vlm.server`** as a local fork | **L** (1–2 weeks) | M-H (Mamba/GDN checkpoint serialisation is non-trivial; need to land [#832](https://github.com/Blaizzy/mlx-vlm/issues/832)-style hooks in `qwen3_5_vl_moe.py`) | High | Must remove `mx.clear_cache()` calls in `generate.py`; need stable checkpoint API across mlx-vlm versions |
| **D** | **Wait for / cherry-pick PR [#995](https://github.com/Blaizzy/mlx-vlm/pulls)** ("prompt prefix caching with TTL") | **S** to apply, **?** to verify on hybrid arch | H — PR uses a prefix-cache approach; **may silently fall back** on Qwen3.5-VL-MoE per [#980](https://github.com/ml-explore/mlx-lm/issues/980) | Conditional — works if PR uses checkpoint, fails if it uses trim. Need to read PR diff. | Read the PR; verify on our model before deploying |
| **E** | **Switch to LM Studio backend** | M (replace mlx_vlm.server with LM Studio + its OpenAI endpoint) | M ([bug #1319](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1319) — KV cache regression in recent build) | High when working | Validate current LM Studio build's MLX KV cache before committing |
| **F** | **Status quo + better warmer (impossible)** | — | — | — | Ruled out. `mx.clear_cache()` everywhere. Warmer is theatre. |

S=small, M=medium, L=large.

---

## Recommendation

**Two-step plan, low blast radius:**

1. **This week — spike Option A (vMLX) in parallel with prod.** Stand up vMLX on a different port, load `Qwen3.5-VL-35B-A3B-4bit`, run the validator's golden set against it, compare:
   - Output token-equality against current 0.4.4 (cap 5 % drift).
   - Cold first-call TTFT.
   - Warm second-call TTFT (this is the prize — should be < 2 s).
   - Memory under load.
   If parity holds and warm TTFT lands < 5 s, **migrate the gateway to point at vMLX**. Single config change, biggest win for least effort. The "live-verified on Holo3-35B-A3B" claim ([vMLX README](https://github.com/jjang-ai/vmlx)) directly maps to our model class.

2. **If A regresses output quality** (most likely cause: different sampling defaults or a subtly different MoE router path), fall back to **Option C (in-tree checkpoint port)**. The reference implementation in [AirRunner/mlx-engine `feat/vlm-image-kv-cache`](https://github.com/lmstudio-ai/mlx-engine/issues/287) is well-documented and the LM Studio team has already done the hard work of figuring out where to put the checkpoint boundary for Qwen-class VLs. Strip the `mx.clear_cache()` calls in our forked `generate.py`, build a `VisionCacheWrapper`-equivalent keyed on `hash(system_prompt_tokens)`, persist one checkpoint at server start.

**Do not bet on Option D (PR #995).** Maintainer responsiveness is unpredictable, and the PR may use a trim-based approach that silently breaks on Qwen3.5's Mamba layers. Read the diff before considering.

**Do not invest in Option F (warmer improvements).** The architecture forbids it.

---

## Open Questions / Needs More Investigation

1. **Read PR #995's diff.** Determine whether it uses *trim* (broken for us) or *checkpoint* (works). 30 minutes of work; would change the table above significantly. Recommended next step regardless of which option we pick.
2. **Confirm vMLX supports our exact quant** — does `pip install vmlx` actually load `mlx-community/Qwen3.5-VL-35B-A3B-Instruct-4bit` (or whatever HF repo our weights came from)? README claims A3B works; need empirical load test.
3. **Memory headroom on M1 Max 64 GB.** Adding paged-KV + checkpoint state on top of a 35B-A3B-4bit model (~22 GB resident) and a vision tower may push us close to the swap line if we keep multiple checkpoints. Budget: aim for ≤ 1 system-prompt checkpoint + 1 in-flight request.
4. **Quality regression risk.** Our flight recorder pins validator behaviour — any backend swap must reproduce the same pip-by-pip verdicts on a regression set before going live.
5. **GDN / Mamba state checkpoint serialisation.** For Option C: confirm `mlx_vlm.models.qwen3_5_vl_moe` exposes the GDN state via the same per-layer cache interface mlx-engine assumes, or whether we need a custom snapshot path.

---

## Sources

**mlx-vlm**
- [Releases · Blaizzy/mlx-vlm](https://github.com/Blaizzy/mlx-vlm/releases)
- [Pull requests · Blaizzy/mlx-vlm](https://github.com/Blaizzy/mlx-vlm/pulls) (PR #995)
- [Issue #832 — cross-turn image KV cache with Qwen3.5](https://github.com/Blaizzy/mlx-vlm/issues/832)
- [mlx-vlm on PyPI](https://pypi.org/project/mlx-vlm/)
- [Repo README](https://github.com/Blaizzy/mlx-vlm)

**mlx-lm**
- [mlx-lm repo](https://github.com/ml-explore/mlx-lm)
- [Issue #980 — Prefix cache reuse broken for hybrid models](https://github.com/ml-explore/mlx-lm/issues/980)
- [HTTP server reference (DeepWiki)](https://deepwiki.com/ml-explore/mlx-lm/3.3-http-server)

**mlx core**
- [Releases · ml-explore/mlx](https://github.com/ml-explore/mlx/releases)
- [Issue #2955 — FlashAttention/PagedAttention proposal](https://github.com/ml-explore/mlx/issues/2955)
- [Discussion #3203 — oMLX paged SSD caching](https://github.com/ml-explore/mlx/discussions/3203)
- [vllm-metal RFC #188 — Paged attention as MLX primitive](https://github.com/vllm-project/vllm-metal/issues/188)

**Alternative servers**
- [waybarrios/vllm-mlx](https://github.com/waybarrios/vllm-mlx) — continuous batching, prefix cache, OpenAI+Anthropic API
- [vllm-mlx benchmarks](https://github.com/waybarrios/vllm-mlx/blob/main/docs/benchmarks/llm.md)
- [HN: vLLM-mlx — 65 tok/s on Mac with prompt caching](https://news.ycombinator.com/item?id=47162364)
- [jjang-ai/vmlx](https://github.com/jjang-ai/vmlx) — "only MLX engine with full caching stack for VLs"
- [vmlx on PyPI](https://pypi.org/project/vmlx/)
- [cubist38/mlx-openai-server](https://github.com/cubist38/mlx-openai-server)
- [SharpAI/SwiftLM](https://github.com/SharpAI/SwiftLM)
- [jundot/omlx](https://github.com/jundot/omlx)
- [jvines/python-mlx-server](https://github.com/jvines/python-mlx-server)

**LM Studio / mlx-engine**
- [Unified multi-modal MLX engine blog](https://lmstudio.ai/blog/unified-mlx-engine)
- [LM Studio 0.3.4 — ships MLX](https://lmstudio.ai/blog/lmstudio-v0.3.4)
- [Issue #287 — Cross-turn KV cache for VisionModelKit](https://github.com/lmstudio-ai/mlx-engine/issues/287) (refers to AirRunner fork)
- [Bug #1319 — KV caching broken on recent LM Studio build](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1319)

**Other Mac runtimes**
- [Ollama FAQ — KV reuse, keep_alive](https://docs.ollama.com/faq)
- [Ollama multimodal models blog](https://ollama.com/blog/multimodal-models)
- [llama.cpp multimodal docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)
- [llama.cpp #8860 — KV cache across requests sharing prefix](https://github.com/ggml-org/llama.cpp/discussions/8860)

**Architectural / model-specific**
- [Qwen3.5-35B-A3B model card](https://huggingface.co/Qwen/Qwen3.5-35B-A3B) — GDN + attention hybrid
- [Qwen3-VL repo](https://github.com/QwenLM/Qwen3-VL)
- [vLLM Qwen3.5 recipe](https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3.5.html)
- [HF forum — Multimodal prefix caching with Qwen3-VL](https://discuss.huggingface.co/t/multimodal-prefix-caching-with-qwen3-vl/170849)
- [vLLM automatic prefix caching design](https://docs.vllm.ai/en/stable/design/prefix_caching/)
- [opencode #21419 — same problem against mlx_vlm.server](https://github.com/anomalyco/opencode/issues/21419) (confirms broken behaviour upstream)

---

**Confidence breakdown**
- mlx-vlm 0.4.4 has no text prompt cache: **HIGH** (confirmed by release notes, opencode #21419, and our own `generate.py` inspection).
- Qwen3.5 hybrid arch breaks trim-style cache: **HIGH** ([mlx-lm #980](https://github.com/ml-explore/mlx-lm/issues/980), Qwen model card).
- vMLX supports our use case: **MEDIUM** (README + PyPI claim "live-verified on Holo3-35B-A3B"; have not personally tested with our exact weights).
- vllm-mlx benchmarks (28 s → 0.3 s TTFT): **MEDIUM** (project's own benchmarks; not independently reproduced).
- LM Studio `VisionCacheWrapper` is checkpoint-based and would work for hybrid arch: **MEDIUM-HIGH** (issue #287 + AirRunner fork describe the design clearly; not personally read the diff).
- PR #995 will fix our problem: **LOW** (open, unread; may use trim approach that breaks on Qwen3.5).
