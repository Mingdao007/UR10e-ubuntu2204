from __future__ import annotations

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

from step5d_autotune_v3.launch_basis import (
    LaunchBasisError,
    make_launch_basis,
    read_and_validate_launch_basis,
    validate_delivery_observation_binding,
    validate_strict_bridge_ready,
    write_launch_basis,
)
from run_step5d_autotune_v3_coordinator import run as run_coordinator


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


def test_strict_bridge_ready_binds_process_ticket_basis_release_and_health() -> None:
    basis = {
        "basis_sha256": "a" * 64,
        "release_manifest_sha256": "b" * 64,
        "delivery_observation_sha256": "d" * 64,
    }
    ticket = {
        "launch_id": "c" * 32,
        "launch_basis": {"sha256": basis["basis_sha256"], "path": "basis.json"},
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


def _coordinator_args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        experiment_root=tmp_path / "experiment",
        admission=tmp_path / "admission.json",
        authority_root=tmp_path / "authority",
        attempt_id="timing-attempt",
        authority_epoch=7,
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
