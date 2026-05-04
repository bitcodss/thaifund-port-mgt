import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, JSON, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Fund(Base):
    __tablename__ = "funds"

    fund_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    # SEC internal project ID — needed to call FundDailyInfo API
    sec_proj_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    name_th: Mapped[str | None] = mapped_column(String(500), nullable=True)
    name_en: Mapped[str | None] = mapped_column(String(500), nullable=True)
    amc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amc_unique_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    asset_class: Mapped[str | None] = mapped_column(String(100), nullable=True)
    risk_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    benchmark: Mapped[str | None] = mapped_column(String(200), nullable=True)
    fund_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fund_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_nav_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    raw_factsheet: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class NavHistory(Base):
    __tablename__ = "nav_history"

    fund_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    nav: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)


class Dividend(Base):
    __tablename__ = "dividends"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    fund_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    dividend_per_unit: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
