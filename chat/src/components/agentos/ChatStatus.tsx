// GĐ3.7 chat status: connection state (offline / reconnecting / back online, with a reload) and the text-chat cost/quota banner
// (agent daily budget, provider spend cap) from /api/chat/config. Refreshes the quota after each run.
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { useAui, useAuiState } from "@assistant-ui/react";
import { runFailure } from "@/runtime/useAgentOSRuntime";
import { capDetail } from "@/runtime/run-failure";
import { AlertCircleIcon, CheckIcon, CloudOffIcon, Loader2Icon, RefreshCwIcon, WalletIcon, XIcon } from "lucide-react";
import type { ChatConfig } from "@/lib/agentos";

type Phase = "online" | "offline" | "reconnecting" | "resumed";

export function ConnectionBanner() {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<Phase>(() => (navigator.onLine ? "online" : "offline"));
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => {
    const probe = async () => {
      setPhase("reconnecting");
      for (let i = 0; i < 6; i++) {
        try { const r = await fetch("/api/auth/me", { cache: "no-store" }); if (r.ok || r.status === 401) { setPhase("resumed"); timer.current = setTimeout(() => setPhase("online"), 2500); return; } } catch { /* still down */ }
        await new Promise((ok) => setTimeout(ok, 1000 * (i + 1)));
      }
      setPhase("offline");
    };
    const off = () => { clearTimeout(timer.current); setPhase("offline"); };
    const on = () => { void probe(); };
    addEventListener("offline", off); addEventListener("online", on);
    return () => { removeEventListener("offline", off); removeEventListener("online", on); clearTimeout(timer.current); };
  }, []);
  if (phase === "online") return null;
  return (
    <div role="status" aria-live="polite" data-testid="connection-state" data-phase={phase}
      className="border-border bg-background mx-auto mt-2 flex w-full max-w-md items-center gap-2 rounded-xl border px-3 py-2 text-[13px] shadow-sm">
      {phase === "offline" && <><CloudOffIcon className="size-3.5 text-amber-600" /><span className="flex-1">{t("connOffline")}</span>
        <button type="button" className="hover:bg-muted rounded-full px-2 py-0.5 text-xs font-medium" onClick={() => location.reload()}>{t("connReload")}</button></>}
      {phase === "reconnecting" && <><Loader2Icon className="text-muted-foreground size-3.5 animate-spin" /><span className="flex-1">{t("connReconnecting")}</span></>}
      {phase === "resumed" && <><CheckIcon className="size-3.5 text-emerald-600" /><span className="flex-1">{t("connResumed")}</span></>}
    </div>
  );
}

const usd = (x: number) => `$${x < 0.01 ? x.toFixed(4) : x.toFixed(2)}`;

export function ChatQuotaBanner({ cfg, onRefresh }: { cfg: ChatConfig | null; onRefresh: () => void }) {
  const { t } = useTranslation();
  const running = useAuiState((s) => s.thread.isRunning);
  const was = useRef(running);
  useEffect(() => { if (was.current && !running) onRefresh(); was.current = running; }, [running, onRefresh]);
  if (!cfg) return null;
  const q = cfg.quota; const rows: { key: string; label: string; used: number; limit: number }[] = [];
  if (q.agent_budget_usd_day) rows.push({ key: "agent", label: t("quotaAgentDay"), used: q.agent_spent_today_usd, limit: q.agent_budget_usd_day });
  const prov = new Set(cfg.models.map((m) => m.provider));
  for (const [p, c] of Object.entries(q.provider_caps)) if (prov.has(p)) rows.push({ key: p, label: t("quotaProvider", { p }), used: c.spent_usd, limit: c.cap_usd });
  const hot = rows.filter((r) => r.limit > 0 && r.used / r.limit >= 0.8);
  if (!hot.length) return null;
  return (
    <div role="status" data-testid="quota-banner" className="border-border bg-background mx-auto mt-2 flex w-full max-w-md flex-col gap-1.5 rounded-xl border px-3 py-2 text-[13px] shadow-sm">
      {hot.map((r) => {
        const pct = Math.min(100, Math.round((r.used / r.limit) * 100)); const out = r.used >= r.limit;
        return (
          <div key={r.key} data-quota={r.key} data-exhausted={out}>
            <div className="flex items-center gap-2"><WalletIcon className={`size-3.5 ${out ? "text-red-600" : "text-amber-600"}`} />
              <span className="flex-1">{out ? t("quotaOut", { label: r.label }) : t("quotaNear", { label: r.label, pct })}</span>
              <span className="text-muted-foreground font-mono text-xs tabular-nums">{usd(r.used)} / {usd(r.limit)}</span></div>
            <span role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-label={r.label} className="bg-muted mt-1 block h-1 overflow-hidden rounded-full">
              <span className={`block h-full ${out ? "bg-red-500" : "bg-amber-500"}`} style={{ width: `${pct}%` }} /></span>
          </div>);
      })}
    </div>
  );
}

/** GĐ3.7: the server rejected the request before answering -> keep the text, show why, one-click retry (re-sends it). */
export function RunFailureBanner() {
  const { t, i18n } = useTranslation();
  const aui = useAui();
  const f = useSyncExternalStore(runFailure.subscribe, runFailure.get);
  const tid = useAuiState((s) => s.threadListItem.remoteId);
  const first = useRef(tid);
  useEffect(() => { if (tid !== first.current) { first.current = tid; runFailure.set(null); } }, [tid]);
  if (!f) return null;
  const retry = () => { const text = f.text; runFailure.set(null); aui.thread().append({ role: "user", content: [{ type: "text", text }] }); };
  if (f.kind === "cap") return (   // spend cap reached: say why in plain words, no retry (it would be refused again)
    <div role="alert" data-testid="message-error" data-kind="cap"
      className="mx-auto mt-2 flex w-full max-w-md items-start gap-2 rounded-xl border border-red-300 bg-red-50 px-3 py-2 text-[13px] shadow-sm dark:border-red-900 dark:bg-red-950/40">
      <WalletIcon className="mt-0.5 size-3.5 shrink-0 text-red-600" />
      <div className="min-w-0 flex-1">
        <div className="font-medium">{t("capReached")}</div>
        <div className="text-muted-foreground text-xs">{capDetail(f.error, i18n.language, t, f.cap)}</div>
        <div className="text-muted-foreground mt-0.5 truncate text-xs">{t("capNotSent", { text: f.text.slice(0, 80) })}</div>
      </div>
      <button type="button" aria-label={t("close")} onClick={() => runFailure.set(null)} className="hover:bg-muted rounded-full p-0.5"><XIcon className="size-3" /></button>
    </div>);
  return (
    <div role="alert" data-testid="message-error" data-kind="pre-run"
      className="border-destructive/40 bg-destructive/5 mx-auto mt-2 flex w-full max-w-md items-start gap-2 rounded-xl border px-3 py-2 text-[13px] shadow-sm">
      <AlertCircleIcon className="text-destructive mt-0.5 size-3.5 shrink-0" />
      <div className="min-w-0 flex-1">
        <div>{t("runRejected")}</div>
        <div className="text-muted-foreground truncate text-xs" title={f.error}>“{f.text.slice(0, 80)}” · {f.error}</div>
      </div>
      <button type="button" data-testid="retry" onClick={retry} className="hover:bg-muted inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium">
        <RefreshCwIcon className="size-3" />{t("retry")}</button>
      <button type="button" aria-label={t("close")} onClick={() => runFailure.set(null)} className="hover:bg-muted rounded-full p-0.5"><XIcon className="size-3" /></button>
    </div>
  );
}

