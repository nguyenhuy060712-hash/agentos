#!/bin/bash
# GĐ9.Z: full backend suite, ONE PROCESS PER TEST FILE (a single pytest process that imports every heavy library
# - onnxruntime OCR, pyarrow, presidio, langgraph - outgrew the 6 GB address-space cap after ~650 tests and aborted).
# Each file still runs under a RAM cap. Usage: bash tests/run_all.sh [pattern]   (from agentos-local; local server must be up)
cd "$(dirname "$0")/.." || exit 2
PY=${PY:-/home/ad/.venvs/agentos/bin/python}; CAP=${CAP:-6000000}; pat=${1:-test_*.py}
pass=0; fail=0; skip=0; bad=()
for f in $(ls tests/$pat | sort); do
  out=$( (ulimit -v "$CAP"; timeout 1800 "$PY" -m pytest -q -p no:randomly -p no:cacheprovider "$f" 2>&1) | tail -1 )
  p=$(grep -oE '[0-9]+ passed' <<<"$out" | grep -oE '[0-9]+'); x=$(grep -oE '[0-9]+ (failed|error)' <<<"$out" | grep -oE '[0-9]+' | paste -sd+ | bc 2>/dev/null)
  pass=$((pass + ${p:-0}))
  # pytest exit code 5 = every test in the file skipped (e.g. *_real.py without AOS_REAL_SANDBOX=1): counts as skipped, not failed
  if [ -z "$p" ] && [ -z "$x" ] && grep -qE '[0-9]+ skipped' <<<"$out"; then skip=$((skip + 1)); echo "$f :: $out"; continue; fi
  if [ -n "$x" ] && [ "$x" != 0 ] || ! grep -q passed <<<"$out"; then fail=$((fail + ${x:-1})); bad+=("$f :: $out"); fi
  echo "$f :: $out"
done
echo "TOTAL passed=$pass failed=$fail skipped_files=$skip"; for b in "${bad[@]}"; do echo "FAIL $b"; done
[ "$fail" = 0 ]
