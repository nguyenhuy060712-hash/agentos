"""Word (.docx) inspect / edit / validate / render for the virtual computer (python-docx 1.2 + lxml; soffice + poppler for render).
Keeps the original file untouched: `edit` always writes a NEW file (default /workspace/out/<name>-edited.docx).
  python docx_tool.py inspect  FILE                      -> JSON: paragraphs (index, style, text), tables, tracked-change counts
  python docx_tool.py to-md    FILE                      -> Markdown on stdout
  python docx_tool.py edit     FILE --ops OPS [--out OUT]  OPS = JSON text or a .json file with a list of ops:
      {"op":"replace","find":"cũ","with":"mới","track":false}       text across runs, keeps the format of the first matched run
      {"op":"insert_after_heading","heading":"Giới thiệu","text":"...","track":false}    ("text" may be a list = several paragraphs)
      {"op":"add_table_rows","table":0,"rows":[["a","b"]],"track":false}                  (copies the format of the last row)
      "track":true = real Word tracked change (w:ins / w:del), author "Agent OS"; the user accepts or rejects it in Word / LibreOffice.
  python docx_tool.py validate FILE                      -> JSON {"ok":bool,"problems":[...]}; exit 1 when not ok
  python docx_tool.py render   FILE [--pages 1] [--out DIR]   soffice -> PDF -> PNG (poppler); prints the PNG paths
Vietnamese: all text is UTF-8; new runs inherit the font of the text they replace."""
import argparse, copy, datetime, json, os, re, subprocess, sys, tempfile, zipfile

AUTHOR = "Agent OS"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

def q(tag): return "{%s}%s" % (W, tag)

def _now(): return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class Ids:
    """Unique w:id for tracked changes (above every id already in the document)."""
    def __init__(self, root):
        ids = [int(v) for el in root.iter() for k, v in el.attrib.items() if k == q("id") and str(v).isdigit()]
        self.n = max(ids + [900])
    def next(self): self.n += 1; return str(self.n)

def _mark(el, ids, tag):
    return el.makeelement(q(tag), {q("id"): ids.next(), q("author"): AUTHOR, q("date"): _now()})

def _run_text(r): return "".join(t.text or "" for t in r.findall(q("t")))

def _set_run_text(r, text):
    ts = r.findall(q("t"))
    for t in ts[1:]: r.remove(t)
    if ts: t = ts[0]
    else: t = r.makeelement(q("t"), {}); r.append(t)
    t.text = text; t.set(XML_SPACE, "preserve")

def _simple_runs(p): return [r for r in p if r.tag == q("r")]          # direct runs only (text inside existing w:ins / hyperlinks is left alone)

def _split_at(p, runs, pos):
    """Make `pos` (character offset into the direct runs) a run boundary; returns the refreshed run list."""
    off = 0
    for r in runs:
        n = len(_run_text(r))
        if off < pos < off + n:
            k = pos - off; txt = _run_text(r); r2 = copy.deepcopy(r)
            _set_run_text(r, txt[:k]); _set_run_text(r2, txt[k:]); r.addnext(r2)
            return _simple_runs(p)
        off += n
    return runs

def _replace_in_par(p, find, new, track, ids):
    count = 0; start_from = 0
    while find and count < 10000:
        runs = _simple_runs(p); full = "".join(_run_text(r) for r in runs)
        i = full.find(find, start_from)
        if i < 0: return count
        j = i + len(find)
        runs = _split_at(p, runs, i); runs = _split_at(p, runs, j)
        hit, off = [], 0
        for r in runs:
            n = len(_run_text(r))
            if n and off >= i and off + n <= j: hit.append(r)
            off += n
        first = hit[0]
        newrun = copy.deepcopy(first); _set_run_text(newrun, new)
        if track:
            d = _mark(p, ids, "del"); first.addprevious(d)
            for r in hit:
                for t in r.findall(q("t")): t.tag = q("delText"); t.set(XML_SPACE, "preserve")
                d.append(r)
            if new:
                ins = _mark(p, ids, "ins"); ins.append(newrun); d.addnext(ins)
            start_from = i                       # old text left the direct runs; the new text sits inside w:ins (not searched again)
        else:
            first.addprevious(newrun)
            for r in hit: p.remove(r)
            start_from = i + len(new)
        count += 1
    return count

def _all_pars(doc): return list(doc.element.body.iter(q("p")))

def _par_text(p): return "".join(t.text or "" for t in p.iter(q("t")))

def _style(p):
    ps = p.find(q("pPr")); s = ps.find(q("pStyle")) if ps is not None else None
    return s.get(q("val")) if s is not None else "Normal"

def _is_heading(p):
    s = _style(p).lower(); return s.startswith("heading") or s == "title"

def _new_par_like(src, text, track, ids):
    p = src.makeelement(q("p"), {})
    ppr = src.find(q("pPr"))
    if ppr is not None and not _is_heading(src): p.append(copy.deepcopy(ppr))      # body paragraph takes the format of the next normal paragraph
    r = p.makeelement(q("r"), {}); t = r.makeelement(q("t"), {}); t.text = text; t.set(XML_SPACE, "preserve"); r.append(t)
    if track:
        ppr = p.find(q("pPr"))
        if ppr is None: ppr = p.makeelement(q("pPr"), {}); p.insert(0, ppr)
        rpr = ppr.find(q("rPr"))
        if rpr is None: rpr = ppr.makeelement(q("rPr"), {}); ppr.append(rpr)
        rpr.insert(0, _mark(p, ids, "ins"))                              # the paragraph mark itself is an insertion
        ins = _mark(p, ids, "ins"); ins.append(r); p.append(ins)
    else: p.append(r)
    return p

def op_replace(doc, o, ids):
    n = sum(_replace_in_par(p, o["find"], o.get("with", ""), bool(o.get("track")), ids) for p in _all_pars(doc))
    if n == 0: raise ValueError("replace: text not found: %r (it must sit inside one paragraph)" % o["find"])
    return n

def op_insert_after_heading(doc, o, ids):
    want = o["heading"].strip().lower()
    pars = [p for p in _all_pars(doc) if _is_heading(p)]
    hit = next((p for p in pars if _par_text(p).strip().lower() == want), None)
    if hit is None: hit = next((p for p in pars if want in _par_text(p).lower()), None)
    if hit is None: raise ValueError("insert_after_heading: heading not found: %r. Headings: %s" % (o["heading"], [_par_text(p) for p in pars][:20]))
    texts = o["text"] if isinstance(o["text"], list) else [o["text"]]
    nxt = hit.getnext(); ref = nxt if nxt is not None and nxt.tag == q("p") else hit
    cur = hit
    for t in texts:
        np_ = _new_par_like(ref, str(t), bool(o.get("track")), ids); cur.addnext(np_); cur = np_
    return len(texts)

def op_add_table_rows(doc, o, ids):
    tbls = doc.element.body.findall(q("tbl")); i = int(o.get("table", 0))
    if i >= len(tbls): raise ValueError("add_table_rows: table %d not found (document has %d)" % (i, len(tbls)))
    last = tbls[i].findall(q("tr"))[-1]
    for vals in o["rows"]:
        tr = copy.deepcopy(last)
        for c, v in zip(tr.findall(q("tc")), vals):
            ps = c.findall(q("p"))
            for extra in ps[1:]: c.remove(extra)
            p = ps[0]; rs = _simple_runs(p)
            for extra in rs[1:]: p.remove(extra)
            for x in list(p):
                if x.tag in (q("ins"), q("del")): p.remove(x)
            if rs: _set_run_text(rs[0], str(v))
            else:
                r = p.makeelement(q("r"), {}); t = r.makeelement(q("t"), {}); t.text = str(v); r.append(t); p.append(r); rs = [r]
            if o.get("track"):
                ins = _mark(p, ids, "ins"); rs[0].addprevious(ins); ins.append(rs[0])
        if o.get("track"):
            trpr = tr.find(q("trPr"))
            if trpr is None:
                trpr = tr.makeelement(q("trPr"), {}); ex = tr.find(q("tblPrEx"))
                if ex is not None: ex.addnext(trpr)
                else: tr.insert(0, trpr)
            trpr.append(_mark(tr, ids, "ins"))
        last.addnext(tr); last = tr
    return len(o["rows"])

OPS = {"replace": op_replace, "insert_after_heading": op_insert_after_heading, "add_table_rows": op_add_table_rows}

def _load_ops(arg):
    s = open(arg, encoding="utf-8").read() if os.path.isfile(arg) else arg
    ops = json.loads(s)
    return [ops] if isinstance(ops, dict) else ops

def cmd_edit(a):
    from docx import Document
    src = os.path.abspath(a.file)
    out = a.out or os.path.join(os.environ.get("AOS_OUT_DIR", "/workspace/out"), os.path.splitext(os.path.basename(src))[0] + "-edited.docx")
    if not out.lower().endswith(".docx"): out += ".docx"
    if os.path.abspath(out) == src: raise ValueError("--out must differ from the input file: the original stays untouched")
    doc = Document(src); ids = Ids(doc.element); res = []
    for k, o in enumerate(_load_ops(a.ops)):
        if o.get("op") not in OPS: raise ValueError("op %d: unknown op %r (use: %s)" % (k, o.get("op"), ", ".join(OPS)))
        res.append({"op": o["op"], "changed": OPS[o["op"]](doc, o, ids)})
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True); doc.save(out)
    bad = validate(out)
    print(json.dumps({"out": out, "results": res, "valid": not bad, "problems": bad}, ensure_ascii=False))
    return 1 if bad else 0

def validate(path):
    """Structural check: zip + well-formed XML, python-docx opens it, tracked changes have unique ids/author, no w:t inside w:del."""
    from lxml import etree
    pr = []
    try: z = zipfile.ZipFile(path)
    except Exception as e: return ["not a zip/docx file: %s" % e]
    bad = z.testzip()
    if bad: pr.append("corrupt zip member: %s" % bad)
    names = set(z.namelist())
    for need in ("[Content_Types].xml", "word/document.xml"):
        if need not in names: pr.append("missing part: %s" % need)
    for n in names:
        if n.endswith(".xml") or n.endswith(".rels"):
            try: etree.fromstring(z.read(n))
            except Exception as e: pr.append("XML not well-formed in %s: %s" % (n, e))
    if pr: return pr
    root = etree.fromstring(z.read("word/document.xml")); seen = set()
    for el in root.iter(q("ins"), q("del")):
        i = el.get(q("id"))
        if not i or not el.get(q("author")): pr.append("tracked change without id/author"); continue
        if i in seen: pr.append("duplicate tracked-change id %s" % i)
        seen.add(i)
        if el.tag == q("del") and el.getparent().tag not in (q("rPr"), q("trPr")) and list(el.iter(q("t"))): pr.append("w:t inside w:del (must be w:delText)")
    try:
        from docx import Document; Document(path)
    except Exception as e: pr.append("python-docx cannot open it: %s" % e)
    return pr

def cmd_validate(a):
    pr = validate(a.file); print(json.dumps({"ok": not pr, "problems": pr}, ensure_ascii=False)); return 1 if pr else 0

def _cell_text(c): return "".join(t.text or "" for t in c.iter(q("t")))

def cmd_inspect(a):
    from docx import Document
    body = Document(a.file).element.body
    pars = [{"i": k, "style": _style(p), "text": _par_text(p)} for k, p in enumerate(body.findall(q("p")))]
    tabs = [[[_cell_text(c) for c in r.findall(q("tc"))] for r in t.findall(q("tr"))] for t in body.findall(q("tbl"))]
    print(json.dumps({"paragraphs": pars, "tables": tabs, "tracked": {"ins": len(list(body.iter(q("ins")))), "del": len(list(body.iter(q("del"))))}}, ensure_ascii=False, indent=1))
    return 0

def cmd_to_md(a):
    from docx import Document
    out = []
    for el in Document(a.file).element.body:
        if el.tag == q("p"):
            t = _par_text(el).strip(); s = _style(el).replace(" ", "").lower()
            if not t: continue
            m = re.fullmatch(r"heading(\d)", s)
            if s == "title": out.append("# " + t)
            elif m: out.append("#" * min(int(m.group(1)), 6) + " " + t)
            elif s.startswith("listbullet"): out.append("- " + t)
            elif s.startswith("listnumber"): out.append("1. " + t)
            else: out.append(t)
            out.append("")
        elif el.tag == q("tbl"):
            rows = [[_cell_text(c).replace("|", "\\|") for c in r.findall(q("tc"))] for r in el.findall(q("tr"))]
            if rows:
                out.append("| " + " | ".join(rows[0]) + " |"); out.append("|" + "---|" * len(rows[0]))
                for r in rows[1:]: out.append("| " + " | ".join(r) + " |")
                out.append("")
    print("\n".join(out)); return 0

def cmd_render(a):
    src = os.path.abspath(a.file); outdir = a.out or tempfile.mkdtemp(prefix="docx-render-"); os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    try:
        subprocess.run(["soffice", "--headless", "--norestore", "-env:UserInstallation=file://" + tempfile.mkdtemp(prefix="lo-profile-"), "--convert-to", "pdf", "--outdir", outdir, src],
                       check=True, capture_output=True, timeout=a.timeout)
    except subprocess.TimeoutExpired: raise RuntimeError("render failed: LibreOffice did not finish in %d s (the .docx itself is untouched)" % a.timeout)
    except subprocess.CalledProcessError as e: raise RuntimeError("render failed: soffice exit %s %s" % (e.returncode, e.stderr.decode("utf-8", "replace")[:300]))
    pdf = os.path.join(outdir, stem + ".pdf")
    if not os.path.isfile(pdf): raise RuntimeError("render failed: no PDF produced")
    subprocess.run(["pdftoppm", "-png", "-r", str(a.dpi), "-f", "1", "-l", str(a.pages), pdf, os.path.join(outdir, stem)], check=True, capture_output=True, timeout=a.timeout)
    pngs = sorted(f for f in os.listdir(outdir) if f.endswith(".png") and f.startswith(stem + "-"))
    print(json.dumps({"pdf": pdf, "png": [os.path.join(outdir, f) for f in pngs]}, ensure_ascii=False)); return 0

def main(argv=None):
    ap = argparse.ArgumentParser(prog="docx_tool.py"); sp = ap.add_subparsers(dest="cmd", required=True)
    for n in ("inspect", "to-md", "validate"): sp.add_parser(n).add_argument("file")
    e = sp.add_parser("edit"); e.add_argument("file"); e.add_argument("--ops", required=True); e.add_argument("--out")
    r = sp.add_parser("render"); r.add_argument("file"); r.add_argument("--pages", type=int, default=1); r.add_argument("--out"); r.add_argument("--dpi", type=int, default=80); r.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args(argv)
    try:
        return {"inspect": cmd_inspect, "to-md": cmd_to_md, "validate": cmd_validate, "edit": cmd_edit, "render": cmd_render}[a.cmd](a)
    except (ValueError, RuntimeError, KeyError, OSError) as ex:
        print(json.dumps({"error": str(ex)}, ensure_ascii=False)); return 1

if __name__ == "__main__":
    sys.exit(main())
