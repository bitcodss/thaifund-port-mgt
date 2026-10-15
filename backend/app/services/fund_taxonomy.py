"""
Fixed region/theme vocabulary for fund categorisation, plus the display label.
Dependency-free so schemas can import it without import cycles.
"""
from __future__ import annotations

REGIONS = [
    "US", "Global", "Global Developed", "Europe", "Japan", "China", "Asia ex-Japan",
    "Asia Pacific", "India", "Vietnam", "Emerging Markets", "Thailand",
]
THEMES = [
    "Broad Market", "AI", "Technology", "Semiconductor", "Healthcare", "Clean Energy",
    "Gold & Precious Metals", "Commodities", "Dividend", "Infrastructure", "Property & REITs",
    "Consumer", "Financials", "ESG", "Small & Mid Cap", "Fixed Income", "Money Market",
    "Multi-Asset",
]
# Themes that just restate the asset class — not shown as a label prefix
_UNPREFIXED_THEMES = {"Broad Market", "Fixed Income", "Money Market", "Multi-Asset"}


def category_label(theme: str | None, region: str | None, asset_class: str | None) -> str | None:
    """e.g. ("AI", "US", "Equity") → "AI · US Equity"; Broad Market → "US Equity"."""
    if not region:
        return theme
    base = f"{region} {asset_class}" if asset_class else region
    if theme and theme not in _UNPREFIXED_THEMES:
        return f"{theme} · {base}"
    return base
