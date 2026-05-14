"""
Portfolio + fund analytics endpoints.
"""
from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.portfolio import Portfolio, PortfolioAiSummary
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.analytics import (
    AiSummary, AllocationResult, FundPerformance, FundRiskMetrics,
    HoldingRow, LotEligibility, NavPoint, PortfolioSummary,
)
from app.services import portfolio_service as ps
from app.services import performance_service as perf
from app.services import ai_service
from app.services.clock import today_ict

router = APIRouter(tags=["analytics"])


async def _check_portfolio_access(portfolio_id: UUID, user: User, db: AsyncSession) -> Portfolio:
    """Read access: owner or admin (admin = support read-only)."""
    from sqlalchemy import select
    p = await db.get(Portfolio, portfolio_id)
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if p.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    return p


async def _require_portfolio_write(portfolio_id: UUID, user: User, db: AsyncSession) -> Portfolio:
    """Write access: owner only. Admin role does NOT grant write."""
    p = await db.get(Portfolio, portfolio_id)
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if p.user_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return p


@router.get("/portfolios/{portfolio_id}/analytics/summary", response_model=PortfolioSummary)
async def portfolio_summary(
    portfolio_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _check_portfolio_access(portfolio_id, user, db)
    return await ps.get_summary(portfolio_id, db)


@router.get("/portfolios/{portfolio_id}/analytics/holdings", response_model=list[HoldingRow])
async def portfolio_holdings(
    portfolio_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _check_portfolio_access(portfolio_id, user, db)
    return await ps.get_holdings(portfolio_id, db)


@router.get("/portfolios/{portfolio_id}/analytics/allocation", response_model=AllocationResult)
async def portfolio_allocation(
    portfolio_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _check_portfolio_access(portfolio_id, user, db)
    return await ps.get_allocation(portfolio_id, db)


@router.get("/portfolios/{portfolio_id}/analytics/tax-eligibility", response_model=list[LotEligibility])
async def tax_eligibility(
    portfolio_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _check_portfolio_access(portfolio_id, user, db)
    return await ps.get_tax_eligibility(portfolio_id, db, today_ict(), user.date_of_birth)


@router.get("/portfolios/{portfolio_id}/analytics/ai-summary", response_model=AiSummary)
async def get_ai_summary(
    portfolio_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _check_portfolio_access(portfolio_id, user, db)
    existing = await db.get(PortfolioAiSummary, portfolio_id)
    if not existing:
        raise HTTPException(status_code=404, detail="No AI summary yet — click Refresh to generate")
    return AiSummary(
        portfolio_id=portfolio_id,
        content=existing.content,
        generated_at=existing.generated_at,
    )


@router.post("/portfolios/{portfolio_id}/analytics/ai-summary/refresh", response_model=AiSummary)
async def refresh_ai_summary(
    portfolio_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _require_portfolio_write(portfolio_id, user, db)
    data = await _build_ai_data(portfolio_id, db)
    content = await ai_service.generate_summary(portfolio_id, data, db)
    await db.commit()
    from datetime import datetime, timezone
    return AiSummary(portfolio_id=portfolio_id, content=content, generated_at=datetime.now(timezone.utc))


async def _build_ai_data(portfolio_id: UUID, db: AsyncSession) -> dict:
    """Collect summary + holdings + risk metrics into a dict for the AI prompt."""
    summary = await ps.get_summary(portfolio_id, db)
    holdings = await ps.get_holdings(portfolio_id, db)

    holdings_data = []
    for h in holdings:
        perf_data = await perf.get_fund_performance(h.fund_code, db, since_date=h.fund_entry_date)
        risk_data = await perf.get_fund_risk_metrics(h.fund_code, db)
        holdings_data.append({
            "fund": h.fund_code,
            "scheme": h.tax_scheme,
            "entry_cost": float(h.entry_cost_in_fund) if h.entry_cost_in_fund else float(h.cost_basis),
            "market_value": float(h.market_value) if h.market_value else None,
            "fund_return_pct": float(h.fund_pnl_pct) if h.fund_pnl_pct else None,
            "days_held_in_fund": (
                (today_ict() - h.fund_entry_date).days
                if h.fund_entry_date else h.holding_days
            ),
            "return_7d_pct": float(perf_data.returns_7d) * 100 if perf_data.returns_7d else None,
            "return_30d_pct": float(perf_data.returns_30d) * 100 if perf_data.returns_30d else None,
            "return_1y_pct": float(perf_data.returns_1y) * 100 if perf_data.returns_1y else None,
            "return_since_entry_pct": float(perf_data.returns_max) * 100 if perf_data.returns_max else None,
            "sharpe": float(risk_data.sharpe_ratio) if risk_data.sharpe_ratio else None,
            "max_drawdown_pct": float(risk_data.max_drawdown) * 100 if risk_data.max_drawdown else None,
            "volatility_pct": float(risk_data.annualized_volatility) * 100 if risk_data.annualized_volatility else None,
        })

    return {
        "total_market_value": float(summary.total_market_value) if summary.total_market_value else None,
        "total_cost_basis": float(summary.total_cost_basis),
        "unrealized_pnl": float(summary.unrealized_pnl) if summary.unrealized_pnl else None,
        "unrealized_pnl_pct": float(summary.unrealized_pnl_pct) if summary.unrealized_pnl_pct else None,
        "xirr_pct": float(summary.xirr) * 100 if summary.xirr else None,
        "holdings": holdings_data,
    }


@router.get("/funds/{fund_code}/performance", response_model=FundPerformance)
async def fund_performance(
    fund_code: str,
    since_date: date | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await perf.get_fund_performance(fund_code.upper(), db, since_date=since_date)


@router.get("/funds/{fund_code}/risk-metrics", response_model=FundRiskMetrics)
async def fund_risk_metrics(
    fund_code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await perf.get_fund_risk_metrics(fund_code.upper(), db)


@router.get("/funds/{fund_code}/nav-history", response_model=list[NavPoint])
async def fund_nav_history(
    fund_code: str,
    days: int = 365,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    rows = await perf.get_fund_nav_history(fund_code.upper(), db, days=min(days, 1500))
    return rows


@router.get("/analytics/dividends")
async def user_dividend_summary(
    year: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Dividend income grouped by fund across all user's portfolios.
    Optional ?year=YYYY filter. Returns [{fund_code, gross, tax_withheld, net}].
    """
    port_result = await db.execute(
        select(Portfolio.id).where(Portfolio.user_id == user.id)
    )
    portfolio_ids = [r[0] for r in port_result.all()]
    if not portfolio_ids:
        return []

    q = (
        select(
            Transaction.fund_code,
            func.sum(Transaction.amount).label("gross"),
            func.sum(Transaction.tax_withheld).label("tax_withheld"),
        )
        .where(
            Transaction.portfolio_id.in_(portfolio_ids),
            Transaction.type == "DIVIDEND",
            Transaction.fund_code.isnot(None),
        )
    )
    if year is not None:
        q = q.where(extract("year", Transaction.date) == year)

    q = q.group_by(Transaction.fund_code).order_by(func.sum(Transaction.amount).desc())
    result = await db.execute(q)

    return [
        {
            "fund_code": r.fund_code,
            "gross": str(round(Decimal(str(r.gross)), 2)),
            "tax_withheld": str(round(Decimal(str(r.tax_withheld)), 2)),
            "net": str(round(Decimal(str(r.gross)) - Decimal(str(r.tax_withheld)), 2)),
        }
        for r in result.all()
        if r.fund_code
    ]


@router.get("/analytics/dividend-years")
async def user_dividend_years(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return distinct years that have DIVIDEND transactions for this user."""
    port_result = await db.execute(
        select(Portfolio.id).where(Portfolio.user_id == user.id)
    )
    portfolio_ids = [r[0] for r in port_result.all()]
    if not portfolio_ids:
        return []

    result = await db.execute(
        select(extract("year", Transaction.date).label("yr"))
        .where(
            Transaction.portfolio_id.in_(portfolio_ids),
            Transaction.type == "DIVIDEND",
        )
        .distinct()
        .order_by(extract("year", Transaction.date).desc())
    )
    return [int(r.yr) for r in result.all()]
