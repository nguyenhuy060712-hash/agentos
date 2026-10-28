// GĐ8.6: side panel "files in this chat" = the sandbox output tree (/workspace/out) of every code run in the thread, newest state of the
// messages (tool results carry `[aos-files] {...}`); each file is a card (download, PDF/image preview). Same slot as the canvas panel.
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useAuiState } from "@assistant-ui/react";
import { FolderOpenIcon, FolderTreeIcon, XIcon } from "lucide-react";
import { filesStore } from "@/lib/filesStore";
import { fileTree, threadFiles, type TreeNode } from "@/genui/files-helpers";
import { FileCard } from "@/genui/FileCards";
import { ComputerPanel } from "./ComputerPanel";
import { ROOT, totalFiles } from "./computer-helpers";
import { getThreadBox, listBoxFiles, type BoxFile } from "@/lib/agentos";

function Node({ n, depth }: { n: TreeNode; depth: number }) {
  if (n.file) return <div style={{ paddingInlineStart: depth * 14 }}><FileCard f={n.file} compact /></div>;
  return (
    <div className="grid gap-2" data-testid="files-folder">
      <div className="text-muted-foreground flex items-center gap-1.5 text-xs font-medium" style={{ paddingInlineStart: depth * 14 }}><FolderOpenIcon className="size-3.5" />{n.name}/</div>
      {n.children.map((c) => <Node key={c.path} n={c} depth={depth + 1} />)}
    </div>);
}

export function FilesPanel({ desktop, threadId }: { desktop: boolean; threadId?: string }) {
  const { t } = useTranslation();
  const messages = useAuiState((s) => s.thread.messages);
  const files = useMemo(() => threadFiles(messages as never), [messages]);
  const [tab, setTab] = useState<"out" | "computer">(() => (files.length ? "out" : "computer"));   // GĐ11.52: "Computer" tab = the thread's virtual computer; no output files yet -> start there
  const tree = useMemo(() => fileTree(files), [files]);
  // F-4: the counter also counts what is in the thread's running computer (it showed 0 while /workspace had files); refreshed when the tab changes or output files change
  const [root, setRoot] = useState<BoxFile[]>([]);
  useEffect(() => {
    let live = true; setRoot([]);
    if (threadId) getThreadBox(threadId).then((b) => (b?.status === "running" ? listBoxFiles(b.id, ROOT) : null)).then((r) => { if (live && r) setRoot(r.items); }).catch(() => { /* counter only: the panel shows the error itself */ });
    return () => { live = false; };
  }, [threadId, tab, files.length]);
  useEffect(() => {
    const f = (e: KeyboardEvent) => { if (e.key === "Escape") filesStore.close(); };
    addEventListener("keydown", f); return () => removeEventListener("keydown", f);
  }, []);
  return (
    <aside data-testid="files-panel" aria-label={t("filesPanel")}
      className={desktop ? "bg-background flex w-[clamp(320px,34vw,520px)] shrink-0 flex-col border-s" : "bg-background fixed inset-0 z-40 flex flex-col"}>
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <FolderTreeIcon className="text-muted-foreground size-4 shrink-0" />
        <div className="min-w-0 flex-1 truncate text-sm font-semibold">{t("filesPanel")}</div>
        <span className="text-muted-foreground text-xs" data-testid="files-count">{totalFiles(files.length, root)}</span>
        <button aria-label={t("close")} data-testid="files-close" className="hover:bg-muted rounded-lg p-1.5" onClick={() => filesStore.close()}><XIcon className="size-4" /></button>
      </div>
      <div className="flex gap-1 border-b px-3 py-1.5" role="tablist">
        {(["out", "computer"] as const).map((k) => (<button key={k} role="tab" aria-selected={tab === k} data-testid={`files-tab-${k}`} onClick={() => setTab(k)}
          className={`rounded-lg px-2.5 py-1 text-xs font-medium ${tab === k ? "bg-muted" : "text-muted-foreground hover:bg-muted/60"}`}>{t(k === "out" ? "compTabOut" : "compTabComputer")}</button>))}
      </div>
      {tab === "computer" ? <ComputerPanel threadId={threadId} /> : (
      <div className="grid min-h-0 flex-1 content-start gap-2 overflow-auto p-3" data-testid="files-tree">
        {tree.length ? tree.map((n) => <Node key={n.path} n={n} depth={0} />) : <p className="text-muted-foreground text-sm" data-testid="files-empty">{t("filesEmpty")}</p>}
      </div>)}
    </aside>);
}
