"""master-fund currency per peer profile (parsed from the FULL SEC investment policy)

Revision ID: 014
Revises: 013
Create Date: 2026-10-17 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("peer_profiles", sa.Column("currency", sa.String(3), nullable=True))
    op.add_column("peer_profiles", sa.Column("currency_source", sa.String(12), nullable=True))


def downgrade() -> None:
    op.drop_column("peer_profiles", "currency_source")
    op.drop_column("peer_profiles", "currency")
