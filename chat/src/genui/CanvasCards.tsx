// GĐ5b tool cards: `canvas` (opens the side panel, assistant-ui Artifacts pattern) and `chay_code_python` (gVisor run: code, output, charts).
import { useEffect, useRef, useState } from "react";
import { useAuiState } from "@assistant-ui/react";
import { useTranslation } from "react-i18next";
import { AlertTriangleIcon, CheckCircle2Icon, ChevronDownIcon, FileCodeIcon, FileTextIcon, LoaderIcon, PanelRightOpenIcon, TerminalIcon, TimerOffIcon } from "lucide-react";
import { canvasStore, useCanvas } from "@/lib/canvasStore";
import { canvasRef, parseCodeRun, type CodeRun } from "./gd5b-helpers";
import { CodeView } from "@/components/agentos/md/shiki";
import { FileCards } from "./FileCards";

type Status = { type: string } | undefined;
const AUTO_OPENED = new Set<string>();

export function CanvasCard({ args, result, status }: { args: { tieu_de?: string; loai?: string; ngon_ngu?: string; ghi_chu?: string }; result?: unknown; status?: Status }) {
  const { t } = useTranslation();
  const ref = canvasRef(result);
  const cur = useCanvas();
  // opened automatically only when the card saw the call stream live (not when an old chat is loaded)
  // (mounted while the thread runs, or before its result = this turn is live; loading an old chat mounts it idle with its result).
  // Each version opens by itself at most once per page, so closing the panel sticks even if the card re-mounts.
  const running = useAuiState((s) => s.thread.isRunning);
  const live = useRef(running || (result === undefined && status?.type !== "incomplete"));
  useEffect(() => {
    if (!ref || !live.current) return; const k = `${ref.doc}:${ref.v}`; live.current = false;
    if (!AUTO_OPENED.has(k)) { AUTO_OPENED.add(k); canvasStore.open(ref.doc, ref.v); }
  }, [ref?.doc, ref?.v]);   // eslint-disable-line react-hooks/exhaustive-deps
  const Icon = args.loai === "code" ? FileCodeIcon : FileTextIcon;
  if (!ref) {
    const failed = typeof result === "string";
    return (<div className="aos-tool-card aos-tool-inline" data-testid="canvas-card" data-state={failed ? "error" : "writing"}>
      {failed ? <AlertTriangleIcon className="size-3.5 text-amber-600" /> : <LoaderIcon className="size-3.5 animate-spin" />}
      <span>{failed ? String(result).slice(0, 160) : t("canvasWriting", { title: args.tieu_de ?? "" })}</span></div>);
  }
  const open = cur.doc === ref.doc;
  return (
    <button type="button" data-testid="canvas-card" data-state="ready" data-doc={ref.doc} aria-pressed={open} onClick={() => (open ? canvasStore.close() : canvasStore.open(ref.doc, ref.v))}
      className="aos-canvas-card hover:bg-muted/70 aria-pressed:border-primary/60 my-2 flex w-full max-w-md items-center gap-3 rounded-xl border p-3 text-start transition-colors">
      <span className="bg-primary/10 text-primary flex size-9 shrink-0 items-center justify-center rounded-lg"><Icon className="size-4.5" /></span>
      <span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{args.tieu_de || t("canvas")}</span>
        <span className="text-muted-foreground block truncate text-xs">v{ref.v} · {args.loai === "code" ? `${t("canvasCode")}${args.ngon_ngu ? ` · ${args.ngon_ngu}` : ""}` : t("canvasDoc")}{args.ghi_chu ? ` · ${args.ghi_chu}` : ""}</span></span>
      <PanelRightOpenIcon className="text-muted-foreground size-4 shrink-0" aria-hidden /><span className="sr-only">{open ? t("close") : t("canvasOpen")}</span>
    </button>);
}

export function CodeOutput({ r }: { r: CodeRun }) {
  const { t } = useTranslation();
  const ok = r.status === "ok";
  return (
    <div data-testid="code-output" data-status={r.status} className="grid gap-2">
      <div className={`flex items-center gap-1.5 text-xs font-medium ${ok ? "text-emerald-700 dark:text-emerald-400" : "text-amber-700 dark:text-amber-400"}`}>
        {ok ? <CheckCircle2Icon className="size-3.5" /> : r.status === "timeout" ? <TimerOffIcon className="size-3.5" /> : <AlertTriangleIcon className="size-3.5" />}
        <span>{ok ? t("codeOk") : r.status === "timeout" ? t("codeTimeout") : t("codeError", { code: r.exit })}</span>
        <span className="text-muted-foreground font-normal">· {(r.ms / 1000).toLocaleString("vi-VN", { maximumFractionDigits: 1 })} s{r.runtime ? ` · ${r.runtime}` : ""}</span>
      </div>
      {r.stdout && <pre className="aos-code-out" data-testid="code-stdout">{r.stdout}</pre>}
      {r.stderr && <pre className="aos-code-out aos-code-err" data-testid="code-stderr">{r.stderr}</pre>}
      {r.images.length > 0 && <div className="grid gap-2 sm:grid-cols-2" data-testid="code-images">{r.images.map((id, i) => (
        <a key={id} href={`/api/files/${id}`} target="_blank" rel="noreferrer" className="bg-white overflow-hidden rounded-lg border">
          <img src={`/api/files/${id}`} alt={t("codeChart", { n: i + 1 })} loading="lazy" className="block h-auto w-full" /></a>))}</div>}
      {r.files && r.files.length > 0 && <FileCards files={r.files} />}
      {!r.stdout && !r.stderr && !r.images.length && !r.files?.length && <span className="text-muted-foreground text-xs">{t("codeNoOutput")}</span>}
    </div>);
}

export function CodeRunCard({ args, result }: { args: { code?: string }; result?: unknown }) {
  const { t } = useTranslation();
  const r = parseCodeRun(result);
  const [showCode, setShowCode] = useState(false);
  return (
    <div className="aos-tool-card" data-testid="code-card" data-state={r ? r.status : typeof result === "string" ? "refused" : "running"}>
      <button type="button" className="aos-tool-head w-full" aria-expanded={showCode} onClick={() => setShowCode((x) => !x)} data-testid="code-toggle">
        {r || typeof result === "string" ? <TerminalIcon className="size-3.5" /> : <LoaderIcon className="size-3.5 animate-spin" />}
        <span>{r || typeof result === "string" ? t("codeRan") : t("codeRunning")}</span><span className="aos-tool-sub">Python · {t("codeSandbox")}</span>
        <ChevronDownIcon className={`ms-auto size-3.5 transition-transform ${showCode ? "rotate-180" : ""}`} />
      </button>
      {showCode && args.code && <div className="mt-1.5 max-h-72 overflow-auto"><CodeView code={args.code} language="python" /></div>}
      {r ? <div className="mt-2"><CodeOutput r={r} /></div> : typeof result === "string" && <p className="text-muted-foreground mt-1 text-xs" data-testid="code-refused">{result.slice(0, 300)}</p>}
    </div>);
}
