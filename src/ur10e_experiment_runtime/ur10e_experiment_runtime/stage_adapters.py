"""Step5d-owned declarative adapter and frozen legacy-wire parity contract."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Mapping

from .identity import canonical_sha256
from .candidate_identity import ControlCandidateUid


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
PATH_ORIGIN_XY_M = (0.487795411149049, 0.12932679270060748)
PATH_U_ALONG_XY = (-0.010785642631908187, 0.9999418332648238)
PATH_P_LATERAL_XY = (-0.9999418332648239, -0.010785642631908406)
ACTIVE_STAGE25_CODE = 25.0
STAGE_CODE_TOLERANCE = 0.03
CONTROLLER_TICK_PERIOD_NS = 2_000_000
KNOWN_INACTIVE_CONTROLLER_STAGES = (
    20.0,
    22.0,
    23.0,
    24.0,
    24.2,
    25.05,
    25.15,
    25.3,
    25.95,
    29.0,
    70.0,
)


class ControllerProgressPhase(IntEnum):
    UNKNOWN = 0
    INACTIVE = 1
    ACTIVE_STAGE25 = 2


@dataclass(slots=True)
class ControllerProgress:
    phase: ControllerProgressPhase = ControllerProgressPhase.UNKNOWN
    controller_tick_seq: int = 0
    controller_timestamp_ns: int = 0
    age_ns: int = -1
    progress_s: float = math.nan
    center_x_m: float = math.nan
    center_y_m: float = math.nan
    center_z_m: float = math.nan
    reference_sha256: str = ""
    monotonic: bool = False
    center_frozen: bool = False


def moving_sphere_reference_sha256(physical_prior_sha256: str) -> str:
    if (
        len(physical_prior_sha256) != 64
        or any(character not in "0123456789abcdef" for character in physical_prior_sha256)
    ):
        raise ValueError("physical prior fingerprint must be a lowercase SHA256")
    return canonical_sha256(
        {
            "schema": "step5d.moving-sphere-reference/v2",
            "trajectory_parameters_sha256": TRAJECTORY_PARAMETERS_SHA256,
            "trajectory": TRAJECTORY_PARAMETERS,
            "origin_xy_m": list(PATH_ORIGIN_XY_M),
            "u_along_xy": list(PATH_U_ALONG_XY),
            "p_lateral_xy": list(PATH_P_LATERAL_XY),
            "physical_prior_sha256": physical_prior_sha256,
        }
    )


def stage_autotune_adapter_fingerprint() -> str:
    return canonical_sha256(
        {
            "schema": "ur10e.stage-autotune-adapter-identity/v1",
            "component_id": ADAPTER_ID,
            "component_version": COMPONENT_VERSION,
            "stage_id": STAGE_ID,
            "source_stage_id": SOURCE_STAGE_ID,
            "controller_id": CONTROLLER_ID,
            "trajectory_parameters_sha256": TRAJECTORY_PARAMETERS_SHA256,
            "frame_contract_sha256": FRAME_CONTRACT_SHA256,
            "surface_sha256": SURFACE_SHA256,
            "safety_policy_sha256": SAFETY_POLICY_SHA256,
            "register_contract": integer_register_contract(),
        }
    )


def moving_sphere_safety_envelope_fingerprint(
    physical_prior_sha256: str,
    *,
    stopping_bound_fingerprint: str | None,
) -> str:
    reference_sha256 = moving_sphere_reference_sha256(physical_prior_sha256)
    if stopping_bound_fingerprint is not None and (
        len(stopping_bound_fingerprint) != 64
        or any(
            character not in "0123456789abcdef"
            for character in stopping_bound_fingerprint
        )
    ):
        raise ValueError("stopping bound fingerprint must be a lowercase SHA256")
    return canonical_sha256(
        {
            "schema": "ur10e.moving-sphere-safety-envelope/v1",
            "active_stage": ACTIVE_STAGE25_CODE,
            "radius_m": 0.015,
            "reference_sha256": reference_sha256,
            "safety_policy_sha256": SAFETY_POLICY_SHA256,
            "stopping_bound_fingerprint": stopping_bound_fingerprint,
            "legacy_aabb_enforced": False,
            "fail_closed": True,
        }
    )


def _cycloid_scalars(elapsed_s: float) -> tuple[float, float, float, float, float]:
    duration_s = float(TRAJECTORY_PARAMETERS["duration_s"])
    parameters = TRAJECTORY_PARAMETERS["parameters"]
    amplitude_m = float(parameters["amplitude_m"])
    omega_rad_s = float(parameters["omega_rad_s"])
    progress_s = min(max(float(elapsed_s), 0.0), duration_s)
    phase_rad = omega_rad_s * progress_s
    local_x_m = amplitude_m * (phase_rad - math.sin(phase_rad))
    local_y_m = amplitude_m * (1.0 - math.cos(phase_rad))
    return progress_s, phase_rad, local_x_m, local_y_m, omega_rad_s


def frozen_step5d_path_reference(
    pose_xy: tuple[float, float],
    elapsed_s: float,
) -> dict[str, Any]:
    """Legacy bridge-shaped view of the adapter-owned frozen trajectory."""

    progress_s, phase_rad, local_x_m, local_y_m, omega_rad_s = _cycloid_scalars(
        elapsed_s
    )
    desired_x = (
        PATH_ORIGIN_XY_M[0]
        + local_x_m * PATH_U_ALONG_XY[0]
        + local_y_m * PATH_P_LATERAL_XY[0]
    )
    desired_y = (
        PATH_ORIGIN_XY_M[1]
        + local_x_m * PATH_U_ALONG_XY[1]
        + local_y_m * PATH_P_LATERAL_XY[1]
    )
    amplitude_m = float(TRAJECTORY_PARAMETERS["parameters"]["amplitude_m"])
    local_vx_m_s = amplitude_m * omega_rad_s * (1.0 - math.cos(phase_rad))
    local_vy_m_s = amplitude_m * omega_rad_s * math.sin(phase_rad)
    desired_vx = (
        local_vx_m_s * PATH_U_ALONG_XY[0]
        + local_vy_m_s * PATH_P_LATERAL_XY[0]
    )
    desired_vy = (
        local_vx_m_s * PATH_U_ALONG_XY[1]
        + local_vy_m_s * PATH_P_LATERAL_XY[1]
    )
    local = {
        "path_time_s": progress_s,
        "progress": progress_s,
        "phase_rad": phase_rad,
        "local_x_m": local_x_m,
        "local_y_m": local_y_m,
        "local_vx_m_s": local_vx_m_s,
        "local_vy_m_s": local_vy_m_s,
    }
    return {
        "stage_id": STAGE_ID,
        "progress": progress_s,
        "path_time_s": progress_s,
        "phase_rad": phase_rad,
        "desired_xy": (desired_x, desired_y),
        "desired_velocity_xy": (desired_vx, desired_vy),
        "path_error_xy": (desired_x - pose_xy[0], desired_y - pose_xy[1]),
        "local": local,
    }


class Stage25ControllerProgressAdapter:
    """Allocation-free typed controller-progress owner for the sphere tick."""

    __slots__ = (
        "reference_sha256",
        "allow_tick_gaps",
        "progress",
        "last_controller_timestamp_ns",
        "_last_controller_tick_seq",
        "last_progress_s",
        "anchor_z_m",
    )

    def __init__(
        self,
        *,
        physical_prior_sha256: str,
        allow_tick_gaps: bool = False,
    ) -> None:
        self.reference_sha256 = moving_sphere_reference_sha256(
            physical_prior_sha256
        )
        self.allow_tick_gaps = bool(allow_tick_gaps)
        self.progress = ControllerProgress(reference_sha256=self.reference_sha256)
        self.last_controller_timestamp_ns = 0
        self._last_controller_tick_seq = 0
        self.last_progress_s = math.nan
        self.anchor_z_m = math.nan

    def reset(self) -> None:
        self.progress.phase = ControllerProgressPhase.UNKNOWN
        self.progress.controller_tick_seq = 0
        self.progress.controller_timestamp_ns = 0
        self.progress.age_ns = -1
        self.progress.progress_s = math.nan
        self.progress.center_x_m = math.nan
        self.progress.center_y_m = math.nan
        self.progress.center_z_m = math.nan
        self.progress.reference_sha256 = self.reference_sha256
        self.progress.monotonic = False
        self.progress.center_frozen = False
        self.last_controller_timestamp_ns = 0
        self._last_controller_tick_seq = 0
        self.last_progress_s = math.nan
        self.anchor_z_m = math.nan

    def sample(
        self,
        *,
        stage: float | None,
        controller_progress_s: float | None,
        controller_tick_seq: int | None,
        controller_timestamp_s: float | None,
        age_ns: int | None,
        tcp_z_m: float | None,
    ) -> ControllerProgress:
        out = self.progress
        out.reference_sha256 = self.reference_sha256
        out.age_ns = age_ns if isinstance(age_ns, int) and not isinstance(age_ns, bool) else -1
        out.progress_s = (
            float(controller_progress_s)
            if controller_progress_s is not None
            else math.nan
        )
        out.center_x_m = math.nan
        out.center_y_m = math.nan
        out.center_z_m = math.nan
        out.monotonic = False
        out.center_frozen = False
        if stage is None or not math.isfinite(float(stage)):
            out.phase = ControllerProgressPhase.UNKNOWN
            return out
        stage_value = float(stage)
        if abs(stage_value - ACTIVE_STAGE25_CODE) < STAGE_CODE_TOLERANCE:
            out.phase = ControllerProgressPhase.ACTIVE_STAGE25
        else:
            inactive = False
            for known in KNOWN_INACTIVE_CONTROLLER_STAGES:
                if abs(stage_value - known) < STAGE_CODE_TOLERANCE:
                    inactive = True
                    break
            if inactive:
                out.phase = ControllerProgressPhase.INACTIVE
                return out
            out.phase = ControllerProgressPhase.UNKNOWN
            return out
        timestamp_value = (
            float(controller_timestamp_s)
            if controller_timestamp_s is not None
            else math.nan
        )
        tick_valid = (
            isinstance(controller_tick_seq, int)
            and not isinstance(controller_tick_seq, bool)
            and controller_tick_seq > 0
        )
        if math.isfinite(timestamp_value) and timestamp_value > 0.0 and tick_valid:
            timestamp_ns = int(timestamp_value * 1_000_000_000.0)
            out.controller_timestamp_ns = timestamp_ns
            out.controller_tick_seq = controller_tick_seq
            if (
                timestamp_ns > self.last_controller_timestamp_ns
                and (
                    self.last_controller_timestamp_ns == 0
                    or controller_tick_seq == self._last_controller_tick_seq + 1
                    or (
                        self.allow_tick_gaps
                        and controller_tick_seq > self._last_controller_tick_seq
                    )
                )
            ):
                out.monotonic = True
                self.last_controller_timestamp_ns = timestamp_ns
                self._last_controller_tick_seq = controller_tick_seq
        else:
            out.controller_timestamp_ns = 0
            out.controller_tick_seq = 0
        if not math.isfinite(out.progress_s):
            return out
        if not 0.0 <= out.progress_s <= float(TRAJECTORY_PARAMETERS["duration_s"]):
            return out
        if not math.isfinite(self.anchor_z_m):
            if tcp_z_m is None or not math.isfinite(float(tcp_z_m)):
                return out
            self.anchor_z_m = float(tcp_z_m)
        amplitude_m = float(TRAJECTORY_PARAMETERS["parameters"]["amplitude_m"])
        omega_rad_s = float(TRAJECTORY_PARAMETERS["parameters"]["omega_rad_s"])
        phase_rad = omega_rad_s * out.progress_s
        local_x_m = amplitude_m * (phase_rad - math.sin(phase_rad))
        local_y_m = amplitude_m * (1.0 - math.cos(phase_rad))
        out.center_x_m = (
            PATH_ORIGIN_XY_M[0]
            + local_x_m * PATH_U_ALONG_XY[0]
            + local_y_m * PATH_P_LATERAL_XY[0]
        )
        out.center_y_m = (
            PATH_ORIGIN_XY_M[1]
            + local_x_m * PATH_U_ALONG_XY[1]
            + local_y_m * PATH_P_LATERAL_XY[1]
        )
        out.center_z_m = self.anchor_z_m
        out.center_frozen = (
            math.isfinite(self.last_progress_s)
            and out.progress_s == self.last_progress_s
        )
        self.last_progress_s = out.progress_s
        return out

HOST_TO_TP_INTEGER_REGISTERS = {
    "campaign_epoch": 24,
    "trial_id": 25,
    "command": 26,
    "candidate_token": 27,
    "execution_profile_id": 28,
    "command_seq": 29,
    "batch_row_index": 30,
}
TP_TO_HOST_INTEGER_REGISTERS = {
    "campaign_epoch_echo": 24,
    "trial_id_echo": 25,
    "state": 26,
    "candidate_token_echo": 27,
    "terminal_reason": 28,
    "execution_profile_id_echo": 29,
    "consumed_command_seq": 30,
    "batch_row_index_echo": 31,
    "return_reference_kind_echo": 32,
    "return_guard_mask": 33,
}
CSV_IDENTITY_COLUMNS = ("autotune_trial_uid", "autotune_backend_id")
CSV_HANDSHAKE_COLUMNS = tuple(
    f"ur_output_int_register_{index}" for index in range(24, 34)
)
OVERLAY_FIELDS = (
    "force_p_gain",
    "force_i_gain",
    "force_damping",
    "orientation_ko",
    "normal_filter_tau_s",
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
LEGACY_OVERLAY_FIELDS = tuple(
    field for field in OVERLAY_FIELDS if field != "normal_filter_tau_s"
)
CONTROL_CANDIDATE_FIELDS = (
    "force_p_gain",
    "force_i_gain",
    "force_damping",
    "orientation_ko",
    "normal_filter_tau_s",
)
LEGACY_CONTROL_CANDIDATE_FIELDS = tuple(
    field for field in CONTROL_CANDIDATE_FIELDS if field != "normal_filter_tau_s"
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
    fields = (
        CONTROL_CANDIDATE_FIELDS
        if "normal_filter_tau_s" in candidate
        else LEGACY_CONTROL_CANDIDATE_FIELDS
    )
    values = {
        name: _finite(candidate.get(name), name) for name in fields
    }
    return ControlCandidateUid.from_overlay(values).digest


def normalize_trial_overlay(overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the actual legacy V3 mailbox overlay without reading config."""

    if not isinstance(overlay, Mapping) or frozenset(overlay) not in {
        frozenset(OVERLAY_FIELDS),
        frozenset(LEGACY_OVERLAY_FIELDS),
    }:
        raise ValueError("Step5d V3 trial overlay fields differ from the frozen wire schema")
    normalized: dict[str, Any] = {}
    for name in OVERLAY_FIELDS:
        if name == "normal_filter_tau_s" and name not in overlay:
            continue
        value = overlay[name]
        normalized[name] = (
            str(value) if name in {"control_candidate_uid", "execution_profile_id"}
            else _finite(value, name)
        )
    if normalized["force_p_gain"] <= 0.0 or normalized["force_damping"] <= 0.0:
        raise ValueError("force P and damping must be positive")
    if normalized["force_i_gain"] < 0.0:
        raise ValueError("force I must be non-negative")
    if normalized.get("normal_filter_tau_s", 0.35) <= 0.0:
        raise ValueError("normal filter tau must be positive")
    supplied_control_uid = ControlCandidateUid.parse(
        normalized["control_candidate_uid"], allow_legacy=True
    )
    expected_digest = control_candidate_uid(normalized)
    if supplied_control_uid.digest != expected_digest:
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
                "sha256": "70e6d4ef41a1427acbfcbff38b898b00d9ebf277547331d701f253b49d7a72ce",
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
