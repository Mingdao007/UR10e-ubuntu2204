from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import preflight_step5d_manual_bridge as preflight  # noqa: E402
import run_step5d_manual_bridge as wrapper  # noqa: E402
import run_step5d_manual_bridge_live as live  # noqa: E402
import step5d_manual_bridge as bridge  # noqa: E402
from step5d_manual_atomic_release import canonical_bytes  # noqa: E402


def _fake_release() -> tuple[dict, dict]:
    manifest = {
        "identity": {
            "parent_r009_commit": "b" * 40,
            "parent_r009_release_manifest_sha256": "c" * 64,
        },
        "artifacts": {
            extension: {"path": f"program{extension}", "sha256": character * 64}
            for extension, character in ((".script", "1"), (".txt", "2"), (".urp", "3"))
        },
        "source_fingerprints": {"tools/manual.py": "4" * 64},
    }
    return {"manifest_sha256": "a" * 64}, manifest


def test_context_is_write_once_digest_bound_and_no_arm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launch = tmp_path / "launch.json"
    launch.write_text("{}")
    monkeypatch.setattr(bridge, "_release_document", lambda _root: _fake_release())
    monkeypatch.setattr(bridge, "load_launch_profile", lambda _path: SimpleNamespace(fingerprint="5" * 64))
    now = datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc)
    payload = bridge.build_context(tmp_path, plant_epoch=1, launch_profile_path=launch, now=now)
    assert payload["program"] == "step5d_strict_rnn_manual_tune_v1"
    assert payload["protocol"] == "v3_full_home_manual_hold_v1"
    assert payload["wire_protocol"] == "v3_full_home_rolling_arm_v1"
    assert payload["bridge_authorized"] is True
    assert payload["arm_authorized"] is False
    assert payload["motion_authorized"] is False
    output = tmp_path / "context.json"
    bridge.write_once(output, payload)
    with pytest.raises(bridge.ManualBridgeError, match="already exists"):
        bridge.write_once(output, payload)
    loaded = bridge.load_context(tmp_path, output, now=now)
    assert loaded == payload
    tampered = json.loads(output.read_text())
    tampered["arm_authorized"] = True
    output.write_text(json.dumps(tampered))
    with pytest.raises(bridge.ManualBridgeError, match="digest"):
        bridge.load_context(tmp_path, output, now=now)


def test_preflight_requires_exact_manual_program() -> None:
    stopped = preflight._program_safe(
        {"programState": "STOPPED /programs/andyl/kunwei/step5/step5d_strict_rnn_manual_tune_v1.urp"},
        {},
    )
    wrong = preflight._program_safe(
        {"programState": "STOPPED /programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3_r009.urp"},
        {},
    )
    assert stopped["ok"] is True
    assert wrong["ok"] is False


def test_live_preflight_validation_is_no_arm_and_exact() -> None:
    predicates = {
        name: {"ok": True}
        for name in (
            "safety_normal", "program_safe_for_bridge", "robot_stationary",
            "prealign_start_clearance", "no_existing_writer", "mailbox_initial_zero",
            "runtime_dependencies",
        )
    }
    context = {"manual_release_manifest_sha256": "a" * 64}
    payload = {
        "schema": bridge.PREFLIGHT_SCHEMA,
        "ok": True,
        "fresh": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_stage_id": bridge.RELEASE_STAGE,
        "control_profile_id": bridge.CONTROL_PROFILE,
        "tp_program_id": bridge.PROGRAM,
        "manual_release_manifest_sha256": "a" * 64,
        "predicates": predicates,
    }
    path = ROOT / "tests" / ".manual-preflight-never-written.json"
    original = live.strict_object
    try:
        live.strict_object = lambda _path, _role: payload
        assert live._validate_preflight(path, context)["ok"] is True
        payload["tp_program_id"] = "step5d_strict_rnn_autotune_v3_r009"
        with pytest.raises(bridge.ManualBridgeError):
            live._validate_preflight(path, context)
    finally:
        live.strict_object = original


def test_runtime_ticket_binds_parent_argv_context_and_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context_path = tmp_path / "context.json"
    preflight_path = tmp_path / "preflight.json"
    context_path.write_text("{}")
    preflight_path.write_text(json.dumps({
        "ok": True,
        "fresh": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tp_program_id": bridge.PROGRAM,
        "manual_release_manifest_sha256": "a" * 64,
        "bridge_start_context_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
    }))
    monkeypatch.setattr(wrapper, "load_context", lambda _root, _path: {
        "manual_release_manifest_sha256": "a" * 64
    })
    argv = ["--bridge-profile", bridge.CONTROL_PROFILE]
    ticket = {
        "schema": bridge.TICKET_SCHEMA,
        "parent_pid": os.getppid(),
        "argv_sha256": wrapper._argv_sha256(argv),
        "launch_id": "1" * 32,
        "scope": wrapper.TICKET_SCOPE,
        "program": bridge.PROGRAM,
        "protocol": bridge.PROTOCOL,
        "wire_protocol": bridge.WIRE_PROTOCOL,
        "control_profile_id": bridge.CONTROL_PROFILE,
        "release_stage_id": bridge.RELEASE_STAGE,
        "manual_release_manifest_sha256": "a" * 64,
        "bridge_start_context": {
            "path": str(context_path),
            "sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
        },
        "preflight": {
            "path": str(preflight_path),
            "sha256": hashlib.sha256(preflight_path.read_bytes()).hexdigest(),
        },
    }
    ticket_path = tmp_path / "ticket.json"
    ticket_path.write_text(json.dumps(ticket))
    assert wrapper.strict_ticket(ticket_path, argv)["scope"] == "manual_bridge_no_arm"
    ticket["program"] = "step5d_strict_rnn_autotune_v3_r009"
    ticket_path.write_text(json.dumps(ticket))
    with pytest.raises(bridge.ManualBridgeError, match="identity"):
        wrapper.strict_ticket(ticket_path, argv)


def test_manual_authorization_seam_is_bridge_only_no_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bridge = SimpleNamespace(require_v29_live_bridge_authorization=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wrapper.r009_bridge, "install_v3_seams", lambda *_args, **_kwargs: fake_bridge)
    monkeypatch.setattr(wrapper, "load_context", lambda _root, _path: {
        "arm_authorized": False,
        "motion_authorized": False,
    })
    installed = wrapper.install_manual_seams({
        "scope": wrapper.TICKET_SCOPE,
        "program": bridge.PROGRAM,
        "protocol": bridge.PROTOCOL,
        "wire_protocol": bridge.WIRE_PROTOCOL,
        "bridge_start_context": {"path": "/tmp/manual-context.json", "sha256": "a" * 64},
    })
    result = installed.require_v29_live_bridge_authorization(SimpleNamespace(
        bridge_profile=bridge.CONTROL_PROFILE,
        step5d_autotune_command_mailbox=Path("/tmp/manual-mailbox.json"),
        step5d_stage25_control_mode="speedj_rnn_live",
    ))
    assert result["scope"] == "manual_bridge_no_arm"
    assert result["protocol_id"] == bridge.PROTOCOL
    assert result["wire_protocol_id"] == bridge.WIRE_PROTOCOL
    assert result["live_motion_authorized"] is False
