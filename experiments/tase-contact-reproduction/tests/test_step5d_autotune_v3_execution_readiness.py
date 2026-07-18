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
    "config/step5d_autotune_controller_readback_v2.json",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v2.deploy-manifest.json",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v2.script",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v2.txt",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v2.urp",
    "tools/step5d_autotune_v3/service.py",
    "tools/step5d_autotune_v3/state.py",
    "tools/step5d_autotune_v3/postprocess.py",
    "tools/step5d_autotune_v3/cli.py",
    "tools/run_step5d_autotune_campaign.py",
    "tools/step5d_autotune_coordinator.py",
    "tools/step5d_autotune_journal.py",
    "tools/step5d_autotune_store.py",
    "tools/step5d_autotune_live_driver.py",
    "tools/step5d_autotune_batch_plan.py",
    "scripts/step5d-autotune-v3.sh",
    "config/systemd/step5d-autotune-v3.service",
    "config/step5/step5d_autotune_v3_control_contract.json",
}


def _fixture_root(tmp_path: Path) -> Path:
    fixture = tmp_path / "experiment"
    for relative in RELATIVES:
        source = ROOT / relative
        target = fixture / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
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
    assert report["state"] == "ready_for_hil_authorization"
    assert report["public_success_signal"] == "ready_for_hil_authorization"
    assert report["package_delivery"] == (
        "controller_readback_verified_content_addressed_reuse"
    )
    assert report["ready_to_execute"] is False
    assert report["current_stage_id"] == readiness.V1_STAGE_ID
    assert report["next_owner"] == "ur10e-live-bench"


def test_historical_v1_authorization_cannot_be_reused(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    _mutate_v3(
        fixture,
        lambda row: row["execution_readiness"]["authorization"].update(
            {
                "status": "authorized",
                "source": "resolver_current_stage",
                "historical_live_authorization_reused": True,
            }
        ),
    )
    with pytest.raises(readiness.ReadinessError, match="authorization status"):
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


def test_content_addressed_reuse_requires_matching_fresh_readback_time(
    tmp_path: Path,
) -> None:
    fixture = _fixture_root(tmp_path)
    _mutate_v3(
        fixture,
        lambda row: row["package_delivery"]["content_addressed_reuse"].update(
            {"fresh_controller_sha_at": "2026-07-17T20:03:47+08:00"}
        ),
    )
    with pytest.raises(readiness.ReadinessError, match="fresh controller timestamp"):
        readiness.verify(fixture)


def test_local_triplet_drift_invalidates_package_readiness(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    script = (
        fixture
        / "programs/step5/step5d/step5d_strict_rnn_autotune_v2.script"
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
