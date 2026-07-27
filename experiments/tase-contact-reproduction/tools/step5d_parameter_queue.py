#!/usr/bin/env python3
"""Durable, unbounded Step5d parameter receiver with one inflight dispatch."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
from typing import Any, Iterator, Mapping, Sequence

from ur10e_experiment_runtime.candidate_identity import (
    ControlCandidateUid,
    OccurrenceUid,
    ParameterUid,
    TransportCandidateUid,
)
from step5d_autotune_v3.profile import canonical_json_bytes
from step5d_autotune_v3.runtime_profile import (
    DEFAULT_OVERLAY,
    load_launch_profile,
    normalize_trial_overlay,
    normalized_overlay_sha256,
)


STATE_SCHEMA = "step5d.parameter-receiver/state-v2"
LEGACY_STATE_SCHEMA = "step5d.parameter-receiver/state-v1"
REQUEST_SCHEMA = "step5d.parameter-receiver/request-v1"
DISPATCH_SCHEMA = "step5d.parameter-receiver/dispatch-v1"
RECEIPT_SCHEMA = "step5d.parameter-receiver/receipt-v3"
LEGACY_RECEIPT_SCHEMA = "step5d.parameter-receiver/receipt-v2"
RECONCILIATION_SCHEMA = "step5d.parameter-receiver/reconciliation-v1"
MIGRATION_SCHEMA = "step5d.parameter-receiver/migration-v1"
NEXT_ARM_SCHEMA = "step5d.parameter-receiver/governance-next-arm-v1"
TERMINAL_RECEIPT_SCHEMA = "step5d.parameter-receiver/governance-terminal-receipt-v1"
PROTOCOL = "v3_full_home_parameter_receiver_v1"
PROFILE_INTEGER_ID = 633
MAX_JSON_BYTES = 16 * 1024
POSITIONS = frozenset({"tail", "next"})
RECEIPT_STATUSES = frozenset({"SUCCEEDED", "FAILED"})
LEGACY_RECEIPT_STATUSES = frozenset({"COMPLETE", "DATA_ISSUE"})


class ParameterQueueError(RuntimeError):
    """The parameter receiver is unsafe, inconsistent, or malformed."""


def _sha256_bytes(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path, role: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ParameterQueueError(f"{role} must be a real regular file")
    return _sha256_bytes(path.read_bytes())


def _load_profile(path: Path):
    payload = _strict_json(path, "V3 launch profile")
    program = payload.get("tp_program_id")
    if not isinstance(program, str) or not program:
        raise ParameterQueueError("V3 launch profile lacks tp_program_id")
    return load_launch_profile(path, expected_tp_program_id=program)


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(dict(payload))


def _strict_json(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ParameterQueueError(f"{role} must be a real regular file")
    encoded = path.read_bytes()
    if len(encoded) > MAX_JSON_BYTES:
        raise ParameterQueueError(f"{role} exceeds {MAX_JSON_BYTES} bytes")

    def unique(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ParameterQueueError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ParameterQueueError(f"{role} contains non-finite {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ParameterQueueError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParameterQueueError(f"{role} must be a JSON object")
    return payload


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ParameterQueueError(f"{path.name} exceeds {MAX_JSON_BYTES} bytes")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
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


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ParameterQueueError(f"{path.name} exceeds {MAX_JSON_BYTES} bytes")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != encoded:
            raise ParameterQueueError(f"immutable record differs: {path.name}")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextlib.contextmanager
def _lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ParameterQueueError("queue root must be a real directory")
    descriptor = os.open(
        root / ".queue.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ParameterQueueError("queue lock must be a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _state_path(root: Path) -> Path:
    return root / "state.json"


def _migration_path(root: Path) -> Path:
    return root / "migration.json"


def _request_path(root: Path, sequence: int) -> Path:
    return root / "requests" / f"{sequence:012d}.json"


def _dispatch_path(root: Path, dispatch_sequence: int) -> Path:
    return root / "dispatches" / f"{dispatch_sequence:012d}.json"


def _receipt_path(root: Path, request_uid: str) -> Path:
    return root / "receipts" / f"{request_uid.rsplit(':', 1)[-1]}.json"


def _reconciliation_path(root: Path, dispatch_sequence: int) -> Path:
    return root / "reconciliations" / f"{dispatch_sequence:012d}.json"


def _governance_root(root: Path) -> Path:
    return root / "governance"


def _next_arm_path(root: Path) -> Path:
    return _governance_root(root) / "next_arm.json"


def _terminal_receipts_root(root: Path) -> Path:
    return _governance_root(root) / "terminal_receipts"


def _terminal_receipt_path(root: Path, dispatch_identity: str) -> Path:
    return _terminal_receipts_root(root) / f"{_sha256_bytes(dispatch_identity.encode('utf-8'))}.json"


def _request_equivalent(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        left.get(key) == right.get(key)
        for key in (
            "schema",
            "request_uid",
            "occurrence_nonce",
            "control_candidate_uid",
            "normalized_overlay_sha256",
            "overlay",
            "source",
            "position",
        )
    )


def _next_arm_document(
    *,
    dispatch_identity: str,
    dispatch_sequence: int,
    campaign_fingerprint: str,
    mailbox_packet_sha256: str,
    observed_at: int,
) -> dict[str, Any]:
    if (
        not isinstance(dispatch_identity, str)
        or not dispatch_identity
        or "\n" in dispatch_identity
        or not dispatch_identity.startswith("dispatch:v1:")
    ):
        raise ParameterQueueError("dispatch_identity must be dispatch:v1:<digest>")
    if isinstance(dispatch_sequence, bool) or not isinstance(dispatch_sequence, int):
        raise ParameterQueueError("dispatch_sequence must be a positive integer")
    if dispatch_sequence <= 0:
        raise ParameterQueueError("dispatch_sequence must be positive")
    if not _is_sha256(campaign_fingerprint):
        raise ParameterQueueError("campaign_fingerprint must be a SHA-256 hex digest")
    if not _is_sha256(mailbox_packet_sha256):
        raise ParameterQueueError("mailbox_packet_sha256 must be a SHA-256 hex digest")
    if isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at < 0:
        raise ParameterQueueError("observed_at must be a non-negative integer")
    return {
        "schema": NEXT_ARM_SCHEMA,
        "dispatch_identity": dispatch_identity,
        "dispatch_sequence": dispatch_sequence,
        "campaign_fingerprint": campaign_fingerprint,
        "mailbox_packet_sha256": mailbox_packet_sha256,
        "observed_at": observed_at,
    }


def _validate_next_arm(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "dispatch_identity",
        "dispatch_sequence",
        "campaign_fingerprint",
        "mailbox_packet_sha256",
        "observed_at",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ParameterQueueError("next-arm fields differ")
    if payload["schema"] != NEXT_ARM_SCHEMA:
        raise ParameterQueueError("next-arm schema mismatch")
    if (
        not isinstance(payload["dispatch_identity"], str)
        or not payload["dispatch_identity"]
        or "\n" in payload["dispatch_identity"]
        or not payload["dispatch_identity"].startswith("dispatch:v1:")
    ):
        raise ParameterQueueError("next-arm dispatch_identity is invalid")
    if (
        isinstance(payload["dispatch_sequence"], bool)
        or not isinstance(payload["dispatch_sequence"], int)
        or payload["dispatch_sequence"] <= 0
    ):
        raise ParameterQueueError("next-arm dispatch_sequence is invalid")
    if not _is_sha256(payload["campaign_fingerprint"]):
        raise ParameterQueueError("next-arm campaign_fingerprint is not SHA-256")
    if not _is_sha256(payload["mailbox_packet_sha256"]):
        raise ParameterQueueError("next-arm mailbox_packet_sha256 is not SHA-256")
    if (
        isinstance(payload["observed_at"], bool)
        or not isinstance(payload["observed_at"], int)
        or payload["observed_at"] < 0
    ):
        raise ParameterQueueError("next-arm observed_at is invalid")
    return dict(payload)


def _load_next_arm(root: Path) -> dict[str, Any] | None:
    path = _next_arm_path(root)
    if not path.exists():
        if path.is_symlink():
            raise ParameterQueueError("next-arm must be a real regular file")
        return None
    return _validate_next_arm(_strict_json(path, "governance next-arm"))


def _terminal_receipt_document(
    *,
    process_composition_sha256: str,
    dispatch_identity: str,
    dispatch_sequence: int,
    terminal_state: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "process_composition_sha256": process_composition_sha256,
        "dispatch_identity": dispatch_identity,
        "dispatch_sequence": dispatch_sequence,
        "terminal_state": dict(terminal_state),
    }
    payload["schema"] = TERMINAL_RECEIPT_SCHEMA
    payload["terminal_state_sha256"] = _sha256_bytes(_canonical(payload["terminal_state"]))
    return payload


def _record_terminal_receipt_locked(
    root: Path,
    existing: tuple[dict[str, Any], ...],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    dispatch_identity = str(record["dispatch_identity"])
    requested_record = dict(record)
    path = _terminal_receipt_path(root, dispatch_identity)
    for item in existing:
        if item["dispatch_identity"] == dispatch_identity:
            if item == requested_record:
                return item
            raise ParameterQueueError("terminal receipt dispatch_identity already exists")
    highest = 0
    for item in existing:
        if item["process_composition_sha256"] != record["process_composition_sha256"]:
            continue
        if int(item["dispatch_sequence"]) >= int(record["dispatch_sequence"]):
            raise ParameterQueueError("terminal receipt sequence is not monotonic")
        highest = max(highest, int(item["dispatch_sequence"]))
    if highest and int(record["dispatch_sequence"]) <= highest:
        raise ParameterQueueError("terminal receipt sequence is not monotonic")
    _atomic_json(path, requested_record)
    return requested_record


def _validate_terminal_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "process_composition_sha256",
        "dispatch_identity",
        "dispatch_sequence",
        "terminal_state",
        "terminal_state_sha256",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ParameterQueueError("terminal receipt fields differ")
    if payload["schema"] != TERMINAL_RECEIPT_SCHEMA:
        raise ParameterQueueError("terminal receipt schema mismatch")
    if (
        not isinstance(payload["dispatch_identity"], str)
        or not payload["dispatch_identity"]
        or "\n" in payload["dispatch_identity"]
    ):
        raise ParameterQueueError("terminal receipt dispatch_identity is invalid")
    if (
        isinstance(payload["dispatch_sequence"], bool)
        or not isinstance(payload["dispatch_sequence"], int)
        or payload["dispatch_sequence"] <= 0
    ):
        raise ParameterQueueError("terminal receipt dispatch_sequence is invalid")
    if not _is_sha256(payload["process_composition_sha256"]):
        raise ParameterQueueError("terminal receipt process_composition_sha256 is not SHA-256")
    if not isinstance(payload["terminal_state"], Mapping):
        raise ParameterQueueError("terminal receipt terminal_state must be an object")
    if (
        not isinstance(payload["terminal_state_sha256"], str)
        or len(payload["terminal_state_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in payload["terminal_state_sha256"]
        )
    ):
        raise ParameterQueueError("terminal receipt terminal_state_sha256 is invalid")
    if (
        _sha256_bytes(_canonical(dict(payload["terminal_state"])))
        != payload["terminal_state_sha256"]
    ):
        raise ParameterQueueError("terminal receipt terminal_state_sha256 is inconsistent")
    return dict(payload)


def _load_terminal_receipts(root: Path) -> tuple[dict[str, Any], ...]:
    root = _terminal_receipts_root(root)
    if not root.exists():
        return ()
    if root.is_file() or root.is_symlink():
        raise ParameterQueueError("terminal receipts must be a directory")
    rows = []
    for path in sorted(root.iterdir(), key=lambda node: node.name):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix != ".json":
            continue
        rows.append(_validate_terminal_receipt(_strict_json(path, "governance terminal receipt")))
    return tuple(rows)


def publish_next_arm(
    root: Path,
    *,
    dispatch_identity: str,
    dispatch_sequence: int,
    campaign_fingerprint: str,
    mailbox_packet_sha256: str,
    observed_at: int,
) -> dict[str, Any]:
    with _lock(root):
        record = _next_arm_document(
            dispatch_identity=dispatch_identity,
            dispatch_sequence=dispatch_sequence,
            campaign_fingerprint=campaign_fingerprint,
            mailbox_packet_sha256=mailbox_packet_sha256,
            observed_at=observed_at,
        )
        path = _next_arm_path(root)
        existing = _load_next_arm(root)
        if existing is not None:
            if int(record["dispatch_sequence"]) == int(existing["dispatch_sequence"]):
                if (
                    existing["dispatch_identity"] == record["dispatch_identity"]
                    and existing["mailbox_packet_sha256"]
                    == record["mailbox_packet_sha256"]
                ):
                    return existing
                raise ParameterQueueError(
                    "next-arm publication conflicts with prior publish"
                )
            if int(record["dispatch_sequence"]) < int(existing["dispatch_sequence"]):
                raise ParameterQueueError("next-arm dispatch_sequence must increase")
        _atomic_json(path, record)
        return record


def read_next_arm(root: Path) -> dict[str, Any] | None:
    with _lock(root):
        return _load_next_arm(root)


def record_terminal_receipt(
    root: Path,
    *,
    process_composition_sha256: str,
    dispatch_identity: str,
    dispatch_sequence: int,
    terminal_state: Mapping[str, Any],
) -> dict[str, Any]:
    if not _is_sha256(process_composition_sha256):
        raise ParameterQueueError("process_composition_sha256 must be SHA-256")
    if not isinstance(terminal_state, Mapping):
        raise ParameterQueueError("terminal_state must be an object")
    if isinstance(dispatch_sequence, bool) or not isinstance(dispatch_sequence, int):
        raise ParameterQueueError("dispatch_sequence must be an integer")
    if dispatch_sequence <= 0:
        raise ParameterQueueError("dispatch_sequence must be positive")
    record = _terminal_receipt_document(
        process_composition_sha256=process_composition_sha256,
        dispatch_identity=dispatch_identity,
        dispatch_sequence=dispatch_sequence,
        terminal_state=terminal_state,
    )
    with _lock(root):
        existing = _load_terminal_receipts(root)
        return _record_terminal_receipt_locked(
            root=root,
            existing=existing,
            record=record,
        )


def _initial_state(
    *,
    campaign_id: str,
    release_manifest_sha256: str | None = None,
    launch_profile_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "protocol": PROTOCOL,
        "campaign_id": campaign_id,
        "revision": 0,
        "dispatch_sequence": 0,
        "home_identity": None,
        "inflight": None,
    }


def _validate_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    common = {
        "schema",
        "protocol",
        "campaign_id",
        "revision",
        "dispatch_sequence",
        "home_identity",
        "inflight",
    }
    if not isinstance(payload, Mapping) or not common.issubset(payload):
        raise ParameterQueueError("queue state fields differ")
    schema = payload["schema"]
    if schema == LEGACY_STATE_SCHEMA:
        required = common | {"release_manifest_sha256", "launch_profile_sha256"}
    elif schema == STATE_SCHEMA:
        required = common
    else:
        raise ParameterQueueError("queue state schema or protocol differs")
    if set(payload) != required or payload["protocol"] != PROTOCOL:
        raise ParameterQueueError("queue state schema or protocol differs")
    for key in ("campaign_id",):
        if not isinstance(payload[key], str) or not payload[key]:
            raise ParameterQueueError(f"queue {key} is invalid")
    if schema == LEGACY_STATE_SCHEMA:
        for key in ("release_manifest_sha256", "launch_profile_sha256"):
            value = payload[key]
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ParameterQueueError(f"queue {key} is not SHA-256")
    for key in ("revision", "dispatch_sequence"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ParameterQueueError(f"queue {key} is invalid")
    home = payload["home_identity"]
    if home is not None:
        if not isinstance(home, Mapping) or set(home) != {
            "campaign_epoch",
            "last_trial_id",
            "last_command_seq",
        }:
            raise ParameterQueueError("queue Home identity fields differ")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in home.values()
        ):
            raise ParameterQueueError("queue Home identity is invalid")
    inflight = payload["inflight"]
    if inflight is not None:
        if not isinstance(inflight, Mapping) or set(inflight) != {
            "dispatch_sequence",
            "request_uid",
        }:
            raise ParameterQueueError("queue inflight fields differ")
    return dict(payload)


def initialize(
    root: Path,
    *,
    campaign_id: str,
    release_manifest_sha256: str | None = None,
    launch_profile_path: Path | None = None,
) -> dict[str, Any]:
    launch_sha = (
        None
        if launch_profile_path is None
        else _sha256_file(launch_profile_path, "launch profile")
    )
    with _lock(root):
        path = _state_path(root)
        if path.exists() or path.is_symlink():
            state = _validate_state(_strict_json(path, "queue state"))
            if state["campaign_id"] != campaign_id:
                raise ParameterQueueError("queue immutable identity changed")
            if state["schema"] == LEGACY_STATE_SCHEMA and (
                release_manifest_sha256 is None
                or launch_sha is None
                or state["release_manifest_sha256"] != release_manifest_sha256
                or state["launch_profile_sha256"] != launch_sha
            ):
                raise ParameterQueueError("legacy queue immutable identity changed")
            return state
        state = _initial_state(
            campaign_id=campaign_id,
            release_manifest_sha256=release_manifest_sha256,
            launch_profile_sha256=launch_sha,
        )
        _atomic_json(path, state)
        return state


def load_state(root: Path) -> dict[str, Any]:
    return _validate_state(_strict_json(_state_path(root), "queue state"))


def adopt_selected_legacy_binding(
    root: Path,
    *,
    legacy_root: Path,
    campaign_id: str,
) -> dict[str, Any]:
    """Adopt one explicitly named legacy binding exactly once.

    The old tree is copied byte-for-byte and never rewritten.  No release
    directory enumeration or best-effort merge is allowed.
    """

    root = root.resolve()
    legacy_root = legacy_root.resolve()
    if root == legacy_root:
        raise ParameterQueueError("stable receiver root must differ from legacy root")
    if not legacy_root.exists() or legacy_root.is_symlink() or not legacy_root.is_dir():
        raise ParameterQueueError("selected legacy receiver binding is missing or unsafe")
    marker = _migration_path(root)
    if marker.is_file() or marker.is_symlink():
        migration = _strict_json(marker, "receiver migration")
        expected = {
            "schema": MIGRATION_SCHEMA,
            "legacy_root": str(legacy_root),
            "campaign_id": campaign_id,
        }
        if migration != expected:
            raise ParameterQueueError("receiver migration binding differs")
        return load_state(root)
    if _state_path(root).exists() or _state_path(root).is_symlink():
        state = load_state(root)
        raise ParameterQueueError("stable receiver root already exists without migration marker")
    if legacy_root.exists() or legacy_root.is_symlink():
        legacy_state = _validate_state(
            _strict_json(legacy_root / "state.json", "legacy queue state")
        )
        if legacy_state["schema"] != LEGACY_STATE_SCHEMA:
            raise ParameterQueueError("selected legacy binding is not v1")
        if legacy_state["campaign_id"] != campaign_id:
            raise ParameterQueueError("legacy receiver campaign identity differs")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for dirname in (
            "requests",
            "dispatches",
            "receipts",
            "reconciliations",
            "physical_attempts",
        ):
            source_dir = legacy_root / dirname
            if not source_dir.exists():
                continue
            if source_dir.is_symlink() or not source_dir.is_dir():
                raise ParameterQueueError(f"legacy {dirname} tree is unsafe")
            for source in sorted(source_dir.iterdir(), key=lambda path: path.name):
                if source.is_symlink() or not source.is_file():
                    raise ParameterQueueError(f"legacy {dirname} entry is unsafe")
                target = root / dirname / source.name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if target.exists() or target.is_symlink():
                    if target.is_symlink() or target.read_bytes() != source.read_bytes():
                        raise ParameterQueueError("legacy migration target differs")
                else:
                    shutil.copyfile(source, target)
        migrated = _initial_state(campaign_id=campaign_id)
        migrated["revision"] = legacy_state["revision"]
        migrated["dispatch_sequence"] = legacy_state["dispatch_sequence"]
        migrated["home_identity"] = legacy_state["home_identity"]
        migrated["inflight"] = legacy_state["inflight"]
        _atomic_json(_state_path(root), migrated)
        _write_once(marker, {
            "schema": MIGRATION_SCHEMA,
            "legacy_root": str(legacy_root),
            "campaign_id": campaign_id,
        })
        return migrated
    raise ParameterQueueError("selected legacy receiver binding is missing")


def _request_document(
    *,
    enqueue_sequence: int,
    launch_profile_path: Path,
    force_p: float,
    force_i: float,
    force_damping: float,
    normal_filter_tau_s: float,
    orientation_ko: float,
    source: str,
    position: str,
    occurrence_nonce: str,
) -> dict[str, Any]:
    if position not in POSITIONS:
        raise ParameterQueueError("position must be tail or next")
    if not source or "\n" in source:
        raise ParameterQueueError("request source must be one non-empty line")
    if (
        len(occurrence_nonce) != 32
        or any(character not in "0123456789abcdef" for character in occurrence_nonce)
    ):
        raise ParameterQueueError("occurrence nonce must be 16 lowercase hex bytes")
    profile = _load_profile(launch_profile_path)
    overlay_input = {
        **DEFAULT_OVERLAY,
        "force_p_gain": force_p,
        "force_i_gain": force_i,
        "force_damping": force_damping,
        "normal_filter_tau_s": normal_filter_tau_s,
        "orientation_ko": orientation_ko,
    }
    overlay_input.pop("control_candidate_uid", None)
    overlay = normalize_trial_overlay(overlay_input, profile=profile)
    overlay_sha = normalized_overlay_sha256(profile, overlay)
    identity = {
        "protocol": PROTOCOL,
        "occurrence_nonce": occurrence_nonce,
        "control_candidate_uid": overlay["control_candidate_uid"],
        "normalized_overlay_sha256": overlay_sha,
        "source": source,
        "position": position,
    }
    request_uid = f"request:v1:{_sha256_bytes(_canonical(identity))}"
    return {
        "schema": REQUEST_SCHEMA,
        "request_uid": request_uid,
        "enqueue_sequence": enqueue_sequence,
        "occurrence_nonce": occurrence_nonce,
        "control_candidate_uid": overlay["control_candidate_uid"],
        "normalized_overlay_sha256": overlay_sha,
        "overlay": overlay,
        "source": source,
        "position": position,
    }


def _visible_requests(root: Path, state: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for sequence in range(1, int(state["revision"]) + 1):
        row = _strict_json(_request_path(root, sequence), "parameter request")
        if (
            row.get("schema") != REQUEST_SCHEMA
            or row.get("enqueue_sequence") != sequence
        ):
            raise ParameterQueueError("parameter request identity differs")
        rows.append(row)
    return rows


def submit_manifest(
    root: Path,
    *,
    launch_profile_path: Path,
    rows: Sequence[Mapping[str, Any]],
    attempted_control_uids: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], ...]:
    if not rows:
        return ()
    with _lock(root):
        state = load_state(root)
        existing = _visible_requests(root, state)
        del attempted_control_uids
        by_uid = {str(row["request_uid"]): row for row in existing}
        prepared = []
        new_requests = []
        new_count = 0
        for row in rows:
            request = _request_document(
                enqueue_sequence=0,
                launch_profile_path=launch_profile_path,
                force_p=float(row["force_p_gain"]),
                force_i=float(row["force_i_gain"]),
                force_damping=float(row["force_damping"]),
                normal_filter_tau_s=float(
                    row.get("normal_filter_tau_s", 0.35)
                ),
                orientation_ko=float(row.get("orientation_ko", 0.4)),
                source=str(row.get("source", "manual_cli")),
                position=str(row.get("position", "tail")),
                occurrence_nonce=str(row.get("occurrence_nonce") or secrets.token_hex(16)),
            )
            prior = by_uid.get(str(request["request_uid"]))
            if prior is not None:
                if not _request_equivalent(prior, request):
                    raise ParameterQueueError("duplicate request UID differs")
                prepared.append(prior)
                continue
            new_count += 1
            request["enqueue_sequence"] = int(state["revision"]) + new_count
            by_uid[str(request["request_uid"])] = request
            new_requests.append(request)
            prepared.append(request)
        for request in new_requests:
            _write_once(
                _request_path(root, int(request["enqueue_sequence"])), request
            )
        if new_requests:
            state["revision"] = int(state["revision"]) + len(new_requests)
            _atomic_json(_state_path(root), state)
        return tuple(prepared)


def submit(
    root: Path,
    *,
    launch_profile_path: Path,
    force_p: float,
    force_i: float,
    force_damping: float,
    normal_filter_tau_s: float = 0.35,
    orientation_ko: float = 0.4,
    source: str = "manual_cli",
    position: str = "tail",
    occurrence_nonce: str | None = None,
    attempted_control_uids: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    return submit_manifest(
        root,
        launch_profile_path=launch_profile_path,
        rows=(
            {
                "force_p_gain": force_p,
                "force_i_gain": force_i,
                "force_damping": force_damping,
                "normal_filter_tau_s": normal_filter_tau_s,
                "orientation_ko": orientation_ko,
                "source": source,
                "position": position,
                "occurrence_nonce": occurrence_nonce,
            },
        ),
        attempted_control_uids=attempted_control_uids,
    )[0]


def _pending(
    root: Path, state: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    inflight_uid = (
        None if state["inflight"] is None else state["inflight"]["request_uid"]
    )
    rows = [
        row
        for row in _visible_requests(root, state)
        if row["request_uid"] != inflight_uid
        and not _receipt_path(root, str(row["request_uid"])).exists()
    ]
    rows.sort(
        key=lambda row: (
            0 if row["position"] == "next" else 1,
            int(row["enqueue_sequence"]),
        )
    )
    return tuple(rows)


def list_pending(root: Path) -> tuple[dict[str, Any], ...]:
    return _pending(root, load_state(root))


def list_requests(root: Path) -> tuple[dict[str, Any], ...]:
    return tuple(_visible_requests(root, load_state(root)))


def bind_home(
    root: Path,
    *,
    campaign_epoch: int,
    last_trial_id: int,
    last_command_seq: int,
) -> dict[str, Any]:
    home = {
        "campaign_epoch": campaign_epoch,
        "last_trial_id": last_trial_id,
        "last_command_seq": last_command_seq,
    }
    with _lock(root):
        state = load_state(root)
        if state["home_identity"] is None:
            state["home_identity"] = home
            _atomic_json(_state_path(root), state)
        elif state["home_identity"] != home and state["dispatch_sequence"] == 0:
            raise ParameterQueueError("initial Home identity changed")
        return state


def rebind_transport_home(
    root: Path,
    *,
    campaign_epoch: int,
    last_trial_id: int,
    last_command_seq: int,
) -> dict[str, Any]:
    """Start a new TP transport session without resetting receiver history."""

    home = {
        "campaign_epoch": campaign_epoch,
        "last_trial_id": last_trial_id,
        "last_command_seq": last_command_seq,
    }
    if (
        isinstance(campaign_epoch, bool)
        or not isinstance(campaign_epoch, int)
        or campaign_epoch <= 0
        or last_trial_id != 0
        or last_command_seq != 0
    ):
        raise ParameterQueueError(
            "transport rebind requires positive epoch and zero trial/command"
        )
    with _lock(root):
        state = load_state(root)
        if state["inflight"] is not None:
            raise ParameterQueueError("cannot rebind an inflight dispatch")
        state["home_identity"] = home
        _atomic_json(_state_path(root), state)
        return state


def prepare_next_dispatch(root: Path) -> dict[str, Any] | None:
    with _lock(root):
        state = load_state(root)
        if state["home_identity"] is None:
            raise ParameterQueueError("queue is not bound to READY_HOME")
        if state["inflight"] is not None:
            return _strict_json(
                _dispatch_path(root, int(state["inflight"]["dispatch_sequence"])),
                "parameter dispatch",
            )
        pending = _pending(root, state)
        if not pending:
            return None
        request = pending[0]
        # A zero Home is an explicit new TP transport session.  A prior
        # NOT_CONSUMED dispatch was not accepted by that session, so preserve
        # its evidence but allocate a new dispatch identity/high-water entry.
        home = state["home_identity"]
        reuse_not_consumed = any(
            int(home[key]) != 0 for key in ("last_trial_id", "last_command_seq")
        )
        for prior_sequence in range(int(state["dispatch_sequence"]), 0, -1):
            if not reuse_not_consumed:
                break
            prior_path = _dispatch_path(root, prior_sequence)
            reconciliation_path = _reconciliation_path(root, prior_sequence)
            if not prior_path.is_file() or not reconciliation_path.is_file():
                continue
            prior = _strict_json(prior_path, "parameter dispatch")
            reconciliation = _strict_json(
                reconciliation_path,
                "parameter reconciliation",
            )
            if (
                prior["request"]["request_uid"] == request["request_uid"]
                and reconciliation.get("status") == "NOT_CONSUMED"
                and reconciliation.get("dispatch_sha256")
                == prior["dispatch_sha256"]
            ):
                state["inflight"] = {
                    "dispatch_sequence": prior_sequence,
                    "request_uid": request["request_uid"],
                }
                _atomic_json(_state_path(root), state)
                return prior
        dispatch_sequence = int(state["dispatch_sequence"]) + 1
        control = ControlCandidateUid.parse(str(request["control_candidate_uid"]))
        occurrence = OccurrenceUid.from_control(
            control,
            protocol=PROTOCOL,
            logical_batch_sequence=dispatch_sequence,
            row_index=1,
            plan_revision=dispatch_sequence,
            selection_role=str(request["source"]),
            replicate_ordinal=1,
        )
        transport = TransportCandidateUid.from_occurrence(
            occurrence,
            parameter_uid=ParameterUid.from_candidate_digest(
                str(request["normalized_overlay_sha256"])
            ),
            protocol=PROTOCOL,
        )
        candidate_token = int(occurrence.digest[:8], 16) & 0x7FFFFFFF or 1
        home = state["home_identity"]
        dispatch = {
            "schema": DISPATCH_SCHEMA,
            "protocol": PROTOCOL,
            "dispatch_sequence": dispatch_sequence,
            "request": request,
            "packet": {
                "campaign_epoch": home["campaign_epoch"],
                "trial_id": home["last_trial_id"] + 1,
                "command": 1,
                "candidate_token": candidate_token,
                "execution_profile_id": PROFILE_INTEGER_ID,
                "command_seq": home["last_command_seq"] + 1,
                "logical_batch_sequence": dispatch_sequence,
                "batch_row_index": 1,
            },
            "request_identity": {
                "occurrence_uid": str(occurrence),
                "transport_candidate_uid": str(transport),
                "normalized_overlay_sha256": request["normalized_overlay_sha256"],
            },
        }
        dispatch["dispatch_identity"] = (
            "dispatch:v1:"
            + _sha256_bytes(
                _canonical(
                    {
                        "protocol": PROTOCOL,
                        "request_uid": request["request_uid"],
                        "dispatch_sequence": dispatch_sequence,
                        "campaign_epoch": home["campaign_epoch"],
                        "trial_id": home["last_trial_id"] + 1,
                        "command_seq": home["last_command_seq"] + 1,
                    }
                )
            )
        )
        digest = _sha256_bytes(_canonical(dispatch))
        dispatch["dispatch_sha256"] = digest
        _write_once(_dispatch_path(root, dispatch_sequence), dispatch)
        state["dispatch_sequence"] = dispatch_sequence
        state["inflight"] = {
            "dispatch_sequence": dispatch_sequence,
            "request_uid": request["request_uid"],
        }
        _atomic_json(_state_path(root), state)
        return dispatch


def finish_dispatch(
    root: Path,
    *,
    status: str,
    observed: Mapping[str, Any],
    process_composition_sha256: str | None = None,
    detail: str | None = None,
    failure_class: str | None = None,
) -> dict[str, Any]:
    if status not in RECEIPT_STATUSES:
        raise ParameterQueueError("dispatch status must be SUCCEEDED or FAILED")
    if failure_class not in {None, "IDENTITY"}:
        raise ParameterQueueError(
            "failure_class is internal-only and may only mark identity recovery"
        )
    if status == "SUCCEEDED" and failure_class is not None:
        raise ParameterQueueError("SUCCEEDED dispatch must not have failure_class")
    if process_composition_sha256 is not None and not _is_sha256(
        process_composition_sha256
    ):
        raise ParameterQueueError("process_composition_sha256 must be SHA-256")
    with _lock(root):
        state = load_state(root)
        if state["inflight"] is None:
            raise ParameterQueueError("no parameter dispatch is inflight")
        dispatch = _strict_json(
            _dispatch_path(root, int(state["inflight"]["dispatch_sequence"])),
            "parameter dispatch",
        )
        packet = dispatch["packet"]
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
        if failure_class == "IDENTITY":
            positive_identity = (
                "campaign_epoch",
                "trial_id",
                "consumed_command_seq",
            )
            if (
                isinstance(observed.get("state"), bool)
                or not isinstance(observed.get("state"), int)
                or observed["state"] != 78
                or isinstance(observed.get("safety_mode"), bool)
                or not isinstance(observed.get("safety_mode"), int)
                or observed["safety_mode"] != 1
                or any(
                    isinstance(observed.get(key), bool)
                    or not isinstance(observed.get(key), int)
                    or observed[key] <= 0
                    for key in positive_identity
                )
                or observed["consumed_command_seq"] < int(packet["command_seq"])
            ):
                raise ParameterQueueError(
                    "IDENTITY failure requires a safe terminal Home observation"
                )
            home_identity = {
                "campaign_epoch": observed["campaign_epoch"],
                "last_trial_id": observed["trial_id"],
                "last_command_seq": observed["consumed_command_seq"],
            }
        else:
            terminal_reason = observed.get("terminal_reason")
            if isinstance(terminal_reason, bool) or not isinstance(terminal_reason, int):
                raise ParameterQueueError(
                    "READY_HOME_NEXT terminal observation lacks raw terminal_reason"
                )
            expected["terminal_reason"] = terminal_reason
            if dict(observed) != expected:
                raise ParameterQueueError(
                    "READY_HOME_NEXT identity differs from dispatch"
                )
            home_identity = {
                "campaign_epoch": packet["campaign_epoch"],
                "last_trial_id": packet["trial_id"],
                "last_command_seq": packet["command_seq"],
            }
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "request_uid": dispatch["request"]["request_uid"],
            "dispatch_sequence": dispatch["dispatch_sequence"],
            "dispatch_sha256": dispatch["dispatch_sha256"],
            "status": status,
            "detail": detail,
            "terminal_observation": dict(observed),
        }
        _write_once(
            _receipt_path(root, str(receipt["request_uid"])),
            receipt,
        )
        if process_composition_sha256 is not None:
            _record_terminal_receipt_locked(
                root=root,
                existing=_load_terminal_receipts(root),
                record=_terminal_receipt_document(
                    process_composition_sha256=process_composition_sha256,
                    dispatch_identity=str(dispatch["dispatch_identity"]),
                    dispatch_sequence=int(dispatch["dispatch_sequence"]),
                    terminal_state=observed,
                ),
            )
        state["home_identity"] = home_identity
        state["inflight"] = None
        _atomic_json(_state_path(root), state)
        return receipt


def reconcile_not_consumed(
    root: Path,
    detail: str,
    observed_command_seq: int,
) -> dict[str, Any]:
    if not isinstance(detail, str) or not detail:
        raise ParameterQueueError("reconciliation detail must be non-empty")
    if (
        isinstance(observed_command_seq, bool)
        or not isinstance(observed_command_seq, int)
        or observed_command_seq < 0
    ):
        raise ParameterQueueError("observed_command_seq must be a non-negative integer")
    with _lock(root):
        state = load_state(root)
        if state["inflight"] is None:
            raise ParameterQueueError("no parameter dispatch is inflight")
        dispatch = _strict_json(
            _dispatch_path(root, int(state["inflight"]["dispatch_sequence"])),
            "parameter dispatch",
        )
        packet = dispatch["packet"]
        if observed_command_seq >= int(packet["command_seq"]):
            raise ParameterQueueError("observed command sequence consumed the dispatch")
        reconciliation = {
            "schema": RECONCILIATION_SCHEMA,
            "request_uid": dispatch["request"]["request_uid"],
            "dispatch_sequence": dispatch["dispatch_sequence"],
            "dispatch_sha256": dispatch["dispatch_sha256"],
            "status": "NOT_CONSUMED",
            "detail": detail,
            "observed_command_seq": observed_command_seq,
            "dispatched_command_seq": packet["command_seq"],
        }
        _write_once(
            _reconciliation_path(root, int(dispatch["dispatch_sequence"])),
            reconciliation,
        )
        state["inflight"] = None
        _atomic_json(_state_path(root), state)
        return reconciliation


def status(root: Path) -> dict[str, Any]:
    state = load_state(root)
    pending = list_pending(root)
    receipts = ()
    if (root / "receipts").is_dir():
        receipts = tuple(
            path
            for path in (root / "receipts").glob("*.json")
            if not path.is_symlink()
            and path.is_file()
            and _strict_json(path, "parameter receipt").get("schema")
            in {RECEIPT_SCHEMA, LEGACY_RECEIPT_SCHEMA, "step5d.parameter-receiver/receipt-v1"}
        )
    return {
        "schema": STATE_SCHEMA,
        "campaign_id": state["campaign_id"],
        "release_manifest_sha256": state.get("release_manifest_sha256"),
        "revision": state["revision"],
        "dispatch_sequence": state["dispatch_sequence"],
        "pending_count": len(pending),
        "terminal_receipt_count": len(receipts),
        "inflight": state["inflight"],
        "next_request": None if not pending else pending[0],
        "accepting": True,
        "capacity": None,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("--queue-root", type=Path, required=True)
    submit_parser.add_argument("--launch-profile", type=Path, required=True)
    submit_parser.add_argument("--force-p", type=float, required=True)
    submit_parser.add_argument("--force-i", type=float, required=True)
    submit_parser.add_argument("--force-damping", type=float, required=True)
    submit_parser.add_argument("--normal-filter-tau-s", type=float, default=0.35)
    submit_parser.add_argument("--orientation-ko", type=float, default=0.4)
    submit_parser.add_argument("--source", default="manual_cli")
    submit_parser.add_argument("--position", choices=sorted(POSITIONS), default="tail")
    submit_parser.add_argument(
        "--experiment-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--queue-root", type=Path, required=True)
    migrate_parser = subparsers.add_parser("migrate")
    migrate_parser.add_argument("--queue-root", type=Path, required=True)
    migrate_parser.add_argument("--legacy-root", type=Path, required=True)
    migrate_parser.add_argument("--campaign-id", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "status":
        payload = status(args.queue_root)
    elif args.command == "migrate":
        payload = adopt_selected_legacy_binding(
            args.queue_root,
            legacy_root=args.legacy_root,
            campaign_id=args.campaign_id,
        )
    else:
        payload = submit(
            args.queue_root,
            launch_profile_path=args.launch_profile,
            force_p=args.force_p,
            force_i=args.force_i,
            force_damping=args.force_damping,
            normal_filter_tau_s=args.normal_filter_tau_s,
            orientation_ko=args.orientation_ko,
            source=args.source,
            position=args.position,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ParameterQueueError, ValueError) as exc:
        print(f"parameter receiver blocked: {exc}", file=__import__("sys").stderr)
        raise SystemExit(2)
