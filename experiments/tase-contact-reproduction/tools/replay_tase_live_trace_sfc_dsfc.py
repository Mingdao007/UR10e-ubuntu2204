#!/usr/bin/env python3
"""Run offline SFC/DSFC command counterfactuals on one sealed TASE-RNN trace.

The measured TASE-RNN feedback, pose, reference, Jacobian, bounds, and timing
are held fixed. Candidate outputs are not fed back into subsequent observations,
so this is a source-trace command replay, not closed-loop robot evidence.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from audit_tase_qp_replay_inputs import inspect_attempt
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from step5d_autotune_v4_r004.timing import MAX_FRESH_GAP_S
from tase_sfc_dsfc_comparison import METHODS
from tase_sfc_dsfc_replay import _load_frozen_profiles, _replay_input_digest, _replay_one


SCHEMA = "ur10e.tase-sfc-dsfc-live-rnn-trace-replay-v1"
TRACE_SCHEMA = "tase.qp-replay-command-timeline-v3"


def _vector(value: Any, width: int, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if array.shape != (width,) or not np.isfinite(array).all():
        raise ValueError(f"{name} must be a finite vector of width {width}")
    return array


def command_row_to_replay_row(source: Mapping[str, Any]) -> dict[str, Any]:
    """Map captured consumed inputs without reconstructing absent measurements."""
    if source.get("schema") != TRACE_SCHEMA or source.get("reference_phase") != "path":
        raise ValueError("source row is not a v3 formal PATH command-timeline row")
    captured = source.get("sfc_replay_inputs")
    if not isinstance(captured, Mapping):
        raise ValueError("source row lacks captured SFC/DSFC inputs")
    provenance = source.get("sfc_replay_input_provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("source row lacks replay-input provenance")
    normal_note = provenance.get("control_reaction_normal_base")
    force_note = provenance.get("control_wrench_tcp_n_nm")
    native_note = provenance.get("native_wrench_tcp_n_nm")
    if (
        normal_note != "fixed_base_z_control_axis"
        or force_note != "kunwei_baseline_corrected"
        or native_note != "kunwei_native"
    ):
        raise ValueError("source row has incomplete normal or wrench provenance")

    control_wrench = _vector(captured.get("control_wrench_tcp_n_nm"), 6, "control wrench")
    native_wrench = _vector(captured.get("native_wrench_tcp_n_nm"), 6, "native wrench")
    pose = _vector(captured.get("actual_tcp_pose_m_rad"), 6, "actual TCP pose")
    speed = _vector(captured.get("actual_tcp_speed_m_s_rad_s"), 6, "actual TCP speed")
    normal = _vector(captured.get("control_reaction_normal_base"), 3, "control reaction normal")
    normal_norm = float(np.linalg.norm(normal))
    if not 0.999 <= normal_norm <= 1.001:
        raise ValueError("captured control reaction axis is not unit length")
    reference_position = _vector(
        captured.get("reference_position_base_m"), 3, "reference position",
    )
    reference_velocity = _vector(
        captured.get("reference_velocity_base_m_s"), 3, "reference velocity",
    )
    state_age = float(captured.get("state_age_s", math.nan))
    time_s = float(source.get("host_monotonic_s", math.nan))
    path_reference_time_s = float(source.get("reference_time_s", math.nan))
    dt_s = float(source.get("actual_dt_s", math.nan))
    if (
        not math.isfinite(state_age) or state_age < 0
        or not math.isfinite(time_s)
        or not math.isfinite(path_reference_time_s)
        or path_reference_time_s < 0.0
        or not math.isfinite(dt_s) or not 0.0 < dt_s <= MAX_FRESH_GAP_S
    ):
        raise ValueError("source row has invalid time or observation age")

    rotation = np.asarray(rotvec_to_matrix(pose[3:]), dtype=float)
    force_base = rotation @ control_wrench[:3]
    torque_base = rotation @ control_wrench[3:]
    native_force_base = rotation @ native_wrench[:3]
    jacobian = np.asarray(source.get("jacobian_6x6"), dtype=float)
    if jacobian.shape != (6, 6) or not np.isfinite(jacobian).all():
        raise ValueError("captured calibrated Jacobian is invalid")
    qdot = _vector(source.get("published_packet_qdot_rad_s"), 6, "published qdot")
    q = _vector(source.get("actual_q_rad"), 6, "actual joint position")
    lower = _vector(source.get("solver_qdot_lower_rad_s"), 6, "joint lower bounds")
    upper = _vector(source.get("solver_qdot_upper_rad_s"), 6, "joint upper bounds")
    force_target = float(source.get("force_target_n", math.nan))
    if not math.isfinite(force_target):
        raise ValueError("captured force target is invalid")

    return {
        "time_s": time_s,
        "dt_s": dt_s,
        "inputs": {
            "path_reference_time_s": path_reference_time_s,
            # The existing SFC/DSFC replay field means the force signal before
            # controller filtering. Here it is explicitly the baseline-corrected
            # Kunwei signal actually supplied to TASE, not ground truth.
            "raw_force_base_n": force_base.tolist(),
            "force_control_base_n": force_base.tolist(),
            "force_control_torque_base_nm": torque_base.tolist(),
            "native_force_base_n": native_force_base.tolist(),
            "force_signal_provenance": (
                "baseline-corrected Kunwei input supplied to TASE before its normal filter; "
                "not an independent task-force measurement"
            ),
            "native_force_signal_provenance": "native Kunwei sample before baseline correction",
            "state_age_s": state_age,
            "tcp_position_base_m": pose[:3].tolist(),
            "tcp_rotation_base": rotation.tolist(),
            "actual_tcp_speed_m_s_rad_s": speed.tolist(),
            "joint_position_rad": q.tolist(),
            "jacobian": jacobian.tolist(),
            "joint_velocity_lower_rad_s": lower.tolist(),
            "joint_velocity_upper_rad_s": upper.tolist(),
            "estimated_outward_normal_base": normal.tolist(),
            "normal_signal_provenance": (
                "fixed approved base-Z reaction axis for this TASE profile; not a measured or estimated surface normal"
            ),
            "software_injection_base_n": [0.0, 0.0, 0.0],
            "reference_position_base_m": reference_position.tolist(),
            "reference_velocity_base_m_s": reference_velocity.tolist(),
            "force_target_n": force_target,
        },
        "output": {
            "realized_jqdot_m_s_rad_s": (jacobian @ qdot).tolist(),
        },
    }


def _read_v3_rows(attempt_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    path = Path(attempt_dir) / "command_timeline.jsonl"
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                rows.append(command_row_to_replay_row(row))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid v3 command timeline row {line_number}: {exc}") from exc
    if not rows:
        raise ValueError("sealed command timeline is empty")
    return rows


def run_replay(*, attempt_dir: Path, output_dir: Path) -> dict[str, Any]:
    attempt_dir = Path(attempt_dir).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    audit = inspect_attempt(attempt_dir)
    if audit.get("sfc_dsfc_same_input_replay_inputs_complete") is not True:
        raise ValueError("sealed attempt does not have complete v3 SFC/DSFC inputs")
    rows = _read_v3_rows(attempt_dir)
    profile = _load_frozen_profiles()
    input_digest = _replay_input_digest(rows)
    source_result = json.loads((attempt_dir / "attempt-result.json").read_text(encoding="utf-8"))
    source_binding = source_result.get("parameter_binding", {})
    timeline_path = attempt_dir / "command_timeline.jsonl"
    timeline_sha = hashlib.sha256(timeline_path.read_bytes()).hexdigest()
    source_attempt = {
        "attempt_id": attempt_dir.name,
        "candidate_id": source_binding.get("candidate_id"),
        "protocol_id": audit.get("protocol_id"),
        "path_duration_s": audit.get("metric_window", {}).get("formal_reference_duration_s"),
        "trace_sha256": timeline_sha,
    }

    output_dir.mkdir(parents=True, exist_ok=False)
    result_rows: dict[str, dict[str, Any]] = {}
    for method, law in zip(METHODS, ("SFC", "DSFC")):
        replay = _replay_one(
            rows,
            method=method,
            law=law,
            profile=profile,
            include_command_rows=True,
            include_collision_intent=False,
        )
        commands = replay.pop("command_rows")
        command_path = output_dir / f"{law.lower()}-commands.jsonl.gz"
        with gzip.open(command_path, "wt", encoding="utf-8", compresslevel=6) as stream:
            for row in commands:
                stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False))
                stream.write("\n")
        result_rows[law] = {
            **replay,
            "command_trace_path": command_path.name,
            "command_trace_sha256": hashlib.sha256(command_path.read_bytes()).hexdigest(),
        }

    document = {
        "schema": SCHEMA,
        "evidence_class": "offline_open_loop_counterfactual_on_sealed_TASE_RNN_feedback_trace",
        "source_attempt": source_attempt,
        "source_command_timeline_sha256": source_attempt["trace_sha256"],
        "source_feedback_input_sha256": input_digest,
        "source_input_rows": len(rows),
        "same_input_verified": all(
            item["common_input_sha256"] == input_digest for item in result_rows.values()
        ),
        "closed_loop": False,
        "physical_candidate_executed": False,
        "candidate_commands_fed_back": False,
        "primary_force_metric_recomputed": False,
        "force_feedback_claim_limit": (
            "Both candidate laws consume the same recorded baseline-corrected Kunwei feedback. "
            "It is not an independent task-force truth measurement. Candidate commands do not move the robot, "
            "and the recorded TASE-RNN state/observations are not regenerated under SFC or DSFC."
        ),
        "normal_axis_claim_limit": (
            "The recorded axis is the fixed approved base-Z TASE reaction axis, not a measured or estimated surface normal."
        ),
        "native_law_elapsed_time_treatment": {
            "recorded_observed_dt_consumed": True,
            "maximum_observed_dt_s": MAX_FRESH_GAP_S,
            "native_single_step_limit_s": 0.004,
            "policy": "zero_order_hold_each recorded TASE-RNN feedback sample across its measured interval, split into equal native-law substeps of at most 4 ms",
            "claim_limit": "Substeps are a numerical integration of one held sample; no intermediate sensor or robot measurements are synthesized.",
        },
        "frozen_native_profile": profile["tangential_parameters"],
        "results": result_rows,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_replay(attempt_dir=args.attempt_dir, output_dir=args.output_dir)
    print(json.dumps({
        "schema": result["schema"],
        "output_dir": str(args.output_dir.resolve()),
        "source_rows": result["source_input_rows"],
        "same_input_verified": result["same_input_verified"],
        "closed_loop": result["closed_loop"],
        "candidate_command_digests": {
            law: value["command_output_sha256"] for law, value in result["results"].items()
        },
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
