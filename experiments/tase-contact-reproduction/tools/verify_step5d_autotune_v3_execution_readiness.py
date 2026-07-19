#!/usr/bin/env python3
"""Resolve the one legal Step5d Autotune V3 live action fail-closed."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v3.profile import (
    ContractViolation,
    contract_sha256,
    control_fingerprint,
    load_contract,
)
from step5d_autotune_v3.state import StateError, orchestration_fingerprint


ROOT = Path(__file__).resolve().parents[1]
V1_STAGE_ID = "step5d_strict_rnn_autotune_v1"
V3_STAGE_ID = "step5d_strict_rnn_autotune_v3"
VALIDATION_SCOPE = "deterministic_live_entry_prerequisites"
MACHINE_BINDING = "machine_generated_epoch_and_process_fingerprint"
HISTORICAL_VALIDATION_IDENTITY = {
    "contract_sha256": "96a83126c1bf4b11ed41429060e9b83c4b867f799dea337478c69bcf84ad57f0",
    "control_fingerprint": "12a494fc44fc238a0623e951b1ae328c22cd18a3b966d79692c9c2b032ed80ea",
    "orchestration_fingerprint": "8fdeb1b435bbafeff84877a354d41830f6ccfa7d7033d80401a3ae559cb29e94",
}
HISTORICAL_VALIDATION_DECISION = {
    "go_no_go": "go",
    "acceptance_scope": VALIDATION_SCOPE,
    "current_selector": V1_STAGE_ID,
    "v3_active": False,
    "user_authorization_required": False,
    "one_play_real_motion": True,
    "execution_readiness": "ready_for_v3_live_continuous_campaign",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReadinessError(RuntimeError):
    """The persisted release state cannot support a truthful operator signal."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReadinessError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, *, role: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReadinessError(f"{role} is missing or unsafe")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ReadinessError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"{role} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReadinessError(f"{role} must be an object")
    return payload


def _require(actual: Any, expected: Any, role: str) -> None:
    if actual != expected:
        raise ReadinessError(
            f"{role} differs: expected={expected!r}, observed={actual!r}"
        )


def _zoned_timestamp(value: Any, *, role: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReadinessError(f"{role} is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReadinessError(f"{role} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReadinessError(f"{role} lacks an explicit timezone")
    return value


def _v3_row(table: Mapping[str, Any]) -> Mapping[str, Any]:
    rows = [
        row
        for row in table.get("stages", [])
        if isinstance(row, dict) and row.get("id") == V3_STAGE_ID
    ]
    if len(rows) != 1:
        raise ReadinessError("v3 stage row must exist exactly once")
    return rows[0]


def _reference(root: Path, value: Any, *, role: str) -> tuple[Path, str]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ReadinessError(f"{role} reference fields differ")
    relative = value.get("path")
    expected_sha = value.get("sha256")
    if (
        not isinstance(relative, str)
        or not isinstance(expected_sha, str)
        or _SHA256.fullmatch(expected_sha) is None
    ):
        raise ReadinessError(f"{role} reference is invalid")
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ReadinessError(f"{role} is missing or unsafe")
    _require(_sha256(path), expected_sha, f"{role} digest")
    return path, expected_sha


def _verify_validation(
    root: Path,
    *,
    current_identity: Mapping[str, str],
    readback_relative: str,
    readback_sha256: str,
) -> tuple[Path, Mapping[str, Any]]:
    path = root / "config/step5/step5d_autotune_v3_offline_validation.json"
    validation = _load_json(path, role="deterministic validation")
    _require(
        validation.get("schema"),
        "step5d.autotune-v3/deterministic-validation-v2",
        "validation schema",
    )
    _zoned_timestamp(validation.get("observed_at"), role="validation timestamp")
    _require(
        validation.get("identity"),
        HISTORICAL_VALIDATION_IDENTITY,
        "historical validation identity",
    )
    _require(
        validation.get("decision"),
        HISTORICAL_VALIDATION_DECISION,
        "historical validation decision",
    )
    if validation.get("identity") == current_identity:
        raise ReadinessError(
            "historical validation must not promote the current candidate identity"
        )

    gates = validation.get("gates") or {}
    for name in (
        "repository_validation",
        "control_semantics",
        "ten_trial_v3",
        "package_and_readback",
        "cadence_soak",
    ):
        _require((gates.get(name) or {}).get("status"), "pass", f"validation gate {name}")
    ten_trial = gates.get("ten_trial_v3") or {}
    _require(ten_trial.get("batch_size"), 10, "V3 validation batch size")
    _require(ten_trial.get("async_compact_capture"), True, "async compact capture gate")
    _require(
        ten_trial.get("pareto_windows_s"),
        {"round_a": [5, 60], "round_b": [0, 60]},
        "Pareto evaluation windows",
    )
    package_gate = gates.get("package_and_readback") or {}
    _require(package_gate.get("controller_readback"), readback_relative, "validation readback path")
    _require(package_gate.get("controller_readback_sha256"), readback_sha256, "validation readback digest")
    cadence = gates.get("cadence_soak") or {}
    try:
        cadence_bounds_ok = (
            float(cadence.get("paced_elapsed_s")) >= 124.9
            and int(cadence.get("samples")) >= 62_000
            and float(cadence.get("compute_p99_ms")) <= 2.0
            and float(cadence.get("row_gap_max_ms")) <= 20.0
        )
    except (TypeError, ValueError):
        cadence_bounds_ok = False
    _require(cadence_bounds_ok, True, "cadence soak numeric bounds")
    for key, expected in (
        ("row_gap_over_20ms_count", 0),
        ("row_gap_45_to_60ms_count", 0),
        ("scheduler_restored_to_other", True),
    ):
        _require(cadence.get(key), expected, f"cadence soak {key}")

    return path, validation


def _verify_live_promotion(
    root: Path,
    *,
    current_identity: Mapping[str, str],
    validation_path: Path,
    readback_relative: str,
    readback_sha256: str,
) -> None:
    promotion = _load_json(
        root / "config/step5/step5d_autotune_v3_live_promotion.json",
        role="V3 live promotion",
    )
    required = {
        "schema",
        "candidate_stage_id",
        "control_profile_id",
        "current_selector",
        "identity",
        "deterministic_validation",
        "controller_readback",
        "machine_campaign_binding",
        "same_process_startup_gate",
        "user_authorization_required",
        "live_runtime_promoted",
        "blocker",
    }
    if set(promotion) != required:
        raise ReadinessError("V3 live promotion fields differ")
    for key, expected in (
        ("schema", "step5d.autotune-v3/live-promotion-v2"),
        ("candidate_stage_id", V3_STAGE_ID),
        ("control_profile_id", V1_STAGE_ID),
        ("current_selector", V1_STAGE_ID),
        ("identity", current_identity),
        ("machine_campaign_binding", MACHINE_BINDING),
        ("same_process_startup_gate", True),
        ("user_authorization_required", True),
        ("live_runtime_promoted", False),
        ("blocker", "requires_attended_tp_upload_readback_and_certified_stopping_bound"),
    ):
        _require(promotion.get(key), expected, f"live promotion {key}")
    referenced_validation, _ = _reference(
        root, promotion.get("deterministic_validation"), role="deterministic validation"
    )
    _require(referenced_validation, validation_path, "live promotion validation path")
    referenced_readback, referenced_readback_sha = _reference(
        root, promotion.get("controller_readback"), role="controller readback"
    )
    _require(
        str(referenced_readback.relative_to(root)),
        readback_relative,
        "live promotion readback path",
    )
    _require(referenced_readback_sha, readback_sha256, "live promotion readback digest")


def verify(root: Path = ROOT, *, require_live: bool = False) -> dict[str, Any]:
    """Verify direct-live readiness; ``require_live`` remains API-compatible."""

    root = root.expanduser().resolve(strict=True)
    try:
        contract = load_contract(
            root / "config/step5/step5d_autotune_v3_control_contract.json"
        )
        current_identity = {
            "contract_sha256": contract_sha256(contract),
            "control_fingerprint": control_fingerprint(contract),
            "orchestration_fingerprint": orchestration_fingerprint(root),
        }
    except (ContractViolation, StateError) as exc:
        raise ReadinessError(f"current source fingerprint failed: {exc}") from exc

    current = _load_json(root / "config/current_stage.json", role="current selector")
    _require(current.get("current_stage_id"), V1_STAGE_ID, "rollback selector")
    _require(current.get("program"), V1_STAGE_ID, "rollback program")

    table = _load_json(root / "config/step5_stage_table.json", role="stage table")
    v3 = _v3_row(table)
    for field, expected in (("active", False), ("blocked", True), ("bridge", False)):
        _require(v3.get(field), expected, f"v3 {field}")

    package = v3.get("package_delivery") or {}
    _require(package.get("status"), "requires_attended_tp_upload_readback", "package status")
    _require(package.get("controller_uploaded_by_v3"), False, "V3 upload claim")
    _require(package.get("controller_readback_verified"), False, "V3 readback claim")
    program = package.get("program_basename")
    local_triplet = package.get("local_triplet")
    if not isinstance(program, str) or not isinstance(local_triplet, str):
        raise ReadinessError("package identity is missing")
    triplet = package.get("sha256") or {}
    if set(triplet) != {".script", ".txt", ".urp"}:
        raise ReadinessError("package triplet digest set differs")
    for extension, expected_sha in triplet.items():
        if not isinstance(expected_sha, str) or _SHA256.fullmatch(expected_sha) is None:
            raise ReadinessError(f"package digest is invalid: {extension}")
        path = root / f"{local_triplet}{extension}"
        if path.is_symlink() or not path.is_file():
            raise ReadinessError(f"local package file is missing or unsafe: {extension}")
        _require(_sha256(path), expected_sha, f"local package digest {extension}")

    basis = root / f"{local_triplet}.deploy-manifest.json"
    _require(_sha256(basis), package.get("tp_fingerprint"), "TP basis fingerprint")
    readback_relative = package.get("controller_readback_manifest")
    readback_sha = package.get("controller_readback_manifest_sha256")
    if not isinstance(readback_relative, str) or not isinstance(readback_sha, str):
        raise ReadinessError("controller readback binding is missing")
    readback_path = root / readback_relative
    _require(_sha256(readback_path), readback_sha, "controller readback manifest digest")
    readback = _load_json(readback_path, role="controller readback manifest")
    _require(readback.get("verified"), True, "controller readback verification")
    _require(readback.get("program"), program, "controller readback program")
    _require(
        readback.get("tp_fingerprint"),
        contract["deployment_tp_identity"]["tp_fingerprint"],
        "controller TP fingerprint",
    )
    _require(readback.get("triplet_sha256"), contract["tp_artifact_sha256"], "controller triplet digests")
    readback_at = _zoned_timestamp(
        readback.get("fresh_controller_checked_at"), role="readback timestamp"
    )
    _require(package.get("fresh_controller_sha_at"), readback_at, "fresh controller timestamp")

    validation_path, _validation = _verify_validation(
        root,
        current_identity=current_identity,
        readback_relative=readback_relative,
        readback_sha256=readback_sha,
    )
    offline = v3.get("offline_validation") or {}
    _require(offline.get("report"), str(validation_path.relative_to(root)), "validation report")
    _require(_sha256(validation_path), offline.get("report_sha256"), "validation report digest")

    readiness = v3.get("execution_readiness") or {}
    for key, expected in (
        ("schema", "step5d.autotune-v3/execution-readiness-v2"),
        ("state", "requires_attended_tp_upload_readback"),
        ("public_success_signal", "requires_attended_tp_upload_readback"),
        ("deterministic_validation_complete", True),
        ("package_delivery_complete", False),
        ("live_runtime_promoted", False),
        ("same_process_startup_gate_complete", False),
        ("ready_to_execute", False),
        ("ready_to_start_bridge", False),
        ("ready_for_contact_or_motion", False),
    ):
        _require(readiness.get(key), expected, f"readiness {key}")
    trigger = readiness.get("operator_trigger") or {}
    for key, expected in (
        ("candidate_stage_id", V3_STAGE_ID),
        ("user_confirmation_required", True),
        ("user_authorization_required", True),
        ("internal_launch_binding", MACHINE_BINDING),
        ("tp_action", "attended_upload_readback_required"),
        ("play_effect", "forbidden_in_offline_tranche"),
    ):
        _require(trigger.get(key), expected, f"operator trigger {key}")

    _verify_live_promotion(
        root,
        current_identity=current_identity,
        validation_path=validation_path,
        readback_relative=readback_relative,
        readback_sha256=readback_sha,
    )
    if require_live:
        raise ReadinessError(
            "requires_attended_tp_upload_readback_and_certified_stopping_bound"
        )
    return {
        "schema": "step5d.autotune-v3/execution-readiness-report-v2",
        "ok": True,
        "candidate_stage_id": V3_STAGE_ID,
        "current_stage_id": V1_STAGE_ID,
        "state": "requires_attended_tp_upload_readback",
        "public_success_signal": "requires_attended_tp_upload_readback",
        "ready_to_execute": False,
        "package_delivery": "requires_attended_tp_upload_readback",
        "controller_readback_at": readback_at,
        "controller_target": package.get("controller_target"),
        "identity": current_identity,
        "next_owner": "attended_tp_owner",
        "next_legal_action": "attended TP upload/readback, then certify stopping bound",
        "canonical_gate": [],
        "user_authorization_required": True,
        "hil_hold_required": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = verify(args.root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"step5d_v3_readiness={report['public_success_signal']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
