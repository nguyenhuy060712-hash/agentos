# Moza Agent OS
Self-hosted agent platform, tự viết, chạy trên VPS #83 (127.0.0.1:8020, Caddy basic auth :8092, cloudflared quick tunnel).
Tính năng mô phỏng bộ tính năng công khai của LangSmith/LangGraph Platform (đọc docs công khai, KHÔNG sao chép mã/tài sản).
Stack mã nguồn mở: deepagents, langchain, langgraph (+checkpoint-postgres), langchain.mcp (FastMCP client), mcp 2.x, a2a-sdk, langgraph-sdk, langchain-google-genai,
FastAPI/uvicorn, psycopg, PostgreSQL 16 + pgvector (container lc-postgres). Model: chỉ Gemini.

## Cấu trúc
- `deepagent_api.py` – app FastAPI: agents (deepagents), chat/stream, tools/MCP (HA chỉ đọc mặc định, khoá/báo động/gara bị chặn server-side), memory/KB, playground, deployments, fleet, settings
- `gateway.py` – LLM gateway: fallback model, retry, circuit breaker 429, RPM, ngân sách, usage, span tracing
- `obs.py` – P1 Observability: projects, tr_runs (partition theo tháng), ingest SDK, ngôn ngữ lọc, threads, dashboard, alerts, automation rules, share link HMAC, retention, worker nền
- `evals.py` – P2 Evaluation: datasets (phiên bản/split/tag), evaluators code (sandbox) & LLM-judge, experiments, so sánh, pairwise, annotation queues, lịch cron
- `ui.py`, `ui_obs.py` – giao diện HTML/JS inline (không CDN), tiếng Việt
- `sdk/agentos_sdk.py` – SDK `@traceable` cho Python bất kỳ (chỉ thư viện chuẩn)
- `tests/` – script E2E (qua URL public) + test UI (Playwright, chạy trên box)
- `deploy/` – file Caddy, systemd drop-in

## Chạy
systemd `deepagent-api` (WorkingDirectory=/opt/agentos qua drop-in), EnvironmentFile=/opt/langchain/.env, MemoryMax=700M.
Đường public không basic auth (xác thực trong app): `/d/*` (X-API-Key deployment), `/ingest/*` (X-API-Key ingest), `/share/*` (link ký HMAC, có hạn & thu hồi).

## Ngôn ngữ lọc trace
`and(eq(status,"error"), gt(latency,2), has(tags,"agent:1"), search("nhiệt độ"), gte(feedback.dung,4), eq(metadata.agent_version,"2"), in(model,"a","b"), not(eq(run_type,"llm")))`
Trường: name, run_type, status, error, model, thread_id, latency (giây), latency_ms, total/input/output_tokens, cost, start_time, trace_id, id, is_root, tags, metadata.<k>, feedback.<k>.
