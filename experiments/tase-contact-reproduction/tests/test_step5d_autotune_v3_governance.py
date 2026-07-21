from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from step5d_autotune_v3 import cli, governance
from step5d_autotune_v3.governance import (
    BRIDGE_HEARTBEAT_MAX_AGE_NS,
    CONTROLLER_FRESH_GET_MAX_AGE_NS,
    CURRENT_LAUNCH_ATTEMPT_POINTER_SCHEMA,
    CURRENT_OBSERVATION_POINTER_SCHEMA,
    FSM_STATES,
    GOVERNED_STATUS_SCHEMA,
    INVALIDATION_TABLE,
    OBSERVED_ATTESTATION_SCHEMA,
    TRANSITION_TABLE,
    CurrentReleaseSnapshot,
    GovernanceError,
    build_campaign_lease,
    load_current_launch_attempt,
    load_current_observation,
    publish_launch_attempt,
    publish_observed_attestation,
    reduce_observed_attestation,
    resolve_governed_status,
    validate_launch_attempt,
    validate_observed_attestation,
)


ROOT = Path(__file__).resolve().parents[1]
NOW_NS = 2_000_000_000_000_000_000
BRIDGE_PID = 4101
BRIDGE_STARTTIME = 7101
SUPERVISOR_PID = 4100
SUPERVISOR_STARTTIME = 7100


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def evidence(
    root: Path, name: str, payload: dict[str, object] | None = None
) -> dict[str, str]:
    path = root / "evidence" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"name": name} if payload is None else payload,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def delivery_evidence(root: Path, observed_at_unix_ns: int) -> dict[str, str]:
    return evidence(
        root,
        "delivery",
        {
            "schema": "step5d.autotune-v3/delivery-observation-v1",
            "transaction_id": "d" * 32,
            "fresh_controller_checked_at": datetime.fromtimestamp(
                observed_at_unix_ns / 1_000_000_000,
                tz=timezone.utc,
            ).isoformat(),
        },
    )


def release() -> CurrentReleaseSnapshot:
    return CurrentReleaseSnapshot(
        manifest_sha256=digest("manifest"),
        manifest_path=f"config/step5d/releases/{digest('manifest')}/manifest.json",
        program_id="step5d_strict_rnn_autotune_v3_r010",
        release_stage_id="step5d_strict_rnn_autotune_v3",
        source_fingerprint=digest("source"),
        launcher_sha256=digest("launcher"),
        safety_envelope_sha256=digest("safety"),
        expected_triplet_sha256={
            ".script": digest("script"),
            ".txt": digest("txt"),
            ".urp": digest("urp"),
        },
        expected_loaded_program=(
            "/programs/andyl/kunwei/step5/"
            "step5d_strict_rnn_autotune_v3_r010.urp"
        ),
        expected_tp_runtime_identity={
            "protocol_version": 1,
            "digest_hi": 123,
            "digest_lo": 456,
        },
        valid=True,
        error=None,
    )


def observed_attestation(root: Path) -> dict[str, object]:
    current = release()
    fresh_get_at = NOW_NS - 50_000_000
    bindings = {
        "manifest_sha256": current.manifest_sha256,
        "source_fingerprint": current.source_fingerprint,
        "launcher_sha256": current.launcher_sha256,
        "environment_sha256": digest("environment"),
        "process_tree_fingerprint": digest("process-tree"),
        "safety_envelope_sha256": current.safety_envelope_sha256,
        "campaign_fingerprint": digest("campaign"),
    }
    lease = build_campaign_lease(
        campaign_id="campaign-1",
        manifest_sha256=current.manifest_sha256,
        campaign_fingerprint=bindings["campaign_fingerprint"],
        safety_envelope_sha256=current.safety_envelope_sha256,
        issued_at_unix_ns=NOW_NS - 2_000_000_000,
        expires_at_unix_ns=NOW_NS + 10_000_000_000,
        supervisor_pid=SUPERVISOR_PID,
        supervisor_starttime_ticks=SUPERVISOR_STARTTIME,
    )
    return {
        "schema": OBSERVED_ATTESTATION_SCHEMA,
        "sequence": 1,
        "run_id": "run-1",
        "campaign_id": "campaign-1",
        "observed_at_unix_ns": NOW_NS,
        "bindings": bindings,
        "offline": {
            "evidence": evidence(root, "offline"),
            "completed_at_unix_ns": NOW_NS - 1_000_000_000,
            "manifest_sha256": bindings["manifest_sha256"],
            "source_fingerprint": bindings["source_fingerprint"],
            "launcher_sha256": bindings["launcher_sha256"],
            "environment_sha256": bindings["environment_sha256"],
            "process_tree_fingerprint": bindings["process_tree_fingerprint"],
        },
        "process": {
            "bridge_pid": BRIDGE_PID,
            "bridge_starttime_ticks": BRIDGE_STARTTIME,
            "heartbeat_at_unix_ns": NOW_NS - 50_000_000,
            "process_tree_fingerprint": bindings["process_tree_fingerprint"],
            "writer_pids": [BRIDGE_PID],
            "evidence": evidence(root, "process"),
        },
        "controller": {
            "observed_at_unix_ns": NOW_NS - 50_000_000,
            "fresh_get_observed_at_unix_ns": fresh_get_at,
            "delivery_transaction_id": "d" * 32,
            "uploaded_triplet_sha256": current.expected_triplet_sha256,
            "readback_triplet_sha256": current.expected_triplet_sha256,
            "loaded_program": current.expected_loaded_program,
            "tp_runtime_identity": current.expected_tp_runtime_identity,
            "rtde_output_fields": [
                f"output_int_register_{index}" for index in range(24, 38)
            ],
            "rtde_output_types": ["INT32"] * 14,
            "rtde_observed_at_unix_ns": NOW_NS - 20_000_000,
            "kunwei_observed_at_unix_ns": NOW_NS - 20_000_000,
            "delivery_observation": delivery_evidence(root, fresh_get_at),
            "evidence": evidence(root, "controller"),
        },
        "mailbox": {
            "observed_at_unix_ns": NOW_NS - 20_000_000,
            "pending_arm_sequence": None,
            "duplicate_arm_detected": False,
            "evidence": evidence(root, "mailbox"),
        },
        "lease": lease,
        "events": {
            "waiting_for_play_at_unix_ns": None,
            "play_observed_at_unix_ns": None,
            "play_identity_recheck": None,
            "first_arm_ack": None,
            "trial_completion": None,
            "next_arm_ack": None,
            "campaign_terminal": None,
        },
        "external_blocker": None,
    }


def process_reader(pid: int) -> int | None:
    return {
        BRIDGE_PID: BRIDGE_STARTTIME,
        SUPERVISOR_PID: SUPERVISOR_STARTTIME,
    }.get(pid)


def reduce(root: Path, row: dict[str, object] | None, **kwargs: object) -> dict[str, object]:
    return reduce_observed_attestation(
        kwargs.pop("release", release()),
        row,
        campaign_root=root,
        now_ns=kwargs.pop("now_ns", NOW_NS),
        proc_starttime_reader=kwargs.pop("proc_starttime_reader", process_reader),
        **kwargs,
    )


def offline_proof(root: Path) -> dict[str, object]:
    current = release()
    return {
        "evidence": evidence(root, "qualification"),
        "completed_at_unix_ns": NOW_NS - 1,
        "manifest_sha256": current.manifest_sha256,
        "source_fingerprint": current.source_fingerprint,
        "launcher_sha256": current.launcher_sha256,
        "environment_sha256": digest("environment"),
        "process_tree_fingerprint": digest("process-tree"),
    }


def test_strict_attestation_rejects_manual_readiness_flag(tmp_path: Path) -> None:
    row = observed_attestation(tmp_path)
    row["bench_ready"] = True

    with pytest.raises(GovernanceError, match="fields differ"):
        validate_observed_attestation(row)


def test_fsm_transitions_are_derived_from_observations(tmp_path: Path) -> None:
    row = observed_attestation(tmp_path)
    assert reduce(tmp_path, None)["state"] is None

    dead = copy.deepcopy(row)
    dead_status = reduce(tmp_path, dead, proc_starttime_reader=lambda _pid: None)
    assert dead_status["state"] == "OFFLINE_PROVEN"

    assert reduce(tmp_path, row)["state"] == "BENCH_READY"

    waiting = copy.deepcopy(row)
    waiting["events"]["waiting_for_play_at_unix_ns"] = NOW_NS - 500_000_000
    assert reduce(tmp_path, waiting)["state"] == "WAITING_FOR_PLAY"

    running = copy.deepcopy(waiting)
    running["events"].update(
        {
            "play_observed_at_unix_ns": NOW_NS - 400_000_000,
            "play_identity_recheck": {
                "observed_at_unix_ns": NOW_NS - 300_000_000,
                "readback_triplet_sha256": release().expected_triplet_sha256,
                "loaded_program": release().expected_loaded_program,
                "tp_runtime_identity": release().expected_tp_runtime_identity,
            },
            "first_arm_ack": {
                "sequence": 1,
                "observed_at_unix_ns": NOW_NS - 200_000_000,
            },
        }
    )
    status = reduce(tmp_path, running)
    assert status["state"] == "RUNNING"
    assert status["predicates"]["bench_ready"] is True
    assert {transition["to"] for transition in TRANSITION_TABLE} == set(FSM_STATES)


def test_dashboard_refresh_cannot_renew_stale_controller_fresh_get(
    tmp_path: Path,
) -> None:
    row = observed_attestation(tmp_path)
    stale_at = NOW_NS - CONTROLLER_FRESH_GET_MAX_AGE_NS - 1_000
    row["controller"]["fresh_get_observed_at_unix_ns"] = stale_at
    row["controller"]["delivery_observation"] = delivery_evidence(
        tmp_path, stale_at
    )

    status = reduce(tmp_path, row)

    assert status["predicates"]["controller_fresh"] is True
    assert status["predicates"]["controller_fresh_get"] is False
    assert status["predicates"]["bench_ready"] is False
    assert "CONTROLLER_FRESH_GET_STALE" in status["blocker"]["reason_codes"]


def test_stopped_program_never_claims_bench_ready_before_runtime_identity(
    tmp_path: Path,
) -> None:
    waiting = observed_attestation(tmp_path)
    waiting["controller"]["tp_runtime_identity"] = None
    waiting["events"]["waiting_for_play_at_unix_ns"] = NOW_NS - 500_000_000

    before_play = reduce(tmp_path, waiting)
    assert before_play["state"] == "WAITING_FOR_IDENTITY_PLAY"
    assert before_play["predicates"]["tp_runtime_identity_verified"] is False
    assert before_play["predicates"]["play_prompt_ready"] is True
    assert before_play["predicates"]["bench_ready"] is False
    assert before_play["next_action"] == "press_play_or_stop"
    assert "TP_RUNTIME_IDENTITY_UNAVAILABLE" not in before_play["blocker"]["reason_codes"]

    after_play = copy.deepcopy(waiting)
    after_play["events"]["play_observed_at_unix_ns"] = NOW_NS - 400_000_000
    after_play_status = reduce(tmp_path, after_play)
    assert after_play_status["state"] == "OFFLINE_PROVEN"
    assert after_play_status["predicates"]["bench_ready"] is False
    assert "TP_RUNTIME_IDENTITY_UNAVAILABLE" in after_play_status["blocker"]["reason_codes"]


def test_offline_qualification_is_a_state_without_live_attestation(
    tmp_path: Path,
) -> None:
    status = reduce(tmp_path, None, offline_proof=offline_proof(tmp_path))

    assert status["state"] == "OFFLINE_PROVEN"
    assert status["predicates"]["offline_proven"] is True
    assert status["blocker"]["class"] is None
    assert "CURRENT_OBSERVATION_MISSING" not in status["blocker"]["reason_codes"]
    assert status["next_action"] == "start_canonical_bridge"

    drifted = offline_proof(tmp_path)
    drifted["source_fingerprint"] = digest("drifted-source")
    blocked = reduce(tmp_path, None, offline_proof=drifted)
    assert blocked["predicates"]["offline_proven"] is False
    assert "OFFLINE_BINDING_MISMATCH" in blocked["blocker"]["reason_codes"]


def test_live_proven_is_only_an_outcome_pointer(tmp_path: Path) -> None:
    row = observed_attestation(tmp_path)
    row["events"].update(
        {
            "waiting_for_play_at_unix_ns": NOW_NS - 600_000_000,
            "play_observed_at_unix_ns": NOW_NS - 500_000_000,
            "play_identity_recheck": {
                "observed_at_unix_ns": NOW_NS - 400_000_000,
                "readback_triplet_sha256": release().expected_triplet_sha256,
                "loaded_program": release().expected_loaded_program,
                "tp_runtime_identity": release().expected_tp_runtime_identity,
            },
            "first_arm_ack": {
                "sequence": 1,
                "observed_at_unix_ns": NOW_NS - 300_000_000,
            },
            "trial_completion": {
                "trial_id": "trial-1",
                "observed_at_unix_ns": NOW_NS - 200_000_000,
                "evidence": evidence(tmp_path, "trial"),
            },
            "next_arm_ack": {
                "sequence": 2,
                "observed_at_unix_ns": NOW_NS - 100_000_000,
            },
        }
    )
    row["process"]["heartbeat_at_unix_ns"] = NOW_NS - 50_000_000

    status = reduce(
        tmp_path,
        row,
        now_ns=NOW_NS + BRIDGE_HEARTBEAT_MAX_AGE_NS + 1,
    )
    assert status["outcome"]["live_proven"] is True
    assert "live_proven" not in status["predicates"]
    assert "LIVE_PROVEN" not in FSM_STATES
    assert status["state"] == "OFFLINE_PROVEN"


def test_completed_terminal_retains_outcome_without_reusing_live_readiness(
    tmp_path: Path,
) -> None:
    row = observed_attestation(tmp_path)
    row["events"].update(
        {
            "waiting_for_play_at_unix_ns": NOW_NS - 600_000_000,
            "play_observed_at_unix_ns": NOW_NS - 500_000_000,
            "play_identity_recheck": {
                "observed_at_unix_ns": NOW_NS - 400_000_000,
                "readback_triplet_sha256": release().expected_triplet_sha256,
                "loaded_program": release().expected_loaded_program,
                "tp_runtime_identity": release().expected_tp_runtime_identity,
            },
            "first_arm_ack": {
                "sequence": 1,
                "observed_at_unix_ns": NOW_NS - 300_000_000,
            },
            "trial_completion": {
                "trial_id": "trial-1",
                "observed_at_unix_ns": NOW_NS - 200_000_000,
                "evidence": evidence(tmp_path, "terminal-trial"),
            },
            "next_arm_ack": {
                "sequence": 2,
                "observed_at_unix_ns": NOW_NS - 100_000_000,
            },
            "campaign_terminal": {
                "observed_at_unix_ns": NOW_NS - 70_000_000,
                "reason": "campaign_complete",
                "runner_exit_code": 0,
            },
        }
    )
    row["lease"]["revoked_at_unix_ns"] = NOW_NS - 80_000_000
    row["lease"]["revocation_reason"] = "campaign_terminal"

    status = reduce(
        tmp_path,
        row,
        now_ns=NOW_NS + 20_000_000_000,
        proc_starttime_reader=lambda _pid: None,
    )

    assert status["terminal"] == {
        "completed": True,
        "reason": "campaign_complete",
        "observed_at_unix_ns": NOW_NS - 70_000_000,
        "runner_exit_code": 0,
    }
    assert status["outcome"]["live_proven"] is True
    assert status["state"] == "OFFLINE_PROVEN"
    assert status["blocker"]["class"] is None
    assert status["blocker"]["reason_codes"] == []
    assert status["next_action"] == "campaign_complete"
    assert status["predicates"]["offline_proven"] is True
    assert all(
        value is False
        for name, value in status["predicates"].items()
        if name not in {"release_current", "offline_proven"}
    )


def test_terminal_event_requires_zero_exit_and_terminal_lease_revocation(
    tmp_path: Path,
) -> None:
    row = observed_attestation(tmp_path)
    row["events"].update(
        {
            "waiting_for_play_at_unix_ns": NOW_NS - 600_000_000,
            "play_observed_at_unix_ns": NOW_NS - 500_000_000,
            "play_identity_recheck": {
                "observed_at_unix_ns": NOW_NS - 400_000_000,
                "readback_triplet_sha256": release().expected_triplet_sha256,
                "loaded_program": release().expected_loaded_program,
                "tp_runtime_identity": release().expected_tp_runtime_identity,
            },
            "first_arm_ack": {
                "sequence": 1,
                "observed_at_unix_ns": NOW_NS - 300_000_000,
            },
            "trial_completion": {
                "trial_id": "trial-1",
                "observed_at_unix_ns": NOW_NS - 200_000_000,
                "evidence": evidence(tmp_path, "invalid-terminal-trial"),
            },
            "next_arm_ack": {
                "sequence": 2,
                "observed_at_unix_ns": NOW_NS - 100_000_000,
            },
            "campaign_terminal": {
                "observed_at_unix_ns": NOW_NS - 70_000_000,
                "reason": "campaign_complete",
                "runner_exit_code": 0,
            },
        }
    )
    with pytest.raises(GovernanceError, match="terminal lease revocation"):
        validate_observed_attestation(row)

    row["lease"]["revoked_at_unix_ns"] = NOW_NS - 80_000_000
    row["lease"]["revocation_reason"] = "campaign_terminal"
    row["events"]["campaign_terminal"]["runner_exit_code"] = 1
    with pytest.raises(GovernanceError, match="exit code zero"):
        validate_observed_attestation(row)


def test_every_invalidation_fails_closed_without_erasing_outcome() -> None:
    assert set(INVALIDATION_TABLE) == {
        "manifest_sha_changed",
        "source_fingerprint_changed",
        "launcher_changed",
        "environment_changed",
        "process_tree_changed",
        "safety_envelope_changed",
        "campaign_fingerprint_changed",
        "controller_readback_changed",
        "loaded_program_changed",
        "tp_runtime_identity_changed",
        "rtde_recipe_changed",
        "bridge_pid_reused",
        "bridge_heartbeat_stale",
        "rtde_lost",
        "kunwei_lost",
        "writer_lock_lost",
        "mailbox_dirty",
        "user_stop",
        "user_cancel",
        "hard_safety_fault",
        "campaign_terminal",
        "supervisor_exit",
    }
    for predicates in INVALIDATION_TABLE.values():
        assert "bench_ready" in predicates
        assert "live_proven" not in predicates


def test_pid_reuse_and_stale_heartbeat_fail_closed(tmp_path: Path) -> None:
    row = observed_attestation(tmp_path)
    reused = reduce(
        tmp_path,
        row,
        proc_starttime_reader=lambda pid: (
            BRIDGE_STARTTIME + 1 if pid == BRIDGE_PID else SUPERVISOR_STARTTIME
        ),
    )
    assert reused["predicates"]["bridge_process_alive"] is False
    assert "BRIDGE_PID_REUSED" in reused["blocker"]["reason_codes"]

    stale = copy.deepcopy(row)
    stale["process"]["heartbeat_at_unix_ns"] = (
        NOW_NS - BRIDGE_HEARTBEAT_MAX_AGE_NS - 1
    )
    stale_status = reduce(tmp_path, stale)
    assert stale_status["predicates"]["bridge_heartbeat_fresh"] is False
    assert stale_status["predicates"]["bench_ready"] is False


@pytest.mark.parametrize(
    ("field", "new_value", "reason"),
    [
        ("manifest_sha256", digest("new-manifest"), "OBSERVATION_RELEASE_MISMATCH"),
        ("source_fingerprint", digest("new-source"), "SOURCE_BINDING_MISMATCH"),
        ("launcher_sha256", digest("new-launcher"), "LAUNCHER_BINDING_MISMATCH"),
        ("environment_sha256", digest("new-environment"), "OFFLINE_BINDING_MISMATCH"),
        (
            "process_tree_fingerprint",
            digest("new-process-tree"),
            "PROCESS_TREE_BINDING_MISMATCH",
        ),
    ],
)
def test_content_binding_changes_invalidate_offline_proof(
    tmp_path: Path, field: str, new_value: str, reason: str
) -> None:
    row = observed_attestation(tmp_path)
    row["bindings"][field] = new_value

    status = reduce(tmp_path, row)
    assert status["predicates"]["offline_proven"] is False
    assert status["predicates"]["bench_ready"] is False
    assert reason in status["blocker"]["reason_codes"]


@pytest.mark.parametrize("changed", ["campaign", "safety", "expired", "revoked"])
def test_lease_fingerprint_and_revocation_invalidate_bench(
    tmp_path: Path, changed: str
) -> None:
    row = observed_attestation(tmp_path)
    if changed == "campaign":
        row["bindings"]["campaign_fingerprint"] = digest("new-campaign")
    elif changed == "safety":
        altered = replace(
            release(), safety_envelope_sha256=digest("new-safety")
        )
        status = reduce(tmp_path, row, release=altered)
        assert status["predicates"]["lease_valid"] is False
        assert "SAFETY_ENVELOPE_BINDING_MISMATCH" in status["blocker"]["reason_codes"]
        return
    elif changed == "expired":
        row["lease"] = build_campaign_lease(
            campaign_id="campaign-1",
            manifest_sha256=release().manifest_sha256,
            campaign_fingerprint=row["bindings"]["campaign_fingerprint"],
            safety_envelope_sha256=release().safety_envelope_sha256,
            issued_at_unix_ns=NOW_NS - 2_000_000_000,
            expires_at_unix_ns=NOW_NS - 1,
            supervisor_pid=SUPERVISOR_PID,
            supervisor_starttime_ticks=SUPERVISOR_STARTTIME,
        )
    else:
        row["lease"]["revoked_at_unix_ns"] = NOW_NS - 1
        row["lease"]["revocation_reason"] = "user_stop"
    status = reduce(tmp_path, row)
    assert status["predicates"]["lease_valid"] is False
    assert status["predicates"]["bench_ready"] is False


def test_current_pointer_is_content_addressed_atomic_and_monotonic(tmp_path: Path) -> None:
    row = observed_attestation(tmp_path)
    pointer = publish_observed_attestation(tmp_path, row)
    assert pointer["schema"] == CURRENT_OBSERVATION_POINTER_SCHEMA
    loaded, loaded_pointer = load_current_observation(tmp_path)
    assert loaded == row
    assert loaded_pointer == pointer
    assert publish_observed_attestation(tmp_path, row) == pointer

    newer = copy.deepcopy(row)
    newer["sequence"] = 2
    publish_observed_attestation(tmp_path, newer)
    with pytest.raises(GovernanceError):
        publish_observed_attestation(tmp_path, row)


def test_launch_attempt_pointer_is_content_addressed_and_phase_monotonic(
    tmp_path: Path,
) -> None:
    started = publish_launch_attempt(
        tmp_path,
        attempt_id="attempt-1",
        state="STARTED",
        phase="qualification",
        manifest_sha256=release().manifest_sha256,
        observed_at_unix_ns=NOW_NS - 2,
    )
    assert started["pointer"]["schema"] == CURRENT_LAUNCH_ATTEMPT_POINTER_SCHEMA
    assert started["pointer"]["attestation_path"] == (
        "governance/launch-attempts/"
        f"{started['pointer']['attestation_sha256']}/attestation.json"
    )
    loaded, pointer = load_current_launch_attempt(tmp_path)
    assert loaded == started["attestation"]
    assert pointer == started["pointer"]

    failed = publish_launch_attempt(
        tmp_path,
        attempt_id="attempt-1",
        state="FAILED",
        phase="qualification",
        manifest_sha256=release().manifest_sha256,
        observed_at_unix_ns=NOW_NS - 1,
        exit_code=23,
        reason_code="LAUNCH_ATTEMPT_FAILED",
        detail="qualification exited 23",
    )
    assert failed["attestation"]["sequence"] == 2
    assert load_current_launch_attempt(tmp_path)[0] == failed["attestation"]
    with pytest.raises(GovernanceError, match="terminal"):
        publish_launch_attempt(
            tmp_path,
            attempt_id="attempt-1",
            state="STARTED",
            phase="tp_delivery",
            observed_at_unix_ns=NOW_NS,
        )
    with pytest.raises(GovernanceError, match="must start"):
        publish_launch_attempt(
            tmp_path,
            attempt_id="attempt-2",
            state="FAILED",
            phase="status_before",
            observed_at_unix_ns=NOW_NS,
            exit_code=1,
            reason_code="LAUNCH_ATTEMPT_FAILED",
        )


def test_launch_attempt_external_class_requires_positive_evidence(
    tmp_path: Path,
) -> None:
    row = {
        "schema": governance.LAUNCH_ATTEMPT_SCHEMA,
        "sequence": 1,
        "attempt_id": "attempt-1",
        "state": "FAILED",
        "phase": "preflight",
        "observed_at_unix_ns": NOW_NS,
        "manifest_sha256": release().manifest_sha256,
        "exit_code": 2,
        "reason_code": "DASHBOARD_UNREACHABLE",
        "detail": "dashboard connection refused",
        "external_evidence": None,
    }
    with pytest.raises(GovernanceError, match="positive evidence"):
        validate_launch_attempt(row)


def _stub_governed_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(governance, "load_current_release_snapshot", lambda _root: release())
    monkeypatch.setattr(
        governance,
        "_load_current_offline_proof",
        lambda _experiment, _campaign, _release: None,
    )


def test_unsuperseded_launch_failure_is_status_visible_and_unknown_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_governed_release(monkeypatch)
    publish_launch_attempt(
        tmp_path,
        attempt_id="attempt-1",
        state="STARTED",
        phase="preflight",
        manifest_sha256=release().manifest_sha256,
        observed_at_unix_ns=NOW_NS - 2,
    )
    publish_launch_attempt(
        tmp_path,
        attempt_id="attempt-1",
        state="FAILED",
        phase="preflight",
        manifest_sha256=release().manifest_sha256,
        observed_at_unix_ns=NOW_NS - 1,
        exit_code=17,
        reason_code="LAUNCH_ATTEMPT_FAILED",
        detail="preflight exited 17",
    )

    status = resolve_governed_status(
        ROOT,
        tmp_path,
        now_ns=NOW_NS,
        proc_starttime_reader=process_reader,
    )

    assert status["launch_attempt"]["attempt_id"] == "attempt-1"
    assert status["launch_attempt"]["phase"] == "preflight"
    assert status["launch_attempt"]["superseded"] is False
    assert status["blocker"]["class"] == "UNKNOWN"
    assert status["blocker"]["reason_codes"] == ["LAUNCH_ATTEMPT_FAILED"]
    assert status["next_action"] == "collect_read_only_evidence"


def test_positive_external_launch_failure_is_external_until_evidence_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_governed_release(monkeypatch)
    external = evidence(tmp_path, "dashboard-launch-outage")
    publish_launch_attempt(
        tmp_path,
        attempt_id="attempt-external",
        state="STARTED",
        phase="preflight",
        manifest_sha256=release().manifest_sha256,
        observed_at_unix_ns=NOW_NS - 2,
    )
    publish_launch_attempt(
        tmp_path,
        attempt_id="attempt-external",
        state="FAILED",
        phase="preflight",
        manifest_sha256=release().manifest_sha256,
        observed_at_unix_ns=NOW_NS - 1,
        exit_code=2,
        reason_code="DASHBOARD_UNREACHABLE",
        detail="dashboard connection refused",
        external_evidence=external,
    )

    status = resolve_governed_status(ROOT, tmp_path, now_ns=NOW_NS)
    assert status["blocker"]["class"] == "BLOCKED_EXTERNAL"
    assert "DASHBOARD_UNREACHABLE" in status["blocker"]["reason_codes"]

    (tmp_path / external["path"]).unlink()
    lost = resolve_governed_status(ROOT, tmp_path, now_ns=NOW_NS)
    assert lost["blocker"]["class"] == "UNKNOWN"
    assert "DASHBOARD_UNREACHABLE" not in lost["blocker"]["reason_codes"]
    assert "UNKNOWN_OBSERVATION" in lost["blocker"]["reason_codes"]


def test_newer_runtime_observation_supersedes_matching_launch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_governed_release(monkeypatch)
    publish_launch_attempt(
        tmp_path,
        attempt_id="run-1",
        state="STARTED",
        phase="live_handoff",
        manifest_sha256=release().manifest_sha256,
        observed_at_unix_ns=NOW_NS - 2,
    )
    publish_launch_attempt(
        tmp_path,
        attempt_id="run-1",
        state="FAILED",
        phase="live_handoff",
        manifest_sha256=release().manifest_sha256,
        observed_at_unix_ns=NOW_NS - 1,
        exit_code=2,
        reason_code="LAUNCH_ATTEMPT_FAILED",
    )
    publish_observed_attestation(tmp_path, observed_attestation(tmp_path))

    status = resolve_governed_status(
        ROOT,
        tmp_path,
        now_ns=NOW_NS,
        proc_starttime_reader=process_reader,
    )

    assert status["launch_attempt"]["superseded"] is True
    assert status["launch_attempt"]["superseded_by_run_id"] == "run-1"
    assert "LAUNCH_ATTEMPT_FAILED" not in status["blocker"]["reason_codes"]
    assert status["state"] == "BENCH_READY"


def test_external_blocker_requires_fresh_positive_evidence(tmp_path: Path) -> None:
    row = observed_attestation(tmp_path)
    row["external_blocker"] = {
        "reason_code": "RTDE_UNREACHABLE",
        "observed_at_unix_ns": NOW_NS - 10_000_000,
        "evidence": evidence(tmp_path, "rtde-outage"),
    }
    status = reduce(tmp_path, row)
    assert status["blocker"]["class"] == "BLOCKED_EXTERNAL"
    assert "RTDE_UNREACHABLE" in status["blocker"]["reason_codes"]

    row["external_blocker"]["observed_at_unix_ns"] = NOW_NS - 2_000_000_000
    stale = reduce(tmp_path, row)
    assert "RTDE_UNREACHABLE" not in stale["blocker"]["reason_codes"]
    assert "UNKNOWN_OBSERVATION" in stale["blocker"]["reason_codes"]


def test_fresh_external_evidence_outranks_derived_physical_absence(
    tmp_path: Path,
) -> None:
    row = observed_attestation(tmp_path)
    row["controller"]["loaded_program"] = None
    row["external_blocker"] = {
        "reason_code": "DASHBOARD_UNREACHABLE",
        "observed_at_unix_ns": NOW_NS - 10_000_000,
        "evidence": evidence(tmp_path, "dashboard-outage"),
    }

    status = reduce(tmp_path, row)

    assert "DASHBOARD_LOADED_PROGRAM_MISMATCH" in status["blocker"]["reason_codes"]
    assert status["blocker"]["class"] == "BLOCKED_EXTERNAL"
    assert status["next_action"] == "restore_external_dependency"


def test_repeated_status_reanchor_has_no_hidden_state(tmp_path: Path) -> None:
    row = observed_attestation(tmp_path)
    pointer = publish_observed_attestation(tmp_path, row)
    first = reduce(tmp_path, row, pointer=pointer)
    second = reduce(tmp_path, row, pointer=pointer)

    assert first == second
    assert first["schema"] == GOVERNED_STATUS_SCHEMA
    assert isinstance(first["next_action"], str) and first["next_action"]
    assert "next_actions" not in first
    for field in (
        "release",
        "predicates",
        "bridge",
        "controller",
        "campaign_lease",
        "terminal",
        "blocker",
        "outcome",
        "next_action",
    ):
        assert field in first


def test_queue_integrity_error_is_explicit_internal_blocker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    campaign = tmp_path / "campaign"
    plan = campaign / "control/candidate_plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({"schema_version": "obsolete"}), encoding="utf-8")

    assert (
        cli.main(
            [
                "--experiment-root",
                str(ROOT),
                "--campaign-root",
                str(campaign),
                "status",
                "--json",
            ]
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)
    assert status["blocker"]["class"] == "INTERNAL"
    assert "QUEUE_INTEGRITY_ERROR" in status["blocker"]["reason_codes"]
    assert "queue" not in status
    assert "legacy" not in status
    assert isinstance(status["next_action"], str)


def test_hidden_cli_records_started_and_failed_launch_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    campaign = tmp_path / "campaign"
    common = [
        "--experiment-root",
        str(ROOT),
        "--campaign-root",
        str(campaign),
        "--_launch-attempt-id",
        "shell-attempt-1",
        "--_launch-attempt-phase",
        "status_before",
    ]
    assert cli.main([*common, "--_launch-attempt-state", "STARTED"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                *common,
                "--_launch-attempt-state",
                "FAILED",
                "--_launch-attempt-exit-code",
                "41",
                "--_launch-attempt-reason-code",
                "LAUNCH_ATTEMPT_FAILED",
                "--_launch-attempt-detail",
                "status_before exited 41",
            ]
        )
        == 0
    )
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["attestation"]["state"] == "FAILED"
    assert recorded["attestation"]["exit_code"] == 41
    assert load_current_launch_attempt(campaign)[0] == recorded["attestation"]
    assert "_launch-attempt" not in cli.build_parser().format_help()
