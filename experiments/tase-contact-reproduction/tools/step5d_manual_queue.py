#!/usr/bin/env python3
"""Crash-safe one-row manual queue for the Step5d full-home hold protocol."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from step5d_autotune_v3.profile import canonical_json_bytes
from step5d_autotune_v3.runtime_profile import (
    DEFAULT_OVERLAY,
    control_candidate_uid,
    load_launch_profile,
    normalize_trial_overlay,
    normalized_overlay_sha256,
)


SCHEMA = "step5d.autotune-v3/manual-queue-v1"
PROTOCOL = "v3_full_home_manual_hold_v1"
PARENT_R009_COMMIT = "bf6eb59d9530cf7f170f29c68c8812f00613b381"
EXECUTION_PROFILE_ID = "nf100-slew050-a050"
EXECUTION_PROFILE_INTEGER_ID = 633
OPEN_EMPTY = "OPEN_EMPTY"
OPEN_READY = "OPEN_READY"
CLOSED_COMPLETE = "CLOSED_COMPLETE"


class ManualQueueError(RuntimeError):
    """The manual queue is unsafe, inconsistent, or outside its envelope."""


def _sha256(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ManualQueueError(f"{role} must be a lowercase SHA-256")
    return value


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest()


def _strict_json(encoded: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ManualQueueError(f"manual queue repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ManualQueueError(f"manual queue contains non-finite {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManualQueueError(f"manual queue is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManualQueueError("manual queue must be a JSON object")
    return payload


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ManualQueueError("manual queue must be a real regular file")
    return validate_queue(_strict_json(path.read_bytes()))


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(
        payload, indent=2, sort_keys=True, allow_nan=False
    ).encode("utf-8") + b"\n"
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


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ManualQueueError("manual queue lock is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def validate_queue(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "protocol",
        "parent_r009_commit",
        "campaign_id",
        "release_manifest_sha256",
        "execution_profile_id",
        "execution_profile_integer_id",
        "revision",
        "lifecycle",
        "requests",
        "closure",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ManualQueueError("manual queue fields differ")
    if payload["schema"] != SCHEMA or payload["protocol"] != PROTOCOL:
        raise ManualQueueError("manual queue schema or protocol differs")
    if payload["parent_r009_commit"] != PARENT_R009_COMMIT:
        raise ManualQueueError("manual queue parent r009 commit differs")
    if not isinstance(payload["campaign_id"], str) or not payload["campaign_id"]:
        raise ManualQueueError("manual queue campaign identity is missing")
    _sha256(payload["release_manifest_sha256"], "release manifest")
    if (
        payload["execution_profile_id"] != EXECUTION_PROFILE_ID
        or payload["execution_profile_integer_id"] != EXECUTION_PROFILE_INTEGER_ID
    ):
        raise ManualQueueError("manual queue execution profile differs")
    requests = payload["requests"]
    if not isinstance(requests, list):
        raise ManualQueueError("manual queue requests must be an array")
    if payload["revision"] != len(requests):
        raise ManualQueueError("manual queue revision differs from request count")
    seen_occurrence: set[str] = set()
    seen_transport: set[str] = set()
    seen_token: set[int] = set()
    for sequence, row in enumerate(requests, start=1):
        fields = {
            "logical_batch_sequence",
            "row_index",
            "occurrence_nonce",
            "occurrence_uid",
            "transport_candidate_uid",
            "control_candidate_uid",
            "candidate_token",
            "overlay",
            "normalized_overlay_sha256",
            "source",
        }
        if not isinstance(row, Mapping) or set(row) != fields:
            raise ManualQueueError("manual request fields differ")
        if row["logical_batch_sequence"] != sequence or row["row_index"] != 1:
            raise ManualQueueError("manual request sequence or row differs")
        if not isinstance(row["occurrence_nonce"], str) or len(row["occurrence_nonce"]) != 32:
            raise ManualQueueError("manual occurrence nonce differs")
        occurrence = _sha256(row["occurrence_uid"], "occurrence UID")
        transport = _sha256(row["transport_candidate_uid"], "transport UID")
        control = _sha256(row["control_candidate_uid"], "control UID")
        if occurrence in seen_occurrence or transport in seen_transport:
            raise ManualQueueError("manual request repeats occurrence or transport identity")
        seen_occurrence.add(occurrence)
        seen_transport.add(transport)
        token = row["candidate_token"]
        if isinstance(token, bool) or not isinstance(token, int) or not 1 <= token <= 2_147_483_647:
            raise ManualQueueError("manual candidate token is outside signed int32")
        if token in seen_token:
            raise ManualQueueError("manual candidate-token projection collided")
        seen_token.add(token)
        if not isinstance(row["source"], str) or not row["source"]:
            raise ManualQueueError("manual request source is missing")
        overlay = row["overlay"]
        if not isinstance(overlay, Mapping) or overlay.get("control_candidate_uid") != control:
            raise ManualQueueError("manual overlay control identity differs")
        overlay_sha = _sha256(row["normalized_overlay_sha256"], "overlay")
        if control_candidate_uid(overlay) != control:
            raise ManualQueueError("manual overlay parameters differ from control identity")
        if hashlib.sha256(canonical_json_bytes(dict(overlay))).hexdigest() != overlay_sha:
            raise ManualQueueError("manual normalized overlay digest differs")
        expected_occurrence = _digest(
            {
                "schema": "step5d.autotune-v3/manual-occurrence-v1",
                "protocol": PROTOCOL,
                "campaign_id": payload["campaign_id"],
                "logical_batch_sequence": sequence,
                "row_index": 1,
                "occurrence_nonce": row["occurrence_nonce"],
                "control_candidate_uid": control,
            }
        )
        expected_transport = _digest(
            {
                "schema": "step5d.autotune-v3/manual-transport-v1",
                "occurrence_uid": occurrence,
                "normalized_overlay_sha256": overlay_sha,
            }
        )
        if occurrence != expected_occurrence or transport != expected_transport:
            raise ManualQueueError("manual request identity digest differs")
        expected_token = int(occurrence[:8], 16) & 0x7FFFFFFF or 1
        if token != expected_token:
            raise ManualQueueError("manual candidate-token projection differs")
    lifecycle = payload["lifecycle"]
    if lifecycle not in {OPEN_EMPTY, OPEN_READY, CLOSED_COMPLETE}:
        raise ManualQueueError("manual queue lifecycle differs")
    if lifecycle == CLOSED_COMPLETE:
        closure = payload["closure"]
        if not isinstance(closure, Mapping) or set(closure) != {"reason", "evidence_sha256"}:
            raise ManualQueueError("manual queue closure differs")
        _sha256(closure["evidence_sha256"], "closure evidence")
    elif payload["closure"] is not None:
        raise ManualQueueError("open manual queue cannot contain closure")
    return dict(payload)


def enqueue(
    path: Path,
    *,
    campaign_id: str,
    release_manifest_sha256: str,
    launch_profile_path: Path,
    force_p: float,
    force_i: float,
    force_damping: float,
    orientation_ko: float = 0.4,
    source: str = "manual_cli",
    occurrence_nonce: str | None = None,
) -> dict[str, Any]:
    profile = load_launch_profile(launch_profile_path)
    overlay_input = {
        **DEFAULT_OVERLAY,
        "force_p_gain": force_p,
        "force_i_gain": force_i,
        "force_damping": force_damping,
        "orientation_ko": orientation_ko,
    }
    overlay_input.pop("control_candidate_uid", None)
    overlay = normalize_trial_overlay(overlay_input, profile=profile)
    overlay_sha = normalized_overlay_sha256(profile, overlay)
    release_sha = _sha256(release_manifest_sha256, "release manifest")
    nonce = occurrence_nonce or secrets.token_hex(16)
    if len(nonce) != 32 or any(character not in "0123456789abcdef" for character in nonce):
        raise ManualQueueError("occurrence nonce must be 16 lowercase hex bytes")
    with _locked(path):
        if path.exists() or path.is_symlink():
            payload = _read(path)
            if payload["campaign_id"] != campaign_id:
                raise ManualQueueError("manual queue campaign identity changed")
            if payload["release_manifest_sha256"] != release_sha:
                raise ManualQueueError("manual queue release identity changed")
            if payload["lifecycle"] == CLOSED_COMPLETE:
                raise ManualQueueError("closed manual queue cannot be appended")
        else:
            payload = {
                "schema": SCHEMA,
                "protocol": PROTOCOL,
                "parent_r009_commit": PARENT_R009_COMMIT,
                "campaign_id": campaign_id,
                "release_manifest_sha256": release_sha,
                "execution_profile_id": EXECUTION_PROFILE_ID,
                "execution_profile_integer_id": EXECUTION_PROFILE_INTEGER_ID,
                "revision": 0,
                "lifecycle": OPEN_EMPTY,
                "requests": [],
                "closure": None,
            }
        sequence = payload["revision"] + 1
        control_uid = overlay["control_candidate_uid"]
        occurrence_uid = _digest(
            {
                "schema": "step5d.autotune-v3/manual-occurrence-v1",
                "protocol": PROTOCOL,
                "campaign_id": campaign_id,
                "logical_batch_sequence": sequence,
                "row_index": 1,
                "occurrence_nonce": nonce,
                "control_candidate_uid": control_uid,
            }
        )
        transport_uid = _digest(
            {
                "schema": "step5d.autotune-v3/manual-transport-v1",
                "occurrence_uid": occurrence_uid,
                "normalized_overlay_sha256": overlay_sha,
            }
        )
        candidate_token = int(occurrence_uid[:8], 16) & 0x7FFFFFFF or 1
        if any(row["candidate_token"] == candidate_token for row in payload["requests"]):
            raise ManualQueueError("manual candidate-token projection collision; retry enqueue")
        request = {
            "logical_batch_sequence": sequence,
            "row_index": 1,
            "occurrence_nonce": nonce,
            "occurrence_uid": occurrence_uid,
            "transport_candidate_uid": transport_uid,
            "control_candidate_uid": control_uid,
            "candidate_token": candidate_token,
            "overlay": overlay,
            "normalized_overlay_sha256": overlay_sha,
            "source": source,
        }
        payload["requests"] = [*payload["requests"], request]
        payload["revision"] = sequence
        payload["lifecycle"] = OPEN_READY
        validate_queue(payload)
        _atomic_write(path, payload)
        return request


def close(path: Path, *, reason: str) -> dict[str, Any]:
    if not reason:
        raise ManualQueueError("manual queue closure reason is required")
    with _locked(path):
        payload = _read(path)
        if payload["lifecycle"] == CLOSED_COMPLETE:
            return payload
        evidence = _digest(
            {
                "schema": "step5d.autotune-v3/manual-closure-evidence-v1",
                "campaign_id": payload["campaign_id"],
                "revision": payload["revision"],
                "reason": reason,
                "last_occurrence_uid": (
                    None if not payload["requests"] else payload["requests"][-1]["occurrence_uid"]
                ),
            }
        )
        payload["lifecycle"] = CLOSED_COMPLETE
        payload["closure"] = {"reason": reason, "evidence_sha256": evidence}
        validate_queue(payload)
        _atomic_write(path, payload)
        return payload


def status(path: Path) -> dict[str, Any]:
    payload = _read(path)
    return {
        "schema": SCHEMA,
        "campaign_id": payload["campaign_id"],
        "revision": payload["revision"],
        "lifecycle": payload["lifecycle"],
        "release_manifest_sha256": payload["release_manifest_sha256"],
        "last_request": None if not payload["requests"] else payload["requests"][-1],
        "closure": payload["closure"],
    }


def load_queue(path: Path) -> dict[str, Any]:
    return _read(path)


def next_request(path: Path, *, completed_sequences: set[int]) -> dict[str, Any] | None:
    payload = _read(path)
    for request in payload["requests"]:
        if request["logical_batch_sequence"] not in completed_sequences:
            return dict(request)
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    enqueue_parser = subparsers.add_parser("enqueue")
    enqueue_parser.add_argument("--queue", type=Path, required=True)
    enqueue_parser.add_argument("--campaign-id", required=True)
    enqueue_parser.add_argument("--release-manifest-sha256", required=True)
    enqueue_parser.add_argument("--launch-profile", type=Path, required=True)
    enqueue_parser.add_argument("--force-p", type=float, required=True)
    enqueue_parser.add_argument("--force-i", type=float, required=True)
    enqueue_parser.add_argument("--force-damping", type=float, required=True)
    enqueue_parser.add_argument("--orientation-ko", type=float, default=0.4)
    enqueue_parser.add_argument("--source", default="manual_cli")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--queue", type=Path, required=True)
    close_parser = subparsers.add_parser("close")
    close_parser.add_argument("--queue", type=Path, required=True)
    close_parser.add_argument("--reason", default="operator_complete")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "enqueue":
        result = enqueue(
            args.queue,
            campaign_id=args.campaign_id,
            release_manifest_sha256=args.release_manifest_sha256,
            launch_profile_path=args.launch_profile,
            force_p=args.force_p,
            force_i=args.force_i,
            force_damping=args.force_damping,
            orientation_ko=args.orientation_ko,
            source=args.source,
        )
    elif args.command == "status":
        result = status(args.queue)
    else:
        result = close(args.queue, reason=args.reason)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ManualQueueError, ValueError) as exc:
        print(f"manual queue blocked: {exc}", file=__import__("sys").stderr)
        raise SystemExit(2)
