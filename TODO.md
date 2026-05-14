# Code Review & Bug Hunt Plan

Strategy: prioritize by *blast radius* (money-correctness > auth > data integrity > UX), since the dangerous bugs in this codebase are the financial ones.

---

## Phase 1 — Scope & Severity Rubric

**In scope:** correctness bugs, security vulnerabilities, data-integrity risks, race conditions, error handling, edge cases.

**Out of scope:** style nits, formatting, missing comments, refactors, "could be more idiomatic" suggestions.

**Severity tiers** (applied to every finding):
- **Critical** — incorrect money/units, lost data, auth bypass, persistent corruption
- **High** — wrong analytics output, broken sync, lost transactions on errors
- **Medium** — UX bugs, edge-case crashes, errors swallowed silently
- **Low** — minor input validation, suboptimal queries

---

## Phase 2 — Priority Targets (ordered by risk)

### Tier 1: The Lot Engine (money-correctness) — HIGHEST PRIORITY
Files: `backend/app/services/lot_engine.py`, `backend/app/services/transaction_service.py`, `backend/app/models/tax_lot.py`, `backend/app/models/transaction.py`

Bug categories:
- [ ] FIFO rounding: when last-lot is partially consumed, do `units_consumed` and `cost_basis_consumed` sum back to requested totals at `numeric(20,8)` precision? Look for `Decimal` quantization drift.
- [ ] Switch cost-basis preservation: total cost in == total cost out across the pair? Re-derive on chained A→B→C scenarios.
- [ ] `original_purchase_date` immutability — grep for any assignment outside `apply_buy` and SWITCH_IN.
- [ ] Tax scheme isolation: confirm `fifo_consume` callers always pre-filter by `(fund_code, tax_scheme)`. A missing filter would let SELL silently consume RMF lots.
- [ ] `rebuild_lots` on transaction delete — transactional? Half-failed state? Are `lot_consumptions` properly wiped before replay?
- [ ] Edge case: SELL/SWITCH_OUT requesting more units than open → does `InsufficientUnitsError` surface as 4xx, or 500?
- [ ] `source_lot_id` audit chain: does deleting a transaction that originated a switch break the chain in the target portfolio?

### Tier 2: Analytics Math
Files: `backend/app/services/portfolio_service.py`, `backend/app/services/performance_service.py`

Bug categories:
- [ ] XIRR: empty cash-flow list, all-positive or all-negative flows, single cash flow, two flows on the same day. Does `scipy.optimize.brentq` always get a bracket containing a root?
- [ ] TWR: sub-period definition, handling of cash flows on weekends/holidays, period with zero NAV move.
- [ ] Realized P&L: fees double-counted (on transaction AND in cost basis)?
- [ ] Performance windows (7d/30d/6m/1y/YTD/max): missing NAV on anchor date? `_nav_on_or_before` fallback correct? Fund didn't exist yet?
- [ ] Sharpe ratio: hard-coded 1.5% risk-free — annualized correctly? Weekly returns → annualized vol must multiply by √52.
- [ ] Max drawdown: strictly-increasing NAV series should be 0, not NaN.
- [ ] 5-minute in-memory cache: invalidated on **every** mutation (transactions, switch, CSV import, delete, transfer-holding)? Process-local → won't sync across workers if scaled (deployment landmine).

### Tier 3: Auth & Authorization
Files: `backend/app/api/deps.py`, `backend/app/api/auth.py`, `backend/app/api/users.py`, `backend/app/api/portfolios.py`, `backend/app/api/sync.py`, `backend/app/services/auth_service.py`

Bug categories:
- [ ] **IDOR**: every route taking `portfolio_id`/`transaction_id`/`user_id` verifies ownership? Grep for handlers loading by ID without `current_user.id` check.
- [ ] `require_admin` coverage: every sync endpoint and `/users/{id}` admin route.
- [ ] JWT: expiry honored? Algorithm pinned to HS256 (defense vs. `alg=none`)? `decode_token` swallowing all `JWTError` — masks malformed vs. expired.
- [ ] Password hashing: bcrypt cost factor, length cap (silently truncates at 72 bytes).
- [ ] CORS is `allow_origins=["*"]` with `allow_credentials=True` — invalid per spec and risky.
- [ ] No rate limiting on `/auth/token` — credential stuffing wide open.
- [ ] `transfer-holding`: both portfolios belong to same user before moving lots?

### Tier 4: CSV Import & Input Validation
Files: `backend/app/services/csv_import.py`, `backend/app/api/transactions.py`

Bug categories:
- [ ] Duplicate detection: is `(date, type, fund_code, units, amount)` enough? Two genuine identical BUYs same day?
- [ ] Switch pair detection: `pair_id` reused across portfolios or days?
- [ ] Decimal parsing: locale separators (`,` vs `.`), leading/trailing spaces, scientific notation.
- [ ] File size limits — multipart upload of 1GB file?
- [ ] Header validation — extra/missing columns gracefully?
- [ ] Cross-row validation: SWITCH_OUT must precede SWITCH_IN in row order?

### Tier 5: SEC Sync Resilience
Files: `backend/app/services/sec_api.py`, `backend/app/services/sync_service.py`, `backend/app/services/finnomena_service.py`

Bug categories:
- [ ] Throttling: 9 req/s claim vs. 3000/300s = 10 req/s limit. Off-by-one → 421 storms.
- [ ] Retry on 421/429: reads response wait-header or hardcoded delay?
- [ ] `sync_nav_range` partial failure: single date failing in 365-day backfill aborts rest?
- [ ] Idempotency: re-running `sync_nav_for_date(today)` no-op/upsert, never duplicate.
- [ ] `sync_dividends` auto-creating `DIVIDEND` transactions: which portfolio? Dedupe applied at transaction level?
- [ ] `sync_jobs` status: process crash mid-job → row stuck `running`? Startup cleanup?
- [ ] Secondary SEC key failover: triggers on quota errors specifically, or any error?
- [ ] Finnomena password: pulled at runtime from env or persisted?

### Tier 6: Concurrency & DB Transactions
Cross-cutting.

Bug categories:
- [ ] `apply_sell` / `apply_switch` read-then-write on `tax_lots` — row-level lock? Two simultaneous SELLs over-consuming.
- [ ] Sync jobs concurrent with user transaction adds — lock contention or stale reads?
- [ ] Lot mutations wrapped in `async with db.begin()` so lot row + `lot_consumptions` commit atomically?
- [ ] APScheduler jobs running concurrently — NAV sync vs. dividend sync touching same fund?

### Tier 7: Frontend
Files: `frontend/src/app/**`, `frontend/src/lib/api.ts`

Bug categories:
- [ ] 401 handling: global response interceptor? `api.ts` throws on `!res.ok` but no special 401 → each page must catch and redirect (likely inconsistent).
- [ ] Token in `localStorage` is XSS-readable; review `innerHTML` / `dangerouslySetInnerHTML`.
- [ ] Decimal display rounding: `Number.toFixed(2)` on `Decimal` strings coerces through float, corrupts precision.
- [ ] Date handling: UTC server times rendered as local — Bangkok user sees correct trade date?
- [ ] Loading/error states: tabs/cards handle `null`/`undefined` gracefully or show "NaN"/"Invalid Date"?
- [ ] CSV import error display: row-level errors or just "import failed"?
- [ ] Forms: submit-while-submitting, double-click protection?
- [ ] Stale-NAV alert logic: how stale is "stale"? Off-by-one on weekends?

---

## Phase 3 — Methodology

For each tier:

1. **Read tests first** (`backend/app/tests/*`) — what's covered tells you what *isn't*. Missing scenarios from CLAUDE.md's "Testing Priorities" are red flags.
2. **Read the implementation** with the bug categories in mind.
3. **Grep for patterns**:
   - `float(` in money paths → red flag (should be `Decimal`)
   - `.toFixed(` on currency in frontend → red flag
   - `try: ... except: pass` → swallowed errors
   - `# TODO` / `# FIXME` / `# XXX`
   - `assert` in business logic (Python `-O` disables asserts)
   - Raw SQL strings → injection check
4. **Trace a transaction end-to-end** for: BUY, SELL, partial SELL, full switch, partial switch, CSV import of mixed batch, deletion-then-rebuild. Note untested branches.
5. **Build adversarial scenarios** matching CLAUDE.md's invariants and check the code actually enforces them — don't trust comments.

---

## Phase 4 — Deliverable

A `code-review-findings.md` at the repo root, structured as:

```
# Code Review Findings — <date>

## Summary
- N Critical, N High, N Medium, N Low

## Critical
### C1. <Short title>
- Location: file:line
- Evidence: <minimal repro / code excerpt>
- Impact: <what breaks, who notices>
- Suggested fix: <one paragraph>

## High
### H1. ...

## Medium / Low
...

## Not-bugs but worth flagging
<deployment landmines, missing tests, observability gaps>
```

Every finding includes `file:line` citations so it's independently verifiable.

---

## Phase 5 — Stretch Checks (only if time remains)

- Dependency audit: `pip-audit` / `npm audit` for known CVEs.
- Migration safety: 001→004 run cleanly on empty DB? Any irreversible `downgrade()`?
- Docker config: containers run as non-root? Secrets baked into images?
- Backup story: Postgres volume only — flag if not.

---

## Effort Estimate

- Tiers 1–3: ~60–70% of review time (highest leverage)
- Tiers 4–6: next priority
- Tier 7 (frontend): last — catches presentation bugs but won't surface dangerous money-correctness ones
