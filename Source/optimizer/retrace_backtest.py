"""
Retrace state machine backtest — compare M5 vs M15 detection.

For each closed trade:
1. Fetch M5 and M15 candles covering entry → exit + 2 hours
2. Compute EMA 21/55/100 and BB(20,2) on both timeframes
3. Run retrace state machine on both: record state transitions,
   threat scores, and when auto-close would fire at thresholds 75/90
4. Compare: detection speed, false signals, outcome impact

Usage:
    python optimizer/retrace_backtest.py [--pairs EUR_USD,NZD_USD] [--since 2026-03-24]
"""

from __future__ import annotations
import sys, os, time, json, sqlite3, argparse, logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import requests
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OANDA candle fetcher
# ---------------------------------------------------------------------------

def _get_oanda_config():
    import config
    return config.BASE_URL, config.get_default_headers()


def fetch_candles(pair: str, granularity: str, from_time: str, to_time: str) -> pd.DataFrame:
    """Fetch candles from OANDA and return as DataFrame."""
    base, headers = _get_oanda_config()
    all_candles = []
    current_from = from_time

    while True:
        params = {
            'granularity': granularity,
            'from': current_from,
            'to': to_time,
        }
        r = requests.get(f'{base}/v3/instruments/{pair}/candles', headers=headers, params=params)
        data = r.json()
        candles = data.get('candles', [])
        if not candles:
            break
        all_candles.extend(candles)
        # Move forward
        last_time = candles[-1]['time']
        if last_time >= to_time or len(candles) < 2:
            break
        current_from = last_time

    if not all_candles:
        return pd.DataFrame()

    rows = []
    for c in all_candles:
        m = c['mid']
        rows.append({
            'time': c['time'][:19],
            'open': float(m['o']),
            'high': float(m['h']),
            'low': float(m['l']),
            'close': float(m['c']),
        })

    df = pd.DataFrame(rows).drop_duplicates(subset='time').sort_values('time').reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------

def compute_emas(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMA 21, 55, 100 columns."""
    df = df.copy()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema55'] = df['close'].ewm(span=55, adjust=False).mean()
    df['ema100'] = df['close'].ewm(span=100, adjust=False).mean()
    return df


def compute_bb(df: pd.DataFrame, period: int = 20, std_mult: float = 2.0) -> pd.DataFrame:
    """Add Bollinger Band columns."""
    df = df.copy()
    df['bb_mid'] = df['close'].rolling(period).mean()
    df['bb_std'] = df['close'].rolling(period).std()
    df['bb_upper'] = df['bb_mid'] + std_mult * df['bb_std']
    df['bb_lower'] = df['bb_mid'] - std_mult * df['bb_std']
    df['bb_width'] = df['bb_upper'] - df['bb_lower']
    return df


# ---------------------------------------------------------------------------
# Retrace state machine (standalone simulation)
# ---------------------------------------------------------------------------

@dataclass
class RetraceSimResult:
    """Result of simulating the retrace state machine on one trade."""
    trade_id: str
    pair: str
    direction: str
    timeframe: str  # 'M5' or 'M15'
    entry_price: float
    sl_price: float
    tp_price: float
    actual_outcome: str
    actual_pnl: float

    # State transitions: list of (bar_time, old_state, new_state, pnl_at_bar)
    transitions: List[Tuple[str, str, str, float]] = field(default_factory=list)

    # Auto-close events
    auto_close_75_time: Optional[str] = None
    auto_close_75_pnl: Optional[float] = None
    auto_close_90_time: Optional[str] = None
    auto_close_90_pnl: Optional[float] = None

    # How long trade spent in each state (bar count)
    bars_trending: int = 0
    bars_retracing: int = 0
    bars_continuing: int = 0

    # Retrace oscillation count (trending↔retracing flips)
    oscillation_count: int = 0

    # Would trade have hit SL or TP?
    sl_hit_time: Optional[str] = None
    tp_hit_time: Optional[str] = None


def simulate_retrace(
    df: pd.DataFrame,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    direction: str,
    entry_idx: int,
    trade_id: str,
    pair: str,
    timeframe: str,
    confirm_bars: int = 2,  # bars needed to confirm state change
) -> RetraceSimResult:
    """
    Run retrace state machine on candle data from entry_idx onward.

    Args:
        confirm_bars: number of consecutive confirming bars needed for
                      state transitions (M5: 2, M15: 1 for comparison)
    """
    is_long = direction.lower() in ('buy', 'long')
    pip_size = 0.01 if 'JPY' in pair else 0.0001

    result = RetraceSimResult(
        trade_id=trade_id, pair=pair, direction=direction,
        timeframe=timeframe, entry_price=entry_price,
        sl_price=sl_price, tp_price=tp_price,
        actual_outcome='', actual_pnl=0.0,
    )

    state = 'trending'
    peak_ema_sep = 0.0
    peak_bb_width = 0.0
    ema_sep_history = []
    bb_width_history = []
    reexpansion_count = 0
    contraction_count = 0  # for confirming retracing entry
    expansion_confirm = 0  # for confirming continuing→trending exit
    prev_state = state

    for i in range(entry_idx, len(df)):
        row = df.iloc[i]
        bar_time = row['time']
        price = row['close']
        high = row['high']
        low = row['low']

        # Check SL/TP
        if is_long:
            pnl_pips = (price - entry_price) / pip_size
            if low <= sl_price and result.sl_hit_time is None:
                result.sl_hit_time = bar_time
            if high >= tp_price and result.tp_hit_time is None:
                result.tp_hit_time = bar_time
        else:
            pnl_pips = (entry_price - price) / pip_size
            if high >= sl_price and result.sl_hit_time is None:
                result.sl_hit_time = bar_time
            if low <= tp_price and result.tp_hit_time is None:
                result.tp_hit_time = bar_time

        # Skip if indicators not ready
        if pd.isna(row.get('ema21')) or pd.isna(row.get('ema55')) or pd.isna(row.get('bb_width')):
            continue

        e21 = row['ema21']
        e55 = row['ema55']
        e100 = row['ema100'] if not pd.isna(row.get('ema100')) else 0
        bb_width = row['bb_width']

        # EMA separation
        ema_sep = abs(e21 - e55)
        ema_sep_history.append(ema_sep)
        if ema_sep > peak_ema_sep:
            peak_ema_sep = ema_sep

        # BB width tracking
        bb_width_history.append(bb_width)
        if bb_width > peak_bb_width:
            peak_bb_width = bb_width

        # Contraction / expansion detection
        ema_contracting = False
        bb_contracting = False
        if len(ema_sep_history) >= 2:
            ema_contracting = ema_sep_history[-1] < ema_sep_history[-2]
        if len(bb_width_history) >= 2:
            bb_contracting = bb_width_history[-1] < bb_width_history[-2]

        both_contracting = bb_contracting and ema_contracting
        both_expanding = not bb_contracting and not ema_contracting

        # ── Retrace state machine (with confirmation) ──
        old_state = state

        if state == 'trending':
            if both_contracting:
                contraction_count += 1
            else:
                contraction_count = 0

            if contraction_count >= confirm_bars:
                state = 'retracing'
                reexpansion_count = 0
                contraction_count = 0

        elif state == 'retracing':
            if both_expanding:
                reexpansion_count += 1
            else:
                reexpansion_count = 0

            if reexpansion_count >= confirm_bars:
                state = 'continuing'
                reexpansion_count = 0

        elif state == 'continuing':
            if both_contracting:
                contraction_count += 1
                expansion_confirm = 0
            elif both_expanding:
                expansion_confirm += 1
                contraction_count = 0
            else:
                expansion_confirm = 0
                contraction_count = 0

            # Need confirm_bars of sustained expansion to declare trending
            if expansion_confirm >= confirm_bars:
                state = 'trending'
                expansion_confirm = 0

            # New retrace starting
            if contraction_count >= confirm_bars:
                state = 'retracing'
                reexpansion_count = 0
                contraction_count = 0

        # Track state
        if state == 'trending':
            result.bars_trending += 1
        elif state == 'retracing':
            result.bars_retracing += 1
        elif state == 'continuing':
            result.bars_continuing += 1

        # Track transitions
        if state != old_state:
            result.transitions.append((bar_time, old_state, state, round(pnl_pips, 1)))
            if (old_state == 'trending' and state == 'retracing') or \
               (old_state in ('continuing', 'retracing') and state == 'trending'):
                result.oscillation_count += 1

        # ── Threat scoring (simplified) ──
        # Mirrors the core threat drivers from score_threat():
        # - E100 proximity
        # - Fan width collapse
        # - Fan not favorable
        threat = 0
        in_retrace = state in ('retracing', 'continuing')

        if e100 > 0:
            e100_dist_pct = abs(price - e100) / e100 * 100
            fan_width_pct = abs(e21 - e100) / e100 * 100 if e21 > 0 else 0

            # Proximity risk
            proximity_risk = 0
            e100_wrong_side = (is_long and price < e100) or (not is_long and price > e100)
            if e100_wrong_side:
                proximity_risk = 70
            elif e100_dist_pct < 0.02:
                proximity_risk = 50
            elif e100_dist_pct < 0.05:
                proximity_risk = 35
            elif e100_dist_pct < 0.10:
                proximity_risk = 15

            # Retrace discount
            if in_retrace and proximity_risk > 0 and e55 > 0:
                price_to_e55 = abs(price - e55)
                price_to_e100 = abs(price - e100)
                ema_convergence = abs(e55 - e100) / e100 * 100
                if price_to_e55 < price_to_e100:
                    if ema_convergence < 0.05:
                        proximity_risk = int(proximity_risk * 0.20)
                    elif ema_convergence < 0.10:
                        proximity_risk = int(proximity_risk * 0.40)
                    elif ema_convergence < 0.15:
                        proximity_risk = int(proximity_risk * 0.60)
                    else:
                        proximity_risk = int(proximity_risk * 0.80)

            # Fan collapse penalty (only outside retrace)
            fan_favorable = (is_long and e21 > e55) or (not is_long and e21 < e55)
            if fan_width_pct < 0.03 and e100_dist_pct < 0.10 and not in_retrace and not fan_favorable:
                proximity_risk = min(100, proximity_risk + 20)

            threat = min(100, proximity_risk + 27 if proximity_risk >= 35 else proximity_risk)

        # Check auto-close thresholds
        in_retrace_protection = state in ('retracing', 'continuing')

        if threat >= 75 and not in_retrace_protection and result.auto_close_75_time is None:
            result.auto_close_75_time = bar_time
            result.auto_close_75_pnl = round(pnl_pips, 1)

        if threat >= 90 and not in_retrace_protection and result.auto_close_90_time is None:
            result.auto_close_90_time = bar_time
            result.auto_close_90_pnl = round(pnl_pips, 1)

        prev_state = state

    return result


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_backtest(since: str = '2026-03-24', pairs: Optional[List[str]] = None):
    """Run the full retrace backtest comparing M5 vs M15."""

    DB = '~/Jarvis/Database/v2/trading_forex.db'
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT id, pair, direction, entry_price, sl_price, tp_price,
               pnl_pips, pnl_usd, outcome, source, entry_time, exit_time
        FROM live_trades
        WHERE status='closed' AND entry_time >= ?
    """
    params = [since]
    if pairs:
        placeholders = ','.join('?' * len(pairs))
        query += f' AND pair IN ({placeholders})'
        params.extend(pairs)
    query += ' ORDER BY entry_time'

    trades = conn.execute(query, params).fetchall()
    conn.close()

    log.info(f"Found {len(trades)} closed trades since {since}")

    results_m5 = []
    results_m15 = []
    skipped = 0

    for trade in trades:
        tid = trade['id']
        pair = trade['pair']
        direction = trade['direction']
        entry_price = trade['entry_price']
        sl_price = trade['sl_price']
        tp_price = trade['tp_price']
        entry_time = trade['entry_time']
        exit_time = trade['exit_time']

        if not entry_price or not sl_price or not tp_price or not entry_time or not exit_time:
            skipped += 1
            continue

        # Parse times — add buffer before entry and after exit
        try:
            if '+' in entry_time:
                entry_dt = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
            elif entry_time.endswith('Z'):
                entry_dt = datetime.fromisoformat(entry_time[:-1]).replace(tzinfo=timezone.utc)
            else:
                entry_dt = datetime.fromisoformat(entry_time).replace(tzinfo=timezone.utc)

            if '+' in exit_time:
                exit_dt = datetime.fromisoformat(exit_time.split('.')[0].replace('Z', '+00:00'))
            elif exit_time.endswith('Z'):
                exit_dt = datetime.fromisoformat(exit_time.split('.')[0]).replace(tzinfo=timezone.utc)
            else:
                exit_dt = datetime.fromisoformat(exit_time.split('.')[0]).replace(tzinfo=timezone.utc)
        except Exception as e:
            log.warning(f"  Trade {tid}: time parse error: {e}")
            skipped += 1
            continue

        # Need candles starting 100 bars before entry for EMA warmup
        # M5: 100 bars = ~8.3 hours, M15: 100 bars = 25 hours
        from_m5 = (entry_dt - timedelta(hours=9)).strftime('%Y-%m-%dT%H:%M:%SZ')
        from_m15 = (entry_dt - timedelta(hours=26)).strftime('%Y-%m-%dT%H:%M:%SZ')
        to_time = (exit_dt + timedelta(hours=3)).strftime('%Y-%m-%dT%H:%M:%SZ')

        log.info(f"Trade {tid} {pair} {direction} ({trade['outcome']} {trade['pnl_pips']}p)...")

        # Fetch candles
        try:
            df_m5 = fetch_candles(pair, 'M5', from_m5, to_time)
            df_m15 = fetch_candles(pair, 'M15', from_m15, to_time)
        except Exception as e:
            log.warning(f"  Fetch failed: {e}")
            skipped += 1
            continue

        if len(df_m5) < 50 or len(df_m15) < 30:
            log.warning(f"  Insufficient candles: M5={len(df_m5)}, M15={len(df_m15)}")
            skipped += 1
            continue

        # Compute indicators
        df_m5 = compute_emas(compute_bb(df_m5))
        df_m15 = compute_emas(compute_bb(df_m15))

        # Find entry bar index
        entry_str = entry_dt.strftime('%Y-%m-%dT%H:%M')

        m5_entry_idx = None
        for idx, row in df_m5.iterrows():
            if row['time'][:16] >= entry_str[:16]:
                m5_entry_idx = idx
                break

        m15_entry_idx = None
        for idx, row in df_m15.iterrows():
            if row['time'][:16] >= entry_str[:16]:
                m15_entry_idx = idx
                break

        if m5_entry_idx is None or m15_entry_idx is None:
            log.warning(f"  Could not find entry bar")
            skipped += 1
            continue

        # Run simulations
        # M5 with 2-bar confirmation (research recommendation)
        r_m5 = simulate_retrace(
            df_m5, entry_price, sl_price, tp_price, direction,
            m5_entry_idx, str(tid), pair, 'M5', confirm_bars=2
        )
        r_m5.actual_outcome = trade['outcome']
        r_m5.actual_pnl = trade['pnl_pips'] or 0
        results_m5.append(r_m5)

        # M15 with 1-bar confirmation (current behavior, but per-bar not per-tick)
        r_m15 = simulate_retrace(
            df_m15, entry_price, sl_price, tp_price, direction,
            m15_entry_idx, str(tid), pair, 'M15', confirm_bars=1
        )
        r_m15.actual_outcome = trade['outcome']
        r_m15.actual_pnl = trade['pnl_pips'] or 0
        results_m15.append(r_m15)

        # Rate limit OANDA API
        time.sleep(0.3)

    log.info(f"\nSkipped {skipped} trades (missing data)")

    # ── Print comparison report ──
    print_report(results_m5, results_m15)

    return results_m5, results_m15


def print_report(m5_results: List[RetraceSimResult], m15_results: List[RetraceSimResult]):
    """Print comparison report."""

    print("\n" + "=" * 90)
    print("RETRACE BACKTEST: M5 (2-bar confirm) vs M15 (1-bar confirm)")
    print("=" * 90)

    for label, results in [("M5 (2-bar confirm)", m5_results), ("M15 (1-bar)", m15_results)]:
        print(f"\n{'─' * 45}")
        print(f"  {label}: {len(results)} trades")
        print(f"{'─' * 45}")

        total = len(results)
        if total == 0:
            continue

        # Oscillation stats
        osc = [r.oscillation_count for r in results]
        print(f"  Retrace oscillations: avg={sum(osc)/total:.1f}, max={max(osc)}, "
              f"zero={sum(1 for o in osc if o == 0)}/{total}")

        # Time in states
        avg_trending = sum(r.bars_trending for r in results) / total
        avg_retracing = sum(r.bars_retracing for r in results) / total
        avg_continuing = sum(r.bars_continuing for r in results) / total
        total_bars = avg_trending + avg_retracing + avg_continuing
        if total_bars > 0:
            print(f"  Avg bars: trending={avg_trending:.0f} ({avg_trending/total_bars*100:.0f}%), "
                  f"retracing={avg_retracing:.0f} ({avg_retracing/total_bars*100:.0f}%), "
                  f"continuing={avg_continuing:.0f} ({avg_continuing/total_bars*100:.0f}%)")

        # Auto-close at 75
        ac75 = [r for r in results if r.auto_close_75_time]
        ac75_losses = [r for r in ac75 if r.actual_outcome == 'loss']
        ac75_wins = [r for r in ac75 if r.actual_outcome == 'win']
        print(f"\n  Auto-close @ 75 would fire: {len(ac75)}/{total} trades "
              f"({len(ac75_losses)} actual losses, {len(ac75_wins)} actual wins)")
        if ac75:
            avg_ac75_pnl = sum(r.auto_close_75_pnl for r in ac75) / len(ac75)
            print(f"    Avg PnL at fire: {avg_ac75_pnl:+.1f}p")

        # Auto-close at 90
        ac90 = [r for r in results if r.auto_close_90_time]
        ac90_losses = [r for r in ac90 if r.actual_outcome == 'loss']
        ac90_wins = [r for r in ac90 if r.actual_outcome == 'win']
        print(f"  Auto-close @ 90 would fire: {len(ac90)}/{total} trades "
              f"({len(ac90_losses)} actual losses, {len(ac90_wins)} actual wins)")
        if ac90:
            avg_ac90_pnl = sum(r.auto_close_90_pnl for r in ac90) / len(ac90)
            print(f"    Avg PnL at fire: {avg_ac90_pnl:+.1f}p")

        # Trades where auto-close would have killed winners
        ac75_killed_wins = [r for r in ac75_wins if r.auto_close_75_pnl and r.auto_close_75_pnl < 0]
        ac90_killed_wins = [r for r in ac90_wins if r.auto_close_90_pnl and r.auto_close_90_pnl < 0]
        print(f"\n  Winners killed at 75: {len(ac75_killed_wins)} "
              f"(lost: {sum(r.auto_close_75_pnl for r in ac75_killed_wins):.1f}p total)")
        print(f"  Winners killed at 90: {len(ac90_killed_wins)} "
              f"(lost: {sum(r.auto_close_90_pnl for r in ac90_killed_wins):.1f}p total)")

        # Losses saved by auto-close (closed before SL hit)
        ac75_saved = [r for r in ac75_losses
                      if r.auto_close_75_pnl is not None and r.actual_pnl < r.auto_close_75_pnl]
        ac90_saved = [r for r in ac90_losses
                      if r.auto_close_90_pnl is not None and r.actual_pnl < r.auto_close_90_pnl]
        saved_75 = sum(r.auto_close_75_pnl - r.actual_pnl for r in ac75_saved) if ac75_saved else 0
        saved_90 = sum(r.auto_close_90_pnl - r.actual_pnl for r in ac90_saved) if ac90_saved else 0
        print(f"  Loss pips saved at 75: {saved_75:+.1f}p ({len(ac75_saved)} trades)")
        print(f"  Loss pips saved at 90: {saved_90:+.1f}p ({len(ac90_saved)} trades)")

    # ── Head-to-head comparison ──
    print(f"\n{'=' * 90}")
    print("HEAD-TO-HEAD: Trades where M5 and M15 disagree")
    print("=" * 90)

    for r5, r15 in zip(m5_results, m15_results):
        m5_fires = r5.auto_close_75_time is not None
        m15_fires = r15.auto_close_75_time is not None
        if m5_fires != m15_fires:
            who = "M5 only" if m5_fires else "M15 only"
            pnl = r5.auto_close_75_pnl if m5_fires else r15.auto_close_75_pnl
            print(f"  #{r5.trade_id} {r5.pair} {r5.direction} ({r5.actual_outcome} {r5.actual_pnl:+.1f}p): "
                  f"AC@75 fires on {who} at {pnl:+.1f}p | "
                  f"M5 osc={r5.oscillation_count} M15 osc={r15.oscillation_count}")

    # Oscillation reduction
    m5_osc = sum(r.oscillation_count for r in m5_results)
    m15_osc = sum(r.oscillation_count for r in m15_results)
    print(f"\n  Total oscillations: M5={m5_osc}, M15={m15_osc}")
    if m15_osc > 0:
        print(f"  Oscillation reduction with M5: {(1 - m5_osc/m15_osc)*100:.0f}%")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Retrace state machine backtest')
    parser.add_argument('--since', default='2026-03-24', help='Start date')
    parser.add_argument('--pairs', default=None, help='Comma-separated pairs')
    args = parser.parse_args()

    pairs = args.pairs.split(',') if args.pairs else None
    run_backtest(since=args.since, pairs=pairs)
