# Sandboxes: snapshots, mounts, service URLs, CLI

Isolation: gVisor (`runsc`) when Docker has it, else runc with the same limits; 256 MB RAM, 0.5 CPU, 64 processes, read-only root, user `nobody`, **no network** for code runs.

## Snapshots
`POST /api/sandboxes/{id}/snapshots` captures the sandbox's files (not processes). `GET /api/sandboxes/{id}/snapshots`, `POST /api/sandbox_snapshots/{snap}/restore/{id}`,
`POST /api/sandboxes/from-snapshot {"snapshot": "<id or name>"}` boots a new sandbox. A snapshot that a sandbox was created or restored from cannot be deleted (409). 20 per workspace, 90-day retention.
Not offered: building snapshots from container registries / Dockerfiles (one fixed, audited image; no registry credentials on the box) and memory snapshots.

## Mounts
Persistent **volumes** are mounted at `/mnt/<name>` (read-write or read-only, up to 4, shared by the sandboxes of a workspace): `POST /api/sandbox_volumes`, `POST /api/sandboxes/{id}/exec_mounted`.
`POST /api/sandbox_volumes/import` copies a **public git repository** (https only, shallow, no hooks, no credentials, public addresses only, ≤ 50 MB / 5000 files, symlinks and `.git` removed) or a **Context Hub pack** into a *locked* volume that can only be mounted read-only.
S3 / GCS bucket mounts are not offered: a sandbox has no network by design — bring data in through volumes, uploads or the agent's own tools.

## Service URLs
Start a long-running service (`POST /api/sandboxes/{id}/services`, internal no-egress network, max 3 at once). It is reachable
* by signed-in members at `/api/sandboxes/{id}/svc/{svc}/…` (HTTPS only), and
* by anyone holding a **service URL**: `POST /api/sandboxes/{id}/service-url {"service": "<id>", "expires_in_seconds": 600}` (≤ 24 h) → `browser_url` (sets a cookie scoped to that one service) and `service_url` + `X-Aos-Sandbox-Service-Token`.
The token is an HMAC over (sandbox, service, expiry, epoch): wrong, expired, other-service or revoked (`POST …/services/{svc}/revoke-links`) tokens all get the same 403; the token and cookies are stripped before the request reaches the service; the page runs under `Content-Security-Policy: sandbox`.

## Auth proxy
Not offered, on purpose: code in a sandbox cannot make network calls at all, so there is nothing to allow-list or inject credentials into. Credentials never enter a sandbox; agents call third-party APIs through host-side tools (`oauth_request`, workspace secrets), which are audited.

## CLI
`python app/sdk/aos_cli.py` (needs only `httpx`; `AOS_URL`, `AOS_API_KEY`):
```
aos sandbox create --name demo            aos sandbox put ID ./data.csv         aos sandbox exec ID -c 'print(open("data.csv").read())'
aos sandbox snapshot create ID --name base   aos sandbox create --from-snapshot base
aos sandbox service start ID --port 8000 -- python -m http.server 8000     aos sandbox service url ID SVC --ttl 3600
aos volume import-git docs https://github.com/org/repo --ref main          aos apps push ./my-app
```
