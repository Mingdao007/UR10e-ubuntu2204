from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
TEST_FIGURE8_FINGERPRINT = "1" * 64
CENSOR_HOME_CALIBRATION_SHA256 = "9" * 64
CENSOR_HOME_OBSERVATION = {
    "fresh": True,
    "safety_normal": True,
    "stationary": True,
    "pose_closed": True,
}

from build_step6_figure8_autotune_v1 import (  # noqa: E402
    PROGRAM_NAME,
)
import run_step6_figure8_autotune_v1_live as figure8_live_cli  # noqa: E402
from build_step6_figure8_no_contact_canary_v1 import (  # noqa: E402
    ANALYTIC_PEAK_SPEED_M_S as CANARY_ANALYTIC_PEAK_SPEED_M_S,
    COMMAND_SPEED_CAP_M_S as CANARY_COMMAND_SPEED_CAP_M_S,
    build_script as build_no_contact_script,
    build_txt as build_no_contact_txt,
    numeric_sanity as no_contact_numeric_sanity,
    validate_package as validate_no_contact_package,
)
from build_step6a_eight_no_contact import build_urp as build_tp_urp  # noqa: E402
from run_step6_figure8_autotune_v1_live import (  # noqa: E402
    FakeWriter,
    SafetyFaultError,
    execute_candidate_transaction,
)
import run_step6_figure8_autotune_v1_live as step6_entry  # noqa: E402
from step6_figure8_autotune_v1.campaign_runner import (  # noqa: E402
    SafeReturnProposalOverlapV1,
)
from run_step6_figure8_no_contact_canary_v1_live import summarize_trace  # noqa: E402
from step6_figure8_autotune_v1 import (  # noqa: E402
    CorrectionPolicyV1,
    CorrectionStateV1,
    CompleteCandidateV1,
    EvidenceReferenceV1,
    FigureEightLaunchReceiptV1,
    FigureEightPathProviderV1,
    FigureEightSchedulerV1,
    FigureEightCensoredReceiptV1,
    ProductionProposalUnavailable,
    ReportEvidenceContractV1,
    OfflineCampaignV1,
    PersistedSobolCursorV1,
    R013ProductionProposalProviderV1,
    StrictAdmissionV1,
    TrialEvidenceV1,
    build_campaign_fingerprint,
    build_frozen_campaign_fingerprint,
    build_report_evidence_contract,
    evaluate_figure8_censor_prefix,
    make_figure8_censored_receipt,
    load_campaign_config,
    make_metric_result,
    repeat_aware_yvar,
    OperationalSegmentBudgetV1,
)
from step6_figure8_autotune_v1.physical_censor import (  # noqa: E402
    FigureEightCensorRuntimeError,
    FigureEightPhysicalCensorSeamV1,
    next_cold_attempt_sequence,
)
from step6_figure8_autotune_v1.live_composition import (  # noqa: E402
    FIGURE8_ANALYTIC_PEAK_SPEED_M_S,
    FIGURE8_CALIBRATION_HOME_POSE,
    FIGURE8_CALIBRATION_HOME_PROFILE_ID,
    FIGURE8_FINAL_HOME_OFFSET_M,
    FIGURE8_FINAL_HOME_PROFILE_ID,
    FIGURE8_HOME_POSE,
    FigureEightHomeReferenceV1,
    FigureEightPathEvidenceCollectorV1,
    FigureEightPathGuardStackV1,
    figure8_fresh_frame_wait_policy,
    figure8_motion_profile,
    figure8_motion_profile_receipt,
    figure8_path_provider,
    figure8_home_profile,
    derive_figure8_home_calibration_receipt,
    derive_figure8_home_start_receipt,
    load_figure8_home_calibration_receipt,
    load_figure8_home_binding,
    figure8_runtime_path_reference,
    live_composition_receipt,
)
from step6_figure8_autotune_v1.physical_candidate import (  # noqa: E402
    FigureEightPhysicalCandidateV1,
)
from step6_figure8_autotune_v1.physical_ledger import (  # noqa: E402
    FigureEightPhysicalLedgerError,
    FigureEightPhysicalLedgerV1,
)
from step6_figure8_autotune_v1.prepare_live import verify_controller_readback  # noqa: E402
from step6_figure8_autotune_v1.source_identity import build_source_identity  # noqa: E402
from step5d_autotune_v4_r005.observations import ObservationRecord  # noqa: E402
from step5d_force_objective import ForcePathSample  # noqa: E402
from step5d_autotune_v4_r004.path_reference import PATH_STAGE_ID  # noqa: E402
from step5d_autotune_v4_r006.live_adapter import (  # noqa: E402
    R006HomeBindingV1,
    R006HomeStartReceiptV1,
    R006PathEvidenceCollector,
    _R006ScopedRuntimeInjection,
)
from step5d_autotune_v4_r004.evidence import PathSample  # noqa: E402
from step5d_eoat_profiles import load_new_eoat_profile  # noqa: E402
from step5d_autotune_v4_r012.path_cbf_live import R012GuardStop  # noqa: E402


def _fingerprint():
    config = load_campaign_config()
    package_dir = ROOT / "programs" / "step6"
    triplet = {
        suffix: hashlib.sha256((package_dir / f"{PROGRAM_NAME}.{suffix}").read_bytes()).hexdigest()
        for suffix in ("script", "txt", "urp")
    }
    return config, build_campaign_fingerprint(
        config=config,
        source_sha256=config.raw["controller"]["source_parent_sha256"],
        controller_triplet_sha256=triplet,
    )


def _complete_samples() -> tuple[dict[str, float], ...]:
    return tuple({"time_s": round(index * 0.1, 10), "normal_load_n": 5.0} for index in range(600))


def test_dry_run_forwards_explicit_offline_evidence_without_live_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readback_dir = tmp_path / "fresh-readback"
    canary_dir = tmp_path / "fresh-canary"
    home_receipt = tmp_path / "home-receipt.json"
    captured: dict[str, object] = {}
    original_load_fingerprint = figure8_live_cli.load_fingerprint

    def fake_validate(config_path: Path, **kwargs: object) -> dict[str, object]:
        captured["validate_config"] = config_path
        captured.update(kwargs)
        return {"controller_readback": True, "no_contact_canary": True}

    def capture_fingerprint(config, *, home_calibration_receipt=None):
        captured["fingerprint_home_receipt"] = home_calibration_receipt
        return original_load_fingerprint(config)

    monkeypatch.setattr(figure8_live_cli, "validate_offline_surfaces", fake_validate)
    monkeypatch.setattr(figure8_live_cli, "load_fingerprint", capture_fingerprint)

    result = figure8_live_cli.dry_run(
        ROOT / "config" / "step6" / "r013_figure8_direct_campaign_v1.json",
        tmp_path / "state",
        controller_readback_dir=readback_dir,
        canary_dir=canary_dir,
        home_calibration_receipt=home_receipt,
        test_mode=True,
    )

    assert captured["controller_readback_dir"] == readback_dir
    assert captured["canary_dir"] == canary_dir
    assert captured["home_calibration_receipt"] == home_receipt
    assert captured["fingerprint_home_receipt"] == home_receipt
    assert result["surfaces"]["controller_readback"] is True
    assert result["live_executed"] is False


def test_configured_source_identity_matches_explicit_function_closure() -> None:
    identity = build_source_identity(ROOT)
    assert identity["source_sha256"] == load_campaign_config().raw["controller"][
        "source_parent_sha256"
    ]
    assert identity["config_self_reference_policy"] == (
        "source_parent_sha256_zeroed_before_hash"
    )


def test_path_metric_and_raw_gap_seal_are_exact() -> None:
    provider = FigureEightPathProviderV1()
    assert provider.duration_s == 60.0
    assert provider.sample(0.0).scalar_speed_m_s == pytest.approx(0.00447213595)
    result = make_metric_result(_complete_samples())
    assert result.metric_fingerprint == load_campaign_config().metric
    assert result.observed_formal_bin_count == 550
    assert result.formal_mae_n == 0.0
    with pytest.raises(Exception, match="formal gaps"):
        make_metric_result(_complete_samples()[:-1])


def test_figure8_contact_search_restores_v4_equivalent_contract() -> None:
    config = load_campaign_config()
    contract = config.raw["contact_search"]
    assert contract["schema"] == "step6.autotune/figure8-contact-search-v4-equivalent-v1"
    assert contract["boundary_m"] == pytest.approx(0.011029311)
    assert contract["far_speed_m_s"] == pytest.approx(0.005)
    assert contract["near_speed_m_s"] == pytest.approx(0.0002)
    assert contract["far_acceleration_m_s2"] == pytest.approx(0.01)
    assert contract["near_acceleration_m_s2"] == pytest.approx(0.005)
    assert contract["fuse_n"] == pytest.approx(50.0)
    assert contract["max_travel_m"] == pytest.approx(0.025)
    assert contract["timeout_s"] == pytest.approx(90.0)
    assert contract["contract_basis"] == "v4_equivalent_restored_not_failed_16mm_experiment"


def test_controller_upload_manifest_is_normalized_after_three_way_sha_closure(tmp_path: Path) -> None:
    root = tmp_path / "experiment"
    local_dir = root / "programs" / "step6"
    readback_dir = root / "readback"
    local_dir.mkdir(parents=True)
    readback_dir.mkdir()
    hashes = {}
    for role, payload in (("script", b"script"), ("txt", b"txt"), ("urp", b"urp")):
        path = local_dir / f"step6_figure8_autotune_v1.{role}"
        path.write_bytes(payload)
        (readback_dir / path.name).write_bytes(payload)
        hashes[role] = hashlib.sha256(payload).hexdigest()
    manifest = {
        "status": "controller read-back verified",
        "target_dir": "/programs/andyl/kunwei/step6",
        "validation": {
            "program": "step6_figure8_autotune_v1",
            "target_dir": "/programs/andyl/kunwei/step6",
            "script_sha256": hashes["script"],
            "txt_sha256": hashes["txt"],
            "urp_sha256": hashes["urp"],
        },
        "sha256": {
            section: {f".{role}": digest for role, digest in hashes.items()}
            for section in ("local", "controller", "readback")
        },
        "fresh_controller_sha_verified": True,
        "readback_source": "fresh_controller_get",
    }
    (readback_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    normalized, triplet = verify_controller_readback(root=root, readback_dir=readback_dir)
    assert normalized["schema"] == "step6.figure8/controller-readback-v1"
    assert normalized["state"] == "controller_readback_verified"
    assert normalized["byte_for_byte_sha_closure"] is True
    assert triplet == hashes


def test_no_contact_canary_is_one_shot_guarded_and_returns_exact_figure8_home() -> None:
    stamp = "2026-08-16T0640HKT_STEP6_FIGURE8_NO_CONTACT_CANARY_V1"
    script = build_no_contact_script(stamp)
    txt = build_no_contact_txt(stamp)
    urp = build_tp_urp(
        script,
        "step6_figure8_no_contact_canary_v1",
        "/programs/andyl/kunwei/step6",
    )
    checks = validate_no_contact_package(script, txt, urp, stamp)
    sanity = no_contact_numeric_sanity()
    assert all(checks.values())
    assert CANARY_ANALYTIC_PEAK_SPEED_M_S < CANARY_COMMAND_SPEED_CAP_M_S
    assert sanity["path"]["duration_s"] == 60.0
    assert sanity["tracking_guard"]["hard_guard_evaluated_before_motion_command"]
    assert script.find("elif hard_rho > 1.000000000") < script.find(
        "speedl([cmd_vx"
    )
    assert script.count("movel(home_pose") == 2
    assert "force_mode(" not in script
    assert "zero_ftsensor(" not in script


def test_no_contact_trace_summary_requires_full_600_bins_terminal_home_and_static() -> None:
    rows = []
    for index in range(600):
        t = index * 0.1
        sample = figure8_path_provider().sample(t)
        row = {
            "host_monotonic_s": index * 0.01,
            "actual_TCP_pose": list(sample.desired_pose_base),
            "actual_TCP_speed": [0.0] * 6,
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "actual_TCP_force": [0.0, 0.0, -4.5, 0.0, 0.0, 0.0],
            "output_int_register_40": 2,
            "output_int_register_41": 0,
            "output_double_register_40": t,
            "output_double_register_41": sample.desired_pose_base[0],
            "output_double_register_42": sample.desired_pose_base[1],
            "output_double_register_43": 0.0,
            "output_double_register_44": 0.0,
            "output_double_register_45": sample.scalar_speed_m_s,
            "output_double_register_46": 0.0,
            "output_double_register_47": 0.0,
        }
        rows.append(row)
    terminal = dict(rows[-1])
    terminal.update(
        {
            "host_monotonic_s": 3.0,
            "actual_TCP_pose": list(FIGURE8_HOME_POSE),
            "output_int_register_40": 4,
        }
    )
    rows.append(terminal)
    stopped = {
        "is in remote control": "true",
        "safetymode": "Safetymode: NORMAL",
        "robotmode": "Robotmode: RUNNING",
        "running": "Program running: false",
        "programState": "STOPPED step6_figure8_no_contact_canary_v1.urp",
        "get loaded program": "Loaded program: /programs/andyl/kunwei/step6/step6_figure8_no_contact_canary_v1.urp",
    }
    summary = summarize_trace(rows, preflight=stopped, loaded=stopped, postflight=stopped)
    assert summary["passed"] is True
    assert summary["trace"]["path_bin_count"] == 600
    assert summary["trace"]["terminal_state"] == 4
    assert summary["final_home"]["position_error_m"] == pytest.approx(0.0)


def test_live_composition_uses_exact_figure8_reference_profile_and_60s_collector() -> None:
    provider = figure8_path_provider()
    sample = provider.sample(0.0)
    reference = figure8_runtime_path_reference(
        PATH_STAGE_ID, FIGURE8_HOME_POSE[:2], 0.0
    )
    assert reference["desired_xy"] == pytest.approx(FIGURE8_HOME_POSE[:2])
    assert reference["desired_velocity_xy"] == pytest.approx(
        sample.desired_twist_base[:2]
    )
    assert sample.scalar_speed_m_s == pytest.approx(
        FIGURE8_ANALYTIC_PEAK_SPEED_M_S
    )
    profile = figure8_motion_profile()
    receipt = figure8_motion_profile_receipt()
    assert profile.xy_path_speed_m_s == pytest.approx(0.010)
    assert receipt["analytic_peak_speed_m_s"] < receipt["tangential_cap_m_s"]
    assert FigureEightPathEvidenceCollectorV1.REQUIRED_DURATION_S == 60.0
    assert FigureEightPathEvidenceCollectorV1.REQUIRED_BINS == 550
    assert figure8_fresh_frame_wait_policy().as_dict() == load_campaign_config().raw[
        "physical_writer"
    ]["fresh_frame_wait_policy"]
    assert live_composition_receipt()["fresh_frame_wait_policy"]["wait_s"] == 0.004


def test_figure8_mature_envelope_and_r013_raw_metric_keep_separate_bin_contracts() -> None:
    collector = FigureEightPathEvidenceCollectorV1()
    for index in range(600):
        path_time_s = (
            0.0
            if index == 0
            else math.nextafter(index * 0.1, math.inf)
        )
        collector.observe(
            PathSample(
                observed_at_s=path_time_s,
                filtered_normal_n=5.0,
                force_norm_n=5.0,
                torque_norm_nm=0.0,
                sensor_fresh=True,
                state=25,
                safety_normal=True,
                desired_xy_m=(0.0, 0.0),
                actual_xy_m=(0.0, 0.0),
                path_time_s=path_time_s,
                path_phase=6,
                desired_velocity_m_s=(0.0, 0.0),
                actual_velocity_m_s=(0.0, 0.0),
                qdot=(0.0,) * 6,
                actual_qd=(0.0,) * 6,
                source_ages_s={"writer": 0.0, "rtde": 0.0, "kunwei": 0.0, "tp": 0.0},
                source_sequences={
                    "writer": index,
                    "rtde": path_time_s,
                    "kunwei": index,
                    "tp": index,
                },
                qd_lag_s=0.0,
            )
        )

    mature = collector.finalize(
        return_gate_passed=True,
        contact_gate_passed=True,
        home_proof={"stationary": True},
    )
    assert mature.complete_bins == 550
    assert mature.path_samples == 600

    metric = make_metric_result(_complete_samples())
    assert metric.sealed is True
    assert metric.observed_full_bin_count == 600
    assert metric.required_full_bin_count == 600
    assert metric.observed_formal_bin_count == 550
    assert metric.required_formal_bin_count == 550
    with pytest.raises(Exception, match="formal gaps"):
        make_metric_result(_complete_samples()[:-1])


def test_figure8_guard_is_relative_to_moving_reference_and_hard_stops_first() -> None:
    guard = FigureEightPathGuardStackV1()
    reference = figure8_path_provider().sample(5.0)
    nominal = guard.apply(
        reference.desired_twist_base,
        mode="path",
        actual_tcp_pose=reference.desired_pose_base,
        path_time_s=5.0,
        state_age_s=0.0,
    )
    assert nominal.terminal_stop is False
    assert nominal.hard_value == pytest.approx(0.0, abs=1e-12)
    breached_pose = list(reference.desired_pose_base)
    breached_pose[0] += 0.05
    with pytest.raises(R012GuardStop, match="figure8_hard_outer_ellipse_breach"):
        guard.apply(
            reference.desired_twist_base,
            mode="path",
            actual_tcp_pose=breached_pose,
            path_time_s=5.0,
            state_age_s=0.0,
        )


def test_r006_scoped_injection_accepts_typed_figure8_collector_and_preserves_default() -> None:
    default = _R006ScopedRuntimeInjection(
        motion_profile=figure8_motion_profile(),
        path_reference=figure8_runtime_path_reference,
    )
    injected = _R006ScopedRuntimeInjection(
        motion_profile=figure8_motion_profile(),
        path_reference=figure8_runtime_path_reference,
        path_evidence_collector_type=FigureEightPathEvidenceCollectorV1,
    )
    assert default.path_evidence_collector_type is R006PathEvidenceCollector
    assert injected.path_evidence_collector_type is FigureEightPathEvidenceCollectorV1


def test_figure8_home_binding_scopes_profile_reference_and_canary_ik_branch() -> None:
    import step5d_autotune_v4_r004_live_writer as writer_module
    import step5d_autotune_v4_r004.session as session_module

    eoat = load_new_eoat_profile()
    q = (0.7452290058, -1.8211170636, -2.5630011559, -0.3089323801, 1.5276441574, -0.8237493674)
    receipt = R006HomeStartReceiptV1(
        receipt_sha256="1" * 64,
        script_sha256="2" * 64,
        observed_at_s=1.0,
        final_pose=FIGURE8_HOME_POSE,
        final_q=q,
        stationary=True,
        safety_mode="NORMAL",
        eoat_identity_sha256=eoat.profile_sha256,
        home_profile_id=FIGURE8_CALIBRATION_HOME_PROFILE_ID,
    )
    binding = R006HomeBindingV1(
        profile=figure8_home_profile(eoat),
        reference_type=FigureEightHomeReferenceV1,
        entry_receipt=receipt,
    )
    injection = _R006ScopedRuntimeInjection(
        motion_profile=figure8_motion_profile(),
        path_reference=figure8_runtime_path_reference,
        path_evidence_collector_type=FigureEightPathEvidenceCollectorV1,
        home_binding=binding,
    )
    old_loader = writer_module.load_fixed_home_profile
    old_writer_reference = writer_module.HomeReference
    old_session_reference = session_module.HomeReference
    injection.activate()
    try:
        assert writer_module.load_fixed_home_profile().pose == FIGURE8_HOME_POSE
        assert writer_module.HomeReference(FIGURE8_HOME_POSE, q).q == q
        assert session_module.HomeReference is FigureEightHomeReferenceV1
    finally:
        injection.deactivate()
    assert writer_module.load_fixed_home_profile is old_loader
    assert writer_module.HomeReference is old_writer_reference
    assert session_module.HomeReference is old_session_reference


def test_figure8_home_binding_rejects_a_receipt_from_another_home() -> None:
    eoat = load_new_eoat_profile()
    bad_pose = (FIGURE8_HOME_POSE[0] + 0.002, *FIGURE8_HOME_POSE[1:])
    receipt = R006HomeStartReceiptV1(
        receipt_sha256="1" * 64,
        script_sha256="2" * 64,
        observed_at_s=1.0,
        final_pose=bad_pose,
        final_q=(0.0,) * 6,
        stationary=True,
        safety_mode="NORMAL",
        eoat_identity_sha256=eoat.profile_sha256,
        home_profile_id=FIGURE8_CALIBRATION_HOME_PROFILE_ID,
    )
    with pytest.raises(Exception, match="position differs"):
        R006HomeBindingV1(
            profile=figure8_home_profile(eoat),
            reference_type=FigureEightHomeReferenceV1,
            entry_receipt=receipt,
        )


def test_figure8_home_start_receipt_cold_rederives_from_canary(tmp_path: Path) -> None:
    canary = ROOT / "runs" / "step6_figure8_no_contact_canary_v1_20260816_065032"
    if not canary.is_dir():
        pytest.skip("retained live no-contact canary is not present")
    with pytest.raises(ValueError, match="no-contact contract"):
        derive_figure8_home_start_receipt(
            canary_evidence_path=canary / "evidence_receipt_v2.json",
            frame_receipt_path=canary / "frame_receipt_v2.json",
            raw_trace_path=canary / "raw_rtde_trace.jsonl",
            calibration_only=True,
        )


def test_contact_calibration_cold_derives_final_home_from_three_non_bo_runs(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "home-calibration"
    run_dir.mkdir()
    contact_z_m = (0.0200, 0.0202, 0.0199)
    ledger_rows = [{"record_type": "header"}]
    search_rows = []
    baseline_rows = []
    for ordinal, z_m in enumerate(contact_z_m, start=1):
        ledger_rows.append(
            {
                "record_type": "observation",
                "attempt_sequence": ordinal,
                "epoch": 1,
                "kind": "QUALIFICATION",
                "objective": None,
                "force_objective": None,
                "binding_ok": True,
                "contact_gate": True,
                "qualification_passed": True,
                "return_gate": True,
                "safe_return": True,
                "safety_gate": True,
                "sealed": True,
                "timing_gate": True,
                "row_sha256": f"{ordinal}" * 64,
            }
        )
        search_rows.append(
            {
                "attempt_ordinal": ordinal,
                "tcp_pose_m_rad": [
                    *FIGURE8_CALIBRATION_HOME_POSE[:2],
                    z_m,
                    *FIGURE8_CALIBRATION_HOME_POSE[3:],
                ],
            }
        )
        baseline_rows.append(
            {
                "attempt_ordinal": ordinal,
                "tcp_pose_m_rad": [
                    *FIGURE8_CALIBRATION_HOME_POSE[:2],
                    z_m - 0.0002,
                    *FIGURE8_CALIBRATION_HOME_POSE[3:],
                ],
            }
        )

    for name, rows in (
        ("r006-physical-observations.jsonl", ledger_rows),
        ("r008-state20-search-trace.jsonl", search_rows),
        ("r013-state21-baseline-trace.jsonl", baseline_rows),
    ):
        (run_dir / name).write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
    (run_dir / "controller_receipt.json").write_text(
        json.dumps(
            {
                "script_sha256": "a" * 64,
                "txt_sha256": "b" * 64,
                "urp_sha256": "c" * 64,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (run_dir / "home_start_receipt.json").write_text(
        json.dumps(
            {
                "home_profile_id": FIGURE8_CALIBRATION_HOME_PROFILE_ID,
                "home_pose": list(FIGURE8_CALIBRATION_HOME_POSE),
                "calibration_only": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    receipt = derive_figure8_home_calibration_receipt(calibration_run_dir=run_dir)
    expected_z_m = max(contact_z_m) + FIGURE8_FINAL_HOME_OFFSET_M
    assert receipt["contact_acquisition_count"] == 3
    assert receipt["home_profile_id"] == FIGURE8_FINAL_HOME_PROFILE_ID
    assert receipt["final_home_pose"][2] == pytest.approx(expected_z_m)
    receipt_path = run_dir / "home_calibration_receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert load_figure8_home_calibration_receipt(receipt_path) == receipt


def test_continuous_figure8_physical_candidate_accepts_probes_but_rejects_box_escape(
    tmp_path: Path,
) -> None:
    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "physical-candidate-sobol.json")
    )
    probe = scheduler.ask()
    assert probe is not None
    candidate = FigureEightPhysicalCandidateV1.from_canonical(
        probe.candidate["controller_path"]
    )
    assert candidate.normal_filter_tau_s == pytest.approx(0.03094)
    escaped = dict(candidate.canonical)
    escaped["motion_kp"] = 6.01
    with pytest.raises(Exception, match="motion Kp"):
        FigureEightPhysicalCandidateV1.from_canonical(escaped)


def _figure8_force_samples() -> tuple[ForcePathSample, ...]:
    return tuple(
        ForcePathSample(
            path_time_s=round(index * 0.1, 10),
            path_phase=25,
            filtered_normal_n=5.0,
            source_sequences={"rtde": index + 1, "tp": index + 1, "kunwei": index + 1},
            source_ages_s={"rtde": 0.0, "tp": 0.0, "kunwei": 0.0},
            commanded_qdot=(0.0,) * 6,
            actual_qd=(0.0,) * 6,
            timestamp_s=index * 0.1,
        )
        for index in range(600)
    )


def test_figure8_physical_ledger_cold_recomputes_550_bins_and_fails_on_raw_tamper(
    tmp_path: Path,
) -> None:
    candidate_payload = PersistedSobolCursorV1(
        tmp_path / "ledger-sobol.json"
    ).fresh_pool()[0]["controller_path"]
    candidate = FigureEightPhysicalCandidateV1.from_canonical(candidate_payload)
    fingerprint = "2" * 64
    ledger = FigureEightPhysicalLedgerV1(
        tmp_path / "figure8-physical.jsonl",
        campaign_fingerprint=fingerprint,
        eoat_sha256="3" * 64,
    )
    record = ObservationRecord(
        campaign_fingerprint=fingerprint,
        epoch=1,
        attempt_sequence=1,
        kind="BO_TRIAL",
        candidate=candidate,
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=True,
        timing_gate=True,
        identity_gate=True,
        qualification_passed=False,
        duration_s=60.0,
        metrics={"execution_id": "figure8-ledger-test"},
        raw_path_samples=_figure8_force_samples(),
    )
    sealed = ledger.append(record)
    assert sealed.eligible is True
    assert sealed.mae_n == 0.0
    assert sealed.metric_result["observed_formal_bin_count"] == 550
    assert sealed.metric_result["observed_full_bin_count"] == 600
    assert "qualification_passed" not in sealed.payload()
    resumed = FigureEightPhysicalLedgerV1(
        ledger.path,
        campaign_fingerprint=fingerprint,
        eoat_sha256="3" * 64,
    )
    assert resumed.records[0].observation_uid == sealed.observation_uid
    artifact = resumed.path.parent / resumed.records[0].raw_artifact["relative_path"]
    artifact.write_bytes(artifact.read_bytes() + b" ")
    with pytest.raises(FigureEightPhysicalLedgerError, match="artifact bytes differ"):
        resumed.fresh_process_verify()


def test_figure8_physical_ledger_rejects_qualification_rows(tmp_path: Path) -> None:
    candidate_payload = PersistedSobolCursorV1(
        tmp_path / "qualification-sobol.json"
    ).fresh_pool()[0]["controller_path"]
    candidate = FigureEightPhysicalCandidateV1.from_canonical(candidate_payload)
    fingerprint = "6" * 64
    ledger = FigureEightPhysicalLedgerV1(
        tmp_path / "figure8-qualification.jsonl",
        campaign_fingerprint=fingerprint,
        eoat_sha256="7" * 64,
    )
    record = ObservationRecord(
        campaign_fingerprint=fingerprint,
        epoch=1,
        attempt_sequence=1,
        kind="QUALIFICATION",
        candidate=candidate,
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=False,
        timing_gate=True,
        identity_gate=True,
        qualification_passed=True,
        duration_s=60.0,
        metrics={"execution_id": "figure8-qualification-test"},
        sealed=False,
    )

    with pytest.raises(FigureEightPhysicalLedgerError, match="does not accept qualification rows"):
        ledger.append(record)
    assert not (tmp_path / "figure8_raw_path_artifacts").exists()


def test_incomplete_figure8_curve_is_preserved_but_never_sealed_or_eligible(
    tmp_path: Path,
) -> None:
    candidate_payload = PersistedSobolCursorV1(
        tmp_path / "incomplete-ledger-sobol.json"
    ).fresh_pool()[0]["controller_path"]
    candidate = FigureEightPhysicalCandidateV1.from_canonical(candidate_payload)
    fingerprint = "4" * 64
    ledger = FigureEightPhysicalLedgerV1(
        tmp_path / "incomplete-figure8-physical.jsonl",
        campaign_fingerprint=fingerprint,
        eoat_sha256="5" * 64,
    )
    record = ObservationRecord(
        campaign_fingerprint=fingerprint,
        epoch=1,
        attempt_sequence=1,
        kind="BO_TRIAL",
        candidate=candidate,
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=True,
        timing_gate=True,
        identity_gate=True,
        qualification_passed=False,
        duration_s=59.9,
        metrics={"execution_id": "figure8-incomplete-ledger-test"},
        raw_path_samples=_figure8_force_samples()[:-1],
    )
    preserved = ledger.append(record)
    assert preserved.raw_artifact["sample_count"] == 599
    assert preserved.raw_artifact["metric_status"] == "incomplete_raw_preserved"
    assert preserved.metric_result is None
    assert preserved.sealed is False
    assert preserved.eligible is False
    assert ledger.fresh_process_verify()["record_count"] == 1


def test_runtime_context_correction_is_signed_slew_limited_and_candidate_bound(
    monkeypatch, tmp_path: Path
) -> None:
    import step5d_autotune_v4_r013.live_runtime as live_runtime
    from step5d_paper_outer_loop import Step5dOuterLoopState

    controller = PersistedSobolCursorV1(
        tmp_path / "runtime-sobol.json"
    ).fresh_pool()[7]["controller_path"]
    complete = CompleteCandidateV1(
        controller_path=controller,
        correction_weights=(-0.5, 0.0, 0.0, 0.0, 0.0, 0.0),
    )
    fingerprint = "1" * 64
    policy = CorrectionPolicyV1(
        normalization_scales=(1.0, 0.01, 0.002, 100.0, 1.0, 1.0),
        weights=complete.correction_weights,
    )
    monkeypatch.setattr(live_runtime, "_ACTIVE_PATH_PROVIDER", figure8_path_provider())
    monkeypatch.setattr(live_runtime, "_ACTIVE_CORRECTION_POLICY", policy)
    monkeypatch.setattr(live_runtime, "_ACTIVE_CORRECTION_FINGERPRINT", fingerprint)
    monkeypatch.setattr(live_runtime, "_ACTIVE_COMPLETE_CANDIDATE", complete)

    class FakeBase:
        def __init__(self, candidate):
            self.candidate = candidate
            self._outer_state = Step5dOuterLoopState()
            self.targets = []

        def desired_twist(self, *, mode, **kwargs):
            self.targets.append(float(kwargs["internal_setpoint_n"]))
            return (0.0,) * 6

    runtime_type = live_runtime._make_runtime_class(FakeBase)
    runtime = runtime_type(SimpleNamespace(**controller))
    common = {
        "internal_setpoint_n": 5.0,
        "actual_tcp_pose": FIGURE8_HOME_POSE,
        "actual_tcp_speed": (0.0,) * 6,
        "force_tcp_n": (0.0,) * 3,
        "filtered_normal_n": 5.0,
        "actual_dt_s": 0.01,
    }
    for target in (1.0, 2.0, 3.0, 4.0, 5.0):
        runtime.desired_twist(
            mode="baseline",
            path_time_s=0.0,
            **{**common, "internal_setpoint_n": target},
        )
    runtime.desired_twist(mode="path", path_time_s=0.0, **common)
    runtime.desired_twist(mode="path", path_time_s=0.1, **common)
    assert runtime.internal_setpoint_bounds_n == (3.75, 6.25)
    assert runtime.targets == pytest.approx([1.0, 2.0, 3.0, 4.0, 5.0, 5.0, 5.05])
    assert runtime._r013_last_context_correction_n == pytest.approx(-0.05)
    assert runtime._r013_last_correction_receipt["path_context"]["path_id"] == "r013_figure8_v1"


def test_correction_has_six_normalized_features_and_zero_default() -> None:
    _config, fingerprint = _fingerprint()
    policy = CorrectionPolicyV1.zero((1.0, 0.01, 0.002, 100.0, 1.0, 1.0))
    context = FigureEightPathProviderV1().sample(0.1)
    state = CorrectionStateV1(fingerprint.sha256)
    next_state, receipt = policy.apply(state, context, fingerprint_sha256=fingerprint.sha256)
    assert len(receipt.raw_features) == len(receipt.normalized_features) == 6
    assert receipt.applied_n == 0.0
    assert receipt.effective_target_n == 5.0
    assert next_state.last_path_time_s == 0.1


def test_fingerprint_binds_step6_identity_and_repeat_noise() -> None:
    _config, fingerprint = _fingerprint()
    payload = fingerprint.as_dict()
    assert payload["handoff_policy"] == "handoff_pending"
    assert set(payload["controller_triplet_sha256"]) == {"script", "txt", "urp"}
    assert payload["home_pose"][2] == 0.0345
    noise = repeat_aware_yvar(
        [
            {"fingerprint_sha256": fingerprint.sha256, "candidate_key": "a", "mae_n": 0.4},
            {"fingerprint_sha256": fingerprint.sha256, "candidate_key": "a", "mae_n": 0.6},
            {"fingerprint_sha256": fingerprint.sha256, "candidate_key": "b", "mae_n": 0.5},
        ]
    )
    grouped = {row["candidate_key"]: row for row in noise}
    assert grouped["a"]["n"] == 2
    assert grouped["a"]["shrinkage_nu0"] == 2
    assert 1e-4 <= grouped["a"]["yvar_n2"] <= 2e-2
    assert grouped["b"]["n"] == 1


def test_trainable_fingerprint_requires_and_binds_contact_derived_home() -> None:
    config, pending = _fingerprint()
    final_pose = list(FIGURE8_CALIBRATION_HOME_POSE)
    final_pose[2] = 0.033725311
    home_materialization = {
        "schema": "step6.autotune/figure8-home-calibration-receipt-v1",
        "passed": True,
        "home_profile_id": FIGURE8_FINAL_HOME_PROFILE_ID,
        "final_home_rule": "max(contact_confirm_z_m)+0.013525311 m",
        "final_home_pose": final_pose,
        "receipt_sha256": CENSOR_HOME_CALIBRATION_SHA256,
    }
    pending_final_home = build_campaign_fingerprint(
        config=config,
        source_sha256=config.raw["controller"]["source_parent_sha256"],
        controller_triplet_sha256=pending.controller_triplet_sha256,
        home_materialization=home_materialization,
    )
    assert pending_final_home.home_pose == tuple(final_pose)
    assert pending_final_home.home_calibration_receipt_sha256 == CENSOR_HOME_CALIBRATION_SHA256
    assert pending_final_home.path_identity_sha256 != pending.path_identity_sha256
    frozen = build_frozen_campaign_fingerprint(
        config=config,
        source_sha256=config.raw["controller"]["source_parent_sha256"],
        controller_triplet_sha256=pending.controller_triplet_sha256,
        home_materialization=home_materialization,
        matched_ab={
            "winner": True,
            "winner_handoff_policy": "freeze_carry_v1",
            "winner_n": 5,
            "same_function_fingerprint": True,
            "arm_fingerprints": {"A": "a" * 64, "B": "b" * 64},
            "winner_fingerprint_sha256": "b" * 64,
        },
    )
    assert frozen.as_dict()["trainable"] is True
    assert frozen.home_pose == tuple(final_pose)


def test_sobol_resume_returns_two_fresh_128_pools_including_beyond_160(tmp_path: Path) -> None:
    path = tmp_path / "sobol.json"
    first = PersistedSobolCursorV1(path)
    pool_a = first.fresh_pool()
    resumed = PersistedSobolCursorV1(path)
    pool_b = resumed.fresh_pool()
    keys_a = {json.dumps(row, sort_keys=True) for row in pool_a}
    keys_b = {json.dumps(row, sort_keys=True) for row in pool_b}
    assert len(pool_a) == len(pool_b) == 128
    assert not keys_a & keys_b
    assert resumed.cursor_index >= 256


def test_scheduler_freezes_phases_and_probe_repeats_do_not_consume_novel(tmp_path: Path) -> None:
    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "sobol.json"),
        campaign_fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    assert scheduler.phase_for_novel(1) == ("novel_warm_start", "designed_probe", "controller_path")
    assert scheduler.phase_for_novel(25)[1] == "global_sobol"
    assert scheduler.phase_for_novel(26)[1] == "qlognei"
    assert scheduler.phase_for_novel(30)[1] == "global_sobol"
    assert scheduler.phase_for_novel(53)[0] == "controller_path_bo"
    assert scheduler.phase_for_novel(101)[0] == "correction_sobol"
    assert scheduler.phase_for_novel(113)[0] == "correction_bo"
    assert scheduler.phase_for_novel(161)[2] == "controller_path"
    plan = scheduler.ask()
    assert plan is not None
    scheduler.queue_probe_repeat(plan, repeat_index=2)
    scheduler.complete(plan, accepted=True, mae_n=1.0)
    repeat = scheduler.ask()
    assert repeat is not None and repeat.kind == "repeat"
    assert scheduler.novel_count == 1


def test_rejected_incomplete_observation_never_calls_tell_exact(tmp_path: Path) -> None:
    _config, fingerprint = _fingerprint()
    calls: list[tuple[dict, float, float]] = []

    class Optimizer:
        def tell_exact(self, candidate, mae_n, yvar_n2):
            calls.append((candidate, mae_n, yvar_n2))

    campaign = OfflineCampaignV1(
        StrictAdmissionV1(fingerprint, load_campaign_config().metric),
        tmp_path / "receipts.jsonl",
        optimizer=Optimizer(),
    )
    trial = TrialEvidenceV1("e1", "t1", fingerprint.sha256, True, True, None, ( {"time_s": 5.0}, ))
    receipt = campaign.tell_exact(candidate={"x": 1}, trial=trial)
    assert receipt.accepted is False
    assert calls == []
    assert "exact_550_bin_seal_failed" in receipt.reasons
    assert json.loads((tmp_path / "receipts.jsonl").read_text().splitlines()[0])["status"] == "rejected_incomplete_or_ineligible"


def test_timing_motion_and_fingerprint_failures_never_call_tell_exact(tmp_path: Path) -> None:
    _config, fingerprint = _fingerprint()
    calls: list[tuple[dict, float, float]] = []

    class Optimizer:
        def tell_exact(self, candidate, mae_n, yvar_n2):
            calls.append((candidate, mae_n, yvar_n2))

    candidate = PersistedSobolCursorV1(tmp_path / "sobol.json").fresh_pool()[0]
    campaign = OfflineCampaignV1(
        StrictAdmissionV1(fingerprint, load_campaign_config().metric),
        tmp_path / "receipts.jsonl",
        optimizer=Optimizer(),
    )
    for index, (motion, timing, trial_fp) in enumerate(
        ((False, True, fingerprint.sha256), (True, False, fingerprint.sha256), (True, True, "0" * 64))
    ):
        receipt = campaign.tell_exact(
            candidate=candidate,
            trial=TrialEvidenceV1(
                "e1", f"t{index}", trial_fp, motion, timing,
                make_metric_result(_complete_samples()), _complete_samples(),
            ),
        )
        assert receipt.accepted is False
    assert calls == []


def test_fake_writer_whole_flow_is_home_arm_path_seal_admit_home(tmp_path: Path) -> None:
    config, fingerprint = _fingerprint()
    scheduler = FigureEightSchedulerV1(PersistedSobolCursorV1(tmp_path / "sobol.json"))
    plan = scheduler.ask()
    assert plan is not None
    calls: list[tuple[dict, float, float]] = []

    class Optimizer:
        def tell_exact(self, candidate, mae_n, yvar_n2):
            calls.append((candidate, mae_n, yvar_n2))

    campaign = OfflineCampaignV1(StrictAdmissionV1(fingerprint, config.metric), tmp_path / "receipts.jsonl", Optimizer())
    writer = FakeWriter()
    receipt = execute_candidate_transaction(
        writer=writer,
        campaign=campaign,
        scheduler=scheduler,
        fingerprint=fingerprint,
        candidate=plan.candidate,
        trial_id="t1",
        epoch_id="e1",
    )
    assert receipt.strict_passed
    assert writer.events == ["Home", "ARM/apply", "60 s PATH", "seal", "Home"]
    assert len(calls) == 0


def test_dry_run_forwards_explicit_offline_inputs_without_live_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    readback_dir = tmp_path / "fresh-readback"
    canary_dir = tmp_path / "fresh-canary"
    home_receipt = tmp_path / "home-calibration-receipt.json"
    readback_dir.mkdir()
    canary_dir.mkdir()
    home_receipt.write_text("fresh-home-receipt", encoding="utf-8")
    home_sha = "a" * 64
    home_pose = tuple(
        float(value) for value in step6_entry.numeric_sanity()["path"]["anchor_pose"]
    )
    home_materialization = {
        "schema": "step6.autotune/figure8-home-calibration-receipt-v1",
        "passed": True,
        "home_profile_id": FIGURE8_FINAL_HOME_PROFILE_ID,
        "final_home_rule": "max(contact_confirm_z_m)+0.013525311 m",
        "final_home_pose": list(home_pose),
        "receipt_sha256": home_sha,
    }
    captured: dict[str, Path | None] = {}

    def fake_validate(
        config_path: Path,
        *,
        controller_readback_dir: Path,
        canary_dir: Path,
        home_calibration_receipt: Path | None,
    ) -> dict[str, object]:
        captured.update(
            {
                "config_path": config_path,
                "controller_readback_dir": controller_readback_dir,
                "canary_dir": canary_dir,
                "home_calibration_receipt": home_calibration_receipt,
            }
        )
        return {
            "controller_readback_manifest": str(controller_readback_dir / "manifest.json"),
            "no_contact_canary_dir": str(canary_dir),
            "home_calibration_receipt_sha256": home_sha,
        }

    monkeypatch.setattr(step6_entry, "validate_offline_surfaces", fake_validate)
    monkeypatch.setattr(
        step6_entry,
        "_load_home_materialization",
        lambda path: (home_materialization, home_pose, home_sha),
    )

    assert step6_entry.main(
        [
            "--dry-run",
            "--state-dir",
            str(tmp_path / "state"),
            "--controller-readback-dir",
            str(readback_dir),
            "--canary-dir",
            str(canary_dir),
            "--home-calibration-receipt",
            str(home_receipt),
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert captured == {
        "config_path": step6_entry.DEFAULT_CONFIG,
        "controller_readback_dir": readback_dir,
        "canary_dir": canary_dir,
        "home_calibration_receipt": home_receipt,
    }
    assert result["surfaces"]["controller_readback_manifest"] == str(
        readback_dir / "manifest.json"
    )
    assert result["surfaces"]["no_contact_canary_dir"] == str(canary_dir)
    assert result["surfaces"]["home_calibration_receipt_sha256"] == home_sha
    assert result["transaction_events"] == [
        "Home",
        "ARM/apply",
        "60 s PATH",
        "seal",
        "Home",
    ]
    assert result["live_executed"] is False


def _qlognei_delegate(calls: list[int]):
    def delegate(pool, **_kwargs):
        calls.append(len(pool))
        return {
            "candidate": pool[7],
            "acquisition": "qLogNEI",
            "production_provider": "test-production-provider",
            "fit_receipt": {"schema": "test-fit"},
        }
    return delegate


def test_qlognei_receipt_invokes_production_provider_on_exactly_128_fresh_candidates(tmp_path: Path) -> None:
    calls: list[int] = []
    from step6_figure8_autotune_v1 import R013ProductionProposalProviderV1

    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "sobol.json"),
        proposal_provider=R013ProductionProposalProviderV1(_qlognei_delegate(calls)),
    )
    scheduler.novel_count = 25
    plan = scheduler.ask()
    assert plan is not None
    assert plan.proposal_receipt["acquisition"] == "qlognei"
    assert calls == [128]
    assert plan.proposal_receipt["fresh_candidate_count"] == 128
    CompleteCandidateV1.from_mapping(plan.candidate)


def test_safe_return_prefetch_scores_pending_candidate_but_cannot_arm(
    tmp_path: Path,
) -> None:
    calls: list[int] = []
    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "async-sobol.json"),
        proposal_provider=R013ProductionProposalProviderV1(_qlognei_delegate(calls)),
        campaign_fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    scheduler.novel_count = 25
    current = scheduler.ask()
    assert current is not None and current.novel_ordinal == 26
    prefetched = scheduler.prefetch_next_for_safe_return()
    assert prefetched is not None and prefetched.novel_ordinal == 27
    assert prefetched.proposal_receipt["async_prefetched"] is True
    assert prefetched.proposal_receipt["overlap_phase"] == "safe_return"
    assert prefetched.proposal_receipt["pending_candidate_keys"] == [
        current.candidate_key
    ]
    with pytest.raises(Exception, match="already in flight"):
        scheduler.ask()
    scheduler.complete(
        current,
        accepted=True,
        mae_n=0.5,
        fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    next_plan = scheduler.ask()
    assert next_plan == prefetched
    assert calls == [128, 128]


def test_safe_return_overlap_callback_starts_no_compute_before_trigger(
    tmp_path: Path,
) -> None:
    calls: list[int] = []
    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "overlap-sobol.json"),
        proposal_provider=R013ProductionProposalProviderV1(_qlognei_delegate(calls)),
        campaign_fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    scheduler.novel_count = 25
    plan = scheduler.ask()
    callbacks: list[object] = []

    def configure(callback):
        callbacks.append(callback)

    context = SimpleNamespace(
        configure_safe_return_overlap=configure,
        safe_return_overlap_errors=[],
    )
    overlap = SafeReturnProposalOverlapV1(
        scheduler=scheduler,
        artifact_dir=tmp_path / "overlap-receipts",
    )
    try:
        overlap.arm(context=context, plan=plan)
        assert calls == [128]
        assert callbacks[-1] is not None
        callbacks[-1]()
        receipt = overlap.finish_after_home(context=context)
    finally:
        overlap.close()
    assert receipt["passed"] is True
    assert receipt["proposal_prefetched"] is True
    assert receipt["arm_authorized"] is False
    assert receipt["next_arm_requires_current_home_and_admission"] is True
    assert Path(receipt["path"]).is_file()
    assert calls == [128, 128]


def test_qlognei_without_production_path_fails_closed(tmp_path: Path) -> None:
    scheduler = FigureEightSchedulerV1(PersistedSobolCursorV1(tmp_path / "sobol.json"))
    scheduler.novel_count = 25
    with pytest.raises(ProductionProposalUnavailable):
        scheduler.ask()


def test_censored_novel_is_non_gp_and_refills_a_fresh_128_pool(tmp_path: Path) -> None:
    config, fingerprint = _fingerprint()
    calls: list[int] = []
    from step6_figure8_autotune_v1 import R013ProductionProposalProviderV1

    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "censor-sobol.json"),
        proposal_provider=R013ProductionProposalProviderV1(_qlognei_delegate(calls)),
        campaign_fingerprint_sha256=fingerprint.sha256,
    )
    scheduler.novel_count = 25
    plan = scheduler.ask()
    assert plan is not None and plan.kind == "novel"
    receipt = make_figure8_censored_receipt(
        candidate=plan.candidate,
        campaign_fingerprint_sha256=fingerprint.sha256,
        campaign_id="figure8",
        run_id="run-1",
        attempt_id="attempt-1",
        trial_id="trial-1",
        run_kind="BO_TRIAL",
        closed_absolute_errors=(1.1,) * 25,
        raw_samples=tuple({"time_s": 5.0 + index * 0.1} for index in range(25)),
        watermark_s=7.5,
        confirmed_incumbent_mean_n=0.5,
        request_sequence=9,
        ack_sequence=9,
        home_profile_id=FIGURE8_FINAL_HOME_PROFILE_ID,
        home_calibration_receipt_sha256=CENSOR_HOME_CALIBRATION_SHA256,
        home_observation=CENSOR_HOME_OBSERVATION,
    )
    assert isinstance(receipt, FigureEightCensoredReceiptV1)
    assert receipt.causal_lower_bound_n == pytest.approx(27.5 / 550.0)
    campaign = OfflineCampaignV1(
        StrictAdmissionV1(fingerprint, config.metric),
        tmp_path / "censor-receipts.jsonl",
        optimizer=SimpleNamespace(
            tell_exact=lambda *_args: pytest.fail("censored row reached tell_exact")
        ),
    )
    campaign.record_censored(receipt)
    scheduler.complete_censored(plan, receipt)
    assert scheduler.exact_novel_count == 25
    assert receipt.candidate_key in scheduler.evaluated_candidate_keys
    next_plan = scheduler.ask()
    assert next_plan is not None and next_plan.novel_ordinal == 26
    assert next_plan.candidate_key != receipt.candidate_key
    assert next_plan.proposal_receipt["fresh_candidate_count"] == 128
    assert calls == [128, 128]


def test_censor_guard_uses_25_bins_kappa_two_and_excludes_boundary_probes() -> None:
    before_guard = evaluate_figure8_censor_prefix(
        (1.1,) * 24,
        run_kind="BO_TRIAL",
        confirmed_incumbent_mean_n=0.5,
    )
    triggered = evaluate_figure8_censor_prefix(
        (1.1,) * 25,
        run_kind="BO_TRIAL",
        confirmed_incumbent_mean_n=0.5,
    )
    probe = evaluate_figure8_censor_prefix(
        (10.0,) * 25,
        run_kind="BOUNDARY_PROBE",
        confirmed_incumbent_mean_n=0.5,
    )
    assert before_guard.triggered is False
    assert before_guard.reason == "guard_not_reached"
    assert triggered.triggered is True
    assert triggered.incumbent_threshold_n == pytest.approx(1.0)
    assert triggered.causal_lower_bound_n == pytest.approx(0.05)
    assert probe.active is False and probe.triggered is False
    assert probe.reason == "run_kind_excluded"


def test_figure8_physical_censor_seam_flow_and_closure_negatives(tmp_path: Path) -> None:
    class Registers:
        def __init__(self, *, matched_ack: bool = True) -> None:
            self.inputs: dict[int, int] = {}
            self.outputs = {28: 0, 35: 0, 36: 0}
            self.matched_ack = matched_ack

        def write_input_integer_register(self, index: int, value: int) -> None:
            self.inputs[index] = value
            if index == 35 and value > 0:
                self.outputs[36] = value if self.matched_ack else value + 1
                self.outputs[28] = 0
                self.outputs[35] = 0

        def read_output_integer_register(self, index: int) -> int:
            return int(self.outputs.get(index, 0))

    class SinkOwner:
        def __init__(self) -> None:
            self.samples: list[dict[str, float | int]] = []
            self._path_sample_sink = self.samples.append

    _config, fingerprint = _fingerprint()
    candidate = PersistedSobolCursorV1(tmp_path / "flow-sobol.json").fresh_pool()[0]

    def run_case(
        *,
        run_kind: str = "BO_TRIAL",
        incumbent: float | None = 0.5,
        matched_ack: bool = True,
        home: bool = True,
    ) -> tuple[FigureEightPhysicalCensorSeamV1, SinkOwner, Registers]:
        owner = SinkOwner()
        registers = Registers(matched_ack=matched_ack)
        seam = FigureEightPhysicalCensorSeamV1(
            sink_owner=owner,
            register_writer=registers,
            closure_provider=lambda: {
                "return_guard_value": 123,
                "return_guard_closed": True,
                "home_closed": home,
                    "safe_return_closed": home,
                    "home_calibrated": home,
                    "home_profile_id": FIGURE8_FINAL_HOME_PROFILE_ID,
                    "home_calibration_receipt_sha256": CENSOR_HOME_CALIBRATION_SHA256,
                    "home_observation": {
                        **CENSOR_HOME_OBSERVATION,
                        "pose_closed": home,
                    },
            },
        )
        seam.arm(
            candidate=candidate,
            campaign_fingerprint_sha256=fingerprint.sha256,
            campaign_id="figure8",
            run_id="run-1",
            attempt_id="attempt-7",
            trial_id="trial-7",
            run_kind=run_kind,
            confirmed_incumbent_mean_n=incumbent,
            attempt_sequence=7,
        )
        for index in range(26):
            seam.original_sink  # the existing sink was captured before wrapping
            owner._path_sample_sink({
                "state": 25,
                "path_time_s": 5.0 + index * 0.1,
                "filtered_normal_n": 8.0,
            })
        return seam, owner, registers

    seam, owner, registers = run_case()
    assert registers.inputs[35] == 7
    assert owner.samples[-1]["filtered_normal_n"] == 8.0
    receipt = seam.seal_short_exception(RuntimeError("R013 exact trial duration is short: 7.5"))
    assert isinstance(receipt, FigureEightCensoredReceiptV1)
    assert receipt.closed_bin_count == 25
    assert receipt.causal_lower_bound_n == pytest.approx(75.0 / 550.0)
    assert receipt.request_sequence == receipt.ack_sequence == 7
    assert receipt.home_calibrated is True
    seam.close()
    assert owner._path_sample_sink is not None

    calls: list[int] = []
    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "flow-refill.json"),
        proposal_provider=R013ProductionProposalProviderV1(_qlognei_delegate(calls)),
        campaign_fingerprint_sha256=fingerprint.sha256,
        fixed_sentinel_candidate=candidate,
    )
    scheduler.novel_count = 25
    scheduler.novel_dispatch_count = 9
    plan = scheduler.ask()
    assert plan is not None and plan.novel_ordinal == 26
    campaign = OfflineCampaignV1(
        StrictAdmissionV1(fingerprint, _config.metric),
        tmp_path / "flow-receipts.jsonl",
        optimizer=SimpleNamespace(tell_exact=lambda *_args: pytest.fail("censored row reached tell_exact")),
    )
    receipt_for_plan = make_figure8_censored_receipt(
        candidate=plan.candidate,
        campaign_fingerprint_sha256=fingerprint.sha256,
        campaign_id=receipt.campaign_id,
        run_id=receipt.run_id,
        attempt_id=receipt.attempt_id,
        trial_id=receipt.trial_id,
        run_kind="BO_TRIAL",
        closed_absolute_errors=(1.1,) * 25,
        raw_samples=tuple({"time_s": 5.0 + index * 0.1} for index in range(25)),
        watermark_s=7.5,
        confirmed_incumbent_mean_n=0.5,
        request_sequence=7,
        ack_sequence=7,
        home_profile_id=FIGURE8_FINAL_HOME_PROFILE_ID,
        home_calibration_receipt_sha256=CENSOR_HOME_CALIBRATION_SHA256,
        home_observation=CENSOR_HOME_OBSERVATION,
    )
    campaign.record_censored(receipt_for_plan)
    scheduler.complete_censored(plan, receipt_for_plan)
    assert scheduler.exact_novel_count == 25
    assert scheduler.novel_dispatch_count == 10
    scheduler.schedule_sentinel_repeats({})
    assert scheduler.sentinels and scheduler.sentinels[-1]["novel_dispatch_count"] == 10
    sentinel = scheduler.ask()
    assert sentinel is not None and sentinel.kind == "sentinel"
    scheduler.complete(sentinel, accepted=True, mae_n=0.5, fingerprint_sha256=fingerprint.sha256)
    refill = scheduler.ask()
    assert refill is not None and refill.novel_ordinal == 26
    assert refill.candidate_key != receipt_for_plan.candidate_key
    assert refill.proposal_receipt["fresh_candidate_count"] == 128
    assert calls == [128, 128]

    for kwargs in (
        {"run_kind": "SENTINEL"},
        {"incumbent": None},
        {"matched_ack": False},
        {"home": False},
    ):
        negative, _owner, _registers = run_case(**kwargs)
        with pytest.raises(FigureEightCensorRuntimeError):
            negative.seal_short_exception(RuntimeError("R013 exact trial duration is short: 7.5"))


def test_censor_attempt_sequence_is_scoped_to_the_current_resident_segment(
    tmp_path: Path,
) -> None:
    ledger = SimpleNamespace(
        records=tuple(SimpleNamespace(attempt_sequence=index) for index in (1, 2, 3))
    )
    receipts = tmp_path / "censored.jsonl"
    receipts.write_text(
        "\n".join(
            json.dumps(
                {
                    "censored_receipt": {
                        "run_id": run_id,
                        "attempt_sequence": sequence,
                    }
                },
                sort_keys=True,
            )
            for run_id, sequence in (
                ("campaign-segment-001", 91),
                ("campaign-segment-002", 4),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    assert next_cold_attempt_sequence(
        ledger,
        receipts,
        run_id="campaign-segment-002",
    ) == 5
    assert next_cold_attempt_sequence(
        ledger,
        receipts,
        run_id="campaign-segment-003",
    ) == 4


def test_operational_segment_budget_is_injectable_and_not_completion() -> None:
    now = [100.0]
    budget = OperationalSegmentBudgetV1(
        max_attempts=2,
        max_duration_s=10.0,
        now=lambda: now[0],
    )
    budget.start()
    assert budget.due() is False
    budget.record_attempt()
    now[0] += 10.0
    assert budget.due() is True
    assert budget.snapshot()["attempts"] == 1


def test_boundary_probes_are_single_axis_and_n3_only_qualifying_bound_opens(tmp_path: Path) -> None:
    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "sobol.json"),
        campaign_fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    probes = []
    for ordinal in range(3):
        plan = scheduler.ask()
        assert plan is not None and plan.probe_axis is not None
        probes.append(plan)
        scheduler.complete(plan, accepted=True, mae_n=0.4 if ordinal == 0 else 0.5)
        for _ in range(2):
            repeat = scheduler.ask()
            assert repeat is not None and repeat.kind == "repeat"
            scheduler.complete(repeat, accepted=True, mae_n=0.4 if ordinal == 0 else 0.5)
    assert all(len(scheduler.probe_results[plan.probe_axis]) == 3 for plan in probes)
    assert scheduler.open_probe_bound("normal_filter_tau_s", handoff_anchor_mean_n=0.5, strict_gates=True)
    assert not scheduler.open_probe_bound("orientation_ko", handoff_anchor_mean_n=0.5, strict_gates=True)
    assert not scheduler.open_probe_bound("force_i_gain", handoff_anchor_mean_n=0.5, strict_gates=True)
    assert probes[0].candidate["controller_path"]["normal_filter_tau_s"] == pytest.approx(0.03094)
    assert probes[1].candidate["controller_path"]["orientation_ko"] == pytest.approx(0.03536)
    anchor = dict(probes[0].candidate["controller_path"])
    anchor["normal_filter_tau_s"] = 0.04375
    anchor["orientation_ko"] = 0.05
    anchor["force_i_gain"] = anchor["force_p_gain"] * 0.5
    for plan in probes[1:]:
        current = plan.candidate["controller_path"]
        changed = [key for key in anchor if key != "target_force_n" and anchor[key] != current[key]]
        assert len(changed) == 1


def test_rejected_attempt_does_not_consume_novel_budget_and_three_same_failures_pause(
    tmp_path: Path,
) -> None:
    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "rejection-sobol.json"),
        campaign_fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    for expected_count in (1, 2, 3):
        plan = scheduler.ask()
        assert plan is not None
        assert plan.novel_ordinal == 1
        scheduler.complete(
            plan,
            accepted=False,
            failure_signature="same-physical-signature",
        )
        assert scheduler.novel_count == 0
        assert scheduler.novel_dispatch_count == 0
        assert scheduler.consecutive_failure_count == expected_count
    assert scheduler.failure_pause_required is True


def test_fixed_sentinel_and_top_repeat_never_increment_novel_budget(tmp_path: Path) -> None:
    anchor = PersistedSobolCursorV1(tmp_path / "anchor-sobol.json").fresh_pool()[0]
    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "sentinel-sobol.json"),
        campaign_fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
        fixed_sentinel_candidate=anchor,
    )
    scheduler.novel_count = 3
    plan = scheduler.ask()
    assert plan is not None
    scheduler.complete(
        plan,
        accepted=True,
        mae_n=0.4,
        fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    novel_before = scheduler.novel_count
    fixed_key = scheduler.queue_fixed_sentinel()
    sentinel = scheduler.ask()
    assert sentinel is not None and sentinel.kind == "sentinel"
    assert sentinel.repeat_of == fixed_key
    scheduler.complete(
        sentinel,
        accepted=True,
        mae_n=0.41,
        fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    assert scheduler.novel_count == novel_before


def test_controller_phase_freeze_uses_repeat_qualified_winner(tmp_path: Path) -> None:
    cursor = PersistedSobolCursorV1(tmp_path / "phase-freeze-sobol.json")
    candidate = CompleteCandidateV1.from_mapping(
        cursor.fresh_pool(domain="controller_path")[0]
    )
    scheduler = FigureEightSchedulerV1(
        cursor,
        campaign_fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    scheduler.novel_count = 100
    scheduler.accepted_observations = [
        {
            "candidate": candidate.as_dict(),
            "candidate_key": candidate.candidate_key,
            "mae_n": 0.4,
            "fingerprint": TEST_FIGURE8_FINGERPRINT,
            "fingerprint_sha256": TEST_FIGURE8_FINGERPRINT,
            "n": 1,
            "novel_ordinal": 100,
            "kind": "novel",
        }
    ]
    scheduler._refresh_observation_groups()
    for expected_n in (2, 3):
        assert scheduler.schedule_controller_freeze_repeats() == candidate.candidate_key
        repeat = scheduler.ask()
        assert repeat is not None and repeat.kind == "phase_boundary_repeat"
        scheduler.complete(
            repeat,
            accepted=True,
            mae_n=0.4,
            fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
        )
        assert scheduler.observation_groups[candidate.candidate_key]["n"] == expected_n
        assert scheduler.novel_count == 100


def test_every_frozen_phase_emits_a_complete_candidate(tmp_path: Path) -> None:
    calls: list[int] = []
    from step6_figure8_autotune_v1 import R013ProductionProposalProviderV1

    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(tmp_path / "sobol.json"),
        proposal_provider=R013ProductionProposalProviderV1(_qlognei_delegate(calls)),
    )
    seed_complete = None
    for ordinal in (1, 24, 25, 26, 101, 113, 161, 162, 200):
        scheduler.novel_count = ordinal - 1
        scheduler.in_flight = None
        if ordinal >= 101:
            assert seed_complete is not None
            scheduler.frozen_controller_path = dict(seed_complete.controller_path)
        if ordinal >= 161:
            scheduler.polish_incumbent = seed_complete.as_dict()
        plan = scheduler.ask()
        assert plan is not None
        candidate = CompleteCandidateV1.from_mapping(plan.candidate)
        seed_complete = seed_complete or candidate
        assert len(candidate.correction_weights) == 6
        assert set(candidate.controller_path) >= {
            "force_p_gain", "force_damping", "force_i_gain", "normal_filter_tau_s",
            "orientation_ko", "motion_kp", "i_off",
        }
        scheduler.complete(plan, accepted=False, failure_signature="test")


def test_cold_resume_preserves_state_cursor_and_no_duplicate_at_boundaries(tmp_path: Path) -> None:
    calls: list[int] = []
    from step6_figure8_autotune_v1 import R013ProductionProposalProviderV1

    state_path = tmp_path / "campaign.json"
    cursor_path = tmp_path / "sobol.json"
    provider = R013ProductionProposalProviderV1(_qlognei_delegate(calls))
    scheduler = FigureEightSchedulerV1(
        PersistedSobolCursorV1(cursor_path),
        state_path=state_path,
        proposal_provider=provider,
        campaign_fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    while scheduler.novel_count < 24:
        plan = scheduler.ask()
        assert plan is not None
        scheduler.complete(plan, accepted=True, mae_n=0.5)
        while scheduler.repeat_queue:
            repeat = scheduler.ask()
            assert repeat is not None
            scheduler.complete(repeat, accepted=True, mae_n=0.5)
    before = {item["candidate_key"] for item in scheduler.completed}
    resumed = FigureEightSchedulerV1(
        PersistedSobolCursorV1(cursor_path),
        state_path=state_path,
        proposal_provider=provider,
        campaign_fingerprint_sha256=TEST_FIGURE8_FINGERPRINT,
    )
    assert resumed.novel_count == 24
    next_plan = resumed.ask()
    assert next_plan is not None and next_plan.candidate_key not in before
    assert resumed.cursor.cursor_index >= scheduler.cursor.cursor_index + 128
    resumed.complete(next_plan, accepted=False, failure_signature="test")
    resumed.complete(next_plan, accepted=False, failure_signature="replay")
    assert len([row for row in resumed.completed if row["candidate_key"] == next_plan.candidate_key]) == 1


def test_launch_receipt_never_accepts_forged_booleans_or_pending_handoff(tmp_path: Path) -> None:
    config, pending = _fingerprint()
    forged = {
        "schema": "step6.autotune/figure8-launch-receipt-v2",
        "version": 2,
        "local_package_readback": True,
        "no_contact_canary": True,
        "fingerprint_sha256": pending.sha256,
        "package_triplet_sha256": {role: "0" * 64 for role in ("script", "txt", "urp")},
    }
    with pytest.raises(Exception, match="derived"):
        FigureEightLaunchReceiptV1.from_mapping(forged)
    with pytest.raises(Exception, match="pending"):
        FigureEightLaunchReceiptV1.derive(
            evidence_paths={},
            final_fingerprint=pending,
            package_paths={},
            expected_source_sha256="0" * 64,
            expected_home_frame_sha256="0" * 64,
            expected_controller_identity_sha256="0" * 64,
            expected_eoat_tcp_payload_sha256="0" * 64,
            expected_admission_policy_sha256="0" * 64,
        )
    stale = EvidenceReferenceV1("package_readback", str(tmp_path / "x.json"), "0" * 64)
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    with pytest.raises(Exception, match="hash"):
        stale.verify()


def test_mature_profile_seam_keeps_cycloid_defaults_and_figure8_profile() -> None:
    from step5d_autotune_v4_r013.live_owner import (
        R013PathProfileV1,
        _r013_compat_contract,
        _run_mature_profile_path,
        _run_r013_profile,
    )
    from step5d_autotune_v4_r006.live_adapter import (
        _controller_target_for_contract,
    )

    cycloid = R013PathProfileV1.cycloid()
    figure8 = R013PathProfileV1.figure8()
    assert (cycloid.duration_s, cycloid.required_bins) == (60.0, 550)
    assert (figure8.duration_s, figure8.required_bins) == (60.0, 550)
    assert _r013_compat_contract(cycloid).raw["script2"]["controller_target"] == (
        "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v4_r013.urp"
    )
    assert _r013_compat_contract(figure8).raw["script2"]["controller_target"] == (
        "/programs/andyl/kunwei/step6/step6_figure8_autotune_v1.urp"
    )
    assert _controller_target_for_contract(_r013_compat_contract(figure8)) == (
        "/programs/andyl/kunwei/step6/step6_figure8_autotune_v1.urp"
    )

    class Runtime:
        def run_profile(self, attempt, profile):
            return (attempt, profile.profile_id)

    assert _run_r013_profile(Runtime(), "attempt", figure8)[1] == figure8.profile_id

    observed_duration_s: list[float] = []

    class Writer:
        def __init__(self) -> None:
            self.writer = SimpleNamespace(_path_duration_s=60.0)

        def run_60s(self, attempt: object) -> None:
            del attempt
            observed_duration_s.append(self.writer._path_duration_s)
            raise RuntimeError("deterministic duration test")

    writer = Writer()
    with pytest.raises(RuntimeError, match="deterministic duration test"):
        _run_mature_profile_path(writer, "attempt", figure8)
    assert observed_duration_s == [60.0]
    assert writer.writer._path_duration_s == 60.0


def test_safety_fault_latches_and_never_auto_homes_or_retries(tmp_path: Path) -> None:
    config, fingerprint = _fingerprint()
    scheduler = FigureEightSchedulerV1(PersistedSobolCursorV1(tmp_path / "sobol.json"))
    plan = scheduler.ask()
    assert plan is not None
    campaign = OfflineCampaignV1(StrictAdmissionV1(fingerprint, config.metric), tmp_path / "r.jsonl")

    class FaultWriter(FakeWriter):
        def path_60s(self, candidate):
            self.events.append("60 s PATH")
            raise SafetyFaultError("force_hard_fault")

    writer = FaultWriter()
    with pytest.raises(SafetyFaultError):
        execute_candidate_transaction(
            writer=writer, campaign=campaign, scheduler=scheduler,
            fingerprint=fingerprint, candidate=plan.candidate, trial_id="fault", epoch_id="e1",
        )
    assert writer.events == ["Home", "ARM/apply", "60 s PATH", "release:force_hard_fault"]


def test_convergence_batches_never_stop_before_exact_novel_target(tmp_path: Path) -> None:
    scheduler = FigureEightSchedulerV1(PersistedSobolCursorV1(tmp_path / "sobol.json"))
    with pytest.raises(TypeError):
        scheduler.should_stop(novel_count=80, fresh_proposal_checks=25)  # type: ignore[call-arg]
    for batch_index in range(25):
        scheduler.record_convergence_proposal_batch([
            {
                "candidate_key": f"batch-{batch_index}-{index}",
                "posterior_probability_of_robust_improvement": 0.01,
                "fresh": True,
            }
            for index in range(128)
    ])
    scheduler.top_candidates = [{"n": 5}, {"n": 5}, {"n": 5}]
    assert scheduler.should_stop(novel_count=80) is False
    assert scheduler.should_stop(novel_count=200) is True
    assert len(scheduler.convergence_checks) == 25


def test_figure8_compatibility_threshold_cannot_recreate_point35_early_stop() -> None:
    from step6_figure8_autotune_v1.prepare_live import (
        FIGURE8_DISABLED_APPLICATION_THRESHOLD_N,
        _threshold_receipt,
    )

    receipt = _threshold_receipt(
        contract_sha256="a" * 64,
        campaign_fingerprint="b" * 64,
        issued_at_s=1.0,
    )
    assert receipt["application_mae_threshold_n"] == 1e-12
    assert receipt["application_mae_threshold_n"] == FIGURE8_DISABLED_APPLICATION_THRESHOLD_N
    assert receipt["application_mae_threshold_n"] < 1e-6


def test_live_preparation_fails_before_controller_readback_without_runtime_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from step6_figure8_autotune_v1.prepare_live import (
        FigureEightPreparationError,
        prepare_figure8_live_run,
    )

    monkeypatch.delenv("STEP5D_EXECUTION_ADMISSION", raising=False)
    monkeypatch.delenv("STEP5D_EXECUTION_ADMISSION_RECEIPT", raising=False)
    with pytest.raises(FigureEightPreparationError, match="execution admission"):
        prepare_figure8_live_run(
            tmp_path / "run",
            root=ROOT,
            robot_host="controller.invalid",
            kunwei_host="sensor.invalid",
            kunwei_port=5152,
            controller_readback_dir=tmp_path / "readback",
            canary_dir=tmp_path / "canary",
            home_calibration_receipt_path=None,
            fingerprint=None,  # type: ignore[arg-type]
            campaign_id="test",
            run_id="run",
            attempt_id="r006-test",
        )


def test_step6_bootstrap_invalid_runtime_never_launches_child(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import step5d_managed_runtime as managed_runtime

    launched = False

    class InvalidReceipt:
        status = "invalid"

        def as_dict(self) -> dict[str, object]:
            return {
                "schema": "step5d.execution-admission/runtime-receipt-v1",
                "status": "invalid",
                "reason_code": "CONTROL_RUNTIME_INVALID",
                "detail": "scipy is unavailable",
                "authority_granted": False,
            }

    def fail_launch(*_args: object, **_kwargs: object) -> int:
        nonlocal launched
        launched = True
        raise AssertionError("invalid runtime must not launch the child")

    monkeypatch.setattr(managed_runtime, "admit_control_runtime", lambda *_args, **_kwargs: InvalidReceipt())
    monkeypatch.setattr(managed_runtime, "launch_manifest_route", fail_launch)

    assert figure8_live_cli._bootstrap_managed_runtime(("--status",)) == 2
    assert launched is False
    assert "CONTROL_RUNTIME_INVALID" in capsys.readouterr().err


def test_step6_runtime_request_separates_offline_and_live_side_effects() -> None:
    assert figure8_live_cli._runtime_request(("--status",)).side_effect_level == "offline"
    assert figure8_live_cli._runtime_request(("--validate",)).side_effect_level == "offline"
    assert figure8_live_cli._runtime_request(("--execute-live",)).side_effect_level == "live_mutation"
    assert figure8_live_cli._runtime_request(("--probe-recipe",)).side_effect_level == "live_mutation"


def test_v5_live_bundle_holds_shared_formal_timing_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    profile = object()

    class Lease:
        def __enter__(self):
            events.append("enter")
            return {"pid": 123, "task": "v5-test"}

        def __exit__(self, *_args: object) -> None:
            events.append("exit")

    monkeypatch.setattr(
        figure8_live_cli,
        "ResourceProfile",
        SimpleNamespace(from_env=lambda: profile),
    )
    monkeypatch.setattr(
        figure8_live_cli,
        "formal_timing_lease",
        lambda actual, *, task, blocking: (
            events.append((actual, task, blocking)) or Lease()
        ),
    )

    with figure8_live_cli._v5_formal_timing_lease(tmp_path) as owner:
        assert owner == {"pid": 123, "task": "v5-test"}
        events.append("body")

    assert events[0] == (profile, f"autotuner-v5-live:{tmp_path.resolve()}", False)
    assert events[1:] == ["enter", "body", "exit"]


def test_v5_live_bundle_busy_formal_timing_fails_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = object()
    current_owner = {"pid": 456, "task": "pytest:v5-live-owner"}
    notices: list[tuple[object, str]] = []

    class BusyLease:
        def __enter__(self):
            raise BlockingIOError("busy")

        def __exit__(self, *_args: object) -> None:
            raise AssertionError("busy lease was never entered")

    monkeypatch.setattr(
        figure8_live_cli,
        "ResourceProfile",
        SimpleNamespace(from_env=lambda: profile),
    )
    monkeypatch.setattr(
        figure8_live_cli,
        "formal_timing_lease",
        lambda *_args, **_kwargs: BusyLease(),
    )
    monkeypatch.setattr(
        figure8_live_cli,
        "formal_timing_owner",
        lambda actual: current_owner if actual is profile else None,
    )
    monkeypatch.setattr(
        figure8_live_cli,
        "notify_formal_timing_owner",
        lambda owner, message: notices.append((owner, message)) or True,
    )

    with pytest.raises(figure8_live_cli.FigureEightError, match="no live retry"):
        with figure8_live_cli._v5_formal_timing_lease(tmp_path):
            raise AssertionError("busy formal timing lease must not enter the body")

    assert notices == [
        (
            current_owner,
            f"Autotuner V5 live bundle is blocked by formal_timing: {tmp_path.resolve()}",
        )
    ]


def test_report_labels_and_mature_step6_package_boundary() -> None:
    config, _fingerprint_value = _fingerprint()
    contract = build_report_evidence_contract(
        [
            {"candidate_key": "a", "mae_n": 0.4},
            {"candidate_key": "a", "mae_n": 0.5},
            {"candidate_key": "a", "mae_n": 0.3},
        ],
        metric=config.metric,
        posterior_incumbent={"candidate_key": "a", "probability": 0.9},
    )
    payload = contract.as_dict()
    assert payload["metric_fingerprint"]["required_bin_count"] == 550
    assert payload["single_trial_minimum"] == 0.3
    assert payload["repeated_incumbent"]["n"] == 3
    package_dir = ROOT / "programs" / "step6"
    script = (package_dir / f"{PROGRAM_NAME}.script").read_text(encoding="utf-8")
    assert "speedj(active_path_qdot, 40.000000000, bounded_motion_dt)" in script
    assert "speedj(path_qdot, 40.000000000, actual_path_dt)" not in script
    assert "speedl([cmd_vx, cmd_vy" not in script


def test_report_closeout_requires_cold_release_home_safety_evidence(tmp_path: Path) -> None:
    from step6_figure8_autotune_v1.report import (
        FigureEightReportError,
        _require_closeout_evidence,
    )

    with pytest.raises(FigureEightReportError, match="release evidence is missing"):
        _require_closeout_evidence(tmp_path, final=False)

    release = {
        "passed": False,
        "writer_released": False,
        "safety_mode": "NORMAL",
        "position_error_m": 0.0,
        "orientation_error_rad": 0.0,
        "linear_speed_m_s": 0.0,
        "angular_speed_rad_s": 0.0,
    }
    (tmp_path / "checkpoint_080_writer_release.json").write_text(
        json.dumps(release), encoding="utf-8"
    )
    with pytest.raises(FigureEightReportError, match="not passed Home/Safety closeout"):
        _require_closeout_evidence(tmp_path, final=False)

    release.update({"passed": True, "writer_released": True})
    (tmp_path / "checkpoint_080_writer_release.json").write_text(
        json.dumps(release), encoding="utf-8"
    )
    evidence = _require_closeout_evidence(tmp_path, final=False)
    assert evidence["release"]["writer_released"] is True


def test_report_uses_one_canonical_artifact_renderer_and_keeps_censor_nontrainable(
    tmp_path: Path,
) -> None:
    from step5d_autotune_v4_r013.lifecycle_trace import LifecycleTrace
    from step6_figure8_autotune_v1.report import build_figure8_report

    state = tmp_path / "campaign"
    (state / "trial_receipts").mkdir(parents=True)
    (state / "optimizer").mkdir()
    (state / "raw").mkdir()
    (state / "censored_raw").mkdir()
    fingerprint = "a" * 64
    cursor = PersistedSobolCursorV1(tmp_path / "report-sobol.json")
    candidate = CompleteCandidateV1.from_mapping(
        cursor.fresh_pool(domain="controller_path")[0]
    )
    censored_candidate = CompleteCandidateV1.from_mapping(
        cursor.fresh_pool(domain="controller_path")[0]
    )

    trace = LifecycleTrace(
        state / "lifecycle",
        chunk_records=64,
        path_min_duration_s=59.5,
        max_gap_s=0.2,
    )
    trace.begin_attempt(1, "report-exec", "BO_TRIAL", epoch=1, path_requested=True)

    def output(timestamp: float) -> SimpleNamespace:
        return SimpleNamespace(
            timestamp=timestamp,
            observed_at_s=timestamp,
            tcp_pose_m_rad=(0.4620551816, 0.1778825964, 0.03, 3.120752062, 0.0, 0.068626833),
            tcp_speed_m_s_rad_s=(0.0,) * 6,
            qd_rad_s=(0.0,) * 6,
        )

    def sensor(force: float) -> SimpleNamespace:
        return SimpleNamespace(
            normal_load_n=force,
            force_norm_n=abs(force),
            filtered_normal_n=force,
            torque_norm_nm=0.0,
            wrench=(0.0, 0.0, -force, 0.0, 0.0, 0.0),
            sensor_fresh=True,
        )

    for index, state_code in enumerate((11, 20, 21)):
        trace.observe_tick(
            monotonic_s=index * 0.1,
            output=output(index * 0.1),
            sensor=sensor(5.0),
            tp_state=state_code,
            setpoint_n=5.0,
            packet_sequence=index,
            consumed_packet_sequence=index,
        )
    for index in range(601):
        timestamp = 0.3 + index * 0.1
        trace.observe_tick(
            monotonic_s=timestamp,
            output=output(timestamp),
            sensor=sensor(5.0 + 0.05 * ((index % 7) - 3)),
            tp_state=25,
            setpoint_n=5.0,
            packet_sequence=index + 3,
            consumed_packet_sequence=index + 3,
        )
    trace.observe_tick(
        monotonic_s=60.4,
        output=output(60.4),
        sensor=sensor(5.0),
        tp_state=40,
        setpoint_n=5.0,
            packet_sequence=604,
            consumed_packet_sequence=604,
    )
    trace.observe_terminal(
        monotonic_s=60.5,
        output=output(60.5),
        sensor=sensor(5.0),
        tp_state=78,
        packet_sequence=605,
        consumed_packet_sequence=605,
    )
    lifecycle = trace.finalize_attempt()
    assert lifecycle["coverage_complete"] is True

    samples = [
        {
            "path_time_s": round(index * 0.1, 10),
            "filtered_normal_n": 5.0 + 0.05 * ((index % 7) - 3),
        }
        for index in range(600)
    ]
    raw_path = state / "raw" / "incumbent.json"
    raw_path.write_text(json.dumps({"samples": samples}), encoding="utf-8")
    for index, mae in enumerate((0.39, 0.41, 0.40), start=1):
        receipt = {
            "schema": "step6.autotune/figure8-production-trial-receipt-v1",
            "plan": {
                "novel_ordinal": index,
                "phase": "novel_warm_start",
                "kind": "novel" if index == 1 else "repeat",
                "candidate": candidate.as_dict(),
            },
            "physical_admission": {
                "dispatch_id": f"exact-{index}",
                "trial_admission_passed": True,
                "motion_gate": True,
                "timing_gate": True,
                "sealed": True,
                "sealed_mae_n": mae,
            },
            "raw_artifact_path": str(raw_path),
            "force_lifecycle": lifecycle,
            "safe_return": True,
        }
        (state / "trial_receipts" / f"trial-{index:06d}.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )

    prefix_samples = [
        {"path_time_s": round(index * 0.02, 10), "filtered_normal_n": 7.0}
        for index in range(376)
    ]
    prefix_path = state / "censored_raw" / "censored-000004.json"
    prefix_payload = {
        "schema": "step6.autotune/figure8-censored-raw-prefix-v1",
        "version": 1,
        "candidate_key": censored_candidate.candidate_key,
        "campaign_fingerprint_sha256": fingerprint,
        "trial_id": "censored-4",
        "watermark_s": 7.5,
        "closed_bin_count": 25,
        "samples": prefix_samples,
        "raw_gaps": [],
    }
    prefix_path.write_text(json.dumps(prefix_payload), encoding="utf-8")
    censored_receipt = {
        "candidate": censored_candidate.as_dict(),
        "candidate_key": censored_candidate.candidate_key,
        "campaign_fingerprint_sha256": fingerprint,
        "attempt_id": "censored-attempt-4",
        "prefix_mean_n": 2.0,
        "causal_lower_bound_n": 0.2,
        "watermark_s": 7.5,
        "closed_bin_count": 25,
        "safe_return_closed": True,
    }
    (state / "trial_receipts" / "trial-000004-censored.json").write_text(
        json.dumps({
            "schema": "step6.autotune/figure8-production-trial-receipt-v1",
            "plan": {
                "novel_ordinal": 4,
                "phase": "novel_warm_start",
                "kind": "novel",
                "candidate": censored_candidate.as_dict(),
            },
            "censored": True,
            "censored_receipt": censored_receipt,
            "raw_prefix_artifact_path": str(prefix_path),
            "raw_prefix_artifact_sha256": hashlib.sha256(prefix_path.read_bytes()).hexdigest(),
        }),
        encoding="utf-8",
    )
    scheduler = {
        "campaign_fingerprint_sha256": fingerprint,
        "novel_count": 80,
        "novel_dispatch_count": 81,
        "probe_bounds_open": {},
        "drift_state": {"paused": False},
        "convergence_checks": [],
        "observation_groups": {
            candidate.candidate_key: {
                "candidate": candidate.as_dict(),
                "n": 3,
                "mean_n": 0.4,
                "sample_std_n": 0.01,
                "yvar_n2": 0.0001,
            }
        },
    }
    (state / "scheduler_state.json").write_text(
        json.dumps(scheduler), encoding="utf-8"
    )
    optimizer_response = {
        "observed_posterior_mean_n": {candidate.candidate_key: 0.405},
        "observed_posterior_variance_n2": {candidate.candidate_key: 0.0004},
        "fit_receipt": {
            "block": "controller_path",
            "lengthscales_unit_fitted": [1.0, 1.1, 1.2, 1.3, 1.4, 1.5],
        },
    }
    (state / "optimizer" / "qlognei-000001-response.json").write_text(
        json.dumps(optimizer_response), encoding="utf-8"
    )
    (state / "matched_handoff_ab.json").write_text(
        json.dumps({"winner_handoff_policy": "freeze_carry_v1"}),
        encoding="utf-8",
    )
    (state / "checkpoint_080_writer_release.json").write_text(
        json.dumps({
            "passed": True,
            "writer_released": True,
            "safety_mode": "NORMAL",
            "position_error_m": 0.0,
            "orientation_error_rad": 0.0,
            "linear_speed_m_s": 0.0,
            "angular_speed_rad_s": 0.0,
        }),
        encoding="utf-8",
    )

    result = build_figure8_report(state, checkpoint_novel=80, final=False)
    artifact = json.loads(Path(result["artifact"]).read_text(encoding="utf-8"))
    ids = [block["id"] for block in artifact["manifest"]["blocks"]]
    assert ids.index("bo_progress_figure") < ids.index("best_diagnostics_figure")
    assert ids.index("best_diagnostics_figure") < ids.index("repeatability_figure")
    assert artifact["snapshot"]["datasets"]["censor_table"][0]["prefix_mean_n"] == 2.0
    assert Path(result["html"]).is_file()
    assert Path(result["canonical_qa"]).is_file()
    delivery = json.loads(
        Path(result["portable_delivery_receipt"]).read_text(encoding="utf-8")
    )
    assert delivery["stages"]["verification"] in {"passed", "structural_only"}


def test_final_report_callback_requires_verified_visible_chrome_and_saves_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from step6_figure8_autotune_v1 import report

    report_path = tmp_path / "reports" / "final" / "report.html"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<!doctype html><title>final</title>", encoding="utf-8")
    monkeypatch.setattr(
        report,
        "build_figure8_report",
        lambda *_args, **_kwargs: {"html": str(report_path)},
    )
    opened = {
        "ok": True,
        "visible_verified": True,
        "artifacts": [
            {
                "path": str(report_path.resolve()),
                "visible_verified": True,
                "window_id": "0x123",
            }
        ],
    }
    monkeypatch.setattr(
        report,
        "_open_final_report_in_chrome",
        lambda path: opened if path == report_path else pytest.fail("wrong report path"),
    )

    report.checkpoint_report_callback(
        tmp_path,
        200,
        {"final": True},
    )

    persisted = json.loads(
        report_path.with_name("chrome_open_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted == opened
