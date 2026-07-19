#!/usr/bin/env python3
"""Replay v29 logged Step5d projections through the v30 offline gate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_DIR = EXPERIMENT_ROOT / "runs" / "imported_v29_20260710_104820"
STAGE_REGISTER = "ur_output_double_register_35"
EVIDENCE_FILES = (
    "summary.json",
    "stage_frequency_summary.json",
    "step5d_bridge_analysis.json",
    "bridge_run_manifest.json",
    "metadata.json",
    "bridge_rtde_500hz.csv",
)
REQUIRED_REPLAY_COLUMNS = (
    "_step5d_outer_xdot_limited_approach_normal_m_s",
    "_step5d_jqdot_raw_approach_normal_m_s",
    "_step5d_constraint_residual_norm",
    "_step5d_active_bounds_count",
    "_step4e_control_normal_b_x",
    "_step4e_control_normal_b_y",
    "_step4e_control_normal_b_z",
    "_step5d_force_sign_convention",
    *(f"_step5d_rnn_raw_qd{index}_rad_s" for index in range(6)),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_evidence_manifest(
    run_dir: Path,
    *,
    source_run: str,
    expected_csv_size_bytes: int | None = None,
    source_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    files: dict[str, Any] = {}
    missing: list[str] = []
    for name in EVIDENCE_FILES:
        path = run_dir / name
        if not path.is_file():
            missing.append(name)
            continue
        local_hash = _sha256(path)
        source_hash = None if source_hashes is None else source_hashes.get(name)
        files[name] = {
            "bytes": path.stat().st_size,
            "local_sha256": local_hash,
            "source_sha256": source_hash,
            "source_local_sha256_match": source_hash == local_hash if source_hash is not None else False,
        }
    csv_size = files.get("bridge_rtde_500hz.csv", {}).get("bytes")
    csv_complete = expected_csv_size_bytes is None or csv_size == expected_csv_size_bytes
    blockers = [f"missing:{name}" for name in missing]
    if not csv_complete:
        blockers.append("bridge_csv_size_mismatch_or_copy_in_progress")
    for name in EVIDENCE_FILES:
        if name not in files:
            continue
        if files[name]["source_sha256"] is None:
            blockers.append(f"missing_source_sha256:{name}")
        elif not files[name]["source_local_sha256_match"]:
            blockers.append(f"source_local_sha256_mismatch:{name}")
    return {
        "schema_version": "step5d_imported_evidence_v1",
        "source_run": source_run,
        "source_mode": "read_only_copy",
        "local_run": run_dir.relative_to(EXPERIMENT_ROOT).as_posix()
        if run_dir.is_relative_to(EXPERIMENT_ROOT)
        else str(run_dir),
        "allowlisted_files": list(EVIDENCE_FILES),
        "files": files,
        "expected_bridge_csv_size_bytes": expected_csv_size_bytes,
        "copy_complete": not blockers,
        "blockers": blockers,
        "safety_boundary": [
            "read-only source",
            "no Ubuntu runtime mutation",
            "no controller upload",
            "no bridge start",
            "no robot motion",
        ],
    }


def _finite(row: Mapping[str, Any], name: str) -> float | None:
    try:
        value = float(row.get(name, ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def load_logged_projection_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = sorted(set(REQUIRED_REPLAY_COLUMNS) - fields)
        if missing:
            raise RuntimeError(f"missing required replay columns: {', '.join(missing)}")
        rows = list(reader)
    if STAGE_REGISTER in fields:
        selected = []
        for row in rows:
            stage = _finite(row, STAGE_REGISTER)
            if stage is not None and math.isclose(stage, 25.0, abs_tol=1e-6):
                selected.append(row)
        return selected
    return rows


def replay_logged_projection_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    qdot_cap_rad_s: float = 0.05,
    max_tracking_error_m_s: float = 5e-4,
    max_residual_norm: float = 1e-3,
    required_acceptance_ratio: float = 0.99,
) -> dict[str, Any]:
    reason_counts: Counter[str] = Counter()
    legacy_migrations: Counter[str] = Counter()
    accepted = safe_hold = stop = nonfinite = over_bound = normal_mismatch = semantic_invalid = 0
    legacy_reason_rows = legacy_reason_migrated_rows = 0
    processed = 0
    for row in rows:
        processed += 1
        desired = _finite(row, "_step5d_outer_xdot_limited_approach_normal_m_s")
        predicted = _finite(row, "_step5d_jqdot_raw_approach_normal_m_s")
        residual = _finite(row, "_step5d_constraint_residual_norm")
        active_bounds = _finite(row, "_step5d_active_bounds_count")
        qdot = [_finite(row, f"_step5d_rnn_raw_qd{index}_rad_s") for index in range(6)]
        normal = [_finite(row, f"_step4e_control_normal_b_{axis}") for axis in "xyz"]
        normal_norm = (
            math.sqrt(sum(float(value) ** 2 for value in normal if value is not None))
            if all(value is not None for value in normal)
            else math.nan
        )
        sign_convention = str(row.get("_step5d_force_sign_convention") or "")
        if any(value is None for value in (desired, predicted, residual, active_bounds, *qdot, *normal)):
            reason = "nonfinite_or_missing_replay_evidence"
            nonfinite += 1
            stop += 1
        elif sign_convention != "step5_step6_positive_normal_load" or not math.isclose(normal_norm, 1.0, abs_tol=1e-3):
            reason = "frame_or_normal_contract_invalid"
            semantic_invalid += 1
            stop += 1
        elif max(abs(float(value)) for value in qdot if value is not None) > qdot_cap_rad_s + 1e-12:
            reason = "qdot_bound_exceeded"
            over_bound += 1
            stop += 1
        elif desired <= 0.0:
            reason = "outer_approach_not_pressing"
            safe_hold += 1
        elif predicted <= 0.0:
            reason = "approach_normal_unload_mismatch"
            normal_mismatch += 1
            safe_hold += 1
        elif abs(predicted - desired) > max_tracking_error_m_s:
            reason = "approach_normal_tracking_error"
            safe_hold += 1
        elif residual > max_residual_norm:
            reason = "constraint_residual_norm_exceeded"
            safe_hold += 1
        elif active_bounds > 0.0:
            reason = "active_bounds_present"
            safe_hold += 1
        else:
            reason = "ok"
            accepted += 1
        legacy_reason = str(row.get("_step5d_rnn_reject_reason") or "").strip()
        if legacy_reason:
            legacy_reason_rows += 1
            if legacy_reason != reason:
                legacy_reason_migrated_rows += 1
                legacy_migrations[f"{legacy_reason}->{reason}"] += 1
        reason_counts[reason] += 1
    ratio = accepted / processed if processed else 0.0
    acceptance_pass = (
        processed > 0
        and ratio >= required_acceptance_ratio
        and nonfinite == 0
        and over_bound == 0
        and normal_mismatch == 0
        and semantic_invalid == 0
    )
    blockers: list[str] = []
    if not processed:
        blockers.append("no_stage25_rows")
    if ratio < required_acceptance_ratio:
        blockers.append("accepted_ratio_below_0p99")
    if normal_mismatch:
        blockers.append("normal_direction_mismatch_present")
    if nonfinite:
        blockers.append("nonfinite_or_missing_output_present")
    if over_bound:
        blockers.append("qdot_bound_violation_present")
    if semantic_invalid:
        blockers.append("frame_or_normal_contract_invalid")
    return {
        "schema_version": "step5d_v30_logged_projection_replay_v1",
        "profile": "cupy/1024/epsilon=0.010/r=0.8/qdot_cap=0.05",
        "replay_mode": "logged_projection_evidence_no_command_output",
        "candidate_scope": "raw strict-RNN candidate before direction-preserving slew; final command must be rechecked by SafetyEnvelope",
        "normal_contract": "n_reaction = -n_approach; compare desired and Jqdot in one declared frame",
        "legacy_label_migration_rule": (
            "ignore the legacy reject label when it contradicts finite canonical logged "
            "outer_approach and Jqdot_raw_approach projections; recompute the unchanged "
            "fail-closed guard from those projections"
        ),
        "rows": processed,
        "accepted_rows": accepted,
        "safe_hold_rows": safe_hold,
        "stop_rows": stop,
        "accepted_ratio": ratio,
        "required_accepted_ratio": required_acceptance_ratio,
        "normal_mismatch_rows": normal_mismatch,
        "semantic_contract_invalid_rows": semantic_invalid,
        "nonfinite_rows": nonfinite,
        "qdot_over_bound_rows": over_bound,
        "reason_counts": dict(sorted(reason_counts.items())),
        "legacy_reason_rows": legacy_reason_rows,
        "legacy_reason_migrated_rows": legacy_reason_migrated_rows,
        "legacy_label_migrations": dict(sorted(legacy_migrations.items())),
        "acceptance_pass": acceptance_pass,
        "blockers": blockers,
        "safety_boundary": [
            "offline replay only",
            "DLS is not a runtime fallback",
            "no RTDE write",
            "no bridge start",
            "no motion authorization",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--source-run", default="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/bridge_step5d_strict_rnn_ablation_v29_20260710_104820")
    parser.add_argument("--expected-csv-size", type=int, default=43_700_470)
    parser.add_argument("--source-hashes-json", type=Path)
    parser.add_argument("--replay-csv", type=Path)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument("--replay-output", type=Path)
    args = parser.parse_args()
    source_hashes = None
    if args.source_hashes_json:
        source_hashes = json.loads(args.source_hashes_json.read_text(encoding="utf-8"))
        if isinstance(source_hashes, dict) and isinstance(source_hashes.get("sha256"), dict):
            source_hashes = source_hashes["sha256"]
    manifest = build_evidence_manifest(
        args.run_dir,
        source_run=args.source_run,
        expected_csv_size_bytes=args.expected_csv_size,
        source_hashes=source_hashes,
    )
    replay_csv = args.replay_csv or (
        args.run_dir / "bridge_rtde_500hz_rnn_replay_slice.csv"
        if (args.run_dir / "bridge_rtde_500hz_rnn_replay_slice.csv").is_file()
        else args.run_dir / "bridge_rtde_500hz.csv"
    )
    replay = replay_logged_projection_rows(load_logged_projection_rows(replay_csv))
    replay["input_csv"] = str(replay_csv)
    replay["input_csv_sha256"] = _sha256(replay_csv)
    source_sidecar = replay_csv.with_suffix(".source.json")
    if source_sidecar.is_file():
        replay["source_sidecar"] = str(source_sidecar)
        replay["source_sidecar_sha256"] = _sha256(source_sidecar)
        replay["source_sidecar_payload"] = json.loads(source_sidecar.read_text(encoding="utf-8"))
    result = {"evidence_manifest": manifest, "replay": replay}
    if args.manifest_output:
        args.manifest_output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.replay_output:
        args.replay_output.write_text(json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if manifest["copy_complete"] and replay["acceptance_pass"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
