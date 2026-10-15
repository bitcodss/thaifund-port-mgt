"""
SEC-format Thai fund factsheet (PDF) → top holdings.

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
from datetime import date, datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.market import FundFactsheet
from app.services import sec_api

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
    return out.decode("utf-8", errors="replace")


async def ensure_factsheet(db: AsyncSession, proj_id: str) -> FundFactsheet | None:
    """
    Stored parsed factsheet for a project, re-downloaded only when SEC lists a newer
    one (as_of changed). Download/parse failures keep the stored copy.
    """
    stored = await db.get(FundFactsheet, proj_id)
    try:
        urls = [u for u in await sec_api.get_factsheet_urls(settings.SEC_API_KEY, proj_id) if u.get("pdf_factsheet")]
        if not urls:
            return stored
        latest = max(urls, key=lambda u: u.get("as_of_date") or "")
        as_of = date.fromisoformat(latest["as_of_date"][:10]) if latest.get("as_of_date") else None
        if stored and stored.as_of == as_of and stored.pdf_url == latest["pdf_factsheet"]:
            return stored
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(latest["pdf_factsheet"])
            resp.raise_for_status()
        text = await pdf_to_text(resp.content)
    except (sec_api.SecApiError, httpx.HTTPError, RuntimeError, OSError) as exc:
        logger.warning("Factsheet fetch failed for %s: %s", proj_id, exc)
        return stored

    parsed = parse_top_holdings(text)
    if stored is None:
        stored = FundFactsheet(sec_proj_id=proj_id)
        db.add(stored)
    stored.pdf_url = latest["pdf_factsheet"]
    stored.as_of = as_of
    stored.text_hash = hashlib.sha256(text.encode()).hexdigest()
    stored.holdings = parsed["rows"] if parsed else None
    stored.holdings_scope = (None if not parsed else "fund_units" if parsed.get("fund_units_only")
                             else "master" if parsed["master"] else "fund")
    stored.master_isin = parse_master_isin(text)
    stored.fetched_at = datetime.now(timezone.utc)
    await db.flush()
    return stored
