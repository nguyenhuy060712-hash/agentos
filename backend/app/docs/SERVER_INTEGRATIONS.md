# Integrations

How other software connects to Agent OS (self-hosted; everything below is open source or an open standard).

## Send traces to Agent OS

| You use | How |
|---|---|
| **LangChain / LangGraph / Deep Agents** | OpenTelemetry (below), or `app/sdk/agentos_sdk.py` (batches to `POST /ingest/runs` with a tracing-project ingest key `x-api-key`) |
| **OpenTelemetry (any language)** | OTLP/HTTP to `https://<host>/otel/v1/traces` with `x-api-key: <ingest key>`; GenAI semantic conventions v1.44 are understood |
| **OpenAI SDK, Anthropic SDK, Vercel AI SDK, LiteLLM, DSPy, CrewAI, AutoGen, Pydantic AI, LlamaIndex, Mastra, Claude Agent SDK, OpenAI Agents SDK** | their OpenTelemetry / OpenInference instrumentation → the OTLP endpoint above |
| **Any Python code** | `app/sdk/agentos_sdk.py`: `@ao.traceable`, `ao.trace(...)`, feedback |
| **pytest** | `app/sdk/agentos_pytest.py` |

Environments (dev/staging/prod), W3C `traceparent` propagation (in and out) and Parquet / OTLP export are in Settings.

## Use models through the gateway

`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`, `/v1/messages` (Anthropic format), `/v1/decisions` (classification / scoring) with a virtual key (`vk-…`): budgets, rate limits, per-customer `X-Gateway-*` policies, PII masking, fallbacks, routing (`model: "auto"`). Any OpenAI-compatible client works by changing `base_url`.

## Talk to agents

| Protocol | Endpoint |
|---|---|
| LangGraph Agent Server API (`langgraph-sdk`, `agent-chat-ui`, RemoteGraph) | `https://<host>/api/v1` |
| MCP 2026-07-28 (`mcp` 2.x) | `/mcp` (list/ask agents), `/mcp/aos` (platform tools), `/mcp/agents/{id}` |
| A2A 1.0 | `POST /a2a/{agent_id}` (JSON-RPC), card at `/a2a/{agent_id}/.well-known/agent-card.json` |
| Channels | Telegram, Slack, Zalo OA, webhook |
| Browser apps | Custom Apps (sandboxed UIs, `aos apps push`) |

## Tools

MCP servers (Tools page), per-user OAuth connections (GitHub, Google, Microsoft 365, custom providers), workspace Google / Microsoft tools, code sandbox, Context Hub packs and skills.

## Operate

Parquet export, OpenTelemetry export, S3-compatible and rclone/restic offsite backups, the `aos` CLI (`app/sdk/aos_cli.py`: sandboxes, snapshots, service URLs, volumes, apps), `/openapi.json` with security schemes for generated clients.
