// G1: live progress of Deep Agents sub-agents (subgraphs). Fed by "<mode>|<checkpoint_ns>" stream events (stream_subgraphs)
// and, after a reload, by getState({subgraphs:true}).tasks[].state. Read by the `task` tool UI.
import { useSyncExternalStore } from "react";

export type SubMsg = { type: string; content: unknown; tool_calls?: { name: string; args?: unknown }[]; name?: string };
export type SubEntry = { ns: string; description: string; messages: SubMsg[]; live: string; status: "running" | "interrupted" | "done"; updatedAt: number };
const entries = new Map<string, SubEntry>(); const subs = new Set<() => void>(); let version = 0;
const bump = () => { version++; subs.forEach((f) => f()); };
const text = (c: unknown) => (typeof c === "string" ? c : Array.isArray(c) ? c.map((b) => (b && typeof b === "object" && "text" in b ? String((b as { text: unknown }).text) : "")).join(" ") : "");
const firstHuman = (ms: SubMsg[]) => text(ms.find((m) => m.type === "human")?.content ?? "").trim();

function upsert(ns: string, patch: Partial<SubEntry>) {
  const e = entries.get(ns) ?? { ns, description: "", messages: [], live: "", status: "running" as const, updatedAt: 0 };
  const n = { ...e, ...patch, updatedAt: Date.now() }; if (patch.messages) n.description = firstHuman(patch.messages) || n.description;
  entries.set(ns, n); bump();
}
export const subagentStore = {
  values(ns: string, v: { messages?: SubMsg[] }) { if (Array.isArray(v?.messages)) upsert(ns, { messages: v.messages, live: "" }); },
  token(ns: string, t: string) { const e = entries.get(ns); upsert(ns, { live: (e?.live ?? "") + t }); },
  interrupted(ns: string) { upsert(ns, { status: "interrupted" }); },
  finishAll() { for (const [k, e] of entries) if (e.status === "running") entries.set(k, { ...e, status: "done" }); bump(); },
  /** from getState({subgraphs:true}) */
  fromState(tasks: { checkpoint_ns?: string; state?: { values?: { messages?: SubMsg[] }; interrupts?: unknown[]; tasks?: unknown[] } | null }[]) {
    for (const t of tasks ?? []) if (t.checkpoint_ns && t.state) {
      upsert(t.checkpoint_ns, { messages: t.state.values?.messages ?? [], status: (t.state.interrupts ?? []).length ? "interrupted" : "running" });
      this.fromState((t.state.tasks ?? []) as never);
    }
  },
  /** stream chunk router: returns true when the chunk was a subgraph event consumed here */
  route(ev: string, data: unknown): boolean {
    const i = ev.indexOf("|"); if (i < 0) return false;
    const mode = ev.slice(0, i); const ns = ev.slice(i + 1);
    if (mode === "values") this.values(ns, data as { messages?: SubMsg[] });
    else if (mode === "messages") { const [m] = (data as [{ content?: unknown }]) ?? []; this.token(ns, text(m?.content)); return true; } // keep sub tokens out of the main thread
    else if (mode === "updates" && (data as { __interrupt__?: unknown })?.__interrupt__) this.interrupted(ns);
    return false;
  },
  find(description: string): SubEntry | undefined {
    const d = description.trim(); let best: SubEntry | undefined;
    for (const e of entries.values()) if (e.description === d && (!best || e.updatedAt > best.updatedAt)) best = e;
    return best;
  },
  all: () => [...entries.values()],
};
/** F-1 detail pane: the full timeline of one sub-agent (nothing cut except huge tool results) */
export type TimelineItem = { kind: "call" | "result" | "text"; name?: string; text: string };
export const RESULT_MAX = 4000;
export function timeline(messages: SubMsg[]): TimelineItem[] {
  const out: TimelineItem[] = [];
  for (const m of messages ?? []) {
    if (m.type === "human") continue;
    if (m.type === "ai") {
      const t = text(m.content).trim(); if (t) out.push({ kind: "text", text: t });
      for (const c of m.tool_calls ?? []) out.push({ kind: "call", name: c.name, text: c.args === undefined ? "" : JSON.stringify(c.args, null, 2) });
    } else if (m.type === "tool") { const t = text(m.content); out.push({ kind: "result", name: m.name, text: t.length > RESULT_MAX ? t.slice(0, RESULT_MAX) + "…" : t }); }
  }
  return out;
}
export function useSubagent(description: string) {
  useSyncExternalStore((f) => { subs.add(f); return () => subs.delete(f); }, () => version);
  return subagentStore.find(description);
}
