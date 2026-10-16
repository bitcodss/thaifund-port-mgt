"""
SEC-format Thai fund factsheet (PDF) → top holdings, AIMC category, FX hedge %.

Feeder funds' factsheets usually carry a "ทรัพย์สินที่ลงทุน 5 อันดับแรกของกองทุนหลัก"
block — the MASTER fund's top 5 — which is the best source of news tickers for a
feeder (its own SEC top-5 is just the master fund unit). Layout differs per AMC
and pdftotext drops Thai vowels ("5 อนดบแรก"), so parsing is anchored on the
"5 อันดับ" heading and reads rows in that heading's column.

Requires the `pdftotext` binary (poppler-utils).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.fund import Fund
from app.models.market import FundFactsheet
from app.models.tax_lot import SyncJob
from app.services import sec_api
from app.services.aimc import canonical_category

logger = logging.getLogger(__name__)

# "5 อันดับ" incl. pdftotext's dropped vowels/tone marks ("5 อนดบ", "5 อั นดับ")
_ANCHOR = re.compile(r"5\s*อ\S{0,2}น?\S{0,2}ด\S{0,2}บ")
_MASTER_HINT = re.compile(r"กอง\S{0,3}ท?\S{0,3}น?\S{0,3}หล\S{0,2}ก|กองหลัก|master fund", re.I)
_END = re.compile(r"หม\S{0,3}ายเหต|Weblink|ISIN")
_ROW = re.compile(r"([A-Za-z][A-Za-z0-9&.,'()\-/ ]{1,90}?)\s{2,}(-?\d{1,3}\.\d{1,2})\s*%?\s*$")
_ISIN = re.compile(r"ISIN\s*code\s*:\s*([A-Z]{2}[A-Z0-9]{9}\d)")
# Allocation-table rows (countries, sectors) that look like "name  12.34" but aren't holdings
_ALLOCATION_ROW = re.compile(
    r"^(UNITED STATES( OF AMERICA)?|USA|JAPAN|CHINA|TAIWAN|KOREA(, REPUBLIC OF)?|HONG KONG|INDIA|NETHERLANDS|"
    r"IRELAND|LUXEMBOURG|ISRAEL|CANADA|FRANCE|GERMANY|UNITED KINGDOM|SWITZERLAND|DENMARK|SPAIN|AUSTRALIA|"
    r"OTHERS?|CASH|INFORMATION TECHNOLOGY|TECHNOLOGY|HEALTH ?CARE|FINANCIALS|INDUSTRIALS|MATERIALS|ENERGY|"
    r"UTILITIES|REAL ESTATE|COMMUNICATION SERVICES|CONSUMER (DISCRETIONARY|STAPLES|DUR\S*))$", re.I)
_COMPANY_HINT = re.compile(r"\b(Inc|Corp|Corporation|Co|Ltd|Limited|plc|PLC|Holdings?|Group|AG|SA|NV|ASA|"
                           r"KGaA|SE|Bhd|Tbk|Class [A-Z]|[A-Z]{1,5} (US|UW|UN|UQ))\b\.?", re.I)
_FUND_UNIT = re.compile(r"\b(FUND|ETF|PORTFOLIO|SICAV|UCITS|TRUST)\b", re.I)


def parse_top_holdings(text: str) -> dict | None:
    """
    Best "top 5" block in factsheet text → {"rows": [{"name", "pct"}], "master": bool}.
    Prefers blocks of real securities over the feeder's own fund-unit row, and blocks
    labelled as the master fund's holdings. None if no holdings block is found.
    """
    lines = text.split("\n")
    blocks = []
    for i, line in enumerate(lines):
        m = _ANCHOR.search(line)
        if not m:
            continue
        col = max(0, m.start() - 45)
        rows = []
        for nxt in lines[i + 1: i + 16]:
            seg = nxt[col:] if len(nxt) > col else ""
            if rows and _END.search(seg):
                break
            r = _ROW.search(seg)
            if r and not _ALLOCATION_ROW.match(r.group(1).strip()):
                rows.append({"name": r.group(1).strip(), "pct": float(r.group(2))})
        if rows:
            # a block can mix the feeder's own fund-unit row with the master's securities
            # (>= 50% of NAV in one line is a fund unit even when the name is cut off)
            securities = [r for r in rows if not _FUND_UNIT.search(r["name"]) and r["pct"] < 50]
            context = " ".join(lines[max(0, i - 1): i + 16])
            blocks.append({"rows": (securities or rows)[:5], "master": bool(_MASTER_HINT.search(context))})
    if not blocks:
        return None

    def score(b):
        companies = sum(1 for r in b["rows"]
                        if _COMPANY_HINT.search(r["name"]) and not _FUND_UNIT.search(r["name"]) and r["pct"] < 50)
        return (companies, b["master"], len(b["rows"]))
    best = max(blocks, key=score)
    return best if score(best)[0] > 0 else {"rows": best["rows"], "master": best["master"], "fund_units_only": True}


def parse_master_isin(text: str) -> str | None:
    m = _ISIN.search(text)
    return m.group(1) if m else None


async def pdf_to_text(pdf: bytes) -> str:
    proc = await asyncio.create_subprocess_exec(
        "pdftotext", "-layout", "-", "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(pdf)
    if proc.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {err.decode(errors='replace')[:200]}")
    # NFKC: some AMCs' PDFs use ligatures ("Paciﬁc") that break Latin-text matching
    return unicodedata.normalize("NFKC", out.decode("utf-8", errors="replace"))


# "กลุ่ม Technology Equity" / "• กลุม US Equity" / "กลุ่มกองทุนรวม: Health Care" (vowels often dropped)
_AIMC = re.compile(r"กล\S{0,2}ม(?:\S{0,2}กอง\S{0,2}ท\S{0,2}น\S{0,2}รวม\S{0,2}\s*:)?\s+"
                   r"([A-Z][A-Za-z0-9 &/\-\.]+?)(?=\s{2,}|$)", re.M)
_FX_HEDGE_PCT = re.compile(r"FX\s*Hedging\s*:?\s*(-?\d{1,3}(?:\.\d+)?)\s*%", re.I)


def parse_aimc_category(text: str) -> str | None:
    """AIMC category label as printed near the top of page 1 (raw — see aimc.canonical_category)."""
    head = "\n".join(text.split("\n")[:45])
    m = _AIMC.search(head)
    return m.group(1).strip() if m else None


def parse_fx_hedge_pct(text: str) -> Decimal | None:
    """ACTUAL hedge ratio ("FX Hedging 75.96 %") — only some AMCs print it."""
    m = _FX_HEDGE_PCT.search(text)
    return Decimal(m.group(1)) if m else None


def apply_parsed(stored: FundFactsheet, text: str) -> None:
    parsed = parse_top_holdings(text)
    raw_cat = parse_aimc_category(text)
    stored.text_hash = hashlib.sha256(text.encode()).hexdigest()
    stored.holdings = parsed["rows"] if parsed else None
    stored.holdings_scope = (None if not parsed else "fund_units" if parsed.get("fund_units_only")
                             else "master" if parsed["master"] else "fund")
    stored.master_isin = parse_master_isin(text)
    stored.aimc_category_raw = raw_cat
    stored.aimc_category = canonical_category(raw_cat)
    stored.fx_hedge_pct = parse_fx_hedge_pct(text)


async def _latest_url(proj_id: str) -> tuple[str, date | None] | None:
    urls = [u for u in await sec_api.get_factsheet_urls(settings.SEC_API_KEY, proj_id) if u.get("pdf_factsheet")]
    if not urls:
        return None
    latest = max(urls, key=lambda u: u.get("as_of_date") or "")
    as_of = date.fromisoformat(latest["as_of_date"][:10]) if latest.get("as_of_date") else None
    return latest["pdf_factsheet"], as_of


async def _download_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return await pdf_to_text(resp.content)


_FETCH_ERRORS = (sec_api.SecApiError, httpx.HTTPError, RuntimeError, OSError)


async def ensure_factsheet(db: AsyncSession, proj_id: str) -> FundFactsheet | None:
    """
    Stored parsed factsheet for a project, re-downloaded only when SEC lists a newer
    one (as_of changed). Download/parse failures keep the stored copy.
    """
    stored = await db.get(FundFactsheet, proj_id)
    try:
        latest = await _latest_url(proj_id)
        if latest is None:
            return stored
        url, as_of = latest
        if stored and stored.scan_status == "ok" and stored.as_of == as_of and stored.pdf_url == url:
            return stored
        text = await _download_text(url)
    except _FETCH_ERRORS as exc:
        logger.warning("Factsheet fetch failed for %s: %s", proj_id, exc)
        return stored

    if stored is None:
        stored = FundFactsheet(sec_proj_id=proj_id)
        db.add(stored)
    stored.pdf_url, stored.as_of = url, as_of
    apply_parsed(stored, text)
    stored.scan_status, stored.scan_error = "ok", None
    stored.fetched_at = datetime.now(timezone.utc)
    await db.flush()
    return stored


SCAN_DELAY_SECONDS = 0.5   # between PDF downloads — be polite to SEC's document storage
SCAN_PROGRESS_EVERY = 50


async def scan_factsheets(db: AsyncSession, force: bool = False, limit: int | None = None,
                          delay: float = SCAN_DELAY_SECONDS) -> dict:
    """
    Parse the latest factsheet of every active (RG) fund project — AIMC category,
    holdings (names + weights only; tickers are resolved for held funds elsewhere),
    master ISIN, actual FX hedge %.

    Resumable: each project's row is its checkpoint. Projects already scanned
    ("ok" / "no_factsheet") are skipped unless force=True; "error" rows are retried.
    Only one scan runs at a time (a running fund_factsheet_scan job blocks others).
    """
    running = (await db.execute(select(SyncJob).where(
        SyncJob.type == "factsheet_scan", SyncJob.status == "running"))).scalars().first()
    if running:
        return {"skipped": "another factsheet scan is running", "job_id": str(running.id)}

    proj_ids = sorted({p for (p,) in (await db.execute(
        select(Fund.sec_proj_id).where(Fund.sec_proj_id.isnot(None), Fund.fund_status == "RG")
    )).all()})
    done = set() if force else {p for (p,) in (await db.execute(
        select(FundFactsheet.sec_proj_id).where(FundFactsheet.scan_status.in_(("ok", "no_factsheet")))
    )).all()}
    todo = [p for p in proj_ids if p not in done][:limit]

    job = SyncJob(id=uuid.uuid4(), type="factsheet_scan", started_at=datetime.now(timezone.utc), status="running",
                  error_message=f"0/{len(todo)} (skipped {len(proj_ids) - len(todo)} already scanned)")
    db.add(job)
    await db.commit()

    counts = {"ok": 0, "no_factsheet": 0, "error": 0}
    for n, proj_id in enumerate(todo, 1):
        stored = await db.get(FundFactsheet, proj_id) or FundFactsheet(sec_proj_id=proj_id)
        db.add(stored)
        try:
            latest = await _latest_url(proj_id)
            if latest is None:
                stored.scan_status, stored.scan_error = "no_factsheet", None
            else:
                stored.pdf_url, stored.as_of = latest
                apply_parsed(stored, await _download_text(stored.pdf_url))
                stored.scan_status, stored.scan_error = "ok", None
                await asyncio.sleep(delay)
        except _FETCH_ERRORS as exc:
            stored.scan_status, stored.scan_error = "error", f"{type(exc).__name__}: {exc}"[:500]
        stored.fetched_at = datetime.now(timezone.utc)
        counts[stored.scan_status] += 1
        if n % SCAN_PROGRESS_EVERY == 0:
            (await db.get(SyncJob, job.id)).error_message = f"{n}/{len(todo)} " + \
                " ".join(f"{k}:{v}" for k, v in counts.items())
        await db.commit()   # checkpoint

    job = await db.get(SyncJob, job.id)
    job.completed_at = datetime.now(timezone.utc)
    job.status = "success"
    job.error_message = f"{len(todo)}/{len(todo)} " + " ".join(f"{k}:{v}" for k, v in counts.items())
    await db.commit()
    logger.info("Factsheet scan: %s", counts)
    return {"scanned": len(todo), **counts}
