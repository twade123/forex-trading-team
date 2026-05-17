"""CLI launcher — starts the gateway via uvicorn.

Usage:
  python3 -m serving.run_gateway [--config PATH] [--host HOST] [--port PORT]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn
import yaml

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))

from serving.gateway import app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(_HERE / "config.yaml"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg = yaml.safe_load(open(args.config))
    host = args.host or cfg["gateway"]["host"]
    port = args.port or int(cfg["gateway"]["port"])

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app.state.config_path = args.config

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
