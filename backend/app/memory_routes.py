"""GĐ11.70 Two-layer agent memory as files (F13). Folders the agent sees through the Deep Agents file tools:
  /memories/user/   one memory per person  = store namespace of deepagent_api._mem_ns (user / project / external chatter) - NOT shared
  /memories/agent/  shared by every thread = store namespace ("agent", <id>, "memories")
No new namespace, no data move: an old item `{"text": ...}` (written by luu_ghi_nho) shows up as `/<key>.md`; a new file is stored under the key = its path
with value {content, encoding, created_at, modified_at, text} (`text` keeps the semantic search of tim_ghi_nho working on it).
Write rule for the shared layer (also for the "user" layer when it falls back to the shared namespace = no signed-in person, e.g. cron):
  only a signed-in person (session / basic / oidc) who may edit the agent (owner, workspace admin, 'edit' share, editor on a workspace agent) and a run that
  `run_trust.can_write_agent_memory` (11.72) allows. Anything else (cron, API key, external source) = read only. Writes to the shared layer refuse
  secret-looking text and mask PII (pii.py). Refusals are counted (REFUSED) and put on the current span as `aos.memory.refused`."""
import logging, re
from deepagents.backends.protocol import (BackendProtocol, DeleteResult, EditResult, FileDownloadResponse, FileUploadResponse, GlobResult, GrepResult, LsResult,
                                          ReadResult, WriteResult)
from deepagents.backends.utils import (_glob_search_files, create_file_data, file_data_to_string, grep_matches_from_files, perform_string_replacement,
                                       slice_read_response, update_file_data)

log = logging.getLogger("aos.memory_routes")
MSG_READ_ONLY = "Bộ nhớ chung của agent chỉ đọc trong lượt chạy này"
MSG_PROPOSED = "Đã lưu thành đề xuất, chờ chủ agent duyệt / saved as a proposal, waiting for the agent owner to approve"
MSG_SECRET = "Không lưu được: nội dung có vẻ chứa mật khẩu / khoá bí mật / cannot save: the text looks like a secret"
MSG_NO_PATH = "Error: invalid path (use a file name like /ghi_chu.md)"
MSG_BIG = "Error: file too large (max 20000 characters)"
MAX_CHARS = 20000
SIGNED_IN = ("session", "basic", "oidc", "oidc_token")     # auth kinds of a person at the keyboard; api_key:* / channel / run / custom_auth / agent_identity are not
REFUSED = {"read_only": 0, "secret": 0, "pii_masked": 0}   # counters, read by tests / metrics

# small secret set (the secret-pattern module of 11.15 does not exist yet): provider keys, tokens, private keys, "password: value" pairs
SECRET_RX = re.compile("|".join([
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bsk-[A-Za-z0-9_\-]{16,}", r"\bAIza[0-9A-Za-z_\-]{30,}", r"\bAKIA[0-9A-Z]{16}\b", r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}", r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", r"\bBearer\s+[A-Za-z0-9._\-]{20,}",
    r"(?i:\b(?:password|passwd|mật\s*khẩu|api[_\- ]?key|secret|token)\b\s*[:=]\s*\S{6,})"]))

def looks_secret(text: str) -> bool: return bool(SECRET_RX.search(text or ""))

def _refused(kind):
    REFUSED[kind] = REFUSED.get(kind, 0) + 1
    try:
        from opentelemetry import trace
        sp = trace.get_current_span(); sp.set_attribute("aos.memory.refused", sum(v for k, v in REFUSED.items() if k != "pii_masked")); sp.set_attribute("aos.memory.refused_kind", kind)
    except Exception: pass  # noqa: BLE001

def _mask(text: str) -> str:
    try:
        import pii
        out = pii.mask_text(text)
        if out != text: REFUSED["pii_masked"] += 1
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("memory PII mask unavailable: %s", e); return text

# ------------------------------------------------------------------ who may write the shared layer
async def can_write_agent_memory(agent_id) -> bool:
    import admin
    u = admin.cur()
    if not u or not u.get("id") or str(u.get("auth") or "") not in SIGNED_IN: return False
    import run_trust      # 11.72: an external-source run (stranger, incoming mail, public channel) never writes the shared layer; import failure = error = no write (fail closed)
    if not run_trust.can_write_agent_memory(): return False
    try:
        import agent_sharing
        return bool((await agent_sharing.access(int(agent_id)))["edit"])
    except Exception: return False  # noqa: BLE001  (agent invisible to the caller / no DB context)

# ------------------------------------------------------------------ the backend
def _norm(path):
    p = "/" + str(path or "").strip().lstrip("/")
    if p == "/" or ".." in p.split("/") or "\x00" in p or p.endswith("/"): return None
    return p

class MemoryLayer(BackendProtocol):
    """One layer as a Deep Agents file backend (async only - the agent graph is async). layer: 'user' | 'agent'."""
    def __init__(self, agent_id, layer: str):
        self.agent_id, self.layer = int(agent_id), layer

    def _ns(self):
        if self.layer == "agent": return ("agent", str(self.agent_id), "memories")
        import deepagent_api as A
        return A._mem_ns(self.agent_id)

    def _shared(self): return self.layer == "agent" or self._ns() == ("agent", str(self.agent_id), "memories")
    def _st(self):
        import deepagent_api as A
        return A.S["store"]

    async def _deny(self, text=None, path=None):
        """None = write allowed, else the error text. Also cleans `text` for the shared layer (returns (err, clean)).
        13.6: a write (path given) that may not go through (external run, API key, cron) becomes a pending proposal for the owner (secret text is never stored)."""
        if len(text or "") > MAX_CHARS: return MSG_BIG, text
        if not self._shared(): return None, text
        if not await can_write_agent_memory(self.agent_id):
            _refused("read_only")
            if path and text is not None and not looks_secret(text):
                try:
                    import memory_admin
                    if await memory_admin.add_proposal(self.agent_id, path, _mask(text)): return MSG_PROPOSED, text
                except Exception as e: log.warning("memory proposal not stored: %s", e)  # noqa: BLE001
            return MSG_READ_ONLY, text
        if text is not None:
            if looks_secret(text): _refused("secret"); return MSG_SECRET, text
            text = _mask(text)
        return None, text

    async def _all(self):
        """path -> (store key, legacy?, value) for every file of the namespace."""
        st, ns, out, off = self._st(), self._ns(), {}, 0
        while True:
            page = await st.asearch(ns, limit=100, offset=off)
            for it in page:
                if tuple(it.namespace) != tuple(ns) or not isinstance(it.value, dict): continue
                if isinstance(it.value.get("content"), str) and str(it.key).startswith("/"): out[str(it.key)] = (it.key, False, it.value)
                elif isinstance(it.value.get("text"), str) and "content" not in it.value: out.setdefault(f"/{it.key}.md", (it.key, True, it.value))
            if len(page) < 100: return out
            off += 100

    async def _get(self, path):
        st, ns = self._st(), self._ns()
        it = await st.aget(ns, path)
        if it is not None and isinstance(it.value.get("content"), str): return path, False, it.value
        if path.endswith(".md") and "/" not in path[1:]:
            it = await st.aget(ns, path[1:-3])
            if it is not None and isinstance(it.value.get("text"), str) and "content" not in it.value: return it.key, True, it.value
        return None

    @staticmethod
    def _fd(legacy, v):
        if legacy:
            t = v.get("text") or ""
            return {"content": t, "encoding": "utf-8", **({"created_at": v["created_at"]} if isinstance(v.get("created_at"), str) else {})}
        return {"content": v["content"], "encoding": v.get("encoding") or "utf-8", **{k: v[k] for k in ("created_at", "modified_at") if isinstance(v.get(k), str)}}

    @staticmethod
    def _val(fd, legacy_old=None):
        if legacy_old is not None: return {**legacy_old, "text": fd["content"]}
        return {"content": fd["content"], "encoding": "utf-8", "created_at": fd.get("created_at"), "modified_at": fd.get("modified_at"), "text": fd["content"]}

    # --- reads
    async def als(self, path):
        base = "/" + str(path or "/").strip("/"); base = base if base.endswith("/") else base + "/"
        infos, dirs = [], set()
        for p, (_, leg, v) in (await self._all()).items():
            if not p.startswith(base): continue
            rel = p[len(base):]
            if "/" in rel: dirs.add(base + rel.split("/")[0] + "/"); continue
            fd = self._fd(leg, v); infos.append({"path": p, "is_dir": False, "size": len(file_data_to_string(fd).encode()), "modified_at": fd.get("modified_at", "")})
        infos += [{"path": d, "is_dir": True, "size": 0, "modified_at": ""} for d in sorted(dirs)]
        return LsResult(entries=sorted(infos, key=lambda x: x["path"]))

    async def aread(self, file_path, offset=0, limit=2000):
        p = _norm(file_path)
        if not p: return ReadResult(error=MSG_NO_PATH)
        r = await self._get(p)
        if r is None: return ReadResult(error=f"File '{file_path}' not found")
        return slice_read_response(self._fd(r[1], r[2]), offset, limit)

    async def _files(self): return {p: self._fd(leg, v) for p, (_, leg, v) in (await self._all()).items()}

    async def aglob(self, pattern, path=None):
        files = await self._files()
        try: res = _glob_search_files(files, pattern, path)
        except Exception as e: return GlobResult(error=str(e))  # noqa: BLE001
        if res == "No files found": return GlobResult(matches=[])
        return GlobResult(matches=[{"path": p, "is_dir": False, "size": len(file_data_to_string(files[p]).encode()) if p in files else 0,
                                    "modified_at": files.get(p, {}).get("modified_at", "")} for p in res.split("\n")])

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        return grep_matches_from_files(await self._files(), pattern, path, glob, max_count=max_count)

    async def adownload_files(self, paths):
        out = []
        for p in paths:
            r = await self._get(_norm(p) or "")
            out.append(FileDownloadResponse(path=p, content=None, error="file_not_found") if r is None else
                       FileDownloadResponse(path=p, content=file_data_to_string(self._fd(r[1], r[2])).encode(), error=None))
        return out

    # --- writes
    async def awrite(self, file_path, content):
        p = _norm(file_path)
        if not p: return WriteResult(error=MSG_NO_PATH)
        err, content = await self._deny(content, p)
        if err: return WriteResult(error=err)
        await self._put(p, content)
        return WriteResult(path=file_path)

    async def _put(self, p, content):
        r = await self._get(p); st, ns = self._st(), self._ns()
        if r is None: await st.aput(ns, p, self._val(create_file_data(content)))
        else: await st.aput(ns, r[0], self._val(update_file_data(self._fd(r[1], r[2]), content), r[2] if r[1] else None))

    async def owner_write(self, file_path, content):
        """13.6: write by the owner UI / an approved proposal (the caller already checked the right to edit). Same size / secret / PII rules. Returns the error text or None."""
        p = _norm(file_path)
        if not p: return MSG_NO_PATH
        if len(content or "") > MAX_CHARS: return MSG_BIG
        if looks_secret(content): _refused("secret"); return MSG_SECRET
        await self._put(p, _mask(content)); return None

    async def aedit(self, file_path, old_string, new_string, replace_all=False):
        p = _norm(file_path)
        if not p: return EditResult(error=MSG_NO_PATH)
        err, _ = await self._deny(None)
        if err: return EditResult(error=err)
        r = await self._get(p)
        if r is None: return EditResult(error=f"Error: File '{file_path}' not found")
        res = perform_string_replacement(file_data_to_string(self._fd(r[1], r[2])), old_string, new_string, replace_all)
        if isinstance(res, str): return EditResult(error=res)
        new, n = res
        err, new = await self._deny(new)
        if err: return EditResult(error=err)
        await self._st().aput(self._ns(), r[0], self._val(update_file_data(self._fd(r[1], r[2]), new), r[2] if r[1] else None))
        return EditResult(path=file_path, occurrences=int(n))

    async def adelete(self, file_path):
        p = _norm(file_path)
        if not p: return DeleteResult(error=MSG_NO_PATH)
        err, _ = await self._deny(None)
        if err: return DeleteResult(error=err)
        r = await self._get(p)
        if r is None: return DeleteResult(error=f"Error: File '{file_path}' not found")
        await self._st().adelete(self._ns(), r[0]); return DeleteResult(path=file_path)

    async def owner_delete(self, file_path):
        r = await self._get(_norm(file_path) or "")
        if r is None: return False
        await self._st().adelete(self._ns(), r[0]); return True

    async def aupload_files(self, files):
        out = []
        for p, b in files:
            try: w = await self.awrite(p, b.decode("utf-8"))
            except UnicodeDecodeError: out.append(FileUploadResponse(path=p, error="invalid_path")); continue
            out.append(FileUploadResponse(path=p, error=None if not w.error else "permission_denied"))
        return out

def routes(agent_id) -> dict:
    """CompositeBackend routes of one agent (added next to /pack/ /skills/ /memory/)."""
    return {"/memories/user/": MemoryLayer(agent_id, "user"), "/memories/agent/": MemoryLayer(agent_id, "agent")}

PROMPT_NOTE = ("\n\nMemory folders: /memories/user/ = what you know about THIS person (private to them); /memories/agent/ = shared notes for every conversation of this agent "
               "(only trusted signed-in editors can write there; never put secrets there). Use short .md files.")

# ------------------------------------------------------------------ permanent delete of the agent (11.34)
async def purge_for_agent(agent_id, workspace_id=None, identifier=None):
    """All long-term memory of the agent: namespaces ("agent", id, ...) = both layers, every person, project-less chatter. Repeatable.
    (agent_purge also deletes the same rows inside its transaction; this hook is the belt-and-braces / retry path.)"""
    import deepagent_api as A
    p = f"agent.{int(agent_id)}"
    try:
        async with A.S["pool"].connection() as c:
            await c.execute("SELECT set_config('app.ws','*',false), set_config('app.uid','0',false), set_config('app.role','admin',false), set_config('role','none',false)")
            if not (await (await c.execute("SELECT to_regclass('public.store') IS NOT NULL ok")).fetchone())["ok"]: return {"deleted": 0}
            cur = await c.execute("DELETE FROM store WHERE prefix = %s OR left(prefix, %s) = %s", (p, len(p) + 1, p + "."))
            n = max(cur.rowcount, 0)
            if (await (await c.execute("SELECT to_regclass('public.memory_proposals') IS NOT NULL ok")).fetchone())["ok"]:
                n += max((await c.execute("DELETE FROM memory_proposals WHERE agent_id = %s", (int(agent_id),))).rowcount, 0)   # 13.6
            return {"deleted": n}
    except Exception as e:  # noqa: BLE001
        return {"errors": [str(e)[:120]]}

def register():
    try:
        import agent_purge
        agent_purge.register_purge("memory", purge_for_agent)
    except Exception as e: log.warning("memory purge hook not registered: %s", e)  # noqa: BLE001
register()
