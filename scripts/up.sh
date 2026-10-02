#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PROFILE="${PROFILE:-university-ft}"
k() { minikube -p "$PROFILE" kubectl -- "$@"; }
minikube start -p "$PROFILE" --driver=docker --nodes=3 --cpus=2 --memory=2200
minikube -p "$PROFILE" image build --all -t university:local .
for mode in baseline ft; do
  python3 scripts/render.py "$mode" | k apply -f -
  k -n "university-$mode" rollout status statefulset/postgres --timeout=180s
  for role in student payment records gateway; do
    k -n "university-$mode" rollout status "deployment/$role" --timeout=180s
  done
done
bash scripts/backup.sh backup
minikube -p "$PROFILE" service gateway -n university-ft --url
minikube -p "$PROFILE" service gateway -n university-baseline --url
