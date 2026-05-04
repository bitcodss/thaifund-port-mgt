import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TransactionType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    SWITCH_OUT = "SWITCH_OUT"
    SWITCH_IN = "SWITCH_IN"
    DIVIDEND = "DIVIDEND"
    INTEREST = "INTEREST"


class TaxScheme(str, enum.Enum):
    NORMAL = "NORMAL"
    RMF = "RMF"
    SSF = "SSF"
    THAI_ESG = "THAI_ESG"
    THAI_ESG_EXTRA = "THAI_ESG_EXTRA"
    LTF = "LTF"


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    fund_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    units: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    nav: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    tax_withheld: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    target_fund_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pair_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    tax_scheme: Mapped[str] = mapped_column(String(20), nullable=False, default=TaxScheme.NORMAL.value)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    portfolio: Mapped["Portfolio"] = relationship("Portfolio", back_populates="transactions")
    lot_consumptions: Mapped[list["LotConsumption"]] = relationship(
        "LotConsumption", back_populates="transaction", cascade="all, delete-orphan"
    )
