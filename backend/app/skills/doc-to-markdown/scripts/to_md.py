"""Document -> Markdown (GĐ11.67). Runs inside the virtual computer: python /memories/skills/doc-to-markdown/scripts/to_md.py <file> [name]
Input is usually /workspace/uploads/<file>; output /workspace/out/<name>.md. Prints a short summary (pages / sheets / slides, tables, characters).
Exit code 0 = ok, 2 = the file cannot be read as text (scanned PDF: OCR needed; nothing is guessed), 1 = other error."""
import re, shutil, subprocess, sys, tempfile
from pathlib import Path

OUT = Path("/workspace/out")
LEGACY = {".doc": "docx", ".odt": "docx", ".rtf": "docx", ".xls": "xlsx", ".ods": "xlsx", ".ppt": "pptx", ".odp": "pptx"}   # -> LibreOffice target
DIRECT = {".docx", ".xlsx", ".pptx", ".pdf", ".epub", ".html", ".htm", ".txt", ".csv", ".json", ".xml", ".md"}
PANDOC = {".rtf": "rtf", ".epub": "epub", ".odt": "odt", ".doc": None}
MIN_CHARS_PER_PAGE = 25          # a PDF page with less text than this is treated as an image (scan)
SCANNED_MSG = ("Tệp PDF này là bản quét (ảnh), gần như không có chữ nên chưa đọc được. Cần OCR (nhận dạng chữ) trước. "
               "Scanned PDF, OCR needed: no text layer found, nothing was converted.")

class ToMdError(Exception):
    def __init__(self, msg, code=1): super().__init__(msg); self.code = code

def soffice_convert(src: Path, target: str, tmp: Path) -> Path:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe: raise ToMdError("LibreOffice is not installed in this computer")
    out = tmp / "lo"; out.mkdir()
    cmd = [exe, f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--norestore", "--convert-to", target, "--outdir", str(out), str(src)]
    try: r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired: raise ToMdError(f"LibreOffice took too long (120 s) to convert {src.name}")
    res = out / (src.stem + "." + target)
    if not res.exists(): raise ToMdError(f"LibreOffice could not convert {src.name}: {(r.stderr or r.stdout).strip()[:200]}")
    return res

def pandoc_md(src: Path, fmt) -> str:
    exe = shutil.which("pandoc")
    if not exe: raise ToMdError("pandoc is not installed in this computer")
    cmd = [exe, "-t", "gfm", "--wrap=none", *(["-f", fmt] if fmt else []), str(src)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode: raise ToMdError(f"pandoc failed on {src.name}: {r.stderr.strip()[:200]}")
    return r.stdout

def markitdown_md(src: Path) -> str:
    from markitdown import MarkItDown
    return MarkItDown(enable_plugins=False).convert(str(src)).markdown

def pdf_pages(src: Path) -> list[int]:
    """Characters of text per page (pdfminer comes with markitdown[pdf])."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    return [sum(len(e.get_text().strip()) for e in pg if isinstance(e, LTTextContainer)) for pg in extract_pages(str(src))]

def count_tables(md: str) -> int:
    return len(re.findall(r"^(?=.*\|)\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", md, re.M))

def convert(src: Path, tmp: Path) -> tuple[str, dict]:
    ext = src.suffix.lower(); info = {}
    if ext == ".pdf":
        per = pdf_pages(src); info["pages"] = len(per)
        if not per or sum(per) / len(per) < MIN_CHARS_PER_PAGE: raise ToMdError(SCANNED_MSG, 2)
        return markitdown_md(src), info
    if ext in LEGACY:
        try:
            lo = soffice_convert(src, LEGACY[ext], tmp)
            md = markitdown_md(lo)
            if md.strip(): return md, info
        except Exception as e:
            if ext not in PANDOC or not shutil.which("pandoc"): raise (e if isinstance(e, ToMdError) else ToMdError(f"cannot read {src.name}: {e}"))
        return pandoc_md(src, PANDOC[ext]), info                     # fallback: LibreOffice/MarkItDown failed or empty
    if ext in DIRECT:
        try:
            md = markitdown_md(src)
            if md.strip() or ext not in PANDOC: return md, info
        except Exception as e:
            if ext not in PANDOC or not shutil.which("pandoc"): raise ToMdError(f"cannot read {src.name}: {e}")
        return pandoc_md(src, PANDOC[ext]), info
    raise ToMdError(f"unsupported file type {ext or '(none)'}: use .doc .docx .odt .rtf .xls .xlsx .ods .ppt .pptx .odp .pdf .epub .html")

def main(path: str, name: str | None = None, out_dir: Path = OUT) -> str:
    src = Path(path)
    if not src.is_file(): raise ToMdError(f"file not found: {path}")
    with tempfile.TemporaryDirectory() as t:
        md, info = convert(src, Path(t))
    if not md.strip(): raise ToMdError(f"no text found in {src.name}", 2)
    stem = re.sub(r"[^\w.-]+", "-", name or src.stem, flags=re.U).strip("-") or "tai-lieu"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{stem}.md"; dest.write_text(md, encoding="utf-8")
    parts = [f"{k}={v}" for k, v in info.items()] + [f"tables={count_tables(md)}", f"chars={len(md)}"]
    return f"{dest}  ({', '.join(parts)})"

if __name__ == "__main__":
    if len(sys.argv) < 2: sys.exit("usage: to_md.py <file> [name] [out_dir]")
    try: print(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, Path(sys.argv[3]) if len(sys.argv) > 3 else OUT))
    except ToMdError as e: print(f"ERROR: {e}"); sys.exit(e.code)
