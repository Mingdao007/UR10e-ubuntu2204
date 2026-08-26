from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r004.transport import R004OutputSnapshot  # noqa: E402
from step5d_autotune_v4_r004.wire import SensorPacket  # noqa: E402
from step5d_autotune_v4_r013.lifecycle_trace import (  # noqa: E402
    FLAG_COMMAND_PRESENT,
    LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
    LifecycleTrace,
    load_lifecycle_artifact,
)
from step6_figure8_autotune_v1.v5_composition_contract import (  # noqa: E402
    BaseOutputOverlayV2,
    RolloverCommand,
    RolloverOutputOverlayV2,
    V5AttemptKind,
    V5RolloverInput,
    V5TPState,
)
from step6_figure8_autotune_v1.core import CompleteCandidateV1  # noqa: E402
from step6_figure8_autotune_v1.v5_campaign import (  # noqa: E402
    CampaignRoleV2,
    ProposalMethodV2,
    ProposalReceiptV2,
    TrialStageV2,
    V5TrialPlan,
    V5_WIRE_EPOCH,
    _candidate_token,
)
from step6_figure8_autotune_v1.v5_lifecycle_ledger import (  # noqa: E402
    BoundaryMode,
    LedgerRole,
)
from step6_figure8_autotune_v1.v5_live_runtime import (  # noqa: E402
    V5BackendCommandV1,
)
from step6_figure8_autotune_v1.v5_live_owner import (  # noqa: E402
    ROLLOVER_COMMIT_LEAD_S,
    V5ActivationJournalEntryV1,
    V5ActivationDurabilityBarrierV1,
    V5GCWindowReceiptV1,
    V5LifecycleTraceAdapter,
    V5LiveChainRequestV1,
    V5LiveContextV1,
    V5LiveOwnerError,
    V5PrebuiltBackendCacheV1,
    V5RolloverActivationJournalV1,
    V5RolloverCoordinatorV1,
    V5RolloverEchoKind,
    V5RolloverPhase,
    V5SingleWriterOwnerV1,
    candidate_identity_from_plan,
    runtime_spec_from_plan,
)
import step6_figure8_autotune_v1.v5_live_owner as live_owner_module  # noqa: E402
from step6_figure8_autotune_v1.v5_register_transport import (  # noqa: E402
    V5OutputSnapshotV1,
)
from step6_figure8_autotune_v1.v5_rollover import (  # noqa: E402
    CandidateIdentityV1,
    PATH_END_S,
    TAIL_END_S,
)


def _identity(ordinal: int, token: int) -> CandidateIdentityV1:
    return CandidateIdentityV1(
        9,
        ordinal,
        V5AttemptKind.PRIMARY_NOVEL,
        token,
    )


def _view(
    state: V5TPState,
    overlay: BaseOutputOverlayV2 | RolloverOutputOverlayV2,
    *,
    timestamp: float = 1.0,
    identity: CandidateIdentityV1 | None = None,
) -> V5OutputSnapshotV1:
    echo_identity = _identity(1, 1) if identity is None else identity
    snapshot = R004OutputSnapshot(
        observed_at_s=timestamp,
        timestamp=timestamp,
        payload_kg=1.0,
        payload_cog_m=(0.0, 0.0, 0.0),
        tcp_offset_m_rad=(0.0,) * 6,
        tcp_speed_m_s_rad_s=(0.0,) * 6,
        tcp_pose_m_rad=(0.0,) * 6,
        q_rad=(0.0,) * 6,
        qd_rad_s=(0.0,) * 6,
        safety_mode="NORMAL",
        robot_mode="RUNNING",
        runtime_state=2,
        consumed_packet_sequence=0,
        integer_echoes={
            24: echo_identity.epoch,
            25: echo_identity.ordinal,
            26: int(state),
            27: echo_identity.candidate_token,
            28: 0,
            **overlay.by_register,
            32: 607007,
            33: 5,
            34: 520607,
        },
    )
    return V5OutputSnapshotV1(snapshot, state, overlay)


@pytest.mark.parametrize("pre_enabled", [True, False])
def test_v5_gc_window_receipt_restores_exact_state_before_post_home_seal(
    monkeypatch: pytest.MonkeyPatch, pre_enabled: bool
) -> None:
    state = {"enabled": pre_enabled}

    monkeypatch.setattr(live_owner_module.gc, "isenabled", lambda: state["enabled"])
    monkeypatch.setattr(
        live_owner_module.gc,
        "disable",
        lambda: state.__setitem__("enabled", False),
    )
    monkeypatch.setattr(
        live_owner_module.gc,
        "enable",
        lambda: state.__setitem__("enabled", True),
    )
    receipt = V5GCWindowReceiptV1.capture()
    receipt.enter()
    assert state["enabled"] is False
    receipt.restore_after_timing_lease()
    assert state["enabled"] is pre_enabled
    assert receipt.as_dict()["restored_after_timing_lease"] is True


def test_rollover_coordinator_requires_typed_prepare_then_exact_commit_ack() -> None:
    current = _identity(1, 101)
    successor = _identity(2, 202)
    coordinator = V5RolloverCoordinatorV1(current, successor, generation=1)

    prepare = coordinator.observe(
        _view(V5TPState.PATH, BaseOutputOverlayV2(1, current.attempt_kind, 0)),
        path_time_s=1.0,
        monotonic_s=1.0,
    )
    assert prepare.prepare_runtime is True
    assert prepare.wire_input.command is RolloverCommand.PREPARE
    assert prepare.wire_input.next_candidate_token == successor.candidate_token
    assert coordinator.phase is V5RolloverPhase.PREPARE_SENT

    prepared = coordinator.observe(
        _view(V5TPState.ROLLOVER_PREPARED, RolloverOutputOverlayV2(1, 0, 202)),
        path_time_s=2.0,
        monotonic_s=2.0,
    )
    assert prepared.wire_input.command is RolloverCommand.NONE
    assert coordinator.phase is V5RolloverPhase.PREPARED

    waiting = coordinator.observe(
        _view(V5TPState.CLOSURE_TAIL, RolloverOutputOverlayV2(1, 0, 202)),
        path_time_s=PATH_END_S - 0.001,
        monotonic_s=3.0,
    )
    assert waiting.wire_input.command is RolloverCommand.NONE

    tail_waiting = coordinator.observe(
        _view(V5TPState.CLOSURE_TAIL, RolloverOutputOverlayV2(1, 0, 202)),
        path_time_s=PATH_END_S,
        monotonic_s=4.0,
    )
    assert tail_waiting.wire_input.command is RolloverCommand.NONE
    assert coordinator.phase is V5RolloverPhase.PREPARED

    commit = coordinator.observe(
        _view(V5TPState.CLOSURE_TAIL, RolloverOutputOverlayV2(1, 0, 202)),
        path_time_s=TAIL_END_S - ROLLOVER_COMMIT_LEAD_S,
        monotonic_s=5.0,
    )
    assert commit.arm_commit is True
    assert commit.wire_input.command is RolloverCommand.COMMIT
    assert commit.wire_input.qdot_generation == 1

    acknowledged = coordinator.observe(
        _view(V5TPState.ROLLOVER_COMMITTED, RolloverOutputOverlayV2(1, 1, 0)),
        path_time_s=TAIL_END_S,
        monotonic_s=5.106,
    )
    assert acknowledged.acknowledge_commit is True
    pending = coordinator.resolve_echo(
        _view(
            V5TPState.ROLLOVER_COMMITTED,
            RolloverOutputOverlayV2(1, 1, 0),
            identity=successor,
        ),
        active_identity=current,
        prepared_identity=successor,
    )
    assert pending.kind is V5RolloverEchoKind.ACTIVATION_PENDING
    with pytest.raises(V5LiveOwnerError, match="activation-pending overlay"):
        coordinator.resolve_echo(
            _view(
                V5TPState.ROLLOVER_COMMITTED,
                RolloverOutputOverlayV2(2, 2, 0),
                identity=successor,
            ),
            active_identity=current,
            prepared_identity=successor,
        )
    assert acknowledged.wire_input.command is RolloverCommand.NONE
    assert coordinator.phase is V5RolloverPhase.COMMITTED


def test_rollover_coordinator_rejects_wrong_token_or_qdot_generation() -> None:
    coordinator = V5RolloverCoordinatorV1(
        _identity(1, 101),
        _identity(2, 202),
        generation=1,
    )
    coordinator.observe(
        _view(V5TPState.PATH, BaseOutputOverlayV2(1, V5AttemptKind.PRIMARY_NOVEL, 0)),
        path_time_s=1.0,
        monotonic_s=1.0,
    )
    with pytest.raises(V5LiveOwnerError, match="PREPARE acknowledgement"):
        coordinator.observe(
            _view(V5TPState.ROLLOVER_PREPARED, RolloverOutputOverlayV2(1, 0, 999)),
            path_time_s=2.0,
            monotonic_s=2.0,
        )

    coordinator = V5RolloverCoordinatorV1(
        _identity(1, 101),
        _identity(2, 202),
        generation=1,
    )
    coordinator.observe(
        _view(V5TPState.PATH, BaseOutputOverlayV2(1, V5AttemptKind.PRIMARY_NOVEL, 0)),
        path_time_s=1.0,
        monotonic_s=1.0,
    )
    coordinator.observe(
        _view(V5TPState.ROLLOVER_PREPARED, RolloverOutputOverlayV2(1, 0, 202)),
        path_time_s=2.0,
        monotonic_s=2.0,
    )
    coordinator.observe(
        _view(V5TPState.CLOSURE_TAIL, RolloverOutputOverlayV2(1, 0, 202)),
        path_time_s=TAIL_END_S - ROLLOVER_COMMIT_LEAD_S,
        monotonic_s=3.0,
    )
    with pytest.raises(V5LiveOwnerError, match="COMMIT acknowledgement"):
        coordinator.observe(
            _view(V5TPState.ROLLOVER_COMMITTED, RolloverOutputOverlayV2(1, 2, 0)),
            path_time_s=TAIL_END_S,
            monotonic_s=3.106,
        )

    runtime_active_identity = _identity(2, 202)
    with pytest.raises(V5LiveOwnerError, match="active rollover identity"):
        coordinator.resolve_echo(
            _view(
                V5TPState.ROLLOVER_COMMITTED,
                RolloverOutputOverlayV2(1, 1, 0),
                identity=runtime_active_identity,
            ),
            active_identity=runtime_active_identity,
            prepared_identity=_identity(2, 202),
        )

    runtime_prepared_identity = _identity(3, 303)
    with pytest.raises(V5LiveOwnerError, match="successor identity"):
        coordinator.resolve_echo(
            _view(
                V5TPState.ROLLOVER_COMMITTED,
                RolloverOutputOverlayV2(1, 1, 0),
                identity=_identity(2, 202),
            ),
            active_identity=_identity(1, 101),
            prepared_identity=runtime_prepared_identity,
        )

    spurious = V5RolloverCoordinatorV1(
        _identity(1, 101),
        _identity(2, 202),
        generation=1,
    )
    with pytest.raises(V5LiveOwnerError, match="prior activation"):
        spurious.resolve_echo(
            _view(
                V5TPState.ROLLOVER_COMMITTED,
                RolloverOutputOverlayV2(0, 0, 0),
                identity=_identity(1, 101),
            ),
            active_identity=_identity(1, 101),
            prepared_identity=_identity(2, 202),
        )

    post = V5RolloverCoordinatorV1(
        _identity(2, 202),
        _identity(3, 303),
        generation=2,
    )
    resolved = post.resolve_echo(
        _view(
            V5TPState.ROLLOVER_COMMITTED,
            RolloverOutputOverlayV2(1, 1, 0),
            identity=_identity(2, 202),
        ),
        active_identity=_identity(2, 202),
        prepared_identity=_identity(3, 303),
    )
    assert resolved.kind is V5RolloverEchoKind.POST_COMMIT_DRAIN
    assert resolved.expected_identity == _identity(2, 202)
    assert resolved.expected_generation == 1

    with pytest.raises(V5LiveOwnerError, match="overlay"):
        post.resolve_echo(
            _view(
                V5TPState.ROLLOVER_COMMITTED,
                RolloverOutputOverlayV2(2, 1, 0),
                identity=_identity(2, 202),
            ),
            active_identity=_identity(2, 202),
            prepared_identity=_identity(3, 303),
        )

    with pytest.raises(V5LiveOwnerError, match="successor echo"):
        coordinator.resolve_echo(
            _view(
                V5TPState.ROLLOVER_COMMITTED,
                RolloverOutputOverlayV2(1, 1, 0),
                identity=_identity(2, 999),
            ),
            active_identity=_identity(1, 101),
            prepared_identity=_identity(2, 202),
        )


def test_rollover_coordinator_without_successor_never_mints_a_command() -> None:
    coordinator = V5RolloverCoordinatorV1(
        _identity(5, 505),
        None,
        generation=5,
    )
    for index, (state, path_time) in enumerate((
        (V5TPState.PATH, 0.0),
        (V5TPState.CLOSURE_TAIL, PATH_END_S),
    )):
        decision = coordinator.observe(
            _view(state, BaseOutputOverlayV2(1, V5AttemptKind.PRIMARY_NOVEL, 0)),
            path_time_s=path_time,
            monotonic_s=float(index),
        )
        assert decision.wire_input.command is RolloverCommand.NONE
        assert decision.prepare_runtime is False
        assert decision.arm_commit is False
        assert decision.acknowledge_commit is False


def test_rollover_coordinator_rejects_commit_ack_after_eight_milliseconds_past_seam() -> None:
    current = _identity(1, 101)
    successor = _identity(2, 202)
    coordinator = V5RolloverCoordinatorV1(current, successor, generation=1)
    coordinator.observe(
        _view(V5TPState.PATH, BaseOutputOverlayV2(1, current.attempt_kind, 0)),
        path_time_s=1.0,
        monotonic_s=1.0,
    )
    coordinator.observe(
        _view(
            V5TPState.ROLLOVER_PREPARED,
            RolloverOutputOverlayV2(1, 0, successor.candidate_token),
        ),
        path_time_s=2.0,
        monotonic_s=2.0,
    )
    coordinator.observe(
        _view(
            V5TPState.CLOSURE_TAIL,
            RolloverOutputOverlayV2(1, 0, successor.candidate_token),
        ),
        path_time_s=TAIL_END_S - ROLLOVER_COMMIT_LEAD_S,
        monotonic_s=3.0,
    )
    with pytest.raises(V5LiveOwnerError, match="deadline exceeded"):
        coordinator.observe(
            _view(
                V5TPState.ROLLOVER_COMMITTED,
                RolloverOutputOverlayV2(1, 1, 0),
            ),
            path_time_s=TAIL_END_S,
            monotonic_s=3.0 + ROLLOVER_COMMIT_LEAD_S + 0.009,
        )


def test_rollover_coordinator_keeps_durable_intent_and_transport_delay_before_seam() -> None:
    current = _identity(1, 101)
    successor = _identity(2, 202)
    coordinator = V5RolloverCoordinatorV1(current, successor, generation=1)
    coordinator.observe(
        _view(V5TPState.PATH, BaseOutputOverlayV2(1, current.attempt_kind, 0)),
        path_time_s=1.0,
        monotonic_s=1.0,
    )
    coordinator.observe(
        _view(
            V5TPState.ROLLOVER_PREPARED,
            RolloverOutputOverlayV2(1, 0, successor.candidate_token),
        ),
        path_time_s=2.0,
        monotonic_s=2.0,
    )
    intent = coordinator.observe(
        _view(
            V5TPState.CLOSURE_TAIL,
            RolloverOutputOverlayV2(1, 0, successor.candidate_token),
        ),
        path_time_s=TAIL_END_S - ROLLOVER_COMMIT_LEAD_S,
        monotonic_s=3.0,
    )
    assert intent.arm_commit is True

    first_ack = coordinator.resolve_echo(
        _view(
            V5TPState.ROLLOVER_COMMITTED,
            RolloverOutputOverlayV2(1, 1, 0),
            identity=successor,
        ),
        active_identity=current,
        prepared_identity=successor,
    )
    assert first_ack.kind is V5RolloverEchoKind.FIRST_COMMIT_ACK
    assert first_ack.expected_identity == successor
    assert first_ack.expected_generation == 1

    # The failed live artifact measured a 16.406 ms durable-journal gap plus
    # a four-frame (about 8 ms) RTDE input pipeline.  Both are pre-seam work,
    # so neither may consume the unchanged 8 ms post-seam ACK deadline.
    holding = coordinator.observe(
        _view(
            V5TPState.ROLLOVER_PREPARED,
            RolloverOutputOverlayV2(1, 0, successor.candidate_token),
        ),
        path_time_s=TAIL_END_S - 0.075,
        monotonic_s=3.025,
    )
    assert holding.wire_input.command is RolloverCommand.COMMIT
    acknowledged = coordinator.observe(
        _view(
            V5TPState.ROLLOVER_COMMITTED,
            RolloverOutputOverlayV2(1, 1, 0),
        ),
        path_time_s=TAIL_END_S,
        monotonic_s=3.0 + ROLLOVER_COMMIT_LEAD_S + 0.006,
    )
    assert acknowledged.acknowledge_commit is True


def test_lifecycle_adapter_seals_effective_target_without_changing_sensor(
    tmp_path: Path,
) -> None:
    trace = LifecycleTrace(tmp_path, path_min_duration_s=0.001)
    adapter = V5LifecycleTraceAdapter(trace)
    adapter.begin_attempt(
        1,
        "effective-target",
        "PRIMARY_NOVEL",
        epoch=9,
        path_requested=False,
        capture_started_before_motion=True,
    )
    adapter.set_effective_target(5.75)
    output = SimpleNamespace(
        timestamp=1.0,
        observed_at_s=1.0,
        tcp_pose_m_rad=(0.0,) * 6,
        tcp_speed_m_s_rad_s=(0.0,) * 6,
        qd_rad_s=(0.0,) * 6,
    )
    sensor = SensorPacket(
        normal_load_n=2.0,
        force_norm_n=2.5,
        heartbeat=1.0,
        sensor_fresh=True,
        stop_request=False,
        eoat_get_ack=True,
        torque_norm_nm=0.1,
        wrench=(0.0, 0.0, -2.0, 0.0, 0.0, 0.1),
        filtered_normal_n=1.9,
    )
    assert adapter.observe_tick(
        monotonic_s=1.0,
        output=output,
        sensor=sensor,
        tp_state=int(V5TPState.ONE_NEWTON_ENTRY),
        setpoint_n=1.0,
        packet_sequence=0,
        consumed_packet_sequence=0,
    )
    receipt = adapter.finalize_attempt(terminal_state=90, home_verified=False)
    _, rows = load_lifecycle_artifact(Path(receipt["artifact_path"]))
    assert rows[0]["setpoint_n"] == pytest.approx(5.75)
    assert rows[0]["normal_load_n"] == pytest.approx(2.0)


def _candidate(motion_kp: float) -> CompleteCandidateV1:
    return CompleteCandidateV1(
        controller_path={
            "force_p_gain": 0.02,
            "force_damping": 100.0,
            "force_i_gain": 0.01,
            "i_off": False,
            "normal_filter_tau_s": 0.04,
            "orientation_ko": 0.04,
            "motion_kp": motion_kp,
        },
        correction_weights=(0.0,) * 6,
    )


def _plan(ordinal: int, motion_kp: float) -> V5TrialPlan:
    candidate = _candidate(motion_kp)
    key = candidate.candidate_key
    proposal = ProposalReceiptV2(
        ProposalMethodV2.FIXED_REPLAY,
        "controller_path",
        (),
        key,
        (),
        "a" * 64,
        CampaignRoleV2.PRIMARY,
        "whole-flow-fixture",
    )
    trial = f"whole-flow-{ordinal}"
    return V5TrialPlan(
        trial,
        trial + "-attempt",
        V5AttemptKind.PRIMARY_NOVEL,
        candidate,
        key,
        _candidate_token(key),
        ordinal,
        proposal,
        TrialStageV2.PRIMARY_NOVEL,
        False,
        True,
        V5_WIRE_EPOCH,
        ordinal,
        ordinal,
    )


def test_rollover_activation_journal_is_ordered_fsynced_and_cold_bound(
    tmp_path: Path,
) -> None:
    plans = (_plan(1, 2.0), _plan(2, 2.1))
    request = V5LiveChainRequestV1(
        "activation-chain",
        plans,
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "activation-result.json").resolve()),
    )
    journal = V5RolloverActivationJournalV1(request)
    old = candidate_identity_from_plan(plans[0])
    new = candidate_identity_from_plan(plans[1])
    with pytest.raises(V5LiveOwnerError, match="transition"):
        journal.append(
            "COMMIT_INTENT",
            generation=1,
            old_identity=old,
            next_identity=new,
            details={"premature": True},
        )
    for state in ("PREPARED", "COMMIT_INTENT", "COMMIT_ACK", "ACTIVATED"):
        journal.append(
            state,
            generation=1,
            old_identity=old,
            next_identity=new,
            details={"state": state},
        )
    receipt = journal.receipt(require_complete=True)
    assert receipt["complete"] is True
    assert receipt["activation_state_count"] == 4
    assert V5RolloverActivationJournalV1.verify_receipt(request, receipt) == receipt
    forged = dict(receipt)
    forged["journal_head_sha256"] = "f" * 64
    with pytest.raises(V5LiveOwnerError, match="receipt differs"):
        V5RolloverActivationJournalV1.verify_receipt(request, forged)


def test_activation_ack_and_activated_batch_uses_one_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = (_plan(1, 2.0), _plan(2, 2.1))
    request = V5LiveChainRequestV1(
        "activation-batch-chain",
        plans,
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "activation-batch-result.json").resolve()),
    )
    journal = V5RolloverActivationJournalV1(request)
    old = candidate_identity_from_plan(plans[0])
    new = candidate_identity_from_plan(plans[1])
    for state in ("PREPARED", "COMMIT_INTENT"):
        journal.append(
            state,
            generation=1,
            old_identity=old,
            next_identity=new,
            details={"state": state},
        )
    real_fsync = os.fsync
    calls: list[int] = []

    def counted_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", counted_fsync)
    receipt = journal.append_batch(
        (
            V5ActivationJournalEntryV1(
                "COMMIT_ACK", 1, old, new, {"state": "COMMIT_ACK"}
            ),
            V5ActivationJournalEntryV1(
                "ACTIVATED", 1, old, new, {"state": "ACTIVATED"}
            ),
        )
    )

    assert len(calls) == 1
    assert receipt.states == ("COMMIT_ACK", "ACTIVATED")
    assert receipt.journal_head_sha256 == receipt.row_sha256s[-1]
    assert journal.receipt(require_complete=True)["complete"] is True


def test_activation_durability_barrier_is_a_prewarmed_low_priority_process(
    tmp_path: Path,
) -> None:
    plans = (_plan(1, 2.0), _plan(2, 2.1))
    request = V5LiveChainRequestV1(
        "activation-process-isolation",
        plans,
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "activation-process-result.json").resolve()),
    )
    journal = V5RolloverActivationJournalV1(request)
    old = candidate_identity_from_plan(plans[0])
    new = candidate_identity_from_plan(plans[1])
    for state in ("PREPARED", "COMMIT_INTENT"):
        journal.append(
            state,
            generation=1,
            old_identity=old,
            next_identity=new,
            details={"state": state},
        )
    barrier = V5ActivationDurabilityBarrierV1(journal)
    try:
        assert barrier.worker_pid != os.getpid()
        assert barrier.worker_nice == 19
        assert not any(
            thread.name.startswith("v5-activation-durability")
            for thread in threading.enumerate()
        )
        barrier.submit(
            (
                V5ActivationJournalEntryV1(
                    "COMMIT_ACK", 1, old, new, {"state": "COMMIT_ACK"}
                ),
                V5ActivationJournalEntryV1(
                    "ACTIVATED", 1, old, new, {"state": "ACTIVATED"}
                ),
            )
        )
        deadline = time.monotonic() + 2.0
        settlement = None
        while settlement is None and time.monotonic() < deadline:
            settlement = barrier.settle_if_ready()
            time.sleep(0.001)
        assert settlement is not None
        assert settlement.batch.states == ("COMMIT_ACK", "ACTIVATED")
    finally:
        barrier.close()
    assert journal.receipt(require_complete=True)["complete"] is True
class _Backend:
    def __init__(self) -> None:
        self.filtered_normal_n = 1.0
        self.seeded: tuple[float, tuple[float, ...]] | None = None

    def observe_filter(
        self,
        *,
        raw_normal_n: float,
        actual_dt_s: float,
        setpoint_n: float,
        mode: str,
    ) -> float:
        del actual_dt_s, setpoint_n, mode
        self.filtered_normal_n = float(raw_normal_n)
        return self.filtered_normal_n

    def command(self, **_kwargs: object) -> V5BackendCommandV1:
        return V5BackendCommandV1(
            self.filtered_normal_n,
            (0.0,) * 6,
            {
                "guard_stack": {
                    "terminal_stop": False,
                    "soft": {"fail_closed": False},
                },
                "qdot": {"allowed": True, "reason": "fixture"},
            },
        )

    def seed_physical_continuity(
        self,
        *,
        filtered_normal_n: float,
        previous_qdot: tuple[float, ...],
    ) -> None:
        self.filtered_normal_n = float(filtered_normal_n)
        self.seeded = (self.filtered_normal_n, tuple(previous_qdot))

    def validate_activation_hold(self, **kwargs: object) -> dict[str, object]:
        assert tuple(kwargs["qdot"]) == tuple(kwargs["qdot"])
        return {
            "guard_stack": {
                "terminal_stop": False,
                "soft": {"fail_closed": False},
            },
            "qdot": {"allowed": True, "reason": "activation-hold-fixture"},
            "activation_pending": {
                "seed_exact": True,
                "candidate_backend_executed": False,
            },
        }


class _VaryingBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.command_count = 0

    def command(self, **_kwargs: object) -> V5BackendCommandV1:
        self.command_count += 1
        value = self.command_count * 1e-7
        return V5BackendCommandV1(
            self.filtered_normal_n,
            (value,) * 6,
            {
                "guard_stack": {
                    "terminal_stop": False,
                    "soft": {"fail_closed": False},
                },
                "qdot": {"allowed": True, "reason": "varying-fixture"},
            },
        )


class _Transport:
    def __init__(self) -> None:
        from step6_figure8_autotune_v1.v5_composition_contract import V5RolloverInput

        self.rollover_input = V5RolloverInput()
        self.latest_v5: V5OutputSnapshotV1 | None = None
        self.active_attempt_kind = V5AttemptKind.PRIMARY_NOVEL

    def bind_attempt_kind(self, kind: V5AttemptKind) -> None:
        self.active_attempt_kind = kind

    def set_rollover_input(self, value: object) -> None:
        self.rollover_input = value


class _Session:
    def __init__(self) -> None:
        self.finished = False

    def finish_attempt(self, decision: object) -> None:
        assert getattr(decision, "passed") is True
        self.finished = True


class _Writer:
    DT = 0.002

    def __init__(
        self,
        transport: _Transport,
        *,
        commit_ack_delay_frames: int = 0,
        rollover_input_delay_frames: int = 0,
        trace_send_latency_s: float = 0.0,
        timing_gap_at_frame: int | None = None,
    ) -> None:
        self.transport = transport
        self.fresh_frame_wait_policy = SimpleNamespace(wait_s=0.0)
        self.session = _Session()
        self._home = SimpleNamespace(pose=(0.0,) * 6, q=(0.0,) * 6)
        self._entry_linear_speed_m_s = 0.0
        self._entry_angular_speed_rad_s = 0.0
        self._packet_sequence = 0
        self._ordinal = 1
        self._candidate_token = 1
        self._controller_ordinal = 1
        self._controller_candidate_token = 1
        self._last_poll_was_fresh = True
        self._last_output: R004OutputSnapshot | None = None
        self._r013_lifecycle_trace: V5LifecycleTraceAdapter | None = None
        self._sticky_latched = 0
        self._now = 0.0
        self._startup = [
            V5TPState.ARMING,
            V5TPState.CONTACT_ACQUISITION,
        ]
        self._path_origin: float | None = None
        self._prepared_generation = 0
        self._prepared_token = 0
        self._active_qdot_generation = 0
        self._commit_ack_delay_frames = int(commit_ack_delay_frames)
        self._rollover_input_queue = [
            V5RolloverInput()
            for _ in range(int(rollover_input_delay_frames))
        ]
        self._trace_send_latency_s = float(trace_send_latency_s)
        self._timing_gap_at_frame = timing_gap_at_frame
        self._inside_arm = False
        self._commit_generation_seen = 0
        self._commit_delay_remaining = 0
        self._returning = False
        self._terminal = False
        self.failed_closed: str | None = None
        self.fresh_frame_count = 0
        self.gc_enabled_during_poll: list[bool] = []
        self.sent_packets: list[tuple[V5TPState, RolloverCommand, tuple[float, ...]]] = []
        self.sent_packet_echoes: list[
            tuple[V5TPState, RolloverCommand, tuple[float, ...], tuple[int, int, int]]
        ] = []
        self.sent_packet_authority: list[
            tuple[float, int, int, RolloverCommand, tuple[float, ...]]
        ] = []

    def _mono_clock(self) -> float:
        return self._now

    def arm_unbounded(self, **kwargs: object) -> SimpleNamespace:
        self._ordinal = int(kwargs["ordinal"])
        self._candidate_token = int(kwargs["candidate_token"])
        self._controller_ordinal = self._ordinal
        self._controller_candidate_token = self._candidate_token
        view = self._snapshot(
            V5TPState.ARMING,
            BaseOutputOverlayV2(1, self.transport.active_attempt_kind, 0),
        )
        self.transport.latest_v5 = view
        self._last_output = view.r004_snapshot
        self._inside_arm = True
        try:
            packet = self._send_packet(self._read_sensor(), command_mode=0)
        finally:
            self._inside_arm = False
        return SimpleNamespace(output=view.r004_snapshot, packet=packet)

    def _snapshot(
        self,
        state: V5TPState,
        overlay: BaseOutputOverlayV2 | RolloverOutputOverlayV2,
    ) -> V5OutputSnapshotV1:
        snapshot = R004OutputSnapshot(
            observed_at_s=self._now,
            timestamp=self._now,
            payload_kg=1.0,
            payload_cog_m=(0.0,) * 3,
            tcp_offset_m_rad=(0.0,) * 6,
            tcp_speed_m_s_rad_s=(0.0,) * 6,
            tcp_pose_m_rad=(0.0,) * 6,
            q_rad=(0.0,) * 6,
            qd_rad_s=(0.0,) * 6,
            safety_mode="NORMAL",
            robot_mode="RUNNING",
            runtime_state=2,
            consumed_packet_sequence=max(-1, self._packet_sequence - 1),
            integer_echoes={
                24: V5_WIRE_EPOCH,
                25: self._controller_ordinal,
                26: int(state),
                27: self._controller_candidate_token,
                28: 0,
                **overlay.by_register,
                32: 607007,
                33: 5,
                34: 520607,
            },
        )
        return V5OutputSnapshotV1(snapshot, state, overlay)

    def _poll_checked(self, **_kwargs: object) -> R004OutputSnapshot:
        # The production RTDE receive releases the GIL.  Mirror that scheduler
        # boundary so the prewarmed durability worker can run in this fake.
        time.sleep(0)
        if (
            self._last_output is not None
            and self._last_output.integer_echoes[26]
            == int(V5TPState.ROLLOVER_COMMITTED)
            and self.transport.rollover_input.command
            is RolloverCommand.COMMIT
        ):
            # Unlike the accelerated path fixture, a real RTDE receive cannot
            # advance 62 s of controller time while a sub-second fsync worker
            # gets one scheduler slice.  Preserve that real-time boundary only
            # for the staged activation interval.
            time.sleep(0.01)
        elif (
            self._last_output is not None
            and self._last_output.integer_echoes[26]
            == int(V5TPState.ROLLOVER_PREPARED)
        ):
            time.sleep(0.01)
        elif (
            self._path_origin is not None
            and self._prepared_token > 0
            and self._now - self._path_origin >= 62.70
        ):
            # COMMIT_INTENT is durable before its deferred wire publication.
            # Keep the accelerated fake from consuming the real 100 ms lead in
            # less than one filesystem scheduler slice.
            time.sleep(0.01)
        self.gc_enabled_during_poll.append(gc.isenabled())
        self._now += self.DT
        if self.fresh_frame_count == self._timing_gap_at_frame:
            self._now += 0.03
        if self._startup:
            state = self._startup.pop(0)
            overlay: BaseOutputOverlayV2 | RolloverOutputOverlayV2 = (
                BaseOutputOverlayV2(1, self.transport.active_attempt_kind, 0)
            )
        elif self._terminal:
            state = V5TPState.READY_HOME_NEXT
            overlay = BaseOutputOverlayV2(1, self.transport.active_attempt_kind, 127)
        elif self._returning:
            self._terminal = True
            state = V5TPState.RETURNING_HOME
            overlay = BaseOutputOverlayV2(1, self.transport.active_attempt_kind, 127)
        else:
            if self._path_origin is None:
                self._path_origin = self._now
            elapsed = self._now - self._path_origin
            self._rollover_input_queue.append(self.transport.rollover_input)
            rollover = self._rollover_input_queue.pop(0)
            command = getattr(rollover, "command")
            if int(command) == int(RolloverCommand.PREPARE):
                self._prepared_generation = int(getattr(rollover, "generation"))
                self._prepared_token = int(getattr(rollover, "next_candidate_token"))
                state = V5TPState.ROLLOVER_PREPARED
                overlay = RolloverOutputOverlayV2(
                    self._prepared_generation,
                    self._active_qdot_generation,
                    self._prepared_token,
                )
            elif (
                int(command) == int(RolloverCommand.COMMIT)
                and int(getattr(rollover, "generation"))
                == self._active_qdot_generation
                and self._prepared_token == 0
            ):
                # The real resident keeps state28 visible until the host's
                # NONE packet has drained the RTDE input pipeline.
                state = V5TPState.ROLLOVER_COMMITTED
                overlay = RolloverOutputOverlayV2(
                    self._active_qdot_generation,
                    self._active_qdot_generation,
                    0,
                )
            elif (
                int(command) == int(RolloverCommand.COMMIT)
                and elapsed >= 62.83185307179586
            ):
                generation = int(getattr(rollover, "generation"))
                if generation != self._commit_generation_seen:
                    self._commit_generation_seen = generation
                    self._commit_delay_remaining = self._commit_ack_delay_frames
                if self._commit_delay_remaining > 0:
                    self._commit_delay_remaining -= 1
                    state = V5TPState.ROLLOVER_PREPARED
                    overlay = RolloverOutputOverlayV2(
                        self._prepared_generation,
                        self._active_qdot_generation,
                        self._prepared_token,
                    )
                else:
                    self._active_qdot_generation = generation
                    self._controller_ordinal = int(
                        getattr(rollover, "next_attempt_ordinal")
                    )
                    self._controller_candidate_token = int(
                        getattr(rollover, "next_candidate_token")
                    )
                    state = V5TPState.ROLLOVER_COMMITTED
                    overlay = RolloverOutputOverlayV2(generation, generation, 0)
                    self._path_origin = self._now
                    self._prepared_generation = 0
                    self._prepared_token = 0
            elif elapsed >= 62.83185307179586:
                self._returning = True
                state = V5TPState.RETURNING_HOME
                overlay = BaseOutputOverlayV2(1, self.transport.active_attempt_kind, 127)
            elif elapsed >= 60.0:
                state = V5TPState.CLOSURE_TAIL
                overlay = RolloverOutputOverlayV2(
                    self._prepared_generation,
                    self._active_qdot_generation,
                    self._prepared_token,
                )
            else:
                state = V5TPState.PATH
                overlay = BaseOutputOverlayV2(1, self.transport.active_attempt_kind, 0)
        view = self._snapshot(state, overlay)
        self.transport.latest_v5 = view
        self._last_output = view.r004_snapshot
        self._last_poll_was_fresh = True
        self.fresh_frame_count += 1
        return view.r004_snapshot

    def _read_sensor(self) -> SensorPacket:
        path = self._last_output is not None and self._last_output.integer_echoes[26] in {
            25,
            26,
            27,
            28,
        }
        normal = 5.0 if path else 1.0
        return SensorPacket(
            normal,
            normal,
            float(self.fresh_frame_count),
            True,
            False,
            True,
            0.1,
            (0.0, 0.0, -normal, 0.0, 0.0, 0.1),
            normal,
        )

    def _send_packet(
        self,
        sensor: SensorPacket,
        *,
        command_mode: object,
        proposed_qdot: tuple[float, ...] = (0.0,) * 6,
        internal_setpoint_n: float = 1.0,
    ) -> SimpleNamespace:
        assert self._last_output is not None
        self.sent_packets.append(
            (
                V5TPState(self._last_output.integer_echoes[26]),
                self.transport.rollover_input.command,
                tuple(proposed_qdot),
            )
        )
        self.sent_packet_echoes.append(
            (
                V5TPState(self._last_output.integer_echoes[26]),
                self.transport.rollover_input.command,
                tuple(proposed_qdot),
                (
                    int(self._last_output.integer_echoes[24]),
                    int(self._last_output.integer_echoes[25]),
                    int(self._last_output.integer_echoes[27]),
                ),
            )
        )
        self.sent_packet_authority.append(
            (
                self._now,
                self._ordinal,
                self._candidate_token,
                self.transport.rollover_input.command,
                tuple(proposed_qdot),
            )
        )
        sequence = self._packet_sequence
        self._packet_sequence += 1
        trace = self._r013_lifecycle_trace
        assert trace is not None
        trace.observe_tick(
            monotonic_s=(
                self._now
                if self._inside_arm
                else self._now + self._trace_send_latency_s
            ),
            output=self._last_output,
            sensor=sensor,
            tp_state=self._last_output.integer_echoes[26],
            command_mode=int(command_mode),
            qdot=proposed_qdot,
            setpoint_n=internal_setpoint_n,
            packet_sequence=sequence,
            consumed_packet_sequence=self._last_output.consumed_packet_sequence,
        )
        return SimpleNamespace(sequence=sequence)

    def _fail_closed(self, reason: str) -> None:
        self.failed_closed = reason


def test_single_writer_owner_whole_flow_rollover_seals_two_contiguous_slices(
    tmp_path: Path,
) -> None:
    assert gc.isenabled() is True
    transport = _Transport()
    writer = _Writer(transport, trace_send_latency_s=0.0004)
    trace = V5LifecycleTraceAdapter(
        LifecycleTrace(
            tmp_path / "life",
            path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
        )
    )
    backends: list[_Backend] = []
    constructed_at_frame: list[int] = []

    def backend_factory(_spec: object) -> _Backend:
        constructed_at_frame.append(writer.fresh_frame_count)
        # Reproduce the real Pinocchio/URDF constructor cost in the fake
        # controller clock.  Any lazy post-ARM construction must therefore
        # trip the unchanged <20 ms TimingGuard instead of being hidden by an
        # accelerated unit-test loop.
        if writer.fresh_frame_count > 0:
            writer._now += 0.039
        backend = _Backend()
        backends.append(backend)
        return backend

    context = V5LiveContextV1(
        writer=writer,
        transport=transport,  # type: ignore[arg-type]
        trace=trace,
        backend_factory=backend_factory,
        anchor_pose=(0.0,) * 6,
        close_callback=lambda: None,
    )
    request = V5LiveChainRequestV1(
        "whole-flow-chain",
        (_plan(1, 2.0), _plan(2, 2.1)),
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "whole-flow-result.json").resolve()),
        timeout_s=300.0,
    )
    result = V5SingleWriterOwnerV1(context).execute_chain(request)

    assert gc.isenabled() is True
    assert writer.gc_enabled_during_poll
    assert not any(writer.gc_enabled_during_poll)
    assert writer.failed_closed is None
    assert writer.session.finished is True
    assert len(result.records) == 2
    assert len(result.chain.attempts) == 2
    first, second = result.chain.attempts
    assert first.sample_end_index == second.sample_start_index
    assert first.boundary.mode.value == "CONTACT_ROLLOVER"
    assert second.boundary.mode.value == "HOME"
    assert first.metric_snapshot.formal_mae_n == pytest.approx(0.0)
    assert second.metric_snapshot.formal_mae_n == pytest.approx(0.0)
    assert result.lifecycle_receipt["coverage_complete"] is True
    assert backends[1].seeded == (5.0, (0.0,) * 6)
    sidecar_path = Path(
        result.event_bundle["gate_observation_artifact"]["artifact_path"]
    )
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["timing_count"] > sidecar["observation_count"]
    _metadata, lifecycle_rows = load_lifecycle_artifact(
        Path(result.lifecycle_receipt["artifact_path"])
    )
    timing_samples = [int(row[0]) for row in sidecar["timing_rows"]]
    assert [lifecycle_rows[index]["tp_state"] for index in timing_samples[:2]] == [
        int(V5TPState.ARMING),
        int(V5TPState.CONTACT_ACQUISITION),
    ]
    marker = lifecycle_rows[timing_samples[0] - 1]
    assert not (int(marker["flags"]) & FLAG_COMMAND_PRESENT)
    assert marker["packet_sequence"] == -1
    cold_timing = result.records[0].closure.evidence.timing_acceptance
    for field in ("passed", "rate_hz", "p99_gap_s", "max_gap_s", "stop_reason"):
        assert sidecar["runtime_timing_acceptance"][field] == cold_timing[field]


def test_single_writer_owner_delegates_lease_entry_to_mature_arm_and_releases_at_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Lease:
        def __init__(self, _profile: object) -> None:
            self.entered = False
            self.restored = False

        def enter(self) -> dict[str, object]:
            events.append("lease.enter")
            self.entered = True
            return self.receipt()

        def exit(self) -> dict[str, object]:
            events.append("lease.exit")
            self.restored = True
            return self.receipt()

        def receipt(self) -> dict[str, object]:
            return {
                "status": "restored" if self.restored else "entered",
                "restore_verified": self.restored,
            }

    class MatureSeamWriter(_Writer):
        def __init__(self, transport: _Transport) -> None:
            super().__init__(transport)
            self._test_lease: Lease | None = None

        def prepare_timing_scheduler_lease(self) -> None:
            events.append("gc.prepare")
            self._prearm_gc_collected = True

        def install_timing_scheduler_lease(self, lease: Lease) -> None:
            assert self._test_lease is None
            assert lease.entered is False
            events.append("lease.install")
            self._test_lease = lease

        def arm_unbounded(self, **kwargs: object) -> SimpleNamespace:
            assert self._test_lease is not None
            assert self._test_lease.entered is False
            events.append("prearm.output")
            self._test_lease.enter()
            events.append("arm.mutation")
            return super().arm_unbounded(**kwargs)

        def release_timing_scheduler_lease(self) -> dict[str, object] | None:
            lease = self._test_lease
            self._test_lease = None
            events.append("writer.release")
            return None if lease is None else lease.exit()

    monkeypatch.setattr(live_owner_module, "FormalTimingSchedulerLeaseV2", Lease)
    transport = _Transport()
    writer = MatureSeamWriter(transport)
    trace = V5LifecycleTraceAdapter(
        LifecycleTrace(
            tmp_path / "life",
            path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
        )
    )
    request = V5LiveChainRequestV1(
        "writer-owned-timing-lease",
        (_plan(1, 2.0),),
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "result.json").resolve()),
        timeout_s=150.0,
    )
    context = V5LiveContextV1(
        writer=writer,
        transport=transport,  # type: ignore[arg-type]
        trace=trace,
        backend_factory=lambda _spec: _Backend(),
        anchor_pose=(0.0,) * 6,
        close_callback=lambda: None,
    )

    result = V5SingleWriterOwnerV1(context).execute_chain(request)

    assert events == [
        "gc.prepare",
        "lease.install",
        "prearm.output",
        "lease.enter",
        "arm.mutation",
        "writer.release",
        "lease.exit",
    ]
    assert result.lifecycle_receipt["timing_scheduler"]["restore_verified"] is True


def test_single_writer_owner_preserves_timing_failure_detail_and_restores_gc(
    tmp_path: Path,
) -> None:
    assert gc.isenabled() is True
    transport = _Transport()
    writer = _Writer(transport, timing_gap_at_frame=5000)
    trace = V5LifecycleTraceAdapter(
        LifecycleTrace(
            tmp_path / "life",
            path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
        )
    )
    context = V5LiveContextV1(
        writer=writer,
        transport=transport,  # type: ignore[arg-type]
        trace=trace,
        backend_factory=lambda _spec: _Backend(),
        anchor_pose=(0.0,) * 6,
        close_callback=lambda: None,
    )
    request = V5LiveChainRequestV1(
        "timing-gap-chain",
        (_plan(1, 2.0),),
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "never-sealed.json").resolve()),
        timeout_s=150.0,
    )

    with pytest.raises(V5LiveOwnerError, match="gate-family closure") as caught:
        V5SingleWriterOwnerV1(context).execute_chain(request)

    assert gc.isenabled() is True
    assert writer.gc_enabled_during_poll
    assert not any(writer.gc_enabled_during_poll)
    assert writer.failed_closed == str(caught.value)
    assert '"passed": false' in str(caught.value)
    assert '"max_gap_s": 0.032' in str(caught.value)
    receipt_path = next((tmp_path / "life").glob("*.r013life.json"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["errors"] == [f"V5 live chain failed: {caught.value}"]


@pytest.mark.parametrize(
    ("motion_kp_values", "expected_rollovers"),
    (
        ((2.0, 2.0), 1),
        ((2.0, 2.1, 2.0, 2.1, 2.0), 4),
    ),
)
def test_single_writer_owner_capability_chains_accept_same_candidate_and_four_rollovers(
    tmp_path: Path,
    motion_kp_values: tuple[float, ...],
    expected_rollovers: int,
) -> None:
    transport = _Transport()
    writer = _Writer(transport)
    trace = V5LifecycleTraceAdapter(
        LifecycleTrace(
            tmp_path / "life",
            path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
        )
    )
    backends: list[_Backend] = []

    def backend_factory(_spec: object) -> _Backend:
        backend = _Backend()
        backends.append(backend)
        return backend

    context = V5LiveContextV1(
        writer=writer,
        transport=transport,  # type: ignore[arg-type]
        trace=trace,
        backend_factory=backend_factory,
        anchor_pose=(0.0,) * 6,
        close_callback=lambda: None,
    )
    plans = tuple(
        _plan(index, motion_kp)
        for index, motion_kp in enumerate(motion_kp_values, start=1)
    )
    request = V5LiveChainRequestV1(
        f"capability-{expected_rollovers}-rollovers",
        plans,
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "result.json").resolve()),
        timeout_s=800.0,
    )
    result = V5SingleWriterOwnerV1(context).execute_chain(request)

    assert writer.failed_closed is None
    assert writer.session.finished is True
    assert len(result.records) == len(plans)
    assert result.activation_receipt is not None
    assert result.activation_receipt["complete"] is True
    assert result.activation_receipt["rollover_count"] == expected_rollovers
    assert result.activation_receipt["activation_state_count"] == 4 * expected_rollovers
    assert [record.boundary.mode for record in result.records[:-1]] == [
        BoundaryMode.CONTACT_ROLLOVER
    ] * expected_rollovers
    assert result.records[-1].boundary.mode is BoundaryMode.HOME
    assert all(record.eligible for record in result.records)
    assert all(backend.seeded is not None for backend in backends[1:])


def test_backend_prebuild_failure_occurs_before_arm_trace_or_wire_publish(
    tmp_path: Path,
) -> None:
    transport = _Transport()
    writer = _Writer(transport)
    life_dir = tmp_path / "life"
    trace = V5LifecycleTraceAdapter(
        LifecycleTrace(
            life_dir,
            path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
        )
    )
    constructed: list[int] = []

    def failing_factory(_spec: object) -> _Backend:
        constructed.append(len(constructed) + 1)
        if len(constructed) == 3:
            raise V5LiveOwnerError("injected backend prebuild failure")
        return _Backend()

    context = V5LiveContextV1(
        writer=writer,
        transport=transport,  # type: ignore[arg-type]
        trace=trace,
        backend_factory=failing_factory,
        anchor_pose=(0.0,) * 6,
        close_callback=lambda: None,
    )
    request = V5LiveChainRequestV1(
        "backend-prebuild-failure",
        tuple(_plan(index, 2.0 + index * 0.1) for index in range(1, 6)),
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "never-created.json").resolve()),
        timeout_s=800.0,
    )

    with pytest.raises(V5LiveOwnerError, match="prebuild failure"):
        V5SingleWriterOwnerV1(context).execute_chain(request)

    assert constructed == [1, 2, 3]
    assert writer.fresh_frame_count == 0
    assert writer.sent_packets == []
    assert writer._last_output is None
    assert trace.active is False
    assert not list(life_dir.glob("*.r013life*"))
    assert transport.rollover_input == V5RolloverInput()


def test_prebuilt_backend_cache_rejects_out_of_order_duplicate_and_exhaustion() -> None:
    specs = tuple(runtime_spec_from_plan(_plan(index, 2.0 + index * 0.1)) for index in range(1, 4))
    cache = V5PrebuiltBackendCacheV1(specs, lambda _spec: _Backend())

    with pytest.raises(V5LiveOwnerError, match="order/full-spec"):
        cache.take(specs[1])
    assert cache.take(specs[0]) is not None
    with pytest.raises(V5LiveOwnerError, match="order/full-spec"):
        cache.take(specs[0])
    assert cache.take(specs[1]) is not None
    assert cache.take(specs[2]) is not None
    with pytest.raises(V5LiveOwnerError, match="exhausted"):
        cache.take(specs[2])


def test_single_writer_stages_four_rollovers_while_activation_fsync_is_slow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport()
    writer = _Writer(
        transport,
        commit_ack_delay_frames=0,
        rollover_input_delay_frames=4,
    )
    original_settle = V5ActivationDurabilityBarrierV1.settle_if_ready
    release_at: dict[tuple[int, int], float] = {}

    def settle_after_measured_durable_delay(
        barrier: V5ActivationDurabilityBarrierV1,
    ) -> object:
        sequence = barrier._pending_sequence
        if sequence is None:
            return original_settle(barrier)
        key = (id(barrier), sequence)
        deadline = release_at.setdefault(
            key,
            time.monotonic() + 0.047876683,
        )
        if time.monotonic() < deadline:
            return None
        result = original_settle(barrier)
        if result is not None:
            release_at.pop(key, None)
        return result

    monkeypatch.setattr(
        V5ActivationDurabilityBarrierV1,
        "settle_if_ready",
        settle_after_measured_durable_delay,
    )
    trace = V5LifecycleTraceAdapter(
        LifecycleTrace(
            tmp_path / "life",
            path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
        )
    )

    context = V5LiveContextV1(
        writer=writer,
        transport=transport,  # type: ignore[arg-type]
        trace=trace,
        backend_factory=lambda _spec: _VaryingBackend(),
        anchor_pose=(0.0,) * 6,
        close_callback=lambda: None,
    )
    request = V5LiveChainRequestV1(
        "endpoint-ack-latency",
        tuple(_plan(index, 2.0 + index * 0.1) for index in range(1, 6)),
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "result.json").resolve()),
        timeout_s=800.0,
    )
    result = V5SingleWriterOwnerV1(context).execute_chain(request)

    assert writer.failed_closed is None
    assert writer.session.finished is True
    assert len(result.records) == 5
    assert len(result.chain.attempts) == 5
    assert result.lifecycle_receipt["coverage_profile"] == (
        LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH
    )
    assert result.lifecycle_receipt["coverage_complete"] is True
    assert result.records[-1].boundary.mode is BoundaryMode.HOME
    assert result.activation_receipt is not None
    assert result.activation_receipt["complete"] is True
    assert result.activation_receipt["rollover_count"] == 4
    assert result.activation_receipt["activation_state_count"] == 16
    _cold_receipt, activation_rows = V5RolloverActivationJournalV1.verify_receipt_with_rows(
        request,
        result.activation_receipt,
    )
    assert [row["state"] for row in activation_rows] == [
        "PREPARED",
        "COMMIT_INTENT",
        "COMMIT_ACK",
        "ACTIVATED",
    ] * 4
    assert [row["generation"] for row in activation_rows] == [
        generation
        for generation in range(1, 5)
        for _state in range(4)
    ]
    assert [
        record.candidate_identity for record in result.records
    ] == [candidate_identity_from_plan(plan) for plan in request.plans]
    assert all(record.eligible for record in result.records)
    assert all(record.metric_snapshot.formal_mae_n == pytest.approx(0.0) for record in result.records)
    assert all(record.metric_snapshot.closure.sealed for record in result.records)
    assert all(record.closure.all_passed for record in result.records)
    assert all(
        record.closure.evidence is not None
        and record.closure.evidence.timing_acceptance["passed"] is True
        for record in result.records
    )
    assert len(result.event_bundle["switch_gate_receipts"]) == 4
    assert result.records[0].closure.evidence is not None
    assert result.records[0].closure.evidence.timing_acceptance["max_gap_s"] < 0.020
    publications = result.activation_receipt["publication_receipts"]
    assert len(publications) == 4
    _metadata, rows = load_lifecycle_artifact(
        Path(result.lifecycle_receipt["artifact_path"])
    )
    for generation, publication in enumerate(publications, start=1):
        old_identity = candidate_identity_from_plan(request.plans[generation - 1])
        next_identity = candidate_identity_from_plan(request.plans[generation])
        switch = result.event_bundle["switch_gate_receipts"][generation - 1]
        seed = tuple(float(value) for value in switch["commit_seed_qdot"])
        pending_samples = publication["pending_sample_indices"]
        assert len(pending_samples) >= 2
        assert publication["durability_settled_monotonic_s"] < publication[
            "first_successor_publish_monotonic_s"
        ]
        for sample_index in pending_samples:
            row = rows[sample_index]
            sequence = int(row["packet_sequence"])
            _clock, host_ordinal, host_token, command, qdot = (
                writer.sent_packet_authority[sequence]
            )
            assert row["tp_state"] == int(V5TPState.ROLLOVER_COMMITTED)
            assert host_ordinal == old_identity.ordinal
            assert host_token == old_identity.candidate_token
            assert command is RolloverCommand.COMMIT
            assert qdot == seed
        first_index = publication["first_successor_sample_index"]
        first_row = rows[first_index]
        sequence = int(first_row["packet_sequence"])
        _clock, host_ordinal, host_token, command, _qdot = (
            writer.sent_packet_authority[sequence]
        )
        assert first_index == pending_samples[-1] + 1
        assert host_ordinal == next_identity.ordinal
        assert host_token == next_identity.candidate_token
        assert command is RolloverCommand.NONE


@pytest.mark.parametrize("failure_mode", ("fsync_error", "partial_write"))
def test_activation_durability_failure_never_publishes_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    transport = _Transport()
    writer = _Writer(transport)
    original_submit = V5ActivationDurabilityBarrierV1.submit

    def submit_to_broken_storage(
        barrier: V5ActivationDurabilityBarrierV1,
        entries: object,
        *,
        deferred_rollover_input: V5RolloverInput | None = None,
    ) -> None:
        states = tuple(entry.state for entry in entries)  # type: ignore[union-attr]
        if states == ("COMMIT_ACK", "ACTIVATED"):
            if failure_mode == "partial_write":
                with barrier.journal.path.open("ab") as stream:
                    stream.write(b'{"partial":')
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                barrier.journal.path.chmod(0o444)
        original_submit(
            barrier,
            entries,  # type: ignore[arg-type]
            deferred_rollover_input=deferred_rollover_input,
        )

    monkeypatch.setattr(
        V5ActivationDurabilityBarrierV1,
        "submit",
        submit_to_broken_storage,
    )
    trace = V5LifecycleTraceAdapter(
        LifecycleTrace(
            tmp_path / "life",
            path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
        )
    )
    request = V5LiveChainRequestV1(
        f"activation-{failure_mode}",
        (_plan(1, 2.0), _plan(2, 2.1)),
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "result.json").resolve()),
        timeout_s=300.0,
    )
    context = V5LiveContextV1(
        writer=writer,
        transport=transport,  # type: ignore[arg-type]
        trace=trace,
        backend_factory=lambda _spec: _Backend(),
        anchor_pose=(0.0,) * 6,
        close_callback=lambda: None,
    )

    with pytest.raises(V5LiveOwnerError, match="durability worker failed"):
        V5SingleWriterOwnerV1(context).execute_chain(request)

    successor = candidate_identity_from_plan(request.plans[1])
    assert writer.failed_closed is not None
    assert not any(
        host_ordinal == successor.ordinal
        and host_token == successor.candidate_token
        and command is RolloverCommand.NONE
        for _clock, host_ordinal, host_token, command, _qdot
        in writer.sent_packet_authority
    )
    assert not any(
        thread.name.startswith("v5-activation-durability")
        for thread in threading.enumerate()
    )


def test_activation_durability_timeout_never_publishes_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport()
    writer = _Writer(transport)
    original_submit = V5ActivationDurabilityBarrierV1.submit

    def submit_to_stopped_worker(
        barrier: V5ActivationDurabilityBarrierV1,
        entries: object,
        *,
        deferred_rollover_input: V5RolloverInput | None = None,
    ) -> None:
        states = tuple(entry.state for entry in entries)  # type: ignore[union-attr]
        if states == ("COMMIT_ACK", "ACTIVATED"):
            os.kill(barrier.worker_pid, signal.SIGSTOP)
        original_submit(
            barrier,
            entries,  # type: ignore[arg-type]
            deferred_rollover_input=deferred_rollover_input,
        )
        if states == ("COMMIT_ACK", "ACTIVATED"):
            barrier._submitted_at_s = time.monotonic() - 0.021

    def short_barrier(
        journal: V5RolloverActivationJournalV1,
    ) -> V5ActivationDurabilityBarrierV1:
        return V5ActivationDurabilityBarrierV1(journal, timeout_s=0.02)

    monkeypatch.setattr(
        V5ActivationDurabilityBarrierV1,
        "submit",
        submit_to_stopped_worker,
    )
    monkeypatch.setattr(
        live_owner_module, "V5ActivationDurabilityBarrierV1", short_barrier
    )
    trace = V5LifecycleTraceAdapter(
        LifecycleTrace(
            tmp_path / "life",
            path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
        )
    )
    request = V5LiveChainRequestV1(
        "activation-timeout",
        (_plan(1, 2.0), _plan(2, 2.1)),
        "a" * 64,
        "b" * 64,
        LedgerRole.PRIMARY,
        str((tmp_path / "result.json").resolve()),
        timeout_s=300.0,
    )
    context = V5LiveContextV1(
        writer=writer,
        transport=transport,  # type: ignore[arg-type]
        trace=trace,
        backend_factory=lambda _spec: _Backend(),
        anchor_pose=(0.0,) * 6,
        close_callback=lambda: None,
    )

    with pytest.raises(V5LiveOwnerError, match="bounded timeout"):
        V5SingleWriterOwnerV1(context).execute_chain(request)

    successor = candidate_identity_from_plan(request.plans[1])
    assert not any(
        host_ordinal == successor.ordinal
        and host_token == successor.candidate_token
        and command is RolloverCommand.NONE
        for _clock, host_ordinal, host_token, command, _qdot
        in writer.sent_packet_authority
    )
    assert not any(
        thread.name.startswith("v5-activation-durability")
        for thread in threading.enumerate()
    )
