#!/usr/bin/env python3
"""
economic_calendar_fetcher.py — Economic calendar events for the intelligence package.

Sources (in priority order):
  1. ForexFactory calendar page (scrape, no API key needed)
  2. Investing.com economic calendar RSS (fallback)

Returns next 24h medium+high impact events for our 8 currencies.
Cache TTL: 6 hours (intelligence_cache table in v2/trading_forex.db).
"""

import json
import logging
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

FOREXFACTORY_URL = "https://www.forexfactory.com/calendar"
INVESTING_RSS_URL = "https://www.investing.com/rss/economic_calendar.rss"

CALENDAR_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}

ALL_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "NZD_USD", "USD_CAD",
    "USD_CHF", "EUR_GBP", "EUR_JPY", "GBP_JPY", "AUD_JPY", "EUR_AUD", "EUR_CHF"
]

# Impact keyword mapping for RSS fallback
_IMPACT_KEYWORDS = {
    "high":   {"non-farm", "nfp", "gdp", "fomc", "rate decision", "cpi", "unemployment"},
    "medium": {"pmi", "retail", "trade balance", "housing", "confidence", "ism", "payroll"},
}


def _map_currency_to_pairs(currency: str) -> List[str]:
    """Map a currency code to all 13 pairs that include it."""
    return [p for p in ALL_PAIRS if currency in p.split("_")]


def _try_forexfactory(hours_ahead: int) -> List[Dict]:
    """
    Scrape ForexFactory calendar page.
    Parses the JSON data embedded in the page (FF uses a data-object approach).
    Falls back to HTML table parsing if JSON not found.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        req = urllib.request.Request(FOREXFACTORY_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        events = []
        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc + timedelta(hours=hours_ahead)

        # FF embeds calendar data as window.calendarComponentStates JSON
        import re
        json_match = re.search(r'calendarComponentStates\s*=\s*(\[.*?\]);', html, re.DOTALL)
        if not json_match:
            # Try alternate pattern
            json_match = re.search(r'"calendar":\s*(\[.*?\])', html, re.DOTALL)

        if json_match:
            try:
                raw = json.loads(json_match.group(1))
                for item in (raw if isinstance(raw, list) else []):
                    currency = (item.get("currency") or item.get("cur") or "").upper()
                    if currency not in CALENDAR_CURRENCIES:
                        continue

                    impact_raw = (item.get("impact") or item.get("imp") or "").lower()
                    if "red" in impact_raw or impact_raw == "3":
                        impact = "high"
                    elif "orange" in impact_raw or "ora" in impact_raw or impact_raw == "2":
                        impact = "medium"
                    else:
                        impact = "low"

                    if impact not in ("medium", "high"):
                        continue

                    # Parse event time
                    event_time = _parse_ff_time(item, now_utc)
                    if event_time is None or event_time > cutoff:
                        continue

                    events.append({
                        "event_name": item.get("title") or item.get("name") or "Unknown Event",
                        "currency": currency,
                        "impact": impact,
                        "time_utc": event_time.isoformat(),
                        "expected": str(item.get("forecast") or item.get("exp") or "") or None,
                        "previous": str(item.get("previous") or item.get("prev") or "") or None,
                        "actual":   str(item.get("actual") or item.get("act") or "") or None,
                        "affects_pairs": _map_currency_to_pairs(currency),
                        "source": "forexfactory",
                    })
                return events
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # HTML table fallback — parse <tr class="calendar__row"> elements
        # Impact icons: icon--ff-impact-red, icon--ff-impact-ora, icon--ff-impact-yel
        row_pattern = re.compile(r'<tr[^>]+class="[^"]*calendar__row[^"]*"[^>]*>(.*?)</tr>', re.DOTALL)
        cell_pattern = re.compile(r'<td[^>]*class="[^"]*calendar__([a-z]+)[^"]*"[^>]*>(.*?)</td>', re.DOTALL)
        tag_strip = re.compile(r'<[^>]+>')

        current_date = now_utc.date()
        for row_m in row_pattern.finditer(html):
            row_html = row_m.group(1)
            cells = {m.group(1): tag_strip.sub("", m.group(2)).strip()
                     for m in cell_pattern.finditer(row_html)}

            currency = cells.get("currency", "").upper()
            if currency not in CALENDAR_CURRENCIES:
                continue

            # Impact from icon class
            if "impact-red" in row_html:
                impact = "high"
            elif "impact-ora" in row_html:
                impact = "medium"
            else:
                continue  # Skip low/holiday

            event_name = cells.get("event", "").strip()
            time_str = cells.get("time", "").strip()
            date_str = cells.get("date", "").strip()

            if date_str:
                try:
                    current_date = datetime.strptime(date_str, "%a%b %d").replace(
                        year=now_utc.year).date()
                except ValueError:
                    pass

            event_time = _parse_time_str(time_str, current_date)
            if event_time is None or event_time > cutoff:
                continue

            events.append({
                "event_name": event_name,
                "currency": currency,
                "impact": impact,
                "time_utc": event_time.isoformat(),
                "expected": cells.get("forecast") or None,
                "previous": cells.get("previous") or None,
                "actual": cells.get("actual") or None,
                "affects_pairs": _map_currency_to_pairs(currency),
                "source": "forexfactory",
            })

        return events

    except Exception as e:
        logger.warning(f"ForexFactory scrape failed: {e}")
        return []


def _parse_ff_time(item: Dict, now_utc: datetime) -> Optional[datetime]:
    """Parse a time field from a ForexFactory JSON item."""
    for field in ("timestamp", "date", "time", "datetime", "eventDateTime"):
        val = item.get(field)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            try:
                return datetime.fromtimestamp(val, tz=timezone.utc)
            except (OSError, OverflowError):
                pass
        if isinstance(val, str):
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
    return None


def _parse_time_str(time_str: str, date: "datetime.date") -> Optional[datetime]:
    """Parse an HH:MM AM/PM time string combined with a date into UTC datetime."""
    if not time_str:
        return None
    import re
    m = re.match(r'(\d{1,2}):(\d{2})(am|pm)', time_str.lower().replace(" ", ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if m.group(3) == "pm" and hour != 12:
        hour += 12
    elif m.group(3) == "am" and hour == 12:
        hour = 0
    # FF times are Eastern — offset ~4-5 hours to UTC (approximate, sufficient for calendar use)
    try:
        et = datetime(date.year, date.month, date.day, hour, minute, tzinfo=timezone.utc)
        return et + timedelta(hours=4)  # Approximate ET→UTC
    except ValueError:
        return None


def _try_investing_rss(hours_ahead: int) -> List[Dict]:
    """Fallback: Investing.com economic calendar RSS feed."""
    try:
        req = urllib.request.Request(
            INVESTING_RSS_URL,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_data = resp.read().decode("utf-8", errors="replace")

        root = ET.fromstring(xml_data)
        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc + timedelta(hours=hours_ahead)
        events = []

        for item in root.iter("item"):
            title_el = item.find("title")
            pub_el   = item.find("pubDate")
            desc_el  = item.find("description")

            if title_el is None:
                continue

            title = title_el.text or ""
            desc  = (desc_el.text or "").lower() if desc_el is not None else ""

            # Extract currency from title (e.g., "USD - Non-Farm Payrolls")
            import re
            ccy_m = re.match(r'^([A-Z]{3})\s*[-–]\s*(.+)$', title.strip())
            if not ccy_m:
                continue
            currency = ccy_m.group(1).upper()
            event_name = ccy_m.group(2).strip()

            if currency not in CALENDAR_CURRENCIES:
                continue

            # Classify impact from title/description keywords
            combined = title.lower() + " " + desc
            if any(kw in combined for kw in _IMPACT_KEYWORDS["high"]):
                impact = "high"
            elif any(kw in combined for kw in _IMPACT_KEYWORDS["medium"]):
                impact = "medium"
            else:
                continue  # Skip low-impact

            # Parse pubDate
            pub_str = pub_el.text if pub_el is not None else ""
            event_time = None
            for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
                try:
                    event_time = datetime.strptime(pub_str.strip(), fmt)
                    if event_time.tzinfo is None:
                        event_time = event_time.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    pass

            if event_time is None or event_time > cutoff:
                continue

            events.append({
                "event_name": event_name,
                "currency": currency,
                "impact": impact,
                "time_utc": event_time.isoformat(),
                "expected": None,
                "previous": None,
                "actual": None,
                "affects_pairs": _map_currency_to_pairs(currency),
                "source": "investing_rss",
            })

        return events

    except Exception as e:
        logger.warning(f"Investing.com RSS fallback failed: {e}")
        return []


def fetch_economic_calendar(hours_ahead: int = 24) -> Dict:
    """
    Fetch economic events for the next N hours.
    Filters to medium+high impact events for our 8 currencies.

    Returns structured dict ready for package assembly.
    """
    events = _try_forexfactory(hours_ahead)
    if not events:
        logger.info("ForexFactory returned 0 events — trying Investing.com RSS fallback")
        events = _try_investing_rss(hours_ahead)

    # Sort by time
    events.sort(key=lambda e: e.get("time_utc", ""))

    high = [e for e in events if e["impact"] == "high"]
    medium = [e for e in events if e["impact"] == "medium"]

    # Find next high-impact event
    now_iso = datetime.now(timezone.utc).isoformat()
    next_high = None
    for e in high:
        if e["time_utc"] >= now_iso:
            # Calculate hours away
            try:
                t = datetime.fromisoformat(e["time_utc"].replace("Z", "+00:00"))
                hours_away = round((t - datetime.now(timezone.utc)).total_seconds() / 3600, 1)
                next_high = {"event": e["event_name"], "hours_away": hours_away}
            except Exception:
                next_high = {"event": e["event_name"], "hours_away": None}
            break

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "window": f"next_{hours_ahead}h",
        "events": events,
        "high_impact_count": len(high),
        "medium_impact_count": len(medium),
        "next_high_impact": next_high,
        "source_used": events[0]["source"] if events else "none",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = fetch_economic_calendar(24)
    print(f"Fetched {len(result['events'])} events ({result['high_impact_count']} high, {result['medium_impact_count']} medium)")
    for ev in result["events"][:10]:
        print(f"  [{ev['impact'].upper():6}] {ev['time_utc'][:16]} {ev['currency']} — {ev['event_name']}")
