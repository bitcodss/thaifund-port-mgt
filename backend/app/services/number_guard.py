"""
Check that every number an LLM wrote is backed by the facts it was given.

Prompt rules alone don't stop a local model from inventing figures, so output
is verified after generation: digits (incl. Thai digits ๐-๙) must match an
allowed value within rounding tolerance, and numbers spelled out in words
("สิบเปอร์เซ็นต์", "ร้อยละห้า", "ten percent") are rejected outright because
they can't be verified — the prompt requires Arabic digits.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable

_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_NUMBER_RE = re.compile(r"(?<![\w.])[-+−]?\d[\d,]*(?:\.\d+)?")
_THAI_NUMBER_WORDS = "ศูนย์|หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า|สิบ|ยี่สิบ|ร้อย|พัน|หมื่น|แสน|ล้าน|ครึ่ง"
_SPELLED_PATTERNS = [
    re.compile(rf"ร้อยละ\s*(?:{_THAI_NUMBER_WORDS})"),
    re.compile(rf"(?:{_THAI_NUMBER_WORDS})+\s*(?:เปอร์เซ็นต์|เปอร์เซ็น|%)"),
    re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|"
               r"thirty|forty|fifty|hundred|half)\b[\s-]*(?:percent|per cent|%)", re.IGNORECASE),
]
_YEAR = range(1900, 2201)


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "").replace("−", "-").replace("+", ""))
    except InvalidOperation:
        return None


def extract_numbers(text: str) -> list[tuple[str, Decimal]]:
    """[(as_written, value)] for every digit-number in text (Thai digits normalised)."""
    out = []
    for m in _NUMBER_RE.finditer(text.translate(_THAI_DIGITS)):
        v = _to_decimal(m.group())
        if v is not None:
            out.append((m.group(), v))
    return out


def allowed_from_text(*texts: str) -> set[Decimal]:
    return {abs(v) for t in texts if t for _, v in extract_numbers(t)}


def spelled_numbers(text: str) -> list[str]:
    return [m.group() for p in _SPELLED_PATTERNS for m in p.finditer(text)]


def _matches(v: Decimal, allowed: set[Decimal]) -> bool:
    for a in allowed:
        # rounding tolerance: 12.345 may be written 12.3 or 12.35; 1,234,567 as 1,234,570
        tol = max(Decimal("0.051"), abs(a) * Decimal("0.005"))
        if abs(v - a) <= tol:
            return True
    return False


def unsupported_numbers(text: str, allowed: Iterable[Decimal], allow_small_ints: int = 12) -> list[str]:
    """
    Numbers in `text` not backed by `allowed` (compared by absolute value, since
    "fell 5%" and "-5%" are the same fact). Integers 0..allow_small_ints
    ("3 months", "top 5") and plausible years are always accepted.
    Spelled-out numbers are reported too.
    """
    allowed_set = {abs(Decimal(str(a))) for a in allowed}
    bad = []
    for raw, v in extract_numbers(text):
        av = abs(v)
        if av == av.to_integral_value() and (av <= allow_small_ints or int(av) in _YEAR):
            continue
        if not _matches(av, allowed_set):
            bad.append(raw)
    return bad + spelled_numbers(text)
