"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-01-01 00:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        sa.Column("date_of_birth", sa.Date, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "portfolios",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_portfolios_user_id", "portfolios", ["user_id"])

    op.create_table(
        "funds",
        sa.Column("fund_code", sa.String(20), primary_key=True),
        sa.Column("name_th", sa.String(500), nullable=True),
        sa.Column("name_en", sa.String(500), nullable=True),
        sa.Column("amc", sa.String(200), nullable=True),
        sa.Column("asset_class", sa.String(100), nullable=True),
        sa.Column("risk_level", sa.Integer, nullable=True),
        sa.Column("benchmark", sa.String(200), nullable=True),
        sa.Column("fund_type", sa.String(100), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_factsheet", postgresql.JSONB, nullable=True),
    )

    op.create_table(
        "nav_history",
        sa.Column("fund_code", sa.String(20), primary_key=True),
        sa.Column("trade_date", sa.Date, primary_key=True),
        sa.Column("nav", sa.Numeric(20, 8), nullable=False),
        sa.Column("change_pct", sa.Numeric(20, 8), nullable=True),
    )
    # Phase 3 performance queries need this index
    op.create_index("ix_nav_history_fund_date", "nav_history", ["fund_code", "trade_date"])

    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("fund_code", sa.String(20), nullable=True),
        sa.Column("units", sa.Numeric(20, 8), nullable=True),
        sa.Column("nav", sa.Numeric(20, 8), nullable=True),
        sa.Column("amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("fee", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("tax_withheld", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("target_fund_code", sa.String(20), nullable=True),
        sa.Column("pair_id", sa.String(100), nullable=True),
        sa.Column("tax_scheme", sa.String(20), nullable=False, server_default="NORMAL"),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_transactions_portfolio_date", "transactions", ["portfolio_id", "date"])
    op.create_index("ix_transactions_pair_id", "transactions", ["pair_id"])

    op.create_table(
        "tax_lots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fund_code", sa.String(20), nullable=False),
        sa.Column("original_purchase_date", sa.Date, nullable=False),
        sa.Column("units_remaining", sa.Numeric(20, 8), nullable=False),
        sa.Column("cost_basis_remaining", sa.Numeric(20, 8), nullable=False),
        sa.Column("tax_scheme", sa.String(20), nullable=False),
        sa.Column("source_lot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tax_lots.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tax_lots_portfolio_fund", "tax_lots", ["portfolio_id", "fund_code"])

    op.create_table(
        "lot_consumptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tax_lots.id"), nullable=False),
        sa.Column("units_consumed", sa.Numeric(20, 8), nullable=False),
        sa.Column("cost_basis_consumed", sa.Numeric(20, 8), nullable=False),
    )
    op.create_index("ix_lot_consumptions_transaction", "lot_consumptions", ["transaction_id"])
    op.create_index("ix_lot_consumptions_lot", "lot_consumptions", ["lot_id"])

    op.create_table(
        "dividends",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fund_code", sa.String(20), nullable=False),
        sa.Column("ex_date", sa.Date, nullable=False),
        sa.Column("payment_date", sa.Date, nullable=True),
        sa.Column("dividend_per_unit", sa.Numeric(20, 8), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dividends_fund_code", "dividends", ["fund_code"])

    op.create_table(
        "tax_scheme_rules",
        sa.Column("scheme", sa.String(20), primary_key=True),
        sa.Column("holding_years", sa.Numeric(5, 2), nullable=False),
        sa.Column("age_requirement", sa.Integer, nullable=True),
        sa.Column("active_from", sa.Date, nullable=False),
    )

    op.create_table(
        "sync_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("error_message", sa.String(2000), nullable=True),
    )

    # Seed default tax scheme rules
    op.execute("""
        INSERT INTO tax_scheme_rules (scheme, holding_years, age_requirement, active_from) VALUES
        ('NORMAL',        0,  NULL, '2000-01-01'),
        ('RMF',           5,  55,   '2000-01-01'),
        ('SSF',           10, NULL, '2020-01-01'),
        ('THAI_ESG',      5,  NULL, '2023-01-01'),
        ('THAI_ESG_EXTRA',8,  NULL, '2023-01-01'),
        ('LTF',           5,  NULL, '2000-01-01')
    """)


def downgrade() -> None:
    op.drop_table("sync_jobs")
    op.drop_table("tax_scheme_rules")
    op.drop_table("dividends")
    op.drop_table("lot_consumptions")
    op.drop_table("tax_lots")
    op.drop_table("transactions")
    op.drop_table("nav_history")
    op.drop_table("funds")
    op.drop_table("portfolios")
    op.drop_table("users")
