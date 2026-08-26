from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from step6_figure8_autotune_v1.core import CompleteCandidateV1  # noqa: E402
from step6_figure8_autotune_v1.v5_campaign import (  # noqa: E402
    CampaignReportV2,
    CampaignRoleV2,
)
from step6_figure8_autotune_v1.v5_lifecycle_ledger import (  # noqa: E402
    FigureEightPhysicalRecordV2,
    LedgerRole,
    V5PhysicalAdmissionLedgerV2,
    canonical_sha256,
)
from step6_figure8_autotune_v1.v5_report import (  # noqa: E402
    V5EvidenceReportError,
    build_v5_evidence_report,
    build_v5_trial_evidence,
    cold_verify_v5_evidence_report,
)
from test_step6_autotuner_v5_lifecycle_ledger import (  # noqa: E402
    FP,
    RELEASE,
    _chain,
    _gate_closure,
)


CORRECTION_FP = "c" * 64


def _controller(scale: float) -> dict[str, object]:
    return {
        "force_p_gain": 0.02 * scale,
        "force_damping": 100.0,
        "force_i_gain": 0.01,
        "i_off": False,
        "normal_filter_tau_s": 0.04,
        "orientation_ko": 0.04,
        "motion_kp": 2.0,
    }


def _candidate(scale: float, weights=(0.0,) * 6) -> CompleteCandidateV1:
    return CompleteCandidateV1(_controller(scale), tuple(weights))


def _reports() -> tuple[CampaignReportV2, CampaignReportV2]:
    candidates = [_candidate(1.0), _candidate(1.1), _candidate(1.2)]
    primary = CampaignReportV2(
        role=CampaignRoleV2.PRIMARY,
        campaign_fingerprint=FP,
        exact_novel_count=200,
        winner_candidate_key=candidates[0].candidate_key,
        winner_candidate=candidates[0].as_dict(),
        decision="winner_mean_mae_n=0.1",
        top_candidates=tuple(
            {
                "candidate_key": candidate.candidate_key,
                "original_mae_n": mean,
                "confirmation_maes_n": [mean] * 4,
                "total_n": 5,
                "repeated_mean_n": mean,
            }
            for candidate, mean in zip(candidates, (0.1, 0.2, 0.3), strict=True)
        ),
    )
    corrected = _candidate(1.0, (0.1, 0.0, 0.0, 0.0, 0.0, 0.0))
    correction = CampaignReportV2(
        role=CampaignRoleV2.CORRECTION,
        campaign_fingerprint=CORRECTION_FP,
        exact_novel_count=60,
        winner_candidate_key=None,
        winner_candidate=None,
        corrected_best_candidate_key=corrected.candidate_key,
        corrected_best_candidate=corrected.as_dict(),
        paired_differences=(-0.1,) * 5,
        paired_mean_difference_n=-0.1,
        paired_ci90_n=(-0.1, -0.1),
        paired_df=4,
        automatic_promotion=False,
        decision="improvement",
        primary_winner_controller_sha256=canonical_sha256(_controller(1.0)),
        matched_zero_maes_n=(1.0,) * 5,
        matched_corrected_maes_n=(0.9,) * 5,
    )
    return primary, correction


def _ledgers(tmp_path: Path):
    artifact, _binding, records = _chain(
        tmp_path / "artifact", attempt_count=1, prefix="report"
    )
    primary_record = records[0]
    primary = V5PhysicalAdmissionLedgerV2(
        tmp_path / "primary.jsonl",
        campaign_fingerprint=FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.PRIMARY,
    )
    primary.append_record(primary_record)

    correction_slice = replace(
        primary_record.trial_slice,
        attempt_id="correction-attempt",
        trial_id="correction-trial",
    )
    correction_record = FigureEightPhysicalRecordV2(
        CORRECTION_FP,
        RELEASE,
        LedgerRole.CORRECTION,
        "correction-chain",
        correction_slice.attempt_id,
        correction_slice.trial_id,
        correction_slice.candidate_identity,
        correction_slice.metric_snapshot,
        correction_slice.boundary,
        correction_slice,
        artifact,
        _gate_closure(artifact, correction_slice),
    )
    correction = V5PhysicalAdmissionLedgerV2(
        tmp_path / "correction.jsonl",
        campaign_fingerprint=CORRECTION_FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.CORRECTION,
    )
    correction.append_record(correction_record)
    return primary_record, primary, correction


def test_trial_report_cold_derives_raw_coverage_bins_tail_and_no_interpolation(
    tmp_path: Path,
) -> None:
    record, _primary, _correction = _ledgers(tmp_path)
    evidence = build_v5_trial_evidence(record)
    assert evidence["source_artifact"]["coverage_complete"] is True
    assert set(evidence["source_artifact"]["gaps_and_errors"].values()) == {0}
    assert evidence["source_artifact"]["interpolation_used"] is False
    assert evidence["evidence_0_60"]["bin_count"] == 600
    assert evidence["formal_5_60"]["bin_count"] == 550
    assert evidence["formal_5_60"]["raw_measured_force_is_metric_input"] is False
    assert evidence["ext60_tail"]["row_count"] == 1417
    assert evidence["ext60_tail"]["included_in_gp_or_budget"] is False
    assert evidence["narrow_force_window_telemetry"]["blocking"] is False


def test_final_v5_report_is_self_contained_cold_verified_and_tamper_evident(
    tmp_path: Path,
) -> None:
    _record, primary_ledger, correction_ledger = _ledgers(tmp_path)
    primary_report, correction_report = _reports()
    release = {
        "home_verified": True,
        "stopped": True,
        "writer_released": True,
    }
    capability_body = {
        "schema": "step6.autotune/autotuner-v5-capability-acceptance-v1",
        "version": 1,
        "status": "PASSED",
        "release_identity_sha256": RELEASE,
        "verified_home": True,
        "stopped": True,
        "writer_released": True,
        "campaign_qualification": False,
        "narrow_force_windows_blocking": False,
        "contact_capabilities": {
            "home_vs_rollover_five_paired_entries": {
                "ci90_n": [-0.01, 0.01]
            }
        },
    }
    capability = {
        **capability_body,
        "receipt_sha256": canonical_sha256(capability_body),
    }
    receipt = build_v5_evidence_report(
        output_root=tmp_path / "report",
        release_identity={"release_identity_sha256": RELEASE},
        primary_report=primary_report,
        correction_report=correction_report,
        primary_physical=primary_ledger,
        correction_physical=correction_ledger,
        primary_writer_release=release,
        correction_writer_release=release,
        capability_acceptance=capability,
        sidecars={
            "physical_campaign_dependency": False,
            "control_authority": False,
        },
    )
    evidence = cold_verify_v5_evidence_report(tmp_path / "report" / "build_receipt.json")
    assert receipt["status"] == "COMPLETE"
    assert evidence["selection"]["physically_repeated_incumbent"]["total_n"] == 5
    assert evidence["selection"]["matched_zero_vs_corrected"]["paired_ci90_n"] == [-0.1, -0.1]
    assert evidence["scope"]["tail_excluded_from_mae_censor_gp_budget"] is True
    assert evidence["capability_acceptance"]["status"] == "PASSED"
    html_text = (tmp_path / "report" / "report.html").read_text(encoding="utf-8")
    assert "http://" not in html_text and "https://" not in html_text
    assert "550 bins" in html_text and "No interpolation" in html_text

    evidence_path = tmp_path / "report" / "evidence.json"
    tampered = json.loads(evidence_path.read_text(encoding="utf-8"))
    tampered["scope"]["automatic_promotion"] = True
    evidence_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(V5EvidenceReportError):
        cold_verify_v5_evidence_report(tmp_path / "report" / "build_receipt.json")
