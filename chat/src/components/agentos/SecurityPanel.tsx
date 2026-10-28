// GĐ6.7 Settings › Bảo mật: two-factor (TOTP) status/enable/disable/backup codes + signed-in devices (revoke). SecurityView is pure (testable); SecurityPanel loads the data.
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { getSessions, getTwoFa, revokeOtherSessions, revokeSession, twoFaDisable, twoFaEnable, twoFaRegenerate, twoFaSetup, type SessionRow, type TwoFaStatus } from "@/lib/agentos";

const field = "bg-background rounded-lg border px-2.5 py-1.5 text-sm";
export type Setup = { secret: string; qr_svg: string };
export type SecurityViewProps = {
  status: TwoFaStatus; sessions: SessionRow[]; setup: Setup | null; recovery: string[] | null; dialog: "disable" | "regen" | null; msg: string;
  onStart: () => void; onEnable: (code: string) => void; onRegen: (code: string) => void; onDisable: (password: string, v: string) => void;
  onDialog: (d: "disable" | "regen" | null) => void; onRecoverySaved: () => void; onRevoke: (id: string) => void; onRevokeOthers: () => void;
};

export function SecurityView(p: SecurityViewProps) {
  const { t } = useTranslation();
  const [code, setCode] = useState(""); const [pw, setPw] = useState("");
  const { status: st } = p;
  return (
    <div className="space-y-5" data-testid="security-panel">
      {st.must_enroll && <p className="rounded-lg border border-amber-500/50 bg-amber-500/10 p-3" role="alert" data-testid="sec-must">{t("secMust")}</p>}
      <section className="space-y-3 rounded-xl border p-3" data-testid="sec-2fa">
        <div className="flex items-center gap-2"><h3 className="font-medium">{t("secTitle")}</h3>
          <span className="ms-auto rounded-full border px-2 py-0.5 text-xs" data-testid="sec-state">{st.enabled ? t("secOn") : t("secOff")}</span></div>
        <p className="text-muted-foreground text-xs">{st.demo ? t("secDemo") : st.enabled ? t("secLeft", { count: st.recovery_left }) : t("secIntro")}</p>
        {p.recovery ? (
          <div className="space-y-2" data-testid="sec-recovery">
            <h4 className="font-medium">{t("secRecoveryTitle")}</h4><p className="text-muted-foreground text-xs">{t("secRecoveryHint")}</p>
            <pre className="bg-muted rounded-lg p-3 font-mono text-sm leading-7">{p.recovery.join("\n")}</pre>
            <Button size="sm" onClick={p.onRecoverySaved} data-testid="sec-recovery-done">{t("secSaved")}</Button></div>
        ) : p.setup ? (
          <div className="space-y-2" data-testid="sec-setup">
            <p>{t("secScan")}</p>
            <img alt="QR" width={160} height={160} className="rounded-lg bg-white p-1" src={`data:image/svg+xml;utf8,${encodeURIComponent(p.setup.qr_svg)}`} data-testid="sec-qr" />
            <p className="text-muted-foreground text-xs">{t("secManual")} <code className="font-mono">{p.setup.secret}</code></p>
            <p>{t("secEnter")}</p>
            <div className="flex gap-2"><input className={`${field} w-36`} inputMode="numeric" autoComplete="one-time-code" maxLength={7} placeholder={t("secCode")} value={code} onChange={(e) => setCode(e.target.value)} data-testid="sec-code" />
              <Button size="sm" onClick={() => p.onEnable(code.trim())} data-testid="sec-confirm">{t("secConfirm")}</Button></div></div>
        ) : p.dialog ? (
          <div className="space-y-2" data-testid={`sec-${p.dialog}`}>
            {p.dialog === "disable" && <input className={`${field} block w-60`} type="password" placeholder={t("secPassword")} value={pw} onChange={(e) => setPw(e.target.value)} data-testid="sec-pw" />}
            <input className={`${field} block w-60`} placeholder={t("secCodeOrRecovery")} value={code} onChange={(e) => setCode(e.target.value)} data-testid="sec-code" />
            <div className="flex gap-2"><Button size="sm" onClick={() => (p.dialog === "disable" ? p.onDisable(pw, code.trim()) : p.onRegen(code.trim()))} data-testid="sec-go">{p.dialog === "disable" ? t("secDisable") : t("secRegen")}</Button>
              <Button size="sm" variant="outline" onClick={() => p.onDialog(null)}>{t("cancel")}</Button></div></div>
        ) : !st.demo && (
          <div className="flex flex-wrap gap-2">{st.enabled ? (<>
            <Button size="sm" variant="outline" onClick={() => p.onDialog("regen")} data-testid="sec-regen">{t("secRegen")}</Button>
            <Button size="sm" variant="outline" onClick={() => p.onDialog("disable")} data-testid="sec-disable">{t("secDisable")}</Button></>
          ) : <Button size="sm" onClick={p.onStart} data-testid="sec-enable">{t("secEnable")}</Button>}</div>)}
      </section>
      <section className="space-y-2" data-testid="sec-sessions">
        <div className="flex items-center gap-2"><h3 className="font-medium">{t("secSessions")}</h3>
          <Button size="sm" variant="outline" className="ms-auto" onClick={p.onRevokeOthers} data-testid="sec-revoke-others">{t("secRevokeOthers")}</Button></div>
        <ul className="divide-y rounded-lg border">
          {p.sessions.map((s) => (
            <li key={s.id} className="flex items-center gap-3 p-2.5" data-testid={`sec-session-${s.id}`}>
              <span className="min-w-0 flex-1"><span className="block truncate font-medium">{s.device || "?"}{s.current && <span className="ms-2 rounded-full border px-2 py-0.5 text-xs" data-testid="sec-current">{t("secThis")}</span>}</span>
                <span className="text-muted-foreground block truncate text-xs">{s.ip} · {t("secLastSeen")}: {s.last_seen.replace("T", " ").slice(0, 16)}</span></span>
              <Button size="sm" variant="outline" onClick={() => p.onRevoke(s.id)} data-testid={`sec-revoke-${s.id}`}>{s.current ? t("secSignOut") : t("secRevoke")}</Button></li>))}
          {!p.sessions.length && <li className="text-muted-foreground p-3">{t("secNoSessions")}</li>}
        </ul>
      </section>
      {p.msg && <p className="text-muted-foreground text-sm" role="status" data-testid="sec-msg">{p.msg}</p>}
    </div>);
}

export function SecurityPanel() {
  const [status, setStatus] = useState<TwoFaStatus | null>(null); const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [setup, setSetup] = useState<Setup | null>(null); const [recovery, setRecovery] = useState<string[] | null>(null);
  const [dialog, setDialog] = useState<"disable" | "regen" | null>(null); const [msg, setMsg] = useState("");
  const load = useCallback(() => { setMsg(""); Promise.all([getTwoFa(), getSessions()]).then(([a, b]) => { setStatus(a); setSessions(b); }).catch((e) => setMsg(String((e as Error).message))); }, []);
  useEffect(load, [load]);
  const run = (f: () => Promise<void>) => { setMsg(""); f().catch((e) => setMsg(String((e as Error).message))); };
  if (!status) return msg ? <p className="text-destructive text-sm">{msg}</p> : null;
  return (<SecurityView status={status} sessions={sessions} setup={setup} recovery={recovery} dialog={dialog} msg={msg}
    onStart={() => run(async () => setSetup(await twoFaSetup()))}
    onEnable={(c) => run(async () => { const r = await twoFaEnable(c); setSetup(null); setRecovery(r.recovery_codes); })}
    onRegen={(c) => run(async () => { const r = await twoFaRegenerate(c); setDialog(null); setRecovery(r.recovery_codes); })}
    onDisable={(pw, v) => run(async () => { await twoFaDisable(pw, v); setDialog(null); load(); })}
    onDialog={setDialog} onRecoverySaved={() => { setRecovery(null); load(); }}
    onRevoke={(id) => run(async () => { const r = await revokeSession(id); if (r.current) location.href = "/login"; else load(); })}
    onRevokeOthers={() => run(async () => { await revokeOtherSessions(); load(); })} />);
}
