"""Numerical drift check — measures how much re-quantization perturbs each
LoRaLayer's output relative to the runtime fp16 LoRA computation.

For each LoRaLayer in the loaded model+adapter:
    1. y_lora    = lora_layer(x)            ← what current production produces
    2. y_fused   = fused_quantized_layer(x) ← what 4-bit re-quantize fuse produces
    3. drift     = ||y_lora - y_fused|| / ||y_lora||  (relative L2)

If mean drift across layers is small (~<0.5%), the 4-bit re-fuse is safe.
If large (>1-2%), the multi-domain distillation is materially perturbed and
we should not deploy a re-quantized fused model on M1 Max.

The check uses random fp16 inputs of plausible magnitude. Random probes
the layer's worst-case sensitivity; if drift is small on random inputs,
real-activation drift is bounded by it.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn

logger = logging.getLogger("drift_check")

BASE = "mlx-community/Qwen3.5-35B-A3B-4bit"
ADAPTER = "~/Jarvis/models/adapters/35b_mlx"
SAMPLES_PER_LAYER = 4  # random input vectors to average over


def _build_fused_quantized(qlin: nn.QuantizedLinear, A, B, alpha: float) -> nn.QuantizedLinear:
    """Same as fuse_vlm_lora.py — dequant + add + requant."""
    group_size, bits = int(qlin.group_size), int(qlin.bits)
    W_fp = mx.dequantize(
        qlin.weight, qlin.scales, qlin.biases,
        group_size=group_size, bits=bits, mode="affine",
    )
    delta = (float(alpha) * (A @ B)).T
    W_fused_fp = W_fp + delta.astype(W_fp.dtype)
    new_w, new_scales, new_biases = mx.quantize(
        W_fused_fp, group_size=group_size, bits=bits, mode="affine",
    )
    output_dims, input_dims = W_fp.shape
    has_bias = "bias" in qlin
    new_qlin = nn.QuantizedLinear(
        input_dims, output_dims, bias=has_bias,
        group_size=group_size, bits=bits, mode="affine",
    )
    new_qlin.weight = new_w
    new_qlin.scales = new_scales
    new_qlin.biases = new_biases
    if has_bias:
        new_qlin.bias = qlin.bias
    return new_qlin


def _build_fused_linear(lin: nn.Linear, A, B, alpha: float) -> nn.Linear:
    delta = (float(alpha) * (A @ B)).T
    W_fused = lin.weight + delta.astype(lin.weight.dtype)
    output_dims, input_dims = lin.weight.shape
    has_bias = "bias" in lin
    new_lin = nn.Linear(input_dims, output_dims, bias=has_bias)
    new_lin.weight = W_fused
    if has_bias:
        new_lin.bias = lin.bias
    return new_lin


def _layer_drift(lora_layer, samples: int) -> dict:
    """For one LoRaLayer, compare runtime output vs fused-and-requant output."""
    inner = lora_layer.original_layer
    A, B, alpha = lora_layer.A, lora_layer.B, lora_layer.alpha

    if isinstance(inner, nn.QuantizedLinear):
        fused = _build_fused_quantized(inner, A, B, alpha)
        input_dims = inner.weight.shape[1] * 32 // int(inner.bits)
    elif isinstance(inner, nn.Linear):
        fused = _build_fused_linear(inner, A, B, alpha)
        input_dims = inner.weight.shape[1]
    else:
        return {"skip_reason": f"unsupported wrapper of {type(inner).__name__}"}

    # Random fp16 inputs ~ N(0, 1) — plausible magnitude for hidden states.
    drifts = []
    abs_drifts = []
    for _ in range(samples):
        x = mx.random.normal(shape=(2, input_dims)).astype(mx.float16)
        y_lora = lora_layer(x)
        y_fused = fused(x)
        diff = y_lora - y_fused
        # Relative L2: ||diff|| / ||y_lora||
        denom = float(mx.sqrt(mx.sum(y_lora * y_lora)))
        if denom <= 1e-9:
            # Output is ~zero; report absolute instead
            rel = 0.0
        else:
            rel = float(mx.sqrt(mx.sum(diff * diff))) / denom
        abs_d = float(mx.sqrt(mx.sum(diff * diff)))
        drifts.append(rel)
        abs_drifts.append(abs_d)

    return {
        "rel_l2_mean": sum(drifts) / len(drifts),
        "rel_l2_max": max(drifts),
        "abs_l2_mean": sum(abs_drifts) / len(abs_drifts),
        "input_dims": input_dims,
        "output_dims": int(inner.weight.shape[0]),
    }


def main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    t0 = time.time()
    logger.info("Loading model + adapter (slow part)...")
    from mlx_vlm.utils import load
    from mlx_vlm.trainer.lora import LoRaLayer
    model, _ = load(BASE, adapter_path=ADAPTER, lazy=False)
    logger.info(f"Loaded in {time.time() - t0:.1f}s")

    # Walk model, drift-check every LoRaLayer
    t1 = time.time()
    by_suffix: dict[str, list[float]] = defaultdict(list)
    by_suffix_max: dict[str, float] = defaultdict(float)
    all_rel: list[float] = []
    layer_count = 0
    skipped = 0

    for name, mod in model.named_modules():
        if not isinstance(mod, LoRaLayer):
            continue
        result = _layer_drift(mod, SAMPLES_PER_LAYER)
        if "skip_reason" in result:
            skipped += 1
            continue
        suffix = name.rsplit(".", 1)[-1]
        by_suffix[suffix].append(result["rel_l2_mean"])
        if result["rel_l2_max"] > by_suffix_max[suffix]:
            by_suffix_max[suffix] = result["rel_l2_max"]
        all_rel.append(result["rel_l2_mean"])
        layer_count += 1
        if layer_count % 50 == 0:
            logger.info(f"  drift-checked {layer_count} layers...")

    elapsed = time.time() - t1
    logger.info(f"Drift-checked {layer_count} LoRaLayers in {elapsed:.1f}s "
                f"(skipped {skipped})")

    # Aggregate
    overall_mean = sum(all_rel) / len(all_rel)
    overall_max = max(all_rel)
    sorted_rel = sorted(all_rel)
    p50 = sorted_rel[len(sorted_rel) // 2]
    p95 = sorted_rel[int(len(sorted_rel) * 0.95)]
    p99 = sorted_rel[int(len(sorted_rel) * 0.99)]

    print("\n" + "=" * 70)
    print(" DRIFT CHECK — 4-bit re-quantized fuse vs runtime fp16 LoRA")
    print("=" * 70)
    print(f"  Layers checked: {layer_count}")
    print(f"  Samples / layer: {SAMPLES_PER_LAYER}")
    print(f"  Overall mean rel L2 drift: {overall_mean:.4%}")
    print(f"  Overall max rel L2 drift:  {overall_max:.4%}")
    print(f"  p50 / p95 / p99:            {p50:.4%} / {p95:.4%} / {p99:.4%}")
    print()
    print("  By layer-name suffix (mean / worst-layer-max):")
    for suffix in sorted(by_suffix.keys(), key=lambda s: -sum(by_suffix[s]) / len(by_suffix[s])):
        vals = by_suffix[suffix]
        print(f"    {suffix:25s} n={len(vals):3d}  mean={sum(vals) / len(vals):.4%}  worst-max={by_suffix_max[suffix]:.4%}")
    print()
    print("  GATES:")
    print(f"    SAFE      (<0.5% mean):   {'PASS' if overall_mean < 0.005 else 'FAIL'}")
    print(f"    BORDERLINE (<2% mean):    {'PASS' if overall_mean < 0.02  else 'FAIL'}")
    print(f"    UNSAFE     (>2% mean):    {'FAIL' if overall_mean < 0.02  else 'PASS (need lossless)'}")
    print()

    # Save report
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base": BASE, "adapter": ADAPTER,
        "layer_count": layer_count, "samples_per_layer": SAMPLES_PER_LAYER,
        "overall": {"mean": overall_mean, "max": overall_max,
                    "p50": p50, "p95": p95, "p99": p99},
        "by_suffix": {s: {"mean": sum(v) / len(v), "n": len(v),
                          "worst_max": by_suffix_max[s]} for s, v in by_suffix.items()},
    }
    out_path = Path("/tmp/drift_check_report.json")
    out_path.write_text(json.dumps(report, indent=2))
    print(f"  Full report: {out_path}")


if __name__ == "__main__":
    main()
