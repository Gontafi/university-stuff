import csv
import json
from pathlib import Path
import sys


def summarize(raw):
    samples = raw["samples"]
    probes = sorted((sample for sample in samples if sample["kind"] == "probe"), key=lambda sample: sample["t"])
    down = 0.0
    episodes = []
    episode_start = None
    previous_end = 0.0
    for index, sample in enumerate(probes):
        until = probes[index + 1]["t"] if index + 1 < len(probes) else raw["duration_s"]
        unavailable = not sample["sla_ok"]
        if unavailable:
            down += max(0.0, until - sample["t"])
            if episode_start is None:
                episode_start = sample["t"]
        elif episode_start is not None:
            episodes.append({"start": episode_start, "end": sample["t"], "censored": False})
            episode_start = None
    if episode_start is not None:
        episodes.append({"start": episode_start, "end": raw["duration_s"], "censored": True})
    complete = [episode for episode in episodes if not episode["censored"]]
    duration = raw["duration_s"]
    uptime = max(0.0, duration - down)
    latencies = sorted(sample["latency_s"] for sample in samples)
    failures = [sample for sample in probes if sample["t"] >= raw["injected_at_s"] and not sample["sla_ok"]]
    recovered = [sample for sample in probes if sample["t"] >= raw["cleared_at_s"] and sample["sla_ok"]]
    healthy_intervals = []
    for episode in episodes:
        healthy_intervals.append(max(0.0, episode["start"] - previous_end))
        previous_end = episode["end"]
    failure_rate = len(episodes) / uptime if uptime > 0 else None
    return {
        "mode": raw["mode"], "scenario": raw["scenario"], "environment": raw["environment"],
        "duration_s": duration, "requests": len(samples),
        "successful_requests": sum(sample["http_ok"] for sample in samples),
        "failed_requests": sum(not sample["http_ok"] for sample in samples),
        "sla_failed_requests": sum(not sample["sla_ok"] for sample in samples),
        "recovered_requests": sum(sample["recovered"] for sample in samples),
        "degraded_requests": sum(sample["degraded"] for sample in samples),
        "recovered_transactions": raw["observations"].get("recovered_transactions", 0),
        "request_availability": sum(sample["http_ok"] for sample in samples) / len(samples) if samples else None,
        "sla_request_availability": sum(sample["sla_ok"] for sample in samples) / len(samples) if samples else None,
        "full_request_availability": sum(sample["sla_ok"] and not sample["degraded"] for sample in samples) / len(samples) if samples else None,
        "sampled_time_availability": uptime / duration if duration > 0 else None,
        "downtime_s": down, "failure_episodes": len(episodes),
        "failure_rate_per_uptime_s": failure_rate,
        "mttf_observed_s": episodes[0]["start"] if episodes else None,
        "mean_uptime_before_failure_s": sum(healthy_intervals) / len(healthy_intervals) if healthy_intervals else None,
        "mtbf_observed_s": (episodes[-1]["start"] - episodes[0]["start"]) / (len(episodes) - 1) if len(episodes) > 1 else None,
        "exposure_s_per_failure": duration / len(episodes) if episodes else None,
        "mttr_observed_s": sum(episode["end"] - episode["start"] for episode in complete) / len(complete) if complete else None,
        "right_censored_episodes": len(episodes) - len(complete),
        "service_detection_s": failures[0]["end_t"] - raw["injected_at_s"] if failures else None,
        "recovery_after_clear_s": recovered[0]["end_t"] - raw["cleared_at_s"] if recovered else None,
        "p95_latency_s": latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None,
        "consistent": raw["consistent"], "episodes": episodes,
    }


def main():
    directory = Path(sys.argv[1])
    summaries = []
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text())
        if isinstance(raw, dict) and "samples" in raw:
            summaries.append(summarize(raw))
    (directory / "summary.json").write_text(json.dumps(summaries, indent=2))
    flat = [{key: value for key, value in summary.items() if key != "episodes"} for summary in summaries]
    if flat:
        with (directory / "summary.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
            writer.writeheader()
            writer.writerows(flat)
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
