import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from app.api import auth, users, portfolios, transactions
from app.api import funds as funds_router_module
from app.api import sync as sync_router_module
from app.api import analytics as analytics_router_module
from app.database import AsyncSessionLocal
from app.models.fund import Fund, NavHistory
from app.services import sync_service
from app.services.clock import today_ict
from app.services.portfolio_service import clear_all_cache

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")

# Hard cap on the catch-up window so a long outage doesn't burst the SEC API.
NAV_CATCHUP_MAX_DAYS = 30


async def nav_catchup(db) -> None:
    """Backfill missing NAV days for portfolio funds, from the most recent
    `nav_history.trade_date` we already have through yesterday (ICT).

    Replaces the old `fetch today's NAV at 19:30 ICT` scheduler that always
    returned synced:0 because SEC FundDailyInfo doesn't publish trade-date T's
    NAV until the morning of T+1. Idempotent, no-op when up-to-date, capped at
    NAV_CATCHUP_MAX_DAYS so a long absence doesn't burst the SEC API.

    Takes an explicit session so it's directly unit-testable; the cron wrapper
    `_run_nav_catchup` opens the production session.
    """
    yesterday = today_ict() - timedelta(days=1)
    proj_ids = await sync_service.get_portfolio_proj_ids(db)
    if not proj_ids:
        logger.info("NAV catch-up skipped: no portfolio funds")
        return

    result = await db.execute(
        select(func.max(NavHistory.trade_date))
        .join(Fund, NavHistory.fund_code == Fund.fund_code)
        .where(Fund.sec_proj_id.in_(proj_ids))
    )
    last_have = result.scalar()
    if last_have:
        start = last_have + timedelta(days=1)
    else:
        start = yesterday - timedelta(days=NAV_CATCHUP_MAX_DAYS - 1)
    if start > yesterday:
        logger.info("NAV catch-up: up to date through %s", last_have)
        return

    min_start = yesterday - timedelta(days=NAV_CATCHUP_MAX_DAYS - 1)
    if start < min_start:
        logger.warning(
            "NAV catch-up: gap exceeds %d-day cap (last NAV %s); "
            "older days need a manual backfill",
            NAV_CATCHUP_MAX_DAYS, last_have,
        )
        start = min_start

    logger.info("NAV catch-up: %s → %s", start, yesterday)
    outcome = await sync_service.sync_nav_range(
        db, start, yesterday, proj_ids=proj_ids,
    )
    logger.info("NAV catch-up result: %s", outcome)
    clear_all_cache()


async def _run_nav_catchup() -> None:
    """Production wrapper: open a session and delegate to nav_catchup."""
    async with AsyncSessionLocal() as db:
        try:
            await nav_catchup(db)
        except Exception:
            logger.exception("NAV catch-up failed")


async def _weekly_metadata_sync():
    """Runs Sunday 01:00 UTC."""
    async with AsyncSessionLocal() as db:
        try:
            result = await sync_service.sync_fund_metadata(db)
            logger.info("Scheduled metadata sync: %s", result)
            clear_all_cache()
        except Exception:
            logger.exception("Scheduled metadata sync failed")


async def _daily_dividend_sync():
    """Runs daily at 13:30 UTC (20:30 ICT)."""
    async with AsyncSessionLocal() as db:
        try:
            result = await sync_service.sync_dividends(db)
            logger.info("Scheduled dividend sync: %s", result)
            clear_all_cache()
        except Exception:
            logger.exception("Scheduled dividend sync failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Mark any sync_jobs rows left in 'running' status by a crashed prior
    # process as 'error'. Otherwise they show up as phantom in-progress jobs.
    async with AsyncSessionLocal() as db:
        try:
            cleaned = await sync_service.cleanup_stale_running_jobs(db)
            if cleaned:
                logger.info("Marked %d stale sync_jobs as error on startup", cleaned)
        except Exception:
            logger.exception("Stale sync_jobs cleanup failed")

    # Run an immediate NAV catch-up so users aren't stuck on stale data after
    # a deploy or restart — without this they'd wait until the next 09:00 ICT cron.
    await _run_nav_catchup()

    # Tue-Sat 02:00 UTC = 09:00 ICT (Tue-Sat). Fetches the prior business day's
    # NAV, which is when SEC FundDailyInfo data has actually been published.
    # Sun/Mon are no-ops because the prior day is a weekend.
    scheduler.add_job(_run_nav_catchup, CronTrigger(day_of_week="tue-sat", hour=2, minute=0))
    scheduler.add_job(_weekly_metadata_sync, CronTrigger(day_of_week="sun", hour=1, minute=0))
    scheduler.add_job(_daily_dividend_sync, CronTrigger(hour=13, minute=30))
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Thai Fund Tracker API", version="1.0.0", lifespan=lifespan)

# CORS: explicit origins only. Bearer tokens travel in the Authorization header
# (not cookies), so allow_credentials stays off — that combo with allow_origins=["*"]
# was both spec-invalid and unnecessary for this auth model.
from app.config import settings as _cfg
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cfg.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api/v1"

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(users.router, prefix=API_PREFIX)
app.include_router(portfolios.router, prefix=API_PREFIX)
app.include_router(transactions.router, prefix=API_PREFIX)
app.include_router(transactions.lots_router, prefix=API_PREFIX)
app.include_router(funds_router_module.router, prefix=API_PREFIX)
app.include_router(sync_router_module.router, prefix=API_PREFIX)
app.include_router(analytics_router_module.router, prefix=API_PREFIX)


@app.get("/health")
async def health():
    return {"status": "ok"}
