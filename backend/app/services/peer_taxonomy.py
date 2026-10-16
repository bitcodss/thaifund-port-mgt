"""
Fixed peer-theme lists per AIMC category (approved — docs/monthly-fund-report-plan.md).
The LLM may only pick a theme from its fund's category list; categories not listed
here have a single theme equal to the category (no LLM call). Passive and ESG are
separate flags, never themes.
"""
from __future__ import annotations

THEMES_BY_CATEGORY: dict[str, list[str]] = {
    "Technology Equity": ["AI", "Semiconductor", "Cybersecurity", "Robotics & Automation",
                          "Internet & Digital", "General Technology"],
    "Other Global Sector Equity": ["Gold & Precious Metals Miners", "Financials", "Consumer",
                                   "Natural Resources", "Multi-Sector / Quant", "Other"],
    "Health Care": ["Life Sciences & Biotech", "Pharma & Large-Cap", "Medical Technology",
                    "General Health Care"],
    "Global Equity": ["Growth", "Value", "Quality / Dividend", "Small Cap", "General"],
    "US Equity": ["Broad / S&P 500", "Mega-Cap Concentrated", "Growth", "Value / Dividend", "Small–Mid Cap"],
    "Greater China Equity": ["China Tech & Innovation", "All-China Broad", "Hong Kong / H-Shares",
                             "China Consumer"],
    "Japan Equity": ["Broad / Index", "Growth", "Value / Dividend", "Small Cap"],
    # "Single Country": one-market funds (e.g. MSCI Korea) — kept out of the Broad group
    "Asia Pacific Ex Japan": ["Broad", "Growth", "Dividend / Income", "Single Country"],
    "Emerging Market": ["Broad EM", "EM Asia", "EM ex-China"],
}

# A theme group is used only when it has this many distinct masters after dedup;
# otherwise peers are the whole AIMC category.
MIN_THEME_GROUP = 5


def themes_for(category: str | None) -> list[str] | None:
    """Allowed themes for an AIMC category, or None when the category is its own single theme."""
    return THEMES_BY_CATEGORY.get(category or "")
