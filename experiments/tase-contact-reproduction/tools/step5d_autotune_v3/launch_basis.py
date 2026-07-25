"""Bounded launch-basis artifact shared by admission and downstream workers."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "step5d.autotune-v3/launch-basis-v1"
MAX_BYTES = 32 * 1024
REQUIRED_FIELDS = frozenset({
    "schema", "release_manifest_sha256", "runtime_identity_sha256",
    "campaign_fingerprint", "delivery_observation_sha256", "owner_pid",
    "owner_starttime", "authority_epoch", "launch_nonce", "argv_sha256",
    "effective_config_sha256", "issued_at_unix_ns", "expires_at_unix_ns",
    "basis_sha256",
})


class LaunchBasisError(RuntimeError):
    """The launch basis is missing, stale, malformed, or not owner-bound."""


READY_SCHEMA = "step5d_bridge_ready_v2"
_SHA256_HEX = frozenset("0123456789abcdef")


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def make_launch_basis(**fields: Any) -> dict[str, Any]:
    payload = {"schema": SCHEMA, **fields}
    payload.pop("basis_sha256", None)
    expected = REQUIRED_FIELDS - {"basis_sha256"}
    if set(payload) != expected:
        raise LaunchBasisError(f"launch basis fields differ: missing={sorted(expected - set(payload))}, extra={sorted(set(payload) - expected)}")
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


def read_and_validate_launch_basis(path: Path, *, owner_pid: int, owner_starttime: int, now_unix_ns: int | None = None, expected_basis_sha256: str | None = None) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LaunchBasisError("launch basis is missing or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LaunchBasisError(f"launch basis is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != REQUIRED_FIELDS:
        raise LaunchBasisError("launch basis fields differ")
    supplied = payload["basis_sha256"]
    unsigned = dict(payload)
    unsigned.pop("basis_sha256")
    if supplied != _sha(unsigned):
        raise LaunchBasisError("launch basis digest mismatch")
    if expected_basis_sha256 is not None and supplied != expected_basis_sha256:
        raise LaunchBasisError("launch basis digest differs from admission")
    if payload["schema"] != SCHEMA:
        raise LaunchBasisError("launch basis schema differs")
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

    if path.is_symlink() or not path.is_file():
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
    if ready.get("pid") != bridge_pid or bridge_starttime_ticks <= 0:
        raise LaunchBasisError("bridge readiness process identity differs")
    if ready.get("launch_nonce") != launch_nonce:
        raise LaunchBasisError("bridge readiness launch nonce differs")
    if not isinstance(launch_nonce, str) or len(launch_nonce) != 32 or set(launch_nonce) - _SHA256_HEX:
        raise LaunchBasisError("bridge launch nonce is invalid")
    if ticket.get("launch_id") != launch_nonce:
        raise LaunchBasisError("bridge readiness ticket identity differs")
    if ticket.get("control_profile_id") != expected_profile:
        raise LaunchBasisError("bridge ticket profile differs")
    basis_digest = basis.get("basis_sha256")
    if not isinstance(basis_digest, str) or len(basis_digest) != 64 or set(basis_digest) - _SHA256_HEX:
        raise LaunchBasisError("bridge readiness basis digest is invalid")
    ticket_basis = ticket.get("launch_basis")
    ticket_delivery = ticket.get("delivery_observation")
    if (
        not isinstance(ticket_basis, Mapping)
        or set(ticket_basis) != {"path", "sha256"}
        or ticket_basis.get("sha256") != basis_digest
        or not isinstance(ticket_delivery, Mapping)
        or set(ticket_delivery) != {"path", "sha256"}
        or ticket_delivery.get("sha256") != basis.get("delivery_observation_sha256")
    ):
        raise LaunchBasisError("bridge readiness basis identity differs")
    manifest = release.get("manifest_sha256") if isinstance(release, Mapping) else getattr(release, "manifest_sha256", None)
    program = release.get("program_id") if isinstance(release, Mapping) else getattr(release, "program_id", None)
    if ticket.get("manifest_sha256") != manifest or ticket.get("tp_program_id") != program:
        raise LaunchBasisError("bridge readiness release identity differs")
    if basis.get("release_manifest_sha256") != manifest:
        raise LaunchBasisError("bridge readiness basis release differs")
    if admission is not None:
        admission_release = admission.get("release")
        if isinstance(admission_release, Mapping) and (
            admission_release.get("manifest_sha256") != manifest
            or admission_release.get("program_id") != program
        ):
            raise LaunchBasisError("bridge readiness admission release differs")
    for field in ("rtde_connected", "rtde_send_succeeded", "sensor_stream_ready"):
        if ready.get(field) is not True:
            raise LaunchBasisError(f"bridge readiness {field} is not true")
    if ready.get("prewarm_status") != "ok":
        raise LaunchBasisError("bridge readiness prewarm is not complete")
    fields = ready.get("rtde_output_fields")
    types = ready.get("rtde_output_types")
    if not isinstance(fields, list) or not isinstance(types, list) or not fields or len(fields) != len(types):
        raise LaunchBasisError("bridge readiness RTDE recipe is incomplete")
    return dict(ready)
