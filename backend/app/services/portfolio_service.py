"""
Portfolio analytics: holdings, summary, XIRR, allocation, tax eligibility.
All monetary values stay Decimal throughout; only converted to float inside xirr().
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fund import Fund, NavHistory
from app.models.tax_lot import TaxLot, LotConsumption, TaxSchemeRule
from app.models.transaction import Transaction
from app.schemas.analytics import (
    AllocationItem, AllocationResult, HoldingRow, LotEligibility, PortfolioSummary,
)
from app.services.clock import today_ict

logger = logging.getLogger(__name__)

QUANT = Decimal("0.00000001")
QUANT2 = Decimal("0.01")
_CACHE_TTL = 300  # 5 minutes

_cache: dict[str, tuple[Any, float]] = {}


def _cache_get(key: str) -> Any:
    entry = _cache.get(key)
    if entry and time.monotonic() - entry[1] < _CACHE_TTL:
        return entry[0]
    return None


def _cache_set(key: str, val: Any) -> None:
    _cache[key] = (val, time.monotonic())


def invalidate_portfolio(portfolio_id: UUID) -> None:
    prefix = str(portfolio_id)
    for k in list(_cache):
        if k.startswith(prefix):
            del _cache[k]


def clear_all_cache() -> None:
    _cache.clear()


# ── helpers ───────────────────────────────────────────────────────────────────

def _age_on_date(birth: date, on: date) -> int:
    """Compute exact age in whole years, accounting for leap years."""
    years = on.year - birth.year
    if (on.month, on.day) < (birth.month, birth.day):
        years -= 1
    return years


def _pct(part: Decimal, total: Decimal) -> Decimal:
    if not total:
        return Decimal("0")
    return (part / total * 100).quantize(QUANT)


def _safe_pnl_pct(pnl: Decimal, cost: Decimal) -> Decimal | None:
    if not cost:
        return None
    return (pnl / cost * 100).quantize(QUANT)


async def _latest_nav(db: AsyncSession, fund_code: str) -> tuple[Decimal | None, date | None]:
    result = await db.execute(
        select(NavHistory.nav, NavHistory.trade_date)
        .where(NavHistory.fund_code == fund_code)
        .order_by(NavHistory.trade_date.desc())
        .limit(1)
    )
    row = result.one_or_none()
    if row:
        return row.nav, row.trade_date
    return None, None


async def _latest_navs_bulk(
    db: AsyncSession, fund_codes: list[str]
) -> dict[str, tuple[Decimal, date]]:
    """Fetch latest NAV for multiple funds in one query."""
    if not fund_codes:
        return {}
    # Subquery: max trade_date per fund_code
    sub = (
        select(NavHistory.fund_code, func.max(NavHistory.trade_date).label("max_date"))
        .where(NavHistory.fund_code.in_(fund_codes))
        .group_by(NavHistory.fund_code)
        .subquery()
    )
    result = await db.execute(
        select(NavHistory.fund_code, NavHistory.nav, NavHistory.trade_date)
        .join(sub, (NavHistory.fund_code == sub.c.fund_code) & (NavHistory.trade_date == sub.c.max_date))
    )
    return {row.fund_code: (row.nav, row.trade_date) for row in result.all()}


# ── holdings ──────────────────────────────────────────────────────────────────

async def get_holdings(portfolio_id: UUID, db: AsyncSession) -> list[HoldingRow]:
    """Open lots grouped by (fund_code, tax_scheme) with current NAV."""
    today = today_ict()
    # `today` in the cache key so the holding_days fields auto-evict at midnight ICT.
    key = f"{portfolio_id}:holdings:{today.isoformat()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    result = await db.execute(
        select(
            TaxLot.fund_code,
            TaxLot.tax_scheme,
            func.sum(TaxLot.units_remaining).label("total_units"),
            func.sum(TaxLot.cost_basis_remaining).label("total_cost"),
            func.min(TaxLot.original_purchase_date).label("oldest_date"),
        )
        .where(TaxLot.portfolio_id == portfolio_id, TaxLot.units_remaining > 0)
        .group_by(TaxLot.fund_code, TaxLot.tax_scheme)
        .order_by(TaxLot.fund_code, TaxLot.tax_scheme)
    )
    rows = result.all()
    if not rows:
        return []

    fund_codes = list({r.fund_code for r in rows})
    nav_map = await _latest_navs_bulk(db, fund_codes)

    # Fund metadata
    fund_result = await db.execute(select(Fund).where(Fund.fund_code.in_(fund_codes)))
    fund_map: dict[str, Fund] = {f.fund_code: f for f in fund_result.scalars().all()}

    # First entry date into this (fund_code, tax_scheme) — used for display.
    # We DON'T use lifetime entry totals for cost calculations any more (they
    # were wrong after partial sells): cost basis comes from open lots directly.
    entry_result = await db.execute(
        select(
            Transaction.fund_code,
            Transaction.tax_scheme,
            func.min(Transaction.date).label("entry_date"),
        )
        .where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.type.in_(["BUY", "SWITCH_IN"]),
        )
        .group_by(Transaction.fund_code, Transaction.tax_scheme)
    )
    entry_date_map: dict[tuple[str, str], date | None] = {
        (r.fund_code, r.tax_scheme): r.entry_date
        for r in entry_result.all()
        if r.fund_code
    }

    # Dividends received per (fund_code, tax_scheme)
    div_result = await db.execute(
        select(
            Transaction.fund_code,
            Transaction.tax_scheme,
            func.sum(Transaction.amount).label("gross"),
            func.sum(Transaction.tax_withheld).label("withheld"),
        )
        .where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.type == "DIVIDEND",
            Transaction.fund_code.isnot(None),
        )
        .group_by(Transaction.fund_code, Transaction.tax_scheme)
    )
    div_map: dict[tuple[str, str], tuple[Decimal, Decimal]] = {
        (r.fund_code, r.tax_scheme): (Decimal(str(r.gross)), Decimal(str(r.withheld)))
        for r in div_result.all()
        if r.fund_code
    }

    holdings: list[HoldingRow] = []
    for row in rows:
        units = Decimal(str(row.total_units))
        cost = Decimal(str(row.total_cost))
        nav_info = nav_map.get(row.fund_code)
        fund = fund_map.get(row.fund_code)

        latest_nav = nav_info[0] if nav_info else None
        latest_nav_date = nav_info[1] if nav_info else None
        market_value = (units * latest_nav).quantize(QUANT) if latest_nav else None
        upnl = (market_value - cost).quantize(QUANT) if market_value is not None else None
        upnl_pct = _safe_pnl_pct(upnl, cost) if upnl is not None else None
        oldest_date = row.oldest_date
        holding_days = (today - oldest_date).days if oldest_date else None

        # Fund-entry P&L: derived from open lots only — equivalent to cost-basis P&L
        # once partial sells are accounted for correctly. The "fund entry NAV"
        # encoded in the lot's cost_basis_remaining / units_remaining IS the
        # weighted average entry NAV in the current fund (preserved across switches).
        entry_cost_in_fund: Decimal | None = cost if units > 0 else None
        fund_pnl_pct: Decimal | None = upnl_pct
        fund_entry_date: date | None = entry_date_map.get((row.fund_code, row.tax_scheme))

        # Dividends received for this (fund_code, tax_scheme)
        div_info = div_map.get((row.fund_code, row.tax_scheme))
        dividends_gross = div_info[0] if div_info else Decimal("0")
        dividends_net = (div_info[0] - div_info[1]) if div_info else Decimal("0")
        total_return_pct = _safe_pnl_pct(upnl + dividends_net, cost) if upnl is not None else None
        total_return_fund_pct = (
            _safe_pnl_pct(upnl + dividends_net, entry_cost_in_fund)
            if entry_cost_in_fund is not None and upnl is not None
            else None
        )

        holdings.append(HoldingRow(
            fund_code=row.fund_code,
            fund_name_en=fund.name_en if fund else None,
            amc=fund.amc if fund else None,
            asset_class=fund.asset_class if fund else None,
            benchmark=fund.benchmark if fund else None,
            tax_scheme=row.tax_scheme,
            units=units,
            cost_basis=cost,
            latest_nav=latest_nav,
            latest_nav_date=latest_nav_date,
            market_value=market_value,
            unrealized_pnl=upnl,
            unrealized_pnl_pct=upnl_pct,
            oldest_purchase_date=oldest_date,
            holding_days=holding_days,
            entry_cost_in_fund=entry_cost_in_fund,
            fund_pnl_pct=fund_pnl_pct,
            fund_entry_date=fund_entry_date,
            dividends_gross=dividends_gross,
            dividends_net=dividends_net,
            total_return_pct=total_return_pct,
            total_return_fund_pct=total_return_fund_pct,
        ))
    _cache_set(key, holdings)
    return holdings


# ── realized P&L ──────────────────────────────────────────────────────────────

async def _realized_pnl(portfolio_id: UUID, db: AsyncSession) -> Decimal:
    """Sum realized gains from SELL transactions only."""
    result = await db.execute(
        select(Transaction.id, Transaction.amount, Transaction.fee, Transaction.tax_withheld)
        .where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.type == "SELL",
        )
    )
    txs = result.all()
    if not txs:
        return Decimal("0")

    total = Decimal("0")
    for tx in txs:
        # Net proceeds = sale amount minus broker fee minus any withholding tax.
        # Without subtracting WHT we'd overstate realized P&L for users subject
        # to early-redemption WHT on RMF/SSF or non-resident WHT.
        proceeds = Decimal(str(tx.amount)) - Decimal(str(tx.fee)) - Decimal(str(tx.tax_withheld))
        lc_result = await db.execute(
            select(func.sum(LotConsumption.cost_basis_consumed))
            .where(LotConsumption.transaction_id == tx.id)
        )
        raw_cost = lc_result.scalar()
        if raw_cost is None:
            logger.warning("No lot consumptions found for SELL tx %s — realized P&L may be incorrect", tx.id)
            cost = Decimal("0")
        else:
            cost = Decimal(str(raw_cost))
        total += proceeds - cost
    return total.quantize(QUANT)


# ── summary ───────────────────────────────────────────────────────────────────

async def get_summary(portfolio_id: UUID, db: AsyncSession) -> PortfolioSummary:
    today = today_ict()
    key = f"{portfolio_id}:summary:{today.isoformat()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    holdings = await get_holdings(portfolio_id, db)
    realized = await _realized_pnl(portfolio_id, db)

    total_cost = sum((h.cost_basis for h in holdings), Decimal("0"))
    total_value: Decimal | None = None
    if all(h.market_value is not None for h in holdings) and holdings:
        total_value = sum((h.market_value for h in holdings), Decimal("0"))  # type: ignore[misc]

    upnl = (total_value - total_cost).quantize(QUANT) if total_value is not None else None
    upnl_pct = _safe_pnl_pct(upnl, total_cost) if upnl is not None else None

    # Total invested = sum of all BUY (amount + fee)
    inv_result = await db.execute(
        select(func.sum(Transaction.amount + Transaction.fee))
        .where(Transaction.portfolio_id == portfolio_id, Transaction.type == "BUY")
    )
    total_invested = Decimal(str(inv_result.scalar() or 0)).quantize(QUANT)

    xirr_val, xirr_err = await compute_xirr(portfolio_id, db, total_value)
    twr_val, twr_err = await compute_twr(portfolio_id, db)

    summary = PortfolioSummary(
        portfolio_id=portfolio_id,
        as_of_date=today,
        total_cost_basis=total_cost.quantize(QUANT),
        total_market_value=total_value,
        unrealized_pnl=upnl,
        unrealized_pnl_pct=upnl_pct,
        realized_pnl=realized,
        total_invested=total_invested,
        xirr=xirr_val,
        xirr_error=xirr_err,
        twr=twr_val,
        twr_error=twr_err,
        open_positions=len(holdings),
    )
    _cache_set(key, summary)
    return summary


# ── TWR ───────────────────────────────────────────────────────────────────────

async def compute_twr(
    portfolio_id: UUID,
    db: AsyncSession,
) -> tuple[Decimal | None, str | None]:
    """
    Time-Weighted Return — annualized when total_days >= 365, else period return.

    Standard methodology: replay every BUY/SELL/SWITCH transaction in order while
    maintaining per-fund unit positions. Between successive transaction dates,
    compute the holding-period return as V_end / V_start using NAVs on those
    boundary dates, where:
      V_start = portfolio market value AFTER applying flows on the start boundary
      V_end   = portfolio market value BEFORE applying flows on the end boundary
    Chain-link the HPRs to eliminate the timing effect of cash flows.

    This differs critically from the previous (broken) implementation, which used
    current open-lot units across all historical sub-periods and valued switched
    lots against the wrong fund's NAV history.

    Returns (twr, error_code). error_code is None on success.
    """
    tx_result = await db.execute(
        select(
            Transaction.date,
            Transaction.type,
            Transaction.fund_code,
            Transaction.units,
            Transaction.amount,
            Transaction.fee,
            Transaction.tax_scheme,
            Transaction.pair_id,
            Transaction.target_fund_code,
        )
        .where(Transaction.portfolio_id == portfolio_id)
        .order_by(Transaction.date, Transaction.type)
    )
    txs = tx_result.all()
    if not txs:
        return None, "no_cashflows"

    boundary_dates = sorted({t.date for t in txs})
    today = today_ict()
    if boundary_dates[-1] < today:
        boundary_dates.append(today)
    if len(boundary_dates) < 2:
        return None, "no_history"

    fund_codes = list({t.fund_code for t in txs if t.fund_code})
    if not fund_codes:
        return None, "no_positions"

    nav_result = await db.execute(
        select(NavHistory.fund_code, NavHistory.trade_date, NavHistory.nav)
        .where(NavHistory.fund_code.in_(fund_codes), NavHistory.trade_date >= boundary_dates[0])
        .order_by(NavHistory.fund_code, NavHistory.trade_date)
    )
    nav_lookup: dict[str, list[tuple[date, Decimal]]] = {}
    for row in nav_result.all():
        nav_lookup.setdefault(row.fund_code, []).append((row.trade_date, row.nav))

    def nav_on_or_before(fund_code: str, d: date) -> Decimal | None:
        entries = nav_lookup.get(fund_code)
        if not entries:
            return None
        lo, hi = 0, len(entries) - 1
        idx = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if entries[mid][0] <= d:
                idx = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return entries[idx][1] if idx >= 0 else None

    # Position state: (fund_code, tax_scheme) -> units. Cost basis is not needed
    # for TWR (which uses market value only) but we track it to share the
    # proportional reduction logic with apply_sell.
    positions: dict[tuple[str, str], Decimal] = {}

    def portfolio_value_on(d: date) -> Decimal:
        v = Decimal("0")
        for (fund_code, _scheme), units in positions.items():
            if units <= 0:
                continue
            nav = nav_on_or_before(fund_code, d)
            if nav is None:
                continue
            v += units * nav
        return v

    def apply_flows_on(d: date) -> None:
        """Apply every BUY/SELL/SWITCH transaction dated d. Switches are matched
        by pair_id; both legs in one logical step. DIVIDEND/INTEREST do not
        change units."""
        seen_pairs: set[str] = set()
        for t in txs:
            if t.date != d:
                continue
            if t.type == "BUY" and t.fund_code:
                key = (t.fund_code, t.tax_scheme)
                positions[key] = positions.get(key, Decimal("0")) + Decimal(str(t.units or 0))
            elif t.type == "SELL" and t.fund_code:
                key = (t.fund_code, t.tax_scheme)
                positions[key] = positions.get(key, Decimal("0")) - Decimal(str(t.units or 0))
            elif t.type in ("SWITCH_OUT", "SWITCH_IN") and t.pair_id:
                if t.pair_id in seen_pairs:
                    continue
                seen_pairs.add(t.pair_id)
                pair = [p for p in txs if p.pair_id == t.pair_id and p.date == d]
                out_tx = next((p for p in pair if p.type == "SWITCH_OUT"), None)
                in_tx = next((p for p in pair if p.type == "SWITCH_IN"), None)
                if out_tx and out_tx.fund_code:
                    key = (out_tx.fund_code, out_tx.tax_scheme)
                    positions[key] = positions.get(key, Decimal("0")) - Decimal(str(out_tx.units or 0))
                if in_tx and in_tx.fund_code:
                    key = (in_tx.fund_code, in_tx.tax_scheme)
                    positions[key] = positions.get(key, Decimal("0")) + Decimal(str(in_tx.units or 0))

    # Apply flows on the first boundary, then snapshot V.
    apply_flows_on(boundary_dates[0])
    v_after = portfolio_value_on(boundary_dates[0])

    twr_factor = Decimal("1")
    valid_periods = 0

    for i in range(1, len(boundary_dates)):
        d = boundary_dates[i]
        v_before = portfolio_value_on(d)
        if v_after > 0 and v_before > 0:
            twr_factor *= v_before / v_after
            valid_periods += 1
        apply_flows_on(d)
        v_after = portfolio_value_on(d)

    if valid_periods == 0 or twr_factor <= 0:
        return None, "no_nav"

    total_days = (boundary_dates[-1] - boundary_dates[0]).days
    if total_days <= 0:
        return (twr_factor - Decimal("1")).quantize(QUANT), None

    # Annualize only when the period is at least one year; otherwise return the
    # cumulative period return (annualizing a 30-day return is misleading).
    if total_days < 365:
        return (twr_factor - Decimal("1")).quantize(QUANT), None

    try:
        annualized = Decimal(str((float(twr_factor) ** (365.25 / total_days)) - 1))
        return annualized.quantize(QUANT), None
    except Exception:
        return None, "convergence"


# ── XIRR ──────────────────────────────────────────────────────────────────────

def _xirr_solve(cash_flows: list[tuple[date, Decimal]]) -> Decimal:
    """Solve XIRR for a list of (date, amount) pairs."""
    from scipy.optimize import brentq

    t0 = cash_flows[0][0]
    floats = [(d, float(a)) for d, a in cash_flows]

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** ((d - t0).days / 365.25) for d, a in floats)

    result = brentq(npv, -0.999, 100.0, xtol=1e-8, maxiter=1000)
    return Decimal(str(round(result, 8)))


async def compute_xirr(
    portfolio_id: UUID,
    db: AsyncSession,
    current_value: Decimal | None,
) -> tuple[Decimal | None, str | None]:
    """
    Returns (xirr_rate, error_code) where error_code is None on success,
    or "no_nav" | "no_cashflows" | "convergence" on failure.
    """
    if current_value is None:
        return None, "no_nav"

    result = await db.execute(
        select(Transaction.date, Transaction.type, Transaction.amount, Transaction.fee, Transaction.tax_withheld)
        .where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.type.in_(["BUY", "SELL", "DIVIDEND", "INTEREST", "SWITCH_OUT"]),
        )
        .order_by(Transaction.date)
    )
    txs = result.all()
    if not txs:
        return None, "no_cashflows"

    cash_flows: list[tuple[date, Decimal]] = []
    for tx in txs:
        amount = Decimal(str(tx.amount))
        fee = Decimal(str(tx.fee))
        withheld = Decimal(str(tx.tax_withheld))
        if tx.type == "BUY":
            cash_flows.append((tx.date, -(amount + fee)))
        elif tx.type == "SELL":
            # Subtract WHT to keep parity with the DIVIDEND/INTEREST branch below.
            cash_flows.append((tx.date, amount - fee - withheld))
        elif tx.type in ("DIVIDEND", "INTEREST"):
            # Use net received amount (gross minus withholding tax)
            cash_flows.append((tx.date, amount - withheld))
        elif tx.type == "SWITCH_OUT" and fee > 0:
            # A switch is zero-sum at portfolio level, but fees are real cash costs
            cash_flows.append((tx.date, -fee))

    if not cash_flows:
        return None, "no_cashflows"

    # Terminal: current portfolio value as of today (ICT, not server TZ).
    today = today_ict()
    if current_value > 0:
        cash_flows.append((today, current_value))

    if len(cash_flows) < 2:
        return None, "no_cashflows"

    try:
        return _xirr_solve(cash_flows), None
    except Exception:
        return None, "convergence"


# ── allocation ────────────────────────────────────────────────────────────────

async def get_allocation(portfolio_id: UUID, db: AsyncSession) -> AllocationResult:
    key = f"{portfolio_id}:allocation:{today_ict().isoformat()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    holdings = await get_holdings(portfolio_id, db)
    if not holdings:
        empty: list[AllocationItem] = []
        return AllocationResult(by_asset_class=empty, by_amc=empty, by_tax_scheme=empty, by_risk_level=empty)

    # Only use holdings with a known market_value; fall back to cost_basis
    def value_of(h: HoldingRow) -> Decimal:
        return h.market_value if h.market_value is not None else h.cost_basis

    total = sum(value_of(h) for h in holdings)
    if not total:
        empty = []
        return AllocationResult(by_asset_class=empty, by_amc=empty, by_tax_scheme=empty, by_risk_level=empty)

    # Helper: group by key function → list[AllocationItem] sorted by value desc
    def group(key_fn) -> list[AllocationItem]:
        buckets: dict[str, Decimal] = {}
        for h in holdings:
            k = key_fn(h) or "Unknown"
            buckets[k] = buckets.get(k, Decimal("0")) + value_of(h)
        return sorted(
            [AllocationItem(label=k, value=v.quantize(QUANT2), pct=_pct(v, total).quantize(QUANT2)) for k, v in buckets.items()],
            key=lambda x: x.value, reverse=True,
        )

    # Need risk_level from fund metadata
    fund_codes = [h.fund_code for h in holdings]
    fund_result = await db.execute(select(Fund).where(Fund.fund_code.in_(fund_codes)))
    fund_map = {f.fund_code: f for f in fund_result.scalars().all()}

    def risk_label(h: HoldingRow) -> str | None:
        f = fund_map.get(h.fund_code)
        return str(f.risk_level) if f and f.risk_level else None

    result = AllocationResult(
        by_asset_class=group(lambda h: h.asset_class),
        by_amc=group(lambda h: h.amc),
        by_tax_scheme=group(lambda h: h.tax_scheme),
        by_risk_level=group(risk_label),
    )
    _cache_set(key, result)
    return result


# ── tax eligibility ────────────────────────────────────────────────────────────

async def get_tax_eligibility(
    portfolio_id: UUID,
    db: AsyncSession,
    today: date,
    user_dob: date | None,
) -> list[LotEligibility]:
    key = f"{portfolio_id}:tax:{today}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    result = await db.execute(
        select(TaxLot)
        .where(TaxLot.portfolio_id == portfolio_id, TaxLot.units_remaining > 0)
        .order_by(TaxLot.fund_code, TaxLot.original_purchase_date)
    )
    lots: Sequence[TaxLot] = result.scalars().all()
    if not lots:
        return []

    fund_codes = list({lot.fund_code for lot in lots})
    nav_map = await _latest_navs_bulk(db, fund_codes)

    # Build full switch-chain ancestry for each lot.
    # We iterate outward from open-lot source IDs, fetching consumed ancestor lots
    # too, until there are no more IDs to resolve.
    ancestor_map: dict[UUID, tuple[str, UUID | None]] = {}  # lot_id → (fund_code, source_lot_id)
    to_fetch: set[UUID] = {lot.source_lot_id for lot in lots if lot.source_lot_id}
    while to_fetch:
        rows = await db.execute(
            select(TaxLot.id, TaxLot.fund_code, TaxLot.source_lot_id).where(TaxLot.id.in_(to_fetch))
        )
        fetched = rows.all()
        if not fetched:
            break
        next_frontier: set[UUID] = set()
        for row_id, fund_code, src_id in fetched:
            if row_id not in ancestor_map:
                ancestor_map[row_id] = (fund_code, src_id)
                if src_id and src_id not in ancestor_map:
                    next_frontier.add(src_id)
        to_fetch = next_frontier

    def build_switch_chain(source_lot_id: UUID | None) -> list[str]:
        """Walk ancestor_map from source_lot_id upward, return fund codes oldest-first."""
        chain: list[str] = []
        current_id = source_lot_id
        visited: set[UUID] = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            info = ancestor_map.get(current_id)
            if not info:
                break
            fund_code, next_id = info
            chain.append(fund_code)
            current_id = next_id
        chain.reverse()
        return chain

    # Keep source_fund_map for backward compat (direct parent lookup)
    source_fund_map: dict[UUID, str] = {
        lot_id: fc for lot_id, (fc, _) in ancestor_map.items()
    }

    # Load all relevant tax scheme rules
    schemes = list({lot.tax_scheme for lot in lots})
    rules_result = await db.execute(
        select(TaxSchemeRule).where(TaxSchemeRule.scheme.in_(schemes))
    )
    all_rules: list[TaxSchemeRule] = list(rules_result.scalars().all())

    def get_rule(scheme: str, purchase_date: date) -> TaxSchemeRule | None:
        # Rule in effect at purchase time (most recent active_from ≤ purchase_date)
        applicable = [r for r in all_rules if r.scheme == scheme and r.active_from <= purchase_date]
        return max(applicable, key=lambda r: r.active_from) if applicable else None

    results: list[LotEligibility] = []
    for lot in lots:
        rule = get_rule(lot.tax_scheme, lot.original_purchase_date)
        if rule is None:
            # Missing rule for this scheme — fail safe (NOT eligible) rather than
            # silently allow withdrawal. The NORMAL scheme has its own row
            # (holding_years=0) in seeded data, so this branch only fires for
            # truly unknown schemes or a missing seed.
            logger.warning(
                "tax_scheme_rules row missing for scheme=%s — marking lot %s NOT eligible",
                lot.tax_scheme, lot.id,
            )
            results.append(LotEligibility(
                lot_id=lot.id,
                source_lot_id=lot.source_lot_id,
                source_fund_code=source_fund_map.get(lot.source_lot_id) if lot.source_lot_id else None,
                switch_chain=build_switch_chain(lot.source_lot_id),
                fund_code=lot.fund_code,
                tax_scheme=lot.tax_scheme,
                original_purchase_date=lot.original_purchase_date,
                units_remaining=lot.units_remaining,
                cost_basis_remaining=lot.cost_basis_remaining,
                market_value=None,
                unrealized_pnl=None,
                is_eligible=False,
                eligible_date=None,
                days_remaining=0,
                holding_years_required=Decimal("0"),
                age_requirement=None,
            ))
            continue

        # Time gate: anniversary-based ("day-for-day"). Buy 2023-05-30 →
        # eligible 2033-05-30. Whole-year rules only (all current schemes).
        years_int = int(rule.holding_years)
        try:
            time_eligible_date = lot.original_purchase_date.replace(
                year=lot.original_purchase_date.year + years_int
            )
        except ValueError:
            # Feb 29 purchase landing in a non-leap target year — round to Mar 1.
            time_eligible_date = date(lot.original_purchase_date.year + years_int, 3, 1)
        eligible_date = time_eligible_date

        # Age gate: if rule requires age ≥ N and DOB is known
        if rule.age_requirement and user_dob:
            age_at_purchase = _age_on_date(user_dob, lot.original_purchase_date)
            if age_at_purchase < rule.age_requirement:
                # Age gate not yet satisfied at purchase time — eligible_date = later of the two
                try:
                    age_eligible_date = user_dob.replace(year=user_dob.year + rule.age_requirement)
                except ValueError:
                    # Handle Feb 29 leap-year birthday — round up to Mar 1
                    age_eligible_date = date(user_dob.year + rule.age_requirement, 3, 1)
                eligible_date = max(time_eligible_date, age_eligible_date)
            # else: already ≥ required age at purchase — only time gate applies

        is_eligible = today >= eligible_date
        days_remaining = max(0, (eligible_date - today).days)

        nav_info = nav_map.get(lot.fund_code)
        latest_nav = nav_info[0] if nav_info else None
        mv = (lot.units_remaining * latest_nav).quantize(QUANT) if latest_nav else None
        upnl = (mv - lot.cost_basis_remaining).quantize(QUANT) if mv is not None else None

        results.append(LotEligibility(
            lot_id=lot.id,
            source_lot_id=lot.source_lot_id,
            source_fund_code=source_fund_map.get(lot.source_lot_id) if lot.source_lot_id else None,
            switch_chain=build_switch_chain(lot.source_lot_id),
            fund_code=lot.fund_code,
            tax_scheme=lot.tax_scheme,
            original_purchase_date=lot.original_purchase_date,
            units_remaining=lot.units_remaining,
            cost_basis_remaining=lot.cost_basis_remaining,
            market_value=mv,
            unrealized_pnl=upnl,
            is_eligible=is_eligible,
            eligible_date=eligible_date,
            days_remaining=days_remaining,
            holding_years_required=rule.holding_years,
            age_requirement=rule.age_requirement,
        ))

    _cache_set(key, results)
    return results
