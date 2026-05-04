"""Add portfolio AI summary table

Revision ID: 004
Revises: 003
Create Date: 2026-05-03 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portfolio_ai_summaries",
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("portfolio_id"),
    )


def downgrade():
    op.drop_table("portfolio_ai_summaries")
