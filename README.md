# Thai Fund Portfolio Tracker

A self-hosted web app for tracking Thai mutual fund investments. Pulls NAV and dividend data from the Thailand SEC Open Data API, tracks tax lots through fund switches, and computes portfolio performance with proper handling of Thai tax-advantaged schemes (RMF, SSF, ThaiESG, LTF).

---

## Features

### Portfolio Management
- Multi-user with admin-managed accounts (no self-registration)
- Each user can have multiple named portfolios
- Rename and delete portfolios
- Move a fund holding (with all its lots and transactions) between portfolios

### Transactions
Supports all 6 transaction types:
- **BUY** — creates a new tax lot
- **SELL** — FIFO consumes lots within the requested tax scheme
- **SWITCH_OUT / SWITCH_IN** — paired transactions that transfer lots between funds, preserving original purchase date and cost basis
- **DIVIDEND** — records gross/net/withholding per fund
- **INTEREST** — portfolio-level cash interest

### CSV Import
Bulk import transactions from CSV. Template downloadable from the UI.

```csv
date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note
2024-03-15,BUY,SCBSET,1000,12.3456,12345.60,0,0,,,NORMAL,
2024-08-20,SELL,SCBSET,500,13.2100,6605.00,33.03,0,,,NORMAL,
2024-09-10,SWITCH_OUT,SCBSET,500,13.5000,6750.00,0,0,SCBTOP,switch-001,NORMAL,
2024-09-10,SWITCH_IN,SCBTOP,450,15.0000,6750.00,0,0,SCBSET,switch-001,NORMAL,
2024-12-15,DIVIDEND,SCBSET,,,250.00,0,25.00,,,NORMAL,Q4 dividend
2024-12-31,INTEREST,,,,150.50,0,15.05,,,NORMAL,Cash interest
```

### Tax Lot Engine
- FIFO consumption within each tax scheme (SSF lots are never mixed with RMF lots)
- Full switch tracking: `original_purchase_date` is never changed — even after A→B→C chain switches
- Audit trail: every lot mutation logged in `lot_consumptions`
- Full lot rebuild when a transaction is deleted (replay from scratch)

### Tax Schemes
| Scheme | Holding Requirement |
|---|---|
| NORMAL | None |
| RMF | 5 years AND age ≥ 55 |
| SSF | 10 calendar years |
| THAI_ESG | 5 years |
| THAI_ESG_EXTRA | Configurable (stored as data) |
| LTF | 5 calendar years (legacy) |

Rules are stored in `tax_scheme_rules` table — no code changes needed to update them.

### Analytics
- **Summary**: total portfolio value, unrealized P&L, realized P&L, XIRR (money-weighted annualized return)
- **Holdings table**: per fund/scheme — units, cost basis, market value, P&L, age, dividends received
- **P&L basis selector**: choose between original cost vs fund-entry NAV, with or without dividend income
- **Performance tab**: P&L ranking bar chart, fund timeframe returns (7D / 30D / 6M / 1Y / YTD / MAX), Sharpe ratio, Max Drawdown, annualized volatility
- **Allocation tab**: pie charts by asset class, AMC, tax scheme, SEC risk level (1–8)
- **Tax Lots tab**: collapsible scheme→fund→lot tree with eligibility status, countdown to eligibility date, full switch chain ancestry
- **Dividend income table**: gross, net, withholding tax, yield per fund

### Dashboard Overview
Across all portfolios:
- Aggregate KPIs: total value, unrealized P&L, income (dividends + realized), total return %
- Top holdings by value
- Top gainers / underperformers
- Allocation bars by asset class and tax scheme
- Stale NAV warning (flags funds with NAV older than 5 days)

### SEC Data Sync (Thailand Open Data API)
Admin-only sync panel:
- **Fund Metadata Sync**: discovers all AMCs and funds, populates `sec_proj_id`, asset class, risk level
- **NAV Sync**: fetches daily NAV for all active funds (or portfolio-only subset)
- **NAV Backfill**: fill historical NAV between a date range
- **Dividend Sync**: fetches full dividend history per fund, deduplicates by (fund, ex_date)
- **Scheduled**: nightly NAV at 19:30 ICT weekdays, Sunday metadata sync, daily dividend at 20:30 ICT

### AI Portfolio Analysis
Generates a Thai-language 3–4 sentence portfolio summary via a local Ollama model. Covers P&L overview, best/worst performing funds, risk profile, and a one-line recommendation. Result is cached in the database and refreshable on demand.

### Fund Search
Autocomplete search by fund code or name against the SEC fund database when adding transactions.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (Python 3.12+), SQLAlchemy 2.x async, Alembic, APScheduler |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind CSS, shadcn/ui, Recharts |
| Database | PostgreSQL 16 |
| Auth | JWT (HS256), 24-hour token expiry |
| Containerization | Docker Compose |
| AI | Ollama (local LLM, default: gemma4:26b) |

---

## Quick Start

### Prerequisites
- Docker and Docker Compose
- A Thailand SEC Open Data API key (free, register at https://api-portal.sec.or.th/)
- Ollama running locally (optional, for AI summaries)

### 1. Clone and configure

```bash
git clone https://github.com/bitcodss/thaifund-port-mgt.git
cd thaifund-port-mgt
cp .env.example .env
```

Edit `.env` — see [Configuration](#configuration) below.

### 2. Start services

```bash
docker compose up -d
```

This starts:
- `postgres` on internal port 5432
- `backend` on port 8000
- `frontend` on port 3000

The backend runs `alembic upgrade head` automatically on startup.

### 3. Access the app

Open `http://localhost:3000` and log in with the admin credentials set in `.env`.

### 4. Initial data sync (admin only)

Go to **Sync** page and run:
1. **Fund Metadata Sync** — discovers all SEC funds and their `sec_proj_id`
2. **NAV Backfill** — pull historical NAV for your date range (e.g. 2020-01-01 to today)

---

## Configuration

All configuration is via environment variables in a `.env` file at the project root.

### Required

| Variable | Description |
|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL password |
| `SECRET_KEY` | JWT signing secret — use a long random string |
| `FIRST_ADMIN_EMAIL` | Email for the initial admin account (created on first startup) |
| `FIRST_ADMIN_PASSWORD` | Password for the initial admin account |

### SEC API Keys

Register at https://api-portal.sec.or.th/ and subscribe to the two API products:

| Variable | API Product | Used For |
|---|---|---|
| `SEC_API_KEY` | Fund Daily Info | NAV sync, dividend sync, AMC list |
| `SEC_FACTSHEET_KEY` | Fund Factsheet | Fund metadata (name, asset class, risk level) |

If `SEC_FACTSHEET_KEY` is not set, it falls back to `SEC_API_KEY`. Some metadata endpoints require a separate Factsheet subscription.

Optional secondary key for failover:

| Variable | Description |
|---|---|
| `SEC_API_KEY_SECONDARY` | Backup key if primary hits quota |

### Database (optional overrides)

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_HOST` | `postgres` | Hostname (use `postgres` for Docker Compose) |
| `POSTGRES_PORT` | `5432` | Port |
| `POSTGRES_DB` | `thaiund` | Database name |
| `POSTGRES_USER` | `thaiuser` | Database user |

### Auth

| Variable | Default | Description |
|---|---|---|
| `ALGORITHM` | `HS256` | JWT algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | Token lifetime (24 hours) |

### Ollama (AI summaries)

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `gemma4:26b` | Model to use for Thai-language summaries |

To use a different model, pull it first: `ollama pull <model>` then set `OLLAMA_MODEL=<model>`.

If Ollama is not available, AI summaries show an error message — all other features work normally.

### Finnomena (fallback only)

| Variable | Description |
|---|---|
| `FINNOMENA_EMAIL` | Finnomena account email |
| `FINNOMENA_PASSWORD` | Finnomena account password |

This is a fallback for Eastspring (ES-) funds. As of 2026, these funds are available directly via the SEC API using their `sec_proj_id`. The Finnomena sync path is kept but rarely needed.

### Example `.env`

```env
# Database
POSTGRES_PASSWORD=change_me_strong_password

# Auth
SECRET_KEY=your-very-long-random-secret-key-here
FIRST_ADMIN_EMAIL=admin@example.com
FIRST_ADMIN_PASSWORD=change_me_admin_password

# SEC API
SEC_API_KEY=your-sec-dailyinfo-api-key
SEC_FACTSHEET_KEY=your-sec-factsheet-api-key

# Ollama (optional)
OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_MODEL=gemma4:26b
```

---

## SEC API Rate Limits

The Thailand SEC Open Data API allows **3,000 requests per 300 seconds** (~10 req/s). The backend throttles at 9 req/s and handles HTTP 421 (rate limit) with exponential backoff using the `Retry-After` response header.

For full NAV backfill (all funds, multiple years), expect the job to take tens of minutes. Use the portfolio-only backfill option to only fetch funds present in your transactions, which is much faster.

---

## Database Schema

```
users              — id, email, password_hash, role, date_of_birth, is_active
portfolios         — id, user_id, name
transactions       — id, portfolio_id, date, type, fund_code, units, nav,
                     amount, fee, tax_withheld, target_fund_code, pair_id,
                     tax_scheme, note
tax_lots           — id, portfolio_id, fund_code, original_purchase_date,
                     units_remaining, cost_basis_remaining, tax_scheme,
                     source_lot_id
lot_consumptions   — id, transaction_id, lot_id, units_consumed, cost_basis_consumed
                     (audit trail — every lot mutation logged here)
funds              — fund_code (PK), sec_proj_id, name_th, name_en, amc,
                     asset_class, risk_level, benchmark, fund_status
nav_history        — fund_code + trade_date (composite PK), nav, change_pct
dividends          — id, fund_code, ex_date, payment_date, dividend_per_unit, source
tax_scheme_rules   — scheme (PK), holding_years, age_requirement, active_from
sync_jobs          — id, type, started_at, completed_at, status, error_message
portfolio_ai_summaries — portfolio_id (PK), content, generated_at
```

All monetary and unit columns use `numeric(20, 8)`. No floats in financial calculations.

---

## Alembic Migrations

Migrations run automatically on backend startup. To run manually:

```bash
docker compose exec backend alembic upgrade head
```

To create a new migration:

```bash
docker compose exec backend alembic revision --autogenerate -m "description"
```

---

## Development

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run tests (requires a running Postgres)
pytest app/tests/
```

### Frontend

```bash
cd frontend
npm install
npm run dev   # http://localhost:3000
```

---

## Project Structure

```
thai-fund-tracker/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic/              # DB migrations
│   └── app/
│       ├── main.py           # FastAPI app + APScheduler
│       ├── config.py         # Settings from .env
│       ├── database.py       # Async SQLAlchemy session
│       ├── api/              # Route handlers
│       │   ├── auth.py
│       │   ├── users.py
│       │   ├── portfolios.py
│       │   ├── transactions.py
│       │   ├── funds.py
│       │   ├── sync.py
│       │   └── analytics.py
│       ├── models/           # SQLAlchemy ORM models
│       ├── schemas/          # Pydantic request/response schemas
│       ├── services/         # Business logic
│       │   ├── lot_engine.py         # Pure FIFO logic
│       │   ├── transaction_service.py # DB wrapper for lot engine
│       │   ├── portfolio_service.py   # Holdings, XIRR, allocation
│       │   ├── performance_service.py # NAV returns, risk metrics
│       │   ├── sync_service.py        # SEC API orchestration
│       │   ├── sec_api.py             # Throttled SEC API client
│       │   ├── finnomena_service.py   # Fallback NAV (Eastspring)
│       │   ├── csv_import.py          # CSV parse + validation
│       │   └── ai_service.py          # Ollama Thai summary
│       └── tests/
└── frontend/
    ├── Dockerfile
    ├── next.config.js
    └── src/
        ├── app/
        │   ├── dashboard/page.tsx           # Portfolio overview
        │   ├── dashboard/portfolios/[id]/   # Portfolio detail (5 tabs)
        │   ├── dashboard/sync/page.tsx      # Admin sync panel
        │   └── dashboard/settings/page.tsx  # User preferences
        ├── components/ui/                   # shadcn/ui components
        └── lib/
            ├── api.ts        # Typed API client
            └── settings.ts   # P&L basis preference (localStorage)
```

---

## Deployment Notes

- The app is designed for **single-instance self-hosted** use on a LAN or VPS
- APScheduler runs inside the backend process — no separate worker needed
- For HTTPS, add a Caddy or nginx reverse proxy in front of the frontend container
- Data is persisted in a named Docker volume (`postgres_data`)

---

## Out of Scope

The following are intentionally not implemented:
- DCA scheduler / planner
- Rebalancing alerts
- Self-registration (admin creates all accounts)
- PDF / email statement parsing (CSV import only)
- Year-end tax filing reports (withholding amounts are stored; filing is manual)
- Goal-based investing features
