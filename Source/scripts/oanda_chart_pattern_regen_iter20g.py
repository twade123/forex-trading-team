"""oanda_chart_pattern_regen_iter20g.py — iter 20g chart helper.

As of 2026-05-13 chart_generator.py natively integrates the shared
backtester.ema_separation.format_chart_signals() function (same source the
dashboard backend uses), so the Exit↓/↑, Close↑/↓, return-exit, E100-test,
entry and EMA-cross markers Tim sees on his trading UI ARE NOW automatically
on the validator's chart.

This helper is now a thin wrapper over the standard
scripts/oanda_chart_pattern_regen.regenerate_chart_with_patterns. It exists
only so replay_iter20g.py has a stable import name; semantically there's
nothing iter20g-specific to do here anymore.
"""

from scripts.oanda_chart_pattern_regen import regenerate_chart_with_patterns


def regenerate_chart_with_patterns_and_exits(pair, entry_time_iso, output_path):
    return regenerate_chart_with_patterns(pair, entry_time_iso, output_path)


def smoke_test(pair="EUR_AUD", entry_time_iso="2026-05-13T16:46:03+00:00"):
    import os
    out = "/tmp/smoke_iter20g_exit.png"
    result, fires = regenerate_chart_with_patterns_and_exits(pair, entry_time_iso, out)
    if result and os.path.exists(result):
        print(f"SMOKE TEST PASS: {pair} {entry_time_iso}")
        print(f"  → {result} ({os.path.getsize(result)//1024}KB)")
        print(f"  → pattern fires: {[f['name'] for f in fires]}")
        print(f"  (EMA signals via format_chart_signals render automatically inside chart_generator)")
        return True
    print(f"SMOKE TEST FAIL: {pair} {entry_time_iso}")
    return False


if __name__ == "__main__":
    import logging, sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    pair = sys.argv[1] if len(sys.argv) > 1 else "EUR_AUD"
    entry = sys.argv[2] if len(sys.argv) > 2 else "2026-05-13T16:46:03+00:00"
    smoke_test(pair, entry)
