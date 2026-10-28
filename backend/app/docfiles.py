"""GD6.8: document uploads (pdf, docx, xlsx, pptx, csv, txt, md, json) for the chat. Pure functions, no DB:
- sniff_doc(data, name)   real type from magic bytes / zip structure (extension must agree), zip-bomb + macro guards -> (kind, mime) or ValueError
- convert(data, kind, name) markitdown (MIT) in a worker thread with a timeout -> {"md", "pages", "units"}; failures raise (caller keeps the file, marks it unreadable)"""
import asyncio, io, json, os, re, zipfile

DOC_MAX_BYTES = int(os.environ.get("AOS_DOC_MAX_BYTES", str(20 * 1024 * 1024)))
ZIP_MAX_TOTAL = 200 * 1024 * 1024
ZIP_MAX_RATIO = 100
ZIP_MAX_ENTRIES = 10_000
CONVERT_TIMEOUT = int(os.environ.get("AOS_DOC_CONVERT_TIMEOUT", "60"))
MD_MAX_CHARS = 5_000_000

MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv", "txt": "text/plain", "md": "text/markdown", "json": "application/json",
}
MAIN_PART = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}
TEXT_KINDS = ("csv", "txt", "md", "json")
DOC_EXTS = set(MIME) | {"docm", "xlsm", "pptm", "doc", "xls", "ppt", "jpg", "jpeg", "png", "webp", "gif"}
UNITS = {"pdf": "page", "xlsx": "sheet", "pptx": "slide"}

def ext_of(name: str | None) -> str:
    return (name or "").rsplit(".", 1)[-1].lower() if name and "." in name else ""

def _zip_check(data: bytes) -> zipfile.ZipFile:
    try: z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile: raise ValueError("không phải tệp Office hợp lệ (zip hỏng)") from None
    infos = z.infolist()
    if len(infos) > ZIP_MAX_ENTRIES: raise ValueError(f"zip có quá nhiều mục ({len(infos)} > {ZIP_MAX_ENTRIES})")
    total = sum(i.file_size for i in infos); comp = sum(i.compress_size for i in infos) or 1
    if total > ZIP_MAX_TOTAL: raise ValueError(f"zip giải nén quá lớn ({total >> 20} MB > {ZIP_MAX_TOTAL >> 20} MB)")
    if total / comp > ZIP_MAX_RATIO or any(i.file_size > (1 << 20) and i.file_size / (i.compress_size or 1) > ZIP_MAX_RATIO for i in infos):
        raise ValueError("tỉ lệ nén bất thường (nghi zip bomb)")
    # headers can lie: really inflate (bounded) and compare
    got = 0
    for i in infos:
        with z.open(i) as f:
            while chunk := f.read(1 << 20):
                got += len(chunk)
                if got > ZIP_MAX_TOTAL: raise ValueError("zip giải nén quá lớn (nghi zip bomb)")
    return z

def _text_ok(data: bytes, kind: str) -> str:
    if kind == "csv" and data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try: return data.decode("utf-16")
        except UnicodeDecodeError: raise ValueError("csv UTF-16 không hợp lệ") from None
    if b"\x00" in data: raise ValueError("tệp văn bản chứa byte nhị phân")
    try: return data.decode("utf-8-sig")
    except UnicodeDecodeError: raise ValueError("tệp văn bản phải mã hoá UTF-8") from None

def sniff_doc(data: bytes, name: str | None) -> tuple[str, str]:
    """-> (kind, mime). The extension must agree with the content; the client-declared MIME is never used."""
    ext = ext_of(name)
    if ext in ("docm", "xlsm", "pptm"): raise ValueError("tệp có macro không được nhận")
    if data.startswith(b"%PDF"): kind = "pdf"
    elif data[:4] == b"PK\x03\x04":
        z = _zip_check(data); names = {n.lower() for n in z.namelist()}
        if any(n.endswith("vbaproject.bin") for n in names): raise ValueError("tệp có macro không được nhận")
        if "[content_types].xml" not in names: raise ValueError("không phải tệp Office (thiếu [Content_Types].xml)")
        if b"macroEnabled" in z.read("[Content_Types].xml"): raise ValueError("tệp có macro không được nhận")
        kind = next((k for k, p in MAIN_PART.items() if p in names), "")
        if not kind: raise ValueError("không phải docx/xlsx/pptx (thiếu phần chính)")
    elif data.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")) or data[:4] == b"\xd0\xcf\x11\xe0":
        raise ValueError("nội dung không khớp đuôi tệp" if ext in MIME else "loại tệp không được hỗ trợ")
    else:
        if ext not in TEXT_KINDS: raise ValueError("loại tệp không được hỗ trợ (nhận pdf, docx, xlsx, pptx, csv, txt, md, json)")
        kind = ext; txt = _text_ok(data, kind)
        if kind == "json":
            try: json.loads(txt)
            except ValueError: raise ValueError("json không hợp lệ") from None
    if ext and ext != kind: raise ValueError(f"đuôi .{ext} không khớp nội dung thật ({kind})")
    return kind, MIME[kind]

def _convert_sync(data: bytes, kind: str) -> dict:
    pages = None
    if kind in TEXT_KINDS:
        txt = _text_ok(data, kind)
        if kind == "csv":
            from markitdown import MarkItDown, StreamInfo
            md = MarkItDown(enable_plugins=False).convert_stream(io.BytesIO(txt.encode()), stream_info=StreamInfo(extension=".csv", mimetype="text/csv", charset="utf-8")).markdown
        else: md = txt
    else:
        from markitdown import MarkItDown, StreamInfo
        md = MarkItDown(enable_plugins=False).convert_stream(io.BytesIO(data), stream_info=StreamInfo(extension="." + kind, mimetype=MIME[kind])).markdown
        try:
            if kind == "pdf":
                from pdfminer.pdfpage import PDFPage
                pages = sum(1 for _ in PDFPage.get_pages(io.BytesIO(data)))
            elif kind == "xlsx":
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True); pages = len(wb.sheetnames); wb.close()
            elif kind == "pptx":
                from pptx import Presentation
                pages = len(Presentation(io.BytesIO(data)).slides)
        except Exception:  # noqa: BLE001  (count is optional)
            pages = None
    md = (md or "").strip()
    if not md: raise ValueError("không có văn bản để đọc (có thể là bản quét/ảnh)")
    return {"md": md[:MD_MAX_CHARS], "pages": pages, "units": UNITS.get(kind)}

async def convert(data: bytes, kind: str, timeout: float | None = None) -> dict:
    """markitdown in a worker thread; a hung conversion is abandoned after `timeout` seconds (60 by default)."""
    try: return await asyncio.wait_for(asyncio.to_thread(_convert_sync, data, kind), timeout or CONVERT_TIMEOUT)
    except asyncio.TimeoutError: raise ValueError(f"đọc tệp quá {int(timeout or CONVERT_TIMEOUT)} giây") from None

def est_tokens(md: str) -> int: return (len(md) + 3) // 4

def safe_name(name: str | None, fallback: str = "file") -> str:
    n = re.sub(r"[^\w.\- ]", "_", os.path.basename(name or "") or fallback, flags=re.U).strip(" .") or fallback
    return n[:80]
