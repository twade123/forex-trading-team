"""Validator Training Extractor — V4

Extracts (prompt → validator reasoning) training pairs from trade_decisions
to fine-tune the 35B CSO model (Qwen3.5-35B-A3B-4bit) to think like Sonnet's validator.

The 35B is the TARGET model that will replace Anthropic claude-sonnet-4-6 validator calls.
Every validator decision Sonnet makes is a training example for the 35B.

Sources:
  - trade_decisions.validator_reasoning  → Sonnet's output (gold standard CoT)
  - trade_decisions.market_agent_data    → what was sent to the validator
  - trade_decisions.validator_verdict    → CONFIRM / WATCH / REJECT label
  - collect_live_pair()                  → real-time capture per cycle (richer context)

Output: JSONL at ~/jarvis/models/training_data/validator_35b_training.jsonl
Format: {"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}]}

Training data also captures:
  - All trading outcomes (win/loss/pips) via position_guardian callback
  - Bad outcomes flagged as negative examples for correction feedback
  - Claude Code session CoT via session_training.jsonl (trevor_35b track)
"""

import json
import os
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Paths
_DB_PATH = "~/jarvis/Database/v2/trading_forex.db"
_OUTPUT_DIR = Path(os.path.expanduser("~/jarvis/models/training_data"))
_OUTPUT_FILE = _OUTPUT_DIR / "validator_35b_training.jsonl"
_STATE_FILE = Path(os.path.expanduser("~/jarvis/models/training_state.json"))

_SYSTEM_PROMPT = (
    "You are the V4 Vision Trading Brain — the sole trade decision authority for a forex trading system. "
    "Analyze the market structure data provided and produce a clear, reasoned verdict. "
    "Read the EMA fan state, Bollinger Band expansion, momentum indicators (RSI/Stoch/ADX/MACD), "
    "and TA narrative to tell the full 100-bar story of what the market is doing. "
    "Output your verdict as: CONFIRM (trade now — conditions fully met), "
    "WATCH (conditions partially met — set snipe for missing items), or "
    "REJECT (no trade — setup not present or unfavorable). "
    "Always explain WHAT YOU SEE in the data: EMA fan direction and state, BB expansion status, "
    "momentum trajectory, and whether price structure supports the proposed direction. "
    "Be specific and concrete. This reasoning becomes the training signal for the next generation model."
)


def _get_db():
    conn = sqlite3.connect(_DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=15000")
    # Ensure index exists for the verdict+date query pattern used in extract_validator_pairs()
    try:
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_trade_decisions_verdict_created
            ON trade_decisions(validator_verdict, created_at DESC)""")
        conn.commit()
    except Exception:
        pass  # Table may not exist yet
    return conn


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _load_existing_ids() -> set:
    """Load cycle_ids already in the training file to avoid duplicates."""
    ids = set()
    if not _OUTPUT_FILE.exists():
        return ids
    try:
        with open(_OUTPUT_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    meta = obj.get("metadata", {})
                    cid = meta.get("cycle_id") or meta.get("decision_id")
                    if cid:
                        ids.add(str(cid))
                except Exception:
                    pass
    except Exception:
        pass
    return ids


def extract_validator_pairs(min_reasoning_len: int = 150, limit: int = 50) -> int:
    """
    Extract (market context → validator reasoning) training pairs.

    Includes ALL verdicts (CONFIRM/WATCH/REJECT) — the model needs to learn
    when NOT to trade as much as when to trade.

    Args:
        min_reasoning_len: Minimum chars in validator_reasoning to include.
        limit: Max rows to return (default 50 to prevent context overflow).
               Pass limit=0 to fetch all (use only for offline batch export).

    Returns number of new pairs written.
    """
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    existing_ids = _load_existing_ids()

    conn = _get_db()

    query = """
        SELECT
            rowid AS id,
            pair,
            direction,
            validator_verdict,
            validator_confidence,
            validator_reasoning,
            market_agent_data,
            validator_confluence AS confluence_score,
            final_action,
            final_action_reason,
            created_at
        FROM trade_decisions
        WHERE validator_reasoning IS NOT NULL
          AND LENGTH(validator_reasoning) >= ?
          AND validator_verdict IN ('CONFIRM', 'WATCH', 'REJECT', 'HOLD', 'CAUTION', 'TRADE')
        ORDER BY created_at DESC
    """
    if limit:
        query += f" LIMIT {limit}"

    rows = conn.execute(query, (min_reasoning_len,)).fetchall()
    conn.close()
    
    new_pairs = 0
    with open(_OUTPUT_FILE, 'a') as out:
        for row in rows:
            decision_id = str(row['id'])
            if decision_id in existing_ids:
                continue
            
            reasoning = (row['validator_reasoning'] or '').strip()
            if not reasoning or len(reasoning) < min_reasoning_len:
                continue
            
            # Build user message from available market context
            pair = row['pair'] or 'UNKNOWN'
            direction = row['direction'] or 'unknown'
            verdict = row['validator_verdict']
            confidence = row['validator_confidence'] or 0
            confluence = row['confluence_score'] or 0
            
            # Try to extract structured market data from market_agent_data
            market_context = ""
            if row['market_agent_data']:
                try:
                    mdata = json.loads(row['market_agent_data']) if isinstance(row['market_agent_data'], str) else row['market_agent_data']
                    # Pull key fields
                    mp = mdata.get('market_picture', {})
                    sniper = mdata.get('sniper_result', {})
                    ta = mdata.get('ta_interpretation', {})
                    
                    parts = [f"Pair: {pair} | Direction bias: {direction}"]
                    if mp:
                        parts.append(f"EMA fan: {mp.get('fan_state','?')} {mp.get('fan_direction','?')}")
                        parts.append(f"Trend health: {mp.get('trend_health',0):.0f}/100 | Reversal risk: {mp.get('reversal_risk','?')}")
                        parts.append(f"BB expanding: {mp.get('bb_expanding','?')} | Velocity: {mp.get('separation_velocity',0):.5f}%/bar")
                    if sniper:
                        parts.append(f"Sniper: buy={sniper.get('buy_score',0)} sell={sniper.get('sell_score',0)} threshold={sniper.get('threshold',12)}")
                        ind = sniper.get('indicators', {})
                        if ind:
                            parts.append(f"RSI: {ind.get('rsi',50):.1f} | Stoch K: {ind.get('stoch_k',50):.1f} | ADX: {ind.get('adx',25):.1f}")
                    if ta:
                        parts.append(f"TA narrative: {ta.get('narrative','')[:200]}")
                        parts.append(f"TA clarity: {ta.get('clarity','?')}")
                    parts.append(f"Pre-validator confluence: {confluence}/75")
                    market_context = "\n".join(parts)
                except Exception:
                    market_context = f"Pair: {pair} | Direction: {direction} | Confluence: {confluence}/75"
            else:
                market_context = f"Pair: {pair} | Direction: {direction} | Confluence: {confluence}/75"
            
            user_message = (
                f"Validate this trading setup:\n\n"
                f"{market_context}\n\n"
                f"Analyze the market structure and give your verdict: CONFIRM, WATCH, or REJECT. "
                f"Be specific about what you see — EMA fan state, BB behavior, momentum, candle structure."
            )
            
            # Assistant response = the Sonnet validator's actual reasoning
            # Prepend verdict for clean format
            if confidence and confidence <= 1.0:
                conf_str = f"{confidence:.0%}"
            elif confidence and confidence > 1:
                conf_str = f"{confidence:.0f}%"
            else:
                conf_str = "unknown"
            
            # Normalise legacy verdicts to current vocabulary
            _v_norm = {"HOLD": "WATCH", "CAUTION": "WATCH", "TRADE": "CONFIRM"}.get(verdict, verdict)
            assistant_message = f"VERDICT: {_v_norm} (confidence: {conf_str})\n\n{reasoning}"
            
            record = {
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": assistant_message},
                ],
                "metadata": {
                    "decision_id": decision_id,
                    "pair": pair,
                    "verdict": verdict,
                    "created_at": row['created_at'],
                    "source": "trade_decisions.validator_reasoning",
                    "model": "claude-sonnet-4-6",
                }
            }
            
            out.write(json.dumps(record) + "\n")
            existing_ids.add(decision_id)
            new_pairs += 1
    
    logger.info("Validator training extractor: wrote %d new pairs to %s", new_pairs, _OUTPUT_FILE)
    return new_pairs


def get_training_stats() -> dict:
    """Return stats on the validator training dataset."""
    stats = {
        "output_file": str(_OUTPUT_FILE),
        "exists": _OUTPUT_FILE.exists(),
        "total_pairs": 0,
        "by_verdict": {"CONFIRM": 0, "WATCH": 0, "REJECT": 0, "HOLD": 0, "CAUTION": 0},
        "file_size_kb": 0,
        "last_updated": None,
    }
    
    if not _OUTPUT_FILE.exists():
        return stats
    
    stats["file_size_kb"] = round(_OUTPUT_FILE.stat().st_size / 1024, 1)
    stats["last_updated"] = datetime.fromtimestamp(_OUTPUT_FILE.stat().st_mtime).isoformat()
    
    try:
        with open(_OUTPUT_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    stats["total_pairs"] += 1
                    verdict = obj.get("metadata", {}).get("verdict", "?")
                    stats["by_verdict"][verdict] = stats["by_verdict"].get(verdict, 0) + 1
                except Exception:
                    pass
    except Exception as e:
        stats["error"] = str(e)
    
    return stats


def collect_live_pair(cycle_id: str, instrument: str, verdict: str,
                      reasoning: str, market_context: str,
                      confidence: float = 0.0):
    """
    Called after each live cycle to save the validator pair immediately.
    This runs in real-time as trades happen (not just backfill).
    """
    if not reasoning or len(reasoning) < 100:
        return
    
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    existing_ids = _load_existing_ids()
    
    if cycle_id in existing_ids:
        return  # Already saved
    
    conf_str = f"{confidence:.0%}" if confidence <= 1.0 else f"{confidence:.0f}%"
    
    record = {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Validate this trading setup:\n\n{market_context}\n\n"
                f"Analyze the market structure and give your verdict: CONFIRM, WATCH, or REJECT."
            )},
            {"role": "assistant", "content": f"VERDICT: {verdict} (confidence: {conf_str})\n\n{reasoning}"},
        ],
        "metadata": {
            "cycle_id": cycle_id,
            "pair": instrument,
            "verdict": verdict,
            "created_at": datetime.utcnow().isoformat(),
            "source": "live_cycle",
            "model": "claude-sonnet-4-6",
        }
    }
    
    try:
        with open(_OUTPUT_FILE, 'a') as f:
            f.write(json.dumps(record) + "\n")
        logger.debug("[TRAINING] Saved live validator pair: %s %s %s", cycle_id, instrument, verdict)
    except Exception as e:
        logger.warning("[TRAINING] Failed to save live pair: %s", e)


def stamp_outcome(cycle_id: str, outcome: str, pnl_pips: float):
    """Stamp a trade outcome onto a previously saved training pair.

    Called by position_guardian when a trade closes.
    Marks pairs as WIN/LOSS so the 35B learns from results, not just verdicts.
    Pairs with bad outcomes (CONFIRM but LOSS) are flagged as negative examples
    for the correction feedback loop.

    Args:
        cycle_id: The cycle that produced the trade
        outcome: 'win' or 'loss'
        pnl_pips: Profit/loss in pips
    """
    if not _OUTPUT_FILE.exists():
        return

    try:
        pairs = []
        updated = 0
        with open(_OUTPUT_FILE) as f:
            for line in f:
                try:
                    d = json.loads(line.strip())
                    meta = d.get("metadata", {})
                    if meta.get("cycle_id") == cycle_id and "outcome" not in meta:
                        meta["outcome"] = outcome
                        meta["pnl_pips"] = round(pnl_pips, 2)
                        # Flag bad outcomes: validator said CONFIRM but trade lost
                        if meta.get("verdict") == "CONFIRM" and outcome == "loss":
                            meta["negative_example"] = True
                        d["metadata"] = meta
                        updated += 1
                    pairs.append(d)
                except Exception:
                    pairs.append(json.loads(line.strip()) if line.strip() else None)

        if updated > 0:
            with open(_OUTPUT_FILE, 'w') as f:
                for p in pairs:
                    if p:
                        f.write(json.dumps(p) + "\n")
            logger.info("[TRAINING] Stamped outcome %s (%.1f pips) on cycle %s",
                        outcome, pnl_pips, cycle_id)
    except Exception as e:
        logger.warning("[TRAINING] Failed to stamp outcome: %s", e)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    
    print("Extracting validator training pairs from trade_decisions...")
    n = extract_validator_pairs()
    print(f"New pairs extracted: {n}")
    
    stats = get_training_stats()
    print(f"\nTraining dataset stats:")
    print(f"  File: {stats['output_file']}")
    print(f"  Total pairs: {stats['total_pairs']}")
    print(f"  By verdict: CONFIRM={stats['by_verdict']['CONFIRM']} WATCH={stats['by_verdict']['WATCH']} REJECT={stats['by_verdict']['REJECT']}")
    print(f"  File size: {stats['file_size_kb']} KB")
    print(f"  Last updated: {stats['last_updated']}")
