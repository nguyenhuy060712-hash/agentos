---
name: pdf
description: Use when the user wants a PDF - a printable report, quotation, certificate, one-page summary. Dùng khi người dùng cần tệp PDF - báo cáo để in, báo giá, giấy chứng nhận, bản tóm tắt một trang.
compatibility: Python 3 with WeasyPrint (installed in the Agent OS sandbox image). Run inside the sandbox or the virtual computer; no internet needed.
---

# PDF

Write Markdown (or HTML for a custom layout), then one `chay_code_python` call:

```python
import sys; sys.path.insert(0, "/memories/skills/pdf/scripts"); from make_pdf import build
print(build({"title": "Báo cáo doanh thu quý 3 – Hà Nội, Đà Nẵng",
             "markdown": "## Tóm tắt\n\nDoanh thu **tăng 12%**.\n\n| Chi nhánh | Q3 |\n|---|---|\n| Hà Nội | 1.200 |\n| Đà Nẵng | 860 |"},
            "bao-cao-q3"))   # -> /workspace/out/bao-cao-q3.pdf (A4, page numbers)
```

- The helper is `/memories/skills/pdf/scripts/make_pdf.py` (read-only). In the sandbox just import it as above; in a virtual computer you may also read it or run it with `execute`: `python /memories/skills/pdf/scripts/make_pdf.py <spec.json> <name>`.
- Custom layout: `build({"html": "<h1>…</h1>…", "css": "…"}, name)`. Images: save a chart to `/workspace/out/x.png` first, then `<img src="x.png">` (no internet).
- Vietnamese renders with Noto Sans and the font is embedded, so the PDF looks the same everywhere. Do not change `font-family` to a font that is not installed.
- The PDF shows up for the user with a preview and a download button; reply with a short summary.
- To read an attached PDF use the chat's file search, not this skill.
- Limits: no internet, 512 MB RAM, 60 s per run.
