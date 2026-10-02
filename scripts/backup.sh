#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE="${PROFILE:-university-ft}"
k() { minikube -p "$PROFILE" kubectl -- -n university-ft "$@"; }
mkdir -p results/runs
if [[ "${1:-backup}" == backup ]]; then
  job="backup-manual-$(date +%s)"
  k create job "$job" --from=cronjob/postgres-backup
  k wait --for=condition=complete "job/$job" --timeout=120s
  k logs "job/$job" > results/runs/backup.log
elif [[ "$1" == verify ]]; then
  job="restore-verify-$(date +%s)"
  python3 - "$job" <<'PY' | k apply -f -
import json, os, sys
profile=os.environ.get("PROFILE","university-ft")
command='''export PGPASSWORD="$POSTGRES_PASSWORD"
sha256sum -c /backups/latest.sha256
dropdb -h postgres -U university --if-exists recovery_verify
createdb -h postgres -U university recovery_verify
pg_restore --exit-on-error --no-owner -h postgres -U university -d recovery_verify /backups/latest.dump
psql -h postgres -U university -d recovery_verify -v ON_ERROR_STOP=1 -c "SELECT count(*) AS balance_mismatches FROM students s WHERE s.paid <> COALESCE((SELECT sum(amount) FROM payments p WHERE p.student_id=s.id),0);"
psql -h postgres -U university -d recovery_verify -v ON_ERROR_STOP=1 -c "DO \\$\\$ BEGIN IF EXISTS (SELECT 1 FROM students s WHERE s.paid <> COALESCE((SELECT sum(amount) FROM payments p WHERE p.student_id=s.id),0)) THEN RAISE EXCEPTION 'inconsistent restored balances'; END IF; END \\$\\$;"
psql -h postgres -U university -d recovery_verify -v ON_ERROR_STOP=1 -c "SELECT (SELECT count(*) FROM students) AS students, (SELECT count(*) FROM payments) AS payments, (SELECT count(*) FROM grades) AS grades;"
'''
json.dump({"apiVersion":"batch/v1","kind":"Job","metadata":{"name":sys.argv[1],"namespace":"university-ft"},"spec":{"backoffLimit":0,"template":{"spec":{"restartPolicy":"Never","nodeSelector":{"kubernetes.io/hostname":profile+"-m03"},"containers":[{"name":"restore","image":"postgres:17-alpine","envFrom":[{"secretRef":{"name":"university"}}],"command":["sh","-ec",command],"volumeMounts":[{"name":"backups","mountPath":"/backups"}]}],"volumes":[{"name":"backups","hostPath":{"path":"/data/university-ft-backups","type":"Directory"}}]}}}},sys.stdout)
PY
  k wait --for=condition=complete "job/$job" --timeout=120s
  k logs "job/$job" | tee results/runs/restore.log
else
  echo 'usage: backup.sh backup|verify' >&2
  exit 1
fi
