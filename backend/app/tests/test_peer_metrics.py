"""Chunk 4: monthly peer metrics — percentile, hedge handling, FX, backfill, own-history z."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.fund import Fund, NavHistory
from app.models.market import FundFactsheet, FxRate, PeerProfile, PeerSnapshot
from app.services import peer_metrics as pm
from app.services.factsheet_service import parse_fx_hedge_policy


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


SEP, AUG, JUL = date(2026, 9, 30), date(2026, 8, 31), date(2026, 7, 31)


def _nav(db, code, values: dict[date, float]):
    for d, v in values.items():
        db.add(NavHistory(fund_code=code, trade_date=d, nav=Decimal(str(v))))


async def _fund(db, pid, theme, master, hedge="unhedged", aug=10.0, sep=10.0, cat="Technology Equity", status="agreed"):
    db.add(Fund(fund_code=pid, sec_proj_id=pid, fund_status="RG"))
    db.add(PeerProfile(sec_proj_id=pid, aimc_category=cat, aimc_source="parsed", peer_theme=theme,
                       theme_status=status, master_key=master, master_key_kind="isin",
                       representative_code=pid, hedge_group=hedge))
    _nav(db, pid, {JUL: 10.0, AUG: aug, SEP: sep})


class TestHelpers:

    def test_percentile_and_ties(self):
        assert pm.percentile(5, [1, 2, 3, 4]) == 100
        assert pm.percentile(0, [1, 2, 3, 4]) == 0
        assert pm.percentile(2, [1, 2, 3, 4]) == 37.5

    def test_hedge_ratio_actual_estimated_domestic(self):
        assert pm.hedge_ratio("dynamic", Decimal("76.62")) == (0.7662, False)
        assert pm.hedge_ratio("unhedged", None) == (0.0, True)
        assert pm.hedge_ratio("domestic", None) == (1.0, False)

    def test_month_arithmetic(self):
        assert pm.months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)
        assert pm.months_back(date(2026, 1, 31), 3) == date(2025, 10, 31)
        assert pm.last_complete_month(date(2026, 10, 17)) == SEP

    def test_stale_month_end_is_missing(self):
        me = pm.MonthEnds([(date(2026, 9, 20), 10.0), (AUG, 9.0)])
        assert me.at(SEP) is None and me.ret(SEP, 1) is None


class TestHedgePolicyParse:

    @pytest.mark.parametrize("text,expected", [
        ("การป้องกันความเสี่ยง FX             ไม่ป้องกัน (unhedged)", "unhedged"),
        ("การปองกันความเสี่ยง FX             ไมปองกัน", "unhedged"),               # marks dropped (MEGA10)
        ("กํารป้องกันควํามเสี่ยง FX           ป้องกันทั้งหมด", "full"),            # stray ํ (UGREF)
        ("การป้องกันความเสี่ยง                ตามดุลพินจิ ของผู้จัดการกองทุน", "dynamic"),
        ("กองทุนไมปองกันความเสี่ยงจากอัตราแลกเปลี่ยน จึงอาจมีความเสี่ยง", None),  # prose, not the label
        ("การป้องกันความเสี่ยง  ตามดุลพินิจ\nการป้องกันความเสี่ยง  ไม่ป้องกัน (unhedged)", None),  # classes differ
        # MEGA10: marks as private-use glyphs
        ("Peer avg        การป\uf706องกันความเสี่ยง FX ไม\uf70aป\uf706องกัน (unhedged)", "unhedged"),
        # KT-GEQ: letters spaced out, "ด้าน" inserted, value spaced out too
        ("ทมตวแปรเป็นอตราแลก    การป\uf706 อ งก ั น ความเส ี่ ย งด\uf70b า น FX       ไม\uf70a ป \uf706 อ งก ั น", "unhedged"),
    ])
    def test_label_values(self, text, expected):
        assert parse_fx_hedge_policy(text) == expected


class TestSnapshots:

    @pytest.mark.asyncio
    async def test_percentile_among_other_masters_same_hedge(self, db):
        await _fund(db, "ME", "AI", "M0", sep=11.0)                    # +10% in Sep
        for i, r in enumerate([0.0, 0.05, 0.15, 0.20], start=1):      # 4 other masters
            await _fund(db, f"P{i}", "AI", f"M{i}", sep=10 * (1 + r))
        await _fund(db, "ME2", "AI", "M0", sep=10.4)                   # other feeder of MY master: not a peer
        await db.commit()
        r = await pm.build_snapshots(db, [SEP], backfilled=False)
        s = (await db.execute(select(PeerSnapshot).where(PeerSnapshot.sec_proj_id == "ME"))).scalar_one()
        assert s.basis == "theme" and s.flag_eligible and s.hedge_basis == "same_hedge" and s.peer_masters == 4
        assert float(s.ret_1m) == pytest.approx(0.10) and float(s.pct_1m) == 50.0
        assert float(s.median_1m) == pytest.approx(0.10)
        same = s.details["same_master"]
        assert [f["sec_proj_id"] for f in same] == ["ME2"] and same[0]["ret_1m"] == pytest.approx(0.04)
        assert r["written"] >= 5

    @pytest.mark.asyncio
    async def test_small_same_hedge_subgroup_falls_back_to_fx_adjusted_theme(self, db):
        # ME unhedged; peers are fully hedged → only 1 master in my hedge group → FX-adjusted
        await _fund(db, "ME", "AI", "M0", sep=11.0)
        for i in range(1, 5):
            await _fund(db, f"P{i}", "AI", f"M{i}", hedge="full", sep=10.5)
        for d, v in ((JUL, 33.0), (AUG, 33.0), (SEP, 34.65)):          # USD/THB +5% in Sep (default ccy)
            db.add(FxRate(pair="USDTHB", trade_date=d, rate=Decimal(str(v))))
        await db.commit()
        await pm.build_snapshots(db, [SEP], backfilled=False)
        s = (await db.execute(select(PeerSnapshot).where(PeerSnapshot.sec_proj_id == "ME"))).scalar_one()
        assert s.hedge_basis == "fx_adjusted" and s.fx_adjusted_masters == 4
        # hedged peer +5% → as if unhedged: 0.05 + (1.0 − 0.0) × 0.05 = +10%
        assert float(s.median_1m) == pytest.approx(0.10)
        assert float(s.fx_effect_1m) == pytest.approx(0.05) and s.hedge_ratio_estimated

    @pytest.mark.asyncio
    async def test_category_basis_is_not_flag_eligible(self, db):
        await _fund(db, "ME", "Value", "M0", cat="Global Equity")
        for i in range(1, 6):
            await _fund(db, f"P{i}", "Growth", f"M{i}", cat="Global Equity")
        await db.commit()
        await pm.build_snapshots(db, [SEP], backfilled=False)
        s = (await db.execute(select(PeerSnapshot).where(PeerSnapshot.sec_proj_id == "ME"))).scalar_one()
        assert s.basis == "category" and s.flag_eligible is False

    @pytest.mark.asyncio
    async def test_backfill_never_overwrites_live_snapshot(self, db):
        await _fund(db, "ME", "AI", "M0", sep=11.0)
        await db.commit()
        await pm.build_snapshots(db, [SEP], backfilled=False)
        r = await pm.build_snapshots(db, [AUG, SEP], backfilled=True)
        rows = {s.month: s.backfilled for s in (await db.execute(select(PeerSnapshot))).scalars()}
        assert rows == {SEP: False, AUG: True} and r["kept_live"] == 1

    @pytest.mark.asyncio
    async def test_nav_code_case_insensitive_and_fallback_class(self, db):
        db.add(Fund(fund_code="TSP4-EXPLORER", sec_proj_id="A", fund_status="RG"))
        db.add(PeerProfile(sec_proj_id="A", aimc_category="X", aimc_source="parsed", representative_code="tsp4-explorer"))
        db.add(Fund(fund_code="LHSELECT-D", sec_proj_id="B", fund_status="RG"))
        db.add(PeerProfile(sec_proj_id="B", aimc_category="X", aimc_source="parsed", representative_code="LHSELECT-A"))
        _nav(db, "TSP4-EXPLORER", {SEP: 1.0})
        _nav(db, "LHSELECT-D", {SEP: 1.0})
        await db.commit()
        codes = await pm.nav_codes(db, (await db.execute(select(PeerProfile))).scalars().all())
        assert codes == {"A": ("TSP4-EXPLORER", False), "B": ("LHSELECT-D", True)}

    def test_own_history_zscore(self):
        months, m = [], date(2025, 1, 31)
        while m <= SEP:
            months.append(m)
            m = pm.months_back(m, -1)
        navs, v = [], 10.0
        for i, m in enumerate(months[:-1]):
            v *= 1.01 if i % 2 else 0.99
            navs.append((m, v))
        navs.append((SEP, v * 0.85))                                    # −15% month
        me = pm.MonthEnds(navs)
        hist = [r for mm in months[:-1] if (r := me.ret(mm, 1)) is not None]
        assert pm.zscore(me.ret(SEP, 1), hist) < -2


class TestCurrencyAndSmallGroups:

    @pytest.mark.parametrize("policy,master,cat,expected", [
        ("ชนิดหน่วยลงทุน I Shares (Acc.) สกุลเงินเยน (JPY) กองทุนหลัก", None, "Japan Equity", ("JPY", "policy")),
        ("ลงทุนใน share class สกุลเงินดอลลาร์สหรัฐ", None, "Japan Equity", ("USD", "policy")),
        ("ชนิดหน่วยลงทุนสกุลเงินยูโร", None, "Global Equity", ("EUR", "policy")),
        ("", "JPMorgan Pacific Technology - Class C (acc) - USD", "Technology Equity", ("USD", "master_name")),
        ("", "Eleva European Selection Fund", "European Equity", ("EUR", "region")),
        ("", "Some Fund", "Japan Equity", ("JPY", "region")),
        ("", None, "Technology Equity", ("USD", "default")),
    ])
    def test_fund_currency(self, policy, master, cat, expected):
        p = PeerProfile(sec_proj_id="X", aimc_category=cat, classifier_input={"policy": policy, "master_fund": master})
        assert pm.fund_currency(p) == expected

    def test_rank(self):
        assert pm.rank(0.05, [0.1, 0.0, 0.2, -0.1]) == (3, 5)

    @pytest.mark.asyncio
    async def test_small_group_records_rank_and_peer_currency_used_for_fx_adjustment(self, db):
        await _fund(db, "ME", "Growth", "M0", cat="Japan Equity", sep=10.0)
        for i in range(1, 5):
            await _fund(db, f"P{i}", "Growth", f"M{i}", cat="Japan Equity", hedge="full", sep=10.0)
        for pair, sep in (("USDTHB", 34.65), ("JPYTHB", 0.209)):        # USD +5%, JPY −5%
            for d, v in ((JUL, 33.0 if pair == "USDTHB" else 0.22), (AUG, 33.0 if pair == "USDTHB" else 0.22), (SEP, sep)):
                db.add(FxRate(pair=pair, trade_date=d, rate=Decimal(str(v))))
        await db.commit()
        await pm.build_snapshots(db, [SEP], backfilled=False)
        s = (await db.execute(select(PeerSnapshot).where(PeerSnapshot.sec_proj_id == "ME"))).scalar_one()
        # hedged peers flat → as if unhedged in YEN terms: 0 + (1 − 0) × (−5%) = −5%; I'm flat → best of 5
        assert float(s.median_1m) == pytest.approx(-0.05)
        assert s.details["ranks"]["1m"] == [1, 5] and s.details["small_group"] is True
        assert s.details["currency"] == "JPY" and float(s.fx_effect_1m) == pytest.approx(-0.05)
        assert set(s.details["months_in_3m"]) == {"2026-09-30", "2026-08-31", "2026-07-31"}


@pytest.mark.asyncio
async def test_thin_fx_pair_uses_usd_cross_rate(db):
    from unittest.mock import AsyncMock, patch
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(300)]
    data = {"THB=X": [(d, 32.0) for d in days], "CNYTHB=X": [(days[-1], 5.0)], "CNY=X": [(d, 6.4) for d in days]}
    with patch("app.services.peer_metrics.FX_TICKERS", {"USD": "THB=X", "CNY": "CNYTHB=X"}), \
            patch("app.services.yahoo_client.history", new=AsyncMock(side_effect=lambda t, r: data[t])):
        out = await pm.sync_fx(db)
    assert out == {"USD": 300, "CNY": 300}
    rate = (await db.execute(select(FxRate.rate).where(FxRate.pair == "CNYTHB"))).scalars().first()
    assert float(rate) == pytest.approx(5.0)                             # 32 / 6.4


@pytest.mark.parametrize("policy,master,cat,expected", [
    # real SEC policy fragments of held funds (found beyond the 1,200-char classifier cut)
    ("Share Class I1USD ISIN LU1985812756 สกุลเงิน ดอลลาร์สหรัฐ (U.S. dollars)", None, "Global Equity", ("USD", "policy")),
    ("ลงทุนในรูปของเงินสกุลดอลลาร์สหรัฐฯ ส่วนที่เหลือ", None, "Emerging Market", ("USD", "policy")),
    ("MSCI World Value TR Net EUR Index ticker MXWO000V Index สกุลเงิน ยูโร (EUR)", None, "Other", ("EUR", "policy")),
    ("ในชนิดหน่วยลงทุน (share class) “Class C (USD)” โดยเฉลี่ย", None, "Global Equity - Alternative Energy", ("USD", "policy")),
    ("โดยจะลงทุนในสกุลเงินดอลล่าร์ฮ่องกง (HKD)", None, "Asia Pacific Ex Japan", ("HKD", "policy")),
    ("ลงทุนในตราสารทุนที่จดทะเบียนซื้อขายในตลาดหลักทรัพย์ของประเทศสหรัฐอเมริกา", None, "US Equity", ("USD", "listing")),
    ("เปลี่ยนแปลงสกุลเงินลงทุนเป็นสกุลเงินอื่นใดนอกเหนือจากสกุลเงินบาท", None, "Greater China Equity", ("HKD", "region")),
])
def test_currency_from_full_policy_text(policy, master, cat, expected):
    from app.services.currency import currency_from_text
    assert currency_from_text(policy, master, cat) == expected
