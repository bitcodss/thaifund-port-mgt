"""
Fund performance / risk metrics tests — covers PR 5 finding M7 (binary-search
NAV lookback) and the surrounding query logic.
"""
from datetime import date
from decimal import Decimal

from app.services.performance_service import _nav_on_or_before


class TestNavOnOrBefore:
    def test_returns_none_on_empty(self):
        assert _nav_on_or_before([], date(2024, 1, 1)) is None

    def test_exact_match(self):
        pairs = [(date(2024, 1, 1), Decimal("10")), (date(2024, 1, 5), Decimal("12"))]
        assert _nav_on_or_before(pairs, date(2024, 1, 5)) == Decimal("12")

    def test_picks_most_recent_before_anchor(self):
        pairs = [
            (date(2024, 1, 1), Decimal("10")),
            (date(2024, 1, 3), Decimal("11")),
            (date(2024, 1, 5), Decimal("12")),
        ]
        # Anchor is Jan 4 → should pick Jan 3 (most recent <= anchor)
        assert _nav_on_or_before(pairs, date(2024, 1, 4)) == Decimal("11")

    def test_anchor_before_all_returns_none(self):
        pairs = [(date(2024, 1, 5), Decimal("12"))]
        assert _nav_on_or_before(pairs, date(2024, 1, 1)) is None

    def test_handles_gaps_longer_than_10_days(self):
        """M7 regression — the old impl only looked back 10 days. With a
        20-day gap (e.g., a sync gap or market closure), it returned None
        even though earlier NAVs existed."""
        pairs = [(date(2024, 1, 1), Decimal("10")), (date(2024, 2, 15), Decimal("12"))]
        # Anchor Feb 10 → 40 days after Jan 1, no NAV between → should
        # still find Jan 1
        assert _nav_on_or_before(pairs, date(2024, 2, 10)) == Decimal("10")

    def test_handles_decade_old_anchor(self):
        """Stress: anchor 10 years after the latest NAV still returns the
        latest available value."""
        pairs = [(date(2014, 1, 1), Decimal("10"))]
        assert _nav_on_or_before(pairs, date(2024, 1, 1)) == Decimal("10")
