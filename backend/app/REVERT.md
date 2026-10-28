# REVERT — Moza Agent OS / web chat (#83) — cập nhật 19/10/2026

## A. Quay về bản chat+eval trước "Agent OS" (giữ web, Caddy, tunnel)
cd /opt/langchain/projects/ha_test
cp deepagent_api.py.bak-pre-os deepagent_api.py && rm -f ui.py gateway.py
systemctl restart deepagent-api
cp /root/backup-20261018-adk/deepagent-web.caddy.bak-pre-os /etc/caddy/conf.d/deepagent-web.caddy   # bỏ route /d/* (không basic auth)
caddy validate --config /etc/caddy/Caddyfile && systemctl restart caddy        # admin off -> restart (HA/ADK site chớp ~2s)
# (tuỳ chọn) xoá bảng Agent OS:
docker exec lc-postgres psql -U langgraph -d langgraph -c 'DROP TABLE IF EXISTS deployment_history, deployments, prompt_versions, prompts, gateway_usage, test_runs, test_cases, test_sets, kb_chunks, kb_docs, tool_settings, mcp_servers, agent_versions, agents CASCADE; ALTER TABLE eval_runs DROP COLUMN IF EXISTS agent_id, DROP COLUMN IF EXISTS agent_version, DROP COLUMN IF EXISTS trace, DROP COLUMN IF EXISTS test_run_id, DROP COLUMN IF EXISTS test_case_id, DROP COLUMN IF EXISTS spans, DROP COLUMN IF EXISTS models_used, DROP COLUMN IF EXISTS deployment;'
docker image rm python:3.12-alpine   # image sandbox (83.6 MB)

## B. Gỡ hẳn web (về deepagent-api thuần API)
systemctl disable --now cloudflared-deepagent && rm /etc/systemd/system/cloudflared-deepagent.service && systemctl daemon-reload
rm /etc/caddy/conf.d/deepagent-web.caddy && systemctl restart caddy
cd /opt/langchain/projects/ha_test && cp deepagent_api.py.bak-pre-ui deepagent_api.py && rm -f ui.py gateway.py && systemctl restart deepagent-api
docker exec lc-postgres psql -U langgraph -d langgraph -c 'DROP TABLE IF EXISTS eval_scores, eval_runs CASCADE;'   # + các bảng ở mục A
rm /root/deepagent_web.txt

## Ghi chú
- Mã nguồn trên box: /workspace/adk_ha_build/os/{app.py, ui.py, gateway.py, e2e*.py, ui_test.py}
- Secret: /workspace/secrets/deepagent_web.txt, /workspace/secrets/deepagent_deploy_test.txt (600), /root/deepagent_web.txt trên #83 (600)
- Gemini key trong /opt/langchain/.env (600). Trang Gateway có nút "Thay key" -> ghi vào file này.

## C. Gỡ P1/P2 (Observability + Evaluation, 19/10/2026) — repo /opt/agentos
Thay đổi: (1) systemd drop-in `/etc/systemd/system/deepagent-api.service.d/agentos.conf` (WorkingDirectory=/opt/agentos);
(2) Caddy `/etc/caddy/conf.d/deepagent-web.caddy` thêm đường không basic-auth `/ingest/*`, `/share/*` (xác thực trong app), body tối đa 5MB;
(3) `AGENTOS_SECRET` trong /opt/langchain/.env; (4) bảng Postgres mới trong DB langgraph (lc-postgres).
Gỡ:
```bash
# quay về code cũ ở ha_test
rm /etc/systemd/system/deepagent-api.service.d/agentos.conf && systemctl daemon-reload && systemctl restart deepagent-api
# Caddy về trước P1 (validate rồi restart; admin tắt nên không reload)
cp /root/backup-20261018-adk/deepagent-web.caddy.bak-pre-p1 /etc/caddy/conf.d/deepagent-web.caddy && caddy validate --config /etc/caddy/Caddyfile && systemctl restart caddy
# (tuỳ chọn) xoá bảng P1/P2
docker exec lc-postgres psql -U langgraph -d langgraph -c "DROP TABLE IF EXISTS tr_runs, feedback, projects, ingest_keys, alert_rules, alert_events, automation_rules, automation_log, shares, outbox, datasets, dataset_examples, example_revisions, dataset_tags, evaluators, experiments, exp_results, pairwise_runs, pairwise_results, annotation_queues, queue_items, eval_schedules CASCADE"
sed -i '/^AGENTOS_SECRET=/d' /opt/langchain/.env
```
Kiểm tra sau gỡ: `curl -s -o /dev/null -w %{http_code} 127.0.0.1:8092/` → 401; ADK :8091 → 401; HA không bị đụng.

## D. P3/P4 + vá gap P1/P2 (19/10/2026)
File mới/sửa trong /opt/agentos: studio.py, ui_studio.py (mới); app.py, obs.py, evals.py, gateway.py, ui.py, ui_obs.py, sdk/agentos_sdk.py, sdk/agentos_pytest.py, tests/p34_test.py, tests/shots_p34.py.
Revert code: `cd /opt/agentos && git revert --no-edit HEAD` (hoặc `git checkout b6d9835 -- .`) rồi `systemctl restart deepagent-api`.
Bảng mới (xoá nếu muốn gỡ sạch, KHÔNG ảnh hưởng HA/Odoo):
```
docker exec lc-postgres psql -U langgraph -d langgraph -c "DROP TABLE IF EXISTS hook_calls, agent_hooks, agent_crons, approvals, hub_webhooks, prompt_tags, simulations, run_attachments, saved_filters, redaction_rules CASCADE; ALTER TABLE prompt_versions DROP COLUMN IF EXISTS parent_version, DROP COLUMN IF EXISTS output_schema; ALTER TABLE experiments DROP COLUMN IF EXISTS summary_scores;"
```
Lưu ý: cron agent test đã tắt (enabled=false). Caddy không đổi trong bước này.

## E. P5 Agent Server + P6 Gateway + P7 Admin + gap cuối (19/10/2026)
Backup trước khi đổi: `/root/backup-20261019-p567/` (deepagent-web.caddy, env.bak (600), git_head, langgraph.dump = pg_dump -Fc toàn DB langgraph).
Thay đổi:
1. Code mới: server.py (P5 /api/v1), gw_api.py (P6 /v1 OpenAI-compatible), admin.py (P7 đăng nhập/RBAC/workspace/API key/audit/sandbox), ui_admin.py; sửa app.py, obs.py (OTLP protobuf, gom cụm ngữ nghĩa), evals.py (attachment example), gateway.py, ui.py, ui_obs.py.
2. Caddy `/etc/caddy/conf.d/deepagent-web.caddy`: BỎ basic_auth ở handle chính (xác thực chuyển vào app, mặc định chặn — test quét 237 route ẩn danh đều 401/303); thêm `/v1/*` vào nhóm public (virtual key). TLS (`moza-tls.caddy`) KHÔNG đổi.
3. `/opt/langchain/.env`: thêm `AGENTOS_OWNER_PW_HASH` (scrypt của mật khẩu hiện tại) → app tự tạo/giữ user `moza` superadmin.
4. Thư mục sandbox bền: `/opt/agentos/sandboxes/` (uid 65534, ≤50MB/sandbox, TTL).
5. Sửa lỗi có sẵn: `aembed` — gemini-embedding-2 gộp cả list thành 1 vector; nay nhúng từng văn bản.
Gỡ:
```bash
cp /root/backup-20261019-p567/deepagent-web.caddy /etc/caddy/conf.d/deepagent-web.caddy && caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl restart caddy   # khôi phục basic auth Caddy
cd /opt/agentos && git revert --no-edit HEAD && systemctl restart deepagent-api
sed -i '/^AGENTOS_OWNER_PW_HASH=/d' /opt/langchain/.env
# (tuỳ chọn) xoá dữ liệu mới
docker exec lc-postgres psql -U langgraph -d langgraph -c "DROP TABLE IF EXISTS lg_run_events, lg_runs, lg_crons, lg_threads, lg_assistant_versions, lg_assistants, gw_cache, gw_tool_sigs, gw_vkeys, audit_log, sessions, user_api_keys, memberships, sandboxes, example_attachments CASCADE; DROP TABLE IF EXISTS users, workspaces CASCADE; ALTER TABLE agents DROP COLUMN IF EXISTS workspace_id; ALTER TABLE projects DROP COLUMN IF EXISTS workspace_id; ALTER TABLE datasets DROP COLUMN IF EXISTS workspace_id; ALTER TABLE gateway_usage DROP COLUMN IF EXISTS vkey_id, DROP COLUMN IF EXISTS cached;"
rm -rf /opt/agentos/sandboxes
```
Khôi phục toàn DB nếu cần: `docker exec -i lc-postgres pg_restore -U langgraph -d langgraph --clean < /root/backup-20261019-p567/langgraph.dump`.
Dữ liệu test đã vô hiệu: 9 virtual key test (enabled=false), 2 user test editor*/viewer* (disabled, phiên + key không dùng được), cron test đã xoá.
