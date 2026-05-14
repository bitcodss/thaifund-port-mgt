"""
Shared switch-pair validation — used by the /switch API endpoint and the CSV
importer so both paths enforce the same CLAUDE.md invariants.

Rules:
  1. Source and target funds must differ.
  2. SWITCH_OUT.target_fund_code == SWITCH_IN.fund_code, and vice versa.
  3. Both legs share the same date.
  4. Both legs share the same tax_scheme.
  5. Amounts agree within 0.5% (allows fund-house fees).

Same-AMC validation is intentionally NOT included here: it requires a DB lookup
against the funds table, which keeps this module pure. Apply it at the call site
when fund metadata is available.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

SWITCH_AMOUNT_TOLERANCE_PCT = Decimal("0.005")  # 0.5%


@dataclass(frozen=True)
class SwitchLeg:
    fund_code: str
    target_fund_code: str | None
    date: date
    tax_scheme: str
    amount: Decimal


def validate_switch_pair(out_leg: SwitchLeg, in_leg: SwitchLeg) -> list[str]:
    """Return a list of error strings, empty if the pair is valid."""
    errors: list[str] = []

    if out_leg.fund_code == in_leg.fund_code:
        errors.append(f"Switch source and target funds must differ ({out_leg.fund_code})")

    if out_leg.target_fund_code and out_leg.target_fund_code != in_leg.fund_code:
        errors.append(
            f"SWITCH_OUT.target_fund_code={out_leg.target_fund_code} "
            f"does not match SWITCH_IN.fund_code={in_leg.fund_code}"
        )

    if in_leg.target_fund_code and in_leg.target_fund_code != out_leg.fund_code:
        errors.append(
            f"SWITCH_IN.target_fund_code={in_leg.target_fund_code} "
            f"does not match SWITCH_OUT.fund_code={out_leg.fund_code}"
        )

    if out_leg.date != in_leg.date:
        errors.append(
            f"Switch legs must share a date "
            f"(SWITCH_OUT={out_leg.date}, SWITCH_IN={in_leg.date})"
        )

    if out_leg.tax_scheme != in_leg.tax_scheme:
        errors.append(
            f"Switch legs must share a tax_scheme "
            f"(SWITCH_OUT={out_leg.tax_scheme}, SWITCH_IN={in_leg.tax_scheme})"
        )

    if out_leg.amount > Decimal("0"):
        pct_diff = abs(out_leg.amount - in_leg.amount) / out_leg.amount
        if pct_diff > SWITCH_AMOUNT_TOLERANCE_PCT:
            errors.append(
                f"Switch amounts differ by {pct_diff * 100:.2f}% (max 0.5%): "
                f"SWITCH_OUT={out_leg.amount}, SWITCH_IN={in_leg.amount}"
            )

    return errors
