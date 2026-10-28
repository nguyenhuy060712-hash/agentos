"""GĐ9.1 Context Hub = versioned context packs (24/10/2026), comparable to LangSmith Context Hub / Prompt Hub.
A pack is a set of text files: AGENTS.md (instructions -> agent memory), skills/<name>/SKILL.md (+ assets), prompts/*, policies/*.
- Immutable commits: hash = sha256(parent, tree hash, author, message, created_at); the tree maps path -> blob sha256 (blobs are
  de-duplicated inside a pack, so a 1-line change stores 1 new blob). Mutable tags (prod, staging, any name) point to commits;
  every tag move is logged (rollback = move the tag back, or `restore` = a NEW commit with the old content).
- File-level diff between any two refs (added / removed / modified + side-by-side rows + unified text).
- Tables context_packs / context_blobs / context_commits / context_tags / context_tag_events: workspace_id + RLS (admin.RLS_DIRECT).
- Agents pin `context_pack = "pack@tag"` (or "@<commit prefix>", "@latest"). At build time the pack is mounted READ-ONLY at /pack/ in the
  deepagents virtual filesystem through PackBackend, a subclass of deepagents' own ContextHubBackend (same read/ls/grep/glob code) whose
  source is our Postgres snapshot instead of the LangSmith Hub and whose write ops are refused. /pack/skills/ -> SkillsMiddleware,
  /pack/AGENTS.md -> MemoryMiddleware (reloaded every run). The built agent is cached per (agent, version); the cache entry is stamped with the
  commit hash and is rebuilt when the ref now resolves to another commit => re-tagging switches agents on the next run, no restart.
- Old `contexts` / `context_versions` API (ui_api) keeps working: rows are migrated once (one commit per published version, tag prod) and
  every later legacy write is mirrored into its pack (mirror_legacy, one-way legacy -> pack)."""
import difflib, hashlib, io, json, posixpath, re, unicodedata, zipfile
from collections import OrderedDict
from datetime import datetime, timezone
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS context_packs(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, name TEXT NOT NULL, description TEXT,
  legacy_context_id INT, created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, name));
CREATE TABLE IF NOT EXISTS context_blobs(workspace_id INT NOT NULL DEFAULT 1, pack_id INT NOT NULL REFERENCES context_packs(id) ON DELETE CASCADE,
  sha TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(pack_id, sha));
CREATE TABLE IF NOT EXISTS context_commits(workspace_id INT NOT NULL DEFAULT 1, pack_id INT NOT NULL REFERENCES context_packs(id) ON DELETE CASCADE,
  hash TEXT NOT NULL, seq INT NOT NULL, parent TEXT, tree JSONB NOT NULL, tree_hash TEXT NOT NULL, author TEXT, message TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(pack_id, hash), UNIQUE(pack_id, seq));
CREATE TABLE IF NOT EXISTS context_tags(workspace_id INT NOT NULL DEFAULT 1, pack_id INT NOT NULL REFERENCES context_packs(id) ON DELETE CASCADE,
  tag TEXT NOT NULL, hash TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_by TEXT, PRIMARY KEY(pack_id, tag));
CREATE TABLE IF NOT EXISTS context_tag_events(id BIGSERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1,
  pack_id INT NOT NULL REFERENCES context_packs(id) ON DELETE CASCADE, tag TEXT NOT NULL, from_hash TEXT, to_hash TEXT,
  at TIMESTAMPTZ NOT NULL DEFAULT now(), by TEXT);
CREATE INDEX IF NOT EXISTS context_tag_events_pack ON context_tag_events(pack_id, at DESC);
"""
TABLES = ["context_packs", "context_blobs", "context_commits", "context_tags", "context_tag_events"]   # -> admin.RLS_DIRECT

NAME_RX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
TAG_RX = re.compile(r"^[\w\-.]{1,40}$")
HEX_RX = re.compile(r"^[0-9a-f]{6,64}$")
MAX_FILES, MAX_FILE, MAX_TOTAL = 200, 200_000, 5 * 1024 * 1024
RO = "Read-only: context packs are changed by commits in the Context Hub, not by the agent"

def A():
    import deepagent_api
    return deepagent_api

def _sha(t: str) -> str: return hashlib.sha256(t.encode("utf-8")).hexdigest()
def _short(h): return (h or "")[:8]
def _me() -> str:
    import admin
    return (admin.USER.get() or {}).get("username") or "system"
def _no_demo():
    import admin, gateway as gw
    if admin.is_demo() or admin.cur_ws() in gw.DEMO_WS: raise HTTPException(403, "Off in the demo workspace")
def _is_admin() -> bool:
    import admin
    return (admin.cur() or {}).get("role") == "admin" and not admin.is_demo()
def _guard(pack):
    """GĐ12.86: a skill pack with a recorded creator is changed only by its creator or an admin (403 for any other editor)."""
    if pack.get("kind") != "skill" or pack.get("created_by_id") is None: return
    import admin
    if _is_admin() or (admin.cur() or {}).get("id") == pack["created_by_id"]: return
    raise HTTPException(403, "Only the creator or an admin can change this skill")
def _tree_hash(tree: dict) -> str: return _sha(json.dumps(sorted(tree.items()), ensure_ascii=False))
def _commit_hash(parent, tree_hash, author, message, at) -> str:
    return _sha(json.dumps({"parent": parent, "tree": tree_hash, "author": author, "message": message, "at": at.isoformat()}, sort_keys=True, ensure_ascii=False))

def _check_path(p):
    if (not isinstance(p, str) or not p or len(p) > 200 or p.startswith("/") or "\\" in p or "\x00" in p or p in (".", "..")
            or p != posixpath.normpath(p) or p.startswith("../") or ":" in p.split("/")[0]):
        raise HTTPException(400, f"Invalid file path: {str(p)[:80]}")

def _validate(files: dict, touched=None):
    """files = the full new snapshot {path: text}. touched = paths changed by this commit (skill frontmatter is checked for those only)."""
    if len(files) > MAX_FILES: raise HTTPException(400, f"Too many files ({len(files)} > {MAX_FILES})")
    tot = 0
    for p, v in files.items():
        _check_path(p)
        if not isinstance(v, str): raise HTTPException(400, f"File {p}: content must be text")
        if len(v) > MAX_FILE: raise HTTPException(400, f"File {p} is larger than {MAX_FILE // 1000} KB")
        tot += len(v)
    if tot > MAX_TOTAL: raise HTTPException(400, "Pack is larger than 5 MB")
    import ui_api
    for name in sorted({p.split("/")[1] for p in (touched if touched is not None else files) if p.startswith("skills/") and p.count("/") >= 2}):
        md = files.get(f"skills/{name}/SKILL.md")
        if md is None:
            if any(p.startswith(f"skills/{name}/") for p in files): raise HTTPException(400, f"skills/{name}/ needs a SKILL.md")
            continue
        ui_api.skill_check(name, {"SKILL.md": md})
    for p in (touched if touched is not None else files):
        if p.startswith("skills/") and p.count("/") < 2 and p in files: raise HTTPException(400, f"{p}: skill files must live in skills/<name>/")

# ------------------------------------------------------------------ storage
async def _pack(pid):
    r = await A().q("SELECT * FROM context_packs WHERE id=%s", (pid,), one=True)
    if not r: raise HTTPException(404, "Pack not found")
    return r

async def _head(pid):
    return await A().q("SELECT * FROM context_commits WHERE pack_id=%s ORDER BY seq DESC LIMIT 1", (pid,), one=True)

async def _commit_row(pid, h):
    return await A().q("SELECT * FROM context_commits WHERE pack_id=%s AND hash=%s", (pid, h), one=True)

async def resolve_commit(pid, ref):
    """ref = tag | commit hash prefix (>= 6 hex) | 'latest'. -> commit row (404 if none)."""
    ref = (ref or "latest").strip() or "latest"
    c = None
    if ref == "latest": c = await _head(pid)
    else:
        c = await A().q("SELECT c.* FROM context_tags t JOIN context_commits c ON c.pack_id=t.pack_id AND c.hash=t.hash WHERE t.pack_id=%s AND t.tag=%s", (pid, ref), one=True)
        if not c and HEX_RX.match(ref):
            rows = await A().q("SELECT * FROM context_commits WHERE pack_id=%s AND hash LIKE %s LIMIT 2", (pid, ref + "%"))
            if len(rows) > 1: raise HTTPException(400, f"Commit prefix '{ref}' is ambiguous")
            c = rows[0] if rows else None
    if not c: raise HTTPException(404, f"No tag or commit '{ref}' in this pack")
    return c

_SNAP: OrderedDict = OrderedDict()   # (pack_id, commit hash) -> {path: text}; commits are immutable so entries never go stale
async def files_of(pid, commit) -> dict:
    k = (pid, commit["hash"])
    if k in _SNAP: _SNAP.move_to_end(k); return dict(_SNAP[k])
    shas = list(set(commit["tree"].values()))
    got = {r["sha"]: r["content"] for r in await A().q("SELECT sha, content FROM context_blobs WHERE pack_id=%s AND sha = ANY(%s)", (pid, shas))} if shas else {}
    files = {p: got[s] for p, s in commit["tree"].items() if s in got}
    _SNAP[k] = files
    while len(_SNAP) > 64: _SNAP.popitem(last=False)
    return dict(files)

async def _write_commit(pack, files: dict, message, author, created_at=None):
    """Store `files` (full snapshot) as the next commit of the pack. Returns the commit row, or None when nothing changed."""
    q = A().q; pid, ws = pack["id"], pack["workspace_id"]
    head = await _head(pid)
    tree = {p: _sha(v) for p, v in files.items()}
    if head and head["tree"] == tree: return None
    at = created_at or datetime.now(timezone.utc)
    th = _tree_hash(tree); parent = head["hash"] if head else None
    h = _commit_hash(parent, th, author, message, at)
    shas = list(tree.values())
    await q("INSERT INTO context_blobs(workspace_id, pack_id, sha, content) SELECT %s, %s, s, c FROM unnest(%s::text[], %s::text[]) AS t(s, c) ON CONFLICT DO NOTHING",
            (ws, pid, [tree[p] for p in files], list(files.values())))
    try:
        await q("INSERT INTO context_commits(workspace_id, pack_id, hash, seq, parent, tree, tree_hash, author, message, created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (ws, pid, h, (head["seq"] + 1) if head else 1, parent, Jsonb(tree), th, author, message[:2000], at))
    except Exception as e:  # noqa: BLE001
        if "context_commits" in str(e) and "unique" in str(e).lower(): raise HTTPException(409, "The pack changed while you were committing; reload and retry")  # noqa: B904
        raise
    _SNAP[(pid, h)] = dict(files)
    return await _commit_row(pid, h)

async def _set_tag(pack, tag, h, by):
    pid = pack["id"]
    old = await A().q("SELECT hash FROM context_tags WHERE pack_id=%s AND tag=%s", (pid, tag), one=True)
    if old and old["hash"] == h: return False
    await A().q("INSERT INTO context_tags(workspace_id, pack_id, tag, hash, updated_by) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(pack_id, tag) DO UPDATE SET hash=EXCLUDED.hash, updated_at=now(), updated_by=EXCLUDED.updated_by",
                (pack["workspace_id"], pid, tag, h, by))
    await A().q("INSERT INTO context_tag_events(workspace_id, pack_id, tag, from_hash, to_hash, by) VALUES(%s,%s,%s,%s,%s,%s)", (pack["workspace_id"], pid, tag, old and old["hash"], h, by))
    return True

# ------------------------------------------------------------------ diff
def _rows(a: list, b: list, ctx=3, cap=2500):
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            rows += [{"t": "eq", "l": i1 + k + 1, "lt": a[i1 + k], "r": j1 + k + 1, "rt": b[j1 + k]} for k in range(i2 - i1)]
        else:
            for k in range(max(i2 - i1, j2 - j1)):
                L, R = (i1 + k < i2), (j1 + k < j2)
                rows.append({"t": "chg" if L and R else "del" if L else "add", "l": i1 + k + 1 if L else None, "lt": a[i1 + k] if L else "",
                             "r": j1 + k + 1 if R else None, "rt": b[j1 + k] if R else ""})
    keep = set()
    for i, r in enumerate(rows):
        if r["t"] != "eq": keep.update(range(max(0, i - ctx), min(len(rows), i + ctx + 1)))
    out, gap = [], 0
    for i, r in enumerate(rows):
        if i in keep:
            if gap: out.append({"t": "gap", "n": gap}); gap = 0
            out.append(r)
        else: gap += 1
    if gap: out.append({"t": "gap", "n": gap})
    return out[:cap], len(out) > cap

async def diff_commits(pid, ca, cb):
    ta, tb = (ca["tree"] if ca else {}), cb["tree"]
    fa = await files_of(pid, ca) if ca else {}
    fb = await files_of(pid, cb)
    out = []
    for p in sorted(set(ta) | set(tb)):
        if ta.get(p) == tb.get(p): continue
        a, b = fa.get(p, "").splitlines(), fb.get(p, "").splitlines()
        st = "added" if p not in ta else "removed" if p not in tb else "modified"
        rows, trunc = _rows(a, b)
        out.append({"path": p, "status": st, "added": sum(1 for r in rows if r["t"] in ("add", "chg")), "removed": sum(1 for r in rows if r["t"] in ("del", "chg")),
                    "rows": rows, "truncated": trunc, "unified": "\n".join(difflib.unified_diff(a, b, f"a/{p}", f"b/{p}", lineterm=""))[:200_000]})
    ref = lambda c: {"hash": c["hash"], "short": _short(c["hash"]), "message": c["message"]} if c else None
    return {"a": ref(ca), "b": ref(cb), "files": out, "summary": {s: sum(1 for f in out if f["status"] == s) for s in ("added", "removed", "modified")}}

# ------------------------------------------------------------------ pack API
class PackIn(BaseModel):
    name: str = Field(pattern=NAME_RX.pattern)
    description: str | None = Field(default=None, max_length=500)
    files: dict[str, str] | None = None
    message: str = Field(default="Initial commit", max_length=2000)
    tag: str | None = Field(default=None, pattern=TAG_RX.pattern)

class CommitIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    changes: dict[str, str | None]       # path -> new text, null = delete the file
    replace: bool = False                # true: `changes` is the whole new snapshot (null entries ignored)
    parent: str | None = None            # optimistic lock: 409 when this is not the current head (hash prefix)
    tag: str | None = Field(default=None, pattern=TAG_RX.pattern)

class TagIn(BaseModel):
    tag: str = Field(pattern=TAG_RX.pattern)
    commit: str = "latest"               # tag | hash prefix | latest

class RestoreIn(BaseModel):
    commit: str
    message: str | None = Field(default=None, max_length=2000)
    tag: str | None = Field(default=None, pattern=TAG_RX.pattern)

def _default_files(name, description):
    return {"AGENTS.md": f"# {name}\n\n{description or 'Instructions for the agent.'}\n"}

def _tag_ok(tag):
    if tag == "latest" or HEX_RX.match(tag): raise HTTPException(400, "Tag name must not be 'latest' or look like a commit hash")

async def _create_pack(name, description, files, message, author, ws):
    if await A().q("SELECT 1 FROM context_packs WHERE workspace_id=%s AND name=%s", (ws, name), one=True): raise HTTPException(409, f"A pack named '{name}' already exists")
    pack = await A().q("INSERT INTO context_packs(workspace_id, name, description, created_by) VALUES(%s,%s,%s,%s) RETURNING *", (ws, name, description, author), one=True)
    c = await _write_commit(pack, files, message, author)
    return pack, c

@router.get("/api/packs")
async def packs_list(legacy: int | None = None):
    import admin
    me = (admin.cur() or {}).get("id")
    rows = await A().q("""SELECT p.id, p.name, p.description, p.legacy_context_id, p.created_at, p.kind, p.visibility, p.owner_agent_id, p.source, p.created_by_id, p.lint, h.hash head, h.created_at updated, h.message head_message,
        (SELECT count(*) FROM jsonb_object_keys(h.tree)) n_files, (SELECT count(*) FROM context_commits c WHERE c.pack_id=p.id) commits,
        (SELECT coalesce(json_agg(json_build_object('tag', t.tag, 'hash', t.hash) ORDER BY t.tag), '[]'::json) FROM context_tags t WHERE t.pack_id=p.id) tags,
        (SELECT coalesce(json_agg(DISTINCT split_part(k, '/', 2)), '[]'::json) FROM jsonb_object_keys(h.tree) k WHERE k LIKE 'skills/%%/%%') skills,
        (h.tree ? 'AGENTS.md') has_agents_md
        FROM context_packs p LEFT JOIN LATERAL (SELECT * FROM context_commits c WHERE c.pack_id=p.id ORDER BY seq DESC LIMIT 1) h ON true
        WHERE (%s::int IS NULL OR p.legacy_context_id=%s) AND NOT p.archived
          AND (p.visibility <> 'private' OR %s OR p.created_by_id IS NOT DISTINCT FROM %s) ORDER BY h.created_at DESC NULLS LAST, p.id DESC""", (legacy, legacy, _is_admin(), me))
    return rows

@router.post("/api/packs")
async def pack_new(b: PackIn):
    _no_demo()
    files = b.files if b.files is not None else _default_files(b.name, b.description)
    _validate(files)
    import admin
    pack, c = await _create_pack(b.name, b.description, files, b.message, _me(), admin.cur_ws())
    if b.tag and c: _tag_ok(b.tag); await _set_tag(pack, b.tag, c["hash"], _me())
    return {"id": pack["id"], "name": pack["name"], "hash": c and c["hash"]}

async def _detail(pid):
    pack = await _pack(pid)
    q = A().q
    commits = await q("SELECT hash, seq, parent, author, message, created_at, (SELECT count(*) FROM jsonb_object_keys(tree)) n_files FROM context_commits WHERE pack_id=%s ORDER BY seq DESC", (pid,))
    tags = await q("SELECT tag, hash, updated_at, updated_by FROM context_tags WHERE pack_id=%s ORDER BY tag", (pid,))
    by = {}
    for t in tags: by.setdefault(t["hash"], []).append(t["tag"])
    for c in commits: c["short"] = _short(c["hash"]); c["tags"] = by.get(c["hash"], [])
    events = await q("SELECT tag, from_hash, to_hash, at, by FROM context_tag_events WHERE pack_id=%s ORDER BY id DESC LIMIT 30", (pid,))
    used = await q("""SELECT a.id, a.name, v.config->>'context_pack' ref FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version
        WHERE NOT coalesce(a.deleted, false) AND split_part(v.config->>'context_pack', '@', 1) = %s""", (pack["name"],))
    return {"pack": pack, "head": commits[0]["hash"] if commits else None, "commits": commits, "tags": tags, "events": events, "used_by": used}

@router.get("/api/packs/{pid}")
async def pack_get(pid: int): return await _detail(pid)

@router.put("/api/packs/{pid}")
async def pack_put(pid: int, b: dict):
    _no_demo(); _guard(await _pack(pid))
    await A().q("UPDATE context_packs SET description=%s WHERE id=%s", (str(b.get("description") or "")[:500] or None, pid))
    return {"ok": True}

@router.delete("/api/packs/{pid}")
async def pack_del(pid: int):
    _no_demo(); d = await _detail(pid); _guard(d["pack"])
    if d["used_by"]: raise HTTPException(409, "Agents still pin this pack: " + ", ".join(a["name"] for a in d["used_by"][:5]))
    await A().q("DELETE FROM context_packs WHERE id=%s", (pid,)); return {"ok": True}

@router.get("/api/packs/{pid}/files")
async def pack_files(pid: int, ref: str = "latest"):
    await _pack(pid); c = await resolve_commit(pid, ref)
    return {"commit": c["hash"], "short": _short(c["hash"]), "message": c["message"], "files": await files_of(pid, c)}

@router.get("/api/packs/{pid}/commits")
async def pack_commits(pid: int): return (await _detail(pid))["commits"]

@router.post("/api/packs/{pid}/commits")
async def pack_commit(pid: int, b: CommitIn):
    _no_demo(); pack = await _pack(pid); _guard(pack); head = await _head(pid)
    if b.parent and not (head and head["hash"].startswith(b.parent)): raise HTTPException(409, "The pack changed since you opened it; reload and retry")
    if b.replace: files = {p: v for p, v in b.changes.items() if v is not None}
    else:
        files = await files_of(pid, head) if head else {}
        for p, v in b.changes.items():
            _check_path(p)
            if v is None: files.pop(p, None)
            else: files[p] = v
    _validate(files, None if b.replace else list(b.changes))
    c = await _write_commit(pack, files, b.message, _me())
    if not c: raise HTTPException(400, "No changes to commit")
    if b.tag: _tag_ok(b.tag); await _set_tag(pack, b.tag, c["hash"], _me())
    return {"hash": c["hash"], "short": _short(c["hash"]), "seq": c["seq"], "n_files": len(files)}

@router.get("/api/packs/{pid}/diff")
async def pack_diff(pid: int, b: str = "latest", a: str | None = None):
    await _pack(pid); cb = await resolve_commit(pid, b)
    if a: ca = await resolve_commit(pid, a)
    else: ca = await _commit_row(pid, cb["parent"]) if cb["parent"] else None
    return await diff_commits(pid, ca, cb)

@router.post("/api/packs/{pid}/tags")
async def pack_tag(pid: int, t: TagIn):
    _no_demo(); pack = await _pack(pid); _guard(pack); _tag_ok(t.tag)
    c = await resolve_commit(pid, t.commit)
    moved = await _set_tag(pack, t.tag, c["hash"], _me())
    return {"ok": True, "tag": t.tag, "hash": c["hash"], "moved": moved}

@router.delete("/api/packs/{pid}/tags/{tag}")
async def pack_untag(pid: int, tag: str):
    _no_demo(); pack = await _pack(pid); _guard(pack)
    old = await A().q("DELETE FROM context_tags WHERE pack_id=%s AND tag=%s RETURNING hash", (pid, tag), one=True)
    if not old: raise HTTPException(404, "No such tag")
    await A().q("INSERT INTO context_tag_events(workspace_id, pack_id, tag, from_hash, to_hash, by) VALUES(%s,%s,%s,%s,NULL,%s)", (pack["workspace_id"], pid, tag, old["hash"], _me()))
    return {"ok": True}

@router.post("/api/packs/{pid}/restore")
async def pack_restore(pid: int, b: RestoreIn):
    """Rollback as a NEW commit (history stays linear and auditable): head content := content of `commit`. To only switch what an agent reads, move a tag instead."""
    _no_demo(); pack = await _pack(pid); _guard(pack); old = await resolve_commit(pid, b.commit)
    c = await _write_commit(pack, await files_of(pid, old), b.message or f"Restore {_short(old['hash'])}", _me())
    if not c: raise HTTPException(400, "The pack already has this content")
    if b.tag: _tag_ok(b.tag); await _set_tag(pack, b.tag, c["hash"], _me())
    return {"hash": c["hash"], "short": _short(c["hash"])}

# ---- zip import / export (guards = skill_import.read_skill_zip, GĐ8.8)
@router.get("/api/packs/{pid}/export")
async def pack_export(pid: int, ref: str = "latest"):
    pack = await _pack(pid); c = await resolve_commit(pid, ref); files = await files_of(pid, c)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p, v in sorted(files.items()): z.writestr(p, v)
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{pack["name"]}-{_short(c["hash"])}.zip"'})

@router.post("/api/packs/import")
async def pack_import(file: UploadFile = File(...), name: str = Form(...), message: str = Form("Import zip"), tag: str = Form("")):
    import admin, skill_import
    _no_demo()
    if not NAME_RX.match(name): raise HTTPException(400, "Pack name: lowercase letters, digits, . _ - (max 63)")
    data = await file.read(skill_import.MAX_ZIP + 1)
    files, skipped = skill_import.read_skill_zip(data, require_skill=False)
    tops = {p.split("/", 1)[0] for p in files}
    if len(tops) == 1 and all("/" in p for p in files) and next(iter(tops)) not in ("skills", "prompts", "policies"):   # zip of one top folder -> strip it
        files = {p.split("/", 1)[1]: v for p, v in files.items()}
    if not files: raise HTTPException(400, "The zip has no text files")
    _validate(files)
    ws = admin.cur_ws()
    pack = await A().q("SELECT * FROM context_packs WHERE workspace_id=%s AND name=%s", (ws, name), one=True)
    if pack: _guard(pack); c = await _write_commit(pack, files, message[:2000] or "Import zip", _me()); created = False
    else: pack, c = await _create_pack(name, None, files, message[:2000] or "Import zip", _me(), ws); created = True
    if not c: raise HTTPException(400, "The zip has the same content as the current head")
    if tag: _tag_ok(tag); await _set_tag(pack, tag, c["hash"], _me())
    return {"id": pack["id"], "name": name, "hash": c["hash"], "files": sorted(files), "skipped": skipped, "created": created}

# ------------------------------------------------------------------ agents: mount the pack read-only
from deepagents.backends.context_hub import ContextHubBackend
from deepagents.backends.protocol import DeleteResult, EditResult, FileUploadResponse, WriteResult

class _NoClient:
    def __getattr__(self, n): raise RuntimeError("Context packs are local: there is no LangSmith client")

class PackBackend(ContextHubBackend):
    """deepagents ContextHubBackend whose source is an immutable commit snapshot of our Postgres Context Hub. All read paths (read / ls / grep /
    glob / download_files) are the stock deepagents code; every mutation is refused (the pack is read-only for the agent)."""
    def __init__(self, label: str, files: dict, commit: str):
        super().__init__(label, client=_NoClient())
        self._cache = {p.lstrip("/"): v for p, v in files.items()}
        self._commit_hash = commit

    @property
    def commit(self): return self._commit_hash
    def write(self, file_path, content): return WriteResult(error=RO)
    def edit(self, file_path, old_string, new_string, replace_all=False): return EditResult(error=RO)   # noqa: FBT002
    def delete(self, file_path): return DeleteResult(error=RO)
    def upload_files(self, files): return [FileUploadResponse(path=p, error=RO) for p, _ in files]

from deepagents.middleware.memory import MemoryMiddleware

class FreshMemoryMiddleware(MemoryMiddleware):
    """deepagents MemoryMiddleware loads its sources once per thread (state.memory_contents). Pack AGENTS.md can change between two turns of the same
    thread (tag moved), so this variant reloads on every turn. The sources are local snapshots: no I/O cost."""
    def before_agent(self, state, runtime, config):
        return super().before_agent({k: v for k, v in state.items() if k != "memory_contents"}, runtime, config)
    async def abefore_agent(self, state, runtime, config):
        return await super().abefore_agent({k: v for k, v in state.items() if k != "memory_contents"}, runtime, config)

def split_ref(ref: str):
    name, _, r = (ref or "").strip().partition("@")
    return name.strip(), (r.strip() or "latest")

async def _resolve_ws(ws, ref):
    """-> (pack row, commit row) for `name@ref` inside workspace `ws` (explicit, like deepagent_api.resolve_context)."""
    name, r = split_ref(ref)
    pack = await A().q("SELECT * FROM context_packs WHERE workspace_id=%s AND name=%s", (ws, name), one=True)
    if not pack: return None, None
    try: return pack, await resolve_commit(pack["id"], r)
    except HTTPException: return pack, None

async def validate_ref(ws, ref) -> str:
    """Agent save time: the pack and the tag/commit must exist. Returns the normalised 'name@ref'."""
    name, r = split_ref(ref)
    pack, c = await _resolve_ws(ws, ref)
    if not pack: raise HTTPException(400, f"Context pack '{name}' does not exist")
    if not c: raise HTTPException(400, f"Context pack '{name}' has no tag or commit '{r}'")
    return f"{name}@{r}"

class Mount:
    def __init__(self, backend, pack, commit, ref, ws, files):
        self.backend, self.ref, self.ws = backend, ref, ws
        self.stamp = (ws, ref, commit["hash"])
        self.has_skills = any(p.startswith("skills/") and p.endswith("/SKILL.md") for p in files)
        self.has_agents = "AGENTS.md" in files
        self.others = sorted(p for p in files if p != "AGENTS.md" and not p.startswith("skills/"))
        self.info = {"ref": ref, "pack": pack["name"], "pack_id": pack["id"], "commit": commit["hash"], "short": _short(commit["hash"]), "message": commit["message"], "n_files": len(files)}

async def mount(ref, ws):
    """ref = cfg.context_pack. -> Mount | None (None = not configured; a configured but missing ref raises so the run does not silently lose its instructions)."""
    if not ref: return None
    pack, c = await _resolve_ws(ws, ref)
    if not pack or not c: raise HTTPException(400, f"Context pack '{ref}' not found (pack or tag removed?)")
    files = await files_of(pack["id"], c)
    return Mount(PackBackend(f"{pack['name']}@{_short(c['hash'])}", files, c["hash"]), pack, c, ref, ws, files)

async def is_current(stamp) -> bool:
    """Cheap check done on every run of a cached agent: does the pinned ref still resolve to the commit the agent was built with?"""
    ws, ref, h = stamp
    try: _, c = await _resolve_ws(ws, ref)
    except Exception: return False  # noqa: BLE001
    return bool(c and c["hash"] == h)

def prompt_note(m: Mount) -> str:
    extra = ", ".join(m.others[:30]) + (" ..." if len(m.others) > 30 else "")
    return (f"\n\nContext pack `{m.info['pack']}` (commit {m.info['short']}) is mounted read-only at /pack/." + (f" Extra reference files: {extra}. Read them with read_file when relevant." if m.others else ""))

@router.get("/api/agents/{aid}/context")
async def agent_context(aid: int):
    """What this agent reads from its pinned context pack right now (and which commit its cached build holds)."""
    import deepagent_api as D
    r = await D.q("SELECT a.workspace_id, v.config FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version WHERE a.id=%s", (aid,), one=True)
    if not r: raise HTTPException(404, "Agent not found")
    ref = (r["config"] or {}).get("context_pack")
    if not ref: return {"ref": None}
    pack, c = await _resolve_ws(r["workspace_id"], ref)
    built = sorted({D.S["pack_stamp"][id(v[0])][2] for k, v in D.S["agents"].items() if k[0] == aid and id(v[0]) in D.S.get("pack_stamp", {})})
    tags = [t["tag"] for t in await D.q("SELECT tag FROM context_tags WHERE pack_id=%s AND hash=%s", (pack["id"], c["hash"]))] if pack and c else []
    return {"ref": ref, "pack": pack and {"id": pack["id"], "name": pack["name"]}, "commit": c and c["hash"], "short": c and _short(c["hash"]), "tags": tags,
            "message": c and c["message"], "files": sorted(c["tree"]) if c else [], "built_commits": built}

# ------------------------------------------------------------------ legacy `contexts` -> packs
def _slug(s: str) -> str:
    s = unicodedata.normalize("NFD", (s or "").replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-._")[:63].strip("-._")
    return s if NAME_RX.match(s) else "pack"

def legacy_tree(kind, name, files: dict) -> dict:
    """skill context (files relative to the skill root) -> skills/<name>/<file>; agent context (AGENTS.md ...) -> files as they are."""
    return {f"skills/{name}/{k.lstrip('/')}": v for k, v in (files or {}).items()} if kind == "skill" else {k.lstrip("/"): v for k, v in (files or {}).items()}

async def _free_name(ws, name, kind):
    base = _slug(name)
    for cand in (base, f"{base[:55]}-{kind}", f"{base[:50]}-{kind}-{abs(hash(name)) % 1000}"):
        if not await A().q("SELECT 1 FROM context_packs WHERE workspace_id=%s AND name=%s", (ws, cand), one=True): return cand
    return f"{base[:40]}-{kind}-{__import__('uuid').uuid4().hex[:6]}"

async def _import_legacy(c):
    """One pack per legacy context: one commit per published version (history kept), tag prod = latest published, tag draft = unpublished edits."""
    q = A().q; ws = c["workspace_id"] or 1
    name = await _free_name(ws, c["name"], c["kind"])
    pack = await q("INSERT INTO context_packs(workspace_id, name, description, legacy_context_id, created_by, kind) VALUES(%s,%s,%s,%s,'migration',%s) RETURNING *",
                   (ws, name, ((c["description"] or "") + (f" (was: {c['name']})" if name != c["name"] else "")).strip()[:500] or None, c["id"], "skill" if c["kind"] == "skill" else "agent"), one=True)
    last = None
    for v in await q("SELECT version, files, published FROM context_versions WHERE context_id=%s ORDER BY version", (c["id"],)):
        last = await _write_commit(pack, legacy_tree(c["kind"], c["name"], v["files"]), f"Legacy publish v{v['version']}", "migration", v["published"]) or last
    if last: await _set_tag(pack, "prod", last["hash"], "migration")
    cur = await _write_commit(pack, legacy_tree(c["kind"], c["name"], c["files"]), f"Legacy {c['status']} v{c['version']}", "migration", c["updated"])
    if cur: await _set_tag(pack, "prod" if c["status"] == "active" else "draft", cur["hash"], "migration")   # unpublished edits stay out of prod
    return pack

async def migrate():
    """Boot (idempotent, after ui_api.init created `contexts`): every legacy context without a pack gets one."""
    for c in await A().q("SELECT * FROM contexts c WHERE NOT EXISTS (SELECT 1 FROM context_packs p WHERE p.legacy_context_id=c.id) ORDER BY id"):
        try: await _import_legacy(c)
        except Exception as e: print("context pack migration failed for context", c["id"], str(e)[:200])  # noqa: BLE001

async def mirror_legacy(cid):
    """Called after every legacy write (ui_api._snapshot): keep the pack in step. One-way legacy -> pack; never raises into the legacy API."""
    try:
        q = A().q
        c = await q("SELECT * FROM contexts WHERE id=%s", (cid,), one=True)
        if not c: return
        pack = await q("SELECT * FROM context_packs WHERE legacy_context_id=%s", (cid,), one=True)
        if not pack: await _import_legacy(c); return
        by = "legacy-api"
        cm = await _write_commit(pack, legacy_tree(c["kind"], c["name"], c["files"]), f"Legacy {c['status']} v{c['version']}", by)
        h = (cm or await _head(pack["id"]))["hash"]
        if c["status"] == "active": await _set_tag(pack, "prod", h, by)
    except Exception as e:  # noqa: BLE001
        print("context pack mirror failed for context", cid, str(e)[:200])

async def mirror_delete(cid):
    """Legacy context deleted -> its pack goes too, unless an agent still pins it (then the pack stays, so that agent keeps its instructions)."""
    try:
        p = await A().q("SELECT id, name FROM context_packs WHERE legacy_context_id=%s", (cid,), one=True)
        if not p: return
        used = await A().q("SELECT 1 FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version WHERE NOT coalesce(a.deleted, false) AND split_part(v.config->>'context_pack', '@', 1)=%s", (p["name"],), one=True)
        if not used: await A().q("DELETE FROM context_packs WHERE id=%s", (p["id"],))
    except Exception as e:  # noqa: BLE001
        print("context pack delete mirror failed for context", cid, str(e)[:200])

# ------------------------------------------------------------------ pack -> legacy skill/memory pins (GĐ9.P)
async def legacy_pack_files(ws, kind, name, ref="prod"):
    """Hub side of a legacy pin: the pack mirrored from legacy context (kind, name) at its tag/commit `ref` (default prod = latest published, or whatever the Context Hub
    UI tagged since). -> (files in the legacy shape {relative path: text}, commit hash) or None (no pack / no such tag / skill folder removed: caller falls back to the legacy row)."""
    pack = await A().q("SELECT p.id FROM context_packs p JOIN contexts c ON c.id=p.legacy_context_id WHERE c.kind=%s AND c.name=%s AND c.workspace_id IS NOT DISTINCT FROM %s",
                       (kind, name, ws), one=True)
    if not pack: return None
    try: c = await resolve_commit(pack["id"], ref)
    except HTTPException: return None
    files = await files_of(pack["id"], c)
    if kind == "skill":
        pre = f"skills/{name}/"
        files = {k[len(pre):]: v for k, v in files.items() if k.startswith(pre)}
        if "SKILL.md" not in files: return None
    elif "AGENTS.md" not in files: return None
    return files, c["hash"]

async def skill_resolve(ws, name, ref="prod", agent_id=None):
    """GĐ12.86: a Skill stored as a pack (`kind='skill'`, not mirrored from a legacy context). ref = tag (default `prod` = always in sync with the latest published edit) or commit
    prefix (pinned). A PRIVATE skill resolves only for its `owner_agent_id` agent. -> {"name","version"=ref,"files"=relative paths,"_pack"=(ws,'skill',name,ref,commit,agent_id)} or None.
    Archived skills still resolve (agents that already use one keep working); they are only hidden from lists."""
    ref = (ref or "prod").strip() or "prod"
    pack = await A().q("SELECT id, visibility, owner_agent_id FROM context_packs WHERE workspace_id=%s AND name=%s AND kind='skill' AND legacy_context_id IS NULL", (ws, name), one=True)
    if not pack: return None
    if pack["visibility"] == "private" and (agent_id is None or pack["owner_agent_id"] != int(agent_id)): return None
    try: c = await resolve_commit(pack["id"], ref)
    except HTTPException: return None
    pre = f"skills/{name}/"
    files = {k[len(pre):]: v for k, v in (await files_of(pack["id"], c)).items() if k.startswith(pre)}
    if "SKILL.md" not in files: return None
    return {"name": name, "version": ref, "files": files, "_pack": (ws, "skill", name, ref, c["hash"], agent_id)}

async def legacy_is_current(stamps) -> bool:
    """stamps = [(ws, kind, name, ref, commit hash)] recorded at build time. False as soon as any pinned tag moved (the cached agent must be rebuilt)."""
    for st in stamps:
        ws, kind, name, ref, h = st[:5]
        try:
            r = await legacy_pack_files(ws, kind, name, ref)
            cur = r[1] if r else None
            if cur is None and kind == "skill":        # GĐ12.86: a skill pack (no legacy context): the stamp may carry the agent id (6th item) for private skills
                g = await skill_resolve(ws, name, ref, st[5] if len(st) > 5 else None)
                cur = g["_pack"][4] if g else None
        except Exception: return False  # noqa: BLE001
        if cur != h: return False
    return True

async def init():
    """Before admin.rls_init: tables (owner role)."""
    for st in [x for x in DDL.split(";\n") if x.strip()]: await A().q(st)
