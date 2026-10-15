"""market context: fund proxies, factsheets, news items, weekly news digests

Revision ID: 006
Revises: 005
Create Date: 2026-10-16 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_proxies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("sec_proj_id", sa.String(50), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("ticker", sa.String(30), nullable=False),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("valid", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("history_days", sa.Integer, nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("source_note", sa.String(300), nullable=True),
        sa.Column("source_as_of", sa.Date, nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("sec_proj_id", "kind", "ticker", name="uq_fund_proxy"),
    )
    op.create_index("ix_fund_proxies_sec_proj_id", "fund_proxies", ["sec_proj_id"])

    op.create_table(
        "fund_factsheets",
        sa.Column("sec_proj_id", sa.String(50), primary_key=True),
        sa.Column("pdf_url", sa.String(1000), nullable=False),
        sa.Column("as_of", sa.Date, nullable=True),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("holdings", sa.JSON, nullable=True),
        sa.Column("holdings_scope", sa.String(12), nullable=True),
        sa.Column("master_isin", sa.String(12), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "news_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ticker", sa.String(30), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("link", sa.String(1000), nullable=False),
        sa.Column("title_key", sa.String(200), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("ticker", "link", name="uq_news_ticker_link"),
    )
    op.create_index("ix_news_items_ticker", "news_items", ["ticker"])
    op.create_index("ix_news_items_published_at", "news_items", ["published_at"])

    op.create_table(
        "news_digests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ticker", sa.String(30), nullable=False),
        sa.Column("week_start", sa.Date, nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("sentiment", sa.String(10), nullable=True),
        sa.Column("item_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sources", sa.JSON, nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("ticker", "week_start", name="uq_digest_ticker_week"),
    )
    op.create_index("ix_news_digests_ticker", "news_digests", ["ticker"])


def downgrade() -> None:
    op.drop_index("ix_news_digests_ticker", "news_digests")
    op.drop_table("news_digests")
    op.drop_table("fund_factsheets")
    op.drop_index("ix_news_items_published_at", "news_items")
    op.drop_index("ix_news_items_ticker", "news_items")
    op.drop_table("news_items")
    op.drop_index("ix_fund_proxies_sec_proj_id", "fund_proxies")
    op.drop_table("fund_proxies")
