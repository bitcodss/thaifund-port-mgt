"""Chunk 5: report facts — flags, one watchlist entry per holding, driver month."""
import uuid
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.fund import Fund, NavHistory
from app.models.market import PeerSnapshot
from app.models.portfolio import Portfolio
from app.models.tax_lot import TaxLot
from app.models.user import User
from app.services import peer_metrics as pm
from app.services import report_facts as rf

SEP, AUG, JUL, JUN = date(2026, 9, 30), date(2026, 8, 31), date(2026, 7, 31), date(2026, 6, 30)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _snap(month, pct=None, eligible=True, ranks=None, small=False, months=None, backfilled=True):
    return PeerSnapshot(sec_proj_id="P1", month=month, basis="theme" if eligible else "category",
                        flag_eligible=eligible, pct_3m=None if pct is None else Decimal(str(pct)),
                        ret_3m=Decimal("-0.2"), backfilled=backfilled,
                        details={"ranks": {"3m": ranks} if ranks else {}, "small_group": small,
                                 "months_in_3m": months or {}})


class TestBottomRule:

    def test_percentile_and_small_group_last_place(self):
        assert rf.is_bottom(_snap(SEP, pct=11))
        assert not rf.is_bottom(_snap(SEP, pct=30))
        assert not rf.is_bottom(_snap(SEP, pct=0, eligible=False))          # category basis: context only
        assert rf.is_bottom(_snap(SEP, pct=20, ranks=[5, 5], small=True))
        assert not rf.is_bottom(_snap(SEP, pct=0, ranks=[4, 5], small=True))  # small group: last place only

    def test_driver_month(self):
        s = _snap(SEP, months={"2026-09-30": 0.096, "2026-08-31": 0.052, "2026-07-31": -0.338})
        assert rf.driver_month(s)["month"] == "2026-07-31"


@pytest.mark.asyncio
async def test_watchlist_one_entry_with_all_reasons_and_single_event(db):
    user = User(id=uuid.uuid4(), email="u@x", password_hash="x", role="user")
    port = Portfolio(id=uuid.uuid4(), user_id=user.id, name="Main")
    db.add_all([user, port, Fund(fund_code="ASP", sec_proj_id="P1", fund_status="RG", name_en="AI fund")])
    db.add(TaxLot(portfolio_id=port.id, fund_code="ASP", original_purchase_date=date(2024, 1, 2),
                  units_remaining=Decimal("100"), cost_basis_remaining=Decimal("2000"), tax_scheme="RMF"))
    m, v = date(2024, 1, 31), 20.0
    while m <= JUN:                                                    # calm history: ±1% a month
        db.add(NavHistory(fund_code="ASP", trade_date=m, nav=Decimal(str(round(v, 4)))))
        v *= 1.01 if m.month % 2 else 0.99
        m = pm.months_back(m, -1)
    for d, nav in ((JUL, v * 0.66), (AUG, v * 0.66 * 1.05), (SEP, v * 0.66 * 1.05 * 0.98)):
        db.add(NavHistory(fund_code="ASP", trade_date=d, nav=Decimal(str(round(nav, 4)))))
    jul_window = {"2026-07-31": -0.34, "2026-06-30": 0.01, "2026-05-31": -0.01}
    db.add(_snap(AUG, pct=13, months={"2026-08-31": 0.05, **jul_window}))
    db.add(_snap(SEP, pct=11, months={"2026-09-30": -0.02, "2026-08-31": 0.05, "2026-07-31": -0.34}))
    await db.commit()

    facts = await rf.portfolio_facts(db, port.id, SEP)
    assert len(facts["watchlist"]) == 1                                   # one entry, several reasons
    entry = facts["watchlist"][0]
    assert entry["fund_code"] == "ASP" and set(entry["reasons"]) == {"abnormal_3m", "bottom_3m", "loss_10pct"}
    bottom = next(f for f in entry["flags"] if f["rule"] == "bottom_3m")
    assert bottom["driver"]["month"] == "2026-07-31" and bottom["single_event"] is True
    assert bottom["uses_backfilled_snapshot"] is True
    fund = facts["funds"][0]
    assert fund["pnl_pct"] < -0.10 and fund["weight"] == 1.0 and fund["peers"]["display"] == "percentile"


@pytest.mark.asyncio
async def test_loss_flag_measures_from_switch_in_not_tax_lot_cost(db):
    """Old fund +20%, switch, new fund −25%: lot cost shows −10%, this fund cost us −25%."""
    from app.models.tax_lot import LotConsumption
    from app.models.transaction import Transaction
    user = User(id=uuid.uuid4(), email="s@x", password_hash="x", role="user")
    port = Portfolio(id=uuid.uuid4(), user_id=user.id, name="RMF")
    db.add_all([user, port, Fund(fund_code="OLD", fund_status="RG"), Fund(fund_code="NEW", fund_status="RG")])
    src = TaxLot(id=uuid.uuid4(), portfolio_id=port.id, fund_code="OLD", original_purchase_date=date(2023, 1, 5),
                 units_remaining=Decimal("0"), cost_basis_remaining=Decimal("0"), tax_scheme="RMF")
    new = TaxLot(id=uuid.uuid4(), portfolio_id=port.id, fund_code="NEW", original_purchase_date=date(2023, 1, 5),
                 units_remaining=Decimal("120"), cost_basis_remaining=Decimal("1000"), tax_scheme="RMF",
                 source_lot_id=src.id)
    out_tx = Transaction(id=uuid.uuid4(), portfolio_id=port.id, date=date(2026, 3, 2), type="SWITCH_OUT",
                         fund_code="OLD", units=Decimal("100"), nav=Decimal("12"), amount=Decimal("1200"),
                         pair_id="sw1", tax_scheme="RMF")
    in_tx = Transaction(id=uuid.uuid4(), portfolio_id=port.id, date=date(2026, 3, 2), type="SWITCH_IN",
                        fund_code="NEW", units=Decimal("120"), nav=Decimal("10"), amount=Decimal("1200"),
                        pair_id="sw1", tax_scheme="RMF")
    db.add_all([src, new, out_tx, in_tx,
                LotConsumption(transaction_id=out_tx.id, lot_id=src.id, units_consumed=Decimal("100"),
                               cost_basis_consumed=Decimal("1000"))])
    db.add(NavHistory(fund_code="NEW", trade_date=SEP, nav=Decimal("7.5")))      # −25% since switch-in
    await db.commit()

    facts = await rf.portfolio_facts(db, port.id, SEP)
    fund = facts["funds"][0]
    assert fund["pnl_pct"] == pytest.approx(-0.25) and fund["entry_basis"] == ["switch_in"]
    assert fund["pnl_lot_cost_pct"] == pytest.approx(-0.10)
    assert fund["entry_dates"] == ["2026-03-02"]
    assert facts["watchlist"][0]["reasons"] == ["loss_10pct"]
    lot = fund["tax_lots"][0]
    assert lot["scheme"] == "RMF" and lot["original_purchase_date"] == "2023-01-05" and lot["eligible"] is False
    assert "RMF" in facts["tax_rules"] and any("transfer" in n for n in facts["tax_rules"]["RMF"]["notes"])
