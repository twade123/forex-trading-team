#!/usr/bin/env python3
"""
update_system_road_map.py
Regenerates system_road_map.md by introspecting all V2 databases and known file stores.
Prints a diff summary of what changed.

Usage:
    python "Forex Trading Team/Source/scripts/update_system_road_map.py"
"""

import sqlite3
import os
import json
import glob
import difflib
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths — resolve relative to Jarvis root
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
JARVIS_ROOT = SCRIPT_DIR.parents[2]  # scripts -> Source -> Forex Trading Team -> Jarvis

V2_DB_DIR = JARVIS_ROOT / "Database" / "v2"
KNOWLEDGE_DB = JARVIS_ROOT / "knowledge" / "_index.db"
INTENTS_DIR = JARVIS_ROOT / "Intents"
MIROFISH_SIM_DIR = JARVIS_ROOT / "MiroFish" / "simulation_output"
OUTPUT_FILE = JARVIS_ROOT / "Forex Trading Team" / "Source" / "system_road_map.md"

# Non-V2 databases with notes
EXTRA_DBS = [
    {
        "path": JARVIS_ROOT / "Forex Trading Team" / "Data" / "trade_log.db",
        "section": "Trade P&L / Flight Log",
        "note": (
            "**NOTE:** `manual_trades` is where actual win/loss results are recorded by the "
            "position guardian. `live_trades` in this DB does NOT contain reliable outcome "
            "data — **always use `manual_trades` for P&L analysis.**"
        ),
    },
    {
        "path": JARVIS_ROOT / "Forex Trading Team" / "Source" / "flight_recorder.db",
        "section": "Flight Recorder",
        "note": (
            "Per-cycle execution audit log. Records every stage of the trading pipeline "
            "(`flight_log`) plus live trade phase transitions (`trade_phases`) and workflow "
            "anomalies (`workflow_findings`)."
        ),
    },
    {
        "path": JARVIS_ROOT / "Forex Trading Team" / "Data" / "historical_cache.db",
        "section": "Historical Cache",
        "note": "Short-term OHLCV candle cache used by the pipeline to avoid repeated broker API calls.",
    },
]

# Tables to skip (internal SQLite / FTS internals)
SKIP_TABLE_PATTERNS = {"sqlite_", "fts_", "_data", "_idx", "_docsize", "_config", "_content"}


def skip_table(name: str) -> bool:
    return any(name.startswith(p) or name.endswith(p) for p in SKIP_TABLE_PATTERNS)


def introspect_db(db_path: Path) -> dict:
    """Return {table: {columns: [...], row_count: int}} for a SQLite database."""
    result = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall() if not skip_table(r[0])]
        for tbl in tables:
            try:
                cur.execute(f'PRAGMA table_info("{tbl}")')
                columns = [(r[1], r[2]) for r in cur.fetchall()]  # (name, type)
                cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
                row_count = cur.fetchone()[0]
                result[tbl] = {"columns": columns, "row_count": row_count}
            except Exception:
                result[tbl] = {"columns": [], "row_count": -1}
        conn.close()
    except Exception as e:
        print(f"  [WARN] Could not open {db_path.name}: {e}")
    return result


def check_file_stores() -> dict:
    """Return info about known file-based data stores."""
    stores = {}

    # Intents JSON files
    if INTENTS_DIR.exists():
        intent_files = list(INTENTS_DIR.glob("*.json"))
        stores["Intents/"] = {
            "path": str(INTENTS_DIR.relative_to(JARVIS_ROOT)),
            "files": [f.name for f in intent_files],
            "count": len(intent_files),
        }

    # MiroFish simulation output
    if MIROFISH_SIM_DIR.exists():
        sim_files = sorted(MIROFISH_SIM_DIR.glob("*"), key=os.path.getmtime, reverse=True)
        stores["MiroFish/simulation_output/"] = {
            "path": str(MIROFISH_SIM_DIR.relative_to(JARVIS_ROOT)),
            "files": [f.name for f in sim_files[:5]],  # show 5 most recent
            "count": len(sim_files),
        }

    # Knowledge vault
    if KNOWLEDGE_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{KNOWLEDGE_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM files")
            file_count = cur.fetchone()[0]
            cur.execute("SELECT file_type, COUNT(*) FROM files GROUP BY file_type ORDER BY COUNT(*) DESC")
            types = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM fts_content")
            fts_count = cur.fetchone()[0]
            conn.close()
            stores["knowledge/_index.db"] = {
                "path": str(KNOWLEDGE_DB.relative_to(JARVIS_ROOT)),
                "file_count": file_count,
                "fts_rows": fts_count,
                "file_types": types,
            }
        except Exception as e:
            stores["knowledge/_index.db"] = {"error": str(e)}

    return stores


def format_columns(columns: list[tuple]) -> str:
    """Format column list as compact string."""
    return ", ".join(f"`{name}` {typ}" for name, typ in columns[:12]) + (
        f" ... +{len(columns)-12} more" if len(columns) > 12 else ""
    )


def build_markdown(db_schemas: dict, extra_db_schemas: list[dict], file_stores: dict) -> str:
    """Build the full markdown document."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Jarvis System Road Map",
        f"*Auto-generated {now}. Run `scripts/update_system_road_map.py` to refresh.*",
        "",
        "---",
        "",
        "## V2 Database Summary",
        "",
        "| Database | Tables | Total Rows (approx) |",
        "|---|---|---|",
    ]

    for db_name, schema in db_schemas.items():
        total_rows = sum(t["row_count"] for t in schema.values() if t["row_count"] >= 0)
        lines.append(f"| `{db_name}` | {len(schema)} | {total_rows:,} |")

    lines += ["", "---", ""]

    for db_name, schema in db_schemas.items():
        lines.append(f"## {db_name}")
        lines.append(f"**Path:** `Database/v2/{db_name}`")
        lines.append("")
        lines.append("| Table | Rows | Key Columns |")
        lines.append("|---|---|---|")
        for tbl, info in sorted(schema.items()):
            cols = format_columns(info["columns"])
            rows = f"{info['row_count']:,}" if info["row_count"] >= 0 else "?"
            lines.append(f"| `{tbl}` | {rows} | {cols} |")
        lines.append("")

    # Non-V2 databases
    lines += ["---", "", "## Non-V2 Databases", ""]
    for entry in extra_db_schemas:
        db_path = entry["db_path"]
        section = entry["section"]
        note = entry["note"]
        schema = entry["schema"]
        lines.append(f"### {section}")
        lines.append(f"**Path:** `{db_path}`")
        lines.append("")
        lines.append(note)
        lines.append("")
        if schema:
            lines.append("| Table | Rows | Key Columns |")
            lines.append("|---|---|---|")
            for tbl, info in sorted(schema.items()):
                cols = format_columns(info["columns"])
                rows = f"{info['row_count']:,}" if info["row_count"] >= 0 else "?"
                lines.append(f"| `{tbl}` | {rows} | {cols} |")
        lines.append("")

    # File stores section
    lines += ["---", "", "## File-Based Stores", ""]


    for store_name, info in file_stores.items():
        lines.append(f"### {store_name}")
        if "error" in info:
            lines.append(f"*Error: {info['error']}*")
        elif store_name == "knowledge/_index.db":
            lines.append(f"**Path:** `{info['path']}`")
            lines.append(f"**Indexed files:** {info['file_count']} | **FTS rows:** {info['fts_rows']}")
            lines.append("")
            lines.append("| File Type | Count |")
            lines.append("|---|---|")
            for ft, count in info["file_types"]:
                lines.append(f"| `{ft}` | {count} |")
            lines.append("")
            lines.append("**FTS5 Search:**")
            lines.append("```sql")
            lines.append("-- knowledge/_index.db")
            lines.append("SELECT path, title FROM fts_content WHERE fts_content MATCH 'your keywords' LIMIT 10;")
            lines.append("-- Column filter: title:scout  |  Phrase: \"EMA fan\"  |  Prefix: scout*")
            lines.append("```")
        else:
            lines.append(f"**Path:** `{info['path']}`")
            lines.append(f"**File count:** {info['count']}")
            if info.get("files"):
                lines.append(f"**Recent files:** {', '.join(info['files'])}")
        lines.append("")

    lines += [
        "---",
        "",
        "## Quick Lookup: Where Data Lives",
        "",
        "| What you need | DB | Table |",
        "|---|---|---|",
        "| **Actual P&L / win-loss outcomes** | `trade_log.db` (non-V2) | **`manual_trades`** — source of truth for realized results |",
        "| Live / open trades (execution state) | `trading_forex.db` | `live_trades` (outcome fields unreliable — use manual_trades for P&L) |",
        "| Active snipe list | `trading_forex.db` | `user_snipe_list WHERE is_active=1` |",
        "| Scout alerts | `trading_forex.db` | `scout_alerts` |",
        "| Scout findings (resolved) | `trading_forex.db` | `scout_findings` |",
        "| Full boardroom decisions | `trading_forex.db` | `trade_decisions` |",
        "| Latest intelligence briefing | `intelligence.db` | `intelligence_packages` (latest row) |",
        "| Per-trade macro snapshot | `intelligence.db` | `intelligence_snapshots_v2` |",
        "| Chart annotations | `trading_forex.db` | `user_chart_annotations` |",
        "| Watch suggestions | `trading_forex.db` | `watch_suggestions` |",
        "| Setup P&L stats | `trading_forex.db` | `setup_revenue` |",
        "| Backtest stats | `trading_forex.db` | `backtest_setup_performance` |",
        "| Agent roster | `agents.db` | `agent_registry` |",
        "| Vault search (FTS5) | `knowledge/_index.db` | `fts_content` |",
        "| Wolfram query templates | File | `Intents/intents_wolfram.json` |",
        "| MiroFish simulation results | File | `MiroFish/simulation_output/*.md` |",
        "",
        "---",
        "",
        "## Key Query Reference",
        "",
        "```sql",
        "-- trading_forex.db",
        "",
        "-- Active snipes",
        "SELECT setup_name, pair, direction, lifetime_win_rate, lifetime_pnl_usd",
        "FROM user_snipe_list WHERE is_active=1 ORDER BY lifetime_win_rate DESC;",
        "",
        "-- Recent trade losses",
        "SELECT pair, setup, direction, pnl_pips, pnl_usd, outcome, entry_time",
        "FROM live_trades WHERE outcome='loss' ORDER BY entry_time DESC LIMIT 10;",
        "",
        "-- Open trades",
        "SELECT id, pair, direction, entry_price, sl_price, tp_price, entry_time",
        "FROM live_trades WHERE status='open';",
        "",
        "-- P&L by setup",
        "SELECT setup, COUNT(*) trades, SUM(pnl_usd) total_usd, AVG(outcome_r) avg_r",
        "FROM live_trades WHERE status='closed' GROUP BY setup ORDER BY total_usd DESC;",
        "",
        "-- High-score untriggered scout alerts",
        "SELECT pair, direction, setup_code, sniper_score, historical_win_rate, timestamp",
        "FROM scout_alerts WHERE snipe_triggered=0 AND sniper_score > 70",
        "ORDER BY timestamp DESC LIMIT 10;",
        "",
        "-- Active watches with progress",
        "SELECT instrument, suggestion_type, conditions_met_count, conditions_total_count,",
        "       validator_verdict, expires_at",
        "FROM watch_suggestions WHERE status='active' ORDER BY conditions_met_count DESC;",
        "",
        "-- Best setups by backtest profit factor (min 30 trades)",
        "SELECT setup, pair, timeframe, win_rate, profit_factor, trade_count, best_session",
        "FROM backtest_setup_performance WHERE trade_count >= 30",
        "ORDER BY profit_factor DESC LIMIT 15;",
        "",
        "-- Promoted setups by total USD",
        "SELECT setup_name, pair, total_trades, win_rate, total_usd, avg_r_multiple",
        "FROM setup_revenue WHERE promoted=1 ORDER BY total_usd DESC;",
        "",
        "-- Trade decisions: approved but failed",
        "SELECT pair, setup_code, sniper_score, outcome_pips, validator_reasoning",
        "FROM trade_decisions WHERE final_action='EXECUTE' AND outcome='loss'",
        "ORDER BY created_at DESC LIMIT 20;",
        "",
        "-- Chart annotations for a pair",
        "SELECT annotation_type, price, direction, note, fan_state, created_at",
        "FROM user_chart_annotations WHERE pair='EUR_USD' AND active=1;",
        "",
        "-- Exit learning by setup/reason",
        "SELECT setup_name, exit_reason, AVG(actual_rr) avg_rr, AVG(pnl_pips) avg_pips, COUNT(*) n",
        "FROM exit_learning GROUP BY setup_name, exit_reason ORDER BY n DESC;",
        "```",
        "",
        "```sql",
        "-- intelligence.db",
        "",
        "-- Latest intelligence briefing",
        "SELECT id, generated_at, substr(package_text,1,500)",
        "FROM intelligence_packages ORDER BY generated_at DESC LIMIT 1;",
        "",
        "-- Macro snapshots where trading was blocked",
        "SELECT instrument, macro_bias, verdict, confidence, summary, pips_result",
        "FROM intelligence_snapshots_v2 WHERE block_trading=1 ORDER BY created_at DESC LIMIT 10;",
        "",
        "-- Intelligence snapshot for a pair",
        "SELECT instrument, macro_bias, verdict, kelly_fraction, pips_result, created_at",
        "FROM intelligence_snapshots_v2 WHERE instrument='EUR_USD' ORDER BY created_at DESC LIMIT 5;",
        "```",
        "",
        "```sql",
        "-- knowledge/_index.db (FTS5)",
        "",
        "-- Full-text search vault",
        "SELECT path, title FROM fts_content WHERE fts_content MATCH 'EMA fan scout signal' LIMIT 10;",
        "",
        "-- Search by file type",
        "SELECT path, title FROM fts_content",
        "  WHERE fts_content MATCH 'loss pattern' AND file_type = 'scout_retrospective' LIMIT 5;",
        "",
        "-- Snippet with context",
        "SELECT path, snippet(fts_content, 2, '[', ']', '...', 32) excerpt",
        "FROM fts_content WHERE fts_content MATCH 'validator rejection' LIMIT 5;",
        "",
        "-- FTS5 syntax: word1 word2 (AND) | word1 OR word2 | \"phrase\" | prefix* | col:term",
        "```",
        "",
        "---",
        f"*Generated by `scripts/update_system_road_map.py` on {now}*",
    ]

    return "\n".join(lines)


def show_diff(old_content: str, new_content: str) -> None:
    """Print a summary diff of what changed."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile="old", tofile="new", n=0))

    if not diff:
        print("No changes detected.")
        return

    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    print(f"\nDiff summary: +{added} lines added, -{removed} lines removed")

    # Show changed sections (hunk headers)
    hunks = [l.strip() for l in diff if l.startswith("@@")]
    if hunks:
        print("Changed regions:")
        for h in hunks[:10]:
            print(f"  {h}")


def main():
    print(f"Jarvis root: {JARVIS_ROOT}")
    print(f"Scanning V2 databases in: {V2_DB_DIR}")
    print()

    # Introspect all V2 databases
    db_schemas = {}
    for db_path in sorted(V2_DB_DIR.glob("*.db")):
        if db_path.stem in ("workspace_shard_template",):
            continue  # Skip template
        print(f"  [{db_path.name}] ...", end=" ", flush=True)
        schema = introspect_db(db_path)
        db_schemas[db_path.name] = schema
        total = sum(t["row_count"] for t in schema.values() if t["row_count"] >= 0)
        print(f"{len(schema)} tables, {total:,} rows")

    print()
    print("Checking file stores...")
    file_stores = check_file_stores()
    for name, info in file_stores.items():
        if "error" in info:
            print(f"  [{name}] ERROR: {info['error']}")
        elif "file_count" in info:
            print(f"  [{name}] {info['file_count']} files indexed")
        else:
            print(f"  [{name}] {info.get('count', '?')} files")

    print()
    print(f"Building markdown → {OUTPUT_FILE}")

    # Introspect extra (non-V2) databases
    extra_db_schemas = []
    for entry in EXTRA_DBS:
        db_path = entry["path"]
        rel_path = str(db_path.relative_to(JARVIS_ROOT)) if db_path.exists() else str(db_path)
        print(f"  [{db_path.name}] ...", end=" ", flush=True)
        if db_path.exists():
            schema = introspect_db(db_path)
            total = sum(t["row_count"] for t in schema.values() if t["row_count"] >= 0)
            print(f"{len(schema)} tables, {total:,} rows")
        else:
            schema = {}
            print("NOT FOUND")
        extra_db_schemas.append({
            "db_path": rel_path,
            "section": entry["section"],
            "note": entry["note"],
            "schema": schema,
        })

    new_content = build_markdown(db_schemas, extra_db_schemas, file_stores)

    # Diff against existing file
    old_content = ""
    if OUTPUT_FILE.exists():
        old_content = OUTPUT_FILE.read_text()

    show_diff(old_content, new_content)

    OUTPUT_FILE.write_text(new_content)
    line_count = new_content.count("\n")
    print(f"\nWrote {line_count} lines to {OUTPUT_FILE.relative_to(JARVIS_ROOT)}")


if __name__ == "__main__":
    main()
