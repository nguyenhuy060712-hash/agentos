// Agent OS voice control plane (same origin, session cookie). Phase 0: served by the local overlay; Phase 1: additive endpoints in Agent OS.
import type { VoiceConfig, VoiceGrant, VoiceModelId } from "./types";

export class VoiceHttpError extends Error {
  status: number; detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : ((detail as { message?: string })?.message ?? `HTTP ${status}`));
    this.status = status; this.detail = detail;
  }
}
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new VoiceHttpError(r.status, (body as { detail?: unknown }).detail ?? body);
  return body as T;
}
const post = <T>(p: string, b: unknown) => call<T>(p, { method: "POST", body: JSON.stringify(b) });
const put = <T>(p: string, b: unknown) => call<T>(p, { method: "PUT", body: JSON.stringify(b) });

export const getVoiceConfig = (agentId: string) => call<VoiceConfig>(`/api/voice/config?agent_id=${encodeURIComponent(agentId)}`);
export const mintVoiceSession = (b: { model: VoiceModelId; agent_id: string; thread_id: string | null; sdp?: string; purpose?: "conversation" | "dictation" | "readaloud" }) => post<VoiceGrant>("/api/voice/session", b);
export const endVoiceSession = (id: string, seconds: number, reason: string) =>
  post<{ status: string; seconds: number; cost_usd: number }>(`/api/voice/session/${id}/end`, { seconds, reason });
export type DelegateResult = { call_id: string; run_id: string; status: "success" | "interrupted" | "error" | "cancelled"; reply: string | null;
  approval: null | { approval_id: number | null; interrupts: { action_requests: { name: string; args: Record<string, unknown>; description?: string }[];
    review_configs: { action_name: string; allowed_decisions: string[] }[] }[] }; resumed_by?: string };
export const delegate = (b: { voice_session_id: string; call_id: string; request: string; context?: string }) => post<DelegateResult>("/api/voice/delegate", b);
export const cancelDelegate = (callId: string, reason = "barge_in") =>
  post<{ call_id: string; cancelled: boolean; run_id?: string }>(`/api/voice/delegate/${encodeURIComponent(callId)}/cancel`, { reason });
export type TranscriptItem = { role: "user" | "assistant"; text: string; t_start?: number; t_end?: number; phase?: "turn" | "preamble" | "readback"; call_id?: string;
  /** barge-in cut: `text` is what was played; persisted server-side as additional_kwargs.voice_truncated (+ played/generated ms) */
  truncated?: boolean; played_ms?: number; generated_ms?: number; full_chars?: number };
export const delegateStatus = (callId: string) => call<DelegateResult>(`/api/voice/delegate/${encodeURIComponent(callId)}`);
export const postTranscript = (voice_session_id: string, items: TranscriptItem[]) =>
  post<{ status: string; n: number }>("/api/voice/transcript", { voice_session_id, items });

// ---- Settings (Agent OS "Voice" page)
export type CatalogRow = { id: string; provider: string; display_name: string; transport: string; price_unit: string; price_per_min?: number;
  price_in?: number; price_out?: number; est_usd_per_min: number; enabled: boolean; voice?: string; notes?: string };
export type WorkspaceVoice = { allowed_models: string[]; default_model: string; max_session_min: number; daily_min_per_user: number;
  daily_budget_usd: number; language: string; barge_in: boolean; voice_roles?: string[] };
export type VoiceSettings = { catalog: CatalogRow[]; workspace: WorkspaceVoice; agents: Record<string, { allowed_models?: string[]; default_model?: string }>;
  audit: { at: string; by: string; what: string }[]; can_edit: boolean; can_override_agent: boolean };
export const getVoiceSettings = () => call<VoiceSettings>("/api/settings/voice");
export const putVoiceSettings = (workspace: WorkspaceVoice, catalog?: CatalogRow[]) => put<VoiceSettings>("/api/settings/voice", { workspace, catalog });
export const putAgentVoice = (graphId: string, o: { allowed_models?: string[] | null; default_model?: string | null }) =>
  put<VoiceSettings>(`/api/settings/voice/agents/${graphId}`, o);
/** GĐ4: a fresh locked ephemeral token for an ACTIVE session (each Gemini token is single-use, so a resumed socket needs a new one) */
export const refreshVoiceToken = (id: string) => post<{ value: string; expires_at: number; reissues: number }>(`/api/voice/session/${id}/token`, {});
export const postUsage = (id: string, usage: Record<string, unknown>) =>
  post<{ session_usd: number; real_spend_usd: number; stop: boolean; abort_at: number }>(`/api/voice/session/${id}/usage`, { usage });
