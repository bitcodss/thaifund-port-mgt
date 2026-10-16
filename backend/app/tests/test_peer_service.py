"""Peer-group helpers: one peer per master fund."""
import pytest

from app.services.peer_service import master_key, normalise_master_name


@pytest.mark.parametrize("a,b", [
    ("JPMorgan Funds - US Growth Fund, Class I (acc) - USD", "JPMorgan Funds – US Growth Fund A2 USD"),
    ("Goldman Sachs India Equity Portfolio Class I Shares (Acc.)", "Goldman Sachs India Equity Portfolio"),
    ("Janus Henderson Global Life Sciences Fund I Acc", "JANUS HENDERSON GLOBAL LIFE SCIENCES FUND"),
    ("กองทุน PIMCO GIS Income Fund, Class INST USD Acc", "PIMCO GIS Income Fund"),
    ("Baillie Gifford Worldwide Discovery Fund Class B USD Acc", "Baillie Gifford Worldwide Discovery Fund"),
])
def test_same_master_written_differently_normalises_equal(a, b):
    assert normalise_master_name(a) == normalise_master_name(b)


def test_different_masters_stay_different():
    assert normalise_master_name("JPMorgan Funds - US Growth Fund") != \
        normalise_master_name("JPMorgan Funds - US Value Fund")
    assert normalise_master_name("iShares MSCI ACWI ETF") != normalise_master_name("iShares MSCI World ETF")


def test_isin_wins_over_name():
    assert master_key("us4642882579", "iShares MSCI ACWI ETF") == ("isin", "US4642882579")
    assert master_key(None, "iShares MSCI ACWI ETF") == ("name", "ISHARES MSCI ACWI ETF")
    assert master_key(None, None) is None


# ── chunk 3: peer profiles ────────────────────────────────────────────────────

from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.fund import Fund
from app.models.market import FundFactsheet, PeerProfile
from app.services import peer_service as ps
from app.services.llm import LlmError


def _cls(name, detail, tax="", fx="", abbr="X"):
    return {"proj_abbr_name": abbr, "fund_class_name": name, "fund_class_detail": detail,
            "fund_class_tax_incentive_type": tax, "exchange_rate_protection_policy": fx}


SSF = "กองทุนรวมเพื่อการออม (Super Savings Fund : SSF)"


@pytest.mark.parametrize("classes,expected", [
    # real SEC class layouts (SCBSEMI, MEGA10AI, KKP DIVIDEND, KKP PGE-UH, MEGA10)
    ([_cls("SCBSEMI(A)", "ชนิดสะสมมูลค่า"), _cls("SCBSEMI(D)", "ชนิดจ่ายเงินปันผล"),
      _cls("SCBSEMI(E)", "ชนิดช่องทางอิเล็กทรอนิกส์"), _cls("SCBSEMI(IN)", "ชนิดควบประกัน"),
      _cls("SCBSEMI(P)", "ชนิดผู้ลงทุนกลุ่ม/บุคคล"), _cls("SCBSEMI(SSF)", "ชนิดเพื่อการออม", SSF)], "SCBSEMI(A)"),
    ([_cls("MEGA10AI-I", "ชนิดผู้ลงทุนสถาบัน"), _cls("MEGA10AI-SSF", "ชนิดเพื่อการออม", SSF),
      _cls("MEGA10AI-A", "ชนิดสะสมมูลค่า")], "MEGA10AI-A"),
    ([_cls("KKP DIVIDEND-F", "ชนิด F"), _cls("KKP DIVIDEND", "ชนิดทั่วไป"),
      _cls("KKP DIVIDEND-SSF", "ชนิดเพื่อการออม", SSF)], "KKP DIVIDEND"),
    ([_cls("main", None, abbr="DAOL-GOLDRMF")], "DAOL-GOLDRMF"),
    ([_cls("MEGA10-SSF", "ชนิดเพื่อการออม", SSF), _cls("MEGA10-A", "ชนิดสะสมมูลค่า")], "MEGA10-A"),
    # RMF-only project: every class has the tax incentive → fall back to its accumulation class
    ([_cls("X-RMF-D", "ชนิดจ่ายเงินปันผล", "RMF"), _cls("X-RMF-A", "ชนิดสะสมมูลค่า", "RMF")], "X-RMF-A"),
])
def test_representative_class(classes, expected):
    assert ps.representative_class(classes) == expected


@pytest.mark.parametrize("policy,flag,expected", [
    ("ไม่ป้องกัน (unhedged)", "1", "unhedged"),
    ("ทั้งหมด (fully hedged) (95%-105% ของมูลค่าความเสี่ยง)", "1", "full"),
    ("ดุลยพินิจ (dynamic hedging) (0%-105% ของมูลค่าความเสี่ยง)", "1", "dynamic"),
    (None, "3", "domestic"), ("", "1", "unknown"),
])
def test_hedge_group(policy, flag, expected):
    assert ps.hedge_group(policy, flag) == expected


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _profile(proj, abbr, policy="ตราสารทุน", flag="1", style="PN", master=None):
    return [{"proj_id": proj, "proj_abbr_name": abbr, "proj_name_en": abbr, "policy_desc": policy,
             "invest_country_flag": flag, "management_style": style, "feederfund_master_fund": master,
             "fund_class_name": "main", "fund_class_detail": None, "fund_class_tax_incentive_type": "",
             "exchange_rate_protection_policy": "ไม่ป้องกัน", "investment_policy_desc": "<p>US semis</p>"}]


async def _seed(db, proj, cat, isin=None, scope="fund_units"):
    db.add(Fund(fund_code=f"F-{proj}", sec_proj_id=proj, fund_status="RG"))
    db.add(FundFactsheet(sec_proj_id=proj, scan_status="ok", aimc_category=cat, master_isin=isin,
                         holdings_scope=scope, holdings=[]))
    await db.commit()


async def _run(db, profiles, theme=None, infer=None, force=False):
    theme = theme or AsyncMock(return_value={"theme": "Semiconductor", "is_esg": False, "reason": "chips"})
    infer = infer or AsyncMock(return_value={"category": "Technology Equity", "reason": "AI ETF"})
    with patch("app.services.sec_api.get_fund_profiles", new=AsyncMock(side_effect=lambda k, p: profiles[p])), \
            patch("app.services.sec_api.get_benchmarks", new=AsyncMock(return_value=[{"benchmark": "SOX"}])), \
            patch("app.services.peer_service.classify_theme", new=theme), \
            patch("app.services.peer_service.infer_aimc_category", new=infer), \
            patch("app.services.peer_service.settings") as st:
        st.SEC_API_KEY, st.OLLAMA_MODEL = "k", "test-model"
        return await ps.build_peer_profiles(db, force=force), theme, infer


class TestBuildPeerProfiles:

    @pytest.mark.asyncio
    async def test_parsed_category_with_theme_list_is_themed(self, db):
        await _seed(db, "P1", "Technology Equity", isin="IE00BMC38736")
        r, theme, infer = await _run(db, {"P1": _profile("P1", "SEMI", master="VanEck Semiconductor UCITS ETF")})
        p = await db.get(PeerProfile, "P1")
        assert (p.aimc_category, p.aimc_source, p.peer_theme, p.theme_source) == \
            ("Technology Equity", "parsed", "Semiconductor", "ai")
        assert (p.master_key_kind, p.master_key, p.is_passive, p.hedge_group) == ("isin", "IE00BMC38736", True, "unhedged")
        infer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uncategorised_foreign_equity_is_inferred_then_themed(self, db):
        await _seed(db, "P2", None)
        r, theme, infer = await _run(db, {"P2": _profile("P2", "LHAI", master="Roundhill Generative AI & Technology ETF")})
        p = await db.get(PeerProfile, "P2")
        assert (p.aimc_category, p.aimc_source) == ("Technology Equity", "inferred")
        assert p.master_key_kind == "name"                         # no ISIN → normalised master name
        assert r["inferred"] == 1 and r["themed"] == 1

    @pytest.mark.asyncio
    async def test_domestic_or_non_equity_without_category_gets_no_llm(self, db):
        await _seed(db, "P3", None)
        r, theme, infer = await _run(db, {"P3": _profile("P3", "THAIBOND", policy="ตราสารหนี้", flag="3")})
        p = await db.get(PeerProfile, "P3")
        assert p.aimc_category is None and p.hedge_group == "unhedged"
        infer.assert_not_awaited(); theme.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_theme_category_needs_no_llm(self, db):
        await _seed(db, "P4", "India Equity")
        r, theme, infer = await _run(db, {"P4": _profile("P4", "INDIA")})
        assert (await db.get(PeerProfile, "P4")).peer_theme == "India Equity"
        theme.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unchanged_inputs_skip_llm_and_manual_theme_is_kept(self, db):
        await _seed(db, "P1", "Technology Equity")
        prof = {"P1": _profile("P1", "SEMI")}
        await _run(db, prof)
        p = await db.get(PeerProfile, "P1")
        p.peer_theme, p.theme_source = "AI", "manual"
        await db.commit()
        r, theme, _ = await _run(db, prof)
        assert r["unchanged"] == 1
        theme.assert_not_awaited()
        r, theme, _ = await _run(db, prof, force=True)
        theme.assert_not_awaited()                                   # manual override survives force
        assert (await db.get(PeerProfile, "P1")).peer_theme == "AI"

    @pytest.mark.asyncio
    async def test_llm_failure_keeps_deterministic_fields_and_retries(self, db):
        await _seed(db, "P1", "Technology Equity", isin="IE00BMC38736")
        prof = {"P1": _profile("P1", "SEMI")}
        r, _, _ = await _run(db, prof, theme=AsyncMock(side_effect=LlmError("down")))
        p = await db.get(PeerProfile, "P1")
        assert r["error"] == 1 and p.master_key == "IE00BMC38736" and p.input_hash is None
        r, theme, _ = await _run(db, prof)
        theme.assert_awaited_once()                                  # retried next run


# ── peer groups + API ─────────────────────────────────────────────────────────

import uuid
from datetime import date
from decimal import Decimal


async def _prof(db, proj, cat, theme, master=None, hedge="unhedged"):
    db.add(Fund(fund_code=f"C-{proj}", sec_proj_id=proj, fund_status="RG"))
    db.add(PeerProfile(sec_proj_id=proj, aimc_category=cat, aimc_source="parsed", peer_theme=theme,
                       theme_source="ai", master_key_kind="isin" if master else None, master_key=master,
                       representative_code=f"C-{proj}", hedge_group=hedge))


class TestPeerGroup:

    @pytest.mark.asyncio
    async def test_theme_group_when_five_distinct_masters_else_category(self, db):
        for i in range(5):
            await _prof(db, f"S{i}", "Technology Equity", "Semiconductor", master=f"ISIN{i}")
        await _prof(db, "S5", "Technology Equity", "Semiconductor", master="ISIN0")   # second feeder of ISIN0
        await _prof(db, "A1", "Technology Equity", "AI", master="AI1")
        await db.commit()
        g = await ps.peer_group(db, "S0")
        assert g["basis"] == "theme" and g["theme_masters"] == 5
        assert len(g["masters"]) == 5
        own = g["masters"][0]                                           # own master listed first
        assert own["master_key"] == "ISIN0" and {f["sec_proj_id"] for f in own["funds"]} == {"S0", "S5"}

        g = await ps.peer_group(db, "A1")                               # AI theme has 1 master → category
        assert g["basis"] == "category" and len(g["masters"]) == 6

    @pytest.mark.asyncio
    async def test_direct_stock_funds_are_their_own_peer(self, db):
        await _prof(db, "D1", "US Equity", "Growth")
        await _prof(db, "D2", "US Equity", "Growth")
        await db.commit()
        g = await ps.peer_group(db, "D1")
        assert len(g["masters"]) == 2


@pytest_asyncio.fixture
async def admin_client(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from app.api.deps import get_current_user
    from app.models.user import User
    admin = User(id=uuid.uuid4(), email="admin@x", password_hash="x", role="admin")
    db.add(admin)
    await db.commit()

    async def _override_db():
        yield db

    async def _override_user():
        return admin

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    yield TestClient(app), admin
    app.dependency_overrides.clear()


class TestPeersApi:

    @pytest.mark.asyncio
    async def test_held_groups_and_theme_override(self, db, admin_client):
        from app.models.portfolio import Portfolio
        from app.models.tax_lot import TaxLot
        client, admin = admin_client
        await _prof(db, "S0", "Technology Equity", "Semiconductor", master="ISIN0")
        pid = uuid.uuid4()
        db.add(Portfolio(id=pid, user_id=admin.id, name="P"))
        db.add(TaxLot(id=uuid.uuid4(), portfolio_id=pid, fund_code="C-S0", original_purchase_date=date(2025, 1, 1),
                      units_remaining=Decimal("1"), cost_basis_remaining=Decimal("1"), tax_scheme="NORMAL"))
        await db.commit()

        held = client.get("/api/v1/peers/held").json()
        assert [(h["sec_proj_id"], h["held_codes"], h["peer_theme"]) for h in held] == [("S0", ["C-S0"], "Semiconductor")]

        assert client.put("/api/v1/peers/S0/theme", json={"theme": "Crypto"}).status_code == 422
        r = client.put("/api/v1/peers/S0/theme", json={"theme": "AI"}).json()
        assert (r["peer_theme"], r["theme_source"]) == ("AI", "manual")
        client.put("/api/v1/peers/S0/theme", json={"theme": None})
        p = await db.get(PeerProfile, "S0")
        await db.refresh(p)
        assert p.theme_source is None and p.input_hash is None          # re-classified next build


class TestInferenceImprovements:

    @pytest.mark.asyncio
    async def test_inherits_category_from_same_master_siblings(self, db):
        await _prof(db, "SIB1", "Global Equity", "Growth", master="IE00SAME")
        await _prof(db, "SIB2", "Global Equity", "Growth", master="IE00SAME")
        await db.commit()
        await _seed(db, "NEW", None, isin="IE00SAME")
        r, theme, infer = await _run(db, {"NEW": _profile("NEW", "ONE-DISC")},
                                     theme=AsyncMock(return_value={"theme": "Growth", "is_esg": False, "reason": "x"}))
        p = await db.get(PeerProfile, "NEW")
        assert (p.aimc_category, p.aimc_source) == ("Global Equity", "inferred")
        assert "Same master fund as 2" in p.aimc_reason
        infer.assert_not_awaited()                                    # no LLM guess needed

    @pytest.mark.asyncio
    async def test_manual_category_survives_build(self, db):
        await _seed(db, "P9", None)
        db.add(PeerProfile(sec_proj_id="P9", aimc_category="Health Care", aimc_source="manual",
                           peer_theme="Life Sciences & Biotech", theme_source="manual"))
        await db.commit()
        r, theme, infer = await _run(db, {"P9": _profile("P9", "SCBGENOME")}, force=True)
        p = await db.get(PeerProfile, "P9")
        assert (p.aimc_category, p.aimc_source, p.peer_theme) == ("Health Care", "manual", "Life Sciences & Biotech")
        infer.assert_not_awaited()

    def test_every_inferable_category_has_a_definition(self):
        from app.services.aimc import AIMC_DEFINITIONS
        assert set(AIMC_DEFINITIONS) == set(ps.INFERABLE_CATEGORIES)


@pytest.mark.asyncio
async def test_category_override_api(db, admin_client):
    client, _ = admin_client
    await _prof(db, "G1", "Global Equity - Alternative Energy", "Global Equity - Alternative Energy")
    await db.commit()
    assert client.put("/api/v1/peers/G1/category", json={"category": "Miscellaneous"}).status_code == 422
    r = client.put("/api/v1/peers/G1/category", json={"category": "Health Care"}).json()
    assert (r["aimc_category"], r["aimc_source"]) == ("Health Care", "manual")
    p = await db.get(PeerProfile, "G1")
    await db.refresh(p)
    assert p.theme_source is None                                       # re-themed within Health Care next build


class TestPendingInferred:

    @pytest.mark.asyncio
    async def test_inferred_members_excluded_until_approved(self, db, admin_client):
        client, _ = admin_client
        for i in range(4):
            await _prof(db, f"A{i}", "Technology Equity", "AI", master=f"M{i}")
        db.add(Fund(fund_code="C-I1", sec_proj_id="I1", fund_status="RG", name_en="Inferred AI fund",
                    master_fund="Some AI ETF"))
        db.add(PeerProfile(sec_proj_id="I1", aimc_category="Technology Equity", aimc_source="inferred",
                           aimc_reason="AI ETF", peer_theme="AI", theme_source="ai", master_key="M9",
                           master_key_kind="isin"))
        await db.commit()
        g = await ps.peer_group(db, "A0")
        assert g["basis"] == "category" and g["theme_masters"] == 4    # pending fund not counted
        assert g["pending_excluded"] == 1
        assert all(f["sec_proj_id"] != "I1" for m in g["masters"] for f in m["funds"])

        pending = client.get("/api/v1/peers/pending").json()
        assert [(p["fund_code"], p["master_fund"], p["aimc_category"]) for p in pending] == \
            [("C-I1", "Some AI ETF", "Technology Equity")]
        assert client.post("/api/v1/peers/approve", json={"proj_ids": ["I1", "A0"]}).json() == {"approved": ["I1"]}
        g = await ps.peer_group(db, "A0")
        assert g["basis"] == "theme" and g["theme_masters"] == 5 and g["pending_excluded"] == 0

    @pytest.mark.asyncio
    async def test_categories_endpoint_excludes_miscellaneous(self, admin_client):
        client, _ = admin_client
        cats = client.get("/api/v1/peers/categories").json()
        assert len(cats) == 44 and "Miscellaneous" not in cats
