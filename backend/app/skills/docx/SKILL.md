---
name: docx
description: Use when the user wants a Word document (.docx) - report, letter, contract draft, minutes. Dùng khi người dùng cần tệp Word (.docx) - báo cáo, công văn, hợp đồng nháp, biên bản.
compatibility: Python 3 with python-docx (installed in the Agent OS sandbox image). Run inside the sandbox or the virtual computer; no internet needed.
---

# Word (.docx)

Two jobs: **create** a new document (below), or **edit / check** an existing one (section "Edit an existing .docx").

Write the content as Markdown (or a JSON spec), then make the file in ONE `chay_code_python` call:

```python
import sys; sys.path.insert(0, "/memories/skills/docx/scripts"); from make_docx import build
md = """# Báo cáo doanh thu quý 3

Tóm tắt: doanh thu **tăng 12%** so với quý 2.

| Chi nhánh | Q3 (triệu đ) |
|---|---|
| Hà Nội | 1.200 |
| Đà Nẵng | 860 |

- The helper is `/memories/skills/docx/scripts/make_docx.py` (read-only). In the sandbox just import it as above; in a virtual computer you may also read it or run it with `execute`: `python /memories/skills/docx/scripts/make_docx.py <spec.json> <name>`.
- Đà Nẵng tăng nhanh nhất
- Cần thêm nhân sự bán hàng
"""
print(build(md, "bao-cao-q3"))    # -> /workspace/out/bao-cao-q3.docx
```

- The file is shown to the user as a download card automatically. Do not paste the whole document again in your answer; give a 1-2 line summary.
- Markdown supported: `#`..`####`, paragraphs, `-`/`1.` lists, `| tables |`, `**bold**`. More control: JSON `{"title", "blocks": [...]}` (see `make_docx.py`), blocks include `image` (a PNG you saved in `/workspace/out`) and `page_break`.
- Vietnamese: write with full diacritics (UTF-8); the font is Noto Sans on every style. Numbers in Vietnamese style: `1.200.000 đ`, dates `23/10/2026`.
- Files the user attached are readable at `/tmp/<file name>`.
- Limits: no internet, 512 MB RAM, 60 s per run.

## Edit an existing .docx (helper `docx_tool.py`, run with `execute`)

The original is never changed: `edit` writes a new file in `/workspace/out/<name>-edited.docx`.

```
T=/memories/skills/docx/scripts/docx_tool.py
python $T inspect  /tmp/hop-dong.docx            # paragraphs (index, style, text), tables, tracked-change counts
python $T to-md    /tmp/hop-dong.docx            # read it as Markdown
python $T edit     /tmp/hop-dong.docx --ops '[{"op":"replace","find":"Công ty A","with":"Công ty B","track":true}]'
python $T validate /workspace/out/hop-dong-edited.docx
python $T render   /workspace/out/hop-dong-edited.docx --pages 1     # PNG of page 1 to look at
```

- Ops: `replace` (text across runs, format kept), `insert_after_heading` (`heading`, `text` string or list), `add_table_rows` (`table` index, `rows`). Add `"track": true` for a real Word tracked change (author "Agent OS"): use it whenever the user says review / suggest / "để tôi duyệt"; without it the change is silent.
- Always run `validate` after `edit` (edit already reports `valid`). If `render` says LibreOffice timed out, the .docx is still fine; say so.
- Text to replace must sit inside one paragraph. Use `inspect` first, copy the exact words.
