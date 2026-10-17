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
- loss_10pct    P&L ≤ −10% SINCE ENTRY INTO THIS FUND: a bought lot's cost, a switched-in
                lot's units × NAV on the switch-in date — what this fund did to the money,
                not the previous fund's gain or loss. The tax-lot P&L (original cost carried
                through switches, as on the Holdings page) is kept alongside as context.

Tax facts: every lot's scheme, eligibility date and days remaining (rules table), plus the
transfer / early-sale rules for RMF and SSF, so the narrative never suggests a move the
holder can't make (e.g. selling an RMF before its conditions are met).
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
from app.models.tax_lot import LotConsumption, TaxLot, TaxSchemeRule
from app.models.transaction import Transaction, TransactionType
from app.models.user import User
from app.services import peer_metrics as pm
from app.services import portfolio_service

Z_FLAG = -2.0
LOSS_FLAG = -0.10
BOTTOM_PCT = 25
NAV_HISTORY_FROM = date(2015, 1, 31)

# Scheme rules beyond the holding period (holding years / age come from tax_scheme_rules).
# Verified 2026-10-17 against Revenue Department sources; each note carries its source.
_RD_369 = "Revenue Dept announcement (income tax) No. 369, 13 Mar 2020 — https://www.rd.go.th/fileadmin/centralize/news_law_update/2563/03/dg369.pdf"
_RD_2158 = "Revenue Dept ruling กค 0702/2158, 21 Apr 2023 (applying announcements No. 170/171) — https://www.rd.go.th/65449.html"
TAX_SCHEME_NOTES = {
    "RMF": [
        {"text": "Breaking the conditions (selling before holding 5 years from the first purchase AND age 55, or "
                 "the purchase rule below): the tax for the years whose RMF purchases were deducted, within 5 years "
                 "of each year's filing, must be paid back with the section 27 surcharge (waived if filed by March "
                 "of the following year), and the gain on sale is taxable.", "source": _RD_2158},
        {"text": "Purchase rule: buy RMF at least once a year and never pause buying for more than 1 consecutive "
                 "year. Stopping RMF purchases altogether breaks the conditions.", "source": _RD_2158},
        {"text": "Switching to another RMF of the same AMC keeps the original purchase date and holding period.",
         "source": "app lot engine / plan"},
        {"text": "Moving to another AMC is a transfer to that AMC's RMF (allowed, keeps the holding period); "
                 "slower than a same-AMC switch.", "source": "plan (cross-AMC transfer rule)"},
        {"text": "Moving the money to a non-RMF fund counts as selling the RMF.", "source": _RD_2158},
    ],
    "SSF": [
        {"text": "Breaking the condition (selling before holding 10 years from purchase, except disability or "
                 "death): the tax for the years whose SSF purchases were deducted, within 5 years of each year's "
                 "filing, must be paid back with the section 27 surcharge; the gain on sale is taxable (cost by "
                 "FIFO).", "source": _RD_369 + " (clauses 1–4)"},
        {"text": "Transfer to another SSF (same or another AMC, all or part) keeps the holding period only if "
                 "completed within 5 business days of the transfer order.", "source": _RD_369 + " (clause 6)"},
        {"text": "SSF deductions cover purchases from 1 Jan 2020 to 31 Dec 2024 only — no new SSF purchases "
                 "are deductible now.", "source": _RD_369 + " (clause 1)"},
        {"text": "Moving the money to a non-SSF fund counts as selling the SSF.", "source": _RD_369},
    ],
}
# Narrative constraint: while any lot of a tax scheme is not yet eligible, the report may only
# discuss moves that keep the scheme — never "sell".
ALLOWED_MOVES = {
    "RMF": ["hold", "switch to another RMF of the same AMC", "transfer to another AMC's RMF"],
    "SSF": ["hold", "switch to another SSF of the same AMC", "transfer to another AMC's SSF (within 5 business days)"],
}


async def lot_entry_values(db: AsyncSession, lots: list[TaxLot]) -> dict:
    """
    lot_id → (entry value of the remaining units, entry date, how). A bought lot enters at its
    cost; a switched-in lot at units × NAV of the SWITCH_IN that created it — found through its
    source lot's SWITCH_OUT consumption and the paired SWITCH_IN into this lot's fund.
    """
    out = {}
    for lot in lots:
        if lot.source_lot_id is None:
            out[lot.id] = (lot.cost_basis_remaining, lot.original_purchase_date, "buy")
            continue
        outs = (await db.execute(
            select(Transaction, LotConsumption.cost_basis_consumed)
            .join(LotConsumption, LotConsumption.transaction_id == Transaction.id)
            .where(LotConsumption.lot_id == lot.source_lot_id,
                   Transaction.type == TransactionType.SWITCH_OUT.value))).all()
        best = None
        for out_tx, consumed in outs:
            in_tx = (await db.execute(select(Transaction).where(
                Transaction.portfolio_id == lot.portfolio_id, Transaction.pair_id == out_tx.pair_id,
                Transaction.type == TransactionType.SWITCH_IN.value,
                Transaction.fund_code == lot.fund_code))).scalars().first()
            if in_tx is None or not in_tx.nav:
                continue
            # one source lot switched into the same fund twice: the consumption whose cost
            # the new lot inherited (cost only shrinks afterwards, on sells)
            gap = consumed - lot.cost_basis_remaining
            if gap >= 0 and (best is None or gap < best[0]):
                best = (gap, in_tx)
        if best:
            in_tx = best[1]
            out[lot.id] = (lot.units_remaining * in_tx.nav, in_tx.date, "switch_in")
        else:
            out[lot.id] = (lot.cost_basis_remaining, lot.original_purchase_date, "cost_basis_fallback")
    return out


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


def _constraints(tax_lots: list[dict]) -> dict | None:
    """Locked scheme lots → never suggest selling; only hold / switch / transfer within the scheme."""
    locked = sorted({t["scheme"] for t in tax_lots if not t["eligible"] and t["scheme"] in ALLOWED_MOVES})
    if not locked:
        return None
    return {"never_suggest": ["sell", "move to a fund outside the scheme"], "locked_schemes": locked,
            "allowed_moves": [m for sch in locked for m in ALLOWED_MOVES[sch]],
            "eligible_dates": sorted({t["eligible_date"] for t in tax_lots if t["eligible_date"]})}


async def portfolio_facts(db: AsyncSession, portfolio_id, month: date) -> dict:
    """Facts + flags for one portfolio at a month-end."""
    portfolio = await db.get(Portfolio, portfolio_id)
    lots = (await db.execute(select(TaxLot).where(TaxLot.portfolio_id == portfolio_id,
                                                  TaxLot.units_remaining > 0))).scalars().all()
    entries = await lot_entry_values(db, lots)
    held: dict[str, dict] = defaultdict(lambda: {"units": Decimal(0), "cost": Decimal(0), "entry": Decimal(0),
                                                 "entry_dates": set(), "entry_how": set()})
    for lot in lots:
        h = held[lot.fund_code]
        h["units"] += lot.units_remaining
        h["cost"] += lot.cost_basis_remaining
        value, when, how = entries[lot.id]
        h["entry"] += value
        h["entry_dates"].add(when)
        h["entry_how"].add(how)

    owner = await db.get(User, portfolio.user_id) if portfolio else None
    eligibility = await portfolio_service.get_tax_eligibility(
        portfolio_id, db, month, owner.date_of_birth if owner else None)
    tax_by_code: dict[str, list] = defaultdict(list)
    for e in eligibility:
        if e.tax_scheme != "NORMAL":
            tax_by_code[e.fund_code].append({
                "scheme": e.tax_scheme, "units": float(e.units_remaining),
                "original_purchase_date": str(e.original_purchase_date), "switch_chain": e.switch_chain,
                "eligible": e.is_eligible, "eligible_date": str(e.eligible_date) if e.eligible_date else None,
                "days_remaining": e.days_remaining, "age_requirement": e.age_requirement})
    rules = {r.scheme: r for r in (await db.execute(select(TaxSchemeRule))).scalars()}
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
        pnl = float(value / h["entry"] - 1) if value is not None and h["entry"] > 0 else None
        pnl_lot = float(value / h["cost"] - 1) if value is not None and h["cost"] > 0 else None

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
            "nav": nav, "value": float(value) if value is not None else None,
            "pnl_pct": pnl, "entry_value": float(h["entry"]),
            "entry_dates": sorted(str(d) for d in h["entry_dates"]), "entry_basis": sorted(h["entry_how"]),
            "pnl_lot_cost_pct": pnl_lot,     # incl. the previous fund's result (tax-lot cost)
            "tax_lots": tax_by_code.get(code, []),
            "narrative_constraints": _constraints(tax_by_code.get(code, [])),
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
        "tax_rules": {s: {"holding_years": float(rules[s].holding_years) if s in rules else None,
                          "age_requirement": rules[s].age_requirement if s in rules else None,
                          "notes": TAX_SCHEME_NOTES.get(s, [])}
                      for s in sorted({t["scheme"] for r in rows for t in r["tax_lots"]})},
        # one entry per holding, every reason listed together
        "watchlist": [{"fund_code": r["fund_code"], "reasons": [f["rule"] for f in r["flags"]], "flags": r["flags"]}
                      for r in sorted(rows, key=lambda r: (-len(r["flags"]), -(r["weight"] or 0))) if r["flags"]],
    }
