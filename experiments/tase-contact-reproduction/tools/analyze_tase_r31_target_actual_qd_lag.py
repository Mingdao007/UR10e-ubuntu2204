#!/usr/bin/env python3
"""Inspect whether RTDE target-to-actual qd lag explains the sealed TASE-QP gate miss.

The scan is diagnostic only. Official eligibility continues to use the
published-qdot versus RTDE actual_qd metric on the exact consumed sequence.
Positive row offsets pair a target_qd frame with a later actual_qd frame.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
from typing import Any, Sequence

from analyze_tase_r31_cartesian_tracking import pearson, verify_sealed_sources


class QdLagAnalysisError(ValueError):
    """Sealed qd streams cannot be joined or contain invalid values."""


def _finite(value: Any, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise QdLagAnalysisError(f"{label} must be finite")
    return result


def _vector(value: Any, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        raise QdLagAnalysisError(f"{label} must contain six values")
    return tuple(_finite(item, label) for item in value)


def _flatten(rows: Sequence[Sequence[float]]) -> list[float]:
    return [float(value) for row in rows for value in row]


def scan_target_actual_offsets(
    target_qd: Sequence[Sequence[float]],
    actual_qd: Sequence[Sequence[float]],
    controller_timestamps: Sequence[float],
    *,
    max_offset_rows: int = 24,
) -> dict[str, Any]:
    """Scan row shifts without changing the official same-consumed-packet gate."""
    if len(target_qd) != len(actual_qd) or len(target_qd) != len(controller_timestamps):
        raise QdLagAnalysisError("target, actual and timestamp rows must have equal length")
    if len(target_qd) < 2:
        raise QdLagAnalysisError("at least two joined RTDE rows are required")
    if type(max_offset_rows) is not int or max_offset_rows < 0:
        raise QdLagAnalysisError("max_offset_rows must be a nonnegative integer")
    target = [_vector(row, "target_qd") for row in target_qd]
    actual = [_vector(row, "actual_qd") for row in actual_qd]
    stamps = [_finite(stamp, "controller timestamp") for stamp in controller_timestamps]
    if any(b <= a for a, b in zip(stamps, stamps[1:])):
        raise QdLagAnalysisError("controller timestamps must be strictly increasing")

    scan: list[dict[str, Any]] = []
    n = len(target)
    for offset in range(-max_offset_rows, max_offset_rows + 1):
        first = max(0, -offset)
        stop = min(n, n - offset)
        indices = range(first, stop)
        expected = [target[i] for i in indices]
        observed = [actual[i + offset] for i in indices]
        time_offsets = [stamps[i + offset] - stamps[i] for i in indices]
        per_joint = [
            pearson([row[j] for row in expected], [row[j] for row in observed])
            for j in range(6)
        ]
        scan.append({
            "row_offset": offset,
            "positive_means_actual_later": True,
            "matched_rows": len(expected),
            "median_controller_time_offset_s": statistics.median(time_offsets),
            "flattened_pearson": pearson(_flatten(expected), _flatten(observed)),
            "per_joint_pearson": per_joint,
        })

    zero = next(row for row in scan if row["row_offset"] == 0)
    best = max(
        scan,
        key=lambda row: (
            -1.0 if row["flattened_pearson"] is None else row["flattened_pearson"]
        ),
    )
    per_joint: list[dict[str, Any]] = []
    for joint in range(6):
        best_joint = max(
            scan,
            key=lambda row: (
                -1.0 if row["per_joint_pearson"][joint] is None
                else row["per_joint_pearson"][joint]
            ),
        )
        per_joint.append({
            "joint_1_based": joint + 1,
            "target_qd_std_rad_s": statistics.pstdev(row[joint] for row in target),
            "actual_qd_std_rad_s": statistics.pstdev(row[joint] for row in actual),
            "same_row_pearson": zero["per_joint_pearson"][joint],
            "best_scan_pearson": best_joint["per_joint_pearson"][joint],
            "best_scan_row_offset": best_joint["row_offset"],
            "best_scan_time_offset_s": best_joint["median_controller_time_offset_s"],
        })
    return {
        "joined_rows": n,
        "scan_row_offset_range": [-max_offset_rows, max_offset_rows],
        "same_row": zero,
        "best_flattened_scan": best,
        "per_joint": per_joint,
        "all_offsets": scan,
    }


def analyze_attempt(label: str, attempt_dir: Path, *, max_offset_rows: int) -> dict[str, Any]:
    attempt_dir = attempt_dir.expanduser().resolve(strict=True)
    source_hashes = verify_sealed_sources(attempt_dir)
    attempt = json.loads((attempt_dir / "attempt-result.json").read_text(encoding="utf-8"))
    commands: dict[int, dict[str, float]] = {}
    for line_number, line in enumerate(
        (attempt_dir / "command_timeline.jsonl").open(encoding="utf-8"), 1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("reference_phase") != "path":
            continue
        reference_time = _finite(row.get("reference_time_s"), f"reference time line {line_number}")
        if not 0.0 <= reference_time < 60.0:
            continue
        sequence = int(row.get("packet_sequence", -1))
        if sequence < 0:
            raise QdLagAnalysisError(f"invalid PATH packet sequence at line {line_number}")
        commands.setdefault(sequence, {
            "reference_time_s": reference_time,
            "published_monotonic_s": _finite(
                row.get("published_monotonic_s"), f"publish time line {line_number}"
            ),
        })

    joined: dict[int, dict[str, Any]] = {}
    repeated_echoes = 0
    early_echoes = 0
    for line_number, line in enumerate(
        (attempt_dir / "robot_frames.jsonl").open(encoding="utf-8"), 1
    ):
        if not line.strip():
            continue
        frame = json.loads(line)
        sequence = int(frame.get("consumed_packet_sequence", -1))
        command = commands.get(sequence)
        if command is None:
            continue
        received = _finite(frame.get("received_monotonic_s"), f"RTDE receive line {line_number}")
        if received - command["published_monotonic_s"] < -0.001:
            early_echoes += 1
            continue
        if sequence in joined:
            repeated_echoes += 1
            continue
        joined[sequence] = {
            "sequence": sequence,
            "reference_time_s": command["reference_time_s"],
            "timestamp": _finite(frame.get("timestamp"), f"RTDE timestamp line {line_number}"),
            "target_qd": _vector(frame.get("target_qd_rad_s"), "RTDE target_qd"),
            "actual_qd": _vector(frame.get("qd_rad_s"), "RTDE actual_qd"),
        }

    rows = sorted(joined.values(), key=lambda row: row["reference_time_s"])
    if any(b["sequence"] <= a["sequence"] for a, b in zip(rows, rows[1:])):
        raise QdLagAnalysisError("consumed PATH packet sequence regressed")
    scan = scan_target_actual_offsets(
        [row["target_qd"] for row in rows],
        [row["actual_qd"] for row in rows],
        [row["timestamp"] for row in rows],
        max_offset_rows=max_offset_rows,
    )
    evidence = attempt.get("evidence") or {}
    metrics = evidence.get("metrics") or {}
    return {
        "label": str(label),
        "attempt_dir": str(attempt_dir),
        "evidence_eligible": attempt.get("evidence_eligible"),
        "official_published_qdot_actual_qd_gate": {
            "correlation": metrics.get("qd_correlation", evidence.get("qd_correlation")),
            "required_minimum": 0.9,
            "passed": metrics.get("motion_gate_passed") is True,
        },
        "joined_echo_rows": len(rows),
        "repeated_echo_rows_ignored": repeated_echoes,
        "early_echo_rows_ignored": early_echoes,
        "sealed_source_sha256": source_hashes,
        "target_actual_scan": scan,
    }


def _parse_input(value: str) -> tuple[str, Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not label.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("input must be LABEL=ATTEMPT_DIR")
    return label.strip(), Path(raw_path.strip())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=_parse_input, required=True)
    parser.add_argument("--max-offset-rows", type=int, default=24)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if len({label for label, _ in args.input}) != len(args.input):
        parser.error("input labels must be unique")
    result = {
        "schema": "tase.r31-target-actual-qd-lag-diagnostic-v1",
        "claim_class": "offline sealed RTDE telemetry timing diagnostic; not live qualification",
        "protocol_id": "figure8_window60_r013_rate460_v1",
        "method": "TASE_QP versus TASE_RNN_MATURE references",
        "join": "first RTDE frame that echoes the exact consumed packet sequence, received no earlier than publish time minus 1 ms",
        "offset_definition": "positive row offsets pair an earlier target_qd frame with a later actual_qd frame; per-row controller timestamps report the actual time shift",
        "official_gate_unchanged": "published_qdot versus RTDE actual_qd on the same consumed packet sequence; this scan cannot replace or pass that gate",
        "max_offset_rows": args.max_offset_rows,
        "attempts": [
            analyze_attempt(label, path, max_offset_rows=args.max_offset_rows)
            for label, path in args.input
        ],
        "limitations": [
            "An offset peak is descriptive timing evidence and does not establish a physical root cause.",
            "Rows are exact consumed-sequence joins; skipped echoes make adjacent rows nonuniform in time, so each offset also reports controller-time displacement.",
            "Joint 5 can have very small target_qd variance; its Pearson peak is not interpretable as a stable servo-lag estimate.",
            "The current QP attempts remain failed when their frozen published_qdot-to-actual_qd gate is below 0.900.",
        ],
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "attempts": len(result["attempts"]),
        "joined_rows": [item["joined_echo_rows"] for item in result["attempts"]],
        "same_row_target_actual_r": [item["target_actual_scan"]["same_row"]["flattened_pearson"] for item in result["attempts"]],
        "best_offset_target_actual_r": [item["target_actual_scan"]["best_flattened_scan"]["flattened_pearson"] for item in result["attempts"]],
        "offline_only": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
