#!/usr/bin/env python3
"""
news_sentiment_scorer.py — LLM-based news sentiment scoring for forex pairs.

Primary: Local CSO 35B model (port 11502 — same as intelligence synthesis).
Fallback: Keyword-based scoring if local LLM is unavailable.

Input:  List of news article dicts (title, summary, source)
Output: Same articles with added sentiment fields + aggregate pair-level score.
"""

import json
import logging
import re
import urllib.request
import urllib.error
from typing import List, Dict

logger = logging.getLogger(__name__)

LOCAL_LLM_URL   = "http://localhost:11503/v1/chat/completions"  # serving gateway → MLX 35B
LOCAL_LLM_MODEL = "mlx-community/Qwen3.5-35B-A3B-4bit"

BULLISH_WORDS = frozenset({
    "surge", "rally", "gain", "gains", "strong", "strength", "hawkish",
    "beat", "beats", "exceed", "exceeds", "exceeded", "growth", "upgrade",
    "upgrades", "upgraded", "accelerate", "accelerates", "expansion",
    "tightening", "hike", "hikes", "hiked", "outperform", "boost", "boosted",
    "record", "high", "resilient", "robust", "optimistic", "positive",
})
BEARISH_WORDS = frozenset({
    "drop", "drops", "fall", "falls", "decline", "declines", "weak", "weakness",
    "dovish", "miss", "misses", "missed", "cut", "cuts", "recession", "downgrade",
    "downgrades", "downgraded", "contraction", "slowdown", "slows", "slowdown",
    "easing", "ease", "underperform", "selloff", "sell-off", "concern", "concerns",
    "risk", "risks", "pressure", "pressures", "disappoint", "disappoints",
    "disappointing", "disappointed", "negative", "warning", "warns",
})


def _keyword_fallback(articles: List[Dict], pair: str) -> List[Dict]:
    """Keyword-based sentiment scoring — used when local LLM is unavailable."""
    base_ccy = pair.split("_")[0] if "_" in pair else pair[:3]

    for article in articles:
        text = (
            (article.get("title") or "") + " " +
            (article.get("summary") or "")
        ).lower()

        bull = sum(1 for w in BULLISH_WORDS if w in text)
        bear = sum(1 for w in BEARISH_WORDS if w in text)

        if bull > bear:
            article["sentiment"]            = "bullish"
        elif bear > bull:
            article["sentiment"]            = "bearish"
        else:
            article["sentiment"]            = "neutral"

        article["sentiment_confidence"] = "low"
        article["impact_level"]         = "medium"

    return articles


def _parse_json_response(content: str) -> List[Dict]:
    """Extract JSON array from LLM response, tolerant of markdown wrappers."""
    # Strip thinking tags (Qwen3)
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

    # Extract JSON array
    m = re.search(r'\[.*\]', content, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return []


def score_news_batch(articles: List[Dict], pair: str) -> List[Dict]:
    """
    Score a batch of news articles for sentiment relevant to a forex pair.
    Uses local CSO 35B in a single batch call for efficiency.

    Input:  list of {"title": str, "summary": str, "source": str}
    Output: same list with added fields: sentiment, sentiment_confidence, impact_level
    """
    if not articles:
        return []

    # Build headlines string
    headlines = "\n".join(
        f"{i+1}. [{a.get('source', 'Unknown')}] {a.get('title', '')}"
        for i, a in enumerate(articles)
    )

    base_ccy, quote_ccy = (pair.split("_") + ["?"])[:2]
    prompt = (
        f"/no_think\n"
        f"Score each headline for its impact on {base_ccy}/{quote_ccy} forex pair. "
        f"Base currency: {base_ccy}. Quote currency: {quote_ccy}.\n"
        f"For each headline:\n"
        f"- sentiment: bullish, bearish, or neutral (from {base_ccy}'s perspective)\n"
        f"- confidence: high, medium, or low\n"
        f"- impact: high, medium, or low (how much this could move the pair)\n\n"
        f"Headlines:\n{headlines}\n\n"
        f"Respond ONLY as a JSON array:\n"
        f'[{{"index": 1, "sentiment": "bullish", "confidence": "high", "impact": "medium"}}, ...]'
    )

    try:
        payload = json.dumps({
            "model":       LOCAL_LLM_MODEL,
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens":  600,
            "extra_body":  {"chat_template_kwargs": {"enable_thinking": False}},
        }).encode("utf-8")

        req = urllib.request.Request(
            LOCAL_LLM_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Jarvis-Tenant": "trading",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())

        content = result["choices"][0]["message"]["content"] or ""
        scores  = _parse_json_response(content)

        if not scores:
            raise ValueError("Empty scores from LLM")

        for score in scores:
            idx = int(score.get("index", 0)) - 1
            if 0 <= idx < len(articles):
                articles[idx]["sentiment"]            = score.get("sentiment", "neutral")
                articles[idx]["sentiment_confidence"] = score.get("confidence", "low")
                articles[idx]["impact_level"]         = score.get("impact", "low")

        # Fill any articles the LLM skipped
        for a in articles:
            if "sentiment" not in a:
                a["sentiment"]            = "neutral"
                a["sentiment_confidence"] = "low"
                a["impact_level"]         = "low"

        logger.debug(f"[{pair}] Scored {len(scores)}/{len(articles)} articles via local LLM")
        return articles

    except Exception as e:
        logger.warning(f"[{pair}] LLM sentiment scoring failed ({e}), using keyword fallback")
        return _keyword_fallback(articles, pair)


def aggregate_sentiment(articles: List[Dict]) -> Dict:
    """
    Aggregate individual article sentiments into a pair-level score.
    Weighted by impact level × confidence.
    Returns score in range [-1, +1] plus a human-readable label.
    """
    WEIGHTS = {
        ("high",   "high"):   3.0,
        ("high",   "medium"): 2.5,
        ("high",   "low"):    1.5,
        ("medium", "high"):   2.0,
        ("medium", "medium"): 1.5,
        ("medium", "low"):    1.0,
        ("low",    "high"):   1.0,
        ("low",    "medium"): 0.75,
        ("low",    "low"):    0.5,
    }
    SENTIMENT_MAP = {"bullish": 1, "neutral": 0, "bearish": -1}

    if not articles:
        return {"score": 0.0, "label": "neutral", "article_count": 0, "high_impact_count": 0}

    total_weight  = 0.0
    weighted_sum  = 0.0
    high_count    = 0

    for a in articles:
        impact     = a.get("impact_level", "low")
        confidence = a.get("sentiment_confidence", "low")
        weight     = WEIGHTS.get((impact, confidence), 0.5)
        val        = SENTIMENT_MAP.get(a.get("sentiment", "neutral"), 0)
        weighted_sum  += val * weight
        total_weight  += weight
        if impact == "high":
            high_count += 1

    score = weighted_sum / total_weight if total_weight > 0 else 0.0

    if   score >  0.3: label = "bullish"
    elif score < -0.3: label = "bearish"
    else:              label = "neutral"

    return {
        "score":             round(score, 3),
        "label":             label,
        "article_count":     len(articles),
        "high_impact_count": high_count,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_articles = [
        {"title": "Fed signals rate hike on strong jobs data", "summary": "Strong NFP", "source": "Reuters"},
        {"title": "ECB dovish pivot imminent amid weak PMI", "summary": "PMI below 50", "source": "Bloomberg"},
        {"title": "EUR/USD technical levels to watch", "summary": "Key support at 1.08", "source": "FXStreet"},
    ]
    scored = score_news_batch(test_articles, "EUR_USD")
    agg    = aggregate_sentiment(scored)
    print(f"Aggregate: {agg['label']} ({agg['score']:+.3f}) from {agg['article_count']} articles")
    for a in scored:
        print(f"  [{a['sentiment']:8}] ({a.get('impact_level', '?')} impact) {a['title'][:60]}")
