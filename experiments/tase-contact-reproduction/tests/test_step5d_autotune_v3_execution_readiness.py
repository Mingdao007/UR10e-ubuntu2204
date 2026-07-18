from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_autotune_v3_execution_readiness as readiness  # noqa: E402


RELATIVES = {
    "config/current_stage.json",
    "config/step5_stage_table.json",
    "config/step5/step5d_autotune_v3_offline_validation.json",
    "config/step5/step5d_autotune_v3_live_promotion.json",
    "evidence/step5d_autotune_v3/hil_acceptance.json",
    "evidence/step5d_autotune_v3/hil_acceptance_20260719_fe6e119f.json",
    "config/step5d_autotune_controller_readback_v3.json",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.deploy-manifest.json",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.script",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.txt",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.urp",
    "tools/step5d_autotune_v3/service.py",
    "tools/step5d_autotune_v3/state.py",
    "tools/step5d_autotune_v3/postprocess.py",
    "tools/step5d_autotune_v3/cli.py",
    "tools/step5d_autotune_v3/launcher.py",
    "tools/step5d_autotune_v3/runtime_calibration.py",
    "tools/step5d_autotune_v3/runtime_profile.py",
    "tools/run_step5d_autotune_v3_bridge.py",
    "tools/run_step5d_autotune_v3_hil_hold.py",
    "tools/run_step5d_autotune_v3_live.py",
    "tools/preflight_step5d_autotune_v3.py",
    "tools/verify_step5d_autotune_v3_hil_authorization.py",
    "tools/verify_step5d_autotune_v3_execution_readiness.py",
    "tools/promote_step5d_autotune_v3_hil.py",
    "tools/run_step5d_autotune_campaign.py",
    "tools/step5d_autotune_coordinator.py",
    "tools/step5d_autotune_journal.py",
    "tools/step5d_autotune_store.py",
    "tools/step5d_autotune_live_driver.py",
    "tools/step5d_autotune_batch_plan.py",
    "scripts/step5d-autotune-v3.sh",
    "scripts/step5d-autotune-v3-hil-hold.sh",
    "config/systemd/step5d-autotune-v3.service",
    "config/step5/step5d_autotune_v3_control_contract.json",
    "config/step5/step5d_autotune_v3_launch_profile.json",
    "config/step5d/manifests/step5d_strict_rnn_autotune_v3/runtime_calibration.json",
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
    assert report["state"] == "ready_for_hil_full_bridge_hold"
    assert report["public_success_signal"] == "ready_for_hil_full_bridge_hold"
    assert report["package_delivery"] == (
        "controller_readback_verified_explicit_v3"
    )
    assert report["ready_to_execute"] is False
    assert report["current_stage_id"] == readiness.V1_STAGE_ID
    assert report["next_owner"] == "ur10e-live-bench"
    assert report["canonical_gate"] == [
        "python3",
        "tools/verify_step5d_autotune_v3_hil_authorization.py",
        "--json",
    ]


def test_repository_live_signal_requires_verified_nonphysical_hil_carryforward() -> None:
    report = readiness.verify(ROOT, require_live=True)
    assert report["ok"] is True
    assert report["state"] == "ready_for_v3_live_continuous_campaign"
    assert report["ready_to_execute"] is True


def test_hil_carryforward_changed_path_attestation_is_fail_closed(
    tmp_path: Path,
) -> None:
    fixture = _fixture_root(tmp_path)
    evidence_path = fixture / "evidence/step5d_autotune_v3/hil_acceptance.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["carryforward"]["changed_orchestration_paths"] = []
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    promotion_path = fixture / "config/step5/step5d_autotune_v3_live_promotion.json"
    promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
    promotion["hil_acceptance"]["sha256"] = hashlib.sha256(
        evidence_path.read_bytes()
    ).hexdigest()
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")

    with pytest.raises(readiness.ReadinessError, match="changed paths"):
        readiness.verify(fixture, require_live=True)


def test_user_confirmation_cannot_be_reintroduced(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    _mutate_v3(
        fixture,
        lambda row: row["execution_readiness"]["operator_trigger"].update(
            {"user_confirmation_required": True}
        ),
    )
    with pytest.raises(readiness.ReadinessError, match="user confirmation"):
        readiness.verify(fixture)


def test_offline_success_cannot_claim_ready_to_execute(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    _mutate_v3(
        fixture,
        lambda row: row["execution_readiness"].update(
            {"state": "ready_to_execute", "ready_to_execute": True}
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


def test_offline_live_start_blocker_cannot_disappear(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    service = fixture / "tools/step5d_autotune_v3/service.py"
    service.write_text(
        service.read_text(encoding="utf-8").replace(
            'OFFLINE_BLOCKER = "offline_only_live_start_disabled"',
            'OFFLINE_BLOCKER = "live_start_enabled"',
        ),
        encoding="utf-8",
    )
    validation_path = (
        fixture / "config/step5/step5d_autotune_v3_offline_validation.json"
    )
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["identity"]["orchestration_fingerprint"] = (
        readiness.orchestration_fingerprint(fixture)
    )
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    validation_sha = hashlib.sha256(validation_path.read_bytes()).hexdigest()
    stage_path = fixture / "config/step5_stage_table.json"
    stage = json.loads(stage_path.read_text(encoding="utf-8"))
    row = next(
        item
        for item in stage["stages"]
        if item.get("id") == readiness.V3_STAGE_ID
    )
    row["offline_validation"]["report_sha256"] = validation_sha
    stage_path.write_text(json.dumps(stage), encoding="utf-8")
    with pytest.raises(readiness.ReadinessError, match="offline live-start blocker"):
        readiness.verify(fixture)
