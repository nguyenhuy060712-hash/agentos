#!/usr/bin/env bash
# GĐ11.73 backup + restore of the virtual computers ("boxes") and the skill trees. Pure local script: no SSH. Called by ops/backup_vps83.sh (piped to the VPS
# with `bash -s -- backup <dir>`) and by ops/backup_drill_local.sh (the restore drill on this machine).
#   backup_computers.sh backup  <dest-dir>
#   backup_computers.sh restore <src-dir> [--prefix P] [--no-images]
# backup  : per box (docker volumes labelled aos.box=1, names aos-box-<16 hex>-workspace / -opt): <volume>.tgz (the /opt/aos-user one WITHOUT cache/) + a sha256 manifest;
#           snapshot archives (VAR/snapshots), skill trees (VAR/skills), committed images aos-snap-*, aos-ws*-agent-*--setup and (old tag style) agent-*--setup (docker save), egress network definitions.
#           NOT saved: RAM / running processes, logs older than 30 days (they are cut by the app), tool_runner (rebuilt from the image), the egress proxy container (re-created by the app).
#           No proxy CA exists (TLS passthrough), so there is no CA or key of the egress proxy to save. Postgres rows (policies, snapshot rows, sandboxes) ride the normal pg_dump.
# restore : volumes are created NEW (an existing volume is never touched: the script stops). --prefix P restores them as P<name>, labelled aos.drill=1 (throw-away copies, used by the drill).
#           Files are checked against the manifest after extraction. Snapshots/skills go to $AOS_RESTORE_VAR (default $AOS_VAR_DIR) and never overwrite an existing file.
# Only docker volumes whose label is aos.box=1 AND whose name matches the box pattern are read: other volumes on the host (Odoo, Home Assistant, Hermes ...) are never touched.
# Env: DOCKER (docker), AOS_VAR_DIR (/opt/agentos/var), AOS_RESTORE_VAR, AOS_BK_IMAGE (helper image = the computer image, already on the host; it needs tar + sha256sum, no pull).
set -euo pipefail
DOCKER=${DOCKER:-docker}
VAR=${AOS_VAR_DIR:-/opt/agentos/var}
RVAR=${AOS_RESTORE_VAR:-$VAR}
IMG=${AOS_BK_IMAGE:-aos-computer:20261026}
VOL_RE='^aos-box-[0-9a-f]{16}-(workspace|opt)$'
NET_RE='^(aos-egress-out|aos-sbx-egress-[0-9a-f]{16})$'

# One throw-away helper container: no network, no published ports, label for cleanup. Root inside so every file of the box can be read / written back with its owner.
_h() { "$DOCKER" run --rm --network none --security-opt no-new-privileges --pids-limit 200 --memory 1g --label aos.backup.helper=1 "$@"; }
_vols() { "$DOCKER" volume ls -q --filter label=aos.box=1 | grep -E "$VOL_RE" | sort || true; }
_excl() { case "$1" in *-opt) echo "./cache";; *) echo "./.aos-no-exclude";; esac; }     # cache/ of /opt/aos-user is rebuilt by the box; nothing is excluded from /workspace
_manifest() { # <volume> -> sha256 of every file (sorted), without the excluded folder
  local ex; ex=$(_excl "$1")
  _h -v "$1:/src:ro" --entrypoint sh "$IMG" -c "cd /src && find . -type f ! -path '$ex/*' -print0 | sort -z | xargs -0 -r sha256sum"
}

bk_backup() { # <dest>
  local D=$1 t0=$SECONDS nv=0 ni=0; umask 077; mkdir -p "$D/volumes" "$D/snapshots" "$D/network" "$D/images"; chmod 700 "$D"
  local v ex
  for v in $(_vols); do
    ex=$(_excl "$v")
    _h -v "$v:/src:ro" --entrypoint tar "$IMG" -C /src --numeric-owner --exclude="$ex" -czf - . > "$D/volumes/$v.tgz.part"
    mv "$D/volumes/$v.tgz.part" "$D/volumes/$v.tgz"; _manifest "$v" > "$D/volumes/$v.sha256"; nv=$((nv+1))
  done
  [ -d "$VAR/snapshots" ] && cp -a "$VAR/snapshots/." "$D/snapshots/" || true
  [ -d "$VAR/skills" ] && tar -C "$VAR" -czf "$D/skills.tgz" skills || true
  local refs; refs=$("$DOCKER" image ls --format '{{.Repository}}:{{.Tag}}' --filter 'reference=aos-snap-*' --filter 'reference=aos-ws*-agent-*--setup' --filter 'reference=agent-*--setup' | grep -v '<none>' | sort -u || true)
  if [ -n "$refs" ]; then
    # shellcheck disable=SC2086
    "$DOCKER" save -o "$D/images/images.tar" $refs; echo "$refs" > "$D/images/images.list"; ni=$(echo "$refs" | wc -l)
  fi
  local n
  for n in $("$DOCKER" network ls --format '{{.Name}}' | grep -E "$NET_RE" || true); do "$DOCKER" network inspect "$n" > "$D/network/$n.json"; done
  { echo "boxes_volumes $nv"; echo "snapshot_images $ni"; echo "snapshot_files $(find "$D/snapshots" -type f | wc -l)"; echo "skills $([ -f "$D/skills.tgz" ] && echo yes || echo no)"
    echo "networks $(ls "$D/network" | wc -l)"; echo "seconds $((SECONDS-t0))"; echo "bytes $(du -sb "$D" | cut -f1)"; } > "$D/COMPUTERS.txt"
  (cd "$D" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 -r sha256sum > SHA256SUMS); chmod -R go-rwx "$D"
  echo "computers backup ok: $nv volumes, $ni images, $(find "$D/snapshots" -type f | wc -l) snapshot files -> $D"
}

bk_restore() { # <src> [--prefix P] [--no-images]
  local S=$1; shift; local P="" IM=1
  while [ $# -gt 0 ]; do case "$1" in --prefix) P=$2; shift;; --no-images) IM=0;; *) echo "unknown option $1" >&2; return 2;; esac; shift; done
  [ -d "$S/volumes" ] || { echo "no volumes/ in $S" >&2; return 1; }
  (cd "$S" && sha256sum -c --quiet SHA256SUMS) || { echo "backup checksum mismatch" >&2; return 1; }
  local f v t lab chk bad=0 nf=0 nv=0
  for f in "$S"/volumes/*.tgz; do
    [ -e "$f" ] || continue; v=$(basename "$f" .tgz); echo "$v" | grep -Eq "$VOL_RE" || { echo "skip odd name $v" >&2; continue; }
    t="$P$v"
    if "$DOCKER" volume inspect "$t" >/dev/null 2>&1; then echo "volume $t exists: refusing to overwrite" >&2; return 1; fi
    if [ -n "$P" ]; then lab=(--label aos.drill=1); else lab=(--label aos.box=1 --label "aos.box.id=$(echo "$v" | sed -E 's/^aos-box-([0-9a-f]{16}).*/\1/')"); fi
    "$DOCKER" volume create "${lab[@]}" "$t" >/dev/null
    _h -i -v "$t:/dst" --entrypoint tar "$IMG" -C /dst --numeric-owner -xzpf - < "$f"
    chk=$(mktemp); _manifest "$t" > "$chk"
    if cmp -s "$chk" "$S/volumes/$v.sha256"; then nf=$((nf+$(wc -l < "$chk"))); else echo "MISMATCH in $t" >&2; bad=1; fi
    rm -f "$chk"; nv=$((nv+1))
  done
  [ "$bad" = 0 ] || return 1
  mkdir -p "$RVAR/snapshots"; [ -d "$S/snapshots" ] && cp -an "$S/snapshots/." "$RVAR/snapshots/" 2>/dev/null || true
  if [ -f "$S/skills.tgz" ]; then tar -C "$RVAR" -xzkf "$S/skills.tgz" 2>/dev/null || true; fi
  if [ "$IM" = 1 ] && [ -f "$S/images/images.tar" ]; then "$DOCKER" load -i "$S/images/images.tar" >/dev/null; fi
  echo "computers restore ok: $nv volumes ($nf files checked) -> prefix '${P}'"
}

main() {
  case "${1:-}" in
    backup) bk_backup "${2:?dest dir}";;
    restore) shift; bk_restore "$@";;
    *) sed -n 2,4p "$0" 2>/dev/null || true; exit 2;;
  esac
}
# sourced for tests / the drill: AOS_BK_SOURCED=1 (then only the functions load)
[ "${AOS_BK_SOURCED:-0}" = 1 ] || main "$@"
