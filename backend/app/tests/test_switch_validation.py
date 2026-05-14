"""
Switch-pair validation tests — covers the rules from CLAUDE.md
that both the /switch API endpoint and the CSV importer rely on.
"""
from datetime import date
from decimal import Decimal

from app.services.switch_validation import SwitchLeg, validate_switch_pair


def leg(
    fund_code: str = "FUND_A",
    target_fund_code: str | None = "FUND_B",
    d: date = date(2024, 9, 10),
    tax_scheme: str = "NORMAL",
    amount: Decimal = Decimal("10000"),
) -> SwitchLeg:
    return SwitchLeg(
        fund_code=fund_code,
        target_fund_code=target_fund_code,
        date=d,
        tax_scheme=tax_scheme,
        amount=amount,
    )


class TestValidPair:
    def test_well_formed_pair_returns_no_errors(self):
        out = leg(fund_code="FUND_A", target_fund_code="FUND_B")
        in_ = leg(fund_code="FUND_B", target_fund_code="FUND_A")
        assert validate_switch_pair(out, in_) == []

    def test_amount_within_half_percent_accepted(self):
        out = leg(fund_code="FUND_A", target_fund_code="FUND_B", amount=Decimal("10000.00"))
        in_ = leg(fund_code="FUND_B", target_fund_code="FUND_A", amount=Decimal("9960.00"))  # 0.40% off
        assert validate_switch_pair(out, in_) == []


class TestSourceTargetMustDiffer:
    def test_same_fund_both_legs_rejected(self):
        out = leg(fund_code="FUND_A", target_fund_code="FUND_A")
        in_ = leg(fund_code="FUND_A", target_fund_code="FUND_A")
        errs = validate_switch_pair(out, in_)
        assert any("must differ" in e for e in errs)


class TestTargetFundCrossCheck:
    def test_switch_out_target_must_match_switch_in_fund(self):
        out = leg(fund_code="FUND_A", target_fund_code="FUND_X")  # wrong target
        in_ = leg(fund_code="FUND_B", target_fund_code="FUND_A")
        errs = validate_switch_pair(out, in_)
        assert any("SWITCH_OUT.target_fund_code" in e for e in errs)

    def test_switch_in_target_must_match_switch_out_fund(self):
        out = leg(fund_code="FUND_A", target_fund_code="FUND_B")
        in_ = leg(fund_code="FUND_B", target_fund_code="FUND_Y")  # wrong target
        errs = validate_switch_pair(out, in_)
        assert any("SWITCH_IN.target_fund_code" in e for e in errs)

    def test_missing_target_fund_codes_does_not_trigger_cross_check(self):
        """If both legs omit target_fund_code, cross-check is skipped (legacy CSVs)."""
        out = leg(fund_code="FUND_A", target_fund_code=None)
        in_ = leg(fund_code="FUND_B", target_fund_code=None)
        errs = validate_switch_pair(out, in_)
        # Other rules still apply, but no target_fund_code error
        assert not any("target_fund_code" in e for e in errs)


class TestDateMustMatch:
    def test_different_dates_rejected(self):
        out = leg(fund_code="FUND_A", target_fund_code="FUND_B", d=date(2024, 9, 10))
        in_ = leg(fund_code="FUND_B", target_fund_code="FUND_A", d=date(2024, 9, 11))
        errs = validate_switch_pair(out, in_)
        assert any("share a date" in e for e in errs)


class TestTaxSchemeMustMatch:
    def test_scheme_mismatch_rejected(self):
        out = leg(fund_code="FUND_A", target_fund_code="FUND_B", tax_scheme="SSF")
        in_ = leg(fund_code="FUND_B", target_fund_code="FUND_A", tax_scheme="NORMAL")
        errs = validate_switch_pair(out, in_)
        assert any("share a tax_scheme" in e for e in errs)


class TestAmountTolerance:
    def test_amount_difference_over_half_percent_rejected(self):
        out = leg(fund_code="FUND_A", target_fund_code="FUND_B", amount=Decimal("10000"))
        in_ = leg(fund_code="FUND_B", target_fund_code="FUND_A", amount=Decimal("9900"))  # 1% off
        errs = validate_switch_pair(out, in_)
        assert any("amounts differ" in e for e in errs)

    def test_amount_difference_at_exactly_half_percent_accepted(self):
        out = leg(fund_code="FUND_A", target_fund_code="FUND_B", amount=Decimal("10000"))
        in_ = leg(fund_code="FUND_B", target_fund_code="FUND_A", amount=Decimal("9950"))  # exactly 0.5%
        errs = validate_switch_pair(out, in_)
        assert not any("amounts differ" in e for e in errs)


class TestMultipleErrorsReported:
    def test_all_violations_reported_at_once(self):
        """All applicable rule violations are returned together (no early-exit)."""
        out = leg(
            fund_code="FUND_A",
            target_fund_code="FUND_X",
            d=date(2024, 9, 10),
            tax_scheme="SSF",
            amount=Decimal("10000"),
        )
        in_ = leg(
            fund_code="FUND_B",
            target_fund_code="FUND_Y",
            d=date(2024, 9, 11),
            tax_scheme="NORMAL",
            amount=Decimal("8000"),
        )
        errs = validate_switch_pair(out, in_)
        # We expect at least: target mismatch (out), target mismatch (in), date, scheme, amount
        assert len(errs) >= 5
