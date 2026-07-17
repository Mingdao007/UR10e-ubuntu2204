from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_autotune_v2.bridge as bridge_module
from step5d_autotune_v2.bridge import BridgeError, BridgeProcess
from step5d_autotune_v2.cli import _fresh_status
from step5d_autotune_v2.heartbeat import HeartbeatError, HeartbeatPublisher
from step5d_autotune_v2.mailbox import AtomicMailbox
from step5d_autotune_v2.model import BatchSpec, CandidateSpec, DeploymentSpec
from step5d_autotune_v2.reducer import LifecycleEvent
from step5d_autotune_v2.report import (
    reconcile_missing_trial_reports,
    render_trial_report,
)
from step5d_autotune_v2.repository import Repository, RepositoryError
from step5d_autotune_v2.runtime import JsonlBridgePort
from step5d_autotune_v2.supervisor import CampaignSupervisor, RuntimeFailure
from test_step5d_autotune_v2_supervisor import (
    FakePort,
    batch,
    deployment,
    repository,
)


EXPECTED_COMPARISON_KEYS = {
    "G1": "8971c2834cd9e496730014e5a9d086053b99d06b89dede9be0622961a3cdd7af",
    "G2": "e4b6283d0b335fd455d72b275708f7d85ee7c636c2111cb710cf24d5f1b9eb79",
    "G3": "4ff7eb0e2c361f72398f65a05c709bcaa93e62a4df0ada35689ee27a41c23e33",
    "G4": "3ba389181b3cc55e766b52671a058d309f4a2dfab2bd44647a5b5770ef47afd8",
    "G5": "39b617c75130f52896eedd6c87fa7b35926e654745796e81721d2d4e394191f3",
    "G6": "6023caf15278883d65c7a989ace9fa11e0df6f5cca1751781d2ddf8d5f5b6ef8",
    "G7": "d86be2579add83e0105073e56e6171d27bd3242aa4c5cb21231997e56664064d",
    "G8": "d573f9ea6706ff3e0097005db2c9dc4230bc6ee3e5f899acf574d72917e97769",
    "G9": "473e147f98221aab9d492ad4e9a90e153d79813c1a185ac1fd7d9a1c9d1490fb",
    "G10": "3f533ad5c1f3646687b444ab959bb865682cc6e1d19877ba56404b97ab87d304",
    "G11": "2231b3e298252e60d892c225b32e6175485ac155a8c449e30f9458da6d1fe459",
    "G12": "20576ec9eb52d77990ecc3f65c57a8dc588ff3875310875f3a1a1c67e0f124a5",
    "G13": "e2eb89641db880db3f37910eb8ff2ff3dc038debc0bdd06d0a3ce0ac754b0ea6",
    "G14": "da8700025c5fe36c9b291033507cde2b0d849d6ccab0bd08bb1f3187c4fbf46f",
    "G15": "35fa37e8a924123f8f5513a5619dcafe3f81f6f06880d4674b2b059c672432fd",
}


def test_health_reader_accepts_complete_inode_unlinked_by_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    health_path = tmp_path / "bridge_health.json"

    def payload(sequence: int) -> dict[str, object]:
        return {
            "schema": bridge_module.HEALTH_SCHEMA,
            "deployment_id": "deployment",
            "pid": 123,
            "launch_nonce": "a" * 64,
            "health_sequence": sequence,
            "observed_at": "2026-07-17T00:00:00+00:00",
            "configured_rate_hz": 500,
            "sample_counter": sequence,
            "rtde_healthy": True,
            "command_transport_healthy": True,
            "event_transport_healthy": True,
        }

    health_path.write_text(json.dumps(payload(1)), encoding="ascii")
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(payload(2)), encoding="ascii")
    real_open = bridge_module.os.open

    def open_then_replace(path: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == health_path:
            bridge_module.os.replace(replacement, health_path)
        return descriptor

    monkeypatch.setattr(bridge_module.os, "open", open_then_replace)
    observed = bridge_module._read_health_snapshot(health_path)
    assert observed is not None
    assert observed["health_sequence"] == 1


def _legacy_candidates() -> dict[str, CandidateSpec]:
    payload = json.loads(
        (ROOT / "config/step5/autotune_legacy_map_v1.json").read_text(
            encoding="utf-8"
        )
    )
    result: dict[str, CandidateSpec] = {}
    allowed = {
        "group_id",
        "p",
        "i",
        "d",
        "profile_id",
        "log2_p",
        "log2_i",
        "log2_d",
        "i_multiplier",
    }
    for batch_row in payload["batches"]:
        for row in batch_row["candidates"]:
            candidate = CandidateSpec.from_mapping(
                {key: value for key, value in row.items() if key in allowed}
            )
            result[candidate.group_id] = candidate
    return result


def _candidate_with_profile(candidate: CandidateSpec, profile_id: str) -> CandidateSpec:
    return CandidateSpec(
        group_id=candidate.group_id,
        p=candidate.p,
        i=candidate.i,
        d=candidate.d,
        profile_id=profile_id,
        log2_p=candidate.log2_p,
        log2_i=candidate.log2_i,
        log2_d=candidate.log2_d,
        i_multiplier=candidate.i_multiplier,
    )


def _other_deployment(identity: str) -> DeploymentSpec:
    current = deployment()
    return DeploymentSpec(
        deployment_id=identity,
        code_fingerprint="4" * 64,
        tp_fingerprint=current.tp_fingerprint,
        guard_fingerprint=current.guard_fingerprint,
        profile=current.profile,
        deployment_authorized=True,
        controller_readback_verified=True,
    )


def test_profile_id_is_derived_and_g1_g15_comparison_keys_are_frozen() -> None:
    assert deployment().profile_id == "nf050-slew050-a050"
    actual = {
        group: candidate.comparison_key
        for group, candidate in _legacy_candidates().items()
    }
    assert actual == EXPECTED_COMPARISON_KEYS


def test_enqueue_profile_mismatch_rolls_back_the_whole_batch(tmp_path: Path) -> None:
    repo = Repository((tmp_path / "campaign.sqlite3").resolve())
    repo.initialize()
    repo.register_deployment(deployment())
    wrong = tuple(
        _candidate_with_profile(candidate, "caller-controlled")
        for candidate in batch().candidates
    )
    with pytest.raises(RepositoryError, match="differs from deployment profile"):
        repo.enqueue_batch(
            BatchSpec("wrong-profile", "negative fixture", wrong),
            deployment_id=deployment().deployment_id,
        )
    assert repo.batch_detail("wrong-profile") is None
    assert repo.list_candidates() == []


def test_create_trial_and_arm_reject_profile_bypass(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    candidate = repo.next_pending_candidate()
    assert candidate is not None
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE candidates SET profile_id='tampered' WHERE candidate_id=?",
            (candidate["candidate_id"],),
        )
    with pytest.raises(RepositoryError, match="differs from the deployment profile"):
        repo.create_trial(
            candidate_id=candidate["candidate_id"],
            deployment_id=deployment().deployment_id,
        )

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE candidates SET profile_id=? WHERE candidate_id=?",
            (deployment().profile_id, candidate["candidate_id"]),
        )
    trial_id = repo.create_trial(
        candidate_id=candidate["candidate_id"],
        deployment_id=deployment().deployment_id,
    )
    repo.apply_lifecycle_event(trial_id, LifecycleEvent.PERSIST_ARM)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    port = JsonlBridgePort(
        repository=repo,
        mailbox=AtomicMailbox((runtime / "command.json").resolve()),
        event_path=runtime / "events.jsonl",
        allowed_artifact_root=runtime,
        deployment_id=deployment().deployment_id,
        analyzer_argv=(),
        command_root=ROOT,
        transfer_destination=None,
    )
    tampered = repo.candidate(candidate["candidate_id"])
    tampered["profile_id"] = "caller-controlled"
    with pytest.raises(RuntimeFailure, match="differs from the deployment profile"):
        port.publish_arm(
            trial_id=trial_id,
            sequence=repo.trial_detail(trial_id)["arm_sequence"],
            candidate=tampered,
        )
    assert not (runtime / "command.json").exists()


def test_current_incumbent_is_profile_bound_and_g10_stays_historical(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    legacy = _other_deployment("legacy-g10")
    repo.register_deployment(legacy)
    g10 = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id=legacy.deployment_id, maximum=1
    )[0]
    g11 = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]

    current = repo.diagnostic_incumbent(
        deployment_id=deployment().deployment_id,
        profile_id=deployment().profile_id,
    )
    historical = repo.historical_diagnostic_reference(
        profile_id=deployment().profile_id, group_id="G10"
    )
    assert current is not None and current["trial_id"] == g11.trial_id
    assert historical is not None and historical["trial_id"] == g10.trial_id
    assert historical["optimizer_history"] is False

    report = render_trial_report(repo, trial_id=g11.trial_id)
    assert "历史诊断参考 G10（只读）" in report
    assert len([line for line in report.splitlines() if line.startswith("|---")]) == 2


def test_startup_report_reconciliation_is_current_epoch_only_and_idempotent(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    legacy = _other_deployment("legacy-g10")
    repo.register_deployment(legacy)
    old = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id=legacy.deployment_id, maximum=1
    )[0]
    current = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]

    recovered = reconcile_missing_trial_reports(
        repo,
        deployment_id=deployment().deployment_id,
        output_root=tmp_path / "reports",
    )
    assert len(recovered) == 1
    assert repo.artifact_for_trial(
        current.trial_id, role="trial_markdown_report"
    ) is not None
    assert repo.artifact_for_trial(old.trial_id, role="trial_markdown_report") is None
    assert reconcile_missing_trial_reports(
        repo,
        deployment_id=deployment().deployment_id,
        output_root=tmp_path / "reports",
    ) == []
    assert repo.next_pending_candidate()["group_id"] == "G12"


REPEATING_HEALTH_CHILD = r"""
import json, os, time
from datetime import datetime, timezone
from pathlib import Path
root = Path(os.environ['STEP5D_AUTOTUNE_V2_RUNTIME_ROOT'])
def publish(name, payload):
    temporary = root / ('.' + name + '.tmp')
    temporary.write_text(json.dumps(payload), encoding='ascii')
    temporary.replace(root / name)
identity = {
    'deployment_id': os.environ['STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID'],
    'launch_nonce': os.environ['STEP5D_AUTOTUNE_V2_LAUNCH_NONCE'],
    'pid': os.getpid(),
}
publish('bridge_ready.json', {
    'schema': 'step5d.autotune.bridge-ready/v2',
    'bridge_ready': True,
    **identity,
    'sample_rate_hz': 500,
    'startup_stationary_verified': True,
    'command_transport_ready': True,
    'event_transport_ready': True,
})
sequence = 0
while True:
    sequence += 1
    publish('bridge_health.json', {
        'schema': 'step5d.autotune.bridge-health/v2',
        **identity,
        'health_sequence': sequence,
        'observed_at': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
        'configured_rate_hz': 500,
        'sample_counter': sequence,
        'rtde_healthy': True,
        'command_transport_healthy': True,
        'event_transport_healthy': True,
    })
    time.sleep(0.002)
"""


STATIC_HEALTH_CHILD = REPEATING_HEALTH_CHILD.replace(
    "while True:\n    sequence += 1", "while sequence < 2:\n    sequence += 1"
).replace("    time.sleep(0.002)\n", "    time.sleep(0.2)\n") + "\ntime.sleep(10)\n"


def _bridge(tmp_path: Path, child: str) -> BridgeProcess:
    bridge = BridgeProcess(
        argv=(sys.executable, "-c", child),
        root=ROOT,
        runtime_root=tmp_path / "bridge-runtime",
        deployment_id=deployment().deployment_id,
    )
    bridge.start(timeout_s=2.0)
    return bridge


def _health_details(bridge: BridgeProcess) -> dict[str, object]:
    health = bridge.check_health(max_age_s=2.0, require_progress=True)
    return {
        "health_sequence": health.health_sequence,
        "sample_counter": health.sample_counter,
        "configured_rate_hz": health.configured_rate_hz,
    }


def _live_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        deployment=deployment(),
        payload={"live_cutover": {"enabled": True, "blocked_until": []}},
        runtime_root_path=tmp_path,
    )


class _StatusRepository:
    def __init__(self, observed_at: str, *, details: dict[str, object] | None = None):
        self.observed_at = observed_at
        self.details = details or {"deployment_id": deployment().deployment_id}

    def status(self) -> dict[str, object]:
        return {
            "runtime_ready": True,
            "deployment_authorized": True,
            "primary_blocker": None,
            "observed_at": self.observed_at,
            "fresh": False,
            "details": dict(self.details),
        }


@pytest.mark.parametrize(
    "timestamp_case",
    ("naive", "future"),
    ids=("naive_timestamp", "future_beyond_clock_skew"),
)
def test_cli_status_rejects_naive_or_excessively_future_runtime_timestamp(
    tmp_path: Path, timestamp_case: str
) -> None:
    now = datetime.now(timezone.utc)
    observed_at = (
        now.replace(tzinfo=None).isoformat(timespec="microseconds")
        if timestamp_case == "naive"
        else (now + timedelta(seconds=2)).isoformat(timespec="microseconds")
    )
    status = _fresh_status(
        _StatusRepository(observed_at), 5.0, _live_config(tmp_path)
    )
    assert status["fresh"] is False
    assert status["runtime_ready"] is False
    assert status["primary_blocker"] == "runtime_status_stale"


def test_cli_status_allows_only_bounded_positive_clock_skew(tmp_path: Path) -> None:
    observed_at = (datetime.now(timezone.utc) + timedelta(seconds=0.5)).isoformat(
        timespec="microseconds"
    )
    status = _fresh_status(
        _StatusRepository(observed_at), 5.0, _live_config(tmp_path)
    )
    assert status["fresh"] is True
    assert status["runtime_ready"] is True


def test_child_kill_revokes_ready_within_250ms_and_quarantines_trial(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    token = "writer-token"
    repo.claim_writer(token=token)
    candidate = repo.next_pending_candidate()
    assert candidate is not None
    trial_id = repo.create_trial(
        candidate_id=candidate["candidate_id"],
        deployment_id=deployment().deployment_id,
    )
    repo.apply_lifecycle_event(trial_id, LifecycleEvent.PERSIST_ARM)
    bridge = _bridge(tmp_path, REPEATING_HEALTH_CHILD)
    heartbeat_path = (tmp_path / "host-heartbeat.json").resolve()
    publisher = HeartbeatPublisher(
        repository=repo,
        mailbox=AtomicMailbox(heartbeat_path),
        writer_token=token,
        deployment_id=deployment().deployment_id,
        ready_details={"bridge_pid": bridge.process.pid},
        interval_s=0.05,
        health_probe=lambda: _health_details(bridge),
        health_failure_callback=lambda exc: repo.revoke_runtime_for_bridge_loss(
            deployment_authorized=True,
            primary_blocker="bridge_liveness_lost",
            details={"error": f"{type(exc).__name__}:{exc}"},
        ),
    )
    try:
        time.sleep(0.02)
        bridge.start_watcher(publisher.fail_from_bridge, interval_s=0.02)
        publisher.start()
        before = AtomicMailbox(heartbeat_path).read_latest()
        assert before is not None
        assert repo.status()["runtime_ready"] is True
        started = time.monotonic()
        assert bridge.process is not None
        bridge.process.kill()
        deadline = started + 0.5
        while time.monotonic() < deadline:
            if repo.status()["primary_blocker"] == "bridge_liveness_lost":
                break
            time.sleep(0.005)
        elapsed = time.monotonic() - started
        status = repo.status()
        assert elapsed <= 0.25
        assert status["runtime_ready"] is False
        assert status["primary_blocker"] == "bridge_liveness_lost"
        assert repo.trial_detail(trial_id)["state"] == "uncertain_attempt"
        stopped_at = AtomicMailbox(heartbeat_path).read_latest()
        assert stopped_at is not None
        time.sleep(0.1)
        assert AtomicMailbox(heartbeat_path).read_latest().sequence == stopped_at.sequence
        with pytest.raises(HeartbeatError):
            publisher.check()
    finally:
        try:
            publisher.stop()
        except HeartbeatError:
            pass
        bridge.stop()
        repo.release_writer(token)


def test_production_watcher_sidecar_demotes_within_250ms_under_sqlite_contention(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    token = "contended-writer"
    repo.claim_writer(token=token)
    candidate = repo.next_pending_candidate()
    assert candidate is not None
    trial_id = repo.create_trial(
        candidate_id=candidate["candidate_id"],
        deployment_id=deployment().deployment_id,
    )
    repo.apply_lifecycle_event(trial_id, LifecycleEvent.PERSIST_ARM)
    bridge = _bridge(tmp_path, REPEATING_HEALTH_CHILD)
    ready_payload = json.loads(
        (bridge.runtime_root / "bridge_ready.json").read_text(encoding="ascii")
    )
    heartbeat_path = (tmp_path / "contended-host-heartbeat.json").resolve()
    revocation_path = (bridge.runtime_root / "bridge_revocation.json").resolve()
    publisher = HeartbeatPublisher(
        repository=repo,
        mailbox=AtomicMailbox(heartbeat_path),
        writer_token=token,
        deployment_id=deployment().deployment_id,
        ready_details={
            "bridge_pid": ready_payload["pid"],
            "bridge_launch_nonce": ready_payload["launch_nonce"],
        },
        interval_s=0.05,
        health_probe=lambda: _health_details(bridge),
        health_failure_callback=lambda exc: repo.revoke_runtime_for_bridge_loss(
            deployment_authorized=True,
            primary_blocker="bridge_liveness_lost",
            details={"error": f"{type(exc).__name__}:{exc}"},
        ),
        failure_mailbox=AtomicMailbox(revocation_path),
    )
    holder = sqlite3.connect(repo.path, timeout=0.1, isolation_level=None)
    try:
        time.sleep(0.02)
        bridge.start_watcher(publisher.fail_from_bridge, interval_s=0.1)
        publisher.start()
        assert repo.status()["runtime_ready"] is True
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        assert bridge.process is not None
        bridge.process.kill()
        config = _live_config(bridge.runtime_root)
        status = None
        deadline = started + 0.5
        while time.monotonic() < deadline:
            status = _fresh_status(repo, 5.0, config)
            if status["primary_blocker"] == "bridge_liveness_lost":
                break
            time.sleep(0.005)
        elapsed = time.monotonic() - started
        assert elapsed <= 0.25
        assert status is not None
        assert status["runtime_ready"] is False
        assert status["primary_blocker"] == "bridge_liveness_lost"
        evidence = AtomicMailbox(revocation_path).read_latest()
        assert evidence is not None
        assert evidence.payload["bridge_pid"] == ready_payload["pid"]
        assert evidence.payload["bridge_launch_nonce"] == ready_payload["launch_nonce"]
        assert evidence.payload["durable_revocation"] == "pending"
        with pytest.raises(HeartbeatError):
            publisher.check()
        stopped_sequence = AtomicMailbox(heartbeat_path).read_latest().sequence
        time.sleep(0.12)
        assert AtomicMailbox(heartbeat_path).read_latest().sequence == stopped_sequence

        holder.execute("ROLLBACK")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if repo.status()["primary_blocker"] == "bridge_liveness_lost":
                break
            time.sleep(0.005)
        assert repo.status()["primary_blocker"] == "bridge_liveness_lost"
        assert repo.trial_detail(trial_id)["state"] == "uncertain_attempt"
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()
        try:
            publisher.stop()
        except HeartbeatError:
            pass
        bridge.stop()
        repo.release_writer(token)


def test_failed_durable_revocation_remains_latched_and_preserved_in_sidecar(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    runtime = tmp_path / "failure-runtime"
    runtime.mkdir()
    heartbeat_path = (runtime / "host-heartbeat.json").resolve()
    revocation_path = (runtime / "bridge_revocation.json").resolve()

    def fail_durable_revocation(exc: BaseException) -> None:
        raise sqlite3.OperationalError("database is locked")

    publisher = HeartbeatPublisher(
        repository=repo,
        mailbox=AtomicMailbox(heartbeat_path),
        writer_token="unused",
        deployment_id=deployment().deployment_id,
        ready_details={"bridge_pid": 1234, "bridge_launch_nonce": "a" * 64},
        health_failure_callback=fail_durable_revocation,
        failure_mailbox=AtomicMailbox(revocation_path),
    )
    started = time.monotonic()
    publisher.fail_from_bridge(BridgeError("child exited"))
    assert time.monotonic() - started <= 0.25
    deadline = time.monotonic() + 1.0
    evidence = None
    while time.monotonic() < deadline:
        evidence = AtomicMailbox(revocation_path).read_latest()
        if evidence is not None and evidence.payload["durable_revocation"] == "failed":
            break
        time.sleep(0.005)
    assert evidence is not None
    assert evidence.payload["durable_revocation"] == "failed"
    assert evidence.payload["durable_revocation_error"].startswith(
        "OperationalError:database is locked"
    )
    with pytest.raises(HeartbeatError):
        publisher.check()
    with pytest.raises(HeartbeatError, match="durable_revocation=OperationalError"):
        publisher.stop()


def test_stale_revocation_does_not_permanently_block_and_new_launch_clears_it(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "bridge-runtime"
    runtime.mkdir()
    nonce = "b" * 64
    revocation_path = (runtime / "bridge_revocation.json").resolve()
    AtomicMailbox(revocation_path).publish(
        sequence=1,
        payload={
            "schema": "step5d.autotune.bridge-revocation/v2",
            "deployment_id": deployment().deployment_id,
            "bridge_pid": 1234,
            "bridge_launch_nonce": nonce,
            "observed_at": "2000-01-01T00:00:00+00:00",
            "reason": "BridgeError:old child exited",
            "durable_revocation": "complete",
            "durable_revocation_error": None,
        },
    )
    status = _fresh_status(
        _StatusRepository(
            datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            details={
                "deployment_id": deployment().deployment_id,
                "bridge_pid": 1234,
                "bridge_launch_nonce": nonce,
            },
        ),
        5.0,
        _live_config(runtime),
    )
    assert status["runtime_ready"] is True

    bridge = _bridge(tmp_path, REPEATING_HEALTH_CHILD)
    try:
        assert not revocation_path.exists()
    finally:
        bridge.stop()


def test_fresh_revocation_must_match_current_bridge_identity(tmp_path: Path) -> None:
    nonce = "c" * 64
    revocation_path = (tmp_path / "bridge_revocation.json").resolve()
    AtomicMailbox(revocation_path).publish(
        sequence=1,
        payload={
            "schema": "step5d.autotune.bridge-revocation/v2",
            "deployment_id": deployment().deployment_id,
            "bridge_pid": 1234,
            "bridge_launch_nonce": "d" * 64,
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "reason": "BridgeError:wrong child",
            "durable_revocation": "pending",
            "durable_revocation_error": None,
        },
    )
    status = _fresh_status(
        _StatusRepository(
            datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            details={
                "deployment_id": deployment().deployment_id,
                "bridge_pid": 1234,
                "bridge_launch_nonce": nonce,
            },
        ),
        5.0,
        _live_config(tmp_path),
    )
    assert status["runtime_ready"] is False
    assert status["primary_blocker"] == "bridge_revocation_evidence_invalid"


def test_bridge_stop_terminates_child_even_when_watcher_join_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _bridge(tmp_path, REPEATING_HEALTH_CHILD)
    process = bridge.process
    assert process is not None

    def fail_watcher_stop() -> None:
        raise BridgeError("watcher thread did not stop")

    monkeypatch.setattr(bridge, "stop_watcher", fail_watcher_stop)
    try:
        with pytest.raises(BridgeError, match="after child cleanup"):
            bridge.stop(timeout_s=0.5)
        assert process.poll() is not None
        assert bridge.process is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.0)


def test_watcher_probe_does_not_consume_heartbeat_progress_baseline(
    tmp_path: Path,
) -> None:
    bridge = _bridge(tmp_path, STATIC_HEALTH_CHILD)
    try:
        path = bridge.runtime_root / "bridge_health.json"
        payload = json.loads(path.read_text(encoding="ascii"))
        payload["health_sequence"] += 1
        payload["sample_counter"] += 1
        temporary = path.with_name(".bridge_health.progress.tmp")
        temporary.write_text(json.dumps(payload), encoding="ascii")
        temporary.replace(path)

        watcher_view = bridge.check_health(
            max_age_s=2.0, require_progress=False
        )
        heartbeat_view = bridge.check_health(
            max_age_s=2.0, require_progress=True
        )
        assert heartbeat_view.health_sequence == watcher_view.health_sequence
        assert heartbeat_view.sample_counter == watcher_view.sample_counter
    finally:
        bridge.stop()


@pytest.mark.parametrize(
    ("mutation", "match", "require_progress"),
    (
        ("pid", "exact child", False),
        ("nonce", "exact child", False),
        ("rate", "exact child", False),
        ("stale", "stale", False),
        ("counter", "did not progress", True),
        ("transport", "transport identity", False),
    ),
)
def test_health_sidecar_rejects_identity_freshness_and_progress_drift(
    tmp_path: Path, mutation: str, match: str, require_progress: bool
) -> None:
    bridge = _bridge(tmp_path, STATIC_HEALTH_CHILD)
    try:
        path = bridge.runtime_root / "bridge_health.json"
        payload = json.loads(path.read_text(encoding="ascii"))
        if mutation == "pid":
            payload["pid"] += 1
        elif mutation == "nonce":
            payload["launch_nonce"] = "0" * 64
        elif mutation == "rate":
            payload["configured_rate_hz"] = 499
        elif mutation == "stale":
            payload["observed_at"] = "2000-01-01T00:00:00+00:00"
        elif mutation == "transport":
            payload["rtde_healthy"] = False
        temporary = path.with_name(".bridge_health.test.tmp")
        temporary.write_text(json.dumps(payload), encoding="ascii")
        temporary.replace(path)
        with pytest.raises(BridgeError, match=match):
            bridge.check_health(
                max_age_s=2.0, require_progress=require_progress
            )
    finally:
        bridge.stop()
