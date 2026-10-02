import json
import os
import sys

mode = sys.argv[1]
if mode not in ("baseline", "ft"):
    raise SystemExit("usage: render.py baseline|ft")
profile = os.environ.get("PROFILE", "university-ft")
ns = "university-" + mode
ft = mode == "ft"
items = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}]


def resource(api, kind, name, **fields):
    obj = {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": ns}, **fields}
    items.append(obj)
    return obj


resource("v1", "Secret", "university", type="Opaque", stringData={
    "POSTGRES_PASSWORD": "university-demo-password",
    "DATABASE_URL": "postgres://university:university-demo-password@postgres:5432/university?sslmode=disable&pool_max_conns=10",
    "FAULT_TOKEN": "demo-fault-token",
})
items.append({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": ns + "-db"}, "spec": {
    "capacity": {"storage": "1Gi"}, "accessModes": ["ReadWriteOnce"], "storageClassName": "",
    "persistentVolumeReclaimPolicy": "Retain", "hostPath": {"path": "/data/" + ns + "/postgres", "type": "DirectoryOrCreate"},
    "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": [profile]}]}]}},
    "claimRef": {"namespace": ns, "name": "postgres-data"},
}})
resource("v1", "PersistentVolumeClaim", "postgres-data", spec={
    "accessModes": ["ReadWriteOnce"], "storageClassName": "", "volumeName": ns + "-db", "resources": {"requests": {"storage": "1Gi"}},
})
resource("v1", "Service", "postgres", spec={"selector": {"app": "postgres"}, "ports": [{"port": 5432, "targetPort": 5432}]})
resource("apps/v1", "StatefulSet", "postgres", spec={
    "serviceName": "postgres", "replicas": 1, "selector": {"matchLabels": {"app": "postgres"}},
    "template": {"metadata": {"labels": {"app": "postgres"}}, "spec": {
        "nodeSelector": {"kubernetes.io/hostname": profile},
        "containers": [{"name": "postgres", "image": "postgres:17-alpine", "ports": [{"containerPort": 5432}],
            "env": [{"name": "POSTGRES_USER", "value": "university"}, {"name": "POSTGRES_DB", "value": "university"},
                    {"name": "POSTGRES_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "university", "key": "POSTGRES_PASSWORD"}}},
                    {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"}],
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "384Mi"}},
            "readinessProbe": {"exec": {"command": ["pg_isready", "-U", "university"]}, "periodSeconds": 2},
            "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}]}],
        "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "postgres-data"}}],
    }},
})

for role in ("gateway", "student", "payment", "records"):
    pod = {
        "terminationGracePeriodSeconds": 8,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001},
        "containers": [{"name": role, "image": "university:local", "imagePullPolicy": "Never",
            "ports": [{"containerPort": 8080}],
            "envFrom": [{"secretRef": {"name": "university"}}],
            "env": [{"name": "ROLE", "value": role}, {"name": "MODE", "value": mode}],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}},
        }],
    }
    if ft:
        pod["affinity"] = {
            "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": [profile + "-m02", profile + "-m03"]}]}]}},
            "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": {"app": role}}, "topologyKey": "kubernetes.io/hostname"}]},
        }
        container = pod["containers"][0]
        container["readinessProbe"] = {"httpGet": {"path": "/health/ready", "port": 8080}, "periodSeconds": 2, "timeoutSeconds": 1, "failureThreshold": 1}
        container["livenessProbe"] = {"httpGet": {"path": "/health/live", "port": 8080}, "periodSeconds": 3, "timeoutSeconds": 1, "failureThreshold": 2}
        container["startupProbe"] = {"httpGet": {"path": "/health/live", "port": 8080}, "periodSeconds": 2, "failureThreshold": 90}
        resource("policy/v1", "PodDisruptionBudget", role, spec={"minAvailable": 1, "selector": {"matchLabels": {"app": role}}})
    else:
        pod["nodeSelector"] = {"kubernetes.io/hostname": profile + "-m02"}
    resource("apps/v1", "Deployment", role, spec={
        "replicas": 2 if ft else 1, "selector": {"matchLabels": {"app": role}},
        "template": {"metadata": {"labels": {"app": role}}, "spec": pod},
    })
    spec = {"selector": {"app": role}, "ports": [{"port": 8080, "targetPort": 8080}]}
    if role == "gateway":
        spec["type"] = "NodePort"
        spec["ports"][0]["nodePort"] = 30080 if ft else 30081
    resource("v1", "Service", role, spec=spec)

if ft:
    backup_pod = {
        "restartPolicy": "OnFailure", "nodeSelector": {"kubernetes.io/hostname": profile + "-m03"},
        "containers": [{"name": "backup", "image": "postgres:17-alpine",
            "envFrom": [{"secretRef": {"name": "university"}}],
            "command": ["sh", "-ec", 'export PGPASSWORD="$POSTGRES_PASSWORD"; pg_dump -h postgres -U university -d university -Fc -f /backups/latest.dump.tmp; mv /backups/latest.dump.tmp /backups/latest.dump; sha256sum /backups/latest.dump > /backups/latest.sha256'],
            "volumeMounts": [{"name": "backups", "mountPath": "/backups"}],
            "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}},
        }],
        "volumes": [{"name": "backups", "hostPath": {"path": "/data/university-ft-backups", "type": "DirectoryOrCreate"}}],
    }
    resource("batch/v1", "CronJob", "postgres-backup", spec={
        "schedule": "*/5 * * * *", "concurrencyPolicy": "Forbid", "successfulJobsHistoryLimit": 2, "failedJobsHistoryLimit": 2,
        "jobTemplate": {"spec": {"backoffLimit": 2, "template": {"spec": backup_pod}}},
    })

json.dump({"apiVersion": "v1", "kind": "List", "items": items}, sys.stdout, indent=2)
print()
