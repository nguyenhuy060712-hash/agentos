#!/usr/bin/env bash
# Full point-in-time backup of Agent OS on VPS #83 -> /opt/agentos_backups/<name>_<ts> (mode 700/600), same layout as demo_baseline_20261021_1051.
# Usage: ops/backup_vps83.sh <name> [--pull]   (--pull copies it to /workspace/backups/vps83/<name>_<ts>, sha256-checked)
# Never prints env/key contents. Does not stop services (pg_dump -Fc is transactionally consistent).
set -euo pipefail
NAME=${1:?name}; PULL=${2:-}
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -i "$HOME/.ssh/moza_vps82" -p 30029 root@103.142.24.220)
TS=$(TZ=Asia/Bangkok date +%Y%m%d_%H%M); B=/opt/agentos_backups/${NAME}_$TS
"${SSH[@]}" bash -s "$B" "$TS" <<'R'
set -euo pipefail; B=$1; TS=$2; umask 077; mkdir -p "$B"; chmod 700 "$B"
tar -C /opt -czf "$B/agentos_code.tgz" --exclude='agentos/var/sandboxes' --exclude='agentos/sandboxes' --exclude='agentos/volumes' --exclude='agentos/snapshots' agentos
tar -C /opt -czf "$B/agentos-show.tgz" agentos-show 2>/dev/null || true
tar -C / -czf "$B/etc_caddy.tgz" etc/caddy $( [ -d /etc/caddy-show ] && echo etc/caddy-show )
tar -C / -czf "$B/systemd_units.tgz" etc/systemd/system/deepagent-api.service etc/systemd/system/deepagent-api.service.d etc/systemd/system/deepagent-worker.service
[ -d /opt/agentos/var/files ] && tar -C /opt/agentos/var -czf "$B/files.tgz" files || true
[ -d /opt/agentos/var/exports ] && tar -C /opt/agentos/var -czf "$B/exports.tgz" exports || true   # GĐ12.48a: Parquet exports (rebuildable from tr_runs, kept when present); the master key stays out of this tgz, see MANIFEST
install -m 600 /opt/langchain/.env "$B/langchain.env"
docker exec lc-postgres pg_dump -U langgraph -Fc langgraph > "$B/langgraph.dump"
n=$(docker exec -i lc-postgres pg_restore -l < "$B/langgraph.dump" | grep -c ' TABLE DATA ')
miss=""; for t in gw_providers ws_secrets connections oauth_providers oauth_connections oauth_states mcp_servers crypto_deks gw_policies; do [ "$(docker exec -i lc-postgres pg_restore -l < "$B/langgraph.dump" | grep -c " TABLE DATA public $t ")" -gt 0 ] || miss="$miss $t"; done   # 11.ZA2: part-A tables must be in the dump (absent = this DB predates that migration)
miss12=""; for t in notify_destinations notify_subscriptions ws_invites trace_retention tr_stats_hourly automation_applied alert_rules context_packs datasets example_revisions; do [ "$(docker exec -i lc-postgres pg_restore -l < "$B/langgraph.dump" | grep -c " TABLE DATA public $t ")" -gt 0 ] || miss12="$miss12 $t"; done   # 12.48a: part-A tables (sealed signing keys live in ws_secrets, invites hold only sha256 of the token, tr_runs.search_tsv is inside the tr_runs data and is rebuildable by fts.backfill)
miss13=""; for t in issue_traces issue_events engine_project_settings engine_runs insight_configs memory_proposals retention_settings tool_idem; do [ "$(docker exec -i lc-postgres pg_restore -l < "$B/langgraph.dump" | grep -c " TABLE DATA public $t ")" -gt 0 ] || miss13="$miss13 $t"; done   # 13.60: GĐ13 tables (agent memory files live in the LangGraph `store` table, checkpoints in checkpoints*; nothing of GĐ13 is stored outside Postgres)
{ echo "agentos HEAD $(git -C /opt/agentos log -1 --format='%h %s' | cut -c1-100)"; echo "pg_restore TOC TABLE DATA: $n"; echo "ts $TS (Asia/Bangkok)"; echo "part-A tables missing from dump:${miss:- none}"; echo "GD12 tables missing from dump:${miss12:- none}"; echo "GD13 tables missing from dump:${miss13:- none}"; [ -f /opt/agentos/var/aos_secret.key ] && echo "dev master key var/aos_secret.key is inside agentos_code.tgz" || echo "no var/aos_secret.key: master key lives in the env file (AOS_SECRET_KEYS), which is NOT in this backup - keep it elsewhere"; } > "$B/MANIFEST.txt"
cd "$B" && sha256sum *.tgz *.dump *.env MANIFEST.txt > SHA256SUMS && chmod 600 "$B"/*
echo "VPS backup $B ok ($(du -sh "$B" | cut -f1), TOC $n, GD13 tables missing:${miss13:- none})"
R
# 11.73: boxes (volumes without cache/), snapshot archives + images, skill trees, egress network definitions -> $B/computers (local script piped over ssh; a failure warns, never aborts the rest)
"${SSH[@]}" bash -s -- backup "$B/computers" < "$(dirname "$0")/backup_computers.sh" || echo "WARNING: computers backup failed (rest of the backup is fine)"
"${SSH[@]}" "cd $B && [ -d computers ] && find computers -type f -print0 | xargs -0 -r sha256sum >> SHA256SUMS; true"
if [ "$PULL" = "--pull" ]; then
  D=/workspace/backups/vps83/${NAME}_$TS; umask 077; mkdir -p "$D"
  "${SSH[@]}" "tar -C $B -cf - ." | tar -C "$D" -xf -
  (cd "$D" && sha256sum -c --quiet SHA256SUMS) && echo "pulled -> $D (sha256 ok)"
  n=$(pg_restore -l "$D/langgraph.dump" | grep -c ' TABLE DATA '); echo "local pg_restore TOC: $n"
fi
echo "BACKUP=$B"
