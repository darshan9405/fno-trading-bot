"""`fetch_news` tool — recent symbol-relevant news via Google News RSS.

Background: Microsoft retired the Bing News Search API on 2025-08-11 and the
previous fallback (scraping the public Bing News HTML) has become fragile as
Bing has tightened its HTML/JS rendering. We now use Google's public News RSS
endpoint instead — no API key, no quota, India-localised by default, and the
XML is stable enough to parse with the stdlib.

Each item returned has the same shape the LLM has always consumed:

  {
    "title":     "<headline, without the trailing ' - Publisher' suffix>",
    "url":       "<Google redirect URL — opens the real article in a browser>",
    "snippet":   "<plain-text summary, HTML stripped>",
    "source":    "<publisher display name, e.g. 'The Economic Times'>",
    "published": "<ISO-8601 timestamp in UTC, or None>",
    "age":       "<relative age string, e.g. '2h ago', or None>",
  }

The query is `"{symbol}" stock NSE India` to bias results toward the Indian
equity context. Symbols with special characters (e.g. `M&M`) are quoted so
they parse correctly.

Pure network fetch — no broker wiring. Caller (agent loop) should pass a
6-8 second timeout; we clamp it ourselves to avoid blocking the scheduler.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Google News titles end with ` - PublisherName`. We strip that trailing suffix
# (we already surface the publisher separately via the `<source>` element) so
# the LLM doesn't get a redundant field duplicated in the title.
_TITLE_PUBLISHER_SUFFIX_RE = re.compile(r"\s+-\s+[^-\n]+$")


def _strip_html(s: str | None) -> str:
    """Cheap HTML tag stripper (we control the input — Google RSS only)."""
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def _clean_title(raw: str | None, publisher: str) -> str:
    """Trim the trailing ` - PublisherName` Google appends, if we can detect it.

    Google always appends ` - <PublisherName>` to the title where the publisher
    matches the `<source>` element's text. If we can't match exactly we still
    drop a generic ` - <words>` suffix as a best-effort cleanup, capped to one
    suffix (so we don't strip legitimate hyphens in the headline).
    """
    title = (raw or "").strip()
    if not title:
        return ""
    if publisher and title.endswith(f" - {publisher}"):
        return title[: -len(f" - {publisher}")].rstrip()
    # Generic fallback: strip one trailing ` - <short token>` suffix only when
    # it's clearly a publisher tag (no spaces inside, <= 40 chars).
    m = _TITLE_PUBLISHER_SUFFIX_RE.search(title)
    if m:
        suffix = m.group(0).lstrip(" -").strip()
        if " " not in suffix and len(suffix) <= 40:
            return title[: m.start()].rstrip()
    return title


def _age_human(iso_date: str | None) -> str | None:
    """Best-effort relative age from an ISO date string (no TZ assumed UTC)."""
    if not iso_date:
        return None
    try:
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


def _parse_pub_date(rfc822: str | None) -> str | None:
    """Convert an RFC-822 pubDate (Google's format) to ISO-8601 UTC.

    Returns None on any parse failure so the caller can degrade gracefully.
    """
    if not rfc822:
        return None
    try:
        dt = parsedate_to_datetime(rfc822)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _google_news_rss(symbol: str, n: int) -> list[dict[str, Any]]:
    """Hit Google's public News RSS search and return up to `n` parsed items."""
    # Quote the symbol so multi-word / ampersand tickers like `M&M` work;
    # append `stock NSE India` to bias towards Indian-equity coverage.
    query = f'"{symbol}" stock NSE India'
    params = {
        "q": query,
        "hl": "en-IN",
        "gl": "IN",
        "ceid": "IN:en",
    }
    url = f"{GOOGLE_NEWS_RSS}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            body = resp.content
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_news: google RSS fetch failed for %s (%s)", symbol, e)
        return []

    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        log.warning("fetch_news: google RSS parse failed for %s (%s)", symbol, e)
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    out: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for item in channel.findall("item"):
        raw_title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = item.findtext("description")
        source_el = item.find("source")
        publisher = (source_el.text or "").strip() if source_el is not None else ""
        pub_date_raw = item.findtext("pubDate")
        published_iso = _parse_pub_date(pub_date_raw)

        if not raw_title or not link:
            # Google always populates both, but if either is missing we can't
            # surface a meaningful row to the LLM.
            continue

        title = _clean_title(raw_title, publisher)
        snippet = _strip_html(description)[:500]

        # Dedupe: same headline syndicated across multiple publishers is the
        # norm on Google News. Keeping the first occurrence preserves the
        # recency ordering Google already gave us.
        dedupe_key = title.lower()
        if dedupe_key in seen_titles:
            continue
        seen_titles.add(dedupe_key)

        out.append({
            "title": title[:300],
            "url": link,
            "snippet": snippet,
            "source": publisher[:120],
            "published": published_iso,
            "age": _age_human(published_iso),
        })
        if len(out) >= n:
            break

    return out


class FetchNewsTool:
    name = "fetch_news"
    description = (
        "Fetch up to `n` recent news headlines for the underlying symbol from "
        "Google News (India-localised RSS search; no API key required). "
        "Returns title, url, snippet, source (publisher name), and a relative "
        "age string. Use this to ground the breakout call in any material news "
        "(earnings, sector rotation, regulatory events, index re-weightings)."
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

    def __init__(self, context: dict[str, Any]) -> None:  # noqa: D401
        # No external configuration needed: the tool hits a public, key-less
        # endpoint. The constructor still accepts `context` to stay compatible
        # with the agent loop's tool-factory signature.
        self._context = dict(context or {})

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = str(args.get("symbol") or "").strip()
        n = int(args.get("n") or 5)
        if not symbol:
            return {"error": "symbol is required", "items": []}
        items = _google_news_rss(symbol, n)
        if not items:
            return {"items": [], "note": "no news returned; this is normal for illiquid symbols"}
        return {"items": items, "count": len(items)}

    @staticmethod
    def to_schema() -> dict[str, Any]:
        return schema(FetchNewsTool.name, FetchNewsTool.description, FetchNewsTool.parameters)


__all__ = ["FetchNewsTool"]


def _self_test() -> str:  # pragma: no cover - manual sanity check
    """Quick smoke test: print a JSON dump of one fetch. Run via:
        python -c "from app.strategy.llm_breakout.tools.news import _self_test; print(_self_test())"
    """
    return json.dumps(_google_news_rss("RELIANCE", 3), indent=2)
