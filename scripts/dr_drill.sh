#!/usr/bin/env bash
# Disaster recovery drill: take a backup, destroy the database volume, rebuild PostgreSQL
# from the YAML in git, restore the latest dump and check the row count.
set -euo pipefail
cd "$(dirname "$0")/.."
k() { minikube -p ftr-asg3 kubectl -- -n asg3 "$@"; }
log=results/dr-drill.log
: > "$log"
start=$(date +%s.%N)
say() { printf '%7.2f s  %s\n' "$(echo "$(date +%s.%N) - $start" | bc)" "$*" | tee -a "$log"; }
count() { k exec postgres-0 -- psql -U registration -At -c "SELECT count(*) FROM registrations"; }

say "rows before backup: $(count)"
job="backup-drill-$(date +%s)"
k create job "$job" --from=cronjob/pg-backup >/dev/null
k wait --for=condition=complete "job/$job" --timeout=120s >/dev/null
say "backup job finished: $(k logs "job/$job" | tail -1)"
file="registration-$(k logs "job/$job" | tail -1 | awk '{print $2}').dump"
rows=$(count)
say "rows in database at backup time: $rows"

start=$(date +%s.%N)
say "DISASTER: delete PostgreSQL StatefulSet and its volume"
# stop scheduled backups first, otherwise a dump of the empty new database becomes latest.dump
k patch cronjob pg-backup -p '{"spec":{"suspend":true}}' >/dev/null
k delete statefulset postgres --wait=true >/dev/null
k delete pvc data-postgres-0 --wait=true >/dev/null
say "database and volume gone"

k apply -f k8s/postgres.yaml >/dev/null
k rollout status statefulset/postgres --timeout=180s >/dev/null
say "empty PostgreSQL running again from k8s/postgres.yaml"

k delete job restore-drill --ignore-not-found >/dev/null
python3 - "$file" <<'PY' | k apply -f - >/dev/null
import json, sys
script = """set -e
cd /backups
sha256sum -c %(f)s.sha256
# the pod is Ready while initdb still runs on a new volume, so wait for a real TCP connection
until psql -h postgres -U registration -d registration -c 'SELECT 1' >/dev/null 2>&1; do sleep 1; done
pg_restore -h postgres -U registration -d registration --clean --if-exists --no-owner %(f)s
echo restored %(f)s
""" % {"f": sys.argv[1]}
job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "restore-drill", "namespace": "asg3"},
       "spec": {"backoffLimit": 0, "template": {"spec": {
           "restartPolicy": "Never", "nodeSelector": {"kubernetes.io/hostname": "ftr-asg3-m02"},
           "containers": [{"name": "restore", "image": "postgres:17-alpine",
                           "env": [{"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": "registration-db", "key": "password"}}}],
                           "command": ["sh", "-c", script],
                           "volumeMounts": [{"name": "backups", "mountPath": "/backups"}]}],
           "volumes": [{"name": "backups", "hostPath": {"path": "/data/asg3-backups", "type": "Directory"}}]}}}}
json.dump(job, sys.stdout)
PY
k wait --for=condition=complete job/restore-drill --timeout=180s >/dev/null
say "restore job: $(k logs job/restore-drill | tr '\n' ' ')"
after=$(count)
say "rows after restore: $after (expected $rows)"
k rollout status deployment/registration --timeout=120s >/dev/null
until [ "$(curl -s -m 1 -o /dev/null -w '%{http_code}' 'http://192.168.67.2:30090/registrations?student_id=S0001')" = 200 ]; do sleep 0.2; done
say "registration API answers 200 again, full recovery done"
k patch cronjob pg-backup -p '{"spec":{"suspend":false}}' >/dev/null
say "scheduled backups resumed"
k delete job restore-drill >/dev/null
[ "$after" = "$rows" ] && say "RESULT: data matches backup" || say "RESULT: MISMATCH"
