import re, collections
pages = []
for l in open("/tmp/ls_pages.txt"):
    m = re.match(r"\[(.*?)\]\((https://docs.langchain.com/langsmith/(.*?))\.md\)", l)
    if m: pages.append((m.group(3), m.group(1), m.group(2)))
D, P_, T, NA = "✅", "🟡", "⬜", "➖"
# (regex trên slug, phase, status, ghi chú)
RULES = [
 # --- N/A: tích hợp bên thứ ba / nền tảng đóng / hạ tầng không áp dụng
 (r"^trace-(anthropic|bedrock|claude|deepseek|litellm|openai(?!-compat)|with-(autogen|claude|codex|crewai|cursor|instructor|livekit|mastra|microsoft|mistral|n8n|openai-agents|opencode|pi$|pipecat|pydantic|semantic-kernel|strands|temporal|vercel|vscode))", "P1", NA, "tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini)"),
 (r"^(engine|smithtune|harbor|tuned-evaluators|usage-and-billing|llm-gateway-credits|cloud-platform-features|big-query|aes-|custom-encryption|encryption|configure-iam|deploy-to-cloud|deploy-with-control-plane|hybrid|diagnostics-self|self-hosted|env-var-cloud|env-var-self-hosted|engine-)", "-", NA, "dịch vụ đóng/cloud/billing/K8s — không áp dụng"),
 (r"^fleet/(arcade|salesforce|slack-app|teams-app|comparison|changelog|computer-use)", "P4", NA, "tích hợp SaaS bên thứ ba — cần duyệt riêng"),
 (r"^(setup-javascript|vitest-jest|serverless-environments|generative-ui-react|trace-with-vercel)", "-", NA, "JS/TS — dự án chỉ Python"),
 (r"^(trace-gemini-live|trace-voice|trace-openai-realtime|trace-with-livekit)", "P1", NA, "voice/realtime — ngoài phạm vi"),
 (r"^(langchain-runnable|openevals|pytest|test-react-agent-pytest|custom-docker|monorepo|setup-app-req|setup-pyproject|preview-builds|deploy-standalone|deploy-to-self-hosted|typesafe|custom-openai-compliant|custom-endpoint|playground-model-providers|coding-agent-metadata|llm-gateway-coding-agents|trajectory-view-integrations|langsmith-mcp-server|langsmith-remote-mcp|data-export-downstream|data-export-destinations|trace-replicas|otel-gateway)", "-", NA, "đóng gói/khác hệ sinh thái — không cần cho bản tự host"),
 # --- P1 Observability: đã làm
 (r"^(observability$|observability-concepts|observability-quickstart|observability-llm-tutorial|annotate-code|access-current-span|add-metadata-tags|log-llm-trace|log-retriever-trace|log-traces-to-project|trace-generator-functions|trace-without-env-vars|trace-with-api|nest-traces|view-traces|filter-traces$|trace-query-syntax|threads$|use-threads|dashboards|alerts|rules|sample-traces|cost-tracking|mask-inputs-outputs|runs$|run-data-format|feedback-data-format|attach-user-feedback|annotate-traces-inline|trace-with-google-gemini|trace-deep-agents|trace-with-langgraph|trace-with-langchain|conditional-tracing|log-traces-to-agent|query-threads|export-traces|manage-trace)", "P1", D, ""),
 (r"^(filter-traces-in-application|configure-input-output-preview|redact-secrets|webhooks$|trace-with-google-adk|distributed-tracing|insights|custom-output-rendering|ls-metadata-parameters|log-multimodal-traces|upload-files-with-traces|trace-with-opentelemetry|trace-with-openai-compatible|data-export$|data-export-monitor|presigned-feedback-tokens|set-up-feedback-criteria)", "P1", P_, ""),
 # --- P2 Evaluation
 (r"^(evaluation$|evaluation-concepts|evaluation-quickstart|evaluation-types|evaluators$|manage-datasets$|manage-datasets-in-application|manage-datasets-programmatically|example-data-format|code-evaluator-(sdk|ui)|llm-as-judge(-sdk)?$|create-few-shot-evaluators|metric-type|multiple-scores|evaluate-pairwise|annotation-queues$|analyze-an-experiment|compare-experiment-results|repetition|define-target-function|evaluate-llm-application|online-evaluations-code|online-evaluations-llm-as-judge|evaluation-async|experiment-configuration|fetch-perf-metrics-experiment|manage-evaluators-sdk|annotation-queues-sdk|run-evals-api-only|evaluate-chatbot-tutorial)", "P2", D, ""),
 (r"^(summary|composite-evaluators|online-evaluations-(composite|multi-turn|decision)|decision-model-evaluator|bind-evaluator-to-dataset|evaluate-existing-experiment|evaluate-with-retry|filter-experiments-ui|dataset-json-types|dataset-transformations|evaluator-spend|audit-evaluator-scores|improve-judge-evaluator|assertions|evaluate-on-intermediate-steps|trajectory-evals|evaluate-complex-agent|evaluate-rag-tutorial|evaluate-graph|local|read-local-experiment|upload-existing-experiments|evaluate-with-attachments|evaluate-with-opentelemetry|multi-turn-simulation|multiple-messages|run-backtests|optimize-classifier|evaluation-approaches|cicd-pipeline)", "P2", T, ""),
 # --- P3 Prompt
 (r"^(prompt-|create-a-prompt|manage-prompts|managing-model-configurations|use-tools|multimodal-content|write-prompt-with-ai|test-from-playground|run-evaluation-from-playground|use-the-context-hub|manage-contexts-sdk|context-|troubleshooting-variable-caching)", "P3", T, ""),
 # --- P4 Builder/Fleet/Studio
 (r"^(fleet/|studio|use-studio|quick-start-studio|observability-studio|troubleshooting-studio|custom-apps|graph-rebuild|human-in-the-loop-time-travel|add-human-in-the-loop)", "P4", T, ""),
 # --- P5 Deployment / Agent Server
 (r"^(agent-server|assistants|configuration-cloud|streaming|background-run|cron-jobs|use-webhooks|double-texting|enqueue|interrupt-concurrent|reject-concurrent|rollback-concurrent|stateless-runs|same-thread|cancel-run|configure-ttl|semantic-search|server-(a2a|mcp)|use-remote-graph|custom-(routes|middleware|lifespan|checkpointer|store)|configure-checkpointer|configurable-|application-structure|deployment-revisions|manage-deployment|monitor-deployment|handle-model-rate-limiting|caching|integrations|openapi-security|agent-server-feedback)", "P5", T, ""),
 # --- P6 Gateway
 (r"^llm-gateway", "P6", T, ""),
 # --- P7 Admin / auth / sandboxes
 (r"^(auth$|custom-auth|set-up-custom-auth|add-auth-server|agent-auth|resource-auth|store-auth|sandbox)", "P7", T, ""),
]
PRE = {  # trạng thái có từ trước P1/P2 (Agent OS v1)
 "llm-gateway": P_, "llm-gateway-fallbacks": D, "llm-gateway-spend-policies": P_, "llm-gateway-rate-limit-policies": P_, "llm-gateway-monitoring": P_, "llm-gateway-how-it-works": P_,
 "sandboxes": P_, "sandbox-permissions": P_, "deployment-revisions": D, "manage-deployment": P_, "monitor-deployment": P_, "streaming": P_,
 "fleet/index": P_, "fleet/essentials": P_, "fleet/remote-mcp-servers": D, "fleet/tools": P_, "fleet/manage-agent-settings": D, "fleet/skills": P_,
 "prompt-engineering-quickstart": P_, "create-a-prompt": P_, "manage-prompts": P_, "prompt-engineering-concepts": P_, "handle-model-rate-limiting": D, "caching": T,
 "evaluate-existing-experiment": P_, "filter-experiments-ui": P_, "evaluator-spend": P_, "evaluate-chatbot-tutorial": D, "custom-routes": D, "custom-middleware": D, "custom-lifespan": D, "configure-checkpointer": D, "custom-store": D, "semantic-search": D, "same-thread": P_,
}
NOTE = {
 "filter-traces-in-application": "DSL lọc đủ, chưa có lưu bộ lọc/saved views", "configure-input-output-preview": "preview cố định 300 ký tự",
 "redact-secrets": "SDK hide_inputs/outputs; chưa có regex redaction server-side", "webhooks": "rule webhook: nội bộ tự gửi, bên ngoài = bản nháp",
 "trace-with-google-adk": "dùng SDK @traceable thủ công", "distributed-tracing": "parent_run_id qua API, chưa có header propagation",
 "insights": "chưa có phân cụm lỗi tự động", "presigned-feedback-tokens": "có share link ký HMAC; chưa có token feedback riêng",
 "data-export": "chỉ API JSON, chưa xuất parquet", "set-up-feedback-criteria": "rubric ở annotation queue",
 "trace-with-opentelemetry": "chưa có endpoint OTLP", "upload-files-with-traces": "chưa có attachments", "log-multimodal-traces": "chưa",
 "conditional-tracing": "SDK enabled/sampling", "trace-with-google-gemini": "SDK wrap_genai", "trace-deep-agents": "agent OS tự mirror trace",
 "evaluate-existing-experiment": "chạy evaluator lên run có sẵn (run_on) — chưa theo lô cả experiment", "filter-experiments-ui": "lọc theo dataset; chưa lọc theo metadata/điểm", "evaluator-spend": "judge đi qua gateway có ngân sách/ngày chung", "trace-with-langchain": "qua agent OS (spans middleware); không dùng callback LangSmith",
}
POST = {  # P3/P4 + vá gap P1/P2 (19/10/2026, test p34 64/64)
 # P3
 "create-a-prompt": D, "manage-prompts": D, "manage-prompts-programmatically": D, "prompt-commit": D, "prompt-engineering-concepts": D, "prompt-engineering-quickstart": D,
 "prompt-template-format": P_, "managing-model-configurations": P_, "use-tools": P_, "multimodal-content": T, "write-prompt-with-ai": D, "test-from-playground": D,
 "run-evaluation-from-playground": D, "context-hub-webhooks": D, "use-the-context-hub": P_, "prompt-context-hub": P_, "manage-contexts-sdk": P_, "context-engineering-concepts": P_,
 # P4
 "studio": D, "use-studio": D, "quick-start-studio": D, "observability-studio": P_, "troubleshooting-studio": P_, "graph-rebuild": P_, "add-human-in-the-loop": D,
 "human-in-the-loop-time-travel": D, "fleet/index": D, "fleet/essentials": D, "fleet/quickstart": D, "fleet/templates": D, "fleet/schedules": D, "fleet/webhooks": D,
 "fleet/access-and-oversight": P_, "fleet/channels": P_, "fleet/code": P_, "fleet/mcp-framework": P_, "fleet/tools": D, "fleet/skills": P_, "fleet/agent-identity": T, "fleet/auth-format": T, "fleet/workspace-admin": T, "custom-apps": T,
 # gap P1
 "filter-traces-in-application": D, "redact-secrets": D, "insights": P_, "trace-with-opentelemetry": D, "upload-files-with-traces": D, "log-multimodal-traces": P_,
 # gap P2
 "summary": D, "trajectory-evals": D, "evaluate-on-intermediate-steps": D, "evaluate-complex-agent": P_, "multi-turn-simulation": D, "online-evaluations-multi-turn": D,
 "multiple-messages": P_, "upload-existing-experiments": D, "evaluate-existing-experiment": D, "evaluate-with-attachments": P_, "evaluate-with-opentelemetry": P_, "cicd-pipeline-example": P_, "local": P_, "read-local-experiment-results": T,
}
NOTE.update({
 "prompt-template-format": "chỉ {{biến}} (mustache cơ bản), chưa f-string/jinja", "managing-model-configurations": "model+nhiệt độ lưu theo commit; chưa preset dùng chung",
 "use-tools": "output_schema JSON; chưa định nghĩa tool trong playground", "multimodal-content": "chưa", "use-the-context-hub": "hub prompt + tag; chưa context file/thư mục",
 "observability-studio": "trace agent link từ studio qua project agents", "troubleshooting-studio": "", "graph-rebuild": "cache agent xoá khi commit hub/sửa agent",
 "fleet/access-and-oversight": "HITL duyệt + chặn khoá/báo động/garage server-side; chưa phân quyền (P7)", "fleet/channels": "webhook vào /d/hook; Slack/Teams cần duyệt riêng",
 "fleet/code": "sandbox chay_code_python", "fleet/mcp-framework": "MCP HA chỉ-đọc mặc định", "fleet/skills": "template + system prompt; chưa skill files",
 "insights": "gom cụm lỗi theo chữ ký chuẩn hoá (regex), chưa phân cụm ngữ nghĩa/LLM", "log-multimodal-traces": "attachments xem/tải; chưa render ảnh/audio inline",
 "trace-with-opentelemetry": "OTLP/HTTP JSON (protobuf → 415)", "redact-secrets": "regex server-side, quy tắc sửa được trên UI",
 "evaluate-complex-agent": "trajectory + LLM judge; chưa đánh giá từng node", "multiple-messages": "thread evaluator; dataset nhiều message thủ công",
 "evaluate-with-attachments": "attachments cho trace; dataset example chưa kèm file", "evaluate-with-opentelemetry": "trace OTLP chấm được bằng rule/evaluator",
 "cicd-pipeline-example": "plugin pytest → experiment; chưa mẫu CI", "local": "pytest plugin ghi lên server; chưa chế độ offline", "evaluate-existing-experiment": "",
 "filter-traces-in-application": "DSL + saved filters",
})
# ---- P5/P6/P7 + gap cuối (19/10/2026, test p567 106/106)
POST.update({
 # P5 Agent Server
 "agent-server": D, "agent-server-overview": D, "assistants": D, "configuration-cloud": D, "background-run": D, "cancel-run": D, "configure-ttl": D,
 "cron-jobs": D, "double-texting": D, "enqueue-concurrent": D, "interrupt-concurrent": D, "reject-concurrent": D, "rollback-concurrent": D,
 "stateless-runs": D, "use-webhooks": D, "streaming": P_, "same-thread": P_, "manage-deployment": P_, "monitor-deployment": P_,
 "agent-server-feedback": P_, "use-remote-graph": P_, "agent-server-scale": P_,
 # P6 Gateway
 "llm-gateway": D, "llm-gateway-how-it-works": D, "llm-gateway-quickstart": D, "llm-gateway-monitoring": D, "llm-gateway-rate-limit-policies": D,
 "llm-gateway-spend-policies": D, "llm-gateway-access": D, "llm-gateway-admin-setup": D, "llm-gateway-model-access-policies": D,
 "llm-gateway-api-formats": P_, "llm-gateway-data-policy": P_, "llm-gateway-provider-authentication": P_, "llm-gateway-direct-model-access": P_,
 "llm-gateway-model-configurations": P_,
 # P7 Admin / Auth / Sandboxes
 "auth": D, "sandboxes": D, "sandbox-permissions": D, "sandbox-download-links": P_, "sandbox-sdk": P_, "resource-auth": P_,
 "fleet/workspace-admin": D, "fleet/access-and-oversight": D,
 # gap cuối
 "insights": D, "evaluate-with-attachments": D,
})
NOTE.update({
 "streaming": "SSE metadata/updates/values/end + join/Last-Event-ID; chưa stream token (messages)", "same-thread": "nhiều assistant cùng graph trên 1 thread; khác graph → 409",
 "manage-deployment": "assistants có phiên bản + set latest; deploy = tiến trình đơn", "monitor-deployment": "/api/v1/info (hàng đợi, worker) + trace mỗi run",
 "agent-server-feedback": "run có trace_id → feedback API", "use-remote-graph": "langgraph-sdk client chạy được với /api/v1 (đã test); RemoteGraph chưa test",
 "agent-server-scale": "hàng đợi Postgres SKIP LOCKED, 2 worker/tiến trình; chưa nhiều replica",
 "llm-gateway-api-formats": "OpenAI chat/embeddings/models (stream, tools, JSON, ảnh base64); chưa định dạng Anthropic/Responses",
 "llm-gateway-data-policy": "PII mask/block (email, thẻ Luhn, SĐT VN, CCCD, IP) vào+ra; trace đã mask", "llm-gateway-provider-authentication": "1 key Gemini, chỉ admin đổi; chỉ Gemini",
 "llm-gateway-direct-model-access": "gọi trực tiếp model Gemini qua virtual key", "llm-gateway-model-configurations": "fallback + allow-list theo key; chưa alias model tuỳ biến",
 "auth": "đăng nhập nhiều user (scrypt), phiên cookie, API key, Basic tương thích; RBAC admin/editor/viewer theo workspace; audit log",
 "sandbox-download-links": "tải file qua API có xác thực; chưa link ký có hạn", "sandbox-sdk": "REST (exec/files); chưa SDK riêng", "resource-auth": "tách theo workspace; chưa riêng tư theo từng user",
 "fleet/workspace-admin": "workspace + thành viên + vai trò", "fleet/access-and-oversight": "RBAC + HITL + chặn khoá/báo động/garage server-side",
 "insights": "gom cụm theo chữ ký + ngữ nghĩa (Gemini embedding, cosine ≥0.86)", "trace-with-opentelemetry": "OTLP/HTTP JSON + protobuf (+gzip)",
 "evaluate-with-attachments": "file đính kèm example → target (text) + biến {{attachments}} cho evaluator", "log-multimodal-traces": "attachments xem/tải; chưa render ảnh/audio inline",
})
rows = []
for slug, title, url in pages:
    ph, st, nt = "?", T, ""
    for rx, p, s, n in RULES:
        if re.search(rx, slug): ph, st, nt = p, s, n; break
    if slug in PRE and st != NA: st = PRE[slug]
    if slug in POST and st != NA: st = POST[slug]
    nt = NOTE.get(slug, nt)
    if ph == "-": ph = {"P": "P5"}.get("", "Khác")
    rows.append((ph, slug, title, url, st, nt))
order = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "Khác", "?"]
names = {"P1": "P1 Observability", "P2": "P2 Evaluation", "P3": "P3 Prompt engineering", "P4": "P4 Builder / Fleet / Studio", "P5": "P5 Deployment / Agent Server",
         "P6": "P6 LLM Gateway", "P7": "P7 Admin / Auth / Sandboxes", "Khác": "Ngoài phạm vi (cloud/K8s/billing/JS)", "?": "Chưa phân loại"}
score = {D: 1, P_: 0.5, T: 0}
out = ["# Bản đồ tính năng LangSmith → Moza Agent OS (checklist đầy đủ, v2)", "",
       "Nguồn: https://docs.langchain.com/llms.txt (mục LangSmith, bỏ REST reference) — crawl & kiểm tra **19/10/2026**. Mỗi dòng = 1 trang docs công khai.",
       "Ký hiệu: ✅ xong & có test · 🟡 một phần · ⬜ chưa · ➖ ngoài phạm vi (không tính %). Viết lại từ docs công khai, không sao chép mã/tài sản độc quyền.", ""]
summ = ["| Phase | ✅ | 🟡 | ⬜ | ➖ | Hoàn thành (trong phạm vi) |", "|---|---|---|---|---|---|"]
tot_s = tot_n = 0
for ph in order:
    rs = [r for r in rows if r[0] == ph]
    if not rs: continue
    c = collections.Counter(r[4] for r in rs); n = len(rs) - c[NA]; s_ = sum(score[r[4]] for r in rs if r[4] != NA)
    tot_s += s_; tot_n += n
    summ.append(f"| {names[ph]} | {c[D]} | {c[P_]} | {c[T]} | {c[NA]} | {(100*s_/n if n else 0):.0f}% |")
summ.append(f"| **Tổng** | | | | | **{100*tot_s/tot_n:.0f}%** (còn lại {100-100*tot_s/tot_n:.0f}%) |")
out += ["## Tổng hợp", ""] + summ + [""]
for ph in order:
    rs = sorted([r for r in rows if r[0] == ph], key=lambda r: ({D: 0, P_: 1, T: 2, NA: 3}[r[4]], r[1]))
    if not rs: continue
    out += [f"## {names[ph]}", "", "| TT | Tính năng (trang docs) | Ghi chú | Kiểm tra |", "|---|---|---|---|"]
    out += [f"| {r[4]} | [{r[2]}]({r[3]}) | {r[5]} | 19/10/2026 |" for r in rs]
    out.append("")
open("/workspace/adk_ha_build/langsmith_feature_map.md", "w").write("\n".join(out))
print("\n".join(summ)); print(len(rows), "pages")
