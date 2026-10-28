"""PDF from Markdown or HTML (weasyprint 70). Vietnamese renders with the Noto Sans font (fonts-noto-core in the sandbox image);
the font is embedded (subset) in the PDF, so the reader needs no font. A4, page numbers in the footer.
Use:  import sys; sys.path.insert(0, "/memories/skills/pdf/scripts"); from make_pdf import build
      path = build("# Báo cáo\\n\\nNội dung...", "bao-cao")            # Markdown -> /workspace/out/bao-cao.pdf
      path = build({"html": "<h1>…</h1>", "css": "…"}, "bao-cao")       # own HTML (+ extra CSS)
      path = build({"title": "…", "markdown": "…"}, "bao-cao")
Images: Markdown has no image syntax here; use HTML <img src="file:///workspace/out/bieu-do.png"> (no network in the sandbox).
CLI:  python make_pdf.py <spec.json|spec.md|text> <name>"""
import html, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_common"))
from mdlite import blocks as md_blocks, runs, out_path, load_spec  # noqa: E402

CSS = """@page { size: A4; margin: 18mm 16mm; @bottom-right { content: counter(page) " / " counter(pages); font-size: 9pt; color: #666; } }
body { font-family: "Noto Sans", "DejaVu Sans", sans-serif; font-size: 11pt; line-height: 1.45; color: #111; }
h1 { font-size: 20pt; margin: 0 0 8pt; } h2 { font-size: 15pt; margin: 14pt 0 6pt; } h3, h4 { font-size: 12.5pt; margin: 10pt 0 4pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0; } th, td { border: 0.6pt solid #999; padding: 4pt 6pt; text-align: left; }
th { background: #e8eef7; } td.n { text-align: right; } img { max-width: 100%; }"""

def _inline(t: str) -> str:
    return "".join(f"<b>{html.escape(x)}</b>" if b else html.escape(x) for x, b in runs(t))

def md_to_html(md: str) -> str:
    out = []
    for b in md_blocks(md):
        t = b["type"]
        if t == "heading": out.append(f"<h{b['level']}>{_inline(b['text'])}</h{b['level']}>")
        elif t == "paragraph": out.append(f"<p>{_inline(b['text'])}</p>")
        elif t in ("bullets", "numbers"):
            tag = "ul" if t == "bullets" else "ol"; out.append(f"<{tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in b["items"]) + f"</{tag}>")
        elif t == "table" and b["rows"]:
            num = lambda v: v.replace(".", "").replace(",", "").replace("%", "").strip().lstrip("-").isdigit()
            head, *rest = b["rows"]
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td{' class=n' if num(c) else ''}>{_inline(c)}</td>" for c in r) + "</tr>" for r in rest) + "</tbody></table>")
    return "\n".join(out)

def build(spec, name: str = "tai-lieu") -> str:
    from weasyprint import HTML, CSS as WCSS
    if isinstance(spec, str): spec = {"markdown": spec}
    body = spec.get("html") or md_to_html(spec.get("markdown", ""))
    title = spec.get("title", "")
    doc = f'<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>{html.escape(title)}</title></head><body>' + (f"<h1>{html.escape(title)}</h1>" if title else "") + body + "</body></html>"
    p = out_path(name, "pdf")
    HTML(string=doc, base_url=os.path.dirname(p)).write_pdf(p, stylesheets=[WCSS(string=CSS + (spec.get("css") or ""))])
    return p

if __name__ == "__main__":
    print(build(load_spec(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "tai-lieu"))
