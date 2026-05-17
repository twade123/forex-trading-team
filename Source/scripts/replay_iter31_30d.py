"""replay_iter20g.py — iter 20g: EXHAUSTION COMPOSITE (3-item, Option 2).

Same stack as iter 20f PLUS a new "EXHAUSTION SIGNATURE" section that runs
parallel to the CONTINUATION composite. 3 signals — move maturity (8+ bars
no retrace), body decay (recent bodies shrinking), E55 distance overshoot
(> 2× ATR). 2+/3 fire alongside continuation 4+/6 = WATCH; 3/3 = SKIP.

Cohort: iter 20f 24-trade + 7 new from 2026-05-13 losing/open trades (31 total).

PASS gate (deploy to live):
- ≥22/24 acceptable on existing iter 20f base cohort (NO regression)
- ≥5/7 of today's 7 losing/open trades downgraded to WATCH or SKIP
- USD_CHF buy (15205) preserved as TRADE_NOW or WATCH (NOT SKIP)
- Net IDEAL pips ≥ +48.1p (iter 20d baseline)

ITERATE gate (try Option 1 — full 6-item composite):
- 20-21/24 on base cohort (mild regression)
- 3-4/7 on today's trades (partial catch)

REVERT gate:
- <20/24 on base or <3/7 on today

Run:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python3 scripts/replay_iter20g.py
"""

# Iter 20d stack docstring follows:
"""

Stack of changes vs iter 16 v2 baseline:
1. Swing-trace overlay (red/green dots + connecting line)
2. Pattern detectors fire for each chart, tunable via DETECTOR_ENABLED
3. Detected patterns labeled on the chart at fire-bar with verbatim names
4. Prompt dynamically includes "DETECTED PATTERNS" section with library quotes
5. Session-gate awareness (AUD UTC 21-22 weekday + existing rules)
6. Confirmation-candle filter + invalidation-tripwire on patterns (iter 20)
7. Scout history backfill (as-of-entry-time, non-leaky) (iter 20)
8. Iter 20a: badge thresholds n≥5 + strengthened scout guardrail language
9. Iter 20c: 6-signal CONTINUATION composite (fan ordering + candle-vs-all-EMAs +
   candle color + fan velocity + BB state + band-tracing); 4+ of 6 confirm =
   CONTINUATION, deep RSI alone insufficient to SKIP. Recovered 13362 BAD→IDEAL.
10. **NEW iter 20d**: PATTERN-CONFLICT VETO inside the continuation composite —
    confirmed reversal pattern at entry bar against trade direction (e.g.
    Bearish Engulfing on a BUY) subtracts 2 from continuation count.
    Effectively forces WATCH unless 6/6 still confirm post-veto. Targets 13843
    regression (TRADE_NOW BUY despite Bearish Engulfing + Doji gravestone).

Run:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python3 scripts/replay_iter20d.py
"""

import base64
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from scripts.oanda_chart_pattern_regen_iter20g import regenerate_chart_with_patterns_and_exits as regenerate_chart_with_patterns
from scripts.pattern_library_quotes import build_pattern_section
from scripts.pattern_detectors import DETECTOR_ENABLED

PROMPT_PATH = "/tmp/prompt_variants/iter31.md"
INDICATOR_BLOCKS_JSON = "/tmp/cohort_indicator_blocks.json"
LOCAL_ENDPOINT = "http://127.0.0.1:11502/v1/chat/completions"
LOCAL_MODEL_NAME = "mlx-community/Qwen3.5-35B-A3B-4bit"

OUT_RESULTS = "/tmp/iter31_30d_results.json"
OUT_LOG = "/tmp/iter31_30d_replay.log"
PATTERN_CHART_DIR = "/tmp/replay_charts_iter31_30d"

COHORT = [
    ("6717", "USD_CHF", "SELL", "2026-04-16T00:48:18+00:00",    5.5, "winner_snipe_direct"),
    ("6739", "USD_JPY", "SELL", "2026-04-16T01:05:01+00:00",    4.6, "winner_snipe_direct"),
    ("6755", "EUR_AUD", "SELL", "2026-04-16T02:27:54+00:00",    3.9, "winner_snipe_direct"),
    ("6811", "EUR_AUD", "SELL", "2026-04-16T02:49:55+00:00",    4.8, "winner_snipe_direct"),
    ("6845", "USD_CHF", "SELL", "2026-04-16T03:18:28+00:00",  -14.8, "loser_snipe_direct"),
    ("6865", "USD_JPY", "SELL", "2026-04-16T03:37:12+00:00",  -38.4, "loser_snipe_direct"),
    ("6883", "EUR_AUD", "SELL", "2026-04-16T04:04:38+00:00",   -9.6, "loser_snipe_direct"),
    ("7068", "EUR_AUD", "SELL", "2026-04-16T13:47:57+00:00",  -12.7, "loser_snipe_direct"),
    ("7452", "USD_CHF", "SELL", "2026-04-17T12:53:28+00:00",    4.4, "winner_snipe_direct"),
    ("7458", "EUR_AUD", "SELL", "2026-04-17T12:55:02+00:00",    5.3, "winner_snipe_direct"),
    ("7470", "USD_CHF", "SELL", "2026-04-17T13:36:13+00:00",    5.1, "winner_snipe_direct"),
    ("7490", "EUR_AUD", "SELL", "2026-04-17T13:55:11+00:00",  -15.5, "loser_snipe_direct"),
    ("7522", "GBP_JPY", "SELL", "2026-04-17T14:32:42+00:00",    4.1, "winner_scout"),
    ("7544", "USD_CHF", "SELL", "2026-04-17T15:04:47+00:00",   -4.0, "loser_snipe_direct"),
    ("7572", "EUR_CHF", "SELL", "2026-04-19T21:21:50+00:00",    0.0, "break_even_snipe_direct"),
    ("7582", "AUD_JPY", "SELL", "2026-04-19T21:47:18+00:00",    0.0, "break_even_scout"),
    ("7596", "USD_CHF", "SELL", "2026-04-20T10:32:30+00:00",    4.2, "winner_snipe_direct"),
    ("7629", "EUR_CHF", "SELL", "2026-04-20T12:49:54+00:00",    3.9, "winner_snipe_direct"),
    ("7639", "USD_CHF", "SELL", "2026-04-20T13:19:38+00:00",    6.0, "winner_snipe_direct"),
    ("7673", "USD_CHF", "SELL", "2026-04-20T13:47:27+00:00",    6.3, "winner_scout"),
    ("7679", "EUR_CHF", "SELL", "2026-04-20T13:59:37+00:00",   -1.7, "loser_snipe_direct"),
    ("7689", "EUR_AUD", "SELL", "2026-04-20T14:00:53+00:00",   -9.9, "loser_snipe_direct"),
    ("7703", "USD_JPY", "SELL", "2026-04-20T14:34:37+00:00",    5.8, "winner_snipe_direct"),
    ("7713", "EUR_CHF", "SELL", "2026-04-20T14:39:39+00:00",    6.2, "winner_snipe_direct"),
    ("7735", "USD_CHF", "SELL", "2026-04-20T14:59:40+00:00",    4.8, "winner_snipe_direct"),
    ("7745", "EUR_CHF", "SELL", "2026-04-20T15:14:35+00:00",   -5.4, "loser_snipe_direct"),
    ("7783", "USD_JPY", "SELL", "2026-04-20T15:45:17+00:00",   -4.5, "loser_snipe_direct"),
    ("7801", "USD_CHF", "SELL", "2026-04-20T16:45:30+00:00",   -0.5, "loser_snipe_direct"),
    ("7815", "EUR_AUD", "SELL", "2026-04-20T18:20:21+00:00",   -3.2, "loser_snipe_direct"),
    ("7843", "EUR_AUD", "SELL", "2026-04-20T20:15:35+00:00",    0.0, "break_even_snipe_direct"),
    ("7920", "EUR_AUD", "SELL", "2026-04-21T01:32:50+00:00",  -23.9, "loser_snipe_direct"),
    ("8046", "EUR_CHF", "SELL", "2026-04-21T10:16:27+00:00",    0.9, "winner_snipe_direct"),
    ("8444", "AUD_USD", "SELL", "2026-04-21T18:26:59+00:00",    8.5, "winner_snipe_direct"),
    ("8853", "EUR_AUD", "SELL", "2026-04-22T05:42:29+00:00",    3.1, "winner_snipe_direct"),
    ("8867", "EUR_CHF", "SELL", "2026-04-22T06:02:32+00:00",  -10.2, "loser_snipe_direct"),
    ("8879", "USD_CHF", "SELL", "2026-04-22T06:32:36+00:00",    0.8, "winner_snipe_direct"),
    ("8901", "EUR_AUD", "SELL", "2026-04-22T06:57:22+00:00",  -19.0, "loser_snipe_direct"),
    ("9015", "AUD_USD", "SELL", "2026-04-22T13:01:25+00:00",  -15.5, "loser_snipe_direct"),
    ("9247", "AUD_USD", "SELL", "2026-04-23T00:14:52+00:00",    4.9, "winner_snipe_direct"),
    ("9281", "AUD_USD", "SELL", "2026-04-23T01:48:56+00:00",    1.0, "winner_snipe_direct"),
    ("9301", "AUD_USD", "SELL", "2026-04-23T03:03:41+00:00",    3.2, "winner_snipe_direct"),
    ("9403", "EUR_JPY", "SELL", "2026-04-23T10:08:14+00:00",    1.0, "winner_snipe_direct"),
    ("9431", "GBP_JPY", "BUY", "2026-04-23T10:33:47+00:00",  -42.7, "loser_snipe_direct"),
    ("9435", "EUR_JPY", "BUY", "2026-04-23T10:33:47+00:00",  -18.5, "loser_snipe_direct"),
    ("9463", "NZD_USD", "SELL", "2026-04-23T12:20:04+00:00",    2.0, "winner_snipe_direct"),
    ("9495", "GBP_USD", "SELL", "2026-04-23T12:49:52+00:00",    3.1, "winner_snipe_direct"),
    ("9505", "NZD_USD", "SELL", "2026-04-23T12:56:30+00:00",    2.6, "winner_snipe_direct"),
    ("9559", "AUD_JPY", "SELL", "2026-04-23T13:25:38+00:00",  -24.6, "loser_snipe_direct"),
    ("9569", "NZD_USD", "SELL", "2026-04-23T13:32:06+00:00",    0.9, "winner_snipe_direct"),
    ("9579", "GBP_USD", "SELL", "2026-04-23T13:41:33+00:00",   -4.3, "loser_snipe_direct"),
    ("9611", "EUR_JPY", "SELL", "2026-04-23T14:19:19+00:00",    2.8, "winner_snipe_direct"),
    ("9633", "NZD_USD", "SELL", "2026-04-23T14:45:24+00:00",    2.4, "winner_snipe_direct"),
    ("9659", "NZD_USD", "BUY", "2026-04-23T15:12:12+00:00",    2.8, "winner_snipe_direct"),
    ("9673", "EUR_JPY", "SELL", "2026-04-23T15:17:51+00:00",    1.0, "winner_snipe_direct"),
    ("9697", "GBP_USD", "SELL", "2026-04-23T15:34:36+00:00",    7.9, "winner_snipe_direct"),
    ("9729", "NZD_USD", "BUY", "2026-04-23T15:46:14+00:00",  -18.6, "loser_snipe_direct"),
    ("9743", "GBP_USD", "BUY", "2026-04-23T15:46:41+00:00",  -29.8, "loser_snipe_direct"),
    ("9749", "EUR_JPY", "SELL", "2026-04-23T15:49:51+00:00",    2.7, "winner_snipe_direct"),
    ("9785", "EUR_JPY", "BUY", "2026-04-23T16:23:12+00:00",    1.0, "winner_snipe_direct"),
    ("9807", "EUR_JPY", "BUY", "2026-04-23T16:40:18+00:00",    0.0, "break_even_snipe_direct"),
    ("9821", "EUR_USD", "BUY", "2026-04-23T17:33:31+00:00",    3.0, "winner_snipe_direct"),
    ("9831", "NZD_USD", "BUY", "2026-04-23T17:40:06+00:00",    7.0, "winner_snipe_direct"),
    ("9841", "GBP_USD", "BUY", "2026-04-23T17:47:00+00:00",    5.5, "winner_snipe_direct"),
    ("9897", "NZD_USD", "BUY", "2026-04-23T18:39:16+00:00",    0.0, "break_even_snipe_direct"),
    ("9923", "USD_CHF", "SELL", "2026-04-23T19:09:04+00:00",   -3.4, "loser_snipe_direct"),
    ("9941", "AUD_USD", "SELL", "2026-04-23T20:04:20+00:00",    3.5, "winner_snipe_direct"),
    ("9967", "USD_CHF", "SELL", "2026-04-23T20:25:26+00:00",   -3.4, "loser_snipe_direct"),
    ("9990", "EUR_JPY", "BUY", "2026-04-23T21:18:15+00:00",  -15.0, "loser_snipe_direct"),
    ("10008", "NZD_USD", "BUY", "2026-04-24T00:49:26+00:00",    0.9, "winner_snipe_direct"),
    ("10028", "NZD_USD", "BUY", "2026-04-24T01:32:54+00:00",   -8.1, "loser_snipe_direct"),
    ("10038", "EUR_USD", "BUY", "2026-04-24T01:42:57+00:00",    0.7, "winner_snipe_direct"),
    ("10056", "AUD_USD", "BUY", "2026-04-24T02:00:00+00:00",  -11.0, "loser_snipe_direct"),
    ("10076", "GBP_USD", "BUY", "2026-04-24T02:30:42+00:00",   -8.1, "loser_snipe_direct"),
    ("10094", "AUD_USD", "SELL", "2026-04-24T03:31:27+00:00",  -25.8, "loser_snipe_direct"),
    ("10104", "EUR_JPY", "SELL", "2026-04-24T07:20:13+00:00",  -18.2, "loser_snipe_direct"),
    ("10118", "USD_JPY", "BUY", "2026-04-24T16:25:41+00:00",   -0.3, "loser_snipe_direct"),
    ("10128", "USD_CHF", "BUY", "2026-04-24T16:29:58+00:00",   10.5, "winner_snipe_direct"),
    ("10144", "USD_JPY", "BUY", "2026-04-24T17:16:39+00:00",    3.4, "winner_snipe_direct"),
    ("10164", "USD_JPY", "BUY", "2026-04-24T18:07:09+00:00",    2.2, "winner_snipe_direct"),
    ("12632", "USD_CHF", "SELL", "2026-04-27T02:31:38+00:00",    0.8, "winner_snipe_direct"),
    ("12646", "NZD_USD", "SELL", "2026-04-27T15:03:29+00:00",    1.2, "winner_snipe_direct"),
    ("12674", "USD_JPY", "SELL", "2026-04-28T04:04:31+00:00",    5.9, "winner_snipe_direct"),
    ("12692", "USD_JPY", "SELL", "2026-04-28T05:00:01+00:00",  -31.8, "loser_snipe_direct"),
    ("12702", "USD_CHF", "SELL", "2026-04-28T05:38:47+00:00",   -7.9, "loser_snipe_direct"),
    ("12714", "AUD_USD", "SELL", "2026-04-28T06:49:26+00:00",    2.5, "winner_snipe_direct"),
    ("12726", "USD_CHF", "SELL", "2026-04-28T07:16:48+00:00",    0.8, "winner_snipe_direct"),
    ("12744", "USD_CHF", "SELL", "2026-04-28T09:00:08+00:00",  -19.9, "loser_snipe_direct"),
    ("12754", "EUR_USD", "BUY", "2026-04-28T09:00:57+00:00",  -19.6, "loser_snipe_direct"),
    ("12764", "GBP_JPY", "BUY", "2026-04-28T09:19:36+00:00",    0.6, "winner_snipe_direct"),
    ("12834", "GBP_USD", "SELL", "2026-04-28T11:31:30+00:00",    6.7, "winner_snipe_direct"),
    ("12856", "AUD_USD", "SELL", "2026-04-28T11:39:16+00:00",  -24.2, "loser_snipe_direct"),
    ("12870", "GBP_USD", "SELL", "2026-04-28T12:39:18+00:00",  -38.7, "loser_snipe_direct"),
    ("12902", "USD_CHF", "SELL", "2026-04-28T15:23:26+00:00",    2.4, "winner_snipe_direct"),
    ("12920", "EUR_CHF", "SELL", "2026-04-28T17:31:30+00:00",   -2.3, "loser_snipe_direct"),
    ("12938", "AUD_USD", "SELL", "2026-04-29T03:30:20+00:00",    2.6, "winner_snipe_direct"),
    ("12956", "GBP_USD", "SELL", "2026-04-29T06:14:50+00:00",    2.3, "winner_snipe_direct"),
    ("12978", "EUR_USD", "SELL", "2026-04-29T07:01:13+00:00",    0.8, "winner_snipe_direct"),
    ("12992", "AUD_USD", "SELL", "2026-04-29T08:50:29+00:00",    3.9, "winner_snipe_direct"),
    ("13012", "GBP_USD", "SELL", "2026-04-29T13:28:56+00:00",    3.6, "winner_snipe_direct"),
    ("13138", "AUD_JPY", "SELL", "2026-04-29T18:49:36+00:00",  -44.5, "loser_scout"),
    ("13254", "AUD_USD", "SELL", "2026-04-30T04:45:38+00:00",    2.9, "winner_snipe_direct"),
    ("13270", "AUD_USD", "SELL", "2026-04-30T06:12:29+00:00",  -26.8, "loser_snipe_direct"),
    ("13284", "USD_CAD", "SELL", "2026-04-30T09:22:28+00:00",    4.3, "winner_snipe_direct"),
    ("13300", "USD_CAD", "SELL", "2026-04-30T09:47:26+00:00",    1.0, "winner_snipe_direct"),
    ("13310", "AUD_JPY", "SELL", "2026-04-30T09:49:57+00:00",   71.9, "winner_scout"),
    ("13322", "GBP_JPY", "SELL", "2026-04-30T10:15:26+00:00",   19.2, "winner_snipe_direct"),
    ("13362", "AUD_JPY", "SELL", "2026-04-30T10:50:05+00:00",    8.2, "winner_scout"),
    ("13396", "EUR_CHF", "SELL", "2026-04-30T13:48:54+00:00",   17.9, "winner_scout"),
    ("13424", "USD_CAD", "SELL", "2026-04-30T15:45:49+00:00",    4.1, "winner_scout"),
    ("13452", "EUR_AUD", "SELL", "2026-05-01T16:34:10+00:00",    7.1, "winner_scout"),
    ("13486", "EUR_USD", "SELL", "2026-05-04T10:10:27+00:00",    1.0, "winner_snipe_direct"),
    ("13496", "NZD_USD", "SELL", "2026-05-04T10:15:40+00:00",  -13.2, "loser_snipe_direct"),
    ("13514", "EUR_USD", "SELL", "2026-05-04T11:06:15+00:00",    3.5, "winner_snipe_direct"),
    ("13536", "NZD_USD", "SELL", "2026-05-04T15:48:26+00:00",    6.8, "winner_snipe_direct"),
    ("13546", "EUR_USD", "SELL", "2026-05-04T16:04:00+00:00",    3.9, "winner_snipe_direct"),
    ("13556", "AUD_JPY", "SELL", "2026-05-04T16:08:19+00:00",  -12.3, "loser_snipe_direct"),
    ("13578", "AUD_USD", "SELL", "2026-05-04T16:51:45+00:00",    3.5, "winner_scout"),
    ("13607", "USD_CHF", "SELL", "2026-05-05T23:01:23+00:00",    1.0, "winner_snipe_direct"),
    ("13621", "GBP_USD", "BUY", "2026-05-05T23:51:09+00:00",    6.2, "winner_scout"),
    ("13627", "USD_CHF", "SELL", "2026-05-06T00:30:36+00:00",    3.0, "winner_snipe_direct"),
    ("13647", "USD_CHF", "SELL", "2026-05-06T01:08:08+00:00",    1.0, "winner_snipe_direct"),
    ("13665", "USD_CAD", "SELL", "2026-05-06T02:09:42+00:00",    4.6, "winner_scout"),
    ("13681", "USD_CHF", "SELL", "2026-05-06T11:08:42+00:00",  -11.1, "loser_scout"),
    ("13691", "AUD_JPY", "SELL", "2026-05-07T01:52:21+00:00",  -12.3, "loser_snipe_direct"),
    ("13705", "EUR_USD", "BUY", "2026-05-07T10:17:52+00:00",  -10.2, "loser_scout"),
    ("13713", "NZD_USD", "BUY", "2026-05-07T10:28:41+00:00",  -16.0, "loser_scout"),
    ("13727", "AUD_USD", "SELL", "2026-05-07T21:21:27+00:00",  -30.4, "loser_scout"),
    ("13733", "NZD_USD", "SELL", "2026-05-07T21:57:30+00:00",    2.4, "winner_snipe_direct"),
    ("13743", "AUD_JPY", "SELL", "2026-05-07T22:04:25+00:00",  -26.7, "loser_scout"),
    ("13765", "GBP_JPY", "BUY", "2026-05-08T07:10:15+00:00",   29.3, "winner_scout"),
    ("13809", "GBP_USD", "BUY", "2026-05-08T09:36:34+00:00",   -5.1, "loser_scout"),
    ("13817", "EUR_JPY", "BUY", "2026-05-08T10:02:34+00:00",    5.1, "winner_scout"),
    ("13827", "EUR_USD", "BUY", "2026-05-08T10:17:53+00:00",    4.7, "winner_scout"),
    ("13843", "AUD_JPY", "BUY", "2026-05-08T11:17:30+00:00",   -7.7, "loser_scout"),
    ("13901", "USD_CAD", "BUY", "2026-05-08T14:11:23+00:00",  -22.5, "loser_scout"),
    ("13913", "EUR_GBP", "SELL", "2026-05-08T15:23:19+00:00",  -33.2, "loser_snipe_direct"),
    ("13950", "AUD_JPY", "BUY", "2026-05-11T02:31:15+00:00",    5.6, "winner_scout"),
    ("13964", "USD_JPY", "BUY", "2026-05-11T03:34:40+00:00",   -1.9, "loser_scout"),
    ("13976", "NZD_USD", "SELL", "2026-05-11T04:48:10+00:00",  -15.2, "loser_snipe_direct"),
    ("14056", "EUR_JPY", "BUY", "2026-05-11T08:20:43+00:00",    8.0, "winner_scout"),
    ("14062", "GBP_JPY", "BUY", "2026-05-11T08:22:38+00:00",   -3.5, "loser_scout"),
    ("14070", "AUD_JPY", "BUY", "2026-05-11T08:48:00+00:00",    4.1, "winner_scout"),
    ("14088", "EUR_CHF", "BUY", "2026-05-11T09:32:58+00:00",  -13.9, "loser_scout"),
    ("14128", "AUD_USD", "BUY", "2026-05-11T13:17:42+00:00",   -3.6, "loser_scout"),
    ("14137", "EUR_USD", "BUY", "2026-05-11T14:19:30+00:00",   -1.4, "loser_scout"),
    ("14143", "EUR_JPY", "BUY", "2026-05-11T14:36:16+00:00",    3.9, "winner_scout"),
    ("14149", "EUR_AUD", "SELL", "2026-05-11T14:41:36+00:00",    1.3, "winner_snipe_direct"),
    ("14167", "AUD_JPY", "BUY", "2026-05-11T15:02:32+00:00",    3.9, "winner_scout"),
    ("14173", "EUR_AUD", "SELL", "2026-05-11T15:08:15+00:00",    2.2, "winner_snipe_direct"),
    ("14183", "NZD_USD", "BUY", "2026-05-11T15:08:39+00:00",   -4.7, "loser_scout"),
    ("14249", "GBP_JPY", "BUY", "2026-05-11T17:21:58+00:00",  -48.9, "loser_scout"),
    ("14281", "EUR_JPY", "BUY", "2026-05-11T19:38:35+00:00",   -8.0, "loser_scout"),
    ("14291", "EUR_CHF", "BUY", "2026-05-11T20:20:09+00:00",   -9.3, "loser_scout"),
    ("14312", "EUR_JPY", "BUY", "2026-05-11T21:21:35+00:00",   -1.6, "loser_scout"),
    ("14333", "USD_JPY", "BUY", "2026-05-12T00:58:20+00:00",   13.3, "winner_scout"),
    ("14355", "USD_JPY", "BUY", "2026-05-12T01:29:29+00:00",    6.2, "winner_scout"),
    ("14363", "GBP_USD", "SELL", "2026-05-12T02:30:14+00:00",    2.9, "winner_snipe_direct"),
    ("14405", "USD_JPY", "BUY", "2026-05-12T03:27:59+00:00",    4.0, "winner_scout"),
    ("14431", "AUD_JPY", "BUY", "2026-05-12T05:02:41+00:00",  -22.1, "loser_scout"),
    ("14443", "GBP_USD", "SELL", "2026-05-12T06:15:21+00:00",    5.0, "winner_snipe_direct"),
    ("14459", "GBP_JPY", "SELL", "2026-05-12T07:02:48+00:00",   11.2, "winner_snipe_direct"),
    ("14475", "EUR_USD", "SELL", "2026-05-12T07:55:23+00:00",    5.0, "winner_snipe_direct"),
    ("14485", "EUR_AUD", "BUY", "2026-05-12T08:02:49+00:00",  -27.2, "loser_scout"),
    ("14493", "EUR_GBP", "BUY", "2026-05-12T08:21:10+00:00",  -21.9, "loser_scout"),
    ("14499", "USD_CAD", "BUY", "2026-05-12T08:28:14+00:00",    5.7, "winner_scout"),
    ("14513", "USD_CHF", "BUY", "2026-05-12T08:53:08+00:00",    4.4, "winner_scout"),
    ("14519", "EUR_USD", "SELL", "2026-05-12T08:55:23+00:00",    2.8, "winner_snipe_direct"),
    ("14603", "EUR_CHF", "BUY", "2026-05-12T11:49:48+00:00",   -6.7, "loser_scout"),
    ("14609", "EUR_JPY", "SELL", "2026-05-12T14:05:21+00:00",  -23.8, "loser_snipe_direct"),
    ("14621", "USD_CHF", "BUY", "2026-05-12T14:39:19+00:00",  -18.8, "loser_scout"),
    ("14645", "NZD_USD", "SELL", "2026-05-12T15:25:24+00:00",  -20.6, "loser_snipe_direct"),
    ("14699", "EUR_AUD", "SELL", "2026-05-12T19:27:38+00:00",    2.9, "winner_snipe_direct"),
    ("14715", "EUR_AUD", "SELL", "2026-05-12T20:12:02+00:00",    1.6, "winner_snipe_direct"),
    ("14742", "USD_JPY", "BUY", "2026-05-13T00:36:11+00:00",  -13.1, "loser_scout"),
    ("14762", "EUR_AUD", "SELL", "2026-05-13T02:18:20+00:00",    3.4, "winner_snipe_direct"),
    ("14772", "AUD_JPY", "BUY", "2026-05-13T02:32:48+00:00",  -19.1, "loser_scout"),
    ("14778", "NZD_USD", "BUY", "2026-05-13T02:34:22+00:00",  -12.3, "loser_scout"),
    ("14790", "NZD_USD", "SELL", "2026-05-13T04:19:13+00:00",    0.9, "winner_snipe_direct"),
    ("14806", "NZD_USD", "SELL", "2026-05-13T05:03:29+00:00",    1.0, "winner_snipe_direct"),
    ("14828", "EUR_USD", "SELL", "2026-05-13T06:23:39+00:00",    2.7, "winner_snipe_direct"),
    ("14838", "EUR_AUD", "SELL", "2026-05-13T06:33:24+00:00",    3.2, "winner_snipe_direct"),
    ("14866", "EUR_JPY", "SELL", "2026-05-13T07:03:22+00:00",    2.0, "winner_snipe_direct"),
    ("14882", "EUR_CHF", "SELL", "2026-05-13T07:58:25+00:00",  -13.5, "loser_snipe_direct"),
    ("14892", "EUR_GBP", "SELL", "2026-05-13T08:03:28+00:00",    0.9, "winner_snipe_direct"),
    ("14906", "EUR_JPY", "SELL", "2026-05-13T08:07:57+00:00",  -22.7, "loser_scout"),
    ("14920", "EUR_USD", "SELL", "2026-05-13T08:42:01+00:00",    6.9, "winner_scout"),
    ("14932", "USD_CHF", "BUY", "2026-05-13T08:53:05+00:00",    1.7, "winner_scout"),
    ("14960", "EUR_AUD", "SELL", "2026-05-13T09:20:24+00:00",    3.5, "winner_scout"),
    ("14992", "EUR_USD", "SELL", "2026-05-13T09:37:08+00:00",  -18.1, "loser_snipe_direct"),
    ("15054", "EUR_AUD", "SELL", "2026-05-13T10:50:02+00:00",    5.1, "winner_scout"),
    ("15179", "GBP_JPY", "SELL", "2026-05-13T13:23:46+00:00",  -25.7, "loser_scout"),
    ("15205", "USD_CHF", "BUY", "2026-05-13T15:20:19+00:00",  -10.9, "loser_scout"),
    ("15211", "EUR_AUD", "SELL", "2026-05-13T15:33:26+00:00",    3.5, "winner_scout"),
    ("15227", "EUR_AUD", "SELL", "2026-05-13T16:48:31+00:00",  -40.7, "loser_scout"),
    ("15233", "EUR_CHF", "SELL", "2026-05-13T19:46:13+00:00",   -6.2, "loser_snipe_direct"),
    ("15250", "NZD_USD", "SELL", "2026-05-14T00:04:24+00:00",  -11.9, "loser_snipe_direct"),
    ("15272", "EUR_USD", "SELL", "2026-05-14T03:09:19+00:00",    0.8, "winner_snipe_direct"),
    ("15289", "EUR_CHF", "SELL", "2026-05-14T06:49:24+00:00",   -7.5, "loser_snipe_direct"),
    ("15299", "GBP_USD", "SELL", "2026-05-14T07:33:11+00:00",    2.4, "winner_snipe_direct"),
    ("15317", "EUR_USD", "SELL", "2026-05-14T08:04:24+00:00",    2.6, "winner_snipe_direct"),
    ("15327", "AUD_USD", "SELL", "2026-05-14T10:14:26+00:00",    3.5, "winner_snipe_direct"),
    ("15343", "AUD_USD", "SELL", "2026-05-14T10:44:29+00:00",    2.6, "winner_snipe_direct"),
    ("15353", "EUR_CHF", "SELL", "2026-05-14T11:33:21+00:00",    3.6, "winner_scout"),
    ("15365", "EUR_USD", "SELL", "2026-05-14T11:41:05+00:00",    2.3, "winner_snipe_direct"),
    ("15375", "EUR_JPY", "SELL", "2026-05-14T11:51:27+00:00",    1.0, "winner_snipe_direct"),
    ("15385", "NZD_USD", "SELL", "2026-05-14T12:01:02+00:00",    0.6, "winner_snipe_direct"),
    ("15397", "USD_CAD", "BUY", "2026-05-14T12:24:54+00:00",    3.7, "winner_scout"),
    ("15419", "EUR_JPY", "SELL", "2026-05-14T12:46:09+00:00",    2.1, "winner_snipe_direct"),
    ("15439", "EUR_GBP", "SELL", "2026-05-14T12:57:36+00:00",  -13.5, "loser_snipe_direct"),
    ("15457", "NZD_USD", "SELL", "2026-05-14T13:06:09+00:00",    1.0, "winner_snipe_direct"),
    ("15477", "USD_JPY", "BUY", "2026-05-14T13:26:19+00:00",    6.3, "winner_scout"),
    ("15487", "AUD_USD", "SELL", "2026-05-14T13:33:23+00:00",    4.2, "winner_scout"),
    ("15499", "EUR_JPY", "SELL", "2026-05-14T13:37:49+00:00",   36.5, "winner_scout"),
    ("15509", "EUR_JPY", "SELL", "2026-05-14T13:47:39+00:00",  -42.2, "loser_scout"),
    ("15533", "EUR_CHF", "SELL", "2026-05-14T14:17:59+00:00",   -1.3, "loser_scout"),
    ("15577", "EUR_USD", "SELL", "2026-05-14T15:36:33+00:00",    4.0, "winner_scout"),
    ("15615", "EUR_GBP", "BUY", "2026-05-14T17:17:52+00:00",    5.3, "winner_scout"),
    ("15621", "USD_CHF", "BUY", "2026-05-14T17:23:15+00:00",    0.3, "winner_scout"),
    ("15641", "EUR_GBP", "BUY", "2026-05-14T17:42:56+00:00",    5.0, "winner_scout"),
    ("15647", "USD_JPY", "BUY", "2026-05-14T17:50:41+00:00",    8.9, "winner_scout"),
    ("15661", "EUR_GBP", "BUY", "2026-05-14T18:35:31+00:00",   -8.2, "loser_scout"),
    ("15677", "GBP_USD", "SELL", "2026-05-14T19:19:57+00:00",    7.1, "winner_scout"),
    ("15702", "EUR_AUD", "SELL", "2026-05-14T23:20:10+00:00",    0.2, "winner_snipe_direct"),
    ("15720", "EUR_AUD", "SELL", "2026-05-14T23:29:46+00:00",   -8.6, "loser_snipe_direct"),
    ("15740", "NZD_USD", "SELL", "2026-05-15T00:38:40+00:00",    4.5, "winner_scout"),
    ("15746", "AUD_USD", "SELL", "2026-05-15T00:47:31+00:00",    4.3, "winner_scout"),
    ("15762", "NZD_USD", "SELL", "2026-05-15T00:59:02+00:00",    4.9, "winner_scout"),
    ("15776", "NZD_USD", "SELL", "2026-05-15T01:50:20+00:00",    3.7, "winner_scout"),
    ("15782", "EUR_JPY", "SELL", "2026-05-15T01:54:36+00:00",  -12.5, "loser_snipe_direct"),
    ("15794", "AUD_JPY", "SELL", "2026-05-15T02:00:03+00:00",    3.7, "winner_snipe_direct"),
    ("15808", "AUD_USD", "SELL", "2026-05-15T02:04:52+00:00",    3.9, "winner_scout"),
    ("15820", "USD_JPY", "BUY", "2026-05-15T02:40:21+00:00",    5.6, "winner_scout"),
    ("15830", "USD_CAD", "BUY", "2026-05-15T03:08:51+00:00",    3.8, "winner_scout"),
    ("15878", "GBP_USD", "SELL", "2026-05-15T06:00:16+00:00",    3.8, "winner_snipe_direct"),
    ("15888", "EUR_USD", "SELL", "2026-05-15T06:05:40+00:00",    4.2, "winner_snipe_direct"),
    ("15896", "AUD_JPY", "SELL", "2026-05-15T06:17:40+00:00",   10.0, "winner_scout"),
    ("15910", "GBP_USD", "SELL", "2026-05-15T06:29:44+00:00",  -31.0, "loser_snipe_direct"),
    ("15920", "AUD_USD", "SELL", "2026-05-15T06:32:47+00:00",    4.4, "winner_scout"),
    ("15924", "EUR_CHF", "SELL", "2026-05-15T06:32:53+00:00",    0.2, "winner_snipe_direct"),
    ("15972", "EUR_AUD", "BUY", "2026-05-15T06:51:17+00:00",  -18.5, "loser_scout"),
    ("16002", "EUR_USD", "SELL", "2026-05-15T07:04:49+00:00",    3.8, "winner_snipe_direct"),
    ("16014", "EUR_JPY", "SELL", "2026-05-15T07:09:55+00:00",    0.7, "winner_snipe_direct"),
    ("16028", "EUR_CHF", "SELL", "2026-05-15T07:24:48+00:00",    0.6, "winner_snipe_direct"),
    ("16066", "EUR_JPY", "SELL", "2026-05-15T07:58:38+00:00",    3.7, "winner_snipe_direct"),
    ("16104", "EUR_CHF", "SELL", "2026-05-15T08:32:38+00:00",   -3.5, "loser_scout"),
    ("16116", "USD_CHF", "BUY", "2026-05-15T09:53:36+00:00",  -17.8, "loser_scout"),
    ("16130", "EUR_USD", "SELL", "2026-05-15T12:39:20+00:00",   -9.1, "loser_snipe_direct"),
    ("16140", "USD_CAD", "BUY", "2026-05-15T13:40:49+00:00",   -5.1, "loser_scout"),
    ("16144", "USD_JPY", "BUY", "2026-05-15T13:42:22+00:00",    6.7, "winner_scout"),
    ("16158", "EUR_USD", "SELL", "2026-05-15T13:56:43+00:00",   -7.6, "loser_scout"),
    ("16162", "USD_CHF", "BUY", "2026-05-15T14:29:25+00:00",    3.5, "winner_scout"),
    ("16180", "GBP_JPY", "SELL", "2026-05-15T16:21:26+00:00",  -18.9, "loser_snipe_direct"),
]


def load_chart_b64(p):
    if not p or not os.path.exists(p):
        return None, "image/png"
    raw = open(p, "rb").read()
    media = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return base64.b64encode(raw).decode(), media


def build_task_text(pair, direction, indicator_block, pattern_section):
    pd = pair.replace("_", "/")
    pattern_part = f"\n\n{pattern_section}" if pattern_section else ""
    base = (
        f"M15 chart — {pd}. Scout identified a {direction} setup. "
        f"Read the chart fresh and form YOUR OWN thesis from the structure you see.\n\n"
        f"Return ONLY a ```json code block with: verdict (TRADE_NOW/WATCH/SKIP), "
        f"direction (BUY/SELL), confidence (0-10 INTEGER), reasoning (start with CHART READ:), "
        f"re_entry_conditions (list of {{field, op, value, reason}} dicts), "
        f"snipe_entry_zone, snipe_invalidation, snipe_target.\n\n"
        f"After analyzing the chart, respond with ONLY a ```json code block. "
        f"No prose outside the JSON."
    )
    return f"{indicator_block}{pattern_part}\n\n---\n\n{base}"


def call_35b(system_prompt, task_text, chart_b64, chart_media="image/png"):
    content = []
    if chart_b64:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{chart_media};base64,{chart_b64}"}})
    content.append({"type": "text", "text": task_text})
    payload = json.dumps({
        "model": LOCAL_MODEL_NAME,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": content}],
        "temperature": 0, "max_tokens": 2500, "stream": False,
    }).encode()
    req = urllib.request.Request(LOCAL_ENDPOINT, data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    out = data["choices"][0]["message"].get("content", "") or ""
    return re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL).strip()


def parse_verdict(raw):
    cleaned = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned)
    js = m.group(1) if m else None
    if not js:
        i = cleaned.find("{")
        if i == -1:
            return {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": ""}
        depth, end = 0, -1
        for k, ch in enumerate(cleaned[i:]):
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + k + 1; break
        js = cleaned[i:end] if end > 0 else None
    if not js:
        return {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": ""}
    try:
        d = json.loads(js)
        return {"verdict": d.get("verdict", "UNKNOWN"),
                "direction": d.get("direction"),
                "confidence": d.get("confidence"),
                "reasoning": str(d.get("reasoning", ""))[:300]}
    except json.JSONDecodeError:
        return {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": ""}


def bucket(category, verdict, direction, trade_dir):
    v = (verdict or "").upper(); d = (direction or "").upper(); td = trade_dir.upper()
    # winner-like categories: TRADE_NOW = IDEAL, WATCH = OK
    if category in ("winner", "winner_today_open"):
        if v == "TRADE_NOW" and d == td: return "IDEAL"
        if v == "WATCH": return "OK"
        return "BAD"
    # loser-like categories: SKIP = IDEAL, WATCH = OK, TRADE_NOW = BAD
    else:
        if v == "SKIP": return "IDEAL"
        if v == "WATCH": return "OK"
        if v == "TRADE_NOW": return "BAD"
        return "BAD"


def main():
    log_lines = []
    def log(msg):
        print(msg, flush=True); log_lines.append(msg)

    log("=" * 70)
    log("ITER 20a — scout-history threshold n≥3 → n≥5, structural-primary guardrail")
    log("=" * 70)
    log(f"Prompt: {PROMPT_PATH}")
    log(f"Detectors enabled: {DETECTOR_ENABLED}")
    log(f"Chart source: {PATTERN_CHART_DIR} (pattern overlay)")
    system_prompt = Path(PROMPT_PATH).read_text().strip()
    indicator_blocks = json.load(open(INDICATOR_BLOCKS_JSON))
    log(f"System prompt size: {len(system_prompt)} chars")
    log("")

    os.makedirs(PATTERN_CHART_DIR, exist_ok=True)
    results = []
    t0 = time.time()
    for trade_id, pair, direction, entry_iso, actual_pips, category in COHORT:
        log(f"\n[{trade_id}] {pair} {direction} | actual: {actual_pips:+}p ({category})")
        ind = indicator_blocks.get(trade_id)
        if not ind or "block_text" not in ind:
            log(f"  ERROR: no indicator block for {trade_id}"); continue
        chart_out = f"{PATTERN_CHART_DIR}/{trade_id}_{pair}_pattern.png"
        chart_path, fires = regenerate_chart_with_patterns(pair, entry_iso, chart_out)
        if not chart_path:
            log(f"  ERROR: pattern chart regen failed for {trade_id}")
            continue
        pattern_section = build_pattern_section(fires)
        chart_b64, chart_media = load_chart_b64(chart_path)
        sess = "BLOCKED" if ind.get("session_blocked") else "OPEN"
        pattern_names = [f["name"] for f in fires]
        scout = ind.get("scout_history") or {}
        scout_n = scout.get("trade_count", 0)
        scout_wr = scout.get("win_rate")
        log(f"  Chart: {chart_path} ({os.path.getsize(chart_path)//1024}KB) | session={sess}")
        log(f"  Patterns: {pattern_names if pattern_names else 'none'}")
        log(f"  Scout: n={scout_n} WR={scout_wr}%")
        log(f"  Indicator: phase={ind.get('phase')} fan={ind.get('fan',{}).get('fan_direction')} "
            f"{ind.get('fan',{}).get('fan_state')}")
        task = build_task_text(pair, direction, ind["block_text"], pattern_section)
        try:
            tc = time.time()
            raw = call_35b(system_prompt, task, chart_b64, chart_media)
            dt = time.time() - tc
        except Exception as e:
            log(f"  ERROR calling 35B: {e}")
            results.append({"trade_id": trade_id, "pair": pair, "direction": direction,
                            "actual_pips": actual_pips, "category": category,
                            "verdict": "ERROR", "verdict_direction": None, "confidence": None,
                            "reasoning_snippet": str(e), "bucket": "BAD",
                            "patterns": pattern_names})
            continue
        parsed = parse_verdict(raw)
        v = parsed.get("verdict"); vd = parsed.get("direction"); cf = parsed.get("confidence")
        rs = (parsed.get("reasoning") or "")[:300]
        bk = bucket(category, v, vd, direction)
        results.append({"trade_id": trade_id, "pair": pair, "direction": direction,
                        "actual_pips": actual_pips, "category": category,
                        "verdict": v, "verdict_direction": vd, "confidence": cf,
                        "reasoning_snippet": rs, "bucket": bk,
                        "session_blocked": ind.get("session_blocked", False),
                        "patterns": pattern_names,
                        "scout_n": scout_n, "scout_wr": scout_wr,
                        "elapsed_s": round(dt, 1)})
        log(f"  → {v} {vd} conf={cf} [{bk}] ({dt:.1f}s)")
        Path(OUT_RESULTS).write_text(json.dumps(results, indent=2))
        Path(OUT_LOG).write_text("\n".join(log_lines))

    elapsed_min = (time.time() - t0) / 60
    log("")
    log("=" * 70)
    ideal = sum(1 for r in results if r["bucket"] == "IDEAL")
    ok = sum(1 for r in results if r["bucket"] == "OK")
    bad = sum(1 for r in results if r["bucket"] == "BAD")
    raw_pips = sum(r["actual_pips"] for r in results if r["bucket"] == "IDEAL")
    log(f"ITER 20a SUMMARY: IDEAL={ideal}  OK={ok}  BAD={bad}  Acceptable={ideal+ok}/19")
    log(f"  Raw pips (IDEAL only): {raw_pips:+.1f}p")
    log(f"  Baseline iter 16 v2: 10 IDEAL + 5 OK + 4 BAD = 15/19 (raw +18.9p)")
    log(f"  Iter 18b session:     8 IDEAL + 9 OK + 2 BAD = 17/19 (raw -2.1p)")
    log(f"  Iter 19 patterns:     9 IDEAL + 9 OK + 1 BAD = 18/19 (raw +5.6p)")
    log(f"  Iter 20 filters:      7 IDEAL +11 OK + 1 BAD = 18/19 (raw +30.1p)")
    log(f"Elapsed: {elapsed_min:.1f} min")
    log("=" * 70)
    Path(OUT_RESULTS).write_text(json.dumps(results, indent=2))
    Path(OUT_LOG).write_text("\n".join(log_lines))


if __name__ == "__main__":
    main()
