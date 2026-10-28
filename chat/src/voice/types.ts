/**
 * Realtime voice for the Agent OS front-end UI (assistant-ui layer).
 * "Voice" = FULL-DUPLEX, always-listening, two-way conversation with barge-in. Not push-to-talk, not read-aloud.
 * Models (data, not code): rows of the Agent OS voice catalog, e.g. gpt-live-1 (OpenAI) and gemini-3.8-live (Google),
 * chosen at runtime from GET /api/voice/config (agent override > workspace > catalog). See phase0_design.md §4–§9.
 *
 *   browser ==audio both ways==> provider directly (WebRTC for openai, WebSocket for google); no media server, no Pipecat, no LiveKit
 *   browser --> Agent OS  POST /api/voice/session  (cookie, voice.use, allow-list 403, budget 402) -> short-lived credential
 *   model --agent_os_delegate / client delegation--> Agent OS run on the SAME thread (tools, HITL, history, budget stay in Agent OS)
 *
 * Phase 0: MockLiveAdapter only. No provider network, no keys, no spend.
 */
export type VoiceModelId = string; // never a hardcoded union: the catalog lives in Agent OS Settings
export type ProviderId = string;   // "openai" | "google" | "mock" today; a new provider = one new adapter

export type VoiceModelInfo = { id: VoiceModelId; provider: ProviderId; display_name: string; transport: "webrtc" | "websocket"; est_usd_per_min: number; voice?: string };

/** GET /api/voice/config?agent_id= */
export type VoiceConfig = {
  phase0_mock: boolean; real_providers?: string[]; agent_id: string | null; allowed: VoiceModelId[]; default_model: VoiceModelId | null; source: "agent" | "workspace";
  models: VoiceModelInfo[]; barge_in: boolean; language: string; can_use: boolean;
  caps: { max_session_min: number; daily_min_per_user: number; daily_budget_usd: number; used_min_today: number; spent_usd_today: number };
};

/** POST /api/voice/session -> grant. The credential is short-lived and single-use; provider API keys never reach the browser. */
export type VoiceGrant = {
  voice_session_id: string; adapter: "mock" | "provider"; mock: boolean; provider: ProviderId; model: VoiceModelId; transport: "webrtc" | "websocket";
  credential: { type: "sdp_answer" | "ephemeral_token"; value: string; expires_at: number };
  thread_id?: string; ws_url?: string | null; max_session_s: number; barge_in: boolean; language: string; voice?: string;
  tools: { name: string; behavior?: string; scheduling?: string; description: string }[];
  /** "dictation" = composer Mic: the SAME selected live model, input transcription only (no thread, no tools, output not played) */
  purpose?: "conversation" | "dictation" | "readaloud";
};

/** Normalised events every provider adapter emits (wire events are mapped inside the adapter). */
export type RealtimeEvent =
  | { type: "state"; state: "connecting" | "connected" | "reconnecting" | "closed" | "error"; detail?: string }
  | { type: "mode"; mode: "listening" | "thinking" | "speaking" }
  | { type: "user_speech_started"; at: number }
  | { type: "user_speech_stopped" }
  | { type: "interrupted"; latencyMs: number } // barge-in honoured: assistant playback stopped
  | { type: "transcript"; role: "user" | "assistant"; text: string; final: boolean; itemId: string;
      /** set on an assistant final cut by barge-in: `text` is only what was actually PLAYED (see PlaybackTracker) */
      truncated?: { playedMs: number; generatedMs: number; fullChars: number } }
  | { type: "tool_call"; callId: string; name: string; args: { request: string; context?: string } }
  | { type: "tool_call_cancelled"; callIds: string[] }
  | { type: "usage"; audioInSeconds?: number; audioOutSeconds?: number; inputTokens?: number; outputTokens?: number; raw?: Record<string, unknown> }
  | { type: "volume"; level: number }
  | { type: "error"; error: string; fatal: boolean };

export type LiveConnectOptions = { grant: VoiceGrant; instructions: string; signal: AbortSignal; onEvent: (e: RealtimeEvent) => void;
  /** GĐ4: fetch a new single-use credential before a resumed connection (Gemini: 1011 "Token has been used too many times" otherwise) */
  refreshCredential?: () => Promise<{ value: string; expires_at: number }> };

/** One live, full-duplex connection. */
export interface LiveConnection {
  /** Typed text during a call (Gemini: realtimeInput/clientContent text; GPT-Live: conversation item). */
  sendText(text: string): void;
  /** Answer a tool call / delegation (Gemini toolResponse; GPT-Live session.commentary.append). */
  /** silent: the provider must take the result into account WITHOUT speaking (used for a cancelled call). */
  sendToolResult(callId: string, output: string, opts?: { silent?: boolean }): void;
  /** Late context the model should speak, e.g. the result after an on-screen HITL approval. */
  injectContext(text: string): void;
  /** Local barge-in (e.g. tap on the orb): stop playback now, cancel the in-flight response. */
  interrupt(): void;
  setMuted(muted: boolean): void;
  /** dictation: tell the provider the audio stream ended so it flushes the last input transcription */
  endAudio?(): void;
  close(): void;
}

export interface LiveProviderAdapter {
  readonly id: ProviderId;
  connect(opts: LiveConnectOptions): Promise<LiveConnection>;
}
