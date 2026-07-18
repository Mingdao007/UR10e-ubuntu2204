#!/usr/bin/env python3
"""Resolve the next legal Step5d Autotune v3 operator action fail-closed."""

from __future__ import annotations

import argparse
import ast
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
OFFLINE_SCOPE = "offline_tooling_and_ursim_hold_only"
OFFLINE_BLOCKER = "offline_only_live_start_disabled"
READY_FOR_HIL = "ready_for_hil_authorization"
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


def _offline_blocker(path: Path) -> str | None:
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ReadinessError(f"offline service source is unavailable: {exc}") from exc
    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "OFFLINE_BLOCKER":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
            return None
    return None


def _v3_row(table: Mapping[str, Any]) -> Mapping[str, Any]:
    rows = [
        row
        for row in table.get("stages", [])
        if isinstance(row, dict) and row.get("id") == V3_STAGE_ID
    ]
    if len(rows) != 1:
        raise ReadinessError("v3 stage row must exist exactly once")
    return rows[0]


def verify(root: Path = ROOT) -> dict[str, Any]:
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
    _require(current.get("current_stage_id"), V1_STAGE_ID, "current selector")
    _require(current.get("program"), V1_STAGE_ID, "current program")

    table = _load_json(root / "config/step5_stage_table.json", role="stage table")
    v3 = _v3_row(table)
    for field, expected in (("active", False), ("blocked", True), ("bridge", False)):
        _require(v3.get(field), expected, f"v3 {field}")

    package = v3.get("package_delivery") or {}
    _require(
        package.get("status"),
        "controller_readback_verified_content_addressed_reuse_inactive",
        "package delivery status",
    )
    _require(package.get("controller_uploaded_by_v3"), False, "v3 upload claim")
    program = package.get("program_basename")
    if not isinstance(program, str) or not program:
        raise ReadinessError("package program basename is missing")
    triplet = package.get("sha256") or {}
    if set(triplet) != {".script", ".txt", ".urp"}:
        raise ReadinessError("package triplet digest set differs")
    for extension, expected in triplet.items():
        if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
            raise ReadinessError(f"package digest is invalid: {extension}")
        local = root / f"{package.get('local_triplet')}{extension}"
        if local.is_symlink() or not local.is_file():
            raise ReadinessError(f"local package file is missing or unsafe: {extension}")
        _require(_sha256(local), expected, f"local package digest {extension}")

    reuse = package.get("content_addressed_reuse") or {}
    _require(reuse.get("accepted"), True, "content-addressed reuse acceptance")
    _require(reuse.get("same_bytes_verified"), True, "content-addressed byte identity")
    basis_relative = reuse.get("basis_manifest")
    readback_relative = reuse.get("prior_readback_source")
    if not isinstance(basis_relative, str) or not isinstance(readback_relative, str):
        raise ReadinessError("content-addressed reuse evidence paths are missing")
    basis = root / basis_relative
    readback_path = root / readback_relative
    if basis.is_symlink() or not basis.is_file():
        raise ReadinessError("content-addressed basis manifest is missing or unsafe")
    if readback_path.is_symlink() or not readback_path.is_file():
        raise ReadinessError("controller readback manifest is missing or unsafe")
    _require(_sha256(basis), package.get("tp_fingerprint"), "TP basis fingerprint")
    _require(
        _sha256(readback_path),
        package.get("controller_readback_manifest_sha256"),
        "controller readback manifest digest",
    )
    _require(
        package.get("controller_readback_manifest"),
        readback_relative,
        "controller readback source binding",
    )

    readback = _load_json(readback_path, role="controller readback manifest")
    _require(readback.get("verified"), True, "controller readback verification")
    _require(readback.get("program"), program, "controller readback program")
    _require(
        readback.get("tp_fingerprint"),
        package.get("tp_fingerprint"),
        "controller TP fingerprint",
    )
    _require(readback.get("triplet_sha256"), triplet, "controller triplet digests")
    readback_at = _zoned_timestamp(readback.get("readback_at"), role="readback timestamp")
    _require(
        reuse.get("fresh_controller_sha_at"),
        readback_at,
        "content-addressed fresh controller timestamp",
    )

    validation_path = root / "config/step5/step5d_autotune_v3_offline_validation.json"
    validation = _load_json(validation_path, role="offline validation")
    offline = v3.get("offline_validation") or {}
    _require(offline.get("report"), str(validation_path.relative_to(root)), "offline report")
    _require(_sha256(validation_path), offline.get("report_sha256"), "offline report digest")
    decision = validation.get("decision") or {}
    validation_identity = validation.get("identity") or {}
    for field, expected in current_identity.items():
        _require(validation_identity.get(field), expected, f"current identity {field}")
    _require(decision.get("go_no_go"), "go", "offline decision")
    _require(decision.get("acceptance_scope"), OFFLINE_SCOPE, "offline scope")
    _require(decision.get("rollout_authorized"), False, "rollout authorization")
    _require(decision.get("current_selector"), V1_STAGE_ID, "offline selector")
    _require(decision.get("v3_active"), False, "offline v3 activity")
    gates = validation.get("gates") or {}
    _require((gates.get("hosted_offline_release") or {}).get("status"), "pass", "offline lane")
    _require((gates.get("ursim_hold_only") or {}).get("status"), "pass", "URSim lane")
    hil = gates.get("hil_no_motion") or {}
    _require(hil.get("status"), "blocked_not_authorized", "HIL state")
    _require(hil.get("controller_touched"), False, "HIL controller boundary")

    binding = v3.get("current_binding") or {}
    _require(binding.get("is_current"), False, "v3 current binding")
    _require(binding.get("live_authorized"), False, "v3 live authorization")
    readiness = v3.get("execution_readiness") or {}
    _require(readiness.get("schema"), "step5d.autotune-v3/execution-readiness-v1", "readiness schema")
    _require(readiness.get("state"), READY_FOR_HIL, "readiness state")
    _require(readiness.get("public_success_signal"), READY_FOR_HIL, "public success signal")
    _require(readiness.get("next_owner"), "ur10e-live-bench", "readiness next owner")
    _require(readiness.get("offline_acceptance_complete"), True, "offline readiness")
    _require(readiness.get("package_delivery_complete"), True, "package readiness")
    for field in (
        "hil_no_motion_complete",
        "candidate_current",
        "candidate_live_authorized",
        "live_runtime_promoted",
        "same_process_startup_gate_complete",
        "ready_to_execute",
        "ready_to_load_play",
        "ready_to_start_bridge",
        "ready_to_arm",
        "ready_for_contact_or_motion",
    ):
        _require(readiness.get(field), False, f"readiness {field}")
    authorization = readiness.get("authorization") or {}
    _require(
        authorization.get("candidate_stage_id"), V3_STAGE_ID, "authorization candidate"
    )
    _require(authorization.get("status"), "not_authorized", "authorization status")
    _require(authorization.get("source"), "none", "authorization source")
    _require(
        authorization.get("historical_live_authorization_reused"),
        False,
        "historical live authorization reuse",
    )
    _require(
        authorization.get("scope"),
        "candidate_scoped_current_turn_only",
        "authorization scope",
    )

    service = root / "tools/step5d_autotune_v3/service.py"
    _require(_offline_blocker(service), OFFLINE_BLOCKER, "offline live-start blocker")

    return {
        "schema": "step5d.autotune-v3/execution-readiness-report-v1",
        "ok": True,
        "candidate_stage_id": V3_STAGE_ID,
        "current_stage_id": V1_STAGE_ID,
        "state": READY_FOR_HIL,
        "public_success_signal": READY_FOR_HIL,
        "ready_to_execute": False,
        "package_delivery": "controller_readback_verified_content_addressed_reuse",
        "controller_readback_at": readback_at,
        "controller_target": package.get("controller_target"),
        "identity": current_identity,
        "next_owner": "ur10e-live-bench",
        "next_legal_action": (
            "obtain a new current-turn candidate-scoped authorization for the "
            "serialized HIL HOLD-only gate"
        ),
        "forbidden_without_later_gates": [
            "load_play",
            "bridge_start",
            "arm",
            "zero_tare",
            "contact",
            "motion",
        ],
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
