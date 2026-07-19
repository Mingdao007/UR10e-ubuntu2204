from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_autotune_v3_execution_readiness as readiness  # noqa: E402
from step5d_autotune_v3 import state as v3_state  # noqa: E402


RELATIVES = set(v3_state.ORCHESTRATION_RELATIVE_PATHS) | {
    "config/current_stage.json",
    "config/step5_stage_table.json",
    "config/step5/step5d_autotune_v3_offline_validation.json",
    "config/step5/step5d_autotune_v3_live_promotion.json",
    "config/step5d_autotune_controller_readback_v3.json",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.deploy-manifest.json",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.script",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.txt",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.urp",
    "config/step5/step5d_autotune_v3_control_contract.json",
}


def _fixture_root(tmp_path: Path) -> Path:
    fixture = tmp_path / "experiment"
    for relative in RELATIVES:
        source = ROOT / relative
        target = fixture / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return fixture


def _mutate_v3(fixture: Path, mutate) -> None:
    path = fixture / "config/step5_stage_table.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = next(
        item
        for item in payload["stages"]
        if item.get("id") == readiness.V3_STAGE_ID
    )
    mutate(row)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_repository_signal_names_the_next_legal_action() -> None:
    report = readiness.verify(ROOT)
    assert report["ok"] is True
    assert report["state"] == "pre_live_blocked"
    assert report["public_success_signal"] == "requires_attended_tp_upload_readback"
    assert report["package_delivery"] == "requires_attended_tp_upload_readback"
    assert report["ready_to_execute"] is False
    assert report["current_stage_id"] == readiness.V1_STAGE_ID
    assert report["next_owner"] == "ur10e-contact-control-prep"
    assert report["timing_diagnostic"] == "failed_host_schedule"
    assert report["canonical_gate"] == [
        "current_source_paced_seam",
        "formal_500hz_timing",
        "certified_stopping_bound",
    ]
    assert report["user_authorization_required"] is True
    assert report["hil_hold_required"] is False


def test_repository_live_signal_is_the_only_readiness_state() -> None:
    with pytest.raises(
        readiness.ReadinessError,
        match="requires_current_source_paced_seam_formal_timing",
    ):
        readiness.verify(ROOT, require_live=True)


def test_live_promotion_validation_digest_is_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    promotion_path = fixture / "config/step5/step5d_autotune_v3_live_promotion.json"
    promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
    promotion["deterministic_validation"]["sha256"] = "0" * 64
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")

    with pytest.raises(readiness.ReadinessError, match="deterministic validation digest"):
        readiness.verify(fixture, require_live=True)


def test_current_validation_identity_is_frozen(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    validation_path = (
        fixture / "config/step5/step5d_autotune_v3_offline_validation.json"
    )
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["identity"]["control_fingerprint"] = "0" * 64
    validation_path.write_text(json.dumps(validation), encoding="utf-8")

    with pytest.raises(readiness.ReadinessError, match="current validation identity"):
        readiness.verify(fixture)


def test_pre_live_validation_decision_cannot_promote_current_candidate(
    tmp_path: Path,
) -> None:
    fixture = _fixture_root(tmp_path)
    validation_path = (
        fixture / "config/step5/step5d_autotune_v3_offline_validation.json"
    )
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["decision"]["execution_readiness"] = (
        "ready_for_v3_live_continuous_campaign"
    )
    validation_path.write_text(json.dumps(validation), encoding="utf-8")

    with pytest.raises(readiness.ReadinessError, match="pre-live validation decision"):
        readiness.verify(fixture)


def test_sphere_seam_cannot_substitute_for_formal_three_lane_timing(
    tmp_path: Path,
) -> None:
    fixture = _fixture_root(tmp_path)
    validation_path = (
        fixture / "config/step5/step5d_autotune_v3_offline_validation.json"
    )
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["gates"]["formal_500hz_timing"]["status"] = "pass"
    validation["gates"]["formal_500hz_timing"]["release_gate_satisfied"] = True
    validation_path.write_text(json.dumps(validation), encoding="utf-8")

    with pytest.raises(readiness.ReadinessError, match="formal timing status"):
        readiness.verify(fixture)


def test_failed_current_source_paced_seam_cannot_be_mislabeled_pass(
    tmp_path: Path,
) -> None:
    fixture = _fixture_root(tmp_path)
    validation_path = (
        fixture / "config/step5/step5d_autotune_v3_offline_validation.json"
    )
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    seam = validation["gates"]["source_exact_sphere_seam_timing"]
    seam["status"] = "diagnostic_pass"
    seam["absolute_deadline_miss_count"] = 0
    seam["absolute_deadline_miss_count_per_attempt"] = [0, 0]
    validation_path.write_text(json.dumps(validation), encoding="utf-8")

    with pytest.raises(readiness.ReadinessError, match="sphere seam timing"):
        readiness.verify(fixture)


def test_readiness_verification_is_independent_of_checkout_mtime(
    tmp_path: Path,
) -> None:
    fixture = _fixture_root(tmp_path)
    for relative in v3_state.ORCHESTRATION_RELATIVE_PATHS:
        path = fixture / relative
        path.touch()

    report = readiness.verify(fixture)
    assert report["state"] == "pre_live_blocked"


def test_user_confirmation_is_required_while_pre_live_blocked(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    _mutate_v3(
        fixture,
        lambda row: row["execution_readiness"]["operator_trigger"].update(
            {"user_confirmation_required": False}
        ),
    )
    with pytest.raises(readiness.ReadinessError, match="user_confirmation_required"):
        readiness.verify(fixture)


def test_stage_table_cannot_claim_a_different_readiness_state(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    _mutate_v3(
        fixture,
        lambda row: row["execution_readiness"].update(
            {"state": "not_ready", "ready_to_execute": False}
        ),
    )
    with pytest.raises(readiness.ReadinessError, match="readiness state"):
        readiness.verify(fixture)


def test_explicit_v3_readback_requires_matching_fresh_readback_time(
    tmp_path: Path,
) -> None:
    fixture = _fixture_root(tmp_path)
    _mutate_v3(
        fixture,
        lambda row: row["package_delivery"].update(
            {"fresh_controller_sha_at": "2026-07-19T00:24:05+08:00"}
        ),
    )
    with pytest.raises(readiness.ReadinessError, match="fresh controller timestamp"):
        readiness.verify(fixture)


def test_local_triplet_drift_invalidates_package_readiness(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    script = (
        fixture
        / "programs/step5/step5d/step5d_strict_rnn_autotune_v3.script"
    )
    script.write_bytes(script.read_bytes() + b"\n# drift\n")
    with pytest.raises(readiness.ReadinessError, match="local package digest"):
        readiness.verify(fixture)


def test_machine_campaign_binding_cannot_drift(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    promotion_path = fixture / "config/step5/step5d_autotune_v3_live_promotion.json"
    promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
    promotion["machine_campaign_binding"] = "user_token"
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")
    with pytest.raises(readiness.ReadinessError, match="machine_campaign_binding"):
        readiness.verify(fixture)
