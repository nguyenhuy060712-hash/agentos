// Front-end UI <-> Agent OS backend (same origin, session cookie). No API keys in the browser.
import { Client } from "@langchain/langgraph-sdk";

export const API_V1 = new URL("/api/v1", window.location.origin).href;
export const lgClient = new Client({ apiUrl: API_V1 });

export type Me = { id: number; username: string; role: string; ws: number; lang?: string;
  /** demo account (customer trial): read-only except chatting; daily model-spend cap in USD (Asia/Ho_Chi_Minh day) */
  demo?: boolean; daily_cap_usd?: number | null; spent_today_usd?: number;
  /** GĐ6.7: workspace requires 2FA and this user has not enabled it -> the server only lets them reach the security settings */
  must_enroll_2fa?: boolean; totp?: boolean };
/** Read-only surface (viewer or demo): set once from /api/auth/me before the shell renders; the server enforces it anyway (403). */
export const RO: { on: boolean; demo: boolean; cap: number | null; spent: number } = { on: false, demo: false, cap: null, spent: 0 };
export const setRO = (m: Me) => { RO.on = m.role === "viewer" || !!m.demo; RO.demo = !!m.demo; RO.cap = m.daily_cap_usd ?? null; RO.spent = m.spent_today_usd ?? 0; };
export type Assistant = { assistant_id: string; graph_id: string; agent_id: number; name: string; description?: string | null;
  metadata?: Record<string, unknown> | null; config?: Record<string, unknown> | null };
export const logout = () => fetch("/api/auth/logout", { method: "POST" });

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw Object.assign(new Error(`${r.status} ${r.statusText}`), { status: r.status });
  return r.json() as Promise<T>;
}
export const getMe = () => fetch("/api/auth/me").then((r) => j<Me>(r));
export const setLang = (lang: "en" | "vi") =>
  fetch("/api/auth/lang", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lang }) });
export type ChatConfig = { agent_id: string; models: { id: string; provider: string; input_per_1m?: number | null; output_per_1m?: number | null }[];
  default_model: string | null; picker: boolean; queue_max?: number; modes?: import("./chat-modes").ChatModeInfo[]; default_mode?: string | null; genui: boolean; genui_enabled: boolean;
  quota: { agent_budget_usd_day: number | null; agent_spent_today_usd: number; provider_caps: Record<string, { cap_usd: number; spent_usd: number }> } };
/** GĐ3.5–3.7: per-agent chat config (model allow-list, generative UI flag, quota) */
export const getChatConfig = (agentId: string) => fetch(`/api/chat/config?agent_id=${encodeURIComponent(agentId)}`).then((r) => j<ChatConfig>(r));
/** GĐ3 (b): show/hide an agent in the chat sidebar (assistant metadata, merged; nothing is deleted) */
export const setChatVisible = async (assistantId: string, visible: boolean) => {
  const a = (await lgClient.assistants.get(assistantId)) as unknown as Assistant;
  return lgClient.assistants.update(assistantId, { metadata: { ...(a.metadata ?? {}), chat_visible: visible } });
};
/** GĐ7.1: the built-in "Trợ lý Agent OS" of this workspace (created on first use). `graph_id` is the assistant_id to run; threads use source="assistant". */
export type AssistantInfo = { assistant_id: string; graph_id: string; agent_id: number; name: string; model: string; feature: string; can_write: boolean };
export const getAssistant = () => fetch("/api/assistant").then((r) => j<AssistantInfo>(r));
export const listAssistants = () => lgClient.assistants.search({ limit: 100 }) as unknown as Promise<Assistant[]>;

// ---- Feedback -> obs.py (no Langfuse, no Assistant Cloud)
export const postFeedback = (run_id: string, score: 0 | 1, comment?: string) =>
  fetch("/api/obs/feedback", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id, key: "user_thumbs", score, comment }),
  });

// ---- GĐ5a: long-term memory, thread search, @mention delegation (server: app/chat_gd5.py)
const jbody = (method: string, body: unknown): RequestInit => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export type Memory = { agent_id: string; agent_name: string; key: string; text: string; shared: boolean; created_at: string | null; updated_at: string | null };
export type MemoryList = { items: Memory[]; can_edit: boolean; is_admin: boolean };
export type MemoryAgent = { agent_id: string; name: string; enabled: boolean };
export const getMemories = (agentId?: string) => fetch(`/api/chat/memories${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ""}`).then((r) => j<MemoryList>(r));
export const patchMemory = (m: { agent_id: string; key: string; text: string; shared?: boolean }) => fetch("/api/chat/memories", jbody("PATCH", m)).then((r) => j<Memory>(r));
export const deleteMemory = (m: { agent_id: string; key: string; shared?: boolean }) =>
  fetch(`/api/chat/memories?${new URLSearchParams({ agent_id: m.agent_id, key: m.key, shared: String(!!m.shared) })}`, { method: "DELETE" }).then((r) => j<{ ok: boolean }>(r));
export const getMemoryAgents = () => fetch("/api/chat/memory/agents").then((r) => j<MemoryAgent[]>(r));
export const setMemoryAgent = (agentId: string, enabled: boolean) => fetch(`/api/chat/memory/agents/${encodeURIComponent(agentId)}`, jbody("PUT", { enabled })).then((r) => j<MemoryAgent>(r));
export type ThreadHit = { thread_id: string; title: string | null; agent_id: string | null; archived: boolean; pinned: boolean; updated_at: string | null; snippet: string };
export const searchThreads = (q: string, limit = 20, signal?: AbortSignal) =>
  fetch("/api/chat/threads/search", { ...jbody("POST", { q, limit }), signal }).then((r) => j<{ items: ThreadHit[] }>(r));
export type Delegated = { status: string; child_thread: string; agent_id: string; agent_name: string; values: Record<string, unknown> };
export const delegateTo = async (b: { thread_id: string; agent_id: string; text: string; message_id?: string; content?: unknown[] }, signal?: AbortSignal) => {
  const r = await fetch("/api/chat/delegate", { ...jbody("POST", b), signal });
  if (!r.ok) { let d = ""; try { d = String(((await r.json()) as { detail?: unknown }).detail ?? ""); } catch { /* not JSON */ }
    throw Object.assign(new Error(`HTTP ${r.status}: ${JSON.stringify({ detail: d })}`), { status: r.status }); }
  return r.json() as Promise<Delegated>;
};

// ---- GĐ5b: canvas, code run, schedules, share links (server: app/chat_gd5b.py)
const jget = async <T,>(u: string) => { const r = await fetch(u); if (!r.ok) throw await httpErr(r); return r.json() as Promise<T>; };
const jsend = async <T,>(u: string, method: string, body?: unknown) => { const r = await fetch(u, body === undefined ? { method } : jbody(method, body)); if (!r.ok) throw await httpErr(r); return r.json() as Promise<T>; };
async function httpErr(r: Response) {
  let d = ""; try { const x = (await r.json()) as { detail?: unknown }; d = typeof x.detail === "string" ? x.detail : JSON.stringify(x.detail ?? ""); } catch { /* not JSON */ }
  return Object.assign(new Error(d || `HTTP ${r.status}`), { status: r.status });
}
export type CanvasMeta = { doc_id: string; version: number; title: string; kind: "document" | "code"; language: string; author: "agent" | "user"; note?: string; created: string; size?: number };
export type CanvasDoc = { doc_id: string; thread_id: string; title: string; kind: "document" | "code"; language: string; version: number; content: string; versions: CanvasMeta[]; can_edit: boolean };
export const getCanvas = (doc: string) => jget<CanvasDoc>(`/api/chat/canvas/${encodeURIComponent(doc)}`);
export const getCanvasVersion = (doc: string, v: number) => jget<{ version: number; title: string; content: string; author: string; created: string }>(`/api/chat/canvas/${encodeURIComponent(doc)}/v/${v}`);
export const saveCanvas = (doc: string, b: { content: string; base_version: number; title?: string }) => jsend<{ doc_id: string; version: number }>(`/api/chat/canvas/${encodeURIComponent(doc)}`, "POST", b);
export const listCanvas = (threadId: string) => jget<{ items: CanvasMeta[] }>(`/api/chat/canvas?thread_id=${encodeURIComponent(threadId)}`);
export type CodeResult = { status: "ok" | "error" | "timeout"; exit_code: number; ms: number; stdout: string; stderr: string; images: { id: string; url: string }[]; runtime: string;
  files?: { id?: string; url?: string; name: string; size?: number; mime_type?: string; error?: string }[] };   // GĐ8.6: files written to /workspace/out
export const runCode = (code: string, thread_id?: string) => jsend<CodeResult>("/api/chat/code/run", "POST", { code, thread_id });
export type CodeSettings = { enabled: boolean; roles: string[]; can_run: boolean; is_admin: boolean; runtime: string; image: string; timeout_s: number;
  limits: { memory_mb: number; cpus: number; pids: number; network: boolean; runs_per_10min: number } };
export const getCodeSettings = () => jget<CodeSettings>("/api/chat/code/settings");
export const putCodeSettings = (b: { enabled: boolean; roles: string[] }) => jsend<CodeSettings>("/api/chat/code/settings", "PUT", b);
export type Instructions = { about: string; style: string; enabled: boolean; max_chars: number; read_only?: boolean };
export const getInstructions = () => jget<Instructions>("/api/chat/me/instructions");
export const putInstructions = (b: { about: string; style: string; enabled: boolean }) => jsend<Instructions>("/api/chat/me/instructions", "PUT", b);
export type Schedule = { id: number; agent_id: string; agent_name: string; name: string; cron: string; message: string; enabled: boolean; locked: boolean;
  locked_reason: string | null; last_run: string | null; last_status: string | null; next_run: string | null; can_edit: boolean; source?: "ui" | "agent" | "file"; paused?: boolean };
export const getSchedules = () => jget<{ items: Schedule[]; can_create: boolean; tz: string; max_per_day: number }>("/api/chat/schedules");
export const createSchedule = (b: { agent_id: string; name: string; cron: string; message: string }) => jsend<Schedule>("/api/chat/schedules", "POST", b);
export const setScheduleEnabled = (id: number, enabled: boolean) => jsend<Schedule>(`/api/chat/schedules/${id}`, "PATCH", { enabled });
export const deleteSchedule = (id: number) => jsend<{ ok: boolean }>(`/api/chat/schedules/${id}`, "DELETE");
export type ShareLink = { id: string; thread_id: string; created: string; expires: string; revoked: string | null; views: number; last_view?: string | null; token?: string; path?: string };
export const createShare = (thread_id: string, days: number) => jsend<ShareLink>("/api/chat/shares", "POST", { thread_id, days });
export const listShares = (thread_id: string) => jget<{ items: ShareLink[] }>(`/api/chat/shares?thread_id=${encodeURIComponent(thread_id)}`);
export const revokeShare = (id: string) => jsend<{ ok: boolean }>(`/api/chat/shares/${encodeURIComponent(id)}`, "DELETE");
export type SharedPart = { type: "text"; text: string } | { type: "present"; args: Record<string, unknown> } | { type: "note"; text: string };
export type SharedThread = { title: string; agent_name: string | null; created: string; expires: string;
  messages: ({ role: "user"; text: string } | { role: "assistant"; parts: SharedPart[]; delegated_from?: string })[] };
export const openShare = (token: string) => jget<SharedThread>(`/api/chat/share/${encodeURIComponent(token)}`);
export { modelName } from "./models";

// ---- GĐ6.7 security: 2FA + sessions. The chat has no login screen of its own (401 -> server /login); loginStart/loginVerify mirror that page's two steps.
export type Login2fa = { ok?: true; username?: string; need_2fa?: true; challenge?: string; must_enroll_2fa?: boolean };
async function jj<T>(r: Response): Promise<T> {
  const d = await r.json().catch(() => ({})) as T & { detail?: string };
  if (!r.ok) throw Object.assign(new Error(typeof d.detail === "string" ? d.detail : `${r.status} ${r.statusText}`), { status: r.status });
  return d;
}
const post = (url: string, body: unknown = {}, method = "POST") =>
  fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => jj<Record<string, unknown>>(r));
export const loginStart = (username: string, password: string) => post("/api/auth/login", { username, password }) as Promise<Login2fa>;
/** step 2 after {need_2fa}: a 6-digit TOTP `code` or a one-time `recovery_code`; remember = skip step 2 on this device for 30 days */
export const loginVerify = (challenge: string, o: { code?: string; recovery_code?: string; remember_device?: boolean }) =>
  post("/api/auth/2fa/verify", { challenge, ...o }) as Promise<Login2fa>;
export type TwoFaStatus = { enabled: boolean; pending_setup: boolean; recovery_left: number; workspace_requires: boolean; must_enroll: boolean; demo: boolean };
export type SessionRow = { id: string; device: string | null; ip: string | null; created: string; last_seen: string; expires: string; current: boolean };
export const getTwoFa = () => fetch("/api/auth/2fa").then((r) => jj<TwoFaStatus>(r));
export const twoFaSetup = () => post("/api/auth/2fa/setup") as Promise<{ otpauth_uri: string; secret: string; qr_svg: string }>;
export const twoFaEnable = (code: string) => post("/api/auth/2fa/enable", { code }) as Promise<{ recovery_codes: string[] }>;
export const twoFaDisable = (password: string, v: string) =>
  post("/api/auth/2fa/disable", { password, ...(/^\d{6}$/.test(v) ? { code: v } : { recovery_code: v }) });
export const twoFaRegenerate = (code: string) => post("/api/auth/2fa/recovery/regenerate", { code }) as Promise<{ recovery_codes: string[] }>;
export const getSessions = () => fetch("/api/auth/sessions").then((r) => jj<SessionRow[]>(r));
export const revokeSession = (id: string) => post(`/api/auth/sessions/${id}`, {}, "DELETE") as Promise<{ current?: boolean }>;
export const revokeOtherSessions = () => post("/api/auth/sessions/revoke-others") as Promise<{ revoked: number }>;

// ---- GĐ8.9 projects (server: app/chat_projects.py). Private to the owner; instructions + files + memory reach every chat inside the project.
export type ProjectFile = { id: string; name: string; kind: string; size: number; tokens: number | null; mode: "inline" | "search" };
export type Project = { id: number; name: string; instructions: string; created_at?: string; updated_at?: string; n_threads?: number; n_files?: number; files?: ProjectFile[] };
export type ProjectLimits = { name: number; instructions: number; files: number; projects: number };
export const listProjects = () => jget<{ items: Project[]; read_only: boolean; limits: ProjectLimits }>("/api/chat/projects");
export const getProject = (id: number) => jget<Project>(`/api/chat/projects/${id}`);
export const createProject = (b: { name: string; instructions?: string }) => jsend<Project>("/api/chat/projects", "POST", b);
export const patchProject = (id: number, b: { name?: string; instructions?: string }) => jsend<Project>(`/api/chat/projects/${id}`, "PATCH", b);
export const deleteProject = (id: number) => jsend<{ ok: boolean }>(`/api/chat/projects/${id}`, "DELETE");
export const addProjectFile = (id: number, file_id: string) => jsend<Project>(`/api/chat/projects/${id}/files`, "POST", { file_id });
export const removeProjectFile = (id: number, file_id: string) => jsend<Project>(`/api/chat/projects/${id}/files/${encodeURIComponent(file_id)}`, "DELETE");
/** GĐ8.P: the project's own long-term memories (what the AI remembered in this project's chats); deleting = forget */
export type ProjectMemory = { key: string; text: string; updated_at?: string };
export const listProjectMemories = (id: number) => jget<{ items: ProjectMemory[] }>(`/api/chat/projects/${id}/memories`);
export const deleteProjectMemory = (id: number, key: string) => jsend<{ ok: boolean }>(`/api/chat/projects/${id}/memories/${encodeURIComponent(key)}`, "DELETE");
/** move a chat into a project (id) or out of it (null) */
export const moveThreadToProject = (thread_id: string, project_id: number | null) => jsend<{ thread_id: string; project_id: number | null }>("/api/chat/projects/move", "POST", { thread_id, project_id });
/** 6.8 upload (docfiles + markitdown); the returned id is then added to the project */
export const uploadFile = async (f: File) => {
  const fd = new FormData(); fd.append("file", f, f.name);
  const r = await fetch("/api/files", { method: "POST", body: fd }); if (!r.ok) throw await httpErr(r);
  return r.json() as Promise<{ id: string; name: string; kind?: string; status?: string; reason?: string | null }>;
};

// ---- GĐ9.8 "Dữ liệu của tôi": data-subject rights (server: app/privacy.py). Export = zip download, erase = typed confirmation (the username).
export type EraseResult = { ok: boolean; counts: Record<string, number | boolean | string>; message: string };
export const exportMyData = async (): Promise<{ blob: Blob; name: string }> => {
  const r = await fetch("/api/privacy/me/export"); if (!r.ok) throw await httpErr(r);
  const cd = r.headers.get("content-disposition") ?? ""; return { blob: await r.blob(), name: /filename="([^"]+)"/.exec(cd)?.[1] ?? "du-lieu-cua-toi.zip" };
};
export const eraseMyData = (confirm: string, reason?: string) => jsend<EraseResult>("/api/privacy/me/erase", "POST", { confirm, ...(reason ? { reason } : {}) });

// ---- GĐ11.52 "Computer" panel: the thread's virtual computer (server: app/sbx_api.py, /api/v2/sandboxes/boxes + /api/threads/{tid}/computer/activate)
export type Box = { id: string; name: string; status: string; size?: string; core?: string; thread_id?: string | null; internet?: string; last_error?: string | null; disk_warn?: boolean };
export type BoxFile = { path: string; name: string; type: string; size: number; mtime?: number };
export type BoxCmd = { id: number | string; command: string; cwd?: string; status?: string; exit_code: number | null; duration_ms?: number | null; started?: string | null };
export type BoxRun = { stdout: string; exit_code: number; success: boolean; truncated: boolean; timed_out: boolean; duration_ms: number };
const BX = "/api/v2/sandboxes/boxes";
export const getThreadBox = async (threadId: string) => (await jget<{ items: Box[] }>(`${BX}?thread_id=${encodeURIComponent(threadId)}&limit=20`)).items.find((b) => b.status !== "failed") ?? null;
export const activateComputer = (threadId: string) => jsend<Box>(`/api/threads/${encodeURIComponent(threadId)}/computer/activate`, "POST", { size: "small" });
export const startBox = (id: string) => jsend<Box>(`${BX}/${id}/start`, "POST");
export const getBox = (id: string) => jget<Box>(`${BX}/${id}`);
export const listBoxFiles = (id: string, path: string) => jget<{ path: string; items: BoxFile[]; truncated: boolean }>(`${BX}/${id}/files?path=${encodeURIComponent(path)}`);
export const boxFileUrl = (id: string, path: string) => `${BX}/${id}/files/content?path=${encodeURIComponent(path)}`;
export const readBoxFile = async (id: string, path: string, max = 20000) => { const r = await fetch(boxFileUrl(id, path)); if (!r.ok) throw await httpErr(r); return (await r.text()).slice(0, max); };
export const boxMkdir = (id: string, path: string) => jsend<{ ok: boolean }>(`${BX}/${id}/files/mkdir`, "POST", { path });
export const boxMove = (id: string, src: string, dest: string) => jsend<{ ok: boolean }>(`${BX}/${id}/files/move`, "POST", { src, dest });
export const boxUpload = async (id: string, files: File[], path: string) => {
  const fd = new FormData(); fd.append("path", path); files.forEach((f) => fd.append("files", f, f.name));
  const r = await fetch(`${BX}/${id}/files/upload`, { method: "POST", body: fd }); if (!r.ok) throw await httpErr(r);
  return r.json() as Promise<{ uploaded: { path: string; bytes: number }[] }>;
};
export const listBoxCommands = (id: string, q = "") => jget<{ items: BoxCmd[] }>(`${BX}/${id}/commands?limit=30${q ? `&q=${encodeURIComponent(q)}` : ""}`);
export const runBoxCommand = (id: string, command: string) => jsend<BoxRun>(`${BX}/${id}/execute`, "POST", { command, timeout: 60 });

// ---- GĐ11.34 / F-2: archive, restore and permanent delete of an agent (server app/agent_purge.py)
export type ArchivedAgent = { id: number; name: string; owner_id: number | null; visibility?: string; created?: string };
export const listArchivedAgents = () => jget<ArchivedAgent[]>("/api/archived-agents");
export const archiveAgent = (id: number) => jsend<{ ok: boolean; archived: boolean }>(`/api/agents/${id}?mode=archive`, "DELETE");
export const restoreAgent = (id: number) => jsend<{ ok: boolean; restored: boolean }>(`/api/agents/${id}/restore`, "POST");
export const purgeAgent = (id: number, name: string) => jsend<{ ok: boolean; purged: boolean; already?: boolean }>(`/api/agents/${id}?mode=permanent&confirm=${encodeURIComponent(name)}`, "DELETE");

// ---- GĐ11.29: connect card (server: app/connection_runtime.py session API, app/connections.py, app/oauth_tools.py)
export type AuthSession = { auth_id: string; slug: string; status: "pending" | "completed" | "failed" | "expired" };
/** long poll (the server answers when the sign-in ends or after `wait` seconds) */
export const getAuthSession = (authId: string, wait = 25, signal?: AbortSignal) =>
  fetch(`/api/agent-auth/sessions/${encodeURIComponent(authId)}?wait_seconds=${wait}`, { signal }).then((r) => j<AuthSession>(r));
/** a secret (API key) of the person's own, write-only */
export const saveConnectionSecret = (slug: string, value: string) => jsend<{ ok: boolean }>(`/api/connections/${encodeURIComponent(slug)}/credentials`, "POST", { value });
/** loopback_paste providers (Google, Microsoft): the address the browser could not open after sign-in */
export const pasteOAuthCallback = (url: string) => jsend<{ ok: boolean; provider: string }>("/api/oauth/paste-callback", "POST", { url });
/** GĐ12.87: draft a Skill from this conversation (nothing is saved), check it while editing, save it as a PRIVATE skill of the agent. */
export type SkillLintItem = { code: string; msg: string; msg_en: string; file?: string };
export type SkillLint = { errors: SkillLintItem[]; warnings: SkillLintItem[] };
export type SkillDraftResp = { draft: { name: string; description: string; body: string; files: Record<string, string> }; lint: SkillLint; model?: string; saved: false };
export const draftSkill = (b: { mode: "describe" | "conversation" | "fix"; text?: string; thread_id?: string; run_id?: string; agent_id?: number | null }) => jsend<SkillDraftResp>("/api/skills/draft", "POST", b);
export const lintSkill = (name: string, files: Record<string, string>, agent_id?: number | null) => jsend<SkillLint>("/api/skills/lint", "POST", { name, files, agent_id });
export const saveSkill = (name: string, files: Record<string, string>, agent_id: number | null) =>
  jsend<{ id: number; name: string; visibility: string; lint: SkillLint }>("/api/skills", "POST", { name, files, agent_id, visibility: agent_id ? "private" : "workspace", source: "conversation" });
