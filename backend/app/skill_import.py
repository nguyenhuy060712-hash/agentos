"""GĐ8.8 (B21) Import a whole skill folder as a zip -> Context Hub skill (kind=skill, active, visibility public = shared in the workspace).
Agents then use it through the existing skills config ("<name>" -> pinned "<name>@N", materialized by deepagent_api._materialize).
Zip rules: SKILL.md required (at the root or inside one top folder), <= 5 MB unzipped, <= 100 entries, no absolute/".." paths or symlinks,
compression-ratio + real bounded inflate check (same idea as docfiles._zip_check). Text files (UTF-8) are kept; binary files are skipped
(the Context Hub stores text). Name/description from the SKILL.md frontmatter (ui_api.skill_check = agentskills.io rules)."""
import io, posixpath, stat, zipfile
from fastapi import APIRouter, File, HTTPException, UploadFile
from psycopg.types.json import Jsonb

router = APIRouter()
MAX_ZIP = 5 * 1024 * 1024          # upload (compressed) and unzipped total
MAX_TOTAL = 5 * 1024 * 1024
MAX_ENTRIES = 100
MAX_RATIO = 100
SKIP = ("__MACOSX/", ".DS_Store", "Thumbs.db")

def _bad(msg): raise HTTPException(400, msg)

def read_skill_zip(data: bytes, require_skill: bool = True) -> tuple[dict, list]:
    """-> (files {relative path: text}, skipped [paths]). Raises HTTPException(400) on any rule violation.
    require_skill=False (GĐ9.1 context packs): same guards, but no SKILL.md rule and no root-folder stripping (paths kept as in the zip)."""
    if len(data) > MAX_ZIP: _bad(f"Zip quá lớn (> {MAX_ZIP >> 20} MB) / zip too large")
    try: z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile: _bad("Không phải tệp zip hợp lệ / not a valid zip file")
    infos = z.infolist()
    if len(infos) > MAX_ENTRIES: _bad(f"Zip có quá nhiều mục ({len(infos)} > {MAX_ENTRIES}) / too many entries")
    total = sum(i.file_size for i in infos); comp = sum(i.compress_size for i in infos) or 1
    if total > MAX_TOTAL: _bad(f"Giải nén quá lớn (> {MAX_TOTAL >> 20} MB) / unzipped size too large")
    if total / comp > MAX_RATIO and total > (1 << 20): _bad("Tỉ lệ nén bất thường (nghi zip bomb) / suspicious compression ratio")
    for i in infos:
        n = i.filename
        if n.startswith(("/", "\\")) or "\\" in n or ":" in n.split("/")[0] or ".." in n.split("/"):
            _bad(f"Đường dẫn không hợp lệ trong zip / unsafe path: {n[:120]}")
        if stat.S_ISLNK(i.external_attr >> 16): _bad(f"Zip chứa symlink / symlinks are not allowed: {n[:120]}")
    names = [i.filename for i in infos if not i.is_dir() and not any(s in i.filename for s in SKIP)]
    root = ""
    if require_skill and "SKILL.md" not in names:
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) == 1 and f"{next(iter(tops))}/SKILL.md" in names: root = next(iter(tops)) + "/"
        else: _bad("Thiếu SKILL.md ở thư mục gốc của skill / SKILL.md is required at the skill root")
    files, skipped, got = {}, [], 0
    for i in infos:
        if i.is_dir() or i.filename not in names: continue
        rel = posixpath.normpath(i.filename[len(root):]) if i.filename.startswith(root) else None
        if not rel or rel.startswith(("../", "/")) or rel == "..": skipped.append(i.filename); continue
        buf = bytearray()
        with z.open(i) as f:   # headers can lie: inflate for real, bounded
            while chunk := f.read(1 << 16):
                got += len(chunk); buf += chunk
                if got > MAX_TOTAL: _bad("Giải nén quá lớn (nghi zip bomb) / zip bomb suspected")
        try: txt = bytes(buf).decode("utf-8-sig")
        except UnicodeDecodeError: skipped.append(rel); continue
        if "\x00" in txt: skipped.append(rel); continue
        files[rel] = txt
    return files, skipped

@router.post("/api/skills/import")
async def skill_import(file: UploadFile = File(...)):
    import deepagent_api as A, admin, ui_api, gateway as gw
    if admin.is_demo() or admin.cur_ws() in gw.DEMO_WS: raise HTTPException(403, "Tắt ở workspace demo / off in the demo workspace")
    data = await file.read(MAX_ZIP + 1)
    files, skipped = read_skill_zip(data)
    fm = ui_api.skill_check(_name(files), files)
    ui_api._files_ok(files)
    name, desc = fm["name"], fm["description"].strip()[:500]
    old = await A.q("SELECT id FROM contexts WHERE kind='skill' AND name=%s AND workspace_id IS NOT DISTINCT FROM aos_ws()", (name,), one=True)
    if old:
        r = await A.q("UPDATE contexts SET description=%s, files=%s, status='active', visibility='public', version=version+1, updated=now() WHERE id=%s RETURNING id, version",
                      (desc, Jsonb(files), old["id"]), one=True)
    else:
        r = await A.q("INSERT INTO contexts(workspace_id,kind,name,description,visibility,status,files) VALUES(aos_ws(),'skill',%s,%s,'public','active',%s) RETURNING id, version",
                      (name, desc, Jsonb(files)), one=True)
    await ui_api._snapshot(r["id"])
    return {"id": r["id"], "name": name, "version": r["version"], "files": sorted(files), "skipped": skipped, "updated": bool(old)}

def _name(files: dict) -> str:
    """Frontmatter name (skill_check then verifies it and that it matches)."""
    import re, yaml
    m = re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", files.get("SKILL.md") or "", re.S)
    try: n = (yaml.safe_load(m.group(1)) or {}).get("name") if m else None
    except Exception: n = None  # noqa: BLE001
    return n if isinstance(n, str) else ""
