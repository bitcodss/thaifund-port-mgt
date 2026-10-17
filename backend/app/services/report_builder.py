"""
Phase 3 — monthly report builder.

facts (report_facts) → news (weekly digests of each fund's news tickers, ranked by HOLDING
WEIGHT; a ticker that appears in 2+ held funds is a shared driver, told once in the overview)
→ narrative (local LLM, Thai) → HTML.

Every LLM section is checked before use and retried with the problems spelled out:
- every number must appear in the facts given to it (number_guard),
- no buy / sell / switch advice (signal language only),
- for a fund with locked RMF / SSF lots: never mention selling or redeeming it.
After the retries a code-built template is used, so a report never contains unchecked text.
All numbers in tables come from code, not from the LLM.
"""
from __future__ import annotations

import html
import json
import logging
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.fund import Fund
from app.models.market import FundProxy, MonthlyReport, NewsDigest
from app.models.portfolio import Portfolio
from app.models.tax_lot import SyncJob, TaxLot
from app.models.user import User
from app.services import report_facts as rf
from app.services.llm import LlmError, generate_json
from app.services.number_guard import allowed_from_text, unsupported_numbers

logger = logging.getLogger(__name__)

RETRIES = 2
NEWS_PER_FUND = 3            # own (non-shared) news tickers per fund card, by holding weight
DIGESTS_PER_TICKER = 2       # latest weekly digests of the month per ticker
SHARED_MIN_FUNDS = 2         # a ticker in this many held funds is told once, in the overview

THAI_MONTHS = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม",
               "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
RULE_TH = {
    "abnormal_1m": "ผลตอบแทน 1 เดือนลดลงผิดปกติเมื่อเทียบกับความผันผวนของกองเอง",
    "abnormal_3m": "ผลตอบแทน 3 เดือนลดลงผิดปกติเมื่อเทียบกับความผันผวนของกองเอง",
    "bottom_3m": "อยู่กลุ่มท้ายของ peer group (ผลตอบแทน 3 เดือน) สองเดือนติด",
    "loss_10pct": "ขาดทุนตั้งแต่เข้ากองนี้ 10% ขึ้นไป",
}
# selling / redeeming wording (not "แรงขาย" = selling pressure in market news)
_SELL_RX = re.compile(r"ขาย(?:ออก|คืน|หน่วย|กอง|ทิ้ง)|ไถ่ถอน|\bsell\b|\bredeem", re.I)
_ADVICE_RX = re.compile(r"ควร(?:ซื้อ|ขาย|สับเปลี่ยน|ถือ)|แนะนำให้(?:ซื้อ|ขาย|สับเปลี่ยน|ถือ)|"
                        r"\bshould (?:buy|sell|switch|hold)\b", re.I)


# ── formatting ────────────────────────────────────────────────────────────────

def thai_month(d: date | str) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{THAI_MONTHS[d.month - 1]} {d.year}"


def pct(v: float | None) -> str:
    return "–" if v is None else f"{v * 100:+.1f}%"


def money(v: float | None) -> str:
    return "–" if v is None else f"฿{v:,.0f}"


def peer_cell(p: dict | None) -> str:
    if not p:
        return "–"
    if p["display"] == "rank":
        r = (p.get("ranks") or {}).get("3m")
        cell = f"อันดับ {r[0]}/{r[1]}" if r else "–"
    else:
        cell = "–" if p.get("percentile_3m") is None else f"pct {p['percentile_3m']:.0f}"
    return cell + (" (ทั้งหมวด)" if p.get("context_only") else "")


def sort_funds(funds: list[dict]) -> list[dict]:
    """Funds with flags first (most reasons first), then the weakest 3M return."""
    return sorted(funds, key=lambda f: (-len(f["flags"]), f["return_3m"] if f["return_3m"] is not None else 0))


# ── news ──────────────────────────────────────────────────────────────────────

async def news_for_projects(db: AsyncSession, proj_ids: set[str], month: date) -> tuple[dict, dict]:
    """
    (per project: [{ticker, label, weight, digests}] by holding weight, ticker → set of projects).
    Only tickers with an ok digest for a week overlapping the month count.
    """
    proxies = (await db.execute(select(FundProxy).where(
        FundProxy.sec_proj_id.in_(proj_ids), FundProxy.kind == "news", FundProxy.valid.is_(True)))).scalars().all()
    start = month.replace(day=1) - timedelta(days=6)
    digests: dict[str, list[NewsDigest]] = defaultdict(list)
    for d in (await db.execute(select(NewsDigest).where(
            NewsDigest.ticker.in_({p.ticker for p in proxies}), NewsDigest.status == "ok",
            NewsDigest.week_start >= start, NewsDigest.week_start <= month)
            .order_by(NewsDigest.week_start.desc()))).scalars():
        digests[d.ticker].append(d)
    per_proj: dict[str, list[dict]] = defaultdict(list)
    users: dict[str, set[str]] = defaultdict(set)
    for p in sorted(proxies, key=lambda p: (p.weight_pct is None, -(float(p.weight_pct or 0)), p.ticker)):
        if not digests.get(p.ticker):
            continue
        per_proj[p.sec_proj_id].append({
            "ticker": p.ticker, "label": p.label, "weight_pct": float(p.weight_pct) if p.weight_pct else None,
            "verified": p.source != "ai",
            "digests": [{"week_of": str(d.week_start), "sentiment": d.sentiment, "summary": d.summary}
                        for d in digests[p.ticker][:DIGESTS_PER_TICKER]]})
        users[p.ticker].add(p.sec_proj_id)
    return per_proj, users


# ── LLM sections ──────────────────────────────────────────────────────────────

_FUND_PROMPT = """You write one fund card of a monthly portfolio report for a Thai retail investor.
Write in Thai; keep technical terms in English (percentile, peer group, hedge, NAV, FX).
Use ONLY the facts JSON below; add no outside knowledge.
Rules:
- Copy numbers exactly as written in the facts (digits, same rounding). Never compute new numbers.
  Never write numbers as words.
- Signal language only: say what happened and what to watch. Never tell the reader to buy, sell, switch or hold.
- If "constraints" is present: never mention selling or redeeming this fund. Only when "flags" is not empty
  you may state, as information and not advice, that the moves available without breaking the tax
  conditions are those in constraints.allowed_moves.
- A flag bottom_3m with single_event = true: say the 3-month result comes mainly from the drop in its
  driver month, not from persistent weakness. With single_event = false, say it was weak across months.
- peers.context_only = true: say the comparison is with the whole AIMC category because the style group
  has too few funds, so the rank is context only.
- peers.display = "rank": use the rank (อันดับ X จาก N), not a percentile.
- news: summarise only the "news" items (largest holdings first). For "shared_news" only name them and
  say they are covered in the portfolio overview. No news → empty string.
- Fields: headline (1 sentence), performance (2-3 sentences), peers (1-2 sentences),
  news (0-3 sentences), watch (1-2 sentences on what to watch next month).
{retry_note}
Facts (JSON):
{payload}"""

_FUND_FIELDS = ("headline", "performance", "peers", "news", "watch")

_OVERVIEW_PROMPT = """You write the overview of a monthly portfolio report for a Thai retail investor.
Write in Thai; keep technical terms in English.
Use ONLY the facts JSON below; add no outside knowledge.
Rules:
- Copy numbers exactly as written in the facts. Never compute new numbers. Never write numbers as words.
- Signal language only. Never tell the reader to buy, sell, switch or hold.
- summary (2-4 sentences): the month for each portfolio and how many funds are on the watchlist.
- shared_news (0-4 sentences): the shared drivers — news that moves several held funds at once, naming the
  funds affected. Empty string when there are none.
- watch (1-3 sentences): what to watch next month. If rmf_purchase.warn is true you MUST say that no RMF
  purchase is recorded for last year or this year yet, and that pausing RMF purchases for more than one
  consecutive year breaks the RMF tax conditions (based on purchases recorded in this app).
{retry_note}
Facts (JSON):
{payload}"""

_OVERVIEW_FIELDS = ("summary", "shared_news", "watch")


def _schema(fields: tuple[str, ...]) -> dict:
    return {"type": "object", "properties": {f: {"type": "string"} for f in fields}, "required": list(fields)}


def check_text(text: str, allowed, locked: bool) -> list[str]:
    """Problems that make an LLM section unusable (empty list = OK)."""
    problems = []
    if bad := unsupported_numbers(text, allowed):
        problems.append(f"numbers not in the facts: {', '.join(bad)}")
    if _ADVICE_RX.search(text):
        problems.append("buy/sell/switch/hold advice")
    if locked and _SELL_RX.search(text):
        problems.append("mentions selling or redeeming a fund whose RMF/SSF lots are locked")
    return problems


async def write_section(prompt: str, payload: dict, fields: tuple[str, ...], locked: bool) -> tuple[dict | None, str | None]:
    text_payload = json.dumps(payload, ensure_ascii=False)
    allowed = allowed_from_text(text_payload)
    retry_note, problems = "", []
    for _ in range(RETRIES + 1):
        try:
            out = await generate_json(prompt.format(payload=text_payload, retry_note=retry_note), _schema(fields))
        except LlmError as e:
            return None, str(e)
        section = {f: (out.get(f) or "").strip() for f in fields}
        problems = check_text(" ".join(section.values()), allowed, locked)
        if not problems:
            return section, None
        retry_note = "Your previous answer had problems: " + "; ".join(problems) + ". Fix them."
    return None, "; ".join(problems)


# ── payloads + templates ──────────────────────────────────────────────────────

def fund_payload(f: dict, news: list[dict], shared: list[dict]) -> dict:
    p = f["peers"] or {}
    flags = []
    for fl in f["flags"]:
        item = {"rule": fl["rule"], "meaning": RULE_TH[fl["rule"]]}
        if fl["rule"] == "bottom_3m":
            item.update({"single_event": fl["single_event"],
                         "driver_month": thai_month(fl["driver"]["month"]) if fl.get("driver") else None,
                         "driver_month_return": pct(fl["driver"]["return_1m"]) if fl.get("driver") else None,
                         "percentile_3m": None if fl["percentile_3m"] is None else round(fl["percentile_3m"]),
                         "rank_3m": fl.get("rank_3m")})
        elif fl["rule"] == "loss_10pct":
            item["since_entry"] = pct(fl["pnl_pct"])
        else:
            item["return"] = pct(fl.get("return_1m", fl.get("return_3m")))
        flags.append(item)
    return {
        "fund": f["fund_code"], "name": f["name_en"], "weight": pct(f["weight"]).lstrip("+"),
        "return_1m": pct(f["return_1m"]), "return_3m": pct(f["return_3m"]), "return_1y": pct(f["return_1y"]),
        "since_entry": pct(f["pnl_pct"]),
        "peers": None if not p else {
            "category": p["category"], "theme": p["theme"], "basis": p["basis"], "context_only": p["context_only"],
            "display": p["display"], "other_masters": p["peer_masters"],
            "percentile_3m": None if p["percentile_3m"] is None else round(p["percentile_3m"]),
            "rank_3m": (p.get("ranks") or {}).get("3m"),
            "peer_median_3m": pct(p["peer_median_3m"]),
            "fx_adjusted": p["hedge_basis"] == "fx_adjusted",
            "fx_effect_1m": pct(p["fx"]["effect_1m"]), "currency": p["fx"]["currency"],
            "vs_same_master_3m": pct(p["vs_same_master_3m"]) if p.get("vs_same_master_3m") is not None else None,
        },
        "flags": flags,
        "constraints": f["narrative_constraints"],
        "news": [{"ticker": n["ticker"], "name": n["label"], "holding_weight": n["weight_pct"],
                  "weeks": n["digests"]} for n in news],
        "shared_news": [{"ticker": n["ticker"], "name": n["label"]} for n in shared],
    }


def fund_template(f: dict) -> dict:
    p = f["peers"]
    if not p:
        peers = "ยังไม่มีข้อมูล peer group"
    elif p["context_only"]:
        peers = f"เทียบกับทั้งหมวด {p['category']} (กลุ่มสไตล์เดียวกันมีกองไม่พอ): {peer_cell(p)}"
    else:
        peers = f"peer group {p['theme']} ({p['category']}): {peer_cell(p)}, median 3 เดือน {pct(p['peer_median_3m'])}"
    return {
        "headline": f"{f['fund_code']}: 1 เดือน {pct(f['return_1m'])}, 3 เดือน {pct(f['return_3m'])}",
        "performance": f"ผลตอบแทน 1 เดือน {pct(f['return_1m'])}, 3 เดือน {pct(f['return_3m'])}, "
                       f"1 ปี {pct(f['return_1y'])} · ตั้งแต่เข้ากองนี้ {pct(f['pnl_pct'])}",
        "peers": peers, "news": "",
        "watch": " · ".join(RULE_TH[x["rule"]] for x in f["flags"]) or "ไม่มีสัญญาณที่ต้องจับตา",
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

_CSS = ("font-family:'Sarabun','Noto Sans Thai',Tahoma,sans-serif;color:#1f2937;max-width:760px;margin:0 auto;"
        "line-height:1.6;font-size:15px")


def _e(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def render_html(report: dict) -> str:
    out = [f'<div style="{_CSS}">',
           f'<h1 style="font-size:22px;margin:0 0 4px">รายงานกองทุนประจำเดือน {_e(thai_month(report["month"]))}</h1>',
           f'<p style="color:#6b7280;margin:0 0 16px">ข้อมูล ณ สิ้นเดือน · สร้างเมื่อ {_e(report["generated"])}</p>']
    ov = report["overview"]
    out.append(f'<p>{_e(ov["summary"])}</p>')
    rmf = report.get("rmf_purchase")
    if rmf and rmf["warn"]:
        out.append('<div style="border:1px solid #f59e0b;background:#fffbeb;padding:10px 12px;border-radius:8px;margin:12px 0">'
                   f'<b>RMF: ยังไม่พบการซื้อในปี {rmf["year"] - 1} และปี {rmf["year"]}</b><br>'
                   'การเว้นซื้อ RMF เกินกว่า 1 ปีติดต่อกันทำให้ผิดเงื่อนไขทางภาษี (ตรวจจากรายการซื้อที่บันทึกในแอปเท่านั้น)</div>')
    if report["watchlist"]:
        out.append('<h2 style="font-size:18px;margin:20px 0 8px">ควรจับตา</h2><ul style="margin:0;padding-left:20px">')
        for w in report["watchlist"]:
            out.append(f'<li><b>{_e(w["fund_code"])}</b> ({_e(w["portfolio"])}): '
                       f'{_e(" · ".join(RULE_TH[r] for r in w["reasons"]))}</li>')
        out.append("</ul>")
    if ov.get("shared_news"):
        out.append(f'<h2 style="font-size:18px;margin:20px 0 8px">ข่าวที่กระทบหลายกอง</h2><p>{_e(ov["shared_news"])}</p>')
    if ov.get("watch"):
        out.append(f'<p><b>เดือนหน้า:</b> {_e(ov["watch"])}</p>')

    for port in report["portfolios"]:
        out.append(f'<h2 style="font-size:20px;margin:28px 0 8px;border-bottom:2px solid #e5e7eb">'
                   f'พอร์ต {_e(port["name"])} · {_e(money(port["value"]))}</h2>')
        out.append('<table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="background:#f3f4f6">'
                   + "".join(f'<th style="text-align:{a};padding:4px">{h}</th>' for h, a in (
                       ("กองทุน", "left"), ("สัดส่วน", "right"), ("1M", "right"), ("3M", "right"),
                       ("1Y", "right"), ("ตั้งแต่เข้า", "right"), ("Peers 3M", "right"))) + "</tr>")
        for f in port["funds"]:
            cells = [f["fund_code"], pct(f["weight"]).lstrip("+"), pct(f["return_1m"]), pct(f["return_3m"]),
                     pct(f["return_1y"]), pct(f["pnl_pct"]), peer_cell(f["peers"])]
            out.append("<tr>" + "".join(
                f'<td style="padding:4px;border-top:1px solid #e5e7eb;text-align:{"left" if i == 0 else "right"}">{_e(c)}</td>'
                for i, c in enumerate(cells)) + "</tr>")
        out.append("</table>")
        for f in port["funds"]:
            c = f["card"]
            border = "#ef4444" if f["flags"] else "#e5e7eb"
            out.append(f'<div style="border:1px solid {border};border-radius:8px;padding:10px 12px;margin:12px 0">'
                       f'<div style="font-weight:600">{_e(c["headline"])}</div>')
            for k in ("performance", "peers", "news", "watch"):
                if c.get(k):
                    label = {"performance": "", "peers": "เทียบกับกองอื่น: ", "news": "ข่าว: ", "watch": "จับตา: "}[k]
                    out.append(f'<p style="margin:6px 0">{label and f"<b>{label}</b>"}{_e(c[k])}</p>')
            if f["narrative_constraints"]:
                nc = f["narrative_constraints"]
                out.append(f'<p style="margin:6px 0;color:#6b7280;font-size:13px">เงื่อนไขภาษี '
                           f'{_e("/".join(nc["locked_schemes"]))}: ครบเงื่อนไข {_e(", ".join(nc["eligible_dates"]))}</p>')
            if f["card_source"] == "template":
                out.append('<p style="margin:6px 0;color:#9ca3af;font-size:12px">(สรุปแบบอัตโนมัติ)</p>')
            out.append("</div>")
        if port["tax_rules"]:
            out.append('<h3 style="font-size:15px;margin:16px 0 4px">เงื่อนไขภาษีที่เกี่ยวข้อง</h3><ul style="font-size:13px;color:#4b5563">')
            for scheme, rule in port["tax_rules"].items():
                for n in rule["notes"]:
                    out.append(f'<li>{_e(scheme)}: {_e(n.get("th") or n["text"])}</li>')
            out.append("</ul>")

    out.append('<hr style="margin:24px 0 8px;border:none;border-top:1px solid #e5e7eb">'
               '<p style="font-size:12px;color:#6b7280">ผลตอบแทนคำนวณจาก NAV สิ้นเดือน (price return) · peer group '
               'ใช้ share class หลักของแต่ละกอง หนึ่งกองต่อ master fund · ข้อมูลเดือนก่อนเริ่มเก็บจริงสร้างย้อนหลังด้วยรายชื่อ '
               'peer ปัจจุบัน · ผลของ FX บางกองเป็นค่าประมาณ · ข้อความสรุปเขียนโดย AI จากตัวเลขที่ระบบคำนวณ '
               'และตรวจตัวเลขทุกตัวแล้ว · ไม่ใช่คำแนะนำการลงทุน</p></div>')
    return "\n".join(out)


# ── build ─────────────────────────────────────────────────────────────────────

async def build_user_report(db: AsyncSession, user_id, month: date) -> MonthlyReport:
    """Build (or rebuild) one user's report for a month-end and store it."""
    errors: list[str] = []
    ports = (await db.execute(select(Portfolio).where(Portfolio.user_id == user_id).order_by(Portfolio.name))).scalars().all()
    facts = [f for p in ports if (f := await rf.portfolio_facts(db, p.id, month))["funds"]]
    rmf = await rf.rmf_purchase_status(db, user_id, month)

    proj_ids = {f["sec_proj_id"] for pf in facts for f in pf["funds"] if f["sec_proj_id"]}
    news, ticker_projects = await news_for_projects(db, proj_ids, month)
    shared = {t for t, ps in ticker_projects.items() if len(ps) >= SHARED_MIN_FUNDS}
    code_of = {f["sec_proj_id"]: f["fund_code"] for pf in facts for f in pf["funds"]}

    portfolios, watchlist, all_template = [], [], True
    for pf in facts:
        funds = []
        for f in sort_funds(pf["funds"]):
            items = news.get(f["sec_proj_id"], [])
            own = [n for n in items if n["ticker"] not in shared][:NEWS_PER_FUND]
            shared_refs = [n for n in items if n["ticker"] in shared]
            card, err = await write_section(_FUND_PROMPT, fund_payload(f, own, shared_refs), _FUND_FIELDS,
                                            locked=bool(f["narrative_constraints"]))
            if err:
                errors.append(f"{f['fund_code']}: {err}")
            all_template &= card is None
            funds.append({**f, "card": card or fund_template(f), "card_source": "llm" if card else "template",
                          "news_tickers": [n["ticker"] for n in own], "shared_tickers": [n["ticker"] for n in shared_refs]})
        portfolios.append({"name": pf["portfolio"]["name"], "value": pf["portfolio"]["value"],
                           "cost": pf["portfolio"]["cost"], "funds": funds, "tax_rules": pf["tax_rules"]})
        watchlist += [{**w, "portfolio": pf["portfolio"]["name"]} for w in pf["watchlist"]]

    shared_news = []
    for t in sorted(shared):
        item = next(n for ps in ticker_projects[t] for n in news[ps] if n["ticker"] == t)
        shared_news.append({"ticker": t, "name": item["label"],
                            "funds": sorted(code_of[p] for p in ticker_projects[t] if p in code_of),
                            "weeks": item["digests"]})
    overview_payload = {
        "month": thai_month(month),
        "portfolios": [{"name": p["name"], "value": money(p["value"]), "funds": len(p["funds"]),
                        "watchlist": [{"fund": w["fund_code"], "reasons": [RULE_TH[r] for r in w["reasons"]]}
                                      for w in watchlist if w["portfolio"] == p["name"]]} for p in portfolios],
        "shared_news": shared_news,
        "rmf_purchase": rmf and {"warn": rmf["warn"], "year": rmf["year"]},
    }
    overview, err = await write_section(_OVERVIEW_PROMPT, overview_payload, _OVERVIEW_FIELDS, locked=False)
    if err:
        errors.append(f"overview: {err}")
    if overview is None:
        overview = {"summary": " · ".join(f"พอร์ต {p['name']} {money(p['value'])}, {len(p['funds'])} กอง"
                                          for p in portfolios),
                    "shared_news": "", "watch": ""}

    content = {"month": str(month), "generated": datetime.now(timezone.utc).astimezone(
                   timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M"),
               "overview": overview, "overview_source": "llm" if not err else "template",
               "watchlist": watchlist, "rmf_purchase": rmf, "portfolios": portfolios}
    row = (await db.execute(select(MonthlyReport).where(MonthlyReport.user_id == user_id,
                                                        MonthlyReport.month == month))).scalar_one_or_none()
    if row is None:
        row = MonthlyReport(id=uuid.uuid4(), user_id=user_id, month=month, status="ok")
        db.add(row)
    row.facts = {"portfolios": facts, "rmf_purchase": rmf, "shared_tickers": sorted(shared)}
    row.content = json.loads(json.dumps(content, default=str))
    row.html = render_html(content)
    row.status = "failed" if not facts else "partial" if errors else "ok"
    row.errors, row.model = errors or None, settings.OLLAMA_MODEL
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return row


async def build_reports(db: AsyncSession, month: date, user_ids: list | None = None) -> dict:
    """Reports for every active user holding funds (or the given users), tracked as a sync job."""
    q = select(User.id).where(User.is_active.is_(True))
    if user_ids:
        q = q.where(User.id.in_(user_ids))
    holders = {u for (u,) in (await db.execute(
        select(Portfolio.user_id).join(TaxLot, TaxLot.portfolio_id == Portfolio.id).where(TaxLot.units_remaining > 0)
        .distinct())).all()}
    users = [u for (u,) in (await db.execute(q)).all() if u in holders]
    job = SyncJob(id=uuid.uuid4(), type="monthly_reports", started_at=datetime.now(timezone.utc), status="running")
    db.add(job)
    await db.commit()
    counts = {"ok": 0, "partial": 0, "failed": 0}
    for uid in users:
        row = await build_user_report(db, uid, month)
        counts[row.status] += 1
    job = await db.get(SyncJob, job.id)
    job.completed_at, job.status = datetime.now(timezone.utc), "success" if not counts["failed"] else "error"
    job.error_message = f"{month}: " + " ".join(f"{k}:{v}" for k, v in counts.items())
    await db.commit()
    return counts
