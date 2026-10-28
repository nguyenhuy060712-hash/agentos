// GĐ9.8 Settings › Dữ liệu của tôi: "export my data" (zip of threads, memories, files, traces…) and "delete my data" (typed confirmation, irreversible,
// the account is locked afterwards). DataView is pure (testable); DataPanel does the calls. Server: app/privacy.py.
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { eraseMyData, exportMyData, getMe, type Me } from "@/lib/agentos";

export type DataViewProps = { me: Pick<Me, "username"> & { demo?: boolean }; busy: boolean; msg: string; done: boolean; confirm: string; open: boolean;
  onConfirm: (v: string) => void; onOpen: (o: boolean) => void; onExport: () => void; onErase: () => void };

export function DataView({ me, busy, msg, done, confirm, open, onConfirm, onOpen, onExport, onErase }: DataViewProps) {
  const { t } = useTranslation(); const ok = confirm.trim() === me.username;
  return (
    <div className="space-y-5" data-testid="data-panel">
      <p className="text-muted-foreground">{t("dataIntro")}</p>
      <section className="space-y-2 rounded-xl border p-3" data-testid="data-export">
        <h3 className="font-medium">{t("dataExportTitle")}</h3><p className="text-muted-foreground text-xs">{t("dataExportHint")}</p>
        <Button size="sm" onClick={onExport} disabled={busy} data-testid="data-export-btn">{t("dataExportBtn")}</Button></section>
      <section className="space-y-2 rounded-xl border border-red-500/40 p-3" data-testid="data-erase">
        <h3 className="font-medium text-red-600 dark:text-red-400">{t("dataEraseTitle")}</h3><p className="text-muted-foreground text-xs">{t("dataEraseHint")}</p>
        {me.demo ? <p className="text-muted-foreground text-xs" data-testid="data-demo">{t("dataDemo")}</p>
          : !open ? <Button size="sm" variant="outline" onClick={() => onOpen(true)} disabled={busy || done} data-testid="data-erase-open">{t("dataEraseBtn")}</Button>
          : (<div className="space-y-2" data-testid="data-erase-confirm">
              <p className="text-xs">{t("dataEraseType", { name: me.username })}</p>
              <input className="bg-background block w-64 rounded-lg border px-2.5 py-1.5 text-sm" value={confirm} onChange={(e) => onConfirm(e.target.value)} autoComplete="off" data-testid="data-erase-input" aria-label={t("dataEraseType", { name: me.username })} />
              <div className="flex gap-2"><Button size="sm" variant="destructive" onClick={onErase} disabled={!ok || busy || done} data-testid="data-erase-go">{t("dataEraseGo")}</Button>
                <Button size="sm" variant="outline" onClick={() => { onOpen(false); onConfirm(""); }}>{t("cancel")}</Button></div></div>)}
      </section>
      <span className="text-muted-foreground text-sm" role="status" data-testid="data-msg">{msg}</span>
    </div>);
}

export function DataPanel() {
  const { t } = useTranslation(); const [me, setMe] = useState<Me | null>(null);
  useEffect(() => { getMe().then(setMe).catch(() => setMe(null)); }, []);
  const [busy, setBusy] = useState(false); const [msg, setMsg] = useState(""); const [done, setDone] = useState(false); const [confirm, setConfirm] = useState(""); const [open, setOpen] = useState(false);
  const doExport = async () => {
    setBusy(true); setMsg("");
    try { const { blob, name } = await exportMyData(); const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click(); URL.revokeObjectURL(a.href); setMsg(t("dataExported")); }
    catch (e) { setMsg(String((e as Error).message)); } finally { setBusy(false); }
  };
  const doErase = async () => {
    setBusy(true); setMsg("");
    try { await eraseMyData(confirm.trim()); setDone(true); setMsg(t("dataErased")); setTimeout(() => { window.location.href = "/login"; }, 2500); }
    catch (e) { setMsg(String((e as Error).message)); } finally { setBusy(false); }
  };
  if (!me) return null;
  return <DataView me={me} busy={busy} msg={msg} done={done} confirm={confirm} open={open} onConfirm={setConfirm} onOpen={setOpen} onExport={doExport} onErase={doErase} />;
}
