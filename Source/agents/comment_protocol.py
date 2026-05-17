"""
Comment Protocol — standardised task comment communication for the trading agent team.

Every trading cycle is a task. Agents post results as task comments with
structured ``technical_details`` JSON.  The @mention system enables directed
requests between agents.

Schemas are for DOCUMENTATION and optional validation -- not strict enforcement.
Comments always succeed even if technical_details does not match the schema.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trading_bot.comment_protocol")


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


class MessageType:
    """Constants for comment message types."""

    DATA_DELIVERY = "data_delivery"
    ANALYSIS_RESULT = "analysis_result"
    VALIDATION_RESULT = "validation_result"
    TRADE_DECISION = "trade_decision"
    EXECUTION_REPORT = "execution_report"
    CYCLE_SUMMARY = "cycle_summary"
    REQUEST = "request"
    STATUS_UPDATE = "status_update"
    ERROR = "error"

    ALL = [
        DATA_DELIVERY,
        ANALYSIS_RESULT,
        VALIDATION_RESULT,
        TRADE_DECISION,
        EXECUTION_REPORT,
        CYCLE_SUMMARY,
        REQUEST,
        STATUS_UPDATE,
        ERROR,
    ]


# ---------------------------------------------------------------------------
# Technical-details schemas (documentation / soft validation)
# ---------------------------------------------------------------------------

SCHEMAS: Dict[str, Dict[str, Any]] = {
    "oanda_data": {
        "instrument": str,
        "candles": {"count": int, "granularity": str, "latest_time": str},
        "pricing": {"bid": float, "ask": float, "spread_pips": float},
        "account": {"balance": float, "unrealized_pl": float, "margin_used": float},
    },
    "intelligence": {
        "instrument": str,
        "news": {
            "sentiment": float,
            "high_impact_events": list,
            "next_event_minutes": int,
        },
        "wolfram": {"statistical_significance": float, "pattern_probability": float},
        "weather": {"impact_level": str, "affected_currencies": list},
    },
    "technical_analysis": {
        "instrument": str,
        "timeframe": str,
        "indicators": {
            "rsi": float,
            "macd_signal": str,
            "ema_trend": str,
            "adx": float,
            "regime": str,
        },
        "candlestick_patterns": [
            {"name": str, "direction": str, "confidence": float}
        ],
        "chart_patterns": [{"name": str, "direction": str, "target": float}],
        "confluence": {"score": float, "direction": str, "breakdown": dict},
        "mtf_alignment": {"aligned": bool, "direction": str, "score": float},
    },
    "data_validator": {
        "gate1_passed": bool,
        "gate1_confidence": float,
        "gate1_issues": list,
        "gate2_passed": bool,
        "gate2_details": dict,
        "contradictions": {"count": int, "critical": int, "details": list},
        "llm_decision": {"action": str, "confidence": float, "reasoning": str},
    },
    "trade_decision": {
        "action": str,
        "instrument": str,
        "allowed": bool,
        "position_size": int,
        "stop_loss": float,
        "take_profit": float,
        "risk_pct": float,
        "profile": str,
        "reasons": list,
        "cycle_score": float,
    },
    "execution_report": {
        "trade_id": str,
        "instrument": str,
        "direction": str,
        "units": int,
        "entry_price": float,
        "stop_loss": float,
        "take_profit": float,
        "partial_tp_plan": dict,
        "status": str,
    },
    "cycle_summary": {
        "cycle_id": str,
        "instrument": str,
        "timestamp": str,
        "data_agents_completed": int,
        "analysis_score": float,
        "validation_passed": bool,
        "trade_placed": bool,
        "trade_details": dict,
        "duration_seconds": float,
    },
}

# @mention regex
_MENTION_RE = re.compile(r"@(\w+):")


# ---------------------------------------------------------------------------
# CommentProtocol
# ---------------------------------------------------------------------------


class CommentProtocol:
    """Standardised task comment communication for the trading agent team.

    Every trading cycle is a task.  Agents post results as task comments with
    structured ``technical_details`` JSON.  The @mention system enables directed
    requests between agents.
    """

    def __init__(self, task_comment_manager=None, workspace_sharing_manager=None):
        """Initialise with the Jarvis task-comment system.

        Parameters
        ----------
        task_comment_manager : WorkspaceTaskCommentManager | None
            Pre-built manager.  If *None*, lazy-imported.
        workspace_sharing_manager : WorkspaceSharingManager | None
            Pre-built sharing manager for task creation.  If *None*,
            lazy-imported.
        """
        self._comments = task_comment_manager
        self._sharing = workspace_sharing_manager

    # ------------------------------------------------------------------
    # Lazy accessors
    # ------------------------------------------------------------------

    @property
    def comments(self):
        """Lazy-load WorkspaceTaskCommentManager."""
        if self._comments is None:
            try:
                from Database.workspace_task_comments import (
                    WorkspaceTaskCommentManager,
                )

                self._comments = WorkspaceTaskCommentManager()
            except ImportError:
                logger.warning(
                    "WorkspaceTaskCommentManager not available -- running headless"
                )
        return self._comments

    @property
    def sharing(self):
        """Lazy-load WorkspaceSharingManager (for task creation)."""
        if self._sharing is None:
            try:
                from Jarvis_Agent_SDK.import_helper import get_workspace_sharing

                self._sharing = get_workspace_sharing()
            except ImportError:
                logger.warning(
                    "WorkspaceSharing not available -- running headless"
                )
        return self._sharing

    # ------------------------------------------------------------------
    # Cycle task management
    # ------------------------------------------------------------------

    def create_cycle_task(
        self,
        workspace_id: int,
        instrument: str,
        timeframe: str,
    ) -> Optional[int]:
        """Create a new trading-cycle task in the parent workspace.

        Parameters
        ----------
        workspace_id : int
            The parent *Forex Trading Team* workspace ID.
        instrument : str
            Instrument being traded (e.g. ``"EUR_USD"``).
        timeframe : str
            Timeframe (e.g. ``"H1"``).

        Returns
        -------
        int | None
            The created task ID, or *None* on failure.
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        title = f"Trading Cycle: {instrument} {timeframe} {ts}"
        description = (
            f"Automated trading cycle for {instrument} on {timeframe}. "
            "Agents post results as comments on this task."
        )

        if self.sharing is None:
            logger.error("WorkspaceSharingManager unavailable -- cannot create task")
            return None

        try:
            from db_pool import get_workspaces

            conn = get_workspaces()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO workspace_tasks (
                    workspace_id, title, description, status, priority, metadata
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    title,
                    description,
                    "active",
                    "high",
                    json.dumps(
                        {
                            "type": "trading_cycle",
                            "instrument": instrument,
                            "timeframe": timeframe,
                            "created_at": ts,
                        }
                    ),
                ),
            )
            task_id = cursor.lastrowid
            logger.info("Created cycle task %d: %s", task_id, title)
            return task_id
        except Exception as exc:
            logger.error("create_cycle_task failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Posting results
    # ------------------------------------------------------------------

    def post_agent_result(
        self,
        task_id: int,
        agent_name: str,
        message_type: str,
        content_summary: str,
        technical_details: Dict[str, Any],
        mentions: Optional[List[str]] = None,
    ) -> Optional[int]:
        """Post an agent result as a task comment.

        Parameters
        ----------
        task_id : int
            The cycle task to comment on.
        agent_name : str
            The posting agent's name.
        message_type : str
            One of :class:`MessageType` constants.
        content_summary : str
            Human-readable summary text.
        technical_details : dict
            Structured JSON payload.
        mentions : list[str] | None
            Agent names to @mention in the content.

        Returns
        -------
        int | None
            The comment ID, or *None* on failure.
        """
        # Format content with @mentions
        formatted = content_summary
        if mentions:
            mention_prefix = " ".join(f"@{m}:" for m in mentions)
            formatted = f"{mention_prefix} {content_summary}"

        # Validate against schema (warn only, never block)
        self._validate_schema(agent_name, message_type, technical_details)

        # Add metadata to technical_details
        enriched = dict(technical_details)
        enriched["_timestamp"] = datetime.now(timezone.utc).isoformat()
        enriched["_agent"] = agent_name
        enriched["_message_type"] = message_type

        if self.comments is None:
            logger.error("TaskCommentManager unavailable -- cannot post comment")
            return None

        try:
            success, result = self.comments.add_comment(
                task_id=task_id,
                author_id=agent_name,
                author_type="agent",
                content=formatted,
                technical_details=enriched,
            )
            if success:
                logger.info(
                    "Posted %s result for %s on task %d (comment %s)",
                    message_type,
                    agent_name,
                    task_id,
                    result,
                )
                return result
            else:
                logger.error("add_comment failed: %s", result)
                return None
        except Exception as exc:
            logger.error("post_agent_result failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Reading threads
    # ------------------------------------------------------------------

    def get_cycle_thread(self, task_id: int) -> List[Dict[str, Any]]:
        """Get all comments for a cycle task, sorted chronologically.

        Returns
        -------
        list[dict]
            Each dict has: agent_name, message_type, content,
            technical_details, timestamp, mentions.
        """
        if self.comments is None:
            return []

        try:
            success, raw = self.comments.get_task_comments(task_id)
            if not success:
                logger.warning("get_task_comments failed: %s", raw)
                return []

            thread = []
            for comment in raw:
                td = comment.get("technical_details")
                if isinstance(td, str):
                    try:
                        td = json.loads(td)
                    except json.JSONDecodeError:
                        td = {}

                content = comment.get("content", "")
                thread.append(
                    {
                        "agent_name": td.get("_agent", comment.get("author_id", "")),
                        "message_type": td.get("_message_type", ""),
                        "content": content,
                        "technical_details": td,
                        "timestamp": td.get("_timestamp", comment.get("created_at", "")),
                        "mentions": self.parse_mentions(content),
                    }
                )
            return thread
        except Exception as exc:
            logger.error("get_cycle_thread failed: %s", exc)
            return []

    def get_agent_results(
        self, task_id: int, agent_name: str
    ) -> List[Dict[str, Any]]:
        """Filter cycle thread to a specific agent's comments.

        Returns their technical_details (what other agents consume).
        """
        thread = self.get_cycle_thread(task_id)
        return [
            entry["technical_details"]
            for entry in thread
            if entry["agent_name"] == agent_name
        ]

    # ------------------------------------------------------------------
    # @mention helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_mentions(content: str) -> List[str]:
        """Extract @agent_name mentions from comment content.

        Pattern: ``@(\\w+):``

        Returns
        -------
        list[str]
            List of mentioned agent names.
        """
        return _MENTION_RE.findall(content)

    @staticmethod
    def format_mention(target_agent: str, message: str) -> str:
        """Format a mention string.

        Returns
        -------
        str
            ``"@{target_agent}: {message}"``
        """
        return f"@{target_agent}: {message}"

    # ------------------------------------------------------------------
    # Schema validation (soft)
    # ------------------------------------------------------------------

    def _validate_schema(
        self,
        agent_name: str,
        message_type: str,
        technical_details: Dict[str, Any],
    ) -> None:
        """Warn (never block) if technical_details doesn't match the schema."""
        schema_key = self._schema_key_for(agent_name, message_type)
        if schema_key is None or schema_key not in SCHEMAS:
            return

        schema = SCHEMAS[schema_key]
        missing = []
        for key in schema:
            if key not in technical_details:
                missing.append(key)

        if missing:
            logger.warning(
                "Schema mismatch for %s/%s: missing keys %s",
                agent_name,
                message_type,
                missing,
            )

    @staticmethod
    def _schema_key_for(agent_name: str, message_type: str) -> Optional[str]:
        """Map (agent_name, message_type) to a SCHEMAS key."""
        # Direct agent name match
        if agent_name in SCHEMAS:
            return agent_name
        # Message-type to schema mapping
        _type_map = {
            MessageType.TRADE_DECISION: "trade_decision",
            MessageType.EXECUTION_REPORT: "execution_report",
            MessageType.CYCLE_SUMMARY: "cycle_summary",
        }
        return _type_map.get(message_type)
