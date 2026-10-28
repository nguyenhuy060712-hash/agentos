"""GĐ11.12 the ONE list of features that call a model, and which providers / models each may offer (table model_feature_access, no row = no restriction).
`allowed(feature, ref)` is what the 11.14 policy engine will call. `catalog(feature)` = the shared model list used by the chat picker, Playground and judge selects:
models of enabled providers (+ saved configurations custom/x and tiers aos/x), cut down by the feature's row. A provider is "off" when the workspace has a gw_providers row of that name
with enabled=false; a provider with no row and no .env key is simply not offered in the catalog. Demo workspace: Gemini only, no changes."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import gw_router

router = APIRouter()
# feature -> (vi, en). Keep in step with the design (5.2a). The model-access API refuses any other name.
FEATURES = {
    "playground": ("Playground", "Playground"), "evaluator": ("Giám khảo (evaluator)", "Evaluator (judge)"), "online_eval": ("Chấm điểm trực tuyến", "Online evaluation"),
    "decision": ("Mô hình quyết định", "Decision model"), "fleet": ("Đội agent", "Agent fleet"), "chat": ("Chat", "Chat"), "chat_title": ("Đặt tên cuộc chat", "Chat titles"),
    "assistant": ("Trợ lý", "Assistant"), "builder": ("Tạo agent", "Agent builder"), "custom_app": ("Ứng dụng tùy chỉnh", "Custom apps"),
    "insights_thinking": ("Insights - suy luận", "Insights - thinking"), "insights_summary": ("Insights - tóm tắt", "Insights - summary"), "engine": ("Engine", "Engine"),
    "ai_search": ("Tìm kiếm AI", "AI search"), "synthetic": ("Dữ liệu tổng hợp", "Synthetic data"), "pairwise": ("So sánh cặp", "Pairwise comparison"),
    "skill_draft": ("Soạn skill", "Skill drafting"), "prompt_optimize": ("Tối ưu prompt", "Prompt optimizer"), "mail_trigger": ("Kích hoạt từ thư", "Mail trigger"),
}
ALIASES = {"search": "ai_search", "insights": "insights_summary"}   # cost-feature names in use today (gateway_usage.feature): no 10.11 rename
LABEL_VI = {f: v[0] for f, v in FEATURES.items()}

try:   # one list: gateway.FEATURE_VI (402 texts) also knows the new names; existing labels are kept
    import gateway as _gw
    for _f, _l in LABEL_VI.items(): _gw.FEATURE_VI.setdefault(_f, _l)
except Exception: pass   # noqa: BLE001

def norm(feature):
    f = ALIASES.get(feature, feature)
    if f not in FEATURES: raise HTTPException(400, "Tính năng không có: " + str(feature)[:40])
    return f

def _admin():
    import admin, gateway
    u = admin.cur()
    if not u or u.get("role") != "admin" or u.get("demo") or int(u["ws"]) in gateway.DEMO_WS: raise HTTPException(403, "Chỉ admin (không phải demo) chỉnh quyền dùng mô hình")
    return u

async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

async def access_row(ws, feature):
    import admin
    async with admin.as_workspace(int(ws)):
        return await _q("SELECT feature, providers, models, default_ref FROM model_feature_access WHERE workspace_id=%s AND feature=%s", (int(ws), norm(feature)), one=True)

def _pass(row, r, ref):
    """Does the feature row let resolved model `r` (asked for as `ref`) through?"""
    if not row: return True
    if row.get("providers") and r.provider not in row["providers"]: return False
    ms = row.get("models") or []
    return not ms or ref in ms or r.ref in ms

async def allowed(feature, ref, ws=None):
    """True if `ref` may be used by `feature` in the workspace: provider not switched off, provider / model inside the feature's row.
    Unknown feature -> HTTP 400; unresolvable ref -> False."""
    import admin
    ws = int(ws or admin.cur_ws()); f = norm(feature)
    rows = await gw_router.rows_for(ws)
    try: r = gw_router.resolve(ref, ws, rows)
    except gw_router.ResolveError: return False
    p = rows.get(r.provider)
    if p is not None and not p["enabled"]: return False
    return _pass(await access_row(ws, f), r, ref)

async def filter_refs(feature, refs, ws=None):
    """-> (the refs the feature may use, order kept; the feature's default_ref if it survived else None)."""
    import admin
    ws = int(ws or admin.cur_ws()); f = norm(feature)
    rows = await gw_router.rows_for(ws); row = await access_row(ws, f); out = []
    for m in refs:
        try: r = gw_router.resolve(m, ws, rows)
        except gw_router.ResolveError: continue
        p = rows.get(r.provider)
        if (p is None or p["enabled"]) and _pass(row, r, m): out.append(m)
    d = (row or {}).get("default_ref")
    return out, (d if d in out else None)

def _provider_on(rows, name):
    """Is provider `name` offered in the catalog? A workspace row decides; else the two .env providers."""
    import gw_providers
    if name in rows: return bool(rows[name]["enabled"])
    return any(s["name"] == name for s in gw_providers.system_providers())

async def catalog(feature=None, ws=None, admin_view=False):
    import admin, deepagent_api as A, gateway as gw
    ws = int(ws or admin.cur_ws()); f = norm(feature) if feature else None
    rows = await gw_router.rows_for(ws)
    if "list" not in A._models_cache:
        try: await A.models()
        except Exception: pass   # noqa: BLE001
    ids = list(dict.fromkeys(list(A._models_cache.get("list") or []) + list(gw.PRICING)))
    items = [(m, "model", None) for m in ids] + [(f"aos/{t}", "tier", None) for t in gw_router.TIERS]
    async with admin.as_workspace(ws):
        cfgs = await _q("SELECT name, is_default FROM model_presets WHERE workspace_id=%s ORDER BY name", (ws,))
    items += [(f"custom/{c['name']}", "preset", c) for c in cfgs]
    row = await access_row(ws, f) if f else None
    demo = gw.gemini_only(); out = []
    for ref, typ, c in items:
        try: r = gw_router.resolve(ref, ws, rows)
        except gw_router.ResolveError: continue
        if not _provider_on(rows, r.provider) or (demo and r.kind != "gemini") or not _pass(row, r, ref): continue
        pr = rows.get(r.provider); it = {"id": ref, "type": typ, "provider": r.provider, "kind": r.kind, "model": r.model}
        if typ == "preset": it["is_default"] = bool(c["is_default"])
        if admin_view and pr is not None and not pr.get("secret_name") and r.kind != "openai_compat": it["no_key"] = True
        out.append(it)
    d = (row or {}).get("default_ref")
    return {"feature": f, "models": out, "default": d if d in {o["id"] for o in out} else None}

class AccessIn(BaseModel):
    providers: list[str] | None = Field(default=None, max_length=20)
    models: list[str] | None = Field(default=None, max_length=200)
    default_ref: str | None = Field(default=None, max_length=120)

@router.get("/api/gw/features")
async def features_list():
    return {"features": [{"id": f, "vi": v[0], "en": v[1]} for f, v in FEATURES.items()], "aliases": ALIASES}

@router.get("/api/gw/catalog")
async def catalog_get(feature: str | None = None):
    import admin
    u = admin.cur() or {}
    return await catalog(feature, admin_view=u.get("role") == "admin" and not u.get("demo"))

@router.get("/api/model_feature_access")
async def access_list():
    u = _admin(); ws = int(u["ws"])
    got = {r["feature"]: r for r in await _q("SELECT feature, providers, models, default_ref FROM model_feature_access WHERE workspace_id=%s", (ws,))}
    return {"features": [{"id": f, "vi": v[0], "en": v[1], **{k: (got.get(f) or {}).get(k) for k in ("providers", "models", "default_ref")}} for f, v in FEATURES.items()]}

@router.put("/api/model_feature_access/{feature}")
async def access_put(feature: str, b: AccessIn):
    u = _admin(); ws = int(u["ws"]); f = norm(feature)
    rows = await gw_router.rows_for(ws, fresh=True)
    for ref in list(b.models or []) + ([b.default_ref] if b.default_ref else []):
        try: gw_router.resolve(ref, ws, rows)
        except gw_router.ResolveError as e: raise HTTPException(400, str(e)) from None
    if not (b.providers or b.models or b.default_ref):   # empty = back to "no restriction"
        await _q("DELETE FROM model_feature_access WHERE workspace_id=%s AND feature=%s", (ws, f))
        return {"feature": f, "providers": None, "models": None, "default_ref": None}
    return await _q("""INSERT INTO model_feature_access(workspace_id,feature,providers,models,default_ref,updated_by) VALUES(%s,%s,%s,%s,%s,%s)
      ON CONFLICT(workspace_id,feature) DO UPDATE SET providers=EXCLUDED.providers, models=EXCLUDED.models, default_ref=EXCLUDED.default_ref, updated_by=EXCLUDED.updated_by, updated_at=now()
      RETURNING feature, providers, models, default_ref""", (ws, f, b.providers or None, b.models or None, b.default_ref, u.get("username")), one=True)
