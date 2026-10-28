// Settings as a modal with left tabs (ChatGPT/Gemini settings pattern): General, Voice, Agents, Permissions, Budget. Lazy chunk.
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Dialog } from "@base-ui/react/dialog";
import { Tabs } from "@base-ui/react/tabs";
import { BotIcon, BlocksIcon, BrainIcon, DatabaseIcon, UserRoundPenIcon, CalendarClockIcon, LockKeyholeIcon, MicIcon, SettingsIcon, ShieldIcon, WalletIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Theme } from "@/lib/theme";
import { getVoiceSettings, putAgentVoice, putVoiceSettings, type VoiceSettings, type WorkspaceVoice } from "@/voice/api";
import type { VoiceConfig } from "@/voice/types";
import { AgentAvatar, type AgentProfile } from "./ui-context";
import { setChatVisible } from "@/lib/agentos";
import { MemoryPanel } from "./MemoryPanel";
import { SchedulesPanel } from "./SchedulesPanel";
import { CodeRunSettings } from "./CodeRunSettings";
import { SecurityPanel } from "./SecurityPanel";
import { PersonalPanel } from "./PersonalPanel";
import { DataPanel } from "./DataPanel";
import { AgentDangerZone } from "./AgentDangerZone";
import { INTEGRATIONS_URL } from "./connect-helpers";

type Props = { open: boolean; onClose: () => void; agents: AgentProfile[]; onSaved: () => void;
  /** GĐ3 (b): every agent incl. hidden ones, for the "visible in chat" switches */
  allAgents?: AgentProfile[]; onAgentsChanged?: () => void;
  lang: "vi" | "en"; onLang: (l: "vi" | "en") => void; theme: Theme; onTheme: (t: Theme) => void; dev: boolean; onDev: (v: boolean) => void;
  voiceConfig: VoiceConfig | null; model: string | null; onModel: (m: string) => void; agentName: string;
  /** GĐ5a: tab to open (#/settings/<tab>, e.g. the memory chip's "Bộ nhớ" link) */
  tab?: string };

const field = "bg-background rounded-lg border px-2.5 py-1.5 text-sm";

export function SettingsDialog(p: Props) {
  const { t } = useTranslation();
  const [s, setS] = useState<VoiceSettings | null>(null); const [ws, setWs] = useState<WorkspaceVoice | null>(null); const [msg, setMsg] = useState("");
  useEffect(() => { if (p.open) getVoiceSettings().then((x) => { setS(x); setWs(x.workspace); }); }, [p.open]);
  const save = async () => {
    if (!ws) return;
    try { const x = await putVoiceSettings(ws); setS(x); setWs(x.workspace); setMsg(t("saved")); p.onSaved(); } catch (e) { setMsg(String((e as Error).message)); }
  };
  const saveAgent = async (g: string, o: { allowed_models?: string[] | null; default_model?: string | null }) => {
    try { const x = await putAgentVoice(g, o); setS(x); setMsg(t("saved")); p.onSaved(); } catch (e) { setMsg(String((e as Error).message)); }
  };
  const num = (k: keyof WorkspaceVoice, step = 1) => ws && (
    <input type="number" step={step} min={0} data-testid={`ws-${k}`} className={`${field} w-28`} value={ws[k] as number}
      onChange={(e) => setWs({ ...ws, [k]: Number(e.target.value) })} disabled={!s?.can_edit} />);
  const saveRow = (<div className="flex items-center gap-3 pt-2">
    <Button size="sm" onClick={save} disabled={!s?.can_edit} data-testid="settings-save">{t("save")}</Button>
    <span data-testid="settings-msg" className="text-muted-foreground text-sm" role="status">{msg}</span></div>);
  const tabs: [string, string, ReactNode][] = [["general", t("tabGeneral"), <SettingsIcon key="g" className="size-4" />], ["voice", t("tabVoice"), <MicIcon key="v" className="size-4" />],
    ["agents", t("tabAgents"), <BotIcon key="a" className="size-4" />], ["permissions", t("tabPermissions"), <ShieldIcon key="p" className="size-4" />], ["budget", t("tabBudget"), <WalletIcon key="b" className="size-4" />],
    ["memory", t("tabMemory"), <BrainIcon key="m" className="size-4" />], ["schedules", t("tabSchedules"), <CalendarClockIcon key="s" className="size-4" />],
    ["personal", t("tabPersonal"), <UserRoundPenIcon key="pers" className="size-4" />],
    ["security", t("tabSecurity"), <LockKeyholeIcon key="sec" className="size-4" />], ["mydata", t("tabMyData"), <DatabaseIcon key="md" className="size-4" />]];
  const ids = s ? s.catalog.filter((m) => m.enabled).map((m) => m.id) : [];
  const cfg = p.voiceConfig;
  return (
    <Dialog.Root open={p.open} onOpenChange={(o) => { if (!o) p.onClose(); }}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[2px] data-[open]:animate-in data-[open]:fade-in-0" />
        <Dialog.Popup data-testid="settings-page" className="bg-background text-foreground fixed inset-0 z-50 flex flex-col overflow-hidden outline-none sm:inset-auto sm:top-1/2 sm:left-1/2 sm:h-[min(640px,90dvh)] sm:w-[min(880px,94vw)] sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-2xl sm:border sm:shadow-2xl">
          <div className="flex items-center border-b px-5 py-3">
            <Dialog.Title className="text-base font-semibold">{t("settings")}</Dialog.Title>
            <Dialog.Close aria-label={t("close")} data-testid="settings-close" className="hover:bg-muted ms-auto rounded-lg p-1.5"><XIcon className="size-5" /></Dialog.Close>
          </div>
          <Tabs.Root key={p.tab ?? "general"} defaultValue={p.tab && ["general", "voice", "agents", "permissions", "budget", "memory", "schedules", "personal", "security", "mydata"].includes(p.tab) ? p.tab : "general"} orientation="vertical" className="flex min-h-0 flex-1 flex-col sm:flex-row">
            <Tabs.List aria-label={t("settings")} className="flex shrink-0 gap-1 overflow-x-auto border-b p-2 sm:w-52 sm:flex-col sm:border-e sm:border-b-0">
              {tabs.map(([v, label, icon]) => (
                <Tabs.Tab key={v} value={v} data-testid={`settings-tab-${v}`}
                  className="text-muted-foreground hover:bg-muted data-[selected]:bg-muted data-[selected]:text-foreground focus-visible:ring-ring/50 flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium whitespace-nowrap outline-none focus-visible:ring-2">
                  {icon}{label}</Tabs.Tab>))}
              <a href={INTEGRATIONS_URL} data-testid="settings-integrations" className="text-muted-foreground hover:bg-muted focus-visible:ring-ring/50 flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium whitespace-nowrap outline-none focus-visible:ring-2"><BlocksIcon className="size-4" />{t("tabIntegrations")}</a>
            </Tabs.List>
            <div className="min-h-0 flex-1 overflow-y-auto p-5 text-sm">
              <Tabs.Panel value="general" className="space-y-5">
                <Row label={t("uiLang")}>
                  <select data-testid="settings-lang" className={field} value={p.lang} onChange={(e) => p.onLang(e.target.value as "vi" | "en")}>
                    <option value="vi">Tiếng Việt</option><option value="en">English</option></select></Row>
                <Row label={t("theme")}>
                  <div className="flex gap-2" role="radiogroup" aria-label={t("theme")}>
                    {(["light", "dark", "system"] as const).map((v) => (
                      <button key={v} type="button" role="radio" aria-checked={p.theme === v} data-testid={`settings-theme-${v}`} onClick={() => p.onTheme(v)}
                        className="aria-checked:border-foreground aria-checked:bg-muted rounded-lg border px-3 py-1.5">{t(v === "light" ? "themeLight" : v === "dark" ? "themeDark" : "themeSystem")}</button>))}
                  </div></Row>
                <label className="flex items-center gap-2.5"><input type="checkbox" data-testid="dev-toggle" checked={p.dev} onChange={(e) => p.onDev(e.target.checked)} />{t("devMode")}</label>
              </Tabs.Panel>

              <Tabs.Panel value="voice" className="space-y-5">
                <p className="text-muted-foreground">{t("voiceDefinition")}</p>
                {cfg && (
                  <Row label={`${t("myVoiceModel")} · ${p.agentName}`} hint={t("myVoiceModelHint")}>
                    <div className="flex flex-wrap items-center gap-2">
                      <select data-testid="voice-model" className={field} value={p.model ?? ""} onChange={(e) => p.onModel(e.target.value)} aria-label={t("voiceModel")}>
                        {cfg.models.map((m) => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select>
                      <span className="text-muted-foreground text-xs" data-testid="voice-source">{cfg.source === "agent" ? t("agentOverride") : t("workspaceDefault")}</span>
                    </div></Row>)}
                {s && ws && (<>
                  <section className="space-y-2"><h3 className="font-medium">{t("catalog")}</h3>
                    <div className="overflow-x-auto rounded-lg border"><table className="w-full text-xs" data-testid="catalog">
                      <thead className="bg-muted/60"><tr><th className="p-2 text-start">{t("priceColModel")}</th><th className="text-start">{t("priceColProvider")}</th><th className="text-start">{t("price")}</th><th className="text-start">{t("priceColUsdMin")}</th><th>{t("enabled")}</th></tr></thead>
                      <tbody>{s.catalog.map((m) => (
                        <tr key={m.id} className="border-t"><td className="p-2 font-mono">{m.id}</td><td>{m.provider}</td>
                          <td>{m.price_unit === "per_min" ? `$${m.price_per_min}/min` : `$${m.price_in} / $${m.price_out} per 1M tok`}</td>
                          <td>{m.est_usd_per_min}</td><td className="text-center">{m.enabled ? "✓" : "—"}</td></tr>))}</tbody></table></div></section>
                  <Row label={t("allowedModels")}><div className="flex flex-wrap gap-4">{ids.map((id) => (
                    <label key={id} className="flex items-center gap-1.5"><input type="checkbox" data-testid={`allow-${id}`} disabled={!s.can_edit} checked={ws.allowed_models.includes(id)}
                      onChange={(e) => setWs({ ...ws, allowed_models: e.target.checked ? [...ws.allowed_models, id] : ws.allowed_models.filter((x) => x !== id) })} />
                      <span className="font-mono">{id}</span></label>))}</div></Row>
                  <Row label={t("defaultModel")}><select data-testid="default-model" className={field} value={ws.default_model} disabled={!s.can_edit}
                    onChange={(e) => setWs({ ...ws, default_model: e.target.value })}>{ws.allowed_models.map((m) => <option key={m} value={m}>{m}</option>)}</select></Row>
                  <Row label={t("voiceLang")}><input className={`${field} w-24`} value={ws.language} disabled={!s.can_edit} onChange={(e) => setWs({ ...ws, language: e.target.value })} /></Row>
                  <label className="flex items-center gap-2.5"><input type="checkbox" checked={ws.barge_in} disabled={!s.can_edit} onChange={(e) => setWs({ ...ws, barge_in: e.target.checked })} />{t("bargeIn")}</label>
                  {saveRow}</>)}
              </Tabs.Panel>

              <Tabs.Panel value="agents" className="space-y-3">
                <h3 className="font-medium">{t("perAgent")}</h3>
                {s && ws && p.agents.map((a) => {
                  const ov = s.agents[a.graph_id] ?? {}; const allowed = ov.allowed_models ?? [];
                  return (
                    <div key={a.graph_id} className="flex flex-wrap items-center gap-3 rounded-xl border p-3" data-testid={`agent-row-${a.graph_id}`}>
                      <AgentAvatar agent={a} size={30} />
                      <span className="w-48 min-w-0"><span className="block truncate font-medium">{a.name}</span>
                        <span className="text-muted-foreground block truncate text-xs">{a.role} · {a.group}</span></span>
                      {ws.allowed_models.map((m) => (
                        <label key={m} className="flex items-center gap-1.5 text-xs"><input type="checkbox" data-testid={`agent-${a.graph_id}-allow-${m}`} disabled={!s.can_override_agent} checked={allowed.includes(m)}
                          onChange={(e) => { const n = e.target.checked ? [...allowed, m] : allowed.filter((x) => x !== m);
                            void saveAgent(a.graph_id, { allowed_models: n.length ? n : null, default_model: n.includes(ov.default_model ?? "") ? ov.default_model : null }); }} />
                          <span className="font-mono">{m}</span></label>))}
                      <select data-testid={`agent-${a.graph_id}-default`} className={`${field} text-xs`} value={ov.default_model ?? ""} disabled={!s.can_override_agent}
                        onChange={(e) => void saveAgent(a.graph_id, { allowed_models: allowed.length ? allowed : null, default_model: e.target.value || null })}>
                        <option value="">{t("inheritDefault")}</option>
                        {(allowed.length ? allowed : ws.allowed_models).map((m) => <option key={m} value={m}>{m}</option>)}</select>
                    </div>);
                })}
                {p.allAgents && (
                  <details className="rounded-xl border p-3" data-testid="chat-visibility">
                    <summary className="cursor-pointer font-medium">{t("chatVisibility")} <span className="text-muted-foreground text-xs">({p.allAgents.filter((a) => a.visible).length}/{p.allAgents.length})</span></summary>
                    <p className="text-muted-foreground mt-1 text-xs">{t("chatVisibilityHint")}</p>
                    <div className="mt-2 grid gap-1 sm:grid-cols-2">
                      {p.allAgents.map((a) => (
                        <label key={a.graph_id} className="flex items-center gap-2 text-sm"><input type="checkbox" data-testid={`visible-${a.graph_id}`} disabled={!s?.can_edit} checked={a.visible}
                          onChange={(e) => { void setChatVisible(a.assistant_id, e.target.checked).then(() => { setMsg(t("saved")); p.onAgentsChanged?.(); }).catch((err) => setMsg(String((err as Error).message))); }} />
                          <span className="truncate">{a.name}</span></label>))}
                    </div>
                  </details>)}
                <AgentDangerZone agents={p.allAgents ?? p.agents} onChanged={() => { p.onAgentsChanged?.(); p.onSaved(); }} />
                <span className="text-muted-foreground text-sm" role="status">{msg}</span>
              </Tabs.Panel>

              <Tabs.Panel value="permissions" className="space-y-4">
                {s && ws && (<>
                  <div className="flex flex-wrap items-center gap-3" data-testid="voice-roles"><span className="font-medium">{t("voiceUse")}</span>
                    {["admin", "editor", "viewer"].map((r) => (
                      <label key={r} className="flex items-center gap-1.5"><input type="checkbox" data-testid={`role-${r}`} disabled={!s.can_edit || r === "admin"} checked={(ws.voice_roles ?? []).includes(r)}
                        onChange={(e) => setWs({ ...ws, voice_roles: e.target.checked ? [...new Set([...(ws.voice_roles ?? []), r])] : (ws.voice_roles ?? []).filter((x) => x !== r) })} />{r}</label>))}</div>
                  <p className="text-muted-foreground">{t("voiceUseHint")}</p>{saveRow}</>)}
                <CodeRunSettings />
              </Tabs.Panel>

              <Tabs.Panel value="personal"><PersonalPanel /></Tabs.Panel>

              <Tabs.Panel value="security"><SecurityPanel /></Tabs.Panel>

              <Tabs.Panel value="mydata"><DataPanel /></Tabs.Panel>

              <Tabs.Panel value="schedules"><SchedulesPanel agents={p.agents} /></Tabs.Panel>

              <Tabs.Panel value="memory"><MemoryPanel agents={p.allAgents ?? p.agents} /></Tabs.Panel>

              <Tabs.Panel value="budget" className="space-y-5">
                {s && ws && (<>
                  <div className="grid gap-4 sm:grid-cols-3">
                    <label className="flex flex-col gap-1.5">{t("maxMinutes")}{num("max_session_min", 0.05)}</label>
                    <label className="flex flex-col gap-1.5">{t("dailyMinutes")}{num("daily_min_per_user")}</label>
                    <label className="flex flex-col gap-1.5">{t("dailyBudget")}{num("daily_budget_usd", 0.01)}</label></div>
                  {cfg && <p data-testid="budget-usage">{t("usageToday")}: {cfg.caps.used_min_today.toFixed(1)} / {cfg.caps.daily_min_per_user} min · ${cfg.caps.spent_usd_today.toFixed(3)} / ${cfg.caps.daily_budget_usd.toFixed(2)}</p>}
                  {saveRow}
                  <section><h3 className="font-medium">{t("audit")}</h3>
                    <ul className="text-muted-foreground mt-1 space-y-0.5 text-xs" data-testid="audit">{s.audit.slice(-8).reverse().map((x, i) => <li key={i}>{x.at.replace("T", " ").slice(0, 19)} · {x.by} · {x.what}</li>)}</ul></section></>)}
              </Tabs.Panel>
            </div>
          </Tabs.Root>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (<div className="space-y-1.5"><div className="font-medium">{label}</div>{children}{hint && <p className="text-muted-foreground text-xs">{hint}</p>}</div>);
}
