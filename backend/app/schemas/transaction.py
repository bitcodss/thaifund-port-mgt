from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


TransactionType = Literal[
    "BUY", "SELL", "SWITCH_OUT", "SWITCH_IN", "DIVIDEND", "INTEREST"
]
TaxScheme = Literal[
    "NORMAL", "RMF", "SSF", "THAI_ESG", "THAI_ESG_EXTRA", "LTF"
]


class TransactionCreate(BaseModel):
    date: date
    type: TransactionType
    fund_code: str | None = None
    units: Decimal | None = Field(default=None, gt=0)
    nav: Decimal | None = Field(default=None, gt=0)
    amount: Decimal = Field(..., gt=0)
    fee: Decimal = Field(default=Decimal("0"), ge=0)
    tax_withheld: Decimal = Field(default=Decimal("0"), ge=0)
    target_fund_code: str | None = None
    pair_id: str | None = None
    tax_scheme: TaxScheme = "NORMAL"
    note: str | None = None

    @model_validator(mode="after")
    def validate_type_fields(self) -> "TransactionCreate":
        if self.type in {"BUY", "SELL", "SWITCH_OUT", "SWITCH_IN"}:
            if self.units is None:
                raise ValueError(f"units required for {self.type}")
            if self.nav is None:
                raise ValueError(f"nav required for {self.type}")
            if not self.fund_code:
                raise ValueError(f"fund_code required for {self.type}")
        if self.type == "DIVIDEND" and not self.fund_code:
            raise ValueError("fund_code required for DIVIDEND")
        return self


class TransactionOut(BaseModel):
    id: UUID
    portfolio_id: UUID
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
    created_at: datetime

    model_config = {"from_attributes": True}


class TaxLotOut(BaseModel):
    id: UUID
    portfolio_id: UUID
    fund_code: str
    original_purchase_date: date
    units_remaining: Decimal
    cost_basis_remaining: Decimal
    tax_scheme: str
    source_lot_id: UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CsvImportResponse(BaseModel):
    imported: int
    errors: list[str]
