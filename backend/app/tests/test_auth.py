"""
Auth-hardening tests — covers PR 4 findings M9 (rate limit), M10 (current-
password gate), and M11 (CORS config parsing).
"""
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.database import Base
from app.models.user import User
from app.services.auth_service import hash_password, verify_password
from app.services.rate_limit import RateLimiter


# ── M9: rate limiter ──────────────────────────────────────────────────────────

class TestLoginRateLimiter:
    def test_allows_up_to_max_attempts(self):
        rl = RateLimiter(max_attempts=3, window_seconds=60)
        assert rl.check("1.2.3.4") is True
        assert rl.check("1.2.3.4") is True
        assert rl.check("1.2.3.4") is True

    def test_blocks_after_max_attempts(self):
        rl = RateLimiter(max_attempts=3, window_seconds=60)
        for _ in range(3):
            rl.check("1.2.3.4")
        assert rl.check("1.2.3.4") is False

    def test_separate_keys_dont_interfere(self):
        rl = RateLimiter(max_attempts=2, window_seconds=60)
        rl.check("ip-A")
        rl.check("ip-A")
        # ip-A is now at the cap; ip-B should still be unaffected
        assert rl.check("ip-A") is False
        assert rl.check("ip-B") is True

    def test_reset_clears_history(self):
        rl = RateLimiter(max_attempts=2, window_seconds=60)
        rl.check("ip-A")
        rl.check("ip-A")
        assert rl.check("ip-A") is False
        rl.reset("ip-A")
        assert rl.check("ip-A") is True

    def test_window_expiry_allows_new_attempts(self):
        """Older entries beyond the window are dropped from the deque."""
        rl = RateLimiter(max_attempts=2, window_seconds=0.05)
        rl.check("ip-A")
        rl.check("ip-A")
        assert rl.check("ip-A") is False
        import time as _time
        _time.sleep(0.1)
        assert rl.check("ip-A") is True


# ── M10: self-service password change requires current password ────────────────

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


@pytest_asyncio.fixture
async def app_client(db):
    """FastAPI TestClient with the test DB injected. Uses dependency override
    to swap the DB session at request time."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from app.api.deps import get_current_user

    async def _override_db():
        yield db

    async def _override_user():
        # Build (or fetch) a real user row so the route handler can mutate it
        from sqlalchemy import select
        result = await db.execute(select(User).limit(1))
        user = result.scalar_one_or_none()
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


class TestSelfUpdatePasswordGate:
    @pytest.mark.asyncio
    async def test_dob_update_does_not_require_current_password(self, db, app_client):
        # Seed a user
        user = User(
            email="x@example.com",
            password_hash=hash_password("original-pw"),
            role="user",
        )
        db.add(user)
        await db.commit()

        resp = app_client.patch("/api/v1/users/me", json={"date_of_birth": "1990-05-14"})
        assert resp.status_code == 200, resp.text
        await db.refresh(user)
        assert user.date_of_birth is not None
        # Password unchanged
        assert verify_password("original-pw", user.password_hash)

    @pytest.mark.asyncio
    async def test_password_change_without_current_password_is_rejected(self, db, app_client):
        user = User(
            email="y@example.com",
            password_hash=hash_password("original-pw"),
            role="user",
        )
        db.add(user)
        await db.commit()

        resp = app_client.patch("/api/v1/users/me", json={"password": "new-pw"})
        assert resp.status_code == 400
        assert "current password" in resp.text.lower()
        await db.refresh(user)
        # Password hash unchanged
        assert verify_password("original-pw", user.password_hash)

    @pytest.mark.asyncio
    async def test_password_change_with_wrong_current_password_is_rejected(self, db, app_client):
        user = User(
            email="z@example.com",
            password_hash=hash_password("original-pw"),
            role="user",
        )
        db.add(user)
        await db.commit()

        resp = app_client.patch(
            "/api/v1/users/me",
            json={"password": "new-pw", "current_password": "wrong-pw"},
        )
        assert resp.status_code == 400
        await db.refresh(user)
        assert verify_password("original-pw", user.password_hash)

    @pytest.mark.asyncio
    async def test_password_change_with_correct_current_password_succeeds(self, db, app_client):
        user = User(
            email="ok@example.com",
            password_hash=hash_password("original-pw"),
            role="user",
        )
        db.add(user)
        await db.commit()

        resp = app_client.patch(
            "/api/v1/users/me",
            json={"password": "new-stronger-pw", "current_password": "original-pw"},
        )
        assert resp.status_code == 200, resp.text
        await db.refresh(user)
        assert verify_password("new-stronger-pw", user.password_hash)
        assert not verify_password("original-pw", user.password_hash)


# ── M6: DOB change invalidates portfolio analytics cache ──────────────────────

class TestDobChangeInvalidatesCache:
    @pytest.mark.asyncio
    async def test_changing_dob_invalidates_users_portfolio_caches(self, db, app_client):
        import uuid as _uuid
        from app.models.portfolio import Portfolio
        from app.services import portfolio_service as ps

        user = User(
            id=_uuid.uuid4(),
            email="dob@example.com",
            password_hash=hash_password("pw"),
            role="user",
        )
        db.add(user)
        portfolio = Portfolio(id=_uuid.uuid4(), user_id=user.id, name="p1")
        db.add(portfolio)
        await db.commit()

        # Seed the cache as if a prior request had populated it.
        ps._cache_set(f"{portfolio.id}:summary:2026-05-14", "stale-value")
        ps._cache_set(f"{portfolio.id}:tax:2026-05-14", "stale-tax")
        assert ps._cache_get(f"{portfolio.id}:summary:2026-05-14") == "stale-value"

        resp = app_client.patch("/api/v1/users/me", json={"date_of_birth": "1990-05-14"})
        assert resp.status_code == 200, resp.text

        # All cache entries for this portfolio must be gone
        assert ps._cache_get(f"{portfolio.id}:summary:2026-05-14") is None
        assert ps._cache_get(f"{portfolio.id}:tax:2026-05-14") is None

    @pytest.mark.asyncio
    async def test_setting_same_dob_does_not_invalidate(self, db, app_client):
        """Idempotent updates shouldn't blow the cache — flag if dob_changed
        is truly false."""
        import uuid as _uuid
        from app.models.portfolio import Portfolio
        from app.services import portfolio_service as ps

        user = User(
            id=_uuid.uuid4(),
            email="same@example.com",
            password_hash=hash_password("pw"),
            role="user",
            date_of_birth=date(1990, 5, 14),
        )
        db.add(user)
        portfolio = Portfolio(id=_uuid.uuid4(), user_id=user.id, name="p1")
        db.add(portfolio)
        await db.commit()

        ps._cache_set(f"{portfolio.id}:summary:2026-05-14", "keep-me")
        resp = app_client.patch("/api/v1/users/me", json={"date_of_birth": "1990-05-14"})
        assert resp.status_code == 200
        # Same DOB → cache NOT invalidated
        assert ps._cache_get(f"{portfolio.id}:summary:2026-05-14") == "keep-me"


# ── M11: CORS origins parsing ─────────────────────────────────────────────────

class TestCorsOriginParsing:
    def test_default_origin_parsed_as_list(self):
        from app.config import Settings
        s = Settings(
            POSTGRES_PASSWORD="x", SECRET_KEY="x",
            FIRST_ADMIN_EMAIL="x@x", FIRST_ADMIN_PASSWORD="x",
        )
        assert s.cors_origin_list == ["http://localhost:3000"]

    def test_multiple_origins_split_on_comma(self):
        from app.config import Settings
        s = Settings(
            POSTGRES_PASSWORD="x", SECRET_KEY="x",
            FIRST_ADMIN_EMAIL="x@x", FIRST_ADMIN_PASSWORD="x",
            CORS_ORIGINS="http://localhost:3000, https://app.example.com",
        )
        assert s.cors_origin_list == ["http://localhost:3000", "https://app.example.com"]

    def test_empty_strings_filtered(self):
        from app.config import Settings
        s = Settings(
            POSTGRES_PASSWORD="x", SECRET_KEY="x",
            FIRST_ADMIN_EMAIL="x@x", FIRST_ADMIN_PASSWORD="x",
            CORS_ORIGINS=",,,",
        )
        assert s.cors_origin_list == []
