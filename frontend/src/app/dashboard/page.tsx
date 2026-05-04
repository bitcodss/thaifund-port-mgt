"use client";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  api, clearToken,
  type Portfolio, type CurrentUser, type PortfolioSummary, type HoldingRow,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  PlusCircle, LogOut, FolderOpen, Pencil, Trash2, Check, X,
  RefreshCw, TrendingUp, TrendingDown, AlertTriangle, Settings,
} from "lucide-react";
import { loadSettings, derivePnlBasis, pnlFieldKey } from "@/lib/settings";

// ── Format helpers ─────────────────────────────────────────────────────────────

function fmtBaht(v: number, decimals = 2) {
  return `฿${v.toLocaleString("th-TH", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}`;
}
function fmtPctSigned(v: number) {
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}
function pnlCls(v: number | null) {
  if (v === null) return "";
  return v >= 0 ? "text-green-600 dark:text-green-400" : "text-red-600 dark:text-red-400";
}

// ── Allocation bar ─────────────────────────────────────────────────────────────

const ALLOC_COLORS = [
  "bg-blue-500", "bg-violet-500", "bg-amber-500", "bg-emerald-500",
  "bg-rose-500", "bg-cyan-500", "bg-orange-500", "bg-pink-500",
];

function AllocationBars({ data, total }: { data: [string, number][]; total: number }) {
  if (!data.length || !total) return <p className="text-xs text-muted-foreground">No data</p>;
  const sorted = [...data].sort((a, b) => b[1] - a[1]);
  return (
    <div className="space-y-2">
      {sorted.map(([label, value], i) => {
        const pct = (value / total) * 100;
        return (
          <div key={label}>
            <div className="flex justify-between text-xs mb-0.5">
              <span className="truncate max-w-[60%]">{label || "Unknown"}</span>
              <span className="tabular-nums text-muted-foreground">{fmtBaht(value, 0)} ({pct.toFixed(1)}%)</span>
            </div>
            <div className="h-2 rounded bg-muted overflow-hidden">
              <div
                className={`h-2 rounded ${ALLOC_COLORS[i % ALLOC_COLORS.length]}`}
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Main page ──────────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const router = useRouter();
  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);
  const [summaries, setSummaries] = useState<Record<string, PortfolioSummary>>({});
  const [holdingsMap, setHoldingsMap] = useState<Record<string, HoldingRow[]>>({});
  const [retField, setRetField] = useState(() => pnlFieldKey(derivePnlBasis(loadSettings())));
  const [me, setMe] = useState<CurrentUser | null>(null);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  // Rename state
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameError, setRenameError] = useState("");

  // Delete state
  const [deleteTarget, setDeleteTarget] = useState<Portfolio | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    const onStorage = () => setRetField(pnlFieldKey(derivePnlBasis(loadSettings())));
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => {
    if (!localStorage.getItem("token")) { router.replace("/login"); return; }
    api.getMe().then((u) => setMe(u)).catch(() => {});
    api.listPortfolios().then((ps) => {
      setPortfolios(ps);
      ps.forEach((p) => {
        api.getPortfolioSummary(p.id)
          .then((s) => setSummaries((prev) => ({ ...prev, [p.id]: s })))
          .catch(() => {});
        api.getPortfolioHoldings(p.id)
          .then((h) => setHoldingsMap((prev) => ({ ...prev, [p.id]: h })))
          .catch(() => {});
      });
    }).catch(() => { clearToken(); router.replace("/login"); });
  }, [router]);

  // ── Aggregate computations ─────────────────────────────────────────────────

  const allHoldings = useMemo(
    () => portfolios.flatMap((p) => holdingsMap[p.id] ?? []),
    [portfolios, holdingsMap],
  );

  const agg = useMemo(() => {
    const sums = portfolios.map((p) => summaries[p.id]).filter(Boolean);
    if (!sums.length) return null;

    const totalCost = sums.reduce((s, x) => s + Number(x.total_cost_basis), 0);
    const totalInvested = sums.reduce((s, x) => s + Number(x.total_invested), 0);
    const realizedPnL = sums.reduce((s, x) => s + Number(x.realized_pnl), 0);

    // Only compute value / unrealized if ALL summaries have NAV
    const allHaveNav = sums.every((s) => s.total_market_value != null);
    const totalValue = allHaveNav
      ? sums.reduce((s, x) => s + Number(x.total_market_value!), 0)
      : null;
    const unrealizedPnL = totalValue !== null ? totalValue - totalCost : null;
    const unrealizedPnLPct = unrealizedPnL !== null && totalCost > 0
      ? (unrealizedPnL / totalCost) * 100
      : null;

    // Dividends from open holdings only (approximate)
    const dividendsNet = allHoldings.reduce((s, h) => s + Number(h.dividends_net ?? 0), 0);

    // Total return = unrealized + realized + dividends
    const totalReturn = unrealizedPnL !== null ? unrealizedPnL + realizedPnL + dividendsNet : null;
    const totalReturnPct = totalReturn !== null && totalInvested > 0
      ? (totalReturn / totalInvested) * 100
      : null;

    const openPositions = sums.reduce((s, x) => s + x.open_positions, 0);
    const uniqueFunds = new Set(allHoldings.map((h) => h.fund_code)).size;

    return {
      totalValue, totalCost, totalInvested, unrealizedPnL, unrealizedPnLPct,
      realizedPnL, dividendsNet, totalReturn, totalReturnPct,
      openPositions, uniqueFunds,
    };
  }, [portfolios, summaries, allHoldings]);

  // Top holdings by market value
  const topHoldings = useMemo(
    () => [...allHoldings]
      .filter((h) => h.market_value != null)
      .sort((a, b) => Number(b.market_value!) - Number(a.market_value!))
      .slice(0, 5),
    [allHoldings],
  );

  // Gainers / Losers — sorted by whichever return field matches current settings
  const { topGainers, topLosers } = useMemo(() => {
    const ranked = [...allHoldings]
      .filter((h) => h[retField as keyof HoldingRow] != null)
      .sort((a, b) => Number(b[retField as keyof HoldingRow]) - Number(a[retField as keyof HoldingRow]));
    return { topGainers: ranked.slice(0, 3), topLosers: ranked.slice(-3).reverse() };
  }, [allHoldings, retField]);

  // Allocation
  const { byAssetClass, byScheme } = useMemo(() => {
    const cls: Record<string, number> = {};
    const scheme: Record<string, number> = {};
    for (const h of allHoldings) {
      if (!h.market_value) continue;
      const mv = Number(h.market_value);
      cls[h.asset_class ?? "Unknown"] = (cls[h.asset_class ?? "Unknown"] ?? 0) + mv;
      scheme[h.tax_scheme] = (scheme[h.tax_scheme] ?? 0) + mv;
    }
    return {
      byAssetClass: Object.entries(cls),
      byScheme: Object.entries(scheme),
    };
  }, [allHoldings]);

  // Data freshness
  const { staleNavFunds, oldestNavDate } = useMemo(() => {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const stale: string[] = [];
    let oldest: string | null = null;
    for (const h of allHoldings) {
      if (!h.latest_nav_date) { stale.push(h.fund_code); continue; }
      if (!oldest || h.latest_nav_date < oldest) oldest = h.latest_nav_date;
      const days = Math.floor((today.getTime() - new Date(h.latest_nav_date).getTime()) / 86400000);
      if (days > 5) stale.push(h.fund_code);
    }
    return { staleNavFunds: stale, oldestNavDate: oldest };
  }, [allHoldings]);

  // ── Event handlers ─────────────────────────────────────────────────────────

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const p = await api.createPortfolio(newName.trim());
      setPortfolios((prev) => [...prev, p]);
      setNewName("");
      api.getPortfolioSummary(p.id).then((s) => setSummaries((prev) => ({ ...prev, [p.id]: s }))).catch(() => {});
      api.getPortfolioHoldings(p.id).then((h) => setHoldingsMap((prev) => ({ ...prev, [p.id]: h }))).catch(() => {});
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create portfolio");
    } finally {
      setCreating(false);
    }
  }

  function startRename(p: Portfolio, e: React.MouseEvent) {
    e.stopPropagation();
    setRenamingId(p.id);
    setRenameValue(p.name);
    setRenameError("");
  }

  async function confirmRename(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    if (!renameValue.trim()) return;
    try {
      const updated = await api.renamePortfolio(id, renameValue.trim());
      setPortfolios((prev) => prev.map((p) => (p.id === id ? updated : p)));
      setRenamingId(null);
    } catch (err: unknown) {
      setRenameError(err instanceof Error ? err.message : "Rename failed");
    }
  }

  function cancelRename(e: React.MouseEvent) {
    e.stopPropagation();
    setRenamingId(null);
    setRenameError("");
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.deletePortfolio(deleteTarget.id);
      setPortfolios((prev) => prev.filter((p) => p.id !== deleteTarget.id));
      setSummaries((prev) => { const n = { ...prev }; delete n[deleteTarget.id]; return n; });
      setHoldingsMap((prev) => { const n = { ...prev }; delete n[deleteTarget.id]; return n; });
      setDeleteTarget(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setDeleting(false);
    }
  }

  function logout() { clearToken(); router.push("/login"); }

  // ── Render ─────────────────────────────────────────────────────────────────

  const hasAgg = agg !== null && portfolios.length > 0;
  const hasInsights = allHoldings.length > 0;

  return (
    <div className="min-h-screen bg-muted/40">
      {/* ── Header ── */}
      <header className="border-b bg-background px-4 py-3 flex items-center justify-between">
        <h1 className="font-semibold text-lg">Thai Fund Tracker</h1>
        <div className="flex items-center gap-1">
          <ThemeToggle />
          {me?.role === "admin" && (
            <Button variant="ghost" size="sm" onClick={() => router.push("/dashboard/sync")} title="Data sync">
              <RefreshCw className="h-4 w-4 sm:mr-1" />
              <span className="hidden sm:inline">Sync</span>
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={() => router.push("/dashboard/settings")} title="Settings">
            <Settings className="h-4 w-4 sm:mr-1" />
            <span className="hidden sm:inline">Settings</span>
          </Button>
          <Button variant="ghost" size="sm" onClick={logout}>
            <LogOut className="h-4 w-4 sm:mr-1" />
            <span className="hidden sm:inline">Logout</span>
          </Button>
        </div>
      </header>

      <main className="max-w-5xl mx-auto p-4 space-y-6">
        {error && <p className="text-sm text-destructive">{error}</p>}

        {/* ── Stale NAV alert ── */}
        {staleNavFunds.length > 0 && (
          <div className="flex items-start gap-2 rounded-md border border-yellow-400 bg-yellow-50 dark:border-yellow-700 dark:bg-yellow-900/20 px-3 py-2 text-xs text-yellow-800 dark:text-yellow-300">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
            <span>
              <strong>Stale NAV:</strong> {staleNavFunds.slice(0, 5).join(", ")}
              {staleNavFunds.length > 5 ? ` +${staleNavFunds.length - 5} more` : ""} — run NAV sync on the{" "}
              <a href="/dashboard/sync" className="underline font-medium">Sync page</a>.
            </span>
          </div>
        )}

        {/* ── Aggregate KPI bar ── */}
        {hasAgg && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {/* Total Value */}
            <Card>
              <CardContent className="pt-4">
                <p className="text-xs text-muted-foreground mb-1">Total Value</p>
                <p className="text-xl font-bold tabular-nums">
                  {agg.totalValue !== null ? fmtBaht(agg.totalValue, 0) : "–"}
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  Cost {fmtBaht(agg.totalCost, 0)}
                </p>
              </CardContent>
            </Card>

            {/* Unrealized P&L */}
            <Card>
              <CardContent className="pt-4">
                <p className="text-xs text-muted-foreground mb-1">Unrealized P&L</p>
                <p className={`text-xl font-bold tabular-nums ${pnlCls(agg.unrealizedPnL)}`}>
                  {agg.unrealizedPnL !== null
                    ? `${agg.unrealizedPnL >= 0 ? "+" : ""}${fmtBaht(agg.unrealizedPnL, 0)}`
                    : "–"}
                </p>
                <p className={`text-xs mt-1 ${pnlCls(agg.unrealizedPnLPct)}`}>
                  {agg.unrealizedPnLPct !== null ? fmtPctSigned(agg.unrealizedPnLPct) : "–"}
                </p>
              </CardContent>
            </Card>

            {/* Income */}
            <Card>
              <CardContent className="pt-4">
                <p className="text-xs text-muted-foreground mb-1">Income</p>
                <p className="text-xl font-bold tabular-nums text-green-600 dark:text-green-400">
                  {fmtBaht(agg.dividendsNet + agg.realizedPnL, 0)}
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  Div {fmtBaht(agg.dividendsNet, 0)} · Realized {fmtBaht(agg.realizedPnL, 0)}
                </p>
              </CardContent>
            </Card>

            {/* Total Return */}
            <Card>
              <CardContent className="pt-4">
                <p className="text-xs text-muted-foreground mb-1">Total Return</p>
                <p className={`text-xl font-bold tabular-nums ${pnlCls(agg.totalReturn)}`}>
                  {agg.totalReturnPct !== null ? fmtPctSigned(agg.totalReturnPct) : "–"}
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  {agg.uniqueFunds} funds · {agg.openPositions} positions
                </p>
              </CardContent>
            </Card>
          </div>
        )}

        {/* ── Portfolio cards ── */}
        <div>
          <h2 className="text-base font-semibold mb-3">My Portfolios</h2>
          {portfolios.length === 0 ? (
            <p className="text-muted-foreground text-sm">No portfolios yet.</p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {portfolios.map((p) => {
                const s = summaries[p.id];
                const pnl = s?.unrealized_pnl ? Number(s.unrealized_pnl) : null;
                const pnlPct = s?.unrealized_pnl_pct ? Number(s.unrealized_pnl_pct) : null;
                const xirr = s?.xirr ? Number(s.xirr) * 100 : null;
                const realized = s?.realized_pnl ? Number(s.realized_pnl) : 0;
                return (
                  <Card
                    key={p.id}
                    className="cursor-pointer hover:shadow-md transition-shadow"
                    onClick={() => renamingId !== p.id && router.push(`/dashboard/portfolios/${p.id}`)}
                  >
                    <CardHeader className="pb-2">
                      <CardTitle className="text-base flex items-center gap-2">
                        <FolderOpen className="h-4 w-4 text-muted-foreground shrink-0" />
                        {renamingId === p.id ? (
                          <div className="flex items-center gap-1 flex-1" onClick={(e) => e.stopPropagation()}>
                            <Input
                              autoFocus
                              value={renameValue}
                              onChange={(e) => setRenameValue(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") confirmRename(p.id, e as unknown as React.MouseEvent);
                                if (e.key === "Escape") cancelRename(e as unknown as React.MouseEvent);
                              }}
                              className="h-7 text-sm"
                            />
                            <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={(e) => confirmRename(p.id, e)}>
                              <Check className="h-3.5 w-3.5 text-green-600" />
                            </Button>
                            <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={cancelRename}>
                              <X className="h-3.5 w-3.5 text-muted-foreground" />
                            </Button>
                          </div>
                        ) : (
                          <span className="flex-1 truncate">{p.name}</span>
                        )}
                        {renamingId !== p.id && (
                          <div className="flex items-center gap-0.5 ml-auto shrink-0">
                            <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={(e) => startRename(p, e)}>
                              <Pencil className="h-3.5 w-3.5 text-muted-foreground" />
                            </Button>
                            <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={(e) => { e.stopPropagation(); setDeleteTarget(p); }}>
                              <Trash2 className="h-3.5 w-3.5 text-destructive" />
                            </Button>
                          </div>
                        )}
                      </CardTitle>
                      {renameError && renamingId === p.id && (
                        <p className="text-xs text-destructive mt-1">{renameError}</p>
                      )}
                    </CardHeader>
                    <CardContent>
                      {!s ? (
                        <p className="text-xs text-muted-foreground animate-pulse">Loading…</p>
                      ) : (
                        <div className="space-y-1.5">
                          <div className="flex items-baseline justify-between">
                            <span className="text-xs text-muted-foreground">Value</span>
                            <span className="text-sm font-semibold tabular-nums">
                              {s.total_market_value ? fmtBaht(Number(s.total_market_value)) : "–"}
                            </span>
                          </div>
                          <div className="flex items-baseline justify-between">
                            <span className="text-xs text-muted-foreground">Unrealized P&L</span>
                            <span className={`text-sm font-medium tabular-nums ${pnlCls(pnl)}`}>
                              {pnl !== null ? `${pnl >= 0 ? "+" : ""}${fmtBaht(Math.abs(pnl))}` : "–"}
                              {pnlPct !== null && <span className="text-xs ml-1">({fmtPctSigned(pnlPct)})</span>}
                            </span>
                          </div>
                          {realized !== 0 && (
                            <div className="flex items-baseline justify-between">
                              <span className="text-xs text-muted-foreground">Realized P&L</span>
                              <span className={`text-xs tabular-nums ${pnlCls(realized)}`}>
                                {realized >= 0 ? "+" : ""}{fmtBaht(Math.abs(realized))}
                              </span>
                            </div>
                          )}
                          <div className="flex items-baseline justify-between">
                            <span className="text-xs text-muted-foreground">XIRR</span>
                            <span className={`text-xs font-medium tabular-nums ${pnlCls(xirr)}`}>
                              {xirr !== null ? fmtPctSigned(xirr) : s.xirr_error === "no_nav" ? "ไม่มี NAV" : "–"}
                            </span>
                          </div>
                          <div className="flex items-baseline justify-between">
                            <span className="text-xs text-muted-foreground">Funds</span>
                            <span className="text-xs text-foreground">{s.open_positions} positions</span>
                          </div>
                          <p className="text-xs text-muted-foreground pt-0.5">Created {new Date(p.created_at).toLocaleDateString("th-TH")}</p>
                        </div>
                      )}
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          )}
        </div>

        {/* ── Insights (Top Holdings + Gainers/Losers) ── */}
        {hasInsights && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* Top Holdings */}
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-semibold">Top Holdings by Value</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b bg-muted/40">
                      <th className="px-3 py-2 text-left font-medium text-muted-foreground">Fund</th>
                      <th className="px-3 py-2 text-left font-medium text-muted-foreground">Portfolio</th>
                      <th className="px-3 py-2 text-right font-medium text-muted-foreground">Value</th>
                      <th className="px-3 py-2 text-right font-medium text-muted-foreground">Return</th>
                    </tr>
                  </thead>
                  <tbody>
                    {topHoldings.map((h, i) => {
                      const portfolio = portfolios.find((p) =>
                        (holdingsMap[p.id] ?? []).some((x) => x.fund_code === h.fund_code && x.tax_scheme === h.tax_scheme)
                      );
                      const ret = h[retField as keyof HoldingRow] != null ? Number(h[retField as keyof HoldingRow]) : null;
                      return (
                        <tr key={i} className="border-b hover:bg-muted/30">
                          <td className="px-3 py-2 font-mono font-medium">{h.fund_code}</td>
                          <td className="px-3 py-2 text-muted-foreground truncate max-w-[80px]">{portfolio?.name ?? "–"}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{fmtBaht(Number(h.market_value!))}</td>
                          <td className={`px-3 py-2 text-right tabular-nums ${pnlCls(ret)}`}>
                            {ret !== null ? fmtPctSigned(ret) : "–"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </CardContent>
            </Card>

            {/* Gainers / Losers */}
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-semibold">Performance (Total Return %)</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <p className="text-xs font-medium text-green-600 dark:text-green-400 flex items-center gap-1 mb-2">
                      <TrendingUp className="h-3 w-3" /> Top Gainers
                    </p>
                    <div className="space-y-2">
                      {topGainers.length === 0 && <p className="text-xs text-muted-foreground">–</p>}
                      {topGainers.map((h, i) => (
                        <div key={i} className="flex items-center justify-between gap-1">
                          <span className="font-mono text-xs truncate">{h.fund_code}</span>
                          <span className="text-xs font-medium tabular-nums text-green-600 dark:text-green-400 shrink-0">
                            {fmtPctSigned(Number(h[retField as keyof HoldingRow]))}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div>
                    <p className="text-xs font-medium text-red-600 dark:text-red-400 flex items-center gap-1 mb-2">
                      <TrendingDown className="h-3 w-3" /> Underperformers
                    </p>
                    <div className="space-y-2">
                      {topLosers.length === 0 && <p className="text-xs text-muted-foreground">–</p>}
                      {topLosers.map((h, i) => (
                        <div key={i} className="flex items-center justify-between gap-1">
                          <span className="font-mono text-xs truncate">{h.fund_code}</span>
                          <span className="text-xs font-medium tabular-nums text-red-600 dark:text-red-400 shrink-0">
                            {fmtPctSigned(Number(h[retField as keyof HoldingRow]))}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        )}

        {/* ── Allocation ── */}
        {hasInsights && agg?.totalValue && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-semibold">By Asset Class</CardTitle>
              </CardHeader>
              <CardContent>
                <AllocationBars data={byAssetClass} total={agg.totalValue} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-semibold">By Tax Scheme</CardTitle>
              </CardHeader>
              <CardContent>
                <AllocationBars data={byScheme} total={agg.totalValue} />
              </CardContent>
            </Card>
          </div>
        )}

        {/* ── Data freshness footer ── */}
        {oldestNavDate && (
          <p className="text-xs text-muted-foreground text-center">
            Oldest NAV date across holdings: {oldestNavDate}
          </p>
        )}

        {/* ── Create Portfolio ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">New Portfolio</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleCreate} className="flex gap-2">
              <Input
                placeholder="Portfolio name"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                className="flex-1"
              />
              <Button type="submit" disabled={creating} size="sm">
                <PlusCircle className="h-4 w-4 sm:mr-1" />
                <span className="hidden sm:inline">Create</span>
              </Button>
            </form>
          </CardContent>
        </Card>

      </main>

      {/* ── Delete confirmation dialog ── */}
      <Dialog open={!!deleteTarget} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Delete Portfolio</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            Delete <span className="font-medium text-foreground">{deleteTarget?.name}</span>? This will permanently remove all transactions and tax lots inside it. This cannot be undone.
          </p>
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" size="sm" onClick={() => setDeleteTarget(null)}>Cancel</Button>
            <Button variant="destructive" size="sm" disabled={deleting} onClick={confirmDelete}>
              {deleting ? "Deleting…" : "Delete"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
