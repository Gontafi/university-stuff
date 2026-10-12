# Assignment 3. Registration Service resilience

Synthetic AITU student services platform, Registration Service only. Go service, PostgreSQL 17,
two-node minikube cluster (profile `ftr-asg3`).

```sh
minikube start -p ftr-asg3 --driver=docker --nodes=2 --cpus=2 --memory=2200
cd registration && CGO_ENABLED=0 go build -trimpath -o registration . && cd ..
minikube -p ftr-asg3 image build -t registration:asg3 registration
minikube -p ftr-asg3 kubectl -- apply -f k8s/postgres.yaml
minikube -p ftr-asg3 kubectl -- apply -f k8s/registration.yaml -f k8s/backup.yaml

python3 scripts/availability.py               # Part B
python3 scripts/experiment.py one-replica     # Part D, also all-replicas, database, rolling-update
bash scripts/dr_drill.sh                      # Part E, backup, destroy volume, restore
```

Results are in `results/`: raw request samples and pod snapshots (`<scenario>.json`),
Kubernetes events, the availability calculation and the recovery drill logs.
