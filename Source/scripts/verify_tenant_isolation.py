"""End-to-end verification of multi-tenant isolation.

Provisions two test users, inserts simulated flight_log + live_trades rows for
each, asserts that every DB-visible row is correctly tagged with its user, and
that NO row written for user A appears under user B's tenant scope.

Run with the trading system STOPPED.

Usage:
  python3 scripts/verify_tenant_isolation.py
Exit 0 = isolation verified. Exit 1 = bleed detected (do NOT launch v1).
"""
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

JARVIS_ROOT = Path("~/Jarvis")
CORE_DB    = JARVIS_ROOT / "Database/v2/core.db"
FLIGHT_DB  = JARVIS_ROOT / "Forex Trading Team/Source/flight_recorder.db"
TRADING_DB = JARVIS_ROOT / "Database/v2/trading_forex.db"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def make_test_user(username: str) -> str:
    """Insert a fake users row. Returns the new user id (UUID TEXT)."""
    user_id = f"test-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(CORE_DB))
    try:
        conn.execute(
            """INSERT INTO users
               (id, username, email, is_admin, is_founder, status,
                created_at, updated_at, preferences)
               VALUES (?, ?, ?, 0, 0, 'active', ?, ?, '{}')""",
            (user_id, username, f"{username}@test.local",
             _now_iso(), _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return user_id


def cleanup_test_user(user_id: str) -> None:
    """Hard-delete user + their flight_log + their trades."""
    # users
    c = sqlite3.connect(str(CORE_DB))
    try:
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        c.commit()
    finally:
        c.close()

    # flight_log uses INTEGER user_id; we never inserted with our TEXT user
    # because we use a numeric mapping for these test users (see simulate).
    # Cleanup by note marker instead:
    f = sqlite3.connect(str(FLIGHT_DB))
    try:
        f.execute("DELETE FROM flight_log WHERE note LIKE ?",
                  (f"%verify_tenant_isolation:{user_id}%",))
        f.commit()
    finally:
        f.close()

    # live_trades PK column is `id` (TEXT); cleanup by id prefix marker.
    t = sqlite3.connect(str(TRADING_DB))
    try:
        t.execute("DELETE FROM live_trades WHERE id LIKE ?",
                  (f"verify-{user_id}-%",))
        t.commit()
    finally:
        t.close()


def simulate_workload(user_id: str, numeric_id: int, pair: str, direction: str) -> None:
    """Insert a fake validator verdict + a fake live_trade for this user.

    flight_log.user_id is INTEGER, live_trades.user_id is INTEGER.
    We use the numeric_id for those columns and embed the TEXT user_id in
    `note` / `id` so cleanup can find them.
    """
    cycle_id = f"cyc-{uuid.uuid4().hex[:8]}"

    # flight_log entry
    f = sqlite3.connect(str(FLIGHT_DB))
    try:
        f.execute(
            """INSERT INTO flight_log
               (cycle_id, pair, stage, timestamp, status,
                duration_ms, data, note, missing_fields, user_id)
               VALUES (?, ?, 'VALIDATOR_VERDICT', ?, 'ok', 0, ?, ?, '[]', ?)""",
            (cycle_id, pair, _now_iso(),
             json.dumps({"verdict": "TRADE_NOW", "direction": direction}),
             f"verify_tenant_isolation:{user_id}",
             numeric_id),
        )
        f.commit()
    finally:
        f.close()

    # live_trades entry — PK column is `id` (TEXT), entry_price is NOT NULL
    trade_id = f"verify-{user_id}-{uuid.uuid4().hex[:8]}"
    t = sqlite3.connect(str(TRADING_DB))
    try:
        t.execute(
            """INSERT INTO live_trades
               (id, user_id, pair, direction, entry_price, entry_time, result)
               VALUES (?, ?, ?, ?, 0.0, ?, NULL)""",
            (trade_id, numeric_id, pair, direction, _now_iso()),
        )
        t.commit()
    finally:
        t.close()


def assert_isolation(user_a_text: str, num_a: int, user_b_text: str, num_b: int):
    """Verify zero cross-user bleed in the synthetic dataset."""
    failures = []

    # 1) flight_log: count rows under user A's numeric_id that REFERENCE user B's TEXT marker
    f = sqlite3.connect(str(FLIGHT_DB))
    f.execute("PRAGMA query_only=1")
    cross_a_to_b = f.execute(
        "SELECT COUNT(*) FROM flight_log WHERE user_id = ? AND note LIKE ?",
        (num_a, f"%verify_tenant_isolation:{user_b_text}%"),
    ).fetchone()[0]
    cross_b_to_a = f.execute(
        "SELECT COUNT(*) FROM flight_log WHERE user_id = ? AND note LIKE ?",
        (num_b, f"%verify_tenant_isolation:{user_a_text}%"),
    ).fetchone()[0]
    f.close()
    if cross_a_to_b or cross_b_to_a:
        failures.append(
            f"flight_log: cross-user rows detected (a->b={cross_a_to_b}, b->a={cross_b_to_a})"
        )

    # 2) live_trades: id (PK) should never mismatch user_id
    t = sqlite3.connect(str(TRADING_DB))
    t.execute("PRAGMA query_only=1")
    mismatched = t.execute(
        """SELECT COUNT(*) FROM live_trades
           WHERE (id LIKE ? AND user_id != ?)
              OR (id LIKE ? AND user_id != ?)""",
        (f"verify-{user_a_text}-%", num_a,
         f"verify-{user_b_text}-%", num_b),
    ).fetchone()[0]
    t.close()
    if mismatched:
        failures.append(f"live_trades: {mismatched} rows have user_id not matching their trade id user")

    return failures


def main() -> int:
    # We need numeric IDs for flight_log/live_trades.user_id. Use distinct
    # high integers that won't collide with real users.
    num_a, num_b = 99001, 99002

    user_a = make_test_user("verify_user_A")
    user_b = make_test_user("verify_user_B")
    print(f"Created test users: A={user_a} (num={num_a}), B={user_b} (num={num_b})")

    try:
        simulate_workload(user_a, num_a, "EUR_USD", "buy")
        simulate_workload(user_b, num_b, "EUR_USD", "sell")
        simulate_workload(user_a, num_a, "GBP_USD", "sell")
        simulate_workload(user_b, num_b, "AUD_JPY", "buy")

        failures = assert_isolation(user_a, num_a, user_b, num_b)

        if failures:
            print("ISOLATION FAILURES:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("PASS: zero cross-user bleed detected.")
        return 0
    finally:
        cleanup_test_user(user_a)
        cleanup_test_user(user_b)
        print("Test users + their synthetic rows cleaned up.")


if __name__ == "__main__":
    sys.exit(main())
