#!/usr/bin/env bash
# Showcase helper for VPS #83 (run inside WSL). Never prints secrets.
#   bash scripts/vps_show.sh status          -> current show tunnel URL + service state (read-only)
#   bash scripts/vps_show.sh deploy <dist>   -> backup /opt/agentos-show to .prev_<ts>, then rsync dist/ into it
set -euo pipefail
ROOT="/mnt/d/Du an khac X/Agent OS - Langchain"
KEY="$HOME/.ssh/moza_vps82"
[ -s "$KEY" ] || install -d -m700 "$HOME/.ssh" && [ -s "$KEY" ] || install -m600 "$ROOT/_handoff/handoff_claude_code_20261021_1826/secrets/ssh/moza_vps82" "$KEY"
# ssh splits UserKnownHostsFile on spaces, so copy the saved host keys to a space-free path.
KH="$HOME/.ssh/known_hosts_vps83"
[ -s "$KH" ] || install -m600 "$ROOT/_handoff/handoff_claude_code_20261021_1826/secrets/ssh/known_hosts" "$KH"
OPTS="-o BatchMode=yes -o ConnectTimeout=15 -o UserKnownHostsFile=$KH -i $KEY -p 30029"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -o "UserKnownHostsFile=$KH" -i "$KEY" -p 30029 root@103.142.24.220)
case "${1:-status}" in
  status)
    "${SSH[@]}" 'journalctl -u cloudflared-show -o cat | grep -o "https://[a-z-]*\.trycloudflare\.com" | tail -1; systemctl is-active caddy-show cloudflared-show; ls -d /opt/agentos-show.prev_* 2>/dev/null | tail -2; df -h /opt | tail -1' ;;
  deploy)
    DIST="${2:?dist dir}"
    TS=$(date +%Y%m%d_%H%M%S)
    "${SSH[@]}" "cp -a /opt/agentos-show /opt/agentos-show.prev_$TS && echo backup=/opt/agentos-show.prev_$TS"
    rsync -a --delete -e "ssh $OPTS" "$DIST/" root@103.142.24.220:/opt/agentos-show/
    echo deployed ;;
esac
