"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, type SyncJob } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { ArrowLeft, RefreshCw, Database, TrendingUp, Landmark, Loader2, CheckCircle2, XCircle } from "lucide-react";

// Maps each action key to matching job types from the backend
const JOB_MATCH: Record<string, (t: string) => boolean> = {
  funds:     (t) => t === "fund_metadata",
  nav:       (t) => t.startsWith("nav_sync:"),
  backfill:  (t) => t.startsWith("nav_backfill:"),
  dividends: (t) => t === "dividend_sync",
};

function StatusBadge({ status }: { status: string }) {
  if (status === "success") return <Badge variant="success">Success</Badge>;
  if (status === "running") return <Badge variant="warning">Running…</Badge>;
  if (status === "error") return <Badge variant="destructive">Error</Badge>;
  return <Badge variant="outline">{status}</Badge>;
}

function fmtDate(s: string | null) {
  if (!s) return "–";
  return new Date(s).toLocaleString("th-TH", { timeZone: "Asia/Bangkok" });
}

function IndeterminateBar() {
  return (
    <div className="mt-2 h-1 rounded-full bg-muted overflow-hidden">
      <div
        className="h-full w-1/3 rounded-full bg-primary"
        style={{ animation: "indeterminate 1.5s ease-in-out infinite" }}
      />
    </div>
  );
}

export default function SyncPage() {
  const router = useRouter();
  const [jobs, setJobs] = useState<SyncJob[]>([]);
  const [navDate, setNavDate] = useState(new Date().toISOString().slice(0, 10));
  const threeMonthsAgo = new Date(); threeMonthsAgo.setMonth(threeMonthsAgo.getMonth() - 3);
  const [backfillStart, setBackfillStart] = useState(threeMonthsAgo.toISOString().slice(0, 10));
  const [backfillEnd, setBackfillEnd] = useState(new Date().toISOString().slice(0, 10));
  const [backfillPortfolioOnly, setBackfillPortfolioOnly] = useState(true);
  const [messages, setMessages] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  // Track when each action was last triggered so we can match its jobs
  const [lastRunAt, setLastRunAt] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!localStorage.getItem("token")) {
      router.replace("/login");
      return;
    }
    refreshJobs();
  }, [router]);

  async function refreshJobs() {
    try {
      const j = await api.listSyncJobs();
      setJobs(j);
    } catch {
      // non-admin: silently ignore
    }
  }

  // Auto-poll every 3s while any job is running
  useEffect(() => {
    const hasRunning = jobs.some((j) => j.status === "running");
    if (!hasRunning) return;
    const id = setInterval(refreshJobs, 3000);
    return () => clearInterval(id);
  }, [jobs]); // eslint-disable-line react-hooks/exhaustive-deps

  async function trigger(key: string, fn: () => Promise<{ status: string; message?: string; date?: string }>) {
    const runAt = new Date().toISOString();
    setLastRunAt((r) => ({ ...r, [key]: runAt }));
    setLoading((l) => ({ ...l, [key]: true }));
    setMessages((m) => ({ ...m, [key]: "" }));
    try {
      await fn();
      setTimeout(refreshJobs, 1000);
    } catch (err: unknown) {
      setMessages((m) => ({ ...m, [key]: err instanceof Error ? err.message : "Failed" }));
    } finally {
      setLoading((l) => ({ ...l, [key]: false }));
    }
  }

  /** Derive per-card status from the jobs list. */
  function cardStatus(key: string): "running" | "success" | "error" | null {
    const matcher = JOB_MATCH[key];
    if (!matcher) return null;
    const since = lastRunAt[key];
    if (!since) return null;
    // Find jobs started on or after the last trigger for this action
    const matching = jobs.filter((j) => matcher(j.type) && j.started_at >= since);
    if (matching.length === 0) return null;
    // If any is still running, the overall operation is running
    if (matching.some((j) => j.status === "running")) return "running";
    // Most recent job determines final state (jobs sorted desc)
    const latest = matching[0];
    return latest.status === "success" ? "success" : latest.status === "error" ? "error" : null;
  }

  const syncActions = [
    {
      key: "funds",
      icon: <Database className="h-5 w-5" />,
      title: "Fund Metadata",
      description: "Sync fund names, AMC, asset class, risk level from SEC Factsheet API. Run this first before NAV sync.",
      action: () => trigger("funds", api.syncFunds),
    },
    {
      key: "nav",
      icon: <TrendingUp className="h-5 w-5" />,
      title: "NAV History",
      description: "Sync daily NAV for your portfolio funds for the selected date.",
      action: () => trigger("nav", () => api.syncNav(navDate)),
      extra: (
        <div className="flex items-center gap-2 mt-2">
          <Input
            type="date"
            value={navDate}
            onChange={(e) => setNavDate(e.target.value)}
            className="h-8 w-40 text-sm"
          />
          <span className="text-xs text-muted-foreground">Defaults to today</span>
        </div>
      ),
    },
    {
      key: "backfill",
      icon: <TrendingUp className="h-5 w-5" />,
      title: "NAV Backfill",
      description: "Load historical NAV for your portfolio funds over a date range. Required to see 7D/30D/6M/1Y/MAX returns. Skips weekends.",
      action: () => trigger("backfill", () => api.syncNavBackfill(backfillStart, backfillEnd, backfillPortfolioOnly)),
      extra: (
        <div className="mt-2 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <div className="flex items-center gap-1">
              <span className="text-xs text-muted-foreground">From</span>
              <Input
                type="date"
                value={backfillStart}
                onChange={(e) => setBackfillStart(e.target.value)}
                className="h-8 w-36 text-sm"
              />
            </div>
            <div className="flex items-center gap-1">
              <span className="text-xs text-muted-foreground">To</span>
              <Input
                type="date"
                value={backfillEnd}
                onChange={(e) => setBackfillEnd(e.target.value)}
                className="h-8 w-36 text-sm"
              />
            </div>
          </div>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={backfillPortfolioOnly}
              onChange={(e) => setBackfillPortfolioOnly(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            <span className="text-xs text-muted-foreground">
              Portfolio funds only <span className="text-green-600 dark:text-green-400 font-medium">(recommended — much faster)</span>
            </span>
          </label>
        </div>
      ),
    },
    {
      key: "dividends",
      icon: <Landmark className="h-5 w-5" />,
      title: "Dividends",
      description: "Sync dividend history for your portfolio funds from SEC API.",
      action: () => trigger("dividends", api.syncDividends),
    },
  ];

  return (
    <div className="min-h-screen bg-muted/40">
      <header className="border-b bg-background px-4 py-3 flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={() => router.push("/dashboard")}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <h1 className="font-semibold">Data Sync</h1>
        <div className="ml-auto flex items-center gap-1">
          <ThemeToggle />
          <Button variant="ghost" size="sm" onClick={refreshJobs}>
            <RefreshCw className="h-4 w-4" />
          </Button>
        </div>
      </header>

      <main className="max-w-3xl mx-auto p-4 space-y-6">
        <div className="space-y-3">
          <h2 className="text-sm font-medium text-muted-foreground uppercase tracking-wide">Sync Controls</h2>
          {syncActions.map((s) => {
            const status = cardStatus(s.key);
            return (
              <Card key={s.key}>
                <CardContent className="pt-4 pb-4">
                  <div className="flex items-start gap-3">
                    <div className="mt-0.5 text-muted-foreground">{s.icon}</div>
                    <div className="flex-1 min-w-0">
                      <p className="font-medium text-sm">{s.title}</p>
                      <p className="text-xs text-muted-foreground mt-0.5">{s.description}</p>
                      {s.extra}

                      {/* Progress / status indicator */}
                      {status === "running" && <IndeterminateBar />}
                      {status === "running" && (
                        <p className="text-xs text-muted-foreground mt-1 flex items-center gap-1">
                          <Loader2 className="h-3 w-3 animate-spin" /> Running…
                        </p>
                      )}
                      {status === "success" && (
                        <p className="text-xs text-green-600 dark:text-green-400 mt-2 flex items-center gap-1">
                          <CheckCircle2 className="h-3.5 w-3.5" /> Completed
                        </p>
                      )}
                      {status === "error" && (
                        <p className="text-xs text-destructive mt-2 flex items-center gap-1">
                          <XCircle className="h-3.5 w-3.5" /> Failed — see Recent Jobs below
                        </p>
                      )}
                      {messages[s.key] && status === null && (
                        <p className="text-xs mt-2 text-destructive">{messages[s.key]}</p>
                      )}
                    </div>
                    <Button
                      size="sm"
                      disabled={loading[s.key] || status === "running"}
                      onClick={s.action}
                      className="shrink-0"
                    >
                      {loading[s.key] || status === "running" ? (
                        <><Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />Running</>
                      ) : "Run"}
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>

        <div className="space-y-3">
          <h2 className="text-sm font-medium text-muted-foreground uppercase tracking-wide">Recent Jobs</h2>
          {jobs.length === 0 ? (
            <p className="text-sm text-muted-foreground">No sync jobs yet.</p>
          ) : (
            <Card>
              <CardContent className="p-0">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b bg-muted/50">
                        <th className="px-4 py-2 text-left font-medium">Type</th>
                        <th className="px-4 py-2 text-left font-medium">Status</th>
                        <th className="px-4 py-2 text-left font-medium">Started</th>
                        <th className="px-4 py-2 text-left font-medium">Completed</th>
                        <th className="px-4 py-2 text-left font-medium">Notes</th>
                      </tr>
                    </thead>
                    <tbody>
                      {jobs.map((j) => (
                        <tr key={j.id} className="border-b hover:bg-muted/30">
                          <td className="px-4 py-2 font-mono text-xs flex items-center gap-1">
                            {j.status === "running" && <Loader2 className="h-3 w-3 animate-spin shrink-0" />}
                            {j.type}
                          </td>
                          <td className="px-4 py-2"><StatusBadge status={j.status} /></td>
                          <td className="px-4 py-2 text-xs tabular-nums">{fmtDate(j.started_at)}</td>
                          <td className="px-4 py-2 text-xs tabular-nums">{fmtDate(j.completed_at)}</td>
                          <td className="px-4 py-2 text-xs text-muted-foreground max-w-xs truncate">{j.error_message ?? "–"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      </main>
    </div>
  );
}
