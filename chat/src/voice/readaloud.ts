// Message action "Read aloud": the SAME selected live model (gpt-live-1 / gemini-3.8-live) speaks the text verbatim through an
// Agent OS ephemeral session with purpose="readaloud" (no thread, no tools). MOCK provider in tests. No other speech engine (19:07 rule).
import type { SpeechSynthesisAdapter } from "@assistant-ui/react";
import { endVoiceSession, mintVoiceSession } from "./api";
import { adapterFor } from "./registry";
import type { LiveConnection, VoiceGrant } from "./types";

export class LiveReadAloudAdapter implements SpeechSynthesisAdapter {
  private ctx: () => { model: string | null; agentId: string }; private log: (l: string) => void;
  constructor(ctx: () => { model: string | null; agentId: string }, log: (l: string) => void) { this.ctx = ctx; this.log = log; }
  speak(text: string): SpeechSynthesisAdapter.Utterance {
    const subs = new Set<() => void>(); const ac = new AbortController(); const t0 = Date.now();
    let conn: LiveConnection | null = null; let grant: VoiceGrant | null = null;
    const u: SpeechSynthesisAdapter.Utterance = {
      status: { type: "starting" },
      cancel: () => end("cancelled"),
      subscribe: (f) => { subs.add(f); return () => subs.delete(f); },
    };
    const end = (reason: "finished" | "cancelled" | "error") => {
      if (u.status.type === "ended") return; u.status = { type: "ended", reason }; subs.forEach((f) => f());
      try { conn?.close(); } catch { /* */ } ac.abort();
      if (grant) void endVoiceSession(grant.voice_session_id, (Date.now() - t0) / 1000, `readaloud-${reason}`).catch(() => {});
      this.log(`read aloud ${reason}`);
      (window as { __aosReadAloud?: string }).__aosReadAloud = reason;
    };
    const { model, agentId } = this.ctx();
    (async () => {
      if (!model) throw new Error("no voice model selected");
      grant = await mintVoiceSession({ model, agent_id: agentId, thread_id: null, purpose: "readaloud" });
      this.log(`read aloud session ${grant.voice_session_id}: ${grant.provider}/${grant.model}${grant.mock ? " [MOCK]" : ""}`);
      conn = await adapterFor(grant).connect({ grant, instructions: "", signal: ac.signal, onEvent: (e) => {
        if (u.status.type === "ended") return;
        if (e.type === "state" && e.state === "connected") { conn?.setMuted(true); conn?.sendText(text.slice(0, 4000)); }
        else if (e.type === "mode" && e.mode === "speaking") { u.status = { type: "running" }; subs.forEach((f) => f()); (window as { __aosReadAloud?: string }).__aosReadAloud = "speaking"; }
        else if (e.type === "transcript" && e.role === "assistant" && e.final) end("finished");   // released only after the audio played
        else if (e.type === "state" && e.state === "closed") end("finished");
        else if (e.type === "error" && e.fatal) end("error");
      } });
    })().catch((e) => { this.log(`read aloud failed: ${String(e)}`); end("error"); });
    return u;
  }
}
