"""drop price proxies (chunk 5): trend comes from the fund's own NAV and peers

Revision ID: 013
Revises: 012
Create Date: 2026-10-17 00:00:00
"""
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 47 LLM-suggested rows at the time; no manual price overrides existed
    op.execute("DELETE FROM fund_proxies WHERE kind = 'price'")


def downgrade() -> None:
    pass    # deleted LLM suggestions are not restored
