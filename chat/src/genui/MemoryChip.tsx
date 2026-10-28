// GĐ5a "em đã ghi nhớ" chip (assistant-ui Memory chips pattern, docs/elements/memory-chips fetched 20/10/2026): the `luu_ghi_nho`
// tool call of the agent renders inline as a pill with the remembered text; "Quên" deletes exactly that memory (key from the tool
// result "[mem:<key>]", own namespace, DELETE /api/chat/memories) and "Bộ nhớ" opens Settings › Bộ nhớ.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { BrainIcon, LoaderIcon } from "lucide-react";
import { deleteMemory } from "@/lib/agentos";
import { useChatUi } from "@/components/agentos/ui-context";
import { memKey } from "./gd5a-helpers";


export function MemoryChip({ args, result }: { args: { noi_dung?: string }; result?: unknown }) {
  const { t } = useTranslation(); const ui = useChatUi();
  const [st, setSt] = useState<"saved" | "forgetting" | "forgotten" | "error">("saved");
  const key = memKey(result); const text = String(args.noi_dung ?? "");
  if (result === undefined) return (<div className="aos-mem" data-testid="memory-chip" data-state="saving"><LoaderIcon className="size-3.5 animate-spin" /><span>{t("memSaving")}</span></div>);
  const forget = async () => {
    if (!key || !ui) return; setSt("forgetting");
    try { await deleteMemory({ agent_id: ui.agent.graph_id, key }); setSt("forgotten"); } catch (e) { setSt((e as { status?: number }).status === 404 ? "forgotten" : "error"); }
  };
  return (
    <div className="aos-mem" data-testid="memory-chip" data-state={st} data-key={key}>
      <BrainIcon className="size-3.5 shrink-0" aria-hidden />
      <span className="aos-mem-label">{st === "forgotten" ? t("memForgotten") : t("memSaved")}</span>
      <span className={st === "forgotten" ? "aos-mem-text line-through opacity-60" : "aos-mem-text"} title={text}>{text}</span>
      {key && st !== "forgotten" && (
        <button type="button" className="aos-mem-btn" onClick={() => void forget()} disabled={st === "forgetting"} data-testid="memory-forget">{st === "forgetting" ? "…" : t("memForget")}</button>)}
      <a className="aos-mem-btn" href="#/settings/memory" data-testid="memory-open">{t("memOpen")}</a>
      {st === "error" && <span role="alert" className="text-destructive text-xs">{t("memError")}</span>}
    </div>
  );
}
