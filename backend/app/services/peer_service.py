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
from app.services.currency import currency_from_text
from app.services.peer_taxonomy import broad_index_theme, MIN_THEME_GROUP, themes_for
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
Rules: choose by what the fund (or its master fund) actually invests in. A passive index fund still gets the
theme of its index. A fund investing in a single country gets "Single Country" when that theme is allowed.
Fund (JSON):
{data}"""


async def classify_theme(category: str, themes: list[str], data: dict, model: str | None = None) -> str:
    out = await generate_json(
        _THEME_PROMPT.format(category=category, themes=", ".join(themes), data=json.dumps(data, ensure_ascii=False)),
        {"type": "object", "properties": {"theme": {"type": "string", "enum": themes}, "reason": {"type": "string"}},
         "required": ["theme", "reason"]}, model=model)
    if out.get("theme") not in themes:
        raise LlmError(f"theme outside the allowed list: {out}")
    return out["theme"]


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
    Pass 1 (per project): representative class, hedge group, passive, master key, ESG (SEC "SRI"
    designation), effective AIMC category (parsed / same-master inherited / LLM-inferred for
    uncategorised foreign equity; manual kept) and the classifier input.
    Pass 2: themes per master (assign_themes). Manual overrides are never replaced.
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

    counts = {"built": 0, "unchanged": 0, "inherited": 0, "inferred": 0, "error": 0}
    errors: list[str] = []
    for n, proj_id in enumerate(todo, 1):
        fs_row = factsheets[proj_id]
        try:
            profiles = await sec_api.get_fund_profiles(key, proj_id)
            bench = " / ".join(dict.fromkeys(
                (b.get("benchmark") or "").strip() for b in await sec_api.get_benchmarks(key, proj_id)
                if b.get("benchmark")))[:600]
            specs = {sp.get("spec_code") for sp in await sec_api.get_specifications(key, proj_id)}
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
        prof.hedge_source = "sec"
        pct = fs_row.fx_hedge_pct
        contradicts = pct is not None and ((fs_row.fx_hedge_policy == "unhedged" and pct > 0)
                                           or (fs_row.fx_hedge_policy == "full" and pct < 50))
        if prof.hedge_group == "unknown" and fs_row.fx_hedge_policy and not contradicts:
            # SEC profile leaves the policy empty; the factsheet prints it ("ไม่ป้องกัน (unhedged)")
            prof.hedge_group, prof.hedge_source = fs_row.fx_hedge_policy, "factsheet"
        prof.is_passive = base.get("management_style") in _PASSIVE_STYLES
        prof.is_esg = "SRI" in specs              # SEC's official sustainable-fund designation only
        mk = master_key(fs_row.master_isin, base.get("feederfund_master_fund"))
        prof.master_key_kind, prof.master_key = mk if mk else (None, None)
        prof.classifier_input = data
        # currency from the FULL policy text (classifier input is cut at 1,200 chars; currency is often later)
        full_policy = re.sub(r"\s+", " ", re.sub(r"<[^>]+>|&nbsp;", " ", base.get("investment_policy_desc") or ""))
        prof.currency, prof.currency_source = currency_from_text(full_policy, base.get("feederfund_master_fund"),
                                                                 prof.aimc_category or fs_row.aimc_category)

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
                # other Thai feeders of the same master print a category: all agree → rule, approved
                # ("inherited"); they disagree → majority, pending approval ("inferred")
                cat, c = siblings.most_common(1)[0]
                prof.aimc_category = cat
                prof.aimc_source = "inherited" if len(siblings) == 1 else "inferred"
                prof.aimc_reason = f"Same master fund as {c} fund(s) whose factsheets say {cat}" + (
                    "" if len(siblings) == 1 else f" (others say {', '.join(k for k in siblings if k != cat)})")
                counts["inherited" if len(siblings) == 1 else "inferred"] += 1
            elif base.get("policy_desc") == "ตราสารทุน" and base.get("invest_country_flag") in ("1", "4"):
                out = await infer_aimc_category(data)
                prof.aimc_category, prof.aimc_source, prof.aimc_reason = out["category"], "inferred", out["reason"]
                counts["inferred"] += 1
            else:
                prof.aimc_category, prof.aimc_source, prof.aimc_reason = None, None, None
        except LlmError as e:
            counts["error"] += 1
            errors.append(f"{proj_id}: {e}")
            await db.commit()                                  # keep deterministic fields; retry next run
            continue
        if themes_for(prof.aimc_category) is None and prof.theme_source != "manual":
            prof.peer_theme, prof.theme_source, prof.theme_status, prof.theme_alt = \
                prof.aimc_category, "fixed", "fixed", None
        prof.input_hash, prof.model, prof.classified_at = h, settings.OLLAMA_MODEL, datetime.now(timezone.utc)
        counts["built"] += 1
        if n % 50 == 0:
            (await db.get(SyncJob, job.id)).error_message = f"{n}/{len(todo)} " + \
                " ".join(f"{k}:{v}" for k, v in counts.items())
        await db.commit()

    theme_counts = await assign_themes(db, force=force, only=proj_ids)
    job = await db.get(SyncJob, job.id)
    job.completed_at = datetime.now(timezone.utc)
    job.status = "error" if errors or theme_counts["errors"] else "success"
    job.error_message = (f"{len(todo)}/{len(todo)} " + " ".join(f"{k}:{v}" for k, v in counts.items())
                         + " themes " + " ".join(f"{k}:{v}" for k, v in theme_counts.items() if k != "errors")
                         + (f" | {'; '.join((errors + theme_counts['errors'])[:3])}" if errors or theme_counts["errors"] else ""))
    await db.commit()
    return {"projects": len(todo), **counts, "themes": {k: v for k, v in theme_counts.items() if k != "errors"},
            "errors": errors + theme_counts["errors"]}


async def assign_themes(db: AsyncSession, force: bool = False, only: set[str] | None = None) -> dict:
    """
    One theme per master fund (all its Thai feeders share it), classified by two models.
    Both agree → status "agreed" (automatic); they differ → "review" (still counted, labelled
    "theme unreviewed"; counted at category level only). A manual theme on any feeder applies to
    every feeder of that master; a passive broad-index master gets its broad theme by rule.
    `force` re-classifies every master, or only masters containing a project in `only`.
    Models run one after the other (all primary calls, then all review calls) so two ~20 GB
    models aren't swapped in and out of memory per fund.
    """
    rows = (await db.execute(select(PeerProfile).where(PeerProfile.aimc_category.isnot(None)))).scalars().all()
    groups: dict[tuple[str, str], list[PeerProfile]] = {}
    for p in rows:
        if themes_for(p.aimc_category) is not None:
            groups.setdefault((p.aimc_category, _member_key(p)), []).append(p)

    counts = {"masters": len(groups), "manual": 0, "rule": 0, "unchanged": 0, "agreed": 0, "review": 0}
    errors: list[str] = []
    todo: list[tuple[tuple[str, str], list[PeerProfile], dict, str]] = []
    for gkey, members in groups.items():
        manual = next((m for m in members if m.theme_source == "manual" and m.peer_theme in themes_for(gkey[0])), None)
        if manual:
            for m in members:
                m.peer_theme, m.theme_source, m.theme_status = manual.peer_theme, "manual", "manual"
            counts["manual"] += 1
            continue
        rep = max(members, key=lambda m: (bool((m.classifier_input or {}).get("top_holdings")), m.sec_proj_id))
        data = rep.classifier_input or {}
        broad = broad_index_theme(gkey[0], any(m.is_passive for m in members), data)
        if broad:
            for m in members:
                m.peer_theme, m.theme_source, m.theme_status, m.theme_alt = broad, "rule", "rule", None
                m.theme_reason = "Rule: passive fund tracking a broad market index"
            counts["rule"] += 1
            continue
        h = hashlib.sha256(json.dumps([gkey[0], data], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        forced = force and (only is None or any(m.sec_proj_id in only for m in members))
        if not forced and all(m.theme_input_hash == h and m.theme_status in ("agreed", "review") for m in members):
            counts["unchanged"] += 1
            continue
        todo.append((gkey, members, data, h))
    await db.commit()

    primary: dict[tuple[str, str], str] = {}
    for gkey, members, data, h in todo:
        try:
            primary[gkey] = await classify_theme(gkey[0], themes_for(gkey[0]), data)
        except LlmError as e:
            errors.append(f"{gkey}: {e}")
    review: dict[tuple[str, str], str] = {}
    for gkey, members, data, h in todo:
        if gkey not in primary:
            continue
        try:
            review[gkey] = await classify_theme(gkey[0], themes_for(gkey[0]), data, model=settings.OLLAMA_REVIEW_MODEL)
        except LlmError as e:
            errors.append(f"{gkey} (review model): {e}")

    for gkey, members, data, h in todo:
        if gkey not in primary or gkey not in review:
            continue
        status = "agreed" if primary[gkey] == review[gkey] else "review"
        counts[status] += 1
        for m in members:
            m.peer_theme, m.theme_alt, m.theme_status = primary[gkey], review[gkey], status
            m.theme_source, m.theme_input_hash = "ai", h
            m.theme_reason = None if status == "agreed" else \
                f"Models disagree: {settings.OLLAMA_MODEL} → {primary[gkey]}, {settings.OLLAMA_REVIEW_MODEL} → {review[gkey]}"
    await db.commit()
    return {**counts, "errors": errors}


# ── peer groups ───────────────────────────────────────────────────────────────

def is_approved(p: PeerProfile) -> bool:
    """Inferred (LLM) categories don't count as peers until an admin approves them (→ manual)."""
    return p.aimc_source in ("parsed", "manual", "inherited")


def _member_key(p: PeerProfile) -> str:
    """One peer per master fund; direct-stock / no-master funds are their own peer."""
    return f"{p.master_key_kind}:{p.master_key}" if p.master_key else f"proj:{p.sec_proj_id}"


def select_members(me: PeerProfile, all_in_cat: list[PeerProfile]) -> dict:
    """
    Peer membership rules (pure; shared by peer_group and the monthly snapshots):
    approved categories only; theme group without borderline (models-disagree) masters when it
    has >= MIN_THEME_GROUP distinct masters, else the whole category.
    """
    pending = [p for p in all_in_cat if not is_approved(p) and p.sec_proj_id != me.sec_proj_id]
    in_cat = [p for p in all_in_cat if is_approved(p) or p.sec_proj_id == me.sec_proj_id]
    in_theme = [p for p in in_cat if p.peer_theme == me.peer_theme
                and (p.theme_status != "review" or p.sec_proj_id == me.sec_proj_id)]
    borderline = {_member_key(p) for p in in_cat if p.theme_status == "review" and p.sec_proj_id != me.sec_proj_id
                  and me.peer_theme in (p.peer_theme, p.theme_alt)}
    theme_masters = {_member_key(p) for p in in_theme}
    basis, members = ("theme", in_theme) if len(theme_masters) >= MIN_THEME_GROUP else ("category", in_cat)
    return {"basis": basis, "members": members, "theme_masters": theme_masters,
            "pending": pending, "borderline": borderline}


async def peer_group(db: AsyncSession, proj_id: str) -> dict | None:
    """
    Peers of one project: its theme group when that has >= MIN_THEME_GROUP distinct
    masters, otherwise its whole AIMC category. Members are grouped per master.
    Masters whose theme the two models disagreed on ("review") are left out of theme groups
    (they're genuinely borderline) but counted at category level.
    """
    me = await db.get(PeerProfile, proj_id)
    if me is None or not me.aimc_category:
        return None
    all_in_cat = (await db.execute(select(PeerProfile).where(PeerProfile.aimc_category == me.aimc_category))).scalars().all()
    sel = select_members(me, all_in_cat)
    basis, members, theme_masters, pending, borderline = (
        sel["basis"], sel["members"], sel["theme_masters"], sel["pending"], sel["borderline"])

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
                           "aimc_source": p.aimc_source, "theme_status": p.theme_status,
                           "is_self": p.sec_proj_id == proj_id})
    return {
        "sec_proj_id": proj_id, "aimc_category": me.aimc_category, "aimc_source": me.aimc_source,
        "aimc_reason": me.aimc_reason, "peer_theme": me.peer_theme, "theme_source": me.theme_source,
        "theme_reason": me.theme_reason, "theme_status": me.theme_status, "theme_alt": me.theme_alt, "basis": basis, "theme_masters": len(theme_masters),
        "pending_excluded": len([p for p in pending if basis == "category" or p.peer_theme == me.peer_theme]),
        # borderline masters (models disagreed, one answer = this theme) left out of the theme group
        "borderline_excluded": len(borderline) if basis == "theme" else 0,
        # category basis keeps borderline members → label "theme unreviewed"
        "theme_unreviewed": len({_member_key(p) for p in members if p.theme_status == "review"}),
        "masters": sorted(grouped.values(), key=lambda g: (not any(f["is_self"] for f in g["funds"]),
                                                             -len(g["funds"]), g["master_key"] or "")),
    }
