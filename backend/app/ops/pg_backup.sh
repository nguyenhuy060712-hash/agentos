#!/bin/bash
# Nightly logical backup of the Agent OS Postgres (container lc-postgres, db langgraph) with rotation: 7 daily + 4 weekly.
# GĐ3 (20/10/2026): also archives the uploaded-file store (AOS_FILES_DIR, default /opt/agentos/var/files) with the same rotation
# into /opt/agentos_backups/files/{daily,weekly} (tar.gz, mode 600).
# Uses the container's own pg_dump (same major version as the server). Writes a status JSON for the Settings card.
set -uo pipefail
ROOT=/opt/agentos_backups/pg; DB=langgraph; USER=langgraph; CT=lc-postgres
KEEP_DAILY=${KEEP_DAILY:-7}; KEEP_WEEKLY=${KEEP_WEEKLY:-4}
mkdir -p "$ROOT/daily" "$ROOT/weekly"; chmod 700 "$ROOT"
TS=$(date +%Y%m%d_%H%M%S); F="$ROOT/daily/${DB}_${TS}.dump"; T0=$(date +%s)
status() { # ok|error message
  python3 - "$1" "$2" "$F" "$T0" <<'PY'
import json, os, sys, time, glob
st, msg, f, t0 = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
root = "/opt/agentos_backups/pg"; p = f"{root}/status.json"
try: old = json.load(open(p))
except Exception: old = {}
ls = lambda d: sorted(({"file": os.path.basename(x), "bytes": os.path.getsize(x), "mtime": int(os.path.getmtime(x))} for x in glob.glob(f"{root}/{d}/*.dump")), key=lambda x: -x["mtime"])
old.update({"last_backup": {"status": st, "message": msg, "file": os.path.basename(f) if st == "ok" else None, "bytes": os.path.getsize(f) if st == "ok" and os.path.exists(f) else None,
            "at": int(time.time()), "duration_s": int(time.time()) - t0}, "daily": ls("daily"), "weekly": ls("weekly"), "schedule": "daily 02:30 Asia/Bangkok; keep 7 daily + 4 weekly"})
json.dump(old, open(p + ".tmp", "w"), indent=1); os.replace(p + ".tmp", p); os.chmod(p, 0o644)
PY
}
if ! docker exec "$CT" pg_dump -U "$USER" -d "$DB" -Fc -Z 6 > "$F.part" 2> "$ROOT/last_error.log"; then
  rm -f "$F.part"; status error "pg_dump failed: $(tail -c 300 "$ROOT/last_error.log" | tr '\n' ' ')"; exit 1; fi
# integrity: archive must list a TOC
if ! docker exec -i "$CT" pg_restore -l < "$F.part" > /dev/null 2>> "$ROOT/last_error.log"; then
  rm -f "$F.part"; status error "dump failed integrity check (pg_restore -l)"; exit 1; fi
mv "$F.part" "$F"; chmod 600 "$F"
# weekly: Sunday's backup (or the first run if there is none) is also kept as weekly
if [ "$(date +%u)" = 7 ] || [ -z "$(ls -A "$ROOT/weekly")" ]; then cp -p "$F" "$ROOT/weekly/"; fi
ls -1t "$ROOT"/daily/*.dump 2>/dev/null | tail -n +$((KEEP_DAILY+1)) | xargs -r rm -f
ls -1t "$ROOT"/weekly/*.dump 2>/dev/null | tail -n +$((KEEP_WEEKLY+1)) | xargs -r rm -f
# uploaded files (chat attachments): same rotation; a files failure is reported but does not undo the DB backup
FILES_SRC=${AOS_FILES_DIR:-/opt/agentos/var/files}; FROOT=/opt/agentos_backups/files; FMSG=""
if [ -d "$FILES_SRC" ]; then
  mkdir -p "$FROOT/daily" "$FROOT/weekly"; chmod 700 "$FROOT"; FF="$FROOT/daily/files_${TS}.tgz"
  if tar -czf "$FF.part" -C "$(dirname "$FILES_SRC")" "$(basename "$FILES_SRC")" 2>> "$ROOT/last_error.log" && tar -tzf "$FF.part" > /dev/null 2>> "$ROOT/last_error.log"; then
    mv "$FF.part" "$FF"; chmod 600 "$FF"
    if [ "$(date +%u)" = 7 ] || [ -z "$(ls -A "$FROOT/weekly")" ]; then cp -p "$FF" "$FROOT/weekly/"; fi
    ls -1t "$FROOT"/daily/*.tgz 2>/dev/null | tail -n +$((KEEP_DAILY+1)) | xargs -r rm -f
    ls -1t "$FROOT"/weekly/*.tgz 2>/dev/null | tail -n +$((KEEP_WEEKLY+1)) | xargs -r rm -f
    FMSG=" + files $(basename "$FF") $(du -h "$FF" | cut -f1)"
  else rm -f "$FF.part"; FMSG=" + FILES BACKUP FAILED (see last_error.log)"; fi
fi
status ok "backup $(basename "$F") $(du -h "$F" | cut -f1)$FMSG"
echo "ok $F$FMSG"
