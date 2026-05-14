# Code Review Findings — 2026-05-14

Bug-hunt review of the Thai Fund Tracker, prioritizing money-correctness over UX. Plan: `TODO.md`. This file is a living document — tiers are added as the review proceeds.

## Summary

All tiers complete.

| Severity | Count | Tier 1 | Tier 2 | Tier 3 | Tier 4 | Tier 5 | Tier 6 | Tier 7 |
|---|---|---|---|---|---|---|---|---|
| Critical | 2  | 1 | 1 | – | – | – | – | – |
| High     | 7  | 3 | 2 | – | – | 2 | – | – |
| Medium   | 18 | 4 | 4 | 3 | 2 | 3 | – | 2 |
| Low      | 10 | 3 | 1 | 3 | 1 | – | – | 2 |

---

# Tier 1 — Lot Engine

Files reviewed:
- `backend/app/services/lot_engine.py`
- `backend/app/services/transaction_service.py`
- `backend/app/api/transactions.py`
- `backend/app/schemas/transaction.py`
- `backend/app/tests/test_lot_engine.py`

---

## Critical

### C1. Switch endpoint silently accepts any source/target funds — domain invariants from CLAUDE.md not enforced
- **Location:** `backend/app/api/transactions.py:100-130` (`add_switch`), `backend/app/services/transaction_service.py:121-171` (`apply_switch`)
- **Evidence:** The switch endpoint takes two arbitrary `TransactionCreate` payloads and applies them. None of the CLAUDE.md switch invariants are validated:
  - **No check** that `switch_out.target_fund_code == switch_in.fund_code` (and vice versa). Users can submit `SWITCH_OUT` from FUND_A with `target_fund_code=FUND_X` while `SWITCH_IN` is for FUND_B — the code happily consumes from FUND_A and creates lots in FUND_B.
  - **No same-AMC check** (CLAUDE.md: "same AMC — Thai funds only allow same-AMC switching").
  - **No amount tolerance check** (CLAUDE.md: "within 0.5% to allow fees").
  - **No same-date check** (CLAUDE.md: "same date").
  - **No same-`tax_scheme` check** between legs.
  - `add_switch` overwrites `pair_id` server-side (good) but never validates the legs reference each other.
- **Impact:** Users can record nonsensical switches (e.g., SWITCH from RMF Fund A → NORMAL Fund B). Cost basis and `tax_scheme` are inherited from the source lot, so a user could effectively *move funds across tax schemes* through the switch path — corrupting tax-eligibility tracking. The portfolio's lot ledger becomes internally inconsistent with what the user actually did with the fund house.
- **Suggested fix:** In `add_switch`, validate before calling `svc.apply_switch`:
  ```python
  if switch_out.fund_code == switch_in.fund_code:
      raise HTTPException(400, "Switch source and target funds must differ")
  if switch_out.target_fund_code != switch_in.fund_code:
      raise HTTPException(400, "SWITCH_OUT.target_fund_code must match SWITCH_IN.fund_code")
  if switch_in.target_fund_code != switch_out.fund_code:
      raise HTTPException(400, "SWITCH_IN.target_fund_code must match SWITCH_OUT.fund_code")
  if switch_out.date != switch_in.date:
      raise HTTPException(400, "Switch legs must share a date")
  if switch_out.tax_scheme != switch_in.tax_scheme:
      raise HTTPException(400, "Switch legs must share a tax_scheme")
  tol = abs(switch_out.amount - switch_in.amount) / max(switch_out.amount, Decimal("0.01"))
  if tol > Decimal("0.005"):
      raise HTTPException(400, f"Switch amounts differ by {tol*100:.2f}% (max 0.5%)")
  # AMC check — load funds and compare amc / amc_unique_id
  ```
  Apply the same checks in `csv_import.py` for `import-csv`. The CSV importer at `transactions.py:206-236` only checks that exactly one OUT and one IN exist per `pair_id`.

---

## High

### H1. Race condition on concurrent SELL/SWITCH — no row lock on `tax_lots`
- **Location:** `backend/app/services/transaction_service.py:24-49` (`_open_lots`), `apply_sell`, `apply_switch`
- **Evidence:** `_open_lots` issues a plain `SELECT`. Two concurrent SELLs against the same portfolio/fund/scheme can both see the same `units_remaining`, both pass FIFO, and both write conflicting updates. The asyncpg default isolation is `READ COMMITTED`, which allows lost-update on read-modify-write.
- **Impact:** Two simultaneous SELLs can over-consume a lot (each SELL "thinks" it's consuming from a full lot). The end-state has `units_remaining` reduced by only the larger of the two amounts (last writer wins), so total ledger units no longer match the audit trail. Hard to reproduce in single-user use, but a power user double-clicking the SELL button (no client-side debounce — see T7) or running parallel CSV imports could trigger it.
- **Suggested fix:** Add `FOR UPDATE` to the lot read:
  ```python
  result = await db.execute(
      select(TaxLot)
      .where(...)
      .with_for_update()  # PostgreSQL row-lock
  )
  ```
  Or wrap the whole BUY/SELL/SWITCH path in `SERIALIZABLE` isolation and retry on serialization failure. The first option is simpler and sufficient.

### H2. `rebuild_lots` silently drops half-pair switches
- **Location:** `backend/app/services/transaction_service.py:109-117`
- **Evidence:**
  ```python
  pair = [t for t in txs if t.pair_id == tx.pair_id]
  out_tx = next((t for t in pair if t.type == "SWITCH_OUT"), None)
  in_tx  = next((t for t in pair if t.type == "SWITCH_IN"), None)
  if out_tx and in_tx:
      await apply_switch(db, out_tx, in_tx)
  # ↑ if one leg is missing, the switch is silently skipped
  ```
  `delete_transaction` does delete both legs atomically (line 258-267), so under normal use a half-pair shouldn't exist. But:
  - Direct DB edits, partial migrations, or a future code path could leave a half-pair.
  - A CSV import that adds a SWITCH_OUT/IN pair but where one row was deduped (matching an existing transaction by `(date, type, fund_code, units, amount)`) would create a half-pair — the importer's dedup logic at line 173 doesn't know about pair completeness.
- **Impact:** A half-pair SWITCH during rebuild causes the affected units to silently disappear from the ledger. Cost basis and units in the source fund are *not* deducted, the target fund gets *no* lots — net: lost data, no error.
- **Suggested fix:** Raise instead of silently skipping. Add a constraint or pre-check at CSV import that rejects a pair where one leg duplicates existing.

### H3. `cost_basis_remaining` drifts after partial consumptions
- **Location:** `backend/app/services/lot_engine.py:80-83`, `backend/app/services/transaction_service.py:76-77`
- **Evidence:** Partial consumption computes `cost = _q(lot.cost_basis_remaining * fraction)` with ROUND_HALF_UP at 1e-8 precision. After update, `lot_row.cost_basis_remaining -= c.cost_basis_consumed`. Each partial introduces a quantization residual of up to 0.5e-8 ฿. Successive partial sells on the same lot compound the drift.
  - The "exact" branch at line 76-78 only triggers when the residual is below `EPSILON` (1e-6), not on every partial — so a sequence like "sell 100, sell 100, sell 100" from a 500-unit lot leaves the lot with cost basis that doesn't equal the true unconsumed portion.
- **Impact:** Realized P&L (computed from `lot_consumptions.cost_basis_consumed`) and unrealized P&L (computed from `tax_lots.cost_basis_remaining`) can disagree by a few satang on heavily-traded lots. Audit invariants like "consumed + remaining == original" break.
- **Suggested fix:** On the last consumption that fully drains a lot, set `cost_basis_consumed = lot.cost_basis_remaining` regardless of partial-fraction math — this is what the engine *intends* but only does when the residual happens to fall below EPSILON. Better: compute `cost_basis_consumed` for the final consumption as `original_cost - sum(prior_consumptions)` so the audit trail always sums exactly.

---

## Medium

### M1. CSV import + endpoint accept invalid `tax_scheme` and `type` strings — no enum constraint
- **Location:** `backend/app/schemas/transaction.py:8-31`
- **Evidence:** `type: str` and `tax_scheme: str = "NORMAL"` are bare strings. `model_validator.validate_type_fields` only checks `type ∈ {BUY, SELL, SWITCH_OUT, SWITCH_IN}` for units/nav presence — `DIVIDEND/INTEREST` checks `fund_code`. But `type="FOO"` passes all validation and gets persisted. Similarly `tax_scheme="WHATEVER"` is accepted.
- **Impact:** Downstream code paths (`apply_buy/sell/switch`, allocation queries, tax eligibility) silently skip unknown types or scheme strings → data appears in the DB but doesn't show up in summaries. Hard-to-debug data divergence.
- **Suggested fix:** Use a `Literal` or `Enum` for both fields:
  ```python
  type: Literal["BUY", "SELL", "SWITCH_OUT", "SWITCH_IN", "DIVIDEND", "INTEREST"]
  tax_scheme: Literal["NORMAL", "RMF", "SSF", "THAI_ESG", "THAI_ESG_EXTRA", "LTF"] = "NORMAL"
  ```

### M2. Admin can transparently mutate any user's portfolio data
- **Location:** `backend/app/api/transactions.py:32-38` (`_get_portfolio`)
- **Evidence:** `if p.user_id != user.id and user.role != "admin"` — admin role bypasses ownership check, gaining full transaction CRUD (and CSV import, delete) on any user's portfolio. The README/CLAUDE.md describe admin as managing *user accounts*, not other users' financial data.
- **Impact:** Privilege creep. An admin can edit/delete another user's transactions and the user has no audit log to detect it (`lot_consumptions` is technical, not human-facing).
- **Suggested fix:** Drop the admin bypass in transactional endpoints. Admin should only have elevated access on `/users/*` and `/sync/*`. If "support" use cases need read access, expose a separate read-only admin route.

### M3. Stored `tax_scheme_rules` row is implicit on app start; `HoldingRule` is never loaded from DB by the code I reviewed
- **Location:** `backend/app/services/lot_engine.py:131-154` (function), then trace to callers
- **Evidence:** `is_holding_eligible` takes `HoldingRule` as a parameter, but the loader/caller path needs verification (likely in `portfolio_service.get_tax_eligibility`). The test file constructs HoldingRule directly; no test confirms the DB row → HoldingRule wiring matches the schemes used by transactions. If a transaction has `tax_scheme="THAI_ESG_EXTRA"` but the seed didn't insert that row, the eligibility check will crash or default-allow.
- **Impact:** Silent miscalculation of tax eligibility if a scheme is missing from `tax_scheme_rules`. Visible only via the UI's countdown showing wrong numbers or "eligible" too early.
- **Suggested fix:** Read `portfolio_service.get_tax_eligibility` to confirm. If it doesn't validate that every distinct `tax_scheme` in `tax_lots` has a corresponding rule, add a startup check or raise on missing.

### M4. Holding-period uses 365.25-day years; CLAUDE.md says "calendar years"
- **Location:** `backend/app/services/lot_engine.py:131-154`
- **Evidence:** `years_held = days_held / Decimal("365.25")`. For SSF (10 calendar years) the threshold is `10 * 365.25 = 3652.5` days. A purchase on `2015-01-01` becomes eligible on `2024-12-30` (3653 days) by this math — but a strict "calendar years" reading would say it's eligible only on `2025-01-01`. Conversely if a 10-year span happens to cross only 2 leap years (3652 days), the lot would still be ineligible on its 10th anniversary by 1 day.
- **Impact:** Eligibility can flip 1–2 days before/after the true calendar-year anniversary. Significant near year-end tax planning. Test `test_ssf_not_eligible_one_day_short` enshrines the current behavior, so this is intentional — but it doesn't match the regulatory wording.
- **Suggested fix:** Check current SEC/Revenue Department guidance. If it's "calendar year anniversary," use `today >= purchase_date.replace(year=purchase_date.year + n)` with a `Feb 29 → Feb 28/Mar 1` fallback. Either way, lock the choice into a comment with a citation.

---

## Low

### L1. `fifo_consume` may consume slightly more than requested when residual < EPSILON
- **Location:** `backend/app/services/lot_engine.py:74-83`
- **Evidence:**
  ```python
  consume = min(lot.units_remaining, remaining)
  if lot.units_remaining - consume <= EPSILON:
      cost = lot.cost_basis_remaining
      consume = lot.units_remaining   # ← overwrites consume!
  ```
  When the partial residual is below `1e-6` units, the engine treats it as a full-lot consumption and bumps `consume` up to `lot.units_remaining`. The follow-on `remaining -= consume` then drives `remaining` negative — masked by the post-loop check `if remaining > EPSILON`.
- **Impact:** A SELL of, say, 999.99999955 units from a 1000-unit lot will record `units_consumed = 1000` in `lot_consumptions`, even though the transaction's stored `units` is 999.99999955. Audit row and transaction disagree by up to 1e-6. Below display precision but technically incorrect.
- **Suggested fix:** Split the two cases:
  ```python
  if consume >= lot.units_remaining:  # exact full consumption
      consume = lot.units_remaining
      cost = lot.cost_basis_remaining
  elif lot.units_remaining - consume <= EPSILON:  # near-full
      # keep computed consume; quantize cost
      cost = _q(lot.cost_basis_remaining * (consume / lot.units_remaining))
  else:
      cost = _q(lot.cost_basis_remaining * (consume / lot.units_remaining))
  ```

### L2. `build_switch_in_lots` docstring claims exact sum; quantization can drift by 1e-8
- **Location:** `backend/app/services/lot_engine.py:99-128`
- **Evidence:** Docstring: "sum(new_lots.units) == switch_in_total_units exactly". Implementation quantizes each `new_units = _q(switch_in_total_units * fraction)` independently with ROUND_HALF_UP. For two consumptions of equal cost_basis split from a target of `1.00000001`, you get `0.50000001` and `0.50000001` summing to `1.00000002`. Tests don't cover this path with `switch_in_total_units` set.
- **Impact:** Total switch-in units can differ from the fund-house statement by ±k·1e-8 where k = number of lots. Invisible operationally but contradicts the docstring and audit invariant.
- **Suggested fix:** Allocate residual to the last lot:
  ```python
  allocated = Decimal(0)
  for i, c in enumerate(consumptions[:-1]):
      new_units = _q(switch_in_total_units * (c.cost_basis_consumed / total_cost))
      allocated += new_units
      # append
  # final lot gets the residual
  final_units = switch_in_total_units - allocated
  ```
  Add a test covering this path with `switch_in_total_units`.

### L3. `rebuild_lots` regenerates all UUIDs — external lot references break
- **Location:** `backend/app/services/transaction_service.py:87-117`
- **Evidence:** On every transaction delete, all `TaxLot` and `LotConsumption` rows are wiped and replayed with fresh UUIDs. The `source_lot_id` chain re-knits internally, but any external pointer to a lot UUID (URL bookmarks, debugger printouts, future report PDFs) breaks.
- **Impact:** Cosmetic / operational. Not a correctness bug. Flag only because if anything ever stores a lot UUID externally (e.g., in an email notification or a printed statement), it must be considered ephemeral.
- **Suggested fix:** None required for now. Document that lot IDs are not stable across edit history. If a stable handle is needed later, derive it from `(portfolio_id, original_purchase_date, tax_scheme, fund_code, source_lot_id)`.

---

## Not-Bugs but Worth Flagging (Tier 1)

- **Tests don't cover the `switch_in_total_units` path in `build_switch_in_lots`.** All existing tests pass `None` for that argument, exercising only the NAV-based fallback. The newer "fund-house-units" path used by `apply_switch` (line 158) is untested.
- **Tests don't cover concurrent execution** of `apply_sell` / `apply_switch`. Given H1, this matters.
- **`rebuild_lots` is O(N²)** in transaction count due to the inner pair lookup (`pair = [t for t in txs if t.pair_id == tx.pair_id]`). Replace with a `dict[pair_id, list[Transaction]]` pre-pass. Performance, not correctness.
- **`add_transaction` for `SWITCH_OUT`/`SWITCH_IN` types raises 400** (line 84-87). Good — but the message points users to `/switch`, which only the API knows about. Update API docs/UI.
- **No transaction-level idempotency key.** Re-submitting a BUY (e.g., on a flaky network) creates a duplicate. CSV import dedupes by `(date, type, fund_code, units, amount)`, but the form-based endpoint doesn't.
- **Bcrypt password length cap** (72 bytes) — not relevant to Tier 1 but spotted while skimming; will revisit in Tier 3.

---

# Tier 2 — Analytics Math

Files reviewed:
- `backend/app/services/portfolio_service.py`
- `backend/app/services/performance_service.py`

---

## Critical

### C2. TWR only considers currently-open lots and uses their *current* `units_remaining` across all historical sub-periods
- **Location:** `backend/app/services/portfolio_service.py:335-429` (`compute_twr`)
- **Evidence:** TWR pulls "lots where `units_remaining > 0`" (line 346-348) and then iterates sub-periods (line 403-420) computing `v_start += units * ns` and `v_end += units * ne`. The `units` variable is always `lot.units_remaining` — the *current* residual after all sells. Lots that were fully sold are excluded entirely. There is no reconstruction of historical position state.
- **Impact:** Three compounding bugs in one function:
  1. **Closed positions vanish from TWR**: A user who bought, held for two years, and sold at a 50% gain has that period excluded — the trading history that *is* the portfolio's TWR is missing.
  2. **Partially sold lots are mis-valued**: A lot that was 1000 units, with 800 sold last month, is valued at 200 units across *every* sub-period back to its purchase date — over-discounting the past.
  3. **Switched lots use the wrong fund's NAV history**: After A→B switch, the lot's `fund_code` is B. For sub-periods *before* the switch happened, `nav_on_or_before(lot.fund_code='B', date_before_switch)` returns fund B's historical NAV — but at that point in time the position was in fund A. The TWR-period valuation is from the wrong asset.
- The TWR number displayed on the Summary tab is therefore not the portfolio's true time-weighted return. It's "TWR of the current open holdings, projected backwards as if I'd always held them at today's units in today's funds." That's a meaningless quantity that happens to look plausible.
- **Suggested fix:** Reconstruct historical position state on each boundary by replaying transactions chronologically (similar to `rebuild_lots`) but keep only an in-memory map of `(fund_code, scheme) → units` and the per-lot units history. Or, much simpler: compute TWR purely from per-fund NAV returns weighted by historical units actually held. This requires position snapshots, not just current state. Given the complexity, consider gating the TWR display behind an "experimental" label until the rewrite lands. Also add tests with a known scenario (buy → partial sell → switch → today) and a hand-computed expected TWR.

---

## High

### H4. XIRR and realized P&L ignore `tax_withheld` on SELL transactions
- **Location:** `backend/app/services/portfolio_service.py:255-282` (`_realized_pnl`), `:472-486` (XIRR cash flow construction)
- **Evidence:**
  - `_realized_pnl`: `proceeds = amount - fee` (line 270). `tax_withheld` is never read.
  - XIRR: `cash_flows.append((tx.date, amount - fee))` for SELL (line 480). For DIVIDEND/INTEREST the code *does* subtract withholding (`amount - withheld`, line 483), so the inconsistency is the bug — SELL is treated as if no WHT can apply.
- **Impact:** For users subject to withholding tax on redemptions (e.g., early-redemption WHT on RMF/SSF that breach holding period, or non-resident WHT), reported realized P&L is overstated by the WHT amount, and XIRR is inflated. CSV import allows `tax_withheld` on SELL rows (`schemas/transaction.py:14-16` makes it Decimal default 0), so users can enter the data — the analytics just silently drop it.
- **Suggested fix:**
  ```python
  # _realized_pnl
  proceeds = Decimal(str(tx.amount)) - Decimal(str(tx.fee)) - Decimal(str(tx.tax_withheld))

  # compute_xirr — SELL branch
  elif tx.type == "SELL":
      cash_flows.append((tx.date, amount - fee - withheld))
  ```

### H5. `get_holdings` weighted average entry NAV uses lifetime BUY/SWITCH_IN totals — wrong after any partial sell
- **Location:** `backend/app/services/portfolio_service.py:144-162` (`entry_map`), `:202-212` (consumer)
- **Evidence:** `entry_map` aggregates `SUM(amount)` and `SUM(units)` over **all** BUY and SWITCH_IN transactions for `(fund_code, tax_scheme)` since portfolio inception. Then `avg_entry_nav = e_amount / e_units` is multiplied by *current* `units_remaining` to derive `entry_cost_in_fund`. If the user previously bought 1000 units at ฿10 and sold all 1000, then bought 500 at ฿20:
  - `e_amount = 10,000 + 10,000 = 20,000`; `e_units = 1,500`; `avg_entry_nav = 13.33`
  - `entry_cost_in_fund = 500 * 13.33 = 6,667` — but the user's *current* lot cost basis is 10,000.
  - The "fund_pnl_pct" displayed in the UI will therefore be wrong by a large factor.
- **Impact:** The "fund-entry basis" P&L preference (Settings page) — meant to display "how am I doing in this fund right now" — gives nonsense after any historical sell. Fund-entry NAV should be derived from the lots' actual cost-basis and units, not from cumulative transaction history.
- **Suggested fix:** Compute fund-entry stats from open lots only:
  ```python
  # Replace entry_map / avg_entry_nav with:
  # avg_entry_nav_in_fund = cost_basis_remaining / units_remaining  (i.e. avg cost basis per unit of open lots)
  ```
  This is just `cost / units` for the current open lots, which we already have. Drop the BUY/SWITCH_IN aggregation query entirely.

---

## Medium

### M5. `get_tax_eligibility` cache is keyed by `today`, but other portfolio caches aren't — stale `holding_days` and stale eligibility flow into Summary/Holdings after midnight
- **Location:** `backend/app/services/portfolio_service.py:31-49, 113-249`
- **Evidence:** Cache TTL is 5 minutes (`_CACHE_TTL = 300`). `holdings` and `summary` caches are keyed by `{portfolio_id}:holdings` and `{portfolio_id}:summary`. The "today" used in `holding_days = (today - oldest_date).days` (line 199) and in XIRR's terminal cash-flow date (line 492) is captured when the cache miss happens, then frozen until invalidation or TTL.
  - At 23:58 local: a cache miss populates `holding_days = N`.
  - At 00:03 next day: cache still hot, serves the *same* `N` instead of `N+1`. XIRR's terminal date is also still yesterday.
  - On a long-idle session, this can stay stale for hours if no mutation triggers `invalidate_portfolio`.
- **Impact:** Minor numerical staleness in displayed days and XIRR. Confusing around year boundaries / tax holding anniversaries.
- **Suggested fix:** Either (a) include `date.today().isoformat()` in cache keys, or (b) drop "today"-dependent fields from cache values and compute them at read time. Same for the analytics summary's `as_of_date`.

### M6. User DOB change does not invalidate `tax_eligibility` cache
- **Location:** `backend/app/api/users.py:21-34` (`update_me`), `backend/app/services/portfolio_service.py:559-706` (`get_tax_eligibility`)
- **Evidence:** `update_me` writes a new `date_of_birth` and returns — never calls `invalidate_portfolio` for the user's portfolios. The eligibility cache keyed `{portfolio_id}:tax:{today}` holds the previously-computed result (with the old DOB-driven age gate) until TTL expires or some other mutation invalidates the portfolio.
- **Impact:** A user who corrects their DOB (e.g., from blank to set, enabling RMF age check) sees stale eligibility for up to 5 minutes per portfolio. Worse: if no mutation occurs at all, they see stale data until the next portfolio mutation.
- **Suggested fix:** After saving DOB, look up the user's portfolios and call `invalidate_portfolio` for each. Or, factor the `:tax:` cache key to include `(user_dob, today)` so a DOB change naturally evicts.

### M7. `_nav_on_or_before` in performance_service only looks back 10 days
- **Location:** `backend/app/services/performance_service.py:108-114`
- **Evidence:**
  ```python
  for days_back in range(0, 10):
      d = anchor - timedelta(days=days_back)
      if d in nav_map:
          return nav_map[d]
  return None
  ```
  Compared to `portfolio_service`'s binary-search variant which has no day cap.
- **Impact:** If a fund had a long hiatus (>10 days without a NAV — Thai market closures, suspension, or simply a sync gap), the 7d/30d/6m/1y windows return `None`. The Performance tab shows blank cells in places where a more permissive scan would give a number.
- **Suggested fix:** Use the same binary-search lookup as `portfolio_service.compute_twr.nav_on_or_before` (line 385-398). Also: `_nav_on_or_before` in `performance_service` operates on a 400-row recent slice — a 1y lookup near the end of that slice can fall off. Either remove the `limit(400)` or do an explicit DB query for anchor NAVs.

### M8. Realized P&L logs but does not surface "missing lot_consumptions" — silently zero
- **Location:** `backend/app/services/portfolio_service.py:275-281`
- **Evidence:**
  ```python
  if raw_cost is None:
      logger.warning("No lot consumptions found for SELL tx %s ...", tx.id)
      cost = Decimal("0")  # treats the SELL as 100% profit
  ```
  This branch is for SELL transactions that have no `lot_consumptions` audit rows. That can happen if `apply_sell` was never run (e.g., a transaction inserted directly into the DB) or if `rebuild_lots` failed silently for that pair.
- **Impact:** Realized P&L = proceeds (treating cost as zero) for the affected SELL — a large false gain. The user sees inflated realized P&L. The log line is observability, not user-facing.
- **Suggested fix:** Either (a) fail loudly — return an error code on the summary so the UI can warn, or (b) at minimum, mark the row as "needs reconcile" and exclude it from the realized total rather than counting it as 100% profit.

---

## Low

### L4. Performance returns only query latest 400 NAV rows — 1y window can fall off when there are gaps
- **Location:** `backend/app/services/performance_service.py:30-35`
- **Evidence:** `limit(400)` on the NAV history pull. 400 trading days ≈ 18–20 calendar months for a fund with continuous trading; less if there are gaps. The 1y anchor `today - 365 days` is computed and looked up in the 400-row in-memory map.
- **Impact:** For older funds with sparse NAV history near 1y back, `returns_1y` returns None when it shouldn't.
- **Suggested fix:** Drop the limit or compute anchors via direct DB queries instead of an in-memory window. The 6m / 30d / 7d anchors don't have this issue in practice.

---

# Tier 3 — Auth & Authorization

Files reviewed:
- `backend/app/api/deps.py`
- `backend/app/api/auth.py`
- `backend/app/api/users.py`
- `backend/app/api/portfolios.py`
- `backend/app/services/auth_service.py`
- `backend/app/main.py:64-70` (CORS)

---

## Medium

### M9. No rate limiting on `/auth/token` — credential stuffing is wide open
- **Location:** `backend/app/api/auth.py:13-19`
- **Evidence:** The login route has no IP-based or account-based rate limiting and no failed-login counter. A scripted attacker can run unlimited password attempts. The compose file binds backend on `0.0.0.0:8000` by default.
- **Impact:** Brute-force / credential-stuffing against any known email. The mitigating factor is admin-managed accounts (no public signup) and bcrypt (slow hash) — so attacks are slow but not blocked. For a self-hosted deployment behind a home network this is low risk; if exposed publicly via reverse proxy, it's serious.
- **Suggested fix:** Add `slowapi` (or similar) with a per-IP limit of e.g. 5/minute on `/auth/token`. Document in README that exposing the backend port directly to the internet is unsupported. Consider a failed-login counter on the user row that locks for N minutes after K failures.

### M10. `/users/me` PATCH allows password change without current-password re-auth
- **Location:** `backend/app/api/users.py:21-34`
- **Evidence:** Body shape: `{date_of_birth?, password?}`. No `current_password` field. If a session token is leaked (XSS, stolen device, malware), the attacker can change the password and lock the legitimate user out — and they get a fresh 24h token in the process.
- **Impact:** Account lockout / takeover when a token is exposed but the password is not. For an admin-managed system this is moderately concerning because lockout is the most likely attacker objective.
- **Suggested fix:** Require `current_password` for password changes. Verify with `verify_password` before updating. Optional bonus: invalidate other sessions for the user, but with stateless JWTs that requires a `token_version` column.

### M11. CORS `allow_origins=["*"]` combined with `allow_credentials=True` is invalid and confusing
- **Location:** `backend/app/main.py:64-70`
- **Evidence:**
  ```python
  app.add_middleware(
      CORSMiddleware,
      allow_origins=["*"],
      allow_credentials=True,
      allow_methods=["*"],
      allow_headers=["*"],
  )
  ```
  Per the Fetch spec, browsers reject `Access-Control-Allow-Origin: *` when credentials are included. Starlette's CORS middleware echoes the request origin in this case to keep it working, which silently relaxes the "trust everyone" intent.
- **Impact:** In practice the JWT is sent via `Authorization: Bearer` (not cookies), so credentials in the CORS sense aren't actually used. The effective behavior is "any origin can read API responses with the user's token if it has it." Mostly harmless given the token-in-localStorage architecture, but the combination signals confusion and would bite if cookies are added later.
- **Suggested fix:** For local single-user deployment, set `allow_origins=["http://localhost:3000"]` (or the user's reverse-proxy URL via env var) and `allow_credentials=False`. Don't keep both lax options.

---

## Low

### L5. `decode_token` collapses "invalid" and "expired" into the same `None` — clients can't tell to refresh vs. re-login
- **Location:** `backend/app/services/auth_service.py:27-32`
- **Evidence:** Both `JWTError` subclasses (`ExpiredSignatureError`, `JWTClaimsError`, generic decode failure) are swallowed to `None`. The endpoint then returns HTTP 401 "Invalid token" for everything.
- **Impact:** Frontend has no signal to distinguish "token expired, prompt user to log in again" from "token tampered with, log out hard." Since there's no refresh-token flow anyway, this is mostly cosmetic — but it does mean a user whose token just expired sees "Invalid token" which is misleading.
- **Suggested fix:** Catch `ExpiredSignatureError` separately and propagate a distinct response code (e.g., `WWW-Authenticate: Bearer error="invalid_token", error_description="Expired"`), or at minimum log it for observability.

### L6. `get_current_user` crashes 500 on a non-UUID `sub` claim
- **Location:** `backend/app/api/deps.py:24`
- **Evidence:** `select(User).where(User.id == UUID(user_id))` — if a malformed token's `sub` is not a valid UUID, `UUID(user_id)` raises `ValueError`. The exception is not caught, so FastAPI returns HTTP 500.
- **Impact:** A handcrafted/forged token with a non-UUID `sub` yields a 500 instead of a clean 401. Information disclosure is minimal but it's wrong status code semantics.
- **Suggested fix:**
  ```python
  try:
      uid = UUID(user_id)
  except (ValueError, TypeError):
      raise HTTPException(status_code=401, detail="Invalid token")
  ```

### L7. bcrypt silently truncates passwords at 72 bytes
- **Location:** `backend/app/services/auth_service.py:10-15`
- **Evidence:** `bcrypt.hashpw(password.encode(), bcrypt.gensalt())` — bcrypt's input is hard-capped at 72 bytes. Two distinct long passwords sharing their first 72 bytes hash to the same value, and `bcrypt.checkpw` matches either against the stored hash.
- **Impact:** Practical risk is negligible — users with >72 byte passwords are rare, and this is a known bcrypt property. Worth flagging because the admin endpoint (`/users` POST) accepts arbitrary password strings without warning.
- **Suggested fix:** Either pre-hash with SHA-256 (then base64-encode → bcrypt) to lift the cap, or reject `> 72`-byte passwords at the schema level with a clear error. The pre-hash approach is more common.

---

## Not-Bugs but Worth Flagging (Tier 2 + 3)

- **Process-local in-memory cache (`portfolio_service._cache`)** doesn't survive worker restarts and doesn't sync across multiple workers. The deployment currently uses single-worker uvicorn (confirm in Docker `CMD`), but if anyone bumps `--workers 2` for capacity, half the cache invalidations will miss the other workers.
- **Admin role can read/write any user's portfolio data** through `_check_owner` and `_get_portfolio` (`portfolios.py:26-28`, `transactions.py:32-38`) — see Tier 1 M2. Flagged here again from the auth lens: there is no audit log of admin actions on user data.
- **Login times out at the password verify step but not at the user-not-found step.** `select(User).where(User.email == email)` returns quickly; `verify_password` runs bcrypt (slow). Difference is measurable. Standard mitigation is to run a dummy bcrypt verify on the not-found branch to equalize timing.
- **No CSRF** — not applicable with Bearer-in-Authorization-header architecture. Confirmed safe.
- **The `update_user` admin route can promote any user to admin or demote any admin to user.** No demotion guard, no "last admin" check. Possible to lock yourself out of admin entirely.
- **`transfer_holding`'s `source_lot_id` chain spans portfolios after the move** — new lots in target portfolio reference ancestor lots that may stay in source portfolio (if they're a different fund). Not a correctness bug, but the audit chain becomes cross-portfolio, which is surprising.

---

# Tier 4 — CSV Import

Files reviewed:
- `backend/app/services/csv_import.py`
- `backend/app/api/transactions.py:133-241` (`import_csv`)

The CSV parser is actually *stricter* than the `/switch` API endpoint (it does check same-date and 0.5% amount tolerance for switch pairs). Most CSV-specific findings are gaps relative to CLAUDE.md, not bugs in what it validates.

---

## Medium

### M12. CSV switch-pair validation misses cross-leg `target_fund_code` and `tax_scheme` checks
- **Location:** `backend/app/services/csv_import.py:182-200`
- **Evidence:** The pair-validation loop checks (a) both legs exist, (b) same date, (c) amount within 0.5%. It does NOT check:
  - `out_row.target_fund_code == in_row.fund_code`
  - `in_row.target_fund_code == out_row.fund_code`
  - `out_row.tax_scheme == in_row.tax_scheme`
  - That source and target funds differ (a self-switch makes no sense)
- **Impact:** Same class of bug as Tier 1 C1, on a different code path. A CSV can encode a switch where the source fund is fund A and the target fund is fund B, but `target_fund_code` says fund C — the importer accepts it and `apply_switch` consumes lots in A and creates lots in B (ignoring the C reference). Lots inherit `tax_scheme` from source regardless of what the `SWITCH_IN` row says.
- **Suggested fix:** Add these checks alongside the existing date / amount tolerance validation. Use the same logic as the fix proposed in Tier 1 C1 so both code paths converge.

### M13. CSV import accepts negative `units`, `amount`, `fee`, `tax_withheld`
- **Location:** `backend/app/services/csv_import.py:41-48` (`_d`), `:107-120` (row construction)
- **Evidence:** `_d` returns `Decimal(val)` for any parseable string — including `"-100"`. There is no sign validation. A row like `2024-01-01,BUY,SCBSET,-1000,12.0,-12000,0,0,,,NORMAL,` parses fine: `units = -1000`, `amount = -12000`, `units × nav = -12000` matches `amount`, so the "amount mismatch" guard passes.
- **Impact:** `apply_buy` creates a lot with `units_remaining = -1000` and `cost_basis_remaining = -12000`. FIFO then breaks (sort still works, but `units_remaining > 0` filter excludes the lot — so no future SELL can consume it, and analytics ignore it). The transaction is recorded with negative money flow — XIRR will see a positive cash inflow from the "BUY" (since the cash-flow expression `-(amount + fee)` becomes positive). Realized P&L math then drifts. Hard to spot in the UI because the negative-units lot doesn't show in the holdings table.
- **Suggested fix:** Add positive-number validation in `_parse_row`:
  ```python
  if tx_type in {"BUY", "SELL", "SWITCH_OUT", "SWITCH_IN"} and (units <= 0 or amount <= 0):
      return None, f"Row {row_num}: units and amount must be positive for {tx_type}"
  if fee < 0 or tax_withheld < 0:
      return None, f"Row {row_num}: fee and tax_withheld must be non-negative"
  ```
  Also fix Pydantic schema (Tier 1 M1 already flagged that the same gap exists in the API endpoint — both paths need the constraint).

---

## Low

### L8. CSV import is hard-coded to UTF-8 / UTF-8-BOM; Excel exports often arrive as Windows-1252 / TIS-620
- **Location:** `backend/app/api/transactions.py:144`
- **Evidence:** `text = io.StringIO(content.decode("utf-8-sig"))` — no fallback. A user who exports to CSV from Excel on Windows in Thai locale will get a TIS-620 or cp874-encoded file, which `decode("utf-8-sig")` rejects with `UnicodeDecodeError`. The exception isn't caught at the endpoint level — FastAPI returns HTTP 500.
- **Impact:** Common Thai-user workflow (Excel → CSV) breaks with a 500. The user has no clear path forward.
- **Suggested fix:** Try a sequence of encodings:
  ```python
  for enc in ("utf-8-sig", "utf-8", "cp874", "windows-1252"):
      try:
          text = io.StringIO(content.decode(enc))
          break
      except UnicodeDecodeError:
          continue
  else:
      raise HTTPException(400, "Unable to decode CSV — please save as UTF-8")
  ```

---

## Not-Bugs but Worth Flagging (Tier 4)

- **In-file duplicate detection adds an *error* for duplicates (line 153);** the API endpoint's against-DB dedup logs the same kind of conflict as a *skipped* message (line 175). Two different message formats for the same conceptual event — inconsistent UX.
- **The duplicate key `(date, type, fund_code, units, amount)` conflates two genuine identical BUYs on the same day** (e.g., DCA on the same day from two separate sources). The current behavior treats the second as a duplicate. CLAUDE.md is silent on this. Could be intentional — flag for product decision.
- **No multipart file size limit** on `/import-csv` — uploading a 1GB file reads it entirely into memory. Add a Starlette body-size limit middleware.

---

# Tier 5 — SEC Sync Resilience

Files reviewed:
- `backend/app/services/sec_api.py`
- `backend/app/services/sync_service.py`

---

## High

### H6. Rate-limit throttling is effectively a no-op — a new `_ThrottledClient` is constructed for *every* API call
- **Location:** `backend/app/services/sec_api.py:40-93` (`_ThrottledClient`), `:98-162` (all `list_amcs`, `get_daily_nav`, `get_dividends`, …)
- **Evidence:** The class keeps `self._last_call` and `self._lock` as instance state. But every public function in `sec_api.py` instantiates a fresh client:
  ```python
  async def list_amcs(key: str) -> list[dict]:
      client = _ThrottledClient(key)        # fresh _last_call = 0.0
      result = await client.get(...)
  ```
  Each call's throttler is brand-new, so `gap = _REQUEST_INTERVAL - (now - 0.0) ≈ -inf` is always negative, no sleep ever fires across calls. The 9 req/s cap is enforced only *within* a single client instance — and each instance issues exactly one request before being discarded.
- **Impact:** During `sync_fund_metadata` (which fans out across ~30 AMCs × N funds, each calling `list_amc_funds`, `get_fund_policy`, `get_fund_performance`) the backend issues requests as fast as `await client.get(url)` returns — easily 50–100 req/s on a fast network. The SEC API will respond with 421 quickly. The per-call retry-with-Retry-After kicks in (good fallback), but the whole sync becomes thrash: many 421s, exponential back-offs, slow completion, and a high probability of being soft-banned by SEC for the day.
- **Suggested fix:** Make the throttler a module-level singleton (or per-key dictionary), so all calls share state:
  ```python
  _clients: dict[str, _ThrottledClient] = {}
  def _client_for(key: str) -> _ThrottledClient:
      if key not in _clients:
          _clients[key] = _ThrottledClient(key)
      return _clients[key]
  ```
  Or refactor to a single shared client passed into every function. Add a test that batches 50 calls and asserts the elapsed time ≥ 50 × `_REQUEST_INTERVAL` (≈ 5.5s).

### H7. `sync_jobs` rows are left in `"running"` status forever if the worker process crashes mid-job
- **Location:** `backend/app/services/sync_service.py:92-108`
- **Evidence:** `_start_job` writes `status="running"` and flushes. `_finish_job` is only called on the success path of each sync function. If an unhandled exception occurs (e.g. OOM, restart, `SecApiUnauthorizedError` propagating out of `sync_fund_metadata`), the row stays as `running` permanently. There is no startup cleanup that marks old `running` jobs as `error`.
- **Impact:** The `/sync/jobs` page shows phantom "in-progress" jobs that block users (and admins) from understanding which sync ran. Worse: a subsequent sync starts a new job while the old one says "running" — looks like overlapping jobs, but really one is dead.
- **Suggested fix:**
  1. Wrap each `sync_*` body in `try/except/finally`; finish the job in `finally` with status from the exception type.
  2. On app startup (`main.py` lifespan), execute:
     ```sql
     UPDATE sync_jobs SET status='error', completed_at=NOW(),
         error_message='process terminated while job was running'
     WHERE status='running';
     ```

---

## Medium

### M14. Sync writes to `nav_history` / `dividends` never invalidate `portfolio_service` analytics cache
- **Location:** `backend/app/services/sync_service.py:267-368` (`sync_nav_for_date`), `:406-478` (`sync_dividends`), `backend/app/services/portfolio_service.py:31-49`
- **Evidence:** Both sync functions commit new NAV/dividend rows, but never call `invalidate_portfolio` for affected portfolios. The 5-minute TTL cache continues to serve stale `summary` / `holdings` / `tax_eligibility` with the *previous* NAV until expiry.
- **Impact:** Right after the nightly NAV sync at 19:30 ICT, users opening the dashboard see yesterday's market value for the first 5 minutes. Refreshes have no effect (cache is process-local). Manually triggering "Refresh analytics" works, but most users won't know to.
- **Suggested fix:** After a successful NAV write, identify affected portfolios:
  ```python
  affected = await db.execute(
      select(distinct(TaxLot.portfolio_id))
      .where(TaxLot.fund_code.in_(updated_codes))
  )
  for pid, in affected.all():
      invalidate_portfolio(pid)
  ```
  Or, simpler, call `clear_all_cache()` at the end of every sync that touched any fund.

### M15. CLAUDE.md promises auto-created `DIVIDEND` transactions on sync; the code only upserts to the `dividends` registry
- **Location:** `backend/app/services/sync_service.py:406-478` (`sync_dividends`), CLAUDE.md "Feature 8"
- **Evidence:** `sync_dividends` writes to the `dividends` table (per-fund registry), but never creates `Transaction` rows of type `DIVIDEND` in any portfolio. The Dashboard dividend summary reads from `Transaction.type == "DIVIDEND"` (see `analytics.py` endpoints + `portfolio_service.get_holdings:165-183`), so synced dividends never appear in user portfolios until the user manually enters them.
- **Impact:** The "Phase 2, item 8" feature promised in CLAUDE.md ("auto-create DIVIDEND transactions when new ones are found, dedupe by ex_date + fund_code") is missing. Users believe their dividend income is being tracked automatically; in fact only the per-fund registry is populated.
- **Suggested fix:** Two options:
  1. **Auto-create transactions** — after upserting a `Dividend` row, find all portfolios holding `fund_code` at `ex_date` and insert one `Transaction(type="DIVIDEND", amount=units_held * dividend_per_unit, ...)` per portfolio. Dedupe by `(portfolio_id, fund_code, ex_date)`.
  2. **Update the dashboard to read from `dividends`** joined to lots-held-at-ex-date, so the registry is enough. Lighter touch.
  Decide explicitly and remove the CLAUDE.md aspiration if you go with (2).

### M16. `_ThrottledClient.get` uses `asyncio.get_event_loop()`, which is deprecated and unreliable in modern Python
- **Location:** `backend/app/services/sec_api.py:55, 59`
- **Evidence:** `asyncio.get_event_loop().time()` — `get_event_loop()` is deprecated in 3.10+ outside coroutines and triggers a `DeprecationWarning` (with auto-create off, raises `RuntimeError` in 3.12 when called from no-loop contexts).
- **Impact:** Works today because we're always inside a running loop, but Python's async ergonomics are moving away from `get_event_loop()`. Will become a hard error eventually.
- **Suggested fix:** Use `time.monotonic()` — simpler, no loop coupling, same semantics for measuring elapsed time.

---

## Not-Bugs but Worth Flagging (Tier 5)

- **`sync_dividends` re-fetches the full dividend history for every fund on every run.** For an active fund with 20 years of history, that's hundreds of rows fetched and compared per sync. If SEC's API supports `since=` filtering, use it. Otherwise, track `last_dividend_sync_at` per fund and skip recent dividends already in the DB.
- **`sync_nav_range` runs sequentially, one date at a time.** A 1-year backfill (~250 weekdays) × N funds × 0.11s/request = several minutes. Could parallelize across dates with a bounded concurrency limit (e.g. 5 inflight).
- **`SyncJob.error_message` truncates by joining only the first 5 errors** (`sync_service.py:246, 363, 474`). The rest are lost. Either persist all errors as JSON or write to a separate `sync_errors` table.

---

# Tier 6 — Concurrency & DB Transactions

Largely covered by Tier 1 H1 (no `FOR UPDATE` on lot read). Additional spot-checks below.

## Not-Bugs but Worth Flagging (Tier 6)

- **APScheduler jobs run inside the FastAPI process and share the same asyncio loop.** Two scheduled jobs can't truly run concurrently — they cooperate via `await`. So NAV-sync and dividend-sync can interleave but won't have OS-level race conditions on lot tables. (Cron job times also differ: 12:30 vs 13:30 UTC.)
- **`rebuild_lots` is invoked from `delete_transaction` (line 273)** inside the same DB transaction as the delete. So delete + rebuild is atomic per portfolio. If two users (or one user + a sync) modify the same portfolio simultaneously, postgres' READ COMMITTED can still produce inconsistent intermediate reads on the lot table — but the *commit* is all-or-nothing.
- **Database transaction boundaries in `apply_sell`/`apply_switch` rely on the caller** (`add_transaction`, `add_switch`, `import_csv`) to call `db.commit()` / use `begin_nested`. The CSV path uses `begin_nested` per row — good. The single-transaction path doesn't wrap in a savepoint; if the route handler crashed between `apply_sell` and the commit, the framework would roll back, but a hand-rolled `apply_sell` call from a future code path could leave partial state.
- **Connection pool sizing** isn't pinned anywhere I read. If `asyncpg` defaults to a small pool, concurrent users will queue. Add `pool_size`/`max_overflow` to `database.py` as needed.

---

# Tier 7 — Frontend

Files reviewed:
- `frontend/src/lib/api.ts`
- `frontend/src/app/dashboard/page.tsx`
- `frontend/src/app/dashboard/portfolios/[id]/page.tsx`

## Medium

### M17. Inconsistent 401 handling — only one API call triggers logout; all others silently swallow auth errors
- **Location:** `frontend/src/app/dashboard/page.tsx:106` (`listPortfolios.catch → clearToken + replace("/login")`), vs every other `api.*().catch(() => {})` in the file
- **Evidence:** The `request()` helper in `api.ts:32-35` throws a generic `Error` on `!res.ok` — it never distinguishes 401 from other errors. Each call site is responsible for handling it. In the dashboard, only `listPortfolios` triggers logout-on-failure; `getMe`, `getPortfolioSummary`, `getPortfolioHoldings`, `getDividendYears`, `getDividendSummary` all `.catch(() => {})` or silently log.
- **Impact:** If the token expires mid-session (default 24h):
  - The dashboard loads cached state from the last successful render.
  - Each subsequent API call throws → the `.catch(() => {})` swallows it.
  - The user sees empty/loading-forever cards and no error message. They don't know to log in again.
- **Suggested fix:** Centralize 401 handling in `request()`:
  ```typescript
  if (res.status === 401) {
    clearToken();
    if (typeof window !== "undefined") window.location.href = "/login";
    throw new Error("Session expired");
  }
  if (!res.ok) { ... }
  ```
  Then page-level `.catch(() => {})` calls won't be reached for auth failures — the redirect happens centrally.

### M18. No UI path to enter a fund switch — `TX_TYPES` excludes SWITCH_OUT/SWITCH_IN, and `api.ts` has no `addSwitch`
- **Location:** `frontend/src/app/dashboard/portfolios/[id]/page.tsx:51` (`TX_TYPES = ["BUY", "SELL", "DIVIDEND", "INTEREST"]`), `frontend/src/lib/api.ts` (no `addSwitch` method)
- **Evidence:** The transaction-add form's type dropdown deliberately omits SWITCH options. The frontend types `TransactionCreate` includes `target_fund_code` and `pair_id`, but the only way to actually submit a switch is via CSV import. The backend has a working `POST /portfolios/{id}/transactions/switch` endpoint, but the frontend doesn't call it.
- **Impact:** A user who switches between funds in real life has no way to record it in the app's UI. They must construct a CSV with `SWITCH_OUT` + `SWITCH_IN` rows and re-upload, just to log a switch. Friction high enough that some users will fudge it as "SELL old + BUY new" (which breaks tax-scheme/holding-period continuity).
- **Suggested fix:** Add an `addSwitch(portfolioId, switchOut, switchIn)` method to `api.ts` and a "Record switch" button on the transactions tab that opens a dialog with two fund pickers, units/nav for each leg, date, fee. Server-side validation (proposed in Tier 1 C1) will surface mismatches inline.

## Low

### L9. `holding_days` and other date-derived values are stringified on the server using server-local timezone, but the dashboard renders them in `th-TH` locale — there's a TZ mismatch risk if the container's clock isn't UTC
- **Location:** `backend/app/services/portfolio_service.py:185, 199` (uses `date.today()`), frontend `*.toLocaleDateString("th-TH")`
- **Evidence:** `date.today()` returns the server-local date. If the container runs in UTC (per `docker-compose.yml` defaults) and a Bangkok user opens the app at 06:00 ICT (23:00 UTC the previous day), the server thinks "today" is one day earlier. Holding-days countdowns and "is_eligible" comparisons will be 24 hours off near midnight ICT.
- **Impact:** Edge case but real for users near the tax-eligibility threshold. RMF/SSF/ESG holding-period anniversaries can flip a day late.
- **Suggested fix:** Compute "today" in ICT explicitly on the server:
  ```python
  from zoneinfo import ZoneInfo
  today_ict = datetime.now(ZoneInfo("Asia/Bangkok")).date()
  ```
  Replace `date.today()` calls in `portfolio_service.py` and `transaction_service`'s SWITCH consumers. Document the convention.

### L10. Decimal precision is lost on JS `Number()` coercion in aggregations on the dashboard
- **Location:** `frontend/src/app/dashboard/page.tsx:120-127, 135, 189-190`
- **Evidence:** Server returns Decimal-as-string (e.g., `"12345.67890123"`). Frontend coerces with `Number(x.total_cost_basis)` and reduces with `+`. JS numbers are IEEE-754 doubles — accurate to ~15 significant digits. For sums across hundreds of holdings totaling tens of millions of baht, drift is in the satang.
- **Impact:** Display only — the rounded values shown via `.toFixed(2)` look fine to the user. But if anyone wires these aggregates back to the server (e.g. as a "total invested" the user types into a tax form), they'd round-trip wrong.
- **Suggested fix:** For pure display, the current approach is acceptable. Document that the dashboard's KPI numbers are display-precision only and not authoritative. If precision matters, have the server compute the aggregates and return them.

---

## Not-Bugs but Worth Flagging (Tier 7)

- **JWT in `localStorage` is XSS-readable.** Standard tradeoff for this architecture. If a third-party script ever runs on the page (e.g., a chart library with a CDN bug), the token is exfiltrated. Mitigated by the strict CSP one *could* add and by serving the frontend from a known origin.
- **`Number()` is also used to compute sort keys** (`page.tsx:166`). The drift is below precision needed for sorting, so this is fine — but be aware that two holdings within a few satang of each other could swap order between page loads.
- **The transaction form `disabled={submitting}`** on the submit button (line 1513) does prevent double-click submission. ✓ However, the dashboard's "Create Portfolio" button doesn't visibly debounce — quick double-tap can create two portfolios with the same name. Low-impact.
- **No global error boundary** — an unexpected crash in any component blanks the whole tab. Add a Next.js `error.tsx` at the dashboard level.

---

# Cross-Cutting Themes

A few patterns showed up in multiple tiers; worth flagging as systemic issues:

1. **Domain validation gaps** appear in Tier 1 (`/switch` endpoint), Tier 4 (CSV import), and Tier 7 (no UI). The same set of CLAUDE.md switch invariants (same AMC, same date, same scheme, amount tolerance, target/source cross-check) is missing on every entry point. A single shared validator would fix three findings at once.
2. **Cache invalidation gaps** appear in Tier 2 (DOB change, "today" rollover) and Tier 5 (NAV sync, dividend sync). The 5-minute in-memory cache is invalidated only on user-initiated transaction mutations; every other mutator forgets to invalidate. Consider replacing the in-memory cache with HTTP `Cache-Control` headers + a database-driven `analytics_invalidated_at` timestamp.
3. **`tax_withheld` is partially wired** — schemas accept it, CSV imports parse it, but XIRR and realized P&L drop it for SELL while honoring it for DIVIDEND. Consistency cleanup.
4. **Admin role is treated as "godmode" for ownership checks** in `_check_owner` / `_get_portfolio`. Reasonable for some use cases (support read), but the same code path allows admin to *mutate* any user's data with no audit log.
5. **Missing tests for the new code paths** — `switch_in_total_units` allocation, TWR, AI summary, sync_service rate-limit semantics, concurrent SELL — make regressions invisible.

# Tier 4 — CSV Import

_Pending._

# Tier 5 — SEC Sync

_Pending._

# Tier 6 — Concurrency

_Partially covered by Tier 1 / H1. Full pass pending._

# Tier 7 — Frontend

_Pending._
