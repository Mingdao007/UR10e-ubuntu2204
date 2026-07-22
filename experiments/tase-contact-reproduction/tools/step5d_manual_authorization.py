#!/usr/bin/env python3
"""Validate owner-issued Manual V2 capability documents; never issue them."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Mapping

from step5d_manual_bridge import strict_object


AUTHORIZATION_SCHEMA = "step5d.manual-v2/capability-authorization-v1"
CAPABILITIES = ("bridge", "play", "arm", "motion", "zero", "tare")
ISSUER = "ur10e-live-bench-owner"


class ManualAuthorizationError(RuntimeError):
    pass


def load_capability_authorization(
    path: Path,
    *,
    attempt_id: str,
    campaign_id: str,
    release_manifest_sha256: str,
    now_ns: int | None = None,
) -> dict[str, Any]:
    payload = strict_object(path, "manual capability authorization")
    required = {
        "schema",
        "attempt_id",
        "campaign_id",
        "release_manifest_sha256",
        "authorized_at_unix_ns",
        "expires_at_unix_ns",
        "issuer",
        "capabilities",
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
    if not all(capabilities[name] for name in ("bridge", "play", "arm", "motion")):
        raise ManualAuthorizationError("manual Play/ARM/motion capabilities are not authorized")
    if capabilities["zero"] or capabilities["tare"]:
        raise ManualAuthorizationError("manual authorization must not include zero or tare")
    return dict(payload)
