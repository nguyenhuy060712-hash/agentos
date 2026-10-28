// GĐ5b Settings › Quyền: Python run in the gVisor sandbox: on/off + which roles may run (admin edits; others see the state).
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { TerminalIcon } from "lucide-react";
import { getCodeSettings, putCodeSettings, type CodeSettings } from "@/lib/agentos";

export function CodeRunSettings() {
  const { t } = useTranslation();
  const [s, setS] = useState<CodeSettings | null>(null); const [msg, setMsg] = useState("");
  useEffect(() => { getCodeSettings().then(setS).catch((e) => setMsg(String((e as Error).message))); }, []);
  if (!s) return msg ? <p className="text-destructive text-sm">{msg}</p> : null;
  const put = async (b: { enabled: boolean; roles: string[] }) => {
    setMsg(""); const prev = s; setS({ ...s, ...b });
    try { setS(await putCodeSettings(b)); setMsg(t("saved")); } catch (e) { setS(prev); setMsg(String((e as Error).message)); }
  };
  const ro = !s.is_admin;
  return (
    <section className="space-y-2 rounded-xl border p-3" data-testid="code-settings">
      <div className="flex items-center gap-2"><TerminalIcon className="size-4" /><h3 className="text-sm font-semibold">{t("codeSettings")}</h3>
        <label className="ms-auto flex items-center gap-2 text-sm"><input type="checkbox" role="switch" className="size-4 accent-current" checked={s.enabled} disabled={ro}
          onChange={(e) => put({ enabled: e.target.checked, roles: s.roles })} data-testid="code-enabled" />{s.enabled ? t("schedOn") : t("schedOff")}</label></div>
      <p className="text-muted-foreground text-xs" data-testid="code-limits">{t("codeLimits", { rt: s.runtime || "?", mem: s.limits.memory_mb, cpu: s.limits.cpus, sec: s.timeout_s, n: s.limits.runs_per_10min })}</p>
      <div className="flex flex-wrap items-center gap-3 text-sm"><span className="text-muted-foreground">{t("codeRoles")}</span>
        {["admin", "editor", "viewer"].map((r) => (
          <label key={r} className="flex items-center gap-1.5"><input type="checkbox" className="size-4 accent-current" disabled={ro || r === "admin"} checked={s.roles.includes(r)}
            onChange={(e) => put({ enabled: s.enabled, roles: e.target.checked ? [...s.roles, r] : s.roles.filter((x) => x !== r) })} data-testid={`code-role-${r}`} />{t(`role_${r}`)}</label>))}
      </div>
      {ro && <p className="text-muted-foreground text-xs">{t("codeAdminOnly")}</p>}
      {msg && <p className="text-muted-foreground text-xs" role="status">{msg}</p>}
    </section>);
}
