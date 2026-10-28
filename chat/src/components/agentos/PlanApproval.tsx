// GĐ7.7 deep research: the plan the agent proposes (markdown) with Approve / Edit / Reject (buttons only, same HITL decisions as ApprovalCard).
import { useState } from "react";
import { useTranslation } from "react-i18next";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button } from "@/components/ui/button";
import type { ApprovalState } from "@/components/assistant-ui/elements/approval-card";

export function PlanApproval({ plan, state, readOnly, canEdit, canReject, onApprove, onEdit, onReject }:
  { plan: string; state: ApprovalState; readOnly: boolean; canEdit: boolean; canReject: boolean; onApprove: () => void; onEdit: (plan: string) => void; onReject: () => void }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(plan);
  const open = state === "request" && !readOnly;
  return (
    <div data-testid="plan-approval" data-state={state} className="w-full max-w-xl rounded-xl border bg-background p-3 text-sm shadow-sm">
      <div className="font-semibold">{t("planTitle")}</div>
      <p className="text-muted-foreground mt-0.5 text-xs">{readOnly ? t("approvalReadOnly") : t("planHint")}</p>
      {editing
        ? <textarea data-testid="plan-editor" aria-label={t("planEdit")} value={draft} onChange={(e) => setDraft(e.target.value)} rows={12}
            className="bg-background mt-2 w-full resize-y rounded-lg border p-2 font-mono text-xs outline-none" />
        : <article data-testid="plan-md" className="aos-canvas-md mt-2 max-h-80 overflow-auto"><Markdown remarkPlugins={[remarkGfm]} skipHtml>{plan}</Markdown></article>}
      {open && (
        <div className="mt-3 flex flex-wrap justify-end gap-2">
          {canReject && <Button variant="ghost" size="sm" data-testid="plan-reject" onClick={onReject}>{t("reject")}</Button>}
          {canEdit && !editing && <Button variant="outline" size="sm" data-testid="plan-edit" onClick={() => { setDraft(plan); setEditing(true); }}>{t("planEdit")}</Button>}
          {editing
            ? <Button size="sm" data-testid="plan-approve-edited" disabled={!draft.trim()} onClick={() => onEdit(draft)}>{t("planApproveEdited")}</Button>
            : <Button size="sm" data-testid="plan-approve" onClick={onApprove}>{t("approve")}</Button>}
        </div>)}
    </div>
  );
}
