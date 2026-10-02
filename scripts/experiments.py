import argparse
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from metrics import summarize

ROOT = Path(__file__).resolve().parent.parent
PROFILE = os.environ.get("PROFILE", "university-ft")
TOKEN = "demo-fault-token"


def k(mode, *args):
    result = subprocess.run(["minikube", "-p", PROFILE, "kubectl", "--", "-n", "university-" + mode, *args],
                            text=True, capture_output=True, check=True)
    return result.stdout.strip()


def request(base, path, method="GET", payload=None, headers=None):
    start = time.monotonic()
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=6) as response:
            status, raw, response_headers = response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        status, raw, response_headers = error.code, error.read(), dict(error.headers)
    except (OSError, TimeoutError) as error:
        status, raw, response_headers = 0, json.dumps({"error": str(error)}).encode(), {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        data = {"raw": raw.decode(errors="replace")}
    latency = time.monotonic() - start
    degraded = bool(response_headers.get("X-Degraded"))
    return {"status": status, "latency_s": latency, "http_ok": 200 <= status < 400,
            "sla_ok": 200 <= status < 400 and latency <= 1.5,
            "degraded": degraded, "recovered": bool(response_headers.get("X-Recovered")), "data": data}


def must(base, path, method="GET", payload=None, headers=None):
    result = request(base, path, method, payload, headers)
    if not result["http_ok"]:
        raise RuntimeError(f"{path}: {result}")
    return result["data"]


def fault(base, role, delay=0, failures=0):
    return must(base, "/api/faults", "POST", {"role": role, "delay_ms": delay, "failures": failures}, {"X-Fault-Token": TOKEN})


def experiment(mode, scenario, out, base):
    for role in ("student", "payment", "records"):
        fault(base, role)
    student = must(base, "/api/students", "POST", {"name": f"{mode}-{scenario}-{uuid.uuid4().hex[:8]}"})["id"]
    payload = {"student_id": student, "amount": 10000}
    must(base, "/api/payments", "POST", payload, {"Idempotency-Key": uuid.uuid4().hex})
    must(base, "/api/grades", "POST", {"student_id": student, "course": "Fault Tolerance", "grade": 90})
    path = f"/api/transcript?student_id={student}" if scenario == "timeout" else f"/api/payments?student_id={student}"
    for _ in range(12):
        must(base, path)
    stop = threading.Event()
    lock = threading.Lock()
    samples = []
    started = time.monotonic()

    def collect(kind, pause):
        while not stop.is_set():
            at = time.monotonic() - started
            result = request(base, path)
            result.pop("data", None)
            result.update(t=at, end_t=time.monotonic() - started, kind=kind)
            with lock:
                samples.append(result)
            stop.wait(pause)

    probe = threading.Thread(target=collect, args=("probe", 0.25))
    probe.start()
    load_threads = []
    restored = False
    injected = None
    cleared = None
    observations = {"system_state_before": json.loads(k(mode, "get", "pods", "-o", "json"))}
    restore_action = lambda: None
    try:
        time.sleep(3)
        injected = time.monotonic() - started
        if scenario == "crash":
            pods = json.loads(k(mode, "get", "pods", "-l", "app=payment", "-o", "json"))["items"]
            pod = pods[0]["metadata"]["name"]
            observations["target_pod"] = pod
            k(mode, "delete", "pod", pod, "--grace-period=0", "--force", "--wait=false")
            observations["component_detection_s"] = time.monotonic() - started - injected
            observations["component_detection_source"] = "Kubernetes confirmed forced pod deletion; not an independent kubelet detection measurement"
        elif scenario == "database":
            restore_action = lambda: k(mode, "scale", "statefulset/postgres", "--replicas=1")
            k(mode, "scale", "statefulset/postgres", "--replicas=0")
            k(mode, "wait", "--for=delete", "pod/postgres-0", "--timeout=60s")
            observations["component_detection_s"] = time.monotonic() - started - injected
            observations["component_detection_source"] = "observer confirmed PostgreSQL pod removal"
        elif scenario == "timeout":
            restore_action = lambda: fault(base, "records")
            fault(base, "records", delay=2000)
        elif scenario == "node":
            node = PROFILE + "-m02"
            restore_action = lambda: subprocess.run(["docker", "start", node], check=True, capture_output=True)
            subprocess.run(["docker", "stop", "--time=0", node], check=True, capture_output=True)
            observations["target_node"] = node
        elif scenario == "transaction":
            key = uuid.uuid4().hex
            headers = {"Idempotency-Key": key, "X-Fault-Token": TOKEN, "X-Interrupt": "true"}
            result = request(base, "/api/payments", "POST", payload, headers)
            observations["interrupted_payment"] = result
            observations["component_detection_s"] = time.monotonic() - started - injected
            observations["component_detection_source"] = "interrupted payment response observed"
            observations["audit_after_interrupt"] = must(base, f"/api/audit?student_id={student}")
            time.sleep(4)
            result = request(base, "/api/payments", "POST", payload, {"Idempotency-Key": key})
            if not result["http_ok"]:
                raise RuntimeError(f"payment replay did not recover: {result}")
            observations["replayed_payment"] = result
            observations["recovered_transactions"] = 1
        elif scenario == "highload":
            for _ in range(80):
                thread = threading.Thread(target=collect, args=("load", 0))
                thread.start()
                load_threads.append(thread)
        time.sleep(50 if scenario == "node" else 10)
        restore_action()
        restored = True
        cleared = time.monotonic() - started
        if scenario == "highload":
            stop.set()
            for thread in [probe, *load_threads]:
                thread.join()
            stop.clear()
            probe = threading.Thread(target=collect, args=("probe", 0.25))
            probe.start()
        time.sleep(45 if scenario == "node" else 12)
    finally:
        stop.set()
        for thread in [probe, *load_threads]:
            thread.join()
        if not restored:
            restore_action()
    finished = time.monotonic() - started
    if scenario in ("node", "database", "crash"):
        if scenario == "database":
            k(mode, "rollout", "status", "statefulset/postgres", "--timeout=180s")
        for role in ("gateway", "student", "payment", "records"):
            k(mode, "rollout", "status", "deployment/" + role, "--timeout=180s")
    audit = must(base, f"/api/audit?student_id={student}")
    payment_rows = must(base, f"/api/payments?student_id={student}")
    if scenario == "transaction":
        audit["unexpected_payment_rows"] = max(0, len(payment_rows) - 2)
    consistent = all(value == 0 for value in audit.values())
    raw = {"environment": "minikube", "mode": mode, "scenario": scenario,
           "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "student_id": student, "duration_s": finished, "injected_at_s": injected,
           "cleared_at_s": cleared, "observations": observations, "audit": audit,
           "consistent": consistent, "samples": sorted(samples, key=lambda sample: sample["t"])}
    filename = out / f"{mode}-{scenario}.json"
    filename.write_text(json.dumps(raw, indent=2))
    (out / f"{mode}-{scenario}-pods.json").write_text(k(mode, "get", "pods", "-o", "json"))
    (out / f"{mode}-{scenario}-events.json").write_text(k(mode, "get", "events", "-o", "json"))
    for role in ("gateway", "student", "payment", "records"):
        try:
            (out / f"{mode}-{scenario}-{role}.log").write_text(k(mode, "logs", "-l", "app=" + role, "--all-containers", "--prefix", "--tail=5000"))
        except subprocess.CalledProcessError as error:
            (out / f"{mode}-{scenario}-{role}.log").write_text(error.stderr)
    return summarize(raw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", nargs="+", choices=["crash", "database", "timeout", "node", "transaction", "highload"],
                        default=["crash", "database", "timeout", "node", "transaction", "highload"])
    parser.add_argument("--modes", nargs="+", choices=["baseline", "ft"], default=["baseline", "ft"])
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "runs" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    ip = subprocess.run(["minikube", "-p", PROFILE, "ip"], text=True, capture_output=True, check=True).stdout.strip()
    summaries = []
    for scenario in args.scenarios:
        for mode in args.modes:
            print(f"Running {mode}: {scenario}", flush=True)
            base = f"http://{ip}:{30080 if mode == 'ft' else 30081}"
            summary = experiment(mode, scenario, args.out, base)
            summaries.append(summary)
            (args.out / "summary.json").write_text(json.dumps(summaries, indent=2))
            print(json.dumps(summary, indent=2), flush=True)
    subprocess.run(["bash", "scripts/backup.sh", "backup"], cwd=ROOT, check=True)
    subprocess.run(["bash", "scripts/backup.sh", "verify"], cwd=ROOT, check=True)
    print(f"Dataset: {args.out}")


if __name__ == "__main__":
    main()
