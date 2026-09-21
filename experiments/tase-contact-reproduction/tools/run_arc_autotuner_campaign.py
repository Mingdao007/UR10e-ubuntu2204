#!/usr/bin/env python3
"""Run the bounded arc autotuner campaign serially through the live owner.

The first eight candidates are a fixed log-spaced initial design.  The next
twelve use a deterministic bounded RBF expected-improvement proposal over the
same four normal-admittance fields.  Four final attempts repeat the incumbent.
Every attempt is sealed independently; a partial circle keeps its computed
metrics but is never treated as a full-path objective.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-contact-six/bin/python"
BASELINE = {
    "normal_velocity_p_gain": 0.0006,
    "normal_velocity_i_gain": 0.00008,
    "normal_filter_alpha": 0.70,
    "integral_error_limit_n_s": 10.0,
    "target_force_abs_n": 5.0,
    "tangent_speed_m_s": 0.005,
    "circle_xy_p_gain_m_s_per_m": 3.0,
    "circle_xy_velocity_limit_m_s": 0.003,
    "angular_velocity_limit_rad_s": 0.030,
    "attitude_force_gain_rad_s": 0.040,
    "attitude_torque_gain_rad_s_per_nm": 0.050,
    "attitude_filter_alpha": 0.20,
}
TUNED_FIELDS = (
    "normal_velocity_p_gain",
    "normal_velocity_i_gain",
    "normal_filter_alpha",
    "integral_error_limit_n_s",
)
BO_LO = np.array([0.00020, 0.00002, 0.40, 5.0], dtype=float)
BO_HI = np.array([0.00120, 0.00020, 0.90, 15.0], dtype=float)


def candidate_key(candidate: dict[str, Any]) -> tuple[float, ...]:
    return tuple(round(float(candidate[field]), 12) for field in TUNED_FIELDS)


def fixed_initial() -> list[dict[str, Any]]:
    # Keep the historical baseline as one of the eight initial cells.
    p_values = (0.00030, 0.00045, 0.00060, 0.00080)
    i_values = (0.00004, 0.00008)
    values: list[dict[str, Any]] = []
    for p in p_values:
        for i in i_values:
            candidate = dict(BASELINE)
            candidate.update(normal_velocity_p_gain=p, normal_velocity_i_gain=i)
            values.append(candidate)
    return values


def metric_value(record: dict[str, Any]) -> float:
    analysis = record.get("analysis") or {}
    if analysis.get("coverage", {}).get("class") == "full_path":
        value = analysis.get("normal_force_mae_n")
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
    # A failed/partial attempt remains in the denominator and receives a
    # finite surrogate only for proposal ranking; the raw record is untouched.
    return 8.0


def _vector(candidate: dict[str, Any]) -> np.ndarray:
    raw = np.array([float(candidate[field]) for field in TUNED_FIELDS], dtype=float)
    return (raw - BO_LO) / (BO_HI - BO_LO)


def propose_rbf(observations: list[dict[str, Any]], seen: set[tuple[float, ...]], iteration: int) -> dict[str, Any]:
    rng = np.random.default_rng(20260921 + iteration)
    pool = rng.uniform(0.0, 1.0, size=(512, len(TUNED_FIELDS)))
    # Keep a coarse deterministic grid around the incumbent in the pool.
    incumbent = min(observations, key=metric_value) if observations else None
    if incumbent:
        center = _vector(incumbent["candidate"])
        local = np.clip(center + rng.normal(0.0, 0.12, size=(128, len(TUNED_FIELDS))), 0.0, 1.0)
        pool[: len(local)] = local
    x = np.array([_vector(row["candidate"]) for row in observations], dtype=float)
    y = np.array([metric_value(row) for row in observations], dtype=float)
    if len(x) == 0:
        score = np.zeros(len(pool))
    else:
        distances = np.linalg.norm(pool[:, None, :] - x[None, :, :], axis=2)
        weights = np.exp(-0.5 * (distances / 0.22) ** 2)
        denom = np.sum(weights, axis=1) + 1e-9
        mean = np.sum(weights * y[None, :], axis=1) / denom
        variance = np.sum(weights * (y[None, :] - mean[:, None]) ** 2, axis=1) / denom
        uncertainty = np.sqrt(np.maximum(variance, 0.0)) + 0.05 / denom
        best = min(y) if len(y) else 8.0
        score = mean - 0.8 * uncertainty - 0.02 * np.maximum(0.0, best - mean)
    order = np.argsort(score)
    for index in order:
        raw = BO_LO + pool[int(index)] * (BO_HI - BO_LO)
        candidate = dict(BASELINE)
        candidate.update(dict(zip(TUNED_FIELDS, raw.tolist())))
        candidate["normal_filter_alpha"] = float(np.clip(candidate["normal_filter_alpha"], 0.0, 1.0))
        candidate["target_force_abs_n"] = 5.0
        if candidate_key(candidate) not in seen:
            return candidate
    raise RuntimeError("bounded BO proposal pool was exhausted")


def write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str) + "\n")
    tmp.replace(path)


def recovery_blocked(record: dict[str, Any]) -> bool:
    if record.get("state") == "BLOCKED":
        return True
    recovery = record.get("recovery")
    if recovery is not None and recovery.get("success") is not True:
        return True
    if record.get("state") in {"FAILED_ATTEMPT", "FAILED_BUILD"} and recovery is None:
        return True
    return False


def run_command(command: list[str], *, output: Path) -> subprocess.CompletedProcess[str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w") as handle:
        return subprocess.run(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, text=True)


def run(args: argparse.Namespace) -> dict[str, Any]:
    campaign = Path(args.output).resolve()
    if campaign.exists() and not args.resume:
        raise RuntimeError(f"campaign output already exists; use --resume: {campaign}")
    if campaign.exists():
        ledger = json.loads((campaign / "ledger.json").read_text())
        if ledger.get("schema") != "tase_arc_autotuner_campaign_v1":
            raise RuntimeError("campaign ledger schema differs")
        ledger["state"] = "RUNNING"
        observations = list(ledger.get("attempts", []))
        seen = {candidate_key(row["candidate"]) for row in observations if row.get("candidate")}
    else:
        campaign.mkdir(parents=True)
        ledger = {
            "schema": "tase_arc_autotuner_campaign_v1",
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "budget": {"initial": 8, "bo": 12, "repeat": 4},
            "law_identity": "STEP4D_DETSEARCH_SIGNED_FZ_ATTITUDE_MATURE_V1",
            "path_identity": "STEP4D_CIRCLE_MIDDLE_HALF_REFERENCE_FULL_CIRCLE_V1",
            "objective": "formal full-path normal-force MAE; partial metrics are marked and not promoted",
            "attempts": [],
            "state": "RUNNING",
        }
        observations = []
        seen = set()
    existing = Path(args.existing_baseline).resolve() if args.existing_baseline else None
    if existing is not None and not ledger["attempts"]:
        baseline_record = json.loads((existing / "attempt.json").read_text())
        baseline_record["phase"] = "initial"
        baseline_record["budget_index"] = 1
        ledger["attempts"].append(baseline_record)
        observations.append(baseline_record)
        seen.add(candidate_key(baseline_record["candidate"]))
    initial = fixed_initial()
    for candidate in initial:
        if len([a for a in ledger["attempts"] if a.get("phase") == "initial"]) >= 8:
            break
        if candidate_key(candidate) in seen:
            continue
        index = len([a for a in ledger["attempts"] if a.get("phase") == "initial"]) + 1
        record = execute_attempt(candidate, phase="initial", budget_index=index, campaign=campaign, args=args)
        ledger["attempts"].append(record)
        observations.append(record)
        seen.add(candidate_key(candidate))
        write_json(campaign / "ledger.json", ledger)
        if recovery_blocked(record):
            ledger["state"] = "BLOCKED"
            write_json(campaign / "ledger.json", ledger)
            return ledger
    completed_bo = {int(row["budget_index"]) for row in ledger["attempts"] if row.get("phase") == "bo"}
    for bo_index in range(1, 13):
        if bo_index in completed_bo:
            continue
        candidate = propose_rbf(observations, seen, bo_index)
        record = execute_attempt(candidate, phase="bo", budget_index=bo_index, campaign=campaign, args=args)
        ledger["attempts"].append(record)
        observations.append(record)
        seen.add(candidate_key(candidate))
        write_json(campaign / "ledger.json", ledger)
        if recovery_blocked(record):
            ledger["state"] = "BLOCKED"
            write_json(campaign / "ledger.json", ledger)
            return ledger
    full = [row for row in observations if row.get("analysis", {}).get("coverage", {}).get("class") == "full_path"]
    if not full:
        ledger["state"] = "COMPLETE_NO_FULL_PATH"
        write_json(campaign / "ledger.json", ledger)
        return ledger
    incumbent = min(full, key=metric_value)
    completed_repeats = {int(row["budget_index"]) for row in ledger["attempts"] if row.get("phase") == "repeat"}
    for repeat_index in range(1, 5):
        if repeat_index in completed_repeats:
            continue
        record = execute_attempt(dict(incumbent["candidate"]), phase="repeat", budget_index=repeat_index, campaign=campaign, args=args)
        ledger["attempts"].append(record)
        observations.append(record)
        write_json(campaign / "ledger.json", ledger)
        if recovery_blocked(record):
            ledger["state"] = "BLOCKED"
            write_json(campaign / "ledger.json", ledger)
            return ledger
    repeats = [row for row in observations if row.get("phase") == "repeat" and row.get("analysis", {}).get("coverage", {}).get("class") == "full_path"]
    ledger["incumbent"] = {
        "candidate": incumbent["candidate"],
        "single_best_mae_n": metric_value(incumbent),
        "repeat_mae_n": [metric_value(row) for row in repeats],
        "repeat_mean_mae_n": float(np.mean([metric_value(row) for row in repeats])) if repeats else None,
        "repeat_std_mae_n": float(np.std([metric_value(row) for row in repeats], ddof=1)) if len(repeats) >= 2 else None,
        "comparison_note": "descriptive until matched baseline repeats are available",
    }
    ledger["state"] = "COMPLETE"
    ledger["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_json(campaign / "ledger.json", ledger)
    return ledger


def execute_attempt(candidate: dict[str, Any], *, phase: str, budget_index: int, campaign: Path, args: argparse.Namespace) -> dict[str, Any]:
    slug = f"{phase}-{budget_index:02d}"
    package = campaign / "packages" / slug
    candidate_path = campaign / "candidates" / f"{slug}.json"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(candidate_path, candidate)
    package.parent.mkdir(parents=True, exist_ok=True)
    build_command = [
        str(PYTHON),
        "-B",
        "tools/build_arc_autotune_package.py",
        "--output",
        str(package),
        "--candidate-json",
        str(candidate_path),
    ]
    build_log = campaign / "logs" / f"{slug}-build.log"
    built = run_command(build_command, output=build_log)
    if built.returncode != 0:
        return {"phase": phase, "budget_index": budget_index, "candidate": candidate, "state": "FAILED_BUILD", "build_log": str(build_log)}
    attempt_output = campaign / "attempts" / slug
    run_command_line = [
        str(PYTHON),
        "-B",
        "tools/run_arc_autotune_attempt.py",
        "--package-dir",
        str(package),
        "--output",
        str(attempt_output),
        "--execute",
        "--host",
        str(args.host),
        "--video-url",
        str(args.video_url),
    ]
    attempt_log = campaign / "logs" / f"{slug}-attempt.log"
    completed = run_command(run_command_line, output=attempt_log)
    record_path = attempt_output / "attempt.json"
    if record_path.exists():
        record = json.loads(record_path.read_text())
    else:
        record = {"state": "BLOCKED", "failure": f"attempt runner exited {completed.returncode}"}
    record.update({"phase": phase, "budget_index": budget_index, "candidate": candidate, "attempt_log": str(attempt_log), "package_dir": str(package)})
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing-baseline", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--host", default="192.168.1.18")
    parser.add_argument("--video-url", default="rtsp://127.0.0.1:8554/arm")
    args = parser.parse_args(argv)
    value = run(args)
    print(json.dumps(value, indent=2, default=str))
    return 0 if value.get("state") in {"COMPLETE", "COMPLETE_NO_FULL_PATH"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
