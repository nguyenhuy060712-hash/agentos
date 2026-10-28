#!/usr/bin/env bash
# GĐ4 CI gate for the /chat front-end UI (run on the box before every deploy; exit 0 = shippable).
#  1. typecheck + production build          4. licence allow-list on production deps (inside check-a7)
#  2. unit tests (vitest)                   5. npm audit: 0 high / 0 critical (all deps, dev included)
#  3. bundle gate (check-a7): no provider keys, no `new AssistantCloud`, no assistant-api.com / auiusercontent.com,
#     no pipecat/livekit, chunk sizes, lazy chunks
# Output is also written to shots/gd4/ci-<timestamp>.txt. No network calls except the npm registry for `npm audit`.
set -uo pipefail; cd "$(dirname "$0")/.."
mkdir -p shots/gd4; LOG="shots/gd4/ci-$(date +%Y%m%d_%H%M).txt"; exec > >(tee "$LOG") 2>&1
step() { echo; echo "== $1"; }; fail=0
step "1 build (tsc -b && vite build)"; npm run -s build > /tmp/aos-ci-build.log 2>&1 && echo "build OK" || { tail -30 /tmp/aos-ci-build.log; echo "build FAIL"; fail=1; }
step "2 vitest"; npx vitest run 2>&1 | tail -6; [ "${PIPESTATUS[0]}" -eq 0 ] || fail=1
step "3+4 bundle + licence gate (check-a7)"; ./scripts/check-a7.sh || fail=1
step "5 npm audit (fail on high/critical)"
aj=$(npm audit --json 2>/dev/null); echo "$aj" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const v=JSON.parse(s).metadata.vulnerabilities;console.log(JSON.stringify(v));process.exit(v.high+v.critical>0?1:0)})' || fail=1
step "versions"; node -e 'const l=require("./package-lock.json").packages;for(const p of ["@assistant-ui/react","@assistant-ui/react-langgraph","@assistant-ui/react-markdown","@assistant-ui/generative-ui","@base-ui/react","react","vite","katex","mermaid","shiki"])console.log(p.padEnd(32),l["node_modules/"+p]?.version)'
echo; [ $fail -eq 0 ] && echo "CI: PASS" || echo "CI: FAIL"; echo "log: $LOG"; exit $fail
