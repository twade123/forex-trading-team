import sys, sqlite3
sys.path.insert(0, '<repo_root>/Source')
from db_pool import get_trading_forex

conn = get_trading_forex()

# What DB file is this?
db_path = conn.execute("PRAGMA database_list").fetchall()
print(f"Database path: {db_path}")

# Try a direct insert and catch the actual error
try:
    conn.execute("BEGIN")
    conn.execute("""
        INSERT INTO live_trades (id, pair, direction, entry_price, entry_time, status, source, oanda_trade_id, user_id)
        VALUES ('test_2020', 'EUR_JPY', 'long', 162.5, '2026-03-25T01:04:44', 'closed', 'manual', '2020', 2)
    """)
    conn.commit()
    print("INSERT succeeded")
    # Check it's there
    row = conn.execute("SELECT id, pair FROM live_trades WHERE id = 'test_2020'").fetchone()
    print(f"Verified: {row}")
    # Clean up
    conn.execute("DELETE FROM live_trades WHERE id = 'test_2020'")
    conn.commit()
    print("Cleaned up test row")
except Exception as e:
    print(f"INSERT failed: {e}")
    conn.rollback()

# Check if there's maybe a UNIQUE constraint on oanda_trade_id
constraints = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'live_trades'").fetchone()
print(f"\nTable DDL: {constraints[0][:500]}")

# Check existing row with oanda_trade_id 1976 (the most recent one we have)
existing = conn.execute("SELECT id, oanda_trade_id, pair FROM live_trades WHERE oanda_trade_id = '1976'").fetchone()
print(f"\nExisting oanda 1976: {existing}")

# Check if 2020 exists as oanda_trade_id
existing2 = conn.execute("SELECT id, oanda_trade_id, pair FROM live_trades WHERE oanda_trade_id = '2020' OR id = '2020'").fetchone()
print(f"Existing 2020: {existing2}")

conn.close()
