#!/bin/bash
# GD6.11 - off-site backup with restic (client-side encrypted; target = S3-compatible B2/R2, SFTP, or a local dir for drills).
# Backs up: newest nightly pg dump (fresh pg_dump -Fc if none newer than MAX_DUMP_AGE_H) + code dir(s) + .env file(s) + newest uploaded-files tgz.
# Then: restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune; weekly (Sunday) restic check --read-data-subset=1%.
# Writes a status JSON (OFFSITE_STATUS_FILE) for Agent OS to show; exits non-zero on any failure.
#
# Config (env; on the VPS put them in a root-only file and point OFFSITE_ENV_FILE at it, mode 600):
#   RESTIC_REPOSITORY      e.g. s3:https://s3.eu-central-003.backblazeb2.com/BUCKET | s3:https://ACCOUNT.r2.cloudflarestorage.com/BUCKET | /path/to/dir
#   RESTIC_PASSWORD_FILE   file holding the repository password (mode 600) - LOSE IT = LOSE THE BACKUPS, keep a copy elsewhere
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   (B2/R2 application key) - normally inside OFFSITE_ENV_FILE
#   OFFSITE_ENV_FILE       file of KEY=VALUE lines sourced before anything else (mode 600 enforced)
#   PG_DUMP_DIR            where pg_backup.sh writes dumps (default /opt/agentos_backups/pg/daily)
#   PG_CONTAINER/PG_USER/PG_DB   used for a fresh dump (defaults lc-postgres / langgraph / langgraph)
#   OFFSITE_PATHS          space-separated dirs/files to back up (default /opt/agentos; its var/, venv, node_modules are skipped)
#   OFFSITE_ENV_FILES      extra .env files to include (default none; /opt/agentos/.env* is already inside OFFSITE_PATHS)
#   FILES_BACKUP_DIR       uploaded-files tgz dir (default /opt/agentos_backups/files/daily) - newest one is included if present
#   OFFSITE_WORK_DIR       scratch dir for a fresh dump (default /opt/agentos_backups/offsite/work)
#   OFFSITE_STATUS_FILE    default /opt/agentos_backups/offsite/status.json
#   MAX_DUMP_AGE_H=26   OFFSITE_CHECK_DAY=7 (date +%u; OFFSITE_CHECK=1 forces a check now)   RESTIC=restic
# Failure alert (not wired yet): in the systemd service add
#   ExecStopPost=/bin/sh -c '[ "$SERVICE_RESULT" = success ] || curl -fsS -d "Offsite backup FAILED on $(hostname)" https://<notify-url>'
set -uo pipefail
if [ -n "${OFFSITE_ENV_FILE:-}" ]; then
  [ -f "$OFFSITE_ENV_FILE" ] || { echo "OFFSITE_ENV_FILE not found" >&2; exit 2; }
  m=$(stat -c %a "$OFFSITE_ENV_FILE"); case "$m" in 600|400) ;; *) echo "refusing: $OFFSITE_ENV_FILE has mode $m, need 600" >&2; exit 2;; esac
  set -a; . "$OFFSITE_ENV_FILE"; set +a
fi
RESTIC=${RESTIC:-restic}; PG_DUMP_DIR=${PG_DUMP_DIR:-/opt/agentos_backups/pg/daily}
PG_CONTAINER=${PG_CONTAINER:-lc-postgres}; PG_USER=${PG_USER:-langgraph}; PG_DB=${PG_DB:-langgraph}
OFFSITE_PATHS=${OFFSITE_PATHS:-/opt/agentos}; FILES_BACKUP_DIR=${FILES_BACKUP_DIR:-/opt/agentos_backups/files/daily}
OFFSITE_WORK_DIR=${OFFSITE_WORK_DIR:-/opt/agentos_backups/offsite/work}
STATUS=${OFFSITE_STATUS_FILE:-/opt/agentos_backups/offsite/status.json}; MAX_AGE_H=${MAX_DUMP_AGE_H:-26}
T0=$(date +%s); LOG=$(mktemp); SNAP=""; BYTES=""; ADDED=""; DUMP=""
trap 'rm -f "$LOG"' EXIT
write_status() { # ok|error message
  mkdir -p "$(dirname "$STATUS")"
  ST="$1" MSG="$2" SNAP="$SNAP" BYTES="$BYTES" ADDED="$ADDED" DUMP="$(basename "$DUMP")" T0="$T0" REPO="${RESTIC_REPOSITORY:-}" STATUS="$STATUS" python3 - <<'PY'
import json, os, time
e = os.environ; p = e["STATUS"]; now = int(time.time())
try: s = json.load(open(p))
except Exception: s = {}
s.update({"status": e["ST"], "message": e["MSG"], "at": now, "duration_s": now - int(e["T0"]), "repository": e["REPO"],
          "host": os.uname().nodename, "schedule": "daily 03:30 Asia/Bangkok; keep 7 daily + 4 weekly + 6 monthly; check 1% on Sunday"})
if e["ST"] == "ok":
    s.update({"last_ok": now, "snapshot_id": e["SNAP"], "size_bytes": int(e["BYTES"] or 0), "added_bytes": int(e["ADDED"] or 0), "dump": e["DUMP"]})
else:
    s.update({"last_error": e["MSG"], "last_error_at": now})
json.dump(s, open(p + ".tmp", "w"), indent=1); os.replace(p + ".tmp", p); os.chmod(p, 0o644)
PY
}
fail() { echo "offsite backup FAILED: $1" >&2; [ -s "$LOG" ] && tail -n 5 "$LOG" >&2; write_status error "$1 $(tail -c 300 "$LOG" | tr '\n' ' ')"; exit 1; }
command -v "$RESTIC" >/dev/null || fail "restic not found"
[ -n "${RESTIC_REPOSITORY:-}" ] || fail "RESTIC_REPOSITORY not set"
{ [ -n "${RESTIC_PASSWORD_FILE:-}" ] && [ -f "$RESTIC_PASSWORD_FILE" ]; } || fail "RESTIC_PASSWORD_FILE missing"
# 1. init the repo the first time
if ! "$RESTIC" cat config >/dev/null 2>"$LOG"; then "$RESTIC" init >"$LOG" 2>&1 || fail "cannot open or init repository"; fi
# 2. newest dump, or a fresh one
DUMP=$(ls -1t "$PG_DUMP_DIR"/*.dump 2>/dev/null | head -1)
if [ -z "$DUMP" ] || [ -n "$(find "$DUMP" -mmin +$((MAX_AGE_H*60)) 2>/dev/null)" ]; then
  mkdir -p "$OFFSITE_WORK_DIR" && chmod 700 "$OFFSITE_WORK_DIR" || fail "cannot prepare work dir"
  rm -f "${OFFSITE_WORK_DIR:?}"/*.dump "${OFFSITE_WORK_DIR:?}"/*.dump.part
  DUMP="$OFFSITE_WORK_DIR/${PG_DB}_$(date +%Y%m%d_%H%M%S).dump"
  docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc -Z 6 > "$DUMP.part" 2>"$LOG" || { rm -f "$DUMP.part"; fail "pg_dump failed"; }
  docker exec -i "$PG_CONTAINER" pg_restore -l < "$DUMP.part" >/dev/null 2>"$LOG" || { rm -f "$DUMP.part"; fail "dump failed integrity check"; }
  mv "$DUMP.part" "$DUMP"; chmod 600 "$DUMP"
fi
# 3. what to back up
ITEMS=("$DUMP"); EXC=()
for p in $OFFSITE_PATHS ${OFFSITE_ENV_FILES:-}; do
  if [ -e "$p" ]; then ITEMS+=("$p"); [ -d "$p" ] && EXC+=(--exclude "$p/var" --exclude "$p/venv" --exclude "$p/.venv")
  else echo "skip (missing): $p" >&2; fi
done
EXC+=(--exclude "**/node_modules" --exclude "**/__pycache__")
F=$(ls -1t "$FILES_BACKUP_DIR"/*.tgz 2>/dev/null | head -1); [ -n "$F" ] && ITEMS+=("$F")
OUT=$("$RESTIC" backup --json --host "$(hostname)" --tag "host-$(hostname)" --tag "date-$(date +%F)" --tag agentos "${EXC[@]}" "${ITEMS[@]}" 2>"$LOG") || fail "restic backup failed"
read -r SNAP BYTES ADDED < <(printf '%s\n' "$OUT" | python3 -c '
import sys, json
for l in sys.stdin:
    try: d = json.loads(l)
    except Exception: continue
    if d.get("message_type") == "summary": print(d.get("snapshot_id", "")[:8], d.get("total_bytes_processed", 0), d.get("data_added", 0))')
[ -n "$SNAP" ] || fail "no snapshot id in restic output"
# 4. retention + weekly integrity check
"$RESTIC" forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune >"$LOG" 2>&1 || fail "restic forget/prune failed"
CHK=""
if [ "$(date +%u)" = "${OFFSITE_CHECK_DAY:-7}" ] || [ "${OFFSITE_CHECK:-0}" = 1 ]; then
  "$RESTIC" check --read-data-subset=1% >"$LOG" 2>&1 || fail "restic check failed"; CHK=" + check ok"
fi
write_status ok "snapshot $SNAP, $(basename "$DUMP"), $BYTES bytes processed$CHK"
echo "ok snapshot $SNAP$CHK"
