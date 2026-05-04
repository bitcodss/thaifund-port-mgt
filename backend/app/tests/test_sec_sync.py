"""
Phase 2 sync tests — mocked SEC API.
All tests use SQLite in-memory via aiosqlite; no real network calls.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.models.fund import Fund, NavHistory, Dividend
from app.models.tax_lot import SyncJob
from app.database import Base
from app.services import sync_service
from app.services.sec_api import SecApiUnauthorizedError, SecApiError


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed_fund(db: AsyncSession, fund_code: str, proj_id: str | None = "PROJ001") -> Fund:
    fund = Fund(fund_code=fund_code, sec_proj_id=proj_id)
    db.add(fund)
    await db.flush()
    return fund


# ── sec_api module unit tests (no DB) ─────────────────────────────────────────

class TestSecApiClient:
    """Tests for the _ThrottledClient — mock httpx responses."""

    @pytest.mark.asyncio
    async def test_get_returns_json_on_200(self):
        from app.services.sec_api import _ThrottledClient
        client = _ThrottledClient("test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": "ok"}

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await client.get("https://example.com/test")

        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_get_returns_none_on_204(self):
        from app.services.sec_api import _ThrottledClient
        client = _ThrottledClient("test-key")
        mock_resp = AsyncMock()
        mock_resp.status_code = 204

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await client.get("https://example.com/test")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_none_on_404(self):
        from app.services.sec_api import _ThrottledClient
        client = _ThrottledClient("test-key")
        mock_resp = AsyncMock()
        mock_resp.status_code = 404

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await client.get("https://example.com/test")

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_unauthorized_on_401(self):
        from app.services.sec_api import _ThrottledClient
        client = _ThrottledClient("bad-key")
        mock_resp = AsyncMock()
        mock_resp.status_code = 401

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(SecApiUnauthorizedError):
                await client.get("https://example.com/test")

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit_then_succeeds(self):
        """First call returns 429, second returns 200."""
        from app.services.sec_api import _ThrottledClient
        client = _ThrottledClient("key")

        rate_limit_resp = MagicMock()
        rate_limit_resp.status_code = 429
        rate_limit_resp.headers = {"Retry-After": "0"}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"result": "ok"}

        with patch("httpx.AsyncClient") as mock_cls, patch("asyncio.sleep", new_callable=AsyncMock):
            mock_cls.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=[rate_limit_resp, ok_resp]
            )
            result = await client.get("https://example.com/test")

        assert result == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        from app.services.sec_api import _ThrottledClient, SecApiError
        client = _ThrottledClient("key")

        error_resp = AsyncMock()
        error_resp.status_code = 500
        error_resp.headers = {}

        with patch("httpx.AsyncClient") as mock_cls, patch("asyncio.sleep", new_callable=AsyncMock):
            mock_cls.return_value.__aenter__.return_value.get = AsyncMock(return_value=error_resp)
            with pytest.raises(SecApiError):
                await client.get("https://example.com/test")


# ── sync_fund_metadata ─────────────────────────────────────────────────────────

class TestSyncFundMetadata:

    @pytest.mark.asyncio
    async def test_creates_new_funds(self, db):
        amcs = [{"unique_id": "amc-001", "name_en": "SCB Asset Management"}]
        funds = [
            {"proj_id": "P001", "proj_abbr_name": "SCBSET", "proj_name_th": "SCB Set", "proj_name_en": "SCB Set", "fund_status": "RG"},
            {"proj_id": "P002", "proj_abbr_name": "SCBBIG", "proj_name_th": "SCB Big", "proj_name_en": "SCB Big", "fund_status": "RG"},
        ]

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.list_amcs", new_callable=AsyncMock, return_value=amcs),
            patch("app.services.sec_api.list_amc_funds", new_callable=AsyncMock, return_value=funds),
        ):
            mock_settings.factsheet_key = "fake-key"
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_fund_metadata(db)

        assert result["created"] == 2
        assert result["updated"] == 0

        row = await db.get(Fund, "SCBSET")
        assert row is not None
        assert row.sec_proj_id == "P001"
        assert row.amc == "SCB Asset Management"
        assert row.amc_unique_id == "amc-001"
        assert row.fund_status == "RG"

    @pytest.mark.asyncio
    async def test_updates_existing_fund(self, db):
        await _seed_fund(db, "SCBSET", proj_id=None)
        await db.commit()

        amcs = [{"unique_id": "amc-001", "name_en": "SCB AM"}]
        funds = [{"proj_id": "P999", "proj_abbr_name": "SCBSET", "proj_name_th": "x", "proj_name_en": "x", "fund_status": "RG"}]

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.list_amcs", new_callable=AsyncMock, return_value=amcs),
            patch("app.services.sec_api.list_amc_funds", new_callable=AsyncMock, return_value=funds),
        ):
            mock_settings.factsheet_key = "fake-key"
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_fund_metadata(db)

        assert result["updated"] == 1
        assert result["created"] == 0
        row = await db.get(Fund, "SCBSET")
        assert row.sec_proj_id == "P999"

    @pytest.mark.asyncio
    async def test_skips_funds_without_abbr(self, db):
        amcs = [{"unique_id": "amc-001", "name_en": "SCB AM"}]
        funds = [{"proj_id": "P001", "proj_abbr_name": None}]

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.list_amcs", new_callable=AsyncMock, return_value=amcs),
            patch("app.services.sec_api.list_amc_funds", new_callable=AsyncMock, return_value=funds),
        ):
            mock_settings.factsheet_key = "fake-key"
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_fund_metadata(db)

        assert result["created"] == 0

    @pytest.mark.asyncio
    async def test_raises_unauthorized_when_no_factsheet_key(self, db):
        with patch("app.services.sync_service.settings") as mock_settings:
            mock_settings.factsheet_key = None
            result = await sync_service.sync_fund_metadata(db)

        assert "SEC_FACTSHEET_KEY not configured" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_propagates_unauthorized_error(self, db):
        amcs = [{"unique_id": "amc-001", "name_en": "SCB AM"}]

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.list_amcs", new_callable=AsyncMock, return_value=amcs),
            patch("app.services.sec_api.list_amc_funds", new_callable=AsyncMock,
                  side_effect=SecApiUnauthorizedError("not subscribed")),
        ):
            mock_settings.factsheet_key = "fake-key"
            mock_settings.SEC_API_KEY = "fake-key"
            with pytest.raises(SecApiUnauthorizedError):
                await sync_service.sync_fund_metadata(db)

    @pytest.mark.asyncio
    async def test_continues_on_amc_api_error(self, db):
        amcs = [
            {"unique_id": "amc-fail", "name_en": "Bad AMC"},
            {"unique_id": "amc-ok", "name_en": "Good AMC"},
        ]
        good_funds = [{"proj_id": "P001", "proj_abbr_name": "GOODFUND", "proj_name_th": "x", "proj_name_en": "x", "fund_status": "RG"}]

        async def fake_list_amc_funds(key, amc_id):
            if amc_id == "amc-fail":
                raise SecApiError("timeout")
            return good_funds

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.list_amcs", new_callable=AsyncMock, return_value=amcs),
            patch("app.services.sec_api.list_amc_funds", side_effect=fake_list_amc_funds),
        ):
            mock_settings.factsheet_key = "fake-key"
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_fund_metadata(db)

        assert result["created"] == 1
        assert len(result["errors"]) == 1

    @pytest.mark.asyncio
    async def test_writes_sync_job_row(self, db):
        amcs = [{"unique_id": "amc-001", "name_en": "SCB AM"}]
        funds = [{"proj_id": "P001", "proj_abbr_name": "SCBSET", "proj_name_th": "x", "proj_name_en": "x", "fund_status": "RG"}]

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.list_amcs", new_callable=AsyncMock, return_value=amcs),
            patch("app.services.sec_api.list_amc_funds", new_callable=AsyncMock, return_value=funds),
        ):
            mock_settings.factsheet_key = "fake-key"
            mock_settings.SEC_API_KEY = "fake-key"
            await sync_service.sync_fund_metadata(db)

        result = await db.execute(select(SyncJob).where(SyncJob.type == "fund_metadata"))
        job = result.scalar_one()
        assert job.status == "success"
        assert job.completed_at is not None


# ── sync_nav_for_date ──────────────────────────────────────────────────────────

class TestSyncNavForDate:

    @pytest.mark.asyncio
    async def test_upserts_nav_history(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        await db.commit()

        nav_payload = {"last_val": "12.3456", "previous_val": "12.1000"}
        nav_date = date(2026, 1, 15)

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_daily_nav", new_callable=AsyncMock, return_value=nav_payload),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_nav_for_date(db, nav_date)

        assert result["synced"] == 1
        row = await db.get(NavHistory, ("SCBSET", nav_date))
        assert row is not None
        assert row.nav == Decimal("12.3456")
        assert row.change_pct is not None

    @pytest.mark.asyncio
    async def test_updates_existing_nav(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        nav_date = date(2026, 1, 15)
        db.add(NavHistory(fund_code="SCBSET", trade_date=nav_date, nav=Decimal("12.0000"), change_pct=None))
        await db.commit()

        nav_payload = {"last_val": "12.5000", "previous_val": "12.0000"}

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_daily_nav", new_callable=AsyncMock, return_value=nav_payload),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            await sync_service.sync_nav_for_date(db, nav_date)

        row = await db.get(NavHistory, ("SCBSET", nav_date))
        assert row.nav == Decimal("12.5000")

    @pytest.mark.asyncio
    async def test_skips_fund_when_nav_is_none(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        await db.commit()

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_daily_nav", new_callable=AsyncMock, return_value=None),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_nav_for_date(db, date(2026, 1, 11))  # weekend

        assert result["synced"] == 0
        assert result["skipped"] == 1

    @pytest.mark.asyncio
    async def test_skips_fund_on_api_error(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        await db.commit()

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_daily_nav", new_callable=AsyncMock, side_effect=SecApiError("timeout")),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_nav_for_date(db, date(2026, 1, 15))

        assert result["synced"] == 0
        assert result["skipped"] == 1
        assert len(result["errors"]) == 1

    @pytest.mark.asyncio
    async def test_computes_change_pct_correctly(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        await db.commit()

        # 12.5 → 12.5 * (1 + change) = new, change = (12.5 - 10.0) / 10.0 = 25%
        nav_payload = {"last_val": "12.5000", "previous_val": "10.0000"}

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_daily_nav", new_callable=AsyncMock, return_value=nav_payload),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            await sync_service.sync_nav_for_date(db, date(2026, 1, 15))

        row = await db.get(NavHistory, ("SCBSET", date(2026, 1, 15)))
        assert row.change_pct == Decimal("25.00000000")

    @pytest.mark.asyncio
    async def test_no_funds_returns_early(self, db):
        with patch("app.services.sync_service.settings") as mock_settings:
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_nav_for_date(db, date(2026, 1, 15))

        assert result["synced"] == 0
        assert "No funds with sec_proj_id" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_updates_fund_last_nav_date(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        await db.commit()

        nav_date = date(2026, 1, 15)
        nav_payload = {"last_val": "12.0000", "previous_val": "11.9000"}

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_daily_nav", new_callable=AsyncMock, return_value=nav_payload),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            await sync_service.sync_nav_for_date(db, nav_date)

        fund = await db.get(Fund, "SCBSET")
        assert fund.last_nav_date == nav_date


# ── sync_dividends ─────────────────────────────────────────────────────────────

class TestSyncDividends:

    @pytest.mark.asyncio
    async def test_inserts_new_dividend(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        await db.commit()

        div_data = [{"book_close_date": "2026-01-10", "dividend_date": "2026-01-15", "dividend_value": "0.25000000"}]

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_dividends", new_callable=AsyncMock, return_value=div_data),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_dividends(db)

        assert result["synced"] == 1
        rows = await db.execute(select(Dividend).where(Dividend.fund_code == "SCBSET"))
        div = rows.scalar_one()
        assert div.ex_date == date(2026, 1, 10)
        assert div.dividend_per_unit == Decimal("0.25000000")
        assert div.source == "sec_api"

    @pytest.mark.asyncio
    async def test_deduplicates_same_ex_date(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        db.add(Dividend(
            id=uuid.uuid4(),
            fund_code="SCBSET",
            ex_date=date(2026, 1, 10),
            dividend_per_unit=Decimal("0.20000000"),
            source="sec_api",
        ))
        await db.commit()

        div_data = [{"book_close_date": "2026-01-10", "dividend_date": "2026-01-15", "dividend_value": "0.25000000"}]

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_dividends", new_callable=AsyncMock, return_value=div_data),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_dividends(db)

        # synced=0 because update (not insert)
        assert result["synced"] == 0
        rows = await db.execute(select(Dividend).where(Dividend.fund_code == "SCBSET"))
        divs = rows.scalars().all()
        assert len(divs) == 1
        assert divs[0].dividend_per_unit == Decimal("0.25000000")

    @pytest.mark.asyncio
    async def test_skips_row_missing_ex_date(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        await db.commit()

        div_data = [{"dividend_value": "0.25000000"}]  # no ex_date

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_dividends", new_callable=AsyncMock, return_value=div_data),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_dividends(db)

        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_skips_fund_on_api_error(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        await _seed_fund(db, "SCBBIG", "P002")
        await db.commit()

        good_div = [{"book_close_date": "2026-01-10", "dividend_date": "2026-01-15", "dividend_value": "0.10"}]

        async def fake_get_dividends(key, proj_id):
            if proj_id == "P001":
                raise SecApiError("timeout")
            return good_div

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_dividends", side_effect=fake_get_dividends),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_dividends(db)

        assert result["synced"] == 1
        assert result["skipped"] == 1

    @pytest.mark.asyncio
    async def test_handles_alternate_field_names(self, db):
        """API may return ex_date / payment_date / dividend_per_unit instead of book_close_date etc."""
        await _seed_fund(db, "SCBSET", "P001")
        await db.commit()

        div_data = [{"ex_date": "2026-02-01", "payment_date": "2026-02-05", "dividend_per_unit": "0.30"}]

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_dividends", new_callable=AsyncMock, return_value=div_data),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            result = await sync_service.sync_dividends(db)

        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_writes_sync_job_row(self, db):
        await _seed_fund(db, "SCBSET", "P001")
        await db.commit()

        with (
            patch("app.services.sync_service.settings") as mock_settings,
            patch("app.services.sec_api.get_dividends", new_callable=AsyncMock, return_value=[]),
        ):
            mock_settings.SEC_API_KEY = "fake-key"
            await sync_service.sync_dividends(db)

        result = await db.execute(select(SyncJob).where(SyncJob.type == "dividend_sync"))
        job = result.scalar_one()
        assert job.status == "success"
