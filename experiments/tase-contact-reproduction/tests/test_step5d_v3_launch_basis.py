from __future__ import annotations

import json
from pathlib import Path
import hashlib
import subprocess
import time
import sys
from types import SimpleNamespace
from typing import Callable, Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

import run_step5d_autotune_v3_coordinator as coordinator
from step5d_autotune_v3.launch_basis import (
    LaunchBasisError,
    make_launch_basis,
    read_and_validate_launch_basis,
    validate_delivery_observation_binding,
    validate_strict_bridge_ready,
    write_launch_basis,
)
from run_step5d_autotune_v3_coordinator import (
    parse_args,
    run as run_coordinator,
)


def _basis(now: int) -> dict[str, object]:
    return make_launch_basis(
        release_manifest_sha256="a" * 64,
        runtime_identity_sha256="b" * 64,
        campaign_fingerprint="c" * 64,
        delivery_observation_sha256="d" * 64,
        owner_pid=123,
        owner_starttime=456,
        authority_epoch=9,
        launch_nonce="e" * 32,
        argv_sha256="f" * 64,
        effective_config_sha256="0" * 64,
        worktree_root="/tmp/step5d-tase-basis-root",
        repository_head="a" * 40,
        issued_at_unix_ns=now - 1_000_000,
        expires_at_unix_ns=now + 10_000_000_000,
    )


def test_launch_basis_digest_owner_and_freshness(tmp_path: Path) -> None:
    now = time.time_ns()
    path = tmp_path / "launch-basis.json"
    basis = write_launch_basis(path, _basis(now))
    checked = read_and_validate_launch_basis(
        path,
        owner_pid=123,
        owner_starttime=456,
        expected_basis_sha256=basis["basis_sha256"],
        now_unix_ns=now,
    )
    assert checked["authority_epoch"] == 9

    with pytest.raises(LaunchBasisError, match="owner binding"):
        read_and_validate_launch_basis(
            path,
            owner_pid=999,
            owner_starttime=456,
            expected_basis_sha256=basis["basis_sha256"],
            now_unix_ns=now,
        )


def test_launch_basis_rejects_wrong_expected_digest(tmp_path: Path) -> None:
    now = time.time_ns()
    path = tmp_path / "launch-basis.json"
    write_launch_basis(path, _basis(now))
    with pytest.raises(LaunchBasisError, match="digest differs"):
        read_and_validate_launch_basis(
            path,
            owner_pid=123,
            owner_starttime=456,
            expected_basis_sha256="0" * 64,
            now_unix_ns=now,
        )


def test_launch_basis_none_path_fails_closed() -> None:
    with pytest.raises(LaunchBasisError, match="missing or unsafe"):
        read_and_validate_launch_basis(
            None,
            owner_pid=123,
            owner_starttime=456,
        )


def test_consumers_hash_actual_delivery_and_admission_identity(tmp_path: Path) -> None:
    now = time.time_ns()
    delivery = tmp_path / "delivery.json"
    delivery.write_text('{"delivery":true}\n', encoding="utf-8")
    basis = _basis(now)
    basis["delivery_observation_sha256"] = hashlib.sha256(
        delivery.read_bytes()
    ).hexdigest()
    admission = {"delivery_observation": {"path": "delivery.json", "sha256": basis["delivery_observation_sha256"]}}
    assert validate_delivery_observation_binding(
        delivery, basis=basis, admission=admission, experiment_root=tmp_path
    ) == basis["delivery_observation_sha256"]
    delivery.write_text('{"delivery":false}\n', encoding="utf-8")
    with pytest.raises(LaunchBasisError, match="digest"):
        validate_delivery_observation_binding(
            delivery, basis=basis, admission=admission, experiment_root=tmp_path
        )


def _write_basis_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_strict_bridge_ready_binds_process_ticket_basis_release_and_health(tmp_path: Path) -> None:
    basis = {
        "basis_sha256": "a" * 64,
        "release_manifest_sha256": "b" * 64,
        "delivery_observation_sha256": "d" * 64,
    }
    absolute_basis_path = str(tmp_path / "basis.json")
    ticket = {
        "launch_id": "c" * 32,
        "launch_basis": {"sha256": basis["basis_sha256"], "path": absolute_basis_path},
        "delivery_observation": {"sha256": "d" * 64, "path": "delivery.json"},
        "control_profile_id": "profile",
        "manifest_sha256": basis["release_manifest_sha256"],
        "tp_program_id": "program",
    }
    ready = {
        "ready_schema": "step5d_bridge_ready_v2",
        "ok": True,
        "pid": 99,
        "launch_nonce": "c" * 32,
        "bridge_profile": "profile",
        "rtde_connected": True,
        "rtde_send_succeeded": True,
        "sensor_stream_ready": True,
        "prewarm_status": "ok",
        "rtde_output_fields": ["heartbeat"],
        "rtde_output_types": ["DOUBLE"],
    }
    assert validate_strict_bridge_ready(
        ready,
        bridge_pid=99,
        bridge_starttime_ticks=123,
        launch_nonce="c" * 32,
        expected_profile="profile",
        ticket=ticket,
        basis=basis,
        release={"manifest_sha256": "b" * 64, "program_id": "program"},
    )["ok"] is True


def test_launch_basis_v1_schema_still_readable_for_one_compatibility_release(tmp_path: Path) -> None:
    now = time.time_ns()
    path = tmp_path / "launch-basis-v1.json"
    payload = _basis(now)
    payload = {
        key: value
        for key, value in payload.items()
        if key not in {"worktree_root", "repository_head"}
    }
    payload["schema"] = "step5d.autotune-v3/launch-basis-v1"
    payload.pop("basis_sha256", None)
    payload["basis_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()
    _write_basis_json(path, payload)
    checked = read_and_validate_launch_basis(
        path,
        owner_pid=123,
        owner_starttime=456,
        expected_basis_sha256=payload["basis_sha256"],
        now_unix_ns=now,
    )
    assert checked["schema"] == "step5d.autotune-v3/launch-basis-v1"


def test_validate_strict_bridge_ready_rejects_relative_basis_path(tmp_path: Path) -> None:
    basis = {
        "basis_sha256": "a" * 64,
        "release_manifest_sha256": "b" * 64,
        "delivery_observation_sha256": "d" * 64,
    }
    ticket = {
        "launch_id": "c" * 32,
        "launch_basis": {"sha256": basis["basis_sha256"], "path": "basis.json"},
        "delivery_observation": {"sha256": basis["delivery_observation_sha256"], "path": "delivery.json"},
    }
    ready = {
        "ready_schema": "step5d_bridge_ready_v2",
        "ok": True,
        "pid": 99,
        "launch_nonce": "c" * 32,
        "bridge_profile": "profile",
        "rtde_connected": True,
        "rtde_send_succeeded": True,
        "sensor_stream_ready": True,
        "prewarm_status": "ok",
        "rtde_output_fields": ["heartbeat"],
        "rtde_output_types": ["DOUBLE"],
    }
    with pytest.raises(LaunchBasisError, match="absolute"):
        validate_strict_bridge_ready(
            ready,
            bridge_pid=99,
            bridge_starttime_ticks=123,
            launch_nonce="c" * 32,
            expected_profile="profile",
            ticket=ticket,
            basis=basis,
            release={"manifest_sha256": "b" * 64, "program_id": "program"},
        )


def test_validate_strict_bridge_ready_rejects_tampered_basis_sha() -> None:
    basis = {
        "basis_sha256": "a" * 64,
        "release_manifest_sha256": "b" * 64,
        "delivery_observation_sha256": "d" * 64,
    }
    ticket = {
        "launch_id": "c" * 32,
        "launch_basis": {"sha256": "b" * 64, "path": "/tmp/basis.json"},
        "delivery_observation": {"sha256": basis["delivery_observation_sha256"], "path": "/tmp/delivery.json"},
    }
    ready = {
        "ready_schema": "step5d_bridge_ready_v2",
        "ok": True,
        "pid": 99,
        "launch_nonce": "c" * 32,
        "bridge_profile": "profile",
        "rtde_connected": True,
        "rtde_send_succeeded": True,
        "sensor_stream_ready": True,
        "prewarm_status": "ok",
        "rtde_output_fields": ["heartbeat"],
        "rtde_output_types": ["DOUBLE"],
    }
    with pytest.raises(LaunchBasisError, match="basis identity"):
        validate_strict_bridge_ready(
            ready,
            bridge_pid=99,
            bridge_starttime_ticks=123,
            launch_nonce="c" * 32,
            expected_profile="profile",
            ticket=ticket,
            basis=basis,
            release={"manifest_sha256": "b" * 64, "program_id": "program"},
        )


def _coordinator_args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        experiment_root=tmp_path / "experiment",
        admission=tmp_path / "admission.json",
        authority_root=tmp_path / "authority",
        attempt_id="timing-attempt",
        authority_epoch=7,
        authority_resource_id=coordinator.authority.DEFAULT_RESOURCE_ID,
        owner_pid=123,
        owner_starttime=456,
        output_root=tmp_path / "coordinator",
        campaign_root=tmp_path / "campaign",
        delivery_observation=tmp_path / "delivery-observation.json",
        preflight=tmp_path / "coordinator" / "preflight.json",
        launch_basis=tmp_path / "coordinator" / "launch-basis.json",
        basis_ttl_s=120,
    )


def _fake_process(
    *,
    initial_returncode: int | None,
    wait_side_effect: Callable[[], None] | None = None,
) -> Any:
    class _Process:
        def __init__(self) -> None:
            self.returncode = initial_returncode
            self.terminated = False
            self.killed = False
            self.wait_called = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_called = True
            if wait_side_effect is not None:
                wait_side_effect()
            return 0 if self.returncode is None else self.returncode

    return _Process()


def test_run_coordinator_revoke_on_nonzero_lane_terminates_sibling(monkeypatch, tmp_path: Path) -> None:
    args = _coordinator_args(tmp_path)
    revoke_calls: list[str] = []

    def _fake_basis(_args: Any) -> dict[str, Any]:
        return {
            "basis_sha256": "a" * 64,
            "campaign_fingerprint": "c" * 64,
        }

    def _fake_lane_commands(_args: Any, _basis: Any) -> list[list[str]]:
        return [["python", "campaign"], ["python", "preflight"]]

    first = _fake_process(initial_returncode=1)
    second = _fake_process(initial_returncode=None)

    def _fake_popen(command: list[str], **_kwargs: Any) -> Any:
        if command[1] == "campaign":
            return first
        if command[1] == "preflight":
            return second
        raise AssertionError("unexpected command")

    def _fake_revoke(_args: Any, reason: str) -> None:
        revoke_calls.append(reason)

    monkeypatch.setattr("run_step5d_autotune_v3_coordinator._basis", _fake_basis)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator._lane_commands", _fake_lane_commands)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator._revoke", _fake_revoke)

    result = run_coordinator(args)
    assert result == 1
    assert revoke_calls == ["failed"]
    assert second.terminated is True


@pytest.mark.parametrize("mode", ["popen", "timeout"])
def test_run_coordinator_revoke_and_cleanup_on_spawn_or_timeout(monkeypatch, tmp_path: Path, mode: str) -> None:
    args = _coordinator_args(tmp_path)
    revoke_calls: list[str] = []
    spawn_calls: list[list[str]] = []

    def _fake_basis(_args: Any) -> dict[str, Any]:
        return {
            "basis_sha256": "a" * 64,
            "campaign_fingerprint": "c" * 64,
        }

    def _fake_lane_commands(_args: Any, _basis: Any) -> list[list[str]]:
        return [["python", "campaign"], ["python", "preflight"]]

    campaign = _fake_process(initial_returncode=1 if mode == "timeout" else None)
    preflight = _fake_process(
        initial_returncode=None,
        wait_side_effect=(
            (lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="stub", timeout=5.0)))
            if mode == "timeout"
            else None
        ),
    )

    def _fake_popen(command: list[str], **_kwargs: Any) -> Any:
        spawn_calls.append(command)
        if command[1] == "campaign":
            return campaign
        if mode == "popen" and command[1] == "preflight":
            raise RuntimeError("popen failed")
        if command[1] == "preflight":
            return preflight
        raise AssertionError("unexpected command")

    def _fake_revoke(_args: Any, reason: str) -> None:
        revoke_calls.append(reason)

    monkeypatch.setattr("run_step5d_autotune_v3_coordinator._basis", _fake_basis)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator._lane_commands", _fake_lane_commands)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator._revoke", _fake_revoke)

    if mode == "popen":
        with pytest.raises(RuntimeError, match="popen failed"):
            run_coordinator(args)
        assert revoke_calls == ["failed"]
        assert campaign.terminated is True
        assert campaign.killed in (False, True)
    else:
        result = run_coordinator(args)
        assert result == 1
        assert revoke_calls == ["failed"]
        assert preflight.wait_called is True
        assert preflight.terminated is True
        assert preflight.killed is True

    assert spawn_calls


def test_parse_args_defaults_authority_root_and_resource_id() -> None:
    args = parse_args(
        [
            "--experiment-root",
            "/tmp/experiment-root",
            "--admission",
            "/tmp/admission.json",
            "--attempt-id",
            "attempt-defaults",
            "--authority-epoch",
            "11",
            "--owner-pid",
            "123",
            "--owner-starttime",
            "456",
            "--output-root",
            "/tmp/coordinator-output",
            "--campaign-root",
            "/tmp/campaign-root",
            "--delivery-observation",
            "/tmp/delivery-observation.json",
            "--preflight",
            "/tmp/preflight.json",
            "--launch-basis",
            "/tmp/launch-basis.json",
        ]
    )
    assert args.authority_root is None
    assert args.authority_resource_id == coordinator.authority.DEFAULT_RESOURCE_ID


def test_revoke_subprocess_uses_resource_id_and_optional_authority_root(monkeypatch: Any, tmp_path: Path) -> None:
    args = _coordinator_args(tmp_path)
    args.authority_resource_id = "custom-resource-id"
    command_log: list[list[str]] = []
    next_sequence = {"value": args.authority_epoch + 1}
    state_reads = [
        {
            "state": "ACTIVE",
            "attempt_id": args.attempt_id,
            "owner": {"pid": args.owner_pid, "starttime_ticks": args.owner_starttime},
            "sequence": args.authority_epoch,
        },
        {
            "state": "REVOKED",
            "attempt_id": args.attempt_id,
            "owner": {"pid": args.owner_pid, "starttime_ticks": args.owner_starttime},
            "reason": "failed",
            "sequence": args.authority_epoch + 1,
        },
    ]

    def _fake_load_current(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if not state_reads:
            return {}
        return state_reads.pop(0)

    def _fake_run(
        command: list[str],
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> Any:
        command_log.append(command)
        class _Completed:
            stdout = str(next_sequence["value"])
            next_sequence["value"] += 1

        return _Completed()

    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.authority.load_current", _fake_load_current)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.subprocess.run", _fake_run)
    args.authority_root = None
    coordinator._revoke(args, "failed")
    assert command_log, "revoke command should be called"
    absent_root_command = command_log.pop(0)
    assert "--resource-id" in absent_root_command
    assert "custom-resource-id" in absent_root_command
    assert "--authority-root" not in absent_root_command

    state_reads.extend(
        [
            {
                "state": "ACTIVE",
                "attempt_id": args.attempt_id,
                "owner": {"pid": args.owner_pid, "starttime_ticks": args.owner_starttime},
                "sequence": args.authority_epoch + 1,
            },
            {
                "state": "REVOKED",
                "attempt_id": args.attempt_id,
                "owner": {"pid": args.owner_pid, "starttime_ticks": args.owner_starttime},
                "reason": "cancelled",
                "sequence": args.authority_epoch + 2,
            },
        ]
    )
    args.authority_root = tmp_path / "authority-root"
    coordinator._revoke(args, "cancelled")
    present_root_command = command_log.pop(0)
    assert "--resource-id" in present_root_command
    assert "custom-resource-id" in present_root_command
    root_index = present_root_command.index("--authority-root")
    assert present_root_command[root_index + 1] == str(args.authority_root)


def test_basis_binding_is_idempotent_for_duplicate_launch_basis(monkeypatch, tmp_path: Path) -> None:
    args = _coordinator_args(tmp_path)
    args.attempt_id = "e" * 32
    args.experiment_root.mkdir()
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.delivery_observation = args.experiment_root / "delivery-observation.json"
    args.delivery_observation.write_text('{"delivery":true}\n', encoding="utf-8")
    args.admission.write_text("{}", encoding="utf-8")
    state = {
        "state": "ACTIVE",
        "sequence": args.authority_epoch,
        "attempt_id": args.attempt_id,
        "owner": {"pid": args.owner_pid, "starttime_ticks": args.owner_starttime},
        "worktree_root": str(args.experiment_root.resolve()),
        "repository_head": "d" * 40,
        "authority_epoch": args.authority_epoch,
        "launch_basis_path": None,
        "launch_basis_sha256": None,
    }
    counters = {"make": 0, "write": 0, "bind": 0}

    def _fake_load_runtime_release(_root: Path) -> Any:
        return SimpleNamespace(manifest_sha256="a" * 64, program_id="program")

    def _fake_resolve_bridge_admission(_root: Path, release: Any) -> tuple[Path, dict[str, Any]]:
        return (
            args.admission,
            {
                "campaign_fingerprint": "c" * 64,
                "delivery_observation": {"path": "delivery-observation.json"},
            },
        )

    def _fake_release_payload_path(_root: Path, _release: Any, payload_path: str) -> Path:
        return args.experiment_root / payload_path

    def _fake_load_contract(_path: Path) -> Any:
        return {}

    def _fake_load_launch_profile(
        _path: Path, contract: Any | None = None, expected_tp_program_id: Any | None = None
    ) -> Any:
        return {}

    def _fake_check_effective_config(**_kwargs: Any) -> dict[str, Any]:
        return {"effective_config": {}}

    def _fake_release_runtime_contract(_root: Path, _release: Any) -> dict[str, str]:
        return {"tp_runtime_identity": {}}

    def _fake_load_current(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("require_basis_bound"):
            if (
                state.get("launch_basis_path") != kwargs.get("launch_basis_path")
                or state.get("launch_basis_sha256") != kwargs.get("launch_basis_sha256")
            ):
                raise RuntimeError("basis binding not yet committed")
        return dict(state)
    real_make_basis = coordinator.make_launch_basis
    real_write_basis = coordinator.write_launch_basis

    def _fake_bind_basis(
        _authority_root: Path | None = None,
        attempt_id: str | None = None,
        owner_pid: int | None = None,
        owner_starttime_ticks: int | None = None,
        *,
        sequence: int | None = None,
        launch_basis_path: str | None = None,
        launch_basis_sha256: str | None = None,
        resource_id: str | None = None,
    ) -> dict[str, Any]:
        assert attempt_id == args.attempt_id
        assert owner_pid == args.owner_pid
        assert owner_starttime_ticks == args.owner_starttime
        assert sequence == state["sequence"]
        assert resource_id == args.authority_resource_id
        if (
            state["launch_basis_path"] is not None
            and state["launch_basis_sha256"] is not None
            and state["launch_basis_sha256"] != launch_basis_sha256
        ):
            raise RuntimeError("conflicting basis")
        state["launch_basis_path"] = launch_basis_path
        state["launch_basis_sha256"] = launch_basis_sha256
        counters["bind"] += 1
        return dict(state)

    def _fake_make_launch_basis(**kwargs: Any) -> dict[str, Any]:
        counters["make"] += 1
        return real_make_basis(**kwargs)

    def _fake_write_basis(_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        counters["write"] += 1
        payload = dict(payload)
        payload["launch_nonce"] = args.attempt_id
        return real_write_basis(_path, payload)

    monkeypatch.setattr("run_step5d_autotune_v3_coordinator._repository_head", lambda _root: "d" * 40)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.load_runtime_release", _fake_load_runtime_release)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.resolve_bridge_admission", _fake_resolve_bridge_admission)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.release_payload_path", _fake_release_payload_path)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.load_contract", _fake_load_contract)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.load_launch_profile", _fake_load_launch_profile)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.check_effective_config", _fake_check_effective_config)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.release_runtime_contract", _fake_release_runtime_contract)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.authority.load_current", _fake_load_current)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.authority.bind_basis", _fake_bind_basis)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.make_launch_basis", _fake_make_launch_basis)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.write_launch_basis", _fake_write_basis)
    first = coordinator._basis(args)
    args.output_root = args.output_root.parent / "coordinator_retry"
    args.output_root.mkdir()
    second = coordinator._basis(args)
    assert first["basis_sha256"] == second["basis_sha256"]
    assert first["campaign_fingerprint"] == "c" * 64
    assert second["campaign_fingerprint"] == "c" * 64
    assert state["launch_basis_path"] == str(args.launch_basis.resolve())
    assert counters["make"] == 1
    assert counters["write"] == 1
    assert counters["bind"] == 1


def test_basis_binding_rejects_drifted_bound_launch_basis(monkeypatch, tmp_path: Path) -> None:
    args = _coordinator_args(tmp_path)
    args.attempt_id = "f" * 32
    args.experiment_root.mkdir()
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.delivery_observation = args.experiment_root / "delivery-observation.json"
    args.delivery_observation.write_text('{"delivery":true}\n', encoding="utf-8")
    args.admission.write_text("{}", encoding="utf-8")
    state = {
        "state": "ACTIVE",
        "sequence": args.authority_epoch,
        "attempt_id": args.attempt_id,
        "owner": {"pid": args.owner_pid, "starttime_ticks": args.owner_starttime},
        "worktree_root": str(args.experiment_root.resolve()),
        "repository_head": "d" * 40,
        "authority_epoch": args.authority_epoch,
        "launch_basis_path": str(args.launch_basis.resolve()),
        "launch_basis_sha256": None,
    }
    stale_delivery_hash = hashlib.sha256(args.delivery_observation.read_bytes()).hexdigest()
    stale_runtime_identity = {"tag": "stale-runtime"}
    stale_basis = coordinator.make_launch_basis(
        release_manifest_sha256="b" * 64,
        runtime_identity_sha256=coordinator._sha256_json(stale_runtime_identity),
        campaign_fingerprint="e" * 64,
        delivery_observation_sha256=stale_delivery_hash,
        owner_pid=args.owner_pid,
        owner_starttime=args.owner_starttime,
        authority_epoch=args.authority_epoch,
        launch_nonce=args.attempt_id,
        argv_sha256="c" * 64,
        effective_config_sha256="0" * 64,
        worktree_root=str(args.experiment_root.resolve()),
        repository_head="d" * 40,
        issued_at_unix_ns=time.time_ns() - 1_000_000,
        expires_at_unix_ns=time.time_ns() + 10_000_000_000,
    )
    state["launch_basis_sha256"] = coordinator.write_launch_basis(args.launch_basis, stale_basis)["basis_sha256"]
    args.delivery_observation.write_text('{"delivery":false}\n', encoding="utf-8")

    def _fake_load_runtime_release(_root: Path) -> Any:
        return SimpleNamespace(manifest_sha256="a" * 64, program_id="program")

    def _fake_resolve_bridge_admission(_root: Path, release: Any) -> tuple[Path, dict[str, Any]]:
        return (
            args.admission,
            {
                "campaign_fingerprint": "d" * 64,
                "delivery_observation": {"path": "delivery-observation.json"},
            },
        )

    def _fake_release_runtime_contract(_root: Path, _release: Any) -> dict[str, Any]:
        return {"tp_runtime_identity": {"tag": "fresh-runtime"}}

    def _fake_load_current(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("require_basis_bound"):
            if (
                state.get("launch_basis_path") != kwargs.get("launch_basis_path")
                or state.get("launch_basis_sha256") != kwargs.get("launch_basis_sha256")
            ):
                raise RuntimeError("basis binding not yet committed")
        return dict(state)

    monkeypatch.setattr("run_step5d_autotune_v3_coordinator._repository_head", lambda _root: "d" * 40)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.load_runtime_release", _fake_load_runtime_release)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.resolve_bridge_admission", _fake_resolve_bridge_admission)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.release_runtime_contract", _fake_release_runtime_contract)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.authority.load_current", _fake_load_current)

    with pytest.raises(RuntimeError, match="coordinator launch basis identity differs from authority state"):
        coordinator._basis(args)


@pytest.mark.parametrize("only_path_set", [True, False])
def test_basis_binding_fails_for_partial_existing_basis(monkeypatch, tmp_path: Path, only_path_set: bool) -> None:
    args = _coordinator_args(tmp_path)
    args.experiment_root.mkdir()
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.delivery_observation = args.experiment_root / "delivery-observation.json"
    args.delivery_observation.write_text('{"delivery":true}\n', encoding="utf-8")
    args.admission.write_text("{}", encoding="utf-8")
    state = {
        "state": "ACTIVE",
        "sequence": args.authority_epoch,
        "attempt_id": args.attempt_id,
        "owner": {"pid": args.owner_pid, "starttime_ticks": args.owner_starttime},
        "worktree_root": str(args.experiment_root.resolve()),
        "repository_head": "d" * 40,
        "authority_epoch": args.authority_epoch,
        "launch_basis_path": str(args.launch_basis.resolve()) if only_path_set else None,
        "launch_basis_sha256": None if only_path_set else "b" * 64,
    }

    def _fake_load_runtime_release(_root: Path) -> Any:
        return SimpleNamespace(manifest_sha256="a" * 64, program_id="program")

    def _fake_resolve_bridge_admission(_root: Path, release: Any) -> tuple[Path, dict[str, Any]]:
        return (
            args.admission,
            {
                "campaign_fingerprint": "c" * 64,
                "delivery_observation": {"path": "delivery-observation.json"},
            },
        )

    def _fake_load_current(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(state)

    monkeypatch.setattr("run_step5d_autotune_v3_coordinator._repository_head", lambda _root: "d" * 40)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.load_runtime_release", _fake_load_runtime_release)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.resolve_bridge_admission", _fake_resolve_bridge_admission)
    monkeypatch.setattr("run_step5d_autotune_v3_coordinator.authority.load_current", _fake_load_current)

    with pytest.raises(RuntimeError, match="launch basis binding is incomplete"):
        coordinator._basis(args)
