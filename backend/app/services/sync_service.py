"""
Phase 2 sync orchestration.

sync_fund_metadata()  — discovers all funds via FundFactsheet API (needs SEC_FACTSHEET_KEY)
sync_nav_for_date()   — fetches daily NAV for all funds with sec_proj_id set
sync_dividends()      — fetches dividend history for all funds with sec_proj_id set

All three are idempotent (safe to run again after partial failure).
Per-fund failures are logged and skipped; the batch continues.
Progress is written to sync_jobs table.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.fund import Fund, NavHistory, Dividend
from app.models.tax_lot import SyncJob
from app.models.transaction import Transaction
from app.services import sec_api
from app.services.portfolio_service import invalidate_portfolio
from app.services.sec_api import SecApiUnauthorizedError, SecApiError

# Auto-dividend creation: uniform 10% WHT across every tax scheme, per
# user direction. Marker note so users can tell auto-created from manual entries.
DIVIDEND_WHT_RATE = Decimal("0.10")
DIVIDEND_AUTO_NOTE = "Auto-imported from SEC dividend sync"
_AMOUNT_QUANT = Decimal("0.01")

logger = logging.getLogger(__name__)


# ── mapping helpers ───────────────────────────────────────────────────────────

def _policy_to_asset_class(policy_desc: str | None) -> str | None:
    if not policy_desc:
        return None
    d = policy_desc
    if "ตราสารทุน" in d:
        return "Equity"
    if "ตราสารหนี้" in d:
        return "Fixed Income"
    if "ตลาดเงิน" in d:
        return "Money Market"
    if "ผสม" in d:
        return "Mixed"
    if "สินค้าโภคภัณฑ์" in d or "ทองคำ" in d:
        return "Commodity"
    if "อสังหาริมทรัพย์" in d:
        return "Real Estate"
    return None


def _volatility_to_risk_level(pct: float) -> int:
    if pct < 2:   return 1
    if pct < 5:   return 2
    if pct < 10:  return 3
    if pct < 15:  return 4
    if pct < 20:  return 5
    if pct < 25:  return 6
    if pct < 35:  return 7
    return 8


def _latest_volatility_by_class(perf_rows: list[dict]) -> dict[str, float]:
    """
    From the /performance endpoint rows, extract the latest 1-year volatility
    per class_abbr_name. Returns {class_abbr_name: volatility_pct}.
    """
    best: dict[str, tuple[str, float]] = {}  # class → (as_of_date, pct)
    for row in perf_rows:
        if "ผันผวน" not in (row.get("performance_type_desc") or ""):
            continue
        if (row.get("reference_period") or "").strip() != "1 year":
            continue
        cls = (row.get("class_abbr_name") or "").strip()
        val_str = row.get("performance_val")
        as_of = row.get("as_of_date") or ""
        if not cls or val_str is None:
            continue
        try:
            val = float(val_str)
        except (ValueError, TypeError):
            continue
        existing = best.get(cls)
        if existing is None or as_of > existing[0]:
            best[cls] = (as_of, val)
    return {cls: v for cls, (_, v) in best.items()}


# ── helpers ───────────────────────────────────────────────────────────────────

async def _start_job(db: AsyncSession, job_type: str) -> SyncJob:
    job = SyncJob(
        id=uuid.uuid4(),
        type=job_type,
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    db.add(job)
    await db.flush()
    return job


async def _finish_job(db: AsyncSession, job: SyncJob, error: str | None = None, notes: str | None = None) -> None:
    job.completed_at = datetime.now(timezone.utc)
    job.status = "error" if error else "success"
    job.error_message = error or notes
    await db.flush()


async def cleanup_stale_running_jobs(db: AsyncSession) -> int:
    """
    Mark any sync_jobs row stuck in 'running' status as 'error'. Called on app
    startup so that jobs left dangling by a crashed previous process don't
    permanently show as in-progress on the /sync/jobs page.

    Returns the number of rows updated. We don't add a time-since-started
    threshold here because by definition a `running` row at startup is stale
    (no in-process scheduler holds it).
    """
    result = await db.execute(select(SyncJob).where(SyncJob.status == "running"))
    stale = list(result.scalars().all())
    now = datetime.now(timezone.utc)
    for job in stale:
        job.completed_at = now
        job.status = "error"
        job.error_message = "process terminated while job was running"
    if stale:
        await db.commit()
    return len(stale)


async def _funds_with_proj_id(db: AsyncSession, active_only: bool = False) -> Sequence[Fund]:
    q = select(Fund).where(Fund.sec_proj_id.isnot(None))
    if active_only:
        # "RG" = registered/active; NULL = manually created fund (include those too)
        from sqlalchemy import or_
        q = q.where(or_(Fund.fund_status == "RG", Fund.fund_status.is_(None)))
    result = await db.execute(q)
    return result.scalars().all()


# ── Fund metadata sync (FundFactsheet API) ────────────────────────────────────

async def sync_fund_metadata(db: AsyncSession) -> dict:
    """
    Fetch all AMCs → all funds from FundFactsheet API.
    Upserts fund rows (fund_code, sec_proj_id, name, amc, fund_status).
    Returns {"created": n, "updated": n, "errors": [...]}.
    Raises SecApiUnauthorizedError if FundFactsheet key is not subscribed.
    """
    key = settings.factsheet_key
    if not key:
        return {"created": 0, "updated": 0, "errors": ["SEC_FACTSHEET_KEY not configured"]}

    job = await _start_job(db, "fund_metadata")
    created = updated = 0
    errors: list[str] = []

    amcs = await sec_api.list_amcs(settings.SEC_API_KEY)
    if not amcs:
        await _finish_job(db, job, "No AMCs returned from FundDailyInfo/amc")
        await db.commit()
        return {"created": 0, "updated": 0, "errors": ["No AMCs available"]}

    # FundDailyInfo only lists ~27 AMCs; supplement with the FundFactsheet AMC list
    # so that AMCs registered with SEC but absent from FundDailyInfo (e.g. Eastspring)
    # also get their sec_proj_id populated.
    try:
        factsheet_amcs = await sec_api.list_factsheet_amcs(key)
        known_ids = {a.get("unique_id") for a in amcs}
        amcs = amcs + [a for a in factsheet_amcs if a.get("unique_id") not in known_ids]
    except Exception as e:
        errors.append(f"FundFactsheet AMC list failed: {e}")

    for amc in amcs:
        amc_id = amc.get("unique_id", "")
        amc_name = amc.get("name_en", "")
        try:
            funds = await sec_api.list_amc_funds(key, amc_id)
        except SecApiUnauthorizedError:
            raise  # caller must know FundFactsheet is not subscribed
        except SecApiError as e:
            errors.append(f"AMC {amc_id}: {e}")
            continue

        for fdata in funds:
            proj_id = fdata.get("proj_id") or fdata.get("fund_proj_id")
            abbr = fdata.get("proj_abbr_name") or fdata.get("fund_abbr_name")
            if not abbr:
                continue

            fund_code = abbr.strip().upper()
            result = await db.execute(select(Fund).where(Fund.fund_code == fund_code))
            fund = result.scalar_one_or_none()

            if fund is None:
                fund = Fund(fund_code=fund_code)
                db.add(fund)
                created += 1
            else:
                updated += 1

            fund.sec_proj_id = proj_id
            fund.name_th = fdata.get("proj_name_th") or fdata.get("fund_name_th")
            fund.name_en = fdata.get("proj_name_en") or fdata.get("fund_name_en")
            fund.amc = amc_name
            fund.amc_unique_id = amc_id
            fund.fund_status = fdata.get("fund_status")
            fund.last_synced_at = datetime.now(timezone.utc)

    # ── Enrich asset_class + risk_level via /policy and /performance ──────────
    # Only run for active funds that are still missing asset_class (skips re-runs).
    result = await db.execute(
        select(Fund).where(
            Fund.sec_proj_id.isnot(None),
            Fund.fund_status == "RG",
            Fund.asset_class.is_(None),
        )
    )
    funds_to_enrich = result.scalars().all()
    enriched = 0
    seen_proj_ids: set[str] = set()

    for fund in funds_to_enrich:
        proj_id = fund.sec_proj_id
        if proj_id in seen_proj_ids:
            continue
        seen_proj_ids.add(proj_id)

        try:
            policy = await sec_api.get_fund_policy(key, proj_id)
            perf_rows = await sec_api.get_fund_performance(key, proj_id)
        except SecApiError as e:
            errors.append(f"enrich {fund.fund_code}: {e}")
            continue

        asset_class = _policy_to_asset_class(policy.get("policy_desc") if policy else None)
        volatility_by_class = _latest_volatility_by_class(perf_rows)

        # Extract benchmark name from policy response (try known field names)
        benchmark_name: str | None = None
        if policy:
            for field in ("ref_index_desc", "benchmark_name", "benchmark", "ref_index"):
                val = policy.get(field)
                if val and str(val).strip():
                    benchmark_name = str(val).strip()
                    break

        # Update all funds (parent + classes) that share this proj_id
        siblings_result = await db.execute(
            select(Fund).where(Fund.sec_proj_id == proj_id)
        )
        for sibling in siblings_result.scalars().all():
            if asset_class:
                sibling.asset_class = asset_class
            if benchmark_name and not sibling.benchmark:
                sibling.benchmark = benchmark_name
            # Use class-specific volatility if available, else use any available value
            vol = volatility_by_class.get(sibling.fund_code)
            if vol is None and volatility_by_class:
                vol = next(iter(volatility_by_class.values()))
            if vol is not None:
                sibling.risk_level = _volatility_to_risk_level(vol)
            enriched += 1

    await db.flush()
    err_str = "; ".join(errors[:5]) if errors else None
    notes = f"created:{created} updated:{updated} enriched:{enriched}" if not err_str else None
    await _finish_job(db, job, err_str, notes)
    await db.commit()
    return {"created": created, "updated": updated, "errors": errors}


# ── NAV sync (FundDailyInfo API) ──────────────────────────────────────────────

async def get_portfolio_proj_ids(db: AsyncSession) -> set[str]:
    """Return unique sec_proj_ids for all funds referenced in any portfolio transaction."""
    from app.models.transaction import Transaction
    result = await db.execute(
        select(Fund.sec_proj_id)
        .join(Transaction, Fund.fund_code == Transaction.fund_code)
        .where(Fund.sec_proj_id.isnot(None))
        .distinct()
    )
    return {row[0] for row in result.all()}


async def sync_nav_for_date(db: AsyncSession, nav_date: date, proj_ids: set[str] | None = None) -> dict:
    """
    Fetch NAV for the given trade_date.
    If proj_ids is given, only sync those projects (portfolio-only backfill).
    Otherwise syncs all active funds.
    """
    key = settings.SEC_API_KEY
    if not key:
        return {"synced": 0, "skipped": 0, "errors": ["SEC_API_KEY not configured"]}

    if proj_ids is not None:
        result = await db.execute(
            select(Fund).where(Fund.sec_proj_id.in_(proj_ids))
        )
        funds = result.scalars().all()
    else:
        funds = await _funds_with_proj_id(db, active_only=True)
    if not funds:
        return {"synced": 0, "skipped": 0, "errors": ["No funds found"]}

    job = await _start_job(db, f"nav_sync:{nav_date}")
    synced = skipped = 0
    errors: list[str] = []
    seen_proj_ids: set[str] = set()

    for fund in funds:
        if fund.sec_proj_id in seen_proj_ids:
            continue
        seen_proj_ids.add(fund.sec_proj_id)
        try:
            data = await sec_api.get_daily_nav(key, fund.sec_proj_id, nav_date)
        except SecApiError as e:
            errors.append(f"{fund.fund_code}: {e}")
            skipped += 1
            continue

        if not data:
            skipped += 1
            continue

        entries = data if isinstance(data, list) else [data]
        fund_synced = False

        for entry in entries:
            # Use class_abbr_name when present (e.g. "SCBSEMI(SSF)"), else fall back to fund code
            class_abbr = (entry.get("class_abbr_name") or "").strip()
            # "main" is a generic SEC API placeholder, not a real fund abbreviation
            nav_code = class_abbr if class_abbr and class_abbr.lower() != "main" else fund.fund_code

            nav_val = entry.get("last_val") or entry.get("nav_value")
            prev_val = entry.get("previous_val") or entry.get("prev_nav_value")
            if nav_val is None:
                continue

            nav_dec = Decimal(str(nav_val))
            change_pct: Decimal | None = None
            if prev_val:
                prev_dec = Decimal(str(prev_val))
                if prev_dec > 0:
                    change_pct = ((nav_dec - prev_dec) / prev_dec * 100).quantize(Decimal("0.00000001"))

            # Auto-create a Fund record for this class if it doesn't exist yet
            class_fund = await db.get(Fund, nav_code)
            if class_fund is None:
                class_fund = Fund(
                    fund_code=nav_code,
                    sec_proj_id=fund.sec_proj_id,
                    name_en=fund.name_en,
                    name_th=fund.name_th,
                    amc=fund.amc,
                    amc_unique_id=fund.amc_unique_id,
                    fund_status=fund.fund_status,
                )
                db.add(class_fund)
                await db.flush()

            # Upsert nav_history for this class
            existing = await db.get(NavHistory, (nav_code, nav_date))
            if existing:
                existing.nav = nav_dec
                existing.change_pct = change_pct
            else:
                db.add(NavHistory(
                    fund_code=nav_code,
                    trade_date=nav_date,
                    nav=nav_dec,
                    change_pct=change_pct,
                ))

            class_fund.last_nav_date = nav_date
            fund_synced = True
            synced += 1

        if not fund_synced:
            skipped += 1

    err_str = "; ".join(errors[:5]) if errors else None
    notes = f"synced:{synced} skipped:{skipped}" if not err_str else None
    await _finish_job(db, job, err_str, notes)
    await db.commit()
    logger.info("NAV sync %s: synced=%d skipped=%d errors=%d", nav_date, synced, skipped, len(errors))
    return {"synced": synced, "skipped": skipped, "errors": errors}


# ── NAV range backfill ────────────────────────────────────────────────────────

async def sync_nav_range(db: AsyncSession, start_date: date, end_date: date, proj_ids: set[str] | None = None) -> dict:
    """
    Backfill NAV history for every weekday in [start_date, end_date].
    Creates one parent nav_backfill job plus one nav_sync:{date} job per date.
    """
    from datetime import timedelta
    parent_job = await _start_job(db, f"nav_backfill:{start_date}:{end_date}")
    await db.commit()  # make the job visible immediately

    current = start_date
    days_attempted = days_skipped = 0
    errors: list[str] = []
    while current <= end_date:
        if current.weekday() < 5:  # Mon–Fri only
            result = await sync_nav_for_date(db, current, proj_ids=proj_ids)
            if result.get("synced", 0) == 0 and not result.get("errors"):
                days_skipped += 1
            else:
                days_attempted += 1
            errors.extend(result.get("errors", []))
        current += timedelta(days=1)

    # Re-fetch parent_job after all the intermediate commits
    refreshed = await db.get(SyncJob, parent_job.id)
    err_str = "; ".join(errors[:3]) if errors else None
    notes = f"days:{days_attempted} skipped:{days_skipped}"
    await _finish_job(db, refreshed, err_str, notes if not err_str else None)
    await db.commit()
    return {"days_attempted": days_attempted, "days_skipped": days_skipped}


# ── Dividend sync (FundDailyInfo API) ─────────────────────────────────────────

async def _auto_create_dividend_transactions(
    db: AsyncSession,
    fund_code: str,
    ex_date: date,
    dividend_per_unit: Decimal,
) -> tuple[int, int, set[uuid.UUID]]:
    """
    For every portfolio holding `fund_code` at `ex_date` (across all tax schemes),
    insert a DIVIDEND transaction representing the cash dividend received.

    Returns (created_count, skipped_count, affected_portfolio_ids).

    Skip rules:
    - If any DIVIDEND tx already exists for (portfolio_id, fund_code, ex_date,
      tax_scheme) → respect manual entry, skip.
    - If computed units at ex_date <= 0 → portfolio wasn't holding, skip.
    """
    # Sum BUY+SWITCH_IN - SELL-SWITCH_OUT units per (portfolio, tax_scheme)
    # up to and including ex_date.
    rows = await db.execute(
        select(
            Transaction.portfolio_id,
            Transaction.tax_scheme,
            Transaction.type,
            Transaction.units,
        ).where(
            Transaction.fund_code == fund_code,
            Transaction.date <= ex_date,
            Transaction.type.in_(["BUY", "SELL", "SWITCH_IN", "SWITCH_OUT"]),
        )
    )
    units_by_key: dict[tuple[uuid.UUID, str], Decimal] = {}
    for row in rows.all():
        u = Decimal(str(row.units or 0))
        key = (row.portfolio_id, row.tax_scheme)
        delta = u if row.type in ("BUY", "SWITCH_IN") else -u
        units_by_key[key] = units_by_key.get(key, Decimal("0")) + delta

    created = 0
    skipped = 0
    affected: set[uuid.UUID] = set()
    for (portfolio_id, tax_scheme), units in units_by_key.items():
        if units <= 0:
            continue
        # Dedupe per (portfolio_id, fund_code, ex_date, tax_scheme) — manual
        # entries take priority.
        existing = await db.execute(
            select(Transaction.id).where(
                Transaction.portfolio_id == portfolio_id,
                Transaction.fund_code == fund_code,
                Transaction.date == ex_date,
                Transaction.tax_scheme == tax_scheme,
                Transaction.type == "DIVIDEND",
            )
        )
        if existing.first() is not None:
            skipped += 1
            continue

        amount = (units * dividend_per_unit).quantize(_AMOUNT_QUANT)
        tax_withheld = (amount * DIVIDEND_WHT_RATE).quantize(_AMOUNT_QUANT)
        db.add(Transaction(
            id=uuid.uuid4(),
            portfolio_id=portfolio_id,
            date=ex_date,
            type="DIVIDEND",
            fund_code=fund_code,
            amount=amount,
            fee=Decimal("0"),
            tax_withheld=tax_withheld,
            tax_scheme=tax_scheme,
            note=DIVIDEND_AUTO_NOTE,
        ))
        created += 1
        affected.add(portfolio_id)

    return created, skipped, affected


async def sync_dividends(db: AsyncSession, proj_ids: set[str] | None = None) -> dict:
    """
    Fetch full dividend history for portfolio funds (or all funds if proj_ids is None).
    Upserts dividends table. Deduplicates by (fund_code, ex_date).
    """
    key = settings.SEC_API_KEY
    if not key:
        return {"synced": 0, "skipped": 0, "errors": ["SEC_API_KEY not configured"]}

    if proj_ids is not None:
        result = await db.execute(select(Fund).where(Fund.sec_proj_id.in_(proj_ids)))
        funds = result.scalars().all()
    else:
        funds = await _funds_with_proj_id(db)
    if not funds:
        return {"synced": 0, "skipped": 0, "errors": ["No funds found"]}

    job = await _start_job(db, "dividend_sync")
    synced = skipped = 0
    auto_created = auto_skipped = 0
    affected_portfolios: set[uuid.UUID] = set()
    errors: list[str] = []

    for fund in funds:
        try:
            divs = await sec_api.get_dividends(key, fund.sec_proj_id)
        except SecApiError as e:
            errors.append(f"{fund.fund_code}: {e}")
            skipped += 1
            continue

        for div in divs:
            ex_date_raw = div.get("book_close_date") or div.get("ex_date")
            pay_date_raw = div.get("dividend_date") or div.get("payment_date")
            amount_raw = div.get("dividend_value") or div.get("dividend_per_unit")

            if not ex_date_raw or amount_raw is None:
                continue

            try:
                ex_date = date.fromisoformat(str(ex_date_raw)[:10])
                pay_date = date.fromisoformat(str(pay_date_raw)[:10]) if pay_date_raw else None
                amount = Decimal(str(amount_raw))
            except (ValueError, TypeError) as e:
                errors.append(f"{fund.fund_code} div parse error: {e}")
                continue

            # Deduplicate: upsert by (fund_code, ex_date)
            result = await db.execute(
                select(Dividend).where(
                    Dividend.fund_code == fund.fund_code,
                    Dividend.ex_date == ex_date,
                )
            )
            existing_div = result.scalar_one_or_none()
            if existing_div:
                existing_div.dividend_per_unit = amount
                existing_div.payment_date = pay_date
                existing_div.source = "sec_api"
            else:
                db.add(Dividend(
                    id=uuid.uuid4(),
                    fund_code=fund.fund_code,
                    ex_date=ex_date,
                    payment_date=pay_date,
                    dividend_per_unit=amount,
                    source="sec_api",
                ))
                synced += 1

            # Auto-create DIVIDEND transactions for every portfolio that held
            # the fund on ex_date. Idempotent — re-runs hit the per-portfolio
            # dedupe and skip.
            c, s, affected = await _auto_create_dividend_transactions(
                db, fund.fund_code, ex_date, amount,
            )
            auto_created += c
            auto_skipped += s
            affected_portfolios |= affected

    # Drop stale analytics for every portfolio that received new dividends.
    for pid in affected_portfolios:
        invalidate_portfolio(pid)

    err_str = "; ".join(errors[:5]) if errors else None
    notes = (
        f"synced:{synced} skipped:{skipped} "
        f"auto_dividends:created={auto_created} skipped={auto_skipped}"
    ) if not err_str else None
    await _finish_job(db, job, err_str, notes)
    await db.commit()
    return {
        "synced": synced,
        "skipped": skipped,
        "auto_dividends_created": auto_created,
        "auto_dividends_skipped": auto_skipped,
        "errors": errors,
    }
