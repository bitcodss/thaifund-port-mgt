import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TaxLot(Base):
    __tablename__ = "tax_lots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fund_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # NEVER changes after creation — anchors tax holding period
    original_purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    units_remaining: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    cost_basis_remaining: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    tax_scheme: Mapped[str] = mapped_column(String(20), nullable=False)
    source_lot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tax_lots.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    portfolio: Mapped["Portfolio"] = relationship("Portfolio", back_populates="tax_lots")
    consumptions: Mapped[list["LotConsumption"]] = relationship(
        "LotConsumption", back_populates="lot", foreign_keys="LotConsumption.lot_id"
    )


class LotConsumption(Base):
    """Audit trail: every lot mutation is recorded here."""
    __tablename__ = "lot_consumptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tax_lots.id"), nullable=False, index=True
    )
    units_consumed: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    cost_basis_consumed: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)

    transaction: Mapped["Transaction"] = relationship("Transaction", back_populates="lot_consumptions")
    lot: Mapped["TaxLot"] = relationship("TaxLot", back_populates="consumptions", foreign_keys=[lot_id])


class TaxSchemeRule(Base):
    """Holding period rules stored as data so they can be updated without code changes."""
    __tablename__ = "tax_scheme_rules"

    scheme: Mapped[str] = mapped_column(String(20), primary_key=True)
    holding_years: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    age_requirement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_from: Mapped[date] = mapped_column(Date, nullable=False)


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
