"""Focused R012 native queue, resume, and one-shot host tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r012.censor import PathEarlyEndHandshake  # noqa: E402
from step5d_autotune_v4_r012.live_adapter import R012HostLoop, R012TrialExecutor, map_r012_kind_to_queue  # noqa: E402
from step5d_autotune_v4_r012.live_host import (  # noqa: E402
    AutotuneRunMode,
    R012_FORMAL_LIVE_ALLOW_TOKEN,
    R012HostError,
    R012QuarantineViolation,
    build_host_stack,
    require_r012_formal_live,
    require_r012_run_mode,
)
from step5d_autotune_v4_r012.live_owner import (  # noqa: E402
    _r012_force_objective_flow_gates_pass,
    _r012_fresh_timing_acceptance,
)
from step5d_autotune_v4_r012.path_cbf_live import R012GuardStop, R012PathGuardStack  # noqa: E402
from step5d_autotune_v4_r012.register_transport import R012LiveRTDETransport  # noqa: E402
from step5d_autotune_v4_r004.path_reference import PATH_STAGE_ID, R004_PATH_FRAME_SNAPSHOT, step5_path_reference  # noqa: E402
from step5d_autotune_v4_r004.transport import R004OutputSnapshot  # noqa: E402
from step5d_autotune_v4_r012.ledger import Ledger  # noqa: E402
from step5d_autotune_v4_r012.serial_queue import SerialOneSlotQueue  # noqa: E402
from step5d_autotune_v4_r012.scheduler import REFERENCE_CANDIDATE  # noqa: E402
import run_step5d_autotune_v4_r012_live as live_entry  # noqa: E402


CAMPAIGN = "r012-campaign-test"
RUN = "r012-run-test"
ATTEMPT = "attempt-000001"


def test_live_entry_explicitly_enables_production_gp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class StopAfterStack(Exception):
        pass

    monkeypatch.setattr(
        live_entry,
        "_persisted_snapshot",
        lambda path: SimpleNamespace(
            campaign_id=CAMPAIGN,
            run_id=RUN,
            attempt_id=ATTEMPT,
            campaign_mode="one_shot",
        ),
    )
    monkeypatch.setattr(live_entry, "verify_local_controller_triplet", lambda snapshot: True)
    monkeypatch.setattr(live_entry, "verify_cbf_timing_receipt", lambda path: True)
    monkeypatch.setattr(live_entry, "build_readiness_report", lambda snapshot, *, evidence: {"evidence": evidence})
    monkeypatch.setattr(live_entry, "require_bench_launch_ready", lambda report: None)

    def capture_host_stack(**kwargs: object) -> None:
        captured.update(kwargs)
        raise StopAfterStack

    monkeypatch.setattr(live_entry, "build_host_stack", capture_host_stack)
    args = SimpleNamespace(
        mode="bounded_live",
        snapshot=tmp_path / "r012_live_snapshot.json",
        campaign_mode=None,
        controller_readback=None,
    )

    with pytest.raises(StopAfterStack):
        live_entry.run_live(args)
    assert captured["use_production_gp"] is True


def test_production_gp_ask_uses_optimizer_subprocess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ledger_path = tmp_path / "r012.jsonl"
    Ledger.create(ledger_path, campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT)
    stack = build_host_stack(
        campaign_id=CAMPAIGN,
        run_id=RUN,
        attempt_id=ATTEMPT,
        ledger_path=ledger_path,
        use_production_gp=True,
    )
    optimizer = stack["optimizer"]
    monkeypatch.setattr(stack["controller"], "has_pre_bo_scheduler_work", lambda: False)
    monkeypatch.setattr(stack["controller"].scheduler, "enqueue_bo", lambda candidate: None)
    rows = tuple(
        SimpleNamespace(
            candidate=REFERENCE_CANDIDATE,
            objective_n=0.5,
            dispatch_id=f"row-{index}",
            kind="REFERENCE",
        )
        for index in range(3)
    )
    selected = {
        "force_p_gain": 0.004756828460101381,
        "force_damping": 47.09019925420801,
        "force_i_gain": 0.0,
        "i_off": True,
        "normal_filter_tau_s": 0.05202781128136904,
        "orientation_ko": 0.08408964152537146,
        "motion_kp": 1.7838106725040817,
        "target_force_n": 5.0,
    }
    monkeypatch.setattr(
        stack["controller"],
        "next_dispatch",
        lambda: SimpleNamespace(candidate=selected, kind="BO_TRIAL"),
    )
    captured: dict[str, object] = {}

    def request(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "proposal": {
                "candidate": selected,
                "normalized_X": [0.6, 0.55, 1 / 3, 0.1875, 0.125],
                "acquisition_value": -2.9,
                "q": 1,
            },
            "fit_receipt": {
                "fit_completed": True,
                "lengthscales_unit_fitted": [0.2, 0.6, 0.7, 1.7, 1.5],
                "train_yvar_geometric_mean_n2": 0.011,
            },
        }

    monkeypatch.setattr(optimizer.optimizer_client, "request", request)
    result = optimizer.ask(observations=rows)
    assert result.metadata["r012_source"] == "qlognei"
    assert result.metadata["production_gp_fit"]["fit_completed"] is True
    assert len(captured["observations"]) == 3


def test_protocol_is_fixed_and_formal_live_boundary_is_separate() -> None:
    handshake = PathEarlyEndHandshake()
    assert handshake.runtime_protocol == 612012
    assert handshake.request_register == 35
    assert handshake.ack_register == 36
    assert map_r012_kind_to_queue("REFERENCE") == ("REFERENCE", None)


def test_r012_timing_accepts_fresh_full_bins_not_low_capture_rate() -> None:
    base = {
        "force_objective": {"complete_bins": 550, "required_bins": 550},
        "path_observed_span_s": 59.998,
        "path_cadence_hz": 500.0,
        "timing_evidence": {
            "feedback_age_p99_s": 0.009,
            "max_fresh_gap_s": 0.01,
            "layer_rates_hz": {
                "writer_publishes": 355.0,
                "rtde_frames": 355.0,
                "tp_consumed_packet_echoes": 355.0,
                "kunwei_frames": 999.0,
            },
        },
    }
    result = SimpleNamespace(duration_s=60.0, metrics=base)
    assert _r012_fresh_timing_acceptance(result) is True
    base["timing_evidence"]["max_fresh_gap_s"] = 0.021
    assert _r012_fresh_timing_acceptance(result) is False


def test_force_objective_flow_keeps_motion_and_timing_diagnostic() -> None:
    result = SimpleNamespace(
        safe_return=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        identity_gate=True,
        motion_gate=False,
        timing_gate=False,
    )
    assert _r012_force_objective_flow_gates_pass(result) is True
    result.return_gate = False
    assert _r012_force_objective_flow_gates_pass(result) is False


def test_post_completion_arm_replay_does_not_replace_success_reason() -> None:
    snapshot = R004OutputSnapshot(
        observed_at_s=1.0,
        timestamp=1.0,
        payload_kg=1.0,
        payload_cog_m=(0.0, 0.0, 0.0),
        tcp_offset_m_rad=(0.0,) * 6,
        tcp_speed_m_s_rad_s=(0.0,) * 6,
        tcp_pose_m_rad=(0.0,) * 6,
        q_rad=(0.0,) * 6,
        qd_rad_s=(0.0,) * 6,
        safety_mode=1,
        robot_mode=7,
        runtime_state=2,
        consumed_packet_sequence=1,
        integer_echoes={26: 78, 28: 62, 31: 123},
    )
    normalized = R012LiveRTDETransport.normalize_post_completion_replay(snapshot)
    assert normalized.integer_echoes[28] == 0
    incomplete = R004OutputSnapshot(
        **{**snapshot.__dict__, "integer_echoes": {26: 78, 28: 62, 31: 27}}
    )
    assert R012LiveRTDETransport.normalize_post_completion_replay(incomplete) is incomplete


def test_scheduler_has_one_pending_slot_and_refills_after_result() -> None:
    controller = build_host_stack(campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT)["controller"]
    assert len(controller.scheduler.pending) == 1
    first = controller.next_dispatch()
    with pytest.raises(Exception):
        controller.next_dispatch()
    controller.record_exact(candidate=first.candidate, sealed_mae_n=0.5, dispatch_id="dispatch-000001", kind=first.kind)
    assert len(controller.scheduler.pending) == 1
    assert controller.scheduler.pending[0].candidate == REFERENCE_CANDIDATE


def test_serial_queue_resume_does_not_skip_after_inflight_result(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    queue = SerialOneSlotQueue(path)
    queue.enqueue(REFERENCE_CANDIDATE, kind="REFERENCE")
    queue.dispatch()
    resumed = SerialOneSlotQueue(path)
    resumed.result({"state": "HOME", "safe_return": True})
    next_item = resumed.enqueue(REFERENCE_CANDIDATE, kind="REFERENCE")
    assert next_item.dispatch_id == "dispatch-000002"


def test_resume_restores_direct_inflight_candidate_without_chain(tmp_path: Path) -> None:
    ledger_path = tmp_path / "r012.jsonl"
    Ledger.create(ledger_path, campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT)
    first = build_host_stack(campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT, ledger_path=ledger_path)["controller"]
    item = first.next_dispatch()
    first.record_exact(candidate=item.candidate, sealed_mae_n=0.5, dispatch_id="dispatch-000001", kind=item.kind)
    inflight = first.next_dispatch()
    resumed = build_host_stack(campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT, ledger_path=ledger_path)["controller"]
    assert resumed.in_flight is not None
    assert resumed.in_flight.as_dict() == inflight.as_dict()
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert all("previous_record" not in row for row in rows)


def test_one_attempt_returns_home_and_stops(tmp_path: Path) -> None:
    stack = build_host_stack(campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT)
    queue = SerialOneSlotQueue(tmp_path / "queue.jsonl", route_id="route-1", session_id="session-1")
    owner = SimpleNamespace(injection=SimpleNamespace(prepare_control=lambda: SimpleNamespace()))
    executor = R012TrialExecutor(
        owner=owner,
        runner=lambda item: {"state": "HOME", "safe_return": True, "dispatch_id": item.dispatch_id},
    )
    prepared_control = owner.injection.prepare_control()
    assert prepared_control._tube_cbf is executor.guard_stack
    assert prepared_control._r012_guard_bound is True
    loop = R012HostLoop(stack["controller"], stack["path_early_end"], queue, executor=executor)
    result = loop.run_one()
    assert result == {"state": "HOME", "safe_return": True, "dispatch_id": "dispatch-000001"}
    assert loop.terminal is True
    assert loop.stop_reason == "r012_one_attempt_home_and_stop"
    assert queue.in_flight is None


def test_continuous_ingests_home_result_clears_q1_and_dispatches_next(tmp_path: Path) -> None:
    ledger_path = tmp_path / "r012.jsonl"
    Ledger.create(ledger_path, campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT)
    stack = build_host_stack(
        campaign_id=CAMPAIGN,
        run_id=RUN,
        attempt_id=ATTEMPT,
        ledger_path=ledger_path,
    )
    queue = SerialOneSlotQueue(tmp_path / "queue.jsonl", route_id="route-1", session_id="session-1")
    owner = SimpleNamespace(injection=SimpleNamespace(prepare_control=lambda: SimpleNamespace()))
    executor = R012TrialExecutor(
        owner=owner,
        runner=lambda item: {
            "schema": "step5d.autotune-v4/r012-trial-result-v1",
            "dispatch_id": item.dispatch_id,
            "campaign_id": CAMPAIGN,
            "run_id": RUN,
            "attempt_id": ATTEMPT,
            "state": "HOME",
            "safe_return": True,
            "home": True,
            "return_guard": True,
            "sealed_mae_n": 0.5,
        },
    )
    loop = R012HostLoop(
        stack["controller"], stack["path_early_end"], queue,
        executor=executor,
        optimizer=stack["optimizer"],
    )

    first = loop.run_continuous_step()
    assert first is not None and first["dispatch_id"] == "dispatch-000001"
    assert first["sealed_mae_n"] == 0.5
    assert queue.in_flight is not None
    assert queue.in_flight.dispatch_id == "dispatch-000002"
    assert stack["controller"].in_flight is not None
    assert stack["controller"].in_flight.ordinal == 2
    assert [row["record_type"] for row in stack["ledger"].records] == [
        "header", "dispatch", "observation", "dispatch",
    ]

    second = loop.run_continuous_step()
    assert second is not None and second["dispatch_id"] == "dispatch-000002"
    assert queue.in_flight is not None and queue.in_flight.dispatch_id == "dispatch-000003"
    assert stack["controller"].scheduler.dispatch_count == 3

    third = loop.run_continuous_step()
    assert third is not None and third["dispatch_id"] == "dispatch-000003"
    assert queue.in_flight is not None and queue.in_flight.dispatch_id == "dispatch-000004"
    assert stack["controller"].in_flight is not None
    assert stack["controller"].in_flight.kind == "BO_TRIAL"
    assert len(stack["controller"].observations) == 3
    resumed = build_host_stack(
        campaign_id=CAMPAIGN,
        run_id=RUN,
        attempt_id=ATTEMPT,
        ledger_path=ledger_path,
    )["controller"]
    assert resumed.observations == stack["controller"].observations
    assert resumed.in_flight is not None
    assert resumed.in_flight.as_dict() == stack["controller"].in_flight.as_dict()
    assert loop.terminal is False


def test_challenger_confirmation_wins_over_same_step_incumbent_retest(tmp_path: Path) -> None:
    stack = build_host_stack(campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT)
    queue = SerialOneSlotQueue(tmp_path / "queue.jsonl")
    owner = SimpleNamespace(injection=SimpleNamespace(prepare_control=lambda: SimpleNamespace()))

    def runner(item):
        return {
            "schema": "step5d.autotune-v4/r012-trial-result-v1",
            "dispatch_id": item.dispatch_id,
            "campaign_id": CAMPAIGN,
            "run_id": RUN,
            "attempt_id": ATTEMPT,
            "state": "HOME",
            "safe_return": True,
            "home": True,
            "return_guard": True,
            "sealed_mae_n": 0.4 if item.dispatch_id == "dispatch-000011" else 0.5,
        }

    loop = R012HostLoop(
        stack["controller"], stack["path_early_end"], queue,
        executor=R012TrialExecutor(owner=owner, runner=runner),
        optimizer=stack["optimizer"],
    )
    for _ in range(11):
        loop.run_continuous_step()
    assert queue.in_flight is not None
    assert queue.in_flight.dispatch_id == "dispatch-000012"
    assert queue.in_flight.kind == "CHALLENGER_CONFIRM"
    assert stack["controller"].scheduler.novel_bo_dispatches == 8
    loop.run_continuous_step()
    assert queue.in_flight is not None
    assert queue.in_flight.dispatch_id == "dispatch-000013"
    assert queue.in_flight.kind == "CHALLENGER_CONFIRM"


def test_cold_replay_resumes_incomplete_challenger_under_current_policy(tmp_path: Path) -> None:
    ledger_path = tmp_path / "r012.jsonl"
    Ledger.create(ledger_path, campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT)
    stack = build_host_stack(
        campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT,
        ledger_path=ledger_path,
    )
    controller, optimizer = stack["controller"], stack["optimizer"]
    for ordinal in range(1, 4):
        item = controller.next_dispatch()
        controller.record_exact(candidate=item.candidate, sealed_mae_n=0.5, dispatch_id=f"dispatch-{ordinal:06d}", kind=item.kind)
    for ordinal in range(4, 12):
        optimizer.ask()
        item = controller.in_flight
        assert item is not None
        controller.record_exact(candidate=item.candidate, sealed_mae_n=0.4 if ordinal == 11 else 0.5, dispatch_id=f"dispatch-{ordinal:06d}", kind=item.kind)
    confirmation = controller.next_dispatch()
    controller.record_exact(candidate=confirmation.candidate, sealed_mae_n=0.42, dispatch_id="dispatch-000012", kind=confirmation.kind)

    resumed = build_host_stack(
        campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT,
        ledger_path=ledger_path,
    )["controller"]
    assert resumed.in_flight is None
    assert len(resumed.scheduler.pending) == 1
    assert resumed.scheduler.pending[0].kind == "CHALLENGER_CONFIRM"
    assert resumed.scheduler.pending[0].confirmation_index == 3


def test_composed_path_guard_stack_is_nominal_soft_hard_and_fail_closed() -> None:
    stack = R012PathGuardStack()
    reference = step5_path_reference(PATH_STAGE_ID, (0.0, 0.0), 0.0)["desired_xy"]
    along = R004_PATH_FRAME_SNAPSHOT["basis"]["u_along_xy"]
    lateral = R004_PATH_FRAME_SNAPSHOT["basis"]["p_lateral_xy"]
    nominal = stack.apply((0.0, 0.0, 0.0, 0.0, 0.0, 0.0), mode="path", actual_tcp_pose=reference, path_time_s=0.0)
    assert nominal.terminal_stop is False
    interior_command = (0.0012, -0.0007, 0.0003, 0.01, -0.02, 0.03)
    interior = stack.apply(interior_command, mode="path", actual_tcp_pose=reference, path_time_s=0.0)
    assert interior.desired_twist == interior_command
    assert interior.soft.applied is False
    assert interior.soft.result is not None
    assert interior.soft.result.intervention_norm_m_s == 0.0
    soft = stack.apply((0.1, 0.1, 0.0, 0.0, 0.0, 0.0), mode="path", actual_tcp_pose=reference, path_time_s=0.0)
    assert soft.terminal_stop is False
    assert soft.desired_twist == (0.1, 0.1, 0.0, 0.0, 0.0, 0.0)
    assert soft.soft.applied is False
    for axis, distance in ((along, 0.024), (lateral, 0.014)):
        inside = (
            reference[0] + distance * axis[0],
            reference[1] + distance * axis[1],
        )
        assert stack.apply((0.0,) * 6, mode="path", actual_tcp_pose=inside, path_time_s=0.0).terminal_stop is False
    lateral_breach = (
        reference[0] + 0.016 * lateral[0],
        reference[1] + 0.016 * lateral[1],
    )
    with pytest.raises(R012GuardStop) as hard_exc:
        stack.apply((0.1, 0.0, 0.0, 0.0, 0.0, 0.0), mode="path", actual_tcp_pose=lateral_breach, path_time_s=0.0)
    assert hard_exc.value.outcome.terminal_stop is True
    assert hard_exc.value.outcome.reason == "hard_outer_ellipse_breach"
    with pytest.raises(R012GuardStop) as stale_exc:
        stack.apply((0.0, 0.0, 0.0, 0.0, 0.0, 0.0), mode="path", actual_tcp_pose=reference, path_time_s=0.0, state_age_s=0.021)
    assert stale_exc.value.outcome.terminal_stop is True
    assert stale_exc.value.outcome.reason is not None and stale_exc.value.outcome.reason.startswith("hard_guard_fail_closed:")


def test_bounded_live_is_default_and_formal_release_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("R012_ALLOW_FORMAL_LIVE", raising=False)
    monkeypatch.delenv("R008_ALLOW_FORMAL_LIVE", raising=False)
    assert require_r012_run_mode(None, "test") is AutotuneRunMode.BOUNDED_LIVE
    assert require_r012_run_mode("bounded_live", "test") is AutotuneRunMode.BOUNDED_LIVE
    with pytest.raises(R012HostError):
        require_r012_run_mode("formal_release", "test")
    monkeypatch.setenv("R012_ALLOW_FORMAL_LIVE", R012_FORMAL_LIVE_ALLOW_TOKEN)
    assert require_r012_run_mode("formal_release", "test") is AutotuneRunMode.FORMAL_RELEASE
    monkeypatch.setenv("R008_ALLOW_FORMAL_LIVE", "old")
    with pytest.raises(R012QuarantineViolation):
        require_r012_run_mode("formal_release", "test")
