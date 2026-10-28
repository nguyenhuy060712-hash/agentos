"""PowerPoint (.pptx) from a JSON spec (python-pptx 1.0.2), 16:9, Vietnamese-safe font on every text run.
Use:  import sys; sys.path.insert(0, "/memories/skills/pptx/scripts"); from make_pptx import build
      path = build({"title": "Báo cáo quý 3", "subtitle": "Phòng kinh doanh",
                    "slides": [{"title": "Điểm chính", "bullets": ["Doanh thu tăng 12%", "Đà Nẵng dẫn đầu"]},
                               {"title": "Số liệu", "table": [["Chi nhánh", "Q3"], ["Hà Nội", "1.200"]]},
                               {"title": "Biểu đồ", "image": "/workspace/out/bieu-do.png"}]}, "bao-cao-q3")
Slide keys: title, bullets (list; "  - x" = level 2), table (rows, first = header), image (path), notes (speaker notes).
CLI:  python make_pptx.py <spec.json> <name>"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_common"))
from mdlite import out_path, load_spec  # noqa: E402

FONT = "Noto Sans"

def _fontify(shape, font):
    if not getattr(shape, "has_text_frame", False) or not shape.has_text_frame: return
    for p in shape.text_frame.paragraphs:
        for r in p.runs: r.font.name = font

def build(spec, name: str = "trinh-chieu") -> str:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    font = spec.get("font") or FONT
    prs = Presentation(); prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    W, H = prs.slide_width, prs.slide_height
    if spec.get("title"):
        s = prs.slides.add_slide(prs.slide_layouts[0])
        s.shapes.title.text = spec["title"]; s.placeholders[1].text = spec.get("subtitle", "")
        for sh in s.shapes: _fontify(sh, font)
    for sd in spec.get("slides") or []:
        s = prs.slides.add_slide(prs.slide_layouts[1] if sd.get("bullets") else prs.slide_layouts[5])
        s.shapes.title.text = sd.get("title", "")
        if sd.get("bullets"):
            body = s.placeholders[1]; body.left, body.top, body.width, body.height = Inches(0.7), Inches(1.6), W - Inches(1.4), H - Inches(2.2)
            tf = body.text_frame; tf.clear()
            for k, b in enumerate(sd["bullets"]):
                b = str(b); lvl = 1 if b.startswith(("  -", "\t")) else 0
                p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
                p.text = b.lstrip(" \t-"); p.level = lvl; p.font.size = Pt(24 if lvl == 0 else 20)
        if sd.get("table"):
            rows = sd["table"]; nc = max(len(r) for r in rows)
            t = s.shapes.add_table(len(rows), nc, Inches(0.7), Inches(1.6), W - Inches(1.4), Inches(0.5) * len(rows)).table
            for i, r in enumerate(rows):
                for j, v in enumerate(r):
                    t.cell(i, j).text = str(v)
                    for p in t.cell(i, j).text_frame.paragraphs:
                        for run in p.runs: run.font.name = font; run.font.size = Pt(16)
        if sd.get("image"):
            s.shapes.add_picture(sd["image"], Inches(1.5), Inches(1.6), height=H - Inches(2.2))
        if sd.get("notes"): s.notes_slide.notes_text_frame.text = sd["notes"]
        for sh in s.shapes: _fontify(sh, font)
    p = out_path(name, "pptx"); prs.save(p)
    return p

if __name__ == "__main__":
    print(build(load_spec(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "trinh-chieu"))
