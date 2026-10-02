#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE="${PROFILE:-university-ft}"
k() { minikube -p "$PROFILE" kubectl -- -n university-ft "$@"; }
test_db="university_test_$(date +%s)_$RANDOM"
forward_log="$(mktemp)"
forward_pid=""
cleanup() {
  if [[ -n "$forward_pid" ]]; then kill "$forward_pid" 2>/dev/null || true; fi
  k exec postgres-0 -- dropdb -U university --if-exists --force "$test_db" || true
  rm -f "$forward_log"
}
k exec postgres-0 -- createdb -U university "$test_db"
trap cleanup EXIT
k port-forward --address=127.0.0.1 pod/postgres-0 15432:5432 > "$forward_log" 2>&1 &
forward_pid=$!
ready=false
for _ in {1..30}; do
  if [[ "$(cat "$forward_log")" == *"Forwarding from"* ]]; then ready=true; break; fi
  if ! kill -0 "$forward_pid" 2>/dev/null; then cat "$forward_log" >&2; exit 1; fi
  sleep 0.2
done
if [[ "$ready" != true ]]; then cat "$forward_log" >&2; exit 1; fi
TEST_DATABASE_URL="postgres://university:university-demo-password@127.0.0.1:15432/$test_db?sslmode=disable" \
  go test -race -count=1 -v ./internal/app -run TestPaymentTransactionsPostgres
