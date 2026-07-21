#!/usr/bin/env python3
"""Focused tests for the non-recursive Step5d V3 identity owner."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
RUNTIME_SRC = REPOSITORY_ROOT / "src/ur10e_experiment_runtime"
sys.path.insert(0, str(RUNTIME_SRC))
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.identity_layers import (  # noqa: E402
    BOUNDED_HOLD_TIMING_CONTRACT,
    EVIDENCE_VERIFIER_PATHS,
    IdentityLayerError,
    ORCHESTRATION_PATHS,
    SELECTOR_AND_DOCUMENT_PATHS,
    TICK_SEMANTICS_PATHS,
    TIMING_MEASUREMENT_PATHS,
    canonical_repo_relative_path,
    deployment_fingerprint,
    evidence_verifier_fingerprint,
    evidence_verifier_manifest,
    orchestration_fingerprint,
    release_basis_fingerprint,
    release_basis_manifest,
    release_fingerprint,
    release_manifest,
    runtime_environment_fingerprint,
    source_sha256_manifest,
    tick_semantics_fingerprint,
    tick_semantics_manifest,
    timing_harness_fingerprint,
)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_default_surfaces_use_only_repo_relative_subject_paths() -> None:
    subjects = {
        *TICK_SEMANTICS_PATHS,
        *TIMING_MEASUREMENT_PATHS,
        *ORCHESTRATION_PATHS,
    }
    assert subjects.isdisjoint(SELECTOR_AND_DOCUMENT_PATHS)
    assert subjects.isdisjoint(EVIDENCE_VERIFIER_PATHS)
    assert all(canonical_repo_relative_path(path) == path for path in subjects)
    assert all(not Path(path).is_absolute() for path in subjects)
    assert (
        "experiments/tase-contact-reproduction/tools/step5d_autotune_store.py"
        in ORCHESTRATION_PATHS
    )
    assert (
        "src/ur10e_experiment_runtime/ur10e_experiment_runtime/authorization.py"
        in ORCHESTRATION_PATHS
    )
    assert (
        "experiments/tase-contact-reproduction/tools/"
        "rebuild_step5d_autotune_v3_pre_live_evidence.py"
        in EVIDENCE_VERIFIER_PATHS
    )
    assert TIMING_MEASUREMENT_PATHS == (
        "experiments/tase-contact-reproduction/tools/"
        "run_step5d_v30_remote_timing.py",
        "experiments/tase-contact-reproduction/tools/"
        "build_step5d_v30_remote_timing_bundle.py",
        "experiments/tase-contact-reproduction/tools/step5d_v30_timing.py",
    )


@pytest.mark.parametrize(
    "path",
    ["/tmp/source.py", "../source.py", "a/../source.py", "a\\source.py", "a//b.py"],
)
def test_noncanonical_source_paths_are_rejected(path: str) -> None:
    with pytest.raises(IdentityLayerError):
        canonical_repo_relative_path(path)


def test_source_manifest_is_clone_location_independent(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    _write(left, "src/tick.py", "VALUE = 1\n")
    _write(right, "src/tick.py", "VALUE = 1\n")

    left_manifest = source_sha256_manifest(left, ("src/tick.py",))
    right_manifest = source_sha256_manifest(right, ("src/tick.py",))
    assert left_manifest == right_manifest
    assert set(left_manifest) == {"src/tick.py"}
    assert str(left) not in json.dumps(left_manifest)
    assert str(right) not in json.dumps(right_manifest)


def test_selector_and_operational_metadata_do_not_change_tick_subject(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _write(repository, "src/tick.py", "GAIN = 1.0\n")
    _write(repository, "config/current_stage.json", '{"current":"v1"}\n')
    first = tick_semantics_fingerprint(
        repository,
        source_paths=("src/tick.py",),
        semantic_inputs={
            "gain": 1.0,
            "absolute_path": "/checkout/one",
            "observed_at": "2026-07-20T01:00:00Z",
            "pid": 10,
            "hostname": "host-a",
            "output_root": "/tmp/one",
            "promotion_status": "blocked",
            "artifact_self_sha256": "1" * 64,
            "verifier_source_sha256": "2" * 64,
            "builder_source_sha256": "3" * 64,
            "current": "v1",
            "latest": "old",
        },
    )
    _write(repository, "config/current_stage.json", '{"current":"v3"}\n')
    second = tick_semantics_fingerprint(
        repository,
        source_paths=("src/tick.py",),
        semantic_inputs={
            "gain": 1.0,
            "absolute_path": "/checkout/two",
            "observed_at": "2026-07-21T01:00:00Z",
            "pid": 99,
            "hostname": "host-b",
            "output_root": "/tmp/two",
            "promotion_status": "promoted",
            "artifact_self_sha256": "4" * 64,
            "verifier_source_sha256": "5" * 64,
            "builder_source_sha256": "6" * 64,
            "current": "v3",
            "latest": "new",
        },
    )
    assert first == second

    _write(repository, "src/tick.py", "GAIN = 1.1\n")
    changed = tick_semantics_fingerprint(
        repository,
        source_paths=("src/tick.py",),
        semantic_inputs={"gain": 1.0},
    )
    assert changed != first


def test_external_inputs_use_roles_not_absolute_paths(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _write(repository, "src/tick.py", "VALUE = 1\n")
    manifest = tick_semantics_manifest(
        repository,
        source_paths=("src/tick.py",),
        external_inputs={"ur_description_xacro_sha256": "a" * 64},
    )
    assert manifest["external_inputs"] == {
        "ur_description_xacro_sha256": "a" * 64
    }
    with pytest.raises(IdentityLayerError, match="external input role"):
        tick_semantics_manifest(
            repository,
            source_paths=("src/tick.py",),
            external_inputs={"/opt/ros/model.xacro": "a" * 64},
        )


def test_timing_contract_or_measurement_change_invalidates_harness(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _write(repository, "tools/harness.py", "SAMPLES = 30000\n")
    baseline = timing_harness_fingerprint(
        repository,
        source_paths=("tools/harness.py",),
    )

    changed_contract = copy.deepcopy(BOUNDED_HOLD_TIMING_CONTRACT)
    changed_contract["full_tick"]["p99_max_ms"] = 1.81
    assert timing_harness_fingerprint(
        repository,
        source_paths=("tools/harness.py",),
        timing_contract=changed_contract,
    ) != baseline


def test_verifier_change_does_not_change_orchestration_subject(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _write(repository, "tools/orchestrator.py", "STATE = 1\n")
    _write(repository, "tools/verifier.py", "CHECK = 1\n")
    baseline = orchestration_fingerprint(
        repository,
        source_paths=("tools/orchestrator.py",),
    )
    _write(repository, "tools/verifier.py", "CHECK = 2\n")
    assert orchestration_fingerprint(
        repository,
        source_paths=("tools/orchestrator.py",),
    ) == baseline
    _write(repository, "tools/orchestrator.py", "STATE = 2\n")
    assert orchestration_fingerprint(
        repository,
        source_paths=("tools/orchestrator.py",),
    ) != baseline

    _write(repository, "tools/harness.py", "SAMPLES = 29999\n")
    assert timing_harness_fingerprint(
        repository,
        source_paths=("tools/harness.py",),
    ) != baseline


def test_runtime_environment_ignores_locations_but_not_runtime_semantics() -> None:
    first = runtime_environment_fingerprint(
        {
            "python_version": "3.10.12",
            "python_executable": "/usr/bin/python3",
            "pythonpath": "/checkout/one:/runtime/one",
            "ld_library_path": "/cuda/one",
            "scheduler_policy_name": "SCHED_OTHER",
            "scheduler_priority": 0,
            "nice": 0,
            "cpu_affinity": [11, 13, 14, 15],
            "hostname": "host-a",
            "observed_at": "2026-07-20T01:00:00Z",
        }
    )
    relocated = runtime_environment_fingerprint(
        {
            "python_version": "3.10.12",
            "python_executable": "/opt/python3",
            "pythonpath": "/checkout/two:/runtime/two",
            "ld_library_path": "/cuda/two",
            "scheduler_policy_name": "SCHED_OTHER",
            "scheduler_priority": 0,
            "nice": 0,
            "cpu_affinity": [11, 13, 14, 15],
            "hostname": "host-b",
            "observed_at": "2026-07-21T01:00:00Z",
        }
    )
    assert first == relocated
    assert runtime_environment_fingerprint(
        {
            "python_version": "3.10.12",
            "scheduler_policy_name": "SCHED_OTHER",
            "scheduler_priority": 0,
            "nice": 1,
            "cpu_affinity": [11, 13, 14, 15],
        }
    ) != first


def test_deployment_ignores_readback_publication_metadata() -> None:
    triplet = {".script": "1" * 64, ".txt": "2" * 64, ".urp": "3" * 64}
    readback_triplet = {
        ".script": "7" * 64,
        ".txt": "8" * 64,
        ".urp": "9" * 64,
    }
    semantic_readback = {
        "schema": "step5d.autotune.controller-readback/v3",
        "verified": True,
        "program": "step5d_strict_rnn_autotune_v3_r006",
        "control_profile_id": "step5d_strict_rnn_autotune_v1",
        "controller_target": (
            "/programs/andyl/kunwei/step5/"
            "step5d_strict_rnn_autotune_v3_r006.urp"
        ),
        "triplet_sha256": readback_triplet,
        "tp_fingerprint": "4" * 64,
    }
    first = deployment_fingerprint(
        triplet_sha256=triplet,
        controller_readback_identity={
            **semantic_readback,
            "fresh_controller_checked_at": "2026-07-20T01:00:00Z",
            "readback_path": "/checkout/one/readback.json",
            "artifact_self_sha256": "5" * 64,
            "source_stamp": "first-publication",
            "safety_boundary": ["v1 remains current"],
        },
    )
    republished = deployment_fingerprint(
        triplet_sha256=triplet,
        controller_readback_identity={
            **semantic_readback,
            "fresh_controller_checked_at": "2026-07-21T01:00:00Z",
            "readback_path": "/checkout/two/readback.json",
            "artifact_self_sha256": "6" * 64,
            "source_stamp": "second-publication",
            "safety_boundary": ["v3 is selected"],
        },
    )
    assert first == republished
    with pytest.raises(IdentityLayerError, match="readback program"):
        deployment_fingerprint(
            triplet_sha256=triplet,
            controller_readback_identity={
                **semantic_readback,
                "program": "different_program",
            },
        )


def test_release_basis_and_final_release_are_non_recursive(tmp_path: Path) -> None:
    components = {
        "tick_semantics_fingerprint": "1" * 64,
        "timing_harness_fingerprint": "2" * 64,
        "runtime_environment_fingerprint": "3" * 64,
        "deployment_fingerprint": "4" * 64,
        "orchestration_fingerprint": "5" * 64,
        "plant_epoch": 1,
    }
    basis_document = release_basis_manifest(**components)
    basis = release_basis_fingerprint(**components)
    assert set(basis_document) == {
        "schema",
        "release_stage_id",
        "plant_epoch",
        "components",
    }
    assert "release_fingerprint" not in json.dumps(basis_document)
    assert "evidence_verifier" not in json.dumps(basis_document)

    repository = tmp_path / "repo"
    repository.mkdir()
    _write(repository, "verify.py", "VERSION = 1\n")
    verifier_before = evidence_verifier_fingerprint(
        repository, source_paths=("verify.py",)
    )
    _write(repository, "verify.py", "VERSION = 2\n")
    verifier_after = evidence_verifier_fingerprint(
        repository, source_paths=("verify.py",)
    )
    assert verifier_before != verifier_after
    assert release_basis_fingerprint(**components) == basis

    final_document = release_manifest(
        release_basis_fingerprint=basis,
        stopping_bound_fingerprint="6" * 64,
        return_evidence_fingerprint="7" * 64,
    )
    final = release_fingerprint(
        release_basis_fingerprint=basis,
        stopping_bound_fingerprint="6" * 64,
        return_evidence_fingerprint="7" * 64,
    )
    assert "release_fingerprint" not in final_document
    assert final == release_fingerprint(
        release_basis_fingerprint=basis,
        stopping_bound_fingerprint="6" * 64,
        return_evidence_fingerprint="7" * 64,
    )
    assert final != release_fingerprint(
        release_basis_fingerprint=basis,
        stopping_bound_fingerprint="8" * 64,
        return_evidence_fingerprint="7" * 64,
    )


def test_default_manifests_are_buildable_without_importing_live_code() -> None:
    tick = tick_semantics_manifest(
        REPOSITORY_ROOT,
        external_inputs={"ur_description_xacro_sha256": "a" * 64},
    )
    verifier = evidence_verifier_manifest(REPOSITORY_ROOT)
    assert set(tick["sources"]) == set(TICK_SEMANTICS_PATHS)
    assert set(verifier["sources"]) == set(EVIDENCE_VERIFIER_PATHS)
    assert verifier["provenance_only"] is True
