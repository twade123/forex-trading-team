"""
Manual Trade Analyzer — finds patterns in human trades and proposes snipe promotions.

Run periodically (daily cron or heartbeat) to:
1. Cluster closed manual trades by pattern fingerprint
2. Identify winning patterns (65%+ WR, PF >= 1.2, 8+ trades)
3. Generate snipe conditions from the pattern
4. Notify the user of promotable patterns

Usage:
    python -m manual_trade_analyzer          # analyze and print
    python -m manual_trade_analyzer --notify # analyze and send notification
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("trading_bot.manual_trade_analyzer")

_CUSTOM_SETUPS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Config", "manual_playbook.json"
)


def analyze_and_report(user_id: int, pair: str = None, notify: bool = False) -> Dict:
    """Run full analysis on manual trades. Returns report dict."""
    from manual_trade_store import ManualTradeStore
    store = ManualTradeStore()

    stats = store.get_stats(user_id, pair)
    patterns = store.analyze_patterns(min_trades=5)
    promotable = [p for p in patterns if p['promotable']]

    report = {
        'total_trades': stats['total'],
        'overall_wr': stats['win_rate'],
        'overall_pf': stats['profit_factor'],
        'total_pips': stats['total_pips'],
        'total_pl': stats['total_pl'],
        'patterns_found': len(patterns),
        'promotable_count': len(promotable),
        'patterns': patterns,
        'promotable': promotable,
        'analyzed_at': datetime.utcnow().isoformat(),
    }

    if promotable:
        logger.info(f"🎯 Found {len(promotable)} promotable manual trade patterns:")
        for p in promotable:
            logger.info(
                f"  {p['pair']} {p['direction']} | {p['fingerprint']} | "
                f"{p['total_trades']} trades, {p['win_rate']}% WR, PF {p['profit_factor']}, "
                f"{p['total_pips']:+.1f} pips"
            )

    if not patterns:
        logger.info("No patterns found yet — need more manual trades")

    return report


def build_snipe_from_pattern(pattern: Dict) -> Dict:
    """Convert a pattern fingerprint into scout-compatible snipe conditions."""
    fp = pattern['fingerprint']
    parts = fp.split('|')

    if len(parts) < 9:
        return {}

    fan_state, fan_dir, ordered, e100_role, bb, mom, rsi_b, stoch_b, direction = parts

    conditions = []

    # Fan state condition
    if fan_state in ('peaked', 'decelerating', 'contracting'):
        conditions.append({
            'field': 'fan_state',
            'operator': 'in',
            'value': ['peaked', 'decelerating', 'contracting'],
            'description': f'Fan exhausting ({fan_state})',
        })
    elif fan_state in ('expanding', 'accelerating'):
        conditions.append({
            'field': 'fan_state',
            'operator': 'in',
            'value': ['expanding', 'accelerating'],
            'description': f'Fan healthy ({fan_state})',
        })

    # Fan ordering
    if ordered == 'ordered':
        conditions.append({
            'field': 'fan_ordered',
            'operator': '==',
            'value': True,
            'description': 'Fan fully ordered',
        })

    # E100 role
    if e100_role in ('support', 'resistance'):
        conditions.append({
            'field': 'e100_role',
            'operator': '==',
            'value': e100_role,
            'description': f'E100 acting as {e100_role}',
        })

    # BB
    if bb == 'bb_exp':
        conditions.append({
            'field': 'bb_expanding',
            'operator': '==',
            'value': True,
            'description': 'Bollinger Bands expanding',
        })

    # RSI bucket
    rsi_ranges = {'oversold': (0, 30), 'neutral': (30, 70), 'overbought': (70, 100)}
    if rsi_b in rsi_ranges:
        lo, hi = rsi_ranges[rsi_b]
        conditions.append({
            'field': 'rsi',
            'operator': 'between',
            'value': [lo, hi],
            'description': f'RSI {rsi_b} ({lo}-{hi})',
        })

    # Stoch bucket
    stoch_ranges = {'oversold': (0, 20), 'neutral': (20, 80), 'overbought': (80, 100)}
    if stoch_b in stoch_ranges:
        lo, hi = stoch_ranges[stoch_b]
        conditions.append({
            'field': 'stoch_k',
            'operator': 'between',
            'value': [lo, hi],
            'description': f'Stochastic {stoch_b} ({lo}-{hi})',
        })

    # Momentum state
    if mom not in ('unknown', 'neutral'):
        conditions.append({
            'field': 'momentum_state',
            'operator': '==',
            'value': mom,
            'description': f'Momentum: {mom}',
        })

    return {
        'name': f"MANUAL_{pattern['pair']}_{direction.upper()}_{fan_state}",
        'pair': pattern['pair'],
        'direction': direction,
        'conditions': conditions,
        'source': 'manual_trade_analysis',
        'stats': {
            'trades': pattern['total_trades'],
            'win_rate': pattern['win_rate'],
            'profit_factor': pattern['profit_factor'],
            'total_pips': pattern['total_pips'],
        },
        'fingerprint': pattern['fingerprint'],
        'created_at': datetime.utcnow().isoformat(),
    }


def promote_pattern(pattern: Dict) -> str:
    """Add a promotable pattern to the manual playbook JSON."""
    snipe = build_snipe_from_pattern(pattern)
    if not snipe:
        return "Failed to build snipe from pattern"

    # Load or create playbook
    playbook = []
    if os.path.exists(_CUSTOM_SETUPS_PATH):
        try:
            with open(_CUSTOM_SETUPS_PATH) as f:
                playbook = json.load(f)
        except Exception:
            playbook = []

    # Check for duplicate
    for existing in playbook:
        if existing.get('fingerprint') == pattern['fingerprint']:
            return f"Pattern already in playbook: {existing['name']}"

    playbook.append(snipe)

    os.makedirs(os.path.dirname(_CUSTOM_SETUPS_PATH), exist_ok=True)
    with open(_CUSTOM_SETUPS_PATH, 'w') as f:
        json.dump(playbook, f, indent=2)

    # Mark as promoted in unified live_trades table
    from db_pool import get_trading_forex
    _lt_conn = get_trading_forex()
    _lt_conn.execute(
        "UPDATE live_trades SET promoted_to_snipe = 1 WHERE pattern_fingerprint = ?",
        (pattern['fingerprint'],)
    )
    _lt_conn.commit()

    logger.info(f"🎯 Promoted manual pattern to playbook: {snipe['name']}")
    return f"Promoted: {snipe['name']} ({pattern['total_trades']} trades, {pattern['win_rate']}% WR)"


def get_report_text(user_id: int, pair: str = None) -> str:
    """Get a human-readable analysis report."""
    report = analyze_and_report(user_id, pair)

    if report['total_trades'] == 0:
        return "No manual trades yet. Place some BUY/SELL trades from the chart card to start building patterns."

    lines = [
        f"📊 **Manual Trade Analysis** ({report['analyzed_at'][:10]})",
        f"Total: {report['total_trades']} trades | WR: {report['overall_wr']}% | PF: {report['overall_pf']} | {report['total_pips']:+.1f} pips (${report['total_pl']:+.2f})",
        "",
    ]

    if report['patterns']:
        lines.append(f"**Patterns Found: {report['patterns_found']}**")
        for p in sorted(report['patterns'], key=lambda x: -x['win_rate']):
            promo = " 🎯 PROMOTABLE" if p['promotable'] else ""
            lines.append(
                f"  {p['pair']} {p['direction'].upper()} | "
                f"{p['total_trades']}t {p['win_rate']}% WR PF {p['profit_factor']} | "
                f"{p['total_pips']:+.1f}p | {p['fingerprint'][:40]}...{promo}"
            )

    if report['promotable']:
        lines.append("")
        lines.append(f"🎯 **{report['promotable_count']} pattern(s) ready for promotion to scout playbook!**")
        lines.append("Say 'promote manual patterns' to add them.")

    return "\n".join(lines)


if __name__ == '__main__':
    import argparse
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    # Add Source to path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    p = argparse.ArgumentParser(description="Manual trade analyzer (per-user)")
    p.add_argument("--user-id", type=int, required=True,
                   help="User ID to analyze (integer; user_id column in live_trades)")
    p.add_argument("--pair", default=None,
                   help="Optional pair filter (e.g. EUR_USD)")
    p.add_argument("--promote-all", action="store_true",
                   help="Promote all promotable patterns to the playbook")
    args = p.parse_args()

    report = analyze_and_report(args.user_id, args.pair)
    print(get_report_text(args.user_id, args.pair))

    if args.promote_all:
        for pat in report.get('promotable', []):
            result = promote_pattern(pat)
            print(f"  {result}")
