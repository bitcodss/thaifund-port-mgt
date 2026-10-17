"""FX-exposure currency of a Thai fund, from SEC text (no I/O — shared by peer_service and peer_metrics)."""
from __future__ import annotations

import re

_THAI_CCY = (("USD", r"ดอลล[า่]+ร์?\s*สหรัฐ|ดอลล[า่]+ร์?\s*(?:สรอ|US)"), ("HKD", r"ดอลล[า่]+ร์?\s*ฮ่องกง"),
             ("SGD", r"ดอลล[า่]+ร์?\s*สิงคโปร์"), ("EUR", r"ยูโร"), ("JPY", r"เยน"), ("CNY", r"หยวน"),
             ("GBP", r"ปอนด์"))
_CCY_CODE = re.compile(r"\b(USD|EUR|JPY|HKD|CNY|CNH|GBP|SGD)\b")
_REGION_CCY = {"Japan Equity": "JPY", "European Equity": "EUR", "China Equity - A Shares": "CNY",
               "Greater China Equity": "HKD"}


def _code(c: str) -> str:
    return "CNY" if c == "CNH" else c


def currency_from_text(policy: str, master: str | None, category: str | None) -> tuple[str, str]:
    """
    (currency, source): "สกุลเงิน… / เงินสกุล…" or a share class "(USD)" in the SEC investment
    policy, else a code in the master's name, else US-listed stocks → USD, else the category's
    region, else USD ("default" = assumed).
    """
    for m in re.finditer(r"สกุล(?:เงิน)?", policy):
        window = policy[m.end():m.end() + 40]
        if code := _CCY_CODE.search(window):
            return _code(code.group(1)), "policy"
        for ccy, rx in _THAI_CCY:
            if re.match(r"\s*(?:" + rx + ")", window):
                return ccy, "policy"
    if code := re.search(r"class\W{0,3}[A-Z0-9 ]{0,12}\(\s*(USD|EUR|JPY|HKD|CNY|CNH|GBP|SGD)\s*\)", policy, re.I):
        return _code(code.group(1).upper()), "policy"
    if code := _CCY_CODE.search(master or ""):
        return _code(code.group(1)), "master_name"
    if re.search(r"ตลาดหลักทรัพย์(?:ของ)?(?:ประเทศ)?สหรัฐ", policy):
        return "USD", "listing"
    if category in _REGION_CCY:
        return _REGION_CCY[category], "region"
    return "USD", "default"
