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
import step5d_bridge_authority as authority  # noqa: E402
from step5d_autotune_v3.governance import (  # noqa: E402
    publish_launch_attempt,
    LAUNCH_ATTEMPT_SCHEMA,
    read_proc_starttime_ticks,
)


@pytest.fixture(autouse=True)
def _validated_manual_release_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        manual_status,
        "manual_release_contract_scope",
        lambda *_args, **_kwargs: {"release_manifest_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        manual_status,
        "validate_release_contract_result",
        lambda _payload, **kwargs: kwargs["expected_scope"],
    )
    monkeypatch.setattr(
        manual_status,
        "writer_lease_owner",
        lambda _profile: {
            "schema": "ur10e/live-writer-lease-owner-v1",
            "pid": os.getpid(),
            "starttime_ticks": read_proc_starttime_ticks(os.getpid()),
            "task": "step5d-manual-no-arm-bridge",
            "acquired_at": "fixture",
        },
    )
    monkeypatch.setattr(
        bridge_status,
        "release_contract_reference",
        lambda *_args, **_kwargs: {"ok": "contract"},
    )


def _bindings(campaign: Path, output: Path, snapshot: Path) -> dict[str, object]:
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
        "route_snapshot": {
            "path": str(snapshot),
            "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        },
    }


def _owner_authority(authority: Path, attempt_id: str) -> None:
    owner_pid = os.getpid()
    owner_starttime = read_proc_starttime_ticks(owner_pid)
    authority.mkdir(parents=True, exist_ok=True)
    launch_basis = authority / "launch-basis.py"
    launch_basis.write_text("bridge owner launch basis\n", encoding="utf-8")
    _owner_authority_record(
        authority,
        attempt_id=attempt_id,
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
        worktree_root=str(authority),
        repository_head="a" * 40,
        launch_basis_path=str(launch_basis),
        launch_basis_sha256=hashlib.sha256(launch_basis.read_bytes()).hexdigest(),
    )


def _owner_authority_record(
    authority: Path,
    *,
    attempt_id: str,
    owner_pid: int,
    owner_starttime_ticks: int,
    state: str = "ACTIVE",
    sequence: int = 1,
    worktree_root: str | None = None,
    repository_head: str | None = None,
    launch_basis_path: str | None = None,
    launch_basis_sha256: str | None = None,
    reason: str | None = None,
    authority_epoch: int = 1,
    resource_id: str = "step5d-bridge-writer",
) -> None:
    authority.mkdir(parents=True, exist_ok=True)
    (authority / "owner-authority.json").write_text(
        json.dumps(
            {
                "schema": "step5d.bridge/owner-authority-v1",
                "sequence": sequence,
                "authority_epoch": authority_epoch,
                "state": state,
                "attempt_id": attempt_id,
                "owner": {
                    "pid": owner_pid,
                    "starttime_ticks": owner_starttime_ticks,
                },
                "activated_at_unix_ns": 1,
                "revoked_at_unix_ns": None,
                "reason": reason,
                "resource_id": resource_id,
                "worktree_root": worktree_root,
                "repository_head": repository_head,
                "launch_basis_path": launch_basis_path,
                "launch_basis_sha256": launch_basis_sha256,
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


def _manual_status(campaign: Path, *, attempt_id: str) -> None:
    contract = campaign / "manual-release-contract.json"
    contract.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/release-contract-result-v1",
                "ok": True,
                "state": "RELEASE_CONTRACT_PROVEN",
            }
        ),
        encoding="utf-8",
    )
    bridge_csv = campaign.parent / "output/runtime/bridge/bridge_rtde_500hz.csv"
    bridge_csv.parent.mkdir(parents=True, exist_ok=True)
    bridge_csv.write_text(
        "write_index,t_wall_ns,heartbeat,command,step4e_controller_state,ur_safety_mode\n"
        f"1,{time.time_ns()},1,0,0,1\n"
        f"2,{time.time_ns()},2,0,0,1\n",
        encoding="utf-8",
    )
    (bridge_csv.parent / "bridge_ready.json").write_text(
        json.dumps(
            {"ok": True, "pid": os.getpid(), "launch_nonce": "1" * 32}
        ),
        encoding="utf-8",
    )
    starttime = read_proc_starttime_ticks(os.getpid())
    (campaign / "manual_governed_status.json").write_text(
        json.dumps(
            {
                "schema": "step5d.manual-v2/governed-status-v1",
                "state": "WAITING_FOR_PLAY",
                "release_sha": "a" * 64,
                "campaign_id": "manual-test",
                "launch_attempt_id": attempt_id,
                "output_root": str(campaign.parent / "output"),
                "bridge_pid": os.getpid(),
                "bridge_starttime_ticks": starttime,
                "bridge_owner_pid": os.getpid(),
                "bridge_owner_starttime_ticks": starttime,
                "bridge_launch_id": "1" * 32,
                "bridge_heartbeat": True,
                "play_prompt_ready": True,
                "canonical_attempt_bound": True,
                "controller_observation": {
                    "observed_at_unix_ns": time.time_ns(),
                    "loaded_program_response": (
                        "Loaded program: /programs/andyl/kunwei/step5/"
                        "step5d_strict_rnn_manual_tune_v2.urp"
                    ),
                    "program_state": "STOPPED",
                    "program_state_normalized": "STOPPED",
                    "safety_mode": "Safetymode: NORMAL",
                    "safety_mode_normalized": "NORMAL",
                    "expected_loaded_program": (
                        "/programs/andyl/kunwei/step5/"
                        "step5d_strict_rnn_manual_tune_v2.urp"
                    ),
                },
                "release_contract": {
                    "path": str(contract),
                    "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
                },
                "blocker": None,
                "next_action": "press Play once",
            }
        ),
        encoding="utf-8",
    )


def test_status_hard_archives_canonical_manual_attempt(
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
    _manual_status(campaign, attempt_id="attempt-1")
    _owner_authority(authority, "attempt-1")
    publish_launch_attempt(
        authority,
        attempt_id="attempt-1",
        state="STARTED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=_bindings(campaign, output, snapshot),
    )

    status = bridge_status.resolve_status(tmp_path)
    assert status["schema"] == bridge_status.STATUS_SCHEMA
    assert status["route"] == "manual_v2"
    assert status["state"] == "UNPREPARED"
    assert status["blocker"]["reason_codes"] == ["MANUAL_V2_ARCHIVED"]
    assert status["predicates"]["play_prompt_ready"] is False
    assert status["predicates"]["canonical_attempt_bound"] is False
    with pytest.raises(ValueError, match="does not authorize"):
        bridge_status.readiness_claim(status, "WAITING_FOR_PLAY")


def test_archived_manual_attempt_cannot_issue_readiness_claim(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    authority = tmp_path / bridge_status.AUTHORITY_RELATIVE
    authority.parent.mkdir(parents=True)
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    _route_snapshot(snapshot, "manual_v2")
    _manual_status(campaign, attempt_id="attempt-2")
    _owner_authority(authority, "attempt-2")
    publish_launch_attempt(
        authority,
        attempt_id="attempt-2",
        state="STARTED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=_bindings(campaign, output, snapshot),
    )

    status = bridge_status.resolve_status(tmp_path)
    with pytest.raises(ValueError, match="does not authorize"):
        bridge_status.readiness_claim(status, "WAITING_FOR_PLAY")


def test_failed_latest_attempt_hides_stale_route_status(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    authority = tmp_path / bridge_status.AUTHORITY_RELATIVE
    authority.parent.mkdir(parents=True)
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    _route_snapshot(snapshot, "manual_v2")
    _manual_status(campaign, attempt_id="attempt-3")
    _owner_authority(authority, "attempt-3")
    bindings = _bindings(campaign, output, snapshot)
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
    assert status["state"] == "TERMINAL"
    assert status["compatibility_phase"] is None
    assert status["predicates"]["play_prompt_ready"] is False
    assert status["blocker"]["reason_codes"] == ["LAUNCH_ATTEMPT_FAILED"]


def test_status_without_canonical_attempt_never_reuses_stale_v3_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bridge_status,
        "resolve_governed_status",
        lambda *_args, **_kwargs: pytest.fail("stale V3 status must not be consulted"),
    )

    status = bridge_status.resolve_status(tmp_path)

    assert status["state"] == "UNPREPARED"
    assert status["compatibility_phase"] is None
    assert status["predicates"]["play_prompt_ready"] is False
    assert status["blocker"]["reason_codes"] == ["CURRENT_RELEASE_INVALID"]
    assert status["next_action"] == "repair_current_release_before_retry"


def test_status_without_attempt_projects_delivery_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = type("Release", (), {"manifest_sha256": "a" * 64})()
    delivery = tmp_path / "runs/delivery.json"
    delivery.parent.mkdir()
    delivery.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(bridge_status, "load_current_release", lambda _root: release)
    monkeypatch.setattr(
        bridge_status,
        "resolve_delivery_observation",
        lambda *_args, **_kwargs: (delivery, {"transaction_id": "b" * 32}),
    )
    monkeypatch.setattr(
        bridge_status,
        "resolve_bridge_admission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            bridge_status.BridgeAdmissionError("not observed")
        ),
    )

    status = bridge_status.resolve_status(tmp_path)

    assert status["state"] == "DELIVERED"
    assert status["predicates"]["controller_fresh_get"] is True
    assert status["blocker"]["reason_codes"] == []
    assert status["launch_attempt"]["present"] is False


def test_status_requires_runtime_revalidation_before_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = type("Release", (), {"manifest_sha256": "a" * 64})()
    monkeypatch.setattr(bridge_status, "load_current_release", lambda _root: release)
    monkeypatch.setattr(
        bridge_status,
        "release_contract_reference",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            FileNotFoundError("missing")
        ),
    )

    status = bridge_status.resolve_status(tmp_path)

    assert status["blocker"]["reason_codes"] == ["RELEASE_CERTIFICATE_MISSING"]
    assert status["next_action"] == "run_revalidate_current"


def test_status_without_attempt_does_not_project_load_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = type("Release", (), {"manifest_sha256": "a" * 64})()
    delivery = tmp_path / "runs/delivery.json"
    admission = tmp_path / "runs/admission.json"
    delivery.parent.mkdir()
    delivery.write_text("{}\n", encoding="utf-8")
    admission.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(bridge_status, "load_current_release", lambda _root: release)
    monkeypatch.setattr(
        bridge_status,
        "resolve_delivery_observation",
        lambda *_args, **_kwargs: (delivery, {"transaction_id": "b" * 32}),
    )
    monkeypatch.setattr(
        bridge_status,
        "resolve_bridge_admission",
        lambda *_args, **_kwargs: (
            admission,
            {
                "state": "BRIDGE_START_READY",
                "reason_code": "DELIVERY_VERIFIED",
            },
        ),
    )

    status = bridge_status.resolve_status(tmp_path)

    assert status["blocker"]["reason_codes"] == []
    assert status["next_action"] == "run_bridge_live"
    assert status["launch_attempt"]["present"] is False
    assert status["capabilities"] == {"play_prompt": False}
    assert not (tmp_path / bridge_status.AUTHORITY_RELATIVE).exists()


def test_status_without_attempt_does_not_require_loaded_stopped_milestone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = type("Release", (), {"manifest_sha256": "a" * 64})()
    delivery = tmp_path / "runs/delivery.json"
    admission = tmp_path / "runs/admission.json"
    delivery.parent.mkdir()
    delivery.write_text("{}\n", encoding="utf-8")
    admission.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(bridge_status, "load_current_release", lambda _root: release)
    monkeypatch.setattr(
        bridge_status,
        "resolve_delivery_observation",
        lambda *_args, **_kwargs: (delivery, {"transaction_id": "b" * 32}),
    )
    monkeypatch.setattr(
        bridge_status,
        "resolve_bridge_admission",
        lambda *_args, **_kwargs: (
            admission,
            {
                "state": "BRIDGE_START_READY",
                "reason_code": "DELIVERY_VERIFIED",
            },
        ),
    )

    status = bridge_status.resolve_status(tmp_path)

    assert status["blocker"]["reason_codes"] == []
    assert status["next_action"] == "run_bridge_live"
    assert status["capabilities"] == {"play_prompt": False}
    assert status["milestones"]["acceptance_certificate"]["admission"] == []
    assert status["milestones"]["acceptance"]["admission"] == []


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
    _manual_status(campaign, attempt_id="attempt-dead")
    _owner_authority(authority, "attempt-dead")
    bindings = _bindings(campaign, output, snapshot)
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

    assert status["state"] == "UNPREPARED"
    assert status["compatibility_phase"] is None
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
    _manual_status(campaign, attempt_id="attempt-route-mismatch")
    _owner_authority(authority, "attempt-route-mismatch")
    publish_launch_attempt(
        authority,
        attempt_id="attempt-route-mismatch",
        state="STARTED",
        phase="manual_campaign",
        route="manual_v2",
        bindings=_bindings(campaign, output, snapshot),
    )

    status = bridge_status.resolve_status(tmp_path)

    assert status["state"] == "UNPREPARED"
    assert status["compatibility_phase"] is None
    assert status["blocker"]["reason_codes"] == ["LAUNCH_ATTEMPT_BINDING_INVALID"]


def test_v3_campaign_lease_binds_the_canonical_attempt(
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
        bindings=_bindings(campaign, output, snapshot),
    )
    monkeypatch.setattr(
        bridge_status,
        "resolve_governed_status",
        lambda *_args, **_kwargs: {
            "schema": "legacy",
            "generated_at_unix_ns": time.time_ns(),
            "state": "WAITING_FOR_PLAY",
            "predicates": {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "lease_valid": True,
                "single_writer": True,
                "play_prompt_ready": True,
                "bridge_process_alive": True,
            },
            "blocker": {"class": None, "reason_codes": [], "evidence": []},
            "next_action": "press_play_or_stop",
        },
    )
    monkeypatch.setattr(
        bridge_status,
        "_v3_attempt_binding_valid",
        lambda observed_attempt, observed_campaign, observed_status: (
            observed_attempt["attempt_id"] == "attempt-v3"
            and observed_campaign == campaign
            and observed_status["state"] == "WAITING_FOR_PLAY"
        ),
    )

    status = bridge_status.resolve_status(tmp_path)
    claim = bridge_status.readiness_claim(status, "WAITING_FOR_PLAY")

    assert status["compatibility_phase"] == "WAITING_FOR_PLAY"
    assert status["predicates"]["canonical_attempt_bound"] is True
    assert status["capabilities"] == {"play_prompt": True}
    assert status["next_operator_action"] == "PRESS_PLAY"
    assert status["milestones"]["acceptance_certificate"]["admission"] == []
    bridge_status._require_capability(status, bridge_status.PLAY_PROMPT)
    with pytest.raises(ValueError, match="does not authorize"):
        bridge_status._require_capability(
            {**status, "compatibility_phase": "RUNNING"},
            bridge_status.PLAY_PROMPT,
        )
    assert claim["attempt_id"] == "attempt-v3"


def test_owner_authority_state_prefers_global_metadata_for_attempt_with_resource_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_pid = os.getpid()
    owner_starttime = read_proc_starttime_ticks(owner_pid)
    attempt_id = "attempt-v3-global-only"
    metadata = {
        "worktree_root": str((tmp_path / "global-worktree").resolve()),
        "repository_head": "f" * 40,
        "launch_basis_path": str((tmp_path / "global.launch").resolve()),
        "launch_basis_sha256": hashlib.sha256(b"global-basis").hexdigest(),
    }

    calls: list[str | None] = []

    def fake_load_owner_authority(authority_root, **kwargs):
        calls.append(None if authority_root is None else "legacy")
        if authority_root is None:
            return {
                "schema": "step5d.bridge/owner-authority-v1",
                "attempt_id": attempt_id,
                "owner": {
                    "pid": owner_pid,
                    "starttime_ticks": owner_starttime,
                },
                "state": "ACTIVE",
                "sequence": 1,
                "reason": None,
                **{key: metadata[key] for key in metadata},
            }
        return {
            "schema": "step5d.bridge/owner-authority-v1",
            "attempt_id": "stale",
            "owner": {"pid": owner_pid + 1, "starttime_ticks": owner_starttime + 1},
            "state": "ACTIVE",
            "sequence": 99,
            "reason": "stale",
            "worktree_root": str((tmp_path / "legacy-worktree").resolve()),
        }

    monkeypatch.setattr(bridge_status, "load_owner_authority", fake_load_owner_authority)
    attempt = {
        "bindings": {
            "resource_owner": {"pid": owner_pid, "starttime_ticks": owner_starttime},
            "resource_owner_metadata": metadata,
        },
        "attempt_id": attempt_id,
    }

    authority_state = bridge_status._owner_authority_state(tmp_path, attempt)

    assert authority_state is not None
    assert authority_state["worktree_root"] == metadata["worktree_root"]
    assert calls == [None]


def test_owner_authority_state_fails_without_global_authority_for_v2_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_pid = os.getpid()
    owner_starttime = read_proc_starttime_ticks(owner_pid)
    attempt_id = "attempt-v3-no-global"
    metadata = {
        "worktree_root": str((tmp_path / "legacy-worktree").resolve()),
        "repository_head": "a" * 40,
        "launch_basis_path": str((tmp_path / "legacy.launch").resolve()),
        "launch_basis_sha256": hashlib.sha256(b"legacy-basis").hexdigest(),
    }

    def fake_load_owner_authority(authority_root, **_kwargs):
        return None if authority_root is None else {
            "schema": "step5d.bridge/owner-authority-v1",
            "attempt_id": attempt_id,
            "owner": {"pid": owner_pid, "starttime_ticks": owner_starttime},
            "state": "ACTIVE",
            "sequence": 1,
            "reason": None,
            **{key: metadata[key] for key in metadata},
        }

    monkeypatch.setattr(bridge_status, "load_owner_authority", fake_load_owner_authority)
    attempt = {
        "bindings": {
            "resource_owner": {"pid": owner_pid, "starttime_ticks": owner_starttime},
            "resource_owner_metadata": metadata,
        },
        "attempt_id": attempt_id,
    }

    assert bridge_status._owner_authority_state(tmp_path, attempt) is None


def test_status_rejects_v2_metadata_attempt_without_global_owner_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    basis = tmp_path / "launch-basis.py"
    basis.write_text("basis\n", encoding="utf-8")
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    attempt_id = "attempt-v3-no-global-metadata"
    owner_pid = os.getpid()
    owner_starttime = read_proc_starttime_ticks(owner_pid)
    metadata = {
        "worktree_root": str((tmp_path / "foreign-worktree").resolve()),
        "repository_head": "a" * 40,
        "launch_basis_path": str(basis.resolve()),
        "launch_basis_sha256": hashlib.sha256(basis.read_bytes()).hexdigest(),
    }
    _route_snapshot(snapshot, "autotune_v3")
    bindings = _bindings(campaign, output, snapshot)
    bindings["resource_owner_metadata"] = metadata
    attempt = {
        "schema": LAUNCH_ATTEMPT_SCHEMA,
        "attempt_id": attempt_id,
        "state": "STARTED",
        "phase": "live_handoff",
        "route": "autotune_v3",
        "observed_at_unix_ns": time.time_ns(),
        "bindings": bindings,
    }
    monkeypatch.setattr(bridge_status, "_load_attempt", lambda _root: (attempt, None))
    monkeypatch.setattr(
        bridge_status,
        "resolve_governed_status",
        lambda *_args, **_kwargs: {
            "schema": "legacy",
            "generated_at_unix_ns": time.time_ns(),
            "state": "WAITING_FOR_PLAY",
            "predicates": {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "single_writer": True,
                "bridge_process_alive": True,
                "lease_valid": True,
                "play_prompt_ready": True,
            },
            "blocker": {"class": None, "reason_codes": [], "evidence": []},
            "next_action": "press_play_or_stop",
        },
    )
    monkeypatch.setattr(
        bridge_status,
        "_v3_attempt_binding_valid",
        lambda observed_attempt, observed_campaign, observed_status: (
            observed_attempt["attempt_id"] == attempt_id
            and observed_status["state"] == "WAITING_FOR_PLAY"
        ),
    )
    def fake_load_owner_authority(authority_root, **_kwargs):
        if authority_root is None:
            return None
        return {
            "schema": "step5d.bridge/owner-authority-v1",
            "attempt_id": attempt_id,
            "owner": {"pid": owner_pid, "starttime_ticks": owner_starttime},
            "state": "ACTIVE",
            "sequence": 1,
            "reason": None,
            "worktree_root": metadata["worktree_root"],
            "repository_head": metadata["repository_head"],
            "launch_basis_path": metadata["launch_basis_path"],
            "launch_basis_sha256": metadata["launch_basis_sha256"],
        }

    monkeypatch.setattr(bridge_status, "load_owner_authority", fake_load_owner_authority)

    status = bridge_status.resolve_status(tmp_path)

    assert status["state"] == "UNPREPARED"
    assert status["blocker"]["reason_codes"] == ["LAUNCH_ATTEMPT_BINDING_INVALID"]


def test_owner_authority_state_reuses_legacy_when_no_global_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_pid = os.getpid()
    owner_starttime = read_proc_starttime_ticks(owner_pid)
    authority_dir = tmp_path / bridge_status.AUTHORITY_RELATIVE
    attempt_id = "attempt-v3-legacy-fallback"
    _owner_authority(authority_dir, attempt_id)

    def fake_load_owner_authority(authority_root, **_kwargs):
        if authority_root is None:
            return None
        if authority_root != authority_dir:
            return None
        return json.loads(
            (authority_root / "owner-authority.json").read_text(encoding="utf-8")
        )

    monkeypatch.setattr(bridge_status, "load_owner_authority", fake_load_owner_authority)
    attempt = {
        "schema": LAUNCH_ATTEMPT_SCHEMA,
        "attempt_id": attempt_id,
        "state": "STARTED",
        "phase": "live_handoff",
        "route": "autotune_v3",
        "observed_at_unix_ns": time.time_ns(),
        "bindings": {
            "resource_owner": {
                "pid": owner_pid,
                "starttime_ticks": owner_starttime,
                "authority_epoch": 1,
            },
            "campaign_root": str(tmp_path / "campaign"),
            "output_root": str(tmp_path / "output"),
        },
    }

    assert bridge_status._owner_authority_state(tmp_path, attempt) == json.loads(
        (authority_dir / "owner-authority.json").read_text(encoding="utf-8")
    )


def test_v3_status_finds_foreign_worktree_owner_authority_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_dir = tmp_path / "run-user"
    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    campaign.mkdir()
    output.mkdir()
    snapshot = output / "route.json"
    foreign_worktree = tmp_path / "foreign-worktree"
    foreign_worktree.mkdir()
    _route_snapshot(snapshot, "autotune_v3")
    _manual_status(campaign, attempt_id="attempt-v3-foreign")
    basis = tmp_path / "launch-basis.py"
    basis.write_text("launch basis\n", encoding="utf-8")
    launch_basis_sha = hashlib.sha256(basis.read_bytes()).hexdigest()
    repository_head = "a" * 40
    owner_pid = os.getpid()
    owner_starttime = read_proc_starttime_ticks(owner_pid)
    attempt_id = "attempt-v3-foreign"

    monkeypatch.setattr(
        authority,
        "DEFAULT_AUTHORITY_ROOT",
        authority_dir,
    )
    monkeypatch.setattr(authority.os, "getppid", lambda: owner_pid)
    reserved = authority.begin(
        None,
        attempt_id=attempt_id,
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
        worktree_root=str(foreign_worktree),
        repository_head=repository_head,
        launch_basis_path=str(basis),
        launch_basis_sha256=launch_basis_sha,
    )
    authority.bind_basis(
        None,
        attempt_id=attempt_id,
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
        sequence=reserved["sequence"],
        launch_basis_path=str(basis),
        launch_basis_sha256=launch_basis_sha,
    )

    bindings = _bindings(campaign, output, snapshot)
    bindings["resource_owner_metadata"] = {
        "resource_id": authority.DEFAULT_RESOURCE_ID,
        "worktree_root": str(foreign_worktree.resolve()),
        "repository_head": repository_head,
        "launch_basis_path": str(basis.resolve()),
        "launch_basis_sha256": launch_basis_sha,
    }
    attempt = {
        "schema": LAUNCH_ATTEMPT_SCHEMA,
        "attempt_id": attempt_id,
        "state": "STARTED",
        "phase": "live_handoff",
        "route": "autotune_v3",
        "observed_at_unix_ns": time.time_ns(),
        "bindings": bindings,
    }
    monkeypatch.setattr(
        bridge_status,
        "_load_attempt",
        lambda _root: (attempt, None),
    )
    assert bridge_status._owner_authority_state(tmp_path, attempt) is not None
    monkeypatch.setattr(
        bridge_status,
        "resolve_governed_status",
        lambda *_args, **_kwargs: {
            "schema": "legacy",
            "generated_at_unix_ns": time.time_ns(),
            "state": "WAITING_FOR_PLAY",
            "predicates": {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "single_writer": True,
                "bridge_process_alive": True,
                "lease_valid": True,
                "play_prompt_ready": True,
            },
            "blocker": {"class": None, "reason_codes": [], "evidence": []},
            "next_action": "press_play_or_stop",
        },
    )
    monkeypatch.setattr(
        bridge_status,
        "_v3_attempt_binding_valid",
        lambda observed_attempt, observed_campaign, observed_status: (
            observed_attempt["attempt_id"] == attempt_id
            and observed_status["state"] == "WAITING_FOR_PLAY"
        ),
    )

    status = bridge_status.resolve_status(tmp_path)

    assert status["state"] == "BENCH_READY", status.get("blocker")
    assert status["compatibility_phase"] == "WAITING_FOR_PLAY"
    assert status["predicates"]["canonical_attempt_bound"] is True
    assert status["capabilities"] == {"play_prompt": True}
    assert status["next_operator_action"] == "PRESS_PLAY"
    assert status["owner_authority"] == {
        "attempt_id": attempt_id,
        "owner": {
            "pid": owner_pid,
            "starttime_ticks": owner_starttime,
        },
        "worktree_root": str(foreign_worktree.resolve()),
        "repository_head": repository_head,
        "launch_basis_path": str(basis.resolve()),
        "launch_basis_sha256": launch_basis_sha,
    }
    assert not (
        (tmp_path / bridge_status.AUTHORITY_RELATIVE / authority.STATE_FILE).exists()
    )


def test_readiness_claim_rejects_an_aged_status_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_now = 20_000_000_000
    status = {
        "state": "WAITING_FOR_PLAY",
        "generated_at_unix_ns": (
            observed_now - bridge_status.STATUS_CLAIM_MAX_AGE_NS - 1
        ),
        "route": "autotune_v3",
        "launch_attempt": {"attempt_id": "attempt-aged"},
        "predicates": {
            "play_prompt_ready": True,
            "canonical_attempt_bound": True,
            "release_contract_proven": True,
        },
    }
    monkeypatch.setattr(bridge_status.time, "time_ns", lambda: observed_now)

    with pytest.raises(ValueError, match="does not authorize"):
        bridge_status.readiness_claim(status, "WAITING_FOR_PLAY")

    assert not (tmp_path / "readiness-claim.json").exists()


def test_require_capability_requires_play_prompt_capability_and_fresh_binding() -> None:
    now = 1_000
    status = {
        "state": "WAITING_FOR_PLAY",
        "generated_at_unix_ns": now,
        "predicates": {
            "play_prompt_ready": True,
            "canonical_attempt_bound": True,
            "release_contract_proven": True,
        },
        "milestones": {
            "liveness": {
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": True,
            },
            "authorization": {
                "canonical_attempt_bound": True,
                "single_writer": True,
                "lease_valid": True,
            },
            "acceptance_certificate": {
                "release_contract_proven": True,
                "play_prompt_ready": True,
                "admission": [],
            },
        },
        "capabilities": {"play_prompt": True},
    }

    bridge_status._require_capability(
        status,
        bridge_status.PLAY_PROMPT,
        now_ns=now + 1,
    )

    stale_status = {
        **status,
        "generated_at_unix_ns": now - bridge_status.STATUS_CLAIM_MAX_AGE_NS - 1,
    }
    with pytest.raises(ValueError, match="does not authorize"):
        bridge_status._require_capability(
            stale_status,
            bridge_status.PLAY_PROMPT,
            now_ns=now,
        )

    no_auth_status = {
        **status,
        "capabilities": {"play_prompt": False},
    }
    with pytest.raises(ValueError, match="does not authorize"):
        bridge_status._require_capability(
            no_auth_status,
            bridge_status.PLAY_PROMPT,
            now_ns=now + 1,
        )


def test_v3_runtime_evidence_must_bind_same_attempt_campaign_release_and_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_path = tmp_path / "governance/evidence/process.json"
    process_path.parent.mkdir(parents=True)
    process_payload = {
        "schema": "step5d.autotune-v3/process-observation-evidence-v1",
        "processes": [
            {
                "role": "canonical_launcher",
                "pid": 41,
                "starttime_ticks": 701,
            },
            {
                "role": "bridge_wrapper",
                "pid": 42,
                "starttime_ticks": 702,
            },
        ],
    }
    process_path.write_text(json.dumps(process_payload), encoding="utf-8")
    manifest_sha = "a" * 64
    attestation = {
        "run_id": "attempt-v3-bound",
        "campaign_id": "campaign-v3-bound",
        "bindings": {"manifest_sha256": manifest_sha},
        "process": {
            "evidence": {
                "path": str(process_path.relative_to(tmp_path)),
                "sha256": hashlib.sha256(process_path.read_bytes()).hexdigest(),
            },
            "bridge_pid": 42,
            "bridge_starttime_ticks": 702,
        },
    }
    monkeypatch.setattr(
        bridge_status,
        "load_current_observation",
        lambda _campaign: (attestation, {}),
    )
    attempt = {
        "attempt_id": "attempt-v3-bound",
        "manifest_sha256": manifest_sha,
        "bindings": {
            "resource_owner": {"pid": 41, "starttime_ticks": 701},
        },
    }
    status = {
        "release": {"sha256": manifest_sha},
        "attestation": {"run_id": "attempt-v3-bound"},
        "campaign_lease": {"campaign_id": "campaign-v3-bound"},
    }

    assert bridge_status._v3_attempt_binding_valid(attempt, tmp_path, status) is True
    assert (
        bridge_status._v3_attempt_binding_valid(
            {**attempt, "attempt_id": "different-attempt"}, tmp_path, status
        )
        is False
    )
    assert (
        bridge_status._v3_attempt_binding_valid(
            attempt,
            tmp_path,
            {**status, "campaign_lease": {"campaign_id": "different-campaign"}},
        )
        is False
    )


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
    _manual_status(campaign, attempt_id="attempt-complete")
    manual_path = campaign / "manual_governed_status.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    manual["state"] = "COMPLETE"
    manual["play_prompt_ready"] = False
    manual["next_action"] = "none"
    manual_path.write_text(json.dumps(manual), encoding="utf-8")
    _owner_authority(authority, "attempt-complete")
    bindings = _bindings(campaign, output, snapshot)
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

    assert status["state"] == "TERMINAL"
    assert status["compatibility_phase"] is None
    assert status["predicates"]["play_prompt_ready"] is False
    assert status["predicates"]["canonical_attempt_bound"] is False
    with pytest.raises(ValueError, match="pre-Play"):
        bridge_status.readiness_claim(status, "COMPLETE")
