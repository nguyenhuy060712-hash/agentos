# Runbook deploy Agent OS + /chat lên VPS #83

Trạng thái: **đã soạn, CHƯA chạy.** Anh duyệt lúc 20/10/2026 19:22 (giờ Asia/Saigon, UTC+7), với điều kiện chỉ chạy sau khi phần việc local đã xong và cả bộ test xanh. Điều kiện này đã đạt lúc ~19:55 ICT: pytest 35 passed / 2 skipped, vitest 25/25, E2E 60/60, A7 PASS, leak scan 0. Em (Grok Bot) **không tự deploy**. Người vận hành chạy từng bước dưới đây trên VPS #83.

Ràng buộc anh đã chốt:
- Tắt mock OAuth.
- Giữ key Gemini hiện tại (chỉ nằm trong env của server, mode 600).
- Ngân sách test với model thật tối đa **$2 tổng**. gpt-live-1 vẫn bị chặn (provider `openai` luôn bị loại khỏi REAL_PROVIDERS).
- Không có domain: dùng **Cloudflare quick tunnel + Caddy auth**.

Quy ước (kiểm lại tên thật trên VPS trước khi chạy):
```
APP=/opt/agentos            # thư mục cài đặt (app/, var/, chat/dist)
SVC=agentos                 # systemd unit
DB=agentos                  # tên database Postgres
D=$(TZ=Asia/Ho_Chi_Minh date +%Y%m%d_%H%M)
BK=/opt/agentos_backups/pre_$D
SRV=efffe92..26fce92        # commit server (agentos-local)
UI=b1107ac                  # commit UI (agentos-chat)
```

## 0. Trước khi bắt đầu (trên box)
1. Hai repo phải sạch: `git -C /workspace/agentos-local status`, `git -C /workspace/agentos-chat status`.
2. Tạo gói patch:
   - `git -C /workspace/agentos-local format-patch efffe92..26fce92 -o /workspace/handoff/patches/server`
   - `git -C /workspace/agentos-chat archive b1107ac | gzip > /workspace/handoff/agentos-chat-b1107ac.tar.gz`
3. Bỏ ra ngoài (không port): `overlay/fake_llm.py`, `overlay/run_local.py`, override `PG_URI` cục bộ, `scripts/a8*.mjs` (công cụ test).

## 1. Backup → `/opt/agentos_backups/pre_<date>`
```
sudo mkdir -p $BK && sudo chmod 700 $BK
sudo systemctl stop $SVC                                    # thời gian dừng ngắn, để bản backup nhất quán
sudo tar -C / -czf $BK/agentos_app.tgz ${APP#/}             # app/, var/ (voice store, AOS_FILES_DIR), chat/dist
sudo -u postgres pg_dump -Fc $DB > $BK/$DB.dump
sudo cp -a /etc/systemd/system/$SVC.service /etc/caddy/Caddyfile $BK/ 2>/dev/null
sudo sha256sum $BK/* | sudo tee $BK/SHA256SUMS
sudo -u postgres pg_restore -l $BK/$DB.dump | head          # kiểm bản dump đọc được
```
Không bao giờ sao file env chứa key vào chỗ đọc được chung. Bản backup giữ mode 700.

## 2. Git commit (trên VPS, để có điểm rollback)
```
cd $APP && git status && git add -A && git commit -m "pre-deploy snapshot $D" || true
git tag pre_$D
```
Nếu `$APP` chưa phải repo git: `git init && git add -A && git commit -m "baseline = VPS #83 $D"` (khớp với `efffe92`).

## 3. Áp patch + migration
1. `git am /path/patches/server/*.patch`. Nếu xung đột thì dừng, `git am --abort`, rồi báo lại.
2. Port voice API thành module của app, theo bước 3 trong `PATCHES_for_vps83.md`: `overlay/chat_overlay.py` → `app/voice_api.py` + `app.include_router(...)`. `app.py` và `deepagent_api.py` phải giữ đồng bộ.
3. Migration (idempotent, tự chạy khi boot): `chat_files` + RLS, cột `lg_runs.stream_subgraphs`, voice store schema 1→2. Thread metadata thêm khoá `voice`, `title_auto`, không đổi schema.
4. Thư mục file: `sudo install -d -o agentos -g agentos -m 750 $APP/var/files`, rồi đặt `AOS_FILES_DIR=$APP/var/files`. Thư mục này đã nằm trong tar ở bước 1.
5. Caddy: request body tối đa ≥ 6 MB trên `/api/files`.
6. Hồ sơ agent (dữ liệu, không bắt buộc): PATCH metadata `role/group/greeting/suggestions` cho từng assistant (xem `scripts/seed-profiles.mjs`).

## 3b. Python venv từ file khoá (GĐ9.P)
`requirements.lock` (cạnh file này) ghim đúng từng phiên bản của venv đã test cục bộ (Python 3.12.3, giống VPS); `requirements.in` là danh sách thư viện chính kèm chú thích; `requirements-dev.in` chỉ dành cho test (pytest, moto) — KHÔNG cài lên VPS.
```bash
# dựng venv mới giống hệt máy test (không đụng venv đang chạy; đổi tên thư mục khi chuyển):
uv venv /opt/langchain/venv_new --python 3.12
uv pip sync --python /opt/langchain/venv_new/bin/python /opt/agentos/requirements.lock
uv pip check --python /opt/langchain/venv_new/bin/python          # phải báo "All installed packages are compatible"
# thêm/đổi thư viện: sửa requirements.in, cài thử ở máy test, rồi tạo lại khoá:
uv pip freeze --python ~/.venvs/agentos/bin/python | grep -v -E '^(pytest|pluggy|iniconfig)==' > /tmp/new.lock   # xoá các gói dev (moto + phụ thuộc) nếu có
```
Chưa áp lên VPS: việc đổi venv trên #83 chỉ làm khi anh yêu cầu deploy (backup trước, xem mục 1).

## 4. Build /chat
```
tar -xzf agentos-chat-b1107ac.tar.gz -C /tmp/chat && cd /tmp/chat
npm ci && npm run build && ./scripts/check-a7.sh            # A7 phải PASS: không có key trong bundle, không có Assistant Cloud/pipecat/livekit
sudo rsync -a --delete dist/ $APP/chat/dist/                # AOS_CHAT_DIST trỏ vào đây
```

## 5. Env: tắt mock OAuth, giữ key Gemini, giới hạn $2
Trong EnvironmentFile của systemd (vd `/etc/agentos/agentos.env`, `root:root 600`):
```
# AOS_MOCK_OAUTH            -> XOÁ dòng này (hoặc để khác "1"). /mock-oauth/* sẽ trả 404
AOS_VOICE_REAL_PROVIDERS=google        # chỉ Gemini; openai luôn bị loại => gpt-live-1 vẫn bị chặn
AOS_A8_ABORT_USD=2.00                  # từ chối phiên thật mới khi ước tính cộng dồn đạt $2
AOS_GEMINI_LIVE_KEY=<giữ nguyên key hiện tại, không in ra, không commit>
AOS_FILES_DIR=/opt/agentos/var/files
```
Sau đó vào Settings > Ngân sách: `daily_budget_usd ≤ 2`, `max_session_min` nhỏ (vd 2) trong lúc test thật.
```
sudo systemctl daemon-reload && sudo systemctl start $SVC && sudo journalctl -u $SVC -n 50 --no-pager | grep -v -i "AQ\."
```

## 6. Kiểm tra (không có domain: Cloudflare quick tunnel + Caddy auth)
1. Tunnel: `cloudflared tunnel --url http://127.0.0.1:<cổng Caddy>` cho ra URL `https://<random>.trycloudflare.com`. URL này đổi mỗi lần tunnel khởi động lại, nên báo anh URL mới mỗi lần. Caddy giữ `basic_auth` (hoặc forward_auth) trước toàn bộ site. Không mở cổng app ra ngoài.
2. Mock OAuth đã tắt:
   - `curl -s -o /dev/null -w '%{http_code}' -u <caddy> https://<tunnel>/mock-oauth/authorize` phải trả **404**.
   - Danh sách Connections trong Settings không còn provider "mock".
3. Đăng nhập thật: qua tunnel → Caddy auth → `/login` bằng tài khoản admin thật → `/chat/` hiện sidebar, agent và chat. Kiểm thêm:
   - đăng xuất (menu user) quay về `/login`;
   - cookie phiên có cờ `Secure` và `HttpOnly`;
   - nếu đã cấu hình OAuth thật (`AOS_OAUTH_KEY` + client của provider), kết nối 1 provider thật và gọi 1 tool đọc.
4. CSP: `curl -sI -u <caddy> https://<tunnel>/chat/ | grep -i content-security-policy` phải đúng `connect-src 'self' wss://generativelanguage.googleapis.com;`. Console trình duyệt không có lỗi CSP.
5. Smoke (mock LLM, $0): gửi một tin text có stream; duyệt HITL; Stop; đính kèm ảnh; Mic đọc để nhập (chèn chữ, không gửi); Voice mode (overlay, ngắt lời, kết thúc); thread hiện tên theo câu nói đầu tiên; Settings đủ 5 tab; chế độ tối; tiếng Việt.
6. Model thật (≤ $2 tổng, chỉ gemini-3.8-live): 1–3 phiên ngắn (≤ 1 phút), gồm 1 cuộc gọi, 1 lần đọc để nhập, 1 lần đọc to. Sau mỗi phiên kiểm số liệu ước tính ở Settings > Ngân sách, rồi dừng khi gần $2.
7. Các bộ test có sẵn:
   - Trên box (cùng commit), chạy lại trước khi deploy: `pytest -q tests` (35 passed / 2 skipped), `npm run build && npx vitest run && node scripts/e2e.mjs && ./scripts/check-a7.sh`.
   - Trên VPS: `pytest tests/test_fixes.py tests/test_fixes2.py tests/test_redesign.py` chạy với **bản staging** (DB copy từ dump), không chạy với DB production.
   - Chạy leak scan trên `dist/` và log: `KEY_SHA256=… python3 scripts/leakscan.py $APP/chat/dist /var/log/...` phải ra 0.

## 6b. GĐ10 (26/10/2026): biến môi trường mới và migration

Thêm vào EnvironmentFile (`/etc/agentos/agentos.env`) chỉ khi cần. Không có cái nào bắt buộc:
```
AOS_EGRESS_ALLOW=<địa chỉ nội bộ được phép gọi ra, vd 10.0.0.5 hoặc 10.0.0.0/24; để trống = chỉ Internet>
AOS_EGRESS_DENY_HOSTS=<tên máy hoặc IP bị chặn tuyệt đối; thắng AOS_EGRESS_ALLOW>
AOS_SELF_IPS=<IP public của chính VPS, để chặn tự gọi chính mình; hệ thống tự dò thêm IP máy>
AOS_SCHEDULES_DISABLED=1               # công tắc tắt mọi lịch chạy; bỏ dòng này để bật lại
```
- Migration: `app/migrations/` là code (có trong git, đi theo bản deploy). Boot tự chạy các file mới, ghi bảng `schema_migrations`. Xem trạng thái: `python ops/migrate.py status` (chạy trong `$APP`, cần `PG_URI`). Không chạy `down` trên VPS nếu chưa được anh đồng ý.
- Backup: `ops/backup_vps83.sh` không sao lưu `/etc/agentos/agentos.env` (nó chỉ sao lưu `/opt/langchain/.env`, thư mục code `/opt/agentos` và pg_dump). Vì vậy 4 biến trên phải được ghi lại ở nơi an toàn riêng (không ghi giá trị vào git). Thư mục `app/migrations/` nằm trong code nên đã được sao lưu. Không sửa script.
- `ops/fix_gateway_ws.py` (sửa dữ liệu workspace): chỉ chạy dry-run mặc định; không chạy trên VPS khi chưa có quyết định của anh.

## 7. Rollback
```
sudo systemctl stop $SVC
cd $APP && git reset --hard pre_$D                        # code app/ (hoặc: sudo tar -C / -xzf $BK/agentos_app.tgz)
sudo rsync -a --delete <dist cũ từ tar> $APP/chat/dist/   # UI cũ: phải rollback CÙNG server (UI mới + server cũ = 422 ở Mic/Đọc to)
# DB chỉ khi cần (migration đều additive, cột/bảng mới để lại được):
sudo -u postgres pg_restore --clean --if-exists -d $DB $BK/$DB.dump
sudo cp $BK/agentos.service /etc/systemd/system/ && sudo cp $BK/Caddyfile /etc/caddy/ && sudo systemctl daemon-reload
sudo systemctl start $SVC && curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<cổng>/login   # 200
```
Rollback theo từng phần: `git revert 26fce92` (redesign server) cùng UI `3965008`, hoặc revert từng fix theo bảng commit trong `PATCHES_for_vps83.md`. Nếu chỉ lỗi phần voice thật: bỏ `AOS_VOICE_REAL_PROVIDERS` rồi restart, voice quay về mock và chat vẫn chạy.

## 8. Sao lưu hộp máy ảo, kỹ năng và trạng thái phần A (GĐ11.73 + 11.ZA2) — diễn tập 26/10/2026
**Script (chưa chạy trên VPS, chỉ khi anh bảo deploy):** `ops/backup_vps83.sh` giờ có thêm khối `computers/` trong thư mục backup (chạy `ops/backup_computers.sh backup <thư mục>` qua ssh; lỗi khối này chỉ cảnh báo, không làm hỏng phần còn lại; checksum nằm trong `SHA256SUMS`). Dòng `MANIFEST.txt` thêm: bảng phần A nào thiếu trong dump, và khoá chủ nằm ở đâu.

**Có sao lưu:**
- Mỗi hộp (volume Docker có nhãn `aos.box=1`, tên `aos-box-<16 hex>-workspace|opt`): `/workspace` và `/opt/aos-user` (không có `cache/`), kèm bảng sha256 từng file. Chỉ volume khớp nhãn VÀ tên đó; Odoo / Home Assistant / Hermes không bao giờ bị đọc.
- Bản chụp: file `var/snapshots/*` và ảnh Docker `aos-snap-*`, `aos-ws*-agent-*--setup` (và kiểu cũ `agent-*--setup`) (`docker save`); cây kỹ năng `var/skills/` (cũng đã nằm trong `agentos_code.tgz`); định nghĩa mạng `aos-egress-out`, `aos-sbx-egress-*` (chỉ để tham khảo, app tự dựng lại từ `host_state`).
- Postgres (`pg_dump -Fc`, đã có sẵn): `gw_providers, ws_secrets, connections, oauth_providers, oauth_connections, oauth_states, mcp_servers (cả cột tools_cache), gw_policies, gw_fallback_chains, gw_model_access, gw_vkeys, model_*, channel_bindings, mail_cursors, mail_seen, sandboxes, sandbox_snapshots, crypto_deks`, chính sách egress của workspace.

**Không sao lưu (cố ý):** RAM và tiến trình đang chạy của hộp; nhật ký quá 30 ngày (app tự cắt); `tool_runner` (dựng lại từ ảnh); container proxy egress (app tạo lại); bộ nhớ đệm trong tiến trình (cache DEK, cache CRED, cache công cụ MCP — tự nạp lại). **Không có CA hay khoá của proxy** vì GĐ11 chỉ chuyển tiếp TLS (passthrough), nên không có gì để sao lưu ở đó.

**Khoá chủ (rất quan trọng):** mọi bí mật (khoá nhà cung cấp, token OAuth/kết nối, secret của agent) được niêm phong bằng khoá chủ; khoá dữ liệu (DEK) nằm trong bảng `crypto_deks` của dump, nhưng chỉ mở được bằng khoá chủ. **Mất khoá chủ = mất toàn bộ bí mật, dump vô dụng với chúng.** Dev: file `var/aos_secret.key` (nằm trong `agentos_code.tgz`). Prod: `AOS_SECRET_KEYS` trong `/etc/agentos/agentos.env`, file này KHÔNG nằm trong backup (cố ý, để lộ backup không lộ khoá): anh cất riêng một bản ở trình quản lý mật khẩu.

**Khôi phục một hộp (trên máy dev hoặc VPS khi cần):** `ops/backup_computers.sh restore <thư mục backup>/computers [--prefix aosdrill-]`. Volume luôn tạo MỚI (đã tồn tại thì dừng, không ghi đè), giải nén rồi so sha256 từng file; `--prefix` tạo bản thử bỏ đi nhãn `aos.drill=1`. Dữ liệu CSDL: `pg_restore` vào một DB mới, kiểm tra xong mới đổi (xem `pg_restore.sh --into`).

**Diễn tập trên máy này (không đụng VPS):** `bash ops/backup_drill_local.sh` (trong WSL). Tạo một hộp giả + ảnh bản chụp giả + dòng đánh dấu (khoá nhà cung cấp, client secret, access/refresh token, secret của kết nối), sao lưu vào `var/backup_drill/<giờ>/` (kèm bản sao khoá chủ, mode 600), xoá hộp giả, khôi phục vào DB `aos_drill_<giờ>` + volume bỏ đi `aosdrill<giờ>-aos-box-*`, rồi kiểm: checksum từng file, hộp khôi phục chạy được và thấy file + gói, file bản chụp và cây kỹ năng giống hệt, ảnh cùng id, số dòng 23 bảng khớp, mọi ô mã hoá mở được bằng khoá chủ đã khôi phục, và khoá sai thì không mở được gì. Dọn hết (giữ 3 thư mục backup mới nhất). Kết quả ghi `RESULT.txt`.
Lần chạy 26/10/2026 (3 hộp thật + 1 hộp giả): đạt tất cả. Backup 62 MiB (dump 49,5 MiB, hộp 13,1 MiB), sao lưu 22–26 giây, khôi phục 29–30 giây, tổng 62–68 giây; 2033 file khớp checksum, 5/5 dòng đánh dấu giải mã được, 60 ô mã hoá mở được, 0 lỗi. Test: `tests/test_gd11_backup.py` (5 test nhanh dùng docker giả + 1 test chạy diễn tập thật khi đặt `AOS_REAL_SANDBOX=1`).

## 9. Sao lưu trạng thái mới của GĐ12 (12.48a) — diễn tập 26/10/2026
**Script (chưa chạy trên VPS, chỉ khi anh bảo deploy):** `ops/backup_vps83.sh` thêm (a) `exports.tgz` nếu có thư mục `var/exports/` (Parquet xuất ra; dựng lại được từ `tr_runs`), (b) kiểm sau khi dump: các bảng `notify_destinations notify_subscriptions ws_invites trace_retention tr_stats_hourly automation_applied alert_rules context_packs datasets example_revisions` phải có trong dump; dòng `GD12 tables missing from dump:` trong `MANIFEST.txt` (thiếu = DB chưa chạy migration đó). Mọi thứ khác đã nằm trong `pg_dump` sẵn có.

**Nằm ở đâu trong dump:** khoá ký webhook của kênh thông báo = dòng `ws_secrets` (đã niêm phong, `notify_destinations.signing_secret_ref` trỏ tới nó; mở được chỉ bằng khoá chủ như mọi bí mật). Lời mời = `ws_invites.token_hash` (chỉ sha256, token thật không lưu, nên sao lưu không làm lộ link). Kỹ năng/mẫu = `context_packs` (cột `kind`). Luật cảnh báo = `alert_rules`, bậc lưu trữ = `trace_retention` + cột trên `workspaces/projects`, thống kê giờ = `tr_stats_hourly`, bộ dữ liệu = `datasets/dataset_examples/example_revisions`.

**Chỉ mục tìm kiếm trace (`tr_runs.search_tsv`):** nằm sẵn trong dữ liệu `tr_runs` của dump (cột), nên khôi phục là tìm được ngay. Nếu cột trống (dump cũ) thì `fts.backfill` tự dựng lại từ nội dung trace, nên KHÔNG cần sao lưu riêng. Lưu ý: với workspace bật mã hoá trace, chỉ mục dùng khoá chủ (mục đích `mac`); mất khoá chủ thì không dựng lại được, giống mọi bí mật khác.

**Diễn tập trên máy này:** `bash ops/backup_drill_local.sh` (WSL) giờ tạo thêm: kênh webhook có khoá ký niêm phong, lời mời, kỹ năng + mẫu, luật cảnh báo, bậc lưu trữ, dòng thống kê giờ, bộ dữ liệu, một run có chỉ mục tìm kiếm; sau khi khôi phục vào DB tạm + khoá chủ đã khôi phục, kiểm: chữ ký webhook tính lại giống hệt, lời mời vẫn hợp lệ (người mời vẫn là admin, token thật không nằm trong DB), mọi dòng còn đủ, tìm được run, migration 0100–0190 có đủ; khoá sai thì không ký lại được. Kết quả đo 26/10: sao lưu 33 giây, khôi phục 46 giây, tổng 93 giây; thư mục backup 80 MiB (pg dump 62 MB, 173 bảng có dữ liệu; hộp 22 MB); 5 dòng đánh dấu giải mã được, 74 ô mã hoá mở được, 0 lỗi, 38 bảng khớp số dòng. DB tạm và volume tạm được xoá sau khi chạy. Test: `tests/test_gd12_backup.py`.
