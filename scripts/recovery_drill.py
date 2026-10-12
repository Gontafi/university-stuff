"""Assignment 4: recovery drill on synthetic files.

Everything happens in a temporary directory with generated research files. The "encryption"
is an XOR overwrite of those test files only. The drill replays the incident timeline,
then runs the recovery steps from the plan and measures them.
"""
import hashlib
import json
import os
import random
import shutil
import stat
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = 1200
RATE_ALERT = 100          # rule: more than 100 changed files in one minute is an alert
log_lines = []


def log(clock, text):
    line = f"{clock}  {text}"
    log_lines.append(line)
    print(line)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ImmutableStore:
    """Write-once snapshot store with a retention period, like object lock on backup storage."""

    def __init__(self, base, retention_days=7):
        self.base = base
        self.retention = retention_days * 86400
        self.snapshots = {}

    def put(self, name, src, now):
        dst = self.base / name
        shutil.copytree(src, dst)
        manifest = {p.name: sha(p) for p in sorted(dst.iterdir())}
        (dst / "MANIFEST.json").write_text(json.dumps(manifest))
        for p in dst.iterdir():
            p.chmod(stat.S_IRUSR)
        dst.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.snapshots[name] = {"created": now, "until": now + self.retention, "files": len(manifest)}
        return manifest

    def delete(self, name, now):
        if now < self.snapshots[name]["until"]:
            raise PermissionError(f"snapshot {name} is locked until retention ends")
        raise NotImplementedError

    def manifest(self, name):
        return json.loads((self.base / name / "MANIFEST.json").read_text())


def minute(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def main():
    rnd = random.Random(2026)
    tmp = Path(tempfile.mkdtemp(prefix="asg4-drill-"))
    data, store_dir, evidence, restored = tmp / "research", tmp / "store", tmp / "evidence", tmp / "restored"
    for d in (data, store_dir, evidence):
        d.mkdir()
    store = ImmutableStore(store_dir)
    result = {}

    for i in range(FILES):
        rows = "\n".join(f"{i},{j},{rnd.gauss(20, 5):.4f}" for j in range(60))
        (data / f"sample_{i:04d}.csv").write_text("sample,step,value\n" + rows + "\n")
    log("08:55", f"research server holds {FILES} synthetic data files")

    store.put("snap-0900", data, minute("09:00"))
    log("09:00", "immutable snapshot snap-0900 taken (every 5 min)")
    store.put("snap-0905", data, minute("09:05"))
    log("09:05", "immutable snapshot snap-0905 taken")

    legit = [data / f"sample_{i:04d}.csv" for i in range(10)]
    for p in legit:
        p.write_text(p.read_text() + "edited,by,researcher\n")
    log("09:06", "researcher edits 10 files after the last snapshot (this work is at risk)")

    changed = rnd.sample(sorted(data.iterdir()), 320)
    for p in changed:
        p.write_bytes(p.read_bytes() + b"#")
    alert = len(changed) > RATE_ALERT
    log("09:07", f"320 files modified in one minute, rule threshold {RATE_ALERT}: alert={alert}")
    result["detected_at"] = "09:07" if alert else None

    key = bytes(rnd.getrandbits(8) for _ in range(32))
    for p in data.iterdir():
        b = p.read_bytes()
        p.write_bytes(bytes(c ^ key[n % 32] for n, c in enumerate(b)))
    log("09:10", f"{FILES} files overwritten with XOR (simulated encryption)")

    for name in list(store.snapshots):
        try:
            store.delete(name, minute("09:12"))
            log("09:12", f"backup {name} deleted")
        except PermissionError as e:
            log("09:12", f"backup deletion attempt refused: {e}")
    result["snapshots_left"] = len(store.snapshots)

    # recovery steps, timed for real
    steps = {}
    t = time.perf_counter()
    os.chmod(data, stat.S_IRUSR | stat.S_IXUSR)        # isolate: no more writes to the data volume
    steps["isolate"] = time.perf_counter() - t

    t = time.perf_counter()
    evidence_manifest = {p.name: sha(p) for p in sorted(data.iterdir())}
    (evidence / "encrypted_hashes.json").write_text(json.dumps(evidence_manifest))
    (evidence / "timeline.txt").write_text("\n".join(log_lines))
    steps["preserve_evidence"] = time.perf_counter() - t

    t = time.perf_counter()
    snap = "snap-0905"
    shutil.copytree(store_dir / snap, restored)
    restored.chmod(stat.S_IRWXU)
    for p in restored.iterdir():
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    (restored / "MANIFEST.json").unlink()
    steps["restore"] = time.perf_counter() - t

    t = time.perf_counter()
    manifest = store.manifest(snap)
    bad = [n for n, h in manifest.items() if sha(restored / n) != h]
    steps["verify"] = time.perf_counter() - t

    lost = [p.name for p in legit if b"edited,by,researcher" not in (restored / p.name).read_bytes()]
    log("09:18", f"recovery from {snap}: {len(manifest) - len(bad)} of {len(manifest)} files match SHA-256")
    log("09:18", f"researcher edits lost (made after the snapshot): {len(lost)} files")
    for k, v in steps.items():
        log("drill", f"{k}: {v * 1000:.1f} ms")

    result.update({
        "files": FILES, "restored_ok": len(manifest) - len(bad), "restored_bad": len(bad),
        "snapshot_used": snap, "rpo_min": (minute("09:07") - minute("09:05")) // 60,
        "lost_legit_edits": len(lost), "steps_ms": {k: round(v * 1000, 1) for k, v in steps.items()},
        "data_mb": round(sum(p.stat().st_size for p in restored.iterdir()) / 1e6, 2),
    })
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results/recovery_drill.json").write_text(json.dumps(result, indent=2))
    (ROOT / "results/recovery_drill.log").write_text("\n".join(log_lines) + "\n")

    for root, dirs, files in os.walk(tmp):
        os.chmod(root, 0o700)
        for f in files:
            os.chmod(os.path.join(root, f), 0o600)
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
