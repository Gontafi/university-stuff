"""Assignment 4: incident classification, risk matrix and anomaly analysis.

Reads data/incident.csv and data/anomaly_scores.csv, writes results/analysis.json and a text report.
The stage mapping and the likelihood/impact values are my own judgement, explained in the report.
"""
import csv
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THRESHOLD = 0.75

# attack stage for every event, with the evidence used for the decision
STAGES = {
    "09:01": ("Reconnaissance", "first burst of failed logins from one internal host, probing which accounts exist"),
    "09:03": ("Credential attack", "21 more failures in 2 minutes from the same host, brute force against one account"),
    "09:04": ("Privilege compromise", "success right after 36 failures from the same IP, the password was guessed"),
    "09:07": ("Lateral movement", "the workstation 10.20.4.15 now changes files on the research server"),
    "09:08": ("Data destruction", "CPU 95% one minute after mass changes, encryption process running"),
    "09:10": ("Data destruction", "1,200 files encrypted, research data unusable"),
    "09:12": ("Recovery attack", "attempt to delete backups so the victim cannot restore"),
    "09:13": ("Exfiltration", "new outbound connection to an unknown address from a server that should not browse"),
    "09:15": ("Exfiltration", "database read spike two minutes after the outbound channel opened"),
    "09:18": ("Persistence", "antivirus disabled to keep access and hide further tools, which needs admin rights"),
}
EVENT_RISK = {"09:01": "Medium", "09:03": "High", "09:04": "Critical", "09:07": "Critical", "09:08": "High",
              "09:10": "Critical", "09:12": "Critical", "09:13": "High", "09:15": "High", "09:18": "Critical"}

# likelihood and impact on a 1..5 scale
THREATS = [
    ("Credential compromise", 5, 4, "observed: 36 guesses were enough, no lockout or MFA stopped it"),
    ("Ransomware", 4, 5, "observed: 1,200 files encrypted, and the impact is loss of research data"),
    ("Backup destruction", 3, 5, "attempted at 09:12 with unknown success, and without backups nothing can be restored"),
    ("Data exfiltration", 3, 4, "outbound link and DB spike, volume not confirmed, research and personal data at risk"),
    ("Privilege escalation", 4, 4, "disabling antivirus needs admin rights, so escalation most likely happened"),
]


def level(score):
    if score <= 4:
        return "Low"
    if score <= 9:
        return "Medium"
    if score <= 16:
        return "High"
    return "Critical"


def main():
    events = list(csv.DictReader(open(ROOT / "data/incident.csv")))
    rows = []
    for e in events:
        stage, why = STAGES[e["time"]]
        rows.append({**e, "stage": stage, "evidence": why, "risk": EVENT_RISK[e["time"]]})

    t = lambda s: datetime.strptime(s, "%H:%M")
    first, last = t(events[0]["time"]), t(events[-1]["time"])
    timeline = {
        "attack_duration_min": (last - first).seconds // 60,
        "first_failed_login_to_compromise_min": (t("09:04") - first).seconds // 60,
        "compromise_to_encryption_min": (t("09:10") - t("09:04")).seconds // 60,
        "first_file_change_to_backup_attack_min": (t("09:12") - t("09:07")).seconds // 60,
        "failed_logins_total": 15 + 21,
    }

    risks = []
    for name, l, i, why in THREATS:
        risks.append({"threat": name, "likelihood": l, "impact": i, "score": l * i, "level": level(l * i), "why": why})
    # priority: higher score first, at equal score the threat that blocks recovery comes first
    order = {"Ransomware": 0, "Backup destruction": 1}
    risks.sort(key=lambda r: (-r["score"], order.get(r["threat"], 2)))
    for n, r in enumerate(risks, 1):
        r["priority"] = n

    scores = list(csv.DictReader(open(ROOT / "data/anomaly_scores.csv")))
    anomalies = []
    for s in scores:
        normal, observed = float(s["normal_score"]), float(s["observed_score"])
        anomalies.append({"event": s["event"], "normal": normal, "observed": observed,
                          "anomaly": observed >= THRESHOLD, "margin": round(observed - THRESHOLD, 2),
                          "increase": round(observed - normal, 2), "ratio": round(observed / normal, 1)})
    detected = sum(a["anomaly"] for a in anomalies)
    top = max(anomalies, key=lambda a: (a["observed"], a["increase"]))
    ai = {"threshold": THRESHOLD, "detected": detected, "total": len(anomalies),
          "rate_pct": round(100 * detected / len(anomalies), 1), "highest": top["event"],
          "lowest_margin": min(anomalies, key=lambda a: a["margin"])["event"], "items": anomalies}

    out = {"events": rows, "timeline": timeline, "risks": risks, "ai": ai}
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results/analysis.json").write_text(json.dumps(out, indent=2))
    lines = ["Incident classification"]
    lines += [f"  {r['time']}  {r['stage']:22s} {r['risk']:8s} {r['event']}" for r in rows]
    lines += ["", "Timeline"] + [f"  {k}: {v}" for k, v in timeline.items()]
    lines += ["", "Risk matrix (Risk = Likelihood x Impact)"]
    lines += [f"  {r['priority']}. {r['threat']:22s} L={r['likelihood']} I={r['impact']} R={r['score']:2d} {r['level']}" for r in risks]
    lines += ["", f"AI detection, threshold {THRESHOLD}"]
    lines += [f"  {a['event']:20s} {a['normal']:.2f} -> {a['observed']:.2f}  margin {a['margin']:+.2f}  x{a['ratio']}  anomaly={a['anomaly']}" for a in anomalies]
    lines += [f"  detected {detected} of {len(anomalies)} = {ai['rate_pct']}%, highest: {ai['highest']}, closest to threshold: {ai['lowest_margin']}"]
    text = "\n".join(lines)
    (ROOT / "results/analysis.txt").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
