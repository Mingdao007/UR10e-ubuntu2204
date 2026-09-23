"""Offline TASE/RNN plus tangential SFC composition.

This module is deliberately transport-free. It provides the mathematical
projection contract used by offline replays; it does not import a device,
bridge, live writer, TP package, or native solver. A composed method passes its
combined Cartesian task to its selected TASE RNN or QP solver exactly once.
The standalone :class:`FinalBoundedJointVelocityQP` remains a bounded offline
realization diagnostic and is not inserted after those solvers.

The normal task owns force and posture.  SFC is projected into the tangent
plane and is disabled outside PATH.  The conditional integral clamp is kept
as an explicit state machine so its freeze, unwind, and reset decisions are
replayable and can be sealed in an offline receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import math
from typing import Any, Mapping

import numpy as np


COMPOSITION_ID = "TASE_NORMAL_ORIENTATION+SFC_TANGENTIAL_V1"
CLAMP_POLICY_ID = "conditional-double-clamp-v1"
CLAMP_STATE_LIMIT_N_S = 1.0
CLAMP_AUTHORITY_LIMIT_N = 0.5
NORMAL_JUMP_THRESHOLD_RAD = math.radians(30.0)
RESET_BOUNDARIES = (
    "candidate_dispatch",
    "contact_latch",
    "path_entry",
    "contact_loss",
    "abort",
    "invalid_state",
    "normal_jump",
    "mode_exit",
    "unload",
    "home",
)
SATURATION_SOURCES = (
    "velocity",
    "qp",
    "slew",
    "joint",
    "tp",
    "final_actuator",
)


class FusionError(ValueError):
    """Invalid offline fusion input or state transition."""


def _finite_vector(value: Any, size: int, name: str) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FusionError(f"{name} must be a finite vector of shape ({size},)") from exc
    if array.shape != (size,) or not np.isfinite(array).all():
        raise FusionError(f"{name} must be a finite vector of shape ({size},)")
    return array.copy()


def _finite_matrix(value: Any, shape: tuple[int, int], name: str) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FusionError(f"{name} must be a finite matrix of shape {shape}") from exc
    if array.shape != shape or not np.isfinite(array).all():
        raise FusionError(f"{name} must be a finite matrix of shape {shape}")
    return array.copy()


def _unit_normal(value: Any, name: str = "normal") -> np.ndarray:
    vector = _finite_vector(value, 3, name)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise FusionError(f"{name} must have nonzero norm")
    return vector / norm


@dataclass(frozen=True)
class NormalTangentProjectors:
    """Orthogonal projectors for one online unit normal."""

    normal: tuple[float, float, float]
    Pn: np.ndarray
    Pt: np.ndarray

    def project_normal(self, vector: Any) -> np.ndarray:
        return self.Pn @ _finite_vector(vector, 3, "vector")

    def project_tangent(self, vector: Any) -> np.ndarray:
        return self.Pt @ _finite_vector(vector, 3, "vector")


def normal_tangent_projectors(normal: Any) -> NormalTangentProjectors:
    """Return ``Pn=n n.T`` and ``Pt=I-Pn`` for an online normal estimate."""

    unit = _unit_normal(normal)
    pn = np.outer(unit, unit)
    pt = np.eye(3) - pn
    # Do not let callers mutate a projector stored in a receipt or controller
    # state.  ``project_normal`` and ``project_tangent`` only read these arrays.
    pn.setflags(write=False)
    pt.setflags(write=False)
    return NormalTangentProjectors(tuple(float(x) for x in unit), pn, pt)


def _minimal_rotation(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Deterministic shortest rotation taking one unit vector to another."""

    cross = np.cross(source, target)
    sine = float(np.linalg.norm(cross))
    cosine = float(np.clip(np.dot(source, target), -1.0, 1.0))
    if sine <= 1e-12:
        if cosine >= 0.0:
            return np.eye(3)
        # Antipodal normals have no unique shortest rotation.  Select a
        # deterministic axis orthogonal to source and rotate by pi.
        axis_seed = np.eye(3)[int(np.argmin(np.abs(source)))]
        axis = axis_seed - source * float(np.dot(axis_seed, source))
        axis /= float(np.linalg.norm(axis))
        skew = np.array(
            [[0.0, -axis[2], axis[1]],
             [axis[2], 0.0, -axis[0]],
             [-axis[1], axis[0], 0.0]],
            dtype=float,
        )
        return -np.eye(3) + 2.0 * np.outer(axis, axis)
    axis = cross / sine
    skew = np.array(
        [[0.0, -axis[2], axis[1]],
         [axis[2], 0.0, -axis[0]],
         [-axis[1], axis[0], 0.0]],
        dtype=float,
    )
    # Rodrigues with sin(theta)=sine and cos(theta)=cosine.
    return np.eye(3) + sine * skew + (1.0 - cosine) * (skew @ skew)


def _initial_tangent_basis(normal: np.ndarray, reference_axis: Any | None = None) -> np.ndarray:
    if reference_axis is not None:
        axis = _finite_vector(reference_axis, 3, "reference_axis")
        if float(np.linalg.norm(axis)) <= 1e-12:
            raise FusionError("reference_axis must have nonzero norm")
    else:
        axis = np.eye(3)[int(np.argmin(np.abs(normal)))]
    first = axis - normal * float(np.dot(axis, normal))
    first_norm = float(np.linalg.norm(first))
    if first_norm <= 1e-12:
        raise FusionError("reference_axis is parallel to normal")
    first /= first_norm
    second = np.cross(normal, first)
    second /= float(np.linalg.norm(second))
    return np.column_stack((first, second))


class TransportedTangentBasis:
    """Continuously transport a two-vector tangent basis with the normal."""

    def __init__(self, normal: Any, *, reference_axis: Any | None = None):
        self._normal = _unit_normal(normal)
        self._basis = _initial_tangent_basis(self._normal, reference_axis)
        self._updates = 0

    @property
    def normal(self) -> np.ndarray:
        return self._normal.copy()

    @property
    def basis(self) -> np.ndarray:
        return self._basis.copy()

    @property
    def updates(self) -> int:
        return self._updates

    def update(self, normal: Any) -> np.ndarray:
        new_normal = _unit_normal(normal)
        rotation = _minimal_rotation(self._normal, new_normal)
        candidate = rotation @ self._basis

        # Re-orthogonalize after transport.  Sign choices maximize alignment
        # with the transported columns, avoiding arbitrary tangent flips when
        # the estimated normal changes slowly.
        first = candidate[:, 0] - new_normal * float(np.dot(candidate[:, 0], new_normal))
        if float(np.linalg.norm(first)) <= 1e-12:
            first = candidate[:, 1] - new_normal * float(np.dot(candidate[:, 1], new_normal))
        first /= float(np.linalg.norm(first))
        if float(np.dot(first, candidate[:, 0])) < 0.0:
            first = -first
        second = candidate[:, 1] - new_normal * float(np.dot(candidate[:, 1], new_normal))
        second -= first * float(np.dot(second, first))
        if float(np.linalg.norm(second)) <= 1e-12:
            second = np.cross(new_normal, first)
        second /= float(np.linalg.norm(second))
        if float(np.dot(second, candidate[:, 1])) < 0.0:
            second = -second
        self._normal = new_normal
        self._basis = np.column_stack((first, second))
        self._updates += 1
        return self.basis

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "transported-tangent-basis-v1",
            "normal": self._normal.tolist(),
            "basis": self._basis.tolist(),
            "updates": self._updates,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        if state.get("schema") != "transported-tangent-basis-v1":
            raise FusionError("tangent-basis snapshot schema differs")
        normal = _unit_normal(state.get("normal"), "snapshot normal")
        basis = _finite_matrix(state.get("basis"), (3, 2), "snapshot basis")
        if not np.allclose(basis.T @ basis, np.eye(2), atol=1e-9, rtol=0.0):
            raise FusionError("snapshot tangent basis is not orthonormal")
        if not np.allclose(basis.T @ normal, np.zeros(2), atol=1e-9, rtol=0.0):
            raise FusionError("snapshot tangent basis is not tangent")
        updates = state.get("updates")
        if isinstance(updates, bool) or not isinstance(updates, int) or updates < 0:
            raise FusionError("snapshot basis update count is invalid")
        self._normal, self._basis, self._updates = normal, basis, updates


@dataclass(frozen=True)
class FusionResult:
    """Cartesian result before the single joint-velocity realization."""

    twist: tuple[float, ...]
    tase_normal_twist: tuple[float, ...]
    tase_tangent_shadow: tuple[float, ...]
    sfc_tangent_twist: tuple[float, ...]
    sfc_enabled: bool
    phase: str
    diagnostics: dict[str, Any]


class TaseSfcFusion:
    """Compose TASE normal/orientation with an optional tangent SFC output."""

    def __init__(
        self,
        normal: Any,
        *,
        normal_jump_threshold_rad: float = NORMAL_JUMP_THRESHOLD_RAD,
    ):
        threshold = float(normal_jump_threshold_rad)
        if not math.isfinite(threshold) or not 0.0 < threshold <= math.pi:
            raise FusionError("normal_jump_threshold_rad must be in (0, pi]")
        self._basis = TransportedTangentBasis(normal)
        self._normal_jump_threshold_rad = threshold
        self._phase = "idle"
        self._sfc_enabled = False
        self._sfc_frozen = False
        self._sfc_state = np.zeros(2, dtype=float)
        self._reset_events: list[str] = []

    @property
    def composition_id(self) -> str:
        return COMPOSITION_ID

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def sfc_enabled(self) -> bool:
        return self._sfc_enabled

    @property
    def sfc_state(self) -> np.ndarray:
        return self._sfc_state.copy()

    @property
    def tangent_basis(self) -> np.ndarray:
        return self._basis.basis

    @property
    def reset_events(self) -> tuple[str, ...]:
        return tuple(self._reset_events)

    def set_phase(self, phase: str) -> None:
        if not isinstance(phase, str) or not phase:
            raise FusionError("phase must be a nonempty string")
        normalized = phase.lower()
        was_path = self._phase.lower() == "path"
        is_path = normalized == "path"
        if not is_path:
            if was_path or self._sfc_enabled or np.any(self._sfc_state):
                self._reset_events.append("mode_exit")
            self._sfc_enabled = False
            self._sfc_state.fill(0.0)
        elif not was_path:
            self._sfc_enabled = True
            self._sfc_state.fill(0.0)
            self._sfc_frozen = False
            self._reset_events.append("path_entry")
        self._phase = phase

    def reset(self, boundary: str = "abort") -> None:
        if boundary not in RESET_BOUNDARIES:
            raise FusionError(f"unknown fusion reset boundary {boundary!r}")
        self._sfc_enabled = False
        self._sfc_frozen = False
        self._sfc_state.fill(0.0)
        self._reset_events.append(boundary)
        self._phase = "reset"

    def update_sfc_state(self, value: Any) -> np.ndarray:
        if not self._sfc_enabled:
            self._sfc_state.fill(0.0)
            return self.sfc_state
        self._sfc_state = _finite_vector(value, 2, "sfc_state")
        return self.sfc_state

    def fuse(
        self,
        tase_twist: Any,
        sfc_twist: Any,
        normal: Any,
        *,
        phase: str | None = None,
    ) -> FusionResult:
        if phase is not None:
            self.set_phase(phase)
        try:
            projectors = normal_tangent_projectors(normal)
            tase = _finite_vector(tase_twist, 6, "tase_twist")
            sfc = _finite_vector(sfc_twist, 6, "sfc_twist")
        except FusionError:
            self._sfc_enabled = False
            self._sfc_frozen = True
            self._sfc_state.fill(0.0)
            self._reset_events.append("invalid_state")
            raise
        previous_normal = self._basis.normal
        normal_jump_angle = math.acos(
            float(np.clip(np.dot(previous_normal, np.asarray(projectors.normal)), -1.0, 1.0))
        )
        normal_jump = normal_jump_angle > self._normal_jump_threshold_rad
        if normal_jump:
            # Keep the TASE normal/orientation command available, but freeze
            # SFC until an explicit mode transition or reset.  This prevents
            # a discontinuous online normal from injecting a tangent command.
            self._sfc_enabled = False
            self._sfc_frozen = True
            self._sfc_state.fill(0.0)
            self._reset_events.append("normal_jump")
        self._basis.update(np.asarray(projectors.normal, dtype=float))
        tase_normal_linear = projectors.Pn @ tase[:3]
        tase_tangent_linear = projectors.Pt @ tase[:3]
        sfc_tangent_linear = projectors.Pt @ sfc[:3] if self._sfc_enabled else np.zeros(3)
        combined = np.concatenate((tase_normal_linear + sfc_tangent_linear, tase[3:]))
        # SFC has tangent path authority only.  Its angular and normal pieces
        # are deliberately ignored; they cannot perturb the TASE normal or
        # approved orientation policy.
        return FusionResult(
            twist=tuple(float(x) for x in combined),
            tase_normal_twist=tuple(float(x) for x in np.concatenate((tase_normal_linear, tase[3:]))),
            tase_tangent_shadow=tuple(float(x) for x in tase_tangent_linear),
            sfc_tangent_twist=tuple(float(x) for x in np.concatenate((sfc_tangent_linear, np.zeros(3)))),
            sfc_enabled=self._sfc_enabled,
            phase=self._phase,
            diagnostics={
                "schema": "tase-sfc-fusion-result-v1",
                "composition_id": COMPOSITION_ID,
                "normal": list(projectors.normal),
                "Pn": projectors.Pn.tolist(),
                "Pt": projectors.Pt.tolist(),
                "tase_tangent_zeroed": True,
                "sfc_normal_component_zeroed": True,
                "sfc_angular_component_zeroed": True,
                "normal_priority": "TASE",
                "sfc_enabled": self._sfc_enabled,
                "normal_jump": normal_jump,
                "normal_jump_threshold_rad": self._normal_jump_threshold_rad,
                "sfc_frozen": self._sfc_frozen,
            },
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "tase-sfc-fusion-state-v1",
            "composition_id": COMPOSITION_ID,
            "phase": self._phase,
            "sfc_enabled": self._sfc_enabled,
            "sfc_frozen": self._sfc_frozen,
            "normal_jump_threshold_rad": self._normal_jump_threshold_rad,
            "sfc_state": self._sfc_state.tolist(),
            "basis": self._basis.snapshot(),
            "reset_events": list(self._reset_events),
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        if state.get("schema") != "tase-sfc-fusion-state-v1" or state.get("composition_id") != COMPOSITION_ID:
            raise FusionError("fusion snapshot identity differs")
        phase = state.get("phase")
        if not isinstance(phase, str) or not phase:
            raise FusionError("fusion snapshot phase is invalid")
        enabled = state.get("sfc_enabled")
        if type(enabled) is not bool:
            raise FusionError("fusion snapshot SFC enable flag is invalid")
        sfc_state = _finite_vector(state.get("sfc_state"), 2, "snapshot sfc_state")
        frozen = state.get("sfc_frozen", False)
        if type(frozen) is not bool:
            raise FusionError("fusion snapshot SFC frozen flag is invalid")
        threshold = float(state.get("normal_jump_threshold_rad", self._normal_jump_threshold_rad))
        if not math.isclose(threshold, self._normal_jump_threshold_rad, rel_tol=0.0, abs_tol=1e-12):
            raise FusionError("fusion snapshot normal jump threshold differs")
        events = state.get("reset_events")
        if not isinstance(events, list) or not all(isinstance(item, str) for item in events):
            raise FusionError("fusion snapshot reset events are invalid")
        self._basis.restore(state.get("basis"))
        self._phase, self._sfc_enabled, self._sfc_state = phase, enabled, sfc_state
        self._sfc_frozen = frozen
        self._reset_events = list(events)


def _source_key(source: str) -> str:
    key = str(source).lower().replace("_saturation", "").replace("-", "_").replace(" ", "_")
    aliases = {
        "velocity": "velocity",
        "qp": "qp",
        "slew": "slew",
        "joint": "joint",
        "tp": "tp",
        "final_actuator": "final_actuator",
        "actuator": "final_actuator",
    }
    return aliases.get(key, key)


def _source_direction(value: Any) -> tuple[bool, float | None]:
    if isinstance(value, Mapping):
        active = value.get("active", value.get("saturated", False))
        if type(active) is not bool:
            raise FusionError("saturation active flag must be bool")
        direction = value.get("direction", value.get("signed", None))
        if direction is None:
            return active, None
        signed = float(direction)
        if not math.isfinite(signed):
            raise FusionError("saturation direction must be finite")
        return active, signed
    if type(value) is bool:
        return value, None
    signed = float(value)
    if not math.isfinite(signed):
        raise FusionError("saturation direction must be finite")
    return abs(signed) > 0.0, signed


@dataclass(frozen=True)
class ClampStep:
    state_n_s: float
    integral_term_n: float
    effective_limit_n_s: float
    frozen: bool
    freeze_reason: str | None
    unwind: bool
    downstream_saturation_source: str | None
    diagnostics: dict[str, Any]


class ConditionalDoubleClamp:
    """Conditional integrator clamp with explicit downstream saturation evidence."""

    def __init__(
        self,
        *,
        kp: float = 4.0,
        ki: float = 1.0,
        state_limit_n_s: float = CLAMP_STATE_LIMIT_N_S,
        authority_limit_n: float = CLAMP_AUTHORITY_LIMIT_N,
    ):
        self.kp = float(kp)
        self.ki = float(ki)
        self.state_limit_n_s = float(state_limit_n_s)
        self.authority_limit_n = float(authority_limit_n)
        if not all(math.isfinite(x) and x > 0.0 for x in (self.kp, self.ki, self.state_limit_n_s, self.authority_limit_n)):
            raise FusionError("conditional clamp gains and limits must be positive")
        self.state_n_s = 0.0
        self._last: ClampStep | None = None
        self._reset_events: list[str] = []
        self._unwind_events = 0
        self._updates = 0

    def effective_limit(self, authority_error_n: float | None = None) -> float:
        error_authority = self.authority_limit_n if authority_error_n is None else float(authority_error_n)
        if not math.isfinite(error_authority) or error_authority < 0.0:
            raise FusionError("authority_error_n must be finite and nonnegative")
        error_authority = min(error_authority, self.authority_limit_n)
        return min(self.state_limit_n_s, error_authority * self.kp / self.ki)

    def reset(self, boundary: str) -> None:
        if boundary not in RESET_BOUNDARIES:
            raise FusionError(f"unknown clamp reset boundary {boundary!r}")
        self.state_n_s = 0.0
        self._reset_events.append(boundary)
        self._last = ClampStep(
            state_n_s=0.0,
            integral_term_n=0.0,
            effective_limit_n_s=self.effective_limit(),
            frozen=False,
            freeze_reason=None,
            unwind=False,
            downstream_saturation_source=None,
            diagnostics={"reset_boundary": boundary},
        )

    def update(
        self,
        error_n: float,
        dt_s: float,
        *,
        authority_error_n: float | None = None,
        saturation_sources: Mapping[str, Any] | None = None,
        mode: str = "path",
        reset_boundary: str | None = None,
    ) -> ClampStep:
        if reset_boundary is not None:
            self.reset(reset_boundary)
            return self._last  # type: ignore[return-value]
        if not isinstance(mode, str) or not mode:
            raise FusionError("mode must be a nonempty string")
        if mode.lower() != "path":
            self.reset("mode_exit")
            return self._last  # type: ignore[return-value]
        try:
            error = float(error_n)
            dt = float(dt_s)
        except (TypeError, ValueError, OverflowError) as exc:
            self.reset("invalid_state")
            raise FusionError("clamp error and dt must be finite") from exc
        if not math.isfinite(error) or not math.isfinite(dt) or dt <= 0.0:
            self.reset("invalid_state")
            raise FusionError("clamp error must be finite and dt must be positive")
        limit = self.effective_limit(authority_error_n)
        state_was_clamped = abs(self.state_n_s) > limit + 1e-12
        if state_was_clamped:
            # A newly reduced authority bound must constrain the already
            # accumulated state before freeze/unwind decisions are reported.
            # This is a state clamp, not back-calculation from the actuator.
            self.state_n_s = float(np.clip(self.state_n_s, -limit, limit))
        source_name: str | None = None
        observed_source_name: str | None = None
        freeze_reason: str | None = None
        frozen = False
        sources = saturation_sources or {}
        if not isinstance(sources, Mapping):
            raise FusionError("saturation_sources must be a mapping")
        for raw_name, raw_value in sources.items():
            name = _source_key(str(raw_name))
            active, direction = _source_direction(raw_value)
            if not active:
                continue
            if observed_source_name is None:
                observed_source_name = name
            same_direction = direction is None or math.copysign(1.0, direction) == math.copysign(1.0, error)
            if same_direction:
                source_name = name
                freeze_reason = f"{name}_same_direction"
                frozen = True
                break

        # Keep the observed downstream source in the receipt even when its
        # direction is opposite to the force error and therefore permits
        # unwinding.  ``freeze_reason`` remains reserved for same-direction
        # saturation that actually froze the integrator.
        if source_name is None:
            source_name = observed_source_name

        unwind = False
        if not frozen:
            proposed = self.state_n_s + dt * error
            if self.state_n_s != 0.0 and math.copysign(1.0, self.state_n_s) != math.copysign(1.0, error):
                unwind = True
                self._unwind_events += 1
                # Do not integrate through zero when unwinding an old state.
                if math.copysign(1.0, proposed) != math.copysign(1.0, self.state_n_s):
                    proposed = 0.0
            self.state_n_s = float(np.clip(proposed, -limit, limit))
        integral_term = float(np.clip(self.ki * self.state_n_s, -self.authority_limit_n, self.authority_limit_n))
        self._updates += 1
        self._last = ClampStep(
            state_n_s=self.state_n_s,
            integral_term_n=integral_term,
            effective_limit_n_s=limit,
            frozen=frozen,
            freeze_reason=freeze_reason,
            unwind=unwind,
            downstream_saturation_source=source_name,
            diagnostics={
                "schema": "conditional-double-clamp-step-v1",
                "policy": CLAMP_POLICY_ID,
                "state_limit_n_s": self.state_limit_n_s,
                "authority_limit_n": self.authority_limit_n,
                "effective_limit_n_s": limit,
                "authority_error_n": self.authority_limit_n if authority_error_n is None else float(authority_error_n),
                "freeze_reason": freeze_reason,
                "unwind": unwind,
                "downstream_saturation_source": observed_source_name,
                "no_back_calculation": True,
                "state_clamped_to_effective_limit": state_was_clamped,
                "reset_boundaries": list(RESET_BOUNDARIES),
            },
        )
        return self._last

    def receipt_diagnostics(self) -> dict[str, Any]:
        last = self._last
        return {
            "schema": "conditional-double-clamp-receipt-v1",
            "policy": CLAMP_POLICY_ID,
            "state_limit_n_s": self.state_limit_n_s,
            "authority_limit_n": self.authority_limit_n,
            "effective_limit_n_s": None if last is None else last.effective_limit_n_s,
            "state_n_s": self.state_n_s,
            "integral_term_n": None if last is None else last.integral_term_n,
            "freeze_reason": None if last is None else last.freeze_reason,
            "unwind_events": self._unwind_events,
            "downstream_saturation_source": None if last is None else last.downstream_saturation_source,
            "reset_events": list(self._reset_events),
            "updates": self._updates,
            "no_back_calculation": True,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "conditional-double-clamp-state-v1",
            "kp": self.kp,
            "ki": self.ki,
            "state_limit_n_s": self.state_limit_n_s,
            "authority_limit_n": self.authority_limit_n,
            "state_n_s": self.state_n_s,
            "reset_events": list(self._reset_events),
            "unwind_events": self._unwind_events,
            "updates": self._updates,
            "last": None if self._last is None else copy.deepcopy(self._last.__dict__),
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        if state.get("schema") != "conditional-double-clamp-state-v1":
            raise FusionError("clamp snapshot schema differs")
        for name, expected in (("kp", self.kp), ("ki", self.ki), ("state_limit_n_s", self.state_limit_n_s), ("authority_limit_n", self.authority_limit_n)):
            if not math.isclose(float(state.get(name)), expected, rel_tol=0.0, abs_tol=1e-12):
                raise FusionError(f"clamp snapshot {name} differs")
        value = float(state.get("state_n_s"))
        if not math.isfinite(value) or abs(value) > self.state_limit_n_s + 1e-12:
            raise FusionError("clamp snapshot state is invalid")
        events = state.get("reset_events")
        if not isinstance(events, list) or not all(item in RESET_BOUNDARIES for item in events):
            raise FusionError("clamp snapshot reset events are invalid")
        unwind_events = state.get("unwind_events")
        updates = state.get("updates")
        if (
            isinstance(unwind_events, bool)
            or not isinstance(unwind_events, int)
            or unwind_events < 0
            or isinstance(updates, bool)
            or not isinstance(updates, int)
            or updates < 0
        ):
            raise FusionError("clamp snapshot counters are invalid")
        raw_last = state.get("last")
        restored_last: ClampStep | None
        if raw_last is None:
            restored_last = None
        else:
            if not isinstance(raw_last, Mapping):
                raise FusionError("clamp snapshot last step is invalid")
            freeze_reason = raw_last.get("freeze_reason")
            if freeze_reason is not None and not isinstance(freeze_reason, str):
                raise FusionError("clamp snapshot freeze reason is invalid")
            downstream_source = raw_last.get("downstream_saturation_source")
            if downstream_source is not None and not isinstance(downstream_source, str):
                raise FusionError("clamp snapshot saturation source is invalid")
            frozen = raw_last.get("frozen")
            unwind = raw_last.get("unwind")
            if type(frozen) is not bool or type(unwind) is not bool:
                raise FusionError("clamp snapshot boolean state is invalid")
            diagnostics = raw_last.get("diagnostics", {})
            if not isinstance(diagnostics, Mapping):
                raise FusionError("clamp snapshot diagnostics are invalid")
            last_state = float(raw_last.get("state_n_s"))
            last_integral = float(raw_last.get("integral_term_n"))
            last_limit = float(raw_last.get("effective_limit_n_s"))
            if (
                not math.isfinite(last_state)
                or abs(last_state) > self.state_limit_n_s + 1e-12
                or not math.isfinite(last_integral)
                or not math.isfinite(last_limit)
                or last_limit < 0.0
            ):
                raise FusionError("clamp snapshot last step values are invalid")
            restored_last = ClampStep(
                state_n_s=last_state,
                integral_term_n=last_integral,
                effective_limit_n_s=last_limit,
                frozen=frozen,
                freeze_reason=freeze_reason,
                unwind=unwind,
                downstream_saturation_source=downstream_source,
                diagnostics=copy.deepcopy(dict(diagnostics)),
            )
        # Commit only after every field, including the optional last step, has
        # validated. A rejected restore leaves the live state untouched.
        self.state_n_s = value
        self._reset_events = list(events)
        self._unwind_events = unwind_events
        self._updates = updates
        self._last = restored_last


@dataclass(frozen=True)
class JointVelocityRealization:
    qdot_rad_s: tuple[float, ...]
    applied_twist: tuple[float, ...]
    normal_residual: float
    tangent_residual: float
    normal_preserved: bool
    tangent_degraded: bool
    diagnostics: dict[str, Any]


class FinalBoundedJointVelocityQP:
    """The sole offline Cartesian-to-joint bounded QP realization boundary."""

    def __init__(self, *, normal_weight: float = 1_000_000.0, tangent_weight: float = 1.0, regularization: float = 1e-9):
        if not all(math.isfinite(float(x)) and float(x) > 0.0 for x in (normal_weight, tangent_weight, regularization)):
            raise FusionError("QP weights and regularization must be positive")
        self.normal_weight = float(normal_weight)
        self.tangent_weight = float(tangent_weight)
        self.regularization = float(regularization)

    def realize(
        self,
        jacobian: Any,
        *,
        normal: Any,
        normal_twist: Any,
        tangent_twist: Any,
        previous_qdot: Any,
        qdot_lower: Any,
        qdot_upper: Any,
        slew_limit: Any,
    ) -> JointVelocityRealization:
        try:
            jac = np.asarray(jacobian, dtype=float)
        except (TypeError, ValueError, OverflowError) as exc:
            raise FusionError("jacobian must be finite with shape (6, n)") from exc
        if jac.ndim != 2 or jac.shape[0] != 6 or not np.isfinite(jac).all():
            raise FusionError("jacobian must be finite with shape (6, n)")
        n_joints = jac.shape[1]
        if n_joints < 1:
            raise FusionError("jacobian must have at least one joint column")
        n = _unit_normal(normal)
        tase = _finite_vector(normal_twist, 6, "normal_twist")
        tangent = _finite_vector(tangent_twist, 6, "tangent_twist")
        previous = _finite_vector(previous_qdot, n_joints, "previous_qdot")
        lower = _finite_vector(qdot_lower, n_joints, "qdot_lower")
        upper = _finite_vector(qdot_upper, n_joints, "qdot_upper")
        slew = np.asarray(slew_limit, dtype=float)
        if slew.ndim == 0:
            slew = np.full(n_joints, float(slew))
        if slew.shape != (n_joints,) or not np.isfinite(slew).all() or np.any(slew <= 0.0):
            raise FusionError("slew_limit must be positive scalar or a finite vector")
        if np.any(lower > upper):
            raise FusionError("inverted qdot bounds")
        effective_lower = np.maximum(lower, previous - slew)
        effective_upper = np.minimum(upper, previous + slew)
        if np.any(effective_lower > effective_upper + 1e-12):
            raise FusionError("qdot and slew bounds have empty intersection")

        pn = np.outer(n, n)
        pt = np.eye(3) - pn
        normal_mask = np.zeros((6, 6), dtype=float)
        normal_mask[:3, :3] = pn
        normal_mask[3:, 3:] = np.eye(3)
        tangent_mask = np.zeros((6, 6), dtype=float)
        tangent_mask[:3, :3] = pt
        normal_target = normal_mask @ tase
        tangent_target = tangent_mask @ tangent
        # Weighted least-squares QP, solved as a deterministic box-QP by
        # coordinate descent.  The large normal weight gives lexicographic
        # normal priority while retaining a finite, inspectable objective.
        def solve_box_qp(tangent_weight: float) -> np.ndarray:
            with np.errstate(over="ignore", invalid="ignore"):
                h = (
                    self.normal_weight * (jac.T @ normal_mask @ jac)
                    + tangent_weight * (jac.T @ tangent_mask @ jac)
                    + self.regularization * np.eye(n_joints)
                )
                b = self.normal_weight * (jac.T @ normal_mask @ normal_target) + tangent_weight * (jac.T @ tangent_mask @ tangent_target)
            try:
                candidate = np.linalg.solve(h, b)
            except np.linalg.LinAlgError as exc:
                raise FusionError("final QP Hessian is singular") from exc
            if not np.isfinite(candidate).all():
                raise FusionError("final QP produced a nonfinite joint velocity")
            candidate = np.clip(candidate, effective_lower, effective_upper)
            if not np.isfinite(candidate).all():
                raise FusionError("final QP clipping produced a nonfinite joint velocity")
            for _ in range(512):
                before = candidate.copy()
                for index in range(n_joints):
                    diagonal = float(h[index, index])
                    if diagonal <= 0.0 or not math.isfinite(diagonal):
                        raise FusionError("final QP Hessian has invalid diagonal")
                    other = float(b[index] - (h[index] @ candidate - diagonal * candidate[index]))
                    candidate[index] = float(np.clip(other / diagonal, effective_lower[index], effective_upper[index]))
                    if not math.isfinite(candidate[index]):
                        raise FusionError("final QP iteration produced a nonfinite joint velocity")
                if float(np.max(np.abs(candidate - before))) <= 1e-12:
                    break
            return candidate

        # First establish the best achievable normal command with tangent
        # weight zero. Tangent optimization may use that feasible normal
        # manifold, but it is never allowed to trade normal error for tangent
        # progress merely because the normal weight is finite.
        normal_candidate = solve_box_qp(0.0)
        normal_only_applied = jac @ normal_candidate
        if not np.isfinite(normal_only_applied).all():
            raise FusionError("final QP produced a nonfinite normal twist")
        normal_only_residual = float(np.linalg.norm(normal_mask @ (normal_only_applied - normal_target)))
        normal_request_norm = float(np.linalg.norm(normal_target))
        if not math.isfinite(normal_only_residual) or not math.isfinite(normal_request_norm):
            raise FusionError("final QP normal residual is nonfinite")
        normal_tolerance = max(1e-9, 1e-6 * max(1.0, normal_request_norm))
        if normal_only_residual > normal_tolerance:
            raise FusionError(
                "final QP cannot preserve the TASE normal command within qdot/slew bounds"
            )
        candidate = solve_box_qp(self.tangent_weight)
        applied = jac @ candidate
        if not np.isfinite(applied).all():
            raise FusionError("final QP produced a nonfinite applied twist")
        normal_residual = float(np.linalg.norm(normal_mask @ (applied - normal_target)))
        tangent_residual = float(np.linalg.norm(tangent_mask @ (applied - tangent_target)))
        if not math.isfinite(normal_residual) or not math.isfinite(tangent_residual):
            raise FusionError("final QP residual is nonfinite")
        normal_preserved = normal_residual <= normal_tolerance
        normal_solver_fallback = False
        if not normal_preserved:
            candidate = normal_candidate
            applied = normal_only_applied
            normal_residual = normal_only_residual
            tangent_residual = float(np.linalg.norm(tangent_mask @ (applied - tangent_target)))
            if not math.isfinite(tangent_residual):
                raise FusionError("final QP fallback residual is nonfinite")
            normal_preserved = True
            normal_solver_fallback = True
        if not np.isfinite(candidate).all() or not np.all(
            (candidate >= effective_lower - 1e-12)
            & (candidate <= effective_upper + 1e-12)
        ):
            raise FusionError("final QP candidate violates qdot or slew bounds")
        tangent_degraded = tangent_residual > max(1e-9, 1e-6 * max(1.0, float(np.linalg.norm(tangent_target))))
        diagnostics = {
            "schema": "final-bounded-joint-velocity-qp-v1",
            "composition_id": COMPOSITION_ID,
            "normal_priority": True,
            "normal_priority_mode": "hard_feasible_then_tangent",
            "normal_weight": self.normal_weight,
            "tangent_weight": self.tangent_weight,
            "qdot_lower": effective_lower.tolist(),
            "qdot_upper": effective_upper.tolist(),
            "slew_limit": slew.tolist(),
            "slew_intersection_applied": bool(np.any(effective_lower != lower) or np.any(effective_upper != upper)),
            "qdot_bounds_satisfied": bool(np.all(candidate >= lower - 1e-12) and np.all(candidate <= upper + 1e-12)),
            "slew_bounds_satisfied": bool(np.all(np.abs(candidate - previous) <= slew + 1e-12)),
            "tangent_degraded": tangent_degraded,
            "normal_solver_fallback": normal_solver_fallback,
            "single_realization_interface": True,
        }
        return JointVelocityRealization(
            qdot_rad_s=tuple(float(x) for x in candidate),
            applied_twist=tuple(float(x) for x in applied),
            normal_residual=normal_residual,
            tangent_residual=tangent_residual,
            normal_preserved=normal_preserved,
            tangent_degraded=tangent_degraded,
            diagnostics=diagnostics,
        )


__all__ = [
    "COMPOSITION_ID",
    "CLAMP_POLICY_ID",
    "CLAMP_STATE_LIMIT_N_S",
    "CLAMP_AUTHORITY_LIMIT_N",
    "NORMAL_JUMP_THRESHOLD_RAD",
    "RESET_BOUNDARIES",
    "SATURATION_SOURCES",
    "FusionError",
    "NormalTangentProjectors",
    "normal_tangent_projectors",
    "TransportedTangentBasis",
    "FusionResult",
    "TaseSfcFusion",
    "ClampStep",
    "ConditionalDoubleClamp",
    "JointVelocityRealization",
    "FinalBoundedJointVelocityQP",
]
