"""
SEC Thailand Open Data API client.

Two separate API products — each requires its own subscription key:
  FundDailyInfo   → SEC_API_KEY      (NAV + dividends per fund)
  FundFactsheet   → SEC_FACTSHEET_KEY (fund metadata + proj_id discovery)

Rate limit: 3,000 req / 300 s ≈ 10 req/s.
On HTTP 421 (SEC rate limit): back off per Retry-After header.
Skip funds that fail; never abort the whole batch.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FACTSHEET_BASE = "https://api.sec.or.th/FundFactsheet"
DAILY_INFO_BASE = "https://api.sec.or.th/FundDailyInfo"

_REQUEST_INTERVAL = 0.11          # 9 req/s — stay comfortably under 10
_MAX_RETRIES = 3
_TIMEOUT = httpx.Timeout(30.0)


class SecApiError(Exception):
    pass


class SecApiUnauthorizedError(SecApiError):
    """Key is not subscribed to this API product."""
    pass


class _ThrottledClient:
    """
    Thin httpx wrapper that:
    - enforces a minimum interval between requests
    - retries on 421/429 with Retry-After
    - raises SecApiUnauthorizedError on 401 so callers can degrade gracefully

    Per-key state lives here. Use module-level _client_for() to share state
    across calls — instantiating a new client per call defeats the throttler.
    """

    def __init__(self, api_key: str):
        self._key = api_key
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, url: str) -> Any | None:
        async with self._lock:
            now = time.monotonic()
            gap = _REQUEST_INTERVAL - (now - self._last_call)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last_call = time.monotonic()

        headers = {"Ocp-Apim-Subscription-Key": self._key}
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.get(url, headers=headers)
            except httpx.RequestError as exc:
                logger.warning("Network error %s: %s", url, exc)
                if attempt == _MAX_RETRIES - 1:
                    raise SecApiError(f"Network error: {exc}") from exc
                await asyncio.sleep(2 ** attempt)
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 204:
                return None  # valid request, no data
            if resp.status_code == 401:
                raise SecApiUnauthorizedError(
                    f"401 Unauthorized — key not subscribed to this API: {url}"
                )
            if resp.status_code in (421, 429):
                wait = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                logger.warning("Rate limited (%s) on %s, waiting %.1fs", resp.status_code, url, wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 404:
                return None
            logger.error("SEC API %s for %s", resp.status_code, url)
            if attempt == _MAX_RETRIES - 1:
                raise SecApiError(f"HTTP {resp.status_code} from {url}")
            await asyncio.sleep(2 ** attempt)

        return None


# Module-level per-key client registry. Without this, the throttler state
# (_last_call) resets on every call — defeating the rate limit entirely.
_clients: dict[str, _ThrottledClient] = {}


def _client_for(key: str) -> _ThrottledClient:
    """Return the shared throttled client for this API key, creating it lazily."""
    if key not in _clients:
        _clients[key] = _ThrottledClient(key)
    return _clients[key]


# ── FundDailyInfo API ─────────────────────────────────────────────────────────

async def list_amcs(key: str) -> list[dict]:
    """GET /FundDailyInfo/amc → all 27 AMCs with unique_id."""
    result = await _client_for(key).get(f"{DAILY_INFO_BASE}/amc")
    return result or []


async def get_daily_nav(key: str, proj_id: str, nav_date: date) -> dict | list | None:
    """
    GET /FundDailyInfo/{proj_id}/dailynav/{date}
    Returns a list of dicts (one per share class) with keys: last_val, previous_val, etc.
    Some older endpoints return a single dict. Returns None on weekend/holiday (HTTP 204).
    """
    date_str = nav_date.strftime("%Y-%m-%d")
    return await _client_for(key).get(f"{DAILY_INFO_BASE}/{proj_id}/dailynav/{date_str}")


async def get_dividends(key: str, proj_id: str) -> list[dict]:
    """
    GET /FundDailyInfo/{proj_id}/dividend
    Returns list of dividend events with keys:
      book_close_date, dividend_date, dividend_value, unit_base
    """
    result = await _client_for(key).get(f"{DAILY_INFO_BASE}/{proj_id}/dividend")
    return result or []


# ── FundFactsheet API (requires separate subscription) ────────────────────────

async def list_factsheet_amcs(key: str) -> list[dict]:
    """GET /FundFactsheet/fund/amc → all AMCs registered with SEC (superset of FundDailyInfo list)."""
    result = await _client_for(key).get(f"{FACTSHEET_BASE}/fund/amc")
    return result or []


async def list_amc_funds(key: str, amc_unique_id: str) -> list[dict]:
    """
    GET /FundFactsheet/fund/amc/{unique_id}
    Returns list of fund dicts with proj_id, proj_abbr_name, proj_name_th/en,
    fund_status, regis_date, cancel_date.
    Raises SecApiUnauthorizedError if not subscribed to FundFactsheet.
    """
    result = await _client_for(key).get(f"{FACTSHEET_BASE}/fund/amc/{amc_unique_id}")
    return result or []


async def get_fund_policy(key: str, proj_id: str) -> dict | None:
    """GET /FundFactsheet/fund/{proj_id}/policy → policy_desc (asset class in Thai), management_style."""
    return await _client_for(key).get(f"{FACTSHEET_BASE}/fund/{proj_id}/policy")


async def get_fund_performance(key: str, proj_id: str) -> list[dict]:
    """
    GET /FundFactsheet/fund/{proj_id}/performance
    Returns list of rows with class_abbr_name, performance_type_desc, performance_val, as_of_date.
    Filter for rows where performance_type_desc contains 'ผันผวน' to get annualised volatility.
    """
    result = await _client_for(key).get(f"{FACTSHEET_BASE}/fund/{proj_id}/performance")
    return result or []
