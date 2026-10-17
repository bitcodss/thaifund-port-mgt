"""
Chunk 5 — facts and flags for the monthly report. Code computes every number here; the
Phase 3 narrative is written only from this facts set.

Flags (each holding appears ONCE on the watchlist, with every reason that applies):
- abnormal_1m   own 1M return < −2σ of the class's own monthly history
- abnormal_3m   own 3M return < −2σ of its own rolling 3M history
- bottom_3m     bottom of its peers on 3M return at this AND last month-end. Theme-basis
                comparisons only (a whole-category rank is context, never a flag).
                Percentile < 25, or last place in groups under SMALL_GROUP masters.
                Overlapping 3M windows can repeat one bad month, so the facts name the
                month that drove each window and whether both share it (single_event).
- loss_10pct    P&L ≤ −10% on the open lots' cost basis (same definition as the Holdings
                page; a switched-in lot carries its original cost).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fund import Fund
from app.models.market import PeerSnapshot
from app.models.portfolio import Portfolio
from app.models.tax_lot import TaxLot
from app.services import peer_metrics as pm

Z_FLAG = -2.0
LOSS_FLAG = -0.10
BOTTOM_PCT = 25
NAV_HISTORY_FROM = date(2015, 1, 31)


def _f(v) -> float | None:
    return None if v is None else float(v)


def is_bottom(s: PeerSnapshot | None) -> bool:
    """Bottom of a theme-basis peer group on 3M (rank last in small groups, else percentile < 25)."""
    if s is None or not s.flag_eligible:
        return False
    d = s.details or {}
    if d.get("small_group"):
        r = (d.get("ranks") or {}).get("3m")
        return bool(r) and r[0] == r[1]
    return s.pct_3m is not None and float(s.pct_3m) < BOTTOM_PCT


def driver_month(s: PeerSnapshot) -> dict | None:
    """The month inside the 3M window with the lowest 1M return, and its share of the window."""
    months = {m: r for m, r in ((s.details or {}).get("months_in_3m") or {}).items() if r is not None}
    if not months:
        return None
    m, r = min(months.items(), key=lambda kv: kv[1])
    return {"month": m, "return_1m": r, "window_return_3m": _f(s.ret_3m), "monthly_returns": months}


def peer_facts(s: PeerSnapshot | None) -> dict | None:
    if s is None:
        return None
    d = s.details or {}
    small = bool(d.get("small_group"))
    return {
        "month": str(s.month), "backfilled": s.backfilled, "nav_class": s.nav_code,
        "category": s.aimc_category, "theme": s.peer_theme, "basis": s.basis,
        "context_only": not s.flag_eligible,
        "context_reason": None if s.flag_eligible else
        "compared with the whole category because the style group has too few funds",
        "peer_masters": s.peer_masters, "display": "rank" if small else "percentile",
        "hedge_basis": s.hedge_basis, "fx_adjusted_masters": s.fx_adjusted_masters,
        **{f"return_{h}": _f(getattr(s, f"ret_{h}")) for h in pm.HORIZONS},
        **{f"peer_median_{h}": _f(getattr(s, f"median_{h}")) for h in pm.HORIZONS},
        **{f"percentile_{h}": _f(getattr(s, f"pct_{h}")) for h in pm.HORIZONS},
        "ranks": d.get("ranks") or {},
        "vs_same_master_3m": d.get("vs_same_master_3m"), "vs_same_master_1y": d.get("vs_same_master_1y"),
        "same_master_feeders": len(d.get("same_master") or []),
        "fx": {"effect_1m": _f(s.fx_effect_1m), "effect_3m": _f(s.fx_effect_3m), "currency": d.get("currency"),
               "currency_assumed": d.get("currency_source") in ("region", "default"),
               "hedge_ratio": _f(s.hedge_ratio), "hedge_ratio_estimated": s.hedge_ratio_estimated},
    }


async def portfolio_facts(db: AsyncSession, portfolio_id, month: date) -> dict:
    """Facts + flags for one portfolio at a month-end."""
    portfolio = await db.get(Portfolio, portfolio_id)
    lots = (await db.execute(select(TaxLot).where(TaxLot.portfolio_id == portfolio_id,
                                                  TaxLot.units_remaining > 0))).scalars().all()
    held: dict[str, dict] = defaultdict(lambda: {"units": Decimal(0), "cost": Decimal(0)})
    for lot in lots:
        held[lot.fund_code]["units"] += lot.units_remaining
        held[lot.fund_code]["cost"] += lot.cost_basis_remaining
    funds = {f.fund_code: f for f in (await db.execute(select(Fund).where(Fund.fund_code.in_(held)))).scalars()}

    nav_months, m = [], NAV_HISTORY_FROM
    while m <= month:
        nav_months.append(m)
        m = pm.months_back(m, -1)
    navs = await pm._load_month_ends(db, set(held), nav_months)
    proj_ids = {f.sec_proj_id for f in funds.values() if f.sec_proj_id}
    snaps = {(s.sec_proj_id, s.month): s for s in (await db.execute(select(PeerSnapshot).where(
        PeerSnapshot.sec_proj_id.in_(proj_ids), PeerSnapshot.month.in_([month, pm.months_back(month, 1)])))).scalars()}

    rows, total_value = [], Decimal(0)
    for code in sorted(held):
        h, fund, series = held[code], funds.get(code), navs.get(code)
        nav = series.at(month) if series else None
        value = (h["units"] * Decimal(str(nav))).quantize(Decimal("0.01")) if nav is not None else None
        total_value += value or 0
        own = {f"return_{k}": series.ret(month, n) if series else None for k, n in pm.HORIZONS.items()}
        earlier = sorted(x for x in (series.by_month if series else {}) if x < month)
        z = {}
        for k, n in (("1m", 1), ("3m", 3)):
            hist = [r for x in earlier if (r := series.ret(x, n)) is not None]
            z[k] = pm.zscore(own[f"return_{k}"], hist) if own[f"return_{k}"] is not None else None
        pnl = float(value / h["cost"] - 1) if value is not None and h["cost"] > 0 else None

        pid = fund.sec_proj_id if fund else None
        snap, prev = snaps.get((pid, month)), snaps.get((pid, pm.months_back(month, 1)))
        flags = []
        if z["1m"] is not None and z["1m"] < Z_FLAG:
            flags.append({"rule": "abnormal_1m", "return_1m": own["return_1m"], "z": z["1m"]})
        if z["3m"] is not None and z["3m"] < Z_FLAG:
            flags.append({"rule": "abnormal_3m", "return_3m": own["return_3m"], "z": z["3m"]})
        if is_bottom(snap) and is_bottom(prev):
            now_driver, prev_driver = driver_month(snap), driver_month(prev)
            flags.append({"rule": "bottom_3m", "display": peer_facts(snap)["display"],
                          "percentile_3m": _f(snap.pct_3m), "rank_3m": (snap.details or {}).get("ranks", {}).get("3m"),
                          "previous_percentile_3m": _f(prev.pct_3m),
                          "previous_rank_3m": (prev.details or {}).get("ranks", {}).get("3m"),
                          "driver": now_driver, "previous_driver": prev_driver,
                          "single_event": bool(now_driver and prev_driver
                                               and now_driver["month"] == prev_driver["month"]),
                          "uses_backfilled_snapshot": snap.backfilled or prev.backfilled})
        if pnl is not None and pnl <= LOSS_FLAG:
            flags.append({"rule": "loss_10pct", "pnl_pct": pnl})

        rows.append({
            "fund_code": code, "sec_proj_id": pid, "name_en": fund.name_en if fund else None,
            "name_th": fund.name_th if fund else None, "units": float(h["units"]), "cost": float(h["cost"]),
            "nav": nav, "value": float(value) if value is not None else None, "pnl_pct": pnl,
            "missing_month_end_nav": nav is None, **own, "z_1m": z["1m"], "z_3m": z["3m"],
            "peers": peer_facts(snap), "flags": flags,
        })
    for r in rows:
        r["weight"] = r["value"] / float(total_value) if r["value"] is not None and total_value else None

    return {
        "month": str(month),
        "portfolio": {"id": str(portfolio_id), "name": portfolio.name if portfolio else None,
                      "value": float(total_value), "cost": float(sum(h["cost"] for h in held.values()))},
        "funds": rows,
        # one entry per holding, every reason listed together
        "watchlist": [{"fund_code": r["fund_code"], "reasons": [f["rule"] for f in r["flags"]], "flags": r["flags"]}
                      for r in sorted(rows, key=lambda r: (-len(r["flags"]), -(r["weight"] or 0))) if r["flags"]],
    }
