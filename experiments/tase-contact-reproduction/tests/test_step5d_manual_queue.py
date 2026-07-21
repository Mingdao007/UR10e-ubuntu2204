from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_manual_queue import (  # noqa: E402
    CLOSED_COMPLETE,
    ManualQueueError,
    close,
    enqueue,
    load_queue,
    next_request,
)
from step5d_manual_runtime import (  # noqa: E402
    ManualRuntimeError,
    confirm_home_complete,
    issue_prepared_intent,
    load_state,
    prepare_next_intent,
)


PROFILE = ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"
RELEASE_SHA = "a" * 64


def _enqueue(path: Path, *, nonce: str, force_i: float = 1e-4) -> dict:
    return enqueue(
        path,
        campaign_id="manual-campaign-1",
        release_manifest_sha256=RELEASE_SHA,
        launch_profile_path=PROFILE,
        force_p=0.001,
        force_i=force_i,
        force_damping=7.0,
        orientation_ko=0.4,
        occurrence_nonce=nonce,
    )


def test_i1e4_enqueue_is_one_row_and_subsecond(tmp_path: Path) -> None:
    path = tmp_path / "control/manual_queue.json"
    started = time.monotonic()
    request = _enqueue(path, nonce="1" * 32)
    elapsed = time.monotonic() - started
    payload = load_queue(path)
    assert elapsed < 1.0
    assert payload["revision"] == 1
    assert request["row_index"] == 1
    assert request["logical_batch_sequence"] == 1
    assert request["overlay"]["force_p_gain"] == 0.001
    assert request["overlay"]["force_i_gain"] == 0.0001
    assert request["overlay"]["force_damping"] == 7.0
    assert request["overlay"]["orientation_ko"] == 0.4
    assert request["overlay"]["execution_profile_id"] == "nf100-slew050-a050"


def test_exact_repeat_has_distinct_occurrence_transport_and_wire_token(tmp_path: Path) -> None:
    path = tmp_path / "manual_queue.json"
    first = _enqueue(path, nonce="1" * 32)
    second = _enqueue(path, nonce="2" * 32)
    assert first["control_candidate_uid"] == second["control_candidate_uid"]
    assert first["occurrence_uid"] != second["occurrence_uid"]
    assert first["transport_candidate_uid"] != second["transport_candidate_uid"]
    assert first["candidate_token"] != second["candidate_token"]
    assert next_request(path, completed_sequences={1}) == second


def test_queue_rejects_tamper_and_closed_append(tmp_path: Path) -> None:
    path = tmp_path / "manual_queue.json"
    _enqueue(path, nonce="1" * 32)
    payload = json.loads(path.read_text())
    payload["requests"][0]["overlay"]["force_i_gain"] = 0.002
    path.write_text(json.dumps(payload))
    with pytest.raises(ManualQueueError, match="identity|overlay"):
        load_queue(path)

    path.unlink()
    _enqueue(path, nonce="1" * 32)
    closed = close(path, reason="operator_complete")
    assert closed["lifecycle"] == CLOSED_COMPLETE
    with pytest.raises(ManualQueueError, match="closed"):
        _enqueue(path, nonce="2" * 32)


def test_symlink_queue_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "manual_queue.json"
    link.symlink_to(target)
    with pytest.raises(ManualQueueError):
        _enqueue(link, nonce="1" * 32)


def test_manual_request_creates_exact_one_row_runtime_identity(tmp_path: Path) -> None:
    queue = tmp_path / "manual_queue.json"
    request = _enqueue(queue, nonce="1" * 32)
    state = tmp_path / "manual_runtime_state.json"
    intent = prepare_next_intent(
        queue_path=queue,
        state_path=state,
        campaign_id="manual-campaign-1",
        release_manifest_sha256=RELEASE_SHA,
    )
    assert intent is not None
    document = intent.document()
    assert document["protocol"] == "v3_full_home_manual_hold_v1"
    assert document["packet"]["batch_row_index"] == 1
    assert document["packet"]["logical_batch_sequence"] == 1
    assert document["request_identity"]["occurrence_uid"] == request["occurrence_uid"]
    sent: list[dict] = []
    issue_prepared_intent(
        state_path=state,
        campaign_id="manual-campaign-1",
        release_manifest_sha256=RELEASE_SHA,
        sink=lambda payload: sent.append(dict(payload)),
    )
    assert sent == [document]

    packet = document["packet"]
    completed = confirm_home_complete(
        state_path=state,
        campaign_id="manual-campaign-1",
        release_manifest_sha256=RELEASE_SHA,
        observed={
            "campaign_epoch": packet["campaign_epoch"],
            "trial_id": packet["trial_id"],
            "state": 78,
            "candidate_token": packet["candidate_token"],
            "execution_profile_id": packet["execution_profile_id"],
            "consumed_command_seq": packet["command_seq"],
            "logical_batch_sequence": packet["logical_batch_sequence"],
            "batch_row_index": 1,
        },
    )
    assert completed["completed_sequences"] == [1]
    assert completed["inflight"] is None


def test_manual_runtime_restart_does_not_duplicate_inflight_request(tmp_path: Path) -> None:
    queue = tmp_path / "manual_queue.json"
    _enqueue(queue, nonce="1" * 32)
    state = tmp_path / "manual_runtime_state.json"
    prepare_next_intent(
        queue_path=queue,
        state_path=state,
        campaign_id="manual-campaign-1",
        release_manifest_sha256=RELEASE_SHA,
    )
    recovered = load_state(
        state, campaign_id="manual-campaign-1", release_sha=RELEASE_SHA
    )
    assert recovered["inflight"] is not None
    with pytest.raises(ManualRuntimeError, match="already inflight"):
        prepare_next_intent(
            queue_path=queue,
            state_path=state,
            campaign_id="manual-campaign-1",
            release_manifest_sha256=RELEASE_SHA,
        )
