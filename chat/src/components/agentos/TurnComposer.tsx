// GĐ11.36 chat turn UI: send while awaiting approval (reject all, then send), queue while running (max 5, Sửa/Xoá), turn clock, hint line.
// Logic lives in runtime/turn-queue.ts (vitest); this file only wires it to assistant-ui.
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { useAui, useAuiState } from "@assistant-ui/react";
import { useLangGraphInterruptState, useLangGraphSendCommand } from "@assistant-ui/react-langgraph";
import { PencilIcon, XIcon } from "lucide-react";
import { canvasStore } from "@/lib/canvasStore";
import { SKIP_RESUME, isConnectInterrupt } from "./connect-helpers";
import { clockLabel, getQueueMax, queuedToText, rejectAll, skippedApproval, turnClock, turnQueue } from "@/runtime/turn-queue";

/** thread key for the queue: the server id once the thread exists */
const useTid = () => useAuiState((s) => s.threadListItem.remoteId ?? s.threadListItem.id);

/** pending approval of this thread: the key of the interrupt (JSON of its action requests) + how many actions wait */
function usePendingApproval() {
  const interrupt = useLangGraphInterruptState();
  const reqs = (interrupt?.value as { action_requests?: unknown[] } | undefined)?.action_requests;
  if (isConnectInterrupt(interrupt?.value)) return { key: JSON.stringify(interrupt?.value), n: 0, connect: true };   // GĐ11.29: the connect card; a new message skips the connection
  return reqs?.length ? { key: JSON.stringify(reqs), n: reqs.length, connect: false } : null;
}

/** one place decides what "send" means right now: running -> queue; awaiting approval -> reject all then send; else normal send */
export function useTurnControl() {
  const aui = useAui(); const { t } = useTranslation();
  const tid = useTid();
  const running = useAuiState((s) => s.thread.isRunning);
  const pending = usePendingApproval();
  const send = useLangGraphSendCommand();
  const [notice, setNotice] = useState("");
  const busy = useRef(false);
  const awaiting = !running && !!pending;
  const submit = async () => {
    const c = aui.composer().getState();
    if (busy.current || (!c.text.trim() && c.attachments.length === 0)) return;
    if (running) {   // queue (attachments stay in the composer: a queued message is text only)
      if (!c.text.trim()) return;
      const ok = turnQueue.add(tid, c.text, canvasStore.get().doc ?? undefined);
      if (!ok) { setNotice(t("queueFull", { count: getQueueMax() })); return; }
      setNotice(""); aui.composer().setText(""); return;
    }
    if (awaiting && pending) {   // never auto-approve: every pending action is rejected, the new message follows in the same thread
      busy.current = true; if (!pending.connect) skippedApproval.set(pending.key);
      try { await send({ resume: pending.connect ? SKIP_RESUME : { decisions: rejectAll(pending.n) } } as unknown as { resume: string }); aui.composer().send(); }
      finally { busy.current = false; }
      return;
    }
    aui.composer().send();
  };
  return { running, awaiting, submit, notice, tid };
}

/** Enter key while running / awaiting is ours (assistant-ui's own Enter would do nothing or send into a paused run) */
export const enterHandler = (ctl: { running: boolean; awaiting: boolean; submit: () => void }) => (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
  if ((!ctl.running && !ctl.awaiting) || e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
  e.preventDefault(); void ctl.submit();
};

/** runs the next queued message when the current run is over and nothing waits for approval */
export function QueueRunner() {
  const aui = useAui(); const tid = useTid();
  const running = useAuiState((s) => s.thread.isRunning);
  const pending = usePendingApproval();
  const q = useSyncExternalStore(turnQueue.subscribe, () => turnQueue.get(tid));
  useEffect(() => {
    if (running || pending || !q.length) return;
    const id = window.setTimeout(() => {   // a tick after the run ended, so the finished answer is committed first
      const next = turnQueue.take(tid); if (next) aui.thread().append({ role: "user", content: [{ type: "text", text: queuedToText(next) }] });
    }, 50);
    return () => window.clearTimeout(id);
  }, [running, pending, q, tid, aui]);
  return null;
}

/** queued messages: dimmed, with Sửa / Xoá until they start */
export function QueueList() {
  const { t } = useTranslation(); const aui = useAui(); const tid = useTid();
  const q = useSyncExternalStore(turnQueue.subscribe, () => turnQueue.get(tid));
  if (!q.length) return null;
  return (
    <ul data-testid="turn-queue" aria-label={t("queued")} className="mb-1.5 flex flex-col gap-1 px-1">
      {q.map((m) => (
        <li key={m.id} data-testid="queued-message" className="bg-muted/60 text-muted-foreground flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm opacity-70">
          <span className="min-w-0 flex-1 truncate">{m.text}</span>
          {m.canvas ? <span data-testid="queued-canvas-chip" className="bg-background shrink-0 rounded-full border px-2 py-0.5 text-[11px]">canvas · {m.canvas}</span> : null}
          <button type="button" data-testid="queued-edit" onClick={() => { const x = turnQueue.edit(tid, m.id); if (x) aui.composer().setText(x.text); }}
            className="hover:text-foreground flex shrink-0 items-center gap-1 text-xs"><PencilIcon className="size-3" aria-hidden />{t("queuedEdit")}</button>
          <button type="button" data-testid="queued-delete" onClick={() => turnQueue.remove(tid, m.id)}
            className="hover:text-foreground flex shrink-0 items-center gap-1 text-xs"><XIcon className="size-3" aria-hidden />{t("delete")}</button>
        </li>
      ))}
    </ul>
  );
}

/** "Đang suy nghĩ… 0:12" / tool / writing while a run goes; hint line while awaiting approval; notices */
export function TurnStatus({ notice, awaiting, running }: { notice: string; awaiting: boolean; running: boolean }) {
  const { t } = useTranslation();
  const clock = useSyncExternalStore(turnClock.subscribe, turnClock.get);
  const [now, setNow] = useState(() => Date.now());
  const active = running && clock.phase !== "idle";
  useEffect(() => { if (!active) return; setNow(Date.now()); const id = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(id); }, [active]);
  const line = notice ? { id: "queue-notice", text: notice } : active ? { id: "turn-clock", text: clockLabel(clock, now, t) }
    : awaiting ? { id: "awaiting-hint", text: t("awaitingHint") } : null;
  if (!line) return null;
  return <p data-testid={line.id} role="status" className="text-muted-foreground mb-1 px-3 text-xs">{line.text}</p>;
}
