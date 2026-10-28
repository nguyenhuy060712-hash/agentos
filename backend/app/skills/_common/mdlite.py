"""Tiny Markdown -> blocks parser shared by the file-skill helpers (docx / pdf). Stdlib only, runs inside the sandbox.
Supported: # headings (1-4), paragraphs, - / * / 1. lists, | tables | (first row = header), blank-line breaks, **bold** inline."""
import re

def blocks(md: str) -> list[dict]:
    out, para, lines = [], [], (md or "").replace("\r\n", "\n").split("\n")
    def flush():
        if para: out.append({"type": "paragraph", "text": " ".join(x.strip() for x in para)}); para.clear()
    i = 0
    while i < len(lines):
        s = lines[i].rstrip()
        if not s.strip(): flush(); i += 1; continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m: flush(); out.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2).strip()}); i += 1; continue
        if s.lstrip().startswith("|"):
            flush(); rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c): rows.append(cells)
                i += 1
            out.append({"type": "table", "rows": rows, "header": True}); continue
        m = re.match(r"^\s*([-*]|\d+[.)])\s+(.*)$", s)
        if m:
            flush(); kind = "numbers" if m.group(1)[0].isdigit() else "bullets"; items = []
            while i < len(lines) and (mm := re.match(r"^\s*([-*]|\d+[.)])\s+(.*)$", lines[i])):
                items.append(mm.group(2).strip()); i += 1
            out.append({"type": kind, "items": items}); continue
        para.append(s); i += 1
    flush()
    return out

def runs(text: str) -> list[tuple[str, bool]]:
    """'a **b** c' -> [('a ', False), ('b', True), (' c', False)]"""
    parts = re.split(r"\*\*(.+?)\*\*", text or "")
    return [(p, k % 2 == 1) for k, p in enumerate(parts) if p]

OUT_DIR = "/workspace/out"   # files written here are shown to the user as download cards (chat_gd5b collects them after the run)

def out_path(name: str, ext: str) -> str:
    """'bao-cao' -> /workspace/out/bao-cao.<ext>; a path with a folder is kept (relative ones go under OUT_DIR); parent folders are created."""
    import os
    base = os.environ.get("AOS_OUT_DIR", OUT_DIR)
    p = name if os.path.isabs(name) else os.path.join(base, name)
    if not p.lower().endswith("." + ext): p += "." + ext
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p

def load_spec(arg: str):
    """CLI helper: a path to a .json / .md file, or the JSON / Markdown text itself."""
    import json, os
    s = open(arg, encoding="utf-8").read() if os.path.isfile(arg) else arg
    try: return json.loads(s)
    except ValueError: return s
