import type { LiveConnectOptions, LiveConnection, LiveProviderAdapter, RealtimeEvent } from "../types";
import { PlaybackTracker } from "../playback-tracker";

/**
 * MockLiveAdapter: deterministic full-duplex session for Phase 0 (design §12.3).
 * - "Always listening": user speech arrives via simulateUserSpeech() (test hook / dev panel). No microphone is opened in Phase 0.
 * - Plays a LOCAL audio sample (WebAudio oscillator, no network) while the mock model speaks; volume drives the orb.
 * - Barge-in: user speech while speaking stops playback immediately and reports the measured latency (target ≤ 300 ms, A3).
 * - Like a real provider, the transcription streams AHEAD of the audio (textLead × faster); on barge-in the saved final is cut
 *   by the shared PlaybackTracker to what was actually played and flagged `truncated` (same code path as Gemini).
 * - Tasks ("ask/hỏi/check/campaign/publish/…") are handed to Agent OS through the single agent_os_delegate tool (NON_BLOCKING).
 */
const TASK = /\b(ask|check|campaign|publish|post|status|report|hỏi|kiểm tra|chiến dịch|đăng|tình hình|báo cáo)\b/i;
export type MockTuning = { msPerWord: number; vadDelayMs: number; thinkMs: number; textLead?: number };
export const DEFAULT_TUNING: MockTuning = { msPerWord: 180, vadDelayMs: 60, thinkMs: 250, textLead: 3 };

export class MockLiveAdapter implements LiveProviderAdapter {
  readonly id = "mock";
  private tuning: MockTuning;
  constructor(tuning: MockTuning = DEFAULT_TUNING) { this.tuning = tuning; }
  async connect(opts: LiveConnectOptions): Promise<MockLiveConnection> { return new MockLiveConnection(opts, this.tuning); }
}

export class MockLiveConnection implements LiveConnection {
  private timers = new Set<ReturnType<typeof setTimeout>>();
  private speaking = false; private closed = false; private muted = false; private n = 0; private turn = 0;
  private audio: { ctx: AudioContext; gain: GainNode; osc?: OscillatorNode } | null = null;
  private readonly emit = (e: RealtimeEvent) => { if (!this.closed) this.o.onEvent(e); };
  private o: LiveConnectOptions; private t: MockTuning;
  constructor(o: LiveConnectOptions, t: MockTuning) {
    this.o = o; this.t = t;
    try { const Ctx = globalThis.AudioContext; if (Ctx) { const ctx = new Ctx(); const gain = ctx.createGain(); gain.gain.value = 0; gain.connect(ctx.destination); this.audio = { ctx, gain }; } }
    catch { this.audio = null; }
    o.signal.addEventListener("abort", () => this.close());
    this.emit({ type: "state", state: "connecting" });
    this.later(200, () => {
      this.emit({ type: "state", state: "connected", detail: `mock ${o.grant.provider}/${o.grant.model} · ${o.grant.credential.type}` });
      if (this.dictation || this.o.grant.purpose === "readaloud") { this.emit({ type: "mode", mode: "listening" }); return; }   // dictation: the model never speaks
      this.speak(o.grant.language === "vi" ? "Xin chào, em đang nghe anh." : "Hi, I'm listening. Talk to me any time, you can interrupt me.");
    });
  }
  private later(ms: number, f: () => void) { const id = setTimeout(() => { this.timers.delete(id); f(); }, ms); this.timers.add(id); }
  private playStart() {
    if (!this.audio) return;
    const { ctx, gain } = this.audio; const osc = ctx.createOscillator(); osc.type = "sine"; osc.frequency.value = 220 + 40 * (this.turn % 4);
    osc.connect(gain); gain.gain.setValueAtTime(0.04, ctx.currentTime); osc.start(); this.audio.osc = osc;
  }
  private playStop() {
    if (!this.audio?.osc) return;
    const { ctx, gain, osc } = this.audio; gain.gain.setValueAtTime(0, ctx.currentTime); try { osc.stop(); } catch { /* already stopped */ } osc.disconnect(); this.audio.osc = undefined;
  }
  /** Assistant speaks: audio (one word = msPerWord of "audio" on the playback clock) + transcription that runs textLead× ahead. */
  private speak(text: string, after?: () => void) {
    const turn = ++this.turn; const itemId = `a${turn}`; const words = text.split(/\s+/); let shown = "";
    const tr = new PlaybackTracker(); const now = () => performance.now() / 1000; const t0 = now(); const w = this.t.msPerWord / 1000;
    this.speaking = true; this.emit({ type: "mode", mode: "speaking" }); this.playStart();
    words.forEach((_, i) => tr.audio(t0 + i * w, w));   // the whole turn's audio is queued up front (generation > realtime)
    const lead = Math.max(1, this.t.textLead ?? 3);
    words.forEach((wd, i) => this.later((this.t.msPerWord / lead) * (i + 1), () => {   // transcription chunks: ahead of the audio
      if (!this.speaking || this.turn !== turn) return;
      const delta = (i ? " " : "") + wd; shown += delta; tr.text(delta);
      this.emit({ type: "transcript", role: "assistant", text: shown, final: false, itemId });
    }));
    words.forEach((_, i) => this.later(this.t.msPerWord * (i + 1), () => {          // playback clock: one word per tick
      if (!this.speaking || this.turn !== turn) return;
      this.emit({ type: "volume", level: 0.35 + 0.45 * Math.abs(Math.sin(i * 1.3)) });
      if (i === words.length - 1) {
        this.emit({ type: "transcript", role: "assistant", text: words.join(" "), final: true, itemId });
        this.stopSpeaking(); this.emit({ type: "usage", audioOutSeconds: (words.length * this.t.msPerWord) / 1000 }); after?.();
      }
    }));
    this.cutNow = () => tr.cut(now()); this.fullNow = () => shown;
    this.currentItem = itemId;
  }
  private fullNow = () => "";
  /** the saved final of an interrupted turn = only what was played (flagged), or the full text when nothing was cut */
  private emitCut(cut: ReturnType<PlaybackTracker["cut"]>) {
    if (cut) { if (cut.text) this.emit({ type: "transcript", role: "assistant", text: cut.text, final: true, itemId: this.currentItem, truncated: { playedMs: cut.playedMs, generatedMs: cut.generatedMs, fullChars: cut.fullChars } }); }
    else if (this.fullNow()) this.emit({ type: "transcript", role: "assistant", text: this.fullNow(), final: true, itemId: this.currentItem });
  }
  private cutNow: () => ReturnType<PlaybackTracker["cut"]> = () => null; private currentItem = "";
  private stopSpeaking() { this.speaking = false; this.playStop(); this.emit({ type: "volume", level: 0 }); this.emit({ type: "mode", mode: "listening" }); }
  private get dictation() { return this.o.grant.purpose === "dictation"; }
  /** dictation: input transcription only — words stream in as interim text of one item, final when the phrase ends */
  private dictate(text: string) {
    const itemId = `d${++this.n}`; const words = text.split(/\s+/).filter(Boolean); let shown = "";
    this.emit({ type: "user_speech_started", at: performance.now() });
    words.forEach((wd, i) => this.later(70 * (i + 1), () => {
      shown += (i ? " " : "") + wd; this.emit({ type: "volume", level: 0.3 + 0.5 * Math.abs(Math.sin(i * 1.7)) });
      this.emit({ type: "transcript", role: "user", text: shown, final: i === words.length - 1, itemId });
      if (i === words.length - 1) { this.emit({ type: "volume", level: 0 }); this.emit({ type: "user_speech_stopped" }); }
    }));
  }
  endAudio() { /* mock: transcription is already complete when the phrase ends */ }
  /** The mock "hears" the user (stands in for provider-side VAD + ASR on the always-open mic). */
  /** GĐ4 test hook: GoAway / network loss followed by a resumed connection (same session, transcript kept) */
  simulateLinkLoss(kind: "goAway" | "offline" = "goAway", ms = 1200) {
    if (this.closed) return;
    this.emit({ type: "state", state: "reconnecting", detail: kind === "offline" ? "offline" : "goAway 5000 ms" });
    setTimeout(() => { if (!this.closed) this.emit({ type: "state", state: "connected", detail: "resumed #1" }); }, ms);
  }
  simulateUserSpeech(text: string) {
    if (this.closed || this.muted) return;
    if (this.dictation) { this.dictate(text); return; }
    const at = performance.now(); this.emit({ type: "user_speech_started", at });
    this.later(this.t.vadDelayMs, () => {
      if (this.speaking) {
        const cut = this.cutNow(); this.turn++; this.stopSpeaking();
        this.emit({ type: "interrupted", latencyMs: Math.round(performance.now() - at) });
        this.emitCut(cut);
      }
      this.handleUser(text, `u${++this.n}`);
    });
  }
  private handleUser(text: string, itemId: string) {
    this.emit({ type: "transcript", role: "user", text, final: true, itemId }); this.emit({ type: "user_speech_stopped" });
    this.emit({ type: "mode", mode: "thinking" });
    this.later(this.t.thinkMs, () => {
      if (TASK.test(text)) {
        const callId = `call_${itemId}`;
        this.emit({ type: "tool_call", callId, name: "agent_os_delegate", args: { request: text } });
        this.speak(this.o.grant.language === "vi" ? "Để em hỏi Agent OS nhé." : "Let me check that with Agent OS.");
      } else {
        this.speak(`You said: ${text}. This is the Phase 0 mock voice talking for a while so that you can interrupt me at any moment and I will stop right away.`);
      }
    });
  }
  sendText(text: string) {
    // read-aloud: the same live model speaks the given text verbatim, then the session ends
    if (this.o.grant.purpose === "readaloud") { this.speak(text, () => this.later(50, () => this.close())); return; }
    this.simulateUserSpeech(text);
  }
  sendToolResult(_callId: string, output: string, opts?: { silent?: boolean }) { if (opts?.silent) return; this.later(this.speaking ? this.t.msPerWord * 4 : 50, () => this.speak(output.slice(0, 400))); }
  injectContext(text: string) { this.sendToolResult("ctx", text); }
  interrupt() {
    if (!this.speaking) return;
    const at = performance.now(); const cut = this.cutNow(); this.turn++; this.stopSpeaking(); this.emit({ type: "interrupted", latencyMs: Math.round(performance.now() - at) });
    this.emitCut(cut);
  }
  setMuted(m: boolean) { this.muted = m; }
  get isSpeaking() { return this.speaking; }
  close() {
    if (this.closed) return; this.playStop(); this.timers.forEach(clearTimeout); this.timers.clear();
    this.o.onEvent({ type: "state", state: "closed" }); this.closed = true; void this.audio?.ctx.close().catch(() => {});
  }
}
