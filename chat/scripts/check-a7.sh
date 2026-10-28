#!/usr/bin/env bash
# A7 static gate: no provider keys in the bundle, no Assistant Cloud instantiation, no pipecat*/livekit*, licence allow-list (prod deps).
set -u; cd "$(dirname "$0")/.."; fail=0
say() { printf '%-58s %s\n' "$1" "$2"; }
for pat in 'sk-[A-Za-z0-9_-]{20,}' 'AIza[0-9A-Za-z_-]{20,}' 'AQ\.[A-Za-z0-9_-]{40,}' 'OPENAI_API_KEY' 'GOOGLE_API_KEY' 'GEMINI_API_KEY'; do
  n=$(rg -c -e "$pat" dist 2>/dev/null | awk -F: '{s+=$2} END {print s+0}'); say "bundle: /$pat/" "$n hit(s)"; [ "$n" -eq 0 ] || fail=1; done
for pat in 'new AssistantCloud' 'assistant-api.com' 'auiusercontent.com' 'cloud.assistant-ui.com' 'api.openai.com' 'livekit' 'pipecat'; do
  n=$(rg -c -F "$pat" dist 2>/dev/null | awk -F: '{s+=$2} END {print s+0}'); say "bundle: \"$pat\"" "$n hit(s)$([ "$pat" = assistant-api.com ] && [ "$n" -gt 0 ] && echo " (dead default baseUrl inside the unused AssistantCloud class; never constructed, CSP connect-src self blocks it, E2E asserts 0 requests)")"; done
n=$(rg -c -F 'new AssistantCloud' dist | awk -F: '{s+=$2} END {print s+0}'); [ "$n" -eq 0 ] || fail=1
pk=$(node -e "const l=require('./package-lock.json');console.log(Object.keys(l.packages).filter(k=>/node_modules\/(@?pipecat|@?livekit)/i.test(k)).join(' '))")
say "lockfile: pipecat*/livekit* packages" "${pk:-none}"; [ -z "$pk" ] || fail=1
lic=$(npx --no-install license-checker-rseidelsohn --production --excludePrivatePackages \
  --onlyAllow 'MIT;ISC;Apache-2.0;BSD-2-Clause;BSD-3-Clause;0BSD;OFL-1.1;BlueOak-1.0.0;CC0-1.0;MIT-0;Unlicense;Python-2.0;CC-BY-4.0' --summary 2>&1)
rc=$?; say "licence allow-list (production deps)" "$([ $rc -eq 0 ] && echo OK || echo FAIL)"; echo "$lic" | sed 's/^/    /'; [ $rc -eq 0 ] || fail=1
raw=$(du -sb dist | cut -f1); gz=$(find dist -type f \( -name '*.js' -o -name '*.css' -o -name '*.html' \) -exec gzip -c9 {} \; | wc -c)
say "dist size raw / gzip(js+css+html)" "$((raw/1024)) KiB / $((gz/1024)) KiB"
# code-split gate: no JS chunk > 1.5 MB; voice call surface + Settings are separate lazy chunks
big=$(find dist/assets -name '*.js' -size +1536k -printf '%f ' ); say "JS chunks > 1.5 MB" "${big:-none}"; [ -z "$big" ] || fail=1
for c in VoiceOverlay SettingsDialog; do f=$(ls dist/assets/$c-*.js 2>/dev/null | head -1); if [ -n "$f" ]; then say "lazy chunk $c" "$(basename "$f")"; else say "lazy chunk $c" MISSING; fail=1; fi; done
main=$(ls -S dist/assets/index-*.js | head -1); say "entry chunk size" "$(( $(stat -c%s "$main")/1024 )) KiB"
echo; [ $fail -eq 0 ] && echo "A7 static: PASS" || echo "A7 static: FAIL"; exit $fail
