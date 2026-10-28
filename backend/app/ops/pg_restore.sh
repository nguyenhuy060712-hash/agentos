#!/bin/bash
# Restore an Agent OS Postgres backup.
#   pg_restore.sh --test [dump]            restore into a temporary DB, verify, drop it (safe; used by the restore drill)
#   pg_restore.sh --into NAME [dump]       restore into a NEW database NAME (must not exist)
#   pg_restore.sh --live [dump] --yes-overwrite-live   stop the API, recreate db langgraph from the dump, start the API
# dump defaults to the newest daily backup.
set -euo pipefail
ROOT=/opt/agentos_backups/pg; CT=lc-postgres; U=langgraph
MODE=""; NAME=""; DUMP=""; YES=0
while [ $# -gt 0 ]; do case "$1" in
  --test) MODE=test;; --into) MODE=into; NAME="$2"; shift;; --live) MODE=live;; --yes-overwrite-live) YES=1;;
  *) DUMP="$1";; esac; shift; done
[ -z "$MODE" ] && { sed -n 2,6p "$0"; exit 2; }
DUMP=${DUMP:-$(ls -1t $ROOT/daily/*.dump | head -1)}; [ -f "$DUMP" ] || { echo "no dump: $DUMP"; exit 1; }
psql_() { docker exec -i "$CT" psql -U "$U" -d postgres -v ON_ERROR_STOP=1 -Atc "$1"; }
restore_into() { # db
  psql_ "CREATE DATABASE \"$1\" OWNER $U"
  docker exec -i "$CT" pg_restore -U "$U" -d "$1" --no-owner --role="$U" --exit-on-error < "$DUMP"
}
verify() { # db -> prints table count + key row counts, compares with live when testing
  docker exec -i "$CT" psql -U "$U" -d "$1" -Atc "SELECT (SELECT count(*) FROM information_schema.tables WHERE table_schema='public'),
    (SELECT count(*) FROM agents), (SELECT count(*) FROM tr_runs), (SELECT count(*) FROM datasets), (SELECT count(*) FROM lg_assistants)"
}
case $MODE in
test)
  N="restore_test_$(date +%Y%m%d%H%M%S)"; T0=$(date +%s)
  trap 'psql_ "DROP DATABASE IF EXISTS \"$N\"" >/dev/null 2>&1 || true' EXIT
  restore_into "$N"; R=$(verify "$N"); L=$(verify langgraph)
  echo "restored: tables|agents|runs|datasets|assistants = $R"; echo "live now: $L"
  RT=$(echo "$R" | cut -d'|' -f1); LT=$(echo "$L" | cut -d'|' -f1)
  OK=ok; [ "$RT" -ge 1 ] && [ "$RT" -le "$LT" ] && [ "$(echo "$R" | cut -d'|' -f2)" -ge 1 ] || OK=error
  python3 - "$OK" "$(basename "$DUMP")" "$R" "$L" "$(( $(date +%s) - T0 ))" <<'PY'
import json, os, sys, time
p = "/opt/agentos_backups/pg/status.json"
try: s = json.load(open(p))
except Exception: s = {}
s["last_restore_test"] = {"status": sys.argv[1], "file": sys.argv[2], "restored_counts": sys.argv[3], "live_counts": sys.argv[4], "duration_s": int(sys.argv[5]), "at": int(time.time())}
json.dump(s, open(p + ".tmp", "w"), indent=1); os.replace(p + ".tmp", p); os.chmod(p, 0o644)
PY
  echo "restore test: $OK"; [ $OK = ok ];;
into)
  [[ "$NAME" =~ ^[a-z_][a-z0-9_]{0,40}$ ]] || { echo "bad name"; exit 2; }
  [ "$NAME" != langgraph ] || { echo "use --live for the live database"; exit 2; }
  restore_into "$NAME"; verify "$NAME";;
live)
  [ $YES = 1 ] || { echo "refusing: add --yes-overwrite-live"; exit 2; }
  /opt/agentos/ops/pg_backup.sh >/dev/null || { echo "pre-restore safety backup failed"; exit 1; }
  systemctl stop deepagent-api deepagent-worker 2>/dev/null || true
  psql_ "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='langgraph' AND pid<>pg_backend_pid()" >/dev/null
  psql_ "ALTER DATABASE langgraph RENAME TO langgraph_before_restore_$(date +%Y%m%d%H%M%S)"
  restore_into langgraph; verify langgraph
  systemctl start deepagent-api; systemctl start deepagent-worker 2>/dev/null || true
  echo "live restored; previous DB kept as langgraph_before_restore_* (drop it manually when satisfied)";;
esac
