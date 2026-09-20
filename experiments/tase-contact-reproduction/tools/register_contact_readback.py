#!/usr/bin/env python3
"""Register a fresh contact-package controller read-back.

This is a software-only promotion step.  It never talks to the controller and
never changes the live-authority or motion fields.  The fresh delivery
manifest must already prove local/controller/read-back byte identity; the
current and stage-table pointers are then updated atomically while the prior
selection remains available as provenance.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = "step5d_contact_six_qp_v1"
CURRENT_REL = Path("config/current_stage.json")
TABLE_REL = Path("config/step5_stage_table.json")
DEFAULT_OLD_SELECTION = Path("report/yield-live-transition-v1/contact-readback-selection.json")
EXTENSIONS = (".script", ".txt", ".urp")


class RegistrationError(RuntimeError):
    """Raised when a fresh read-back cannot be registered safely."""


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistrationError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RegistrationError(f"JSON root must be an object: {path}")
    return value


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload_sha(payload: Mapping[str, Any]) -> str:
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise RegistrationError(f"artifact is outside experiment root: {path}") from exc


def _triplet_from_manifest(root: Path, manifest: Mapping[str, Any]) -> tuple[dict[str, str], Path]:
    validation = manifest.get("validation")
    if not isinstance(validation, Mapping) or validation.get("program") != PROGRAM:
        raise RegistrationError("fresh manifest validation does not bind the contact program")
    local_raw = manifest.get("local_dir")
    if not isinstance(local_raw, str) or not local_raw:
        raise RegistrationError("fresh manifest local_dir is missing")
    local_dir = (root / local_raw).resolve()
    try:
        local_dir.relative_to(root.resolve())
    except ValueError as exc:
        raise RegistrationError("fresh manifest local_dir escapes experiment root") from exc
    expected = manifest.get("sha256")
    if not isinstance(expected, Mapping):
        raise RegistrationError("fresh manifest sha256 object is missing")
    triplet: dict[str, str] = {}
    for label, extension in ((".script", ".script"), (".txt", ".txt"), (".urp", ".urp")):
        path = local_dir / f"{PROGRAM}{extension}"
        if not path.is_file():
            raise RegistrationError(f"local contact package is missing: {path}")
        actual = _sha(path)
        local_hash = expected.get("local", {}).get(label) if isinstance(expected.get("local"), Mapping) else None
        if actual != local_hash:
            raise RegistrationError(f"local package hash mismatch for {label}")
        triplet[label] = actual
    for side in ("controller", "readback"):
        side_hashes = expected.get(side)
        if not isinstance(side_hashes, Mapping) or dict(side_hashes) != triplet:
            raise RegistrationError(f"fresh manifest {side} hashes differ from local package")
    return triplet, local_dir


def _validate_manifest(root: Path, manifest_path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    manifest = _load(manifest_path)
    required = {
        "status": "controller read-back verified",
        "delivery_mode": "full_upload_readback",
        "fresh_controller_sha_verified": True,
        "readback_source": "fresh_controller_get",
    }
    for key, expected in required.items():
        if manifest.get(key) != expected:
            raise RegistrationError(f"fresh manifest {key!r} is not {expected!r}")
    triplet, _ = _triplet_from_manifest(root, manifest)
    target_resolution = manifest.get("target_resolution")
    if not isinstance(target_resolution, Mapping):
        raise RegistrationError("fresh manifest target resolution is missing")
    target = target_resolution.get("controller_target")
    if target != f"/programs/andyl/kunwei/step5/{PROGRAM}.urp":
        raise RegistrationError("fresh manifest controller target differs from current contact target")
    return triplet, manifest


def _selection_payload(
    *,
    root: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    triplet: Mapping[str, str],
    old_selection_path: Path,
) -> dict[str, Any]:
    old = _load(old_selection_path)
    old_sha = _sha(old_selection_path)
    checked_at = manifest.get("fresh_controller_checked_at")
    if not isinstance(checked_at, str) or not checked_at:
        raise RegistrationError("fresh controller check time is missing")
    return {
        "schema": "contact-readback-selection-v2",
        "status": "controller read-back verified",
        "program": PROGRAM,
        "source_delivery_manifest": _relative(root, manifest_path),
        "source_delivery_manifest_sha256": _sha(manifest_path),
        "readback_directory": _relative(root, manifest_path.parent),
        "readback_time_policy": "fresh controller GET captured at the recorded check time; this selection does not assert runtime freshness or task qualification",
        "sha256": {
            "local": dict(triplet),
            "controller": dict(triplet),
            "readback": dict(triplet),
        },
        "delivery_mode": manifest["delivery_mode"],
        "fresh_controller_sha_verified": True,
        "fresh_controller_checked_at": checked_at,
        "readback_source": manifest["readback_source"],
        "safety_boundary": list(manifest.get("safety_boundary") or []),
        "supersedes": {
            "path": _relative(root, old_selection_path),
            "sha256": old_sha,
            "prior_schema": old.get("schema"),
            "prior_sha256": old.get("sha256"),
        },
    }


def register(
    *,
    root: Path = ROOT,
    manifest_path: Path,
    selection_path: Path,
    old_selection_path: Path = DEFAULT_OLD_SELECTION,
) -> dict[str, Any]:
    root = root.resolve()
    manifest_path = manifest_path.resolve()
    selection_path = selection_path.resolve()
    old_selection_path = old_selection_path.resolve()
    triplet, manifest = _validate_manifest(root, manifest_path)
    if not old_selection_path.is_file():
        raise RegistrationError(f"prior selection is missing: {old_selection_path}")
    selection = _selection_payload(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        triplet=triplet,
        old_selection_path=old_selection_path,
    )
    current_path = root / CURRENT_REL
    table_path = root / TABLE_REL
    current = _load(current_path)
    table = _load(table_path)
    if current.get("program") != PROGRAM or current.get("current_stage_id") != PROGRAM:
        raise RegistrationError("current stage is not the contact-six-q-p program")
    old_pointer = current.get("controller_readback_manifest")
    if old_pointer != _relative(root, old_selection_path):
        raise RegistrationError("current selection changed; refusing compare-and-swap registration")
    row = next((item for item in table.get("stages", []) if isinstance(item, dict) and item.get("id") == PROGRAM), None)
    if row is None:
        raise RegistrationError("contact stage row is missing")
    binding = row.get("current_binding")
    if not isinstance(binding, dict) or binding.get("controller_readback_manifest") != old_pointer:
        raise RegistrationError("stage-table selection changed; refusing compare-and-swap registration")

    selection_rel = _relative(root, selection_path)
    manifest_hashes = {key: dict(triplet) for key in ("local", "controller", "readback")}
    selection_sha = _payload_sha(selection)
    previous_current = {
        "controller_readback_manifest": old_pointer,
        "sha256": current.get("sha256"),
    }
    current["controller_readback_manifest"] = selection_rel
    current["sha256"] = dict(triplet)
    current["status"] = "fresh_controller_readback_verified_pending_liveprep_admission"
    current["evidence"] = dict(current.get("evidence") or {})
    current["evidence"].update({
        "v1_controller_readback_verified": True,
        "v1_controller_readback_manifest": selection_rel,
        "fresh_contact_controller_delivery_manifest": _relative(root, manifest_path),
        "fresh_contact_controller_delivery_manifest_sha256": _sha(manifest_path),
        "fresh_contact_controller_checked_at": manifest["fresh_controller_checked_at"],
        "previous_contact_readback_selection": previous_current,
    })

    binding["controller_readback_manifest"] = selection_rel
    binding["controller_readback_status"] = "verified_current"
    delivery = row.setdefault("package_delivery", {})
    delivery.update({
        "controller_readback_manifest": selection_rel,
        "controller_readback_manifest_sha256": selection_sha,
        "controller_readback_verified": True,
        "controller_readback_status": "verified_current",
        "delivery_mode": manifest["delivery_mode"],
        "fresh_controller_sha_verified": True,
        "fresh_controller_checked_at": manifest["fresh_controller_checked_at"],
        "sha256": dict(triplet),
    })
    row["local_delivery_evidence"] = {
        "controller_readback_verified": True,
        "controller_readback_status": "verified_current",
        "sha256": dict(triplet),
        "source_delivery_manifest": _relative(root, manifest_path),
    }

    registration = {
        "schema": "contact-readback-registration-v1",
        "registered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "program": PROGRAM,
        "selection": selection_rel,
        "selection_sha256": selection_sha,
        "fresh_delivery_manifest": _relative(root, manifest_path),
        "fresh_delivery_manifest_sha256": _sha(manifest_path),
        "previous_selection": _relative(root, old_selection_path),
        "previous_selection_sha256": _sha(old_selection_path),
        "triplet_sha256": dict(triplet),
        "current_stage_updated": True,
        "stage_table_updated": True,
        "live_authority_changed": False,
        "motion_performed": False,
        "bridge_started": False,
        "claim_boundary": "fresh package identity only; no Home observation, resolver readiness, qualification, or figure-eight acceptance",
    }
    report_path = root / "report/yield-live-transition-v1/contact-readback-registration-20260921.json"
    _write_atomic(selection_path, selection)
    _write_atomic(current_path, current)
    _write_atomic(table_path, table)
    _write_atomic(report_path, registration)
    return registration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--old-selection", type=Path, default=DEFAULT_OLD_SELECTION)
    args = parser.parse_args(argv)
    result = register(
        root=args.root,
        manifest_path=args.manifest,
        selection_path=args.selection,
        old_selection_path=args.old_selection,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
