"""
Official AIMC fund categories (mutual funds), from the AIMC Category Performance
Report (as of 30/09/2026) plus "Miscellaneous" (an official category the report
omits). Factsheets print the category with layout noise and small spelling
differences, so matching is done on a normalised key plus a few seen misspellings.
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
    "Fund of Property fund -Thai and Foreign", "Global Bond Discretionary F/X Hedge or Unhedge",
    "Global Bond Fully F/X Hedge", "Global Equity", "Global Equity - Alternative Energy",
    "Global Equity - Consumer Goods and Services", "Global Equity - Infrastructure",
    "Global Equity Fully FX Risk Hedge", "Greater China Equity", "Health Care", "High Yield Bond",
    "India Equity", "Japan Equity", "Long Term General Bond", "Mid Term General Bond", "Miscellaneous",
    "Mid Term Government Bond", "Moderate Allocation", "Money Market General",
    "Money Market Government", "Other Global Sector Equity", "SET 50 Index Fund",
    "Short Term General Bond", "Short Term Government Bond", "Technology Equity", "Thai Free Hold",
    "US Equity", "Vietnam Equity",
]


def category_key(name: str) -> str:
    """Case/spacing/punctuation-insensitive key ("Asia Paciﬁc  Ex-Japan" == "Asia Pacific Ex Japan")."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", name).lower())


# Misspellings found in AMC factsheets
_ALIASES = {
    "Miscelleneous": "Miscellaneous",
    "Emerging Market Bond Discretionary F/X Hedge or Unhedged":
        "Emerging Market Bond Discretionary F/X Hedge or Unhedge",
}

_BY_KEY = {category_key(c): c for c in AIMC_CATEGORIES}
_BY_KEY.update({category_key(a): c for a, c in _ALIASES.items()})


def canonical_category(raw: str | None) -> str | None:
    """Official AIMC category for a parsed label, or None when it isn't on the list."""
    if not raw:
        return None
    return _BY_KEY.get(category_key(raw))


# One-line meaning of each category, for LLM inference (bare names pulled the model toward the
# narrow sector categories — e.g. a global small-cap growth fund into "Alternative Energy").
AIMC_DEFINITIONS = {
    "Aggressive Allocation": "mixed fund, mostly equities (high equity share)",
    "ASEAN Equity": "equities of ASEAN countries (Singapore, Indonesia, Malaysia, Philippines, Vietnam…) as a region or single ASEAN country other than Vietnam",
    "Asia Pacific Ex Japan": "equities across Asia-Pacific excluding Japan, incl. single markets like Korea, Taiwan, Hong Kong when not China-focused",
    "China Equity - A Shares": "onshore mainland China A-shares",
    "Commodities Energy": "oil / energy COMMODITY exposure (futures or commodity funds), not energy company stocks",
    "Commodities Precious Metals": "gold / silver COMMODITY exposure (bullion, gold ETFs), not mining stocks",
    "Conservative Allocation": "mixed fund, mostly bonds",
    "Emerging Market": "equities across global emerging markets",
    "Emerging Market Bond Discretionary F/X Hedge or Unhedge": "emerging-market bonds",
    "Energy": "stocks of energy companies (oil & gas producers, utilities)",
    "Equity General": "Thai equities, general / ESG / active Thai stock funds",
    "Equity Large Cap": "Thai large-cap equities",
    "Equity Small - Mid Cap": "Thai small / mid-cap equities",
    "European Equity": "European equities",
    "Foreign Investment Allocation": "multi-asset fund investing abroad",
    "Fund of Property Fund - Foreign": "foreign REITs / property funds",
    "Fund of Property Fund - Thai": "Thai property funds / REITs",
    "Fund of Property fund -Thai and Foreign": "Thai and foreign property funds / REITs",
    "Global Bond Discretionary F/X Hedge or Unhedge": "global bonds, discretionary FX hedge",
    "Global Bond Fully F/X Hedge": "global bonds, fully FX hedged",
    "Global Equity": "global equities WITHOUT a single-sector mandate (growth, value, small-cap, multi-theme innovation, ESG global funds)",
    "Global Equity - Alternative Energy": "ONLY clean / renewable / alternative energy and energy-transition companies",
    "Global Equity - Consumer Goods and Services": "ONLY consumer goods, retail, e-commerce, consumer services companies",
    "Global Equity - Infrastructure": "ONLY listed infrastructure (utilities, transport, towers, data-center infrastructure)",
    "Global Equity Fully FX Risk Hedge": "global equities with full FX hedging",
    "Greater China Equity": "China / Hong Kong / Taiwan equities, offshore or all-China",
    "Health Care": "health care, biotech, genomics, pharma, medical technology",
    "High Yield Bond": "high-yield bonds",
    "India Equity": "Indian equities",
    "Japan Equity": "ONLY Japanese equities",
    "Long Term General Bond": "Thai long-term bonds",
    "Mid Term General Bond": "Thai mid-term bonds",
    "Mid Term Government Bond": "Thai mid-term government bonds",
    "Moderate Allocation": "mixed fund, balanced equity/bond",
    "Money Market General": "money market",
    "Money Market Government": "government money market",
    "Other Global Sector Equity": "global single-SECTOR equity funds not covered elsewhere: financials, mining / materials / rare earths, gold miners, real assets, sector rotation",
    "SET 50 Index Fund": "Thai SET50 index funds",
    "Short Term General Bond": "Thai short-term bonds",
    "Short Term Government Bond": "Thai short-term government bonds",
    "Technology Equity": "technology, semiconductors, AI, internet, cybersecurity, quantum, metaverse",
    "Thai Free Hold": "Thai freehold property funds (real estate, not equities)",
    "US Equity": "US equities",
    "Vietnam Equity": "Vietnamese equities",
}
