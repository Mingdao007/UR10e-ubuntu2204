"""Replaceable V4 behavior policies behind one explicit composition contract.

The policies produce bounded behavior.  :class:`V4InvariantEnvelope` remains
outside every provider and is therefore not replaceable by an optimizer or an
alternate algorithm implementation.
"""

from __future__ import annotations

import hashlib
import inspect
import math
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable

from .baseline import (
    BaselineCommand,
    BaselineObservation,
    BaselineState,
    step_baseline,
)
from .contracts import TARGET_FORCE_N, V4Candidate, V4Contract, assert_runtime_target
from .entry import EntrySegment, plan_entry
from .runtime import (
    KinematicGateResult,
    RuntimeGuardError,
    StartupHeartbeatGate,
    TimingGuard,
    gate_qdot,
)


ZERO_QDOT = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _implementation_sha256(value: object) -> str:
    try:
        source = inspect.getsource(value.__class__).encode("utf-8")
    except (OSError, TypeError):
        source = (
            f"{value.__class__.__module__}:{value.__class__.__qualname__}:"
            f"{getattr(value, 'implementation_id', '')}"
        ).encode("utf-8")
    return hashlib.sha256(source).hexdigest()


@runtime_checkable
class EntryPolicy(Protocol):
    implementation_id: str

    def plan(self, current_pose: Sequence[float]) -> tuple[EntrySegment, ...]: ...


@runtime_checkable
class BaselinePolicy(Protocol):
    implementation_id: str

    def step(
        self,
        candidate: V4Candidate,
        state: BaselineState,
        observation: BaselineObservation,
    ) -> tuple[BaselineState, BaselineCommand]: ...


@runtime_checkable
class TimingPolicy(Protocol):
    implementation_id: str

    @property
    def stopped(self) -> bool: ...

    @property
    def stop_reason(self) -> str: ...

    def observe(self, monotonic_timestamp_s: float) -> float | None: ...

    def observe_startup(self, heartbeat: float, now_s: float) -> bool: ...

    def acceptance(self) -> Mapping[str, float | bool | str]: ...


@runtime_checkable
class QdotGatePolicy(Protocol):
    implementation_id: str

    def gate(
        self,
        contract: V4Contract,
        *,
        qdot: Sequence[float],
        jacobian_6x6: Sequence[Sequence[float]],
        normal_base: Sequence[float],
        observed_model_hashes: Mapping[str, str],
    ) -> KinematicGateResult: ...


@runtime_checkable
class ForceSearchPolicy(Protocol):
    implementation_id: str

    def profile_id(self) -> str: ...


@runtime_checkable
class CandidateProvider(Protocol):
    """Optional outer-loop provider; BO is not required for useful V4 behavior."""

    implementation_id: str

    def next_candidate(self, incumbent: V4Candidate) -> V4Candidate: ...


class DefaultEntryPolicy:
    implementation_id = "step5d.v4.entry/default-v1"

    def plan(self, current_pose: Sequence[float]) -> tuple[EntrySegment, ...]:
        return plan_entry(current_pose)


class DefaultBaselinePolicy:
    implementation_id = "step5d.v4.baseline/default-1to5n-v1"

    def step(
        self,
        candidate: V4Candidate,
        state: BaselineState,
        observation: BaselineObservation,
    ) -> tuple[BaselineState, BaselineCommand]:
        return step_baseline(candidate, state, observation)


class DefaultTimingPolicy:
    implementation_id = "step5d.v4.timing/actual-dt-75hz-v1"

    def __init__(self) -> None:
        self._timing = TimingGuard()
        self._startup = StartupHeartbeatGate()

    @property
    def stopped(self) -> bool:
        return self._timing.stopped or self._startup.stopped

    @property
    def stop_reason(self) -> str:
        return self._timing.stop_reason or self._startup.stop_reason

    def observe(self, monotonic_timestamp_s: float) -> float | None:
        return self._timing.observe(monotonic_timestamp_s)

    def observe_startup(self, heartbeat: float, now_s: float) -> bool:
        return self._startup.observe(heartbeat, now_s)

    def acceptance(self) -> Mapping[str, float | bool | str]:
        return self._timing.acceptance()


class DefaultQdotGatePolicy:
    implementation_id = "step5d.v4.qdot-gate/hash-bound-jacobian-v1"

    def gate(
        self,
        contract: V4Contract,
        *,
        qdot: Sequence[float],
        jacobian_6x6: Sequence[Sequence[float]],
        normal_base: Sequence[float],
        observed_model_hashes: Mapping[str, str],
    ) -> KinematicGateResult:
        return gate_qdot(
            contract,
            qdot=qdot,
            jacobian_6x6=jacobian_6x6,
            normal_base=normal_base,
            observed_model_hashes=observed_model_hashes,
        )


class DefaultForceSearchPolicy:
    implementation_id = "step5d.force-search/shared-engine-v1"

    def profile_id(self) -> str:
        return "v4_1n_latch"


class AnchorCandidateProvider:
    implementation_id = "step5d.v4.candidate/anchor-v1"

    def next_candidate(self, incumbent: V4Candidate) -> V4Candidate:
        del incumbent
        return V4Candidate()


class _NoMotionEntryPolicy:
    implementation_id = "step5d.v4.entry/no-motion-degraded-v1"

    def plan(self, current_pose: Sequence[float]) -> tuple[EntrySegment, ...]:
        del current_pose
        return ()


class _NoMotionBaselinePolicy:
    implementation_id = "step5d.v4.baseline/no-motion-degraded-v1"

    def step(
        self,
        candidate: V4Candidate,
        state: BaselineState,
        observation: BaselineObservation,
    ) -> tuple[BaselineState, BaselineCommand]:
        assert_runtime_target(candidate, TARGET_FORCE_N)
        return state, BaselineCommand(
            phase=state.phase,
            internal_setpoint_n=1.0,
            candidate_target_force_n=TARGET_FORCE_N,
            approach_speed_m_s=0.0,
            xy_velocity_m_s=(0.0, 0.0),
            angular_velocity_rad_s=(0.0, 0.0, 0.0),
            stop=True,
            retract_allowed=False,
            auto_home=False,
            reason="baseline_provider_unavailable",
        )


class _NoMotionQdotGatePolicy:
    implementation_id = "step5d.v4.qdot-gate/no-motion-degraded-v1"

    def gate(
        self,
        contract: V4Contract,
        *,
        qdot: Sequence[float],
        jacobian_6x6: Sequence[Sequence[float]],
        normal_base: Sequence[float],
        observed_model_hashes: Mapping[str, str],
    ) -> KinematicGateResult:
        del contract, qdot, jacobian_6x6, normal_base, observed_model_hashes
        return KinematicGateResult(
            allowed=False,
            qdot=ZERO_QDOT,
            twist=ZERO_QDOT,
            total_linear_m_s=0.0,
            normal_m_s=0.0,
            tangential_m_s=0.0,
            angular_rad_s=0.0,
            reason="qdot_gate_provider_unavailable",
        )


class _NoMotionTimingPolicy:
    implementation_id = "step5d.v4.timing/no-motion-degraded-v1"

    stopped = True
    stop_reason = "timing_provider_unavailable"

    def observe(self, monotonic_timestamp_s: float) -> float | None:
        del monotonic_timestamp_s
        return None

    def observe_startup(self, heartbeat: float, now_s: float) -> bool:
        del heartbeat, now_s
        return False

    def acceptance(self) -> Mapping[str, float | bool | str]:
        return {
            "passed": False,
            "rate_hz": 0.0,
            "p99_gap_s": math.inf,
            "max_gap_s": math.inf,
            "stop_reason": self.stop_reason,
        }


class _NoMotionForceSearchPolicy:
    implementation_id = "step5d.force-search/no-motion-degraded-v1"

    def profile_id(self) -> str:
        return "no_motion"


class _NoMotionCandidateProvider:
    implementation_id = "step5d.v4.candidate/no-motion-degraded-v1"

    def next_candidate(self, incumbent: V4Candidate) -> V4Candidate:
        return incumbent


@dataclass(frozen=True)
class PolicyIdentity:
    role: str
    implementation_id: str
    implementation_sha256: str


@dataclass
class V4PolicyBundle:
    entry: EntryPolicy
    force_search: ForceSearchPolicy
    baseline: BaselinePolicy
    timing: TimingPolicy
    qdot_gate: QdotGatePolicy
    candidate_provider: CandidateProvider

    def __post_init__(self) -> None:
        providers = (
            ("entry", self.entry, EntryPolicy),
            ("force_search", self.force_search, ForceSearchPolicy),
            ("baseline", self.baseline, BaselinePolicy),
            ("timing", self.timing, TimingPolicy),
            ("qdot_gate", self.qdot_gate, QdotGatePolicy),
            ("candidate_provider", self.candidate_provider, CandidateProvider),
        )
        for role, provider, protocol in providers:
            if not isinstance(provider, protocol):
                raise RuntimeGuardError(
                    f"{role} provider does not satisfy its typed policy contract"
                )
            implementation_id = getattr(provider, "implementation_id", None)
            if not isinstance(implementation_id, str) or not implementation_id:
                raise RuntimeGuardError(f"{role} provider identity is invalid")

    @classmethod
    def defaults(cls) -> "V4PolicyBundle":
        return cls(
            entry=DefaultEntryPolicy(),
            force_search=DefaultForceSearchPolicy(),
            baseline=DefaultBaselinePolicy(),
            timing=DefaultTimingPolicy(),
            qdot_gate=DefaultQdotGatePolicy(),
            candidate_provider=AnchorCandidateProvider(),
        )

    @classmethod
    def safe_degraded(
        cls, *, missing: Sequence[str] = ()
    ) -> "V4PolicyBundle":
        missing_roles = set(missing)
        known_roles = {
            "entry",
            "force_search",
            "baseline",
            "timing",
            "qdot_gate",
            "candidate_provider",
        }
        unknown_roles = missing_roles - known_roles
        if unknown_roles:
            raise RuntimeGuardError(
                "unknown degraded policy roles: " + ",".join(sorted(unknown_roles))
            )
        defaults = cls.defaults()
        if "entry" in missing_roles:
            defaults.entry = _NoMotionEntryPolicy()
        if "baseline" in missing_roles:
            defaults.baseline = _NoMotionBaselinePolicy()
        if "qdot_gate" in missing_roles:
            defaults.qdot_gate = _NoMotionQdotGatePolicy()
        if "timing" in missing_roles:
            defaults.timing = _NoMotionTimingPolicy()
        if "force_search" in missing_roles:
            defaults.force_search = _NoMotionForceSearchPolicy()
        if "candidate_provider" in missing_roles:
            defaults.candidate_provider = _NoMotionCandidateProvider()
        return defaults

    @property
    def no_motion_roles(self) -> tuple[str, ...]:
        values = (
            ("entry", self.entry),
            ("force_search", self.force_search),
            ("baseline", self.baseline),
            ("timing", self.timing),
            ("qdot_gate", self.qdot_gate),
            ("candidate_provider", self.candidate_provider),
        )
        return tuple(
            role
            for role, provider in values
            if provider.implementation_id.endswith("/no-motion-degraded-v1")
        )

    def identities(self) -> tuple[PolicyIdentity, ...]:
        values = (
            ("entry", self.entry),
            ("force_search", self.force_search),
            ("baseline", self.baseline),
            ("timing", self.timing),
            ("qdot_gate", self.qdot_gate),
            ("candidate_provider", self.candidate_provider),
        )
        return tuple(
            PolicyIdentity(role, provider.implementation_id, _implementation_sha256(provider))
            for role, provider in values
        )

    def assert_bound(self, contract: V4Contract) -> None:
        binding = contract.raw.get("policy_binding")
        providers = (
            binding.get("providers") if isinstance(binding, Mapping) else None
        )
        if not isinstance(providers, Mapping):
            raise RuntimeGuardError("campaign has no policy identity binding")
        actual = {
            "entry": self.entry.implementation_id,
            "force_search": self.force_search.implementation_id,
            "baseline": self.baseline.implementation_id,
            "timing": self.timing.implementation_id,
            "qdot_gate": self.qdot_gate.implementation_id,
            "candidate_provider": self.candidate_provider.implementation_id,
        }
        if dict(providers) != actual:
            raise RuntimeGuardError(
                "policy identity differs; create a new V4 revision/fingerprint"
            )


@dataclass(frozen=True)
class V4InvariantEnvelope:
    """Non-replaceable target, authority, hash and physical safety boundary."""

    target_force_n: float = TARGET_FORCE_N
    cartesian_total_cap_m_s: float = 0.0005
    normal_cap_m_s: float = 0.00035
    tangential_cap_m_s: float = 0.00035
    angular_cap_rad_s: float = 0.05
    qdot_cap_rad_s: float = 0.15

    def enforce_candidate(self, candidate: V4Candidate) -> None:
        assert_runtime_target(candidate, self.target_force_n)

    def enforce_gate(self, result: KinematicGateResult) -> KinematicGateResult:
        if not isinstance(result, KinematicGateResult):
            raise RuntimeGuardError("provider returned an untyped kinematic result")
        if (
            not isinstance(result.allowed, bool)
            or not isinstance(result.reason, str)
            or len(result.qdot) != 6
            or len(result.twist) != 6
        ):
            raise RuntimeGuardError("provider returned a non-6D kinematic result")
        try:
            values = tuple(
                float(value)
                for value in (
                    *result.qdot,
                    *result.twist,
                    result.total_linear_m_s,
                    result.normal_m_s,
                    result.tangential_m_s,
                    result.angular_rad_s,
                )
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeGuardError(
                "provider returned a nonnumeric kinematic output"
            ) from exc
        if not all(math.isfinite(value) for value in values):
            raise RuntimeGuardError("provider returned nonfinite kinematic output")
        qdot = tuple(values[:6])
        twist = tuple(values[6:12])
        total_linear, normal, tangential, angular = values[12:]
        if any(
            value < 0.0
            for value in (
                total_linear,
                normal,
                tangential,
                angular,
            )
        ):
            raise RuntimeGuardError("provider returned a negative speed norm")
        violations = (
            max(abs(value) for value in qdot) > self.qdot_cap_rad_s,
            total_linear > self.cartesian_total_cap_m_s,
            normal > self.normal_cap_m_s,
            tangential > self.tangential_cap_m_s,
            angular > self.angular_cap_rad_s,
        )
        if any(violations):
            raise RuntimeGuardError("provider output exceeds invariant envelope")
        if not result.allowed and any(abs(value) > 0.0 for value in qdot):
            raise RuntimeGuardError("blocked provider output must be zero qdot")
        return KinematicGateResult(
            allowed=result.allowed,
            qdot=qdot,
            twist=twist,
            total_linear_m_s=total_linear,
            normal_m_s=normal,
            tangential_m_s=tangential,
            angular_rad_s=angular,
            reason=result.reason,
        )


__all__ = [
    "AnchorCandidateProvider",
    "BaselinePolicy",
    "CandidateProvider",
    "DefaultBaselinePolicy",
    "DefaultEntryPolicy",
    "DefaultForceSearchPolicy",
    "DefaultQdotGatePolicy",
    "DefaultTimingPolicy",
    "EntryPolicy",
    "ForceSearchPolicy",
    "PolicyIdentity",
    "QdotGatePolicy",
    "TimingPolicy",
    "V4InvariantEnvelope",
    "V4PolicyBundle",
    "ZERO_QDOT",
]
