"""Canonical static deployment configuration and fingerprints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .model import (
    APPROVED_I_MULTIPLIERS,
    I_GAIN_MAX,
    I_LOG2_MAX,
    I_LOG2_MIN,
    LOG2_STEP,
    P_D_LOG2_MAX,
    P_D_LOG2_MIN,
    DeploymentSpec,
    ModelError,
    canonical_json_bytes,
)


CONFIG_SCHEMA = "step5d.autotune.deployment/v2"


class ConfigError(RuntimeError):
    """Raised when the static deployment manifest is incomplete or drifts."""


@dataclass(frozen=True)
class StaticConfig:
    root: Path
    path: Path
    payload: Mapping[str, Any]
    deployment: DeploymentSpec
    code_sources: tuple[Path, ...]

    @property
    def database_path(self) -> Path:
        return _safe_runtime_path(self.root, self.payload["paths"]["database"])

    @property
    def mailbox_path(self) -> Path:
        return _safe_runtime_path(self.root, self.payload["paths"]["mailbox"])

    @property
    def heartbeat_path(self) -> Path:
        return _safe_runtime_path(self.root, self.payload["paths"]["heartbeat"])

    @property
    def runtime_root_path(self) -> Path:
        return _safe_runtime_path(self.root, self.payload["paths"]["runtime_root"])


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load canonical config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("canonical config must be a JSON object")
    return value


def _resolve_source(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigError(f"code source escapes experiment root: {relative}") from exc
    if not path.is_file() or path.is_symlink():
        raise ConfigError(f"code source is missing or unsafe: {relative}")
    return path


def _safe_runtime_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigError(f"runtime path escapes experiment root: {relative}") from exc
    return path


def code_fingerprint(
    root: Path,
    sources: tuple[Path, ...],
    *,
    launch_contract: Mapping[str, Any] | None = None,
) -> str:
    rows = []
    for path in sources:
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    material: dict[str, Any] = {"sources": rows}
    if launch_contract is not None:
        material["launch_contract"] = dict(launch_contract)
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def _controller_readback_verified(
    *, root: Path, deployment: Mapping[str, Any], controller: Mapping[str, Any]
) -> bool:
    declared = deployment.get("controller_readback_verified") is True
    attestation_value = deployment.get("readback_attestation")
    if not declared:
        if attestation_value is not None:
            raise ConfigError(
                "unverified deployment must not bind a controller readback attestation"
            )
        return False
    if not isinstance(attestation_value, str):
        raise ConfigError("verified controller readback requires an attestation path")
    attestation_path = _resolve_source(root, attestation_value)
    attestation = _load_object(attestation_path)
    required = {
        "schema",
        "verified",
        "deployment_id",
        "controller_host",
        "program",
        "tp_fingerprint",
        "triplet_sha256",
        "readback_at",
    }
    if set(attestation) != required:
        raise ConfigError("controller readback attestation schema fields differ")
    if (
        attestation.get("schema") != "step5d.autotune.controller-readback/v2"
        or attestation.get("verified") is not True
        or attestation.get("deployment_id") != deployment.get("tp_delivery_id")
        or attestation.get("controller_host") != controller.get("host")
        or attestation.get("program") != controller.get("program")
        or attestation.get("tp_fingerprint") != deployment.get("tp_fingerprint")
    ):
        raise ConfigError("controller readback attestation identity mismatch")
    triplet = attestation.get("triplet_sha256")
    if not isinstance(triplet, dict) or set(triplet) != {".script", ".txt", ".urp"}:
        raise ConfigError("controller readback attestation lacks the exact TP triplet")
    if any(
        not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
        for value in triplet.values()
    ):
        raise ConfigError("controller readback triplet contains an invalid SHA-256")
    try:
        datetime.fromisoformat(str(attestation.get("readback_at")))
    except ValueError as exc:
        raise ConfigError("controller readback timestamp is invalid") from exc
    return True


def _validate_golden_replay_attestation(root: Path, value: object) -> None:
    if not isinstance(value, str):
        raise ConfigError("live cutover requires a golden replay attestation path")
    attestation = _load_object(_resolve_source(root, value))
    required = {
        "schema",
        "passed",
        "golden_spec_sha256",
        "group_id",
        "trial_uid",
        "packet_rows",
        "tp_transitions",
        "bundle_sha256",
        "csv_sha256",
        "control_sources_byte_identical",
        "bridge_frozen_v1_sha256",
        "bridge_v2_sha256",
        "bridge_adapter_patch_sha256",
    }
    if set(attestation) != required:
        raise ConfigError("golden replay attestation schema fields differ")
    golden_spec = root / "config" / "step5" / "golden_replay_g10_v1.json"
    spec_sha = hashlib.sha256(golden_spec.read_bytes()).hexdigest()
    if (
        attestation.get("schema") != "step5d.autotune.golden-replay-result/v2"
        or attestation.get("passed") is not True
        or attestation.get("group_id") != "G10"
        or attestation.get("golden_spec_sha256") != spec_sha
        or not isinstance(attestation.get("packet_rows"), int)
        or attestation["packet_rows"] <= 0
        or not isinstance(attestation.get("tp_transitions"), list)
        or not attestation["tp_transitions"]
        or not isinstance(attestation.get("control_sources_byte_identical"), list)
        or not attestation["control_sources_byte_identical"]
    ):
        raise ConfigError("golden replay attestation identity or result differs")
    for name in (
        "trial_uid",
        "golden_spec_sha256",
        "bundle_sha256",
        "csv_sha256",
        "bridge_frozen_v1_sha256",
        "bridge_v2_sha256",
        "bridge_adapter_patch_sha256",
    ):
        digest = attestation.get(name)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ConfigError(f"golden replay attestation has invalid {name}")


def load_static_config(root: Path, path: Path | None = None) -> StaticConfig:
    root = root.resolve()
    path = path or root / "config" / "step5" / "current.json"
    payload = _load_object(path)
    if payload.get("schema") != CONFIG_SCHEMA:
        raise ConfigError("unknown Step5 deployment config schema")
    allowed = {
        "schema",
        "deployment",
        "controller",
        "execution_profile",
        "control_contract",
        "search_envelope",
        "code_sources",
        "bridge",
        "postprocess",
        "paths",
        "live_cutover",
    }
    unknown = set(payload).difference(allowed)
    if unknown:
        raise ConfigError(f"canonical config has unknown top-level fields: {sorted(unknown)}")
    envelope = payload.get("search_envelope")
    expected_envelope = {
        "p_d_log2_octaves": [str(P_D_LOG2_MIN), str(P_D_LOG2_MAX)],
        "i_log2_octaves": [str(I_LOG2_MIN), str(I_LOG2_MAX)],
        "log2_step": str(LOG2_STEP),
        "i_gain_max": str(I_GAIN_MAX),
        "coarse_i_multipliers": sorted(
            (str(value) for value in APPROVED_I_MULTIPLIERS), key=lambda value: int(value)
        ),
    }
    if envelope != expected_envelope:
        raise ConfigError("search envelope differs from the executable model contract")
    relative_sources = payload.get("code_sources")
    if not isinstance(relative_sources, list) or not relative_sources:
        raise ConfigError("canonical config requires code_sources")
    if any(not isinstance(item, str) or item.endswith(".md") for item in relative_sources):
        raise ConfigError("code fingerprint sources must be code/config files, never Markdown")
    sources = tuple(_resolve_source(root, value) for value in relative_sources)
    bridge = payload.get("bridge")
    if not isinstance(bridge, dict) or set(bridge) != {
        "live_enabled",
        "argv",
        "environment",
        "analyzer_argv",
        "ready_timeout_s",
        "trial_timeout_s",
        "startup_stable_s",
        "operator_play_timeout_s",
    }:
        raise ConfigError("bridge launch contract is missing")
    if type(bridge["live_enabled"]) is not bool:
        raise ConfigError("bridge live_enabled must be a boolean")
    for name in (
        "ready_timeout_s",
        "trial_timeout_s",
        "startup_stable_s",
        "operator_play_timeout_s",
    ):
        if (
            isinstance(bridge[name], bool)
            or not isinstance(bridge[name], (int, float))
            or float(bridge[name]) <= 0
        ):
            raise ConfigError(f"bridge {name} must be positive")
    for name in ("argv", "analyzer_argv"):
        if not isinstance(bridge.get(name), list) or any(
            not isinstance(token, str) or not token for token in bridge[name]
        ):
            raise ConfigError(f"bridge {name} must be a token array")
    if not isinstance(bridge.get("environment"), dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in bridge["environment"].items()
    ):
        raise ConfigError("bridge environment must contain string bindings")
    postprocess = payload.get("postprocess")
    if (
        not isinstance(postprocess, dict)
        or set(postprocess) != {"mac_destination", "transfer_timeout_s"}
        or not isinstance(postprocess.get("mac_destination"), str)
        or ":" not in postprocess["mac_destination"]
        or not isinstance(postprocess.get("transfer_timeout_s"), (int, float))
        or isinstance(postprocess.get("transfer_timeout_s"), bool)
        or not 1 <= float(postprocess["transfer_timeout_s"]) <= 120
    ):
        raise ConfigError("postprocess transfer contract is invalid")
    actual_code = code_fingerprint(
        root,
        sources,
        launch_contract={
            "argv": bridge["argv"],
            "environment": bridge["environment"],
            "analyzer_argv": bridge["analyzer_argv"],
        },
    )
    control_contract = payload.get("control_contract")
    if not isinstance(control_contract, dict):
        raise ConfigError("control_contract block is missing")
    if set(control_contract) != {
        "force_math_changed",
        "trajectory_changed",
        "command_order_changed",
        "guard_policy_changed",
        "guard_sources",
    } or any(
        control_contract[name] is not False
        for name in (
            "force_math_changed",
            "trajectory_changed",
            "command_order_changed",
        )
    ):
        raise ConfigError("control refactor must preserve math, trajectory, and order")
    if control_contract["guard_policy_changed"] is not True:
        raise ConfigError("r15 requires the post-RNN guard policy change declaration")
    guard_rows = control_contract.get("guard_sources")
    if not isinstance(guard_rows, list) or not guard_rows:
        raise ConfigError("control_contract requires guard_sources")
    guard_sources = tuple(_resolve_source(root, value) for value in guard_rows)
    actual_guard = code_fingerprint(root, guard_sources)
    deployment_payload = payload.get("deployment")
    if not isinstance(deployment_payload, dict):
        raise ConfigError("deployment block is missing")
    if set(deployment_payload) != {
        "id",
        "tp_delivery_id",
        "code_fingerprint",
        "tp_fingerprint",
        "guard_fingerprint",
        "authorized",
        "controller_readback_verified",
        "readback_attestation",
    }:
        raise ConfigError("deployment identity fields differ from the v2 live schema")
    controller = payload.get("controller")
    if not isinstance(controller, dict) or set(controller) != {
        "host",
        "required_ports",
        "program",
        "waypoint_policy",
    }:
        raise ConfigError("controller identity block is missing")
    if (
        not isinstance(controller["host"], str)
        or not controller["host"]
        or not isinstance(controller["program"], str)
        or not controller["program"]
        or controller["waypoint_policy"] != "fetch_current_triplet_before_build"
        or not isinstance(controller["required_ports"], list)
        or controller["required_ports"] != [29999, 30004]
        or any(
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
            for port in controller["required_ports"]
        )
    ):
        raise ConfigError("controller endpoint or waypoint policy is invalid")
    readback_verified = _controller_readback_verified(
        root=root, deployment=deployment_payload, controller=controller
    )
    declared_code = deployment_payload.get("code_fingerprint")
    if declared_code not in {None, "auto", actual_code}:
        raise ConfigError("declared code fingerprint differs from canonical source files")
    declared_guard = deployment_payload.get("guard_fingerprint")
    if declared_guard not in {None, "auto", actual_guard}:
        raise ConfigError("declared guard fingerprint differs from canonical guard sources")
    try:
        deployment = DeploymentSpec(
            deployment_id=str(deployment_payload.get("id", "")),
            code_fingerprint=actual_code,
            tp_fingerprint=str(deployment_payload.get("tp_fingerprint", "")),
            guard_fingerprint=actual_guard,
            profile=payload.get("execution_profile") or {},
            deployment_authorized=deployment_payload.get("authorized") is True,
            controller_readback_verified=readback_verified,
        )
    except ModelError as exc:
        raise ConfigError(f"invalid deployment identity: {exc}") from exc
    paths = payload.get("paths")
    if not isinstance(paths, dict) or set(paths) != {
        "database",
        "mailbox",
        "heartbeat",
        "runtime_root",
    }:
        raise ConfigError("paths must bind database, mailbox, heartbeat, and runtime_root")
    for name, relative in paths.items():
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ConfigError(f"{name} must be a safe path relative to the experiment root")
        _safe_runtime_path(root, relative)
    cutover = payload.get("live_cutover")
    if (
        not isinstance(cutover, dict)
        or set(cutover)
        != {
            "enabled",
            "status_freshness_s",
            "golden_replay_attestation",
            "blocked_until",
        }
        or type(cutover["enabled"]) is not bool
        or isinstance(cutover["status_freshness_s"], bool)
        or not isinstance(cutover["status_freshness_s"], (int, float))
        or not 2 <= float(cutover["status_freshness_s"]) <= 30
        or not isinstance(cutover["blocked_until"], list)
        or any(not isinstance(value, str) or not value for value in cutover["blocked_until"])
    ):
        raise ConfigError("live cutover contract is invalid")
    _validate_golden_replay_attestation(root, cutover["golden_replay_attestation"])
    return StaticConfig(root, path.resolve(), payload, deployment, sources)
