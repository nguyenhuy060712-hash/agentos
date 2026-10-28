#!/usr/bin/env bash
# GĐ4 health check for Agent OS on VPS #83, run from the box (read-only: systemctl/df/free/journalctl/curl, no restarts).
# Prints one line per check + a JSON summary to /workspace/backups/vps83/health/last.json; exit 1 if any check fails.
# Checks: services active, disk < 85 %, MemAvailable > 600 MB, newest DB backup < 26 h, /chat 5xx in the last hour (uvicorn access
# log), worker heartbeat timer, public URL reachable through the quick tunnel (/login 200, /chat/ security headers), RAM of
# deepagent-api, showcase site (services, public URL, canonical = current tunnel URL). Never prints secrets: the tunnel password is read from the 600 file and only sent to the tunnel.
set -uo pipefail
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -i "$HOME/.ssh/moza_vps82" -p 30029 root@103.142.24.220)
OUT=/workspace/backups/vps83/health; umask 077; mkdir -p "$OUT"; fail=0; J="{"
chk() { local name="$1" ok="$2" val="$3"; printf '%-34s %-4s %s\n' "$name" "$([ "$ok" = 1 ] && echo OK || echo FAIL)" "$val"; [ "$ok" = 1 ] || fail=1
        J+="\"$name\":{\"ok\":$([ "$ok" = 1 ] && echo true || echo false),\"value\":\"$(echo "$val" | tr -d '"\\' | head -c 200)\"},"; }
R=$("${SSH[@]}" 'set +e
echo "svc $(systemctl is-active deepagent-api deepagent-worker caddy cloudflared-deepagent agentos-worker-health.timer agentos-pgbackup.timer | tr "\n" " ")"
echo "pg $(docker inspect -f {{.State.Status}} lc-postgres 2>/dev/null)"
echo "disk $(df --output=pcent / | tail -1 | tr -dc 0-9)"
echo "mem $(awk "/MemAvailable/{print int(\$2/1024)}" /proc/meminfo)"
f=$(ls -1t /opt/agentos_backups/pg/daily/*.dump 2>/dev/null | head -1); echo "bk $(( ($(date +%s) - $(stat -c %Y "$f")) / 3600 )) $(basename "$f")"
echo "e5xx $(journalctl -u deepagent-api --since -1h --no-pager 2>/dev/null | grep -cE "\" 5[0-9]{2} ")"
echo "c5xx $(journalctl -u deepagent-api --since -1h --no-pager 2>/dev/null | grep -E "\"(GET|POST|PUT|PATCH|DELETE) /(chat|api/(v1|chat|voice|files))" | grep -cE "\" 5[0-9]{2} ")"
echo "rss $(ps -o rss= -p $(systemctl show -p MainPID --value deepagent-api) 2>/dev/null | awk "{print int(\$1/1024)}")"
echo "url $(journalctl -u cloudflared-deepagent --no-pager 2>/dev/null | grep -o "https://[a-z0-9-]*\.trycloudflare\.com" | tail -1)"
echo "head $(git -C /opt/agentos rev-parse --short HEAD)"
echo "ssvc $(systemctl is-active caddy-show cloudflared-show | tr "\n" " ")"
echo "surl $(journalctl -u cloudflared-show --no-pager 2>/dev/null | grep -o "https://[a-z0-9-]*\.trycloudflare\.com" | tail -1)"')
g() { echo "$R" | awk -v k="$1" '$1==k{$1="";sub(/^ /,"");print}'; }
svc=$(g svc); chk services "$([ "$(echo "$svc" | grep -o active | wc -l)" = 6 ] && [ -z "$(echo "$svc" | grep -E 'inactive|failed')" ] && echo 1 || echo 0)" "$svc"
chk postgres_container "$([ "$(g pg)" = running ] && echo 1 || echo 0)" "$(g pg)"
d=$(g disk); chk disk_used_pct "$([ "${d:-100}" -lt 85 ] && echo 1 || echo 0)" "$d %"
m=$(g mem); chk mem_available_mb "$([ "${m:-0}" -gt 600 ] && echo 1 || echo 0)" "$m MB"
b=($(g bk)); chk backup_age_h "$([ "${b[0]:-99}" -lt 26 ] && echo 1 || echo 0)" "${b[0]:-?} h (${b[1]:-none})"
chk http_5xx_last_hour_chat "$([ "$(g c5xx)" = 0 ] && echo 1 || echo 0)" "chat/api $(g c5xx), all $(g e5xx)"
chk api_rss_mb 1 "$(g rss) MB"
chk deployed_head 1 "$(g head)"
U=$(g url); P=$(grep -o 'password=[^ ]*' /workspace/secrets/deepagent_web.txt 2>/dev/null | cut -d= -f2-)
if [ -n "$U" ] && [ -n "$P" ]; then
  t0=$(date +%s%N); c=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -u "moza:$P" "$U/login"); ms=$(( ($(date +%s%N) - t0) / 1000000 ))
  chk tunnel_login "$([ "$c" = 200 ] && echo 1 || echo 0)" "$c in ${ms} ms ($U)"
  H=$(curl -s -D - -o /dev/null -m 20 -u "moza:$P" "$U/chat/")
  miss=""; for h in content-security-policy strict-transport-security x-frame-options x-content-type-options referrer-policy permissions-policy cross-origin-opener-policy; do echo "$H" | grep -qi "^$h:" || miss+="$h "; done
  chk chat_security_headers "$([ -z "$miss" ] && echo 1 || echo 0)" "${miss:-all present}"
else chk tunnel_login 0 "no tunnel URL or credentials"; fi
# Showcase site (public static, caddy-show :8093 + cloudflared-show quick tunnel). Its URL changes when the tunnel restarts;
# the site is built with SITE_URL = that URL, so a new URL means a rebuild + upload (see /workspace/agentos-show/README.md).
ss=$(g ssvc); chk show_services "$([ "$(echo "$ss" | grep -o active | wc -l)" = 2 ] && [ -z "$(echo "$ss" | grep -E 'inactive|failed')" ] && echo 1 || echo 0)" "$ss"
SU=$(g surl)
if [ -n "$SU" ]; then
  c1=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$SU/"); c2=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$SU/en/")
  canon=$(curl -s -m 20 "$SU/" | grep -o 'rel="canonical" href="[^"]*"' | cut -d'"' -f4)
  chk show_public_url "$([ "$c1" = 200 ] && [ "$c2" = 200 ] && echo 1 || echo 0)" "/ $c1, /en/ $c2 ($SU)"
  chk show_canonical_matches "$([ "${canon%/}" = "$SU" ] && echo 1 || echo 0)" "${canon:-none}"
else chk show_public_url 0 "no show tunnel URL"; fi
J+="\"at\":\"$(TZ=Asia/Bangkok date '+%F %T %z')\",\"pass\":$([ $fail = 0 ] && echo true || echo false)}"
echo "$J" > "$OUT/last.json"; cp "$OUT/last.json" "$OUT/$(TZ=Asia/Bangkok date +%Y%m%d_%H%M).json"
ls -1t "$OUT"/20*.json 2>/dev/null | tail -n +200 | xargs -r rm -f
echo; [ $fail = 0 ] && echo "HEALTH: PASS" || echo "HEALTH: FAIL"; exit $fail
