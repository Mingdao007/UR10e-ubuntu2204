"""Spatial policy and independent progress guards for the modular tube core."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Sequence

from .contracts import (
    EoatProxyV1,
    IdentityError,
    ProxyRegistryV1,
    SweptReferenceV1,
    TubeContractError,
    _finite_float,
    _identity_of,
    _nonnegative_float,
    _nonnegative_int,
    _positive_int,
    _sha256,
    _text,
    canonical_sha256,
)
from .geometry import (
    CircleSdfV1,
    CompositeSdfV1,
    EllipseSdfV1,
    GeometryError,
    SignedDistanceGeometry,
    SignedFieldSample,
)


GeometryStrategyV1 = CircleSdfV1 | EllipseSdfV1 | CompositeSdfV1


class TubeState(str, Enum):
    DISABLED = "disabled"
    SAFE = "safe"
    WARNING = "warning"
    STOP = "stop"


class TubeReason(str, Enum):
    DISABLED_BY_UNQUALIFIED_POLICY = "disabled_by_unqualified_policy"
    INSIDE_TUBE = "inside_tube"
    WARNING_DWELL_PENDING = "warning_dwell_pending"
    WARNING_TELEMETRY_ONLY = "warning_telemetry_only"
    WARNING_CLEARED = "warning_cleared"
    STOP_BOUNDARY = "stop_boundary"
    OUTSIDE_TUBE = "outside_tube"
    MISSING_SOLVER_INPUT = "missing_solver_input"
    NONFINITE_SOLVER_INPUT = "nonfinite_solver_input"
    SOLVER_INPUT_INVALID = "solver_input_invalid"
    PROXY_IDENTITY_MISMATCH = "proxy_identity_mismatch"
    REFERENCE_IDENTITY_MISMATCH = "reference_identity_mismatch"
    PROXY_UNREGISTERED = "proxy_unregistered"
    STRATEGY_IDENTITY_MISMATCH = "strategy_identity_mismatch"
    INVALID_POLICY = "invalid_policy"
    MISSING_DWELL_TIME = "missing_dwell_time"
    NONMONOTONIC_DWELL_CLOCK = "nonmonotonic_dwell_clock"


@dataclass(frozen=True, slots=True, init=False)
class TubePolicyV1:
    """Immutable spatial policy compiled once before a runtime tick loop."""

    enabled: bool
    strategy: GeometryStrategyV1 | None
    proxy_sha256: str | None
    reference_sha256: str | None
    warning_threshold_m: float | None
    warning_dwell_ns: int
    hysteresis_m: float
    registration_sha256: str | None
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    def __init__(
        self,
        enabled: bool,
        strategy: GeometryStrategyV1 | str | None = None,
        proxy_sha256: str | None = None,
        reference_sha256: str | None = None,
        warning_threshold_m: float | None = None,
        warning_dwell_ns: int | None = None,
        hysteresis_m: float | None = None,
        registration_sha256: str | None = None,
        *,
        warning_margin_m: float | None = None,
        warning_hysteresis_m: float | None = None,
        warning_dwell_s: float | None = None,
        threshold_m: float | None = None,
        radius_m: float | None = None,
        bounds_m: Sequence[float] | None = None,
        proxy_registration_sha256: str | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise TubeContractError("enabled must be a bool")
        if warning_threshold_m is not None and warning_margin_m is not None and float(warning_threshold_m) != float(warning_margin_m):
            raise TubeContractError("conflicting warning threshold aliases")
        selected_warning_threshold = (
            warning_threshold_m
            if warning_threshold_m is not None
            else warning_margin_m
        )
        if threshold_m is not None:
            raise TubeContractError(
                "threshold_m is not accepted; warning_threshold_m must be explicit"
            )
        if warning_hysteresis_m is not None and hysteresis_m is not None and float(warning_hysteresis_m) != float(hysteresis_m):
            raise TubeContractError("conflicting hysteresis aliases")
        selected_hysteresis = hysteresis_m if hysteresis_m is not None else warning_hysteresis_m
        selected_hysteresis = 0.0 if selected_hysteresis is None else selected_hysteresis
        if warning_dwell_ns is not None and warning_dwell_s is not None:
            if warning_dwell_ns != int(round(float(warning_dwell_s) * 1_000_000_000.0)):
                raise TubeContractError("conflicting warning dwell aliases")
        if warning_dwell_ns is None:
            if warning_dwell_s is None:
                selected_dwell_ns = 0
            else:
                dwell_s = _nonnegative_float(warning_dwell_s, name="warning_dwell_s")
                selected_dwell_ns = int(round(dwell_s * 1_000_000_000.0))
        else:
            selected_dwell_ns = _nonnegative_int(warning_dwell_ns, name="warning_dwell_ns")
        if registration_sha256 is not None and proxy_registration_sha256 is not None and registration_sha256 != proxy_registration_sha256:
            raise TubeContractError("conflicting registration identity aliases")
        selected_registration = (
            registration_sha256
            if registration_sha256 is not None
            else proxy_registration_sha256
        )

        if not enabled:
            if any(
                value is not None
                for value in (
                    strategy,
                    proxy_sha256,
                    reference_sha256,
                    selected_warning_threshold,
                    selected_registration,
                    radius_m,
                    bounds_m,
                )
            ):
                raise TubeContractError(
                    "disabled policy requires no strategy, threshold, bounds, or registration"
                )
            if selected_dwell_ns != 0 or float(selected_hysteresis) != 0.0:
                raise TubeContractError(
                    "disabled policy requires zero warning dwell and hysteresis"
                )
            document = {
                "schema": "ur10e.modular-tube/tube-policy/v1",
                "enabled": False,
                "strategy": None,
                "proxy_sha256": None,
                "reference_sha256": None,
                "warning_threshold_m": None,
                "warning_dwell_ns": 0,
                "hysteresis_m": 0.0,
                "registration_sha256": None,
            }
            object.__setattr__(self, "enabled", False)
            object.__setattr__(self, "strategy", None)
            object.__setattr__(self, "proxy_sha256", None)
            object.__setattr__(self, "reference_sha256", None)
            object.__setattr__(self, "warning_threshold_m", None)
            object.__setattr__(self, "warning_dwell_ns", 0)
            object.__setattr__(self, "hysteresis_m", 0.0)
            object.__setattr__(self, "registration_sha256", None)
            object.__setattr__(self, "_identity_sha256", canonical_sha256(document))
            return

        selected_strategy = _coerce_strategy(
            strategy,
            radius_m=(
                radius_m
                if radius_m is not None
                else threshold_m
                if isinstance(strategy, str) and strategy == CircleSdfV1.strategy_id
                else None
            ),
            bounds_m=bounds_m,
        )
        if selected_strategy is None:
            raise TubeContractError("enabled policy requires exactly one explicit strategy")
        if not isinstance(selected_strategy, (CircleSdfV1, EllipseSdfV1, CompositeSdfV1)):
            raise TubeContractError("enabled policy strategy is not supported")
        proxy_identity = _sha256(proxy_sha256, name="proxy_sha256")
        reference_identity = _sha256(reference_sha256, name="reference_sha256")
        if selected_warning_threshold is None:
            raise TubeContractError(
                "enabled policy requires an explicit warning_threshold_m"
            )
        warning_threshold = _finite_float(
            selected_warning_threshold,
            name="warning_threshold_m",
            positive=True,
        )
        hysteresis = _nonnegative_float(selected_hysteresis, name="hysteresis_m")
        registration_identity = None
        if selected_registration is not None:
            registration_identity = _sha256(
                selected_registration,
                name="registration_sha256",
            )
            if registration_identity != proxy_identity:
                raise IdentityError("registration_sha256 must equal proxy_sha256")
        else:
            raise TubeContractError(
                "enabled policy requires an explicit registration_sha256"
            )
        document = {
            "schema": "ur10e.modular-tube/tube-policy/v1",
            "enabled": True,
            "strategy": selected_strategy.canonical_document(),
            "proxy_sha256": proxy_identity,
            "reference_sha256": reference_identity,
            "warning_threshold_m": warning_threshold,
            "warning_dwell_ns": selected_dwell_ns,
            "hysteresis_m": hysteresis,
            "registration_sha256": registration_identity,
        }
        object.__setattr__(self, "enabled", True)
        object.__setattr__(self, "strategy", selected_strategy)
        object.__setattr__(self, "proxy_sha256", proxy_identity)
        object.__setattr__(self, "reference_sha256", reference_identity)
        object.__setattr__(self, "warning_threshold_m", warning_threshold)
        object.__setattr__(self, "warning_dwell_ns", selected_dwell_ns)
        object.__setattr__(self, "hysteresis_m", hysteresis)
        object.__setattr__(self, "registration_sha256", registration_identity)
        object.__setattr__(self, "_identity_sha256", canonical_sha256(document))

    @classmethod
    def disabled(cls) -> "TubePolicyV1":
        return cls(enabled=False)

    @classmethod
    def enabled_policy(
        cls,
        *,
        strategy: GeometryStrategyV1 | str,
        proxy_sha256: str,
        reference_sha256: str,
        warning_threshold_m: float,
        warning_dwell_ns: int = 0,
        hysteresis_m: float = 0.0,
        registration_sha256: str,
        radius_m: float | None = None,
        bounds_m: Sequence[float] | None = None,
    ) -> "TubePolicyV1":
        return cls(
            enabled=True,
            strategy=strategy,
            proxy_sha256=proxy_sha256,
            reference_sha256=reference_sha256,
            warning_threshold_m=warning_threshold_m,
            warning_dwell_ns=warning_dwell_ns,
            hysteresis_m=hysteresis_m,
            registration_sha256=registration_sha256,
            radius_m=radius_m,
            bounds_m=bounds_m,
        )

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    @property
    def sha256(self) -> str:
        return self._identity_sha256

    @property
    def canonical_sha256(self) -> str:
        return self._identity_sha256

    @property
    def strategy_id(self) -> str | None:
        return None if self.strategy is None else self.strategy.strategy_id

    @property
    def bounds_m(self) -> tuple[float, float, float] | None:
        if self.strategy is None:
            return None
        bounds = getattr(self.strategy, "bounds_m", None)
        return None if bounds is None else tuple(bounds)

    @property
    def warning_margin_m(self) -> float | None:
        return self.warning_threshold_m

    @property
    def warning_hysteresis_m(self) -> float:
        return self.hysteresis_m

    def canonical_document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/tube-policy/v1",
            "enabled": self.enabled,
            "strategy": None if self.strategy is None else self.strategy.canonical_document(),
            "proxy_sha256": self.proxy_sha256,
            "reference_sha256": self.reference_sha256,
            "warning_threshold_m": self.warning_threshold_m,
            "warning_dwell_ns": self.warning_dwell_ns,
            "hysteresis_m": self.hysteresis_m,
            "registration_sha256": self.registration_sha256,
        }


def _coerce_strategy(
    strategy: GeometryStrategyV1 | str | None,
    *,
    radius_m: float | None,
    bounds_m: Sequence[float] | None,
) -> GeometryStrategyV1 | None:
    if isinstance(strategy, (CircleSdfV1, EllipseSdfV1, CompositeSdfV1)):
        if radius_m is not None or bounds_m is not None:
            raise TubeContractError("strategy object cannot be combined with raw strategy bounds")
        return strategy
    if strategy is None:
        return None
    if not isinstance(strategy, str):
        raise TubeContractError("strategy must be one explicit strategy object or known ID")
    if strategy == CircleSdfV1.strategy_id:
        if radius_m is None and bounds_m is not None:
            candidate_bounds = tuple(bounds_m)
            if len(candidate_bounds) != 3 or not (candidate_bounds[0] == candidate_bounds[1] == candidate_bounds[2]):
                raise TubeContractError("circle_sdf_v1 requires one isotropic bound")
            radius_m = candidate_bounds[0]
        if radius_m is None:
            raise TubeContractError("circle_sdf_v1 requires radius_m")
        return CircleSdfV1(radius_m)
    if strategy == EllipseSdfV1.strategy_id:
        if bounds_m is None:
            raise TubeContractError("ellipse_sdf_v1 requires bounds_m")
        return EllipseSdfV1(bounds_m=bounds_m)
    if strategy == CompositeSdfV1.strategy_id:
        raise TubeContractError("composite_sdf_v1 requires a CompositeSdfV1 object")
    raise TubeContractError(f"unknown strategy {strategy!r}")


@dataclass(frozen=True, slots=True)
class TubeDecisionV1:
    """Typed spatial decision; no state ever carries a motion command."""

    state: TubeState
    reason: TubeReason
    stop: bool
    signed_margin_m: float | None = None
    sample: SignedFieldSample | None = None
    telemetry_only: bool = False
    motion_command: None = None

    def __post_init__(self) -> None:
        state = self.state if isinstance(self.state, TubeState) else TubeState(self.state)
        reason = self.reason if isinstance(self.reason, TubeReason) else TubeReason(self.reason)
        if not isinstance(self.stop, bool):
            raise TubeContractError("TubeDecisionV1.stop must be bool")
        margin = self.signed_margin_m
        if margin is not None:
            margin = _finite_float(margin, name="signed_margin_m")
        if self.sample is not None:
            if not isinstance(self.sample, SignedFieldSample):
                raise TubeContractError("TubeDecisionV1.sample must be SignedFieldSample")
            if margin is None:
                margin = self.sample.signed_distance_m
            elif margin != self.sample.signed_distance_m:
                raise TubeContractError("decision margin drifted from geometry sample")
        if state is TubeState.STOP and not self.stop:
            raise TubeContractError("STOP decision must stop")
        if state is not TubeState.STOP and self.stop:
            raise TubeContractError("non-STOP decision cannot stop")
        expected_telemetry = state is TubeState.WARNING
        if self.telemetry_only != expected_telemetry:
            raise TubeContractError("only WARNING is telemetry-only")
        if self.motion_command is not None:
            raise TubeContractError("tube decisions cannot emit motion commands")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "signed_margin_m", margin)

    @property
    def signed_distance_m(self) -> float | None:
        return self.signed_margin_m

    @property
    def should_stop(self) -> bool:
        return self.stop

    @property
    def command(self) -> None:
        return None


class DisabledTubeGuard:
    """Stateless, allocation-free disabled guard."""

    __slots__ = ("_decision",)

    def __init__(self) -> None:
        self._decision = TubeDecisionV1(
            state=TubeState.DISABLED,
            reason=TubeReason.DISABLED_BY_UNQUALIFIED_POLICY,
            stop=False,
            signed_margin_m=None,
            sample=None,
            telemetry_only=False,
        )

    @property
    def decision(self) -> TubeDecisionV1:
        return self._decision

    def evaluate(self, *args: Any, **kwargs: Any) -> TubeDecisionV1:
        del args, kwargs
        return self._decision

    def tick(self, *args: Any, **kwargs: Any) -> TubeDecisionV1:
        return self.evaluate(*args, **kwargs)


class SpatialTubeGuard:
    """Stateful warning/dwell policy around a pure compiled geometry."""

    __slots__ = (
        "policy",
        "geometry",
        "proxy_registry",
        "_warning_started_ns",
        "_warning_active",
        "_last_now_ns",
    )

    def __new__(
        cls,
        policy: TubePolicyV1,
        geometry: SignedDistanceGeometry | None = None,
        proxy_registry: ProxyRegistryV1 | None = None,
        *,
        registry: ProxyRegistryV1 | None = None,
    ) -> "SpatialTubeGuard | DisabledTubeGuard":
        del geometry, proxy_registry, registry
        if isinstance(policy, TubePolicyV1) and not policy.enabled:
            return DisabledTubeGuard()
        return super().__new__(cls)

    def __init__(
        self,
        policy: TubePolicyV1,
        geometry: SignedDistanceGeometry | None = None,
        proxy_registry: ProxyRegistryV1 | None = None,
        *,
        registry: ProxyRegistryV1 | None = None,
    ) -> None:
        if not isinstance(policy, TubePolicyV1):
            raise TubeContractError("SpatialTubeGuard requires TubePolicyV1")
        if proxy_registry is not None and registry is not None and proxy_registry != registry:
            raise TubeContractError("conflicting proxy registry aliases")
        selected_registry = proxy_registry if proxy_registry is not None else registry
        self.policy = policy
        self._warning_started_ns = None
        self._warning_active = False
        self._last_now_ns = None
        if not isinstance(geometry, SignedDistanceGeometry):
            raise TubeContractError("enabled SpatialTubeGuard requires SignedDistanceGeometry")
        if not isinstance(selected_registry, ProxyRegistryV1):
            raise TubeContractError("enabled SpatialTubeGuard requires ProxyRegistryV1")
        if geometry.strategy_id != policy.strategy_id or geometry.identity_sha256 != policy.strategy.sha256:
            raise IdentityError("geometry strategy does not match TubePolicyV1")
        if policy.registration_sha256 is None:
            raise TubeContractError("enabled guard requires a registered proxy identity")
        registered_proxy = selected_registry.resolve(policy.registration_sha256)
        if registered_proxy is None or registered_proxy.T_tool_proxy is None:
            raise TubeContractError("enabled guard requires a registered qualified proxy")
        self.geometry = geometry
        self.proxy_registry = selected_registry

    def _reset_warning(self) -> None:
        self._warning_started_ns = None
        self._warning_active = False

    def _stop(
        self,
        reason: TubeReason,
        *,
        sample: SignedFieldSample | None = None,
    ) -> TubeDecisionV1:
        self._reset_warning()
        return TubeDecisionV1(
            state=TubeState.STOP,
            reason=reason,
            stop=True,
            sample=sample,
            signed_margin_m=None if sample is None else sample.signed_distance_m,
        )

    def _now_ns(self, value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        if self._last_now_ns is not None and value < self._last_now_ns:
            return None
        self._last_now_ns = value
        return value

    def evaluate(
        self,
        actual_pose: Any,
        proxy: Any,
        swept_reference: Any,
        *,
        now_ns: int | None = None,
        observed_monotonic_ns: int | None = None,
    ) -> TubeDecisionV1:
        if actual_pose is None or proxy is None or swept_reference is None:
            return self._stop(TubeReason.MISSING_SOLVER_INPUT)
        if now_ns is not None and observed_monotonic_ns is not None and now_ns != observed_monotonic_ns:
            return self._stop(TubeReason.NONMONOTONIC_DWELL_CLOCK)
        selected_now = now_ns if now_ns is not None else observed_monotonic_ns
        if selected_now is None:
            return self._stop(TubeReason.MISSING_DWELL_TIME)
        checked_now = self._now_ns(selected_now)
        if checked_now is None:
            return self._stop(TubeReason.NONMONOTONIC_DWELL_CLOCK)
        if not isinstance(proxy, EoatProxyV1):
            return self._stop(TubeReason.NONFINITE_SOLVER_INPUT)
        if not isinstance(swept_reference, SweptReferenceV1):
            return self._stop(TubeReason.NONFINITE_SOLVER_INPUT)
        if proxy.T_tool_proxy is None:
            return self._stop(TubeReason.PROXY_UNREGISTERED)
        if proxy.sha256 != self.policy.proxy_sha256:
            return self._stop(TubeReason.PROXY_IDENTITY_MISMATCH)
        if swept_reference.sha256 != self.policy.reference_sha256:
            return self._stop(TubeReason.REFERENCE_IDENTITY_MISMATCH)
        if self.proxy_registry is None or not self.proxy_registry.contains(proxy):
            return self._stop(TubeReason.PROXY_UNREGISTERED)
        if self.geometry is None or self.policy.strategy is None:
            return self._stop(TubeReason.INVALID_POLICY)
        try:
            sample = self.geometry.evaluate(actual_pose, proxy, swept_reference)
        except (GeometryError, TubeContractError, TypeError, ValueError, ArithmeticError):
            return self._stop(TubeReason.SOLVER_INPUT_INVALID)
        if not math.isfinite(sample.signed_distance_m):
            return self._stop(TubeReason.NONFINITE_SOLVER_INPUT)
        signed = sample.signed_distance_m
        if signed >= 0.0:
            return self._stop(
                TubeReason.STOP_BOUNDARY if signed == 0.0 else TubeReason.OUTSIDE_TUBE,
                sample=sample,
            )

        warning_threshold = self.policy.warning_threshold_m
        if warning_threshold is None:  # pragma: no cover - enabled constructor proof
            return self._stop(TubeReason.INVALID_POLICY, sample=sample)
        clear_boundary = -(warning_threshold + self.policy.hysteresis_m)
        if self._warning_active:
            if signed <= clear_boundary:
                self._reset_warning()
                return TubeDecisionV1(
                    state=TubeState.SAFE,
                    reason=TubeReason.WARNING_CLEARED,
                    stop=False,
                    sample=sample,
                )
            return TubeDecisionV1(
                state=TubeState.WARNING,
                reason=TubeReason.WARNING_TELEMETRY_ONLY,
                stop=False,
                sample=sample,
                telemetry_only=True,
            )

        if signed < -warning_threshold:
            self._warning_started_ns = None
            return TubeDecisionV1(
                state=TubeState.SAFE,
                reason=TubeReason.INSIDE_TUBE,
                stop=False,
                sample=sample,
            )

        if self._warning_started_ns is None:
            self._warning_started_ns = checked_now
        if checked_now - self._warning_started_ns >= self.policy.warning_dwell_ns:
            self._warning_active = True
            return TubeDecisionV1(
                state=TubeState.WARNING,
                reason=TubeReason.WARNING_TELEMETRY_ONLY,
                stop=False,
                sample=sample,
                telemetry_only=True,
            )
        return TubeDecisionV1(
            state=TubeState.SAFE,
            reason=TubeReason.WARNING_DWELL_PENDING,
            stop=False,
            sample=sample,
        )

    def tick(self, *args: Any, **kwargs: Any) -> TubeDecisionV1:
        return self.evaluate(*args, **kwargs)


def make_tube_guard(
    policy: TubePolicyV1,
    *,
    geometry: SignedDistanceGeometry | None = None,
    proxy_registry: ProxyRegistryV1 | None = None,
) -> DisabledTubeGuard | SpatialTubeGuard:
    if not policy.enabled:
        return DisabledTubeGuard()
    return SpatialTubeGuard(
        policy,
        geometry=geometry,
        proxy_registry=proxy_registry,
    )


class ProgressReason(str, Enum):
    OK = "ok"
    MISSING_PROGRESS = "missing_progress"
    INVALID_PROGRESS = "invalid_progress"
    SEQUENCE = "sequence"
    TIMESTAMP = "timestamp"
    FRESHNESS = "freshness"
    LAG = "lag"
    FREEZE_PENDING = "freeze_pending"
    FREEZE = "freeze"


@dataclass(frozen=True, slots=True, init=False)
class ProgressSampleV1:
    """Typed progress observation owned solely by :class:`ProgressGuardV1`."""

    sequence: int
    timestamp_ns: int
    freshness_age_ns: int
    lag_ns: int
    observed_monotonic_ns: int | None

    def __init__(
        self,
        sequence: int,
        timestamp_ns: int,
        freshness_age_ns: int = 0,
        lag_ns: int = 0,
        observed_monotonic_ns: int | None = None,
        *,
        age_ns: int | None = None,
        freshness_ns: int | None = None,
    ) -> None:
        aliases = tuple(value for value in (age_ns, freshness_ns) if value is not None)
        if aliases and any(value != aliases[0] for value in aliases):
            raise TubeContractError("conflicting freshness aliases")
        selected_age = freshness_age_ns
        if aliases:
            if freshness_age_ns != 0 and freshness_age_ns != aliases[0]:
                raise TubeContractError("conflicting freshness_age_ns aliases")
            selected_age = aliases[0]
        checked_sequence = _positive_int(sequence, name="sequence")
        checked_timestamp = _positive_int(timestamp_ns, name="timestamp_ns")
        checked_age = _nonnegative_int(selected_age, name="freshness_age_ns")
        checked_lag = _nonnegative_int(lag_ns, name="lag_ns")
        checked_observed = None
        if observed_monotonic_ns is not None:
            checked_observed = _nonnegative_int(
                observed_monotonic_ns,
                name="observed_monotonic_ns",
            )
        object.__setattr__(self, "sequence", checked_sequence)
        object.__setattr__(self, "timestamp_ns", checked_timestamp)
        object.__setattr__(self, "freshness_age_ns", checked_age)
        object.__setattr__(self, "lag_ns", checked_lag)
        object.__setattr__(self, "observed_monotonic_ns", checked_observed)

    @property
    def age_ns(self) -> int:
        return self.freshness_age_ns


@dataclass(frozen=True, slots=True)
class ProgressDecisionV1:
    ok: bool
    reason: ProgressReason
    sequence_ok: bool
    timestamp_ok: bool
    freshness_ok: bool
    lag_ok: bool
    frozen: bool

    def __post_init__(self) -> None:
        reason = self.reason if isinstance(self.reason, ProgressReason) else ProgressReason(self.reason)
        for name in ("ok", "sequence_ok", "timestamp_ok", "freshness_ok", "lag_ok", "frozen"):
            if not isinstance(getattr(self, name), bool):
                raise TubeContractError(f"{name} must be bool")
        if self.ok and self.frozen:
            raise TubeContractError("a frozen progress decision cannot be ok")
        object.__setattr__(self, "reason", reason)

    @property
    def stop(self) -> bool:
        return not self.ok

    @property
    def should_stop(self) -> bool:
        return not self.ok


class ProgressGuardV1:
    """Independent sequence/timestamp/freshness/lag/freeze decision guard."""

    __slots__ = (
        "max_freshness_age_ns",
        "max_lag_ns",
        "freeze_dwell_ns",
        "_last_sequence",
        "_last_timestamp_ns",
        "_freeze_started_ns",
        "_last_observed_ns",
    )

    def __init__(
        self,
        max_freshness_age_ns: int | None = None,
        max_lag_ns: int | None = None,
        freeze_dwell_ns: int = 0,
        *,
        freshness_limit_ns: int | None = None,
        lag_limit_ns: int | None = None,
        freeze_after_ns: int | None = None,
    ) -> None:
        selected_freshness = max_freshness_age_ns if max_freshness_age_ns is not None else freshness_limit_ns
        selected_lag = max_lag_ns if max_lag_ns is not None else lag_limit_ns
        selected_freeze = freeze_after_ns if freeze_after_ns is not None else freeze_dwell_ns
        if selected_freshness is None or selected_lag is None:
            raise TubeContractError("ProgressGuardV1 requires freshness and lag bounds")
        self.max_freshness_age_ns = _positive_int(selected_freshness, name="max_freshness_age_ns")
        self.max_lag_ns = _positive_int(selected_lag, name="max_lag_ns")
        self.freeze_dwell_ns = _nonnegative_int(selected_freeze, name="freeze_dwell_ns")
        self.reset()

    def reset(self) -> None:
        self._last_sequence = None
        self._last_timestamp_ns = None
        self._freeze_started_ns = None
        self._last_observed_ns = None

    def _decision(
        self,
        reason: ProgressReason,
        *,
        sequence_ok: bool,
        timestamp_ok: bool,
        freshness_ok: bool,
        lag_ok: bool,
        frozen: bool = False,
    ) -> ProgressDecisionV1:
        return ProgressDecisionV1(
            ok=reason in {ProgressReason.OK, ProgressReason.FREEZE_PENDING},
            reason=reason,
            sequence_ok=sequence_ok,
            timestamp_ok=timestamp_ok,
            freshness_ok=freshness_ok,
            lag_ok=lag_ok,
            frozen=frozen,
        )

    def evaluate(
        self,
        sample: ProgressSampleV1 | None,
        *,
        observed_monotonic_ns: int | None = None,
    ) -> ProgressDecisionV1:
        if sample is None:
            self.reset()
            return self._decision(
                ProgressReason.MISSING_PROGRESS,
                sequence_ok=False,
                timestamp_ok=False,
                freshness_ok=False,
                lag_ok=False,
            )
        if not isinstance(sample, ProgressSampleV1):
            self.reset()
            return self._decision(
                ProgressReason.INVALID_PROGRESS,
                sequence_ok=False,
                timestamp_ok=False,
                freshness_ok=False,
                lag_ok=False,
            )
        current_observed = observed_monotonic_ns
        if current_observed is None:
            current_observed = sample.observed_monotonic_ns
        prior_observed = self._last_observed_ns
        if current_observed is not None:
            if isinstance(current_observed, bool) or not isinstance(current_observed, int) or current_observed < 0:
                self.reset()
                return self._decision(
                    ProgressReason.INVALID_PROGRESS,
                    sequence_ok=False,
                    timestamp_ok=False,
                    freshness_ok=False,
                    lag_ok=False,
                )
            if self._last_observed_ns is not None and current_observed < self._last_observed_ns:
                self.reset()
                return self._decision(
                    ProgressReason.TIMESTAMP,
                    sequence_ok=False,
                    timestamp_ok=False,
                    freshness_ok=False,
                    lag_ok=False,
                )
            self._last_observed_ns = current_observed
        freshness_ok = sample.freshness_age_ns <= self.max_freshness_age_ns
        lag_ok = sample.lag_ns <= self.max_lag_ns
        if not freshness_ok:
            self.reset()
            return self._decision(
                ProgressReason.FRESHNESS,
                sequence_ok=True,
                timestamp_ok=True,
                freshness_ok=False,
                lag_ok=lag_ok,
            )
        if not lag_ok:
            self.reset()
            return self._decision(
                ProgressReason.LAG,
                sequence_ok=True,
                timestamp_ok=True,
                freshness_ok=True,
                lag_ok=False,
            )

        if self._last_sequence is not None and self._last_timestamp_ns is not None:
            last_sequence = self._last_sequence
            last_timestamp_ns = self._last_timestamp_ns
            same_sample = (
                sample.sequence == last_sequence
                and sample.timestamp_ns == last_timestamp_ns
            )
            if same_sample:
                if self._freeze_started_ns is None:
                    self._freeze_started_ns = (
                        prior_observed
                        if prior_observed is not None
                        else sample.timestamp_ns
                    )
                now = current_observed if current_observed is not None else sample.timestamp_ns
                if now - self._freeze_started_ns >= self.freeze_dwell_ns:
                    return self._decision(
                        ProgressReason.FREEZE,
                        sequence_ok=True,
                        timestamp_ok=True,
                        freshness_ok=True,
                        lag_ok=True,
                        frozen=True,
                    )
                return self._decision(
                    ProgressReason.FREEZE_PENDING,
                    sequence_ok=True,
                    timestamp_ok=True,
                    freshness_ok=True,
                    lag_ok=True,
                    frozen=False,
                )
            self._freeze_started_ns = None
            if sample.sequence <= last_sequence:
                self.reset()
                return self._decision(
                    ProgressReason.SEQUENCE,
                    sequence_ok=False,
                    timestamp_ok=sample.timestamp_ns > last_timestamp_ns,
                    freshness_ok=True,
                    lag_ok=True,
                )
            if sample.timestamp_ns <= last_timestamp_ns:
                self.reset()
                return self._decision(
                    ProgressReason.TIMESTAMP,
                    sequence_ok=True,
                    timestamp_ok=False,
                    freshness_ok=True,
                    lag_ok=True,
                )

        self._last_sequence = sample.sequence
        self._last_timestamp_ns = sample.timestamp_ns
        return self._decision(
            ProgressReason.OK,
            sequence_ok=True,
            timestamp_ok=True,
            freshness_ok=True,
            lag_ok=True,
        )


ProgressReasonV1 = ProgressReason


__all__ = [
    "DisabledTubeGuard",
    "GeometryStrategyV1",
    "ProgressDecisionV1",
    "ProgressGuardV1",
    "ProgressReason",
    "ProgressReasonV1",
    "ProgressSampleV1",
    "SpatialTubeGuard",
    "TubeDecisionV1",
    "TubePolicyV1",
    "TubeReason",
    "TubeState",
    "make_tube_guard",
]
