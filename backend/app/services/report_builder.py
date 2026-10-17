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
# JSON keys leaking into prose ("return_3m", "since_entry") and invented horizons ("31-month")
_KEY_RX = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
_HORIZON_RX = re.compile(r"\d+\s*-?\s*months?\b", re.I)
MOVES_TH = {
    "hold": "ถือต่อ",
    "switch to another RMF of the same AMC": "สับเปลี่ยนไป RMF กองอื่นใน บลจ. เดียวกัน",
    "transfer to another AMC's RMF": "โอนย้ายไป RMF ของ บลจ. อื่น",
    "switch to another SSF of the same AMC": "สับเปลี่ยนไป SSF กองอื่นใน บลจ. เดียวกัน",
    "transfer to another AMC's SSF (within 5 business days)": "โอนย้ายไป SSF ของ บลจ. อื่น (ต้องเสร็จภายใน 5 วันทำการ)",
}
MIN_FX_MENTION = 0.001       # FX effect below 0.1% isn't worth a sentence
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
Write natural Thai; keep technical terms in English (percentile, peer group, hedge, NAV, FX).
Use ONLY the facts JSON below; add no outside knowledge.
Rules:
- Copy numbers exactly as written in the facts. Never compute new numbers, never write numbers as words,
  never invent periods (only 1 เดือน, 3 เดือน, 1 ปี and "ตั้งแต่เข้ากองนี้" exist).
- Never write the JSON keys themselves; write normal Thai sentences.
- Signal language only: say what happened and what to watch. Never tell the reader to buy, sell, switch or hold.
- "สัญญาณที่ต้องจับตา": explain these signals using the sentences given, in your own words but with the
  same meaning. Do NOT add signals or explanations that are not listed. If the list is empty, do not
  describe the fund as weak or as having a problem.
- "ข้อจำกัดภาษี" (when present): never mention selling or redeeming this fund. Only when
  "สัญญาณที่ต้องจับตา" is not empty, you may add, as information and not advice, that the moves that keep
  the tax conditions are those listed in "ทางเลือกที่ไม่ผิดเงื่อนไข".
- "เทียบกับกองอื่น": follow "วิธีเทียบ" exactly (it says whether the rank is only context).
- news: summarise only "ข่าว" (largest holdings first). For "ข่าวที่เล่าในภาพรวมพอร์ต" only name them and
  say they are covered in the portfolio overview. No news → empty string.
- Fields: headline (1 sentence), performance (2-3 sentences), peers (1-2 sentences),
  news (0-3 sentences), watch (1-2 sentences on what to watch next month).
{retry_note}
Facts (JSON):
{payload}"""

_FUND_FIELDS = ("headline", "performance", "peers", "news", "watch")

_OVERVIEW_PROMPT = """You write the overview of a monthly portfolio report for a Thai retail investor.
Write natural Thai; keep technical terms in English.
Use ONLY the facts JSON below; add no outside knowledge.
Rules:
- Copy numbers exactly as written in the facts. Never compute new numbers. Never write numbers as words.
- Never write the JSON keys themselves; write normal Thai sentences.
- Signal language only. Never tell the reader to buy, sell, switch or hold.
- summary (2-4 sentences): each portfolio's value and how many funds are on the watch list, naming them.
- shared_news (0-4 sentences): "ข่าวที่กระทบหลายกอง" — news moving several held funds at once, naming the
  funds affected. Empty string when there are none.
- watch (1-3 sentences): what to watch next month. If "เตือน RMF" is present you MUST include it.
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
    if keys := _KEY_RX.findall(text):
        problems.append(f"JSON keys in the text ({', '.join(sorted(set(keys)))}) — write Thai instead")
    if _HORIZON_RX.search(text):
        problems.append("a period that is not in the facts (only 1 เดือน, 3 เดือน, 1 ปี)")
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

def signal_sentences(f: dict) -> list[str]:
    """Each flag as a Thai sentence written by code — the LLM rephrases, never derives."""
    out = []
    for fl in f["flags"]:
        if fl["rule"] == "abnormal_1m":
            out.append(f"ผลตอบแทน 1 เดือน {pct(fl['return_1m'])} ต่ำผิดปกติเมื่อเทียบกับความผันผวนของกองเอง")
        elif fl["rule"] == "abnormal_3m":
            out.append(f"ผลตอบแทน 3 เดือน {pct(fl['return_3m'])} ต่ำผิดปกติเมื่อเทียบกับความผันผวนของกองเอง")
        elif fl["rule"] == "loss_10pct":
            out.append(f"ขาดทุน {pct(fl['pnl_pct'])} นับตั้งแต่เข้ากองนี้")
        elif fl["rule"] == "bottom_3m":
            pos = (f"อันดับ {fl['rank_3m'][0]} จาก {fl['rank_3m'][1]}" if fl.get("display") == "rank" and fl.get("rank_3m")
                   else f"percentile {fl['percentile_3m']:.0f}")
            text = f"ผลตอบแทน 3 เดือนอยู่กลุ่มท้ายของ peer group สองเดือนติด ({pos})"
            d = fl.get("driver")
            if fl["single_event"] and d:
                text += (f" สาเหตุหลักคือการลดลง {pct(d['return_1m'])} ในเดือน{thai_month(d['month'])} เดือนเดียว"
                         " ไม่ใช่ผลงานแย่ต่อเนื่อง")
            else:
                text += " ผลงานอ่อนกว่ากลุ่มในหลายเดือน ไม่ได้มาจากเดือนเดียว"
            out.append(text)
    return out


def peer_method(p: dict) -> str:
    if p["context_only"]:
        return (f"เทียบกับทั้งหมวด {p['category']} เพราะกลุ่มสไตล์เดียวกันมีกองไม่พอ อันดับนี้ใช้ประกอบเท่านั้น "
                "ไม่ได้บอกว่ากองทำได้ดีหรือแย่ในสไตล์ของตัวเอง")
    return f"เทียบใน peer group ธีม {p['theme']} ของหมวด {p['category']} ({p['peer_masters']} กองหลักอื่น)"


def fund_payload(f: dict, news: list[dict], shared: list[dict]) -> dict:
    p = f["peers"]
    peers = None
    if p:
        r = (p.get("ranks") or {}).get("3m")
        peers = {"วิธีเทียบ": peer_method(p),
                 "ผล 3 เดือน": (f"อันดับ {r[0]} จาก {r[1]}" if p["display"] == "rank" and r else
                                None if p["percentile_3m"] is None else
                                f"percentile {p['percentile_3m']:.0f} (0 = แย่สุด, 100 = ดีสุด)"),
                 "median ของกลุ่ม 3 เดือน": pct(p["peer_median_3m"])}
        if p["hedge_basis"] == "fx_adjusted":
            peers["หมายเหตุ"] = "ปรับผลของค่าเงินให้เทียบกันได้แล้ว เพราะกองในกลุ่มป้องกันความเสี่ยงค่าเงินต่างกัน"
        fx = p["fx"]["effect_1m"]
        if fx is not None and abs(fx) >= MIN_FX_MENTION:
            peers[f"ผลของค่าเงิน ({p['fx']['currency']}) ต่อผลตอบแทน 1 เดือน"] = pct(fx)
        if p.get("vs_same_master_3m") is not None:
            peers["ต่างจาก feeder อื่นของกองหลักเดียวกัน (3 เดือน)"] = pct(p["vs_same_master_3m"])
    out = {
        "กองทุน": f["fund_code"], "ชื่อ": f["name_en"], "สัดส่วนในพอร์ต": pct(f["weight"]).lstrip("+"),
        "ผลตอบแทน": {"1 เดือน": pct(f["return_1m"]), "3 เดือน": pct(f["return_3m"]), "1 ปี": pct(f["return_1y"])},
        "กำไรขาดทุนตั้งแต่เข้ากองนี้": pct(f["pnl_pct"]),
        "เทียบกับกองอื่น": peers,
        "สัญญาณที่ต้องจับตา": signal_sentences(f),
        "ข่าว": [{"ชื่อ": n["label"] or n["ticker"], "สัดส่วนใน top holdings": n["weight_pct"],
                  "สรุปรายสัปดาห์": [w["summary"] for w in n["digests"]]} for n in news],
        "ข่าวที่เล่าในภาพรวมพอร์ต": [n["label"] or n["ticker"] for n in shared],
    }
    nc = f["narrative_constraints"]
    if nc:
        out["ข้อจำกัดภาษี"] = {"ห้ามพูดถึง": "การขายหรือไถ่ถอนกองนี้",
                               "ทางเลือกที่ไม่ผิดเงื่อนไข": [MOVES_TH.get(m, m) for m in nc["allowed_moves"]]}
    return out


def fund_template(f: dict) -> dict:
    p = f["peers"]
    if not p:
        peers = "ยังไม่มีข้อมูล peer group"
    elif p["context_only"]:
        peers = f"{peer_method(p)}: {peer_cell(p)}"
    else:
        peers = f"peer group {p['theme']} ({p['category']}): {peer_cell(p)}, median 3 เดือน {pct(p['peer_median_3m'])}"
    return {
        "headline": f"{f['fund_code']}: 1 เดือน {pct(f['return_1m'])}, 3 เดือน {pct(f['return_3m'])}",
        "performance": f"ผลตอบแทน 1 เดือน {pct(f['return_1m'])}, 3 เดือน {pct(f['return_3m'])}, "
                       f"1 ปี {pct(f['return_1y'])} · ตั้งแต่เข้ากองนี้ {pct(f['pnl_pct'])}",
        "peers": peers, "news": "",
        "watch": " · ".join(signal_sentences(f)) or "ไม่มีสัญญาณที่ต้องจับตา",
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
        "เดือน": thai_month(month),
        "พอร์ต": [{"ชื่อ": p["name"], "มูลค่า": money(p["value"]), "จำนวนกอง": len(p["funds"]),
                   "กองที่ควรจับตา": [{"กอง": w["fund_code"], "เหตุผล": [RULE_TH[r] for r in w["reasons"]]}
                                      for w in watchlist if w["portfolio"] == p["name"]]} for p in portfolios],
        "ข่าวที่กระทบหลายกอง": [{"ชื่อ": n["name"] or n["ticker"], "กองที่ได้รับผล": n["funds"],
                                 "สรุปรายสัปดาห์": [w["summary"] for w in n["weeks"]]} for n in shared_news],
    }
    if rmf and rmf["warn"]:
        overview_payload["เตือน RMF"] = (f"ยังไม่พบการซื้อ RMF ในปี {rmf['year'] - 1} และปี {rmf['year']} "
                                         "การเว้นซื้อเกินกว่า 1 ปีติดต่อกันทำให้ผิดเงื่อนไขทางภาษี "
                                         "(ตรวจจากรายการที่บันทึกในแอปเท่านั้น)")
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
