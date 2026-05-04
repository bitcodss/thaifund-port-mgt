"""
Fund registry — CRUD for fund metadata + manual sec_proj_id management.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.fund import Fund, NavHistory
from app.schemas.fund import FundCreate, FundUpdate, FundOut, NavHistoryOut
from app.api.deps import get_current_user, require_admin

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
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(fund, field, value)
    await db.commit()
    await db.refresh(fund)
    return fund


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
