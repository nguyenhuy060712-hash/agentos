"""GĐ3.1 Generative UI (render-only). Backend half of the chat UI's `present` tool (@assistant-ui/generative-ui 0.0.6, MIT).

The model calls `present` with a `{_type, ...props, children}` tree; the tool runs NO model-written code: it only checks the tree
against the shared schema (genui_schema.json, exported by agentos-chat/scripts/export-present-schema.ts) plus size/depth limits and
answers "shown". The chat UI renders the tool-call ARGS with the same component library. Invalid trees get a short error back
so the model can retry once or answer in plain text.

Enabled per agent by listing "local:present" in its tools. Global kill switch (rollback): AOS_GENUI=0 -> the tool is not offered
and the UI falls back to text."""
from __future__ import annotations
import json, os
from pathlib import Path
from langchain_core.tools import StructuredTool

ENABLED = os.environ.get("AOS_GENUI", "1") != "0"
_RAW = json.loads((Path(__file__).with_name("genui_schema.json")).read_text())
COMPONENTS = set(_RAW["components"])
MAX_BYTES, MAX_DEPTH, MAX_NODES = 48_000, 8, 400

def _flat(schema: dict) -> dict:
    """Model-facing schema without $defs/$ref (Gemini and OpenAI both accept it): nested nodes are plain objects."""
    s = json.loads(json.dumps(schema)); s.pop("$defs", None)
    s["properties"]["children"] = {"description": "Nested components: a string, one node or a list of nodes. Every node has `_type` and its props "
                                   "(same shape as this root object), optionally `children`.",
                                   "anyOf": [{"type": "string"}, {"type": "object"}, {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}}]}
    return _no_type_lists(s)

def _no_type_lists(x):
    """Gemini rejects JSON-schema type lists ("type": ["string","number"] -> 'Invalid type'); the equivalent anyOf works on both providers."""
    if isinstance(x, list): return [_no_type_lists(i) for i in x]
    if not isinstance(x, dict): return x
    x = {k: _no_type_lists(v) for k, v in x.items()}
    if isinstance(x.get("type"), list):
        ts = x.pop("type"); x["anyOf"] = [{"type": t} for t in ts] + [a for a in x.get("anyOf", [])]
    return x

MODEL_SCHEMA = _flat(_RAW["parameters"])
try:
    import jsonschema as _js
    _VALIDATOR = _js.Draft202012Validator(_RAW["parameters"])
except Exception:  # noqa: BLE001  jsonschema missing -> structural checks below still apply
    _VALIDATOR = None

GUIDE = ("\n\n## Trình bày (generative UI)\nKhi câu trả lời có số liệu, bảng, so sánh nhiều lựa chọn, thông số, quy trình hoặc các bước, hãy gọi tool `present` "
         "MỘT lần để hiển thị. Chọn khối theo loại nội dung:\n"
         "- xu hướng / tỷ trọng / số theo nhóm: Chart (variant bar/line/area, data=[{label,value}])\n"
         "- dữ liệu nhiều cột: Table (columns=[{label}], rows=[[...]])\n"
         "- chọn giữa 2-4 phương án: Comparison (options + criteria + verdict)\n"
         "- hồ sơ / thông số: SpecSheet; vài chỉ số chính: Row gồm các Fact\n"
         "- QUY TRÌNH, các bước, cách làm, workflow, lộ trình triển khai: Steps (steps=[{title, detail}], 3-8 bước, title ngắn, detail 1 câu); "
         "có mốc thời gian (ngày, tuần, tháng) thì dùng Timeline (items=[{time, title, detail}]). Không trả lời quy trình bằng danh sách chữ dài. "
         "Nếu quy trình có rẽ nhánh hoặc vòng lặp (điều kiện, duyệt/không duyệt), thay vào đó viết sơ đồ trong câu trả lời bằng khối ```mermaid flowchart TD```.\n"
         "Bọc nhiều khối trong một Col. Sau đó viết 1-3 câu tóm tắt bằng chữ. Không dùng present cho câu trả lời ngắn chỉ có chữ (chào hỏi, cảm ơn, định nghĩa một câu). "
         "Không bịa số liệu: nếu không có dữ liệu thật thì nói rõ là ví dụ minh hoạ.\n"
         "Khi người dùng chỉ nhờ hiển thị, vẽ, trình bày, so sánh hoặc phân tích số liệu họ đưa ra (ví dụ phân bổ ngân sách theo %), CHỈ dùng `present`; "
         "KHÔNG gọi tool tạo, ghi, đăng hay chi tiền (như ads_create_campaign, content_publish, crm_log_deal) trừ khi họ yêu cầu rõ ràng tạo / chạy / đăng / lưu.")

def check(tree) -> str | None:
    """None if the tree is renderable, else a short reason (returned to the model)."""
    if not isinstance(tree, dict): return "root must be an object with _type"
    raw = json.dumps(tree, ensure_ascii=False, default=str)
    if len(raw.encode()) > MAX_BYTES: return f"too large ({len(raw.encode())} bytes > {MAX_BYTES})"
    n = 0
    def walk(node, d):
        nonlocal n
        if isinstance(node, str): return None
        if isinstance(node, list):
            for x in node:
                e = walk(x, d)
                if e: return e
            return None
        if not isinstance(node, dict): return "children must be strings or nodes"
        n += 1
        if n > MAX_NODES: return f"too many nodes (> {MAX_NODES})"
        if d > MAX_DEPTH: return f"nested too deep (> {MAX_DEPTH})"
        t = node.get("_type", node.get("$type"))
        if t not in COMPONENTS: return f"unknown _type {t!r}; use one of: {', '.join(sorted(COMPONENTS))}"
        for k in ("url",):
            v = node.get(k)
            if v is not None and not (isinstance(v, str) and v.startswith(("http://", "https://"))): return "url must be http(s)"
        return walk(node.get("children"), d + 1) if "children" in node else None
    e = walk(tree, 1)
    if e: return e
    if _VALIDATOR is not None:
        errs = sorted(_VALIDATOR.iter_errors(tree), key=lambda x: len(list(x.path)))
        if errs: return f"schema: {'/'.join(map(str, errs[0].path)) or '(root)'}: {errs[0].message[:160]}"
    return None

async def _present(**tree) -> str:
    e = check(tree)
    return f"NOT shown: {e}. Fix the tree and call present again, or answer in text." if e else "Shown to the user. Now add a 1-3 sentence text summary; do not repeat the data."

def tools(agent_id) -> dict:
    if not ENABLED: return {}
    t = StructuredTool.from_function(coroutine=_present, name="present", description=_RAW["description"] +
                                     " Render-only: shows charts, tables, comparisons, spec sheets, steps to the user in the chat.",
                                     args_schema=MODEL_SCHEMA)
    return {"local:present": t}

def prompt_for(tool_keys) -> str:
    return GUIDE if ENABLED and "local:present" in (tool_keys or []) else ""
