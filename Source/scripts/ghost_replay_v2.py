"""ghost_replay_v2.py — live-faithful validator replay.

Composes task_text exactly the way trading_cycle.py:6920-7004 local-35B path does,
by calling the SAME live functions with as-of historical timestamps. No live code
edits — this script orchestrates the live section-builders for a chosen cohort.

Sections built (matching _local_keep filter at trading_cycle.py:6986):
  - Scout Evidence        ← built from flight_log.scout_alert at entry_iso
  - Indicator Data — Raw  ← from /tmp/cohort_indicator_blocks.json (pre-computed)
  - Detected Patterns     ← detect_patterns_for_validator on entry-time candles
  - Scout History         ← fetch_as_of_history(setup, pair, entry_iso)
  - Session Gate          ← _compute_session_window(pair, now_utc=entry_iso)

Chart: reuses iter36 regenerated charts (deterministic given pair + entry_iso).

Run on 1 trade for inspection:
    python3 scripts/ghost_replay_v2.py --single 15499

Run on full 42 cohort:
    python3 scripts/ghost_replay_v2.py --cohort 42

Run on full 30d (requires per-trade indicator blocks built):
    python3 scripts/ghost_replay_v2.py --cohort 30d
"""

from __future__ import annotations
import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from scripts.build_scout_history import fetch_as_of_history, format_scout_section
from scripts.pattern_detectors import detect_patterns_for_validator
from scripts.pattern_library_quotes import build_pattern_section
from scripts.oanda_chart_pattern_regen_iter20g import regenerate_chart_with_patterns_and_exits

# Import live session-window function — no modifications, just call it.
from agents.trading_cycle import _compute_session_window

PROMPT_PATH = "/tmp/prompt_variants/iter39.md"
INDICATOR_BLOCKS_JSON = "/tmp/cohort_indicator_blocks.json"
LOCAL_ENDPOINT = "http://127.0.0.1:11502/v1/chat/completions"
LOCAL_MODEL_NAME = "mlx-community/Qwen3.5-35B-A3B-4bit"

FLIGHT_DB = os.path.expanduser("~/Jarvis/Forex Trading Team/Source/flight_recorder.db")
TRADING_DB = os.path.expanduser("~/Jarvis/Database/v2/trading_forex.db")

PATTERN_CHART_DIR = "/tmp/replay_charts_iter36_full"  # reuse iter36 charts
OUT_RESULTS_DIR = "/tmp/ghost_v2"
os.makedirs(OUT_RESULTS_DIR, exist_ok=True)


# 42-trade cohort — same trade IDs we've been iterating against.
COHORT_42 = [
    ("13310", "AUD_JPY", "SELL", "2026-04-30T09:49:57+00:00",  71.9, "BIG_WIN_71p"),
    ("15499", "EUR_JPY", "SELL", "2026-05-14T13:37:49+00:00",  36.5, "BIG_WIN_36p"),
    ("13765", "GBP_JPY", "BUY",  "2026-05-08T07:10:15+00:00",  29.3, "BIG_WIN_29p"),
    ("15205", "USD_CHF", "BUY",  "2026-05-13T15:15:55+00:00",  10.0, "MID_WIN_10p"),
    ("15647", "USD_JPY", "BUY",  "2026-05-14T17:50:41+00:00",   8.9, "WIN_safety_9p"),
    ("13727", "AUD_USD", "SELL", "2026-05-07T21:21:27+00:00", -30.4, "loser"),
    ("13743", "AUD_JPY", "SELL", "2026-05-07T22:04:25+00:00", -26.7, "loser"),
    ("14249", "GBP_JPY", "BUY",  "2026-05-11T17:21:00+00:00", -48.9, "loser_late"),
    ("14992", "EUR_USD", "SELL", "2026-05-13T09:37:06+00:00", -20.0, "loser_today_open"),
    ("15227", "EUR_AUD", "SELL", "2026-05-13T16:46:03+00:00", -25.0, "loser_today_open"),
    ("13138", "AUD_JPY", "SELL", "2026-04-29T18:49:36+00:00", -44.5, "loser"),
    ("13362", "AUD_JPY", "SELL", "2026-04-30T10:50:05+00:00",   8.2, "winner"),
    ("13396", "EUR_CHF", "SELL", "2026-04-30T13:48:54+00:00",  17.9, "winner"),
    ("13424", "USD_CAD", "SELL", "2026-04-30T15:45:49+00:00",   4.1, "winner"),
    ("13452", "EUR_AUD", "SELL", "2026-05-01T16:34:10+00:00",   7.1, "winner"),
    ("13578", "AUD_USD", "SELL", "2026-05-04T16:51:45+00:00",   3.5, "winner"),
    ("13621", "GBP_USD", "BUY",  "2026-05-05T23:51:09+00:00",   6.2, "winner"),
    ("13665", "USD_CAD", "SELL", "2026-05-06T02:09:42+00:00",   4.6, "winner"),
    ("13681", "USD_CHF", "SELL", "2026-05-06T11:08:42+00:00", -11.1, "loser"),
    ("13705", "EUR_USD", "BUY",  "2026-05-07T10:17:52+00:00", -10.2, "loser"),
    ("13713", "NZD_USD", "BUY",  "2026-05-07T10:28:41+00:00", -16.0, "loser"),
    ("13809", "GBP_USD", "BUY",  "2026-05-08T09:36:34+00:00",  -5.1, "loser"),
    ("13817", "EUR_JPY", "BUY",  "2026-05-08T10:02:34+00:00",   5.1, "winner"),
    ("13827", "EUR_USD", "BUY",  "2026-05-08T10:17:53+00:00",   4.7, "winner"),
    ("13843", "AUD_JPY", "BUY",  "2026-05-08T11:17:30+00:00",  -7.7, "loser"),
    ("13913", "EUR_GBP", "SELL", "2026-05-08T15:23:00+00:00", -33.2, "loser_late"),
    ("14088", "EUR_CHF", "BUY",  "2026-05-11T09:32:00+00:00", -13.9, "loser_late"),
    ("14431", "AUD_JPY", "BUY",  "2026-05-12T05:02:00+00:00", -22.1, "loser_late"),
    ("14485", "EUR_AUD", "BUY",  "2026-05-12T08:02:00+00:00", -27.2, "loser_late"),
    ("14882", "EUR_CHF", "SELL", "2026-05-13T07:58:23+00:00", -13.5, "loser_today"),
    ("14906", "EUR_JPY", "SELL", "2026-05-13T08:04:41+00:00", -22.7, "loser_today"),
    ("15179", "GBP_JPY", "SELL", "2026-05-13T13:20:15+00:00", -25.7, "loser_today"),
    ("15233", "EUR_CHF", "SELL", "2026-05-13T19:46:10+00:00",  -6.2, "loser_today"),
    ("15439", "EUR_GBP", "SELL", "2026-05-14T12:57:36+00:00", -13.5, "loser_new"),
    ("15509", "EUR_JPY", "SELL", "2026-05-14T13:47:39+00:00", -42.2, "loser_new"),
    ("16104", "EUR_CHF", "SELL", "2026-05-15T08:32:38+00:00",  -3.5, "loser_new"),
    ("16116", "USD_CHF", "BUY",  "2026-05-15T09:53:36+00:00", -17.8, "loser_new"),
    ("16130", "EUR_USD", "SELL", "2026-05-15T12:39:20+00:00",  -9.1, "loser_new"),
    ("16140", "USD_CAD", "BUY",  "2026-05-15T13:40:49+00:00",  -5.1, "loser_new"),
    ("16158", "EUR_USD", "SELL", "2026-05-15T13:56:43+00:00",  -7.6, "loser_new"),
    ("16162", "USD_CHF", "BUY",  "2026-05-15T14:29:25+00:00",   3.5, "winner_safety"),
    ("16180", "GBP_JPY", "SELL", "2026-05-15T16:21:26+00:00", -18.9, "loser_new"),
]

# 252-trade non-kronos cohort spanning last 30 days (2026-04-16 → 2026-05-15)
# Extracted from replay_iter31_30d.py — same trades the iter31 30d run used.
COHORT_30D = [

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


# ─────────────────────────────────────────────────────────────────────────────
# Live-section builders — call live functions with as-of historical timestamps.
# ─────────────────────────────────────────────────────────────────────────────

def build_scout_evidence_section(pair: str, entry_iso: str) -> str:
    """Reconstruct Scout Evidence section by pulling scout_alert from flight_log.

    Mirrors trading_cycle.py:6338-6353 _v4_scout_evidence builder. Pulls the
    most recent scout_alert for the pair within ±5 minutes of entry_iso.
    """
    entry_dt = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
    win_start = (entry_dt - timedelta(minutes=10)).isoformat()
    win_end = (entry_dt + timedelta(minutes=1)).isoformat()
    conn = sqlite3.connect(FLIGHT_DB, timeout=30)
    try:
        row = conn.execute(
            "SELECT timestamp, data FROM flight_log "
            "WHERE pair=? AND stage='scout_alert' AND timestamp BETWEEN ? AND ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (pair, win_start, win_end),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return f"### Scout Evidence\n(No scout_alert found for {pair} near {entry_iso})\n"
    scout_ts, data_json = row
    try:
        scout = json.loads(data_json)
    except Exception:
        return f"### Scout Evidence\n(Could not parse scout_alert data)\n"
    alert_type = scout.get("alert_type", "UNKNOWN")
    return (
        f"### Scout Evidence\n"
        f"⚠️ **Scout scanned at {scout_ts} — chart below is the current reality**\n"
        f"- Alert type: **{alert_type}**"
        + (" — All thesis conditions met" if alert_type == "CRITERIA_MET"
           else " — Extreme detected but thesis NOT yet confirmed" if alert_type == "EARLY_WARNING"
           else "")
        + f"\n- Direction: {scout.get('direction', 'N/A')} | "
        f"Fan: {scout.get('fan_direction', '?')} {scout.get('fan_state', '?')} | "
        f"RSI: {scout.get('rsi', 'N/A')} | Stoch K: {scout.get('stoch_k', 'N/A')} | "
        f"Tier1: {scout.get('tier1', False)}\n"
    )


def build_indicator_section(trade_id: str) -> str:
    """Pull pre-computed indicator block from /tmp/cohort_indicator_blocks.json."""
    blocks = json.load(open(INDICATOR_BLOCKS_JSON))
    block = blocks.get(trade_id, {})
    text = block.get("block_text", "")
    if not text:
        return "## Indicator Data — Raw\n(Missing indicator block for this trade)\n"
    return text


def build_session_gate_section(instrument: str, entry_iso: str) -> str:
    """Call live _compute_session_window with as-of historical time."""
    entry_dt = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
    sess = _compute_session_window(instrument, tc_get_fn=None, now_utc=entry_dt)
    line = f"Session gate: {sess['state']}"
    if sess.get("reason"):
        line = f"{line} — {sess['reason']}"
    if sess.get("owning_session"):
        line += f"\nOwning session: {sess['owning_session']}"
    if sess.get("next_open_utc"):
        line += f"\nNext owning-session open: {sess['next_open_utc']}"
    return line


def build_scout_history_section(setup_name: str, pair: str, entry_iso: str, direction: str) -> str:
    """Call live fetch_as_of_history + format_scout_section with as-of historical time."""
    if not setup_name:
        return ""
    history = fetch_as_of_history(setup_name, pair, entry_iso)
    return format_scout_section(history, direction)


def build_detected_patterns_section(pair: str, entry_iso: str, fan_direction: str = "mixed", phase: int = 0) -> str:
    """Run detect_patterns_for_validator on candles ending at entry_iso."""
    # Fetch candles ending at entry_iso. The chart regen tool already does this.
    # Cheapest path: regenerate chart, which returns the fires list.
    chart_out = f"{PATTERN_CHART_DIR}/{pair}_pattern_for_detection.png"
    try:
        chart_path, fires = regenerate_chart_with_patterns_and_exits(pair, entry_iso, chart_out)
    except Exception as e:
        return (f"## Detected Patterns On This Chart\n"
                f"Pattern detector failed: {e}. Read structure visually.\n")
    if not fires:
        return (
            "## Detected Patterns On This Chart\n"
            "No programmatic patterns detected on the most recent bars. "
            "(11 detectors checked: hammer/pin, bullish engulfing, bearish "
            "engulfing, morning/evening star, doji-at-extreme, ascending "
            "triangle, descending triangle, channel, BB-squeeze breakout, "
            "RSI/MACD divergence, plus mutual-exclusion + confirmation/"
            "invalidation filters.) Pattern-conflict veto does not apply — "
            "read structure visually from the chart.\n"
        )
    body = build_pattern_section(fires, body_only=True)
    return f"## Detected Patterns On This Chart\n{body}\n"


def build_preamble(pair: str, story_score: int = 0) -> str:
    """Mirror trading_cycle.py:6924-6935 local-35B preamble exactly."""
    pd = pair.replace("_", "/")
    return (
        f"M15 chart — {pd}. Read it fresh and form YOUR OWN "
        f"thesis from the structure you see (story_score={story_score} "
        f"is informational only, not a directive).\n\n"
        f"Return ONLY a ```json code block with: verdict (TRADE_NOW/WATCH/SKIP), "
        f"direction (BUY/SELL), confidence (0-10), reasoning (start with CHART READ:), "
        f"re_entry_conditions (list of {{field, op, value, reason}} dicts), "
        f"re_entry_direction, re_entry_setup, watch_trigger (SPECIFIC prices: "
        f"entry zone, invalidation, target), watch_for, snipe_entry_zone, "
        f"snipe_invalidation, snipe_target, estimated_candles_to_entry, "
        f"price_target_entry, watch_manifest (MANDATORY for WATCH).\n\n"
    )


def compose_task(trade_id: str, pair: str, direction: str, entry_iso: str) -> tuple[str, list[str]]:
    """Compose the full task_text matching live local-35B path order.

    Order (per trading_cycle.py local_keep filter + section append sequence):
      1. preamble
      2. Scout Evidence
      3. Indicator Data — Raw
      4. Detected Patterns On This Chart
      5. Scout History
      6. Session Gate
      7. JSON-only footer
    """
    blocks = json.load(open(INDICATOR_BLOCKS_JSON))
    block = blocks.get(trade_id, {})
    fan_direction = block.get("fan", {}).get("fan_direction", "mixed")
    phase = block.get("phase", 0)
    # Setup name for scout history — pull from scout alert
    entry_dt = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
    win_start = (entry_dt - timedelta(minutes=10)).isoformat()
    win_end = (entry_dt + timedelta(minutes=1)).isoformat()
    conn = sqlite3.connect(FLIGHT_DB, timeout=30)
    try:
        row = conn.execute(
            "SELECT data FROM flight_log "
            "WHERE pair=? AND stage='scout_alert' AND timestamp BETWEEN ? AND ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (pair, win_start, win_end),
        ).fetchone()
    finally:
        conn.close()
    setup_name = ""
    if row:
        try:
            sc = json.loads(row[0])
            setup_name = sc.get("setup_name") or sc.get("setup_id") or sc.get("alert_type") or ""
        except Exception:
            pass

    section_headings = []
    parts = [build_preamble(pair)]

    s = build_scout_evidence_section(pair, entry_iso)
    parts.append(s); section_headings.append("Scout Evidence")

    s = build_indicator_section(trade_id)
    parts.append(s); section_headings.append("Indicator Data")

    s = build_detected_patterns_section(pair, entry_iso, fan_direction=fan_direction, phase=phase)
    parts.append(s); section_headings.append("Detected Patterns")

    s = build_scout_history_section(setup_name, pair, entry_iso, direction)
    if s:
        parts.append("## Scout History\n" + s)
        section_headings.append("Scout History")

    s = build_session_gate_section(pair, entry_iso)
    parts.append("## Session Gate\n" + s)
    section_headings.append("Session Gate")

    parts.append(
        "\n---\n"
        "After using your tools and analyzing the chart, respond with ONLY a ```json code block. "
        "No prose outside the JSON."
    )

    return "\n\n".join(parts), section_headings


# ─────────────────────────────────────────────────────────────────────────────
# Chart load + 35B call (unchanged shape from prior replays)
# ─────────────────────────────────────────────────────────────────────────────

def load_chart_b64(p: str) -> tuple[str | None, str]:
    if not p or not os.path.exists(p):
        return None, "image/png"
    raw = open(p, "rb").read()
    media = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return base64.b64encode(raw).decode(), media


def call_35b(system_prompt: str, task_text: str, chart_b64: str, chart_media: str = "image/png") -> str:
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


def parse_verdict(raw: str) -> dict:
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
        return {
            "verdict": d.get("verdict", "UNKNOWN"),
            "direction": d.get("direction"),
            "confidence": d.get("confidence"),
            "reasoning": d.get("reasoning", "")[:500],
        }
    except Exception as e:
        return {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": str(e)[:200]}


def bucket(verdict: str, actual_pips: float, verdict_dir: str | None, trade_dir: str) -> str:
    """Tim's framework: TN-loser=BAD, SKIP-winner=BAD, wrong-dir-TN=BAD, else IDEAL/OK."""
    v = (verdict or "").upper()
    is_winner = actual_pips > 0
    if v == "TRADE_NOW":
        if verdict_dir and trade_dir and verdict_dir.upper() != trade_dir.upper():
            return "BAD"
        return "IDEAL" if is_winner else "BAD"
    if v == "SKIP":
        return "BAD" if is_winner else "IDEAL"
    return "OK"  # WATCH


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def run_one(trade: tuple, system_prompt: str, dry_run: bool = False) -> dict:
    trade_id, pair, direction, entry_iso, actual_pips, category = trade
    task_text, section_headings = compose_task(trade_id, pair, direction, entry_iso)
    chart_path = f"{PATTERN_CHART_DIR}/{trade_id}_{pair}_pattern.png"
    chart_b64, chart_media = load_chart_b64(chart_path)
    if not chart_b64:
        # Try to regenerate
        try:
            chart_out, _ = regenerate_chart_with_patterns_and_exits(pair, entry_iso, chart_path)
            chart_b64, chart_media = load_chart_b64(chart_out)
        except Exception as e:
            print(f"[{trade_id}] Chart hydration failed: {e}")

    result = {
        "trade_id": trade_id, "pair": pair, "direction": direction,
        "entry_iso": entry_iso, "actual_pips": actual_pips, "category": category,
        "section_headings": section_headings,
        "task_text_len": len(task_text),
        "chart_path": chart_path,
        "chart_present": bool(chart_b64),
    }
    if dry_run:
        result["task_text"] = task_text
        return result

    t0 = time.time()
    try:
        raw = call_35b(system_prompt, task_text, chart_b64, chart_media)
    except Exception as e:
        result.update(verdict="ERROR", reasoning_snippet=str(e), elapsed_s=time.time()-t0,
                      bucket="BAD")
        return result
    parsed = parse_verdict(raw)
    result.update(
        verdict=parsed["verdict"], verdict_direction=parsed["direction"],
        confidence=parsed["confidence"], reasoning_snippet=parsed["reasoning"][:300],
        elapsed_s=round(time.time()-t0, 1),
        bucket=bucket(parsed["verdict"], actual_pips, parsed["direction"], direction),
    )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--single", help="trade_id for single-trade dry-run inspection")
    parser.add_argument("--cohort", choices=["42","30d"], default=None)
    parser.add_argument("--losers-only", action="store_true", help="filter cohort to actual losers (pnl<0)")
    parser.add_argument("--winners-only", action="store_true", help="filter cohort to actual winners (pnl>0)")
    parser.add_argument("--out", default=None, help="results JSON path (default auto)")
    parser.add_argument("--prompt-path", default=None, help="override prompt path (default iter39.md)")
    parser.add_argument("--label", default="iter39_v2", help="output label for auto-named results")
    args = parser.parse_args()

    prompt_path = args.prompt_path or PROMPT_PATH
    system_prompt = Path(prompt_path).read_text().strip()
    print(f"System prompt: {prompt_path} ({len(system_prompt)} chars)")

    if args.single:
        trade = next((t for t in COHORT_42 if t[0] == args.single), None)
        if not trade:
            print(f"trade {args.single} not in cohort"); sys.exit(1)
        print(f"\n=== DRY-RUN single trade {args.single} ===")
        res = run_one(trade, system_prompt, dry_run=True)
        print(f"\nSection headings ({len(res['section_headings'])}): {res['section_headings']}")
        print(f"Task text length: {res['task_text_len']} chars")
        print(f"Chart present: {res['chart_present']} at {res['chart_path']}")
        print("\n=== FULL TASK TEXT (what would be sent to 35B) ===\n")
        print(res["task_text"])
        return

    if args.cohort == "42":
        out = args.out or f"{OUT_RESULTS_DIR}/{args.label}_42cohort_results.json"
        results = []
        t_total = time.time()
        for i, trade in enumerate(COHORT_42, 1):
            print(f"\n[{i}/{len(COHORT_42)}] {trade[0]} {trade[1]} {trade[2]} actual={trade[4]:+.1f}p ({trade[5]})")
            r = run_one(trade, system_prompt)
            tag = ""
            if r.get("verdict") == "TRADE_NOW":
                tag = " ✓WIN" if r["actual_pips"] > 0 else " ✗LOSER"
            print(f"  → {r.get('verdict')} {r.get('verdict_direction')} c{r.get('confidence')} "
                  f"[{r.get('bucket')}]{tag} ({r.get('elapsed_s')}s)")
            results.append(r)
            Path(out).write_text(json.dumps(results, indent=2))
        print(f"\n=== DONE in {(time.time()-t_total)/60:.1f} min ===")
        print(f"Results: {out}")
        # Summary
        from collections import Counter
        b = Counter(x.get("bucket","?") for x in results)
        v = Counter(x.get("verdict","?") for x in results)
        print(f"Verdicts: {dict(v)}")
        print(f"Buckets:  {dict(b)}")
        tn_winners = [x for x in results if x.get("verdict")=="TRADE_NOW" and x["actual_pips"]>0]
        tn_losers  = [x for x in results if x.get("verdict")=="TRADE_NOW" and x["actual_pips"]<=0]
        print(f"TRADE_NOWs: {len(tn_winners)+len(tn_losers)} ({len(tn_winners)} winners, {len(tn_losers)} losers)")
        print(f"TN-honored pips: {sum(x['actual_pips'] for x in tn_winners+tn_losers):+.1f}p")
        return

    if args.cohort == "30d":
        cohort = COHORT_30D
        suffix = "30d"
        if args.losers_only:
            cohort = [t for t in COHORT_30D if t[4] < 0]
            suffix = "30d_losers"
        elif args.winners_only:
            cohort = [t for t in COHORT_30D if t[4] > 0]
            suffix = "30d_winners"
        out = args.out or f"{OUT_RESULTS_DIR}/{args.label}_{suffix}_results.json"
        print(f"Cohort size: {len(cohort)} (filtered from {len(COHORT_30D)})")
        results = []
        t_total = time.time()
        for i, trade in enumerate(cohort, 1):
            print(f"\n[{i}/{len(cohort)}] {trade[0]} {trade[1]} {trade[2]} actual={trade[4]:+.1f}p ({trade[5]})", flush=True)
            r = run_one(trade, system_prompt)
            tag = ""
            if r.get("verdict") == "TRADE_NOW":
                tag = " ✓WIN" if r["actual_pips"] > 0 else " ✗LOSER"
            print(f"  → {r.get('verdict')} {r.get('verdict_direction')} c{r.get('confidence')} "
                  f"[{r.get('bucket')}]{tag} ({r.get('elapsed_s')}s)", flush=True)
            results.append(r)
            Path(out).write_text(json.dumps(results, indent=2))
        print(f"\n=== 30d DONE in {(time.time()-t_total)/60:.1f} min ===")
        print(f"Results: {out}")
        from collections import Counter
        b = Counter(x.get("bucket","?") for x in results)
        v = Counter(x.get("verdict","?") for x in results)
        tn_w = [x for x in results if x.get("verdict")=="TRADE_NOW" and x["actual_pips"]>0]
        tn_l = [x for x in results if x.get("verdict")=="TRADE_NOW" and x["actual_pips"]<=0]
        print(f"Verdicts: {dict(v)}")
        print(f"Buckets:  {dict(b)}")
        print(f"TN: {len(tn_w)+len(tn_l)} ({len(tn_w)} winners, {len(tn_l)} losers)")
        print(f"TN-honored pips: {sum(x['actual_pips'] for x in tn_w+tn_l):+.1f}p")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
