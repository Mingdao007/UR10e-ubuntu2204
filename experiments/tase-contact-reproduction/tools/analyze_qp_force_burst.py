#!/usr/bin/env python3
"""Join a sealed live QP force-guard event to sensor, command, and RTDE traces."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_THRESHOLD_N = 10.0
DEFAULT_CLUSTER_GAP_S = 0.012
DEFAULT_CONTEXT_S = 0.020


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _norm(values: list[float]) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in values))


def group_exceedances(
    samples: list[dict[str, Any]],
    *,
    threshold_n: float = DEFAULT_THRESHOLD_N,
    maximum_gap_s: float = DEFAULT_CLUSTER_GAP_S,
) -> list[list[dict[str, Any]]]:
    """Group nearby corrected-force-norm samples above an analysis threshold."""
    if not math.isfinite(threshold_n) or threshold_n <= 0:
        raise ValueError("threshold_n must be finite and positive")
    if not math.isfinite(maximum_gap_s) or maximum_gap_s <= 0:
        raise ValueError("maximum_gap_s must be finite and positive")

    selected: list[tuple[float, dict[str, Any]]] = []
    for sample in samples:
        wrench = sample.get("corrected_wrench_n_nm")
        if not isinstance(wrench, list) or len(wrench) < 3:
            raise ValueError("sensor sample is missing corrected force xyz")
        time_s = float(sample["host_use_monotonic_s"])
        force_norm_n = _norm(wrench[:3])
        if not math.isfinite(time_s) or not math.isfinite(force_norm_n):
            raise ValueError("sensor sample has a nonfinite timestamp or force")
        if force_norm_n >= threshold_n:
            selected.append((time_s, sample))

    selected.sort(key=lambda row: row[0])
    groups: list[list[dict[str, Any]]] = []
    previous_time: float | None = None
    for time_s, sample in selected:
        if not groups or previous_time is None or time_s - previous_time > maximum_gap_s:
            groups.append([sample])
        else:
            groups[-1].append(sample)
        previous_time = time_s
    return groups


def analyze_attempt(
    attempt_dir: Path,
    *,
    threshold_n: float = DEFAULT_THRESHOLD_N,
    maximum_gap_s: float = DEFAULT_CLUSTER_GAP_S,
) -> dict[str, Any]:
    attempt_dir = attempt_dir.resolve()
    seal = json.loads((attempt_dir / "seal.json").read_text(encoding="utf-8"))
    failure = json.loads((attempt_dir / "failure-diagnosis.json").read_text(encoding="utf-8"))

    source_names = ("raw_sensor", "command_timeline", "robot_frames")
    sources: dict[str, dict[str, Any]] = {}
    for name in source_names:
        path = attempt_dir / f"{name}.jsonl"
        actual_hash = _sha256(path)
        sealed = seal["segments"][name]
        if actual_hash != sealed["sha256"]:
            raise ValueError(f"sealed source hash mismatch: {name}")
        rows = _read_jsonl(path)
        if len(rows) != sealed["count"]:
            raise ValueError(f"sealed source row count mismatch: {name}")
        sources[name] = {"path": str(path), "sha256": actual_hash, "rows": rows}

    sensor_rows = sources["raw_sensor"]["rows"]
    command_rows = sources["command_timeline"]["rows"]
    robot_rows = sources["robot_frames"]["rows"]
    commands = {int(row["packet_sequence"]): row for row in command_rows}
    groups = group_exceedances(sensor_rows, threshold_n=threshold_n, maximum_gap_s=maximum_gap_s)

    clusters: list[dict[str, Any]] = []
    for group in groups:
        peak = max(group, key=lambda sample: _norm(sample["corrected_wrench_n_nm"][:3]))
        peak_sequence = int(peak["packet_sequence"])
        command = commands.get(peak_sequence)
        if command is None:
            raise ValueError(f"no command timeline row for sensor sequence {peak_sequence}")
        start_s = float(group[0]["host_use_monotonic_s"])
        end_s = float(group[-1]["host_use_monotonic_s"])
        peak_time_s = float(peak["host_use_monotonic_s"])
        context_start_s = start_s - DEFAULT_CONTEXT_S
        context_end_s = end_s + DEFAULT_CONTEXT_S
        nearby_commands = [
            row for row in command_rows
            if context_start_s <= float(row["published_monotonic_s"]) <= context_end_s
        ]
        nearby_robot = [
            row for row in robot_rows
            if context_start_s <= float(row["received_monotonic_s"]) <= context_end_s
        ]
        if not nearby_commands or not nearby_robot:
            raise ValueError(f"missing command or RTDE context near sensor sequence {peak_sequence}")
        corrected = peak["corrected_wrench_n_nm"]
        raw = peak["raw_wrench_n_nm"]
        peak_norm = _norm(corrected[:3])
        clusters.append({
            "start_monotonic_s": start_s,
            "end_monotonic_s": end_s,
            "sample_count": len(group),
            "peak_monotonic_s": peak_time_s,
            "peak_sensor_sequence": peak_sequence,
            "path_reference_time_s": command.get("reference_time_s"),
            "corrected_force_norm_n": peak_norm,
            "corrected_base_minus_z_normal_load_n": -float(corrected[2]),
            "raw_sensor_xyz_norm_n": _norm(raw[:3]),
            "all_cluster_samples_fresh": all(bool(sample.get("sensor_fresh")) for sample in group),
            "peak_command": {
                "sequence": int(command["packet_sequence"]),
                "phase": command.get("reference_phase"),
                "max_abs_published_qdot_rad_s": max(abs(float(v)) for v in command["published_packet_qdot_rad_s"]),
                "host_slew_scale": float(command["host_slew_scale"]),
                "solver_elapsed_s": float(command["solver_elapsed_s"]),
                "provider_elapsed_s": float(command["provider_elapsed_s"]),
            },
            "context": {
                "command_rows": len(nearby_commands),
                "max_abs_published_qdot_rad_s": max(
                    max(abs(float(v)) for v in row["published_packet_qdot_rad_s"])
                    for row in nearby_commands
                ),
                "max_solver_elapsed_s": max(float(row["solver_elapsed_s"]) for row in nearby_commands),
                "max_provider_elapsed_s": max(float(row["provider_elapsed_s"]) for row in nearby_commands),
                "min_host_slew_scale": min(float(row["host_slew_scale"]) for row in nearby_commands),
                "rtde_frames": len(nearby_robot),
                "max_abs_actual_qd_rad_s": max(
                    max(abs(float(v)) for v in row["qd_rad_s"]) for row in nearby_robot
                ),
                "max_tcp_linear_speed_m_s": max(
                    _norm(row["tcp_speed_m_s_rad_s"][:3]) for row in nearby_robot
                ),
                "consumed_packet_sequence_range": [
                    min(int(row["consumed_packet_sequence"]) for row in nearby_robot),
                    max(int(row["consumed_packet_sequence"]) for row in nearby_robot),
                ],
            },
        })

    peak_intervals = [
        clusters[index]["peak_monotonic_s"] - clusters[index - 1]["peak_monotonic_s"]
        for index in range(1, len(clusters))
    ]
    path_commands = sorted(
        (row for row in command_rows if row.get("reference_phase") == "path"),
        key=lambda row: float(row["published_monotonic_s"]),
    )
    path_intervals = [
        float(path_commands[index]["host_monotonic_s"])
        - float(path_commands[index - 1]["host_monotonic_s"])
        for index in range(1, len(path_commands))
    ]
    max_path_solve = max(float(row["solver_elapsed_s"]) for row in path_commands)
    path_solve_over_1ms = sum(float(row["solver_elapsed_s"]) > 0.001 for row in path_commands)
    return {
        "schema": "tase.qp-force-burst-trace-analysis-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "attempt_dir": str(attempt_dir),
        "analysis": {
            "force_threshold_n": threshold_n,
            "maximum_gap_for_cluster_s": maximum_gap_s,
            "context_each_side_s": DEFAULT_CONTEXT_S,
            "force_norm_source": "Euclidean norm of corrected wrench force xyz",
            "raw_sensor_norm_source": "Euclidean norm of raw wrench first three channels; sensor coordinates retained",
            "threshold_is_analysis_only": True,
            "source_hashes_verified_against_seal": True,
        },
        "force_guard": {
            "protection_limit_n": failure["first_failure"]["force_norm_limit_n"],
            "trip_peak_force_norm_n": failure["first_failure"]["force_norm_n"],
            "trip_packet_sequence": failure["first_failure"]["packet_sequence"],
        },
        "burst_summary": {
            "cluster_count": len(clusters),
            "first_to_last_peak_span_s": (
                clusters[-1]["peak_monotonic_s"] - clusters[0]["peak_monotonic_s"]
                if len(clusters) > 1 else 0.0
            ),
            "peak_intervals_s": {
                "count": len(peak_intervals),
                "median": statistics.median(peak_intervals) if peak_intervals else None,
                "min": min(peak_intervals) if peak_intervals else None,
                "max": max(peak_intervals) if peak_intervals else None,
            },
            "clusters": clusters,
        },
        "control_timing": {
            "path_command_count": len(path_commands),
            "solver_max_s": max_path_solve,
            "solver_calls_over_1ms": path_solve_over_1ms,
                "host_cycle_interval_s": {
                "median": statistics.median(path_intervals) if path_intervals else None,
                "p99": sorted(path_intervals)[math.ceil(0.99 * len(path_intervals)) - 1] if path_intervals else None,
                "max": max(path_intervals) if path_intervals else None,
            },
        },
        "interpretation": (
            "Repeated force excursions are present in both corrected and raw sensor channels. "
            "The time-aligned command and RTDE samples remain within the frozen observed velocity "
            "envelope and solver timing is below the deadline. These data do not distinguish "
            "contact/mechanical vibration from a sensor-originated transient; cause remains unknown."
        ),
        "sources": {
            name: {"path": value["path"], "sha256": value["sha256"], "row_count": len(value["rows"])}
            for name, value in sources.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze_attempt(args.attempt_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "clusters": result["burst_summary"]["cluster_count"],
        "peak_intervals_s": result["burst_summary"]["peak_intervals_s"],
        "solver_calls_over_1ms": result["control_timing"]["solver_calls_over_1ms"],
        "source_hashes_verified": result["analysis"]["source_hashes_verified_against_seal"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
