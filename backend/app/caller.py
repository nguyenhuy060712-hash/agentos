"""GĐ9.Q (P4/P5/P7) per-run CALLER context: who a run acts as (+ on whose behalf) and the allow-listed request headers.

LangSmith / LangGraph semantics mirrored here:
- `config["configurable"]["x-organization-id"]` = HTTP headers allow-listed by `http.configurable_headers` (srv_parity),
- `config["configurable"]["langgraph_auth_user"]` = the authenticated user (custom auth / OIDC / session) incl. the custom fields the
  `@auth.authenticate` handler returned (secret-looking fields are dropped, never stored in run metadata),
- Fleet "agent identity": a run made with an agent SERVICE KEY acts as the agent, optionally `on_behalf_of` a user (X-On-Behalf-Of).
The snapshot is taken in the HTTP request (server._create_run -> run_meta) and restored in the queue worker (server._execute -> restore),
because the run may execute in another process. Tools read it with `caller.current()` or from the run config."""
import contextvars, json, re

REQ_HDRS: contextvars.ContextVar = contextvars.ContextVar("aos_req_headers", default=None)   # lower-cased headers of the HTTP request in flight
EXTRA: contextvars.ContextVar = contextvars.ContextVar("aos_caller_extra", default=None)     # set while authenticating: kind, auth_user, on_behalf_of, agent_id
RUN: contextvars.ContextVar = contextvars.ContextVar("aos_run_caller", default=None)         # restored for the run being executed

SECRET_KEY = re.compile(r"token|secret|password|passwd|credential|api[_-]?key|authorization|cookie|private|bearer", re.I)

def clean_auth_user(d) -> dict:
    """The custom-auth user dict, JSON-safe, without anything that looks like a secret (those stay in the secret store)."""
    out = {}
    for k, v in (d or {}).items():
        if SECRET_KEY.search(str(k)): continue
        try: json.dumps(v)
        except (TypeError, ValueError): v = str(v)
        out[str(k)[:60]] = v if not isinstance(v, str) else v[:300]
    return out

def kind_of(u: dict) -> str:
    a = (u or {}).get("auth") or ""
    if (EXTRA.get() or {}).get("kind"): return EXTRA.get()["kind"]
    if a.startswith("api_key"): return "api_key"
    if a in ("channel", "run"): return a
    return a or "user"

def snapshot() -> dict:
    """Identity of the request in flight (called while the HTTP request context is live)."""
    import admin
    u = admin.cur() or {}; ex = EXTRA.get() or {}
    c = {"user_id": u.get("id"), "username": u.get("username"), "role": u.get("role"), "ws": u.get("ws"), "auth": u.get("auth"), "kind": kind_of(u),
         "scope": u.get("scope") or "full"}
    for k in ("on_behalf_of", "agent_id", "scopes"):
        if ex.get(k) is not None: c[k] = ex[k]
    if ex.get("auth_user"): c["auth_user"] = clean_auth_user(ex["auth_user"])
    return c

def _sig(c: dict, h: dict) -> str:
    import ext
    return ext._sign("caller|" + json.dumps([c, h], sort_keys=True, default=str))

def run_meta() -> dict:
    """Added to lg_runs.metadata at creation (same pattern as otel_ctx.run_meta). Always returns the three keys so a client cannot smuggle its own `_caller`/`_hdrs`
    through run metadata, and the pair is HMAC-signed: `restore` ignores anything the server did not sign (cron / channel / hand-written rows)."""
    import srv_parity
    c = snapshot(); h = srv_parity.pick_configurable(REQ_HDRS.get())
    return {"_caller": c, "_hdrs": h, "_csig": _sig(c, h)}

def restore(md: dict | None):
    md = md or {}
    c, h = md.get("_caller"), md.get("_hdrs")
    ok = isinstance(c, dict) and isinstance(h, dict) and isinstance(md.get("_csig"), str)
    if ok:
        import hmac
        try: ok = hmac.compare_digest(md["_csig"], _sig(c, h))
        except Exception: ok = False  # noqa: BLE001
    RUN.set({"caller": c, "headers": h} if ok else None)

def current() -> dict:
    """Caller of the running run (tools). Falls back to the HTTP request identity (direct calls / tests) and to an anonymous system caller."""
    r = RUN.get()
    if r and r.get("caller"): return r["caller"]
    try: return snapshot() if __import__("admin").cur() else {"kind": "system", "username": None, "user_id": None}
    except Exception:  # noqa: BLE001
        return {"kind": "system", "username": None, "user_id": None}

def scopes_of(c: dict) -> list[str]:
    """Scopes the caller may use: key scope (full/read), role, permissions from custom auth; tools can refuse on these."""
    s = [f"role:{c.get('role') or 'none'}", f"scope:{c.get('scope') or 'full'}"]
    s += [f"perm:{p}" for p in ((c.get("auth_user") or {}).get("permissions") or [])][:50]
    s += [str(x) for x in (c.get("scopes") or [])][:50]
    return s

def configurable() -> dict:
    """Merged into the graph config `configurable` of every run (deepagent_api._run_agent)."""
    r = RUN.get() or {}; out = dict(r.get("headers") or {})
    out.pop("aos_trust", None)    # GĐ11.72: the trust flag is never taken from a request header; it is written below from the server's own state and can only go up
    import run_trust
    if run_trust.current() == run_trust.EXTERNAL: out["aos_trust"] = run_trust.EXTERNAL
    c = r.get("caller")
    if c:
        au = dict(c.get("auth_user") or {})
        out["langgraph_auth_user"] = {"identity": au.get("identity") or c.get("username") or "", "display_name": au.get("display_name") or c.get("username") or "",
                                      "is_authenticated": True, "permissions": list(au.get("permissions") or []),
                                      **{k: v for k, v in au.items() if k not in ("identity", "display_name", "permissions")},
                                      "aos": {k: c.get(k) for k in ("kind", "role", "ws", "scope", "on_behalf_of", "agent_id") if c.get(k) is not None}}
    return out
