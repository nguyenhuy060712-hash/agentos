"""Word (.docx) from Markdown or a JSON spec (python-docx 1.2). Vietnamese-safe: one Unicode font (Noto Sans) on every style.
Use:  import sys; sys.path.insert(0, "/memories/skills/docx/scripts"); from make_docx import build
      path = build("# Tiêu đề\\n\\nĐoạn văn...", "bao-cao")          # -> /workspace/out/bao-cao.docx
      path = build({"title": "...", "blocks": [...]}, "bao-cao")
Blocks: {"type":"heading","text","level"} {"type":"paragraph","text"} {"type":"bullets"|"numbers","items":[...]}
        {"type":"table","rows":[[...],...],"header":true} {"type":"image","path","width_cm"} {"type":"page_break"}
CLI:  python make_docx.py <spec.json|spec.md|text> <name>"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_common"))
from mdlite import blocks as md_blocks, runs, out_path, load_spec  # noqa: E402

FONT = "Noto Sans"

def _font(style_or_run, name):
    from docx.oxml.ns import qn
    style_or_run.font.name = name
    rpr = style_or_run.element.get_or_add_rPr() if hasattr(style_or_run.element, "get_or_add_rPr") else style_or_run.element.rPr
    if rpr is None: return
    rf = rpr.find(qn("w:rFonts"))
    if rf is None: rf = rpr.makeelement(qn("w:rFonts"), {}); rpr.append(rf)
    for k in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"): rf.set(qn(k), name)

def _text(par, text):
    for t, bold in runs(text):
        r = par.add_run(t); r.bold = bold or None

def build(spec, name: str = "tai-lieu") -> str:
    from docx import Document
    from docx.shared import Cm, Pt
    if isinstance(spec, str): spec = {"blocks": md_blocks(spec)}
    font = spec.get("font") or FONT
    doc = Document()
    for st in doc.styles:
        if getattr(st, "font", None) is not None and st.type == 1: _font(st, font)   # paragraph styles (Normal, Heading 1-9, Title, List …)
    doc.styles["Normal"].font.size = Pt(spec.get("font_size", 11))
    if spec.get("title"): doc.add_heading(spec["title"], 0)
    for b in spec.get("blocks") or []:
        t = b.get("type")
        if t == "heading": doc.add_heading(b.get("text", ""), min(max(int(b.get("level", 1)), 1), 4))
        elif t == "paragraph": _text(doc.add_paragraph(), b.get("text", ""))
        elif t in ("bullets", "numbers"):
            for it in b.get("items") or []: _text(doc.add_paragraph(style="List Bullet" if t == "bullets" else "List Number"), str(it))
        elif t == "table":
            rows = b.get("rows") or []
            if not rows: continue
            tb = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows)); tb.style = "Table Grid"
            for i, r in enumerate(rows):
                for j, v in enumerate(r):
                    c = tb.cell(i, j); c.text = ""; _text(c.paragraphs[0], str(v))
                    if i == 0 and b.get("header", True):
                        for run in c.paragraphs[0].runs: run.bold = True
        elif t == "image" and b.get("path"): doc.add_picture(b["path"], width=Cm(float(b.get("width_cm", 15))))
        elif t == "page_break": doc.add_page_break()
    p = out_path(name, "docx"); doc.save(p)
    return p

if __name__ == "__main__":
    print(build(load_spec(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "tai-lieu"))
