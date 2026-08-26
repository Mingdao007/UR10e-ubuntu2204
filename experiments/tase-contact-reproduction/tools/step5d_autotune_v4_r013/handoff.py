"""Typed State21 -> State25 handoff contract for R013.

The contact-search branch is owned by the resident TP program.  This module
only describes the host outer-loop boundary after baseline has been reached:
State21 may update the baseline controller, but it must not start the PATH
clock or reset the normal-axis state.  The first real State25 tick carries the
last baseline normal state and starts tangential/orientation PATH at t=0.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from step5d_paper_outer_loop import Step5dOuterLoopState


FREEZE_CARRY_V1 = "freeze_carry_v1"
BLIND_RESET_V0 = "blind_reset_v0"
HANDOFF_SCHEMA = "step5d.autotune-v4/r013-freeze-carry-handoff-v1"
HANDOFF_POLICY_SCHEMA = "step5d.autotune-v4/r013-handoff-policy-v1"
HANDOFF_POLICY_VERSION = 1
HANDOFF_SELECTION_RECEIPT_SEPARATOR = "|selection_receipt_sha256="
_CONTINUITY_TOLERANCE = 1e-12


class HandoffError(RuntimeError):
    """The State21 -> State25 handoff contract is inconsistent."""


@dataclass(frozen=True)
class HandoffPolicy:
    """Typed, versioned policy identity installed into one runtime process."""

    policy: str = FREEZE_CARRY_V1
    schema: str = HANDOFF_POLICY_SCHEMA
    version: int = HANDOFF_POLICY_VERSION

    def __post_init__(self) -> None:
        if (
            self.schema != HANDOFF_POLICY_SCHEMA
            or type(self.version) is not int
            or self.version != HANDOFF_POLICY_VERSION
        ):
            raise HandoffError("R013 handoff policy schema/version differs")
        if self.policy not in {BLIND_RESET_V0, FREEZE_CARRY_V1}:
            raise HandoffError(f"unsupported handoff policy: {self.policy!r}")

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "policy": self.policy}


def validate_handoff_policy(value: HandoffPolicy | Mapping[str, Any] | str | None) -> HandoffPolicy:
    if value is None:
        raise HandoffError("R013 handoff policy selection is required")
    if isinstance(value, HandoffPolicy):
        return value
    if isinstance(value, str):
        return HandoffPolicy(policy=value)
    if not isinstance(value, Mapping):
        raise HandoffError("R013 handoff policy must be typed")
    required = {"schema", "version", "policy"}
    if set(value) != required:
        raise HandoffError("R013 handoff policy fields differ")
    return HandoffPolicy(policy=value["policy"], schema=value["schema"], version=value["version"])


def handoff_policy_identity(value: str) -> str:
    """Return the selected policy limb from a receipt-bound fingerprint value."""

    if type(value) is not str or not value.strip():
        raise HandoffError("R013 handoff policy identity is incomplete")
    return value.split(HANDOFF_SELECTION_RECEIPT_SEPARATOR, 1)[0]


def bind_handoff_policy_identity(policy: HandoffPolicy | str, receipt_sha256: str) -> str:
    """Bind a completed typed A/B-selection receipt into the fingerprint limb."""

    parsed = policy if isinstance(policy, HandoffPolicy) else HandoffPolicy(policy=policy)
    if (
        type(receipt_sha256) is not str
        or len(receipt_sha256) != 64
        or any(character not in "0123456789abcdef" for character in receipt_sha256)
    ):
        raise HandoffError("R013 handoff selection receipt identity is invalid")
    return f"{parsed.policy}{HANDOFF_SELECTION_RECEIPT_SEPARATOR}{receipt_sha256}"


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HandoffError(f"{name} is not finite") from exc
    if not math.isfinite(number):
        raise HandoffError(f"{name} is not finite")
    return number


@dataclass(frozen=True)
class OuterStateSnapshot:
    """Only the state that can create a normal-force handoff transient."""

    force_integral_n_s: float
    xdot_p_prev_m_s: tuple[float, float, float]

    @classmethod
    def from_state(cls, state: Step5dOuterLoopState) -> "OuterStateSnapshot":
        if not isinstance(state, Step5dOuterLoopState):
            raise HandoffError("outer-loop state is not typed")
        velocity = tuple(_finite(value, "xdot_p_prev_m_s") for value in state.xdot_p_prev_m_s)
        if len(velocity) != 3:
            raise HandoffError("xdot_p_prev_m_s must have length three")
        return cls(
            force_integral_n_s=_finite(state.force_integral_n_s, "force_integral_n_s"),
            xdot_p_prev_m_s=velocity,  # type: ignore[arg-type]
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "force_integral_n_s": self.force_integral_n_s,
            "xdot_p_prev_m_s": list(self.xdot_p_prev_m_s),
            "normal_velocity_m_s": self.xdot_p_prev_m_s[2],
        }


@dataclass
class FreezeCarryHandoff:
    """Small state machine that makes the handoff observable and testable."""

    policy: str = FREEZE_CARRY_V1
    phase: str = "BASELINE"
    baseline_update_count: int = 0
    path_update_count: int = 0
    path_clock_start_count: int = 0
    baseline_last_state: OuterStateSnapshot | None = None
    path_entry_state: OuterStateSnapshot | None = None
    path_first_input_state: OuterStateSnapshot | None = None
    path_first_time_s: float | None = None
    first_path_tick_observed: bool = False
    tangential_orientation_started_with_path: bool = False

    def __post_init__(self) -> None:
        if self.policy != FREEZE_CARRY_V1:
            raise HandoffError(f"unsupported handoff policy: {self.policy!r}")

    def reset_for_baseline(self) -> None:
        """Start a fresh handoff episode without changing controller state."""

        self.phase = "BASELINE"
        self.baseline_update_count = 0
        self.path_update_count = 0
        self.path_clock_start_count = 0
        self.baseline_last_state = None
        self.path_entry_state = None
        self.path_first_input_state = None
        self.path_first_time_s = None
        self.first_path_tick_observed = False
        self.tangential_orientation_started_with_path = False

    def observe_baseline_update(
        self,
        state_before: Step5dOuterLoopState,
        state_after: Step5dOuterLoopState,
    ) -> None:
        """Record baseline state evolution; no PATH clock is started here."""

        if self.phase == "PATH":
            # A mature writer can call a hold/baseline helper while unwinding
            # an exception.  Keep the already completed PATH receipt intact;
            # the owner will fail the attempt from its lifecycle gate if this
            # is a real motion sequence.
            return
        OuterStateSnapshot.from_state(state_before)
        self.baseline_last_state = OuterStateSnapshot.from_state(state_after)
        self.baseline_update_count += 1
        self.phase = "BASELINE"

    def begin_path(self, state_before: Step5dOuterLoopState) -> None:
        """Mark PATH entry without resetting the carried normal state."""

        if self.phase == "PATH":
            return
        self.path_entry_state = OuterStateSnapshot.from_state(state_before)
        self.phase = "PATH"
        self.path_clock_start_count += 1

    def observe_path_update(
        self,
        state_before: Step5dOuterLoopState,
        state_after: Step5dOuterLoopState,
        *,
        path_time_s: float,
    ) -> None:
        """Record the first and subsequent PATH outer-loop updates."""

        if self.phase != "PATH":
            self.begin_path(state_before)
        before = OuterStateSnapshot.from_state(state_before)
        OuterStateSnapshot.from_state(state_after)
        path_time = _finite(path_time_s, "path_time_s")
        if path_time < 0.0:
            raise HandoffError("path_time_s is negative")
        if not self.first_path_tick_observed:
            self.path_first_input_state = before
            self.path_first_time_s = path_time
            self.first_path_tick_observed = True
            self.tangential_orientation_started_with_path = path_time <= _CONTINUITY_TOLERANCE
        self.path_update_count += 1

    @staticmethod
    def _delta(left: OuterStateSnapshot | None, right: OuterStateSnapshot | None) -> dict[str, float | None]:
        if left is None or right is None:
            return {
                "integral_delta_n_s": None,
                "normal_velocity_delta_m_s": None,
            }
        return {
            "integral_delta_n_s": right.force_integral_n_s - left.force_integral_n_s,
            "normal_velocity_delta_m_s": (
                right.xdot_p_prev_m_s[2] - left.xdot_p_prev_m_s[2]
            ),
        }

    def receipt(self) -> dict[str, Any]:
        continuity = self._delta(self.path_entry_state, self.path_first_input_state)
        integral_delta = continuity["integral_delta_n_s"]
        velocity_delta = continuity["normal_velocity_delta_m_s"]
        continuity_ok = bool(
            self.baseline_last_state is not None
            and self.first_path_tick_observed
            and integral_delta is not None
            and velocity_delta is not None
            and abs(float(integral_delta)) <= _CONTINUITY_TOLERANCE
            and abs(float(velocity_delta)) <= _CONTINUITY_TOLERANCE
            and self.path_first_time_s is not None
            and self.path_first_time_s <= _CONTINUITY_TOLERANCE
        )
        return {
            "schema": HANDOFF_SCHEMA,
            "policy_schema": HANDOFF_POLICY_SCHEMA,
            "policy_version": HANDOFF_POLICY_VERSION,
            "policy": self.policy,
            "handoff_mode": "freeze_carry",
            "reset_at_path_entry": False,
            "blind_reset_observed": False,
            "phase": self.phase,
            "baseline_update_count": self.baseline_update_count,
            "path_update_count": self.path_update_count,
            "path_clock_start_count": self.path_clock_start_count,
            "path_clock_started_in_baseline": False,
            "baseline_observed": self.baseline_last_state is not None,
            "first_path_tick_observed": self.first_path_tick_observed,
            "first_path_time_s": self.path_first_time_s,
            "tangential_orientation_started_with_path": self.tangential_orientation_started_with_path,
            "baseline_last_state": (
                None if self.baseline_last_state is None else self.baseline_last_state.as_dict()
            ),
            "path_entry_state": (
                None if self.path_entry_state is None else self.path_entry_state.as_dict()
            ),
            "path_first_input_state": (
                None
                if self.path_first_input_state is None
                else self.path_first_input_state.as_dict()
            ),
            "integral_carry_delta_n_s": integral_delta,
            "normal_velocity_carry_delta_m_s": velocity_delta,
            "continuity_ok": continuity_ok,
            "status": "complete" if continuity_ok else "incomplete",
        }


@dataclass
class BlindResetHandoff(FreezeCarryHandoff):
    """Explicit A/B policy that resets the outer normal state at PATH entry."""

    policy: str = BLIND_RESET_V0

    def __post_init__(self) -> None:
        if self.policy != BLIND_RESET_V0:
            raise HandoffError(f"unsupported handoff policy: {self.policy!r}")

    def receipt(self) -> dict[str, Any]:
        value = super().receipt()
        value.update(
            {
                "handoff_mode": "blind_reset",
                "reset_at_path_entry": True,
                "blind_reset_observed": True,
                "continuity_ok": False,
                "status": "complete" if self.first_path_tick_observed else "incomplete",
            }
        )
        return value


def make_handoff(policy: HandoffPolicy | Mapping[str, Any] | str | None) -> FreezeCarryHandoff:
    parsed = validate_handoff_policy(policy)
    return (
        BlindResetHandoff(policy=parsed.policy)
        if parsed.policy == BLIND_RESET_V0
        else FreezeCarryHandoff(policy=parsed.policy)
    )


__all__ = [
    "BLIND_RESET_V0",
    "FREEZE_CARRY_V1",
    "HANDOFF_SCHEMA",
    "HANDOFF_POLICY_SCHEMA",
    "HANDOFF_POLICY_VERSION",
    "HANDOFF_SELECTION_RECEIPT_SEPARATOR",
    "BlindResetHandoff",
    "FreezeCarryHandoff",
    "HandoffPolicy",
    "HandoffError",
    "OuterStateSnapshot",
    "make_handoff",
    "bind_handoff_policy_identity",
    "handoff_policy_identity",
    "validate_handoff_policy",
]
