import sys, sqlite3, os, json
from datetime import datetime, timedelta
sys.path.insert(0, '<repo_root>/Source')
from db_pool import get_trading_forex

conn = get_trading_forex()
conn.row_factory = sqlite3.Row

# All live_trades sorted by entry_time DESC - looking for March 24+
print("=" * 70)
print("ALL LIVE_TRADES (most recent 20)")
print("=" * 70)
trades = conn.execute("""
    SELECT id, pair, direction, outcome, pnl_pips, entry_time, exit_time, status, source, oanda_trade_id
    FROM live_trades
    ORDER BY entry_time DESC
    LIMIT 20
""").fetchall()
for t in trades:
    print(f"  {(t['entry_time'] or '?')[:19]} | {t['pair']:8s} {t['direction']:5s} | "
          f"status={t['status']:6s} | outcome={t['outcome'] or 'None':4s} | "
          f"pnl={t['pnl_pips'] or 0:+.1f}p | oanda={t['oanda_trade_id'] or 'None'} | src={t['source']}")

# Check for any trades after March 20
print("\n" + "=" * 70)
print("TRADES AFTER MARCH 20")
print("=" * 70)
after = conn.execute("""
    SELECT id, pair, direction, outcome, pnl_pips, entry_time, status, source, oanda_trade_id
    FROM live_trades
    WHERE entry_time > '2026-03-20'
    ORDER BY entry_time DESC
""").fetchall()
print(f"Found: {len(after)}")
for t in after:
    print(f"  {(t['entry_time'] or '?')[:19]} | {t['pair']:8s} | status={t['status']} | oanda={t['oanda_trade_id']}")

# Check OANDA for recent closed trades
print("\n" + "=" * 70)
print("OANDA RECENT CLOSED TRADES")
print("=" * 70)
try:
    from broker_credentials import BrokerCredentials
    bc = BrokerCredentials()
    creds = bc.get_connection(2, "oanda")
    import requests
    headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
    # Get trades closed in last 7 days
    since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00.000000000Z")
    resp = requests.get(
        f"{creds['base_url']}/v3/accounts/{creds['account_id']}/trades",
        headers=headers,
        params={"state": "CLOSED", "count": 50},
        timeout=15
    )
    if resp.status_code == 200:
        oanda_trades = resp.json().get("trades", [])
        print(f"OANDA closed trades (last 50): {len(oanda_trades)}")
        for ot in oanda_trades[:15]:
            oid = ot.get("id", "?")
            inst = ot.get("instrument", "?")
            close_time = ot.get("closeTime", "?")[:19]
            open_time = ot.get("openTime", "?")[:19]
            pl = ot.get("realizedPL", "0")
            print(f"  OANDA #{oid} {inst:8s} | opened={open_time} | closed={close_time} | PL=${pl}")
    else:
        print(f"OANDA API error: {resp.status_code} {resp.text[:200]}")
except Exception as e:
    print(f"OANDA check failed: {e}")

# Also check manual_trades table
print("\n" + "=" * 70)
print("RECENT MANUAL_TRADES TABLE")
print("=" * 70)
try:
    manuals = conn.execute("""
        SELECT id, pair, direction, entry_price, trade_id, created_at
        FROM manual_trades
        ORDER BY created_at DESC
        LIMIT 10
    """).fetchall()
    print(f"Found: {len(manuals)}")
    for m in manuals:
        print(f"  {(m['created_at'] or '?')[:19]} | {m['pair']:8s} {m['direction']:5s} | trade_id={m['trade_id']}")
except Exception as e:
    print(f"manual_trades check: {e}")

# Check trade_decisions for recent entries
print("\n" + "=" * 70)
print("RECENT TRADE_DECISIONS")
print("=" * 70)
decisions = conn.execute("""
    SELECT id, pair, final_action, validator_verdict, timestamp
    FROM trade_decisions
    WHERE timestamp > '2026-03-23'
    ORDER BY timestamp DESC
    LIMIT 10
""").fetchall()
print(f"Decisions since March 23: {len(decisions)}")
for d in decisions:
    print(f"  {(d['timestamp'] or '?')[:19]} | {d['pair']:8s} | action={d['final_action']} | verdict={d['validator_verdict']}")

conn.close()
