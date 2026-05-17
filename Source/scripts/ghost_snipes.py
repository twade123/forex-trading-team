#!/usr/bin/env python3
"""Ghost-snipe tracking — records every validator-watch trigger (live-fired OR
session-gate-blocked) and replays the would-have-been trade against M15 candles
to determine actual outcome.

Goal: empirical data on whether validator snipes are profitable, including
the ones currently blocked by session gates. If session-gate-blocked snipes
are net winners, the gate is over-blocking; if losers, the gate is correct.

Usage:
    python ghost_snipes.py record   # scan flight_log for new triggers
    python ghost_snipes.py replay   # replay pending outcomes
    python ghost_snipes.py report   # summary statistics
    python ghost_snipes.py all      # record + replay + report (default)
"""
from __future__ import annotations
import sys
import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SRC = Path("<repo_root>/Source")
sys.path.insert(0, str(SRC))

from oanda_client import OandaClient
from optimizer.replay import TradeSnapshot, candle_walk_replay
from tuning_config import get as tc_get

TRADING_DB = "~/Jarvis/Database/v2/trading_forex.db"
FLIGHT_DB = str(SRC / "flight_recorder.db")

# 2026-04-27: 30-day backfill window
RECORD_LOOKBACK_DAYS = 30

# How long after trigger to wait before declaring outcome (M15 candles)
REPLAY_WINDOW_BARS = 16  # 4 hours of M15 = typical watch lifetime

# Default SL/TP if watch has no stored entry zone (use 1.5×ATR / 2.5×ATR)
DEFAULT_SL_ATR_MULT = 1.5
DEFAULT_TP_ATR_MULT = 2.5


def load_snipe_guardian_params() -> dict:
    """Pull current snipe.* + guardian.* tuning params for apples-to-apples
    comparison with the live system. Falls back to defaults for any missing."""
    p = {}
    # SL/TP (snipe.gate.atr_*_mult is the snipe-specific override; gate.atr_*_mult is the kronos default)
    p["gate.sl_atr_mult"] = (tc_get("snipe.gate.sl_atr_mult", None)
                             or tc_get("gate.sl_atr_mult", DEFAULT_SL_ATR_MULT))
    p["gate.tp_atr_mult"] = (tc_get("snipe.gate.tp_atr_mult", None)
                             or tc_get("gate.tp_atr_mult", DEFAULT_TP_ATR_MULT))
    # Profit floor tiers — prefer snipe-specific, fall back to general guardian
    for tier in ("5p", "8p", "12p", "20p"):
        snipe_key = f"snipe.guardian.profit_floor_{tier}"
        gen_key = f"guardian.profit_floor_{tier}"
        v = tc_get(snipe_key, None)
        if v is None:
            v = tc_get(gen_key, None)
        if v is not None:
            p[gen_key] = float(v)
    # Trailing
    p["guardian.trailing_activation_rr"] = float(
        tc_get("snipe.guardian.trailing_activation_rr", None)
        or tc_get("guardian.trailing_activation_rr", 0.15)
    )
    p["guardian.trailing_atr_mult"] = float(
        tc_get("snipe.guardian.trailing_atr_mult", None)
        or tc_get("guardian.trailing_atr_mult", 0.1)
    )
    # SL buffer + ratchet step
    p["guardian.sl_buffer_pips"] = float(
        tc_get("snipe.guardian.sl_buffer_pips", None)
        or tc_get("guardian.sl_buffer_pips", 1.0)
    )
    p["guardian.ratchet_step_pips"] = float(
        tc_get("guardian.ratchet_step_pips", 1.5)
    )
    return p


def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def parse_iso(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    if "." in s:
        base, frac_off = s.split(".", 1)
        for sep in ("+", "-"):
            if sep in frac_off[1:]:
                idx = frac_off.index(sep, 1)
                frac, off = frac_off[:idx], frac_off[idx:]
                break
        else:
            frac, off = frac_off, "+00:00"
        frac = frac[:6].ljust(6, "0")
        s = f"{base}.{frac}{off}"
    return datetime.fromisoformat(s)


def get_watch_levels(conn: sqlite3.Connection, watch_id: int, pair: str
                     ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Pull entry zone midpoint + invalidation_level + tp price-target from
    the watch's stored conditions JSON."""
    row = conn.execute("SELECT conditions FROM watch_suggestions WHERE id=?",
                       (watch_id,)).fetchone()
    if not row or not row[0]:
        return None, None, None
    try:
        conds = json.loads(row[0])
    except Exception:
        return None, None, None

    entry_mid = None
    invalidation = None
    target = None
    for c in conds:
        if not isinstance(c, dict):
            continue
        f = c.get("field"); v = c.get("value")
        if f == "price_zone" and isinstance(v, str) and "-" in v:
            try:
                lo, hi = v.split("-")
                entry_mid = (float(lo) + float(hi)) / 2
            except Exception:
                pass
        elif f == "invalidation_level":
            try:
                invalidation = float(v)
            except Exception:
                pass
        # Some watches store an explicit target via close > X / close < X
        elif f == "close" and c.get("op") in (">", ">=") and not isinstance(v, str):
            try:
                target = float(v)
            except Exception:
                pass
        elif f == "close" and c.get("op") in ("<", "<=") and not isinstance(v, str):
            try:
                target = float(v)
            except Exception:
                pass
    return entry_mid, invalidation, target


def fetch_m15_after(client: OandaClient, pair: str, from_t: datetime,
                    to_t: datetime) -> List[dict]:
    """Pull complete M15 candles in [from_t, to_t]."""
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    if to_t > now:
        to_t = now
    if to_t <= from_t:
        return []
    raw = client.get_candles(instrument=pair, granularity="M15", price="M",
                             from_time=from_t, to_time=to_t)
    out = []
    for c in raw:
        if not c.get("complete", True):
            continue
        m = c["mid"]
        out.append({
            "time": parse_iso(c["time"]),
            "o": float(m["o"]), "h": float(m["h"]),
            "l": float(m["l"]), "c": float(m["c"]),
        })
    return out


def compute_atr_pips(client: OandaClient, pair: str, anchor: datetime,
                     period: int = 14) -> Optional[float]:
    """Recent ATR for sizing SL/TP fallback."""
    from_t = anchor - timedelta(hours=period * 0.25 + 2)  # ~14 M15 bars + buffer
    candles = fetch_m15_after(client, pair, from_t, anchor)
    if len(candles) < period:
        return None
    pip = pip_size(pair)
    trs = []
    for i in range(1, len(candles)):
        h, l = candles[i]["h"], candles[i]["l"]
        pc = candles[i-1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr / pip)
    return sum(trs[-period:]) / period if trs else None


def record_new_triggers():
    """Scan flight_log for SNIPE_TRIGGERED events not yet in ghost_snipes."""
    fconn = sqlite3.connect(FLIGHT_DB)
    fconn.row_factory = sqlite3.Row
    tconn = sqlite3.connect(TRADING_DB)
    tconn.row_factory = sqlite3.Row
    client = OandaClient()

    # Existing ghost_snipes keys
    existing = set()
    for r in tconn.execute(
        "SELECT watch_id, triggered_at_utc FROM ghost_snipes"
    ).fetchall():
        existing.add((r[0], r[1][:19]))

    # All SNIPE_TRIGGERED events from configured lookback window
    triggers = fconn.execute(f"""
        SELECT timestamp, pair, note, data
          FROM flight_log
         WHERE stage='SNIPE_TRIGGERED'
           AND timestamp >= datetime('now','-{RECORD_LOOKBACK_DAYS} days')
         ORDER BY timestamp
    """).fetchall()

    new_count = 0
    skipped_kronos = 0
    for trig in triggers:
        try:
            data = json.loads(trig["data"]) if trig["data"] else {}
        except Exception:
            data = {}
        watch_id = data.get("watch_id")
        if watch_id is None:
            continue
        ts_iso = trig["timestamp"]
        key = (watch_id, ts_iso[:19])
        if key in existing:
            continue

        pair = trig["pair"]
        direction = (data.get("direction") or data.get("watch_direction") or "").lower()
        if direction not in ("buy", "sell"):
            continue

        # 2026-04-27: Filter validator-origin only — kronos snipes have their
        # own guardian (kronos_guardian / kronos.* params) and audit pipeline
        # (kronos_shadow_scores). Lumping them gives meaningless aggregates.
        # Per Tim feedback (memory/feedback_validator_kronos_separate.md).
        st_row = tconn.execute(
            "SELECT suggestion_type FROM watch_suggestions WHERE id=?",
            (watch_id,)
        ).fetchone()
        suggestion_type = st_row[0] if st_row else None
        if suggestion_type not in ("validator_structured", "validator_text"):
            skipped_kronos += 1
            continue

        # Did this trigger result in a live trade or get blocked?
        # Look at events within 60 seconds of trigger
        ts_dt = parse_iso(ts_iso)
        window_start = (ts_dt - timedelta(seconds=2)).isoformat()
        window_end = (ts_dt + timedelta(seconds=120)).isoformat()
        followup = fconn.execute("""
            SELECT stage, note FROM flight_log
            WHERE pair=? AND timestamp BETWEEN ? AND ?
              AND stage IN ('SNIPE_BLOCKED','SNIPE_OPENED','SNIPE_GATE_BLOCKED')
            ORDER BY timestamp LIMIT 5
        """, (pair, window_start, window_end)).fetchall()

        block_reason = None
        actual_trade_id = None
        for f in followup:
            if f["stage"] == "SNIPE_OPENED":
                # Note format: "Snipe opened trade 12646"
                if f["note"] and "trade" in f["note"].lower():
                    parts = f["note"].split()
                    for p in parts:
                        if p.isdigit():
                            actual_trade_id = p
                            break
                break
            elif f["stage"] == "SNIPE_BLOCKED":
                # Extract block reason
                note = f["note"] or ""
                if "session_gate" in note:
                    block_reason = "session_gate"
                elif "kronos_4rule_trigger" in note:
                    block_reason = "kronos_4rule"
                elif "counter_momentum" in note:
                    block_reason = "counter_momentum"
                elif "ema21_position" in note:
                    block_reason = "ema21_position"
                elif "cooldown" in note.lower():
                    block_reason = "cooldown"
                else:
                    block_reason = "other_block"
                break

        # Get watch levels
        entry_zone, invalidation, _close_target = get_watch_levels(tconn, watch_id, pair)

        # Get current price at trigger time (use M15 candle that contains it)
        candles = fetch_m15_after(client, pair, ts_dt - timedelta(minutes=20),
                                   ts_dt + timedelta(minutes=2))
        if not candles:
            continue
        # Use close of last candle <= trigger time
        entry_price = None
        for c in candles:
            if c["time"] <= ts_dt:
                entry_price = c["c"]
        if entry_price is None:
            entry_price = candles[-1]["c"]

        # Compute SL/TP — prefer invalidation_level; fall back to ATR-based
        atr_p = compute_atr_pips(client, pair, ts_dt)
        pip = pip_size(pair)
        if direction == "sell":
            if invalidation and invalidation > entry_price:
                sl_price = invalidation
            else:
                sl_price = entry_price + (atr_p or 10) * pip * DEFAULT_SL_ATR_MULT
            tp_price = entry_price - (atr_p or 10) * pip * DEFAULT_TP_ATR_MULT
        else:  # buy
            if invalidation and invalidation < entry_price:
                sl_price = invalidation
            else:
                sl_price = entry_price - (atr_p or 10) * pip * DEFAULT_SL_ATR_MULT
            tp_price = entry_price + (atr_p or 10) * pip * DEFAULT_TP_ATR_MULT

        sl_pips = abs(entry_price - sl_price) / pip
        tp_pips = abs(tp_price - entry_price) / pip

        tconn.execute("""
            INSERT OR IGNORE INTO ghost_snipes
            (watch_id, pair, direction, triggered_at, triggered_at_utc,
             block_reason, actual_trade_id,
             entry_price, sl_price, tp_price, sl_pips, tp_pips,
             invalidation_level, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (watch_id, pair, direction, ts_iso, ts_iso[:19],
              block_reason, actual_trade_id,
              entry_price, sl_price, tp_price, round(sl_pips, 1), round(tp_pips, 1),
              invalidation, "pending"))
        new_count += 1

    tconn.commit()
    fconn.close()
    tconn.close()
    print(f"Recorded {new_count} new validator-origin ghost-snipe triggers "
          f"({skipped_kronos} kronos-origin events skipped — those have their own pipeline).")


def replay_pending():
    """Use optimizer.replay.candle_walk_replay (the guardian-equivalent
    simulator) to compute apples-to-apples outcomes against the live snipe
    guardian config. Tracks profit-floor ratchet, trailing activation,
    SL/TP hits — same logic the live guardian runs."""
    import pandas as pd

    tconn = sqlite3.connect(TRADING_DB)
    tconn.row_factory = sqlite3.Row
    client = OandaClient()
    snipe_params = load_snipe_guardian_params()

    pending = tconn.execute("""
        SELECT * FROM ghost_snipes WHERE outcome='pending' ORDER BY triggered_at_utc
    """).fetchall()

    replayed = 0
    for row in pending:
        ts_dt = parse_iso(row["triggered_at"])
        end_t = ts_dt + timedelta(minutes=15 * REPLAY_WINDOW_BARS)
        now = datetime.now(timezone.utc) - timedelta(minutes=1)
        if end_t > now:
            continue  # Not enough data yet

        candles = fetch_m15_after(client, row["pair"], ts_dt, end_t)
        if len(candles) < 2:
            continue

        # Build pandas DataFrame for candle_walk_replay
        df = pd.DataFrame([{
            "time": c["time"], "open": c["o"], "high": c["h"],
            "low": c["l"], "close": c["c"],
        } for c in candles])

        pip = pip_size(row["pair"])
        # Build TradeSnapshot — most fields are placeholders since we're
        # replaying a hypothetical entry, not auditing a real trade.
        # ATR is the one critical field — derived from sl_pips back to raw price.
        atr_raw = (row["sl_pips"] / DEFAULT_SL_ATR_MULT) * pip if row["sl_pips"] else 10 * pip

        snap = TradeSnapshot(
            id=f"ghost-{row['id']}",
            pair=row["pair"],
            direction=row["direction"],
            outcome="unknown",          # placeholder — filled by replay
            pnl_pips=0.0,               # placeholder
            realized_pl=0.0,
            fan_state="stable",
            bb_width=None,
            rsi=None, stoch_k=None,
            story_score=None,
            atr=atr_raw,                # raw price ATR
            confidence=None,
            entry_price=row["entry_price"],
            sl_price=row["sl_price"],
            tp_price=row["tp_price"],
            mfe=None, mae=None,
            session=None,
        )

        # Run guardian-equivalent replay
        result = candle_walk_replay(snap, df, snipe_params, reaction_delay_bars=1)

        sim_pnl = result["simulated_pnl"]
        sim_outcome = result["simulated_outcome"]
        exit_reason = result["exit_reason"]
        peak_pips = result.get("peak_pips", 0.0)
        bars_held = result.get("bars_held", 0)
        exit_bar = result.get("exit_bar", 0)

        # Compute exit price from exit_bar
        if 0 <= exit_bar < len(candles):
            exit_price = candles[exit_bar]["c"]
        else:
            exit_price = candles[-1]["c"]

        # MAE estimate from candles
        max_adv_pips = 0.0
        for i, c in enumerate(candles[:exit_bar + 1] if exit_bar < len(candles) else candles):
            if row["direction"] == "buy":
                adv = (row["entry_price"] - c["l"]) / pip
            else:
                adv = (c["h"] - row["entry_price"]) / pip
            if adv > max_adv_pips:
                max_adv_pips = adv

        # Map outcome to our schema
        if sim_outcome == "win":
            outcome = "win"
        elif sim_outcome == "loss":
            outcome = "loss"
        else:
            outcome = "flat"

        pnl_usd = sim_pnl * 5  # $5/pip standard sizing
        duration_min = bars_held * 15

        tconn.execute("""
            UPDATE ghost_snipes
               SET outcome=?, exit_price=?, exit_reason=?, pnl_pips=?, pnl_usd=?,
                   duration_minutes=?, max_favorable_pips=?, max_adverse_pips=?,
                   replayed_at=CURRENT_TIMESTAMP
             WHERE id=?
        """, (outcome, round(exit_price, 5), exit_reason,
              round(sim_pnl, 1), round(pnl_usd, 2),
              duration_min, round(peak_pips, 1), round(max_adv_pips, 1),
              row["id"]))
        replayed += 1

    tconn.commit()
    tconn.close()
    print(f"Replayed {replayed} pending ghost-snipes (guardian-equivalent logic).")


def report():
    """Summary statistics — was the gate doing its job?"""
    conn = sqlite3.connect(TRADING_DB)
    conn.row_factory = sqlite3.Row

    print("\n=== Ghost Snipes — overall (last 7 days) ===")
    rows = conn.execute("""
        SELECT
          CASE WHEN block_reason IS NULL THEN 'live_traded' ELSE block_reason END as bucket,
          COUNT(*) as n,
          SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
          SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
          SUM(CASE WHEN outcome='pending' THEN 1 ELSE 0 END) as pending,
          ROUND(AVG(CASE WHEN outcome IN ('win','loss','flat') THEN pnl_pips END), 1) as avg_pips,
          ROUND(SUM(CASE WHEN outcome IN ('win','loss','flat') THEN pnl_pips END), 1) as net_pips,
          ROUND(SUM(CASE WHEN outcome IN ('win','loss','flat') THEN pnl_usd END), 1) as net_usd
        FROM ghost_snipes
        WHERE triggered_at >= datetime('now','-7 days')
        GROUP BY bucket
        ORDER BY n DESC
    """).fetchall()

    print(f"{'bucket':<20} {'n':>4} {'win':>4} {'loss':>4} {'pend':>4} "
          f"{'avg_p':>7} {'net_p':>7} {'net_$':>8}")
    print("-" * 70)
    for r in rows:
        wr = (100.0 * r["wins"] / max(r["wins"] + r["losses"], 1)) if (r["wins"] + r["losses"]) else 0
        print(f"{r['bucket']:<20} {r['n']:>4} {r['wins']:>4} {r['losses']:>4} {r['pending']:>4} "
              f"{r['avg_pips'] or 0:>+7.1f} {r['net_pips'] or 0:>+7.1f} {r['net_usd'] or 0:>+8.1f}  "
              f"WR={wr:.0f}%")

    print("\n=== Per-pair breakdown ===")
    rows = conn.execute("""
        SELECT pair, COUNT(*) as n,
          SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
          SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
          ROUND(SUM(CASE WHEN outcome IN ('win','loss','flat') THEN pnl_pips END), 1) as net_pips
        FROM ghost_snipes
        WHERE triggered_at >= datetime('now','-7 days')
        GROUP BY pair
        ORDER BY net_pips DESC NULLS LAST
    """).fetchall()
    for r in rows:
        print(f"  {r['pair']:<10} n={r['n']} wins={r['wins']} losses={r['losses']} "
              f"net_pips={r['net_pips']}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("record", "all"):
        record_new_triggers()
    if cmd in ("replay", "all"):
        replay_pending()
    if cmd in ("report", "all"):
        report()


if __name__ == "__main__":
    main()
