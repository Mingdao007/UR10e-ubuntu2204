"""Strict, offline-only contracts for the lightweight Remote transport.

This module validates bytes and identities only.  It has no ROS client,
network, subprocess, controller-manager, or robot-driver imports.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_contract import ForceCandidate
from step5d_autotune_v3.profile import (
    ContractViolation,
    canonical_json_bytes,
    load_contract,
)
from step5d_autotune_v3.release_identity import ROLLING_PROTOCOL
from step5d_autotune_v3.runtime_profile import (
    LaunchProfile,
    load_launch_profile,
    normalize_trial_overlay,
    normalized_overlay_sha256,
)
from ur10e_experiment_runtime.candidate_identity import (
    ControlCandidateUid,
    OccurrenceUid,
    ParameterUid,
    TransportCandidateUid,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RELEASE_PATH = (
    EXPERIMENT_ROOT / "config/step5d_remote/remote_prep_v1.json"
)
RELEASE_SCHEMA = "step5d.remote-control/release-v1"
RELEASE_STAGE_ID = "step5d_strict_rnn_autotune_v3_remote_prep_v1"
TRANSPORT_ID = "ros2_control_watchdog_velocity_v1"
CONTROL_MODE = "strict_rnn_only"
TRIAL_SOURCE_SCHEMA = "step5d.remote-control/trial-source-v1"
TRIAL_SCHEMA = "step5d.remote-control/prepared-trial-v1"
TRIAL_BUNDLE_SCHEMA = "step5d.remote-control/prepared-trial-bundle-v1"
RECEIPT_SCHEMA = "step5d.remote-control/trial-receipt-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_JOINT_NAMES = (
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
)
REQUIRED_SOURCE_PATHS = frozenset(
    {
        "experiments/tase-contact-reproduction/STEP5_FLOW.md",
        "experiments/tase-contact-reproduction/config/step5/step5d_autotune_v3_control_contract.json",
        "experiments/tase-contact-reproduction/config/step5/step5d_autotune_v3_launch_profile.json",
        "experiments/tase-contact-reproduction/config/step5_stage_table.json",
        "experiments/tase-contact-reproduction/config/step5d_liveprep_solver_gate.json",
        "experiments/tase-contact-reproduction/tools/run_step5d_remote_control.py",
        "experiments/tase-contact-reproduction/tools/step5c_strict_rnn.py",
        "experiments/tase-contact-reproduction/tools/step5d_autotune_contract.py",
        "experiments/tase-contact-reproduction/tools/step5d_control_contract.py",
        "experiments/tase-contact-reproduction/tools/step5d_remote_control/__init__.py",
        "experiments/tase-contact-reproduction/tools/step5d_remote_control/contracts.py",
        "experiments/tase-contact-reproduction/tools/step5d_remote_control/runtime.py",
        "experiments/tase-contact-reproduction/tools/step5d_remote_control/seam.py",
        "experiments/tase-contact-reproduction/tools/step5d_autotune_v3/profile.py",
        "experiments/tase-contact-reproduction/tools/step5d_autotune_v3/release_identity.py",
        "experiments/tase-contact-reproduction/tools/step5d_autotune_v3/runtime_calibration.py",
        "experiments/tase-contact-reproduction/tools/step5d_autotune_v3/runtime_profile.py",
        "src/ur10e_experiment_runtime/ur10e_experiment_runtime/candidate_identity.py",
        "src/ur10e_step5d_remote_watchdog/CMakeLists.txt",
        "src/ur10e_step5d_remote_watchdog/include/ur10e_step5d_remote_watchdog/watchdog_controller.hpp",
        "src/ur10e_step5d_remote_watchdog/include/ur10e_step5d_remote_watchdog/watchdog_gate.hpp",
        "src/ur10e_step5d_remote_watchdog/package.xml",
        "src/ur10e_step5d_remote_watchdog/src/watchdog_controller.cpp",
        "src/ur10e_step5d_remote_watchdog/src/watchdog_gate.cpp",
        "src/ur10e_step5d_remote_watchdog/watchdog_plugin.xml",
    }
)


class RemoteControlError(RuntimeError):
    """Remote preparation contract failed closed."""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reject_constant(value: str) -> None:
    raise RemoteControlError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RemoteControlError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def load_json_object(path: Path, *, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RemoteControlError(f"{role} must be a real regular file: {path}")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteControlError(f"cannot load {role}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RemoteControlError(f"{role} must be a JSON object")
    try:
        canonical_json_bytes(payload)
    except (TypeError, ValueError) as exc:
        raise RemoteControlError(f"{role} must contain finite JSON") from exc
    return payload


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Publish one canonical JSON document without a partially visible file."""

    encoded = canonical_json_bytes(dict(payload)) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _exact_object(name: str, value: Any, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RemoteControlError(f"{name} must be an object")
    if set(value) != keys:
        raise RemoteControlError(
            f"{name} fields differ; missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )
    return value


def _strict_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RemoteControlError(f"{name} must be a non-empty string without NUL")
    return value


def _strict_sha(name: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RemoteControlError(f"{name} must be a lowercase SHA-256")
    return value


def _strict_int(name: str, value: Any, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise RemoteControlError(f"{name} must be an integer >= {minimum}")
    return value


def _safe_relative(name: str, value: Any) -> Path:
    text = _strict_string(name, value)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise RemoteControlError(f"{name} must be a safe relative path")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _flag_rows(contract: Mapping[str, Any]) -> dict[str, list[str]]:
    return {str(row[0]): list(row) for row in contract["cli_arguments"]}


def _flag_value(rows: Mapping[str, list[str]], flag: str) -> str:
    row = rows.get(flag)
    if row is None or len(row) != 2:
        raise RemoteControlError(f"V3 contract must provide exactly one value for {flag}")
    value = row[1]
    if value.startswith("$"):
        raise RemoteControlError(f"V3 contract value for {flag} is unresolved")
    return value


def _positive_float(rows: Mapping[str, list[str]], flag: str) -> float:
    try:
        value = float(_flag_value(rows, flag))
    except ValueError as exc:
        raise RemoteControlError(f"V3 contract value for {flag} is not numeric") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise RemoteControlError(f"V3 contract value for {flag} must be positive")
    return value


@dataclass(frozen=True)
class RemoteRelease:
    path: Path
    experiment_root: Path
    document: Mapping[str, Any]
    release_sha256: str
    contract: Mapping[str, Any]
    launch_profile: LaunchProfile
    control_parameters: Mapping[str, Any]

    @property
    def transport_id(self) -> str:
        return str(self.document["transport_id"])

    @property
    def live_authorized(self) -> bool:
        return bool(self.document["live_authorized"])


def _derive_control_parameters(
    contract: Mapping[str, Any], *, stale_cycles: int
) -> dict[str, Any]:
    rows = _flag_rows(contract)
    update_rate_hz = _positive_float(rows, "--rtde-hz")
    qdot_limit = _positive_float(rows, "--step5d-qdot-limit-rad-s")
    host_slew = _positive_float(rows, "--step5d-autotune-host-slew-rad-s2")
    actuator_acceleration = _positive_float(
        rows, "--step5d-autotune-speedj-acceleration-rad-s2"
    )
    try:
        inner_iterations = int(_flag_value(rows, "--step5d-rnn-inner-iterations"))
    except ValueError as exc:
        raise RemoteControlError("V3 RNN inner iterations are not an integer") from exc
    if inner_iterations < 1:
        raise RemoteControlError("V3 RNN inner iterations must be positive")
    backend = _flag_value(rows, "--step5d-rnn-backend")
    if backend not in {"numpy", "cupy"}:
        raise RemoteControlError("V3 RNN backend is unsupported")
    parameters = {
        "update_rate_hz": update_rate_hz,
        "qdot_limit_rad_s": qdot_limit,
        "max_acceleration_rad_s2": min(host_slew, actuator_acceleration),
        "host_slew_rad_s2": host_slew,
        "actuator_acceleration_rad_s2": actuator_acceleration,
        "watchdog_stale_timeout_s": stale_cycles / update_rate_hz,
        "rnn_epsilon": _positive_float(rows, "--step5d-epsilon"),
        "rnn_sigr_exponent_r": _positive_float(
            rows, "--step5d-sigr-exponent-r"
        ),
        "rnn_inner_iterations": inner_iterations,
        "rnn_backend": backend,
    }
    if not update_rate_hz.is_integer():
        raise RemoteControlError("V3 update rate must be an integer frequency")
    if not 0.0 < float(parameters["rnn_sigr_exponent_r"]) <= 1.0:
        raise RemoteControlError("V3 RNN exponent must be in (0, 1]")

    effective = contract["effective_fields"]
    expected_bindings = {
        "update_rate_hz": effective["control_invariant"]["rtde_hz"],
        "qdot_limit_rad_s": effective["safety_invariant"][
            "step5d_qdot_limit_rad_s"
        ],
        "host_slew_rad_s2": effective["safety_invariant"][
            "step5d_autotune_host_slew_rad_s2"
        ],
        "actuator_acceleration_rad_s2": effective["safety_invariant"][
            "step5d_autotune_speedj_acceleration_rad_s2"
        ],
        "rnn_epsilon": effective["control_invariant"]["step5d_epsilon"],
        "rnn_sigr_exponent_r": effective["control_invariant"][
            "step5d_sigr_exponent_r"
        ],
        "rnn_inner_iterations": effective["control_invariant"][
            "step5d_rnn_inner_iterations"
        ],
        "rnn_backend": effective["control_invariant"]["step5d_rnn_backend"],
    }
    for field, expected in expected_bindings.items():
        observed = parameters[field]
        if isinstance(observed, float) and isinstance(expected, (int, float)):
            matches = math.isclose(observed, float(expected), rel_tol=0.0, abs_tol=0.0)
        else:
            matches = observed == expected
        if not matches:
            raise RemoteControlError(
                f"V3 CLI/effective-field binding differs for {field}"
            )
    return parameters


def load_remote_release(
    path: Path = DEFAULT_RELEASE_PATH,
    *,
    experiment_root: Path = EXPERIMENT_ROOT,
) -> RemoteRelease:
    root = experiment_root.resolve()
    repository_root = root.parents[1]
    document = load_json_object(path, role="Remote release")
    required = {
        "schema",
        "stage_id",
        "transport_id",
        "control_mode",
        "base_commit",
        "live_authorized",
        "inputs",
        "transport_invariants",
        "source_sha256",
    }
    _exact_object("Remote release", document, required)
    expected = {
        "schema": RELEASE_SCHEMA,
        "stage_id": RELEASE_STAGE_ID,
        "transport_id": TRANSPORT_ID,
        "control_mode": CONTROL_MODE,
        "live_authorized": False,
    }
    for field, value in expected.items():
        if document[field] != value:
            raise RemoteControlError(f"Remote release {field} differs")
    if not isinstance(document["base_commit"], str) or _GIT_SHA1.fullmatch(
        document["base_commit"]
    ) is None:
        raise RemoteControlError("Remote release base_commit must be a Git SHA-1")

    inputs = _exact_object(
        "Remote release inputs",
        document["inputs"],
        {"control_contract", "launch_profile", "solver_gate"},
    )
    input_paths = {
        role: root / _safe_relative(f"inputs.{role}", relative)
        for role, relative in inputs.items()
    }
    for role, input_path in input_paths.items():
        if input_path.is_symlink() or not input_path.is_file():
            raise RemoteControlError(f"Remote input {role} is missing or symlinked")

    invariants = _exact_object(
        "Remote transport invariants",
        document["transport_invariants"],
        {
            "controller_plugin",
            "joint_names",
            "command_topic",
            "status_topic",
            "stale_cycles",
            "invalid_command_policy",
            "controller_switching_allowed",
        },
    )
    if invariants["controller_plugin"] != (
        "ur10e_step5d_remote_watchdog/WatchdogController"
    ):
        raise RemoteControlError("Remote controller plugin identity differs")
    if tuple(invariants["joint_names"]) != _JOINT_NAMES:
        raise RemoteControlError("Remote joint order differs from the UR10e contract")
    if invariants["command_topic"] != "~/commands" or invariants["status_topic"] != "~/status":
        raise RemoteControlError("Remote private topic contract differs")
    stale_cycles = _strict_int("stale_cycles", invariants["stale_cycles"], minimum=1)
    if invariants["invalid_command_policy"] != "latch_zero_until_lifecycle_reset":
        raise RemoteControlError("Remote invalid-command policy differs")
    if invariants["controller_switching_allowed"] is not False:
        raise RemoteControlError("Remote release must forbid controller switching")

    sources = document["source_sha256"]
    if not isinstance(sources, dict) or not sources:
        raise RemoteControlError("Remote source_sha256 must be a non-empty object")
    if set(sources) != REQUIRED_SOURCE_PATHS:
        raise RemoteControlError(
            "Remote governed source set differs; "
            f"missing={sorted(REQUIRED_SOURCE_PATHS - set(sources))}, "
            f"extra={sorted(set(sources) - REQUIRED_SOURCE_PATHS)}"
        )
    observed_sources: set[str] = set()
    for relative, expected_digest in sources.items():
        source_relative = _safe_relative("source_sha256 path", relative)
        expected = _strict_sha(f"source_sha256.{relative}", expected_digest)
        source = repository_root / source_relative
        if source.is_symlink() or not source.is_file():
            raise RemoteControlError(f"Remote governed source is missing: {relative}")
        observed = _file_sha256(source)
        if observed != expected:
            raise RemoteControlError(
                f"Remote governed source drifted: {relative} "
                f"expected={expected} observed={observed}"
            )
        observed_sources.add(str(source_relative))
    missing_inputs = {
        str(path.relative_to(repository_root)) for path in input_paths.values()
    } - observed_sources
    if missing_inputs:
        raise RemoteControlError(
            f"Remote input files are not source-bound: {sorted(missing_inputs)}"
        )

    try:
        contract = load_contract(input_paths["control_contract"])
        launch_profile = load_launch_profile(
            input_paths["launch_profile"], contract=contract
        )
    except ContractViolation as exc:
        raise RemoteControlError(f"inherited V3 contract is invalid: {exc}") from exc
    solver_gate = load_json_object(input_paths["solver_gate"], role="RNN solver gate")
    if solver_gate.get("strict_rnn_enabled") is not True:
        raise RemoteControlError("inherited RNN solver gate is not enabled")
    parameters = _derive_control_parameters(contract, stale_cycles=stale_cycles)
    return RemoteRelease(
        path=path.resolve(),
        experiment_root=root,
        document=document,
        release_sha256=canonical_sha256(document),
        contract=contract,
        launch_profile=launch_profile,
        control_parameters=parameters,
    )


def materialize_controller_config(release: RemoteRelease) -> dict[str, Any]:
    """Return a ROS parameter document derived only from the bound V3 release."""

    parameters = release.control_parameters
    invariants = release.document["transport_invariants"]
    return {
        "controller_manager": {
            "ros__parameters": {
                "update_rate": int(round(float(parameters["update_rate_hz"]))),
                "remote_watchdog": {"type": invariants["controller_plugin"]},
            }
        },
        "remote_watchdog": {
            "ros__parameters": {
                "joints": list(invariants["joint_names"]),
                "max_abs_velocity_rad_s": parameters["qdot_limit_rad_s"],
                "max_acceleration_rad_s2": parameters["max_acceleration_rad_s2"],
                "stale_timeout_s": parameters["watchdog_stale_timeout_s"],
            }
        },
    }


@dataclass(frozen=True)
class PreparedControlTrial:
    campaign_id: str
    campaign_fingerprint: str
    trial_uid: str
    source_protocol: str
    logical_batch_sequence: int
    plan_revision: int
    occurrence_uid: str
    transport_candidate_uid: str
    batch_row_index: int
    selection_role: str
    replicate_ordinal: int
    profile_id: str
    plant_epoch: int
    control_candidate_uid: str
    trial_overlay: Mapping[str, Any]
    trial_overlay_sha256: str
    release_sha256: str
    transport_id: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema": TRIAL_SCHEMA,
            "campaign_id": self.campaign_id,
            "campaign_fingerprint": self.campaign_fingerprint,
            "trial_uid": self.trial_uid,
            "source_protocol": self.source_protocol,
            "logical_batch_sequence": self.logical_batch_sequence,
            "plan_revision": self.plan_revision,
            "occurrence_uid": self.occurrence_uid,
            "transport_candidate_uid": self.transport_candidate_uid,
            "batch_row_index": self.batch_row_index,
            "selection_role": self.selection_role,
            "replicate_ordinal": self.replicate_ordinal,
            "profile_id": self.profile_id,
            "plant_epoch": self.plant_epoch,
            "control_candidate_uid": self.control_candidate_uid,
            "trial_overlay": dict(self.trial_overlay),
            "trial_overlay_sha256": self.trial_overlay_sha256,
            "release_sha256": self.release_sha256,
            "transport_id": self.transport_id,
        }

    @property
    def envelope_sha256(self) -> str:
        return canonical_sha256(self.payload())

    def bundle(self) -> dict[str, Any]:
        return {
            "schema": TRIAL_BUNDLE_SCHEMA,
            "envelope": self.payload(),
            "envelope_sha256": self.envelope_sha256,
        }


def prepare_control_trial(
    source: Mapping[str, Any], *, release: RemoteRelease
) -> PreparedControlTrial:
    row = _exact_object(
        "Remote trial source",
        source,
        {
            "schema",
            "campaign_id",
            "campaign_fingerprint",
            "trial_uid",
            "source_protocol",
            "logical_batch_sequence",
            "plan_revision",
            "occurrence_uid",
            "transport_candidate_uid",
            "batch_row_index",
            "selection_role",
            "replicate_ordinal",
            "profile_id",
            "plant_epoch",
            "trial_overlay",
        },
    )
    if row["schema"] != TRIAL_SOURCE_SCHEMA:
        raise RemoteControlError("Remote trial source schema differs")
    campaign_id = _strict_string("campaign_id", row["campaign_id"])
    campaign_fingerprint = _strict_sha(
        "campaign_fingerprint", row["campaign_fingerprint"]
    )
    trial_uid = _strict_sha("trial_uid", row["trial_uid"])
    source_protocol = _strict_string("source_protocol", row["source_protocol"])
    if source_protocol != ROLLING_PROTOCOL:
        raise RemoteControlError("Remote source protocol differs from active V3")
    logical_batch_sequence = _strict_int(
        "logical_batch_sequence", row["logical_batch_sequence"], minimum=1
    )
    plan_revision = _strict_int("plan_revision", row["plan_revision"], minimum=1)
    batch_row_index = _strict_int(
        "batch_row_index", row["batch_row_index"], minimum=1
    )
    selection_role = _strict_string("selection_role", row["selection_role"])
    replicate_ordinal = _strict_int(
        "replicate_ordinal", row["replicate_ordinal"], minimum=1
    )
    profile_id = _strict_string("profile_id", row["profile_id"])
    plant_epoch = _strict_int("plant_epoch", row["plant_epoch"], minimum=1)
    try:
        normalized = normalize_trial_overlay(
            row["trial_overlay"], profile=release.launch_profile
        )
        overlay_sha = normalized_overlay_sha256(
            release.launch_profile, normalized
        )
    except ContractViolation as exc:
        raise RemoteControlError(f"Remote trial overlay is invalid: {exc}") from exc
    if profile_id != normalized["execution_profile_id"]:
        raise RemoteControlError("Remote trial profile_id differs from its overlay")
    try:
        control_uid = ControlCandidateUid.parse(normalized["control_candidate_uid"])
        occurrence_uid = OccurrenceUid.parse(row["occurrence_uid"])
        expected_occurrence_uid = OccurrenceUid.from_control(
            control_uid,
            protocol=source_protocol,
            logical_batch_sequence=logical_batch_sequence,
            row_index=batch_row_index,
            plan_revision=plan_revision,
            selection_role=selection_role,
            replicate_ordinal=replicate_ordinal,
        )
        candidate = ForceCandidate(
            force_p_gain=normalized["force_p_gain"],
            force_i_gain=normalized["force_i_gain"],
            force_damping=normalized["force_damping"],
        )
        transport_candidate_uid = TransportCandidateUid.parse(
            row["transport_candidate_uid"]
        )
        expected_transport_uid = TransportCandidateUid.from_occurrence(
            expected_occurrence_uid,
            parameter_uid=ParameterUid.from_candidate_digest(candidate.candidate_uid),
            protocol=source_protocol,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RemoteControlError(f"Remote trial identity chain is invalid: {exc}") from exc
    if occurrence_uid != expected_occurrence_uid:
        raise RemoteControlError("Remote occurrence UID differs from its r009 materials")
    if transport_candidate_uid != expected_transport_uid:
        raise RemoteControlError(
            "Remote source transport UID differs from its r009 materials"
        )
    return PreparedControlTrial(
        campaign_id=campaign_id,
        campaign_fingerprint=campaign_fingerprint,
        trial_uid=trial_uid,
        source_protocol=source_protocol,
        logical_batch_sequence=logical_batch_sequence,
        plan_revision=plan_revision,
        occurrence_uid=str(occurrence_uid),
        transport_candidate_uid=str(transport_candidate_uid),
        batch_row_index=batch_row_index,
        selection_role=selection_role,
        replicate_ordinal=replicate_ordinal,
        profile_id=profile_id,
        plant_epoch=plant_epoch,
        control_candidate_uid=str(normalized["control_candidate_uid"]),
        trial_overlay=normalized,
        trial_overlay_sha256=overlay_sha,
        release_sha256=release.release_sha256,
        transport_id=release.transport_id,
    )


def load_prepared_control_trial(
    bundle: Mapping[str, Any], *, release: RemoteRelease
) -> PreparedControlTrial:
    wrapper = _exact_object(
        "Remote prepared trial bundle",
        bundle,
        {"schema", "envelope", "envelope_sha256"},
    )
    if wrapper["schema"] != TRIAL_BUNDLE_SCHEMA:
        raise RemoteControlError("Remote prepared trial bundle schema differs")
    envelope = _exact_object(
        "Remote prepared trial",
        wrapper["envelope"],
        {
            "schema",
            "campaign_id",
            "campaign_fingerprint",
            "trial_uid",
            "source_protocol",
            "logical_batch_sequence",
            "plan_revision",
            "occurrence_uid",
            "transport_candidate_uid",
            "batch_row_index",
            "selection_role",
            "replicate_ordinal",
            "profile_id",
            "plant_epoch",
            "control_candidate_uid",
            "trial_overlay",
            "trial_overlay_sha256",
            "release_sha256",
            "transport_id",
        },
    )
    if envelope["schema"] != TRIAL_SCHEMA:
        raise RemoteControlError("Remote prepared trial schema differs")
    expected_envelope_sha = canonical_sha256(envelope)
    if _strict_sha("envelope_sha256", wrapper["envelope_sha256"]) != expected_envelope_sha:
        raise RemoteControlError("Remote prepared trial envelope digest differs")
    source = {
        "schema": TRIAL_SOURCE_SCHEMA,
        "campaign_id": envelope["campaign_id"],
        "campaign_fingerprint": envelope["campaign_fingerprint"],
        "trial_uid": envelope["trial_uid"],
        "source_protocol": envelope["source_protocol"],
        "logical_batch_sequence": envelope["logical_batch_sequence"],
        "plan_revision": envelope["plan_revision"],
        "occurrence_uid": envelope["occurrence_uid"],
        "transport_candidate_uid": envelope["transport_candidate_uid"],
        "batch_row_index": envelope["batch_row_index"],
        "selection_role": envelope["selection_role"],
        "replicate_ordinal": envelope["replicate_ordinal"],
        "profile_id": envelope["profile_id"],
        "plant_epoch": envelope["plant_epoch"],
        "trial_overlay": envelope["trial_overlay"],
    }
    prepared = prepare_control_trial(source, release=release)
    if prepared.payload() != dict(envelope):
        raise RemoteControlError("Remote prepared trial does not reproduce exactly")
    return prepared


@dataclass(frozen=True)
class RemoteTrialReceipt:
    trial_uid: str
    occurrence_uid: str
    transport_candidate_uid: str
    envelope_sha256: str
    release_sha256: str
    transport_id: str
    execution_mode: str
    outcome: str
    artifacts_sha256: Mapping[str, str]
    command_zero_confirmed: bool
    controller_inactive_confirmed: bool
    watchdog_status: str
    optimizer_eligible: bool


def validate_receipt(
    payload: Mapping[str, Any],
    *,
    prepared: PreparedControlTrial,
    release: RemoteRelease,
) -> RemoteTrialReceipt:
    row = _exact_object(
        "Remote trial receipt",
        payload,
        {
            "schema",
            "trial_uid",
            "occurrence_uid",
            "transport_candidate_uid",
            "envelope_sha256",
            "release_sha256",
            "transport_id",
            "execution_mode",
            "outcome",
            "artifacts_sha256",
            "safe_closure",
            "optimizer_eligible",
        },
    )
    if row["schema"] != RECEIPT_SCHEMA:
        raise RemoteControlError("Remote receipt schema differs")
    closure = _exact_object(
        "Remote safe closure",
        row["safe_closure"],
        {"command_zero_confirmed", "controller_inactive_confirmed", "watchdog_status"},
    )
    if closure["command_zero_confirmed"] is not True:
        raise RemoteControlError("Remote receipt lacks an exact-zero closure")
    if closure["controller_inactive_confirmed"] is not True:
        raise RemoteControlError("Remote receipt lacks an inactive-controller closure")
    watchdog_status = _strict_string("watchdog_status", closure["watchdog_status"])
    if watchdog_status not in {"waiting_zero", "latched_zero", "inactive"}:
        raise RemoteControlError("Remote receipt watchdog status is not a safe closure")
    artifacts = row["artifacts_sha256"]
    if not isinstance(artifacts, dict):
        raise RemoteControlError("Remote receipt artifacts_sha256 must be an object")
    checked_artifacts: dict[str, str] = {}
    for role, digest in artifacts.items():
        checked_artifacts[_strict_string("artifact role", role)] = _strict_sha(
            f"artifact {role}", digest
        )
    execution_mode = _strict_string("execution_mode", row["execution_mode"])
    if execution_mode not in {"offline_replay", "live"}:
        raise RemoteControlError("Remote receipt execution_mode is unsupported")
    optimizer_eligible = row["optimizer_eligible"]
    if not isinstance(optimizer_eligible, bool):
        raise RemoteControlError("Remote receipt optimizer_eligible must be boolean")
    if execution_mode == "offline_replay" and optimizer_eligible:
        raise RemoteControlError("offline Remote evidence cannot enter the optimizer")
    if execution_mode == "live" and not release.live_authorized:
        raise RemoteControlError("this Remote release does not authorize live receipts")
    if optimizer_eligible and execution_mode != "live":
        raise RemoteControlError("only live Remote evidence may be optimizer eligible")
    outcome = _strict_string("outcome", row["outcome"])
    if outcome not in {
        "completed",
        "safety_stop",
        "infrastructure_failure",
        "offline_replay",
    }:
        raise RemoteControlError("Remote receipt outcome is unsupported")
    exact_matches = {
        "trial_uid": prepared.trial_uid,
        "occurrence_uid": prepared.occurrence_uid,
        "transport_candidate_uid": prepared.transport_candidate_uid,
        "envelope_sha256": prepared.envelope_sha256,
        "release_sha256": release.release_sha256,
        "transport_id": release.transport_id,
    }
    for field, expected in exact_matches.items():
        if row[field] != expected:
            raise RemoteControlError(f"Remote receipt {field} differs")
    return RemoteTrialReceipt(
        trial_uid=prepared.trial_uid,
        occurrence_uid=prepared.occurrence_uid,
        transport_candidate_uid=prepared.transport_candidate_uid,
        envelope_sha256=prepared.envelope_sha256,
        release_sha256=release.release_sha256,
        transport_id=release.transport_id,
        execution_mode=execution_mode,
        outcome=outcome,
        artifacts_sha256=checked_artifacts,
        command_zero_confirmed=True,
        controller_inactive_confirmed=True,
        watchdog_status=watchdog_status,
        optimizer_eligible=optimizer_eligible,
    )


def import_result(
    payload: Mapping[str, Any],
    *,
    prepared: PreparedControlTrial,
    release: RemoteRelease,
) -> dict[str, Any]:
    """Validate a receipt but refuse campaign mutation until the adapter lands."""

    receipt = validate_receipt(payload, prepared=prepared, release=release)
    return {
        "ok": False,
        "status": "validated_not_committed",
        "blocker": "campaign_transport_adapter_not_synced",
        "trial_uid": receipt.trial_uid,
        "occurrence_uid": receipt.occurrence_uid,
        "transport_candidate_uid": receipt.transport_candidate_uid,
        "transport_id": receipt.transport_id,
        "optimizer_eligible": False,
    }
