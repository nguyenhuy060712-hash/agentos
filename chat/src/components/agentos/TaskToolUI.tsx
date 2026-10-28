// G1: Deep Agents `task` tool (sub-agent delegation) rendered as a progress card: live steps from the sub-agent's subgraph
// (stream_subgraphs) and, after reload, its checkpointed state (getState subgraphs=true). HITL raised inside it uses the normal card.
import { makeAssistantToolUI } from "@assistant-ui/react";
import { useTranslation } from "react-i18next";
import { useSubagent, type SubMsg } from "@/runtime/subagents";
import { subagentPane } from "@/lib/subagentPane";

type Args = { description?: string; subagent_type?: string };
const txt = (c: unknown) => (typeof c === "string" ? c : Array.isArray(c) ? c.map((b) => (b && typeof b === "object" && "text" in b ? String((b as { text: unknown }).text) : "")).join(" ") : "");

function step(m: SubMsg, i: number) {
  if (m.type === "human") return null;
  if (m.type === "ai" && m.tool_calls?.length) return <li key={i}>🔧 {m.tool_calls.map((c) => c.name).join(", ")}</li>;
  if (m.type === "tool") return <li key={i}>↳ {m.name}: {txt(m.content).slice(0, 120)}</li>;
  const t = txt(m.content).trim(); return t ? <li key={i}>💬 {t.slice(0, 160)}</li> : null;
}

export function TaskCard({ args, result, status }: { args: Args; result?: unknown; status: { type: string } }) {
  const { t } = useTranslation();
  const sub = useSubagent(args.description ?? "");
  const done = result !== undefined;
  const state = done ? "done" : sub?.status === "interrupted" || status.type === "requires-action" ? "interrupted" : "running";
  return (
    <div data-testid="subagent-card" data-state={state} className="my-2 rounded-lg border bg-muted/30 p-3 text-sm">
      <div className="flex items-center gap-2">
        <span aria-hidden>🧩</span>
        <span className="font-medium">{/research/i.test(args.subagent_type ?? "") ? t("researcher") : `${t("subagent")}: ${args.subagent_type ?? "?"}`}</span>
        <span className={`ml-auto rounded px-1.5 text-xs ${state === "done" ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200" : state === "interrupted" ? "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200" : "bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200"}`}
              data-testid="subagent-status">{t(`subagent_${state}`)}</span>
      </div>
      <p className="mt-1 text-muted-foreground">{args.description}</p>
      {args.description && <button type="button" data-testid="subagent-open" onClick={() => subagentPane.open(args.description!)} className="mt-1 text-xs underline underline-offset-2 hover:text-foreground">{t("subDetails")}</button>}
      {sub && (sub.messages.length > 1 || sub.live) && (
        <ul data-testid="subagent-steps" className="mt-2 space-y-0.5 border-l pl-3 text-xs text-muted-foreground">
          {sub.messages.map(step)}{sub.live && <li>💬 {sub.live.slice(-160)}</li>}
        </ul>)}
      {done && <p className="mt-2 text-xs" data-testid="subagent-result">{txt(result).slice(0, 300)}</p>}
    </div>
  );
}

export const TaskToolUI = makeAssistantToolUI<Args, unknown>({ toolName: "task", render: (p) => <TaskCard args={p.args} result={p.result} status={p.status} /> });
