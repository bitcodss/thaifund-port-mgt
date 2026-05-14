"""
Pure FIFO lot-consumption logic — no DB dependencies, fully deterministic.
The DB layer (transaction_service.py) wraps these functions.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from typing import Optional
from uuid import UUID

QUANT = Decimal("0.00000001")
EPSILON = Decimal("0.000001")


def _q(d: Decimal) -> Decimal:
    return d.quantize(QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LotSnapshot:
    """Immutable view of a TaxLot used for FIFO computation."""
    id: UUID
    fund_code: str
    original_purchase_date: date
    units_remaining: Decimal
    cost_basis_remaining: Decimal
    tax_scheme: str


@dataclass(frozen=True)
class Consumption:
    lot_id: UUID
    units_consumed: Decimal
    cost_basis_consumed: Decimal


@dataclass(frozen=True)
class NewLot:
    fund_code: str
    original_purchase_date: date
    units_remaining: Decimal
    cost_basis_remaining: Decimal
    tax_scheme: str
    source_lot_id: Optional[UUID] = None


@dataclass(frozen=True)
class HoldingRule:
    """One row from tax_scheme_rules — passed in so rules stay as data."""
    scheme: str
    holding_years: Decimal
    age_requirement: Optional[int]


class InsufficientUnitsError(ValueError):
    pass


def fifo_consume(lots: list[LotSnapshot], units_needed: Decimal) -> list[Consumption]:
    """
    FIFO consume `units_needed` from `lots` (caller must pre-filter by fund+scheme).
    Lots are sorted by original_purchase_date ASC before consumption.
    Raises InsufficientUnitsError if total available < units_needed.
    """
    sorted_lots = sorted(lots, key=lambda l: (l.original_purchase_date, str(l.id)))
    remaining = units_needed
    result: list[Consumption] = []

    for lot in sorted_lots:
        if remaining <= EPSILON:
            break
        consume = min(lot.units_remaining, remaining)
        if consume == lot.units_remaining:
            # Exact full consumption: use exact cost_basis (no quantization drift).
            cost = lot.cost_basis_remaining
        else:
            # Partial: quantize proportional cost. Any sub-EPSILON residual stays
            # in the lot — analytics filters `units_remaining > 0` already and
            # the next consumption will pick it up. Never bump `consume` past
            # what the caller asked for: that would silently over-consume.
            fraction = consume / lot.units_remaining
            cost = _q(lot.cost_basis_remaining * fraction)
        result.append(Consumption(lot_id=lot.id, units_consumed=consume, cost_basis_consumed=cost))
        remaining -= consume

    if remaining > EPSILON:
        available = sum(l.units_remaining for l in lots)
        raise InsufficientUnitsError(
            f"Need {units_needed}, available {available}, short by {remaining}"
        )
    return result


def build_switch_in_lots(
    consumptions: list[Consumption],
    source_lots: dict[UUID, LotSnapshot],
    target_fund_code: str,
    target_nav: Decimal,
    switch_in_total_units: Decimal | None = None,
) -> list[NewLot]:
    """
    For each consumed source lot, create a new lot in the target fund.
    Invariants preserved:
    - original_purchase_date inherited from source
    - tax_scheme inherited from source
    - cost_basis_remaining == cost_basis_consumed (exact preservation)
    - units_remaining: when switch_in_total_units is provided (from the fund
      house's actual statement), units are allocated proportionally by cost_basis
      fraction so that sum(new_lots.units) == switch_in_total_units exactly.
      Falls back to cost_basis_consumed / target_nav when not provided.
    """
    total_cost = sum(c.cost_basis_consumed for c in consumptions)
    new_lots: list[NewLot] = []
    for c in consumptions:
        src = source_lots[c.lot_id]
        if switch_in_total_units is not None and total_cost > 0:
            fraction = c.cost_basis_consumed / total_cost
            new_units = _q(switch_in_total_units * fraction)
        else:
            new_units = _q(c.cost_basis_consumed / target_nav)
        new_lots.append(NewLot(
            fund_code=target_fund_code,
            original_purchase_date=src.original_purchase_date,
            units_remaining=new_units,
            cost_basis_remaining=c.cost_basis_consumed,
            tax_scheme=src.tax_scheme,
            source_lot_id=src.id,
        ))
    return new_lots


def is_holding_eligible(
    rule: HoldingRule,
    purchase_date: date,
    today: date,
    user_age: Optional[int],
) -> bool:
    """
    Return True if the lot has met its holding period requirement.

    Anniversary semantics ("day-for-day"): buy 2023-05-30, eligible on or after
    2033-05-30. NOT a day-count approximation. All current Thai schemes use
    whole-year holding periods; fractional years would need explicit handling.
    """
    if rule.holding_years == Decimal("0"):
        return True

    years_int = int(rule.holding_years)
    try:
        eligible_date = purchase_date.replace(year=purchase_date.year + years_int)
    except ValueError:
        # Feb 29 purchase landing in a non-leap target year — round to Mar 1.
        eligible_date = date(purchase_date.year + years_int, 3, 1)

    if today < eligible_date:
        return False

    if rule.age_requirement is not None:
        if user_age is None or user_age < rule.age_requirement:
            return False

    return True
