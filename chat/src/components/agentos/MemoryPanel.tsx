// GĐ5a Settings › Bộ nhớ: the long-term memories agents keep about the signed-in user (LangGraph Store, own namespace per user),
// with search/filter, edit (editor+) and forget (anyone, own memories). Admins also see agent-wide shared memories and can turn
// long-term memory on/off per agent (a new agent version; nothing is deleted). Server: app/chat_gd5.py.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { BrainIcon, PencilIcon, SearchIcon, Trash2Icon, UsersIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { deleteMemory, getMemories, getMemoryAgents, patchMemory, setMemoryAgent, type Memory, type MemoryAgent } from "@/lib/agentos";
import { AgentAvatar, type AgentProfile } from "./ui-context";

const field = "bg-background rounded-lg border px-2.5 py-1.5 text-sm";
const fold = (s: string) => s.normalize("NFD").replace(/\p{M}/gu, "").replace(/đ/g, "d").replace(/Đ/g, "D").toLowerCase();

export function MemoryPanel({ agents }: { agents: AgentProfile[] }) {
  const { t, i18n } = useTranslation();
  const [data, setData] = useState<{ items: Memory[]; can_edit: boolean; is_admin: boolean } | null>(null);
  const [mAgents, setMAgents] = useState<MemoryAgent[]>([]);
  const [agent, setAgent] = useState(""); const [q, setQ] = useState("");
  const [edit, setEdit] = useState<{ key: string; text: string } | null>(null);
  const [msg, setMsg] = useState(""); const [err, setErr] = useState(""); const [sure, setSure] = useState<string | null>(null);
  const byId = useMemo(() => new Map(agents.map((a) => [a.graph_id, a])), [agents]);
  const load = useCallback(() => getMemories().then(setData).catch((e) => setErr(String((e as Error).message))), []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (data?.is_admin) getMemoryAgents().then(setMAgents).catch(() => {}); }, [data?.is_admin]);
  const items = useMemo(() => (data?.items ?? []).filter((m) => (!agent || m.agent_id === agent) && (!q.trim() || fold(m.text).includes(fold(q.trim())))), [data, agent, q]);
  const present = useMemo(() => [...new Set((data?.items ?? []).map((m) => m.agent_id))], [data]);
  const say = (s: string) => { setMsg(s); setErr(""); setTimeout(() => setMsg((x) => (x === s ? "" : x)), 2500); };
  const fail = (e: unknown) => setErr(String((e as Error).message ?? e));
  const save = async (m: Memory) => {
    if (!edit?.text.trim()) return;
    try { await patchMemory({ agent_id: m.agent_id, key: m.key, text: edit.text.trim(), shared: m.shared }); setEdit(null); say(t("saved")); void load(); } catch (e) { fail(e); }
  };
  const forget = async (m: Memory) => {
    if (sure !== m.key) { setSure(m.key); return; }    // two-step: first click arms the button ("Quên?"), second forgets
    setSure(null);
    try { await deleteMemory({ agent_id: m.agent_id, key: m.key, shared: m.shared }); say(t("memForgotten")); void load(); } catch (e) { fail(e); }
  };
  const date = (s: string | null) => (s ? new Date(s).toLocaleDateString(i18n.language === "en" ? "en-GB" : "vi-VN", { timeZone: "Asia/Bangkok", day: "2-digit", month: "2-digit", year: "numeric" }) : "");
  const visibleAgents = mAgents.filter((a) => byId.has(a.agent_id));
  return (
    <div className="space-y-4" data-testid="memory-panel">
      <p className="text-muted-foreground">{t("memIntro")}</p>
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-48 flex-1">
          <SearchIcon className="text-muted-foreground pointer-events-none absolute start-2.5 top-1/2 size-4 -translate-y-1/2" aria-hidden />
          <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("memSearch")} aria-label={t("memSearch")} data-testid="memory-search" className={`${field} w-full ps-8`} />
        </div>
        <select value={agent} onChange={(e) => setAgent(e.target.value)} className={field} aria-label={t("agent")} data-testid="memory-agent-filter">
          <option value="">{t("memAllAgents")}</option>
          {present.map((id) => <option key={id} value={id}>{byId.get(id)?.name ?? data?.items.find((m) => m.agent_id === id)?.agent_name ?? id}</option>)}
        </select>
      </div>
      {err && <p role="alert" className="text-destructive text-sm">{err}</p>}
      <span className="text-muted-foreground text-sm" role="status" data-testid="memory-msg">{msg}</span>
      {!data ? <p className="text-muted-foreground">…</p> : items.length === 0 ? (
        <div className="text-muted-foreground flex flex-col items-center gap-2 rounded-xl border border-dashed p-8 text-center" data-testid="memory-empty">
          <BrainIcon className="size-6" aria-hidden />{data.items.length ? t("memNoMatch") : t("memEmpty")}</div>
      ) : (
        <ul className="space-y-2" data-testid="memory-list">
          {items.map((m) => {
            const a = byId.get(m.agent_id); const editing = edit?.key === m.key; const canEdit = data.can_edit && (!m.shared || data.is_admin);
            return (
              <li key={`${m.agent_id}/${m.shared ? "s" : "u"}/${m.key}`} className="rounded-xl border p-3" data-testid="memory-item" data-key={m.key}>
                <div className="text-muted-foreground mb-1.5 flex items-center gap-2 text-xs">
                  <AgentAvatar agent={a ?? { name: m.agent_name, color: "#64748b" }} size={20} />
                  <span className="text-foreground font-medium">{a?.name ?? m.agent_name}</span>
                  {m.shared && <span className="inline-flex items-center gap-1 rounded-full border px-1.5 py-px"><UsersIcon className="size-3" />{t("memShared")}</span>}
                  <span className="ms-auto">{date(m.updated_at ?? m.created_at)}</span>
                </div>
                {editing ? (
                  <div className="space-y-2">
                    <textarea value={edit.text} onChange={(e) => setEdit({ key: m.key, text: e.target.value })} rows={3} maxLength={2000} className={`${field} w-full`} data-testid="memory-edit-text" aria-label={t("memEdit")} />
                    <div className="flex justify-end gap-2"><Button size="sm" variant="ghost" onClick={() => setEdit(null)}>{t("cancel")}</Button>
                      <Button size="sm" onClick={() => void save(m)} data-testid="memory-save" disabled={!edit.text.trim()}>{t("save")}</Button></div>
                  </div>
                ) : (
                  <div className="flex items-start gap-2">
                    <p className="min-w-0 flex-1 whitespace-pre-wrap" data-testid="memory-text">{m.text}</p>
                    {canEdit && <Button size="icon" variant="ghost" className="size-8" aria-label={t("memEdit")} title={t("memEdit")} data-testid="memory-edit" onClick={() => setEdit({ key: m.key, text: m.text })}><PencilIcon className="size-4" /></Button>}
                    {(!m.shared || data.is_admin) && (sure === m.key
                      ? <Button size="sm" variant="destructive" className="h-8" data-testid="memory-delete-confirm" onClick={() => void forget(m)} onBlur={() => setSure(null)} autoFocus>{t("memForgetConfirm")}</Button>
                      : <Button size="icon" variant="ghost" className="text-destructive size-8" aria-label={t("memForget")} title={t("memForget")} data-testid="memory-delete" onClick={() => void forget(m)}><Trash2Icon className="size-4" /></Button>)}
                  </div>
                )}
              </li>);
          })}
        </ul>
      )}
      {data?.is_admin && visibleAgents.length > 0 && (
        <details className="rounded-xl border p-3" data-testid="memory-agents">
          <summary className="cursor-pointer font-medium">{t("memAgentsTitle")} <span className="text-muted-foreground text-xs">({visibleAgents.filter((a) => a.enabled).length}/{visibleAgents.length})</span></summary>
          <p className="text-muted-foreground mt-1 text-xs">{t("memAgentsHint")}</p>
          <div className="mt-2 grid gap-1 sm:grid-cols-2">
            {visibleAgents.map((a) => (
              <label key={a.agent_id} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={a.enabled} data-testid={`memory-enable-${a.agent_id}`}
                onChange={(e) => { const on = e.target.checked; const put = (v: boolean) => setMAgents((xs) => xs.map((x) => (x.agent_id === a.agent_id ? { ...x, enabled: v } : x)));
                  put(on);   // optimistic: the box flips at once, reverts if the server refuses
                  void setMemoryAgent(a.agent_id, on).then(() => say(t("saved"))).catch((err) => { put(!on); fail(err); }); }} />
                <span className="truncate">{byId.get(a.agent_id)?.name ?? a.name}</span></label>))}
          </div>
        </details>)}
    </div>
  );
}
