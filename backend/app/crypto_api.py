"""GĐ9.7 API mã hoá dữ liệu: thẻ trạng thái (chỉ đọc) + bật/tắt mã hoá checkpoint/trace theo workspace. Chỉ admin; workspace demo luôn tắt.
Không route nào trả khoá hay bản rõ. Đổi khoá (KEK/DEK) làm bằng lệnh `python -m aos_crypto rotate` (có chủ đích, ngoài giao diện)."""
import asyncio, logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import aos_crypto, crypto

log = logging.getLogger("aos.crypto")
router = APIRouter(tags=["crypto"])
_JOBS: dict = {}   # ws -> kết quả/tiến độ migrate gần nhất

async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

async def init():
    await asyncio.to_thread(crypto.ensure_schema)    # trước RLS (admin.rls_init lọc theo workspace_id)

def _admin():
    import admin
    u = admin.cur()
    if not u or u.get("role") != "admin": raise HTTPException(403, "Chỉ admin xem/đổi cài đặt mã hoá")
    return u

def _view(st: dict, ws: int) -> dict:
    mine = [d for d in st["deks"] if d["workspace_id"] == ws and d["status"] != "destroyed"]
    fl = next((w for w in st["workspaces"] if w["workspace_id"] == ws), {g: False for g in aos_crypto.GROUPS})
    return {"active_kid": st["active_kid"], "alg_default": st["alg_default"], "deks_per_kek": st["deks_per_kek"], "kek_stale_deks": st["kek_stale_deks"],
            "legacy_rows": st["legacy_rows"], "columns": st["columns"], "checkpoints": st["checkpoints"], "traces": st["traces"],
            "payloads": st.get("payloads", {}), "workspace": {"workspace_id": ws, **{g: fl[g] for g in aos_crypto.GROUPS}, "deks": mine},
            "migrate": _JOBS.get(ws), "covered": ["Bí mật workspace + token kênh (Zalo/Slack/Telegram)", "Token OAuth", "Khoá TOTP + mã dự phòng 2 lớp", "Phiên Zalo cá nhân",
                                                    "Tin nhắn trong checkpoint (tuỳ chọn)", "Nội dung trace vào/ra (tuỳ chọn)",
                                                    "Lượt chạy (đầu vào/đầu ra/cấu hình) và sự kiện luồng (tuỳ chọn)", "Ví dụ trong bộ dữ liệu: đầu vào/đầu ra (tuỳ chọn)", "Tệp đính kèm chat: nội dung tệp + văn bản trích xuất (tuỳ chọn)"],
            "unsearchable": ["Tìm kiếm văn bản trong nội dung trace (ILIKE/SQL) với workspace bật mã hoá trace", "Xem trước rút gọn từ SQL hiện '[đã mã hoá]'"]}

@router.get("/api/crypto/status")
async def get_status():
    u = _admin()
    return _view(await asyncio.to_thread(aos_crypto.status), u["ws"])

class SettingsIn(BaseModel):
    checkpoints: bool | None = None
    traces: bool | None = None
    runs: bool | None = None
    datasets: bool | None = None
    attachments: bool | None = None

def _run_migrate(ws: int):
    _JOBS[ws] = {"state": "running"}
    try:
        r = aos_crypto.migrate(ws); _JOBS[ws] = {"state": "done" if r["done"] else "partial", "result": {k: v for k, v in r.items() if k != "done"}}
    except Exception as e:  # noqa: BLE001
        log.warning("migrate ws=%s lỗi: %s", ws, type(e).__name__); _JOBS[ws] = {"state": "error", "error": type(e).__name__}

@router.put("/api/crypto/settings")
async def put_settings(b: SettingsIn):
    u = _admin(); ws = u["ws"]
    if u.get("demo"): raise HTTPException(403, "Workspace demo không bật mã hoá")
    import admin
    if admin.is_demo(u): raise HTTPException(403, "Workspace demo không bật mã hoá")
    ws_row = await _q("SELECT demo FROM workspaces WHERE id=%s", (ws,), one=True)
    if ws_row and ws_row.get("demo"): raise HTTPException(403, "Workspace demo không bật mã hoá")
    ch = {g: getattr(b, g) for g in aos_crypto.GROUPS if getattr(b, g) is not None}
    if not ch: raise HTTPException(422, "Cần ít nhất một mục: " + ", ".join(aos_crypto.GROUPS))
    gs = list(aos_crypto.GROUPS)   # tên cột lấy từ danh sách cố định, không từ đầu vào người dùng
    await _q(f"INSERT INTO crypto_ws(workspace_id,{','.join(gs)},updated_by) VALUES(%s,{','.join('coalesce(%s,false)' for _ in gs)},%s) "
             f"ON CONFLICT(workspace_id) DO UPDATE SET {','.join(f'{g}=coalesce(%s,crypto_ws.{g})' for g in gs)}, updated_at=now(), updated_by=%s",
             (ws, *[ch.get(g) for g in gs], u["username"], *[ch.get(g) for g in gs], u["username"]))
    aos_crypto.invalidate_flags(); aos_crypto._PW.clear(); aos_crypto._RW.clear()
    if any(ch.values()): asyncio.get_running_loop().run_in_executor(None, _run_migrate, ws)   # mã hoá dữ liệu cũ nền, không chặn
    return _view(await asyncio.to_thread(aos_crypto.status), ws)
