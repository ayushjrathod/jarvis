#!/usr/bin/env bash
# Phase A acceptance smoke test. Expects the dispatcher running on :8765.
#   .venv/bin/python -m dispatcher.main   (in another terminal)
set -uo pipefail
BASE=${1:-http://127.0.0.1:8765}
pass=0; fail=0
ok()   { echo "PASS: $1"; pass=$((pass+1)); }
bad()  { echo "FAIL: $1"; fail=$((fail+1)); }

echo "== health =="
curl -sf "$BASE/health" && echo || bad "health endpoint"

echo "== quick question (SSE stream) =="
out=$(curl -sN --max-time 180 -X POST "$BASE/task" \
  -H 'content-type: application/json' \
  -d '{"text": "what is the capital of France?", "source": "api"}')
echo "$out" | tail -5
# join the streamed deltas first: "Paris" may arrive split across SSE lines
answer=$(echo "$out" | python3 -c '
import json, sys
print("".join(json.loads(l[5:]).get("text") or "" for l in sys.stdin
              if l.startswith("data:")))')
echo "$answer" | grep -qi paris && ok "quick answer contains Paris" || bad "quick answer"
echo "$out" | grep -q 'event: done' && ok "quick stream closed with done" || bad "quick done event"

echo "== agentic task (202 + background run) =="
resp=$(curl -s -X POST "$BASE/task" -H 'content-type: application/json' \
  -d '{"text": "summarize the files in vault/notes into vault/briefs/test.md", "source": "api", "metadata": {"task_type": "summarize"}}')
echo "$resp"
tid=$(echo "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')
[ -n "$tid" ] && ok "got task_id $tid" || bad "agentic submit"

echo "waiting for completion (max 10 min)..."
for i in $(seq 1 120); do
  status=$(curl -s "$BASE/task/$tid" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  [ "$status" = done ] || [ "$status" = failed ] && break
  sleep 5
done
echo "final status: $status"
[ "$status" = done ] && ok "agentic task done" || bad "agentic task status=$status"
[ -f vault/briefs/test.md ] && ok "output file exists" || bad "vault/briefs/test.md missing"
curl -s "$BASE/task/$tid" | python3 -m json.tool | grep -E '"(status|cost_usd|model)"' | head -8

echo "== refusal -> fallback (simulated) =="
resp=$(curl -s -X POST "$BASE/task" -H 'content-type: application/json' \
  -d '{"text": "write one line into vault/briefs/refusal-test.md saying hello", "mode": "agentic", "metadata": {"simulate_refusal": true, "task_type": "summarize"}}')
tid=$(echo "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')
for i in $(seq 1 120); do
  status=$(curl -s "$BASE/task/$tid" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  [ "$status" = done ] || [ "$status" = failed ] && break
  sleep 5
done
runs=$(curl -s "$BASE/task/$tid" | python3 -c 'import json,sys; t=json.load(sys.stdin); print(",".join(r["status"] for r in t["runs"]))')
echo "runs: $runs (task: $status)"
[ "${runs%%,*}" = refused ] && ok "attempt 1 logged as refused" || bad "refusal not logged"
echo "$runs" | grep -q ',done' && ok "fallback attempt succeeded" || bad "fallback attempt"

echo
echo "== $pass passed, $fail failed =="
exit $((fail > 0))
