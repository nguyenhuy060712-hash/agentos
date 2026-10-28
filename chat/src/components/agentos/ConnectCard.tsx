// GĐ11.29 ConnectCard: a run paused because a service is not connected (server app/connection_runtime.py, app/agent_identity.py).
// Two pauses, one card: `auth_required` (a tool needs one service) and `credential_authorization_required` (the pre-run gate lists every missing one).
// "Kết nối" opens the sign-in in a popup (connect_url -> provider), the card long-polls /api/agent-auth/sessions/{auth_id} and resumes the run by itself when all are connected.
// A `secret` kind row is a masked input + Save. "Bỏ qua" resumes without the connection (the tool then answers by itself that it is not connected).
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLangGraphInterruptState, useLangGraphSendCommand } from "@assistant-ui/react-langgraph";
import { CheckCircle2Icon, KeyRoundIcon, LoaderIcon, PlugIcon, TriangleAlertIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { RO, getAuthSession, pasteOAuthCallback, saveConnectionSecret } from "@/lib/agentos";
import { POPUP_FEATURES, SKIP_RESUME, isAuthRequired, isCredentialGate, itemsOf, pauseKey, resumeAfter, rowStateOf, type AuthRequired, type ConnectItem, type CredentialGate, type RowState } from "./connect-helpers";

const field = "bg-background focus-visible:ring-ring/40 w-full rounded-lg border px-2.5 py-1.5 text-sm outline-none focus-visible:ring-2";

export function ThreadConnectCard() {
  const interrupt = useLangGraphInterruptState();
  const v = interrupt?.value;
  if (!isAuthRequired(v) && !isCredentialGate(v)) return null;
  return <ConnectCard key={pauseKey(v)} value={v} />;   // a new pause (new links) = a fresh card
}

function ConnectCard({ value }: { value: AuthRequired | CredentialGate }) {
  const { t } = useTranslation();
  const send = useLangGraphSendCommand();
  const items = itemsOf(value);
  const gate = isCredentialGate(value);
  const [states, setStates] = useState<Record<string, RowState>>({});
  const [err, setErr] = useState<Record<string, string>>({});
  const [phase, setPhase] = useState<"idle" | "resuming" | "skipped">("idle");
  const popups = useRef<Record<string, Window | null>>({});
  const aborts = useRef<AbortController[]>([]);
  const resumed = useRef(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, []);
  useEffect(() => () => { aborts.current.forEach((a) => a.abort()); }, []);

  const resume = async (payload: unknown, nextPhase: "resuming" | "skipped") => {
    if (resumed.current) return;
    resumed.current = true; setPhase(nextPhase);
    // LangGraphCommand is typed {resume: string}; the server expects an object -> cast (same as the approval card, finding F5)
    try { await send({ resume: payload } as unknown as { resume: string }); }
    catch { resumed.current = false; setPhase("idle"); }
  };
  const st = useRef<Record<string, RowState>>({});   // source of truth (two sign-ins can end in the same tick); `states` only re-renders
  const setRow = (n: number, s: RowState) => { st.current = { ...st.current, [String(n)]: s }; setStates(st.current); return st.current; };
  const connectedSlugs = (all: Record<string, RowState>) => items.filter((_i, n) => all[String(n)] === "connected").map((i) => i.slug);
  const finish = (n: number, s: RowState) => {
    const all = setRow(n, s);
    popups.current[String(n)]?.close();
    const ended = items.every((_i, k) => ["connected", "failed", "expired"].includes(all[String(k)] ?? ""));
    // all connected -> go on; some link died (gate) -> nothing is left to wait for: the server checks again and pauses with fresh links
    if (ended && (gate || items.every((_i, k) => all[String(k)] === "connected"))) void resume(resumeAfter(value, connectedSlugs(all)), "resuming");
  };

  const watch = async (n: number, it: ConnectItem) => {
    if (!it.authId) return;
    const ac = new AbortController(); aborts.current.push(ac);
    try {
      for (;;) {
        const s = await getAuthSession(it.authId, 25, ac.signal);
        if (s.status !== "pending") { finish(n, rowStateOf(s.status)); return; }
      }
    } catch (e) {
      if ((e as { name?: string }).name === "AbortError") return;
      finish(n, "expired");   // 404 / 410: the link is gone -> the server makes a new one on the next pause
    }
  };
  const open = (n: number, it: ConnectItem) => {
    setErr((x) => ({ ...x, [String(n)]: "" }));
    const w = it.url ? window.open(it.url, "aos-connect", POPUP_FEATURES) : null;
    popups.current[String(n)] = w;
    if (!w) { setErr((x) => ({ ...x, [String(n)]: t("connectPopupBlocked") })); return; }
    setRow(n, "waiting"); void watch(n, it);
  };
  const saved = (n: number) => { finish(n, "connected"); };
  const busy = phase !== "idle";
  const title = items.length === 1 && items[0].name ? t("connectTitleOne", { name: items[0].name }) : t("connectTitleMany", { count: items.length });
  return (
    <div ref={ref} data-testid="connect-card" data-kind={gate ? "gate" : "auth"} data-state={phase} className="mx-auto mb-3 w-full max-w-(--thread-max-width,44rem)">
      <div className="bg-card w-full max-w-md rounded-2xl border p-4 shadow-sm">
        <div className="flex items-start gap-2.5">
          <span className="bg-primary/10 text-primary mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full"><PlugIcon className="size-4" /></span>
          <div className="min-w-0"><div className="font-semibold leading-snug" data-testid="connect-title">{title}</div>
            <p className="text-muted-foreground mt-0.5 text-xs">{RO.on ? t("connectReadOnly") : t("connectHint")}</p></div>
        </div>
        <ul className="mt-3 grid gap-2">
          {items.map((it, n) => (
            <ConnectRow key={n} n={n} item={it} state={states[String(n)] ?? "idle"} err={err[String(n)]} disabled={busy || RO.on}
              onOpen={() => open(n, it)} onSaved={() => saved(n)} />))}
        </ul>
        <div className="mt-4 flex items-center justify-end gap-2">
          {phase === "resuming" && <span className="text-muted-foreground me-auto flex items-center gap-1.5 text-xs" data-testid="connect-resuming"><LoaderIcon className="size-3.5 animate-spin" />{t("connectResuming")}</span>}
          <Button type="button" variant="ghost" size="sm" disabled={busy} data-testid="connect-skip" onClick={() => void resume(SKIP_RESUME, "skipped")}>{t("connectSkip")}</Button>
        </div>
      </div>
    </div>
  );
}

function ConnectRow({ n, item, state, err, disabled, onOpen, onSaved }: { n: number; item: ConnectItem; state: RowState; err?: string; disabled: boolean; onOpen: () => void; onSaved: () => void }) {
  const { t } = useTranslation();
  const [val, setVal] = useState(""); const [saving, setSaving] = useState(false); const [msg, setMsg] = useState("");
  const [paste, setPaste] = useState(""); const [pasting, setPasting] = useState(false);
  const reconnect = item.reason === "needs_reconnect";
  const save = async () => {
    if (!val.trim() || !item.slug) return;
    setSaving(true); setMsg("");
    try { await saveConnectionSecret(item.slug, val.trim()); setVal(""); onSaved(); } catch (e) { setMsg(String((e as Error).message ?? e)); } finally { setSaving(false); }
  };
  const doPaste = async () => {
    setPasting(true); setMsg("");
    try { await pasteOAuthCallback(paste.trim()); setPaste(""); onSaved(); } catch (e) { setMsg(String((e as Error).message ?? e)); } finally { setPasting(false); }
  };
  return (
    <li data-testid="connect-row" data-slug={item.slug} data-state={state} className="rounded-xl border px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{item.name || t("connectUnnamed")}{reconnect && <span className="text-muted-foreground ms-1.5 text-xs font-normal">{t("connectReconnect")}</span>}</span>
        {state === "connected" ? <span className="flex items-center gap-1 text-xs text-emerald-600" data-testid="connect-done"><CheckCircle2Icon className="size-4" />{t("connectDone")}</span>
          : item.secret ? null
          : <Button type="button" size="sm" disabled={disabled || !item.url || state === "waiting" || state === "failed" || state === "expired"} data-testid="connect-open" onClick={onOpen}>
              {state === "waiting" ? <LoaderIcon className="size-4 animate-spin" /> : <PlugIcon className="size-4" />}{state === "waiting" ? t("connectWaiting") : t("connectButton")}</Button>}
      </div>
      {item.secret && state !== "connected" && (
        <form className="mt-2 flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); void save(); }}>
          <KeyRoundIcon className="text-muted-foreground size-4 shrink-0" aria-hidden />
          <input type="password" autoComplete="new-password" value={val} onChange={(e) => setVal(e.target.value)} disabled={disabled || saving} placeholder={t("connectSecretPh")} aria-label={item.name} data-testid={`connect-secret-${n}`} className={field} />
          <Button type="submit" size="sm" disabled={disabled || saving || !val.trim()} data-testid="connect-save">{saving ? <LoaderIcon className="size-4 animate-spin" /> : null}{t("connectSave")}</Button>
        </form>)}
      {state === "failed" && <p role="alert" className="text-destructive mt-1 flex items-center gap-1 text-xs"><TriangleAlertIcon className="size-3.5" />{t("connectFailed")}</p>}
      {state === "expired" && <p role="alert" className="text-destructive mt-1 flex items-center gap-1 text-xs"><TriangleAlertIcon className="size-3.5" />{t("connectExpired")}</p>}
      {err ? <p role="alert" className="text-destructive mt-1 text-xs">{err}</p> : null}
      {msg ? <p role="alert" className="text-destructive mt-1 text-xs">{msg}</p> : null}
      {!item.secret && state !== "connected" && (
        <details className="text-muted-foreground mt-1.5 text-xs">
          <summary className="cursor-pointer select-none">{t("connectPasteSummary")}</summary>
          <div className="mt-1.5 flex items-center gap-2">
            <input value={paste} onChange={(e) => setPaste(e.target.value)} disabled={disabled || pasting} placeholder="http://127.0.0.1:8765/oauth/callback?code=…" aria-label={t("connectPasteSummary")} data-testid="connect-paste" className={`${field} font-mono`} />
            <Button type="button" size="sm" variant="outline" disabled={disabled || pasting || !paste.trim()} data-testid="connect-paste-send" onClick={() => void doPaste()}>{t("connectPasteSend")}</Button>
          </div>
        </details>)}
    </li>
  );
}
