from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import resolve_step5d_bridge_route as route  # noqa: E402
import step5d_manual_status as status  # noqa: E402
from step5d_autotune_v3.governance import LAUNCH_ATTEMPT_PHASES  # noqa: E402


@pytest.fixture(autouse=True)
def _validated_manual_qualification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status,
        "validate_manual_qualification",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        status,
        "load_capability_authorization",
        lambda *_args, **_kwargs: {
            "capabilities": {
                "bridge": True,
                "play": True,
                "arm": True,
                "motion": True,
                "zero": False,
                "tare": False,
            }
        },
    )


def _qualification_ref(campaign: Path) -> dict[str, str]:
    path = campaign / "manual-qualification.json"
    path.write_text(
        json.dumps(
            {
                "schema": status.QUALIFICATION_SCHEMA,
                "ok": True,
                "state": "MANUAL_BRIDGE_PERSISTENT_NO_ARM_PROVEN",
                "manual_release_manifest_sha256": "a" * 64,
                "play_prompt_ready": False,
            }
        ),
        encoding="utf-8",
    )
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _controller_observation() -> dict[str, object]:
    expected = (
        "/programs/andyl/kunwei/step5/step5d_strict_rnn_manual_tune_v2.urp"
    )
    return {
        "observed_at_unix_ns": time.time_ns(),
        "loaded_program_response": f"Loaded program: {expected}",
        "program_state": "STOPPED",
        "safety_mode": "Safetymode: NORMAL",
        "expected_loaded_program": expected,
    }


def _write_bridge_heartbeat(output: Path) -> None:
    path = output / "runtime/bridge/bridge_rtde_500hz.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("heartbeat\n1\n", encoding="utf-8")


def test_manual_shell_phases_are_registered_in_launch_attempt_fsm() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")
    phases = (
        "runtime_gate",
        "route_resolve",
        "manual_qualification",
        "manual_context",
        "manual_preflight",
        "manual_bridge_start",
        "manual_campaign",
    )
    for phase in phases:
        assert phase in LAUNCH_ATTEMPT_PHASES
        assert f"bridge_begin_phase {phase}" in source


def test_route_selects_manual_only_for_exact_loaded_program(monkeypatch) -> None:
    monkeypatch.setattr(
        route,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {
            "programState": "STOPPED",
            "safetymode": "Safetymode: NORMAL",
            "get loaded program": f"Loaded program: {route.MANUAL_PATH}",
        },
    )
    monkeypatch.setattr(
        route,
        "load_manual_release",
        lambda _root: {"manifest_sha256": "a" * 64},
    )
    result = route.resolve(root=ROOT, robot_host="192.0.2.1", timeout_s=0.1)
    assert result["route"] == "manual_v2"
    assert result["manual_release_manifest_sha256"] == "a" * 64


def test_route_blocks_every_unrecognized_loaded_program(monkeypatch) -> None:
    monkeypatch.setattr(
        route,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {
            "programState": "STOPPED",
            "safetymode": "Safetymode: NORMAL",
            "get loaded program": "Loaded program: /programs/andyl/kunwei/step5/other.urp",
        },
    )
    monkeypatch.setattr(
        route,
        "load_current_release_snapshot",
        lambda _root: type("Release", (), {
            "valid": True,
            "expected_loaded_program": "/programs/andyl/kunwei/step5/v3.urp",
            "manifest_sha256": "b" * 64,
        })(),
    )
    result = route.resolve(root=ROOT, robot_host="192.0.2.1", timeout_s=0.1)
    assert result["route"] == "BLOCKED"
    assert result["reason_code"] == "LOADED_PROGRAM_UNSUPPORTED"
    assert result["manual_release_manifest_sha256"] is None


def test_route_selects_v3_only_for_exact_current_loaded_program(monkeypatch) -> None:
    expected = "/programs/andyl/kunwei/step5/v3.urp"
    monkeypatch.setattr(
        route,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {
            "programState": "STOPPED",
            "safetymode": "Safetymode: NORMAL",
            "get loaded program": f"Loaded program: {expected}",
        },
    )
    monkeypatch.setattr(
        route,
        "load_current_release_snapshot",
        lambda _root: type("Release", (), {
            "valid": True,
            "expected_loaded_program": expected,
            "manifest_sha256": "b" * 64,
        })(),
    )
    result = route.resolve(root=ROOT, robot_host="192.0.2.1", timeout_s=0.1)
    assert result["route"] == "autotune_v3"
    assert result["autotune_release_manifest_sha256"] == "b" * 64


def test_status_recomputes_bridge_heartbeat(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    _write_bridge_heartbeat(output)
    authorization = campaign / "authorization.json"
    authorization.write_text("{}\n", encoding="utf-8")
    status.activate(campaign, output)
    machine = {
        "schema": status.STATUS_SCHEMA,
        "state": "WAITING_FOR_IDENTITY_PLAY",
        "release_sha": "a" * 64,
        "campaign_id": "manual-test",
        "launch_attempt_id": "attempt-1",
        "output_root": str(output),
        "bridge_pid": os.getpid(),
        "bridge_heartbeat": False,
        "play_prompt_ready": True,
        "controller_observation": _controller_observation(),
        "capabilities": {
            "bridge": True,
            "play": True,
            "arm": True,
            "motion": True,
            "zero": False,
            "tare": False,
        },
        "authorization": {
            "path": str(authorization),
            "sha256": hashlib.sha256(authorization.read_bytes()).hexdigest(),
        },
        "offline_qualification": _qualification_ref(campaign),
        "blocker": None,
        "next_action": "press Play once",
    }
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))
    observed = status.read_status(campaign)
    assert observed["bridge_heartbeat"] is True
    assert observed["state"] == "WAITING_FOR_IDENTITY_PLAY"

    csv_path = output / "runtime/bridge/bridge_rtde_500hz.csv"
    stale_ns = time.time_ns() - status.BRIDGE_HEARTBEAT_MAX_AGE_NS - 1
    os.utime(csv_path, ns=(stale_ns, stale_ns))
    stale = status.read_status(campaign)
    assert stale["bridge_heartbeat"] is False
    assert stale["state"] == "BLOCKED"
    assert stale["blocker"] == "BRIDGE_HEARTBEAT_LOST"


def test_status_revokes_play_prompt_when_controller_identity_is_stale(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    _write_bridge_heartbeat(output)
    authorization = campaign / "authorization.json"
    authorization.write_text("{}\n", encoding="utf-8")
    controller = _controller_observation()
    controller["observed_at_unix_ns"] = (
        time.time_ns() - status.CONTROLLER_IDENTITY_MAX_AGE_NS - 1
    )
    machine = {
        "schema": status.STATUS_SCHEMA,
        "state": "WAITING_FOR_IDENTITY_PLAY",
        "release_sha": "a" * 64,
        "campaign_id": "manual-test",
        "launch_attempt_id": "attempt-1",
        "output_root": str(output),
        "bridge_pid": os.getpid(),
        "bridge_heartbeat": True,
        "play_prompt_ready": True,
        "controller_observation": controller,
        "offline_qualification": _qualification_ref(campaign),
        "capabilities": {
            "bridge": True,
            "play": True,
            "arm": True,
            "motion": True,
            "zero": False,
            "tare": False,
        },
        "authorization": {
            "path": str(authorization),
            "sha256": hashlib.sha256(authorization.read_bytes()).hexdigest(),
        },
        "blocker": None,
        "next_action": "press Play once",
    }
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))

    observed = status.read_run_status(campaign)

    assert observed["controller_identity_fresh"] is False
    assert observed["state"] == "BRIDGE_ALIVE_NO_ARM"
    assert observed["play_prompt_ready"] is False
    assert observed["blocker"] == "MANUAL_CONTROLLER_IDENTITY_STALE"


def test_status_revokes_play_prompt_without_exact_capability_scope(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    _write_bridge_heartbeat(output)
    status.activate(campaign, output)
    machine = {
        "schema": status.STATUS_SCHEMA,
        "state": "WAITING_FOR_IDENTITY_PLAY",
        "release_sha": "a" * 64,
        "campaign_id": "manual-test",
        "launch_attempt_id": "attempt-1",
        "output_root": str(output),
        "bridge_pid": os.getpid(),
        "bridge_heartbeat": True,
        "play_prompt_ready": True,
        "offline_qualification": _qualification_ref(campaign),
        "capabilities": {
            "bridge": True,
            "play": True,
            "arm": True,
            "motion": False,
            "zero": False,
            "tare": False,
        },
        "blocker": None,
        "next_action": "press Play once",
    }
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))
    observed = status.read_status(campaign)
    assert observed["state"] == "BRIDGE_ALIVE_NO_ARM"
    assert observed["play_prompt_ready"] is False
    assert observed["blocker"] == "AUTHORIZATION_SCOPE_INSUFFICIENT"


def test_status_fails_closed_when_bridge_pid_is_dead(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    _write_bridge_heartbeat(output)
    status.activate(campaign, output)
    machine = {
        "schema": status.STATUS_SCHEMA,
        "state": "RUNNING",
        "release_sha": "a" * 64,
        "campaign_id": "manual-test",
        "launch_attempt_id": "attempt-1",
        "output_root": str(output),
        "bridge_pid": 2_147_483_647,
        "bridge_heartbeat": True,
        "play_prompt_ready": False,
        "offline_qualification": _qualification_ref(campaign),
        "blocker": None,
        "next_action": "none",
    }
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))
    observed = status.read_status(campaign)
    assert observed["state"] == "BLOCKED"
    assert observed["blocker"] == "BRIDGE_HEARTBEAT_LOST"


def test_status_invalidates_full_qualification_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    _write_bridge_heartbeat(output)
    status.activate(campaign, output)
    machine = {
        "schema": status.STATUS_SCHEMA,
        "state": "WAITING_FOR_IDENTITY_PLAY",
        "release_sha": "a" * 64,
        "campaign_id": "manual-test",
        "launch_attempt_id": "attempt-1",
        "output_root": str(output),
        "bridge_pid": os.getpid(),
        "bridge_heartbeat": True,
        "play_prompt_ready": True,
        "offline_qualification": _qualification_ref(campaign),
        "capabilities": {
            "bridge": True,
            "play": True,
            "arm": True,
            "motion": True,
            "zero": False,
            "tare": False,
        },
        "blocker": None,
        "next_action": "press Play once",
    }
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))
    monkeypatch.setattr(
        status,
        "validate_manual_qualification",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("host drift")),
    )

    observed = status.read_status(campaign)

    assert observed["offline_proven"] is False
    assert observed["state"] == "BLOCKED"
    assert observed["blocker"] == "MANUAL_PRODUCTION_QUALIFICATION_INVALID"
