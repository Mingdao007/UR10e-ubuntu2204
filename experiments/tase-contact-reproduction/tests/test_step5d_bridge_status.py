from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_bridge_status as bridge_status  # noqa: E402
import step5d_manual_status as manual_status  # noqa: E402
from step5d_autotune_v3.governance import (  # noqa: E402
    publish_launch_attempt,
    read_proc_starttime_ticks,
)


@pytest.fixture(autouse=True)
def _validated_manual_qualification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        manual_status,
        "validate_manual_qualification",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        manual_status,
        "load_capability_authorization",
        lambda *_args, **_kwargs: {"capabilities": _capabilities(motion=True)},
    )


def _capabilities(*, motion: bool) -> dict[str, bool]:
    return {
        "bridge": True,
        "play": motion,
        "arm": motion,
        "motion": motion,
        "zero": False,
        "tare": False,
    }


def _bindings(
    campaign: Path, output: Path, snapshot: Path, *, motion: bool
) -> dict[str, object]:
    return {
        "repository_head": "a" * 40,
        "runtime_environment_id": hashlib.sha256(b"runtime").hexdigest(),
        "campaign_root": str(campaign),
        "output_root": str(output),
        "resource_owner": {
            "pid": os.getpid(),
            "starttime_ticks": read_proc_starttime_ticks(os.getpid()),
            "authority_epoch": 1,
        },
        "capabilities": _capabilities(motion=motion),
        "route_snapshot": {
            "path": str(snapshot),
            "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        },
    }


def _owner_authority(authority: Path, attempt_id: str) -> None:
    authority.mkdir(parents=True, exist_ok=True)
    (authority / "owner-authority.json").write_text(
        json.dumps(
            {
                "schema": "step5d.bridge/owner-authority-v1",
                "sequence": 1,
                "state": "ACTIVE",
                "attempt_id": attempt_id,
                "owner": {
                    "pid": os.getpid(),
                    "starttime_ticks": read_proc_starttime_ticks(os.getpid()),
                },
                "activated_at_unix_ns": 1,
                "revoked_at_unix_ns": None,
                "reason": None,
            }
        ),
        encoding="utf-8",
    )


def _route_snapshot(path: Path, route: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "step5d.bridge-route/v2",
                "route": route,
                "read_only": True,
            }
        ),
        encoding="utf-8",
    )


def _manual_status(campaign: Path, *, motion: bool, attempt_id: str) -> None:
    authorization = campaign / "authorization.json"
    authorization.write_text("{}\n", encoding="utf-8")
    qualification = campaign / "manual-qualification.json"
    qualification.write_text(
        json.dumps(
            {
                "schema": "step5d.manual-v2/production-startup-qualification-v1",
                "ok": True,
                "state": "MANUAL_BRIDGE_PERSISTENT_NO_ARM_PROVEN",
                "manual_release_manifest_sha256": "a" * 64,
                "play_prompt_ready": False,
            }
        ),
        encoding="utf-8",
    )
    bridge_csv = campaign.parent / "output/runtime/bridge/bridge_rtde_500hz.csv"
    bridge_csv.parent.mkdir(parents=True, exist_ok=True)
    bridge_csv.write_text("heartbeat\n1\n", encoding="utf-8")
    (campaign / "manual_governed_status.json").write_text(
        json.dumps(
            {
                "schema": "step5d.manual-v2/governed-status-v1",
                "state": "WAITING_FOR_IDENTITY_PLAY",
                "release_sha": "a" * 64,
                "campaign_id": "manual-test",
                "launch_attempt_id": attempt_id,
                "output_root": str(campaign.parent / "output"),
                "bridge_pid": os.getpid(),
                "bridge_heartbeat": True,
                "play_prompt_ready": True,
                "controller_observation": {
                    "observed_at_unix_ns": time.time_ns(),
                    "loaded_program_response": (
                        "Loaded program: /programs/andyl/kunwei/step5/"
                        "step5d_strict_rnn_manual_tune_v2.urp"
                    ),
                    "program_state": "STOPPED",
                    "safety_mode": "Safetymode: NORMAL",
                    "expected_loaded_program": (
                        "/programs/andyl/kunwei/step5/"
                        "step5d_strict_rnn_manual_tune_v2.urp"
                    ),
                },
                "capabilities": _capabilities(motion=motion),
                "authorization": {
                    "path": str(authorization),
                    "sha256": hashlib.sha256(authorization.read_bytes()).hexdigest(),
                },
                "offline_qualification": {
                    "path": str(qualification),
                    "sha256": hashlib.sha256(qualification.read_bytes()).hexdigest(),
                },
                "blocker": None,
                "next_action": "press Play once",
            }
        ),
        encoding="utf-8",
    )


def test_route_neutral_status_cannot_promote_bridge_only_manual_attempt(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    authority = tmp_path / bridge_status.AUTHORITY_RELATIVE
    authority.parent.mkdir(parents=True)
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    _route_snapshot(snapshot, "manual_v2")
    _manual_status(campaign, motion=False, attempt_id="attempt-1")
    _owner_authority(authority, "attempt-1")
    publish_launch_attempt(
        authority,
        attempt_id="attempt-1",
        state="STARTED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=_bindings(campaign, output, snapshot, motion=False),
    )

    status = bridge_status.resolve_status(tmp_path)
    assert status["schema"] == bridge_status.STATUS_SCHEMA
    assert status["route"] == "manual_v2"
    assert status["state"] == "BRIDGE_ALIVE_NO_ARM"
    assert status["predicates"]["play_prompt_ready"] is False
    assert status["predicates"]["authorization_scope_valid"] is False
    with pytest.raises(ValueError, match="does not authorize"):
        bridge_status.readiness_claim(status, "WAITING_FOR_IDENTITY_PLAY")


def test_readiness_claim_requires_same_attempt_machine_scope(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    authority = tmp_path / bridge_status.AUTHORITY_RELATIVE
    authority.parent.mkdir(parents=True)
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    _route_snapshot(snapshot, "manual_v2")
    _manual_status(campaign, motion=True, attempt_id="attempt-2")
    _owner_authority(authority, "attempt-2")
    publish_launch_attempt(
        authority,
        attempt_id="attempt-2",
        state="STARTED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=_bindings(campaign, output, snapshot, motion=True),
    )

    status = bridge_status.resolve_status(tmp_path)
    claim = bridge_status.readiness_claim(status, "WAITING_FOR_IDENTITY_PLAY")
    assert claim["schema"] == bridge_status.CLAIM_SCHEMA
    assert claim["attempt_id"] == "attempt-2"
    assert claim["expires_at_unix_ns"] > claim["issued_at_unix_ns"]


def test_failed_latest_attempt_hides_stale_route_status(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    authority = tmp_path / bridge_status.AUTHORITY_RELATIVE
    authority.parent.mkdir(parents=True)
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    _route_snapshot(snapshot, "manual_v2")
    _manual_status(campaign, motion=True, attempt_id="attempt-3")
    _owner_authority(authority, "attempt-3")
    bindings = _bindings(campaign, output, snapshot, motion=True)
    publish_launch_attempt(
        authority,
        attempt_id="attempt-3",
        state="STARTED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=bindings,
    )
    publish_launch_attempt(
        authority,
        attempt_id="attempt-3",
        state="FAILED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=bindings,
        exit_code=2,
        reason_code="LAUNCH_ATTEMPT_FAILED",
        detail="manual child exited",
    )

    status = bridge_status.resolve_status(tmp_path)
    assert status["state"] is None
    assert status["predicates"]["play_prompt_ready"] is False
    assert status["blocker"]["reason_codes"] == ["LAUNCH_ATTEMPT_FAILED"]


def test_closed_world_route_failure_preserves_named_physical_blocker(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    authority = tmp_path / bridge_status.AUTHORITY_RELATIVE
    authority.parent.mkdir(parents=True)
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    _route_snapshot(snapshot, "BLOCKED")
    bindings = _bindings(campaign, output, snapshot, motion=False)
    route_reference = bindings["route_snapshot"]
    bindings["route_snapshot"] = None
    publish_launch_attempt(
        authority,
        attempt_id="attempt-unsupported",
        state="STARTED",
        phase="route_resolve",
        route="UNKNOWN",
        bindings=bindings,
    )
    bindings["route_snapshot"] = route_reference
    publish_launch_attempt(
        authority,
        attempt_id="attempt-unsupported",
        state="FAILED",
        phase="route_resolve",
        route="BLOCKED",
        bindings=bindings,
        exit_code=3,
        reason_code="LOADED_PROGRAM_UNSUPPORTED",
        detail="loaded program is outside the closed route set",
    )

    status = bridge_status.resolve_status(tmp_path)

    assert status["route"] == "BLOCKED"
    assert status["blocker"]["class"] == "PHYSICAL"
    assert status["blocker"]["reason_codes"] == ["LOADED_PROGRAM_UNSUPPORTED"]
    assert status["next_action"] == "load_exact_supported_program_before_retry"


def test_status_without_canonical_attempt_never_reuses_stale_v3_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bridge_status,
        "resolve_governed_status",
        lambda *_args, **_kwargs: pytest.fail("stale V3 status must not be consulted"),
    )

    status = bridge_status.resolve_status(tmp_path)

    assert status["state"] is None
    assert status["predicates"]["play_prompt_ready"] is False
    assert status["blocker"]["reason_codes"] == ["NO_CANONICAL_LAUNCH_ATTEMPT"]
    assert status["next_action"] == "start_canonical_bridge"


def test_passed_phase_with_dead_owner_is_not_a_readiness_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    authority = tmp_path / bridge_status.AUTHORITY_RELATIVE
    authority.parent.mkdir(parents=True)
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    _route_snapshot(snapshot, "manual_v2")
    _manual_status(campaign, motion=True, attempt_id="attempt-dead")
    _owner_authority(authority, "attempt-dead")
    bindings = _bindings(campaign, output, snapshot, motion=True)
    publish_launch_attempt(
        authority,
        attempt_id="attempt-dead",
        state="STARTED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=bindings,
    )
    publish_launch_attempt(
        authority,
        attempt_id="attempt-dead",
        state="PASSED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=bindings,
    )
    monkeypatch.setattr(bridge_status, "read_proc_starttime_ticks", lambda _pid: None)

    status = bridge_status.resolve_status(tmp_path)

    assert status["state"] is None
    assert status["blocker"]["reason_codes"] == ["LAUNCH_ATTEMPT_BINDING_INVALID"]


def test_route_snapshot_content_must_match_bound_route(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    authority = tmp_path / bridge_status.AUTHORITY_RELATIVE
    authority.parent.mkdir(parents=True)
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    _route_snapshot(snapshot, "autotune_v3")
    _manual_status(campaign, motion=True, attempt_id="attempt-route-mismatch")
    _owner_authority(authority, "attempt-route-mismatch")
    publish_launch_attempt(
        authority,
        attempt_id="attempt-route-mismatch",
        state="STARTED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=_bindings(campaign, output, snapshot, motion=True),
    )

    status = bridge_status.resolve_status(tmp_path)

    assert status["state"] is None
    assert status["blocker"]["reason_codes"] == ["LAUNCH_ATTEMPT_BINDING_INVALID"]


def test_v3_campaign_lease_is_the_authorization_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    authority = tmp_path / bridge_status.AUTHORITY_RELATIVE
    authority.parent.mkdir(parents=True)
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    _route_snapshot(snapshot, "autotune_v3")
    _owner_authority(authority, "attempt-v3")
    publish_launch_attempt(
        authority,
        attempt_id="attempt-v3",
        state="STARTED",
        phase="live_handoff",
        route="autotune_v3",
        bindings=_bindings(campaign, output, snapshot, motion=False),
    )
    monkeypatch.setattr(
        bridge_status,
        "resolve_governed_status",
        lambda *_args, **_kwargs: {
            "schema": "legacy",
            "state": "WAITING_FOR_PLAY",
            "predicates": {
                "offline_proven": True,
                "lease_valid": True,
                "play_prompt_ready": True,
            },
            "blocker": {"class": None, "reason_codes": [], "evidence": []},
            "next_action": "press_play_or_stop",
        },
    )

    status = bridge_status.resolve_status(tmp_path)
    claim = bridge_status.readiness_claim(status, "WAITING_FOR_PLAY")

    assert status["predicates"]["authorization_scope_valid"] is True
    assert claim["attempt_id"] == "attempt-v3"


def test_completed_attempt_preserves_outcome_but_cannot_claim_readiness(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    authority = tmp_path / bridge_status.AUTHORITY_RELATIVE
    authority.parent.mkdir(parents=True)
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    _route_snapshot(snapshot, "manual_v2")
    _manual_status(campaign, motion=True, attempt_id="attempt-complete")
    manual_path = campaign / "manual_governed_status.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    manual["state"] = "COMPLETE"
    manual["play_prompt_ready"] = False
    manual["next_action"] = "none"
    manual_path.write_text(json.dumps(manual), encoding="utf-8")
    _owner_authority(authority, "attempt-complete")
    bindings = _bindings(campaign, output, snapshot, motion=True)
    publish_launch_attempt(
        authority,
        attempt_id="attempt-complete",
        state="STARTED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=bindings,
    )
    publish_launch_attempt(
        authority,
        attempt_id="attempt-complete",
        state="COMPLETED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=bindings,
    )
    owner = json.loads((authority / "owner-authority.json").read_text())
    owner.update(
        {
            "sequence": 2,
            "state": "REVOKED",
            "revoked_at_unix_ns": 2,
            "reason": "completed",
        }
    )
    (authority / "owner-authority.json").write_text(json.dumps(owner))

    status = bridge_status.resolve_status(tmp_path)

    assert status["state"] == "COMPLETE"
    assert status["predicates"]["play_prompt_ready"] is False
    assert status["predicates"]["authorization_scope_valid"] is False
    with pytest.raises(ValueError, match="pre-Play"):
        bridge_status.readiness_claim(status, "COMPLETE")
