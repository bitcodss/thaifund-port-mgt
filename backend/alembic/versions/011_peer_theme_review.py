"""peer theme per master by two models: second-model theme, status, theme input hash, classifier inputs

Revision ID: 011
Revises: 010
Create Date: 2026-10-16 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("peer_profiles", sa.Column("theme_alt", sa.String(60), nullable=True))
    op.add_column("peer_profiles", sa.Column("theme_status", sa.String(12), nullable=True))
    op.add_column("peer_profiles", sa.Column("theme_input_hash", sa.String(64), nullable=True))
    op.add_column("peer_profiles", sa.Column("classifier_input", sa.JSON, nullable=True))


def downgrade() -> None:
    for c in ("classifier_input", "theme_input_hash", "theme_status", "theme_alt"):
        op.drop_column("peer_profiles", c)
