"""widen fund_code columns from 20 to 50 chars

Revision ID: 003
Revises: 002
Create Date: 2026-05-03 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

TABLES_COLS = [
    ("funds", "fund_code"),
    ("nav_history", "fund_code"),
    ("dividends", "fund_code"),
    ("tax_lots", "fund_code"),
    ("transactions", "fund_code"),
    ("transactions", "target_fund_code"),
]


def upgrade() -> None:
    for table, col in TABLES_COLS:
        op.alter_column(table, col, type_=sa.String(50), existing_type=sa.String(20))


def downgrade() -> None:
    for table, col in TABLES_COLS:
        op.alter_column(table, col, type_=sa.String(20), existing_type=sa.String(50))
