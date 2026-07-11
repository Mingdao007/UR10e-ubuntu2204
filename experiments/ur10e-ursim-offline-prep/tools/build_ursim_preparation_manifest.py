#!/usr/bin/env python3
"""Build immutable, explicitly unavailable URSim preparation lanes.

This tool performs only local file reads and Python/static contract checks.  It
does not call a container runtime, open a socket, upload a program, or execute
URScript.  Consequently its v1 output can never assert a URSim protocol pass.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence


SCHEMA = "ur10e_ursim_preparation_manifest_v1"
SPEC_SCHEMA = "ur10e_ursim_lane_spec_v1"
P0_LANE = "ursim_5_11_p0_layout524_protocol"
TORQUE_LANE = "ursim_5_23_direct_torque_software"
LANE_IDS = (P0_LANE, TORQUE_LANE)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^5\.[0-9]+(?:\.[0-9]+){1,2}$")


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_spec_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "ursim_lane_spec_v1.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _repository_path(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("binding path must be repository-relative without '..'")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("binding path escapes repository root")
    if not resolved.is_file():
        raise ValueError(f"bound file is missing: {value}")
    return resolved


def validate_spec(payload: Mapping[str, Any]) -> None:
    if payload.get("schema") != SPEC_SCHEMA:
        raise ValueError("unsupported URSim lane spec schema")
    boundary = str(payload.get("claim_boundary", ""))
    if "static contract validation is not URSim protocol execution" not in boundary:
        raise ValueError("URSim preparation claim boundary drifted")
    environment = payload.get("runtime_environment")
    if not isinstance(environment, Mapping):
        raise ValueError("runtime_environment is required")
    expected_environment = {
        "container_service": "inactive",
        "image_inventory": "not_found",
        "mutation_authorized": False,
    }
    for key, expected in expected_environment.items():
        if environment.get(key) != expected:
            raise ValueError(f"runtime environment gate drifted: {key}")

    lanes = payload.get("lanes")
    if not isinstance(lanes, list) or len(lanes) != 2:
        raise ValueError("URSim preparation requires exactly two lanes")
    if tuple(lane.get("id") for lane in lanes) != LANE_IDS:
        raise ValueError("URSim lane order or identity drifted")
    expected_claims = {
        P0_LANE: "p0_ursim_protocol_pass",
        TORQUE_LANE: "direct_torque_ursim_software_pass",
    }
    for lane in lanes:
        lane_id = lane["id"]
        if lane.get("claim_id") != expected_claims[lane_id]:
            raise ValueError(f"claim identity drifted for {lane_id}")
        version = lane.get("polyscope_version")
        if not isinstance(version, Mapping) or version.get("constraint") != "exact":
            raise ValueError(f"{lane_id} must pin an exact PolyScope version")
        if not VERSION_RE.fullmatch(str(version.get("value", ""))):
            raise ValueError(f"invalid pinned PolyScope version for {lane_id}")
        image = lane.get("image")
        if not isinstance(image, Mapping) or image.get("digest_required") is not True:
            raise ValueError(f"{lane_id} must require a digest-bound URSim image")
        if (
            image.get("digest") is not None
            or image.get("reference") is not None
            or image.get("availability") != "not_found"
        ):
            raise ValueError(
                "preparation v1 records the absent image; executed evidence needs a new manifest"
            )
        bindings = lane.get("bindings")
        if not isinstance(bindings, list) or not bindings:
            raise ValueError(f"{lane_id} requires immutable source bindings")
        roles: set[str] = set()
        for binding in bindings:
            role = str(binding.get("role", ""))
            if not role or role in roles:
                raise ValueError(f"duplicate or empty binding role in {lane_id}")
            roles.add(role)
            if not SHA256_RE.fullmatch(str(binding.get("sha256", ""))):
                raise ValueError(f"invalid binding hash in {lane_id}: {role}")
            path = Path(str(binding.get("path", "")))
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"unsafe binding path in {lane_id}: {role}")
        checks = lane.get("required_runtime_checks")
        if not isinstance(checks, list) or not checks or len(checks) != len(set(checks)):
            raise ValueError(f"runtime checks must be a non-empty unique list: {lane_id}")


def _verify_bindings(
    lane: Mapping[str, Any], root: Path
) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    verified: list[dict[str, Any]] = []
    paths: dict[str, Path] = {}
    for binding in lane["bindings"]:
        role = binding["role"]
        path = _repository_path(root, binding["path"])
        actual = sha256_file(path)
        if actual != binding["sha256"]:
            raise ValueError(f"binding hash drifted: {role}")
        verified.append(
            {
                "role": role,
                "path": binding["path"],
                "sha256": actual,
                "verified": True,
            }
        )
        paths[role] = path
    return verified, paths


def _import_from_tools(tool_root: Path, module_name: str):
    value = str(tool_root)
    if value not in sys.path:
        sys.path.insert(0, value)
    return importlib.import_module(module_name)


def _p0_static_contract(paths: Mapping[str, Path], root: Path) -> dict[str, bool]:
    liveprep = _import_from_tools(
        root / "experiments" / "tase-contact-reproduction" / "tools",
        "build_step5d_liveprep",
    )
    runtime_interface = _import_from_tools(
        root / "experiments" / "tase-contact-reproduction" / "tools",
        "step5d_runtime_interface",
    )
    marker = json.loads(paths["p0_v8_candidate_manifest"].read_text(encoding="utf-8"))
    profile = "step5d_strict_rnn_no_contact_p0_v8"
    script = paths["p0_v8_script"].read_text(encoding="utf-8")
    txt = paths["p0_v8_txt"].read_text(encoding="utf-8")
    urp = paths["p0_v8_urp"].read_bytes()
    for suffix, role in (
        (".script", "p0_v8_script"),
        (".txt", "p0_v8_txt"),
        (".urp", "p0_v8_urp"),
    ):
        if marker.get("sha256", {}).get(suffix) != sha256_file(paths[role]):
            raise ValueError(f"P0 candidate manifest hash drifted for {suffix}")
    spec = liveprep.spec_for(profile)
    liveprep.validate_package(script, txt, urp, marker["stamp"], spec)
    experiment_root = root / "experiments" / "tase-contact-reproduction"
    runtime = runtime_interface.resolve_runtime_interface(
        program=profile,
        root=experiment_root,
        env={},
    )
    input_registers = {
        int(value)
        for value in re.findall(r"read_input_float_register\((\d+)\)", script)
    }
    output_registers = {
        int(value)
        for value in re.findall(r"write_output_float_register\((\d+)", script)
    }
    required_input = {24, 25, 26, 27, 28, 30, *range(37, 44), 47}
    required_output = {26, 27, 28, 29, 30, 31, 35, 36, 47}
    checks = {
        "package_triplet_validator_reused": True,
        "candidate_local_only_not_delivered": bool(
            marker.get("local_only") and marker.get("not_delivered")
        ),
        "runtime_contract_reused": bool(
            runtime.program == profile
            and runtime.controller_target == "LOCAL_ONLY_NOT_DELIVERED"
            and "accepts only 47=524" in runtime.register_contract["stage25_0"]
        ),
        "rtde_input_register_contract": required_input <= input_registers,
        "rtde_output_register_contract": required_output <= output_registers,
        "layout524_joint_only": bool(
            "accepts only register 47=524.0" in script
            and "local joint_layout_code = 524.000" in script
            and "if cmd_valid < 0.5 or not joint_layout_ok" in script
            and "speedl([cmd_vx" not in script
        ),
        "layout524_payload_registers_declared": bool(
            "qd0..qd5; 43 cmd_valid, 44 path_time_s" in script
            and required_input <= input_registers
        ),
        "heartbeat_stale_guard_static": bool(
            "local last_heartbeat2 = read_input_float_register(26)" in script
            and "if stale_s2 > 0.100" in script
            and "stop_reason = 2.0" in script
        ),
        "bounded_speedj_tick_static": bool(
            "local qdot_cap_rad_s = 0.050" in script
            and (
                "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, "
                "cmd_qd5], joint_accel_rad_s2, 0.002)"
            )
            in script
        ),
        "speedj_safe_exit_static": bool(
            "stopj(0.3)" in script and "stopl(0.1)" in script
        ),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ValueError(f"P0 5.11 static contract failed: {failed}")
    return checks


def _torque_packet(backends, sequence: int, **overrides):
    stiffness = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
    mass = (2.0, 2.0, 2.0, 0.2, 0.2, 0.2)
    damping = tuple(
        2.0 * math.sqrt(stiffness[index] * mass[index]) for index in range(6)
    )
    values = {
        "sequence_before": sequence,
        "sequence_after": sequence,
        "heartbeat": sequence,
        "lease_id": 77,
        "mode": 1,
        "equilibrium_pose": (0.4, 0.1, 0.05, 3.14, 0.0, 0.0),
        "stiffness": stiffness,
        "damping": damping,
    }
    values.update(overrides)
    return backends.DirectTorquePacket(**values)


def _torque_static_contract(
    lane: Mapping[str, Any], paths: Mapping[str, Path], root: Path
) -> dict[str, bool]:
    vic_root = root / "experiments" / "ur10e-variable-impedance"
    backends = _import_from_tools(vic_root, "ur10e_vic.backends")
    version = lane["polyscope_version"]["value"]
    backends.require_direct_torque_version(version)
    layout = backends.load_direct_torque_bundle(
        paths["direct_torque_rtde_layout"],
        paths["direct_torque_template"],
    )
    script = paths["direct_torque_template"].read_text(encoding="utf-8")
    initial = backends.DirectTorqueGuardState()
    accepted = backends.validate_direct_torque_packet(
        _torque_packet(backends, 1),
        initial,
        release_ready=True,
        runtime_guard_ok=True,
    )
    mixed = backends.validate_direct_torque_packet(
        _torque_packet(backends, 2, sequence_before=1),
        accepted.next_state,
        release_ready=False,
        runtime_guard_ok=True,
    )
    gap = backends.validate_direct_torque_packet(
        _torque_packet(backends, 3),
        accepted.next_state,
        release_ready=False,
        runtime_guard_ok=True,
    )
    invocation = "ur10e_vic_direct_torque_offline_template()"
    invoked = any(
        line.strip() == invocation
        for line in script.splitlines()
        if not line.lstrip().startswith("#")
    )
    checks = {
        "polyscope_version_parser_static": bool(
            backends.parse_polyscope_version(version) == (5, 23, 0)
            and backends.direct_torque_supported(version)
        ),
        "vic_layout_validator_reused": layout.get("schema_version") == 2,
        "rtde_register_layout_static": bool(
            layout["registers"]["input_integer"]
            == {"mode": 24, "sequence": 25, "heartbeat": 26, "exclusive_lease": 27}
            and sorted(
                index
                for indices in layout["registers"]["input_double"].values()
                for index in indices
            )
            == list(range(24, 42))
        ),
        "sequence_oracle_static": bool(
            accepted.accepted
            and not mixed.accepted
            and mixed.reason == "mixed_or_uncommitted_packet"
            and not gap.accepted
            and gap.reason == "frozen_gap_or_regressed_sequence"
        ),
        "heartbeat_sequence_commit_static": bool(
            "sequence_before == sequence_after and heartbeat == sequence" in script
            and "sequence == last_sequence + 1" in script
            and layout["heartbeat_timeout_ticks"] == 10
        ),
        "zero_damping_startup_static": bool(
            layout["zero_torque_startup_ticks"] == 5
            and "zero_startup_count < zero_startup_ticks" in script
            and "vic_safe_exit_tick()" in script
        ),
        "zero_damping_safe_exit_static": bool(
            layout["safe_exit_damping_ticks"] == 5
            and "tau[joint] = coriolis[joint] - joint_damping[joint] * qd[joint]"
            in script
            and "safe_exit_count >= safe_exit_ticks" in script
            and "stopj(10.0)" in script
        ),
        "direct_torque_template_uninvoked": bool(
            not invoked and "Intentionally no invocation" in script
        ),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ValueError(f"5.23 direct-torque static contract failed: {failed}")
    return checks


def build_manifest(spec_path: Path, root: Path | None = None) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    spec_path = spec_path.resolve()
    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    validate_spec(payload)
    try:
        relative_spec = spec_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("lane spec must live inside the repository") from exc

    results: list[dict[str, Any]] = []
    for lane in payload["lanes"]:
        verified_bindings, paths = _verify_bindings(lane, root)
        if lane["id"] == P0_LANE:
            static_contract = _p0_static_contract(paths, root)
        elif lane["id"] == TORQUE_LANE:
            static_contract = _torque_static_contract(lane, paths, root)
        else:  # validate_spec already prevents this.
            raise AssertionError(lane["id"])
        results.append(
            {
                "id": lane["id"],
                "claim_id": lane["claim_id"],
                "polyscope_version": lane["polyscope_version"],
                "image": lane["image"],
                "bindings": verified_bindings,
                "static_contract": static_contract,
                "required_runtime_checks": lane["required_runtime_checks"],
                "runtime_status": "unavailable",
                "protocol_pass": False,
                "status": "blocked",
                "blockers": [
                    "container_service_inactive",
                    "digest_bound_ursim_image_absent",
                    "ursim_runtime_evidence_absent",
                ],
            }
        )
    return {
        "schema": SCHEMA,
        "spec_path": relative_spec,
        "spec_sha256": sha256_file(spec_path),
        "status": "blocked",
        "availability": "unavailable",
        "protocol_execution_performed": False,
        "claim_boundary": payload["claim_boundary"],
        "lanes": results,
        "claims": {
            "p0_ursim_protocol_pass": False,
            "direct_torque_ursim_software_pass": False,
        },
        "live_motion_authorized": False,
        "package_accepted": False,
        "live_accepted": False,
        "reproduction_complete": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=default_spec_path())
    parser.add_argument("--repository-root", type=Path, default=repository_root())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    manifest = build_manifest(args.spec, args.repository_root)
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
