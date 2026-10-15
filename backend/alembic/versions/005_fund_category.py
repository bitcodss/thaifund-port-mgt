"""fund category (region/theme) and SEC classification fields

Revision ID: 005
Revises: 004
Create Date: 2026-10-15 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None

_NEW_COLUMNS = [
    sa.Column("region", sa.String(50), nullable=True),
    sa.Column("theme", sa.String(50), nullable=True),
    sa.Column("category_source", sa.String(10), nullable=True),
    sa.Column("category_reason", sa.Text, nullable=True),
    sa.Column("category_input_hash", sa.String(64), nullable=True),
    sa.Column("categorized_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("management_style", sa.String(10), nullable=True),
    sa.Column("master_fund", sa.String(300), nullable=True),
    sa.Column("fx_hedge", sa.String(200), nullable=True),
    sa.Column("sec_risk_spectrum", sa.Integer, nullable=True),
    sa.Column("top_holdings", sa.JSON, nullable=True),
]


def upgrade() -> None:
    for col in _NEW_COLUMNS:
        op.add_column("funds", col)
    # SEC benchmark descriptions routinely exceed 200 chars
    op.alter_column("funds", "benchmark", type_=sa.String(1000), existing_nullable=True)


def downgrade() -> None:
    op.alter_column("funds", "benchmark", type_=sa.String(200), existing_nullable=True)
    for col in reversed(_NEW_COLUMNS):
        op.drop_column("funds", col.name)
