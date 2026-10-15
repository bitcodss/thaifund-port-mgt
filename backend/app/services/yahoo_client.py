"""
Yahoo Finance access (unofficial public endpoints): symbol search, daily price
history and per-ticker RSS headlines. Kept behind this one module so the data
source can be swapped if Yahoo changes or blocks these endpoints.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
_HEADERS = {"User-Agent": "Mozilla/5.0 (thai-fund-tracker)"}
_MIN_INTERVAL = 0.3  # seconds between calls — be polite to an unofficial API
_TIMEOUT = httpx.Timeout(20.0)

_lock = asyncio.Lock()
_last_call = 0.0


class YahooError(Exception):
    pass


async def _get(url: str, params: dict) -> httpx.Response:
    global _last_call
    async with _lock:
        gap = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if gap > 0:
            await asyncio.sleep(gap)
        _last_call = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise YahooError(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise YahooError(f"HTTP {resp.status_code} from {url}")
    return resp


async def search(query: str, limit: int = 5) -> list[dict]:
    """Symbol search → [{symbol, quote_type, exchange, name}]."""
    resp = await _get(_SEARCH_URL, {"q": query, "quotesCount": limit, "newsCount": 0})
    return [
        {"symbol": q.get("symbol"), "quote_type": q.get("quoteType"),
         "exchange": q.get("exchange"), "name": q.get("shortname") or q.get("longname")}
        for q in resp.json().get("quotes", []) if q.get("symbol")
    ]


async def history(ticker: str, range_: str = "2y") -> list[tuple[date, float]]:
    """Daily closes, oldest first. Empty list if Yahoo has no data for the ticker."""
    resp = await _get(_CHART_URL.format(ticker=ticker), {"range": range_, "interval": "1d"})
    results = (resp.json().get("chart") or {}).get("result") or []
    if not results:
        return []
    r = results[0]
    closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = []
    for ts, c in zip(r.get("timestamp") or [], closes):
        if c is not None:
            out.append((datetime.fromtimestamp(ts, tz=timezone.utc).date(), float(c)))
    return out


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_rss(xml_text: str) -> list[dict]:
    """RSS 2.0 → [{title, description, link, published_at}] (items without link/date skipped)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise YahooError(f"bad RSS: {exc}") from exc
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        title = _clean(it.findtext("title"))
        if not link or not pub or not title:
            continue
        try:
            published = parsedate_to_datetime(pub).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        items.append({"title": title, "description": _clean(it.findtext("description")) or None,
                      "link": link, "published_at": published})
    return items


async def rss(ticker: str) -> list[dict]:
    """Latest ~20 headlines for a ticker (Yahoo keeps no history — collect regularly)."""
    resp = await _get(_RSS_URL, {"s": ticker, "region": "US", "lang": "en-US"})
    return parse_rss(resp.text)
