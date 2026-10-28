"""GĐ8.8 Agent sharing (23/10/2026): per-user shares with role 'use' (chat / run / MCP) or 'edit' (config changes) + agent visibility.
- agents.visibility 'workspace' (default = behaviour before 8.8: every member sees the agent, editors edit it) | 'private'
  (only the owner, workspace admins and users it is shared with see it). agents.owner_id = creator (NULL for older agents).
- Enforced in Postgres: the agents RLS policy hides private agents (child tables agent_versions/deployments/... follow through
  "agent_id IN (SELECT id FROM agents)"; lg_assistants of a hidden agent are hidden too) -> unshared users get 404 everywhere.
- Edit rights (deepagent_api._visible -> require_edit): owner / admin always; a share decides for the shared user ('use' -> 403 on
  config changes); no share -> workspace agents keep today's role rule (editor+), private agents are invisible.
- Groups (GĐ9.P1): workspace user groups (user_groups + user_group_members, admin CRUD under /api/admin/groups); a share with principal_kind
  'group' applies to every member. Honoured by the agents RLS policy, access() (edit beats use), per-agent MCP / A2A (both call access) and the
  viewer chat rule (admin middleware has_share + server._viewer_guard_run).
Tables agent_shares, user_groups, user_group_members: workspace_id + RLS (policies() below)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
DDL = """
ALTER TABLE agents ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'workspace';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_id INT;
CREATE TABLE IF NOT EXISTS agent_shares(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1,
  agent_id INT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  principal_kind TEXT NOT NULL DEFAULT 'user' CHECK (principal_kind IN ('user','group')), principal_id INT NOT NULL,
  role TEXT NOT NULL DEFAULT 'use' CHECK (role IN ('use','edit')), created_by INT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(agent_id, principal_kind, principal_id));
CREATE INDEX IF NOT EXISTS agent_shares_principal ON agent_shares(principal_kind, principal_id);
CREATE TABLE IF NOT EXISTS user_groups(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  created_by INT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, name));
CREATE TABLE IF NOT EXISTS user_group_members(group_id INT NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE, user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  workspace_id INT NOT NULL DEFAULT 1, PRIMARY KEY(group_id, user_id));
CREATE INDEX IF NOT EXISTS user_group_members_user ON user_group_members(user_id);
"""
GROUP_SHARE_SQL = ("(s.principal_kind = 'user' AND s.principal_id = {uid}) OR (s.principal_kind = 'group' AND s.principal_id IN "
                   "(SELECT m.group_id FROM user_group_members m WHERE m.user_id = {uid}))")
# private agents: owner, workspace admins (aos_priv) and shared users only. agent_shares' own policy does not read agents -> no recursion.
AGENTS_POLICY = ("aos_ws() IS NULL OR (workspace_id = aos_ws() AND (visibility <> 'private' OR aos_priv() OR owner_id = aos_uid()"
                 " OR id IN (SELECT s.agent_id FROM agent_shares s WHERE " + GROUP_SHARE_SQL.format(uid="aos_uid()") + ")))")
ASSISTANTS_POLICY = "aos_ws() IS NULL OR (workspace_id = aos_ws() AND (agent_id IS NULL OR agent_id IN (SELECT id FROM agents)))"

async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

async def init():
    """Before admin.rls_init (owner role): columns + table."""
    await _q(DDL)
    try: await _q("ALTER TABLE agents ADD CONSTRAINT agents_visibility_chk CHECK (visibility IN ('private','workspace'))")
    except Exception: pass  # noqa: BLE001  (already there)

async def policies():
    """After admin.rls_init: owner default + the visibility-aware policies (replace the plain workspace policy of agents / lg_assistants)."""
    import admin
    await _q("ALTER TABLE agents ALTER COLUMN owner_id SET DEFAULT aos_uid()")
    for t in ("user_groups", "user_group_members"): await admin._policy(t, "aos_ws() IS NULL OR workspace_id = aos_ws()")
    await admin._policy("agents", AGENTS_POLICY)
    if await _q("SELECT 1 FROM information_schema.tables WHERE table_name='lg_assistants'", one=True):
        await admin._policy("lg_assistants", ASSISTANTS_POLICY)

# ------------------------------------------------------------------ access
async def access(aid: int, deleted_ok: bool = False) -> dict:
    """Agent as seen by the caller (RLS) + the caller's rights. 404 if the agent does not exist / is private and not shared.
    deleted_ok (GĐ11.34): also an archived agent (deleted=true) - only the restore / permanent-delete routes ask for it; the result then carries `deleted`."""
    import admin
    a = await _q("SELECT id, name, owner_id, visibility, workspace_id, deleted FROM agents WHERE id=%s AND (%s OR NOT deleted) AND NOT hidden", (aid, deleted_ok), one=True)
    if not a: raise HTTPException(404, "Agent không tồn tại / agent not found")
    u = admin.cur() or {}
    uid, role = u.get("id"), u.get("role") or "viewer"
    sh = await _q("SELECT s.role FROM agent_shares s WHERE s.agent_id=%s AND (" + GROUP_SHARE_SQL.format(uid="%s") + ") ORDER BY (s.role='edit') DESC LIMIT 1",
                  (aid, uid, uid), one=True) if uid else None   # user share or any group share of the user; 'edit' wins
    sh = sh and sh["role"]
    is_admin, is_owner = role == "admin" and not u.get("demo"), bool(uid and a["owner_id"] == uid)
    editor = admin.ROLES.get(role, 1) >= admin.ROLES["editor"] and not u.get("demo")
    if is_admin or is_owner: use = edit = True
    elif sh: use, edit = True, sh == "edit"
    else: use = edit = a["visibility"] == "workspace" and editor
    manage = is_admin or is_owner or sh == "edit" or (a["owner_id"] is None and edit)
    return {"agent": a, "share": sh, "owner": is_owner, "admin": is_admin, "use": use, "edit": edit, "manage": manage}

async def require_edit(aid: int):
    if not (await access(aid))["edit"]: raise HTTPException(403, "Bạn chỉ có quyền dùng agent này, không được sửa / you can use this agent but not change it")

async def guard_edit(aid):
    """8.P: agent-scoped writes outside the config API (channels, crons, hooks, deployments). Row invisible -> 404; deleted / built-in
    (hidden) agents keep the old role rule (so leftovers can still be cleaned up); otherwise the same rule as require_edit."""
    if aid is None: return
    r = await _q("SELECT deleted, hidden FROM agents WHERE id=%s", (aid,), one=True)
    if not r: raise HTTPException(404, "Agent không tồn tại / agent not found")
    if not (r["deleted"] or r["hidden"]): await require_edit(aid)

async def require_use(aid: int):
    x = await access(aid)
    if not x["use"]: raise HTTPException(403, "Bạn không có quyền dùng agent này / you cannot use this agent")
    return x

# ------------------------------------------------------------------ API
class ShareReq(BaseModel):
    username: str | None = Field(default=None, max_length=80)
    user_id: int | None = None
    group_id: int | None = None
    role: str = Field(default="use", pattern=r"^(use|edit)$")
    visibility: str | None = Field(default=None, pattern=r"^(private|workspace)$")

async def _list(aid, x):
    shares = await _q("SELECT s.id, s.principal_kind, s.principal_id, s.role, s.created_at, u.username, u.display_name, g.name group_name,"
                      " (SELECT count(*) FROM user_group_members m WHERE m.group_id=g.id) group_members FROM agent_shares s"
                      " LEFT JOIN users u ON u.id=s.principal_id AND s.principal_kind='user' LEFT JOIN user_groups g ON g.id=s.principal_id AND s.principal_kind='group'"
                      " WHERE s.agent_id=%s ORDER BY s.id", (aid,))
    groups = await _q("SELECT g.id, g.name, (SELECT count(*) FROM user_group_members m WHERE m.group_id=g.id) members FROM user_groups g"
                      " WHERE g.workspace_id=%s ORDER BY g.name", (x["agent"]["workspace_id"],)) if x["manage"] else []
    owner = await _q("SELECT id, username, display_name FROM users WHERE id=%s", (x["agent"]["owner_id"],), one=True) if x["agent"]["owner_id"] else None
    members = await _q("SELECT u.id, u.username, u.display_name, m.role FROM memberships m JOIN users u ON u.id=m.user_id"
                       " WHERE m.workspace_id=%s AND NOT u.disabled AND NOT u.demo ORDER BY u.username", (x["agent"]["workspace_id"],)) if x["manage"] else []
    return {"agent_id": aid, "visibility": x["agent"]["visibility"], "owner": owner, "shares": shares, "members": members, "groups": groups,
            "me": {k: x[k] for k in ("share", "owner", "admin", "use", "edit", "manage")}, "groups_supported": True}

@router.get("/api/agents/{aid}/shares")
async def shares_get(aid: int):
    x = await access(aid); return await _list(aid, x)

@router.post("/api/agents/{aid}/shares")
async def shares_add(aid: int, b: ShareReq):
    import admin
    x = await access(aid); u = admin.cur() or {}
    if not x["manage"]: raise HTTPException(403, "Chỉ chủ agent, admin hoặc người có quyền sửa mới chia sẻ được / only the owner, an admin or an editor of this agent can share it")
    if b.visibility:
        if not (x["owner"] or x["admin"] or x["agent"]["owner_id"] is None):
            raise HTTPException(403, "Chỉ chủ agent hoặc admin đổi được chế độ hiển thị / only the owner or an admin can change visibility")
        # an older agent without owner: whoever makes it private becomes its owner (else they would lose it)
        await _q("UPDATE agents SET visibility=%s, owner_id=coalesce(owner_id, %s) WHERE id=%s", (b.visibility, u.get("id"), aid))
    if b.group_id:
        g = await _q("SELECT id FROM user_groups WHERE id=%s AND workspace_id=%s", (b.group_id, x["agent"]["workspace_id"]), one=True)
        if not g: raise HTTPException(404, "Không có nhóm này trong workspace / group not found in this workspace")
        if b.role == "edit" and not (x["owner"] or x["admin"] or x["edit"]): raise HTTPException(403, "Không đủ quyền / not allowed")
        await _q("INSERT INTO agent_shares(workspace_id, agent_id, principal_kind, principal_id, role, created_by) VALUES(%s,%s,'group',%s,%s,%s)"
                 " ON CONFLICT (agent_id, principal_kind, principal_id) DO UPDATE SET role=EXCLUDED.role", (x["agent"]["workspace_id"], aid, g["id"], b.role, u.get("id")))
    if b.username or b.user_id:
        t = await _q("SELECT u.id FROM users u JOIN memberships m ON m.user_id=u.id AND m.workspace_id=%s WHERE (u.username=%s OR u.id=%s) AND NOT u.disabled AND NOT u.demo",
                     (x["agent"]["workspace_id"], b.username or "", b.user_id or 0), one=True)
        if not t: raise HTTPException(404, "Không có người dùng này trong workspace / user not found in this workspace")
        if t["id"] == x["agent"]["owner_id"]: raise HTTPException(400, "Đây là chủ agent / this user owns the agent")
        if b.role == "edit" and not (x["owner"] or x["admin"] or x["edit"]): raise HTTPException(403, "Không đủ quyền / not allowed")
        await _q("INSERT INTO agent_shares(workspace_id, agent_id, principal_kind, principal_id, role, created_by) VALUES(%s,%s,'user',%s,%s,%s)"
                 " ON CONFLICT (agent_id, principal_kind, principal_id) DO UPDATE SET role=EXCLUDED.role", (x["agent"]["workspace_id"], aid, t["id"], b.role, u.get("id")))
    return await _list(aid, await access(aid))

@router.delete("/api/agents/{aid}/shares/{sid}")
async def shares_del(aid: int, sid: int):
    x = await access(aid)
    if not x["manage"]: raise HTTPException(403, "Không đủ quyền / not allowed")
    await _q("DELETE FROM agent_shares WHERE id=%s AND agent_id=%s", (sid, aid))
    try: return await _list(aid, await access(aid))
    except HTTPException: return {"ok": True}   # removed one's own share of a private agent

# ------------------------------------------------------------------ user groups (workspace admins; GĐ9.P1)
class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    members: list[int] = Field(default_factory=list, max_length=500)

class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    members: list[int] | None = Field(default=None, max_length=500)   # full replacement of the member list

async def _group_out(gid):
    g = await _q("SELECT id, name, description, created_at FROM user_groups WHERE id=%s", (gid,), one=True)
    if not g: raise HTTPException(404, "Không có nhóm này / group not found")
    g["members"] = await _q("SELECT u.id, u.username, u.display_name FROM user_group_members m JOIN users u ON u.id=m.user_id WHERE m.group_id=%s ORDER BY u.username", (gid,))
    g["shared_agents"] = (await _q("SELECT count(*) n FROM agent_shares WHERE principal_kind='group' AND principal_id=%s", (gid,), one=True))["n"]
    return g

async def _check_members(ws, ids):
    ok = {r["id"] for r in await _q("SELECT u.id FROM users u JOIN memberships m ON m.user_id=u.id AND m.workspace_id=%s WHERE u.id = ANY(%s) AND NOT u.demo", (ws, list(ids)))}
    bad = set(ids) - ok
    if bad: raise HTTPException(404, f"Người dùng không thuộc workspace / users not in this workspace: {sorted(bad)}")

async def _set_members(gid, ws, ids):
    await _check_members(ws, ids)
    await _q("DELETE FROM user_group_members WHERE group_id=%s AND user_id <> ALL(%s)", (gid, list(ids) or [0]))
    for i in ids: await _q("INSERT INTO user_group_members(group_id, user_id, workspace_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (gid, i, ws))

@router.get("/api/admin/groups")
async def groups_list():
    import admin
    rows = await _q("SELECT id FROM user_groups WHERE workspace_id=%s ORDER BY name", (admin.cur_ws(),))
    return [await _group_out(r["id"]) for r in rows]

@router.post("/api/admin/groups")
async def groups_create(b: GroupIn):
    import admin
    ws = admin.cur_ws()
    if await _q("SELECT 1 FROM user_groups WHERE workspace_id=%s AND lower(name)=lower(%s)", (ws, b.name.strip()), one=True):
        raise HTTPException(409, "Tên nhóm đã có / group name already exists")
    await _check_members(ws, list(dict.fromkeys(b.members)))   # before anything is written
    g = await _q("INSERT INTO user_groups(workspace_id, name, description, created_by) VALUES(%s,%s,%s,%s) RETURNING id", (ws, b.name.strip(), b.description, (admin.cur() or {}).get("id")), one=True)
    await _set_members(g["id"], ws, list(dict.fromkeys(b.members)))
    return await _group_out(g["id"])

@router.patch("/api/admin/groups/{gid}")
async def groups_update(gid: int, b: GroupPatch):
    import admin
    ws = admin.cur_ws(); await _group_out(gid)
    if b.members is not None: await _check_members(ws, list(dict.fromkeys(b.members)))
    if b.name is not None:
        if await _q("SELECT 1 FROM user_groups WHERE workspace_id=%s AND lower(name)=lower(%s) AND id<>%s", (ws, b.name.strip(), gid), one=True):
            raise HTTPException(409, "Tên nhóm đã có / group name already exists")
        await _q("UPDATE user_groups SET name=%s WHERE id=%s", (b.name.strip(), gid))
    if b.description is not None: await _q("UPDATE user_groups SET description=%s WHERE id=%s", (b.description, gid))
    if b.members is not None: await _set_members(gid, ws, list(dict.fromkeys(b.members)))
    return await _group_out(gid)

@router.delete("/api/admin/groups/{gid}")
async def groups_delete(gid: int):
    await _group_out(gid)
    await _q("DELETE FROM agent_shares WHERE principal_kind='group' AND principal_id=%s", (gid,))   # shares of a deleted group go with it
    await _q("DELETE FROM user_groups WHERE id=%s", (gid,))
    return {"deleted": gid}
