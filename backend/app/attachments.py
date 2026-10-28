"""G4: self-hosted chat attachments (images) for the front-end UI. No third-party upload service.
- POST /api/files            multipart `file` -> {id, url, mime_type, size, sha256, name}; type sniffed from magic bytes, size/type limits enforced
- GET  /api/files/{id}       the bytes (workspace/owner scoped by RLS on chat_files)
- normalize_content(blocks)  server-side guard for /api/v1 run input: every image block becomes a LangChain standard content block
                             {"type":"image","url":"/api/files/<id>","mime_type":...,"id":...} (data: URLs / base64 are stored here first)
- LocalImageMiddleware       official AgentMiddleware.awrap_model_call: for the MODEL request only, local image URLs are materialised to
                             base64 standard blocks (state/checkpoints keep the small URL block, never the bytes)
Bytes live under AOS_FILES_DIR (content-addressed by sha256)."""
import base64, binascii, hashlib, os, re, uuid
from pathlib import Path
from urllib.parse import quote
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool as lc_tool
import deepagent_api as A
import docfiles as D

router = APIRouter(tags=["attachments"])
DIR = Path(os.environ.get("AOS_FILES_DIR", str(Path(__file__).resolve().parent.parent / "var" / "files")))
MAX_BYTES = int(os.environ.get("AOS_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))      # per image
MAX_PER_MESSAGE = int(os.environ.get("AOS_FILE_MAX_PER_MESSAGE", "5"))              # images + documents together (GD6.8)
INLINE_TOKENS = int(os.environ.get("AOS_DOC_INLINE_TOKENS", "30000"))               # converted text <= this (chars/4) -> inlined, else KB search
INDEX_MAX_CHARS = int(os.environ.get("AOS_DOC_INDEX_MAX_CHARS", "400000"))          # long docs: first N chars are embedded
ALLOWED = ("image/png", "image/jpeg", "image/webp", "image/gif")
URL_RE = re.compile(r"^/api/files/([0-9a-f-]{36})$")
DDL = """CREATE TABLE IF NOT EXISTS chat_files(id UUID PRIMARY KEY, sha256 TEXT NOT NULL, mime_type TEXT NOT NULL, size INT NOT NULL,
  name TEXT, created_by INT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE chat_files ADD COLUMN IF NOT EXISTS kind TEXT, ADD COLUMN IF NOT EXISTS md TEXT, ADD COLUMN IF NOT EXISTS status TEXT,
  ADD COLUMN IF NOT EXISTS reason TEXT, ADD COLUMN IF NOT EXISTS pages INT, ADD COLUMN IF NOT EXISTS units TEXT, ADD COLUMN IF NOT EXISTS tokens INT,
  ADD COLUMN IF NOT EXISTS enc BOOLEAN NOT NULL DEFAULT false;   -- 9.P2: enc=true -> bytes ở var/files/_enc/<id> (AES-GCM theo workspace) + md dạng "aosenc$<id>$<token>"
CREATE TABLE IF NOT EXISTS chat_thread_files(thread_id TEXT NOT NULL, file_id UUID NOT NULL, PRIMARY KEY(thread_id, file_id))"""

async def init():
    DIR.mkdir(parents=True, exist_ok=True)
    await A.q(DDL)
    import chat_projects; await chat_projects.init()   # GĐ8.9 chat_projects / chat_project_files / lg_threads.project_id (needs chat_files; before RLS)

def sniff(b: bytes) -> str | None:
    """Content type from magic bytes (the client-declared type is never trusted)."""
    if b.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if b.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "image/webp"
    if b[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    return None

def _meta(r) -> dict:
    d = {"id": str(r["id"]), "url": f"/api/files/{r['id']}", "mime_type": r["mime_type"], "size": r["size"], "sha256": r["sha256"], "name": r["name"]}
    if r.get("kind"):
        mode = "inline" if (r.get("tokens") or 0) <= INLINE_TOKENS else "search"
        d.update(kind=r["kind"], status=r.get("status"), reason=r.get("reason"), pages=r.get("pages"), units=r.get("units"), tokens=r.get("tokens"), mode=mode)
    return d

def _ws_enc() -> int | None:
    """Workspace của người đang tải lên nếu nó bật mã hoá 'attachments', ngược lại None."""
    import admin, aos_crypto as C
    w = (admin.cur() or {}).get("ws")
    return int(w) if str(w).isdigit() and C.on("attachments", int(w)) else None

def _save_enc(data: bytes, fid: str, ws: int):
    import aos_crypto as C, crypto
    C.write_atomic(C.enc_path(fid), crypto.seal_bytes(data, ws, C.blob_ctx(fid)))

def _save(data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    p = DIR / sha[:2] / sha
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True); tmp = p.with_suffix(".tmp"); tmp.write_bytes(data); tmp.replace(p)
    return sha

async def store(data: bytes, name: str | None = None, declared: str | None = None) -> dict:
    import admin
    if not data: raise HTTPException(422, "empty file")
    mime = sniff(data)
    if mime is None:   # not an image -> document path (GD6.8)
        if len(data) > D.DOC_MAX_BYTES: raise HTTPException(413, f"file too large ({len(data)} B > {D.DOC_MAX_BYTES} B)")
        try: kind, dmime = D.sniff_doc(data, name)
        except ValueError as e: raise HTTPException(415, f"unsupported file: {e}") from None
        res, status, reason = None, "ok", None
        try: res = await D.convert(data, kind)
        except Exception as e:  # noqa: BLE001  (file is kept, flagged unreadable)
            status, reason = "error", str(e)[:300] or e.__class__.__name__
        fid = str(uuid.uuid4()); md = (res or {}).get("md"); ews = _ws_enc()
        if ews is not None:   # 9.P2: mã hoá bytes + văn bản trích xuất; sha256 (của bản rõ) vẫn lưu để đối chiếu/xoá
            import aos_crypto as C
            sha = hashlib.sha256(data).hexdigest(); _save_enc(data, fid, ews); stored_md = C.seal_text("attachments", "chat_files.md", fid, md, ews)
        else: sha = _save(data); stored_md = md
        await A.q("INSERT INTO chat_files(id,sha256,mime_type,size,name,created_by,kind,md,status,reason,pages,units,tokens,enc) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (fid, sha, dmime, len(data), (name or "")[:200] or None, (admin.cur() or {}).get("id"), kind, stored_md, status, reason,
                   (res or {}).get("pages"), (res or {}).get("units"), D.est_tokens(md) if md else None, ews is not None))
        return _meta(await A.q("SELECT * FROM chat_files WHERE id=%s", (fid,), one=True))
    if D.ext_of(name) in D.MIME: raise HTTPException(415, f"unsupported file: đuôi .{D.ext_of(name)} không khớp nội dung thật ({mime})")
    if len(data) > MAX_BYTES: raise HTTPException(413, f"image too large ({len(data)} B > {MAX_BYTES} B)")
    if mime not in ALLOWED: raise HTTPException(415, f"unsupported file type (allowed: {', '.join(ALLOWED)})")
    if declared and declared not in (mime, "application/octet-stream") and declared.startswith("image/") and declared != mime and not (declared == "image/jpg" and mime == "image/jpeg"):
        raise HTTPException(415, f"declared type {declared} does not match content ({mime})")
    fid = str(uuid.uuid4()); ews = _ws_enc()
    if ews is not None: sha = hashlib.sha256(data).hexdigest(); _save_enc(data, fid, ews)
    else: sha = _save(data)
    await A.q("INSERT INTO chat_files(id,sha256,mime_type,size,name,created_by,enc) VALUES(%s,%s,%s,%s,%s,%s,%s)",
              (fid, sha, mime, len(data), (name or "")[:200] or None, (admin.cur() or {}).get("id"), ews is not None))
    return {"id": fid, "url": f"/api/files/{fid}", "mime_type": mime, "size": len(data), "sha256": sha, "name": name}

async def _row(fid: str):
    try: uuid.UUID(fid)
    except ValueError: raise HTTPException(404, "file not found")  # noqa: B904
    r = await A.q("SELECT * FROM chat_files WHERE id=%s", (fid,), one=True)
    if not r: raise HTTPException(404, "file not found")
    return r

def _bytes(r) -> bytes:
    if r.get("enc"):   # 9.P2: giải mã trong suốt (AAD gắn id tệp; workspace lấy từ DEK, RLS đã lọc dòng)
        import aos_crypto as C, crypto
        return crypto.unseal_bytes(C.enc_path(r["id"]).read_bytes(), None, C.blob_ctx(r["id"]))
    return (DIR / r["sha256"][:2] / r["sha256"]).read_bytes()

# GĐ4: per-user upload rate limit (sliding window, in the API process that owns /api/files). 429 + Retry-After.
RATE_MIN = int(os.environ.get("AOS_UPLOADS_PER_MIN", "12"))
RATE_DAY = int(os.environ.get("AOS_UPLOADS_PER_DAY", "300"))
_hits: dict = {}

def rate_check(user_key, now: float | None = None):
    """Raise 429 when this user exceeded RATE_MIN uploads in 60 s or RATE_DAY in 24 h."""
    import time
    now = time.time() if now is None else now
    h = [t for t in _hits.get(user_key, []) if now - t < 86400]
    last_min = [t for t in h if now - t < 60]
    if len(last_min) >= RATE_MIN:
        raise HTTPException(429, f"too many uploads (max {RATE_MIN}/min)", headers={"Retry-After": str(max(1, int(60 - (now - last_min[0])) + 1))})
    if len(h) >= RATE_DAY:
        raise HTTPException(429, f"too many uploads today (max {RATE_DAY}/day)", headers={"Retry-After": str(max(1, int(86400 - (now - h[0])) + 1))})
    h.append(now); _hits[user_key] = h
    if len(_hits) > 5000:   # bound memory: drop idle users
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 86400]: _hits.pop(k, None)

@router.post("/api/files")
async def upload(file: UploadFile = File(...)):
    import admin
    u = admin.cur() or {}
    rate_check(u.get("id") or u.get("username") or "anon")
    data = await file.read(D.DOC_MAX_BYTES + 1)
    return await store(data, file.filename, file.content_type)

@router.get("/api/files/config")
async def config(): return {"max_bytes": MAX_BYTES, "max_doc_bytes": D.DOC_MAX_BYTES, "max_per_message": MAX_PER_MESSAGE, "allowed": list(ALLOWED),
                                                  "documents": list(D.MIME), "inline_tokens": INLINE_TOKENS}

@router.get("/api/files/{fid}")
async def download(fid: str, inline: int = 0):
    r = await _row(fid)
    doc = bool(r.get("kind")) and not (inline and r["mime_type"] == "application/pdf")   # GĐ8.6: ?inline=1 = PDF preview in the chat file card
    fn = quote((r.get("name") or "").rsplit("/", 1)[-1])
    return Response(_bytes(r), media_type=r["mime_type"], headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
                                                                    "Content-Security-Policy": "default-src 'none'",
                                                                    "Content-Disposition": ("attachment" if doc else "inline") + (f"; filename*=UTF-8''{fn}" if fn else "")})

@router.get("/api/files/{fid}/meta")
async def file_meta(fid: str): return _meta(await _row(fid))

@router.get("/api/files/{fid}/text")
async def file_text(fid: str):
    r = await _row(fid)
    if r.get("md") is None: raise HTTPException(404, "no converted text for this file")
    return {"id": str(r["id"]), "name": r["name"], "md": r["md"]}

# ------------------------------------------------------------ run input normalisation (server.py calls this)
def _data_url(u: str):
    m = re.match(r"^data:([\w.+/-]+)?;base64,(.*)$", u, re.S)
    if not m: return None
    try: return m.group(1), base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError): raise HTTPException(422, "invalid base64 image")  # noqa: B904

async def _standard(url: str | None = None, b64: str | None = None, mime: str | None = None, name: str | None = None) -> dict:
    if url and (m := URL_RE.match(url)):
        r = await _row(m.group(1))
        return {"type": "image", "url": url, "mime_type": r["mime_type"], "id": str(r["id"]), "extras": {"size": r["size"], "name": r["name"]}}
    if url and url.startswith("data:"):
        mime, data = _data_url(url)
    elif b64:
        try: data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError): raise HTTPException(422, "invalid base64 image")  # noqa: B904
    elif url:
        raise HTTPException(422, "remote image URLs are not accepted; upload the image to /api/files first")
    else:
        raise HTTPException(422, "image block without data")
    f = await store(data, name, mime)
    if f.get("kind"): raise HTTPException(415, "an image block must contain an image (use a file block for documents)")
    return {"type": "image", "url": f["url"], "mime_type": f["mime_type"], "id": f["id"], "extras": {"size": f["size"], "name": name}}

def _file_ref(b: dict) -> str | None:
    for k in ("url", "data", "id"):
        v = str(b.get(k) or "")
        if m := URL_RE.match(v): return m.group(1)
        if k == "id" and re.fullmatch(r"[0-9a-f-]{36}", v): return v
    return None

async def _doc_block(b: dict, thread_id: str | None) -> dict:
    """Standard file block {"type":"file","url":"/api/files/<id>"} (or base64 data + filename, stored first). The stored/checkpointed block stays
    small; the text reaches the model only through DocMiddleware. Unreadable files are refused here (422), not silently dropped."""
    fid = _file_ref(b)
    if not fid:
        raw = b.get("base64") or b.get("data")
        if not raw: raise HTTPException(422, "file block without data")
        try: data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError): raise HTTPException(422, "invalid base64 file")  # noqa: B904
        fid = (await store(data, b.get("filename") or (b.get("metadata") or {}).get("filename") or (b.get("extras") or {}).get("name")))["id"]
    r = await _row(fid)
    if not r.get("kind"): raise HTTPException(415, "not a document file")
    if r.get("status") != "ok": raise HTTPException(422, f"tệp {r['name']} không đọc được: {r.get('reason')}")
    m = _meta(r)
    if thread_id:
        await A.q("INSERT INTO chat_thread_files(thread_id,file_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (thread_id, fid))
        if m["mode"] == "search": await index_for_thread(r, thread_id)
    return {"type": "file", "url": m["url"], "mime_type": m["mime_type"], "id": fid,
            "extras": {k: m[k] for k in ("size", "name", "kind", "pages", "units", "tokens", "mode")}}

async def index_for_thread(r, thread_id: str):
    """Long document: chunk (same splitter as the KB) + embed into kb_docs kind='thread', scoped to (workspace, user, thread). Idempotent."""
    if await A.q("SELECT 1 FROM kb_docs WHERE kind='thread' AND thread_id=%s AND file_id=%s", (thread_id, str(r["id"])), one=True): return
    await index_doc(r, f"thread:{thread_id}", "thread", thread_id)

async def aembed_masked(texts: list) -> list:
    """GĐ9.P2: embeddings are an external model call too. When the workspace has PII masking on (gateway scope) the provider only sees `[PHONE]`-style masked text
    (documents and search queries alike); the ORIGINAL chunk text stays in kb_chunks (local, RLS-scoped) and is masked/restored by the gateway when it reaches a model."""
    import gateway as gw, pii
    cfg = await gw._pii_cfg()
    if cfg: texts = await pii.amask_obj(list(texts), {**cfg, "mode": "mask"})
    return await A.aembed(texts)

async def index_doc(r, kb: str, kind: str, thread_id: str | None = None, owner_id: int | None = None):
    """Chunk + embed one converted document into kb_docs/kb_chunks (kind 'thread' = 6.8 chat attachment, 'project' = GĐ8.9 project file)."""
    import admin
    u = admin.cur() or {}
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    chunks = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100).split_text((r["md"] or "")[:INDEX_MAX_CHARS])
    embs = []
    for i in range(0, len(chunks), 50): embs += await aembed_masked(chunks[i:i + 50])
    did = (await A.q("INSERT INTO kb_docs(kb,title,n_chunks,kind,thread_id,user_id,file_id) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                     (kb, r["name"], len(chunks), kind, thread_id, owner_id or u.get("id") or 0, str(r["id"])), one=True))["id"]
    async with A.S["pool"].connection() as c:
        ctx = A.S["rls"]()
        await c.execute("SELECT set_config('app.ws',%s,false), set_config('app.uid',%s,false), set_config('app.role',%s,false), set_config('role', CASE WHEN %s ~ '^[0-9]+$' THEN 'aos_rls' ELSE 'none' END, false)", ctx + (ctx[0],))
        async with c.cursor() as cur:
            await cur.executemany("INSERT INTO kb_chunks(doc_id,idx,text,embedding) VALUES(%s,%s,%s,%s::vector)", [(did, i, ch, str(e)) for i, (ch, e) in enumerate(zip(chunks, embs))])

async def search_thread_files(query: str, thread_id: str, k: int = 4) -> list:
    import admin
    u = admin.cur() or {}
    if not u.get("id") or not thread_id: return []
    v = str((await aembed_masked([query]))[0])
    rows = await A.q("SELECT d.title, c.idx, c.text, 1-(c.embedding <=> %s::vector) AS score FROM kb_chunks c JOIN kb_docs d ON d.id=c.doc_id "
                     "WHERE d.kind='thread' AND d.thread_id=%s AND d.user_id=%s AND d.workspace_id=%s ORDER BY c.embedding <=> %s::vector LIMIT %s",
                     (v, thread_id, u["id"], u.get("ws") or 1, v, k))
    return [{**r, "score": round(float(r["score"]), 4)} for r in rows]

_DEC = {"dir": None}
def _plain_path(r) -> str:
    """Đường dẫn bản rõ cho sandbox. Tệp mã hoá được giải mã vào thư mục tạm trên RAM (/dev/shm nếu có), quyền 0700, dọn sau 15 phút / khi thoát."""
    if not r.get("enc"): return str(DIR / r["sha256"][:2] / r["sha256"])
    import atexit, shutil, tempfile, time
    d = _DEC["dir"]
    if d is None or not os.path.isdir(d):
        d = _DEC["dir"] = tempfile.mkdtemp(prefix="aos_dec_", dir="/dev/shm" if os.path.isdir("/dev/shm") else None); atexit.register(shutil.rmtree, d, True)
    now = time.time()
    for f in os.listdir(d):   # dọn tệp cũ
        fp = os.path.join(d, f)
        if now - os.path.getmtime(fp) > 900:
            try: os.unlink(fp)
            except OSError: pass
    p = os.path.join(d, str(r["id"]))
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f: f.write(_bytes(r))
    return p

async def thread_data_files(thread_id: str) -> list:
    """csv/xlsx originals attached to this thread (RLS: only files the caller may see) -> [(host_path, safe_name)] for the sandbox."""
    if not thread_id: return []
    rows = await A.q("SELECT f.id, f.enc, f.sha256, f.name, f.kind FROM chat_thread_files t JOIN chat_files f ON f.id=t.file_id WHERE t.thread_id=%s AND f.kind IN ('csv','xlsx') ORDER BY f.created_at",
                     (thread_id,))
    out, used = [], set()
    for r in rows:
        n = D.safe_name(r["name"], "data." + r["kind"]); base = n
        i = 1
        while n in used: i += 1; n = f"{i}_{base}"
        used.add(n); out.append((_plain_path(r), n))
    return out

def _thread_of(config) -> str | None:
    t = ((config or {}).get("configurable") or {}).get("thread_id") or ""
    return t.split(":", 1)[1] if ":" in t else (t or None)

async def normalize_content(content, thread_id: str | None = None):
    """Run-input guard: list content -> text blocks + standard image blocks (stored locally); limits enforced."""
    if not isinstance(content, list): return content
    out, n = [], 0
    for b in content:
        if isinstance(b, str): out.append({"type": "text", "text": b}); continue
        if not isinstance(b, dict): continue
        t = b.get("type")
        if t in ("text", "text_delta"):
            if (b.get("text") or "").strip(): out.append({"type": "text", "text": b["text"]})
            continue
        if t == "image_url":
            iu = b.get("image_url"); url = iu if isinstance(iu, str) else (iu or {}).get("url")
            blk = await _standard(url=url)
        elif t == "image":
            if b.get("source_type") == "base64" or b.get("base64") or b.get("data"):
                blk = await _standard(b64=b.get("base64") or b.get("data"), mime=b.get("mime_type"), name=(b.get("extras") or {}).get("name"))
            else:
                blk = await _standard(url=b.get("url"))
        elif t == "file" and str(b.get("mime_type", "")).startswith("image/"):
            blk = await _standard(url=b.get("url"), b64=b.get("base64") or b.get("data"), mime=b.get("mime_type"), name=b.get("filename"))
        elif t == "file":
            blk = await _doc_block(b, thread_id)
        else:
            raise HTTPException(415, f"unsupported content block type: {t}")
        n += 1
        if n > MAX_PER_MESSAGE: raise HTTPException(413, f"too many files in one message (max {MAX_PER_MESSAGE})")
        out.append(blk)
    return out

# ------------------------------------------------------------ model-side materialisation
def _fence(md: str) -> str:
    runs = re.findall(r"`+", md); f = "`" * max([3] + [len(x) + 1 for x in runs])
    return f"{f}markdown" + chr(10) + md + chr(10) + f

async def _doc_text(fid: str) -> tuple[str, bool]:
    """-> (text block for the model request, is_search_mode)"""
    r = await _row(fid); name = (r["name"] or "tệp").replace('"', "'")
    if (r.get("tokens") or 0) <= INLINE_TOKENS:
        nl = chr(10)
        return f'<attachment name="{name}" file="{fid}">{nl}### Tệp đính kèm: {name}{nl}{_fence(r["md"] or "")}{nl}</attachment>', False
    return (f'<attachment name="{name}" file="{fid}">Tệp dài (~{r["tokens"]} token) nên không chèn thẳng. '
            f'Dùng công cụ search_attached_files(query) để tra cứu nội dung tệp "{name}".</attachment>'), True

@lc_tool
async def search_attached_files(query: str, config: RunnableConfig = None) -> str:
    """Search inside the LONG documents the user attached to this conversation (semantic search over their chunks). Use it when the user asks about
    an attached file that was too long to be shown inline. Returns the best matching passages with file name and chunk number."""
    rs = await search_thread_files(query, _thread_of(config) or "")
    return (chr(10) * 2).join(f"[{r['title']} #{r['idx']} · {r['score']}] {r['text']}" for r in rs) or "Không tìm thấy trong các tệp đính kèm."

_OCR_CACHE: dict = {}   # (file id, masking config) -> (bytes, mime) | None ; small LRU so one photo is OCR'd once per conversation, not once per model call
_OCR_SEM = None

async def _image_block(r) -> dict:
    """Standard image block for the model. GĐ9.P2: when the workspace masks PII, the image is OCR'd locally (ocr_mask) and personal data is blacked out on a copy;
    if it cannot be checked the image is replaced by a note. The stored original is never changed."""
    import asyncio, gateway as gw
    data, mime, fid = _bytes(r), r["mime_type"], str(r["id"])
    cfg = await gw._pii_cfg()
    if cfg:
        import json, ocr_mask
        global _OCR_SEM
        key = (fid, json.dumps({k: cfg.get(k) for k in ("entities", "threshold", "ner")}, sort_keys=True, default=str))
        if key not in _OCR_CACHE:
            _OCR_SEM = _OCR_SEM or asyncio.Semaphore(1)   # one OCR at a time: bounded RAM
            async with _OCR_SEM:
                res = await asyncio.to_thread(ocr_mask.mask_image, data, cfg)
            if len(_OCR_CACHE) >= 32: _OCR_CACHE.pop(next(iter(_OCR_CACHE)))
            _OCR_CACHE[key] = res
        res = _OCR_CACHE[key]
        if res is None:
            return {"type": "text", "text": f"[image {fid} not shown: personal-data masking is on and the image could not be checked]"}
        if res[2]: data, mime = res[0], res[2]
    return {"type": "image", "base64": base64.b64encode(data).decode(), "mime_type": mime, "id": fid}

class LocalImageMiddleware(AgentMiddleware):
    """Before each model call (main agent and sub-agents): standard image blocks that point at /api/files/<id> are replaced, in the
    request only, by base64 standard blocks so any provider (and the local fake) receives the actual image. GD6.8: document file blocks
    become a fenced text block (short docs) or a note pointing at search_attached_files (long docs; the tool is hidden when not needed)."""
    tools = [search_attached_files]
    async def _mat(self, msgs):
        out, changed, search = [], False, False
        for m in msgs:
            c = getattr(m, "content", None)
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") in ("image", "file") and URL_RE.match(str(b.get("url", ""))) for b in c):
                nc = []
                for b in c:
                    mm = URL_RE.match(str(b.get("url", ""))) if isinstance(b, dict) and b.get("type") in ("image", "file") else None
                    if mm and b.get("type") == "file":
                        try:
                            txt, s_ = await _doc_text(mm.group(1)); search = search or s_
                            nc.append({"type": "text", "text": txt})
                        except Exception:  # noqa: BLE001
                            nc.append({"type": "text", "text": f"[file {mm.group(1)} unavailable]"})
                    elif mm:
                        try:
                            r = await _row(mm.group(1))
                            blk = await _image_block(r)
                            nc.append(blk)
                        except Exception:  # noqa: BLE001  (file gone) -> tell the model instead of failing the run
                            nc.append({"type": "text", "text": f"[image {mm.group(1)} unavailable]"})
                    else: nc.append(b)
                m = m.model_copy(update={"content": nc}); changed = True
            out.append(m)
        return out, changed, search

    async def awrap_model_call(self, request, handler):
        msgs, changed, search = await self._mat(request.messages)
        req = request.override(messages=msgs) if changed else request
        if not search:
            req = req.override(tools=[t for t in req.tools if (t.name if hasattr(t, "name") else t.get("name")) != "search_attached_files"])
        return await handler(req)
