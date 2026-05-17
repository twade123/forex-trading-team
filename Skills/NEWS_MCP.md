# News MCP Skill — handler_news_info (NewsAPI.org)

Complete reference for the News MCP. Covers everything this tool can do, for any use case.

---

## Connection
- **API**: NewsAPI.org v2 — `https://newsapi.org/v2/everything`
- **Key**: `~/jarvis/API/NEWS_API_KEY.txt`
- **Free tier**: 100 requests/day, article content truncated to ~212 chars
- **Coverage**: Last 30 days only. No historical archive beyond that.
- **Sources**: 80,000+ sources worldwide (Reuters, Bloomberg, BBC, AP, TechCrunch, etc.)

---

## Function: fetch_news

Single function — all news retrieval goes through this.

```python
# handler_news_info.fetch_news(api_key, query, from_date, ...)
# Async — handles pagination internally (loops through all pages)

params: {
    query: str,              # REQUIRED. Search terms. Supports AND, OR, NOT, quotes.
    from_date: str,          # REQUIRED. "YYYY-MM-DD". Max 30 days back.
    to_date: str,            # Optional. Defaults to today.
    sort_by: str,            # "relevancy" | "popularity" | "publishedAt". Default: "popularity"
    page_size: int,          # Results per page. Max 100. Default: 100.
    language: str,           # ISO code: "en", "es", "fr", "de", "it", "pt", "ar", "zh", etc.
    domains: str,            # CSV of domains to include: "reuters.com,bloomberg.com"
    exclude_domains: str     # CSV of domains to exclude: "tabloids.com,clickbait.com"
}
```

### Query Syntax
- **AND**: `"Federal Reserve AND interest rate"` — both terms required
- **OR**: `"bitcoin OR cryptocurrency"` — either term
- **NOT**: `"apple NOT fruit"` — exclude term
- **Quotes**: `"machine learning"` — exact phrase
- **Combine**: `"(bitcoin OR ethereum) AND regulation"` — grouping with parentheses

### Response Format
```json
[
    {
        "source": {"id": "reuters", "name": "Reuters"},
        "author": "John Smith",
        "title": "Fed Signals Rate Cut Path for 2026",
        "description": "Federal Reserve officials indicated a potential shift in monetary policy...",
        "url": "https://reuters.com/article/...",
        "urlToImage": "https://...",
        "publishedAt": "2026-02-10T19:00:45Z",
        "content": "Federal Reserve Bank of Dallas President Lorie Logan said... [+2043 chars]"
    }
]
```

**Field notes:**
- `description` — usually 1-2 sentences, better than `content` on free tier
- `content` — truncated to ~212 chars on free tier, ends with `[+NNN chars]`
- `publishedAt` — ISO 8601 UTC timestamp
- `source.id` — can be null for smaller sources
- `author` — can be null
- `urlToImage` — article thumbnail, can be null
- Returns **empty list** `[]` for queries with no matches (not an error)

### Pagination
The handler paginates automatically — loops until all results fetched. 1-second delay between pages. You get back the full list, not pages.

### Sorting Options
| sort_by | Behavior |
|---------|----------|
| `"relevancy"` | Most relevant to query first. Best for targeted research. |
| `"popularity"` | Most-read articles first. Best for trending topics. |
| `"publishedAt"` | Newest first. Best for time-sensitive monitoring. |

### Domain Filtering
Use `domains` to restrict to trusted sources:
```python
# Financial news only
domains="reuters.com,bloomberg.com,ft.com,wsj.com,cnbc.com"

# Tech news only
domains="techcrunch.com,theverge.com,arstechnica.com,wired.com"

# Exclude tabloids
exclude_domains="dailymail.co.uk,nypost.com"
```

---

## Rate Limits & Best Practices
- **100 requests/day** on free tier — plan queries carefully
- **Pagination counts as multiple requests** — a query returning 300 articles = 3 requests
- **Cache results** — news doesn't change retroactively, cache by query+date for reuse
- **Use `page_size=10-20`** when you only need headlines, not full scrapes
- **`sort_by="relevancy"`** when searching for specific topics
- **`sort_by="publishedAt"`** when monitoring for breaking news
- **30-day window max** — anything older is not available

---

## Confirmed Live Behavior (Feb 2026)
- `q="Federal Reserve AND interest rate"` → 260 results ✅
- `q="ECB AND monetary policy"` → 13 results ✅
- `q="nonsense_query_xyz"` → empty list (no error) ✅
- AND/OR operators work correctly ✅
- `description` field gives better summaries than truncated `content` ✅
- Auto-pagination fetches all pages with 1s delay ✅

---

## Error Handling
- **HTTP 401**: Invalid or missing API key
- **HTTP 426**: Free tier trying to access paid features
- **HTTP 429**: Rate limit exceeded — handler logs error, returns what it has
- **HTTP 500**: NewsAPI server error — retry after delay
- **No results**: Returns empty list `[]`, not an error
