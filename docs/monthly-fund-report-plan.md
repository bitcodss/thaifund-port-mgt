# Monthly fund report — plan and decisions

Monthly per-user email: per-fund LLM narrative (sentiment, news, what to watch),
numbers computed by code, poorly performing funds first. Local Ollama only.

## Principles
- Code computes every number; the LLM only writes narrative from a fixed facts set.
- Every number in LLM output is verified against the facts (`number_guard`: Arabic and
  Thai digits; spelled-out numbers rejected); retry, then fall back to a template.
- Signal language only ("what to watch"), never buy/sell advice.
- Thai output, technical terms in English.

## Phase 1 — news (built on feat/fund-news-digest)
- News proxies come from REAL holdings, LLM only as a flagged fallback:
  1. direct-stock fund → its SEC top-5 holdings (`source=holding`)
  2. feeder → master fund's top 5 from the Thai SEC factsheet PDF (`source=factsheet`)
  3. otherwise LLM suggestion, `source=ai`, exposed as `verified=false` ("Unverified" badge);
     never mixed with holding tickers (needs ≥2 verified holding tickers or the fallback is used)
  4. admin override (`source=manual`) always wins, never replaced by sync
- Holding name → ticker: simplified-name Yahoo search, must share a distinctive word with the
  holding name; listing order US exchange → US OTC ADR → home primary → regional cross-listing;
  must have ≥3 Yahoo headlines in the last 14 days.
- Collector every 2h (Yahoo RSS keeps ~20 items); raw items pruned after 90 days.
- Weekly digest per ticker (Mon 06:00 ICT), kept permanently with its sources.
- Funds without parseable holdings get a one-off admin override researched from real holdings
  (master factsheet / ETF holdings), stored with `source_note` + `source_as_of`; the UI warns when
  an override's source data is > 3 months old (sync never replaces overrides). No per-manager
  parsers — not worth it for a handful of funds.

## Phase 2 — peer comparison (design, not built)
- Drop Yahoo ETF price proxies; trend / drawdown / volatility from the fund's own NAV.
  Yahoo only for FX (USD/THB and other exposures).
- Peer groups: AIMC category (coarse, parsed from SEC factsheet "กลุ่ม …"; validated against
  the official list in the monthly AIMC Category Performance Report) → fine theme chosen by
  LLM from a fixed list per AIMC category, using name, master fund, benchmark, holdings and
  policy text. Theme group if ≥5 funds after dedup, else AIMC category. Dedup one per
  project / master fund. Median + percentile; rank ("2 of 4") under ~5 funds.
- Passive and ESG are separate flags (`is_passive`, `is_esg`), not themes.
- Theme lists (approved): Technology Equity: AI · Semiconductor · Cybersecurity · Robotics &
  Automation · Internet & Digital · General Technology | Other Global Sector Equity: Gold &
  Precious Metals Miners · Financials · Consumer · Natural Resources · Multi-Sector / Quant ·
  Other | Health Care: Life Sciences & Biotech · Pharma & Large-Cap · Medical Technology ·
  General Health Care | Global Equity: Growth · Value · Quality / Dividend · Small Cap ·
  General | US Equity: Broad / S&P 500 · Mega-Cap Concentrated · Growth · Value / Dividend ·
  Small–Mid Cap | Greater China Equity: China Tech & Innovation · All-China Broad · Hong Kong /
  H-Shares · China Consumer | Japan Equity: Broad / Index · Growth · Value / Dividend · Small
  Cap | Asia Pacific Ex Japan: Broad · Growth · Dividend / Income | Emerging Market: Broad EM ·
  EM Asia · EM ex-China | all other categories: a single theme = the category.
- Peer NAV for the WHOLE market (no per-category conditional backfill): ~2,391 projects,
  1Y backfill ≈ 11–12k SEC calls + bulk insert, then monthly ≈ 2.4k calls.
- FX: hedge policy from SEC profile `exchange_rate_protection_policy` (store for all funds);
  FX effect uses the ACTUAL hedge ratio from the factsheet ("FX Hedging xx%") when present,
  otherwise it is shown as an estimate — never silently assume 0% or 100%.
- Flags: 1M return < −2σ (own history), 3M < −2σ of 3M, peer percentile < 25 two months
  running, P&L since entry ≤ −10%.

## Phase 3 — report builder (not built)
- Many funds share news tickers (TSM, BABA, TCEHY appear in ~6 funds). Mention shared
  drivers ONCE at portfolio level and refer to them from fund cards — never repeat the
  same story on every card.
- Rank a fund's news by HOLDING WEIGHT (`fund_proxies.weight_pct`), not by headline count:
  big names (TSM) out-publish smaller holdings (MercadoLibre, First Quantum) many times over,
  so a count-based pick would make every EM card a TSM story. Proxies without a weight
  (LLM fallback) rank last.
- Tax constraints: RMF/SSF/ThaiESG holding rules per lot; cross-AMC moves are transfers
  (allowed, slower), not instant switches.
- Look-through exposure (top-5 only) and allocation drift vs last month.

## Phase 4 — delivery (not built)
- Gmail SMTP app password; one email per user, section per portfolio.
- Send when all held funds have month-end NAV, cap at 5th business day (list missing funds).
