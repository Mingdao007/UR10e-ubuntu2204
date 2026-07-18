from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_autotune_v3_artifacts as artifacts  # noqa: E402


ORCHESTRATION_INPUTS = {
    "tools/run_step5d_autotune_campaign.py",
    "tools/step5d_autotune_coordinator.py",
    "tools/step5d_autotune_journal.py",
    "tools/step5d_autotune_store.py",
    "tools/step5d_autotune_live_driver.py",
    "tools/step5d_autotune_batch_plan.py",
    "tools/step5d_autotune_v3/state.py",
    "tools/step5d_autotune_v3/service.py",
    "tools/step5d_autotune_v3/postprocess.py",
    "tools/step5d_autotune_v3/cli.py",
    "scripts/step5d-autotune-v3.sh",
    "config/systemd/step5d-autotune-v3.service",
}


def _fixture_root(tmp_path: Path) -> Path:
    fixture = tmp_path / "experiment"
    relatives = set(artifacts.EXPECTED_SHA256) | ORCHESTRATION_INPUTS | {
        "config/current_stage.json",
        "config/step5_stage_table.json",
        "config/step5d_autotune_v3_test_matrix.json",
    }
    for relative in relatives:
        source = ROOT / relative
        target = fixture / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return fixture


def test_repository_immutable_artifact_bundle_passes() -> None:
    report = artifacts.verify(ROOT)
    assert report["ok"] is True
    assert report["current_stage_id"] == artifacts.V1_STAGE_ID
    assert report["v3_active"] is False
    assert report["execution_readiness"] == "ready_for_hil_authorization"
    assert report["ready_to_execute"] is False
    assert report["acceptance_scope"] == "offline_tooling_and_ursim_hold_only"
    assert report["rollout_authorized"] is False
    assert "config/step5/step5d_autotune_v3_ursim_hold_raw.json" in report["verified_paths"]
    assert "config/step5/step5d_autotune_v3_ursim_hold_result.json" in report["verified_paths"]


def test_triplet_byte_mutation_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    target = fixture / "programs/step5/step5d/step5d_strict_rnn_autotune_v2.script"
    target.write_bytes(target.read_bytes() + b"\n# drift\n")
    with pytest.raises(artifacts.ArtifactVerificationError, match="digest differs"):
        artifacts.verify(fixture)


def test_v3_selector_cannot_become_active_in_offline_bundle(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    stage_table = fixture / "config/step5_stage_table.json"
    text = stage_table.read_text(encoding="utf-8")
    marker = '"id": "step5d_strict_rnn_autotune_v3"'
    start = text.index(marker)
    active = text.index('"active": false', start)
    stage_table.write_text(
        text[:active] + text[active:].replace('"active": false', '"active": true', 1),
        encoding="utf-8",
    )
    with pytest.raises(artifacts.ArtifactVerificationError, match="selector active"):
        artifacts.verify(fixture)


def test_ursim_matrix_cannot_allow_robot_network(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    matrix_path = fixture / "config/step5d_autotune_v3_test_matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["lanes"]["large_ursim"]["robot_network_allowed"] = True
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    with pytest.raises(artifacts.ArtifactVerificationError, match="robot_network_allowed"):
        artifacts.verify(fixture)
