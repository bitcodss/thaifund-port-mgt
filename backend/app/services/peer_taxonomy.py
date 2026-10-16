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


# Rule (not LLM): a PASSIVE fund tracking a broad market index gets the category's broad theme.
# Matched on master fund name + benchmark; factor / sector / screened variants are excluded.
BROAD_INDEX_RULES: dict[str, tuple[str, list[str]]] = {
    "Global Equity": ("General", [r"\bACWI\b", r"\bAC World\b", r"\bMSCI World\b", r"\bAll[- ]World\b",
                                  r"\bTotal World\b", r"\bGlobal Stock Market\b", r"\bTotal International\b"]),
    "US Equity": ("Broad / S&P 500", [r"\bS&P 500\b", r"\bTotal (US )?Stock Market\b"]),
    "Japan Equity": ("Broad / Index", [r"\bNikkei 225\b", r"\bTOPIX\b", r"\bMSCI Japan\b"]),
    "Asia Pacific Ex Japan": ("Broad", [r"\bAsia (Pacific )?\(?ex[- ]Japan\b"]),
    "Emerging Market": ("Broad EM", [r"\bMSCI Emerging Markets?\b", r"\bFTSE Emerging Markets?\b"]),
    "Greater China Equity": ("All-China Broad", [r"\bMSCI China All Shares\b", r"\bS&P China\b"]),
}
BROAD_INDEX_EXCLUDE = (r"quality|dividend|yield|value|momentum|growth|small|mid[- ]cap|islamic|factor|"
                       r"volatility|equal weight|esg|sri|sustainable|semiconductor|tech|ex[- ]china|"
                       r"miners|buyback|sector|leveraged|inverse")


def broad_index_theme(category: str | None, is_passive: bool, data: dict) -> str | None:
    """The category's broad theme when a passive fund tracks a plain broad index, else None."""
    import re
    rule = BROAD_INDEX_RULES.get(category or "")
    if not rule or not is_passive:
        return None
    text = " / ".join(filter(None, [data.get("master_fund"), data.get("benchmark")]))
    if re.search(BROAD_INDEX_EXCLUDE, text, re.I):
        return None
    return rule[0] if any(re.search(p, text, re.I) for p in rule[1]) else None


def themes_for(category: str | None) -> list[str] | None:
    """Allowed themes for an AIMC category, or None when the category is its own single theme."""
    return THEMES_BY_CATEGORY.get(category or "")
