"use client";
import React, { useEffect, useRef, useState } from "react";
import { useRouter, useParams } from "next/navigation";
import {
  PieChart, Pie, Cell, Tooltip, ResponsiveContainer, Legend,
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  BarChart, Bar, ReferenceLine, LabelList,
} from "recharts";
import {
  api,
  type Transaction, type TransactionCreate, type Portfolio,
  type PortfolioSummary, type HoldingRow, type AllocationResult, type LotEligibility,
  type FundPerformance, type FundRiskMetrics, type FundResult, type NavPoint, type AiSummary,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { ArrowLeft, PlusCircle, Upload, Download, Trash2, RefreshCw, X, Sparkles, ChevronDown, ChevronUp, ArrowUp, ArrowDown, ArrowUpDown, MoveRight, Settings } from "lucide-react";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { loadSettings, derivePnlBasis, pnlFieldKey, pnlBasisLabel, type PnlBasis } from "@/lib/settings";

const CSV_TEMPLATE = `date,type,fund_code,units,nav,amount,fee,tax_withheld,target_fund_code,pair_id,tax_scheme,note
2024-03-15,BUY,SCBSET,1000,12.3456,12345.60,0,0,,,NORMAL,First purchase
2024-08-20,SELL,SCBSET,500,13.2100,6605.00,33.03,0,,,NORMAL,Partial sale
2024-09-10,SWITCH_OUT,SCBSET,500,13.5000,6750.00,0,0,SCBTOP,switch-001,NORMAL,Switch to SCBTOP
2024-09-10,SWITCH_IN,SCBTOP,450,15.0000,6750.00,0,0,SCBSET,switch-001,NORMAL,Switch from SCBSET
2024-12-15,DIVIDEND,SCBSET,,,250.00,0,25.00,,,NORMAL,Q4 dividend
2024-12-31,INTEREST,,,,150.50,0,15.05,,,NORMAL,Cash interest
`;

function downloadTemplate() {
  const blob = new Blob([CSV_TEMPLATE], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "import_template.csv";
  a.click();
  URL.revokeObjectURL(url);
}

const TX_TYPES = ["BUY", "SELL", "DIVIDEND", "INTEREST"];
const SCHEMES = ["NORMAL", "RMF", "SSF", "THAI_ESG", "THAI_ESG_EXTRA", "LTF"];

const TYPE_BADGE: Record<string, string> = {
  BUY: "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
  SELL: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
  SWITCH_OUT: "bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300",
  SWITCH_IN: "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300",
  DIVIDEND: "bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-300",
  INTEREST: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300",
};

const PIE_COLORS = [
  "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
  "#06b6d4", "#f97316", "#84cc16", "#ec4899", "#6b7280",
];

function fmtNum(n: string | null | undefined, decimals = 2) {
  if (!n) return "–";
  return Number(n).toLocaleString("th-TH", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtPct(n: string | null | undefined) {
  if (!n) return "–";
  const v = Number(n);
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function fmtXirr(n: string | null | undefined) {
  if (!n) return "–";
  const v = Number(n) * 100;
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function pnlClass(n: string | null | undefined) {
  if (!n) return "";
  return Number(n) >= 0
    ? "text-green-600 dark:text-green-400"
    : "text-red-600 dark:text-red-400";
}

function fmtDaysLeft(days: number): string {
  if (days <= 0) return "–";
  const y = Math.floor(days / 365);
  const m = Math.floor((days % 365) / 30);
  const d = days % 30;
  const parts: string[] = [];
  if (y > 0) parts.push(`${y}y`);
  if (m > 0) parts.push(`${m}m`);
  if (d > 0) parts.push(`${d}d`);
  return parts.join(" ") || "–";
}

function fmtAge(days: number | null | undefined) {
  if (!days) return "–";
  if (days >= 365) {
    const y = Math.floor(days / 365);
    const m = Math.floor((days % 365) / 30);
    return m > 0 ? `${y}ปี ${m}เดือน` : `${y}ปี`;
  }
  if (days >= 30) return `${Math.floor(days / 30)}เดือน`;
  return `${days}วัน`;
}

// ── Summary cards ─────────────────────────────────────────────────────────────

function SummarySection({ summary }: { summary: PortfolioSummary }) {
  const cards = [
    {
      label: "Portfolio Value",
      value: summary.total_market_value ? `฿${fmtNum(summary.total_market_value)}` : "–",
      sub: `Cost basis ฿${fmtNum(summary.total_cost_basis)}`,
      subClass: "",
    },
    {
      label: "Unrealized P&L",
      value: summary.unrealized_pnl ? `฿${fmtNum(summary.unrealized_pnl)}` : "–",
      sub: fmtPct(summary.unrealized_pnl_pct),
      subClass: pnlClass(summary.unrealized_pnl_pct),
      valueClass: pnlClass(summary.unrealized_pnl),
    },
    {
      label: "Realized P&L",
      value: `฿${fmtNum(summary.realized_pnl)}`,
      sub: "",
      subClass: "",
      valueClass: pnlClass(summary.realized_pnl),
    },
    {
      label: "XIRR (Money-Weighted)",
      value: summary.xirr ? fmtXirr(summary.xirr) : summary.xirr_error ?? "–",
      sub: "Annualized return",
      subClass: "text-muted-foreground",
      valueClass: summary.xirr ? pnlClass(summary.xirr) : "text-muted-foreground",
    },
    {
      label: "TWR (Time-Weighted)",
      value: summary.twr ? fmtXirr(summary.twr) : summary.twr_error ?? "–",
      sub: "Annualized return",
      subClass: "text-muted-foreground",
      valueClass: summary.twr ? pnlClass(summary.twr) : "text-muted-foreground",
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      {cards.map((c) => (
        <Card key={c.label}>
          <CardContent className="pt-4 pb-3">
            <p className="text-xs text-muted-foreground">{c.label}</p>
            <p className={`text-lg font-semibold tabular-nums ${c.valueClass ?? ""}`}>{c.value}</p>
            {c.sub && <p className={`text-xs tabular-nums ${c.subClass}`}>{c.sub}</p>}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// ── AI Summary ────────────────────────────────────────────────────────────────

function AiSummarySection({ portfolioId }: { portfolioId: string }) {
  const [summary, setSummary] = useState<AiSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.getAiSummary(portfolioId)
      .then(setSummary)
      .catch(() => setSummary(null))
      .finally(() => setLoading(false));
  }, [portfolioId]);

  async function handleRefresh() {
    setRefreshing(true);
    setError("");
    const prevAt = summary?.generated_at ?? "";

    try {
      const s = await api.refreshAiSummary(portfolioId);
      setSummary(s);
      setRefreshing(false);
      return;
    } catch {
      // Proxy timed out — backend is still generating. Poll until saved.
    }

    const deadline = Date.now() + 180_000;
    while (Date.now() < deadline) {
      await new Promise<void>((r) => setTimeout(r, 4000));
      try {
        const s = await api.getAiSummary(portfolioId);
        if (s.generated_at !== prevAt) {
          setSummary(s);
          setRefreshing(false);
          return;
        }
      } catch {}
    }

    setError("การสร้างใช้เวลานานเกินไป — ลองกดอีกครั้ง");
    setRefreshing(false);
  }

  return (
    <Card>
      <CardContent className="pt-4 pb-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-1.5 text-sm font-medium">
            <Sparkles className="h-4 w-4 text-purple-500" />
            AI วิเคราะห์พอร์ต
          </div>
          <Button variant="ghost" size="sm" onClick={handleRefresh} disabled={refreshing} className="h-7 px-2 text-xs shrink-0">
            <RefreshCw className={`h-3 w-3 mr-1 ${refreshing ? "animate-spin" : ""}`} />
            {refreshing ? "กำลังวิเคราะห์…" : "อัปเดต"}
          </Button>
        </div>
        {loading ? (
          <p className="text-xs text-muted-foreground mt-2">กำลังโหลด…</p>
        ) : summary ? (
          <div className="mt-2">
            <p className="text-sm leading-relaxed">{summary.content}</p>
            <p className="text-xs text-muted-foreground mt-1">
              อัปเดต: {new Date(summary.generated_at).toLocaleString("th-TH", { timeZone: "Asia/Bangkok" })}
            </p>
          </div>
        ) : (
          <p className="text-xs text-muted-foreground mt-2">
            ยังไม่มีการวิเคราะห์ — กดปุ่ม &quot;อัปเดต&quot; เพื่อสร้าง
          </p>
        )}
        {error && <p className="text-xs text-destructive mt-1">{error}</p>}
      </CardContent>
    </Card>
  );
}

// ── P&L Ranking chart ─────────────────────────────────────────────────────────

function PnlRankingChart({ holdings, pnlBasis }: { holdings: HoldingRow[]; pnlBasis: PnlBasis }) {
  const pctKey = pnlFieldKey(pnlBasis) as keyof HoldingRow;
  const data = [...holdings]
    .filter((h) => h[pctKey] !== null)
    .sort((a, b) => Number(b[pctKey]) - Number(a[pctKey]))
    .map((h) => {
      const entryDate = h.fund_entry_date ?? h.oldest_purchase_date;
      const ageDays = entryDate ? Math.floor((Date.now() - new Date(entryDate).getTime()) / 86400000) : null;
      return {
        name: h.fund_code,
        pct: parseFloat(Number(h[pctKey]).toFixed(2)),
        market_value: h.market_value ? Number(h.market_value) : null,
        dividends_net: Number(h.dividends_net),
        ageDays,
      };
    });

  if (!data.length) return null;

  function PnlTooltip({ active, payload }: { active?: boolean; payload?: { payload: typeof data[0] }[] }) {
    if (!active || !payload?.length) return null;
    const d = payload[0].payload;
    const ageStr = d.ageDays !== null
      ? d.ageDays >= 365
        ? `${(d.ageDays / 365.25).toFixed(1)} ปี`
        : `${d.ageDays} วัน`
      : "–";
    const fmtBaht = (v: number) => `฿${v.toLocaleString("th-TH", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    return (
      <div className="rounded-md border bg-background px-3 py-2 text-xs shadow-md space-y-1">
        <p className="font-semibold">{d.name}</p>
        <p className={d.pct >= 0 ? "text-green-500" : "text-red-500"}>
          {d.pct >= 0 ? "+" : ""}{d.pct.toFixed(2)}%
        </p>
        <p className="text-muted-foreground">
          มูลค่า: {d.market_value !== null ? fmtBaht(d.market_value) : "–"}
        </p>
        {d.dividends_net > 0 && (
          <p className="text-muted-foreground">เงินปันผล (net): {fmtBaht(d.dividends_net)}</p>
        )}
        <p className="text-muted-foreground">อายุ: {ageStr}</p>
      </div>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-1">
        <CardTitle className="text-base">
          P&L Ranking (กำไร/ขาดทุน %)
          <span className="ml-2 text-xs font-normal text-muted-foreground">{pnlBasisLabel(loadSettings())}</span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={Math.max(80, data.length * 44)}>
          <BarChart data={data} layout="vertical" margin={{ left: 8, right: 40, top: 4, bottom: 4 }}>
            <CartesianGrid strokeDasharray="3 3" horizontal={false} />
            <XAxis type="number" tickFormatter={(v) => `${v}%`} domain={["auto", "auto"]} tick={{ fontSize: 11, fill: "#ffffff" }} />
            <YAxis type="category" dataKey="name" tick={{ fontSize: 11, fill: "#ffffff" }} width={110} />
            <Tooltip content={<PnlTooltip />} />
            <ReferenceLine x={0} stroke="#888" />
            <Bar dataKey="pct" radius={[0, 3, 3, 0]}>
              {data.map((entry, i) => (
                <Cell key={i} fill={entry.pct >= 0 ? "#10b981" : "#ef4444"} />
              ))}
              <LabelList dataKey="pct" position="right" formatter={(v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`} style={{ fontSize: 11, fill: "#ffffff" }} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}

// ── NAV History chart ─────────────────────────────────────────────────────────

const NAV_RANGES = [
  { label: "1M", days: 30 },
  { label: "3M", days: 90 },
  { label: "1Y", days: 365 },
  { label: "MAX", days: 1500 },
];

function NavChart({ fundCode, entryDate }: { fundCode: string; entryDate?: string }) {
  const [range, setRange] = useState(365);
  const [allHistory, setAllHistory] = useState<NavPoint[]>([]);
  const [loading, setLoading] = useState(true);

  // Fetch all data since entry date once; range buttons filter client-side
  useEffect(() => {
    setLoading(true);
    const daysSinceEntry = entryDate
      ? Math.ceil((Date.now() - new Date(entryDate).getTime()) / 86400000) + 5
      : 1500;
    api.getFundNavHistory(fundCode, Math.max(daysSinceEntry, 30))
      .then(setAllHistory)
      .catch(() => setAllHistory([]))
      .finally(() => setLoading(false));
  }, [fundCode, entryDate]);

  const history = (() => {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - range);
    // Never show data before fund entry date
    const effectiveCutoff = entryDate && new Date(entryDate) > cutoff ? new Date(entryDate) : cutoff;
    const cutoffStr = effectiveCutoff.toISOString().slice(0, 10);
    return allHistory.filter((p) => p.date >= cutoffStr);
  })();

  const minNav = history.length ? Math.min(...history.map((p) => p.nav)) : 0;
  const maxNav = history.length ? Math.max(...history.map((p) => p.nav)) : 0;
  const padding = (maxNav - minNav) * 0.05 || 0.1;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs font-medium text-muted-foreground">
          {fundCode} — NAV History
          {entryDate && <span className="ml-1 text-muted-foreground/60">(since {entryDate})</span>}
        </p>
        <div className="flex gap-1">
          {NAV_RANGES.map((r) => (
            <button
              key={r.label}
              onClick={() => setRange(r.days)}
              className={`text-xs px-2 py-0.5 rounded ${range === r.days ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>
      {loading ? (
        <div className="h-32 flex items-center justify-center text-xs text-muted-foreground">Loading…</div>
      ) : history.length < 2 ? (
        <div className="h-32 flex items-center justify-center text-xs text-muted-foreground">ข้อมูลไม่เพียงพอ — รัน NAV Backfill</div>
      ) : (
        <ResponsiveContainer width="100%" height={160}>
          <LineChart data={history} margin={{ left: 0, right: 4, top: 4, bottom: 4 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.4} />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 10 }}
              tickFormatter={(d) => d.slice(5)}
              interval="preserveStartEnd"
            />
            <YAxis
              domain={[minNav - padding, maxNav + padding]}
              tick={{ fontSize: 10 }}
              tickFormatter={(v) => v.toFixed(2)}
              width={48}
            />
            <Tooltip
              content={({ active, payload, label }) => {
                if (!active || !payload?.length) return null;
                return (
                  <div className="rounded border bg-background px-3 py-2 text-xs shadow-md">
                    <p className="font-medium text-foreground mb-1">{label}</p>
                    <p className="text-muted-foreground">NAV: <span className="font-mono font-medium text-foreground">฿{Number(payload[0].value).toFixed(4)}</span></p>
                  </div>
                );
              }}
            />
            <Line
              type="monotone"
              dataKey="nav"
              dot={false}
              strokeWidth={1.5}
              stroke={history[history.length - 1]?.nav >= history[0]?.nav ? "#10b981" : "#ef4444"}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

// ── Dividend Summary ───────────────────────────────────────────────────────────

function DividendSummarySection({ holdings }: { holdings: HoldingRow[] }) {
  const rows = holdings
    .filter((h) => Number(h.dividends_gross) > 0)
    .sort((a, b) => Number(b.dividends_net) - Number(a.dividends_net));

  if (!rows.length) return null;

  const totalGross = rows.reduce((s, h) => s + Number(h.dividends_gross), 0);
  const totalNet = rows.reduce((s, h) => s + Number(h.dividends_net), 0);
  const totalTax = totalGross - totalNet;

  const fmtB = (v: number) => `฿${v.toLocaleString("th-TH", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  return (
    <Card>
      <CardHeader className="pb-1">
        <CardTitle className="text-base">Dividend Income</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50 text-xs">
                <th className="px-3 py-2 text-left font-medium text-muted-foreground">Fund</th>
                <th className="px-3 py-2 text-left font-medium text-muted-foreground">Scheme</th>
                <th className="px-3 py-2 text-right font-medium text-muted-foreground">Gross</th>
                <th className="px-3 py-2 text-right font-medium text-muted-foreground">Tax Withheld</th>
                <th className="px-3 py-2 text-right font-medium text-muted-foreground">Net</th>
                <th className="px-3 py-2 text-right font-medium text-muted-foreground">Yield (net/cost)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((h, i) => {
                const gross = Number(h.dividends_gross);
                const net = Number(h.dividends_net);
                const tax = gross - net;
                const yieldPct = Number(h.cost_basis) > 0 ? (net / Number(h.cost_basis)) * 100 : null;
                return (
                  <tr key={i} className="border-b hover:bg-muted/30">
                    <td className="px-3 py-2 font-mono text-xs font-medium">{h.fund_code}</td>
                    <td className="px-3 py-2">
                      <span className="text-xs bg-secondary text-secondary-foreground rounded px-1.5 py-0.5">{h.tax_scheme}</span>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">{fmtB(gross)}</td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{fmtB(tax)}</td>
                    <td className="px-3 py-2 text-right tabular-nums font-medium text-green-600 dark:text-green-400">{fmtB(net)}</td>
                    <td className="px-3 py-2 text-right tabular-nums text-xs text-muted-foreground">
                      {yieldPct !== null ? `${yieldPct.toFixed(2)}%` : "–"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr className="border-t bg-muted/30 font-medium text-xs">
                <td className="px-3 py-2" colSpan={2}>Total</td>
                <td className="px-3 py-2 text-right tabular-nums">{fmtB(totalGross)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{fmtB(totalTax)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-green-600 dark:text-green-400">{fmtB(totalNet)}</td>
                <td className="px-3 py-2" />
              </tr>
            </tfoot>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Holdings table ─────────────────────────────────────────────────────────────

type HoldingSortKey = "fund_code" | "tax_scheme" | "units" | "market_value" | "unrealized_pnl" | "unrealized_pnl_pct" | "fund_pnl_pct" | "total_return_pct" | "total_return_fund_pct" | "oldest_purchase_date" | "holding_days";

function HoldingsSection({
  holdings, portfolioId, portfolios, onTransferred, pnlBasis,
}: {
  holdings: HoldingRow[];
  portfolioId: string;
  portfolios: Portfolio[];
  onTransferred: () => void;
  pnlBasis: PnlBasis;
}) {
  const [expandedFund, setExpandedFund] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<HoldingSortKey>("market_value");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [moveTarget, setMoveTarget] = useState<string>("");
  const [movingFund, setMovingFund] = useState<string | null>(null);
  const [moveError, setMoveError] = useState("");
  const [moveLoading, setMoveLoading] = useState(false);

  const otherPortfolios = portfolios.filter((p) => p.id !== portfolioId);

  async function handleMove(fundCode: string, taxScheme: string) {
    if (!moveTarget) return;
    setMoveLoading(true);
    setMoveError("");
    try {
      await api.transferHolding(portfolioId, fundCode, taxScheme, moveTarget);
      setMovingFund(null);
      setMoveTarget("");
      onTransferred();
    } catch (e: unknown) {
      setMoveError(e instanceof Error ? e.message : "Transfer failed");
    } finally {
      setMoveLoading(false);
    }
  }

  if (!holdings.length) {
    return <p className="text-sm text-muted-foreground py-6 text-center">No open positions.</p>;
  }

  function toggleSort(key: HoldingSortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  function SortIcon({ col }: { col: HoldingSortKey }) {
    if (sortKey !== col) return <ArrowUpDown className="h-3 w-3 ml-1 opacity-30 inline" />;
    return sortDir === "asc"
      ? <ArrowUp className="h-3 w-3 ml-1 inline" />
      : <ArrowDown className="h-3 w-3 ml-1 inline" />;
  }

  function Th({ col, children, right }: { col: HoldingSortKey; children: React.ReactNode; right?: boolean }) {
    return (
      <th
        className={`px-3 py-2 font-medium cursor-pointer select-none hover:text-foreground text-muted-foreground whitespace-nowrap ${right ? "text-right" : "text-left"}`}
        onClick={() => toggleSort(col)}
      >
        {children}<SortIcon col={col} />
      </th>
    );
  }

  const sorted = [...holdings].sort((a, b) => {
    let cmp = 0;
    switch (sortKey) {
      case "fund_code":            cmp = a.fund_code.localeCompare(b.fund_code); break;
      case "tax_scheme":           cmp = a.tax_scheme.localeCompare(b.tax_scheme); break;
      case "units":                cmp = Number(a.units) - Number(b.units); break;
      case "market_value":         cmp = Number(a.market_value ?? 0) - Number(b.market_value ?? 0); break;
      case "unrealized_pnl":       cmp = Number(a.unrealized_pnl ?? 0) - Number(b.unrealized_pnl ?? 0); break;
      case "unrealized_pnl_pct":   cmp = Number(a.unrealized_pnl_pct ?? 0) - Number(b.unrealized_pnl_pct ?? 0); break;
      case "fund_pnl_pct":         cmp = Number(a.fund_pnl_pct ?? 0) - Number(b.fund_pnl_pct ?? 0); break;
      case "total_return_pct":      cmp = Number(a.total_return_pct ?? 0) - Number(b.total_return_pct ?? 0); break;
      case "total_return_fund_pct": cmp = Number(a.total_return_fund_pct ?? 0) - Number(b.total_return_fund_pct ?? 0); break;
      case "oldest_purchase_date": cmp = (a.fund_entry_date ?? a.oldest_purchase_date ?? "").localeCompare(b.fund_entry_date ?? b.oldest_purchase_date ?? ""); break;
      case "holding_days": {
        const aDays = a.fund_entry_date ? Math.floor((Date.now() - new Date(a.fund_entry_date).getTime()) / 86400000) : (a.holding_days ?? 0);
        const bDays = b.fund_entry_date ? Math.floor((Date.now() - new Date(b.fund_entry_date).getTime()) / 86400000) : (b.holding_days ?? 0);
        cmp = aDays - bDays; break;
      }
    }
    return sortDir === "asc" ? cmp : -cmp;
  });

  const unknownFunds = holdings.filter((h) => !h.fund_name_en).map((h) => h.fund_code);

  return (
    <div>
      {unknownFunds.length > 0 && (
        <div className="mx-3 mt-3 mb-1 rounded border border-yellow-300 bg-yellow-50 dark:border-yellow-700 dark:bg-yellow-900/20 px-3 py-2 text-xs text-yellow-800 dark:text-yellow-300">
          <strong>Fund code mismatch:</strong> {unknownFunds.join(", ")} not found in the SEC fund database.
          Run Fund Metadata sync on the <a href="/dashboard/sync" className="underline font-medium">Sync page</a>.
        </div>
      )}
      <div className="px-3 py-2 flex items-center gap-2 text-xs text-muted-foreground border-b">
        <span>Return: <span className="font-medium text-foreground">{pnlBasisLabel(loadSettings())}</span></span>
        <a href="/dashboard/settings" className="ml-auto flex items-center gap-1 hover:text-foreground transition-colors">
          <Settings className="h-3 w-3" />
          Change
        </a>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50 text-xs">
              <Th col="fund_code">Fund</Th>
              <Th col="tax_scheme">Scheme</Th>
              <Th col="units" right>Units</Th>
              <th className="px-3 py-2 text-right font-medium text-muted-foreground whitespace-nowrap">Latest NAV</th>
              <Th col="market_value" right>Market Value</Th>
              <Th col="unrealized_pnl" right>Unrealized P&L</Th>
              <Th col={pnlFieldKey(pnlBasis) as HoldingSortKey} right>Return %</Th>
              <Th col="oldest_purchase_date" right>Start Date</Th>
              <Th col="holding_days" right>Age</Th>
              {otherPortfolios.length > 0 && <th className="px-3 py-2" />}
            </tr>
          </thead>
          <tbody>
            {sorted.map((h, i) => (
              <>
                <tr
                  key={i}
                  className="border-b hover:bg-muted/30 cursor-pointer"
                  onClick={() => setExpandedFund(expandedFund === `${h.fund_code}:${h.tax_scheme}` ? null : `${h.fund_code}:${h.tax_scheme}`)}
                >
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-1">
                      {expandedFund === `${h.fund_code}:${h.tax_scheme}`
                        ? <ChevronUp className="h-3 w-3 text-muted-foreground shrink-0" />
                        : <ChevronDown className="h-3 w-3 text-muted-foreground shrink-0" />
                      }
                      <div>
                        <div className="font-mono text-xs font-medium">{h.fund_code}</div>
                        {h.fund_name_en && <div className="text-xs text-muted-foreground truncate max-w-[140px]">{h.fund_name_en}</div>}
                      </div>
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <span className="text-xs bg-secondary text-secondary-foreground rounded px-1.5 py-0.5">{h.tax_scheme}</span>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">{fmtNum(h.units, 4)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {h.latest_nav ? `฿${fmtNum(h.latest_nav, 4)}` : "–"}
                    {h.latest_nav_date && <div className="text-xs text-muted-foreground">{h.latest_nav_date}</div>}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums font-medium">
                    {h.market_value ? `฿${fmtNum(h.market_value)}` : "–"}
                  </td>
                  <td className={`px-3 py-2 text-right tabular-nums ${pnlClass(h.unrealized_pnl)}`}>
                    {h.unrealized_pnl ? `฿${fmtNum(h.unrealized_pnl)}` : "–"}
                  </td>
                  <td className={`px-3 py-2 text-right tabular-nums ${pnlClass(h[pnlFieldKey(pnlBasis) as keyof HoldingRow] as string | null)}`}>
                    {fmtPct(h[pnlFieldKey(pnlBasis) as keyof HoldingRow] as string | null)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-xs">
                    <div>{h.fund_entry_date ?? h.oldest_purchase_date ?? "–"}</div>
                    {h.fund_entry_date && h.oldest_purchase_date && h.fund_entry_date !== h.oldest_purchase_date && (
                      <div className="text-muted-foreground/60">({h.oldest_purchase_date})</div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-xs">
                    {(() => {
                      const entryDays = h.fund_entry_date
                        ? Math.floor((Date.now() - new Date(h.fund_entry_date).getTime()) / 86400000)
                        : null;
                      const showOriginal = entryDays !== null && h.holding_days !== null && entryDays !== h.holding_days;
                      return (
                        <>
                          <div>{fmtAge(entryDays ?? h.holding_days)}</div>
                          {showOriginal && <div className="text-muted-foreground/60">({fmtAge(h.holding_days)})</div>}
                        </>
                      );
                    })()}
                  </td>
                  {otherPortfolios.length > 0 && (
                    <td className="px-2 py-2" onClick={(e) => e.stopPropagation()}>
                      <Dialog
                        open={movingFund === `${h.fund_code}:${h.tax_scheme}`}
                        onOpenChange={(open) => { setMovingFund(open ? `${h.fund_code}:${h.tax_scheme}` : null); setMoveError(""); setMoveTarget(""); }}
                      >
                        <DialogTrigger asChild>
                          <Button variant="ghost" size="sm" className="h-7 w-7 p-0 opacity-40 hover:opacity-100">
                            <MoveRight className="h-3.5 w-3.5" />
                          </Button>
                        </DialogTrigger>
                        <DialogContent className="max-w-sm">
                          <DialogHeader>
                            <DialogTitle>Move {h.fund_code} to another portfolio</DialogTitle>
                          </DialogHeader>
                          <div className="space-y-4 pt-2">
                            <p className="text-sm text-muted-foreground">
                              All open lots and transactions for <span className="font-mono font-medium">{h.fund_code}</span> ({h.tax_scheme}) will be moved to the selected portfolio.
                            </p>
                            <Select value={moveTarget} onValueChange={setMoveTarget}>
                              <SelectTrigger>
                                <SelectValue placeholder="Select target portfolio…" />
                              </SelectTrigger>
                              <SelectContent>
                                {otherPortfolios.map((p) => (
                                  <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            {moveError && <p className="text-xs text-destructive">{moveError}</p>}
                            <div className="flex justify-end gap-2">
                              <Button variant="outline" size="sm" onClick={() => setMovingFund(null)}>Cancel</Button>
                              <Button size="sm" disabled={!moveTarget || moveLoading} onClick={() => handleMove(h.fund_code, h.tax_scheme)}>
                                {moveLoading ? "Moving…" : "Move"}
                              </Button>
                            </div>
                          </div>
                        </DialogContent>
                      </Dialog>
                    </td>
                  )}
                </tr>
                {expandedFund === `${h.fund_code}:${h.tax_scheme}` && (
                  <tr key={`${i}-chart`} className="border-b bg-muted/20">
                    <td colSpan={otherPortfolios.length > 0 ? 10 : 9} className="px-4 py-3">
                      <NavChart fundCode={h.fund_code} entryDate={h.fund_entry_date ?? undefined} />
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Allocation charts ─────────────────────────────────────────────────────────

const AMC_SHORT: Record<string, string> = {
  "ABERDEEN ASSET MANAGEMENT (THAILAND) LIMITED":           "Aberdeen",
  "AIA INVESTMENT MANAGEMENT (THAILAND) LIMITED":           "AIA",
  "ASSET PLUS FUND MANAGEMENT COMPANY LIMITED":             "ASP",
  "BANGKOK CAPITAL ASSET MANAGEMENT COMPANY LIMITED":       "BKK",
  "BBL ASSET MANAGEMENT COMPANY LIMITED":                   "BBL",
  "DAOL INVESTMENT MANAGEMENT COMPANY LIMITED":             "DAOL",
  "EASTSPRING ASSET MANAGEMENT (THAILAND) COMPANY LIMITED": "E-Spring",
  "FINANSA ASSET MANAGEMENT COMPANY LIMITED":               "Finansa",
  "FIRST PLUS ASSET MANAGEMENT (THAILAND) COMPANY LIMITED": "First",
  "KASIKORN ASSET MANAGEMENT COMPANY LIMITED":              "KBank",
  "KIATNAKIN PHATRA ASSET MANAGEMENT COMPANY LIMITED":      "KKP",
  "KRUNG THAI ASSET MANAGEMENT PUBLIC COMPANY LIMITED":     "KTB",
  "KRUNGSRI ASSET MANAGEMENT COMPANY LIMITED":              "K-Asset",
  "LAND AND HOUSES FUND MANAGEMENT COMPANY LIMITED":        "L&H",
  "MERCHANT PARTNERS ASSET MANAGEMENT LIMITED":             "Merchant",
  "MFC ASSET MANAGEMENT PUBLIC COMPANY LIMITED":            "MFC",
  "ONE ASSET MANAGEMENT LIMITED":                           "ONE",
  "PHILLIP ASSET MANAGEMENT COMPANY LIMITED":               "Phillip",
  "PRINCIPAL ASSET MANAGEMENT COMPANY LIMITED":             "CIMB",
  "RENAISSANCE FUND MANAGEMENT LIMITED":                    "Renaissance",
  "SAWAKAMI ASSET MANAGEMENT (THAILAND) COMPANY LIMITED":   "Sawakami",
  "SCB ASSET MANAGEMENT COMPANY LIMITED":                   "SCB",
  "SIAM KNIGHT FUND MANAGEMENT SECURITIES COMPANY LIMITED": "Siam Knight",
  "TALIS ASSET MANAGEMENT COMPANY LIMITED":                 "TALIS",
  "THANACHART FUND MANAGEMENT COMPANY LIMITED":             "TCAP",
  "TISCO ASSET MANAGEMENT COMPANY LIMITED":                 "TISCO",
  "TMB ASSET MANAGEMENT COMPANY LIMITED":                   "TMB",
  "UOB ASSET MANAGEMENT (THAILAND) COMPANY LIMITED":        "UOB",
  "XSPRING ASSET MANAGEMENT COMPANY LIMITED":               "XSPRING",
};

function shortenAmc(name: string): string {
  return AMC_SHORT[name] ?? name;
}

function AllocationChart({ items, title }: { items: { label: string; value: string; pct: string }[]; title: string }) {
  if (!items.length) return null;
  const data = items.map((i) => ({ name: i.label, value: Number(i.value) }));
  return (
    <div>
      <p className="text-sm font-medium mb-2">{title}</p>
      <ResponsiveContainer width="100%" height={200}>
        <PieChart>
          <Pie data={data} cx="50%" cy="50%" innerRadius={50} outerRadius={80} dataKey="value" stroke="none">
            {data.map((_, idx) => <Cell key={idx} fill={PIE_COLORS[idx % PIE_COLORS.length]} />)}
          </Pie>
          <Tooltip formatter={(val: number) => [`฿${val.toLocaleString("th-TH", { minimumFractionDigits: 2 })}`, "Value"]} />
          <Legend wrapperStyle={{ fontSize: "11px" }} />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
}

function AllocationSection({ allocation }: { allocation: AllocationResult }) {
  return (
    <div className="grid grid-cols-1 gap-6 sm:grid-cols-2">
      <AllocationChart items={allocation.by_asset_class} title="By Asset Class" />
      <AllocationChart items={allocation.by_amc.map(i => ({ ...i, label: shortenAmc(i.label) }))} title="By AMC" />
      <AllocationChart items={allocation.by_tax_scheme} title="By Tax Scheme" />
      <AllocationChart items={allocation.by_risk_level} title="By Risk Level" />
    </div>
  );
}

// ── Tax eligibility ────────────────────────────────────────────────────────────

const SCHEME_ORDER = ["RMF", "SSF", "THAI_ESG", "THAI_ESG_EXTRA", "LTF", "NORMAL"];

const SCHEME_STYLE: Record<string, { border: string; bg: string; badge: string }> = {
  RMF:            { border: "border-l-blue-500",   bg: "bg-blue-500/5",   badge: "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300" },
  SSF:            { border: "border-l-green-500",  bg: "bg-green-500/5",  badge: "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300" },
  THAI_ESG:       { border: "border-l-teal-500",   bg: "bg-teal-500/5",   badge: "bg-teal-100 text-teal-800 dark:bg-teal-900/40 dark:text-teal-300" },
  THAI_ESG_EXTRA: { border: "border-l-teal-500",   bg: "bg-teal-500/5",   badge: "bg-teal-100 text-teal-800 dark:bg-teal-900/40 dark:text-teal-300" },
  LTF:            { border: "border-l-orange-500", bg: "bg-orange-500/5", badge: "bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300" },
  NORMAL:         { border: "border-l-gray-400",   bg: "bg-gray-500/5",   badge: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300" },
};

function TaxLotsSection({ lots }: { lots: LotEligibility[] }) {
  const [collapsedSchemes, setCollapsedSchemes] = useState<Set<string>>(new Set());
  const [collapsedFunds, setCollapsedFunds] = useState<Set<string>>(new Set());

  if (!lots.length) {
    return <p className="text-sm text-muted-foreground py-6 text-center">No open tax lots.</p>;
  }

  const byScheme = new Map<string, Map<string, LotEligibility[]>>();
  for (const lot of lots) {
    if (!byScheme.has(lot.tax_scheme)) byScheme.set(lot.tax_scheme, new Map());
    const byFund = byScheme.get(lot.tax_scheme)!;
    if (!byFund.has(lot.fund_code)) byFund.set(lot.fund_code, []);
    byFund.get(lot.fund_code)!.push(lot);
  }

  const sortedSchemes = Array.from(byScheme.keys()).sort((a, b) => {
    const ai = SCHEME_ORDER.indexOf(a), bi = SCHEME_ORDER.indexOf(b);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  function toggleScheme(scheme: string) {
    setCollapsedSchemes((prev) => { const s = new Set(prev); s.has(scheme) ? s.delete(scheme) : s.add(scheme); return s; });
  }
  function toggleFund(key: string) {
    setCollapsedFunds((prev) => { const s = new Set(prev); s.has(key) ? s.delete(key) : s.add(key); return s; });
  }

  // Build all rows imperatively so every row lives in one <table> → columns align globally
  const rows: React.ReactNode[] = [];

  for (const scheme of sortedSchemes) {
    const byFund = byScheme.get(scheme)!;
    const schemeLots = Array.from(byFund.values()).flat();
    const eligibleCount = schemeLots.filter((l) => l.is_eligible).length;
    const schemeValue = schemeLots.reduce((s, l) => s + (l.market_value ? Number(l.market_value) : 0), 0);
    const style = SCHEME_STYLE[scheme] ?? SCHEME_STYLE.NORMAL;
    const isSchemeCollapsed = collapsedSchemes.has(scheme);
    const isNormal = scheme === "NORMAL";

    // ── Scheme header row ──────────────────────────────────────────────────────
    rows.push(
      <tr key={`s:${scheme}`} className={style.bg}>
        <td colSpan={8} className={`border-l-4 ${style.border} px-3 py-2`}>
          <button type="button" onClick={() => toggleScheme(scheme)}
            className="w-full flex items-center justify-between text-left gap-2">
            <div className="flex items-center gap-2 flex-wrap">
              {isSchemeCollapsed
                ? <ChevronDown className="h-4 w-4 text-muted-foreground shrink-0" />
                : <ChevronUp className="h-4 w-4 text-muted-foreground shrink-0" />}
              <span className={`text-xs font-semibold rounded px-1.5 py-0.5 ${style.badge}`}>{scheme}</span>
              <span className="text-xs text-muted-foreground">
                {byFund.size} fund{byFund.size !== 1 ? "s" : ""} · {schemeLots.length} lot{schemeLots.length !== 1 ? "s" : ""}
              </span>
              {!isNormal && (
                <span className="text-xs text-muted-foreground">
                  · <span className="text-green-600 dark:text-green-400">{eligibleCount} eligible</span>
                  {" · "}
                  <span className="text-orange-600">{schemeLots.length - eligibleCount} locked</span>
                </span>
              )}
            </div>
            <span className="text-sm font-semibold tabular-nums shrink-0">
              {schemeValue > 0 ? `฿${fmtNum(String(schemeValue))}` : "–"}
            </span>
          </button>
        </td>
      </tr>
    );

    if (isSchemeCollapsed) continue;

    for (const [fundCode, fundLots] of Array.from(byFund.entries()).sort(([a], [b]) => a.localeCompare(b))) {
      const fundKey = `${scheme}:${fundCode}`;
      const isFundCollapsed = collapsedFunds.has(fundKey);
      const fundEligible = fundLots.filter((l) => l.is_eligible).length;
      const fundValue = fundLots.reduce((s, l) => s + (l.market_value ? Number(l.market_value) : 0), 0);
      const sortedLots = [...fundLots].sort((a, b) => a.original_purchase_date.localeCompare(b.original_purchase_date));

      // ── Fund header row ──────────────────────────────────────────────────────
      rows.push(
        <tr key={`f:${fundKey}`} className="border-t bg-muted/60">
          <td colSpan={8} className="pl-7 pr-3 py-1.5">
            <button type="button" onClick={() => toggleFund(fundKey)}
              className="w-full flex items-center justify-between text-left gap-2">
              <div className="flex items-center gap-2 flex-wrap">
                {isFundCollapsed
                  ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                  : <ChevronUp className="h-3.5 w-3.5 text-muted-foreground shrink-0" />}
                <span className="font-mono text-sm font-semibold">{fundCode}</span>
                <span className="text-xs text-muted-foreground">{fundLots.length} lot{fundLots.length !== 1 ? "s" : ""}</span>
                {!isNormal && (
                  <div className="flex gap-1">
                    {fundEligible > 0 && <Badge variant="success">{fundEligible} eligible</Badge>}
                    {fundLots.length - fundEligible > 0 && <Badge variant="warning">{fundLots.length - fundEligible} locked</Badge>}
                  </div>
                )}
              </div>
              <span className="text-xs tabular-nums text-muted-foreground shrink-0">
                {fundValue > 0 ? `฿${fmtNum(String(fundValue))}` : "–"}
              </span>
            </button>
          </td>
        </tr>
      );

      if (isFundCollapsed) continue;

      // ── Lot rows ─────────────────────────────────────────────────────────────
      for (const lot of sortedLots) {
        rows.push(
          <tr key={lot.lot_id} className="border-t hover:bg-muted/20">
            <td className="pl-12 pr-2 py-1.5 text-xs">
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="text-muted-foreground select-none">└</span>
                <span className="tabular-nums">{lot.original_purchase_date}</span>
                {lot.switch_chain.length > 0 && (
                  <span className="text-muted-foreground" title={`Switch chain: ${lot.switch_chain.join(" → ")}`}>
                    · ↩ <span className="font-mono">{lot.switch_chain.join(" → ")}</span>
                  </span>
                )}
              </div>
            </td>
            <td className="px-2 py-1.5 text-right tabular-nums text-xs">{fmtNum(lot.units_remaining, 4)}</td>
            <td className="px-2 py-1.5 text-right tabular-nums text-xs">฿{fmtNum(lot.cost_basis_remaining)}</td>
            <td className="px-2 py-1.5 text-right tabular-nums text-xs">
              {lot.market_value ? `฿${fmtNum(lot.market_value)}` : "–"}
            </td>
            <td className={`px-2 py-1.5 text-right tabular-nums text-xs ${pnlClass(lot.unrealized_pnl)}`}>
              {lot.unrealized_pnl ? `฿${fmtNum(lot.unrealized_pnl)}` : "–"}
            </td>
            <td className="px-2 py-1.5 text-center">
              {isNormal
                ? <span className="text-xs text-muted-foreground">–</span>
                : lot.is_eligible
                  ? <Badge variant="success">Eligible</Badge>
                  : <Badge variant="warning">Locked</Badge>}
            </td>
            <td className="px-2 py-1.5 text-right tabular-nums text-xs">
              {isNormal ? <span className="text-muted-foreground">–</span> : (lot.eligible_date ?? "–")}
            </td>
            <td className="px-2 py-1.5 text-right tabular-nums text-xs">
              {isNormal || lot.days_remaining === 0
                ? <span className="text-muted-foreground">–</span>
                : <span className="text-orange-600 font-medium">{fmtDaysLeft(lot.days_remaining)}</span>}
            </td>
          </tr>
        );
      }
    }
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b bg-muted/50 text-muted-foreground text-xs">
            <th className="px-3 py-2 text-left font-medium">Purchase Date</th>
            <th className="px-2 py-2 text-right font-medium whitespace-nowrap">Units</th>
            <th className="px-2 py-2 text-right font-medium whitespace-nowrap">Cost Basis</th>
            <th className="px-2 py-2 text-right font-medium whitespace-nowrap">Market Value</th>
            <th className="px-2 py-2 text-right font-medium whitespace-nowrap">P&L</th>
            <th className="px-2 py-2 text-center font-medium whitespace-nowrap">Status</th>
            <th className="px-2 py-2 text-right font-medium whitespace-nowrap">Eligible Date</th>
            <th className="px-2 py-2 text-right font-medium whitespace-nowrap">Days Left</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  );
}

// ── Fund performance + risk ───────────────────────────────────────────────────

function ReturnCell({ value }: { value: string | null | undefined }) {
  if (!value) return <td className="px-2 py-2 text-right text-xs text-muted-foreground">–</td>;
  const pct = Number(value) * 100;
  const cls = pct >= 0 ? "text-green-600 dark:text-green-400" : "text-red-600 dark:text-red-400";
  return (
    <td className={`px-2 py-2 text-right tabular-nums text-xs font-medium ${cls}`}>
      {pct >= 0 ? "+" : ""}{pct.toFixed(2)}%
    </td>
  );
}

type PerfSortKey =
  | "fund_code" | "latest_nav"
  | "returns_7d" | "returns_30d" | "returns_6m" | "returns_1y" | "returns_ytd" | "returns_max"
  | "sharpe_ratio" | "max_drawdown" | "annualized_volatility";

function FundPerformanceSection({ holdings }: { holdings: HoldingRow[] }) {
  const fundCodes = Array.from(new Set(holdings.map((h) => h.fund_code)));
  const benchmarkMap: Record<string, string | null> = {};
  for (const h of holdings) benchmarkMap[h.fund_code] = h.benchmark ?? null;
  const [perf, setPerf] = useState<Record<string, FundPerformance>>({});
  const [risk, setRisk] = useState<Record<string, FundRiskMetrics>>({});
  const [loading, setLoading] = useState(true);
  const [sortKey, setSortKey] = useState<PerfSortKey>("returns_max");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  // Build since_date map from holdings (oldest lot per fund_code)
  const sinceDateMap: Record<string, string | undefined> = {};
  for (const h of holdings) {
    const cur = sinceDateMap[h.fund_code];
    if (!cur || (h.oldest_purchase_date && h.oldest_purchase_date < cur)) {
      sinceDateMap[h.fund_code] = h.oldest_purchase_date ?? undefined;
    }
  }

  const sinceDatesKey = fundCodes.map((c) => `${c}:${sinceDateMap[c] ?? ""}`).join(",");

  useEffect(() => {
    if (!fundCodes.length) { setLoading(false); return; }
    Promise.all([
      ...fundCodes.map((code) =>
        api.getFundPerformance(code, sinceDateMap[code]).then((p) => [code, p] as const).catch(() => null)
      ),
      ...fundCodes.map((code) =>
        api.getFundRiskMetrics(code).then((r) => [code, r] as const).catch(() => null)
      ),
    ]).then((results) => {
      const pm: Record<string, FundPerformance> = {};
      const rm: Record<string, FundRiskMetrics> = {};
      const half = fundCodes.length;
      results.slice(0, half).forEach((r) => { if (r) pm[r[0] as string] = r[1] as FundPerformance; });
      results.slice(half).forEach((r) => { if (r) rm[r[0] as string] = r[1] as FundRiskMetrics; });
      setPerf(pm);
      setRisk(rm);
    }).finally(() => setLoading(false));
  }, [sinceDatesKey]); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) return <p className="text-xs text-muted-foreground py-4 text-center">Loading performance data…</p>;
  if (!Object.keys(perf).length) return (
    <p className="text-xs text-muted-foreground py-4 text-center">
      No NAV history yet. Run NAV Backfill from the <a href="/dashboard/sync" className="underline">Sync page</a>.
    </p>
  );

  function getValue(code: string, key: PerfSortKey): number | null {
    const p = perf[code];
    const r = risk[code];
    if (!p) return null;
    switch (key) {
      case "fund_code":              return null; // handled as string sort below
      case "latest_nav":             return p.latest_nav ? Number(p.latest_nav) : null;
      case "returns_7d":             return p.returns_7d ? Number(p.returns_7d) : null;
      case "returns_30d":            return p.returns_30d ? Number(p.returns_30d) : null;
      case "returns_6m":             return p.returns_6m ? Number(p.returns_6m) : null;
      case "returns_1y":             return p.returns_1y ? Number(p.returns_1y) : null;
      case "returns_ytd":            return p.returns_ytd ? Number(p.returns_ytd) : null;
      case "returns_max":            return p.returns_max ? Number(p.returns_max) : null;
      case "sharpe_ratio":           return r?.sharpe_ratio ? Number(r.sharpe_ratio) : null;
      case "max_drawdown":           return r?.max_drawdown ? Number(r.max_drawdown) : null;
      case "annualized_volatility":  return r?.annualized_volatility ? Number(r.annualized_volatility) : null;
    }
  }

  const sorted = [...fundCodes].sort((a, b) => {
    if (sortKey === "fund_code") {
      return sortDir === "asc" ? a.localeCompare(b) : b.localeCompare(a);
    }
    const av = getValue(a, sortKey);
    const bv = getValue(b, sortKey);
    if (av === null && bv === null) return 0;
    if (av === null) return 1;   // nulls last
    if (bv === null) return -1;
    return sortDir === "asc" ? av - bv : bv - av;
  });

  function toggleSort(key: PerfSortKey) {
    if (sortKey === key) setSortDir((d) => d === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir("desc"); }
  }

  function SortIcon({ col }: { col: PerfSortKey }) {
    if (sortKey !== col) return <ArrowUpDown className="h-3 w-3 ml-1 opacity-30 inline" />;
    return sortDir === "asc"
      ? <ArrowUp className="h-3 w-3 ml-1 inline" />
      : <ArrowDown className="h-3 w-3 ml-1 inline" />;
  }

  function Th({ col, children, right, title }: { col: PerfSortKey; children: React.ReactNode; right?: boolean; title?: string }) {
    return (
      <th
        title={title}
        onClick={() => toggleSort(col)}
        className={`px-2 py-2 font-medium cursor-pointer select-none hover:text-foreground whitespace-nowrap ${right ? "text-right" : "text-left"}`}
      >
        {children}<SortIcon col={col} />
      </th>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b bg-muted/50 text-muted-foreground text-xs">
            <Th col="fund_code">Fund</Th>
            <Th col="latest_nav" right>Latest NAV</Th>
            <Th col="returns_7d" right>7D</Th>
            <Th col="returns_30d" right>30D</Th>
            <Th col="returns_6m" right>6M</Th>
            <Th col="returns_1y" right>1Y</Th>
            <Th col="returns_ytd" right>YTD</Th>
            <Th col="returns_max" right title="Since first current holding date">MAX*</Th>
            <Th col="sharpe_ratio" right>Sharpe</Th>
            <Th col="max_drawdown" right>MaxDD</Th>
            <Th col="annualized_volatility" right>Volatility</Th>
            <th className="px-2 py-2 font-medium text-left whitespace-nowrap">Benchmark</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((code) => {
            const p = perf[code];
            const r = risk[code];
            if (!p) return null;
            return (
              <tr key={code} className="border-b hover:bg-muted/30">
                <td className="px-2 py-2 font-mono text-xs font-medium">{code}</td>
                <td className="px-2 py-2 text-right tabular-nums text-xs">
                  {p.latest_nav ? `฿${Number(p.latest_nav).toFixed(4)}` : "–"}
                  {p.latest_nav_date && <div className="text-muted-foreground">{p.latest_nav_date}</div>}
                </td>
                <ReturnCell value={p.returns_7d} />
                <ReturnCell value={p.returns_30d} />
                <ReturnCell value={p.returns_6m} />
                <ReturnCell value={p.returns_1y} />
                <ReturnCell value={p.returns_ytd} />
                <ReturnCell value={p.returns_max} />
                <td className="px-2 py-2 text-right tabular-nums text-xs">
                  {r?.sharpe_ratio ? Number(r.sharpe_ratio).toFixed(2) : "–"}
                </td>
                <td className={`px-2 py-2 text-right tabular-nums text-xs ${r?.max_drawdown ? "text-red-600 dark:text-red-400" : ""}`}>
                  {r?.max_drawdown ? `${(Number(r.max_drawdown) * 100).toFixed(1)}%` : "–"}
                </td>
                <td className="px-2 py-2 text-right tabular-nums text-xs">
                  {r?.annualized_volatility ? `${(Number(r.annualized_volatility) * 100).toFixed(1)}%` : "–"}
                </td>
                <td className="px-2 py-2 text-xs text-muted-foreground max-w-[160px] truncate" title={benchmarkMap[code] ?? undefined}>
                  {benchmarkMap[code] ?? "–"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="text-xs text-muted-foreground px-3 py-1">* MAX คำนวณจากวันที่ซื้อล็อตปัจจุบัน (ไม่นับล็อตที่ขายไปแล้ว)</p>
    </div>
  );
}

// ── Fund code search input ────────────────────────────────────────────────────

function FundSearchInput({ value, onChange }: { value: string; onChange: (code: string) => void }) {
  const [query, setQuery] = useState(value);
  const [results, setResults] = useState<FundResult[]>([]);
  const [open, setOpen] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => { setQuery(value); }, [value]);

  useEffect(() => {
    function onMouseDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, []);

  function handleChange(e: React.ChangeEvent<HTMLInputElement>) {
    const q = e.target.value.toUpperCase();
    setQuery(q);
    onChange(q);
    clearTimeout(timer.current);
    if (q.length < 2) { setResults([]); setOpen(false); return; }
    timer.current = setTimeout(async () => {
      try {
        const r = await api.searchFunds(q);
        setResults(r);
        setOpen(r.length > 0);
      } catch { /* ignore */ }
    }, 300);
  }

  function select(fund: FundResult) {
    setQuery(fund.fund_code);
    onChange(fund.fund_code);
    setResults([]);
    setOpen(false);
  }

  return (
    <div ref={containerRef} className="relative">
      <Input value={query} onChange={handleChange} placeholder="Type to search SEC funds…" autoComplete="off" />
      {open && (
        <div className="absolute z-50 w-full mt-1 max-h-52 overflow-y-auto rounded-md border bg-background shadow-lg">
          {results.map((f) => (
            <button key={f.fund_code} type="button"
              className="w-full px-3 py-2 text-left hover:bg-muted flex flex-col gap-0.5"
              onClick={() => select(f)}>
              <span className="font-mono text-xs font-semibold">{f.fund_code}</span>
              {(f.name_en || f.name_th) && <span className="text-xs text-muted-foreground truncate">{f.name_en ?? f.name_th}</span>}
              {f.amc && <span className="text-xs text-muted-foreground">{f.amc}{f.risk_level != null ? ` · Risk ${f.risk_level}` : ""}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function PortfolioPage() {
  const router = useRouter();
  const params = useParams();
  const portfolioId = params.id as string;

  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [holdings, setHoldings] = useState<HoldingRow[]>([]);
  const openHoldings = holdings.filter((h) => Number(h.units) > 0);
  const [pnlBasis, setPnlBasis] = useState<PnlBasis>(() => derivePnlBasis(loadSettings()));
  const [allocation, setAllocation] = useState<AllocationResult | null>(null);
  const [taxLots, setTaxLots] = useState<LotEligibility[]>([]);
  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);

  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [txToDelete, setTxToDelete] = useState<Transaction | null>(null);
  const [deletingTx, setDeletingTx] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [importResult, setImportResult] = useState<{ imported: number; errors: string[] } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const [form, setForm] = useState<TransactionCreate>({
    date: new Date().toISOString().slice(0, 10),
    type: "BUY",
    fund_code: "",
    units: "",
    nav: "",
    amount: "",
    fee: "0",
    tax_withheld: "0",
    tax_scheme: "NORMAL",
    note: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");

  useEffect(() => {
    setPnlBasis(derivePnlBasis(loadSettings()));
    // Re-sync when settings change in another tab or the settings page
    const onStorage = () => setPnlBasis(derivePnlBasis(loadSettings()));
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => {
    if (!localStorage.getItem("token")) { router.replace("/login"); return; }
    const load = async () => {
      try {
        const [txs, sum, hold, alloc, lots, pfolios] = await Promise.all([
          api.listTransactions(portfolioId),
          api.getPortfolioSummary(portfolioId).catch(() => null),
          api.getPortfolioHoldings(portfolioId).catch(() => []),
          api.getPortfolioAllocation(portfolioId).catch(() => null),
          api.getTaxEligibility(portfolioId).catch(() => []),
          api.listPortfolios().catch(() => []),
        ]);
        setTransactions(txs);
        setSummary(sum);
        setHoldings(hold as HoldingRow[]);
        setAllocation(alloc);
        setTaxLots(lots as LotEligibility[]);
        setPortfolios(pfolios);
      } catch { setError("Failed to load portfolio data"); }
      finally { setLoading(false); }
    };
    load();
  }, [portfolioId, router]);

  useEffect(() => {
    if (!["BUY", "SELL"].includes(form.type)) return;
    const u = parseFloat(form.units ?? "");
    const n = parseFloat(form.nav ?? "");
    if (!isNaN(u) && !isNaN(n) && u > 0 && n > 0) {
      setForm((prev) => ({ ...prev, amount: (u * n).toFixed(2) }));
    }
  }, [form.units, form.nav, form.type]);

  async function reloadAnalytics() {
    const [sum, hold, alloc, lots] = await Promise.all([
      api.getPortfolioSummary(portfolioId).catch(() => null),
      api.getPortfolioHoldings(portfolioId).catch(() => []),
      api.getPortfolioAllocation(portfolioId).catch(() => null),
      api.getTaxEligibility(portfolioId).catch(() => []),
    ]);
    setSummary(sum);
    setHoldings(hold as HoldingRow[]);
    setAllocation(alloc);
    setTaxLots(lots as LotEligibility[]);
  }

  async function handleDelete() {
    setDeleting(true);
    try {
      await api.deletePortfolio(portfolioId);
      router.replace("/dashboard");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Delete failed");
      setDeleteConfirm(false);
    } finally { setDeleting(false); }
  }

  async function handleRefresh() {
    setRefreshing(true);
    try {
      await api.refreshAnalytics(portfolioId);
      await reloadAnalytics();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Refresh failed");
    } finally { setRefreshing(false); }
  }

  async function handleDeleteTx() {
    if (!txToDelete) return;
    setDeletingTx(true);
    try {
      await api.deleteTransaction(portfolioId, txToDelete.id);
      setTransactions((prev) => prev.filter((t) => {
        if (t.id === txToDelete.id) return false;
        if (txToDelete.pair_id !== null && t.pair_id === txToDelete.pair_id) return false;
        return true;
      }));
      setTxToDelete(null);
      await reloadAnalytics();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Delete failed");
      setTxToDelete(null);
    } finally { setDeletingTx(false); }
  }

  const needsUnitsNav = ["BUY", "SELL"].includes(form.type);
  const needsFund = !["INTEREST"].includes(form.type);

  async function handleAddTx(e: React.FormEvent) {
    e.preventDefault();
    setFormError("");
    setSubmitting(true);
    try {
      const payload: TransactionCreate = {
        ...form,
        units: needsUnitsNav && form.units ? form.units : undefined,
        nav: needsUnitsNav && form.nav ? form.nav : undefined,
        fund_code: needsFund && form.fund_code ? form.fund_code : undefined,
      };
      const tx = await api.addTransaction(portfolioId, payload);
      setTransactions((prev) => [tx, ...prev]);
      setDialogOpen(false);
      await reloadAnalytics();
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : "Failed to add transaction");
    } finally { setSubmitting(false); }
  }

  async function handleImport(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const result = await api.importCsv(portfolioId, file);
      setImportResult(result);
      if (result.imported > 0) {
        const txs = await api.listTransactions(portfolioId);
        setTransactions(txs);
        await reloadAnalytics();
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Import failed");
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <div className="min-h-screen bg-muted/40">
      <header className="border-b bg-background px-4 py-3 flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={() => router.push("/dashboard")}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <h1 className="font-semibold">Portfolio</h1>
        <div className="ml-auto flex items-center gap-1">
          <ThemeToggle />
          <Button variant="ghost" size="sm" onClick={() => router.push("/dashboard/settings")} title="Settings">
            <Settings className="h-4 w-4" />
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm">
                <Upload className="h-4 w-4 sm:mr-1" />
                <span className="hidden sm:inline">Import</span>
                <ChevronDown className="h-3 w-3 ml-0.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onClick={() => fileRef.current?.click()}>
                <Upload className="h-4 w-4" />
                Import CSV
              </DropdownMenuItem>
              <DropdownMenuItem onClick={downloadTemplate}>
                <Download className="h-4 w-4" />
                Download Template
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <input ref={fileRef} type="file" accept=".csv" className="hidden" onChange={handleImport} />
          <Button variant="ghost" size="sm" onClick={() => setDeleteConfirm(true)} title="Delete portfolio">
            <Trash2 className="h-4 w-4 text-destructive" />
          </Button>
          <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
            <DialogTrigger asChild>
              <Button size="sm">
                <PlusCircle className="h-4 w-4 sm:mr-1" />
                <span className="hidden sm:inline">Add Transaction</span>
              </Button>
            </DialogTrigger>
            <DialogContent className="max-w-lg max-h-[90vh] overflow-y-auto">
              <DialogHeader><DialogTitle>Add Transaction</DialogTitle></DialogHeader>
              <form onSubmit={handleAddTx} className="space-y-3 mt-2">
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1">
                    <Label>Date</Label>
                    <Input type="date" value={form.date} onChange={(e) => setForm({ ...form, date: e.target.value })} required />
                  </div>
                  <div className="space-y-1">
                    <Label>Type</Label>
                    <Select value={form.type} onValueChange={(v) => setForm({ ...form, type: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>{TX_TYPES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}</SelectContent>
                    </Select>
                  </div>
                </div>
                {needsFund && (
                  <div className="space-y-1">
                    <Label>Fund Code</Label>
                    <FundSearchInput value={form.fund_code ?? ""} onChange={(code) => setForm((f) => ({ ...f, fund_code: code }))} />
                    <p className="text-xs text-muted-foreground">Search by code or name. Set Tax Scheme below for SSF/RMF variants.</p>
                  </div>
                )}
                {needsUnitsNav && (
                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <Label>Units</Label>
                      <Input type="number" step="any" placeholder="0.00" value={form.units} onChange={(e) => setForm({ ...form, units: e.target.value })} />
                    </div>
                    <div className="space-y-1">
                      <Label>NAV</Label>
                      <Input type="number" step="any" placeholder="0.0000" value={form.nav} onChange={(e) => setForm({ ...form, nav: e.target.value })} />
                    </div>
                  </div>
                )}
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                  <div className="space-y-1">
                    <Label>Amount (฿) {needsUnitsNav && <span className="text-xs text-muted-foreground font-normal">auto</span>}</Label>
                    <Input type="number" step="any" placeholder="0.00" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} required />
                  </div>
                  <div className="space-y-1">
                    <Label>Fee (฿)</Label>
                    <Input type="number" step="any" placeholder="0" value={form.fee} onChange={(e) => setForm({ ...form, fee: e.target.value })} />
                  </div>
                  <div className="space-y-1">
                    <Label>Tax Withheld (฿)</Label>
                    <Input type="number" step="any" placeholder="0" value={form.tax_withheld} onChange={(e) => setForm({ ...form, tax_withheld: e.target.value })} />
                  </div>
                </div>
                <div className="space-y-1">
                  <Label>Tax Scheme</Label>
                  <Select value={form.tax_scheme} onValueChange={(v) => setForm({ ...form, tax_scheme: v })}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>{SCHEMES.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}</SelectContent>
                  </Select>
                </div>
                <div className="space-y-1">
                  <Label>Note</Label>
                  <Input placeholder="Optional note" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
                </div>
                {formError && <p className="text-sm text-destructive">{formError}</p>}
                <Button type="submit" className="w-full" disabled={submitting}>{submitting ? "Saving…" : "Add Transaction"}</Button>
              </form>
            </DialogContent>
          </Dialog>
        </div>
      </header>

      <main className="max-w-5xl mx-auto p-4 space-y-4">
        {error && <p className="text-sm text-destructive">{error}</p>}
        {importResult && (
          <Card>
            <CardContent className="pt-4">
              <p className="text-sm font-medium text-green-700 dark:text-green-400">Imported {importResult.imported} transactions</p>
              {importResult.errors.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {importResult.errors.map((e, i) => <li key={i} className="text-xs text-destructive">{e}</li>)}
                </ul>
              )}
            </CardContent>
          </Card>
        )}

        {loading ? (
          <p className="text-sm text-muted-foreground py-8 text-center">Loading…</p>
        ) : (
          <>
            {summary && <SummarySection summary={summary} />}
            <AiSummarySection portfolioId={portfolioId} />

            <Tabs defaultValue="overview">
              <div className="overflow-x-auto">
                <TabsList className="w-max min-w-full sm:w-auto">
                  <TabsTrigger value="overview">Holdings</TabsTrigger>
                  <TabsTrigger value="performance">Performance</TabsTrigger>
                  <TabsTrigger value="allocation">Allocation</TabsTrigger>
                  <TabsTrigger value="tax">Tax Lots</TabsTrigger>
                  <TabsTrigger value="transactions">Transactions</TabsTrigger>
                </TabsList>
              </div>

              <TabsContent value="overview" className="space-y-3">
                <Card>
                  <CardHeader><CardTitle className="text-base">Open Positions ({openHoldings.length})</CardTitle></CardHeader>
                  <CardContent className="p-0">
                    <HoldingsSection holdings={openHoldings} portfolioId={portfolioId} portfolios={portfolios} onTransferred={() => { api.getPortfolioHoldings(portfolioId).then(h => setHoldings(h as HoldingRow[])); }} pnlBasis={pnlBasis} />
                  </CardContent>
                </Card>
                <DividendSummarySection holdings={openHoldings} />
              </TabsContent>

              <TabsContent value="performance" className="space-y-3">
                {openHoldings.length > 0 && <PnlRankingChart holdings={openHoldings} pnlBasis={pnlBasis} />}
                {openHoldings.length > 0 && (
                  <Card>
                    <CardHeader><CardTitle className="text-base">Fund Performance & Risk Metrics</CardTitle></CardHeader>
                    <CardContent className="p-0">
                      <FundPerformanceSection holdings={openHoldings} />
                    </CardContent>
                  </Card>
                )}
              </TabsContent>

              <TabsContent value="allocation">
                <Card>
                  <CardHeader><CardTitle className="text-base">Asset Allocation</CardTitle></CardHeader>
                  <CardContent>
                    {allocation ? <AllocationSection allocation={allocation} /> : <p className="text-sm text-muted-foreground py-4 text-center">No allocation data.</p>}
                  </CardContent>
                </Card>
              </TabsContent>

              <TabsContent value="tax">
                <Card>
                  <CardHeader><CardTitle className="text-base">Tax-Advantaged Lot Eligibility</CardTitle></CardHeader>
                  <CardContent className="p-0"><TaxLotsSection lots={taxLots} /></CardContent>
                </Card>
              </TabsContent>

              <TabsContent value="transactions">
                <Card>
                  <CardHeader><CardTitle className="text-base">Transactions ({transactions.length})</CardTitle></CardHeader>
                  <CardContent className="p-0">
                    {transactions.length === 0 ? (
                      <p className="p-4 text-sm text-muted-foreground">No transactions yet.</p>
                    ) : (
                      <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                          <thead>
                            <tr className="border-b bg-muted/50">
                              <th className="px-3 py-2 text-left font-medium">Date</th>
                              <th className="px-3 py-2 text-left font-medium">Type</th>
                              <th className="px-3 py-2 text-left font-medium">Fund</th>
                              <th className="px-3 py-2 text-right font-medium">Units</th>
                              <th className="px-3 py-2 text-right font-medium">NAV</th>
                              <th className="px-3 py-2 text-right font-medium">Amount (฿)</th>
                              <th className="px-3 py-2 text-left font-medium">Scheme</th>
                              <th className="px-3 py-2"></th>
                            </tr>
                          </thead>
                          <tbody>
                            {transactions.map((tx) => (
                              <tr key={tx.id} className="border-b hover:bg-muted/30">
                                <td className="px-3 py-2 tabular-nums">{tx.date}</td>
                                <td className="px-3 py-2">
                                  <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${TYPE_BADGE[tx.type] ?? "bg-gray-100 text-gray-800"}`}>{tx.type}</span>
                                </td>
                                <td className="px-3 py-2 font-mono text-xs">{tx.fund_code ?? "–"}</td>
                                <td className="px-3 py-2 text-right tabular-nums">{tx.units ? fmtNum(tx.units, 4) : "–"}</td>
                                <td className="px-3 py-2 text-right tabular-nums">{tx.nav ? fmtNum(tx.nav, 4) : "–"}</td>
                                <td className="px-3 py-2 text-right tabular-nums font-medium">{fmtNum(tx.amount)}</td>
                                <td className="px-3 py-2 text-xs text-muted-foreground">{tx.tax_scheme}</td>
                                <td className="px-3 py-2">
                                  <button onClick={() => setTxToDelete(tx)} className="text-muted-foreground hover:text-destructive transition-colors" title="Delete transaction">
                                    <X className="h-3.5 w-3.5" />
                                  </button>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </CardContent>
                </Card>
              </TabsContent>
            </Tabs>
          </>
        )}
      </main>

      {/* Delete transaction dialog */}
      <Dialog open={!!txToDelete} onOpenChange={(open) => !open && setTxToDelete(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader><DialogTitle>Delete Transaction</DialogTitle></DialogHeader>
          {txToDelete && (() => {
            const isLotMutating = ["BUY", "SELL", "SWITCH_OUT", "SWITCH_IN"].includes(txToDelete.type);
            const isSwitch = ["SWITCH_OUT", "SWITCH_IN"].includes(txToDelete.type);
            return (
              <>
                <p className="text-sm text-muted-foreground">
                  {isSwitch ? "Both legs of this switch (SWITCH_OUT + SWITCH_IN) will be deleted." : `Delete this ${txToDelete.type} on ${txToDelete.date}?`}
                </p>
                {isLotMutating && (
                  <p className="text-sm text-yellow-700 dark:text-yellow-400 bg-yellow-50 dark:bg-yellow-900/20 rounded px-3 py-2 mt-1">
                    All tax lots will be recalculated from scratch.
                  </p>
                )}
                <div className="flex justify-end gap-2 mt-2">
                  <Button variant="outline" size="sm" onClick={() => setTxToDelete(null)}>Cancel</Button>
                  <Button variant="destructive" size="sm" disabled={deletingTx} onClick={handleDeleteTx}>
                    {deletingTx ? "Deleting…" : "Delete"}
                  </Button>
                </div>
              </>
            );
          })()}
        </DialogContent>
      </Dialog>

      {/* Delete portfolio dialog */}
      <Dialog open={deleteConfirm} onOpenChange={(open) => !open && setDeleteConfirm(false)}>
        <DialogContent className="max-w-sm">
          <DialogHeader><DialogTitle>Delete Portfolio</DialogTitle></DialogHeader>
          <p className="text-sm text-muted-foreground">This will permanently delete the portfolio and all its data. This cannot be undone.</p>
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" size="sm" onClick={() => setDeleteConfirm(false)}>Cancel</Button>
            <Button variant="destructive" size="sm" disabled={deleting} onClick={handleDelete}>
              {deleting ? "Deleting…" : "Delete Portfolio"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
