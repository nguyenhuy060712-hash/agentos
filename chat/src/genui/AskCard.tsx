// GĐ5a ask-for-info card (assistant-ui "Elicitation form" pattern, docs/elements/elicitation-form fetched 20/10/2026).
// The agent calls `hoi_thong_tin` (server tool, app/chat_gd5.py) -> LangGraph interrupt {type:"ask_user", tool_call_id, title, note, fields}.
// This render-only toolkit entry shows the form for THAT pending interrupt; "Gửi" resumes the run with {answers}, "Bỏ qua" with
// {declined:true} (useLangGraphSendCommand, same thread). Once answered the card shows a read-only summary from the tool result.
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useLangGraphInterruptState, useLangGraphSendCommand } from "@assistant-ui/react-langgraph";
import { CalendarIcon, CheckCircle2Icon, ClipboardListIcon, LoaderIcon, XCircleIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { isAsk, isoToVi, maskViDate, parseAnswered, viToIso, type AskField } from "./gd5a-helpers";

type Answers = Record<string, string | number | boolean | string[]>;

const input = "bg-background focus-visible:ring-ring/40 w-full rounded-lg border px-2.5 py-1.5 text-sm outline-none focus-visible:ring-2";

export function AskCard({ args, result, toolCallId }: { args: { tieu_de?: string; ghi_chu?: string }; result?: unknown; toolCallId: string }) {
  const { t } = useTranslation();
  const interrupt = useLangGraphInterruptState();
  const send = useLangGraphSendCommand();
  const pending = isAsk(interrupt?.value) && interrupt.value.tool_call_id === toolCallId && result === undefined ? interrupt.value : null;
  const [ans, setAns] = useState<Answers>({});
  const [state, setState] = useState<"idle" | "sending" | "sent" | "declined">("idle");
  const [err, setErr] = useState("");
  const ref = useRef<HTMLFormElement>(null);
  useEffect(() => { if (pending) ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [!!pending]);   // eslint-disable-line react-hooks/exhaustive-deps
  const missing = useMemo(() => (pending?.fields ?? []).filter((f) => f.required && f.kind !== "toggle" && (ans[f.name] === undefined || ans[f.name] === "" || (Array.isArray(ans[f.name]) && !(ans[f.name] as string[]).length))), [pending, ans]);
  // GĐ5b: dates typed as dd/mm/yyyy -> must be a real date (sent as ISO)
  const badDates = useMemo(() => (pending?.fields ?? []).filter((f) => f.kind === "date" && typeof ans[f.name] === "string" && ans[f.name] !== "" && !viToIso(ans[f.name] as string)), [pending, ans]);
  const title = pending?.title ?? args.tieu_de ?? t("askTitle");

  if (result !== undefined) {   // answered (or declined): read-only summary
    const pairs = parseAnswered(result);
    return (
      <div className="aos-tool-card" data-testid="ask-card" data-state={pairs ? "answered" : "declined"}>
        <div className="aos-tool-head">{pairs ? <CheckCircle2Icon className="size-3.5 text-emerald-600" /> : <XCircleIcon className="size-3.5" />}
          <span>{pairs ? t("askAnswered") : t("askSkipped")}</span><span className="aos-tool-sub">{title}</span></div>
        {pairs && <dl className="mt-1.5 grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">{pairs.map(([k, v]) => (
          <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd className="font-medium">{v}</dd></div>))}</dl>}
      </div>);
  }
  if (!pending) {   // streaming the call, or another client already answered and the result is not loaded yet
    return (<div className="aos-tool-card aos-tool-inline" data-testid="ask-card" data-state="preparing"><LoaderIcon className="size-3.5 animate-spin" /><span>{t("askPreparing")}</span></div>);
  }
  const set = (k: string, v: Answers[string]) => { setErr(""); setAns((a) => ({ ...a, [k]: v })); };
  const submit = async (declined: boolean) => {
    if (!declined && missing.length) { setErr(t("askMissing", { fields: missing.map((f) => f.label).join(", ") })); return; }
    if (!declined && badDates.length) { setErr(t("askBadDate", { fields: badDates.map((f) => f.label).join(", ") })); return; }
    const out: Answers = { ...ans };
    for (const f of pending.fields) if (f.kind === "date" && typeof out[f.name] === "string" && out[f.name]) out[f.name] = viToIso(out[f.name] as string) ?? out[f.name];
    setErr(""); setState(declined ? "declined" : "sending");
    // LangGraphCommand is typed {resume: string}; the tool expects an object -> cast (same as the approval card, finding F5)
    try { await send({ resume: declined ? { declined: true } : { answers: out } } as unknown as { resume: string }); setState(declined ? "declined" : "sent"); }
    catch (e) { setState("idle"); setErr(String((e as Error).message ?? e)); }
  };
  const busy = state !== "idle";
  return (
    <form ref={ref} data-testid="ask-card" data-state="pending" onSubmit={(e) => { e.preventDefault(); void submit(false); }}
      className="bg-card my-2 w-full max-w-lg rounded-2xl border p-4 shadow-sm">
      <div className="flex items-start gap-2.5">
        <span className="bg-primary/10 text-primary mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full"><ClipboardListIcon className="size-4" /></span>
        <div className="min-w-0"><div className="font-semibold leading-snug">{title}</div>
          {pending.note ? <p className="text-muted-foreground mt-0.5 text-xs">{pending.note}</p> : null}</div>
      </div>
      <div className="mt-3 grid gap-3">
        {pending.fields.map((f) => (
          <Field key={f.name} f={f}>
            {f.kind === "choice" ? (
              <div role="radiogroup" aria-label={f.label} className="flex flex-wrap gap-1.5">{f.options.map((o) => (
                <button key={o} type="button" role="radio" aria-checked={ans[f.name] === o} disabled={busy} data-testid={`ask-${f.name}-${o}`} onClick={() => set(f.name, o)}
                  className="aria-checked:border-primary aria-checked:bg-primary/10 aria-checked:text-primary hover:bg-muted rounded-full border px-3 py-1 text-sm transition-colors">{o}</button>))}</div>
            ) : f.kind === "multi" ? (
              <div role="group" aria-label={f.label} className="flex flex-wrap gap-1.5">{f.options.map((o) => { const cur = (ans[f.name] as string[] | undefined) ?? []; const on = cur.includes(o);
                return <button key={o} type="button" aria-pressed={on} disabled={busy} data-testid={`ask-${f.name}-${o}`} onClick={() => set(f.name, on ? cur.filter((x) => x !== o) : [...cur, o])}
                  className="aria-pressed:border-primary aria-pressed:bg-primary/10 aria-pressed:text-primary hover:bg-muted rounded-full border px-3 py-1 text-sm transition-colors">{o}</button>; })}</div>
            ) : f.kind === "toggle" ? (
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" className="size-4 accent-current" disabled={busy} data-testid={`ask-${f.name}`}
                checked={!!ans[f.name]} onChange={(e) => set(f.name, e.target.checked)} />{t("askYes")}</label>
            ) : f.kind === "date" ? (
              <DateVi id={`ask-${toolCallId}-${f.name}`} testid={`ask-${f.name}`} disabled={busy} value={(ans[f.name] as string | undefined) ?? ""} onChange={(v) => set(f.name, v)} />
            ) : (
              <input id={`ask-${toolCallId}-${f.name}`} data-testid={`ask-${f.name}`} disabled={busy} className={input} placeholder={f.placeholder}
                type={f.kind === "number" ? "number" : "text"} inputMode={f.kind === "number" ? "decimal" : undefined}
                value={(ans[f.name] as string | undefined) ?? ""} onChange={(e) => set(f.name, e.target.value)} />
            )}
          </Field>))}
      </div>
      {err && <p role="alert" className="text-destructive mt-2 text-xs">{err}</p>}
      <div className="mt-4 flex items-center justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void submit(true)} data-testid="ask-decline">{t("askDecline")}</Button>
        <Button type="submit" size="sm" disabled={busy} data-testid="ask-submit">{state === "sending" ? <LoaderIcon className="size-4 animate-spin" /> : null}{t("askSend")}</Button>
      </div>
    </form>
  );
}

function Field({ f, children }: { f: AskField; children: ReactNode }) {
  const { t } = useTranslation();
  const lab = <>{f.label}{f.required && f.kind !== "toggle" ? <span className="text-destructive ms-0.5" aria-hidden>*</span> : <span className="text-muted-foreground ms-1 text-xs font-normal">{t("askOptional")}</span>}</>;
  return f.kind === "choice" || f.kind === "multi" || f.kind === "toggle"
    ? <div className="grid gap-1.5"><span className="text-sm font-medium">{lab}</span>{children}</div>
    : <label className="grid gap-1.5"><span className="text-sm font-medium">{lab}</span>{children}</label>;
}

/** GĐ5b: Vietnamese date field: typed as dd/mm/yyyy (slashes inserted), plus the browser's own calendar through a hidden native input. */
function DateVi({ id, testid, value, disabled, onChange }: { id: string; testid: string; value: string; disabled: boolean; onChange: (v: string) => void }) {
  const { t } = useTranslation(); const pick = useRef<HTMLInputElement>(null);
  return (
    <div className="relative">
      <input id={id} data-testid={testid} disabled={disabled} className={`${input} pe-9`} placeholder="dd/mm/yyyy" inputMode="numeric" autoComplete="off"
        value={value} onChange={(e) => onChange(e.target.value.length < value.length ? e.target.value : maskViDate(e.target.value))} aria-describedby={`${id}-fmt`} />
      <span id={`${id}-fmt`} className="sr-only">{t("askDateFormat")}</span>
      <button type="button" disabled={disabled} aria-label={t("askPickDate")} title={t("askPickDate")} data-testid={`${testid}-pick`}
        className="text-muted-foreground hover:text-foreground absolute inset-y-0 end-0 flex w-9 items-center justify-center"
        onClick={() => { const el = pick.current; if (!el) return; el.value = viToIso(value) ?? ""; try { el.showPicker(); } catch { el.focus(); } }}>
        <CalendarIcon className="size-4" /></button>
      <input ref={pick} type="date" tabIndex={-1} aria-hidden className="pointer-events-none absolute end-0 bottom-0 size-px opacity-0"
        onChange={(e) => { if (e.target.value) onChange(isoToVi(e.target.value)); }} />
    </div>);
}
