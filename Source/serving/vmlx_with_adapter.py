"""vmlx-with-adapter — thin wrapper that adds --adapter-path to `vmlx serve`.

vMLX 1.3.x doesn't expose adapter loading on its CLI even though the underlying
`mlx_vlm.load()` accepts an `adapter_path` argument. This wrapper monkey-patches
`mlx_vlm.load` to always inject our adapter path, then calls vMLX's CLI normally.

The monkey-patch works because vmlx_engine/models/mllm.py imports `from mlx_vlm
import load` *inside* its load method (lazy resolution at call time). We patch
mlx_vlm.load before the engine starts loading the model.

Usage (drop-in replacement for `vmlx serve`):
  python -m serving.vmlx_with_adapter \\
      mlx-community/Qwen3.5-35B-A3B-4bit \\
      --adapter-path ~/Jarvis/models/adapters/35b_mlx \\
      --is-mllm --continuous-batching --enable-prefix-cache \\
      --port 11502 --host 127.0.0.1

All other flags pass through to `vmlx serve` unchanged.

Safe across `pip install vmlx --upgrade` — no vMLX source files modified.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

logger = logging.getLogger("vmlx_with_adapter")


def _parse_adapter_path(argv: list[str]) -> tuple[str | None, list[str]]:
    """Pull --adapter-path / VMLX_ADAPTER_PATH out of argv. Returns (adapter_path, remaining_argv).

    We do NOT use argparse with `parse_known_args` against the full vmlx-serve
    flag set because we'd risk stealing flags vmlx wants. Instead we scan for
    our specific flag and remove it surgically.
    """
    adapter_path = os.environ.get("VMLX_ADAPTER_PATH")
    remaining = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--adapter-path":
            if i + 1 >= len(argv):
                raise SystemExit("--adapter-path requires a value")
            adapter_path = argv[i + 1]
            i += 2
            continue
        if a.startswith("--adapter-path="):
            adapter_path = a.split("=", 1)[1]
            i += 1
            continue
        remaining.append(a)
        i += 1
    return adapter_path, remaining


def _install_monkey_patch(adapter_path: str) -> None:
    """Replace mlx_vlm.load so every call injects our adapter_path.

    Note: vmlx_engine/models/mllm.py uses `from mlx_vlm import load` inside its
    load method. Each call re-resolves the name from the mlx_vlm module, so
    swapping mlx_vlm.load before model load takes effect.
    """
    import mlx_vlm

    if not os.path.isdir(adapter_path):
        raise SystemExit(f"--adapter-path is not a directory: {adapter_path}")
    if not os.path.isfile(os.path.join(adapter_path, "adapter_config.json")):
        raise SystemExit(
            f"--adapter-path missing adapter_config.json: {adapter_path}"
        )

    _orig_load = mlx_vlm.load

    def _patched_load(path_or_hf_repo, adapter_path=None, lazy=False,
                      revision=None, **kwargs):
        # If caller explicitly passed adapter_path, honor it. Otherwise inject ours.
        effective_adapter = adapter_path or _PATCH_STATE["adapter_path"]
        logger.info(
            "[vmlx_with_adapter] mlx_vlm.load(%s, adapter_path=%s, lazy=%s)",
            path_or_hf_repo, effective_adapter, lazy,
        )
        return _orig_load(
            path_or_hf_repo,
            adapter_path=effective_adapter,
            lazy=lazy,
            revision=revision,
            **kwargs,
        )

    mlx_vlm.load = _patched_load
    _PATCH_STATE["adapter_path"] = adapter_path
    _PATCH_STATE["installed"] = True
    logger.info("[vmlx_with_adapter] monkey-patch installed; adapter_path=%s",
                adapter_path)


_PATCH_STATE = {"adapter_path": None, "installed": False}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    adapter_path, remaining = _parse_adapter_path(sys.argv[1:])

    if adapter_path:
        _install_monkey_patch(adapter_path)
    else:
        logger.warning(
            "[vmlx_with_adapter] no --adapter-path given (and VMLX_ADAPTER_PATH "
            "not set); vMLX will load the base model without our distillation"
        )

    # Forward to vMLX CLI. Reconstruct argv as `vmlx <subcommand> ...`. The
    # original argv had the script name + flags; vMLX's main() expects the
    # first element to be the program name, then the subcommand (`serve`).
    # The user invokes us as `python -m serving.vmlx_with_adapter <subcommand> ...`
    # so `remaining` already starts with the subcommand.
    sys.argv = [sys.argv[0]] + remaining

    from vmlx_engine.cli import main as vmlx_main  # type: ignore[import-not-found]
    vmlx_main()


if __name__ == "__main__":
    main()
