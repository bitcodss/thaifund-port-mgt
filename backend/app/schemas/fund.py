import uuid
from datetime import date, datetime
from decimal import Decimal

from typing import Literal

from pydantic import BaseModel, computed_field, field_validator

from app.services.fund_taxonomy import REGIONS, THEMES, category_label


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
    # Setting region/theme marks the category as a manual override (never
    # overwritten by sync). category_source="ai" hands it back to the classifier.
    region: str | None = None
    theme: str | None = None
    category_source: Literal["ai"] | None = None

    @field_validator("region")
    @classmethod
    def _known_region(cls, v: str | None) -> str | None:
        if v is not None and v not in REGIONS:
            raise ValueError(f"region must be one of {REGIONS}")
        return v

    @field_validator("theme")
    @classmethod
    def _known_theme(cls, v: str | None) -> str | None:
        if v is not None and v not in THEMES:
            raise ValueError(f"theme must be one of {THEMES}")
        return v


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
    region: str | None = None
    theme: str | None = None
    category_source: str | None = None
    category_reason: str | None = None
    management_style: str | None = None
    master_fund: str | None = None
    fx_hedge: str | None = None
    sec_risk_spectrum: int | None = None
    top_holdings: list[dict] | None = None

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def category_label(self) -> str | None:
        return category_label(self.theme, self.region, self.asset_class)


class CategoryOptions(BaseModel):
    regions: list[str]
    themes: list[str]


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


class FundProxyOut(BaseModel):
    kind: str
    ticker: str
    label: str | None
    source: str
    reason: str | None
    valid: bool
    history_days: int | None
    validated_at: datetime | None
    weight_pct: Decimal | None = None
    source_note: str | None = None
    source_as_of: date | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def verified(self) -> bool:
        """False for LLM-suggested fallbacks: not derived from real holdings or an admin."""
        return self.source != "ai"

    @computed_field
    @property
    def override_stale(self) -> bool:
        """Manual override whose source data is over 90 days old (holdings may have drifted)."""
        if self.source != "manual":
            return False
        ref = self.source_as_of or (self.created_at.date() if self.created_at else None)
        return ref is not None and (date.today() - ref).days > 90


class FundProxyUpdate(BaseModel):
    tickers: list[str]   # empty → remove the manual override; sync re-suggests
    source_note: str | None = None     # e.g. "Master fund factsheet (Baillie Gifford)"
    source_as_of: date | None = None   # date of that source data
    weights: dict[str, Decimal] | None = None   # {ticker: holding % of NAV} from that source


class NewsDigestOut(BaseModel):
    ticker: str
    week_start: date
    summary: str | None
    sentiment: str | None
    item_count: int
    sources: list[dict] | None
    status: str
    error: str | None

    model_config = {"from_attributes": True}
