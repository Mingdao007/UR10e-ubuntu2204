from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v2.bridge import BridgeError, BridgeProcess
from step5d_autotune_v2.heartbeat import HeartbeatPublisher
from step5d_autotune_v2.mailbox import AtomicMailbox
from step5d_autotune_v2.postprocess import analyze_capture
from step5d_autotune_v2.reducer import LifecycleEvent
from step5d_autotune_v2.runtime import EVENT_SCHEMA, JsonlBridgePort
from step5d_autotune_v2.supervisor import CampaignSupervisor, RuntimeFailure
from step5d_autotune_v2.transfer import TransferWorker, retry_delay_s
from test_step5d_autotune_v2_supervisor import FakePort, deployment, repository


def test_bridge_ready_must_bind_fresh_child_pid_and_launch_nonce(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    child = """
import json, os, time
from datetime import datetime, timezone
from pathlib import Path
root = Path(os.environ['STEP5D_AUTOTUNE_V2_RUNTIME_ROOT'])
def publish(name, payload):
  temporary = root / ('.' + name + '.tmp')
  temporary.write_text(json.dumps(payload), encoding='ascii')
  temporary.replace(root / name)
payload = {
  'schema': 'step5d.autotune.bridge-ready/v2',
  'bridge_ready': True,
  'deployment_id': os.environ['STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID'],
  'launch_nonce': os.environ['STEP5D_AUTOTUNE_V2_LAUNCH_NONCE'],
  'pid': os.getpid(),
  'sample_rate_hz': 500,
  'startup_home_verified': True,
  'command_transport_ready': True,
  'event_transport_ready': True,
}
publish('bridge_ready.json', payload)
sequence = 0
while True:
  sequence += 1
  publish('bridge_health.json', {
    'schema': 'step5d.autotune.bridge-health/v2',
    'deployment_id': os.environ['STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID'],
    'pid': os.getpid(),
    'launch_nonce': os.environ['STEP5D_AUTOTUNE_V2_LAUNCH_NONCE'],
    'health_sequence': sequence,
    'observed_at': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
    'configured_rate_hz': 500,
    'sample_counter': sequence * 10,
    'rtde_healthy': True,
    'command_transport_healthy': True,
    'event_transport_healthy': True,
  })
  time.sleep(0.02)
"""
    bridge = BridgeProcess(
        argv=(sys.executable, "-c", child),
        root=ROOT,
        runtime_root=runtime,
        deployment_id="test-deployment",
    )
    try:
        ready = bridge.start(timeout_s=2)
        assert ready.pid == bridge.process.pid
        assert len(ready.launch_nonce) == 64
        assert json.loads(ready.evidence_path.read_text())["launch_nonce"] == ready.launch_nonce
    finally:
        bridge.stop()


def test_stale_bridge_ready_file_can_never_publish_ready(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "bridge_ready.json").write_text(
        json.dumps(
            {
                "bridge_ready": True,
                "deployment_id": "test-deployment",
                "sample_rate_hz": 500,
            }
        ),
        encoding="utf-8",
    )
    bridge = BridgeProcess(
        argv=(sys.executable, "-c", "import time; time.sleep(10)"),
        root=ROOT,
        runtime_root=runtime,
        deployment_id="test-deployment",
    )
    try:
        with pytest.raises(BridgeError, match="did not publish"):
            bridge.start(timeout_s=0.15)
    finally:
        bridge.stop()


def test_heartbeat_keeps_runtime_status_fresh_and_writer_owned(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    token = "writer-token"
    repo.claim_writer(token=token)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    mailbox = AtomicMailbox((runtime / "host_heartbeat.json").resolve())
    publisher = HeartbeatPublisher(
        repository=repo,
        mailbox=mailbox,
        writer_token=token,
        deployment_id=deployment().deployment_id,
        ready_details={"bridge_ready": True},
        interval_s=0.05,
    )
    try:
        publisher.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            snapshot = mailbox.read_latest()
            if snapshot is not None and snapshot.sequence >= 2:
                break
            time.sleep(0.01)
        publisher.check()
        assert snapshot is not None and snapshot.payload["command"] == "HOST_HEARTBEAT"
        assert repo.status()["runtime_ready"] is True
    finally:
        publisher.stop()
        repo.release_writer(token)


def test_bridge_event_must_echo_persisted_command_sequence_and_checksum(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    candidate = repo.next_pending_candidate()
    assert candidate is not None
    trial_id = repo.create_trial(
        candidate_id=candidate["candidate_id"], deployment_id=deployment().deployment_id
    )
    repo.apply_lifecycle_event(trial_id, LifecycleEvent.PERSIST_ARM)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    mailbox = AtomicMailbox((runtime / "command.json").resolve())
    events = runtime / "bridge_events.jsonl"
    port = JsonlBridgePort(
        repository=repo,
        mailbox=mailbox,
        event_path=events,
        allowed_artifact_root=runtime,
        deployment_id=deployment().deployment_id,
        analyzer_argv=(),
        command_root=ROOT,
        transfer_destination=None,
        timeout_s=0.1,
    )
    arm = port.publish_arm(
        trial_id=trial_id,
        sequence=repo.trial_detail(trial_id)["arm_sequence"],
        candidate=repo.candidate(candidate["candidate_id"]),
    )
    repo.apply_lifecycle_event(trial_id, LifecycleEvent.PUBLISH_COMMAND, arm)
    event = {
        "schema": EVENT_SCHEMA,
        "deployment_id": deployment().deployment_id,
        "trial_id": trial_id,
        "event": "tp_consumed",
        "command_sequence": arm["sequence"] + 1,
        "command_checksum": arm["mailbox_checksum"],
    }
    events.write_text(json.dumps(event) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeFailure, match="persisted command"):
        port.wait_tp_consumed(trial_id=trial_id)
    event["command_sequence"] = arm["sequence"]
    with events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    assert port.wait_tp_consumed(trial_id=trial_id)["event"] == "tp_consumed"


def test_raw_capture_is_copied_to_content_addressed_read_only_seal(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    candidate = repo.next_pending_candidate()
    assert candidate is not None
    trial_id = repo.create_trial(
        candidate_id=candidate["candidate_id"],
        deployment_id=deployment().deployment_id,
    )
    repo.apply_lifecycle_event(trial_id, LifecycleEvent.PERSIST_ARM)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    mailbox = AtomicMailbox((runtime / "command.json").resolve())
    events = runtime / "bridge_events.jsonl"
    port = JsonlBridgePort(
        repository=repo,
        mailbox=mailbox,
        event_path=events,
        allowed_artifact_root=runtime,
        deployment_id=deployment().deployment_id,
        analyzer_argv=(),
        command_root=ROOT,
        transfer_destination=None,
        timeout_s=0.1,
    )
    candidate_row = repo.candidate(candidate["candidate_id"])
    arm = port.publish_arm(
        trial_id=trial_id,
        sequence=repo.trial_detail(trial_id)["arm_sequence"],
        candidate=candidate_row,
    )
    repo.apply_lifecycle_event(trial_id, LifecycleEvent.PUBLISH_COMMAND, arm)
    capture = runtime / "capture.json"
    original = b'{"immutable":"source snapshot"}\n'
    capture.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    events.write_text(
        json.dumps(
            {
                "schema": EVENT_SCHEMA,
                "deployment_id": deployment().deployment_id,
                "trial_id": trial_id,
                "event": "raw_capture_sealed",
                "command_sequence": arm["sequence"],
                "command_checksum": arm["mailbox_checksum"],
                "path": str(capture.resolve()),
                "sha256": digest,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sealed = port.seal_raw(trial_id=trial_id)
    assert sealed.path == runtime / "immutable_raw" / "sha256" / digest
    assert sealed.path.read_bytes() == original
    assert sealed.path.stat().st_mode & 0o777 == 0o400
    capture.write_text("mutated after bridge seal", encoding="utf-8")
    assert sealed.path.read_bytes() == original


def test_transfer_failure_stays_in_retry_queue_and_trial_remains_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path)
    outcome = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]
    artifact = repo.artifact_for_trial(outcome.trial_id, role="raw_capture_seal")
    assert artifact is not None
    repo.queue_transfer(
        artifact_id=artifact["artifact_id"],
        destination="andyl@127.0.0.1:/Users/andyl/Downloads/step5d_autotune/",
    )

    def fail_transfer(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=0.1)

    monkeypatch.setattr(subprocess, "run", fail_transfer)
    monkeypatch.setattr(
        "step5d_autotune_v2.transfer.retry_delay_s", lambda attempts: 30.0
    )
    worker = TransferWorker(repo, timeout_s=1)
    worker.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        pending = repo.pending_transfers()
        if pending and pending[0]["attempts"] == 1:
            break
        time.sleep(0.01)
    worker.stop()
    pending = repo.pending_transfers()
    assert pending[0]["status"] == "retry_pending"
    assert pending[0]["attempts"] == 1
    assert repo.trial_detail(outcome.trial_id)["state"] == "complete"


@pytest.mark.parametrize(
    ("attempts", "expected_s"),
    [(0, 0.0), (1, 0.25), (2, 1.0), (3, 4.0), (4, 16.0), (5, 30.0), (99, 30.0)],
)
def test_transfer_retry_backoff_uses_durable_attempt_count(
    attempts: int, expected_s: float
) -> None:
    assert retry_delay_s(attempts) == expected_s


def test_transfer_retries_in_same_worker_and_completion_stays_post_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path)
    outcome = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]
    artifact = repo.artifact_for_trial(outcome.trial_id, role="raw_capture_seal")
    assert artifact is not None
    transfer_id = repo.queue_transfer(
        artifact_id=artifact["artifact_id"],
        destination="andyl@127.0.0.1:/Users/andyl/Downloads/step5d_autotune/",
    )
    trial_before = repo.trial_detail(outcome.trial_id)
    assert trial_before["ack_sequence"] is not None
    calls: list[list[str]] = []
    call_times: list[float] = []

    def fail_once_then_succeed(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess:
        calls.append(args)
        call_times.append(time.monotonic())
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=args, timeout=0.1)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fail_once_then_succeed)
    worker = TransferWorker(repo, timeout_s=1)
    worker.start()
    try:
        deadline = time.monotonic() + 2.0
        row = None
        while time.monotonic() < deadline:
            with repo._connect() as connection:
                fetched = connection.execute(
                    "SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)
                ).fetchone()
            row = dict(fetched) if fetched is not None else None
            if row is not None and row["status"] == "complete":
                break
            time.sleep(0.01)
    finally:
        worker.stop()

    assert row is not None
    assert row["status"] == "complete"
    assert row["attempts"] == 2
    assert len(calls) == 3
    assert call_times[1] - call_times[0] >= 0.20
    trial_after = repo.trial_detail(outcome.trial_id)
    assert trial_after["state"] == "complete"
    assert trial_after["ack_sequence"] == trial_before["ack_sequence"]


def test_png_failure_is_a_warning_not_a_metrics_failure(tmp_path: Path) -> None:
    capture = tmp_path / "immutable_trial_bundle.json"
    capture.write_text(
        json.dumps(
            {
                "evaluation": {
                    "complete_bins": 550,
                    "safe_closure": True,
                    "eligible": False,
                    "metrics": {
                        "governor": {
                            "nontrainable_profile_diagnostic": {"force_mae_n": 0.55}
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    result = analyze_capture(
        capture=capture,
        trial_id="trial-1",
        output_dir=tmp_path / "plots",
    )
    assert result["metrics"]["force_mae_n"] == 0.55
    assert result["diagnostic_eligible"] is True
    assert result["artifacts"] == []
    assert result["warnings"][0].startswith("png:")
