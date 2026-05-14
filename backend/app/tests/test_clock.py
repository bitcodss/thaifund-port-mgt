"""
Tests for the ICT clock helper. The server's local TZ is often UTC in
containers; "today" for a Bangkok user must come from ICT, not UTC, or
midnight-rollover values (holding_days, tax-eligibility) shift by 24h.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.services.clock import ICT, today_ict


def test_today_ict_returns_a_date():
    assert isinstance(today_ict(), date)


def test_ict_is_bangkok():
    assert ICT == ZoneInfo("Asia/Bangkok")


def test_today_ict_matches_explicit_now_in_bangkok():
    # Direct comparison: today_ict() must equal datetime.now(ICT).date().
    assert today_ict() == datetime.now(ICT).date()
