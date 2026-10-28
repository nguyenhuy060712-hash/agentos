// GĐ11.35 thread inbox: which chats are unread / waiting for an approval (server: GET /api/inbox, POST /api/threads/{tid}/read).
// Plain external store (like projectsStore); the sidebar chips filter the list, each row shows a blue dot or a "Chờ duyệt" badge.
export type InboxFilter = "all" | "unread" | "awaiting";
export type InboxEntry = { unread: boolean; awaiting: boolean };
export type InboxRow = { thread_id: string; unread: boolean; awaiting: boolean };
export type InboxState = { byThread: Record<string, InboxEntry>; counts: { unread: number; awaiting: number }; filter: InboxFilter; enabled: boolean };

let s: InboxState = { byThread: {}, counts: { unread: 0, awaiting: 0 }, filter: "all", enabled: true };
const subs = new Set<() => void>();
const set = (p: Partial<InboxState>) => { s = { ...s, ...p }; subs.forEach((cb) => cb()); };

/** a thread passes the chip filter ("all" shows everything; a thread the inbox does not know is neither unread nor awaiting) */
export const matchesFilter = (filter: InboxFilter, e: InboxEntry | undefined): boolean =>
  filter === "all" || (filter === "unread" ? !!e?.unread : !!e?.awaiting);

export const inboxStore = {
  get: () => s,
  subscribe: (cb: () => void) => { subs.add(cb); return () => { subs.delete(cb); }; },
  setFilter(filter: InboxFilter) { set({ filter }); },
  seed(rows: readonly InboxRow[], counts: { unread: number; awaiting: number }) {
    const byThread: Record<string, InboxEntry> = {};
    for (const r of rows) byThread[r.thread_id] = { unread: !!r.unread, awaiting: !!r.awaiting };
    set({ byThread, counts });
  },
  /** optimistic: the thread was opened / its run ended while open */
  read(tid: string) {
    const e = s.byThread[tid]; if (!e?.unread) return;
    set({ byThread: { ...s.byThread, [tid]: { ...e, unread: false } }, counts: { ...s.counts, unread: Math.max(0, s.counts.unread - 1) } });
  },
  disable() { set({ enabled: false, byThread: {}, counts: { unread: 0, awaiting: 0 }, filter: "all" }); },
  reset() { s = { byThread: {}, counts: { unread: 0, awaiting: 0 }, filter: "all", enabled: true }; },
};

/** server list -> store (the first 200 newest threads cover the sidebar; older ones simply show no badge) */
export async function refreshInbox(fetcher: typeof fetch = fetch): Promise<void> {
  if (!s.enabled) return;
  try {
    const r = await fetcher("/api/inbox?filter=all&limit=200");
    if (!r.ok) return;
    const d = (await r.json()) as { items: InboxRow[]; counts: { unread: number; awaiting: number } };
    inboxStore.seed(d.items, d.counts);
  } catch { /* offline: keep what we know */ }
}

/** mark a thread read on the server (and in the store at once); never throws */
export async function markThreadRead(tid: string, fetcher: typeof fetch = fetch): Promise<void> {
  if (!s.enabled || !tid) return;
  inboxStore.read(tid);
  try { await fetcher(`/api/threads/${encodeURIComponent(tid)}/read`, { method: "POST" }); } catch { /* next refresh corrects it */ }
}
