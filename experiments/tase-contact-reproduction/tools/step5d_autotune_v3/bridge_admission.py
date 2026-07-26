"""Read-only admission for the exact TP-local Step5d release."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping

from .atomic_io import AtomicIOError, atomic_bytes
from .delivery_observation import (
    load_delivery_observation,
    resolve_delivery_observation,
)
from .release_certificate import certificate_path, load_release_certificate
from .release_contract import (
    release_contract_scope_for_release,
    validate_release_contract_result,
)
from .release_identity import (
    ReleaseIdentity,
    load_runtime_release,
)


SCHEMA = "step5d.autotune-v3/bridge-admission-v2"
INDEX_ROOT = Path("runs/step5d_autotune_v3/bridge-admissions")
ADMISSION_MAX_AGE_NS = 5_000_000_000
ADMISSION_FUTURE_SKEW_NS = 5_000_000
ADMISSION_MAX_JSON_BYTES = 32 * 1024


class BridgeAdmissionError(RuntimeError):
    """The read-only admission observation could not be completed safely."""


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise BridgeAdmissionError("admission evidence is missing or unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release_contract_reference(
    root: Path,
    release: ReleaseIdentity,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    certificate_root = root.resolve(strict=True) / "runs/step5d_autotune_v3"
    scope = release_contract_scope_for_release(
        root,
        release,
        environment=environment,
    )
    path = certificate_path(certificate_root, scope)
    _certificate, evidence, payload = load_release_certificate(
        certificate_root,
        path,
        expected_scope=scope,
    )
    validate_release_contract_result(payload, expected_scope=scope)
    return {
        "certificate_path": path.relative_to(root.resolve(strict=True)).as_posix(),
        "certificate_sha256": _sha256(path),
        "evidence_path": evidence.relative_to(root.resolve(strict=True)).as_posix(),
        "evidence_sha256": _sha256(evidence),
    }


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = (
            json.dumps(
                dict(value),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BridgeAdmissionError(
            f"bridge admission is not canonical JSON: {exc}"
        ) from exc
    if len(encoded) > ADMISSION_MAX_JSON_BYTES:
        raise BridgeAdmissionError(
            f"bridge admission JSON exceeds {ADMISSION_MAX_JSON_BYTES} bytes"
        )
    return encoded


def validate_bridge_admission(
    root: Path,
    value: Mapping[str, Any],
    *,
    release: ReleaseIdentity,
    now_ns: int | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    required = {
        "schema",
        "observed_at_unix_ns",
        "state",
        "ok",
        "reason_code",
        "release",
        "release_contract",
        "delivery_observation",
        "authority_acquired",
        "attempt_created",
        "campaign_fingerprint",
    }
    row = dict(value)
    observed_at = row.get("observed_at_unix_ns")
    observed_now = time.time_ns() if now_ns is None else now_ns
    if (
        set(row) != required
        or row.get("schema") != SCHEMA
        or isinstance(observed_at, bool)
        or not isinstance(observed_at, int)
        or observed_at <= 0
        or observed_at > observed_now + ADMISSION_FUTURE_SKEW_NS
        or observed_now - observed_at > ADMISSION_MAX_AGE_NS
        or row.get("state") != "BRIDGE_START_READY"
        or row.get("ok") is not True
        or row.get("reason_code") != "DELIVERY_VERIFIED"
        or row.get("authority_acquired") is not False
        or row.get("attempt_created") is not False
    ):
        raise BridgeAdmissionError("bridge admission fields or freshness differ")
    release_ref = row.get("release")
    delivery_ref = row.get("delivery_observation")
    if (
        not isinstance(release_ref, Mapping)
        or release_ref
        != {
            "manifest_sha256": release.manifest_sha256,
            "program_id": release.program_id,
        }
        or not isinstance(delivery_ref, Mapping)
        or set(delivery_ref) != {"path", "sha256", "transaction_id"}
    ):
        raise BridgeAdmissionError("bridge admission release binding differs")
    campaign_fingerprint = row.get("campaign_fingerprint")
    if (
        not isinstance(campaign_fingerprint, str)
        or len(campaign_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in campaign_fingerprint)
    ):
        raise BridgeAdmissionError("bridge admission campaign fingerprint differs")
    if environment is None:
        expected_release_contract = release_contract_reference(root, release)
    else:
        expected_release_contract = release_contract_reference(
            root,
            release,
            environment=environment,
        )
    if row.get("release_contract") != expected_release_contract:
        raise BridgeAdmissionError("bridge admission release contract differs")
    experiment = root.resolve(strict=True)
    relative_delivery = Path(str(delivery_ref["path"]))
    if (
        relative_delivery.is_absolute()
        or any(part in {"", ".", ".."} for part in relative_delivery.parts)
    ):
        raise BridgeAdmissionError("bridge admission delivery path is unsafe")
    unresolved_delivery = experiment / relative_delivery
    if unresolved_delivery.is_symlink():
        raise BridgeAdmissionError("bridge admission delivery path is unsafe")
    delivery_path = unresolved_delivery.resolve(strict=True)
    try:
        delivery_path.relative_to(experiment)
    except ValueError as exc:
        raise BridgeAdmissionError(
            "bridge admission delivery path is unsafe"
        ) from exc
    delivery = load_delivery_observation(root, delivery_path, release=release)
    if (
        _sha256(delivery_path) != delivery_ref["sha256"]
        or delivery["transaction_id"] != delivery_ref["transaction_id"]
    ):
        raise BridgeAdmissionError("bridge admission delivery binding differs")
    return row


def admission_index_path(root: Path, value: Mapping[str, Any]) -> Path:
    release = value.get("release")
    if not isinstance(release, Mapping):
        raise BridgeAdmissionError("bridge admission release binding differs")
    release_sha256 = release.get("manifest_sha256")
    if (
        not isinstance(release_sha256, str)
        or len(release_sha256) != 64
        or any(character not in "0123456789abcdef" for character in release_sha256)
    ):
        raise BridgeAdmissionError("bridge admission release digest differs")
    digest = hashlib.sha256(_canonical_bytes(value)).hexdigest()
    return root.resolve(strict=True) / INDEX_ROOT / release_sha256 / f"{digest}.json"


def write_indexed_bridge_admission(
    root: Path,
    value: Mapping[str, Any],
) -> Path:
    encoded = _canonical_bytes(value)
    indexed_output = admission_index_path(root, value)
    if indexed_output.exists() or indexed_output.is_symlink():
        if (
            indexed_output.is_symlink()
            or not indexed_output.is_file()
            or indexed_output.read_bytes() != encoded
        ):
            raise BridgeAdmissionError(
                "content-addressed bridge admission differs"
            )
        return indexed_output
    try:
        atomic_bytes(indexed_output, encoded)
    except (AtomicIOError, OSError) as exc:
        raise BridgeAdmissionError(
            f"cannot write indexed bridge admission: {exc}"
        ) from exc
    try:
        if (
            indexed_output.is_symlink()
            or not indexed_output.is_file()
            or indexed_output.read_bytes() != encoded
        ):
            raise BridgeAdmissionError(
                "indexed bridge admission write is not idempotent"
            )
    except OSError as exc:
        raise BridgeAdmissionError(
            f"cannot verify indexed bridge admission: {exc}"
        ) from exc
    return indexed_output


def resolve_bridge_admission(
    root: Path,
    *,
    release: ReleaseIdentity,
    now_ns: int | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    experiment = root.resolve(strict=True)
    index = experiment / INDEX_ROOT / release.manifest_sha256
    if index.is_symlink() or not index.is_dir():
        raise BridgeAdmissionError("current release has no indexed bridge admission")
    candidates: list[tuple[int, str, Path, dict[str, Any]]] = []
    for path in sorted(index.glob("*.json")):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            observed_sha256 = _sha256(path)
            if path.stem != observed_sha256:
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                continue
            row = validate_bridge_admission(
                experiment,
                raw,
                release=release,
                now_ns=now_ns,
                environment=environment,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, BridgeAdmissionError):
            continue
        candidates.append(
            (int(row["observed_at_unix_ns"]), observed_sha256, path, row)
        )
    if not candidates:
        raise BridgeAdmissionError("current release has no fresh valid bridge admission")
    _observed_at, _digest, path, row = max(candidates)
    return path, row


def observe_bridge_admission(
    root: Path,
    *,
    compatibility_delivery_observation: Path | None = None,
    robot_host: str | None = None,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    del robot_host, timeout_s
    experiment = root.resolve(strict=True)
    release = load_runtime_release(experiment)
    release_contract = release_contract_reference(experiment, release)
    delivery_path, delivery = resolve_delivery_observation(
        experiment,
        release=release,
        compatibility_path=compatibility_delivery_observation,
    )
    from step5d_autotune_backend import Step5dV35Backend

    try:
        campaign_fingerprint = Step5dV35Backend(
            experiment
        ).freeze_fingerprint().composite_fingerprint
    except Exception as exc:
        raise BridgeAdmissionError(
            f"verified campaign fingerprint unavailable: {type(exc).__name__}:{exc}"
        ) from exc
    return validate_bridge_admission(
        experiment,
        {
        "schema": SCHEMA,
        "observed_at_unix_ns": time.time_ns(),
        "state": "BRIDGE_START_READY",
        "ok": True,
        "reason_code": "DELIVERY_VERIFIED",
        "release": {
            "manifest_sha256": release.manifest_sha256,
            "program_id": release.program_id,
        },
        "release_contract": release_contract,
        "delivery_observation": {
            "path": delivery_path.relative_to(experiment).as_posix(),
            "sha256": _sha256(delivery_path),
            "transaction_id": delivery["transaction_id"],
        },
        "authority_acquired": False,
        "attempt_created": False,
        "campaign_fingerprint": campaign_fingerprint,
        },
        release=release,
    )


__all__ = [
    "BridgeAdmissionError",
    "SCHEMA",
    "admission_index_path",
    "observe_bridge_admission",
    "release_contract_reference",
    "resolve_bridge_admission",
    "validate_bridge_admission",
    "write_indexed_bridge_admission",
]
