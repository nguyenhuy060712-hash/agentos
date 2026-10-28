# Bản đồ tính năng LangSmith → Moza Agent OS (checklist đầy đủ, v2)

Nguồn: https://docs.langchain.com/llms.txt (mục LangSmith, bỏ REST reference) — crawl & kiểm tra **19/10/2026**. Mỗi dòng = 1 trang docs công khai.
Ký hiệu: ✅ xong & có test · 🟡 một phần · ⬜ chưa · ➖ ngoài phạm vi (không tính %). Viết lại từ docs công khai, không sao chép mã/tài sản độc quyền.

## Tổng hợp

| Phase | ✅ | 🟡 | ⬜ | ➖ | Hoàn thành (trong phạm vi) |
|---|---|---|---|---|---|
| P1 Observability | 44 | 12 | 0 | 31 | 89% |
| P2 Evaluation | 40 | 7 | 18 | 0 | 67% |
| P3 Prompt engineering | 10 | 7 | 2 | 0 | 71% |
| P4 Builder / Fleet / Studio | 16 | 7 | 3 | 7 | 75% |
| P5 Deployment / Agent Server | 23 | 7 | 10 | 0 | 66% |
| P6 LLM Gateway | 10 | 5 | 2 | 0 | 74% |
| P7 Admin / Auth / Sandboxes | 3 | 3 | 10 | 0 | 28% |
| Ngoài phạm vi (cloud/K8s/billing/JS) | 0 | 0 | 0 | 59 | 0% |
| **Tổng** | | | | | **71%** (còn lại 29%) |

## P1 Observability

| TT | Tính năng (trang docs) | Ghi chú | Kiểm tra |
|---|---|---|---|
| ✅ | [Access the current run (span) within a traced function](https://docs.langchain.com/langsmith/access-current-span) |  | 19/10/2026 |
| ✅ | [Add metadata and tags to traces](https://docs.langchain.com/langsmith/add-metadata-tags) |  | 19/10/2026 |
| ✅ | [Alerts in LangSmith](https://docs.langchain.com/langsmith/alerts) |  | 19/10/2026 |
| ✅ | [Custom instrumentation](https://docs.langchain.com/langsmith/annotate-code) |  | 19/10/2026 |
| ✅ | [Annotate traces and runs inline](https://docs.langchain.com/langsmith/annotate-traces-inline) |  | 19/10/2026 |
| ✅ | [Log user feedback using the SDK](https://docs.langchain.com/langsmith/attach-user-feedback) |  | 19/10/2026 |
| ✅ | [Conditional tracing](https://docs.langchain.com/langsmith/conditional-tracing) | SDK enabled/sampling | 19/10/2026 |
| ✅ | [Cost tracking](https://docs.langchain.com/langsmith/cost-tracking) |  | 19/10/2026 |
| ✅ | [Monitor projects with dashboards](https://docs.langchain.com/langsmith/dashboards) |  | 19/10/2026 |
| ✅ | [Query traces using the SDK](https://docs.langchain.com/langsmith/export-traces) |  | 19/10/2026 |
| ✅ | [Feedback data format](https://docs.langchain.com/langsmith/feedback-data-format) |  | 19/10/2026 |
| ✅ | [Filter traces](https://docs.langchain.com/langsmith/filter-traces) |  | 19/10/2026 |
| ✅ | [Filter traces (ClickHouse)](https://docs.langchain.com/langsmith/filter-traces-in-application) | DSL + saved filters | 19/10/2026 |
| ✅ | [Discover errors and usage patterns with Insights](https://docs.langchain.com/langsmith/insights) | gom cụm theo chữ ký + ngữ nghĩa (Gemini embedding, cosine ≥0.86) | 19/10/2026 |
| ✅ | [Log LLM calls](https://docs.langchain.com/langsmith/log-llm-trace) |  | 19/10/2026 |
| ✅ | [Log retriever traces](https://docs.langchain.com/langsmith/log-retriever-trace) |  | 19/10/2026 |
| ✅ | [Log traces to an agent](https://docs.langchain.com/langsmith/log-traces-to-agent) |  | 19/10/2026 |
| ✅ | [Log traces to a specific project](https://docs.langchain.com/langsmith/log-traces-to-project) |  | 19/10/2026 |
| ✅ | [Manage a trace](https://docs.langchain.com/langsmith/manage-trace) |  | 19/10/2026 |
| ✅ | [Prevent logging of sensitive data in traces](https://docs.langchain.com/langsmith/mask-inputs-outputs) |  | 19/10/2026 |
| ✅ | [Troubleshoot trace nesting](https://docs.langchain.com/langsmith/nest-traces) |  | 19/10/2026 |
| ✅ | [LangSmith Observability](https://docs.langchain.com/langsmith/observability) |  | 19/10/2026 |
| ✅ | [Observability concepts](https://docs.langchain.com/langsmith/observability-concepts) |  | 19/10/2026 |
| ✅ | [Trace an LLM application tutorial](https://docs.langchain.com/langsmith/observability-llm-tutorial) |  | 19/10/2026 |
| ✅ | [Tracing quickstart](https://docs.langchain.com/langsmith/observability-quickstart) |  | 19/10/2026 |
| ✅ | [Query threads using the SDK](https://docs.langchain.com/langsmith/query-threads) |  | 19/10/2026 |
| ✅ | [Redact secrets from traces](https://docs.langchain.com/langsmith/redact-secrets) | regex server-side, quy tắc sửa được trên UI | 19/10/2026 |
| ✅ | [Set up automation rules](https://docs.langchain.com/langsmith/rules) |  | 19/10/2026 |
| ✅ | [Run (span) data format](https://docs.langchain.com/langsmith/run-data-format) |  | 19/10/2026 |
| ✅ | [Runs](https://docs.langchain.com/langsmith/runs) |  | 19/10/2026 |
| ✅ | [Set a sampling rate for traces](https://docs.langchain.com/langsmith/sample-traces) |  | 19/10/2026 |
| ✅ | [Configure threads](https://docs.langchain.com/langsmith/threads) |  | 19/10/2026 |
| ✅ | [Trace Deep Agents applications](https://docs.langchain.com/langsmith/trace-deep-agents) | agent OS tự mirror trace | 19/10/2026 |
| ✅ | [Trace generator functions](https://docs.langchain.com/langsmith/trace-generator-functions) |  | 19/10/2026 |
| ✅ | [Trace query syntax](https://docs.langchain.com/langsmith/trace-query-syntax) |  | 19/10/2026 |
| ✅ | [Trace with API](https://docs.langchain.com/langsmith/trace-with-api) |  | 19/10/2026 |
| ✅ | [Trace Google Gemini applications](https://docs.langchain.com/langsmith/trace-with-google-gemini) | SDK wrap_genai | 19/10/2026 |
| ✅ | [Trace LangChain applications (Python and JS/TS)](https://docs.langchain.com/langsmith/trace-with-langchain) | qua agent OS (spans middleware); không dùng callback LangSmith | 19/10/2026 |
| ✅ | [Trace LangGraph applications](https://docs.langchain.com/langsmith/trace-with-langgraph) |  | 19/10/2026 |
| ✅ | [Trace with OpenTelemetry](https://docs.langchain.com/langsmith/trace-with-opentelemetry) | OTLP/HTTP JSON + protobuf (+gzip) | 19/10/2026 |
| ✅ | [Trace without setting environment variables](https://docs.langchain.com/langsmith/trace-without-env-vars) |  | 19/10/2026 |
| ✅ | [Upload files with traces](https://docs.langchain.com/langsmith/upload-files-with-traces) | chưa có attachments | 19/10/2026 |
| ✅ | [Use threads](https://docs.langchain.com/langsmith/use-threads) |  | 19/10/2026 |
| ✅ | [View traces](https://docs.langchain.com/langsmith/view-traces) |  | 19/10/2026 |
| 🟡 | [Configure run input and output preview](https://docs.langchain.com/langsmith/configure-input-output-preview) | preview cố định 300 ký tự | 19/10/2026 |
| 🟡 | [Custom output rendering](https://docs.langchain.com/langsmith/custom-output-rendering) |  | 19/10/2026 |
| 🟡 | [Bulk export trace data](https://docs.langchain.com/langsmith/data-export) | chỉ API JSON, chưa xuất parquet | 19/10/2026 |
| 🟡 | [Monitor and troubleshoot bulk exports](https://docs.langchain.com/langsmith/data-export-monitor) |  | 19/10/2026 |
| 🟡 | [Implement distributed tracing](https://docs.langchain.com/langsmith/distributed-tracing) | parent_run_id qua API, chưa có header propagation | 19/10/2026 |
| 🟡 | [Log multimodal traces](https://docs.langchain.com/langsmith/log-multimodal-traces) | attachments xem/tải; chưa render ảnh/audio inline | 19/10/2026 |
| 🟡 | [Metadata parameters reference](https://docs.langchain.com/langsmith/ls-metadata-parameters) |  | 19/10/2026 |
| 🟡 | [Collect feedback with presigned URLs](https://docs.langchain.com/langsmith/presigned-feedback-tokens) | có share link ký HMAC; chưa có token feedback riêng | 19/10/2026 |
| 🟡 | [Set up feedback criteria](https://docs.langchain.com/langsmith/set-up-feedback-criteria) | rubric ở annotation queue | 19/10/2026 |
| 🟡 | [Trace Google ADK applications](https://docs.langchain.com/langsmith/trace-with-google-adk) | dùng SDK @traceable thủ công | 19/10/2026 |
| 🟡 | [Trace OpenAI-compatible providers](https://docs.langchain.com/langsmith/trace-with-openai-compatible) |  | 19/10/2026 |
| 🟡 | [Configure webhook notifications for rules](https://docs.langchain.com/langsmith/webhooks) | rule webhook: nội bộ tự gửi, bên ngoài = bản nháp | 19/10/2026 |
| ➖ | [Trace Anthropic applications](https://docs.langchain.com/langsmith/trace-anthropic) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Amazon Bedrock applications](https://docs.langchain.com/langsmith/trace-bedrock) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Claude Agent SDK applications](https://docs.langchain.com/langsmith/trace-claude-agent-sdk) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Claude Code applications](https://docs.langchain.com/langsmith/trace-claude-code) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace DeepSeek applications](https://docs.langchain.com/langsmith/trace-deepseek) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Gemini Live applications](https://docs.langchain.com/langsmith/trace-gemini-live) | voice/realtime — ngoài phạm vi | 19/10/2026 |
| ➖ | [Trace LiteLLM applications](https://docs.langchain.com/langsmith/trace-litellm) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace OpenAI applications](https://docs.langchain.com/langsmith/trace-openai) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace OpenAI Realtime applications](https://docs.langchain.com/langsmith/trace-openai-realtime) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Voice tracing fundamentals](https://docs.langchain.com/langsmith/trace-voice-fundamentals) | voice/realtime — ngoài phạm vi | 19/10/2026 |
| ➖ | [Trace AutoGen applications](https://docs.langchain.com/langsmith/trace-with-autogen) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Claude Managed Agents](https://docs.langchain.com/langsmith/trace-with-claude-managed-agents) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace OpenAI Codex sessions](https://docs.langchain.com/langsmith/trace-with-codex) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace CrewAI applications](https://docs.langchain.com/langsmith/trace-with-crewai) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Cursor sessions](https://docs.langchain.com/langsmith/trace-with-cursor) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Instructor applications](https://docs.langchain.com/langsmith/trace-with-instructor) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace LiveKit applications](https://docs.langchain.com/langsmith/trace-with-livekit) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Mastra applications](https://docs.langchain.com/langsmith/trace-with-mastra) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Microsoft Agent Framework applications](https://docs.langchain.com/langsmith/trace-with-microsoft-agent-framework) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Mistral applications](https://docs.langchain.com/langsmith/trace-with-mistral) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace n8n workflows](https://docs.langchain.com/langsmith/trace-with-n8n) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace OpenAI Agents SDK applications](https://docs.langchain.com/langsmith/trace-with-openai-agents-sdk) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace OpenCode sessions](https://docs.langchain.com/langsmith/trace-with-opencode) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Pi coding agent sessions](https://docs.langchain.com/langsmith/trace-with-pi) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Pipecat applications](https://docs.langchain.com/langsmith/trace-with-pipecat) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace PydanticAI applications](https://docs.langchain.com/langsmith/trace-with-pydantic-ai) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Semantic Kernel applications](https://docs.langchain.com/langsmith/trace-with-semantic-kernel) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Strands Agents applications](https://docs.langchain.com/langsmith/trace-with-strands-agents) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace with Temporal](https://docs.langchain.com/langsmith/trace-with-temporal) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Vercel AI SDK applications (JS/TS only)](https://docs.langchain.com/langsmith/trace-with-vercel-ai-sdk) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |
| ➖ | [Trace Visual Studio Code Copilot Chat sessions](https://docs.langchain.com/langsmith/trace-with-vscode-copilot) | tích hợp framework/nhà cung cấp khác — ngoài phạm vi (chỉ Gemini) | 19/10/2026 |

## P2 Evaluation

| TT | Tính năng (trang docs) | Ghi chú | Kiểm tra |
|---|---|---|---|
| ✅ | [Analyze an experiment](https://docs.langchain.com/langsmith/analyze-an-experiment) |  | 19/10/2026 |
| ✅ | [Use annotation queues](https://docs.langchain.com/langsmith/annotation-queues) |  | 19/10/2026 |
| ✅ | [Manage feedback & annotation queues programmatically](https://docs.langchain.com/langsmith/annotation-queues-sdk) |  | 19/10/2026 |
| ✅ | [How to define a code evaluator](https://docs.langchain.com/langsmith/code-evaluator-sdk) |  | 19/10/2026 |
| ✅ | [How to define a code evaluator](https://docs.langchain.com/langsmith/code-evaluator-ui) |  | 19/10/2026 |
| ✅ | [How to compare experiment results](https://docs.langchain.com/langsmith/compare-experiment-results) |  | 19/10/2026 |
| ✅ | [How to improve your evaluator with few-shot examples](https://docs.langchain.com/langsmith/create-few-shot-evaluators) |  | 19/10/2026 |
| ✅ | [How to define a target function to evaluate](https://docs.langchain.com/langsmith/define-target-function) |  | 19/10/2026 |
| ✅ | [Evaluate a chatbot](https://docs.langchain.com/langsmith/evaluate-chatbot-tutorial) |  | 19/10/2026 |
| ✅ | [How to add evaluators to an existing experiment (Python only)](https://docs.langchain.com/langsmith/evaluate-existing-experiment) |  | 19/10/2026 |
| ✅ | [How to evaluate agents](https://docs.langchain.com/langsmith/evaluate-llm-application) |  | 19/10/2026 |
| ✅ | [How to evaluate an application's intermediate steps](https://docs.langchain.com/langsmith/evaluate-on-intermediate-steps) |  | 19/10/2026 |
| ✅ | [How to run a pairwise evaluation](https://docs.langchain.com/langsmith/evaluate-pairwise) |  | 19/10/2026 |
| ✅ | [Run an evaluation with multimodal content](https://docs.langchain.com/langsmith/evaluate-with-attachments) | file đính kèm example → target (text) + biến {{attachments}} cho evaluator | 19/10/2026 |
| ✅ | [LangSmith Evaluation](https://docs.langchain.com/langsmith/evaluation) |  | 19/10/2026 |
| ✅ | [How to run an evaluation asynchronously](https://docs.langchain.com/langsmith/evaluation-async) |  | 19/10/2026 |
| ✅ | [Evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts) |  | 19/10/2026 |
| ✅ | [Evaluation quickstart](https://docs.langchain.com/langsmith/evaluation-quickstart) |  | 19/10/2026 |
| ✅ | [Evaluation types](https://docs.langchain.com/langsmith/evaluation-types) |  | 19/10/2026 |
| ✅ | [Manage evaluators](https://docs.langchain.com/langsmith/evaluators) |  | 19/10/2026 |
| ✅ | [Example data format](https://docs.langchain.com/langsmith/example-data-format) |  | 19/10/2026 |
| ✅ | [Experiment configuration](https://docs.langchain.com/langsmith/experiment-configuration) |  | 19/10/2026 |
| ✅ | [How to fetch performance metrics for an experiment](https://docs.langchain.com/langsmith/fetch-perf-metrics-experiment) |  | 19/10/2026 |
| ✅ | [How to define an LLM-as-a-judge evaluator](https://docs.langchain.com/langsmith/llm-as-judge) |  | 19/10/2026 |
| ✅ | [How to define an LLM-as-a-judge evaluator](https://docs.langchain.com/langsmith/llm-as-judge-sdk) |  | 19/10/2026 |
| ✅ | [Manage datasets](https://docs.langchain.com/langsmith/manage-datasets) |  | 19/10/2026 |
| ✅ | [Create and manage datasets in the UI](https://docs.langchain.com/langsmith/manage-datasets-in-application) |  | 19/10/2026 |
| ✅ | [How to create and manage datasets programmatically](https://docs.langchain.com/langsmith/manage-datasets-programmatically) |  | 19/10/2026 |
| ✅ | [Manage evaluators with the SDK](https://docs.langchain.com/langsmith/manage-evaluators-sdk) |  | 19/10/2026 |
| ✅ | [How to return categorical vs numerical metrics](https://docs.langchain.com/langsmith/metric-type) |  | 19/10/2026 |
| ✅ | [How to simulate multi-turn interactions](https://docs.langchain.com/langsmith/multi-turn-simulation) |  | 19/10/2026 |
| ✅ | [How to return multiple scores in one evaluator](https://docs.langchain.com/langsmith/multiple-scores) |  | 19/10/2026 |
| ✅ | [Set up online code evaluators](https://docs.langchain.com/langsmith/online-evaluations-code) |  | 19/10/2026 |
| ✅ | [Set up LLM-as-a-judge online evaluators](https://docs.langchain.com/langsmith/online-evaluations-llm-as-judge) |  | 19/10/2026 |
| ✅ | [Set up multi-turn online evaluators](https://docs.langchain.com/langsmith/online-evaluations-multi-turn) |  | 19/10/2026 |
| ✅ | [How to evaluate with repetitions](https://docs.langchain.com/langsmith/repetition) |  | 19/10/2026 |
| ✅ | [How to use the REST API](https://docs.langchain.com/langsmith/run-evals-api-only) |  | 19/10/2026 |
| ✅ | [How to define a summary evaluator](https://docs.langchain.com/langsmith/summary) |  | 19/10/2026 |
| ✅ | [How to evaluate your agent with trajectory evaluations](https://docs.langchain.com/langsmith/trajectory-evals) |  | 19/10/2026 |
| ✅ | [How to upload experiments run outside of LangSmith with the REST API](https://docs.langchain.com/langsmith/upload-existing-experiments) |  | 19/10/2026 |
| 🟡 | [Implement a CI/CD pipeline using LangSmith Deployment and Evaluation](https://docs.langchain.com/langsmith/cicd-pipeline-example) | plugin pytest → experiment; chưa mẫu CI | 19/10/2026 |
| 🟡 | [Evaluate a complex agent](https://docs.langchain.com/langsmith/evaluate-complex-agent) | trajectory + LLM judge; chưa đánh giá từng node | 19/10/2026 |
| 🟡 | [How to evaluate with OpenTelemetry](https://docs.langchain.com/langsmith/evaluate-with-opentelemetry) | trace OTLP chấm được bằng rule/evaluator | 19/10/2026 |
| 🟡 | [Track and limit evaluator spend](https://docs.langchain.com/langsmith/evaluator-spend) | judge đi qua gateway có ngân sách/ngày chung | 19/10/2026 |
| 🟡 | [How to filter experiments in the UI](https://docs.langchain.com/langsmith/filter-experiments-ui) | lọc theo dataset; chưa lọc theo metadata/điểm | 19/10/2026 |
| 🟡 | [How to run an evaluation locally (Python only)](https://docs.langchain.com/langsmith/local) | pytest plugin ghi lên server; chưa chế độ offline | 19/10/2026 |
| 🟡 | [Test multi-turn conversations](https://docs.langchain.com/langsmith/multiple-messages) | thread evaluator; dataset nhiều message thủ công | 19/10/2026 |
| ⬜ | [Use assertions](https://docs.langchain.com/langsmith/assertions) |  | 19/10/2026 |
| ⬜ | [How to audit evaluator scores](https://docs.langchain.com/langsmith/audit-evaluator-scores) |  | 19/10/2026 |
| ⬜ | [Automatically run evaluators on experiments](https://docs.langchain.com/langsmith/bind-evaluator-to-dataset) |  | 19/10/2026 |
| ⬜ | [How to create a composite evaluator](https://docs.langchain.com/langsmith/composite-evaluators-sdk) |  | 19/10/2026 |
| ⬜ | [How to create a composite evaluator](https://docs.langchain.com/langsmith/composite-evaluators-ui) |  | 19/10/2026 |
| ⬜ | [Dataset prebuilt JSON schema types](https://docs.langchain.com/langsmith/dataset-json-types) |  | 19/10/2026 |
| ⬜ | [Dataset transformations](https://docs.langchain.com/langsmith/dataset-transformations) |  | 19/10/2026 |
| ⬜ | [How to define a decision model evaluator](https://docs.langchain.com/langsmith/decision-model-evaluator) |  | 19/10/2026 |
| ⬜ | [How to evaluate a graph](https://docs.langchain.com/langsmith/evaluate-graph) |  | 19/10/2026 |
| ⬜ | [Evaluate a RAG application](https://docs.langchain.com/langsmith/evaluate-rag-tutorial) |  | 19/10/2026 |
| ⬜ | [How to retry failed runs in experiments (Python only)](https://docs.langchain.com/langsmith/evaluate-with-retry) |  | 19/10/2026 |
| ⬜ | [Application-specific evaluation approaches](https://docs.langchain.com/langsmith/evaluation-approaches) |  | 19/10/2026 |
| ⬜ | [Improve LLM-as-judge evaluators using human feedback](https://docs.langchain.com/langsmith/improve-judge-evaluator-feedback) |  | 19/10/2026 |
| ⬜ | [Set up composite online evaluators](https://docs.langchain.com/langsmith/online-evaluations-composite) |  | 19/10/2026 |
| ⬜ | [Set up decision model online evaluators](https://docs.langchain.com/langsmith/online-evaluations-decision-models) |  | 19/10/2026 |
| ⬜ | [Optimize a classifier](https://docs.langchain.com/langsmith/optimize-classifier) |  | 19/10/2026 |
| ⬜ | [How to read experiment results locally](https://docs.langchain.com/langsmith/read-local-experiment-results) |  | 19/10/2026 |
| ⬜ | [Run backtests on a new version of an agent](https://docs.langchain.com/langsmith/run-backtests-new-agent) |  | 19/10/2026 |

## P3 Prompt engineering

| TT | Tính năng (trang docs) | Ghi chú | Kiểm tra |
|---|---|---|---|
| ✅ | [Configure Context Hub commit webhooks](https://docs.langchain.com/langsmith/context-hub-webhooks) |  | 19/10/2026 |
| ✅ | [Create a prompt](https://docs.langchain.com/langsmith/create-a-prompt) |  | 19/10/2026 |
| ✅ | [Manage prompts](https://docs.langchain.com/langsmith/manage-prompts) |  | 19/10/2026 |
| ✅ | [Manage prompts programmatically](https://docs.langchain.com/langsmith/manage-prompts-programmatically) |  | 19/10/2026 |
| ✅ | [How to sync prompts with GitHub](https://docs.langchain.com/langsmith/prompt-commit) |  | 19/10/2026 |
| ✅ | [Prompt engineering concepts](https://docs.langchain.com/langsmith/prompt-engineering-concepts) |  | 19/10/2026 |
| ✅ | [Prompt engineering quickstart](https://docs.langchain.com/langsmith/prompt-engineering-quickstart) |  | 19/10/2026 |
| ✅ | [Run an evaluation from the Playground](https://docs.langchain.com/langsmith/run-evaluation-from-playground) |  | 19/10/2026 |
| ✅ | [Test from the Playground](https://docs.langchain.com/langsmith/test-from-playground) |  | 19/10/2026 |
| ✅ | [Write your prompt with AI](https://docs.langchain.com/langsmith/write-prompt-with-ai) |  | 19/10/2026 |
| 🟡 | [Context engineering concepts](https://docs.langchain.com/langsmith/context-engineering-concepts) |  | 19/10/2026 |
| 🟡 | [Manage contexts with the SDK](https://docs.langchain.com/langsmith/manage-contexts-sdk) |  | 19/10/2026 |
| 🟡 | [Configure prompt settings](https://docs.langchain.com/langsmith/managing-model-configurations) | model+nhiệt độ lưu theo commit; chưa preset dùng chung | 19/10/2026 |
| 🟡 | [Prompt & Context Hub](https://docs.langchain.com/langsmith/prompt-context-hub) |  | 19/10/2026 |
| 🟡 | [Prompt template format guide](https://docs.langchain.com/langsmith/prompt-template-format) | chỉ {{biến}} (mustache cơ bản), chưa f-string/jinja | 19/10/2026 |
| 🟡 | [Use the Context Hub](https://docs.langchain.com/langsmith/use-the-context-hub) | hub prompt + tag; chưa context file/thư mục | 19/10/2026 |
| 🟡 | [Use tools in a prompt](https://docs.langchain.com/langsmith/use-tools) | output_schema JSON; chưa định nghĩa tool trong playground | 19/10/2026 |
| ⬜ | [Include multimodal content in a prompt](https://docs.langchain.com/langsmith/multimodal-content) | chưa | 19/10/2026 |
| ⬜ | [Troubleshoot variable caching](https://docs.langchain.com/langsmith/troubleshooting-variable-caching) |  | 19/10/2026 |

## P4 Builder / Fleet / Studio

| TT | Tính năng (trang docs) | Ghi chú | Kiểm tra |
|---|---|---|---|
| ✅ | [Human-in-the-loop using server API](https://docs.langchain.com/langsmith/add-human-in-the-loop) |  | 19/10/2026 |
| ✅ | [Access & oversight](https://docs.langchain.com/langsmith/fleet/access-and-oversight) | RBAC + HITL + chặn khoá/báo động/garage server-side | 19/10/2026 |
| ✅ | [Essentials](https://docs.langchain.com/langsmith/fleet/essentials) |  | 19/10/2026 |
| ✅ | [No-code agents with LangSmith Fleet](https://docs.langchain.com/langsmith/fleet/index) |  | 19/10/2026 |
| ✅ | [Manage agent settings](https://docs.langchain.com/langsmith/fleet/manage-agent-settings) |  | 19/10/2026 |
| ✅ | [Quickstart](https://docs.langchain.com/langsmith/fleet/quickstart) |  | 19/10/2026 |
| ✅ | [Remote MCP servers](https://docs.langchain.com/langsmith/fleet/remote-mcp-servers) |  | 19/10/2026 |
| ✅ | [Schedules](https://docs.langchain.com/langsmith/fleet/schedules) |  | 19/10/2026 |
| ✅ | [Templates](https://docs.langchain.com/langsmith/fleet/templates) |  | 19/10/2026 |
| ✅ | [Tool integrations](https://docs.langchain.com/langsmith/fleet/tools) |  | 19/10/2026 |
| ✅ | [Fleet webhooks](https://docs.langchain.com/langsmith/fleet/webhooks) |  | 19/10/2026 |
| ✅ | [Manage workspace administration](https://docs.langchain.com/langsmith/fleet/workspace-admin) | workspace + thành viên + vai trò | 19/10/2026 |
| ✅ | [Time travel using the server API](https://docs.langchain.com/langsmith/human-in-the-loop-time-travel) |  | 19/10/2026 |
| ✅ | [Get started with Studio](https://docs.langchain.com/langsmith/quick-start-studio) |  | 19/10/2026 |
| ✅ | [LangSmith Studio](https://docs.langchain.com/langsmith/studio) |  | 19/10/2026 |
| ✅ | [How to use Studio](https://docs.langchain.com/langsmith/use-studio) |  | 19/10/2026 |
| 🟡 | [Channels](https://docs.langchain.com/langsmith/fleet/channels) | webhook vào /d/hook; Slack/Teams cần duyệt riêng | 19/10/2026 |
| 🟡 | [Use Fleet agents in code](https://docs.langchain.com/langsmith/fleet/code) | sandbox chay_code_python | 19/10/2026 |
| 🟡 | [LangSmith Tool Server](https://docs.langchain.com/langsmith/fleet/mcp-framework) | MCP HA chỉ-đọc mặc định | 19/10/2026 |
| 🟡 | [Skills](https://docs.langchain.com/langsmith/fleet/skills) | template + system prompt; chưa skill files | 19/10/2026 |
| 🟡 | [Rebuild graph at runtime](https://docs.langchain.com/langsmith/graph-rebuild) | cache agent xoá khi commit hub/sửa agent | 19/10/2026 |
| 🟡 | [Observability in Studio](https://docs.langchain.com/langsmith/observability-studio) | trace agent link từ studio qua project agents | 19/10/2026 |
| 🟡 | [Studio troubleshooting](https://docs.langchain.com/langsmith/troubleshooting-studio) |  | 19/10/2026 |
| ⬜ | [Custom Apps](https://docs.langchain.com/langsmith/custom-apps) |  | 19/10/2026 |
| ⬜ | [Agent identity](https://docs.langchain.com/langsmith/fleet/agent-identity) |  | 19/10/2026 |
| ⬜ | [Auth-aware tool responses](https://docs.langchain.com/langsmith/fleet/auth-format) |  | 19/10/2026 |
| ➖ | [Arcade integration](https://docs.langchain.com/langsmith/fleet/arcade) | tích hợp SaaS bên thứ ba — cần duyệt riêng | 19/10/2026 |
| ➖ | [LangSmith Fleet changelog](https://docs.langchain.com/langsmith/fleet/changelog) | tích hợp SaaS bên thứ ba — cần duyệt riêng | 19/10/2026 |
| ➖ | [Agent platform comparison](https://docs.langchain.com/langsmith/fleet/comparison) | tích hợp SaaS bên thứ ba — cần duyệt riêng | 19/10/2026 |
| ➖ | [Computer use](https://docs.langchain.com/langsmith/fleet/computer-use) | tích hợp SaaS bên thứ ba — cần duyệt riêng | 19/10/2026 |
| ➖ | [Salesforce integration](https://docs.langchain.com/langsmith/fleet/salesforce) | tích hợp SaaS bên thứ ba — cần duyệt riêng | 19/10/2026 |
| ➖ | [Integrate Slack with an agent](https://docs.langchain.com/langsmith/fleet/slack-app) | tích hợp SaaS bên thứ ba — cần duyệt riêng | 19/10/2026 |
| ➖ | [Integrate Teams with an agent](https://docs.langchain.com/langsmith/fleet/teams-app) | tích hợp SaaS bên thứ ba — cần duyệt riêng | 19/10/2026 |

## P5 Deployment / Agent Server

| TT | Tính năng (trang docs) | Ghi chú | Kiểm tra |
|---|---|---|---|
| ✅ | [Agent Server](https://docs.langchain.com/langsmith/agent-server) |  | 19/10/2026 |
| ✅ | [Agent Server](https://docs.langchain.com/langsmith/agent-server-overview) |  | 19/10/2026 |
| ✅ | [Assistants](https://docs.langchain.com/langsmith/assistants) |  | 19/10/2026 |
| ✅ | [How to kick off background runs](https://docs.langchain.com/langsmith/background-run) |  | 19/10/2026 |
| ✅ | [How to cancel a run](https://docs.langchain.com/langsmith/cancel-run) |  | 19/10/2026 |
| ✅ | [Manage assistants](https://docs.langchain.com/langsmith/configuration-cloud) |  | 19/10/2026 |
| ✅ | [Configure checkpointer backend](https://docs.langchain.com/langsmith/configure-checkpointer) |  | 19/10/2026 |
| ✅ | [How to add TTLs to your application](https://docs.langchain.com/langsmith/configure-ttl) |  | 19/10/2026 |
| ✅ | [Use cron jobs](https://docs.langchain.com/langsmith/cron-jobs) |  | 19/10/2026 |
| ✅ | [How to add custom lifespan events](https://docs.langchain.com/langsmith/custom-lifespan) |  | 19/10/2026 |
| ✅ | [How to add custom middleware](https://docs.langchain.com/langsmith/custom-middleware) |  | 19/10/2026 |
| ✅ | [How to add custom routes](https://docs.langchain.com/langsmith/custom-routes) |  | 19/10/2026 |
| ✅ | [How to use a custom store](https://docs.langchain.com/langsmith/custom-store) |  | 19/10/2026 |
| ✅ | [Revisions](https://docs.langchain.com/langsmith/deployment-revisions) |  | 19/10/2026 |
| ✅ | [Double texting](https://docs.langchain.com/langsmith/double-texting) |  | 19/10/2026 |
| ✅ | [Enqueue concurrent](https://docs.langchain.com/langsmith/enqueue-concurrent) |  | 19/10/2026 |
| ✅ | [How to handle model rate limits](https://docs.langchain.com/langsmith/handle-model-rate-limiting) |  | 19/10/2026 |
| ✅ | [Interrupt concurrent](https://docs.langchain.com/langsmith/interrupt-concurrent) |  | 19/10/2026 |
| ✅ | [Reject Concurrent](https://docs.langchain.com/langsmith/reject-concurrent) |  | 19/10/2026 |
| ✅ | [Rollback Concurrent](https://docs.langchain.com/langsmith/rollback-concurrent) |  | 19/10/2026 |
| ✅ | [How to add semantic search to your agent deployment](https://docs.langchain.com/langsmith/semantic-search) |  | 19/10/2026 |
| ✅ | [Stateless runs](https://docs.langchain.com/langsmith/stateless-runs) |  | 19/10/2026 |
| ✅ | [Use webhooks](https://docs.langchain.com/langsmith/use-webhooks) |  | 19/10/2026 |
| 🟡 | [How to collect user feedback for Agent Server runs](https://docs.langchain.com/langsmith/agent-server-feedback) | run có trace_id → feedback API | 19/10/2026 |
| 🟡 | [Configure Agent Server for scale](https://docs.langchain.com/langsmith/agent-server-scale) | hàng đợi Postgres SKIP LOCKED, 2 worker/tiến trình; chưa nhiều replica | 19/10/2026 |
| 🟡 | [Manage a deployment](https://docs.langchain.com/langsmith/manage-deployment) | assistants có phiên bản + set latest; deploy = tiến trình đơn | 19/10/2026 |
| 🟡 | [Monitor a deployment](https://docs.langchain.com/langsmith/monitor-deployment) | /api/v1/info (hàng đợi, worker) + trace mỗi run | 19/10/2026 |
| 🟡 | [How to run multiple assistants on the same thread](https://docs.langchain.com/langsmith/same-thread) | nhiều assistant cùng graph trên 1 thread; khác graph → 409 | 19/10/2026 |
| 🟡 | [Streaming API](https://docs.langchain.com/langsmith/streaming) | SSE metadata/updates/values/end + join/Last-Event-ID; chưa stream token (messages) | 19/10/2026 |
| 🟡 | [How to interact with a deployment using RemoteGraph](https://docs.langchain.com/langsmith/use-remote-graph) | langgraph-sdk client chạy được với /api/v1 (đã test); RemoteGraph chưa test | 19/10/2026 |
| ⬜ | [Distributed tracing with Agent Server](https://docs.langchain.com/langsmith/agent-server-distributed-tracing) |  | 19/10/2026 |
| ⬜ | [Application structure](https://docs.langchain.com/langsmith/application-structure) |  | 19/10/2026 |
| ⬜ | [Use server-side caching](https://docs.langchain.com/langsmith/caching) |  | 19/10/2026 |
| ⬜ | [Use HTTP headers for runtime configuration](https://docs.langchain.com/langsmith/configurable-headers) |  | 19/10/2026 |
| ⬜ | [Include HTTP headers in server logs](https://docs.langchain.com/langsmith/configurable-logs) |  | 19/10/2026 |
| ⬜ | [How to use a custom checkpointer](https://docs.langchain.com/langsmith/custom-checkpointer) |  | 19/10/2026 |
| ⬜ | [Integrations](https://docs.langchain.com/langsmith/integrations) |  | 19/10/2026 |
| ⬜ | [Document API authentication in OpenAPI](https://docs.langchain.com/langsmith/openapi-security) |  | 19/10/2026 |
| ⬜ | [A2A endpoint in Agent Server](https://docs.langchain.com/langsmith/server-a2a) |  | 19/10/2026 |
| ⬜ | [MCP endpoint in Agent Server](https://docs.langchain.com/langsmith/server-mcp) |  | 19/10/2026 |

## P6 LLM Gateway

| TT | Tính năng (trang docs) | Ghi chú | Kiểm tra |
|---|---|---|---|
| ✅ | [LLM Gateway](https://docs.langchain.com/langsmith/llm-gateway) |  | 19/10/2026 |
| ✅ | [Traces and access control](https://docs.langchain.com/langsmith/llm-gateway-access) |  | 19/10/2026 |
| ✅ | [Admin setup](https://docs.langchain.com/langsmith/llm-gateway-admin-setup) |  | 19/10/2026 |
| ✅ | [Model fallbacks](https://docs.langchain.com/langsmith/llm-gateway-fallbacks) |  | 19/10/2026 |
| ✅ | [How the LLM Gateway works](https://docs.langchain.com/langsmith/llm-gateway-how-it-works) |  | 19/10/2026 |
| ✅ | [Model access policies](https://docs.langchain.com/langsmith/llm-gateway-model-access-policies) |  | 19/10/2026 |
| ✅ | [Monitor LLM Gateway spend](https://docs.langchain.com/langsmith/llm-gateway-monitoring) |  | 19/10/2026 |
| ✅ | [LLM Gateway quickstart](https://docs.langchain.com/langsmith/llm-gateway-quickstart) |  | 19/10/2026 |
| ✅ | [Rate limit policies](https://docs.langchain.com/langsmith/llm-gateway-rate-limit-policies) |  | 19/10/2026 |
| ✅ | [Spend policies](https://docs.langchain.com/langsmith/llm-gateway-spend-policies) |  | 19/10/2026 |
| 🟡 | [API formats](https://docs.langchain.com/langsmith/llm-gateway-api-formats) | OpenAI chat/embeddings/models (stream, tools, JSON, ảnh base64); chưa định dạng Anthropic/Responses | 19/10/2026 |
| 🟡 | [Data policy](https://docs.langchain.com/langsmith/llm-gateway-data-policy) | PII mask/block (email, thẻ Luhn, SĐT VN, CCCD, IP) vào+ra; trace đã mask | 19/10/2026 |
| 🟡 | [Direct model access](https://docs.langchain.com/langsmith/llm-gateway-direct-model-access) | gọi trực tiếp model Gemini qua virtual key | 19/10/2026 |
| 🟡 | [Custom model configurations](https://docs.langchain.com/langsmith/llm-gateway-model-configurations) | fallback + allow-list theo key; chưa alias model tuỳ biến | 19/10/2026 |
| 🟡 | [Provider authentication](https://docs.langchain.com/langsmith/llm-gateway-provider-authentication) | 1 key Gemini, chỉ admin đổi; chỉ Gemini | 19/10/2026 |
| ⬜ | [Decision models](https://docs.langchain.com/langsmith/llm-gateway-decision-models) |  | 19/10/2026 |
| ⬜ | [Per-customer policies](https://docs.langchain.com/langsmith/llm-gateway-header-policies) |  | 19/10/2026 |

## P7 Admin / Auth / Sandboxes

| TT | Tính năng (trang docs) | Ghi chú | Kiểm tra |
|---|---|---|---|
| ✅ | [Authentication & access control](https://docs.langchain.com/langsmith/auth) | đăng nhập nhiều user (scrypt), phiên cookie, API key, Basic tương thích; RBAC admin/editor/viewer theo workspace; audit log | 19/10/2026 |
| ✅ | [Sandbox access permissions](https://docs.langchain.com/langsmith/sandbox-permissions) |  | 19/10/2026 |
| ✅ | [LangSmith Sandboxes](https://docs.langchain.com/langsmith/sandboxes) |  | 19/10/2026 |
| 🟡 | [Make conversations private](https://docs.langchain.com/langsmith/resource-auth) | tách theo workspace; chưa riêng tư theo từng user | 19/10/2026 |
| 🟡 | [Sandbox download links](https://docs.langchain.com/langsmith/sandbox-download-links) | tải file qua API có xác thực; chưa link ký có hạn | 19/10/2026 |
| 🟡 | [Sandbox SDK usage](https://docs.langchain.com/langsmith/sandbox-sdk) | REST (exec/files); chưa SDK riêng | 19/10/2026 |
| ⬜ | [Connect an authentication provider](https://docs.langchain.com/langsmith/add-auth-server) |  | 19/10/2026 |
| ⬜ | [Set up Agent Auth](https://docs.langchain.com/langsmith/agent-auth) |  | 19/10/2026 |
| ⬜ | [Add custom authentication](https://docs.langchain.com/langsmith/custom-auth) |  | 19/10/2026 |
| ⬜ | [Sandbox auth proxy](https://docs.langchain.com/langsmith/sandbox-auth-proxy) |  | 19/10/2026 |
| ⬜ | [Sandbox CLI](https://docs.langchain.com/langsmith/sandbox-cli) |  | 19/10/2026 |
| ⬜ | [Sandbox mounts](https://docs.langchain.com/langsmith/sandbox-mounts) |  | 19/10/2026 |
| ⬜ | [Sandbox service URLs](https://docs.langchain.com/langsmith/sandbox-service-urls) |  | 19/10/2026 |
| ⬜ | [Sandbox snapshots](https://docs.langchain.com/langsmith/sandbox-snapshots) |  | 19/10/2026 |
| ⬜ | [Set up custom authentication](https://docs.langchain.com/langsmith/set-up-custom-auth) |  | 19/10/2026 |
| ⬜ | [Isolate store data per user](https://docs.langchain.com/langsmith/store-auth) |  | 19/10/2026 |

## Ngoài phạm vi (cloud/K8s/billing/JS)

| TT | Tính năng (trang docs) | Ghi chú | Kiểm tra |
|---|---|---|---|
| ➖ | [Configure AES encryption at rest](https://docs.langchain.com/langsmith/aes-encryption) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Export trace data to BigQuery](https://docs.langchain.com/langsmith/big-query-bulk-export) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Cloud platform features](https://docs.langchain.com/langsmith/cloud-platform-features) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Coding agent metadata contract](https://docs.langchain.com/langsmith/coding-agent-metadata-contract) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Configure IAM authentication for data stores](https://docs.langchain.com/langsmith/configure-iam-auth) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [How to customize the Dockerfile](https://docs.langchain.com/langsmith/custom-docker) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Configure custom encryption at rest](https://docs.langchain.com/langsmith/custom-encryption) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Connect to a custom model](https://docs.langchain.com/langsmith/custom-endpoint) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Connect to an OpenAI compliant model provider/proxy](https://docs.langchain.com/langsmith/custom-openai-compliant-model) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Manage bulk export destinations](https://docs.langchain.com/langsmith/data-export-destinations) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Import exported data](https://docs.langchain.com/langsmith/data-export-downstream) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Self-host standalone servers](https://docs.langchain.com/langsmith/deploy-standalone-server) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Create a deployment](https://docs.langchain.com/langsmith/deploy-to-cloud) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Deploy to Cloud](https://docs.langchain.com/langsmith/deploy-to-cloud-overview) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Deploy to self-hosted](https://docs.langchain.com/langsmith/deploy-to-self-hosted-overview) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Deploy with control plane](https://docs.langchain.com/langsmith/deploy-with-control-plane) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Troubleshooting for self-hosted deployments](https://docs.langchain.com/langsmith/diagnostics-self-hosted) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Encryption at rest](https://docs.langchain.com/langsmith/encryption) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [LangSmith Engine](https://docs.langchain.com/langsmith/engine) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Validate fixes automatically](https://docs.langchain.com/langsmith/engine-fix-validation) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Find and fix your agent's issues with LangSmith Engine](https://docs.langchain.com/langsmith/engine-issues) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [LangSmith Engine notifications](https://docs.langchain.com/langsmith/engine-notifications) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Proactively identify issues with Red Teaming](https://docs.langchain.com/langsmith/engine-red-teaming) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Connect Engine to your repository](https://docs.langchain.com/langsmith/engine-repositories) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [LangSmith Engine security](https://docs.langchain.com/langsmith/engine-security) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [LangSmith Engine on Self-hosted and BYOC](https://docs.langchain.com/langsmith/engine-self-hosted) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Configure GitHub for Engine on self-hosted and BYOC](https://docs.langchain.com/langsmith/engine-self-hosted-github) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Configure model providers for Engine on self-hosted and BYOC](https://docs.langchain.com/langsmith/engine-self-hosted-model-providers) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Set up LangSmith Engine](https://docs.langchain.com/langsmith/engine-setup) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Cloud Agent Server environment variables](https://docs.langchain.com/langsmith/env-var-cloud) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Self-hosted Agent Server environment variables](https://docs.langchain.com/langsmith/env-var-self-hosted) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [How to implement generative user interfaces with LangGraph](https://docs.langchain.com/langsmith/generative-ui-react) | JS/TS — dự án chỉ Python | 19/10/2026 |
| ➖ | [Harbor integrations](https://docs.langchain.com/langsmith/harbor-integrations) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Hybrid](https://docs.langchain.com/langsmith/hybrid) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [How to evaluate a runnable](https://docs.langchain.com/langsmith/langchain-runnable) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [LangSmith MCP Server](https://docs.langchain.com/langsmith/langsmith-mcp-server) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [LangSmith Remote MCP](https://docs.langchain.com/langsmith/langsmith-remote-mcp) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Set up coding agents](https://docs.langchain.com/langsmith/llm-gateway-coding-agents) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Gateway Credits](https://docs.langchain.com/langsmith/llm-gateway-credits) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Monorepo support](https://docs.langchain.com/langsmith/monorepo-support) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Run evals with openevals package](https://docs.langchain.com/langsmith/openevals) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Redact sensitive data with the OpenTelemetry Gateway architecture](https://docs.langchain.com/langsmith/otel-gateway-trace-redaction) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Model providers](https://docs.langchain.com/langsmith/playground-model-providers) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Preview builds](https://docs.langchain.com/langsmith/preview-builds) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [How to run evaluations with pytest](https://docs.langchain.com/langsmith/pytest) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Agent Server metrics](https://docs.langchain.com/langsmith/self-hosted-agent-server-metrics) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Self-hosted platform features](https://docs.langchain.com/langsmith/self-hosted-platform-features) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Trace JS functions in serverless environments](https://docs.langchain.com/langsmith/serverless-environments) | JS/TS — dự án chỉ Python | 19/10/2026 |
| ➖ | [How to set up an application with requirements.txt](https://docs.langchain.com/langsmith/setup-app-requirements-txt) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [How to set up a JavaScript application](https://docs.langchain.com/langsmith/setup-javascript) | JS/TS — dự án chỉ Python | 19/10/2026 |
| ➖ | [How to set up an application with pyproject.toml](https://docs.langchain.com/langsmith/setup-pyproject) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Fine-tune models with Smithtune](https://docs.langchain.com/langsmith/smithtune) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Test a ReAct agent with Pytest/Vitest and LangSmith](https://docs.langchain.com/langsmith/test-react-agent-pytest) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Write traces to multiple destinations with replicas](https://docs.langchain.com/langsmith/trace-replicas) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Trajectory view integrations](https://docs.langchain.com/langsmith/trajectory-view-integrations) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Set up LangChain Tuned Evaluators](https://docs.langchain.com/langsmith/tuned-evaluators) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [Connect to a TypeSafe-compatible model provider](https://docs.langchain.com/langsmith/typesafe-compatible-model) | đóng gói/khác hệ sinh thái — không cần cho bản tự host | 19/10/2026 |
| ➖ | [Usage and billing](https://docs.langchain.com/langsmith/usage-and-billing) | dịch vụ đóng/cloud/billing/K8s — không áp dụng | 19/10/2026 |
| ➖ | [How to run evaluations with Vitest/Jest](https://docs.langchain.com/langsmith/vitest-jest) | JS/TS — dự án chỉ Python | 19/10/2026 |
