"""Read-only admission for the exact TP-local Step5d release."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from .dashboard import dashboard_exchange
from .delivery_observation import (
    load_delivery_observation,
    resolve_delivery_observation,
)
from .profile import ContractViolation, load_contract
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


SCHEMA = "step5d.autotune-v3/bridge-admission-v1"
INDEX_ROOT = Path("runs/step5d_autotune_v3/bridge-admissions")
ADMISSION_MAX_AGE_NS = 5_000_000_000
ADMISSION_FUTURE_SKEW_NS = 5_000_000


class BridgeAdmissionError(RuntimeError):
    """The read-only admission observation could not be completed safely."""


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise BridgeAdmissionError("delivery observation is missing or unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
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


def validate_bridge_admission(
    root: Path,
    value: Mapping[str, Any],
    *,
    release: ReleaseIdentity,
    now_ns: int | None = None,
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
        "delivery_observation",
        "dashboard",
        "authority_acquired",
        "attempt_created",
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


def resolve_bridge_admission(
    root: Path,
    *,
    release: ReleaseIdentity,
    now_ns: int | None = None,
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
        },
        release=release,
    )


__all__ = [
    "BridgeAdmissionError",
    "SCHEMA",
    "admission_index_path",
    "compute_program_admission",
    "observe_bridge_admission",
    "release_robot_host",
    "resolve_bridge_admission",
    "validate_bridge_admission",
]
