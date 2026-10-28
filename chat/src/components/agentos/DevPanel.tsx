// Developer panel (off the chat surface by default; Settings > General): mock/real badge, usage/cost, last call, barge-in latency, log.
import { useTranslation } from "react-i18next";
import { useVoiceState } from "@assistant-ui/react";
import type { VoiceUiState } from "./voice-state";

export function DevPanel({ ui }: { ui: VoiceUiState }) {
  const { t } = useTranslation(); const vs = useVoiceState();
  const st = vs?.status?.type ?? "idle"; const cfg = ui.config;
  const p = cfg?.models.find((m) => m.id === ui.model)?.provider; const real = !!p && (cfg?.real_providers ?? []).includes(p);
  return (
    <aside data-testid="dev-panel" aria-label={t("devPanel")} className="bg-muted/30 hidden w-80 shrink-0 flex-col gap-2 overflow-auto border-s p-3 text-xs lg:flex">
      <div className="flex items-center gap-2 font-medium">{t("devPanel")}
        {real ? <span data-testid="real-badge" className="rounded bg-red-100 px-1 text-red-900 dark:bg-red-900/40 dark:text-red-200">REAL · A8</span>
              : <span className="rounded bg-amber-100 px-1 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">{t("mockBadge")}</span>}</div>
      <div>voice: <span data-testid="dev-voice-status">{st}</span> · {ui.model ?? "—"} · {cfg?.source === "agent" ? t("agentOverride") : t("workspaceDefault")}</div>
      {cfg && <div data-testid="voice-usage">{t("today")}: {cfg.caps.used_min_today.toFixed(1)}/{cfg.caps.daily_min_per_user} min · ${cfg.caps.spent_usd_today.toFixed(3)}/${cfg.caps.daily_budget_usd.toFixed(2)}</div>}
      {ui.session && <div>session {ui.session.grant.voice_session_id} · ${(ui.spend?.session ?? 0).toFixed(4)}</div>}
      {ui.last && <div>{t("lastCall")}: {ui.last.seconds.toFixed(1)} s · ${ui.last.cost_usd.toFixed(4)} · {ui.last.reason}</div>}
      {ui.bargeIns.length > 0 && <div data-testid="barge-latency">barge-in: {ui.bargeIns.slice(-3).join(", ")} ms</div>}
      <pre data-testid="voice-log" className="bg-background max-h-[60vh] overflow-auto rounded border p-1.5 text-[10px] leading-tight whitespace-pre-wrap">{ui.log.slice(-40).join("\n")}</pre>
    </aside>
  );
}
