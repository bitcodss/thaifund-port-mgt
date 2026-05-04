const BASE = "";

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("token");
}

export function saveToken(token: string) {
  localStorage.setItem("token", token);
}

export function clearToken() {
  localStorage.removeItem("token");
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  auth = true,
): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (auth) {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(`${BASE}/api/v1${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Request failed");
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  // Auth
  login: (email: string, password: string) =>
    request<{ access_token: string; token_type: string }>(
      "POST", "/auth/token", { email, password }, false,
    ),
  getMe: () => request<CurrentUser>("GET", "/users/me"),
  updateMe: (date_of_birth: string | null) =>
    request<CurrentUser>("PATCH", "/users/me", { date_of_birth }),

  // Sync (admin only)
  syncFunds: () => request<{ status: string; message: string }>("POST", "/sync/funds"),
  syncNav: (navDate?: string) =>
    request<{ status: string; date: string; message: string }>(
      "POST", `/sync/nav${navDate ? `?nav_date=${navDate}` : ""}`,
    ),
  syncNavBackfill: (startDate: string, endDate?: string, portfolioOnly?: boolean) => {
    const params = new URLSearchParams({ start_date: startDate });
    if (endDate) params.set("end_date", endDate);
    if (portfolioOnly) params.set("portfolio_only", "true");
    return request<{ status: string; message: string }>("POST", `/sync/nav/backfill?${params}`);
  },
  syncDividends: () => request<{ status: string; message: string }>("POST", "/sync/dividends"),
  syncFinnomenaNav: () => request<{ status: string; message: string }>("POST", "/sync/finnomena-nav"),
  listSyncJobs: () => request<SyncJob[]>("GET", "/sync/jobs"),
  searchFunds: (q: string) => request<FundResult[]>("GET", `/funds/search?q=${encodeURIComponent(q)}`),

  // Portfolios
  listPortfolios: () => request<Portfolio[]>("GET", "/portfolios"),
  createPortfolio: (name: string) => request<Portfolio>("POST", "/portfolios", { name }),
  renamePortfolio: (id: string, name: string) =>
    request<Portfolio>("PATCH", `/portfolios/${id}`, { name }),
  deletePortfolio: (id: string) => request<void>("DELETE", `/portfolios/${id}`),
  transferHolding: (sourceId: string, fundCode: string, taxScheme: string, targetPortfolioId: string) =>
    request<{ moved_lots: number; fund_code: string }>("POST", `/portfolios/${sourceId}/transfer-holding`, {
      fund_code: fundCode,
      tax_scheme: taxScheme,
      target_portfolio_id: targetPortfolioId,
    }),
  refreshAnalytics: (id: string) => request<void>("POST", `/portfolios/${id}/analytics/refresh`),

  // Transactions
  listTransactions: (portfolioId: string) =>
    request<Transaction[]>("GET", `/portfolios/${portfolioId}/transactions`),
  addTransaction: (portfolioId: string, tx: TransactionCreate) =>
    request<Transaction>("POST", `/portfolios/${portfolioId}/transactions`, tx),
  deleteTransaction: (portfolioId: string, txId: string) =>
    request<void>("DELETE", `/portfolios/${portfolioId}/transactions/${txId}`),

  // Lots
  listLots: (portfolioId: string) =>
    request<TaxLot[]>("GET", `/portfolios/${portfolioId}/lots`),

  // Analytics
  getPortfolioSummary: (portfolioId: string) =>
    request<PortfolioSummary>("GET", `/portfolios/${portfolioId}/analytics/summary`),
  getPortfolioHoldings: (portfolioId: string) =>
    request<HoldingRow[]>("GET", `/portfolios/${portfolioId}/analytics/holdings`),
  getPortfolioAllocation: (portfolioId: string) =>
    request<AllocationResult>("GET", `/portfolios/${portfolioId}/analytics/allocation`),
  getTaxEligibility: (portfolioId: string) =>
    request<LotEligibility[]>("GET", `/portfolios/${portfolioId}/analytics/tax-eligibility`),
  getFundPerformance: (fundCode: string, sinceDate?: string) =>
    request<FundPerformance>("GET", `/funds/${fundCode}/performance${sinceDate ? `?since_date=${sinceDate}` : ""}`),
  getFundRiskMetrics: (fundCode: string) =>
    request<FundRiskMetrics>("GET", `/funds/${fundCode}/risk-metrics`),
  getFundNavHistory: (fundCode: string, days = 365) =>
    request<NavPoint[]>("GET", `/funds/${fundCode}/nav-history?days=${days}`),
  getAiSummary: (portfolioId: string) =>
    request<AiSummary>("GET", `/portfolios/${portfolioId}/analytics/ai-summary`),
  refreshAiSummary: (portfolioId: string) =>
    request<AiSummary>("POST", `/portfolios/${portfolioId}/analytics/ai-summary/refresh`),

  // CSV import
  importCsv: async (portfolioId: string, file: File) => {
    const token = getToken();
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${BASE}/api/v1/portfolios/${portfolioId}/transactions/import-csv`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail ?? "Import failed");
    }
    return res.json() as Promise<{ imported: number; errors: string[] }>;
  },
};

// ── Types ─────────────────────────────────────────────────────────────────────

export interface CurrentUser {
  id: string;
  email: string;
  role: string;
  date_of_birth: string | null;
  is_active: boolean;
  created_at: string;
}

export interface SyncJob {
  id: string;
  type: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  error_message: string | null;
}

export interface FundResult {
  fund_code: string;
  name_en: string | null;
  name_th: string | null;
  amc: string | null;
  fund_status: string | null;
  asset_class: string | null;
  risk_level: number | null;
}

export interface Portfolio {
  id: string;
  user_id: string;
  name: string;
  created_at: string;
}

export interface Transaction {
  id: string;
  portfolio_id: string;
  date: string;
  type: string;
  fund_code: string | null;
  units: string | null;
  nav: string | null;
  amount: string;
  fee: string;
  tax_withheld: string;
  target_fund_code: string | null;
  pair_id: string | null;
  tax_scheme: string;
  note: string | null;
  created_at: string;
}

export interface TransactionCreate {
  date: string;
  type: string;
  fund_code?: string;
  units?: string;
  nav?: string;
  amount: string;
  fee?: string;
  tax_withheld?: string;
  target_fund_code?: string;
  pair_id?: string;
  tax_scheme: string;
  note?: string;
}

export interface TaxLot {
  id: string;
  portfolio_id: string;
  fund_code: string;
  original_purchase_date: string;
  units_remaining: string;
  cost_basis_remaining: string;
  tax_scheme: string;
  source_lot_id: string | null;
  created_at: string;
}

// ── Analytics types ────────────────────────────────────────────────────────────

export interface PortfolioSummary {
  portfolio_id: string;
  as_of_date: string;
  total_cost_basis: string;
  total_market_value: string | null;
  unrealized_pnl: string | null;
  unrealized_pnl_pct: string | null;
  realized_pnl: string;
  total_invested: string;
  xirr: string | null;
  xirr_error: string | null;
  open_positions: number;
}

export interface HoldingRow {
  fund_code: string;
  fund_name_en: string | null;
  amc: string | null;
  asset_class: string | null;
  tax_scheme: string;
  units: string;
  cost_basis: string;
  latest_nav: string | null;
  latest_nav_date: string | null;
  market_value: string | null;
  unrealized_pnl: string | null;
  unrealized_pnl_pct: string | null;
  oldest_purchase_date: string | null;
  holding_days: number | null;
  entry_cost_in_fund: string | null;
  fund_pnl_pct: string | null;
  fund_entry_date: string | null;
  dividends_gross: string;
  dividends_net: string;
  total_return_pct: string | null;
  total_return_fund_pct: string | null;
}

export interface AllocationItem {
  label: string;
  value: string;
  pct: string;
}

export interface AllocationResult {
  by_asset_class: AllocationItem[];
  by_amc: AllocationItem[];
  by_tax_scheme: AllocationItem[];
  by_risk_level: AllocationItem[];
}

export interface FundPerformance {
  fund_code: string;
  latest_nav: string | null;
  latest_nav_date: string | null;
  returns_7d: string | null;
  returns_30d: string | null;
  returns_6m: string | null;
  returns_1y: string | null;
  returns_ytd: string | null;
  returns_max: string | null;
}

export interface FundRiskMetrics {
  fund_code: string;
  data_weeks: number;
  annualized_volatility: string | null;
  sharpe_ratio: string | null;
  max_drawdown: string | null;
  risk_free_rate_used: string;
}

export interface NavPoint {
  date: string;
  nav: number;
}

export interface AiSummary {
  portfolio_id: string;
  content: string;
  generated_at: string;
}

export interface LotEligibility {
  lot_id: string;
  source_lot_id: string | null;
  source_fund_code: string | null;
  switch_chain: string[];
  fund_code: string;
  tax_scheme: string;
  original_purchase_date: string;
  units_remaining: string;
  cost_basis_remaining: string;
  market_value: string | null;
  unrealized_pnl: string | null;
  is_eligible: boolean;
  eligible_date: string | null;
  days_remaining: number;
  holding_years_required: string;
  age_requirement: number | null;
}
