"""
V4 Vision Validator — Calls Claude with teaching images + live chart.

This is the BRAIN of the trading pipeline. 98.6% precision proven on 371 backtested charts.

Usage:
    from vision_validator import VisionValidator
    validator = VisionValidator()
    result = validator.evaluate(pair, chart_path, indicators, narrative)
    # result = {"verdict": "TRADE_NOW", "direction": "SELL", "confidence": 0.92, ...}
"""

import base64
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from db_pool import get_trading_forex

logger = logging.getLogger("trading_bot.vision_validator")

# Teaching image descriptions (matched to filenames in charts/teaching/)
TEACHING_IMAGES = [
    # Tim's annotated TRADE examples
    {
        "file": "tim_teach_1.png",
        "description": "TRADE EXAMPLE — AUD_USD: Green zone shows fan opening wide, BBs expanding. Clean unmistakable expansion. THIS is what a valid entry looks like.",
    },
    {
        "file": "tim_teach_2.png",
        "description": "TRADE EXAMPLE — GBP_USD: Green zone shows clear downward expansion after cross. EMAs separating in order, BBs widening. Obvious trend.",
    },
    # Backtester TRADE examples
    {
        "file": "trade_364_USD_JPY_SHORT_WIN_+190p.png",
        "description": "TRADE EXAMPLE — USD_JPY SHORT +190 pips: Perfect expansion. Fan opens wide, BBs expand, candles drop cleanly. Fan Width bars grow tall and green.",
    },
    {
        "file": "trade_311_EUR_JPY_LONG_WIN_+93p.png",
        "description": "TRADE EXAMPLE — EUR_JPY LONG +93 pips: Bullish expansion. EMAs separating upward, BBs confirming. Entry when expansion was visually clear.",
    },
    # Tim's annotated SKIP examples
    {
        "file": "tim_teach_3.png",
        "description": "SKIP EXAMPLE — EUR_CHF: Red zone shows flat/contracting fan. No expansion. EMAs converging, BBs tight. Nothing is happening. Do NOT trade this.",
    },
    {
        "file": "tim_teach_4.png",
        "description": "SKIP EXAMPLE — EUR_USD: Red zone shows fan peaked then contracting. BBs tightening. Move already happened. Too late. Do NOT trade this.",
    },
    # Backtester SKIP examples  
    {
        "file": "trade_338_GBP_JPY_SHORT_LOSS_-74p.png",
        "description": "SKIP EXAMPLE — GBP_JPY SHORT -74 pips LOSS: Fan never expanded. Entered on cross but EMAs stayed tangled. Fan Width shows short inconsistent bars.",
    },
    {
        "file": "trade_103_AUD_JPY_SHORT_LOSS_-34p.png",
        "description": "SKIP EXAMPLE — AUD_JPY SHORT -34 pips LOSS: Choppy. E100 too close. No clear separation. Fan Width shows no sustained growth.",
    },
]


class VisionValidator:
    """V4 Vision-based trade validator using Claude with teaching images."""

    def __init__(
        self,
        api_key_path: str = None,
        teaching_dir: str = None,
        model: str = "claude-sonnet-4-20250514",
        db_path: str = None,
    ):
        # API key
        if api_key_path is None:
            api_key_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "API", "CLAUDE_API_KEY.txt"
            )
        with open(api_key_path) as f:
            self.api_key = f.read().strip()

        # Teaching images directory
        if teaching_dir is None:
            teaching_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "Data", "charts", "teaching"
            )
        self.teaching_dir = teaching_dir

        # Model
        self.model = model

        # DB for logging
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "Data", "trade_log.db"
            )
        self.db_path = db_path

        # Cache teaching images in memory (loaded once)
        self._teaching_cache: Optional[List[Dict]] = None

        logger.info("VisionValidator initialized: model=%s, teaching_dir=%s", model, teaching_dir)

    def _load_teaching_images(self) -> List[Dict]:
        """Load and cache teaching images as base64."""
        if self._teaching_cache is not None:
            return self._teaching_cache

        self._teaching_cache = []
        for img_info in TEACHING_IMAGES:
            fpath = os.path.join(self.teaching_dir, img_info["file"])
            if not os.path.exists(fpath):
                logger.warning("Teaching image missing: %s", fpath)
                continue
            with open(fpath, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode("utf-8")
            self._teaching_cache.append({
                "b64": b64,
                "description": img_info["description"],
                "filename": img_info["file"],
            })

        logger.info("Loaded %d/%d teaching images", len(self._teaching_cache), len(TEACHING_IMAGES))
        return self._teaching_cache

    def _load_system_prompt(self) -> str:
        """Load validator system prompt from vault (canonical), with learnings appended.

        Source priority:
        1. knowledge/agents/validator/prompt.md  ← vault (canonical)
        2. Forex Trading Team/Prompts/validator_v4.md   ← legacy fallback
        """
        jarvis_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        vault_prompt_path = os.path.join(jarvis_root, "knowledge", "agents", "validator", "prompt.md")
        legacy_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "Prompts", "validator_v4.md")

        if os.path.exists(vault_prompt_path):
            with open(vault_prompt_path) as f:
                base_prompt = f.read()
            logger.info("Loaded validator prompt from vault (%d chars)", len(base_prompt))
        else:
            logger.warning("Vault prompt not found, falling back to legacy path")
            with open(legacy_path) as f:
                base_prompt = f.read()

        # Append vault learnings — what this agent has accumulated across all sessions
        try:
            import sys as _sys
            _sys.path.insert(0, jarvis_root)
            from knowledge.vault_writer import VaultWriter
            vw = VaultWriter()
            vault_ctx = vw.load_agent_context("validator", max_learnings=8)
            if vault_ctx:
                base_prompt += f"\n\n---\n## YOUR INSTITUTIONAL MEMORY\n{vault_ctx}\n---\n"
        except Exception as e:
            logger.debug("Vault context load failed (non-critical): %s", e)

        return base_prompt

    def _format_trader_context(self, pair: str, annotations: list) -> str:
        """
        Format trader annotations into a clear context block for the prompt.
        Returns empty string if no annotations.
        """
        if not annotations:
            return ""

        lines = [
            f"## ⚠️ TRADER'S CHART ANALYSIS — {pair}",
            "",
            "The trader (Tim) has manually studied this chart and left the following notes.",
            "This is human expert analysis. Read it carefully before making your verdict.",
            "",
        ]

        # Separate directional bias from price-level markers
        directional = [a for a in annotations if a.get("direction") or a.get("annotation_type") in ("bias", "pattern")]
        price_levels = [a for a in annotations if a.get("annotation_type") in ("support", "resistance") and a.get("price")]

        if directional:
            lines.append("### Trader's Directional Read:")
            for a in directional:
                ts = str(a.get("created_at", ""))[:16]
                atype = a.get("annotation_type", "note").upper()
                direction = a.get("direction", "")
                note = a.get("note", "").strip()
                dir_tag = f" [{direction}]" if direction else ""
                lines.append(f"  • [{ts}] {atype}{dir_tag}: {note}")
            lines.append("")

        if price_levels:
            lines.append("### Trader's Key Price Levels:")
            for a in price_levels:
                ts = str(a.get("created_at", ""))[:16]
                atype = a.get("annotation_type", "").upper()
                price = a.get("price", "?")
                note = a.get("note", "").strip()
                lines.append(f"  • [{ts}] {atype} @ {price}: {note}")
            lines.append("")

        # Extract dominant direction from annotations
        sell_count = sum(1 for a in directional if str(a.get("direction", "")).upper() == "SELL"
                         or "sell" in str(a.get("note", "")).lower())
        buy_count = sum(1 for a in directional if str(a.get("direction", "")).upper() == "BUY"
                        or " buy" in str(a.get("note", "")).lower())

        if sell_count > buy_count:
            dominant = "SELL"
        elif buy_count > sell_count:
            dominant = "BUY"
        else:
            dominant = None

        if dominant:
            opposite = "BUY" if dominant == "SELL" else "SELL"
            lines += [
                f"### ⚠️ DIRECTION CONSTRAINT:",
                f"The trader's most recent analysis points to {dominant}.",
                f"If you are considering a {opposite} verdict, you MUST explicitly state:",
                f"  1. What specific chart evidence contradicts the trader's {dominant} read",
                f"  2. Why the chart has changed since the trader's analysis",
                f"  3. That you are consciously overriding the trader's call",
                f"Do NOT silently flip direction. The trader's analysis carries weight.",
                "",
            ]

        return "\n".join(lines)

    def evaluate(
        self,
        pair: str,
        chart_path: str,
        indicators: Dict[str, Any],
        narrative: str = "",
        alert_data: Dict = None,
        alert_id: int = None,
        trader_annotations: list = None,
    ) -> Dict[str, Any]:
        """
        Evaluate a trading setup using vision.

        Args:
            pair: Instrument (e.g. "EUR_USD")
            chart_path: Path to the M15 chart image
            indicators: Dict of current indicator values
            narrative: TA agent's market narrative
            alert_data: Scout alert data (optional context)
            alert_id: Scout alert DB id (for linking)

        Returns:
            Dict with verdict, direction, confidence, reasoning, etc.
        """
        import anthropic

        start_time = time.time()
        teaching_images = self._load_teaching_images()

        if not os.path.exists(chart_path):
            logger.error("Chart image not found: %s", chart_path)
            return {"verdict": "SKIP", "reasoning": "Chart image not found", "confidence": 0}

        # Load live chart
        with open(chart_path, "rb") as f:
            chart_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

        # Build message content
        content = []

        # Teaching images first (few-shot examples)
        content.append({"type": "text", "text": "## Teaching Examples\nStudy these examples to understand what TRADE and SKIP setups look like:\n"})

        for img in teaching_images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": img["b64"]},
            })
            content.append({"type": "text", "text": img["description"]})

        # Now the live chart + data
        content.append({"type": "text", "text": f"\n---\n\n## Current Setup: {pair}\n"})

        if narrative:
            content.append({"type": "text", "text": f"**TA Narrative:** {narrative}\n"})

        # Key indicators as structured text
        ind_text = self._format_indicators(pair, indicators)
        content.append({"type": "text", "text": f"**Indicators:**\n```\n{ind_text}\n```\n"})

        # The chart image
        content.append({"type": "text", "text": "**Current M15 Chart:**"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": chart_b64},
        })

        # Inject trader annotations as hard context — these are Tim's eyes on the chart
        trader_context = self._format_trader_context(pair, trader_annotations or [])
        if trader_context:
            content.append({"type": "text", "text": f"\n---\n\n{trader_context}"})
            logger.info("Injected trader annotations into validator prompt for %s (%d annotations)",
                        pair, len(trader_annotations))

        content.append({
            "type": "text",
            "text": (
                "\nBased on the teaching examples, this chart, and the trader's analysis above, "
                "what is your verdict? Look at the WHOLE PICTURE — fan width panel, BB expansion, "
                "EMA separation, RSI position, candle behavior. If trader annotations are present, "
                "your reasoning MUST address them. Respond with JSON only."
            ),
        })

        # Call Claude
        client = anthropic.Anthropic(api_key=self.api_key)
        system_prompt = self._load_system_prompt()

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=500,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )
            raw_text = response.content[0].text
            api_cost = self._estimate_cost(response)
            elapsed = time.time() - start_time

            logger.info("Vision validator response for %s in %.1fs ($%.4f): %s",
                        pair, elapsed, api_cost, raw_text[:200])

        except Exception as e:
            logger.error("Vision validator API error for %s: %s", pair, e)
            return {"verdict": "SKIP", "reasoning": f"API error: {e}", "confidence": 0}

        # Parse response
        result = self._parse_response(raw_text)
        result["api_cost"] = api_cost
        result["elapsed_seconds"] = elapsed
        result["model_used"] = self.model
        result["chart_path"] = chart_path

        # Flag if the validator overrode the trader's explicit directional call
        if trader_annotations:
            trader_directions = []
            for a in trader_annotations:
                d = str(a.get("direction", "")).upper()
                if d in ("BUY", "SELL"):
                    trader_directions.append(d)
                elif "sell" in str(a.get("note", "")).lower() and a.get("annotation_type") in ("bias", "pattern"):
                    trader_directions.append("SELL")
                elif " buy" in str(a.get("note", "")).lower() and a.get("annotation_type") in ("bias", "pattern"):
                    trader_directions.append("BUY")
            if trader_directions:
                dominant_trader = max(set(trader_directions), key=trader_directions.count)
                validator_dir = result.get("direction", "")
                if validator_dir and validator_dir != dominant_trader:
                    result["overrode_trader_call"] = True
                    result["trader_called"] = dominant_trader
                    logger.warning(
                        "⚠️ VALIDATOR OVERRODE TRADER: %s — Trader said %s, validator says %s",
                        pair, dominant_trader, validator_dir
                    )
                else:
                    result["overrode_trader_call"] = False
                    result["trader_called"] = dominant_trader

        # Log to DB
        self._log_verdict(pair, result, chart_path, indicators, alert_id)
        self._log_training_data(pair, chart_path, content, raw_text, result)

        return result

    def _format_indicators(self, pair: str, indicators: Dict) -> str:
        """Format indicators as readable text for the prompt."""
        lines = []
        
        # Key fields the validator needs
        fields = [
            ("fan_state", "Fan State"),
            ("fan_direction", "Fan Direction"),
            ("separation_pct", "EMA Separation %"),
            ("separation_velocity", "Separation Velocity"),
            ("bb_expanding", "BB Expanding"),
            ("bb_width", "BB Width"),
            ("rsi", "RSI"),
            ("stoch_k", "Stoch K"),
            ("stoch_d", "Stoch D"),
            ("atr", "ATR"),
            ("trend_health", "Trend Health"),
            ("reversal_risk", "Reversal Risk"),
            ("e100_distance_pips", "E100 Distance (pips)"),
            ("bars_since_cross", "Bars Since Cross"),
            ("price", "Current Price"),
        ]

        for key, label in fields:
            val = indicators.get(key)
            if val is not None:
                if isinstance(val, float):
                    lines.append(f"{label}: {val:.4f}")
                else:
                    lines.append(f"{label}: {val}")

        return "\n".join(lines) if lines else "No indicator data available"

    def _parse_response(self, raw_text: str) -> Dict:
        """Parse JSON response from Claude."""
        # Strip markdown code blocks if present
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON in the response
            import re
            match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group())
                except json.JSONDecodeError:
                    logger.error("Failed to parse validator response: %s", text[:300])
                    return {"verdict": "SKIP", "reasoning": f"Parse error: {text[:200]}", "confidence": 0}
            else:
                logger.error("No JSON found in validator response: %s", text[:300])
                return {"verdict": "SKIP", "reasoning": f"No JSON: {text[:200]}", "confidence": 0}

        # Normalize
        verdict = result.get("verdict", "SKIP").upper()
        if verdict not in ("TRADE_NOW", "SNIPE", "SKIP"):
            verdict = "SKIP"

        direction = result.get("direction")
        if direction:
            direction = direction.upper()
            if direction not in ("BUY", "SELL"):
                direction = None

        return {
            "verdict": verdict,
            "direction": direction,
            "confidence": float(result.get("confidence", 0)),
            "reasoning": result.get("reasoning", ""),
            "sl_atr": float(result.get("sl_atr", 2.5)),
            "watch_for": result.get("watch_for"),
        }

    def _estimate_cost(self, response) -> float:
        """Estimate API cost from response usage."""
        usage = response.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        # Sonnet 4 pricing: $3/1M input, $15/1M output
        cost = (input_tokens * 3.0 / 1_000_000) + (output_tokens * 15.0 / 1_000_000)
        return round(cost, 4)

    def _log_verdict(self, pair: str, result: Dict, chart_path: str, indicators: Dict, alert_id: int = None):
        """Log verdict to vision_verdicts table."""
        try:
            conn = get_trading_forex()
            conn.execute("""
                INSERT INTO vision_verdicts
                (timestamp, pair, alert_id, verdict, direction, confidence, chart_path,
                 indicators_json, reasoning, model_used, api_cost)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                pair,
                alert_id,
                result["verdict"],
                result.get("direction"),
                result.get("confidence", 0),
                chart_path,
                json.dumps(indicators),
                result.get("reasoning", ""),
                result.get("model_used", self.model),
                result.get("api_cost", 0),
            ))
            conn.commit()
        except Exception as e:
            logger.error("Failed to log vision verdict: %s", e)

    def _log_training_data(self, pair: str, chart_path: str, input_content: list, output_text: str, result: Dict):
        """Log to vision_training_data for future distillation."""
        try:
            conn = get_trading_forex()
            # Store a compact version of input (skip base64 images to save space)
            input_summary = json.dumps([
                c for c in input_content
                if c.get("type") == "text"
            ])
            conn.execute("""
                INSERT INTO vision_training_data
                (timestamp, agent, chart_path, input_prompt, output_response, verdict, model_used)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                "validator",
                chart_path,
                input_summary,
                output_text,
                result["verdict"],
                result.get("model_used", self.model),
            ))
            conn.commit()
        except Exception as e:
            logger.error("Failed to log training data: %s", e)
