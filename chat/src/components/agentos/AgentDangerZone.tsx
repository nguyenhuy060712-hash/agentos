// F-2 (11.34): "Danger zone" in Settings > Agents: archive an agent, restore an archived one, delete one permanently by typing its exact name.
// The server (app/agent_purge.py) decides who may do what (owner / admin for permanent, editor for archive); this only shows its answer.
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { archiveAgent, listArchivedAgents, purgeAgent, restoreAgent, RO, type ArchivedAgent } from "@/lib/agentos";
import type { AgentProfile } from "./ui-context";
import { agentNumId, nameMatches } from "./agent-danger-helpers";

const field = "bg-background rounded-lg border px-2.5 py-1.5 text-sm";

/** one agent's permanent-delete box: type the exact name, then the red button works */
function PurgeBox({ name, onDone }: { name: string; onDone: () => void }) {
  const { t } = useTranslation(); const [typed, setTyped] = useState(""); const [busy, setBusy] = useState(false);
  return (
    <div className="grid gap-1.5" data-testid="purge-box">
      <label className="text-xs">{t("dzTypeName", { name })}
        <input data-testid="purge-confirm" className={`${field} mt-1 block w-full`} value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" /></label>
      <p className="text-muted-foreground text-xs">{t("dzKeptNote")}</p>
      <Button size="sm" variant="destructive" data-testid="purge-go" disabled={busy || !nameMatches(typed, name)}
        onClick={() => { setBusy(true); onDone(); }}>{t("dzDeleteForever")}</Button>
    </div>);
}

export function AgentDangerZone({ agents, onChanged }: { agents: AgentProfile[]; onChanged: () => void }) {
  const { t } = useTranslation();
  const [archived, setArchived] = useState<ArchivedAgent[]>([]); const [pick, setPick] = useState(""); const [msg, setMsg] = useState("");
  const [purging, setPurging] = useState<{ id: number; name: string } | null>(null);
  const reload = useCallback(() => { listArchivedAgents().then(setArchived).catch(() => setArchived([])); }, []);
  useEffect(reload, [reload]);
  if (RO.on) return null;
  const cur = agents.find((a) => a.graph_id === pick); const curId = cur ? agentNumId(cur.graph_id) : null;
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    try { await fn(); setMsg(ok); setPurging(null); setPick(""); reload(); onChanged(); } catch (e) { setMsg(String((e as Error).message)); setPurging(null); }
  };
  return (
    <details className="border-destructive/40 rounded-xl border p-3" data-testid="danger-zone">
      <summary className="text-destructive cursor-pointer font-medium">{t("dzTitle")}</summary>
      <p className="text-muted-foreground mt-1 text-xs">{t("dzHint")}</p>
      <div className="mt-3 grid gap-2">
        <select data-testid="danger-pick" className={field} value={pick} onChange={(e) => { setPick(e.target.value); setPurging(null); }}>
          <option value="">{t("dzPick")}</option>
          {agents.filter((a) => agentNumId(a.graph_id) !== null).map((a) => <option key={a.graph_id} value={a.graph_id}>{a.name}</option>)}</select>
        {cur && curId !== null && (
          <div className="grid gap-2 rounded-lg border p-2.5">
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" data-testid="danger-archive" onClick={() => void run(() => archiveAgent(curId), t("dzArchivedOk", { name: cur.name }))}>{t("dzArchive")}</Button>
              <Button size="sm" variant="destructive" data-testid="danger-delete" onClick={() => setPurging({ id: curId, name: cur.name })}>{t("dzDeleteForever")}</Button></div>
            <p className="text-muted-foreground text-xs">{t("dzArchiveHint")}</p>
            {purging?.id === curId && <PurgeBox name={purging.name} onDone={() => void run(() => purgeAgent(purging.id, purging.name), t("dzPurgedOk", { name: purging.name }))} />}
          </div>)}
        <h4 className="mt-2 text-sm font-medium">{t("dzArchivedList")} ({archived.length})</h4>
        {archived.length === 0 ? <p className="text-muted-foreground text-xs" data-testid="danger-no-archived">{t("dzNoArchived")}</p> : <div className="grid max-h-64 gap-2 overflow-y-auto" data-testid="archived-list">{archived.map((a) => (
          <div key={a.id} className="grid gap-2 rounded-lg border p-2.5" data-testid="archived-row">
            <div className="flex flex-wrap items-center gap-2"><span className="min-w-0 flex-1 truncate text-sm">{a.name}</span>
              <Button size="sm" variant="outline" data-testid="archived-restore" onClick={() => void run(() => restoreAgent(a.id), t("dzRestoredOk", { name: a.name }))}>{t("dzRestore")}</Button>
              <Button size="sm" variant="destructive" data-testid="archived-delete" onClick={() => setPurging({ id: a.id, name: a.name })}>{t("dzDeleteForever")}</Button></div>
            {purging?.id === a.id && <PurgeBox name={a.name} onDone={() => void run(() => purgeAgent(a.id, a.name), t("dzPurgedOk", { name: a.name }))} />}
          </div>))}</div>}
        <span className="text-muted-foreground text-sm" role="status" data-testid="danger-msg">{msg}</span>
      </div>
    </details>);
}
