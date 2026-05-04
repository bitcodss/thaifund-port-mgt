# Thai Mutual Fund Portfolio Tracker

## Project Overview

A self-hosted web app for tracking Thai mutual fund investments. Pulls NAV and dividend data from the Thailand SEC Open Data API, tracks tax lots through fund switches, and computes portfolio performance with proper handling of Thai tax-advantaged schemes (RMF, SSF, ThaiESG, LTF).

- **Deployment**: Self-hosted via Docker Compose on the user's own machine. Mobile-responsive web UI.
- **Users**: Multi-user with admin-managed accounts (no self-registration). Each user has multiple portfolios.

---

## Tech Stack

- **Backend**: FastAPI (Python 3.12+), SQLAlchemy 2.x, Alembic, APScheduler
- **Frontend**: Next.js 14+ (App Router) + TypeScript + Tailwind + shadcn/ui + Recharts
- **Database**: PostgreSQL 16+
- **Auth**: JWT, admin-only user creation
- **Containerization**: Docker Compose

Why this stack:
- FastAPI gives async I/O for the SEC API and pandas/numpy/scipy for XIRR + risk metrics
- PostgreSQL: robust time-series queries on NAV history, JSONB for fund metadata
- Next.js: mobile-responsive, server components for fast portfolio dashboards

---

## External APIs

### SEC Thailand Open Data API

- **Portal**: https://api-portal.sec.or.th/ — new portal launched 12 Jan 2026; old portal sunsets 30 Jun 2026
- **Subscribe** to: Fund Factsheet API, Fund Daily Info API
- **Auth**: Subscription key in `Ocp-Apim-Subscription-Key` header
- **Rate limit**: 3,000 calls per 300 seconds. Implement client-side throttling at ~10 req/sec; on HTTP 421, back off per the response header
- **Important**: Always verify against https://api-portal.sec.or.th/changes before changing schemas. Endpoint paths have been migrating recently

Key data we use:
- Fund factsheet: name, AMC, asset class, risk level (1–8), benchmark, fund type
- Daily NAV: NAV value per fund per date
- Dividend events: per-unit dividend amount and ex/payment dates

---

## Domain Model — Critical Concepts

### Tax Lot (the core abstraction)

Every BUY creates a Tax Lot — a unit of holding with its own original purchase date that anchors the tax holding period.

**Invariant**: `original_purchase_date` is set on creation and **never changes**, even when the lot moves between funds via SWITCH.

```python
class TaxLot:
    id: UUID
    portfolio_id: UUID
    fund_code: str                  # current fund (changes on SWITCH)
    original_purchase_date: date    # NEVER changes — anchors tax holding period
    units_remaining: Decimal        # changes on SELL/SWITCH_OUT
    cost_basis_remaining: Decimal   # carries through SWITCH; reduces proportionally on SELL
    tax_scheme: TaxScheme           # NORMAL | RMF | SSF | THAI_ESG | LTF
    source_lot_id: UUID | None      # set on SWITCH_IN — links to predecessor
    created_at: datetime
```

### Transaction Types

- `BUY` — creates a new lot
- `SELL` — consumes lots FIFO **within the requested `tax_scheme`**, reduces `units_remaining` and `cost_basis_remaining` proportionally
- `SWITCH_OUT` — closes (or partially closes) lots in source fund, FIFO within `tax_scheme`
- `SWITCH_IN` — creates new lot(s) in target fund, **inheriting** `original_purchase_date`, `tax_scheme`, and `cost_basis_remaining` from each consumed source lot
- `DIVIDEND` — records gross/net/withholding for a fund event; no lot mutation
- `INTEREST` — portfolio-level cash interest; no fund linkage; no lot mutation

### FIFO Rule

When a `SELL` or `SWITCH_OUT` is recorded:
1. Filter open lots: `fund_code == source_fund AND tax_scheme == requested_scheme AND units_remaining > 0`
2. Order by `original_purchase_date ASC` (oldest first)
3. Consume from oldest until requested units are satisfied
4. The last lot consumed may be partially consumed (split — reduce its `units_remaining` and `cost_basis_remaining` proportionally)

**Why filter by `tax_scheme`**: the user explicitly chooses which scheme to sell from (e.g., "sell SSF units"). Mixing schemes during FIFO would corrupt tax tracking. The user's choice is recorded on the transaction.

### SWITCH Pairing

A fund switch is **two paired transactions** sharing a `pair_id`:
- `SWITCH_OUT` from fund A
- `SWITCH_IN` to fund B

Validation: `SWITCH_OUT.amount` ≈ `SWITCH_IN.amount` (within 0.5% to allow fees), same date, **same AMC** (Thai funds only allow same-AMC switching).

Lot transformation on a switch:
1. FIFO-consume source lots in fund A matching `tax_scheme`
2. For each consumed lot, create a new lot in fund B with:
   - `original_purchase_date` = source lot's date (inherited — this is the whole point)
   - `tax_scheme` = source lot's scheme (inherited)
   - `cost_basis_remaining` = portion of source lot's cost (preserved across the switch)
   - `units_remaining` = `cost_basis_remaining / target_fund_NAV_at_switch_date`
   - `source_lot_id` = source lot's id (audit trail)

### Tax Holding Period Rules

| Scheme | Requirement (verify current rules at runtime) |
|---|---|
| RMF | 5 years AND age ≥ 55 |
| SSF | 10 calendar years from purchase |
| ThaiESG | 5 years (was 8y for older variants — TESG Extra and others may differ; design rules as data, not hardcode) |
| LTF | 5 calendar years (legacy; no new buys but existing holdings still tracked) |
| NORMAL | none |

Compute `today − original_purchase_date` per lot vs. the scheme's threshold. Store rules in a `tax_scheme_rules` table so they can be updated without code changes.

---

## Features by Phase

### Phase 1 — Foundation (build first, no Phase 2 until tests pass)

1. Admin-managed user auth: login, JWT, admin user CRUD
2. Portfolio CRUD per user
3. Transaction CRUD with all 6 types
4. Tax Lot engine: FIFO + switch pairing + lot consumption audit trail
5. CSV import (schema in this doc)

### Phase 2 — Data Sync

6. Fund metadata sync from SEC Fund Factsheet API (manual + nightly)
7. NAV daily sync (scheduled, after Thai market close ~19:00 ICT on weekdays)
8. Dividend sync from SEC API (auto-create `DIVIDEND` transactions when new ones are found, dedupe by ex_date + fund_code)
9. Local NAV history cache for fast charts

### Phase 3 — Dashboard

10. Portfolio overview: total value, unrealized P&L, realized P&L, total return %
11. Per-fund performance with timeframes: 7d, 30d, 6M, 1Y, YTD, MAX
12. **XIRR (money-weighted)** and **TWR (time-weighted)** returns — XIRR is the headline number for total period
13. Asset allocation pie charts: by asset class, region, AMC, SEC risk level (1–8)
14. Risk metrics per fund computed from NAV history: Sharpe ratio, Max Drawdown, annualized volatility
15. Benchmark comparison per fund (use the benchmark from each fund's factsheet)
16. Tax-advantaged holding tracker:
    - Per-lot countdown to eligibility
    - Alert when a lot becomes tax-free-eligible
    - Warning UI when planning a sell that would breach holding rules

### Phase 4 — Polish

17. Mobile-responsive layout (test at 375px width)
18. Dark mode
19. Performance: indexes on `nav_history(fund_code, date)` and `transactions(portfolio_id, date)`; query result caching for dashboards

---

## CSV Import Format

```csv
date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note
2024-03-15,BUY,SCBSET,1000,12.3456,12345.60,0,0,,,NORMAL,
2024-08-20,SELL,SCBSET,500,13.2100,6605.00,33.03,0,,,NORMAL,
2024-09-10,SWITCH_OUT,SCBSET,500,13.5000,6750.00,0,0,SCBTOP,switch-001,NORMAL,
2024-09-10,SWITCH_IN,SCBTOP,450,15.0000,6750.00,0,0,SCBSET,switch-001,NORMAL,
2024-12-15,DIVIDEND,SCBSET,,,250.00,0,25.00,,,NORMAL,Q4 dividend
2024-12-31,INTEREST,,,,150.50,0,15.05,,,NORMAL,Cash interest
```

Validation rules:
- `BUY/SELL/SWITCH_OUT/SWITCH_IN`: `units`, `nav`, `amount` required; `units × nav` must equal `amount` within ฿0.01
- `SWITCH_OUT` + `SWITCH_IN`: must arrive in pairs sharing `pair_id`, same date; `amount` within 0.5% tolerance
- `DIVIDEND`: `fund_code`, `amount`, `tax_withheld` required; no `units`/`nav`
- `INTEREST`: `amount`, `tax_withheld` required; no `fund_code`/`units`/`nav`
- Reject rows with the same `(date, type, fund_code, units, amount)` already present in the same portfolio

---

## Database Schema (outline)

```
users (id, email, password_hash, role, created_at)
portfolios (id, user_id, name, created_at)

funds (fund_code PK, name_th, name_en, amc, asset_class, risk_level,
       benchmark, fund_type, last_synced_at, raw_factsheet JSONB)

nav_history (fund_code, trade_date, nav, change_pct,
             PRIMARY KEY(fund_code, trade_date))

tax_lots (id, portfolio_id, fund_code, original_purchase_date,
          units_remaining, cost_basis_remaining, tax_scheme,
          source_lot_id, created_at)

transactions (id, portfolio_id, date, type, fund_code, units, nav,
              amount, fee, tax_withheld, target_fund_code, pair_id,
              tax_scheme, note, created_at)

lot_consumptions (id, transaction_id, lot_id, units_consumed,
                  cost_basis_consumed)
  -- audit trail: every lot mutation logged here

dividends (id, fund_code, ex_date, payment_date, dividend_per_unit,
           source[manual|sec_api], created_at)

tax_scheme_rules (scheme PK, holding_years, age_requirement, active_from)

sync_jobs (id, type, started_at, completed_at, status, error_message)
```

All monetary and unit columns: `numeric(20, 8)`.

---

## Engineering Conventions

### Decimals everywhere
Use `decimal.Decimal` (Python) and `numeric(20, 8)` (PostgreSQL). **Never use floats for money or units.**

### Idempotency
The lot engine must be deterministic. Same input transaction → same output state. Wrap every lot mutation in a DB transaction (BEGIN/COMMIT) and write the audit row in `lot_consumptions` in the same transaction.

### Audit trail
Every lot mutation creates `lot_consumptions` rows linking the transaction to affected lot(s). This is the source of truth for "why does this lot have these units now". Used to debug FIFO behavior and to support future "show me which lots were sold on date X" queries.

### NAV sync resilience
- Cache last successful sync per fund
- Exponential backoff on HTTP 421 (rate limit) — respect the wait time in response
- Skip funds that fail; don't abort the batch
- Log per-fund failures into `sync_jobs.error_message`

### Time zones
- Store all timestamps as UTC in DB
- Display in `Asia/Bangkok` (ICT) on frontend
- A NAV's `trade_date` is a Thai trading date, not a UTC instant — keep it as `date`, not `timestamptz`

### Money math
- XIRR: scipy.optimize.brentq on the NPV function with daily granularity
- TWR: chain-link by sub-period returns between cash flows
- Risk metrics: weekly NAV returns are smooth enough; use ≥104 weeks for stable estimates

---

## Docker Compose Layout

```yaml
services:
  postgres:    # data persistence in named volume
  backend:     # FastAPI + APScheduler in same process; depends on postgres
  frontend:    # Next.js production build
  # optional: caddy or nginx for HTTPS reverse proxy
```

Single-instance deployment is fine for self-hosted. APScheduler runs in the backend process — no separate worker needed at this scale.

---

## Out of Scope (do not build)

- DCA scheduler or planner
- Rebalancing alerts
- Goal-based investing
- Statement (PDF / email) parsing — CSV import only
- Fee impact analysis
- Self-registration
- Tax-loss harvesting recommendations
- Year-end tax filing reports (just store withholding amounts; user does their own filing)

---

## Testing Priorities

Write tests **before** implementation for these scenarios:

1. **Lot FIFO**:
   - BUY → BUY → partial SELL: oldest lot consumed first, second lot untouched
   - BUY → BUY → SELL larger than oldest: oldest fully consumed, second partially consumed
   - BUY → full SWITCH: source lot closed, new lot in target fund with same `original_purchase_date` and `cost_basis`
   - BUY → BUY → partial SWITCH: FIFO applies; only oldest is switched
   - Chained switches A → B → C: `original_purchase_date` survives both hops

2. **Tax scheme isolation**: SELL of SSF lots when both SSF and RMF lots exist for same fund — RMF lots must be untouched

3. **Cost basis preservation**: total cost basis on switch out = total cost basis on switch in

4. **XIRR**: hand-built scenarios (lump sum, regular monthly DCA) with known answers

5. **CSV import**: valid input, malformed rows, switch pair amount mismatch, missing pair partner, duplicate detection

6. **Holding period**: lot becomes eligible on the right date for each scheme
