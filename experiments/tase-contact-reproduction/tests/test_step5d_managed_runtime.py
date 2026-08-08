"""Generic managed-runtime manifest, lane-isolation, and launcher proofs."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from step5d_managed_runtime import (
    CONTROL_PROFILE,
    MANIFEST_SCHEMA,
    OPTIMIZER_PROFILE,
    ManagedControlBinding,
    ManagedRuntimeError,
    launch_manifest_route,
    load_runtime_manifest,
    resolve_managed_control_runtime,
    resolve_managed_optimizer_runtime,
    resolve_managed_runtime,
)


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def _synthetic_manifest(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "v5-root"
    release_path = root / "config/release.json"
    release = {
        "schema": "synthetic-v5/release-v1",
        "program": "synthetic_v5",
        "revision": 5,
    }
    _write(release_path, json.dumps(release, sort_keys=True))
    closure = [
        "tools/step5d_managed_runtime.py",
        "tools/step5d_optimizer_runtime.py",
        "tools/step5d_autotune_v3/runtime_installation.py",
        "tools/step5d_autotune_v3/runtime_environment.py",
        "config/release.json",
        "tools/launch_synthetic_v5.py",
    ]
    for relative in closure:
        if relative != "config/release.json":
            _write(root / relative, b"# synthetic source closure\n")
    document = {
        "schema": MANIFEST_SCHEMA,
        "lineage_id": "synthetic_v5",
        "launcher_path": "tools/launch_synthetic_v5.py",
        "release_contract": {
            "path": "config/release.json",
            "schema": release["schema"],
            "program": release["program"],
            "revision": release["revision"],
        },
        "routes": {
            "prepare": {"module": "synthetic_v5.prepare"},
            "host": {"module": "synthetic_v5.host"},
        },
        "control": {
            "profile": CONTROL_PROFILE,
            "verification_mode": "full_audit",
            "required_imports": ["synthetic_control"],
            "required_ros_packages": ["ros-humble-pinocchio"],
            "required_ros_pythonpath": ["/opt/ros/humble/lib/python3.10/site-packages"],
        },
        "optimizer": {
            "profile": OPTIMIZER_PROFILE,
            "resolver": "step5d_optimizer_runtime.resolve_optimizer_runtime",
            "worker_module": "synthetic_v5.optimizer_worker",
        },
        "no_system_install": True,
        "source_closure": closure,
    }
    manifest_path = root / "config/synthetic_v5_runtime_manifest.json"
    _write(manifest_path, json.dumps(document, sort_keys=True))
    return manifest_path, document


def _binding(manifest) -> ManagedControlBinding:
    return ManagedControlBinding(
        manifest_path=manifest.path,
        manifest_sha256=manifest.sha256,
        profile=CONTROL_PROFILE,
        bundle_id="a" * 64,
        contract_sha256="b" * 64,
        lock_sha256="c" * 64,
        attestation_sha256="d" * 64,
        environment_id="e" * 64,
        python_executable=Path("/managed-v3/control/bin/python"),
        python_prefix=Path("/managed-v3/control"),
        pythonpath=(),
        required_imports=(),
        required_distributions={},
        dependency_attestation={},
        environment={},
        route_modules=manifest.route_modules,
    )


def test_synthetic_v5_routes_use_managed_profiles_without_generic_changes(tmp_path: Path) -> None:
    path, _document = _synthetic_manifest(tmp_path)
    manifest = load_runtime_manifest(path, root=path.parents[1])
    binding = _binding(manifest)
    calls: list[tuple[list[str], Path]] = []

    def runner(command, *, cwd, **_kwargs):
        calls.append((list(command), Path(cwd)))
        return SimpleNamespace(returncode=19)

    assert launch_manifest_route(
        path,
        ("prepare", "--future", "V5"),
        root=path.parents[1],
        binding=binding,
        process_runner=runner,
    ) == 19
    assert launch_manifest_route(
        path,
        ("host", "--offline", ""),
        root=path.parents[1],
        binding=binding,
        process_runner=runner,
    ) == 19
    assert [call[0][3] for call in calls] == [
        "synthetic_v5.prepare",
        "synthetic_v5.host",
    ]
    assert [call[0][4:] for call in calls] == [["--future", "V5"], ["--offline", ""]]

    runtime = SimpleNamespace(
        declaration=SimpleNamespace(profile=OPTIMIZER_PROFILE),
        python_executable=Path("/managed-v3/optimizer/bin/python"),
        environment_id="f" * 64,
    )
    optimizer = resolve_managed_optimizer_runtime(
        manifest,
        resolver=lambda declaration: runtime,
    )
    assert manifest.control_profile == CONTROL_PROFILE
    assert optimizer.profile == OPTIMIZER_PROFILE
    assert optimizer.worker_module == "synthetic_v5.optimizer_worker"
    assert optimizer.python_executable == runtime.python_executable


def test_synthetic_v5_control_resolver_stays_on_v3_full_audit_profile(tmp_path: Path) -> None:
    path, _document = _synthetic_manifest(tmp_path)
    manifest = load_runtime_manifest(path, root=path.parents[1])
    managed_root = tmp_path / "managed-control"
    executable = managed_root / "bin/python"
    target = tmp_path / "system-python3.10"
    executable.parent.mkdir(parents=True)
    target.write_bytes(b"managed-python")
    target.chmod(0o755)
    executable.symlink_to(target)
    pointer = {
        "schema": "step5d.autotune-v3/runtime-pointer-v2",
        "bundle_id": "1" * 64,
        "contract_sha256": "2" * 64,
        "lock_sha256": "3" * 64,
        "attestation_sha256": "4" * 64,
        "profiles": {
            "control": {
                "root": str(managed_root),
                "python_executable": str(executable),
                "environment_id": "5" * 64,
                "profile_tree_sha256": "6" * 64,
            }
        },
    }
    ros_path = manifest.control_required_ros_pythonpath[0]
    environment = {
        "STEP5D_V3_RUNTIME_PROFILE": CONTROL_PROFILE,
        "STEP5D_V3_CONTROL_ENVIRONMENT_ID": "5" * 64,
        "STEP5D_V3_CONTROL_PYTHON": str(executable),
        "PYTHONPATH": ros_path,
    }

    def probe(**kwargs):
        assert kwargs["python_executable"] == executable
        assert kwargs["required_imports"] == ("v3_control", "synthetic_control")
        return {
            "python_executable": str(executable),
            "python_resolved_executable": str(executable.resolve()),
            "python_prefix": str(managed_root.resolve()),
            "required_imports": {
                "v3_control": "managed/v3_control",
                "synthetic_control": "managed/synthetic_control",
            },
            "required_distributions": {"v3-control": "1.0"},
        }

    binding = resolve_managed_control_runtime(
        manifest,
        source_environment={},
        pointer_loader=lambda _mode, *, environ: pointer,
        environment_builder=lambda source, *, profile, runtime_pointer: environment,
        dependency_probe=probe,
        runtime_contract_loader=lambda: {
            "profiles": {
                "control": {
                    "required_imports": ["v3_control"],
                    "required_distributions": {"v3-control": "1.0"},
                }
            },
            "ros": {"packages": {"ros-humble-pinocchio": "managed"}},
        },
    )
    assert binding.profile == CONTROL_PROFILE
    assert binding.python_executable == executable
    assert "STEP5D_V3_OPTIMIZER_PYTHON" not in binding.environment


def test_manifest_and_route_fail_closed_without_fallback_or_profile_mixing(tmp_path: Path) -> None:
    path, _document = _synthetic_manifest(tmp_path)
    root = path.parents[1]
    manifest = load_runtime_manifest(path, root=root)
    binding = _binding(manifest)
    with pytest.raises(ManagedRuntimeError, match="missing"):
        load_runtime_manifest(root / "missing.json", root=root)
    with pytest.raises(ManagedRuntimeError, match="unknown route"):
        launch_manifest_route(path, ("future-unknown",), root=root, binding=binding)
    with pytest.raises(ManagedRuntimeError, match="cannot be mixed"):
        resolve_managed_runtime(manifest, "system")
    generic_source = (Path(__file__).parents[1] / "tools/step5d_managed_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "pip install" not in generic_source
    assert "python3" not in generic_source


def test_r005_optimizer_manifest_resolves_managed_v3_without_parent_torch() -> None:
    from step5d_autotune_v4_r005.contracts import load_contract

    assert "torch" not in sys.modules
    assert "botorch" not in sys.modules
    manifest = load_contract().runtime_manifest
    binding = resolve_managed_optimizer_runtime(manifest)
    pointer_row = binding.runtime.pointer["profiles"][OPTIMIZER_PROFILE]
    assert binding.profile == OPTIMIZER_PROFILE
    assert binding.resolver == "step5d_optimizer_runtime.resolve_optimizer_runtime"
    assert binding.python_executable == Path(pointer_row["python_executable"])
    assert binding.worker_module == manifest.optimizer_worker_module
    assert "torch" not in sys.modules
    assert "botorch" not in sys.modules
