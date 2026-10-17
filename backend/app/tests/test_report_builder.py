"""Phase 3: report builder — guarded LLM sections, shared news, template fallback, HTML."""
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.fund import Fund, NavHistory
from app.models.market import FundProxy, MonthlyReport, NewsDigest
from app.models.portfolio import Portfolio
from app.models.tax_lot import TaxLot
from app.models.user import User
from app.services import report_builder as rb

SEP = date(2026, 9, 30)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


class TestChecks:

    def test_numbers_must_come_from_facts(self):
        allowed = rb.allowed_from_text('{"return_1m": "+9.6%"}')
        assert rb.check_text("กองนี้ขึ้น 9.6% ในเดือนนี้", allowed, locked=False) == []
        assert rb.check_text("กองนี้ขึ้น 12.4%", allowed, locked=False)

    def test_advice_rejected(self):
        assert rb.check_text("ควรขายกองนี้", set(), locked=False)
        assert rb.check_text("You should buy more", set(), locked=False)

    def test_sell_wording_rejected_only_for_locked_funds_and_not_market_selling_pressure(self):
        assert rb.check_text("อาจพิจารณาขายคืนหน่วยลงทุน", set(), locked=True)
        assert rb.check_text("อาจพิจารณาขายคืนหน่วยลงทุน", set(), locked=False) == []
        assert rb.check_text("หุ้นชิปเจอแรงขายในเดือนนี้", set(), locked=True) == []

    def test_sort_flagged_first_then_weakest(self):
        funds = [{"fund_code": "A", "flags": [], "return_3m": -0.2}, {"fund_code": "B", "flags": [1], "return_3m": 0.1},
                 {"fund_code": "C", "flags": [], "return_3m": 0.05}]
        assert [f["fund_code"] for f in rb.sort_funds(funds)] == ["B", "A", "C"]


async def _seed(db):
    user = User(id=uuid.uuid4(), email="u@x", password_hash="x", role="user", date_of_birth=date(1988, 1, 1))
    port = Portfolio(id=uuid.uuid4(), user_id=user.id, name="RMF")
    db.add_all([user, port])
    for code, pid in (("SEMI-RMF", "P1"), ("ASIA-RMF", "P2")):
        db.add(Fund(fund_code=code, sec_proj_id=pid, fund_status="RG", name_en=code))
        db.add(TaxLot(portfolio_id=port.id, fund_code=code, original_purchase_date=date(2025, 1, 2),
                      units_remaining=Decimal("100"), cost_basis_remaining=Decimal("1000"), tax_scheme="RMF"))
        for d, v in ((date(2026, 6, 30), 10), (date(2026, 7, 31), 10), (date(2026, 8, 31), 10), (SEP, 10.96)):
            db.add(NavHistory(fund_code=code, trade_date=d, nav=Decimal(str(v))))
    # TSM held by both funds → shared driver; NVDA only by SEMI (bigger weight than MU)
    for pid, ticker, w in (("P1", "TSM", 9), ("P2", "TSM", 8), ("P1", "NVDA", 11), ("P1", "MU", 4)):
        db.add(FundProxy(sec_proj_id=pid, kind="news", ticker=ticker, label=ticker, source="factsheet",
                         valid=True, weight_pct=Decimal(w)))
    for ticker in ("TSM", "NVDA", "MU"):
        db.add(NewsDigest(ticker=ticker, week_start=date(2026, 9, 21), summary=f"{ticker} news 3 items",
                          sentiment="positive", item_count=3, status="ok"))
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_shared_ticker_told_once_and_own_news_by_weight(db):
    await _seed(db)
    per_proj, users = await rb.news_for_projects(db, {"P1", "P2"}, SEP)
    assert [n["ticker"] for n in per_proj["P1"]] == ["NVDA", "TSM", "MU"]          # by holding weight
    assert users["TSM"] == {"P1", "P2"}


@pytest.mark.asyncio
async def test_build_uses_checked_llm_text_falls_back_to_template_and_renders(db):
    user = await _seed(db)
    calls = {"n": 0}

    async def fake_llm(prompt, schema, **kw):
        calls["n"] += 1
        fields = list(schema["properties"])
        if "fund card" in prompt and '"กองทุน": "SEMI-RMF"' in prompt:
            assert '"ชื่อ": "NVDA"' in prompt and '"ข่าวที่เล่าในภาพรวมพอร์ต": ["TSM"]' in prompt
            return {f: "ขึ้น +9.6% ในเดือนนี้" for f in fields}                     # numbers from the facts
        if "fund card" in prompt:
            return {f: "อาจขายคืนหน่วยลงทุนได้ 55%" for f in fields}                # locked + bad number → template
        return {f: "ภาพรวมเดือนกันยายน" for f in fields}
    with patch("app.services.report_builder.generate_json", new=AsyncMock(side_effect=fake_llm)):
        row = await rb.build_user_report(db, user.id, SEP)

    assert row.status == "partial" and any("ASIA-RMF" in e for e in row.errors)
    cards = {f["fund_code"]: f for p in row.content["portfolios"] for f in p["funds"]}
    assert cards["SEMI-RMF"]["card_source"] == "llm" and cards["ASIA-RMF"]["card_source"] == "template"
    assert cards["SEMI-RMF"]["shared_tickers"] == ["TSM"] and cards["SEMI-RMF"]["news_tickers"] == ["NVDA", "MU"]
    assert row.facts["shared_tickers"] == ["TSM"]
    assert "รายงานกองทุนประจำเดือน กันยายน 2026" in row.html and "SEMI-RMF" in row.html
    assert "ไม่ใช่คำแนะนำการลงทุน" in row.html and "ขายคืนหน่วยลงทุนได้" not in row.html
    assert calls["n"] == 1 + (rb.RETRIES + 1) + 1                                    # retries only for the bad card

    with patch("app.services.report_builder.generate_json", new=AsyncMock(side_effect=fake_llm)):
        await rb.build_user_report(db, user.id, SEP)                                # rebuild replaces, not duplicates
    from sqlalchemy import func, select
    assert (await db.execute(select(func.count()).select_from(MonthlyReport))).scalar() == 1


def test_users_cannot_read_other_users_reports_but_admins_can():
    from fastapi import HTTPException
    from app.api.reports import _target
    me, other = uuid.uuid4(), uuid.uuid4()
    user = User(id=me, email="a@x", password_hash="x", role="user")
    admin = User(id=uuid.uuid4(), email="b@x", password_hash="x", role="admin")
    assert _target(user, None) == me and _target(user, me) == me
    with pytest.raises(HTTPException) as e:
        _target(user, other)
    assert e.value.status_code == 403
    assert _target(admin, other) == other


def test_json_keys_and_invented_periods_rejected():
    allowed = rb.allowed_from_text("-31.0%")
    assert rb.check_text("ผลตอบแทน return_3m อยู่ที่ -31.0%", allowed, locked=False)
    assert rb.check_text("ผลตอบแทน 31-month อยู่ที่ -31.0%", allowed, locked=False)
    assert rb.check_text("ผลตอบแทน 3 เดือนอยู่ที่ -31.0%", allowed, locked=False) == []


def test_signal_sentences_explain_only_listed_flags():
    base = {"flags": []}
    assert rb.signal_sentences(base) == []
    f = {"flags": [{"rule": "bottom_3m", "display": "percentile", "percentile_3m": 11.0, "single_event": True,
                    "driver": {"month": "2026-07-31", "return_1m": -0.338}}]}
    s = rb.signal_sentences(f)[0]
    assert "กรกฎาคม 2026" in s and "-33.8%" in s and "ไม่ใช่ผลงานแย่ต่อเนื่อง" in s
    f["flags"][0]["single_event"] = False
    assert "หลายเดือน" in rb.signal_sentences(f)[0]


def test_misspelled_fund_code_rejected():
    src = '{"กองทุน": "LHCYBERRMF", "ชื่อ": "SCBSEMI(SSF)"}'
    assert rb.check_text("LHCYBERRRM สัดส่วน", set(), locked=False, source=src)
    assert rb.check_text("LHCYBERRMF และ SCBSEMI(SSF) ทำได้ดี", set(), locked=False, source=src) == []


def test_single_theme_category_not_repeated():
    p = {"context_only": False, "category": "Global Equity - Alternative Energy",
         "theme": "Global Equity - Alternative Energy", "peer_masters": 10}
    assert rb.peer_method(p).count("Alternative Energy") == 1
