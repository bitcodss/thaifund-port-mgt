"""
Manual sync trigger endpoints + sync job history.
All mutating endpoints require admin role.
Sync operations run in the background (returns immediately with a job ID).
"""
import logging
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_db
from app.models.tax_lot import SyncJob
from app.schemas.fund import SyncJobOut
from app.api.deps import require_admin
from app.services import sync_service
from app.services import finnomena_service
from app.services import factsheet_service, fund_category_service, news_service, proxy_service
from app.services.sec_api import SecApiUnauthorizedError
from app.services.portfolio_service import clear_all_cache

router = APIRouter(prefix="/sync", tags=["sync"])
logger = logging.getLogger(__name__)


async def _run_fund_sync():
    async with AsyncSessionLocal() as db:
        try:
            await sync_service.sync_fund_metadata(db)
        except Exception:
            logger.exception("Background fund metadata sync failed")


async def _run_categorize(force: bool):
    async with AsyncSessionLocal() as db:
        try:
            await fund_category_service.categorize_funds(db, force=force)
            clear_all_cache()
        except Exception:
            logger.exception("Background fund categorisation failed")


async def _run_proxies(force: bool):
    async with AsyncSessionLocal() as db:
        try:
            await proxy_service.refresh_proxies(db, force=force)
        except Exception:
            logger.exception("Background proxy refresh failed")


async def _run_news_collect():
    async with AsyncSessionLocal() as db:
        try:
            await news_service.collect_news(db)
        except Exception:
            logger.exception("Background news collection failed")


async def _run_digests(week_start: date | None, force: bool):
    async with AsyncSessionLocal() as db:
        try:
            await news_service.build_digests(db, week_start=week_start, force=force)
        except Exception:
            logger.exception("Background news digests failed")


async def _run_factsheet_scan(force: bool):
    async with AsyncSessionLocal() as db:
        try:
            await factsheet_service.scan_factsheets(db, force=force)
        except Exception:
            logger.exception("Background factsheet scan failed")


async def _run_nav_sync(nav_date: date):
    async with AsyncSessionLocal() as db:
        try:
            proj_ids = await sync_service.get_portfolio_proj_ids(db) or None
            await sync_service.sync_nav_for_date(db, nav_date, proj_ids=proj_ids)
            clear_all_cache()
        except Exception:
            logger.exception("Background NAV sync failed for %s", nav_date)


async def _run_nav_backfill(start_date: date, end_date: date, proj_ids: set[str] | None = None):
    async with AsyncSessionLocal() as db:
        try:
            await sync_service.sync_nav_range(db, start_date, end_date, proj_ids=proj_ids)
            clear_all_cache()
        except Exception:
            logger.exception("Background NAV backfill failed")


async def _run_dividend_sync():
    async with AsyncSessionLocal() as db:
        try:
            proj_ids = await sync_service.get_portfolio_proj_ids(db) or None
            await sync_service.sync_dividends(db, proj_ids=proj_ids)
            clear_all_cache()
        except Exception:
            logger.exception("Background dividend sync failed")


@router.post("/funds")
async def trigger_fund_metadata_sync(
    background_tasks: BackgroundTasks,
    _=Depends(require_admin),
):
    """Trigger fund metadata sync asynchronously. Returns immediately."""
    background_tasks.add_task(_run_fund_sync)
    return {"status": "started", "message": "Fund metadata sync running in background"}


@router.post("/nav")
async def trigger_nav_sync(
    background_tasks: BackgroundTasks,
    nav_date: date | None = None,
    _=Depends(require_admin),
):
    """Trigger NAV sync for a given date (defaults to today). Returns immediately."""
    target = nav_date or date.today()
    background_tasks.add_task(_run_nav_sync, target)
    return {"status": "started", "date": str(target), "message": "NAV sync running in background"}


@router.post("/nav/backfill")
async def trigger_nav_backfill(
    background_tasks: BackgroundTasks,
    start_date: date,
    end_date: date | None = None,
    portfolio_only: bool = True,
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Backfill NAV history for a date range. Skips weekends. Runs in background."""
    end = end_date or date.today()
    if start_date > end:
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")

    proj_ids: set[str] | None = None
    if portfolio_only:
        proj_ids = await sync_service.get_portfolio_proj_ids(db)
        if not proj_ids:
            raise HTTPException(status_code=400, detail="No portfolio funds with SEC project IDs found")

    weekdays = sum(
        1 for i in range((end - start_date).days + 1)
        if (start_date + timedelta(days=i)).weekday() < 5
    )
    background_tasks.add_task(_run_nav_backfill, start_date, end, proj_ids)
    funds_msg = f"{len(proj_ids)} portfolio funds" if proj_ids is not None else "all funds"
    return {
        "status": "started",
        "message": f"NAV backfill: {start_date} to {end}, {funds_msg}, ~{weekdays} weekdays",
    }


async def _run_finnomena_sync(fund_codes: list[str] | None = None):
    async with AsyncSessionLocal() as db:
        try:
            await finnomena_service.sync_finnomena_nav(db, fund_codes=fund_codes)
            clear_all_cache()
        except Exception:
            logger.exception("Background Finnomena NAV sync failed")


@router.post("/finnomena-nav")
async def trigger_finnomena_nav_sync(
    background_tasks: BackgroundTasks,
    _=Depends(require_admin),
):
    """Sync NAV history from Finnomena for all ES- funds in portfolio. Runs in background."""
    background_tasks.add_task(_run_finnomena_sync)
    return {"status": "started", "message": "Finnomena NAV sync running in background (ES- funds)"}


@router.post("/dividends")
async def trigger_dividend_sync(
    background_tasks: BackgroundTasks,
    _=Depends(require_admin),
):
    """Trigger dividend sync asynchronously. Returns immediately."""
    background_tasks.add_task(_run_dividend_sync)
    return {"status": "started", "message": "Dividend sync running in background"}


@router.post("/categories")
async def trigger_fund_categorisation(
    background_tasks: BackgroundTasks,
    force: bool = False,
    _=Depends(require_admin),
):
    """Refresh SEC classification fields + region/theme (local LLM) for portfolio funds.
    force=True re-classifies even when the SEC inputs are unchanged."""
    background_tasks.add_task(_run_categorize, force)
    return {"status": "started", "message": "Fund categorisation running in background"}


@router.post("/proxies")
async def trigger_proxy_refresh(background_tasks: BackgroundTasks, force: bool = False, _=Depends(require_admin)):
    """Suggest + validate news proxy tickers for portfolio funds (manual overrides kept)."""
    background_tasks.add_task(_run_proxies, force)
    return {"status": "started", "message": "Proxy refresh running in background"}


@router.post("/factsheets")
async def trigger_factsheet_scan(background_tasks: BackgroundTasks, force: bool = False, _=Depends(require_admin)):
    """Scan SEC factsheets of all active funds (AIMC category, holdings, FX hedge %).
    Resumable: already-scanned funds are skipped unless force=true."""
    background_tasks.add_task(_run_factsheet_scan, force)
    return {"status": "started", "message": "Factsheet scan running in background"}


@router.post("/news")
async def trigger_news_collect(background_tasks: BackgroundTasks, _=Depends(require_admin)):
    background_tasks.add_task(_run_news_collect)
    return {"status": "started", "message": "News collection running in background"}


@router.post("/news/digests")
async def trigger_news_digests(
    background_tasks: BackgroundTasks,
    week_start: date | None = None,
    force: bool = False,
    _=Depends(require_admin),
):
    """Build weekly digests (default: last complete Mon–Sun week, Asia/Bangkok)."""
    if week_start is not None and week_start.weekday() != 0:
        raise HTTPException(status_code=400, detail="week_start must be a Monday")
    background_tasks.add_task(_run_digests, week_start, force)
    return {"status": "started", "message": "News digests running in background"}


@router.get("/jobs", response_model=list[SyncJobOut])
async def list_sync_jobs(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    result = await db.execute(
        select(SyncJob).order_by(desc(SyncJob.started_at)).limit(limit)
    )
    return result.scalars().all()
