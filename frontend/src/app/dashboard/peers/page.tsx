"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, type PeerGroup, type PendingCategory } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { ArrowLeft, ChevronDown, ChevronUp, Loader2 } from "lucide-react";

const HEDGE_LABEL: Record<string, string> = {
  unhedged: "Unhedged", full: "Fully hedged", dynamic: "Dynamic", partial: "Partial",
  domestic: "Domestic", unknown: "Hedge ?",
};

function GroupCard({ g, themes, isAdmin, onChanged }: {
  g: PeerGroup; themes: Record<string, string[]>; isAdmin: boolean; onChanged: (g: PeerGroup) => void;
}) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const allowed = g.aimc_category ? themes[g.aimc_category] : undefined;
  const masters = g.masters ?? [];

  async function save(theme: string | null) {
    setSaving(true);
    try { onChanged({ ...g, ...(await api.setPeerTheme(g.sec_proj_id, theme)) }); } finally { setSaving(false); }
  }

  return (
    <Card>
      <CardContent className="p-4 space-y-2 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono font-semibold">{(g.held_codes ?? [g.sec_proj_id]).join(", ")}</span>
          <span className="text-muted-foreground text-xs">{g.sec_proj_id}</span>
        </div>
        {!g.aimc_category ? (
          <p className="text-muted-foreground text-xs">No AIMC category — not compared with peers.</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <span>{g.aimc_category}</span>
              {g.aimc_source === "inferred" && (
                <Badge variant="warning" className="text-[10px]" title={g.aimc_reason ?? undefined}>Inferred</Badge>
              )}
              <span className="text-muted-foreground">›</span>
              {isAdmin && allowed ? (
                <select className="h-7 rounded-md border bg-background px-2 text-xs" value={g.peer_theme ?? ""}
                  disabled={saving} onChange={(e) => save(e.target.value)} aria-label="Peer theme">
                  {allowed.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              ) : (
                <span className="font-medium">{g.peer_theme}</span>
              )}
              {g.theme_source && g.theme_source !== "fixed" && (
                <Badge variant="outline" className="text-[10px]">{g.theme_source === "manual" ? "Manual" : "AI"}</Badge>
              )}
              {isAdmin && g.theme_source === "manual" && (
                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" disabled={saving}
                  onClick={() => save(null)}>Reset to AI</Button>
              )}
              {saving && <Loader2 className="h-3 w-3 animate-spin" />}
            </div>
            {g.theme_reason && <p className="text-xs text-muted-foreground">{g.theme_reason}</p>}
            <p className="text-xs text-muted-foreground">
              Compared with{" "}
              <span className="text-foreground font-medium">
                {g.basis === "theme"
                  ? `${masters.length} peers in the "${g.peer_theme}" theme`
                  : `${masters.length} peers in the whole category`}
              </span>
              {g.basis === "category" && ` (theme group has only ${g.theme_masters} — needs 5)`}
              {" "}· one peer per master fund
              {!!g.pending_excluded && ` · ${g.pending_excluded} AI-categorised fund(s) excluded until approved`}
              {!!g.borderline_excluded && ` · ${g.borderline_excluded} borderline master(s) left out (models disagreed on theme)`}
              {!!g.theme_unreviewed && ` · theme unreviewed: ${g.theme_unreviewed} master(s)`}
            </p>
            <button className="flex items-center gap-1 text-xs text-primary" onClick={() => setOpen(!open)}>
              {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />} Peers
            </button>
            {open && (
              <ul className="space-y-1 text-xs">
                {masters.map((m, i) => (
                  <li key={i} className="rounded border px-2 py-1">
                    <div className="text-muted-foreground">
                      {m.master_key ? `${m.master_key_kind === "isin" ? "ISIN" : "Master"}: ${m.master_key}` : "Direct holdings"}
                    </div>
                    <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                      {m.funds.map((f) => (
                        <span key={f.sec_proj_id} className={f.is_self ? "font-semibold" : ""}>
                          <span className="font-mono">{f.representative_code ?? f.fund_code}</span>
                          <span className="text-muted-foreground"> · {HEDGE_LABEL[f.hedge_group ?? "unknown"]}</span>
                          {f.is_passive && <span className="text-muted-foreground"> · passive</span>}
                          {f.is_esg && <span className="text-muted-foreground"> · ESG</span>}
                        </span>
                      ))}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function PendingTable({ categories, onDone }: { categories: string[]; onDone: () => void }) {
  const [rows, setRows] = useState<PendingCategory[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = () => api.getPendingCategories().then(setRows).catch((e) => setError(String(e)));
  useEffect(() => { load(); }, []);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try { await fn(); await load(); onDone(); } catch (e) { setError(e instanceof Error ? e.message : "Failed"); }
    finally { setBusy(false); }
  }

  if (!rows) return null;
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">No AI-categorised funds awaiting approval.</p>;
  return (
    <Card>
      <CardContent className="p-4 space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="font-semibold text-sm">Pending categories ({rows.length})</h2>
            <p className="text-xs text-muted-foreground">
              Factsheets print no AIMC category, so the AI chose one. Excluded from peer statistics until approved.
            </p>
          </div>
          <Button size="sm" disabled={busy} onClick={() => run(() => api.approveCategories(rows.map((r) => r.sec_proj_id)))}>
            Approve all shown
          </Button>
        </div>
        {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-muted-foreground">
              <tr><th className="py-1 pr-2">Fund</th><th className="py-1 pr-2">Master fund</th><th className="py-1 pr-2">AI category</th><th /></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.sec_proj_id} className="border-t align-top">
                  <td className="py-1 pr-2">
                    <div className="font-mono">{r.fund_code}</div>
                    <div className="text-muted-foreground">{r.name_en}</div>
                  </td>
                  <td className="py-1 pr-2 text-muted-foreground">{r.master_fund ?? "–"}</td>
                  <td className="py-1 pr-2">
                    <select className="h-7 max-w-[220px] rounded-md border bg-background px-1 text-xs" disabled={busy}
                      value={r.aimc_category ?? ""} title={r.aimc_reason ?? undefined}
                      onChange={(e) => run(() => api.setPeerCategory(r.sec_proj_id, e.target.value))}
                      aria-label="Edit category">
                      {categories.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                    {r.peer_theme && r.peer_theme !== r.aimc_category && <div className="text-muted-foreground">{r.peer_theme}</div>}
                  </td>
                  <td className="py-1">
                    <Button variant="outline" size="sm" className="h-7 text-xs" disabled={busy}
                      onClick={() => run(() => api.approveCategories([r.sec_proj_id]))}>Approve</Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

export default function PeersPage() {
  const router = useRouter();
  const [groups, setGroups] = useState<PeerGroup[] | null>(null);
  const [themes, setThemes] = useState<Record<string, string[]>>({});
  const [isAdmin, setIsAdmin] = useState(false);
  const [categories, setCategories] = useState<string[]>([]);
  const [error, setError] = useState("");

  const loadGroups = () =>
    api.getHeldPeerGroups().then(setGroups).catch((e) => setError(e instanceof Error ? e.message : "Load failed"));
  useEffect(() => {
    loadGroups();
    api.getPeerThemes().then(setThemes).catch(() => {});
    api.getPeerCategories().then(setCategories).catch(() => {});
    api.getMe().then((u) => setIsAdmin(u.role === "admin")).catch(() => {});
  }, []);

  return (
    <div className="min-h-screen bg-muted/40">
      <header className="border-b bg-background px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => router.push("/dashboard")}><ArrowLeft className="h-4 w-4" /></Button>
          <h1 className="font-semibold text-lg">Peer groups</h1>
        </div>
        <ThemeToggle />
      </header>
      <main className="mx-auto max-w-3xl p-4 space-y-3">
        <p className="text-sm text-muted-foreground">
          The peers each held fund is compared with. Feeders of the same master fund count as one peer.
        </p>
        {isAdmin && categories.length > 0 && <PendingTable categories={categories} onDone={loadGroups} />}
        {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
        {!groups && !error && <Loader2 className="h-5 w-5 animate-spin" />}
        {groups?.map((g, i) => (
          <GroupCard key={g.sec_proj_id} g={g} themes={themes} isAdmin={isAdmin}
            onChanged={(ng) => setGroups((gs) => gs && gs.map((x, j) => (j === i ? ng : x)))} />
        ))}
      </main>
    </div>
  );
}
