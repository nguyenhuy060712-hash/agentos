// GĐ11.36 chat turn: (1) send while a run waits for approval = reject every pending action, then send the new message; (2) send while a run
// is going = queue (max 5 per thread, edit / delete until it starts); (3) turn clock labels; (4) copy of an answer / of all its steps.
// Pure helpers + two tiny external stores (useSyncExternalStore), no React here so vitest covers them directly.

export const QUEUE_MAX = 5;   // default of the server key chat.queue_max
let queueMax = QUEUE_MAX;
/** limit from the server (`queue_max` in /api/chat/config); anything that is not a whole number 1..20 keeps the default */
export const setQueueMax = (n: unknown) => { queueMax = typeof n === "number" && Number.isInteger(n) && n >= 1 && n <= 20 ? n : QUEUE_MAX; };
export const getQueueMax = () => queueMax;
export const REJECT_MESSAGE = "Người dùng gửi tin mới, bỏ yêu cầu này";
/** one reject per pending action (never an auto-approve) */
export const rejectAll = (n: number) => Array.from({ length: n }, () => ({ type: "reject" as const, message: REJECT_MESSAGE }));

export type Queued = { id: string; text: string; canvas?: string };
let queues: Record<string, Queued[]> = {};
const qsubs = new Set<() => void>();
const qset = (next: Record<string, Queued[]>) => { queues = next; qsubs.forEach((cb) => cb()); };
const EMPTY: Queued[] = [];
let seq = 0;

export const turnQueue = {
  subscribe: (cb: () => void) => { qsubs.add(cb); return () => { qsubs.delete(cb); }; },
  /** stable reference per thread state (useSyncExternalStore needs it) */
  get: (tid: string): Queued[] => queues[tid] ?? EMPTY,
  /** false = blank, or the queue is full (the caller shows a notice) */
  add(tid: string, text: string, canvas?: string, max = queueMax): boolean {
    const cur = queues[tid] ?? [];
    if (!text.trim() || cur.length >= max) return false;
    qset({ ...queues, [tid]: [...cur, { id: `q${++seq}`, text, ...(canvas ? { canvas } : {}) }] });
    return true;
  },
  remove(tid: string, id: string) { qset({ ...queues, [tid]: (queues[tid] ?? []).filter((q) => q.id !== id) }); },
  /** Sửa: take the message out of the queue and give it back to the composer */
  edit(tid: string, id: string): Queued | undefined { const q = (queues[tid] ?? []).find((x) => x.id === id); if (q) turnQueue.remove(tid, id); return q; },
  /** next message to run (oldest first) */
  take(tid: string): Queued | undefined { const q = (queues[tid] ?? [])[0]; if (q) turnQueue.remove(tid, q.id); return q; },
  reset() { qset({}); },
};
/** the text that is actually sent for a queued message (canvas chip = a reference line) */
export const queuedToText = (q: Queued) => (q.canvas ? `${q.text}\n\n[canvas: ${q.canvas}]` : q.text);

/** the approval card of this interrupt key was skipped because the user typed a new message (card shows "đã bỏ qua") */
let skipped = "";
const ssubs = new Set<() => void>();
export const skippedApproval = {
  get: () => skipped,
  set: (key: string) => { skipped = key; ssubs.forEach((cb) => cb()); },
  subscribe: (cb: () => void) => { ssubs.add(cb); return () => { ssubs.delete(cb); }; },
};

// ---- turn clock -------------------------------------------------------------------------------------------------------------------
export type ClockPhase = "idle" | "thinking" | "tool" | "writing";
export type ClockState = { phase: ClockPhase; tool?: string; startedAt: number };
let clock: ClockState = { phase: "idle", startedAt: 0 };
const csubs = new Set<() => void>();
const cset = (c: ClockState) => { clock = c; csubs.forEach((cb) => cb()); };

/** next phase from one stream event (`messages*` chunk / `updates` step); null = no change */
export function phaseFromEvent(ev: string, data: unknown): { phase: ClockPhase; tool?: string } | null {
  if (ev.startsWith("messages")) {
    const c = (Array.isArray(data) ? data[0] : undefined) as { content?: unknown; tool_call_chunks?: { name?: string }[] } | undefined;
    const tc = c?.tool_call_chunks?.find((x) => x?.name);
    if (tc?.name) return { phase: "tool", tool: tc.name };
    const hasText = typeof c?.content === "string" ? c.content.length > 0 : Array.isArray(c?.content) && c.content.some((p: { text?: string }) => !!p?.text);
    return hasText ? { phase: "writing" } : null;
  }
  if (ev === "updates" && data && typeof data === "object") {
    for (const v of Object.values(data as Record<string, unknown>)) {
      const s = v as { kind?: string; name?: string } | undefined;
      if (s?.kind === "result") return { phase: "thinking" };
      if (s?.kind && s.kind !== "sub_answer" && s.name) return { phase: "tool", tool: s.name };
    }
  }
  return null;
}
export const turnClock = {
  get: () => clock,
  subscribe: (cb: () => void) => { csubs.add(cb); return () => { csubs.delete(cb); }; },
  start(now = Date.now()) { cset({ phase: "thinking", startedAt: now }); },
  feed(ev: string, data: unknown) {
    if (clock.phase === "idle") return;
    const n = phaseFromEvent(ev, data);
    if (n && (n.phase !== clock.phase || n.tool !== clock.tool)) cset({ ...clock, phase: n.phase, tool: n.tool });
  },
  stop() { if (clock.phase !== "idle") cset({ phase: "idle", startedAt: 0 }); },
};
export const mmss = (ms: number) => { const s = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
/** "Đang suy nghĩ… 0:12" -> "Đang gọi công cụ <tên>…" -> "Đang viết câu trả lời…" (t = i18n function) */
export function clockLabel(c: ClockState, now: number, t: (k: string, o?: Record<string, unknown>) => string): string {
  if (c.phase === "tool") return t("clockTool", { name: c.tool ?? "" });
  if (c.phase === "writing") return t("clockWriting");
  return `${t("clockThinking")} ${mmss(now - c.startedAt)}`;
}

// ---- copy --------------------------------------------------------------------------------------------------------------------------
export const RESULT_MAX = 4000;
const cut = (s: string) => (s.length > RESULT_MAX ? s.slice(0, RESULT_MAX) + "…" : s);
const asText = (v: unknown) => (typeof v === "string" ? v : v === undefined ? "" : JSON.stringify(v, null, 1));
export type CopyPart = { type?: string; text?: string; toolName?: string; args?: unknown; argsText?: string; result?: unknown; isError?: boolean };
/** "Sao chép câu trả lời": the text parts only */
export const answerText = (parts: readonly CopyPart[]) => parts.filter((p) => p.type === "text").map((p) => p.text ?? "").join("\n\n").trim();
/** "Sao chép toàn bộ các bước": Markdown with reasoning, every tool call (args) and its result (cut at 4,000 chars), then the answer */
export function stepsMarkdown(parts: readonly CopyPart[]): string {
  const out: string[] = [];
  for (const p of parts) {
    if (p.type === "reasoning" && p.text) out.push(`> ${p.text.trim().replace(/\n/g, "\n> ")}`);
    else if (p.type === "tool-call") {
      out.push(`**${p.toolName ?? "tool"}**\n\n\`\`\`json\n${p.argsText ?? asText(p.args)}\n\`\`\``);
      if (p.result !== undefined) out.push(`${p.isError ? "Error" : "Result"}:\n\n\`\`\`\n${cut(asText(p.result))}\n\`\`\``);
    } else if (p.type === "text" && p.text) out.push(p.text.trim());
  }
  return out.join("\n\n");
}
