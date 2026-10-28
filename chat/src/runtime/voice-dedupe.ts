// GĐ3 fix (a): after a real call the transcript showed twice. react-langgraph keeps the spoken turns it appended locally
// (client ids) across a refetch, and the refetched thread also carries the same turns as written server-side by
// /api/voice/transcript and /api/voice/delegate (server ids). Here the server copies ADOPT the ids of the local copies they
// stand for, so the runtime reconciles them as one message. Pairing: in order, same role, only messages the client has not
// seen yet; the server text wins (it may be the delegated request or the barge-in-truncated reply).
import type { LangChainMessage } from "@assistant-ui/react-langgraph";

export type LocalMsg = { id: string; role: string; modality?: string };
const roleOf = (m: LangChainMessage) => (m.type === "human" ? "user" : m.type === "ai" ? "assistant" : m.type);
const isVoice = (m: LangChainMessage) => !!(m as { additional_kwargs?: { modality?: string } }).additional_kwargs?.modality;

export function adoptLocalVoiceIds(server: LangChainMessage[], local: readonly LocalMsg[]): LangChainMessage[] {
  const serverIds = new Set(server.map((m) => m.id).filter(Boolean) as string[]);
  const pending = local.filter((m) => m.modality === "voice" && !serverIds.has(m.id));
  if (!pending.length) return server;
  const known = new Set(local.map((m) => m.id));
  let k = 0;
  return server.map((m) => {
    if (k >= pending.length || !isVoice(m) || (m.id && known.has(m.id))) return m;
    // skip local turns of another role (e.g. a user line the server dropped because it became the delegated request)
    while (k < pending.length && pending[k]!.role !== roleOf(m)) k++;
    if (k >= pending.length) return m;
    return { ...m, id: pending[k++]!.id } as LangChainMessage;
  });
}
