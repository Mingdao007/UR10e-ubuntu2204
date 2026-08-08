"""Deterministic offline acceptance matrix for Autotune V4 r006."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE  # noqa: E402
from step5d_autotune_v4_r004.path_reference import (  # noqa: E402
    PATH_AMPLITUDE_M,
    PATH_DURATION_S,
    PATH_OMEGA_RAD_S,
)
from step5d_autotune_v4_r006.contracts import (  # noqa: E402
    OBJECTIVE_SEMANTIC_FINGERPRINT,
    load_contract,
)
from step5d_autotune_v4_r006.fake_rtde import (  # noqa: E402
    FakeFrame,
    FakeRTDE,
    FakeRTDEError,
    FreshFramePacketCache,
)
from step5d_autotune_v4_r006.lattice import (  # noqa: E402
    ANCHOR_POINT,
    IMode,
    ParameterPoint,
    PointObservation,
    RegionTraversal,
    TrustRegion,
    neighbors,
    route_bfs,
    select_second_center,
    second_warm_start_plan,
    transition_is_legal,
    warm_start_plan,
)
from step5d_autotune_v4_r006.managed import resolve_managed_runtime  # noqa: E402
from step5d_autotune_v4_r006.motion_profile import ACTIVE_MOTION_ENVELOPE_V2  # noqa: E402
from step5d_autotune_v4_r006.objective import (  # noqa: E402
    R006ObjectiveBuilder,
    R006ObjectiveError,
    R006ObjectiveReceipt,
    build_receipt_from_samples,
    cold_read_verify,
    cold_read_verify_subprocess,
)
from step5d_autotune_v4_r006.optimizer import (  # noqa: E402
    ConditionalMatern52GP,
    CertifiedRegionState,
    GPObservation,
    GPPosterior,
    LocalPAC,
    ManagedOptimizer,
    OptimizerError,
    QLogNEI,
    QLogNEIScheduler,
    RBF_SHADOW,
    STUDENT_T_VARIATIONAL_SHADOW,
    WarmStartOptimizer,
    feature_map,
)
from step5d_autotune_v4_r006.queue import OfflineR006DurableQueue  # noqa: E402
from step5d_autotune_v4_r006.runtime import (  # noqa: E402
    AttemptIdentity,
    AttemptResult,
    OfflineR006AttemptStore,
    Disposition,
    LifecycleState,
    R006RuntimeError,
    ResidentLifecycle,
    RuntimeThresholds,
    run_fake_campaign,
    apply_disposition,
)


def _sample(index: int, force: float = 5.0, path_time_s: float | None = None) -> object:
    from step5d_force_objective import ForcePathSample

    time_s = index / 10.0 if path_time_s is None else path_time_s
    return ForcePathSample(
        path_time_s=time_s,
        path_phase=25,
        filtered_normal_n=force,
        source_sequences={"controller": index + 1, "rtde": index + 1},
        source_ages_s={"controller": 0.001, "rtde": 0.001},
        timestamp_s=1000.0 + time_s,
    )


def _complete_samples(force: float = 5.0) -> tuple[object, ...]:
    return tuple(
        _sample(index, force=force, path_time_s=0.05 + index * 0.1)
        for index in range(550)
    ) + tuple(
        _sample(550 + index, force=force, path_time_s=55.05 + index * 0.1)
        for index in range(50)
    )


def test_content_addressed_contract_and_parent_isolation() -> None:
    contract = load_contract()
    assert contract.program == "step5d_strict_rnn_autotune_v4_r006"
    assert contract.raw["parent_identity"]["lineage"] == "step5d_strict_rnn_autotune_v4_r005"
    for relative, expected in contract.parent_hashes.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
    assert contract.raw["offline_boundary"]["controller_upload"] is False
    assert contract.raw["runtime"]["production_stop_after_ordinal"] is False
    assert contract.raw["objective"]["target_is_optimizer_dimension"] is False
    closure = json.loads(contract.source_closure_path.read_text(encoding="utf-8"))
    assert closure["content_address"]["sha256"] == contract.raw["source_closure"]["sha256"]
    assert closure["parent_identity"]["r004_r005_identity_preserved"] is True
    assert closure["parent_identity"]["qualifications_imported"] is False


def test_active_caps_double_only_in_typed_adapter_and_path_guards_stay_frozen() -> None:
    assert ACTIVE_MOTION_ENVELOPE_V2.execution_profile is R004_MOTION_PROFILE.execution_profile
    assert (
        ACTIVE_MOTION_ENVELOPE_V2.xy_path_speed_m_s,
        ACTIVE_MOTION_ENVELOPE_V2.total_linear_cap_m_s,
        ACTIVE_MOTION_ENVELOPE_V2.normal_linear_cap_m_s,
        ACTIVE_MOTION_ENVELOPE_V2.angular_cap_rad_s,
        ACTIVE_MOTION_ENVELOPE_V2.qdot_cap_rad_s,
        ACTIVE_MOTION_ENVELOPE_V2.host_qdot_slew_rad_s2,
        ACTIVE_MOTION_ENVELOPE_V2.tp_speedj_acceleration_rad_s2,
        ACTIVE_MOTION_ENVELOPE_V2.normal_update_rate_rad_s,
    ) == pytest.approx((0.008, 2.0, 2.0, 0.5, 5.0, 5.0, 40.0, 200.0))
    assert (PATH_OMEGA_RAD_S, PATH_DURATION_S, PATH_AMPLITUDE_M) == pytest.approx((0.1, 60.0, 0.015))
    unchanged = ACTIVE_MOTION_ENVELOPE_V2.as_dict["unchanged"]
    assert unchanged["contact_search_speed_m_s"] == pytest.approx(0.0002)
    assert unchanged["contact_search_acceleration_m_s2"] == pytest.approx(0.005)
    assert unchanged["active_lease_s"] == pytest.approx(0.08)
    assert unchanged["force_hard_guard_abs_normal_n"] == 60.0
    assert unchanged["force_hard_guard_norm_n"] == 100.0
    assert unchanged["torque_hard_guard_norm_nm"] == 3.0
    assert unchanged["home_xy_transfer_speed_m_s"] == pytest.approx(0.09)
    assert unchanged["home_xy_transfer_acceleration_m_s2"] == pytest.approx(0.135)


def test_lifecycle_scopes_80ms_only_to_active_and_home_is_unbounded() -> None:
    lifecycle = ResidentLifecycle()
    lifecycle.wait_at_home()
    lifecycle.transition(LifecycleState.DISPATCH)
    with pytest.raises(R006RuntimeError, match="required before ARM"):
        lifecycle.arm(execution_id="execution-missing-thresholds", now_s=1.0)
    lifecycle.state = LifecycleState.HOME_IDLE
    lifecycle.provide_thresholds(RuntimeThresholds("step5d.autotune-v4/r006-runtime-thresholds-v1", "r006-v1", 0.05, 0.2))
    lifecycle.transition(LifecycleState.DISPATCH)
    lease = lifecycle.arm(execution_id="execution-1", now_s=1.0)
    lease.validate(now_s=1.05, state=LifecycleState.ACTIVE)
    with pytest.raises(R006RuntimeError, match="expired"):
        lifecycle.active_tick(now_s=1.081)
    lifecycle.state = LifecycleState.ACTIVE
    lifecycle.lease = lease
    lifecycle.safe_return()
    with pytest.raises(R006RuntimeError, match="scoped only"):
        lease.validate(now_s=1.05, state=LifecycleState.RETURN)
    lifecycle.transition(LifecycleState.TELL)
    lifecycle.transition(LifecycleState.REFILL)
    lifecycle.wait_at_home()
    assert lifecycle.state is LifecycleState.HOME_IDLE


def test_ordinal_four_continues_and_queue_is_two_plus_one(tmp_path: Path) -> None:
    point = ANCHOR_POINT
    queue = OfflineR006DurableQueue(tmp_path / "queue")
    queue.enqueue(point, kind="BO_TRIAL")
    queue.enqueue(point.with_step("P", 1), kind="BO_TRIAL")
    with pytest.raises(Exception, match="two pending"):
        queue.enqueue(point.with_step("D", 1), kind="BO_TRIAL")
    dispatch = queue.prepare_next()
    assert dispatch is not None
    assert queue.inflight == dispatch
    assert len(queue.pending()) == 1
    assert queue.prepare_next() == dispatch
    queue.complete(dispatch, status=Disposition.OBJECTIVE.value)
    identity = AttemptIdentity(4, "execution-4", "logical-4", "BO_TRIAL", point)
    assert identity.attempt_sequence == 4
    assert identity.attempt_sequence > 3


def test_packet_sequence_only_advances_on_distinct_frame_and_stall_stops() -> None:
    cache = FreshFramePacketCache()
    first = cache.observe(FakeFrame(1, 1, 0.0, (1.0,)), now_s=0.0)
    cached = cache.observe(FakeFrame(1, 1, 0.001, (1.0,)), now_s=0.001)
    distinct = cache.observe(FakeFrame(1, 2, 0.002, (2.0,)), now_s=0.002)
    assert first.fresh and not cached.fresh and distinct.fresh
    assert cached.packet_sequence == first.packet_sequence
    assert distinct.packet_sequence == first.packet_sequence + 1
    with pytest.raises(FakeRTDEError, match="changed payload"):
        cache.observe(FakeFrame(1, 2, 0.003, (3.0,)), now_s=0.003)
    with pytest.raises(FakeRTDEError, match="80 ms"):
        cache.observe(None, now_s=0.083)


def test_unbounded_lattice_portal_trust_region_warm_start_and_bfs() -> None:
    far = ParameterPoint(p_step=10000, d_step=-10000, tau_step=17, ko_step=42, kp_step=-31)
    assert "target" not in far.canonical
    assert transition_is_legal(ANCHOR_POINT, ParameterPoint(p_step=1))
    assert transition_is_legal(ANCHOR_POINT, ParameterPoint(i_mode=IMode.ON, i_step=-4))
    assert not transition_is_legal(ANCHOR_POINT, ParameterPoint(i_mode=IMode.ON, i_step=-3))
    offset_off = ParameterPoint(p_step=2, d_step=-1, tau_step=1, ko_step=2, kp_step=-2)
    offset_entry = ParameterPoint(p_step=2, d_step=-1, tau_step=1, i_mode=IMode.ON, i_step=-4, ko_step=2, kp_step=-2)
    assert offset_entry in neighbors(offset_off)
    assert transition_is_legal(offset_off, offset_entry)
    region = TrustRegion(ANCHOR_POINT)
    assert len(region.center_modes) == 6
    assert all(region.contains(point) for point in region.center_modes)
    plan = warm_start_plan()
    assert len(plan) == 25
    assert plan[-4:] == (
        ParameterPoint(i_mode=IMode.ON, i_step=-4),
        ParameterPoint(i_mode=IMode.ON, i_step=-3),
        ParameterPoint(i_mode=IMode.ON, i_step=-4),
        ANCHOR_POINT,
    )
    for left, right in zip(plan, plan[1:]):
        assert left == right or transition_is_legal(left, right)
    on_center = ParameterPoint(i_mode=IMode.ON, i_step=-2)
    assert len(second_warm_start_plan(on_center)) == 25
    route = route_bfs(ANCHOR_POINT, ParameterPoint(p_step=1, d_step=1))
    assert route.points[0] == ANCHOR_POINT and route.points[-1] == ParameterPoint(p_step=1, d_step=1)
    assert all(transition_is_legal(left, right) for left, right in zip(route.points, route.points[1:]))
    observations = (
        PointObservation(ANCHOR_POINT, 0.2, 0.02),
        PointObservation(ParameterPoint(p_step=1), 0.1, 0.01),
        PointObservation(ParameterPoint(d_step=1), 0.1, 0.03),
    )
    assert select_second_center(observations) == ParameterPoint(p_step=1)


def test_region_traversal_alternates_boundary_and_bfs_without_starvation() -> None:
    region = TrustRegion(ANCHOR_POINT)
    traversal = RegionTraversal(region, score=lambda point: float(point.p_step))
    selected = [traversal.next() for _ in range(12)]
    assert all(point is not None for point in selected)
    assert len(set(selected)) == 12
    assert selected[0] != ANCHOR_POINT  # optimistic boundary has priority on first turn
    assert any(point == ANCHOR_POINT for point in selected[1:])  # BFS frontier is not starved


def test_raw_objective_cold_read_forged_scalar_and_tamper_rejection() -> None:
    campaign = "a" * 64
    builder = R006ObjectiveBuilder(attempt_sequence=4, execution_id="execution-4", campaign_fingerprint=campaign)
    for sample in _complete_samples(force=5.25):
        builder.add(sample)
    sealed = builder.finalize()
    assert not sealed.trainable
    assert sealed.r004_legacy_shadow_mae_n == pytest.approx(0.25)
    assert sealed.r005_abs_bin_mean_shadow_mae_n == pytest.approx(0.25)
    verified = cold_read_verify(sealed, expected_campaign_fingerprint=campaign)
    assert verified.trainable
    assert verified.objective == pytest.approx(0.25)
    assert cold_read_verify_subprocess(sealed, expected_campaign_fingerprint=campaign).objective == pytest.approx(0.25)
    restored = cold_read_verify(R006ObjectiveReceipt.from_mapping(verified.as_dict()), expected_campaign_fingerprint=campaign)
    assert restored.objective == pytest.approx(0.25)
    forged = {"objective_mae_n": 0.0, "target_force_n": 5.0}
    with pytest.raises(R006ObjectiveError, match="not a receipt"):
        R006ObjectiveReceipt.from_mapping(forged)
    tampered = verified.as_dict()
    tampered["formal_bin_sum_n"][0] += 1.0
    with pytest.raises(R006ObjectiveError, match="seal"):
        R006ObjectiveReceipt.from_mapping(tampered)


def test_oscillatory_samples_separate_formal_within_bin_mae_from_r005_shadow() -> None:
    campaign = "c" * 64
    builder = R006ObjectiveBuilder(
        attempt_sequence=6,
        execution_id="execution-oscillatory",
        campaign_fingerprint=campaign,
    )
    sequence = 1
    for bin_index in range(50):
        builder.add(
            _sample(
                sequence,
                force=5.0,
                path_time_s=0.01 + bin_index * 0.1,
            )
        )
        sequence += 1
    for bin_index in range(550):
        for offset, force in ((0.01, 4.0), (0.06, 6.0)):
            builder.add(
                _sample(
                    sequence,
                    force=force,
                    path_time_s=5.0 + bin_index * 0.1 + offset,
                )
            )
            sequence += 1
    receipt = cold_read_verify(builder.finalize(), expected_campaign_fingerprint=campaign)
    assert receipt.objective == pytest.approx(1.0)
    assert receipt.r005_abs_bin_mean_shadow_mae_n == pytest.approx(0.0)
    assert receipt.r004_legacy_shadow_mae_n == pytest.approx(0.0)


def test_missing_bin_is_safe_nontrainable_and_objective_is_not_caller_controlled() -> None:
    campaign = "b" * 64
    builder = R006ObjectiveBuilder(attempt_sequence=5, execution_id="execution-5", campaign_fingerprint=campaign)
    samples = list(_complete_samples())
    samples.pop(100)
    for sample in samples:
        builder.add(sample)
    receipt = builder.finalize()
    assert receipt.objective_mae_n is None
    assert not cold_read_verify(receipt).trainable
    result = AttemptResult(
        identity=AttemptIdentity(5, "execution-5", "logical-5", "BO_TRIAL", ANCHOR_POINT),
        disposition=Disposition.SAFE_NONTRAINABLE,
        safe_return=True,
        safety_gate=True,
        motion_gate=True,
        timing_gate=True,
        contact_gate=True,
        return_gate=True,
        identity_gate=True,
        receipt=None,
    )
    assert not result.eligible


def test_production_gp_noise_freeze_features_pac_and_shadow_invariance() -> None:
    gp = ConditionalMatern52GP()
    p = ANCHOR_POINT
    plus = ParameterPoint(p_step=1)
    gp.add(GPObservation(p, 0.4, 0.01))
    gp.add(GPObservation(p, 0.42, 0.01))
    gp.add(GPObservation(plus, 0.2, 0.02))
    first = gp.fit(1)
    assert gp.kernel_name == "conditional_matern52"
    assert gp.train_yvar_n2 == pytest.approx(1e-4)
    assert len(feature_map(p)) == 7
    assert gp.feature_names == ("-log2(P)", "log2(D/P)", "conditional_log2(I/P)", "log2(tau)", "log2(Ko)", "log2(Kp)", "I_mode")
    gp.add(GPObservation(ParameterPoint(d_step=1), 0.3, 0.01))
    gp.fit(2)
    gp.freeze()
    with pytest.raises(OptimizerError, match="frozen"):
        gp.fit(1)
    assert first.signal_scale_n >= 1e-4
    qlognei = QLogNEI(gp)
    selected = qlognei.ask((plus, ParameterPoint(d_step=1), ParameterPoint(tau_step=1), ParameterPoint(ko_step=1)), pending=(plus,), q=1)
    assert selected and qlognei.last_pending == (plus,)
    assert STUDENT_T_VARIATIONAL_SHADOW.command_signature(p, (plus,)) == RBF_SHADOW.command_signature(p, (plus,))
    pac = LocalPAC(epsilon_n=0.05).certify({p: GPPosterior(0.10, 0.0001), plus: GPPosterior(0.11, 0.0001)}, p)
    assert pac.passed and not pac.global_convergence_claim
    assert QLogNEIScheduler(epsilon_n=0.05).q_for(pac) == 1
    assert QLogNEIScheduler(epsilon_n=0.05).q_for(None) == 4
    region = CertifiedRegionState.from_trust_region(TrustRegion(p))
    expanded = region.recenter_expand(plus)
    assert expanded.center == plus and expanded.radius_octave > region.radius_octave and not expanded.global_convergence_claim


def test_managed_runtime_resolver_requires_cuda_and_never_degrades() -> None:
    values = {
        "environment_id": "e" * 64,
        "environment_hash": "f" * 64,
        "torch_version": "2.x",
        "botorch_version": "0.x",
        "gpytorch_version": "1.x",
        "cuda_available": True,
        "gpu_uuid": "gpu-test",
    }
    runtime = resolve_managed_runtime(optimizer_resolver=lambda: values)
    assert runtime.manifest.lineage_id == "step5d_strict_rnn_autotune_v4_r006"
    assert runtime.optimizer.cuda_available
    assert runtime.remote_startup.live_action_performed is False
    with pytest.raises(OptimizerError, match="degraded fallback"):
        ManagedOptimizer(lambda: {**values, "cuda_available": False}).resolve()


def test_fake_rtde_495_vs_500_and_source_stall_regression() -> None:
    fake = FakeRTDE()
    evidence = fake.timing_regression()
    assert evidence.writer_count == 30000
    assert evidence.rtde_count == 29700
    assert evidence.writer_rate_hz == pytest.approx(500.0)
    assert evidence.rtde_rate_hz == pytest.approx(495.0)
    assert evidence.echo_backlog == 0
    assert evidence.passes
    published, backlog = fake.exercise_no_echo_backlog()
    assert published == 29700 and backlog == 0
    assert fake.stall_stops_at(stall_duration_s=0.081) == pytest.approx(10.081)


def test_terminal_dispositions_hard_stop_and_crash_resume_identity(tmp_path: Path) -> None:
    assert {item.value for item in Disposition} == {"OBJECTIVE", "SAFE_NONTRAINABLE", "CODE_OR_EVIDENCE_BUG", "SAFETY_OR_RETURN_FAILURE"}
    lifecycle = ResidentLifecycle()
    lifecycle.provide_thresholds(RuntimeThresholds("step5d.autotune-v4/r006-runtime-thresholds-v1", "r006-v1", 0.05, 0.2))
    lifecycle.transition(LifecycleState.DISPATCH)
    lifecycle.arm(execution_id="execution-hard", now_s=0.0)
    lifecycle.transition(LifecycleState.STOPPED)
    with pytest.raises(R006RuntimeError):
        lifecycle.safe_return()
    assert apply_disposition(ResidentLifecycle(), Disposition.SAFE_NONTRAINABLE) == "CONTINUE_AT_HOME"
    store_path = tmp_path / "attempt-store"
    store = OfflineR006AttemptStore(store_path)
    identity = AttemptIdentity(9, "execution-9", "logical-9", "BO_TRIAL", ANCHOR_POINT)
    store.record_incomplete(identity)
    resumed = store.resume_after_home(identity)
    assert resumed.attempt_sequence == identity.attempt_sequence
    assert resumed.logical_request_uid == identity.logical_request_uid
    assert resumed.execution_id != identity.execution_id
    store.record_complete(resumed)
    reloaded = OfflineR006AttemptStore(store_path)
    assert any(row.identity.execution_id == resumed.execution_id and row.complete for row in reloaded.rows)
    with pytest.raises(R006RuntimeError, match="cannot be rerun"):
        reloaded.record_complete(resumed)


def test_full_fake_qualification_warm_start_threshold_pac_retests_complete(tmp_path: Path) -> None:
    result = run_fake_campaign(tmp_path)
    assert result.status == "COMPLETE"
    assert len(result.qualification_execution_ids) == 3
    assert len(set(result.qualification_execution_ids)) == 3
    assert len(result.first_warm_start) == 25
    assert len(result.second_warm_start) == 25
    assert result.hyperparameters_frozen
    assert 4 in {attempt.identity.attempt_sequence for attempt in result.attempts}
    # The accepted r006 route records its safe graph observations between the
    # two exact warm-start groups; those observations consume one additional
    # physical attempt compared with the earlier descriptor fixture.
    assert result.attempts[-1].identity.attempt_sequence == 58
    assert result.scheduler_q_history == (4, 4, 1)
    assert result.pending_cancelled == 2
    assert result.timing.passes and result.echo_backlog == 0
    assert result.completion.application_accepted
    assert result.completion.local_pac_accepted
    assert result.completion.retest_passes == 3
    assert result.completion.retest_median_n is not None and result.completion.retest_median_n < 0.95 * result.completion.anchor_median_n
    assert result.path_snapshot_stage_id == "step5d_strict_rnn_autotune_v1"
    assert result.live_evidence is False


def test_package_stage_and_cli_are_offline_and_no_ordinal_stop() -> None:
    contract = load_contract()
    for suffix in (".script", ".txt", ".urp", ".numeric-sanity.json", ".deploy-manifest.json"):
        path = ROOT / "programs/step5/step5d" / f"{contract.program}{suffix}"
        assert path.is_file()
    script = (ROOT / "programs/step5/step5d" / f"{contract.program}.script").read_text(encoding="utf-8")
    assert "READY_HOME_NEXT" in script and "ACTIVE_LEASE_S = 0.080000000" in script
    assert "stop-after-ordinal" not in script
    package_manifest = json.loads((ROOT / "programs/step5/step5d" / f"{contract.program}.deploy-manifest.json").read_text(encoding="utf-8"))
    assert package_manifest["controller_upload"] is False
    stage = json.loads((ROOT / "config/step5_stage_table.json").read_text(encoding="utf-8"))
    ids = {row["id"] for row in stage["stages"]}
    assert "step5d_strict_rnn_autotune_v4_r006" in ids
    assert "Staged independent V4 r006 offline implementation" in (ROOT / "STEP5D_FLOW.md").read_text(encoding="utf-8")
    tree = ast.parse((ROOT / "tools/run_step5d_autotune_v4_r006.py").read_text(encoding="utf-8"))
    names = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "socket" not in names
    live_help = __import__("subprocess").run(
        [sys.executable, str(ROOT / "tools/run_step5d_autotune_v4_r006.py"), "live", "--help"],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": f"{ROOT / 'tools'}:{ROOT.parents[1] / 'src' / 'ur10e_experiment_runtime'}",
        },
    ).stdout
    assert "stop-after-ordinal" not in live_help
