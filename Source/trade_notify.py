"""
trade_notify.py — Telegram notifications for trading events.

Writes notification payloads to a watched directory.
OpenClaw's notification watcher cron picks them up and sends via Telegram.

Events:
  - trade_opened     : entry price, direction, pair, units
  - trade_closed     : outcome, pips, P&L, exit reason
  - sniper_fired     : snipe triggered, conditions met
  - snipe_trade      : trade opened from a sniper alert
  - eod_summary      : end of day P&L recap

Usage:
    from trade_notify import notify_trade_opened, notify_trade_closed, ...
"""

import os
import json
import time
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_ID    = "6368550107"
NOTIFY_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "..", "notifications")
os.makedirs(NOTIFY_DIR, exist_ok=True)

_PREF_KEY_MAP = {
    "trade_opened":  "notif_trade_opened",
    "trade_closed":  "notif_trade_closed",
    "sniper_fired":  "notif_sniper_fired",
    "snipe_trade":   "notif_trade_opened",
    "eod_summary":   "notif_eod_summary",
}

def _get_pref(event_type: str, user_id: int = None) -> str:
    """Read user notification preference for this event. Default: realtime."""
    try:
        import sqlite3 as _sq
        # Resolve user_id from arg or TRADING_USER_ID env (set by serve_ui.py)
        _uid = user_id
        if not _uid:
            _env = os.environ.get("TRADING_USER_ID")
            _uid = int(_env) if _env else None
        _db = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "..", "Database", "v2", "core.db")
        _db = os.path.normpath(_db)
        _key = _PREF_KEY_MAP.get(event_type, "")
        if not _key:
            return "realtime"
        conn = _sq.connect(_db, timeout=3)
        row = conn.execute(
            "SELECT value FROM trading_preferences WHERE user_id=? AND key=?", (_uid, _key)
        ).fetchone()
        conn.close()
        return row[0] if row else "realtime"
    except Exception:
        return "realtime"


def _write(event_type: str, payload: dict, user_id: int = None):
    """Write notification payload to disk. OpenClaw cron picks it up.
    Respects user preference: realtime → write now | hourly/daily → bucket file | off → skip.
    """
    pref = _get_pref(event_type, user_id)
    if pref == "off":
        logger.debug("Notification suppressed (off): %s", event_type)
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    payload["event"] = event_type
    payload["telegram_id"] = TELEGRAM_ID
    payload["created_at"] = datetime.now(timezone.utc).isoformat()
    payload["pref"] = pref

    if pref == "realtime":
        path = os.path.join(NOTIFY_DIR, f"{event_type}_{ts}.json")
    elif pref == "hourly":
        # Bucket: one file per hour, appended as a list
        hour = datetime.now(timezone.utc).strftime("%Y%m%d_%H00")
        path = os.path.join(NOTIFY_DIR, f"digest_hourly_{hour}.json")
        _append_digest(path, payload)
        return
    else:  # daily
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = os.path.join(NOTIFY_DIR, f"digest_daily_{day}.json")
        _append_digest(path, payload)
        return

    try:
        with open(path, "w") as f:
            json.dump(payload, f)
        logger.debug("Notification queued: %s", os.path.basename(path))
    except Exception as e:
        logger.warning("Failed to queue notification: %s", e)


def _append_digest(path: str, payload: dict):
    """Append payload to a digest bucket file (list of events)."""
    try:
        existing = []
        if os.path.exists(path):
            with open(path) as f:
                existing = json.load(f)
        existing.append(payload)
        with open(path, "w") as f:
            json.dump(existing, f)
    except Exception as e:
        logger.warning("Failed to append digest: %s", e)


def notify_trade_opened(
    trade_id: str,
    pair: str,
    direction: str,
    units: int,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    source: str = "trading_team",   # 'sniper', 'trading_team', 'manual'
    snipe_id: str = None,
    user_id: int = None,
):
    """Fire when OANDA confirms a trade opened."""
    dir_emoji = "🔼 BUY" if direction.lower() == "buy" else "🔽 SELL"
    source_tag = f" 🎯 from sniper #{snipe_id}" if snipe_id else ""
    msg = (
        f"✅ TRADE OPENED{source_tag}\n"
        f"{dir_emoji} {pair}  {units:,} units\n"
        f"Entry: {entry_price}  SL: {sl_price}  TP: {tp_price}\n"
        f"Trade #{trade_id}"
    )
    _write("trade_opened", {
        "message": msg,
        "trade_id": trade_id,
        "pair": pair,
        "direction": direction,
        "units": units,
        "entry_price": entry_price,
        "source": source,
        "snipe_id": snipe_id,
    }, user_id)


def notify_trade_closed(
    trade_id: str,
    pair: str,
    direction: str,
    pnl_pips: float,
    pnl_usd: float,
    exit_reason: str,
    exit_price: float,
    units: int = 0,
    duration_min: int = None,
    from_snipe: bool = False,
    user_id: int = None,
):
    """Fire when guardian closes a trade."""
    win = pnl_pips > 0
    result_emoji = "✅ WIN" if win else ("❌ LOSS" if pnl_pips < 0 else "➖ BREAKEVEN")
    dir_str = "BUY" if direction.lower() == "buy" else "SELL"
    dur_str = f"  ⏱ {duration_min}min" if duration_min else ""
    snipe_tag = "  🎯 sniper trade" if from_snipe else ""
    msg = (
        f"{result_emoji}  {pair} {dir_str}{snipe_tag}\n"
        f"Pips: {pnl_pips:+.1f}p  |  P&L: ${pnl_usd:+.2f}{dur_str}\n"
        f"Exit @ {exit_price}  ({exit_reason})"
    )
    _write("trade_closed", {
        "message": msg,
        "trade_id": trade_id,
        "pair": pair,
        "pnl_pips": pnl_pips,
        "pnl_usd": pnl_usd,
        "result": "win" if win else "loss",
        "exit_reason": exit_reason,
    }, user_id)


def notify_sniper_fired(
    watch_id: int,
    pair: str,
    conditions_met: int,
    conditions_total: int,
    peak_pct: float,
    direction: str = None,
    user_id: int = None,
):
    """Fire when a snipe threshold is hit and trade is queued."""
    dir_str = f" ({direction.upper()})" if direction else ""
    pct = round(conditions_met / conditions_total * 100) if conditions_total else 0
    msg = (
        f"🎯 SNIPER FIRED  {pair}{dir_str}\n"
        f"Conditions: {conditions_met}/{conditions_total} ({pct}%)"
        f"  |  Peak: {peak_pct:.0f}%\n"
        f"Snipe #{watch_id} — queuing trade cycle"
    )
    _write("sniper_fired", {
        "message": msg,
        "watch_id": watch_id,
        "pair": pair,
        "conditions_met": conditions_met,
        "conditions_total": conditions_total,
    }, user_id)


def notify_eod_summary(
    date_str: str,
    wins: int,
    losses: int,
    total_pnl_usd: float,
    gross_wins_usd: float,
    gross_losses_usd: float,
    best_trade: dict = None,   # {"pair": ..., "pnl_usd": ..., "pips": ...}
    worst_trade: dict = None,
    user_id: int = None,
):
    """End of day summary — called by cron at 5 PM ET."""
    wr = round(wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    net_emoji = "📈" if total_pnl_usd >= 0 else "📉"
    best_str = (f"\nBest:  {best_trade['pair']} +${best_trade['pnl_usd']:.2f} ({best_trade['pips']:+.0f}p)"
                if best_trade else "")
    worst_str = (f"\nWorst: {worst_trade['pair']} ${worst_trade['pnl_usd']:.2f} ({worst_trade['pips']:+.0f}p)"
                 if worst_trade else "")
    msg = (
        f"{net_emoji} END OF DAY  {date_str}\n"
        f"Trades: {wins}W / {losses}L  ({wr}% WR)\n"
        f"Net P&L: ${total_pnl_usd:+.2f}"
        f"  (Wins: +${gross_wins_usd:.2f}  Losses: -${abs(gross_losses_usd):.2f})"
        f"{best_str}{worst_str}"
    )
    _write("eod_summary", {
        "message": msg,
        "date": date_str,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "total_pnl_usd": total_pnl_usd,
    }, user_id)


def notify_watchdog_restart(service_name: str, reason: str = "health check failed") -> None:
    """Alert when watchdog auto-restarts a critical service."""
    _write("watchdog_restart", {
        "message": (
            f"⚠️ WATCHDOG RESTART\n"
            f"Service: {service_name}\n"
            f"Reason: {reason}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        ),
        "service": service_name,
        "reason": reason,
    })
