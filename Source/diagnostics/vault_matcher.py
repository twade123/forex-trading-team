"""FTS search of ~/Jarvis/knowledge/_index.db."""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional

VAULT_ROOT = os.path.expanduser("~/Jarvis/knowledge")
INDEX_DB = os.path.join(VAULT_ROOT, "_index.db")


def _sanitize_fts(query: str) -> str:
    """FTS5 MATCH disallows bare special chars; quote each term."""
    terms = [t for t in query.replace('"', "").split() if t]
    return " ".join(f'"{t}"' for t in terms) if terms else '""'


def match_symptom(description: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Search the vault FTS index for the given symptom description.

    Returns a list of {path, snippet} dicts (snippet is first 400 chars
    of the matched vault file, if readable).
    """
    if not os.path.exists(INDEX_DB):
        return []
    q = _sanitize_fts(description)
    conn = sqlite3.connect(INDEX_DB)
    try:
        rows = conn.execute(
            "SELECT path FROM fts_content WHERE fts_content MATCH ? LIMIT ?",
            (q, limit),
        ).fetchall()
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for (p,) in rows:
        full = os.path.join(VAULT_ROOT, p)
        snippet = ""
        if os.path.exists(full):
            with open(full, "r", errors="ignore") as f:
                snippet = f.read(400)
        out.append({"path": p, "snippet": snippet})
    return out


def recent_patterns(agent: Optional[str] = None, days: int = 7) -> List[Dict[str, Any]]:
    """Walk agents/<agent>/log/ or collective/patterns/ for recent files.

    When `agent` is provided, scans agents/<agent>/log/; otherwise scans
    collective/patterns/. Returns {path, snippet} for files modified within
    the last `days` days.
    """
    import time
    cutoff = time.time() - days * 86400
    if agent:
        search_roots = [os.path.join(VAULT_ROOT, "agents", agent, "log")]
    else:
        search_roots = [os.path.join(VAULT_ROOT, "collective", "patterns")]
    out: List[Dict[str, Any]] = []
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for f in sorted(os.listdir(root), reverse=True):
            full = os.path.join(root, f)
            if os.path.isfile(full) and os.path.getmtime(full) >= cutoff:
                with open(full, "r", errors="ignore") as fh:
                    body = fh.read(800)
                out.append({"path": os.path.relpath(full, VAULT_ROOT), "snippet": body})
    return out
