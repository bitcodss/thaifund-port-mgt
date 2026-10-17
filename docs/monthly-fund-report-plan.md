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
- Market-wide holdings (names + weights from SEC factsheets) are ONLY an input to the LLM
  peer-theme classifier — no ticker resolution outside held funds. Parser tuning stops once
  holdings are missing for less than ~10–15% of funds in the AIMC categories of held funds'
  peer groups. Funds without holdings are classified from fund name, master fund, benchmark
  and investment policy instead.
- Every factsheet row records `holdings_parser_version` / `aimc_parser_version` and parse
  times (`legacy` = before versions were recorded), so suspicious data can be traced to a
  parser version and re-parsed selectively.
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
- Uncategorised foreign-equity funds (~35–53, mostly ONE / LH factsheets that print no
  category): the LLM infers an AIMC category from the 44 report categories only (never
  "Miscellaneous"), using master fund + investment policy (SEC profile) and benchmark (SEC
  factsheet data); stored as `inferred`, never mixed with parsed categories.
- Dedup: one peer per master fund — master ISIN first (70% of peers), normalised master name
  only as fallback (21%), one per project for direct-stock funds (9%).
- Representative NAV: one main retail share class per project (no institutional or
  SSF/RMF/ThaiESG duplicates — fees differ). A deduped master's return = MEDIAN of its
  feeders' returns in the SAME hedge group as the fund being compared; if no feeder shares
  the hedge group, use all and label it "mixed hedge".
- Chunk 4 also reports "vs other feeders of the same master" as its own metric — the direct
  fee / tracking comparison, and the basis for switching to a cheaper fund holding the same thing.
- Flags: 1M return < −2σ (own history), 3M < −2σ of 3M, peer percentile < 25 two months
  running, P&L since entry ≤ −10%.
- The bottom-quartile rule fires ONLY on a theme-basis comparison. A fund compared with its
  whole category (its theme group has < 5 masters — e.g. KKP GVALUE, a value fund among 133
  mostly growth / broad Global Equity masters) would sit in the bottom quartile every month a
  style is out of favour. For these, rank is shown as context only, the report states plainly
  that the comparison is the whole category because the style group is too small, and only
  the two own-history rules and the −10% rule can flag. Monthly snapshots record the basis.
- Bottom-quartile flag uses the 3M return. 3M windows of consecutive months overlap, so one bad
  month can keep a fund at the bottom for up to three reports: the facts name the month that drove
  each 3M window and mark `single_event` when both flagged windows share it, so the narrative says
  "bottom because of the July drop", not "persistently weak".
- Groups under 8 masters (incl. the fund) show a rank ("4 of 5") instead of a percentile, and
  the bottom flag there means last place only.
- FX per currency: the master's share-class currency from the SEC investment policy / master
  name, else the category's region (Japan → JPY, Europe → EUR, A-shares → CNY, Greater China →
  HKD), else USD. FX-adjusted peers use the peer's currency. CNY/THB via the USD cross rate.
- Watchlist ("ควรจับตา"): ONE entry per holding listing every reason that applies.
- −10% rule: P&L on the open lots' cost basis, the same definition as the Holdings page
  (a switched-in lot keeps its original cost).
- Price proxies removed (chunk 5); the LLM is now called for proxies only as the news fallback.
- Theme groups: masters the two models disagree on are left out (counted at category level);
  passive plain broad-index funds get the broad theme by rule — never an index that carves
  out a country (ex-US, ex-China…), except "ex Japan" in Asia Pacific Ex Japan.

## Phase 3 — report builder (built: report_builder.py, Reports page)
- LLM sections are checked: numbers from the facts only, no advice, no "sell" for locked RMF/SSF,
  no JSON keys, no invented periods, fund codes copied exactly; 2 retries, then a code template.
- Signals reach the LLM as Thai sentences written by code (driver month included); the LLM
  rephrases, never derives. The RMF purchase warning is code-rendered only.
- Many funds share news tickers (TSM, BABA, TCEHY appear in ~6 funds). Mention shared
  drivers ONCE at portfolio level and refer to them from fund cards — never repeat the
  same story on every card.
- Rank a fund's news by HOLDING WEIGHT (`fund_proxies.weight_pct`), not by headline count:
  big names (TSM) out-publish smaller holdings (MercadoLibre, First Quantum) many times over,
  so a count-based pick would make every EM card a TSM story. Proxies without a weight
  (LLM fallback) rank last.
- Tax constraints: RMF/SSF/ThaiESG holding rules per lot; cross-AMC moves are transfers
  (allowed, slower), not instant switches.
- Narrative rule: while a fund's RMF / SSF lots are not yet eligible, the report NEVER suggests
  selling it or moving to a fund outside the scheme — only hold, switch within the AMC, or
  transfer to another AMC's fund of the same scheme (facts: `narrative_constraints`).
- Tax notes in the facts carry their source (Revenue Dept No. 369 for SSF; ruling กค 0702/2158,
  2023, for RMF incl. the "never pause buying > 1 consecutive year" rule). Both schemes repay
  tax for deducted years within 5 years of filing (+ s.27 surcharge), gain taxable.
- Look-through exposure (top-5 only) and allocation drift vs last month.

## Phase 4 — delivery (not built)
- Gmail SMTP app password; one email per user, section per portfolio.
- Send when all held funds have month-end NAV, cap at 5th business day (list missing funds).
