// GĐ11.52: write_file / edit_file tool calls as a card with a coloured line diff (green = added, red = removed). write_file shows the whole file as added.
import { useTranslation } from "react-i18next";
import { lineDiff } from "@/components/agentos/computer-helpers";

type Args = { file_path?: string; path?: string; content?: string; old_string?: string; new_string?: string };
const ROW = { "+": "bg-emerald-500/15 text-emerald-900 dark:text-emerald-200", "-": "bg-red-500/15 text-red-900 dark:text-red-200", " ": "text-muted-foreground" } as const;

export function FileDiffCard({ tool, args, result }: { tool: "write_file" | "edit_file"; args: Args; result?: unknown }) {
  const { t } = useTranslation();
  const d = tool === "write_file" ? lineDiff("", args.content ?? "") : lineDiff(args.old_string ?? "", args.new_string ?? "");
  const name = args.file_path ?? args.path ?? "?";
  const failed = typeof result === "string" && /^error/i.test(result.trim());
  return (
    <div className="my-2 overflow-hidden rounded-lg border text-xs" data-testid="filediff-card" data-tool={tool}>
      <div className="bg-muted/40 flex items-center gap-2 border-b px-2.5 py-1.5">
        <span className="min-w-0 flex-1 truncate font-mono">{name}</span>
        <span className="text-emerald-700 dark:text-emerald-300" data-testid="filediff-added">+{d.added}</span><span className="text-red-700 dark:text-red-300" data-testid="filediff-removed">-{d.removed}</span>
        {failed && <span className="text-destructive">{t("diffFailed")}</span>}
      </div>
      <pre className="max-h-64 overflow-auto font-mono leading-5">
        {d.lines.map((l, i) => <div key={i} className={`px-2.5 whitespace-pre-wrap ${ROW[l.op]}`} data-op={l.op}>{l.op} {l.text}</div>)}
        {d.cut && <div className="text-muted-foreground px-2.5">…</div>}
      </pre>
    </div>);
}
