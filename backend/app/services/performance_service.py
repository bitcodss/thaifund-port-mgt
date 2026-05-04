"""
Per-fund NAV performance (timeframe returns) and risk metrics.
Weekly NAV series aligned to ISO week (Friday close) for stability.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fund import NavHistory
from app.schemas.analytics import FundPerformance, FundRiskMetrics

logger = logging.getLogger(__name__)

RISK_FREE_RATE_ANNUAL = Decimal("0.015")  # 1.5% p.a. — adjustable
MIN_WEEKS_FOR_RISK = 104
QUANT = Decimal("0.00000001")


async def get_fund_performance(fund_code: str, db: AsyncSession, since_date: date | None = None) -> FundPerformance:
    """
    Compute return for standard timeframes using cached NAV history.
    since_date: when provided, MAX return is measured from that date (first holding date).
    """
    result = await db.execute(
        select(NavHistory.trade_date, NavHistory.nav)
        .where(NavHistory.fund_code == fund_code)
        .order_by(NavHistory.trade_date.desc())
        .limit(400)
    )
    rows = result.all()

    if not rows:
        return FundPerformance(
            fund_code=fund_code,
            latest_nav=None, latest_nav_date=None,
            returns_7d=None, returns_30d=None, returns_6m=None,
            returns_1y=None, returns_ytd=None, returns_max=None,
        )

    latest_nav = rows[0].nav
    latest_date = rows[0].trade_date
    nav_map: dict[date, Decimal] = {r.trade_date: r.nav for r in rows}

    def ret(anchor_date: date) -> Decimal | None:
        nav_at = _nav_on_or_before(nav_map, anchor_date)
        if nav_at is None or nav_at == 0:
            return None
        return ((latest_nav - nav_at) / nav_at).quantize(QUANT)

    today = latest_date
    ytd_anchor = date(today.year, 1, 1) - timedelta(days=1)

    # MAX: from since_date (first holding date) if provided, else absolute earliest
    r_max: Decimal | None = None
    if since_date and since_date < today:
        # Fetch NAV on or just after since_date (first buy NAV may not be in history)
        max_result = await db.execute(
            select(NavHistory.trade_date, NavHistory.nav)
            .where(NavHistory.fund_code == fund_code, NavHistory.trade_date >= since_date)
            .order_by(NavHistory.trade_date.asc())
            .limit(1)
        )
        max_row = max_result.one_or_none()
        if max_row and max_row.nav and max_row.trade_date < today:
            r_max = ((latest_nav - max_row.nav) / max_row.nav).quantize(QUANT)
    else:
        max_result = await db.execute(
            select(NavHistory.trade_date, NavHistory.nav)
            .where(NavHistory.fund_code == fund_code)
            .order_by(NavHistory.trade_date.asc())
            .limit(1)
        )
        max_row = max_result.one_or_none()
        if max_row and max_row.nav and max_row.trade_date < today:
            r_max = ((latest_nav - max_row.nav) / max_row.nav).quantize(QUANT)

    return FundPerformance(
        fund_code=fund_code,
        latest_nav=latest_nav,
        latest_nav_date=latest_date,
        returns_7d=ret(today - timedelta(days=7)),
        returns_30d=ret(today - timedelta(days=30)),
        returns_6m=ret(today - timedelta(days=182)),
        returns_1y=ret(today - timedelta(days=365)),
        returns_ytd=ret(ytd_anchor),
        returns_max=r_max,
    )


async def get_fund_nav_history(fund_code: str, db: AsyncSession, days: int = 365) -> list[dict]:
    """Return NAV history for charting, most recent `days` trading days."""
    result = await db.execute(
        select(NavHistory.trade_date, NavHistory.nav)
        .where(NavHistory.fund_code == fund_code)
        .order_by(NavHistory.trade_date.desc())
        .limit(days)
    )
    rows = result.all()
    return [{"date": str(r.trade_date), "nav": float(r.nav)} for r in reversed(rows)]


def _nav_on_or_before(nav_map: dict[date, Decimal], anchor: date) -> Decimal | None:
    """Find the most recent NAV on or before anchor date from an in-memory map."""
    for days_back in range(0, 10):
        d = anchor - timedelta(days=days_back)
        if d in nav_map:
            return nav_map[d]
    return None


async def get_fund_risk_metrics(fund_code: str, db: AsyncSession) -> FundRiskMetrics:
    """
    Compute risk metrics from weekly NAV series.
    Requires ≥104 weeks of data; returns None fields otherwise.
    """
    # Get full NAV history for this fund
    result = await db.execute(
        select(NavHistory.trade_date, NavHistory.nav)
        .where(NavHistory.fund_code == fund_code)
        .order_by(NavHistory.trade_date.asc())
    )
    rows = result.all()

    if len(rows) < 10:
        return FundRiskMetrics(
            fund_code=fund_code, data_weeks=0,
            annualized_volatility=None, sharpe_ratio=None,
            max_drawdown=None, risk_free_rate_used=RISK_FREE_RATE_ANNUAL,
        )

    # Resample to weekly (last NAV of each ISO week)
    weekly: dict[tuple[int, int], Decimal] = {}
    for r in rows:
        iso = r.trade_date.isocalendar()
        key = (iso.year, iso.week)
        weekly[key] = r.nav  # overwrite → keeps last (latest) of the week

    navs = [float(v) for v in weekly.values()]
    data_weeks = len(navs)

    if data_weeks < MIN_WEEKS_FOR_RISK:
        return FundRiskMetrics(
            fund_code=fund_code, data_weeks=data_weeks,
            annualized_volatility=None, sharpe_ratio=None,
            max_drawdown=None, risk_free_rate_used=RISK_FREE_RATE_ANNUAL,
        )

    nav_arr = np.array(navs)
    returns = np.diff(nav_arr) / nav_arr[:-1]

    # Annualized volatility
    vol_annual = float(np.std(returns, ddof=1)) * (52 ** 0.5)

    # Sharpe: weekly risk-free rate
    rf_weekly = float((1 + float(RISK_FREE_RATE_ANNUAL)) ** (1 / 52) - 1)
    excess = returns - rf_weekly
    sharpe = float(np.mean(excess) / np.std(excess, ddof=1)) * (52 ** 0.5) if np.std(excess, ddof=1) > 0 else None

    # Max drawdown
    cum_max = np.maximum.accumulate(nav_arr)
    drawdowns = (nav_arr - cum_max) / cum_max
    max_dd = float(np.min(drawdowns))

    def d(v: float) -> Decimal:
        return Decimal(str(round(v, 8)))

    return FundRiskMetrics(
        fund_code=fund_code,
        data_weeks=data_weeks,
        annualized_volatility=d(vol_annual),
        sharpe_ratio=d(sharpe) if sharpe is not None else None,
        max_drawdown=d(max_dd),
        risk_free_rate_used=RISK_FREE_RATE_ANNUAL,
    )
