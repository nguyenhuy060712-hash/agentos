"""GĐ12.82: fake Google Drive v3 for the fake workspace server (loaded through EXTRA_ROUTES in tests/fake_workspace_api.py).
Takes over the old main-file routes through hooks (`hook_drive_files`, `hook_drive_file`) so ONE per-person store answers search / get / media, and adds:
  GET /drive/v3/files/{id}/export?mimeType=...  (Google Doc -> text/plain | docx | pdf; Google Sheet -> text/csv | xlsx | pdf)
  POST /drive/v3/files                          (create a folder, or an empty file: JSON metadata)
  POST /upload/drive/v3/files?uploadType=multipart   (multipart/related: JSON metadata + media; ALWAYS a new file, even when the name exists, like Drive)
Each person starts with the files of PEOPLE (same ids / names / text as before), plus a folder, a Google Doc, a Google Sheet and a binary xlsx (`bin-alice`). The other person's token gets 404 for them.
Scopes: read = drive.readonly | drive ; write (create / upload) = drive.file | drive (enforced when S["enforce_scopes"] is on). ST["uploads"] records every upload that was accepted."""
import io, json, re, secrets
from aiohttp import web

GA = "https://www.googleapis.com/auth/"
RD, WR = (GA + "drive.readonly", GA + "drive"), (GA + "drive.file", GA + "drive")
FOLDER, GDOC, GSHEET = "application/vnd.google-apps.folder", "application/vnd.google-apps.document", "application/vnd.google-apps.spreadsheet"
DOCX, XLSX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ST = {"files": {}, "uploads": [], "seq": 0}
_H = {}

def reset(S):
    ST["files"].clear(); ST["uploads"].clear(); ST["seq"] = 0

def _nid(p="gf"): ST["seq"] += 1; return f"{p}-{ST['seq']}-{secrets.token_hex(2)}"

def make_xlsx(rows) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    for r in rows: ws.append(list(r))
    b = io.BytesIO(); wb.save(b); return b.getvalue()

def make_docx(text) -> bytes:
    import docx
    d = docx.Document()
    for line in text.split("\n"): d.add_paragraph(line)
    b = io.BytesIO(); d.save(b); return b.getvalue()

def _add(owner, fid, name, mime, data, parents=None, **kw):
    ST["files"][fid] = {"id": fid, "owner": owner, "name": name, "mime": mime, "data": data, "parents": list(parents or []), "trashed": False, "modified": "2026-10-20T08:00:00Z", **kw}

def _seed():
    if ST["files"]: return
    import fake_workspace_api as F
    for who, p in F.PEOPLE.items():
        for fid, name, mime, text in p["files"]: _add(who, fid, name, mime, text.encode())
    _add("alice", "fo-alice", "Hồ sơ khách hàng", FOLDER, b"")
    _add("alice", "gd-alice", "Kế hoạch tháng 11 (Doc)", GDOC, "Kế hoạch tháng 11\nMục tiêu doanh thu 2 tỷ.".encode())
    _add("alice", "gs-alice", "Bảng lương ALICE (Sheet)", GSHEET, b"Ten,Luong\nLan,10000000\nHuong,12000000\n")
    _add("alice", "bin-alice", "bao-cao-q3.xlsx", XLSX, make_xlsx([["Quý", "Doanh thu"], ["Q3", 1200]]))
    _add("bob", "gd-bob", "BOB-SECRET kế hoạch (Doc)", GDOC, "BOB-SECRET nội dung".encode())

def _err(status, message): return web.json_response({"error": {"code": status, "message": message, "status": "NOT_FOUND" if status == 404 else "INVALID_ARGUMENT"}}, status=status)

def _meta(f):
    return {"id": f["id"], "name": f["name"], "mimeType": f["mime"], "size": str(len(f["data"])), "modifiedTime": f["modified"], "parents": f["parents"] or ["root"],
            "webViewLink": f"https://drive.google.com/file/d/{f['id']}/view"}

def _mine(who, fid):
    _seed(); f = ST["files"].get(fid)
    return f if f and f["owner"] == who and not f["trashed"] else None

def hook_drive_files(who, query):
    _seed(); q = query.get("q", ""); term = q.split("'")[1].lower() if "'" in q else ""
    fs = [f for f in ST["files"].values() if f["owner"] == who and not f["trashed"] and f["mime"] != FOLDER
          and (term in f["name"].lower() or term in (f["data"].decode("utf-8", "ignore").lower() if f["mime"].startswith(("text/", GDOC, GSHEET)) else ""))]
    n = int(query.get("pageSize") or 100)
    return {"files": [_meta(f) for f in fs[:n]]}

def hook_drive_file(who, fid, query):
    _seed(); f = ST["files"].get(fid)
    if f is None: return None
    f = _mine(who, fid)
    if not f: return _err(404, "File not found: " + fid)
    if query.get("alt") == "media":
        if f["mime"].startswith("application/vnd.google-apps."): return _err(403, "Only files with binary content can be downloaded. Use Export with Docs Editors files.")
        return web.Response(body=f["data"], content_type=f["mime"].split(";")[0])
    return _meta(f)

def _export(f, mime):
    text = f["data"].decode("utf-8", "replace")
    if mime == "application/pdf": return b"%PDF-1.4\n" + text.encode() + b"\n%%EOF\n"
    if f["mime"] == GDOC:
        if mime == "text/plain": return text.encode()
        if mime == DOCX: return make_docx(text)
    if f["mime"] == GSHEET:
        if mime == "text/csv": return f["data"]
        if mime == XLSX: return make_xlsx([line.split(",") for line in text.splitlines()])
    return None

async def export(req):
    who = _H["who"](req, "google", *RD); f = _mine(who, req.match_info["id"])
    if not f: return _err(404, "File not found: " + req.match_info["id"])
    if not f["mime"].startswith("application/vnd.google-apps."): return _err(403, "Export only supports Docs Editors files.")
    data = _export(f, req.query.get("mimeType", ""))
    if data is None: return _err(400, "The requested conversion is not supported.")
    return web.Response(body=data, content_type=req.query["mimeType"])

def _parent_ok(who, parents):
    for p in parents or []:
        if p == "root": continue
        pf = _mine(who, p)
        if not pf or pf["mime"] != FOLDER: return False
    return True

async def create(req):
    who = _H["who"](req, "google", *WR); _seed(); b = await req.json()
    if not _parent_ok(who, b.get("parents")): return _err(404, "File not found: " + str((b.get("parents") or [""])[0]))
    fid = _nid("fo" if b.get("mimeType") == FOLDER else "gf")
    _add(who, fid, str(b.get("name") or "Untitled"), b.get("mimeType") or "application/octet-stream", b"", b.get("parents"))
    return web.json_response(_meta(ST["files"][fid]))

async def upload(req):
    who = _H["who"](req, "google", *WR); _seed()
    ct = req.headers.get("Content-Type", ""); m = re.search(r'boundary="?([^";]+)"?', ct)
    if req.query.get("uploadType") != "multipart" or not ct.startswith("multipart/related") or not m: return _err(400, "Only uploadType=multipart is supported by this fake.")
    body = await req.read(); parts = [p for p in body.split(b"--" + m.group(1).encode()) if p.strip(b"\r\n-")]
    if len(parts) != 2: return _err(400, "Multipart body must have a metadata part and a media part.")
    def split(p):
        head, _, data = p.lstrip(b"\r\n").partition(b"\r\n\r\n"); return head.decode("latin-1"), (data[:-2] if data.endswith(b"\r\n") else data)
    h1, d1 = split(parts[0]); h2, d2 = split(parts[1])
    try: meta = json.loads(d1.decode("utf-8"))
    except ValueError: return _err(400, "Invalid metadata JSON.")
    if not _parent_ok(who, meta.get("parents")): return _err(404, "File not found: " + str((meta.get("parents") or [""])[0]))
    mime = (re.search(r"(?i)content-type:\s*([^\r\n;]+)", h2) or [0, "application/octet-stream"])[1].strip()
    fid = _nid(); _add(who, fid, str(meta.get("name") or "Untitled"), mime, d2, meta.get("parents"))   # always NEW: same name twice = two files
    ST["uploads"].append({"who": who, "id": fid, "name": ST["files"][fid]["name"], "mime": mime, "size": len(d2), "parents": meta.get("parents") or []})
    return web.json_response(_meta(ST["files"][fid]))

def routes(app, S, helpers):
    _H.update(helpers)
    app.add_routes([web.get("/drive/v3/files/{id}/export", export), web.post("/drive/v3/files", create), web.post("/upload/drive/v3/files", upload)])
