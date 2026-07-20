"""Fail-closed evaluation of the PolyScope 5.26 controller/runtime contract.

The evaluator consumes already-captured, hash-bound evidence.  It contains no
network, controller, upload, or motion capability.  A version string on its own
is deliberately insufficient to establish controller verification.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence


CONTRACT_SCHEMA = "ur10e_controller_runtime_contract/v1"
TARGET_POLYSCOPE = (5, 26, 0)
REQUIRED_URCAPS = frozenset({"external_control"})
REQUIRED_CONFIGURATION_ROLES = frozenset(
    {"installation", "safety_configuration", "tcp_payload"}
)
REQUIRED_CALIBRATION_ROLES = frozenset(
    {"robot_calibration", "sensor_calibration", "sensor_to_tcp_transform"}
)
REQUIRED_DIRECT_TORQUE_APIS = frozenset(
    {"direct_torque_v2", "get_jacobian", "get_coriolis_and_centrifugal_torques"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _valid_sha256(value: Any) -> bool:
    return bool(_SHA256_RE.fullmatch(str(value)))


def _version(value: Any) -> tuple[int, int, int]:
    match = re.search(r"(?:^|\D)(\d+)\.(\d+)(?:\.(\d+))?", str(value))
    if not match:
        raise ValueError(f"cannot parse PolyScope version: {value!r}")
    return tuple(int(part or 0) for part in match.groups())


def _artifact_roles(value: Any) -> set[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return set()
    roles: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role", ""))
        if role and _valid_sha256(item.get("sha256")):
            roles.add(role)
    return roles


@dataclass(frozen=True)
class ControllerRuntimeDecision:
    accepted: bool
    controller_verified: bool
    version_observed: bool
    blockers: tuple[str, ...]


def evaluate_controller_runtime_contract(
    record: Mapping[str, Any],
) -> ControllerRuntimeDecision:
    """Evaluate evidence without granting live/contact/model authorization."""

    blockers: list[str] = []
    if record.get("schema") != CONTRACT_SCHEMA:
        blockers.append("runtime_contract_schema_mismatch")

    try:
        installed = _version(record.get("polyscope_version", ""))
        version_observed = installed[:2] == TARGET_POLYSCOPE[:2]
    except ValueError:
        installed = (0, 0, 0)
        version_observed = False
        blockers.append("polyscope_version_missing_or_invalid")
    if installed[:2] != TARGET_POLYSCOPE[:2]:
        blockers.append("polyscope_not_5_26_x")
    if record.get("probe_mode") != "read_only":
        blockers.append("runtime_probe_not_read_only")

    identity = record.get("identity", {})
    if not isinstance(identity, Mapping):
        identity = {}
    for key in ("controller_serial", "robot_serial"):
        if not str(identity.get(key, "")).strip():
            blockers.append(f"{key}_missing")
    if not _valid_sha256(identity.get("evidence_sha256")):
        blockers.append("identity_evidence_missing_or_unhashed")

    urcaps = record.get("urcaps", ())
    found_urcaps: set[str] = set()
    if not isinstance(urcaps, Sequence) or isinstance(urcaps, (str, bytes)):
        blockers.append("urcap_inventory_invalid")
    else:
        for urcap in urcaps:
            if not isinstance(urcap, Mapping):
                blockers.append("urcap_entry_invalid")
                continue
            name = str(urcap.get("name", "")).strip().lower().replace(" ", "_")
            if name:
                found_urcaps.add(name)
            if (
                urcap.get("compatibility") != "compatible"
                or not _valid_sha256(urcap.get("evidence_sha256"))
            ):
                blockers.append("urcap_incompatible_or_unverified")
    blockers.extend(
        f"required_urcap_missing_{role}"
        for role in sorted(REQUIRED_URCAPS - found_urcaps)
    )

    configuration_roles = _artifact_roles(record.get("configuration_artifacts"))
    calibration_roles = _artifact_roles(record.get("calibration_artifacts"))
    blockers.extend(
        f"configuration_missing_or_unhashed_{role}"
        for role in sorted(REQUIRED_CONFIGURATION_ROLES - configuration_roles)
    )
    blockers.extend(
        f"calibration_missing_or_unhashed_{role}"
        for role in sorted(REQUIRED_CALIBRATION_ROLES - calibration_roles)
    )

    runtime = record.get("runtime_readback", {})
    if not isinstance(runtime, Mapping):
        runtime = {}
    if runtime.get("safety_status") != "NORMAL":
        blockers.append("safety_not_normal")
    if runtime.get("program_state") != "STOPPED":
        blockers.append("program_not_stopped")
    for interface in ("dashboard", "rtde", "network"):
        if runtime.get(interface) is not True:
            blockers.append(f"{interface}_readback_failed")
    if not _valid_sha256(runtime.get("evidence_sha256")):
        blockers.append("runtime_readback_missing_or_unhashed")

    api_readback = record.get("direct_torque_api_readback", {})
    if not isinstance(api_readback, Mapping):
        api_readback = {}
    apis = api_readback.get("available_apis", ())
    if not isinstance(apis, Sequence) or isinstance(apis, (str, bytes)):
        apis = ()
        blockers.append("direct_torque_api_readback_invalid")
    blockers.extend(
        f"missing_api_{api}"
        for api in sorted(REQUIRED_DIRECT_TORQUE_APIS - set(apis))
    )
    if not _valid_sha256(api_readback.get("evidence_sha256")):
        blockers.append("direct_torque_api_evidence_missing_or_unhashed")

    actions = record.get("actions_observed", {})
    if not isinstance(actions, Mapping):
        actions = {}
        blockers.append("actions_observed_invalid")
    for action in (
        "program_played",
        "bridge_started",
        "torque_sent",
        "force_torque_zeroed",
        "robot_motion_commanded",
        "controller_setting_written",
    ):
        if actions.get(action) is not False:
            blockers.append(f"prohibited_or_unreported_{action}")

    authorization = record.get("authorization", {})
    if not isinstance(authorization, Mapping):
        authorization = {}
    for key in ("live_motion", "contact", "model_active", "upload"):
        if authorization.get(key) is not False:
            blockers.append(f"runtime_contract_cannot_authorize_{key}")

    if record.get("controller_verified") is not False:
        blockers.append("input_must_not_self_assert_controller_verified")
    accepted = not blockers
    controller_verified = accepted

    return ControllerRuntimeDecision(
        accepted=accepted,
        controller_verified=controller_verified,
        version_observed=version_observed,
        blockers=tuple(dict.fromkeys(blockers)),
    )
