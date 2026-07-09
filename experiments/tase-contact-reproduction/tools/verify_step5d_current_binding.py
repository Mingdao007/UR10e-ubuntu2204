#!/usr/bin/env python3
"""Verify the selected Step5d live-prep package, read-back, and runtime binding."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from step5d_liveprep_readiness import (
    READINESS_SCHEMA,
    reviewed_source_sha256,
    validate_review_manifest,
)
from step5d_runtime_interface import resolve_runtime_interface
from verify_current_stage_readback import EXPERIMENT_ROOT, fail, load_json, verify


STEP5D_PACKAGE_PREFIXES = ("step5d_strict_rnn_liveprep_", "step5d_strict_rnn_ablation_")
STEP5D_ABLATION_V29 = "step5d_strict_rnn_ablation_v29"
V29_EXACT_RUNTIME_PROFILE: dict[str, Any] = {
    "backend": "cupy",
    "inner_iterations": 1024,
    "epsilon": 0.01,
    "sigr_exponent_r": 0.8,
    "qdot_cap_rad_s": 0.05,
    "control_mode": "speedj_rnn_live",
    "joint_layout_code": 524.0,
}


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _local_triplet_paths(root: Path, current: dict[str, Any], program: str) -> list[Path]:
    stem_value = current.get("local_triplet")
    if stem_value:
        stem = root / str(stem_value)
    else:
        stem = root / "programs" / "step5" / program
    return [stem.with_suffix(ext) for ext in (".script", ".txt", ".urp")]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_table_entry(root: Path, current: dict[str, Any], program: str) -> dict[str, Any]:
    table_rel = current.get("stage_table_path") or "config/step5_stage_table.json"
    table = load_json(root / str(table_rel))
    for row in table.get("stages", []):
        if row.get("id") == program:
            if row.get("active") is not True:
                fail(f"stage table row {program} is not active")
            return row
    fail(f"stage table row {program} is missing")


def verify_binding(root: Path, program: str | None = None, target_dir: str | None = None) -> dict[str, Any]:
    readback = verify(root, program, target_dir)
    current = load_json(root / "config" / "current_stage.json")
    selected = readback["program"]
    if not selected.startswith(STEP5D_PACKAGE_PREFIXES):
        fail(f"{selected} is not a Step5d TP package")

    interface = resolve_runtime_interface(program=selected, root=root)
    if current.get("controller_target") != interface.controller_target:
        fail(f"runtime interface target {interface.controller_target} differs from current_stage target {current.get('controller_target')}")
    if current.get("bridge_profile", {}).get("step4e_version") not in (None, selected):
        fail(f"bridge_profile.step4e_version is {current.get('bridge_profile', {}).get('step4e_version')}, expected {selected}")

    missing_triplet = [path for path in _local_triplet_paths(root, current, selected) if not path.exists()]
    if missing_triplet:
        fail("local triplet is incomplete: " + ", ".join(_relative(root, path) for path in missing_triplet))

    current_sha = current.get("sha256") or current.get("evidence", {}).get("sha256") or {}
    local_triplet = _local_triplet_paths(root, current, selected)
    for ext, path in zip((".script", ".txt", ".urp"), local_triplet):
        expected_sha = current_sha.get(ext)
        if not expected_sha:
            fail(f"current_stage sha256 for {ext} is missing")
        actual_sha = _sha256_file(path)
        if actual_sha != expected_sha:
            fail(
                "local triplet sha256 "
                f"{_relative(root, path)} is {actual_sha}, expected current_stage {ext} {expected_sha}"
            )

    stage_entry = _stage_table_entry(root, current, selected)

    return {
        "ok": True,
        "program": selected,
        "target_dir": readback["target_dir"],
        "manifest": readback["manifest"],
        "delivery_mode": readback["delivery_mode"],
        "runtime_interface": asdict(interface),
        "stage_table": {
            "path": current.get("stage_table_path") or "config/step5_stage_table.json",
            "id": stage_entry.get("id"),
            "active": stage_entry.get("active"),
            "blocked": stage_entry.get("blocked"),
        },
        "local_triplet": [_relative(root, path) for path in local_triplet],
    }


def _stage_bool(row: dict[str, Any], *keys: str) -> bool:
    containers = [row, row.get("acceptance"), row.get("contact_policy"), row.get("current_binding")]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            if container.get(key) is True:
                return True
    return False


def _confined_regular_file(root: Path, raw_path: Any, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        fail(f"{label} path is missing")
    unresolved = root / raw_path
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        fail(f"{label} path escapes experiment root")
    if unresolved.is_symlink() or not resolved.is_file():
        fail(f"{label} is not a regular in-root file: {raw_path}")
    return resolved


def _exact_v29_runtime_profile(
    *,
    stage25_control_mode: str | None,
    rnn_backend: str | None,
    rnn_inner_iterations: int | None,
    epsilon: float | None,
    sigr_exponent_r: float | None,
    qdot_cap_rad_s: float | None,
) -> dict[str, Any]:
    try:
        iteration_value = float(rnn_inner_iterations) if rnn_inner_iterations is not None else None
        if iteration_value is not None and (not iteration_value.is_integer() or not math.isfinite(iteration_value)):
            raise ValueError("inner iterations must be a finite integer")
        observed = {
            "backend": str(rnn_backend or ""),
            "inner_iterations": int(iteration_value) if iteration_value is not None else None,
            "epsilon": float(epsilon) if epsilon is not None else None,
            "sigr_exponent_r": float(sigr_exponent_r) if sigr_exponent_r is not None else None,
            "qdot_cap_rad_s": float(qdot_cap_rad_s) if qdot_cap_rad_s is not None else None,
            "control_mode": str(stage25_control_mode or ""),
            "joint_layout_code": 524.0,
        }
    except (TypeError, ValueError):
        fail("v29 live bridge requires the exact runtime profile")
    if observed != V29_EXACT_RUNTIME_PROFILE:
        fail(
            "v29 live bridge requires the exact runtime profile "
            "speedj_rnn_live/cupy/1024/epsilon=0.01/r=0.8/qdot=0.05"
        )
    return observed


def _verify_v29_readiness(
    root: Path,
    current: dict[str, Any],
    stage_entry: dict[str, Any],
    selected: str,
) -> dict[str, Any]:
    liveprep = current.get("liveprep_status")
    row_liveprep = stage_entry.get("liveprep_status")
    if not isinstance(liveprep, dict) or not isinstance(row_liveprep, dict):
        fail("v29 liveprep status is missing")
    if stage_entry.get("blocked") is True or liveprep.get("state") == "blocked":
        fail("v29 liveprep is blocked; package evidence remains valid but live authorization is refused")
    if liveprep.get("state") != "awaiting_live_authorization":
        fail("v29 liveprep state must be awaiting_live_authorization")
    if row_liveprep.get("state") != liveprep.get("state"):
        fail("v29 stage row liveprep status does not match current_stage")
    if row_liveprep.get("readiness_artifact") != liveprep.get("readiness_artifact"):
        fail("v29 stage row readiness artifact does not match current_stage")
    if stage_entry.get("runtime_profile") != V29_EXACT_RUNTIME_PROFILE:
        fail("v29 stage row does not bind the exact runtime profile")

    readiness_path = _confined_regular_file(root, liveprep.get("readiness_artifact"), "v29 readiness artifact")
    try:
        readiness = load_json(readiness_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("v29 readiness artifact is not valid JSON")
    if readiness.get("schema_version") != READINESS_SCHEMA:
        fail("v29 readiness artifact schema is invalid")
    if readiness.get("program") != selected or readiness.get("profile") != selected:
        fail("v29 readiness artifact is bound to a different program/profile")
    if readiness.get("workflow_state") != "awaiting_live_authorization":
        fail("v29 readiness workflow_state must be awaiting_live_authorization")
    if readiness.get("ready_for_explicit_live_authorization") is not True:
        fail("v29 readiness ready_for_explicit_live_authorization is not true")
    if readiness.get("blockers") != []:
        fail("v29 readiness blockers must be empty")
    if (
        readiness.get("runtime_profile_match") is not True
        or readiness.get("runtime_profile") != V29_EXACT_RUNTIME_PROFILE
    ):
        fail("v29 readiness artifact does not bind the exact runtime profile")

    expected_package_sha = current.get("sha256")
    if not isinstance(expected_package_sha, dict) or readiness.get("package_sha256") != expected_package_sha:
        fail("v29 readiness package hash binding does not match current_stage")
    if readiness.get("package_hashes_match") is not True or readiness.get("controller_readback_verified") is not True:
        fail("v29 readiness package/readback evidence is not accepted")

    readback_pointer = current.get("controller_readback_manifest")
    if readiness.get("controller_readback_manifest") != readback_pointer:
        fail("v29 readiness controller readback manifest binding does not match current_stage")
    readback_path = _confined_regular_file(root, readback_pointer, "v29 controller readback manifest")
    if readiness.get("controller_readback_manifest_sha256") != _sha256_file(readback_path):
        fail("v29 readiness controller readback manifest hash is stale")

    review = readiness.get("review")
    if not isinstance(review, dict):
        fail("v29 readiness milestone review is missing")
    checked_review = validate_review_manifest(review, manifest_dir=readiness_path.parent)
    if checked_review.get("ok") is not True:
        fail("v29 readiness milestone review is invalid")
    try:
        expected_source_sha = reviewed_source_sha256(root)
    except OSError:
        fail("v29 readiness reviewed source set is incomplete")
    if (
        review.get("reviewed_source_sha256") != expected_source_sha
        or review.get("expected_source_sha256") != expected_source_sha
    ):
        fail("v29 readiness reviewed source hash is stale")
    return {
        "path": _relative(root, readiness_path),
        "workflow_state": readiness["workflow_state"],
        "reviewed_source_sha256": expected_source_sha,
        "package_sha256": expected_package_sha,
        "controller_readback_manifest_sha256": readiness["controller_readback_manifest_sha256"],
    }


def verify_live_bridge_authorization(
    root: Path,
    program: str | None = None,
    stage25_control_mode: str | None = None,
    target_dir: str | None = None,
    *,
    rnn_backend: str | None = None,
    rnn_inner_iterations: int | None = None,
    epsilon: float | None = None,
    sigr_exponent_r: float | None = None,
    qdot_cap_rad_s: float | None = None,
) -> dict[str, Any]:
    binding = verify_binding(root, program, target_dir)
    current = load_json(root / "config" / "current_stage.json")
    selected = binding["program"]
    trigger = current.get("bridge_trigger")
    if not isinstance(trigger, dict):
        fail("current_stage bridge_trigger is missing")
    stage_entry = _stage_table_entry(root, current, selected)
    mode = str(stage25_control_mode or "")
    runtime_profile = None
    readiness = None
    if selected == STEP5D_ABLATION_V29:
        runtime_profile = _exact_v29_runtime_profile(
            stage25_control_mode=stage25_control_mode,
            rnn_backend=rnn_backend,
            rnn_inner_iterations=rnn_inner_iterations,
            epsilon=epsilon,
            sigr_exponent_r=sigr_exponent_r,
            qdot_cap_rad_s=qdot_cap_rad_s,
        )
        readiness = _verify_v29_readiness(root, current, stage_entry, selected)
    p0_required = _stage_bool(
        stage_entry,
        "strict_rnn_no_contact_p0_required_before_live",
        "no_contact_symbol_verification_required_before_live",
    )
    p0_passed = _stage_bool(
        stage_entry,
        "strict_rnn_no_contact_p0_passed",
        "strict_rnn_no_contact_p0_verified",
        "no_contact_symbol_verification_passed",
    )
    live_motion_authorized = trigger.get("live_motion_authorized") is True
    if not live_motion_authorized:
        fail("live motion is not authorized by current_stage.bridge_trigger.live_motion_authorized")
    if mode == "speedj_rnn_live":
        if p0_required and not p0_passed:
            fail("speedj_rnn_live requires no-contact P0 verification before live bridge authorization")

    return {
        "ok": True,
        "program": selected,
        "stage25_control_mode": mode,
        "live_motion_authorized": live_motion_authorized,
        "strict_rnn_no_contact_p0_required_before_live": p0_required,
        "strict_rnn_no_contact_p0_passed": p0_passed,
        "runtime_profile": runtime_profile,
        "readiness": readiness,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--program", default=None)
    parser.add_argument("--target-dir", default=None)
    parser.add_argument("--stage25-control-mode", default=None)
    parser.add_argument("--rnn-backend", default=None)
    parser.add_argument("--rnn-inner-iterations", type=int, default=None)
    parser.add_argument("--epsilon", type=float, default=None)
    parser.add_argument("--sigr-exponent-r", type=float, default=None)
    parser.add_argument("--qdot-cap-rad-s", type=float, default=None)
    parser.add_argument("--require-live-bridge-authorization", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.require_live_bridge_authorization:
        result = verify_live_bridge_authorization(
            args.root,
            args.program,
            args.stage25_control_mode,
            args.target_dir,
            rnn_backend=args.rnn_backend,
            rnn_inner_iterations=args.rnn_inner_iterations,
            epsilon=args.epsilon,
            sigr_exponent_r=args.sigr_exponent_r,
            qdot_cap_rad_s=args.qdot_cap_rad_s,
        )
    else:
        result = verify_binding(args.root, args.program, args.target_dir)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"[operator] Step5d current binding passed for {result['program']}: {result['manifest']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(24)
