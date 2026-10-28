"""GĐ6.5 kho bí mật workspace: giá trị mã hoá AES-GCM (crypto.py), chỉ tham chiếu theo tên {{secret:NAME}}, chỉ được thay vào
request tới máy đích nằm trong allowed_hosts. Không route nào trả giá trị. Ghi = admin (admin.ADMIN_ONLY chặn editor/viewer/demo)."""
import re
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import crypto, aos_crypto

async def _q(*a, **k):   # nạp lười để test đơn vị không phải nạp cả deepagent_api
    import deepagent_api as A
    return await A.q(*a, **k)

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS ws_secrets(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, name TEXT NOT NULL, value_enc TEXT NOT NULL,
  allowed_hosts TEXT[] NOT NULL DEFAULT '{}', created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, name));
"""
NAME_RE = r"^[A-Za-z0-9_.\-]{1,64}$"
HOST_RE = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$")
REF = re.compile(r"\{\{\s*secret:([A-Za-z0-9_.\-]{1,64})\s*\}\}")
PUBLIC_COLS = "name, allowed_hosts, created_by, created_at, updated_at"

async def init(): await _q(DDL)

def seal_value(ws, name: str, plain: str) -> str:
    """GĐ9.7: mã hoá phong bì (DEK của workspace), AAD gắn (bảng.cột, workspace, tên) -> không đem sang bí mật khác được."""
    return crypto.seal(plain, int(ws), aos_crypto.ctx("ws_secrets.value_enc", name))

def open_value(tok: str, ws, name: str) -> str:
    return crypto.unseal(tok, int(ws), aos_crypto.ctx("ws_secrets.value_enc", name))   # v1/Fernet cũ vẫn đọc được tới khi migrate

def host_allowed(host: str | None, patterns) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h: return False
    for p in patterns or []:
        p = p.lower()
        if p.startswith("*."):
            if h.endswith(p[1:]) and len(h) > len(p) - 1: return True      # *.example.com: subdomain only, not example.com itself
        elif h == p: return True
    return False

async def resolve_secrets(text, dest_url: str, ws: int):
    """Thay {{secret:NAME}} trong text bằng giá trị đã giải mã, CHỈ khi host của dest_url khớp allowed_hosts (đúng hoặc *.suffix).
    Sai host -> PermissionError; không có bí mật -> LookupError. Không bao giờ log giá trị."""
    if not isinstance(text, str) or "{{" not in text: return text
    names = set(REF.findall(text))
    if not names: return text
    host = urlparse(dest_url or "").hostname
    vals = {}
    for n in names:
        r = await _q("SELECT value_enc, allowed_hosts FROM ws_secrets WHERE workspace_id=%s AND name=%s", (int(ws), n), one=True)
        if not r: raise LookupError(f"Không có bí mật {n}")
        if not host_allowed(host, r["allowed_hosts"]): raise PermissionError(f"Bí mật {n} không được phép gửi tới máy {host or dest_url!r}")
        vals[n] = open_value(r["value_enc"], ws, n)
    return REF.sub(lambda m: vals[m.group(1)], text)

async def get_secret(name: str, ws: int, dest_host: str) -> str:
    """GĐ8.1: decrypted value of one secret for a channel adapter, ONLY when dest_host matches its allowed_hosts.
    Missing -> LookupError; other host -> PermissionError. Never log the value."""
    r = await _q("SELECT value_enc, allowed_hosts FROM ws_secrets WHERE workspace_id=%s AND name=%s", (int(ws), name), one=True)
    if not r: raise LookupError(f"Không có bí mật {name}")
    if not host_allowed(dest_host, r["allowed_hosts"]): raise PermissionError(f"Bí mật {name} không được phép gửi tới máy {dest_host!r}")
    return open_value(r["value_enc"], ws, name)

async def resolve_headers(headers: dict | None, dest_url: str, ws: int):
    if not headers: return headers
    return {k: await resolve_secrets(v, dest_url, ws) if isinstance(v, str) else v for k, v in headers.items()}

class SecretIn(BaseModel):
    value: str = Field(min_length=1, max_length=8000)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=20)

def _hosts(hs):
    out = [h.strip().lower() for h in hs if h.strip()]
    if any(not HOST_RE.match(h) for h in out): raise HTTPException(422, "allowed_hosts: chỉ nhận tên máy (vd api.telegram.org) hoặc *.suffix")
    return out

def _admin():
    import admin; u = admin.cur()
    if not u or u.get("role") != "admin" or u.get("demo"): raise HTTPException(403, "Chỉ admin quản lý bí mật")
    return u

@router.get("/api/secrets")
async def list_secrets():
    import admin
    return await _q(f"SELECT {PUBLIC_COLS} FROM ws_secrets WHERE workspace_id=%s ORDER BY name", (admin.cur_ws(),))

@router.post("/api/secrets/{name}")
async def create_secret(name: str, b: SecretIn):
    u = _admin()
    if not re.match(NAME_RE, name): raise HTTPException(422, "Tên bí mật: chữ, số, _ . - (tối đa 64)")
    hs = _hosts(b.allowed_hosts)
    r = await _q(f"INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts,created_by) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING {PUBLIC_COLS}",
                  (u["ws"], name, seal_value(u["ws"], name, b.value), hs, u["username"]), one=True)
    if not r: raise HTTPException(409, "Bí mật đã tồn tại (dùng PUT để đổi)")
    return r

class SecretUpd(BaseModel):
    value: str | None = Field(default=None, min_length=1, max_length=8000)
    allowed_hosts: list[str] | None = Field(default=None, max_length=20)

@router.put("/api/secrets/{name}")
async def update_secret(name: str, b: SecretUpd):
    u = _admin()
    if b.value is None and b.allowed_hosts is None: raise HTTPException(422, "Cần value hoặc allowed_hosts")
    r = await _q(f"UPDATE ws_secrets SET value_enc=coalesce(%s,value_enc), allowed_hosts=coalesce(%s,allowed_hosts), updated_at=now() WHERE workspace_id=%s AND name=%s RETURNING {PUBLIC_COLS}",
                  (seal_value(u["ws"], name, b.value) if b.value is not None else None, _hosts(b.allowed_hosts) if b.allowed_hosts is not None else None, u["ws"], name), one=True)
    if not r: raise HTTPException(404, "Không có bí mật này")
    return r

@router.delete("/api/secrets/{name}")
async def delete_secret(name: str):
    u = _admin()
    r = await _q("DELETE FROM ws_secrets WHERE workspace_id=%s AND name=%s RETURNING 1", (u["ws"], name), one=True)
    if not r: raise HTTPException(404, "Không có bí mật này")
    return {"ok": True}
