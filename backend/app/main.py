import logging
from contextlib import asynccontextmanager
from datetime import date, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, users, portfolios, transactions
from app.api import funds as funds_router_module
from app.api import sync as sync_router_module
from app.api import analytics as analytics_router_module
from app.database import AsyncSessionLocal
from app.services import sync_service

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


async def _nightly_nav_sync():
    """Runs at 12:30 UTC (19:30 ICT) on weekdays."""
    async with AsyncSessionLocal() as db:
        try:
            result = await sync_service.sync_nav_for_date(db, date.today())
            logger.info("Scheduled NAV sync: %s", result)
        except Exception:
            logger.exception("Scheduled NAV sync failed")


async def _weekly_metadata_sync():
    """Runs Sunday 01:00 UTC."""
    async with AsyncSessionLocal() as db:
        try:
            result = await sync_service.sync_fund_metadata(db)
            logger.info("Scheduled metadata sync: %s", result)
        except Exception:
            logger.exception("Scheduled metadata sync failed")


async def _daily_dividend_sync():
    """Runs daily at 13:30 UTC (20:30 ICT)."""
    async with AsyncSessionLocal() as db:
        try:
            result = await sync_service.sync_dividends(db)
            logger.info("Scheduled dividend sync: %s", result)
        except Exception:
            logger.exception("Scheduled dividend sync failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(_nightly_nav_sync, CronTrigger(day_of_week="mon-fri", hour=12, minute=30))
    scheduler.add_job(_weekly_metadata_sync, CronTrigger(day_of_week="sun", hour=1, minute=0))
    scheduler.add_job(_daily_dividend_sync, CronTrigger(hour=13, minute=30))
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Thai Fund Tracker API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
