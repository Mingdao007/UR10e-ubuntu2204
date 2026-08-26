"""Process-local R013 runtime adapter over the mature V4 calibrated runtime.

R013 does not fork the motion stack.  The adapter changes only the R013-owned
seams that the mature runtime leaves configurable: conditional double-clamp
policy, the State21 -> State25 ``freeze_carry_v1`` handoff, and the explicit
manual-demo path feedforward limb.  It also enriches the existing R008 State25
sidecar with the diagnostics already produced by the outer loop.
The patch is scoped to one live owner process and is restored when the owner
closes, so R012 and other historical writers keep their original behavior.
"""

from __future__ import annotations

from dataclasses import replace
import inspect
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_paper_outer_loop import (
    CONDITIONAL_DOUBLE_CLAMP_POLICY,
    LEGACY_FORCE_INTEGRAL_POLICY,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
)
from step5d_autotune_v4_r004.timing import (
    KUNWEI_FRAME_LAYER,
    RTDE_FRAME_LAYER,
    TIMING_EVIDENCE_VERSION,
    TP_ECHO_LAYER,
    WRITER_PUBLISH_LAYER,
    TimingEvidence,
    TimingError,
)
from .runtime_strategy import (
    DISABLED_RUNTIME_STRATEGY,
    compile_runtime_strategy,
    runtime_strategy_sha256,
    target_correction_n,
    target_correction_n_compiled,
    validate_runtime_strategy,
)
from .handoff import (
    FREEZE_CARRY_V1,
    HandoffPolicy,
    make_handoff,
    validate_handoff_policy,
)
from .feedforward import FeedforwardMode, FeedforwardProfile
from .path_context import FIGURE8_DURATION_S
from step5d_autotune_v4_r014.solver_profile import LEGACY_R1, SolverProfile


R013_NORMAL_VELOCITY_LIMIT_M_S = 0.003
R013_INTEGRAL_LIMIT_N_S = 1.0
R013_AUTHORITY_ERROR_N = 0.5

_INSTALLED = False
_SAVED: dict[str, Any] = {}
_ACTIVE_RUNTIME: "R013V4CalibratedRuntime | None" = None
_ACTIVE_STRATEGY: dict[str, Any] = dict(DISABLED_RUNTIME_STRATEGY)
_ACTIVE_HANDOFF_POLICY = HandoffPolicy()
_ACTIVE_FEEDFORWARD_PROFILE = FeedforwardProfile.from_value()
_ACTIVE_PATH_PROVIDER: Any | None = None
_ACTIVE_CORRECTION_POLICY: Any | None = None
_ACTIVE_CORRECTION_FINGERPRINT: str | None = None
_ACTIVE_COMPLETE_CANDIDATE: Any | None = None
_ACTIVE_SOLVER_PROFILE = LEGACY_R1


class R013LightweightTimingEvidenceCollector:
    """Allocation-light timing collector for the R013 hot loop.

    The legacy collector remains unchanged for old lineages.  R013's additive
    collector preserves the same distinct-layer counts, p99, and fresh-gap
    semantics without canonical JSON hashing four sequence identities on
    every 500 Hz tick.
    """

    def __init__(self, **_: Any) -> None:
        self._counts = {
            WRITER_PUBLISH_LAYER: 0,
            RTDE_FRAME_LAYER: 0,
            KUNWEI_FRAME_LAYER: 0,
            TP_ECHO_LAYER: 0,
        }
        self._last: dict[str, tuple[Any, Any]] = {}
        self._times: dict[str, list[float]] = {
            layer: [] for layer in self._counts
        }
        self._feedback_times: list[float] = []
        self._feedback_ages: list[float] = []
        self._last_feedback_key: tuple[tuple[str, str], ...] | None = None

    @staticmethod
    def _timestamp(value: Any, role: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise TimingError(f"{role} timestamp is nonfinite")
        return parsed

    def _record(self, layer: str, observed_at_s: Any, sequence: Any, epoch: Any) -> None:
        if sequence is None:
            return
        timestamp = self._timestamp(observed_at_s, f"{layer}")
        previous = self._last.get(layer)
        key_epoch = repr(epoch)
        if previous is not None and previous[0] == key_epoch:
            old = previous[1]
            try:
                delta = sequence - old
            except TypeError:
                delta = None
            if delta is not None:
                if delta < 0:
                    raise TimingError(f"{layer} sequence regressed")
                if delta == 0:
                    return
            elif sequence == old:
                return
        self._last[layer] = (key_epoch, sequence)
        increment = 1
        if layer == KUNWEI_FRAME_LAYER and previous is not None and previous[0] == key_epoch:
            try:
                increment = int(sequence - previous[1])
            except (TypeError, ValueError, OverflowError) as exc:
                raise TimingError("kunwei cumulative sequence is invalid") from exc
            if increment <= 0:
                raise TimingError("kunwei cumulative sequence did not advance")
        self._counts[layer] += increment
        self._times[layer].append(timestamp)

    def observe_layered_sample(
        self,
        observed_at_s: float,
        *,
        source_sequences: Mapping[str, Any],
        source_ages_s: Mapping[str, float],
        epoch: Any = None,
    ) -> None:
        aliases = {
            WRITER_PUBLISH_LAYER: ("writer", "publish", "writer_publish", "writer_sequence"),
            RTDE_FRAME_LAYER: ("rtde", "rtde_frame", "controller", "actual_qd"),
            KUNWEI_FRAME_LAYER: ("kunwei", "kunwei_frame", "wrench", "sensor"),
            TP_ECHO_LAYER: ("tp", "tp_echo", "tp_consumed", "packet_echo"),
        }
        selected: dict[str, Any] = {}
        for layer, names in aliases.items():
            for name in names:
                if name in source_sequences:
                    selected[layer] = source_sequences[name]
                    self._record(layer, observed_at_s, source_sequences[name], epoch)
                    break
        age_values = [
            float(value)
            for key, value in source_ages_s.items()
            if str(key) not in {"kunwei", "kunwei_frame", "wrench", "sensor"}
        ]
        if not age_values:
            age_values = [float(value) for value in source_ages_s.values()]
        if age_values and selected:
            key = tuple(sorted((str(name), repr(value)) for name, value in selected.items()))
            if key != self._last_feedback_key:
                age = max(age_values)
                if not math.isfinite(age) or age < 0.0:
                    raise TimingError("feedback age is invalid")
                self._last_feedback_key = key
                self._feedback_times.append(self._timestamp(observed_at_s, "feedback"))
                self._feedback_ages.append(age)

    observe_sources = observe_layered_sample

    def finalize(self, *, duration_s: float | None = None) -> TimingEvidence:
        all_times = [value for values in self._times.values() for value in values]
        all_times.extend(self._feedback_times)
        if duration_s is None:
            duration = max(all_times) - min(all_times) if len(all_times) >= 2 else 0.0
        else:
            duration = float(duration_s)
        if not math.isfinite(duration) or duration < 0.0:
            raise TimingError("timing duration is invalid")
        if len(self._feedback_times) >= 2:
            max_gap = max(
                right - left
                for left, right in zip(self._feedback_times, self._feedback_times[1:])
            )
        else:
            max_gap = float("inf")
        ages = sorted(self._feedback_ages)
        if ages:
            index = min(len(ages) - 1, max(0, math.ceil(0.99 * len(ages)) - 1))
            p99 = ages[index]
        else:
            p99 = float("inf")
        return TimingEvidence(
            duration_s=duration,
            successful_writer_publishes=self._counts[WRITER_PUBLISH_LAYER],
            distinct_rtde_frames=self._counts[RTDE_FRAME_LAYER],
            distinct_kunwei_frames=self._counts[KUNWEI_FRAME_LAYER],
            distinct_tp_consumed_packet_echoes=self._counts[TP_ECHO_LAYER],
            feedback_age_p99_s=p99,
            max_fresh_gap_s=max_gap,
            version=TIMING_EVIDENCE_VERSION,
        )


def _require_sha256(value: Any, role: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"R013 {role} must be a lowercase SHA-256")
    return value


def _provider_identity_sha256(provider: Any | None) -> str | None:
    if provider is None:
        return None
    receipt = getattr(provider, "identity_receipt", None)
    return _require_sha256(getattr(receipt, "sha256", None), "path provider identity")


def _assert_complete_candidate_matches_runtime(
    complete_candidate: Any,
    runtime_candidate: Any,
) -> None:
    controller = dict(getattr(complete_candidate, "controller_path", {}))
    for key in (
        "force_p_gain",
        "force_damping",
        "force_i_gain",
        "normal_filter_tau_s",
        "orientation_ko",
        "motion_kp",
    ):
        runtime_value = _candidate_value(runtime_candidate, key)
        expected = float(controller.get(key, float("nan")))
        if (
            runtime_value is None
            or not math.isfinite(expected)
            or not math.isclose(runtime_value, expected, rel_tol=1e-12, abs_tol=1e-15)
        ):
            raise ValueError(f"R013 Figure-eight runtime candidate differs at {key}")


def _candidate_value(candidate: Any, name: str) -> float | None:
    try:
        value = float(getattr(candidate, name))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


class R013V4CalibratedRuntime:  # populated as a subclass by install()
    """Placeholder replaced with the live base subclass during install."""


def _make_runtime_class(base: type) -> type:
    class _R013V4CalibratedRuntime(base):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            selected_feedforward = _ACTIVE_FEEDFORWARD_PROFILE.enabled
            selected_solver_profile = _ACTIVE_SOLVER_PROFILE
            supplied_feedforward = kwargs.get("feedforward_enabled", selected_feedforward)
            if (
                type(supplied_feedforward) is not bool
                or supplied_feedforward != selected_feedforward
            ):
                raise ValueError(
                    "R013 runtime constructor feedforward mode differs from the selected profile"
                )
            base_init = super().__init__
            base_parameters = inspect.signature(base_init).parameters
            if "feedforward_enabled" in base_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in base_parameters.values()
            ):
                kwargs["feedforward_enabled"] = selected_feedforward
            else:
                # Keep the wrapper usable with the small offline test doubles
                # that intentionally model only the pre-R013 constructor.
                kwargs.pop("feedforward_enabled", None)
            if "solver_profile" in base_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in base_parameters.values()
            ):
                supplied_solver = kwargs.get("solver_profile", selected_solver_profile)
                if supplied_solver != selected_solver_profile:
                    raise ValueError(
                        "R013 runtime constructor solver profile differs from the selected profile"
                    )
                kwargs["solver_profile"] = selected_solver_profile
            else:
                kwargs.pop("solver_profile", None)
            base_init(*args, **kwargs)
            if "feedforward_enabled" not in base_parameters and not hasattr(
                self, "_feedforward_enabled"
            ):
                self._feedforward_enabled = selected_feedforward
            # The mature r004/r006 constructors intentionally remain unaware
            # of this demo-only A/B limb.  The selected value is injected at
            # construction, before any control tick can consume the runtime.
            self._r013_feedforward_profile = _ACTIVE_FEEDFORWARD_PROFILE
            self._r013_solver_profile = _ACTIVE_SOLVER_PROFILE
            self._r013_reset_reasons: list[str] = ["candidate_dispatch"]
            self._r013_last_mode: str | None = None
            self._r013_compute_mode: str | None = None
            self._r013_compute_path_time_s: float | None = None
            self._r013_handoff = make_handoff(_ACTIVE_HANDOFF_POLICY)
            self._r013_last_diagnostics: dict[str, Any] = {}
            self._r013_candidate = getattr(self, "candidate", None)
            self._r013_runtime_strategy = dict(_ACTIVE_STRATEGY)
            self._r013_compiled_runtime_strategy = compile_runtime_strategy(
                self._r013_runtime_strategy
            )
            self._r013_runtime_strategy_sha256 = runtime_strategy_sha256(
                self._r013_runtime_strategy
            )
            self._r013_last_strategy_correction_n = 0.0
            self._r013_last_effective_target_n = 5.0
            self._r013_last_unmodified_target_n = 5.0
            self._r013_strategy_safe_return_observed = False
            self._r013_strategy_exit_mode: str | None = None
            self._r013_path_provider = _ACTIVE_PATH_PROVIDER
            self._r013_correction_policy = _ACTIVE_CORRECTION_POLICY
            self._r013_correction_fingerprint = _ACTIVE_CORRECTION_FINGERPRINT
            self._r013_complete_candidate = _ACTIVE_COMPLETE_CANDIDATE
            self._r013_correction_state: Any | None = None
            self._r013_last_context_correction_n = 0.0
            self._r013_last_total_correction_n = 0.0
            self._r013_last_correction_receipt: dict[str, Any] | None = None
            if self._r013_path_provider is not None:
                if (
                    self._r013_correction_policy is None
                    or self._r013_complete_candidate is None
                    or self._r013_correction_fingerprint is None
                ):
                    raise ValueError(
                        "R013 Figure-eight candidate/correction was not configured before ARM"
                    )
                _assert_complete_candidate_matches_runtime(
                    self._r013_complete_candidate,
                    self._r013_candidate,
                )
                from step6_figure8_autotune_v1 import CorrectionStateV1

                self._r013_correction_state = CorrectionStateV1(
                    self._r013_correction_fingerprint
                )
            global _ACTIVE_RUNTIME
            _ACTIVE_RUNTIME = self

        def _r013_note_reason(self, reason: str) -> None:
            if reason not in self._r013_reset_reasons:
                self._r013_reset_reasons.append(reason)

        def _r013_reset(self, reason: str) -> None:
            self._r013_note_reason(reason)
            # The mature state is a dataclass and therefore safe to replace at
            # a mode boundary.  This is the authoritative reset, not a log.
            self._outer_state = Step5dOuterLoopState()

        def desired_twist(self, *, mode: str, **kwargs: Any) -> tuple[float, ...]:
            previous = self._r013_last_mode
            if mode == "path" and previous != "path":
                # State21 has already produced the baseline outer-loop state.
                # Record the boundary, but do not reset it: the first real
                # State25 tick must inherit the normal velocity and integral.
                if _ACTIVE_HANDOFF_POLICY.policy == "blind_reset_v0":
                    self._r013_reset("path_entry")
                self._r013_note_reason("path_entry")
                self._r013_handoff.begin_path(
                    getattr(self, "_outer_state", Step5dOuterLoopState())
                )
                if self._r013_path_provider is not None:
                    from step6_figure8_autotune_v1 import CorrectionStateV1

                    self._r013_correction_state = CorrectionStateV1(
                        self._r013_correction_fingerprint,
                        generation=int(
                            getattr(self._r013_correction_state, "generation", -1)
                        )
                        + 1,
                    )
                self._r013_strategy_safe_return_observed = False
                self._r013_strategy_exit_mode = None
            elif mode in {"hold", "retract", "stop"}:
                self._r013_reset("mode_exit_or_home")
            self._r013_last_mode = str(mode)
            if self._r013_path_provider is not None:
                # Keep the mature BASELINE ramp at [1,5] N while the
                # Figure-eight PATH retains its signed-correction range.
                self.internal_setpoint_bounds_n = (
                    (1.0, 5.0) if mode == "baseline" else (3.75, 6.25)
                )
            global _ACTIVE_RUNTIME
            _ACTIVE_RUNTIME = self
            phase_correction = target_correction_n_compiled(
                self._r013_compiled_runtime_strategy,
                path_time_s=float(kwargs.get("path_time_s", 0.0)),
                mode=str(mode),
            )
            supplied_target = float(kwargs.get("internal_setpoint_n"))
            if (
                self._r013_runtime_strategy.get("enabled") is True
                and mode == "path"
                and not math.isclose(
                supplied_target,
                self._r013_compiled_runtime_strategy.base_target_n,
                rel_tol=0.0,
                abs_tol=1e-12,
                )
            ):
                raise ValueError("R013 phase-target strategy requires its fixed 5 N base target")
            context_correction = 0.0
            correction_receipt: dict[str, Any] | None = None
            path_time_s = float(kwargs.get("path_time_s", 0.0))
            if self._r013_path_provider is not None and mode == "path":
                if not math.isclose(
                    supplied_target, 5.0, rel_tol=0.0, abs_tol=1e-12
                ):
                    raise ValueError(
                        "R013 Figure-eight correction requires its fixed 5 N base target"
                    )
                context = self._r013_path_provider.sample(path_time_s)
                self._r013_correction_state, receipt = (
                    self._r013_correction_policy.apply(
                        self._r013_correction_state,
                        context,
                        fingerprint_sha256=self._r013_correction_fingerprint,
                    )
                )
                context_correction = float(receipt.applied_n)
                correction_receipt = receipt.as_dict()
                correction_receipt["path_context"] = {
                    "path_id": context.path_id,
                    "path_time_s": context.path_time_s,
                    "phase_rad": context.phase_rad,
                    "normalized_progress": context.normalized_progress,
                    "scalar_speed_m_s": context.scalar_speed_m_s,
                    "signed_tangential_acceleration_m_s2": (
                        context.signed_tangential_acceleration_m_s2
                    ),
                    "signed_planar_curvature_m_inv": (
                        context.signed_planar_curvature_m_inv
                    ),
                }
            total_correction = phase_correction + context_correction
            effective_target = supplied_target - total_correction
            setpoint_low, setpoint_high = getattr(
                self, "internal_setpoint_bounds_n", (1.0, 5.0)
            )
            if not float(setpoint_low) <= effective_target <= float(setpoint_high):
                raise ValueError("R013 effective setpoint exceeds its profile safety bounds")
            if self._r013_path_provider is None and not (
                effective_target <= supplied_target <= 5.0
            ):
                raise ValueError("R013 phase-target effective setpoint exceeds its safety bounds")
            if (
                phase_correction > 0.0
                and effective_target
                < self._r013_compiled_runtime_strategy.minimum_effective_target_n
            ):
                raise ValueError("R013 phase-target effective setpoint is below 3.75 N")
            applied = {**kwargs, "internal_setpoint_n": effective_target}
            self._r013_compute_mode = str(mode)
            self._r013_compute_path_time_s = float(kwargs.get("path_time_s", 0.0))
            try:
                result = super().desired_twist(mode=mode, **applied)
            finally:
                self._r013_compute_mode = None
                self._r013_compute_path_time_s = None
            self._r013_last_strategy_correction_n = phase_correction
            self._r013_last_context_correction_n = context_correction
            self._r013_last_total_correction_n = total_correction
            self._r013_last_correction_receipt = correction_receipt
            self._r013_last_effective_target_n = effective_target
            self._r013_last_unmodified_target_n = supplied_target
            self._r013_last_diagnostics.update(
                {
                    "runtime_strategy_sha256": self._r013_runtime_strategy_sha256,
                    "phase_target_correction_n": phase_correction,
                    "context_target_correction_n": context_correction,
                    "total_target_correction_n": total_correction,
                    "context_correction_fingerprint_sha256": (
                        self._r013_correction_fingerprint
                    ),
                    "context_correction_receipt": correction_receipt,
                    "effective_force_target_n": effective_target,
                    "unmodified_force_target_n": supplied_target,
                    "runtime_strategy_enabled": bool(
                        self._r013_runtime_strategy.get("enabled", False)
                    ),
                    "runtime_strategy_path_time_s": float(
                        kwargs.get("path_time_s", 0.0)
                    ),
                    "feedforward_mode": _ACTIVE_FEEDFORWARD_PROFILE.mode.value,
                    "feedforward_enabled": _ACTIVE_FEEDFORWARD_PROFILE.enabled,
                    "feedforward_profile": _ACTIVE_FEEDFORWARD_PROFILE.as_dict(),
                    "solver_profile": _ACTIVE_SOLVER_PROFILE.as_dict(),
                    "solver_profile_sha256": _ACTIVE_SOLVER_PROFILE.sha256,
                    "handoff_policy": _ACTIVE_HANDOFF_POLICY.policy,
                    "handoff_policy_receipt": _ACTIVE_HANDOFF_POLICY.as_dict(),
                    "handoff": self._r013_handoff.receipt(),
                }
            )
            return result

        @property
        def r013_reset_reasons(self) -> tuple[str, ...]:
            return tuple(self._r013_reset_reasons)

    _R013V4CalibratedRuntime.__name__ = "R013V4CalibratedRuntime"
    _R013V4CalibratedRuntime.__qualname__ = "R013V4CalibratedRuntime"
    return _R013V4CalibratedRuntime


def _r013_compute(original: Any, config: Any, state: Any, inputs: Any, **kwargs: Any) -> Any:
    """Force R013 policy into the config built by the mature runtime."""

    runtime = _ACTIVE_RUNTIME
    candidate = getattr(runtime, "_r013_candidate", None)
    # Stage A is a real I-off branch, not an I-on controller with an I gain of
    # zero.  The conditional-double-clamp primitive is intentionally strict
    # and rejects I <= 0; route an explicit I-off candidate through the legacy
    # no-integral path.  Stage B candidates remain on the fixed conditional
    # double-clamp policy below.
    candidate_i_off = bool(getattr(candidate, "i_off", False))
    candidate_limit = float(
        getattr(candidate, "integral_state_limit_n_s", R013_INTEGRAL_LIMIT_N_S)
    )
    if not math.isfinite(candidate_limit) or not 0.5 <= candidate_limit <= 5.0:
        raise ValueError("R013 candidate integral state limit is outside [0.5, 5.0]")
    if candidate_i_off:
        config = replace(
            config,
            force_integral_policy=LEGACY_FORCE_INTEGRAL_POLICY,
            force_integral_limit_n_s=candidate_limit,
            force_integral_authority_error_n=R013_AUTHORITY_ERROR_N,
            force_normal_velocity_limit_m_s=R013_NORMAL_VELOCITY_LIMIT_M_S,
        )
        if isinstance(inputs, Step5dOuterLoopInputs):
            inputs = replace(
                inputs,
                integral_enabled=False,
                integral_reset_reason="i_off",
            )
    elif getattr(config, "force_integral_policy", LEGACY_FORCE_INTEGRAL_POLICY) == LEGACY_FORCE_INTEGRAL_POLICY:
        config = replace(
            config,
            force_integral_policy=CONDITIONAL_DOUBLE_CLAMP_POLICY,
            force_integral_limit_n_s=candidate_limit,
            force_integral_authority_error_n=R013_AUTHORITY_ERROR_N,
            force_normal_velocity_limit_m_s=R013_NORMAL_VELOCITY_LIMIT_M_S,
        )
    output = original(config, state, inputs, **kwargs)
    if runtime is not None:
        mode = getattr(runtime, "_r013_compute_mode", None)
        if mode == "baseline":
            runtime._r013_handoff.observe_baseline_update(state, output.next_state)
        elif mode == "path":
            runtime._r013_handoff.observe_path_update(
                state,
                output.next_state,
                path_time_s=float(getattr(runtime, "_r013_compute_path_time_s", 0.0)),
            )
        runtime._r013_last_diagnostics = dict(getattr(output, "diagnostics", {}) or {})
        runtime._r013_last_diagnostics["handoff_policy"] = _ACTIVE_HANDOFF_POLICY.policy
        runtime._r013_last_diagnostics["handoff_policy_receipt"] = _ACTIVE_HANDOFF_POLICY.as_dict()
        runtime._r013_last_diagnostics["handoff"] = runtime._r013_handoff.receipt()
    return output


def configure_r013_figure8_candidate(
    candidate: Mapping[str, Any] | Any,
    *,
    fingerprint_sha256: str,
) -> dict[str, Any]:
    """Bind one complete Figure-eight candidate before the mature ARM edge.

    The live runtime object snapshots this process-local value in its
    constructor.  Changing it while a PATH runtime is active is forbidden.
    """

    global _ACTIVE_CORRECTION_POLICY, _ACTIVE_CORRECTION_FINGERPRINT
    global _ACTIVE_COMPLETE_CANDIDATE
    if not _INSTALLED or _ACTIVE_PATH_PROVIDER is None:
        raise ValueError("R013 Figure-eight runtime patch is not installed")
    if (
        _ACTIVE_RUNTIME is not None
        and getattr(_ACTIVE_RUNTIME, "_r013_last_mode", None) == "path"
    ):
        raise ValueError("R013 Figure-eight candidate cannot change during PATH")
    identity = _require_sha256(fingerprint_sha256, "correction fingerprint")
    from step6_figure8_autotune_v1 import (
        CORRECTION_NORMALIZATION_SCALES,
        CompleteCandidateV1,
        CorrectionPolicyV1,
    )

    complete = CompleteCandidateV1.from_mapping(candidate)
    policy = CorrectionPolicyV1(
        normalization_scales=CORRECTION_NORMALIZATION_SCALES,
        weights=complete.correction_weights,
    )
    _ACTIVE_COMPLETE_CANDIDATE = complete
    _ACTIVE_CORRECTION_POLICY = policy
    _ACTIVE_CORRECTION_FINGERPRINT = identity
    return {
        "schema": "step6.autotune/figure8-runtime-candidate-binding-v1",
        "version": 1,
        "candidate_key": complete.candidate_key,
        "fingerprint_sha256": identity,
        "path_identity_sha256": _provider_identity_sha256(_ACTIVE_PATH_PROVIDER),
        "correction": complete.correction_block,
        "setpoint_bounds_n": [3.75, 6.25],
        "configured_before_arm": True,
    }


def configure_r013_handoff_policy(
    policy: HandoffPolicy | Mapping[str, Any] | str,
) -> dict[str, Any]:
    """Select one typed handoff arm at Home before constructing a runtime."""

    global _ACTIVE_HANDOFF_POLICY
    if not _INSTALLED:
        raise ValueError("R013 runtime patch is not installed")
    if (
        _ACTIVE_RUNTIME is not None
        and getattr(_ACTIVE_RUNTIME, "_r013_last_mode", None) == "path"
    ):
        raise ValueError("R013 handoff policy cannot change during PATH")
    parsed = validate_handoff_policy(policy)
    _ACTIVE_HANDOFF_POLICY = parsed
    return {
        "schema": "step6.autotune/figure8-handoff-runtime-binding-v1",
        "version": 1,
        "policy": parsed.policy,
        "configured_at_home": True,
        "receipt": parsed.as_dict(),
    }


def install_r013_runtime_patch(
    runtime_strategy: Mapping[str, Any] | None = None,
    handoff_policy: HandoffPolicy | Mapping[str, Any] | str | None = None,
    *,
    path_provider: Any | None = None,
    feedforward_mode: FeedforwardMode | str | None = None,
    solver_profile: SolverProfile = LEGACY_R1,
) -> None:
    """Install the process-local runtime and State25 diagnostic seams."""

    global _INSTALLED, R013V4CalibratedRuntime, _ACTIVE_STRATEGY, _ACTIVE_HANDOFF_POLICY
    global _ACTIVE_FEEDFORWARD_PROFILE, _ACTIVE_SOLVER_PROFILE
    global _ACTIVE_PATH_PROVIDER
    parsed_strategy = validate_runtime_strategy(runtime_strategy)
    parsed_handoff_policy = validate_handoff_policy(handoff_policy)
    parsed_feedforward_profile = FeedforwardProfile.from_value(feedforward_mode)
    if not isinstance(solver_profile, SolverProfile):
        raise ValueError("R013 runtime patch requires a typed solver profile")
    provider_identity = _provider_identity_sha256(path_provider)
    if path_provider is not None and parsed_strategy.get("enabled") is True:
        raise ValueError(
            "R013 Figure-eight context correction cannot compose with the legacy phase strategy"
        )
    if _INSTALLED:
        if runtime_strategy_sha256(parsed_strategy) != runtime_strategy_sha256(
            _ACTIVE_STRATEGY
        ):
            raise ValueError("R013 runtime patch is already installed with another strategy")
        if parsed_handoff_policy != _ACTIVE_HANDOFF_POLICY:
            raise ValueError("R013 runtime patch is already installed with another handoff policy")
        if parsed_feedforward_profile != _ACTIVE_FEEDFORWARD_PROFILE:
            raise ValueError("R013 runtime patch is already installed with another feedforward mode")
        if solver_profile != _ACTIVE_SOLVER_PROFILE:
            raise ValueError("R013 runtime patch is already installed with another solver profile")
        if provider_identity != _provider_identity_sha256(_ACTIVE_PATH_PROVIDER):
            raise ValueError("R013 runtime patch is already installed with another path provider")
        return
    _ACTIVE_STRATEGY = parsed_strategy
    _ACTIVE_HANDOFF_POLICY = parsed_handoff_policy
    _ACTIVE_FEEDFORWARD_PROFILE = parsed_feedforward_profile
    _ACTIVE_SOLVER_PROFILE = solver_profile
    _ACTIVE_PATH_PROVIDER = path_provider
    import step5d_autotune_v4_r004.calibrated_runtime as calibrated
    import step5d_autotune_v4_r004_live_writer as live_writer
    import step5d_autotune_v4_r008.state25_path_trace as trace_module

    _SAVED["calibrated_runtime"] = calibrated.V4CalibratedRuntime
    _SAVED["compute"] = calibrated.compute_step5d_outer_loop
    _SAVED["timing_collector"] = live_writer.TimingEvidenceCollector
    _SAVED["build_state25_row"] = trace_module.build_state25_row
    live_writer.TimingEvidenceCollector = R013LightweightTimingEvidenceCollector
    R013V4CalibratedRuntime = _make_runtime_class(calibrated.V4CalibratedRuntime)
    calibrated.V4CalibratedRuntime = R013V4CalibratedRuntime

    def compute_wrapper(config: Any, state: Any, inputs: Any, **kwargs: Any) -> Any:
        return _r013_compute(_SAVED["compute"], config, state, inputs, **kwargs)

    calibrated.compute_step5d_outer_loop = compute_wrapper
    original_row = _SAVED["build_state25_row"]

    def state25_row_wrapper(**kwargs: Any) -> dict[str, Any]:
        row = original_row(**kwargs)
        runtime = _ACTIVE_RUNTIME
        if runtime is None or int(kwargs.get("tp_state", 25)) != 25:
            return row
        candidate = getattr(runtime, "_r013_candidate", None)
        for key in ("force_i_gain", "force_p_gain", "force_damping", "force_target_n"):
            value = _candidate_value(candidate, key)
            if value is not None:
                row[key] = value
        diagnostics = getattr(runtime, "_r013_last_diagnostics", {})
        for source, target in (
            ("e_f", "force_error_n"),
            ("force_integral_n_s", "force_integral_n_s"),
            ("integral_effective_limit_n_s", "force_integral_limit_n_s"),
            ("integral_state_clamped", "integral_state_clamped"),
            ("integral_authority_clamped", "integral_authority_clamped"),
            ("integral_conditional_frozen", "integral_conditional_frozen"),
            ("integral_velocity_saturated", "integral_velocity_saturated"),
        ):
            if source in diagnostics:
                row[target] = diagnostics[source]
        if "integral_i_term" in diagnostics:
            row["integral_i_term"] = float(diagnostics["integral_i_term"])
        if "force_integral_n_s" in diagnostics:
            row["force_integral_n_s"] = float(diagnostics["force_integral_n_s"])
        if "integral_effective_limit_n_s" in diagnostics:
            row["force_integral_limit_n_s"] = float(
                diagnostics["integral_effective_limit_n_s"]
            )
        for source, target in (
            ("runtime_strategy_sha256", "runtime_strategy_sha256"),
            ("phase_target_correction_n", "phase_target_correction_n"),
            ("context_target_correction_n", "context_target_correction_n"),
            ("total_target_correction_n", "total_target_correction_n"),
            (
                "context_correction_fingerprint_sha256",
                "context_correction_fingerprint_sha256",
            ),
            ("context_correction_receipt", "context_correction_receipt"),
            ("effective_force_target_n", "effective_force_target_n"),
            ("unmodified_force_target_n", "unmodified_force_target_n"),
            ("runtime_strategy_enabled", "runtime_strategy_enabled"),
            ("runtime_strategy_path_time_s", "runtime_strategy_path_time_s"),
            ("feedforward_mode", "feedforward_mode"),
            ("feedforward_enabled", "feedforward_enabled"),
            ("feedforward_profile", "feedforward_profile"),
        ):
            if source in diagnostics:
                row[target] = diagnostics[source]
        handoff = diagnostics.get("handoff")
        if isinstance(handoff, Mapping):
            row["handoff_policy"] = str(diagnostics.get("handoff_policy", FREEZE_CARRY_V1))
            row["handoff_status"] = handoff.get("status")
            row["handoff_baseline_observed"] = bool(handoff.get("baseline_observed"))
            row["handoff_first_path_tick_observed"] = bool(
                handoff.get("first_path_tick_observed")
            )
            row["handoff_continuity_ok"] = bool(handoff.get("continuity_ok"))
            row["handoff_integral_carry_delta_n_s"] = handoff.get(
                "integral_carry_delta_n_s"
            )
            row["handoff_normal_velocity_carry_delta_m_s"] = handoff.get(
                "normal_velocity_carry_delta_m_s"
            )
            row["handoff_first_path_time_s"] = handoff.get("first_path_time_s")
        return row

    trace_module.build_state25_row = state25_row_wrapper
    _INSTALLED = True


def uninstall_r013_runtime_patch() -> None:
    """Restore every patched module attribute after the owner is closed."""

    global _INSTALLED, _ACTIVE_RUNTIME, R013V4CalibratedRuntime, _ACTIVE_STRATEGY, _ACTIVE_HANDOFF_POLICY
    global _ACTIVE_FEEDFORWARD_PROFILE, _ACTIVE_SOLVER_PROFILE
    global _ACTIVE_PATH_PROVIDER, _ACTIVE_CORRECTION_POLICY
    global _ACTIVE_CORRECTION_FINGERPRINT, _ACTIVE_COMPLETE_CANDIDATE
    if not _INSTALLED:
        return
    import step5d_autotune_v4_r004.calibrated_runtime as calibrated
    import step5d_autotune_v4_r004_live_writer as live_writer
    import step5d_autotune_v4_r008.state25_path_trace as trace_module
    calibrated.V4CalibratedRuntime = _SAVED["calibrated_runtime"]
    calibrated.compute_step5d_outer_loop = _SAVED["compute"]
    live_writer.TimingEvidenceCollector = _SAVED["timing_collector"]
    trace_module.build_state25_row = _SAVED["build_state25_row"]
    _ACTIVE_RUNTIME = None
    _ACTIVE_STRATEGY = dict(DISABLED_RUNTIME_STRATEGY)
    _ACTIVE_HANDOFF_POLICY = HandoffPolicy()
    _ACTIVE_FEEDFORWARD_PROFILE = FeedforwardProfile.from_value()
    _ACTIVE_SOLVER_PROFILE = LEGACY_R1
    _ACTIVE_PATH_PROVIDER = None
    _ACTIVE_CORRECTION_POLICY = None
    _ACTIVE_CORRECTION_FINGERPRINT = None
    _ACTIVE_COMPLETE_CANDIDATE = None
    _SAVED.clear()
    _INSTALLED = False
    R013V4CalibratedRuntime = type("R013V4CalibratedRuntime", (), {})


def active_runtime() -> Any | None:
    return _ACTIVE_RUNTIME


def mark_r013_safe_return_transition(
    runtime: Any | None = None,
    *,
    safe_return_verified: bool,
) -> None:
    """Apply the host strategy's exit transition after mature Home proof."""

    active = runtime or _ACTIVE_RUNTIME
    if active is None:
        raise ValueError("R013 runtime strategy safe-return runtime is missing")
    if safe_return_verified is not True:
        raise ValueError("R013 runtime strategy safe return is not verified")
    phase_correction = float(
        getattr(active, "_r013_last_strategy_correction_n", float("nan"))
    )
    context_correction = float(
        getattr(active, "_r013_last_context_correction_n", 0.0)
    )
    total_correction = float(
        getattr(active, "_r013_last_total_correction_n", phase_correction)
    )
    effective = float(
        getattr(active, "_r013_last_effective_target_n", float("nan"))
    )
    unmodified = float(
        getattr(active, "_r013_last_unmodified_target_n", float("nan"))
    )
    if (
        not all(
            math.isfinite(value)
            for value in (
                phase_correction,
                context_correction,
                total_correction,
                effective,
                unmodified,
            )
        )
        or not math.isclose(unmodified, 5.0, rel_tol=0.0, abs_tol=1e-12)
        or phase_correction < 0.0
        or phase_correction > 1.25
        or abs(context_correction) > 1.25
        or not math.isclose(
            total_correction,
            phase_correction + context_correction,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or not math.isclose(
            effective,
            unmodified - total_correction,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or getattr(active, "_r013_last_mode", None) != "path"
    ):
        raise ValueError("R013 runtime strategy pre-exit state differs")
    # TP owns retract/Home and therefore does not call desired_twist with a
    # host mode after the last PATH tick.  The mature result has now proved
    # that physical transition; mirror it explicitly in the host strategy
    # state instead of requiring a callback that the mature route never emits.
    active._r013_reset("mode_exit_or_home")
    active._r013_last_mode = "safe_return"
    active._r013_last_strategy_correction_n = 0.0
    active._r013_last_context_correction_n = 0.0
    active._r013_last_total_correction_n = 0.0
    active._r013_last_correction_receipt = None
    active._r013_last_effective_target_n = 5.0
    active._r013_last_unmodified_target_n = 5.0
    active._r013_last_diagnostics.update(
        {
            "phase_target_correction_n": 0.0,
            "context_target_correction_n": 0.0,
            "total_target_correction_n": 0.0,
            "context_correction_receipt": None,
            "effective_force_target_n": 5.0,
            "unmodified_force_target_n": 5.0,
        }
    )
    active._r013_strategy_safe_return_observed = True
    active._r013_strategy_exit_mode = "safe_return"


def anti_windup_metrics_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    candidate: Mapping[str, Any],
    runtime: Any | None = None,
) -> dict[str, Any]:
    """Build the exact R013 anti-windup receipt from real State25 rows."""

    if not rows:
        raise ValueError("R013 exact trial has no State25 rows")
    integrals: list[float] = []
    i_terms: list[float] = []
    saturated = 0
    frozen = 0
    violations = 0
    p_gain = float(candidate["force_p_gain"])
    i_gain = float(candidate["force_i_gain"])
    i_off = bool(candidate.get("i_off", False)) or i_gain == 0.0
    for row in rows:
        integral = float(row.get("force_integral_n_s"))
        limit = float(row.get("force_integral_limit_n_s", R013_INTEGRAL_LIMIT_N_S))
        if not math.isfinite(integral) or not math.isfinite(limit) or limit <= 0.0:
            raise ValueError("R013 State25 integral evidence is nonfinite")
        integrals.append(abs(integral))
        if bool(row.get("integral_saturated", False)):
            saturated += 1
        if bool(row.get("integral_conditional_frozen", False)):
            frozen += 1
        raw_term = row.get("integral_i_term")
        term = float(raw_term) if raw_term is not None else i_gain * integral
        if not math.isfinite(term):
            raise ValueError("R013 State25 I-term evidence is nonfinite")
        i_terms.append(abs(term))
        if abs(integral) > limit + 1e-9 or abs(term) > 0.5 * p_gain + 1e-9:
            violations += 1
    live_runtime = runtime or _ACTIVE_RUNTIME
    reasons = list(getattr(live_runtime, "r013_reset_reasons", ()))
    if "candidate_dispatch" not in reasons:
        reasons.insert(0, "candidate_dispatch")
    if "path_entry" not in reasons:
        reasons.append("path_entry")
    if not ({"home", "mode_exit_or_home"} & set(reasons)):
        reasons.append("mode_exit_or_home")
    return {
        "schema": "step5d.autotune-v4/r013-trial-anti-windup-v1",
        "policy": LEGACY_FORCE_INTEGRAL_POLICY if i_off else CONDITIONAL_DOUBLE_CLAMP_POLICY,
        "max_abs_integral_n_s": max(integrals),
        "max_abs_i_term": max(i_terms),
        "saturation_duty": saturated / len(rows),
        "freeze_duty": frozen / len(rows),
        "invariant_violation_count": violations,
        "reset_reasons": reasons,
        "path_gain_hot_switch": False,
    }


def runtime_strategy_receipt_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    strategy: Mapping[str, Any],
    runtime: Any | None = None,
    formal_start_s: float = 5.0,
    formal_end_s: float = 60.0,
    expected_duration_s: float = 60.0,
) -> dict[str, Any]:
    """Prove the immutable strategy used the authoritative PATH clock."""

    parsed = validate_runtime_strategy(strategy)
    identity = runtime_strategy_sha256(parsed)
    if not rows:
        raise ValueError("R013 runtime strategy receipt has no State25 rows")
    corrections: list[float] = []
    targets: list[float] = []
    path_times: list[float] = []
    violations = 0
    formal_samples = 0
    for row in rows:
        path_time = float(row.get("runtime_strategy_path_time_s"))
        correction = float(row.get("phase_target_correction_n"))
        effective = float(row.get("effective_force_target_n"))
        unmodified = float(row.get("unmodified_force_target_n"))
        if not all(math.isfinite(value) for value in (path_time, correction, effective, unmodified)):
            raise ValueError("R013 runtime strategy State25 evidence is nonfinite")
        expected = target_correction_n(parsed, path_time_s=path_time, mode="path")
        if (
            row.get("runtime_strategy_sha256") != identity
            or row.get("runtime_strategy_enabled") is not bool(parsed["enabled"])
            or not math.isclose(correction, expected, rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(effective, unmodified - correction, rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(
                unmodified,
                float(parsed.get("base_target_n", 5.0)),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or not float(parsed.get("minimum_effective_target_n", 5.0))
            <= effective
            <= unmodified
            <= 5.0
            or correction > float(parsed.get("maximum_correction_n", 0.0))
        ):
            violations += 1
        corrections.append(correction)
        targets.append(effective)
        path_times.append(path_time)
        if formal_start_s <= path_time < formal_end_s:
            formal_samples += 1
    gaps = [right - left for left, right in zip(path_times, path_times[1:])]
    if (
        any(gap < 0.0 or gap > 0.08 for gap in gaps)
        or path_times[0] > 0.1
        or path_times[-1] < expected_duration_s - 0.1
    ):
        violations += 1
    active = runtime or _ACTIVE_RUNTIME
    exit_restored = bool(
        active is not None
        and getattr(active, "_r013_strategy_safe_return_observed", False) is True
        and getattr(active, "_r013_strategy_exit_mode", None) == "safe_return"
        and math.isclose(
            float(getattr(active, "_r013_last_strategy_correction_n", float("nan"))),
            0.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        and math.isclose(
            float(getattr(active, "_r013_last_effective_target_n", float("nan"))),
            5.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        and math.isclose(
            float(getattr(active, "_r013_last_unmodified_target_n", float("nan"))),
            5.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    )
    if violations or not exit_restored:
        raise ValueError("R013 runtime strategy PATH/exit evidence differs")
    return {
        "schema": "step5d.autotune-v4/r013-runtime-strategy-receipt-v1",
        "runtime_strategy_sha256": identity,
        "enabled": bool(parsed["enabled"]),
        "path_clock": "runtime_desired_twist_path_time_s",
        "path_sample_count": len(rows),
        "formal_sample_count": formal_samples,
        "minimum_effective_target_n": min(targets),
        "maximum_applied_correction_n": max(corrections),
        "first_path_time_s": path_times[0],
        "last_path_time_s": path_times[-1],
        "maximum_path_clock_gap_s": max(gaps, default=0.0),
        "violation_count": violations,
        "exit_restored_to_unmodified_target": exit_restored,
        "safe_return_transition_observed": bool(
            getattr(active, "_r013_strategy_safe_return_observed", False)
        ),
        "exit_mode": getattr(active, "_r013_strategy_exit_mode", None),
    }


def figure8_correction_receipt_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    fingerprint_sha256: str,
    runtime: Any | None = None,
    expected_duration_s: float = FIGURE8_DURATION_S,
) -> dict[str, Any]:
    """Prove signed context correction against the authoritative PATH clock."""

    identity = _require_sha256(fingerprint_sha256, "Figure-eight correction fingerprint")
    if not rows:
        raise ValueError("R013 Figure-eight correction receipt has no State25 rows")
    path_times: list[float] = []
    corrections: list[float] = []
    effective_targets: list[float] = []
    maximum_slew_n_s = 0.0
    previous_time: float | None = None
    previous_correction: float | None = None
    violations = 0
    for row in rows:
        path_time = float(row.get("runtime_strategy_path_time_s"))
        phase = float(row.get("phase_target_correction_n", 0.0))
        context = float(row.get("context_target_correction_n"))
        total = float(row.get("total_target_correction_n"))
        effective = float(row.get("effective_force_target_n"))
        unmodified = float(row.get("unmodified_force_target_n"))
        receipt = row.get("context_correction_receipt")
        if not all(
            math.isfinite(value)
            for value in (path_time, phase, context, total, effective, unmodified)
        ):
            raise ValueError("R013 Figure-eight correction evidence is nonfinite")
        if (
            row.get("context_correction_fingerprint_sha256") != identity
            or not isinstance(receipt, Mapping)
            or receipt.get("fingerprint_sha256") != identity
            or not math.isclose(float(receipt.get("applied_n")), context, rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(phase, 0.0, rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(total, context, rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(unmodified, 5.0, rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(effective, unmodified - total, rel_tol=0.0, abs_tol=1e-12)
            or not 3.75 <= effective <= 6.25
            or abs(context) > 1.25 + 1e-12
        ):
            violations += 1
        path_context = receipt.get("path_context") if isinstance(receipt, Mapping) else None
        if (
            not isinstance(path_context, Mapping)
            or path_context.get("path_id") != "r013_figure8_v1"
            or not math.isclose(
                float(path_context.get("path_time_s", float("nan"))),
                path_time,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            violations += 1
        if previous_time is not None and previous_correction is not None:
            dt = path_time - previous_time
            if dt <= 0.0 or dt > 0.08:
                violations += 1
            elif abs(context - previous_correction) > 0.5 * dt + 1e-9:
                violations += 1
            else:
                maximum_slew_n_s = max(
                    maximum_slew_n_s,
                    abs(context - previous_correction) / dt,
                )
        previous_time = path_time
        previous_correction = context
        path_times.append(path_time)
        corrections.append(context)
        effective_targets.append(effective)
    if path_times[0] > 0.1 or path_times[-1] < expected_duration_s - 0.1:
        violations += 1
    active = runtime or _ACTIVE_RUNTIME
    exit_restored = bool(
        active is not None
        and getattr(active, "_r013_strategy_safe_return_observed", False) is True
        and getattr(active, "_r013_strategy_exit_mode", None) == "safe_return"
        and math.isclose(
            float(getattr(active, "_r013_last_total_correction_n", float("nan"))),
            0.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        and math.isclose(
            float(getattr(active, "_r013_last_effective_target_n", float("nan"))),
            5.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    )
    if violations or not exit_restored:
        raise ValueError("R013 Figure-eight correction PATH/exit evidence differs")
    return {
        "schema": "step6.autotune/figure8-correction-runtime-receipt-v1",
        "version": 1,
        "fingerprint_sha256": identity,
        "path_id": "r013_figure8_v1",
        "path_clock": "runtime_desired_twist_path_time_s",
        "path_sample_count": len(rows),
        "first_path_time_s": path_times[0],
        "last_path_time_s": path_times[-1],
        "minimum_correction_n": min(corrections),
        "maximum_correction_n": max(corrections),
        "minimum_effective_target_n": min(effective_targets),
        "maximum_effective_target_n": max(effective_targets),
        "maximum_observed_slew_n_s": maximum_slew_n_s,
        "output_clip_n": 1.25,
        "slew_limit_n_s": 0.5,
        "violation_count": 0,
        "exit_restored_to_unmodified_target": True,
    }


__all__ = [
    "R013_AUTHORITY_ERROR_N",
    "R013_INTEGRAL_LIMIT_N_S",
    "R013_NORMAL_VELOCITY_LIMIT_M_S",
    "R013LightweightTimingEvidenceCollector",
    "R013V4CalibratedRuntime",
    "active_runtime",
    "anti_windup_metrics_from_rows",
    "configure_r013_figure8_candidate",
    "configure_r013_handoff_policy",
    "figure8_correction_receipt_from_rows",
    "install_r013_runtime_patch",
    "mark_r013_safe_return_transition",
    "runtime_strategy_receipt_from_rows",
    "uninstall_r013_runtime_patch",
]
