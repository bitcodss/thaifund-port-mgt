export type ReturnBasis = "cost" | "fund";
export type ReturnType = "pnl" | "total";

export interface UserSettings {
  returnBasis: ReturnBasis;
  returnType: ReturnType;
}

// "cost+pnl" | "fund+pnl" | "cost+total" | "fund+total"
export type PnlBasis = "cost" | "fund" | "total_cost" | "total";

const DEFAULTS: UserSettings = { returnBasis: "fund", returnType: "pnl" };
const STORAGE_KEY = "tft_settings";

export function loadSettings(): UserSettings {
  if (typeof window === "undefined") return DEFAULTS;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch {
    return DEFAULTS;
  }
}

export function saveSettings(s: UserSettings): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY, newValue: JSON.stringify(s) }));
}

export function derivePnlBasis(s: UserSettings): PnlBasis {
  if (s.returnBasis === "cost" && s.returnType === "pnl") return "cost";
  if (s.returnBasis === "fund" && s.returnType === "pnl") return "fund";
  if (s.returnBasis === "cost" && s.returnType === "total") return "total_cost";
  return "total";
}

export function pnlFieldKey(basis: PnlBasis): string {
  switch (basis) {
    case "cost":       return "unrealized_pnl_pct";
    case "fund":       return "fund_pnl_pct";
    case "total_cost": return "total_return_pct";
    case "total":      return "total_return_fund_pct";
  }
}

export function pnlBasisLabel(s: UserSettings): string {
  const base = s.returnBasis === "fund" ? "Fund entry" : "Original cost";
  const type = s.returnType === "total" ? " + dividends" : "";
  return base + type;
}
