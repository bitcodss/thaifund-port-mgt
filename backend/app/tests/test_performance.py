"""
Fund performance / risk metrics tests — covers PR 5 finding M7 (binary-search
NAV lookback) and the surrounding query logic.
"""
from datetime import date
from decimal import Decimal

from app.services.performance_service import _nav_on_or_before


class TestNavOnOrBefore:
    def test_returns_none_on_empty(self):
        assert _nav_on_or_before([], date(2024, 1, 1)) is None

    def test_exact_match(self):
        pairs = [(date(2024, 1, 1), Decimal("10")), (date(2024, 1, 5), Decimal("12"))]
        assert _nav_on_or_before(pairs, date(2024, 1, 5)) == Decimal("12")

    def test_picks_most_recent_before_anchor(self):
        pairs = [
            (date(2024, 1, 1), Decimal("10")),
            (date(2024, 1, 3), Decimal("11")),
            (date(2024, 1, 5), Decimal("12")),
        ]
        # Anchor is Jan 4 → should pick Jan 3 (most recent <= anchor)
        assert _nav_on_or_before(pairs, date(2024, 1, 4)) == Decimal("11")

    def test_anchor_before_all_returns_none(self):
        pairs = [(date(2024, 1, 5), Decimal("12"))]
        assert _nav_on_or_before(pairs, date(2024, 1, 1)) is None

    def test_handles_gaps_longer_than_10_days(self):
        """M7 regression — the old impl only looked back 10 days. With a
        20-day gap (e.g., a sync gap or market closure), it returned None
        even though earlier NAVs existed."""
        pairs = [(date(2024, 1, 1), Decimal("10")), (date(2024, 2, 15), Decimal("12"))]
        # Anchor Feb 10 → 40 days after Jan 1, no NAV between → should
        # still find Jan 1
        assert _nav_on_or_before(pairs, date(2024, 2, 10)) == Decimal("10")

    def test_handles_decade_old_anchor(self):
        """Stress: anchor 10 years after the latest NAV still returns the
        latest available value."""
        pairs = [(date(2014, 1, 1), Decimal("10"))]
        assert _nav_on_or_before(pairs, date(2024, 1, 1)) == Decimal("10")


# ── get_fund_performance: timeframes vs. fund entry date ──────────────────────

import pytest
import pytest_asyncio
from datetime import timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.fund import NavHistory
from app.services.performance_service import get_fund_performance

LATEST = date(2026, 10, 14)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        # Daily NAV for ~2 years, rising 0.01/day → every anchor has a value
        start = LATEST - timedelta(days=730)
        for i in range(731):
            session.add(NavHistory(fund_code="FUND", trade_date=start + timedelta(days=i),
                                   nav=Decimal("10") + Decimal("0.01") * i))
        await session.commit()
        yield session
    await engine.dispose()


class TestTimeframesVsEntryDate:

    @pytest.mark.asyncio
    async def test_all_timeframes_present_without_since_date(self, db):
        p = await get_fund_performance("FUND", db)
        for f in ("returns_7d", "returns_30d", "returns_3m", "returns_6m",
                  "returns_1y", "returns_ytd", "returns_max"):
            assert getattr(p, f) is not None, f

    @pytest.mark.asyncio
    async def test_3m_uses_91_day_anchor(self, db):
        p = await get_fund_performance("FUND", db)
        latest = Decimal("10") + Decimal("0.01") * 730
        anchor = Decimal("10") + Decimal("0.01") * (730 - 91)
        assert p.returns_3m == ((latest - anchor) / anchor).quantize(Decimal("0.00000001"))

    @pytest.mark.asyncio
    async def test_windows_starting_before_entry_are_none(self, db):
        entry = LATEST - timedelta(days=60)   # held ~2 months
        p = await get_fund_performance("FUND", db, since_date=entry)
        assert p.returns_7d is not None
        assert p.returns_30d is not None
        assert p.returns_3m is None
        assert p.returns_6m is None
        assert p.returns_1y is None
        assert p.returns_ytd is None          # YTD anchor 2025-12-31 is before entry
        assert p.returns_max is not None

    @pytest.mark.asyncio
    async def test_window_starting_on_entry_day_is_kept(self, db):
        entry = LATEST - timedelta(days=30)
        p = await get_fund_performance("FUND", db, since_date=entry)
        assert p.returns_30d is not None
        assert p.returns_3m is None
