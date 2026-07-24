#!/usr/bin/env python3
"""Production/medium localhost vertical slice for Step5d V3 qualification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
RUNTIME_SOURCE = REPOSITORY_ROOT / "src/ur10e_experiment_runtime"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(RUNTIME_SOURCE))

from step5d_autotune_v3.qualification import (  # noqa: E402
    CANONICAL_LAUNCH_ENV,
    run_endpoint_qualification,
    validate_qualification_result,
)
from step5d_autotune_v3.release_identity import (  # noqa: E402
    load_local_release_candidate,
)
import build_step5d_autotune_tp_v3 as builder  # noqa: E402
import promote_step5d_r009_atomic_release as promotion  # noqa: E402


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    os.link(source, destination)


def _qualified_release_fixture(tmp_path: Path) -> tuple[Path, object]:
    repository = tmp_path / "workspace"
    experiment = repository / "experiments/tase-contact-reproduction"
    shutil.copytree(
        ROOT,
        experiment,
        copy_function=os.link,
        ignore=shutil.ignore_patterns("runs", ".pytest_cache", "__pycache__", "*.pyc"),
    )
    for relative in promotion.REPOSITORY_SOURCE_INPUTS:
        _copy_file(REPOSITORY_ROOT / relative, repository / relative)
    _copy_file(
        REPOSITORY_ROOT / "src/ur10e_bringup/config/ur10e_calibration.yaml",
        repository / "src/ur10e_bringup/config/ur10e_calibration.yaml",
    )
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "qualification@localhost"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Step5d qualification"],
        cwd=repository,
        check=True,
    )
    assert not (repository / ".git/ur10e-artifacts").exists()
    assert not (repository / "experiments/archive").exists()
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "hermetic qualification fixture"],
        cwd=repository,
        check=True,
    )

    artifact_dir = experiment / "runs/qualification-release/artifacts"
    artifact_dir.mkdir(parents=True)
    generated = builder.write_triplet(
        artifact_dir,
        builder.IMMUTABLE_RELEASE_STAMP,
    )
    current_pointer = experiment / "config/step5d/current.json"
    deployed_current_bytes = current_pointer.read_bytes()
    compatibility_paths = tuple(
        experiment / relative for relative in promotion.STATIC_PROJECTIONS
    )
    deployed_compatibility_bytes = {
        path: path.read_bytes() for path in compatibility_paths
    }
    descriptor = promotion.stage_local_candidate(experiment, artifact_dir)
    descriptor_path = (
        experiment / "runs/qualification-release/local-release-candidate.json"
    )
    descriptor_path.write_text(
        json.dumps(descriptor, sort_keys=True) + "\n", encoding="utf-8"
    )
    candidate, _descriptor = load_local_release_candidate(
        experiment, descriptor_path
    )
    assert generated["program"] == promotion.PROGRAM
    assert current_pointer.read_bytes() == deployed_current_bytes
    assert all(
        path.read_bytes() == deployed_compatibility_bytes[path]
        for path in compatibility_paths
    )
    return experiment, candidate


def test_endpoint_qualification_is_independent_from_optimizer_gpu_authority() -> None:
    source = (
        ROOT / "tools/step5d_autotune_v3/qualification.py"
    ).read_text(encoding="utf-8")

    assert "load_gpu_functional_attestation" not in source
    assert "gpu_functional_evidence" not in source


def test_run_endpoint_qualification_completes_the_production_tree(tmp_path: Path) -> None:
    experiment, candidate = _qualified_release_fixture(tmp_path)
    canonical = experiment / "scripts/step5d-autotune-v3.sh"
    environment = dict(os.environ)
    environment[CANONICAL_LAUNCH_ENV] = str(canonical)
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(experiment / "tools"),
            str(experiment.parents[1] / "src/ur10e_experiment_runtime"),
            environment.get("PYTHONPATH", ""),
        )
    ).rstrip(os.pathsep)
    payload, evidence = run_endpoint_qualification(
        experiment,
        experiment / "runs/qualification-output",
        environment=environment,
        release_identity=candidate,
    )

    assert payload["ok"] is True
    assert payload["remaining_integration_seam"] is None
    assert [event["phase"] for event in payload["events"]] == [
        "STARTED",
        "BRIDGE_READY",
        "WAITING_FOR_PLAY",
        "PLAY_OBSERVED",
        "FIRST_ARM_ACK",
        "TRIAL_COMPLETE",
        "NEXT_ARM_ACK",
        "QUALIFIED",
    ]
    binding = payload["binding"]
    processes = {
        process["role"]: process
        for process in binding["process_tree"]["processes"]
    }
    assert set(processes) == {
        "canonical_launcher",
        "launcher_supervisor",
        "bridge_wrapper",
        "campaign_runner",
    }
    assert processes["launcher_supervisor"]["ppid"] == processes[
        "canonical_launcher"
    ]["pid"]
    assert processes["bridge_wrapper"]["ppid"] == processes[
        "launcher_supervisor"
    ]["pid"]
    assert processes["campaign_runner"]["ppid"] == processes[
        "launcher_supervisor"
    ]["pid"]
    shell_result = payload["canonical_shell_result"]
    assert shell_result["returncode"] == 0
    assert shell_result["pid"] == processes["canonical_launcher"]["pid"]
    assert processes["canonical_launcher"]["argv"][-10:] == [
        str(canonical),
        "bridge-live",
        "--output-root",
        str(Path(shell_result["contract_ref"]["path"]).parent / "live"),
        "--campaign-root",
        str(Path(shell_result["contract_ref"]["path"]).parent / "campaign"),
        "--ready-timeout-s",
        "60.0",
        "--play-timeout-s",
        "30.0",
    ]
    validate_qualification_result(
        payload,
        experiment_root=experiment,
        manifest_sha256=binding["manifest_sha256"],
        source_fingerprint=binding["source"]["fingerprint"],
        launcher_sha256=binding["launcher"]["sha256"],
        release_identity=candidate,
    )

    evidence_path = Path(evidence["path"])
    assert evidence_path.is_file() and not evidence_path.is_symlink()
    assert hashlib.sha256(evidence_path.read_bytes()).hexdigest() == evidence["sha256"]
    certificate = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert certificate["schema"] == "step5d.autotune-v3/release-certificate-v1"
    qualification_reference = certificate["qualification_evidence"]
    qualification_path = (
        experiment / "runs/qualification-output" / qualification_reference["path"]
    )
    assert hashlib.sha256(qualification_path.read_bytes()).hexdigest() == (
        qualification_reference["sha256"]
    )
    assert json.loads(qualification_path.read_text(encoding="utf-8"))["ok"] is True
    endpoint_path = Path(payload["endpoint_evidence"]["path"])
    endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
    assert endpoint["counters"]["rtde"]["trials_completed"] >= 1
    assert endpoint["counters"]["rtde"]["arm_acknowledgements"] >= 2


def test_candidate_qualification_does_not_mutate_or_require_deployed_current(
    tmp_path: Path,
) -> None:
    experiment, candidate = _qualified_release_fixture(tmp_path)
    current_path = experiment / "config/step5d/current.json"
    deployed_current_bytes = current_path.read_bytes()
    assert json.loads(deployed_current_bytes)["manifest_sha256"] != (
        candidate.manifest_sha256
    )
    contract_relative = "config/step5/step5d_autotune_v3_control_contract.json"
    root_contract = experiment / contract_relative
    candidate_contract = (
        experiment / candidate.manifest_path
    ).parent / contract_relative
    assert hashlib.sha256(root_contract.read_bytes()).hexdigest() != (
        candidate.source_fingerprints[contract_relative]
    )
    assert hashlib.sha256(candidate_contract.read_bytes()).hexdigest() == (
        candidate.source_fingerprints[contract_relative]
    )
    canonical = experiment / "scripts/step5d-autotune-v3.sh"
    environment = dict(os.environ)
    environment[CANONICAL_LAUNCH_ENV] = str(canonical)
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(experiment / "tools"),
            str(experiment.parents[1] / "src/ur10e_experiment_runtime"),
            environment.get("PYTHONPATH", ""),
        )
    ).rstrip(os.pathsep)

    payload, _evidence = run_endpoint_qualification(
        experiment,
        experiment / "runs/qualification-output",
        environment=environment,
        release_identity=candidate,
    )

    assert payload["ok"] is True
    assert current_path.read_bytes() == deployed_current_bytes
