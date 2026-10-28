"""GĐ9.7 vận hành mã hoá dữ liệu: migrate (bản rõ/cũ -> v2), xoay KEK/DEK, trạng thái, mã hoá checkpoint LangGraph, mã hoá payload trace.

CLI (chạy trong thư mục app, cần PG_URI + khoá KEK như server):
  python -m aos_crypto status [--json]
  python -m aos_crypto migrate [--ws N]          # dữ liệu cũ (v1/Fernet) -> v2; + checkpoint/trace/lượt chạy/ví dụ dataset/tệp đính kèm của workspace đã bật nhóm tương ứng
  python -m aos_crypto rotate --kek              # bọc lại mọi DEK bằng KEK đang dùng (nhanh, không đụng dữ liệu)
  python -m aos_crypto rotate --dek WS           # DEK mới cho workspace WS, mã hoá lại dữ liệu theo lô, chạy online, chạy lại được nếu bị ngắt
Không bao giờ in/ghi log bản rõ hay khoá. Mọi thao tác idempotent."""
import argparse, base64, json, logging, os, struct, sys, threading, time
import crypto
from crypto import CryptoError, PLATFORM_WS

log = logging.getLogger("aos.crypto")
ENV = "$aos_enc"            # khoá của phong bì JSON trong tr_runs.inputs/outputs: {"$aos_enc": "v2:...", "c": "inputs", "r": "<run id>"}
BATCH = 200
CP_TYPE = "aos2"            # tên cipher trong cột type của checkpoint ("msgpack+aos2")

# ------------------------------------------------------------------ cờ theo workspace (đọc đồng bộ, cache ngắn; nhiều tiến trình dùng chung)
_FL: dict = {"t": 0.0, "d": {}}
FL_TTL = 10.0
GROUPS = ("checkpoints", "traces", "runs", "datasets", "attachments")   # 9.P2: nhóm dữ liệu bật/tắt mã hoá theo workspace
_OFF = {g: False for g in GROUPS}

def flags(ws: int | None) -> dict:
    now = time.monotonic()
    if now - _FL["t"] > FL_TTL:
        try:
            crypto.ensure_schema()
            with crypto._conn() as c:
                _FL["d"] = {r[0]: dict(zip(GROUPS, r[1:])) for r in c.execute("SELECT workspace_id, " + ", ".join(GROUPS) + " FROM crypto_ws")}
            _FL["t"] = now
        except Exception as e:  # noqa: BLE001  DB lỗi: giữ cờ cũ, thử lại sau; không chặn chat
            log.warning("đọc crypto_ws lỗi: %s", type(e).__name__); _FL["t"] = now - FL_TTL + 2
    return _FL["d"].get(ws, _OFF) if ws is not None else _OFF

def any_traces() -> bool:
    flags(None); return any(v["traces"] for v in _FL["d"].values())

def any_enc() -> bool:
    """Có workspace nào từng cấu hình mã hoá (kể cả đã tắt: dữ liệu cũ vẫn là phong bì và phải đọc được) -> decrypt_rows phải quét."""
    flags(None); return bool(_FL["d"])

def on(group: str, ws) -> bool:
    return ws is not None and bool(flags(int(ws)).get(group))

def invalidate_flags(): _FL["t"] = 0.0

def ctx(table_col: str, *key) -> str:
    """Ngữ cảnh AAD của một ô: 'bảng.cột|khoá1:khoá2' (cùng công thức khi ghi, đọc và migrate)."""
    return f"{table_col}|" + ":".join(str(k) for k in key)

def _cur_ws() -> int | None:
    try:
        import admin
        w = admin.rls_ctx()[0]
        return int(w) if str(w).isdigit() else None
    except Exception:  # noqa: BLE001
        return None

# ------------------------------------------------------------------ checkpoint LangGraph: cipher cho EncryptedSerializer (dùng DEK của ta, không phải LANGGRAPH_AES_KEY)
def _cp_ctx(ws: int) -> str: return f"checkpoint|ws{int(ws)}"

class AosCipher:
    """CipherProtocol của langgraph. Workspace bật 'checkpoints' -> mã hoá bằng DEK workspace; khác -> 'none' (giữ nguyên bytes).
    Giải mã luôn dựa vào header (dek_id), nên bật/tắt hay đổi DEK không làm mất lịch sử."""
    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        ws = _cur_ws()
        if ws is not None and flags(ws)["checkpoints"]: return CP_TYPE, crypto.seal_bytes(plaintext, ws, _cp_ctx(ws))
        return "none", plaintext
    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        if ciphername == "none": return ciphertext
        if ciphername != CP_TYPE: raise CryptoError("cipher checkpoint không hỗ trợ: " + ciphername)
        dws = crypto._dek(crypto.header(ciphertext)["dek_id"])[1]
        return crypto.unseal_bytes(ciphertext, _cur_ws(), _cp_ctx(dws))   # đang ở workspace khác -> từ chối

def make_serde():
    """serde= cho AsyncPostgresSaver: EncryptedSerializer chuẩn của LangGraph + cipher của ta (qua JsonPlusSerializer mặc định)."""
    from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
    return EncryptedSerializer(AosCipher())

# ------------------------------------------------------------------ payload trace (tr_runs.inputs/outputs)
# Phong bì JSON dùng chung cho nhiều bảng: {"$aos_enc": token, "c": cột, "r": khoá dòng, ["t": bảng, mặc định tr_runs]}. AAD = bảng.cột|khoá dòng.
JSON_COLS = {"tr_runs": ("inputs", "outputs"), "lg_runs": ("input", "command", "output", "config"), "lg_run_events": ("data",), "example_revisions": ("inputs", "outputs")}

def wrap_json(col: str, run_id, value, ws: int, table: str = "tr_runs") -> dict:
    rid = str(run_id).lower()
    env = {ENV: crypto.seal(json.dumps(value, ensure_ascii=False, default=str), ws, ctx(f"{table}.{col}", rid)), "c": col, "r": rid}
    if table != "tr_runs": env["t"] = table
    if table == "lg_runs" and col == "output" and isinstance(value, dict) and value.get("cost_usd") is not None:
        env["cost_usd"] = value["cost_usd"]      # số tiền không nhạy cảm; giữ rõ để SQL tính hạn mức chi tiêu (output->>'cost_usd') vẫn chạy
    return env

def is_env(v) -> bool: return isinstance(v, dict) and ENV in v and isinstance(v[ENV], str)

def unwrap_json(env: dict, *, key: str | None = None, row_id=None, ws: int | None = None):
    col, rid, tbl = env.get("c"), env.get("r"), env.get("t", "tr_runs")
    if tbl not in JSON_COLS: raise CryptoError("phong bì thuộc bảng lạ")
    if key in JSON_COLS[tbl] and key != col: raise CryptoError("phong bì trace nằm sai cột")
    if row_id is not None and str(row_id).lower() != rid: raise CryptoError("phong bì trace thuộc dòng khác")
    dws = crypto._dek(crypto.header(base64.urlsafe_b64decode(env[ENV][3:].encode()))["dek_id"])[1]
    return json.loads(crypto.unseal(env[ENV], ws if ws is not None else dws, ctx(f"{tbl}.{col}", rid)))

def seal_jsonb(group: str, table: str, col: str, key, j, ws):
    """Ghi: nếu workspace bật nhóm `group` -> Jsonb(phong bì), ngược lại giữ nguyên. j: Jsonb | None. key: khoá dòng trong AAD ('run_id', 'run_id:seq', 'example_id:version')."""
    if j is None or not on(group, ws): return j
    from psycopg.types.json import Jsonb
    obj = j.obj if hasattr(j, "obj") else j
    return j if is_env(obj) else Jsonb(wrap_json(col, key, obj, int(ws), table))

_RW: dict = {}
async def run_ws(rid) -> int | None:
    """Workspace của một run (lg_runs) nếu nó bật mã hoá runs, ngược lại None (cache 60 giây). Không truy vấn DB khi chưa workspace nào bật."""
    flags(None)
    if not any(v["runs"] for v in _FL["d"].values()): return None
    k = str(rid); hit = _RW.get(k)
    if not hit or time.monotonic() - hit[1] > 60:
        import deepagent_api as A
        r = await A.q("SELECT workspace_id FROM lg_runs WHERE run_id=%s", (rid,), one=True)
        hit = (r["workspace_id"] if r else None, time.monotonic()); _RW[k] = hit
        if len(_RW) > 5000: _RW.clear()
    return hit[0] if on("runs", hit[0]) else None

async def ds_ws(did) -> int | None:
    """Workspace của dataset nếu nó bật mã hoá 'datasets', ngược lại None (không truy vấn DB khi chưa workspace nào bật)."""
    flags(None)
    if not any(v["datasets"] for v in _FL["d"].values()): return None
    import deepagent_api as A
    r = await A.q("SELECT workspace_id FROM datasets WHERE id=%s", (did,), one=True)
    return r["workspace_id"] if r and on("datasets", r["workspace_id"]) else None

# Văn bản (chat_files.md): "aosenc$<id tệp>$<token v2>"; AAD = chat_files.md|<id tệp>
TXT = "aosenc$"
def seal_text(group: str, col_ctx: str, key, text, ws):
    if text is None or not on(group, ws) or text.startswith(TXT): return text
    return f"{TXT}{key}${crypto.seal(text, int(ws), ctx(col_ctx, key))}"

def open_text(col_ctx: str, text: str, row_id=None) -> str:
    _, key, tok = text.split("$", 2)
    if row_id is not None and str(row_id) != key: raise CryptoError("văn bản mã hoá thuộc dòng khác")
    dws = crypto._dek(_tok_dek(tok))[1]   # workspace lấy từ DEK trong header (RLS đã lọc dòng)
    return crypto.unseal(tok, dws, ctx(col_ctx, key))

def _bind(tbl: str, k: str, r: dict):
    """Khoá dòng thật (nếu truy vấn có chọn các cột đó) để phát hiện phong bì bị chép sang dòng khác."""
    try:
        if tbl == "tr_runs": return r.get("id") if k in ("inputs", "outputs") else None
        if tbl == "lg_runs": return r["run_id"] if k in JSON_COLS[tbl] else None
        if tbl == "lg_run_events": return f"{r['run_id']}:{r['seq']}" if k == "data" else None
        if tbl == "example_revisions": return f"{r['example_id']}:{r['version']}" if k in ("inputs", "outputs") else None
    except KeyError: return None
    return None

_PH = "[đã mã hoá]"

def decrypt_rows(rows):
    """Giải mã phong bì trace trong kết quả truy vấn (gọi từ deepagent_api.q). Chuỗi bị cắt từ SQL (left(inputs::text,..)) -> '[đã mã hoá]'
    (tìm kiếm văn bản/ILIKE trong SQL không dùng được với workspace bật mã hoá trace). Không bao giờ làm hỏng truy vấn."""
    if not rows or not any_enc(): return rows
    for r in rows:
        if not isinstance(r, dict): continue
        for k, v in list(r.items()):
            if is_env(v):
                try: r[k] = unwrap_json(v, key=k, row_id=_bind(v.get("t", "tr_runs"), k, r))
                except Exception as e:  # noqa: BLE001
                    log.warning("không giải mã được trace envelope (%s)", type(e).__name__); r[k] = {"error": "không giải mã được (khoá/dữ liệu)"}
            elif k == "md" and isinstance(v, str) and v.startswith(TXT):
                try: r[k] = open_text("chat_files.md", v, r.get("id") if len(str(r.get("id"))) == 36 else None)
                except Exception as e:  # noqa: BLE001
                    log.warning("không giải mã được văn bản tệp (%s)", type(e).__name__); r[k] = "[không giải mã được]"
            elif isinstance(v, str) and len(v) < 4000 and f'"{ENV}"' in v: r[k] = _PH
    return rows

_PW: dict = {}

async def trace_ws(project_id) -> int | None:
    """Workspace của project nếu nó bật mã hoá trace, ngược lại None (cache 30 giây)."""
    if not any_traces() or project_id is None: return None
    hit = _PW.get(project_id)
    if not hit or time.monotonic() - hit[1] > 30:
        import deepagent_api as A
        r = await A.q("SELECT workspace_id FROM projects WHERE id=%s", (project_id,), one=True)
        hit = (r["workspace_id"] if r else None, time.monotonic()); _PW[project_id] = hit
    return hit[0] if hit[0] is not None and flags(hit[0])["traces"] else None

def encrypt_trace_vals(vals: dict, ws: int):
    """Mã hoá inputs/outputs (Jsonb) của một run trước khi ghi."""
    for k in ("inputs", "outputs"):
        j = vals.get(k)
        if j is not None and hasattr(j, "obj") and not is_env(j.obj):
            vals[k] = type(j)(wrap_json(k, vals["id"], j.obj, ws))

# ------------------------------------------------------------------ bảng/cột cần mã hoá (chung cho migrate / rotate / status)
# (tên, bảng, cột, cột khoá-dòng của AAD, cột phụ cần để dựng ctx, biểu thức workspace)
TEXT_TARGETS = [
    dict(name="ws_secrets.value_enc", table="ws_secrets", col="value_enc", key=("name",), ws="workspace_id"),
    dict(name="oauth_providers.client_secret_enc", table="oauth_providers", col="client_secret_enc", key=("id",), ws="workspace_id"),
    dict(name="oauth_providers.token_headers_enc", table="oauth_providers", col="token_headers_enc", key=("id",), ws="workspace_id"),   # GĐ11.25: sealed JSON of extra token-endpoint headers
    dict(name="oauth_providers.registration_token_enc", table="oauth_providers", col="registration_token_enc", key=("id",), ws="workspace_id"),   # GĐ11.26: RFC 7592 registration access token
    dict(name="oauth_connections.access_token_enc", table="oauth_connections", col="access_token_enc", key=("provider_id", "aad_owner"), ws="workspace_id"),   # GĐ11.24: aad_owner = user id for user rows
    dict(name="oauth_connections.refresh_token_enc", table="oauth_connections", col="refresh_token_enc", key=("provider_id", "aad_owner"), ws="workspace_id"),
    dict(name="oauth_connections.value_enc", table="oauth_connections", col="value_enc", key=("id",), ws="workspace_id"),
    dict(name="users.totp_secret_enc", table="users", col="totp_secret_enc", key=("id",), ws=str(PLATFORM_WS)),
]

class Stop(Exception): pass

class Budget:
    """Giới hạn số lô (cho test 'bị ngắt giữa chừng' và chạy từng phần)."""
    def __init__(self, n=None): self.n = n
    def tick(self):
        if self.n is not None:
            if self.n <= 0: raise Stop()
            self.n -= 1

def _tables(c) -> set:
    return {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}

def _tok_dek(tok: str) -> int | None:
    try: return crypto.header(base64.urlsafe_b64decode(tok[3:].encode()))["dek_id"]
    except Exception:  # noqa: BLE001
        return None

def _text_pass(c, t: dict, ws_only: int | None, want, stats: dict, bud: Budget, batch: int):
    """Quét một cột text theo lô (keyset theo id). want(tok, ws) -> True nếu dòng cần mã hoá lại."""
    ws_expr = t["ws"]; keys = ", ".join(f"{k} AS k_{k}" for k in dict.fromkeys(t["key"]))
    last = 0
    while True:
        sql = f"SELECT id AS _id, {t['col']} AS _v, {ws_expr} AS _ws, {keys} FROM {t['table']} WHERE id > %s AND {t['col']} IS NOT NULL"
        args: list = [last]
        if ws_only is not None: sql += f" AND {ws_expr} = %s"; args.append(ws_only)
        cur = c.execute(sql + " ORDER BY id LIMIT %s", (*args, batch)); rows = cur.fetchall(); names = [d.name for d in cur.description]
        if not rows: return
        bud.tick()
        for raw in rows:
            r = dict(zip(names, raw)); last = r["_id"]; tok = r["_v"]; k = crypto.kind(tok)
            stats["rows"] += 1
            if not want(tok, r["_ws"]): stats["skipped"] += 1; continue
            c_ = ctx(t["name"], *(r["k_" + x] for x in t["key"]))
            try: new = crypto.seal(crypto.unseal(tok, r["_ws"], c_), r["_ws"], c_)
            except CryptoError as e:
                stats["failed"] += 1; log.warning("%s id=%s không đọc được (%s)", t["name"], r["_id"], type(e).__name__); continue
            n = c.execute(f"UPDATE {t['table']} SET {t['col']}=%s WHERE id=%s AND {t['col']}=%s", (new, r["_id"], tok)).rowcount   # CAS: app vừa ghi đè thì bỏ qua
            stats["updated" if n else "raced"] += 1

def _active_ids(c) -> dict:
    return {(r[0], r[1]): r[2] for r in c.execute("SELECT workspace_id, purpose, id FROM crypto_deks WHERE status='active'")}

# ---- checkpoints (bytea): type 'msgpack' | 'msgpack+none' | 'msgpack+aos2'
CP_TABLES = {"checkpoint_blobs": ("thread_id", "checkpoint_ns", "channel", "version"), "checkpoint_writes": ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx")}
_WS_OF_THREAD = "(SELECT workspace_id FROM agents WHERE id = split_part(substr(thread_id,2),':',1)::int)"
_THREAD_IS_AGENT = "thread_id ~ '^a[0-9]+:'"

def _cp_pass(c, table: str, ws: int, mode: str, active: int, stats: dict, bud: Budget, batch: int, retired: list):
    """mode='encrypt': bản rõ/'+none' của thread thuộc ws -> aos2. mode='rekey': aos2 với DEK trong retired -> DEK active."""
    pk = CP_TABLES[table]; pkc = ", ".join(pk)
    if mode == "encrypt":
        cond = f"{_THREAD_IS_AGENT} AND {_WS_OF_THREAD} = %s AND blob IS NOT NULL AND type NOT LIKE '%%+{CP_TYPE}'"; args: tuple = (ws,)
    else:
        if not retired: return
        cond = f"type LIKE '%%+{CP_TYPE}' AND substring(blob from 3 for 4) = ANY(%s)"; args = ([struct.pack(">I", d) for d in retired],)
    while True:
        cur = c.execute(f"SELECT {pkc}, type, blob FROM {table} WHERE {cond} LIMIT %s", (*args, batch)); rows = cur.fetchall()
        if not rows: return
        bud.tick(); progressed = 0
        for r in rows:
            keys, typ, blob = r[:len(pk)], r[len(pk)], bytes(r[len(pk) + 1])
            base = typ.split("+", 1)[0]; stats["rows"] += 1
            try:
                plain = blob if mode == "encrypt" else crypto.unseal_bytes(blob, None, _cp_ctx(crypto._dek(crypto.header(blob)["dek_id"])[1]))
                new = crypto.seal_bytes(plain, ws, _cp_ctx(ws))
            except CryptoError as e:
                stats["failed"] += 1; log.warning("%s: dòng không đọc được (%s)", table, type(e).__name__); continue
            where = " AND ".join(f"{k}=%s" for k in pk)
            n = c.execute(f"UPDATE {table} SET type=%s, blob=%s WHERE {where} AND type=%s", (f"{base}+{CP_TYPE}", new, *keys, typ)).rowcount
            stats["updated" if n else "raced"] += 1; progressed += n
        if not progressed: return          # toàn dòng lỗi/đua: tránh vòng lặp vô hạn (lần chạy sau thử lại)

# ---- tr_runs (jsonb phong bì)
def _tr_pass(c, ws: int, mode: str, active: int, stats: dict, bud: Budget, batch: int, retired: list):
    last = None
    while True:
        base = "SELECT r.id, r.inputs, r.outputs FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE p.workspace_id=%s"
        args: list = [ws]
        if last is not None: base += " AND r.id > %s"; args.append(last)
        rows = c.execute(base + " ORDER BY r.id LIMIT %s", (*args, batch)).fetchall()
        if not rows: return
        bud.tick()
        for rid, inp, out in rows:
            last = rid; sets = {}
            for col, v in (("inputs", inp), ("outputs", out)):
                if v is None: continue
                try:
                    if is_env(v):
                        if mode != "rekey" or _tok_dek(v[ENV]) not in retired: continue
                        plain = unwrap_json(v, key=col, row_id=rid)
                    elif mode == "encrypt": plain = v
                    else: continue
                    sets[col] = wrap_json(col, rid, plain, ws)
                except CryptoError as e:
                    stats["failed"] += 1; log.warning("tr_runs: dòng không đọc được (%s)", type(e).__name__)
            stats["rows"] += 1
            if sets:
                from psycopg.types.json import Jsonb
                cols = ", ".join(f"{k}=%s" for k in sets)
                n = c.execute(f"UPDATE tr_runs SET {cols} WHERE id=%s", (*[Jsonb(x) for x in sets.values()], rid)).rowcount
                stats["updated" if n else "raced"] += 1
            else: stats["skipped"] += 1

# ---- các bảng JSON dùng phong bì chung (9.P2): lg_runs, lg_run_events, example_revisions
JSON_TARGETS = [
    dict(name="lg_runs", group="runs", table="lg_runs", from_="lg_runs t", ws="t.workspace_id", pk=("run_id",), key=("run_id",), cols=("input", "command", "output", "config")),
    dict(name="lg_run_events", group="runs", table="lg_run_events", from_="lg_run_events t JOIN lg_runs x ON x.run_id=t.run_id", ws="x.workspace_id",
         pk=("run_id", "seq"), key=("run_id", "seq"), cols=("data",)),
    dict(name="example_revisions", group="datasets", table="example_revisions",
         from_="example_revisions t JOIN dataset_examples e ON e.id=t.example_id JOIN datasets d ON d.id=e.dataset_id", ws="d.workspace_id",
         pk=("id",), key=("example_id", "version"), cols=("inputs", "outputs")),
]

def _json_pass(c, t: dict, ws: int, mode: str, stats: dict, bud: Budget, batch: int, retired: list):
    """mode='encrypt': JSON rõ -> phong bì (workspace đã bật nhóm). mode='rekey': phong bì dùng DEK trong retired -> DEK đang dùng. Keyset theo khoá chính, CAS khi ghi."""
    from psycopg.types.json import Jsonb
    names = list(dict.fromkeys(t["pk"] + t["key"] + t["cols"])); sel = ", ".join(f"t.{x}" for x in names); pkc = ", ".join(f"t.{x}" for x in t["pk"]); last = None
    while True:
        where, args = f"{t['ws']}=%s", [ws]
        if last is not None: where += f" AND ({pkc}) > ({', '.join(['%s'] * len(t['pk']))})"; args += list(last)
        rows = c.execute(f"SELECT {sel} FROM {t['from_']} WHERE {where} ORDER BY {pkc} LIMIT %s", (*args, batch)).fetchall()
        if not rows: return
        bud.tick()
        for raw in rows:
            r = dict(zip(names, raw)); last = tuple(r[k] for k in t["pk"]); key = ":".join(str(r[k]) for k in t["key"]); sets = {}
            for col in t["cols"]:
                v = r[col]
                if v is None: continue
                try:
                    if is_env(v):
                        if mode != "rekey" or _tok_dek(v[ENV]) not in retired: continue
                        plain = unwrap_json(v, key=col, row_id=key)
                    elif mode == "encrypt": plain = v
                    else: continue
                    sets[col] = (v, wrap_json(col, key, plain, ws, t["table"]))
                except CryptoError as e:
                    stats["failed"] += 1; log.warning("%s: dòng không đọc được (%s)", t["name"], type(e).__name__)
            stats["rows"] += 1
            if not sets: stats["skipped"] += 1; continue
            cond = " AND ".join(f"{k}=%s" for k in t["pk"]) + "".join(f" AND {k}=%s::jsonb" for k in sets)
            n = c.execute(f"UPDATE {t['table']} SET " + ", ".join(f"{k}=%s" for k in sets) + f" WHERE {cond}",
                          (*[Jsonb(x[1]) for x in sets.values()], *[r[k] for k in t["pk"]], *[Jsonb(x[0]) for x in sets.values()])).rowcount
            stats["updated" if n else "raced"] += 1

# ---- tệp đính kèm chat: bytes trên đĩa (var/files/_enc/<id[:2]>/<id>) + chat_files.md
def files_dir():
    from pathlib import Path
    return Path(os.environ.get("AOS_FILES_DIR", str(Path(__file__).resolve().parent.parent / "var" / "files")))

def enc_path(fid):
    s = str(fid); return files_dir() / "_enc" / s[:2] / s

def blob_ctx(fid) -> str: return ctx("chat_files.blob", fid)

def write_atomic(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True); tmp = p.with_suffix(".tmp"); tmp.write_bytes(data); tmp.replace(p)

def _att_pass(c, ws: int, mode: str, stats: dict, bud: Budget, batch: int, retired: list):
    """mode='encrypt': tệp rõ (enc=false) của ws -> mã hoá (bytes + md). mode='rekey': tệp mã hoá bằng DEK đã nghỉ -> DEK đang dùng."""
    last = "00000000-0000-0000-0000-000000000000"
    while True:
        rows = c.execute("SELECT id, sha256, md, enc FROM chat_files WHERE workspace_id=%s AND id > %s ORDER BY id LIMIT %s", (ws, last, batch)).fetchall()
        if not rows: return
        bud.tick()
        for fid, sha, md, enc in rows:
            last = fid; stats["rows"] += 1; fid = str(fid)
            try:
                if not enc:
                    if mode != "encrypt": stats["skipped"] += 1; continue
                    src = files_dir() / sha[:2] / sha
                    data = src.read_bytes() if src.exists() else None
                    if data is None: stats["failed"] += 1; continue
                    write_atomic(enc_path(fid), crypto.seal_bytes(data, ws, blob_ctx(fid)))
                    new_md = f"{TXT}{fid}${crypto.seal(md, ws, ctx('chat_files.md', fid))}" if md is not None and not md.startswith(TXT) else md
                    n = c.execute("UPDATE chat_files SET enc=true, md=%s WHERE id=%s AND NOT enc", (new_md, fid)).rowcount
                    stats["updated" if n else "raced"] += 1
                    if n and not c.execute("SELECT 1 FROM chat_files WHERE sha256=%s AND NOT enc LIMIT 1", (sha,)).fetchone():
                        src.unlink(missing_ok=True)       # không còn dòng nào dùng bản rõ này
                    continue
                p = enc_path(fid); blob = p.read_bytes(); new_blob = new_md = None
                if mode == "rekey":
                    if crypto.header(blob)["dek_id"] in retired:
                        new_blob = crypto.seal_bytes(crypto.unseal_bytes(blob, None, blob_ctx(fid)), ws, blob_ctx(fid))
                    if md and md.startswith(TXT) and _tok_dek(md.split("$", 2)[2]) in retired:
                        new_md = f"{TXT}{fid}${crypto.seal(open_text('chat_files.md', md, fid), ws, ctx('chat_files.md', fid))}"
                if new_blob is None and new_md is None: stats["skipped"] += 1; continue
                if new_blob is not None: write_atomic(p, new_blob)
                if new_md is not None: c.execute("UPDATE chat_files SET md=%s WHERE id=%s AND md=%s", (new_md, fid, md))
                stats["updated"] += 1
            except (CryptoError, OSError) as e:
                stats["failed"] += 1; log.warning("chat_files: tệp không xử lý được (%s)", type(e).__name__)

def _exatt_pass(c, ws: int, mode: str, stats: dict, bud: Budget, batch: int, retired: list):
    """example_attachments.data (bytea, row-bound by id): mode 'encrypt' = plain -> sealed, 'rekey' = sealed with a retired DEK -> active."""
    last = 0
    while True:
        rows = c.execute("""SELECT a.id, a.data, a.enc FROM example_attachments a JOIN dataset_examples e ON e.id=a.example_id JOIN datasets d ON d.id=e.dataset_id
                            WHERE d.workspace_id=%s AND a.id > %s ORDER BY a.id LIMIT %s""", (ws, last, batch)).fetchall()
        if not rows: return
        bud.tick()
        for aid, data, enc in rows:
            last = aid; stats["rows"] += 1; data = bytes(data); cx = ctx("example_attachments.data", aid)
            try:
                if not enc:
                    if mode != "encrypt": stats["skipped"] += 1; continue
                    new = crypto.seal_bytes(data, ws, cx)
                elif mode == "rekey" and crypto.header(data)["dek_id"] in retired: new = crypto.seal_bytes(crypto.unseal_bytes(data, None, cx), ws, cx)
                else: stats["skipped"] += 1; continue
            except CryptoError as e:
                stats["failed"] += 1; log.warning("example_attachments: dòng không đọc được (%s)", type(e).__name__); continue
            n = c.execute("UPDATE example_attachments SET data=%s, enc=true WHERE id=%s AND data=%s", (new, aid, data)).rowcount
            stats["updated" if n else "raced"] += 1

def _has_cols(c, table: str, *cols) -> bool:
    got = {r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,))}
    return all(x in got for x in cols)

def _new_stats(): return {"rows": 0, "updated": 0, "skipped": 0, "failed": 0, "raced": 0}

def migrate(ws: int | None = None, *, batch: int = BATCH, max_batches: int | None = None) -> dict:
    """Idempotent. Cột text: v1/Fernet -> v2 (mọi ws, hoặc 1 ws). Checkpoint/trace: chỉ workspace đã bật cờ tương ứng (hoặc ws chỉ định có cờ bật)."""
    crypto.ensure_schema(); bud = Budget(max_batches); out = {"text": _new_stats(), "checkpoints": _new_stats(), "traces": _new_stats(), "runs": _new_stats(), "datasets": _new_stats(), "attachments": _new_stats(), "done": True}
    with crypto._conn() as c:
        have = _tables(c)
        def need(tok, w):
            k = crypto.kind(tok); return k in ("v1", "fernet")
        try:
            for t in TEXT_TARGETS:
                if t["table"] in have: _text_pass(c, t, ws if t["ws"] != str(PLATFORM_WS) else None, need, out["text"], bud, batch)
            flg = {r[0]: dict(zip(GROUPS, r[1:])) for r in c.execute("SELECT workspace_id, " + ", ".join(GROUPS) + " FROM crypto_ws")}
            for w, f in flg.items():
                if ws is not None and w != ws: continue
                cp, tr = f["checkpoints"], f["traces"]
                for t in JSON_TARGETS:
                    if f[t["group"]] and t["table"] in have and (t["table"] != "example_revisions" or "datasets" in have):
                        _json_pass(c, t, w, "encrypt", out[t["group"]], bud, batch, [])
                if f["datasets"] and "example_attachments" in have and _has_cols(c, "example_attachments", "enc"):
                    _exatt_pass(c, w, "encrypt", out["datasets"], bud, batch, [])
                if f["attachments"] and "chat_files" in have and _has_cols(c, "chat_files", "enc", "workspace_id"):
                    _att_pass(c, w, "encrypt", out["attachments"], bud, batch, [])
                if cp and "checkpoint_blobs" in have:
                    a = crypto.active_dek(w)[0]
                    for tb in CP_TABLES: _cp_pass(c, tb, w, "encrypt", a, out["checkpoints"], bud, batch, [])
                if tr and "tr_runs" in have:
                    _tr_pass(c, w, "encrypt", crypto.active_dek(w)[0], out["traces"], bud, batch, [])
        except Stop: out["done"] = False
    return out

# ------------------------------------------------------------------ xoay khoá
def rotate_kek(ws: list | None = None) -> dict:
    """Bọc lại mọi DEK bằng KEK đang dùng. Idempotent, từng DEK một (CAS theo kid cũ) nên ngắt giữa chừng cũng an toàn."""
    crypto._load(); crypto.ensure_schema(); n = skipped = failed = 0
    with crypto._conn() as c:
        cur = c.execute("SELECT * FROM crypto_deks WHERE wrapped IS NOT NULL AND kid <> %s AND (%s::int[] IS NULL OR workspace_id = ANY(%s)) ORDER BY id", (crypto._CUR, ws, ws)); names = [d.name for d in cur.description]
        for raw in cur.fetchall():
            row = dict(zip(names, raw))
            try: dek = crypto.unwrap_dek(row)
            except CryptoError as e:
                failed += 1; log.warning("DEK #%s không mở được bằng KEK '%s' (%s)", row["id"], row["kid"], type(e).__name__); continue
            w = crypto.wrap_dek(dek, row["id"], row["workspace_id"], row["purpose"], crypto._CUR, row["alg"])  # type: ignore[arg-type]
            m = c.execute("UPDATE crypto_deks SET wrapped=%s, kid=%s WHERE id=%s AND kid=%s", (w, crypto._CUR, row["id"], row["kid"])).rowcount
            n += m; skipped += 0 if m else 1
    return {"rewrapped": n, "raced": skipped, "failed": failed, "active_kid": crypto._CUR}

def rotate_dek(ws: int, *, batch: int = BATCH, max_batches: int | None = None, settle: float | None = None) -> dict:
    """DEK mới cho ws + mã hoá lại dữ liệu theo lô (online, chạy lại được). Khi không còn dòng nào dùng DEK cũ -> huỷ DEK cũ (crypto-erase)."""
    crypto._load(); crypto.ensure_schema(); bud = Budget(max_batches); res = {"text": _new_stats(), "checkpoints": _new_stats(), "traces": _new_stats(), "runs": _new_stats(), "datasets": _new_stats(), "attachments": _new_stats(), "done": True, "destroyed": []}
    with crypto._conn() as c:
        retired = [r[0] for r in c.execute("SELECT id FROM crypto_deks WHERE workspace_id=%s AND purpose='data' AND status='retired' AND wrapped IS NOT NULL", (ws,))]
        if not retired:                                                       # chưa có lần xoay dở -> bắt đầu mới
            with c.transaction():
                old = [r[0] for r in c.execute("SELECT id FROM crypto_deks WHERE workspace_id=%s AND purpose='data' AND status='active' FOR UPDATE", (ws,))]
                if old: c.execute("UPDATE crypto_deks SET status='retired', rotated_at=now() WHERE id = ANY(%s)", (old,))
                crypto._new_dek(c, ws, "data"); retired = old
            crypto._ACT.pop((ws, "data"), None)
        active = c.execute("SELECT id FROM crypto_deks WHERE workspace_id=%s AND purpose='data' AND status='active'", (ws,)).fetchone()[0]
        crypto._ACT[(ws, "data")] = (active, time.monotonic())

        def sweep():
            have = _tables(c)
            act = crypto._ACT[(ws, "data")][0]
            def want(tok, w):
                k = crypto.kind(tok)
                return k in ("v1", "fernet") or (k == "v2" and _tok_dek(tok) in retired)
            for t in TEXT_TARGETS:
                if t["table"] in have and (t["ws"] == str(PLATFORM_WS)) == (ws == PLATFORM_WS):
                    _text_pass(c, t, None if ws == PLATFORM_WS else ws, want, res["text"], bud, batch)
            if "checkpoint_blobs" in have:
                for tb in CP_TABLES: _cp_pass(c, tb, ws, "rekey", act, res["checkpoints"], bud, batch, retired)
            if "tr_runs" in have: _tr_pass(c, ws, "rekey", act, res["traces"], bud, batch, retired)
            for t in JSON_TARGETS:
                if t["table"] in have and (t["table"] != "example_revisions" or "datasets" in have): _json_pass(c, t, ws, "rekey", res[t["group"]], bud, batch, retired)
            if "example_attachments" in have and _has_cols(c, "example_attachments", "enc"): _exatt_pass(c, ws, "rekey", res["datasets"], bud, batch, retired)
            if "chat_files" in have and _has_cols(c, "chat_files", "enc", "workspace_id"): _att_pass(c, ws, "rekey", res["attachments"], bud, batch, retired)
        try:
            sweep()
            time.sleep(crypto.ACT_TTL + 2 if settle is None else settle)       # tiến trình khác có thể còn ghi bằng DEK cũ (cache TTL) -> quét lại
            sweep()
            if not any(res[k]["failed"] for k in ("text", "checkpoints", "traces", "runs", "datasets", "attachments")):
                for d in retired:
                    c.execute("UPDATE crypto_deks SET wrapped=NULL, status='destroyed' WHERE id=%s", (d,)); crypto._DEKS.pop(d, None); res["destroyed"].append(d)
        except Stop: res["done"] = False
    return res

# ------------------------------------------------------------------ trạng thái
def status() -> dict:
    crypto._load(); crypto.ensure_schema(); out: dict = {"active_kid": crypto._CUR, "alg_default": crypto.ALG_NAME[crypto.default_alg()]}
    with crypto._conn() as c:
        have = _tables(c)
        out["deks"] = [dict(id=r[0], workspace_id=r[1], purpose=r[2], kid=r[3], alg=crypto.ALG_NAME.get(r[4]), status=r[5], created_at=r[6].isoformat() if r[6] else None)
                       for r in c.execute("SELECT id, workspace_id, purpose, kid, alg, status, created_at FROM crypto_deks ORDER BY id")]
        by_kid: dict = {}
        for d in out["deks"]:
            if d["status"] != "destroyed": by_kid[d["kid"]] = by_kid.get(d["kid"], 0) + 1
        out["deks_per_kek"] = by_kid
        out["kek_stale_deks"] = sum(n for k, n in by_kid.items() if k != crypto._CUR)
        cols: dict = {}; legacy = 0
        for t in TEXT_TARGETS:
            if t["table"] not in have: continue
            cnt: dict = {}
            for (tok,) in c.execute(f"SELECT {t['col']} FROM {t['table']} WHERE {t['col']} IS NOT NULL"):
                k = crypto.kind(tok); label = k
                if k == "v2": label = f"v2/dek{_tok_dek(tok)}"
                elif k == "v1": label = "v1/kid:" + tok.split(":", 2)[1]
                cnt[label] = cnt.get(label, 0) + 1
                legacy += k in ("v1", "fernet", "plain")
            cols[t["name"]] = cnt
        if "users" in have:
            cols["users.totp_recovery"] = {"hmac(m1)": c.execute("SELECT count(*) FROM users, unnest(totp_recovery) h WHERE h LIKE 'm1:%'").fetchone()[0],
                                           "sha256(cũ)": c.execute("SELECT count(*) FROM users, unnest(totp_recovery) h WHERE h NOT LIKE 'm1:%'").fetchone()[0]}
        out["columns"] = cols; out["legacy_rows"] = legacy
        cp: dict = {}
        for tb in CP_TABLES:
            if tb not in have: continue
            for typ, d, n in c.execute(f"SELECT split_part(type,'+',2) s, CASE WHEN type LIKE '%%+{CP_TYPE}' THEN encode(substring(blob from 3 for 4),'hex') END, count(*) FROM {tb} GROUP BY 1,2"):
                label = "aos2/dek" + str(int(d, 16)) if d else ("none/plain" if typ in ("", "none") else typ)
                cp[label] = cp.get(label, 0) + n
        out["checkpoints"] = cp
        tr: dict = {}
        if "tr_runs" in have:
            tr["plain"] = c.execute(f"SELECT count(*) FROM tr_runs WHERE NOT (inputs ? '{ENV}') AND NOT (outputs ? '{ENV}')").fetchone()[0]
            for d, n in c.execute(f"""SELECT get_byte(b,2)*16777216+get_byte(b,3)*65536+get_byte(b,4)*256+get_byte(b,5) dek, count(*) FROM
                (SELECT decode(translate(substr(coalesce(inputs,outputs)->>'{ENV}',4),'-_','+/'),'base64') b FROM tr_runs WHERE inputs ? '{ENV}' OR outputs ? '{ENV}') x GROUP BY 1"""):
                tr[f"v2/dek{d}"] = n
        out["traces"] = tr
        pay: dict = {}
        for t in JSON_TARGETS:
            if t["table"] not in have: continue
            envx = "coalesce(" + ", ".join(f"t.{x}->>'{ENV}'" for x in t["cols"]) + ")"; anyc = " OR ".join(f"t.{x} IS NOT NULL" for x in t["cols"])
            plain = " OR ".join(f"(t.{x} IS NOT NULL AND NOT t.{x} ? '{ENV}')" for x in t["cols"])
            d = {"plain": c.execute(f"SELECT count(*) FROM {t['table']} t WHERE {plain}").fetchone()[0]}
            for dk, n in c.execute(f"""SELECT get_byte(b,2)*16777216+get_byte(b,3)*65536+get_byte(b,4)*256+get_byte(b,5) dek, count(*) FROM
                (SELECT decode(translate(substr({envx},4),'-_','+/'),'base64') b FROM {t['table']} t WHERE {anyc}) x WHERE b IS NOT NULL GROUP BY 1"""):
                d[f"v2/dek{dk}"] = n
            pay[t["name"]] = d
        if "chat_files" in have and _has_cols(c, "chat_files", "enc"):
            pay["chat_files"] = {"plain": c.execute("SELECT count(*) FROM chat_files WHERE NOT enc").fetchone()[0], "v2": c.execute("SELECT count(*) FROM chat_files WHERE enc").fetchone()[0]}
        if "example_attachments" in have and _has_cols(c, "example_attachments", "enc"):
            pay["example_attachments"] = {"plain": c.execute("SELECT count(*) FROM example_attachments WHERE NOT enc").fetchone()[0], "v2": c.execute("SELECT count(*) FROM example_attachments WHERE enc").fetchone()[0]}
        out["payloads"] = pay
        out["workspaces"] = [dict(zip(("workspace_id", *GROUPS), r)) for r in c.execute("SELECT workspace_id, " + ", ".join(GROUPS) + " FROM crypto_ws ORDER BY 1")]
    return out

# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="aos_crypto", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    m = sub.add_parser("migrate"); m.add_argument("--ws", type=int); m.add_argument("--batch", type=int, default=BATCH)
    r = sub.add_parser("rotate"); g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--kek", action="store_true"); g.add_argument("--dek", type=int, metavar="WS"); r.add_argument("--batch", type=int, default=BATCH)
    a = ap.parse_args(argv)
    if a.cmd == "status":
        st = status(); print(json.dumps(st, indent=1, ensure_ascii=False) if a.json else _fmt(st))
    elif a.cmd == "migrate": print(json.dumps(migrate(a.ws, batch=a.batch)))
    elif a.kek: print(json.dumps(rotate_kek()))
    else: print(json.dumps(rotate_dek(a.dek, batch=a.batch)))
    return 0

def _fmt(st: dict) -> str:
    L = [f"KEK đang dùng: {st['active_kid']}  | thuật toán mặc định: {st['alg_default']}", f"DEK theo KEK: {st['deks_per_kek']}  (cần bọc lại: {st['kek_stale_deks']})", f"Dòng cũ chưa migrate: {st['legacy_rows']}"]
    for k, v in st["columns"].items(): L.append(f"  {k}: {v}")
    L.append(f"  checkpoints: {st['checkpoints']}"); L.append(f"  traces: {st['traces']}")
    for k, v in (st.get("payloads") or {}).items(): L.append(f"  {k}: {v}")
    return "\n".join(L)

if __name__ == "__main__":
    sys.exit(main())
