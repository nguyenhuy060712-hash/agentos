// Agent OS front-end UI (assistant-ui OSS, MIT, Base UI flavour). Static Vite SPA served at /chat/ by Agent OS. No Assistant Cloud.
// Layout (ChatGPT / Gemini web, Oct 2026): left sidebar = Agents + Chats; main = slim header, centered welcome, 768 px message column,
// floating composer (+, Mic = dictation, Voice/Send); Voice mode = full-screen overlay; Settings = modal with tabs.
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Dialog } from "@base-ui/react/dialog";
import { AssistantRuntimeProvider, AuiConfig, Tools, useAui, useAuiState, useVoiceState } from "@assistant-ui/react";
import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ThreadApprovalCard } from "@/components/agentos/ApprovalCard";
import { ThreadConnectCard } from "@/components/agentos/ConnectCard";
import { AppSidebar } from "@/components/agentos/AppSidebar";
import { AgentWelcome, BudgetBanner, CallEndedNotice, ChatHeader, DemoBanner } from "@/components/agentos/ChatChrome";
import { DevPanel } from "@/components/agentos/DevPanel";
import type { VoiceUiState } from "@/components/agentos/voice-state";
import type { Delegation } from "@/components/agentos/VoiceOverlay";
import { ChatUiContext, profileOf } from "@/components/agentos/ui-context";
// code-split: the voice overlay and the Settings modal are separate chunks, loaded on demand
const VoiceOverlay = lazy(() => import("@/components/agentos/VoiceOverlay").then((m) => ({ default: m.VoiceOverlay })));
const SettingsDialog = lazy(() => import("@/components/agentos/SettingsDialog").then((m) => ({ default: m.SettingsDialog })));
// GĐ5b: canvas side panel and the read-only shared-chat view are their own chunks too
const FilesPanel = lazy(() => import("@/components/agentos/FilesPanel").then((m) => ({ default: m.FilesPanel })));
const SubagentPane = lazy(() => import("@/components/agentos/SubagentPane").then((m) => ({ default: m.SubagentPane })));
const CanvasPanel = lazy(() => import("@/components/agentos/CanvasPanel").then((m) => ({ default: m.CanvasPanel })));
const ShareView = lazy(() => import("@/components/agentos/ShareView").then((m) => ({ default: m.ShareView })));
import { canvasStore, useCanvas } from "@/lib/canvasStore";
import { filesStore, useFilesOpen } from "@/lib/filesStore";
import { useAgentOSRuntime, threadAgent, bindThread } from "@/runtime/useAgentOSRuntime";
import { modesOf, readPicks, resolveMode, writePick } from "@/lib/chat-modes";
import { getChatConfig, getMe, lgClient, listAssistants, logout, modelName, setLang, setRO, type Assistant, type ChatConfig, type Me } from "@/lib/agentos";
import { getDev, setDevStored, useTheme } from "@/lib/theme";
import { getVoiceConfig, VoiceHttpError } from "@/voice/api";
import { createProviderVoiceAdapter, type PendingApproval } from "@/voice/provider-voice-adapter";
import { LiveDictationAdapter } from "@/voice/dictation";
import { LiveReadAloudAdapter } from "@/voice/readaloud";
import type { LiveConnection } from "@/voice/types";
import i18n from "@/i18n";
import { loadLimits } from "@/runtime/attachments";
import { setQueueMax } from "@/runtime/turn-queue";
import { useSubagentPane, subagentPane } from "@/lib/subagentPane";
import { makeToolkit } from "@/genui/toolkit";
import { mentionAgents } from "@/runtime/mentions";
import { ConnectionBanner, ChatQuotaBanner, RunFailureBanner } from "@/components/agentos/ChatStatus";
void loadLimits();

declare global { interface Window { __aosVoice?: { say: (t: string) => void; linkLoss?: (k?: "goAway" | "offline") => void; interrupt: () => void; conn: LiveConnection | null }; __aosLastConn?: LiveConnection } }

const useHash = () => {
  const [h, setH] = useState(location.hash || "#/");
  useEffect(() => { const f = () => setH(location.hash || "#/"); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return h;
};
const useDesktop = () => {
  const q = "(min-width: 768px)"; const [d, setD] = useState(() => matchMedia(q).matches);
  useEffect(() => { const m = matchMedia(q); const f = () => setD(m.matches); m.addEventListener("change", f); return () => m.removeEventListener("change", f); }, []);
  return d;
};

export default function App() {
  const [me, setMe] = useState<Me | null | "anon">(null);
  const [agents, setAgents] = useState<Assistant[]>([]);
  const hash = useHash();
  useEffect(() => { getMe().then((m) => { setRO(m); setMe(m); void i18n.changeLanguage(m.lang === "en" ? "en" : "vi"); }).catch(() => setMe("anon")); }, []);
  useEffect(() => { if (me && me !== "anon") listAssistants().then(setAgents); }, [me]);
  if (me === "anon") { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.hash); return null; }
  if (me && me.must_enroll_2fa) { location.href = "/settings?s=security"; return null; }   // GĐ6.7: workspace requires 2FA
  if (me && hash.startsWith("#/s/")) return <Suspense fallback={null}><ShareView token={decodeURIComponent(hash.slice(4))} /></Suspense>;
  if (!me || !agents.length) return <div className="text-muted-foreground p-6 text-sm" role="status">…</div>;
  return <Shell me={me} agents={agents} />;
}

type Bridge = { aui: ReturnType<typeof useAui> | null };
/** Lives inside the runtime provider: exposes the aui client and the current thread id to the shell. */
function RuntimeBridge({ bridge, onThread }: { bridge: Bridge; onThread: (id: string | undefined) => void }) {
  const aui = useAui(); bridge.aui = aui;
  const ext = useAuiState((s) => s.threadListItem?.externalId);
  useEffect(() => { onThread(ext); }, [ext, onThread]);
  return null;
}

/** Screen-reader status of the voice session (+ stable hooks for the E2E suite). */
function VoiceStatus() {
  const vs = useVoiceState(); const st = vs?.status?.type ?? "idle"; const mode = vs && "mode" in vs ? (vs as { mode?: string }).mode : undefined;
  return (
    <div data-testid="voice-bar" data-voice-status={st} data-voice-mode={mode ?? ""} className="sr-only" role="status" aria-live="polite">
      <span data-testid="voice-status">{st}{mode ? ` · ${mode}` : ""}</span>
    </div>
  );
}

function VoiceLayer(p: Omit<React.ComponentProps<typeof VoiceOverlay>, "approval"> & { approval: React.ReactNode }) {
  const vs = useVoiceState(); const st = vs?.status?.type ?? "idle";
  if (st !== "starting" && st !== "running") return null;
  return <Suspense fallback={null}><VoiceOverlay {...p} /></Suspense>;
}

function Shell({ me, agents: rawInit }: { me: Me; agents: Assistant[] }) {
  const [raw, setRawAgents] = useState(rawInit);
  const { t, i18n: i } = useTranslation();
  const hash = useHash(); const desktop = useDesktop();
  const allAgents = useMemo(() => raw.map((a) => profileOf(a, t)), [raw, t]);
  // GĐ3 (b): test agents (tok-*, bench-*, …) carry metadata.chat_visible=false -> not listed in the chat sidebar (still usable/openable)
  const agents = useMemo(() => { const v = allAgents.filter((a) => a.visible); return v.length ? v : allAgents; }, [allAgents]);
  const [agent, setAgent] = useState(agents[0].graph_id);
  const [threadId, setThreadId] = useState<string | undefined>();
  const [, setBindTick] = useState(0);   // threadAgent is a plain Map: re-render once a thread opened by link/reload is bound
  const agentRef = useRef(agent); agentRef.current = agent;
  const activeThread = useRef<string | undefined>(undefined);
  const bridge = useRef<Bridge>({ aui: null }).current;
  const effAgent = (threadId && threadAgent.get(threadId)) || agent;
  const profile = allAgents.find((a) => a.graph_id === effAgent) ?? agents[0];
  // GĐ3.5–3.7: per-agent chat config (allow-listed models, generative UI flag, quota)
  const [chatCfg, setChatCfg] = useState<ChatConfig | null>(null);
  const [chatCfgTick, setChatCfgTick] = useState(0);
  useEffect(() => { let live = true; getChatConfig(effAgent).then((c) => { if (live) { setChatCfg(c); setQueueMax(c.queue_max); } }).catch(() => { if (live) setChatCfg(null); }); return () => { live = false; }; }, [effAgent, chatCfgTick]);
  const [modelPick, setModelPick] = useState<Record<string, string>>(() => { try { return JSON.parse(localStorage.getItem("aos_chat_model") ?? "{}"); } catch { return {}; } });
  const chatModel = chatCfg ? (modelPick[effAgent] && chatCfg.models.some((m) => m.id === modelPick[effAgent]) ? modelPick[effAgent] : chatCfg.default_model) : null;
  const chatModelRef = useRef<string | null>(null);
  chatModelRef.current = chatCfg && chatModel && chatModel !== chatCfg.default_model ? chatModel : null;   // only an explicit pick is sent
  const onChatModel = useCallback((m: string) => setModelPick((p) => { const n = { ...p, [effAgent]: m }; localStorage.setItem("aos_chat_model", JSON.stringify(n)); return n; }), [effAgent]);
  // chat modes (Nhanh / Tiêu chuẩn / Suy nghĩ sâu): shown instead of the model picker when the server offers >= 2; sent per run as configurable.chat_mode
  const [modePick, setModePick] = useState<Record<string, string>>(() => readPicks());
  const chatModes = useMemo(() => modesOf(chatCfg), [chatCfg]);
  const chatMode = resolveMode(chatCfg, modePick, effAgent);
  const chatModeRef = useRef<string | null>(null);
  chatModeRef.current = chatMode;
  const onChatMode = useCallback((m: string) => setModePick(writePick(effAgent, m)), [effAgent]);
  const toolkitCfg = useMemo(() => AuiConfig({ tools: Tools({ toolkit: makeToolkit({ genui: chatCfg?.genui_enabled !== false }) }) }), [chatCfg?.genui_enabled]);
  const suggestions = useMemo(() => profile.followups.map((prompt) => ({ prompt })), [profile.followups]);
  const [vui, setVui] = useState<VoiceUiState>({ config: null, model: null, grantError: null, session: null, last: null, bargeIns: [], log: [] });
  const [lastFor, setLastFor] = useState<string | null>(null);
  const [vMode, setVMode] = useState<"listening" | "thinking" | "speaking" | null>(null);
  const [vLink, setVLink] = useState<"ok" | "reconnecting" | "offline" | "resumed">("ok");
  const [delegations, setDelegations] = useState<Delegation[]>([]);
  const [userModel, setUserModel] = useState<string | null>(() => localStorage.getItem("aos_voice_model"));
  const [theme, setTheme] = useTheme();
  const [dev, setDevState] = useState(getDev); const setDev = (v: boolean) => { setDevStored(v); setDevState(v); };
  const [sbOpen, setSbOpen] = useState(() => localStorage.getItem("aos_sidebar") !== "0");
  const [drawer, setDrawer] = useState(false);
  const pending = useRef<PendingApproval | null>(null);
  const addLog = useCallback((l: string) => {
    ((window as unknown as { __aosLog?: string[] }).__aosLog ??= []).push(l); // full log for E2E (dev panel shows the last lines)
    setVui((v) => ({ ...v, log: [...v.log.slice(-80), `${new Date().toLocaleTimeString("en-GB", { timeZone: "Asia/Bangkok" })} ${l}`] }));
  }, []);

  const refreshConfig = useCallback(() => {
    getVoiceConfig(effAgent).then((config) => setVui((v) => ({ ...v, config }))).catch((e) => addLog(`voice config: ${String(e)}`));
  }, [effAgent, addLog]);
  useEffect(() => { refreshConfig(); }, [refreshConfig]);
  const model = vui.config ? (userModel && vui.config.allowed.includes(userModel) ? userModel : vui.config.default_model) : null;
  const ctxRef = useRef({ model, agent: effAgent }); ctxRef.current = { model, agent: effAgent };
  const onThread = useCallback((id: string | undefined) => {
    activeThread.current = id; setThreadId(id);
    // a thread opened by link (#/c/<id>) or after reload: show ITS agent (header, follow-ups, model list), not the selected one
    if (id && !threadAgent.has(id)) void lgClient.threads.get(id).then((t) => { bindThread(t as never); setBindTick((x) => x + 1); }).catch(() => {});
  }, []);

  const voice = useMemo(() => {
    let reloadT: ReturnType<typeof setTimeout> | undefined;
    return createProviderVoiceAdapter(() => ({
      model: ctxRef.current.model ?? "", agentId: ctxRef.current.agent,
      ensureThread: async () => {
        const item = bridge.aui!.threads().item("main"); const fresh = !item.getState().remoteId;
        const r = await item.initialize();
        const id = r.externalId ?? r.remoteId; threadAgent.set(id, threadAgent.get(id) ?? ctxRef.current.agent);
        // voice thread: placeholder title until the first utterance (Agent OS sets it from what the user said)
        if (fresh) { await lgClient.threads.update(id, { metadata: { title_auto: true, voice: true } }).catch(() => {}); item.rename(i18n.t("voiceCallTitle")); }
        return id;
      },
      onServerThread: (id) => addLog(`voice thread created by Agent OS: ${id.slice(0, 8)} (listed after the call)`),
    }), {
      log: addLog,
      onGrantError: (e: VoiceHttpError) => { setVui((v) => ({ ...v, grantError: e })); addLog(`session refused: HTTP ${e.status} ${e.message}`); },
      onApproval: (p) => { pending.current = p; },
      onSession: (s, ended) => {
        if (s) { setDelegations([]); setVMode(null); } setVLink("ok");
        setVui((v) => ({ ...v, session: s, grantError: s ? null : v.grantError, last: ended ?? v.last }));
        if (!s && ended) { setLastFor(activeThread.current ?? null); refreshConfigRef.current(); void syncTitle(); }
      },
      onBargeIn: (ms) => setVui((v) => ({ ...v, bargeIns: [...v.bargeIns, ms] })),
      onThreadChanged: () => { clearTimeout(reloadT); reloadT = setTimeout(() => { void bridge.aui?.threads().reloadMainThread().catch(() => {}); }, 300); },
      onSpend: (session, total) => setVui((v) => ({ ...v, spend: { session, total } })),
      onMode: setVMode,
      onLink: (l) => { setVLink(l); if (l === "resumed") setTimeout(() => setVLink((x) => (x === "resumed" ? "ok" : x)), 2500); },
      onDelegation: (d) => setDelegations((xs) => { const i = xs.findIndex((x) => x.callId === d.callId); if (i < 0) return [...xs, d];
        const n = [...xs]; n[i] = { ...n[i], ...d, request: d.request || n[i].request }; return n; }),
      expose: (conn) => { if (conn) window.__aosLastConn = conn; window.__aosVoice = conn ? { conn, say: (x) => (conn as unknown as { simulateUserSpeech: (t: string) => void }).simulateUserSpeech?.(x), linkLoss: (k?: "goAway" | "offline") => (conn as unknown as { simulateLinkLoss?: (k?: string) => void }).simulateLinkLoss?.(k), interrupt: () => conn.interrupt() } : undefined; },
    });
  }, [addLog, bridge]);
  const refreshConfigRef = useRef(refreshConfig); refreshConfigRef.current = refreshConfig;
  /** after a call the server may have titled the thread from the first utterance -> reflect it in the sidebar */
  const syncTitle = async () => {
    const id = activeThread.current; if (!id) return;
    const md = (await lgClient.threads.get(id).catch(() => null))?.metadata as { title?: string } | undefined;
    const item = bridge.aui?.threads().item("main");
    if (md?.title && item && item.getState().title !== md.title) item.rename(md.title);
  };
  const dictation = useMemo(() => new LiveDictationAdapter(() => ({ model: ctxRef.current.model, agentId: ctxRef.current.agent }),
    { log: addLog, onError: (e) => { addLog(`dictation error: ${String(e)}`); if (e instanceof VoiceHttpError) setVui((v) => ({ ...v, grantError: e })); } }), [addLog]);
  const speech = useMemo(() => new LiveReadAloudAdapter(() => ({ model: ctxRef.current.model, agentId: ctxRef.current.agent }), addLog), [addLog]);

  const localMessages = useCallback(() => (bridge.aui?.thread().getState().messages ?? []).map((m) => ({ id: m.id, role: m.role, modality: m.metadata?.modality })), [bridge]);
  const runtime = useAgentOSRuntime({ agentRef, activeThread, voice, log: addLog, dictation, speech, chatModel: chatModelRef, chatMode: chatModeRef, suggestions, localMessages });
  const [attErr, setAttErr] = useState<string | null>(null);
  useEffect(() => { const f = (e: Event) => { setAttErr(String((e as CustomEvent).detail)); setTimeout(() => setAttErr(null), 6000); };
    addEventListener("aos-attachment-error", f); return () => removeEventListener("aos-attachment-error", f); }, []);
  // deep link from "Share · Copy link": #/c/<thread id>
  useEffect(() => { const m = hash.match(/^#\/c\/([\w-]{8,})/); if (!m || !bridge.aui) return;
    const id = m[1]; let n = 0; let tm: ReturnType<typeof setTimeout>;
    const go = () => { Promise.resolve().then(() => bridge.aui!.threads().switchToThread(id)).then(() => history.replaceState(null, "", "#/"))
      .catch(() => { if (++n < 8) tm = setTimeout(go, 400); }); };
    tm = setTimeout(go, 200); return () => clearTimeout(tm); }, [hash, bridge]);
  const lang = i.language === "en" ? "en" : "vi";
  const changeLang = (n: "vi" | "en") => { void i.changeLanguage(n); void setLang(n); document.documentElement.lang = n; };
  useEffect(() => { document.documentElement.lang = lang; }, [lang]);
  const newChat = (g?: string) => { if (g) setAgent(g); void bridge.aui?.threads().switchToNewThread(); setDrawer(false); };
  const newChatRef = useRef(() => newChat()); newChatRef.current = () => newChat();
  // GĐ5a: "@" mentions resolve against every agent (hidden test agents included, so old messages still show chips)
  useEffect(() => { mentionAgents.set(allAgents.map((a) => ({ id: a.graph_id, label: a.name }))); }, [allAgents]);
  // a fresh chat routed to the @mentioned agent: re-render header/model list for the agent it is now bound to
  useEffect(() => { const f = () => setBindTick((x) => x + 1); addEventListener("aos-thread-bound", f); return () => removeEventListener("aos-thread-bound", f); }, []);
  // mobile drawer (and the canvas of the previous chat) closes when another chat is opened; a new chat getting its id is not "another chat"
  const prevThread = useRef(threadId);
  useEffect(() => { setDrawer(false); if (prevThread.current && prevThread.current !== threadId) { canvasStore.close(); filesStore.close(); subagentPane.close(); } prevThread.current = threadId; }, [threadId]);
  const canvasOpen = !!useCanvas().doc; const filesOpen = useFilesOpen(); const subPane = useSubagentPane();
  const toggleSb = (o: boolean) => { localStorage.setItem("aos_sidebar", o ? "1" : "0"); setSbOpen(o); };
  const ui = useMemo(() => ({ agent: profile, voiceReady: !!vui.config?.can_use && !!model, voiceModel: model, cancelDictation: () => dictation.cancelActive(),
    chatModels: chatCfg?.picker ? chatCfg.models : [], chatModel, onChatModel, chatModes, chatMode, onChatMode, agents, newChat: () => newChatRef.current() }),
    [profile, vui.config?.can_use, model, dictation, chatCfg, chatModel, onChatModel, chatModes, chatMode, onChatMode, agents]);
  const sidebar = (onClose?: () => void) => (
    <AppSidebar me={me} agents={agents} activeAgent={effAgent} onNewChat={newChat} onClose={onClose} lang={lang} onLang={changeLang}
      theme={theme} onTheme={setTheme} onSettings={() => { setDrawer(false); location.hash = "#/settings"; }}
      onSignOut={() => { void logout().finally(() => { location.href = "/login"; }); }} />);
  const onDecided = () => { const p = pending.current; if (p && p.threadId === activeThread.current) { pending.current = null; void p.onResolved(); } };
  const live = !!vui.session;
  // GĐ3 case 9: the text-chat approval card sits in the message flow (right after the agent's turn), not under the composer.
  // Stable component identity (refs), so the card keeps its running/done state across App re-renders.
  const decidedRef = useRef(onDecided); decidedRef.current = onDecided;
  const liveRef = useRef(live); liveRef.current = live;
  const threadComponents = useMemo(() => ({
    Welcome: () => <AgentWelcome agent={profile} />,
    AfterMessages: () => (liveRef.current ? null : <><ThreadApprovalCard onDecided={() => decidedRef.current()} /><ThreadConnectCard /></>),   // GĐ11.29: a run paused for a missing connection
  }), [profile, live]);
  return (
    <TooltipProvider>
      <AssistantRuntimeProvider runtime={runtime} config={toolkitCfg}>
        <RuntimeBridge bridge={bridge} onThread={onThread} />
        <VoiceStatus />
        <ChatUiContext.Provider value={ui}>
          <div className="bg-background text-foreground flex h-dvh overflow-hidden">
            {desktop && sbOpen && <aside className="border-sidebar-border w-[268px] shrink-0 border-e" data-testid="sidebar">{sidebar(() => toggleSb(false))}</aside>}
            {!desktop && (
              <Dialog.Root open={drawer} onOpenChange={setDrawer}>
                <Dialog.Portal>
                  <Dialog.Backdrop className="fixed inset-0 z-40 bg-black/40 data-[open]:animate-in data-[open]:fade-in-0" />
                  <Dialog.Popup data-testid="sidebar" aria-label={t("chats")} className="fixed inset-y-0 start-0 z-50 w-[86vw] max-w-[320px] shadow-xl outline-none data-[open]:animate-in data-[open]:slide-in-from-left">
                    <Dialog.Title className="sr-only">{t("chats")}</Dialog.Title>{sidebar(() => setDrawer(false))}
                  </Dialog.Popup>
                </Dialog.Portal>
              </Dialog.Root>)}
            <main className="flex min-w-0 flex-1 flex-col">
              <ChatHeader agent={profile} modelLabel={modelName((chatModes.length >= 2 ? chatModes.find((m) => m.id === chatMode)?.model : null) ?? chatModel ?? profile.model)} sidebarOpen={desktop && sbOpen} onOpenSidebar={() => (desktop ? toggleSb(true) : setDrawer(true))} onNewChat={() => newChat()} dev={dev} onDev={setDev} />
              <DemoBanner />
              <BudgetBanner cfg={vui.config} refused={vui.grantError} />
              <ConnectionBanner />
              <RunFailureBanner />
              <ChatQuotaBanner cfg={chatCfg} onRefresh={() => setChatCfgTick((x) => x + 1)} />
              {vui.grantError && vui.grantError.status !== 402 && <p role="alert" data-testid="voice-error" className="text-destructive mx-auto mt-1 text-xs">{vui.grantError.message}</p>}
              {attErr && <div role="alert" data-testid="attachment-error" className="border-destructive/40 bg-destructive/10 text-destructive mx-auto mt-2 rounded-lg border px-3 py-1 text-sm">{attErr}</div>}
              {!live && lastFor && lastFor === threadId && <CallEndedNotice last={vui.last} />}
              <div className="min-h-0 flex-1"><Thread components={threadComponents} /></div>
            </main>
            {canvasOpen && <Suspense fallback={null}><CanvasPanel desktop={desktop} threadId={threadId} /></Suspense>}
            {filesOpen && !canvasOpen && <Suspense fallback={null}><FilesPanel desktop={desktop} threadId={threadId} /></Suspense>}
            {subPane && !canvasOpen && !filesOpen && <Suspense fallback={null}><SubagentPane desktop={desktop} description={subPane} /></Suspense>}
            {dev && <DevPanel ui={{ ...vui, model }} />}
          </div>
          <VoiceLayer agent={profile} model={model} mode={vMode} link={vLink} delegations={delegations} dev={dev} mock={vui.session?.grant.mock !== false}
            approval={live ? <ThreadApprovalCard onDecided={onDecided} /> : null}
            onInterrupt={() => window.__aosVoice?.interrupt()} say={(x) => window.__aosVoice?.say(x)} />
          <Suspense fallback={null}>
            {hash.startsWith("#/settings") && (
              <SettingsDialog open tab={hash.split("/")[2]} onClose={() => { location.hash = "#/"; }} agents={agents} allAgents={allAgents} onAgentsChanged={() => { listAssistants().then(setRawAgents); }} onSaved={refreshConfig} lang={lang} onLang={changeLang}
                theme={theme} onTheme={setTheme} dev={dev} onDev={setDev} voiceConfig={vui.config} model={model} agentName={profile.name}
                onModel={(m) => { localStorage.setItem("aos_voice_model", m); setUserModel(m); }} />)}
          </Suspense>
        </ChatUiContext.Provider>
      </AssistantRuntimeProvider>
    </TooltipProvider>
  );
}
