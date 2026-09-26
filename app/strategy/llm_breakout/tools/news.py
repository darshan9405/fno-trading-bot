"""`fetch_news` tool — recent symbol-relevant news via Bing.

Two modes:
  * Bing News Search API (preferred when `LLM_BING_API_KEY` is configured)
  * Public Bing News HTML search (no key, used as a fallback)

Returns up to `n` items: title, url, snippet, source, age. Pure network
fetch — no broker wiring. Caller (agent loop) should pass a 6-8 second
timeout; we clamp it ourselves to avoid blocking the scheduler tick.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
from typing import Any

import httpx

from app.strategy.llm_breakout.tools.base import schema

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 8.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _age_human(iso_date: str | None) -> str | None:
    """Best-effort relative age from an ISO date string (no TZ assumed UTC)."""
    if not iso_date:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _bing_news_api(symbol: str, n: int, api_key: str) -> list[dict[str, Any]]:
    """Bing News Search API (Microsoft Cognitive Services / Azure)."""
    endpoint = "https://api.bing.microsoft.com/v7.0/news/search"
    # Quote the symbol so searches like 'M&M' work; also add "stock NSE" for
    # Indian-market relevance.
    query = f'"{symbol}" stock NSE'
    headers = {"Ocp-Apim-Subscription-Key": api_key, "User-Agent": USER_AGENT}
    params = {"q": query, "count": min(max(n, 1), 25), "mkt": "en-IN", "freshness": "Week"}
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = client.get(endpoint, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_news: bing API failed for %s (%s)", symbol, e)
        return []
    out: list[dict[str, Any]] = []
    for v in (data.get("value") or [])[:n]:
        out.append({
            "title": v.get("name", ""),
            "url": v.get("url", ""),
            "snippet": v.get("description", ""),
            "source": (v.get("provider") or [{}])[0].get("name", ""),
            "published": v.get("datePublished", ""),
            "age": _age_human(v.get("datePublished")),
        })
    return out


_NEWS_BLOCK_RE = re.compile(
    r'<div[^>]*class="[^"]*news-card[^"]*"[^>]*>(.*?)(?=<div[^>]*class="[^"]*news-card|</main)',
    re.DOTALL | re.IGNORECASE,
)


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _bing_html(symbol: str, n: int) -> list[dict[str, Any]]:
    """Scrape the public Bing News HTML search results (no API key)."""
    query = f'{symbol} stock NSE India'
    url = "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": query, "qft": "interval=\"9\""})
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            html_body = resp.text
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_news: bing HTML fetch failed for %s (%s)", symbol, e)
        return []

    # Bing's HTML is loosely structured; we look for <a class="title"> and the
    # snippet <div class="snippet"> inside each news-card block.
    pattern = re.compile(
        r'<a[^>]*class="title"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'<div[^>]*class="snippet"[^>]*>(?P<snippet>.*?)</div>.*?'
        r'<span[^>]*tabindex="0"[^>]*>\s*(?P<age>[^<]+?)\s*</span>.*?'
        r'<div[^>]*class="source"[^>]*>\s*(?P<source>[^<]+?)\s*<',
        re.DOTALL | re.IGNORECASE,
    )
    out: list[dict[str, Any]] = []
    for m in pattern.finditer(html_body):
        out.append({
            "title": _strip_tags(m.group("title"))[:300],
            "url": m.group("url"),
            "snippet": _strip_tags(m.group("snippet"))[:500],
            "source": _strip_tags(m.group("source"))[:120],
            "published": None,
            "age": _strip_tags(m.group("age"))[:40] or None,
        })
        if len(out) >= n:
            break

    if not out:
        # Fallback: pull anchors + adjacent snippets more aggressively.
        anchor_re = re.compile(
            r'<a[^>]*class="title"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        )
        snippet_re = re.compile(
            r'<div[^>]*class="snippet"[^>]*>(?P<snippet>.*?)</div>',
            re.DOTALL | re.IGNORECASE,
        )
        anchors = list(anchor_re.finditer(html_body))
        for i, m in enumerate(anchors[:n]):
            window = html_body[m.start(): m.end() + 1500]
            snip = snippet_re.search(window)
            out.append({
                "title": _strip_tags(m.group("title"))[:300],
                "url": m.group("url"),
                "snippet": _strip_tags(snip.group("snippet"))[:500] if snip else "",
                "source": "",
                "published": None,
                "age": None,
            })
    return out


class FetchNewsTool:
    name = "fetch_news"
    description = (
        "Fetch up to `n` recent news headlines for the underlying symbol from "
        "Bing News (Bing News Search API if LLM_BING_API_KEY is configured, "
        "otherwise public Bing News HTML search). Returns title, url, snippet, "
        "source, and a relative age string. Use this to ground the breakout "
        "call in any material news (earnings, sector rotation, regulatory "
        "events, index re-weightings)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Underlying symbol, e.g. RELIANCE, NIFTY"},
            "n": {"type": "integer", "minimum": 1, "maximum": 15,
                  "description": "Number of headlines to return (default 5)."},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    }

    def __init__(self, context: dict[str, Any]) -> None:
        self._api_key = str(context.get("bing_api_key") or "").strip()

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = str(args.get("symbol") or "").strip()
        n = int(args.get("n") or 5)
        if not symbol:
            return {"error": "symbol is required", "items": []}
        if self._api_key:
            items = _bing_news_api(symbol, n, self._api_key)
        else:
            items = _bing_html(symbol, n)
        if not items:
            return {"items": [], "note": "no news returned; this is normal for illiquid symbols"}
        return {"items": items, "count": len(items)}

    @staticmethod
    def to_schema() -> dict[str, Any]:
        return schema(FetchNewsTool.name, FetchNewsTool.description, FetchNewsTool.parameters)