"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, type CurrentUser } from "@/lib/api";
import {
  loadSettings, saveSettings,
  type ReturnBasis, type ReturnType, type UserSettings,
  pnlBasisLabel, derivePnlBasis, pnlFieldKey,
} from "@/lib/settings";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { ArrowLeft, LogOut } from "lucide-react";

export default function SettingsPage() {
  const router = useRouter();
  const [me, setMe] = useState<CurrentUser | null>(null);
  const [settings, setSettings] = useState<UserSettings>({ returnBasis: "fund", returnType: "pnl" });
  const [saved, setSaved] = useState(false);

  const [dobValue, setDobValue] = useState("");
  const [dobSaving, setDobSaving] = useState(false);
  const [dobError, setDobError] = useState("");
  const [dobSaved, setDobSaved] = useState(false);

  useEffect(() => {
    if (!localStorage.getItem("token")) { router.replace("/login"); return; }
    setSettings(loadSettings());
    api.getMe()
      .then((u) => { setMe(u); setDobValue(u.date_of_birth ?? ""); })
      .catch(() => {});
  }, [router]);

  function updateSetting<K extends keyof UserSettings>(key: K, value: UserSettings[K]) {
    const next = { ...settings, [key]: value };
    setSettings(next);
    saveSettings(next);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  async function saveDob(e: React.FormEvent) {
    e.preventDefault();
    setDobSaving(true);
    setDobError("");
    setDobSaved(false);
    try {
      const updated = await api.updateMe(dobValue || null);
      setMe(updated);
      setDobSaved(true);
      setTimeout(() => setDobSaved(false), 3000);
    } catch (err: unknown) {
      setDobError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setDobSaving(false);
    }
  }

  const previewBasis = derivePnlBasis(settings);
  const previewField = pnlFieldKey(previewBasis);
  const previewLabel = pnlBasisLabel(settings);

  const RadioGroup = ({
    label, name, value, options, onChange,
  }: {
    label: string;
    name: string;
    value: string;
    options: { value: string; label: string; description: string }[];
    onChange: (v: string) => void;
  }) => (
    <div className="space-y-2">
      <p className="text-sm font-medium">{label}</p>
      <div className="space-y-2">
        {options.map((opt) => (
          <label
            key={opt.value}
            className={`flex items-start gap-3 rounded-md border p-3 cursor-pointer transition-colors ${
              value === opt.value
                ? "border-primary bg-primary/5"
                : "border-border hover:bg-muted/50"
            }`}
          >
            <input
              type="radio"
              name={name}
              value={opt.value}
              checked={value === opt.value}
              onChange={() => onChange(opt.value)}
              className="mt-0.5 accent-primary"
            />
            <div>
              <p className="text-sm font-medium">{opt.label}</p>
              <p className="text-xs text-muted-foreground">{opt.description}</p>
            </div>
          </label>
        ))}
      </div>
    </div>
  );

  return (
    <div className="min-h-screen bg-muted/40">
      <header className="border-b bg-background px-4 py-3 flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={() => router.push("/dashboard")}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <h1 className="font-semibold">Settings</h1>
        <div className="ml-auto flex items-center gap-1">
          <ThemeToggle />
          <Button variant="ghost" size="sm" onClick={() => { localStorage.removeItem("token"); router.push("/login"); }}>
            <LogOut className="h-4 w-4 sm:mr-1" />
            <span className="hidden sm:inline">Logout</span>
          </Button>
        </div>
      </header>

      <main className="max-w-2xl mx-auto p-4 space-y-6">

        {/* Return Display Settings */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center justify-between">
              Return Display
              {saved && <span className="text-xs font-normal text-green-600 dark:text-green-400">Saved</span>}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-6">
            <RadioGroup
              label="Return Basis"
              name="returnBasis"
              value={settings.returnBasis}
              onChange={(v) => updateSetting("returnBasis", v as ReturnBasis)}
              options={[
                {
                  value: "fund",
                  label: "Fund entry",
                  description: "Return % measured from the date you first bought or switched into each fund — shows how the fund itself is performing for you.",
                },
                {
                  value: "cost",
                  label: "Original cost",
                  description: "Return % measured from your original investment cost, including cost carried through fund switches — shows overall investment efficiency.",
                },
              ]}
            />
            <RadioGroup
              label="Return Type"
              name="returnType"
              value={settings.returnType}
              onChange={(v) => updateSetting("returnType", v as ReturnType)}
              options={[
                {
                  value: "pnl",
                  label: "Price return only",
                  description: "Unrealized gain/loss from NAV movement — does not include dividends received.",
                },
                {
                  value: "total",
                  label: "Total return (include dividends)",
                  description: "Price return plus net dividends received — the true economic return of your holding.",
                },
              ]}
            />

            {/* Preview */}
            <div className="rounded-md bg-muted/60 border px-3 py-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">Active mode: </span>
              {previewLabel}
              <span className="ml-2 font-mono text-muted-foreground/60">({previewField})</span>
            </div>
          </CardContent>
        </Card>

        {/* Profile */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Profile</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {me && (
              <div className="text-sm text-muted-foreground">
                Signed in as <span className="font-medium text-foreground">{me.email}</span>
              </div>
            )}
            <form onSubmit={saveDob} className="space-y-2">
              <Label htmlFor="dob">
                Date of Birth
                <span className="ml-1 font-normal text-muted-foreground text-xs">
                  — used for RMF age-55 eligibility check
                </span>
              </Label>
              <div className="flex gap-2 items-center">
                <Input
                  id="dob"
                  type="date"
                  value={dobValue}
                  onChange={(e) => setDobValue(e.target.value)}
                  className="w-44"
                />
                <Button type="submit" size="sm" disabled={dobSaving}>
                  {dobSaving ? "Saving…" : "Save"}
                </Button>
                {dobSaved && <span className="text-xs text-green-600 dark:text-green-400">Saved</span>}
              </div>
              {dobValue && (() => {
                const dob = new Date(dobValue);
                const age = Math.floor((Date.now() - dob.getTime()) / (365.25 * 24 * 3600 * 1000));
                const yr55 = new Date(dob);
                yr55.setFullYear(dob.getFullYear() + 55);
                return (
                  <p className="text-xs text-muted-foreground">
                    อายุปัจจุบัน <span className="font-medium text-foreground">{age} ปี</span>
                    {age < 55 && (
                      <> · ถึง 55 ปี: <span className="font-medium text-foreground">{yr55.toLocaleDateString("th-TH")}</span></>
                    )}
                    {age >= 55 && (
                      <> · <span className="text-green-600 dark:text-green-400 font-medium">อายุครบ 55 ปีแล้ว</span></>
                    )}
                  </p>
                );
              })()}
              {dobError && <p className="text-xs text-destructive">{dobError}</p>}
            </form>
          </CardContent>
        </Card>

      </main>
    </div>
  );
}
