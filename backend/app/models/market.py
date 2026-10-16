"""
Market context for monthly fund reports: proxy tickers per SEC fund project,
raw news collected from Yahoo RSS, and weekly per-ticker news digests.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, Integer, JSON, Numeric, String, Text, UniqueConstraint, Uuid, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FundProxy(Base):
    """
    A market ticker standing in for a fund. kind="price" tracks what the fund's
    market did (index/ETF); kind="news" is what we collect headlines for (theme
    ETF or the fund's actual stock holdings — index tickers give noisy news).
    source: "holding" (resolved from top holdings), "ai" (LLM suggestion) or
    "manual" (admin; when any manual row exists for a kind, sync leaves that kind alone).
    """
    __tablename__ = "fund_proxies"
    __table_args__ = (UniqueConstraint("sec_proj_id", "kind", "ticker", name="uq_fund_proxy"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sec_proj_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    ticker: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    history_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Manual overrides: where the tickers came from and how current that data is, so a
    # stale override (holdings drift, sync never replaces it) can be flagged.
    # Weight (% of NAV) of the holding this ticker represents — Phase 3 ranks a fund's
    # news by holding weight, not by how many headlines a ticker gets.
    weight_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    source_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewsItem(Base):
    """Raw headline from Yahoo RSS. Pruned after 90 days — digests keep the sources."""
    __tablename__ = "news_items"
    __table_args__ = (UniqueConstraint("ticker", "link", name="uq_news_ticker_link"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str] = mapped_column(String(1000), nullable=False)
    title_key: Mapped[str] = mapped_column(String(200), nullable=False)  # normalised title for dedupe
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewsDigest(Base):
    """Weekly LLM summary of one ticker's news. Kept permanently with its sources,
    so a past monthly report can always be traced back to what it was based on."""
    __tablename__ = "news_digests"
    __table_args__ = (UniqueConstraint("ticker", "week_start", name="uq_digest_ticker_week"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)   # Monday, Asia/Bangkok
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(10), nullable=True)  # positive|neutral|negative
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{title, link, published_at}]
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # ok|no_news|failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FundFactsheet(Base):
    """Latest SEC-format factsheet per project, parsed. holdings_scope: "master" (feeder's
    master-fund top 5), "fund" (the fund's own top 5) or "fund_units" (only fund/ETF units —
    no stock-level holdings available)."""
    __tablename__ = "fund_factsheets"

    sec_proj_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    pdf_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    holdings: Mapped[list | None] = mapped_column(JSON, nullable=True)   # [{name, pct}]
    holdings_scope: Mapped[str | None] = mapped_column(String(12), nullable=True)
    holdings_issue: Mapped[str | None] = mapped_column(String(20), nullable=True)  # see factsheet_service
    master_isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    aimc_category_raw: Mapped[str | None] = mapped_column(String(150), nullable=True)
    aimc_category: Mapped[str | None] = mapped_column(String(150), nullable=True, index=True)  # official name
    fx_hedge_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)  # actual, when printed
    scan_status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")  # ok|no_factsheet|error
    # Provenance: which parser produced the stored holdings / AIMC category, and when. A protected
    # re-parse keeps older data when the new result is worse, so these can differ per field group.
    holdings_parser_version: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    holdings_parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    aimc_parser_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    aimc_parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PeerProfile(Base):
    """
    Per SEC project: what it is compared against. Effective AIMC category (parsed from the
    factsheet, or LLM-inferred when the factsheet prints none), fixed-list peer theme,
    passive / ESG flags, master key (one peer per master), representative retail share
    class and its FX-hedge group. theme_source="manual" is an admin override — never
    replaced by sync.
    """
    __tablename__ = "peer_profiles"

    sec_proj_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    aimc_category: Mapped[str | None] = mapped_column(String(150), nullable=True, index=True)
    aimc_source: Mapped[str | None] = mapped_column(String(10), nullable=True)      # parsed|inherited|inferred|manual
    aimc_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    peer_theme: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    theme_source: Mapped[str | None] = mapped_column(String(10), nullable=True)     # fixed|ai|manual
    theme_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_passive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_esg: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    master_key_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)  # isin|name
    master_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    representative_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    hedge_group: Mapped[str | None] = mapped_column(String(12), nullable=True)      # unhedged|full|dynamic|partial|domestic|unknown
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Theme is classified once per master by two models: agreed → automatic; they differ → "review"
    # (still counted in peer stats, labelled "theme unreviewed"). theme_alt = second model's answer.
    theme_alt: Mapped[str | None] = mapped_column(String(60), nullable=True)
    theme_status: Mapped[str | None] = mapped_column(String(12), nullable=True)   # agreed|review|manual|rule|fixed
    theme_input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classifier_input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
