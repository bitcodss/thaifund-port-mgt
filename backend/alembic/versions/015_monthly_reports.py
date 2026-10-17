"""monthly_reports (Phase 3 report builder)

Revision ID: 015
Revises: 014
Create Date: 2026-10-18 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monthly_reports",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("user_id", sa.Uuid, nullable=False, index=True),
        sa.Column("month", sa.Date, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("facts", sa.JSON),
        sa.Column("content", sa.JSON),
        sa.Column("html", sa.Text),
        sa.Column("model", sa.String(100)),
        sa.Column("errors", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "month", name="uq_monthly_report_user_month"),
    )


def downgrade() -> None:
    op.drop_table("monthly_reports")
