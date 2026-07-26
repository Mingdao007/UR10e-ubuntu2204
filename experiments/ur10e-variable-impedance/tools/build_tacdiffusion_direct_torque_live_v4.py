#!/usr/bin/env python3
"""Build an immutable, episode-bound Direct Torque receiver v4 bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (  # noqa: E402
    LIVE_PROTOCOL_TOKEN,
    LIVE_RECEIVER_SCHEMA,
    LiveTubeContract,
    build_live_receiver_source,
    parse_live_receiver_source,
)
from ur10e_vic.tacdiffusion.unknown_surface_episode import (  # noqa: E402
    rotation_vector_distance_rad,
)


DEFAULT_REFERENCE = (
    ROOT.parent
    / "tase-contact-reproduction"
    / "runs"
    / "tacdiffusion"
    / "passive_remote_baseline_20260726"
    / "unknown_surface_anchor_circle_no_contact_2s_reference_v2.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_reference_content(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("reference artifact must contain one JSON object")
    supplied = payload.get("content_sha256")
    if not isinstance(supplied, str):
        raise ValueError("reference artifact content SHA-256 is missing")
    unsigned = dict(payload)
    unsigned.pop("content_sha256")
    canonical = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != supplied:
        raise ValueError("reference artifact content SHA-256 mismatch")
    trajectory = payload.get("trajectory")
    if not isinstance(trajectory, Mapping):
        raise ValueError("reference trajectory metadata is missing")
    if (
        trajectory.get("family") != "anchor_circle"
        or trajectory.get("target_load_n") != 0.0
        or trajectory.get("preload_n") != 0.0
        or float(trajectory.get("capture_duration_s", 0.0)) > 2.0
        or trajectory.get("initial_release_pose_ready") is not True
    ):
        raise ValueError("reference is not the bounded no-contact canary")
    rows = payload.get("references")
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("reference rows are missing")
    return payload


def _numeric_sanity(
    reference: Mapping[str, object],
    tube: LiveTubeContract,
) -> dict[str, object]:
    """Prove the bounded no-contact canary geometry before bundle handoff."""

    trajectory = reference["trajectory"]
    rows = reference["references"]
    assert isinstance(trajectory, Mapping)
    assert isinstance(rows, list)
    rate_hz = int(trajectory["rate_hz"])
    capture_duration_s = float(trajectory["capture_duration_s"])
    if rate_hz != 500 or capture_duration_s != 2.0:
        raise ValueError("live canary must contain exactly 2 s at 500 Hz")
    expected_rows = int(round(capture_duration_s * rate_hz)) + 1
    if len(rows) != expected_rows or int(trajectory["row_count"]) != expected_rows:
        raise ValueError("live canary row count is inconsistent")
    curvature = float(trajectory["max_curvature_m_inv"])
    if not math.isfinite(curvature) or curvature <= 0.0:
        raise ValueError("canary curvature is invalid")
    design_radius_m = 1.0 / curvature
    if abs(design_radius_m - 0.001) > 1e-9:
        raise ValueError("canary design radius is not 1 mm")

    anchor = tuple(float(value) for value in reference["anchor_pose_base"])
    maximum_anchor_translation_m = 0.0
    maximum_speed_m_s = 0.0
    maximum_acceleration_m_s2 = 0.0
    maximum_orientation_error_rad = 0.0
    progress: list[float] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("canary row is not an object")
        pose = tuple(float(value) for value in row["desired_pose_base"])
        twist = tuple(float(value) for value in row["desired_twist_base"])
        acceleration = tuple(float(value) for value in row["desired_acceleration_base"])
        if (
            len(pose) != 6
            or len(twist) != 6
            or len(acceleration) != 6
            or not all(math.isfinite(value) for value in (*pose, *twist, *acceleration))
        ):
            raise ValueError("canary row contains invalid kinematics")
        tube.assert_contains_pose(pose, role="desired")
        maximum_anchor_translation_m = max(
            maximum_anchor_translation_m,
            math.dist(pose[:3], anchor[:3]),
        )
        maximum_speed_m_s = max(
            maximum_speed_m_s,
            math.sqrt(sum(value * value for value in twist[:3])),
        )
        maximum_acceleration_m_s2 = max(
            maximum_acceleration_m_s2,
            math.sqrt(sum(value * value for value in acceleration[:3])),
        )
        maximum_orientation_error_rad = max(
            maximum_orientation_error_rad,
            rotation_vector_distance_rad(pose[3:], anchor[3:]),
        )
        progress.append(float(row["progress_s"]))
    if progress[0] != 0.0 or abs(progress[-1] - capture_duration_s) > 1e-12:
        raise ValueError("canary progress does not span the capture duration")
    if any(right <= left for left, right in zip(progress, progress[1:])):
        raise ValueError("canary progress is not strictly increasing")
    if maximum_anchor_translation_m > 0.002 + 1e-12:
        raise ValueError("canary path exceeds its 2 mm diameter envelope")
    if maximum_speed_m_s > 0.002:
        raise ValueError("canary speed exceeds 2 mm/s")
    if maximum_acceleration_m_s2 > 0.005:
        raise ValueError("canary acceleration exceeds 5 mm/s^2")
    if maximum_orientation_error_rad > 1e-6:
        raise ValueError("canary orientation is not constant")
    if (
        trajectory.get("target_load_n") != 0.0
        or trajectory.get("preload_n") != 0.0
        or reference.get("cad_height_or_normal_feedforward") is not False
    ):
        raise ValueError("canary is not zero-load/no-contact")
    return {
        "ok": True,
        "controller_target": "secondary_client_urscript_port_30002",
        "controller_loop_hz": 500,
        "capture_duration_s": capture_duration_s,
        "row_count": len(rows),
        "design_radius_m": design_radius_m,
        "maximum_anchor_translation_m": maximum_anchor_translation_m,
        "maximum_speed_m_s": maximum_speed_m_s,
        "maximum_acceleration_m_s2": maximum_acceleration_m_s2,
        "maximum_orientation_error_rad": maximum_orientation_error_rad,
        "normal_half_width_m": tube.normal_half_width_m,
        "target_load_n": 0.0,
        "preload_n": 0.0,
        "feedforward_wrench": [0.0] * 6,
        "receiver_wrench_limits": [20.0, 20.0, 20.0, 2.0, 2.0, 2.0],
        "receiver_joint_torque_limits_nm": [20.0, 20.0, 20.0, 8.0, 8.0, 8.0],
        "claim_class": "offline_numeric_sanity_no_live_actions",
    }


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"bundle artifact already exists: {path}")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def build(args: argparse.Namespace) -> dict[str, object]:
    reference_path = args.reference.resolve()
    reference = _validate_reference_content(reference_path)
    tube = LiveTubeContract.from_reference_artifact(reference_path)
    source = build_live_receiver_source(
        tube,
        heartbeat_timeout_ticks=args.heartbeat_timeout_ticks,
    )
    contract = parse_live_receiver_source(source)
    numeric_sanity = _numeric_sanity(reference, tube)
    source_bytes = source.encode("utf-8")
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    manifest: dict[str, object] = {
        "schema": "ur10e_tacdiffusion_direct_torque_live_bundle/v1",
        "claim_class": "offline_built_no_live_authorization",
        "receiver_schema": LIVE_RECEIVER_SCHEMA,
        "receiver_protocol_token": LIVE_PROTOCOL_TOKEN,
        "control_rate_hz": contract.control_rate_hz,
        "heartbeat_timeout_ticks": contract.heartbeat_timeout_ticks,
        "receiver_source": args.receiver_source.name,
        "receiver_source_sha256": source_sha,
        "reference_artifact": str(reference_path),
        "reference_artifact_sha256": _sha256(reference_path),
        "reference_content_sha256": reference["content_sha256"],
        "reference_rows": len(reference["references"]),
        "capture_duration_s": reference["trajectory"]["capture_duration_s"],
        "numeric_sanity": numeric_sanity,
        "tube": {
            "center_base_m": list(tube.center_base_m),
            "anchor_pose_base": list(tube.anchor_pose_base),
            "u_axis_base": list(tube.u_axis_base),
            "v_axis_base": list(tube.v_axis_base),
            "safe_u_half_width_m": tube.safe_u_half_width_m,
            "safe_v_half_width_m": tube.safe_v_half_width_m,
            "normal_half_width_m": tube.normal_half_width_m,
            "orientation_tolerance_rad": tube.orientation_tolerance_rad,
        },
        "register_contract": {
            "input_double_registers": list(range(24, 48)),
            "input_integer_registers": list(range(24, 36)),
            "output_double_registers": list(range(24, 44)),
            "output_integer_registers": list(range(24, 36)),
            "applied_action": {
                "filtered_f_ff": list(range(26, 32)),
                "stiffness_k": list(range(32, 38)),
                "commanded_joint_torque_nm": list(range(38, 44)),
            },
        },
        "gates": {
            "source_invoked": contract.invocation_present,
            "bounded_packet_hold": True,
            "complete_identity_echo": contract.complete_identity_echo,
            "applied_action_echo": contract.applied_action_echo,
            "hard_tube_guard": contract.hard_tube_guard,
            "explicit_position_handoff": contract.explicit_position_handoff,
            "single_top_level_program": True,
            "continuous_500hz_torque_site": True,
            "zero_torque_startup_or_exit": False,
            "friction_compensation": "v2_scales_blended_from_zero",
            "source_builder_physical_io": contract.source_builder_physical_io_enabled,
            "controller_runtime_physical_io": contract.controller_runtime_physical_io_enabled,
        },
        "live_actions": False,
    }
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _write_new(args.receiver_source, source_bytes)
    try:
        _write_new(args.manifest, manifest_bytes)
    except Exception:
        args.receiver_source.unlink(missing_ok=True)
        raise
    return {
        "ok": True,
        "receiver_source": str(args.receiver_source.resolve()),
        "receiver_source_sha256": source_sha,
        "manifest": str(args.manifest.resolve()),
        "manifest_sha256": _sha256(args.manifest),
        "reference_rows": len(reference["references"]),
        "live_actions": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--receiver-source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--heartbeat-timeout-ticks", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        result = build(args)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
