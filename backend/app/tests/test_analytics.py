"""
Analytics service tests — XIRR calculations, P&L, tax eligibility.
DB tests use SQLite in-memory; XIRR/performance tests are pure Python.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.database import Base
from app.models.fund import Fund, NavHistory
from app.models.tax_lot import TaxLot, LotConsumption, TaxSchemeRule
from app.models.transaction import Transaction
from app.models.portfolio import Portfolio
from app.models.user import User
from app.services import portfolio_service as ps
from app.services.portfolio_service import _xirr_solve


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


async def _seed_basic(db: AsyncSession):
    """Create user → portfolio → fund → lots."""
    user = User(
        id=uuid.uuid4(),
        email="test@test.com",
        password_hash="x",
        role="user",
        date_of_birth=date(1970, 1, 1),
    )
    db.add(user)
    portfolio = Portfolio(id=uuid.uuid4(), user_id=user.id, name="Test")
    db.add(portfolio)
    fund = Fund(fund_code="TESTFUND")
    db.add(fund)
    await db.flush()
    return user, portfolio, fund


# ── XIRR unit tests (no DB needed) ────────────────────────────────────────────

class TestXirrSolve:

    def test_simple_one_year_return(self):
        """Invest 100 today, receive 110 in 1 year → ~10% XIRR."""
        t0 = date(2025, 1, 1)
        t1 = date(2026, 1, 1)
        cfs = [(t0, Decimal("-100")), (t1, Decimal("110"))]
        rate = _xirr_solve(cfs)
        assert abs(float(rate) - 0.1) < 0.001

    def test_two_year_return(self):
        """Invest 100, receive 121 in 2 years → ~10% XIRR."""
        t0 = date(2023, 1, 1)
        t2 = date(2025, 1, 1)
        cfs = [(t0, Decimal("-100")), (t2, Decimal("121"))]
        rate = _xirr_solve(cfs)
        assert abs(float(rate) - 0.1) < 0.001

    def test_monthly_dca_converges(self):
        """12 monthly investments of 1000, final value 13200 → positive XIRR."""
        cfs = []
        base = date(2024, 1, 1)
        for i in range(12):
            d = date(base.year, base.month, 1) if i == 0 else (
                date(base.year + (base.month + i - 1) // 12,
                     (base.month + i - 1) % 12 + 1, 1)
            )
            cfs.append((d, Decimal("-1000")))
        cfs.append((date(2025, 1, 1), Decimal("13200")))
        rate = _xirr_solve(cfs)
        assert float(rate) > 0

    def test_negative_return(self):
        """Invest 100, receive 80 → negative XIRR."""
        t0 = date(2024, 1, 1)
        t1 = date(2025, 1, 1)
        cfs = [(t0, Decimal("-100")), (t1, Decimal("80"))]
        rate = _xirr_solve(cfs)
        assert float(rate) < 0

    def test_zero_return(self):
        """Invest 100, get back exactly 100 → ~0% XIRR."""
        t0 = date(2024, 1, 1)
        t1 = date(2025, 1, 1)
        cfs = [(t0, Decimal("-100")), (t1, Decimal("100"))]
        rate = _xirr_solve(cfs)
        assert abs(float(rate)) < 0.001


# ── holdings tests ─────────────────────────────────────────────────────────────

class TestHoldings:

    @pytest.mark.asyncio
    async def test_empty_portfolio_returns_empty(self, db):
        _, portfolio, _ = await _seed_basic(db)
        result = await ps.get_holdings(portfolio.id, db)
        assert result == []

    @pytest.mark.asyncio
    async def test_single_lot_with_nav(self, db):
        _, portfolio, _ = await _seed_basic(db)
        db.add(TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=date(2024, 1, 1),
            units_remaining=Decimal("1000"), cost_basis_remaining=Decimal("12000"),
            tax_scheme="NORMAL",
        ))
        db.add(NavHistory(fund_code="TESTFUND", trade_date=date(2026, 1, 15), nav=Decimal("15.0")))
        await db.flush()

        holdings = await ps.get_holdings(portfolio.id, db)
        assert len(holdings) == 1
        h = holdings[0]
        assert h.fund_code == "TESTFUND"
        assert h.units == Decimal("1000")
        assert h.cost_basis == Decimal("12000")
        assert h.latest_nav == Decimal("15.0")
        assert h.market_value == Decimal("15000.00000000")
        assert h.unrealized_pnl == Decimal("3000.00000000")

    @pytest.mark.asyncio
    async def test_lot_without_nav_returns_none_value(self, db):
        _, portfolio, _ = await _seed_basic(db)
        db.add(TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=date(2024, 1, 1),
            units_remaining=Decimal("500"), cost_basis_remaining=Decimal("5000"),
            tax_scheme="NORMAL",
        ))
        await db.flush()

        holdings = await ps.get_holdings(portfolio.id, db)
        assert len(holdings) == 1
        assert holdings[0].market_value is None
        assert holdings[0].unrealized_pnl is None

    @pytest.mark.asyncio
    async def test_zero_units_lot_excluded(self, db):
        _, portfolio, _ = await _seed_basic(db)
        db.add(TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=date(2024, 1, 1),
            units_remaining=Decimal("0"), cost_basis_remaining=Decimal("0"),
            tax_scheme="NORMAL",
        ))
        await db.flush()

        holdings = await ps.get_holdings(portfolio.id, db)
        assert holdings == []

    @pytest.mark.asyncio
    async def test_same_fund_different_schemes_two_rows(self, db):
        _, portfolio, _ = await _seed_basic(db)
        for scheme in ("NORMAL", "SSF"):
            db.add(TaxLot(
                id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
                original_purchase_date=date(2024, 1, 1),
                units_remaining=Decimal("300"), cost_basis_remaining=Decimal("3000"),
                tax_scheme=scheme,
            ))
        await db.flush()

        holdings = await ps.get_holdings(portfolio.id, db)
        assert len(holdings) == 2
        schemes = {h.tax_scheme for h in holdings}
        assert schemes == {"NORMAL", "SSF"}

    @pytest.mark.asyncio
    async def test_latest_nav_picked_correctly(self, db):
        """Multiple NAV rows — latest date wins."""
        _, portfolio, _ = await _seed_basic(db)
        db.add(TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=date(2024, 1, 1),
            units_remaining=Decimal("100"), cost_basis_remaining=Decimal("1000"),
            tax_scheme="NORMAL",
        ))
        for nav, d in [("10.0", date(2026, 1, 10)), ("12.0", date(2026, 1, 15)), ("11.0", date(2026, 1, 13))]:
            db.add(NavHistory(fund_code="TESTFUND", trade_date=d, nav=Decimal(nav)))
        await db.flush()

        holdings = await ps.get_holdings(portfolio.id, db)
        assert holdings[0].latest_nav == Decimal("12.0")


# ── realized P&L tests ──────────────────────────────────────────────────────

class TestRealizedPnl:

    @pytest.mark.asyncio
    async def test_no_sell_returns_zero(self, db):
        _, portfolio, _ = await _seed_basic(db)
        result = await ps._realized_pnl(portfolio.id, db)
        assert result == Decimal("0")

    @pytest.mark.asyncio
    async def test_sell_with_gain(self, db):
        _, portfolio, _ = await _seed_basic(db)
        lot = TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=date(2024, 1, 1),
            units_remaining=Decimal("0"), cost_basis_remaining=Decimal("0"),
            tax_scheme="NORMAL",
        )
        db.add(lot)

        tx = Transaction(
            id=uuid.uuid4(), portfolio_id=portfolio.id,
            date=date(2025, 6, 1), type="SELL",
            amount=Decimal("13000"), fee=Decimal("50"), tax_withheld=Decimal("0"),
            tax_scheme="NORMAL",
        )
        db.add(tx)
        db.add(LotConsumption(
            id=uuid.uuid4(), transaction_id=tx.id, lot_id=lot.id,
            units_consumed=Decimal("1000"), cost_basis_consumed=Decimal("10000"),
        ))
        await db.flush()

        pnl = await ps._realized_pnl(portfolio.id, db)
        # proceeds = 13000 - 50 = 12950; cost = 10000; gain = 2950
        assert pnl == Decimal("2950.00000000")

    @pytest.mark.asyncio
    async def test_switch_out_not_counted(self, db):
        """SWITCH_OUT transactions must not appear in realized P&L."""
        _, portfolio, _ = await _seed_basic(db)
        db.add(Transaction(
            id=uuid.uuid4(), portfolio_id=portfolio.id,
            date=date(2025, 6, 1), type="SWITCH_OUT",
            amount=Decimal("5000"), fee=Decimal("0"), tax_withheld=Decimal("0"),
            tax_scheme="NORMAL",
        ))
        await db.flush()

        pnl = await ps._realized_pnl(portfolio.id, db)
        assert pnl == Decimal("0")


# ── tax eligibility tests ──────────────────────────────────────────────────────

class TestTaxEligibility:

    @pytest_asyncio.fixture
    async def setup(self, db):
        user, portfolio, fund = await _seed_basic(db)
        # Seed scheme rules
        for scheme, holding_years, age_req in [
            ("NORMAL", "0", None),
            ("SSF", "10", None),
            ("RMF", "5", 55),
            ("THAI_ESG", "5", None),
            ("THAI_ESG_EXTRA", "8", None),
            ("LTF", "5", None),
        ]:
            db.add(TaxSchemeRule(
                scheme=scheme,
                holding_years=Decimal(holding_years),
                age_requirement=age_req,
                active_from=date(2000, 1, 1),
            ))
        await db.flush()
        return user, portfolio, fund

    @pytest.mark.asyncio
    async def test_ssf_eligible_after_10_years(self, db, setup):
        user, portfolio, fund = setup
        purchase = date(2015, 1, 1)
        today = date(2025, 4, 1)  # > 10 years (3652.5 days)
        db.add(TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=purchase,
            units_remaining=Decimal("500"), cost_basis_remaining=Decimal("5000"),
            tax_scheme="SSF",
        ))
        await db.flush()

        lots = await ps.get_tax_eligibility(portfolio.id, db, today, user.date_of_birth)
        assert len(lots) == 1
        assert lots[0].is_eligible is True
        assert lots[0].days_remaining == 0

    @pytest.mark.asyncio
    async def test_ssf_not_eligible_early(self, db, setup):
        user, portfolio, fund = setup
        purchase = date(2023, 1, 1)
        today = date(2025, 1, 1)  # < 10 years
        db.add(TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=purchase,
            units_remaining=Decimal("500"), cost_basis_remaining=Decimal("5000"),
            tax_scheme="SSF",
        ))
        await db.flush()

        lots = await ps.get_tax_eligibility(portfolio.id, db, today, user.date_of_birth)
        assert lots[0].is_eligible is False
        assert lots[0].days_remaining > 0

    @pytest.mark.asyncio
    async def test_rmf_eligible_time_and_age(self, db, setup):
        user_dob = date(1970, 1, 1)
        today = date(2026, 1, 1)  # age = 56
        _, portfolio, _ = setup
        purchase = date(2019, 1, 1)  # > 5 years
        db.add(TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=purchase,
            units_remaining=Decimal("200"), cost_basis_remaining=Decimal("2000"),
            tax_scheme="RMF",
        ))
        await db.flush()

        lots = await ps.get_tax_eligibility(portfolio.id, db, today, user_dob)
        assert lots[0].is_eligible is True

    @pytest.mark.asyncio
    async def test_rmf_not_eligible_age_under_55(self, db, setup):
        user_dob = date(1985, 1, 1)
        today = date(2026, 1, 1)  # age = 41
        _, portfolio, _ = setup
        purchase = date(2019, 1, 1)
        db.add(TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=purchase,
            units_remaining=Decimal("200"), cost_basis_remaining=Decimal("2000"),
            tax_scheme="RMF",
        ))
        await db.flush()

        lots = await ps.get_tax_eligibility(portfolio.id, db, today, user_dob)
        assert lots[0].is_eligible is False

    @pytest.mark.asyncio
    async def test_normal_always_eligible(self, db, setup):
        _, portfolio, _ = setup
        db.add(TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=date(2025, 12, 1),  # very recent
            units_remaining=Decimal("100"), cost_basis_remaining=Decimal("1000"),
            tax_scheme="NORMAL",
        ))
        await db.flush()

        lots = await ps.get_tax_eligibility(portfolio.id, db, date(2025, 12, 2), None)
        assert lots[0].is_eligible is True

    @pytest.mark.asyncio
    async def test_eligible_date_computed(self, db, setup):
        """Eligible date = purchase + required_days (ceil)."""
        _, portfolio, _ = setup
        purchase = date(2020, 3, 15)
        db.add(TaxLot(
            id=uuid.uuid4(), portfolio_id=portfolio.id, fund_code="TESTFUND",
            original_purchase_date=purchase,
            units_remaining=Decimal("100"), cost_basis_remaining=Decimal("1000"),
            tax_scheme="SSF",
        ))
        await db.flush()

        lots = await ps.get_tax_eligibility(portfolio.id, db, date(2025, 1, 1), None)
        assert lots[0].eligible_date is not None
        # 10 × 365.25 = 3652.5 → ceil = 3653 days from purchase
        import math
        expected = purchase + __import__("datetime").timedelta(days=math.ceil(10 * 365.25))
        assert lots[0].eligible_date == expected
