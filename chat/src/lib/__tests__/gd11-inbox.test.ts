import { beforeEach, describe, expect, it, vi } from "vitest";
import { inboxStore, markThreadRead, matchesFilter, refreshInbox } from "../inboxStore";

const rows = [{ thread_id: "a", unread: true, awaiting: false }, { thread_id: "b", unread: false, awaiting: true }, { thread_id: "c", unread: false, awaiting: false }];
const jsonRes = (body: unknown, ok = true) => ({ ok, json: async () => body }) as Response;

describe("GĐ11.35 inbox store", () => {
  beforeEach(() => inboxStore.reset());

  it("filters: all / unread / awaiting", () => {
    inboxStore.seed(rows, { unread: 1, awaiting: 1 });
    const by = inboxStore.get().byThread;
    expect(["a", "b", "c"].filter((t) => matchesFilter("unread", by[t]))).toEqual(["a"]);
    expect(["a", "b", "c"].filter((t) => matchesFilter("awaiting", by[t]))).toEqual(["b"]);
    expect(["a", "b", "c", "zz"].filter((t) => matchesFilter("all", by[t]))).toHaveLength(4);
    expect(matchesFilter("unread", undefined)).toBe(false);
  });

  it("opening a thread clears its unread dot and lowers the badge count, and tells the server", async () => {
    inboxStore.seed(rows, { unread: 1, awaiting: 1 });
    const f = vi.fn(async () => jsonRes({ ok: true })) as unknown as typeof fetch;
    await markThreadRead("a", f);
    expect(inboxStore.get().byThread.a!.unread).toBe(false);
    expect(inboxStore.get().counts.unread).toBe(0);
    expect(f).toHaveBeenCalledWith("/api/threads/a/read", { method: "POST" });
  });

  it("refresh seeds from /api/inbox; a failed call keeps the old state; disabled store does nothing", async () => {
    await refreshInbox((async () => jsonRes({ items: rows, counts: { unread: 1, awaiting: 1 } })) as unknown as typeof fetch);
    expect(inboxStore.get().counts).toEqual({ unread: 1, awaiting: 1 });
    await refreshInbox((async () => jsonRes({}, false)) as unknown as typeof fetch);
    expect(inboxStore.get().byThread.b!.awaiting).toBe(true);
    inboxStore.disable();
    const f = vi.fn(); await refreshInbox(f as unknown as typeof fetch); expect(f).not.toHaveBeenCalled();
  });
});
