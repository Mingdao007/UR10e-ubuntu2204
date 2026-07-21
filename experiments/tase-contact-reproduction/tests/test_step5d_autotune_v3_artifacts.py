from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_autotune_v3_artifacts as artifacts  # noqa: E402
import build_step5d_autotune_tp_v3 as tp_v3  # noqa: E402
import promote_step5d_r009_atomic_release as promoter  # noqa: E402
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
    generated_r010 = {
        f"programs/step5/step5d/{artifacts.TP_PROGRAM_ID}{suffix}"
        for suffix in (
            ".script",
            ".txt",
            ".urp",
            ".deploy-manifest.json",
            ".numeric-sanity.json",
        )
    }
    relatives = (
        set(artifacts.BOUND_PATHS) - generated_r010
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

    package_dir = fixture / "programs/step5/step5d"
    generated = tp_v3.write_triplet(package_dir, tp_v3.IMMUTABLE_RELEASE_STAMP)
    triplet = generated["sha256"]
    deploy_path = package_dir / f"{artifacts.TP_PROGRAM_ID}.deploy-manifest.json"
    numeric_path = package_dir / f"{artifacts.TP_PROGRAM_ID}.numeric-sanity.json"
    deploy_sha = hashlib.sha256(deploy_path.read_bytes()).hexdigest()
    numeric_sha = hashlib.sha256(numeric_path.read_bytes()).hexdigest()
    readback_path = fixture / "config/step5d_autotune_controller_readback_v3.json"
    readback = promoter._canonical_readback(
        triplet_sha256=triplet,
        tp_fingerprint=deploy_sha,
    )
    readback_path.write_bytes(promoter._pretty(readback))
    readback_sha = hashlib.sha256(readback_path.read_bytes()).hexdigest()

    stage_table_path = fixture / "config/step5_stage_table.json"
    stage_table = promoter._render_stage_table(
        json.loads((ROOT / "config/step5_stage_table.json").read_text(encoding="utf-8")),
        triplet_sha256=triplet,
        deploy_manifest_sha256=deploy_sha,
        numeric_sanity_sha256=numeric_sha,
        readback_sha256=readback_sha,
    )
    stage_table_path.write_bytes(promoter._pretty(stage_table))
    contract_path = fixture / "config/step5/step5d_autotune_v3_control_contract.json"
    contract = promoter._render_contract(
        ROOT,
        json.loads(
            (ROOT / "config/step5/step5d_autotune_v3_control_contract.json").read_text(
                encoding="utf-8"
            )
        ),
        triplet_sha256=triplet,
        deploy_manifest_sha256=deploy_sha,
        numeric_sanity_sha256=numeric_sha,
        readback_sha256=readback_sha,
    )
    contract_path.write_bytes(promoter._pretty(contract))
    return fixture


def test_generated_r010_immutable_artifact_bundle_passes(tmp_path: Path) -> None:
    report = artifacts.verify(_fixture_root(tmp_path))
    assert report["ok"] is True
    assert report["scope"] == "immutable_release_identity_and_artifact_integrity"
    assert report["current_stage_id"] == artifacts.V3_STAGE_ID
    assert report["v3_active"] is True
    assert report["tp_program_id"] == artifacts.TP_PROGRAM_ID
    assert report["tp_protocol_id"] == artifacts.HOST_PROTOCOL_ID
    assert len(report["artifact_set_fingerprint"]) == 64
    assert {
        "execution_readiness",
        "ready_to_execute",
        "acceptance_scope",
        "certification_motion_authorization_required",
        "campaign_authorization_required",
    }.isdisjoint(report)
    assert "evidence/step5d_autotune_v3/start_pose_prior_20260719.json" in report["verified_paths"]


def test_artifact_verifier_has_no_cached_readiness_dependency() -> None:
    source = Path(artifacts.__file__).read_text(encoding="utf-8")
    assert "verify_step5d_autotune_v3_execution_readiness" not in source
    assert "READINESS_EVIDENCE_RELATIVE_PATHS" not in source
    assert not any("live_promotion" in path for path in artifacts.BOUND_PATHS)


def test_triplet_byte_mutation_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    target = fixture / f"programs/step5/step5d/{artifacts.TP_PROGRAM_ID}.script"
    target.write_bytes(target.read_bytes() + b"\n# drift\n")
    with pytest.raises(artifacts.ArtifactVerificationError, match=r"V3 \.script digest"):
        artifacts.verify(fixture)


def _rebind_deploy_fingerprint(fixture: Path, deploy_path: Path) -> None:
    import hashlib

    stage_table = fixture / "config/step5_stage_table.json"
    table = json.loads(stage_table.read_text(encoding="utf-8"))
    row = next(
        item
        for item in table["stages"]
        if item.get("id") == artifacts.V3_STAGE_ID
    )
    row["package_delivery"]["tp_fingerprint"] = hashlib.sha256(
        deploy_path.read_bytes()
    ).hexdigest()
    stage_table.write_text(json.dumps(table), encoding="utf-8")


def test_legacy_deploy_schema_cannot_satisfy_r010_artifact_gate(
    tmp_path: Path,
) -> None:
    fixture = _fixture_root(tmp_path)
    deploy_path = (
        fixture
        / f"programs/step5/step5d/{artifacts.TP_PROGRAM_ID}.deploy-manifest.json"
    )
    deploy = json.loads(deploy_path.read_text(encoding="utf-8"))
    deploy["schema_version"] = 1
    deploy_path.write_text(json.dumps(deploy), encoding="utf-8")
    _rebind_deploy_fingerprint(fixture, deploy_path)

    with pytest.raises(artifacts.ArtifactVerificationError, match="TP deploy schema"):
        artifacts.verify(fixture)


def test_deploy_runtime_identity_tamper_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    deploy_path = (
        fixture
        / f"programs/step5/step5d/{artifacts.TP_PROGRAM_ID}.deploy-manifest.json"
    )
    deploy = json.loads(deploy_path.read_text(encoding="utf-8"))
    deploy["tp_runtime_identity"]["digest_hi"] ^= 1
    deploy_path.write_text(json.dumps(deploy), encoding="utf-8")
    _rebind_deploy_fingerprint(fixture, deploy_path)

    with pytest.raises(
        artifacts.ArtifactVerificationError,
        match="TP runtime identity binding",
    ):
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
    with pytest.raises(
        artifacts.ArtifactVerificationError,
        match="V3 selector active differs",
    ):
        artifacts.verify(fixture)


def test_pose_prior_cannot_become_optimizer_objective(tmp_path: Path) -> None:
    fixture = _fixture_root(tmp_path)
    prior_path = fixture / "evidence/step5d_autotune_v3/start_pose_prior_20260719.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    prior["derivation"]["eligible_as_optimizer_objective"] = True
    prior_path.write_text(json.dumps(prior), encoding="utf-8")
    with pytest.raises(artifacts.ArtifactVerificationError, match="optimizer exclusion"):
        artifacts.verify(fixture)
