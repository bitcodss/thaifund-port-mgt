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

# Bump whenever parsing rules change, so stored rows can be traced to (and re-parsed by) version.
#   legacy — rows parsed before versions were recorded (several rule sets, 2026-10-16)
#   fs-5   — Thai-script holdings, per-cell headings, security-like quality comparison
PARSER_VERSION = "fs-5"

# "5 อันดับ" incl. pdftotext's dropped vowels/tone marks ("5 อนดบ", "5 อั นดับ")
_ANCHOR = re.compile(r"5\s*อ\S{0,2}น?\S{0,2}ด\S{0,2}บ")
_MASTER_HINT = re.compile(r"กอง\S{0,3}ท?\S{0,3}น?\S{0,3}หล\S{0,2}ก|กองหลัก|master fund", re.I)
_END = re.compile(r"หม\S{0,3}ายเหต|Weblink|ISIN")
# Holding names may be Latin ("NVIDIA CORP") or Thai ("บมจ.ปตท.") — Thai stock and bond funds.
# The name is only the text after the LAST wide gap before the weight, so left-column
# Thai prose on the same layout line never becomes part of it.
_ROW = re.compile(r"(?:^|\s{2,})(\S(?:(?!\s{2}).){1,90}?)\s{2,}(-?\d{1,3}\.\d{1,2})\s*%?\s*$")
_NAME_START = re.compile(r"[A-Za-z\u0e00-\u0e7f]")
_THAI_DEPOSIT = re.compile(r"เง\S{0,2}นฝาก|ใบแสดงส\S{0,2}ทธ")   # deposit / depositary-receipt row prefixes
# Thai table filler that lands in the weight column: other assets, deposits, fees, page text
_THAI_FILLER = re.compile(r"อ\S{0,2}น\s*ๆ|อ\S{0,2}น$|เง\S{0,2}นฝาก|ค\S{0,2}าธรรมเน|หน\S{0,2}า\s*\d|ลดลง|จร\S{0,2}ง$")
# "5 อันดับแรก" headings of allocation tables (industry / country / credit rating) — not holdings
_ALLOCATION_ANCHOR = re.compile(r"อ\S{0,2}ตสาหกรรม|ต\S{0,2}างประเทศ|ประเทศ\s*5|ความน\S{0,2}าเช\S{0,2}อถ\S{0,2}อ")
_ISIN = re.compile(r"ISIN\s*code\s*:\s*([A-Z]{2}[A-Z0-9]{9}\d)")
# Allocation-table rows (countries, sectors) that look like "name  12.34" but aren't holdings
_ALLOCATION_ROW = re.compile(
    r"^(UNITED STATES( OF AMERICA)?|USA|JAPAN|CHINA|TAIWAN|KOREA(, REPUBLIC OF)?|HONG KONG|INDIA|NETHERLANDS|"
    r"IRELAND|LUXEMBOURG|ISRAEL|CANADA|FRANCE|GERMANY|UNITED KINGDOM|SWITZERLAND|DENMARK|SPAIN|AUSTRALIA|"
    r"OTHERS?|CASH|INFORMATION TECHNOLOGY|TECHNOLOGY|HEALTH ?CARE|FINANCIALS|INDUSTRIALS|MATERIALS|ENERGY|"
    r"UTILITIES|REAL ESTATE|COMMUNICATION SERVICES|CONSUMER (DISCRETIONARY|STAPLES|DUR\S*))$", re.I)
_COMPANY_HINT = re.compile(r"\b(Inc|Corp|Corporation|Co|Ltd|Limited|plc|PLC|Holdings?|Group|AG|SA|NV|ASA|"
                           r"KGaA|SE|Bhd|Tbk|Class [A-Z]|[A-Z]{1,5} (US|UW|UN|UQ))\b\.?|"
                           # Thai issuers/instruments, tolerant of pdftotext's dropped vowels
                           r"บมจ|บร\S{0,2}ษ\S{0,2}ท|ธ\S{0,2}นาค|พ\S{0,2}นธ\S{0,2}บ\S{0,2}ต|ห\S{0,2}นก\S{0,2}|"
                           r"ต\S{0,3}วแลกเง|\b[A-Z]{2,5}\d{2,}(?:/\d+)?\b", re.I)
# Fund / ETF units and depositary receipts ("ใบแสดงสิทธิในผลประโยชน์ E1VFVN30") — not stock-level holdings
_FUND_UNIT = re.compile(r"\b(FUNDS?|ETFS?|PORTFOLIO|SICAV|UCITS|TRUST|ISHARES|SPDR|VANGUARD|XTRACKERS|AMUNDI)\b|หน\S{0,2}วยลงท\S{0,2}น|กองท\S{0,2}น|"
                        r"ใบแสดงส\S{0,2}ทธ", re.I)


def parse_top_holdings(text: str) -> dict | None:
    """
    Best "top 5" block in factsheet text → {"rows": [{"name", "pct"}], "master": bool}.
    Prefers blocks of real securities over the feeder's own fund-unit row, and blocks
    labelled as the master fund's holdings. None if no holdings block is found.
    """
    lines = text.split("\n")
    blocks = []
    for i, line in enumerate(lines):
        for m in _ANCHOR.finditer(line):
            # judge the heading by its own cell, not the whole line: a holdings heading can
            # share a layout line with an allocation-table title in another column
            cell_start = max((g.end() for g in re.finditer(r"\s{3,}", line[:m.start()])), default=0)
            if not _ALLOCATION_ANCHOR.search(line[cell_start:m.end()]):
                block = _read_block(lines, i, m)
                if block:
                    blocks.append(block)
    if not blocks:
        return None

    def score(b):
        companies = sum(1 for r in b["rows"]
                        if _COMPANY_HINT.search(r["name"]) and not _FUND_UNIT.search(r["name"]) and r["pct"] < 50)
        return (companies, b["master"], len(b["rows"]))
    best = max(blocks, key=score)
    return best if score(best)[0] > 0 else {"rows": best["rows"], "master": best["master"], "fund_units_only": True}


def _read_block(lines: list[str], i: int, m: re.Match) -> dict | None:
    """Rows under the "5 อันดับ" heading at lines[i] (match m), read in that heading's column."""
    col = max(0, m.start() - 45)
    rows = []
    for nxt in lines[i + 1: i + 16]:
        nm = _ANCHOR.search(nxt)
        if nm and abs(nm.start() - m.start()) < 40:
            break   # next "5 อันดับ" table starts in this column — don't merge two tables
        seg = nxt[col:] if len(nxt) > col else ""
        if rows and _END.search(seg):
            break
        r = _ROW.search(seg)
        if not r:
            continue
        # strip a left-column value glued on with one space ("96.19 NVIDIA CORP", "% Eli Lilly & Co.")
        name = re.sub(r"^[-−%\d.,]+\s+", "", r.group(1).strip())
        latin = re.search(r"[A-Za-z]", name)
        if latin and re.match(r"[฀-๿]", name):
            # Thai text in front of a Latin name is either this row's instrument type
            # ("เงินฝาก The Bank of New York Mellon", "ใบแสดงสิทธิ… E1VFVN30", "หน่วยลงทุน PIMCO…")
            # or left-column prose bleeding in ("…ค่าธรรมเนียม Eli Lilly & Co.", "หน TENCENT…").
            # Only the word right before the name decides the row; prose is stripped.
            words = name[:latin.start()].split()
            prefix_word = words[-1] if words else ""
            if _THAI_DEPOSIT.search(prefix_word):
                continue
            if _FUND_UNIT.search(prefix_word):
                rows.append({"name": name, "pct": float(r.group(2))})
                continue
            name = name[latin.start():]
        elif _FUND_UNIT.search(name) and re.match(r"[฀-๿]", name):
            rows.append({"name": name, "pct": float(r.group(2))})   # Thai-named fund unit
            continue
        if _NAME_START.match(name) and not _ALLOCATION_ROW.match(name) and not _THAI_FILLER.search(name):
            rows.append({"name": name, "pct": float(r.group(2))})
    if rows:
        # a block can mix the feeder's own fund-unit row with the master's securities
        # (>= 50% of NAV in one line is a fund unit even when the name is cut off)
        securities = [r for r in rows if not _FUND_UNIT.search(r["name"]) and r["pct"] < 50]
        context = " ".join(lines[max(0, i - 1): i + 16])
        return {"rows": (securities or rows)[:5], "master": bool(_MASTER_HINT.search(context))}
    return None


_PRIVATE_USE = re.compile("[\ue000-\uf8ff]")   # glyphs pdftotext couldn't map (broken Thai fonts)
GARBLED_PUA_MIN = 20


def holdings_issue(text: str, parsed: dict | None) -> str | None:
    """
    Why no stock-level holdings came out of a factsheet (None = holdings parsed):
      no_text       — PDF has no extractable text (scanned image)
      garbled_thai  — Thai font maps to private-use glyphs, so the "5 อันดับ" heading is unreadable
      no_heading    — readable text but no top-5 holdings heading
      no_rows       — heading found but no "name  weight" rows (holdings drawn as a chart, LHCYBER-style)
      fund_units    — only the feeder's fund/ETF units are listed
    """
    if parsed is not None:
        return "fund_units" if parsed.get("fund_units_only") else None
    if len(text.strip()) < 200:
        return "no_text"
    if any(_ANCHOR.search(line) for line in text.split("\n")):
        return "no_rows"
    if len(_PRIVATE_USE.findall(text)) >= GARBLED_PUA_MIN:
        return "garbled_thai"
    return "no_heading"


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


# "กลุ่ม Technology Equity" / "• กลุม US Equity" / "กลุ่มกองทุนรวม: Health Care" /
# "• กลุมกองทุน : Technology Equity" (vowels often dropped)
# Tone marks may be doubled or follow "ม" ("กลุ่่ม�", "กลุม่") depending on the AMC's PDF fonts.
_AIMC = re.compile(r"กล\S{0,4}ม\S{0,2}(?:กอง\S{0,2}ท\S{0,2}น(?:\S{0,2}รวม)?\S{0,2}\s*:)?\s+"
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


def parse_fields(text: str) -> dict:
    parsed = parse_top_holdings(text)
    raw_cat = parse_aimc_category(text)
    return {
        "text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "holdings": parsed["rows"] if parsed else None,
        "holdings_scope": (None if not parsed else "fund_units" if parsed.get("fund_units_only")
                           else "master" if parsed["master"] else "fund"),
        "holdings_issue": holdings_issue(text, parsed),
        "master_isin": parse_master_isin(text),
        "aimc_category_raw": raw_cat,
        "aimc_category": canonical_category(raw_cat),
        "fx_hedge_pct": parse_fx_hedge_pct(text),
    }


_RATING = re.compile(r"^(AAA|AA|A|BBB|BB|B|GOV|unrated|NAV)\b", re.I)


def is_security_name(name: str) -> bool:
    """A row name that looks like a real security (not a fund unit, ETF, deposit, allocation
    label, credit rating or a truncated fragment like "LTD" / "i Group")."""
    n = (name or "").strip()
    thai = bool(re.match(r"[\u0e00-\u0e7f]", n))
    if not thai and (len(n) < 4 or not n[0].isupper()):
        return False
    return not (_FUND_UNIT.search(n) or _ALLOCATION_ROW.match(n) or _RATING.match(n)
                or _THAI_DEPOSIT.search(n) or _THAI_FILLER.search(n)
                or re.fullmatch(r"(LTD|INC|CORP|PLC|CO|GROUP)\.?", n, re.I))


def _name_key(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", re.sub(r"[\u0e00-\u0e7f]", "", name or "").upper())[:12]


def _stock_rows(scope: str | None, holdings: list | None, known_units: set[str] = frozenset()) -> int:
    """Quality of a stored/parsed holdings block: rows that look like real securities.
    Old and new results are judged by the SAME rules, so junk the old parser accepted
    (fund units, ratings, "NAV Industrials") doesn't count as data worth keeping."""
    if scope not in ("master", "fund"):
        return 0
    return sum(1 for h in holdings or []
               if is_security_name(h.get("name")) and (h.get("pct") or 0) < 50
               and _name_key(h.get("name")) not in known_units)


def _compare(old, new) -> str:
    return "better" if new > old else "worse" if new < old else "same"


_HOLDINGS_FIELDS = ("holdings", "holdings_scope", "holdings_issue")


def apply_parsed(stored: FundFactsheet, text: str, same_document: bool = False) -> dict[str, str]:
    """
    Write parsed fields onto `stored`. When re-parsing the SAME document (same PDF and
    as-of), a result that is worse than what is stored never overwrites it — a parser
    change must not lose data it used to get. A newer document always wins for holdings
    (they genuinely change month to month); a category or hedge % that comes back empty
    keeps the stored value either way. Returns {field: better|same|worse} for re-parses.
    """
    new = parse_fields(text)
    outcome: dict[str, str] = {}
    keep: set[str] = set()
    if same_document:
        # rows the NEW parse identified as fund units ("หน่วยลงทุน PIMCO GIS INCOME") don't count
        # as old stock data either — the old Latin-only parser just couldn't see the Thai prefix
        units = {_name_key(h["name"]) for h in new["holdings"] or [] if _FUND_UNIT.search(h["name"])}
        outcome["holdings"] = _compare(_stock_rows(stored.holdings_scope, stored.holdings, units),
                                       _stock_rows(new["holdings_scope"], new["holdings"]))
        if outcome["holdings"] == "worse":
            keep |= set(_HOLDINGS_FIELDS)
    for field in ("aimc_category", "fx_hedge_pct"):
        old_has, new_has = getattr(stored, field) is not None, new[field] is not None
        if same_document:
            outcome[field] = _compare(old_has, new_has)
        if old_has and not new_has and (same_document or field == "aimc_category"):
            keep.add(field)
            if field == "aimc_category":
                keep.add("aimc_category_raw")
    for field, value in new.items():
        if field not in keep:
            setattr(stored, field, value)
    now = datetime.now(timezone.utc)
    if not keep & set(_HOLDINGS_FIELDS):
        stored.holdings_parser_version, stored.holdings_parsed_at = PARSER_VERSION, now
    if "aimc_category" not in keep:
        stored.aimc_parser_version, stored.aimc_parsed_at = PARSER_VERSION, now
    return outcome


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

    same = stored is not None and stored.pdf_url == url and stored.as_of == as_of
    if stored is None:
        stored = FundFactsheet(sec_proj_id=proj_id)
        db.add(stored)
    stored.pdf_url, stored.as_of = url, as_of
    apply_parsed(stored, text, same_document=same)
    stored.scan_status, stored.scan_error = "ok", None
    stored.fetched_at = datetime.now(timezone.utc)
    await db.flush()
    return stored


SCAN_DELAY_SECONDS = 0.5   # between PDF downloads — be polite to SEC's document storage
SCAN_PROGRESS_EVERY = 50


async def scan_factsheets(db: AsyncSession, force: bool = False, limit: int | None = None,
                          delay: float = SCAN_DELAY_SECONDS, proj_ids: set[str] | None = None) -> dict:
    """
    Parse the latest factsheet of every active (RG) fund project — AIMC category,
    holdings (names + weights only; tickers are resolved for held funds elsewhere),
    master ISIN, actual FX hedge %.

    Resumable: each project's row is its checkpoint. Projects already scanned
    ("ok" / "no_factsheet") are skipped unless force=True; "error" rows are retried.
    Only one scan runs at a time (a running fund_factsheet_scan job blocks others).
    proj_ids restricts the scan to those projects (e.g. re-parse only failures).
    """
    running = (await db.execute(select(SyncJob).where(
        SyncJob.type == "factsheet_scan", SyncJob.status == "running"))).scalars().first()
    if running:
        return {"skipped": "another factsheet scan is running", "job_id": str(running.id)}

    universe = sorted({p for (p,) in (await db.execute(
        select(Fund.sec_proj_id).where(Fund.sec_proj_id.isnot(None), Fund.fund_status == "RG")
    )).all()})
    proj_ids = universe if proj_ids is None else [p for p in universe if p in proj_ids]
    done = set() if force else {p for (p,) in (await db.execute(
        select(FundFactsheet.sec_proj_id).where(FundFactsheet.scan_status.in_(("ok", "no_factsheet")))
    )).all()}
    todo = [p for p in proj_ids if p not in done][:limit]

    job = SyncJob(id=uuid.uuid4(), type="factsheet_scan", started_at=datetime.now(timezone.utc), status="running",
                  error_message=f"0/{len(todo)} (skipped {len(proj_ids) - len(todo)} already scanned)")
    db.add(job)
    await db.commit()

    counts = {"ok": 0, "no_factsheet": 0, "error": 0}
    reparse: dict[str, dict[str, int]] = {}   # field → {better, same, worse} for re-parsed documents
    for n, proj_id in enumerate(todo, 1):
        stored = await db.get(FundFactsheet, proj_id) or FundFactsheet(sec_proj_id=proj_id)
        db.add(stored)
        try:
            latest = await _latest_url(proj_id)
            if latest is None:
                result = "no_factsheet"
                if stored.scan_status != "ok":   # never drop a parsed factsheet because SEC's list is empty today
                    stored.scan_status, stored.scan_error = "no_factsheet", None
            else:
                same = stored.scan_status == "ok" and (stored.pdf_url, stored.as_of) == latest
                text = await _download_text(latest[0])
                stored.pdf_url, stored.as_of = latest
                for field, result in apply_parsed(stored, text, same_document=same).items():
                    reparse.setdefault(field, {"better": 0, "same": 0, "worse": 0})[result] += 1
                stored.scan_status, stored.scan_error = "ok", None
                result = "ok"
                await asyncio.sleep(delay)
        except _FETCH_ERRORS as exc:
            result = "error"
            if stored.scan_status != "ok":   # a failed re-download keeps the stored result
                stored.scan_status = "error"
            stored.scan_error = f"{type(exc).__name__}: {exc}"[:500]
        stored.fetched_at = datetime.now(timezone.utc)
        counts[result] += 1
        if n % SCAN_PROGRESS_EVERY == 0:
            (await db.get(SyncJob, job.id)).error_message = f"{n}/{len(todo)} " + \
                " ".join(f"{k}:{v}" for k, v in counts.items())
        await db.commit()   # checkpoint

    job = await db.get(SyncJob, job.id)
    job.completed_at = datetime.now(timezone.utc)
    job.status = "success"
    job.error_message = (f"{len(todo)}/{len(todo)} " + " ".join(f"{k}:{v}" for k, v in counts.items())
                         + "".join(f" {f}[{r['better']}+/{r['same']}=/{r['worse']}-]" for f, r in reparse.items()))
    await db.commit()
    logger.info("Factsheet scan: %s re-parse: %s", counts, reparse)
    return {"scanned": len(todo), **counts, "reparse": reparse}
