"""Mã hoá dữ liệu khi lưu (GĐ6.5 -> GĐ9.7): mã hoá phong bì (envelope encryption).

KEK (khoá bọc, ở .env/file, KHÔNG nằm trong DB)  ->  bọc  ->  DEK theo từng workspace (bảng crypto_deks)  ->  mã hoá dữ liệu.
  * KEK: AOS_SECRET_KEYS="kid1:<b64>,kid2:<b64>" (+ AOS_ACTIVE_KID, mặc định khoá đầu danh sách = khoá đang dùng để bọc),
    hoặc kiểu cũ AOS_SECRET_KEY (+ AOS_SECRET_KEYS_OLD). Khoá = base64 của 32 byte. Đổi KEK = bọc lại DEK (nhanh, không đụng dữ liệu).
  * Thiếu KEK: dev/local tự sinh var/aos_secret.key (chmod 600); AOS_ENV=prod thì báo lỗi.
  * DEK: 32 byte ngẫu nhiên / workspace / mục đích ('data' mã hoá, 'mac' băm có khoá). workspace 0 = nền tảng (khoá TOTP của người dùng).
  * Mã v2 "v2:<b64url(ver|alg|dek_id|nonce12|ct+tag)>": header 6 byte (ver=2, alg 1=AES-256-GCM / 2=AES-256-GCM-SIV, dek_id u32) nằm trong AAD
    cùng ngữ cảnh "bảng.cột|workspace|khoá dòng" -> chuyển mã sang dòng/cột/workspace khác thì giải mã thất bại. Không log bản rõ hay khoá.
  * Mặc định AES-256-GCM-SIV (RFC 8452): chống lặp nonce (một DEK mã hoá hàng triệu dòng nonce ngẫu nhiên 96 bit); đặt AOS_CRYPTO_ALG=gcm nếu cần FIPS.
  * Đọc được mã cũ: v1 (KEK trực tiếp, GĐ6.5) và Fernet (AOS_OAUTH_KEY của OAuth 8.5) -> aos_crypto migrate đổi sang v2.
encrypt()/decrypt() (v1, không DEK) giữ cho token ngắn hạn tự ký (2FA challenge/cookie); dữ liệu lưu DB dùng seal()/unseal()."""
import base64, hashlib, hmac, logging, os, re, struct, threading, time
from pathlib import Path
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, AESGCMSIV

log = logging.getLogger("aos.crypto")
VERSION = "v1"
KEY_FILE = Path(os.environ.get("AOS_SECRET_KEY_FILE") or Path(__file__).resolve().parent.parent / "var" / "aos_secret.key")
_RING: dict | None = None
_CUR: str | None = None
_LOCK = threading.RLock()
_KID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
ALG_GCM, ALG_SIV = 1, 2
ALG_NAME = {ALG_GCM: "aes-256-gcm", ALG_SIV: "aes-256-gcm-siv"}
PLATFORM_WS = 0
_DEKS: dict[int, tuple] = {}          # dek_id -> (key, ws, purpose)
_ACT: dict[tuple, tuple] = {}         # (ws, purpose) -> (dek_id, loaded_at)
ACT_TTL = 20.0                        # giây: tiến trình khác xoay DEK thì sau tối đa ngần này ta dùng DEK mới
_SCHEMA_OK = False

DDL = """
CREATE TABLE IF NOT EXISTS crypto_deks(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, purpose TEXT NOT NULL DEFAULT 'data', kid TEXT NOT NULL,
  alg SMALLINT NOT NULL DEFAULT 2, wrapped BYTEA, status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ NOT NULL DEFAULT now(), rotated_at TIMESTAMPTZ);
CREATE UNIQUE INDEX IF NOT EXISTS crypto_deks_active ON crypto_deks(workspace_id, purpose) WHERE status='active';
CREATE TABLE IF NOT EXISTS crypto_ws(workspace_id INT PRIMARY KEY, checkpoints BOOLEAN NOT NULL DEFAULT false, traces BOOLEAN NOT NULL DEFAULT false,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_by TEXT);
ALTER TABLE crypto_ws ADD COLUMN IF NOT EXISTS runs BOOLEAN NOT NULL DEFAULT false, ADD COLUMN IF NOT EXISTS datasets BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS attachments BOOLEAN NOT NULL DEFAULT false;   -- 9.P2: lg_runs/lg_run_events, ví dụ dataset, tệp đính kèm chat
"""

class CryptoError(ValueError): pass

# ------------------------------------------------------------------ KEK (khoá bọc)
def _parse(b64: str) -> bytes:
    try: k = base64.b64decode(b64.strip(), validate=True)
    except Exception as e: raise CryptoError("khoá không phải base64") from e
    if len(k) != 32: raise CryptoError("khoá phải là 32 byte (base64)")
    return k

def _kid(k: bytes) -> str: return hashlib.sha256(k).hexdigest()[:8]

def _load():
    global _RING, _CUR
    if _RING is not None: return
    with _LOCK:
        if _RING is not None: return
        ring: dict = {}; cur = None
        for part in filter(None, (p.strip() for p in os.environ.get("AOS_SECRET_KEYS", "").split(","))):
            kid, _, b64 = part.partition(":")
            if not _KID_RE.fullmatch(kid) or not b64: raise CryptoError("AOS_SECRET_KEYS phải có dạng kid:base64,kid2:base64")
            ring[kid] = _parse(b64); cur = cur or kid
        if cur:
            cur = os.environ.get("AOS_ACTIVE_KID", "").strip() or cur
            if cur not in ring: raise CryptoError("AOS_ACTIVE_KID không có trong AOS_SECRET_KEYS")
        raw = os.environ.get("AOS_SECRET_KEY", "").strip()
        if not raw and not cur:
            if os.environ.get("AOS_ENV", "").lower() in ("prod", "production"):
                raise CryptoError("Thiếu AOS_SECRET_KEY / AOS_SECRET_KEYS (bắt buộc ở môi trường prod)")
            if KEY_FILE.exists(): raw = KEY_FILE.read_text().strip()
            else:
                raw = base64.b64encode(os.urandom(32)).decode()
                KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f: f.write(raw)
                log.warning("AOS_SECRET_KEY chưa đặt: đã sinh khoá dev vào %s (chỉ dùng local; prod hãy đặt AOS_SECRET_KEYS trong .env)", KEY_FILE)
        if raw:
            k = _parse(raw); ring[_kid(k)] = k; cur = cur or _kid(k)
        for old in filter(None, (x.strip() for x in os.environ.get("AOS_SECRET_KEYS_OLD", "").split(","))):
            k = _parse(old); ring.setdefault(_kid(k), k)
        for k in list(ring.values()): ring.setdefault(_kid(k), k)      # bí danh sha8 để đọc mã v1 cũ khi khoá được đặt tên
        _DEKS.clear(); _ACT.clear()
        _RING, _CUR = ring, cur

def reset():
    """Quên KEK + cache DEK (mô phỏng khởi động lại tiến trình; dùng trong test/CLI)."""
    global _RING, _CUR
    with _LOCK: _RING = None; _CUR = None; _DEKS.clear(); _ACT.clear()

def key_id() -> str:
    _load(); return _CUR  # type: ignore[return-value]

# ------------------------------------------------------------------ v1 (KEK trực tiếp, cho token tự ký ngắn hạn + đọc dữ liệu cũ)
def encrypt(plain: str) -> str:
    _load(); nonce = os.urandom(12)
    ct = AESGCM(_RING[_CUR]).encrypt(nonce, plain.encode(), _CUR.encode())  # type: ignore[index]
    return f"{VERSION}:{_CUR}:" + base64.urlsafe_b64encode(nonce + ct).decode()

def decrypt(token: str) -> str:
    _load()
    try:
        ver, kid, blob = token.split(":", 2)
        if ver != VERSION: raise CryptoError("phiên bản mã hoá không hỗ trợ")
        if kid not in _RING: raise CryptoError("không có khoá cho kid " + kid)  # type: ignore[operator]
        raw = base64.urlsafe_b64decode(blob.encode())
        return AESGCM(_RING[kid]).decrypt(raw[:12], raw[12:], kid.encode()).decode()  # type: ignore[index]
    except CryptoError: raise
    except (InvalidTag, ValueError) as e:
        raise CryptoError("dữ liệu mã hoá bị hỏng hoặc sai khoá") from e

# ------------------------------------------------------------------ DEK (DB đồng bộ: chỉ chạm DB khi cache trượt)
def _conn():
    import psycopg
    uri = os.environ.get("PG_URI") or os.environ.get("AOS_PG_URI")
    if not uri: raise CryptoError("Thiếu PG_URI để đọc khoá dữ liệu (crypto_deks)")
    return psycopg.connect(uri, autocommit=True)

def ensure_schema():
    global _SCHEMA_OK
    if _SCHEMA_OK: return
    with _conn() as c, c.transaction():
        c.execute("SELECT pg_advisory_xact_lock(hashtext('aos_crypto_ddl'))")
        c.execute(DDL)
    _SCHEMA_OK = True

def _aead(alg: int, key: bytes):
    if alg == ALG_GCM: return AESGCM(key)
    if alg == ALG_SIV: return AESGCMSIV(key)
    raise CryptoError(f"thuật toán {alg} không hỗ trợ")

def default_alg() -> int:
    return ALG_GCM if os.environ.get("AOS_CRYPTO_ALG", "").lower() in ("gcm", "aes-gcm", "aes-256-gcm") else ALG_SIV

def _wrap_aad(dek_id: int, ws: int, purpose: str, kid: str) -> bytes: return f"aos2-dek|{dek_id}|{ws}|{purpose}|{kid}".encode()

def wrap_dek(dek: bytes, dek_id: int, ws: int, purpose: str, kid: str, alg: int) -> bytes:
    _load(); nonce = os.urandom(12)
    return nonce + _aead(alg, _RING[kid]).encrypt(nonce, dek, _wrap_aad(dek_id, ws, purpose, kid))  # type: ignore[index]

def unwrap_dek(row: dict) -> bytes:
    _load()
    if row["kid"] not in _RING: raise CryptoError(f"không có KEK '{row['kid']}' để mở khoá dữ liệu của workspace {row['workspace_id']}")  # type: ignore[operator]
    w = bytes(row["wrapped"])
    try: return _aead(row["alg"], _RING[row["kid"]]).decrypt(w[:12], w[12:], _wrap_aad(row["id"], row["workspace_id"], row["purpose"], row["kid"]))  # type: ignore[index]
    except InvalidTag as e: raise CryptoError("khoá dữ liệu bị hỏng hoặc sai KEK") from e

def _row_dict(cur, r) -> dict: return dict(zip([d.name for d in cur.description], r))

def _dek(dek_id: int) -> tuple:
    _load()
    hit = _DEKS.get(dek_id)
    if hit: return hit
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT * FROM crypto_deks WHERE id=%s", (dek_id,)); r = cur.fetchone()
        if not r: raise CryptoError(f"không tìm thấy khoá dữ liệu #{dek_id} (đã bị xoá?)")
        row = _row_dict(cur, r)
    if not row["wrapped"]: raise CryptoError(f"khoá dữ liệu #{dek_id} đã bị huỷ")
    v = (unwrap_dek(row), row["workspace_id"], row["purpose"]); _DEKS[dek_id] = v; return v

def _new_dek(c, ws: int, purpose: str) -> int:
    """Tạo DEK active mới trong transaction của c (c đang trong c.transaction())."""
    dek = os.urandom(32); alg = default_alg()
    dek_id = c.execute("INSERT INTO crypto_deks(workspace_id,purpose,kid,alg,status) VALUES(%s,%s,%s,%s,'new') RETURNING id", (ws, purpose, _CUR, alg)).fetchone()[0]
    c.execute("UPDATE crypto_deks SET wrapped=%s, status='active' WHERE id=%s", (wrap_dek(dek, dek_id, ws, purpose, _CUR, alg), dek_id))  # type: ignore[arg-type]
    _DEKS[dek_id] = (dek, ws, purpose); return dek_id

def active_dek(ws: int, purpose: str = "data") -> tuple[int, bytes]:
    _load(); ws = int(ws)
    hit = _ACT.get((ws, purpose))
    if hit and time.monotonic() - hit[1] < ACT_TTL: return hit[0], _DEKS[hit[0]][0]
    ensure_schema()
    for _ in range(3):
        with _conn() as c:
            r = c.execute("SELECT id FROM crypto_deks WHERE workspace_id=%s AND purpose=%s AND status='active'", (ws, purpose)).fetchone()
        if r:
            dek_id = r[0]; key = _dek(dek_id)[0]; break
        try:
            with _conn() as c, c.transaction(): dek_id = _new_dek(c, ws, purpose)
            key = _DEKS[dek_id][0]; break
        except Exception as e:  # noqa: BLE001  tiến trình khác vừa tạo (unique index) -> đọc lại
            if type(e).__name__ != "UniqueViolation": raise
    else: raise CryptoError("không tạo được khoá dữ liệu")
    _ACT[(ws, purpose)] = (dek_id, time.monotonic()); return dek_id, key

# ------------------------------------------------------------------ v2 seal / unseal
def _hdr(alg: int, dek_id: int) -> bytes: return bytes([2, alg]) + struct.pack(">I", dek_id)
def _aad(hdr: bytes, ctx: str) -> bytes: return hdr + b"|" + ctx.encode()

def seal_bytes(plain: bytes, ws: int, ctx: str) -> bytes:
    dek_id, key = active_dek(ws); alg = default_alg(); hdr = _hdr(alg, dek_id); nonce = os.urandom(12)
    return hdr + nonce + _aead(alg, key).encrypt(nonce, plain, _aad(hdr, ctx))

def header(blob: bytes) -> dict:
    if len(blob) < 6 + 12 + 16 or blob[0] != 2: raise CryptoError("không phải dữ liệu mã hoá v2")
    return {"ver": 2, "alg": blob[1], "dek_id": struct.unpack(">I", blob[2:6])[0]}

def unseal_bytes(blob: bytes, ws: int | None, ctx: str) -> bytes:
    h = header(blob); key, dws, purpose = _dek(h["dek_id"])
    if purpose != "data": raise CryptoError("khoá không dùng để mã hoá dữ liệu")
    if ws is not None and int(ws) != dws: raise CryptoError("dữ liệu mã hoá thuộc workspace khác")
    try: return _aead(h["alg"], key).decrypt(blob[6:18], blob[18:], _aad(blob[:6], ctx))
    except InvalidTag as e: raise CryptoError("dữ liệu mã hoá bị hỏng, sai khoá hoặc bị chuyển sang dòng/cột khác") from e

def seal(plain: str, ws: int, ctx: str) -> str:
    """Mã hoá một giá trị chuỗi cho cột DB. ctx = 'bảng.cột|<khoá dòng>' (gắn vào AAD)."""
    return "v2:" + base64.urlsafe_b64encode(seal_bytes(plain.encode(), ws, f"{ctx}|ws{int(ws)}")).decode()

def kind(tok: str | None) -> str:
    if not tok: return "empty"
    if tok.startswith("v2:"): return "v2"
    if tok.startswith("v1:"): return "v1"
    if tok.startswith("gAAAA"): return "fernet"
    return "plain"

def _fernet_open(tok: str) -> str:
    from cryptography.fernet import Fernet, MultiFernet, InvalidToken
    keys = [k for k in (os.environ.get("AOS_OAUTH_KEY"), os.environ.get("AOS_OAUTH_KEY_OLD")) if k]
    if not keys: raise CryptoError("token OAuth cũ (Fernet) nhưng thiếu AOS_OAUTH_KEY để đọc")
    try: return MultiFernet([Fernet(k) for k in keys]).decrypt(tok.encode()).decode()
    except InvalidToken as e: raise CryptoError("token OAuth cũ không giải mã được (đổi khoá?)") from e

def unseal(tok: str, ws: int, ctx: str) -> str:
    k = kind(tok)
    if k == "v2":
        try: raw = base64.urlsafe_b64decode(tok[3:].encode())
        except ValueError as e: raise CryptoError("dữ liệu mã hoá bị hỏng") from e
        return unseal_bytes(raw, ws, f"{ctx}|ws{int(ws)}").decode()
    if k == "v1": return decrypt(tok)            # dữ liệu GĐ6.5 chưa migrate
    if k == "fernet": return _fernet_open(tok)   # token OAuth GĐ8.5 chưa migrate
    raise CryptoError("giá trị không có định dạng mã hoá")

# ------------------------------------------------------------------ băm có khoá (mã dự phòng 2FA)
def mac(data: str, ws: int = PLATFORM_WS) -> str:
    """HMAC-SHA256 bằng khoá 'mac' riêng của workspace (bọc bởi KEK, không bị xoay theo DEK dữ liệu). Chống dò ngoại tuyến khi lộ DB."""
    _, key = active_dek(ws, "mac")
    return "m1:" + hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
