"""Deterministic proof for the r005 managed control-runtime admission seam."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r005 import control_runtime  # noqa: E402
from step5d_autotune_v4_r005.contracts import load_contract  # noqa: E402
import step5d_managed_runtime as managed_runtime  # noqa: E402
from step5d_autotune_v3.runtime_installation import RuntimeVerificationMode  # noqa: E402


launcher = importlib.import_module("launch_step5d_autotune_v4_r005_control")


ROS_PATHS = (
    "/opt/ros/humble/lib/python3.10/site-packages",
    "/opt/ros/humble/local/lib/python3.10/dist-packages",
)


def _manifest():
    return load_contract().runtime_manifest


def _fake_runtime(tmp_path: Path) -> tuple[dict[str, object], dict[str, str], Path]:
    root = tmp_path / "managed-control"
    executable = root / "bin/python"
    target = tmp_path / "system-python3.10"
    executable.parent.mkdir(parents=True)
    target.write_bytes(b"managed-python")
    target.chmod(0o755)
    executable.symlink_to(target)
    pointer = {
        "schema": "step5d.autotune-v3/runtime-pointer-v2",
        "bundle_id": "a" * 64,
        "contract_sha256": "b" * 64,
        "lock_sha256": "c" * 64,
        "attestation_sha256": "d" * 64,
        "profiles": {
            "control": {
                "root": str(root),
                "python_executable": str(executable),
                "environment_id": "e" * 64,
                "profile_tree_sha256": "f" * 64,
            }
        },
    }
    environment = {
        "STEP5D_V3_RUNTIME_PROFILE": "control",
        "STEP5D_V3_CONTROL_ENVIRONMENT_ID": "e" * 64,
        "STEP5D_V3_CONTROL_PYTHON": str(executable),
        "PYTHONPATH": os_pathsep(
            (str(ROOT / "tools"), str(ROOT.parents[1] / "src/ur10e_experiment_runtime"), *ROS_PATHS)
        ),
    }
    return pointer, environment, executable


def os_pathsep(values: tuple[str, ...]) -> str:
    import os

    return os.pathsep.join(values)


def test_control_runtime_fails_closed_before_heavy_imports() -> None:
    tree = ast.parse(
        (ROOT / "tools/step5d_autotune_v4_r005/control_runtime.py").read_text(
            encoding="utf-8"
        )
    )
    heavy = {"numpy", "pinocchio", "torch", "cupy", "botorch", "gpytorch"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & heavy


def test_resolver_selects_control_profile_and_ros_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pointer, environment, executable = _fake_runtime(tmp_path)
    assert executable.is_symlink()
    manifest = _manifest()
    runtime_contract = {
        "profiles": {
            "control": {
                "required_imports": ["numpy"],
                "required_distributions": {"numpy": "1.24.4"},
            }
        },
        "ros": {"packages": {"ros-humble-pinocchio": "4.0.0"}},
    }
    calls: list[RuntimeVerificationMode] = []

    def pointer_loader(mode: RuntimeVerificationMode, *, environ: dict[str, str]) -> dict[str, object]:
        calls.append(mode)
        return pointer

    def builder(source: dict[str, str], *, profile: str, runtime_pointer: dict[str, object]) -> dict[str, str]:
        assert profile == "control"
        assert runtime_pointer == pointer
        return dict(environment)

    def probe(**kwargs: object) -> dict[str, object]:
        assert kwargs["python_executable"] == executable
        assert kwargs["required_imports"] == ("numpy", "pinocchio")
        return {
            "python_executable": str(executable),
            "python_resolved_executable": str(executable.resolve()),
            "python_prefix": str(executable.parent.parent.resolve()),
            "required_imports": {"numpy": "managed/numpy", "pinocchio": "managed/pinocchio"},
            "required_distributions": {"numpy": "1.24.4"},
        }

    monkeypatch.setattr(control_runtime, "load_contract", lambda: type("Contract", (), {"runtime_manifest": manifest})())
    monkeypatch.setattr(managed_runtime, "load_runtime_contract", lambda: runtime_contract)
    binding = control_runtime.resolve_control_runtime(
        source_environment={},
        pointer_loader=pointer_loader,
        environment_builder=builder,
        dependency_probe=probe,
    )
    assert calls == [RuntimeVerificationMode.FULL_AUDIT]
    assert binding.profile == "control"
    assert binding.python_executable == executable
    assert set(ROS_PATHS).issubset(set(binding.pythonpath))
    assert "STEP5D_V3_OPTIMIZER_ENVIRONMENT_ID" not in binding.environment
    assert binding.command("run_step5d_autotune_v4_r005")[:4] == (
        str(executable),
        "-B",
        "-m",
        "run_step5d_autotune_v4_r005",
    )


def test_child_probe_accepts_real_venv_symlink_shape(tmp_path: Path) -> None:
    root = tmp_path / "venv"
    executable = root / "bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(Path(sys.executable).resolve())
    attestation = control_runtime._probe_control_interpreter(
        python_executable=executable,
        python_prefix=Path(sys.prefix).resolve(),
        environment={"PYTHONPATH": str(ROOT / "tools"), "PYTHONNOUSERSITE": "1"},
        required_imports=(),
        required_distributions={},
    )
    assert attestation["python_executable"] == str(executable)
    assert attestation["python_resolved_executable"] == str(executable.resolve())
    assert attestation["python_prefix"] == str(Path(sys.prefix).resolve())


@pytest.mark.parametrize("bad_shape", ("broken", "misdirected"))
def test_broken_or_misdirected_python_symlink_fails_closed(
    tmp_path: Path,
    bad_shape: str,
) -> None:
    pointer, _environment, executable = _fake_runtime(tmp_path)
    executable.unlink()
    if bad_shape == "broken":
        executable.symlink_to(tmp_path / "missing-python3.10")
    else:
        other = tmp_path / "other-python3.10"
        other.write_bytes(b"other-python")
        other.chmod(0o755)
        executable.symlink_to(other)
        pointer["profiles"]["control"]["python_executable"] = str(other)
    with pytest.raises(control_runtime.ControlRuntimeError, match="CONTROL_RUNTIME_INVALID"):
        control_runtime._pointer_row(pointer, _manifest())


def test_pointer_failure_prevents_dependency_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest()
    probed = False

    def probe(**kwargs: object) -> dict[str, object]:
        nonlocal probed
        probed = True
        return {}

    monkeypatch.setattr(control_runtime, "load_contract", lambda: type("Contract", (), {"runtime_manifest": manifest})())

    def pointer_loader(mode: RuntimeVerificationMode, *, environ: dict[str, str]) -> dict[str, object]:
        raise RuntimeError("pointer missing")

    with pytest.raises(control_runtime.ControlRuntimeError, match="CONTROL_RUNTIME_INVALID"):
        control_runtime.resolve_control_runtime(
            source_environment={}, pointer_loader=pointer_loader, dependency_probe=probe
        )
    assert not probed


@pytest.mark.parametrize(
    ("route", "expected_module", "remaining"),
    (
        ("prepare", "prepare_step5d_autotune_v4_r005_live", ("--help", "tail")),
        ("host", "run_step5d_autotune_v4_r005", ("--offline", "value", "")),
    ),
)
def test_launcher_allowlisted_routes_forward_remaining_args_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    expected_module: str,
    remaining: tuple[str, ...],
) -> None:
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def fake_launch(manifest_path: Path, args: tuple[str, ...]) -> int:
        calls.append((manifest_path, args))
        return 23

    monkeypatch.setattr(launcher, "launch_manifest_route", fake_launch)
    assert launcher.main((route, *remaining)) == 23
    assert calls == [(_manifest().path, (route, *remaining))]
    assert _manifest().routes[route] == expected_module


@pytest.mark.parametrize("argv", ((), ("unknown", "--help")))
def test_launcher_rejects_missing_or_unknown_route_before_child(
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
) -> None:
    calls: list[tuple[Path, tuple[str, ...]]] = []
    with pytest.raises(SystemExit):
        launcher.main(argv)
    assert calls == []
