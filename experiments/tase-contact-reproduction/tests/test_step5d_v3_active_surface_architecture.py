from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
RUNTIME_SOURCE = REPOSITORY_ROOT / "src/ur10e_experiment_runtime"
ACTIVE = ROOT / "config/step5d/v3_active_surface.json"
MATRIX = ROOT / "config/step5d_autotune_v3_test_matrix.json"


def _compatibility_fixture(tmp_path: Path, *, revision: int) -> tuple[Path, Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    adapter = scripts / "step5d-autotune-live.sh"
    shutil.copy2(ROOT / "scripts/step5d-autotune-live.sh", adapter)
    canonical = scripts / "step5d-autotune-v3.sh"
    canonical.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$@\" > \"${STEP5D_TEST_ARGV:?}\"\n",
        encoding="utf-8",
    )
    canonical.chmod(0o755)

    program_id = f"step5d_strict_rnn_autotune_v3_r{revision:03d}"
    manifest = {
        "schema": "step5d.autotune-v3/release-manifest-v3",
        "identity": {"program_id": program_id},
    }
    encoded = json.dumps(manifest, sort_keys=True).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    manifest_path = tmp_path / f"config/step5d/releases/{digest}/manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(encoded)
    pointer = {
        "manifest_path": manifest_path.relative_to(tmp_path).as_posix(),
        "manifest_sha256": digest,
    }
    pointer_path = tmp_path / "config/step5d/current.json"
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    return adapter, tmp_path / "argv.txt"


def test_active_surface_has_one_public_live_entrypoint_and_internal_workers() -> None:
    active = json.loads(ACTIVE.read_text(encoding="utf-8"))

    assert active["schema"] == "step5d.autotune-v3/active-surface-v3"
    assert active["tp_program_id"] == "step5d_strict_rnn_autotune_v3_r010"
    assert active["release_truth"]["manifest_schema"] == (
        "step5d.autotune-v3/release-manifest-v3"
    )
    assert active["entrypoints"]["public_live"] == (
        "scripts/step5d-autotune-v3.sh bridge"
    )
    assert active["entrypoints"]["public_status"] == (
        "scripts/step5d-autotune-v3.sh status --json"
    )
    assert set(active["entrypoints"]["internal_workers"]) == {
        "tools/run_step5d_autotune_v3_live.py",
        "tools/run_step5d_autotune_v3_bridge.py",
        "tools/run_step5d_autotune_campaign.py",
        "tools/run_step5d_autotune_v3_qualification.py",
    }
    assert all(
        not value.endswith(".py")
        for name, value in active["entrypoints"].items()
        if name != "internal_workers"
    )


def test_active_surface_contains_no_cached_dynamic_readiness() -> None:
    active = json.loads(ACTIVE.read_text(encoding="utf-8"))
    forbidden = set(active["release_truth"]["dynamic_readiness_fields_forbidden"])

    assert forbidden.isdisjoint(active)
    assert active["tp_program_disposition"] == (
        "immutable_identity_only_readiness_is_observed"
    )
    assert active["release_truth"]["observed_attestation_location"] == (
        "campaign_root/governance/evidence"
    )


def test_every_runtime_module_is_explicitly_active_or_historical() -> None:
    active = json.loads(ACTIVE.read_text(encoding="utf-8"))
    active_paths = set(active["active_orchestration_paths"])
    historical_paths = set(active["historical_only"])
    runtime_paths = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tools/step5d_autotune_v3").glob("*.py")
        if path.is_file() and not path.is_symlink()
    }

    assert active_paths.isdisjoint(historical_paths)
    assert runtime_paths <= active_paths | historical_paths
    assert {
        "tools/step5d_autotune_v3/delivery_observation.py",
        "tools/step5d_autotune_v3/rtde_client.py",
        "tools/step5d_autotune_v3/runtime_environment.py",
        "tools/step5d_autotune_v3/source_closure.py",
    } <= active_paths


def test_stale_release_claims_are_historical_not_active() -> None:
    active = json.loads(ACTIVE.read_text(encoding="utf-8"))
    claims = active["release_claims"]

    assert claims["step5d_strict_rnn_autotune_v3_r010"].startswith("governed_identity")
    for revision in (4, 6, 8, 9):
        assert claims[
            f"step5d_strict_rnn_autotune_v3_r{revision:03d}"
        ].startswith("historical_")
    assert all("r010" in path for path in active["deployment_paths"])


def test_only_one_compatibility_adapter_and_retired_stubs_have_no_live_path() -> None:
    active = json.loads(ACTIVE.read_text(encoding="utf-8"))
    adapter = active["compatibility_adapter"]

    assert adapter == {
        "path": "scripts/step5d-autotune-live.sh",
        "delegates_to": "scripts/step5d-autotune-v3.sh",
        "allowed_subcommands": ["bridge"],
        "last_supported_tp_revision": 10,
        "after_cutoff": "deterministic_exit_64",
        "writes_before_exec": False,
    }
    for relative in active["retired_entrypoints"]:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "exit 64" in source
        assert "bridge-line-operator.sh" not in source
        assert "run_step5d_autotune" not in source


def test_compatibility_adapter_preserves_bridge_argv_through_r010(
    tmp_path: Path,
) -> None:
    adapter, argv_path = _compatibility_fixture(tmp_path, revision=10)
    environment = {**os.environ, "STEP5D_TEST_ARGV": str(argv_path)}

    result = subprocess.run(
        [str(adapter), "bridge", "--output-root", "/tmp/compat-output"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert argv_path.read_text(encoding="utf-8").splitlines() == [
        "bridge",
        "--output-root",
        "/tmp/compat-output",
    ]


def test_compatibility_adapter_fails_deterministically_after_r010(
    tmp_path: Path,
) -> None:
    adapter, argv_path = _compatibility_fixture(tmp_path, revision=11)
    environment = {**os.environ, "STEP5D_TEST_ARGV": str(argv_path)}

    result = subprocess.run(
        [str(adapter), "bridge"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert "cutoff passed at r010" in result.stderr
    assert not argv_path.exists()


def test_compatibility_adapter_rejects_implicit_or_non_bridge_routes(
    tmp_path: Path,
) -> None:
    adapter, argv_path = _compatibility_fixture(tmp_path, revision=10)
    environment = {**os.environ, "STEP5D_TEST_ARGV": str(argv_path)}

    for arguments in ([], ["status"], ["qualify"]):
        result = subprocess.run(
            [str(adapter), *arguments],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 64
        assert "V1 launcher is retired" in result.stderr
    assert not argv_path.exists()


def test_systemd_consumer_uses_canonical_shell_only() -> None:
    service = (ROOT / "config/systemd/step5d-autotune-v3.service").read_text(
        encoding="utf-8"
    )

    assert "ExecStart=%h/.local/bin/step5d-autotune-v3.sh --_service" in service
    assert "run_step5d_autotune_v3_live.py" not in service
    assert "run_step5d_autotune_v3_bridge.py" not in service


def test_owner_surfaces_do_not_publish_internal_workers() -> None:
    active = json.loads(ACTIVE.read_text(encoding="utf-8"))
    internal_workers = set(active["entrypoints"]["internal_workers"])
    owner_surfaces = (
        ROOT / "STEP5_FLOW.md",
        ROOT / "config/step5_stage_table.json",
        ROOT / "config/systemd/step5d-autotune-v3.service",
    )

    for surface in owner_surfaces:
        source = surface.read_text(encoding="utf-8")
        exposed = {worker for worker in internal_workers if worker in source}
        assert not exposed, f"{surface}: {sorted(exposed)}"


def test_internal_campaign_runner_refuses_direct_v3_execution() -> None:
    environment = {
        **os.environ,
        "STEP5D_V3_CUDA_BOOTSTRAPPED": "1",
    }
    environment.pop("STEP5D_V3_CANONICAL_LAUNCHER", None)
    environment.pop("STEP5D_V3_SUPERVISOR_PID", None)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/run_step5d_autotune_campaign.py"),
            "--v3-runtime-root",
            "/tmp/direct-v3-runner",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert "step5d-autotune-v3.sh bridge" in result.stderr


def test_internal_tp_transaction_refuses_direct_execution() -> None:
    environment = dict(os.environ)
    environment.pop("STEP5D_V3_CANONICAL_LAUNCHER", None)
    environment.pop("STEP5D_V3_SHELL_PID", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        path
        for path in (
            str(ROOT / "tools"),
            str(RUNTIME_SOURCE),
            environment.get("PYTHONPATH", ""),
        )
        if path
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/run_step5d_autotune_v3_tp_transaction.py"),
            "--artifact-dir",
            "/tmp/direct-v3-delivery",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert "step5d-autotune-v3.sh bridge" in result.stderr


def test_authoritative_matrix_classifies_stale_release_files() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    gate = matrix["authoritative_bridge_gate"]

    assert gate["current_tp_program_id"] == "step5d_strict_rnn_autotune_v3_r010"
    assert gate["unclassified_failure_policy"] == "block"
    assert gate["canonical_launcher"] == "scripts/step5d-autotune-v3.sh bridge"
    assert set(gate["classified_test_files"]) == {"active", "obsolete", "unrelated"}
    obsolete = gate["classified_test_files"]["obsolete"]
    assert "tests/test_step5d_v3_r004_release_candidate.py" in obsolete
    assert "tests/test_step5d_r006_production_chain.py" in obsolete
    assert "tests/test_step5d_r008_production_chain.py" in obsolete
    assert gate["active_legacy_filename_reasons"] == {
        "tests/test_step5d_r008_rolling_policy.py": (
            "current rolling policy regression retained under its historical filename"
        ),
        "tests/test_step5d_r009_release_core.py": (
            "r010 release-manifest-v3 regression retained under its compatibility filename"
        ),
    }

    active_tests = set(gate["classified_test_files"]["active"])
    assert {
        "tests/test_step5d_autotune_v3_qualification_production.py",
        "tests/test_step5d_autotune_v3_tp_delivery_transaction.py",
        "tests/test_step5d_runtime_environment.py",
        "tests/test_step5d_v3_immutable_payload_routing.py",
    } <= active_tests
    assert "release_manifest_v3_verified" in gate["acceptance_path"]
