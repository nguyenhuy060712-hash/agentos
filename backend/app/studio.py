"""P3 Prompt engineering + P4 Builder/Fleet/Studio (tự viết, dựa trên mô tả tính năng công khai).
P3: Prompt Hub (commit/tag/diff/webhook), playground nhiều model song song, chạy playground trên dataset, cải thiện prompt bằng AI (canvas),
    gắn prompt vào agent (AgentCfg.prompt_ref).
P4: tạo agent từ mô tả ngôn ngữ tự nhiên, template, duyệt thủ công (HITL interrupt) trước thao tác ghi, agent chạy theo cron, webhook trigger,
    Studio: đồ thị agent, chạy từng bước, lịch sử checkpoint, sửa state, rẽ nhánh / time-travel."""
import asyncio, difflib, hashlib, json, os, re, time, uuid
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import deepagent_api as A
import obs

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS prompt_tags(prompt_id INT REFERENCES prompts(id) ON DELETE CASCADE, tag TEXT NOT NULL, version INT NOT NULL,
  updated TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(prompt_id, tag));
ALTER TABLE prompt_versions ADD COLUMN IF NOT EXISTS parent_version INT;
ALTER TABLE prompt_versions ADD COLUMN IF NOT EXISTS output_schema JSONB;
CREATE TABLE IF NOT EXISTS hub_webhooks(id SERIAL PRIMARY KEY, url TEXT NOT NULL, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS approvals(id SERIAL PRIMARY KEY, agent_id INT, version INT, thread_id TEXT, payload JSONB, trace_id UUID,
  status TEXT NOT NULL DEFAULT 'pending', decision JSONB, result TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now(), decided TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS agent_crons(id SERIAL PRIMARY KEY, agent_id INT REFERENCES agents(id) ON DELETE CASCADE, name TEXT NOT NULL, cron TEXT NOT NULL,
  message TEXT NOT NULL, new_thread BOOLEAN NOT NULL DEFAULT true, enabled BOOLEAN NOT NULL DEFAULT true, last_run TIMESTAMPTZ, last_trace UUID, last_status TEXT,
  created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS agent_hooks(id SERIAL PRIMARY KEY, agent_id INT REFERENCES agents(id) ON DELETE CASCADE, name TEXT, token_hash TEXT UNIQUE NOT NULL,
  prefix TEXT, enabled BOOLEAN NOT NULL DEFAULT true, calls INT NOT NULL DEFAULT 0, last_call TIMESTAMPTZ, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS hook_calls(id BIGSERIAL PRIMARY KEY, hook_id INT REFERENCES agent_hooks(id) ON DELETE CASCADE, thread_id TEXT, status TEXT,
  trace_id UUID, reply TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now());
"""
JOBS = {}

async def init():
    await A.q(DDL)

# ============================================================ P3 Prompt Hub
async def resolve_prompt(ref: str):
    """'tên' | 'tên:tag' | 'tên:số' → dòng prompt_versions (mới nhất nếu không chỉ định)."""
    name, _, sel = ref.partition(":")
    p = await A.q("SELECT id FROM prompts WHERE name=%s", (name.strip(),), one=True)
    if not p: return None
    if sel.isdigit():
        return await A.q("SELECT * FROM prompt_versions WHERE prompt_id=%s AND version=%s", (p["id"], int(sel)), one=True)
    if sel:
        t = await A.q("SELECT version FROM prompt_tags WHERE prompt_id=%s AND tag=%s", (p["id"], sel), one=True)
        if not t: return None
        return await A.q("SELECT * FROM prompt_versions WHERE prompt_id=%s AND version=%s", (p["id"], t["version"]), one=True)
    return await A.q("SELECT * FROM prompt_versions WHERE prompt_id=%s ORDER BY version DESC LIMIT 1", (p["id"],), one=True)

def _invalidate_agents(): A.S["agents"].clear()

class Commit(BaseModel):
    name: str = Field(pattern=r"^[\w\-. ]{1,80}$")
    template: str = Field(min_length=1, max_length=50000)
    model: str = A.DEFAULT_MODEL
    temperature: float = Field(default=0.3, ge=0, le=2)
    variables: dict = {}
    output_schema: dict | None = None
    message: str | None = Field(default=None, max_length=500)
    parent_version: int | None = None
    tag: str | None = Field(default=None, pattern=r"^[\w\-.]{1,40}$")
    template_format: str = Field(default="mustache", pattern=r"^(mustache|f-string|jinja2)$")

async def _fire_hub_hooks(name, version, message):
    try: rows = await A.q("SELECT url, headers, workspace_id FROM hub_webhooks WHERE 'commit' = ANY(triggers) AND (prompt_id IS NULL OR prompt_id=(SELECT id FROM prompts WHERE name=%s LIMIT 1))", (name,))
    except Exception: rows = await A.q("SELECT url, workspace_id FROM hub_webhooks")  # before the ui_api migration ran
    for h in rows:
        await obs.deliver("webhook", h["url"], f"Prompt Hub: {name} v{version}", json.dumps({"prompt": name, "version": version, "message": message}, ensure_ascii=False), headers=h.get("headers") or None, ws_id=h.get("workspace_id"))

@router.post("/api/hub/commit")
async def hub_commit(c: Commit):
    r = await A.prompt_save(A.PromptSave(name=c.name, template=c.template, model=c.model, temperature=c.temperature, variables=c.variables, note=c.message))
    await A.q("UPDATE prompt_versions SET parent_version=%s, output_schema=%s WHERE prompt_id=%s AND version=%s",
              (c.parent_version or (r["version"] - 1 or None), Jsonb(c.output_schema) if c.output_schema else None, r["id"], r["version"]))
    if c.template_format != "mustache": await A.q("UPDATE prompt_versions SET template_format=%s WHERE prompt_id=%s AND version=%s", (c.template_format, r["id"], r["version"]))
    if c.tag: await hub_tag(r["id"], TagIn(tag=c.tag, version=r["version"]))
    await _fire_hub_hooks(c.name, r["version"], c.message); _invalidate_agents()
    return r

@router.get("/api/hub")
async def hub_list():
    return await A.q("""SELECT p.id, p.name, max(v.version) latest, max(v.created) updated, count(v.*) commits,
        (SELECT json_agg(json_build_object('tag',t.tag,'version',t.version)) FROM prompt_tags t WHERE t.prompt_id=p.id) tags
        FROM prompts p JOIN prompt_versions v ON v.prompt_id=p.id GROUP BY p.id ORDER BY updated DESC""")

@router.get("/api/hub/pull")
async def hub_pull(ref: str):
    pv = await resolve_prompt(ref)
    if not pv: raise HTTPException(404, "Không có prompt/tag/phiên bản")
    return pv

@router.get("/api/hub/{pid}")
async def hub_get(pid: int):
    p = await A.q("SELECT * FROM prompts WHERE id=%s", (pid,), one=True)
    if not p: raise HTTPException(404)
    vs = await A.q("SELECT * FROM prompt_versions WHERE prompt_id=%s ORDER BY version DESC", (pid,))
    tags = await A.q("SELECT tag, version, updated FROM prompt_tags WHERE prompt_id=%s ORDER BY tag", (pid,))
    agents = await A.q("""SELECT a.id, a.name, v.config->>'prompt_ref' ref FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version
        WHERE split_part(v.config->>'prompt_ref', ':', 1) = %s""", (p["name"],))
    return {"prompt": p, "versions": vs, "tags": tags, "used_by": agents}

class TagIn(BaseModel):
    tag: str = Field(pattern=r"^[\w\-.]{1,40}$")
    version: int

@router.post("/api/hub/{pid}/tags")
async def hub_tag(pid: int, t: TagIn):
    if not await A.q("SELECT 1 FROM prompt_versions WHERE prompt_id=%s AND version=%s", (pid, t.version)): raise HTTPException(404, "Không có phiên bản")
    await A.q("INSERT INTO prompt_tags(prompt_id,tag,version) VALUES(%s,%s,%s) ON CONFLICT(prompt_id,tag) DO UPDATE SET version=EXCLUDED.version, updated=now()", (pid, t.tag, t.version))
    _invalidate_agents()
    try:
        for h in await A.q("SELECT url, headers, workspace_id FROM hub_webhooks WHERE 'tag' = ANY(triggers) AND (prompt_id IS NULL OR prompt_id=%s)", (pid,)):
            await obs.deliver("webhook", h["url"], f"Prompt Hub tag: {t.tag} -> v{t.version}", json.dumps({"prompt_id": pid, "tag": t.tag, "version": t.version}), headers=h.get("headers") or None, ws_id=h.get("workspace_id"))
    except Exception: pass
    return {"ok": True}

@router.delete("/api/hub/{pid}/tags/{tag}")
async def hub_untag(pid: int, tag: str):
    await A.q("DELETE FROM prompt_tags WHERE prompt_id=%s AND tag=%s", (pid, tag)); _invalidate_agents(); return {"ok": True}

@router.get("/api/hub/{pid}/diff")
async def hub_diff(pid: int, a: int, b: int):
    va = await A.q("SELECT template, model, temperature FROM prompt_versions WHERE prompt_id=%s AND version=%s", (pid, a), one=True)
    vb = await A.q("SELECT template, model, temperature FROM prompt_versions WHERE prompt_id=%s AND version=%s", (pid, b), one=True)
    if not va or not vb: raise HTTPException(404)
    d = list(difflib.unified_diff(va["template"].splitlines(), vb["template"].splitlines(), f"v{a}", f"v{b}", lineterm=""))
    return {"diff": "\n".join(d), "settings": {"a": {k: va[k] for k in ("model", "temperature")}, "b": {k: vb[k] for k in ("model", "temperature")}}}

class HookIn(BaseModel):
    url: str = Field(pattern=r"^https?://")
@router.post("/api/hub/webhooks")
async def hub_hook_new(h: HookIn): return await A.q("INSERT INTO hub_webhooks(url) VALUES(%s) RETURNING id", (h.url,), one=True)
@router.get("/api/hub/webhooks/list")
async def hub_hook_list(): return await A.q("SELECT * FROM hub_webhooks ORDER BY id")
@router.delete("/api/hub/webhooks/{hid}")
async def hub_hook_del(hid: int): await A.q("DELETE FROM hub_webhooks WHERE id=%s", (hid,)); return {"ok": True}

# ---- playground nhiều model (so sánh song song, không fallback để so công bằng)
class MultiReq(BaseModel):
    template: str = Field(min_length=1, max_length=50000)
    variables: dict = {}
    models: list[str] = Field(min_length=1, max_length=4)
    template_format: str = Field(default="mustache", pattern=r"^(mustache|f-string|jinja2)$")
    temperature: float = Field(default=0.3, ge=0, le=2)
    output_schema: dict | None = None
    prompt_ref: str | None = None

async def _one_model(prompt, model, temp, schema, ref):
    t0 = time.time(); err = txt = None; i = o = 0
    p2 = prompt + (("\n\nTrả về JSON đúng schema sau:\n" + json.dumps(schema, ensure_ascii=False)) if schema else "")
    try:
        txt, m, i, o = await A.gw.generate(A._g, p2, [model], json_mode=bool(schema), temperature=temp, feature="playground")
    except Exception as e:  # noqa: BLE001
        err = str(e)[:300]
    valid = None
    if schema and txt:
        try:
            d = json.loads(txt); req = schema.get("required") or []
            valid = isinstance(d, dict) and all(k in d for k in req)
        except Exception: valid = False  # noqa: BLE001
    ms = int((time.time() - t0) * 1000); rid = str(uuid.uuid4())
    await obs.upsert_run(await obs.project_id("playground"), {"id": rid, "name": f"playground · {model}", "run_type": "llm", "start_time": t0, "end_time": time.time(),
                         "inputs": {"prompt": p2}, "outputs": {"answer": txt}, "error": err, "model": model, "usage": {"input_tokens": i, "output_tokens": o},
                         "tags": ["playground"] + ([f"prompt:{ref}"] if ref else []), "metadata": {"temperature": temp}})
    return {"model": model, "output": txt, "error": err, "ms": ms, "input_tokens": i, "output_tokens": o, "cost_usd": A.gw.cost(model, i, o) if not err else 0,
            "schema_valid": valid, "run_id": rid}

@router.post("/api/playground/multi")
async def play_multi(r: MultiReq):
    import ext
    prompt = await ext.render_async(r.template, r.variables, getattr(r, "template_format", None))
    res = await asyncio.gather(*(_one_model(prompt, m, r.temperature, r.output_schema, r.prompt_ref) for m in r.models))
    return {"rendered": prompt, "results": res}

class BatchReq(BaseModel):
    prompt_id: int
    version: int | None = None
    dataset_id: int
    model: str | None = None
    evaluator_ids: list[int] = []
    split: str | None = None

@router.post("/api/playground/batch")
async def play_batch(b: BatchReq):
    """Chạy prompt trên cả dataset = tạo experiment target prompt (Run evaluation from Playground)."""
    import evals
    p = await A.q("SELECT name FROM prompts WHERE id=%s", (b.prompt_id,), one=True)
    if not p: raise HTTPException(404)
    eid = await evals.start_experiment(evals.ExpIn(name=f"Playground · {p['name']}" + (f"@v{b.version}" if b.version else "") + (f" · {b.model}" if b.model else ""),
                                                   dataset_id=b.dataset_id, split=b.split, evaluator_ids=b.evaluator_ids,
                                                   target={"type": "prompt", "prompt_id": b.prompt_id, "version": b.version, "model": b.model}))
    return {"experiment_id": eid}

# ---- canvas: cải thiện prompt bằng AI
class ImproveReq(BaseModel):
    template: str = Field(min_length=1, max_length=50000)
    instructions: str = Field(default="Làm prompt rõ ràng, có cấu trúc, nêu định dạng đầu ra.", max_length=2000)
    feedback_examples: list[dict] = []    # [{"input":..,"output":..,"comment":..}] từ trace có điểm thấp

@router.post("/api/prompts/improve")
async def improve(r: ImproveReq):
    vars_ = sorted(set(re.findall(r"\{\{\s*(\w+)\s*\}\}", r.template)))
    ex = ("\nVí dụ đầu ra bị chấm thấp (cần khắc phục):\n" + "\n".join(json.dumps(x, ensure_ascii=False)[:600] for x in r.feedback_examples[:5])) if r.feedback_examples else ""
    prompt = (f"Bạn là chuyên gia prompt engineering. Hãy viết lại prompt dưới đây theo yêu cầu: {r.instructions}\n"
              f"BẮT BUỘC giữ nguyên các biến {', '.join('{{' + v + '}}' for v in vars_) or '(không có)'} (đúng cú pháp {{{{tên}}}}), giữ ngôn ngữ gốc.{ex}\n\n"
              f"PROMPT GỐC:\n<<<\n{r.template}\n>>>\n\nTrả JSON: {{\"improved\": \"<prompt mới>\", \"changes\": [\"<thay đổi 1>\", \"...\"]}}")
    txt, m, _, _ = await A.gw.generate(A._g, prompt, [A.DEFAULT_MODEL] + A.gw.DEFAULT_FALLBACKS, json_mode=True, temperature=0.4, feature="prompt-hub")
    d = json.loads(txt); d = d[0] if isinstance(d, list) else d
    new = str(d.get("improved") or "")
    missing = [v for v in vars_ if not re.search(r"\{\{\s*" + v + r"\s*\}\}", new)]
    diff = "\n".join(difflib.unified_diff(r.template.splitlines(), new.splitlines(), "hiện tại", "đề xuất", lineterm=""))
    return {"improved": new, "changes": d.get("changes") or [], "missing_variables": missing, "diff": diff, "model": m}

# ============================================================ P4 Builder
TEMPLATES = {
    "tro-ly-nha-doc": {"title": "Trợ lý nhà (chỉ đọc)", "cfg": {"name": "Trợ lý nhà (template)", "description": "Đọc trạng thái Home Assistant, trả lời ngắn.",
        "system_prompt": "Bạn là trợ lý nhà. Chỉ ĐỌC trạng thái qua GetLiveContext, trả lời tiếng Việt ngắn gọn. Không điều khiển thiết bị.",
        "tools": ["ha:homeassistant__GetLiveContext", "ha:llm__GetDateTime"], "planning": False, "filesystem": False}},
    "hoi-dap-kb": {"title": "Hỏi đáp tài liệu (RAG)", "cfg": {"name": "Hỏi đáp tài liệu (template)", "description": "Trả lời từ cơ sở kiến thức, có trích nguồn.",
        "system_prompt": "Luôn gọi tra_cuu_kien_thuc trước, trả lời dựa trên kết quả, trích [tiêu đề]. Không thấy thì nói không biết.",
        "tools": ["local:tra_cuu_kien_thuc"], "planning": False, "filesystem": False}},
    "phan-tich-du-lieu": {"title": "Phân tích số liệu (sandbox)", "cfg": {"name": "Phân tích số liệu (template)", "description": "Tính toán/phân tích bằng Python trong sandbox; mỗi lần chạy code cần duyệt.",
        "system_prompt": "Dùng chay_code_python để tính toán chính xác, giải thích kết quả ngắn gọn bằng tiếng Việt.", "sandbox": True, "planning": True,
        "filesystem": False, "interrupt_on": ["local:chay_code_python"]}},
    "bao-cao-sang": {"title": "Báo cáo buổi sáng (cron)", "cfg": {"name": "Báo cáo buổi sáng (template)", "description": "Tóm tắt tình trạng nhà, dùng với lịch cron.",
        "system_prompt": "Lập báo cáo buổi sáng: giờ hiện tại, nhiệt độ/độ ẩm các phòng, thiết bị đang bật. Tối đa 8 gạch đầu dòng. Chỉ đọc.",
        "tools": ["ha:homeassistant__GetLiveContext", "ha:llm__GetDateTime"], "planning": False, "filesystem": False}},
}

# GĐ8.8: 8 mẫu theo ngành cho khách Việt Nam. Tool đọc dùng ngay; mọi tool GHI (CRM, đăng bài, chạy code tạo file) nằm trong interrupt_on
# -> dừng chờ người duyệt. Agent có sandbox tự nhận file skill builtin:docx/xlsx/pptx/pdf (deepagent_api.with_file_skills); "skills" ghi rõ gợi ý.
_VN_RULES = ("\n\nQuy tắc chung:\n- Luôn trả lời bằng tiếng Việt, xưng \"em\", gọi người dùng là \"anh/chị\"; ngắn gọn, rõ ràng, có gạch đầu dòng khi liệt kê.\n"
             "- Không bịa số liệu, giá, điều khoản hay tên người. Không chắc thì nói rõ là chưa chắc và đề xuất cách kiểm tra.\n"
             "- Thông tin cá nhân (số điện thoại, CCCD, lương, tài khoản ngân hàng) chỉ dùng cho đúng việc được hỏi, không nhắc lại thừa.\n"
             "- Thao tác ghi/gửi/đăng chỉ làm khi người dùng yêu cầu rõ; hệ thống sẽ hỏi duyệt trước khi chạy.")
def _vn(title, name, desc, prompt, tools=(), interrupt=(), sandbox=False, planning=False, memory=False, skills=()):
    return {"title": title, "industry": True, "skills_hint": list(skills), "cfg": {
        "name": name, "description": desc, "system_prompt": prompt + _VN_RULES, "tools": list(tools), "interrupt_on": list(interrupt) + (["local:chay_code_python"] if sandbox else []),
        "sandbox": sandbox, "planning": planning, "filesystem": False, "long_term_memory": memory, "skills": list(skills), "budget_usd_day": 1.0}}
TEMPLATES.update({
    "vn-ban-hang": _vn("Bán hàng", "Trợ lý bán hàng", "Tư vấn sản phẩm, ghi nhận khách tiềm năng và lịch chăm sóc vào CRM (ghi cần duyệt).",
        "Bạn là trợ lý bán hàng của doanh nghiệp. Nhiệm vụ: hiểu nhu cầu khách (sản phẩm, số lượng, ngân sách, thời gian), tra cứu thông tin sản phẩm và chính sách giá "
        "bằng tra_cuu_kien_thuc trước khi báo giá, gợi ý sản phẩm phù hợp kèm lý do. Khi khách quan tâm: tóm tắt cơ hội (khách, sản phẩm, giá trị ước tính, bước tiếp theo) "
        "rồi đề xuất ghi vào CRM và hẹn lịch chăm sóc. Xem danh sách cơ hội bằng crm_list_deals trước khi tạo mới để tránh trùng. Không hứa chiết khấu ngoài chính sách.",
        tools=["local:tra_cuu_kien_thuc", "local:crm_list_deals", "local:crm_log_deal", "local:crm_update_deal", "local:crm_schedule_followup"],
        interrupt=["local:crm_log_deal", "local:crm_update_deal", "local:crm_schedule_followup"], memory=True),
    "vn-cskh": _vn("Chăm sóc khách hàng", "Trợ lý CSKH", "Trả lời câu hỏi thường gặp, đổi trả, bảo hành theo tài liệu nội bộ; chuyển người thật khi cần.",
        "Bạn là nhân viên chăm sóc khách hàng. Luôn tra tra_cuu_kien_thuc (chính sách đổi trả, bảo hành, vận chuyển, câu hỏi thường gặp) trước khi trả lời và nêu nguồn [tiêu đề]. "
        "Giọng lịch sự, đồng cảm; khách bực thì xin lỗi ngắn gọn và đi thẳng vào cách giải quyết. Cần thêm thông tin (mã đơn, số điện thoại) thì hỏi bằng hoi_thong_tin. "
        "Việc vượt chính sách (hoàn tiền ngoại lệ, khiếu nại nghiêm trọng): tóm tắt vụ việc và nói rõ sẽ chuyển cho nhân viên phụ trách.",
        tools=["local:tra_cuu_kien_thuc", "local:hoi_thong_tin", "local:crm_list_deals"]),
    "vn-ke-toan": _vn("Kế toán", "Trợ lý kế toán", "Giải thích nghiệp vụ, tính toán số liệu, lập bảng Excel/PDF (chạy code cần duyệt).",
        "Bạn là trợ lý kế toán cho doanh nghiệp Việt Nam. Giúp hạch toán nghiệp vụ (nêu Nợ/Có theo hệ thống tài khoản hiện hành), tính thuế GTGT, TNCN, khấu hao, đối chiếu công nợ. "
        "Mọi phép tính nhiều bước dùng chay_code_python để tính chính xác; khi cần bảng tính hay báo cáo thì tạo file Excel (skill xlsx) hoặc PDF (skill pdf). "
        "Tra tra_cuu_kien_thuc cho quy định nội bộ. Số liệu thuế/luật có thể đã thay đổi: luôn ghi chú người dùng đối chiếu văn bản mới nhất hoặc hỏi kế toán trưởng.",
        tools=["local:tra_cuu_kien_thuc"], sandbox=True, planning=True, skills=["builtin:xlsx", "builtin:pdf"]),
    "vn-nhan-su": _vn("Nhân sự", "Trợ lý nhân sự", "Hỏi đáp nội quy, phép năm, lương thưởng; soạn thông báo, thư mời nhận việc (Word).",
        "Bạn là trợ lý phòng nhân sự. Trả lời câu hỏi của nhân viên về nội quy, ngày phép, bảo hiểm, quy trình tuyển dụng dựa trên tra_cuu_kien_thuc (nêu nguồn). "
        "Soạn mô tả công việc, câu hỏi phỏng vấn, thông báo nội bộ, thư mời nhận việc; khi cần file thì tạo Word (skill docx). "
        "Không tiết lộ lương hay thông tin cá nhân của người khác. Câu hỏi về Bộ luật Lao động: trả lời khái quát và khuyên đối chiếu văn bản hiện hành.",
        tools=["local:tra_cuu_kien_thuc", "local:hoi_thong_tin"], sandbox=True, skills=["builtin:docx"]),
    "vn-marketing": _vn("Marketing", "Trợ lý marketing", "Lên ý tưởng nội dung, viết bài, xem số liệu quảng cáo; đăng bài/tạo chiến dịch cần duyệt.",
        "Bạn là chuyên viên marketing. Viết nội dung cho Facebook, Zalo, TikTok, website theo giọng thương hiệu (hỏi lại nếu chưa rõ đối tượng và mục tiêu). "
        "Nghiên cứu xu hướng, đối thủ bằng web_search/fetch_url và trích nguồn. Xem hiệu quả quảng cáo bằng ads_get_stats rồi đề xuất tối ưu cụ thể. "
        "Đăng bài (content_publish) và tạo chiến dịch (ads_create_campaign) chỉ khi người dùng yêu cầu rõ, kèm ngân sách; các thao tác này luôn chờ duyệt. "
        "Tuân thủ quy định quảng cáo: không cam kết sai sự thật, không so sánh hạ thấp đối thủ.",
        tools=["local:web_search", "local:fetch_url", "local:ads_get_stats", "local:ads_create_campaign", "local:content_publish"],
        interrupt=["local:ads_create_campaign", "local:content_publish"], planning=True),
    "vn-phap-che": _vn("Pháp chế nội bộ", "Trợ lý pháp chế nội bộ", "Rà soát hợp đồng, tra quy định nội bộ và văn bản pháp luật; chỉ đọc, không thay luật sư.",
        "Bạn là trợ lý pháp chế nội bộ. Rà soát hợp đồng/văn bản người dùng gửi: liệt kê điều khoản rủi ro (thanh toán, phạt vi phạm, bồi thường, chấm dứt, bảo mật, giải quyết tranh chấp), "
        "giải thích bằng lời dễ hiểu và đề xuất câu chữ sửa. Tra quy định nội bộ bằng tra_cuu_kien_thuc, văn bản pháp luật bằng web_search/fetch_url (ưu tiên nguồn chính thống, ghi số hiệu và ngày hiệu lực). "
        "Luôn nhắc: đây là ý kiến tham khảo, việc quan trọng cần luật sư hoặc bộ phận pháp chế duyệt. Không soạn nội dung lách luật.",
        tools=["local:tra_cuu_kien_thuc", "local:web_search", "local:fetch_url"], planning=True),
    "vn-giao-vien": _vn("Giáo viên", "Trợ lý giáo viên", "Soạn giáo án, đề kiểm tra, phiếu học tập, slide bài giảng (Word/PowerPoint).",
        "Bạn là trợ lý cho giáo viên phổ thông Việt Nam (Chương trình GDPT 2018). Soạn giáo án theo mẫu 4 bước (khởi động, hình thành kiến thức, luyện tập, vận dụng), "
        "đề kiểm tra có ma trận và đáp án, phiếu học tập, câu hỏi trắc nghiệm theo mức độ (nhận biết, thông hiểu, vận dụng, vận dụng cao). "
        "Hỏi rõ môn, lớp, bài, thời lượng nếu thiếu. Khi cần file thì tạo Word (skill docx) hoặc slide PowerPoint (skill pptx). Kiến thức phải chính xác, phù hợp lứa tuổi.",
        tools=["local:tra_cuu_kien_thuc", "local:web_search"], sandbox=True, planning=True, skills=["builtin:docx", "builtin:pptx"]),
    "vn-tro-ly-giam-doc": _vn("Trợ lý giám đốc", "Trợ lý giám đốc", "Tổng hợp tình hình kinh doanh, tóm tắt tài liệu, chuẩn bị họp và báo cáo (slide/Excel).",
        "Bạn là trợ lý cho giám đốc doanh nghiệp. Tổng hợp nhanh tình hình: cơ hội bán hàng (crm_list_deals), hiệu quả quảng cáo (ads_get_stats), tài liệu nội bộ (tra_cuu_kien_thuc), "
        "tin thị trường (web_search). Trình bày theo kiểu điều hành: kết luận trước, 3-5 ý chính, rủi ro và đề xuất việc cần quyết. "
        "Chuẩn bị nội dung họp, biên bản, email; khi cần thì tạo slide (skill pptx) hoặc bảng Excel (skill xlsx). Nhớ các ưu tiên giám đốc đã dặn.",
        tools=["local:tra_cuu_kien_thuc", "local:crm_list_deals", "local:ads_get_stats", "local:web_search"], sandbox=True, planning=True, memory=True,
        skills=["builtin:pptx", "builtin:xlsx"]),
})

@router.get("/api/builder/templates")
async def tpl_list(): return [{"key": k, "title": v["title"], "config": v["cfg"], "industry": v.get("industry", False), "skills": v.get("skills_hint", [])} for k, v in TEMPLATES.items()]

@router.post("/api/builder/templates/{key}")
async def tpl_create(key: str):
    t = TEMPLATES.get(key)
    if not t: raise HTTPException(404)
    aid, v = await A.create_agent(A.AgentCfg(**t["cfg"]), f"từ template {key}")
    return {"id": aid, "version": v}

async def _catalog():
    allowed = await A.enabled_tool_keys()
    cat = [{"key": k, "description": (t.description or "")[:200], "read_only": A.is_ha_read(k.split(":")[0], k.split(":", 1)[1])}
           for k, t in __import__("mcp_registry").tools_for(__import__("mcp_registry").cur_ws()).items() if allowed.get(k)]
    cat += [{"key": "local:tra_cuu_kien_thuc", "description": "Tra cứu cơ sở kiến thức nội bộ (pgvector)", "read_only": True}]
    cat += [{k: c[k] for k in ("key", "description", "read_only")} for c in await __import__("workspace_tools").catalog()]   # GĐ8.5 google:*/ms365:* (enabled per workspace)
    return cat

class GenReq(BaseModel):
    description: str = Field(min_length=5, max_length=4000)

@router.post("/api/builder/generate")
async def gen_agent(r: GenReq):
    """Mô tả bằng lời → cấu hình agent nháp (chưa tạo). Chỉ dùng tool đang bật; tool ghi tự động cần duyệt (HITL)."""
    cat = await _catalog(); keys = {c["key"] for c in cat}
    prompt = ("Bạn thiết kế cấu hình agent cho nền tảng Moza Agent OS từ mô tả của người dùng. Tool khả dụng (CHỈ được chọn trong danh sách):\n"
              + "\n".join(f"- {c['key']}: {c['description']}{' (chỉ đọc)' if c['read_only'] else ' (GHI — sẽ cần người duyệt)'}" for c in cat)
              + "\nTuỳ chọn khác: sandbox (true nếu cần tính toán/chạy code Python), planning (true nếu nhiệm vụ nhiều bước), long_term_memory (true nếu cần nhớ người dùng),"
                " subagents (tối đa 2, mỗi cái {name: chữ thường-gạch, description, system_prompt, tools}).\n"
              f"MÔ TẢ: {r.description}\n\nTrả JSON: {{\"name\": \"...\", \"description\": \"...\", \"system_prompt\": \"<prompt tiếng Việt chi tiết>\", \"tools\": [...],"
              " \"sandbox\": false, \"planning\": false, \"long_term_memory\": false, \"subagents\": [], \"reasoning\": \"<vì sao chọn vậy>\"}")
    txt, m, _, _ = await A.gw.generate(A._g, prompt, [A.DEFAULT_MODEL] + A.gw.DEFAULT_FALLBACKS, json_mode=True, temperature=0.3, feature="builder")
    d = json.loads(txt); d = d[0] if isinstance(d, list) else d
    warn = []
    tools = [t for t in d.get("tools") or [] if t in keys]
    if len(tools) != len(d.get("tools") or []): warn.append("Đã bỏ tool không có/không bật: " + ", ".join(set(d.get("tools") or []) - set(tools)))
    blocked = [t for t in tools if A.block_re().search(t)]
    if blocked: tools = [t for t in tools if t not in blocked]; warn.append("Đã bỏ tool bị chặn (khoá/báo động/gara): " + ", ".join(blocked))
    subs = []
    for sa in (d.get("subagents") or [])[:2]:
        try: subs.append(A.SubAgentCfg(name=re.sub(r"[^a-z0-9_-]", "-", str(sa.get("name", "sub")).lower())[:40] or "sub", description=str(sa.get("description", ""))[:500],
                                       system_prompt=str(sa.get("system_prompt", ""))[:8000], tools=[t for t in sa.get("tools") or [] if t in tools or t in keys]).model_dump())
        except Exception as e: warn.append(f"Bỏ sub-agent lỗi: {e}")  # noqa: BLE001
    cfg = A.AgentCfg(name=str(d.get("name") or "Agent mới")[:80], description=str(d.get("description") or "")[:500], system_prompt=str(d.get("system_prompt") or "")[:20000],
                     tools=tools, sandbox=bool(d.get("sandbox")), planning=bool(d.get("planning")), filesystem=False,
                     long_term_memory=bool(d.get("long_term_memory")), subagents=subs)
    writes = [t for t in tools if not t.startswith("local:") and not A.is_ha_read(t.split(":")[0], t.split(":", 1)[1])]
    if writes: warn.append("Tool GHI sẽ dừng chờ duyệt trước mỗi lần chạy: " + ", ".join(writes))
    return {"config": cfg.model_dump(exclude={"note"}), "reasoning": d.get("reasoning"), "warnings": warn, "model": m}

class CreateReq(BaseModel):
    config: dict
@router.post("/api/builder/create")
async def builder_create(r: CreateReq):
    aid, v = await A.create_agent(A.AgentCfg(**r.config), "tạo bằng AI builder"); return {"id": aid, "version": v}

# ---- HITL approvals
@router.get("/api/approvals")
async def appr_list(status: str = "pending"):
    return await A.q("SELECT p.*, a.name agent FROM approvals p LEFT JOIN agents a ON a.id=p.agent_id WHERE (%s='all' OR status=%s) ORDER BY id DESC LIMIT 100", (status, status))

class Decide(BaseModel):
    decision: str = Field(pattern=r"^(approve|reject|edit)$")
    message: str | None = None
    args: dict | None = None      # cho 'edit': tham số mới (áp cho mọi action trong yêu cầu nếu chỉ 1)

@router.post("/api/approvals/{aid}")
async def appr_decide(aid: int, d: Decide):
    ap = await A.q("SELECT * FROM approvals WHERE id=%s", (aid,), one=True)
    if not ap: raise HTTPException(404)
    if ap["status"] != "pending": raise HTTPException(409, "Đã xử lý")
    reqs = [ar for p in (ap["payload"] or []) for ar in (p.get("action_requests") or [])]
    if d.decision == "edit":
        if len(reqs) != 1 or d.args is None: raise HTTPException(400, "Sửa chỉ hỗ trợ 1 action và cần args")
        txt = json.dumps({"name": reqs[0]["name"], "args": d.args}, ensure_ascii=False)
        if A.block_re().search(txt): raise HTTPException(403, "Tham số chứa thiết bị bị chặn (khoá/báo động/gara)")
        decisions = [{"type": "edit", "edited_action": {"name": reqs[0]["name"], "args": d.args}}]
    elif d.decision == "approve": decisions = [{"type": "approve"} for _ in reqs]
    else: decisions = [{"type": "reject", "message": d.message or "Người dùng từ chối thao tác này."} for _ in reqs]
    th = await A.q("SELECT assistant_id, agent_id FROM lg_threads WHERE thread_id=%s", (ap["thread_id"],), one=True)
    if th:   # GĐ7.Z: a chat (agent-server) thread resumes through the server run queue like the chat's own approve button: same checkpoint key
        import server   # (deep research runs a hidden agent on a<thread agent>:<tid>), research mode, thread status; the run claims + closes this row
        latest = await A.q("SELECT id FROM approvals WHERE thread_id=%s AND status='pending' ORDER BY id DESC LIMIT 1", (ap["thread_id"],), one=True)
        if not latest or latest["id"] != aid:   # the server resumes the thread's LATEST interrupt: an older row's decisions must not be applied to it
            raise HTTPException(409, "Yêu cầu duyệt này đã cũ (cuộc trò chuyện có yêu cầu mới hơn). Mở cuộc trò chuyện để duyệt yêu cầu mới nhất.")
        r = await server._create_run(ap["thread_id"], server.RunIn(assistant_id=str(th["assistant_id"] or f"agent-{th['agent_id']}"), command={"resume": {"decisions": decisions}}))
        try: await server._join(str(r["run_id"]), timeout=25)   # long runs (deep research ~10 min) keep going; the Inbox shows "processing" instead of timing out
        except HTTPException as e:
            if e.status_code != 504: raise
        run = await A.q("SELECT status, error, output FROM lg_runs WHERE run_id=%s", (r["run_id"],), one=True) or {}
        row = await A.q("SELECT status FROM approvals WHERE id=%s", (aid,), one=True)
        reply = (run.get("output") or {}).get("reply") or (None if run.get("status") not in ("pending", "running") else "Đang chạy tiếp, xem kết quả trong cuộc trò chuyện.")
        return {"status": row["status"], "events": [{"reply": reply, "error": run.get("error")}]}
    await A.q("UPDATE approvals SET status='processing', decision=%s, decided=now() WHERE id=%s", (Jsonb({"decisions": decisions}), aid))
    last = None; events = []
    async for ev in A.run_agent(ap["agent_id"], ap["version"], ap["thread_id"], None, resume={"decisions": decisions}):
        events.append(ev); last = ev
    st = {"approve": "approved", "reject": "rejected", "edit": "edited"}[d.decision]
    await A.q("UPDATE approvals SET status=%s, result=%s WHERE id=%s", (st, (last or {}).get("reply") or (last or {}).get("error"), aid))
    return {"status": st, "events": events}

# ---- cron agents
async def _guard(aid):   # 8.P: agent-scoped writes need edit rights on the agent (share 'use' -> 403)
    await __import__("agent_sharing").guard_edit(aid)

async def _guard_row(table, rid):
    r = await A.q(f"SELECT agent_id FROM {table} WHERE id=%s", (rid,), one=True)
    if r: await _guard(r["agent_id"])

class CronIn(BaseModel):
    agent_id: int
    name: str = Field(min_length=1, max_length=100)
    cron: str
    message: str = Field(min_length=1, max_length=4000)
    new_thread: bool = True
    timezone: str | None = None
    run_at: str | None = None

@router.post("/api/agent_crons")
async def cron_new(c: CronIn):
    import evals
    try: cron, tz, ro = evals.norm_schedule(c.cron, c.timezone, c.run_at)
    except Exception as e: raise HTTPException(400, f"cron sai: {e}")  # noqa: BLE001
    await _guard(c.agent_id)
    return await A.q("INSERT INTO agent_crons(agent_id,name,cron,message,new_thread,timezone,run_once_at) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id", (c.agent_id, c.name, cron, c.message, c.new_thread, tz, ro), one=True)

@router.get("/api/agent_crons")
async def cron_list(): return await A.q("SELECT c.*, a.name agent FROM agent_crons c LEFT JOIN agents a ON a.id=c.agent_id ORDER BY c.id")

@router.patch("/api/agent_crons/{cid}")
async def cron_toggle(cid: int, enabled: bool | None = None, is_enabled: bool | None = None):
    enabled = is_enabled if is_enabled is not None else enabled
    if enabled is None: raise HTTPException(422, "cần enabled hoặc is_enabled")
    await _guard_row("agent_crons", cid)
    if enabled and await _cron_locked(cid): raise HTTPException(409, "Lịch đã khoá (P0-1), không bật lại được")   # GĐ5b
    await A.q("UPDATE agent_crons SET enabled=%s WHERE id=%s", (enabled, cid)); return {"ok": True}

async def _cron_locked(cid: int) -> bool:
    try: return bool((await A.q("SELECT locked_reason FROM agent_crons WHERE id=%s", (cid,), one=True) or {}).get("locked_reason"))
    except Exception: return False   # noqa: BLE001  column added by chat_gd5b.ensure()

@router.delete("/api/agent_crons/{cid}")
async def cron_del(cid: int):
    await _guard_row("agent_crons", cid)
    if await _cron_locked(cid): raise HTTPException(409, "Lịch đã khoá (P0-1), giữ lại để đối chiếu")   # GĐ5b
    await A.q("DELETE FROM agent_crons WHERE id=%s", (cid,)); return {"ok": True}

async def _run_cron(c):
    tid = f"cron{c['id']}-{int(time.time())}" if c["new_thread"] else (c.get("thread_id") or f"cron{c['id']}")   # GĐ13.28: an agent-made schedule continues the thread it was made in
    last = None
    async for ev in A.run_agent(c["agent_id"], await A.cur_version(c["agent_id"]), tid, c["message"], tags=["cron", f"cron:{c['id']}"]): last = ev
    stt = "chờ duyệt" if last.get("approval_id") else ("lỗi" if last.get("error") else "ok")
    await A.q("UPDATE agent_crons SET last_trace=%s, last_status=%s WHERE id=%s", (last.get("trace_id"), stt, c["id"]))
    if c["name"].startswith("follow-up deal #") and stt != "lỗi":   # lịch follow-up CRM là một lần: chạy xong thì tắt + đánh dấu done
        await A.q("UPDATE agent_crons SET enabled=false WHERE id=%s", (c["id"],))
        try: await A.q("UPDATE crm_followups SET status='done' WHERE cron_id=%s", (c["id"],))
        except Exception: pass

@router.post("/api/agent_crons/{cid}/run_now")
async def cron_now(cid: int):
    c = await A.q("SELECT * FROM agent_crons WHERE id=%s", (cid,), one=True)
    if not c: raise HTTPException(404)
    await _guard(c["agent_id"])
    if c.get("locked_reason"): raise HTTPException(409, "Lịch đã khoá (P0-1), không chạy được")   # GĐ5b
    await A.q("UPDATE agent_crons SET last_run=now(), last_status='đang chạy' WHERE id=%s", (cid,))
    JOBS[f"c{cid}"] = asyncio.create_task(_run_cron(c)); return {"ok": True}

async def run_crons():
    import evals, datetime as dt
    if evals.schedules_disabled(): return
    now = dt.datetime.now(dt.timezone.utc)
    for c in await A.q("SELECT * FROM agent_crons WHERE enabled AND NOT coalesce(paused, false)"):
        if c.get("locked_reason"): continue   # GĐ5b: a locked (P0-1) cron never runs, even if someone flips enabled in SQL
        try: due = evals.is_due(c["cron"], c.get("timezone"), c.get("run_once_at"), now, c["last_run"], c.get("end_time"))
        except ValueError: continue
        if due:
            await A.q("UPDATE agent_crons SET last_run=now(), last_status='đang chạy'" + (", enabled=false" if c.get("run_once_at") else "") + " WHERE id=%s", (c["id"],))
            JOBS[f"c{c['id']}"] = asyncio.create_task(_run_cron(c))

# ---- webhook trigger (public: /d/hook/{token}, Caddy đã cho /d/* qua, xác thực bằng token)
class HookNew(BaseModel):
    agent_id: int
    name: str | None = None

def _h(k): return hashlib.sha256(k.encode()).hexdigest()
RATE = {}

@router.post("/api/agent_hooks")
async def hook_new(h: HookNew):
    await _guard(h.agent_id)
    tok = "wh_" + os.urandom(20).hex()
    r = await A.q("INSERT INTO agent_hooks(agent_id,name,token_hash,prefix) VALUES(%s,%s,%s,%s) RETURNING id", (h.agent_id, h.name, _h(tok), tok[:9]), one=True)
    return {"id": r["id"], "path": f"/d/hook/{tok}", "note": "Chỉ hiện một lần. Gửi POST JSON {\"message\": \"...\", \"thread_id\": \"tuỳ chọn\"}."}

@router.get("/api/agent_hooks")
async def hook_list():
    return await A.q("""SELECT h.id, h.agent_id, a.name agent, h.name, h.prefix, h.enabled, h.calls, h.last_call,
        (SELECT json_agg(c ORDER BY c.id DESC) FROM (SELECT id, thread_id, status, trace_id, left(reply,200) reply, created FROM hook_calls WHERE hook_id=h.id ORDER BY id DESC LIMIT 5) c) recent
        FROM agent_hooks h LEFT JOIN agents a ON a.id=h.agent_id ORDER BY h.id""")

@router.delete("/api/agent_hooks/{hid}")
async def hook_del(hid: int): await _guard_row("agent_hooks", hid); await A.q("DELETE FROM agent_hooks WHERE id=%s", (hid,)); return {"ok": True}

class HookCall(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    thread_id: str | None = Field(default=None, max_length=100)
    wait: bool = False

async def _hook_run(hid, cid, aid, tid, msg):
    last = None
    async for ev in A.run_agent(aid, await A.cur_version(aid), tid, msg, tags=["webhook", f"hook:{hid}"]): last = ev
    st = "chờ duyệt" if last.get("approval_id") else ("lỗi" if last.get("error") else "ok")
    await A.q("UPDATE hook_calls SET status=%s, trace_id=%s, reply=%s WHERE id=%s", (st, last.get("trace_id"), last.get("reply") or last.get("error"), cid))
    return {"status": st, "reply": last.get("reply"), "trace_id": last.get("trace_id"), "approval_id": last.get("approval_id")}

@router.post("/d/hook/{token}", tags=["triggers"])
async def hook_call(token: str, b: HookCall):
    h = await A.q("SELECT * FROM agent_hooks WHERE token_hash=%s AND enabled", (_h(token),), one=True)
    if not h: raise HTTPException(401, "Token sai hoặc đã tắt")
    now = time.time(); RATE[h["id"]] = [t for t in RATE.get(h["id"], []) if now - t < 60] + [now]
    if len(RATE[h["id"]]) > 10: raise HTTPException(429, "Quá 10 lần/phút")
    tid = b.thread_id or f"hook{h['id']}-{int(now)}"
    import admin
    ag = await A.q("SELECT workspace_id FROM agents WHERE id=%s", (h["agent_id"],), one=True)
    if not ag: raise HTTPException(404, "Agent của hook không còn tồn tại")
    async with admin.as_workspace(ag["workspace_id"]):   # GĐ10.1: the call log, counters and the run itself live in the workspace of the hook's agent (a background task inherits it)
        cid = (await A.q("INSERT INTO hook_calls(hook_id,thread_id,status) VALUES(%s,%s,'running') RETURNING id", (h["id"], tid), one=True))["id"]
        await A.q("UPDATE agent_hooks SET calls=calls+1, last_call=now() WHERE id=%s", (h["id"],))
        if b.wait: return {"thread_id": tid, **(await _hook_run(h["id"], cid, h["agent_id"], tid, b.message))}
        JOBS[f"h{cid}"] = asyncio.create_task(_hook_run(h["id"], cid, h["agent_id"], tid, b.message))
    return {"accepted": True, "thread_id": tid, "call_id": cid}

# ============================================================ P4 Studio (đồ thị, từng bước, checkpoint, time-travel)
async def _agent(aid, version=None):
    v = version or await A.cur_version(aid); ag, cfg = await A.build_agent(aid, v); return ag, cfg, v

@router.get("/api/studio/{aid}/graph")
async def st_graph(aid: int, version: int | None = None):
    ag, cfg, v = await _agent(aid, version)
    g = ag.get_graph()
    return {"version": v, "nodes": [{"id": n, "name": getattr(x, "name", n)} for n, x in g.nodes.items()],
            "edges": [{"source": e.source, "target": e.target, "conditional": e.conditional} for e in g.edges],
            "tools": cfg.tools + (["local:chay_code_python"] if cfg.sandbox else []), "subagents": [s.name for s in cfg.subagents],
            "interrupt_on": cfg.interrupt_on, "prompt_ref": cfg.prompt_ref}

def _msg_view(m):
    d = {"type": m.type, "id": getattr(m, "id", None), "content": A._text(m.content)[:3000]}
    if getattr(m, "tool_calls", None): d["tool_calls"] = [{"name": t["name"], "args": t.get("args")} for t in m.tool_calls]
    if m.type == "tool": d["name"] = m.name
    return d

@router.get("/api/studio/{aid}/threads/{tid}/history")
async def st_history(aid: int, tid: str, limit: int = 40):
    ag, _, _ = await _agent(aid)
    out = []
    async for snap in ag.aget_state_history({"configurable": {"thread_id": f"a{aid}:{tid}"}}, limit=limit):
        msgs = (snap.values or {}).get("messages") or []
        out.append({"checkpoint_id": snap.config["configurable"]["checkpoint_id"], "step": (snap.metadata or {}).get("step"),
                    "source": (snap.metadata or {}).get("source"), "next": list(snap.next), "created": snap.created_at, "n_messages": len(msgs),
                    "last": _msg_view(msgs[-1]) if msgs else None, "interrupts": [str(i.value)[:300] for i in (snap.interrupts or [])],
                    "parent": (snap.parent_config or {}).get("configurable", {}).get("checkpoint_id")})
    return out

@router.get("/api/studio/{aid}/threads/{tid}/state")
async def st_state(aid: int, tid: str, checkpoint_id: str | None = None):
    ag, _, _ = await _agent(aid)
    c = {"configurable": {"thread_id": f"a{aid}:{tid}"}}
    if checkpoint_id: c["configurable"]["checkpoint_id"] = checkpoint_id
    s = await ag.aget_state(c)
    return {"checkpoint_id": s.config["configurable"].get("checkpoint_id"), "next": list(s.next), "messages": [_msg_view(m) for m in (s.values or {}).get("messages") or []],
            "todos": (s.values or {}).get("todos"), "interrupts": [i.value for i in (s.interrupts or [])]}

class StRun(BaseModel):
    message: str | None = Field(default=None, max_length=8000)
    checkpoint_id: str | None = None
    step: bool = False
    files: list[str] = Field(default_factory=list, max_length=5)   # GĐ9.5: uploaded files (POST /api/files) attached to this message

@router.post("/api/studio/{aid}/threads/{tid}/run")
async def st_run(aid: int, tid: str, r: StRun):
    """Chạy (hoặc tiếp tục khi message trống) — step=True: dừng trước mỗi lượt gọi tool; checkpoint_id: rẽ nhánh từ quá khứ."""
    v = await A.cur_version(aid); events = []
    import studio_tt
    async for ev in A.run_agent(aid, v, tid, await studio_tt._content(r.message, r.files, tid), checkpoint_id=r.checkpoint_id, step=r.step, tags=["studio"]): events.append(ev)
    return {"events": events, "done": events[-1]}

class StEdit(BaseModel):
    checkpoint_id: str | None = None
    message_id: str | None = None        # sửa nội dung một message (theo id)
    content: str | None = None
    append_user: str | None = None       # hoặc chèn thêm tin nhắn người dùng

@router.post("/api/studio/{aid}/threads/{tid}/state")
async def st_edit(aid: int, tid: str, e: StEdit):
    """Sửa state tại checkpoint → tạo checkpoint mới (nhánh). Sau đó chạy tiếp bằng /run với checkpoint_id mới và message trống."""
    from langchain_core.messages import AIMessage, HumanMessage
    ag, _, _ = await _agent(aid)
    c = {"configurable": {"thread_id": f"a{aid}:{tid}"}}
    if e.checkpoint_id: c["configurable"]["checkpoint_id"] = e.checkpoint_id
    s = await ag.aget_state(c)
    msgs = (s.values or {}).get("messages") or []
    if e.message_id is not None:
        m = next((x for x in msgs if x.id == e.message_id), None)
        if not m: raise HTTPException(404, "Không có message")
        new = m.model_copy(update={"content": e.content or ""})
        if isinstance(m, AIMessage) and e.content is not None: new = AIMessage(id=m.id, content=e.content, tool_calls=[] if e.content else m.tool_calls)
        upd = {"messages": [new]}
    elif e.append_user:
        upd = {"messages": [HumanMessage(content=e.append_user)]}
    else: raise HTTPException(400, "Cần message_id+content hoặc append_user")
    nc = await ag.aupdate_state(s.config, upd)
    return {"checkpoint_id": nc["configurable"]["checkpoint_id"]}
