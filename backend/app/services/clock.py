"""
Single source of truth for "what date is it" when the answer should be a Thai
trading-business date (not the server's local date).

Containers commonly run UTC. A user opening the dashboard at 06:00 ICT
(23:00 UTC the previous day) would otherwise see "yesterday" everywhere —
holding-day counters, tax-eligibility countdowns, XIRR terminal dates all
shifted by 24 hours. Wrong, but only visible near midnight ICT.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

ICT = ZoneInfo("Asia/Bangkok")


def today_ict() -> date:
    """The current calendar date in Asia/Bangkok."""
    return datetime.now(ICT).date()
