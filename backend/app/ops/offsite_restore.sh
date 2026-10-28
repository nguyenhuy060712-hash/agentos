#!/bin/bash
# GD6.11 - restore from the off-site restic repository.
#   offsite_restore.sh --list                                  list snapshots
#   offsite_restore.sh [--snapshot ID|latest] --target DIR     restore the snapshot's files into DIR (default: new dir under /tmp)
#   ... --db NAME                                              also pg_restore the dump found in the snapshot into database NAME
#                                                              (NAME is created if missing; if it exists, objects are replaced with --clean --if-exists).
#                                                              Without --db but with --pg: a NEW scratch DB aos_restore_<timestamp>.
#   ... --into-live --yes-overwrite-live                       allow --db to be the live DB (PG_DB). Stop the API first!
# Env: same as offsite_backup.sh (OFFSITE_ENV_FILE, RESTIC_REPOSITORY, RESTIC_PASSWORD_FILE, PG_CONTAINER, PG_USER, PG_DB, RESTIC).
set -euo pipefail
if [ -n "${OFFSITE_ENV_FILE:-}" ]; then
  [ -f "$OFFSITE_ENV_FILE" ] || { echo "OFFSITE_ENV_FILE not found" >&2; exit 2; }
  m=$(stat -c %a "$OFFSITE_ENV_FILE"); case "$m" in 600|400) ;; *) echo "refusing: $OFFSITE_ENV_FILE has mode $m, need 600" >&2; exit 2;; esac
  set -a; . "$OFFSITE_ENV_FILE"; set +a
fi
RESTIC=${RESTIC:-restic}; PG_CONTAINER=${PG_CONTAINER:-lc-postgres}; PG_USER=${PG_USER:-langgraph}; PG_DB=${PG_DB:-langgraph}
SNAP=latest; TARGET=""; DB=""; PG=0; LIST=0; LIVE=0; YES=0
while [ $# -gt 0 ]; do case "$1" in
  --list) LIST=1;; --snapshot) SNAP="$2"; shift;; --target) TARGET="$2"; shift;; --db) DB="$2"; PG=1; shift;; --pg) PG=1;;
  --into-live) LIVE=1;; --yes-overwrite-live) YES=1;; *) sed -n 2,10p "$0"; exit 2;; esac; shift; done
[ -n "${RESTIC_REPOSITORY:-}" ] && [ -f "${RESTIC_PASSWORD_FILE:-/nonexistent}" ] || { echo "RESTIC_REPOSITORY / RESTIC_PASSWORD_FILE not set" >&2; exit 2; }
if [ $LIST = 1 ]; then "$RESTIC" snapshots; exit $?; fi
TARGET=${TARGET:-$(mktemp -d /tmp/aos_offsite_restore.XXXXXX)}; mkdir -p "$TARGET"
"$RESTIC" restore "$SNAP" --target "$TARGET" >/dev/null; echo "files restored to $TARGET"
[ $PG = 1 ] || exit 0
DUMP=$(find "$TARGET" -name '*.dump' -type f -printf '%T@ %p\n' | sort -rn | head -1 | cut -d' ' -f2-)
[ -n "$DUMP" ] || { echo "no *.dump in snapshot" >&2; exit 1; }
DB=${DB:-aos_restore_$(date +%Y%m%d%H%M%S)}
[[ "$DB" =~ ^[a-z_][a-z0-9_]{0,40}$ ]] || { echo "bad db name" >&2; exit 2; }
if [ "$DB" = "$PG_DB" ]; then
  { [ $LIVE = 1 ] && [ $YES = 1 ]; } || { echo "refusing: $DB is the live database; add --into-live --yes-overwrite-live" >&2; exit 2; }
fi
psql_() { docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -Atc "$1"; }
if [ "$(psql_ "SELECT 1 FROM pg_database WHERE datname='$DB'")" = 1 ]; then
  docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$DB" --clean --if-exists --no-owner < "$DUMP"
else
  psql_ "CREATE DATABASE \"$DB\"" >/dev/null
  docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$DB" --no-owner --exit-on-error < "$DUMP"
fi
echo "database restored into $DB from $(basename "$DUMP")"
