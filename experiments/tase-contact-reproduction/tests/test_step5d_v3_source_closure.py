#!/usr/bin/env python3
"""Focused tests for the Active V3 static-input closure."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3 import source_closure  # noqa: E402


def _write(root: Path, relative: str, value: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _mini_experiment(
    tmp_path: Path,
    name: str,
    module_suffix: str = "",
) -> Path:
    repository = tmp_path / name
    experiment = repository / "experiments/tase-contact-reproduction"
    pyproject = "[project]\nname = \"closure-test\"\n"
    calibration_yaml = "kinematics:\n  hash: governed\n"
    calibration = {
        "calibrated_model": {
            "calibration_yaml": "../../src/calibration.yaml",
            "calibration_yaml_sha256": hashlib.sha256(
                calibration_yaml.encode("utf-8")
            ).hexdigest(),
        }
    }
    calibration_text = json.dumps(calibration, sort_keys=True) + "\n"
    contract = {
        "schema": "step5d.autotune-v3/runtime-contract-v1",
        "python": {
            "bootstrap_executable": "/contract/python",
            "bootstrap_sha256": "1" * 64,
        },
        "uv": {
            "dependency_manifest_sha256": hashlib.sha256(
                pyproject.encode("utf-8")
            ).hexdigest(),
            "executable_name": "uv",
            "sha256": "2" * 64,
        },
        "profiles": {
            "control": {
                "required_distributions": {"numpy": "1.24.4"},
                "required_imports": ["numpy"],
            }
        },
        "host": {"os_id": "ubuntu", "os_version_id": "22.04"},
        "ros": {
            "packages": {
                "ros-humble-pinocchio": "4.0.0",
                "ros-humble-xacro": "2.1.1",
            }
        },
        "gpu": {"uuid": "GPU-test", "driver_version": "test"},
        "calibration": {
            "artifact_path": "config/runtime_calibration.json",
            "artifact_sha256": hashlib.sha256(
                calibration_text.encode("utf-8")
            ).hexdigest(),
            "yaml_sha256": hashlib.sha256(
                calibration_yaml.encode("utf-8")
            ).hexdigest(),
        },
        "owner_dependencies": {
            "controller_helper": {
                "owner_id": "ur10e-controller-access",
                "sha256": "3" * 64,
            }
        },
    }
    probe = (
        "\n_PROFILE_PROBE = r'''\n"
        "import importlib\n"
        'for name in contract["required_imports"]:\n'
        "    module = importlib.import_module(name)\n"
        "'''\n"
        + module_suffix
    )
    _write(
        experiment,
        "tools/step5d_autotune_v3/runtime_installation.py",
        probe,
    )
    _write(experiment, "pyproject.toml", pyproject)
    _write(experiment, "uv.lock", '[[package]]\nname = "numpy"\n')
    _write(
        experiment,
        "config/step5/step5d_v3_runtime_contract.json",
        json.dumps(contract, sort_keys=True) + "\n",
    )
    _write(experiment, "config/runtime_calibration.json", calibration_text)
    _write(repository, "src/calibration.yaml", calibration_yaml)
    return experiment


@pytest.fixture
def mini_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        source_closure,
        "PRODUCTION_EXPERIMENT_SEEDS",
        frozenset({"tools/step5d_autotune_v3/runtime_installation.py"}),
    )
    monkeypatch.setattr(
        source_closure,
        "PRODUCTION_EXPERIMENT_DATA",
        frozenset(
            {
                "config/step5/step5d_v3_runtime_contract.json",
                "config/runtime_calibration.json",
                "pyproject.toml",
                "uv.lock",
            }
        ),
    )
    monkeypatch.setattr(
        source_closure,
        "PRODUCTION_REPOSITORY_ASSETS",
        frozenset({"src/calibration.yaml"}),
    )


def test_production_report_classifies_every_active_provider() -> None:
    report = source_closure.production_source_closure_report(ROOT)
    assert set(report) == {
        "schema",
        "reason_code_on_failure",
        "experiment_paths",
        "repository_paths",
        "classifications",
        "unresolved",
    }
    assert report["schema"] == source_closure.SOURCE_CLOSURE_SCHEMA
    assert report["reason_code_on_failure"] == "ACTIVE_SOURCE_CLOSURE_UNRESOLVED"
    assert report["unresolved"] == []
    assert set(report["classifications"]) == {
        "stdlib",
        "repository",
        "uv_lock",
        "host_contract",
        "sha_bound_owner",
    }

    uv = {
        row["name"]: row["provider"]
        for row in report["classifications"]["uv_lock"]
    }
    assert uv["cupy"] == "cupy-cuda12x"
    assert uv["pexpect"] == "pexpect"
    assert {"torch", "botorch", "gpytorch", "cupy_backends"}.isdisjoint(uv)
    host_imports = {
        row["name"]: row["provider"]
        for row in report["classifications"]["host_contract"]
        if row["kind"] == "python_import"
    }
    assert host_imports == {
        "pinocchio": "ros-humble-pinocchio",
        "xacro": "ros-humble-xacro",
    }
    assert any(
        row["kind"] == "dynamic_import_seam"
        and row["name"] == "runtime_profile_probe"
        and row["contract_key"] == "profiles.control.required_imports"
        for row in report["classifications"]["host_contract"]
    )
    assert report["classifications"]["sha_bound_owner"] == [
        {
            "kind": "executable",
            "name": "controller_helper",
            "owner_id": "ur10e-controller-access",
            "contract_key": "owner_dependencies.controller_helper.sha256",
            "sha256": "82296c159d241589420a259faf6a480c9c1b3372c9904ba474ecc6140fe03579",
        }
    ]
    assert "pyproject.toml" in report["experiment_paths"]
    assert "uv.lock" in report["experiment_paths"]
    assert {
        "config/step5d/artifact_locators/step5d_v35_retained_inputs.json",
        "config/step5d/manifests/step5d_strict_rnn_ablation_v35/"
        "controller_readback_receipt.json",
        "config/step5d_autotune_campaign_v1.json",
        "programs/step5/step5d/step5d_strict_rnn_ablation_v35.script",
        "programs/step5/step5d/step5d_strict_rnn_ablation_v35.txt",
        "programs/step5/step5d/step5d_strict_rnn_ablation_v35.urp",
    } <= set(report["experiment_paths"])
    assert (
        "src/ur10e_bringup/config/ur10e_calibration.yaml"
        in report["repository_paths"]
    )
    assert {
        "src/ur10e_experiment_runtime/ur10e_experiment_runtime/schemas/"
        "experiment_spec.schema.json",
        "src/ur10e_experiment_runtime/ur10e_experiment_runtime/schemas/"
        "parallel_run_manifest.schema.json",
        "src/ur10e_experiment_runtime/ur10e_experiment_runtime/schemas/"
        "run_manifest.schema.json",
    } <= set(report["repository_paths"])
    assert not any(
        "ur10e_nominal.xml" in path for path in report["repository_paths"]
    )
    assert {
        "tools/step5d_autotune_optimizer.py",
        "tools/step5d_autotune_r008_policy.py",
        "tools/step5d_autotune_v3/optimizer_worker.py",
        "tools/step5d_autotune_v3/runtime_functional_gates.py",
    }.isdisjoint(report["experiment_paths"])
    assert not any(
        "manual" in path or "optimizer" in path
        for path in report["experiment_paths"]
    )
    encoded = json.dumps(report, sort_keys=True)
    assert str(ROOT) not in encoded
    assert "/home/andy" not in encoded
    assert source_closure.production_source_closure(ROOT) == (
        frozenset(report["experiment_paths"]),
        frozenset(report["repository_paths"]),
    )


def test_report_is_clone_location_independent(
    tmp_path: Path, mini_closure: None
) -> None:
    left = _mini_experiment(tmp_path, "left")
    right = _mini_experiment(tmp_path, "right")
    assert source_closure.production_source_closure_report(
        left
    ) == source_closure.production_source_closure_report(right)


def test_unknown_static_import_fails_with_fixed_reason(
    tmp_path: Path, mini_closure: None
) -> None:
    experiment = _mini_experiment(tmp_path, "unknown", "\nimport mystery_runtime\n")
    with pytest.raises(source_closure.SourceClosureError) as caught:
        source_closure.production_source_closure_report(experiment)
    assert caught.value.reason_code == "ACTIVE_SOURCE_CLOSURE_UNRESOLVED"
    assert "unclassified import 'mystery_runtime'" in caught.value.detail


def test_nonliteral_dynamic_import_fails_with_fixed_reason(
    tmp_path: Path, mini_closure: None
) -> None:
    experiment = _mini_experiment(
        tmp_path,
        "dynamic",
        "\nimport importlib\nselected = 'numpy'\nimportlib.import_module(selected)\n",
    )
    with pytest.raises(source_closure.SourceClosureError) as caught:
        source_closure.production_source_closure_report(experiment)
    assert caught.value.reason_code == "ACTIVE_SOURCE_CLOSURE_UNRESOLVED"
    assert "non-literal dynamic import" in caught.value.detail


def test_uv_alias_and_ros_package_mappings_are_verified(
    tmp_path: Path,
    mini_closure: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _mini_experiment(tmp_path, "mapping", "\nimport pinocchio\n")
    report = source_closure.production_source_closure_report(experiment)
    assert any(
        row.get("name") == "pinocchio"
        and row.get("provider") == "ros-humble-pinocchio"
        for row in report["classifications"]["host_contract"]
    )

    monkeypatch.setitem(
        source_closure.UV_IMPORT_DISTRIBUTIONS, "numpy", "missing-distribution"
    )
    with pytest.raises(source_closure.SourceClosureError) as caught:
        source_closure.production_source_closure_report(experiment)
    assert "absent uv.lock distribution 'missing-distribution'" in caught.value.detail
