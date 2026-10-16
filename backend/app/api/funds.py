"""
Fund registry — CRUD for fund metadata + manual sec_proj_id management.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.fund import Fund, NavHistory
from app.models.market import FundProxy, NewsDigest
from app.schemas.fund import (
    CategoryOptions, FundCreate, FundOut, FundProxyOut, FundProxyUpdate, FundUpdate,
    NavHistoryOut, NewsDigestOut,
)
from app.services import proxy_service
from app.services.fund_taxonomy import REGIONS, THEMES
from app.api.deps import get_current_user, require_admin
from app.services.portfolio_service import clear_all_cache

router = APIRouter(prefix="/funds", tags=["funds"])


@router.get("/search", response_model=list[FundOut])
async def search_funds(
    q: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    """Search funds by code or name (case-insensitive, partial match). Returns up to 20 results."""
    from sqlalchemy import or_
    pattern = f"%{q.strip()}%"
    result = await db.execute(
        select(Fund)
        .where(or_(Fund.fund_code.ilike(pattern), Fund.name_en.ilike(pattern)))
        .order_by(Fund.fund_code)
        .limit(20)
    )
    return result.scalars().all()


@router.get("", response_model=list[FundOut])
async def list_funds(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    result = await db.execute(select(Fund).order_by(Fund.fund_code))
    return result.scalars().all()


@router.post("", response_model=FundOut, status_code=status.HTTP_201_CREATED)
async def create_fund(
    body: FundCreate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    fund_code = body.fund_code.strip().upper()
    existing = await db.get(Fund, fund_code)
    if existing:
        raise HTTPException(status_code=400, detail="Fund code already exists")
    fund = Fund(**{k: v for k, v in body.model_dump().items() if v is not None})
    fund.fund_code = fund_code
    db.add(fund)
    await db.commit()
    await db.refresh(fund)
    return fund


@router.get("/category-options", response_model=CategoryOptions)
async def get_category_options(_=Depends(get_current_user)):
    """Allowed region/theme values for the category override picker."""
    return CategoryOptions(regions=REGIONS, themes=THEMES)


@router.get("/{fund_code}", response_model=FundOut)
async def get_fund(
    fund_code: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    fund = await db.get(Fund, fund_code.upper())
    if not fund:
        raise HTTPException(status_code=404, detail="Fund not found")
    return fund


@router.patch("/{fund_code}", response_model=FundOut)
async def update_fund(
    fund_code: str,
    body: FundUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    fund = await db.get(Fund, fund_code.upper())
    if not fund:
        raise HTTPException(status_code=404, detail="Fund not found")
    updates = body.model_dump(exclude_none=True)
    if updates.pop("category_source", None) == "ai":
        # Hand back to the classifier: next categorisation run re-classifies it
        fund.category_source = "ai"
        fund.category_input_hash = None
    if "region" in updates or "theme" in updates:
        updates["category_source"] = "manual"
        updates["category_reason"] = "Set manually by admin"
    for field, value in updates.items():
        setattr(fund, field, value)
    await db.commit()
    clear_all_cache()  # holdings/allocation embed fund metadata
    await db.refresh(fund)
    return fund


async def _proj_id(fund_code: str, db: AsyncSession) -> str:
    fund = await db.get(Fund, fund_code.upper())
    if not fund:
        raise HTTPException(status_code=404, detail="Fund not found")
    if not fund.sec_proj_id:
        raise HTTPException(status_code=400, detail="Fund has no SEC project id")
    return fund.sec_proj_id


@router.get("/{fund_code}/proxies", response_model=list[FundProxyOut])
async def get_fund_proxies(
    fund_code: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    proj_id = await _proj_id(fund_code, db)
    rows = await db.execute(
        select(FundProxy).where(FundProxy.sec_proj_id == proj_id).order_by(FundProxy.kind, FundProxy.ticker)
    )
    return rows.scalars().all()


@router.put("/{fund_code}/proxies/{kind}", response_model=list[FundProxyOut])
async def set_fund_proxies(
    fund_code: str,
    kind: str,
    body: FundProxyUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    """Admin override of price or news proxies (validated against Yahoo). Empty list = back to auto."""
    if kind not in proxy_service.KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {proxy_service.KINDS}")
    proj_id = await _proj_id(fund_code, db)
    return await proxy_service.set_manual(db, proj_id, kind, body.tickers, body.source_note,
                                          body.source_as_of, body.weights)


@router.get("/{fund_code}/news-digests", response_model=list[NewsDigestOut])
async def get_fund_news_digests(
    fund_code: str,
    weeks: int = 8,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    """Weekly digests for this fund's news proxies, newest first."""
    proj_id = await _proj_id(fund_code, db)
    tickers = (await db.execute(select(FundProxy.ticker).where(
        FundProxy.sec_proj_id == proj_id, FundProxy.kind == "news", FundProxy.valid.is_(True)))).scalars().all()
    if not tickers:
        return []
    rows = await db.execute(
        select(NewsDigest).where(NewsDigest.ticker.in_(tickers))
        .order_by(NewsDigest.week_start.desc(), NewsDigest.ticker)
        .limit(max(1, min(weeks, 52)) * len(tickers))
    )
    return rows.scalars().all()


@router.get("/{fund_code}/nav", response_model=list[NavHistoryOut])
async def get_fund_nav_history(
    fund_code: str,
    limit: int = 365,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    result = await db.execute(
        select(NavHistory)
        .where(NavHistory.fund_code == fund_code.upper())
        .order_by(NavHistory.trade_date.desc())
        .limit(limit)
    )
    return result.scalars().all()
