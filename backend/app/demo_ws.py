"""Demo workspace (21/10/2026, anh Huy): a customer trial account lives in its OWN workspace, so Row-Level Security hides every
trace, thread, run, feedback, file, memory, dataset and project of the owner's workspace. The chosen agents are cloned into it
(same chat profile, skills and fixed memory, Gemini-only models), with a small fictional sample CRM/Ads data set and only the
KB documents named here (the owner's private house notes are never copied).

POST /api/admin/demo-workspace  (admin only, idempotent)
  {"user_id": 82, "name": "Demo khách hàng", "source_agents": [2,106,...], "kb_titles": ["Acme Cloud support policy"], "daily_cap_usd": 2.0}
-> workspaces.demo=true (=> Gemini only for every call made there, gateway.gemini_only), the demo user becomes a viewer of that
   workspace ONLY (other memberships removed, open sessions moved), demo_agents = the cloned ids, the owner is admin there."""
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import admin as _adm
import deepagent_api as A
import gateway as gw

router = APIRouter()
DDL = "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS demo BOOLEAN NOT NULL DEFAULT false"
SAMPLE_DEALS = [("Công ty Sao Mai", "Gói văn phòng 2.500.000 VND/tháng", 30_000_000, "proposal"),
                ("Chị Hạnh (shop quà tặng)", "Drip bag box 10 gói x 50 hộp", 7_950_000, "qualified"),
                ("Anh Tuấn (quán cà phê)", "Hạt 500g x 20 túi", 5_780_000, "lead"),
                ("Văn phòng luật Minh An", "Gói văn phòng 6 tháng", 15_000_000, "won"),
                ("Chị Ngọc (đặt quà Tết)", "Drip bag Tết 2027, 30 hộp", 4_770_000, "lead")]
SAMPLE_ADS = [("Drip bag Tết 2027 - Facebook", "Facebook", 8_000_000, 182_000, 3_640, 91),
              ("Gói văn phòng - Google Search", "Google", 6_000_000, 41_500, 1_660, 37),
              ("Hạt rang mới - Zalo OA", "Zalo", 3_000_000, 58_200, 1_050, 22)]

async def init():
    await A.q(DDL)
    await load_demo_ws()

async def load_demo_ws():
    """Refresh gateway.DEMO_WS (system query, no RLS). Called at boot, by the run executor and after provisioning."""
    tok = _adm.USER.set(None)
    try:
        rows = await A.q("SELECT id FROM workspaces WHERE demo")
    finally:
        _adm.USER.reset(tok)
    gw.DEMO_WS.clear(); gw.DEMO_WS.update(r["id"] for r in rows)
    return gw.DEMO_WS

class DemoWsIn(BaseModel):
    user_id: int
    name: str = Field(default="Demo khách hàng", min_length=1, max_length=80)
    source_agents: list[int] = Field(min_length=1, max_length=20)
    kb_titles: list[str] = Field(default_factory=list, max_length=20)
    daily_cap_usd: float = Field(default=2.0, ge=0, le=20)

def _gem(m): return gw.DEMO_MODEL   # every demo agent / sub-agent runs on the latest priced Gemini model

def demo_config(cfg, idmap, cap):
    """Clone an agent config for the demo workspace: Gemini models only, sub-agent refs remapped, per-agent budget = the user cap."""
    c = dict(cfg)
    c["model"] = _gem(c.get("model")); c["fallback_models"] = list(gw.DEMO_FALLBACKS)
    c["budget_usd_day"] = cap
    subs = []
    for s in c.get("subagents") or []:
        s = dict(s)
        if s.get("model"): s["model"] = _gem(s["model"])
        if s.get("agent_ref") in idmap: s["agent_ref"] = idmap[s["agent_ref"]]
        subs.append(s)
    c["subagents"] = subs
    return c

async def provision(b: DemoWsIn, owner_id=None):
    tok = _adm.USER.set(None)   # system context ('*'): writes rows into another workspace than the caller's
    try:
        return await _provision(b, owner_id)
    finally:
        _adm.USER.reset(tok)

async def _provision(b, owner_id):
    u = await A.q("SELECT id, username, demo, is_superadmin FROM users WHERE id=%s", (b.user_id,), one=True)
    if not u: raise HTTPException(404, "Không có user")
    if not u["demo"] or u["is_superadmin"]: raise HTTPException(400, "Chỉ tài khoản demo (không phải chủ sở hữu) mới được chuyển sang workspace demo")
    await A.q(DDL)
    w = await A.q("INSERT INTO workspaces(name, demo) VALUES(%s, true) ON CONFLICT(name) DO UPDATE SET demo=true RETURNING id", (b.name,), one=True)
    ws = w["id"]
    if ws == 1: raise HTTPException(400, "Không dùng workspace mặc định làm demo")
    src = await A.q("""SELECT a.id, a.name, a.workspace_id, v.config FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version
                       WHERE a.id = ANY(%s) AND NOT a.deleted ORDER BY a.id""", (b.source_agents,))
    if len(src) != len(set(b.source_agents)): raise HTTPException(404, "Thiếu agent nguồn")
    src_ws = {r["workspace_id"] for r in src}
    if ws in src_ws: raise HTTPException(400, "Agent nguồn phải ở workspace khác")
    # 1) agents (reuse by name if already cloned -> idempotent)
    idmap, created = {}, []
    for r in src:
        ex = await A.q("SELECT id FROM agents WHERE workspace_id=%s AND name=%s AND NOT deleted", (ws, r["name"]), one=True)
        if ex: idmap[r["id"]] = ex["id"]; continue
        n = await A.q("INSERT INTO agents(name, current_version, workspace_id) VALUES(%s, 1, %s) RETURNING id", (r["name"], ws), one=True)
        idmap[r["id"]] = n["id"]; created.append(r["id"])
    # 2) versions (Gemini-only config) + system assistants with the same chat profile
    for r in src:
        nid = idmap[r["id"]]
        cfg = demo_config(r["config"], idmap, b.daily_cap_usd)
        cur = await A.q("SELECT config FROM agent_versions v JOIN agents a ON a.id=v.agent_id AND a.current_version=v.version WHERE a.id=%s", (nid,), one=True)
        if not cur:
            await A.q("INSERT INTO agent_versions(agent_id, version, config, note) VALUES(%s, 1, %s, %s)", (nid, Jsonb(cfg), f"demo clone of agent {r['id']} (Gemini only)"))
        elif cur["config"] != cfg:
            v = (await A.q("SELECT max(version)+1 v FROM agent_versions WHERE agent_id=%s", (nid,), one=True))["v"]
            await A.q("INSERT INTO agent_versions(agent_id, version, config, note) VALUES(%s, %s, %s, %s)", (nid, v, Jsonb(cfg), f"demo re-sync from agent {r['id']}"))
            await A.q("UPDATE agents SET current_version=%s WHERE id=%s", (v, nid))
        sa = await A.q("SELECT metadata, description FROM lg_assistants WHERE agent_id=%s AND metadata->>'created_by'='system' LIMIT 1", (r["id"],), one=True)
        md = {**((sa or {}).get("metadata") or {}), "created_by": "system", "chat_visible": True}
        if not await A.q("SELECT 1 FROM lg_assistants WHERE agent_id=%s AND metadata->>'created_by'='system'", (nid,), one=True):
            await A.q("INSERT INTO lg_assistants(assistant_id, graph_id, agent_id, name, description, metadata, workspace_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                      (str(uuid.uuid4()), f"agent-{nid}", nid, r["name"], (sa or {}).get("description"), Jsonb(md), ws))
    # 3) skills / fixed memory the agents reference (all published versions, same numbers so "name@N" pins stay valid)
    refs = set()
    for r in src:
        c = r["config"]
        for x in (c.get("skills") or []): refs.add(("skill", x.split("@")[0]))
        for x in (c.get("memory") or []): refs.add(("agent", x.split("@")[0]))
        for s in c.get("subagents") or []:
            for x in (s.get("skills") or []): refs.add(("skill", x.split("@")[0]))
            for x in (s.get("memory") or []): refs.add(("agent", x.split("@")[0]))
    nctx = 0
    for kind, name in sorted(refs):
        if await A.q("SELECT 1 FROM contexts WHERE workspace_id=%s AND kind=%s AND name=%s", (ws, kind, name), one=True): continue
        c = await A.q("SELECT * FROM contexts WHERE workspace_id = ANY(%s) AND kind=%s AND name=%s ORDER BY id LIMIT 1", (list(src_ws), kind, name), one=True)
        if not c: continue
        n = await A.q("""INSERT INTO contexts(workspace_id, kind, name, description, visibility, status, files, version) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                      (ws, kind, name, c["description"], c["visibility"], c["status"], Jsonb(c["files"]), c["version"]), one=True)
        await A.q("INSERT INTO context_versions(context_id, version, files, published) SELECT %s, version, files, published FROM context_versions WHERE context_id=%s",
                  (n["id"], c["id"])); nctx += 1
    # 4) KB: only the named (non-private) documents, chunks + embeddings copied (no embedding call, $0)
    nkb = 0
    for t in b.kb_titles:
        if await A.q("SELECT 1 FROM kb_docs WHERE workspace_id=%s AND title=%s", (ws, t), one=True): continue
        d = await A.q("SELECT * FROM kb_docs WHERE workspace_id = ANY(%s) AND title=%s ORDER BY id LIMIT 1", (list(src_ws), t), one=True)
        if not d: continue
        n = await A.q("INSERT INTO kb_docs(kb, title, n_chunks, workspace_id) VALUES(%s,%s,%s,%s) RETURNING id", (d["kb"], d["title"], d["n_chunks"], ws), one=True)
        await A.q("INSERT INTO kb_chunks(doc_id, idx, text, embedding) SELECT %s, idx, text, embedding FROM kb_chunks WHERE doc_id=%s", (n["id"], d["id"])); nkb += 1
    # 5) fictional sample CRM / Ads data so the CRM, Ads and Analytics agents have something to show (read-only for the demo)
    if not await A.q("SELECT 1 FROM crm_deals WHERE workspace_id=%s", (ws,), one=True):
        for cu, pr, v, st in SAMPLE_DEALS:
            await A.q("INSERT INTO crm_deals(workspace_id, customer, product, value_vnd, stage, note) VALUES(%s,%s,%s,%s,%s,'dữ liệu mẫu demo')", (ws, cu, pr, v, st))
    if not await A.q("SELECT 1 FROM ads_campaigns WHERE workspace_id=%s", (ws,), one=True):
        for nm, ch, bu, im, cl, cv in SAMPLE_ADS:
            await A.q("INSERT INTO ads_campaigns(workspace_id, name, channel, budget_vnd, audience, creative, impressions, clicks, conversions) VALUES(%s,%s,%s,%s,'dữ liệu mẫu','dữ liệu mẫu',%s,%s,%s)",
                      (ws, nm, ch, bu, im, cl, cv))
    await A.q("""INSERT INTO feedback_configs(workspace_id, key, type, min, max, categories, description)
                 SELECT %s, key, type, min, max, categories, description FROM feedback_configs WHERE workspace_id=1 AND key='helpful' ON CONFLICT DO NOTHING""", (ws,))
    # 6) the demo user: viewer of the demo workspace ONLY; owner keeps admin there to look in
    await A.q("DELETE FROM memberships WHERE user_id=%s AND workspace_id<>%s", (b.user_id, ws))
    await A.q("INSERT INTO memberships(user_id, workspace_id, role) VALUES(%s,%s,'viewer') ON CONFLICT(user_id, workspace_id) DO UPDATE SET role='viewer'", (b.user_id, ws))
    await A.q("UPDATE sessions SET workspace_id=%s WHERE user_id=%s", (ws, b.user_id))
    await A.q("UPDATE user_api_keys SET revoked=true WHERE user_id=%s AND workspace_id<>%s", (b.user_id, ws))
    await A.q("UPDATE users SET demo_agents=%s, daily_cap_usd=%s WHERE id=%s", ([idmap[i] for i in b.source_agents], b.daily_cap_usd, b.user_id))
    if owner_id:
        await A.q("INSERT INTO memberships(user_id, workspace_id, role) VALUES(%s,%s,'admin') ON CONFLICT DO NOTHING", (owner_id, ws))
    await load_demo_ws()
    return {"workspace_id": ws, "agents": {str(k): v for k, v in idmap.items()}, "cloned_now": created, "contexts": nctx, "kb_docs": nkb,
            "daily_cap_usd": b.daily_cap_usd, "demo_model": gw.DEMO_MODEL}

@router.post("/api/admin/demo-workspace")
async def demo_workspace(b: DemoWsIn):
    u = _adm.cur()
    if not u or u.get("role") != "admin" or _adm.is_demo(u): raise HTTPException(403, "admin only")
    if not await _adm._is_platform_owner(u["id"]): raise HTTPException(403, "Chỉ chủ sở hữu được dựng workspace demo")   # GĐ12.100b: it copies agents / KB / skills from ANY workspace and moves a user between workspaces
    return await provision(b, owner_id=u["id"])
