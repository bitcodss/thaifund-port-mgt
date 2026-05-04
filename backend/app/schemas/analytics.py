from datetime import date, datetime
from decimal import Decimal
import uuid

from pydantic import BaseModel


class HoldingRow(BaseModel):
    fund_code: str
    fund_name_en: str | None
    amc: str | None
    asset_class: str | None
    benchmark: str | None = None
    tax_scheme: str
    units: Decimal
    cost_basis: Decimal
    latest_nav: Decimal | None
    latest_nav_date: date | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pnl_pct: Decimal | None
    oldest_purchase_date: date | None
    holding_days: int | None
    entry_cost_in_fund: Decimal | None
    fund_pnl_pct: Decimal | None
    fund_entry_date: date | None
    dividends_gross: Decimal = Decimal("0")
    dividends_net: Decimal = Decimal("0")
    total_return_pct: Decimal | None = None          # (unrealized_pnl + dividends_net) / cost_basis
    total_return_fund_pct: Decimal | None = None     # (unrealized_pnl_vs_entry + dividends_net) / entry_cost_in_fund

    model_config = {"from_attributes": True}


class PortfolioSummary(BaseModel):
    portfolio_id: uuid.UUID
    as_of_date: date
    total_cost_basis: Decimal
    total_market_value: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pnl_pct: Decimal | None
    realized_pnl: Decimal
    total_invested: Decimal
    xirr: Decimal | None
    xirr_error: str | None
    twr: Decimal | None
    twr_error: str | None
    open_positions: int


class AllocationItem(BaseModel):
    label: str
    value: Decimal
    pct: Decimal


class AllocationResult(BaseModel):
    by_asset_class: list[AllocationItem]
    by_amc: list[AllocationItem]
    by_tax_scheme: list[AllocationItem]
    by_risk_level: list[AllocationItem]


class FundPerformance(BaseModel):
    fund_code: str
    latest_nav: Decimal | None
    latest_nav_date: date | None
    returns_7d: Decimal | None
    returns_30d: Decimal | None
    returns_6m: Decimal | None
    returns_1y: Decimal | None
    returns_ytd: Decimal | None
    returns_max: Decimal | None


class FundRiskMetrics(BaseModel):
    fund_code: str
    data_weeks: int
    annualized_volatility: Decimal | None
    sharpe_ratio: Decimal | None
    max_drawdown: Decimal | None
    risk_free_rate_used: Decimal


class NavPoint(BaseModel):
    date: date
    nav: float


class LotEligibility(BaseModel):
    lot_id: uuid.UUID
    source_lot_id: uuid.UUID | None
    source_fund_code: str | None
    switch_chain: list[str]  # full ancestry oldest-first, e.g. ["SCBGOLDH-SSF", "SCBRF2000(SSF)"]
    fund_code: str
    tax_scheme: str
    original_purchase_date: date
    units_remaining: Decimal
    cost_basis_remaining: Decimal
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    is_eligible: bool
    eligible_date: date | None
    days_remaining: int
    holding_years_required: Decimal
    age_requirement: int | None

    model_config = {"from_attributes": True}


class AiSummary(BaseModel):
    portfolio_id: uuid.UUID
    content: str
    generated_at: datetime
