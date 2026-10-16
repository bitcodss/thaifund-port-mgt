"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, type PeerGroup } from "@/lib/api";
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

export default function PeersPage() {
  const router = useRouter();
  const [groups, setGroups] = useState<PeerGroup[] | null>(null);
  const [themes, setThemes] = useState<Record<string, string[]>>({});
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.getHeldPeerGroups().then(setGroups).catch((e) => setError(e instanceof Error ? e.message : "Load failed"));
    api.getPeerThemes().then(setThemes).catch(() => {});
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
