---
name: pptx
description: Use when the user wants slides / a PowerPoint deck (.pptx) - presentation, pitch, training or meeting slides. Dùng khi người dùng cần bài trình chiếu PowerPoint (.pptx) - thuyết trình, giới thiệu, đào tạo, họp.
compatibility: Python 3 with python-pptx (installed in the Agent OS sandbox image). Run inside the sandbox or the virtual computer; no internet needed.
---

# PowerPoint (.pptx)

**A slide / PowerPoint request = a `.pptx` file, never HTML** (and not a chat outline only). Two jobs: create a new deck (below) or edit / check an existing one (section "Edit an existing .pptx").

Plan 5-10 slides (one idea each, ≤ 6 short bullets), then one `chay_code_python` call:

```python
import sys; sys.path.insert(0, "/memories/skills/pptx/scripts"); from make_pptx import build
print(build({"title": "Báo cáo doanh thu quý 3", "subtitle": "Hà Nội, Đà Nẵng",
             "slides": [{"title": "Điểm chính", "bullets": ["Doanh thu tăng 12%", "  - Đà Nẵng +23%"], "notes": "Nói 1 phút"},
                        {"title": "Số liệu", "table": [["Chi nhánh", "Q3"], ["Hà Nội", "1.200"], ["Đà Nẵng", "860"]]}]},
            "bao-cao-q3"))   # -> /workspace/out/bao-cao-q3.pptx
```

- The helper is `/memories/skills/pptx/scripts/make_pptx.py` (read-only). In the sandbox just import it as above; in a virtual computer you may also read it or run it with `execute`: `python /memories/skills/pptx/scripts/make_pptx.py <spec.json> <name>`.
- Slide keys: `title`, `bullets` (`"  - x"` = sub-bullet), `table` (rows), `image` (PNG path, e.g. a matplotlib chart you saved with `plt.savefig("/workspace/out/bieu-do.png", dpi=150)` in the same code), `notes`.
- 16:9, font Noto Sans on every text (Vietnamese OK). Keep bullet text short; put detail in `notes`.
- The deck shows up for the user as a download card; reply with a short outline only.
- Limits: no internet, 512 MB RAM, 60 s per run.

## Edit an existing .pptx (helper `pptx_tool.py`, run with `execute`)

The original is never changed: `edit` writes a new file in `/workspace/out/<name>-edited.pptx`. Slide numbers start at 1.

```
T=/memories/skills/pptx/scripts/pptx_tool.py
python $T inspect  /tmp/deck.pptx                 # layouts, per slide: shape names and text, notes
python $T edit     /tmp/deck.pptx --ops '[{"op":"text","slide":2,"shape":"Title 1","text":"Điểm chính mới"},{"op":"add_slide","layout":"Title and Content","title":"Kết luận","bullets":["Tăng 12%"],"at":3}]'
python $T validate /workspace/out/deck-edited.pptx
python $T render   /workspace/out/deck-edited.pptx --pages 3    # PNG thumbnails to look at
```

- Ops: `text`, `replace` (find / with), `add_slide`, `delete`, `move`, `notes`, `replace_image`. Run `validate` after `edit` (edit already reports `valid`); warnings (empty placeholder, shape outside the slide) are worth fixing.
- If `render` says LibreOffice timed out, the .pptx is still fine; say so.
- To put the finished deck in OneDrive use `onedrive_upload` (a new file; it never replaces one); to read a deck that is already in OneDrive use `onedrive_pptx_read`.
