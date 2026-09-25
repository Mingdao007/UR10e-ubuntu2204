#!/usr/bin/env python3
"""Compare sealed command, target, joint, and RTDE TCP velocity streams.

This is a diagnostic reducer, not an acceptance scorer. It joins a command row
to the first RTDE frame that echoes the same consumed packet sequence, after
checking the sealed source hashes. It never shifts sequences to improve a
score.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
from typing import Any, Sequence


class AnalysisError(ValueError):
    """A sealed stream is incomplete or cannot be joined without guessing."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sealed_sources(attempt_dir: Path) -> dict[str, str]:
    seal_path = attempt_dir / "seal.json"
    if not seal_path.is_file():
        raise AnalysisError(f"missing seal: {seal_path}")
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    segments = seal.get("segments")
    if not isinstance(segments, dict):
        raise AnalysisError("seal has no segment map")
    hashes: dict[str, str] = {}
    for key in ("command_timeline", "robot_frames"):
        record = segments.get(key)
        if not isinstance(record, dict) or not isinstance(record.get("sha256"), str):
            raise AnalysisError(f"seal has no SHA for {key}")
        path = attempt_dir / f"{key}.jsonl"
        observed = _sha256(path)
        if observed != record["sha256"]:
            raise AnalysisError(f"sealed SHA mismatch for {key}: {observed}")
        hashes[key] = observed
    return hashes


def _finite(value: Any, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise AnalysisError(f"non-finite {name}")
    return result


def _vector(value: Any, size: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise AnalysisError(f"{name} must have {size} values")
    return tuple(_finite(v, name) for v in value)


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) != len(y) or len(x) < 2:
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    dx, dy = [v - mx for v in x], [v - my for v in y]
    xx, yy = sum(v * v for v in dx), sum(v * v for v in dy)
    if xx <= 1e-30 or yy <= 1e-30:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / math.sqrt(xx * yy)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def pair_metrics(expected: Sequence[Sequence[float]],
                 observed: Sequence[Sequence[float]]) -> dict[str, Any]:
    if len(expected) != len(observed) or not expected:
        raise AnalysisError("velocity pair is empty or has unequal length")
    dimensions = len(expected[0])
    if dimensions == 0 or any(len(row) != dimensions for row in (*expected, *observed)):
        raise AnalysisError("velocity pair dimensions are inconsistent")
    count = len(expected)
    axis: list[dict[str, float | None]] = []
    residual_norms: list[float] = []
    for j in range(dimensions):
        x = [float(row[j]) for row in expected]
        y = [float(row[j]) for row in observed]
        residual = [b - a for a, b in zip(x, y)]
        expected_std = statistics.stdev(x) if len(x) > 1 else None
        observed_std = statistics.stdev(y) if len(y) > 1 else None
        rmse = math.sqrt(statistics.fmean([v * v for v in residual]))
        axis.append({
            "pearson_r": pearson(x, y),
            "expected_sample_std": expected_std,
            "observed_sample_std": observed_std,
            "bias_observed_minus_expected": statistics.fmean(residual),
            "rmse": rmse,
            "rmse_over_expected_std": (
                rmse / expected_std if expected_std is not None and expected_std > 0.0 else None
            ),
            "p95_abs_error": _percentile([abs(v) for v in residual], 0.95),
        })
    for i in range(count):
        residual_norms.append(math.sqrt(sum(
            (float(observed[i][j]) - float(expected[i][j])) ** 2
            for j in range(dimensions)
        )))
    flat_expected = [float(v) for row in expected for v in row]
    flat_observed = [float(v) for row in observed for v in row]
    flat_residual = [b - a for a, b in zip(flat_expected, flat_observed)]
    return {
        "samples": count,
        "dimensions": dimensions,
        "pearson_r_flat": pearson(flat_expected, flat_observed),
        "rmse_flat": math.sqrt(statistics.fmean([v * v for v in flat_residual])),
        "bias_flat_observed_minus_expected": statistics.fmean(flat_residual),
        "p95_vector_error_norm": _percentile(residual_norms, 0.95),
        "max_vector_error_norm": max(residual_norms),
        "per_axis": axis,
    }


def _matvec(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> tuple[float, ...]:
    if len(matrix) != 6 or any(len(row) != 6 for row in matrix):
        raise AnalysisError("command Jacobian must be 6x6")
    return tuple(sum(float(a) * float(b) for a, b in zip(row, vector)) for row in matrix)


def analyze_attempt(label: str, attempt_dir: Path) -> dict[str, Any]:
    attempt_dir = attempt_dir.expanduser().resolve()
    hashes = verify_sealed_sources(attempt_dir)
    result_path = attempt_dir / "attempt-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))

    # Keep only PATH commands whose consumed RTDE evidence can be joined. The
    # command's own reference clock is the only window selector used here.
    commands: dict[int, dict[str, Any]] = {}
    duplicate_sequences = 0
    for line_no, line in enumerate((attempt_dir / "command_timeline.jsonl").open(encoding="utf-8"), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("reference_phase") != "path":
            continue
        seq = int(row.get("packet_sequence", -1))
        ref_time = _finite(row.get("reference_time_s"), f"reference_time_s line {line_no}")
        if seq < 0 or not (0.0 <= ref_time < 60.0):
            continue
        if seq in commands:
            duplicate_sequences += 1
            continue
        commands[seq] = {
            "sequence": seq,
            "reference_time_s": ref_time,
            "published_monotonic_s": _finite(row.get("published_monotonic_s"), "published time"),
            "command_qd": _vector(row.get("published_packet_qdot_rad_s"), 6, "published qdot"),
            "timeline_actual_qd": _vector(row.get("actual_qd_rad_s"), 6, "timeline actual qdot"),
            "jacobian": row.get("jacobian_6x6"),
        }

    joined: dict[int, dict[str, Any]] = {}
    repeated_echo_frames = 0
    early_echo_frames = 0
    for line in (attempt_dir / "robot_frames.jsonl").open(encoding="utf-8"):
        if not line.strip():
            continue
        frame = json.loads(line)
        seq = int(frame.get("consumed_packet_sequence", -1))
        command = commands.get(seq)
        if command is None:
            continue
        received = _finite(frame.get("received_monotonic_s"), "RTDE receive time")
        delay = received - command["published_monotonic_s"]
        if delay < -0.001:
            early_echo_frames += 1
            continue
        if seq in joined:
            repeated_echo_frames += 1
            continue
        target_qd = _vector(frame.get("target_qd_rad_s"), 6, "RTDE target qdot")
        actual_qd = _vector(frame.get("qd_rad_s"), 6, "RTDE actual qdot")
        actual_tcp = _vector(frame.get("tcp_speed_m_s_rad_s"), 6, "RTDE actual TCP speed")
        jacobian = command["jacobian"]
        predicted_command = _matvec(jacobian, command["command_qd"])
        predicted_target = _matvec(jacobian, target_qd)
        predicted_actual = _matvec(jacobian, actual_qd)
        joined[seq] = {
            **command,
            "received_monotonic_s": received,
            "echo_delay_s": delay,
            "target_qd": target_qd,
            "actual_qd": actual_qd,
            "actual_tcp_speed": actual_tcp,
            "predicted_command_twist": predicted_command,
            "predicted_target_twist": predicted_target,
            "predicted_actual_twist": predicted_actual,
        }

    rows = sorted(joined.values(), key=lambda row: row["reference_time_s"])
    if not rows:
        raise AnalysisError(f"no exact consumed-sequence joins in {attempt_dir}")

    def selected(start: float, end: float) -> list[dict[str, Any]]:
        return [row for row in rows if start <= row["reference_time_s"] < end]

    def summarize_window(window_rows: list[dict[str, Any]]) -> dict[str, Any]:
        expected_qd = [row["command_qd"] for row in window_rows]
        target_qd = [row["target_qd"] for row in window_rows]
        actual_qd = [row["actual_qd"] for row in window_rows]
        actual_tcp = [row["actual_tcp_speed"] for row in window_rows]
        delays = [row["echo_delay_s"] for row in window_rows]

        def cartesian_pair(key: str) -> dict[str, Any]:
            predicted = [row[key] for row in window_rows]
            return {
                "linear_3d": {
                    "units": "m/s",
                    **pair_metrics([row[:3] for row in predicted],
                                   [row[:3] for row in actual_tcp]),
                },
                "angular_3d": {
                    "units": "rad/s",
                    **pair_metrics([row[3:] for row in predicted],
                                   [row[3:] for row in actual_tcp]),
                },
            }

        return {
            "samples": len(window_rows),
            "reference_window_s": [
                window_rows[0]["reference_time_s"] if window_rows else None,
                window_rows[-1]["reference_time_s"] if window_rows else None,
            ],
            "echo_delay_s": {
                "median": statistics.median(delays) if delays else None,
                "p95": _percentile(delays, 0.95),
                "max": max(delays) if delays else None,
            },
            "joint_velocity": {
                "command_vs_target": pair_metrics(expected_qd, target_qd),
                "target_vs_actual": pair_metrics(target_qd, actual_qd),
                "command_vs_actual": pair_metrics(expected_qd, actual_qd),
            },
            "cartesian_velocity": {
                "J_command_vs_RTDE_actual_TCP_speed": cartesian_pair(
                    "predicted_command_twist"
                ),
                "J_RTDE_target_qd_vs_RTDE_actual_TCP_speed": cartesian_pair(
                    "predicted_target_twist"
                ),
                "J_RTDE_actual_qd_vs_RTDE_actual_TCP_speed": cartesian_pair(
                    "predicted_actual_twist"
                ),
            },
        }

    protocol_id = result.get("parameter_binding", {}).get("protocol_id")
    evidence = result.get("evidence", {})
    qd_corr = evidence.get("qd_correlation") if isinstance(evidence, dict) else None
    return {
        "label": label,
        "attempt_dir": str(attempt_dir),
        "physical_attempt_state": result.get("state"),
        "evidence_eligible": result.get("evidence_eligible"),
        "formal_mae_n": evidence.get("mae_n") if isinstance(evidence, dict) else None,
        "frozen_acceptance_qd_correlation": qd_corr,
        "protocol_id": protocol_id,
        "parameters": result.get("parameter_binding"),
        "lifecycle": {
            key: result.get("lifecycle", {}).get(key)
            for key in ("path_complete", "home_verified", "ready_for_next", "sealed")
        },
        "source_sha256": hashes,
        "join": {
            "path_command_sequences": len(commands),
            "matched_consumed_sequences": len(rows),
            "matched_fraction": len(rows) / len(commands) if commands else None,
            "duplicate_path_sequences": duplicate_sequences,
            "repeated_echo_frames_ignored": repeated_echo_frames,
            "early_echo_frames_ignored": early_echo_frames,
            "association": "first RTDE frame with exact consumed_packet_sequence and receive time >= command publish time - 1 ms",
            "packet_offsets_applied": 0,
        },
        "windows": {
            "path_0_60_s": summarize_window(selected(0.0, 60.0)),
            "formal_5_60_s": summarize_window(selected(5.0, 60.0)),
        },
    }


def parse_attempt(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError("attempt must be LABEL=ATTEMPT_DIR")
    label, path = spec.split("=", 1)
    if not label or not path:
        raise argparse.ArgumentTypeError("attempt must be LABEL=ATTEMPT_DIR")
    return label, Path(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", action="append", type=parse_attempt, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = {
        "schema": "tase.r31-cartesian-velocity-diagnostic-v1",
        "claim_class": "sealed-live-telemetry-diagnostic-not-acceptance",
        "method": "exact consumed_packet_sequence join; no packet shifting",
        "assumptions": [
            "Jacobian is sampled at host command time; RTDE target/actual velocities are sampled at the first frame echoing that exact command sequence.",
            "RTDE actual_TCP_speed is compared as recorded; the Jacobian comparison localizes telemetry relationships and is not a controller acceptance gate.",
            "This diagnostic does not establish why command and physical velocity differ.",
        ],
        "attempts": [analyze_attempt(label, path) for label, path in args.attempt],
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
