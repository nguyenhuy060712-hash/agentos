#!/usr/bin/env bash
# GĐ11.73 + 11.ZA2: restore drill on THIS machine only (WSL, local docker Postgres `agentos-pg`). Never touches the VPS, Hermes, Home Assistant or Odoo volumes.
#   ops/backup_drill_local.sh [--keep-old]
# 1) makes a small fake box (2 volumes with files + a package + cache/), a fake snapshot image and snapshot archive, and marker rows (provider key, OAuth client secret/access/refresh,
#    secret connection) in the local DB;  2) backs up to var/backup_drill/<run>/: Postgres dump + master key copy + ops/backup_computers.sh output (real local boxes included);
# 3) deletes the fake box / image / archive / marker rows;  4) restores into a scratch database (aos_drill_<run>) and throw-away volumes (aosdrill<run>-aos-box-*);
# 5) verifies: file checksums of every volume, the fake box "starts" and sees its files + package, snapshot archive + skills identical, image id identical, row counts equal,
#    every sealed cell opens and the markers decrypt with the RESTORED key file, and a WRONG key fails;  GĐ12.48a: also a webhook destination whose sealed signing key still signs
#    (same signature as before), an invite whose hashed token still validates, skill + template packs, alert rule, retention tier, tr_stats_hourly, a trace found by full-text search, a dataset, migrations 0100-0190;  6) removes everything it created except the backup folder.
# Output: one RESULT line per step + var/backup_drill/<run>/RESULT.txt. Exit 0 = every check passed.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd); APPDIR=$(cd "$HERE/.." && pwd)
export AOS_VAR_DIR=${AOS_VAR_DIR:-$APPDIR/var}
PY=${PY:-/home/ad/.venvs/agentos/bin/python}; PGC=${PG_CONTAINER:-agentos-pg}; PGU=${PG_USER:-aos}; PGDB=${PG_DB:-agentos}
LIVE_URI=${PG_URI:-postgresql://aos:aoslocal@127.0.0.1:5432/$PGDB}
RUN=$(date +%Y%m%d_%H%M%S); ROOT=$AOS_VAR_DIR/backup_drill; W=$ROOT/$RUN; SCR=aos_drill_$RUN; PFX=aosdrill${RUN//_/}-
HEX=$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n'); BOX=aos-box-$HEX; IMGTAG=aos-snap-drill$HEX:1; ARC=$AOS_VAR_DIR/snapshots/drill-$RUN.tar.zst
IMG=${AOS_BK_IMAGE:-aos-computer:20261026}
FAIL=0; T0=$SECONDS; mkdir -p "$W"; chmod 700 "$ROOT" "$W"; : > "$W/RESULT.txt"
say() { echo "$*" | tee -a "$W/RESULT.txt"; }
ok() { say "PASS  $*"; }
bad() { say "FAIL  $*"; FAIL=1; }
psql_() { docker exec -i "$PGC" psql -U "$PGU" -d "${2:-postgres}" -v ON_ERROR_STOP=1 -Atc "$1"; }
cleanup() {
  psql_ "DROP DATABASE IF EXISTS \"$SCR\" WITH (FORCE)" >/dev/null 2>&1
  for v in $(docker volume ls -q --filter label=aos.drill=1 | grep "^$PFX"); do docker volume rm -f "$v" >/dev/null 2>&1; done
  docker volume rm -f "$BOX-workspace" "$BOX-opt" >/dev/null 2>&1; docker image rm "$IMGTAG" >/dev/null 2>&1; rm -f "$ARC"
  [ -f "$W/seed.json" ] && AOS_SECRET_KEY_FILE=$AOS_VAR_DIR/aos_secret.key PG_URI=$LIVE_URI PYTHONPATH=$APPDIR/app "$PY" "$HERE/backup_verify.py" unseed --in "$W/seed.json" >/dev/null 2>&1
  [ "${1:-}" = keep ] || true
}
trap cleanup EXIT
[ -f "$AOS_VAR_DIR/aos_secret.key" ] || { echo "no $AOS_VAR_DIR/aos_secret.key (the local master key)"; exit 2; }
V() { AOS_SECRET_KEY_FILE=${KEYF:-$AOS_VAR_DIR/aos_secret.key} PYTHONPATH=$APPDIR/app "$PY" "$HERE/backup_verify.py" "$@"; }
HELP=(docker run --rm --network none --label aos.backup.helper=1 --entrypoint sh)

# ---- 1. fake state
S0=$SECONDS
docker volume create --label aos.box=1 --label "aos.box.id=$HEX" "$BOX-workspace" >/dev/null && docker volume create --label aos.box=1 --label "aos.box.id=$HEX" "$BOX-opt" >/dev/null
"${HELP[@]}" -v "$BOX-workspace:/workspace" -v "$BOX-opt:/opt/aos-user" "$IMG" -c '
  mkdir -p /workspace/notes /workspace/out /opt/aos-user/lib/drillpkg /opt/aos-user/cache/pip
  echo "drill-marker-'"$HEX"'" > /workspace/notes/hello.txt; head -c 3000000 /dev/urandom > /workspace/out/data.bin
  echo "VALUE = 42" > /opt/aos-user/lib/drillpkg/__init__.py; echo "#!/bin/sh" > /opt/aos-user/setup.sh; head -c 500000 /dev/urandom > /opt/aos-user/cache/pip/junk.whl
  chown -R 1000:1000 /workspace /opt/aos-user' || { bad "could not create the fake box"; exit 1; }
T=$(mktemp -d); echo drill > "$T/f"; tar -C "$T" -cf - f | docker import - "$IMGTAG" >/dev/null; rm -rf "$T"; IMGID=$(docker image inspect --format '{{.Id}}' "$IMGTAG")
mkdir -p "$AOS_VAR_DIR/snapshots"; head -c 2000000 /dev/urandom > "$ARC"; ARC_SUM=$(sha256sum < "$ARC")
KEYSUM=$(sha256sum < "$AOS_VAR_DIR/aos_secret.key")
PG_URI=$LIVE_URI V seed --out "$W/seed.json" >/dev/null && ok "fake box + snapshot image + snapshot archive + marker rows created" || { bad "seed failed"; exit 1; }
PG_URI=$LIVE_URI V counts --out "$W/counts.json" >/dev/null

# ---- 2. backup
S1=$SECONDS
docker exec "$PGC" pg_dump -U "$PGU" -Fc "$PGDB" > "$W/$PGDB.dump" && NT=$(docker exec -i "$PGC" pg_restore -l < "$W/$PGDB.dump" | grep -c ' TABLE DATA ') || { bad "pg_dump failed"; exit 1; }
mkdir -p "$W/master"; install -m 600 "$AOS_VAR_DIR/aos_secret.key" "$W/master/aos_secret.key"
bash "$HERE/backup_computers.sh" backup "$W/computers" | tee -a "$W/RESULT.txt" || { bad "computers backup failed"; exit 1; }
S2=$SECONDS; BYTES=$(du -sb "$W" | cut -f1); ok "backup done in $((S2-S1))s, $BYTES bytes ($((BYTES/1048576)) MiB): pg dump $(stat -c %s "$W/$PGDB.dump") B ($NT tables with data), computers $(du -sb "$W/computers" | cut -f1) B"
for f in "$W"/computers/volumes/*.tgz; do say "      $(basename "$f") $(stat -c %s "$f") B"; done

# ---- 3. lose everything the fake box had
PG_URI=$LIVE_URI V unseed --in "$W/seed.json" >/dev/null; mv "$W/seed.json" "$W/seed.keep.json"
docker volume rm -f "$BOX-workspace" "$BOX-opt" >/dev/null; docker image rm "$IMGTAG" >/dev/null; rm -f "$ARC"
docker volume inspect "$BOX-workspace" >/dev/null 2>&1 && bad "fake box volume still exists" || ok "fake box deleted (volumes, image, snapshot archive, marker rows)"

# ---- 4. restore
S3=$SECONDS
psql_ "CREATE DATABASE \"$SCR\"" >/dev/null && docker exec -i "$PGC" pg_restore -U "$PGU" -d "$SCR" --no-owner --exit-on-error < "$W/$PGDB.dump" && ok "database restored into scratch $SCR" || bad "pg_restore failed"
AOS_RESTORE_VAR=$W/restored_var bash "$HERE/backup_computers.sh" restore "$W/computers" --prefix "$PFX" | tee -a "$W/RESULT.txt" || bad "computers restore failed"
S4=$SECONDS; ok "restore done in $((S4-S3))s"

# ---- 5. checks
R_WS=$PFX$BOX-workspace; R_OPT=$PFX$BOX-opt
OUT=$(docker run --rm --network none --label aos.backup.helper=1 -v "$R_WS:/workspace" -v "$R_OPT:/opt/aos-user" --entrypoint sh "$IMG" -c '
  grep -q "drill-marker-'"$HEX"'" /workspace/notes/hello.txt && [ "$(stat -c %s /workspace/out/data.bin)" = 3000000 ] &&
  PYTHONPATH=/opt/aos-user/lib python3 -c "import drillpkg; assert drillpkg.VALUE == 42" && [ -f /opt/aos-user/setup.sh ] &&
  [ "$(stat -c %u /workspace/notes/hello.txt)" = 1000 ] && [ ! -e /opt/aos-user/cache ] && echo BOXOK' 2>&1)
[ "$OUT" = BOXOK ] && ok "restored box starts: its files, the package, the owner uid are there, cache/ was not backed up" || bad "restored box check: $OUT"
[ "$(sha256sum < "$W/restored_var/snapshots/drill-$RUN.tar.zst" 2>/dev/null)" = "$ARC_SUM" ] && ok "snapshot archive identical" || bad "snapshot archive differs"
[ "$(docker image inspect --format '{{.Id}}' "$IMGTAG" 2>/dev/null)" = "$IMGID" ] && ok "snapshot image restored with the same id" || bad "snapshot image missing or different"
if [ -d "$AOS_VAR_DIR/skills" ]; then diff -r "$AOS_VAR_DIR/skills" "$W/restored_var/skills" >/dev/null && ok "skill trees identical ($(find "$AOS_VAR_DIR/skills" -type f | wc -l) files)" || bad "skill trees differ"; fi
[ "$(sha256sum < "$W/master/aos_secret.key")" = "$KEYSUM" ] && ok "master key copy identical" || bad "master key copy differs"
KEYF=$W/master/aos_secret.key
R=$(PG_URI=${LIVE_URI%/*}/$SCR KEYF=$KEYF V check --in "$W/seed.keep.json" --counts "$W/counts.json"); RC=$?; echo "$R" > "$W/check.json"
[ $RC = 0 ] && ok "restored DB + restored key: $(echo "$R" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(len(d["markers"]),"markers decrypt,",d["sweep_ok"],"sealed cells open,",d["sweep_failed"],"fail,",d["counts_compared"],"table counts compared, diff:",d["counts_diff"])')" || bad "restore check: $R"
# GĐ12.48a: the new part-A state (webhook signing key, invite, skill/template, alert rule, retention, hourly stats, trace search, dataset, migrations)
G=$(echo "$R" | "$PY" -c 'import json,sys; g=json.load(sys.stdin).get("g12",{}); print(" ".join(k+"="+v for k,v in g.items()) if g else "MISSING"); sys.exit(0 if g and all(v=="ok" for v in g.values()) else 1)')
[ $? = 0 ] && ok "GĐ12 state after restore: $G" || bad "GĐ12 state after restore: $G"
# GĐ13.60: the dump holds every GĐ13 table, and after the restore an issue (+ trace + event), a memory file and migrations 0200-0230 are there
GT=$("$PY" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import backup_verify as b; print(" ".join(b.G13_TABLES))' "$HERE"); GMISS=""
for t in $GT; do docker exec -i "$PGC" pg_restore -l < "$W/$PGDB.dump" | grep -q " TABLE DATA public $t " || GMISS="$GMISS $t"; done
[ -z "$GMISS" ] && ok "dump has every GĐ13 table (0 missing)" || bad "GĐ13 tables missing from the dump:$GMISS"
G=$(echo "$R" | "$PY" -c 'import json,sys; g=json.load(sys.stdin).get("g13",{}); print(" ".join(k+"="+v for k, v in g.items()) if g else "MISSING"); sys.exit(0 if g and all(v=="ok" for v in g.values()) else 1)')
[ $? = 0 ] && ok "GĐ13 state after restore: $G" || bad "GĐ13 state after restore: $G"
head -c 32 /dev/urandom | base64 > "$W/wrong.key"; chmod 600 "$W/wrong.key"
R=$(PG_URI=${LIVE_URI%/*}/$SCR KEYF=$W/wrong.key V check --in "$W/seed.keep.json" --expect-fail); [ $? = 0 ] && ok "a wrong master key opens nothing (the check is not vacuous)" || bad "wrong key still opened data: $R"
rm -f "$W/wrong.key"

# ---- 6. report
[ "${1:-}" = --keep-old ] || ls -1dt "$ROOT"/20* 2>/dev/null | tail -n +4 | xargs -r rm -rf      # keep the 3 newest drill folders
say "TOTAL $((SECONDS-T0))s (setup $((S1-S0))s, backup $((S2-S1))s, restore $((S4-S3))s); backup folder $W ($(du -sh "$W" | cut -f1)); $([ $FAIL = 0 ] && echo ALL-PASS || echo SOME-FAILED)"
exit $FAIL
