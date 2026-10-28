// GĐ8.9 projects in the chat sidebar: the list, the open project and which project each chat belongs to (lg_threads.project_id).
// Plain external store (useSyncExternalStore), no React context: the thread-list adapter, the sidebar and the thread menu all read it.
import { useSyncExternalStore } from "react";
import type { Project } from "./agentos";

export type ProjectsState = {
  projects: Project[];
  /** the project open in the sidebar (null = normal chat list) */
  active: number | null;
  /** thread_id -> project id (null = in no project), from /api/v1/threads/search rows and local moves */
  byThread: Record<string, number | null>;
  readOnly: boolean;
};

const KEY = "aos_project";
const initialActive = (): number | null => {
  try { const v = Number(localStorage.getItem(KEY)); return Number.isInteger(v) && v > 0 ? v : null; } catch { return null; }
};
let s: ProjectsState = { projects: [], active: initialActive(), byThread: {}, readOnly: false };
const subs = new Set<() => void>();
const set = (p: Partial<ProjectsState>) => { s = { ...s, ...p }; subs.forEach((cb) => cb()); };

export const projectsStore = {
  get: () => s,
  subscribe: (cb: () => void) => { subs.add(cb); return () => { subs.delete(cb); }; },
  setProjects(projects: Project[], readOnly = false) {
    const active = s.active != null && projects.some((p) => p.id === s.active) ? s.active : null;
    if (active !== s.active) persist(active);
    set({ projects, readOnly, active });
  },
  open(id: number | null) { persist(id); set({ active: id }); },
  upsert(p: Project) {
    const i = s.projects.findIndex((x) => x.id === p.id);
    set({ projects: i < 0 ? [p, ...s.projects] : s.projects.map((x) => (x.id === p.id ? { ...x, ...p } : x)) });
  },
  remove(id: number) {
    const byThread = Object.fromEntries(Object.entries(s.byThread).map(([t, v]) => [t, v === id ? null : v]));
    if (s.active === id) persist(null);
    set({ projects: s.projects.filter((x) => x.id !== id), active: s.active === id ? null : s.active, byThread });
  },
  assign(threadId: string, projectId: number | null) { set({ byThread: { ...s.byThread, [threadId]: projectId } }); },
  /** server rows are authoritative: replaces what is known about these threads */
  seed(rows: readonly { thread_id: string; project_id?: number | null }[]) {
    const byThread = { ...s.byThread };
    for (const r of rows) byThread[r.thread_id] = r.project_id ?? null;
    set({ byThread });
  },
  reset() { s = { projects: [], active: null, byThread: {}, readOnly: false }; },
};
function persist(id: number | null) { try { if (id) localStorage.setItem(KEY, String(id)); else localStorage.removeItem(KEY); } catch { /* private mode */ } }

/** project of a thread list item: local/server map first (by remote id), else the adapter's custom.project */
export const projectOfThread = (byThread: Record<string, number | null>, remoteId: string | undefined, custom?: Record<string, unknown>): number | null => {
  if (remoteId && remoteId in byThread) return byThread[remoteId] ?? null;
  const p = custom?.project; return typeof p === "number" ? p : null;
};
/** a chat is listed only in its own project (or in the normal list when it has none), like ChatGPT projects */
export const showInList = (threadProject: number | null, active: number | null) => (threadProject ?? null) === (active ?? null);

/** "" when valid, else an i18n key */
export const projectNameError = (name: string, max = 100) => { const n = name.trim(); return !n ? "projectNameEmpty" : n.length > max ? "projectNameLong" : ""; };

export const useProjects = () => useSyncExternalStore(projectsStore.subscribe, projectsStore.get);
