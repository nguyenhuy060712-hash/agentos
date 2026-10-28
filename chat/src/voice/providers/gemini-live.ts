import type { LiveConnectOptions, LiveConnection, LiveProviderAdapter, RealtimeEvent } from "../types";
import { assertRealCallsApproved } from "./guard";
import { PlaybackTracker, type Truncation } from "../playback-tracker";
import { ResumeState, durationMs, goAwayDelayMs } from "../resume";

/**
 * GeminiLiveAdapter (gemini-3.8-live). Browser <-> Google over WebSocket with a single-use EPHEMERAL token minted by Agent OS.
 * The token LOCKS the whole setup (model, system prompt, tools=[agent_os_delegate], transcription, VAD, resumption, compression),
 * so the browser only sends {setup:{model}}. Endpoint/fields verified against https://ai.google.dev/api/live (updated 2026-10-09)
 * and https://ai.google.dev/gemini-api/docs/ephemeral-tokens (fetched 20/10/2026):
 *   wss://…/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained?access_token=<token.name>
 * Audio: 16 kHz PCM16 mono up (realtimeInput.audio), 24 kHz PCM16 down (serverContent.modelTurn.parts[].inlineData).
 * Barge-in: server VAD -> serverContent.interrupted -> we flush the playback queue immediately. The saved assistant transcript
 * is cut to what was actually PLAYED (PlaybackTracker: played audio duration -> outputTranscription chunks), flagged `truncated`.
 */
export const GEMINI_WS = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained";

type Part = { inlineData?: { mimeType: string; data: string }; text?: string; thought?: boolean };
export type GeminiMsg = {
  setupComplete?: object;
  serverContent?: { interrupted?: boolean; turnComplete?: boolean; generationComplete?: boolean; waitingForInput?: boolean; interactionStatus?: string;
                    inputTranscription?: { text?: string }; interimInputTranscription?: { text?: string }; outputTranscription?: { text?: string };
                    modelTurn?: { parts?: Part[] } };
  toolCall?: { functionCalls?: { id: string; name: string; args?: Record<string, unknown> }[] };
  toolCallCancellation?: { ids?: string[] };
  usageMetadata?: Record<string, unknown> & { promptTokenCount?: number; responseTokenCount?: number; totalTokenCount?: number };
  voiceActivity?: { type?: string };
  goAway?: { timeLeft?: string };
  sessionResumptionUpdate?: { newHandle?: string; resumable?: boolean };
};

/** Stateful transcript assembler + pure mapping Gemini server message -> normalised events (unit-tested). */
export class GeminiMapper {
  private turn = 0; private user = ""; private asst = ""; private userOpen = false; private asstOpen = false;
  /** every top-level key / serverContent key seen (A8: "real event names") */
  readonly seen = new Set<string>();
  private finishUser(out: RealtimeEvent[]) { if (this.userOpen && this.user.trim()) out.push({ type: "transcript", role: "user", text: this.user.trim(), final: true, itemId: `u${this.turn}` }); this.user = ""; this.userOpen = false; }
  /** cut: what was actually played when the turn was interrupted (null = everything was heard) */
  private finishAsst(out: RealtimeEvent[], cut?: Truncation | null) {
    if (this.asstOpen && this.asst.trim()) {
      out.push(cut ? { type: "transcript", role: "assistant", text: cut.text, final: true, itemId: `a${this.turn}`, truncated: { playedMs: cut.playedMs, generatedMs: cut.generatedMs, fullChars: cut.fullChars } }
                   : { type: "transcript", role: "assistant", text: this.asst.trim(), final: true, itemId: `a${this.turn}` });
    }
    this.asst = ""; this.asstOpen = false;
  }
  /** cut: PlaybackTracker.cut() taken at the moment `serverContent.interrupted` arrived (before the queue is flushed) */
  map(m: GeminiMsg, cut?: Truncation | null): RealtimeEvent[] {
    const out: RealtimeEvent[] = [];
    for (const k of Object.keys(m)) this.seen.add(k);
    for (const k of Object.keys(m.serverContent ?? {})) this.seen.add(`serverContent.${k}`);
    if (m.setupComplete) out.push({ type: "state", state: "connected", detail: "setupComplete" });
    if (m.voiceActivity?.type === "ACTIVITY_START") out.push({ type: "user_speech_started", at: performance.now() });
    const sc = m.serverContent;
    if (sc?.inputTranscription?.text) { this.userOpen = true; this.user += sc.inputTranscription.text; out.push({ type: "transcript", role: "user", text: this.user, final: false, itemId: `u${this.turn}` }); }
    if (sc?.interrupted) { this.finishAsst(out, cut); out.push({ type: "interrupted", latencyMs: -1 }, { type: "mode", mode: "listening" }); }
    const hasAudio = sc?.modelTurn?.parts?.some((p) => p.inlineData?.mimeType?.startsWith("audio/"));
    if (hasAudio || sc?.outputTranscription?.text) { this.finishUser(out); }
    if (hasAudio) out.push({ type: "mode", mode: "speaking" });
    if (sc?.outputTranscription?.text) { this.asstOpen = true; this.asst += sc.outputTranscription.text; out.push({ type: "transcript", role: "assistant", text: this.asst, final: false, itemId: `a${this.turn}` }); }
    if (sc?.turnComplete) { this.finishUser(out); this.finishAsst(out); out.push({ type: "mode", mode: "listening" }); this.turn++; }
    for (const fc of m.toolCall?.functionCalls ?? []) {
      this.finishUser(out);
      if (fc.name === "agent_os_delegate") out.push({ type: "tool_call", callId: fc.id, name: fc.name, args: { request: String(fc.args?.request ?? ""), context: fc.args?.context as string | undefined } });
    }
    if (m.toolCallCancellation?.ids?.length) out.push({ type: "tool_call_cancelled", callIds: m.toolCallCancellation.ids });
    if (m.usageMetadata) out.push({ type: "usage", inputTokens: m.usageMetadata.promptTokenCount, outputTokens: m.usageMetadata.responseTokenCount, raw: m.usageMetadata });
    // goAway is handled by the adapter (proactive resume), not surfaced as a state here
    return out;
  }
}
/** Back-compat helper for tests: stateless single-message mapping. */
export function mapGeminiServerMessage(m: GeminiMsg, _turn?: { n: number }): RealtimeEvent[] { void _turn; return new GeminiMapper().map(m); }

const b64ToBytes = (b: string) => Uint8Array.from(atob(b), (c) => c.charCodeAt(0));
function bytesToB64(u: Uint8Array) { let s = ""; for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode(...u.subarray(i, i + 0x8000)); return btoa(s); }

export type LiveMetrics = { firstAudioAfterSpeechEndMs: number[]; bargeInServerMs: number[]; bargeInStopMs: number[]; connectMs: number; setupMs: number;
  seen: string[]; outPcmBytes: number; inSeconds: number; resumes?: number; goAways?: number };

export class GeminiLiveAdapter implements LiveProviderAdapter {
  readonly id = "google";
  async connect(o: LiveConnectOptions): Promise<LiveConnection & { metrics: () => LiveMetrics; dumpOutputPcm: () => string }> {
    assertRealCallsApproved("google");
    if (o.grant.mock) throw new Error("grant is mock; refusing to open a provider socket");
    const t0 = performance.now();
    const mapper = new GeminiMapper();
    const metrics: LiveMetrics = { firstAudioAfterSpeechEndMs: [], bargeInServerMs: [], bargeInStopMs: [], connectMs: 0, setupMs: 0, seen: [], outPcmBytes: 0, inSeconds: 0 };
    // ---- mic (always open while the call runs) -> 16 kHz PCM16
    const mic = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    const inCtx = new AudioContext({ sampleRate: 16000 });
    await inCtx.audioWorklet.addModule(new URL("worklets/pcm-capture.js", document.baseURI).href);
    const src = inCtx.createMediaStreamSource(mic); const node = new AudioWorkletNode(inCtx, "pcm-capture"); src.connect(node);
    // ---- playback 24 kHz
    const outCtx = new AudioContext({ sampleRate: 24000 }); await outCtx.resume();
    let playHead = 0; const sources = new Set<AudioBufferSourceNode>(); const outChunks: Uint8Array[] = [];
    let tracker = new PlaybackTracker();   // what of the current assistant turn has actually been played
    let held: { e: Extract<RealtimeEvent, { type: "transcript" }>; tracker: PlaybackTracker; timer: ReturnType<typeof setTimeout> } | null = null;
    const releaseHeld = (cut: Truncation | null) => {
      if (!held) return; const h = held; held = null; clearTimeout(h.timer);
      o.onEvent(cut ? { ...h.e, text: cut.text, truncated: { playedMs: cut.playedMs, generatedMs: cut.generatedMs, fullChars: cut.fullChars } } : h.e);
    };
    const flush = () => { for (const s of sources) { try { s.stop(); } catch { /* */ } } sources.clear(); playHead = 0; };
    const playing = () => sources.size > 0;
    // ---- local energy VAD (metrics only; turn-taking is the server's automatic VAD)
    let speaking = false; let lastLoud = 0; let speechEndAt = 0; let onsetAt = 0; let waitingFirstAudio = false; let bargeOnset = 0;
    let muted = false; let closed = false; let ready = false; const pending: string[] = []; let buf: Int16Array[] = []; let bufLen = 0;
    // GĐ4: the socket is replaceable (session resumption after GoAway / network loss); mic + playback survive a reconnect
    const resume = new ResumeState(o.grant.credential.expires_at);
    let token = o.grant.credential.value;
    const wsUrl = () => `${(o.grant as { ws_url?: string }).ws_url ?? GEMINI_WS}?access_token=${encodeURIComponent(token)}`;
    let ws!: WebSocket; let swapping = false; let resuming = false; let resumedNow = false; let goAwayTimer: ReturnType<typeof setTimeout> | null = null; let turnOpen = false;
    const PENDING_MAX = 64;   // only control messages are kept while reconnecting; audio frames are dropped (the user is told)
    const send = (x: unknown) => {
      const j = JSON.stringify(x);
      if (ready && ws.readyState === WebSocket.OPEN) ws.send(j);
      else if (!(x as { realtimeInput?: { audio?: unknown } }).realtimeInput?.audio && pending.length < PENDING_MAX) pending.push(j);
    };
    node.port.onmessage = (ev: MessageEvent<Float32Array>) => {
      if (closed) return;
      const f = ev.data; let e = 0; const pcm = new Int16Array(f.length);
      for (let i = 0; i < f.length; i++) { const v = Math.max(-1, Math.min(1, f[i])); pcm[i] = v * 0x7fff; e += v * v; }
      const rms = Math.sqrt(e / f.length); const now = performance.now();
      o.onEvent({ type: "volume", level: Math.min(1, rms * 6) });
      if (rms > 0.02) { lastLoud = now; if (!speaking) { speaking = true; onsetAt = now; if (playing()) bargeOnset = now; } }
      else if (speaking && now - lastLoud > 350) { speaking = false; speechEndAt = lastLoud; waitingFirstAudio = true; }
      if (muted) pcm.fill(0);
      buf.push(pcm); bufLen += pcm.length; metrics.inSeconds += pcm.length / 16000;
      if (bufLen >= 1600) { // 100 ms frames
        const all = new Int16Array(bufLen); let off = 0; for (const b of buf) { all.set(b, off); off += b.length; } buf = []; bufLen = 0;
        if (ready) send({ realtimeInput: { audio: { data: bytesToB64(new Uint8Array(all.buffer)), mimeType: "audio/pcm;rate=16000" } } });
      }
    };
    void onsetAt;
    o.onEvent({ type: "state", state: "connecting" });
    const reconnect = (why: string) => {
      if (closed || swapping) return;
      if (goAwayTimer) { clearTimeout(goAwayTimer); goAwayTimer = null; }
      const delay = resume.next();
      if (delay === null) { closed = true; cleanup(); o.onEvent({ type: "state", state: "closed", detail: `connection lost (${why}); could not resume`.slice(0, 200) }); return; }
      swapping = true; ready = false;
      o.onEvent({ type: "state", state: "reconnecting", detail: navigator.onLine === false ? "offline" : why });
      const old = ws; try { old.onclose = null; old.onmessage = null; old.close(1000, "resume"); } catch { /* */ }
      const go = async () => {
        if (closed) return;
        // each Gemini ephemeral token opens ONE connection (verified on #83: 1011 "Token has been used too many times"), so ask Agent OS for a new locked one
        if (o.refreshCredential) { try { const c = await o.refreshCredential(); token = c.value; resume.setExpiry(c.expires_at); } catch (err) { o.onEvent({ type: "error", error: `token refresh failed: ${String(err)}`, fatal: false }); } }
        if (closed) return; swapping = false; openWs(resume.handle);
      };
      // offline: wait for the network (bounded by the token lifetime) instead of burning retries
      if (navigator.onLine === false) { const on = () => { window.removeEventListener("online", on); setTimeout(() => void go(), 200); }; window.addEventListener("online", on); }
      else setTimeout(() => void go(), delay);
    };
    const onOffline = () => { if (!closed) o.onEvent({ type: "state", state: "reconnecting", detail: "offline" }); };
    window.addEventListener("offline", onOffline);
    const openWs = (handle: string | null) => {
      resuming = !!handle; ws = new WebSocket(wsUrl());
      ws.onopen = () => {
        if (!metrics.connectMs) metrics.connectMs = Math.round(performance.now() - t0);
        ws.send(JSON.stringify({ setup: { model: `models/${o.grant.model}`, ...(handle ? { sessionResumption: { handle } } : {}) } }));
      };
      ws.onmessage = onMessage;
      ws.onerror = () => o.onEvent({ type: "error", error: "websocket error", fatal: false });
      ws.onclose = (ev) => {
        if (closed || swapping) return;
        // 1000 from the server after GoAway, 1006 on network loss, 1011 internal: resume while we hold a handle and the token is valid
        if (resume.handle && resume.tokenValid()) { reconnect(`ws close ${ev.code}`); return; }
        closed = true; cleanup(); o.onEvent({ type: "state", state: "closed", detail: `ws close ${ev.code} ${ev.reason}`.slice(0, 200) });
      };
    };
    const onMessage = async (ev: MessageEvent) => {
      const msg = JSON.parse(typeof ev.data === "string" ? ev.data : await (ev.data as Blob).text()) as GeminiMsg;
      if (msg.setupComplete) {
        ready = true; if (!metrics.setupMs) metrics.setupMs = Math.round(performance.now() - t0);
        for (const j of pending.splice(0)) ws.send(j);
        if (resuming) { resuming = false; resume.succeeded(); metrics.resumes = resume.resumes; resumedNow = true; }
      }
      if (msg.sessionResumptionUpdate) resume.update(msg.sessionResumptionUpdate);
      if (msg.serverContent?.modelTurn || msg.toolCall) turnOpen = true;
      if (msg.serverContent?.turnComplete || msg.serverContent?.interrupted) turnOpen = false;
      if (msg.goAway && !goAwayTimer) {
        const left = durationMs(msg.goAway.timeLeft);
        metrics.goAways = (metrics.goAways ?? 0) + 1;
        o.onEvent({ type: "state", state: "reconnecting", detail: `goAway ${left} ms` });
        const due = Date.now() + left;
        // finish the sentence being spoken if there is time, then move to a fresh connection of the same session
        const tick = () => { goAwayTimer = null; if (turnOpen || playing()) { if (Date.now() < due - 1200) { goAwayTimer = setTimeout(tick, 200); return; } } reconnect("goAway"); };
        goAwayTimer = setTimeout(tick, goAwayDelayMs(left, false));
      }
      const dictation = o.grant.purpose === "dictation";   // composer Mic: only inputTranscription matters, nothing is played
      for (const part of dictation ? [] : msg.serverContent?.modelTurn?.parts ?? []) {
        if (!part.inlineData?.mimeType?.startsWith("audio/")) continue;
        const bytes = b64ToBytes(part.inlineData.data); outChunks.push(bytes); metrics.outPcmBytes += bytes.length;
        if (waitingFirstAudio && speechEndAt) { metrics.firstAudioAfterSpeechEndMs.push(Math.round(performance.now() - speechEndAt)); waitingFirstAudio = false; }
        const pcm = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength >> 1); const ab = outCtx.createBuffer(1, pcm.length, 24000); const ch = ab.getChannelData(0);
        for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 0x8000;
        const s = outCtx.createBufferSource(); s.buffer = ab; s.connect(outCtx.destination);
        playHead = Math.max(playHead, outCtx.currentTime + 0.02); s.start(playHead); tracker.audio(playHead, ab.duration); playHead += ab.duration; sources.add(s); s.onended = () => sources.delete(s);
      }
      if (msg.serverContent?.outputTranscription?.text) tracker.text(msg.serverContent.outputTranscription.text);
      const now = outCtx.currentTime;
      // barge-in while the tail of a FINISHED turn is still playing (generation is faster than realtime, so turnComplete usually
      // arrives first and the server sends no `interrupted`): stop playback locally and save only what was heard
      const localBarge = msg.voiceActivity?.type === "ACTIVITY_START" && held && playing();
      const cut = msg.serverContent?.interrupted ? tracker.cut(now) : undefined;
      if (msg.serverContent?.interrupted || localBarge) {
        const t = performance.now(); flush(); const stopped = performance.now();
        if (bargeOnset) { metrics.bargeInServerMs.push(Math.round(t - bargeOnset)); metrics.bargeInStopMs.push(Math.round(stopped - bargeOnset)); bargeOnset = 0; }
        if (held) releaseHeld(held.tracker.cut(now));
        if (localBarge) { o.onEvent({ type: "interrupted", latencyMs: metrics.bargeInStopMs.at(-1) ?? Math.round(stopped - t) }); o.onEvent({ type: "mode", mode: "listening" }); }
      }
      if (msg.serverContent?.interrupted) tracker.reset();
      for (const e of mapper.map(msg, cut)) {
        if (e.type === "state" && e.state === "connected" && resumedNow) { resumedNow = false; e.detail = `resumed #${resume.resumes}`; }
        if (e.type === "interrupted") e.latencyMs = metrics.bargeInStopMs.at(-1) ?? -1;
        // an assistant final whose audio is still queued is held until it has really been played (or is cut by a barge-in)
        if (e.type === "transcript" && e.role === "assistant" && e.final && !e.truncated && playHead > outCtx.currentTime + 0.05) {
          if (held) releaseHeld(null);
          const ht = tracker; tracker = new PlaybackTracker();
          held = { e, tracker: ht, timer: setTimeout(() => releaseHeld(null), (playHead - outCtx.currentTime) * 1000 + 80) };
          continue;
        }
        if (e.type === "transcript" && e.role === "assistant" && e.final) tracker.reset();
        o.onEvent(e);
      }
    };
    openWs(null);
    // mic unplugged / revoked: end the call cleanly (no silent dead call)
    mic.getAudioTracks().forEach((tr) => { tr.onended = () => { if (!closed) { o.onEvent({ type: "error", error: "microphone disconnected", fatal: false }); conn.close(); } }; });
    const onVis = () => { if (document.visibilityState === "visible") { void outCtx.resume().catch(() => {}); void inCtx.resume().catch(() => {}); } };
    document.addEventListener("visibilitychange", onVis);
    const cleanup = () => { window.removeEventListener("offline", onOffline); document.removeEventListener("visibilitychange", onVis); if (goAwayTimer) clearTimeout(goAwayTimer); if (held) releaseHeld(held.tracker.cut(outCtx.currentTime)); flush(); try { node.disconnect(); src.disconnect(); } catch { /* */ } mic.getTracks().forEach((t) => t.stop()); void inCtx.close(); void outCtx.close(); };
    o.signal.addEventListener("abort", () => conn.close());
    const conn = {
      sendText: (text: string) => send({ realtimeInput: { text } }),
      sendToolResult: (id: string, output: string, opts?: { silent?: boolean }) =>
        send({ toolResponse: { functionResponses: [{ id, name: "agent_os_delegate", response: { result: output.slice(0, 2000) }, scheduling: opts?.silent ? "SILENT" : "WHEN_IDLE" }] } }),
      injectContext: (text: string) => send({ clientContent: { turns: [{ role: "user", parts: [{ text: `[Kết quả từ Agent OS] ${text.slice(0, 2000)}` }] }], turnComplete: true } }),
      interrupt: () => { if (held) releaseHeld(held.tracker.cut(outCtx.currentTime)); flush(); },
      setMuted: (m: boolean) => { muted = m; },
      endAudio: () => send({ realtimeInput: { audioStreamEnd: true } }),
      close: () => { if (closed) return; closed = true; try { ws.close(1000, "client end"); } catch { /* */ } cleanup(); o.onEvent({ type: "state", state: "closed", detail: "client end" }); },
      metrics: () => ({ ...metrics, seen: [...mapper.seen].sort() }),
      dumpOutputPcm: () => { const n = outChunks.reduce((a, c) => a + c.length, 0); const all = new Uint8Array(n); let off = 0; for (const c of outChunks) { all.set(c, off); off += c.length; } return bytesToB64(all); },
    };
    return conn;
  }
}
