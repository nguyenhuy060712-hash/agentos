// GĐ3.2 plan progress: Deep Agents `write_todos` (TodoListMiddleware). Each call carries the whole list; the latest call in the
// thread renders the full checklist with progress, earlier ones collapse to one line (so a live run shows the plan updating).
import { useAuiState } from "@assistant-ui/react";
import { useTranslation } from "react-i18next";
import { CheckCircle2Icon, CircleIcon, ListChecksIcon, LoaderIcon } from "lucide-react";

type Todo = { content: string; status: "pending" | "in_progress" | "completed" };
type Args = { todos?: Todo[] };

function useIsLatestTodos(toolCallId: string) {
  return useAuiState((s) => {
    for (let i = s.thread.messages.length - 1; i >= 0; i--) {
      const parts = s.thread.messages[i]!.content as readonly { type: string; toolName?: string; toolCallId?: string }[];
      for (let j = parts.length - 1; j >= 0; j--) {
        const p = parts[j]!;
        if (p.type === "tool-call" && p.toolName === "write_todos") return p.toolCallId === toolCallId;
      }
    }
    return true;
  });
}

export function TodoCard({ args, toolCallId }: { args: Args; toolCallId: string }) {
  const { t } = useTranslation();
  const todos = (args.todos ?? []).filter((x) => x && typeof x.content === "string");
  const latest = useIsLatestTodos(toolCallId);
  const done = todos.filter((x) => x.status === "completed").length;
  if (!latest) return <div className="aos-tool-card aos-tool-inline aos-gen-muted" data-testid="todos-old"><ListChecksIcon className="size-3.5" />{t("planUpdated", { done, n: todos.length })}</div>;
  return (
    <div className="aos-tool-card" data-testid="todos" data-done={done} data-total={todos.length}>
      <div className="aos-tool-head"><ListChecksIcon className="size-3.5" /><span>{t("plan")}</span><span className="aos-tool-sub">{done}/{todos.length}</span></div>
      <div className="aos-progress" role="progressbar" aria-valuemin={0} aria-valuemax={todos.length} aria-valuenow={done}><span style={{ width: `${todos.length ? (done / todos.length) * 100 : 0}%` }} /></div>
      <ul className="aos-todos">
        {todos.map((x, i) => (
          <li key={i} data-status={x.status}>
            {x.status === "completed" ? <CheckCircle2Icon className="size-4 text-emerald-600" /> : x.status === "in_progress" ? <LoaderIcon className="size-4 animate-spin text-sky-600" /> : <CircleIcon className="text-muted-foreground size-4" />}
            <span>{x.content}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
