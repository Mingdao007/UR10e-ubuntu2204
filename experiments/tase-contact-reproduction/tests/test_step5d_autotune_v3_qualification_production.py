#!/usr/bin/env python3
"""Production/medium localhost vertical slice for Step5d V3 qualification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
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
    QualificationError,
    run_endpoint_qualification,
    validate_qualification_result,
)
import step5d_autotune_v3.qualification as qualification  # noqa: E402
from step5d_autotune_v3.release_identity import load_current_release  # noqa: E402
from step5d_autotune_v3.runtime_functional_gates import (  # noqa: E402
    RuntimeFunctionalGateError,
)
import build_step5d_autotune_tp_v3 as builder  # noqa: E402
import promote_step5d_r009_atomic_release as promotion  # noqa: E402


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    os.link(source, destination)


def _qualified_release_fixture(
    tmp_path: Path, *, restore_deployed_current: bool = False
) -> tuple[Path, object]:
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
    transaction_id = "c" * 32
    receipt_dir = experiment / "runs/qualification-release/fresh-get"
    receipt_dir.mkdir(parents=True)
    triplet = generated["sha256"]
    for extension in promotion.EXTENSIONS:
        source = artifact_dir / f"{promotion.PROGRAM}{extension}"
        (receipt_dir / source.name).write_bytes(source.read_bytes())
    receipt = {
        "status": "controller read-back verified",
        "controller": "qualification@127.0.0.1",
        "target_dir": promotion.TARGET_DIR,
        "validation": {
            "stamp": builder.IMMUTABLE_RELEASE_STAMP,
            "program": promotion.PROGRAM,
            "target_dir": promotion.TARGET_DIR,
            "script_node_path": (
                f"{promotion.TARGET_DIR}/{promotion.PROGRAM}.script"
            ),
            "script_sha256": triplet[".script"],
            "txt_sha256": triplet[".txt"],
            "urp_sha256": triplet[".urp"],
        },
        "sha256": {
            role: dict(triplet) for role in ("local", "controller", "readback")
        },
        "delivery_mode": "full_upload_readback",
        "fresh_controller_sha_verified": True,
        "fresh_controller_checked_at": datetime.now(timezone.utc).isoformat(),
        "readback_source": "fresh_controller_get",
        "upload_transaction_id": transaction_id,
    }
    receipt_path = receipt_dir / "manifest.json"
    receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    current_pointer = experiment / "config/step5d/current.json"
    deployed_current_bytes = current_pointer.read_bytes()
    promotion.promote(
        experiment,
        receipt_path,
        artifact_dir,
        expected_transaction_id=transaction_id,
        expected_manifest_sha256=receipt_sha256,
    )
    candidate = load_current_release(experiment)
    if restore_deployed_current:
        current_pointer.write_bytes(deployed_current_bytes)
    return experiment, candidate


def test_endpoint_qualification_never_rebuilds_missing_gpu_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment, _candidate = _qualified_release_fixture(tmp_path)
    canonical = experiment / "scripts/step5d-autotune-v3.sh"
    environment = dict(os.environ)
    environment[CANONICAL_LAUNCH_ENV] = str(canonical)

    def missing_gate(**_kwargs: object) -> None:
        raise RuntimeFunctionalGateError("stale test evidence")

    monkeypatch.setattr(qualification, "load_gpu_functional_attestation", missing_gate)
    with pytest.raises(
        QualificationError,
        match="GPU_FUNCTIONAL_GATE_MISSING: stale test evidence",
    ):
        run_endpoint_qualification(
            experiment,
            experiment / "runs/qualification-output",
            environment=environment,
        )


def test_run_endpoint_qualification_completes_the_production_tree(tmp_path: Path) -> None:
    experiment, _candidate = _qualified_release_fixture(tmp_path)
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
        "bridge",
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
    )

    evidence_path = Path(evidence["path"])
    assert evidence_path.is_file() and not evidence_path.is_symlink()
    assert hashlib.sha256(evidence_path.read_bytes()).hexdigest() == evidence["sha256"]
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["ok"] is True
    endpoint_path = Path(payload["endpoint_evidence"]["path"])
    endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
    assert endpoint["counters"]["rtde"]["trials_completed"] >= 1
    assert endpoint["counters"]["rtde"]["arm_acknowledgements"] >= 2


def test_candidate_qualification_does_not_mutate_or_require_deployed_current(
    tmp_path: Path,
) -> None:
    experiment, candidate = _qualified_release_fixture(
        tmp_path, restore_deployed_current=True
    )
    current_path = experiment / "config/step5d/current.json"
    deployed_current_bytes = current_path.read_bytes()
    assert json.loads(deployed_current_bytes)["manifest_sha256"] != (
        candidate.manifest_sha256
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
