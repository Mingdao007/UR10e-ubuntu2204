from __future__ import annotations

from pathlib import Path
import json
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(REPO / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r013.bounded_bo import (  # noqa: E402
    BOUNDED_BO_POLICY,
    BoundedBOSeedConfigV1,
)
from step5d_autotune_v4_r013.campaign import (  # noqa: E402
    Campaign,
    HandoffPolicy,
    PhysicalAdmissionReceipt,
    candidate_token,
    strategy_canary_plan,
)
from step5d_autotune_v4_r013.campaign_config import (  # noqa: E402
    R013_HOST_SOURCE_PATHS,
    r013_host_source_closure,
)
from step5d_autotune_v4_r013.live_owner import (  # noqa: E402
    R013OwnerError,
    _fresh_trial_flow_gates,
)
from run_step5d_autotune_v4_r013_live import (  # noqa: E402
    _bounded_bo_report,
    _validate_bounded_bo_preflight,
)
from step5d_autotune_v4_r013.domain import physical_candidate_key  # noqa: E402
from step5d_autotune_v4_r013.identity import CampaignFingerprint  # noqa: E402


SEED_PATH = ROOT / "config/step5d/r013_0p31_bounded_bo_seed_v1.json"


def _metrics() -> dict[str, object]:
    return {
        "schema": "step5d.autotune-v4/r013-trial-anti-windup-v1",
        "policy": "conditional-double-clamp-v1",
        "max_abs_integral_n_s": 0.0,
        "max_abs_i_term": 0.0,
        "saturation_duty": 0.0,
        "freeze_duty": 0.0,
        "invariant_violation_count": 0,
        "reset_reasons": ["candidate_dispatch", "path_entry", "mode_exit_or_home"],
        "path_gain_hot_switch": False,
    }


def _receipt(strategy: dict[str, object]) -> dict[str, object]:
    from step5d_autotune_v4_r013.runtime_strategy import runtime_strategy_sha256

    return {
        "schema": "step5d.autotune-v4/r013-runtime-strategy-receipt-v1",
        "runtime_strategy_sha256": runtime_strategy_sha256(strategy),
        "enabled": True,
        "path_clock": "runtime_desired_twist_path_time_s",
        "path_sample_count": 30001,
        "formal_sample_count": 27500,
        "minimum_effective_target_n": 3.9632449269199883,
        "maximum_applied_correction_n": 1.0367550730800117,
        "first_path_time_s": 0.0,
        "last_path_time_s": 60.0,
        "maximum_path_clock_gap_s": 0.002,
        "violation_count": 0,
        "exit_restored_to_unmodified_target": True,
        "safe_return_transition_observed": True,
        "exit_mode": "safe_return",
    }


def _admission(
    dispatch,
    *,
    objective: float = 0.5,
    eligible: bool = True,
) -> PhysicalAdmissionReceipt:
    return PhysicalAdmissionReceipt(
        dispatch_id=dispatch.dispatch_id,
        candidate_token=candidate_token(dispatch.candidate),
        candidate_key=physical_candidate_key(dispatch.candidate),
        attempt_sequence=dispatch.ordinal,
        execution_id=f"execution-{dispatch.ordinal}",
        sealed_mae_n=objective,
        physical_eligible=eligible,
        timing_gate=eligible,
        motion_gate=eligible,
        qualification_passed=eligible,
        observation_uid=f"observation-{dispatch.ordinal}",
    )


def _campaign(tmp_path: Path) -> tuple[Campaign, BoundedBOSeedConfigV1]:
    seed = BoundedBOSeedConfigV1.load(SEED_PATH)
    fingerprint = CampaignFingerprint.legacy_default(
        handoff_policy=HandoffPolicy().policy,
        runtime_strategy_identity=seed.runtime_strategy_sha256,
    )
    campaign = Campaign.create(
        tmp_path / "r013_ledger.jsonl",
        campaign_id="r013-bounded-test",
        run_id="fresh",
        attempt_id="attempt",
        noise_floor_n2=0.01,
        r012_seed_source={},
        seed_template=seed.candidate,
        runtime_strategy=seed.runtime_strategy,
        handoff_policy=HandoffPolicy(),
        campaign_fingerprint=fingerprint,
        bounded_bo_profile=seed.campaign_profile(),
        campaign_role={
            "schema": "step5d.autotune-v4/r013-bounded-bo-role-v1",
            "version": 1,
            "role": "bounded_bo_v1",
            "formal_campaign_tell_exact": True,
            "launch_ready": True,
        },
    )
    campaign.install_strategy_canary_plan(
        strategy_canary_plan(runtime_strategy_sha256_value=campaign.runtime_strategy_sha256)
    )
    return campaign, seed


def test_seed_receipt_matches_historical_control_law_and_is_not_old_objective() -> None:
    seed = BoundedBOSeedConfigV1.load(SEED_PATH)
    assert seed.policy == BOUNDED_BO_POLICY
    assert seed.physical_attempt_budget == 100
    assert seed.candidate["force_damping"] == pytest.approx(188.36079701683204)
    assert candidate_token(seed.candidate) == seed.provenance["historical_candidate_token"]
    assert seed.runtime_strategy_sha256 == "bc3fa1dd6efc2e8fad995e6325789baadca5a71e11b5661315209b359104f4fd"
    assert seed.source_closure_sha256 == r013_host_source_closure(source_root=ROOT)["sha256"]
    assert "historical_objectives_not_imported" in seed.provenance["historical_evidence_scope"]
    assert seed.receipt_sha256 == seed.campaign_profile()["seed_receipt_sha256"]


def test_bounded_source_closure_contains_direct_live_dependencies() -> None:
    required = {
        "tools/step5d_autotune_v4_r004/arm_transition.py",
        "tools/step5d_autotune_v4_r004/evidence.py",
        "tools/step5d_autotune_v4_r004/timing.py",
        "tools/step5d_autotune_v4_r004/transport.py",
        "tools/step5d_autotune_v4_live_writer.py",
        "tools/step5d_autotune_v4_r004_live_writer.py",
        "tools/step5d_autotune_v4_r005/live_adapter.py",
        "tools/step5d_autotune_v4_r005/observations.py",
        "tools/step5d_autotune_v4_r005/runtime.py",
        "tools/step5d_autotune_v4_r006/thresholds.py",
        "tools/step5d_autotune_v4_r008/bounded_resume_ledger.py",
        "tools/step5d_autotune_v4_r008/state20_search_trace.py",
        "tools/step5d_autotune_v4_r008/state25_path_trace.py",
        "tools/step5d_autotune_v4_r012/compat_identity.py",
        "tools/step5d_autotune_v3/dashboard.py",
        "tools/step5d_autotune_v3/rtde_client.py",
        "tools/step5d_bridge_authority.py",
        "tools/step5d_eoat_profiles.py",
        "tools/step5d_remote_startup.py",
        "tools/step6_figure8_autotune_v1/__init__.py",
        "tools/step6_figure8_autotune_v1/live_composition.py",
        "tools/ur10e_parallel.py",
        "tools/upload_ur_tp_package.py",
    }
    assert required.issubset(set(R013_HOST_SOURCE_PATHS))
    assert required.issubset(set(r013_host_source_closure()["files"]))


def test_seed_validator_rejects_non_bounded_budget_or_historical_token() -> None:
    raw = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    raw["physical_attempt_budget"] = 99
    with pytest.raises(ValueError, match="exactly 100"):
        BoundedBOSeedConfigV1.from_mapping(raw)

    raw = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    raw["provenance"]["historical_candidate_token"] = "0" * 64
    with pytest.raises(ValueError, match="historical candidate token"):
        BoundedBOSeedConfigV1.from_mapping(raw)


def test_campaign_profile_rejects_non_bounded_budget(tmp_path: Path) -> None:
    seed = BoundedBOSeedConfigV1.load(SEED_PATH)
    profile = seed.campaign_profile()
    profile["physical_attempt_budget"] = 99
    with pytest.raises(ValueError, match="exactly 100"):
        Campaign.create(
            tmp_path / "r013_ledger.jsonl",
            campaign_id="r013-bounded-invalid-budget",
            run_id="fresh",
            attempt_id="attempt",
            noise_floor_n2=0.01,
            r012_seed_source={},
            seed_template=seed.candidate,
            runtime_strategy=seed.runtime_strategy,
            handoff_policy=HandoffPolicy(),
            campaign_fingerprint=CampaignFingerprint.legacy_default(
                handoff_policy=HandoffPolicy().policy,
                runtime_strategy_identity=seed.runtime_strategy_sha256,
            ),
            bounded_bo_profile=profile,
        )


def test_bounded_bo_uses_seed_then_continues_after_strategy_canary(tmp_path: Path) -> None:
    campaign, seed = _campaign(tmp_path)
    receipt = _receipt(seed.runtime_strategy)
    for objective in (0.30, 0.34, 0.35):
        dispatch, _ = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, objective=objective),
            anti_windup_metrics=_metrics(),
            runtime_strategy_receipt=receipt,
            runtime_strategy_sidecar_sha256="a" * 64,
        )
    assert campaign.confirmation_summary().target_achieved is True
    assert campaign.target_achieved is False
    assert campaign.snapshot["training_lineage"] == "fresh_r013_only"
    next_dispatch, _ = campaign.ask()
    assert next_dispatch.kind == "WARM_FIXED_KI"
    assert next_dispatch.candidate["force_p_gain"] == seed.candidate["force_p_gain"]
    assert campaign.bounded_bo_budget_exhausted is False


def test_bounded_checkpoint_does_not_dispatch_confirmation(tmp_path: Path) -> None:
    campaign, seed = _campaign(tmp_path)
    receipt = _receipt(seed.runtime_strategy)
    for _ in range(3):
        dispatch, _ = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, objective=0.50),
            anti_windup_metrics=_metrics(),
            runtime_strategy_receipt=receipt,
            runtime_strategy_sidecar_sha256="a" * 64,
        )

    warm, _ = campaign.ask()
    campaign.tell_exact(
        admission=_admission(warm, objective=0.30),
        anti_windup_metrics=_metrics(),
        runtime_strategy_receipt=receipt,
        runtime_strategy_sidecar_sha256="a" * 64,
    )
    next_dispatch, _ = campaign.ask()
    assert next_dispatch.kind != "CONFIRMATION"


def test_bounded_bo_budget_counts_physical_dispatches_and_resumes(tmp_path: Path) -> None:
    campaign, seed = _campaign(tmp_path)
    receipt = _receipt(seed.runtime_strategy)
    for _ in range(3):
        canary, _ = campaign.ask()
        campaign.tell_exact(
            admission=_admission(canary),
            anti_windup_metrics=_metrics(),
            runtime_strategy_receipt=receipt,
            runtime_strategy_sidecar_sha256="a" * 64,
        )
    # The budget predicate is intentionally based on serial physical dispatch
    # count, including rows that may later be rejected as timing-ineligible.
    for ordinal in range(97):
        dispatch = campaign._persist_dispatch(  # noqa: SLF001 - contract test
            {**campaign.snapshot["seed_template"], "force_i_gain": 0.0, "i_off": True},
            kind="WARM_FIXED_KI",
            abort_allowed=False,
        )
        campaign.tell_exact(
            admission=_admission(dispatch, eligible=False),
            anti_windup_metrics=_metrics(),
            runtime_strategy_receipt=receipt,
            runtime_strategy_sidecar_sha256="b" * 64,
        )
        assert dispatch.ordinal == ordinal + 4
    assert campaign.physical_attempt_count == 100
    assert campaign.bounded_bo_budget_exhausted is True
    assert campaign.target_achieved is False
    resumed = Campaign.resume(campaign.ledger.path)
    assert resumed.bounded_bo_budget_exhausted is True
    assert resumed.completion_status["checkpoint_only_target"] is True
    report = _bounded_bo_report(campaign=resumed, run_dir=tmp_path)
    assert report["physical_attempt_count"] == 100
    assert report["timing_ineligible_count"] == 97


def test_bounded_bo_preflight_requires_timing_3of3_and_boundary_2of2(tmp_path: Path) -> None:
    campaign, seed = _campaign(tmp_path)
    receipt = {
        "schema": "step5d.autotune-v4/r013-bounded-bo-preflight-v1",
        "version": 1,
        "campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
        "source_closure_sha256": seed.source_closure_sha256,
        "runtime_strategy_sha256": seed.runtime_strategy_sha256,
        "timing_characterization": {"attempts_completed": 3, "attempts_passed": 3},
        "resident_boundary_canary": {"attempts_completed": 2, "attempts_passed": 2},
        "verified_home": True,
        "safety_normal": True,
    }
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert _validate_bounded_bo_preflight(
        campaign=campaign, seed=seed, run_dir=tmp_path, receipt_path=path
    )["version"] == 1
    receipt["timing_characterization"]["attempts_passed"] = 2
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(SystemExit, match="3/3"):
        _validate_bounded_bo_preflight(
            campaign=campaign, seed=seed, run_dir=tmp_path, receipt_path=path
        )


def test_timing_gap_is_ineligible_but_recoverable_after_verified_home() -> None:
    from types import SimpleNamespace

    result = SimpleNamespace(
        safe_return=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        identity_gate=True,
        duration_s=60.0,
        metrics={
            "r013_force_lifecycle_complete": True,
            "r013_force_lifecycle": {
                "path_duration_s": 60.0,
                "max_nonterminal_gap_s": 0.041,
                "max_rtde_gap_s": 0.041,
            },
            "force_objective": {"complete_bins": 550, "required_bins": 550},
        },
    )
    _fresh_trial_flow_gates(result, allow_timing_ineligible=True)
    with pytest.raises(R013OwnerError, match="freshness gap"):
        _fresh_trial_flow_gates(result, allow_timing_ineligible=False)
