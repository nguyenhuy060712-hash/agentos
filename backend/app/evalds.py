"""GD9.Q dataset schema types + transformations (LangSmith "Dataset prebuilt JSON schema types" and "Dataset transformations", checked 24/10/2026).
- JSON Schema validation with the two public LangSmith prebuilt types (message.json, tooldef.json) bundled offline in app/dataset_schemas/ (same $ref URLs work,
  nothing is fetched from the network).
- DATASET_TYPES: one-click schema + transformation presets (LangSmith Chat Model schema + ours: chat, qa, tool_calling, rag).
- Transformations run on every example before it is stored (UI, API, CSV import, from runs, run rules). Items are either a name (legacy, whole inputs+outputs)
  or LangSmith's {"path": ["inputs", "messages"], "transformation_type": "...", ...params}."""
import copy, json
from pathlib import Path
import jsonschema
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT7

LS = "https://api.smith.langchain.com/public/schemas/v1/"
_DIR = Path(__file__).parent / "dataset_schemas"
BUNDLED = {n: json.loads((_DIR / f"{n}.json").read_text(encoding="utf-8")) for n in ("message", "tooldef")}
REGISTRY = Registry().with_resources([(LS + n + ".json", Resource.from_contents(s, default_specification=DRAFT7)) for n, s in BUNDLED.items()])
TRANSFORMS = ("remove_system_messages", "convert_to_openai_message", "convert_to_openai_tool", "extract_tools", "remove_extra_fields", "trim",
              "strip_strings", "lowercase_keys")
_MSG, _TOOL = {"$ref": LS + "message.json"}, {"$ref": LS + "tooldef.json"}

# ------------------------------------------------------------------ validation
def _refs(x):
    if isinstance(x, dict):
        for k, v in x.items():
            if k == "$ref" and isinstance(v, str): yield v
            else: yield from _refs(v)
    elif isinstance(x, list):
        for v in x: yield from _refs(v)

def check_schema(schema):
    """Raises ValueError for a broken schema or a $ref that cannot be resolved offline (only the two bundled LangSmith types are available)."""
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        for ref in _refs(schema):
            if not ref.startswith("#"): REGISTRY.resolver().lookup(ref)
    except jsonschema.SchemaError as e: raise ValueError(f"JSON schema is invalid: {e.message[:200]}") from None
    except Unresolvable as e: raise ValueError(f"unresolved $ref {e}: only {LS}message.json and {LS}tooldef.json are available") from None

def validate(instance, schema):
    """Raises jsonschema.ValidationError (first error, with its path) when the instance does not match."""
    err = next(iter(sorted(jsonschema.Draft202012Validator(schema, registry=REGISTRY).iter_errors(instance), key=lambda e: [str(p) for p in e.path])), None)
    if err is not None: raise err

def problem(instance, schema):
    """-> readable reason or None."""
    try: validate(instance, schema)
    except jsonschema.ValidationError as e: return (("/" + "/".join(str(p) for p in e.path) + ": ") if e.path else "") + e.message[:240]
    except Unresolvable as e: return f"unresolved $ref {e}"
    return None

def _sub_schema(schema, path):
    cur = schema
    for k in path:
        if not isinstance(cur, dict): return None
        if "$ref" in cur: cur = BUNDLED.get(str(cur["$ref"]).rsplit("/", 1)[-1].removesuffix(".json"))
        if not isinstance(cur, dict): return None
        cur = (cur.get("properties") or {}).get(k) if cur.get("type") != "array" else (cur.get("items") or {}).get("properties", {}).get(k)
    if isinstance(cur, dict) and "$ref" in cur: cur = BUNDLED.get(str(cur["$ref"]).rsplit("/", 1)[-1].removesuffix(".json"))
    return cur if isinstance(cur, dict) else None

# ------------------------------------------------------------------ transformations
def normalize(tfs):
    """names / dicts -> [{"path": [...], "transformation_type": ..., **params}]; ValueError for anything unsupported."""
    out = []
    for t in tfs or []:
        if isinstance(t, str): t = {"path": [], "transformation_type": t}
        if not isinstance(t, dict): raise ValueError("transformation must be a name or an object")
        ty, p = t.get("transformation_type") or t.get("type"), t.get("path") or []
        if ty not in TRANSFORMS: raise ValueError(f"unsupported transformation: {ty} (supported: {', '.join(TRANSFORMS)})")
        if not isinstance(p, list) or not all(isinstance(x, str) for x in p) or (p and p[0] not in ("inputs", "outputs")): raise ValueError("path must start with inputs or outputs, e.g. [\"inputs\", \"messages\"]")
        out.append({**{k: v for k, v in t.items() if k not in ("type", "transformation_type", "path")}, "path": p, "transformation_type": ty})
    return out

def to_openai_message(m):
    """LangChain serialisation / OpenAI / plain string -> one OpenAI message dict (langchain_core.convert_to_openai_messages, same function LangSmith names)."""
    try:
        from langchain_core.messages import convert_to_openai_messages
        r = convert_to_openai_messages(m)
        return r[0] if isinstance(r, list) else r
    except Exception:  # noqa: BLE001
        if isinstance(m, str): return {"role": "user", "content": m}
        if not isinstance(m, dict): return m
        role = m.get("role") or {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}.get(m.get("type"), m.get("type") or "user")
        c = m.get("content")
        if isinstance(c, list): c = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
        return {k: v for k, v in {"role": role, "content": c, "tool_calls": m.get("tool_calls"), "tool_call_id": m.get("tool_call_id")}.items() if v is not None}

def to_openai_tool(t):
    try:
        from langchain_core.utils.function_calling import convert_to_openai_tool
        return convert_to_openai_tool(t)
    except Exception: return t  # noqa: BLE001

_SYS = ("system", "developer")
def _is_sys(x): return isinstance(x, dict) and (x.get("role") in _SYS or x.get("type") in _SYS)
def _looks_msg(x): return isinstance(x, dict) and ("role" in x or "type" in x) and "content" in x
_MSG_KEYS = ("messages", "input", "inputs", "output")

def _cut(s, n): return s if len(s) <= n else s[:n] + "…"

def _trim(v, tf):
    n, k = int(tf.get("max_chars") or 4000), tf.get("max_messages")
    if isinstance(v, str): return _cut(v, n)
    if isinstance(v, list):
        if k and v and all(isinstance(x, dict) for x in v):
            sys_ = [x for x in v if _is_sys(x)][:1]; rest = [x for x in v if not _is_sys(x)]
            v = sys_ + rest[-int(k):]
        return [_trim(x, tf) for x in v]
    if isinstance(v, dict): return {a: _trim(b, tf) for a, b in v.items()}
    return v

def _extract_tools(inputs):
    if not isinstance(inputs, dict) or inputs.get("tools"): return inputs
    seen, tools = set(), []
    for m in inputs.get("messages") or []:
        for c in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
            f = c.get("function") or c; n = f.get("name") or c.get("name")
            if n and n not in seen:
                seen.add(n); tools.append({"type": "function", "function": {"name": n, "description": "(extracted from the conversation)", "parameters": {"type": "object", "additionalProperties": True}}})
    return {**inputs, "tools": tools} if tools else inputs

def _apply_one(t, v, tf, schema):
    if t == "remove_system_messages":
        if isinstance(v, list): return [x for x in v if not _is_sys(x)]
        if isinstance(v, dict): return {k: ([x for x in a if not _is_sys(x)] if isinstance(a, list) else a) for k, a in v.items()}
    elif t == "convert_to_openai_message":
        if isinstance(v, list): return [to_openai_message(x) for x in v]
        if _looks_msg(v) or isinstance(v, str): return to_openai_message(v)
        if isinstance(v, dict): return {k: ([to_openai_message(x) for x in a] if k in _MSG_KEYS and isinstance(a, list) else a) for k, a in v.items()}
    elif t == "convert_to_openai_tool":
        if isinstance(v, list): return [to_openai_tool(x) for x in v]
        if isinstance(v, dict): return to_openai_tool(v)
    elif t == "extract_tools": return _extract_tools(v)
    elif t == "remove_extra_fields":
        props = (schema or {}).get("properties")
        if isinstance(v, dict) and isinstance(props, dict): return {k: a for k, a in v.items() if k in props}
    elif t == "trim": return _trim(v, tf)
    elif t == "strip_strings" and isinstance(v, dict): return {k: (a.strip() if isinstance(a, str) else a) for k, a in v.items()}
    elif t == "lowercase_keys" and isinstance(v, dict): return {k.lower(): a for k, a in v.items()}
    return v

_USER_KEYS, _AI_KEYS = ("question", "message", "input", "q", "prompt"), ("answer", "output", "text", "content", "reply")

def apply(inputs, outputs, tfs, in_schema=None, out_schema=None):
    """-> (inputs, outputs) after every transformation (copies; the arguments are not modified)."""
    data = {"inputs": copy.deepcopy(inputs), "outputs": copy.deepcopy(outputs)}; sch = {"inputs": in_schema, "outputs": out_schema}
    for tf in normalize(tfs):
        p, t = tf["path"], tf["transformation_type"]
        for root in ([p[0]] if p else ["inputs", "outputs"]):
            d = data[root]
            if d is None: continue
            sub = p[1:]
            if not sub: data[root] = _apply_one(t, d, tf, sch[root]); continue
            cur = d
            for k in sub[:-1]: cur = cur.get(k) if isinstance(cur, dict) else None
            last = sub[-1]
            if not isinstance(cur, dict): continue
            if last not in cur and t == "convert_to_openai_message":   # derive the message(s) from a well-known field, remove that field
                src = next((k for k in (_USER_KEYS if root == "inputs" else _AI_KEYS) if isinstance(cur.get(k), str)), None)
                if src and cur is d:
                    txt = cur.pop(src); m = {"role": "user" if root == "inputs" else "assistant", "content": txt}
                    cur[last] = [m] if last == "messages" else m
                continue
            if last in cur: cur[last] = _apply_one(t, cur[last], tf, _sub_schema(sch[root], sub))
    return data["inputs"], data["outputs"]

# ------------------------------------------------------------------ prebuilt dataset types
_TF_CHAT = [{"path": ["inputs"], "transformation_type": "remove_extra_fields"}, {"path": ["inputs", "messages"], "transformation_type": "convert_to_openai_message"},
            {"path": ["inputs", "tools"], "transformation_type": "convert_to_openai_tool"}, {"path": ["outputs"], "transformation_type": "remove_extra_fields"},
            {"path": ["outputs", "message"], "transformation_type": "convert_to_openai_message"}]
DATASET_TYPES = {
    "kv": {"name": "Key-value (free form)", "name_vi": "Tự do (khoá-giá trị)", "description": "No schema: any JSON inputs and outputs.", "description_vi": "Không ràng buộc: JSON bất kỳ.",
           "inputs_schema": None, "outputs_schema": None, "transformations": [], "example": {"inputs": {"question": "..."}, "outputs": {"answer": "..."}}},
    "chat_model": {"name": "Chat model (LangSmith)", "name_vi": "Chat model (chuẩn LangSmith)", "description": "LangSmith Chat Model schema: OpenAI messages (+ tools) in, one message out; messages and tools are converted to the OpenAI format.",
                   "description_vi": "Chuẩn Chat Model của LangSmith: tin nhắn kiểu OpenAI (+ tools) vào, một tin nhắn ra; tự chuyển về định dạng OpenAI.",
                   "inputs_schema": {"type": "object", "properties": {"messages": {"type": "array", "items": _MSG}, "tools": {"type": "array", "items": _TOOL}}, "required": ["messages"]},
                   "outputs_schema": {"type": "object", "properties": {"message": _MSG}, "required": ["message"]}, "transformations": _TF_CHAT,
                   "example": {"inputs": {"messages": [{"role": "user", "content": "Hi"}]}, "outputs": {"message": {"role": "assistant", "content": "Hello!"}}}},
    "chat": {"name": "Chat messages", "name_vi": "Hội thoại (messages)", "description": "A conversation (OpenAI messages) in, the expected next message out. System messages are removed so prompts can change freely.",
             "description_vi": "Hội thoại (tin nhắn kiểu OpenAI) vào, tin nhắn đúng tiếp theo ra. Tự bỏ system message để sửa prompt thoải mái.",
             "inputs_schema": {"type": "object", "properties": {"messages": {"type": "array", "items": _MSG, "minItems": 1}}, "required": ["messages"]},
             "outputs_schema": {"type": "object", "properties": {"message": _MSG}, "required": ["message"]},
             "transformations": [{"path": ["inputs", "messages"], "transformation_type": "convert_to_openai_message"}, {"path": ["inputs", "messages"], "transformation_type": "remove_system_messages"},
                                 {"path": ["outputs", "message"], "transformation_type": "convert_to_openai_message"}],
             "example": {"inputs": {"messages": [{"role": "user", "content": "Giá gói Pro?"}]}, "outputs": {"message": {"role": "assistant", "content": "499k/tháng"}}}},
    "qa": {"name": "Question and answer", "name_vi": "Hỏi - đáp", "description": "A question in, the reference answer out (the shape the correctness evaluators expect).",
           "description_vi": "Câu hỏi vào, đáp án mẫu ra (dạng các evaluator đúng/sai cần).",
           "inputs_schema": {"type": "object", "properties": {"question": {"type": "string", "minLength": 1}}, "required": ["question"]},
           "outputs_schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
           "transformations": [{"path": ["inputs"], "transformation_type": "strip_strings"}, {"path": ["outputs"], "transformation_type": "strip_strings"}],
           "example": {"inputs": {"question": "Giá gói Pro?"}, "outputs": {"answer": "499k/tháng"}}},
    "tool_calling": {"name": "Tool calling", "name_vi": "Gọi công cụ", "description": "A conversation + the tool definitions in, the tool calls the agent should make out (works with trajectory evaluators).",
                     "description_vi": "Hội thoại + định nghĩa công cụ vào, các lệnh gọi công cụ agent phải thực hiện ra (dùng với evaluator quỹ đạo).",
                     "inputs_schema": {"type": "object", "properties": {"messages": {"type": "array", "items": _MSG, "minItems": 1}, "tools": {"type": "array", "items": _TOOL}}, "required": ["messages"]},
                     "outputs_schema": {"type": "object", "properties": {"tool_calls": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "args": {"type": "object"}}, "required": ["name"]}},
                                                                         "trajectory": {"type": "array"}}, "required": ["tool_calls"]},
                     "transformations": [{"path": ["inputs", "messages"], "transformation_type": "convert_to_openai_message"}, {"path": ["inputs", "tools"], "transformation_type": "convert_to_openai_tool"},
                                         {"path": ["inputs"], "transformation_type": "extract_tools"}],
                     "example": {"inputs": {"messages": [{"role": "user", "content": "Weather in Hanoi?"}]}, "outputs": {"tool_calls": [{"name": "get_weather", "args": {"city": "Hanoi"}}]}}},
    "rag": {"name": "RAG (question, answer, contexts)", "name_vi": "RAG (câu hỏi, đáp án, ngữ cảnh)", "description": "A question in; the reference answer and, optionally, the reference passages out. Pairs with the RAG evaluators (correctness, groundedness, retrieval relevance).",
            "description_vi": "Câu hỏi vào; đáp án mẫu và (tuỳ chọn) các đoạn tài liệu mẫu ra. Dùng với bộ evaluator RAG (đúng, bám tài liệu, truy xuất liên quan).",
            "inputs_schema": {"type": "object", "properties": {"question": {"type": "string", "minLength": 1}}, "required": ["question"]},
            "outputs_schema": {"type": "object", "properties": {"answer": {"type": "string"}, "contexts": {"type": "array", "items": {"type": "string"}}}, "required": ["answer"]},
            "transformations": [{"path": ["inputs"], "transformation_type": "strip_strings"}, {"path": ["outputs"], "transformation_type": "trim", "max_chars": 8000}],
            "example": {"inputs": {"question": "Chính sách đổi trả bao lâu?"}, "outputs": {"answer": "30 ngày kể từ ngày mua", "contexts": ["Khách được đổi trả trong 30 ngày kể từ ngày mua."]}}},
}
