"""
Finnomena NAV sync — fallback for ES- funds.

NOTE: As of 2026-05, Eastspring (ES-) funds ARE available via the SEC FundDailyInfo
API using their SEC project IDs (e.g. ES-EGRMF → M0253_2557). The standard Fund
Metadata sync now populates sec_proj_id for these funds, and the regular NAV backfill
covers them. This Finnomena path is kept as a fallback in case the SEC API is unavailable.

Finnomena's fn3/api/fund/nav/q endpoint currently returns:
  {"message":"unable to retrives latest data for: <id>"}
for all funds regardless of authentication — this is a Finnomena backend bug.

Auth flow (kept for reference):
  1. GET /fn3/api/auth/loginaction → redirects to auth.finnomena.com, captures challenge
  2. POST auth.finnomena.com/api/web/login with {email, password, challenge}
  3. GET redirect_to URL → sets access_token cookie
  4. Use access_token cookie on all subsequent requests
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from urllib.parse import urlparse, parse_qs

from app.config import settings
from app.models.fund import Fund, NavHistory

logger = logging.getLogger(__name__)

_BASE = "https://www.finnomena.com"
_AUTH_BASE = "https://auth.finnomena.com"
_FUND_LIST_URL = f"{_BASE}/fn3/api/fund/public/list"


class FinnomenaAuthError(Exception):
    pass


async def _get_access_token(email: str, password: str) -> str:
    """Authenticate and return the access_token cookie value."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        # Step 1: get challenge from the login action redirect
        resp = await client.get(
            f"{_BASE}/fn3/api/auth/loginaction",
            params={"return_url": _BASE + "/", "action": "login", "device": "web"},
        )
        # Extract challenge query parameter from the redirected URL
        qs = parse_qs(urlparse(str(resp.url)).query)
        challenge = qs.get("challenge", [None])[0]
        if not challenge:
            raise FinnomenaAuthError("Could not extract login challenge")

        # Step 2: POST credentials
        resp2 = await client.post(
            f"{_AUTH_BASE}/api/web/login",
            content=f'{{"email":"{email}","password":"{password}","challenge":"{challenge}"}}',
            headers={"Content-Type": "application/json"},
        )
        if not resp2.is_success:
            raise FinnomenaAuthError(f"Login failed: {resp2.status_code}")

        data = resp2.json()
        redirect_to = data.get("data", {}).get("redirect_to")
        if not redirect_to:
            raise FinnomenaAuthError("No redirect_to in login response")

        # Step 3: follow redirect to set cookie
        resp3 = await client.get(redirect_to)
        token = client.cookies.get("access_token")
        if not token:
            raise FinnomenaAuthError("access_token cookie not set after login")

        return token


async def _get_fund_mstar_id(fund_code: str) -> str | None:
    """Look up Morningstar ID for a fund code from Finnomena public list."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(_FUND_LIST_URL)
        resp.raise_for_status()
        funds = resp.json()
    return next(
        (f["id"] for f in funds if f.get("short_code") == fund_code),
        None,
    )


async def _fetch_nav_history(mstar_id: str, access_token: str) -> list[dict]:
    """Fetch full NAV history for a fund using authenticated session."""
    cookies = {"access_token": access_token}
    async with httpx.AsyncClient(timeout=60, cookies=cookies) as client:
        resp = await client.get(
            f"{_BASE}/fn3/api/fund/nav/q",
            params={"range": "MAX", "fund": mstar_id},
        )
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict):
        # Error response like {"message": "unable to retrives latest data for: ..."}
        raise ValueError(f"NAV history error: {data.get('message', data)}")
    return data  # list of {nav_date, value}


async def _upsert_fund(fund_code: str, db: AsyncSession) -> None:
    """Ensure a Fund row exists for the given code (creates minimal stub if missing)."""
    existing = await db.get(Fund, fund_code)
    if existing:
        return

    # Fetch details from Finnomena public endpoint
    mstar_id = await _get_fund_mstar_id(fund_code)
    if not mstar_id:
        return

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{_BASE}/fn3/api/fund/public/{mstar_id}")
        details = resp.json() if resp.is_success else {}

    fund = Fund(
        fund_code=fund_code,
        name_en=details.get("name_en"),
        name_th=details.get("name_th"),
        amc=details.get("amc_name_en", "EASTSPRING"),
        asset_class=details.get("aimc_broad_category"),
        risk_level=int(details["risk_level"]) if details.get("risk_level") else None,
        fund_status="RG",
    )
    db.add(fund)
    await db.flush()


async def sync_finnomena_nav(db: AsyncSession, fund_codes: list[str] | None = None) -> dict:
    """
    Sync NAV history from Finnomena for the given fund codes (or all ES- funds in
    transactions if fund_codes is None).

    Returns a summary dict with counts of synced/skipped/failed funds.
    """
    email = settings.FINNOMENA_EMAIL
    password = settings.FINNOMENA_PASSWORD
    if not email or not password:
        return {"status": "error", "message": "FINNOMENA_EMAIL / FINNOMENA_PASSWORD not configured"}

    # If no explicit list, find all ES- fund codes in transactions
    if fund_codes is None:
        from app.models.transaction import Transaction
        result = await db.execute(
            select(Transaction.fund_code)
            .where(Transaction.fund_code.like("ES-%"))
            .distinct()
        )
        fund_codes = [r[0] for r in result.all() if r[0]]
        # Also check target_fund_code
        result2 = await db.execute(
            select(Transaction.target_fund_code)
            .where(Transaction.target_fund_code.like("ES-%"))
            .distinct()
        )
        fund_codes += [r[0] for r in result2.all() if r[0]]
        fund_codes = list(set(fund_codes))

    if not fund_codes:
        return {"status": "ok", "message": "No ES- funds found in portfolio", "synced": 0}

    logger.info("Finnomena NAV sync starting for %d funds: %s", len(fund_codes), fund_codes)

    # Authenticate once
    try:
        token = await _get_access_token(email, password)
    except FinnomenaAuthError as exc:
        logger.error("Finnomena auth failed: %s", exc)
        return {"status": "error", "message": f"Authentication failed: {exc}"}

    # Fetch fund list once for Morningstar ID lookup
    async with httpx.AsyncClient(timeout=30) as client:
        fund_list_resp = await client.get(_FUND_LIST_URL)
        fund_list = fund_list_resp.json() if fund_list_resp.is_success else []
    mstar_map = {f["short_code"]: f["id"] for f in fund_list}

    synced = skipped = failed = 0
    today = date.today()

    for fund_code in fund_codes:
        mstar_id = mstar_map.get(fund_code)
        if not mstar_id:
            logger.warning("Finnomena: no Morningstar ID for %s", fund_code)
            skipped += 1
            continue

        try:
            nav_rows = await _fetch_nav_history(mstar_id, token)
        except Exception as exc:
            logger.warning("Finnomena NAV fetch failed for %s: %s", fund_code, exc)
            failed += 1
            continue

        # Ensure Fund row exists
        await _upsert_fund(fund_code, db)

        # Upsert NAV rows
        inserted = 0
        for row in nav_rows:
            try:
                nav_date_str = row.get("nav_date", "")[:10]  # "2025-01-01T00:00:00Z" → "2025-01-01"
                nav_val = Decimal(str(row["value"]))
                trade_date = date.fromisoformat(nav_date_str)
                if trade_date > today:
                    continue

                existing = await db.get(NavHistory, (fund_code, trade_date))
                if existing:
                    existing.nav = nav_val
                else:
                    db.add(NavHistory(fund_code=fund_code, trade_date=trade_date, nav=nav_val))
                inserted += 1
            except Exception:
                continue

        await db.flush()
        logger.info("Finnomena: upserted %d NAV rows for %s", inserted, fund_code)
        synced += 1

    await db.commit()
    return {
        "status": "ok",
        "synced": synced,
        "skipped": skipped,
        "failed": failed,
        "message": f"Finnomena NAV sync complete: {synced} funds synced, {skipped} skipped, {failed} failed",
    }
