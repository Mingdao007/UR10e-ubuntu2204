"""Offline backend contracts; this module has no network or upload capability."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Sequence

from .contracts import BackendCommand, ImpedanceObservation, ImpedanceProposal
from .math3d import damped_least_squares


DIRECT_TORQUE_MIN_VERSION = (5, 23, 0)
OFFICIAL_API_EVIDENCE = {
    "direct_torque": "https://www.universal-robots.com/manuals/EN/HTML/SW5_23/Content/prod-scriptmanual/all_scripts/direct_torque.htm",
    "get_coriolis_and_centrifugal_torques": "https://www.universal-robots.com/manuals/EN/HTML/SW5_23/Content/prod-scriptmanual/all_scripts/get_coriolis_and_centrifugal_torques.htm",
    "get_jacobian": "https://www.universal-robots.com/manuals/EN/HTML/SW5_23/Content/prod-scriptmanual/all_scripts/get_jacobian.htm",
    "release_notes": "https://www.universal-robots.com/articles/ur/release-notes/release-note-software-version-523x/",
}
SURROGATE_CLAIM_BOUNDARY = (
    "velocity_admittance_surrogate_only; not torque impedance; "
    "not DBIL hardware reproduction"
)
BASELINE_TRANSLATIONAL_STIFFNESS = (600.0, 600.0, 600.0)
DIRECT_TORQUE_BASELINE_K = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
DIRECT_TORQUE_K_MIN = (25.0, 25.0, 25.0, 0.5, 0.5, 0.5)
DIRECT_TORQUE_K_MAX = (1000.0, 1000.0, 1000.0, 60.0, 60.0, 60.0)
DIRECT_TORQUE_K_SLEW = (400.0, 400.0, 400.0, 20.0, 20.0, 20.0)
DIRECT_TORQUE_VIRTUAL_MASS = (2.0, 2.0, 2.0, 0.2, 0.2, 0.2)
DIRECT_TORQUE_MANIFEST_LIMITS = {
    "damping_max": [200.0, 200.0, 200.0, 30.0, 30.0, 30.0],
    "damping_sqrt_tolerance": 0.05,
    "equilibrium_translation_slew_max_m_s": 0.05,
    "force_norm_abs_max_n": 50.0,
    "joint_damping": [1.5, 1.5, 1.2, 0.3, 0.3, 0.2],
    "joint_position_rad_min": [0.4, -2.0, -3.0, -0.8, 1.3, -1.5],
    "joint_position_rad_max": [0.9, -1.2, -2.2, -0.2, 1.8, -0.7],
    "joint_speed_abs_max_rad_s": 1.0,
    "new_run_release_force_norm_max_n": 2.0,
    "new_run_release_pose_error_max_m": 0.002,
    "new_run_release_torque_norm_max_nm": 0.2,
    "orientation_damping_fixed": [4.898979486, 4.898979486, 4.898979486],
    "orientation_stiffness_fixed": [30.0, 30.0, 30.0],
    "pose_error_abs_max": [0.05, 0.05, 0.05, 0.35, 0.35, 0.35],
    "stiffness_baseline": list(DIRECT_TORQUE_BASELINE_K),
    "stiffness_max": list(DIRECT_TORQUE_K_MAX),
    "stiffness_min": list(DIRECT_TORQUE_K_MIN),
    "stiffness_slew_max_per_s": list(DIRECT_TORQUE_K_SLEW),
    "tcp_cage_xyz_max_m": [0.55, 0.2, 0.12],
    "tcp_cage_xyz_min_m": [0.3, 0.0, -0.02],
    "torque_abs_max_nm": [20.0, 20.0, 20.0, 8.0, 8.0, 8.0],
    "torque_norm_abs_max_nm": 3.0,
    "virtual_mass": list(DIRECT_TORQUE_VIRTUAL_MASS),
}


@dataclass(frozen=True)
class Step5bTwistInput:
    """A fresh command emitted by the already-validated Step5b scaffold."""

    sequence: int
    generated_at_s: float
    twist_base: tuple[float, ...] | Sequence[float]
    scaffold_sha256: str

    def __post_init__(self) -> None:
        twist = tuple(float(value) for value in self.twist_base)
        if self.sequence < 0:
            raise ValueError("Step5b sequence must be non-negative")
        if not math.isfinite(self.generated_at_s) or self.generated_at_s < 0.0:
            raise ValueError("Step5b timestamp must be finite and non-negative")
        if len(twist) != 6 or not all(math.isfinite(value) for value in twist):
            raise ValueError("Step5b twist must contain six finite values")
        if not re.fullmatch(r"[0-9a-f]{64}", self.scaffold_sha256):
            raise ValueError("Step5b scaffold hash must be a lowercase SHA-256")
        object.__setattr__(self, "twist_base", twist)


@dataclass(frozen=True)
class DirectTorquePacket:
    """Host-side representation used only to test controller packet gates."""

    sequence_before: int
    sequence_after: int
    heartbeat: int
    lease_id: int
    mode: int
    equilibrium_pose: tuple[float, ...] | Sequence[float]
    stiffness: tuple[float, ...] | Sequence[float]
    damping: tuple[float, ...] | Sequence[float]

    def __post_init__(self) -> None:
        for name in ("equilibrium_pose", "stiffness", "damping"):
            values = tuple(float(value) for value in getattr(self, name))
            if len(values) != 6 or not all(math.isfinite(value) for value in values):
                raise ValueError(f"direct-torque {name} must contain six finite values")
            object.__setattr__(self, name, values)


@dataclass(frozen=True)
class DirectTorqueGuardState:
    last_sequence: int = 0
    lease_id: int = 0
    last_equilibrium_pose: tuple[float, ...] | None = None
    locked_orientation: tuple[float, ...] | None = None
    last_stiffness: tuple[float, ...] | None = None


@dataclass(frozen=True)
class DirectTorqueGuardDecision:
    accepted: bool
    reason: str
    next_state: DirectTorqueGuardState


def validate_direct_torque_packet(
    packet: DirectTorquePacket,
    state: DirectTorqueGuardState,
    *,
    dt_s: float = 0.002,
    release_ready: bool,
    runtime_guard_ok: bool,
) -> DirectTorqueGuardDecision:
    """Python oracle for the offline URScript gate state machine.

    This does not generate torque.  Its purpose is to make frozen/mixed packet,
    slew, release-reset, and lease semantics executable in unit tests before
    URSim is available.
    """

    def reject(reason: str) -> DirectTorqueGuardDecision:
        return DirectTorqueGuardDecision(False, reason, state)

    if not math.isfinite(dt_s) or dt_s <= 0.0:
        raise ValueError("direct-torque oracle dt must be positive")
    if packet.mode != 1:
        return reject("mode_not_armed")
    if (
        packet.sequence_before != packet.sequence_after
        or packet.heartbeat != packet.sequence_after
    ):
        return reject("mixed_or_uncommitted_packet")
    sequence = packet.sequence_after
    if sequence <= 0 or sequence != state.last_sequence + 1:
        return reject("frozen_gap_or_regressed_sequence")
    if packet.lease_id <= 0 or (
        state.lease_id != 0 and packet.lease_id != state.lease_id
    ):
        return reject("exclusive_lease_mismatch")
    if not runtime_guard_ok:
        return reject("runtime_guard_failed")
    for index, (stiffness, damping) in enumerate(
        zip(packet.stiffness, packet.damping)
    ):
        if not DIRECT_TORQUE_K_MIN[index] <= stiffness <= DIRECT_TORQUE_K_MAX[index]:
            return reject("stiffness_out_of_bounds")
        expected_damping = 2.0 * math.sqrt(
            stiffness * DIRECT_TORQUE_VIRTUAL_MASS[index]
        )
        if abs(damping - expected_damping) > 0.05:
            return reject("damping_sqrt_relation_mismatch")
    if any(abs(packet.stiffness[index] - 30.0) > 1e-6 for index in range(3, 6)):
        return reject("orientation_stiffness_not_fixed")

    first_packet = state.last_sequence == 0
    if first_packet:
        if not release_ready:
            return reject("new_run_release_gate_failed")
        if any(
            abs(packet.stiffness[index] - DIRECT_TORQUE_BASELINE_K[index]) > 1e-6
            for index in range(6)
        ):
            return reject("new_run_must_restore_baseline_stiffness")
        locked_orientation = tuple(packet.equilibrium_pose[3:6])
    else:
        assert state.last_stiffness is not None
        assert state.last_equilibrium_pose is not None
        assert state.locked_orientation is not None
        locked_orientation = state.locked_orientation
        for index in range(6):
            decrease = state.last_stiffness[index] - packet.stiffness[index]
            if decrease < -1e-6 or decrease > DIRECT_TORQUE_K_SLEW[index] * dt_s + 1e-6:
                return reject("stiffness_increase_or_slew_violation")
        if any(
            abs(packet.equilibrium_pose[index] - state.last_equilibrium_pose[index])
            > 0.05 * dt_s + 1e-6
            for index in range(3)
        ):
            return reject("equilibrium_translation_slew_violation")
        if any(
            abs(packet.equilibrium_pose[index + 3] - locked_orientation[index])
            > 1e-6
            for index in range(3)
        ):
            return reject("orientation_equilibrium_not_locked")

    next_state = DirectTorqueGuardState(
        last_sequence=sequence,
        lease_id=packet.lease_id,
        last_equilibrium_pose=tuple(packet.equilibrium_pose),
        locked_orientation=locked_orientation,
        last_stiffness=tuple(packet.stiffness),
    )
    return DirectTorqueGuardDecision(True, "accepted", next_state)


def parse_polyscope_version(value: str) -> tuple[int, int, int]:
    match = re.search(r"(?:^|\D)(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        raise ValueError(f"cannot parse PolyScope version: {value!r}")
    return tuple(int(part or 0) for part in match.groups())


def direct_torque_supported(value: str) -> bool:
    return parse_polyscope_version(value) >= DIRECT_TORQUE_MIN_VERSION


def require_direct_torque_version(value: str) -> None:
    if not direct_torque_supported(value):
        raise RuntimeError(
            "direct_torque backend requires PolyScope 5.23.0 or newer; "
            f"received {value!r}"
        )


class VelocityAdmittanceSurrogate:
    """Bounded 5.11-compatible DLS qdot path, explicitly not true impedance."""

    def __init__(
        self,
        *,
        dls_damping: float = 0.05,
        qdot_limit_rad_s: float = 0.05,
        translation_twist_limit_m_s: float = 0.01,
        rotation_twist_limit_rad_s: float = 0.03,
        step5b_scaffold_sha256: str | None = None,
        step5b_activation_ready: bool = False,
        step5b_freshness_s: float = 0.01,
    ) -> None:
        if min(
            dls_damping,
            qdot_limit_rad_s,
            translation_twist_limit_m_s,
            rotation_twist_limit_rad_s,
        ) <= 0.0:
            raise ValueError("surrogate backend limits must be positive")
        self.dls_damping = dls_damping
        self.qdot_limit_rad_s = qdot_limit_rad_s
        self.twist_limits = (
            translation_twist_limit_m_s,
            translation_twist_limit_m_s,
            translation_twist_limit_m_s,
            rotation_twist_limit_rad_s,
            rotation_twist_limit_rad_s,
            rotation_twist_limit_rad_s,
        )
        if step5b_scaffold_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", step5b_scaffold_sha256
        ):
            raise ValueError("configured Step5b scaffold hash must be SHA-256")
        if not math.isfinite(step5b_freshness_s) or step5b_freshness_s <= 0.0:
            raise ValueError("Step5b freshness must be positive")
        self.step5b_scaffold_sha256 = step5b_scaffold_sha256
        self.step5b_activation_ready = bool(step5b_activation_ready)
        self.step5b_freshness_s = step5b_freshness_s

    def command(
        self,
        observation: ImpedanceObservation,
        proposal: ImpedanceProposal,
        *,
        step5b_input: Step5bTwistInput | None = None,
    ) -> BackendCommand:
        # A valid proposal is not necessarily command-capable.  In particular,
        # every DBIL proposal in this experiment is permanently shadow-only.
        # Keep this check at the backend boundary as a second line of defence
        # even when a capability-separated mux is used upstream.
        if proposal.shadow_only:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "shadow_proposal_not_command_capable"),),
            )
        if not proposal.valid:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "invalid_impedance_proposal"),),
            )
        if self.step5b_scaffold_sha256 is None:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "step5b_scaffold_binding_not_configured"),),
            )
        if not self.step5b_activation_ready:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "step5b_scaffold_activation_not_accepted"),),
            )
        if step5b_input is None:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "step5b_scaffold_not_bound"),),
            )
        step5b_age_s = observation.timestamp_s - step5b_input.generated_at_s
        if (
            step5b_input.scaffold_sha256 != self.step5b_scaffold_sha256
            or step5b_input.sequence != observation.sequence
            or step5b_age_s < 0.0
            or step5b_age_s > self.step5b_freshness_s
        ):
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "step5b_scaffold_binding_or_freshness_mismatch"),),
            )
        if any(
            abs(step5b_input.twist_base[index]) > self.twist_limits[index]
            for index in range(6)
        ):
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "step5b_twist_out_of_bounds"),),
            )
        if observation.jacobian_base is None:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "missing_calibrated_jacobian"),),
            )
        force = observation.wrench[:3]
        force_norm = math.sqrt(sum(value * value for value in force))
        if force_norm <= 1e-9:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "missing_reaction_normal"),),
            )
        reaction_normal = tuple(value / force_norm for value in force)
        baseline_linear = step5b_input.twist_base[:3]
        normal_velocity = sum(
            baseline_linear[index] * reaction_normal[index] for index in range(3)
        )
        effective_k = sum(
            reaction_normal[index] ** 2 * proposal.stiffness[index]
            for index in range(3)
        )
        baseline_k = sum(
            reaction_normal[index] ** 2
            * BASELINE_TRANSLATIONAL_STIFFNESS[index]
            for index in range(3)
        )
        # This dimensionless <=1 modulation changes only the normal component;
        # the validated Step5b tangential and orientation commands stay intact.
        compliance_scale = math.sqrt(max(0.0, min(1.0, effective_k / baseline_k)))
        desired_twist = [
            baseline_linear[index]
            + (compliance_scale - 1.0)
            * normal_velocity
            * reaction_normal[index]
            for index in range(3)
        ]
        desired_twist.extend(step5b_input.twist_base[3:])
        qdot = damped_least_squares(
            observation.jacobian_base, desired_twist, self.dls_damping
        )
        bounded_qdot = tuple(
            max(-self.qdot_limit_rad_s, min(self.qdot_limit_rad_s, value))
            for value in qdot
        )
        if not all(math.isfinite(value) for value in bounded_qdot):
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "nonfinite_qdot"),),
            )
        return BackendCommand(
            backend="velocity_admittance_surrogate",
            backend_fidelity="surrogate",
            mode="command",
            qdot_rad_s=bounded_qdot,
            shadow_only=False,
            claim_boundary=SURROGATE_CLAIM_BOUNDARY,
            diagnostics=(
                ("controller_compatibility", "PolyScope_5.11_velocity_path"),
                ("control_source", "hash_bound_validated_Step5b_twist"),
                ("step5b_scaffold_sha256", step5b_input.scaffold_sha256),
                ("normal_modulation", "dimensionless_sqrt_K_ratio_le_1"),
                ("policy_source", proposal.source),
            ),
        )


@dataclass(frozen=True)
class VelocityMuxResult:
    """One capability-mux result, including the route used for audit."""

    mode: str
    qdot_rad_s: tuple[float, ...] | None
    route: str


@dataclass(frozen=True)
class ShadowCommandMuxResult:
    command: tuple[float, ...]
    command_valid: bool
    route: str


class CapabilitySeparatedShadowMux:
    """Generic evidence mux that cannot route a shadow proposal to output."""

    def select(
        self,
        proposal: ImpedanceProposal | None,
        baseline_command: Sequence[float],
        baseline_valid: bool,
    ) -> ShadowCommandMuxResult:
        command = tuple(float(value) for value in baseline_command)
        if len(command) != 6 or not all(math.isfinite(value) for value in command):
            raise ValueError("baseline command must contain six finite values")
        if proposal is None:
            return ShadowCommandMuxResult(
                command, bool(baseline_valid), "baseline_no_proposal"
            )
        if proposal.shadow_only:
            return ShadowCommandMuxResult(
                command, bool(baseline_valid), "baseline_shadow_bypass"
            )
        return ShadowCommandMuxResult((0.0,) * 6, False, "active_capability_denied")


class CapabilitySeparatedVelocityMux:
    """Keep shadow computation physically unable to modify the output path.

    The established baseline is the only output when no active proposal is
    selected or when a proposal is marked ``shadow_only``.  A non-shadow
    proposal requires an explicit capability at construction time; backend
    rejection then produces stop rather than silently returning to baseline.
    """

    def __init__(
        self,
        backend: VelocityAdmittanceSurrogate,
        *,
        active_capability: bool = False,
    ) -> None:
        self.backend = backend
        self.active_capability = bool(active_capability)

    @staticmethod
    def _baseline(values: Sequence[float]) -> tuple[float, ...]:
        result = tuple(float(value) for value in values)
        if len(result) != 6 or not all(math.isfinite(value) for value in result):
            raise ValueError("baseline qdot must contain six finite values")
        return result

    def select(
        self,
        observation: ImpedanceObservation,
        proposal: ImpedanceProposal | None,
        baseline_qdot_rad_s: Sequence[float],
        *,
        step5b_input: Step5bTwistInput | None = None,
    ) -> VelocityMuxResult:
        baseline = self._baseline(baseline_qdot_rad_s)
        if proposal is None:
            return VelocityMuxResult("command", baseline, "baseline_no_proposal")
        if proposal.shadow_only:
            return VelocityMuxResult("command", baseline, "baseline_shadow_bypass")
        if not self.active_capability:
            return VelocityMuxResult("stop", None, "active_capability_denied")
        command = self.backend.command(
            observation, proposal, step5b_input=step5b_input
        )
        if command.mode != "command":
            return VelocityMuxResult("stop", None, "backend_rejected")
        if command.shadow_only or command.qdot_rad_s is None:
            raise RuntimeError("backend violated the active-command capability contract")
        return VelocityMuxResult("command", command.qdot_rad_s, "active_backend")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_step5b_scaffold_binding(
    binding_path: Path, repository_root: Path
) -> dict[str, Any]:
    """Validate stable pure-core/replay/acceptance bindings, never the big bridge."""

    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported Step5b scaffold binding schema")
    bindings = (
        ("source_path_from_repository_root", "source_sha256"),
        ("replay_validator_path_from_repository_root", "replay_validator_sha256"),
        ("acceptance_path_from_repository_root", "acceptance_sha256"),
    )
    for path_key, hash_key in bindings:
        relative = Path(str(payload.get(path_key, "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Step5b binding paths must be repository-relative")
        actual_path = repository_root / relative
        if not actual_path.is_file() or _sha256(actual_path) != payload.get(hash_key):
            raise ValueError(f"Step5b binding drifted: {path_key}")
    acceptance = json.loads(
        (repository_root / payload["acceptance_path_from_repository_root"]).read_text(
            encoding="utf-8"
        )
    )
    if bool(payload.get("activation_ready")) != bool(acceptance.get("accepted")):
        raise ValueError("Step5b activation readiness must mirror acceptance artifact")
    if payload.get("activation_ready"):
        raise ValueError(
            "this offline experiment may not self-promote Step5b live acceptance"
        )
    return payload


def load_direct_torque_bundle(layout_path: Path, template_path: Path) -> dict[str, Any]:
    """Validate that the offline template is exactly bound to its RTDE layout."""

    payload = json.loads(layout_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 2:
        raise ValueError("unsupported direct-torque layout schema")
    if payload.get("default_mode") != "disabled" or payload.get("upload_authorized"):
        raise ValueError("offline direct-torque bundle must default to disabled/no-upload")
    if payload.get("minimum_polyscope") != "5.23.0":
        raise ValueError("direct-torque layout must retain the 5.23.0 version gate")
    if payload.get("official_api_evidence") != OFFICIAL_API_EVIDENCE:
        raise ValueError("official PolyScope 5.23 API evidence binding drifted")
    expected_hash = payload.get("template_sha256")
    actual_hash = _sha256(template_path)
    if expected_hash != actual_hash:
        raise ValueError("URScript template hash does not match RTDE manifest")

    registers = payload.get("registers", {})
    input_double = registers.get("input_double", {})
    flattened = [index for indices in input_double.values() for index in indices]
    if sorted(flattened) != list(range(24, 42)) or len(flattened) != len(set(flattened)):
        raise ValueError("input doubles must bind exactly registers 24..41 once")
    input_integer = registers.get("input_integer", {})
    if input_integer != {
        "mode": 24,
        "sequence": 25,
        "heartbeat": 26,
        "exclusive_lease": 27,
    }:
        raise ValueError("input integer packet/lease binding drifted")
    if registers.get("output_integer") != {
        "state": 24,
        "echo_sequence": 25,
        "fault_code": 26,
        "echo_exclusive_lease": 27,
    }:
        raise ValueError("output integer state/sequence/fault binding drifted")
    if registers.get("output_double") != {
        "max_abs_tau_nm": 24,
        "steptime_s": 25,
    }:
        raise ValueError("output double diagnostic binding drifted")
    for family in registers.values():
        values: list[int] = []
        if isinstance(family, dict):
            for value in family.values():
                values.extend(value if isinstance(value, list) else [value])
        if any(index < 24 or index > 47 for index in values):
            raise ValueError("RTDE registers must remain in the external 24..47 range")
        if len(values) != len(set(values)):
            raise ValueError("registers within one RTDE direction/type must be unique")
    if payload.get("heartbeat_timeout_ticks") != 10:
        raise ValueError("heartbeat timeout must remain 10 controller ticks (20 ms)")
    if payload.get("zero_torque_startup_ticks") != 5:
        raise ValueError("direct torque must retain five zero-wrench startup ticks")
    if not payload.get("torque_backend_exclusive"):
        raise ValueError("direct torque bundle requires an exclusive lease")
    if payload.get("sequence_policy") != (
        "coherent before/after read, heartbeat equals sequence, exact +1 advancement; "
        "frozen/gap/mixed packet fails closed"
    ):
        raise ValueError("controller packet sequence policy drifted")
    limits = payload.get("limits", {})
    if limits != DIRECT_TORQUE_MANIFEST_LIMITS:
        raise ValueError(
            "controller-side direct-torque limits drifted from the URScript/oracle contract"
        )
    return payload


def torque_formula_reference(
    jacobian: Sequence[Sequence[float]],
    wrench_command: Sequence[float],
    coriolis: Sequence[float],
    joint_velocity: Sequence[float],
    joint_damping: Sequence[float],
) -> tuple[float, ...]:
    """Host-side oracle for URSim comparison; never a runtime fallback."""

    if len(jacobian) != 6 or any(len(row) != 6 for row in jacobian):
        raise ValueError("jacobian must be 6x6")
    if not all(len(values) == 6 for values in (wrench_command, coriolis, joint_velocity, joint_damping)):
        raise ValueError("torque formula vectors must have six values")
    result = []
    for joint in range(6):
        value = float(coriolis[joint]) - float(joint_damping[joint]) * float(
            joint_velocity[joint]
        )
        value += sum(
            float(jacobian[axis][joint]) * float(wrench_command[axis])
            for axis in range(6)
        )
        if not math.isfinite(value):
            raise ValueError("torque formula produced non-finite output")
        result.append(value)
    return tuple(result)
