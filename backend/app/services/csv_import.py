"""
CSV import — two phases:
  1. parse_csv(file) → (valid_rows, error_messages)   [pure, no DB]
  2. import_rows(db, portfolio_id, rows) → count       [DB, calls lot engine]
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO, TextIOWrapper
from typing import Union

VALID_TYPES = {"BUY", "SELL", "SWITCH_OUT", "SWITCH_IN", "DIVIDEND", "INTEREST"}
VALID_SCHEMES = {"NORMAL", "RMF", "SSF", "THAI_ESG", "THAI_ESG_EXTRA", "LTF"}
AMOUNT_TOLERANCE = Decimal("0.01")
SWITCH_AMOUNT_TOLERANCE_PCT = Decimal("0.005")


@dataclass
class CsvRow:
    date: date
    type: str
    fund_code: str | None
    units: Decimal | None
    nav: Decimal | None
    amount: Decimal
    fee: Decimal
    tax_withheld: Decimal
    target_fund_code: str | None
    pair_id: str | None
    tax_scheme: str
    note: str | None


class CsvValidationError(Exception):
    pass


def _d(val: str) -> Decimal | None:
    val = val.strip()
    if not val:
        return None
    try:
        return Decimal(val)
    except InvalidOperation:
        return None


def _req_d(val: str, field: str, row_num: int) -> tuple[Decimal | None, str | None]:
    result = _d(val)
    if result is None:
        return None, f"Row {row_num}: {field} is required and must be a number"
    return result, None


def _parse_row(raw: dict, row_num: int) -> tuple[CsvRow | None, str | None]:
    try:
        tx_date = date.fromisoformat(raw["date"].strip())
    except (ValueError, KeyError):
        return None, f"Row {row_num}: invalid date '{raw.get('date', '')}' (expected YYYY-MM-DD)"

    tx_type = raw.get("type", "").strip().upper()
    if tx_type not in VALID_TYPES:
        return None, f"Row {row_num}: invalid type '{tx_type}'"

    tax_scheme = raw.get("tax_scheme", "").strip().upper()
    if tax_scheme not in VALID_SCHEMES:
        return None, f"Row {row_num}: invalid tax_scheme '{tax_scheme}'"

    fund_code = raw.get("fund_code", "").strip() or None
    target_fund_code = raw.get("target_fund_code", "").strip() or None
    pair_id = raw.get("pair_id", "").strip() or None
    note = raw.get("note", "").strip() or None

    units = _d(raw.get("units", ""))
    nav = _d(raw.get("nav", ""))
    fee = _d(raw.get("fee", "") or "0") or Decimal("0")
    tax_withheld = _d(raw.get("tax_withheld", "") or "0") or Decimal("0")

    amount, err = _req_d(raw.get("amount", ""), "amount", row_num)
    if err:
        return None, err

    # Type-specific validation
    if tx_type in {"BUY", "SELL", "SWITCH_OUT", "SWITCH_IN"}:
        if units is None:
            return None, f"Row {row_num}: units is required for {tx_type}"
        if nav is None:
            return None, f"Row {row_num}: nav is required for {tx_type}"
        expected = (units * nav).quantize(Decimal("0.01"))
        actual = amount.quantize(Decimal("0.01"))
        if abs(expected - actual) > AMOUNT_TOLERANCE:
            return None, (
                f"Row {row_num}: amount mismatch — units×nav={expected}, amount={actual}"
            )

    if tx_type == "DIVIDEND":
        if not fund_code:
            return None, f"Row {row_num}: fund_code is required for DIVIDEND"

    if tx_type == "INTEREST":
        if amount is None:
            return None, f"Row {row_num}: amount is required for INTEREST"

    return CsvRow(
        date=tx_date,
        type=tx_type,
        fund_code=fund_code,
        units=units,
        nav=nav,
        amount=amount,
        fee=fee,
        tax_withheld=tax_withheld,
        target_fund_code=target_fund_code,
        pair_id=pair_id,
        tax_scheme=tax_scheme,
        note=note,
    ), None


def parse_csv(file: Union[StringIO, TextIOWrapper]) -> tuple[list[CsvRow], list[str]]:
    """
    Phase 1: parse and validate CSV rows.
    Returns (valid_rows, error_messages).
    Switch pairs, duplicates, and cross-row checks are validated here.
    """
    reader = csv.DictReader(file)
    raw_rows = list(reader)

    valid: list[CsvRow] = []
    errors: list[str] = []

    # Per-row parse
    parsed: list[CsvRow | None] = []
    for i, raw in enumerate(raw_rows, start=2):  # row 1 = header
        row, err = _parse_row(raw, i)
        if err:
            errors.append(err)
            parsed.append(None)
        else:
            parsed.append(row)

    valid_parsed = [r for r in parsed if r is not None]

    # Duplicate detection within the same file
    seen: set[tuple] = set()
    deduped: list[CsvRow] = []
    for i, row in enumerate(valid_parsed):
        key = (row.date, row.type, row.fund_code, row.units, row.amount)
        if key in seen:
            errors.append(
                f"Duplicate row detected: {row.date} {row.type} {row.fund_code} "
                f"units={row.units} amount={row.amount}"
            )
        else:
            seen.add(key)
            deduped.append(row)

    # Switch pair validation
    switch_out: dict[str, CsvRow] = {}
    switch_in: dict[str, CsvRow] = {}
    for row in deduped:
        if row.type == "SWITCH_OUT" and row.pair_id:
            switch_out[row.pair_id] = row
        if row.type == "SWITCH_IN" and row.pair_id:
            switch_in[row.pair_id] = row

    orphan_pair_ids: set[str] = set()

    for pid, out_row in switch_out.items():
        if pid not in switch_in:
            orphan_pair_ids.add(pid)
            errors.append(f"SWITCH_OUT pair_id='{pid}' has no matching SWITCH_IN")

    for pid, in_row in switch_in.items():
        if pid not in switch_out:
            orphan_pair_ids.add(pid)
            errors.append(f"SWITCH_IN pair_id='{pid}' has no matching SWITCH_OUT")

    for pid in switch_out.keys() & switch_in.keys():
        out_row = switch_out[pid]
        in_row = switch_in[pid]
        # Same date
        if out_row.date != in_row.date:
            orphan_pair_ids.add(pid)
            errors.append(
                f"Switch pair '{pid}': SWITCH_OUT date {out_row.date} != SWITCH_IN date {in_row.date}"
            )
            continue
        # Amount within 0.5%
        if out_row.amount > Decimal("0"):
            pct_diff = abs(out_row.amount - in_row.amount) / out_row.amount
            if pct_diff > SWITCH_AMOUNT_TOLERANCE_PCT:
                orphan_pair_ids.add(pid)
                errors.append(
                    f"Switch pair '{pid}': amount mismatch {out_row.amount} vs {in_row.amount} "
                    f"({pct_diff:.2%} > 0.5%)"
                )

    # Remove orphaned switch rows from final output
    final: list[CsvRow] = []
    for row in deduped:
        if row.pair_id and row.pair_id in orphan_pair_ids:
            continue
        final.append(row)

    return final, errors
