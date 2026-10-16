"""factsheet scan: AIMC category, actual FX hedge %, resumable scan status

Revision ID: 007
Revises: 006
Create Date: 2026-10-16 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("fund_factsheets", "pdf_url", existing_type=sa.String(1000), nullable=True)
    op.alter_column("fund_factsheets", "text_hash", existing_type=sa.String(64), nullable=True)
    op.add_column("fund_factsheets", sa.Column("aimc_category_raw", sa.String(150), nullable=True))
    op.add_column("fund_factsheets", sa.Column("aimc_category", sa.String(150), nullable=True))
    op.add_column("fund_factsheets", sa.Column("fx_hedge_pct", sa.Numeric(6, 2), nullable=True))
    op.add_column("fund_factsheets", sa.Column("scan_status", sa.String(20), nullable=False, server_default="ok"))
    op.add_column("fund_factsheets", sa.Column("scan_error", sa.Text, nullable=True))
    op.create_index("ix_fund_factsheets_aimc_category", "fund_factsheets", ["aimc_category"])


def downgrade() -> None:
    op.drop_index("ix_fund_factsheets_aimc_category", "fund_factsheets")
    for col in ("scan_error", "scan_status", "fx_hedge_pct", "aimc_category", "aimc_category_raw"):
        op.drop_column("fund_factsheets", col)
    op.execute("DELETE FROM fund_factsheets WHERE pdf_url IS NULL OR text_hash IS NULL")
    op.alter_column("fund_factsheets", "text_hash", existing_type=sa.String(64), nullable=False)
    op.alter_column("fund_factsheets", "pdf_url", existing_type=sa.String(1000), nullable=False)
