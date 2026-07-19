from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_autotune_v3_artifacts as artifacts  # noqa: E402
from step5d_autotune_v3.state import (  # noqa: E402
    ORCHESTRATION_RELATIVE_PATHS,
    ORCHESTRATION_REPO_RELATIVE_PATHS,
)


ORCHESTRATION_INPUTS = set(ORCHESTRATION_RELATIVE_PATHS)


def _fixture_root(tmp_path: Path) -> Path:
    fixture = tmp_path / "workspace/experiments/tase-contact-reproduction"
    relatives = set(artifacts.BOUND_PATHS) | ORCHESTRATION_INPUTS
    for relative in relatives:
        source = ROOT / relative
        target = fixture / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    repository_fixture = fixture.parents[1]
    repository_source = ROOT.parents[1]
    for relative in ORCHESTRATION_REPO_RELATIVE_PATHS:
        source = repository_source / relative
        target = repository_fixture / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return fixture


def test_repository_immutable_artifact_bundle_passes() -> None:
    report = artifacts.verify(ROOT)
    assert report["ok"] is True
    assert report["current_stage_id"] == artifacts.V3_STAGE_ID
    assert report["v3_active"] is True
    assert report["execution_readiness"] == "pre_live_blocked"
    assert report["ready_to_execute"] is False
    assert report["acceptance_scope"] == "offline_pre_live_only"
    assert report["certification_motion_authorization_required"] is True
    assert report["campaign_authorization_required"] is True
    assert "evidence/step5d_autotune_v3/start_pose_prior_20260719.json" in report["verified_paths"]


def test_triplet_byte_mutation_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    target = fixture / "programs/step5/step5d/step5d_strict_rnn_autotune_v3.script"
    target.write_bytes(target.read_bytes() + b"\n# drift\n")
    with pytest.raises(artifacts.ArtifactVerificationError, match="package digest"):
        artifacts.verify(fixture)


def test_v3_selector_cannot_become_inactive_in_offline_bundle(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    stage_table = fixture / "config/step5_stage_table.json"
    table = json.loads(stage_table.read_text(encoding="utf-8"))
    row = next(
        item
        for item in table["stages"]
        if item.get("id") == artifacts.V3_STAGE_ID
    )
    row["active"] = False
    stage_table.write_text(json.dumps(table), encoding="utf-8")
    with pytest.raises(artifacts.ArtifactVerificationError, match="V3 selector active"):
        artifacts.verify(fixture)


def test_pose_prior_cannot_become_optimizer_objective(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    prior_path = fixture / "evidence/step5d_autotune_v3/start_pose_prior_20260719.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    prior["derivation"]["eligible_as_optimizer_objective"] = True
    prior_path.write_text(json.dumps(prior), encoding="utf-8")
    with pytest.raises(artifacts.ArtifactVerificationError, match="optimizer exclusion"):
        artifacts.verify(fixture)
