"""
Peer-group helpers (Phase 2).

master_key(): one peer per master fund. Thai feeders of the same master are the
same exposure (12 Thai funds feed iShares MSCI ACWI), so peer stats count it once.
The master ISIN parsed from the factsheet is authoritative; the master fund NAME
is only a fallback, because the same master is written differently per share
class and AMC ("…Fund A2 USD" vs "…Fund, Class I (acc) - USD").
"""
from __future__ import annotations

import re

_CURRENCIES = r"USD|EUR|JPY|GBP|HKD|SGD|AUD|CNY|CNH|CHF|THB"
_SHARE_CLASS = re.compile(
    r"\b(CLASS|SHARE CLASS|SHARES?|UNITS?)\b(\s+[A-Z0-9]{1,4}\b)?"      # "Class I", "Class INST", "Shares"
    r"|\b(ACC|ACCUMULATION|ACCUMULATING|DIST|DISTRIBUTION|DISTRIBUTING|INST|INSTITUTIONAL|"
    r"HEDGED|UNHEDGED|H|UH)\b"
    r"|\b(" + _CURRENCIES + r")\b"
    r"|\b(?=[A-Z]*\d)[A-Z]{1,2}\d{1,2}\b"                               # A2, I1, X1, IA2 …
    r"|\b(I|IA|IB|A|B|C|D|E|F|P|R|S|T|W|X|Y|Z)\b(?=\s*$)",              # trailing single-letter class
    re.I,
)


def normalise_master_name(name: str | None) -> str | None:
    if not name:
        return None
    n = re.sub(r"[฀-๿]+", " ", name)            # Thai text ("กองทุน …")
    n = re.sub(r"[–—]", "-", n)
    n = re.sub(r"[^A-Za-z0-9 ]+", " ", n).upper()
    prev = None
    while prev != n:                                       # class tokens can be stacked
        prev = n
        n = re.sub(r"\s+", " ", _SHARE_CLASS.sub(" ", n)).strip()
    return n or None


def master_key(master_isin: str | None, master_name: str | None) -> tuple[str, str] | None:
    """("isin", ISIN) when known, else ("name", normalised name); None for non-feeders."""
    if master_isin:
        return "isin", master_isin.strip().upper()
    norm = normalise_master_name(master_name)
    return ("name", norm) if norm else None
