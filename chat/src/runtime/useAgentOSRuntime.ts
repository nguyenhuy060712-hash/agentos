// Front-end UI runtime: useLangGraphRuntime (MIT) -> Agent OS /api/v1 (server.py). NEVER AssistantCloud (`cloud` option unused).
import { RESEARCH_MODE, researchStore, withConfigurable } from "@/lib/research";
import i18n from "@/i18n";
import { useMemo, useRef } from "react";
import {
  CompositeAttachmentAdapter, SimpleTextAttachmentAdapter,
  type FeedbackAdapter, type RemoteThreadListAdapter,
} from "@assistant-ui/react";
import { unstable_createLangGraphStream, useLangGraphRuntime, type LangChainMessage, type LangGraphTupleMetadata } from "@assistant-ui/react-langgraph";
import { createAssistantStream } from "assistant-stream";
import { delegateTo, lgClient, moveThreadToProject, postFeedback } from "@/lib/agentos";
import { projectsStore } from "@/lib/projectsStore";
import { firstAgentMention } from "./mentions";
import type { DictationAdapter, RealtimeVoiceAdapter, SpeechSynthesisAdapter } from "@assistant-ui/react";
import { AgentOSDocumentAttachmentAdapter, AgentOSImageAttachmentAdapter, toStandardBlocks } from "./attachments";
import { subagentStore } from "./subagents";
import { adoptLocalVoiceIds, type LocalMsg } from "./voice-dedupe";
import { failureOf, type RunFailure } from "./run-failure";
import { awaitActiveRun, stopRun } from "./run-lifecycle";
import { turnClock } from "./turn-queue";
import { markThreadRead } from "@/lib/inboxStore";

/** thread_id -> graph_id ("agent-<id>"): an Agent OS thread is bound to one agent (server returns 409 otherwise). */
export const threadAgent = new Map<string, string>();
/** message id -> tuple metadata (run_id ...) captured from `messages-tuple` stream chunks, for feedback. */
export const messageMeta = new Map<string, LangGraphTupleMetadata>();
export const lastRun = { threadId: "", runId: "" };
/** Bind a thread to its agent from the server row (lg_threads.agent_id is authoritative once it ran), else metadata.graph_id. */
export function bindThread(t: { thread_id: string; metadata?: unknown; agent_id?: number | null }) {
  const g = t.agent_id ? `agent-${t.agent_id}` : (t.metadata as { graph_id?: string } | undefined)?.graph_id;
  if (g) threadAgent.set(t.thread_id, g);
  return g;
}

export type { RunFailure } from "./run-failure";
let failure: RunFailure = null; const failureSubs = new Set<() => void>();
export const runFailure = {
  get: () => failure,
  set: (f: RunFailure) => { failure = f; failureSubs.forEach((cb) => cb()); },
  subscribe: (cb: () => void) => { failureSubs.add(cb); return () => { failureSubs.delete(cb); }; },
};
const humanText = (m: LangChainMessage | undefined) => {
  const c = (m as { content?: unknown } | undefined)?.content;
  return typeof c === "string" ? c : Array.isArray(c) ? c.map((p: { type?: string; text?: string }) => (p?.type === "text" ? p.text ?? "" : "")).join("") : "";
};

/** GĐ3.7: ids of assistant messages the user stopped. react-langgraph 0.14.36 shows a stopped message as "complete"
 *  (it never fills cancelledMessageIds), so the stream wrapper records them; kept in localStorage (last 200) for reloads. */
const STOP_KEY = "aos_stopped_ids";
let stopped: string[] = (() => { try { return JSON.parse(localStorage.getItem(STOP_KEY) ?? "[]") as string[]; } catch { return []; } })();
const stoppedSubs = new Set<() => void>();
export const stoppedMessages = {
  has: (id: string | undefined) => !!id && stopped.includes(id),
  add: (id: string) => { if (stopped.includes(id)) return; stopped = [...stopped, id].slice(-200);
    try { localStorage.setItem(STOP_KEY, JSON.stringify(stopped)); } catch { /* private mode */ } stoppedSubs.forEach((cb) => cb()); },
  version: () => stopped,
  subscribe: (cb: () => void) => { stoppedSubs.add(cb); return () => { stoppedSubs.delete(cb); }; },
};
const aiIdOf = (ev: string, data: unknown): string | undefined => {
  const m = (ev.startsWith("messages") ? (Array.isArray(data) ? data[0] : undefined) : undefined) as { id?: string; type?: string } | undefined;
  return m && (m.type === "ai" || m.type === "AIMessageChunk") && typeof m.id === "string" ? m.id : undefined;
};

export const STREAM_MODES = ["messages-tuple", "updates", "values"] as const; // design C5 (server emits `values` per step, ids aligned: G2/G12 fixed)

type Opts = { agentRef: { current: string }; activeThread: { current: string | undefined }; voice?: RealtimeVoiceAdapter; log: (l: string) => void;
  /** GĐ7.1 assistant panel: its threads are source="assistant" (never listed in the normal chat); `owner` = only this user's threads; `extraInput` = added to every run input (page `context`) */
  mode?: { source: "chat" | "assistant"; owner?: number; extraInput?: () => Record<string, unknown> };
  /** composer Mic + Read aloud: both use the selected live model (see voice/dictation.ts, voice/readaloud.ts) */
  dictation?: DictationAdapter; speech?: SpeechSynthesisAdapter;
  /** GĐ3.5: model picked in the composer (null = the agent's own model; the server enforces the allow-list) */
  chatModel?: { current: string | null };
  /** chat mode picked in the composer -> configurable.chat_mode (wins over `model` on the server) */
  chatMode?: { current: string | null };
  /** GĐ3.6: follow-up chips shown after an answer */
  suggestions?: readonly { prompt: string }[];
  /** GĐ3 fix (a): the thread's current messages (ids, role, modality) so a refetch can merge spoken turns */
  localMessages?: () => readonly LocalMsg[] };

/** Typed title: the first user words, without inlined attachment bodies. */
export const titleFrom = (text: string) => {
  const t = text.replace(/<attachment[\s\S]*?(<\/attachment>|$)/g, " ").replace(/\s+/g, " ").trim();
  return t.length <= 60 ? t : t.slice(0, 57).replace(/\s+\S*$/, "") + "…";
};

export function useAgentOSRuntime({ agentRef, activeThread, voice, log, dictation, speech, chatModel, chatMode, suggestions, localMessages, mode }: Opts) {
  const source = mode?.source ?? "chat"; const owner = mode?.owner; const modeRef = useRef(mode); modeRef.current = mode;
  const threadList = useMemo<RemoteThreadListAdapter>(() => ({
    async list() {
      const ts = await lgClient.threads.search({ metadata: { source }, limit: 100, ...(owner ? { owner_id: owner } : {}) } as never);
      if (source === "chat") projectsStore.seed(ts as never);   // GĐ8.9: lg_threads.project_id of every row
      return {
        threads: ts.map((t) => {
          const md = (t.metadata ?? {}) as { title?: string; graph_id?: string; archived?: boolean; voice?: boolean; pinned?: boolean };
          const agent = bindThread(t as never);
          return { status: md.archived ? "archived" : "regular", remoteId: t.thread_id, externalId: t.thread_id, title: md.title,
                   lastMessageAt: t.updated_at ? new Date(t.updated_at) : undefined, custom: { voice: !!md.voice, agent, pinned: !!md.pinned } } as const;
        }),
      };
    },
    async initialize() {
      const graph_id = agentRef.current;
      const t = await lgClient.threads.create({ metadata: { source, graph_id } });
      threadAgent.set(t.thread_id, graph_id);
      // GĐ8.9: a new chat started while a project is open belongs to it (before the first run, so the run already gets the project)
      const pid = source === "chat" ? projectsStore.get().active : null;
      if (pid) { projectsStore.assign(t.thread_id, pid); await moveThreadToProject(t.thread_id, pid).catch(() => projectsStore.assign(t.thread_id, null)); }
      return { remoteId: t.thread_id, externalId: t.thread_id };
    },
    async rename(id, title) { // a typed title is kept; the voice placeholder stays replaceable by the first utterance (server)
      await lgClient.threads.update(id, { metadata: { title, title_auto: title === i18n.t("voiceCallTitle") } }); },
    async archive(id) { await lgClient.threads.update(id, { metadata: { archived: true } }); },
    async unarchive(id) { await lgClient.threads.update(id, { metadata: { archived: false } }); },
    // GĐ5a: pin = metadata.pinned (merged server-side; the sidebar shows pinned chats first)
    async updateCustom(id, custom) { await lgClient.threads.update(id, { metadata: { pinned: !!(custom as { pinned?: boolean } | undefined)?.pinned } }); },
    async delete(id) { await lgClient.threads.delete(id); threadAgent.delete(id); },
    async fetch(id) {
      const t = await lgClient.threads.get(id); const md = (t.metadata ?? {}) as { title?: string; archived?: boolean };
      return { status: md.archived ? "archived" : "regular", remoteId: id, externalId: id, title: md.title };
    },
    // 0đ title: first 60 chars of the first user message (no LLM, no Assistant Cloud title model).
    async generateTitle(id, msgs) {
      const first = msgs.find((m) => m.role === "user");
      const text = titleFrom(first?.content.map((p) => (p.type === "text" ? p.text : "")).join(" ") ?? "") || "…";
      await lgClient.threads.update(id, { metadata: { title: text } }).catch(() => {});
      return createAssistantStream((c) => { c.appendText(text); });
    },
  }), [agentRef, source, owner]);

  const feedback = useMemo<FeedbackAdapter>(() => ({
    submit: async ({ message, type }) => {
      // run_id from tuple metadata (live stream) or the thread's last run. obs.py maps an /api/v1 run id to its trace itself (G14 fixed).
      const tid = activeThread.current;
      let rid = (messageMeta.get(message.id)?.run_id as string | undefined) ?? (lastRun.threadId === tid ? lastRun.runId : "");
      if (!rid && tid) rid = String((await lgClient.runs.list(tid, { limit: 1 }))[0]?.run_id ?? "");
      const r = await postFeedback(rid, type === "positive" ? 1 : 0);
      log(`feedback ${type} run=${rid} -> HTTP ${r.status}`);
    },
  }), [activeThread, log]);

  return useLangGraphRuntime({
    unstable_allowCancellation: true,
    ...(suggestions ? { suggestions } : {}),
    unstable_threadListAdapter: threadList,
    stream: async function* (messages, config) {
      const { externalId } = await config.initialize();
      const tid = externalId!; activeThread.current = tid;
      let graph = threadAgent.get(tid) || agentRef.current; threadAgent.set(tid, graph);
      // Spoken turns carried by react-langgraph (additional_kwargs.modality) are already persisted server-side by
      // /api/voice/transcript (design §8) -> do not send them again (would duplicate them in the graph).
      const clean = messages.filter((m) => !(m as { additional_kwargs?: { modality?: string } }).additional_kwargs?.modality)
        .map((m) => (m.type === "human" ? toStandardBlocks(m) : m)) as LangChainMessage[];   // G4: standard image content blocks
      if (!clean.length && config.command == null && messages.length) return;
      // GĐ5a @mention: an empty chat is ROUTED to the mentioned agent (it becomes the chat's agent); in a chat that already has
      // an agent, the message is DELEGATED: the mentioned agent answers on a hidden child thread and both turns land here.
      const human = config.command == null ? [...clean].reverse().find((m) => m.type === "human") : undefined;
      const mention = human ? firstAgentMention(humanText(human)) : null;
      if (mention && mention.id !== graph) {
        const row = (await lgClient.threads.get(tid)) as { agent_id?: number | null };
        if (!row.agent_id) {
          graph = mention.id; threadAgent.set(tid, graph);
          await lgClient.threads.update(tid, { metadata: { graph_id: graph } }).catch(() => {});
          dispatchEvent(new Event("aos-thread-bound"));
        } else {
          runFailure.set(null);
          try {
            // GĐ5b: image attachments go along (standard image blocks, already uploaded to /api/files)
            const c = (human as { content?: unknown }).content;
            const withImages = Array.isArray(c) && c.some((p: { type?: string }) => p?.type === "image" || p?.type === "image_url") ? (c as unknown[]) : undefined;
            const d = await delegateTo({ thread_id: tid, agent_id: mention.id, text: humanText(human), message_id: human!.id, content: withImages }, config.abortSignal);
            yield { event: "values", data: d.values } as never;
          } catch (e) {
            if ((e as { name?: string })?.name !== "AbortError") runFailure.set(failureOf(humanText(human), String((e as Error)?.message ?? e)));
            throw e;
          }
          return;
        }
      }
      // GĐ9.P1: onDisconnect "continue" = a closed tab / dropped network does NOT cancel the run (it finishes server-side; reopening the thread shows the result).
      // Only an explicit Stop (abort of this stream while the page is alive) cancels it, through stopRun() below.
      // G1: stream_subgraphs -> sub-agent progress ("<mode>|<ns>" events) goes to the sub-agent store, not into the main thread
      const base = unstable_createLangGraphStream({ client: lgClient, assistantId: graph, streamMode: [...STREAM_MODES], onDisconnect: "continue", streamSubgraphs: true });
      const cancelNow = () => { void stopRun(lgClient, tid, lastRun.threadId === tid ? lastRun.runId : undefined); };
      const model = chatModel?.current; const chat_mode = chatMode?.current;
      const extra = modeRef.current?.extraInput?.();   // GĐ7.1: page context travels in the run input (server appends it to the user turn)
      const withCtx = extra && Object.keys(extra).length ? { ...config, state: { ...(config.state as object | undefined), ...extra } } : config;
      researchStore.adopt(tid);
      const cfg = withConfigurable(withCtx, { ...(model ? { model } : {}), ...(chat_mode ? { chat_mode } : {}), ...(researchStore.get(tid) ? { mode: RESEARCH_MODE } : {}) });   // GĐ7.7 deep research
      let got = false, done = false, failed = false, lastAi: string | undefined; runFailure.set(null);
      turnClock.start();   // GĐ11.36 turn clock: thinking -> tool -> writing
      try {
        for await (const ch of await base(clean, cfg as typeof config)) {
          got = true; turnClock.feed(String(ch.event), ch.data);
          if (subagentStore.route(String(ch.event), ch.data)) continue;
          lastAi = aiIdOf(String(ch.event), ch.data) ?? lastAi;
          yield ch;
        }
        done = true;
      } catch (e) {
        failed = true;
        const err = e as { name?: string; message?: string };
        if (!got && err?.name !== "AbortError") {
          const text = humanText([...clean].reverse().find((m) => m.type === "human"));
          if (text) runFailure.set(failureOf(text, String(err?.message ?? e)));
        }
        if ((e as { name?: string })?.name === "AbortError") { cancelNow(); if (lastAi) stoppedMessages.add(lastAi); }
        throw e;
      } finally {
        if (!done && !failed) { cancelNow(); if (lastAi) stoppedMessages.add(lastAi); }   // consumer stopped the generator = user pressed Stop
        subagentStore.finishAll(); turnClock.stop();
        if (activeThread.current === tid) void markThreadRead(tid);   // GĐ11.35: the chat the user is looking at is read when its run ends
      }
    },
    load: async (externalId) => {
      activeThread.current = externalId;
      // after a reload the in-memory binding is gone: resume/HITL must go to the thread's own agent (server 409 otherwise)
      if (!threadAgent.has(externalId)) bindThread(await lgClient.threads.get(externalId) as never);
      await awaitActiveRun(lgClient, externalId);   // GĐ9.P1: the run kept going after the tab was closed -> wait for it, then show the finished answer
      const st = await lgClient.threads.getState<{ messages: LangChainMessage[] }>(externalId, undefined, { subgraphs: true });
      subagentStore.fromState(st.tasks as never);   // G1: an interrupted sub-agent shows its progress after reload
      const msgs = adoptLocalVoiceIds(st.values.messages ?? [], localMessages?.() ?? []);
      return { messages: msgs, interrupts: st.tasks.flatMap((t) => t.interrupts ?? []) };   // {value,id,ns?} (G16/G1)
    },
    eventHandlers: {
      onMetadata: (m) => { const md = m as { run_id?: string; thread_id?: string }; if (md.run_id) { lastRun.runId = md.run_id; lastRun.threadId = md.thread_id ?? ""; } },
      onMessageChunk: (chunk, meta) => { if (chunk.id) messageMeta.set(chunk.id, meta); },
    },
    adapters: {
      attachments: new CompositeAttachmentAdapter([new AgentOSImageAttachmentAdapter(), new AgentOSDocumentAttachmentAdapter(), new SimpleTextAttachmentAdapter()]),
      feedback,
      ...(voice ? { voice } : {}),
      ...(dictation ? { dictation } : {}),
      ...(speech ? { speech } : {}),
    },
  });
}
