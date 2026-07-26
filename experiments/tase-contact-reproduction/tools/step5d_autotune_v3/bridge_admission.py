"""Read-only admission for the exact TP-local Step5d release."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from .atomic_io import AtomicIOError, atomic_bytes
from .dashboard import dashboard_exchange
from .delivery_observation import (
    load_delivery_observation,
    resolve_delivery_observation,
)
from .profile import ContractViolation, load_contract
from .release_certificate import certificate_path, load_release_certificate
from .release_contract import (
    release_contract_scope_for_release,
    validate_release_contract_result,
)
from .release_identity import (
    SAFETY_ENVELOPE_PATH,
    ReleaseIdentity,
    load_runtime_release,
    release_payload_path,
)
from .runtime_gate import (
    RuntimeGateError,
    loaded_program_matches,
    release_runtime_contract,
)
from .release_transition import (
    ReleaseTransitionError,
    resolve_publication_lineage,
)


SCHEMA = "step5d.autotune-v3/bridge-admission-v1"
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
        "checks",
        "program_state",
        "loaded_program",
        "expected_loaded_program",
        "operator_action",
        "release",
        "release_contract",
        "publication_lineage",
        "delivery_observation",
        "dashboard",
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
    if row.get("ok") is True:
        if (
            not isinstance(campaign_fingerprint, str)
            or len(campaign_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in campaign_fingerprint)
        ):
            raise BridgeAdmissionError("bridge admission campaign fingerprint differs")
    elif campaign_fingerprint is not None:
        raise BridgeAdmissionError("action-required admission must not carry a campaign fingerprint")
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
    try:
        lineage_path, _lineage = resolve_publication_lineage(
            root,
            release=release,
        )
    except ReleaseTransitionError as exc:
        raise BridgeAdmissionError(
            f"bridge admission publication lineage differs: {exc}"
        ) from exc
    expected_lineage = {
        "path": lineage_path.relative_to(root.resolve(strict=True)).as_posix(),
        "sha256": _sha256(lineage_path),
    }
    if row.get("publication_lineage") != expected_lineage:
        raise BridgeAdmissionError(
            "bridge admission publication lineage differs"
        )
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
    try:
        runtime_contract = release_runtime_contract(experiment, release)
        expected_program = str(runtime_contract["expected_loaded_program"])
    except (KeyError, TypeError, ValueError, RuntimeGateError) as exc:
        raise BridgeAdmissionError(
            f"bridge admission release contract differs: {exc}"
        ) from exc
    if row.get("expected_loaded_program") != expected_program:
        raise BridgeAdmissionError("bridge admission expected program differs")
    computed = compute_program_admission(
        {
            "programState": row.get("program_state"),
            "get loaded program": row.get("loaded_program"),
        },
        expected_program=expected_program,
    )
    for field in (
        "state",
        "ok",
        "reason_code",
        "checks",
        "operator_action",
    ):
        if row.get(field) != computed[field]:
            raise BridgeAdmissionError("bridge admission computed view differs")
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


def release_robot_host(root: Path, release: ReleaseIdentity) -> str:
    contract_path = release_payload_path(root, release, SAFETY_ENVELOPE_PATH)
    contract = load_contract(contract_path)
    try:
        robot_host = contract["effective_fields"]["runtime_identity"]["robot_host"]
    except (KeyError, TypeError) as exc:
        raise ContractViolation(
            "immutable release contract lacks robot_host"
        ) from exc
    if not isinstance(robot_host, str) or not robot_host or any(
        character in robot_host for character in ("\x00", "\r", "\n")
    ):
        raise ContractViolation("immutable release robot_host is unsafe")
    return robot_host


def compute_program_admission(
    dashboard: Mapping[str, Any],
    *,
    expected_program: str,
) -> dict[str, Any]:
    raw_state = str(
        dashboard.get("programState", dashboard.get("program_state", ""))
    )
    raw_loaded = str(
        dashboard.get("get loaded program", dashboard.get("loaded_program", ""))
    )
    state = raw_state.split(maxsplit=1)[0].upper() if raw_state else ""
    checks = {
        "exact_program_loaded": loaded_program_matches(
            raw_loaded,
            expected_program,
        ),
        "program_stopped": state == "STOPPED",
    }
    ready = all(checks.values())
    return {
        "state": "BENCH_READY" if ready else "ACTION_REQUIRED",
        "ok": ready,
        "reason_code": (
            "PROGRAM_LOADED_STOPPED"
            if ready
            else "EXTERNAL_ACTION_REQUIRED"
        ),
        "checks": checks,
        "program_state": raw_state,
        "loaded_program": raw_loaded,
        "expected_loaded_program": expected_program,
        "operator_action": (
            None
            if ready
            else "LOAD_EXACT_PROGRAM_ON_TP_AND_LEAVE_STOPPED"
        ),
    }


def observe_bridge_admission(
    root: Path,
    *,
    compatibility_delivery_observation: Path | None = None,
    dashboard_reader: Callable[..., Mapping[str, Any]] = dashboard_exchange,
    robot_host: str | None = None,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    experiment = root.resolve(strict=True)
    release = load_runtime_release(experiment)
    runtime_contract = release_runtime_contract(experiment, release)
    release_contract = release_contract_reference(experiment, release)
    try:
        lineage_path, _lineage = resolve_publication_lineage(
            experiment,
            release=release,
        )
    except ReleaseTransitionError as exc:
        raise BridgeAdmissionError(
            f"publication lineage gate failed: {exc}"
        ) from exc
    delivery_path, delivery = resolve_delivery_observation(
        experiment,
        release=release,
        compatibility_path=compatibility_delivery_observation,
    )
    host = robot_host or release_robot_host(experiment, release)
    try:
        dashboard = dashboard_reader(
            host,
            ["programState", "get loaded program"],
            timeout=timeout_s,
        )
    except Exception as exc:
        raise BridgeAdmissionError(
            f"Dashboard read-only admission failed: {type(exc).__name__}:{exc}"
        ) from exc
    if not isinstance(dashboard, Mapping):
        raise BridgeAdmissionError("Dashboard admission response is not an object")
    computed = compute_program_admission(
        dashboard,
        expected_program=str(runtime_contract["expected_loaded_program"]),
    )
    campaign_fingerprint = None
    if computed["ok"] is True:
        from step5d_autotune_backend import Step5dV35Backend

        try:
            campaign_fingerprint = Step5dV35Backend(experiment).freeze_fingerprint().composite_fingerprint
        except Exception as exc:
            raise BridgeAdmissionError(
                f"verified campaign fingerprint unavailable: {type(exc).__name__}:{exc}"
            ) from exc
    return validate_bridge_admission(
        experiment,
        {
        "schema": SCHEMA,
        "observed_at_unix_ns": time.time_ns(),
        **computed,
        "release": {
            "manifest_sha256": release.manifest_sha256,
            "program_id": release.program_id,
        },
        "release_contract": release_contract,
        "publication_lineage": {
            "path": lineage_path.relative_to(experiment).as_posix(),
            "sha256": _sha256(lineage_path),
        },
        "delivery_observation": {
            "path": delivery_path.relative_to(experiment).as_posix(),
            "sha256": _sha256(delivery_path),
            "transaction_id": delivery["transaction_id"],
        },
        "dashboard": {
            "host": host,
            "commands": ["programState", "get loaded program"],
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
    "compute_program_admission",
    "observe_bridge_admission",
    "release_contract_reference",
    "release_robot_host",
    "resolve_bridge_admission",
    "validate_bridge_admission",
    "write_indexed_bridge_admission",
]
