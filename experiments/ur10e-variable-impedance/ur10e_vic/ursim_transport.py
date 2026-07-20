"""Transport-neutral PolyScope 5.26 URSim request and capture contracts.

No production transport is implemented here.  In particular this module does
not import socket, RTDE, Docker, subprocess, or ROS.  A fake transport supports
deterministic contract tests but is structurally unable to claim a simulation
run.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, runtime_checkable


SPEC_SCHEMA = "ur10e_ursim_protocol/v2"
REQUEST_SCHEMA = "ur10e_ursim_request/v1"
CAPTURE_SCHEMA = "ur10e_ursim_capture/v1"
TARGET_VERSION_PREFIX = "5.26."
REQUIRED_STEPS = (
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
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


@dataclass(frozen=True)
class URSimRequest:
    schema: str
    protocol_fingerprint_sha256: str
    target_version: str
    host: str
    steps: tuple[str, ...]
    execution_authorized: bool

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "protocol_fingerprint_sha256": self.protocol_fingerprint_sha256,
            "target_version": self.target_version,
            "host": self.host,
            "steps": list(self.steps),
            "execution_authorized": self.execution_authorized,
        }


@dataclass(frozen=True)
class URSimCaptureDecision:
    accepted: bool
    simulation_run: bool
    stage: str
    blockers: tuple[str, ...]
    capture_fingerprint_sha256: str


@runtime_checkable
class URSimTransport(Protocol):
    """Injected transport boundary; implementations live outside this package."""

    @property
    def transport_kind(self) -> str: ...

    def execute(self, request: URSimRequest) -> Mapping[str, Any]: ...


class FakeURSimTransport:
    """Deterministic test double that can never provide URSim evidence."""

    transport_kind = "fake"

    def __init__(self, overrides: Mapping[str, Any] | None = None) -> None:
        self._overrides = dict(overrides or {})

    def execute(self, request: URSimRequest) -> Mapping[str, Any]:
        capture: dict[str, Any] = {
            "schema": CAPTURE_SCHEMA,
            "request_fingerprint_sha256": request.protocol_fingerprint_sha256,
            "transport_kind": self.transport_kind,
            "runtime_kind": "fake",
            "polyscope_version": request.target_version,
            "host": request.host,
            "runtime_image_digest": "",
            "runtime_fingerprint_sha256": "",
            "protocol_execution_performed": True,
            "physical_controller_connected": False,
            "robot_motion_observed": False,
            "step_outcomes": {step: True for step in request.steps},
            "timing": {
                "duration_s": 60.0,
                "ticks": 30_000,
                "p99_s": 0.001,
                "max_s": 0.0015,
                "deadline_misses": 0,
                "nonfinite_outputs": 0,
            },
        }
        capture.update(self._overrides)
        return capture


def validate_spec(spec: Mapping[str, Any], *, experiment_root: Path) -> tuple[str, ...]:
    blockers: list[str] = []
    if spec.get("schema") != SPEC_SCHEMA:
        blockers.append("spec_schema_mismatch")
    target = spec.get("target", {})
    if not isinstance(target, Mapping):
        target = {}
    if target.get("polyscope_version") != "5.26.0":
        blockers.append("target_must_be_exact_5_26_0")
    if target.get("robot_model") != "ur10e":
        blockers.append("target_robot_must_be_ur10e")
    if target.get("control_rate_hz") != 500:
        blockers.append("control_rate_must_be_500hz")
    if target.get("direct_torque_generation") != "V2":
        blockers.append("direct_torque_generation_must_be_v2")

    policy = spec.get("runtime_policy", {})
    if not isinstance(policy, Mapping):
        policy = {}
    expected_policy = {
        "host_policy": "loopback_only",
        "container_start_allowed": False,
        "image_pull_allowed": False,
        "physical_controller_allowed": False,
        "production_transport_in_repository": False,
        "fake_transport_can_promote_simulation": False,
    }
    for key, expected in expected_policy.items():
        if policy.get(key) != expected:
            blockers.append(f"runtime_policy_mismatch:{key}")
    if not _SHA256_RE.fullmatch(str(policy.get("runtime_adapter_sha256", ""))):
        blockers.append("runtime_adapter_not_hash_bound")
    else:
        adapter = experiment_root / "ur10e_vic" / "ursim_transport.py"
        if not adapter.is_file() or _file_sha256(adapter) != policy["runtime_adapter_sha256"]:
            blockers.append("runtime_adapter_hash_mismatch")

    if spec.get("protocol_steps") != list(REQUIRED_STEPS):
        blockers.append("protocol_step_set_or_order_mismatch")
    for binding in spec.get("source_bindings", ()):
        if not isinstance(binding, Mapping):
            blockers.append("source_binding_invalid")
            continue
        relative = binding.get("path")
        digest = str(binding.get("sha256", ""))
        if not isinstance(relative, str) or not _SHA256_RE.fullmatch(digest):
            blockers.append("source_binding_invalid")
            continue
        path = experiment_root / relative
        if not path.is_file():
            blockers.append(f"source_binding_missing:{relative}")
        elif _file_sha256(path) != digest:
            blockers.append(f"source_binding_hash_mismatch:{relative}")
    return tuple(dict.fromkeys(blockers))


def build_request(spec: Mapping[str, Any], *, experiment_root: Path) -> URSimRequest:
    blockers = validate_spec(spec, experiment_root=experiment_root)
    target = spec.get("target", {})
    return URSimRequest(
        schema=REQUEST_SCHEMA,
        protocol_fingerprint_sha256=canonical_sha256(dict(spec)),
        target_version=str(target.get("polyscope_version", "")),
        host="127.0.0.1",
        steps=tuple(spec.get("protocol_steps", ())),
        execution_authorized=not blockers,
    )


def evaluate_capture(
    capture: Mapping[str, Any], request: URSimRequest
) -> URSimCaptureDecision:
    blockers: list[str] = []
    if not request.execution_authorized:
        blockers.append("request_not_authorized_by_spec")
    if capture.get("schema") != CAPTURE_SCHEMA:
        blockers.append("capture_schema_mismatch")
    if capture.get("request_fingerprint_sha256") != request.protocol_fingerprint_sha256:
        blockers.append("request_fingerprint_mismatch")
    if capture.get("transport_kind") != "ursim_loopback":
        blockers.append("non_ursim_transport_cannot_promote_simulation")
    if capture.get("runtime_kind") != "ursim":
        blockers.append("runtime_kind_must_be_ursim")
    if not str(capture.get("polyscope_version", "")).startswith(TARGET_VERSION_PREFIX):
        blockers.append("runtime_version_must_be_5_26_x")
    if not _is_loopback(str(capture.get("host", ""))):
        blockers.append("runtime_host_must_be_loopback")
    if capture.get("protocol_execution_performed") is not True:
        blockers.append("protocol_execution_not_performed")
    if capture.get("physical_controller_connected") is not False:
        blockers.append("physical_controller_connection_forbidden")
    if capture.get("robot_motion_observed") is not False:
        blockers.append("physical_robot_motion_forbidden")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(capture.get("runtime_image_digest", ""))):
        blockers.append("runtime_image_digest_missing_or_invalid")
    if not _SHA256_RE.fullmatch(str(capture.get("runtime_fingerprint_sha256", ""))):
        blockers.append("runtime_fingerprint_missing_or_invalid")

    outcomes = capture.get("step_outcomes", {})
    if not isinstance(outcomes, Mapping):
        blockers.append("step_outcomes_missing")
    else:
        for step in request.steps:
            if outcomes.get(step) is not True:
                blockers.append(f"protocol_step_failed:{step}")

    timing = capture.get("timing", {})
    if not isinstance(timing, Mapping) or not all(
        _finite(timing.get(key)) for key in ("duration_s", "ticks", "p99_s", "max_s")
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

    accepted = not blockers
    fingerprint = canonical_sha256(dict(capture))
    return URSimCaptureDecision(
        accepted=accepted,
        simulation_run=accepted,
        stage="simulation_run" if accepted else "deterministic_tested",
        blockers=tuple(dict.fromkeys(blockers)),
        capture_fingerprint_sha256=fingerprint,
    )


def execute_with_transport(
    transport: URSimTransport, request: URSimRequest
) -> tuple[Mapping[str, Any], URSimCaptureDecision]:
    capture = transport.execute(request)
    if capture.get("transport_kind") != transport.transport_kind:
        capture = dict(capture)
        capture["transport_kind"] = "transport_kind_mismatch"
    return capture, evaluate_capture(capture, request)
