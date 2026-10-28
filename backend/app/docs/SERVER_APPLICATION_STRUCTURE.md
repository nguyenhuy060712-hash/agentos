# Application structure

An Agent OS "application" is smaller than a LangGraph Platform one, because the server already runs the graph for you:

| LangSmith Deployment | Agent OS (this server) |
|---|---|
| `langgraph.json` (graphs, dependencies, env, http, auth, checkpointer) | **agents** are rows in Postgres (Build page / `POST /api/agents`, versioned) + one optional file **`aos.json`** for server behaviour |
| a graph per file (`./agent.py:graph`) | `create_deep_agent(...)` built from the agent's config (model, prompt, tools, sub-agents, skills, context pack) |
| `.env` | the systemd service's `.env` (secrets never go into `aos.json` or the database) |
| Agent Server API (`/assistants`, `/threads`, `/runs`, `/store`) | the same contract under `/api/v1` — use `langgraph-sdk` with `url=https://<host>/api/v1` |
| LangSmith API key | an Agent OS API key `aos_…` (`X-Api-Key`), a session cookie, or a provider JWT (Settings > SSO) |

## Files

```
/opt/agentos/                 # the service's directory
├── .env                      # GOOGLE_API_KEY, PG_URI, AOS_* secrets (mode 600)
├── aos.json                  # optional: http headers, checkpointer, custom authentication
├── auth.py                   # optional: a langgraph_sdk.Auth object (custom authentication)
└── checkpointer.py           # optional: an async context manager that yields a BaseCheckpointSaver
```

`aos.json` is validated against a JSON schema (`GET /api/server-config/schema`; add `"$schema": "https://<host>/api/server-config/schema"` for editor completion).
An invalid file is ignored and reported in `GET /api/server-config`. The path can be changed with `AOS_CONFIG`.

```json
{
  "http": {
    "configurable_headers": { "includes": ["x-user-id", "x-organization-id", "my-prefix-*"], "excludes": ["x-internal-*"] },
    "logging_headers":      { "includes": ["x-request-id", "*-trace-*"], "excludes": ["x-user-id"] }
  },
  "auth": {
    "path": "./auth.py:auth",
    "paths": ["/api/v1/"],
    "max_role": "editor",
    "workspace": 1,
    "openapi": { "securitySchemes": { "oauth2": { "type": "oauth2", "flows": { "implicit": { "authorizationUrl": "https://idp.example/authorize", "scopes": { "threads": "Manage threads" } } } } }, "security": [ { "oauth2": ["threads"] } ] }
  },
  "checkpointer": "postgres",
  "cache": { "max_ttl_s": 86400 }
}
```

* **Declarative** parts (the header lists) can also be edited in the UI (Server settings); the UI value wins over the file.
* **Code-loading** parts (`auth.path`, a custom `checkpointer`) can only come from the file or the environment on the server disk. They are deliberately not editable over HTTP: an API that loads Python by path would be remote code execution for any admin account.

## Runtime configuration from HTTP headers

`http.configurable_headers` copies matching request headers into the run's `config["configurable"]` (lower-case names). `*` matches any characters, matching is
case-insensitive, **excludes win over includes**, and credentials (`authorization`, `cookie`, `x-api-key`, `proxy-authorization`, …) can never be copied.

```python
def search_everything(query: str, config: RunnableConfig):
    org = config["configurable"].get("x-organization-id")
```

The headers are captured when the run is created (and signed), so a queued or scheduled run sees the headers of the request that created it; a client cannot smuggle its own through run metadata.

## Headers in server logs

Every API request writes one line to the service log (`method path -> status ms`). Headers appear only if `http.logging_headers` allows them; values are cut to 300 characters and control characters are replaced, so a client cannot forge log lines.
Requests that carry an allow-listed header are also kept for 7 days in `http_log` (Server settings > Logs; `GET /api/server-config/logs?header=x-request-id&value=abc`).

## Server-side caching

`srv_parity.swr / cache_get / cache_set` (workspace-scoped, Postgres, TTL capped at 1 day, JSON values up to 256 KB) mirror `langgraph_sdk.cache`, and `PgCache()` is a LangGraph `BaseCache` for node `CachePolicy`:

```python
from srv_parity import swr, PgCache
res = await swr("config:global", load_config, fresh_for=timedelta(minutes=5), max_age=timedelta(hours=1))   # res.value, res.status = miss|fresh|stale|expired
graph = builder.compile(cache=PgCache())      # + builder.add_node("n", fn, cache_policy=CachePolicy(ttl=60))
```
Over HTTP (any signed-in member): `PUT/GET/DELETE /api/cache/{key}`.

## Custom checkpointer

`"checkpointer"`: `"postgres"` (default, supports per-workspace encryption of checkpoints) · `"sqlite:///var/checkpoints.db"` · `"memory"` (development only) · `{"path": "./checkpointer.py:generate_checkpointer"}`.
A custom one is an async context manager that yields a `BaseCheckpointSaver`; the server opens it for its whole life and runs a put/get/list/delete smoke test before serving.
Redis, MongoDB, … work the same way through a custom module (install the package in the venv). Postgres-only features (checkpoint encryption, SQL maintenance) do not apply to other backends.

## OpenAPI

`/openapi.json` declares the security schemes (`ApiKeyAuth`, `BearerAuth`, `BasicAuth`, `SessionCookie`, `GatewayKey` for `/v1/*`, `IngestKey` for tracing ingest) so generated clients know how to authenticate. `auth.openapi` in `aos.json` adds or replaces schemes for your own provider (documentation only: the enforcement is the authentication code).
