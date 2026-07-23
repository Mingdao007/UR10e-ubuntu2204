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
from step5d_autotune_v3.governance import (  # noqa: E402
    LAUNCH_ATTEMPT_PHASES,
    read_proc_starttime_ticks,
)


@pytest.fixture(autouse=True)
def _validated_manual_qualification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status,
        "validate_manual_qualification",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        status,
        "writer_lease_owner",
        lambda _profile: {
            "schema": "ur10e/live-writer-lease-owner-v1",
            "pid": os.getpid(),
            "starttime_ticks": read_proc_starttime_ticks(os.getpid()),
            "task": "step5d-manual-no-arm-bridge",
            "acquired_at": "fixture",
        },
    )


def _bridge_process_fields() -> dict[str, object]:
    starttime = read_proc_starttime_ticks(os.getpid())
    return {
        "bridge_pid": os.getpid(),
        "bridge_starttime_ticks": starttime,
        "bridge_owner_pid": os.getpid(),
        "bridge_owner_starttime_ticks": starttime,
        "bridge_launch_id": "1" * 32,
    }


def _qualification_ref(campaign: Path) -> dict[str, str]:
    path = campaign / "manual-qualification.json"
    path.write_text(
        json.dumps(
            {
                "schema": status.QUALIFICATION_SCHEMA,
                "ok": True,
                "state": "MANUAL_PRODUCTION_SECOND_GROUP_RUN_PROVEN",
                "manual_release_manifest_sha256": "a" * 64,
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
        "program_state_normalized": "STOPPED",
        "safety_mode": "Safetymode: NORMAL",
        "safety_mode_normalized": "NORMAL",
        "expected_loaded_program": expected,
    }


def _write_bridge_heartbeat(output: Path) -> None:
    path = output / "runtime/bridge/bridge_rtde_500hz.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "write_index,t_wall_ns,heartbeat,command,step4e_controller_state,ur_safety_mode\n"
        f"1,{time.time_ns()},1,0,0,1\n"
        f"2,{time.time_ns()},2,0,0,1\n",
        encoding="utf-8",
    )
    (path.parent / "bridge_ready.json").write_text(
        json.dumps(
            {"ok": True, "pid": os.getpid(), "launch_nonce": "1" * 32}
        ),
        encoding="utf-8",
    )


def test_manual_shell_records_only_post_route_authority_phases() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")
    phases = (
        "runtime_gate",
        "manual_qualification",
        "manual_context",
        "manual_preflight",
        "manual_bridge_start",
        "manual_campaign",
    )
    for phase in phases:
        assert phase in LAUNCH_ATTEMPT_PHASES
        if phase == "runtime_gate":
            assert 'launch_attempt_phase="runtime_gate"' in source
            assert "bridge_record_launch_attempt STARTED runtime_gate" in source
        else:
            assert f"bridge_begin_phase {phase}" in source
    assert "route_resolve" in LAUNCH_ATTEMPT_PHASES
    assert "bridge_begin_phase route_resolve" not in source
    route_observation = source.index(
        '"${EXPERIMENT_ROOT}/tools/resolve_step5d_bridge_route.py"'
    )
    manual_branch = source.index('if [[ "${bridge_route}" == "manual_v2" ]]')
    authority = source.index("bridge_acquire_authority", manual_branch)
    assert route_observation < authority


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
    expected = (
        "/programs/andyl/kunwei/step5/"
        "step5d_strict_rnn_autotune_v3_r012.urp"
    )
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


@pytest.mark.parametrize(
    ("program_id", "expected_mode"),
    (
        ("step5d_strict_rnn_autotune_v3_r012", "active"),
        ("step5d_strict_rnn_autotune_v3_r011", "recovery"),
        ("step5d_strict_rnn_autotune_v3_r010", "recovery"),
        ("step5d_strict_rnn_autotune_v3_r009", "recovery"),
    ),
)
def test_v3_delivery_recovery_route_precedes_current_release_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    program_id: str,
    expected_mode: str,
) -> None:
    root = tmp_path / "experiment"
    for relative in (
        Path("config/step5d/v3_active_surface.json"),
        Path("config/step5d/current.json"),
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())
    pointer = json.loads((root / "config/step5d/current.json").read_text())
    manifest = Path(pointer["manifest_path"])
    destination = root / manifest
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((ROOT / manifest).read_bytes())

    current = route.load_current_release_snapshot(root)
    assert current.valid is False
    assert current.error == "GovernanceError:TP runtime identity fields differ"

    monkeypatch.setattr(
        route,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {
            "programState": "STOPPED",
            "safetymode": "Safetymode: NORMAL",
            "get loaded program": (
                "Loaded program: /programs/andyl/kunwei/step5/"
                f"{program_id}.urp"
            ),
        },
    )

    result = route.resolve(root=root, robot_host="192.0.2.1", timeout_s=0.1)

    assert result["route"] == "autotune_v3"
    assert result["autotune_route_mode"] == expected_mode
    assert result["autotune_release_manifest_sha256"] is None


def test_status_recomputes_bridge_heartbeat(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    _write_bridge_heartbeat(output)
    status.activate(campaign, output)
    machine = {
        "schema": status.STATUS_SCHEMA,
        "state": "WAITING_FOR_PLAY",
        "release_sha": "a" * 64,
        "campaign_id": "manual-test",
        "launch_attempt_id": "attempt-1",
        "output_root": str(output),
        **_bridge_process_fields(),
        "bridge_heartbeat": False,
        "play_prompt_ready": True,
        "canonical_attempt_bound": True,
        "controller_observation": _controller_observation(),
        "offline_qualification": _qualification_ref(campaign),
        "blocker": None,
        "next_action": "press Play once",
    }
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))
    observed = status.read_status(campaign)
    assert observed["bridge_heartbeat"] is True
    assert observed["state"] == "WAITING_FOR_PLAY"

    machine["bridge_starttime_ticks"] += 1
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))
    reused = status.read_status(campaign)
    assert reused["bridge_heartbeat"] is False
    assert reused["blocker"] == "BRIDGE_HEARTBEAT_LOST"
    machine["bridge_starttime_ticks"] -= 1
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))

    csv_path = output / "runtime/bridge/bridge_rtde_500hz.csv"
    csv_path.write_text(
        "write_index,t_wall_ns,heartbeat,command,step4e_controller_state,ur_safety_mode\n"
        f"2,{time.time_ns()},2,0,0,1\n"
        f"2,{time.time_ns()},2,0,0,1\n",
        encoding="utf-8",
    )
    stalled = status.read_status(campaign)
    assert stalled["bridge_heartbeat"] is False
    assert stalled["blocker"] == "BRIDGE_HEARTBEAT_LOST"
    _write_bridge_heartbeat(output)

    stale_ns = time.time_ns() - status.BRIDGE_HEARTBEAT_MAX_AGE_NS - 1
    os.utime(csv_path, ns=(stale_ns, stale_ns))
    stale = status.read_status(campaign)
    assert stale["bridge_heartbeat"] is False
    assert stale["state"] == "BLOCKED"
    assert stale["blocker"] == "BRIDGE_HEARTBEAT_LOST"


def test_status_rejects_touched_mtime_with_stale_row_timestamp(tmp_path: Path) -> None:
    output = tmp_path / "output"
    _write_bridge_heartbeat(output)
    csv_path = output / "runtime/bridge/bridge_rtde_500hz.csv"
    stale_ns = time.time_ns() - status.BRIDGE_HEARTBEAT_MAX_AGE_NS - 1
    csv_path.write_text(
        "write_index,t_wall_ns,heartbeat,command,step4e_controller_state,ur_safety_mode\n"
        f"1,{stale_ns},1,0,0,1\n"
        f"2,{stale_ns},2,0,0,1\n",
        encoding="utf-8",
    )
    assert status._bridge_heartbeat(
        {"output_root": str(output), **_bridge_process_fields()}
    ) is False


def test_status_rejects_unterminated_partial_heartbeat_row(tmp_path: Path) -> None:
    output = tmp_path / "output"
    _write_bridge_heartbeat(output)
    csv_path = output / "runtime/bridge/bridge_rtde_500hz.csv"
    with csv_path.open("a", encoding="utf-8") as stream:
        stream.write(f"3,{time.time_ns()},3,0")
    assert status._bridge_heartbeat(
        {"output_root": str(output), **_bridge_process_fields()}
    ) is False


def test_status_keeps_one_bound_controller_preflight_without_age_expiry(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    _write_bridge_heartbeat(output)
    controller = _controller_observation()
    controller["observed_at_unix_ns"] = 1
    machine = {
        "schema": status.STATUS_SCHEMA,
        "state": "WAITING_FOR_PLAY",
        "release_sha": "a" * 64,
        "campaign_id": "manual-test",
        "launch_attempt_id": "attempt-1",
        "output_root": str(output),
        **_bridge_process_fields(),
        "bridge_heartbeat": True,
        "play_prompt_ready": True,
        "canonical_attempt_bound": True,
        "controller_observation": controller,
        "offline_qualification": _qualification_ref(campaign),
        "blocker": None,
        "next_action": "press Play once",
    }
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))

    observed = status.read_run_status(campaign)

    assert observed["controller_preflight_valid"] is True
    assert observed["state"] == "WAITING_FOR_PLAY"
    assert observed["play_prompt_ready"] is True
    assert observed["blocker"] is None


def test_status_blocks_without_exact_controller_preflight(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    _write_bridge_heartbeat(output)
    status.activate(campaign, output)
    machine = {
        "schema": status.STATUS_SCHEMA,
        "state": "WAITING_FOR_PLAY",
        "release_sha": "a" * 64,
        "campaign_id": "manual-test",
        "launch_attempt_id": "attempt-1",
        "output_root": str(output),
        **_bridge_process_fields(),
        "bridge_heartbeat": True,
        "play_prompt_ready": True,
        "canonical_attempt_bound": True,
        "offline_qualification": _qualification_ref(campaign),
        "controller_observation": {
            **_controller_observation(),
            "program_state_normalized": "PLAYING",
        },
        "blocker": None,
        "next_action": "press Play once",
    }
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))
    observed = status.read_status(campaign)
    assert observed["state"] == "BLOCKED"
    assert observed["play_prompt_ready"] is False
    assert observed["blocker"] == "MANUAL_CONTROLLER_PREFLIGHT_INVALID"


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
        **_bridge_process_fields(),
        "bridge_pid": 2_147_483_647,
        "bridge_starttime_ticks": 1,
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
        "state": "WAITING_FOR_PLAY",
        "release_sha": "a" * 64,
        "campaign_id": "manual-test",
        "launch_attempt_id": "attempt-1",
        "output_root": str(output),
        **_bridge_process_fields(),
        "bridge_heartbeat": True,
        "play_prompt_ready": True,
        "canonical_attempt_bound": True,
        "offline_qualification": _qualification_ref(campaign),
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
