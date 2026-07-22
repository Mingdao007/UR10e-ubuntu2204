#!/usr/bin/env python3
"""Validate externally issued Manual V2 capabilities for one bridge attempt."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Mapping

from step5d_autotune_v3.governance import read_proc_starttime_ticks
from step5d_bridge_authority import (
    SCHEMA as OWNER_AUTHORITY_SCHEMA,
    STATE_FILE as OWNER_AUTHORITY_STATE_FILE,
)


AUTHORIZATION_SCHEMA = "step5d.manual-v2/capability-authorization-v2"
CAPABILITIES = ("bridge", "play", "arm", "motion", "zero", "tare")
ISSUER = "ur10e-live-bench-owner"
EXPECTED_AUTHORITY_ROOT = (
    Path(__file__).resolve().parents[1] / "runs/step5d_bridge_authority"
)


class ManualAuthorizationError(RuntimeError):
    pass


def _capture_regular(path: Path, role: str) -> tuple[bytes, dict[str, str]]:
    exact = path.expanduser().absolute()
    descriptor = os.open(exact, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ManualAuthorizationError(f"{role} is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            encoded = stream.read()
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ManualAuthorizationError(f"{role} changed while captured")
    return encoded, {
        "path": str(exact),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _validate_owner_provenance(
    value: Any, *, attempt_id: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "path",
        "sha256",
        "sequence",
        "owner",
    }:
        raise ManualAuthorizationError("manual owner provenance fields differ")
    expected_path = EXPECTED_AUTHORITY_ROOT / OWNER_AUTHORITY_STATE_FILE
    path = Path(str(value["path"]))
    if path != expected_path:
        raise ManualAuthorizationError("manual owner provenance path differs")
    encoded, reference = _capture_regular(path, "manual owner authority")
    if reference != {"path": str(path), "sha256": value["sha256"]}:
        raise ManualAuthorizationError("manual owner authority bytes differ")
    try:
        authority = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManualAuthorizationError("manual owner authority is invalid") from exc
    owner = value.get("owner")
    if (
        not isinstance(authority, Mapping)
        or authority.get("schema") != OWNER_AUTHORITY_SCHEMA
        or authority.get("state") != "ACTIVE"
        or authority.get("attempt_id") != attempt_id
        or authority.get("sequence") != value.get("sequence")
        or authority.get("owner") != owner
        or not isinstance(owner, Mapping)
        or set(owner) != {"pid", "starttime_ticks"}
        or read_proc_starttime_ticks(owner.get("pid"))
        != owner.get("starttime_ticks")
    ):
        raise ManualAuthorizationError("manual owner authority is not current")
    return dict(value)


def load_capability_authorization_bytes(
    encoded: bytes,
    *,
    attempt_id: str,
    campaign_id: str,
    release_manifest_sha256: str,
    now_ns: int | None = None,
) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ManualAuthorizationError(
                    f"manual capability authorization repeats JSON key {key!r}"
                )
            payload[key] = value
        return payload

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ManualAuthorizationError(
                    f"manual capability authorization contains {value}"
                )
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManualAuthorizationError(
            f"manual capability authorization is invalid JSON: {exc}"
        ) from exc
    required = {
        "schema",
        "attempt_id",
        "campaign_id",
        "release_manifest_sha256",
        "authorized_at_unix_ns",
        "expires_at_unix_ns",
        "issuer",
        "capabilities",
        "owner_authority",
    }
    if set(payload) != required or payload.get("schema") != AUTHORIZATION_SCHEMA:
        raise ManualAuthorizationError("manual capability authorization schema differs")
    capabilities = payload.get("capabilities")
    if (
        not isinstance(capabilities, Mapping)
        or set(capabilities) != set(CAPABILITIES)
        or any(not isinstance(capabilities[name], bool) for name in CAPABILITIES)
    ):
        raise ManualAuthorizationError("manual capability authorization fields differ")
    observed_now = time.time_ns() if now_ns is None else now_ns
    issued = payload.get("authorized_at_unix_ns")
    expires = payload.get("expires_at_unix_ns")
    if (
        isinstance(issued, bool)
        or not isinstance(issued, int)
        or isinstance(expires, bool)
        or not isinstance(expires, int)
        or issued < 1
        or expires <= issued
        or not issued <= observed_now < expires
    ):
        raise ManualAuthorizationError("manual capability authorization is not current")
    if (
        payload.get("attempt_id") != attempt_id
        or payload.get("campaign_id") != campaign_id
        or payload.get("release_manifest_sha256") != release_manifest_sha256
        or payload.get("issuer") != ISSUER
    ):
        raise ManualAuthorizationError("manual capability authorization binding differs")
    _validate_owner_provenance(payload.get("owner_authority"), attempt_id=attempt_id)
    if not all(capabilities[name] for name in ("bridge", "play", "arm", "motion")):
        raise ManualAuthorizationError("manual Play/ARM/motion capabilities are not authorized")
    if capabilities["zero"] or capabilities["tare"]:
        raise ManualAuthorizationError("manual authorization must not include zero or tare")
    return dict(payload)


def capture_capability_authorization(
    path: Path,
    *,
    attempt_id: str,
    campaign_id: str,
    release_manifest_sha256: str,
    now_ns: int | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    exact = path.expanduser().absolute()
    encoded, reference = _capture_regular(
        exact, "manual capability authorization"
    )
    payload = load_capability_authorization_bytes(
        encoded,
        attempt_id=attempt_id,
        campaign_id=campaign_id,
        release_manifest_sha256=release_manifest_sha256,
        now_ns=now_ns,
    )
    return payload, reference


def load_capability_authorization(
    path: Path,
    *,
    attempt_id: str,
    campaign_id: str,
    release_manifest_sha256: str,
    now_ns: int | None = None,
) -> dict[str, Any]:
    payload, _reference = capture_capability_authorization(
        path,
        attempt_id=attempt_id,
        campaign_id=campaign_id,
        release_manifest_sha256=release_manifest_sha256,
        now_ns=now_ns,
    )
    return payload
