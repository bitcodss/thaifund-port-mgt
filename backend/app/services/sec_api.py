"""
SEC Thailand Open Data API client (v2 "Fund API", product sec-openapi-normal).

One subscription key (SEC_API_KEY) covers NAV, dividends and factsheet data.
The legacy /FundDailyInfo and /FundFactsheet APIs were retired on 2026-07-01
and now answer every request with HTTP 503.

v2 list endpoints wrap results as {message, page_size, next_cursor, items};
_get_items() follows next_cursor until it is empty. HTTP 204 = no data.

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

FUND_BASE = "https://api.sec.or.th/v2/fund"

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

    async def get(self, url: str, params: dict | None = None) -> Any | None:
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
                    resp = await client.get(url, headers=headers, params=params)
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


async def _get_items(key: str, path: str, params: dict | None = None) -> list[dict]:
    """GET a v2 list endpoint and return all `items`, following next_cursor."""
    params = {**(params or {}), "page_size": 100}
    items: list[dict] = []
    while True:
        result = await _client_for(key).get(f"{FUND_BASE}{path}", params=params)
        if not result:
            return items
        items.extend(result.get("items") or [])
        cursor = result.get("next_cursor")
        if not cursor:
            return items
        params = {**params, "next_cursor": cursor}


# ── Daily info ────────────────────────────────────────────────────────────────

async def list_amcs(key: str) -> list[dict]:
    """GET /general-info/amcs → AMCs with unique_id, comp_name_en, comp_name_th."""
    return await _get_items(key, "/general-info/amcs")


async def get_nav_range(key: str, proj_id: str, start_date: date, end_date: date) -> list[dict]:
    """
    GET /daily-info/nav for [start_date, end_date] in one paginated request.
    Returns one dict per (share class, trading day) with keys: fund_class_name,
    nav_date, last_val, ... — unsorted. Weekends/holidays are simply absent.
    """
    return await _get_items(key, "/daily-info/nav", {
        "proj_id": proj_id,
        "start_nav_date": start_date.strftime("%Y-%m-%d"),
        "end_nav_date": end_date.strftime("%Y-%m-%d"),
    })


async def get_dividends(key: str, proj_id: str) -> list[dict]:
    """
    GET /daily-info/dividend-history
    Returns list of dividend events with keys:
      class_abbr_name, book_close_date, dividend_date, dividend_value
    """
    return await _get_items(key, "/daily-info/dividend-history", {"proj_id": proj_id})


# ── General info / factsheet ──────────────────────────────────────────────────

async def list_amc_funds(key: str, amc_unique_id: str) -> list[dict]:
    """
    GET /general-info/profiles filtered by AMC.
    Returns list of fund dicts with proj_id, proj_abbr_name, proj_name_th/en,
    fund_status, policy_desc, regis_date, cancel_date.
    """
    return await _get_items(key, "/general-info/profiles", {"company_info": amc_unique_id})


async def get_fund_policy(key: str, proj_id: str) -> dict | None:
    """GET /general-info/profiles for one fund → policy_desc (asset class in Thai)."""
    items = await _get_items(key, "/general-info/profiles", {"project_info": proj_id})
    return next((i for i in items if i.get("proj_id") == proj_id), None)


async def get_fund_performance(key: str, proj_id: str) -> list[dict]:
    """
    GET /factsheet/performance (latest factsheet)
    Returns list of rows with fund_class_name, performance_type_desc,
    reference_period, performance_value, start_date.
    """
    return await _get_items(key, "/factsheet/performance", {"proj_id": proj_id, "latest": "true"})


async def get_fund_profiles(key: str, proj_id: str) -> list[dict]:
    """GET /general-info/profiles for one fund → one row per share class
    (fund_class_name, management_style, feederfund_master_fund,
    exchange_rate_protection_policy, investment_policy_desc, ...)."""
    items = await _get_items(key, "/general-info/profiles", {"project_info": proj_id})
    return [i for i in items if i.get("proj_id") == proj_id]


async def get_benchmarks(key: str, proj_id: str) -> list[dict]:
    """GET /factsheet/benchmarks (latest factsheet) → rows with benchmark, group_seq."""
    return await _get_items(key, "/factsheet/benchmarks", {"proj_id": proj_id, "latest": "true"})


async def get_top5_holdings(key: str, proj_id: str) -> list[dict]:
    """GET /factsheet/top5-holdings (latest factsheet)."""
    return await _get_items(key, "/factsheet/top5-holdings", {"proj_id": proj_id, "latest": "true"})


async def get_specifications(key: str, proj_id: str) -> list[dict]:
    """GET /general-info/specifications → spec_code per class (e.g. "SRI" = SEC sustainable fund)."""
    return await _get_items(key, "/general-info/specifications", {"proj_id": proj_id})


async def get_risk_spectrum(key: str, proj_id: str) -> list[dict]:
    """GET /factsheet/risk-spectrum (latest factsheet) → rows with risk_spectrum e.g. "RS6"."""
    return await _get_items(key, "/factsheet/risk-spectrum", {"proj_id": proj_id, "latest": "true"})


async def get_factsheet_urls(key: str, proj_id: str) -> list[dict]:
    """GET /factsheet/urls → rows with pdf_factsheet (SEC copy), amc_url_factsheet, as_of_date."""
    return await _get_items(key, "/factsheet/urls", {"proj_id": proj_id})
