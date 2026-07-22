from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Mapping

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src/ur10e_experiment_runtime"))

from step5d_autotune_v3 import runtime_installation as runtime  # noqa: E402


RESOLVER_PATH = ROOT / "tools/resolve_step5d_autotune_v3_runtime.py"
RESOLVER_SPEC = importlib.util.spec_from_file_location(
    "step5d_v3_runtime_resolver_test", RESOLVER_PATH
)
assert RESOLVER_SPEC is not None and RESOLVER_SPEC.loader is not None
resolver = importlib.util.module_from_spec(RESOLVER_SPEC)
RESOLVER_SPEC.loader.exec_module(resolver)


def _runtime_pointer_fixture(tmp_path: Path) -> dict[str, object]:
    return {
        "bundle_id": "a" * 64,
        "contract_sha256": "b" * 64,
        "lock_sha256": "c" * 64,
        "attestation_sha256": "d" * 64,
        "profiles": {
            profile: {
                "root": str(tmp_path / profile),
                "environment_id": ("1" if profile == "control" else "2") * 64,
                "python_executable": str(tmp_path / profile / "bin/python"),
                "record_tree_sha256": (
                    "3" if profile == "control" else "4"
                )
                * 64,
                "profile_tree_sha256": (
                    "5" if profile == "control" else "6"
                )
                * 64,
            }
            for profile in runtime.PROFILES
        },
    }


def test_runtime_contract_and_lock_define_two_distinct_profiles() -> None:
    contract = runtime.load_runtime_contract()

    assert tuple(sorted(contract["profiles"])) == runtime.PROFILES
    assert len(runtime.runtime_bundle_id(contract)) == 64
    assert runtime.profile_environment_id("control", contract) != (
        runtime.profile_environment_id("optimizer", contract)
    )
    assert contract["profiles"]["control"]["required_distributions"]["numpy"] == (
        "1.24.4"
    )
    assert contract["profiles"]["optimizer"]["required_distributions"]["torch"] == (
        "2.11.0+cu128"
    )


def test_missing_runtime_pointer_has_one_machine_reason(tmp_path: Path) -> None:
    environment = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    status = runtime.runtime_status(environ=environment)

    assert status["observed_environment_id"] is None
    assert status["control_ready"] is False
    assert status["optimizer_ready"] is False
    assert status["gpu_identity_ready"] is False
    assert status["runtime_attestation_sha256"] is None
    assert set(status["profiles"]) == set(runtime.PROFILES)
    assert all(
        status["profiles"][profile]["ready"] is False
        for profile in runtime.PROFILES
    )
    assert status["reason_code"] == "RUNTIME_NOT_PROVISIONED"


def test_runtime_binding_exposes_exact_dual_profile_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = _runtime_pointer_fixture(tmp_path)
    monkeypatch.setattr(runtime, "load_runtime_pointer", lambda **_kwargs: pointer)

    binding = runtime.runtime_binding()

    assert binding["schema"] == "step5d.autotune-v3/runtime-process-binding-v1"
    assert binding["bundle_id"] == pointer["bundle_id"]
    assert binding["contract_sha256"] == pointer["contract_sha256"]
    assert binding["lock_sha256"] == pointer["lock_sha256"]
    assert binding["runtime_attestation_sha256"] == pointer["attestation_sha256"]
    assert binding["gpu_uuid"] == runtime.load_runtime_contract()["gpu"]["uuid"]
    for profile in runtime.PROFILES:
        assert binding["profiles"][profile] == {
            key: value
            for key, value in pointer["profiles"][profile].items()
            if key != "root"
        }


def test_runtime_status_uses_one_static_integrity_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = _runtime_pointer_fixture(tmp_path)
    calls: list[Mapping[str, str] | None] = []
    monkeypatch.setattr(runtime, "runtime_bundle_id", lambda: pointer["bundle_id"])
    monkeypatch.setattr(
        runtime,
        "profile_environment_id",
        lambda profile: pointer["profiles"][profile]["environment_id"],
    )

    def load_integrity(*, environ=None):
        calls.append(environ)
        return pointer

    monkeypatch.setattr(runtime, "load_runtime_pointer_integrity", load_integrity)
    monkeypatch.setattr(
        runtime,
        "load_runtime_pointer",
        lambda **_kwargs: pytest.fail("status must not import/smoke both profiles"),
    )

    status = runtime.runtime_status(environ={"HOME": str(tmp_path)})

    assert status["reason_code"] is None
    assert calls == [{"HOME": str(tmp_path)}]


def test_status_resolver_reuses_one_runtime_status_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def status() -> dict[str, object]:
        calls.append("status")
        return {
            "reason_code": "RUNTIME_NOT_PROVISIONED",
            "detail": "fixture",
        }

    monkeypatch.setattr(resolver, "runtime_status", status)
    monkeypatch.setattr(
        resolver,
        "load_runtime_pointer",
        lambda: pytest.fail("status resolver must not reload the pointer"),
    )
    monkeypatch.setattr(
        resolver,
        "load_runtime_pointer_integrity",
        lambda: pytest.fail("status resolver must reuse runtime_status"),
    )

    assert resolver.main(["--status-json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == ["status"]
    assert payload["blocker"]["reason_codes"] == ["RUNTIME_NOT_PROVISIONED"]
    assert payload["next_action"] == "provision_runtime"


def test_shell_binding_uses_identity_pointer_before_command_full_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pointer = _runtime_pointer_fixture(tmp_path)
    pointer["attestation_sha256"] = "d" * 64
    calls: list[str] = []

    def load_identity() -> dict[str, object]:
        calls.append("identity")
        return pointer

    monkeypatch.setattr(resolver, "load_runtime_pointer_identity", load_identity)
    monkeypatch.setattr(
        resolver,
        "load_runtime_pointer",
        lambda: pytest.fail("shell binding must not import/smoke both profiles"),
    )

    assert resolver.main(["--shell-binding"]) == 0
    fields = capsys.readouterr().out.strip().split("\t")
    assert calls == ["identity"]
    assert fields[:2] == [
        pointer["profiles"]["control"]["python_executable"],
        pointer["profiles"]["optimizer"]["python_executable"],
    ]


def test_require_runtime_profile_rejects_cross_profile_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = _runtime_pointer_fixture(tmp_path)
    monkeypatch.setattr(runtime, "load_runtime_pointer", lambda **_kwargs: pointer)
    control_python = pointer["profiles"]["control"]["python_executable"]
    optimizer_python = pointer["profiles"]["optimizer"]["python_executable"]

    monkeypatch.setattr(runtime.sys, "executable", control_python)
    monkeypatch.setattr(runtime.sys, "prefix", pointer["profiles"]["control"]["root"])
    assert runtime.require_runtime_profile("control") == pointer

    with pytest.raises(runtime.RuntimeInstallationError) as caught:
        runtime.require_runtime_profile("optimizer")
    assert caught.value.reason_code == "OPTIMIZER_RUNTIME_INVALID"
    assert optimizer_python in caught.value.detail
    assert control_python in caught.value.detail


def test_runtime_status_reports_exact_profile_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = _runtime_pointer_fixture(tmp_path)
    required_profiles = {"control": "7" * 64, "optimizer": "8" * 64}
    monkeypatch.setattr(runtime, "runtime_bundle_id", lambda: pointer["bundle_id"])
    monkeypatch.setattr(
        runtime, "load_runtime_pointer_integrity", lambda **_kwargs: pointer
    )
    monkeypatch.setattr(
        runtime,
        "profile_environment_id",
        lambda profile: required_profiles[profile],
    )

    status = runtime.runtime_status()

    assert status["required_environment_id"] == pointer["bundle_id"]
    assert status["observed_environment_id"] == pointer["bundle_id"]
    assert status["runtime_attestation_sha256"] == pointer["attestation_sha256"]
    assert status["gpu_identity_ready"] is True
    assert status["reason_code"] is None
    for profile in runtime.PROFILES:
        assert status["profiles"][profile] == {
            "required_environment_id": required_profiles[profile],
            "observed_environment_id": pointer["profiles"][profile][
                "environment_id"
            ],
            "ready": True,
            "python_executable": pointer["profiles"][profile]["python_executable"],
            "record_tree_sha256": pointer["profiles"][profile][
                "record_tree_sha256"
            ],
            "profile_tree_sha256": pointer["profiles"][profile][
                "profile_tree_sha256"
            ],
        }


def _write_distribution(site: Path, name: str, version: str, payload: bytes) -> None:
    module = site / f"{name}.py"
    module.write_bytes(payload)
    metadata = site / f"{name}-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )
    rows = [
        f"{name}.py,,",
        f"{metadata.name}/METADATA,,",
        f"{metadata.name}/RECORD,,",
    ]
    (metadata / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_record_tree_changes_when_an_installed_file_changes(tmp_path: Path) -> None:
    environment = tmp_path / "environment"
    site = environment / "lib/python3.10/site-packages"
    site.mkdir(parents=True)
    _write_distribution(site, "demo", "1.0", b"VALUE = 1\n")

    first, count, distributions = runtime._record_tree(environment, site)
    (site / "demo.py").write_bytes(b"VALUE = 2\n")
    second, second_count, second_distributions = runtime._record_tree(environment, site)

    assert count == second_count == 3
    assert distributions == second_distributions == {"demo": "1.0"}
    assert first != second


def test_record_tree_rejects_duplicate_distribution_metadata(tmp_path: Path) -> None:
    environment = tmp_path / "environment"
    site = environment / "lib/python3.10/site-packages"
    site.mkdir(parents=True)
    _write_distribution(site, "demo", "1.0", b"VALUE = 1\n")
    duplicate = site / "demo-2.0.dist-info"
    duplicate.mkdir()
    (duplicate / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo\nVersion: 2.0\n",
        encoding="utf-8",
    )
    (duplicate / "RECORD").write_text(
        f"{duplicate.name}/METADATA,,\n{duplicate.name}/RECORD,,\n",
        encoding="utf-8",
    )

    with pytest.raises(runtime.RuntimeInstallationError, match="duplicate distributions"):
        runtime._record_tree(environment, site)


def test_runtime_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    payload = json.loads(runtime.CONTRACT_PATH.read_text(encoding="utf-8"))
    payload["accidental_local_override"] = True
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(runtime.RuntimeInstallationError, match="fields differ"):
        runtime.load_runtime_contract(path)


def test_runtime_contract_malformed_nested_objects_have_one_typed_reason(
    tmp_path: Path,
) -> None:
    original = json.loads(runtime.CONTRACT_PATH.read_text(encoding="utf-8"))
    mutations = [
        lambda value: value.__setitem__("python", []),
        lambda value: value["uv"].pop("sha256"),
        lambda value: value["host"].__setitem__("required_available_cpus", [11, 11]),
        lambda value: value["ros"].__setitem__("xacro_path", "relative.xacro"),
        lambda value: value["gpu"].__setitem__("uuid", "not-a-gpu-uuid"),
        lambda value: value["calibration"].__setitem__("artifact_path", "../escape"),
        lambda value: value.__setitem__("owner_dependencies", []),
        lambda value: value["profiles"]["control"]["forbidden_imports"].append(
            "cupy"
        ),
    ]
    for index, mutate in enumerate(mutations):
        payload = json.loads(json.dumps(original))
        mutate(payload)
        path = tmp_path / f"contract-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(runtime.RuntimeInstallationError) as caught:
            runtime.load_runtime_contract(path)
        assert caught.value.reason_code == "HOST_CONTRACT_MISMATCH"


def test_dependency_manifest_change_invalidates_environment_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = runtime.load_runtime_contract()
    monkeypatch.setattr(runtime, "dependency_manifest_sha256", lambda: "0" * 64)

    with pytest.raises(runtime.RuntimeInstallationError) as caught:
        runtime.runtime_bundle_id(contract)

    assert caught.value.reason_code == "RUNTIME_LOCK_MISMATCH"


def test_host_identity_binds_calibration_bytes_not_checkout_location() -> None:
    first = {
        "schema": "step5d.autotune-v3/host-observation-v1",
        "observed_at_unix_ns": 1,
        "calibration": {
            "artifact_path": "/checkout-a/runtime-calibration.json",
            "artifact_sha256": "a" * 64,
            "yaml_path": "/checkout-a/calibration.yaml",
            "yaml_sha256": "b" * 64,
        },
    }
    relocated = json.loads(json.dumps(first))
    relocated["observed_at_unix_ns"] = 2
    relocated["calibration"]["artifact_path"] = "/checkout-b/runtime-calibration.json"
    relocated["calibration"]["yaml_path"] = "/checkout-b/calibration.yaml"

    assert runtime._host_identity(first) == runtime._host_identity(relocated)
    relocated["calibration"]["yaml_sha256"] = "c" * 64
    assert runtime._host_identity(first) != runtime._host_identity(relocated)


def test_profile_tree_digest_covers_untracked_files_symlinks_and_modes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    package = root / "package.py"
    package.write_text("VALUE = 1\n", encoding="utf-8")
    first, first_count = runtime._filesystem_tree(root)

    untracked = root / "sitecustomize.py"
    untracked.write_text("raise SystemExit('must never execute')\n", encoding="utf-8")
    second, second_count = runtime._filesystem_tree(root)
    untracked.chmod(0o444)
    third, _ = runtime._filesystem_tree(root)
    link = root / "alias.py"
    link.symlink_to(package.name)
    fourth, fourth_count = runtime._filesystem_tree(root)

    assert len({first, second, third, fourth}) == 4
    assert second_count == first_count + 1
    assert fourth_count == second_count + 1


def test_pointer_payload_binds_manifest_and_both_profile_trees(tmp_path: Path) -> None:
    snapshot = runtime._capture_runtime_inputs()
    profiles = {
        profile: {
            "environment_id": runtime.profile_environment_id(
                profile,
                snapshot.contract,
                contract_sha256_value=snapshot.contract_sha256,
                lock_sha256_value=snapshot.lock_sha256,
            ),
            "root": str(tmp_path / profile),
            "python_executable": str(tmp_path / profile / "bin/python"),
            "record_tree_sha256": ("1" if profile == "control" else "2") * 64,
            "profile_tree_sha256": ("3" if profile == "control" else "4") * 64,
        }
        for profile in runtime.PROFILES
    }
    pointer = runtime._pointer_payload(
        bundle_id=runtime._snapshot_bundle_id(snapshot),
        snapshot=snapshot,
        attestation_path=tmp_path / "attestation.json",
        attestation_sha256="5" * 64,
        installation_manifest_sha256="6" * 64,
        profiles=profiles,
    )

    assert pointer["installation_manifest_sha256"] == "6" * 64
    assert pointer["profiles"]["control"]["profile_tree_sha256"] == "3" * 64
    assert pointer["profiles"]["optimizer"]["profile_tree_sha256"] == "4" * 64


def test_provisioning_commands_pin_copy_mode_and_disable_ambient_uv_config() -> None:
    source = Path(runtime.__file__).read_text(encoding="utf-8")

    assert '"--link-mode",\n            "copy"' in source
    assert source.count('"--no-config"') >= 3
    assert '"--no-build"' in source
    assert '"--frozen"' in source
    assert '"lock",\n                "--check",\n                "--offline"' in source


def test_bootstrap_tools_do_not_require_the_unprovisioned_application_runtime() -> None:
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
    }
    for tool in (
        ROOT / "tools/provision_step5d_autotune_v3_runtime.py",
        ROOT / "tools/resolve_step5d_autotune_v3_runtime.py",
    ):
        before = {
            path: path.stat().st_mtime_ns
            for path in (ROOT / "tools/step5d_autotune_v3").rglob("*.pyc")
        }
        completed = subprocess.run(
            ["/usr/bin/python3.10", "-B", "-I", str(tool), "--help"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        after = {
            path: path.stat().st_mtime_ns
            for path in (ROOT / "tools/step5d_autotune_v3").rglob("*.pyc")
        }
        assert after == before


def test_runtime_resolution_has_no_integrity_bypass() -> None:
    assert "verify_integrity" not in inspect.signature(
        runtime.load_runtime_pointer
    ).parameters

    completed = subprocess.run(
        [
            "/usr/bin/python3.10",
            "-B",
            "-I",
            str(ROOT / "tools/resolve_step5d_autotune_v3_runtime.py"),
            "--no-integrity-check",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "unrecognized arguments: --no-integrity-check" in completed.stderr


def test_malformed_current_pointer_preserves_existing_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    snapshot = runtime._capture_runtime_inputs()
    bundle_id = "a" * 64
    final = runtime.runtime_store(environment) / bundle_id
    final.mkdir(parents=True)
    pointer = runtime.current_pointer_path(environment)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text("{not-json\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_capture_runtime_inputs", lambda: snapshot)
    monkeypatch.setattr(runtime, "_snapshot_bundle_id", lambda _snapshot: bundle_id)
    monkeypatch.setattr(runtime, "observe_host", lambda *_args, **_kwargs: {})

    with pytest.raises(runtime.RuntimeInstallationError):
        runtime._provision_runtime_locked(
            uv_executable=tmp_path / "uv",
            controller_helper=tmp_path / "helper",
            environ=environment,
        )

    assert final.is_dir()
    assert pointer.is_file()
    assert not list(final.parent.glob(f".quarantine-{bundle_id}-*"))


def test_valid_old_pointer_allows_new_orphan_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    snapshot = runtime._capture_runtime_inputs()
    candidate_bundle = "a" * 64
    old_bundle = "b" * 64
    final = runtime.runtime_store(environment) / candidate_bundle
    final.mkdir(parents=True)
    pointer_path = runtime.current_pointer_path(environment)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(
        json.dumps(
            {
                "schema": runtime.POINTER_SCHEMA,
                "bundle_id": old_bundle,
                "contract_sha256": "c" * 64,
                "lock_sha256": "d" * 64,
                "attestation_path": str(
                    runtime.runtime_cache_root(environment)
                    / old_bundle
                    / "attestations/old-attestation.json"
                ),
                "attestation_sha256": "e" * 64,
                "installation_manifest_sha256": "f" * 64,
                "profiles": {
                    profile: {
                        "environment_id": ("1" if profile == "control" else "2") * 64,
                        "root": str(
                            runtime.runtime_store(environment) / old_bundle / profile
                        ),
                        "python_executable": str(
                            runtime.runtime_store(environment)
                            / old_bundle
                            / profile
                            / "bin/python"
                        ),
                        "record_tree_sha256": ("3" if profile == "control" else "4") * 64,
                        "profile_tree_sha256": ("5" if profile == "control" else "6") * 64,
                    }
                    for profile in runtime.PROFILES
                },
            }
        ),
        encoding="utf-8",
    )
    quarantined: list[Path] = []

    def stop_after_quarantine(path: Path, *, store: Path) -> Path:
        quarantined.append(path)
        raise runtime.RuntimeInstallationError("TEST_STOP", str(store))

    monkeypatch.setattr(runtime, "_capture_runtime_inputs", lambda: snapshot)
    monkeypatch.setattr(
        runtime,
        "_snapshot_bundle_id",
        lambda _snapshot: candidate_bundle,
    )
    monkeypatch.setattr(runtime, "observe_host", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runtime, "_quarantine_bundle", stop_after_quarantine)

    with pytest.raises(runtime.RuntimeInstallationError) as caught:
        runtime._provision_runtime_locked(
            uv_executable=tmp_path / "uv",
            controller_helper=tmp_path / "helper",
            environ=environment,
        )

    assert caught.value.reason_code == "TEST_STOP"
    assert quarantined == [final]


def test_cross_bound_current_pointer_preserves_candidate_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    snapshot = runtime._capture_runtime_inputs()
    candidate_bundle = "a" * 64
    old_bundle = "b" * 64
    final = runtime.runtime_store(environment) / candidate_bundle
    final.mkdir(parents=True)
    pointer_path = runtime.current_pointer_path(environment)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    profiles = {
        profile: {
            "environment_id": ("1" if profile == "control" else "2") * 64,
            "root": str(runtime.runtime_store(environment) / old_bundle / profile),
            "python_executable": str(
                runtime.runtime_store(environment)
                / old_bundle
                / profile
                / "bin/python"
            ),
            "record_tree_sha256": ("3" if profile == "control" else "4") * 64,
            "profile_tree_sha256": ("5" if profile == "control" else "6") * 64,
        }
        for profile in runtime.PROFILES
    }
    profiles["control"]["root"] = str(final / "control")
    profiles["control"]["python_executable"] = str(final / "control/bin/python")
    pointer_path.write_text(
        json.dumps(
            {
                "schema": runtime.POINTER_SCHEMA,
                "bundle_id": old_bundle,
                "contract_sha256": "c" * 64,
                "lock_sha256": "d" * 64,
                "attestation_path": str(
                    runtime.runtime_cache_root(environment)
                    / old_bundle
                    / "attestations/old-attestation.json"
                ),
                "attestation_sha256": "e" * 64,
                "installation_manifest_sha256": "f" * 64,
                "profiles": profiles,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "_capture_runtime_inputs", lambda: snapshot)
    monkeypatch.setattr(
        runtime,
        "_snapshot_bundle_id",
        lambda _snapshot: candidate_bundle,
    )
    monkeypatch.setattr(runtime, "observe_host", lambda *_args, **_kwargs: {})

    with pytest.raises(runtime.RuntimeInstallationError):
        runtime._provision_runtime_locked(
            uv_executable=tmp_path / "uv",
            controller_helper=tmp_path / "helper",
            environ=environment,
        )

    assert final.is_dir()
    assert not list(final.parent.glob(f".quarantine-{candidate_bundle}-*"))


def test_owner_dependency_comes_from_verified_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "controller-helper.py"
    helper.write_text("print('owner fixture')\n", encoding="utf-8")
    helper_sha256 = runtime._sha256_file(helper)
    contract = json.loads(json.dumps(runtime.load_runtime_contract()))
    contract["owner_dependencies"]["controller_helper"]["sha256"] = (
        helper_sha256
    )
    attestation_path = tmp_path / "attestation.json"
    binding = {
        "owner_id": "ur10e-controller-access",
        "path": str(helper),
        "sha256": helper_sha256,
    }
    monkeypatch.setattr(runtime, "load_runtime_contract", lambda: contract)
    monkeypatch.setattr(
        runtime,
        "load_runtime_pointer",
        lambda **_kwargs: {"attestation_path": str(attestation_path)},
    )
    monkeypatch.setattr(
        runtime,
        "_load_json",
        lambda *_args, **_kwargs: {
            "host": {"owner_dependencies": {"controller_helper": binding}}
        },
    )

    assert runtime.owner_dependency("controller_helper") == binding
    helper.write_text("print('tampered')\n", encoding="utf-8")
    with pytest.raises(runtime.RuntimeInstallationError) as caught:
        runtime.owner_dependency("controller_helper")
    assert caught.value.reason_code == "OWNER_DEPENDENCY_MISMATCH"
