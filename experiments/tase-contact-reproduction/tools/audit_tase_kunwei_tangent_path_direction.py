#!/usr/bin/env python3
"""Audit corrected Kunwei tangent-force direction in eligible rate460 A traces."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from step5c_calibrated_kinematics_audit import rotvec_to_matrix


SCHEMA = "tase.kunwei-tangent-path-direction-audit-v1"
PROTOCOL_ID = "figure8_window60_r013_rate460_v1"
TARGET_PARAMS = {
    "Md_scalar": 9.565272137974492,
    "Bd_scalar": 693.6559295653944,
    "force_integral_limit_n_s": 1.0,
    "force_integral_policy": "legacy-clamp-v1",
}


class AuditError(RuntimeError):
    """A selected source attempt is incomplete or has an invalid force frame."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tangent_alignment_cosine(
    *,
    force_tcp_n: Any,
    tcp_rotvec_rad: Any,
    normal_base: Any,
    reference_velocity_base_m_s: Any,
) -> tuple[float, float, float]:
    """Return force/reference cosine and both tangent magnitudes."""
    force_tcp = np.asarray(force_tcp_n, dtype=float)
    rotvec = np.asarray(tcp_rotvec_rad, dtype=float)
    normal = np.asarray(normal_base, dtype=float)
    reference = np.asarray(reference_velocity_base_m_s, dtype=float)
    if (
        force_tcp.shape != (3,) or rotvec.shape != (3,) or normal.shape != (3,)
        or reference.shape != (3,)
        or not all(np.isfinite(value).all() for value in (force_tcp, rotvec, normal, reference))
    ):
        raise AuditError("tangent alignment input contains invalid vectors")
    norm_n = float(np.linalg.norm(normal))
    if norm_n <= 1e-12:
        raise AuditError("normal is degenerate")
    normal /= norm_n
    projector = np.eye(3) - np.outer(normal, normal)
    rotation = np.asarray(rotvec_to_matrix(rotvec), dtype=float)
    force_tangent = projector @ (rotation @ force_tcp)
    reference_tangent = projector @ reference
    force_norm = float(np.linalg.norm(force_tangent))
    reference_norm = float(np.linalg.norm(reference_tangent))
    denominator = force_norm * reference_norm
    if denominator <= 1e-12:
        raise AuditError("tangent force or reference has zero magnitude")
    cosine = float(np.dot(force_tangent, reference_tangent) / denominator)
    return cosine, force_norm, reference_norm


def _selected_attempts(runs_root: Path) -> list[Path]:
    selected: list[Path] = []
    for receipt_path in sorted(runs_root.glob("**/attempt-result.json")):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        binding = receipt.get("parameter_binding") or {}
        evidence = receipt.get("evidence") or {}
        metrics = evidence.get("metrics") or {}
        lifecycle = receipt.get("lifecycle") or {}
        if not str(binding.get("candidate_id", "")).endswith("-A"):
            continue
        actual = receipt.get("applied_runtime_parameters") or {}
        if any(actual.get(name) != value for name, value in TARGET_PARAMS.items()):
            continue
        if (
            receipt.get("evidence_eligible") is not True
            or lifecycle.get("path_complete") is not True
            or metrics.get("protocol_id") != PROTOCOL_ID
            or metrics.get("complete_bins") != 550
            or metrics.get("complete") is not True
        ):
            continue
        if (receipt_path.parent / "command_timeline.jsonl").is_file():
            selected.append(receipt_path.parent)
    return selected


def audit_attempt(attempt_dir: Path) -> dict[str, Any]:
    receipt_path = attempt_dir / "attempt-result.json"
    timeline_path = attempt_dir / "command_timeline.jsonl"
    robot_path = attempt_dir / "robot_frames.jsonl"
    sensor_path = attempt_dir / "raw_sensor.jsonl"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    with robot_path.open("r", encoding="utf-8") as stream:
        robot_first = json.loads(next(line for line in stream if line.strip()))
    with sensor_path.open("r", encoding="utf-8") as stream:
        sensor_first = json.loads(next(line for line in stream if line.strip()))
    if "actual_TCP_force" in robot_first:
        raise AuditError("selected robot RTDE trace contains UR internal force")
    if "corrected_wrench_n_nm" not in sensor_first:
        raise AuditError("selected raw force trace is not identified as corrected external Kunwei")

    cosines: list[float] = []
    force_norms: list[float] = []
    reference_speeds: list[float] = []
    formal_bins: set[int] = set()
    path_rows = 0
    first_time: float | None = None
    last_time: float | None = None
    with timeline_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("reference_phase") != "path":
                continue
            inputs = row.get("sfc_replay_inputs")
            if not isinstance(inputs, Mapping):
                raise AuditError("PATH command row lacks synchronized Kunwei/RTDE replay inputs")
            pose = inputs.get("actual_tcp_pose_m_rad")
            wrench = inputs.get("control_wrench_tcp_n_nm")
            time_s = float(row["reference_time_s"])
            cosine, force_norm, reference_norm = tangent_alignment_cosine(
                force_tcp_n=wrench[:3],
                tcp_rotvec_rad=pose[3:],
                normal_base=inputs["control_reaction_normal_base"],
                reference_velocity_base_m_s=inputs["reference_velocity_base_m_s"],
            )
            path_rows += 1
            first_time = time_s if first_time is None else first_time
            last_time = time_s
            cosines.append(cosine)
            force_norms.append(force_norm)
            reference_speeds.append(reference_norm)
            if 5.0 <= time_s < 60.0:
                formal_bins.add(int(math.floor((time_s - 5.0) / 0.1 + 1e-9)))

    if path_rows == 0 or len(formal_bins) != 550:
        raise AuditError(f"PATH coverage is incomplete: {path_rows} rows, {len(formal_bins)}/550 bins")
    evidence = receipt["evidence"]
    return {
        "attempt_dir": str(attempt_dir),
        "candidate_id": receipt["parameter_binding"]["candidate_id"],
        "formal_mae_n": evidence["mae_n"],
        "effective_rate_hz": evidence["effective_rate_hz"],
        "path_rows": path_rows,
        "formal_bins": len(formal_bins),
        "reference_time_range_s": [first_time, last_time],
        "mean_tangent_force_path_cosine": statistics.fmean(cosines),
        "negative_direction_fraction": sum(value < 0.0 for value in cosines) / len(cosines),
        "positive_direction_fraction": sum(value >= 0.0 for value in cosines) / len(cosines),
        "mean_tangent_force_n": statistics.fmean(force_norms),
        "mean_reference_tangent_speed_m_s": statistics.fmean(reference_speeds),
        "source_timeline_sha256": _sha256(timeline_path),
        "receipt_sha256": _sha256(receipt_path),
        "force_source": "baseline-corrected Kunwei KWR75 control_wrench_tcp_n_nm",
    }


def run_audit(runs_root: Path) -> dict[str, Any]:
    attempts = [audit_attempt(path) for path in _selected_attempts(runs_root)]
    if not attempts:
        raise AuditError("no eligible rate460 A attempts found")
    return {
        "schema": SCHEMA,
        "evidence_class": "offline_completed_A_trace_analysis",
        "protocol_id": PROTOCOL_ID,
        "fixed_runtime_parameters": dict(TARGET_PARAMS),
        "eligible_attempts": len(attempts),
        "attempts": attempts,
        "mean_attempt_cosine": statistics.fmean(
            row["mean_tangent_force_path_cosine"] for row in attempts
        ),
        "mean_attempt_negative_direction_fraction": statistics.fmean(
            row["negative_direction_fraction"] for row in attempts
        ),
        "causal_interpretation": "not identified: recorded tangent force may combine path/friction reaction, residual bias, and external disturbance",
        "independent_task_force_ground_truth": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = run_audit(args.runs_root.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": report["schema"],
        "eligible_attempts": report["eligible_attempts"],
        "mean_attempt_cosine": report["mean_attempt_cosine"],
        "mean_attempt_negative_direction_fraction": report["mean_attempt_negative_direction_fraction"],
        "out": str(args.out.resolve()),
    }, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
