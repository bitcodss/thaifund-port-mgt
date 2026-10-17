"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, type MonthlyReport, type ReportListItem } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { ArrowLeft, Loader2 } from "lucide-react";

const monthLabel = (m: string) => new Date(m).toLocaleDateString("th-TH", { month: "long", year: "numeric" });

export default function ReportsPage() {
  const router = useRouter();
  const [list, setList] = useState<ReportListItem[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<MonthlyReport | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = () =>
    api.listReports().then((l) => {
      setList(l);
      if (l.length && !selected) setSelected(l[0].month);
    }).catch((e) => setError(e instanceof Error ? e.message : "Load failed"));
  useEffect(() => {
    load();
    api.getMe().then((u) => setIsAdmin(u.role === "admin")).catch(() => {});
  }, []);
  useEffect(() => {
    if (!selected) return;
    setReport(null);
    api.getReport(selected).then(setReport).catch((e) => setError(e instanceof Error ? e.message : "Load failed"));
  }, [selected]);

  async function build() {
    setMessage("");
    try {
      const r = await api.buildReports();
      setMessage(`Building the report for ${monthLabel(r.month)} in the background — refresh in a few minutes.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Build failed");
    }
  }

  return (
    <div className="min-h-screen bg-muted/40">
      <header className="border-b bg-background px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => router.push("/dashboard")}><ArrowLeft className="h-4 w-4" /></Button>
          <h1 className="font-semibold text-lg">Monthly reports</h1>
        </div>
        <ThemeToggle />
      </header>
      <main className="mx-auto max-w-4xl p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {list?.map((r) => (
            <Button key={r.month} size="sm" variant={r.month === selected ? "default" : "outline"}
              onClick={() => setSelected(r.month)}>
              {monthLabel(r.month)}
              {r.status !== "ok" && <Badge variant="warning" className="ml-1 text-[10px]">{r.status}</Badge>}
            </Button>
          ))}
          {list && list.length === 0 && <p className="text-sm text-muted-foreground">No reports yet.</p>}
          {isAdmin && <Button size="sm" variant="secondary" onClick={build}>Build last month</Button>}
          <Button size="sm" variant="ghost" onClick={() => { setSelected(null); load(); }}>Refresh</Button>
        </div>
        {message && <p className="text-sm text-muted-foreground">{message}</p>}
        {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
        {selected && !report && !error && <Loader2 className="h-5 w-5 animate-spin" />}
        {report && (
          <Card>
            <CardContent className="p-0">
              {isAdmin && report.errors.length > 0 && (
                <details className="px-4 pt-3 text-xs text-muted-foreground">
                  <summary>{report.errors.length} section(s) used the automatic template</summary>
                  <ul className="list-disc pl-5">{report.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
                </details>
              )}
              {/* Report HTML is rendered by our backend from escaped text; sandboxed anyway. */}
              <iframe title="Monthly report" sandbox="" srcDoc={report.html ?? ""}
                className="w-full min-h-[80vh] rounded-md bg-white" />
            </CardContent>
          </Card>
        )}
      </main>
    </div>
  );
}
