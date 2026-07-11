#!/usr/bin/env python3
"""Verify one manifest-bound P0 v8 2/10/60 second no-contact canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import verify_step5d_no_contact_p0 as legacy


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "step5d_strict_rnn_no_contact_p0_v8"
PHASES_S = (2.0, 10.0, 60.0)
PACKAGE_BASE = ROOT / "programs" / "step5" / "step5d" / PROFILE
POLICY_PATH = ROOT / "config" / "step5d_review_policy_v2.json"
CURRENT_PATH = ROOT / "config" / "current_stage.json"
CONSUMPTION_MIN_RATIO = 0.98
SOLVER_OK_STATUS = 40.0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def resolve_run_dir(path: Path) -> tuple[Path, Path]:
    if path.is_dir():
        return path, path / "bridge_rtde_500hz.csv"
    return path.parent, path


def phase_equal(left: object, right: float) -> bool:
    try:
        return math.isclose(float(left), right, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def required_prior_phases(phase_s: float) -> tuple[float, ...]:
    if phase_s == 2.0:
        return ()
    if phase_s == 10.0:
        return (2.0,)
    return (2.0, 10.0)


def validate_bindings(
    run_dir: Path,
    *,
    phase_s: float,
) -> tuple[list[str], dict[str, Any]]:
    blockers: list[str] = []
    details: dict[str, Any] = {}
    manifest_path = run_dir / "bridge_run_manifest.json"
    metadata_path = run_dir / "metadata.json"
    summary_path = run_dir / "summary.json"
    for path, blocker in (
        (manifest_path, "bridge_run_manifest_missing"),
        (metadata_path, "metadata_missing"),
        (summary_path, "summary_missing"),
    ):
        if not path.is_file():
            blockers.append(blocker)
    if blockers:
        return blockers, details

    manifest = read_json(manifest_path)
    metadata = read_json(metadata_path)
    summary = read_json(summary_path)
    canary = manifest.get("p0_v8_canary") or {}
    terminal = canary.get("terminal") or {}
    if manifest.get("profile") != PROFILE:
        blockers.append("manifest_profile_mismatch")
    if not phase_equal(canary.get("phase_s"), phase_s):
        blockers.append("manifest_canary_phase_mismatch")
    args = metadata.get("args") or {}
    if args.get("bridge_profile") != PROFILE:
        blockers.append("metadata_profile_mismatch")
    if not phase_equal(args.get("step5d_stop_register_canary_s"), phase_s):
        blockers.append("metadata_canary_phase_mismatch")
    if terminal.get("stop_request_sent") is not True:
        blockers.append("terminal_stop_request_not_sent")
    if terminal.get("tp_stop_acknowledged") is not True:
        blockers.append("terminal_tp_stop_not_acknowledged")
    if canary.get("summary_sha256") != sha256_file(summary_path):
        blockers.append("summary_hash_mismatch")
    summary_canary = summary.get("p0_v8_canary") or {}
    if not phase_equal(summary_canary.get("phase_s"), phase_s):
        blockers.append("summary_canary_phase_mismatch")

    package_sha = canary.get("package_sha256") or {}
    actual_package_sha = {
        ext: sha256_file(Path(f"{PACKAGE_BASE}{ext}"))
        for ext in (".script", ".txt", ".urp")
    }
    if package_sha != actual_package_sha:
        blockers.append("package_hash_mismatch")
    if canary.get("review_policy_sha256") != sha256_file(POLICY_PATH):
        blockers.append("review_policy_hash_mismatch")

    current = read_json(CURRENT_PATH)
    candidate = current.get("p0_v8_candidate") or {}
    capture = (current.get("bridge_trigger") or {}).get("no_contact_p0_v8_capture") or {}
    fingerprint = str(canary.get("composite_fingerprint") or "")
    if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        blockers.append("composite_fingerprint_invalid")
    if candidate.get("composite_fingerprint") != fingerprint:
        blockers.append("current_fingerprint_mismatch")
    if capture.get("sha256") != package_sha:
        blockers.append("current_package_hash_mismatch")
    for required in required_prior_phases(phase_s):
        if not any(
            isinstance(item, Mapping)
            and phase_equal(item.get("phase_s"), required)
            and item.get("composite_fingerprint") == fingerprint
            and item.get("canary_passed") is True
            for item in (canary.get("prior_canaries") or [])
        ):
            blockers.append(f"prior_{required:g}s_canary_missing_or_stale")

    details.update(
        {
            "manifest": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "metadata": str(metadata_path),
            "summary": str(summary_path),
            "composite_fingerprint": fingerprint,
            "package_sha256": actual_package_sha,
        }
    )
    return blockers, details


def validate_contract_rows(
    rows: list[dict[str, str]],
    *,
    phase_s: float,
) -> tuple[list[str], dict[str, Any]]:
    blockers: list[str] = []
    stage_rows = legacy.speedj_rnn_stage25_rows(rows)
    consumed_rows = [row for row in stage_rows if legacy.row_stage25_consumed(row)]
    first_consumed_index = next(
        (index for index, row in enumerate(stage_rows) if legacy.row_stage25_consumed(row)),
        None,
    )
    scored_rows = stage_rows[first_consumed_index:] if first_consumed_index is not None else []
    terminal_rows = [
        row
        for row in scored_rows
        if legacy.finite_int(row.get("_step5d_p0_v8_canary_stop_active")) == 1
    ]
    nonterminal_rows = [row for row in scored_rows if row not in terminal_rows]
    consumption_ratio = len(consumed_rows) / len(stage_rows) if stage_rows else 0.0
    if not stage_rows:
        blockers.append("p0_v8_stage25_rows_missing")
    if consumption_ratio < CONSUMPTION_MIN_RATIO:
        blockers.append("command_consumption_ratio_below_0p98")
    if not terminal_rows:
        blockers.append("terminal_canary_row_missing")

    rejected_rows = 0
    safe_hold_rows = 0
    solver_status_bad_rows = 0
    contract_missing_rows = 0
    dls_missing_rows = 0
    dls_fallback_rows = 0
    dls_sign_mismatch_rows = 0
    nonfinite_rows = 0
    for row in nonterminal_rows:
        if legacy.finite_int(row.get("_step5d_p0_rnn_accepted")) != 1:
            rejected_rows += 1
        if legacy.finite_int(row.get("_step5d_p0_safe_hold_active")) not in {0}:
            safe_hold_rows += 1
        if legacy.finite_float(row.get("_step5d_solver_status")) != SOLVER_OK_STATUS:
            solver_status_bad_rows += 1
        if legacy.finite_int(row.get("_step5d_p0_v8_contract_active")) != 1:
            contract_missing_rows += 1
        if legacy.finite_int(row.get("_step5d_dls_shadow_present")) != 1:
            dls_missing_rows += 1
        if legacy.finite_int(row.get("_step5d_dls_shadow_runtime_fallback_allowed")) != 0:
            dls_fallback_rows += 1
        if legacy.finite_int(row.get("_step5d_dls_shadow_normal_sign_difference")) != 0:
            dls_sign_mismatch_rows += 1
        required_finite = (
            "_step5d_constraint_residual_norm",
            "_step5d_qdot_max_abs_rad_s",
            "_step5d_p0_effective_ko",
            "_step5d_dls_shadow_residual_norm",
            "_step5d_dls_shadow_qdot_delta_norm",
            "_step5d_dls_shadow_twist_delta_norm",
        )
        if any(legacy.finite_float(row.get(field)) is None for field in required_finite):
            nonfinite_rows += 1
    if rejected_rows:
        blockers.append("p0_v8_rejected_rows_present")
    if safe_hold_rows:
        blockers.append("p0_v8_safe_hold_rows_present")
    if solver_status_bad_rows:
        blockers.append("p0_v8_solver_status_not_40")
    if contract_missing_rows:
        blockers.append("p0_v8_contract_evidence_missing")
    if dls_missing_rows:
        blockers.append("dls_shadow_missing")
    if dls_fallback_rows:
        blockers.append("dls_runtime_fallback_observed")
    if dls_sign_mismatch_rows:
        blockers.append("dls_shadow_normal_sign_mismatch")
    if nonfinite_rows:
        blockers.append("p0_v8_nonfinite_evidence_rows")

    terminal_zero_bad_rows = 0
    terminal_stop_bad_rows = 0
    for row in terminal_rows:
        command = [
            legacy.finite_float(row.get(field))
            for field in (
                "step4e_cmd_vx_m_s",
                "step4e_cmd_vy_m_s",
                "step4e_cmd_vz_m_s",
                "step4e_cmd_wx_rad_s",
                "step4e_cmd_wy_rad_s",
                "step4e_cmd_wz_rad_s",
            )
        ]
        if any(value is None or abs(value) > 1e-12 for value in command):
            terminal_zero_bad_rows += 1
        if (
            legacy.finite_float(row.get("stop_request")) is None
            or legacy.finite_float(row.get("stop_request")) <= 0.5  # type: ignore[operator]
            or legacy.finite_int(row.get("step4e_cmd_valid")) != 1
        ):
            terminal_stop_bad_rows += 1
    if terminal_zero_bad_rows:
        blockers.append("terminal_canary_qdot_not_zero")
    if terminal_stop_bad_rows:
        blockers.append("terminal_canary_stop_contract_invalid")

    return blockers, {
        "stage25_rows": len(stage_rows),
        "consumed_rows": len(consumed_rows),
        "consumption_ratio": consumption_ratio,
        "scored_rows": len(scored_rows),
        "terminal_rows": len(terminal_rows),
        "rejected_rows": rejected_rows,
        "safe_hold_rows": safe_hold_rows,
        "solver_status_bad_rows": solver_status_bad_rows,
        "dls_missing_rows": dls_missing_rows,
        "dls_fallback_rows": dls_fallback_rows,
        "dls_sign_mismatch_rows": dls_sign_mismatch_rows,
        "nonfinite_rows": nonfinite_rows,
        "phase_s": phase_s,
    }


def verify(path: Path, *, phase_s: float) -> dict[str, Any]:
    run_dir, csv_path = resolve_run_dir(path)
    if not csv_path.is_file():
        return {
            "ok": False,
            "blockers": ["bridge_rtde_csv_missing"],
            "p0_v8_passed": False,
            "canary_passed": False,
        }
    rows = legacy.read_rows(csv_path)
    base = legacy.verify_rows(
        rows,
        qdot_cap_rad_s=0.05,
        max_low_force_posture_effective_ko=0.010001,
        max_low_force_posture_gain_scale=0.002001,
        expected_low_force_posture_policy="weak_posture_v2",
        expected_low_force_posture_effective_ko=0.01,
        min_stage25_accepted_duration_s=phase_s,
    )
    binding_blockers, binding = validate_bindings(run_dir, phase_s=phase_s)
    contract_blockers, contract = validate_contract_rows(rows, phase_s=phase_s)
    blockers = list(dict.fromkeys([*base.get("blockers", []), *binding_blockers, *contract_blockers]))
    canary_passed = not blockers
    return {
        "schema_version": "step5d_no_contact_p0_v8_canary_verification_v1",
        "ok": canary_passed,
        "canary_passed": canary_passed,
        "p0_v8_passed": canary_passed and phase_s == 60.0,
        "phase_s": phase_s,
        "blockers": blockers,
        "binding": binding,
        "contract": contract,
        "base_verifier": base,
        "claim_boundary": {
            "no_contact_canary_accepted": canary_passed,
            "p0_v8_accepted": canary_passed and phase_s == 60.0,
            "contact_live_accepted": False,
            "reproduction_complete": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--phase-s", type=float, required=True, choices=PHASES_S)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = verify(args.run, phase_s=float(args.phase_s))
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["ok"] else 24


if __name__ == "__main__":
    raise SystemExit(main())
