"""GĐ12.82b: files that leave the workspace (Gmail / Outlook attachments, Drive / OneDrive / Word / Excel uploads) are loaded and CHECKED here, before any API call.
Small API (all errors are `workspace_tools._Err`: plain English the model may read):
  await load(refs, max_total=, max_count=, sniff=None) -> [(name, mime, bytes)]   chat file id | /api/files/<id> | /workspace/... (Gmail's old `_load_atts`, texts unchanged)
  check(name, data) -> mime            the strict test for UPLOADS: type/extension agree, no macros, no external links / OLE / ActiveX / DDE, no zip bomb, no hostile zip entries, no XXE
  repair_b64(s) -> bytes               tolerant base64 (whitespace, URL-safe alphabet, missing padding), then strict
  await prepare_upload(ref_or_b64, name=None, *, mime=None) -> (name, mime, bytes)   the one call an upload tool makes
Who can read what: a chat file is read through `attachments._row` (RLS of the running workspace; another workspace's id = "not found"); a /workspace path through the agent's own box.
Nothing is cached in this module. Hyperlinks to http(s) / mailto are allowed (every real document has them); every other EXTERNAL relationship is refused.
Chart data inside pptx / docx (embeddings/*.xlsx) is allowed only if that workbook passes the same check, one level deep."""
import asyncio, base64, binascii, html as _html, io, re, stat, xml.etree.ElementTree as ET, zipfile, zlib
import docfiles as D
from workspace_tools import _Err

MAX_UPLOAD = D.DOC_MAX_BYTES                 # 20 MB, same as chat uploads
XML_MAX = 50 * 1024 * 1024                   # one XML part; bigger = cannot be inspected = refused
_CHAT_FILE = re.compile(r"^(?:file:|/api/files/)?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
MACRO_EXTS = {"docm", "dotm", "xlsm", "xltm", "xlam", "pptm", "potm", "ppam", "ppsm", "sldm"}
IMG = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
ZIP_KINDS = ("docx", "xlsx", "pptx")
MAIN_CT = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
           "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
           "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"}
BAD_EXT = (".zip", ".jar", ".7z", ".rar", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".cab", ".iso", ".exe", ".dll", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta", ".scr", ".lnk", ".com")
BAD_MAGIC = ((b"PK\x03\x04", "a zip archive"), (b"PK\x05\x06", "a zip archive"), (b"\xd0\xcf\x11\xe0", "an OLE object"), (b"MZ", "a program"), (b"\x7fELF", "a program"),
             (b"7z\xbc\xaf", "an archive"), (b"Rar!", "an archive"), (b"\x1f\x8b", "an archive"))
BAD_REL = {"oleobject", "externallink", "externallinkpath", "vbaproject", "vbaprojectsignature", "vbadata", "control", "activexcontrol", "activexcontrolbinary", "macrosheet", "ddelink", "olelink"}
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")

class _Bad(ValueError): pass

# ------------------------------------------------------------------ base64
def repair_b64(s) -> bytes:
    """Base64 as models write it (line breaks, URL-safe alphabet, no padding, `data:...;base64,` prefix) -> bytes. After the repair the decoding is strict."""
    try: return _repair(s)
    except _Bad as e: raise _Err(str(e)) from None

def _repair(s) -> bytes:
    if isinstance(s, (bytes, bytearray)): s = bytes(s).decode("ascii", "replace")
    s = str(s or ""); m = re.match(r"^\s*data:[^,;]*(?:;[^,;]*)*;base64,", s, re.I)
    if m: s = s[m.end():]
    clean = re.sub(r"\s+", "", s).rstrip("=")
    if not clean: raise _Bad("not valid base64: it is empty")
    std, url = re.search(r"[+/]", clean), re.search(r"[-_]", clean)
    if std and url: raise _Bad("not valid base64: it mixes the standard (+ /) and URL-safe (- _) alphabets")
    bad = re.search(r"[^A-Za-z0-9+/\-_]", clean)
    if bad:   # report the position in the text the caller sent (whitespace counted)
        pos, n = 0, 0
        for i, ch in enumerate(s):
            if ch.isspace(): continue
            if n == bad.start(): pos = i; break
            n += 1
        raise _Bad(f"not valid base64 at char {pos}: {clean[bad.start()]!r}")
    if len(clean) % 4 == 1: raise _Bad("not valid base64: the length is impossible (the text is cut off)")
    clean = clean.replace("-", "+").replace("_", "/") + "=" * (-len(clean) % 4)
    try: return base64.b64decode(clean, validate=True)
    except (binascii.Error, ValueError) as e: raise _Bad(f"not valid base64: {e}") from None

# ------------------------------------------------------------------ the strict check (uploads)
def check(name, data) -> str:
    """-> mime of the REAL content. Refuses (with the reason) macros, hostile or oversized files and an extension that does not match the content."""
    try: return _check(name, data)
    except _Bad as e: raise _Err(f"file {name} refused: {e}") from None

def _check(name, data) -> str:
    ext = D.ext_of(name)
    if not data: raise _Bad("it is empty")
    if len(data) > MAX_UPLOAD: raise _Bad(f"it is larger than {MAX_UPLOAD >> 20} MB")
    if ext in MACRO_EXTS: raise _Bad(f"macro-enabled files (.{ext}) are not accepted; save a copy without macros as .docx / .xlsx / .pptx")
    import attachments as AT
    mime = AT.sniff(data)
    if mime:
        if ext and IMG.get(ext) != mime: raise _Bad(f"the extension .{ext} does not match the content ({mime})")
        return mime
    if data[:4] == b"\xd0\xcf\x11\xe0":
        raise _Bad("old binary Office formats (.doc / .xls / .ppt) can carry macros and are not accepted; save as .docx / .xlsx / .pptx" if b"VBA" in data or b"V\x00B\x00A\x00" in data
                   else "old binary Office formats (.doc / .xls / .ppt) are not accepted; save as .docx / .xlsx / .pptx")
    if data[:4] != b"PK\x03\x04":
        if ext in ZIP_KINDS: raise _Bad(f"not a zip: .{ext} files are zip packages but this one does not start with the zip header (damaged, cut off or another type)")
        try: return D.sniff_doc(data, name)[1]
        except ValueError as e: raise _Bad(str(e)) from None
    if ext not in ZIP_KINDS: raise _Bad("an Office file needs the extension .docx, .xlsx or .pptx" if not ext else f"the extension .{ext} does not match the content (a zip package)")
    kind = _inspect(data)
    if kind != ext: raise _Bad(f"the extension .{ext} does not match the content ({kind})")
    try: return D.sniff_doc(data, name)[1]   # the old guards run too (bounded inflate of every entry)
    except ValueError as e: raise _Bad(str(e)) from None

def _inspect(data, depth=0) -> str:
    """Looks inside an OOXML zip. -> 'docx' | 'xlsx' | 'pptx'. Nothing is extracted to disk."""
    try: return _inspect_zip(data, depth)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, OSError):
        raise _Bad("the zip is damaged or uses an unsupported feature (encryption, odd compression)") from None

def _inspect_zip(data, depth) -> str:
    try: z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile: raise _Bad("not a zip: missing end-of-central-directory (the file is cut off or damaged)") from None
    infos = z.infolist()
    if len(infos) > D.ZIP_MAX_ENTRIES: raise _Bad(f"the zip has too many entries ({len(infos)} > {D.ZIP_MAX_ENTRIES})")
    total = sum(i.file_size for i in infos); comp = sum(i.compress_size for i in infos) or 1
    if total > D.ZIP_MAX_TOTAL: raise _Bad(f"the zip would expand to {total >> 20} MB (limit {D.ZIP_MAX_TOTAL >> 20} MB): zip bomb")
    if total / comp > D.ZIP_MAX_RATIO or any(i.file_size > (1 << 20) and i.file_size / (i.compress_size or 1) > D.ZIP_MAX_RATIO for i in infos):
        raise _Bad("abnormal compression ratio: zip bomb")   # decided from the headers only, nothing is inflated yet
    seen, names, ctypes = set(), set(), ""
    for i in infos:
        n, low = i.filename, i.filename.lower()
        if n.startswith(("/", "\\")) or "\\" in n or "\x00" in n or re.match(r"^[A-Za-z]:", n) or ".." in n.split("/"): raise _Bad(f"unsafe path inside the zip: {n[:80]!r}")
        if low in seen: raise _Bad(f"the zip has two entries named {n[:80]!r} (different programs would read different content)")
        seen.add(low); names.add(low)
        if i.flag_bits & 0x1: raise _Bad(f"encrypted entry {n[:80]!r} cannot be inspected")
        if stat.S_IFMT(i.external_attr >> 16) == stat.S_IFLNK: raise _Bad(f"symbolic link inside the zip: {n[:80]!r}")
        p = "/" + low
        base = low.rsplit("/", 1)[-1]
        if base.endswith("vbaproject.bin") or base in ("vbaprojectsignature.bin", "vbadata.xml") or "/macrosheets/" in p: raise _Bad("it contains macros (VBA project / Excel 4 macro sheet)")
        if "/activex/" in p: raise _Bad("it contains ActiveX controls")
        if "/externallinks/" in p: raise _Bad("it contains links to other workbooks (externalLinks)")
        if low.endswith(BAD_EXT): raise _Bad(f"it contains an archive or program ({n[:80]!r})")
    kinds = [k for k, part in D.MAIN_PART.items() if part in names]
    if len(kinds) != 1: raise _Bad("it is not a single Word / Excel / PowerPoint document" + (f" (it mixes {', '.join(kinds)} parts)" if kinds else " (main part missing)"))
    kind = kinds[0]
    if "[content_types].xml" not in names: raise _Bad("[Content_Types].xml is missing")
    for i in infos:
        n, low = i.filename, i.filename.lower()
        if i.is_dir(): continue
        if low.endswith((".xml", ".rels", ".vml")):
            if i.file_size > XML_MAX: raise _Bad(f"XML part {n[:80]!r} is larger than {XML_MAX >> 20} MB and cannot be inspected")
            part = z.read(i)
            _xml_safe(n, part)
            if low == "[content_types].xml": ctypes = part.decode("utf-8", "replace")
            elif low.endswith(".rels"): _rels(n, part)
            elif low.startswith("word/") and low.endswith(".xml"): _dde(n, part, "instr")
            elif low.startswith("xl/worksheets/") and low.endswith(".xml"): _dde(n, part, "f")
            continue
        with z.open(i) as f: head = f.read(8)
        emb = "/embeddings/" in "/" + low
        if emb and low.endswith(tuple("." + k for k in ZIP_KINDS)) and head[:4] == b"PK\x03\x04":
            if depth >= 1: raise _Bad(f"{n[:80]!r}: a package inside a package inside a document is not accepted")
            sub = _inspect(z.read(i), depth + 1)
            if not low.endswith("." + sub): raise _Bad(f"{n[:80]!r}: the extension does not match the embedded content ({sub})")
            continue
        if emb: raise _Bad(f"it contains an embedded object ({n[:80]!r})")
        if low.endswith((".odttf", ".fntdata", ".ttf")): continue   # embedded fonts are obfuscated: their first bytes are random
        for magic, what in BAD_MAGIC:
            if head.startswith(magic): raise _Bad(f"{n[:80]!r} is {what} hidden inside the document")
    cts = set(re.findall(r'ContentType\s*=\s*"([^"]*)"', ctypes))
    low_ct = ctypes.lower()
    if "macroenabled" in low_ct or "vbaproject" in low_ct: raise _Bad("it contains macros")
    if "oleobject" in low_ct or "activex" in low_ct: raise _Bad("it contains OLE / ActiveX objects")
    if MAIN_CT[kind] not in cts: raise _Bad(f"the content types do not declare a normal .{kind} document (template or other type)")
    return kind

def _xml_safe(part, b):
    """No DTD at all (Office never writes one) = no XXE / entity bombs for any parser that reads the file later. Only UTF-8 / ASCII XML, so the DTD test cannot be bypassed by encoding."""
    if b[:3] == b"\xef\xbb\xbf": b = b[3:]
    m = re.match(rb"\s*<", b)
    if not m or b"\x00" in b[:16]: raise _Bad(f"{part[:80]!r}: the XML is not UTF-8 / ASCII text")
    decl = re.match(rb"\s*<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']*)[\"']", b[:512])
    if decl and decl.group(1).lower() not in (b"utf-8", b"utf8", b"us-ascii", b"ascii"): raise _Bad(f"{part[:80]!r}: unsupported XML encoding {decl.group(1)[:20]!r}")
    if b"<!DOCTYPE" in b or b"<!ENTITY" in b: raise _Bad(f"{part[:80]!r}: it declares an XML DTD / entity (external entity attack)")

def _is_unc(t): return t.startswith(("\\\\", "//"))

def _rels(part, b):
    try: root = ET.fromstring(b)
    except ET.ParseError: raise _Bad(f"{part[:80]!r}: the relationships are not valid XML") from None
    for r in root.iter():
        if r.tag.rsplit("}", 1)[-1] != "Relationship": continue
        typ = (r.get("Type") or "").rstrip("/").rsplit("/", 1)[-1].lower(); tgt = (r.get("Target") or "").strip()
        ext = (r.get("TargetMode") or "").lower() == "external"
        if typ in BAD_REL: raise _Bad(f"it contains a {typ} relationship (external workbook link, OLE object or ActiveX)")
        m = _SCHEME.match(tgt); sch = m.group(1).lower() if m and len(m.group(1)) > 1 else ""   # "C:\x" is a drive, not a scheme
        remote = ext or _is_unc(tgt) or bool(sch)
        if not remote: continue
        if typ == "hyperlink":
            if _is_unc(tgt) or (sch and sch not in ("http", "https", "mailto")) or (not sch and re.match(r"^[A-Za-z]:", tgt)):
                raise _Bad(f"a hyperlink points to a local, network or program address ({tgt[:60]!r}); only http, https and mailto links are accepted")
        elif typ == "attachedtemplate":   # Word writes file:///C:\...\Normal.dotm for its own template; only a URL / network share is the attack
            local = (sch == "file" and re.match(r"(?i)^file:///(?![/\\])", tgt)) or (not sch and not _is_unc(tgt))
            if not local: raise _Bad(f"the document loads a template from {tgt[:60]!r} (remote template injection)")
        else: raise _Bad(f"it links to an external {typ or 'resource'} ({tgt[:60]!r}); external links are not accepted")

def _dde(part, b, what):
    """Word field codes (DDE / DDEAUTO) and Excel DDE formulas (`cmd|'/c calc'!A1`), found with a real XML parse so split text and odd prefixes do not hide them."""
    codes = []
    try:
        for _, el in ET.iterparse(io.BytesIO(b), events=("end",)):
            tag = el.tag.rsplit("}", 1)[-1]
            if what == "instr":
                if tag == "instrText": codes.append(el.text or "")
                elif tag == "fldSimple": codes.append(next((v for k, v in el.attrib.items() if k.rsplit("}", 1)[-1] == "instr"), ""))
            elif tag == "f":
                s = re.sub(r'"[^"]*"', "", _html.unescape(el.text or ""))
                if "|" in s: raise _Bad(f"{part[:80]!r}: a formula calls another program (DDE)")
            if tag in ("p", "tr", "f", "row", "c"): el.clear()
    except ET.ParseError: raise _Bad(f"{part[:80]!r}: the XML is damaged") from None
    _dde_scan(part, "".join(codes))

def _dde_scan(part, s):
    if re.search(r"(?i)(?:^|[^A-Za-z])DDE(?:AUTO)?(?:[^A-Za-z]|$)", s): raise _Bad(f"{part[:80]!r}: a field runs another program (DDE / DDEAUTO)")

# ------------------------------------------------------------------ loading (Gmail attachments, uploads)
def sniff_attachment(data, name) -> str:
    """Gmail / Outlook attachment rule (the old `_sniff`): images by magic bytes, documents by docfiles.sniff_doc (macros, mismatching extension, unsupported types refused)."""
    import attachments as AT
    mime = AT.sniff(data)
    if mime: return mime
    try: return D.sniff_doc(data, name)[1]
    except ValueError as e: raise _Err(f"attachment {name} refused: {e}") from None

async def load(refs, *, max_total, max_count, sniff=None) -> list:
    """['<chat file id>' | '/api/files/<id>' | '/workspace/...'] -> [(name, mime, bytes)]. Everything is checked here, before any API call."""
    sniff = sniff or sniff_attachment
    refs = [] if refs is None else ([refs] if isinstance(refs, str) else list(refs))
    if len(refs) > max_count: raise _Err(f"too many attachments (max {max_count})")
    import attachments as AT
    mb = f"{max_total >> 20} MB"
    out, total = [], 0
    for ref in refs:
        ref = str(ref or "").strip(); m = _CHAT_FILE.match(ref)
        if m:
            import admin
            if admin.rls_ctx()[0] == "*": raise _Err(f"attachment not found or not allowed: {ref[:60]}")   # no user / workspace context = the unfiltered system context: never read a chat file there
            try: row = await AT._row(m.group(1).lower())   # RLS: only the running workspace's files
            except Exception: raise _Err(f"attachment not found or not allowed: {ref[:60]}") from None  # noqa: BLE001
            if int(row.get("size") or 0) > max_total: raise _Err(f"attachment {row.get('name') or ref[:40]} is larger than {mb}")
            data = await asyncio.to_thread(AT._bytes, row); name = D.safe_name(row.get("name"), "attachment")
        elif ref.startswith("/workspace/"):
            if ".." in ref.split("/") or "\x00" in ref: raise _Err(f"{ref[:80]}: a path with '..' is not allowed")
            import sbx_backend
            try: box = await sbx_backend.for_config(create=False)
            except Exception: box = None  # noqa: BLE001
            if box is None: raise _Err(f"{ref[:80]}: this agent has no computer, so there is no /workspace file to attach")
            try: data = await box.get(ref)
            except Exception as e: raise _Err(f"cannot read {ref[:80]}: {sbx_backend._file_error(e)}") from None  # noqa: BLE001
            name = D.safe_name(ref.rsplit("/", 1)[-1], "attachment")
        else: raise _Err("an attachment is a chat file id (as listed in the conversation) or a path under /workspace/")
        total += len(data)
        if not data: raise _Err(f"attachment {name} is empty")
        if total > max_total: raise _Err(f"attachments are larger than {mb} in total")
        out.append((name, sniff(data, name), data))
    return out

async def prepare_upload(ref_or_b64, name=None, *, mime=None) -> tuple:
    """The one call an upload tool makes. `ref_or_b64` = chat file id | /api/files/<id> | /workspace/... | base64 text (or raw bytes). -> (safe name, real mime, bytes), or _Err.
    `mime` (optional) is what the model declared; it is never trusted, but a declared type that contradicts the real content is refused."""
    if isinstance(ref_or_b64, (bytes, bytearray)): data = bytes(ref_or_b64); fname = name
    else:
        s = str(ref_or_b64 or "").strip()
        if not s: raise _Err("nothing to upload: give a chat file id, a /workspace/ path or the file content as base64")
        if _CHAT_FILE.match(s) or s.startswith("/workspace/"):
            (fname0, _m, data), = await load([s], max_total=MAX_UPLOAD, max_count=1, sniff=lambda d, n: "")   # checked below, with the final name
            fname = name or fname0
        else:
            if len(s) > MAX_UPLOAD * 4 // 3 + 4096: raise _Err(f"the content is larger than {MAX_UPLOAD >> 20} MB")
            data = repair_b64(s); fname = name
    if not fname: raise _Err("a file name with an extension (e.g. report.docx) is needed")
    fname = D.safe_name(fname, "file")
    real = check(fname, data)
    want = (mime or "").split(";")[0].strip().lower()
    if want and want != "application/octet-stream" and want != real: raise _Err(f"file {fname} refused: declared type {want} does not match the content ({real})")
    return fname, real, data
