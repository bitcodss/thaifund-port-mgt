"""
CSV import parsing and validation tests — no DB required.
Only the parse+validate phase is tested here; DB persistence is integration.
"""
import pytest
from decimal import Decimal
from io import StringIO

from app.services.csv_import import parse_csv, CsvRow, CsvValidationError


VALID_CSV = """\
date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note
2024-03-15,BUY,SCBSET,1000,12.3456,12345.60,0,0,,,NORMAL,
2024-08-20,SELL,SCBSET,500,13.2100,6605.00,33.03,0,,,NORMAL,
2024-09-10,SWITCH_OUT,SCBSET,500,13.5000,6750.00,0,0,SCBTOP,switch-001,NORMAL,
2024-09-10,SWITCH_IN,SCBTOP,450,15.0000,6750.00,0,0,SCBSET,switch-001,NORMAL,
2024-12-15,DIVIDEND,SCBSET,,,250.00,0,25.00,,,NORMAL,Q4 dividend
2024-12-31,INTEREST,,,,150.50,0,15.05,,,NORMAL,Cash interest
"""


class TestValidCsv:
    def test_parse_valid_csv_returns_all_rows(self):
        rows, errors = parse_csv(StringIO(VALID_CSV))
        assert len(errors) == 0
        assert len(rows) == 6

    def test_buy_row_fields(self):
        rows, _ = parse_csv(StringIO(VALID_CSV))
        buy = rows[0]
        assert buy.type == "BUY"
        assert buy.fund_code == "SCBSET"
        assert buy.units == Decimal("1000")
        assert buy.nav == Decimal("12.3456")
        assert buy.amount == Decimal("12345.60")
        assert buy.tax_scheme == "NORMAL"

    def test_sell_row_fields(self):
        rows, _ = parse_csv(StringIO(VALID_CSV))
        sell = rows[1]
        assert sell.type == "SELL"
        assert sell.fee == Decimal("33.03")

    def test_switch_pair_parsed(self):
        rows, _ = parse_csv(StringIO(VALID_CSV))
        sw_out = rows[2]
        sw_in = rows[3]
        assert sw_out.type == "SWITCH_OUT"
        assert sw_in.type == "SWITCH_IN"
        assert sw_out.pair_id == sw_in.pair_id == "switch-001"
        assert sw_out.target_fund_code == "SCBTOP"
        assert sw_in.target_fund_code == "SCBSET"

    def test_dividend_row_no_units_nav(self):
        rows, _ = parse_csv(StringIO(VALID_CSV))
        div = rows[4]
        assert div.type == "DIVIDEND"
        assert div.units is None
        assert div.nav is None
        assert div.amount == Decimal("250.00")
        assert div.tax_withheld == Decimal("25.00")

    def test_interest_row_no_fund_code(self):
        rows, _ = parse_csv(StringIO(VALID_CSV))
        interest = rows[5]
        assert interest.type == "INTEREST"
        assert interest.fund_code is None
        assert interest.amount == Decimal("150.50")


class TestMalformedRows:
    def test_buy_missing_units(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-01-01,BUY,SCBSET,,12.00,12000.00,0,0,,,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("units" in e.lower() for e in errors)

    def test_buy_units_nav_amount_mismatch(self):
        """units × nav must equal amount within 0.01."""
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-01-01,BUY,SCBSET,1000,12.00,9999.00,0,0,,,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("amount" in e.lower() or "mismatch" in e.lower() for e in errors)

    def test_invalid_date_format(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "01/01/2024,BUY,SCBSET,1000,12.00,12000.00,0,0,,,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert len(errors) > 0

    def test_invalid_transaction_type(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-01-01,DONATE,SCBSET,1000,12.00,12000.00,0,0,,,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert len(errors) > 0

    def test_dividend_missing_fund_code(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-01-01,DIVIDEND,,,,250.00,0,25.00,,,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("fund_code" in e.lower() for e in errors)

    def test_interest_missing_amount(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-01-01,INTEREST,,,,,0,0,,,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("amount" in e.lower() for e in errors)

    def test_invalid_tax_scheme(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-01-01,BUY,SCBSET,1000,12.00,12000.00,0,0,,,BOGUS,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert len(errors) > 0


class TestSwitchPairValidation:
    def test_switch_amount_mismatch_over_half_percent(self):
        """SWITCH_OUT amount vs SWITCH_IN amount more than 0.5%: rejected."""
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-09-10,SWITCH_OUT,SCBSET,500,13.50,6750.00,0,0,SCBTOP,sw1,NORMAL,\n"
            "2024-09-10,SWITCH_IN,SCBTOP,400,14.00,5600.00,0,0,SCBSET,sw1,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("amount" in e.lower() or "mismatch" in e.lower() for e in errors)

    def test_switch_amount_within_half_percent_accepted(self):
        """SWITCH_OUT vs SWITCH_IN within 0.5%: accepted."""
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-09-10,SWITCH_OUT,SCBSET,500,13.50,6750.00,0,0,SCBTOP,sw1,NORMAL,\n"
            "2024-09-10,SWITCH_IN,SCBTOP,449,15.0334,6750.00,0,0,SCBSET,sw1,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(errors) == 0
        assert len(rows) == 2

    def test_switch_out_without_in_is_rejected(self):
        """SWITCH_OUT with no matching SWITCH_IN pair_id: rejected."""
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-09-10,SWITCH_OUT,SCBSET,500,13.50,6750.00,0,0,SCBTOP,sw-orphan,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("pair" in e.lower() or "switch" in e.lower() for e in errors)

    def test_switch_in_without_out_is_rejected(self):
        """SWITCH_IN with no matching SWITCH_OUT: rejected."""
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-09-10,SWITCH_IN,SCBTOP,450,15.00,6750.00,0,0,SCBSET,sw-orphan,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("pair" in e.lower() or "switch" in e.lower() for e in errors)

    def test_switch_different_dates_is_rejected(self):
        """SWITCH_OUT and SWITCH_IN with different dates: rejected."""
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-09-10,SWITCH_OUT,SCBSET,500,13.50,6750.00,0,0,SCBTOP,sw1,NORMAL,\n"
            "2024-09-11,SWITCH_IN,SCBTOP,450,15.00,6750.00,0,0,SCBSET,sw1,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("date" in e.lower() for e in errors)


class TestDuplicateDetection:
    def test_duplicate_rows_in_same_csv_rejected(self):
        """Same (date, type, fund_code, units, amount) twice in one file: second rejected."""
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-01-01,BUY,SCBSET,1000,12.00,12000.00,0,0,,,NORMAL,\n"
            "2024-01-01,BUY,SCBSET,1000,12.00,12000.00,0,0,,,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 1  # first accepted, second deduplicated
        assert len(errors) == 1


class TestSwitchTargetFundCrossCheck:
    def test_switch_out_target_must_match_switch_in_fund(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-09-10,SWITCH_OUT,SCBSET,500,13.50,6750.00,0,0,WRONG_FUND,sw1,NORMAL,\n"
            "2024-09-10,SWITCH_IN,SCBTOP,450,15.00,6750.00,0,0,SCBSET,sw1,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("target_fund_code" in e for e in errors)

    def test_switch_legs_must_have_same_tax_scheme(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-09-10,SWITCH_OUT,SCBSET,500,13.50,6750.00,0,0,SCBTOP,sw1,SSF,\n"
            "2024-09-10,SWITCH_IN,SCBTOP,450,15.00,6750.00,0,0,SCBSET,sw1,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("tax_scheme" in e for e in errors)

    def test_switch_to_same_fund_is_rejected(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-09-10,SWITCH_OUT,SCBSET,500,13.50,6750.00,0,0,SCBSET,sw1,NORMAL,\n"
            "2024-09-10,SWITCH_IN,SCBSET,500,13.50,6750.00,0,0,SCBSET,sw1,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("must differ" in e for e in errors)


class TestPositiveNumberValidation:
    def test_negative_units_rejected(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-01-01,BUY,SCBSET,-1000,12.00,-12000.00,0,0,,,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("positive" in e.lower() for e in errors)

    def test_zero_amount_rejected(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-01-01,DIVIDEND,SCBSET,,,0.00,0,0,,,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("amount" in e.lower() and "positive" in e.lower() for e in errors)

    def test_negative_fee_rejected(self):
        csv = (
            "date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note\n"
            "2024-01-01,BUY,SCBSET,1000,12.00,12000.00,-5,0,,,NORMAL,\n"
        )
        rows, errors = parse_csv(StringIO(csv))
        assert len(rows) == 0
        assert any("fee" in e.lower() for e in errors)
