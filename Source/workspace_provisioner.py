"""
Workspace Provisioner — creates per-user trading workspace + agent team.

On registration:
1. Creates parent workspace "Trading Team — {username}"
2. Creates 8 child workspaces (one per agent)
3. Clones the template agent team → new agent entries owned by this user
4. Clones all agent skills from the template
5. Assigns cloned agents to the user's workspaces
6. Saves workspace ID on the user record
7. Creates default trading preferences

Each user gets their OWN agents, skills, swarm, and workspace tree.
All cycle data, conversations, and decisions are scoped to the user's workspace.
"""

import os
import sqlite3
import logging
import time
import uuid
from pathlib import Path

from db_pool import get_workspaces, get_core

logger = logging.getLogger("workspace_provisioner")

# Template team — the "master copy" agents get cloned from
TEMPLATE_TEAM_ID = "2676292a-0f9d-4626-a245-3f51dde60762"

AGENT_NAMES = [
    "oanda_data",
    "intelligence",
    "technical_analyst",
    "validator",
    "execution",
    "trade_monitor",
    "reporter",
    "cycle_orchestrator",
]

AGENT_DESCRIPTIONS = {
    "oanda_data": "Market data collection from OANDA API",
    "intelligence": "News, weather, and macro intelligence gathering",
    "technical_analyst": "Technical indicator computation and scoring",
    "validator": "Trade validation and risk checks",
    "execution": "Order execution and position management",
    "trade_monitor": "Open position monitoring (5-min cron)",
    "reporter": "Performance reporting and learning loop",
    "cycle_orchestrator": "Master trader — orchestrates all decisions",
}

_DB_DIR = Path(__file__).parent.parent.parent / "Database"
_BOARDROOM_DB = _DB_DIR / "v2" / "workspaces.db"
_AGENTS_DB = _DB_DIR / "v2" / "agents.db"
_USERS_DB = _DB_DIR / "v2" / "core.db"


def provision_trading_workspace(user_id: int, username: str, is_founder: bool = False) -> dict:
    """
    Create a complete trading workspace + cloned agent team for a user.

    Args:
        user_id: The user's integer primary key.
        username: Display name used to label the parent workspace.
        is_founder: When True, sets users.is_founder=1 on the user record.
                    Defaults to False (regular subscriber provisioning).

    Returns:
        parent_workspace_id: int
        agent_workspaces: {agent_name: workspace_id}
        team_id: str (new team UUID for this user)
        agent_ids: {agent_name: agent_id}
        provisioned: bool
    """
    boardroom = get_workspaces()
    # ATTACH agents.db so agent_registry + agent_skills are accessible on the same connection
    # (v2 migration split the old boardroom.db into workspaces.db + agents.db)
    boardroom.execute(f"ATTACH DATABASE '{_AGENTS_DB}' AS agents_db")

    try:
        # --- Check if already provisioned ---
        existing = _get_existing_workspace(boardroom, user_id)
        if existing:
            logger.info("User %s (id=%d) already provisioned", username, user_id)
            return {**existing, "provisioned": False}

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        new_team_id = str(uuid.uuid4())

        # --- 1. Create parent workspace ---
        boardroom.execute(
            """INSERT INTO workspaces
               (name, description, created_by, created_at, updated_at, status, owner_id)
               VALUES (?, ?, ?, ?, ?, 'active', ?)""",
            (f"Trading Team — {username}",
             f"Forex trading workspace for {username}",
             "system", now, now, user_id),
        )
        parent_id = boardroom.execute("SELECT last_insert_rowid()").fetchone()[0]

        # --- 2. Load template agents + skills ---
        template_agents = boardroom.execute(
            """SELECT agent_id, agent_name, agent_type, capabilities, metadata
               FROM agents_db.agent_registry WHERE team_id=?""",
            (TEMPLATE_TEAM_ID,),
        ).fetchall()

        if not template_agents:
            raise RuntimeError(f"Template team {TEMPLATE_TEAM_ID} not found in agent_registry")

        # --- 3. Clone agents + skills + create workspaces ---
        agent_workspaces = {}
        agent_ids = {}

        for tmpl_agent_id, agent_name, agent_type, capabilities, metadata in template_agents:
            # Create agent workspace
            desc = AGENT_DESCRIPTIONS.get(agent_name, f"{agent_name} workspace")
            boardroom.execute(
                """INSERT INTO workspaces
                   (name, description, created_by, created_at, updated_at,
                    status, parent_workspace_id, owner_id)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
                (agent_name, desc, "system", now, now, parent_id, user_id),
            )
            ws_id = boardroom.execute("SELECT last_insert_rowid()").fetchone()[0]
            agent_workspaces[agent_name] = ws_id

            # Clone agent
            new_agent_id = str(uuid.uuid4())
            agent_ids[agent_name] = new_agent_id
            boardroom.execute(
                """INSERT INTO agents_db.agent_registry
                   (id, agent_id, agent_name, agent_type, module_name,
                    capabilities, status, created_at, updated_at, metadata, team_id)
                   VALUES (?, ?, ?, ?, 'trading_bot', ?, 'active', ?, ?, ?, ?)""",
                (str(uuid.uuid4()), new_agent_id, agent_name, agent_type,
                 capabilities, time.time(), time.time(), metadata or '{}', new_team_id),
            )

            # Clone skills
            template_skills = boardroom.execute(
                "SELECT skill_name, skill_type, definition_json FROM agents_db.agent_skills WHERE agent_id=?",
                (tmpl_agent_id,),
            ).fetchall()
            for skill_name, skill_type, definition_json in template_skills:
                boardroom.execute(
                    """INSERT INTO agents_db.agent_skills
                       (id, agent_id, skill_name, skill_type, definition_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), new_agent_id, skill_name, skill_type, definition_json),
                )

            # Assign agent to workspace
            boardroom.execute(
                """INSERT INTO workspace_agent_assignments
                   (workspace_id, agent_id, agent_name, agent_type, role, status)
                   VALUES (?, ?, ?, 'trading_agent', 'worker', 'active')""",
                (ws_id, new_agent_id, agent_name),
            )

            logger.info(
                "Cloned agent %s → %s (ws=%d, %d skills)",
                agent_name, new_agent_id[:12], ws_id, len(template_skills),
            )

        boardroom.commit()

        # --- 4. Save workspace on user record ---
        _save_workspace_to_user(user_id, parent_id, new_team_id, is_founder=is_founder)

        # --- 5. Create default trading preferences ---
        _ensure_trading_defaults(user_id)

        logger.info(
            "Provisioned workspace for %s: parent=%d, team=%s, %d agents",
            username, parent_id, new_team_id[:12], len(agent_ids),
        )

        return {
            "parent_workspace_id": parent_id,
            "agent_workspaces": agent_workspaces,
            "team_id": new_team_id,
            "agent_ids": agent_ids,
            "provisioned": True,
        }

    except Exception as e:
        boardroom.rollback()
        logger.error("Provisioning failed for %s: %s", username, e)
        raise
    finally:
        try:
            boardroom.execute("DETACH DATABASE agents_db")
        except Exception:
            pass


def _get_existing_workspace(db, user_id: int) -> dict | None:
    """Check if user already has a trading workspace."""
    row = db.execute(
        "SELECT id, name FROM workspaces WHERE name LIKE 'Trading Team —%' AND owner_id=?",
        (user_id,),
    ).fetchone()
    if not row:
        return None

    parent_id = row[0]

    # Get agent workspaces + IDs
    agent_ws = {}
    agent_ids = {}
    rows = db.execute(
        """SELECT waa.agent_name, waa.workspace_id, waa.agent_id
           FROM workspace_agent_assignments waa
           JOIN workspaces w ON w.id = waa.workspace_id
           WHERE w.parent_workspace_id = ? AND w.owner_id = ?""",
        (parent_id, user_id),
    ).fetchall()
    for name, ws_id, agent_id in rows:
        agent_ws[name] = ws_id
        agent_ids[name] = agent_id

    # Get team_id from one of the agents
    team_id = None
    if agent_ids:
        first_aid = list(agent_ids.values())[0]
        team_row = db.execute(
            "SELECT team_id FROM agents_db.agent_registry WHERE agent_id=?",
            (first_aid,),
        ).fetchone()
        if team_row:
            team_id = team_row[0]

    return {
        "parent_workspace_id": parent_id,
        "agent_workspaces": agent_ws,
        "team_id": team_id,
        "agent_ids": agent_ids,
    }


def _apply_user_workspace_link(
    conn,
    user_id: int,
    workspace_id: int,
    is_founder: bool,
) -> None:
    """Set trading_workspace_id (and optionally is_founder) on a user row.

    This is a small, testable helper so the provisioner's user-update logic
    can be exercised in isolation without standing up the full workspace pipeline.

    Args:
        conn: An open sqlite3 connection to core.db (must already be in a
              transaction context — caller is responsible for commit/rollback).
        user_id: The user's integer primary key.
        workspace_id: The newly created parent workspace id to assign.
        is_founder: When True, also sets users.is_founder=1.
    """
    conn.execute(
        "UPDATE users SET trading_workspace_id=?, is_founder=? WHERE id=?",
        (workspace_id, 1 if is_founder else 0, user_id),
    )


def _save_workspace_to_user(
    user_id: int,
    parent_workspace_id: int,
    team_id: str,
    is_founder: bool = False,
) -> None:
    """Save workspace ID, team ID, and founder flag on the user record."""
    db = get_core()
    # Ensure columns exist (guard for DBs that predate Task 3 / Task 3.5 migrations)
    cols = [c[1] for c in db.execute("PRAGMA table_info(users)").fetchall()]
    if "trading_workspace_id" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN trading_workspace_id INTEGER")
    if "trading_team_id" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN trading_team_id TEXT")
    if "is_founder" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN is_founder INTEGER NOT NULL DEFAULT 0")

    db.execute(
        "UPDATE users SET trading_team_id=? WHERE id=?",
        (team_id, user_id),
    )
    _apply_user_workspace_link(db, user_id, parent_workspace_id, is_founder)
    db.commit()
    logger.info(
        "Saved workspace=%d team=%s is_founder=%s on user %d",
        parent_workspace_id, team_id[:12], is_founder, user_id,
    )


def _ensure_trading_defaults(user_id: int):
    """Create default trading preferences for a new user."""
    db = get_core()
    try:
        existing = {r[0] for r in db.execute(
            "SELECT pref_key FROM trading_preferences WHERE user_id=?", (user_id,),
        ).fetchall()}

        defaults = {
            "active_pair": "EUR_USD",
            "timeframe": "M15",
            "min_confluence": "30",
            "max_risk_pct": "2.0",
            "max_daily_loss_pct": "5.0",
            "max_concurrent": "3",
            "min_rr_ratio": "1.5",
        }
        for key, value in defaults.items():
            if key not in existing:
                db.execute(
                    "INSERT INTO trading_preferences (user_id, pref_key, pref_value) VALUES (?, ?, ?)",
                    (user_id, key, value),
                )
        db.commit()
    except Exception as e:
        logger.warning("Trading defaults for user %d: %s", user_id, e)


def get_user_workspace(user_id: int) -> dict | None:
    """Get a user's trading workspace info. Returns None if not provisioned."""
    db = get_workspaces()
    db.execute(f"ATTACH DATABASE '{_AGENTS_DB}' AS agents_db")
    try:
        return _get_existing_workspace(db, user_id)
    finally:
        try:
            db.execute("DETACH DATABASE agents_db")
        except Exception:
            pass


def get_user_team_id(user_id: int) -> str | None:
    """Get the user's team_id for loading their agents into SwarmHandler."""
    db = get_core()
    row = db.execute(
        "SELECT trading_team_id FROM users WHERE id=?", (user_id,),
    ).fetchone()
    return row[0] if row else None
