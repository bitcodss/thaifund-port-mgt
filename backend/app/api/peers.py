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
    if body.theme is None:
        prof.theme_source, prof.input_hash = None, None      # next build re-classifies
    else:
        allowed = themes_for(prof.aimc_category)
        if allowed is None or body.theme not in allowed:
            raise HTTPException(status_code=422, detail=f"theme must be one of {allowed} for {prof.aimc_category}")
        prof.peer_theme, prof.theme_source, prof.theme_reason = body.theme, "manual", "Set manually by admin"
    await db.commit()
    return await peer_service.peer_group(db, proj_id)
