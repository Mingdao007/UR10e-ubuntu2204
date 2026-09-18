from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(TOOLS))

import contact_benchmark_preflight as preflight  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_receipt(root: Path, venv: Path, receipt: Path) -> None:
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            {
                "schema": "ur10e.contact-six-provision-v1",
                "profile_id": preflight.PROFILE_ID,
                "dependency_group": preflight.DEPENDENCY_GROUP,
                "venv": str(venv),
                "pyproject_sha256": _sha256(root / "pyproject.toml"),
                "uv_lock_sha256": _sha256(root / "uv.lock"),
            }
        ),
        encoding="utf-8",
    )


def test_contact_control_group_has_exact_cpu_pins_without_accelerator_groups() -> None:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 test hosts
        import tomli as tomllib

    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    group = document["dependency-groups"]["contact-control"]
    assert group == [
        "jsonschema==4.26.0",
        "numpy==1.24.4",
        "osqp==1.1.1",
        "pytest==8.4.1",
        "pyyaml==6.0.3",
        "scipy==1.15.3",
        "setuptools==81.0.0",
        "ur10e-experiment-runtime==0.1.0",
    ]
    assert not any(name.startswith(("torch", "cupy")) for name in group)
    source = document["tool"]["uv"]["sources"]["ur10e-experiment-runtime"]
    assert source == {
        "path": "../../src/ur10e_experiment_runtime",
        "editable": False,
    }


def test_lock_declares_contact_control_and_osqp_without_removing_legacy_groups() -> None:
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "osqp"' in lock
    assert 'name = "ur10e-experiment-runtime"' in lock
    assert "contact-control = [" in lock
    assert 'name = "cupy-cuda12x"' in lock
    assert 'name = "torch"' in lock


def test_status_without_managed_runtime_is_explicit_provision_action(tmp_path: Path) -> None:
    payload, returncode = preflight.run_status(
        experiment_root=tmp_path,
        venv=tmp_path / ".venv-contact-six",
        receipt=tmp_path / "runs" / "provision.json",
    )
    assert returncode == 2
    assert payload["ok"] is False
    assert payload["action"] == "provision"
    assert payload["offline_boundary"] == {
        "network_used": False,
        "device_io": False,
        "motion_authorized": False,
        "live_qualified": False,
        "hardware_qualified": False,
    }


def test_receipt_binds_both_project_inputs_and_requests_reprovision(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    venv = tmp_path / ".venv-contact-six"
    receipt = tmp_path / "runs" / "provision.json"
    _write_receipt(tmp_path, venv, receipt)
    assert preflight._receipt_observation(preflight.Paths(tmp_path, venv, receipt))["ok"] is True

    (tmp_path / "uv.lock").write_text("version = 1\nchanged = true\n", encoding="utf-8")
    observation = preflight._receipt_observation(preflight.Paths(tmp_path, venv, receipt))
    assert observation["ok"] is False
    assert observation["action"] == "provision"
    assert "uv_lock_sha256" in observation["mismatches"]


def test_interpreter_observation_rejects_global_python_even_when_receipt_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = tmp_path / ".venv-contact-six"
    monkeypatch.setattr(preflight.sys, "prefix", "/usr")
    monkeypatch.setattr(preflight.sys, "base_prefix", "/usr")
    observed = preflight._interpreter_observation(preflight.Paths(tmp_path, venv, tmp_path / "r"))
    assert observed["ok"] is False


def test_ros_setup_requires_explicit_ament_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
    with pytest.raises(preflight.PreflightError, match="AMENT_PREFIX_PATH"):
        preflight._prepare_ros_import_paths()


def test_contact_status_dispatch_is_offline_and_does_not_enter_legacy_runtime() -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    completed = subprocess.run(
        [str(SCRIPTS / "step5d-autotune-v3.sh"), "contact-status"],
        cwd=ROOT.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode in (0, 2)
    payload = json.loads(completed.stdout)
    assert payload["command"] == "status"
    assert payload["profile_id"] == preflight.PROFILE_ID
    assert payload["offline_boundary"]["device_io"] is False
    assert payload["offline_boundary"]["network_used"] is False
    assert "RUNTIME_LOCK_MISMATCH" not in completed.stderr


def test_contact_six_status_reports_missing_runtime_without_invoking_uv() -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    env["PATH"] = "/usr/bin:/bin"
    completed = subprocess.run(
        [str(SCRIPTS / "contact-six.sh"), "status"],
        cwd=ROOT.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode in (0, 2)
    payload = json.loads(completed.stdout)
    assert payload["offline_boundary"]["device_io"] is False
    assert payload["offline_boundary"]["network_used"] is False


def test_managed_status_reports_full_composition_imports_when_ready() -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    env["PATH"] = "/usr/bin:/bin"
    completed = subprocess.run(
        [str(SCRIPTS / "contact-six.sh"), "status"],
        cwd=ROOT.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(completed.stdout)
    if completed.returncode == 0:
        assert payload["full_composition_imports_ready"] is True
        composition = payload["checks"]["composition_imports"]
        assert composition["ok"] is True
        assert composition["forbidden_after"] == []
        assert set(composition["modules"]) == {
            "ur10e_experiment_runtime",
            "contact_benchmark_runtime",
            "contact_benchmark_provider",
            "contact_benchmark_timing",
            "contact_benchmark_campaign",
            "step5d_autotune_v4_r006.live_adapter",
            "step5d_autotune_v4_r013.live_owner",
        }
    else:
        assert payload["full_composition_imports_ready"] is False
