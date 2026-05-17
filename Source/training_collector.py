#!/usr/bin/env python3
"""Training data collector for MLX model fine-tuning.

Collects training pairs from winning trades:
- TA 9B model: technical_analyst agent communications
- Trade Monitor 35B: trade_monitor agent communications

Data sources:
- v2/workspaces.db: agent_communications table
- v2/trading_forex.db: live_trades table

Output: JSONL files with OpenAI chat format for MLX LoRA training.
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger("trading_bot.training_collector")

# Paths
JARVIS_ROOT = Path(__file__).resolve().parent.parent.parent
BOARDROOM_DB = JARVIS_ROOT / "Database" / "v2" / "workspaces.db"
TREVOR_DB = JARVIS_ROOT / "Database" / "v2" / "trading_forex.db"
TRAINING_DATA_DIR = Path.home() / "jarvis" / "models" / "training_data"
TRAINING_STATE_FILE = Path.home() / "jarvis" / "models" / "training_state.json"

# Ensure directories exist
TRAINING_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Training file paths
TA_TRAINING_FILE = TRAINING_DATA_DIR / "ta_9b_training.jsonl"
MONITOR_TRAINING_FILE = TRAINING_DATA_DIR / "trade_monitor_35b_training.jsonl"


def _get_boardroom_conn() -> sqlite3.Connection:
    """Get connection to boardroom database."""
    conn = sqlite3.connect(str(BOARDROOM_DB), timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.row_factory = sqlite3.Row
    return conn


def _get_trevor_conn() -> sqlite3.Connection:
    """Get connection to trevor database."""
    conn = sqlite3.connect(str(TREVOR_DB), timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.row_factory = sqlite3.Row
    return conn


def _load_processed_cycles() -> Set[str]:
    """Load set of already-processed cycle IDs."""
    processed_file = TRAINING_DATA_DIR / ".processed_cycles.json"
    if processed_file.exists():
        try:
            with open(processed_file, 'r') as f:
                return set(json.load(f))
        except Exception as e:
            logger.warning("Failed to load processed cycles: %s", e)
    return set()


def _save_processed_cycle(cycle_id: str):
    """Mark cycle as processed."""
    processed_file = TRAINING_DATA_DIR / ".processed_cycles.json"
    processed = _load_processed_cycles()
    processed.add(cycle_id)
    try:
        with open(processed_file, 'w') as f:
            json.dump(list(processed), f)
    except Exception as e:
        logger.warning("Failed to save processed cycle: %s", e)


def _format_training_pair(system_prompt: str, user_prompt: str, assistant_response: str) -> Dict:
    """Format a training pair in OpenAI chat format for MLX."""
    return {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": assistant_response}
        ]
    }


def _extract_ta_pairs(cycle_id: str, instrument: str, is_negative: bool = False) -> List[Dict]:
    """Extract technical analyst training pairs for a completed cycle.

    Finds technical_analyst agent communications and trade_decisions data.
    When is_negative=True (losing trade), appends a correction note to the
    assistant turn so the 35B learns NOT to repeat this pattern.
    """
    pairs = []

    # Try agent_communications first
    try:
        conn = _get_boardroom_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT content_full, content_summary, metadata
            FROM agent_communications
            WHERE sender_agent_name = 'technical_analyst'
              AND (content_full LIKE ? OR content_summary LIKE ?)
            ORDER BY timestamp DESC
            LIMIT 20
        """, (f"%{instrument}%", f"%{instrument}%"))

        messages = cursor.fetchall()
        conn.close()

        for msg in messages:
            try:
                content_full = msg['content_full'] or ""
                if len(content_full) > 100:
                    system_prompt = """You are an expert technical analyst for forex trading. Analyze price action, indicators, and market structure to provide actionable trading insights. Focus on confluence, risk-reward, and precise entry/exit levels."""
                    user_prompt = f"Analyze {instrument} for trading opportunities. Provide technical analysis with specific levels and reasoning."
                    assistant_content = content_full
                    if is_negative:
                        assistant_content += "\n\n[POST-TRADE CORRECTION: This analysis led to a losing trade. Do not repeat this pattern.]"
                    pair = _format_training_pair(system_prompt, user_prompt, assistant_content)
                    pairs.append(pair)
            except Exception as e:
                logger.debug("Failed to parse TA message: %s", e)

    except Exception as e:
        logger.debug("No agent_communications data: %s", e)

    # Fallback: extract from trade_decisions table
    if len(pairs) == 0:
        try:
            conn = _get_trevor_conn()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT market_agent_data, validator_reasoning, pair, regime
                FROM trade_decisions
                WHERE live_trade_id = ?
                LIMIT 1
            """, (cycle_id,))

            row = cursor.fetchone()
            conn.close()

            if row:
                market_data = row['market_agent_data'] or ""
                validator_reasoning = row['validator_reasoning'] or ""

                # Create training pair from market analysis
                if len(market_data) > 100:
                    system_prompt = """You are an expert technical analyst for forex trading. Analyze price action, indicators, and market structure to provide actionable trading insights."""
                    user_prompt = f"Analyze {instrument} in {row['regime'] or 'current'} market regime. Provide comprehensive technical analysis."

                    # Combine market data and validator reasoning for rich training signal
                    assistant_response = f"{market_data}\n\nValidator Analysis: {validator_reasoning}"
                    if is_negative:
                        assistant_response += "\n\n[POST-TRADE CORRECTION: This analysis led to a losing trade. Do not repeat this pattern.]"

                    if len(assistant_response) > 100:
                        pair = _format_training_pair(system_prompt, user_prompt, assistant_response)
                        pairs.append(pair)

        except Exception as e:
            logger.debug("Failed to extract from trade_decisions: %s", e)

    if not pairs:
        logger.warning("[TRAINING] No TA data found for cycle %s (%s) — possible orphaned cycle_id",
                       cycle_id, instrument)
    else:
        logger.info("Extracted %d TA pairs for cycle %s (%s)", len(pairs), cycle_id, instrument)
    return pairs


def _extract_monitor_pairs(cycle_id: str, instrument: str) -> List[Dict]:
    """Extract trade monitor training pairs for a winning cycle.

    Finds trade_monitor agent communications and exit learning data.
    """
    pairs = []

    # Try agent_communications first
    try:
        conn = _get_boardroom_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT content_full, content_summary, metadata
            FROM agent_communications
            WHERE sender_agent_name = 'trade_monitor'
              AND (content_full LIKE ? OR content_summary LIKE ?)
            ORDER BY timestamp DESC
            LIMIT 20
        """, (f"%{instrument}%", f"%{instrument}%"))

        messages = cursor.fetchall()
        conn.close()

        for msg in messages:
            try:
                content_full = msg['content_full'] or ""
                if len(content_full) > 100:
                    system_prompt = """You are an expert trade monitor managing live forex positions. Analyze real-time market conditions, threat levels, and position management. Provide clear reasoning for hold/exit decisions with specific risk assessments."""
                    user_prompt = f"Monitor active {instrument} position. Assess current market conditions, threat level, and recommend position management actions."
                    pair = _format_training_pair(system_prompt, user_prompt, content_full)
                    pairs.append(pair)
            except Exception as e:
                logger.debug("Failed to parse monitor message: %s", e)

    except Exception as e:
        logger.debug("No agent_communications data: %s", e)

    # Fallback: use exit learning data from flight_recorder
    if len(pairs) == 0:
        try:
            # Check for exit learning data in flight_recorder
            flight_db = Path(__file__).resolve().parent / "flight_recorder.db"
            if flight_db.exists():
                conn = sqlite3.connect(str(flight_db), timeout=10, isolation_level=None)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # Look for guardian action data for this trade
                cursor.execute("""
                    SELECT data, note
                    FROM flight_log
                    WHERE trade_id = ? AND stage = 'GUARDIAN_ACTION'
                    ORDER BY timestamp DESC
                    LIMIT 5
                """, (cycle_id,))

                rows = cursor.fetchall()
                conn.close()

                for row in rows:
                    try:
                        data_str = row['data'] or "{}"
                        data = json.loads(data_str) if isinstance(data_str, str) else data_str
                        note = row['note'] or ""

                        # Extract guardian reasoning
                        threat_level = data.get('threat_level', 0)
                        zone = data.get('zone', '')
                        reasons = data.get('reasons', [])

                        if reasons:
                            system_prompt = """You are an expert trade monitor managing live forex positions. Analyze real-time market conditions, threat levels, and position management."""
                            user_prompt = f"Assess position management for {instrument}. Current threat level: {threat_level} ({zone}). Provide reasoning for recommended actions."
                            assistant_response = f"Threat Assessment: {threat_level}/100 ({zone} zone)\n\nReasons:\n" + "\n".join(f"- {r}" for r in reasons)
                            assistant_response += f"\n\nAction: {note}"

                            if len(assistant_response) > 100:
                                pair = _format_training_pair(system_prompt, user_prompt, assistant_response)
                                pairs.append(pair)
                    except Exception as e:
                        logger.debug("Failed to parse flight log: %s", e)

        except Exception as e:
            logger.debug("Failed to extract from flight_recorder: %s", e)

    if not pairs:
        logger.warning("[TRAINING] No monitor data found for cycle %s (%s) — possible orphaned cycle_id",
                       cycle_id, instrument)
    else:
        logger.info("Extracted %d monitor pairs for cycle %s (%s)", len(pairs), cycle_id, instrument)
    return pairs


def _append_to_jsonl(pairs: List[Dict], filepath: Path):
    """Append training pairs to JSONL file."""
    if not pairs:
        return

    try:
        with open(filepath, 'a') as f:
            for pair in pairs:
                f.write(json.dumps(pair) + '\n')
        logger.info("Appended %d pairs to %s", len(pairs), filepath.name)
    except Exception as e:
        logger.error("Failed to append pairs to %s: %s", filepath, e)


def collect_cycle_pairs(cycle_id: str, instrument: str, outcome: str):
    """Collect training pairs from a completed cycle.

    Processes winning AND losing trades:
    - Wins → positive examples for TA and monitor
    - Losses → negative examples for TA (with correction note appended)
    - Breakeven → skip (no clear signal)

    Args:
        cycle_id: Unique cycle identifier
        instrument: Trading pair (e.g., 'EUR_USD')
        outcome: Trade outcome ('win', 'loss', 'breakeven')
    """
    if outcome not in ('win', 'loss'):
        logger.debug("Skipping non-actionable outcome %s for cycle %s", outcome, cycle_id)
        return

    is_win = (outcome == 'win')

    # Check if already processed
    processed = _load_processed_cycles()
    if cycle_id in processed:
        logger.debug("Cycle %s already processed", cycle_id)
        return

    logger.info("Collecting training pairs for %s cycle: %s (%s)", outcome, cycle_id, instrument)

    # Extract TA pairs — wins as positive, losses as negative examples
    ta_pairs = _extract_ta_pairs(cycle_id, instrument, is_negative=(not is_win))
    if ta_pairs:
        _append_to_jsonl(ta_pairs, TA_TRAINING_FILE)

    # Monitor pairs: wins only — losses don't have useful monitor data
    if is_win:
        monitor_pairs = _extract_monitor_pairs(cycle_id, instrument)
        if monitor_pairs:
            _append_to_jsonl(monitor_pairs, MONITOR_TRAINING_FILE)
    else:
        monitor_pairs = []

    # Mark as processed
    _save_processed_cycle(cycle_id)

    logger.info("Cycle %s (%s): collected %d TA + %d monitor pairs",
                cycle_id, outcome, len(ta_pairs), len(monitor_pairs))


def backfill_from_history():
    """Backfill training data from all historical winning trades.

    Processes all WIN trades from live_trades table that haven't been
    processed yet. Useful for initial training data collection.
    """
    logger.info("Starting backfill from historical winning trades...")

    try:
        conn = _get_trevor_conn()
        cursor = conn.cursor()

        # Find all winning trades with their timing
        cursor.execute("""
            SELECT trade_id, pair, result, exit_reason, entry_time, exit_time
            FROM live_trades
            WHERE result = 'win'
            ORDER BY entry_time ASC
        """)

        wins = cursor.fetchall()
        logger.info("Found %d historical winning trades", len(wins))

        # For each winning trade, find associated trade_decisions by pair + timing
        processed = _load_processed_cycles()
        new_count = 0
        pairs_collected = 0

        for trade in wins:
            cycle_id = trade['trade_id']
            instrument = trade['pair']
            entry_time = trade['entry_time']

            if cycle_id in processed:
                continue

            # Find matching trade_decisions by pair and timing (within 5 minutes)
            cursor.execute("""
                SELECT market_agent_data, validator_reasoning, regime, final_action
                FROM trade_decisions
                WHERE pair = ?
                  AND datetime(timestamp) BETWEEN datetime(?, '-5 minutes') AND datetime(?, '+5 minutes')
                ORDER BY created_at DESC
                LIMIT 1
            """, (instrument, entry_time, entry_time))

            decision = cursor.fetchone()

            if decision:
                market_data = decision['market_agent_data'] or ""
                validator_reasoning = decision['validator_reasoning'] or ""
                regime = decision['regime'] or "unknown"
                final_action = decision['final_action'] or ""

                # Create TA training pair from validator reasoning (rich analysis data)
                if len(validator_reasoning) > 100:
                    system_prompt = """You are an expert technical analyst for forex trading. Analyze price action, indicators, and market structure to provide actionable trading insights. Focus on confluence, risk-reward, regime analysis, and pattern recognition."""
                    user_prompt = f"Analyze {instrument} in {regime} market regime for {final_action} trade. Evaluate setup quality, directional thesis, momentum indicators, and pattern confluence. Provide detailed reasoning for the trade decision."

                    # Use validator reasoning as the rich training signal
                    assistant_response = validator_reasoning
                    if market_data and len(market_data) > 50:
                        assistant_response = f"{market_data}\n\n{validator_reasoning}"

                    if len(assistant_response) > 100:
                        pair = _format_training_pair(system_prompt, user_prompt, assistant_response)
                        _append_to_jsonl([pair], TA_TRAINING_FILE)
                        pairs_collected += 1
                        logger.debug("Collected TA pair for %s (%s, %d chars)", cycle_id, instrument, len(assistant_response))

            _save_processed_cycle(cycle_id)
            new_count += 1

        conn.close()
        logger.info("Backfill complete: processed %d trades, collected %d pairs", new_count, pairs_collected)

        return {
            "total_wins": len(wins),
            "newly_processed": new_count,
            "already_processed": len(wins) - new_count,
            "pairs_collected": pairs_collected
        }

    except Exception as e:
        logger.error("Backfill failed: %s", e)
        return {"error": str(e)}


def get_training_stats() -> Dict:
    """Get current training data statistics.

    Returns:
        Dict with pair counts, file sizes, and training status
    """
    stats = {
        "ta_9b": {
            "file": str(TA_TRAINING_FILE),
            "pair_count": 0,
            "file_size_mb": 0,
            "last_updated": None
        },
        "trade_monitor_35b": {
            "file": str(MONITOR_TRAINING_FILE),
            "pair_count": 0,
            "file_size_mb": 0,
            "last_updated": None
        },
        "processed_cycles": len(_load_processed_cycles())
    }

    # Count TA pairs
    if TA_TRAINING_FILE.exists():
        try:
            with open(TA_TRAINING_FILE, 'r') as f:
                stats['ta_9b']['pair_count'] = sum(1 for _ in f)
            stats['ta_9b']['file_size_mb'] = TA_TRAINING_FILE.stat().st_size / (1024 * 1024)
            stats['ta_9b']['last_updated'] = datetime.fromtimestamp(
                TA_TRAINING_FILE.stat().st_mtime
            ).isoformat()
        except Exception as e:
            logger.error("Failed to stat TA file: %s", e)

    # Count monitor pairs
    if MONITOR_TRAINING_FILE.exists():
        try:
            with open(MONITOR_TRAINING_FILE, 'r') as f:
                stats['trade_monitor_35b']['pair_count'] = sum(1 for _ in f)
            stats['trade_monitor_35b']['file_size_mb'] = MONITOR_TRAINING_FILE.stat().st_size / (1024 * 1024)
            stats['trade_monitor_35b']['last_updated'] = datetime.fromtimestamp(
                MONITOR_TRAINING_FILE.stat().st_mtime
            ).isoformat()
        except Exception as e:
            logger.error("Failed to stat monitor file: %s", e)

    # Load training state
    if TRAINING_STATE_FILE.exists():
        try:
            with open(TRAINING_STATE_FILE, 'r') as f:
                training_state = json.load(f)
                stats['training_state'] = training_state
        except Exception as e:
            logger.error("Failed to load training state: %s", e)

    return stats


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        result = backfill_from_history()
        print(json.dumps(result, indent=2))
    else:
        stats = get_training_stats()
        print(json.dumps(stats, indent=2))
