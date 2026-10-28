// GĐ7.7 Deep research mode: per-thread toggle (store) -> sent as runConfig.configurable.mode = "deep_research".
// Flow on the server: propose_plan (HITL approve/edit/reject) -> researcher sub-agents -> report in Canvas. Pure helpers (unit-tested).
import { useSyncExternalStore } from "react";

export const RESEARCH_MODE = "deep_research";
export const NEW_KEY = "new";   // a chat that has no server thread yet
let on: Record<string, boolean> = {};
let snap = 0;
const subs = new Set<() => void>();
const emit = () => { snap++; subs.forEach((f) => f()); };
export const researchStore = {
  get: (key?: string | null) => !!on[key || NEW_KEY],
  set: (key: string | null | undefined, v: boolean) => { const k = key || NEW_KEY; if (!!on[k] === v) return; on = { ...on, [k]: v }; emit(); },
  /** the first run of a new chat creates its thread: the "new chat" choice moves to that thread */
  adopt: (tid: string) => { if (on[NEW_KEY] && on[tid] === undefined) { on = { ...on, [tid]: true, [NEW_KEY]: false }; emit(); } },
  subscribe: (f: () => void) => { subs.add(f); return () => { subs.delete(f); }; },
  version: () => snap,
};
export const useResearch = (key?: string | null) => useSyncExternalStore(researchStore.subscribe, () => researchStore.get(key));

/** merge extra keys into runConfig.configurable without losing what is already there */
export function withConfigurable<C extends object>(cfg: C, extra: Record<string, unknown>): C {
  if (!Object.keys(extra).length) return cfg;
  const rc = (cfg as { runConfig?: { configurable?: object } }).runConfig;
  return { ...cfg, runConfig: { ...rc, configurable: { ...rc?.configurable, ...extra } } };
}

/** HITL resume decision for propose_plan: approve as is, or edit (the user's own plan text replaces the proposed one) */
export function planDecision(kind: "approve" | "edit", name: string, plan: string) {
  return kind === "approve" ? { type: "approve" } : { type: "edit", edited_action: { name, args: { plan } } };
}
export const planOf = (reqs: { name: string; args: Record<string, unknown> }[]) =>
  reqs.length === 1 && reqs[0].name === "propose_plan" ? String(reqs[0].args.plan ?? "") : null;

export const canvasPdfUrl = (doc: string, v?: number) => `/api/chat/canvas/${encodeURIComponent(doc)}/pdf${v ? `?v=${v}` : ""}`;
/** report as PDF (the server renders it); the blob is saved by the caller (same session cookie, no keys in the browser) */
export const getCanvasPdf = async (doc: string, v?: number) => {
  const r = await fetch(canvasPdfUrl(doc, v)); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.blob();
};
