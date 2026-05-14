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


# ── throttler singleton behavior ──────────────────────────────────────────────

class TestThrottlerSingleton:
    """H6 regression — the throttler must be a process-wide singleton per API key.
    Otherwise the rate limit isn't actually enforced across separate function calls."""

    def test_client_for_returns_same_instance_for_same_key(self):
        from app.services.sec_api import _client_for, _clients
        _clients.clear()
        a = _client_for("key-A")
        b = _client_for("key-A")
        assert a is b

    def test_client_for_returns_distinct_instances_per_key(self):
        from app.services.sec_api import _client_for, _clients
        _clients.clear()
        a = _client_for("key-A")
        b = _client_for("key-B")
        assert a is not b

    @pytest.mark.asyncio
    async def test_two_sequential_calls_share_throttler_state(self):
        """Two back-to-back calls via public sec_api functions must hit the same
        client. Without this, _last_call resets every call and rate-limit is a no-op."""
        from app.services import sec_api
        from app.services.sec_api import _clients
        _clients.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            await sec_api.list_amcs("test-key")
            client_after_first = _clients["test-key"]
            await sec_api.list_amcs("test-key")
            client_after_second = _clients["test-key"]
        assert client_after_first is client_after_second
        # Confirms _last_call > 0 — the throttler is actually tracking timing
        assert client_after_second._last_call > 0


# ── stale sync_jobs cleanup ───────────────────────────────────────────────────

class TestCleanupStaleRunningJobs:
    """H7 regression — sync_jobs left in 'running' status after a crash must be
    marked 'error' on next app startup. Without this, the /sync/jobs page shows
    phantom in-progress rows forever."""

    @pytest.mark.asyncio
    async def test_marks_running_jobs_as_error(self, db):
        from app.services.sync_service import cleanup_stale_running_jobs
        db.add_all([
            SyncJob(
                id=uuid.uuid4(), type="nav_sync",
                started_at=datetime.now(timezone.utc), status="running",
            ),
            SyncJob(
                id=uuid.uuid4(), type="dividend_sync",
                started_at=datetime.now(timezone.utc), status="running",
            ),
            SyncJob(
                id=uuid.uuid4(), type="nav_sync",
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                status="success",
            ),
        ])
        await db.commit()

        cleaned = await cleanup_stale_running_jobs(db)
        assert cleaned == 2

        rows = (await db.execute(select(SyncJob))).scalars().all()
        statuses = sorted(r.status for r in rows)
        assert statuses == ["error", "error", "success"]
        # The two formerly-running rows now have an error_message explaining why
        error_msgs = [r.error_message for r in rows if r.status == "error"]
        assert all("terminated" in (m or "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_no_running_jobs_returns_zero(self, db):
        from app.services.sync_service import cleanup_stale_running_jobs
        cleaned = await cleanup_stale_running_jobs(db)
        assert cleaned == 0


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


# ── M15: auto-create DIVIDEND transactions on dividend sync ───────────────────

class TestAutoCreateDividendTransactions:
    """Verifies the M15 behavior: sync_dividends auto-creates per-portfolio
    DIVIDEND transactions for every holder of a fund on its ex_date, applies
    10% WHT uniformly, and dedupes against manual entries."""

    async def _stub_get_dividends(self, monkeypatch_target, dividend_value):
        """Helper: monkeypatch sec_api.get_dividends to return one fixed dividend."""
        ex_date = date(2025, 6, 15)
        async def _fake(_key, _proj):
            return [{
                "book_close_date": ex_date.isoformat(),
                "dividend_date": ex_date.isoformat(),
                "dividend_value": str(dividend_value),
            }]
        return _fake, ex_date

    @pytest.mark.asyncio
    async def test_auto_creates_dividend_for_each_scheme_with_10pct_wht(self, db):
        """A user holds 500 NORMAL + 200 SSF units of FUND_X at ex_date with
        dpu=2. Auto-creation produces TWO rows: amount 1000 (WHT 100) and
        amount 400 (WHT 40), per-scheme."""
        from app.models.portfolio import Portfolio
        from app.models.transaction import Transaction
        from app.models.user import User

        user = User(id=uuid.uuid4(), email="div@x", password_hash="x", role="user")
        p = Portfolio(id=uuid.uuid4(), user_id=user.id, name="P")
        fund = await _seed_fund(db, "FUND_X", proj_id="PROJX")
        db.add_all([user, p])
        # NORMAL position: BUY 500 units before ex_date
        db.add(Transaction(
            id=uuid.uuid4(), portfolio_id=p.id, date=date(2024, 1, 1),
            type="BUY", fund_code="FUND_X", units=Decimal("500"),
            nav=Decimal("10"), amount=Decimal("5000"),
            fee=Decimal("0"), tax_withheld=Decimal("0"), tax_scheme="NORMAL",
        ))
        # SSF position: BUY 200 units before ex_date
        db.add(Transaction(
            id=uuid.uuid4(), portfolio_id=p.id, date=date(2024, 6, 1),
            type="BUY", fund_code="FUND_X", units=Decimal("200"),
            nav=Decimal("10"), amount=Decimal("2000"),
            fee=Decimal("0"), tax_withheld=Decimal("0"), tax_scheme="SSF",
        ))
        await db.commit()

        fake, ex_date = await self._stub_get_dividends("sec_api.get_dividends", Decimal("2"))
        with patch("app.services.sync_service.sec_api.get_dividends", new=fake):
            with patch("app.services.sync_service.settings") as mock_settings:
                mock_settings.SEC_API_KEY = "fake-key"
                result = await sync_service.sync_dividends(db, proj_ids={"PROJX"})

        assert result["auto_dividends_created"] == 2
        assert result["auto_dividends_skipped"] == 0

        # Verify both rows
        rows = await db.execute(
            select(Transaction).where(
                Transaction.portfolio_id == p.id,
                Transaction.type == "DIVIDEND",
                Transaction.date == ex_date,
            )
        )
        divs = {(r.tax_scheme, r.amount, r.tax_withheld) for r in rows.scalars().all()}
        assert (
            "NORMAL", Decimal("1000.00"), Decimal("100.00"),
        ) in divs
        assert (
            "SSF", Decimal("400.00"), Decimal("40.00"),
        ) in divs

    @pytest.mark.asyncio
    async def test_skips_when_user_already_entered_dividend(self, db):
        """If the user manually entered a DIVIDEND for the same (portfolio,
        fund, ex_date, scheme), auto-creation skips that row."""
        from app.models.portfolio import Portfolio
        from app.models.transaction import Transaction
        from app.models.user import User

        user = User(id=uuid.uuid4(), email="d2@x", password_hash="x", role="user")
        p = Portfolio(id=uuid.uuid4(), user_id=user.id, name="P")
        fund = await _seed_fund(db, "FUND_X", proj_id="PROJX")
        db.add_all([user, p])
        db.add(Transaction(
            id=uuid.uuid4(), portfolio_id=p.id, date=date(2024, 1, 1),
            type="BUY", fund_code="FUND_X", units=Decimal("100"),
            nav=Decimal("10"), amount=Decimal("1000"),
            fee=Decimal("0"), tax_withheld=Decimal("0"), tax_scheme="NORMAL",
        ))
        # Pre-existing manual entry — different amount
        ex_date = date(2025, 6, 15)
        db.add(Transaction(
            id=uuid.uuid4(), portfolio_id=p.id, date=ex_date,
            type="DIVIDEND", fund_code="FUND_X",
            amount=Decimal("999"), fee=Decimal("0"), tax_withheld=Decimal("99"),
            tax_scheme="NORMAL", note="Manual entry",
        ))
        await db.commit()

        fake, _ = await self._stub_get_dividends("sec_api.get_dividends", Decimal("2"))
        with patch("app.services.sync_service.sec_api.get_dividends", new=fake):
            with patch("app.services.sync_service.settings") as mock_settings:
                mock_settings.SEC_API_KEY = "fake-key"
                result = await sync_service.sync_dividends(db, proj_ids={"PROJX"})

        assert result["auto_dividends_created"] == 0
        assert result["auto_dividends_skipped"] == 1
        # The original manual row must still be there, unchanged
        rows = await db.execute(
            select(Transaction).where(
                Transaction.portfolio_id == p.id, Transaction.type == "DIVIDEND",
            )
        )
        all_divs = rows.scalars().all()
        assert len(all_divs) == 1
        assert all_divs[0].amount == Decimal("999")

    @pytest.mark.asyncio
    async def test_skips_when_portfolio_held_zero_units_at_ex_date(self, db):
        """A user who bought then fully sold before ex_date holds zero units;
        no auto-dividend should be created."""
        from app.models.portfolio import Portfolio
        from app.models.transaction import Transaction
        from app.models.user import User

        user = User(id=uuid.uuid4(), email="d3@x", password_hash="x", role="user")
        p = Portfolio(id=uuid.uuid4(), user_id=user.id, name="P")
        fund = await _seed_fund(db, "FUND_X", proj_id="PROJX")
        db.add_all([user, p])
        ex_date = date(2025, 6, 15)
        # BUY then SELL all, both before ex_date — net position = 0
        db.add(Transaction(
            id=uuid.uuid4(), portfolio_id=p.id, date=date(2024, 1, 1),
            type="BUY", fund_code="FUND_X", units=Decimal("100"),
            nav=Decimal("10"), amount=Decimal("1000"),
            fee=Decimal("0"), tax_withheld=Decimal("0"), tax_scheme="NORMAL",
        ))
        db.add(Transaction(
            id=uuid.uuid4(), portfolio_id=p.id, date=date(2024, 6, 1),
            type="SELL", fund_code="FUND_X", units=Decimal("100"),
            nav=Decimal("12"), amount=Decimal("1200"),
            fee=Decimal("0"), tax_withheld=Decimal("0"), tax_scheme="NORMAL",
        ))
        await db.commit()

        fake, _ = await self._stub_get_dividends("sec_api.get_dividends", Decimal("2"))
        with patch("app.services.sync_service.sec_api.get_dividends", new=fake):
            with patch("app.services.sync_service.settings") as mock_settings:
                mock_settings.SEC_API_KEY = "fake-key"
                result = await sync_service.sync_dividends(db, proj_ids={"PROJX"})

        # Zero net units at ex_date → no auto-creation
        assert result["auto_dividends_created"] == 0

    @pytest.mark.asyncio
    async def test_idempotent_on_rerun(self, db):
        """Running sync_dividends a second time produces zero new auto-rows."""
        from app.models.portfolio import Portfolio
        from app.models.transaction import Transaction
        from app.models.user import User

        user = User(id=uuid.uuid4(), email="d4@x", password_hash="x", role="user")
        p = Portfolio(id=uuid.uuid4(), user_id=user.id, name="P")
        fund = await _seed_fund(db, "FUND_X", proj_id="PROJX")
        db.add_all([user, p])
        db.add(Transaction(
            id=uuid.uuid4(), portfolio_id=p.id, date=date(2024, 1, 1),
            type="BUY", fund_code="FUND_X", units=Decimal("100"),
            nav=Decimal("10"), amount=Decimal("1000"),
            fee=Decimal("0"), tax_withheld=Decimal("0"), tax_scheme="NORMAL",
        ))
        await db.commit()

        fake, _ = await self._stub_get_dividends("sec_api.get_dividends", Decimal("2"))
        with patch("app.services.sync_service.sec_api.get_dividends", new=fake):
            with patch("app.services.sync_service.settings") as mock_settings:
                mock_settings.SEC_API_KEY = "fake-key"
                first = await sync_service.sync_dividends(db, proj_ids={"PROJX"})
                second = await sync_service.sync_dividends(db, proj_ids={"PROJX"})

        assert first["auto_dividends_created"] == 1
        assert second["auto_dividends_created"] == 0
        assert second["auto_dividends_skipped"] == 1
