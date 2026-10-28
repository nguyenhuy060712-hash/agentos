#!/usr/bin/env bash
# GĐ4 off-VPS backup (no paid storage): pull the newest nightly Agent OS backup of VPS #83 onto the box.
#   - newest pg_dump      /opt/agentos_backups/pg/daily/langgraph_*.dump      (written 02:3x ICT by agentos-pgbackup.timer)
#   - newest files tar    /opt/agentos_backups/files/daily/files_*.tgz         (chat attachments, if present)
#   - status.json (Settings backup card)
# -> /workspace/backups/vps83/<YYYYMMDD_HHMM>/ (mode 700/600), sha256 checked against the VPS, pg_restore -l TOC check, keep 14 pulls.
# Read-only on the VPS (ssh + cat/sha256sum only). Never touches env files or secrets. Safe to run from cron on the box:
#   45 3 * * *  /workspace/agentos-local/ops/pull_backup_vps83.sh >> /workspace/backups/vps83/pull.log 2>&1
set -euo pipefail
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -i "$HOME/.ssh/moza_vps82" -p 30029 root@103.142.24.220)
DEST_ROOT=${DEST_ROOT:-/workspace/backups/vps83}; KEEP=${KEEP:-14}
TS=$(TZ=Asia/Bangkok date +%Y%m%d_%H%M); D="$DEST_ROOT/$TS"; umask 077; mkdir -p "$D"
newest() { "${SSH[@]}" "ls -1t $1 2>/dev/null | head -1"; }
pull() { # remote path -> local file, verified by sha256
  local r="$1" l="$D/$(basename "$1")" rs ls
  rs=$("${SSH[@]}" "sha256sum '$r'" | cut -d' ' -f1)
  "${SSH[@]}" "cat '$r'" > "$l.part" && mv "$l.part" "$l"
  ls=$(sha256sum "$l" | cut -d' ' -f1)
  [ "$rs" = "$ls" ] || { echo "SHA256 MISMATCH $r"; return 1; }
  echo "$ls  $(basename "$l")" >> "$D/SHA256SUMS"; echo "ok $(basename "$l") $(du -h "$l" | cut -f1) sha256 ${ls:0:12}"
}
DB=$(newest '/opt/agentos_backups/pg/daily/*.dump'); [ -n "$DB" ] || { echo "no dump on VPS"; exit 1; }
pull "$DB"
FI=$(newest '/opt/agentos_backups/files/daily/*.tgz' || true); [ -n "$FI" ] && pull "$FI" || echo "files tar: none yet (created by the next nightly run)"
"${SSH[@]}" "cat /opt/agentos_backups/pg/status.json" > "$D/status.json" || true
# integrity: archive TOC must be readable (pg_restore 17 reads pg16 custom dumps)
n=$(pg_restore -l "$D/$(basename "$DB")" | grep -c ' TABLE DATA ' || true); echo "pg_restore -l: $n TABLE DATA entries"; [ "$n" -gt 10 ] || { echo "dump TOC too small"; exit 1; }
[ -n "${FI:-}" ] && { tar -tzf "$D/$(basename "$FI")" > /dev/null && echo "files tar: listable"; }
age_h=$(( ( $(date +%s) - $("${SSH[@]}" "stat -c %Y '$DB'") ) / 3600 )); echo "newest dump age: ${age_h} h"
printf '{"pulled_at":"%s","dump":"%s","dump_age_h":%s,"files":"%s","table_data":%s}\n' "$TS" "$(basename "$DB")" "$age_h" "$(basename "${FI:-}")" "$n" > "$D/pull.json"
# rotation
ls -1dt "$DEST_ROOT"/20*/ 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -rf
echo "pulled -> $D ($(du -sh "$D" | cut -f1)); keeping $KEEP pulls"
