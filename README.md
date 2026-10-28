# Agent OS — mã nguồn bản GĐ10–13 (28/10/2026)

Bản chụp sạch (không kèm lịch sử cũ) của 3 phần đang dùng, đủ để dựng lại trên một VPS:

| Thư mục | Là gì | Nguồn |
|---|---|---|
| `backend/` | Máy chủ Agent OS (FastAPI + LangChain/LangGraph/Deep Agents), worker, giao diện quản trị, migration dữ liệu, máy tính ảo (gVisor), script vận hành, bộ kiểm thử | `agentos-local` nhánh `dev`, tag `gd13-done` + 1 commit sửa test |
| `chat/` | Agent OS Chat (React 19 + Vite 8 + assistant-ui), phục vụ tại `/chat/` | `agentos-chat` nhánh `dev` |
| `web/` | Website giới thiệu (Astro 7 + Tailwind 4), VI + `/en/`, có mục "Có gì mới" GĐ10–13, khoá học `/hoc/`, đào tạo `/dao-tao/` | `agentos-show` nhánh `dev` |

**Không có trong kho (cố ý):** mật khẩu, khoá API, file `.env`, khoá SSH; tài liệu so sánh/thiết kế/báo cáo cũ, ảnh nghiệm thu; các script kiểm thử cũ chạy thẳng vào VPS; tài liệu giảng viên; `node_modules`, `dist`. Bí mật nằm ở thư mục bí mật trên máy anh Huy, **không bao giờ commit**.

## Dữ liệu demo
- Tài khoản demo khách hàng được dựng bằng mã: `backend/app/demo_ws.py` (workspace riêng `demo=true`, chỉ Gemini, trần $2/ngày, agent được nhân bản, bộ CRM/quảng cáo mẫu hư cấu). Gọi `POST /api/admin/demo-workspace` bằng tài khoản admin — gọi lại nhiều lần vẫn an toàn.
- Rà rò rỉ sang tài khoản demo: `backend/tests/tools/demo_leak_audit.py <BASE> <admin> <demo>` (mật khẩu đưa qua biến môi trường `AUDIT_OWNER_PW`, `AUDIT_DEMO_PW`). Phải ra 0 rò trước khi mở cho khách.
- Dữ liệu dùng khi kiểm thử: `backend/tests/data/`, `backend/tests/fixtures/` (câu trả lời Gemini thật đã ghi lại để phát lại miễn phí), `backend/overlay/seed_local_fixtures.py`.
- Website: nội dung nằm trong `web/src/data/*.ts`, ảnh trong `web/src/assets/`, PDF đào tạo trong `web/public/dao-tao/`.

## Yêu cầu
- Ubuntu 24.04, Python 3.12, PostgreSQL 16/17 + pgvector, Docker + gVisor (`runsc`) cho máy tính ảo, Node 24.22, Caddy, LibreOffice (đọc tài liệu cũ).
- Thư viện Python ghim đúng phiên bản: `backend/requirements.lock` (cài bằng `uv pip sync`). Chat/web: `npm ci` theo `package-lock.json`.

## Triển khai lên VPS (tóm tắt — chi tiết trong `backend/DEPLOY_RUNBOOK_vps83.md`)
1. **Sao lưu trước** (code + DB + env): `backend/ops/backup_vps83.sh pre_gd13 --pull`.
2. Code backend: nội dung `backend/app/` + `backend/requirements.lock` + `backend/ops/` → `/opt/agentos` (dịch vụ `deepagent-api`, `deepagent-worker`).
3. Biến môi trường: chép `backend/.env.example` thành file env của systemd (quyền 600), điền giá trị thật. Không commit.
4. Migration chạy tự động khi khởi động (`backend/app/migrations/`, bảng `schema_migrations`). Xem trạng thái: `python ops/migrate.py status`. **Nên chạy thử các migration GĐ13 (0200–0230) trên bản sao DB trước.**
5. Máy tính ảo: build ảnh từ `backend/sandbox/` (Dockerfile.computer, Dockerfile.py-data, egress proxy).
6. Chat: `cd chat && npm ci && npm run build && ./scripts/check-a7.sh` → chép `dist/` vào thư mục `AOS_CHAT_DIST`.
7. Website: `cd web && npm ci && SITE_URL=<địa chỉ web> PUBLIC_AGENTOS_URL=<...> PUBLIC_CHAT_URL=<.../chat/> npm run build` → chép `dist/` vào `/opt/agentos-show` (Caddy tĩnh, không cần khởi động lại).
8. Kiểm tra: `/health`, đăng nhập, `/chat/`, rà rò rỉ demo (mục trên). Quay lại bản cũ: mục 7 của runbook.

## Chạy thử trên máy (không tốn tiền AI)
`backend/overlay/run_local.py` dựng máy chủ với AI giả trên `127.0.0.1:8020` (cần Postgres cục bộ). Bộ kiểm thử: `bash backend/tests/run_all.sh`.

## Việc nên làm trước khi giao khách trả tiền
Nhờ chuyên gia bảo mật xem: sàn chặn mạng, cách ly workspace, máy tính ảo, OAuth, thư tự kích hoạt agent, Engine chạy lại hội thoại, agent tự đặt lịch, chống gửi trùng. Nhờ luật sư xem phần dữ liệu cá nhân.
