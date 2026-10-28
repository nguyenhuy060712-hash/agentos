// GĐ6.9 Settings › Cá nhân hoá: two free-text boxes + on/off, stored per user and added to the system prompt of every chat run (demo: read-only).
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { getInstructions, putInstructions, type Instructions } from "@/lib/agentos";

export type PersonalViewProps = { value: Instructions; msg: string; dirty: boolean; onChange: (v: Instructions) => void; onSave: () => void };

export function PersonalView({ value, msg, dirty, onChange, onSave }: PersonalViewProps) {
  const { t } = useTranslation(); const ro = !!value.read_only; const max = value.max_chars;
  const box = (k: "about" | "style", label: string) => (
    <label className="block space-y-1.5"><span className="font-medium">{label}</span>
      <textarea data-testid={`pers-${k}`} className="bg-background min-h-28 w-full rounded-lg border px-2.5 py-1.5 text-sm" maxLength={max} value={value[k]} disabled={ro}
        onChange={(e) => onChange({ ...value, [k]: e.target.value })} />
      <span className="text-muted-foreground block text-end text-xs" data-testid={`pers-${k}-count`}>{t("persCount", { n: value[k].length, max })}</span></label>);
  return (
    <div className="space-y-4" data-testid="personal-panel">
      <p className="text-muted-foreground">{t("persIntro")}</p>
      <label className="flex items-center gap-2.5"><input type="checkbox" role="switch" data-testid="pers-enabled" checked={value.enabled} disabled={ro}
        onChange={(e) => onChange({ ...value, enabled: e.target.checked })} />{t("persOn")}</label>
      {box("about", t("persAbout"))}{box("style", t("persStyle"))}
      {ro && <p className="text-muted-foreground text-xs" data-testid="pers-demo">{t("persDemo")}</p>}
      <div className="flex items-center gap-3"><Button size="sm" onClick={onSave} disabled={ro || !dirty} data-testid="pers-save">{t("save")}</Button>
        <span className="text-muted-foreground text-sm" role="status" data-testid="pers-msg">{msg}</span></div>
    </div>);
}

export function PersonalPanel() {
  const { t } = useTranslation();
  const [saved, setSaved] = useState<Instructions | null>(null); const [v, setV] = useState<Instructions | null>(null); const [msg, setMsg] = useState("");
  useEffect(() => { getInstructions().then((x) => { setSaved(x); setV(x); }).catch((e) => setMsg(String((e as Error).message))); }, []);
  if (!v || !saved) return msg ? <p className="text-destructive text-sm">{msg}</p> : null;
  const save = async () => {
    try { const x = await putInstructions({ about: v.about, style: v.style, enabled: v.enabled }); setSaved(x); setV(x); setMsg(t("saved")); } catch (e) { setMsg(String((e as Error).message)); }
  };
  const dirty = v.about !== saved.about || v.style !== saved.style || v.enabled !== saved.enabled;
  return <PersonalView value={v} msg={msg} dirty={dirty} onChange={(x) => { setV(x); setMsg(""); }} onSave={save} />;
}
