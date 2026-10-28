"""GĐ9.8 Privacy: per-workspace PII masking settings + data-subject rights (Luật Bảo vệ dữ liệu cá nhân, hiệu lực 1/1/2026).

  GET/PUT /api/privacy/settings        workspace masking config (PUT admin only): enabled, mode mask|hash|pseudonym, entities, threshold,
                                       apply_traces (mask at ingest, before storing), apply_gateway (mask before the model sees it), ner
  POST    /api/privacy/test            {text, [overrides]} -> masked text + findings (editor+; try the settings before saving)
  GET     /api/privacy/me/export       "Xuất dữ liệu của tôi": zip (JSON + uploaded files) of the caller's threads+messages, memories, files, projects,
                                       traces, canvas, code runs, usage counters, sessions/keys metadata, audit rows
  POST    /api/privacy/me/erase        "Xoá dữ liệu của tôi": {confirm: <username>}; cascades; account disabled + anonymised; sessions revoked
  GET     /api/admin/privacy/users/{uid}/export | POST .../erase   admin on request: same, limited to the admin's workspace
  GET     /api/privacy/requests        admin: log of export/erase requests (kept: the legally needed evidence, no content)
What is kept after erasure (anonymised): audit_log rows (username/ip removed, method/path/status/time kept), gateway_usage cost rows (user_id removed),
privacy_requests (who/when/counts, no content). Everything else owned by the user is deleted: see `_erase`."""
import asyncio, contextlib, datetime as dt, io, json, secrets, zipfile
from fastapi import APIRouter, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import admin as _adm
import deepagent_api as A
import pii

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS pii_settings(workspace_id INT PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT false, mode TEXT NOT NULL DEFAULT 'mask' CHECK (mode IN ('mask','hash','pseudonym')),
  entities TEXT[] NOT NULL DEFAULT '{}', threshold REAL NOT NULL DEFAULT 0.5, apply_traces BOOLEAN NOT NULL DEFAULT true, apply_gateway BOOLEAN NOT NULL DEFAULT true,
  ner BOOLEAN NOT NULL DEFAULT false, salt TEXT NOT NULL DEFAULT '', updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_by INT);
CREATE TABLE IF NOT EXISTS privacy_requests(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, kind TEXT NOT NULL CHECK (kind IN ('export','erase')),
  origin TEXT NOT NULL CHECK (origin IN ('self','admin')), subject_user_id INT NOT NULL, subject_label TEXT, requested_by INT, requested_by_name TEXT,
  status TEXT NOT NULL DEFAULT 'done', counts JSONB NOT NULL DEFAULT '{}', reason TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS privacy_requests_ws ON privacy_requests(workspace_id, created DESC);
"""

async def init(): await A.q(DDL)

def _me():
    u = _adm.cur()
    if not u: raise HTTPException(401, "login required")
    return u

@contextlib.contextmanager
def system_ctx():
    """Queries inside run as the system (RLS off): every statement below filters explicitly by user / workspace."""
    t1 = _adm.USER.set(None); t2 = _adm.WS_CTX.set(None)
    try: yield
    finally: _adm.WS_CTX.reset(t2); _adm.USER.reset(t1)

# ------------------------------------------------------------------ settings
class SettingsIn(BaseModel):
    enabled: bool = False
    mode: str = Field(default="mask", pattern=r"^(mask|hash|pseudonym)$")
    entities: list[str] = Field(default_factory=list, max_length=30)
    threshold: float = Field(default=0.5, ge=0.3, le=0.95)
    apply_traces: bool = True
    apply_gateway: bool = True
    ner: bool = False

def _catalogue():
    return [{"id": k, "tag": v[0], "label_vi": v[1], "label_en": v[2], "default": v[3]} for k, v in pii.ENTITIES.items() if k != "LOCATION" or pii.ner_available()]

async def _settings(ws) -> dict:
    row = await A.q("SELECT * FROM pii_settings WHERE workspace_id=%s", (ws,), one=True)
    base = {"enabled": False, "mode": "mask", "entities": pii.DEFAULT_ENTITIES, "threshold": 0.5, "apply_traces": True, "apply_gateway": True, "ner": False}
    if row: base.update({k: row[k] for k in base}); base["entities"] = list(row["entities"] or pii.DEFAULT_ENTITIES)
    return base

@router.get("/api/privacy/settings")
async def settings_get():
    u = _me(); import gateway as gw
    demo = int(u.get("ws") or 1) in gw.DEMO_WS or bool(u.get("demo"))
    return {**await _settings(_adm.cur_ws()), "catalogue": _catalogue(), "modes": list(pii.MODES), "ner_available": pii.ner_available(),
            "demo": demo, "can_edit": u.get("role") == "admin" and not demo}

@router.put("/api/privacy/settings")
async def settings_put(b: SettingsIn):
    u = _me(); ws = _adm.cur_ws(); import gateway as gw
    if u.get("role") != "admin": raise HTTPException(403, "Chỉ admin được đổi cài đặt che dữ liệu cá nhân")
    if ws in gw.DEMO_WS or u.get("demo"): raise HTTPException(403, "Workspace demo không bật được tính năng này")
    bad = [e for e in b.entities if e not in pii.ENTITIES]
    if bad: raise HTTPException(422, f"Loại dữ liệu không hợp lệ: {', '.join(bad[:5])}")
    if b.ner and not pii.ner_available(): raise HTTPException(422, "Chưa cài mô hình NER (xx_ent_wiki_sm); mô hình này tốn thêm ~300 MB RAM và nhận diện tên tiếng Việt kém hơn bộ quy tắc có sẵn")
    ents = b.entities or pii.DEFAULT_ENTITIES
    await A.q("""INSERT INTO pii_settings(workspace_id,enabled,mode,entities,threshold,apply_traces,apply_gateway,ner,salt,updated_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                 ON CONFLICT (workspace_id) DO UPDATE SET enabled=EXCLUDED.enabled, mode=EXCLUDED.mode, entities=EXCLUDED.entities, threshold=EXCLUDED.threshold,
                 apply_traces=EXCLUDED.apply_traces, apply_gateway=EXCLUDED.apply_gateway, ner=EXCLUDED.ner, updated_at=now(), updated_by=EXCLUDED.updated_by""",
              (ws, b.enabled, b.mode, ents, b.threshold, b.apply_traces, b.apply_gateway, b.ner, secrets.token_hex(16), u["id"]))
    pii.cache_clear(ws)
    if b.enabled: await asyncio.to_thread(pii.engine, b.ner)   # load the analyzer now, not inside the first user request
    await _audit(u, ws, "privacy: cập nhật cài đặt che dữ liệu cá nhân (" + ("bật" if b.enabled else "tắt") + f", chế độ {b.mode})")
    return await settings_get()

class TestIn(BaseModel):
    text: str = Field(max_length=20000)
    mode: str | None = Field(default=None, pattern=r"^(mask|hash|pseudonym)$")
    entities: list[str] | None = None
    threshold: float | None = Field(default=None, ge=0.3, le=0.95)

@router.post("/api/privacy/test")
async def privacy_test(b: TestIn):
    _me(); s = await _settings(_adm.cur_ws())
    cfg = {"mode": b.mode or s["mode"], "entities": [e for e in (b.entities or s["entities"]) if e in pii.ENTITIES] or pii.DEFAULT_ENTITIES,
           "threshold": b.threshold or s["threshold"], "ner": s["ner"], "salt": "preview"}
    pz = pii.Pseudonymizer() if cfg["mode"] == "pseudonym" else None
    out, fs = await asyncio.to_thread(pii.mask_text, b.text, cfg, pz, True)
    t = b.text
    return {"masked": out, "findings": [{"entity": f.entity, "tag": f.tag, "start": f.start, "end": f.end, "score": round(f.score, 2), "text": t[f.start:f.end]} for f in fs],
            "restored": pz.restore(out) if pz else None}

# ------------------------------------------------------------------ audit + request log
async def _audit(u, ws, detail, path="/api/privacy"):
    try:
        await A.q("INSERT INTO audit_log(user_id,username,workspace_id,auth,method,path,status,ms,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (u.get("id"), u.get("username"), ws, u.get("auth"), "PRIVACY", path, 200, 0, detail[:500]))
    except Exception as e:  # noqa: BLE001
        print("privacy audit error", e)

async def _log_request(kind, origin, ws, subject_id, label, actor, counts, reason=None):
    await A.q("INSERT INTO privacy_requests(workspace_id,kind,origin,subject_user_id,subject_label,requested_by,requested_by_name,counts,reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
              (ws, kind, origin, subject_id, label, actor.get("id"), actor.get("username"), Jsonb(counts), (reason or None) and reason[:500]))

@router.get("/api/privacy/requests")
async def requests_list():
    u = _me()
    if u.get("role") != "admin": raise HTTPException(403, "admin only")
    return await A.q("SELECT id, kind, origin, subject_user_id, subject_label, requested_by_name, status, counts, reason, created FROM privacy_requests WHERE workspace_id=%s ORDER BY id DESC LIMIT 200", (_adm.cur_ws(),))

# ------------------------------------------------------------------ export
FILE_BYTES_CAP = 25 * 1024 * 1024
_J = lambda o: json.dumps(jsonable_encoder(o), ensure_ascii=False, indent=1, default=str)   # noqa: E731

async def _export(uid: int, scope_ws) -> tuple[bytes, dict]:
    """zip of everything the user owns; scope_ws None = all workspaces. Returns (bytes, counts)."""
    import server as SV, attachments as AT
    W = "(%s::int IS NULL OR workspace_id=%s)"; wa = (scope_ws, scope_ws)
    z = io.BytesIO(); counts: dict = {}
    with system_ctx(), zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        user = await A.q("SELECT id, username, display_name, lang, created, last_login, demo, totp_enabled, instr_about, instr_style, instr_enabled FROM users WHERE id=%s", (uid,), one=True)
        if not user: raise HTTPException(404, "Không có người dùng này")
        prof = {**user, "memberships": await A.q("SELECT workspace_id, role FROM memberships WHERE user_id=%s", (uid,)),
                "api_keys": await A.q(f"SELECT name, prefix, scope, role, created, expires, last_used, revoked FROM user_api_keys WHERE user_id=%s AND {W}", (uid, *wa)),
                "sessions": await A.q("SELECT device, ip, created, last_seen, expires FROM sessions WHERE user_id=%s", (uid,)),
                "oauth_connections": await A.q(f"SELECT provider_id, account, scope, status, created FROM oauth_connections WHERE user_id=%s AND {W}", (uid, *wa)),
                "agent_shares": await A.q(f"SELECT agent_id, role, created_at FROM agent_shares WHERE principal_kind='user' AND principal_id=%s AND {W}", (uid, *wa))}
        zf.writestr("ho_so.json", _J(prof))
        threads = await A.q(f"SELECT thread_id, agent_id, metadata, status, workspace_id, created_at, updated_at FROM lg_threads WHERE owner_id=%s AND {W} ORDER BY created_at", (uid, *wa))
        out = []
        for t in threads:
            msgs = []
            if t["agent_id"]:
                try: msgs = ((await SV._state({"agent_id": t["agent_id"], "thread_id": t["thread_id"]})).get("values") or {}).get("messages") or []
                except Exception as e:  # noqa: BLE001
                    msgs = [{"error": f"không đọc được tin nhắn: {type(e).__name__}"}]
            out.append({**t, "messages": msgs})
        zf.writestr("cuoc_tro_chuyen.json", _J(out)); counts["threads"] = len(out); counts["messages"] = sum(len(x["messages"]) for x in out)
        mem = await A.q("SELECT prefix, key, value, created_at, updated_at FROM store WHERE prefix ~ %s OR prefix ~ %s", (rf"^agent\.[0-9]+\.u{uid}\.memories$", rf"^ws\.[0-9]+\.user\.{uid}(\.|$)"))
        if scope_ws is not None: mem = [m for m in mem if not m["prefix"].startswith("ws.") or m["prefix"].split(".")[1] == str(scope_ws)]
        zf.writestr("bo_nho.json", _J(mem)); counts["memories"] = len(mem)
        files = await A.q(f"SELECT id, sha256, mime_type, size, name, kind, enc, created_at FROM chat_files WHERE created_by=%s AND {W}", (uid, *wa))
        used = 0
        for f in files:
            try:
                if used + f["size"] > FILE_BYTES_CAP: f["omitted"] = "quá giới hạn dung lượng xuất"; continue
                zf.writestr(f"tep/{f['id']}_{(f['name'] or 'tep').replace('/', '_')[:80]}", AT._bytes(f)); used += f["size"]
            except Exception: f["omitted"] = "không đọc được tệp"  # noqa: BLE001
        zf.writestr("tep.json", _J(files)); counts["files"] = len(files)
        projs = await A.q(f"SELECT id, workspace_id, name, instructions, created_at FROM chat_projects WHERE owner_id=%s AND {W}", (uid, *wa))
        zf.writestr("du_an.json", _J(projs)); counts["projects"] = len(projs)
        runs = await A.q("""SELECT r.id, r.trace_id, r.name, r.run_type, r.start_time, r.status, r.inputs, r.outputs, r.error, r.model, r.input_tokens, r.output_tokens, r.cost_usd
                            FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE r.user_id=%s AND (%s::int IS NULL OR p.workspace_id=%s) ORDER BY r.start_time DESC LIMIT 20000""", (uid, *wa))
        zf.writestr("trace.json", _J(runs)); counts["trace_runs"] = len(runs)
        canvas = await A.q(f"SELECT doc_id, version, thread_id, title, kind, content, created FROM chat_canvas WHERE author_id=%s AND {W}", (uid, *wa))
        code = await A.q(f"SELECT thread_id, source, status, created FROM chat_code_runs WHERE user_id=%s AND {W}", (uid, *wa))
        zf.writestr("canvas_va_chay_ma.json", _J({"canvas": canvas, "code_runs": code})); counts["canvas"] = len(canvas); counts["code_runs"] = len(code)
        use = await A.q(f"SELECT count(*) calls, coalesce(sum(input_tokens),0) input_tokens, coalesce(sum(output_tokens),0) output_tokens, coalesce(sum(cost_usd),0) cost_usd, min(ts) first_call, max(ts) last_call FROM gateway_usage WHERE user_id=%s AND {W}", (uid, *wa), one=True)
        aud = await A.q(f"SELECT ts, method, path, status, ip, detail FROM audit_log WHERE user_id=%s AND {W} ORDER BY id DESC LIMIT 5000", (uid, *wa))
        zf.writestr("su_dung_va_nhat_ky.json", _J({"usage_summary": use, "audit": aud})); counts["audit_rows"] = len(aud)
        zf.writestr("DOC_TOI_DAY.txt", "Dữ liệu cá nhân của bạn trong Agent OS (xuất " + dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") + ").\n"
                    "Mỗi tệp .json là một nhóm dữ liệu; thư mục tep/ chứa các tệp bạn đã tải lên.\nKhông gồm: mật khẩu, khoá bí mật, token OAuth.\n")
    return z.getvalue(), counts

def _zip_response(data: bytes, username: str):
    name = f"du-lieu-cua-{username}-{dt.date.today():%Y%m%d}.zip"
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

@router.get("/api/privacy/me/export")
async def me_export():
    u = _me()
    data, counts = await _export(int(u["id"]), None)
    await _log_request("export", "self", _adm.cur_ws(), u["id"], u["username"], u, counts)
    await _audit(u, _adm.cur_ws(), "privacy: người dùng xuất dữ liệu của chính mình", "/api/privacy/me/export")
    return _zip_response(data, u["username"])

@router.get("/api/admin/privacy/users/{uid}/export")
async def admin_export(uid: int):
    u = _me(); ws = _adm.cur_ws(); target = await _member(uid, ws, u)
    data, counts = await _export(uid, ws)
    await _log_request("export", "admin", ws, uid, target["username"], u, counts)
    await _audit(u, ws, f"privacy: admin xuất dữ liệu của user #{uid} theo yêu cầu", f"/api/admin/privacy/users/{uid}/export")
    return _zip_response(data, target["username"])

async def _member(uid: int, ws, actor) -> dict:
    t = await A.q("SELECT u.id, u.username, u.is_superadmin FROM users u JOIN memberships m ON m.user_id=u.id AND m.workspace_id=%s WHERE u.id=%s", (ws, uid), one=True)
    if not t: raise HTTPException(404, "Người dùng không thuộc workspace này")
    return t

# ------------------------------------------------------------------ erase
class EraseIn(BaseModel):
    confirm: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=500)

async def _guard(uid: int, scope_ws) -> dict:
    t = await A.q("SELECT id, username, is_superadmin, demo FROM users WHERE id=%s", (uid,), one=True)
    if not t: raise HTTPException(404, "Không có người dùng này")
    if t["is_superadmin"]: raise HTTPException(409, "Không xoá được tài khoản chủ sở hữu (superadmin)")
    if t["demo"]: raise HTTPException(403, "Tài khoản demo không xoá dữ liệu được")
    for m in await A.q("SELECT workspace_id FROM memberships WHERE user_id=%s AND role='admin' AND (%s::int IS NULL OR workspace_id=%s)", (uid, scope_ws, scope_ws)):
        left = await A.q("SELECT count(*) n FROM memberships WHERE workspace_id=%s AND role='admin' AND user_id<>%s", (m["workspace_id"], uid), one=True)
        if left["n"] == 0: raise HTTPException(409, f"Người này là admin duy nhất của workspace #{m['workspace_id']}; hãy chuyển quyền admin trước")
    return t

async def _erase(uid: int, scope_ws, username: str) -> dict:
    """Delete everything the user owns (scope_ws None = every workspace) and anonymise what must stay. Returns counts."""
    import server as SV, attachments as AT
    c: dict = {}; W = "(%s::int IS NULL OR workspace_id=%s)"; wa = (scope_ws, scope_ws)
    async def dele(key, sql, args=()):
        r = await A.q(sql + " RETURNING 1", args); c[key] = len(r or [])
        return r
    with system_ctx():
        threads = await A.q(f"SELECT * FROM lg_threads WHERE owner_id=%s AND {W}", (uid, *wa))
        for t in threads:
            try: await SV._delete_thread(t)   # cancels runs, drops the checkpoint, then the lg_threads row (cascades chat_fts / canvas / shares)
            except Exception as e: print("privacy: delete thread", t["thread_id"], type(e).__name__)  # noqa: BLE001
        tids = [t["thread_id"] for t in threads]; c["threads"] = len(tids)
        if tids:
            runs = await A.q("DELETE FROM lg_runs WHERE thread_id = ANY(%s) RETURNING run_id", (tids,)); c["runs"] = len(runs)
            if runs: await A.q("DELETE FROM lg_run_events WHERE run_id = ANY(%s)", ([r["run_id"] for r in runs],))
            await A.q("DELETE FROM channel_threads WHERE thread_id = ANY(%s)", (tids,))
            await A.q("DELETE FROM chat_thread_files WHERE thread_id = ANY(%s)", (tids,))
            await A.q("DELETE FROM approvals WHERE thread_id = ANY(%s)", (tids,))
            await A.q("DELETE FROM lg_threads WHERE thread_id = ANY(%s)", (tids,))
        tr = await A.q("""DELETE FROM tr_runs r USING projects p WHERE p.id=r.project_id AND r.user_id=%s AND (%s::int IS NULL OR p.workspace_id=%s) RETURNING r.trace_id""", (uid, *wa))
        trace_ids = list({x["trace_id"] for x in tr}); c["trace_runs"] = len(tr)
        if trace_ids:
            await A.q("DELETE FROM run_attachments WHERE trace_id = ANY(%s)", (trace_ids,))
            await A.q("DELETE FROM feedback WHERE trace_id = ANY(%s)", (trace_ids,))
            await A.q("DELETE FROM shares WHERE trace_id = ANY(%s)", (trace_ids,))
        mem = await A.q("DELETE FROM store WHERE (prefix ~ %s OR prefix ~ %s) AND (%s::int IS NULL OR prefix !~ '^ws\\.' OR split_part(prefix,'.',2)=%s::text) RETURNING 1",
                        (rf"^agent\.[0-9]+\.u{uid}\.memories$", rf"^ws\.[0-9]+\.user\.{uid}(\.|$)", scope_ws, scope_ws)); c["memories"] = len(mem)
        files = await A.q(f"DELETE FROM chat_files WHERE created_by=%s AND {W} RETURNING id, sha256", (uid, *wa)); c["files"] = len(files)
        if files:
            await A.q("DELETE FROM chat_thread_files WHERE file_id = ANY(%s)", ([f["id"] for f in files],))
            for f in files:   # 9.P2: tệp mã hoá nằm ở var/files/_enc/<id>
                with contextlib.suppress(OSError): __import__("aos_crypto").enc_path(f["id"]).unlink()
            for sha in {f["sha256"] for f in files}:   # content-addressed: keep the bytes while another row still points at them
                if not await A.q("SELECT 1 FROM chat_files WHERE sha256=%s LIMIT 1", (sha,), one=True):
                    with contextlib.suppress(OSError): (AT.DIR / sha[:2] / sha).unlink()
        await dele("projects", f"DELETE FROM chat_projects WHERE owner_id=%s AND {W}", (uid, *wa))
        await dele("kb_docs", f"DELETE FROM kb_docs WHERE user_id=%s AND {W}", (uid, *wa))
        await dele("code_runs", f"DELETE FROM chat_code_runs WHERE user_id=%s AND {W}", (uid, *wa))
        await dele("oauth_connections", f"DELETE FROM oauth_connections WHERE user_id=%s AND {W}", (uid, *wa))
        await dele("agent_shares", f"DELETE FROM agent_shares WHERE principal_kind='user' AND principal_id=%s AND {W}", (uid, *wa))
        await dele("api_keys", f"DELETE FROM user_api_keys WHERE user_id=%s AND {W}", (uid, *wa))
        pr = await __import__("sbx_runtime").purge_for_user(uid, scope_ws)   # GĐ11.42: containers + volumes + folder + rows (it used to delete only the rows)
        c["sandboxes"] = pr["boxes"]
        if pr["errors"]: c["sandbox_purge_errors"] = len(pr["errors"])
        await A.q(f"UPDATE gateway_usage SET user_id=NULL WHERE user_id=%s AND {W}", (uid, *wa))   # cost rows stay (accounting), the link to the person goes
        await A.q(f"UPDATE chat_canvas SET author_id=NULL, author='đã xoá' WHERE author_id=%s AND {W}", (uid, *wa))
        await A.q("UPDATE feedback SET created_by=NULL WHERE created_by=%s", (username,))
        # account: leaves the workspace(s); with no membership left the login itself is anonymised + disabled
        await A.q("DELETE FROM memberships WHERE user_id=%s AND (%s::int IS NULL OR workspace_id=%s)", (uid, *wa))
        left = await A.q("SELECT count(*) n FROM memberships WHERE user_id=%s", (uid,), one=True)
        c["account_anonymised"] = left["n"] == 0
        if left["n"] == 0:
            label = f"da-xoa-{uid}-{secrets.token_hex(3)}"
            await A.q("DELETE FROM sessions WHERE user_id=%s", (uid,))
            await A.q("""UPDATE users SET username=%s, display_name=NULL, pw_hash=%s, disabled=true, totp_secret_enc=NULL, totp_enabled=false, totp_recovery='{}',
                         instr_about='', instr_style='', instr_enabled=false, daily_cap_usd=NULL, demo_agents=NULL WHERE id=%s""", (label, "erased:" + secrets.token_hex(16), uid))
            await A.q("UPDATE audit_log SET username=%s, ip=NULL, detail=CASE WHEN detail ILIKE 'privacy:%%' THEN detail ELSE NULL END WHERE user_id=%s", (label, uid))
            c["subject_label"] = label
        else:
            await A.q("DELETE FROM sessions WHERE user_id=%s AND workspace_id = %s", (uid, scope_ws))
            await A.q("UPDATE audit_log SET ip=NULL, detail=CASE WHEN detail ILIKE 'privacy:%%' THEN detail ELSE NULL END WHERE user_id=%s AND workspace_id=%s", (uid, scope_ws))
    return c

@router.post("/api/privacy/me/erase")
async def me_erase(b: EraseIn):
    u = _me()
    if u.get("demo"): raise HTTPException(403, "Tài khoản demo không xoá dữ liệu được")
    if b.confirm.strip() != u["username"]: raise HTTPException(422, "Xác nhận chưa đúng: gõ chính xác tên đăng nhập của bạn")
    with system_ctx(): await _guard(int(u["id"]), None)
    ws = _adm.cur_ws(); uid = int(u["id"])
    counts = await _erase(uid, None, u["username"])
    await _log_request("erase", "self", ws, uid, counts.get("subject_label", u["username"]), u, counts, b.reason)
    await _audit({"id": uid, "username": counts.get("subject_label", "da-xoa"), "auth": u.get("auth")}, ws, "privacy: người dùng tự xoá dữ liệu của mình", "/api/privacy/me/erase")
    return {"ok": True, "counts": counts, "message": "Đã xoá dữ liệu của bạn và khoá tài khoản. Bạn sẽ bị đăng xuất."}

@router.post("/api/admin/privacy/users/{uid}/erase")
async def admin_erase(uid: int, b: EraseIn):
    u = _me(); ws = _adm.cur_ws(); target = await _member(uid, ws, u)
    if b.confirm.strip() != target["username"]: raise HTTPException(422, "Xác nhận chưa đúng: gõ chính xác tên đăng nhập của người dùng")
    if uid == u["id"]: raise HTTPException(409, "Dùng chức năng 'Xoá dữ liệu của tôi' cho chính mình")
    with system_ctx(): await _guard(uid, ws)
    counts = await _erase(uid, ws, target["username"])
    await _log_request("erase", "admin", ws, uid, counts.get("subject_label", target["username"]), u, counts, b.reason)
    await _audit(u, ws, f"privacy: admin xoá dữ liệu của user #{uid} theo yêu cầu" + (f" ({b.reason})" if b.reason else ""), f"/api/admin/privacy/users/{uid}/erase")
    return {"ok": True, "counts": counts}

def setup():
    _adm.allow_viewer_post("/api/privacy/me/", "/api/privacy/test")   # every member may export / erase his own data; the handlers check identity
