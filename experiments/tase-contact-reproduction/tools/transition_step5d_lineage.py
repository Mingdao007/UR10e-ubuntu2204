#!/usr/bin/env python3
"""Canonical two-level V3/V4 selector transition; never edits the V3 pointer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SELECTOR = ROOT / "config/step5d/lineage_selector.json"
EOAT_CONTEXT = ROOT / "config/eoat_context.json"
SCHEMA = "step5d.lineage-selector/v1"
EVIDENCE_SCHEMA = "step5d.autotune-v4/activation-evidence-v1"
EXPECTED_V3_POINTER_SHA = (
    "646edefaf6fbbd76b0cbfe86bc00b5d8ac26b787231f9ac345425256bbc78f5e"
)


class LineageTransitionError(RuntimeError):
    """The canonical lineage transition is not ready or is inconsistent."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LineageTransitionError(f"{role} must be a lowercase SHA-256")
    return value


def _receipt_sha256(receipt: dict[str, Any]) -> str:
    material = {
        "profile_id": receipt.get("profile_id"),
        "profile_sha256": receipt.get("profile_sha256"),
        "readback_sha256": receipt.get("readback_sha256"),
        "payload_set": receipt.get("payload_set"),
        "tcp_set": receipt.get("tcp_set"),
        "fresh_get_verified": receipt.get("fresh_get_verified"),
        "stationary_verified": receipt.get("stationary_verified"),
        "safety_normal_verified": receipt.get("safety_normal_verified"),
        "single_writer_verified": receipt.get("single_writer_verified"),
    }
    return hashlib.sha256(
        json.dumps(
            material, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _load(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LineageTransitionError(f"{role} must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LineageTransitionError(f"{role} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise LineageTransitionError(f"{role} must be an object")
    return value


def _resolved(path_text: Any, role: str) -> Path:
    if not isinstance(path_text, str) or not path_text:
        raise LineageTransitionError(f"{role} path is invalid")
    path = Path(path_text)
    return path if path.is_absolute() else ROOT / path


def _hash_bound(
    payload: dict[str, Any], path_key: str, sha_key: str, role: str
) -> Path:
    path = _resolved(payload.get(path_key), role)
    expected = payload.get(sha_key)
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or path.is_symlink()
        or not path.is_file()
        or _sha(path) != expected
    ):
        raise LineageTransitionError(f"{role} SHA binding differs")
    return path


_EOAT_RECEIPT_FLAGS = (
    "payload_set",
    "tcp_set",
    "fresh_get_verified",
    "stationary_verified",
    "safety_normal_verified",
    "single_writer_verified",
)


def _eoat_receipt_blockers(
    receipt: Any,
    *,
    expected_profile_id: Any,
    expected_profile_sha256: Any,
    role: str,
) -> list[str]:
    prefix = f"{role}_eoat"
    if not isinstance(receipt, dict):
        return [f"{prefix}_fresh_apply_verify_receipt_missing"]
    blockers: list[str] = []
    if receipt.get("profile_id") != expected_profile_id:
        blockers.append(f"{prefix}_receipt_profile_id_mismatch")
    if receipt.get("profile_sha256") != expected_profile_sha256:
        blockers.append(f"{prefix}_receipt_profile_sha256_mismatch")
    for field in ("readback_sha256", "receipt_sha256"):
        try:
            _digest(receipt.get(field), f"{prefix} {field}")
        except LineageTransitionError:
            blockers.append(f"{prefix}_{field}_invalid")
    if "passed" in receipt and receipt.get("passed") is not True:
        blockers.append(f"{prefix}_receipt_not_passed")
    if not blockers:
        try:
            receipt_hash_matches = receipt.get("receipt_sha256") == _receipt_sha256(
                receipt
            )
        except (TypeError, ValueError, OverflowError):
            receipt_hash_matches = False
        if not receipt_hash_matches:
            blockers.append(f"{prefix}_receipt_hash_mismatch")
    blockers.extend(
        f"{prefix}_{field}_missing"
        for field in _EOAT_RECEIPT_FLAGS
        if receipt.get(field) is not True
    )
    return blockers


def _active_eoat_resolution(
    active: dict[str, Any], *, role: str
) -> tuple[dict[str, Any], Path | None]:
    blockers: list[str] = []
    profile_path: Path | None = None
    profile_id: Any = None
    try:
        profile_path = _hash_bound(
            active,
            "eoat_contract",
            "eoat_contract_sha256",
            f"{role} EOAT profile",
        )
        profile_id = _load(profile_path, f"{role} EOAT profile").get("profile_id")
    except LineageTransitionError as exc:
        blockers.append(f"{role}_eoat_profile_binding_invalid")
        blockers.append(str(exc))
    if profile_path is not None:
        blockers.extend(
            _eoat_receipt_blockers(
                active.get("fresh_apply_and_verify_receipt"),
                expected_profile_id=profile_id,
                expected_profile_sha256=active.get("eoat_contract_sha256"),
                role=role,
            )
        )
    if active.get("live_compatible_now") is not True:
        blockers.append(f"{role}_eoat_live_compatibility_not_verified")
    return (
        {
            "role": role,
            "eoat_mode": active.get("eoat_mode"),
            "eoat_contract": active.get("eoat_contract"),
            "eoat_contract_sha256": active.get("eoat_contract_sha256"),
            "fresh_apply_and_verify_receipt": active.get(
                "fresh_apply_and_verify_receipt"
            ),
            "live_compatible_now": active.get("live_compatible_now") is True
            and not blockers,
            "blockers": blockers,
        },
        profile_path,
    )


def resolve_active_eoat() -> dict[str, Any]:
    """Resolve the selected EOAT and fail closed without touching a controller."""
    selector = _load(SELECTOR, "lineage selector")
    if selector.get("schema") != SCHEMA:
        raise LineageTransitionError("lineage selector schema differs")
    active = selector.get("active")
    if not isinstance(active, dict):
        raise LineageTransitionError("active lineage selector is missing")
    resolution, profile_path = _active_eoat_resolution(active, role="active")
    return {
        "schema": "step5d.lineage-selector/active-eoat-resolution-v1",
        "lineage": active.get("lineage"),
        "program": active.get("program"),
        "eoat_mode": resolution["eoat_mode"],
        "eoat_contract": resolution["eoat_contract"],
        "eoat_contract_sha256": resolution["eoat_contract_sha256"],
        "profile_exists_and_hash_bound": profile_path is not None,
        "compatible": resolution["live_compatible_now"],
        "blockers": resolution["blockers"],
    }


def inspect_transition(evidence_path: Path | None = None) -> dict[str, Any]:
    selector = _load(SELECTOR, "lineage selector")
    if selector.get("schema") != SCHEMA:
        raise LineageTransitionError("lineage selector schema differs")
    active = selector.get("active")
    staged = selector.get("staged")
    if not isinstance(active, dict) or not isinstance(staged, dict):
        raise LineageTransitionError("lineage selector sections are missing")
    if (
        active.get("lineage") != "step5d_strict_rnn_autotune_v3"
        or active.get("program") != "step5d_strict_rnn_autotune_v3_r034"
        or active.get("current_pointer_sha256") != EXPECTED_V3_POINTER_SHA
    ):
        raise LineageTransitionError("active V3 selector binding differs")
    v3_pointer = _hash_bound(
        active,
        "current_pointer",
        "current_pointer_sha256",
        "V3 current pointer",
    )
    active_eoat, _ = _active_eoat_resolution(active, role="active_v3")
    _hash_bound(
        staged,
        "release_contract",
        "release_contract_sha256",
        "V4 release contract",
    )
    _hash_bound(
        staged,
        "eoat_contract",
        "eoat_contract_sha256",
        "V4 EOAT contract",
    )
    # Active-lineage EOAT compatibility is a live-run gate for that lineage,
    # not a prerequisite for switching to a separately hash-bound lineage.
    # In particular, fitting the new EOAT must block V3/r034 while still
    # allowing a fully evidenced V4 activation.
    blockers: list[str] = []
    if staged.get("offline_closure") is None:
        blockers.append("v4_offline_closure_missing")
    else:
        _hash_bound(
            staged,
            "offline_closure",
            "offline_closure_sha256",
            "V4 offline closure",
        )
    evidence: dict[str, Any] | None = None
    if evidence_path is None:
        blockers.append("v4_activation_evidence_missing")
    else:
        evidence = _load(evidence_path, "V4 activation evidence")
        if evidence.get("schema") != EVIDENCE_SCHEMA:
            blockers.append("v4_activation_evidence_schema_invalid")
        if evidence.get("campaign_fingerprint") != staged.get(
            "campaign_fingerprint"
        ) and "campaign_fingerprint" in staged:
            blockers.append("v4_campaign_fingerprint_mismatch")
        required_booleans = (
            "r006_controller_readback_verified",
            "r006_live_success",
            "three_5n_baseline_successes",
            "formal_review_v3_passed",
            "v4_triplet_controller_readback_verified",
            "fresh_owner_route_gates",
        )
        blockers.extend(
            f"{field}_missing"
            for field in required_booleans
            if evidence.get(field) is not True
        )
        receipts = evidence.get("baseline_success_receipts")
        valid_receipts = bool(
            isinstance(receipts, list)
            and len(receipts) == 3
            and all(
                isinstance(receipt, dict)
                and receipt.get("terminal_stage") == 22
                and receipt.get("campaign_fingerprint")
                == staged.get("campaign_fingerprint")
                and receipt.get("eoat_sha256")
                == staged.get("eoat_contract_sha256")
                and receipt.get("target_force_n") == 5
                and receipt.get("sensor_authority") == "kunwei_only"
                and isinstance(receipt.get("completion_sha256"), str)
                and len(receipt["completion_sha256"]) == 64
                for receipt in receipts
            )
        )
        if not valid_receipts:
            blockers.append("three_baseline_success_receipts_missing")
        v4_eoat_receipt = evidence.get("v4_eoat_apply_verify_receipt")
        try:
            staged_profile = _load(
                _resolved(staged["eoat_contract"], "V4 EOAT profile"),
                "V4 EOAT profile",
            )
            blockers.extend(
                _eoat_receipt_blockers(
                    v4_eoat_receipt,
                    expected_profile_id=staged_profile.get("profile_id"),
                    expected_profile_sha256=staged.get("eoat_contract_sha256"),
                    role="v4",
                )
            )
        except LineageTransitionError as exc:
            blockers.append(f"v4_eoat_profile_binding_invalid:{exc}")
        v4_pointer = evidence.get("v4_release_pointer")
        if not isinstance(v4_pointer, dict):
            blockers.append("v4_release_pointer_missing")
        else:
            try:
                _hash_bound(
                    v4_pointer,
                    "path",
                    "sha256",
                    "V4 release pointer",
                )
            except LineageTransitionError as exc:
                blockers.append(str(exc))
    return {
        "schema": "step5d.lineage-selector/transition-inspection-v1",
        "ready": not blockers,
        "active_lineage": active["lineage"],
        "staged_lineage": staged.get("lineage"),
        "v3_pointer": str(v3_pointer),
        "v3_pointer_sha256": _sha(v3_pointer),
        "v3_pointer_will_be_modified": False,
        "active_eoat": active_eoat,
        "blockers": blockers,
        "selector": selector,
        "evidence": evidence,
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def activate(evidence_path: Path) -> dict[str, Any]:
    inspection = inspect_transition(evidence_path)
    if not inspection["ready"]:
        raise LineageTransitionError(
            "V4 transition remains blocked: " + ",".join(inspection["blockers"])
        )
    selector = inspection["selector"]
    staged = selector["staged"]
    evidence = inspection["evidence"]
    assert isinstance(evidence, dict)
    v4_pointer = evidence["v4_release_pointer"]
    activated_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
    new_selector = {
        **selector,
        "active": {
            "lineage": staged["lineage"],
            "program": staged["program"],
            "current_pointer": v4_pointer["path"],
            "current_pointer_sha256": v4_pointer["sha256"],
            "eoat_mode": "new_eoat_v4",
            "eoat_contract": staged["eoat_contract"],
            "eoat_contract_sha256": staged["eoat_contract_sha256"],
            "fresh_apply_and_verify_receipt": evidence[
                "v4_eoat_apply_verify_receipt"
            ],
            "live_compatible_now": True,
            "block_reason": "",
            "activated_at": activated_at,
        },
        "staged": {
            "lineage": "step5d_strict_rnn_autotune_v3",
            "program": "step5d_strict_rnn_autotune_v3_r034",
            "current_pointer": "config/step5d/current.json",
            "current_pointer_sha256": EXPECTED_V3_POINTER_SHA,
            "status": "preserved_old_eoat_lineage_not_active",
        },
    }
    eoat_context = {
        "schema": "ur10e.eoat-context/v1",
        "active_contract": staged["eoat_contract"],
        "active_contract_sha256": staged["eoat_contract_sha256"],
        "active_lineage": staged["lineage"],
        "active_program": staged["program"],
        "activated_at": activated_at,
        "claim_boundary": [
            "Any current package without the active EOAT SHA binding is machine-blocked.",
            "The preserved V3 pointer remains unchanged and can be selected only through a later canonical old-EOAT transition.",
        ],
    }
    if _sha(_resolved(selector["active"]["current_pointer"], "V3 pointer")) != EXPECTED_V3_POINTER_SHA:
        raise LineageTransitionError("V3 pointer changed before V4 selector commit")
    _atomic_json(EOAT_CONTEXT, eoat_context)
    _atomic_json(SELECTOR, new_selector)
    if _sha(ROOT / "config/step5d/current.json") != EXPECTED_V3_POINTER_SHA:
        raise LineageTransitionError("V3 pointer changed during V4 selector commit")
    return {
        "activated": True,
        "active_lineage": staged["lineage"],
        "active_program": staged["program"],
        "v3_pointer_unchanged": True,
        "selector_sha256": _sha(SELECTOR),
        "eoat_context_sha256": _sha(EOAT_CONTEXT),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "activate"))
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    if args.action == "inspect":
        result = inspect_transition(args.evidence)
    else:
        if args.evidence is None:
            parser.error("activate requires --evidence")
        result = activate(args.evidence)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ready", result.get("activated", False)) else 2


if __name__ == "__main__":
    raise SystemExit(main())
