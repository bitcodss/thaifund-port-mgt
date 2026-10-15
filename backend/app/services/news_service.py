"""
News pipeline for the monthly report.

collect_news()  — every 2h: Yahoo RSS for every valid news proxy. RSS only
                  returns the latest ~20 items (busy tickers fill that in a
                  day), so it must run often. Dedupes by link and normalised
                  title; prunes raw items older than RAW_RETENTION_DAYS.
build_digests() — weekly: per ticker, rank the week's items by relevance,
                  ask the LLM for a short summary + sentiment, verify every
                  number against the source text, store permanently with
                  the sources (raw items are pruned later).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.market import FundProxy, NewsDigest, NewsItem
from app.models.tax_lot import SyncJob
from app.services import yahoo_client
from app.services.llm import LlmError, generate_json
from app.services.number_guard import allowed_from_text, unsupported_numbers
from app.services.yahoo_client import YahooError

logger = logging.getLogger(__name__)

ICT = ZoneInfo("Asia/Bangkok")
RAW_RETENTION_DAYS = 90
DIGEST_TOP_N = 15
DIGEST_RETRIES = 2

_DIGEST_PROMPT = """Summarise this week's news for {ticker} ({label}) for a long-term fund investor.
Write 3-5 sentences in English: what happened and why it matters for the price. Then give overall sentiment.
Rules:
- Use ONLY the news items below. Do not add outside facts.
- Write numbers only as digits, and only numbers that appear in the items. Never spell numbers as words.
- If the items are unrelated noise, say there was no significant news and use sentiment "neutral".
- used_items = the item numbers you relied on.
{retry_note}
News items (JSON):
{items}"""

_DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "used_items": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["summary", "sentiment", "used_items"],
}


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:200]


async def news_tickers(db: AsyncSession) -> dict[str, str | None]:
    """{ticker: label} for every valid news proxy."""
    rows = (await db.execute(
        select(FundProxy.ticker, FundProxy.label).where(FundProxy.kind == "news", FundProxy.valid.is_(True))
    )).all()
    out: dict[str, str | None] = {}
    for t, label in rows:
        out.setdefault(t, label)
    return out


async def collect_news(db: AsyncSession) -> dict:
    tickers = await news_tickers(db)
    inserted = 0
    errors: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=RAW_RETENTION_DAYS)
    for ticker in sorted(tickers):
        try:
            items = await yahoo_client.rss(ticker)
        except YahooError as e:
            errors.append(f"{ticker}: {e}")
            continue
        known = (await db.execute(
            select(NewsItem.link, NewsItem.title_key).where(NewsItem.ticker == ticker)
        )).all()
        links = {k[0] for k in known}
        keys = {k[1] for k in known}
        for it in items:
            k = title_key(it["title"])
            if it["link"] in links or k in keys or it["published_at"] < cutoff:
                continue
            db.add(NewsItem(ticker=ticker, title=it["title"], description=it["description"],
                            link=it["link"][:1000], title_key=k, published_at=it["published_at"]))
            links.add(it["link"])
            keys.add(k)
            inserted += 1
        await db.commit()

    pruned = (await db.execute(delete(NewsItem).where(NewsItem.published_at < cutoff))).rowcount or 0
    await db.commit()
    if errors:
        # Success runs every 2h aren't logged as jobs (would flood the Sync page); failures are.
        db.add(SyncJob(id=uuid.uuid4(), type="news_collect", started_at=datetime.now(timezone.utc),
                       completed_at=datetime.now(timezone.utc), status="error",
                       error_message="; ".join(errors[:5])))
        await db.commit()
    logger.info("News collect: tickers=%d inserted=%d pruned=%d errors=%d",
                len(tickers), inserted, pruned, len(errors))
    return {"tickers": len(tickers), "inserted": inserted, "pruned": pruned, "errors": errors}


def last_full_week_start(today: date) -> date:
    """Monday of the most recent complete Mon–Sun week."""
    return today - timedelta(days=today.weekday() + 7)


def relevance(item: NewsItem, ticker: str, label: str | None) -> int:
    words = {ticker.lower()} | {w.lower() for w in re.findall(r"[A-Za-z]{4,}", label or "")
                                if w.lower() not in {"corp", "inc", "class", "fund", "trust", "ishares",
                                                     "etf", "index", "global", "shares", "spdr", "msci"}}
    title = item.title.lower()
    desc = (item.description or "").lower()
    return sum(3 for w in words if w in title) + sum(1 for w in words if w in desc)


async def build_digest(db: AsyncSession, ticker: str, label: str | None, week_start: date) -> NewsDigest:
    start = datetime.combine(week_start, time.min, tzinfo=ICT)
    end = start + timedelta(days=7)
    items = (await db.execute(
        select(NewsItem).where(NewsItem.ticker == ticker,
                               NewsItem.published_at >= start, NewsItem.published_at < end)
    )).scalars().all()
    ranked = sorted(items, key=lambda i: (relevance(i, ticker, label), i.published_at), reverse=True)[:DIGEST_TOP_N]
    sources = [{"n": n, "title": i.title, "link": i.link,
                "published_at": i.published_at.isoformat()} for n, i in enumerate(ranked, 1)]
    digest = NewsDigest(ticker=ticker, week_start=week_start, item_count=len(items),
                        sources=sources, model=settings.OLLAMA_MODEL)
    if not ranked:
        digest.status = "no_news"
        return digest

    payload = [{"n": n, "title": i.title, "description": i.description,
                "date": i.published_at.astimezone(ICT).date().isoformat()} for n, i in enumerate(ranked, 1)]
    allowed = allowed_from_text(*(i.title for i in ranked), *((i.description or "") for i in ranked))
    retry_note = ""
    for attempt in range(DIGEST_RETRIES + 1):
        try:
            out = await generate_json(_DIGEST_PROMPT.format(
                ticker=ticker, label=label or ticker, retry_note=retry_note,
                items=json.dumps(payload, ensure_ascii=False)), _DIGEST_SCHEMA)
        except LlmError as e:
            digest.status, digest.error = "failed", str(e)
            return digest
        bad = unsupported_numbers(out.get("summary") or "", allowed)
        if not bad:
            used = {n for n in out.get("used_items") or [] if isinstance(n, int)}
            for s in sources:
                s["used"] = s["n"] in used
            digest.summary = out["summary"].strip()
            digest.sentiment = out.get("sentiment")
            digest.status = "ok"
            return digest
        retry_note = (f"Your previous answer used numbers not found in the items: {', '.join(bad)}. "
                      "Remove them or use only numbers that appear in the items.")
    digest.status = "failed"
    digest.error = f"unsupported numbers after {DIGEST_RETRIES + 1} attempts: {', '.join(bad)}"
    return digest


async def build_digests(db: AsyncSession, week_start: date | None = None, force: bool = False) -> dict:
    """Weekly digests for every news ticker (default: last complete week). Stored permanently."""
    week_start = week_start or last_full_week_start(datetime.now(ICT).date())
    job = SyncJob(id=uuid.uuid4(), type=f"news_digest:{week_start}",
                  started_at=datetime.now(timezone.utc), status="running")
    db.add(job)
    await db.commit()

    counts = {"ok": 0, "no_news": 0, "failed": 0, "skipped": 0}
    errors: list[str] = []
    for ticker, label in sorted((await news_tickers(db)).items()):
        existing = (await db.execute(select(NewsDigest).where(
            NewsDigest.ticker == ticker, NewsDigest.week_start == week_start))).scalar_one_or_none()
        if existing and existing.status != "failed" and not force:
            counts["skipped"] += 1
            continue
        digest = await build_digest(db, ticker, label, week_start)
        if existing:
            await db.delete(existing)
            await db.flush()
        db.add(digest)
        await db.commit()
        counts[digest.status] += 1
        if digest.status == "failed":
            errors.append(f"{ticker}: {digest.error}")

    job = await db.get(SyncJob, job.id)
    job.completed_at = datetime.now(timezone.utc)
    job.status = "error" if errors else "success"
    job.error_message = "; ".join(errors[:5]) if errors else " ".join(f"{k}:{v}" for k, v in counts.items())
    await db.commit()
    return {"week_start": week_start.isoformat(), **counts, "errors": errors}
