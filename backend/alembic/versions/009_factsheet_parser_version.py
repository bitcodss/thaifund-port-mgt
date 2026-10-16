"""factsheet parser provenance: parser version + parse time for holdings and AIMC category

Revision ID: 009
Revises: 008
Create Date: 2026-10-16 00:00:00

Existing rows came from several unrecorded parser versions (a protected re-parse
keeps older data when the new result is worse), so they are marked "legacy".
"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None

_COLS = [
    ("holdings_parser_version", sa.String(20)),
    ("holdings_parsed_at", sa.DateTime(timezone=True)),
    ("aimc_parser_version", sa.String(20)),
    ("aimc_parsed_at", sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    for name, type_ in _COLS:
        op.add_column("fund_factsheets", sa.Column(name, type_, nullable=True))
    op.execute("""
        UPDATE fund_factsheets
        SET holdings_parser_version = 'legacy', holdings_parsed_at = fetched_at,
            aimc_parser_version = 'legacy', aimc_parsed_at = fetched_at
        WHERE scan_status = 'ok'
    """)
    op.create_index("ix_fund_factsheets_holdings_parser_version", "fund_factsheets", ["holdings_parser_version"])


def downgrade() -> None:
    op.drop_index("ix_fund_factsheets_holdings_parser_version", "fund_factsheets")
    for name, _ in reversed(_COLS):
        op.drop_column("fund_factsheets", name)
