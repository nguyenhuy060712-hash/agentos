// GĐ12.87: "Save this way of working as a Skill": the server drafts a Skill from the conversation (nothing saved), the user reviews name / description / body, Save = private Skill of this agent.
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Dialog } from "@base-ui/react/dialog";
import { CheckIcon, LoaderIcon, SparklesIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { draftSkill, lintSkill, saveSkill, type SkillLint } from "@/lib/agentos";
import { canSave, errorKey, lintErrors, lintText, skillFiles } from "./skill-save-helpers";

export function SkillSaveDialog({ threadId, runId, agentId, onClose }: { threadId: string; runId: string | null; agentId: number | null; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const [name, setName] = useState(""); const [desc, setDesc] = useState(""); const [body, setBody] = useState("");
  const extra = useRef<Record<string, string>>({});
  const [lint, setLint] = useState<SkillLint | null>(null); const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(""); const [saved, setSaved] = useState("");
  const say = (e: unknown) => { const k = errorKey(e); setErr(k ? t(k) : String((e as Error).message ?? e)); };
  useEffect(() => {
    let live = true;
    draftSkill({ mode: "conversation", thread_id: threadId, run_id: runId ?? undefined, agent_id: agentId })
      .then((r) => { if (!live) return; setName(r.draft.name); setDesc(r.draft.description); setBody(r.draft.body); extra.current = r.draft.files; setLint(r.lint); })
      .catch((e) => live && say(e)).finally(() => live && setLoading(false));
    return () => { live = false; };
  }, [threadId, runId, agentId]);   // eslint-disable-line react-hooks/exhaustive-deps
  const first = useRef(true);
  useEffect(() => {   // check again 0.5 s after the last edit; Save stays off while there is an error
    if (loading) return;
    if (first.current) { first.current = false; return; }
    const h = setTimeout(() => { lintSkill(name, skillFiles(name, desc, body, extra.current), agentId).then(setLint).catch(() => {}); }, 500);
    return () => clearTimeout(h);
  }, [name, desc, body]);   // eslint-disable-line react-hooks/exhaustive-deps
  const save = async () => {
    setBusy(true); setErr("");
    try { const r = await saveSkill(name, skillFiles(name, desc, body, extra.current), agentId); setSaved(r.name); }
    catch (e) { say(e); } finally { setBusy(false); }
  };
  const ok = canSave(name, desc, body, lint) && !busy && !saved;
  const field = "bg-background w-full rounded-lg border px-2.5 py-1.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50";
  return (
    <Dialog.Root open onOpenChange={(o) => { if (!o) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[2px]" />
        <Dialog.Popup data-testid="skill-save-dialog" className="bg-background text-foreground fixed inset-x-3 top-[6dvh] z-50 mx-auto max-h-[88dvh] max-w-xl overflow-auto rounded-2xl border p-5 shadow-2xl outline-none">
          <div className="flex items-center gap-2">
            <SparklesIcon className="size-4" /><Dialog.Title className="text-base font-semibold">{t("skillSaveTitle")}</Dialog.Title>
            <Dialog.Close aria-label={t("close")} className="hover:bg-muted ms-auto rounded-lg p-1.5"><XIcon className="size-4" /></Dialog.Close>
          </div>
          <Dialog.Description className="text-muted-foreground mt-1 text-sm">{t("skillSaveDesc")}</Dialog.Description>
          {loading ? <p className="text-muted-foreground mt-4 flex items-center gap-2 text-sm" data-testid="skill-save-loading"><LoaderIcon className="size-4 animate-spin" />{t("skillSaveDrafting")}</p> : (
            <div className="mt-4 space-y-3">
              <label className="block text-sm font-medium">{t("skillSaveName")}<input data-testid="skill-save-name" className={field + " mt-1 font-mono"} value={name} onChange={(e) => setName(e.target.value)} maxLength={64} /></label>
              <label className="block text-sm font-medium">{t("skillSaveDescription")}<textarea data-testid="skill-save-description" rows={3} className={field + " mt-1"} value={desc} onChange={(e) => setDesc(e.target.value)} maxLength={1024} /></label>
              <label className="block text-sm font-medium">{t("skillSaveBody")}<textarea data-testid="skill-save-body" rows={10} className={field + " mt-1 font-mono text-xs"} value={body} onChange={(e) => setBody(e.target.value)} /></label>
              {lint && (lint.errors.length > 0 || lint.warnings.length > 0) && (
                <ul className="space-y-1 text-xs" data-testid="skill-save-lint">
                  {lint.errors.map((x, i) => <li key={"e" + i} className="text-destructive" data-kind="error">• {lintText(x, i18n.language)}</li>)}
                  {lint.warnings.map((x, i) => <li key={"w" + i} className="text-muted-foreground" data-kind="warning">• {lintText(x, i18n.language)}</li>)}
                </ul>)}
              {lintErrors(lint) > 0 && <p className="text-destructive text-xs">{t("skillSaveFix")}</p>}
            </div>)}
          {err && <p role="alert" className="text-destructive mt-3 text-sm" data-testid="skill-save-error">{err}</p>}
          {saved && <p className="mt-3 flex items-center gap-1.5 text-sm text-emerald-700 dark:text-emerald-400" data-testid="skill-save-done"><CheckIcon className="size-4" />{t("skillSaveDone", { name: saved })}</p>}
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={onClose}>{saved ? t("close") : t("cancel")}</Button>
            {!saved && <Button size="sm" disabled={!ok || loading} onClick={save} data-testid="skill-save-submit">{busy && <LoaderIcon className="size-4 animate-spin" />}{t("skillSaveSubmit")}</Button>}
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>);
}
