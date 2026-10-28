"""GĐ12.16 trace query syntax: `parse_query(text)` turns what a person types in the Tracing search box into the function DSL of `obs.compile_filter`, so every old caller
(UI chips, rules, alerts, nlsearch, API) keeps working: this module only WRITES that DSL, it never touches SQL.
  status:error          eq(status,"error")                  -tag:test        not(has(tags,"test"))
  name:~agent           like(name,"%agent%")  (contains)    model:gemini*    like(model,"gemini%")  (prefix)
  metadata.customer:*   exists(metadata.customer)           latency:[2 TO 10]  and(gte(latency,2), lte(latency,10))   ([* TO 10] / [2 TO *] allowed)
  tool:web_search       tool("web_search")                  attachments:*    has(attachments)
  feedback.score:1      eq(feedback.score,1)                feedback.k.comment:~late  like(feedback.k.comment,"%late%")
  bare words            joined with AND into ONE search("w1 w2")   ("quoted phrase" also gives its words)
Values with spaces: name:"my agent". Unknown field -> 400 with the position and a "did you mean" hint. Values are only ever quoted with json.dumps; the SQL parameters are made by compile_filter."""
import difflib, json, math, re
from fastapi import HTTPException

NUMERIC = ("latency", "latency_ms", "total_tokens", "input_tokens", "output_tokens", "cost", "depth")
TIME_FIELDS = ("start_time", "first_token_time")
PATH_ROOTS = ("metadata", "inputs", "outputs")
EXTRA = ("tag", "tags", "tool", "attachments")
FB_COLS = ("value", "comment", "source", "score")
LOOKS = re.compile(r"(?:^|\s)-?[A-Za-z_][\w.\-]*:\S")   # the box text holds at least one `field:value` (UI sends it as `query=` instead of search())
TERM = re.compile(r'(?P<neg>-)?(?P<field>[A-Za-z_][\w.\-]*):(?P<val>\[[^\]]*\]|"(?:[^"\\]|\\.)*"|\S*)')
BARE = re.compile(r'"(?:[^"\\]|\\.)*"|\S+')

class QueryError(HTTPException):
    def __init__(self, msg, pos=0, suggestion=None):
        super().__init__(400, msg); self.position, self.suggestion = pos, suggestion

def looks_like_query(text) -> bool: return bool(text and LOOKS.search(text))

def _q(v) -> str: return json.dumps(str(v), ensure_ascii=False)

def _esc(v) -> str:   # LIKE wildcards typed by the user are data, not wildcards
    return str(v).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def _num(s, pos):
    try:
        f = float(str(s).strip())
        if not math.isfinite(f): raise ValueError
    except ValueError: raise QueryError(f"Giá trị số không hợp lệ: {s!r} (vị trí {pos}) / Not a number: {s!r}", pos) from None
    return str(int(f)) if f == int(f) else repr(f)

def _kind(field, pos):
    """('col'|'tag'|'path'|'feedback'|'tool'|'attachments', numeric?) or QueryError with a suggestion."""
    import obs
    if field in obs.FIELDS: return "col", field in NUMERIC
    if field in ("tag", "tags"): return "tag", False
    if field in ("tool", "attachments"): return field, False
    root, _, rest = field.partition(".")
    if root in PATH_ROOTS and rest: return "path", False
    if root == "feedback" and rest:
        parts = rest.rsplit(".", 1)
        return "feedback", (parts[1] == "score") if len(parts) == 2 and parts[1] in FB_COLS else True   # plain feedback.<key> = the numeric score
    cands = [*obs.FIELDS, *EXTRA, "metadata.<khoá>", "inputs.<đường dẫn>", "outputs.<đường dẫn>", "feedback.<khoá>"]
    m = difflib.get_close_matches(field, [c for c in cands if "<" not in c], n=1, cutoff=0.6)
    hint = f" - có phải {m[0]!r}? / did you mean {m[0]!r}?" if m else ""
    raise QueryError(f"Trường không có: {field!r} (vị trí {pos}){hint} Dùng: {', '.join(cands)} / Unknown field", pos, m[0] if m else None)

def _one(field, val, pos):
    """DSL for one `field:value` (without negation)."""
    kind, numeric = _kind(field, pos)
    if val == "":
        raise QueryError(f"Thiếu giá trị sau {field}: (vị trí {pos}) / Missing value after {field}:", pos)
    if val.startswith("["):   # range
        m = re.fullmatch(r"\[\s*(\S+)\s+TO\s+(\S+)\s*\]", val)
        if not m or not (numeric or field in TIME_FIELDS):
            raise QueryError(f"Khoảng [a TO b] chỉ dùng cho trường số hoặc thời gian: {field!r} (vị trí {pos}) / Ranges need a numeric or time field", pos)
        lo, hi = m.groups(); out = []
        for b, op in ((lo, "gte"), (hi, "lte")):
            if b == "*": continue
            out.append(f"{op}({field},{_num(b, pos) if numeric else _q(b.strip(chr(34)))})")
        if not out: raise QueryError(f"Khoảng rỗng (vị trí {pos}) / Empty range", pos)
        return out[0] if len(out) == 1 else "and(" + ", ".join(out) + ")"
    quoted = val.startswith('"')
    if quoted:
        try: val = json.loads(val)
        except ValueError: raise QueryError(f"Chuỗi trong ngoặc kép không hợp lệ (vị trí {pos}) / Bad quoted string", pos) from None
    if not quoted and val == "*":
        if kind == "attachments": return "has(attachments)"
        if kind == "tool": raise QueryError(f"tool: cần tên công cụ (vị trí {pos}) / tool: needs a tool name", pos)
        return f"exists({field})"
    contains = not quoted and val.startswith("~"); prefix = not quoted and not contains and val.endswith("*") and len(val) > 1
    if contains: val = val[1:]
    if prefix: val = val[:-1]
    if val == "": raise QueryError(f"Thiếu giá trị sau {field}: (vị trí {pos}) / Missing value after {field}:", pos)
    if kind == "attachments":
        if val.lower() in ("true", "yes", "1"): return "has(attachments)"
        raise QueryError(f"attachments chỉ nhận * hoặc true (vị trí {pos}) / attachments takes * or true", pos)
    if kind == "tool":
        if contains or prefix: raise QueryError(f"tool: chỉ nhận tên đầy đủ (vị trí {pos}) / tool: takes a full name", pos)
        return f"tool({_q(val)})"
    if kind == "tag":
        if contains or prefix: raise QueryError(f"tag: chỉ nhận tên đầy đủ (vị trí {pos}) / tag: takes a full name", pos)
        return f"has(tags,{_q(val)})"
    if contains: return f"like({field},{_q('%' + _esc(val) + '%')})"
    if prefix: return f"like({field},{_q(_esc(val) + '%')})"
    return f"eq({field},{_num(val, pos) if numeric else _q(val)})"

def parse_query(text: str) -> str:
    """The function DSL for `text` ('' for an empty box). Raises QueryError (HTTP 400) with `.position` / `.suggestion`."""
    text = text or ""; pos, parts, words = 0, [], []
    while pos < len(text):
        if text[pos].isspace(): pos += 1; continue
        m = TERM.match(text, pos)
        if m:
            d = _one(m.group("field"), m.group("val"), pos + 1)
            parts.append(f"not({d})" if m.group("neg") else d); pos = m.end(); continue
        m = BARE.match(text, pos); w = m.group(0)
        words.append(json.loads(w) if w.startswith('"') and w.endswith('"') and len(w) > 1 else w); pos = m.end()
    if words: parts.append(f"search({_q(' '.join(words))})")
    return "" if not parts else parts[0] if len(parts) == 1 else "and(" + ", ".join(parts) + ")"

def both(filter_: str, query: str) -> str:
    """AND of an existing DSL string and a parsed query text (either may be empty)."""
    q = parse_query(query); f = (filter_ or "").strip()
    return f"and({f}, {q})" if f and q else f or q
