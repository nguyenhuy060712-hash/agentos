// F-1 (11.36 step 4): right-side detail pane of one sub-agent: its full timeline (tool calls with arguments, results, text). Same slot as the files / canvas panel.
import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { XIcon } from "lucide-react";
import { useSubagent, timeline } from "@/runtime/subagents";
import { subagentPane } from "@/lib/subagentPane";

export function SubagentPane({ desktop, description }: { desktop: boolean; description: string }) {
  const { t } = useTranslation();
  const sub = useSubagent(description);
  const items = timeline(sub?.messages ?? []);
  useEffect(() => {
    const f = (e: KeyboardEvent) => { if (e.key === "Escape") subagentPane.close(); };
    addEventListener("keydown", f); return () => removeEventListener("keydown", f);
  }, []);
  return (
    <aside data-testid="subagent-pane" aria-label={t("subPaneTitle")}
      className={desktop ? "bg-background flex w-[clamp(320px,34vw,520px)] shrink-0 flex-col border-s" : "bg-background fixed inset-0 z-40 flex flex-col"}>
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <span aria-hidden>🧩</span>
        <div className="min-w-0 flex-1 truncate text-sm font-semibold">{t("subPaneTitle")}</div>
        <button aria-label={t("close")} data-testid="subagent-pane-close" className="hover:bg-muted rounded-lg p-1.5" onClick={() => subagentPane.close()}><XIcon className="size-4" /></button>
      </div>
      <div className="grid min-h-0 flex-1 content-start gap-2 overflow-auto p-3 text-sm">
        <p className="text-muted-foreground" data-testid="subagent-pane-task">{description}</p>
        {sub && <span className="text-xs font-medium" data-testid="subagent-pane-status">{t(`subagent_${sub.status}`)}</span>}
        {items.length === 0 && !sub?.live && <p className="text-muted-foreground" data-testid="subagent-pane-empty">{t("subNoSteps")}</p>}
        <ol className="grid gap-2" data-testid="subagent-pane-timeline">
          {items.map((it, i) => (
            <li key={i} data-kind={it.kind} className="rounded-lg border p-2.5">
              {it.kind !== "text" && <div className="text-muted-foreground mb-1 text-xs font-medium">{it.kind === "call" ? `🔧 ${t("subCall")}` : `↳ ${t("subResult")}`}: {it.name}</div>}
              {it.kind === "text" ? <p className="whitespace-pre-wrap">{it.text}</p> : it.text ? <pre className="bg-muted/50 max-h-64 overflow-auto rounded p-2 text-xs whitespace-pre-wrap">{it.text}</pre> : null}
            </li>))}
          {sub?.live ? <li className="rounded-lg border p-2.5"><p className="whitespace-pre-wrap">{sub.live}</p></li> : null}
        </ol>
      </div>
    </aside>);
}
