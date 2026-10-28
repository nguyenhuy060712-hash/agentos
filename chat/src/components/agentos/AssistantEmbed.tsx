// GĐ7.1 "Trợ lý Agent OS" side panel: this chat app in embed mode (/chat/?embed=assistant&ctx=<json>), loaded in an iframe by every Agent OS page (✦ / Ctrl+I).
// Compact header, no sidebar, fixed agent = the built-in assistant, page context sent with every message, approval cards for write actions (deepagents interrupt_on).
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { AssistantRuntimeProvider, AuiConfig, Tools } from "@assistant-ui/react";
import { HistoryIcon, SquarePenIcon } from "lucide-react";
import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import { ThreadListItems, ThreadListRoot } from "@/components/assistant-ui/elements/thread-list.aui";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ThreadApprovalCard } from "@/components/agentos/ApprovalCard";
import { ThreadConnectCard } from "@/components/agentos/ConnectCard";
import { ConnectionBanner, RunFailureBanner } from "@/components/agentos/ChatStatus";
import { ChatUiContext, type AgentProfile } from "@/components/agentos/ui-context";
import { ctxFromMessage, ctxFromSearch, ctxInput, ctxLabel, type PageCtx } from "@/components/agentos/assistant-embed";
import { getAssistant, getMe, setRO, type AssistantInfo, type Me } from "@/lib/agentos";
import { useAuiBridge } from "@/components/agentos/AssistantBridge";
import { useAgentOSRuntime } from "@/runtime/useAgentOSRuntime";
import { makeToolkit } from "@/genui/toolkit";
import i18n from "@/i18n";

const COLOR = "#7c3aed";

export function AssistantEmbed() {
  const { t } = useTranslation();
  const [me, setMe] = useState<Me | null>(null);
  const [info, setInfo] = useState<AssistantInfo | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    const ql = new URLSearchParams(location.search).get("lang");   // the Agent OS page passes its own UI language (cookie aos_lang) so the panel never flashes the other one
    if (ql === "en" || ql === "vi") void i18n.changeLanguage(ql);
    getMe().then((m) => { setRO(m); void i18n.changeLanguage(ql === "en" || ql === "vi" ? ql : m.lang === "en" ? "en" : "vi"); setMe(m); return getAssistant(); }).then(setInfo)
      .catch((e: { status?: number }) => { if (e?.status === 401) location.href = "/login?next=" + encodeURIComponent("/"); else setErr(true); });
  }, []);
  if (err) return <div role="alert" className="text-muted-foreground p-4 text-sm">{t("asstLoadFail")}</div>;
  if (!me || !info) return <div role="status" className="text-muted-foreground p-4 text-sm">…</div>;
  return <Panel me={me} info={info} />;
}

function Panel({ me, info }: { me: Me; info: AssistantInfo }) {
  const { t } = useTranslation();
  const [ctx, setCtx] = useState<PageCtx>(() => ctxFromSearch(location.search));
  const ctxRef = useRef(ctx); ctxRef.current = ctx;
  useEffect(() => {   // the parent page may update the context without reloading the iframe: postMessage {type:"aos-ctx", ctx} (same origin only)
    const f = (ev: MessageEvent) => { const c = ctxFromMessage(ev, location.origin); if (c) setCtx(c); };
    addEventListener("message", f); return () => removeEventListener("message", f);
  }, []);
  const agentRef = useRef(info.graph_id); agentRef.current = info.graph_id;
  const activeThread = useRef<string | undefined>(undefined);
  const [history, setHistory] = useState(false);
  const mode = useMemo(() => ({ source: "assistant" as const, owner: me.id, extraInput: () => ctxInput(ctxRef.current) }), [me.id]);
  const runtime = useAgentOSRuntime({ agentRef, activeThread, log: () => {}, mode });
  const bridge = useAuiBridge();
  const toolkitCfg = useMemo(() => AuiConfig({ tools: Tools({ toolkit: makeToolkit({ genui: true }) }) }), []);
  const agent: AgentProfile = useMemo(() => ({ graph_id: info.graph_id, assistant_id: info.assistant_id, name: t("asstTitle"), role: t("asstGreeting"), group: "Agent OS", greeting: t("asstGreeting"),
    suggestions: [t("asstS1"), t("asstS2"), t("asstS3"), t("asstS4")], color: COLOR, visible: false, followups: [], model: info.model }), [info, t]);
  const newChat = useCallback(() => { setHistory(false); void bridge.aui?.threads().switchToNewThread(); }, [bridge]);
  const ui = useMemo(() => ({ agent, voiceReady: false, voiceModel: null, cancelDictation: () => {}, chatModels: [], chatModel: null, onChatModel: () => {}, agents: [], newChat, compact: true }), [agent, newChat]);
  const threadComponents = useMemo(() => ({
    Welcome: () => (<div data-testid="asst-welcome" className="mb-4 px-2 text-center"><div className="text-2xl" aria-hidden style={{ color: COLOR }}>✦</div>
      <p className="mt-1 text-[15px] font-medium text-balance">{agent.greeting}</p><p className="text-muted-foreground mt-1 text-xs">{t("asstWrites")}</p></div>),
    AfterMessages: () => <><ThreadApprovalCard /><ThreadConnectCard /></>,
  }), [agent.greeting, t]);
  const label = ctxLabel(ctx);
  return (
    <TooltipProvider>
      <AssistantRuntimeProvider runtime={runtime} config={toolkitCfg}>
        <bridge.Probe />
        <ChatUiContext.Provider value={ui}>
          <div className="bg-background text-foreground relative flex h-dvh flex-col overflow-hidden" data-testid="assistant-panel" data-agent={info.graph_id}>
            {/* the Agent OS shell pane already shows the title + the one close button; this toolbar only holds chat actions */}
            <header className="flex h-10 shrink-0 items-center justify-end gap-1 border-b px-2" data-testid="asst-header">
              <button type="button" onClick={newChat} aria-label={t("newChat")} title={t("newChat")} data-testid="asst-new" className="hover:bg-muted flex size-8 items-center justify-center rounded-lg"><SquarePenIcon className="size-4" /></button>
              <button type="button" onClick={() => setHistory((h) => !h)} aria-label={t("asstHistory")} title={t("asstHistory")} aria-pressed={history} data-testid="asst-history" className="hover:bg-muted flex size-8 items-center justify-center rounded-lg"><HistoryIcon className="size-4" /></button>
            </header>
            {label && <div data-testid="asst-ctx" className="text-muted-foreground truncate border-b px-3 py-1 text-xs" title={JSON.stringify(ctx)}>{t("asstViewing")}: {label}</div>}
            <ConnectionBanner />
            <RunFailureBanner />
            <div className="min-h-0 flex-1"><Thread components={threadComponents} /></div>
            {history && (
              <div data-testid="asst-history-list" className="bg-background absolute inset-x-0 top-10 bottom-0 z-20 overflow-auto p-2" onClick={() => setHistory(false)}>
                <Suspense fallback={null}><ThreadListRoot><ThreadListItems /></ThreadListRoot></Suspense>
              </div>)}
          </div>
        </ChatUiContext.Provider>
      </AssistantRuntimeProvider>
    </TooltipProvider>
  );
}
