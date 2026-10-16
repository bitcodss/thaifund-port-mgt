"""
Peer-group helpers (Phase 2).

master_key(): one peer per master fund. Thai feeders of the same master are the
same exposure (12 Thai funds feed iShares MSCI ACWI), so peer stats count it once.
The master ISIN parsed from the factsheet is authoritative; the master fund NAME
is only a fallback, because the same master is written differently per share
class and AMC ("…Fund A2 USD" vs "…Fund, Class I (acc) - USD").
"""
from __future__ import annotations

import re
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.fund import Fund
from app.models.market import FundFactsheet, PeerProfile
from app.models.tax_lot import SyncJob
from app.services import sec_api
from collections import Counter

from app.services.aimc import AIMC_CATEGORIES, AIMC_DEFINITIONS
from app.services.llm import LlmError, generate_json
from app.services.peer_taxonomy import MIN_THEME_GROUP, themes_for
from app.services.sec_api import SecApiError


_CURRENCIES = r"USD|EUR|JPY|GBP|HKD|SGD|AUD|CNY|CNH|CHF|THB"
_SHARE_CLASS = re.compile(
    r"\b(CLASS|SHARE CLASS|SHARES?|UNITS?)\b(\s+[A-Z0-9]{1,4}\b)?"      # "Class I", "Class INST", "Shares"
    r"|\b(ACC|ACCUMULATION|ACCUMULATING|DIST|DISTRIBUTION|DISTRIBUTING|INST|INSTITUTIONAL|"
    r"HEDGED|UNHEDGED|H|UH)\b"
    r"|\b(" + _CURRENCIES + r")\b"
    r"|\b(?=[A-Z]*\d)[A-Z]{1,2}\d{1,2}\b"                               # A2, I1, X1, IA2 …
    r"|\b(I|IA|IB|A|B|C|D|E|F|P|R|S|T|W|X|Y|Z)\b(?=\s*$)",              # trailing single-letter class
    re.I,
)


def normalise_master_name(name: str | None) -> str | None:
    if not name:
        return None
    n = re.sub(r"[฀-๿]+", " ", name)            # Thai text ("กองทุน …")
    n = re.sub(r"[–—]", "-", n)
    n = re.sub(r"[^A-Za-z0-9 ]+", " ", n).upper()
    prev = None
    while prev != n:                                       # class tokens can be stacked
        prev = n
        n = re.sub(r"\s+", " ", _SHARE_CLASS.sub(" ", n)).strip()
    return n or None


def master_key(master_isin: str | None, master_name: str | None) -> tuple[str, str] | None:
    """("isin", ISIN) when known, else ("name", normalised name); None for non-feeders."""
    if master_isin:
        return "isin", master_isin.strip().upper()
    norm = normalise_master_name(master_name)
    return ("name", norm) if norm else None


# ── peer profiles (chunk 3) ───────────────────────────────────────────────────

logger = logging.getLogger(__name__)

# Inference may only pick a report category — "Miscellaneous" says nothing about peers.
INFERABLE_CATEGORIES = [c for c in AIMC_CATEGORIES if c != "Miscellaneous"]
_PASSIVE_STYLES = {"PM", "PN"}
# share classes that are never the representative retail class (fees / eligibility differ)
_EXCLUDED_CLASS = re.compile(r"สถาบ|กล\S{0,2}ม\S{0,2}/\S{0,2}บ|กล\S{0,2}มพ\S{0,2}เศษ|ควบประก|เพ\S{0,2}อการออม|"
                             r"ไทยเพ\S{0,2}อความย|ครบเง\S{0,2}อนไขทางภาษ")
_CLASS_PREFERENCE = [
    re.compile(r"สะสมม\S{0,2}ลค"),                  # accumulation
    re.compile(r"ท\S{0,2}วไป"),                     # general / general investors
    None,                                           # main / no detail
    re.compile(r"อ\S{0,2}เล\S{0,2}กทรอน"),          # online channel
    re.compile(r"จ\S{0,2}ายเง\S{0,2}นป\S{0,2}นผล"), # dividend
    re.compile(r"ร\S{0,2}บซ\S{0,2}อค\S{0,2}น|ขายค\S{0,2}น"),  # auto-redemption
]


def _class_code(row: dict) -> str:
    cls = (row.get("fund_class_name") or "").strip()
    return cls if cls and cls.lower() not in ("main", "-") else (row.get("proj_abbr_name") or "").strip()


def representative_class(profiles: list[dict]) -> str | None:
    """Main retail share class of a project (its NAV represents the project in peer stats)."""
    if not profiles:
        return None
    eligible = [p for p in profiles if not (p.get("fund_class_tax_incentive_type") or "").strip()
                and not _EXCLUDED_CLASS.search(p.get("fund_class_detail") or "")]
    pool = eligible or profiles          # e.g. RMF-only project: every class carries the tax incentive
    for pref in _CLASS_PREFERENCE:
        for p in pool:
            detail = p.get("fund_class_detail") or ""
            if (pref is None and (not detail or (p.get("fund_class_name") or "").lower() in ("main", "-"))) \
                    or (pref is not None and pref.search(detail)):
                return _class_code(p)
    return _class_code(pool[0])


def hedge_group(policy: str | None, invest_country_flag: str | None = None) -> str:
    p = (policy or "").lower()
    if "ไม่ป้องกัน" in p or "unhedged" in p:
        return "unhedged"
    if "ทั้งหมด" in p or "fully" in p:
        return "full"
    if "ดุลยพินิจ" in p or "dynamic" in p:
        return "dynamic"
    if "บางส่วน" in p or "partial" in p:
        return "partial"
    return "domestic" if invest_country_flag == "3" else "unknown"


_THEME_PROMPT = """Classify this Thai mutual fund into exactly one peer theme within its AIMC category "{category}".
Allowed themes: {themes}
Also say whether it is an ESG / sustainability fund (is_esg).
Rules: choose by what the fund (or its master fund) actually invests in. A passive index fund still gets the theme of its index.
Fund (JSON):
{data}"""

_AIMC_PROMPT = """This Thai mutual fund's factsheet prints no AIMC category. Choose the single AIMC category it belongs to.
Categories (name: what belongs there):
{categories}
Rules:
- Decide by what the fund or its master fund actually holds and where.
- Use a sector category ONLY when the mandate is that sector. Broad or multi-theme funds go to their region's category.
- Thai equity funds (including Thai ESG / ThaiESG / ThaiESGX funds) are Thai categories, never property or freehold.
Fund (JSON):
{data}"""


async def classify_theme(category: str, themes: list[str], data: dict) -> dict:
    out = await generate_json(
        _THEME_PROMPT.format(category=category, themes=", ".join(themes), data=json.dumps(data, ensure_ascii=False)),
        {"type": "object", "properties": {"theme": {"type": "string", "enum": themes},
                                           "is_esg": {"type": "boolean"}, "reason": {"type": "string"}},
         "required": ["theme", "is_esg", "reason"]})
    if out.get("theme") not in themes:
        raise LlmError(f"theme outside the allowed list: {out}")
    return out


async def infer_aimc_category(data: dict) -> dict:
    out = await generate_json(
        _AIMC_PROMPT.format(categories="\n".join(f"- {c}: {AIMC_DEFINITIONS[c]}" for c in INFERABLE_CATEGORIES),
                            data=json.dumps(data, ensure_ascii=False)),
        {"type": "object", "properties": {"category": {"type": "string", "enum": INFERABLE_CATEGORIES},
                                           "reason": {"type": "string"}},
         "required": ["category", "reason"]})
    if out.get("category") not in INFERABLE_CATEGORIES:
        raise LlmError(f"category outside the allowed list: {out}")
    return out


def _policy_text(profile: dict) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", profile.get("investment_policy_desc") or "")).strip()[:1200]


async def build_peer_profiles(db: AsyncSession, proj_ids: set[str] | None = None, force: bool = False) -> dict:
    """
    Build / refresh peer profiles for active projects with a parsed factsheet (default: all).
    LLM work only where needed: an inferred AIMC category for uncategorised FOREIGN EQUITY
    funds, and a theme for categories that have a theme list. Re-runs skip projects whose
    inputs are unchanged; manual theme overrides are kept.
    """
    key = settings.SEC_API_KEY
    q = select(FundFactsheet).where(FundFactsheet.scan_status == "ok")
    if proj_ids is not None:
        q = q.where(FundFactsheet.sec_proj_id.in_(proj_ids))
    factsheets = {f.sec_proj_id: f for f in (await db.execute(q)).scalars()}
    active = {p for (p,) in (await db.execute(
        select(Fund.sec_proj_id).where(Fund.fund_status == "RG", Fund.sec_proj_id.isnot(None)))).all()}
    todo = sorted(p for p in factsheets if p in active)

    job = SyncJob(id=uuid.uuid4(), type="peer_profiles", started_at=datetime.now(timezone.utc), status="running",
                  error_message=f"0/{len(todo)}")
    db.add(job)
    await db.commit()

    counts = {"built": 0, "unchanged": 0, "inferred": 0, "themed": 0, "error": 0}
    errors: list[str] = []
    for n, proj_id in enumerate(todo, 1):
        fs_row = factsheets[proj_id]
        try:
            profiles = await sec_api.get_fund_profiles(key, proj_id)
            bench = " / ".join(dict.fromkeys(
                (b.get("benchmark") or "").strip() for b in await sec_api.get_benchmarks(key, proj_id)
                if b.get("benchmark")))[:600]
        except SecApiError as e:
            counts["error"] += 1
            errors.append(f"{proj_id}: {e}")
            continue
        if not profiles:
            continue
        base = profiles[0]
        data = {
            "fund": base.get("proj_abbr_name"), "name_en": base.get("proj_name_en"),
            "asset_type": base.get("policy_desc"), "master_fund": base.get("feederfund_master_fund"),
            "benchmark": bench or None, "policy": _policy_text(base),
            "top_holdings": [h.get("name") for h in (fs_row.holdings or [])][:5]
            if fs_row.holdings_scope in ("master", "fund") else [],
        }
        h = hashlib.sha256(json.dumps([data, fs_row.aimc_category], ensure_ascii=False, sort_keys=True)
                           .encode()).hexdigest()
        prof = await db.get(PeerProfile, proj_id) or PeerProfile(sec_proj_id=proj_id)
        db.add(prof)

        # deterministic fields — refreshed every run
        rep = representative_class(profiles)
        rep_row = next((p for p in profiles if _class_code(p) == rep), base)
        prof.representative_code = rep
        prof.hedge_group = hedge_group(rep_row.get("exchange_rate_protection_policy"), base.get("invest_country_flag"))
        prof.is_passive = base.get("management_style") in _PASSIVE_STYLES
        mk = master_key(fs_row.master_isin, base.get("feederfund_master_fund"))
        prof.master_key_kind, prof.master_key = mk if mk else (None, None)

        if prof.input_hash == h and not force:
            counts["unchanged"] += 1
            await db.commit()
            continue
        try:
            siblings = Counter(c for (c,) in (await db.execute(
                select(PeerProfile.aimc_category).where(PeerProfile.master_key == prof.master_key,
                                                        PeerProfile.sec_proj_id != proj_id,
                                                        PeerProfile.aimc_source == "parsed"))).all()) \
                if prof.master_key else Counter()
            if prof.aimc_source == "manual":
                pass                                           # admin category override survives sync
            elif fs_row.aimc_category:
                prof.aimc_category, prof.aimc_source, prof.aimc_reason = fs_row.aimc_category, "parsed", None
            elif siblings:
                # other Thai feeders of the same master print a category — inherit the majority
                cat, n = siblings.most_common(1)[0]
                prof.aimc_category, prof.aimc_source = cat, "inferred"
                prof.aimc_reason = f"Same master fund as {n} fund(s) whose factsheets say {cat}"
                counts["inferred"] += 1
            elif base.get("policy_desc") == "ตราสารทุน" and base.get("invest_country_flag") in ("1", "4"):
                out = await infer_aimc_category(data)
                prof.aimc_category, prof.aimc_source, prof.aimc_reason = out["category"], "inferred", out["reason"]
                counts["inferred"] += 1
            else:
                prof.aimc_category, prof.aimc_source, prof.aimc_reason = None, None, None

            themes = themes_for(prof.aimc_category)
            if prof.theme_source == "manual" and (themes is None or prof.peer_theme in themes):
                pass                                           # admin override survives sync
            elif themes is None:
                prof.peer_theme, prof.theme_source, prof.theme_reason = prof.aimc_category, "fixed", None
                prof.is_esg = bool(re.search(r"\bESG\b|sustainab|ยั่งยืน", f"{data['name_en']} {data['policy']}", re.I))
            else:
                out = await classify_theme(prof.aimc_category, themes, data)
                prof.peer_theme, prof.theme_source, prof.theme_reason = out["theme"], "ai", out["reason"]
                prof.is_esg = bool(out["is_esg"])
                counts["themed"] += 1
        except LlmError as e:
            counts["error"] += 1
            errors.append(f"{proj_id}: {e}")
            await db.commit()                                  # keep deterministic fields; retry LLM next run
            continue
        prof.input_hash, prof.model, prof.classified_at = h, settings.OLLAMA_MODEL, datetime.now(timezone.utc)
        counts["built"] += 1
        if n % 50 == 0:
            (await db.get(SyncJob, job.id)).error_message = f"{n}/{len(todo)} " + \
                " ".join(f"{k}:{v}" for k, v in counts.items())
        await db.commit()

    job = await db.get(SyncJob, job.id)
    job.completed_at = datetime.now(timezone.utc)
    job.status = "error" if errors else "success"
    job.error_message = (f"{len(todo)}/{len(todo)} " + " ".join(f"{k}:{v}" for k, v in counts.items())
                         + (f" | {'; '.join(errors[:3])}" if errors else ""))
    await db.commit()
    return {"projects": len(todo), **counts, "errors": errors}


# ── peer groups ───────────────────────────────────────────────────────────────

def _member_key(p: PeerProfile) -> str:
    """One peer per master fund; direct-stock / no-master funds are their own peer."""
    return f"{p.master_key_kind}:{p.master_key}" if p.master_key else f"proj:{p.sec_proj_id}"


async def peer_group(db: AsyncSession, proj_id: str) -> dict | None:
    """
    Peers of one project: its theme group when that has >= MIN_THEME_GROUP distinct
    masters, otherwise its whole AIMC category. Members are grouped per master.
    """
    me = await db.get(PeerProfile, proj_id)
    if me is None or not me.aimc_category:
        return None
    in_cat = (await db.execute(select(PeerProfile).where(PeerProfile.aimc_category == me.aimc_category))).scalars().all()
    in_theme = [p for p in in_cat if p.peer_theme == me.peer_theme]
    theme_masters = {_member_key(p) for p in in_theme}
    basis, members = ("theme", in_theme) if len(theme_masters) >= MIN_THEME_GROUP else ("category", in_cat)

    names = {pid: code for pid, code in (await db.execute(
        select(Fund.sec_proj_id, Fund.fund_code).where(Fund.sec_proj_id.in_({p.sec_proj_id for p in members}))
        .order_by(Fund.fund_code))).all()}
    grouped: dict[str, dict] = {}
    for p in members:
        g = grouped.setdefault(_member_key(p), {"master_key": p.master_key, "master_key_kind": p.master_key_kind,
                                                "funds": []})
        g["funds"].append({"sec_proj_id": p.sec_proj_id, "fund_code": names.get(p.sec_proj_id),
                           "representative_code": p.representative_code, "hedge_group": p.hedge_group,
                           "is_passive": p.is_passive, "is_esg": p.is_esg, "peer_theme": p.peer_theme,
                           "aimc_source": p.aimc_source, "is_self": p.sec_proj_id == proj_id})
    return {
        "sec_proj_id": proj_id, "aimc_category": me.aimc_category, "aimc_source": me.aimc_source,
        "aimc_reason": me.aimc_reason, "peer_theme": me.peer_theme, "theme_source": me.theme_source,
        "theme_reason": me.theme_reason, "basis": basis, "theme_masters": len(theme_masters),
        "masters": sorted(grouped.values(), key=lambda g: (not any(f["is_self"] for f in g["funds"]),
                                                             -len(g["funds"]), g["master_key"] or "")),
    }
