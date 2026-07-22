"""Transport-neutral, crash-safe one-request manual-hold runtime core."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, Callable, Mapping, Protocol

from step5d_manual_queue import CLOSED_COMPLETE, PROTOCOL, load_queue, next_request
from step5d_autotune_v3.profile import canonical_json_bytes


SCHEMA = "step5d.manual-hold/runtime-state-v1"
INTENT_SCHEMA = "step5d.manual-hold/arm-intent-v1"
PROFILE_INTEGER_ID = 633


class ManualRuntimeError(RuntimeError):
    pass


class IntentSink(Protocol):
    def __call__(self, intent: Mapping[str, Any]) -> None: ...


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(payload))).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass(frozen=True)
class ManualArmIntent:
    campaign_id: str
    release_manifest_sha256: str
    campaign_epoch: int
    trial_id: int
    command_seq: int
    logical_batch_sequence: int
    candidate_token: int
    occurrence_uid: str
    transport_candidate_uid: str
    normalized_overlay_sha256: str
    overlay: Mapping[str, Any]

    def document(self) -> dict[str, Any]:
        payload = {
            "schema": INTENT_SCHEMA,
            "protocol": PROTOCOL,
            "campaign_id": self.campaign_id,
            "release_manifest_sha256": self.release_manifest_sha256,
            "packet": {
                "campaign_epoch": self.campaign_epoch,
                "trial_id": self.trial_id,
                "command": 1,
                "candidate_token": self.candidate_token,
                "execution_profile_id": PROFILE_INTEGER_ID,
                "command_seq": self.command_seq,
                "logical_batch_sequence": self.logical_batch_sequence,
                "batch_row_index": 1,
            },
            "request_identity": {
                "occurrence_uid": self.occurrence_uid,
                "transport_candidate_uid": self.transport_candidate_uid,
                "normalized_overlay_sha256": self.normalized_overlay_sha256,
            },
            "overlay": dict(self.overlay),
        }
        return {**payload, "intent_sha256": _sha256(payload)}


def _initial_state(campaign_id: str, release_sha: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "protocol": PROTOCOL,
        "campaign_id": campaign_id,
        "release_manifest_sha256": release_sha,
        "campaign_epoch": 1,
        "last_trial_id": 0,
        "last_command_seq": 0,
        "completed_sequences": [],
        "inflight": None,
    }


def load_state(path: Path, *, campaign_id: str, release_sha: str) -> dict[str, Any]:
    if not path.exists():
        return _initial_state(campaign_id, release_sha)
    if path.is_symlink() or not path.is_file():
        raise ManualRuntimeError("manual runtime state must be a real regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManualRuntimeError(f"manual runtime state is invalid JSON: {exc}") from exc
    required = {
        "schema", "protocol", "campaign_id", "release_manifest_sha256",
        "campaign_epoch", "last_trial_id", "last_command_seq",
        "completed_sequences", "inflight",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ManualRuntimeError("manual runtime state fields differ")
    if (
        payload["schema"] != SCHEMA
        or payload["protocol"] != PROTOCOL
        or payload["campaign_id"] != campaign_id
        or payload["release_manifest_sha256"] != release_sha
    ):
        raise ManualRuntimeError("manual runtime identity differs")
    completed = payload["completed_sequences"]
    if (
        not isinstance(completed, list)
        or completed != sorted(set(completed))
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in completed)
    ):
        raise ManualRuntimeError("manual completed sequence ledger differs")
    if payload["inflight"] is not None:
        intent = payload["inflight"]
        if not isinstance(intent, Mapping) or intent.get("schema") != INTENT_SCHEMA:
            raise ManualRuntimeError("manual inflight intent differs")
        expected = dict(intent)
        digest = expected.pop("intent_sha256", None)
        if digest != _sha256(expected):
            raise ManualRuntimeError("manual inflight intent digest differs")
    return payload


def seed_home_state(
    path: Path,
    *,
    campaign_id: str,
    release_sha: str,
    campaign_epoch: int,
    last_trial_id: int,
    last_command_seq: int,
) -> dict[str, Any]:
    """Seed a new manual ledger from one exact observed READY_HOME identity."""

    if path.exists() or path.is_symlink():
        raise ManualRuntimeError("manual runtime state already exists")
    if isinstance(campaign_epoch, bool) or not isinstance(campaign_epoch, int) or campaign_epoch < 1:
        raise ManualRuntimeError("campaign_epoch must be a positive integer")
    for name, value in (
        ("last_trial_id", last_trial_id),
        ("last_command_seq", last_command_seq),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ManualRuntimeError(f"{name} must be a non-negative observed integer")
    state = _initial_state(campaign_id, release_sha)
    state["campaign_epoch"] = campaign_epoch
    state["last_trial_id"] = last_trial_id
    state["last_command_seq"] = last_command_seq
    _atomic_json(path, state)
    return state


def wait_for_request(
    queue_path: Path,
    *,
    campaign_id: str,
    release_manifest_sha256: str,
    completed_sequences: set[int],
    stop_requested: Callable[[], bool] | None = None,
    safety_ready: Callable[[], bool] | None = None,
    poll_s: float = 0.05,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Wait indefinitely at Home; TP heartbeat remains the independent live guard."""

    while True:
        if stop_requested is not None and stop_requested():
            raise ManualRuntimeError("manual request wait stopped")
        if queue_path.is_file() and not queue_path.is_symlink():
            queue = load_queue(queue_path)
            if queue["campaign_id"] != campaign_id:
                raise ManualRuntimeError("manual queue campaign identity differs")
            if queue["release_manifest_sha256"] != release_manifest_sha256:
                raise ManualRuntimeError("manual queue release identity differs")
            if safety_ready is not None and not safety_ready():
                raise ManualRuntimeError("manual Home safety snapshot is not ready")
            selected = next_request(queue_path, completed_sequences=completed_sequences)
            if selected is not None or queue["lifecycle"] == CLOSED_COMPLETE:
                return selected, queue
        time.sleep(poll_s)


def prepare_next_intent(
    *, queue_path: Path, state_path: Path, campaign_id: str,
    release_manifest_sha256: str,
) -> ManualArmIntent | None:
    state = load_state(state_path, campaign_id=campaign_id, release_sha=release_manifest_sha256)
    if state["inflight"] is not None:
        raise ManualRuntimeError("one manual request is already inflight")
    request, queue = wait_for_request(
        queue_path,
        campaign_id=campaign_id,
        release_manifest_sha256=release_manifest_sha256,
        completed_sequences=set(state["completed_sequences"]),
    )
    if request is None:
        return None
    sequence = request["logical_batch_sequence"]
    if sequence != len(state["completed_sequences"]) + 1:
        raise ManualRuntimeError("manual queue is not the exact next occurrence")
    intent = ManualArmIntent(
        campaign_id=campaign_id,
        release_manifest_sha256=release_manifest_sha256,
        campaign_epoch=state["campaign_epoch"],
        trial_id=state["last_trial_id"] + 1,
        command_seq=state["last_command_seq"] + 1,
        logical_batch_sequence=sequence,
        candidate_token=request["candidate_token"],
        occurrence_uid=request["occurrence_uid"],
        transport_candidate_uid=request["transport_candidate_uid"],
        normalized_overlay_sha256=request["normalized_overlay_sha256"],
        overlay=request["overlay"],
    )
    state["inflight"] = intent.document()
    _atomic_json(state_path, state)
    return intent


def issue_prepared_intent(
    *, state_path: Path, campaign_id: str, release_manifest_sha256: str,
    sink: IntentSink,
) -> dict[str, Any]:
    state = load_state(state_path, campaign_id=campaign_id, release_sha=release_manifest_sha256)
    if state["inflight"] is None:
        raise ManualRuntimeError("no durable manual ARM intent is prepared")
    sink(state["inflight"])
    return dict(state["inflight"])


def confirm_home_complete(
    *, state_path: Path, campaign_id: str, release_manifest_sha256: str,
    observed: Mapping[str, Any],
) -> dict[str, Any]:
    """Commit one request only after exact READY_HOME_NEXT identity is observed."""

    state = load_state(state_path, campaign_id=campaign_id, release_sha=release_manifest_sha256)
    intent = state["inflight"]
    if intent is None:
        raise ManualRuntimeError("no inflight manual request can complete")
    packet = intent["packet"]
    expected = {
        "campaign_epoch": packet["campaign_epoch"],
        "trial_id": packet["trial_id"],
        "state": 78,
        "candidate_token": packet["candidate_token"],
        "execution_profile_id": packet["execution_profile_id"],
        "consumed_command_seq": packet["command_seq"],
        "logical_batch_sequence": packet["logical_batch_sequence"],
        "batch_row_index": 1,
    }
    if dict(observed) != expected:
        raise ManualRuntimeError("READY_HOME_NEXT observation differs from inflight identity")
    sequence = packet["logical_batch_sequence"]
    state["completed_sequences"] = [*state["completed_sequences"], sequence]
    state["last_trial_id"] = packet["trial_id"]
    state["last_command_seq"] = packet["command_seq"]
    state["inflight"] = None
    _atomic_json(state_path, state)
    return state


__all__ = [
    "ManualArmIntent", "ManualRuntimeError", "confirm_home_complete",
    "issue_prepared_intent", "load_state", "prepare_next_intent",
    "seed_home_state", "wait_for_request",
]
