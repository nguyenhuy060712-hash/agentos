// GĐ5b Settings › Lịch chạy: agent crons (agent_crons). View all of the workspace, create (editor+), pause/resume and delete
// (admin, or the editor who created it). Rows locked by P0-1 (test crons disabled 20/10/2026) are shown disabled with their
// reason and can never be re-enabled here (the server refuses too, 409).
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { CalendarClockIcon, LoaderIcon, LockIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { createSchedule, deleteSchedule, getSchedules, setScheduleEnabled, type Schedule } from "@/lib/agentos";
import { cronToVi, presetCron, viDateTime, type Preset } from "@/genui/gd5b-helpers";
import type { AgentProfile } from "./ui-context";

const field = "bg-background rounded-lg border px-2.5 py-1.5 text-sm";
const DOWS = [1, 2, 3, 4, 5, 6, 0];

export function SchedulesPanel({ agents }: { agents: AgentProfile[] }) {
  const { t } = useTranslation();
  const [d, setD] = useState<{ items: Schedule[]; can_create: boolean; tz: string; max_per_day: number } | null>(null);
  const [err, setErr] = useState(""); const [busy, setBusy] = useState<number | "new" | null>(null); const [confirm, setConfirm] = useState<number | null>(null);
  const [form, setForm] = useState(false);
  const [f, setF] = useState({ agent: agents[0]?.graph_id ?? "", name: "", preset: "weekdays" as Preset, time: "08:30", dow: 1, dom: 1, custom: "0 8 * * 1-5", message: "" });
  const load = () => getSchedules().then(setD).catch((e) => setErr(String((e as Error).message)));
  useEffect(() => { void load(); }, []);
  const cron = presetCron(f.preset, f.time, { dow: f.dow, dom: f.dom, custom: f.custom });
  const toggle = async (s: Schedule) => {
    setBusy(s.id); setErr("");
    try { const n = await setScheduleEnabled(s.id, !s.enabled); setD((x) => x && { ...x, items: x.items.map((i) => (i.id === s.id ? n : i)) }); }
    catch (e) { setErr(String((e as Error).message)); } finally { setBusy(null); }
  };
  const del = async (id: number) => {
    setBusy(id); setErr("");
    try { await deleteSchedule(id); setConfirm(null); setD((x) => x && { ...x, items: x.items.filter((i) => i.id !== id) }); }
    catch (e) { setErr(String((e as Error).message)); } finally { setBusy(null); }
  };
  const create = async () => {
    setBusy("new"); setErr("");
    try { await createSchedule({ agent_id: f.agent, name: f.name, cron, message: f.message }); setForm(false); setF({ ...f, name: "", message: "" }); await load(); }
    catch (e) { setErr(String((e as Error).message)); } finally { setBusy(null); }
  };
  if (!d) return err ? <p role="alert" className="text-destructive text-sm">{err}</p> : <LoaderIcon className="size-4 animate-spin" />;
  const groups: [string, Schedule[]][] = [["schedActive", d.items.filter((s) => s.enabled && !s.locked)], ["schedPaused", d.items.filter((s) => !s.enabled && !s.locked)],
    ["schedLocked", d.items.filter((s) => s.locked)]];
  return (
    <div className="space-y-4" data-testid="schedules">
      <div className="flex items-start gap-3">
        <p className="text-muted-foreground flex-1 text-sm">{t("schedIntro", { tz: d.tz, max: d.max_per_day })}</p>
        {d.can_create && !form && <Button size="sm" onClick={() => setForm(true)} data-testid="sched-new"><PlusIcon className="size-4" />{t("schedNew")}</Button>}
      </div>
      {form && (
        <div className="bg-muted/40 grid gap-3 rounded-xl border p-3" data-testid="sched-form">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1 text-sm">{t("schedAgent")}
              <select className={field} value={f.agent} onChange={(e) => setF({ ...f, agent: e.target.value })} data-testid="sched-agent">
                {agents.map((a) => <option key={a.graph_id} value={a.graph_id}>{a.name}</option>)}</select></label>
            <label className="grid gap-1 text-sm">{t("schedName")}
              <input className={field} value={f.name} maxLength={100} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder={t("schedNamePh")} data-testid="sched-name" /></label>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="grid gap-1 text-sm">{t("schedRepeat")}
              <select className={field} value={f.preset} onChange={(e) => setF({ ...f, preset: e.target.value as Preset })} data-testid="sched-preset">
                {(["daily", "weekdays", "weekly", "monthly", "custom"] as Preset[]).map((p) => <option key={p} value={p}>{t(`sched_${p}`)}</option>)}</select></label>
            {f.preset === "weekly" && <label className="grid gap-1 text-sm">{t("schedDow")}
              <select className={field} value={f.dow} onChange={(e) => setF({ ...f, dow: Number(e.target.value) })} data-testid="sched-dow">
                {DOWS.map((x) => <option key={x} value={x}>{t(`dow${x}`)}</option>)}</select></label>}
            {f.preset === "monthly" && <label className="grid gap-1 text-sm">{t("schedDom")}
              <input type="number" min={1} max={28} className={`${field} w-20`} value={f.dom} onChange={(e) => setF({ ...f, dom: Math.min(28, Math.max(1, Number(e.target.value) || 1)) })} data-testid="sched-dom" /></label>}
            {f.preset !== "custom" ? <label className="grid gap-1 text-sm">{t("schedTime")}
              <input type="time" step={300} className={field} value={f.time} onChange={(e) => setF({ ...f, time: e.target.value })} data-testid="sched-time" /></label>
            : <label className="grid gap-1 text-sm">{t("schedCron")}
              <input className={`${field} w-44 font-mono`} value={f.custom} onChange={(e) => setF({ ...f, custom: e.target.value })} placeholder="phút giờ ngày tháng thứ" data-testid="sched-cron" /></label>}
            <span className="text-muted-foreground pb-2 text-xs" data-testid="sched-preview">{cronToVi(cron) ?? cron} · <code>{cron}</code></span>
          </div>
          <label className="grid gap-1 text-sm">{t("schedMessage")}
            <textarea className={`${field} min-h-20`} value={f.message} maxLength={4000} onChange={(e) => setF({ ...f, message: e.target.value })} placeholder={t("schedMessagePh")} data-testid="sched-message" /></label>
          <p className="text-muted-foreground text-xs">{t("schedCost")}</p>
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setForm(false)}>{t("cancel")}</Button>
            <Button size="sm" onClick={create} disabled={busy === "new" || !f.name.trim() || !f.message.trim() || !f.agent || !cron} data-testid="sched-create">
              {busy === "new" && <LoaderIcon className="size-4 animate-spin" />}{t("schedCreate")}</Button>
          </div>
        </div>)}
      {err && <p role="alert" className="text-destructive text-sm" data-testid="sched-error">{err}</p>}
      {groups.map(([k, rows]) => rows.length > 0 && (
        <section key={k}>
          <h3 className="text-muted-foreground mb-1.5 flex items-center gap-1.5 text-xs font-semibold tracking-wide uppercase">{k === "schedLocked" && <LockIcon className="size-3.5" />}{t(k)} · {rows.length}</h3>
          <ul className="divide-y rounded-xl border">{rows.map((s) => (
            <li key={s.id} className={`flex items-start gap-3 px-3 py-2.5 ${s.locked ? "opacity-70" : ""}`} data-testid="sched-row" data-id={s.id} data-locked={s.locked || undefined} data-enabled={s.enabled}>
              <CalendarClockIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-x-2"><span className="text-sm font-medium">{s.name}</span><span className="text-muted-foreground text-xs">{s.agent_name}</span>{s.source === "agent" && <span className="bg-muted text-muted-foreground rounded px-1.5 py-0.5 text-[10px]" data-testid="sched-by-agent">{t("schedByAgent")}</span>}{s.paused && <span className="text-muted-foreground text-[10px]">{t("schedPausedTag")}</span>}</div>
                <div className="text-muted-foreground text-xs">{cronToVi(s.cron) ?? s.cron} · <code title={t("schedCron")}>{s.cron}</code></div>
                <div className="text-muted-foreground text-xs">
                  {s.locked ? <span data-testid="sched-locked-reason">{t("schedLockedWhy", { why: s.locked_reason })}</span>
                    : <>{s.next_run && <>{t("schedNext", { at: viDateTime(s.next_run) })} · </>}{s.last_run ? t("schedLast", { at: viDateTime(s.last_run), st: s.last_status ?? "" }) : t("schedNever")}</>}
                </div>
                <div className="text-muted-foreground mt-0.5 line-clamp-1 text-xs italic" title={s.message}>“{s.message}”</div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <label className={`flex items-center gap-1.5 text-xs ${!s.can_edit ? "cursor-not-allowed" : ""}`} title={s.locked ? t("schedLockedHint") : !s.can_edit ? t("schedNoPerm") : ""}>
                  <input type="checkbox" role="switch" className="size-4 accent-current" checked={s.enabled && !s.locked} disabled={!s.can_edit || busy === s.id}
                    onChange={() => toggle(s)} aria-label={t("schedEnabled", { name: s.name })} data-testid="sched-toggle" />
                  <span className="hidden sm:inline">{s.locked ? t("schedLockedShort") : s.enabled ? t("schedOn") : t("schedOff")}</span></label>
                {s.can_edit && (confirm === s.id
                  ? <><Button size="sm" variant="destructive" onClick={() => del(s.id)} disabled={busy === s.id} data-testid="sched-delete-yes">{t("delete")}</Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirm(null)}>{t("cancel")}</Button></>
                  : <button aria-label={t("schedDelete", { name: s.name })} className="hover:bg-muted text-muted-foreground rounded-lg p-1.5" onClick={() => setConfirm(s.id)} data-testid="sched-delete"><Trash2Icon className="size-4" /></button>)}
              </div>
            </li>))}</ul>
        </section>))}
      {!d.items.length && <p className="text-muted-foreground text-sm">{t("schedNone")}</p>}
    </div>);
}
