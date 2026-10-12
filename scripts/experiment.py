"""Failure experiments for the Registration Service (Assignment 3).

Runs a small constant load against the NodePort service, injects one failure with kubectl,
watches the pods and writes raw samples plus a summary to results/<scenario>.json.

usage: python3 scripts/experiment.py one-replica|all-replicas|database|rolling-update
"""
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROFILE = "ftr-asg3"
NS = "asg3"
URL = "http://192.168.67.2:30090"
SLA = 0.5            # max API latency from the requirements, seconds
WORKERS = 4
PAUSE = 0.05
ROOT = Path(__file__).resolve().parent.parent


def kubectl(*args, check=True):
    r = subprocess.run(["minikube", "-p", PROFILE, "kubectl", "--", "-n", NS, *args],
                       capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout


def call(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()
            return resp.status, resp.headers.get("X-Pod", ""), start, time.monotonic()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("X-Pod", ""), start, time.monotonic()
    except OSError:
        return 0, "", start, time.monotonic()


class Run:
    def __init__(self, scenario):
        self.scenario = scenario
        self.tag = f"{scenario[:3].upper()}{int(time.time()) % 100000}"
        self.t0 = time.monotonic()
        self.samples = []
        self.pods = []
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def t(self, x=None):
        return round((time.monotonic() if x is None else x) - self.t0, 3)

    def worker(self, n):
        i = 0
        while not self.stop.is_set():
            i += 1
            if i % 2:
                sid = f"{self.tag}-{n}-{i}"
                status, pod, a, b = call("POST", "/registrations", {"student_id": sid, "course": "FT-301"})
                kind = "write"
            else:
                sid = ""
                status, pod, a, b = call("GET", f"/registrations?student_id={self.tag}-{n}-1")
                kind = "read"
            with self.lock:
                self.samples.append({"t": self.t(a), "end": self.t(b), "latency": round(b - a, 4),
                                     "status": status, "pod": pod, "kind": kind, "student": sid})
            self.stop.wait(PAUSE)

    def watcher(self):
        while not self.stop.is_set():
            at = self.t()
            try:
                items = json.loads(kubectl("get", "pods", "-o", "json"))["items"]
            except Exception:
                items = []
            snap = []
            for p in items:
                ready = any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
                snap.append({"name": p["metadata"]["name"], "app": p["metadata"]["labels"].get("app"),
                             "ready": ready, "deleting": "deletionTimestamp" in p["metadata"]})
            self.pods.append({"t": at, "pods": snap})
            self.stop.wait(0.25)

    def ready_count(self, snap, app="registration"):
        return sum(1 for p in snap["pods"] if p["app"] == app and p["ready"] and not p["deleting"])


def main():
    scenario = sys.argv[1]
    run = Run(scenario)
    threads = [threading.Thread(target=run.worker, args=(n,)) for n in range(WORKERS)]
    threads.append(threading.Thread(target=run.watcher))
    for th in threads:
        th.start()
    events = {}
    try:
        time.sleep(5)
        before = [p["metadata"]["name"] for p in json.loads(kubectl("get", "pods", "-l", "app=registration", "-o", "json"))["items"]]
        events["pods_before"] = before
        events["injected"] = run.t()
        if scenario == "one-replica":
            kubectl("delete", "pod", before[0], "--grace-period=0", "--force", "--wait=false")
            events["target"] = before[0]
        elif scenario == "all-replicas":
            kubectl("delete", "pod", *before, "--grace-period=0", "--force", "--wait=false")
            events["target"] = before
        elif scenario == "database":
            kubectl("scale", "statefulset/postgres", "--replicas=0")
            time.sleep(20)
            events["cleared"] = run.t()
            kubectl("scale", "statefulset/postgres", "--replicas=1")
        elif scenario == "rolling-update":
            kubectl("rollout", "restart", "deployment/registration")
            kubectl("rollout", "status", "deployment/registration", "--timeout=120s")
            events["rollout_done"] = run.t()
        # wait until both replicas are ready again, then observe 10 s more
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if run.pods and run.ready_count(run.pods[-1]) == 2 and run.t() > events.get("cleared", events["injected"]) + 3:
                break
        time.sleep(10)
    finally:
        run.stop.set()
        for th in threads:
            th.join()
    events["finished"] = run.t()
    summary = summarize(run, events)
    out = {"scenario": scenario, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "tag": run.tag, "events": events, "summary": summary, "samples": run.samples, "pods": run.pods}
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / f"{scenario}.json").write_text(json.dumps(out, indent=1))
    kubectl_events = kubectl("get", "events", "--sort-by=.lastTimestamp")
    (ROOT / "results" / f"{scenario}-events.txt").write_text(kubectl_events)
    print(json.dumps(summary, indent=2))


def summarize(run, ev):
    inj = ev["injected"]
    clear = ev.get("cleared", inj)
    s = sorted(run.samples, key=lambda x: x["t"])
    bad = lambda x: x["status"] != 200 or x["latency"] > SLA
    after = [x for x in s if x["t"] >= inj]
    failed = [x for x in after if x["status"] != 200]
    slow = [x for x in after if x["status"] == 200 and x["latency"] > SLA]
    first_bad = next((x for x in after if bad(x)), None)
    last_bad = max((x for x in after if bad(x)), key=lambda x: x["t"], default=None)
    # first moment Kubernetes reacted: the watcher sees fewer ready registration pods
    k8s_detect = next((p["t"] for p in run.pods if p["t"] >= inj and run.ready_count(p) < 2), None)
    replacement = None
    names_before = set(ev["pods_before"])
    for p in run.pods:
        if p["t"] >= inj and any(q["app"] == "registration" and q["name"] not in names_before for q in p["pods"]):
            replacement = p["t"]
            break
    all_ready = next((p["t"] for p in run.pods if p["t"] >= clear + 0.5 and run.ready_count(p) == 2
                      and (ev.get("cleared") or k8s_detect is None or p["t"] > k8s_detect)), None)
    service_ok = next((x["end"] for x in s if last_bad and x["t"] > last_bad["t"] and not bad(x)), None)
    lat = sorted(x["latency"] for x in after if x["status"] == 200)
    writes_ok = [x["student"] for x in s if x["kind"] == "write" and x["status"] == 200]
    writes_failed = [x["student"] for x in s if x["kind"] == "write" and x["status"] != 200]
    rows = db_rows(run.tag)
    pods_seen = sorted({x["pod"] for x in s if x["pod"]})
    return {
        "requests_after_injection": len(after),
        "failed_requests": len(failed),
        "slow_requests_over_500ms": len(slow),
        "user_visible_errors_pct": round(100 * (len(failed) + len(slow)) / len(after), 2) if after else None,
        "service_detection_s": round(first_bad["end"] - inj, 3) if first_bad else None,
        "k8s_detection_s": round(k8s_detect - inj, 3) if k8s_detect is not None else None,
        "replacement_pod_seen_s": round(replacement - inj, 3) if replacement else None,
        "service_recovered_s": round(service_ok - clear, 3) if service_ok else 0.0,
        "all_replicas_ready_s": round(all_ready - clear, 3) if all_ready else None,
        "outage_window_s": round(last_bad["end"] - first_bad["t"], 3) if first_bad else 0.0,
        "p95_latency_ok_s": lat[max(0, int(len(lat) * 0.95) - 1)] if lat else None,
        "max_latency_ok_s": lat[-1] if lat else None,
        "pods_that_answered": pods_seen,
        "acknowledged_writes": len(writes_ok),
        "acknowledged_writes_missing_in_db": len(set(writes_ok) - rows) if rows is not None else None,
        "failed_writes_found_in_db": len(set(writes_failed) & rows) if rows is not None else None,
    }


def db_rows(tag):
    for _ in range(30):
        r = subprocess.run(["minikube", "-p", PROFILE, "kubectl", "--", "-n", NS, "exec", "postgres-0", "--",
                            "psql", "-U", "registration", "-At", "-c",
                            f"SELECT student_id FROM registrations WHERE student_id LIKE '{tag}-%'"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return set(r.stdout.split())
        time.sleep(2)
    return None


if __name__ == "__main__":
    main()
