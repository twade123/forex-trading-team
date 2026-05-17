"""Render M15 charts AS OF each of today's trade entries with the fixed
iter 20d pattern detector + label pipeline.

For each closed trade today: fetch M15 candles up to entry time (plus 30h
warmup), run pattern detection, render chart with labels + EMA fan + BB.
Save to outputs/today_trades/ with descriptive filename.
"""
from __future__ import annotations
import sys
import os
import sqlite3
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)
sys.path.insert(0, SOURCE_DIR)

import pandas as pd
from oanda_client import OandaClient
from chart_generator import generate_chart
from scripts.pattern_detectors import detect_patterns_for_validator

DB = '~/Jarvis/Database/v2/trading_forex.db'
OUT_DIR = '~/Documents/Cowork Files/outputs/today_trades_2026-05-11'
os.makedirs(OUT_DIR, exist_ok=True)


def parse_iso(s):
    if not s:
        return None
    s = s.replace('Z', '').rstrip()
    if '.' in s:
        b, f = s.split('.', 1); s = f"{b}.{f[:6]}"
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def main():
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("""
        SELECT id, pair, direction, source, entry_time, exit_time,
               entry_price, pnl_pips, pnl_usd,
               max_favorable_excursion_pips AS mfe,
               max_adverse_excursion_pips AS mae,
               exit_trigger
        FROM live_trades
        WHERE date(exit_time,'localtime')=date('now','localtime')
          AND status='closed'
        ORDER BY entry_time
    """).fetchall()]
    conn.close()
    print(f'{len(rows)} trades to render → {OUT_DIR}')

    oanda = OandaClient()
    for t in rows:
        try:
            entry_time = parse_iso(t['entry_time'])
            # Pull candles from 30h before entry → entry time (so chart shows
            # what was visible at the moment validator confirmed)
            candles = oanda.fetch_candles_range(
                instrument=t['pair'], granularity='M15',
                from_time=entry_time - timedelta(hours=30),
                to_time=entry_time + timedelta(minutes=1),
                price='M',
            )
            if not candles or len(candles) < 60:
                print(f'  {t["id"]} {t["pair"]}: insufficient candles ({len(candles) if candles else 0})')
                continue

            fires = detect_patterns_for_validator(
                candles,
                fan_direction='bullish' if t['direction'] in ('buy', 'long') else 'bearish',
                phase=3,
                pair_hint=t['pair'],
            )

            # Build df for chart
            df_rows = []
            for c in candles:
                m = c.get('mid', {})
                df_rows.append({
                    'time': c['time'],
                    'open': float(m['o']), 'high': float(m['h']),
                    'low': float(m['l']), 'close': float(m['c']),
                    'volume': c.get('volume', 0),
                })
            df = pd.DataFrame(df_rows)

            # Tag the chart filename with outcome
            outcome = 'W' if (t['pnl_pips'] or 0) > 0 else 'L'
            ent_et = (entry_time - timedelta(hours=4)).strftime('%H%M')
            trig = (t['exit_trigger'] or 'natural').replace('failed_rally_lock', 'FRL')[:8]
            base = f"{t['pair']}_{t['direction'][:1].upper()}_{ent_et}_{outcome}_{t['pnl_pips']:+.1f}p_{trig}_id{t['id']}"

            # Render with pattern_labels (fixed pipeline)
            chart_path = generate_chart(
                pair=t['pair'], df=df,
                pattern_labels=fires or None,
            )

            # Move to outputs folder with descriptive name
            target = os.path.join(OUT_DIR, base + '.png')
            os.rename(chart_path, target)
            print(f'  ✓ {t["id"]} {t["pair"]} {t["direction"]} '
                  f'pnl={t["pnl_pips"]:+.1f}p MFE={t["mfe"] or 0:.1f} MAE={t["mae"] or 0:.1f} '
                  f'fires={len(fires)} → {os.path.basename(target)}')
        except Exception as e:
            print(f'  ✗ {t["id"]} {t["pair"]}: {e}')

    print()
    print(f'All charts in: {OUT_DIR}')
    # List sorted by outcome for review
    files = sorted(os.listdir(OUT_DIR))
    print(f'\n{len(files)} chart files saved.')


if __name__ == '__main__':
    main()
