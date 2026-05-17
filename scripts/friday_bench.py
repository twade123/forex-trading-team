"""Friday-close 35B validator bench — durable cron entry point.

Validates the v1-canonical-plus shipped config (commit 6e96703e) against the
14 historical Opus-TRADE_NOW charts. Self-contained: reads the LIVE prompt
from the project, pulls candles via the project's existing helpers, POSTs to
the MLX 35B server, writes results to JSON + vault.

Run manually:
    source ~/myenv/bin/activate && python3 "Forex Trading Team/scripts/friday_bench.py"

Scheduled by user crontab (May 1, 2026 17:37 ET = Friday market close).
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path("<repo_root>")
sys.path.insert(0, str(BASE / "Source"))

from agents.wrappers import fetch_candles  # noqa: E402
from backtester.candle_patterns import detect_all_patterns, detect_support_resistance  # noqa: E402
import pandas as pd  # noqa: E402

DB = "~/Jarvis/Database/v2/trading_forex.db"
PROMPT_FILE = BASE / "Prompts/ghost_validator_v1.md"
TEACH_IMG = BASE / "Data/charts/teaching/tim_teach_eurchf_annotated_short_snipe.png"
TEACHING_DESC = (
    "REFERENCE TRADE — EUR/CHF SHORT SNIPE: Annotated chart showing EMA cross, "
    "EMA fan, Bollinger expansion, and short snipe entry. Full thesis with entry "
    "zone marked. Mirror this pattern (and its bullish counterpart) when reading "
    "the live chart below."
)
MLX_URL = "http://127.0.0.1:11502/v1/chat/completions"
MODEL = "mlx-community/Qwen3.5-35B-A3B-4bit"

IDS = [1735, 1730, 1729, 1685, 1651, 1615, 1505, 1499, 1489, 1463, 1438, 1422, 1419, 1602]

OUT_JSON = Path("/tmp") / f"friday_bench_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
LOG_PATH = Path("/tmp") / f"friday_bench_{datetime.now().strftime('%Y%m%d_%H%M')}.log"
VAULT_CLI = os.path.expanduser("~/Jarvis/knowledge/vault_cli.py")


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    LOG_PATH.write_text(LOG_PATH.read_text() + line + "\n" if LOG_PATH.exists() else line + "\n")


def preflight() -> None:
    text = PROMPT_FILE.read_text()
    if "**6+ = TRADE_NOW**" not in text:
        raise SystemExit(
            "ABORT: ghost_validator_v1.md no longer has the 6+ threshold. "
            "Live config has changed — manual review required."
        )
    if not TEACH_IMG.exists():
        raise SystemExit(f"ABORT: teaching image missing at {TEACH_IMG}")
    try:
        with urllib.request.urlopen("http://127.0.0.1:11502/v1/models", timeout=5) as r:
            r.read(200)
    except Exception as e:
        raise SystemExit(f"ABORT: MLX 35B server unreachable on :11502 — {e}")
    log("preflight OK: prompt threshold intact, teaching image present, MLX reachable")


def candles_to_df(resp: dict) -> pd.DataFrame | None:
    candles = resp.get("candles", [])
    rows = []
    for c in candles:
        if not c.get("complete", True):
            continue
        mid = c.get("mid", {})
        rows.append({
            "time": c.get("time"),
            "open": float(mid.get("o", 0)),
            "high": float(mid.get("h", 0)),
            "low": float(mid.get("l", 0)),
            "close": float(mid.get("c", 0)),
            "volume": float(c.get("volume", 0)),
        })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"])
    return df.sort_values("time").reset_index(drop=True)


def detect_patterns_for_timestamp(pair: str, ts_iso: str) -> list[str]:
    try:
        to_time = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except Exception:
        return []
    try:
        from_time = to_time - timedelta(minutes=15 * 200)
        resp = fetch_candles(
            instrument=pair, timeframe="M15", count=200,
            from_time=from_time.astimezone(timezone.utc),
            to_time=to_time.astimezone(timezone.utc),
        )
    except Exception as e:
        return [f"fetch_error: {e}"]
    df = candles_to_df(resp)
    if df is None or len(df) < 5:
        return ["no_candles"]
    try:
        df = detect_all_patterns(df)
        df = detect_support_resistance(df, lookback=100)
    except Exception as e:
        return [f"detect_error: {e}"]
    candle_cols = [c for c in df.columns if c in (
        "hammer", "shooting_star", "bullish_engulfing", "bearish_engulfing",
        "morning_star", "evening_star", "doji", "gravestone_doji", "dragonfly_doji",
        "three_white_soldiers", "three_black_crows", "marubozu_bull", "marubozu_bear",
        "spinning_top", "inverted_hammer", "piercing_pattern", "dark_cloud",
        "tweezer_top", "tweezer_bottom", "harami_bull", "harami_bear",
        "candle_bull_signal", "candle_bear_signal",
    )]
    sr_cols = [c for c in df.columns if c in ("double_top", "double_bottom")]
    tags: list[str] = []
    for bars_ago in range(min(3, len(df))):
        row = df.iloc[-(1 + bars_ago)]
        for col in candle_cols:
            try:
                if bool(row.get(col, False)):
                    tag = col if bars_ago == 0 else f"{col}@{bars_ago}_bars_ago"
                    if col not in tags and tag not in tags:
                        tags.append(tag)
            except Exception:
                pass
    for col in sr_cols:
        for idx in range(max(0, len(df) - 10), len(df)):
            try:
                if bool(df.iloc[idx].get(col, False)):
                    bars_back = len(df) - 1 - idx
                    tag = col if bars_back == 0 else f"{col}@{bars_back}_bars_back"
                    if tag not in tags:
                        tags.append(tag)
                    break
            except Exception:
                pass
    return tags


def post(system_prompt: str, user_content: list, timeout: int = 240) -> dict:
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.7, "top_p": 0.8, "max_tokens": 1536, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        MLX_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        raw = data["choices"][0]["message"].get("content", "") or ""
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        return {"raw": raw, "latency": round(time.time() - t0, 1), "error": None}
    except Exception as e:
        return {"raw": "", "latency": round(time.time() - t0, 1), "error": str(e)}


def extract(raw: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    blob = m.group(1) if m else raw
    try:
        return json.loads(blob)
    except Exception:
        return {}


def write_vault(verdict_counts: dict, total: int, regression: bool, results: list) -> None:
    summary = (
        f"Friday bench post-ship validation: TN={verdict_counts['TRADE_NOW']} "
        f"WCH={verdict_counts['WATCH']} SKIP={verdict_counts['SKIP']} "
        f"(REGRESSION)" if regression else
        f"Friday bench post-ship validation: TN={verdict_counts['TRADE_NOW']} "
        f"WCH={verdict_counts['WATCH']} SKIP={verdict_counts['SKIP']}"
    )
    table = "\n".join(
        f"  - id={r['id']} {r['pair']}: {r.get('verdict')} dir={r.get('direction')} "
        f"c={r.get('confidence')} conds={r.get('n_conds')} ({r.get('latency')}s)"
        for r in results
    )
    context = (
        f"Bench: 14 historical Opus-TRADE_NOW charts vs LIVE config "
        f"(ghost_validator_v1.md @ 6+ threshold + canonical EUR/CHF teaching image + bare task).\n"
        f"Endpoint: MLX 35B port 11502.\nResults:\n{table}\n"
        f"Total actionable: {verdict_counts['TRADE_NOW'] + verdict_counts['WATCH']}/{total}.\n"
        f"Baseline (2026-04-26): TN=5 WCH=7 SKIP=2 (12/14 actionable)."
    )
    try:
        subprocess.run(
            [
                "python3", VAULT_CLI,
                "--agent", "claude-code",
                "--type", "failure" if regression else "note",
                "--summary", summary,
                "--context", context,
                "--tags", "validator,bench,friday,35b",
            ],
            check=True, timeout=30,
        )
        log(f"vault write OK: {summary}")
    except Exception as e:
        log(f"vault write FAILED: {e}")


def main() -> None:
    LOG_PATH.write_text("")
    log(f"START friday_bench — model={MODEL} db={DB}")
    preflight()
    system_prompt = PROMPT_FILE.read_text()
    teach_b64 = base64.b64encode(TEACH_IMG.read_bytes()).decode()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    q = ",".join("?" * len(IDS))
    rows = conn.execute(
        f"SELECT id, chart_path, timestamp, input_prompt FROM vision_training_data WHERE id IN ({q})",
        IDS,
    ).fetchall()
    conn.close()
    rows_by_id = {r["id"]: r for r in rows}

    results = []
    for i, id_ in enumerate(IDS, 1):
        if i > 1:
            time.sleep(15)
        r = rows_by_id.get(id_)
        if not r or not os.path.exists(r["chart_path"]):
            log(f"[{i:2d}/14] SKIP id={id_} (chart missing)")
            continue
        try:
            ip = json.loads(r["input_prompt"]) if isinstance(r["input_prompt"], str) else r["input_prompt"]
        except Exception:
            ip = {}
        pair = ip.get("pair", "?")
        indicators = ip.get("indicators") or {}
        patterns = detect_patterns_for_timestamp(pair, r["timestamp"])
        chart_b64 = base64.b64encode(Path(r["chart_path"]).read_bytes()).decode()
        pair_d = pair.replace("_", "/")
        ind_lines = [f"  - {k}: {v}" for k, v in indicators.items() if v is not None]
        ind_text = "\n".join(ind_lines) if ind_lines else "  (no indicator data)"
        candles_line = ", ".join(patterns) if patterns else "None detected"

        user_content = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{teach_b64}"}},
            {"type": "text", "text": f"TEACHING IMAGE: {TEACHING_DESC}"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{chart_b64}"}},
            {"type": "text", "text": (
                f"LIVE CHART — {pair_d} M15. Read this fresh, mirror the teaching pattern, "
                f"form your own thesis from the structure you see.\n\n"
                f"## Live indicator data\n{ind_text}\n\n"
                f"## Candlestick patterns detected (last 3 bars)\n  {candles_line}\n\n"
                f"---\n"
                f"Return ONLY a ```json code block with: verdict (TRADE_NOW/WATCH/SKIP), "
                f"direction (BUY/SELL), confidence (0-10), reasoning (start with CHART READ:), "
                f"re_entry_conditions, snipe_entry_zone, snipe_invalidation, snipe_target."
            )},
        ]

        res = post(system_prompt, user_content)
        f = extract(res["raw"])
        v = f.get("verdict")
        d = f.get("direction")
        c = f.get("confidence")
        n = len(f.get("re_entry_conditions") or []) if isinstance(f.get("re_entry_conditions"), list) else 0
        mark = {"TRADE_NOW": "✅", "WATCH": "⚠️", "SKIP": "❌"}.get(v, "💥")
        log(f"[{i:2d}/14] {pair} | {mark} {v} dir={d} c={c} conds={n} ({res['latency']}s)")
        if res.get("error"):
            log(f"        ERR: {res['error']}")
        results.append({
            "id": id_, "pair": pair,
            "verdict": v, "direction": d, "confidence": c, "n_conds": n,
            "latency": res["latency"], "error": res.get("error"),
            "parsed": f, "raw": res["raw"],
        })

    OUT_JSON.write_text(json.dumps(results, indent=2, default=str))
    counts = {"TRADE_NOW": 0, "WATCH": 0, "SKIP": 0}
    pf = 0
    for r in results:
        v = r.get("verdict")
        if v in counts:
            counts[v] += 1
        elif not v:
            pf += 1
    total = sum(counts.values()) + pf
    actionable = counts["TRADE_NOW"] + counts["WATCH"]
    regression = counts["TRADE_NOW"] < 4 or actionable < 10

    log("=" * 60)
    log(
        f"DONE — TN={counts['TRADE_NOW']} WCH={counts['WATCH']} "
        f"SKIP={counts['SKIP']} parse_fail={pf}/{total}"
    )
    log(f"actionable: {actionable}/{total} ({100*actionable//max(1,total)}%)")
    log(f"baseline:   12/14 (86%)")
    log(f"regression flag: {regression}")
    log(f"raw -> {OUT_JSON}")

    write_vault(counts, total, regression, results)


if __name__ == "__main__":
    main()
