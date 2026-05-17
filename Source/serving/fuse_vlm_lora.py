"""Fuse a LoRA adapter into a 4-bit quantized VLM base model.

mlx_lm.fuse fails on VLMs because it expects text-only config (config.num_layers).
mlx_vlm doesn't ship its own fuse tool. mlx_vlm's LoRaLayer (in trainer/lora.py)
doesn't expose .fuse() like mlx_lm's LoRA class does. This script fills that gap.

Algorithm per LoRaLayer:
    original_layer = QuantizedLinear(weight, scales, biases, group_size=64, bits=4)
    LoRA: y = original(x) + alpha * (x @ A) @ B
    Equivalent fused weight: W_fused = W + alpha * (A @ B).T
    Steps: dequantize → add delta → re-quantize → replace LoRaLayer with new QuantizedLinear

Usage:
    python fuse_vlm_lora.py \\
        --base mlx-community/Qwen3.5-35B-A3B-4bit \\
        --adapter ~/Jarvis/models/adapters/35b_mlx \\
        --out ~/Jarvis/models/qwen35b-trading-fused

Output: a new model directory with fused weights + tokenizer/config copied from base.
The fused model is ~the same size as base (still 4-bit quantized).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn
from mlx.utils import tree_unflatten
from huggingface_hub import snapshot_download

logger = logging.getLogger("fuse_vlm_lora")


def _quant_params(qlinear: nn.QuantizedLinear) -> tuple[int, int]:
    """Return (group_size, bits) from a QuantizedLinear."""
    return int(qlinear.group_size), int(qlinear.bits)


def _fuse_lora_into_quantized(
    qlin: nn.QuantizedLinear,
    A: mx.array,
    B: mx.array,
    alpha: float,
) -> nn.QuantizedLinear:
    """Dequantize qlin, add LoRA delta, re-quantize, return a new QuantizedLinear.

    Forward equivalence: ``y = qlin(x) + alpha * (x @ A) @ B``
    becomes ``y = new_qlin(x)`` with the LoRA delta absorbed into the weight.
    """
    group_size, bits = _quant_params(qlin)

    W_fp = mx.dequantize(
        qlin.weight, qlin.scales, qlin.biases,
        group_size=group_size, bits=bits, mode="affine",
    )

    # Linear stores W as (output_dims, input_dims) and computes y = x @ W.T,
    # so the LoRA delta added to W is (alpha * (A @ B)).T.
    delta = (float(alpha) * (A @ B)).T  # (output_dims, input_dims)
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


def _fuse_lora_into_linear(
    lin: nn.Linear,
    A: mx.array,
    B: mx.array,
    alpha: float,
) -> nn.Linear:
    """Same as quantized fuse, but for plain (non-quantized) Linear layers."""
    delta = (float(alpha) * (A @ B)).T
    W_fused = lin.weight + delta.astype(lin.weight.dtype)
    output_dims, input_dims = lin.weight.shape
    has_bias = "bias" in lin
    new_lin = nn.Linear(input_dims, output_dims, bias=has_bias)
    new_lin.weight = W_fused
    if has_bias:
        new_lin.bias = lin.bias
    return new_lin


def _fuse_lora_layer(lora_layer) -> nn.Module:
    """Dispatch on the wrapped layer type."""
    inner = lora_layer.original_layer
    A, B = lora_layer.A, lora_layer.B
    alpha = lora_layer.alpha
    if isinstance(inner, nn.QuantizedLinear):
        return _fuse_lora_into_quantized(inner, A, B, alpha)
    if isinstance(inner, nn.Linear):
        return _fuse_lora_into_linear(inner, A, B, alpha)
    raise TypeError(
        f"Unsupported layer type wrapped by LoRaLayer: {type(inner).__name__}"
    )


def fuse(base: str, adapter_path: str, out: str) -> None:
    out_path = Path(out)
    out_path.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    logger.info("Loading base + adapter (this is the slow part)...")
    from mlx_vlm.utils import load, save_weights
    from mlx_vlm.trainer.lora import LoRaLayer
    model, _ = load(base, adapter_path=adapter_path, lazy=False)
    logger.info(f"Loaded in {time.time() - t0:.1f}s")

    t1 = time.time()
    fused_modules: list[tuple[str, nn.Module]] = []
    for name, mod in model.named_modules():
        if isinstance(mod, LoRaLayer):
            fused = _fuse_lora_layer(mod)
            fused_modules.append((name, fused))

    n_fused = len(fused_modules)
    if not n_fused:
        raise RuntimeError(
            "No LoRaLayer instances found. Adapter may not have applied — "
            "check apply_lora_layers behavior in mlx_vlm.utils."
        )
    logger.info(f"Fused {n_fused} LoRaLayers in {time.time() - t1:.1f}s")

    t2 = time.time()
    model.update_modules(tree_unflatten(fused_modules))
    logger.info(f"Updated module tree in {time.time() - t2:.1f}s")

    # save_weights forces tensor materialization on serialization.
    t3 = time.time()
    save_weights(out_path, model)
    logger.info(f"Saved weights in {time.time() - t3:.1f}s")

    # Copy support files (config.json, tokenizer.json, processor_config.json, etc.)
    # but NOT the original safetensors — we just wrote new ones.
    t4 = time.time()
    src_dir = Path(snapshot_download(base))
    skip_suffixes = {".safetensors", ".bin"}
    skip_files = {"model.safetensors.index.json"}
    copied = 0
    for entry in src_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix in skip_suffixes or entry.name in skip_files:
            continue
        shutil.copy2(entry, out_path / entry.name)
        copied += 1
    logger.info(f"Copied {copied} support files in {time.time() - t4:.1f}s")

    total_bytes = sum(p.stat().st_size for p in out_path.iterdir() if p.is_file())
    logger.info(f"Fused model written: {out_path}")
    logger.info(f"  Files: {len(list(out_path.iterdir()))}  Size: {total_bytes / 1024**3:.1f} GB")
    logger.info(f"Total wall-clock: {time.time() - t0:.1f}s")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base", default="mlx-community/Qwen3.5-35B-A3B-4bit")
    p.add_argument("--adapter", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not Path(args.adapter, "adapter_config.json").is_file():
        sys.exit(f"adapter dir missing adapter_config.json: {args.adapter}")
    if not Path(args.adapter, "adapters.safetensors").is_file():
        sys.exit(f"adapter dir missing adapters.safetensors: {args.adapter}")

    fuse(args.base, args.adapter, args.out)


if __name__ == "__main__":
    main()
