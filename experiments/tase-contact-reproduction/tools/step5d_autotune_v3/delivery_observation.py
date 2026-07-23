"""Per-campaign controller upload and fresh-GET evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any, Mapping


SCHEMA = "step5d.autotune-v3/delivery-observation-v1"
INDEX_ROOT = Path("runs/step5d_autotune_v3/delivery-receipts")
_TRANSACTION = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRIPLET = frozenset({".script", ".txt", ".urp"})


class DeliveryObservationError(RuntimeError):
    pass


def _sha256(path: Path, role: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise DeliveryObservationError(f"{role} is missing or unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(root: Path, path: Path, role: str) -> str:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise DeliveryObservationError(f"{role} escapes experiment root") from exc
    if path.is_symlink():
        raise DeliveryObservationError(f"{role} must not be a symlink")
    return PurePosixPath(relative).as_posix()


def _load(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise DeliveryObservationError(f"{role} is missing or unsafe")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DeliveryObservationError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                DeliveryObservationError(
                    f"{role} contains non-finite {constant}"
                )
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeliveryObservationError(f"{role} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DeliveryObservationError(f"{role} must be a JSON object")
    return value


def _sha256_text(value: Any, role: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DeliveryObservationError(f"{role} must be a lowercase SHA-256")
    return value


def _triplet(value: Any, role: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _TRIPLET:
        raise DeliveryObservationError(f"{role} triplet fields differ")
    return {
        extension: _sha256_text(value[extension], f"{role} {extension}")
        for extension in sorted(_TRIPLET)
    }


def _timestamp(value: Any, role: str) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise DeliveryObservationError(f"{role} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DeliveryObservationError(f"{role} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeliveryObservationError(f"{role} timestamp lacks timezone")
    return value, parsed.astimezone(timezone.utc)


def fresh_get_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable controller-GET time and delivery transaction binding."""

    if value.get("schema") != SCHEMA:
        raise DeliveryObservationError("delivery observation schema differs")
    transaction_id = value.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or _TRANSACTION.fullmatch(transaction_id) is None
    ):
        raise DeliveryObservationError("delivery transaction ID is invalid")
    _checked_text, checked = _timestamp(
        value.get("fresh_controller_checked_at"), "delivery fresh-GET"
    )
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = checked - epoch
    observed_at_unix_ns = (
        delta.days * 86_400_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )
    if observed_at_unix_ns <= 0:
        raise DeliveryObservationError("delivery fresh-GET timestamp is invalid")
    return {
        "transaction_id": transaction_id,
        "observed_at_unix_ns": observed_at_unix_ns,
    }


def _receipt_identity(
    receipt: Mapping[str, Any],
    *,
    transaction_id: str,
    release: Any,
) -> tuple[dict[str, str], str, datetime]:
    hashes = receipt.get("sha256")
    validation = receipt.get("validation")
    if (
        not isinstance(transaction_id, str)
        or _TRANSACTION.fullmatch(transaction_id) is None
        or receipt.get("upload_transaction_id") != transaction_id
        or not isinstance(hashes, Mapping)
        or set(hashes) != {"local", "controller", "readback"}
        or not isinstance(validation, Mapping)
    ):
        raise DeliveryObservationError("delivery receipt identity closure differs")
    local = _triplet(hashes["local"], "delivery receipt local")
    controller = _triplet(hashes["controller"], "delivery receipt controller")
    readback = _triplet(hashes["readback"], "delivery receipt readback")
    release_triplet = _triplet(
        release.artifact_sha256, "release artifact"
    )
    controller_target = PurePosixPath(str(release.controller_target))
    validation_triplet = {
        extension: validation.get(field)
        for extension, field in {
            ".script": "script_sha256",
            ".txt": "txt_sha256",
            ".urp": "urp_sha256",
        }.items()
    }
    if (
        local != controller
        or local != readback
        or readback != release_triplet
        or validation_triplet != readback
        or receipt.get("status") != "controller read-back verified"
        or receipt.get("delivery_mode")
        not in {
            "full_upload_readback",
            "existing_program_fresh_readback",
        }
        or receipt.get("readback_source") != "fresh_controller_get"
        or receipt.get("fresh_controller_sha_verified") is not True
        or receipt.get("target_dir") != controller_target.parent.as_posix()
        or validation.get("program") != release.program_id
        or validation.get("target_dir") != controller_target.parent.as_posix()
        or validation.get("script_node_path")
        != controller_target.with_suffix(".script").as_posix()
    ):
        raise DeliveryObservationError("delivery receipt identity closure differs")
    checked_text, checked = _timestamp(
        receipt.get("fresh_controller_checked_at"), "delivery fresh-GET"
    )
    return readback, checked_text, checked


def build_delivery_observation(
    root: Path,
    *,
    receipt_path: Path,
    receipt_sha256: str,
    transaction_id: str,
    release: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    experiment = root.resolve(strict=True)
    receipt = _load(receipt_path, "delivery receipt")
    observed_sha = _sha256(receipt_path, "delivery receipt")
    if observed_sha != _sha256_text(
        receipt_sha256, "delivery receipt SHA-256"
    ):
        raise DeliveryObservationError("delivery receipt SHA-256 differs")
    triplet, checked_text, _checked = _receipt_identity(
        receipt,
        transaction_id=transaction_id,
        release=release,
    )
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None or observed_now.utcoffset() is None:
        raise DeliveryObservationError("delivery observation clock lacks timezone")
    row = {
        "schema": SCHEMA,
        "recorded_at_unix_ns": (
            time.time_ns()
            if now is None
            else int(observed_now.timestamp() * 1_000_000_000)
        ),
        "release_manifest_sha256": release.manifest_sha256,
        "program_id": release.program_id,
        "controller_target": release.controller_target,
        "transaction_id": transaction_id,
        "fresh_controller_checked_at": checked_text,
        "triplet_sha256": triplet,
        "receipt": {
            "path": _relative(experiment, receipt_path, "delivery receipt"),
            "sha256": receipt_sha256,
        },
    }
    return validate_delivery_observation(
        experiment,
        row,
        release=release,
    )


def validate_delivery_observation(
    root: Path,
    value: Mapping[str, Any],
    *,
    release: Any,
) -> dict[str, Any]:
    required = {
        "schema",
        "recorded_at_unix_ns",
        "release_manifest_sha256",
        "program_id",
        "controller_target",
        "transaction_id",
        "fresh_controller_checked_at",
        "triplet_sha256",
        "receipt",
    }
    row = dict(value)
    if set(row) != required or row["schema"] != SCHEMA:
        raise DeliveryObservationError("delivery observation fields or schema differ")
    if (
        isinstance(row["recorded_at_unix_ns"], bool)
        or not isinstance(row["recorded_at_unix_ns"], int)
        or row["recorded_at_unix_ns"] <= 0
    ):
        raise DeliveryObservationError("delivery observation timestamp is invalid")
    try:
        datetime.fromtimestamp(
            row["recorded_at_unix_ns"] / 1_000_000_000,
            tz=timezone.utc,
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise DeliveryObservationError(
            "delivery observation timestamp is invalid"
        ) from exc
    receipt_ref = row["receipt"]
    if not isinstance(receipt_ref, Mapping) or set(receipt_ref) != {"path", "sha256"}:
        raise DeliveryObservationError("delivery receipt reference differs")
    receipt_relative = receipt_ref["path"]
    if not isinstance(receipt_relative, str) or not receipt_relative:
        raise DeliveryObservationError("delivery receipt path is unsafe")
    relative = PurePosixPath(receipt_relative)
    if (
        relative.is_absolute()
        or relative.as_posix() != receipt_relative
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise DeliveryObservationError("delivery receipt path is unsafe")
    experiment = root.resolve(strict=True)
    unresolved_receipt = experiment / Path(*relative.parts)
    receipt_path = unresolved_receipt.resolve(strict=True)
    try:
        receipt_path.relative_to(experiment)
    except ValueError as exc:
        raise DeliveryObservationError("delivery receipt path is unsafe") from exc
    if unresolved_receipt.is_symlink():
        raise DeliveryObservationError("delivery receipt path is unsafe")
    receipt_sha256 = _sha256_text(
        receipt_ref["sha256"], "delivery receipt reference SHA-256"
    )
    if _sha256(receipt_path, "delivery receipt") != receipt_sha256:
        raise DeliveryObservationError("delivery receipt reference SHA-256 differs")
    triplet = _triplet(row["triplet_sha256"], "delivery observation")
    if (
        _sha256_text(
            row["release_manifest_sha256"], "release manifest SHA-256"
        )
        != release.manifest_sha256
        or row["program_id"] != release.program_id
        or row["controller_target"] != release.controller_target
        or triplet != _triplet(release.artifact_sha256, "release artifact")
        or not isinstance(row["transaction_id"], str)
        or _TRANSACTION.fullmatch(row["transaction_id"]) is None
    ):
        raise DeliveryObservationError("delivery observation release binding differs")
    checked_text, _checked = _timestamp(
        row["fresh_controller_checked_at"], "delivery fresh-GET"
    )
    receipt = _load(receipt_path, "delivery receipt")
    receipt_triplet, receipt_checked_text, _receipt_checked = _receipt_identity(
        receipt,
        transaction_id=row["transaction_id"],
        release=release,
    )
    if receipt_triplet != triplet or receipt_checked_text != checked_text:
        raise DeliveryObservationError("delivery receipt content differs")
    return row


def load_delivery_observation(
    root: Path,
    path: Path,
    *,
    release: Any,
) -> dict[str, Any]:
    return validate_delivery_observation(
        root,
        _load(path, "delivery observation"),
        release=release,
    )


def delivery_index_path(root: Path, value: Mapping[str, Any]) -> Path:
    """Return the immutable content-addressed index path for one observation."""

    release_sha256 = _sha256_text(
        value.get("release_manifest_sha256"),
        "delivery observation release SHA-256",
    )
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
        raise DeliveryObservationError(
            f"delivery observation is not canonical JSON: {exc}"
        ) from exc
    digest = hashlib.sha256(encoded).hexdigest()
    return (
        root.resolve(strict=True)
        / INDEX_ROOT
        / release_sha256
        / f"{digest}.json"
    )


def resolve_delivery_observation(
    root: Path,
    *,
    release: Any,
    compatibility_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Resolve one valid receipt from the current release without mutable mirrors."""

    experiment = root.resolve(strict=True)
    if compatibility_path is not None:
        unresolved = compatibility_path.expanduser()
        if unresolved.is_symlink():
            raise DeliveryObservationError(
                "compatibility delivery observation path is unsafe"
            )
        path = unresolved.resolve(strict=True)
        try:
            path.relative_to(experiment)
        except ValueError as exc:
            raise DeliveryObservationError(
                "compatibility delivery observation escapes experiment root"
            ) from exc
        return path, load_delivery_observation(
            experiment,
            path,
            release=release,
        )

    release_sha256 = _sha256_text(
        release.manifest_sha256,
        "current release manifest SHA-256",
    )
    index = experiment / INDEX_ROOT / release_sha256
    if index.is_symlink():
        raise DeliveryObservationError("delivery receipt index is unsafe")
    if not index.is_dir():
        raise DeliveryObservationError(
            "current release has no indexed delivery receipt"
        )
    candidates: list[tuple[int, str, Path, dict[str, Any]]] = []
    for path in sorted(index.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise DeliveryObservationError(
                "delivery receipt index contains an unsafe entry"
            )
        observed_sha256 = _sha256(path, "indexed delivery observation")
        if path.stem != observed_sha256:
            raise DeliveryObservationError(
                "indexed delivery observation filename differs from content"
            )
        row = load_delivery_observation(
            experiment,
            path,
            release=release,
        )
        candidates.append(
            (
                int(row["recorded_at_unix_ns"]),
                observed_sha256,
                path,
                row,
            )
        )
    if not candidates:
        raise DeliveryObservationError(
            "current release has no indexed delivery receipt"
        )
    _recorded_at, _digest, path, row = max(candidates)
    return path, row


__all__ = [
    "DeliveryObservationError",
    "SCHEMA",
    "build_delivery_observation",
    "fresh_get_provenance",
    "load_delivery_observation",
    "validate_delivery_observation",
]
