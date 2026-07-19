"""Step5d-owned declarative adapter and frozen legacy-wire parity contract."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from .identity import canonical_sha256


if TYPE_CHECKING:  # pragma: no cover
    from .contracts import ExperimentSpec
    from .registry import ComponentRegistry


COMPONENT_VERSION = "1"
CONTROLLER_ID = "strict_rnn_speedj_autotune_v1"
STAGE_ID = "step5d_strict_rnn_autotune_v1"
SOURCE_STAGE_ID = "step5d_strict_rnn_ablation_v35"
ADAPTER_ID = "step5d_strict_rnn_autotune_adapter_v1"
TRAJECTORY_ID = "cycloid_v1"
TRAJECTORY_PARAMETERS = {
    "schema_version": "ur10e.trajectory_parameters/v1",
    "owner_experiment_id": STAGE_ID,
    "trajectory_id": TRAJECTORY_ID,
    "duration_s": 60.0,
    "reference_frame": "base",
    "parameters": {
        "equation_id": "canonical_cycloid_linear_time_v1",
        "amplitude_m": 0.015,
        "omega_rad_s": 0.1,
    },
}


def _legacy_canonical_sha256(value: Mapping[str, Any]) -> str:
    """Digest the frozen pre-runtime Step5d identity without making it canonical truth."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


TRAJECTORY_PARAMETERS_SHA256 = _legacy_canonical_sha256(TRAJECTORY_PARAMETERS)
FRAME_CONTRACT_SHA256 = (
    "2d54d55e65700d7a1ce99a61d5fe9284cd5d1177bfed38dfb7d1cdf556613952"
)
SURFACE_SHA256 = (
    "e0e4cda8c07d68161ff448223626b46d0eef903973d74e1af0483bf110aecfa4"
)
SAFETY_POLICY_SHA256 = (
    "e240e1f8b291c43bcb2df01bfc1c12e9393a1cdbce59618e2d103ea0e06c52e8"
)

HOST_TO_TP_INTEGER_REGISTERS = {
    "campaign_epoch": 24,
    "trial_id": 25,
    "command": 26,
    "candidate_token": 27,
    "execution_profile_id": 28,
    "command_seq": 29,
}
TP_TO_HOST_INTEGER_REGISTERS = {
    "campaign_epoch_echo": 24,
    "trial_id_echo": 25,
    "state": 26,
    "candidate_token_echo": 27,
    "terminal_reason": 28,
    "execution_profile_id_echo": 29,
    "consumed_command_seq": 30,
}
CSV_IDENTITY_COLUMNS = ("autotune_trial_uid", "autotune_backend_id")
CSV_HANDSHAKE_COLUMNS = tuple(
    f"ur_output_int_register_{index}" for index in range(24, 31)
)
OVERLAY_FIELDS = (
    "force_p_gain",
    "force_i_gain",
    "force_damping",
    "orientation_ko",
    "control_candidate_uid",
    "execution_profile_id",
    "step5d_preload_filtered_min_n",
    "step5d_preload_filtered_max_n",
    "step5d_preload_raw_min_n",
    "step5d_preload_raw_max_n",
    "step5d_preload_force_norm_max_n",
    "step5d_preload_hold_s",
    "step5d_preload_timeout_s",
)
CONTROL_CANDIDATE_FIELDS = (
    "force_p_gain",
    "force_i_gain",
    "force_damping",
    "orientation_ko",
)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def control_candidate_uid(candidate: Mapping[str, Any]) -> str:
    values = {
        name: _finite(candidate.get(name), name) for name in CONTROL_CANDIDATE_FIELDS
    }
    return _legacy_canonical_sha256(
        {"schema": "step5d.autotune-v3/control-candidate/v2", **values}
    )


def normalize_trial_overlay(overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the actual legacy V3 mailbox overlay without reading config."""

    if not isinstance(overlay, Mapping) or set(overlay) != set(OVERLAY_FIELDS):
        raise ValueError("Step5d V3 trial overlay fields differ from the frozen wire schema")
    normalized: dict[str, Any] = {}
    for name in OVERLAY_FIELDS:
        value = overlay[name]
        normalized[name] = (
            str(value) if name in {"control_candidate_uid", "execution_profile_id"}
            else _finite(value, name)
        )
    if normalized["force_p_gain"] <= 0.0 or normalized["force_damping"] <= 0.0:
        raise ValueError("force P and damping must be positive")
    if normalized["force_i_gain"] < 0.0:
        raise ValueError("force I must be non-negative")
    if normalized["control_candidate_uid"] != control_candidate_uid(normalized):
        raise ValueError("control_candidate_uid differs from the actual overlay")
    return normalized


def legacy_trial_uid(material: Mapping[str, Any]) -> str:
    """Reproduce the current Step5d ``TrialSpec.trial_uid`` identity exactly."""

    required = {
        "campaign_id",
        "campaign_epoch",
        "campaign_fingerprint",
        "trial_id",
        "candidate_token",
        "command_seq",
        "plant_epoch",
        "candidate",
        "execution_profile",
        "backend_id",
        "source_fingerprint",
        "config_fingerprint",
        "transition",
        "search_attestation",
    }
    if not isinstance(material, Mapping) or set(material) != required:
        raise ValueError("legacy trial identity material differs")
    return _legacy_canonical_sha256(
        {"schema": "step5d.autotune/v1", **dict(material)}
    )


def verify_exact_ack(
    *,
    arm: Mapping[str, Any],
    ack: Mapping[str, Any],
    safe_closure: bool,
    immutable_bundle_written: bool,
) -> bool:
    """Check current ACK parity: exact identity, newer sequence, bundle and closure."""

    identity_fields = (
        "campaign_epoch",
        "trial_id",
        "candidate_token",
        "execution_profile_id",
    )
    return bool(
        safe_closure
        and immutable_bundle_written
        and ack.get("command") == 2
        and all(ack.get(field) == arm.get(field) for field in identity_fields)
        and isinstance(arm.get("command_seq"), int)
        and isinstance(ack.get("command_seq"), int)
        and ack["command_seq"] > arm["command_seq"]
    )


@dataclass(frozen=True)
class StageAutotuneAdapter:
    component_id: str = ADAPTER_ID
    version: str = COMPONENT_VERSION

    def validate_spec(self, document: Mapping[str, Any]) -> None:
        stage = _mapping(document.get("stage"), "stage")
        if dict(stage) != {
            "stage_key": "step5d",
            "stage_id": STAGE_ID,
            "program_id": STAGE_ID,
            "source_stage_id": SOURCE_STAGE_ID,
        }:
            raise ValueError("Step5d stage identity differs")
        components = _mapping(document.get("components"), "components")
        expected = {
            "trajectory": TRAJECTORY_ID,
            "controller": CONTROLLER_ID,
            "stage_adapter": ADAPTER_ID,
        }
        for role, component_id in expected.items():
            if dict(_mapping(components.get(role), f"components.{role}")) != {
                "id": component_id,
                "version": COMPONENT_VERSION,
            }:
                raise ValueError(f"components.{role} differs")
        if document.get("objective") != {
            "target_force_n": 12.0,
            "window_s": [5.0, 60.0],
            "window_semantics": "half_open",
            "bins": 550,
            "parameter_semantics": "force_pi_damping_v1",
        }:
            raise ValueError("Step5d objective differs")
        if document.get("trajectory_contract") != {
            "duration_s": 60.0,
            "reference_frame": "base",
            "parameters_sha256": TRAJECTORY_PARAMETERS_SHA256,
        }:
            raise ValueError("Step5d trajectory contract differs")
        if document.get("frame_contract") != {
            "base_frame": "base",
            "task_frame": "surface_tangent",
            "sensor_frame": "tcp",
            "tool_frame": "tool0",
            "contract_sha256": FRAME_CONTRACT_SHA256,
        }:
            raise ValueError("Step5d frame contract differs")
        if document.get("surface_contract") != {
            "surface_id": "step5_contact_surface_v1",
            "calibration_sha256": SURFACE_SHA256,
            "workspace_cage_sha256": SURFACE_SHA256,
        }:
            raise ValueError("Step5d surface contract differs")
        if document.get("safety_contract") != {
            "policy_id": "step5d_v35_permissive_contact_quota_safe_other",
            "policy_sha256": SAFETY_POLICY_SHA256,
            "fail_closed": True,
        }:
            raise ValueError("Step5d safety contract differs")
        bindings = _mapping(document.get("bindings"), "bindings")
        if bindings.get("source_sha256") != TRAJECTORY_PARAMETERS_SHA256:
            raise ValueError("Step5d legacy trajectory source digest differs")
        if bindings.get("config_sha256") != FRAME_CONTRACT_SHA256:
            raise ValueError("Step5d force/frame config digest differs")
        if bindings.get("controller") != {"id": CONTROLLER_ID, "sha256": None}:
            raise ValueError("Step5d controller readback must remain unclaimed")
        if bindings.get("tp_sha256") != {
            "program": None,
            "script": None,
            "txt": None,
        }:
            raise ValueError("Step5d TP delivery must remain unclaimed")
        readiness = document.get("readiness")
        if readiness != {
            "calibration_required": True,
            "calibration_complete": True,
            "evidence_sha256": SURFACE_SHA256,
        }:
            raise ValueError("Step5d calibration evidence differs")
        if document.get("warm_start") is not None:
            raise ValueError("Step5d parity spec does not accept a warm start")
        provenance = document.get("legacy_provenance")
        expected_provenance = [
            {
                "source_id": "step5d_autotune_v1_campaign",
                "artifact_path": "config/step5d_autotune_campaign_v1.json",
                "sha256": "90f1c92ba3497bfdcb08f9154ba007c98c25b8d31cf0df24716c4d69e7d44c43",
                "claim_class": "authoritative",
            },
            {
                "source_id": "step5d_autotune_v1_program_seed",
                "artifact_path": (
                    "programs/step5/step5d/step5d_strict_rnn_autotune_v1.script"
                ),
                "sha256": "6af0254d27ab241c5cf6b952483c5c573255e3afee9daf95f55a62b2387dc29e",
                "claim_class": "authoritative",
            },
        ]
        if provenance != expected_provenance:
            raise ValueError("Step5d legacy provenance differs")

    def plan(self, spec: "ExperimentSpec", lane_id: str) -> dict[str, Any]:
        self.validate_spec(spec.document)
        lane = spec.lane(lane_id)
        return {
            "schema": "ur10e.stage_autotune_plan/v1",
            "stage_id": STAGE_ID,
            "source_stage_id": SOURCE_STAGE_ID,
            "experiment_fingerprint": spec.fingerprint,
            "campaign_fingerprint": canonical_sha256(
                {
                    "schema": "ur10e.autotune_campaign_identity/v1",
                    "experiment_id": spec.experiment_id,
                    "experiment_fingerprint": spec.fingerprint,
                    "stage_key": "step5d",
                    "controller_id": CONTROLLER_ID,
                    "parameter_semantics": "force_pi_damping_v1",
                }
            ),
            "lane": lane_id,
            "backend_id": lane["backend_id"],
            "motion_ceiling": lane["motion_ceiling"],
            "trajectory_parameters": TRAJECTORY_PARAMETERS,
            "register_contract": integer_register_contract(),
            "csv_contract": {
                "identity_columns": list(CSV_IDENTITY_COLUMNS),
                "handshake_columns": list(CSV_HANDSHAKE_COLUMNS),
            },
            "overlay_fields": list(OVERLAY_FIELDS),
            "legacy_runtime_inputs": [],
            "external_actions": [],
        }


STEP5D_ADAPTER = StageAutotuneAdapter()


def integer_register_contract() -> dict[str, dict[str, int]]:
    return {
        "host_to_tp": dict(HOST_TO_TP_INTEGER_REGISTERS),
        "tp_to_host": dict(TP_TO_HOST_INTEGER_REGISTERS),
    }


def register_stage_adapters(registry: "ComponentRegistry") -> None:
    from .registry import ComponentKind

    registry.register(
        ComponentKind.STAGE_ADAPTER,
        STEP5D_ADAPTER.component_id,
        STEP5D_ADAPTER.version,
        STEP5D_ADAPTER,
    )
