"""
Lot engine tests — written before implementation.
All tests use pure Python dataclasses: no DB, no async, fully deterministic.
"""
import pytest
from decimal import Decimal
from datetime import date
from uuid import uuid4

from app.services.lot_engine import (
    LotSnapshot,
    Consumption,
    NewLot,
    HoldingRule,
    InsufficientUnitsError,
    fifo_consume,
    build_switch_in_lots,
    is_holding_eligible,
)

D = Decimal


def lot(fund, purchase_date, units, cost, scheme="NORMAL") -> LotSnapshot:
    return LotSnapshot(
        id=uuid4(),
        fund_code=fund,
        original_purchase_date=purchase_date,
        units_remaining=D(str(units)),
        cost_basis_remaining=D(str(cost)),
        tax_scheme=scheme,
    )


# ── FIFO Consume ─────────────────────────────────────────────────────────────

class TestFifoConsume:
    def test_partial_sell_oldest_first(self):
        """BUY → BUY → partial SELL: oldest lot consumed first, second lot untouched."""
        lot1 = lot("SCBSET", date(2023, 1, 1), 1000, 12000)
        lot2 = lot("SCBSET", date(2023, 6, 1), 1000, 13000)

        result = fifo_consume([lot1, lot2], D("500"))

        assert len(result) == 1
        assert result[0].lot_id == lot1.id
        assert result[0].units_consumed == D("500")
        assert result[0].cost_basis_consumed == D("6000.00000000")

    def test_sell_larger_than_oldest_partially_consumes_second(self):
        """BUY → BUY → SELL larger than oldest: oldest fully consumed, second partially consumed."""
        lot1 = lot("SCBSET", date(2023, 1, 1), 1000, 12000)
        lot2 = lot("SCBSET", date(2023, 6, 1), 1000, 13000)

        result = fifo_consume([lot1, lot2], D("1500"))

        assert len(result) == 2
        assert result[0].lot_id == lot1.id
        assert result[0].units_consumed == D("1000")
        assert result[0].cost_basis_consumed == D("12000")
        assert result[1].lot_id == lot2.id
        assert result[1].units_consumed == D("500")
        assert result[1].cost_basis_consumed == D("6500.00000000")

    def test_fifo_sorts_by_date_regardless_of_input_order(self):
        """Lots not in date order: FIFO must sort by original_purchase_date."""
        lot_newer = lot("SCBSET", date(2023, 6, 1), 1000, 13000)
        lot_older = lot("SCBSET", date(2023, 1, 1), 1000, 12000)

        result = fifo_consume([lot_newer, lot_older], D("500"))

        assert result[0].lot_id == lot_older.id

    def test_exact_full_consumption_uses_exact_cost(self):
        """Consuming all units uses exact cost_basis_remaining to avoid rounding drift."""
        lot1 = lot("SCBSET", date(2023, 1, 1), "1000", "12345.67890123")
        result = fifo_consume([lot1], D("1000"))
        assert result[0].cost_basis_consumed == lot1.cost_basis_remaining

    def test_insufficient_units_raises(self):
        lot1 = lot("SCBSET", date(2023, 1, 1), 100, 1000)
        with pytest.raises(InsufficientUnitsError):
            fifo_consume([lot1], D("200"))

    def test_empty_lot_list_raises(self):
        with pytest.raises(InsufficientUnitsError):
            fifo_consume([], D("100"))

    def test_tax_scheme_isolation_caller_filters(self):
        """
        SELL of SSF lots when both SSF and RMF exist: caller pre-filters by scheme.
        This test confirms FIFO only consumes what it's given.
        """
        ssf_lot = lot("SCBSET", date(2023, 1, 1), 500, 6000, scheme="SSF")
        rmf_lot = lot("SCBSET", date(2023, 1, 1), 500, 6000, scheme="RMF")

        # Caller passes only SSF lots — RMF must never be in the input
        result = fifo_consume([ssf_lot], D("500"))

        assert len(result) == 1
        assert result[0].lot_id == ssf_lot.id
        # rmf_lot was never passed, so it's never touched — confirmed by absence

    def test_multiple_lots_same_date_stable_order(self):
        """Two lots on same date: consume both in some deterministic order."""
        lot1 = lot("SCBSET", date(2023, 1, 1), 500, 5000)
        lot2 = lot("SCBSET", date(2023, 1, 1), 500, 6000)
        result = fifo_consume([lot1, lot2], D("1000"))
        consumed_ids = {c.lot_id for c in result}
        assert consumed_ids == {lot1.id, lot2.id}

    def test_cost_proportional_on_partial(self):
        """Partial consume: cost_basis_consumed = proportion * total cost."""
        lot1 = lot("SCBSET", date(2023, 1, 1), 1000, 10000)
        result = fifo_consume([lot1], D("250"))
        # 250/1000 = 0.25 fraction → 0.25 * 10000 = 2500
        assert result[0].cost_basis_consumed == D("2500.00000000")


# ── Switch In Lots ───────────────────────────────────────────────────────────

class TestBuildSwitchInLots:
    def test_full_switch_inherits_original_date_and_cost(self):
        """BUY → full SWITCH: new lot in target fund with same original_purchase_date and cost."""
        purchase_date = date(2022, 5, 15)
        src = lot("SCBSET", purchase_date, 1000, 12000)
        consumptions = fifo_consume([src], D("1000"))

        new_lots = build_switch_in_lots(
            consumptions=consumptions,
            source_lots={src.id: src},
            target_fund_code="SCBTOP",
            target_nav=D("15.0000"),
        )

        assert len(new_lots) == 1
        nl = new_lots[0]
        assert nl.fund_code == "SCBTOP"
        assert nl.original_purchase_date == purchase_date  # inherited
        assert nl.cost_basis_remaining == D("12000")        # preserved
        assert nl.source_lot_id == src.id
        assert nl.tax_scheme == "NORMAL"

    def test_switch_units_computed_from_cost_and_nav(self):
        """units_remaining = cost_basis_consumed / target_nav."""
        src = lot("SCBSET", date(2022, 1, 1), 1000, 15000)
        consumptions = fifo_consume([src], D("1000"))

        new_lots = build_switch_in_lots(
            consumptions, {src.id: src}, "SCBTOP", D("20.0000")
        )

        # 15000 / 20 = 750
        assert new_lots[0].units_remaining == D("750.00000000")

    def test_partial_switch_fifo_only_oldest(self):
        """BUY → BUY → partial SWITCH: FIFO; only oldest lot is switched."""
        lot1 = lot("SCBSET", date(2022, 1, 1), 1000, 10000)
        lot2 = lot("SCBSET", date(2023, 1, 1), 1000, 12000)

        consumptions = fifo_consume([lot1, lot2], D("1000"))
        new_lots = build_switch_in_lots(
            consumptions, {lot1.id: lot1, lot2.id: lot2}, "SCBTOP", D("12.5000")
        )

        assert len(new_lots) == 1
        assert new_lots[0].original_purchase_date == lot1.original_purchase_date

    def test_chained_switches_original_date_survives(self):
        """Chained switches A → B → C: original_purchase_date survives both hops."""
        original_date = date(2020, 3, 10)

        # Hop 1: A → B
        lot_a = lot("FUND_A", original_date, 1000, 10000)
        cons_ab = fifo_consume([lot_a], D("1000"))
        lots_b = build_switch_in_lots(cons_ab, {lot_a.id: lot_a}, "FUND_B", D("12.0000"))

        # Materialise as LotSnapshot for next hop
        snap_b = LotSnapshot(
            id=uuid4(),
            fund_code="FUND_B",
            original_purchase_date=lots_b[0].original_purchase_date,
            units_remaining=lots_b[0].units_remaining,
            cost_basis_remaining=lots_b[0].cost_basis_remaining,
            tax_scheme=lots_b[0].tax_scheme,
        )

        # Hop 2: B → C
        cons_bc = fifo_consume([snap_b], snap_b.units_remaining)
        lots_c = build_switch_in_lots(cons_bc, {snap_b.id: snap_b}, "FUND_C", D("15.0000"))

        assert lots_c[0].original_purchase_date == original_date  # survived both hops

    def test_cost_basis_preserved_total_across_switch(self):
        """Total cost basis: sum(switch_out) == sum(switch_in)."""
        lot1 = lot("SCBSET", date(2022, 1, 1), 600, D("7200.12345678"))
        lot2 = lot("SCBSET", date(2023, 1, 1), 400, D("5200.87654321"))
        total_cost = lot1.cost_basis_remaining + lot2.cost_basis_remaining

        consumptions = fifo_consume([lot1, lot2], D("1000"))
        new_lots = build_switch_in_lots(
            consumptions, {lot1.id: lot1, lot2.id: lot2}, "SCBTOP", D("14.0000")
        )

        switched_cost = sum(nl.cost_basis_remaining for nl in new_lots)
        assert switched_cost == total_cost

    def test_tax_scheme_inherited_on_switch(self):
        """tax_scheme of source lot is inherited by the new lot."""
        src = lot("SCBSET", date(2022, 1, 1), 1000, 12000, scheme="RMF")
        consumptions = fifo_consume([src], D("1000"))
        new_lots = build_switch_in_lots(consumptions, {src.id: src}, "SCBTOP", D("15.0000"))
        assert new_lots[0].tax_scheme == "RMF"

    def test_multi_lot_switch_creates_one_new_lot_per_consumed(self):
        """When two lots are consumed in a switch, two new lots are created in target fund."""
        lot1 = lot("SCBSET", date(2022, 1, 1), 500, 5000)
        lot2 = lot("SCBSET", date(2023, 1, 1), 500, 6000)
        consumptions = fifo_consume([lot1, lot2], D("1000"))
        new_lots = build_switch_in_lots(
            consumptions, {lot1.id: lot1, lot2.id: lot2}, "SCBTOP", D("13.0000")
        )
        assert len(new_lots) == 2
        dates = {nl.original_purchase_date for nl in new_lots}
        assert dates == {lot1.original_purchase_date, lot2.original_purchase_date}


# ── Holding Period ────────────────────────────────────────────────────────────

class TestHoldingPeriod:
    """is_holding_eligible takes a HoldingRule so rules are data, not hardcode."""

    SSF_RULE = HoldingRule(scheme="SSF", holding_years=D("10"), age_requirement=None)
    RMF_RULE = HoldingRule(scheme="RMF", holding_years=D("5"), age_requirement=55)
    LTF_RULE = HoldingRule(scheme="LTF", holding_years=D("5"), age_requirement=None)
    THAI_ESG_RULE = HoldingRule(scheme="THAI_ESG", holding_years=D("5"), age_requirement=None)
    THAI_ESG_EXTRA_RULE = HoldingRule(scheme="THAI_ESG_EXTRA", holding_years=D("8"), age_requirement=None)
    NORMAL_RULE = HoldingRule(scheme="NORMAL", holding_years=D("0"), age_requirement=None)

    def test_ssf_eligible_just_after_10_years(self):
        purchase = date(2015, 1, 1)
        today = date(2025, 1, 2)  # > 10 years
        assert is_holding_eligible(self.SSF_RULE, purchase, today, user_age=None) is True

    def test_ssf_not_eligible_one_day_short(self):
        # 2015-01-01 to 2024-12-31 = 3652 days (< 3652.5 = 10×365.25)
        # so still short of the 10-year threshold
        purchase = date(2015, 1, 1)
        today = date(2024, 12, 31)
        assert is_holding_eligible(self.SSF_RULE, purchase, today, user_age=None) is False

    def test_rmf_eligible_with_5_years_and_age_55(self):
        purchase = date(2018, 6, 1)
        today = date(2024, 6, 2)  # > 5 years
        assert is_holding_eligible(self.RMF_RULE, purchase, today, user_age=55) is True

    def test_rmf_not_eligible_if_age_under_55(self):
        purchase = date(2018, 6, 1)
        today = date(2024, 6, 2)
        assert is_holding_eligible(self.RMF_RULE, purchase, today, user_age=54) is False

    def test_rmf_not_eligible_if_age_none(self):
        purchase = date(2018, 6, 1)
        today = date(2024, 6, 2)
        assert is_holding_eligible(self.RMF_RULE, purchase, today, user_age=None) is False

    def test_rmf_not_eligible_under_5_years_even_if_age_ok(self):
        purchase = date(2021, 1, 1)
        today = date(2024, 6, 1)  # < 5 years
        assert is_holding_eligible(self.RMF_RULE, purchase, today, user_age=60) is False

    def test_ltf_eligible_after_5_years(self):
        purchase = date(2018, 1, 1)
        today = date(2023, 1, 2)
        assert is_holding_eligible(self.LTF_RULE, purchase, today, user_age=None) is True

    def test_thai_esg_eligible_after_5_years(self):
        purchase = date(2019, 3, 1)
        today = date(2024, 3, 2)
        assert is_holding_eligible(self.THAI_ESG_RULE, purchase, today, user_age=None) is True

    def test_thai_esg_extra_eligible_after_8_years(self):
        purchase = date(2015, 1, 1)
        today = date(2023, 1, 2)
        assert is_holding_eligible(self.THAI_ESG_EXTRA_RULE, purchase, today, user_age=None) is True

    def test_thai_esg_extra_not_eligible_at_5_years(self):
        purchase = date(2018, 1, 1)
        today = date(2023, 1, 2)  # 5 years — not enough for EXTRA (needs 8)
        assert is_holding_eligible(self.THAI_ESG_EXTRA_RULE, purchase, today, user_age=None) is False

    def test_normal_always_eligible(self):
        purchase = date(2024, 1, 1)
        today = date(2024, 1, 2)
        assert is_holding_eligible(self.NORMAL_RULE, purchase, today, user_age=None) is True

    def test_anniversary_day_for_day_user_example(self):
        """User's example: buy 2023-05-30, sellable on 2033-05-30 exactly.
        Day-before is NOT eligible; day-of IS eligible."""
        purchase = date(2023, 5, 30)
        assert is_holding_eligible(self.SSF_RULE, purchase, date(2033, 5, 29), None) is False
        assert is_holding_eligible(self.SSF_RULE, purchase, date(2033, 5, 30), None) is True

    def test_feb_29_purchase_falls_back_to_mar_1_in_non_leap_year(self):
        """Feb 29, 2024 + 5 years = 2029-Feb-29 doesn't exist → use 2029-03-01."""
        purchase = date(2024, 2, 29)
        # 5y anniversary lands on a non-leap year — eligible_date = Mar 1
        assert is_holding_eligible(self.LTF_RULE, purchase, date(2029, 2, 28), None) is False
        assert is_holding_eligible(self.LTF_RULE, purchase, date(2029, 3, 1), None) is True
