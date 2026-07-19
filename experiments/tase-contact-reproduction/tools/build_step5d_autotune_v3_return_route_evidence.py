#!/usr/bin/env python3
"""Build source-bound offline evidence for the Step5d V3 return route."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from ur10e_experiment_runtime.identity import canonical_sha256
from ur10e_experiment_runtime.return_route import (
    RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
    RETURN_ANGULAR_ACCELERATION_LIMIT_RAD_S2,
    RETURN_ANGULAR_SPEED_GUARD_RAD_S,
    RETURN_ANGULAR_SPEED_LIMIT_RAD_S,
    RETURN_ANGULAR_STOP_DECELERATION_RAD_S2,
    RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
    RETURN_CONTROLLER_PERIOD_S,
    RETURN_ORIENTATION_ADMISSION_LIMIT_RAD,
    SAFE_TRANSFER_Z_M,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = EXPERIMENT_ROOT.parents[1]
DEFAULT_OUTPUT = (
    EXPERIMENT_ROOT
    / "config/step5/step5d_autotune_v3_return_route_evidence.json"
)
PACKAGE_ROOT = EXPERIMENT_ROOT / "programs/step5/step5d"
BASENAME = "step5d_strict_rnn_autotune_v3"
SOURCE_PATHS = {
    "return_route": (
        REPOSITORY_ROOT
        / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/return_route.py"
    ),
    "tp_generator": EXPERIMENT_ROOT / "tools/build_step5d_autotune_tp_v3.py",
    "bridge_capture": EXPERIMENT_ROOT / "tools/run_step5d_autotune_v3_bridge.py",
    "campaign_adapter": EXPERIMENT_ROOT / "tools/run_step5d_autotune_campaign.py",
    "closure_collector": (
        EXPERIMENT_ROOT / "tools/step5d_autotune_runtime_lifecycle.py"
    ),
}


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"return-route evidence input is missing or symlinked: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _strict_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_document() -> dict[str, object]:
    source_sha256 = {name: _sha256(path) for name, path in SOURCE_PATHS.items()}
    triplet_sha256 = {
        suffix: _sha256(PACKAGE_ROOT / f"{BASENAME}{suffix}")
        for suffix in (".script", ".txt", ".urp")
    }
    deploy = _strict_json(PACKAGE_ROOT / f"{BASENAME}.deploy-manifest.json")
    declared = {
        str(row["filename"])[len(BASENAME) :]: row["sha256"]
        for row in deploy["artifacts"]
    }
    if declared != triplet_sha256:
        raise ValueError("return-route local triplet differs from deploy manifest")
    sanity = _strict_json(PACKAGE_ROOT / f"{BASENAME}.numeric-sanity.json")
    expected_sanity = {
        "safe_transfer_z_m": SAFE_TRANSFER_Z_M,
        "return_segment_count": 3,
        "return_controller": "speedl_bounded_twist_v1",
        "return_angular_speed_limit_rad_s": RETURN_ANGULAR_SPEED_LIMIT_RAD_S,
        "return_angular_acceleration_limit_rad_s2": (
            RETURN_ANGULAR_ACCELERATION_LIMIT_RAD_S2
        ),
        "return_angular_speed_guard_rad_s": RETURN_ANGULAR_SPEED_GUARD_RAD_S,
        "return_angular_acceleration_guard_rad_s2": (
            RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2
        ),
        "return_angular_stop_deceleration_rad_s2": (
            RETURN_ANGULAR_STOP_DECELERATION_RAD_S2
        ),
        "return_orientation_admission_limit_rad": (
            RETURN_ORIENTATION_ADMISSION_LIMIT_RAD
        ),
        "return_controller_period_s": RETURN_CONTROLLER_PERIOD_S,
        "return_controller_max_sample_gap_s": RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
        "return_sample_gap_clock": "controller_monotonic_time_mode_0",
        "return_segment_phase_codes": [40.1, 40.2, 40.3],
        "return_continuous_telemetry_output_float_registers": list(range(39, 45)),
    }
    for key, expected in expected_sanity.items():
        if sanity.get(key) != expected:
            raise ValueError(f"return-route numeric sanity differs: {key}")
    script = (PACKAGE_ROOT / f"{BASENAME}.script").read_text(encoding="utf-8")
    for marker in (
        "speedl([vx, vy, vz, wx, wy, wz]",
        "aRot=0.100",
        "controller_clock = time()",
        "controller_time_s - last_controller_time_s",
        "write_output_float_register(39, codex_autotune_return_segment_id)",
        "write_output_float_register(44, codex_autotune_return_max_sample_gap_s)",
        "stopl(0.3, 0.100)",
    ):
        if marker not in script:
            raise ValueError(f"return-route script lacks required marker: {marker}")
    return {
        "schema": "step5d.autotune-v3/return-route-evidence-v1",
        "status": "offline_enforcement_complete_attended_certification_required",
        "certified": False,
        "source_binding_sha256": canonical_sha256(source_sha256),
        "source_sha256": source_sha256,
        "local_triplet_sha256": triplet_sha256,
        "policy": expected_sanity,
        "offline_guards": {
            "dynamic_start_orientation_admission": True,
            "three_segment_route_exact": True,
            "bounded_speedl_twist": True,
            "continuous_actual_angular_speed_guard": True,
            "continuous_finite_difference_angular_acceleration_guard": True,
            "controller_monotonic_sample_gap_guard": True,
            "exact_stop_angular_deceleration": True,
            "phase_and_telemetry_capture": True,
            "pre_and_post_ack_closure_validation": True,
        },
        "missing_certification": {
            "motion_capable_ursim_trace_sha256": None,
            "attended_controller_readback_sha256": None,
            "source_exact_return_telemetry_sha256": None,
        },
        "live_effect": "return_route_angular_envelope_gate_blocked",
    }


def _encoded() -> bytes:
    return (json.dumps(build_document(), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    expected = _encoded()
    output = args.output.resolve()
    if args.check:
        if output.is_symlink() or not output.is_file() or output.read_bytes() != expected:
            raise SystemExit("return-route evidence artifact is missing or stale")
        return 0
    _write_atomic(output, expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
