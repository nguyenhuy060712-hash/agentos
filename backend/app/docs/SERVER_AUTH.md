# Authentication and authorization for the Agent Server API

Three layers, in this order:

1. **Built-in** (always on): session cookie, `X-Api-Key: aos_…`, HTTP Basic. Roles viewer / editor / admin per workspace, Postgres row-level security.
2. **Identity provider** (Settings > SSO): OpenID Connect login for people, and provider **JWTs as `Authorization: Bearer …`** for programs.
3. **Custom authentication** (`aos.json` > `auth.path`): your own `@auth.authenticate` and `@auth.on` handlers, written with the official `langgraph_sdk.Auth` class — code written for LangGraph Platform runs unchanged.

Layers 2 and 3 only identify callers on `auth.paths` (default `/api/v1/` = the Agent Server API). They can never reach the admin UI or `/api/admin`, `/api/auth`, `/api/secrets`, … , and the role they receive is capped by `auth.max_role` (default `editor`).

## Custom authentication (`auth.py`)

```python
from langgraph_sdk import Auth
auth = Auth()

@auth.authenticate                       # runs for callers the built-in methods did not recognise
async def authenticate(authorization: str | None, headers: dict) -> Auth.types.MinimalUserDict:
    user = await verify_with_my_idp(authorization)          # raise Auth.exceptions.HTTPException(401, "…") to refuse
    return {"identity": user.id, "display_name": user.name, "permissions": user.scopes, "role": "editor", "org": user.org}

@auth.on                                 # the single-owner pattern: every write is stamped, every read is filtered
async def owner_only(ctx: Auth.types.AuthContext, value: dict):
    md = value.setdefault("metadata", {}); md["owner"] = ctx.user.identity
    return {"owner": ctx.user.identity}

@auth.on.threads.create_run              # most specific handler wins: (resource, action) > (resource, *) > (*, action) > global
async def runs(ctx, value): …
```

* Parameters injected by name into `authenticate`: `request`, `body`, `path`, `method`, `path_params`, `query_params`, `headers` (bytes keys), `authorization`.
* Resources: `assistants`, `threads` (create / read / update / delete / search / create_run), `runs` (stateless runs), `crons`, `store` (put / get / search / delete / list_namespaces).
* A handler returns `None`/`True` (allow), `False` (403) or a **filter** (`{"owner": "x"}`, `{"k": {"$eq": "x"}}`, `{"tags": {"$contains": "x"}}`). A filter is enforced on single resources (a non-matching thread answers 404, as if it did not exist) and merged into search bodies. Changes to `value["metadata"]` / `value["namespace"]` are written back.
* Fail-closed: if the module is configured but cannot be imported, the API answers 503 until it is fixed; a handler that raises denies the call; an API route that is not covered by the handler table is denied while authorization is active (a test lists every `/api/v1` route).
* `ctx.permissions` always contains `authenticated` and `role:<viewer|editor|admin>` plus the permissions you returned.
* The authenticated user reaches the graph as `config["configurable"]["langgraph_auth_user"]` (identity, display_name, permissions, your custom fields, and `aos` = {kind, role, scope, on_behalf_of}). Fields whose name looks secret (`token`, `secret`, `password`, `api_key`, …) are dropped: keep credentials in the workspace secret store.
* Identities become hidden Agent OS users (`ext.custom.<id>`, no password) in `auth.workspace`; a superadmin can never be reached through an external identity.

### Without code: the declarative policy (Server settings > Authentication)

```json
{"rules": [
  {"resources": ["threads", "runs", "crons"], "actions": ["*"], "mode": "owner", "admin_bypass": true, "workspaces": [1]},
  {"resources": ["assistants"], "actions": ["create", "update", "delete"], "mode": "deny", "roles": ["editor"]}
]}
```
First matching rule wins; `mode` is `allow`, `deny` or `owner` (the owner pattern above, workspace admins exempt unless `admin_bypass` is false). The dry run `POST /api/server-config/auth/test` shows the decision for an identity.

## Connect an identity provider

Settings > SSO: pick a preset (Google, Microsoft Entra ID with your tenant id, Keycloak, Okta, Auth0), fill the client id, put the client secret in the service `.env` (the form stores only the variable *name*). Then:

* **Group → role mapping**: the group claim (`groups`, or a dotted path like `realm_access.roles`) is mapped to workspace roles: `{"agentos-admins": {"role": "editor", "workspace": 2}}`. `admin` is only honoured with *Allow admin from groups*; `Sync` removes memberships the groups no longer grant; `Allowed groups` / `Require a mapped group` refuse everyone else.
* **API access with the provider's tokens**: enable *Accept provider JWTs on the API* and set the audience. The server verifies signature (JWKS, rotated keys re-fetched), `iss`, `aud`, `exp`/`nbf`, accepts only asymmetric algorithms (never `none` or `HS*`), and clamps the role to `auth.max_role`.

```bash
curl -H "Authorization: Bearer $JWT" https://<host>/api/v1/threads/search -d '{}' -H 'content-type: application/json'
```
```python
client = get_client(url="https://<host>/api/v1", headers={"Authorization": f"Bearer {jwt}"})
```

## Agents and identity

* **Agent identity** (agent page > Identity): `user` credentials (the chatting user's connected accounts) or `agent` credentials (the owner's accounts, fixed — needed for channels / schedules). Only the owner or an admin can change it; every change is audited.
* **Service key**: `POST /api/agents/{id}/service-key` → one `aos_…` key that can only run that agent and read its threads. Send `X-On-Behalf-Of: <username>` to label whose request you are serving; the audit (`GET /api/agents/{id}/audit`) then reads "agent X acted as agent … on behalf of user Y". It labels; it does not lend Y's credentials.
* **Auth-aware tools**: a tool that returns `{"auth_required": true, "auth_url": "…", "auth_id": "…"}` pauses the run with an `auth_required` interrupt; after the user connects, resume the run (`command.resume`) and the same call is retried once. Unattended callers get a plain-text message instead. Tools can read the caller (`caller.current()`, `aos_whoami(require_scope=…)`) to tailor or refuse.
