// Main-area chrome: slim header (agent + subtle model, share/more menu), centered welcome, budget banner, call-ended notice.
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Menu } from "@base-ui/react/menu";
import { useAui, useAuiState } from "@assistant-ui/react";
import { FolderTreeIcon, LinkIcon, MoreHorizontalIcon, Share2Icon, PanelLeftOpenIcon, PhoneOffIcon, SquarePenIcon, TerminalSquareIcon } from "lucide-react";
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button";
import type { VoiceConfig } from "@/voice/types";
import { AgentAvatar, type AgentProfile } from "./ui-context";
import { RO, getThreadBox } from "@/lib/agentos";
import { useSkillSaveReq, skillSaveStore } from "@/lib/skillSaveStore";
import { agentIdOf } from "./skill-save-helpers";
import { filesStore, useFilesOpen } from "@/lib/filesStore";
import { threadFiles } from "@/genui/files-helpers";
const ShareDialog = lazy(() => import("./ShareDialog").then((m) => ({ default: m.ShareDialog })));
const SkillSaveDialog = lazy(() => import("./SkillSaveDialog").then((m) => ({ default: m.SkillSaveDialog })));   // GĐ12.87

export function ChatHeader({ agent, modelLabel, sidebarOpen, onOpenSidebar, onNewChat, dev, onDev }: { agent: AgentProfile; modelLabel?: string; sidebarOpen: boolean; onOpenSidebar: () => void;
  onNewChat: () => void; dev: boolean; onDev: (v: boolean) => void }) {
  const { t } = useTranslation(); const ext = useAuiState((s) => s.threadListItem?.externalId); const [copied, setCopied] = useState(false); const [share, setShare] = useState(false); const skillReq = useSkillSaveReq();
  const messages = useAuiState((s) => s.thread.messages); const nFiles = useMemo(() => threadFiles(messages as never).length, [messages]); const filesOpen = useFilesOpen();
  const [hasBox, setHasBox] = useState(false);   // GĐ11.52: the thread has a virtual computer -> the side-panel button shows even before any file exists
  useEffect(() => { if (!ext) { setHasBox(false); return; } let live = true; getThreadBox(ext).then((b) => live && setHasBox(!!b)).catch(() => live && setHasBox(false)); return () => { live = false; }; }, [ext, messages.length]);
  const copy = async () => { const u = `${location.origin}/chat/#/c/${ext ?? ""}`; await navigator.clipboard?.writeText(u).catch(() => {}); setCopied(true); setTimeout(() => setCopied(false), 1500); };
  return (
    <header data-testid="chat-header" className="flex h-12 shrink-0 items-center gap-1.5 px-2 sm:px-3">
      {!sidebarOpen && (<>
        <TooltipIconButton tooltip={t("expandSidebar")} variant="ghost" size="icon" className="size-9" onClick={onOpenSidebar} data-testid="sidebar-open"><PanelLeftOpenIcon className="size-[18px]" /></TooltipIconButton>
        <TooltipIconButton tooltip={t("newChat")} variant="ghost" size="icon" className="size-9 md:hidden" onClick={onNewChat}><SquarePenIcon className="size-[18px]" /></TooltipIconButton>
      </>)}
      <div className="flex min-w-0 items-center gap-2 px-1" data-testid="header-agent" title={agent.role}>
        <AgentAvatar agent={agent} size={24} />
        <span className="truncate text-[15px] font-semibold">{agent.name}</span>
        {/* GĐ5b: the agent's chat model (picked or default); the live voice model is shown in the voice UI only */}
        {modelLabel && <span className="text-muted-foreground hidden truncate text-xs sm:inline" data-testid="header-model">{modelLabel}</span>}
      </div>
      <div className="ms-auto flex items-center gap-1">
        {(nFiles > 0 || hasBox) && (   /* GĐ8.6: files the agent created in this chat -> side panel (sandbox file tree) */
          <TooltipIconButton tooltip={t("fileOpenPanel")} variant="ghost" size="icon" className="size-9 aria-pressed:bg-muted" aria-pressed={filesOpen} onClick={() => filesStore.toggle()} data-testid="files-open">
            <FolderTreeIcon className="size-[18px]" /><span className="sr-only">{t("files")} ({nFiles})</span></TooltipIconButton>)}
        <Menu.Root>
          <Menu.Trigger data-testid="chat-more" aria-label={t("more")} className="hover:bg-muted data-[popup-open]:bg-muted focus-visible:ring-ring/50 flex size-9 items-center justify-center rounded-lg outline-none focus-visible:ring-2">
            <MoreHorizontalIcon className="size-[18px]" /></Menu.Trigger>
          <Menu.Portal><Menu.Positioner align="end" sideOffset={6} className="z-50">
            <Menu.Popup className="bg-popover text-popover-foreground w-56 rounded-xl border p-1.5 shadow-lg outline-none">
              <Menu.Item disabled={!ext} onClick={copy} data-testid="share-link" className="data-[highlighted]:bg-accent data-[disabled]:opacity-50 flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm outline-none">
                <LinkIcon className="size-4" />{copied ? t("linkCopied") : t("copyLink")}</Menu.Item>
              {!RO.on && <Menu.Item disabled={!ext} onClick={() => setShare(true)} data-testid="share-readonly" className="data-[highlighted]:bg-accent data-[disabled]:opacity-50 flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm outline-none">
                <Share2Icon className="size-4" />{t("shareReadonly")}</Menu.Item>}
              <Menu.Item onClick={() => onDev(!dev)} data-testid="dev-menu" className="data-[highlighted]:bg-accent flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm outline-none">
                <TerminalSquareIcon className="size-4" />{t("devPanel")}{dev ? " ✓" : ""}</Menu.Item>
            </Menu.Popup></Menu.Positioner></Menu.Portal>
        </Menu.Root>
      </div>
      {share && ext && <Suspense fallback={null}><ShareDialog threadId={ext} onClose={() => setShare(false)} /></Suspense>}
      {skillReq && ext && !RO.on && <Suspense fallback={null}><SkillSaveDialog threadId={ext} runId={skillReq.runId} agentId={agentIdOf(agent.graph_id)} onClose={() => skillSaveStore.close()} /></Suspense>}
    </header>
  );
}

/** Centered welcome for an empty chat: agent avatar + greeting + 3–4 suggestion chips (from the agent's metadata). */
export function AgentWelcome({ agent }: { agent: AgentProfile }) {
  const aui = useAui();
  const chips = agent.suggestions;
  const send = (x: string) => { aui.composer().setText(x); void aui.composer().send(); };
  return (
    <div data-testid="welcome" className="mb-6 flex flex-col items-center gap-4 px-2 text-center">
      <AgentAvatar agent={agent} size={52} />
      <h1 className="font-heading animate-in fade-in slide-in-from-bottom-1 text-2xl font-semibold tracking-tight text-balance sm:text-[28px]">{agent.greeting}</h1>
      {chips.length > 0 && (
        <div className="mt-1 flex max-w-2xl flex-wrap justify-center gap-2" data-testid="suggestions">
          {chips.map((c) => (
            <button key={c} type="button" onClick={() => send(c)} data-testid="suggestion"
              className="hover:bg-muted focus-visible:ring-ring/50 rounded-full border px-3.5 py-1.5 text-sm outline-none transition-colors focus-visible:ring-2">{c}</button>))}
        </div>)}
    </div>
  );
}

/** Budget only as a subtle banner when near (≥ 80 %) or over the daily voice limit, or after a 402. */
/** Demo account (customer trial): read-only notice + today's model-spend cap. */
export function DemoBanner() {
  const { t } = useTranslation(); if (!RO.demo) return null;
  return (
    <div role="status" data-testid="demo-banner"
      className="mx-auto mt-1 w-[min(48rem,calc(100%-2rem))] rounded-xl border border-sky-300/60 bg-sky-50 px-3 py-1.5 text-xs text-sky-900 dark:border-sky-500/30 dark:bg-sky-500/10 dark:text-sky-200">
      {t("demoBanner", { cap: RO.cap != null ? `$${RO.cap.toFixed(2)}` : "-" })}
    </div>
  );
}

export function BudgetBanner({ cfg, refused }: { cfg: VoiceConfig | null; refused: { status: number; message: string } | null }) {
  const { t } = useTranslation(); if (!cfg) return null;
  const { spent_usd_today: used, daily_budget_usd: limit, used_min_today: mins, daily_min_per_user: maxMin } = cfg.caps;
  const over = refused?.status === 402 || used >= limit || mins >= maxMin; const near = !over && (used >= 0.8 * limit || mins >= 0.8 * maxMin);
  if (!over && !near) return null;
  const u = `$${used.toFixed(2)}`, l = `$${limit.toFixed(2)}`;
  return (
    <div role="status" data-testid="quota-banner" data-level={over ? "over" : "near"}
      className="mx-auto mt-1 w-[min(48rem,calc(100%-2rem))] rounded-xl border border-amber-300/60 bg-amber-50 px-3 py-1.5 text-xs text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200">
      {over ? t("budgetOver", { used: u, limit: l }) : t("budgetNear", { used: u, limit: l })}
      {refused?.status === 402 && <span className="sr-only"> HTTP 402 {refused.message}</span>}
    </div>
  );
}

export function CallEndedNotice({ last }: { last: { seconds: number; reason: string } | null }) {
  const { t } = useTranslation(); if (!last) return null;
  return (
    <div data-testid="voice-last" data-reason={last.reason} role="status"
      className="text-muted-foreground mx-auto mt-1 flex w-fit items-center gap-1.5 rounded-full border px-3 py-1 text-xs">
      <PhoneOffIcon className="size-3.5" aria-hidden />{t("callEnded", { s: last.seconds.toFixed(0) })}{last.reason === "max_minutes" ? ` · ${t("callEndedMax")}` : ""}
    </div>
  );
}
