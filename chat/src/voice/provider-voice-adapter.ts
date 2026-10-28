import { createVoiceSession, type RealtimeVoiceAdapter } from "@assistant-ui/react";
import { adapterFor } from "./registry";
import { cancelDelegate, delegate, delegateStatus, endVoiceSession, mintVoiceSession, postTranscript, postUsage, refreshVoiceToken, VoiceHttpError, type DelegateResult, type TranscriptItem } from "./api";
import type { LiveConnection, VoiceGrant, VoiceModelId } from "./types";

export type VoiceCallContext = { model: VoiceModelId; agentId: string; ensureThread: () => Promise<string>; onServerThread?: (id: string) => void };
export type PendingApproval = { callId: string; threadId: string; agentId: string; result: DelegateResult;
  /** call after the on-screen decision has resumed the run; the voice session then speaks the outcome */
  onResolved: () => Promise<void> };
export type VoiceUiHooks = {
  log: (line: string) => void;
  onGrantError: (e: VoiceHttpError) => void;
  onApproval: (p: PendingApproval) => void;
  onSession: (s: { grant: VoiceGrant; startedAt: number } | null, ended?: { seconds: number; cost_usd: number; reason: string }) => void;
  onBargeIn: (latencyMs: number) => void;
  onThreadChanged: () => void; // server wrote transcript / delegated run -> refetch thread
  expose?: (conn: LiveConnection | null) => void; // test hook
  onSpend?: (sessionUsd: number, totalUsd: number) => void;
  /** raw provider mode incl. "thinking" (assistant-ui only knows listening/speaking) — drives the voice overlay state */
  onMode?: (mode: "listening" | "thinking" | "speaking") => void;
  /** GĐ4: provider link state during a call (GoAway / network loss -> session resumption) */
  onLink?: (link: "ok" | "reconnecting" | "offline" | "resumed") => void;
  /** delegated Agent OS run lifecycle, shown as a card in the voice overlay */
  onDelegation?: (d: { callId: string; request: string; status: "running" | "approval" | "success" | "error" | "cancelled"; reply?: string | null }) => void;
};

/**
 * ProviderVoiceAdapter: the single assistant-ui RealtimeVoiceAdapter the front-end UI sees (adapters.voice).
 * It mints a session at Agent OS for the model chosen at runtime, then hands the call to the provider adapter from the registry.
 * Agent OS owns: allow-list (403), budget/minutes (402), delegation runs, HITL approvals, server-side transcript.
 */
export function createProviderVoiceAdapter(ctx: () => VoiceCallContext, ui: VoiceUiHooks): RealtimeVoiceAdapter {
  return {
    connect: ({ abortSignal }) => createVoiceSession({ abortSignal }, async (h) => {
      const c = ctx();
      // viewers holding voice.use cannot POST /threads -> let Agent OS create (and bind) the voice thread (G9)
      let threadId: string | null = await c.ensureThread().catch((e) => { ui.log(`thread create refused (${String(e).slice(0, 60)}) -> server creates the voice thread`); return null; });
      let grant: VoiceGrant;
      try { grant = await mintVoiceSession({ model: c.model, agent_id: c.agentId, thread_id: threadId }); }
      catch (e) { if (e instanceof VoiceHttpError) ui.onGrantError(e); throw e; }
      if (!threadId && grant.thread_id) c.onServerThread?.(grant.thread_id);
      const tid: string = threadId ?? grant.thread_id ?? "";
      const t0 = Date.now(); ui.onSession({ grant, startedAt: t0 });
      ui.log(`session ${grant.voice_session_id}: ${grant.provider}/${grant.model} via ${grant.transport}${grant.mock ? " [MOCK]" : " [REAL, ephemeral token]"}, max ${grant.max_session_s}s`);
      const ac = new AbortController(); abortSignal?.addEventListener("abort", () => ac.abort());
      // --- server-side transcript (design §8): batch finals; the delegated request itself is written by the run, not twice
      let queue: TranscriptItem[] = [];
      // --- delegated-call bookkeeping: what the model says around a call is attached to the run's answer (server), not a new turn;
      //     a barge-in while a run is in flight cancels it (proper run cancel + transcript note)
      const inflight = new Set<string>(); const cancelled = new Set<string>();
      let cur: { callId: string; resultAt?: number; preamble?: boolean } | null = null;
      const firstSeen = new Map<string, number>();
      const phaseFor = (itemId: string): Pick<TranscriptItem, "phase" | "call_id"> => {
        if (!cur || cancelled.has(cur.callId)) return {};
        const seen = firstSeen.get(itemId) ?? performance.now();
        if (!cur.preamble && (cur.resultAt === undefined || seen < cur.resultAt)) { cur.preamble = true; return { phase: "preamble", call_id: cur.callId }; }
        if (cur.resultAt !== undefined && seen >= cur.resultAt) { const id = cur.callId; cur = null; return { phase: "readback", call_id: id }; }
        return {};
      };
      const bargeCancel = (why: string) => {
        for (const id of inflight) {
          if (cancelled.has(id)) continue; cancelled.add(id);
          ui.log(`barge-in during delegation -> cancel ${id} (${why})`);
          cancelDelegate(id, "barge_in").then((r) => ui.log(`run ${String(r.run_id ?? "").slice(0, 8)} cancelled: ${r.cancelled}`)).catch((e) => ui.log(`cancel failed: ${String(e)}`));
        }
      };
      let flushT: ReturnType<typeof setTimeout> | undefined;
      const flush = async () => {
        clearTimeout(flushT); flushT = undefined; if (!queue.length) return;
        const items = queue; queue = [];
        try { const r = await postTranscript(grant.voice_session_id, items); ui.log(`transcript -> Agent OS: ${r.n} item(s) ${r.status}`); }
        catch (e) { ui.log(`transcript write failed: ${String(e)}`); }
      };
      const schedule = () => { if (!flushT) flushT = setTimeout(flush, 1500); };
      let conn: LiveConnection | null = null; let ended = false;
      const recent = new Map<string, { at: number; callId: string; p: Promise<DelegateResult | null> }>();
      const finish = async (reason: string) => {
        if (ended) return; ended = true; clearTimeout(maxT); await flush();
        const secs = (Date.now() - t0) / 1000;
        try { const r = await endVoiceSession(grant.voice_session_id, secs, reason); ui.onSession(null, { seconds: r.seconds, cost_usd: r.cost_usd, reason }); }
        catch { ui.onSession(null, { seconds: secs, cost_usd: 0, reason }); }
        ui.expose?.(null); ui.onThreadChanged();
      };
      const maxT = setTimeout(() => { ui.log(`max session length reached (${grant.max_session_s}s) -> auto end`); void finish("max_minutes"); conn?.close(); }, grant.max_session_s * 1000);
      const resolveLater = (callId: string) => async () => {
        let r = await delegateStatus(callId);
        for (let i = 0; i < 60 && !["success", "error", "interrupted"].includes(r.status); i++) { await new Promise((ok) => setTimeout(ok, 500)); r = await delegateStatus(callId); }
        ui.log(`approval resolved -> run ${r.status}: ${(r.reply ?? "").slice(0, 80)}`);
        ui.onDelegation?.({ callId, request: "", status: r.status === "success" ? "success" : "error", reply: r.reply });
        conn?.injectContext(r.status === "success" ? (r.reply ?? "Done.") : `The task ended with status ${r.status}.`);
        ui.onThreadChanged();
      };
      conn = await adapterFor(grant).connect({
        grant, signal: ac.signal, refreshCredential: () => refreshVoiceToken(grant.voice_session_id),
        instructions: "Speak briefly in the user's language. Hand every task that needs data, tools or actions to agent_os_delegate.",
        onEvent: async (e) => {
          if (h.isDisposed()) return;
          switch (e.type) {
            case "state":
              if (e.state === "connected") { h.setStatus({ type: "running" }); h.emitMode("listening"); ui.log(`connected (${e.detail ?? ""})`); ui.onLink?.(e.detail?.startsWith("resumed") ? "resumed" : "ok"); }
              if (e.state === "reconnecting") { ui.log(`reconnecting: ${e.detail ?? ""}`); ui.onLink?.(e.detail === "offline" ? "offline" : "reconnecting"); }
              if (e.state === "closed") { if (e.detail) ui.log(`closed: ${e.detail}`); await finish("closed"); h.end("finished"); }
              if (e.state === "error") { await finish("error"); h.end("error", e.detail); }
              break;
            case "mode": ui.onMode?.(e.mode); h.emitMode(e.mode === "thinking" ? "listening" : e.mode); break;
            case "volume": h.emitVolume(e.level); break;
            case "interrupted": ui.onBargeIn(e.latencyMs); ui.log(`barge-in: playback stopped in ${e.latencyMs} ms`); bargeCancel("interrupted"); break;
            case "user_speech_started": bargeCancel("user speech"); break;
            case "transcript":
              h.emitTranscript({ role: e.role, text: e.text, isFinal: e.final });
              if (e.role === "assistant" && !firstSeen.has(e.itemId)) firstSeen.set(e.itemId, performance.now());
              if (e.final) {
                const cut = e.truncated ? { truncated: true, played_ms: e.truncated.playedMs, generated_ms: e.truncated.generatedMs, full_chars: e.truncated.fullChars } : {};
                if (e.truncated) ui.log(`barge-in cut: saved ${e.text.length}/${e.truncated.fullChars} chars (played ${e.truncated.playedMs} of ${e.truncated.generatedMs} ms)`);
                queue.push({ role: e.role, text: e.text, t_end: (Date.now() - t0) / 1000, ...(e.role === "assistant" ? phaseFor(e.itemId) : {}), ...cut }); schedule();
              }
              break;
            case "tool_call": {
              // A8 finding: gemini-3.8-live (NON_BLOCKING tools) may emit the SAME agent_os_delegate twice (new call id, same args)
              // within ~1.5 s. One user request must start ONE Agent OS run: the duplicate gets the first call's result.
              const key = e.args.request.trim().toLowerCase(); const prev = recent.get(key);
              if (prev && Date.now() - prev.at < 15000) {
                ui.log(`duplicate delegate ${e.callId} (same request as ${prev.callId}) -> reusing run, no new run`);
                prev.p.then((r) => conn?.sendToolResult(e.callId, r?.reply ?? `Run ended: ${r?.status ?? "error"}`)).catch(() => {});
                break;
              }
              let settle!: (r: DelegateResult | null) => void; recent.set(key, { at: Date.now(), callId: e.callId, p: new Promise((ok) => { settle = ok; }) });
              inflight.add(e.callId); cur = { callId: e.callId };
              // drop the queued user line that became the delegated request (the run writes it as its input message)
              { const i = queue.map((q) => q.role).lastIndexOf("user"); if (i >= 0) queue.splice(i, 1); }
              await flush();
              ui.log(`delegate ${e.callId}: "${e.args.request}" -> Agent OS ${c.agentId} (thread ${tid.slice(0, 8)})`);
              ui.onDelegation?.({ callId: e.callId, request: e.args.request, status: "running" });
              try {
                const r = await delegate({ voice_session_id: grant.voice_session_id, call_id: e.callId, request: e.args.request, context: e.args.context });
                settle(r); inflight.delete(e.callId);
                const markResult = () => { if (cur?.callId === e.callId) cur.resultAt = performance.now(); };
                if (r.status === "cancelled" || cancelled.has(e.callId)) {
                  ui.log(`run ${r.run_id.slice(0, 8)} cancelled by barge-in -> silent tool result`);
                  conn?.sendToolResult(e.callId, "Cancelled: the user interrupted. Do not read any result.", { silent: true }); if (cur?.callId === e.callId) cur = null;
                  ui.onDelegation?.({ callId: e.callId, request: e.args.request, status: "cancelled" });
                } else if (r.status === "interrupted" && r.approval) {
                  ui.log(`run ${r.run_id.slice(0, 8)} paused for approval -> card on screen (no voice approval)`);
                  markResult(); conn?.sendToolResult(e.callId, "This needs your approval on screen. I will tell you the result once it is approved.");
                  ui.onApproval({ callId: e.callId, threadId: tid, agentId: c.agentId, result: r, onResolved: resolveLater(e.callId) });
                  ui.onDelegation?.({ callId: e.callId, request: e.args.request, status: "approval" });
                } else {
                  ui.log(`run ${r.run_id.slice(0, 8)} ${r.status}: ${(r.reply ?? "").slice(0, 80)}`);
                  markResult(); conn?.sendToolResult(e.callId, r.reply ?? `Run ended: ${r.status}`);
                  ui.onDelegation?.({ callId: e.callId, request: e.args.request, status: r.status === "success" ? "success" : "error", reply: r.reply });
                }
                ui.onThreadChanged();
              } catch (err) { settle(null); inflight.delete(e.callId); if (cur?.callId === e.callId) cur = null; conn?.sendToolResult(e.callId, `Agent OS error: ${String(err)}`); }
              break;
            }
            case "tool_call_cancelled":
              for (const id of e.callIds) if (inflight.has(id) && !cancelled.has(id)) { cancelled.add(id); ui.log(`provider cancelled ${id} -> cancel run`); void cancelDelegate(id, "provider_cancel").catch(() => {}); }
              break;
            case "usage":
              if (e.raw && !grant.mock) {
                try {
                  const r = await postUsage(grant.voice_session_id, e.raw);
                  ui.onSpend?.(r.session_usd, r.real_spend_usd);
                  if (r.stop) { ui.log(`A8 budget guard: $${r.real_spend_usd.toFixed(4)} ≥ $${r.abort_at} -> ending call`); await finish("budget"); conn?.close(); }
                } catch (err) { ui.log(`usage post failed: ${String(err)}`); }
              }
              break;
            case "error": ui.log(`error: ${e.error}`); if (e.fatal) { await finish("error"); h.end("error", e.error); } break;
          }
        },
      });
      ui.expose?.(conn);
      return {
        disconnect: () => { void finish("user"); conn?.close(); ac.abort(); },
        mute: () => conn?.setMuted(true),
        unmute: () => conn?.setMuted(false),
        sendText: (text: string) => conn?.sendText(text),
      };
    }),
  };
}
