#!/usr/bin/env python3
"""Fetch 3 years of H1 EUR_USD candles from OANDA API."""

import json
import csv
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api-fxpractice.oanda.com"
MAX_CANDLES = 5000  # OANDA limit per request


def load_api_key() -> str:
    key_file = Path(__file__).resolve().parent.parent.parent.parent / "API" / "OANDA_API_KEY.txt"
    return key_file.read_text().strip()


def fetch_candles(
    instrument: str = "EUR_USD",
    granularity: str = "H1",
    from_time: str = "2023-02-13T00:00:00Z",
    to_time: str = None,
) -> list:
    """Fetch all candles between from_time and to_time, paginating as needed."""
    api_key = load_api_key()
    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{API_URL}/v3/instruments/{instrument}/candles"

    all_candles = []
    current_from = from_time

    while True:
        params = {"granularity": granularity, "from": current_from, "count": MAX_CANDLES}
        logger.info("Fetching %s %s from %s ...", instrument, granularity, current_from)
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

        candles = data.get("candles", [])
        if not candles:
            break

        all_candles.extend(candles)
        last_time = candles[-1]["time"]
        logger.info("  Got %d candles, last=%s (total=%d)", len(candles), last_time, len(all_candles))

        # If we got less than max, we're done
        if len(candles) < MAX_CANDLES:
            break

        # If we have a to_time and passed it, stop
        if to_time and last_time >= to_time:
            break

        # Paginate: start from last candle time
        current_from = last_time
        time.sleep(0.5)  # Rate limiting

    # Filter to to_time if specified
    if to_time:
        all_candles = [c for c in all_candles if c["time"] <= to_time]

    # Deduplicate by timestamp
    seen = set()
    unique = []
    for c in all_candles:
        if c["time"] not in seen:
            seen.add(c["time"])
            unique.append(c)

    logger.info("Total unique candles: %d", len(unique))
    return unique


def candles_to_rows(candles: list) -> list:
    """Convert OANDA candle format to flat dicts."""
    rows = []
    for c in candles:
        if not c.get("complete", True):
            continue
        mid = c["mid"]
        rows.append({
            "timestamp": c["time"],
            "open": float(mid["o"]),
            "high": float(mid["h"]),
            "low": float(mid["l"]),
            "close": float(mid["c"]),
            "volume": int(c.get("volume", 0)),
        })
    return rows


def save_json(rows: list, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(rows, f, indent=2)
    logger.info("Saved %d rows to %s", len(rows), path)


def save_csv(rows: list, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["timestamp", "open", "high", "low", "close", "volume"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Saved %d rows to %s", len(rows), path)


def fetch_and_save(
    instrument: str = "EUR_USD",
    granularity: str = "H1",
    from_time: str = "2023-02-13T00:00:00Z",
    to_time: str = None,
    data_dir: Path = None,
) -> Path:
    """Fetch candles and save to Data/ directory. Returns CSV path."""
    if data_dir is None:
        data_dir = Path(__file__).resolve().parent.parent.parent / "Data"

    candles = fetch_candles(instrument, granularity, from_time, to_time)
    rows = candles_to_rows(candles)

    tag = f"{instrument.lower()}_{granularity.lower()}_3yr"
    json_path = data_dir / f"{tag}.json"
    csv_path = data_dir / f"{tag}.csv"

    save_json(rows, json_path)
    save_csv(rows, csv_path)

    return csv_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    csv_path = fetch_and_save(from_time="2023-02-13T00:00:00Z", to_time=now)
    print(f"\nData saved to {csv_path}")
