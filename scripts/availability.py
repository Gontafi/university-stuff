"""Part B: availability of the redundant subsystems and of the whole request path."""
import json
from pathlib import Path

A = {"lb": 0.9995, "gateway": 0.9990, "auth": 0.995, "course": 0.995, "registration": 0.995, "db": 0.997}
TARGET = 0.999


def parallel(*r):
    q = 1.0
    for x in r:
        q *= 1 - x
    return 1 - q


def chain(parts):
    a = 1.0
    for x in parts.values():
        a *= x
    return a


given = {
    "lb": A["lb"],
    "gateway": A["gateway"],
    "auth": parallel(A["auth"], A["auth"]),
    "course": parallel(A["course"], A["course"]),
    "registration": parallel(A["registration"], A["registration"]),
    "db": parallel(A["db"], A["db"]),
}
base = chain(given)
lines = []
for k, v in given.items():
    lines.append(f"{k:14s} {v:.8f}")
lines.append(f"{'end-to-end':14s} {base:.8f}  target {TARGET}  met={base >= TARGET}")
lines.append(f"downtime per year: {(1 - base) * 8760:.2f} h (target allows {(1 - TARGET) * 8760:.2f} h)")
lines.append("")
lines.append("unavailability share per block (1 - A) / sum:")
total_u = sum(1 - v for v in given.values())
for k, v in sorted(given.items(), key=lambda kv: kv[1]):
    lines.append(f"  {k:14s} {(1 - v) * 100:.5f}%  share {(1 - v) / total_u * 100:5.1f}%")
lines.append("")
lines.append("what-if, one change at a time:")
changes = {
    "second API gateway": dict(given, gateway=parallel(A["gateway"], A["gateway"])),
    "second load balancer": dict(given, lb=parallel(A["lb"], A["lb"])),
    "third replica of each service": dict(given, **{s: parallel(A[s], A[s], A[s]) for s in ("auth", "course", "registration")}),
    "third database node": dict(given, db=parallel(A["db"], A["db"], A["db"])),
    "second gateway and second LB": dict(given, gateway=parallel(A["gateway"], A["gateway"]), lb=parallel(A["lb"], A["lb"])),
}
whatif = {}
for name, parts in changes.items():
    a = chain(parts)
    whatif[name] = a
    lines.append(f"  {name:32s} {a:.8f}  gain {(a - base) * 100:+.5f} pp  met={a >= TARGET}")
text = "\n".join(lines)
print(text)
out = Path(__file__).resolve().parent.parent / "results"
out.mkdir(exist_ok=True)
(out / "availability.txt").write_text(text + "\n")
(out / "availability.json").write_text(json.dumps({"given": given, "end_to_end": base, "whatif": whatif}, indent=2))
