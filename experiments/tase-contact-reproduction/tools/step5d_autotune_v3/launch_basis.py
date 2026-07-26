"""Bounded launch-basis artifact shared by admission and downstream workers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

SCHEMA_V1 = "step5d.autotune-v3/launch-basis-v1"
SCHEMA_V2 = "step5d.autotune-v3/launch-basis-v2"
SCHEMA = SCHEMA_V2
MAX_BYTES = 32 * 1024
REQUIRED_FIELDS_V1 = frozenset({
    "schema",
    "release_manifest_sha256",
    "runtime_identity_sha256",
    "campaign_fingerprint",
    "delivery_observation_sha256",
    "owner_pid",
    "owner_starttime",
    "authority_epoch",
    "launch_nonce",
    "argv_sha256",
    "effective_config_sha256",
    "issued_at_unix_ns",
    "expires_at_unix_ns",
    "basis_sha256",
})
REQUIRED_FIELDS = frozenset({
    "schema",
    "release_manifest_sha256",
    "runtime_identity_sha256",
    "campaign_fingerprint",
    "delivery_observation_sha256",
    "owner_pid",
    "owner_starttime",
    "authority_epoch",
    "launch_nonce",
    "argv_sha256",
    "effective_config_sha256",
    "worktree_root",
    "repository_head",
    "issued_at_unix_ns",
    "expires_at_unix_ns",
    "basis_sha256",
})


class LaunchBasisError(RuntimeError):
    """The launch basis is missing, stale, malformed, or not owner-bound."""


READY_SCHEMA = "step5d_bridge_ready_v2"
_SHA256_HEX = frozenset("0123456789abcdef")


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= _SHA256_HEX
    )


def _required_fields(schema: str) -> frozenset[str]:
    if schema == SCHEMA_V2:
        return REQUIRED_FIELDS
    if schema == SCHEMA_V1:
        return REQUIRED_FIELDS_V1
    raise LaunchBasisError("launch basis schema differs")


def _normal_absolute_root(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        resolved = Path(value).resolve()
    except OSError:
        return None
    return resolved.as_posix() if str(resolved) == value else None


def make_launch_basis(**fields: Any) -> dict[str, Any]:
    payload = {"schema": SCHEMA, **fields}
    payload["worktree_root"] = _normal_absolute_root(payload.get("worktree_root"))
    if payload["worktree_root"] is None:
        raise LaunchBasisError("worktree root is required and must be absolute normalized")
    payload.pop("basis_sha256", None)
    expected = REQUIRED_FIELDS - {"basis_sha256"}
    if set(payload) != expected:
        raise LaunchBasisError(
            "launch basis fields differ: "
            f"missing={sorted(expected - set(payload))}, "
            f"extra={sorted(set(payload) - expected)}"
        )
    if not isinstance(payload["release_manifest_sha256"], str) or not _is_sha256(
        payload["release_manifest_sha256"]
    ):
        raise LaunchBasisError("launch basis release manifest sha256 is malformed")
    if not isinstance(payload["runtime_identity_sha256"], str) or not _is_sha256(
        payload["runtime_identity_sha256"]
    ):
        raise LaunchBasisError("launch basis runtime identity sha256 is malformed")
    if not isinstance(payload["campaign_fingerprint"], str) or not _is_sha256(
        payload["campaign_fingerprint"]
    ):
        raise LaunchBasisError("launch basis campaign fingerprint is malformed")
    if not isinstance(payload["delivery_observation_sha256"], str) or not _is_sha256(
        payload["delivery_observation_sha256"]
    ):
        raise LaunchBasisError("launch basis delivery observation sha256 is malformed")
    if not isinstance(payload["owner_pid"], int) or payload["owner_pid"] <= 0:
        raise LaunchBasisError("launch basis owner pid is malformed")
    if not isinstance(payload["owner_starttime"], int) or payload["owner_starttime"] <= 0:
        raise LaunchBasisError("launch basis owner starttime is malformed")
    if not isinstance(payload["authority_epoch"], int) or payload["authority_epoch"] <= 0:
        raise LaunchBasisError("launch basis authority epoch is malformed")
    if not isinstance(payload["launch_nonce"], str) or len(payload["launch_nonce"]) != 32 or set(payload["launch_nonce"]) - _SHA256_HEX:
        raise LaunchBasisError("launch basis launch nonce is malformed")
    if not _is_sha256(payload["argv_sha256"]):
        raise LaunchBasisError("launch basis argv_sha256 is malformed")
    if not _is_sha256(payload["effective_config_sha256"]):
        raise LaunchBasisError("launch basis effective config sha256 is malformed")
    if payload["worktree_root"] != _normal_absolute_root(payload["worktree_root"]):
        raise LaunchBasisError("launch basis worktree root must be an absolute normalized path")
    if not isinstance(payload["repository_head"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", payload["repository_head"]
    ):
        raise LaunchBasisError("launch basis repository head is malformed")
    if not isinstance(payload["issued_at_unix_ns"], int) or not isinstance(
        payload["expires_at_unix_ns"], int
    ):
        raise LaunchBasisError("launch basis freshness values are malformed")
    payload["basis_sha256"] = _sha(payload)
    if len(_canonical(payload)) + 1 > MAX_BYTES:
        raise LaunchBasisError("launch basis exceeds 32 KiB")
    return payload


def write_launch_basis(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    if path.is_symlink():
        raise LaunchBasisError("launch basis path must not be a symlink")
    normalized = make_launch_basis(**dict(payload))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as handle:
        handle.write(_canonical(normalized) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return normalized


def read_and_validate_launch_basis(
    path: Path,
    *,
    owner_pid: int,
    owner_starttime: int,
    now_unix_ns: int | None = None,
    expected_basis_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(path, Path) or path.is_symlink() or not path.is_file():
        raise LaunchBasisError("launch basis is missing or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LaunchBasisError(f"launch basis is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LaunchBasisError("launch basis fields differ")
    schema = payload.get("schema")
    expected = _required_fields(schema)
    if set(payload) != expected:
        raise LaunchBasisError("launch basis fields differ")
    supplied = payload["basis_sha256"]
    unsigned = dict(payload)
    unsigned.pop("basis_sha256")
    if not _is_sha256(supplied):
        raise LaunchBasisError("launch basis digest is malformed")
    if supplied != _sha(unsigned):
        raise LaunchBasisError("launch basis digest mismatch")
    if expected_basis_sha256 is not None and supplied != expected_basis_sha256:
        raise LaunchBasisError("launch basis digest differs from admission")
    if (
        payload["schema"] == SCHEMA_V2
        and payload["worktree_root"] != _normal_absolute_root(payload["worktree_root"])
    ):
        raise LaunchBasisError("launch basis worktree root must be an absolute normalized path")
    if payload["schema"] == SCHEMA_V2 and not re.fullmatch(
        r"[0-9a-f]{40}", payload["repository_head"]
    ):
        raise LaunchBasisError("launch basis repository head is malformed")
    if payload["owner_pid"] != owner_pid or payload["owner_starttime"] != owner_starttime:
        raise LaunchBasisError("launch basis owner binding differs")
    now = time.time_ns() if now_unix_ns is None else now_unix_ns
    issued = payload["issued_at_unix_ns"]
    expires = payload["expires_at_unix_ns"]
    if not isinstance(issued, int) or not isinstance(expires, int) or issued > now or now >= expires:
        raise LaunchBasisError("launch basis is stale or not yet valid")
    return payload


def validate_delivery_observation_binding(
    path: Path,
    *,
    basis: Mapping[str, Any],
    admission: Mapping[str, Any] | None = None,
    experiment_root: Path | None = None,
) -> str:
    """Hash the actual delivery observation at every downstream consumer."""

    if not isinstance(path, Path) or path.is_symlink() or not path.is_file():
        raise LaunchBasisError("delivery observation is missing or unsafe")
    observed = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = basis.get("delivery_observation_sha256")
    if observed != expected:
        raise LaunchBasisError("delivery observation digest differs from launch basis")
    if admission is not None:
        reference = admission.get("delivery_observation")
        if isinstance(reference, Mapping):
            reference_sha = reference.get("sha256")
            reference_path = reference.get("path")
            if reference_sha != observed:
                raise LaunchBasisError("delivery observation digest differs from admission")
            if isinstance(reference_path, str) and experiment_root is not None:
                try:
                    relative = Path(reference_path)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise LaunchBasisError("delivery observation path is unsafe")
                    if path.resolve() != (experiment_root.resolve() / relative).resolve():
                        raise LaunchBasisError("delivery observation path differs from admission")
                except OSError as exc:
                    raise LaunchBasisError("delivery observation path is unsafe") from exc
    return observed


def validate_strict_bridge_ready(
    ready: Mapping[str, Any],
    *,
    bridge_pid: int,
    bridge_starttime_ticks: int,
    launch_nonce: str,
    expected_profile: str,
    ticket: Mapping[str, Any],
    basis: Mapping[str, Any],
    release: Mapping[str, Any] | Any,
    admission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one bridge-ready observation against all launch identities."""

    if not isinstance(ready, Mapping):
        raise LaunchBasisError("bridge readiness is not an object")
    if ready.get("ready_schema") != READY_SCHEMA or ready.get("ok") is not True:
        raise LaunchBasisError("bridge readiness schema/ok differs")
    if ready.get("bridge_profile") != expected_profile:
        raise LaunchBasisError("bridge readiness profile differs")
    if not isinstance(ready.get("pid"), int) or ready.get("pid") != bridge_pid:
        raise LaunchBasisError("bridge readiness process identity differs")
    if bridge_starttime_ticks <= 0:
        raise LaunchBasisError("bridge readiness process identity differs")
    if ready.get("launch_nonce") != launch_nonce:
        raise LaunchBasisError("bridge readiness launch nonce differs")
    if not isinstance(launch_nonce, str) or len(launch_nonce) != 32 or set(launch_nonce) - _SHA256_HEX:
        raise LaunchBasisError("bridge launch nonce is invalid")
    if ticket.get("launch_id") != launch_nonce:
        raise LaunchBasisError("bridge readiness ticket identity differs")
    basis_digest = basis.get("basis_sha256")
    if not _is_sha256(basis_digest):
        raise LaunchBasisError("bridge readiness basis digest is invalid")
    ticket_basis = ticket.get("launch_basis")
    ticket_delivery = ticket.get("delivery_observation")
    if not isinstance(ticket_basis, Mapping):
        raise LaunchBasisError("bridge readiness ticket basis is malformed")
    if set(ticket_basis) != {"path", "sha256"}:
        raise LaunchBasisError("bridge readiness ticket basis is incomplete")
    basis_path = ticket_basis.get("path")
    if not isinstance(basis_path, str):
        raise LaunchBasisError("bridge readiness ticket basis path is malformed")
    if not Path(basis_path).is_absolute():
        raise LaunchBasisError("bridge readiness ticket basis path must be absolute")
    if not _is_sha256(ticket_basis.get("sha256")):
        raise LaunchBasisError("bridge readiness ticket basis digest is malformed")
    if ticket_basis.get("sha256") != basis_digest:
        raise LaunchBasisError("bridge readiness basis identity differs")
    if not isinstance(ticket_delivery, Mapping):
        raise LaunchBasisError("bridge readiness ticket delivery observation is malformed")
    if set(ticket_delivery) != {"path", "sha256"}:
        raise LaunchBasisError("bridge readiness ticket delivery observation is incomplete")
    if not _is_sha256(ticket_delivery.get("sha256")):
        raise LaunchBasisError("bridge readiness ticket delivery observation digest is malformed")
    if ticket_delivery.get("sha256") != basis.get("delivery_observation_sha256"):
        raise LaunchBasisError("bridge readiness basis identity differs")
    manifest = release.get("manifest_sha256") if isinstance(release, Mapping) else getattr(release, "manifest_sha256", None)
    if basis.get("release_manifest_sha256") != manifest:
        raise LaunchBasisError("bridge readiness basis release differs")
    if admission is not None:
        admission_release = admission.get("release")
        if (
            isinstance(admission_release, Mapping)
            and admission_release.get("manifest_sha256") != manifest
        ):
            raise LaunchBasisError("bridge readiness admission release differs")
    for field in ("rtde_connected", "rtde_send_succeeded", "sensor_stream_ready"):
        if ready.get(field) is not True:
            raise LaunchBasisError(f"bridge readiness {field} is not true")
    if ready.get("prewarm_status") != "ok":
        raise LaunchBasisError("bridge readiness prewarm is not complete")
    fields = ready.get("rtde_output_fields")
    types = ready.get("rtde_output_types")
    if (
        not isinstance(fields, list)
        or not isinstance(types, list)
        or not fields
        or len(fields) != len(types)
    ):
        raise LaunchBasisError("bridge readiness RTDE recipe is incomplete")
    return dict(ready)
