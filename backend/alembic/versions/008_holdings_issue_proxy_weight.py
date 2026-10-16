"""factsheet holdings_issue; proxy holding weight

Revision ID: 008
Revises: 007
Create Date: 2026-10-16 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fund_factsheets", sa.Column("holdings_issue", sa.String(20), nullable=True))
    op.add_column("fund_proxies", sa.Column("weight_pct", sa.Numeric(6, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("fund_proxies", "weight_pct")
    op.drop_column("fund_factsheets", "holdings_issue")
