"""
Peer groups for review (Phase 2): only the groups that held funds fall into.
Admins can override a project's peer theme (kept across syncs) or hand it back to the LLM.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.database import get_db
from app.models.fund import Fund
from app.models.market import PeerProfile
from app.models.portfolio import Portfolio
from app.models.tax_lot import TaxLot
from app.models.user import User, UserRole
from app.services import peer_service
from app.services.peer_service import INFERABLE_CATEGORIES
from app.services.peer_taxonomy import THEMES_BY_CATEGORY, themes_for

router = APIRouter(prefix="/peers", tags=["peers"])


class CategoryOverride(BaseModel):
    category: str | None   # None → back to parsed / inferred on the next build


class ThemeOverride(BaseModel):
    theme: str | None   # None → hand back to the LLM classifier


@router.get("/themes")
async def peer_themes(_=Depends(get_current_user)):
    """Allowed peer themes per AIMC category (categories not listed have a single theme)."""
    return THEMES_BY_CATEGORY


@router.get("/held")
async def held_peer_groups(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Peer group of every fund held by the user (admins: all portfolios), one entry per SEC project."""
    q = (select(Fund.sec_proj_id, Fund.fund_code)
         .join(TaxLot, TaxLot.fund_code == Fund.fund_code)
         .join(Portfolio, Portfolio.id == TaxLot.portfolio_id)
         .where(TaxLot.units_remaining > 0, Fund.sec_proj_id.isnot(None)))
    if user.role != UserRole.ADMIN.value:
        q = q.where(Portfolio.user_id == user.id)
    held: dict[str, set[str]] = {}
    for proj_id, code in (await db.execute(q)).all():
        held.setdefault(proj_id, set()).add(code)
    out = []
    for proj_id in sorted(held):
        group = await peer_service.peer_group(db, proj_id)
        out.append({"held_codes": sorted(held[proj_id]), **(group or {"sec_proj_id": proj_id, "aimc_category": None})})
    return out


@router.get("/categories")
async def peer_categories(_=Depends(get_current_user)):
    """The 44 AIMC report categories an inferred / manual category may take."""
    return INFERABLE_CATEGORIES


@router.get("/pending")
async def pending_categories(db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    """LLM-inferred categories awaiting approval (excluded from peer stats until approved)."""
    rows = (await db.execute(select(PeerProfile).where(PeerProfile.aimc_source == "inferred")
                             .order_by(PeerProfile.aimc_category, PeerProfile.sec_proj_id))).scalars().all()
    funds = {}
    for f in (await db.execute(select(Fund).where(Fund.sec_proj_id.in_({r.sec_proj_id for r in rows}))
                                .order_by(Fund.fund_code))).scalars():
        funds.setdefault(f.sec_proj_id, f)
    return [{"sec_proj_id": r.sec_proj_id, "fund_code": funds[r.sec_proj_id].fund_code if r.sec_proj_id in funds else None,
             "name_en": funds[r.sec_proj_id].name_en if r.sec_proj_id in funds else None,
             "master_fund": funds[r.sec_proj_id].master_fund if r.sec_proj_id in funds else None,
             "aimc_category": r.aimc_category, "peer_theme": r.peer_theme, "aimc_reason": r.aimc_reason}
            for r in rows]


class ApproveBody(BaseModel):
    proj_ids: list[str]


@router.post("/approve")
async def approve_categories(body: ApproveBody, db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    """Approve inferred categories as they are (→ manual, counted in peer stats from now on)."""
    approved = []
    for pid in dict.fromkeys(body.proj_ids):
        prof = await db.get(PeerProfile, pid)
        if prof is not None and prof.aimc_source == "inferred":
            prof.aimc_source = "manual"
            prof.aimc_reason = f"Approved by admin (AI: {prof.aimc_reason or 'no reason given'})"[:2000]
            approved.append(pid)
    await db.commit()
    return {"approved": approved}


@router.put("/{proj_id}/category")
async def set_peer_category(proj_id: str, body: CategoryOverride, db: AsyncSession = Depends(get_db),
                            _=Depends(require_admin)):
    """Admin override for a fund's AIMC category (e.g. a wrong LLM inference) — kept across syncs."""
    prof = await db.get(PeerProfile, proj_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="No peer profile for this project")
    if body.category is None:
        prof.aimc_source, prof.input_hash = None, None        # next build re-derives it
    else:
        if body.category not in INFERABLE_CATEGORIES:
            raise HTTPException(status_code=422, detail="category must be one of the 44 AIMC report categories")
        prof.aimc_category, prof.aimc_source, prof.aimc_reason = body.category, "manual", "Set manually by admin"
        allowed = themes_for(body.category)
        if allowed is None:
            prof.peer_theme, prof.theme_source = body.category, "fixed"
        elif prof.peer_theme not in allowed:
            prof.theme_source, prof.input_hash = None, None   # re-theme within the new category next build
    await db.commit()
    return await peer_service.peer_group(db, proj_id)


@router.put("/{proj_id}/theme")
async def set_peer_theme(proj_id: str, body: ThemeOverride, db: AsyncSession = Depends(get_db),
                         _=Depends(require_admin)):
    prof = await db.get(PeerProfile, proj_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="No peer profile for this project")
    allowed = themes_for(prof.aimc_category)
    if body.theme is not None and (allowed is None or body.theme not in allowed):
        raise HTTPException(status_code=422, detail=f"theme must be one of {allowed} for {prof.aimc_category}")
    # one theme per master: the override (or its removal) applies to every feeder of this master
    members = [prof]
    if prof.master_key:
        members = (await db.execute(select(PeerProfile).where(
            PeerProfile.master_key == prof.master_key, PeerProfile.aimc_category == prof.aimc_category))).scalars().all()
    for m in members:
        if body.theme is None:
            m.theme_source, m.theme_status, m.theme_input_hash = None, None, None   # re-classified next build
        else:
            m.peer_theme, m.theme_source, m.theme_status = body.theme, "manual", "manual"
            m.theme_reason = "Set manually by admin (applies to all feeders of this master)"
    await db.commit()
    return await peer_service.peer_group(db, proj_id)
