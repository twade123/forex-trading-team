#!/usr/bin/env python3
"""
Scout Retrospective — Daily M15 feedback loop.

Runs at EOD (default 5 PM ET). For each of the 13 pairs:
  1. Pulls M15 candles for the day
  2. Computes EMA fan state at each scout decision point
  3. Scores what actually happened AFTER each scout call
  4. Classifies: correct block, missed setup, correct watch, false snipe
  5. Writes structured report to knowledge vault
  6. After 5+ days, extracts pattern learnings for agents

Usage:
    python3 scout_retrospective.py              # analyze today
    python3 scout_retrospective.py 2026-03-05   # analyze specific date
    python3 scout_retrospective.py --summarize  # weekly pattern summary
"""
import os, sys, re, json, sqlite3, logging, urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_connection import get_db

OANDA_KEY   = os.environ.get('OANDA_API_KEY') or open(os.path.expanduser('~/jarvis/API/OANDA_API_KEY.txt')).read().strip()
ACCOUNT     = '101-001-24637237-001'

# ── Primary data source: signal_log + validation_log in trade_log.db ──
# The old SCOUT_LOGS text files (/tmp/serve_ui.log, dashboard.log) no longer
# exist. All validator verdicts are now stored in the signal_log table with
# verdict type in decision_reasoning JSON and action field.
_SOURCE_DIR  = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_DB = os.path.join(os.path.dirname(_SOURCE_DIR), 'Data', 'trade_log.db')
FLIGHT_DB    = os.path.join(_SOURCE_DIR, 'flight_recorder.db')

# Legacy log paths — kept as fallback but these files typically don't exist
SCOUT_LOGS  = [
    '/tmp/serve_ui.log',
    os.path.expanduser('~/jarvis/Forex Trading Team/Source/logs/dashboard.log'),
    os.path.expanduser('~/jarvis/Forex Trading Team/Source/logs/dashboard.log.prev'),
]
VAULT_DIR   = os.path.expanduser('~/jarvis/knowledge')
RETRO_DIR   = os.path.join(VAULT_DIR, 'collective', 'scout-retrospective')
PATTERNS_DIR= os.path.join(VAULT_DIR, 'collective', 'patterns')
VAULT_DB    = os.path.join(VAULT_DIR, '_index.db')

PAIRS = ['AUD_JPY','AUD_USD','EUR_AUD','EUR_CHF','EUR_GBP',
         'EUR_JPY','EUR_USD','GBP_JPY','GBP_USD','NZD_USD',
         'USD_CAD','USD_CHF','USD_JPY']

PIP_MULTIPLIER = {'JPY': 100, 'DEFAULT': 10000}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger('retrospective')

# ── trade_log.db connection via get_db context manager ─────────────────────
# trade_log.db is NOT in the pool (it's not trading_forex.db), so use get_db().
# Callers should use:  with get_db(TRADE_LOG_DB) as conn: ...
# Legacy helper kept for call-sites that haven't been converted to context-mgr yet.
def _get_trade_log_conn():
    """Get a short-lived connection to trade_log.db via get_db.

    NOTE: Returns a context-manager-wrapped connection. Caller must use
    ``with _get_trade_log_conn() as conn:`` or call the returned cm directly.
    For backward compat this still returns a plain connection, but callers
    should migrate to ``with get_db(TRADE_LOG_DB) as conn:``.
    """
    return get_db(TRADE_LOG_DB)

# ── Helpers ─────────────────────────────────────────────────────────────────

def pip_mult(pair):
    return PIP_MULTIPLIER['JPY'] if 'JPY' in pair else PIP_MULTIPLIER['DEFAULT']

def to_pips(price_diff, pair):
    return abs(price_diff) * pip_mult(pair)

def ema(data, period):
    if len(data) < period:
        return data[-1] if data else 0
    k = 2 / (period + 1)
    e = sum(data[:period]) / period
    for p in data[period:]:
        e = p * k + e * (1 - k)
    return e

def fan_state(closes, i):
    """Compute EMA fan direction at bar i using preceding bars."""
    window = closes[:i+1]
    if len(window) < 110:
        return 'insufficient_data'
    e21  = ema(window, 21)
    e55  = ema(window, 55)
    e100 = ema(window, 100)
    if e21 > e55 > e100:
        return 'bullish_ordered'
    elif e21 < e55 < e100:
        return 'bearish_ordered'
    else:
        return 'disordered'

def fan_width(closes, i):
    window = closes[:i+1]
    if len(window) < 110:
        return 0
    e21  = ema(window, 21)
    e100 = ema(window, 100)
    return abs(e21 - e100)

def measure_outcome(candles, from_idx, pair, lookahead=48):
    """
    Measure what actually happened in the next `lookahead` M15 bars.
    Returns: dict with max_bull_move, max_bear_move, net_move, direction, clean_trend
    """
    future = candles[from_idx+1 : from_idx+1+lookahead]
    if not future:
        return None
    entry_close = float(candles[from_idx]['mid']['c'])
    highs  = [float(c['mid']['h']) for c in future]
    lows   = [float(c['mid']['l']) for c in future]
    closes = [float(c['mid']['c']) for c in future]
    if not highs:
        return None
    max_high = max(highs)
    min_low  = min(lows)
    final    = closes[-1]
    bull_pips = to_pips(max_high - entry_close, pair)
    bear_pips = to_pips(entry_close - min_low, pair)
    net_pips  = (final - entry_close) * pip_mult(pair)
    direction = 'bullish' if net_pips > 5 else 'bearish' if net_pips < -5 else 'choppy'
    clean_trend = max(bull_pips, bear_pips) > 20 and min(bull_pips, bear_pips) < 10
    return {
        'bull_pips': round(bull_pips, 1),
        'bear_pips': round(bear_pips, 1),
        'net_pips':  round(net_pips, 1),
        'direction': direction,
        'clean_trend': clean_trend,
        'max_move': round(max(bull_pips, bear_pips), 1)
    }

# ── OANDA data fetch ─────────────────────────────────────────────────────────

def fetch_m15(pair, target_date: date):
    """Fetch M15 candles for target_date (use count=200, filter to date)."""
    # OANDA practice doesn't accept `to` with nanosecond precision — use count only
    url = (f'https://api-fxpractice.oanda.com/v3/instruments/{pair}/candles'
           f'?granularity=M15&count=200&price=M')
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {OANDA_KEY}'})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        candles = data.get('candles', [])
        date_str = str(target_date)
        return [c for c in candles if c['time'].startswith(date_str)]
    except Exception as e:
        log.warning(f"Failed to fetch {pair} M15: {e}")
        return []

# ── Scout log parser ─────────────────────────────────────────────────────────

def parse_scout_decisions(target_date: date):
    """
    Parse validator verdicts for target_date from signal_log in trade_log.db.

    Primary source: signal_log table has action (buy/sell/hold) and
    decision_reasoning JSON with verdict prefix (WATCH:/REJECT:/SKIP:/CONFIRM:).
    Fallback: legacy text log files (rarely available).

    Returns: {pair: [{'time': HH:MM, 'type': CONFIRM|WATCH|REJECT|SKIP,
                       'direction': str, 'reason': str}]}
    """
    date_str = str(target_date)       # YYYY-MM-DD for SQL
    seen = set()
    by_pair = {p: [] for p in PAIRS}

    # ── Primary: read from signal_log (trade_log.db) ──────────────────────
    db_found = 0
    if os.path.exists(TRADE_LOG_DB):
        try:
            with get_db(TRADE_LOG_DB) as conn:
              rows = conn.execute("""
                SELECT instrument, action, timestamp, decision_reasoning, confluence_score
                FROM signal_log
                WHERE substr(timestamp, 1, 10) = ?
                  AND instrument IN ({})
                ORDER BY timestamp
              """.format(','.join('?' * len(PAIRS))), [date_str] + PAIRS).fetchall()

            for row in rows:
                pair = row['instrument']
                action = row['action']
                ts = row['timestamp']
                hhmm = ts[11:16] if len(ts) > 15 else '00:00'
                dr = row['decision_reasoning'] or '{}'

                # Deduplicate by pair + minute
                key = f"{pair}_{hhmm}"
                if key in seen:
                    continue
                seen.add(key)

                # Extract verdict type from decision_reasoning JSON
                verdict = 'SKIP'  # default
                direction = ''
                reason = ''
                try:
                    d = json.loads(dr)
                    reasons = d.get('reasons', [])
                    if reasons:
                        reason = reasons[0][:200]
                        # Extract verdict prefix: "WATCH: ...", "REJECT: ...", etc.
                        m = re.match(r'^([A-Z_]+):', reason)
                        if m:
                            raw_verdict = m.group(1)
                            # Map to canonical types
                            if raw_verdict in ('WATCH',):
                                verdict = 'WATCH'
                            elif raw_verdict in ('REJECT',):
                                verdict = 'REJECT'
                            elif raw_verdict in ('SKIP', 'SL'):
                                verdict = 'SKIP'
                            elif raw_verdict in ('CONFIRM',):
                                verdict = 'CONFIRM'
                            else:
                                verdict = raw_verdict
                except Exception:
                    pass

                # Override verdict based on action (buy/sell = CONFIRM)
                if action in ('buy', 'sell'):
                    verdict = 'CONFIRM'
                    direction = action.upper()
                elif 'dir=' in reason:
                    dm = re.search(r'dir=(\w+)', reason)
                    if dm and dm.group(1) != 'None':
                        direction = dm.group(1).upper()

                by_pair[pair].append({
                    'time':      hhmm,
                    'type':      verdict,
                    'direction': direction,
                    'reason':    reason or f"Validator {verdict} {direction}"
                })
                db_found += 1

            log.info(f"Parsed {db_found} verdicts from signal_log for {date_str}")
        except Exception as e:
            log.warning(f"signal_log query failed: {e}")

    # ── Fallback: also check flight_log for any extra validator_verdict entries ──
    if os.path.exists(FLIGHT_DB):
        try:
            with get_db(FLIGHT_DB) as fc:
              fl_rows = fc.execute("""
                SELECT pair, note, timestamp
                FROM flight_log
                WHERE stage = 'validator_verdict'
                  AND substr(timestamp, 1, 10) = ?
                ORDER BY timestamp
              """, (date_str,)).fetchall()

            for row in fl_rows:
                pair = row['pair']
                if pair not in PAIRS:
                    continue
                ts = row['timestamp']
                hhmm = ts[11:16] if len(ts) > 15 else '00:00'
                key = f"{pair}_{hhmm}"
                if key in seen:
                    continue
                seen.add(key)

                note = row['note'] or ''
                # Parse: "WATCH dir=BUY conf=1.2 setup=? 2pass=False"
                verdict = 'SKIP'
                direction = ''
                vm = re.match(r'^(\w+)\s', note)
                if vm:
                    raw = vm.group(1)
                    if raw in ('WATCH', 'CONFIRM', 'REJECT', 'SKIP'):
                        verdict = raw
                dm = re.search(r'dir=(\w+)', note)
                if dm and dm.group(1) != 'None':
                    direction = dm.group(1).upper()

                by_pair[pair].append({
                    'time':      hhmm,
                    'type':      verdict,
                    'direction': direction,
                    'reason':    note
                })
                db_found += 1

        except Exception as e:
            log.warning(f"flight_log query failed: {e}")

    # ── Legacy fallback: text log files (rarely exist) ────────────────────
    if db_found == 0:
        log_date_str = target_date.strftime('%Y%m%d')
        for log_path in SCOUT_LOGS:
            try:
                with open(log_path, errors='replace') as f:
                    for line in f:
                        m = re.search(
                            r'Training data saved: ([A-Z_]+)_(CONFIRM|WATCH|REJECT|SKIP)_?([A-Z]*)_(\d{8})_(\d{6})',
                            line
                        )
                        if not m:
                            continue
                        pair, v, direction, fdate, ftime = m.groups()
                        if fdate != log_date_str or pair not in PAIRS:
                            continue
                        key = f"{pair}_{fdate}_{ftime}"
                        if key in seen:
                            continue
                        seen.add(key)
                        hhmm = f"{ftime[:2]}:{ftime[2:4]}"
                        by_pair[pair].append({
                            'time':      hhmm,
                            'type':      v,
                            'direction': direction,
                            'reason':    f"Validator {v} {direction}"
                        })
            except Exception:
                pass

    total = sum(len(v) for v in by_pair.values())
    log.info(f"Found {total} validator verdicts across {len(PAIRS)} pairs for {date_str}")
    return by_pair

# ── Retrospective scorer ─────────────────────────────────────────────────────

def score_pair(pair, candles, decisions):
    """
    For each scout decision on this pair, compute what actually happened after.
    Returns list of scored events.
    """
    if not candles:
        return []

    closes = [float(c['mid']['c']) for c in candles]
    times  = [c['time'][11:16] for c in candles]  # HH:MM
    scored = []

    for dec in decisions:
        dec_time = dec['time']
        # Find candle index closest to decision time
        idx = None
        for i, t in enumerate(times):
            if t >= dec_time:
                idx = i
                break
        if idx is None or idx >= len(candles) - 5:
            continue

        outcome = measure_outcome(candles, idx, pair, lookahead=16)
        if not outcome:
            continue

        state = fan_state(closes, idx)
        width = fan_width(closes, idx)
        close_at_decision = closes[idx]

        # Score the decision
        # FIX: training log uses CONFIRM/WATCH/SKIP/REJECT verdicts, but scorer only
        # handled BLOCKED/RETRACEMENT/SNIPE/FILTERED — causing correct_alerts = 0 always.
        # Mapped all V4 verdict types to correct scoring categories.
        verdict = 'UNKNOWN'
        _alert_types = ('CONFIRM', 'WATCH', 'RETRACEMENT', 'SNIPE', 'TRADE_NOW', 'CRITERIA_MET')
        _block_types  = ('SKIP', 'REJECT', 'BLOCKED', 'FILTERED', 'HOLD')

        if dec['type'] in _alert_types:
            # Scout said "this is a setup" — grade by whether price actually moved
            if outcome['max_move'] >= 15:
                verdict = 'CORRECT_ALERT'       # scout flagged it, 15+ pip move confirmed
            elif outcome['max_move'] < 8:
                verdict = 'FALSE_ALERT'         # scout flagged but nothing happened
            else:
                verdict = 'PARTIAL'             # small move, inconclusive
        elif dec['type'] in _block_types:
            # Scout said "no edge here" — grade by whether it correctly suppressed
            # Threshold: 30+ pip clean move in 4h window to count as genuinely missed
            if outcome['clean_trend'] and outcome['max_move'] >= 30:
                verdict = 'MISSED_OPPORTUNITY'  # scout blocked but a clean 30+ pip move followed
            else:
                verdict = 'CORRECT_BLOCK'       # scout blocked, no clean move = correct
        elif dec['type'] == 'CORRECT_FILTER':
            # Legacy type
            if outcome['clean_trend'] and outcome['max_move'] >= 30:
                verdict = 'MISSED_OPPORTUNITY'
            else:
                verdict = 'CORRECT_BLOCK'

        scored.append({
            'pair':       pair,
            'time':       dec_time,
            'scout_call': dec['type'],
            'reason':     dec.get('reason', ''),
            'verdict':    verdict,
            'fan_state':  state,
            'fan_width':  round(width * pip_mult(pair), 1),
            'price':      close_at_decision,
            'outcome':    outcome
        })

    return scored

# ── Vault writer ─────────────────────────────────────────────────────────────

def write_vault_report(target_date: date, all_scored, summary):
    """Write daily retrospective to knowledge vault."""
    Path(RETRO_DIR).mkdir(parents=True, exist_ok=True)
    date_str = target_date.strftime('%Y-%m-%d')
    filepath = os.path.join(RETRO_DIR, f"{date_str}.md")

    missed     = [s for s in all_scored if s['verdict'] == 'MISSED_OPPORTUNITY']
    correct_bl = [s for s in all_scored if s['verdict'] == 'CORRECT_BLOCK']
    correct_al = [s for s in all_scored if s['verdict'] == 'CORRECT_ALERT']
    false_al   = [s for s in all_scored if s['verdict'] == 'FALSE_ALERT']

    lines = [
        f"---",
        f"type: scout_retrospective",
        f"date: {date_str}",
        f"pairs_scanned: {len(PAIRS)}",
        f"total_decisions: {len(all_scored)}",
        f"missed_opportunities: {len(missed)}",
        f"correct_blocks: {len(correct_bl)}",
        f"correct_alerts: {len(correct_al)}",
        f"false_alerts: {len(false_al)}",
        f"tags: [scout, retrospective, M15, feedback-loop]",
        f"---",
        f"",
        f"# Scout Retrospective — {date_str} (M15)",
        f"",
        f"## Summary",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Total scout decisions | {len(all_scored)} |",
        f"| ✅ Correct blocks | {len(correct_bl)} |",
        f"| ✅ Correct alerts | {len(correct_al)} |",
        f"| ⚠️ Missed opportunities | {len(missed)} |",
        f"| ❌ False alerts | {len(false_al)} |",
        f"",
        f"**Scout accuracy:** {summary.get('accuracy_pct', 0):.0f}%  ",
        f"**Missed opportunity rate:** {summary.get('miss_rate_pct', 0):.0f}% of blocked pairs had 20+ pip clean moves",
        f"",
    ]

    if missed:
        lines += [
            f"## ⚠️ Missed Opportunities (scout blocked — move happened anyway)",
            f"These are the most valuable for refining scout's block logic.",
            f"",
        ]
        for s in missed:
            o = s['outcome']
            lines += [
                f"### {s['pair']} @ {s['time']}",
                f"- **Scout said:** {s['scout_call']} — {s['reason'][:120]}",
                f"- **Fan state at decision:** {s['fan_state']} (width: {s['fan_width']} pips)",
                f"- **Price:** {s['price']:.5f}",
                f"- **What happened next (48 M15 bars):** {o['direction'].upper()} move, max {o['max_move']} pips, net {o['net_pips']:+.1f} pips",
                f"- **Verdict:** Scout was too conservative. Consider relaxing block threshold for this pattern.",
                f"",
            ]

    if correct_al:
        lines += [
            f"## ✅ Correct Alerts",
            f"",
        ]
        for s in correct_al:
            o = s['outcome']
            lines += [
                f"- **{s['pair']} @ {s['time']}** ({s['scout_call']}): {o['direction']} {o['max_move']} pips — fan={s['fan_state']}",
            ]
        lines.append("")

    if false_al:
        lines += [
            f"## ❌ False Alerts (scout flagged — nothing materialized)",
            f"",
        ]
        for s in false_al:
            o = s['outcome']
            lines += [
                f"- **{s['pair']} @ {s['time']}** ({s['scout_call']}): only {o['max_move']} pips in 48 bars — fan={s['fan_state']}",
            ]
        lines.append("")

    if correct_bl:
        lines += [
            f"## ✅ Correct Blocks (blocked — market stayed choppy)",
            f"",
        ]
        for s in correct_bl[:10]:  # cap at 10 for readability
            lines += [
                f"- **{s['pair']} @ {s['time']}**: blocked ({s['reason'][:80]}) — max move only {s['outcome']['max_move']} pips ✓",
            ]
        lines.append("")

    lines += [
        f"## Raw Decision Log",
        f"",
        f"```json",
        json.dumps(all_scored, indent=2)[:3000],
        f"```",
    ]

    with open(filepath, 'w') as f:
        f.write('\n'.join(lines))

    log.info(f"Vault report written: {filepath}")
    return filepath

def index_vault_file(filepath, date_str):
    """Add/update file in vault FTS index."""
    try:
        with get_db(VAULT_DB) as conn:
            content = open(filepath).read()
            rel_path = os.path.relpath(filepath, VAULT_DIR)
            now = datetime.utcnow().isoformat()
            import hashlib
            content_hash = hashlib.md5(content.encode()).hexdigest()
            conn.execute("""
                INSERT OR REPLACE INTO files
                (path, title, file_type, created_at, updated_at, status, content_hash)
                VALUES (?, ?, 'scout_retrospective', ?, ?, 'active', ?)
            """, (rel_path, f"Scout Retrospective {date_str}", now, now, content_hash))
            # FTS full-text index
            try:
                conn.execute("INSERT INTO fts_content(content) VALUES (?)", (content,))
            except Exception:
                pass  # FTS table structure may vary
            log.info(f"Indexed in vault: {rel_path}")
    except Exception as e:
        log.warning(f"Vault index failed (non-critical): {e}")

def write_pattern_learnings(all_days_scored):
    """
    After 5+ days of data, extract patterns and write to collective/patterns/.
    Called from --summarize mode.
    """
    Path(PATTERNS_DIR).mkdir(parents=True, exist_ok=True)
    today = date.today().strftime('%Y-%m-%d')
    filepath = os.path.join(PATTERNS_DIR, f"scout-learnings-{today}.md")

    # Aggregate missed opportunities by pair and fan_state
    from collections import defaultdict, Counter
    miss_by_pair      = Counter()
    miss_by_fan       = Counter()
    miss_by_block_type = Counter()
    correct_by_pair   = Counter()
    total_by_pair     = Counter()

    for s in all_days_scored:
        total_by_pair[s['pair']] += 1
        if s['verdict'] == 'MISSED_OPPORTUNITY':
            miss_by_pair[s['pair']] += 1
            miss_by_fan[s['fan_state']] += 1
            reason = s.get('reason','')
            if 'disordered' in reason: miss_by_block_type['fan_disordered'] += 1
            elif 'fan_direction=neutral' in reason: miss_by_block_type['fan_neutral'] += 1
            elif 'chop zone' in reason: miss_by_block_type['chop_zone'] += 1
            elif 'E100 dist' in reason: miss_by_block_type['e100_dist'] += 1
        elif s['verdict'] in ('CORRECT_ALERT', 'CORRECT_BLOCK'):
            correct_by_pair[s['pair']] += 1

    lines = [
        f"---",
        f"type: scout_pattern_learnings",
        f"created: {today}",
        f"days_analyzed: {len(set(s.get('date','') for s in all_days_scored))}",
        f"total_decisions: {len(all_days_scored)}",
        f"tags: [scout, learnings, M15, playbook, feedback-loop]",
        f"---",
        f"",
        f"# Scout Pattern Learnings — Generated {today}",
        f"",
        f"## ⚠️ Most Frequent Missed Opportunities",
        f"These are the block conditions that most often precede real moves.",
        f"Scout's block logic should be reviewed for these patterns.",
        f"",
        f"### By Block Reason:",
    ]
    for reason, count in miss_by_block_type.most_common():
        lines.append(f"- **{reason}**: {count} missed opportunities")
    lines += [
        f"",
        f"### By Fan State at Block:",
    ]
    for state, count in miss_by_fan.most_common():
        lines.append(f"- **{state}**: {count} misses")
    lines += [
        f"",
        f"### By Pair (miss count / total decisions):",
    ]
    for pair in sorted(miss_by_pair, key=lambda p: -miss_by_pair[p]):
        total = total_by_pair.get(pair, 1)
        miss  = miss_by_pair[pair]
        lines.append(f"- **{pair}**: {miss} misses / {total} decisions ({miss/total*100:.0f}% miss rate)")
    lines += [
        f"",
        f"## 📋 Recommended Scout Tuning",
        f"Based on the data above, consider these adjustments:",
        f"",
    ]

    # Generate specific recommendations based on patterns
    if miss_by_block_type.get('fan_disordered', 0) >= 3:
        lines.append(f"- **Relax `fan_disordered` threshold**: {miss_by_block_type['fan_disordered']} misses suggest fan order detection may be too strict. Consider increasing `fan_order_tolerance` or checking if the 'disordered' classification fires too early during valid retracements.")
    if miss_by_block_type.get('fan_neutral', 0) >= 2:
        lines.append(f"- **Review `fan_direction=neutral` filter**: {miss_by_block_type['fan_neutral']} misses — neutral fan may sometimes precede explosive directional moves. Consider running a validator cycle even on neutral pairs when RSI is extreme.")
    if miss_by_block_type.get('chop_zone', 0) >= 2:
        lines.append(f"- **Review E100 chop zone distance**: {miss_by_block_type['chop_zone']} misses near E100 — current 5-pip minimum may be filtering valid E100 touch entries.")
    if not any(v >= 2 for v in miss_by_block_type.values()):
        lines.append(f"- No strong patterns yet — need more data days. Continue collecting.")
    lines += [
        f"",
        f"## ✅ Scout's Strongest Pairs",
        f"Pairs where scout's correct call rate is highest:",
        f"",
    ]
    for pair in sorted(correct_by_pair, key=lambda p: -correct_by_pair[p])[:5]:
        lines.append(f"- **{pair}**: {correct_by_pair[pair]} correct calls")
    lines.append("")

    with open(filepath, 'w') as f:
        f.write('\n'.join(lines))
    log.info(f"Pattern learnings written: {filepath}")
    return filepath

# ── Main ─────────────────────────────────────────────────────────────────────

def run_retrospective(target_date: date):
    log.info(f"=== Scout Retrospective: {target_date} (M15) ===")
    decisions = parse_scout_decisions(target_date)

    total_decisions = sum(len(v) for v in decisions.values())
    log.info(f"Found {total_decisions} scout decisions across {len(PAIRS)} pairs")

    all_scored = []
    for pair in PAIRS:
        pair_decisions = decisions.get(pair, [])
        if not pair_decisions:
            log.info(f"  {pair}: no decisions logged")
            continue
        log.info(f"  {pair}: {len(pair_decisions)} decisions — fetching M15...")
        candles = fetch_m15(pair, target_date)
        if not candles:
            log.warning(f"  {pair}: no M15 data")
            continue
        scored = score_pair(pair, candles, pair_decisions)
        for s in scored:
            s['date'] = str(target_date)
        all_scored.extend(scored)
        log.info(f"  {pair}: scored {len(scored)} decisions")

    # ── Check UNCYCLED pairs for truly big moves (genuine missed opportunities) ─
    # Only flag if: (a) price had a VERY clean directional move (50+ pips net),
    # (b) the move was directional (range < 2x net), and (c) there are real
    # cycled decisions to compare against — otherwise the entire report is just
    # "everything was missed" which provides no signal.
    cycled_pairs = set(p for p in PAIRS if decisions.get(p))
    uncycled     = [p for p in PAIRS if p not in cycled_pairs]
    if uncycled and cycled_pairs:  # Only add UNCYCLED if we have SOME real data
        log.info(f"Checking {len(uncycled)} uncycled pairs for missed moves...")
        for pair in uncycled:
            candles = fetch_m15(pair, target_date)
            if not candles or len(candles) < 10:
                continue
            closes = [float(c['mid']['c']) for c in candles]
            highs  = [float(c['mid']['h']) for c in candles]
            lows   = [float(c['mid']['l']) for c in candles]
            day_range = to_pips(max(highs) - min(lows), pair)
            net       = (closes[-1] - closes[0]) * pip_mult(pair)
            # Raised threshold: 50+ pip net move AND clean trend (range < 2x net)
            if abs(net) >= 50 and day_range < abs(net) * 2.0:
                direction = 'bullish' if net > 0 else 'bearish'
                all_scored.append({
                    'pair':       pair,
                    'time':       candles[0]['time'][11:16],
                    'scout_call': 'UNCYCLED',
                    'reason':     f'Scout never ran a cycle — pair completely ignored',
                    'verdict':    'MISSED_OPPORTUNITY',
                    'fan_state':  'unknown',
                    'fan_width':  0,
                    'price':      closes[0],
                    'date':       str(target_date),
                    'outcome':    {
                        'bull_pips': round(to_pips(max(highs)-closes[0], pair), 1),
                        'bear_pips': round(to_pips(closes[0]-min(lows), pair), 1),
                        'net_pips':  round(net, 1),
                        'direction': direction,
                        'clean_trend': True,
                        'max_move':  round(day_range, 1)
                    }
                })
                log.info(f"  UNCYCLED MISS: {pair} moved {net:+.1f} pips, range {day_range:.1f}p")
    elif uncycled and not cycled_pairs:
        log.warning(f"No cycled decisions found for {target_date} — skipping UNCYCLED check (no baseline data)")

    if not all_scored:
        log.warning("No decisions could be scored — check scout log for today's date")
        return

    # Compute summary stats
    missed   = sum(1 for s in all_scored if s['verdict'] == 'MISSED_OPPORTUNITY')
    correct  = sum(1 for s in all_scored if s['verdict'] in ('CORRECT_BLOCK','CORRECT_ALERT'))
    total    = len(all_scored)
    summary  = {
        'accuracy_pct':  correct / total * 100 if total else 0,
        'miss_rate_pct': missed  / total * 100 if total else 0,
        'total':         total,
        'missed':        missed,
        'correct':       correct,
    }

    # Print summary
    print(f"\n{'='*50}")
    print(f"SCOUT RETROSPECTIVE — {target_date}")
    print(f"{'='*50}")
    print(f"Total decisions scored:  {total}")
    print(f"Correct calls:           {correct} ({summary['accuracy_pct']:.0f}%)")
    print(f"Missed opportunities:    {missed}  ({summary['miss_rate_pct']:.0f}%)")
    print(f"")
    if missed > 0:
        print("MISSED OPPORTUNITIES:")
        for s in all_scored:
            if s['verdict'] == 'MISSED_OPPORTUNITY':
                o = s['outcome']
                print(f"  {s['pair']} @ {s['time']}: blocked ({s['reason'][:60]})")
                print(f"    → {o['direction']} {o['max_move']} pips followed")
    print(f"{'='*50}\n")

    # Write vault report
    filepath = write_vault_report(target_date, all_scored, summary)
    index_vault_file(filepath, str(target_date))

    print(f"✅ Report saved: {filepath}")
    return all_scored, summary

def run_weekly_summary():
    """Load last 7 days of retrospectives, extract patterns, update vault."""
    all_days = []
    for i in range(7):
        d = date.today() - timedelta(days=i+1)
        filepath = os.path.join(RETRO_DIR, f"{d}.md")
        if not os.path.exists(filepath):
            continue
        # Re-run retrospective if needed, or load existing JSON from it
        result = run_retrospective(d)
        if result:
            all_days.extend(result[0])

    if len(all_days) < 5:
        print(f"Only {len(all_days)} scored decisions across last 7 days — need more data")
        return

    filepath = write_pattern_learnings(all_days)

    # Index in vault
    index_vault_file(filepath, date.today().strftime('%Y-%m-%d'))
    print(f"\n✅ Weekly pattern learnings written: {filepath}")
    print("All agents with vault access will see these recommendations on next query.")

if __name__ == '__main__':
    if '--summarize' in sys.argv:
        run_weekly_summary()
    else:
        # Parse date arg or use today
        target = date.today()
        for arg in sys.argv[1:]:
            if re.match(r'\d{4}-\d{2}-\d{2}', arg):
                target = date.fromisoformat(arg)
                break
        run_retrospective(target)
