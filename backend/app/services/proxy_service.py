"""
Proxy tickers per SEC fund project, for the monthly report.

- kind="price": index/ETF whose price shows what the fund's market did.
  Must have >= MIN_HISTORY_DAYS of Yahoo history (some indices, e.g. China
  STAR 50, return almost nothing).
- kind="news": tickers to collect headlines for, taken from REAL holdings:
    source="holding"   — direct-stock fund: its own SEC top-5 holdings
    source="factsheet" — feeder fund: the master fund's top 5 from the Thai
                         SEC factsheet PDF (factsheet_service)
    source="ai"        — fallback ONLY when the holdings route yields fewer than
                         MIN_HOLDING_TICKERS tickers with Yahoo news (no holdings
                         source, ETF/fund units only, …). Never mixed with holding
                         tickers; exposed as unverified in the API/UI.
  Index tickers (^GSPC) are never news proxies — their feeds are noise.

Every ticker is validated against Yahoo. Admin rows (source="manual") for a kind
are never touched by sync. Re-suggestion runs only when the fund's inputs
(incl. factsheet holdings) change, or force=True.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fund import Fund
from app.models.market import FundFactsheet, FundProxy
from app.models.tax_lot import SyncJob
from app.services import yahoo_client
from app.services.factsheet_service import ensure_factsheet
from app.services.llm import LlmError, generate_json
from app.services.sync_service import get_portfolio_proj_ids
from app.services.yahoo_client import YahooError

logger = logging.getLogger(__name__)

KINDS = ("price", "news")
MIN_HISTORY_DAYS = 200
# A news proxy is only useful if Yahoo actually publishes about it: broad region
# ETFs (EEM, EWJ, URTH…) often have no headlines for weeks.
MIN_RECENT_NEWS = 3
RECENT_NEWS_DAYS = 14
MAX_PRICE_PROXIES = 2
MAX_NEWS_PROXIES = 5
MIN_HOLDING_TICKERS = 2
HOLDING_SOURCES = ("holding", "factsheet")
_US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "BTS", "PCX", "NAS", "NYS"}
_US_OTC = {"PNK"}   # ADRs like TCEHY, SSNLF
# Regional cross-listings / depositary receipts — never preferred over a primary listing
_SECONDARY_EXCHANGES = {"FRA", "STU", "MUN", "DUS", "HAM", "HAN", "BER", "VIE", "SAO", "MEX", "IOB", "CXE",
                        "DXE", "SGO", "TLO", "BUE", "SES"}
# Legal-form noise: Yahoo search on "Eli Lilly & Co" returns only Vienna/Milan listings,
# "Eli Lilly" returns LLY first.
_LEGAL_SUFFIX = re.compile(r"\b(co|ltd|inc|corp|corporation|plc|sa|ag|nv|as|asa|kgaa|se|limited|company|"
                           r"class [a-z]|bhd|tbk)\b\.?|/the|[,&]|\.(?=\s|$)", re.I)   # keep dots inside words: AMAZON.COM
_BLOOMBERG_TICKER = re.compile(r"^([A-Z][A-Z0-9.]{0,5})\s+(US|UW|UN|UQ|UR)$")   # "LRCX US"
_FUND_UNIT = re.compile(r"\b(FUND|ETF|PORTFOLIO|SICAV|UCITS|TRUST)\b|หน่วยลงทุน", re.I)
_GENERIC_WORDS = {"corp", "corporation", "company", "holding", "holdings", "group", "limited", "class",
                  "international", "global", "industries", "technology", "technologies", "financial",
                  "systems", "the", "inc", "ltd", "plc"}
# SEC management_style codes for Thai feeders of a master fund — their "top holding"
# is the master fund itself, not stocks.
_FEEDER_STYLES = {"AN", "PN", "IN", "LN"}

_PROMPT = """You pick market tickers (Yahoo Finance symbols) that represent a Thai mutual fund.

price_proxies (1-2): an index or broad ETF whose price best tracks this fund's market and benchmark.
  Prefer long-history, liquid symbols (e.g. ^GSPC, SPY, ACWI, URTH, EEM, AAXJ, EWJ, ^N225, SMH, XLV, ICLN, GDX, CIBR, MCHI).
news_proxies (1-3): symbols whose news explains this fund's moves: a liquid US-listed theme ETF or major stocks in the theme.
  NEVER use index symbols starting with "^" for news.
  Broad region/country ETFs (EEM, EWJ, URTH, EFA, AAXJ, MCHI, INDA) get almost no news coverage: for broad-market
  region funds use the region's 2-3 largest US-listed stocks or ADRs instead (e.g. Asia: TSM, BABA; Japan: TM, SONY).
  (news_proxies are only used when the fund's real holdings can't be resolved.)
If the master fund is itself an ETF, include it or its US-listed twin. Use only real Yahoo Finance symbols.

Fund (JSON):
{data}"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "price_proxies": {"type": "array", "minItems": 1, "maxItems": MAX_PRICE_PROXIES, "items": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}, "name": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["ticker", "name", "reason"]}},
        "news_proxies": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}, "name": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["ticker", "name", "reason"]}},
    },
    "required": ["price_proxies", "news_proxies"],
}


def holds_stocks_directly(fund: Fund) -> bool:
    if fund.management_style in _FEEDER_STYLES:
        return False
    names = [h.get("name") or "" for h in (fund.top_holdings or [])]
    return bool(names) and not any(n.startswith("หน่วยลงทุน") for n in names)


def proxy_inputs(fund: Fund, factsheet: FundFactsheet | None = None) -> dict:
    return {
        "fund": fund.fund_code,
        "name_en": fund.name_en,
        "theme": fund.theme,
        "region": fund.region,
        "asset_class": fund.asset_class,
        "master_fund": fund.master_fund,
        "benchmark": fund.benchmark,
        "top_holdings": [h.get("name") for h in (fund.top_holdings or [])],
        "master_top_holdings": [r.get("name") for r in (factsheet.holdings or [])]
        if factsheet and factsheet.holdings_scope in ("master", "fund") else [],
    }


def inputs_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _name_words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z]{4,}", text or "")} - _GENERIC_WORDS


def search_name(holding: str) -> str:
    return re.sub(r"\s+", " ", _LEGAL_SUFFIX.sub(" ", holding)).strip()


def _listing_rank(exchange: str | None) -> int:
    if exchange in _US_EXCHANGES:
        return 0
    if exchange in _US_OTC:
        return 1
    if exchange in _SECONDARY_EXCHANGES:
        return 3
    return 2   # home-market primary listing (005930.KS, 6861.T, VWS.CO)


async def resolve_holding(name: str) -> dict | None:
    """
    Stock holding name → Yahoo equity that (a) shares a distinctive word with the
    holding name (so a truncated "net, Inc." can't match a random company) and
    (b) passes the news check. Listings are tried US exchange → US OTC ADR →
    home primary → regional cross-listing (better news coverage first).
    Fund units and ETFs return None — they aren't stock-level holdings.
    """
    if _FUND_UNIT.search(name or ""):
        return None
    bbg = _BLOOMBERG_TICKER.match((name or "").strip())
    query = bbg.group(1) if bbg else search_name(name or "")
    try:
        quotes = await yahoo_client.search(query, limit=8)
    except YahooError:
        return None
    if bbg:
        candidates = [q for q in quotes if q["symbol"] == bbg.group(1) and q["quote_type"] == "EQUITY"]
    else:
        words = _name_words(query)
        candidates = [q for q in quotes if q["quote_type"] == "EQUITY" and words & _name_words(q["name"] or "")]
        candidates.sort(key=lambda q: _listing_rank(q["exchange"]))   # stable: keeps Yahoo order within a rank
    for q in candidates[:3]:
        if (await validate("news", q["symbol"]))[0]:
            return q
    return None


async def validate(kind: str, ticker: str) -> tuple[bool, int | None]:
    """price: needs MIN_HISTORY_DAYS of closes. news: non-index symbol with
    >= MIN_RECENT_NEWS headlines in the last RECENT_NEWS_DAYS."""
    try:
        if kind == "price":
            days = len(await yahoo_client.history(ticker, "2y"))
            return days >= MIN_HISTORY_DAYS, days
        if ticker.startswith("^"):
            return False, None
        cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_NEWS_DAYS)
        recent = [i for i in await yahoo_client.rss(ticker) if i["published_at"] >= cutoff]
        return len(recent) >= MIN_RECENT_NEWS, None
    except YahooError:
        return False, None


async def news_from_holdings(fund: Fund, factsheet: FundFactsheet | None) -> tuple[list[dict], str | None]:
    """Verified news tickers from real holdings, or ([], why-not) when the route fails."""
    if holds_stocks_directly(fund):
        rows, source, scope = fund.top_holdings or [], "holding", "fund"
    elif factsheet and factsheet.holdings_scope in ("master", "fund") and factsheet.holdings:
        rows, source, scope = factsheet.holdings, "factsheet", factsheet.holdings_scope
    elif factsheet and factsheet.holdings_scope == "fund_units":
        return [], "factsheet lists only fund/ETF units, no stock-level holdings"
    else:
        return [], "no stock-level holdings source (factsheet has no top-5 block)"

    out: list[dict] = []
    for h in rows[:5]:
        n = h.get("name") or ""
        q = await resolve_holding(n)
        if q and all(q["symbol"] != o["ticker"] for o in out):
            label = "master-fund top holding" if scope == "master" else "top holding"
            out.append({"ticker": q["symbol"], "label": q["name"] or n, "source": source,
                        "reason": f"{label}: {n}", "valid": True, "weight": h.get("pct")})
    if len(out) < MIN_HOLDING_TICKERS:
        return [], f"only {len(out)} of {len(rows)} holdings resolved to tickers with Yahoo news"
    return out, None


async def suggest(fund: Fund, factsheet: FundFactsheet | None = None) -> dict[str, list[dict]]:
    """Candidate proxies per kind: [{ticker, label, source, reason, valid?}]."""
    out: dict[str, list[dict]] = {"price": [], "news": []}
    out["news"], why_not = await news_from_holdings(fund, factsheet)
    llm = await generate_json(
        _PROMPT.format(data=json.dumps(proxy_inputs(fund, factsheet), ensure_ascii=False)), _SCHEMA,
    )
    for p in llm.get("price_proxies") or []:
        t = (p.get("ticker") or "").strip().upper()
        if t and all(t != c["ticker"] for c in out["price"]):
            out["price"].append({"ticker": t, "label": p.get("name"), "source": "ai", "reason": p.get("reason")})
    if not out["news"]:
        # Fallback only — never mixed with holding tickers; flagged unverified downstream.
        for p in llm.get("news_proxies") or []:
            t = (p.get("ticker") or "").strip().upper()
            if t and all(t != c["ticker"] for c in out["news"]):
                out["news"].append({"ticker": t, "label": p.get("name"), "source": "ai",
                                    "reason": f"Unverified LLM fallback ({why_not}): {p.get('reason')}"})
    out["price"] = out["price"][:MAX_PRICE_PROXIES]
    out["news"] = out["news"][:MAX_NEWS_PROXIES]
    return out


async def refresh_proxies(db: AsyncSession, proj_ids: set[str] | None = None, force: bool = False) -> dict:
    """Suggest + validate proxies for SEC projects (default: all held in portfolios)."""
    if proj_ids is None:
        proj_ids = await get_portfolio_proj_ids(db)
    if not proj_ids:
        return {"updated": 0, "unchanged": 0, "errors": []}

    job = SyncJob(id=uuid.uuid4(), type="fund_proxies", started_at=datetime.now(timezone.utc), status="running")
    db.add(job)
    await db.commit()

    updated = unchanged = 0
    errors: list[str] = []
    for proj_id in sorted(proj_ids):
        fund = (await db.execute(
            select(Fund).where(Fund.sec_proj_id == proj_id).order_by(Fund.fund_code).limit(1)
        )).scalar_one_or_none()
        if fund is None:
            continue
        existing = (await db.execute(select(FundProxy).where(FundProxy.sec_proj_id == proj_id))).scalars().all()
        factsheet = None if holds_stocks_directly(fund) else await ensure_factsheet(db, proj_id)
        h = inputs_hash(proxy_inputs(fund, factsheet))
        kinds_to_do = [
            k for k in KINDS
            if not any(p.kind == k and p.source == "manual" for p in existing)
            and (force or not any(p.kind == k for p in existing)
                 or any(p.kind == k and p.input_hash != h for p in existing))
        ]
        if not kinds_to_do:
            unchanged += 1
            continue
        try:
            candidates = await suggest(fund, factsheet)
        except LlmError as e:
            errors.append(f"{fund.fund_code}: {e}")
            continue

        now = datetime.now(timezone.utc)
        for kind in kinds_to_do:
            await db.execute(delete(FundProxy).where(
                FundProxy.sec_proj_id == proj_id, FundProxy.kind == kind, FundProxy.source != "manual"))
            for c in candidates[kind]:
                ok, days = (True, None) if c.get("valid") else await validate(kind, c["ticker"])
                db.add(FundProxy(
                    sec_proj_id=proj_id, kind=kind, ticker=c["ticker"], label=(c["label"] or "")[:200] or None,
                    source=c["source"], reason=c["reason"], valid=ok, history_days=days,
                    input_hash=h, validated_at=now,
                    weight_pct=Decimal(str(c["weight"])) if c.get("weight") is not None else None,
                ))
            if not any(c for c in candidates[kind]):
                errors.append(f"{fund.fund_code}: no {kind} proxy suggested")
        updated += 1
        await db.commit()

    job = await db.get(SyncJob, job.id)
    job.completed_at = datetime.now(timezone.utc)
    job.status = "error" if errors else "success"
    job.error_message = "; ".join(errors[:5]) if errors else f"updated:{updated} unchanged:{unchanged}"
    await db.commit()
    logger.info("Proxy refresh: updated=%d unchanged=%d errors=%d", updated, unchanged, len(errors))
    return {"updated": updated, "unchanged": unchanged, "errors": errors}


OVERRIDE_STALE_DAYS = 90


async def set_manual(
    db: AsyncSession, proj_id: str, kind: str, tickers: list[str],
    source_note: str | None = None, source_as_of: date | None = None,
    weights: dict[str, Decimal] | None = None,
) -> list[FundProxy]:
    """
    Admin override for one kind. An empty list removes the override (sync re-suggests).
    source_note / source_as_of record where the tickers came from (e.g. "master fund
    factsheet", 2026-09-30) — overrides older than OVERRIDE_STALE_DAYS are flagged stale.
    weights: {ticker: % of NAV of the underlying holding} from that same source.
    """
    await db.execute(delete(FundProxy).where(FundProxy.sec_proj_id == proj_id, FundProxy.kind == kind))
    rows = []
    now = datetime.now(timezone.utc)
    for t in dict.fromkeys(t.strip().upper() for t in tickers if t.strip()):
        ok, days = await validate(kind, t)
        row = FundProxy(sec_proj_id=proj_id, kind=kind, ticker=t, source="manual",
                        reason="Set manually by admin", valid=ok, history_days=days, validated_at=now,
                        source_note=(source_note or None) and source_note[:300], source_as_of=source_as_of,
                        weight_pct={k.strip().upper(): v for k, v in (weights or {}).items()}.get(t))
        db.add(row)
        rows.append(row)
    await db.commit()
    return rows
