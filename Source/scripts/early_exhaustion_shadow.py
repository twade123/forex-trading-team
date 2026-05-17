"""early_exhaustion_shadow — dry-run daemon for the failed_rally rewrite.

Polls open trades every POLL_SECS, evaluates each through the
early_exhaustion_evaluator, and LOGS what the rule would do. Takes NO live
action — guardian code is untouched.

Per-trade state is kept across polls so we only LOG a "would-fire" once per
trade (subsequent polls of the same trade are suppressed).

Stop with Ctrl-C or send SIGTERM.

Output:
  - Console stream of polls + would-fire events
  - JSON log appended at scripts/early_exhaustion_shadow_<date>.jsonl
"""
from __future__ import annotations
import sys
import os
import json
import time
import signal
import sqlite3
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)

from oanda_client import OandaClient
from early_exhaustion_evaluator import evaluate_trade
import tuning_config as tc

DB = '~/Jarvis/Database/v2/trading_forex.db'
POLL_SECS = 60
OUT_FILE = os.path.join(HERE,
                        f'early_exhaustion_shadow_{datetime.utcnow().strftime("%Y%m%d")}.jsonl')

_STOP = False


def _sig_handler(signum, frame):
    global _STOP
    _STOP = True


signal.signal(signal.SIGINT, _sig_handler)
signal.signal(signal.SIGTERM, _sig_handler)


def to_et(dt):
    return (dt - timedelta(hours=4)).strftime('%m-%d %H:%M ET')


def now_utc():
    return datetime.now(tz=timezone.utc)


def load_open_trades():
    """Pull all open scout/snipe_direct trades. Exclude kronos."""
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, pair, direction, entry_price, entry_time, source, entry_type,
               sl_price, tp_price, pnl_pips
        FROM live_trades
        WHERE status = 'open'
          AND source IN ('scout','snipe_direct')
          AND (entry_type IS NULL OR entry_type NOT LIKE '%kronos%')
          AND source NOT LIKE '%kronos%'
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_event(event: dict):
    """Append JSON line + print key fields."""
    with open(OUT_FILE, 'a') as f:
        f.write(json.dumps(event, default=str) + '\n')
    fire = '🔥 FIRE' if event.get('would_fire') else '·'
    print(f"  {event['ts_et']:>17}  {event['trade_id']:<7}{event['pair']:<10}"
          f"{event['direction']:<5}  MFE={event.get('mfe', 0):>+5.1f}  "
          f"dec_bar={event.get('decision_bar', '-'):>3}  "
          f"p_loser={event.get('p_loser') if event.get('p_loser') is not None else '-':<6}  "
          f"{fire}  {event.get('reason', '')[:70]}")


def main():
    print('=' * 110)
    print('early_exhaustion_shadow — DRY RUN')
    print('=' * 110)
    print(f"  Output  : {OUT_FILE}")
    print(f"  Poll    : every {POLL_SECS}s")
    print(f"  DB      : {DB}")
    print(f"  Tunables (reload from tuning_config on each poll):")
    print(f"    mfe_min_pips        = {tc.get('guardian.early_exhaustion_mfe_min_pips')}")
    print(f"    mfe_max_pips        = {tc.get('guardian.early_exhaustion_mfe_max_pips')}")
    print(f"    arm_window_bars     = {tc.get('guardian.early_exhaustion_arm_window_bars')}")
    print(f"    classifier_threshold= {tc.get('guardian.early_exhaustion_classifier_threshold')}")
    print(f"    lock_pips           = {tc.get('guardian.early_exhaustion_lock_pips')}")
    print()
    print('  Stop with Ctrl-C. Stop file: scripts/early_exhaustion_shadow_STOP')

    oanda = OandaClient()
    fired_trades = set()   # trade_ids we already logged a FIRE for (no repeats)
    poll_count = 0

    while not _STOP:
        # External stop file (so the user can stop the daemon without killing the process)
        if os.path.exists(os.path.join(HERE, 'early_exhaustion_shadow_STOP')):
            print('\n  STOP file detected — exiting.')
            break

        poll_count += 1
        ts = now_utc()
        try:
            open_trades = load_open_trades()
        except Exception as e:
            print(f'  [poll {poll_count}] DB error: {e}')
            time.sleep(POLL_SECS); continue

        if not open_trades:
            if poll_count % 10 == 1:
                print(f"  [{to_et(ts)}] no open trades.")
            time.sleep(POLL_SECS); continue

        print(f"\n  ── poll {poll_count} @ {to_et(ts)}  ({len(open_trades)} open trade(s)) ──")
        for t in open_trades:
            tid = str(t['id'])
            # Re-evaluate even if previously fired (so we see state changes), but
            # only LOG once per fire.
            try:
                entry_time = datetime.fromisoformat(
                    t['entry_time'].replace('Z', '+00:00')
                )
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                # Fetch candles from 30h before entry to now
                candles = oanda.fetch_candles_range(
                    instrument=t['pair'], granularity='M15',
                    from_time=entry_time - timedelta(hours=30),
                    to_time=ts + timedelta(minutes=15), price='M',
                )
                if not candles:
                    continue
                result = evaluate_trade(
                    pair=t['pair'],
                    direction=t['direction'],
                    entry_price=float(t['entry_price']),
                    entry_time_iso=t['entry_time'],
                    m15_candles_since_entry_with_warmup=candles,
                    mfe_min_pips=float(tc.get('guardian.early_exhaustion_mfe_min_pips') or 3.0),
                    mfe_max_pips=float(tc.get('guardian.early_exhaustion_mfe_max_pips') or 10.0),
                    arm_window_bars=int(tc.get('guardian.early_exhaustion_arm_window_bars') or 8),
                    classifier_threshold=float(tc.get('guardian.early_exhaustion_classifier_threshold') or 0.65),
                    lock_pips=float(tc.get('guardian.early_exhaustion_lock_pips') or 0.5),
                )
            except Exception as e:
                print(f'  trade {tid}: eval error: {e}')
                continue

            event = {
                'ts_utc': ts.isoformat(),
                'ts_et': to_et(ts),
                'trade_id': tid,
                'pair': t['pair'],
                'direction': t['direction'],
                'entry_price': t['entry_price'],
                'entry_time': t['entry_time'],
                'source': t['source'],
                'current_pnl_pips': t.get('pnl_pips'),
                **{k: v for k, v in result.items() if k != 'features'},
            }
            if result.get('would_fire'):
                if tid in fired_trades:
                    # Already logged this fire — skip
                    continue
                fired_trades.add(tid)
                event['features'] = result.get('features')
                event['MARK'] = 'FIRST_FIRE'
                log_event(event)
            else:
                # Only log non-fires that are interesting (in-universe, just below threshold)
                if result.get('decision_bar', -1) >= 0 and 3.0 <= result.get('mfe', 0) < 10.0:
                    event['MARK'] = 'NEAR_FIRE'
                    log_event(event)
                elif poll_count % 10 == 0:
                    # Heartbeat every 10 polls for any in-universe trade
                    print(f"  · {tid:<7}{t['pair']:<10}{t['direction']:<5}  "
                          f"MFE={result.get('mfe', 0):+.1f}p  "
                          f"{result.get('reason', '')[:70]}")

        # Sleep until next poll
        for _ in range(POLL_SECS):
            if _STOP:
                break
            time.sleep(1)

    print('\n  Shadow daemon stopped.')
    print(f'  Total fires logged: {len(fired_trades)}')
    print(f'  Log file: {OUT_FILE}')


if __name__ == '__main__':
    main()
