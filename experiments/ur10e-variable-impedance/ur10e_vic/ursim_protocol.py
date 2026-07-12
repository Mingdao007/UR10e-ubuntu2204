"""Hash-bound URSim 5.25.2 protocol contracts with no runtime transport."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping


REQUIRED_PROTOCOL_STEPS = (
    "urscript_parser_load",
    "rtde_register_roundtrip",
    "zero_input_equilibrium_hold",
    "force_filter_500hz",
    "torque_loop_500hz",
    "heartbeat_loss_controlled_stop",
    "sequence_gap_controlled_stop",
    "missed_tick_controlled_stop",
    "continuous_60s_timing",
)


@dataclass(frozen=True)
class URSimProtocolDecision:
    accepted: bool
    stage: str
    blockers: tuple[str, ...]
    protocol_fingerprint_sha256: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def validate_protocol_spec(
    spec: Mapping[str, Any], *, experiment_root: Path
) -> URSimProtocolDecision:
    """Validate the immutable exact-5.25.2 protocol and source bindings."""

    blockers: list[str] = []
    target = spec.get("target", {})
    if target.get("polyscope_version") != "5.25.2":
        blockers.append("target_must_be_exact_5_25_2")
    if target.get("robot_model") != "ur10e":
        blockers.append("target_robot_must_be_ur10e")
    if target.get("control_rate_hz") != 500:
        blockers.append("control_rate_must_be_500hz")
    if target.get("direct_torque_generation") != "V2":
        blockers.append("direct_torque_generation_must_be_v2")
    if target.get("image") != "universalrobots/ursim_e-series:5.25.2":
        blockers.append("ursim_image_reference_mismatch")

    policy = spec.get("runtime_policy", {})
    if policy.get("default_mode") != "dry_run":
        blockers.append("runtime_default_must_be_dry_run")
    if policy.get("host_policy") != "loopback_only":
        blockers.append("runtime_host_policy_must_be_loopback_only")
    if policy.get("container_start_allowed") is not False:
        blockers.append("runner_must_not_start_container")
    if policy.get("image_pull_allowed") is not False:
        blockers.append("runner_must_not_pull_image")
    if policy.get("controller_transport_present") is not False:
        blockers.append("offline_runner_must_not_have_controller_transport")
    if policy.get("runtime_acceptance_enabled") is not False:
        blockers.append("unimplemented_runtime_adapter_must_not_enable_acceptance")
    if policy.get("runtime_adapter_sha256") is not None:
        blockers.append("unimplemented_runtime_adapter_must_not_have_hash")

    steps = spec.get("protocol_steps")
    if steps != list(REQUIRED_PROTOCOL_STEPS):
        blockers.append("protocol_step_set_or_order_mismatch")

    timing = spec.get("timing_acceptance", {})
    expected_timing = {
        "duration_s": 60,
        "minimum_ticks": 30_000,
        "deadline_s": 0.002,
        "p99_max_s": 0.0018,
        "max_strictly_less_than_s": 0.002,
        "deadline_misses": 0,
        "nonfinite_outputs": 0,
    }
    if timing != expected_timing:
        blockers.append("timing_acceptance_contract_mismatch")

    bindings = spec.get("source_bindings", [])
    if not isinstance(bindings, list) or len(bindings) != 2:
        blockers.append("source_bindings_incomplete")
    else:
        for binding in bindings:
            relative = binding.get("path")
            expected_hash = binding.get("sha256")
            if not isinstance(relative, str) or not re.fullmatch(
                r"[0-9a-f]{64}", str(expected_hash)
            ):
                blockers.append("source_binding_invalid")
                continue
            path = experiment_root / relative
            if not path.is_file():
                blockers.append(f"source_binding_missing:{relative}")
            elif _sha256(path) != expected_hash:
                blockers.append(f"source_binding_hash_mismatch:{relative}")

    fingerprint = _canonical_sha256(dict(spec))
    return URSimProtocolDecision(
        accepted=not blockers,
        stage="deterministic_spec_validated" if not blockers else "invalid_spec",
        blockers=tuple(blockers),
        protocol_fingerprint_sha256=fingerprint,
    )


def dry_run_packet(
    spec: Mapping[str, Any], *, experiment_root: Path
) -> dict[str, Any]:
    """Return a structured, non-executing packet for a future URSim adapter."""

    decision = validate_protocol_spec(spec, experiment_root=experiment_root)
    return {
        "schema": "ur10e_ursim_5_25_2_protocol_packet_v1",
        "mode": "dry_run",
        "spec_valid": decision.accepted,
        "protocol_fingerprint_sha256": decision.protocol_fingerprint_sha256,
        "protocol_execution_performed": False,
        "runtime_accepted": False,
        "availability": "unavailable_until_loopback_runtime_and_adapter_exist",
        "blockers": list(decision.blockers) + ["runtime_not_executed"],
        "container_started": False,
        "image_pulled": False,
        "controller_connected": False,
        "robot_motion": False,
    }


def evaluate_runtime_result(
    result: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    experiment_root: Path,
) -> URSimProtocolDecision:
    """Evaluate externally captured loopback URSim evidence; never open a socket."""

    spec_decision = validate_protocol_spec(spec, experiment_root=experiment_root)
    blockers = list(spec_decision.blockers)
    policy = spec.get("runtime_policy", {})
    if (
        policy.get("runtime_acceptance_enabled") is not True
        or not re.fullmatch(r"[0-9a-f]{64}", str(policy.get("runtime_adapter_sha256", "")))
    ):
        blockers.append("runtime_adapter_not_implemented_or_hash_bound")
    if result.get("protocol_fingerprint_sha256") != spec_decision.protocol_fingerprint_sha256:
        blockers.append("protocol_fingerprint_mismatch")
    if result.get("runtime_kind") != "ursim":
        blockers.append("runtime_kind_must_be_ursim")
    if result.get("polyscope_version") != "5.25.2":
        blockers.append("runtime_version_must_be_exact_5_25_2")
    if not _is_loopback(str(result.get("host", ""))):
        blockers.append("runtime_host_must_be_loopback")
    if result.get("protocol_execution_performed") is not True:
        blockers.append("protocol_execution_not_performed")
    digest = str(result.get("image_digest", ""))
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        blockers.append("ursim_image_digest_missing_or_invalid")
    if result.get("controller_connected") is not False:
        blockers.append("physical_controller_connection_forbidden")
    if result.get("robot_motion") is not False:
        blockers.append("physical_robot_motion_forbidden")

    outcomes = result.get("step_outcomes", {})
    if not isinstance(outcomes, Mapping):
        blockers.append("step_outcomes_missing")
    else:
        for step in REQUIRED_PROTOCOL_STEPS:
            if outcomes.get(step) is not True:
                blockers.append(f"protocol_step_failed:{step}")

    timing = result.get("timing", {})
    numeric_fields = ("duration_s", "ticks", "p99_s", "max_s")
    if not isinstance(timing, Mapping) or not all(
        _is_finite_number(timing.get(field)) for field in numeric_fields
    ):
        blockers.append("timing_result_missing_or_nonfinite")
    else:
        if float(timing["duration_s"]) < 60.0:
            blockers.append("timing_duration_below_60s")
        if int(timing["ticks"]) < 30_000:
            blockers.append("timing_tick_count_below_30000")
        if float(timing["p99_s"]) > 0.0018:
            blockers.append("timing_p99_exceeds_1_8ms")
        if float(timing["max_s"]) >= 0.002:
            blockers.append("timing_max_not_below_2ms")
        if timing.get("deadline_misses") != 0:
            blockers.append("timing_deadline_miss")
        if timing.get("nonfinite_outputs") != 0:
            blockers.append("timing_nonfinite_output")

    return URSimProtocolDecision(
        accepted=not blockers,
        stage="ursim_runtime_validated" if not blockers else "ursim_runtime_rejected",
        blockers=tuple(blockers),
        protocol_fingerprint_sha256=spec_decision.protocol_fingerprint_sha256,
    )
