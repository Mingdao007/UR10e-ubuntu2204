from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_test_matrix as runner  # noqa: E402
import validate_step5d_autotune_v3_refactor as validator  # noqa: E402


def test_small_uses_bounded_xdist_while_medium_remains_serial() -> None:
    commands = runner.load_commands(runner.MATRIX, ["small", "medium"], 4)
    assert commands["small"][4:10] == [
        "-p", "xdist.plugin", "-n", "4", "--dist", "loadgroup"
    ]
    assert "-n" not in commands["medium"]


def test_serial_fallback_has_no_xdist() -> None:
    commands = runner.load_commands(runner.MATRIX, ["small", "medium"], 1)
    assert all("-n" not in command for command in commands.values())


def test_auto_workers_use_affinity_and_reserve_medium_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda _pid: {0, 1})
    assert runner.resolve_workers("auto", ["small", "medium"]) == 1
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda _pid: set(range(16)))
    assert runner.resolve_workers("auto", ["small", "medium"]) == 4


def test_serial_fallback_serializes_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def fake_run(name: str, _command: list[str], _output: Path) -> dict[str, object]:
        observed.append(name)
        return {"lane": name, "returncode": 0}

    monkeypatch.setattr(runner, "_run_lane", fake_run)
    payload = runner.run(
        ["small", "medium"],
        workers="auto",
        output=tmp_path / "serial",
        serial=True,
    )

    assert observed == ["small", "medium"]
    assert payload["parallel_policy"]["max_concurrent_lanes"] == 1
    assert payload["parallel_policy"]["serial_fallback"] is True


def test_installed_runtime_runs_only_after_passing_hermetic_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def fake_run(name: str, _command: list[str], _output: Path) -> dict[str, object]:
        observed.append(name)
        return {"lane": name, "returncode": 0}

    monkeypatch.setattr(runner, "_run_lane", fake_run)
    monkeypatch.setattr(
        runner,
        "load_installed_runtime_command",
        lambda _path: ["/governed/control/bin/python", "-m", "pytest", "-q"],
    )
    monkeypatch.setattr(
        runner,
        "_installed_runtime_precondition",
        lambda: {
            "schema": runner.INSTALLED_RUNTIME_PRECONDITION_SCHEMA,
            "ok": True,
            "release_mode": "deployed-current",
            "reason_code": "CURRENT_RELEASE_VALID",
            "detail": "",
            "program_id": "step5d_strict_rnn_autotune_v3_r017",
            "manifest_path": "config/step5d/releases/fixture/manifest.json",
            "manifest_sha256": "f" * 64,
        },
    )
    monkeypatch.setattr(
        runner,
        "_runtime_binding",
        lambda: ["/control/python", "/optimizer/python", *("a" * 64 for _ in range(6)), "GPU-fixture", "/runtime/nvidia", "/runtime/cupy-cache"],
    )
    payload = runner.run(
        ["small", "medium"],
        workers=2,
        output=tmp_path / "installed",
        include_installed_runtime=True,
    )

    assert set(observed[:2]) == {"small", "medium"}
    assert observed[-1] == "local_installed_runtime"
    assert payload["parallel_policy"]["installed_runtime_status"] == (
        "executed_serial_after_hermetic"
    )
    assert payload["installed_runtime_binding"]["gpu_uuid"] == "GPU-fixture"
    assert payload["installed_runtime_precondition"]["reason_code"] == (
        "CURRENT_RELEASE_VALID"
    )


def test_invalid_current_release_blocks_installed_runtime_before_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def fake_run(name: str, _command: list[str], _output: Path) -> dict[str, object]:
        observed.append(name)
        return {"lane": name, "returncode": 0}

    monkeypatch.setattr(runner, "_run_lane", fake_run)
    monkeypatch.setattr(
        runner,
        "_installed_runtime_precondition",
        lambda: {
            "schema": runner.INSTALLED_RUNTIME_PRECONDITION_SCHEMA,
            "ok": False,
            "release_mode": "deployed-current",
            "reason_code": "CURRENT_RELEASE_INVALID",
            "detail": "ReleaseIdentityError: current source fingerprints differ",
            "program_id": "",
            "manifest_path": "",
            "manifest_sha256": "",
        },
    )
    monkeypatch.setattr(
        runner,
        "_runtime_binding",
        lambda: pytest.fail("runtime binding must not run after failed precondition"),
    )

    payload = runner.run(
        ["small", "medium"],
        workers=2,
        output=tmp_path / "stale-current",
        include_installed_runtime=True,
    )

    assert set(observed) == {"small", "medium"}
    assert payload["ok"] is False
    assert payload["parallel_policy"]["installed_runtime_status"] == (
        "blocked_by_current_release_precondition"
    )
    assert payload["installed_runtime_binding"] is None
    assert payload["installed_runtime_precondition"]["reason_code"] == (
        "CURRENT_RELEASE_INVALID"
    )


def test_installed_runtime_precondition_bounds_failure_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenReleaseIdentityModule:
        def __getattr__(self, _name: str) -> object:
            raise RuntimeError("x" * 2048)

    monkeypatch.setitem(
        sys.modules,
        "step5d_autotune_v3.release_identity",
        BrokenReleaseIdentityModule(),
    )

    payload = runner._installed_runtime_precondition()

    assert payload == {
        "schema": runner.INSTALLED_RUNTIME_PRECONDITION_SCHEMA,
        "ok": False,
        "release_mode": "deployed-current",
        "reason_code": "CURRENT_RELEASE_INVALID",
        "detail": payload["detail"],
        "program_id": "",
        "manifest_path": "",
        "manifest_sha256": "",
    }
    assert payload["detail"].startswith("RuntimeError: ")
    assert len(payload["detail"]) == runner.PRECONDITION_DETAIL_MAX_CHARS


def test_repository_binding_drift_blocks_a_passing_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = iter(
        [
            {
                "root": "/repo",
                "head": "a" * 40,
                "clean": True,
                "status_sha256": "0" * 64,
                "matrix_sha256": "1" * 64,
            },
            {
                "root": "/repo",
                "head": "b" * 40,
                "clean": True,
                "status_sha256": "0" * 64,
                "matrix_sha256": "1" * 64,
            },
        ]
    )
    monkeypatch.setattr(runner, "_repository_binding", lambda: next(bindings))
    monkeypatch.setattr(
        runner,
        "_run_lane",
        lambda name, _command, _output: {"lane": name, "returncode": 0},
    )

    payload = runner.run(
        ["small"],
        workers=1,
        output=tmp_path / "drift",
        require_clean=True,
    )

    assert payload["ok"] is False
    assert payload["repository_binding"]["stable"] is False


def test_installed_runtime_lane_uses_governed_cuda_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = ["/control/python", "/optimizer/python", *(["a" * 64] * 6)]
    binding.extend(["GPU-fixture", "/runtime/nvidia", "/runtime/cupy-cache"])
    observed: dict[str, str] = {}

    def fake_run(*_args, **kwargs):
        observed.update(kwargs["env"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner, "_runtime_binding", lambda: binding)
    monkeypatch.setattr(runner, "_pytest_overlay", lambda output: output)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner._run_lane(
        "local_installed_runtime",
        ["/control/python", "-m", "pytest", "-q"],
        tmp_path,
    )

    assert result["returncode"] == 0
    assert observed["CUDA_VISIBLE_DEVICES"] == "GPU-fixture"
    assert observed["LD_LIBRARY_PATH"] == "/runtime/nvidia"
    assert observed["CUPY_CACHE_DIR"] == "/runtime/cupy-cache"


def test_installed_runtime_pytest_overlay_comes_from_frozen_venv(
    tmp_path: Path,
) -> None:
    hermetic_site = tmp_path / "hermetic-site"
    (hermetic_site / "pytest").mkdir(parents=True)
    (hermetic_site / "_pytest").mkdir()
    (hermetic_site / "pytest" / "__init__.py").write_text("\n", encoding="utf-8")
    (hermetic_site / "_pytest" / "__init__.py").write_text("\n", encoding="utf-8")
    original_run = runner.subprocess.run

    def fake_run(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(returncode=0, stdout=f"{hermetic_site}\n")

    runner.subprocess.run = fake_run
    try:
        overlay = runner._pytest_overlay(tmp_path)
    finally:
        runner.subprocess.run = original_run

    assert not (overlay / "pytest").is_symlink()
    assert not (overlay / "_pytest").is_symlink()
    assert (overlay / "pytest" / "__init__.py").is_file()
    assert (overlay / "_pytest" / "__init__.py").is_file()


@pytest.mark.parametrize("lanes", [["large_ursim"], ["hil_no_motion"], []])
def test_runner_refuses_nonhermetic_or_empty_selection(lanes: list[str]) -> None:
    with pytest.raises(runner.TestMatrixError):
        runner.load_commands(runner.MATRIX, lanes, 2)


def test_failed_lane_emits_bounded_log_tail_without_polluting_manifest(
    tmp_path: Path,
) -> None:
    failed = tmp_path / "failed.log"
    failed.write_text(
        "\n".join(f"diagnostic-{index:03d}" for index in range(250)) + "\n",
        encoding="utf-8",
    )
    passed = tmp_path / "passed.log"
    passed.write_text("successful-secret\n", encoding="utf-8")
    payload = {
        "results": [
            {"lane": "small", "returncode": 1, "log": str(failed)},
            {"lane": "medium", "returncode": 0, "log": str(passed)},
        ]
    }
    stream = io.StringIO()

    runner.emit_failure_logs(payload, stream=stream)

    output = stream.getvalue()
    assert "BEGIN FAILED LANE small LOG TAIL" in output
    assert "diagnostic-249" in output
    assert "diagnostic-049" not in output
    assert "successful-secret" not in output


def test_authoritative_gate_runs_every_active_file_and_no_obsolete_file() -> None:
    payload = json.loads(runner.MATRIX.read_text(encoding="utf-8"))
    gate = payload["authoritative_bridge_gate"]
    classifications = gate["classified_test_files"]
    commanded = {
        token
        for lane in payload["lanes"].values()
        for command in lane["commands"]
        for token in command
        if token.startswith("tests/")
    }
    commanded.update(
        token
        for token in payload["local_installed_runtime_gate"]["command"]
        if token.startswith("tests/")
    )
    classified = set().union(*map(set, classifications.values()))

    assert gate["current_release_pointer"] == "config/step5d/current.json"
    assert gate["unclassified_failure_policy"] == "block"
    assert set(classifications["active"]).issubset(commanded)
    assert set(classifications["obsolete"]).isdisjoint(commanded)
    assert set(classifications["active"]).isdisjoint(classifications["obsolete"])
    assert validator.step5d_v3_test_paths(ROOT) <= classified


def test_authoritative_gate_binds_the_production_vertical_slice() -> None:
    payload = json.loads(runner.MATRIX.read_text(encoding="utf-8"))
    gate = payload["authoritative_bridge_gate"]

    assert gate["canonical_launcher"] == "scripts/step5d-autotune-v3.sh bridge-live"
    assert gate["status_reanchor"] == "scripts/step5d-autotune-v3.sh status --json"
    assert gate["acceptance_path"][1] == "release_manifest_v3_verified"
    assert gate["acceptance_path"][-5:] == [
        "one_trial_completed",
        "command_bound_next_arm_grant",
        "next_arm_acknowledged",
        "bridge_process_still_alive_at_campaign_outcome",
        "campaign_terminal_attested_before_bridge_cleanup",
    ]

    active = set(gate["classified_test_files"]["active"])
    assert {
        "tests/test_step5d_release_contract.py",
        "tests/test_step5d_autotune_v3_tp_delivery_transaction.py",
        "tests/test_step5d_runtime_environment.py",
        "tests/test_step5d_autotune_live_driver.py",
        "tests/test_step5d_autotune_v3_bridge_wrapper.py",
        "tests/test_step5d_parameter_queue.py",
    } <= active


def test_historical_incident_regressions_are_authoritative_and_resolvable() -> None:
    payload = json.loads(runner.MATRIX.read_text(encoding="utf-8"))
    commands = [
        token
        for lane in payload["lanes"].values()
        for command in lane["commands"]
        for token in command
    ] + payload["local_installed_runtime_gate"]["command"]
    commanded_files = {token for token in commands if token.startswith("tests/")}
    required_ids = {
        "r004_return_telemetry_mismatch_not_fresh_row_timeout",
        "r005_typed_closure_v2_independent_cold_read",
        "r005_post_ack_prefixed_csv_schema",
        "r005_batch_bootstrap_null_source_reaches_arm2",
        "initial_parameters_seed_once_without_physical_replay",
        "p0_v7_outer_output_requires_cmd_valid",
        "r010_manual_required_launch_fields",
        "r010_release_manifest_path_required",
    }
    requirements = {row["id"]: row for row in payload["requirements"]}

    assert required_ids <= requirements.keys()
    for incident_id in required_ids:
        row = requirements[incident_id]
        fixture = ROOT / row["incident_fixture"]
        test_file = row["test_node"].split("::", 1)[0]
        assert fixture.is_file()
        assert test_file in commanded_files
        assert f"def {row['test_node'].rsplit('::', 1)[-1]}(" in (
            ROOT / test_file
        ).read_text(encoding="utf-8")
