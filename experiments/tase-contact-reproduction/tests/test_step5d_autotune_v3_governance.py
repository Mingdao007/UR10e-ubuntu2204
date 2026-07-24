from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path

import pytest

from step5d_autotune_v3 import cli, governance
from step5d_autotune_v3.governance import (
    BRIDGE_HEARTBEAT_MAX_AGE_NS,
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


def test_governance_process_identity_rejects_zombie_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Path.read_text

    def fake_read_text(path: Path, *args, **kwargs) -> str:
        if path == Path("/proc/43/stat"):
            return "43 (zombie child) Z " + " ".join(["0"] * 18 + ["701"])
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert governance.read_proc_starttime_ticks(43) is None


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
        program_id="step5d_strict_rnn_autotune_v3_r012",
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
            "step5d_strict_rnn_autotune_v3_r012.urp"
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


def test_offline_proof_status_uses_content_binding_without_replaying_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from step5d_autotune_v3 import qualification, release_certificate
    from step5d_autotune_v3 import release_identity

    payload = {
        "content": "already fully validated by qualification",
        "completed_at_unix_ns": NOW_NS,
    }
    evidence_path = tmp_path / "qualification/evidence/proof/qualification.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")
    evidence_sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    scope = release_certificate.release_certificate_scope(
        release_manifest_sha256=release().manifest_sha256,
        source_fingerprint=release().source_fingerprint,
        source_files_fingerprint=digest("source-files"),
        launcher_sha256=release().launcher_sha256,
        control_environment_sha256=digest("control-environment"),
        process_tree_fingerprint=digest("safety-process-tree"),
        runtime_epoch=digest("runtime-epoch"),
        endpoint_content_sha256=digest("endpoint-content"),
        qualification_profile="formal_transition_v1",
        claim_class="state_machine_contract",
        optimizer_exercised=False,
    )
    certificate_path = release_certificate.certificate_path(tmp_path, scope)
    certificate_path.parent.mkdir(parents=True)
    certificate_path.write_text("{}\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def validate_binding(
        observed: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(observed)
        return {
            "environment": {"fingerprint": digest("environment")},
            "process_tree": {"fingerprint": digest("process-tree")},
        }

    monkeypatch.setattr(
        qualification,
        "release_certificate_scope_for_release",
        lambda *_args, **_kwargs: scope,
    )
    monkeypatch.setattr(
        release_identity,
        "load_current_release",
        lambda _root: type(
            "Release",
            (),
            {"manifest_sha256": release().manifest_sha256},
        )(),
    )
    monkeypatch.setattr(
        release_certificate,
        "load_release_certificate",
        lambda *_args, **_kwargs: ({}, evidence_path, payload),
    )
    monkeypatch.setattr(
        qualification,
        "validate_qualification_binding",
        validate_binding,
    )
    monkeypatch.setattr(
        qualification,
        "validate_qualification_result",
        lambda *_args, **_kwargs: pytest.fail(
            "status replayed full qualification lifecycle"
        ),
    )

    proof = governance._load_current_offline_proof(
        ROOT,
        tmp_path,
        release(),
    )

    assert calls == [payload]
    assert proof is not None
    assert proof["evidence"]["sha256"] == evidence_sha
    assert proof["environment_sha256"] == digest("environment")
    assert proof["process_tree_fingerprint"] == digest("process-tree")


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


def test_old_controller_get_remains_valid_when_content_binding_is_current(
    tmp_path: Path,
) -> None:
    row = observed_attestation(tmp_path)
    old_get_at = NOW_NS - 86_400_000_000_000
    row["controller"]["fresh_get_observed_at_unix_ns"] = old_get_at
    row["controller"]["delivery_observation"] = delivery_evidence(
        tmp_path, old_get_at
    )

    status = reduce(tmp_path, row)

    assert status["predicates"]["controller_fresh"] is True
    assert status["predicates"]["controller_fresh_get"] is True


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
        "runtime_package_changed",
        "runtime_lock_changed",
        "host_contract_changed",
        "gpu_identity_changed",
        "gpu_functional_evidence_changed",
        "owner_dependency_changed",
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


def test_launch_attempt_admits_monotonic_manual_bridge_phases(tmp_path: Path) -> None:
    phases = (
        "runtime_gate",
        "route_resolve",
        "manual_qualification",
        "manual_context",
        "manual_preflight",
        "manual_bridge_start",
        "manual_campaign",
    )
    for offset, phase in enumerate(phases):
        recorded = publish_launch_attempt(
            tmp_path,
            attempt_id="manual-attempt-1",
            state="STARTED",
            phase=phase,
            observed_at_unix_ns=NOW_NS + offset,
        )
        assert recorded["attestation"]["sequence"] == offset + 1
        assert recorded["attestation"]["phase"] == phase


def test_launch_attempt_v2_binds_route_runtime_and_owner(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    campaign = tmp_path / "campaign"
    output.mkdir()
    campaign.mkdir()
    snapshot = output / "route.json"
    snapshot.write_text('{"route":"manual_v2"}\n', encoding="utf-8")
    bindings = {
        "repository_head": "a" * 40,
        "runtime_environment_id": digest("runtime"),
        "campaign_root": str(campaign),
        "output_root": str(output),
        "resource_owner": {
            "pid": os.getpid(),
            "starttime_ticks": governance.read_proc_starttime_ticks(os.getpid()),
            "authority_epoch": 1,
        },
        "route_snapshot": None,
    }
    started = publish_launch_attempt(
        tmp_path,
        attempt_id="manual-v2-attempt",
        state="STARTED",
        phase="runtime_gate",
        route="UNKNOWN",
        bindings=bindings,
        observed_at_unix_ns=NOW_NS,
    )
    assert started["attestation"]["schema"] == governance.LAUNCH_ATTEMPT_SCHEMA
    assert started["attestation"]["bindings"]["resource_owner"]["pid"] == os.getpid()

    publish_launch_attempt(
        tmp_path,
        attempt_id="manual-v2-attempt",
        state="PASSED",
        phase="runtime_gate",
        route="UNKNOWN",
        bindings=bindings,
        observed_at_unix_ns=NOW_NS + 1,
    )
    publish_launch_attempt(
        tmp_path,
        attempt_id="manual-v2-attempt",
        state="STARTED",
        phase="route_resolve",
        route="UNKNOWN",
        bindings=bindings,
        observed_at_unix_ns=NOW_NS + 2,
    )
    bindings["route_snapshot"] = {
        "path": str(snapshot),
        "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
    }
    passed = publish_launch_attempt(
        tmp_path,
        attempt_id="manual-v2-attempt",
        state="PASSED",
        phase="route_resolve",
        route="manual_v2",
        bindings=bindings,
        observed_at_unix_ns=NOW_NS + 3,
    )
    assert passed["attestation"]["route"] == "manual_v2"
    assert passed["attestation"]["bindings"]["route_snapshot"]["sha256"]

    with pytest.raises(GovernanceError, match="next phase"):
        publish_launch_attempt(
            tmp_path,
            attempt_id="manual-v2-attempt",
            state="STARTED",
            phase="route_resolve",
            route="manual_v2",
            bindings=bindings,
            observed_at_unix_ns=NOW_NS + 4,
        )


def test_launch_attempt_v2_rejects_mid_attempt_authority_binding_drift(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    campaign = tmp_path / "campaign"
    output.mkdir()
    campaign.mkdir()
    bindings = {
        "repository_head": "a" * 40,
        "runtime_environment_id": digest("runtime"),
        "campaign_root": str(campaign),
        "output_root": str(output),
        "resource_owner": {
            "pid": os.getpid(),
            "starttime_ticks": governance.read_proc_starttime_ticks(os.getpid()),
            "authority_epoch": 1,
        },
        "route_snapshot": None,
    }
    publish_launch_attempt(
        tmp_path,
        attempt_id="immutable-bindings",
        state="STARTED",
        phase="runtime_gate",
        route="UNKNOWN",
        bindings=bindings,
        observed_at_unix_ns=NOW_NS,
    )
    changed = json.loads(json.dumps(bindings))
    changed["resource_owner"]["authority_epoch"] = 2
    with pytest.raises(GovernanceError, match="resource_owner binding cannot change"):
        publish_launch_attempt(
            tmp_path,
            attempt_id="immutable-bindings",
            state="PASSED",
            phase="runtime_gate",
            route="UNKNOWN",
            bindings=changed,
            observed_at_unix_ns=NOW_NS + 1,
        )


def test_launch_attempt_records_closed_world_route_blocker(tmp_path: Path) -> None:
    output = tmp_path / "output"
    campaign = tmp_path / "campaign"
    output.mkdir()
    campaign.mkdir()
    snapshot = output / "route.json"
    snapshot.write_text('{"route":"BLOCKED"}\n', encoding="utf-8")
    bindings = {
        "repository_head": "a" * 40,
        "runtime_environment_id": digest("runtime"),
        "campaign_root": str(campaign),
        "output_root": str(output),
        "resource_owner": {
            "pid": os.getpid(),
            "starttime_ticks": governance.read_proc_starttime_ticks(os.getpid()),
            "authority_epoch": 1,
        },
        "route_snapshot": None,
    }
    publish_launch_attempt(
        tmp_path,
        attempt_id="unsupported-route",
        state="STARTED",
        phase="route_resolve",
        route="UNKNOWN",
        bindings=bindings,
        observed_at_unix_ns=NOW_NS,
    )
    bindings["route_snapshot"] = {
        "path": str(snapshot),
        "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
    }
    failed = publish_launch_attempt(
        tmp_path,
        attempt_id="unsupported-route",
        state="FAILED",
        phase="route_resolve",
        route="BLOCKED",
        bindings=bindings,
        observed_at_unix_ns=NOW_NS + 1,
        exit_code=3,
        reason_code="LOADED_PROGRAM_UNSUPPORTED",
        detail="loaded program is outside the closed route set",
    )
    assert failed["attestation"]["route"] == "BLOCKED"
    assert failed["attestation"]["reason_code"] == "LOADED_PROGRAM_UNSUPPORTED"


def test_launch_attempt_completed_is_terminal(tmp_path: Path) -> None:
    output = tmp_path / "output"
    campaign = tmp_path / "campaign"
    output.mkdir()
    campaign.mkdir()
    bindings = {
        "repository_head": "a" * 40,
        "runtime_environment_id": digest("runtime"),
        "campaign_root": str(campaign),
        "output_root": str(output),
        "resource_owner": {
            "pid": os.getpid(),
            "starttime_ticks": governance.read_proc_starttime_ticks(os.getpid()),
            "authority_epoch": 1,
        },
        "route_snapshot": None,
    }
    started = publish_launch_attempt(
        tmp_path,
        attempt_id="completed-attempt",
        state="STARTED",
        phase="live_handoff",
        route="UNKNOWN",
        bindings=bindings,
        observed_at_unix_ns=NOW_NS,
    )
    assert started["attestation"]["state"] == "STARTED"
    completed = publish_launch_attempt(
        tmp_path,
        attempt_id="completed-attempt",
        state="COMPLETED",
        phase="live_handoff",
        route="UNKNOWN",
        bindings=bindings,
        observed_at_unix_ns=NOW_NS + 1,
    )
    assert completed["attestation"]["state"] == "COMPLETED"
    with pytest.raises(GovernanceError, match="terminal"):
        publish_launch_attempt(
            tmp_path,
            attempt_id="completed-attempt",
            state="STARTED",
            phase="live_handoff",
            route="UNKNOWN",
            bindings=bindings,
            observed_at_unix_ns=NOW_NS + 2,
        )


def test_launch_attempt_external_class_requires_positive_evidence(
    tmp_path: Path,
) -> None:
    row = {
        "schema": governance.LAUNCH_ATTEMPT_SCHEMA_V1,
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
        lambda _experiment, _certificate_root, _release: None,
    )
    monkeypatch.setattr(
        governance,
        "_environment_status",
        lambda _root: (
            {
                "required_environment_id": digest("environment"),
                "observed_environment_id": digest("environment"),
                "control_ready": True,
                "optimizer_ready": True,
                "host_contract_ready": True,
                "gpu_identity_ready": True,
                "gpu_functional_proven": True,
                "environment_attestation_sha256": None,
                "blocker": {"reason_code": None, "detail": None},
            },
            [],
            [],
        ),
    )


@pytest.mark.parametrize(
    ("reason", "next_action"),
    [
        ("RUNTIME_NOT_PROVISIONED", "provision_runtime"),
        ("RUNTIME_LOCK_MISMATCH", "provision_runtime_for_current_lock"),
        ("RUNTIME_PACKAGE_INTEGRITY_MISMATCH", "reprovision_runtime"),
        ("CONTROL_RUNTIME_INVALID", "reprovision_control_runtime"),
        ("OPTIMIZER_RUNTIME_INVALID", "reprovision_optimizer_runtime"),
        ("HOST_CONTRACT_MISMATCH", "restore_host_contract"),
        ("GPU_IDENTITY_MISMATCH", "restore_governed_gpu_identity"),
        ("GPU_FUNCTIONAL_GATE_MISSING", "run_native_gpu_functional_gates"),
        ("OWNER_DEPENDENCY_MISMATCH", "restore_owner_dependency"),
        ("ACTIVE_SOURCE_CLOSURE_UNRESOLVED", "resolve_active_source_closure"),
    ],
)
def test_environment_reason_has_one_internal_recovery_action(
    reason: str,
    next_action: str,
) -> None:
    assert reason in governance.INTERNAL_REASON_CODES
    assert governance._next_action(None, [reason], False) == next_action


def test_environment_blocker_invalidates_status_before_play(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_governed_release(monkeypatch)
    monkeypatch.setattr(
        governance,
        "_environment_status",
        lambda _root: (
            {
                "required_environment_id": digest("required-environment"),
                "observed_environment_id": None,
                "control_ready": False,
                "optimizer_ready": False,
                "host_contract_ready": False,
                "gpu_identity_ready": False,
                "gpu_functional_proven": False,
                "environment_attestation_sha256": None,
                "blocker": {
                    "reason_code": "RUNTIME_NOT_PROVISIONED",
                    "detail": "current runtime pointer is missing",
                },
            },
            ["RUNTIME_NOT_PROVISIONED"],
            [
                {
                    "role": "runtime_environment",
                    "path": None,
                    "sha256": None,
                    "detail": "current runtime pointer is missing",
                }
            ],
        ),
    )

    status = resolve_governed_status(ROOT, tmp_path, now_ns=NOW_NS)

    assert status["environment"]["required_environment_id"] == digest(
        "required-environment"
    )
    assert status["environment"]["control_ready"] is False
    assert status["environment"]["gpu_functional_proven"] is False
    assert status["blocker"]["class"] == "INTERNAL"
    assert status["blocker"]["reason_codes"][0] == "RUNTIME_NOT_PROVISIONED"
    assert status["predicates"]["offline_proven"] is False
    assert status["predicates"]["play_prompt_ready"] is False
    assert status["predicates"]["bench_ready"] is False
    assert status["state"] is None
    assert status["next_action"] == "provision_runtime"


def test_status_freshness_is_anchored_before_slow_environment_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_governed_release(monkeypatch)
    publish_observed_attestation(tmp_path, observed_attestation(tmp_path))
    clock = {"now_ns": NOW_NS}
    environment_status = governance._environment_status

    def slow_environment_status(root: Path):
        clock["now_ns"] += 30_000_000_000
        return environment_status(root)

    monkeypatch.setattr(governance, "_environment_status", slow_environment_status)
    monkeypatch.setattr(governance.time, "time_ns", lambda: clock["now_ns"])

    status = resolve_governed_status(
        ROOT,
        tmp_path,
        proc_starttime_reader=process_reader,
    )

    assert status["generated_at_unix_ns"] == NOW_NS
    assert status["predicates"]["bridge_heartbeat_fresh"] is True
    assert status["predicates"]["controller_fresh"] is True
    assert status["predicates"]["rtde_fresh"] is True
    assert status["predicates"]["kunwei_fresh"] is True
    assert status["predicates"]["mailbox_clean"] is True
    assert status["state"] == "BENCH_READY"


def test_status_uses_observed_snapshot_time_for_freshness_when_observation_is_published_mid_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_governed_release(monkeypatch)
    clock = {"now_ns": NOW_NS}

    def delayed_publish_environment_status(root: Path):
        clock["now_ns"] += 340_000_000
        row = observed_attestation(tmp_path)
        future_snapshot = clock["now_ns"]
        row["observed_at_unix_ns"] = future_snapshot
        row["process"]["heartbeat_at_unix_ns"] = future_snapshot - 50_000_000
        row["controller"]["observed_at_unix_ns"] = future_snapshot - 50_000_000
        row["controller"]["fresh_get_observed_at_unix_ns"] = future_snapshot - 100_000_000
        row["controller"]["rtde_observed_at_unix_ns"] = future_snapshot - 20_000_000
        row["controller"]["kunwei_observed_at_unix_ns"] = future_snapshot - 20_000_000
        row["mailbox"]["observed_at_unix_ns"] = future_snapshot - 20_000_000
        row["controller"]["delivery_observation"] = delivery_evidence(
            tmp_path, row["controller"]["fresh_get_observed_at_unix_ns"]
        )
        publish_observed_attestation(tmp_path, row)
        return (
            {
                "required_environment_id": digest("environment"),
                "observed_environment_id": digest("environment"),
                "control_ready": True,
                "optimizer_ready": True,
                "host_contract_ready": True,
                "gpu_identity_ready": True,
                "gpu_functional_proven": True,
                "environment_attestation_sha256": None,
                "blocker": {"reason_code": None, "detail": None},
            },
            [],
            [],
        )

    monkeypatch.setattr(governance, "_environment_status", delayed_publish_environment_status)
    monkeypatch.setattr(governance.time, "time_ns", lambda: clock["now_ns"])

    status = resolve_governed_status(
        ROOT,
        tmp_path,
        proc_starttime_reader=process_reader,
    )

    assert status["generated_at_unix_ns"] == NOW_NS
    assert status["attestation"]["observed_at_unix_ns"] > NOW_NS
    assert status["predicates"]["bridge_heartbeat_fresh"] is True
    assert status["predicates"]["controller_fresh"] is True
    assert status["predicates"]["rtde_fresh"] is True
    assert status["predicates"]["kunwei_fresh"] is True
    assert status["predicates"]["mailbox_clean"] is True
    assert status["state"] == "BENCH_READY"


def test_future_observation_cannot_advance_freshness_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_governed_release(monkeypatch)
    row = observed_attestation(tmp_path)
    future_snapshot = NOW_NS + 86_400_000_000_000
    row["observed_at_unix_ns"] = future_snapshot
    row["process"]["heartbeat_at_unix_ns"] = future_snapshot
    row["controller"]["observed_at_unix_ns"] = future_snapshot
    row["controller"]["fresh_get_observed_at_unix_ns"] = future_snapshot
    row["controller"]["rtde_observed_at_unix_ns"] = future_snapshot
    row["controller"]["kunwei_observed_at_unix_ns"] = future_snapshot
    row["mailbox"]["observed_at_unix_ns"] = future_snapshot
    row["controller"]["delivery_observation"] = delivery_evidence(
        tmp_path,
        future_snapshot,
    )
    publish_observed_attestation(tmp_path, row)

    status = resolve_governed_status(
        ROOT,
        tmp_path,
        now_ns=NOW_NS,
        proc_starttime_reader=process_reader,
    )

    assert "OBSERVED_ATTESTATION_FUTURE" in status["blocker"]["reason_codes"]
    assert status["state"] != "BENCH_READY"


def test_live_status_consumes_attested_hotpath_without_requalification() -> None:
    source = inspect.getsource(governance._environment_status)

    assert "runtime_status(full_integrity=False)" in source
    assert "production_source_closure_report" not in source


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


def test_hidden_cli_rejects_unbound_legacy_launch_attempt(
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
    assert cli.main([*common, "--_launch-attempt-state", "STARTED"]) == 2
    captured = capsys.readouterr()
    assert "unbound launch-attempt recording is retired" in captured.err
    assert not (campaign / "governance/current-launch.json").exists()
    assert "_launch-attempt" not in cli.build_parser().format_help()


def test_hidden_cli_is_fenced_to_active_authority_owner(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import step5d_bridge_authority as bridge_authority

    campaign = tmp_path / "campaign"
    output = tmp_path / "output"
    owner_pid = 42
    owner_starttime = 700
    monkeypatch.setattr(os, "getppid", lambda: owner_pid)
    monkeypatch.setattr(
        bridge_authority,
        "read_proc_starttime_ticks",
        lambda pid: owner_starttime if pid == owner_pid else None,
    )
    monkeypatch.setattr(
        governance,
        "read_proc_starttime_ticks",
        lambda pid: owner_starttime if pid == owner_pid else None,
    )
    bridge_authority.begin(
        campaign,
        attempt_id="shell-attempt-bound",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
    )
    common = [
        "--experiment-root",
        str(ROOT),
        "--campaign-root",
        str(campaign),
        "--_launch-attempt-id",
        "shell-attempt-bound",
        "--_launch-attempt-phase",
        "status_before",
        "--_launch-attempt-route",
        "UNKNOWN",
        "--_launch-repository-head",
        "a" * 40,
        "--_launch-runtime-environment-id",
        "b" * 64,
        "--_launch-campaign-path",
        str(campaign),
        "--_launch-output-root",
        str(output),
        "--_launch-owner-pid",
        str(owner_pid),
        "--_launch-owner-starttime",
        str(owner_starttime),
        "--_launch-owner-authority-epoch",
        "1",
    ]
    assert cli.main([*common, "--_launch-attempt-state", "STARTED"]) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["attestation"]["state"] == "STARTED"
    before, before_pointer = load_current_launch_attempt(campaign)

    monkeypatch.setattr(os, "getppid", lambda: owner_pid + 1)
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
                "sibling recorder rejected",
            ]
        )
        == 2
    )
    assert "active authority-owner child" in capsys.readouterr().err
    after, after_pointer = load_current_launch_attempt(campaign)
    assert after == before
    assert after_pointer == before_pointer
