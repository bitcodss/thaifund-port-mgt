"""
Fund categorisation tests — SEC API and the LLM are mocked; SQLite in-memory.
"""
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.fund import Fund
from app.models.tax_lot import SyncJob
from app.services import fund_category_service as fcs
from app.services.fund_category_service import ClassificationError
from app.services.fund_taxonomy import REGIONS, category_label


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


PROFILES = [
    {"proj_id": "P1", "proj_abbr_name": "MEGA10AI", "fund_class_name": "MEGA10AI-A",
     "proj_name_en": "MEGA 10 ARTIFICIAL INTELLIGENCE FUND", "policy_desc": "ตราสารทุน",
     "management_style": "OT", "feederfund_master_fund": None,
     "exchange_rate_protection_policy": "ไม่ป้องกัน", "investment_policy_desc": "<p>US AI&nbsp;stocks</p>"},
    {"proj_id": "P1", "proj_abbr_name": "MEGA10AI", "fund_class_name": "MEGA10AI-SSF",
     "proj_name_en": "MEGA 10 ARTIFICIAL INTELLIGENCE FUND", "policy_desc": "ตราสารทุน",
     "management_style": "OT", "feederfund_master_fund": None,
     "exchange_rate_protection_policy": "ไม่ป้องกัน (SSF)", "investment_policy_desc": "<p>US AI&nbsp;stocks</p>"},
]
BENCH = [{"benchmark": "S&P 500 TR", "group_seq": 1}]
TOP5 = [{"asset_name": "NVIDIA CORP", "asset_ratio": 9.1, "asset_seq": 2},
        {"asset_name": "MICRON TECHNOLOGY INC", "asset_ratio": 9.8, "asset_seq": 1}]
RISK = [{"risk_spectrum": "RS6"}]
LLM_OK = {"region": "US", "theme": "AI", "reason": "US-listed AI companies"}


def _sec_patches(profiles=PROFILES):
    return (
        patch("app.services.sec_api.get_fund_profiles", new_callable=AsyncMock, return_value=profiles),
        patch("app.services.sec_api.get_benchmarks", new_callable=AsyncMock, return_value=BENCH),
        patch("app.services.sec_api.get_top5_holdings", new_callable=AsyncMock, return_value=TOP5),
        patch("app.services.sec_api.get_risk_spectrum", new_callable=AsyncMock, return_value=RISK),
        patch("app.services.fund_category_service.settings"),
    )


async def _run(db, llm, force=False, profiles=PROFILES):
    p1, p2, p3, p4, p5 = _sec_patches(profiles)
    with p1, p2, p3, p4, p5 as mock_settings, \
            patch("app.services.fund_category_service.classify_with_llm", new=llm):
        mock_settings.SEC_API_KEY = "fake-key"
        return await fcs.categorize_funds(db, proj_ids={"P1"}, force=force)


async def _seed(db, *codes):
    for c in codes:
        db.add(Fund(fund_code=c, sec_proj_id="P1"))
    await db.commit()


class TestCategoryLabel:
    def test_theme_prefix(self):
        assert category_label("AI", "US", "Equity") == "AI · US Equity"

    def test_broad_market_has_no_prefix(self):
        assert category_label("Broad Market", "China", "Equity") == "China Equity"

    def test_asset_class_theme_not_repeated(self):
        assert category_label("Fixed Income", "Global", "Fixed Income") == "Global Fixed Income"

    def test_missing_asset_class_or_region(self):
        assert category_label("Healthcare", "Global", None) == "Healthcare · Global"
        assert category_label("AI", None, "Equity") == "AI"
        assert category_label(None, None, None) is None


class TestCategorizeFunds:

    @pytest.mark.asyncio
    async def test_classifies_and_stores_sec_fields_on_every_class(self, db):
        await _seed(db, "MEGA10AI-A", "MEGA10AI-SSF")
        llm = AsyncMock(return_value=LLM_OK)
        result = await _run(db, llm)

        assert result == {"classified": 1, "unchanged": 0, "errors": []}
        llm.assert_awaited_once()                       # one call per project, not per class
        data = llm.await_args.args[0]
        assert data["policy"] == "US AI stocks"          # HTML stripped
        assert data["top_holdings"] == ["MICRON TECHNOLOGY INC", "NVIDIA CORP"]  # by asset_seq

        a = await db.get(Fund, "MEGA10AI-A")
        ssf = await db.get(Fund, "MEGA10AI-SSF")
        for f in (a, ssf):
            assert (f.region, f.theme, f.category_source) == ("US", "AI", "ai")
            assert f.sec_risk_spectrum == 6
            assert f.benchmark == "S&P 500 TR"
            assert f.management_style == "OT"
            assert f.asset_class == "Equity"
            assert f.top_holdings[0] == {"name": "MICRON TECHNOLOGY INC", "pct": 9.8}
        assert a.fx_hedge == "ไม่ป้องกัน"
        assert ssf.fx_hedge == "ไม่ป้องกัน (SSF)"      # matched to its own class row

    @pytest.mark.asyncio
    async def test_skips_llm_when_inputs_unchanged(self, db):
        await _seed(db, "MEGA10AI-SSF")
        await _run(db, AsyncMock(return_value=LLM_OK))
        llm = AsyncMock(return_value={"region": "Global", "theme": "Technology", "reason": "x"})
        result = await _run(db, llm)

        llm.assert_not_awaited()
        assert result["unchanged"] == 1
        assert (await db.get(Fund, "MEGA10AI-SSF")).theme == "AI"

    @pytest.mark.asyncio
    async def test_reclassifies_when_inputs_change_or_forced(self, db):
        await _seed(db, "MEGA10AI-SSF")
        await _run(db, AsyncMock(return_value=LLM_OK))

        changed = [dict(p, investment_policy_desc="Global tech") for p in PROFILES]
        llm = AsyncMock(return_value={"region": "Global", "theme": "Technology", "reason": "x"})
        await _run(db, llm, profiles=changed)
        llm.assert_awaited_once()
        assert (await db.get(Fund, "MEGA10AI-SSF")).theme == "Technology"

        forced = AsyncMock(return_value=LLM_OK)
        await _run(db, forced, force=True, profiles=changed)
        forced.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_manual_override_never_overwritten(self, db):
        db.add(Fund(fund_code="MEGA10AI-SSF", sec_proj_id="P1",
                    region="Global", theme="Technology", category_source="manual"))
        await _seed(db, "MEGA10AI-A")
        llm = AsyncMock(return_value=LLM_OK)
        await _run(db, llm, force=True)

        manual = await db.get(Fund, "MEGA10AI-SSF")
        assert (manual.region, manual.theme, manual.category_source) == ("Global", "Technology", "manual")
        assert manual.sec_risk_spectrum == 6             # verbatim SEC fields still refresh
        assert (await db.get(Fund, "MEGA10AI-A")).theme == "AI"

    @pytest.mark.asyncio
    async def test_llm_failure_keeps_previous_values_and_records_error(self, db):
        await _seed(db, "MEGA10AI-SSF")
        await _run(db, AsyncMock(return_value=LLM_OK))
        failing = AsyncMock(side_effect=ClassificationError("LLM call failed: ConnectError"))
        result = await _run(db, failing, force=True)

        assert result["classified"] == 0
        assert "ConnectError" in result["errors"][0]
        f = await db.get(Fund, "MEGA10AI-SSF")
        assert (f.region, f.theme) == ("US", "AI")
        jobs = (await db.execute(select(SyncJob).order_by(SyncJob.started_at))).scalars().all()
        assert [j.status for j in jobs] == ["success", "error"]


class TestClassifyWithLlm:

    @pytest.mark.asyncio
    async def test_rejects_values_outside_allowed_lists(self):
        resp = AsyncMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"response": '{"region": "Mars", "theme": "AI", "reason": "x"}'}
        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            with pytest.raises(ClassificationError, match="outside the allowed lists"):
                await fcs.classify_with_llm({"fund": "X"})

    @pytest.mark.asyncio
    async def test_sends_schema_and_disables_thinking(self):
        resp = AsyncMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"response": '{"region": "US", "theme": "AI", "reason": "ok"}'}
        with patch("httpx.AsyncClient") as mock_cls:
            post = AsyncMock(return_value=resp)
            mock_cls.return_value.__aenter__.return_value.post = post
            out = await fcs.classify_with_llm({"fund": "X"})

        assert out == {"region": "US", "theme": "AI", "reason": "ok"}
        body = post.await_args.kwargs["json"]
        assert body["think"] is False
        assert body["format"]["properties"]["region"]["enum"] == REGIONS
        assert body["options"]["temperature"] == 0


# ── API override + allocation ─────────────────────────────────────────────────

import uuid
from datetime import date
from decimal import Decimal

from app.models.fund import NavHistory
from app.models.portfolio import Portfolio
from app.models.tax_lot import TaxLot
from app.models.user import User


@pytest_asyncio.fixture
async def admin_client(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from app.api.deps import get_current_user

    admin = User(id=uuid.uuid4(), email="admin@x", password_hash="x", role="admin")
    db.add(admin)
    await db.commit()

    async def _override_db():
        yield db

    async def _override_user():
        return admin

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestCategoryOverrideApi:

    @pytest.mark.asyncio
    async def test_patch_region_theme_marks_manual(self, db, admin_client):
        db.add(Fund(fund_code="MEGA10AI-SSF", sec_proj_id="P1", asset_class="Equity",
                    region="US", theme="AI", category_source="ai", category_input_hash="h"))
        await db.commit()

        r = admin_client.patch("/api/v1/funds/MEGA10AI-SSF", json={"region": "Global", "theme": "Technology"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["category_source"] == "manual"
        assert body["category_label"] == "Technology · Global Equity"

    @pytest.mark.asyncio
    async def test_patch_rejects_unknown_theme(self, db, admin_client):
        db.add(Fund(fund_code="X", sec_proj_id="P1"))
        await db.commit()
        r = admin_client.patch("/api/v1/funds/X", json={"theme": "Crypto Moon"})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_patch_category_source_ai_hands_back_to_classifier(self, db, admin_client):
        db.add(Fund(fund_code="X", sec_proj_id="P1", region="Global", theme="Technology",
                    category_source="manual", category_input_hash="h"))
        await db.commit()
        r = admin_client.patch("/api/v1/funds/X", json={"category_source": "ai"})
        assert r.status_code == 200
        f = await db.get(Fund, "X")
        await db.refresh(f)
        assert f.category_source == "ai"
        assert f.category_input_hash is None             # forces re-classification next run

    @pytest.mark.asyncio
    async def test_category_options_endpoint(self, admin_client):
        r = admin_client.get("/api/v1/funds/category-options")
        assert r.status_code == 200
        assert "AI" in r.json()["themes"] and "US" in r.json()["regions"]


class TestRegionThemeAllocation:

    @pytest.mark.asyncio
    async def test_allocation_groups_by_region_and_theme(self, db):
        from app.services import portfolio_service as ps
        ps.clear_all_cache()
        user = User(id=uuid.uuid4(), email="u@x", password_hash="x", role="user")
        p = Portfolio(id=uuid.uuid4(), user_id=user.id, name="P")
        db.add_all([user, p,
                    Fund(fund_code="A", asset_class="Equity", region="US", theme="AI"),
                    Fund(fund_code="B", asset_class="Equity", region="US", theme="Broad Market"),
                    Fund(fund_code="C", asset_class="Equity")])
        for code, cost in (("A", "100"), ("B", "300"), ("C", "100")):
            db.add(TaxLot(id=uuid.uuid4(), portfolio_id=p.id, fund_code=code,
                          original_purchase_date=date(2025, 1, 1), units_remaining=Decimal(cost),
                          cost_basis_remaining=Decimal(cost), tax_scheme="NORMAL"))
            db.add(NavHistory(fund_code=code, trade_date=date(2026, 10, 14), nav=Decimal("1")))
        await db.commit()

        holdings = {h.fund_code: h for h in await ps.get_holdings(p.id, db)}
        assert holdings["A"].category_label == "AI · US Equity"
        assert holdings["C"].category_label is None

        alloc = await ps.get_allocation(p.id, db)
        assert {i.label: float(i.pct) for i in alloc.by_region} == {"US": 80.0, "Unknown": 20.0}
        assert {i.label: float(i.pct) for i in alloc.by_theme} == {"Broad Market": 60.0, "AI": 20.0, "Unknown": 20.0}
