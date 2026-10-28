"""LOCAL-ONLY fake chat model for the Phase-0 compatibility spike. Never deployed.
Deterministic, zero network, zero spend. Behaviour keyed on the last human message:
  contains "delegate" -> calls the Deep Agents `task` tool (sub-agent "researcher" = a subgraph); the sub-agent
                         receives the text without "delegate" (so "delegate publish x" -> HITL inside the subgraph)
  contains "publish"  -> calls tool content_publish (gated by interrupt_on -> HITL interrupt)
  contains "todo"     -> calls write_todos, then answers
  contains "slow"     -> streams a long answer slowly (to test Stop/cancel)
  GĐ3: "biểu đồ" -> present(Chart); "lập bảng" -> present(Table); "so sánh" -> present(Comparison);
       "thông số" -> present(SpecSheet); "ui lỗi" -> present with an unknown _type (tool rejects it);
       "tìm web"/"search web" -> web_search (faked in run_local, no network); "kế hoạch" -> 3 write_todos updates (live progress);
       "markdown demo" -> code block + KaTeX math + mermaid diagram
  otherwise           -> streams "Echo: <text>" word by word
After a ToolMessage it answers with a short summary of the tool result."""
from __future__ import annotations
import asyncio, json, re, uuid
from typing import Any, AsyncIterator, Iterator
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

def _images(c) -> list[str]:
    """G4: the fake is multimodal: it reports every image block it actually received (decoded bytes, so a URL-only block would show 0 B)."""
    import base64, hashlib
    out = []
    for p in c if isinstance(c, list) else []:
        if isinstance(p, dict) and p.get("type") in ("image", "image_url"):
            b64 = p.get("base64") or p.get("data") or ""
            raw = base64.b64decode(b64) if b64 else b""
            out.append(f"{p.get('mime_type', '?')} {len(raw)} B sha256:{hashlib.sha256(raw).hexdigest()[:12]}" if raw else f"url-only {p.get('url', '')}")
    return out

def _txt(c) -> str:
    if isinstance(c, str): return c
    return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type", "text") == "text")

class FakeAgentModel(BaseChatModel):
    model_name: str = "fake-echo"
    delay: float = 0.03
    struct_schema: dict | None = None   # GĐ9.3: set by with_structured_output (bind_tools(ls_structured_output_format=...)) -> the answer is a call of the schema's "tool"
    tool_names: list[str] = []          # GĐ11.29: names of the tools bound to this model (the "mcp-whoami" trigger needs the MCP tool's model name)

    @property
    def _llm_type(self) -> str: return "fake-agent-model"

    def bind_tools(self, tools, **kw):  # tools are visible to the agent loop; the fake decides by keyword
        f = kw.get("ls_structured_output_format")
        if f: return self.model_copy(update={"struct_schema": f.get("schema")})
        if kw.get("tool_choice") == "any" and tools and not isinstance(tools[-1], dict) and isinstance(getattr(tools[-1], "args_schema", None), dict):   # GĐ13.7 LangChain ToolStrategy (agent response_format): the LAST bound tool is the structured-output tool -> answer = a call of it
            t = tools[-1]; return self.model_copy(update={"struct_schema": {**t.args_schema, "title": t.name}})
        return self.model_copy(update={"tool_names": [n for n in (getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in tools) if n]})

    def _plan(self, messages: list[BaseMessage]) -> tuple[str, list[dict]]:
        last = messages[-1] if messages else None
        cv = _canvas_plan(messages)   # GĐ9.2 Prompt Canvas rewriter
        if cv is not None: return cv
        if self.struct_schema is not None:   # GĐ9.3 structured output
            sp = _struct_plan(messages, self.struct_schema)
            if sp is not None: return sp
        r = _research_plan(messages)   # GD7.7: Deep research orchestrator / researcher (recognised by their system prompt)
        if r is not None: return r
        g = _gen_plan(messages)
        if g is not None: return g
        if isinstance(last, ToolMessage):
            return f"Done. Tool `{last.name}` returned: {_txt(last.content)[:300]}", []
        human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        text = _txt(human.content) if human else ""
        low = text.lower()
        # only plan a tool call once per human turn
        after_human = messages[messages.index(human) + 1:] if human else []
        if any(isinstance(m, AIMessage) and m.tool_calls for m in after_human):
            return "Finished.", []
        if "show-system" in low:   # GĐ9.1 test aid: echo the system prompt the agent was built with (context pack AGENTS.md / skills / note)
            sm = next((m for m in messages if isinstance(m, SystemMessage)), None)
            return (_txt(sm.content) if sm else "(no system message)")[:12000], []
        if "[json]" in low or "[json-invalid]" in low:   # GĐ9.3 JSON mode (no schema binding): the answer is the JSON after the marker; [json-invalid] = text that is not JSON
            return ("this is not json" if "[json-invalid]" in low else text.split("[json]", 1)[1].strip()), []
        if "mcp-whoami" in low:   # GĐ11.29: call the `whoami` tool of an OAuth MCP server (tests/fakes/fake_mcp_oauth.py) -> pre-run gate / auth_required pause -> card -> run continues
            mt = next((n for n in self.tool_names if n.endswith("whoami") and not n.startswith(("aos_", "local:"))), None)
            if mt: return "", [{"name": mt, "args": {}, "id": "call_" + uuid.uuid4().hex[:10], "type": "tool_call"}]
        if "báo giá" in low:   # GĐ12.92 test aid: a scripted model that reads the SKILL.md listed in its system prompt when the user says "báo giá" (any other prompt: no read)
            sm = next((m for m in messages if isinstance(m, SystemMessage)), None)
            pm = re.search(r"/skills/(?:main|sub/[^/\s]+)/[a-z0-9\-]+/SKILL\.md", _txt(sm.content)) if sm else None
            if pm and "read_file" in self.tool_names: return "", [{"name": "read_file", "args": {"file_path": pm.group(0)}, "id": "call_" + uuid.uuid4().hex[:10], "type": "tool_call"}]
        _sk = re.search(r"skill-(save|share)[:\s]+([a-z0-9\-]+)", low)   # GĐ12.87 test aid: call skill_save / skill_share (both wait for approval)
        if _sk:
            nm = _sk.group(2)
            args = ({"name": nm, "description": "Dùng khi cần ví dụ thử. Use when the user asks for a sample (mẫu, sample, test).", "body": "# Ví dụ\nTrả lời ngắn gọn.\n\nThử: làm một ví dụ."} if _sk.group(1) == "save" else {"name": nm})
            return "", [{"name": "skill_" + _sk.group(1), "args": args, "id": "call_" + uuid.uuid4().hex[:10], "type": "tool_call"}]
        if "whoami" in low:   # GĐ9.Q: tools that read the caller: "whoami-need" asks for scope perm:billing (the tool refuses without it)
            return "", [{"name": "aos_whoami", "args": ({"require_scope": "perm:billing"} if "whoami-need" in low else {}), "id": "call_" + uuid.uuid4().hex[:10], "type": "tool_call"}]
        if "connect-check" in low:   # GĐ9.Q: auth-aware tool (Fleet auth_required JSON -> run pauses -> retried after the user connects)
            return "", [{"name": "aos_connect_check", "args": {"service": "demo"}, "id": "call_" + uuid.uuid4().hex[:10], "type": "tool_call"}]
        if "delegate" in low:   # G1: hand the request to the Deep Agents sub-agent (runs as a subgraph)
            return "", [{"name": "task", "args": {"description": text.replace("delegate", "").replace("Delegate", "").strip() or "report", "subagent_type": "researcher"},
                         "id": "call_" + uuid.uuid4().hex[:10], "type": "tool_call"}]
        if "publish" in low:
            return "", [{"name": "content_publish", "args": {"channel": "facebook", "title": "Phase 0 test", "body": text[:200]}, "id": "call_" + uuid.uuid4().hex[:10], "type": "tool_call"}]
        if "todo" in low:
            return "", [{"name": "write_todos", "args": {"todos": [{"content": "Read the request", "status": "completed"}, {"content": "Draft answer", "status": "in_progress"}, {"content": "Review", "status": "pending"}]}, "id": "call_" + uuid.uuid4().hex[:10], "type": "tool_call"}]
        if "slow" in low:
            return " ".join(f"word{i}" for i in range(120)), []
        if "chiến dịch" in low:   # A8: a canned, clearly-fake status so the spoken read-back is checkable
            return ("[Dữ liệu giả lập] Chiến dịch tháng 10: 1.240 khách tiềm năng, chi phí 18 triệu đồng, "
                    "tỉ lệ chuyển đổi 3,2 phần trăm, đang vượt mục tiêu 8 phần trăm."), []
        imgs = _images(human.content) if human else []
        if imgs: return f"Echo: {text.strip()} [I received {len(imgs)} image(s): " + "; ".join(imgs) + "]", []
        return f"Echo: {text}" if text else "Echo: (empty)", []

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        content, calls = self._plan(messages)
        ut = _usage_tokens(messages); msg = AIMessage(content=content, tool_calls=calls, usage_metadata={"input_tokens": ut, "output_tokens": 0, "total_tokens": ut})
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        r = self._generate(messages, stop, None, **kw)
        if "word0 word1" in r.generations[0].message.content: await asyncio.sleep(20)   # "slow": a slow model also when not streaming
        return r

    async def _astream(self, messages, stop=None, run_manager=None, **kw) -> AsyncIterator[ChatGenerationChunk]:
        content, calls = self._plan(messages)
        slow = "word0 word1" in content
        if calls and calls[0]["name"] == "write_todos" and any(k in _txt(next((m for m in reversed(messages) if isinstance(m, HumanMessage)), HumanMessage("")).content).lower() for k in ("kế hoạch",)):
            await asyncio.sleep(1.5)   # GĐ3.2: visible step-by-step todo updates
        if calls:
            ch = AIMessageChunk(content="", tool_call_chunks=[{"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i, "type": "tool_call_chunk"} for i, c in enumerate(calls)])
            yield ChatGenerationChunk(message=ch)
        else:
            words = content.split(" ")
            for i, w in enumerate(words):
                tok = (w if i == 0 else " " + w)
                ch = ChatGenerationChunk(message=AIMessageChunk(content=tok))
                if run_manager: await run_manager.on_llm_new_token(tok, chunk=ch)
                yield ch
                await asyncio.sleep(0.35 if slow else self.delay)
        ut = _usage_tokens(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata={"input_tokens": ut, "output_tokens": 0, "total_tokens": ut}, chunk_position="last"))


_OTHER_FLOWS = ("[spec]", "[insights]", "[insights-traces]", "[insights-exec]", "[insights-config]", "[engine]", "[engine-validate]", "[builder]")
def _sample(sc):
    """A value that satisfies a (simple) JSON schema."""
    t = sc.get("type")
    if "enum" in sc: return sc["enum"][0]
    if t == "object": return {k: _sample(v) for k, v in (sc.get("properties") or {}).items()}
    if t == "array": return [_sample(sc.get("items") or {})]
    return {"string": "giả lập", "integer": 1, "number": 1.5, "boolean": True}.get(t, "giả lập")

def _struct_plan(messages, schema):
    """GĐ9.3: with_structured_output on the fake -> a tool call named like the schema. "[json] {obj}" in the user turn = that object; "[json-invalid]" = an object
    that breaks the schema (no required keys); otherwise a sample generated from the schema. Other scripted flows (spec/insights/engine/builder) are left alone."""
    allh = "\n".join(_txt(m.content) for m in messages if isinstance(m, HumanMessage))
    if any(k in allh for k in _OTHER_FLOWS): return None
    name = schema.get("title") or schema.get("name") or "Output"
    if "[json-invalid]" in allh: args = {"unexpected": 1}
    elif "[json]" in allh: args = json.JSONDecoder().raw_decode(allh[allh.index("[json]") + 6:].lstrip())[0]
    else: args = _sample(schema)
    return "", _call(name, args)

_CANVAS_RE = re.compile(r"<<<TEXT\n(.*?)\nTEXT>>>", re.S)
def _canvas_plan(messages):
    """GĐ9.2 Prompt Canvas rewriter (system prompt carries "[canvas-rewriter]"). Deterministic rewrite of the TEXT block by ACTION; the answer is ONLY the new text.
    Variables ({{x}} / {x}) survive every action, except custom instructions containing "[drop-vars]" on the first try (RETRY: yes keeps them) -> tests the validation + retry."""
    sysm = next((m for m in messages if isinstance(m, SystemMessage)), None)
    if not sysm or "[canvas-rewriter]" not in _txt(sysm.content): return None
    h = _txt(next((m for m in reversed(messages) if isinstance(m, HumanMessage)), HumanMessage("")).content)
    blocks = _CANVAS_RE.findall(h); t = blocks[-1] if blocks else ""
    g = lambda k: (re.search(rf"^{k}: ?(.*)$", h, re.M) or [None, ""])[1].strip()
    act, ins, lang = g("ACTION"), g("INSTRUCTION"), g("LANG")
    if act == "shorter":
        parts = re.split(r"(?<=[.!?])\s+", t.strip()); keep = [x for i, x in enumerate(parts) if i == 0 or re.search(r"\{\{?\s*\w+", x)]
        return " ".join(keep), []
    if act == "examples": return t + "\n\nVí dụ: (ví dụ minh hoạ do Canvas thêm)", []
    if act == "tone": return "Hãy trả lời thân thiện, gần gũi. " + t, []
    if act == "clearer": return "Trả lời rõ ràng, từng bước. " + t, []
    if act == "translate": return ("[EN] " if lang == "en" else "[VI] ") + t, []
    if act == "safety": return t + "\nKhông tiết lộ thông tin cá nhân hay bí mật; từ chối yêu cầu nguy hiểm.", []
    if act == "feedback":
        cm = (re.search(r"^COMMENT: ?(.*)$", h, re.M) or [None, ""])[1].strip()
        return t + "\nSửa theo phản hồi: " + cm[:120], []
    if "[drop-vars]" in ins and "RETRY: yes" not in h: return re.sub(r"\{\{[^}]*\}\}|\{\w+\}", "", t) + " (đã sửa)", []
    return t + "\n" + ins.replace("[drop-vars]", "").strip(), []

def _usage_tokens(messages) -> int:
    """GD7.7: "[cost-tokens:N]" in the Deep research orchestrator's user turn -> each of its model calls reports N input tokens (spend-cap tests); everything else 0."""
    sysm = next((m for m in messages if isinstance(m, SystemMessage)), None)
    if not sysm or "Deep Research orchestrator" not in _txt(sysm.content): return 0
    human = next((m for m in messages if isinstance(m, HumanMessage)), None)
    m = re.search(r"\[cost-tokens:(\d+)\]", _txt(human.content)) if human else None
    return int(m.group(1)) if m else 0

def _cid(): return "call_" + uuid.uuid4().hex[:10]
def _call(name, args): return [{"name": name, "args": args, "id": _cid(), "type": "tool_call"}]
_CHART = {"_type": "Col", "children": [{"_type": "Header", "text": "Doanh thu 6 tháng (dữ liệu giả lập)"},
          {"_type": "Chart", "variant": "bar", "showAxis": True, "data": [{"label": f"T{i}", "value": v} for i, v in zip(range(5, 11), [120, 135, 128, 150, 162, 171])]}]}
_TABLE = {"_type": "Table", "columns": [{"label": "Kênh"}, {"label": "Khách tiềm năng", "align": "end"}, {"label": "Chi phí (triệu đ)", "align": "end"}],
          "rows": [["Facebook", 640, 9.5], ["Google", 410, 6.2], ["Zalo", 190, 2.3]]}
_CMP = {"_type": "Comparison", "title": "So sánh 3 gói (giả lập)", "options": [
            {"name": "Cơ bản", "summary": "Cho cửa hàng nhỏ", "pros": ["Rẻ"], "cons": ["Ít báo cáo"]},
            {"name": "Tiêu chuẩn", "badge": "Khuyên dùng", "pros": ["Đủ báo cáo", "Hỗ trợ 24/7"], "cons": []},
            {"name": "Nâng cao", "pros": ["API riêng"], "cons": ["Đắt"]}],
        "criteria": [{"label": "Giá/tháng", "values": ["290k", "590k", "1,2tr"]}, {"label": "Số người dùng", "values": [2, 10, "Không giới hạn"]}],
        "verdict": "Gói Tiêu chuẩn cân bằng nhất cho đội 5–10 người."}
_SPEC = {"_type": "SpecSheet", "title": "Chiến dịch Tháng 10", "subtitle": "Dữ liệu giả lập", "specs": [
            {"label": "Ngân sách", "value": "18 triệu đ"}, {"label": "Kênh", "value": "Facebook, Google"}, {"label": "Tỉ lệ chuyển đổi", "value": "3,2%"}]}
_STEPS = {"_type": "Col", "children": [{"_type": "Header", "text": "Quy trình ra mắt chiến dịch (giả lập)"}, {"_type": "Steps", "steps": [
    {"title": "Lập kế hoạch", "detail": "Mục tiêu, ngân sách, đối tượng"}, {"title": "Sản xuất nội dung", "detail": "Bài viết, ảnh, video"},
    {"title": "Chạy quảng cáo", "detail": "Cần duyệt trước khi chi tiền"}, {"title": "Đo lường", "detail": "CTR, CPA, ROAS mỗi tuần"}]}]}
_SPLIT = {"_type": "Chart", "variant": "bar", "showAxis": True, "data": [{"label": "Facebook", "value": 15}, {"label": "Google", "value": 9}, {"label": "TikTok", "value": 6}]}
_MD = ("Ví dụ hiển thị:\n\n```python\ndef tong(a: int, b: int) -> int:\n    return a + b  # cộng hai số\n```\n\n"
       "Công thức: $E = mc^2$ và\n\n$$\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}$$\n\n"
       "```mermaid\nflowchart LR\n  A[Khách hỏi] --> B{Agent}\n  B --> C[Tra cứu]\n  B --> D[Trả lời]\n```\n")

_RS = [("LangGraph documentation", "https://docs.langchain.com/oss/python/langgraph/overview"), ("Deep Agents", "https://docs.langchain.com/oss/python/deepagents/overview"),
       ("assistant-ui", "https://www.assistant-ui.com/docs")]

def _research_plan(messages):
    """GD7.7 scripted Deep research. Orchestrator: propose_plan -> (approved) write_todos -> 2 parallel task(researcher) -> canvas(report with TOC, [n], Sources) -> short answer.
    Researcher: web_search -> think_tool -> findings with [n] citations. Markers in the user turn: [skip-plan] (try task before the plan), [flood] (researchers fire 7 parallel searches),
    [cost-tokens:N] (see _usage_tokens). A rejected plan -> a short answer without any research."""
    sysm = next((m for m in messages if isinstance(m, SystemMessage)), None)
    sp = _txt(sysm.content) if sysm else ""
    orch, resr = "Deep Research orchestrator" in sp, "Deep Research researcher" in sp
    if not (orch or resr): return None
    human = next((m for m in messages if isinstance(m, HumanMessage)), None)   # first human turn (the user's question / the task description)
    text = _txt(human.content) if human else ""
    topic = re.sub(r"\[[a-z-]+(?::\d+)?\]", "", text).strip()[:80] or "chủ đề"
    done = [m for m in messages[messages.index(human) + 1:] if isinstance(m, ToolMessage)] if human else []
    ok = lambda n: [m for m in done if m.name == n and getattr(m, "status", "success") != "error"]
    srcs = "\n".join(f"[{i+1}] {t}: {u}" for i, (t, u) in enumerate(_RS))
    if resr:
        if not done: return "", [{"name": "web_search", "args": {"query": f"{topic} #{i}", "max_results": 3}, "id": _cid(), "type": "tool_call"} for i in range(7 if "[flood]" in text else 1)]
        if not [m for m in done if m.name == "think_tool"]: return "", _call("think_tool", {"reflection": "Đã có 3 nguồn phù hợp, đủ để trả lời."})
        return (f"## Kết quả: {topic}\n\nLangGraph là khung điều phối cấp thấp cho agent có trạng thái [1]. Deep Agents bổ sung lập kế hoạch, sub-agent và hệ thống tệp [2]. "
                f"assistant-ui cung cấp thành phần giao diện chat React [3].\n\n### Sources\n{srcs}"), []
    if not [m for m in done if m.name == "propose_plan"]:
        if "[skip-plan]" in text and not [m for m in done if m.name == "task"]:
            return "", _call("task", {"description": f"{topic}: tìm nguồn", "subagent_type": "researcher"})
        return "", _call("propose_plan", {"plan": f"## Kế hoạch nghiên cứu\n- Mục tiêu: {topic}\n- Câu hỏi 1: khái niệm và kiến trúc\n- Câu hỏi 2: so sánh các lựa chọn\n- Dùng 2 researcher song song\n- Báo cáo trong Canvas: mục lục, trích dẫn [n], nguồn"})
    if not ok("propose_plan"): return "Kế hoạch chưa được duyệt nên em dừng ở đây. Anh cho em biết cần đổi gì trong kế hoạch nhé.", []
    if not [m for m in done if m.name == "write_todos"]:
        return "", _call("write_todos", {"todos": [{"content": "Nghiên cứu khái niệm", "status": "in_progress"}, {"content": "So sánh lựa chọn", "status": "in_progress"}, {"content": "Viết báo cáo", "status": "pending"}]})
    fl = " [flood]" if "[flood]" in text else ""
    if len(ok("task")) < 2:
        if [m for m in done if m.name == "task"]: return "Chưa đủ kết quả nghiên cứu để viết báo cáo.", []
        return "", [{"name": "task", "args": {"description": d, "subagent_type": "researcher"}, "id": _cid(), "type": "tool_call"} for d in (f"{topic}: khái niệm và kiến trúc{fl}", f"{topic}: so sánh các lựa chọn{fl}")]
    if not [m for m in done if m.name == "canvas"]:
        rep = (f"# Báo cáo nghiên cứu: {topic}\n\n## Mục lục\n- Tổng quan\n- So sánh\n- Kết luận\n\n## Tổng quan\nLangGraph là khung điều phối cấp thấp cho agent có trạng thái [1]. "
               f"Deep Agents bổ sung lập kế hoạch và sub-agent [2].\n\n## So sánh\nGiao diện chat dùng assistant-ui [3], còn phần điều phối dùng LangGraph [1].\n\n## Kết luận\n"
               f"Nên dùng Deep Agents cho tác vụ nghiên cứu nhiều bước [2].\n\n### Nguồn\n{srcs}")
        return "", _call("canvas", {"tieu_de": f"Báo cáo: {topic}"[:100], "noi_dung": rep, "loai": "document"})
    return "Em đã viết báo cáo nghiên cứu trong Canvas với mục lục, trích dẫn [1]–[3] và danh sách nguồn.", []

_BQ = {"audience": "Agent sẽ trò chuyện với ai (khách hàng, nhân viên, phòng ban nào)?", "data": "Agent cần dùng dữ liệu hoặc tài liệu nào?",
       "actions": "Agent được làm gì thay anh/chị (trả lời, gửi, tạo, cập nhật...)?", "tone": "Anh/chị muốn agent nói chuyện với giọng điệu thế nào?"}
_BKW = {"audience": ("khách", "nhân viên", "đội ", "phòng ", "cho ai"), "data": ("dữ liệu", "tài liệu", "file", "excel", "bảng giá", "chính sách"),
        "actions": ("gửi", "tạo", "đặt", "cập nhật", "lập "), "tone": ("giọng", "thân thiện", "lịch sự", "trang trọng", "vui vẻ")}
_BDOM = [("bán hàng", "Trợ lý bán hàng", "zalo", []), ("cskh", "Trợ lý chăm sóc khách hàng", "zalo", []), ("chăm sóc khách", "Trợ lý chăm sóc khách hàng", "zalo", []),
         ("kế toán", "Trợ lý kế toán", "none", ["xlsx", "pdf"]), ("nhân sự", "Trợ lý nhân sự", "telegram", ["docx"]), ("nội bộ", "Trợ lý nội bộ", "slack", [])]

def _builder_turn(allh):
    """GD8.7 fake builder: topics found by keywords in the first brief, one topic answered per later user reply; '[nhieu-cau]' keeps asking
    (the endpoint's cap + BUILDER_ERROR retry must stop it); '[tool:x]' adds tool x to the draft (unknown tools must be dropped)."""
    pl = json.JSONDecoder().raw_decode(allh[allh.index("[builder]") + 9:].lstrip())[0]
    users = [m["content"] for m in pl["conversation"] if m["role"] == "user"]; first = users[0].lower(); everything = " ".join(users).lower()
    answered = ["job"] + [t for t in ("audience", "data", "actions", "tone") if any(k in first for k in _BKW[t])]
    missing = [t for t in ("audience", "data", "actions", "tone") if t not in answered]
    answered += missing[:len(users) - 1]; missing = missing[len(users) - 1:]
    retry = "BUILDER_ERROR:" in allh
    if "[no-actions]" in first and not retry: answered = ["job", "audience", "data", "tone"] + (["actions"] if len(users) > 1 else []); missing = []   # GĐ9.P1: model drafts without covering "actions" (server must still ask)
    if not retry and ("[nhieu-cau]" in first or (missing and not pl["must_finish"])):
        return "", _call("BuilderTurn", {"answered": answered, "question": _BQ[(missing or ["tone"])[0]]})
    name, ch, skills = next(((n, c, s) for k, n, c, s in _BDOM if k in everything), ("Trợ lý mới", "none", []))
    keys = [t["key"] for t in pl["tools"]]
    tools = (["local:tra_cuu_kien_thuc"] if "local:tra_cuu_kien_thuc" in keys else []) + re.findall(r"\[tool:([^\]\s]+)\]", everything)
    instr = (f"Bạn là {name.lower()}. Nhiệm vụ: {users[0]}\n" + "".join(f"- {u}\n" for u in users[1:])
             + "Trả lời tiếng Việt, ngắn gọn, lịch sự. Luôn tra cứu tài liệu nội bộ trước khi trả lời; không biết thì nói không biết. Không tự ý hứa giảm giá hay thay đổi dữ liệu.")
    return "", _call("BuilderTurn", {"answered": answered, "draft": {"name": name, "description": users[0][:200], "instructions": instr, "tools": tools,
                                                                     "model": pl.get("default_model"), "channel": ch, "cost_cap_usd_month": 2.0, "skills": skills}})

def _gen_plan(messages):
    """GĐ3 fake flows; None = not a GĐ3 request (fall through to the Phase-0 behaviour)."""
    human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    if human is None: return None
    # GD7.2 (natural-language search tests): a question containing "[spec] {json}" is answered with that FilterSpec as a structured-output tool call;
    # after a "FILTER_ERROR:" message (the endpoint's one retry) "[spec2] {json}" is used instead if present
    allh = "\n".join(_txt(m.content) for m in messages if isinstance(m, HumanMessage))
    if "[insights-traces]" in allh:   # GĐ13 12.43 Insights traces mode: per trace "Hỏi: <first words of the input>" (+ " (lỗi)" on an error), attributes derived from the trace
        hd = json.JSONDecoder().raw_decode(allh[allh.index("[insights-traces]") + 17:].lstrip())[0]; items = []
        for m in re.finditer(r"### trace (\d+)\nInput: (.*?)\nOutput: (.*?)\nError: (.*?)\nFeedback: ", allh, re.S):
            words = m.group(2).split(); err = m.group(4).strip() != "none"
            val = lambda a: ("false" if err else "true") if a["type"] == "boolean" else str(len(words)) if a["type"] == "number" else ("loại " + (words[0] if words else "?")) if a["type"] == "category" else " ".join(words[:3])
            items.append({"i": int(m.group(1)), "summary": "Hỏi: " + " ".join(words[:6]) + (" (lỗi)" if err else ""), "category": "loại " + (words[0] if words else "?"),
                          "attributes": [{"name": a["name"], "value": val(a)} for a in hd["attributes"]]})
        return "", _call("TraceBatch", {"items": items})
    if "[insights-exec]" in allh:   # 12.43 executive summary: finding 1 = valid ids of group 0, finding 2 = one valid + one invented id, finding 3 = only invented / malformed ids (must be dropped)
        pl = json.JSONDecoder().raw_decode(allh[allh.index("[insights-exec]") + 15:].lstrip())[0]; g = pl["groups"]
        ids = lambda k: [s["trace_id"] for s in g[min(k, len(g) - 1)]["samples"]]
        return "", _call("ExecSummary", {"overview": f"Giả lập: {pl['n']} trace, {len(g)} nhóm; nhóm lớn nhất: {g[0]['label']}.", "findings": [
            {"title": f"Nhóm lớn nhất: {g[0]['label']}", "detail": f"{g[0]['n']} trace, tỷ lệ lỗi {g[0]['error_rate']}.", "trace_ids": ids(0)},
            {"title": "Có id bịa", "detail": "Một id thật và một id không tồn tại.", "trace_ids": ids(1)[:1] + ["11111111-1111-1111-1111-111111111111"]},
            {"title": "Chỉ id bịa", "detail": "Phải bị loại.", "trace_ids": ["22222222-2222-2222-2222-222222222222", "not-a-uuid"]}]})
    if "[insights-config]" in allh:   # 12.43 config draft from one question
        q = json.JSONDecoder().raw_decode(allh[allh.index("[insights-config]") + 17:].lstrip())[0]["question"]
        return "", _call("ConfigDraft", {"name": "Báo cáo: " + q[:40], "summary_prompt": f"Với mỗi trace, tóm tắt: {q}", "attributes": [
            {"name": "Resolved", "type": "boolean", "description": "Yêu cầu đã được giải quyết"}, {"name": "topic", "type": "category", "description": "Chủ đề"}]})
    if "[insights]" in allh:   # GD7.3 Insights labelling: label = "Chủ đề: " + first words of the first sample, one entry per sub-cluster
        pl = json.JSONDecoder().raw_decode(allh[allh.index("[insights]") + 10:].lstrip())[0]
        nm = lambda c: "Chủ đề: " + " ".join((c["samples"] or [""])[0].split()[:3])
        return "", _call("ClusterLabels", {"label": nm(pl["cluster"]), "summary": f"{pl['cluster']['n']} cuộc hội thoại về chủ đề này.", "subs": [{"label": nm(s), "summary": f"{s['n']} cuộc."} for s in pl["subs"]]})
    if "[engine-validate]" in allh:   # GĐ13 12.35 Engine "Test issue" judge: the verdict is named in the replay's own text ("verdict:reproduced" / "verdict:not_reproduced"), anything else is inconclusive
        pl = json.JSONDecoder().raw_decode(allh[allh.index("[engine-validate]") + 17:].lstrip())[0]
        txt = (pl["replay"].get("output") or "") + " " + (pl["replay"].get("error") or "")
        v = "not_reproduced" if "verdict:not_reproduced" in txt else "reproduced" if "verdict:reproduced" in txt else "inconclusive"
        return "", _call("Verdict", {"verdict": v, "reason": f"Giả lập: {v} ({len(pl['replay'].get('blocked_tools') or [])} công cụ bị chặn)."})
    if "[engine]" in allh:   # GD7.4 Engine analysis: deterministic root cause quoting the first sample; prompt fix = current prompt + one rule (config when the issue has no agent)
        pl = json.JSONDecoder().raw_decode(allh[allh.index("[engine]") + 8:].lstrip())[0]
        s0 = pl["samples"][0]; ag = pl.get("agent") or {}
        prop = ({"type": "prompt", "target": "system prompt of " + ag["name"], "new_text": (ag.get("current_prompt") or "") + "\nQuy tắc Engine: nếu gặp lỗi, giải thích ngắn gọn và đề nghị người dùng thử lại." + ("\nverdict:not_reproduced" if pl.get("failed_replays") and "heal" in pl["issue"]["title"] else ""), "rationale": "Giả lập: thêm quy tắc xử lý lỗi."}
                if ag.get("current_prompt") else {"type": "config", "target": "temperature", "rationale": "Giả lập: giảm temperature."})
        q0 = (s0["error"] or s0["input"] or s0["output"] or "")[:80]; st0 = (s0.get("steps") or [{}])[0].get("span_id")
        return "", _call("EngineAnalysis", {"root_cause": f"Giả lập: {pl['issue']['title']} ({pl['issue']['count']} lần).",   # 12.34: category / severity two steps away (clamped) / claims (one invented quote, one false) / test questions
                                            "category": "low_quality", "severity": min(3, pl["issue"].get("severity", 2) + 2) if pl["issue"].get("severity", 2) < 2 else max(0, pl["issue"].get("severity", 2) - 2), "description": "Giả lập: mô tả vấn đề.",
                                            "evidence": [{"run_id": s0["run_id"], "span_id": st0, "quote": q0}, {"run_id": "not-a-sample", "quote": "bịa"}],
                                            "claims": [{"statement": "Agent nói đã hoàn tất", "run_id": s0["run_id"], "verdict": "true", "quote": q0}, {"statement": "Agent bịa mã đơn hàng", "run_id": s0["run_id"], "verdict": "false", "quote": q0},
                                                       {"statement": "Agent nói đã gửi email", "run_id": s0["run_id"], "verdict": "true", "quote": "câu không có trong trace"}],
                                            "sample_notes": [{"run_id": s["run_id"], "test_questions": ["Có xin lỗi không?", "Có đề nghị thử lại không?", "Không lộ thông tin cá nhân?"], "expected_output": "Giả lập: câu trả lời đúng."} for s in pl["samples"]] + [{"run_id": "not-a-sample", "test_questions": ["x"]}],
                                            "proposal": prop})
    if "[builder]" in allh:   # GD8.7 agent-by-chat builder (BuilderTurn)
        return _builder_turn(allh)
    if "[spec]" in allh:
        mk ="[spec2]" if ("FILTER_ERROR:" in allh and "[spec2]" in allh) else "[spec]"
        return "", _call("FilterSpec", json.JSONDecoder().raw_decode(allh[allh.index(mk) + len(mk):].lstrip())[0])
    low = _txt(human.content).lower()
    after = messages[messages.index(human) + 1:]
    tools_done = [m for m in after if isinstance(m, ToolMessage)]
    def once(name, args, done_text):
        return (done_text, []) if tools_done else ("", _call(name, args))
    # GĐ7.1 (assistant tests): "[tool:<name>] {json args}" -> call that tool, then answer with its result; "echo-user" -> echo the user turn exactly as the model received it
    m7 = re.match(r"\s*\[tool:([a-z_]+)\]\s*(\{.*\})?", _txt(human.content), re.S)
    if m7:
        if tools_done: return (f"Trợ lý đã dùng {tools_done[-1].name}: {_txt(tools_done[-1].content)[:6000]}", [])
        return "", _call(m7[1], json.loads(m7[2] or "{}"))
    if low.strip().startswith("echo-user"): return "USER<<" + _txt(human.content) + ">>", []
    # GĐ5a: "ghi nhớ: <fact>" -> luu_ghi_nho (long-term memory); "hỏi thông tin" -> hoi_thong_tin form (interrupt, resume with answers)
    if "show system prompt" in low:   # GD6.9 test hook: echo the system message the model actually received
        sysm = next((m for m in messages if isinstance(m, SystemMessage)), None)
        return "SYS<<" + (_txt(sysm.content) if sysm else "") + ">>", []
    if "tìm trong tệp:" in low:   # GD6.8: long attached documents -> search_attached_files(query)
        qy = _txt(human.content).split("tìm trong tệp:", 1)[1].strip().split("<attachment")[0].strip()
        return once("search_attached_files", {"query": qy}, "Đã tra cứu tệp đính kèm.")
    if "tìm trong dự án:" in low:   # GD8.9: long project files -> search_project_files(query)
        qy = _txt(human.content).split("tìm trong dự án:", 1)[1].strip()
        return once("search_project_files", {"query": qy}, "Đã tra cứu tệp của dự án.")
    if "ghi nhớ:" in low:
        fact = _txt(human.content).split(":", 1)[1].strip() or "(trống)"
        return once("luu_ghi_nho", {"noi_dung": fact}, f"Em đã ghi nhớ: {fact}")
    if "hỏi thông tin" in low:
        if tools_done: return (f"Cảm ơn anh. {_txt(tools_done[-1].content)[:300]}", [])
        return "", _call("hoi_thong_tin", {"tieu_de": "Em cần thêm vài thông tin để lên kế hoạch chiến dịch", "ghi_chu": "Dữ liệu giả lập (fake LLM).",
                  "truong": [{"name": "ngan_sach", "label": "Ngân sách (triệu đồng)", "kind": "number", "placeholder": "vd 20"},
                             {"name": "kenh", "label": "Kênh chính", "kind": "choice", "options": ["Facebook", "Google", "TikTok", "Zalo"]},
                             {"name": "doi_tuong", "label": "Đối tượng", "kind": "multi", "options": ["Sinh viên", "Văn phòng", "Phụ huynh"], "required": False},
                             {"name": "ngay_bat_dau", "label": "Ngày bắt đầu", "kind": "date"},
                             {"name": "ghi_chu", "label": "Ghi chú thêm", "kind": "text", "required": False},
                             {"name": "can_bao_cao", "label": "Gửi báo cáo hằng tuần", "kind": "toggle"}]})
    # GĐ5b: "viết canvas" -> canvas (document); "sửa canvas <doc_id>" -> canvas_doc then a new version; "chạy code" -> chay_code_python
    if "sửa canvas" in low:
        doc = (low.split("sửa canvas", 1)[1].strip().split() or [""])[0]
        if not tools_done: return "", _call("canvas_doc", {"doc_id": doc})
        if len(tools_done) == 1:
            body = _txt(tools_done[-1].content).split(":\n", 1)[-1]
            return "", _call("canvas", {"tieu_de": "", "doc_id": doc, "noi_dung": body + "\n\n## Bổ sung\n- Thêm mục KPI tuần.", "ghi_chu": "thêm mục KPI"})
        return ("Em đã thêm mục KPI vào bản mới trong canvas.", [])
    if "viết canvas" in low:
        return once("canvas", {"tieu_de": "Kế hoạch nội dung tháng 11", "loai": "document",
                               "noi_dung": "# Kế hoạch nội dung tháng 11\n\n## Mục tiêu\n- Tăng 20% lượt tiếp cận\n- 12 bài viết\n\n## Lịch đăng\n| Tuần | Chủ đề |\n|---|---|\n| 1 | Ra mắt drip bag |\n| 2 | Câu chuyện khách hàng |\n"},
                    "Em đã mở bản kế hoạch trong canvas, anh xem và sửa trực tiếp nhé.")
    if "code canvas" in low:
        return once("canvas", {"tieu_de": "tinh_tong.py", "loai": "code", "ngon_ngu": "python",
                               "noi_dung": "def tong(n):\n    return n * (n + 1) // 2\n\nprint(tong(100))\n"}, "File code đã nằm trong canvas, anh bấm Chạy để thử.")
    if "chạy code" in low:
        return once("chay_code_python", {"code": "import matplotlib.pyplot as plt\nxs=[1,2,3,4]\nys=[120,135,150,171]\nprint('Tổng', sum(ys))\nplt.plot(xs, ys, marker='o')\nplt.title('Doanh thu')"},
                    "Tổng doanh thu 4 tháng là 576, biểu đồ ở trên.")
    # GĐ3 leftovers: case 16 (process question -> Steps), case 9 (visualise a budget split -> present only; explicit launch -> HITL)
    if "quy trình" in low or "các bước" in low: return once("present", _STEPS, "Bốn bước, đo lường xong thì quay lại tối ưu.")
    if "phân bổ" in low: return once("present", _SPLIT, "Facebook chiếm một nửa ngân sách.")
    if "tạo chiến dịch" in low or "chạy chiến dịch" in low:
        if tools_done: return (f"Kết quả: {_txt(tools_done[-1].content)[:200]}", [])
        return "", _call("ads_create_campaign", {"name": "Drip bag Tết 2027", "channel": "facebook", "budget_vnd": 8000000, "audience": "Dân văn phòng 25-40, TP.HCM", "creative": "Cà phê Moza drip bag, quà Tết tiện lợi (giả lập)"})
    if "markdown demo" in low: return (_MD, []) if not tools_done else ("Finished.", [])
    if "ui lỗi" in low: return once("present", {"_type": "Script", "src": "x"}, "Không hiển thị được giao diện, em trả lời bằng chữ: (giả lập).")
    if "biểu đồ" in low: return once("present", _CHART, "Doanh thu tăng đều, tháng 10 cao nhất (171).")
    if "so sánh" in low: return once("present", _CMP, "Em khuyên gói Tiêu chuẩn.")
    if "thông số" in low: return once("present", _SPEC, "Đây là tóm tắt chiến dịch.")
    if "lập bảng" in low: return once("present", _TABLE, "Facebook mang lại nhiều khách tiềm năng nhất.")
    if "tìm web" in low or "search web" in low:
        if tools_done: return ("Theo kết quả tìm kiếm [1] và [2], Agent OS hỗ trợ LangGraph (giả lập).", [])
        return "", _call("web_search", {"query": "Agent OS LangGraph", "max_results": 3})
    if "kế hoạch" in low:
        steps = ["Đọc yêu cầu", "Thu thập dữ liệu", "Viết báo cáo"]
        k = len([m for m in tools_done if m.name == "write_todos"])
        if k >= 3: return ("Xong kế hoạch: đã hoàn thành 3/3 bước.", [])
        todos = [{"content": c, "status": "completed" if i < k else ("in_progress" if i == k else "pending")} for i, c in enumerate(steps)]
        if k == 2: todos[2]["status"] = "completed"
        return "", _call("write_todos", {"todos": todos})
    return None

# ---- GD7.6 / GD7.5: scripted LLM judge behind gateway.generate (json_mode) for the openevals catalogue + judge alignment tests. Deterministic, $0.
_FEW = re.compile(r"<example>\n<input>(.*?)</input>\n<output>(.*?)</output>(?:\n<reasoning>(.*?)</reasoning>)?(?:\n<score>(.*?)</score>)?\n</example>", re.S)

def _fake_decision(prompt: str):
    """GD9.Q: scripted decision-model evaluator. The STATE can carry "[decide:<question>=<value>]" markers (number for predicate / score, option name for choice,
    A | B | tie for preference); a preference question without marker picks the answer that contains "[better]" (A / B), else tie. Defaults: predicate 1.0, choice first option, score 0."""
    state = prompt.split("STATE:\n", 1)[1].split("\n\nANSWER A:", 1)[0].split("\n\nQUESTIONS:", 1)[0]
    mk = dict(re.findall(r"\[decide:([\w\-.]+)=([^\]]*)\]", state)); ab = re.search(r"ANSWER A: (.*)\nANSWER B: (.*)\n\nQUESTIONS:", prompt, re.S)
    ans = {}
    for n, t, rest in re.findall(r"^\d+\. name=(\S+) type=(\w+):(.*)$", prompt, re.M):
        if n in mk: ans[n] = float(mk[n]) if t in ("predicate", "score") else mk[n]
        elif t == "predicate": ans[n] = 1.0
        elif t == "score": ans[n] = 0
        elif t == "choice":
            o = re.search(r"Options: (.*?) -> answer", rest); ans[n] = re.sub(r"\s*\(.*$", "", o.group(1).split(" | ")[0]).strip() if o else ""
        else: ans[n] = "tie" if not ab else ("A" if "[better]" in ab.group(1) else "B" if "[better]" in ab.group(2) else "tie")
    return json.dumps({"answers": ans})

def _fake_assertions(prompt: str):
    """GD9.Q: scripted assertion judge: every assertion is satisfied unless the OUTPUT carries "[assert:<key>=false]"."""
    out = prompt.split("OUTPUT:", 1)[1].split("\n\nASSERTIONS:", 1)[0]
    return json.dumps({"results": {k: f"[assert:{k}=false]" not in out for k in re.findall(r"^- ([\w\-.]+):", prompt.split("ASSERTIONS:", 1)[1], re.M)}})

def _fake_gw_decision(prompt: str):
    """GĐ9.Q: scripted gateway decision model (/v1/decisions + `model: auto` routing). The INPUT carries "[decide:<question>=<value>]" markers; defaults: predicate 0.0,
    choice = the option named in "[complexity:<opt>]" else "moderate" if offered else the first option, score = lower bound of the scale."""
    inp = prompt.split("INPUT:\n", 1)[1].split("\n\nQUESTIONS:", 1)[0]
    mk = dict(re.findall(r"\[decide:([\w\-.]+)=([^\]]*)\]", inp)); cx = re.search(r"\[complexity:(\w+)\]", inp); ans = {}
    for n, t, rest in re.findall(r"^\d+\. name=(\S+) type=(\w+):(.*)$", prompt, re.M):
        if n in mk: ans[n] = float(mk[n]) if t in ("predicate", "score") else mk[n]
        elif t == "predicate": ans[n] = 0.0
        elif t == "score":
            sc = re.search(r"in \[([-\d.]+), ([-\d.]+)\]", rest); ans[n] = float(sc.group(1)) if sc else 0.0
        else:
            o = [x.strip() for x in (re.search(r"exactly one of: (.*?)$", rest).group(1).split("|") if re.search(r"exactly one of: (.*?)$", rest) else [])]
            ans[n] = cx.group(1) if (cx and n == "complexity") else ("moderate" if "moderate" in o else (o[0] if o else ""))
    return json.dumps({"answers": ans})

def fake_judge(prompt: str):
    """Returns a JSON string for a judge prompt (evalcat.GatewayJudge ends it with 'Return ONLY JSON: {"reasoning"...'), else None.
    "[judge-score:X]" in the judged text -> that score (number | true | false), whatever the template.
    "[judge-align] ... topic:T" -> the judge is LENIENT (True) unless its few-shot examples (openevals appends <example>..<score>False</score>) show the same topic T with a score:
    then it answers with that example's score. So few-shot examples change its answers predictably (judge-alignment tests)."""
    if isinstance(prompt, str) and prompt.startswith("DECISION MODEL."): return _fake_decision(prompt)
    if isinstance(prompt, str) and prompt.startswith("ASSERTION CHECK."): return _fake_assertions(prompt)
    if isinstance(prompt, str) and prompt.startswith("GATEWAY DECISION."): return _fake_gw_decision(prompt)
    if isinstance(prompt, str) and "--- MATERIAL ---" in prompt:   # GĐ12.87 skill_draft: the draft is the JSON after the first "[json]" in the user's material (none -> not a draft -> HTTP 502)
        mat = prompt.split("--- MATERIAL ---", 1)[1]
        return mat.split("[json]", 1)[1].strip() if "[json]" in mat else None
    if not isinstance(prompt, str) or 'Return ONLY JSON: {"reasoning"' not in prompt: return None
    want_bool = "<true or false>" in prompt
    shots = list(_FEW.finditer(prompt))
    subject = prompt[:shots[0].start()] if shots else prompt
    def out(v, why):
        if want_bool: v = (v >= 0.5) if isinstance(v, (int, float)) and not isinstance(v, bool) else bool(v)
        else: v = float(v)
        return json.dumps({"score": v, "reasoning": why})
    m = re.search(r"\[judge-score:\s*(true|false|[0-9.]+)\s*\]", subject, re.I)
    if m:
        x = m.group(1).lower(); return out((x == "true") if x in ("true", "false") else float(x), f"scripted {x}")
    if "[judge-align]" in subject:
        tp = re.search(r"topic:(\w+)", subject); tp = tp.group(1) if tp else None
        known = {}
        for s in shots:
            t = re.search(r"topic:(\w+)", s.group(1) + " " + s.group(2))
            if t and s.group(4) is not None: known[t.group(1)] = s.group(4).strip().lower() in ("true", "1", "1.0")
        v = known.get(tp, True)
        return out(v, f"fake-align topic={tp} {'from few-shot' if tp in known else 'default lenient'}")
    return out(True, "fake default")
