"""add sec_proj_id and related fields to funds

Revision ID: 002
Revises: 001
Create Date: 2026-01-01 00:01:00
"""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("funds", sa.Column("sec_proj_id", sa.String(50), nullable=True))
    op.add_column("funds", sa.Column("amc_unique_id", sa.String(50), nullable=True))
    op.add_column("funds", sa.Column("fund_status", sa.String(10), nullable=True))
    op.add_column("funds", sa.Column("last_nav_date", sa.Date, nullable=True))
    op.create_index("ix_funds_sec_proj_id", "funds", ["sec_proj_id"])


def downgrade() -> None:
    op.drop_index("ix_funds_sec_proj_id", "funds")
    op.drop_column("funds", "last_nav_date")
    op.drop_column("funds", "fund_status")
    op.drop_column("funds", "amc_unique_id")
    op.drop_column("funds", "sec_proj_id")
