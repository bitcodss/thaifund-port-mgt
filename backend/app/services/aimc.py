"""
Official AIMC fund categories (mutual funds), from the AIMC Category Performance
Report (as of 30/09/2026). Factsheets print the category with layout noise and
small spelling differences, so matching is done on a normalised key.
"""
from __future__ import annotations

import re
import unicodedata

AIMC_CATEGORIES = [
    "Aggressive Allocation", "ASEAN Equity", "Asia Pacific Ex Japan", "China Equity - A Shares",
    "Commodities Energy", "Commodities Precious Metals", "Conservative Allocation", "Emerging Market",
    "Emerging Market Bond Discretionary F/X Hedge or Unhedge", "Energy", "Equity General",
    "Equity Large Cap", "Equity Small - Mid Cap", "European Equity", "Foreign Investment Allocation",
    "Fund of Property Fund - Foreign", "Fund of Property Fund - Thai",
    "Fund of Property fund -Thai and Foreign", "Global Bond Discretionary F/X Hedge",
    "Global Bond Fully F/X Hedge", "Global Equity", "Global Equity - Alternative Energy",
    "Global Equity - Consumer Goods and Services", "Global Equity - Infrastructure",
    "Global Equity Fully FX Risk Hedge", "Greater China Equity", "Health Care", "High Yield Bond",
    "India Equity", "Japan Equity", "Long Term General Bond", "Mid Term General Bond",
    "Mid Term Government Bond", "Moderate Allocation", "Money Market General",
    "Money Market Government", "Other Global Sector Equity", "SET 50 Index Fund",
    "Short Term General Bond", "Short Term Government Bond", "Technology Equity", "Thai Free Hold",
    "US Equity", "Vietnam Equity",
]


def category_key(name: str) -> str:
    """Case/spacing/punctuation-insensitive key ("Asia Paciﬁc  Ex-Japan" == "Asia Pacific Ex Japan")."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", name).lower())


_BY_KEY = {category_key(c): c for c in AIMC_CATEGORIES}


def canonical_category(raw: str | None) -> str | None:
    """Official AIMC category for a parsed label, or None when it isn't on the list."""
    if not raw:
        return None
    return _BY_KEY.get(category_key(raw))
