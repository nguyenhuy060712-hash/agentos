"""GĐ11.64 skills inside the virtual computer: ONE tree per agent, seen two ways at the same path `/memories/skills/`.
  * file tools (`read_file`, `ls`, `glob`, `grep`): a read-only route of the CompositeBackend rooted at the tree's hash folder (`RoHostBackend`)
  * the shell (`execute python /memories/skills/xlsx/scripts/xlsx_tool.py ...`): the agent folder `<SBX_ROOT.parent>/skills/agent-<id>/` is a READ-ONLY bind mount of
    the box (set at create, sbx_backend.ensure_box). Inside it: `.h/<hash>/<skill>/...` (content addressed, never changed) and one RELATIVE symlink per skill
    (`xlsx -> .h/<hash>/xlsx`). New content = new hash folder + atomic symlink swap; a running gVisor box sees it at once (11.40a Q5: < 0.7 s), so no upload is needed.
Limits until the `context_hub.*` limits table exists: 500 files / 2 MB per file / 20 MB per package, binaries allowed. The skills are written by the server only: the box
cannot write the mount, and the file route / `permissions` refuse writes."""
import hashlib, os, re, shutil, threading
from pathlib import Path

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import DeleteResult, EditResult, FileUploadResponse, WriteResult

MOUNT = "/memories/skills"
MAX_FILES, MAX_FILE_BYTES, MAX_PKG_BYTES, MAX_SKILL_MD = 500, 2 << 20, 20 << 20, 10 << 20
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
APP_SKILLS = Path(__file__).parent / "skills"
KEEP_OLD = 1                       # previous hash folders kept (a box that is still reading a script of the old version)
_LOCK = threading.Lock()

class SkillError(ValueError): pass

def root() -> Path:
    import admin
    return admin.SBX_ROOT.parent / "skills"

def agent_dir(agent_id: int, base: Path | None = None) -> Path: return (base or root()) / f"agent-{int(agent_id)}"

# ------------------------------------------------------------------ validation (own minimal check; `skills-ref` is not pinned in the repo)
def frontmatter(md: str) -> dict:
    import yaml
    m = re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", md, re.S)
    if not m: raise SkillError("SKILL.md must start with YAML frontmatter between --- lines")
    try: fm = yaml.safe_load(m.group(1)) or {}
    except Exception as e: raise SkillError(f"invalid YAML frontmatter: {str(e)[:120]}") from None   # noqa: BLE001
    if not isinstance(fm, dict): raise SkillError("frontmatter must be a mapping")
    return fm

def validate_skill(name: str, files: dict) -> dict:
    """files: {relative path in the skill: bytes|str}. name 1-64 (lowercase, digits, hyphens) = folder, description <= 1024, SKILL.md <= 10 MB, `metadata.include_tools`
    a space-separated string (a list never matches a tool in deepagents 0.7.32). Returns the frontmatter."""
    md = files.get("SKILL.md")
    if md is None: raise SkillError(f"skill {name}: SKILL.md is missing")
    if isinstance(md, str): md = md.encode()
    if len(md) > MAX_SKILL_MD: raise SkillError(f"skill {name}: SKILL.md is bigger than 10 MB")
    fm = frontmatter(md.decode("utf-8", "replace"))
    n, d = fm.get("name"), fm.get("description")
    if not isinstance(n, str) or not (1 <= len(n) <= 64) or not NAME_RE.match(n): raise SkillError(f"skill {name}: name must be 1-64 chars, lowercase letters/digits/hyphens")
    if n != name: raise SkillError(f"skill {name}: frontmatter name '{n}' must equal the folder name")
    if not isinstance(d, str) or not (1 <= len(d.strip()) <= 1024): raise SkillError(f"skill {name}: description is required, at most 1024 chars")
    meta = fm.get("metadata")
    inc = meta.get("include_tools") if isinstance(meta, dict) else None
    if inc is not None and (not isinstance(inc, str) or "," in inc): raise SkillError(f"skill {name}: metadata.include_tools must be a space-separated string of tool names")
    return fm

def _bytes(v) -> bytes: return v if isinstance(v, bytes) else str(v).encode()

def check_tree(tree: dict) -> dict:
    """tree: {"<skill>/<path>": bytes|str} -> same with bytes values. Enforces the limits and validates every skill (folders without SKILL.md, like `_common`, are helpers)."""
    out, total = {}, 0
    if len(tree) > MAX_FILES: raise SkillError(f"too many files in the skills package ({len(tree)} > {MAX_FILES})")
    for k, v in tree.items():
        parts = k.split("/")
        if k.startswith("/") or len(parts) < 2 or any(p in ("", ".", "..") or "\x00" in p or "\\" in p for p in parts): raise SkillError(f"bad skill file path: {k[:80]}")
        b = _bytes(v)
        if len(b) > MAX_FILE_BYTES: raise SkillError(f"{k[:80]} is bigger than 2 MB")
        total += len(b); out[k] = b
    if total > MAX_PKG_BYTES: raise SkillError("the skills package is bigger than 20 MB")
    for name in {k.split("/", 1)[0] for k in out}:
        if any(k == f"{name}/SKILL.md" for k in out): validate_skill(name, {k.split("/", 1)[1]: v for k, v in out.items() if k.startswith(name + "/")})
    return out

# ------------------------------------------------------------------ building the tree
def builtin_tree(names) -> dict:
    """Built-in skills (app/skills/<name>/**, incl. scripts/) + the `_common` helpers they import (a folder without SKILL.md: not offered as a skill)."""
    out = {}
    for n in dict.fromkeys(names):
        d = APP_SKILLS / n
        if not NAME_RE.match(n) or not d.is_dir(): continue
        for f in sorted(d.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc": out[f"{n}/{f.relative_to(d).as_posix()}"] = f.read_bytes()
    if any(k.split("/", 1)[0] in ("docx", "xlsx", "pptx", "pdf") for k in out):
        for f in sorted((APP_SKILLS / "_common").rglob("*.py")): out[f"_common/{f.relative_to(APP_SKILLS / '_common').as_posix()}"] = f.read_bytes()
    return out

async def tree_for(ws, skills, agent_id=None) -> dict:
    """The agent's `skills` refs ('builtin:<name>' | context name | name@N | name@tag) -> {"<skill>/<path>": bytes}."""
    import deepagent_api as A
    out = {}
    plain = []
    for ref in skills:
        if ref.startswith("builtin:"): plain.append(ref[8:])
        else:
            r = await A.resolve_context(ws, "skill", ref, agent_id)
            if r:
                for k, v in (r["files"] or {}).items(): out[f"{r['name']}/{k.lstrip('/')}"] = v
    bt = builtin_tree([n for n in plain if n != "agentos"])
    if "agentos" in plain: bt.update({k: v.encode() for k, v in A.builtin_skill_files("agentos", "").items()})
    return {**bt, **out}

def tree_hash(tree: dict) -> str:
    h = hashlib.sha256()
    for k in sorted(tree): h.update(k.encode() + b"\x00" + hashlib.sha256(tree[k]).digest())
    return h.hexdigest()[:16]

# ------------------------------------------------------------------ publishing (hash folder + symlink swap)
def _write_tree(dst: Path, tree: dict):
    for k, b in tree.items():
        p = dst / k; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b); os.chmod(p, 0o755 if "/scripts/" in "/" + k else 0o644)
    for d, _, _ in os.walk(dst): os.chmod(d, 0o755)

def publish(agent_id: int, tree: dict, base: Path | None = None) -> tuple[Path, Path]:
    """-> (agent folder to mount at MOUNT, hash folder with the files). Same content = same folders (nothing rewritten). Never raises for an empty tree."""
    tree = check_tree(tree)
    ad = agent_dir(agent_id, base)
    h = tree_hash(tree)
    with _LOCK:
        ad.mkdir(parents=True, exist_ok=True); os.chmod(ad, 0o755)
        hroot = ad / ".h"; hroot.mkdir(exist_ok=True); os.chmod(hroot, 0o755)
        hd = hroot / h
        if not hd.is_dir():
            tmp = hroot / f".tmp-{h}-{os.getpid()}-{threading.get_ident()}"
            shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
            _write_tree(tmp, tree); os.rename(tmp, hd)
        tops = sorted({k.split("/", 1)[0] for k in tree})
        for t in tops:                                                           # relative links: they resolve inside the box too
            link, new = ad / t, ad / f".new-{t}"
            if os.path.islink(link) and os.readlink(link) == f".h/{h}/{t}": continue
            if os.path.lexists(new): os.remove(new)
            os.symlink(f".h/{h}/{t}", new); os.replace(new, link)                # atomic swap
        for e in os.listdir(ad):                                                 # skills that are no longer in the agent
            if e != ".h" and not e.startswith(".new-") and e not in tops and os.path.islink(ad / e): os.remove(ad / e)
        olds = sorted((p for p in hroot.iterdir() if p.is_dir() and p.name != h and not p.name.startswith(".tmp-")), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in olds[KEEP_OLD:]: shutil.rmtree(p, ignore_errors=True)
    return ad, hd

async def prepare(agent_id: int, ws, skills, base: Path | None = None):
    """build_agent hook: publish the agent's skills -> (agent folder, hash folder, skill names) or None when the agent has none. A bad skill is raised as SkillError."""
    import asyncio
    if not skills: return None
    tree = await tree_for(ws, skills, agent_id)
    if not tree: return None
    ad, hd = await asyncio.to_thread(publish, agent_id, tree, base)
    return ad, hd, sorted({k.split("/", 1)[0] for k in tree if k.endswith("/SKILL.md")})

# ------------------------------------------------------------------ the read-only file route
class RoHostBackend(FilesystemBackend):
    """Read-only window on a hash folder (virtual paths: `/xlsx/SKILL.md` for `/memories/skills/xlsx/SKILL.md`)."""
    RO = "Error: read-only folder (skills are managed in Context Hub)"
    def __init__(self, root_dir): super().__init__(root_dir=str(root_dir), virtual_mode=True)
    def write(self, file_path, content): return WriteResult(error=self.RO)
    def edit(self, file_path, old_string, new_string, replace_all=False): return EditResult(error=self.RO)   # noqa: FBT002
    def delete(self, file_path): return DeleteResult(error=self.RO)
    def upload_files(self, files): return [FileUploadResponse(path=p, error=self.RO) for p, _ in files]
