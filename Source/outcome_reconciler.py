"""
outcome_reconciler.py
---------------------
Reconciles OANDA closed trades against trade_decisions (and scout_findings)
in v2/trading_forex.db, filling in outcome / outcome_pips / live_trade_id where
they are currently NULL.

Usage:
    python3 outcome_reconciler.py            # live run
    python3 outcome_reconciler.py --dry-run  # preview only, no DB writes
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from db_pool import get_trading_forex

# Flight recorder for TRADE_CLOSE events
try:
    from flight_recorder import flight, FlightStage
    _has_flight = True
except ImportError:
    _has_flight = False

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
sys.path.insert(0, "~/jarvis/Forex Trading Team/Source")

try:
    from broker_credentials import BrokerCredentials
    _creds = BrokerCredentials().get_connection(2, "oanda")
except Exception as _e:
    _creds = {"configured": False, "error": str(_e)}


def _get_creds() -> Dict[str, Any]:
    if not _creds.get("configured"):
        raise RuntimeError(
            f"OANDA credentials not available: {_creds.get('error', 'not configured')}"
        )
    return _creds


# ---------------------------------------------------------------------------
# Instrument normalisation helpers
# ---------------------------------------------------------------------------

def _oanda_to_slash(instrument: str) -> str:
    """USD_CAD → USD/CAD"""
    return instrument.replace("_", "/")


def _slash_to_oanda(pair: str) -> str:
    """USD/CAD → USD_CAD"""
    return pair.replace("/", "_")


def _normalise(instrument: str) -> str:
    """Return a canonical form (slash-separated, upper-case) for matching."""
    return instrument.replace("_", "/").upper()


# ---------------------------------------------------------------------------
# OANDA trade fetcher
# ---------------------------------------------------------------------------

def fetch_closed_trades(base_url: str, account_id: str, api_key: str) -> List[Dict]:
    """Paginate through all CLOSED trades from OANDA (count=500 per page)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{base_url}/v3/accounts/{account_id}/trades"
    params: Dict[str, Any] = {"state": "CLOSED", "count": 500}

    all_trades: List[Dict] = []
    page = 0

    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        trades = data.get("trades", [])
        all_trades.extend(trades)
        page += 1

        # OANDA pagination: if we got a full page there may be more
        # Use the last trade id as the 'beforeID' cursor
        if len(trades) < 500:
            break
        last_id = trades[-1]["id"]
        params["beforeID"] = last_id

    print(f"  Fetched {len(all_trades)} closed trades from OANDA ({page} page(s))")
    return all_trades


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _parse_dt(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (with or without fractional seconds) to UTC datetime."""
    if not ts:
        return None
    # strip trailing Z and fractional seconds beyond microseconds
    ts = ts.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _within_5min(t1: Optional[datetime], t2: Optional[datetime]) -> bool:
    if t1 is None or t2 is None:
        return False
    return abs((t1 - t2).total_seconds()) <= 300  # 5 minutes


def _calc_pips(instrument: str, entry: float, exit_: float, direction: str) -> float:
    """Very rough pip calc — 4-decimal pairs = 0.0001, JPY pairs = 0.01."""
    pip_size = 0.01 if "JPY" in instrument else 0.0001
    if direction and direction.lower() in ("sell", "short"):
        return round((entry - exit_) / pip_size, 1)
    return round((exit_ - entry) / pip_size, 1)


# ---------------------------------------------------------------------------
# Partial-run cleanup helpers
# ---------------------------------------------------------------------------

def _cleanup_partial_audit(auditor, trade_id: str):
    """Remove any partial audit data for a trade before re-running.

    Called when a trade has learning_status='pending', meaning a previous run
    was interrupted.  INSERT OR REPLACE handles trade_audits automatically,
    but we also purge stale flight_log entries so they don't pile up.
    """
    try:
        aconn = auditor._conn()
        try:
            aconn.execute(
                "DELETE FROM trade_audits WHERE trade_id = ?", (trade_id,))
            aconn.commit()
        finally:
            aconn.close()
    except Exception:
        pass  # table might not exist yet on first run

    # Clean learning-stage flight_log entries for this trade
    try:
        import sqlite3 as _sql
        _fr_path = os.path.join(os.path.dirname(__file__), "flight_recorder.db")
        if os.path.exists(_fr_path):
            _frconn = _sql.connect(_fr_path, timeout=5)
            _frconn.execute(
                """DELETE FROM flight_log
                   WHERE trade_id = ? AND stage IN (
                       'trade_audit', 'learning_audit', 'learning_scout',
                       'learning_validator', 'learning_guardian',
                       'learning_knowledge', 'learning_drift',
                       'learning_dashboard', 'learning_complete'
                   )""",
                (trade_id,),
            )
            _frconn.commit()
            _frconn.close()
    except Exception:
        pass  # non-critical


# ---------------------------------------------------------------------------
# Main reconcile logic
# ---------------------------------------------------------------------------

def reconcile(dry_run: bool = False) -> None:
    creds = _get_creds()
    base_url = creds["base_url"].rstrip("/")
    account_id = creds["account_id"]
    api_key = creds["api_key"]

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Fetching closed trades from OANDA …")
    oanda_trades = fetch_closed_trades(base_url, account_id, api_key)

    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # 0. Ensure learning_status column exists (safe idempotent migration)
    # ------------------------------------------------------------------
    try:
        conn.execute("ALTER TABLE live_trades ADD COLUMN learning_status TEXT")
        print("  ✅ Added learning_status column to live_trades")
    except Exception:
        pass  # column already exists

    # ------------------------------------------------------------------
    # 1. Reconcile trade_decisions
    # ------------------------------------------------------------------
    print("\n── trade_decisions reconciliation ──")

    matched_d = 0
    unmatched_d = 0
    wins_d = 0
    losses_d = 0

    # Begin transaction for trade_decisions updates
    if not dry_run:
        conn.execute("BEGIN")

    # First pass: match by live_trade_id directly (most reliable)
    id_pending = conn.execute(
        """
        SELECT id, pair, direction, live_trade_id
        FROM trade_decisions
        WHERE outcome IS NULL AND live_trade_id IS NOT NULL
        """
    ).fetchall()
    oanda_by_id = {t['id']: t for t in oanda_trades}
    for row in id_pending:
        tid = str(row['live_trade_id'])
        if tid not in oanda_by_id:
            unmatched_d += 1
            continue
        t = oanda_by_id[tid]
        real_pl = float(t.get('realizedPL', 0))
        entry = float(t.get('price', 0))
        exit_avg = float(t.get('averageClosePrice', t.get('closePrice', 0)))
        direction = row['direction'] or ('buy' if float(t.get('initialUnits', 1)) > 0 else 'sell')
        pips = _calc_pips(row['pair'], entry, exit_avg, direction)
        outcome = 'win' if real_pl > 0 else ('loss' if real_pl < 0 else 'breakeven')
        if outcome == 'win': wins_d += 1
        elif outcome == 'loss': losses_d += 1
        matched_d += 1
        if not dry_run:
            conn.execute(
                'UPDATE trade_decisions SET outcome=?, outcome_pips=? WHERE id=?',
                (outcome, round(pips, 1), row['id'])
            )

    # Second pass: time-based match for actual trades without a recorded trade_id
    pending_decisions = conn.execute(
        """
        SELECT id, pair, timestamp, direction
        FROM trade_decisions
        WHERE outcome IS NULL AND live_trade_id IS NULL
          AND final_action = 'trade'
        ORDER BY timestamp DESC
        """
    ).fetchall()

    print(f"  Found {len(pending_decisions)} trade_decisions with outcome IS NULL")

    for dec in pending_decisions:
        dec_pair = _normalise(dec["pair"])
        dec_dt = _parse_dt(dec["timestamp"])

        best_trade = None
        for t in oanda_trades:
            if _normalise(t.get("instrument", "")) != dec_pair:
                continue
            trade_open_dt = _parse_dt(t.get("openTime", ""))
            if _within_5min(dec_dt, trade_open_dt):
                best_trade = t
                break

        if best_trade is None:
            unmatched_d += 1
            continue

        # Compute outcome
        entry = float(best_trade.get("price", 0))
        exit_avg = float(best_trade.get("averageClosePrice", best_trade.get("closePrice", 0)))
        real_pl = float(best_trade.get("realizedPL", 0))
        direction = dec["direction"] or ("buy" if real_pl >= 0 else "sell")
        pips = _calc_pips(dec_pair, entry, exit_avg, direction)
        outcome = "win" if real_pl > 0 else ("loss" if real_pl < 0 else "breakeven")
        trade_id = best_trade.get("id", "")

        if outcome == "win":
            wins_d += 1
        elif outcome == "loss":
            losses_d += 1

        matched_d += 1

        if not dry_run:
            conn.execute(
                """
                UPDATE trade_decisions
                SET outcome = ?, outcome_pips = ?, live_trade_id = ?
                WHERE id = ?
                """,
                (outcome, pips, trade_id, dec["id"]),
            )
        else:
            print(
                f"  [DRY] Would update decision {dec['id']}: "
                f"{outcome} {pips:+.1f} pips (trade {trade_id})"
            )

    if not dry_run:
        conn.execute("COMMIT")

    print(f"  trade_decisions — matched: {matched_d}, unmatched: {unmatched_d}")
    print(f"  Win/Loss split: {wins_d}W / {losses_d}L")

    # ------------------------------------------------------------------
    # 2. Backfill scout_findings
    # ------------------------------------------------------------------
    print("\n── scout_findings backfill ──")

    matched_s = 0
    unmatched_s = 0
    wins_s = 0
    losses_s = 0

    # Begin transaction for scout_findings updates
    if not dry_run:
        conn.execute("BEGIN")

    pending_scouts = conn.execute(
        """
        SELECT id, pair, timestamp, trade_direction, trade_entry_price, trade_id
        FROM scout_findings
        WHERE outcome IS NULL AND trade_id IS NOT NULL
        ORDER BY timestamp DESC
        """
    ).fetchall()

    print(f"  Found {len(pending_scouts)} scout_findings with outcome IS NULL and trade_id set")

    for scout in pending_scouts:
        scout_trade_id = str(scout["trade_id"])
        # Try direct ID match first
        direct = next(
            (t for t in oanda_trades if str(t.get("id", "")) == scout_trade_id), None
        )

        if direct is None:
            # Fall back to instrument+time match
            scout_pair = _normalise(scout["pair"])
            scout_dt = _parse_dt(scout["timestamp"])
            direct = next(
                (
                    t
                    for t in oanda_trades
                    if _normalise(t.get("instrument", "")) == scout_pair
                    and _within_5min(scout_dt, _parse_dt(t.get("openTime", "")))
                ),
                None,
            )

        if direct is None:
            unmatched_s += 1
            continue

        real_pl = float(direct.get("realizedPL", 0))
        entry = float(direct.get("price", scout["trade_entry_price"] or 0))
        exit_avg = float(direct.get("averageClosePrice", direct.get("closePrice", 0)))
        direction = scout["trade_direction"] or ("buy" if real_pl >= 0 else "sell")
        pips = _calc_pips(_normalise(scout["pair"]), entry, exit_avg, direction)
        outcome = "win" if real_pl > 0 else ("loss" if real_pl < 0 else "breakeven")

        if outcome == "win":
            wins_s += 1
        elif outcome == "loss":
            losses_s += 1

        matched_s += 1

        if not dry_run:
            conn.execute(
                """
                UPDATE scout_findings
                SET outcome = ?, pips_result = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (outcome, pips, scout["id"]),
            )
        else:
            print(
                f"  [DRY] Would update scout {scout['id']}: "
                f"{outcome} {pips:+.1f} pips"
            )

    if not dry_run:
        conn.execute("COMMIT")

    print(f"  scout_findings   — matched: {matched_s}, unmatched: {unmatched_s}")
    print(f"  Win/Loss split: {wins_s}W / {losses_s}L")

    # ------------------------------------------------------------------
    # 3. Reconcile live_trades (actual executed trades)
    # ------------------------------------------------------------------
    print("\n── live_trades reconciliation ──")

    matched_lt = 0
    unmatched_lt = 0
    wins_lt = 0
    losses_lt = 0
    audited_trades = []

    if not dry_run:
        conn.execute("BEGIN")

    # Pass A: match by oanda_trade_id (direct link)
    # Pull scout context columns so the auditor can grade signal accuracy
    pending_lt = conn.execute(
        """
        SELECT id, pair, direction, entry_price, oanda_trade_id, entry_time,
               status,
               setup, setup_code, entry_type,
               fan_state, fan_direction, fan_ordered, e100_role,
               bb_expanding, momentum_state, rsi, stoch_k,
               story_score, story_entry_type,
               validator_verdict, validator_confidence,
               market_story, market_picture,
               pattern_fingerprint, classified_setup
        FROM live_trades
        WHERE (
            (status = 'closed' AND (pnl_pips IS NULL OR exit_time IS NULL))
            OR
            (status = 'open' AND oanda_trade_id IS NOT NULL)
        )
        ORDER BY entry_time DESC
        """
    ).fetchall()

    print(f"  Found {len(pending_lt)} live_trades needing reconciliation")

    for lt in pending_lt:
        oanda_tid = str(lt["oanda_trade_id"]) if lt["oanda_trade_id"] else None
        matched_trade = None

        # Try direct ID match first
        if oanda_tid and oanda_tid in oanda_by_id:
            matched_trade = oanda_by_id[oanda_tid]
        else:
            # Fall back to instrument + time match
            lt_pair = _normalise(lt["pair"])
            lt_dt = _parse_dt(lt["entry_time"])
            for t in oanda_trades:
                if _normalise(t.get("instrument", "")) != lt_pair:
                    continue
                trade_open_dt = _parse_dt(t.get("openTime", ""))
                if _within_5min(lt_dt, trade_open_dt):
                    matched_trade = t
                    break

        if matched_trade is None:
            unmatched_lt += 1
            continue

        real_pl = float(matched_trade.get("realizedPL", 0))
        entry = float(matched_trade.get("price", lt["entry_price"] or 0))
        exit_avg = float(matched_trade.get("averageClosePrice", matched_trade.get("closePrice", 0)))
        close_time = matched_trade.get("closeTime", "")
        direction = lt["direction"] or ("buy" if float(matched_trade.get("initialUnits", 1)) > 0 else "sell")
        pips = _calc_pips(lt["pair"], entry, exit_avg, direction)
        outcome = "win" if real_pl > 0 else ("loss" if real_pl < 0 else "breakeven")
        pnl_usd = round(real_pl, 2)

        if outcome == "win":
            wins_lt += 1
        elif outcome == "loss":
            losses_lt += 1
        matched_lt += 1

        if not dry_run:
            # Also set status='closed' + result for trades still marked 'open'
            conn.execute(
                """
                UPDATE live_trades
                SET status = 'closed',
                    pnl_pips = ?, pnl_usd = ?, outcome = ?, outcome_pips = ?,
                    outcome_usd = ?, exit_price = ?, exit_time = ?,
                    pips = ?, realized_pl = ?, result = ?,
                    oanda_trade_id = ?,
                    learning_status = 'pending'
                WHERE id = ?
                """,
                (round(pips, 1), pnl_usd, outcome, round(pips, 1),
                 pnl_usd, exit_avg, close_time,
                 round(pips, 1), pnl_usd, outcome,
                 matched_trade.get("id", oanda_tid),
                 lt["id"]),
            )
            # Collect data for audit pipeline — include scout context so auditor
            # can grade signal accuracy (without this, scout_signal_accuracy=0%)
            audited_trades.append({
                "trade_id": str(lt["id"]),
                "pair": lt["pair"],
                "direction": direction,
                "setup_name": lt["setup"] or lt["setup_code"] or "manual",
                "entry_type": lt["entry_type"] or ("snipe" if lt["setup"] else "manual"),
                "outcome": outcome,
                "pnl_pips": round(pips, 1),
                "pnl_usd": pnl_usd,
                "entry_price": entry,
                "exit_price": exit_avg,
                "entry_time": lt["entry_time"],
                "exit_time": close_time,
                # Scout context for signal accuracy grading
                "fan_state": lt["fan_state"],
                "fan_direction": lt["fan_direction"],
                "fan_ordered": lt["fan_ordered"],
                "e100_role": lt["e100_role"],
                "bb_expanding": lt["bb_expanding"],
                "momentum_state": lt["momentum_state"],
                "rsi": lt["rsi"],
                "stoch_k": lt["stoch_k"],
                "story_score": lt["story_score"],
                "story_entry_type": lt["story_entry_type"],
                "validator_verdict": lt["validator_verdict"],
                "validator_confidence": lt["validator_confidence"],
                "market_story": lt["market_story"],
                "market_picture": lt["market_picture"],
                "pattern_fingerprint": lt["pattern_fingerprint"],
                "classified_setup": lt["classified_setup"],
                "user_id": lt["user_id"] if "user_id" in lt.keys() else None,
            })

            # Log TRADE_CLOSE to flight recorder so check_learning_flow() can track
            if _has_flight:
                try:
                    flight.record(
                        stage=FlightStage.TRADE_CLOSE,
                        trade_id=str(lt["id"]),
                        pair=lt["pair"],
                        data={
                            "outcome": outcome,
                            "pnl_pips": round(pips, 1),
                            "pnl_usd": pnl_usd,
                            "entry_price": entry,
                            "exit_price": exit_avg,
                            "entry_time": lt["entry_time"],
                            "exit_time": close_time,
                            "setup": lt["setup"] or "manual",
                        },
                        status="ok",
                        note=f"{lt['pair']} {outcome} {pips:+.1f}p ${pnl_usd:+.2f}",
                    )
                except Exception as _fl_err:
                    print(f"  flight_recorder TRADE_CLOSE error: {_fl_err}")
        else:
            print(
                f"  [DRY] Would update live_trade {lt['id']} ({lt['pair']}): "
                f"{outcome} {pips:+.1f} pips  ${real_pl:+.2f}"
            )

    if not dry_run:
        conn.execute("COMMIT")

    print(f"  live_trades     — matched: {matched_lt}, unmatched: {unmatched_lt}")
    print(f"  Win/Loss split: {wins_lt}W / {losses_lt}L")

    # ------------------------------------------------------------------
    # 4. Recover any interrupted trades from previous runs
    # ------------------------------------------------------------------
    if not dry_run:
        interrupted = conn.execute(
            """
            SELECT id, pair, direction, entry_price, oanda_trade_id, entry_time,
                   exit_time, exit_price, pnl_pips, pnl_usd, outcome,
                   setup, setup_code, entry_type,
                   fan_state, fan_direction, fan_ordered, e100_role,
                   bb_expanding, momentum_state, rsi, stoch_k,
                   story_score, story_entry_type,
                   validator_verdict, validator_confidence,
                   market_story, market_picture,
                   pattern_fingerprint, classified_setup,
                   user_id
            FROM live_trades
            WHERE learning_status = 'pending'
            """
        ).fetchall()

        if interrupted:
            print(f"\n── Recovering {len(interrupted)} interrupted trades from previous run ──")
            for row in interrupted:
                _direction = row["direction"] or "buy"
                td = {
                    "trade_id": str(row["id"]),
                    "pair": row["pair"],
                    "direction": _direction,
                    "setup_name": row["setup"] or row["setup_code"] or "manual",
                    "entry_type": row["entry_type"] or ("snipe" if row["setup"] else "manual"),
                    "outcome": row["outcome"] or ("win" if (row["pnl_usd"] or 0) > 0 else "loss"),
                    "pnl_pips": row["pnl_pips"] or 0,
                    "pnl_usd": row["pnl_usd"] or 0,
                    "entry_price": row["entry_price"] or 0,
                    "exit_price": row["exit_price"] or 0,
                    "entry_time": row["entry_time"] or "",
                    "exit_time": row["exit_time"] or "",
                    "fan_state": row["fan_state"],
                    "fan_direction": row["fan_direction"],
                    "fan_ordered": row["fan_ordered"],
                    "e100_role": row["e100_role"],
                    "bb_expanding": row["bb_expanding"],
                    "momentum_state": row["momentum_state"],
                    "rsi": row["rsi"],
                    "stoch_k": row["stoch_k"],
                    "story_score": row["story_score"],
                    "story_entry_type": row["story_entry_type"],
                    "validator_verdict": row["validator_verdict"] or "",
                    "validator_confidence": row["validator_confidence"],
                    "market_story": row["market_story"],
                    "market_picture": row["market_picture"],
                    "pattern_fingerprint": row["pattern_fingerprint"],
                    "classified_setup": row["classified_setup"],
                    "user_id": row["user_id"],
                }
                audited_trades.append(td)

    # ------------------------------------------------------------------
    # 5. Trigger audit + learning pipeline (new + recovered trades)
    # ------------------------------------------------------------------
    if audited_trades and not dry_run:
        print(f"\n── Learning pipeline: processing {len(audited_trades)} trades ──")
        _audit_ok = 0
        _audit_errors = 0
        try:
            from trade_auditor import TradeAuditor
            from learning_integrator import LearningIntegrator
            auditor = TradeAuditor()
            integrator = LearningIntegrator()

            for td in audited_trades:
                trade_id = td["trade_id"]
                try:
                    # Clean up any partial audit from a previous interrupted run
                    # (INSERT OR REPLACE handles trade_audits, but explicitly
                    # delete first so we don't accumulate stale flight_log entries)
                    _cleanup_partial_audit(auditor, trade_id)

                    audit_result = auditor.audit_trade(
                        cycle_id=f"reconcile_{trade_id}",
                        trade_id=trade_id,
                        pair=td["pair"],
                        direction=td["direction"],
                        entry_price=td["entry_price"],
                        exit_price=td["exit_price"],
                        stop_loss=0,
                        take_profit=0,
                        pnl_pips=td["pnl_pips"],
                        pnl_usd=td["pnl_usd"],
                        setup_name=td["setup_name"],
                        entry_type=td["entry_type"],
                        entry_time=td.get("entry_time", ""),
                        close_time=td.get("exit_time", ""),
                        outcome=td["outcome"],
                        validator_verdict=td.get("validator_verdict", ""),
                        scout_context={
                            "fan_state": td.get("fan_state"),
                            "fan_direction": td.get("fan_direction"),
                            "fan_ordered": td.get("fan_ordered"),
                            "e100_role": td.get("e100_role"),
                            "bb_expanding": td.get("bb_expanding"),
                            "momentum_state": td.get("momentum_state"),
                            "rsi": td.get("rsi"),
                            "stoch_k": td.get("stoch_k"),
                            "story_score": td.get("story_score"),
                            "story_entry_type": td.get("story_entry_type"),
                            "entry_type": td.get("entry_type"),
                            "market_story": td.get("market_story"),
                            "market_picture": td.get("market_picture"),
                            "pattern_fingerprint": td.get("pattern_fingerprint"),
                        },
                        user_id=td.get("user_id"),
                    )
                    if audit_result:
                        learnings = integrator.process_trade_audit(audit_result)
                        # Mark this trade's learning pipeline as complete
                        conn.execute(
                            "UPDATE live_trades SET learning_status = 'complete' WHERE id = ?",
                            (trade_id,),
                        )
                        _audit_ok += 1
                        print(f"    ✅ {td['pair']} {td['outcome']} {td['pnl_pips']:+.1f}p → {len(learnings)} learnings")
                    else:
                        print(f"    ⚠️  {td['pair']} — audit returned None (stays pending)")
                except Exception as ae:
                    _audit_errors += 1
                    print(f"    ❌ {td['pair']} — audit error: {ae} (stays pending for retry)")

            if _audit_errors:
                print(f"  ⚠️  {_audit_errors} failed (will retry next run), {_audit_ok} completed")
            else:
                print(f"  ✅  All {len(audited_trades)} trades audited + learning extracted")
        except ImportError as ie:
            print(f"  ⚠️  Learning pipeline not available: {ie}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n══════════════════════════════════════")
    print(f"  TOTAL matched  : {matched_d + matched_s + matched_lt}")
    print(f"  TOTAL unmatched: {unmatched_d + unmatched_s + unmatched_lt}")
    total_w = wins_d + wins_s + wins_lt
    total_l = losses_d + losses_s + losses_lt
    total   = total_w + total_l
    wr = (total_w / total * 100) if total > 0 else 0.0
    print(f"  Overall W/L    : {total_w}W / {total_l}L  ({wr:.1f}% WR)")
    print(f"{'  *** DRY RUN — no changes written ***' if dry_run else '  Changes committed to DB.'}")
    print("══════════════════════════════════════\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconcile OANDA closed trades → DB outcomes")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no DB writes")
    args = parser.parse_args()
    reconcile(dry_run=args.dry_run)
