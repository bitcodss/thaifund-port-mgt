import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.portfolio import Portfolio
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.portfolio import PortfolioCreate, PortfolioOut, PortfolioUpdate
from app.api.deps import get_current_user
from app.services.portfolio_service import invalidate_portfolio


class TransferHoldingIn(BaseModel):
    fund_code: str
    tax_scheme: str
    target_portfolio_id: uuid.UUID

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


def _require_read_access(portfolio: Portfolio, user: User) -> None:
    """Owner or admin may read."""
    if portfolio.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")


def _require_write_access(portfolio: Portfolio, user: User) -> None:
    """Only the owner may mutate. Admin role does NOT grant write access on
    another user's portfolio — admins manage user accounts, not user data."""
    if portfolio.user_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("", response_model=list[PortfolioOut])
async def list_portfolios(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Portfolio).where(Portfolio.user_id == user.id))
    return result.scalars().all()


@router.post("", response_model=PortfolioOut, status_code=status.HTTP_201_CREATED)
async def create_portfolio(
    body: PortfolioCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = Portfolio(id=uuid.uuid4(), user_id=user.id, name=body.name)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


@router.get("/{portfolio_id}", response_model=PortfolioOut)
async def get_portfolio(
    portfolio_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = await db.get(Portfolio, portfolio_id)
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    _require_read_access(p, user)
    return p


@router.patch("/{portfolio_id}", response_model=PortfolioOut)
async def update_portfolio(
    portfolio_id: uuid.UUID,
    body: PortfolioUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = await db.get(Portfolio, portfolio_id)
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    _require_write_access(p, user)
    p.name = body.name
    await db.commit()
    await db.refresh(p)
    return p


@router.post("/{portfolio_id}/analytics/refresh", status_code=status.HTTP_204_NO_CONTENT)
async def refresh_analytics(
    portfolio_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Clear the analytics cache for this portfolio so the next request re-queries the DB."""
    p = await db.get(Portfolio, portfolio_id)
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    _require_write_access(p, user)
    invalidate_portfolio(portfolio_id)


@router.post("/{portfolio_id}/transfer-holding")
async def transfer_holding(
    portfolio_id: uuid.UUID,
    body: TransferHoldingIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Move all open lots + associated transactions for one fund from this portfolio
    to another portfolio owned by the same user.
    """
    source = await db.get(Portfolio, portfolio_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source portfolio not found")
    _require_write_access(source, user)

    target = await db.get(Portfolio, body.target_portfolio_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target portfolio not found")
    _require_write_access(target, user)

    if source.id == target.id:
        raise HTTPException(status_code=400, detail="Source and target portfolio must be different")

    fund_code = body.fund_code
    tax_scheme = body.tax_scheme

    # Count open lots to validate there's something to move
    lots_result = await db.execute(
        select(TaxLot).where(
            TaxLot.portfolio_id == portfolio_id,
            TaxLot.fund_code == fund_code,
            TaxLot.tax_scheme == tax_scheme,
            TaxLot.units_remaining > 0,
        )
    )
    open_lots = lots_result.scalars().all()
    if not open_lots:
        raise HTTPException(status_code=400, detail=f"No open positions for {fund_code} ({tax_scheme}) in source portfolio")

    # Move ALL lots for this (fund_code, tax_scheme) — open and closed — to keep history consistent
    await db.execute(
        update(TaxLot)
        .where(
            TaxLot.portfolio_id == portfolio_id,
            TaxLot.fund_code == fund_code,
            TaxLot.tax_scheme == tax_scheme,
        )
        .values(portfolio_id=body.target_portfolio_id)
    )

    # Move transactions scoped to this (fund_code, tax_scheme)
    await db.execute(
        update(Transaction)
        .where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.fund_code == fund_code,
            Transaction.tax_scheme == tax_scheme,
        )
        .values(portfolio_id=body.target_portfolio_id)
    )

    await db.commit()
    invalidate_portfolio(portfolio_id)
    invalidate_portfolio(body.target_portfolio_id)

    return {"moved_lots": len(open_lots), "fund_code": fund_code}


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_portfolio(
    portfolio_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = await db.get(Portfolio, portfolio_id)
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    _require_write_access(p, user)
    await db.delete(p)
    await db.commit()
