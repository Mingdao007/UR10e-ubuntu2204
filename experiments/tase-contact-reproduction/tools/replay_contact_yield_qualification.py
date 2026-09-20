"""Open-loop parent/patch replay for a recorded native SFC qualification.

This tool reuses recorded sensor, RTDE, and published-packet inputs. It does
not model plant motion or predict a new physical force. The host qdot-history
intersection is intentionally outside this controller-law comparison; the
recorded RTDE qd and timestamps remain admitted inputs and are reported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from contact_qp import QP_BOUND_VALIDATION_TOLERANCE, QP_EQUALITY_VALIDATION_TOLERANCE
from contact_yield_controller import YieldController, YieldSettings
from contact_yield_math import projector_tangent
from contact_yield_normal import NormalEstimator
from contact_yield_protocol import QP_LIBRARY_PATH
from step5c_calibrated_kinematics_audit import build_calibrated_model, rotvec_to_matrix
from step5d_autotune_v4_r004.calibrated_runtime import tcp_jacobian_base
from yield_native_route import load_observer_parameters, load_route_config


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = EXPERIMENT_ROOT / "runs/yield-sfc-qualification-20260920T120659Z"
DEFAULT_HOME = EXPERIMENT_ROOT / "report/contact-six-qp-20260917/preserved-home.json"
PARENT_COMMIT = "69c818c7"
ORIGINAL_PATCH_COMMIT = "1f4c44b4"
CONTROLLER_RELATIVE = "experiments/tase-contact-reproduction/tools/contact_yield_controller.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def _load_controller_at_commit(repo_root: Path, commit: str, module_name: str):
    source = subprocess.check_output(
        ["git", "show", f"{commit}:{CONTROLLER_RELATIVE}"],
        cwd=repo_root,
    )
    module = types.ModuleType(module_name)
    module.__file__ = str(repo_root / CONTROLLER_RELATIVE)
    sys.modules[module_name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module.YieldController, hashlib.sha256(source).hexdigest()


def _recorded_inputs(run_dir: Path) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], dict[int, tuple[float, dict[str, Any]]], dict[str, Any]]:
    frames = [
        row
        for row in _jsonl(run_dir / "robot_frames.jsonl")
        if row["integer_echoes"].get("26") == 21
    ]
    sensors = {int(row["packet_sequence"]): row for row in _jsonl(run_dir / "raw_sensor.jsonl")}
    packets = {}
    for published_at, payload in _jsonl(run_dir / "published_packets.jsonl"):
        sequence = int(payload["sequence"])
        if payload["command_mode"] == 1:
            packets[sequence] = (float(published_at), payload)
    if not frames or not sensors or not packets:
        raise ValueError("recorded qualification inputs are incomplete")

    sequences = sorted(packets)
    matched: list[dict[str, Any]] = []
    frame_index = 0
    previous_frame_index = -1
    stopped_tail = 0
    for index, sequence in enumerate(sequences):
        published_at = packets[sequence][0]
        while (
            frame_index + 1 < len(frames)
            and frames[frame_index + 1]["received_monotonic_s"] <= published_at
        ):
            frame_index += 1
        if frame_index <= previous_frame_index:
            stopped_tail = len(sequences) - index
            break
        frame = frames[frame_index]
        if frame["received_monotonic_s"] > published_at:
            raise ValueError("packet/frame alignment starts with a future RTDE row")
        if sequence not in sensors:
            raise ValueError(f"sensor packet {sequence} is missing")
        matched.append(frame)
        previous_frame_index = frame_index

    if not matched:
        raise ValueError("no unique packet/frame pairs were aligned")
    selected_sequences = sequences[: len(matched)]
    # The stream is monotonic and the only omitted entries are the tail after
    # the last recorded state21 row; reject a gap in the selected input span.
    if selected_sequences != sequences[: len(selected_sequences)]:
        raise ValueError("packet/frame alignment omitted a non-tail packet")
    aligned = {
        "packet_sequence_start": selected_sequences[0],
        "packet_sequence_end": selected_sequences[-1],
        "packet_count": len(selected_sequences),
        "state21_frame_count": len(frames),
        "unmatched_baseline_tail_packets": stopped_tail,
        "pairing": "latest unique state21 RTDE row received before packet publish time",
        "clock": "packet publish host monotonic time",
    }
    return matched, sensors, packets, {"sequences": selected_sequences, "alignment": aligned}


def _controller(cls, *, approach: np.ndarray, route: dict[str, Any], observer: dict[str, Any], settings: YieldSettings):
    return cls(
        method="SFC",
        qp_library=QP_LIBRARY_PATH,
        approach_inward_base=approach,
        settings=settings,
        law_parameters=route["law_parameters"]["SFC"],
        dt_s=0.002,
        qp_deadline_s=None,
        estimator=NormalEstimator(approach, **observer),
        allow_pre_path_force_ramp=True,
    )


def _replay(
    cls,
    *,
    frames: list[dict[str, Any]],
    sensors: dict[int, dict[str, Any]],
    packets: dict[int, tuple[float, dict[str, Any]]],
    sequences: list[int],
    anchor: np.ndarray,
    model,
    settings: YieldSettings,
) -> dict[str, Any]:
    first_publish = packets[sequences[0]][0]
    controller = _controller(
        cls,
        approach=rotvec_to_matrix(np.asarray(json.loads(DEFAULT_HOME.read_text())["home_pose"][3:]))[:, 2],
        route=load_route_config(),
        observer=load_observer_parameters(),
        settings=settings,
    )
    rows = []
    previous_publish = None
    try:
        for frame, sequence in zip(frames, sequences):
            published_at, packet = packets[sequence]
            dt = 0.002 if previous_publish is None else published_at - previous_publish
            if not 0.0 < dt <= 0.004:
                raise ValueError(f"recorded controller dt outside bounds at {sequence}: {dt}")
            pose = np.asarray(frame["tcp_pose_m_rad"], dtype=float)
            q = np.asarray(frame["q_rad"], dtype=float)
            qd = np.asarray(frame["qd_rad_s"], dtype=float)
            speed = np.asarray(frame["tcp_speed_m_s_rad_s"], dtype=float)
            if q.shape != (6,) or qd.shape != (6,) or not np.all(np.isfinite(qd)):
                raise ValueError(f"recorded joint state is malformed at {sequence}")
            rotation = rotvec_to_matrix(pose[3:])
            wrench = np.asarray(sensors[sequence]["corrected_wrench_n_nm"], dtype=float)
            lower = np.maximum((model.model.lowerPositionLimit - q) / 0.004, -0.05)
            upper = np.minimum((model.model.upperPositionLimit - q) / 0.004, 0.05)
            margin = 2.0 * QP_BOUND_VALIDATION_TOLERANCE
            observation = {
                "time_s": published_at - first_publish,
                "state_age_s": max(
                    published_at - float(sensors[sequence]["received_monotonic_s"]),
                    published_at - float(frame["received_monotonic_s"]),
                ),
                "position_m": pose[:3],
                "rotation": rotation,
                "raw_force_base_n": rotation @ wrench[:3],
                "raw_torque_base_nm": rotation @ wrench[3:],
                "jacobian": tcp_jacobian_base(model, q, frame["tcp_offset_m_rad"][:3]),
                "joint_velocity_lower": lower + margin,
                "joint_velocity_upper": upper - margin,
                "linear_velocity_base_m_s": speed[:3],
            }
            reference = {
                "phase": "baseline",
                "path_time_s": None,
                "force_n": float(packet["double_values"][20]),
                "position_m": anchor,
                "velocity_m_s": (0.0, 0.0, 0.0),
            }
            result = controller.step(observation, reference, dt)
            inward = np.asarray(result["inward_normal_base"], dtype=float)
            tangent = projector_tangent(inward)
            rows.append(
                {
                    "sequence": sequence,
                    "dt_s": dt,
                    "sensor_age_s": observation["state_age_s"],
                    "recorded_qd_max_abs_rad_s": float(np.max(np.abs(qd))),
                    "command_tangent_m_s": float(np.linalg.norm(tangent @ np.asarray(result["twist_base"][:3]))),
                    "applied_tangent_m_s": float(np.linalg.norm(tangent @ np.asarray(result["applied_twist_base"][:3]))),
                    "residual_tangent_n": float(np.linalg.norm(tangent @ np.asarray(result["force_residual_base_n"]))),
                    "result": result,
                }
            )
            previous_publish = published_at
    finally:
        controller.close()
    return {
        "ticks": len(rows),
        "peak_command_tangent_m_s": max(row["command_tangent_m_s"] for row in rows),
        "peak_applied_tangent_m_s": max(row["applied_tangent_m_s"] for row in rows),
        "integrated_applied_tangent_m": sum(row["applied_tangent_m_s"] * row["dt_s"] for row in rows),
        "peak_residual_tangent_n": max(row["residual_tangent_n"] for row in rows),
        "max_sensor_age_s": max(row["sensor_age_s"] for row in rows),
        "max_recorded_qd_rad_s": max(row["recorded_qd_max_abs_rad_s"] for row in rows),
    }


def run(
    *,
    run_dir: Path = DEFAULT_RUN,
    parent_commit: str = PARENT_COMMIT,
    original_patch_commit: str = ORIGINAL_PATCH_COMMIT,
) -> dict[str, Any]:
    repo_root = EXPERIMENT_ROOT.parents[1]
    frames, sensors, packets, alignment = _recorded_inputs(run_dir)
    sequences = alignment["sequences"]
    home = json.loads(DEFAULT_HOME.read_text(encoding="utf-8"))
    home_pose = np.asarray(home["home_pose"], dtype=float)
    anchor = home_pose[:3]
    basis = rotvec_to_matrix(home_pose[3:])
    route = load_route_config()
    observer = load_observer_parameters(route)
    model = build_calibrated_model()
    requested = YieldSettings()
    cartesian_margin = 2.0 * math.sqrt(3.0) * QP_EQUALITY_VALIDATION_TOLERANCE
    settings = replace(
        requested,
        normal_speed_cap_m_s=requested.normal_speed_cap_m_s - cartesian_margin,
        tangent_speed_cap_m_s=requested.tangent_speed_cap_m_s - cartesian_margin,
        angular_speed_cap_rad_s=requested.angular_speed_cap_rad_s - cartesian_margin,
    )
    parent_cls, parent_source_sha256 = _load_controller_at_commit(
        repo_root, parent_commit, "contact_yield_controller_parent_replay"
    )
    original_patch_cls, original_patch_source_sha256 = _load_controller_at_commit(
        repo_root, original_patch_commit, "contact_yield_controller_original_patch_replay"
    )
    patched_source = EXPERIMENT_ROOT / "tools/contact_yield_controller.py"
    result = {
        "schema": "ur10e.contact-yield-open-loop-replay-v1",
        "claim_scope": (
            "recorded-input controller-law comparison only; open-loop replay, "
            "not a prediction of new physical force or closed-loop robot behavior"
        ),
        "source_run": str(run_dir.relative_to(EXPERIMENT_ROOT)),
        "versions": {
            "parent": {
                "commit": parent_commit,
                "controller_sha256": parent_source_sha256,
            },
            "original_patch": {
                "commit": original_patch_commit,
                "controller_sha256": original_patch_source_sha256,
            },
            "candidate_worktree": {
                "commit": "working-tree",
                "controller_sha256": _sha256(patched_source),
            },
        },
        "method": "SFC",
        "phase": "baseline",
        "input_alignment": alignment["alignment"],
        "task": {
            "force_n": 5.0,
            "path_shape": "figure_eight",
            "amplitudes_m": [0.04, 0.01],
            "span_mm": [80.0, 20.0],
            "period_s": 2.0 * math.pi / 0.1,
            "home_pose": home_pose.tolist(),
            "task_basis": basis.tolist(),
            "approach_inward_base": basis[:, 2].tolist(),
        },
        "initialization_assumption": {
            "controller_state": "fresh cold controller per version",
            "normal_estimator": "Home task basis +Z approach mapped to inward base normal",
            "filter_native_law_qp": "zero initial state",
            "host_command_history": "excluded from law comparison; recorded qd is admitted and summarized",
            "physical_closed_loop": False,
        },
        "baseline_policy": {
            "policy": "suppress_native_tangent_command",
            "physical_guarantee": "none; zero tangent command does not guarantee stationary TCP or zero XY motion",
            "residual": "estimated-outward-normal projection applies only in baseline; entry and PATH retain full 3D residual",
            "native_tangent_state": "retained and advanced for diagnostics; not reset",
            "lateral_restoring": "withheld from the baseline command, including pre_integral tangent restoring; this removes lateral restoring authority during baseline",
            "physical_limitation": "disturbance and estimated-normal tilt can still produce physical XY motion",
            "entry_release": "entry resumes native full 3D tangent response, so the first entry tick can jump from the suppressed baseline command",
        },
    }
    result["parent"] = _replay(
        parent_cls,
        frames=frames[: len(sequences)],
        sensors=sensors,
        packets=packets,
        sequences=sequences,
        anchor=anchor,
        model=model,
        settings=settings,
    )
    result["original_patch"] = _replay(
        original_patch_cls,
        frames=frames[: len(sequences)],
        sensors=sensors,
        packets=packets,
        sequences=sequences,
        anchor=anchor,
        model=model,
        settings=settings,
    )
    result["candidate_worktree"] = _replay(
        YieldController,
        frames=frames[: len(sequences)],
        sensors=sensors,
        packets=packets,
        sequences=sequences,
        anchor=anchor,
        model=model,
        settings=settings,
    )
    result["comparison"] = {
        "original_patch_integrated_applied_tangent_reduction_fraction": 1.0
        - result["original_patch"]["integrated_applied_tangent_m"]
        / result["parent"]["integrated_applied_tangent_m"],
        "candidate_integrated_applied_tangent_reduction_fraction": 1.0
        - result["candidate_worktree"]["integrated_applied_tangent_m"]
        / result["parent"]["integrated_applied_tangent_m"],
        "original_patch_peak_applied_tangent_reduction_fraction": 1.0
        - result["original_patch"]["peak_applied_tangent_m_s"]
        / result["parent"]["peak_applied_tangent_m_s"],
        "candidate_peak_applied_tangent_reduction_fraction": 1.0
        - result["candidate_worktree"]["peak_applied_tangent_m_s"]
        / result["parent"]["peak_applied_tangent_m_s"],
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--parent-commit", default=PARENT_COMMIT)
    parser.add_argument("--original-patch-commit", default=ORIGINAL_PATCH_COMMIT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(
        run_dir=args.run_dir,
        parent_commit=args.parent_commit,
        original_patch_commit=args.original_patch_commit,
    )
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(args.output)


if __name__ == "__main__":
    main()
