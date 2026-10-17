"""
Market-context tests: number guard, RSS parsing, proxy suggestion/refresh,
news collection and weekly digests. Yahoo and the LLM are always mocked.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.fund import Fund
from app.models.market import FundFactsheet, FundProxy, NewsDigest, NewsItem
from app.models.tax_lot import SyncJob
from app.services import news_service, proxy_service
from app.services.llm import LlmError
from app.services.number_guard import allowed_from_text, extract_numbers, unsupported_numbers
from app.services.yahoo_client import parse_rss


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ── number guard ──────────────────────────────────────────────────────────────

class TestNumberGuard:

    def test_numbers_backed_by_facts_pass_with_rounding(self):
        assert unsupported_numbers("Fund fell 12.3% while index rose 4.07%", [Decimal("-12.345"), Decimal("4.07")]) == []

    def test_invented_number_is_reported(self):
        assert unsupported_numbers("Fund fell 15.2%", [Decimal("12.3")]) == ["15.2"]

    def test_sign_does_not_matter(self):
        assert unsupported_numbers("ลดลง 5.5%", [Decimal("-5.5")]) == []

    def test_small_ints_and_years_allowed(self):
        assert unsupported_numbers("over 3 months in 2026, top 5 holdings", []) == []

    def test_thai_digits_are_checked(self):
        assert extract_numbers("ลดลง ๑๒.๕%") == [("12.5", Decimal("12.5"))]
        assert unsupported_numbers("ลดลง ๑๒.๕%", [Decimal("3")]) == ["12.5"]

    def test_spelled_numbers_rejected(self):
        assert unsupported_numbers("ลดลงสิบเปอร์เซ็นต์", [Decimal("10")])
        assert unsupported_numbers("ร้อยละห้า", [Decimal("5")])
        assert unsupported_numbers("fell ten percent", [Decimal("10")])

    def test_thousands_separators(self):
        assert unsupported_numbers("assets of 1,234,567 baht", [Decimal("1234567")]) == []

    def test_allowed_from_text(self):
        assert allowed_from_text("Gold lost $100 in hours", "up 2.1%") == {Decimal("100"), Decimal("2.1")}


# ── RSS parsing ───────────────────────────────────────────────────────────────

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Nvidia debuts &amp; new models</title><description>&lt;p&gt;Three open models&lt;/p&gt;</description>
<link>https://x/1</link><pubDate>Thu, 15 Oct 2026 10:00:00 +0000</pubDate></item>
<item><title>No date</title><link>https://x/2</link></item>
</channel></rss>"""


def test_parse_rss_cleans_html_and_skips_incomplete_items():
    items = parse_rss(RSS)
    assert len(items) == 1
    assert items[0]["title"] == "Nvidia debuts & new models"
    assert items[0]["description"] == "Three open models"
    assert items[0]["published_at"] == datetime(2026, 10, 15, 10, tzinfo=timezone.utc)


# ── proxies ───────────────────────────────────────────────────────────────────

LLM_PROXIES = {
    "news_proxies": [{"ticker": "NVDA", "name": "NVIDIA", "reason": "top holding"},
                     {"ticker": "SOXX", "name": "iShares Semi", "reason": "theme"}],
}


def _fund(**kw):
    base = dict(fund_code="F-SSF", sec_proj_id="P1", theme="Semiconductor", region="US",
                management_style="PN", master_fund="VanEck Semiconductor UCITS ETF",
                top_holdings=[{"name": "หน่วยลงทุน VANECK SEMICONDUCTOR UCITS ETF", "pct": 99}])
    base.update(kw)
    return Fund(**base)


_QUOTES = {
    "MICRON TECHNOLOGY": [{"symbol": "MU", "quote_type": "EQUITY", "exchange": "NMS", "name": "Micron Technology, Inc."}],
    "NVIDIA": [{"symbol": "NVDA", "quote_type": "EQUITY", "exchange": "NMS", "name": "NVIDIA Corporation"}],
}


async def _search(q, limit=5):
    return _QUOTES.get(q, [])


class TestResolveHolding:

    @pytest.mark.asyncio
    async def test_prefers_us_listing_and_requires_name_match(self):
        quotes = [{"symbol": "005930.KS", "quote_type": "EQUITY", "exchange": "KSC", "name": "Samsung Electronics Co., Ltd."},
                  {"symbol": "SSNLF", "quote_type": "EQUITY", "exchange": "PNK", "name": "Samsung Electronics Co., Ltd."},
                  {"symbol": "XYZ", "quote_type": "EQUITY", "exchange": "NMS", "name": "Unrelated Widgets"}]
        validate = AsyncMock(side_effect=lambda kind, t: (t == "005930.KS", None))
        with patch("app.services.yahoo_client.search", new=AsyncMock(return_value=quotes)), \
                patch("app.services.proxy_service.validate", new=validate):
            q = await proxy_service.resolve_holding("Samsung Electronics Co Ltd")
        assert q["symbol"] == "005930.KS"      # SSNLF (OTC) tried first but failed the news check
        assert "XYZ" not in [c.args[1] for c in validate.await_args_list]   # name mismatch never tried

    @pytest.mark.asyncio
    async def test_truncated_name_does_not_match(self):
        quotes = [{"symbol": "NET", "quote_type": "EQUITY", "exchange": "NYQ", "name": "Cloudflare, Inc."}]
        with patch("app.services.yahoo_client.search", new=AsyncMock(return_value=quotes)), \
                patch("app.services.proxy_service.validate", new=AsyncMock(return_value=(True, None))):
            assert await proxy_service.resolve_holding("net, Inc.") is None

    @pytest.mark.asyncio
    async def test_bloomberg_ticker_and_fund_units(self):
        quotes = [{"symbol": "LRCX", "quote_type": "EQUITY", "exchange": "NMS", "name": "Lam Research"}]
        with patch("app.services.yahoo_client.search", new=AsyncMock(return_value=quotes)), \
                patch("app.services.proxy_service.validate", new=AsyncMock(return_value=(True, None))):
            assert (await proxy_service.resolve_holding("LRCX US"))["symbol"] == "LRCX"
            assert await proxy_service.resolve_holding("หน่วยลงทุน JANUS HENDERSON FUND") is None
            assert await proxy_service.resolve_holding("Premia China STAR 50 ETF") is None


def test_search_name_and_listing_rank():
    assert proxy_service.search_name("Eli Lilly & Co") == "Eli Lilly"
    assert proxy_service.search_name("J M Smucker Co/The") == "J M Smucker"
    assert proxy_service.search_name("BANK OF CHINA LIMITED CLASS H") == "BANK OF CHINA"
    assert proxy_service.search_name("AMAZON.COM INC") == "AMAZON.COM"
    assert proxy_service.search_name("MIDEA GROUP CO. LTD. CLASS H") == "MIDEA GROUP"
    ranks = [proxy_service._listing_rank(e) for e in ("NYQ", "PNK", "KSC", "FRA")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4


class TestHoldsStocksDirectly:
    def test_feeder_fund_is_not_direct(self):
        assert not proxy_service.holds_stocks_directly(_fund())

    def test_stock_holdings_are_direct(self):
        assert proxy_service.holds_stocks_directly(_fund(
            management_style="OT", top_holdings=[{"name": "NVIDIA CORP", "pct": 9}]))


class TestRefreshProxies:

    async def _run(self, db, llm=None, valid=True, force=False, search=None, factsheet=None):
        llm = llm or AsyncMock(return_value=LLM_PROXIES)
        with patch("app.services.proxy_service.generate_json", new=llm), \
                patch("app.services.proxy_service.validate", new=AsyncMock(return_value=(valid, 500))), \
                patch("app.services.proxy_service.ensure_factsheet", new=AsyncMock(return_value=factsheet)), \
                patch("app.services.yahoo_client.search", new=search or AsyncMock(return_value=[])):
            return await proxy_service.refresh_proxies(db, proj_ids={"P1"}, force=force), llm

    @pytest.mark.asyncio
    async def test_stores_validated_proxies_per_kind(self, db):
        db.add(_fund())
        await db.commit()
        result, _ = await self._run(db)
        assert result["updated"] == 1 and result["errors"] == []
        rows = (await db.execute(select(FundProxy).order_by(FundProxy.kind, FundProxy.ticker))).scalars().all()
        assert [(r.kind, r.ticker, r.source, r.valid) for r in rows] == [
            ("news", "NVDA", "ai", True), ("news", "SOXX", "ai", True)]
        assert (await db.execute(select(SyncJob))).scalar_one().type == "fund_proxies"

    @pytest.mark.asyncio
    async def test_direct_stock_fund_uses_resolved_holdings_only(self, db):
        db.add(_fund(management_style="OT", top_holdings=[
            {"name": "MICRON TECHNOLOGY INC", "pct": 9.8}, {"name": "NVIDIA CORP", "pct": 9.1}]))
        await db.commit()
        await self._run(db, search=_search)
        news = (await db.execute(select(FundProxy).where(FundProxy.kind == "news")
                                 .order_by(FundProxy.ticker))).scalars().all()
        assert [(r.ticker, r.source) for r in news] == [("MU", "holding"), ("NVDA", "holding")]   # no LLM mix-in

    @pytest.mark.asyncio
    async def test_feeder_uses_master_top5_from_factsheet(self, db):
        db.add(_fund())
        await db.commit()
        fs = FundFactsheet(sec_proj_id="P1", pdf_url="u", text_hash="h", holdings_scope="master",
                           holdings=[{"name": "NVIDIA CORP", "pct": 11.3}, {"name": "MICRON TECHNOLOGY INC", "pct": 11}])
        await self._run(db, search=_search, factsheet=fs)
        news = (await db.execute(select(FundProxy).where(FundProxy.kind == "news"))).scalars().all()
        assert sorted((r.ticker, r.source, r.weight_pct) for r in news) == [
            ("MU", "factsheet", Decimal("11")), ("NVDA", "factsheet", Decimal("11.3"))]   # holding weight kept
        assert all("master-fund top holding" in r.reason for r in news)

    @pytest.mark.asyncio
    async def test_fund_units_only_falls_back_to_unverified_llm(self, db):
        db.add(_fund())
        await db.commit()
        fs = FundFactsheet(sec_proj_id="P1", pdf_url="u", text_hash="h", holdings_scope="fund_units",
                           holdings=[{"name": "Premia China STAR 50 ETF", "pct": 73}])
        await self._run(db, factsheet=fs)
        news = (await db.execute(select(FundProxy).where(FundProxy.kind == "news"))).scalars().all()
        assert {r.source for r in news} == {"ai"}
        assert all("fund/ETF units" in r.reason for r in news)

    @pytest.mark.asyncio
    async def test_too_few_resolved_holdings_falls_back(self, db):
        db.add(_fund(management_style="OT", top_holdings=[
            {"name": "MICRON TECHNOLOGY INC", "pct": 9.8}, {"name": "Unknownco Widgets", "pct": 5}]))
        await db.commit()
        await self._run(db, search=_search)
        news = (await db.execute(select(FundProxy).where(FundProxy.kind == "news"))).scalars().all()
        assert {r.source for r in news} == {"ai"}                    # 1 < MIN_HOLDING_TICKERS: no partial mix
        assert all("only 1 of 2 holdings" in r.reason for r in news)

    @pytest.mark.asyncio
    async def test_unchanged_inputs_skip_llm_and_changed_holdings_resync(self, db):
        db.add(_fund())
        await db.commit()
        await self._run(db)
        result, llm = await self._run(db)
        assert result["unchanged"] == 1
        llm.assert_not_awaited()

        fund = await db.get(Fund, "F-SSF")
        fund.top_holdings = [{"name": "หน่วยลงทุน NEW MASTER", "pct": 99}]
        await db.commit()
        result, llm = await self._run(db)
        assert result["updated"] == 1
        llm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_manual_kind_is_never_replaced(self, db):
        db.add(_fund())
        db.add(FundProxy(sec_proj_id="P1", kind="news", ticker="SONY", source="manual", valid=True))
        await db.commit()
        result, llm = await self._run(db, force=True)
        news = (await db.execute(select(FundProxy).where(FundProxy.kind == "news"))).scalars().all()
        assert [(p.ticker, p.source) for p in news] == [("SONY", "manual")]
        llm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_llm_call_when_holdings_give_news_tickers(self, db):
        db.add(_fund(management_style="OT", top_holdings=[
            {"name": "MICRON TECHNOLOGY INC", "pct": 9.8}, {"name": "NVIDIA CORP", "pct": 9.1}]))
        await db.commit()
        _, llm = await self._run(db, search=_search)
        llm.assert_not_awaited()                                      # price proxies gone → LLM only as fallback

    @pytest.mark.asyncio
    async def test_invalid_ticker_stored_as_invalid(self, db):
        db.add(_fund())
        await db.commit()
        await self._run(db, valid=False)
        assert all(not p.valid for p in (await db.execute(select(FundProxy))).scalars())

    @pytest.mark.asyncio
    async def test_llm_failure_keeps_existing(self, db):
        db.add(_fund())
        await db.commit()
        await self._run(db)
        result, _ = await self._run(db, llm=AsyncMock(side_effect=LlmError("down")), force=True)
        assert "down" in result["errors"][0]
        assert len((await db.execute(select(FundProxy))).scalars().all()) == 2

    @pytest.mark.asyncio
    async def test_set_manual_and_clear(self, db):
        with patch("app.services.proxy_service.validate", new=AsyncMock(return_value=(True, None))):
            rows = await proxy_service.set_manual(db, "P1", "news", [" ewj ", "EWJ", "SONY"],
                                                  weights={"sony": Decimal("4.2")})
        assert [(r.ticker, r.source, r.weight_pct) for r in rows] == [
            ("EWJ", "manual", None), ("SONY", "manual", Decimal("4.2"))]
        await proxy_service.set_manual(db, "P1", "news", [])
        assert (await db.execute(select(FundProxy))).scalars().all() == []


@pytest.mark.asyncio
async def test_validate_rejects_index_and_quiet_tickers_for_news():
    now = datetime.now(timezone.utc)
    busy = [{"published_at": now - timedelta(days=d)} for d in (1, 2, 3)]
    quiet = [{"published_at": now - timedelta(days=d)} for d in (1, 30, 40)]
    with patch("app.services.yahoo_client.rss", new=AsyncMock(return_value=busy)):
        assert await proxy_service.validate("news", "^N225") == (False, None)
        assert await proxy_service.validate("news", "SONY") == (True, None)
    with patch("app.services.yahoo_client.rss", new=AsyncMock(return_value=quiet)):
        assert await proxy_service.validate("news", "EWJ") == (False, None)   # too little recent news


# ── news collection ───────────────────────────────────────────────────────────

def _item(n, title=None, days_ago=0, desc=None):
    return {"title": title or f"Headline {n}", "description": desc, "link": f"https://news/{n}",
            "published_at": datetime.now(timezone.utc) - timedelta(days=days_ago)}


class TestCollectNews:

    @pytest.mark.asyncio
    async def test_dedupes_by_link_and_title_and_prunes_old(self, db):
        db.add(FundProxy(sec_proj_id="P1", kind="news", ticker="NVDA", source="ai", valid=True))
        db.add(FundProxy(sec_proj_id="P1", kind="news", ticker="BAD", source="ai", valid=False))
        db.add(NewsItem(ticker="NVDA", title="Ancient", link="https://old", title_key="ancient",
                        published_at=datetime.now(timezone.utc) - timedelta(days=120)))
        await db.commit()
        feed = [_item(1), _item(1), _item(2, title="Headline 1!"), _item(3, days_ago=100), _item(4)]
        rss = AsyncMock(return_value=feed)
        with patch("app.services.yahoo_client.rss", new=rss):
            result = await news_service.collect_news(db)
            again = await news_service.collect_news(db)

        rss.assert_awaited_with("NVDA")                  # invalid proxy not collected
        assert result["inserted"] == 2                   # 1 and 4 (dup link, dup title, too old skipped)
        assert result["pruned"] == 1
        assert again["inserted"] == 0
        assert {i.title for i in (await db.execute(select(NewsItem))).scalars()} == {"Headline 1", "Headline 4"}


# ── weekly digests ────────────────────────────────────────────────────────────

WEEK = date(2026, 10, 12)  # Monday


async def _seed_week(db, n=3):
    db.add(FundProxy(sec_proj_id="P1", kind="news", ticker="XLV", label="Health Care Select Sector SPDR",
                     source="ai", valid=True))
    for i in range(n):
        db.add(NewsItem(ticker="XLV", title=f"Health care stocks rose {i + 1}.5%", description="Sector rally",
                        link=f"https://n/{i}", title_key=f"k{i}",
                        published_at=datetime(2026, 10, 13, 3, tzinfo=timezone.utc)))
    db.add(NewsItem(ticker="XLV", title="Last week item", link="https://n/old", title_key="old",
                    published_at=datetime(2026, 10, 9, 3, tzinfo=timezone.utc)))
    await db.commit()


class TestDigests:

    def test_last_full_week_start(self):
        assert news_service.last_full_week_start(date(2026, 10, 19)) == date(2026, 10, 12)   # Monday
        assert news_service.last_full_week_start(date(2026, 10, 18)) == date(2026, 10, 5)    # Sunday

    @pytest.mark.asyncio
    async def test_ok_digest_keeps_sources_and_used_flags(self, db):
        await _seed_week(db)
        llm = AsyncMock(return_value={"summary": "Health care rose 2.5% midweek.", "sentiment": "positive",
                                      "used_items": [1]})
        with patch("app.services.news_service.generate_json", new=llm):
            result = await news_service.build_digests(db, week_start=WEEK)
        assert result["ok"] == 1
        d = (await db.execute(select(NewsDigest))).scalar_one()
        assert (d.status, d.sentiment, d.item_count) == ("ok", "positive", 3)   # last week's item excluded
        assert len(d.sources) == 3 and sum(s["used"] for s in d.sources) == 1

    @pytest.mark.asyncio
    async def test_invented_number_triggers_retry_then_failure(self, db):
        await _seed_week(db)
        bad = {"summary": "Sector jumped 9.9%.", "sentiment": "positive", "used_items": [1]}
        good = {"summary": "Sector rose 1.5%.", "sentiment": "positive", "used_items": [1]}
        llm = AsyncMock(side_effect=[bad, good])
        with patch("app.services.news_service.generate_json", new=llm):
            await news_service.build_digests(db, week_start=WEEK)
        assert llm.await_count == 2
        assert "9.9" in llm.await_args_list[1].args[0]               # retry prompt names the bad number
        assert (await db.execute(select(NewsDigest))).scalar_one().status == "ok"

        llm = AsyncMock(return_value=bad)
        with patch("app.services.news_service.generate_json", new=llm):
            result = await news_service.build_digests(db, week_start=WEEK, force=True)
        assert llm.await_count == news_service.DIGEST_RETRIES + 1
        assert result["failed"] == 1
        d = (await db.execute(select(NewsDigest))).scalar_one()
        assert d.status == "failed" and "9.9" in d.error and d.summary is None

    @pytest.mark.asyncio
    async def test_no_news_and_skip_existing(self, db):
        db.add(FundProxy(sec_proj_id="P1", kind="news", ticker="EWJ", source="ai", valid=True))
        await db.commit()
        llm = AsyncMock()
        with patch("app.services.news_service.generate_json", new=llm):
            first = await news_service.build_digests(db, week_start=WEEK)
            second = await news_service.build_digests(db, week_start=WEEK)
        llm.assert_not_awaited()
        assert first["no_news"] == 1 and second["skipped"] == 1

    @pytest.mark.asyncio
    async def test_failed_digest_is_retried_next_run(self, db):
        await _seed_week(db)
        with patch("app.services.news_service.generate_json", new=AsyncMock(side_effect=LlmError("down"))):
            await news_service.build_digests(db, week_start=WEEK)
        ok = {"summary": "Quiet week.", "sentiment": "neutral", "used_items": []}
        with patch("app.services.news_service.generate_json", new=AsyncMock(return_value=ok)):
            result = await news_service.build_digests(db, week_start=WEEK)
        assert result["ok"] == 1
        assert len((await db.execute(select(NewsDigest))).scalars().all()) == 1


# ── API ───────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def admin_client(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from app.api.deps import get_current_user
    from app.models.user import User

    admin = User(id=uuid.uuid4(), email="admin@x", password_hash="x", role="admin")
    db.add_all([admin, _fund()])
    await db.commit()

    async def _override_db():
        yield db

    async def _override_user():
        return admin

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestMarketApi:

    @pytest.mark.asyncio
    async def test_put_then_get_proxies(self, db, admin_client):
        with patch("app.services.proxy_service.validate", new=AsyncMock(return_value=(True, 500))):
            r = admin_client.put("/api/v1/funds/F-SSF/proxies/news", json={"tickers": ["nvda"]})
        assert r.status_code == 200, r.text
        assert r.json()[0]["ticker"] == "NVDA" and r.json()[0]["source"] == "manual"
        got = admin_client.get("/api/v1/funds/F-SSF/proxies").json()
        assert [(p["kind"], p["ticker"], p["verified"]) for p in got] == [("news", "NVDA", True)]
        assert admin_client.put("/api/v1/funds/F-SSF/proxies/price", json={"tickers": ["SMH"]}).status_code == 422

    @pytest.mark.asyncio
    async def test_llm_fallback_is_flagged_unverified(self, db, admin_client):
        db.add(FundProxy(sec_proj_id="P1", kind="news", ticker="BABA", source="ai", valid=True))
        db.add(FundProxy(sec_proj_id="P1", kind="news", ticker="TSM", source="factsheet", valid=True))
        await db.commit()
        got = {p["ticker"]: p["verified"] for p in admin_client.get("/api/v1/funds/F-SSF/proxies").json()}
        assert got == {"BABA": False, "TSM": True}

    @pytest.mark.asyncio
    async def test_override_records_source_and_flags_stale(self, db, admin_client):
        with patch("app.services.proxy_service.validate", new=AsyncMock(return_value=(True, None))):
            fresh = admin_client.put("/api/v1/funds/F-SSF/proxies/news", json={
                "tickers": ["NVDA"], "source_note": "Master factsheet", "source_as_of": date.today().isoformat()})
            assert fresh.json()[0]["source_note"] == "Master factsheet"
            assert fresh.json()[0]["override_stale"] is False
            old = admin_client.put("/api/v1/funds/F-SSF/proxies/news", json={
                "tickers": ["SMH"], "source_as_of": (date.today() - timedelta(days=120)).isoformat()})
        assert old.json()[0]["override_stale"] is True

    @pytest.mark.asyncio
    async def test_put_rejects_unknown_kind(self, admin_client):
        r = admin_client.put("/api/v1/funds/F-SSF/proxies/rumours", json={"tickers": ["X"]})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_news_digests_for_fund(self, db, admin_client):
        db.add(FundProxy(sec_proj_id="P1", kind="news", ticker="SOXX", source="ai", valid=True))
        db.add(NewsDigest(ticker="SOXX", week_start=WEEK, summary="Chips rallied.", sentiment="positive",
                          item_count=4, sources=[], status="ok"))
        db.add(NewsDigest(ticker="OTHER", week_start=WEEK, summary="x", item_count=1, status="ok"))
        await db.commit()
        got = admin_client.get("/api/v1/funds/F-SSF/news-digests").json()
        assert [(d["ticker"], d["sentiment"]) for d in got] == [("SOXX", "positive")]

    @pytest.mark.asyncio
    async def test_digest_trigger_requires_monday(self, admin_client):
        r = admin_client.post("/api/v1/sync/news/digests?week_start=2026-10-13")
        assert r.status_code == 400


# ── factsheet parsing ─────────────────────────────────────────────────────────

FEEDER_TEXT = """\
สัดสวนประเภททรัพยสินที่ลงทุน                                       ทรัพยสินที่ลงทุน 5 อันดับแรก
หน่วยลงทุน                         98.57   หน วยลงทุน JPMorgan Funds - Asia Growth Fund     99.00
ทรัพยสิน อื่น                      -0.93   ทรัพยสินที่ลงทุน 5 อันดับแรก ของกองทุนหลัก
                                                  ทรั พ ย ส ิ น                                % NAV
                                                  Taiwan Semiconductor Manufacturing Co Ltd     10.18
                                                  Samsung Electronics Co Ltd                     9.84
                                                  SK hynix Inc                                   7.56
                                                  หมายเหตุ : เปนขอมูลการลงทุนของกองหลัก
ISIN code : LU0943624824
"""

ALLOCATION_ONLY = """\
การจัดสรรการลงทุนในตางประเทศ 5 อันดับแรก
                          UNITED STATES                        87.41
                          ISRAEL                                4.34
"""


class TestFactsheetParsing:

    def test_master_block_preferred_over_fund_unit_row(self):
        from app.services.factsheet_service import parse_top_holdings, parse_master_isin
        b = parse_top_holdings(FEEDER_TEXT)
        assert b["master"] is True
        assert [r["name"] for r in b["rows"]] == [
            "Taiwan Semiconductor Manufacturing Co Ltd", "Samsung Electronics Co Ltd", "SK hynix Inc"]
        assert parse_master_isin(FEEDER_TEXT) == "LU0943624824"

    def test_country_rows_are_not_holdings(self):
        from app.services.factsheet_service import parse_top_holdings
        assert parse_top_holdings(ALLOCATION_ONLY) is None

    def test_truncated_master_fund_unit_counts_as_fund_units(self):
        from app.services.factsheet_service import parse_top_holdings
        text = "ข้อมูลกองทุนหลัก 5 อันดับแรก\n      Series PLC-Jupiter Gold and        97.14\n"
        assert parse_top_holdings(text)["fund_units_only"] is True

    def test_thai_stock_names_and_industry_table_skipped(self):
        from app.services.factsheet_service import parse_top_holdings
        text = ("การจัดสรรการลงทุนในกลุ่มอุตสาหกรรม 5 อันดับแรก\n"
                "   พาณิชย์                                     12.68\n"
                "   ธนาคาร                                      10.10\n"
                "                                              ทรัพย์สินที่ลงทุน 5 อันดับแรก\n"
                "   ค่าธรรมเนียม                                บมจ.เดลต้า อีเลคโทรนิคส์ (ประเทศไทย)            9.97\n"
                "   ขาย)                                        บมจ.ปตท.                                        5.22\n")
        b = parse_top_holdings(text)
        assert [r["name"] for r in b["rows"]] == ["บมจ.เดลต้า อีเลคโทรนิคส์ (ประเทศไทย)", "บมจ.ปตท."]
        assert b.get("fund_units_only") is None

    def test_left_column_value_glued_to_name(self):
        from app.services.factsheet_service import parse_top_holdings
        text = ("x 5 อันดับแรก\n"
                "                    96.19 NVIDIA CORP                              9.97\n"
                "ือหนี้สินอื่น       -0.14 APPLE INC                                9.83\n"
                "                    % Eli Lilly & Co.                              9.39%\n")
        assert [r["name"] for r in parse_top_holdings(text)["rows"]] == ["NVIDIA CORP", "APPLE INC", "Eli Lilly & Co."]

    def test_thai_fund_unit_row_is_a_fund_unit(self):
        from app.services.factsheet_service import parse_top_holdings
        text = "ทรัพย์สินที่ลงทุน 5 อันดับแรก\n   หน่วยลงทุน JANUS HENDERSON FUND        99.14\n"
        assert parse_top_holdings(text)["fund_units_only"] is True

    def test_garbled_thai_anchor(self):
        from app.services.factsheet_service import parse_top_holdings
        text = "ทรพยสนทลงทน 5 อนดบแรก\n      Eli Lilly & Co        9.16\n      AbbVie Inc       4.63\n"
        assert [r["name"] for r in parse_top_holdings(text)["rows"]] == ["Eli Lilly & Co", "AbbVie Inc"]


@pytest.mark.asyncio
async def test_nav_backfill_skip_covered_is_resumable(db):
    from app.services import sync_service
    from app.models.fund import NavHistory
    db.add_all([Fund(fund_code="DONE", sec_proj_id="P_DONE", fund_status="RG"),
                Fund(fund_code="GAP", sec_proj_id="P_GAP", fund_status="RG")])
    for d in (date(2025, 10, 1), date(2026, 10, 14)):
        db.add(NavHistory(fund_code="DONE", trade_date=d, nav=Decimal("10")))
    db.add(NavHistory(fund_code="GAP", trade_date=date(2026, 10, 14), nav=Decimal("10")))   # no early history
    await db.commit()
    fetch = AsyncMock(return_value=[])
    with patch("app.services.sync_service.settings") as s, \
            patch("app.services.sec_api.get_nav_range", new=fetch):
        s.SEC_API_KEY = "k"
        await sync_service.sync_nav_range(db, date(2025, 10, 1), date(2026, 10, 15), skip_covered=True)
    assert [c.args[1] for c in fetch.await_args_list] == ["P_GAP"]
