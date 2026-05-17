"""Backfill live_trades from manual_trades + OANDA for trades that failed the mirror INSERT."""
import sys, sqlite3, os, json, requests
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '<repo_root>/Source')
from db_pool import get_trading_forex
from broker_credentials import BrokerCredentials

conn = get_trading_forex()
conn.row_factory = sqlite3.Row

# Get all manual_trades not yet in live_trades
existing_ids = set()
for row in conn.execute("SELECT id, oanda_trade_id FROM live_trades").fetchall():
    existing_ids.add(str(row['id']))
    if row['oanda_trade_id']:
        existing_ids.add(str(row['oanda_trade_id']))

manual = conn.execute("""
    SELECT id, pair, direction, entry_price, trade_id, created_at,
           market_picture, market_story, sniper_scores, indicators, user_id
    FROM manual_trades
    ORDER BY created_at DESC
""").fetchall()

missing = []
for m in manual:
    tid = str(m['trade_id'])
    if tid not in existing_ids:
        missing.append(m)

print(f"manual_trades total: {len(manual)}, already in live_trades: {len(manual) - len(missing)}, missing: {len(missing)}")

# Get OANDA closed trades for enrichment
bc = BrokerCredentials()
creds = bc.get_connection(2, "oanda")
headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
resp = requests.get(
    f"{creds['base_url']}/v3/accounts/{creds['account_id']}/trades",
    headers=headers,
    params={"state": "CLOSED", "count": 500},
    timeout=15
)
oanda_by_id = {}
if resp.status_code == 200:
    for ot in resp.json().get("trades", []):
        oanda_by_id[ot["id"]] = ot
    print(f"OANDA closed trades fetched: {len(oanda_by_id)}")

inserted = 0
for m in missing:
    tid = str(m['trade_id'])
    ot = oanda_by_id.get(tid, {})

    # Parse market data if available
    mp = {}
    ms = {}
    indicators = {}
    try:
        mp = json.loads(m['market_picture']) if m['market_picture'] else {}
    except: pass
    try:
        ms = json.loads(m['market_story']) if m['market_story'] else {}
    except: pass
    try:
        indicators = json.loads(m['indicators']) if m['indicators'] else {}
    except: pass

    ema_data = mp.get('ema', {})

    # Determine outcome from OANDA
    pl = float(ot.get('realizedPL', 0))
    outcome = None
    pnl_pips = None
    pnl_usd = None
    exit_price = None
    exit_time = None
    status = 'open'

    if ot:
        outcome = 'win' if pl > 0 else 'loss'
        pnl_usd = pl
        exit_time = ot.get('closeTime', '')
        status = 'closed'
        # Calculate pnl_pips
        avg_close = float(ot.get('averageClosePrice', 0))
        open_price = float(ot.get('price', m['entry_price'] or 0))
        inst = m['pair']
        pip_size = 0.01 if 'JPY' in inst else 0.0001
        units = float(ot.get('initialUnits', 0))
        if units > 0:  # long
            pnl_pips = (avg_close - open_price) / pip_size
        else:  # short
            pnl_pips = (open_price - avg_close) / pip_size
        exit_price = avg_close

    # Find closest trade_decision for validator data
    dec_id = None
    val_verdict = None
    val_conf = None
    try:
        dec = conn.execute("""
            SELECT id, validator_verdict, validator_confidence
            FROM trade_decisions
            WHERE pair = ? AND abs(julianday(timestamp) - julianday(?)) < 0.021
            ORDER BY abs(julianday(timestamp) - julianday(?))
            LIMIT 1
        """, (m['pair'], m['created_at'], m['created_at'])).fetchone()
        if dec:
            dec_id = dec['id']
            val_verdict = dec['validator_verdict']
            val_conf = dec['validator_confidence']
    except: pass

    entry_type = ms.get('entry_type', 'manual')
    setup = indicators.get('setup', '') or 'unknown'

    try:
        conn.execute("""
            INSERT OR IGNORE INTO live_trades (
                id, source, oanda_trade_id, pair, timeframe, setup, base_setup,
                direction, entry_time, entry_price, sl_price, tp_price,
                status, regime, user_id,
                exit_price, exit_time, outcome, pnl_pips, pnl_usd,
                decision_id, cycle_id, entry_type, validator_verdict, validator_confidence
            ) VALUES (
                ?, 'manual', ?, ?, 'M15', ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
        """, (
            tid, tid, m['pair'], setup, setup,
            m['direction'], m['created_at'], m['entry_price'], None, None,
            status, 'unknown', m['user_id'],
            exit_price, exit_time, outcome, pnl_pips, pnl_usd,
            dec_id, dec_id, entry_type, val_verdict, val_conf,
        ))
        inserted += 1
        pips_str = f"{pnl_pips:+.1f}p" if pnl_pips else "open"
        print(f"  + {tid} {m['pair']:8s} {m['direction']:5s} | {m['created_at'][:19]} | {outcome or 'open'} {pips_str}")
    except Exception as e:
        print(f"  FAIL {tid} {m['pair']}: {e}")

conn.commit()
print(f"\nInserted {inserted} missing trades into live_trades")

# Final count
total = conn.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0]
with_outcome = conn.execute("SELECT COUNT(*) FROM live_trades WHERE outcome IS NOT NULL AND outcome != ''").fetchone()[0]
print(f"live_trades now: {total} total, {with_outcome} with outcomes")
conn.close()
