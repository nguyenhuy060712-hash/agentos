"""GĐ11.34 Archive / permanent delete of an agent (card 11.34, F1 connections, W22 fleet UX).

DELETE /api/agents/{id}?mode=archive|permanent&confirm=<exact name>
 * archive (default) = today's soft delete (agents.deleted=true): hidden everywhere, nothing removed, `POST /api/agents/{id}/restore` brings it back.
   Same right as editing the agent (owner / workspace admin / 'edit' share / workspace editor on a workspace-visible agent).
 * permanent = OWNER OR WORKSPACE ADMIN only (an 'edit' share or a plain editor is refused), the exact agent name must be typed (`confirm`), demo workspace and
   demo accounts refused, system (hidden) agents refused. Idempotent: repeating it after success answers 200 {"already": true} (marker = the export row).
   1) one Postgres transaction (role aos_rls, app.ws = the agent's workspace, admin view so other users' private chats are reachable): config export row
      (`agent_delete_exports`, 30 days) + every table in PURGED + service users + the agent's long-term store namespace `("agent", id, ...)`;
   2) after commit, each non-DB part is a step that may fail without undoing the delete: provider token revoke (netguard), channel `after_delete` (webhook unlink),
      disk files, MCP live state, then every `register_purge` hook (virtual computer 11.42 is registered below; 11.60 mail triggers / 11.70 memory add theirs).
      A failed step goes to QUEUE (in memory, retried by `tick()` every ~10 min, max 5 tries, logged) - the delete itself stays done.
Kept on purpose (costs / audit / traces): KEPT = gateway_usage, agent_audit, oauth_audit, the export row, and the tr_runs traces with their run_attachments (project retention). Every table with an `agent_id`
column must be in PURGED or KEPT (test_agent_purge_covers_every_agent_id_table fails when a new one appears).
Cross-workspace safety: the agent row is looked up with its workspace, the transaction runs under RLS for THAT workspace only, tables without RLS are reached through
ids collected from the agent's own rows (thread ids, run ids, hook ids ...), `agent_id` is globally unique."""
import inspect, logging, time
from fastapi import APIRouter, HTTPException
from psycopg.types.json import Jsonb

log = logging.getLogger("aos.agent_purge")
router = APIRouter()

MSG_DEMO = "Tắt trong tài khoản demo / off for demo accounts"
PURGED = ("a2a_tasks", "agent_crons", "agent_hooks", "agent_identity", "agent_shares", "agent_versions", "approvals", "channel_bindings", "deployments", "eval_runs",
          "hook_log", "issues", "lg_assistants", "lg_threads", "mcp_servers", "memory_proposals", "sandboxes", "sandbox_snapshots", "simulations", "test_runs")   # sandboxes / sandbox_snapshots: by the "computer" hook (11.42 / 11.47a)
KEPT = ("agent_audit", "agent_delete_exports", "gateway_usage", "oauth_audit")   # agent_delete_exports = the saved config + the "already purged" marker (30 days)
EXPORT_DAYS = 30
MAX_TRIES = 5

# ------------------------------------------------------------------ registry (later cards add their cleanup here)
HOOKS: dict = {}     # name -> async fn(agent_id, workspace_id[, identifier=...]) -> anything; a dict result with a non-empty "errors" counts as failed
QUEUE: list = []     # failed post-commit steps waiting for a retry: {name, agent_id, ws, tries, next_at, error, run}

def register_purge(name: str, fn):
    """Add (or replace, same name) a cleanup run after an agent's permanent delete. fn(agent_id, workspace_id) is async and must be repeatable (a retry calls it again).
    If fn also accepts `identifier` (the agent's text id, gone from the DB by then) it is passed. No hook registered = nothing breaks."""
    if not isinstance(name, str) or not name or not callable(fn): raise ValueError("register_purge(name, fn): a name and an async callable")
    HOOKS[name] = fn; return fn

def unregister_purge(name: str): HOOKS.pop(name, None)

async def _computer(agent_id, workspace_id, *, identifier=None):
    import sbx_runtime
    return await sbx_runtime.purge_for_agent(agent_id, workspace_id=workspace_id, identifier=identifier)   # 11.42: boxes, volumes, folders, commands, agent secrets, its own PURGE_HOOKS
register_purge("computer", _computer)

def _accepts(fn, name):
    try: ps = inspect.signature(fn).parameters
    except (TypeError, ValueError): return False
    return name in ps or any(p.kind is p.VAR_KEYWORD for p in ps.values())

def _failed(r): return isinstance(r, dict) and bool(r.get("errors"))

# ------------------------------------------------------------------ helpers
def _A():
    import deepagent_api as A; return A

async def _q(*a, **k): return await _A().q(*a, **k)

async def _sys_exists(sql, args=()):
    """One read in the SYSTEM context (every workspace, no row filter) - only for 'does any other row still use this blob'. Nothing it returns leaves this module."""
    async with _A().S["pool"].connection() as c:
        await c.execute("SELECT set_config('app.ws','*',false), set_config('app.uid','0',false), set_config('app.role','admin',false), set_config('role','none',false)")
        return bool(await (await c.execute(sql, args)).fetchone())

def _guard_user():
    """Signed-in, non-demo, non-channel, editor+ caller. Demo accounts and demo workspaces never reach the rest."""
    import admin
    A = _A(); u = admin.cur()
    if not u or not u.get("id"): raise HTTPException(401, "Sign in required")
    if u.get("auth") == "channel": raise HTTPException(403, "Kênh ngoài không xoá agent / external channels cannot delete agents")
    if admin.is_demo(u) or int(u.get("ws") or 1) in A.gw.DEMO_WS: raise HTTPException(403, MSG_DEMO)
    if admin.ROLES.get(u.get("role") or "viewer", 1) < admin.ROLES["editor"]: raise HTTPException(403, "Cần quyền chỉnh sửa / editor role required")
    return u

async def _audit(action, aid, ok=True, detail=None):
    try:
        import agent_identity
        await agent_identity.audit(action, agent_id=aid, ok=ok, detail=detail or {}, acted_as="user")
    except Exception as e: log.warning("agent_purge audit failed: %s", e)  # noqa: BLE001

async def _access(aid):
    """Agent row + the caller's rights (RLS: another workspace's agent and an unshared private agent are 404). System (hidden) agents: 403."""
    import agent_sharing
    if await _q("SELECT 1 FROM agents WHERE id=%s AND (hidden OR system_kind IS NOT NULL)", (aid,), one=True): raise HTTPException(403, "Agent hệ thống, không sửa được / built-in agent is read-only")
    return await agent_sharing.access(aid, deleted_ok=True)

# ------------------------------------------------------------------ the route logic
async def delete_agent(aid: int, mode: str = "archive", confirm: str | None = None, purge: bool = False):
    if purge: mode = "permanent"    # legacy test-fixture flag: same path, same rules (confirm and owner/admin are still required)
    if mode not in ("archive", "permanent"): raise HTTPException(422, "mode must be archive or permanent")
    u = _guard_user(); ws = int(u.get("ws") or 1)
    try: x = await _access(aid)
    except HTTPException as e:
        if e.status_code == 404 and mode == "permanent": return await _already(aid, ws, u, confirm)
        raise
    a = x["agent"]
    if mode == "archive":
        if not x["edit"]: raise HTTPException(403, "Bạn chỉ có quyền dùng agent này, không được sửa / you can use this agent but not change it")
        if not a["deleted"]:
            await _q("UPDATE agents SET deleted=true WHERE id=%s AND workspace_id=%s", (aid, ws)); await _audit("agent_archived", aid, detail={"name": a["name"]})
        return {"ok": True, "archived": True}
    if not (x["admin"] or x["owner"]): raise HTTPException(403, "Chỉ chủ agent hoặc quản trị workspace được xoá vĩnh viễn / only the agent owner or a workspace admin can delete permanently")
    if int(a["workspace_id"]) != ws: raise HTTPException(404, "Agent không tồn tại / agent not found")
    if confirm != a["name"]: raise HTTPException(400, "Tên xác nhận không khớp / the confirmation name does not match")
    return await purge_agent(aid, ws, u)

async def _already(aid, ws, u, confirm):
    """The agent row is gone: a finished purge leaves its export row (same workspace, RLS) -> repeat = 200 and a retry of pending steps; otherwise 404 (unknown / other workspace)."""
    e = await _q("SELECT id, name, config FROM agent_delete_exports WHERE agent_id=%s AND workspace_id=%s ORDER BY id DESC LIMIT 1", (aid, ws), one=True)
    if not e: raise HTTPException(404, "Agent không tồn tại / agent not found")
    owner = ((e["config"] or {}).get("meta") or {}).get("owner_id")
    if not (u.get("role") == "admin" or (owner is not None and owner == u["id"])): raise HTTPException(403, "Only the agent owner or a workspace admin")
    if confirm != e["name"]: raise HTTPException(400, "Tên xác nhận không khớp / the confirmation name does not match")
    await retry_queue(aid)
    return {"ok": True, "purged": True, "already": True, "export_id": e["id"]}

@router.post("/api/agents/{aid}/restore")
async def restore_agent(aid: int):
    u = _guard_user(); ws = int(u.get("ws") or 1)
    x = await _access(aid)
    if not x["edit"]: raise HTTPException(403, "Bạn không có quyền khôi phục agent này / you cannot restore this agent")
    if x["agent"]["deleted"]:
        await _q("UPDATE agents SET deleted=false WHERE id=%s AND workspace_id=%s", (aid, ws)); await _audit("agent_restored", aid, detail={"name": x["agent"]["name"]})
    return {"ok": True, "restored": True}

@router.get("/api/archived-agents")
async def archived_agents():
    """Archived agents the caller may restore or delete permanently (RLS hides other workspaces and unshared private agents)."""
    u = _guard_user()
    rows = await _q("SELECT a.id, a.name, a.owner_id, a.visibility, a.created FROM agents a WHERE a.deleted AND NOT a.hidden AND a.workspace_id=%s ORDER BY a.id", (int(u.get("ws") or 1),))
    return rows

def _admin_only():
    import admin
    u = admin.cur()
    if not u or u.get("role") != "admin" or admin.is_demo(u): raise HTTPException(403, "Admin only")
    return u

@router.get("/api/admin/agent-exports")
async def exports_list():
    u = _admin_only()
    return await _q("SELECT id, agent_id, name, deleted_by, created_at, expires_at FROM agent_delete_exports WHERE workspace_id=%s AND expires_at > now() ORDER BY id DESC",
                    (int(u.get("ws") or 1),))

@router.get("/api/admin/agent-exports/{eid}")
async def export_get(eid: int):
    """The saved config of a deleted agent (format moza-agent/1: POST it to /api/agents/import to create the agent again). Admin only, 30 days."""
    u = _admin_only()
    e = await _q("SELECT id, agent_id, name, config, deleted_by, created_at, expires_at FROM agent_delete_exports WHERE id=%s AND workspace_id=%s AND expires_at > now()",
                 (eid, int(u.get("ws") or 1)), one=True)
    if not e: raise HTTPException(404, "Export not found or expired")
    return {"id": e["id"], "agent_id": e["agent_id"], "name": e["name"], "format": "moza-agent/1", "config": (e["config"] or {}).get("config"), "meta": (e["config"] or {}).get("meta"),
            "deleted_by": e["deleted_by"], "created_at": e["created_at"], "expires_at": e["expires_at"]}

# ------------------------------------------------------------------ the purge
async def purge_agent(aid: int, ws: int, u: dict) -> dict:
    """Permission and confirm are checked by the caller. Everything below is scoped to workspace `ws`."""
    A = _A(); aid, ws = int(aid), int(ws)
    deleted, side = {}, {}
    async with A.S["pool"].connection() as c:
        await c.execute("SELECT set_config('app.ws',%s,false), set_config('app.uid','0',false), set_config('app.role','admin',false), set_config('role','aos_rls',false)", (str(ws),))
        try:
            async with c.transaction():
                async def one(sql, args=()): return await (await c.execute(sql, args)).fetchone()
                async def all_(sql, args=()): return await (await c.execute(sql, args)).fetchall()
                async def d(key, sql, args=()):
                    cur = await c.execute(sql, args); deleted[key] = deleted.get(key, 0) + max(cur.rowcount, 0); return cur
                ag = await one("SELECT * FROM agents WHERE id=%s AND workspace_id=%s FOR UPDATE", (aid, ws))
                if not ag: raise HTTPException(404, "Agent không tồn tại / agent not found")
                v = await one("SELECT config FROM agent_versions WHERE agent_id=%s AND version=%s", (aid, ag["current_version"]))
                nver = (await one("SELECT count(*) n FROM agent_versions WHERE agent_id=%s", (aid,)))["n"]
                meta = {"owner_id": ag["owner_id"], "identifier": ag["identifier"], "visibility": ag["visibility"], "version": ag["current_version"], "versions": nver,
                        "created": ag["created"].isoformat() if ag["created"] else None}
                exp = await one("INSERT INTO agent_delete_exports(workspace_id,agent_id,name,config,deleted_by,expires_at) VALUES(%s,%s,%s,%s,%s,now()+make_interval(days=>%s)) RETURNING id",
                                (ws, aid, ag["name"], Jsonb({"format": "moza-agent/1", "config": (v or {}).get("config"), "meta": meta}), u.get("id"), EXPORT_DAYS))
                # --- ids reached through the agent's own rows (tables without RLS are never filtered by anything else)
                asst = [r["assistant_id"] for r in await all_("SELECT assistant_id FROM lg_assistants WHERE agent_id=%s AND workspace_id=%s", (aid, ws))]
                thr = [r["thread_id"] for r in await all_("SELECT thread_id FROM lg_threads WHERE workspace_id=%s AND (agent_id=%s OR assistant_id = ANY(%s::uuid[]))", (ws, aid, asst))]
                runs = [r["run_id"] for r in await all_("SELECT run_id FROM lg_runs WHERE workspace_id=%s AND (thread_id = ANY(%s::text[]) OR assistant_id = ANY(%s::uuid[]))", (ws, thr, asst))]
                hooks = [r["id"] for r in await all_("SELECT id FROM agent_hooks WHERE agent_id=%s", (aid,))]
                bindings = await all_("SELECT id, workspace_id, agent_id, kind, name, secret_ref, status, allowed_users, config, created_by, created_at FROM channel_bindings WHERE agent_id=%s AND workspace_id=%s", (aid, ws))
                creds = await all_("SELECT * FROM oauth_connections WHERE owner_type='agent' AND owner_id=%s AND workspace_id=%s", (aid, ws))
                provs = {p["id"]: p for p in await all_("SELECT * FROM oauth_providers WHERE workspace_id=%s AND id = ANY(%s::int[])", (ws, [r["provider_id"] for r in creds if r["provider_id"]]))}
                side["revokes"] = [(r, provs[r["provider_id"]]) for r in creds if r["kind"] == "oauth" and r["provider_id"] in provs]
                side["bindings"] = bindings
                svc = [r["id"] for r in await all_("SELECT u.id FROM users u JOIN memberships m ON m.user_id=u.id AND m.workspace_id=%s WHERE u.service_agent_id=%s", (ws, aid))]
                side["service_users"] = svc
                # --- chats: runs, checkpoints, reads, files, canvases (cascade), knowledge of those chats
                await d("lg_run_events", "DELETE FROM lg_run_events WHERE run_id = ANY(%s::uuid[])", (runs,))
                await d("lg_runs", "DELETE FROM lg_runs WHERE workspace_id=%s AND run_id = ANY(%s::uuid[])", (ws, runs))
                for t in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"): await d(t, f"DELETE FROM {t} WHERE thread_id = ANY(%s::text[])", (thr,))
                await d("thread_reads", "DELETE FROM thread_reads WHERE workspace_id=%s AND thread_id = ANY(%s::text[])", (ws, thr))
                await d("chat_code_runs", "DELETE FROM chat_code_runs WHERE workspace_id=%s AND thread_id = ANY(%s::text[])", (ws, thr))
                await d("kb_docs", "DELETE FROM kb_docs WHERE workspace_id=%s AND thread_id = ANY(%s::text[])", (ws, thr))   # kb_chunks follow (FK cascade)
                fids = [r["file_id"] for r in await all_("SELECT file_id FROM chat_thread_files WHERE thread_id = ANY(%s::text[])", (thr,))]
                await d("chat_thread_files", "DELETE FROM chat_thread_files WHERE thread_id = ANY(%s::text[])", (thr,))
                cur = await d("chat_files", "DELETE FROM chat_files f WHERE f.workspace_id=%s AND f.id = ANY(%s::uuid[]) AND NOT EXISTS (SELECT 1 FROM chat_thread_files t WHERE t.file_id=f.id)"
                              " AND NOT EXISTS (SELECT 1 FROM chat_project_files p WHERE p.file_id=f.id) AND NOT EXISTS (SELECT 1 FROM kb_docs k WHERE k.file_id=f.id) RETURNING f.id, f.sha256, f.enc", (ws, fids))
                side["files"] = await cur.fetchall()
                await d("hook_calls", "DELETE FROM hook_calls WHERE hook_id = ANY(%s::int[]) OR thread_id = ANY(%s::text[])", (hooks, thr))
                await d("approvals", "DELETE FROM approvals WHERE agent_id=%s", (aid,))
                await d("lg_threads", "DELETE FROM lg_threads WHERE workspace_id=%s AND thread_id = ANY(%s::text[])", (ws, thr))   # chat_fts / chat_canvas / chat_shares cascade
                # --- schedules, hooks, deployments, sharing, channels
                await d("lg_crons", "DELETE FROM lg_crons WHERE workspace_id=%s AND assistant_id = ANY(%s::uuid[])", (ws, asst))
                await d("lg_assistants", "DELETE FROM lg_assistants WHERE agent_id=%s AND workspace_id=%s", (aid, ws))   # lg_assistant_versions cascade
                await d("agent_crons", "DELETE FROM agent_crons WHERE agent_id=%s", (aid,))
                await d("hook_log", "DELETE FROM hook_log WHERE agent_id=%s", (aid,))
                await d("agent_hooks", "DELETE FROM agent_hooks WHERE agent_id=%s", (aid,))
                await d("deployments", "DELETE FROM deployments WHERE agent_id=%s", (aid,))   # deployment_history cascades
                await d("agent_shares", "DELETE FROM agent_shares WHERE agent_id=%s AND workspace_id=%s", (aid, ws))
                await d("channel_bindings", "DELETE FROM channel_bindings WHERE agent_id=%s AND workspace_id=%s", (aid, ws))   # channel_threads / channel_events / zp_sends cascade -> the chat stops answering
                # --- tools and connections
                cur = await d("mcp_servers", "DELETE FROM mcp_servers WHERE agent_id=%s AND workspace_id=%s RETURNING id, name", (aid, ws)); gone = await cur.fetchall()
                side["mcp_ids"] = [r["id"] for r in gone]
                if gone: await d("tool_settings", "DELETE FROM tool_settings t WHERE t.workspace_id=%s AND t.server = ANY(%s::text[]) AND NOT EXISTS (SELECT 1 FROM mcp_servers m WHERE m.workspace_id=t.workspace_id AND m.name=t.server)", (ws, [r["name"] for r in gone]))
                await d("oauth_connections", "DELETE FROM oauth_connections WHERE owner_type='agent' AND owner_id=%s AND workspace_id=%s", (aid, ws))
                # --- evaluation / simulation / identity / service users
                for t in ("eval_runs", "test_runs", "simulations", "issues", "a2a_tasks"):
                    await d(t, f"DELETE FROM {t} WHERE agent_id=%s", (aid,))
                await d("agent_identity", "DELETE FROM agent_identity WHERE agent_id=%s", (aid,))
                if svc:
                    await d("user_api_keys", "DELETE FROM user_api_keys WHERE user_id = ANY(%s::int[])", (svc,))
                    await d("users", "DELETE FROM users WHERE id = ANY(%s::int[]) AND service_agent_id=%s", (svc, aid))   # memberships / sessions cascade
                await d("users_demo_agents", "UPDATE users SET demo_agents = array_remove(demo_agents, %s::int) WHERE %s::int = ANY(demo_agents)", (aid, aid))
                # --- long-term memory: namespace ("agent", "<id>", ...) = store.prefix 'agent.<id>' and 'agent.<id>.*'
                if (await one("SELECT to_regclass('public.store') IS NOT NULL ok"))["ok"]:
                    p = f"agent.{aid}"; await d("store", "DELETE FROM store WHERE prefix = %s OR left(prefix, %s) = %s", (p, len(p) + 1, p + "."))   # store_vectors cascade
                await d("agent_versions", "DELETE FROM agent_versions WHERE agent_id=%s", (aid,))
                await d("agents", "DELETE FROM agents WHERE id=%s AND workspace_id=%s", (aid, ws))
                side.update(export_id=exp["id"], name=ag["name"], identifier=ag["identifier"])
        finally:
            try: await c.execute("SELECT set_config('app.ws','',false), set_config('app.uid','',false), set_config('app.role','',false); RESET ROLE")
            except Exception: pass  # noqa: BLE001
    post = await _after_commit(aid, ws, side)
    await _audit("agent_purged", aid, detail={"name": side["name"], "export_id": side["export_id"], "rows": {k: n for k, n in deleted.items() if n}, "after": post})
    return {"ok": True, "purged": True, "export_id": side["export_id"], "deleted": {k: n for k, n in deleted.items() if n}, "after": post}

# ------------------------------------------------------------------ after the commit: failures are queued, never undo the delete
async def _step(name, aid, ws, run, out):
    try:
        r = await run()
        if _failed(r): raise RuntimeError("; ".join(str(e)[:120] for e in r["errors"][:3]))
        out[name] = "ok" if not isinstance(r, dict) else {k: v for k, v in r.items() if k != "errors"}
    except Exception as e:  # noqa: BLE001
        msg = str(getattr(e, "reason", None) or e)[:200]
        out[name] = "queued: " + msg; log.warning("agent_purge step %s for agent %s failed, queued: %s", name, aid, msg)
        QUEUE.append({"name": name, "agent_id": aid, "ws": ws, "tries": 1, "next_at": time.time() + 300, "error": msg, "run": run})

async def _after_commit(aid, ws, side):
    out = {}
    for r, p in side.get("revokes", ()):
        await _revoke(r, p, out)    # never queued: the token rows are gone, nothing sensitive may sit in memory
    for b in side.get("bindings", ()):
        async def unlink(b=b):
            import channels
            ad = channels.ADAPTERS.get(b["kind"])
            if ad is not None and hasattr(ad, "after_delete"): await ad.after_delete(b)
        await _step(f"channel:{b['kind']}:{b['id']}", aid, ws, unlink, out)
    if side.get("mcp_ids"):
        import mcp_registry
        for sid in side["mcp_ids"]: mcp_registry.drop(sid)
        out["mcp"] = "ok"
    if side.get("service_users"):
        try:
            import agent_identity
            for uid in side["service_users"]: agent_identity._SVC.pop(uid, None)
            agent_identity._MODE.pop(aid, None)
        except Exception: pass  # noqa: BLE001
    if side.get("files"):
        rows = list(side["files"])
        async def files(): return await _unlink_files(rows)
        await _step("files", aid, ws, files, out)
    ident = side.get("identifier")
    for name, fn in list(HOOKS.items()):
        async def run(fn=fn):
            return await (fn(aid, ws, identifier=ident) if _accepts(fn, "identifier") else fn(aid, ws))
        await _step("hook:" + name, aid, ws, run, out)
    return out

async def _revoke(c, p, out):
    """RFC 7009 revoke of an agent's stored token at the provider, through netguard (https, no redirect, never an internal address). Failure only logged."""
    key = "revoke:" + str(p.get("name"))
    if not p.get("revoke_url"): out[key] = "no revoke_url"; return
    try:
        import netguard, oauth_tools as O
        ok = True
        for col, hint in (("refresh_token_enc", "refresh_token"), ("access_token_enc", "access_token")):
            if not c.get(col): continue
            data = {"token": O._tok(c, col), "token_type_hint": hint, "client_id": p["client_id"]}; sec = O._sec(p)
            if p["token_auth"] == "body" and sec: data["client_secret"] = sec
            async with netguard.client("oauth" if "oauth" in netguard.PURPOSES else "webhook") as h:
                r = await h.post(p["revoke_url"], data=data, auth=(p["client_id"], sec) if sec and p["token_auth"] == "basic" else None, timeout=15)
            if r.status_code >= 400: ok = False
        out[key] = "ok" if ok else "provider refused"
    except Exception as e:  # noqa: BLE001
        out[key] = "failed: " + str(getattr(e, "reason", None) or type(e).__name__)[:120]; log.warning("agent_purge token revoke failed (%s)", out[key])

async def _unlink_files(rows):
    """Remove the stored bytes of the deleted chat files (encrypted: by id; plain: content-addressed, only when no other file row has the same hash)."""
    import aos_crypto as C, attachments as AT
    n = 0
    for r in rows:
        try:
            if r["enc"]: p = C.enc_path(r["id"])
            else:
                if await _sys_exists("SELECT 1 FROM chat_files WHERE sha256=%s LIMIT 1", (r["sha256"],)): continue   # plain blobs are shared by hash across users AND workspaces
                p = AT.DIR / r["sha256"][:2] / r["sha256"]
            p.unlink(missing_ok=True); n += 1
        except OSError as e: raise RuntimeError("file removal failed: " + str(e)[:100]) from None
    return {"files": n}

async def retry_queue(agent_id=None):
    """Run the queued steps that are due (agent_id given: that agent's, due or not). Done -> removed; MAX_TRIES -> dropped with an error log."""
    now, done = time.time(), 0
    for it in list(QUEUE):
        if agent_id is not None and it["agent_id"] != agent_id: continue
        if agent_id is None and it["next_at"] > now: continue
        out = {}
        try:
            r = await it["run"]()
            if _failed(r): raise RuntimeError("; ".join(str(e)[:120] for e in r["errors"][:3]))
            QUEUE.remove(it); done += 1
        except Exception as e:  # noqa: BLE001
            it["tries"] += 1; it["error"] = str(getattr(e, "reason", None) or e)[:200]; it["next_at"] = now + 300 * it["tries"]
            if it["tries"] > MAX_TRIES:
                QUEUE.remove(it); log.error("agent_purge step %s for agent %s given up after %s tries: %s", it["name"], it["agent_id"], MAX_TRIES, it["error"])
    return done

async def tick():
    """Worker (obs.worker, every ~10 min): retry queued steps and delete expired export rows (system context = every workspace)."""
    await retry_queue()
    await _q("DELETE FROM agent_delete_exports WHERE expires_at < now()")
