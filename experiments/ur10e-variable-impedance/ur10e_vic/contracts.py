"""Validated, immutable contracts shared by all offline VIC policies."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Iterable, Sequence


HISTORY_WINDOW = 16
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _vector(values: Iterable[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length:
        raise ValueError(f"{name} must contain {length} values")
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must be finite")
    return result


def _matrix(
    rows: Iterable[Iterable[float]],
    row_count: int,
    column_count: int,
    name: str,
) -> tuple[tuple[float, ...], ...]:
    result = tuple(_vector(row, column_count, f"{name} row") for row in rows)
    if len(result) != row_count:
        raise ValueError(f"{name} must contain {row_count} rows")
    return result


@dataclass(frozen=True)
class PoseSample:
    """Position in metres plus scalar-first unit quaternion."""

    position_m: tuple[float, float, float] | Sequence[float]
    quaternion_wxyz: tuple[float, float, float, float] | Sequence[float]

    def __post_init__(self) -> None:
        position = _vector(self.position_m, 3, "position_m")
        quaternion = _vector(self.quaternion_wxyz, 4, "quaternion_wxyz")
        norm = math.sqrt(sum(value * value for value in quaternion))
        if norm <= 1e-12:
            raise ValueError("quaternion_wxyz must have non-zero norm")
        normalized = tuple(value / norm for value in quaternion)
        sign_anchor = next(
            (value for value in normalized if abs(value) > 1e-15),
            1.0,
        )
        if sign_anchor < 0.0:
            normalized = tuple(-value for value in normalized)
        object.__setattr__(self, "position_m", position)
        object.__setattr__(self, "quaternion_wxyz", normalized)


@dataclass(frozen=True)
class ImpedanceObservation:
    """One policy observation with a fixed 80 ms history at 200 Hz."""

    sequence: int
    timestamp_s: float
    pose_history: tuple[PoseSample, ...] | Sequence[PoseSample]
    twist_history: tuple[tuple[float, ...], ...] | Sequence[Sequence[float]]
    wrench_history: tuple[tuple[float, ...], ...] | Sequence[Sequence[float]]
    nominal_zft: PoseSample
    joint_position_rad: tuple[float, ...] | Sequence[float]
    joint_velocity_rad_s: tuple[float, ...] | Sequence[float]
    jacobian_base: (
        tuple[tuple[float, ...], ...] | Sequence[Sequence[float]] | None
    )
    frame_id: str
    sensor_id: str
    calibration_hash: str

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if not math.isfinite(self.timestamp_s) or self.timestamp_s < 0.0:
            raise ValueError("timestamp_s must be finite and non-negative")
        poses = tuple(self.pose_history)
        if len(poses) != HISTORY_WINDOW or not all(
            isinstance(pose, PoseSample) for pose in poses
        ):
            raise ValueError(f"pose_history must contain {HISTORY_WINDOW} PoseSample values")
        twists = _matrix(self.twist_history, HISTORY_WINDOW, 6, "twist_history")
        wrenches = _matrix(self.wrench_history, HISTORY_WINDOW, 6, "wrench_history")
        joint_position = _vector(self.joint_position_rad, 6, "joint_position_rad")
        joint_velocity = _vector(
            self.joint_velocity_rad_s, 6, "joint_velocity_rad_s"
        )
        jacobian = (
            None
            if self.jacobian_base is None
            else _matrix(self.jacobian_base, 6, 6, "jacobian_base")
        )
        for field_name in ("frame_id", "sensor_id", "calibration_hash"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must be non-empty")
        object.__setattr__(self, "pose_history", poses)
        object.__setattr__(self, "twist_history", twists)
        object.__setattr__(self, "wrench_history", wrenches)
        object.__setattr__(self, "joint_position_rad", joint_position)
        object.__setattr__(self, "joint_velocity_rad_s", joint_velocity)
        object.__setattr__(self, "jacobian_base", jacobian)

    @property
    def pose(self) -> PoseSample:
        return self.pose_history[-1]

    @property
    def twist(self) -> tuple[float, ...]:
        return self.twist_history[-1]

    @property
    def wrench(self) -> tuple[float, ...]:
        return self.wrench_history[-1]


@dataclass(frozen=True)
class ImpedanceProposal:
    """A six-axis diagonal impedance proposal; never an authorization."""

    generated_at_s: float
    s_zft: PoseSample
    stiffness: tuple[float, ...] | Sequence[float]
    damping: tuple[float, ...] | Sequence[float]
    confidence: float
    age_s: float
    source: str
    model_hash: str
    valid: bool = True
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.generated_at_s) or self.generated_at_s < 0.0:
            raise ValueError("generated_at_s must be finite and non-negative")
        stiffness = _vector(self.stiffness, 6, "stiffness")
        damping = _vector(self.damping, 6, "damping")
        if any(value < 0.0 for value in stiffness + damping):
            raise ValueError("stiffness and damping must be positive semidefinite")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.valid and self.confidence <= 0.0:
            raise ValueError("valid proposals require confidence > 0")
        if not math.isfinite(self.age_s) or self.age_s < 0.0:
            raise ValueError("age_s must be finite and non-negative")
        if not self.source.strip():
            raise ValueError("source must be non-empty")
        is_dbil = self.source.lower().startswith("dbil")
        if self.valid and is_dbil and not _SHA256_RE.fullmatch(
            self.model_hash
        ):
            raise ValueError("valid DBIL proposals require a lowercase SHA-256 model hash")
        if is_dbil and not self.shadow_only:
            raise ValueError("DBIL proposals are permanently shadow-only")
        object.__setattr__(self, "stiffness", stiffness)
        object.__setattr__(self, "damping", damping)


@dataclass(frozen=True)
class BackendCommand:
    """Backend-neutral command with a strict surrogate/torque claim boundary."""

    backend: str
    backend_fidelity: str
    mode: str
    qdot_rad_s: tuple[float, ...] | Sequence[float] | None = None
    torque_nm: tuple[float, ...] | Sequence[float] | None = None
    shadow_only: bool = True
    claim_boundary: str = "offline_scaffold_only"
    diagnostics: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.backend_fidelity not in {"surrogate", "true_torque"}:
            raise ValueError("backend_fidelity must be surrogate or true_torque")
        if self.mode not in {"command", "hold", "stop"}:
            raise ValueError("mode must be command, hold, or stop")
        qdot = (
            None
            if self.qdot_rad_s is None
            else _vector(self.qdot_rad_s, 6, "qdot_rad_s")
        )
        torque = (
            None
            if self.torque_nm is None
            else _vector(self.torque_nm, 6, "torque_nm")
        )
        if self.mode == "command" and (qdot is None) == (torque is None):
            raise ValueError("command mode requires exactly one of qdot_rad_s or torque_nm")
        if self.mode != "command" and (qdot is not None or torque is not None):
            raise ValueError("hold/stop modes cannot contain a motion command")
        if qdot is not None and self.backend_fidelity != "surrogate":
            raise ValueError("qdot commands must be labelled surrogate")
        if torque is not None and self.backend_fidelity != "true_torque":
            raise ValueError("torque commands must be labelled true_torque")
        if not self.claim_boundary.strip():
            raise ValueError("claim_boundary must be non-empty")
        object.__setattr__(self, "qdot_rad_s", qdot)
        object.__setattr__(self, "torque_nm", torque)


@dataclass(frozen=True)
class ArtifactBinding:
    role: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.role.strip() or not self.path.strip():
            raise ValueError("artifact role and path must be non-empty")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("artifact sha256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True)
class RunManifest:
    """Hash-bound run identity that cannot promote claims by implication."""

    experiment_id: str
    controller_software: str
    profile: str
    backend_fidelity: str
    execution_mode: str
    claim_level: str
    artifacts: tuple[ArtifactBinding, ...] | Sequence[ArtifactBinding]
    live_authorized: bool = False
    dbil_active_enabled: bool = False

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.experiment_id, self.controller_software, self.profile)
        ):
            raise ValueError("experiment, controller software, and profile are required")
        if self.backend_fidelity not in {"surrogate", "true_torque"}:
            raise ValueError("invalid backend_fidelity")
        if self.execution_mode not in {"offline_only", "shadow", "active"}:
            raise ValueError("invalid execution_mode")
        allowed_claims = {
            "offline_scaffold",
            "shadow_evidence",
            "surrogate_live",
            "true_torque_live",
        }
        if self.claim_level not in allowed_claims:
            raise ValueError("invalid claim_level")
        artifacts = tuple(self.artifacts)
        roles = {artifact.role for artifact in artifacts}
        if "package" not in roles:
            raise ValueError("every run manifest requires a package artifact binding")
        if "dbil" in self.profile.lower() and not {"dataset", "checkpoint"} <= roles:
            raise ValueError("DBIL profiles require dataset and checkpoint bindings")
        if self.execution_mode == "offline_only" and self.claim_level != "offline_scaffold":
            raise ValueError("offline-only runs cannot claim live or shadow acceptance")
        if self.execution_mode == "shadow" and self.claim_level != "shadow_evidence":
            raise ValueError("shadow runs can only claim shadow evidence")
        if self.execution_mode == "active" and not self.live_authorized:
            raise ValueError("active runs require explicit live authorization")
        if self.execution_mode == "active":
            expected_live_claim = (
                "surrogate_live"
                if self.backend_fidelity == "surrogate"
                else "true_torque_live"
            )
            if self.claim_level != expected_live_claim:
                raise ValueError(
                    f"active {self.backend_fidelity} runs require {expected_live_claim} claim"
                )
        if self.backend_fidelity == "surrogate" and self.claim_level == "true_torque_live":
            raise ValueError("a surrogate backend cannot claim true torque control")
        if self.dbil_active_enabled:
            raise ValueError("DBIL active mode is permanently disabled")
        if self.execution_mode == "active" and "dbil" in self.profile.lower():
            raise ValueError("DBIL profiles are permanently shadow-only")
        object.__setattr__(self, "artifacts", artifacts)


@dataclass(frozen=True)
class ClaimState:
    """Keep package, authorization, run, acceptance, and reproduction separate."""

    package_status: str
    authorization_status: str
    run_status: str
    acceptance_status: str
    reproduction_status: str

    def __post_init__(self) -> None:
        allowed = {
            "package_status": {"not_ready", "offline_scaffold_ready", "package_accepted"},
            "authorization_status": {"not_requested", "denied", "live_authorized"},
            "run_status": {"not_run", "offline_completed", "live_completed", "failed"},
            "acceptance_status": {"not_evaluated", "rejected", "live_accepted"},
            "reproduction_status": {"not_claimed", "reproduction_complete"},
        }
        for name, values in allowed.items():
            if getattr(self, name) not in values:
                raise ValueError(f"invalid {name}")
        if self.run_status == "live_completed" and self.authorization_status != "live_authorized":
            raise ValueError("live completion requires live authorization")
        if self.acceptance_status == "live_accepted" and self.run_status != "live_completed":
            raise ValueError("live acceptance requires a completed live run")
        if (
            self.reproduction_status == "reproduction_complete"
            and self.acceptance_status != "live_accepted"
        ):
            raise ValueError("reproduction completion requires live acceptance")
