"""
Fund categorisation: region + theme for portfolio funds.

SEC v2 has no region/theme field, so a local LLM (Ollama) reads the fund's
name, policy text, master fund, benchmark and top holdings and must pick one
value from the fixed REGIONS / THEMES lists (enforced by a JSON-schema enum
and re-validated here). Verbatim SEC fields (management style, master fund,
FX hedge, risk spectrum, top holdings) are stored as-is alongside.

- Classification is per SEC project and copied to every share class.
- The LLM is only called when the fund's input data changed (input hash) or
  force=True; admin overrides (category_source="manual") are never touched.
- An LLM/SEC failure keeps the previous values and is logged on the job.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.fund import Fund
from app.models.tax_lot import SyncJob
from app.services import sec_api
from app.services.fund_taxonomy import REGIONS, THEMES
from app.services.sec_api import SecApiError
from app.services.sync_service import _policy_to_asset_class, get_portfolio_proj_ids

logger = logging.getLogger(__name__)

_PROMPT = """Classify this Thai mutual fund by where it invests (region) and its investment theme.
Choose exactly one region from: {regions}
Choose exactly one theme from: {themes}
Rules:
- Region = where the underlying assets are listed/located, NOT where a master fund is domiciled (e.g. Ireland/Luxembourg).
- "Global" = worldwide incl. emerging; "Global Developed" = developed markets only.
- Theme "Broad Market" = diversified, no sector/thematic focus (e.g. S&P 500 index, SET50).
- Use a specific theme only when the fund's mandate focuses on it.
- Theme describes WHAT the fund holds, not HOW it picks securities: quantitative / machine-learning / AI-driven stock selection is NOT the AI theme.
- A broad index benchmark (e.g. MSCI ACWI, S&P 500, China All-Shares) suggests Broad Market unless the mandate restricts sectors.
Fund data (JSON):
{data}"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "region": {"type": "string", "enum": REGIONS},
        "theme": {"type": "string", "enum": THEMES},
        "reason": {"type": "string"},
    },
    "required": ["region", "theme", "reason"],
}


class ClassificationError(Exception):
    pass


def _parse_risk_spectrum(rows: list[dict]) -> int | None:
    for r in rows:
        m = re.fullmatch(r"RS(\d)", (r.get("risk_spectrum") or "").strip())
        if m:
            return int(m.group(1))
    return None


def _clean_html(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _profile_for(fund_code: str, profiles: list[dict]) -> dict:
    """Pick the share-class row for this fund code, else the project-level row."""
    for p in profiles:
        if (p.get("fund_class_name") or "").strip() == fund_code:
            return p
    for p in profiles:
        if (p.get("fund_class_name") or "").strip().lower() in ("main", "-", ""):
            return p
    return profiles[0]


def _benchmark_text(rows: list[dict]) -> str | None:
    seen: list[str] = []
    for r in sorted(rows, key=lambda r: r.get("group_seq") or 0):
        b = (r.get("benchmark") or "").strip()
        if b and b not in seen:
            seen.append(b)
    return " / ".join(seen)[:1000] or None


def _top_holdings(rows: list[dict]) -> list[dict]:
    return [
        {"name": r.get("asset_name"), "pct": r.get("asset_ratio")}
        for r in sorted(rows, key=lambda r: r.get("asset_seq") or 0)
        if r.get("asset_name")
    ][:5]


def build_classifier_input(profile: dict, benchmark: str | None, top_holdings: list[dict]) -> dict:
    return {
        "fund": profile.get("proj_abbr_name"),
        "name_en": profile.get("proj_name_en"),
        "name_th": profile.get("proj_name_th"),
        "asset_type": profile.get("policy_desc"),
        "master_fund": profile.get("feederfund_master_fund"),
        "benchmark": benchmark,
        "top_holdings": [h["name"] for h in top_holdings],
        "policy": _clean_html(profile.get("investment_policy_desc"))[:1500],
    }


def input_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def classify_with_llm(data: dict) -> dict:
    """Ask Ollama for {region, theme, reason}; raise ClassificationError on any failure."""
    prompt = _PROMPT.format(
        regions=", ".join(REGIONS), themes=", ".join(THEMES),
        data=json.dumps(data, ensure_ascii=False),
    )
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{settings.OLLAMA_URL}/api/generate", json={
                "model": settings.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "think": False,          # thinking mode takes minutes for no gain here
                "format": _SCHEMA,
                "options": {"temperature": 0},
            })
            resp.raise_for_status()
            out = json.loads(resp.json().get("response") or "")
    except (httpx.HTTPError, ValueError) as exc:
        raise ClassificationError(f"LLM call failed: {type(exc).__name__}: {exc}") from exc

    if out.get("region") not in REGIONS or out.get("theme") not in THEMES:
        raise ClassificationError(f"LLM returned values outside the allowed lists: {out}")
    return {"region": out["region"], "theme": out["theme"], "reason": str(out.get("reason") or "")[:2000]}


async def categorize_funds(db: AsyncSession, proj_ids: set[str] | None = None, force: bool = False) -> dict:
    """
    Refresh SEC classification fields and region/theme for the given SEC projects
    (default: every project held in any portfolio). Writes a `fund_category` job row.
    """
    key = settings.SEC_API_KEY
    if not key:
        return {"classified": 0, "unchanged": 0, "errors": ["SEC_API_KEY not configured"]}
    if proj_ids is None:
        proj_ids = await get_portfolio_proj_ids(db)
    if not proj_ids:
        return {"classified": 0, "unchanged": 0, "errors": ["No portfolio funds found"]}

    job = SyncJob(id=uuid.uuid4(), type="fund_category", started_at=datetime.now(timezone.utc), status="running")
    db.add(job)
    await db.commit()

    classified = unchanged = 0
    errors: list[str] = []
    for proj_id in sorted(proj_ids):
        siblings = (await db.execute(select(Fund).where(Fund.sec_proj_id == proj_id))).scalars().all()
        if not siblings:
            continue
        try:
            profiles = await sec_api.get_fund_profiles(key, proj_id)
            if not profiles:
                errors.append(f"{proj_id}: no SEC profile")
                continue
            benchmark = _benchmark_text(await sec_api.get_benchmarks(key, proj_id))
            holdings = _top_holdings(await sec_api.get_top5_holdings(key, proj_id))
            risk = _parse_risk_spectrum(await sec_api.get_risk_spectrum(key, proj_id))
        except SecApiError as e:
            errors.append(f"{proj_id}: {e}")
            continue

        for f in siblings:
            p = _profile_for(f.fund_code, profiles)
            f.management_style = p.get("management_style")
            f.master_fund = p.get("feederfund_master_fund")
            f.fx_hedge = p.get("exchange_rate_protection_policy")
            f.sec_risk_spectrum = risk
            f.top_holdings = holdings
            if benchmark:
                f.benchmark = benchmark
            if f.asset_class is None:
                f.asset_class = _policy_to_asset_class(p.get("policy_desc"))

        data = build_classifier_input(_profile_for(siblings[0].fund_code, profiles), benchmark, holdings)
        h = input_hash(data)
        targets = [
            f for f in siblings
            if f.category_source != "manual"
            and (force or f.region is None or f.category_input_hash != h)
        ]
        if targets:
            try:
                result = await classify_with_llm(data)
            except ClassificationError as e:
                errors.append(f"{siblings[0].fund_code}: {e}")
                await db.commit()  # keep the verbatim SEC fields we did get
                continue
            now = datetime.now(timezone.utc)
            for f in targets:
                f.region = result["region"]
                f.theme = result["theme"]
                f.category_reason = result["reason"]
                f.category_source = "ai"
                f.category_input_hash = h
                f.categorized_at = now
            classified += 1
        else:
            unchanged += 1
        await db.commit()

    job = await db.get(SyncJob, job.id)
    job.completed_at = datetime.now(timezone.utc)
    job.status = "error" if errors else "success"
    job.error_message = "; ".join(errors[:5]) if errors else f"classified:{classified} unchanged:{unchanged}"
    await db.commit()
    logger.info("Fund categorisation: classified=%d unchanged=%d errors=%d", classified, unchanged, len(errors))
    return {"classified": classified, "unchanged": unchanged, "errors": errors}
