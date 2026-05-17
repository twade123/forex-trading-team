#!/usr/bin/env python3
"""MLX LoRA trainer for continuous model fine-tuning.

Triggers MLX LoRA training when sufficient new training pairs are collected.
Runs training as background subprocess to avoid blocking trading operations.

Models:
- ta_9b: Qwen3.5-9B-4bit (technical analyst)
- trade_monitor_35b: Qwen3.5-35B-A3B-4bit (trade monitor)

Training params:
- 100 iterations per run
- LoRA adapters saved to ~/jarvis/models/adapters/<model_key>/
- Training logs to /tmp/lora_train_<model_key>.log
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("trading_bot.lora_trainer")

# Paths
TRAINING_DATA_DIR = Path.home() / "jarvis" / "models" / "training_data"
ADAPTER_DIR = Path.home() / "jarvis" / "models" / "adapters"
TRAINING_STATE_FILE = Path.home() / "jarvis" / "models" / "training_state.json"

# Training thresholds
MIN_NEW_PAIRS = 50  # Minimum new pairs before triggering training
TRAINING_ITERS = 100  # LoRA iterations per training run

# Model configurations
MODEL_CONFIGS = {
    "ta_9b": {
        "hf_repo": "mlx-community/Qwen3.5-9B-4bit",
        "training_file": TRAINING_DATA_DIR / "ta_9b_training.jsonl",
        "adapter_path": ADAPTER_DIR / "ta_9b",
        "log_file": "/tmp/lora_train_ta_9b.log",
        "min_new_pairs": MIN_NEW_PAIRS,
        "iters": TRAINING_ITERS,
    },
    "trade_monitor_35b": {
        "hf_repo": "mlx-community/Qwen3.5-35B-A3B-4bit",
        "training_file": TRAINING_DATA_DIR / "trade_monitor_35b_training.jsonl",
        "adapter_path": ADAPTER_DIR / "trade_monitor_35b",
        "log_file": "/tmp/lora_train_trade_monitor_35b.log",
        "min_new_pairs": MIN_NEW_PAIRS,
        "iters": TRAINING_ITERS,
    },
    "trevor_35b": {
        "hf_repo": "mlx-community/Qwen3.5-35B-A3B-4bit",
        "training_file": Path.home() / "jarvis/training_data/sessions/session_training.jsonl",
        "adapter_path": ADAPTER_DIR / "trevor_35b",
        "log_file": "/tmp/lora_train_trevor_35b.log",
        "min_new_pairs": 100,  # Higher threshold — session data is diverse
        "iters": 150,
    },
    "validator_35b": {
        "hf_repo": "mlx-community/Qwen3.5-35B-A3B-4bit",
        "training_file": TRAINING_DATA_DIR / "validator_35b_training.jsonl",
        "adapter_path": ADAPTER_DIR / "validator_35b",
        "log_file": "/tmp/lora_train_validator_35b.log",
        "min_new_pairs": 50,
        "iters": 100,
    },
    # THE one model — all sources combined (Trevor + Claude Code + trading team +
    # boardroom + vault). Built by scripts/build_combined_dataset.py.
    # After training: fuse → GGUF → Ollama. Then distill into ta_9b.
    # Memory config: 2 LoRA layers + seq_len 256 keeps peak under 32GB on M1 Max 64GB
    "combined_35b": {
        "hf_repo": "mlx-community/Qwen3.5-35B-A3B-4bit",
        "training_file": Path.home() / "jarvis/training_data/sessions/_lora_combined_35b/train.jsonl",
        "adapter_path": ADAPTER_DIR / "combined_35b",
        "log_file": "/tmp/lora_train_combined_35b.log",
        "min_new_pairs": 200,
        "iters": 300,
        "num_layers": 2,
        "max_seq_length": 256,
    }
}


def _load_training_state() -> Dict:
    """Load training state from disk."""
    if TRAINING_STATE_FILE.exists():
        try:
            with open(TRAINING_STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load training state: %s", e)
    return {}


def _save_training_state(state: Dict):
    """Save training state to disk."""
    try:
        TRAINING_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRAINING_STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error("Failed to save training state: %s", e)


def _count_training_pairs(training_file: Path) -> int:
    """Count number of training pairs in JSONL file."""
    if not training_file.exists():
        return 0
    try:
        with open(training_file, 'r') as f:
            return sum(1 for _ in f)
    except Exception as e:
        logger.error("Failed to count pairs in %s: %s", training_file, e)
        return 0


def should_train(model_key: str) -> bool:
    """Check if model should be trained.

    Returns True when new_pairs >= min_new_pairs since last training.

    Args:
        model_key: Model identifier

    Returns:
        True if training should be triggered
    """
    if model_key not in MODEL_CONFIGS:
        logger.error("Unknown model key: %s", model_key)
        return False

    config = MODEL_CONFIGS[model_key]
    current_pairs = _count_training_pairs(config['training_file'])

    if current_pairs == 0:
        logger.debug("No training pairs for %s", model_key)
        return False

    # Check last training state
    state = _load_training_state()
    model_state = state.get(model_key, {})
    last_trained_pairs = model_state.get('pairs_at_last_training', 0)

    new_pairs = current_pairs - last_trained_pairs
    min_new_pairs = config.get('min_new_pairs', MIN_NEW_PAIRS)

    if new_pairs >= min_new_pairs:
        logger.info("Model %s: %d new pairs (threshold: %d) - training recommended",
                    model_key, new_pairs, min_new_pairs)
        return True

    logger.debug("Model %s: %d new pairs (need %d more)",
                 model_key, new_pairs, min_new_pairs - new_pairs)
    return False


def run_lora_training(model_key: str) -> Optional[subprocess.Popen]:
    """Run MLX LoRA training as background subprocess.

    Args:
        model_key: Model identifier

    Returns:
        Popen object for background process, or None on error
    """
    if model_key not in MODEL_CONFIGS:
        logger.error("Unknown model key: %s", model_key)
        return None

    config = MODEL_CONFIGS[model_key]

    # Validate training file exists
    if not config['training_file'].exists():
        logger.error("Training file not found: %s", config['training_file'])
        return None

    pair_count = _count_training_pairs(config['training_file'])
    if pair_count == 0:
        logger.error("No training pairs in %s", config['training_file'])
        return None

    # Create adapter directory
    config['adapter_path'].mkdir(parents=True, exist_ok=True)

    # Get training iters from config
    training_iters = config.get('iters', TRAINING_ITERS)

    # mlx_lm.lora requires a --data directory containing a file named train.jsonl.
    # Create a per-model data dir with a symlink so the existing JSONL file is used.
    PYTHON = "~/myenv/bin/python"
    data_dir = config['training_file'].parent / f"_lora_{model_key}"
    data_dir.mkdir(parents=True, exist_ok=True)
    train_link = data_dir / "train.jsonl"
    if not train_link.exists():
        train_link.symlink_to(config['training_file'].resolve())

    # Use conservative memory settings for large models (35B hits GPU OOM without these)
    is_large_model = '35B' in config['hf_repo'] or '14B' in config['hf_repo']
    cmd = [
        PYTHON, "-m", "mlx_lm", "lora",
        "--model", config['hf_repo'],
        "--data", str(data_dir),
        "--iters", str(training_iters),
        "--adapter-path", str(config['adapter_path']),
        "--batch-size", "1",
        "--train",
    ]
    if is_large_model:
        cmd += ["--grad-checkpoint"]
    # Per-model memory overrides (num_layers, max_seq_length)
    if config.get("num_layers"):
        cmd += ["--num-layers", str(config["num_layers"])]
    if config.get("max_seq_length"):
        cmd += ["--max-seq-length", str(config["max_seq_length"])]

    logger.info("Starting LoRA training for %s (%d pairs, %d iters)",
                model_key, pair_count, training_iters)
    logger.info("Command: %s", ' '.join(cmd))
    logger.info("Logs: %s", config['log_file'])

    try:
        # Open log file
        log_fh = open(config['log_file'], 'w')

        # Start background process
        process = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True  # Detach from parent
        )

        # Update training state
        state = _load_training_state()
        state[model_key] = {
            'last_trained': datetime.now().isoformat(),
            'pairs_at_last_training': pair_count,
            'training_iters': training_iters,
            'adapter_path': str(config['adapter_path']),
            'log_file': config['log_file'],
            'pid': process.pid,
            'status': 'running'
        }
        _save_training_state(state)

        logger.info("Training started for %s (PID: %d)", model_key, process.pid)
        return process

    except Exception as e:
        logger.error("Failed to start training for %s: %s", model_key, e)
        return None


def check_training_status(model_key: str) -> Dict:
    """Check status of last training run.

    Args:
        model_key: Model identifier

    Returns:
        Dict with status info
    """
    state = _load_training_state()
    model_state = state.get(model_key, {})

    if not model_state:
        return {"status": "never_trained"}

    status = {
        "last_trained": model_state.get('last_trained'),
        "pairs_trained": model_state.get('pairs_at_last_training', 0),
        "adapter_path": model_state.get('adapter_path'),
        "log_file": model_state.get('log_file')
    }

    # Check if still running
    pid = model_state.get('pid')
    if pid:
        try:
            os.kill(pid, 0)  # Check if process exists
            status['status'] = 'running'
            status['pid'] = pid
        except OSError:
            status['status'] = 'completed'
    else:
        status['status'] = 'completed'

    return status


def train_specific(model_key: str) -> Optional[subprocess.Popen]:
    """Train a specific model immediately, bypassing min_new_pairs check.

    Useful for first run or manual training triggers.

    Args:
        model_key: Model identifier

    Returns:
        Popen object for background process, or None on error
    """
    if model_key not in MODEL_CONFIGS:
        logger.error("Unknown model key: %s", model_key)
        return None

    config = MODEL_CONFIGS[model_key]

    # Check if training file exists and has pairs
    if not config['training_file'].exists():
        logger.error("Training file not found: %s", config['training_file'])
        return None

    pair_count = _count_training_pairs(config['training_file'])
    if pair_count == 0:
        logger.error("No training pairs in %s", config['training_file'])
        return None

    logger.info("Manual training trigger for %s (%d pairs)", model_key, pair_count)
    return run_lora_training(model_key)


def train_all_models():
    """Check and train all models that need training."""
    results = {}

    for model_key in MODEL_CONFIGS.keys():
        if should_train(model_key):
            logger.info("Triggering training for %s", model_key)
            process = run_lora_training(model_key)
            results[model_key] = {
                "triggered": True,
                "pid": process.pid if process else None
            }
        else:
            results[model_key] = {
                "triggered": False,
                "reason": "insufficient_new_pairs"
            }

    return results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "status":
            # Show status for all models
            for model_key in MODEL_CONFIGS.keys():
                status = check_training_status(model_key)
                print(f"\n{model_key}:")
                print(json.dumps(status, indent=2))

        elif cmd == "train":
            # Check if specific model provided
            if len(sys.argv) > 2:
                # Train specific model immediately
                model_key = sys.argv[2]
                if model_key not in MODEL_CONFIGS:
                    print(f"Unknown model: {model_key}")
                    print(f"Available models: {', '.join(MODEL_CONFIGS.keys())}")
                else:
                    process = train_specific(model_key)
                    if process:
                        print(f"Training started for {model_key} (PID: {process.pid})")
                        print(f"Log file: {MODEL_CONFIGS[model_key]['log_file']}")
                    else:
                        print(f"Failed to start training for {model_key}")
            else:
                # Train all models that need it
                results = train_all_models()
                print(json.dumps(results, indent=2))

        elif cmd in MODEL_CONFIGS:
            # Legacy: Train specific model if it needs training
            model_key = cmd
            if should_train(model_key):
                process = run_lora_training(model_key)
                if process:
                    print(f"Training started for {model_key} (PID: {process.pid})")
                else:
                    print(f"Failed to start training for {model_key}")
            else:
                print(f"Model {model_key} does not need training yet")
                print("Use 'python lora_trainer.py train {model_key}' to force training")

        else:
            print(f"Unknown command: {cmd}")
            print(f"Usage: python lora_trainer.py [status|train [model_key]|{model_key}]")
            print(f"Available models: {', '.join(MODEL_CONFIGS.keys())}")

    else:
        # Default: check status
        print("Training Status:")
        for model_key in MODEL_CONFIGS.keys():
            status = check_training_status(model_key)
            print(f"\n{model_key}:")
            print(json.dumps(status, indent=2))


def _convert_to_mlx_format(src_path: Path, dst_path: Path) -> int:
    """Convert any JSONL variant to mlx_lm messages format. Returns pair count."""
    import json, hashlib
    count = 0
    seen = set()
    with open(src_path) as fin, open(dst_path, 'w') as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            # Already messages format
            if 'messages' in rec:
                msgs = rec['messages']
            # conversation field (universal_extractor format)
            elif 'conversation' in rec:
                msgs = rec['conversation']
            # prompt/completion
            elif 'prompt' in rec and 'completion' in rec:
                msgs = [
                    {'role': 'user', 'content': rec['prompt']},
                    {'role': 'assistant', 'content': rec['completion']},
                ]
            # instruction/output (old Alpaca format)
            elif 'instruction' in rec and 'output' in rec:
                msgs = [
                    {'role': 'user', 'content': rec['instruction']},
                    {'role': 'assistant', 'content': rec['output']},
                ]
            else:
                continue
            # Validate
            if not msgs or len(msgs) < 2:
                continue
            user_txt = next((m.get('content','') for m in msgs if m.get('role')=='user'), '')
            asst_txt = next((m.get('content','') for m in msgs if m.get('role')=='assistant'), '')
            if len(user_txt) < 10 or len(asst_txt) < 20:
                continue
            h = hashlib.md5((user_txt + asst_txt).encode()).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            fout.write(json.dumps({'messages': msgs}) + '\n')
            count += 1
    return count
