import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class FundCreate(BaseModel):
    fund_code: str
    sec_proj_id: str | None = None
    name_th: str | None = None
    name_en: str | None = None
    amc: str | None = None
    asset_class: str | None = None
    risk_level: int | None = None
    benchmark: str | None = None
    fund_type: str | None = None


class FundUpdate(BaseModel):
    sec_proj_id: str | None = None
    name_th: str | None = None
    name_en: str | None = None
    amc: str | None = None
    asset_class: str | None = None
    risk_level: int | None = None
    benchmark: str | None = None
    fund_type: str | None = None


class FundOut(BaseModel):
    fund_code: str
    sec_proj_id: str | None
    name_th: str | None
    name_en: str | None
    amc: str | None
    amc_unique_id: str | None
    asset_class: str | None
    risk_level: int | None
    benchmark: str | None
    fund_type: str | None
    fund_status: str | None
    last_synced_at: datetime | None
    last_nav_date: date | None

    model_config = {"from_attributes": True}


class NavHistoryOut(BaseModel):
    fund_code: str
    trade_date: date
    nav: Decimal
    change_pct: Decimal | None

    model_config = {"from_attributes": True}


class SyncResult(BaseModel):
    job_type: str
    synced: int
    skipped: int
    errors: list[str]


class SyncJobOut(BaseModel):
    id: uuid.UUID
    type: str
    started_at: datetime
    completed_at: datetime | None
    status: str
    error_message: str | None

    model_config = {"from_attributes": True}
