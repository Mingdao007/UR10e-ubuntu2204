from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_autotune_v3_artifacts as artifacts  # noqa: E402
from step5d_autotune_v3.identity_layers import (  # noqa: E402
    EVIDENCE_VERIFIER_PATHS,
    ORCHESTRATION_PATHS,
    TICK_SEMANTICS_PATHS,
    TIMING_MEASUREMENT_PATHS,
)
from step5d_autotune_v3.state import (  # noqa: E402
    ORCHESTRATION_RELATIVE_PATHS,
    ORCHESTRATION_REPO_RELATIVE_PATHS,
)


EXPERIMENT_PREFIX = "experiments/tase-contact-reproduction/"
ACTIVE_IDENTITY_PATHS = {
    *TICK_SEMANTICS_PATHS,
    *TIMING_MEASUREMENT_PATHS,
    *ORCHESTRATION_PATHS,
    *EVIDENCE_VERIFIER_PATHS,
}
ACTIVE_EXPERIMENT_RELATIVES = {
    path.removeprefix(EXPERIMENT_PREFIX)
    for path in ACTIVE_IDENTITY_PATHS
    if path.startswith(EXPERIMENT_PREFIX)
}
ACTIVE_REPO_RELATIVES = {
    path for path in ACTIVE_IDENTITY_PATHS if not path.startswith(EXPERIMENT_PREFIX)
}
ORCHESTRATION_INPUTS = set(ORCHESTRATION_RELATIVE_PATHS)


def _fixture_root(tmp_path: Path) -> Path:
    fixture = tmp_path / "workspace/experiments/tase-contact-reproduction"
    relatives = (
        set(artifacts.BOUND_PATHS)
        | ORCHESTRATION_INPUTS
        | ACTIVE_EXPERIMENT_RELATIVES
        | {
            "config/step5_safe_frame.json",
            "config/step5d_liveprep_solver_gate.json",
        }
    )
    for relative in relatives:
        source = ROOT / relative
        target = fixture / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    repository_fixture = fixture.parents[1]
    repository_source = ROOT.parents[1]
    for relative in set(ORCHESTRATION_REPO_RELATIVE_PATHS) | ACTIVE_REPO_RELATIVES:
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
    assert report["certification_motion_authorization_required"] is False
    assert report["campaign_authorization_required"] is False
    assert len(report["tick_semantics_fingerprint"]) == 64
    assert len(report["timing_harness_fingerprint"]) == 64
    assert len(report["deployment_fingerprint"]) == 64
    assert len(report["orchestration_fingerprint"]) == 64
    assert len(report["legacy_control_fingerprint"]) == 64
    assert "evidence/step5d_autotune_v3/start_pose_prior_20260719.json" in report["verified_paths"]


def test_triplet_byte_mutation_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    target = fixture / "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r005.script"
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
    with pytest.raises(artifacts.ArtifactVerificationError, match="v3 active differs"):
        artifacts.verify(fixture)


def test_pose_prior_cannot_become_optimizer_objective(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    prior_path = fixture / "evidence/step5d_autotune_v3/start_pose_prior_20260719.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    prior["derivation"]["eligible_as_optimizer_objective"] = True
    prior_path.write_text(json.dumps(prior), encoding="utf-8")
    with pytest.raises(artifacts.ArtifactVerificationError, match="optimizer exclusion"):
        artifacts.verify(fixture)
