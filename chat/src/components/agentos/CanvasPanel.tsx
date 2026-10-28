// GĐ5b Canvas: side panel next to the chat for documents/code an agent wrote with the `canvas` tool (assistant-ui "Artifacts"
// example pattern, assistant-ui.com/examples/artifacts, fetched 21/10/2026: tool card in the thread + a panel that shows the artifact).
// View (markdown / Shiki), edit (new version, optimistic-lock on base_version), versions, line diff (npm `diff`), copy, download,
// and "Chạy" for Python (POST /api/chat/code/run -> gVisor sandbox on the server). Lazy chunk.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CheckIcon, CopyIcon, DownloadIcon, FileCodeIcon, FileTextIcon, FileDownIcon, GitCompareIcon, LoaderIcon, PencilIcon, PlayIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { canvasStore, useCanvas } from "@/lib/canvasStore";
import { getCanvasPdf } from "@/lib/research";
import { getCanvas, getCanvasVersion, getCodeSettings, runCode, saveCanvas, type CanvasDoc, type CodeResult, type CodeSettings } from "@/lib/agentos";
import { canvasFileName, lineDiff, viDateTime } from "@/genui/gd5b-helpers";
import { CodeView } from "./md/shiki";
import { CodeOutput } from "@/genui/CanvasCards";

type Mode = "view" | "edit" | "diff";

export function CanvasPanel({ desktop, threadId }: { desktop: boolean; threadId?: string }) {
  const { t } = useTranslation();
  const st = useCanvas();
  const [doc, setDoc] = useState<CanvasDoc | null>(null);
  const [sel, setSel] = useState<number>(0);
  const [base, setBase] = useState<number>(0);
  const [texts, setTexts] = useState<Record<number, string>>({});
  const [mode, setMode] = useState<Mode>("view");
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState(""); const [busy, setBusy] = useState(false); const [copied, setCopied] = useState(false);
  const [run, setRun] = useState<CodeResult | null>(null); const [running, setRunning] = useState(false);
  const [code, setCode] = useState<CodeSettings | null>(null);

  const load = useCallback(async (docId: string, want?: number) => {
    setErr("");
    try {
      const d = await getCanvas(docId); setDoc(d); setTexts({ [d.version]: d.content });
      const v = want && want <= d.version ? want : d.version; setSel(v); setBase(Math.max(1, v - 1));
    } catch (e) { setErr(String((e as Error).message)); setDoc(null); }
  }, []);
  useEffect(() => { if (st.doc) { setMode("view"); setRun(null); void load(st.doc, st.v); } }, [st.doc, st.v, st.nonce, load]);
  useEffect(() => { getCodeSettings().then(setCode).catch(() => setCode(null)); }, []);
  // version texts on demand (selected + diff base)
  useEffect(() => {
    if (!doc) return;
    for (const v of mode === "diff" ? [sel, base] : [sel]) if (v && texts[v] === undefined)
      getCanvasVersion(doc.doc_id, v).then((x) => setTexts((m) => ({ ...m, [v]: x.content }))).catch((e) => setErr(String((e as Error).message)));
  }, [doc, sel, base, mode, texts]);
  useEffect(() => {
    const f = (e: KeyboardEvent) => { if (e.key === "Escape" && mode !== "edit") canvasStore.close(); };
    addEventListener("keydown", f); return () => removeEventListener("keydown", f);
  }, [mode]);
  // a different chat -> the panel closes (its document belongs to the old thread)
  useEffect(() => { if (doc && threadId && doc.thread_id !== threadId) canvasStore.close(); }, [threadId]);   // eslint-disable-line react-hooks/exhaustive-deps

  const text = texts[sel];
  const meta = doc?.versions.find((x) => x.version === sel);
  const isPy = doc?.kind === "code" && /^(python|py)$/i.test(doc.language);
  const diff = useMemo(() => (mode === "diff" && texts[base] !== undefined && text !== undefined ? lineDiff(texts[base], text) : null), [mode, texts, base, text]);
  if (!st.doc) return null;

  const save = async () => {
    if (!doc) return; setBusy(true); setErr("");
    try { const r = await saveCanvas(doc.doc_id, { content: draft, base_version: doc.version }); setMode("view"); await load(doc.doc_id, r.version); }
    catch (e) { setErr(String((e as Error).message)); } finally { setBusy(false); }
  };
  const copy = async () => { if (text === undefined) return; try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { setErr(t("copyFailed")); } };
  const download = () => {
    if (!doc || text === undefined) return;
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
    a.download = canvasFileName(doc.title, doc.kind, doc.language); a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  };
  const pdf = async () => {
    if (!doc) return; setErr("");
    try { const a = document.createElement("a"); a.href = URL.createObjectURL(await getCanvasPdf(doc.doc_id, sel || undefined));
      a.download = canvasFileName(doc.title, "document", "markdown").replace(/\.md$/, ".pdf"); a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); }
    catch (e) { setErr(`${t("pdfFailed")}: ${(e as Error).message}`); }
  };
  const exec = async () => {
    if (text === undefined) return; setRunning(true); setErr(""); setRun(null);
    try { setRun(await runCode(mode === "edit" ? draft : text, doc?.thread_id)); } catch (e) { setErr(String((e as Error).message)); } finally { setRunning(false); }
  };
  const Icon = doc?.kind === "code" ? FileCodeIcon : FileTextIcon;
  const tb = "hover:bg-muted text-muted-foreground hover:text-foreground aria-pressed:bg-muted aria-pressed:text-foreground inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-medium disabled:opacity-50";

  return (
    <aside data-testid="canvas-panel" aria-label={t("canvas")} data-doc={st.doc} data-version={sel || undefined}
      className={desktop ? "bg-background flex w-[clamp(360px,44vw,760px)] shrink-0 flex-col border-s" : "bg-background fixed inset-0 z-40 flex flex-col"}>
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <Icon className="text-muted-foreground size-4 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold" data-testid="canvas-title">{doc?.title ?? "…"}</div>
          {doc && <div className="text-muted-foreground truncate text-[11px]">{doc.kind === "code" ? `${t("canvasCode")} · ${doc.language}` : t("canvasDoc")}</div>}
        </div>
        {doc && (
          <select aria-label={t("canvasVersion")} data-testid="canvas-version" className="bg-background max-w-[46%] rounded-lg border px-1.5 py-1 text-xs" value={sel}
            onChange={(e) => { const v = Number(e.target.value); setSel(v); setBase(Math.max(1, v - 1)); setRun(null); if (mode === "edit") setMode("view"); }}>
            {[...doc.versions].reverse().map((x) => (
              <option key={x.version} value={x.version}>v{x.version} · {x.author === "user" ? t("canvasByUser") : t("canvasByAgent")} · {viDateTime(x.created).replace(/\/\d{4}/, "")}</option>))}
          </select>)}
        <button aria-label={t("close")} data-testid="canvas-close" className="hover:bg-muted rounded-lg p-1.5" onClick={() => canvasStore.close()}><XIcon className="size-4" /></button>
      </div>
      <div className="flex flex-wrap items-center gap-0.5 border-b px-2 py-1" role="toolbar" aria-label={t("canvas")}>
        {doc?.can_edit && <button className={tb} aria-pressed={mode === "edit"} data-testid="canvas-edit" disabled={text === undefined}
          onClick={() => { if (mode === "edit") setMode("view"); else { setDraft(text ?? ""); setMode("edit"); } }}><PencilIcon className="size-3.5" />{t("canvasEdit")}</button>}
        <button className={tb} aria-pressed={mode === "diff"} data-testid="canvas-diff" disabled={!doc || doc.versions.length < 2}
          onClick={() => setMode(mode === "diff" ? "view" : "diff")}><GitCompareIcon className="size-3.5" />{t("canvasCompare")}</button>
        <button className={tb} data-testid="canvas-copy" onClick={copy} disabled={text === undefined}>{copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}{copied ? t("copied") : t("copy")}</button>
        <button className={tb} data-testid="canvas-download" onClick={download} disabled={text === undefined}><DownloadIcon className="size-3.5" />{t("download")}</button>
        {doc?.kind === "document" && <button className={tb} data-testid="canvas-pdf" onClick={pdf}><FileDownIcon className="size-3.5" />{t("canvasPdf")}</button>}
        {isPy && code?.can_run && <button className={tb} data-testid="canvas-run" onClick={exec} disabled={running || text === undefined}>
          {running ? <LoaderIcon className="size-3.5 animate-spin" /> : <PlayIcon className="size-3.5" />}{t("canvasRun")}</button>}
        {meta?.note && <span className="text-muted-foreground ms-auto truncate px-1 text-[11px]" title={meta.note}>{meta.note}</span>}
      </div>
      {err && <p role="alert" data-testid="canvas-error" className="text-destructive border-b px-3 py-1.5 text-xs">{err}</p>}
      <div className="min-h-0 flex-1 overflow-auto" data-testid="canvas-body" data-mode={mode}>
        {!doc || text === undefined ? <div className="text-muted-foreground p-4 text-sm" role="status"><LoaderIcon className="me-1 inline size-4 animate-spin" />{t("loading")}</div>
        : mode === "edit" ? (
          <div className="flex h-full flex-col">
            {sel !== doc.version && <p className="text-muted-foreground px-3 pt-2 text-xs">{t("canvasEditOld", { v: sel, n: doc.version + 1 })}</p>}
            <textarea data-testid="canvas-editor" aria-label={t("canvasEdit")} spellCheck={doc.kind !== "code"} value={draft} onChange={(e) => setDraft(e.target.value)}
              className={`bg-background min-h-0 flex-1 resize-none p-4 text-sm outline-none ${doc.kind === "code" ? "font-mono" : ""}`} />
            <div className="flex items-center justify-end gap-2 border-t px-3 py-2">
              <span className="text-muted-foreground me-auto text-xs">{t("canvasChars", { n: draft.length.toLocaleString("vi-VN") })}</span>
              <Button variant="ghost" size="sm" onClick={() => setMode("view")} disabled={busy}>{t("cancel")}</Button>
              <Button size="sm" data-testid="canvas-save" onClick={save} disabled={busy || draft === text}>{busy && <LoaderIcon className="size-4 animate-spin" />}{t("canvasSaveVersion")}</Button>
            </div>
          </div>)
        : mode === "diff" ? (
          <div className="p-3">
            <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
              <label className="flex items-center gap-1.5">{t("canvasCompareWith")}
                <select data-testid="canvas-diff-base" className="bg-background rounded-lg border px-1.5 py-1" value={base} onChange={(e) => setBase(Number(e.target.value))}>
                  {doc.versions.filter((x) => x.version !== sel).map((x) => <option key={x.version} value={x.version}>v{x.version}</option>)}
                </select></label>
              {diff && <span data-testid="canvas-diff-stat"><span className="text-emerald-600">+{diff.added}</span> <span className="text-red-600">−{diff.removed}</span></span>}
            </div>
            {!diff ? <LoaderIcon className="size-4 animate-spin" /> : (
              <div className="aos-diff overflow-x-auto rounded-lg border font-mono text-xs" data-testid="canvas-diff-view">
                {diff.rows.map((r, i) => r.kind === "fold"
                  ? <div key={i} className="aos-diff-fold">⋯ {t("canvasFold", { n: r.n })}</div>
                  : <div key={i} className={`aos-diff-${r.kind}`}><span className="aos-diff-n">{r.kind === "add" ? "" : r.a}</span><span className="aos-diff-n">{r.kind === "del" ? "" : r.b}</span>
                      <span className="aos-diff-s">{r.kind === "add" ? "+" : r.kind === "del" ? "−" : " "}</span><span className="whitespace-pre">{r.text || " "}</span></div>)}
                {!diff.added && !diff.removed && <div className="text-muted-foreground p-2">{t("canvasSame")}</div>}
              </div>)}
          </div>)
        : doc.kind === "code" ? <CodeView code={text} language={doc.language} className="aos-canvas-code" />
        : <article className="aos-canvas-md p-5" data-testid="canvas-doc"><Markdown remarkPlugins={[remarkGfm]} skipHtml
            components={{ code: ({ className, children }) => { const m = /language-(\w+)/.exec(className ?? "");
              return m ? <CodeView code={String(children).replace(/\n$/, "")} language={m[1]} /> : <code>{children}</code>; } }}>{text}</Markdown></article>}
        {(run || running) && <div className="border-t p-3" data-testid="canvas-run-output">{running ? <span className="text-muted-foreground text-sm"><LoaderIcon className="me-1 inline size-4 animate-spin" />{t("codeRunning")}</span>
          : run && <CodeOutput r={{ status: run.status, exit: run.exit_code, ms: run.ms, runtime: run.runtime, stdout: run.stdout, stderr: run.stderr, images: run.images.map((x) => x.id),
            files: (run.files ?? []).filter((x) => x.id).map((x) => ({ id: x.id!, name: x.name, size: x.size ?? 0, mime_type: x.mime_type ?? "" })) }} />}</div>}
      </div>
    </aside>
  );
}
