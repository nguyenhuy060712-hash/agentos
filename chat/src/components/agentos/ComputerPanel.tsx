// GĐ11.52 "Computer" tab of the files side panel: status chip, Files (tree of /workspace, search, upload to uploads/, download, new folder, rename/move, preview)
// and Commands (run a command + history from the box API). The views are pure (tested); ComputerPanel does the calls. Server: app/sbx_api.py.
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { DownloadIcon, FileIcon, FolderIcon, FolderPlusIcon, PencilIcon, PlayIcon, UploadIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { activateComputer, boxFileUrl, boxMkdir, boxMove, boxUpload, getThreadBox, listBoxCommands, listBoxFiles, readBoxFile, runBoxCommand, startBox,
  type Box, type BoxCmd, type BoxFile, type BoxRun } from "@/lib/agentos";
import { ROOT, crumbs, fmtMs, fmtSize, isTextName, moveTarget, sortEntries, statusChip, type Chip } from "./computer-helpers";

const TONE: Record<Chip["tone"], string> = {
  ok: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200", muted: "bg-muted text-muted-foreground",
  bad: "bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200", warn: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200" };

export function StatusChip({ box }: { box: Pick<Box, "status" | "last_error" | "disk_warn"> }) {
  const { t } = useTranslation(); const c = statusChip(box);
  return <span data-testid="computer-status" data-state={c.key} title={c.key === "warn" ? (box.last_error ?? t("compWarnDisk")) : undefined}
    className={`rounded-full px-2 py-0.5 text-xs font-medium ${TONE[c.tone]}`}>{t(`compStatus_${c.key}`)}</span>;
}

export type FileListProps = { path: string; items: BoxFile[]; q: string; onOpen: (e: BoxFile) => void; onCrumb: (p: string) => void; onRename: (e: BoxFile) => void; previewPath?: string; preview?: string };
export function FileList({ path, items, q, onOpen, onCrumb, onRename, previewPath, preview }: FileListProps) {
  const { t } = useTranslation(); const rows = sortEntries(items, q);
  return (
    <div className="grid gap-2" data-testid="computer-files">
      <nav className="text-muted-foreground flex flex-wrap items-center gap-1 text-xs" aria-label={t("compPath")} data-testid="computer-crumbs">
        {crumbs(path).map((c, i, a) => (<span key={c.path} className="flex items-center gap-1">
          {i === a.length - 1 ? <b className="text-foreground">{c.name}</b> : <button className="hover:underline" onClick={() => onCrumb(c.path)}>{c.name}</button>}{i < a.length - 1 && "/"}</span>))}
      </nav>
      {rows.length === 0 ? <p className="text-muted-foreground text-sm" data-testid="computer-empty">{q ? t("compNoMatch") : t("compEmpty")}</p> : (
        <ul className="divide-y rounded-lg border">
          {rows.map((e) => (
            <li key={e.path} className="flex items-center gap-2 px-2.5 py-1.5 text-sm" data-testid="computer-entry">
              <button className="flex min-w-0 flex-1 items-center gap-2 text-start" onClick={() => onOpen(e)}>
                {e.type === "dir" ? <FolderIcon className="size-4 shrink-0 text-sky-600" /> : <FileIcon className="text-muted-foreground size-4 shrink-0" />}
                <span className="truncate">{e.name}</span></button>
              {e.type === "file" && <span className="text-muted-foreground text-xs">{fmtSize(e.size)}</span>}
              <button className="hover:bg-muted rounded p-1" aria-label={t("compRename")} title={t("compRename")} onClick={() => onRename(e)}><PencilIcon className="size-3.5" /></button>
            </li>))}
        </ul>)}
      {previewPath && (
        <div className="rounded-lg border" data-testid="computer-preview">
          <div className="text-muted-foreground border-b px-2.5 py-1 text-xs">{previewPath.split("/").pop()}</div>
          <pre className="max-h-64 overflow-auto p-2.5 text-xs whitespace-pre-wrap">{preview}</pre></div>)}
    </div>);
}

export type CommandListProps = { runs: (BoxRun & { command: string })[]; history: BoxCmd[] };
export function CommandList({ runs, history }: CommandListProps) {
  const { t } = useTranslation();
  const badge = (code: number | null) => <span className={`rounded px-1.5 text-xs font-medium ${code === 0 ? TONE.ok : TONE.bad}`} data-testid="computer-exit">{t("compExit", { code: code ?? "?" })}</span>;
  if (!runs.length && !history.length) return <p className="text-muted-foreground text-sm" data-testid="computer-nocmd">{t("compNoCommands")}</p>;
  return (
    <ul className="grid gap-2" data-testid="computer-commands">
      {runs.map((r, i) => (
        <li key={"r" + i} className="rounded-lg border p-2.5" data-testid="computer-cmd">
          <div className="flex items-center gap-2"><code className="min-w-0 flex-1 truncate text-xs">$ {r.command}</code>{badge(r.exit_code)}<span className="text-muted-foreground text-xs">{fmtMs(r.duration_ms)}</span></div>
          {r.stdout && <pre className="bg-muted/50 mt-1.5 max-h-40 overflow-auto rounded p-2 text-xs whitespace-pre-wrap">{r.stdout.slice(0, 2000)}</pre>}
        </li>))}
      {history.map((h) => (
        <li key={h.id} className="rounded-lg border p-2.5" data-testid="computer-cmd">
          <div className="flex items-center gap-2"><code className="min-w-0 flex-1 truncate text-xs">$ {h.command}</code>{badge(h.exit_code)}<span className="text-muted-foreground text-xs">{fmtMs(h.duration_ms)}</span></div>
        </li>))}
    </ul>);
}

export function ComputerPanel({ threadId }: { threadId?: string }) {
  const { t } = useTranslation();
  const [box, setBox] = useState<Box | null>(null); const [loaded, setLoaded] = useState(false); const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"files" | "commands">("files");
  const [path, setPath] = useState(ROOT); const [items, setItems] = useState<BoxFile[]>([]); const [q, setQ] = useState("");
  const [prev, setPrev] = useState<{ path: string; text: string } | undefined>();
  const [cmd, setCmd] = useState(""); const [runs, setRuns] = useState<(BoxRun & { command: string })[]>([]); const [hist, setHist] = useState<BoxCmd[]>([]);
  const up = useRef<HTMLInputElement>(null);
  const fail = (e: unknown) => setErr((e as Error).message || t("compError"));

  /* eslint-disable react-hooks/exhaustive-deps */
  useEffect(() => { setBox(null); setLoaded(false); setErr(""); if (!threadId) { setLoaded(true); return; }
    getThreadBox(threadId).then(setBox).catch(fail).finally(() => setLoaded(true)); }, [threadId]);
  const running = box?.status === "running";
  const loadFiles = useCallback(async (p: string) => { if (!box) return; try { const r = await listBoxFiles(box.id, p); setItems(r.items); setPath(r.path); setErr(""); } catch (e) { fail(e); } }, [box?.id]);
  const loadHist = useCallback(async () => { if (box) try { setHist((await listBoxCommands(box.id)).items); } catch (e) { fail(e); } }, [box?.id]);
  /* eslint-enable react-hooks/exhaustive-deps */
  useEffect(() => { if (running) { void loadFiles(ROOT); void loadHist(); } }, [running, loadFiles, loadHist]);

  const act = async (f: () => Promise<void>) => { setBusy(true); setErr(""); try { await f(); } catch (e) { fail(e); } finally { setBusy(false); } };
  const open = () => act(async () => { setBox(await activateComputer(threadId!)); });
  const wake = () => act(async () => { setBox(await startBox(box!.id)); });
  const onOpen = (e: BoxFile) => {
    setPrev(undefined);
    if (e.type === "dir") { void loadFiles(e.path); return; }
    if (isTextName(e.name)) readBoxFile(box!.id, e.path).then((text) => setPrev({ path: e.path, text })).catch(fail);
    else { const a = document.createElement("a"); a.href = boxFileUrl(box!.id, e.path); a.download = e.name; a.click(); }
  };
  const onRename = (e: BoxFile) => {
    const v = window.prompt(t("compRenamePrompt"), e.name); if (!v) return;
    const d = moveTarget(e.path, v); if (!d) { setErr(t("compBadPath")); return; }
    void act(async () => { await boxMove(box!.id, e.path, d); await loadFiles(path); });
  };
  const mkdir = () => {
    const v = window.prompt(t("compNewFolderPrompt")); if (!v) return;
    const d = moveTarget(path + "/x", v.includes("/") ? v : `${path}/${v}`); if (!d) { setErr(t("compBadPath")); return; }
    void act(async () => { await boxMkdir(box!.id, d); await loadFiles(path); });
  };
  const upload = (fl: FileList | null) => { if (!fl?.length) return; void act(async () => { await boxUpload(box!.id, [...fl], `${ROOT}/uploads`); await loadFiles(ROOT + "/uploads"); }); };
  const run = () => {
    const c = cmd.trim(); if (!c) return;
    void act(async () => { const r = await runBoxCommand(box!.id, c); setRuns((x) => [{ ...r, command: c }, ...x]); setCmd(""); void loadHist(); void loadFiles(path); });
  };

  if (!loaded) return <p className="text-muted-foreground p-3 text-sm">…</p>;
  if (!box) return (
    <div className="grid content-start gap-3 p-4 text-sm" data-testid="computer-none">
      <p className="text-muted-foreground">{t("compIntro")}</p>
      <div><Button size="sm" onClick={open} disabled={busy || !threadId} data-testid="computer-open">{t("compOpen")}</Button></div>
      {!threadId && <p className="text-muted-foreground text-xs">{t("compNeedThread")}</p>}
      {err && <p className="text-destructive text-xs" role="alert">{err}</p>}
    </div>);
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="computer-panel">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <StatusChip box={box} />
        {box.status === "stopped" && <><span className="text-muted-foreground text-xs">{t("compWakes")}</span><Button size="sm" variant="outline" onClick={wake} disabled={busy} data-testid="computer-wake">{t("compStart")}</Button></>}
        {box.status === "failed" && <Button size="sm" variant="outline" onClick={open} disabled={busy} data-testid="computer-new">{t("compNew")}</Button>}
        <div className="ms-auto flex gap-1" role="tablist">
          {(["files", "commands"] as const).map((k) => (<button key={k} role="tab" aria-selected={tab === k} data-testid={`computer-tab-${k}`} onClick={() => setTab(k)}
            className={`rounded-lg px-2.5 py-1 text-xs font-medium ${tab === k ? "bg-muted" : "text-muted-foreground hover:bg-muted/60"}`}>{t(k === "files" ? "compTabFiles" : "compTabCommands")}</button>))}
        </div>
      </div>
      {err && <p className="text-destructive px-3 pt-2 text-xs" role="alert" data-testid="computer-err">{err}</p>}
      {!running ? <p className="text-muted-foreground p-3 text-sm">{box.status === "failed" ? (box.last_error ?? t("compError")) : t("compNotRunning")}</p> : tab === "files" ? (
        <div className="grid min-h-0 flex-1 content-start gap-2 overflow-auto p-3">
          <div className="flex items-center gap-1.5">
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("compSearch")} aria-label={t("compSearch")} data-testid="computer-search" className="bg-background min-w-0 flex-1 rounded-lg border px-2.5 py-1.5 text-sm" />
            <button className="hover:bg-muted rounded-lg p-1.5" aria-label={t("compUpload")} title={t("compUploadHint")} onClick={() => up.current?.click()} data-testid="computer-upload"><UploadIcon className="size-4" /></button>
            <button className="hover:bg-muted rounded-lg p-1.5" aria-label={t("compNewFolder")} title={t("compNewFolder")} onClick={mkdir} data-testid="computer-mkdir"><FolderPlusIcon className="size-4" /></button>
            <input ref={up} type="file" multiple hidden onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
          </div>
          <FileList path={path} items={items} q={q} onOpen={onOpen} onCrumb={(p) => { setPrev(undefined); void loadFiles(p); }} onRename={onRename} previewPath={prev?.path} preview={prev?.text} />
          {prev && <a className="text-muted-foreground inline-flex items-center gap-1 text-xs hover:underline" href={boxFileUrl(box.id, prev.path)} download><DownloadIcon className="size-3.5" />{t("fileDownload")}</a>}
        </div>) : (
        <div className="grid min-h-0 flex-1 content-start gap-2 overflow-auto p-3">
          <form className="flex gap-1.5" onSubmit={(e) => { e.preventDefault(); run(); }}>
            <input value={cmd} onChange={(e) => setCmd(e.target.value)} placeholder={t("compCmdPlaceholder")} aria-label={t("compCmdPlaceholder")} data-testid="computer-cmd-input" className="bg-background min-w-0 flex-1 rounded-lg border px-2.5 py-1.5 font-mono text-xs" />
            <Button type="submit" size="sm" disabled={busy || !cmd.trim()} data-testid="computer-run"><PlayIcon className="size-3.5" />{t("compRun")}</Button>
          </form>
          <CommandList runs={runs} history={hist} />
        </div>)}
    </div>);
}
