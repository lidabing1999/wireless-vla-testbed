"""Summarize the per-arm control clock and policy responses after a rollout."""
import json
import math
from collections import Counter
from statistics import median


def latency_ms(values):
    if not values:
        return None
    ordered = sorted(value / 1e6 for value in values)
    return {"median": median(ordered), "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
            "max": ordered[-1]}


def summarize(run_dir):
    def records(name):
        with (run_dir / name).open() as handle:
            return [json.loads(line) for line in handle]

    control = records("control.jsonl")
    steps = []
    rtts = []
    for record in records("trace.jsonl"):
        if record["event"] == "step":
            steps.append(record)
        elif record["event"] == "infer":
            rtts.append(record["end_ns"] - record["start_ns"])
    if not control or len(steps) != len(control):
        raise ValueError(f"incomplete control trace in {run_dir}")
    gaps = [b["start_ns"] - a["start_ns"] for a, b in zip(steps, steps[1:])]
    sim_elapsed = steps[-1]["sim_start_s"] - steps[0]["sim_start_s"]
    return {
        "ticks": len(control), "actions": dict(Counter(row["action"] for row in control)),
        "first_step_ns": steps[0]["start_ns"], "last_step_end_ns": steps[-1]["end_ns"],
        "sim_seconds": steps[-1]["sim_end_s"] - steps[0]["sim_start_s"],
        "real_time_factor": sim_elapsed / (sum(gaps) / 1e9) if gaps else None,
        "missed_releases": sum(row["missed_releases"] for row in control),
        "late_steps": sum(not row["finished_in_period"] for row in control),
        "step_interval_ms": latency_ms(gaps),
        "step_duration_ms": latency_ms([row["end_ns"] - row["start_ns"] for row in steps]),
        "inference_requests": len(rtts), "inference_round_trip_ms": latency_ms(rtts),
    }
