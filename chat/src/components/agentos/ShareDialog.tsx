// GĐ5b read-only share links for one chat: create (expiry 1/7/30 days), copy, list (views, status), revoke.
// The token is shown once (only its hash is stored). No domain yet -> the viewer must be signed in to the same workspace.
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Dialog } from "@base-ui/react/dialog";
import { CheckIcon, CopyIcon, LinkIcon, LoaderIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { createShare, listShares, revokeShare, type ShareLink } from "@/lib/agentos";
import { viDateTime } from "@/genui/gd5b-helpers";

export function ShareDialog({ threadId, onClose }: { threadId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [days, setDays] = useState(7);
  const [items, setItems] = useState<ShareLink[] | null>(null);
  const [made, setMade] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); const [err, setErr] = useState(""); const [copied, setCopied] = useState(false);
  const refresh = () => listShares(threadId).then((x) => setItems(x.items)).catch((e) => setErr(String((e as Error).message)));
  useEffect(() => { void refresh(); }, [threadId]);   // eslint-disable-line react-hooks/exhaustive-deps
  const make = async () => {
    setBusy(true); setErr("");
    try { const s = await createShare(threadId, days); setMade(location.origin + (s.path ?? "")); setCopied(false); await refresh(); }
    catch (e) { setErr(String((e as Error).message)); } finally { setBusy(false); }
  };
  const copy = async () => { if (!made) return; await navigator.clipboard?.writeText(made).catch(() => {}); setCopied(true); setTimeout(() => setCopied(false), 1500); };
  const revoke = async (id: string) => { setErr(""); try { await revokeShare(id); await refresh(); } catch (e) { setErr(String((e as Error).message)); } };
  const state = (s: ShareLink) => (s.revoked ? "revoked" : new Date(s.expires) < new Date() ? "expired" : "active");
  return (
    <Dialog.Root open onOpenChange={(o) => { if (!o) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[2px]" />
        <Dialog.Popup data-testid="share-dialog" className="bg-background text-foreground fixed inset-x-3 top-[8dvh] z-50 mx-auto max-h-[84dvh] max-w-lg overflow-auto rounded-2xl border p-5 shadow-2xl outline-none">
          <div className="flex items-center gap-2">
            <LinkIcon className="size-4" /><Dialog.Title className="text-base font-semibold">{t("shareTitle")}</Dialog.Title>
            <Dialog.Close aria-label={t("close")} className="hover:bg-muted ms-auto rounded-lg p-1.5"><XIcon className="size-4" /></Dialog.Close>
          </div>
          <Dialog.Description className="text-muted-foreground mt-1 text-sm">{t("shareDesc")}</Dialog.Description>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <label className="text-sm">{t("shareExpiry")}
              <select data-testid="share-days" className="bg-background ms-2 rounded-lg border px-2 py-1.5 text-sm" value={days} onChange={(e) => setDays(Number(e.target.value))}>
                {[1, 7, 30].map((d) => <option key={d} value={d}>{t("shareDays", { n: d })}</option>)}</select></label>
            <Button size="sm" onClick={make} disabled={busy} data-testid="share-create">{busy && <LoaderIcon className="size-4 animate-spin" />}{t("shareCreate")}</Button>
          </div>
          {made && <div className="bg-muted/60 mt-3 flex items-center gap-2 rounded-lg border p-2" data-testid="share-made">
            <input readOnly value={made} aria-label={t("shareTitle")} className="min-w-0 flex-1 bg-transparent font-mono text-xs outline-none" data-testid="share-url" onFocus={(e) => e.target.select()} />
            <Button size="sm" variant="outline" onClick={copy} data-testid="share-copy">{copied ? <CheckIcon className="size-4" /> : <CopyIcon className="size-4" />}{copied ? t("copied") : t("copy")}</Button>
          </div>}
          {made && <p className="text-muted-foreground mt-1 text-xs">{t("shareOnce")}</p>}
          {err && <p role="alert" className="text-destructive mt-2 text-xs">{err}</p>}
          <h3 className="mt-5 text-sm font-semibold">{t("shareList")}</h3>
          {!items ? <LoaderIcon className="mt-2 size-4 animate-spin" /> : !items.length ? <p className="text-muted-foreground mt-1 text-sm">{t("shareNone")}</p> : (
            <ul className="mt-2 divide-y rounded-lg border" data-testid="share-list">{items.map((s) => { const k = state(s); return (
              <li key={s.id} className="flex items-center gap-2 px-3 py-2 text-sm" data-testid="share-item" data-state={k}>
                <div className="min-w-0 flex-1"><div className="font-mono text-xs">…{s.id.slice(-6)}</div>
                  <div className="text-muted-foreground text-xs">{t("shareMeta", { created: viDateTime(s.created), expires: viDateTime(s.expires), views: s.views })}</div></div>
                <span className={`rounded-full px-2 py-0.5 text-xs ${k === "active" ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400" : "bg-muted text-muted-foreground"}`}>{t(`share_${k}`)}</span>
                {k === "active" && <Button size="sm" variant="ghost" className="text-destructive" onClick={() => revoke(s.id)} data-testid="share-revoke">{t("shareRevoke")}</Button>}
              </li>); })}</ul>)}
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>);
}
