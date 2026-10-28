"""PowerPoint (.pptx) inspect / edit / validate / render for the virtual computer (python-pptx 1.0.2 + lxml; soffice + poppler for render).
Keeps the original file untouched: `edit` always writes a NEW file (default /workspace/out/<name>-edited.pptx). Slide numbers are 1-based.
  python pptx_tool.py inspect  FILE                      -> JSON: layouts + per slide (layout, shapes {name, kind, text}, notes)
  python pptx_tool.py edit     FILE --ops OPS [--out OUT]  OPS = JSON text or a .json file with a list of ops:
      {"op":"text","slide":2,"shape":"Title 1","text":"Tiêu đề mới"}          set the text of a shape (name or 0-based index), keeps the first run's format
      {"op":"replace","find":"cũ","with":"mới","slide":2}                      across runs; "slide" optional (default = every slide)
      {"op":"add_slide","layout":"Title and Content","title":"...","bullets":["a","  - b"],"notes":"...","at":3}   (layout = name or index; "at" optional)
      {"op":"delete","slide":4}   {"op":"move","slide":4,"to":2}   {"op":"notes","slide":2,"text":"..."}
      {"op":"replace_image","slide":2,"shape":"Picture 3","path":"/workspace/out/new.png"}   (shape optional = the first picture; position and size kept)
  python pptx_tool.py validate FILE                      -> JSON {"ok":bool,"problems":[...],"warnings":[...]}; exit 1 when not ok
  python pptx_tool.py render   FILE [--pages N] [--out DIR] [--dpi 50]   soffice -> PDF -> one PNG thumbnail per slide (poppler)
A slide/PowerPoint request = a .pptx file, never HTML."""
import argparse, json, os, subprocess, sys, tempfile, zipfile

FONT = "Noto Sans"

def _text_shapes(slide): return [sh for sh in slide.shapes if getattr(sh, "has_text_frame", False) and sh.has_text_frame]

def _find_shape(slide, ref):
    shapes = list(slide.shapes)
    if isinstance(ref, int): return shapes[ref]
    hit = next((s for s in shapes if s.name == ref), None)
    if hit is None: hit = next((s for s in shapes if str(ref).lower() in s.name.lower()), None)
    if hit is None: raise ValueError("shape not found: %r. Shapes on this slide: %s" % (ref, [s.name for s in shapes]))
    return hit

def _slide(prs, n):
    n = int(n)
    if not 1 <= n <= len(prs.slides): raise ValueError("slide %d does not exist (the deck has %d slides)" % (n, len(prs.slides)))
    return prs.slides[n - 1]

def _set_tf_text(tf, text):
    """Replace the text, keep the format (font, size, colour) of the first run of the first paragraph."""
    lines = str(text).split("\n"); ps = tf.paragraphs
    first_runs = ps[0].runs
    if first_runs:
        first_runs[0].text = lines[0]
        for r in first_runs[1:]: r._r.getparent().remove(r._r)
    else: ps[0].text = lines[0]
    for p in ps[1:]: p._p.getparent().remove(p._p)
    for ln in lines[1:]:
        np_ = tf.add_paragraph(); src = first_runs[0] if first_runs else None
        r = np_.add_run(); r.text = ln
        if src is not None and src.font.name: r.font.name = src.font.name

def _replace_in_par(p, find, new):
    n = 0; start = 0
    while find:
        runs = p.runs; full = "".join(r.text for r in runs); i = full.find(find, start)
        if i < 0: return n
        j = i + len(find); off = 0; first = None
        for r in runs:
            a, b = off, off + len(r.text); off = b
            if b <= i or a >= j: continue
            lo, hi = max(i, a) - a, min(j, b) - a
            if first is None: first = r; r.text = r.text[:lo] + new + r.text[hi:]
            else: r.text = r.text[:lo] + r.text[hi:]
        n += 1; start = i + len(new)
    return n

def _iter_text_frames(slide):
    for sh in slide.shapes:
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame: yield sh.text_frame
        if getattr(sh, "has_table", False) and sh.has_table:
            for row in sh.table.rows:
                for c in row.cells: yield c.text_frame
    if slide.has_notes_slide: yield slide.notes_slide.notes_text_frame

def op_text(prs, o):
    sh = _find_shape(_slide(prs, o["slide"]), o.get("shape", 0))
    if not getattr(sh, "has_text_frame", False) or not sh.has_text_frame: raise ValueError("shape %r has no text" % sh.name)
    _set_tf_text(sh.text_frame, o["text"]); return 1

def op_replace(prs, o):
    slides = [_slide(prs, o["slide"])] if o.get("slide") else list(prs.slides); n = 0
    for s in slides:
        for tf in _iter_text_frames(s):
            for p in tf.paragraphs: n += _replace_in_par(p, o["find"], o.get("with", ""))
    if n == 0: raise ValueError("replace: text not found: %r (it must sit inside one paragraph)" % o["find"])
    return n

def _layout(prs, ref):
    ls = list(prs.slide_layouts)
    if isinstance(ref, int): return ls[ref]
    hit = next((l for l in ls if l.name == ref), None)
    if hit is None: hit = next((l for l in ls if str(ref).lower() in l.name.lower()), None)
    if hit is None: raise ValueError("layout not found: %r. Layouts: %s" % (ref, [l.name for l in ls]))
    return hit

def _move(prs, frm, to):
    lst = prs.slides._sldIdLst; ids = list(lst); el = ids[frm]
    lst.remove(el); ids = list(lst); to = max(0, min(to, len(ids)))
    if to >= len(ids): lst.append(el)
    else: ids[to].addprevious(el)

def op_add_slide(prs, o):
    from pptx.util import Pt
    s = prs.slides.add_slide(_layout(prs, o.get("layout", 1)))
    if o.get("title") is not None and s.shapes.title is not None: s.shapes.title.text = o["title"]
    if o.get("bullets"):
        body = next((p for p in s.placeholders if p.placeholder_format.idx == 1), None)
        if body is None: raise ValueError("this layout has no body placeholder for bullets; choose another layout")
        tf = body.text_frame; tf.clear()
        for k, b in enumerate(o["bullets"]):
            b = str(b); lvl = 1 if b.startswith(("  -", "\t")) else 0
            p = tf.paragraphs[0] if k == 0 else tf.add_paragraph(); p.text = b.lstrip(" \t-"); p.level = lvl
    if o.get("notes"): s.notes_slide.notes_text_frame.text = o["notes"]
    for tf in _iter_text_frames(s):
        for p in tf.paragraphs:
            for r in p.runs: r.font.name = o.get("font") or FONT
    if o.get("at"): _move(prs, len(prs.slides) - 1, int(o["at"]) - 1)
    return 1

def op_delete(prs, o):
    _slide(prs, o["slide"]); lst = prs.slides._sldIdLst; el = list(lst)[int(o["slide"]) - 1]
    prs.part.drop_rel(el.rId); lst.remove(el); return 1

def op_move(prs, o):
    _slide(prs, o["slide"]); _slide(prs, o["to"]); _move(prs, int(o["slide"]) - 1, int(o["to"]) - 1); return 1

def op_notes(prs, o):
    _slide(prs, o["slide"]).notes_slide.notes_text_frame.text = o["text"]; return 1

def op_replace_image(prs, o):
    from pptx.oxml.ns import qn
    s = _slide(prs, o["slide"])
    pics = [sh for sh in s.shapes if sh.shape_type == 13]
    pic = _find_shape(s, o["shape"]) if o.get("shape") is not None else (pics[0] if pics else None)
    if pic is None or pic.shape_type != 13: raise ValueError("replace_image: no picture found on slide %s" % o["slide"])
    _, rid = s.part.get_or_add_image_part(o["path"])
    pic._element.blipFill.find(qn("a:blip")).set(qn("r:embed"), rid); return 1

OPS = {"text": op_text, "replace": op_replace, "add_slide": op_add_slide, "delete": op_delete, "move": op_move, "notes": op_notes, "replace_image": op_replace_image}

def _load_ops(arg):
    s = open(arg, encoding="utf-8").read() if os.path.isfile(arg) else arg
    ops = json.loads(s)
    return [ops] if isinstance(ops, dict) else ops

def cmd_edit(a):
    from pptx import Presentation
    src = os.path.abspath(a.file)
    out = a.out or os.path.join(os.environ.get("AOS_OUT_DIR", "/workspace/out"), os.path.splitext(os.path.basename(src))[0] + "-edited.pptx")
    if not out.lower().endswith(".pptx"): out += ".pptx"
    if os.path.abspath(out) == src: raise ValueError("--out must differ from the input file: the original stays untouched")
    prs = Presentation(src); res = []
    for k, o in enumerate(_load_ops(a.ops)):
        if o.get("op") not in OPS: raise ValueError("op %d: unknown op %r (use: %s)" % (k, o.get("op"), ", ".join(OPS)))
        res.append({"op": o["op"], "changed": OPS[o["op"]](prs, o)})
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True); prs.save(out)
    bad, warn = validate(out)
    print(json.dumps({"out": out, "results": res, "valid": not bad, "problems": bad, "warnings": warn}, ensure_ascii=False))
    return 1 if bad else 0

def validate(path):
    """Returns (problems, warnings). Problems: bad zip / XML, python-pptx cannot open it, duplicate or dangling slide ids, missing layout.
    Warnings: empty placeholders, shapes outside the slide."""
    from lxml import etree
    pr, warn = [], []
    try: z = zipfile.ZipFile(path)
    except Exception as e: return ["not a zip/pptx file: %s" % e], warn
    if z.testzip(): pr.append("corrupt zip member: %s" % z.testzip())
    names = set(z.namelist())
    for need in ("[Content_Types].xml", "ppt/presentation.xml"):
        if need not in names: pr.append("missing part: %s" % need)
    for n in names:
        if n.endswith(".xml") or n.endswith(".rels"):
            try: etree.fromstring(z.read(n))
            except Exception as e: pr.append("XML not well-formed in %s: %s" % (n, e))
    if pr: return pr, warn
    try:
        from pptx import Presentation
        prs = Presentation(path)
    except Exception as e: return ["python-pptx cannot open it: %s" % e], warn
    ids = [int(s.get("id")) for s in prs.slides._sldIdLst]
    if len(ids) != len(set(ids)): pr.append("duplicate slide ids")
    if any(i < 256 for i in ids): pr.append("slide id below 256")
    rels = prs.part.rels
    for s in prs.slides._sldIdLst:
        if s.rId not in rels: pr.append("slide id %s points to a missing relationship %s" % (s.get("id"), s.rId))
    W, H = prs.slide_width, prs.slide_height
    for k, s in enumerate(prs.slides, 1):
        if s.slide_layout is None: pr.append("slide %d has no layout" % k)
        for sh in s.shapes:
            if getattr(sh, "is_placeholder", False) and getattr(sh, "has_text_frame", False) and sh.has_text_frame and not sh.text_frame.text.strip():
                warn.append("slide %d: empty placeholder %r" % (k, sh.name))
            try:
                if sh.left is not None and (sh.left + sh.width > W * 1.02 or sh.top + sh.height > H * 1.02 or sh.left < 0 or sh.top < 0): warn.append("slide %d: %r is outside the slide" % (k, sh.name))
            except TypeError: pass
    return pr, warn

def cmd_validate(a):
    pr, warn = validate(a.file); print(json.dumps({"ok": not pr, "problems": pr, "warnings": warn}, ensure_ascii=False)); return 1 if pr else 0

def cmd_inspect(a):
    from pptx import Presentation
    prs = Presentation(a.file); slides = []
    for k, s in enumerate(prs.slides, 1):
        shapes = [{"i": i, "name": sh.name, "kind": str(sh.shape_type).split(".")[-1].split(" ")[0] if sh.shape_type else ("PLACEHOLDER" if sh.is_placeholder else "?"),
                   "text": sh.text_frame.text if getattr(sh, "has_text_frame", False) and sh.has_text_frame else ""} for i, sh in enumerate(s.shapes)]
        slides.append({"slide": k, "layout": s.slide_layout.name, "shapes": shapes, "notes": s.notes_slide.notes_text_frame.text if s.has_notes_slide else ""})
    print(json.dumps({"size_in": [round(prs.slide_width / 914400, 2), round(prs.slide_height / 914400, 2)], "layouts": [l.name for l in prs.slide_layouts], "slides": slides}, ensure_ascii=False, indent=1))
    return 0

def cmd_render(a):
    src = os.path.abspath(a.file); outdir = a.out or tempfile.mkdtemp(prefix="pptx-render-"); os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    try:
        subprocess.run(["soffice", "--headless", "--norestore", "-env:UserInstallation=file://" + tempfile.mkdtemp(prefix="lo-profile-"), "--convert-to", "pdf", "--outdir", outdir, src],
                       check=True, capture_output=True, timeout=a.timeout)
    except subprocess.TimeoutExpired: raise RuntimeError("render failed: LibreOffice did not finish in %d s (the .pptx itself is untouched)" % a.timeout)
    except subprocess.CalledProcessError as e: raise RuntimeError("render failed: soffice exit %s %s" % (e.returncode, e.stderr.decode("utf-8", "replace")[:300]))
    pdf = os.path.join(outdir, stem + ".pdf")
    if not os.path.isfile(pdf): raise RuntimeError("render failed: no PDF produced")
    cmd = ["pdftoppm", "-png", "-r", str(a.dpi)] + (["-f", "1", "-l", str(a.pages)] if a.pages else []) + [pdf, os.path.join(outdir, stem)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=a.timeout)
    pngs = sorted(f for f in os.listdir(outdir) if f.endswith(".png") and f.startswith(stem + "-"))
    print(json.dumps({"pdf": pdf, "png": [os.path.join(outdir, f) for f in pngs]}, ensure_ascii=False)); return 0

def main(argv=None):
    ap = argparse.ArgumentParser(prog="pptx_tool.py"); sp = ap.add_subparsers(dest="cmd", required=True)
    for n in ("inspect", "validate"): sp.add_parser(n).add_argument("file")
    e = sp.add_parser("edit"); e.add_argument("file"); e.add_argument("--ops", required=True); e.add_argument("--out")
    r = sp.add_parser("render"); r.add_argument("file"); r.add_argument("--pages", type=int, default=0); r.add_argument("--out"); r.add_argument("--dpi", type=int, default=50); r.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args(argv)
    try:
        return {"inspect": cmd_inspect, "validate": cmd_validate, "edit": cmd_edit, "render": cmd_render}[a.cmd](a)
    except (ValueError, RuntimeError, KeyError, OSError, IndexError) as ex:
        print(json.dumps({"error": str(ex)}, ensure_ascii=False)); return 1

if __name__ == "__main__":
    sys.exit(main())
