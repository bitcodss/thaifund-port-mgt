"""
Chunk 4 — monthly peer metrics per SEC project (its representative share class).

For each month-end: NAV price returns over 1M / 3M / 6M / 1Y, the peer median and the
percentile among the OTHER masters of the fund's peer group (peer_service.select_members),
z-scores vs the fund's own history, the FX effect, and the other feeders of the same master.

Hedge handling: each peer master is valued at the median of its feeders in the SAME hedge
group as the fund. When fewer than MIN_THEME_GROUP masters (incl. the fund's own) have such
a feeder, every master in the group is used and feeders of another hedge group are adjusted
to the fund's hedge ratio: r + (h_peer − h_fund) × FX return of the PEER's currency
("fx_adjusted"). Hedge ratios are the factsheet's actual "FX Hedging xx%" when printed,
otherwise an estimate by hedge group (flagged). Currency per fund: the master fund's share-class
currency stated in the SEC investment policy / master name, else the category's region
(Japan → JPY, Europe → EUR, …), else USD.

Groups smaller than SMALL_GROUP masters (incl. the fund) are shown as a rank ("4 of 5")
instead of a percentile; there the bottom flag means last place only.

Backfilled snapshots are computed from stored NAV with TODAY's peer lists and are marked so.
"""
from __future__ import annotations

import calendar
import logging
import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fund import Fund, NavHistory
from app.models.market import FundFactsheet, FxRate, PeerProfile, PeerSnapshot
from app.services import yahoo_client
from app.services.peer_service import _member_key, is_approved, select_members
from app.services.peer_taxonomy import MIN_THEME_GROUP

logger = logging.getLogger(__name__)

HORIZONS = {"1m": 1, "3m": 3, "6m": 6, "1y": 12}
SMALL_GROUP = 8             # masters incl. the fund: below this, rank instead of percentile
FX_TICKERS = {"USD": "THB=X", "EUR": "EURTHB=X", "JPY": "JPYTHB=X", "HKD": "HKDTHB=X",
              "CNY": "CNYTHB=X", "GBP": "GBPTHB=X", "SGD": "SGDTHB=X"}
MIN_FX_ROWS = 200            # fewer daily rows from a direct pair → use the USD cross rate
_THAI_CCY = (("USD", r"ดอลล[า่]+ร์?\s*สหรัฐ|ดอลล[า่]+ร์?\s*(?:สรอ|US)"), ("HKD", r"ดอลล[า่]+ร์?\s*ฮ่องกง"),
             ("SGD", r"ดอลล[า่]+ร์?\s*สิงคโปร์"), ("EUR", r"ยูโร"), ("JPY", r"เยน"), ("CNY", r"หยวน"),
             ("GBP", r"ปอนด์"))
_CCY_CODE = re.compile(r"\b(USD|EUR|JPY|HKD|CNY|CNH|GBP|SGD)\b")
_REGION_CCY = {"Japan Equity": "JPY", "European Equity": "EUR", "China Equity - A Shares": "CNY",
               "Greater China Equity": "HKD"}
STALE_DAYS = 7              # a month-end NAV older than this counts as missing
MIN_PEERS = MIN_THEME_GROUP - 1   # other masters needed for a percentile (group of 5 incl. the fund)
MIN_Z_OBS = 12              # prior observations for an own-history z-score
# Hedge ratio by group when the factsheet prints no actual % (estimate, flagged as such)
ESTIMATED_HEDGE = {"unhedged": 0.0, "full": 1.0, "dynamic": 0.5, "partial": 0.5, "unknown": 0.5}


# ── dates ─────────────────────────────────────────────────────────────────────

def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def months_back(m: date, k: int) -> date:
    y, mo = divmod(m.year * 12 + m.month - 1 - k, 12)
    return month_end(date(y, mo + 1, 1))


def last_complete_month(today: date) -> date:
    return months_back(month_end(today), 1)


# ── pure helpers ──────────────────────────────────────────────────────────────

def hedge_ratio(group: str | None, actual_pct: Decimal | None) -> tuple[float, bool]:
    """(share of FX exposure hedged, estimated?). Domestic funds have no FX exposure (1.0)."""
    if group == "domestic":
        return 1.0, False
    if actual_pct is not None:
        return min(max(float(actual_pct) / 100, 0.0), 1.0), False
    return ESTIMATED_HEDGE.get(group or "unknown", 0.5), True


def fund_currency(p: PeerProfile) -> tuple[str, str]:
    """(currency, source) — master share-class currency from SEC policy / master name, else region, else USD."""
    data = p.classifier_input or {}
    policy = data.get("policy") or ""
    for m in re.finditer(r"สกุลเงิน", policy):
        window = policy[m.end():m.end() + 40]
        if code := _CCY_CODE.search(window):
            return ("CNY" if code.group(1) == "CNH" else code.group(1)), "policy"
        for ccy, rx in _THAI_CCY:
            if re.match(r"\s*(?:" + rx + ")", window):
                return ccy, "policy"
    if code := _CCY_CODE.search(data.get("master_fund") or ""):
        return ("CNY" if code.group(1) == "CNH" else code.group(1)), "master_name"
    if p.aimc_category in _REGION_CCY:
        return _REGION_CCY[p.aimc_category], "region"
    return "USD", "default"


def rank(value: float, others: list[float]) -> tuple[int, int]:
    """(rank, group size) — 1 = best; the fund counts in the group."""
    return 1 + sum(1 for o in others if o > value), len(others) + 1


def percentile(value: float, others: list[float]) -> float:
    """Share of peers doing worse (ties count half), 0 = worst … 100 = best."""
    below = sum(1 for o in others if o < value)
    equal = sum(1 for o in others if o == value)
    return 100 * (below + 0.5 * equal) / len(others)


def zscore(value: float, history: list[float]) -> float | None:
    if len(history) < MIN_Z_OBS:
        return None
    sd = statistics.stdev(history)
    return (value - statistics.mean(history)) / sd if sd > 0 else None


class MonthEnds:
    """Last value on or before each month-end (None when older than STALE_DAYS)."""

    def __init__(self, rows: list[tuple[date, float]]):
        self.by_month: dict[date, tuple[date, float]] = {}
        for d, v in sorted(rows):
            self.by_month[month_end(d)] = (d, v)

    def at(self, m: date) -> float | None:
        hit = self.by_month.get(m)
        return hit[1] if hit and (m - hit[0]).days <= STALE_DAYS else None

    def ret(self, m: date, k: int) -> float | None:
        end, start = self.at(m), self.at(months_back(m, k))
        return end / start - 1 if end is not None and start else None


# ── data loading ──────────────────────────────────────────────────────────────

async def sync_fx(db: AsyncSession) -> dict[str, int]:
    """Daily closes vs THB for every currency in FX_TICKERS (Yahoo, 2 years) → fx_rates."""
    out = {}
    usd: dict[date, float] = {}
    for ccy, ticker in FX_TICKERS.items():
        try:
            rows = await yahoo_client.history(ticker, "2y")
            if ccy == "USD":
                usd = dict(rows)
            elif len(rows) < MIN_FX_ROWS and usd:
                # thin direct pair (Yahoo CNYTHB=X has ~1 row) → cross via USD: USDTHB / USDccy
                cross = dict(await yahoo_client.history(f"{ccy}=X", "2y"))
                rows = [(d, usd[d] / v) for d, v in sorted(cross.items()) if d in usd and v]
        except yahoo_client.YahooError as e:
            logger.warning("FX %s sync failed: %s", ccy, e)
            continue
        for d, v in rows:
            await db.merge(FxRate(pair=f"{ccy}THB", trade_date=d, rate=Decimal(str(round(v, 8)))))
        await db.commit()
        out[ccy] = len(rows)
    return out


def _windows(col, months: list[date]):
    """WHERE clause selecting only the last STALE_DAYS+1 days of each month (portable SQL)."""
    return or_(*[and_(col >= m - timedelta(days=STALE_DAYS), col <= m) for m in months])


async def _load_month_ends(db: AsyncSession, codes: set[str], months: list[date]) -> dict[str, MonthEnds]:
    out: dict[str, list] = defaultdict(list)
    codes_l = sorted(codes)
    for i in range(0, len(codes_l), 500):
        rows = (await db.execute(select(NavHistory.fund_code, NavHistory.trade_date, NavHistory.nav).where(
            NavHistory.fund_code.in_(codes_l[i:i + 500]), _windows(NavHistory.trade_date, months)))).all()
        for c, d, v in rows:
            out[c].append((d, float(v)))
    return {c: MonthEnds(r) for c, r in out.items()}


async def nav_codes(db: AsyncSession, profiles: list[PeerProfile]) -> dict[str, tuple[str, bool]]:
    """
    proj_id → (NAV fund_code, is_fallback). The representative class as stored, else a
    case-insensitive match (SEC "tsp4-explorer" vs stored "TSP4-EXPLORER"), else the
    project's class with the longest NAV history (representative class never synced).
    """
    counts = dict((await db.execute(select(NavHistory.fund_code, func.count()).group_by(NavHistory.fund_code))).all())
    by_upper = {c.upper(): c for c in counts}
    classes: dict[str, list[str]] = defaultdict(list)
    for pid, code in (await db.execute(select(Fund.sec_proj_id, Fund.fund_code).where(
            Fund.sec_proj_id.in_({p.sec_proj_id for p in profiles})))).all():
        classes[pid].append(code)
    out = {}
    for p in profiles:
        rep = p.representative_code or ""
        if rep in counts:
            out[p.sec_proj_id] = (rep, False)
        elif rep.upper() in by_upper:
            out[p.sec_proj_id] = (by_upper[rep.upper()], False)
        else:
            best = max((c for c in classes.get(p.sec_proj_id, []) if c in counts), key=lambda c: counts[c], default=None)
            if best:
                out[p.sec_proj_id] = (best, True)
    return out


# ── snapshot computation ──────────────────────────────────────────────────────

def compute_snapshot(me: PeerProfile, sel: dict, month: date, navs: dict[str, MonthEnds],
                     codes: dict[str, tuple[str, bool]], hedge: dict[str, tuple[float, bool]],
                     fx: dict[str, MonthEnds], same_master: list[PeerProfile]) -> dict | None:
    """One project's peer snapshot for `month` (pure). None when the fund has no 1M return."""
    code = codes.get(me.sec_proj_id, (None, False))[0]
    mine = navs.get(code) if code else None
    if mine is None or mine.ret(month, 1) is None:
        return None

    def ret(p: PeerProfile, k: int) -> float | None:
        c = codes.get(p.sec_proj_id, (None, False))[0]
        return navs[c].ret(month, k) if c in navs else None

    h_me, est_me = hedge[me.sec_proj_id]
    ccy_me, ccy_src = fund_currency(me)

    def fx_ret(p_ccy: str, k: int) -> float | None:
        series = fx.get(p_ccy) or fx.get("USD")      # missing series → USD stand-in
        return series.ret(month, k) if series else None

    own_key = _member_key(me)
    masters: dict[str, list[PeerProfile]] = defaultdict(list)
    for p in sel["members"]:
        masters[_member_key(p)].append(p)
    same_hedge_masters = {k for k, fs in masters.items() if any(f.hedge_group == me.hedge_group for f in fs)}
    mode = "same_hedge" if len(same_hedge_masters | {own_key}) >= MIN_THEME_GROUP else "fx_adjusted"
    population = {k: fs for k, fs in masters.items() if k != own_key and (mode == "fx_adjusted" or k in same_hedge_masters)}

    out = {"hedge_basis": mode, "peer_masters": len(population), "hedge_ratio": h_me, "hedge_ratio_estimated": est_me}
    adjusted_masters: set[str] = set()
    ranks: dict[str, list[int]] = {}
    for name, k in HORIZONS.items():
        r_me = mine.ret(month, k)
        values = []
        for key, feeders in population.items():
            same = [r for f in feeders if f.hedge_group == me.hedge_group and (r := ret(f, k)) is not None]
            if same:
                values.append(statistics.median(same))
                continue
            if mode != "fx_adjusted":
                continue
            # as if the peer had MY hedge ratio, on the peer's own currency exposure
            adj = [r + (hedge[f.sec_proj_id][0] - h_me) * fxr for f in feeders
                   if (r := ret(f, k)) is not None and (fxr := fx_ret(fund_currency(f)[0], k)) is not None]
            if adj:
                values.append(statistics.median(adj))
                adjusted_masters.add(key)
        out[f"ret_{name}"] = r_me
        out[f"median_{name}"] = statistics.median(values) if values else None
        ok = r_me is not None and len(values) >= MIN_PEERS
        out[f"pct_{name}"] = percentile(r_me, values) if ok else None
        if ok:
            ranks[name] = list(rank(r_me, values))
    out["fx_adjusted_masters"] = len(adjusted_masters)

    # own-history z-scores: this month vs every earlier month (1M) / earlier rolling 3M windows
    earlier = sorted(m for m in mine.by_month if m < month)
    for name, k in (("1m", 1), ("3m", 3)):
        hist = [r for m in earlier if (r := mine.ret(m, k)) is not None]
        out[f"z_{name}"] = zscore(out[f"ret_{name}"], hist) if out[f"ret_{name}"] is not None else None
    for name, k in (("1m", 1), ("3m", 3)):
        r_fx = fx_ret(ccy_me, k)
        out[f"fx_effect_{name}"] = (1 - h_me) * r_fx if r_fx is not None else None

    feeders = []
    for f in same_master:
        feeders.append({"sec_proj_id": f.sec_proj_id, "code": codes.get(f.sec_proj_id, (None,))[0],
                        "hedge_group": f.hedge_group, **{f"ret_{n}": ret(f, k) for n, k in HORIZONS.items()}})
    same_hedge = [f for f in feeders if f["hedge_group"] == me.hedge_group]
    gaps = {}
    for n in ("3m", "1y"):
        vals = [f[f"ret_{n}"] for f in same_hedge if f[f"ret_{n}"] is not None]
        if vals and out[f"ret_{n}"] is not None:
            gaps[f"vs_same_master_{n}"] = out[f"ret_{n}"] - statistics.median(vals)
    # monthly returns inside the 3M window — which month drove it (chunk 5 facts)
    months_3m = {str(months_back(month, i)): mine.ret(months_back(month, i), 1) for i in range(3)}
    out["details"] = {"same_master": feeders, **gaps, "ranks": ranks, "small_group": None,
                      "months_in_3m": months_3m, "currency": ccy_me, "currency_source": ccy_src,
                      "nav_code_fallback": codes.get(me.sec_proj_id, (None, False))[1]}
    sizes = [n for _, n in ranks.values()]
    out["details"]["small_group"] = bool(sizes) and max(sizes) < SMALL_GROUP
    return out


def _dec(v, places=8):
    return None if v is None else Decimal(str(round(v, places)))


async def build_snapshots(db: AsyncSession, months: list[date], backfilled: bool,
                          proj_ids: set[str] | None = None) -> dict:
    """
    Compute and upsert snapshots for `months` (month-end dates). A backfilled run never
    overwrites a live (non-backfilled) snapshot of the same month.
    """
    profiles = [p for p in (await db.execute(select(PeerProfile).where(PeerProfile.aimc_category.isnot(None)))).scalars()
                if is_approved(p)]
    by_cat: dict[str, list[PeerProfile]] = defaultdict(list)
    by_master: dict[str, list[PeerProfile]] = defaultdict(list)
    for p in profiles:
        by_cat[p.aimc_category].append(p)
        if p.master_key:
            by_master[p.master_key].append(p)
    targets = [p for p in profiles if proj_ids is None or p.sec_proj_id in proj_ids]

    codes = await nav_codes(db, profiles)
    earliest = min(months_back(min(months), 12), date(2015, 1, 31))
    nav_months = []
    m = month_end(earliest)
    while m <= max(months):
        nav_months.append(m)
        m = months_back(m, -1)
    navs = await _load_month_ends(db, {c for c, _ in codes.values()}, nav_months)
    fx_by: dict[str, list] = defaultdict(list)
    for pair, d, r in (await db.execute(select(FxRate.pair, FxRate.trade_date, FxRate.rate))).all():
        fx_by[pair[:3]].append((d, float(r)))
    fx = {ccy: MonthEnds(rows) for ccy, rows in fx_by.items()}
    pct = dict((await db.execute(select(FundFactsheet.sec_proj_id, FundFactsheet.fx_hedge_pct))).all())
    hedge = {p.sec_proj_id: hedge_ratio(p.hedge_group, pct.get(p.sec_proj_id)) for p in profiles}

    existing = {(s.sec_proj_id, s.month): s for s in (await db.execute(
        select(PeerSnapshot).where(PeerSnapshot.month.in_(months)))).scalars()}
    counts = {"written": 0, "no_nav": 0, "kept_live": 0}
    for me in targets:
        sel = select_members(me, by_cat[me.aimc_category])
        same_master = [f for f in by_master.get(me.master_key or "", []) if f.sec_proj_id != me.sec_proj_id]
        for month in months:
            row = existing.get((me.sec_proj_id, month))
            if row is not None and backfilled and not row.backfilled:
                counts["kept_live"] += 1
                continue
            snap = compute_snapshot(me, sel, month, navs, codes, hedge, fx, same_master)
            if snap is None:
                counts["no_nav"] += 1
                continue
            if row is None:
                row = PeerSnapshot(sec_proj_id=me.sec_proj_id, month=month)
                db.add(row)
            row.nav_code = codes[me.sec_proj_id][0]
            row.aimc_category, row.peer_theme = me.aimc_category, me.peer_theme
            row.basis, row.flag_eligible = sel["basis"], sel["basis"] == "theme"
            row.hedge_group = me.hedge_group
            for k in ("hedge_basis", "peer_masters", "fx_adjusted_masters", "hedge_ratio_estimated", "details"):
                setattr(row, k, snap[k])
            for n in HORIZONS:
                setattr(row, f"ret_{n}", _dec(snap[f"ret_{n}"]))
                setattr(row, f"median_{n}", _dec(snap[f"median_{n}"]))
                setattr(row, f"pct_{n}", _dec(snap[f"pct_{n}"], 2))
            for n in ("1m", "3m"):
                setattr(row, f"z_{n}", _dec(snap[f"z_{n}"], 4))
                setattr(row, f"fx_effect_{n}", _dec(snap[f"fx_effect_{n}"]))
            row.hedge_ratio = _dec(snap["hedge_ratio"], 4)
            row.backfilled, row.computed_at = backfilled, datetime.now(timezone.utc)
            counts["written"] += 1
        await db.commit()
    return counts


async def run_monthly_snapshot(db: AsyncSession, today: date) -> dict:
    """Live snapshot of the last complete month (re-run daily early in the month for late NAVs)."""
    await sync_fx(db)                                  # per-currency failures logged, stored rates used
    return await build_snapshots(db, [last_complete_month(today)], backfilled=False)


# ── own-NAV risk (on demand, daily NAV) ───────────────────────────────────────

async def own_risk(db: AsyncSession, fund_code: str, end: date) -> dict:
    """1Y max drawdown and annualised volatility (weekly returns) of one share class."""
    rows = (await db.execute(select(NavHistory.trade_date, NavHistory.nav).where(
        NavHistory.fund_code == fund_code, NavHistory.trade_date > end - timedelta(days=365),
        NavHistory.trade_date <= end).order_by(NavHistory.trade_date))).all()
    weekly: dict[tuple[int, int], float] = {}
    for d, v in rows:
        iso = d.isocalendar()
        weekly[(iso.year, iso.week)] = float(v)
    navs = list(weekly.values())
    if len(navs) < 26:
        return {"weeks": len(navs), "max_drawdown_1y": None, "volatility_1y": None}
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [b / a - 1 for a, b in zip(navs, navs[1:])]
    return {"weeks": len(navs), "max_drawdown_1y": mdd, "volatility_1y": statistics.stdev(rets) * 52 ** 0.5}
