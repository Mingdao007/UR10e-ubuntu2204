from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_v4_offline_closure as closure_builder  # noqa: E402
import build_step5d_autotune_v4_r003 as tp_builder  # noqa: E402
import transition_step5d_lineage as lineage_transition  # noqa: E402
from step5d_autotune_v4 import baseline, bo, contracts, eligibility, entry, replay, runtime  # noqa: E402
from step5d_autotune_v4.control import RuntimeObservation, V4ControlPrimitive  # noqa: E402
from step5d_autotune_v4.adapter import AdapterTick, V4RuntimeAdapter, seal_attempt_ledger  # noqa: E402
from step5d_autotune_v4.baseline_ledger import BaselineQualificationSnapshot  # noqa: E402
from step5d_autotune_v4.tp import CONTROLLER_DIR, render_script  # noqa: E402
from step5d_autotune_v4.wire import CommandMode, LAYOUT_CODE, SensorPacket, build_wire_packet  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_v4_is_independent_and_v3_bytes_contract_remains_12n_fixed_2ms() -> None:
    v4 = contracts.load_contract()

    assert v4.raw["isolation"]["v3_current_pointer_mutation_allowed"] is False
    assert v4.raw["isolation"]["v3_observations_import_allowed"] is False
    assert v4.raw["isolation"]["v3_gp_import_allowed"] is False
    assert v4.raw["isolation"]["v3_incumbent_import_allowed"] is False
    v3_source = (ROOT / "tools/step5d_autotune_contract.py").read_text(
        encoding="utf-8"
    )
    assert "TARGET_FORCE_N = 12.0" in v3_source
    assert 'NORMAL_FILTER_DT_MODE = "fixed_0.002s"' in v3_source
    assert _sha(ROOT / "tools/step5d_autotune_contract.py") == (
        "973adc8c73e0187f8b4a5ef8dc6520890530b436b0eddaad02f41444bf5cafd6"
    )
    assert _sha(ROOT / "config/step5d/current.json") == (
        "646edefaf6fbbd76b0cbfe86bc00b5d8ac26b787231f9ac345425256bbc78f5e"
    )
    assert _sha(
        ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r034.urp"
    ) == "10f482d8c8e305b1aea979ceefb2cd24ef1fbc79e5e22235dae8e4a2b9093f66"


def test_v4_contract_binds_eoat_and_model_hashes() -> None:
    value = contracts.load_contract()

    assert value.eoat_sha256 == _sha(
        ROOT / "config/step5d/eoat_profile_new_v4.json"
    )
    assert value.raw["fingerprint"]["target_force_n"] == 5.0
    assert value.raw["fingerprint"]["wrench_authority"] == "kunwei_only"
    assert value.raw["fingerprint"]["ur_builtin_force_allowed"] is False
    assert len(value.campaign_fingerprint) == 64
    assert set(value.model_hashes) == {
        "ur_xacro",
        "calibration_yaml",
        "jacobian_gate",
        "kinematics_solver",
        "strict_rnn",
        "contact_semantics",
    }


def test_v4_tp_has_distinct_entry_search_and_post_contact_stop_contracts() -> None:
    script = render_script()

    assert script.count("movel(") == 3
    assert script.count("r=0.0)") == 3
    assert "stopl(0.250000000)" in script
    assert "stopl(0.010000000)" in script
    assert (
        "codex_v4_stationary_dwell(0.250000000, 3.0, 3.0, 0.2)"
        in script
    )
    assert (
        "codex_v4_stationary_dwell(0.250000000, 15.0, 20.0, 1.0)"
        in script
    )
    assert "get_actual_tcp_pose()[2]" not in script
    assert "force_mode(" not in script
    assert "get_tcp_force(" not in script
    assert "movej(" not in script


def test_v4_triplet_round_trip_and_local_manifest(tmp_path: Path) -> None:
    stamp = "2026-07-30TTESTHKT_STEP5D_AUTOTUNE_V4_R003"
    result = tp_builder.write_triplet(tmp_path, stamp)

    script = (tmp_path / f"{contracts.PROGRAM}.script").read_text()
    txt = (tmp_path / f"{contracts.PROGRAM}.txt").read_text()
    urp = (tmp_path / f"{contracts.PROGRAM}.urp").read_bytes()
    assert all(tp_builder.validate_triplet(script, txt, urp, stamp).values())
    assert result["controller_target"] == f"{CONTROLLER_DIR}/{contracts.PROGRAM}.urp"
    manifest = json.loads(
        (tmp_path / f"{contracts.PROGRAM}.deploy-manifest.json").read_text()
    )
    assert manifest["delivery"]["controller_load_or_play"] is False
    assert manifest["delivery"]["v3_current_pointer_changed"] is False
    with pytest.raises(FileExistsError, match="immutable"):
        tp_builder.write_triplet(tmp_path, stamp)


def test_v4_wire_is_stop_dominant_and_target_is_not_a_tunable() -> None:
    contract = contracts.load_contract()
    identity = [
        [1.0 if row == column else 0.0 for column in range(6)]
        for row in range(6)
    ]
    sensor = SensorPacket(
        normal_load_n=5.0,
        force_norm_n=5.1,
        heartbeat=10.0,
        sensor_fresh=True,
        stop_request=False,
        eoat_get_ack=True,
        torque_norm_nm=0.1,
        wrench=(0.0, 0.0, 5.0, 0.0, 0.0, 0.1),
        filtered_normal_load_n=5.0,
    )
    packet = build_wire_packet(
        contract,
        contracts.V4Candidate(),
        sensor=sensor,
        proposed_qdot=(0.0, 0.0, 0.0003, 0.0, 0.0, 0.0),
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
        internal_setpoint_n=5.0,
        command_sequence=1,
        baseline_qualification=BaselineQualificationSnapshot(0, (), False, ""),
        command_mode=CommandMode.BASELINE,
    )
    assert packet.doubles_by_register[43] == 1.0
    assert packet.doubles_by_register[44] == 5.0
    assert packet.doubles_by_register[47] == LAYOUT_CODE
    assert packet.integer_values == (0, int(CommandMode.BASELINE))

    stopped = build_wire_packet(
        contract,
        contracts.V4Candidate(),
        sensor=replace(sensor, sensor_fresh=False),
        proposed_qdot=(0.0, 0.0, 0.0003, 0.0, 0.0, 0.0),
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
        internal_setpoint_n=5.0,
        command_sequence=2,
        baseline_qualification=BaselineQualificationSnapshot(0, (), False, ""),
        command_mode=CommandMode.BASELINE,
    )
    assert stopped.stop_dominant
    assert stopped.doubles_by_register[28] == 1.0
    assert tuple(stopped.doubles_by_register[index] for index in range(37, 43)) == (
        0.0,
    ) * 6


def test_target_is_asserted_at_construction_decode_and_runtime() -> None:
    with pytest.raises(contracts.V4ContractError, match="target_force_n"):
        contracts.V4Candidate(target_force_n=12.0)
    with pytest.raises(contracts.V4ContractError, match="decoded target_force_n"):
        contracts.decode_named7d((0, 0, 0, 0, 1, 0, 0), target_force_n=12.0)
    with pytest.raises(contracts.V4ContractError, match="runtime target_force_n"):
        contracts.assert_runtime_target(contracts.V4Candidate(), 12.0)


def test_typed_i_off_round_trip_is_canonical() -> None:
    off = contracts.V4Candidate()
    encoded = contracts.encode_named7d(off)

    assert encoded == pytest.approx((0, 0, 0, 0, 1, 0, 0))
    assert contracts.decode_named7d(encoded) == off
    with pytest.raises(contracts.V4ContractError, match="canonical"):
        contracts.decode_named7d((0, 0, 0, -1, 1, 0, 0))

    on = replace(off, force_i_gain=5e-6)
    encoded_on = contracts.encode_named7d(on)
    assert encoded_on[3:5] == pytest.approx((-1.0, 0.0))
    assert contracts.decode_named7d(encoded_on) == on


def test_i_off_to_on_counts_one_physical_transition() -> None:
    off = contracts.V4Candidate()
    on = replace(off, force_i_gain=5e-6)

    assert contracts.changed_physical_coordinates(off, on) == ("force_i_gain",)
    assert contracts.validate_live_transition(off, on) == ("force_i_gain",)


def test_live_transition_rejects_two_coordinates_or_more_than_quarter_octave() -> None:
    anchor = contracts.V4Candidate()
    with pytest.raises(contracts.V4ContractError, match="exactly one"):
        contracts.validate_live_transition(
            anchor,
            replace(
                anchor,
                force_p_gain=anchor.force_p_gain * 2**0.25,
                force_damping=anchor.force_damping * 2**0.25,
            ),
        )
    with pytest.raises(contracts.V4ContractError, match="exceeds"):
        contracts.validate_live_transition(
            anchor, replace(anchor, force_p_gain=anchor.force_p_gain * 2**0.5)
        )


def test_initial_batches_and_stage_order_match_plan() -> None:
    batch_a, batch_b = bo.initial_pd_batches()

    assert len(batch_a) == len(batch_b) == 5
    assert batch_a[:3] == (bo.anchor(),) * 3
    assert batch_b[:3] == (bo.anchor(),) * 3
    assert tuple(value.force_p_gain for value in batch_a[3:]) == pytest.approx(
        bo.P_PROBES
    )
    assert tuple(value.force_damping for value in batch_b[3:]) == pytest.approx(
        bo.D_PROBES
    )
    assert tuple(
        value.normal_filter_tau_s
        for value in bo.tau_qualification_batch(bo.anchor())[3:]
    ) == pytest.approx(bo.TAU_PROBES)
    assert tuple(
        math.log2(value.force_i_gain / contracts.I_ON_ANCHOR)
        for value in bo.i_qualification_path(bo.anchor())
    ) == pytest.approx(contracts.I_GRID)


def test_entry_is_rise_transfer_descend_r0_and_contact_z_is_evidence_only() -> None:
    current = (0.49, 0.13, 0.03, 3.1, 0.0, 0.08)
    plan = entry.plan_entry(current)

    entry.validate_entry_plan(current, plan)
    assert tuple(segment.name for segment in plan) == (
        "rise",
        "xy_orientation_transfer",
        "descend",
    )
    assert plan[0].target_pose[2] == pytest.approx(entry.TRANSFER_Z_FLOOR_M)
    assert plan[1].linear_speed_m_s <= 0.01
    assert plan[1].angular_speed_cap_rad_s <= 0.10
    assert plan[2].linear_speed_m_s <= 0.005
    assert all(segment.blend_radius_m == 0.0 for segment in plan)
    assert entry.EXPECTED_CONTACT_Z_M_EVIDENCE_ONLY not in {
        segment.target_pose[2] for segment in plan
    }
    assert entry.ENTRY_STOPL_ACCELERATION_M_S2 == pytest.approx(0.25)
    assert entry.SEARCH_STOPL_ACCELERATION_M_S2 == pytest.approx(0.01)


def test_baseline_ramps_one_to_five_in_eight_seconds_and_has_no_xy_or_angular() -> None:
    candidate = contracts.V4Candidate()
    state = baseline.BaselineState()
    observation = baseline.BaselineObservation(
        dt_s=0.01,
        one_newton_latched=True,
        filtered_normal_n=1.0,
        raw_normal_n=1.0,
        force_norm_n=1.0,
        torque_norm_nm=0.01,
        sensor_fresh=True,
        stationary=False,
    )
    state, command = baseline.step_baseline(candidate, state, observation)
    for _ in range(799):
        state, command = baseline.step_baseline(candidate, state, observation)

    assert command.internal_setpoint_n == pytest.approx(5.0)
    assert command.candidate_target_force_n == 5.0
    assert abs(command.approach_speed_m_s) <= 0.0005
    assert command.xy_velocity_m_s == (0.0, 0.0)
    assert command.angular_velocity_rad_s == (0.0, 0.0, 0.0)


def test_baseline_success_requires_readiness_dwell_plus_ten_second_hold() -> None:
    candidate = contracts.V4Candidate()
    state = baseline.BaselineState(
        phase=baseline.BaselinePhase.ACQUIRE, after_latch_s=8.0
    )
    ready = baseline.BaselineObservation(
        dt_s=0.01,
        one_newton_latched=True,
        filtered_normal_n=5.0,
        raw_normal_n=5.0,
        force_norm_n=5.0,
        torque_norm_nm=0.1,
        sensor_fresh=True,
        stationary=False,
    )
    for _ in range(25 + 1000):
        state, command = baseline.step_baseline(candidate, state, ready)

    assert state.phase is baseline.BaselinePhase.SUCCESS
    assert not command.stop
    assert command.approach_speed_m_s == 0.0
    assert not command.retract_allowed
    stationary = replace(ready, stationary=True)
    state, command = baseline.step_baseline(candidate, state, stationary)
    assert command.retract_allowed
    assert command.auto_home is False


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (
            baseline.BaselineObservation(
                0.01, True, 5.0, 15.0, 15.0, 0.1, True, False
            ),
            "hard_abs_normal_15n",
        ),
        (
            baseline.BaselineObservation(
                0.01, True, 5.0, 5.0, 20.0, 0.1, True, False
            ),
            "hard_force_norm_20n",
        ),
        (
            baseline.BaselineObservation(
                0.01, True, 5.0, 5.0, 5.0, 1.0, True, False
            ),
            "hard_torque_norm_1nm",
        ),
    ],
)
def test_baseline_hard_guards_fail_closed(
    observation: baseline.BaselineObservation, reason: str
) -> None:
    state, command = baseline.step_baseline(
        contracts.V4Candidate(), baseline.BaselineState(), observation
    )

    assert state.phase is baseline.BaselinePhase.FAILED
    assert state.stop_reason == reason
    assert command.stop
    assert not command.retract_allowed


def test_three_consecutive_baseline_successes_unlock_full_path() -> None:
    success = baseline.BaselineState(phase=baseline.BaselinePhase.SUCCESS)
    failure = baseline.BaselineState(phase=baseline.BaselinePhase.FAILED)

    assert baseline.baseline_unlock_allowed([success, success, success])
    assert not baseline.baseline_unlock_allowed([success, failure, success])


def test_actual_dt_timing_acceptance_and_immediate_gap_stops() -> None:
    guard = runtime.TimingGuard()
    for index in range(1000):
        guard.observe(index * 0.01)
    result = guard.acceptance()
    assert result["passed"]
    assert result["rate_hz"] == pytest.approx(100.0)
    assert result["p99_gap_s"] <= 0.02

    duplicate = runtime.TimingGuard()
    duplicate.observe(1.0)
    duplicate.observe(1.0)
    assert duplicate.stopped
    assert duplicate.stop_reason == "nonpositive_actual_dt"

    stale = runtime.TimingGuard()
    stale.observe(1.0)
    stale.observe(1.08)
    assert stale.stopped
    assert stale.stop_reason == "actual_gap_at_least_80ms"


def test_v4_startup_is_two_increments_within_250ms() -> None:
    gate = runtime.StartupHeartbeatGate()

    assert not gate.observe(0.0, 0.0)
    assert not gate.observe(1.0, 0.10)
    assert gate.observe(2.0, 0.20)


def test_hash_bound_jacobian_gate_checks_all_velocity_caps() -> None:
    contract = contracts.load_contract()
    identity = [[1.0 if row == column else 0.0 for column in range(6)] for row in range(6)]
    allowed = runtime.gate_qdot(
        contract,
        qdot=(0.0003, 0.0, 0.0003, 0.0, 0.0, 0.0),
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )
    assert allowed.allowed
    assert allowed.total_linear_m_s <= 0.0005
    assert allowed.normal_m_s <= 0.00035
    assert allowed.tangential_m_s <= 0.00035

    blocked = runtime.gate_qdot(
        contract,
        qdot=(0.0, 0.0, 0.00036, 0.0, 0.0, 0.0),
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )
    assert not blocked.allowed
    assert blocked.reason == "normal_component_cap"
    assert blocked.qdot == (0.0,) * 6

    with pytest.raises(runtime.RuntimeGuardError, match="hash"):
        runtime.gate_qdot(
            contract,
            qdot=(0.0,) * 6,
            jacobian_6x6=identity,
            normal_base=(0.0, 0.0, 1.0),
            observed_model_hashes={**contract.model_hashes, "jacobian_gate": "0" * 64},
        )


def _eligible_attempt(contract: contracts.V4Contract) -> eligibility.AttemptEvidence:
    return eligibility.AttemptEvidence(
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256=contract.eoat_sha256,
        target_force_n=5.0,
        kunwei_only_receipt=True,
        complete_bins=550,
        terminal_closure=True,
        completion_closure=True,
        replay_closure=True,
        safety_failure=False,
        structural_failure=False,
        stale_or_nonfinite=False,
        incomplete=False,
        objective=0.5,
        mae_n=0.25,
        p99_normal_n=7.0,
        max_force_norm_n=9.0,
        max_torque_norm_nm=0.2,
    )


def test_eligibility_allows_safe_bad_performance_but_not_incomplete_or_safety() -> None:
    contract = contracts.load_contract()
    safe_bad = replace(_eligible_attempt(contract), objective=10.0, mae_n=4.0)
    decision = eligibility.evaluate_attempt(contract, safe_bad, prior_eligible_count=10)
    assert decision.eligible
    assert decision.disposition is eligibility.Disposition.GP_OBSERVATION

    incomplete = replace(safe_bad, incomplete=True, complete_bins=500)
    decision = eligibility.evaluate_attempt(contract, incomplete, prior_eligible_count=10)
    assert not decision.eligible
    assert decision.disposition is eligibility.Disposition.ATTEMPT_LEDGER_ONLY

    unsafe = replace(safe_bad, safety_failure=True)
    decision = eligibility.evaluate_attempt(contract, unsafe, prior_eligible_count=10)
    assert decision.disposition is eligibility.Disposition.FREEZE_BO
    assert decision.freeze_bo
    assert decision.zero_qdot_and_stop


def test_first_ten_gate_and_later_guard_escalation() -> None:
    contract = contracts.load_contract()
    good = _eligible_attempt(contract)
    too_high = replace(good, p99_normal_n=8.1)

    assert not eligibility.evaluate_attempt(
        contract, too_high, prior_eligible_count=0
    ).eligible
    assert eligibility.hard_guards_for_campaign([good] * 9) == (15.0, 20.0, 1.0)
    assert eligibility.hard_guards_for_campaign([good] * 10) == (25.0, 30.0, 1.5)
    assert (100.0, 120.0, 5.0) != eligibility.hard_guards_for_campaign([good] * 10)


def test_promotion_requires_two_of_three_mae_and_five_percent_median_gain() -> None:
    assert eligibility.promotion_allowed(
        anchor_objective=1.0,
        retest_mae_n=(0.29, 0.30, 0.5),
        retest_objectives=(0.94, 0.95, 0.96),
    )
    assert not eligibility.promotion_allowed(
        anchor_objective=1.0,
        retest_mae_n=(0.29, 0.31, 0.5),
        retest_objectives=(0.90, 0.91, 0.92),
    )
    assert not eligibility.promotion_allowed(
        anchor_objective=1.0,
        retest_mae_n=(0.2, 0.2, 0.2),
        retest_objectives=(0.96, 0.97, 0.98),
    )


def test_offline_replay_produces_exact_550_bins_from_actual_timestamps() -> None:
    rows = [
        replay.ReplayRow(
            monotonic_s=index * 0.01,
            filtered_normal_n=5.0 + 0.1 * math.sin(index * 0.01),
            raw_normal_n=5.0 + 0.15 * math.sin(index * 0.01),
            force_norm_n=5.1,
            torque_norm_nm=0.1,
            sensor_fresh=True,
        )
        for index in range(6001)
    ]
    result = replay.replay(rows)

    assert result.complete_bins == result.required_bins == 550
    assert result.eligible_shape
    assert result.mae_n is not None and result.mae_n < 0.1
    assert result.timing["passed"]


def test_composed_control_zeroes_qdot_and_freezes_on_structural_failure() -> None:
    contract = contracts.load_contract()
    primitive = V4ControlPrimitive(contract, contracts.V4Candidate())
    identity = [
        [1.0 if row == column else 0.0 for column in range(6)]
        for row in range(6)
    ]
    observation = RuntimeObservation(
        monotonic_s=0.0,
        heartbeat=0.0,
        one_newton_latched=False,
        filtered_normal_n=0.0,
        raw_normal_n=0.0,
        force_norm_n=0.0,
        torque_norm_nm=0.0,
        sensor_fresh=True,
        stationary=True,
        structural_failure=True,
    )

    decision = primitive.step(
        observation,
        proposed_qdot=(0.01,) * 6,
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )

    assert decision.freeze_bo
    assert decision.stop
    assert decision.qdot == (0.0,) * 6
    assert decision.reason == "structural_failure"


def test_runtime_adapter_startup_holds_without_stop_then_hash_failure_freezes(
    tmp_path: Path,
) -> None:
    contract = contracts.load_contract()
    identity = tuple(
        tuple(1.0 if row == column else 0.0 for column in range(6))
        for row in range(6)
    )
    sensor = SensorPacket(
        normal_load_n=1.0,
        force_norm_n=1.0,
        heartbeat=0.0,
        sensor_fresh=True,
        stop_request=False,
        eoat_get_ack=True,
        torque_norm_nm=0.1,
        wrench=(0.0, 0.0, -1.0, 0.0, 0.0, 0.1),
        filtered_normal_load_n=1.0,
    )
    adapter = V4RuntimeAdapter(contract, contracts.V4Candidate())
    first = adapter.tick(
        AdapterTick(
            observation=RuntimeObservation(
                monotonic_s=0.0,
                heartbeat=0.0,
                one_newton_latched=False,
                filtered_normal_n=1.0,
                raw_normal_n=1.0,
                force_norm_n=1.0,
                torque_norm_nm=0.1,
                sensor_fresh=True,
                stationary=True,
            ),
            sensor=sensor,
            proposed_qdot=(0.0,) * 6,
            jacobian_6x6=identity,
            normal_base=(0.0, 0.0, 1.0),
            observed_model_hashes=contract.model_hashes,
            command_sequence=0,
        )
    )
    assert first.decision.reason == "startup_two_increments_pending"
    assert not first.packet.stop_dominant
    assert first.packet.doubles_by_register[43] == 1.0
    assert tuple(first.packet.doubles_by_register[index] for index in range(37, 43)) == (
        0.0,
    ) * 6

    adapter.control.startup_ready = True
    adapter.control.policies.timing.observe_startup(1.0, 0.01)
    adapter.control.policies.timing.observe_startup(2.0, 0.02)
    failed = adapter.tick(
        AdapterTick(
            observation=RuntimeObservation(
                monotonic_s=0.03,
                heartbeat=3.0,
                one_newton_latched=True,
                filtered_normal_n=1.0,
                raw_normal_n=1.0,
                force_norm_n=1.0,
                torque_norm_nm=0.1,
                sensor_fresh=True,
                stationary=False,
            ),
            sensor=sensor,
            proposed_qdot=(0.0,) * 6,
            jacobian_6x6=identity,
            normal_base=(0.0, 0.0, 1.0),
            observed_model_hashes={**contract.model_hashes, "jacobian_gate": "0" * 64},
            command_sequence=1,
        )
    )
    assert failed.decision.freeze_bo
    assert failed.packet.stop_dominant
    assert tuple(failed.packet.doubles_by_register[index] for index in range(37, 43)) == (
        0.0,
    ) * 6

    ledger = tmp_path / "attempt.jsonl"
    seal = seal_attempt_ledger(
        (first.ledger_row, failed.ledger_row),
        ledger,
        terminal_closure={"matched": True},
        completion_closure={"matched": True},
        replay_closure={"matched": False},
    )
    assert seal["rows"] == 2
    assert seal["gp_eligibility_decided"] is False


def test_composed_baseline_forbids_tangential_or_angular_motion() -> None:
    contract = contracts.load_contract()
    primitive = V4ControlPrimitive(contract, contracts.V4Candidate())
    identity = [
        [1.0 if row == column else 0.0 for column in range(6)]
        for row in range(6)
    ]
    common = dict(
        one_newton_latched=True,
        filtered_normal_n=1.0,
        raw_normal_n=1.0,
        force_norm_n=1.0,
        torque_norm_nm=0.01,
        sensor_fresh=True,
        stationary=False,
    )
    primitive.step(
        RuntimeObservation(monotonic_s=0.0, heartbeat=0.0, **common),
        proposed_qdot=(0.0,) * 6,
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )
    primitive.step(
        RuntimeObservation(monotonic_s=0.01, heartbeat=1.0, **common),
        proposed_qdot=(0.0,) * 6,
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )
    decision = primitive.step(
        RuntimeObservation(monotonic_s=0.02, heartbeat=2.0, **common),
        proposed_qdot=(0.0001, 0.0, 0.0001, 0.0, 0.0, 0.0),
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )

    assert decision.stop
    assert decision.qdot == (0.0,) * 6
    assert decision.reason == "baseline_forbids_xy_or_angular_motion"


def test_offline_closure_binds_sources_replay_and_unchanged_v3() -> None:
    payload = closure_builder.build_closure()

    assert payload["lineage"] == "step5d_strict_rnn_autotune_v4"
    assert payload["program"] == contracts.PROGRAM
    assert payload["v3_isolation"]["verified_unchanged"]
    assert not payload["v3_isolation"]["v3_current_pointer_changed"]
    assert payload["replay"]["complete_bins"] == 550
    assert payload["replay"]["eligible_shape"]
    assert payload["live_status"].startswith("BLOCKED_PENDING")


def test_two_level_selector_keeps_v3_active_and_v4_staged() -> None:
    inspection = lineage_transition.inspect_transition()

    assert not inspection["ready"]
    assert inspection["active_lineage"] == "step5d_strict_rnn_autotune_v3"
    assert inspection["staged_lineage"] == "step5d_strict_rnn_autotune_v4"
    assert inspection["v3_pointer_will_be_modified"] is False
    assert inspection["v3_pointer_sha256"] == (
        "646edefaf6fbbd76b0cbfe86bc00b5d8ac26b787231f9ac345425256bbc78f5e"
    )
    assert inspection["blockers"] == ["v4_activation_evidence_missing"]
    assert inspection["active_eoat"]["live_compatible_now"] is False
    assert inspection["active_eoat"]["blockers"] == [
        "active_v3_eoat_fresh_apply_verify_receipt_missing",
        "active_v3_eoat_live_compatibility_not_verified",
    ]
    resolved = lineage_transition.resolve_active_eoat()
    assert not resolved["compatible"]
    assert "active_eoat_fresh_apply_verify_receipt_missing" in resolved["blockers"]
    assert not (ROOT / "config/eoat_context.json").exists()
