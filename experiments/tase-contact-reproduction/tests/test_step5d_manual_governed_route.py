from __future__ import annotations

import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import resolve_step5d_bridge_route as route  # noqa: E402
import step5d_manual_status as status  # noqa: E402
from step5d_autotune_v3.governance import LAUNCH_ATTEMPT_PHASES  # noqa: E402


def test_manual_shell_phases_are_registered_in_launch_attempt_fsm() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")
    phases = (
        "runtime_gate",
        "route_resolve",
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


def test_route_leaves_every_other_loaded_program_on_v3(monkeypatch) -> None:
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
        "load_manual_release",
        lambda _root: (_ for _ in ()).throw(AssertionError("must not verify manual release")),
    )
    result = route.resolve(root=ROOT, robot_host="192.0.2.1", timeout_s=0.1)
    assert result["route"] == "autotune_v3"
    assert result["manual_release_manifest_sha256"] is None


def test_status_recomputes_bridge_heartbeat(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    status.activate(campaign, output)
    machine = {
        "schema": status.STATUS_SCHEMA,
        "state": "WAITING_FOR_IDENTITY_PLAY",
        "bridge_pid": os.getpid(),
        "bridge_heartbeat": False,
        "play_prompt_ready": True,
        "blocker": None,
        "next_action": "press Play once",
    }
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))
    observed = status.read_status(campaign)
    assert observed["bridge_heartbeat"] is True
    assert observed["state"] == "WAITING_FOR_IDENTITY_PLAY"


def test_status_fails_closed_when_bridge_pid_is_dead(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    status.activate(campaign, output)
    machine = {
        "schema": status.STATUS_SCHEMA,
        "state": "RUNNING",
        "bridge_pid": 2_147_483_647,
        "bridge_heartbeat": True,
        "play_prompt_ready": False,
        "blocker": None,
        "next_action": "none",
    }
    (campaign / "manual_governed_status.json").write_text(json.dumps(machine))
    observed = status.read_status(campaign)
    assert observed["state"] == "BLOCKED"
    assert observed["blocker"] == "BRIDGE_HEARTBEAT_LOST"
