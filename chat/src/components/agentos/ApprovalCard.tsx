// HITL approval via BUTTONS only (never by voice), design §7. Uses the assistant-ui "Approval card" element (Base UI flavour).
// Deep Agents interrupt value = {action_requests:[{name,args,description}], review_configs:[{action_name, allowed_decisions}]}
// Resume = command {resume:{decisions:[...]}} through useLangGraphSendCommand -> runs/stream on Agent OS (same thread).
// GĐ3 leftover (case 9): every pending action is listed with a readable label, risk note and formatted args (VND, channel);
// the raw JSON stays one click away. One decision applies to all actions of the interrupt (Deep Agents expects one per request).
import { RO } from "@/lib/agentos";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { useLangGraphInterruptState, useLangGraphSendCommand } from "@assistant-ui/react-langgraph";
import { ApprovalCard as ApprovalCardElement, type ApprovalState } from "@/components/assistant-ui/elements/approval-card";
import { PlanApproval } from "./PlanApproval";
import { planDecision, planOf } from "@/lib/research";
import { describeAction, type Lang } from "./approval-format";
import { isOfficeTool, officeRows, officeTitle } from "./office-approval";
import { skippedApproval } from "@/runtime/turn-queue";

type ActionRequest = { name: string; args: Record<string, unknown>; description?: string };
type ReviewCfg = { action_name: string; allowed_decisions: string[] };

export function ThreadApprovalCard({ onDecided }: { onDecided?: (decision: "approve" | "reject") => void }) {
  const { t, i18n } = useTranslation();
  const lang: Lang = i18n.language?.startsWith("en") ? "en" : "vi";
  const interrupt = useLangGraphInterruptState();
  const send = useLangGraphSendCommand();
  const [state, setState] = useState<ApprovalState>("request");
  const skippedKey = useSyncExternalStore(skippedApproval.subscribe, skippedApproval.get);   // GĐ11.36: the user typed a new message instead
  const ref = useRef<HTMLDivElement>(null);
  const fromNs = (interrupt as { ns?: string[] } | undefined)?.ns;   // G1: raised inside a sub-agent (subgraph)
  const v = interrupt?.value as { action_requests?: ActionRequest[]; review_configs?: ReviewCfg[] } | undefined;
  const key = JSON.stringify(v?.action_requests ?? null);
  useEffect(() => { setState("request"); if (v?.action_requests?.length) ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [key]);   // new interrupt -> fresh card in view
  if (!v?.action_requests?.length) return null;
  const reqs = v.action_requests;
  const skipped = skippedKey === key;
  const canReject = (v.review_configs ?? []).every((c) => !c.allowed_decisions || c.allowed_decisions.includes("reject"));
  const plan = planOf(reqs);   // GĐ7.7 deep research: propose_plan shows the plan itself
  const decide = async (type: "approve" | "reject" | "edit", edited?: string) => {
    setState(type === "reject" ? "denied" : "running");
    const decisions = reqs.map(() => (type === "reject" ? { type: "reject", message: "Rejected in chat" } : type === "edit" ? planDecision("edit", reqs[0].name, edited ?? "") : { type: "approve" }));
    // LangGraphCommand is typed {resume: string}; the HITL middleware expects an object -> cast (finding F5).
    try { await send({ resume: { decisions } } as unknown as { resume: string }); setState(type === "reject" ? "denied" : "done"); onDecided?.(type === "reject" ? "reject" : "approve"); }
    catch { setState("request"); }
  };
  if (plan !== null) {
    const cfg = v.review_configs?.[0];
    return (
      <div ref={ref} data-testid="approval-card" data-tool="propose_plan" className="mx-auto mb-3 flex w-full max-w-(--thread-max-width,44rem) flex-col items-center gap-1">
        <PlanApproval plan={plan} state={state} readOnly={RO.on} canEdit={!cfg?.allowed_decisions || cfg.allowed_decisions.includes("edit")} canReject={canReject}
          onApprove={() => decide("approve")} onEdit={(p) => decide("edit", p)} onReject={() => decide("reject")} />
      </div>
    );
  }
  const d = reqs.map((r) => {
    const x = describeAction(r.name, r.args, lang);
    return isOfficeTool(r.name) ? { ...x, title: officeTitle(r.name, lang), rows: officeRows(r.args, r.description, lang) } : x;   // GĐ11.62: real recipients / attendees / attachments
  });
  const many = reqs.length > 1;
  const details = d.flatMap((x, i) => [...(many ? [{ label: `${i + 1}.`, value: x.title }] : []), ...x.rows]);
  const risks = [...new Set(d.map((x) => x.risk).filter(Boolean))].join(" ");
  const title = many ? t("approvalTitleN", { count: reqs.length }) : `${t("approvalTitle")}: ${d[0].title}`;
  return (
    <div ref={ref} data-testid="approval-card" data-tool={reqs.map((r) => r.name).join(",")} data-from-subagent={fromNs?.length ? "1" : undefined}
      className="mx-auto mb-3 flex w-full max-w-(--thread-max-width,44rem) flex-col items-center gap-1">
      <ApprovalCardElement
        className="max-w-md"
        state={skipped ? "denied" : state} variant={d.some((x) => x.destructive) ? "destructive" : "default"} title={title}
        subtitle={`${reqs.map((r) => r.name).join(", ")}${fromNs?.length ? ` · ${t("fromSubagent")}` : ""}`}
        description={[risks, skipped ? t("approvalSkipped") : RO.on ? t("approvalReadOnly") : t("approvalHint")].filter(Boolean).join(" ")}
        details={details}
        onAllowOnce={RO.on || skipped ? undefined : () => decide("approve")} onDeny={canReject && !RO.on && !skipped ? () => decide("reject") : undefined}
        allowOnceLabel={many ? t("approveAll") : t("approve")} denyLabel={t("reject")} data-state={state}
      />
      <details className="w-full max-w-md text-xs text-muted-foreground" data-testid="approval-raw">
        <summary className="cursor-pointer select-none px-1">{t("rawArgs")}</summary>
        <pre className="mt-1 max-h-48 overflow-auto rounded-lg bg-muted p-2 font-mono whitespace-pre-wrap break-words">{JSON.stringify(reqs.map((r) => ({ tool: r.name, args: r.args })), null, 1)}</pre>
      </details>
    </div>
  );
}
