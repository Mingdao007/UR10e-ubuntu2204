"""Deterministic evidence gates for the conditional PolyScope 5.25.2 upgrade.

This module deliberately has no controller transport.  It evaluates evidence
captured by separate read-only bench tooling and cannot install an update,
start a program, or produce a robot command.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence


TARGET_POLYSCOPE_VERSION = (5, 25, 2)
MINIMUM_DIRECT_UPDATE_VERSION = (5, 5, 0)
REQUIRED_BACKUP_ROLES = frozenset(
    {
        "system_backup",
        "support_file",
        "programs_export",
        "installation_export",
        "safety_export",
        "calibration_export",
        "urcap_inventory",
    }
)
REQUIRED_DIRECT_TORQUE_APIS = frozenset(
    {
        "direct_torque_v2",
        "get_jacobian",
        "get_coriolis_and_centrifugal_torques",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _version(value: str) -> tuple[int, int, int]:
    match = re.search(r"(?:^|\D)(\d+)\.(\d+)(?:\.(\d+))?", str(value))
    if not match:
        raise ValueError(f"cannot parse PolyScope version: {value!r}")
    return tuple(int(part or 0) for part in match.groups())


def _valid_sha256(value: Any) -> bool:
    return bool(_SHA256_RE.fullmatch(str(value)))


def _artifact_hashes(record: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for artifact in record.get("artifacts", ()):
        if not isinstance(artifact, Mapping):
            continue
        role = str(artifact.get("role", ""))
        sha256 = str(artifact.get("sha256", ""))
        if role and _valid_sha256(sha256):
            result[role] = sha256
    return result


@dataclass(frozen=True)
class UpgradeGateDecision:
    stage: str
    accepted: bool
    blockers: tuple[str, ...]
    controller_verified: bool


def evaluate_upgrade_preflight(record: Mapping[str, Any]) -> UpgradeGateDecision:
    """Return whether evidence is sufficient to begin the 5.25.2 install.

    Passing this gate authorizes only the separately controlled installation
    step.  It never establishes true-torque readiness or live authorization.
    """

    blockers: list[str] = []
    try:
        current = _version(str(record.get("current_polyscope_version", "")))
    except ValueError:
        current = (0, 0, 0)
        blockers.append("current_version_missing_or_invalid")
    if current < MINIMUM_DIRECT_UPDATE_VERSION:
        blockers.append("direct_update_path_not_supported")
    if current == TARGET_POLYSCOPE_VERSION:
        blockers.append("target_already_installed_use_post_upgrade_readback")
    if current > TARGET_POLYSCOPE_VERSION:
        blockers.append("target_would_be_a_downgrade")
    if str(record.get("target_polyscope_version", "")) != "5.25.2":
        blockers.append("target_version_not_exactly_5_25_2")
    if record.get("probe_mode") != "read_only":
        blockers.append("preflight_probe_not_read_only")
    if record.get("controller_verified") is not False:
        blockers.append("preflight_must_not_predeclare_controller_verified")
    for key in ("controller_serial", "robot_serial"):
        if not str(record.get(key, "")).strip():
            blockers.append(f"{key}_missing")
    if not _valid_sha256(record.get("tcp_payload_binding_sha256")):
        blockers.append("tcp_payload_binding_missing_or_unhashed")
    if record.get("safety_status") != "NORMAL":
        blockers.append("safety_not_normal")

    artifacts = _artifact_hashes(record)
    missing_roles = sorted(REQUIRED_BACKUP_ROLES - artifacts.keys())
    blockers.extend(f"missing_or_unhashed_{role}" for role in missing_roles)

    urcaps = record.get("urcaps", ())
    if not isinstance(urcaps, Sequence) or isinstance(urcaps, (str, bytes)):
        blockers.append("urcap_inventory_invalid")
    else:
        for urcap in urcaps:
            if not isinstance(urcap, Mapping) or urcap.get("compatibility") != "compatible":
                blockers.append("urcap_incompatible_or_unverified")
                break

    profisafe = record.get("profisafe", {})
    if not isinstance(profisafe, Mapping):
        blockers.append("profisafe_inventory_invalid")
    elif bool(profisafe.get("in_use")) and not bool(
        profisafe.get("breaking_change_reviewed")
    ):
        blockers.append("profisafe_breaking_change_not_reviewed")

    urup = str(record.get("urup_sha256", ""))
    official = str(record.get("official_urup_sha256", ""))
    if not _valid_sha256(urup) or urup != official:
        blockers.append("urup_sha256_missing_or_mismatch")
    if str(record.get("usb_filesystem", "")).upper() != "FAT32":
        blockers.append("upgrade_usb_not_fat32")
    if bool(record.get("program_played")) or bool(record.get("bridge_started")):
        blockers.append("preflight_observed_prohibited_execution")
    if bool(record.get("torque_sent")) or bool(record.get("robot_motion_observed")):
        blockers.append("preflight_observed_torque_or_motion")

    return UpgradeGateDecision(
        stage="upgrade_preflight",
        accepted=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
        controller_verified=False,
    )


def evaluate_post_upgrade_readback(
    record: Mapping[str, Any],
    *,
    preflight_artifact_hashes: Mapping[str, str],
    preflight_identity: Mapping[str, str],
) -> UpgradeGateDecision:
    """Verify post-upgrade identity and preservation without authorizing motion."""

    blockers: list[str] = []
    try:
        installed = _version(str(record.get("polyscope_version", "")))
    except ValueError:
        installed = (0, 0, 0)
        blockers.append("post_upgrade_version_missing_or_invalid")
    if installed != TARGET_POLYSCOPE_VERSION:
        blockers.append("polyscope_not_exactly_5_25_2")
    if record.get("safety_status") != "NORMAL":
        blockers.append("safety_not_normal")
    if not bool(record.get("firmware_update_completed")):
        blockers.append("required_firmware_update_not_completed")
    for key in ("controller_serial", "robot_serial"):
        expected = str(preflight_identity.get(key, ""))
        if not expected or str(record.get(key, "")) != expected:
            blockers.append(f"{key}_identity_mismatch")
    expected_tcp_payload = str(
        preflight_identity.get("tcp_payload_binding_sha256", "")
    )
    if (
        not _valid_sha256(expected_tcp_payload)
        or record.get("tcp_payload_binding_sha256") != expected_tcp_payload
    ):
        blockers.append("tcp_payload_binding_not_preserved")

    preserved = record.get("preserved_artifact_hashes", {})
    if not isinstance(preserved, Mapping):
        blockers.append("preservation_readback_invalid")
    else:
        for role in (
            "installation_export",
            "safety_export",
            "calibration_export",
            "urcap_inventory",
        ):
            expected = preflight_artifact_hashes.get(role)
            actual = preserved.get(role)
            if not _valid_sha256(expected) or actual != expected:
                blockers.append(f"{role}_not_preserved")

    urcaps = record.get("urcaps", ())
    if not isinstance(urcaps, Sequence) or isinstance(urcaps, (str, bytes)):
        blockers.append("post_upgrade_urcap_inventory_invalid")
    elif any(
        not isinstance(urcap, Mapping) or not bool(urcap.get("loaded"))
        for urcap in urcaps
    ):
        blockers.append("urcap_failed_to_load")

    interfaces = record.get("interfaces", {})
    for name in ("dashboard", "rtde", "network"):
        if not isinstance(interfaces, Mapping) or not bool(interfaces.get(name)):
            blockers.append(f"{name}_readback_failed")

    apis = record.get("available_apis", ())
    if not isinstance(apis, Sequence) or isinstance(apis, (str, bytes)):
        blockers.append("api_readback_invalid")
    else:
        missing_apis = sorted(REQUIRED_DIRECT_TORQUE_APIS - set(apis))
        blockers.extend(f"missing_api_{name}" for name in missing_apis)

    if bool(record.get("program_played")) or bool(record.get("bridge_started")):
        blockers.append("post_upgrade_observed_prohibited_execution")
    if bool(record.get("torque_sent")) or bool(record.get("robot_motion_observed")):
        blockers.append("post_upgrade_observed_torque_or_motion")
    if bool(record.get("live_motion_authorized")):
        blockers.append("readback_cannot_imply_live_authorization")

    accepted = not blockers
    return UpgradeGateDecision(
        stage="post_upgrade_readback",
        accepted=accepted,
        blockers=tuple(dict.fromkeys(blockers)),
        controller_verified=accepted,
    )
