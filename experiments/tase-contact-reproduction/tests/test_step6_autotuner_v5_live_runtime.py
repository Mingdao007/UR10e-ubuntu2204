from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r004.wire import CommandMode, SensorPacket  # noqa: E402
from step6_figure8_autotune_v1.core import (  # noqa: E402
    CORRECTION_NORMALIZATION_SCALES,
    CorrectionPolicyV1,
    CorrectionStateV1,
    FigureEightError,
)
from step6_figure8_autotune_v1.v5_composition_contract import (  # noqa: E402
    V5AttemptKind,
    V5TPState,
)
from step6_figure8_autotune_v1.v5_live_runtime import (  # noqa: E402
    V5BackendCommandV1,
    V5CandidateRuntimeSpecV1,
    V5ChainControlV1,
    make_v5_runtime_path_reference,
    v5_path_context,
)
from step6_figure8_autotune_v1.v5_rollover import (  # noqa: E402
    TAIL_END_S,
    CandidateIdentityV1,
)


ANCHOR = (0.462, 0.178, 0.0345, 3.120752062, 0.0, 0.068626833)


@dataclass(frozen=True)
class FakeCandidate:
    name: str

    @property
    def canonical(self) -> dict[str, object]:
        return {"name": self.name}


class FakeBackend:
    def __init__(self, spec: V5CandidateRuntimeSpecV1) -> None:
        self.spec = spec
        self._filtered = 0.0
        self.filter_calls: list[tuple[float, float, float, str]] = []
        self.command_calls: list[dict[str, object]] = []
        self.seeds: list[tuple[float, tuple[float, ...]]] = []

    @property
    def filtered_normal_n(self) -> float:
        return self._filtered

    def observe_filter(
        self,
        *,
        raw_normal_n: float,
        actual_dt_s: float,
        setpoint_n: float,
        mode: str,
    ) -> float:
        self.filter_calls.append((raw_normal_n, actual_dt_s, setpoint_n, mode))
        self._filtered = float(raw_normal_n)
        return self._filtered

    def command(self, **kwargs: object) -> V5BackendCommandV1:
        self.command_calls.append(dict(kwargs))
        value = (
            self.spec.identity.ordinal * 1e-4
            + max(0, len(self.command_calls) - 1) * 1e-6
        )
        return V5BackendCommandV1(
            filtered_normal_n=self._filtered,
            qdot=(value,) * 6,
            gate_receipt={"fake": True},
        )

    def seed_physical_continuity(
        self,
        *,
        filtered_normal_n: float,
        previous_qdot: object,
    ) -> None:
        qdot = tuple(previous_qdot)  # type: ignore[arg-type]
        self.seeds.append((float(filtered_normal_n), qdot))
        self._filtered = float(filtered_normal_n)

    def validate_activation_hold(self, **kwargs: object) -> dict[str, object]:
        return {
            "guard_stack": {
                "terminal_stop": False,
                "soft": {"fail_closed": False},
            },
            "qdot": {"allowed": True, "reason": "fixture"},
            "activation_pending": {
                "seed_exact": True,
                "candidate_backend_executed": False,
                "qdot": list(kwargs["qdot"]),
            },
        }


class FakeFactory:
    def __init__(self) -> None:
        self.backends: list[FakeBackend] = []

    def __call__(self, spec: V5CandidateRuntimeSpecV1) -> FakeBackend:
        backend = FakeBackend(spec)
        self.backends.append(backend)
        return backend


def _spec(ordinal: int, *, weights: tuple[float, ...] = (0.0,) * 6) -> V5CandidateRuntimeSpecV1:
    fingerprint = hashlib.sha256(f"fingerprint-{ordinal}".encode()).hexdigest()
    return V5CandidateRuntimeSpecV1(
        identity=CandidateIdentityV1(
            epoch=9,
            ordinal=ordinal,
            attempt_kind=V5AttemptKind.PRIMARY_NOVEL,
            candidate_token=1000 + ordinal,
        ),
        controller_candidate=FakeCandidate(str(ordinal)),
        correction_policy=CorrectionPolicyV1(
            normalization_scales=CORRECTION_NORMALIZATION_SCALES,
            weights=weights,
        ),
        correction_fingerprint_sha256=fingerprint,
    )


def _sensor(*, heartbeat: float, normal: float) -> SensorPacket:
    return SensorPacket(
        normal_load_n=normal,
        force_norm_n=abs(normal),
        heartbeat=heartbeat,
        sensor_fresh=True,
        stop_request=False,
        eoat_get_ack=True,
        torque_norm_nm=0.0,
        wrench=(0.0, 0.0, -normal, 0.0, 0.0, 0.0),
        filtered_normal_n=normal,
    )


def test_same_tick_latch_enters_path_without_any_readiness_force_window() -> None:
    factory = FakeFactory()
    chain = V5ChainControlV1(
        _spec(1),
        backend_factory=factory,
        anchor_pose=ANCHOR,
    )
    output = SimpleNamespace()
    first = chain.step(
        output=output,
        sensor=_sensor(heartbeat=1.0, normal=0.0),
        tp_state=V5TPState.ONE_NEWTON_ENTRY,
        path_time_s=0.0,
        monotonic_s=10.0,
    )
    second = chain.step(
        output=output,
        sensor=_sensor(heartbeat=2.0, normal=0.0),
        tp_state=V5TPState.ONE_NEWTON_ENTRY,
        path_time_s=0.0,
        monotonic_s=10.002,
    )
    latched = chain.step(
        output=output,
        sensor=_sensor(heartbeat=3.0, normal=1.0),
        tp_state=V5TPState.ONE_NEWTON_ENTRY,
        path_time_s=0.0,
        monotonic_s=10.004,
    )

    assert first.qdot == second.qdot == (0.0,) * 6
    assert latched.command_mode is CommandMode.PATH
    assert latched.sticky_one_newton_latched == 1
    assert latched.base_target_n == 5.0
    assert latched.correction_receipt is None
    assert latched.qdot == (0.0001,) * 6


def test_ramp_and_correction_enable_only_at_five_seconds() -> None:
    factory = FakeFactory()
    chain = V5ChainControlV1(
        _spec(2, weights=(0.1, 0.0, 0.0, 0.0, 0.0, 0.0)),
        backend_factory=factory,
        anchor_pose=ANCHOR,
    )
    output = SimpleNamespace()
    # Establish startup and the sticky latch.
    for index, normal in enumerate((0.0, 0.0, 1.0)):
        chain.step(
            output=output,
            sensor=_sensor(heartbeat=float(index + 1), normal=normal),
            tp_state=V5TPState.ONE_NEWTON_ENTRY,
            path_time_s=0.0,
            monotonic_s=20.0 + index * 0.002,
        )
    at_four = chain.step(
        output=output,
        sensor=_sensor(heartbeat=4.0, normal=5.0),
        tp_state=V5TPState.PATH,
        path_time_s=4.0,
        monotonic_s=20.006,
    )
    before_five = chain.step(
        output=output,
        sensor=_sensor(heartbeat=5.0, normal=5.0),
        tp_state=V5TPState.PATH,
        path_time_s=4.999,
        monotonic_s=20.008,
    )
    at_five = chain.step(
        output=output,
        sensor=_sensor(heartbeat=6.0, normal=5.0),
        tp_state=V5TPState.PATH,
        path_time_s=5.0,
        monotonic_s=20.010,
    )

    assert at_four.base_target_n == pytest.approx(5.0)
    assert at_four.correction_receipt is None
    assert before_five.correction_receipt is None
    assert at_five.correction_receipt is not None
    assert at_five.correction_receipt.applied_n > 0.0
    assert at_five.effective_target_n < 5.0


def test_prepared_backend_is_sensor_silent_until_ack_then_receives_exact_filter_and_slew_seed() -> None:
    factory = FakeFactory()
    chain = V5ChainControlV1(
        _spec(1),
        backend_factory=factory,
        anchor_pose=ANCHOR,
    )
    chain.prepare(_spec(2))
    active, prepared = factory.backends
    assert prepared.filter_calls == []
    assert prepared.command_calls == []

    output = SimpleNamespace()
    for index, normal in enumerate((0.0, 0.0, 1.0)):
        last = chain.step(
            output=output,
            sensor=_sensor(heartbeat=float(index + 1), normal=normal),
            tp_state=V5TPState.ONE_NEWTON_ENTRY,
            path_time_s=0.0,
            monotonic_s=30.0 + index * 0.002,
        )
    seed = chain.arm_commit()
    assert seed == last.qdot
    chain.step(
        output=output,
        sensor=_sensor(heartbeat=4.0, normal=4.25),
        tp_state=V5TPState.CLOSURE_TAIL,
        path_time_s=62.7,
        monotonic_s=30.006,
    )
    assert prepared.filter_calls == []
    refreshed_seed = chain.refresh_commit_seed()
    assert refreshed_seed != seed
    switched = chain.acknowledge_commit()

    assert switched.identity.ordinal == 2
    assert refreshed_seed == chain.previous_qdot
    assert prepared.seeds == [(active.filtered_normal_n, refreshed_seed)]
    assert prepared.filtered_normal_n == 4.25
    assert chain.previous_qdot == refreshed_seed
    assert chain.commit_armed is False


def test_correction_policy_shared_default_still_rejects_equal_path_time() -> None:
    fingerprint = "c" * 64
    policy = CorrectionPolicyV1(
        normalization_scales=CORRECTION_NORMALIZATION_SCALES,
        weights=(0.5, 0.0, 0.0, 0.0, 0.0, 0.0),
    )
    context = v5_path_context(5.0, anchor_pose=ANCHOR)
    state, _ = policy.apply(
        CorrectionStateV1(fingerprint),
        context,
        fingerprint_sha256=fingerprint,
    )

    with pytest.raises(FigureEightError, match="correction path time is non-monotonic"):
        policy.apply(state, context, fingerprint_sha256=fingerprint)


def test_tail_reference_closes_at_twenty_pi_and_does_not_clamp_at_sixty() -> None:
    reference = make_v5_runtime_path_reference(ANCHOR)
    at_sixty = reference(
        "step5d_strict_rnn_autotune_v1",
        ANCHOR[:2],
        60.0,
    )
    at_endpoint = reference(
        "step5d_strict_rnn_autotune_v1",
        ANCHOR[:2],
        TAIL_END_S,
    )
    endpoint_context = v5_path_context(TAIL_END_S, anchor_pose=ANCHOR)

    assert at_sixty["desired_xy"] != pytest.approx(ANCHOR[:2])
    assert at_endpoint["desired_xy"] == pytest.approx(ANCHOR[:2], abs=1e-12)
    assert endpoint_context.path_time_s == pytest.approx(TAIL_END_S)
    assert endpoint_context.desired_pose_base[:2] == pytest.approx(ANCHOR[:2], abs=1e-12)


def test_equal_endpoint_frames_are_idempotent_but_path_time_regression_still_fails() -> None:
    factory = FakeFactory()
    chain = V5ChainControlV1(
        _spec(1, weights=(0.5, 0.0, 0.0, 0.0, 0.0, 0.0)),
        backend_factory=factory,
        anchor_pose=ANCHOR,
    )
    output = SimpleNamespace()
    for index, normal in enumerate((0.0, 0.0, 1.0)):
        chain.step(
            output=output,
            sensor=_sensor(heartbeat=float(index + 1), normal=normal),
            tp_state=V5TPState.ONE_NEWTON_ENTRY,
            path_time_s=0.0,
            monotonic_s=40.0 + index * 0.002,
        )
    endpoint = math.nextafter(TAIL_END_S, 0.0)
    first = chain.step(
        output=output,
        sensor=_sensor(heartbeat=4.0, normal=5.0),
        tp_state=V5TPState.ROLLOVER_PREPARED,
        path_time_s=endpoint,
        monotonic_s=40.006,
    )
    duplicate = chain.step(
        output=output,
        sensor=_sensor(heartbeat=5.0, normal=5.0),
        tp_state=V5TPState.ROLLOVER_PREPARED,
        path_time_s=endpoint,
        monotonic_s=40.008,
    )

    assert first.correction_receipt is not None
    assert duplicate.correction_receipt is not None
    assert duplicate.correction_receipt.applied_n == first.correction_receipt.applied_n
    assert duplicate.correction_receipt.slew_delta_n == 0.0
    assert duplicate.effective_target_n == first.effective_target_n
    with pytest.raises(FigureEightError, match="correction path time is non-monotonic"):
        chain.step(
            output=output,
            sensor=_sensor(heartbeat=6.0, normal=5.0),
            tp_state=V5TPState.ROLLOVER_PREPARED,
            path_time_s=endpoint - 0.001,
            monotonic_s=40.010,
        )
