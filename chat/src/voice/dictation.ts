// Composer Mic = dictation (ChatGPT/Gemini pattern: speech -> editable text in the composer, never auto-sent).
// Voice tech is fixed (20/10/2026 19:07): it reuses the SAME live model selected for voice (gpt-live-1 / gemini-3.8-live) through an
// Agent OS ephemeral session with purpose="dictation" (input transcription only). MOCK provider in tests. No other speech engine.
import type { DictationAdapter } from "@assistant-ui/react";
import { endVoiceSession, mintVoiceSession, VoiceHttpError } from "./api";
import { adapterFor } from "./registry";
import type { LiveConnection, VoiceGrant } from "./types";

export type DictationContext = () => { model: string | null; agentId: string };
export type DictationHooks = { log: (l: string) => void; onError: (e: unknown) => void };

/** Live UI state of the active dictation (waveform level, timer), read with useSyncExternalStore. */
type DictState = { active: boolean; startedAt: number; level: number; levels: number[]; model: string | null };
let state: DictState = { active: false, startedAt: 0, level: 0, levels: [], model: null };
const subs = new Set<() => void>();
const set = (p: Partial<DictState>) => { state = { ...state, ...p }; subs.forEach((f) => f()); };
export const dictationStore = { get: () => state, subscribe: (f: () => void) => { subs.add(f); return () => subs.delete(f); } };

declare global { interface Window { __aosDictation?: { say: (t: string) => void } } }

export class LiveDictationAdapter implements DictationAdapter {
  disableInputDuringDictation = true;
  private ctx: DictationContext; private hooks: DictationHooks;
  private active: DictationAdapter.Session | null = null;
  constructor(ctx: DictationContext, hooks: DictationHooks) { this.ctx = ctx; this.hooks = hooks; }
  /** X button: discard what was dictated (the composer restores its text from before the dictation). */
  cancelActive() { this.active?.cancel(); this.active = null; }

  listen(): DictationAdapter.Session {
    const { model, agentId } = this.ctx(); const hooks = this.hooks;
    const onSpeech = new Set<(r: DictationAdapter.Result) => void>(); const onStart = new Set<() => void>(); const onEnd = new Set<(r: DictationAdapter.Result) => void>();
    const items = new Map<string, string>(); const text = () => [...items.values()].map((x) => x.trim()).filter(Boolean).join(" ");
    const ac = new AbortController(); let conn: LiveConnection | null = null; let grant: VoiceGrant | null = null; const t0 = Date.now();
    let levelT: ReturnType<typeof setInterval> | undefined;
    const session: DictationAdapter.Session = {
      status: { type: "starting" },
      stop: async () => {
        if (session.status.type === "ended") return;
        conn?.endAudio?.();
        await new Promise((r) => setTimeout(r, grant?.mock ? 120 : 700));   // last inputTranscription chunk after audioStreamEnd
        const r = { transcript: text(), isFinal: true };
        if (r.transcript) onSpeech.forEach((f) => f(r));
        finish("stopped", "dictation-confirm"); onEnd.forEach((f) => f(r));
      },
      cancel: () => { if (session.status.type !== "ended") finish("cancelled", "dictation-cancel"); },
      onSpeechStart: (f) => { onStart.add(f); return () => onStart.delete(f); },
      onSpeechEnd: (f) => { onEnd.add(f); return () => onEnd.delete(f); },
      onSpeech: (f) => { onSpeech.add(f); return () => onSpeech.delete(f); },
    };
    const finish = (reason: "stopped" | "cancelled" | "error", why: string) => {
      session.status = { type: "ended", reason }; clearInterval(levelT); window.__aosDictation = undefined;
      try { conn?.close(); } catch { /* */ } ac.abort(); set({ active: false, level: 0, levels: [] });
      if (grant) void endVoiceSession(grant.voice_session_id, (Date.now() - t0) / 1000, why).catch(() => {});
      hooks.log(`dictation ${reason} (${why}): ${text().length} chars`);
    };
    this.active = session;
    set({ active: true, startedAt: t0, level: 0, levels: Array(24).fill(0), model });
    levelT = setInterval(() => set({ levels: [...state.levels.slice(1), state.level], level: state.level * 0.6 }), 80);
    (async () => {
      if (!model) throw new Error("no voice model selected");
      grant = await mintVoiceSession({ model, agent_id: agentId, thread_id: null, purpose: "dictation" });
      if (session.status.type === "ended") { void endVoiceSession(grant.voice_session_id, 0, "dictation-cancel").catch(() => {}); return; }
      hooks.log(`dictation session ${grant.voice_session_id}: ${grant.provider}/${grant.model}${grant.mock ? " [MOCK]" : ""} (input transcription)`);
      conn = await adapterFor(grant).connect({ grant, instructions: "", signal: ac.signal, onEvent: (e) => {
        if (session.status.type === "ended") return;
        if (e.type === "state" && e.state === "connected") { session.status = { type: "running" }; onStart.forEach((f) => f()); }
        else if (e.type === "volume") set({ level: Math.max(state.level, e.level) });
        else if (e.type === "transcript" && e.role === "user") { items.set(e.itemId, e.text); onSpeech.forEach((f) => f({ transcript: text(), isFinal: false })); }
        else if (e.type === "error" && e.fatal) { hooks.onError(new Error(e.error)); finish("error", "provider-error"); }
      } });
      const c = conn as unknown as { simulateUserSpeech?: (t: string) => void };
      if (c.simulateUserSpeech) window.__aosDictation = { say: (t) => c.simulateUserSpeech!(t) };   // mock-only test hook
    })().catch((e) => { hooks.onError(e instanceof VoiceHttpError ? e : new Error(String(e))); if (session.status.type !== "ended") finish("error", "start-failed"); });
    return session;
  }
}
