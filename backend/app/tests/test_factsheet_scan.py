"""Chunk 1: market-wide factsheet scan — parsing, canonical AIMC categories, resumability."""
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.fund import Fund
from app.models.market import FundFactsheet
from app.models.tax_lot import SyncJob
from app.services import factsheet_service as fs
from app.services.aimc import AIMC_CATEGORIES, canonical_category
from app.services.sec_api import SecApiError


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


class TestAimcCategories:
    def test_official_list_has_45(self):
        # 44 from the AIMC Category Performance Report + Miscellaneous (official, omitted there)
        assert len(AIMC_CATEGORIES) == 45 == len(set(AIMC_CATEGORIES))

    def test_aliases_for_seen_misspellings(self):
        assert canonical_category("Miscelleneous") == "Miscellaneous"
        assert canonical_category("Global Bond Discretionary F/X Hedge or Unhedge") == \
            "Global Bond Discretionary F/X Hedge or Unhedge"
        assert canonical_category("Emerging Market Bond Discretionary F/X Hedge or Unhedged") == \
            "Emerging Market Bond Discretionary F/X Hedge or Unhedge"

    def test_canonical_tolerates_spacing_case_and_ligatures(self):
        assert canonical_category("Asia Paciﬁc Ex Japan") == "Asia Pacific Ex Japan"
        assert canonical_category("technology  equity") == "Technology Equity"
        assert canonical_category("Global Equity - Alternative Energy") == "Global Equity - Alternative Energy"
        assert canonical_category("Fund of Property Fund -Thai and Foreign") == "Fund of Property fund -Thai and Foreign"

    def test_unknown_label_is_none(self):
        assert canonical_category("Crypto Moonshots") is None
        assert canonical_category(None) is None


class TestParsing:
    @pytest.mark.parametrize("line,expected", [
        ("• กลุม US Equity                       ลงทุนในตราสารทุน", "US Equity"),
        ("  กลุ่ม Greater China Equity          ของ NAV", "Greater China Equity"),
        ("• กลุ่มกองทนรวม: Health Care                    7 : เสยงสง", "Health Care"),
        ("        กลุม Asia Pacific Ex Japan                      *ความเสี่ยง", "Asia Pacific Ex Japan"),
        ("• กลุมกองทุน : Technology Equity", "Technology Equity"),
    ])
    def test_aimc_category_layouts(self, line, expected):
        assert fs.parse_aimc_category("ประเภทกองทุนรวม / กลุ่มกองทุนรวม\n" + line + "\n") == expected

    def test_aimc_only_from_page_header(self):
        body = "\n" * 60 + "  กลุ่ม Technology Equity   x\n"
        assert fs.parse_aimc_category(body) is None

    def test_fx_hedge_pct(self):
        assert fs.parse_fx_hedge_pct("  FX Hedging  75.96 %  ") == Decimal("75.96")
        assert fs.parse_fx_hedge_pct("FX Hedging 0.00%") == Decimal("0.00")
        assert fs.parse_fx_hedge_pct("การป้องกันความเสี่ยง FX ไม่ป้องกัน") is None

    def test_apply_parsed_sets_raw_and_canonical(self):
        f = FundFactsheet(sec_proj_id="P1")
        fs.apply_parsed(f, "x\n• กลุม Technology Equity    y\nFX Hedging 12.5 %\n")
        assert (f.aimc_category_raw, f.aimc_category, f.fx_hedge_pct) == (
            "Technology Equity", "Technology Equity", Decimal("12.5"))


PAGE = "ประเภท\n• กลุม Japan Equity    x\n"


async def _seed(db, *projs, status="RG"):
    for p in projs:
        db.add(Fund(fund_code=f"F-{p}", sec_proj_id=p, fund_status=status))
    await db.commit()


def _patches(latest=None, text=PAGE, latest_side=None):
    return (patch("app.services.factsheet_service._latest_url",
                  new=AsyncMock(side_effect=latest_side) if latest_side else AsyncMock(return_value=latest)),
            patch("app.services.factsheet_service._download_text", new=AsyncMock(return_value=text)),
            patch("app.services.factsheet_service.asyncio.sleep", new=AsyncMock()))


class TestScan:

    @pytest.mark.asyncio
    async def test_scans_active_projects_once_and_resumes(self, db):
        await _seed(db, "P1", "P2")
        await _seed(db, "PX", status="LI")                       # liquidated: not in the universe
        a, b, c = _patches(latest=("https://pdf", date(2026, 9, 30)))
        with a, b, c:
            first = await fs.scan_factsheets(db)
            second = await fs.scan_factsheets(db)
        assert first == {"scanned": 2, "ok": 2, "no_factsheet": 0, "error": 0, "reparse": {}}
        assert second["scanned"] == 0                              # checkpointed — nothing re-downloaded
        row = await db.get(FundFactsheet, "P1")
        assert (row.aimc_category, row.as_of, row.scan_status) == ("Japan Equity", date(2026, 9, 30), "ok")
        assert await db.get(FundFactsheet, "PX") is None

    @pytest.mark.asyncio
    async def test_errors_are_recorded_and_retried(self, db):
        await _seed(db, "P1", "P2")
        side = [SecApiError("HTTP 503"), None]                      # P1 fails, P2 has no factsheet
        a, b, c = _patches(latest_side=side)
        with a, b, c:
            r = await fs.scan_factsheets(db)
        assert (r["error"], r["no_factsheet"]) == (1, 1)
        assert "503" in (await db.get(FundFactsheet, "P1")).scan_error

        a, b, c = _patches(latest=("https://pdf", None))
        with a, b, c:
            retry = await fs.scan_factsheets(db)
        assert retry["scanned"] == 1 and retry["ok"] == 1            # only the error row is retried
        assert (await db.get(FundFactsheet, "P1")).scan_error is None

    @pytest.mark.asyncio
    async def test_limit_and_force(self, db):
        await _seed(db, "P1", "P2", "P3")
        a, b, c = _patches(latest=("https://pdf", None))
        with a, b, c:
            assert (await fs.scan_factsheets(db, limit=2))["scanned"] == 2
            assert (await fs.scan_factsheets(db))["scanned"] == 1
            assert (await fs.scan_factsheets(db, force=True))["scanned"] == 3

    @pytest.mark.asyncio
    async def test_running_scan_blocks_a_second_one(self, db):
        await _seed(db, "P1")
        db.add(SyncJob(type="factsheet_scan", started_at=datetime.now(timezone.utc), status="running"))
        await db.commit()
        a, b, c = _patches(latest=("https://pdf", None))
        with a, b, c:
            r = await fs.scan_factsheets(db)
        assert "another factsheet scan is running" in r["skipped"]
        assert await db.get(FundFactsheet, "P1") is None


@pytest.mark.asyncio
async def test_metadata_sync_stores_fx_policy_for_every_fund(db):
    from app.services import sync_service
    funds = [{"proj_id": "P1", "proj_abbr_name": "ANYFUND", "fund_status": "Registered",
              "exchange_rate_protection_policy": "ไม่ป้องกัน (unhedged)"}]
    with patch("app.services.sync_service.settings") as s, \
            patch("app.services.sec_api.list_amcs", new_callable=AsyncMock, return_value=[{"unique_id": "A"}]), \
            patch("app.services.sec_api.list_amc_funds", new_callable=AsyncMock, return_value=funds), \
            patch("app.services.sec_api.get_fund_policy", new_callable=AsyncMock, return_value=None), \
            patch("app.services.sec_api.get_fund_performance", new_callable=AsyncMock, return_value=[]):
        s.factsheet_key = s.SEC_API_KEY = "k"
        await sync_service.sync_fund_metadata(db)
    assert (await db.get(Fund, "ANYFUND")).fx_hedge == "ไม่ป้องกัน (unhedged)"


class TestHoldingsIssue:
    FILL = "x" * 300 + "\n"

    def test_reasons(self):
        h = fs.holdings_issue
        assert h("   ", None) == "no_text"
        assert h(self.FILL + "ทรัพย์สินที่ลงทุน 5 อันดับแรก\n  (chart)\n", None) == "no_rows"
        assert h(self.FILL + "" * 25, None) == "garbled_thai"
        assert h(self.FILL + "no holdings section here", None) == "no_heading"
        assert h(self.FILL, {"rows": [], "master": False, "fund_units_only": True}) == "fund_units"
        assert h(self.FILL, {"rows": [{"name": "X Inc", "pct": 5}], "master": True}) is None


@pytest.mark.asyncio
async def test_scan_can_be_restricted_to_projects(db):
    await _seed(db, "P1", "P2", "P3")
    a, b, c = _patches(latest=("https://pdf", None))
    with a, b, c:
        r = await fs.scan_factsheets(db, force=True, proj_ids={"P2"})
    assert r["scanned"] == 1
    assert await db.get(FundFactsheet, "P1") is None and await db.get(FundFactsheet, "P2") is not None


# ── regression: real factsheet excerpts of Thai-named feeders ────────────────
# Fund-unit rows written in Thai ("หน่วยลงทุน กองทุนเปิด…", "กองทุนเปิดเค เวียดนาม หุ้นทุน")
# must stay fund units (≥50% NAV / fund-unit name), never become stock names, and
# deposits / depositary receipts must not become holdings.

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "factsheets"
_UNIT_WORDS = ("กองทุน", "กองทน", "หน่วยลงทุน", "หนวยลงทน", "Portfolio", "ETF", "เงินฝาก", "ใบแสดงสิทธิ")


def _rows(name):
    text = (FIXTURES / f"{name}.txt").read_text()
    parsed = fs.parse_top_holdings(text)
    return parsed, fs.holdings_issue(text, parsed)


@pytest.mark.parametrize("name", ["scbrmviet", "k_vietnamrmf", "k_india", "scbrmmoney", "scbrmmlca"])
def test_thai_named_feeder_units_never_become_holdings(name):
    parsed, _ = _rows(name)
    for r in parsed["rows"]:
        assert r["pct"] < 50, r
        assert not any(w in r["name"] for w in _UNIT_WORDS), r


def test_k_vietnamrmf_master_top5_without_own_depositary_receipt():
    parsed, issue = _rows("k_vietnamrmf")
    names = [r["name"] for r in parsed["rows"]]
    assert issue is None and parsed["master"]
    assert "E1VFVN30" not in " ".join(names)                     # fund's own DR, from the other table
    assert names[0] == "Mobile World Investment Corp"


def test_k_india_deposit_is_not_a_holding():
    parsed, _ = _rows("k_india")
    names = [r["name"] for r in parsed["rows"]]
    assert not any("Bank of New York Mellon" in n for n in names)
    assert names[:2] == ["ICICI Bank Ltd", "HDFC Bank Ltd"]


def test_scbrmviet_master_holdings():
    parsed, issue = _rows("scbrmviet")
    assert issue is None
    assert [r["name"] for r in parsed["rows"]][:2] == ["VINGROUP JSC", "VIETNAM TECHNOLOGICAL AND"]


def test_scbrmmoney_bills_and_bonds_are_holdings_not_fund_units():
    parsed, issue = _rows("scbrmmoney")
    assert issue is None                                          # garbled Thai bill/bond names still count
    assert any("FRB" in r["name"] for r in parsed["rows"])


# ── re-parse must never lose data ─────────────────────────────────────────────

GOOD = ("x\n• กลุม Technology Equity    y\nFX Hedging 80.00 %\n"
        "ทรัพย์สินที่ลงทุน 5 อันดับแรกของกองทุนหลัก\n   NVIDIA CORP          11.31\n   BROADCOM INC        10.22\n")
WORSE = "x\nnothing parseable here\n"


class TestReparseKeepsBetterData:

    @pytest.mark.asyncio
    async def test_same_document_worse_result_keeps_stored_and_is_counted(self, db):
        await _seed(db, "P1")
        a, b, c = _patches(latest=("https://pdf", date(2026, 9, 30)), text=GOOD)
        with a, b, c:
            await fs.scan_factsheets(db)
        a, b, c = _patches(latest=("https://pdf", date(2026, 9, 30)), text=WORSE)
        with a, b, c:
            r = await fs.scan_factsheets(db, force=True)
        assert r["reparse"]["holdings"] == {"better": 0, "same": 0, "worse": 1}
        assert r["reparse"]["aimc_category"]["worse"] == 1 and r["reparse"]["fx_hedge_pct"]["worse"] == 1
        row = await db.get(FundFactsheet, "P1")
        assert [h["name"] for h in row.holdings] == ["NVIDIA CORP", "BROADCOM INC"]
        assert (row.aimc_category, row.fx_hedge_pct) == ("Technology Equity", Decimal("80.00"))

    @pytest.mark.asyncio
    async def test_same_document_better_result_is_taken(self, db):
        await _seed(db, "P1")
        a, b, c = _patches(latest=("https://pdf", date(2026, 9, 30)), text=WORSE)
        with a, b, c:
            await fs.scan_factsheets(db)
        a, b, c = _patches(latest=("https://pdf", date(2026, 9, 30)), text=GOOD)
        with a, b, c:
            r = await fs.scan_factsheets(db, force=True)
        assert r["reparse"]["holdings"] == {"better": 1, "same": 0, "worse": 0}
        assert (await db.get(FundFactsheet, "P1")).holdings_scope == "master"

    @pytest.mark.asyncio
    async def test_newer_document_replaces_holdings_but_keeps_category(self, db):
        await _seed(db, "P1")
        a, b, c = _patches(latest=("https://pdf", date(2026, 8, 31)), text=GOOD)
        with a, b, c:
            await fs.scan_factsheets(db)
        a, b, c = _patches(latest=("https://pdf2", date(2026, 9, 30)), text=WORSE)
        with a, b, c:
            r = await fs.scan_factsheets(db, force=True)
        assert r["reparse"] == {}                                      # not a re-parse of the same document
        row = await db.get(FundFactsheet, "P1")
        assert row.holdings is None and row.as_of == date(2026, 9, 30)   # newer factsheet wins for holdings
        assert row.aimc_category == "Technology Equity"                  # category kept when the new one is blank
        assert row.fx_hedge_pct is None                                  # hedge % is per-document: no stale value

    @pytest.mark.asyncio
    async def test_failed_redownload_keeps_parsed_row(self, db):
        await _seed(db, "P1")
        a, b, c = _patches(latest=("https://pdf", None), text=GOOD)
        with a, b, c:
            await fs.scan_factsheets(db)
        a, b, c = _patches(latest_side=[SecApiError("HTTP 503")])
        with a, b, c:
            r = await fs.scan_factsheets(db, force=True)
        assert r["error"] == 1
        row = await db.get(FundFactsheet, "P1")
        assert row.scan_status == "ok" and row.holdings and "503" in row.scan_error
