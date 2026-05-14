# spec.md — Thai Fund Portfolio Tracker

Knowledge base of **the implementation as it stands today**. Companion to:
- `CLAUDE.md` — design intent, domain rules, phased plan
- `README.md` — user setup guide

This file describes what's actually in the code: real file paths, real endpoints, real models, real pages. Update it when the implementation changes.

---

## 1. Overview

A self-hosted web app for tracking Thai mutual fund investments. Pulls NAV, dividends, and fund metadata from the Thailand SEC Open Data API, tracks tax lots through fund switches with FIFO consumption, and computes portfolio performance with proper handling of tax-advantaged schemes (RMF, SSF, ThaiESG, ThaiESG Extra, LTF).

- **Deployment:** Single-host Docker Compose. Postgres + FastAPI backend (with embedded APScheduler) + Next.js frontend.
- **Users:** Multi-user. Admin-managed accounts (no self-registration). One initial admin is seeded from env vars on first startup. Each user has multiple portfolios.
- **Locale:** Thai language UI strings in places (`<html lang="th">`), `฿` currency, `Asia/Bangkok` business dates; DB stores UTC for timestamps, `date` for trade dates.

**Implementation status vs. CLAUDE.md phases:**

| Phase | Status |
|---|---|
| Phase 1 — auth, portfolios, transactions (all 6 types), tax lot engine, CSV import | Done |
| Phase 2 — fund metadata sync, NAV sync (scheduled), dividend sync, NAV history cache | Done |
| Phase 3 — summary, per-fund performance, XIRR, allocation pies, Sharpe/MDD/vol, benchmark, tax holding tracker | Done; TWR added recently |
| Phase 4 — dark mode (Done), mobile responsiveness (Partial), query caching (Done — 5-min in-memory) | Mostly done |
| Beyond plan | AI summary (Ollama), Finnomena NAV fallback, cross-portfolio transfer of holdings, dividend year filter, sortable performance/risk table |

---

## 2. Tech Stack

### Backend (`backend/`)
- Python 3.12+
- **FastAPI** ≥0.111 — HTTP framework, async
- **SQLAlchemy 2.x** with `asyncio` — ORM
- **asyncpg** — async Postgres driver (app runtime)
- **psycopg2-binary** — sync Postgres driver (Alembic only)
- **Alembic** — migrations
- **APScheduler** (AsyncIOScheduler) — in-process cron jobs
- **pydantic v2 + pydantic-settings** — request/response validation + env config
- **python-jose[cryptography]** — JWT (HS256)
- **bcrypt** — password hashing
- **httpx** — async HTTP client (SEC API, Ollama, Finnomena)
- **numpy + scipy** — risk metrics + XIRR (`scipy.optimize.brentq`)
- **pytest + pytest-asyncio + aiosqlite** — tests (SQLite in-memory)

### Frontend (`frontend/`)
- **Next.js 14.2** App Router, **React 18**, **TypeScript 5**
- **Tailwind 3.4** — utility CSS, `darkMode: "class"`
- **shadcn/ui** built on **Radix UI** primitives (`dialog`, `dropdown-menu`, `label`, `select`, `slot`, `tabs`)
- **Recharts 2.12** — pie / line / bar charts
- **react-hook-form** + **zod** — form validation
- **lucide-react** — icons
- **class-variance-authority** + **clsx** + **tailwind-merge** — class composition
- No state library (Zustand/Redux/Jotai). No data-fetching library (SWR/React Query). State = React hooks; persistence = `localStorage`; fetch = a typed wrapper in `src/lib/api.ts`.

### Database
- **PostgreSQL 16** (Alpine image)
- All money & unit columns: `numeric(20, 8)`. Python uses `decimal.Decimal`. No floats anywhere.

### AI
- **Ollama** (default model `gemma4:26b` — overridable). Local LLM produces Thai-language portfolio summaries cached in `portfolio_ai_summaries`.

### External APIs
- **SEC Thailand Open Data** — FundFactsheet (metadata) + FundDailyInfo (NAV + dividends). Throttled to ~9 req/s with retry on HTTP 421/429.
- **Finnomena** (optional, fallback) — NAV history for `ES-`-prefixed Eastspring funds.

---

## 3. Repository Layout

```
/home/bitcodata/thai-fund-tracker/
├── CLAUDE.md                — design intent / domain rules
├── README.md                — setup guide
├── spec.md                  — this file
├── docker-compose.yml       — postgres + backend + frontend
├── .env.example             — env var template
├── backend/
│   ├── requirements.txt
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   │       ├── 001_initial_schema.py
│   │       ├── 002_add_sec_fields.py
│   │       ├── 003_widen_fund_code.py
│   │       └── 004_ai_summary.py
│   └── app/
│       ├── main.py          — FastAPI app, scheduler, router wiring
│       ├── config.py        — pydantic-settings env config
│       ├── database.py      — SQLAlchemy async engine + session factory
│       ├── init_db.py       — first-run seeding (admin user, tax_scheme_rules)
│       ├── api/
│       │   ├── deps.py          — JWT auth dependency, require_admin
│       │   ├── auth.py          — /auth/token
│       │   ├── users.py         — /users, /users/me
│       │   ├── portfolios.py    — /portfolios + transfer-holding + analytics refresh
│       │   ├── transactions.py  — /portfolios/{id}/transactions + lots_router
│       │   ├── funds.py         — /funds, /funds/{code}, /funds/{code}/nav, /funds/search
│       │   ├── analytics.py     — /portfolios/{id}/analytics/* + /funds/{code}/performance + dividends
│       │   └── sync.py          — /sync/* (admin)
│       ├── models/
│       │   ├── user.py          — User
│       │   ├── portfolio.py     — Portfolio, PortfolioAiSummary
│       │   ├── fund.py          — Fund, NavHistory, Dividend
│       │   ├── transaction.py   — Transaction (+ enums)
│       │   └── tax_lot.py       — TaxLot, LotConsumption, TaxSchemeRule, SyncJob
│       ├── schemas/             — Pydantic DTOs (auth, user, portfolio, transaction, fund, analytics)
│       ├── services/
│       │   ├── auth_service.py        — bcrypt + JWT
│       │   ├── lot_engine.py          — pure FIFO + switch lot construction + holding eligibility
│       │   ├── transaction_service.py — DB-layer wrapper around lot_engine; rebuilds lots on delete
│       │   ├── portfolio_service.py   — summary, holdings, allocation, XIRR, tax eligibility (5-min cache)
│       │   ├── performance_service.py — fund returns (7d/30d/6m/1y/YTD/max), Sharpe, MDD, volatility
│       │   ├── sec_api.py             — throttled SEC Thailand HTTP client
│       │   ├── sync_service.py        — metadata / NAV / dividend orchestration, sync_jobs tracking
│       │   ├── csv_import.py          — pure parse + validate (units×nav=amount, switch pair, dedup)
│       │   ├── ai_service.py          — Ollama call + cache to portfolio_ai_summaries
│       │   └── finnomena_service.py   — fallback NAV sync for ES- funds
│       └── tests/
│           ├── test_lot_engine.py
│           ├── test_csv_import.py
│           ├── test_analytics.py
│           └── test_sec_sync.py
└── frontend/
    ├── package.json
    ├── next.config.js
    ├── tailwind.config.js
    ├── postcss.config.js
    ├── tsconfig.json
    ├── Dockerfile
    ├── public/
    └── src/
        ├── app/
        │   ├── layout.tsx           — root layout, inline theme script (lang="th")
        │   ├── globals.css          — Tailwind base + CSS variables for light/dark themes
        │   ├── page.tsx             — token-based redirect (/dashboard or /login)
        │   ├── login/page.tsx
        │   └── dashboard/
        │       ├── page.tsx                       — aggregate dashboard
        │       ├── settings/page.tsx              — preferences + logout
        │       ├── sync/page.tsx                  — admin sync controls
        │       └── portfolios/[id]/page.tsx       — portfolio detail (5 tabs)
        ├── components/ui/           — shadcn primitives (button, card, dialog, dropdown-menu, input, label, select, tabs, badge, theme-toggle)
        └── lib/
            ├── api.ts               — typed API client (~40 methods, Bearer auth from localStorage)
            ├── settings.ts          — P&L display preference persistence
            └── utils.ts             — cn() class merger
```

### Docker Compose services

| Service | Image | Ports | Notes |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | 5432 (internal only) | Volume `postgres_data` for persistence; healthcheck wired |
| `backend` | built from `./backend` | `8000:8000` | Runs Alembic on startup; depends on healthy postgres |
| `frontend` | built from `./frontend` | `3000:3000` | Talks to backend via `BACKEND_INTERNAL_URL=http://backend:8000` |

### Environment variables (`.env.example`)

Required:
- `POSTGRES_PASSWORD`
- `SECRET_KEY` (JWT signing — generate with `openssl rand -hex 32`)
- `FIRST_ADMIN_EMAIL`, `FIRST_ADMIN_PASSWORD`
- `SEC_API_KEY` (FundDailyInfo: NAV + dividends)

Optional:
- `POSTGRES_DB` (default `thaiund`), `POSTGRES_USER` (default `thaiuser`), `POSTGRES_HOST`, `POSTGRES_PORT`
- `ALGORITHM` (default `HS256`), `ACCESS_TOKEN_EXPIRE_MINUTES` (default `1440` = 24h)
- `SEC_FACTSHEET_KEY` (falls back to `SEC_API_KEY`)
- `SEC_API_KEY_SECONDARY` (failover)
- `OLLAMA_URL` (default `http://host.docker.internal:11434`), `OLLAMA_MODEL` (default `gemma4:26b`)
- `FINNOMENA_EMAIL`, `FINNOMENA_PASSWORD`
- `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`)

---

## 4. Database Schema

All UUIDs are stored natively. All money/unit columns are `numeric(20, 8)`. Timestamps are stored as UTC.

### users
- `id` UUID PK
- `email` VARCHAR(255) UNIQUE, indexed
- `password_hash` VARCHAR(255)
- `role` VARCHAR(20)  — `admin` | `user`
- `date_of_birth` DATE NULL  — used for RMF age-55 eligibility
- `is_active` BOOLEAN DEFAULT true
- `created_at` TIMESTAMPTZ DEFAULT now()
- Rel: `portfolios` (1-N, cascade delete)

### portfolios
- `id` UUID PK
- `user_id` UUID FK→users.id ON DELETE CASCADE, indexed
- `name` VARCHAR(255)
- `created_at` TIMESTAMPTZ
- Rel: `transactions` (1-N), `tax_lots` (1-N)

### portfolio_ai_summaries
- `portfolio_id` UUID PK FK→portfolios.id ON DELETE CASCADE
- `content` TEXT
- `generated_at` TIMESTAMPTZ

### funds
- `fund_code` VARCHAR(50) PK
- `sec_proj_id` VARCHAR(50) NULL, indexed  — SEC project ID for FundDailyInfo API
- `name_th`, `name_en` VARCHAR(500) NULL
- `amc` VARCHAR(200) NULL
- `amc_unique_id` VARCHAR(50) NULL
- `asset_class` VARCHAR(100) NULL  — "Equity", "Fixed Income", "Money Market", …
- `risk_level` INTEGER NULL  — 1–8
- `benchmark` VARCHAR(200) NULL
- `fund_type` VARCHAR(100) NULL
- `fund_status` VARCHAR(10) NULL  — "RG" = registered
- `last_synced_at` TIMESTAMPTZ NULL
- `last_nav_date` DATE NULL
- `raw_factsheet` JSONB NULL

### nav_history
- `(fund_code, trade_date)` composite PK
- `fund_code` VARCHAR(50) FK→funds.fund_code
- `trade_date` DATE
- `nav` NUMERIC(20,8)
- `change_pct` NUMERIC(20,8) NULL

### dividends
- `id` UUID PK
- `fund_code` VARCHAR(50), indexed
- `ex_date` DATE
- `payment_date` DATE NULL
- `dividend_per_unit` NUMERIC(20,8)
- `source` VARCHAR(20) DEFAULT `manual`  — `manual` | `sec_api`
- `created_at` TIMESTAMPTZ

### transactions
- `id` UUID PK
- `portfolio_id` UUID FK→portfolios.id ON DELETE CASCADE, indexed
- `date` DATE, indexed
- `type` VARCHAR(20)  — `BUY` | `SELL` | `SWITCH_OUT` | `SWITCH_IN` | `DIVIDEND` | `INTEREST`
- `fund_code` VARCHAR(50) NULL
- `units`, `nav`, `amount`, `fee`, `tax_withheld` NUMERIC(20,8) (units/nav nullable)
- `target_fund_code` VARCHAR(50) NULL  — set on SWITCH legs
- `pair_id` VARCHAR(100) NULL, indexed  — links SWITCH_OUT ↔ SWITCH_IN
- `tax_scheme` VARCHAR(20)  — `NORMAL` | `RMF` | `SSF` | `THAI_ESG` | `THAI_ESG_EXTRA` | `LTF`
- `note` TEXT NULL
- `created_at` TIMESTAMPTZ
- Rel: `lot_consumptions` (1-N, cascade delete)

### tax_lots
- `id` UUID PK
- `portfolio_id` UUID FK→portfolios.id ON DELETE CASCADE, indexed
- `fund_code` VARCHAR(50), indexed  — current fund (changes on SWITCH)
- `original_purchase_date` DATE  — **immutable**, anchors tax holding period across switches
- `units_remaining` NUMERIC(20,8)
- `cost_basis_remaining` NUMERIC(20,8)
- `tax_scheme` VARCHAR(20)
- `source_lot_id` UUID FK→tax_lots.id NULL  — set on SWITCH_IN (audit chain)
- `created_at` TIMESTAMPTZ

### lot_consumptions  (audit trail)
- `id` UUID PK
- `transaction_id` UUID FK→transactions.id ON DELETE CASCADE, indexed
- `lot_id` UUID FK→tax_lots.id, indexed
- `units_consumed` NUMERIC(20,8)
- `cost_basis_consumed` NUMERIC(20,8)

Every lot mutation (SELL or SWITCH_OUT) writes one row per affected lot. This is the source of truth for "why does this lot have these units now."

### tax_scheme_rules  (data-driven holding rules)
- `scheme` VARCHAR(20) PK
- `holding_years` NUMERIC(5,2)
- `age_requirement` INTEGER NULL
- `active_from` DATE

### sync_jobs
- `id` UUID PK
- `type` VARCHAR(50)  — `fund_metadata` | `nav_sync` | `dividend_sync`
- `started_at`, `completed_at` TIMESTAMPTZ
- `status` VARCHAR(20)  — `running` | `success` | `error`
- `error_message` VARCHAR(2000) NULL

### Alembic migrations

| Rev | File | Effect |
|---|---|---|
| 001 | `001_initial_schema.py` | Creates users, portfolios, funds, nav_history, transactions, tax_lots, lot_consumptions, dividends, tax_scheme_rules |
| 002 | `002_add_sec_fields.py` | Adds `sec_proj_id`, `amc_unique_id`, `fund_status`, `last_nav_date` to `funds` |
| 003 | `003_widen_fund_code.py` | Widens `fund_code` from VARCHAR(20) to VARCHAR(50) across all referencing tables |
| 004 | `004_ai_summary.py` | Creates `portfolio_ai_summaries` |

---

## 5. Backend API Reference

All routes prefixed with `/api/v1`. Authentication via `Authorization: Bearer <jwt>` unless noted. Wired in `backend/app/main.py:72-81`.

### Auth — `backend/app/api/auth.py`
- `POST /auth/token` — body `{email, password}` → `{access_token, token_type}`. **No auth.**

### Users — `backend/app/api/users.py`
- `GET    /users/me` — current user profile
- `PATCH  /users/me` — update `date_of_birth` and/or `password`
- `GET    /users` — list (admin)
- `POST   /users` — create (admin)
- `GET    /users/{user_id}` — get (admin)
- `PATCH  /users/{user_id}` — update (admin)
- `DELETE /users/{user_id}` — delete (admin)

### Portfolios — `backend/app/api/portfolios.py`
- `GET    /portfolios` — list current user's portfolios
- `POST   /portfolios` — create
- `GET    /portfolios/{id}`
- `PATCH  /portfolios/{id}` — rename
- `DELETE /portfolios/{id}`
- `POST   /portfolios/{id}/transfer-holding` — move all units of one fund to another portfolio (preserves lots)
- `POST   /portfolios/{id}/analytics/refresh` — invalidate cache

### Transactions — `backend/app/api/transactions.py`
- `GET    /portfolios/{id}/transactions`
- `POST   /portfolios/{id}/transactions` — BUY / SELL / DIVIDEND / INTEREST
- `POST   /portfolios/{id}/transactions/switch` — body has both `out` and `in` legs; service creates pair_id
- `POST   /portfolios/{id}/transactions/import-csv` — multipart file upload
- `DELETE /portfolios/{id}/transactions/{transaction_id}` — also rebuilds affected lots

### Tax Lots — `backend/app/api/transactions.py` (`lots_router`)
- `GET /portfolios/{id}/lots` — list open lots

### Funds — `backend/app/api/funds.py`
- `GET   /funds/search?q={query}` — case-insensitive, max 20
- `GET   /funds`
- `POST  /funds` (admin)
- `GET   /funds/{fund_code}`
- `PATCH /funds/{fund_code}` (admin)
- `GET   /funds/{fund_code}/nav?limit=365`

### Analytics — `backend/app/api/analytics.py`
- `GET  /portfolios/{id}/analytics/summary` — total value, cost basis, unrealized & realized P&L, XIRR, TWR
- `GET  /portfolios/{id}/analytics/holdings` — per-fund: units, NAV, cost, P&L, holding-period days
- `GET  /portfolios/{id}/analytics/allocation` — by asset class / AMC / scheme / risk level
- `GET  /portfolios/{id}/analytics/tax-eligibility` — per-lot countdown to tax-free
- `GET  /portfolios/{id}/analytics/ai-summary` — cached summary (or null)
- `POST /portfolios/{id}/analytics/ai-summary/refresh` — generate via Ollama
- `GET  /funds/{fund_code}/performance` — returns by window (7d, 30d, 6m, 1y, YTD, max)
- `GET  /funds/{fund_code}/risk-metrics` — Sharpe, max drawdown, annualized volatility
- `GET  /funds/{fund_code}/nav-history?days=365`
- `GET  /analytics/dividends?year={yyyy}` — across all user portfolios
- `GET  /analytics/dividend-years` — distinct years with dividends

### Sync (admin) — `backend/app/api/sync.py`
- `POST /sync/funds` — fund metadata (background)
- `POST /sync/nav?nav_date={date}` — NAV for one date (background)
- `POST /sync/nav/backfill?start_date={d}&end_date={d}&portfolio_only=true` — NAV range (background)
- `POST /sync/finnomena-nav` — fallback for ES- funds (background)
- `POST /sync/dividends` — dividend sync (background)
- `GET  /sync/jobs?limit=20`

### Health
- `GET /health` — returns `{"status": "ok"}`. **No prefix, no auth.**

---

## 6. Backend Services / Business Logic

### `auth_service.py`
JWT (HS256) + bcrypt. `hash_password`, `verify_password`, `create_access_token(subject)`, `decode_token(token) → user_id | None`.

### `lot_engine.py` (pure, no DB)
The deterministic core. Dataclasses `LotSnapshot`, `Consumption`, `NewLot`, `HoldingRule`; exception `InsufficientUnitsError`.
- `fifo_consume(lots, units_needed)` — FIFO over lots already filtered by `(fund_code, tax_scheme)`, oldest `original_purchase_date` first. Last lot may be partially consumed (proportional cost basis split).
- `build_switch_in_lots(consumptions, source_lots, target_fund_code, target_nav)` — for each consumed lot, emits a new lot in the target fund inheriting `original_purchase_date`, `tax_scheme`, and `cost_basis_remaining`; units = cost / target NAV.
- `is_holding_eligible(rule, purchase_date, today, user_age)` — applies `holding_years` and optional `age_requirement` from `tax_scheme_rules`.

### `transaction_service.py` (DB layer)
- `apply_buy(db, tx)` — creates one `TaxLot`.
- `apply_sell(db, tx)` — loads matching open lots, calls `fifo_consume`, mutates lot rows, writes `lot_consumptions`.
- `apply_switch(db, tx_out, tx_in)` — closes source lots via FIFO; `build_switch_in_lots` produces target-fund lots; both legs share `pair_id`.
- `rebuild_lots(portfolio_id, db)` — replays all transactions chronologically when a transaction is deleted (idempotent).

### `portfolio_service.py`
Portfolio-level analytics with a **5-minute in-memory cache** (keyed by portfolio id).
- `get_summary` — total value (current NAV × units), cost basis, unrealized P&L, realized P&L (from `lot_consumptions`), XIRR, TWR.
- `get_holdings`, `get_allocation`, `get_tax_eligibility`.
- `_xirr_solve` — `scipy.optimize.brentq` on the NPV function with daily granularity (per CLAUDE.md money math).
- `invalidate_portfolio(portfolio_id)` — called on mutations.

### `performance_service.py`
Fund-level NAV analytics. Returns over 7d / 30d / 6m / 1y / YTD / max. Risk metrics use weekly NAV returns: Sharpe (1.5% risk-free), max drawdown, annualized volatility.

### `sec_api.py`
Throttled (≤9 req/s) `httpx` client with retry on HTTP 421/429 (respecting the response wait header). Endpoints:
- FundDailyInfo — `/amc`, `/fund/{unique_id}`, `/fund/{proj_id}/nav`, `/fund/{proj_id}/dividends`
- FundFactsheet — `/amc`, `/fund/{amc_id}/{fund_code}`

### `sync_service.py`
- `sync_fund_metadata(db)` — walks AMCs → funds via FundFactsheet, upserts `Fund` rows.
- `sync_nav_for_date(db, date, proj_ids=None)` and `sync_nav_range(db, start, end, proj_ids=None)`.
- `sync_dividends(db, proj_ids=None)` — auto-creates `DIVIDEND` transactions when new dividends found (deduped by `ex_date + fund_code`, per CLAUDE.md).
- `get_portfolio_proj_ids(db)` — limits NAV sync to funds actually held.
- Writes a row per run to `sync_jobs` with start/end/status/error.

### `csv_import.py`
Pure parse + validation, then handed to `transaction_service`. Validations per row: type ∈ enum, scheme ∈ enum, `units × nav` == `amount` within ฿0.01 for trade rows. Cross-row: SWITCH pairs (same `pair_id`, same date, amount within 0.5%); reject duplicates of `(date, type, fund_code, units, amount)` already in the portfolio.

### `ai_service.py`
Thai-language portfolio analysis via Ollama. Builds a prompt with portfolio summary + top movers; persists result to `portfolio_ai_summaries` so subsequent reads are cached.

### `finnomena_service.py`
OAuth-based NAV scraper for `ES-`-prefixed Eastspring funds. Used when SEC data isn't available; kept as a fallback after SEC began publishing ES funds.

---

## 7. Frontend Pages & Components

### Pages (Next.js App Router)

**`src/app/page.tsx`** — checks `localStorage.token`; redirects to `/dashboard` (token present) or `/login`.

**`src/app/login/page.tsx`** — email/password form → `api.login()` → `saveToken()` → push `/dashboard`.

**`src/app/dashboard/page.tsx`** — aggregate dashboard across all of the current user's portfolios:
- KPI bar (total value, unrealized P&L, total income, total return %)
- Top 8 holdings by value
- Top 8 gainers / 8 losers
- Dividend income summary with year filter
- Allocation bars by asset class and tax scheme
- Stale NAV warning
- Portfolio CRUD (create / rename / delete)

**`src/app/dashboard/portfolios/[id]/page.tsx`** — portfolio detail with 5 tabs:
- **Summary** — KPIs + holdings table
- **Performance** — P&L ranking bar chart (Recharts), per-fund returns table (sortable), risk metrics, benchmark column, NAV history line chart
- **Allocation** — pie charts (Recharts) by asset class, AMC, tax scheme, risk level
- **Tax Lots** — collapsible per-fund tree with eligibility countdown per lot
- **Transactions** — table + add/delete + switch form + CSV import (with template)

**`src/app/dashboard/settings/page.tsx`** — P&L display basis (fund-entry vs. original cost, with/without dividends), date-of-birth input (for RMF age check), theme toggle, logout.

**`src/app/dashboard/sync/page.tsx`** — admin-only sync controls: Fund Metadata Sync, NAV Sync (single date), NAV Backfill (range + portfolio-only toggle), Dividend Sync, Finnomena Sync. Polling on `/sync/jobs`.

### Components (`src/components/ui/`)
shadcn primitives on Radix: `badge`, `button`, `card`, `dialog`, `dropdown-menu`, `input`, `label`, `select`, `tabs`, `theme-toggle`.

### Lib (`src/lib/`)
- **`api.ts`** — typed client (~40 methods). Base path `/api/v1{path}`. JWT stored as `localStorage.token`; added as `Authorization: Bearer …` header. Special-cases the CSV upload (FormData, no JSON Content-Type). Helpers: `saveToken`, `clearToken`.
- **`settings.ts`** — persists P&L display preference (`tft_settings` key) and emits a `storage` event so other tabs sync.
- **`utils.ts`** — `cn()` (clsx + tailwind-merge).

### Styling
- Tailwind `darkMode: "class"`. HSL CSS variables in `globals.css` for `:root` (light) and `.dark`.
- Inline script in `layout.tsx` sets the class from `localStorage.theme` or system `prefers-color-scheme` before paint to avoid FOUC.
- Charts use a fixed 10-color palette for pie segments. P&L uses `text-green-600 dark:text-green-400` / `text-red-600 dark:text-red-400`.

### Auth flow on the client
1. Login → store token in `localStorage` → push `/dashboard`.
2. Every API call attaches `Authorization: Bearer <token>`.
3. No silent refresh. On any 401 the page clears the token and redirects to `/login`.
4. Token TTL = `ACCESS_TOKEN_EXPIRE_MINUTES` from backend (default 24h).

---

## 8. Features (as currently implemented)

### Auth & users
- Admin-managed users; first admin seeded from env on first run.
- JWT login with 24h expiry; user `is_active` flag respected.
- Self-service: change own password, set date of birth.
- Admin: full user CRUD.

### Portfolios
- CRUD per user.
- Cross-portfolio transfer of a fund holding (all lots) — same-user only.

### Transactions
- All 6 types: BUY, SELL, SWITCH_OUT, SWITCH_IN, DIVIDEND, INTEREST.
- Form-based entry + CSV bulk import (schema in CLAUDE.md / template downloadable from UI).
- Delete a transaction → lot engine rebuilds from scratch.

### Tax lot engine
- FIFO consumption filtered by `(fund_code, tax_scheme)`.
- Switches preserve `original_purchase_date`, `tax_scheme`, and total cost basis across hops.
- Audit trail via `lot_consumptions` (one row per affected lot per mutation).
- Holding-period eligibility computed from `tax_scheme_rules` (data-driven; no hardcoded years).
- Supports 6 schemes: NORMAL, RMF (5y + age 55), SSF (10y), THAI_ESG (5y), THAI_ESG_EXTRA, LTF (legacy 5y).

### Fund & NAV data
- Fund registry with metadata (asset class, AMC, risk level 1–8, benchmark).
- Daily NAV history.
- Dividends recorded per fund; auto-imported dividends become `DIVIDEND` transactions.
- Fund search (code + name, case-insensitive).

### Sync (SEC API)
- Manual: metadata, NAV (single date), NAV backfill (range), dividends, Finnomena fallback.
- Scheduled (APScheduler, see §10).
- Per-fund failure isolation; failures logged into `sync_jobs.error_message`.

### Analytics
- Per-portfolio summary: total value, cost basis, unrealized P&L, realized P&L, XIRR, TWR.
- Per-fund performance windows: 7d, 30d, 6m, 1y, YTD, max.
- Per-fund risk metrics: Sharpe, max drawdown, annualized volatility.
- Allocation pies: asset class, AMC, tax scheme, risk level.
- Tax holding tracker per lot with countdown and a warning UI before disqualifying sells.
- Dividend income summary across all portfolios with year filter.
- 5-minute in-memory cache; invalidated on transaction mutations.

### Dashboard (aggregate)
- KPI bar, top holdings, gainers/losers (top 8 each), dividend summary, allocation bars, stale NAV alert.

### AI summary
- One Thai-language paragraph per portfolio, generated by Ollama, cached in DB.

### UX
- Dark mode (Tailwind `class` strategy).
- Mobile form fix (recent commit); responsive layout exists but isn't fully polished at <375px.
- Sortable performance/risk table.

---

## 9. Domain Rules in Code (invariants)

| Rule | Enforced in |
|---|---|
| `tax_lot.original_purchase_date` is set on lot creation and never mutated | `transaction_service.apply_buy` / `apply_switch` (creates new lot with date from source) |
| FIFO scoped to `(fund_code, tax_scheme)` — never crosses schemes | `lot_engine.fifo_consume` (input list is pre-filtered) |
| Switch preserves total cost basis | `lot_engine.build_switch_in_lots` (cost basis split proportionally, then new lot's units = cost / target NAV) |
| Holding-period rules are data, not code | `tax_scheme_rules` table + `lot_engine.is_holding_eligible` |
| All money/units are Decimal in Python, `numeric(20,8)` in DB | Throughout models + services |
| Every lot mutation is audited | `lot_consumptions` rows written in same DB transaction as the lot UPDATE |
| `SWITCH_OUT` and `SWITCH_IN` share `pair_id`, same date, amount within 0.5% | `transaction_service.apply_switch` + `csv_import` validations |

---

## 10. Background Jobs (APScheduler)

Wired in `backend/app/main.py:54-56` (lifespan). All times UTC.

| Job | Trigger | Action |
|---|---|---|
| `_nightly_nav_sync` | Mon–Fri, 12:30 UTC (19:30 ICT) | `sync_service.sync_nav_for_date(db, date.today())` |
| `_weekly_metadata_sync` | Sunday, 01:00 UTC | `sync_service.sync_fund_metadata(db)` |
| `_daily_dividend_sync` | Daily, 13:30 UTC (20:30 ICT) | `sync_service.sync_dividends(db)` |

Scheduler is `AsyncIOScheduler` running in-process; no separate worker.

---

## 11. Testing

`pytest` + `pytest-asyncio`. Async DB tests use SQLite in-memory via `aiosqlite`.

| File | Coverage |
|---|---|
| `backend/app/tests/test_lot_engine.py` | FIFO ordering, partial consumption, switch lot construction, holding-period eligibility |
| `backend/app/tests/test_csv_import.py` | CSV parse, row validation, switch-pair validation, deduplication |
| `backend/app/tests/test_analytics.py` | XIRR, P&L, holdings, allocation, tax eligibility |
| `backend/app/tests/test_sec_sync.py` | Throttled SEC client (200/401/421/404), sync service with mocked httpx |

Run: `pytest backend/app/tests/ -v` (from repo root inside the backend env).

---

## 12. Quick-Start Pointers

1. Copy `.env.example` → `.env`; set `POSTGRES_PASSWORD`, `SECRET_KEY`, `FIRST_ADMIN_EMAIL`, `FIRST_ADMIN_PASSWORD`, `SEC_API_KEY`.
2. `docker compose up -d` — postgres on 5432, backend on 8000, frontend on 3000.
3. Visit `http://localhost:3000`, log in as the seeded admin.
4. **Sync page** → run **Fund Metadata Sync**, then **NAV Backfill** for the date range you need.
5. Create a portfolio, add transactions (or import a CSV from the template).

---

## 13. Out of Scope (do not build without explicit ask)

Mirrors CLAUDE.md: no DCA scheduler, no rebalancing alerts, no goal-based investing, no PDF/email statement parsing, no fee impact analysis, no self-registration, no tax-loss harvesting suggestions, no year-end tax filing reports.
