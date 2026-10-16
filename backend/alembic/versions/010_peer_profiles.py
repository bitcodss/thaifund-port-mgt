"""peer profiles: effective AIMC category, peer theme, flags, master key, representative class

Revision ID: 010
Revises: 009
Create Date: 2026-10-16 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "peer_profiles",
        sa.Column("sec_proj_id", sa.String(50), primary_key=True),
        sa.Column("aimc_category", sa.String(150), nullable=True),
        sa.Column("aimc_source", sa.String(10), nullable=True),
        sa.Column("aimc_reason", sa.Text, nullable=True),
        sa.Column("peer_theme", sa.String(60), nullable=True),
        sa.Column("theme_source", sa.String(10), nullable=True),
        sa.Column("theme_reason", sa.Text, nullable=True),
        sa.Column("is_passive", sa.Boolean, nullable=True),
        sa.Column("is_esg", sa.Boolean, nullable=True),
        sa.Column("master_key_kind", sa.String(10), nullable=True),
        sa.Column("master_key", sa.String(200), nullable=True),
        sa.Column("representative_code", sa.String(50), nullable=True),
        sa.Column("hedge_group", sa.String(12), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_peer_profiles_aimc_category", "peer_profiles", ["aimc_category"])
    op.create_index("ix_peer_profiles_peer_theme", "peer_profiles", ["peer_theme"])
    op.create_index("ix_peer_profiles_master_key", "peer_profiles", ["master_key"])


def downgrade() -> None:
    op.drop_table("peer_profiles")
