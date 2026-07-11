#!/usr/bin/env python3
"""Promote a controller-readback Step5d TP package to current_stage."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from verify_step5d_current_binding import verify_v30_evidence_freeze


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = (".script", ".txt", ".urp")
STEP5D_ARCHIVE_DIR = Path("programs/step5/step5d")
STEP5D_CURRENT_DIR = Path("programs/step5")
TARGET_DIR = "/programs/andyl/kunwei/step5"
STEP5D_PACKAGE_PREFIXES = ("step5d_strict_rnn_liveprep_", "step5d_strict_rnn_ablation_")
STEP5D_ABLATION_V25 = "step5d_strict_rnn_ablation_v25"
STEP5D_ABLATION_V26 = "step5d_strict_rnn_ablation_v26"
STEP5D_ABLATION_V27 = "step5d_strict_rnn_ablation_v27"
STEP5D_ABLATION_V28 = "step5d_strict_rnn_ablation_v28"
STEP5D_ABLATION_V29 = "step5d_strict_rnn_ablation_v29"
STEP5D_ABLATION_V30 = "step5d_strict_rnn_ablation_v30"
STEP5D_ABLATION_PROGRAMS = {
    STEP5D_ABLATION_V25,
    STEP5D_ABLATION_V26,
    STEP5D_ABLATION_V27,
    STEP5D_ABLATION_V28,
    STEP5D_ABLATION_V29,
}
STEP5D_STEP5B_SPEEDL_LIVE_PROGRAMS = {
    STEP5D_ABLATION_V27,
    STEP5D_ABLATION_V28,
    STEP5D_ABLATION_V29,
}
STEP5D_OPERATOR_PLAY_WAIT_S = 20


def fail(message: str) -> None:
    raise RuntimeError(message)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"missing JSON file: {path}")
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON file {path}: {exc}")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root))


def local_triplet(root: Path, local_dir: Path, program: str) -> dict[str, Path]:
    files = {ext: local_dir / f"{program}{ext}" for ext in EXTENSIONS}
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        fail(f"missing local triplet file(s): {missing}")
    return files


def triplet_sha(files: dict[str, Path]) -> dict[str, str]:
    return {ext: sha256_file(path) for ext, path in files.items()}


def latest_manifest(root: Path, program: str) -> Path:
    matches = sorted(
        (root / "runs").glob(f"controller_readback_{program}_*/manifest.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        fail(f"no controller read-back manifest found for {program}")
    return matches[0]


def validate_manifest(root: Path, program: str, target_dir: str, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if manifest.get("status") != "controller read-back verified":
        fail(f"manifest status is not controller read-back verified: {manifest.get('status')}")
    if manifest.get("target_dir") != target_dir:
        fail(f"manifest target_dir is {manifest.get('target_dir')}, expected {target_dir}")
    validation = manifest.get("validation", {})
    if validation.get("program") != program:
        fail(f"manifest program is {validation.get('program')}, expected {program}")
    if validation.get("target_dir") != target_dir:
        fail(f"manifest validation target_dir is {validation.get('target_dir')}, expected {target_dir}")
    expected_script = str(PurePosixPath(target_dir) / f"{program}.script")
    if validation.get("script_node_path") != expected_script:
        fail(f"manifest script_node_path is {validation.get('script_node_path')}, expected {expected_script}")
    manifest_sha = manifest.get("sha256", {})
    for section in ("local", "controller", "readback"):
        for ext in EXTENSIONS:
            if not manifest_sha.get(section, {}).get(ext):
                fail(f"manifest sha256 {section} {ext} is missing")
    if not (
        manifest_sha["local"] == manifest_sha["controller"] == manifest_sha["readback"]
    ):
        fail("manifest local/controller/readback sha256 values do not all match")
    readback_dir = manifest_path.parent
    for ext in EXTENSIONS:
        readback_file = readback_dir / f"{program}{ext}"
        if not readback_file.is_file():
            fail(f"read-back file is missing: {readback_file}")
        actual = sha256_file(readback_file)
        if actual != manifest_sha["readback"][ext]:
            fail(f"read-back {ext} sha256 is {actual}, expected {manifest_sha['readback'][ext]}")
    manifest["manifest_path"] = rel(root, manifest_path)
    return manifest


def ensure_formal_local_triplet(
    root: Path,
    program: str,
    source_dir: Path,
    expected_sha: dict[str, str],
) -> dict[str, Path]:
    formal_dir = root / STEP5D_CURRENT_DIR
    formal_dir.mkdir(parents=True, exist_ok=True)
    source_files = local_triplet(root, source_dir, program)
    for ext, source in source_files.items():
        if sha256_file(source) != expected_sha[ext]:
            fail(f"source local {ext} sha does not match manifest")
        dest = formal_dir / source.name
        if dest.exists() and sha256_file(dest) == expected_sha[ext]:
            continue
        shutil.copy2(source, dest)
    formal_files = local_triplet(root, formal_dir, program)
    formal_sha = triplet_sha(formal_files)
    if formal_sha != expected_sha:
        fail(f"formal local triplet sha mismatch: {formal_sha} expected {expected_sha}")
    return formal_files


def archive_previous_triplet(root: Path, program: str, current: dict[str, Any]) -> str:
    local_triplet_value = current.get("local_triplet") or str(STEP5D_CURRENT_DIR / program)
    source_stem = root / str(local_triplet_value)
    archive_dir = root / STEP5D_ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_stem = archive_dir / program
    for ext in EXTENSIONS:
        source = source_stem.with_suffix(ext)
        dest = archived_stem.with_suffix(ext)
        if dest.exists():
            if source.exists():
                if sha256_file(source) != sha256_file(dest):
                    fail(f"archive destination already exists with different bytes: {dest}")
                source.unlink()
            continue
        if source.exists():
            shutil.move(str(source), str(dest))
    return str(STEP5D_ARCHIVE_DIR / f"{program}.{{script,txt,urp}}")


def version_label(program: str) -> str:
    for prefix in STEP5D_PACKAGE_PREFIXES:
        if program.startswith(prefix):
            return program[len(prefix):]
    fail(f"program is not a Step5d package id: {program}")


def live_attempt_evidence(root: Path, program: str) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    run_dirs = {
        *list((root / "runs").glob(f"bridge_step4e_line_outerloop_{program}_*")),
        *list((root / "runs").glob(f"bridge_{program}_*")),
    }
    for run_dir in sorted(run_dirs):
        summary_path = run_dir / "summary.json"
        entry: dict[str, Any] = {"run_dir": rel(root, run_dir)}
        if summary_path.is_file():
            summary = load_json(summary_path)
            entry["summary"] = rel(root, summary_path)
            if "stop_reason" in summary:
                entry["stop_reason"] = summary["stop_reason"]
            if "stage25_ft_line_control" in summary:
                entry["stage25_ft_line_control"] = summary["stage25_ft_line_control"]
        attempts.append(entry)
    latest = attempts[-1] if attempts else {}
    label = version_label(program)
    if label == "v21":
        root_cause = (
            "The v21 TP package was controller read-back verified, but its live run exposed "
            "a Stage25.3->25.0 register-layout hazard: preload parameters in input registers "
            "40/41/42/44/46/47, tagged by 47=521, could still be echoed when Stage25.0 first "
            "consumed 37..42 as qdot. It is superseded by v22's Stage25.95 qdot-clear barrier."
        )
    elif label == "v20":
        root_cause = (
            "The v20 TP package was controller read-back verified, but live attempts did not "
            "complete successfully before it was superseded."
        )
    elif label == "v22":
        root_cause = (
            "The v22 TP package was controller read-back verified and added Stage25.95, "
            "but live attempts still hit normal_force_guard during strict RNN Stage25. "
            "It is superseded by v23's near-zero qdot-clear check and post-RNN normal-direction guard."
        )
    elif label == "v23":
        root_cause = (
            "The v23 TP package was controller read-back verified, but the latest live run "
            "lost contact in Stage25.0, continued active_reacquire_solver qdot for about 1.7s, "
            "and stopped on tcp_cage_braking_margin_exhausted. The 102N summary spike was a "
            "baseline_not_ready startup sample; trusted contact force stayed near 16N. The "
            "RNN/J(q) approach projection opposed the outer-loop press command, so v24 "
            "supersedes v23 with low/no-contact zero-qdot stop, trusted startup summaries, "
            "25N raw/force hard guards, and post-RNN tracking reversal detection."
        )
    elif label == "v27":
        root_cause = (
            "The v27 TP package was controller read-back verified. Its 2026-07-06 04:55:13 "
            "live run passed the 10 s Step5b-live / Step5d-shadow fix-validation window "
            "with live angular command held at zero, Step5d paper/RNN outputs recorded as "
            "shadow diagnostics, and no low-load or force-norm hard stop. It is retained "
            "as successful fix-validation evidence only, not a 60 s Step5d reproduction; "
            "v28 supersedes it with the same runtime interface and a 60 s Stage25 target."
        )
    else:
        root_cause = (
            f"The {label} TP package was controller read-back verified, but no successful "
            "Step5d reproduction completion was recorded before it was superseded."
        )
    result = "retained incomplete live-attempt evidence; no successful Step5d reproduction completion was recorded"
    if label == "v27" and any(
        attempt.get("run_dir") == "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513"
        for attempt in attempts
    ):
        result = (
            "retained successful 10s Step5b-live / Step5d-shadow fix-validation evidence; "
            "not a completed 60s Step5d reproduction"
        )
    return {
        "attempts": attempts,
        "latest_run_dir": latest.get("run_dir"),
        "latest_stop_reason": latest.get("stop_reason"),
        "result": result,
        "root_cause_summary": root_cause,
    }


def find_stage(table: dict[str, Any], stage_id: str) -> dict[str, Any] | None:
    for row in table.get("stages", []):
        if row.get("id") == stage_id:
            return row
    return None


def update_previous_stage(
    root: Path,
    table: dict[str, Any],
    previous: str,
    current: dict[str, Any],
    successor: str,
) -> dict[str, Any]:
    row = find_stage(table, previous)
    if row is None:
        fail(f"stage table row is missing for previous current {previous}")
    archived_triplet = archive_previous_triplet(root, previous, current)
    current_target = current.get("controller_target") or current.get("controller_program")
    archived_controller_target = None
    if isinstance(current_target, str) and current_target:
        target_path = PurePosixPath(current_target)
        if target_path.parent.name == "step5d":
            archived_controller_target = str(target_path)
        else:
            archived_controller_target = str(target_path.parent / "step5d" / target_path.name)
    previous_label = version_label(previous)
    successor_label = version_label(successor)
    row["active"] = False
    row["complete"] = True
    row["completion_target"] = False
    row["block_reason"] = (
        f"Retained {previous_label} live-attempt evidence. Controller read-back was verified, "
        f"but the live attempt did not complete successfully; superseded by {successor_label}."
    )
    row["success_condition"] = (
        f"Retained {previous_label} evidence only; not current and not a completed reproduction claim."
    )
    row["live_run_evidence"] = live_attempt_evidence(root, previous)
    delivery = row.setdefault("local_delivery_evidence", {})
    delivery["local_program_dir"] = str(STEP5D_ARCHIVE_DIR)
    delivery["local_triplet"] = archived_triplet
    delivery["archived_to_step5d_dir"] = True
    if archived_controller_target:
        delivery["controller_target"] = archived_controller_target
    contact_policy = row.setdefault("contact_policy", {})
    contact_policy["live_authorization"] = "retained_live_attempt_evidence_no_current_retry_authorization"
    contact_policy["tp_package_status"] = "retained_controller_readback_verified"
    contact_policy["controller_readback_status"] = "verified_retained"
    binding = row.setdefault("current_binding", {})
    binding.update(
        {
            "source": "config/current_stage.json",
            "is_current": False,
            "flow_claim_status": "retained_evidence_route",
        }
    )
    package_delivery = row.setdefault("package_delivery", {})
    package_delivery.update(
        {
            "program_basename": delivery.get("program_basename", previous),
            "local_program_dir": delivery.get("local_program_dir", str(STEP5D_ARCHIVE_DIR)),
            "local_triplet": delivery.get("local_triplet", archived_triplet),
            "controller_target": delivery.get("controller_target"),
            "controller_dir": delivery.get("controller_dir", TARGET_DIR),
            "controller_readback_status": "verified_retained",
            "controller_readback_manifest": delivery.get("controller_readback"),
            "delivery_mode": delivery.get("delivery_mode", "full_upload_readback"),
            "sha256": delivery.get("sha256", {}),
        }
    )
    cadence = row.setdefault("cadence", {})
    cadence["motion"] = "retained_incomplete_live_attempt_evidence"
    operator = row.setdefault("operator_lifecycle", {})
    if archived_controller_target:
        operator["expected_program"] = archived_controller_target
        binding = row.setdefault("current_binding", {})
        binding["controller_target"] = archived_controller_target
    row["notes"] = [
        f"{previous_label} was controller read-back verified but is retained as failed live-attempt evidence.",
        f"{previous_label} is no longer current; open {successor_label} from the Step5 root on the Teach Pendant.",
        f"{previous_label} local triplet is archived under {STEP5D_ARCHIVE_DIR}.",
    ]
    return row


def preload_gate(program: str) -> dict[str, float]:
    label = version_label(program)
    if label not in {"v21", "v22", "v23", "v24", "v25", "v26", "v27", "v28", "v29"}:
        fail(f"preload gate defaults are not defined for {program}")
    gate = {
        "filtered_normal_load_min_n": 7.5,
        "filtered_normal_load_max_n": 14.0,
        "raw_normal_load_min_n": 7.0,
        "raw_normal_load_max_n": 15.0,
        "force_norm_max_n": 25.0,
        "required_s": 0.1,
        "param_valid_code": 521.0,
    }
    if program == STEP5D_ABLATION_V25:
        gate.update(
            {
                "filtered_normal_load_min_n": 10.5,
                "filtered_normal_load_max_n": 12.8,
                "raw_normal_load_min_n": 9.5,
                "raw_normal_load_max_n": 13.5,
            }
        )
    if program == STEP5D_ABLATION_V26:
        gate.update(
            {
                "filtered_normal_load_min_n": 7.0,
                "filtered_normal_load_max_n": 18.0,
                "raw_normal_load_min_n": 5.0,
                "raw_normal_load_max_n": 20.0,
                "recovery_normal_load_max_n": 24.0,
                "force_norm_stop_n": 25.0,
            }
        )
    if program in STEP5D_STEP5B_SPEEDL_LIVE_PROGRAMS:
        gate.update(
            {
                "filtered_normal_load_min_n": 5.0,
                "filtered_normal_load_max_n": 22.0,
                "raw_normal_load_min_n": 3.0,
                "raw_normal_load_max_n": 25.0,
                "force_norm_max_n": 35.0,
                "recovery_normal_load_max_n": 35.0,
                "force_norm_stop_n": 35.0,
            }
        )
    if label in {"v24", "v25"}:
        gate.update(
            {
                "recovery_normal_load_max_n": 20.0,
                "force_norm_stop_n": 25.0,
            }
        )
    return gate


def sensor_hard_guards(program: str) -> tuple[float, float, float]:
    label = version_label(program)
    if program in STEP5D_STEP5B_SPEEDL_LIVE_PROGRAMS:
        return 50.0, 60.0, 3.0
    if label in {"v24", "v25", "v26"}:
        return 25.0, 25.0, 4.0
    return 100.0, 100.0, 4.0


def stage25_success_target_s(program: str) -> float:
    return 60.0 if program in {STEP5D_ABLATION_V28, STEP5D_ABLATION_V29} else 10.0


def stage25_runtime_limit_s(program: str) -> float:
    return 65.0 if program in {STEP5D_ABLATION_V28, STEP5D_ABLATION_V29} else 15.0


def build_current_stage_row(
    base_row: dict[str, Any],
    program: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    label = version_label(program)
    is_ablation = program in STEP5D_ABLATION_PROGRAMS
    step5b_speedl_live = program in STEP5D_STEP5B_SPEEDL_LIVE_PROGRAMS
    stage25_target_s = stage25_success_target_s(program)
    stage25_limit_s = stage25_runtime_limit_s(program)
    stage25_default_mode = (
        "speedj_rnn_live"
        if program == STEP5D_ABLATION_V29
        else "speedl_cartesian_oracle"
        if program in STEP5D_ABLATION_PROGRAMS
        else None
    )
    hard_raw_guard_n, hard_force_guard_n, hard_torque_guard_nm = sensor_hard_guards(program)
    row = copy.deepcopy(base_row)
    row["id"] = program
    row["active"] = True
    row["blocked"] = False
    row["complete"] = False
    row["completion_target"] = True
    row["duration_s"] = stage25_target_s
    row["block_reason"] = (
        f"Current {label} cage-primary TP/script diagnostic package is generated, uploaded, "
        "and controller read-back verified. It is ready for an explicit "
        f"{stage25_target_s:g} s live bridge run; not a completed reproduction claim."
    )
    row["live_run_evidence"] = None
    if is_ablation:
        policy_refs = row.setdefault("policy_refs", {})
        policy_refs["bridge_startup_policy"] = "bridge_startup_policy"
        policy_refs["startup_gate_profile"] = "startup_gate_profiles.prepared_fast_bridge_v1"
    validation = manifest["validation"]
    sha = manifest["sha256"]["local"]
    controller_target = f"{manifest['target_dir']}/{program}.urp"
    controller_readback_manifest = manifest["manifest_path"]
    row["local_delivery_evidence"] = {
        "program_basename": program,
        "local_program_dir": str(STEP5D_CURRENT_DIR),
        "local_triplet": f"{STEP5D_CURRENT_DIR}/{program}.{{script,txt,urp}}",
        "controller_target": controller_target,
        "controller_dir": manifest["target_dir"],
        "local_package_validated": True,
        "controller_readback_verified": True,
        "controller_readback": controller_readback_manifest,
        "local_controller_readback_sha_match": True,
        "fetched_back_urp_internal_gate_pass": True,
        "sha256": sha,
        "stamp": validation.get("stamp"),
        "installation_relative_path": validation.get("installation_relative_path"),
    }
    if manifest.get("delivery_mode"):
        row["local_delivery_evidence"]["delivery_mode"] = manifest["delivery_mode"]
    if manifest.get("promoted_from_local_candidate"):
        row["local_delivery_evidence"]["promoted_from_local_candidate"] = manifest["promoted_from_local_candidate"]
    row["current_binding"] = {
        "source": "config/current_stage.json",
        "is_current": True,
        "stage_id": program,
        "program": program,
        "controller_target": controller_target,
        "controller_readback_status": "verified_current",
        "controller_readback_manifest": controller_readback_manifest,
        "flow_claim_status": "STEP5_FLOW_top_summary_must_match_current_stage_pointer",
    }
    row["package_delivery"] = {
        "program_basename": program,
        "local_program_dir": str(STEP5D_CURRENT_DIR),
        "local_triplet": f"{STEP5D_CURRENT_DIR}/{program}.{{script,txt,urp}}",
        "controller_target": controller_target,
        "controller_dir": manifest["target_dir"],
        "controller_readback_status": "verified_current",
        "controller_readback_manifest": controller_readback_manifest,
        "delivery_mode": manifest.get("delivery_mode", "full_upload_readback"),
        "sha256": sha,
    }
    entry_gate = preload_gate(program)
    guard = row.setdefault("guard", {})
    guard.update(
        {
            "line_entry_normal_load_min_n": entry_gate["filtered_normal_load_min_n"],
            "line_entry_normal_load_max_n": entry_gate["filtered_normal_load_max_n"],
            "line_entry_raw_sanity_min_n": entry_gate["raw_normal_load_min_n"],
            "line_entry_raw_sanity_max_n": entry_gate["raw_normal_load_max_n"],
            "line_entry_force_norm_max_n": entry_gate["force_norm_max_n"],
            "line_entry_required_s": entry_gate["required_s"],
            "bridge_start_wait_timeout_s": 60.0 if is_ablation else None,
            "line_entry_param_valid_code": entry_gate["param_valid_code"],
            "line_entry_recovery_normal_load_max_n": entry_gate.get(
                "recovery_normal_load_max_n",
                40.0,
            ),
            "line_entry_force_norm_stop_n": entry_gate.get("force_norm_stop_n", 100.0),
            "stage25_95_qdot_clear_required_s": 0.006 if label in {"v22", "v23", "v24"} else None,
            "stage25_95_qdot_clear_timeout_s": 1.0 if label in {"v22", "v23", "v24"} else None,
            "stage25_95_qdot_clear_zero_tol_rad_s": 0.0005 if label in {"v23", "v24"} else None,
            "stage25_95_register_clear_required_s": 0.006 if is_ablation else None,
            "stage25_95_register_clear_timeout_s": 1.0 if is_ablation else None,
            "stage25_95_register_clear_zero_tol": 0.0005 if is_ablation else None,
            "stage25_cartesian_layout_tag": 523.0 if is_ablation else None,
            "stage25_joint_layout_tag": 524.0 if is_ablation else None,
            "stage25_default_control_mode": stage25_default_mode,
            "stage25_post_rnn_normal_guard_hold_load_n": 14.0 if label in {"v23", "v24"} else None,
            "stage25_post_rnn_normal_guard_directional_stop_load_n": 18.0 if label in {"v23", "v24"} else None,
            "stage25_post_rnn_normal_guard_hard_stop_load_n": 25.0 if label in {"v23", "v24"} else None,
            "stage25_post_rnn_normal_guard_hard_stop_force_norm_n": 25.0 if label in {"v23", "v24"} else None,
            "stage25_low_load_hold_timeout_s": 0.050 if label == "v24" else None,
            "stage25_speedl_low_load_hard_stop_n": 2.0 if is_ablation else None,
            "stage25_speedl_low_load_hard_stop_s": 0.100 if is_ablation else None,
            "stage25_speedl_low_load_soft_stop_n": 5.0 if is_ablation else None,
            "stage25_speedl_low_load_soft_stop_s": 0.500 if is_ablation else None,
            "stage25_tracking_guard_opposed_dwell_s": 0.004 if label == "v24" else None,
            "stage25_tracking_guard_outer_press_min_m_s": 0.0001 if label == "v24" else None,
            "stage25_tracking_guard_unload_min_m_s": 0.0005 if label == "v24" else None,
            "raw_normal_guard_n": hard_raw_guard_n,
            "force_norm_guard_n": hard_force_guard_n,
            "torque_norm_guard_nm": hard_torque_guard_nm,
            "target_force_n": 12.0,
            "duration_s": stage25_target_s,
            "runtime_limit_s": stage25_limit_s,
            "stage25_runtime_limit_s": stage25_limit_s if is_ablation else None,
        }
    )
    for key in (
        "stage25_95_qdot_clear_required_s",
        "stage25_95_qdot_clear_timeout_s",
        "stage25_95_qdot_clear_zero_tol_rad_s",
        "stage25_95_register_clear_required_s",
        "stage25_95_register_clear_timeout_s",
        "stage25_95_register_clear_zero_tol",
        "stage25_cartesian_layout_tag",
        "stage25_joint_layout_tag",
                "stage25_default_control_mode",
                "stage25_runtime_limit_s",
        "bridge_start_wait_timeout_s",
        "stage25_post_rnn_normal_guard_hold_load_n",
        "stage25_post_rnn_normal_guard_directional_stop_load_n",
        "stage25_post_rnn_normal_guard_hard_stop_load_n",
        "stage25_post_rnn_normal_guard_hard_stop_force_norm_n",
        "stage25_low_load_hold_timeout_s",
        "stage25_speedl_low_load_hard_stop_n",
        "stage25_speedl_low_load_hard_stop_s",
        "stage25_speedl_low_load_soft_stop_n",
        "stage25_speedl_low_load_soft_stop_s",
        "stage25_tracking_guard_opposed_dwell_s",
        "stage25_tracking_guard_outer_press_min_m_s",
        "stage25_tracking_guard_unload_min_m_s",
    ):
        if guard.get(key) is None:
            guard.pop(key, None)
    if is_ablation:
        guard.update(
            {
                "cartesian_layout_code": 523.0,
                "joint_layout_code": 524.0,
                "speedl_linear_cap_m_s": 0.004,
                "speedl_angular_cap_rad_s": 0.150 if program == STEP5D_ABLATION_V25 else 0.015,
                "attitude_cap_rad_s": 0.150 if program == STEP5D_ABLATION_V25 else 0.015,
                "force_norm_hard_stop_n": hard_force_guard_n,
                "raw_normal_hard_stop_n": hard_raw_guard_n,
                "torque_norm_hard_stop_nm": hard_torque_guard_nm,
                "stage25_success_target_s": stage25_target_s,
                "stage25_runtime_limit_s": stage25_limit_s,
                "runtime_diagnostics": [
                    "_step5d_stage25_control_mode",
                    "_step5d_stage25_echo_consumed",
                    "_step5d_stage25_row_gap_s",
                    "_bridge_loop_rtde_send_s",
                    "_step5d_tcp_cage_distance_m",
                    "_step5d_tcp_cage_braking_margin_m",
                    "_step5d_rnn_raw_qd0_rad_s",
                    "_step5d_jqdot_raw_approach_normal_m_s",
                    "_step5d_jqdot_cmd_approach_normal_m_s",
                    "_step5d_outer_xdot_limited_approach_normal_m_s",
                    "_step5d_qdot_cap_rad_s",
                    "_step5d_jinv_xdot_inf_rad_s",
                    "_step5d_jinv_xdot_inf_over_qdot_cap",
                    "_step5d_jinv_xdot_solve_status",
                    "_step5d_xdot_feasibility_scale",
                    "_step5d_xdot_norm_pre_feasibility_scale",
                    "_step5d_xdot_norm_post_feasibility_scale",
                    "_step5d_xdot_feasibility_scale_active",
                    "_step5d_lambda_state_0",
                    "_step5d_qdot_max_abs_rad_s",
                ],
            }
        )
        if program == STEP5D_ABLATION_V26:
            guard["joint_feasibility_scale"] = (
                "s=min(1,0.9*qdot_cap/||J^-1 xdot_c||inf) before strict RNN/DLS speedj modes"
            )
        if step5b_speedl_live:
            if program == STEP5D_ABLATION_V29:
                guard["speedj_rnn_live_command_policy"] = (
                    "v29 bridge runtime sends strict RNN qdot to speedj layout 524 by default; "
                    "speedl_cartesian_oracle and speedj_dls_oracle remain explicit debug/fallback modes"
                )
            else:
                guard["speedl_angular_live_command_policy"] = (
                    f"{label} bridge runtime forces wx/wy/wz to 0 for all Stage25.0 in "
                    "speedl_cartesian_oracle; limited raw angular remains diagnostic only"
                )
            diagnostics = guard.setdefault("runtime_diagnostics", [])
            for column in (
                "_step5d_live_control_source",
                "_step5d_speedl_orientation_shadow_only",
                "_step5d_speedl_shadow_raw_vx_m_s",
                "_step5d_speedl_shadow_raw_vy_m_s",
                "_step5d_speedl_shadow_raw_vz_m_s",
                "_step5d_speedl_shadow_raw_wx_rad_s",
                "_step5d_speedl_shadow_raw_wy_rad_s",
                "_step5d_speedl_shadow_raw_wz_rad_s",
            ):
                if column not in diagnostics:
                    diagnostics.append(column)
            if program == STEP5D_ABLATION_V29:
                for column in (
                    "_step5d_rnn_accepted",
                    "_step5d_rnn_reject_reason",
                    "_step5d_safe_hold_active",
                    "_step5d_cmd_valid_reason",
                    "_step5d_rnn_inner_iterations",
                    "_step5d_rnn_backend",
                    "_step5d_rnn_epsilon",
                    "_step5d_rnn_sigr_exponent_r",
                    "_step5d_constraint_residual_norm",
                    "_step5d_lambda_norm",
                ):
                    if column not in diagnostics:
                        diagnostics.append(column)
            guard["stage25_cadence_max_gap_s"] = 0.020
            guard["stage25_command_consumption_echo_register"] = 47.0
    contact_policy = row.setdefault("contact_policy", {})
    if is_ablation:
        for stale_key in (
            "joint_solver",
            "dls_fallback_allowed",
            "ik_fallback_allowed",
            "calibrated_kinematics_backend",
        ):
            contact_policy.pop(stale_key, None)
        contact_policy.update(
            {
                "bridge_required": True,
                "tp_role": "multimode_executor_and_guard_only",
                "reference_owner": "bridge",
                "stage25_control_modes": [
                    "speedl_cartesian_oracle",
                    "speedj_dls_oracle",
                    "speedj_rnn_live",
                ],
                "default_stage25_control_mode": stage25_default_mode,
                "speedl_cartesian_oracle_policy": (
                    "v29 keeps speedl_cartesian_oracle as an explicit fallback/debug path; default live source is strict RNN speedj"
                    if program == STEP5D_ABLATION_V29
                    else "Step5b speedl live vx/vy/vz is sent to TP speedl, live wx/wy/wz are forced to zero, "
                    "and Step5d paper/RNN linear/angular outputs are shadow diagnostics only"
                    if step5b_speedl_live
                    else "paper outer-loop xdot_c is sent directly to TP speedl for Cartesian oracle diagnostics; "
                    "strict RNN and J(q) qdot are shadow diagnostics only"
                ),
                "speedj_dls_oracle_policy": (
                    "feasibility-scaled xdot_c is converted through the DLS/Jacobian qdot oracle and sent to TP speedj "
                    "for diagnostic comparison only"
                ),
                "speedj_rnn_live_policy": (
                    "strict RNN qdot from feasibility-scaled xdot_c is sent to TP speedj after solver lambda/theta_dot "
                    "reset on Stage25 lifecycle boundaries; evidence rejection records diagnostics and uses bounded hold "
                    "without clearing cmd_valid unless a hard safety or command-contract violation occurs"
                    if program == STEP5D_ABLATION_V29
                    else "strict RNN qdot from feasibility-scaled xdot_c is sent to TP speedj after solver lambda/theta_dot "
                    "reset on Stage25 lifecycle boundaries"
                ),
                "force_control": True,
                "contact_search": True,
                "timing_policy": (
                    "step5b_v3_no_lift_no_25_2_no_second_search_with_gravity_down_search_and_target_centric_preload"
                    if program == STEP5D_ABLATION_V25
                    else "step5d_v29_contact_strict_rnn_live_stage25_speedj_candidate_with_v28_envelope"
                    if program == STEP5D_ABLATION_V29
                    else "step5b_v3_scaffold_min_delta_stage25_speedl_oracle_with_cadence_consumption_instrumentation"
                    if step5b_speedl_live
                    else "step5b_v3_no_lift_no_25_2_no_second_search_with_gravity_down_search_and_step5b_step6b_evidence_tube_preload"
                ),
                "virtual_clock_freeze": False,
                "deadband_acquire_stage": "25.3",
            }
        )
    contact_policy["live_authorization"] = "current_controller_readback_verified_pending_live_bridge_run"
    contact_policy["tp_package_status"] = "generated_uploaded_readback_verified_current"
    contact_policy["controller_readback_status"] = "verified"
    stage25_policy = (
        "Online broad AABB TCP cage is primary diagnostic boundary; Stage25.3 "
        "uses bridge-time preload parameters, low-load/no-contact freezes path time, "
        "resets outer-loop state during active_reacquire_solver based on action/load semantics, "
        "scales qdot to <=0.035 m/s predicted TCP speed, and preserves semantic/cage/sensor/"
        "heartbeat/Dashboard hard stops."
    )
    if label == "v22":
        stage25_policy = (
            "Online broad AABB TCP cage is primary diagnostic boundary; Stage25.3 "
            "uses bridge-time preload parameters, Stage25.95 waits for the bridge to clear "
            "37..47 away from the preload layout before Stage25.0 consumes qdot, low-load/"
            "no-contact freezes path time, resets outer-loop state during active_reacquire_solver "
            "based on action/load semantics, scales qdot to <=0.035 m/s predicted TCP speed, "
            "and preserves semantic/cage/sensor/heartbeat/Dashboard hard stops."
        )
    if label == "v23":
        stage25_policy = (
            "Online broad AABB TCP cage is primary diagnostic boundary; Stage25.3 "
            "uses bridge-time preload parameters, Stage25.95 waits for bridge-cleared "
            "near-zero qdot registers before Stage25.0 consumes qdot, low-load/no-contact "
            "freezes path time, resets outer-loop state during active_reacquire_solver based "
            "on action/load semantics, scales qdot to <=0.035 m/s predicted TCP speed, "
            "and applies a post-RNN normal-direction guard that holds/stops over-target "
            "pressing commands before the 100N sensor hard guard."
        )
    if label == "v24":
        stage25_policy = (
            "Online broad AABB TCP cage remains a diagnostic boundary; Stage25.3 "
            "uses bridge-time preload parameters with 20N/25N recovery stops, "
            "Stage25.95 waits for bridge-cleared near-zero qdot registers, low-load/"
            "no-contact freezes path time and commands zero qdot instead of executing "
            "active_reacquire_solver qdot, and post-RNN tracking reversal detection "
            "holds/stops when the RNN/J(q) command unloads while the outer loop asks to press."
        )
    if is_ablation:
        preload_policy = (
            f"{entry_gate['filtered_normal_load_min_n']:g}-{entry_gate['filtered_normal_load_max_n']:g}N filtered, "
            f"{entry_gate['raw_normal_load_min_n']:g}-{entry_gate['raw_normal_load_max_n']:g}N raw sanity"
        )
        preload_basis = (
            "target-centric preload parameters"
            if program == STEP5D_ABLATION_V25
            else "Step5b/Step6b evidence tube preload parameters"
        )
        stage25_policy = (
            "Online broad AABB TCP cage remains a hard diagnostic boundary; Stage25.3 "
            f"uses {preload_basis} ({preload_policy}), "
            "Stage25.95 waits for bridge-cleared registers 37..47, and Stage25.0 selects "
            "Cartesian speedl or joint speedj from layout tag 47. v25/v26/v27/v28 default to "
            "speedl_cartesian_oracle with strict RNN/J(q) shadow diagnostics; v29 defaults to "
            "speedj_rnn_live with speedl_cartesian_oracle and speedj_dls_oracle as explicit fallback/debug modes."
        )
        if step5b_speedl_live:
            if program == STEP5D_ABLATION_V29:
                stage25_policy += (
                    " v29 speedj_rnn_live sends strict RNN qdot to TP speedj on layout 524 after "
                    "solver warm-start; residual/lambda evidence failures are diagnostics unless a "
                    "hard safety or command-contract violation occurs."
                )
            else:
                stage25_policy += (
                    f" {label} speedl_cartesian_oracle executes Step5b live vx/vy/vz, forces live wx/wy/wz to zero, "
                    "keeps Step5d paper/RNN linear/angular outputs shadow-only, and logs Stage25.0 cadence, "
                    "TP consumption echo, and bridge loop timing."
                )
    contact_policy["stage25_contact_policy"] = stage25_policy
    if is_ablation:
        row["liveprep_gates"] = [
            "Stage 22 and Stage 24 pre-contact search posture uses gravity-down [pi,0,0], TCP +Z targeting base -Z",
            "Stage 25.3 bridge deadband acquire consumes Cartesian vx/vy/vz in registers 37..39",
            (
                f"Stage 25.3 enters Stage25 only after {entry_gate['filtered_normal_load_min_n']:g}-"
                f"{entry_gate['filtered_normal_load_max_n']:g}N filtered normal_load, "
                f"{entry_gate['raw_normal_load_min_n']:g}-{entry_gate['raw_normal_load_max_n']:g}N raw sanity, "
                f"force_norm <={entry_gate['force_norm_max_n']:g}N, and cmd_valid true for {entry_gate['required_s']:.3f} s"
            ),
            "Stage 25.95 clears registers 37..47 away from preload/cartesian/joint layout tags before Stage25.0 consumption",
            "Stage25.0 register 47 selects 523.0 Cartesian speedl or 524.0 joint speedj; v25/v26/v27/v28 default to speedl_cartesian_oracle and v29 defaults to speedj_rnn_live",
        ]
    else:
        row["liveprep_gates"] = [
            "Stage 22 and Stage 24 pre-contact search posture uses gravity-down [pi,0,0], TCP +Z targeting base -Z",
            "Stage 25.3 bridge deadband acquire consumes Cartesian vx/vy/vz in registers 37..39",
            "Stage 25.3 enters Stage25 only after 7.5-14N filtered normal_load, 7-15N raw sanity, force_norm <=25N, and cmd_valid true for 0.100 s",
            "Stage 25.95 clears registers 37..47 away from the preload layout before Stage25.0 qdot consumption",
            (
                "Stage25 strict RNN qdot is capped at 0.05 rad/s with qdot slew limiting; "
                "v24 computes/logs RNN output but low/no-contact commands zero qdot and stops after 0.050 s"
                if label == "v24"
                else "Stage25 strict RNN qdot is capped at 0.05 rad/s with qdot slew limiting and online broad TCP cage active-reacquire safety"
            ),
        ]
    row["success_condition"] = (
        f"Current {label} package is generated, uploaded, controller read-back verified, "
        f"and awaits explicit {stage25_target_s:g} s live bridge run evidence before any reproduction claim."
    )
    if is_ablation:
        operator = row.setdefault("operator_lifecycle", {})
        operator.update(
            {
                "entrypoint": "scripts/step5d-liveprep-operator.sh",
                "base_operator": "scripts/bridge-line-operator.sh",
                "mode": "line-bridge-fast",
                "confirm_env": "STEP5D_CONFIRM",
                "confirm_token": "LIVE STEP5D STRICT RNN LIVEPREP",
                "expected_program": controller_target,
                "fast_trigger_requires_long_check_cache": True,
                "long_check_cache_path": "runs/.bridge_long_checks_cache.json",
                "long_check_ttl_s": 7200,
                "wait_for_play_s": STEP5D_OPERATOR_PLAY_WAIT_S,
                "autowatch_wait_for_play_s": STEP5D_OPERATOR_PLAY_WAIT_S,
                "dashboard_program_watch_timeout_s": 45,
                "tp_bridge_start_wait_timeout_s": 60,
                "rtde_quick_probe_timeout_s": 1.0,
                "output_start_wait_s": 3.0,
                "post_run_quiet_stop_check": True,
                "post_run_stage_frequency_summary": True,
                "background_push_after_live": False,
                "operator_boundaries": [
                    "wrapper never loads a program or presses Play",
                    "no zero_ftsensor",
                    "no Kunwei tare/zero/config",
                    "no TCP/payload write",
                ],
            }
        )
        operator.pop("live_retry_blocked_until", None)
        if program == STEP5D_ABLATION_V29:
            for stale_key in (
                "no_contact_p0_expected_program",
                "no_contact_p0_confirm_env",
                "no_contact_p0_confirm_token",
                "no_contact_p0_entrypoint",
                "no_contact_p0_operator_mode",
            ):
                operator.pop(stale_key, None)
        operator["live_readiness_state"] = (
            f"controller_readback_verified_pending_explicit_{stage25_target_s:g}s_live_bridge_run"
        )
        runtime_ref = row.setdefault("runtime_interface_ref", {})
        runtime_ref.update(
            {
                "path": "tools/step5d_runtime_interface.py",
                "interface_class": "tp_speedj_strict_rnn_liveprep_v1",
                "tuning_bundle": "v24_startup_quarantine_rnn_tracking_guard",
                "stage25_default_control_mode": stage25_default_mode,
                "stage25_success_target_s": stage25_target_s,
                "stage25_runtime_limit_s": stage25_limit_s,
            }
        )
        if program == STEP5D_ABLATION_V29:
            runtime_ref.pop("stage25_speedl_orientation_policy", None)
    row["notes"] = [
        f"{label} is controller read-back verified and selected as the current Step5d TP/script diagnostic package.",
        f"{label} keeps Stage22/24 gravity-down [pi,0,0] pre-contact search posture.",
        (
            f"{label} is an ablation package with layout-tagged speedl/speedj Stage25.0 modes."
            if is_ablation
            else
            f"{label} stops low-load/no-contact with zero qdot instead of executing active_reacquire_solver qdot."
            if label == "v24"
            else f"{label} uses the retained cage-primary active-reacquire diagnostic policy."
        ),
        (
            f"{label} uses {hard_raw_guard_n:g}N raw-normal, {hard_force_guard_n:g}N force-norm, {hard_torque_guard_nm:g}Nm torque guard, and layout-tagged speedl/speedj Stage25.0."
            if is_ablation
            else
            f"{label} uses 25N raw-normal/force-norm hard guards and post-RNN tracking reversal detection."
            if label == "v24"
            else f"{label} uses retained raw-normal/force-norm hard guards."
        ),
        f"This current row is not a completed Step5d reproduction claim; explicit {stage25_target_s:g} s live bridge evidence is still pending.",
    ]
    analysis = row.setdefault("local_analysis_evidence", {})
    analysis["source"] = (
        "2026-07-02 v21 live-run register-layout root cause plus v22 qdot-clear implementation; "
        "2026-07-03 v22 normal_force_guard, v23 active_reacquire cage-margin failure, and v24 low-load/tracking guard"
    )
    analysis["v21_register_layout_root_cause"] = (
        "Stage25.3 preload values in 40/41/42/44/46/47 with tag 521 were echoed into "
        "Stage25.0 qdot consumption; v22 adds Stage25.95 to require bridge-cleared 37..47."
    )
    if step5b_speedl_live:
        analysis["v27_20260706_045513_step5b_live_step5d_shadow_fix_validation"] = (
            v27_fix_validation_analysis()
        )
    analysis[f"{label}_controller_readback_manifest"] = manifest["manifest_path"]
    cadence = row.setdefault("cadence", {})
    cadence["motion"] = (
        "pending_live_60s_full_run"
        if program in {STEP5D_ABLATION_V28, STEP5D_ABLATION_V29}
        else "pending_live_diagnostic"
    )
    lifecycle = row.setdefault("lifecycle", {})
    lifecycle.update(
        {
            "state": (
                f"current_{label}_controller_readback_verified_pending_60s_full_run"
                if program in {STEP5D_ABLATION_V28, STEP5D_ABLATION_V29}
                else f"current_{label}_controller_readback_verified_pending_live_run"
            ),
            "current_candidate": True,
            "retained_evidence": False,
            "claim_status": (
                "controller_readback_verified_pending_60s_live_run_not_reproduction_claim"
                if program in {STEP5D_ABLATION_V28, STEP5D_ABLATION_V29}
                else "controller_readback_verified_pending_live_run_not_reproduction_claim"
            ),
            "supersedes": (
                "step5d_strict_rnn_ablation_v28"
                if program == STEP5D_ABLATION_V29
                else "step5d_strict_rnn_ablation_v27"
                if program == STEP5D_ABLATION_V28
                else "step5d_strict_rnn_liveprep_v24"
            ),
        }
    )
    evidence_refs = row.setdefault("evidence_refs", {})
    if program == STEP5D_ABLATION_V28:
        evidence_refs.pop("latest_control_oscillation_trigger", None)
        evidence_refs.update(
            {
                "latest_live_run_status": "v27_fix_validation_success_pending_v28_60s_live_run",
                "latest_live_run_summary": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513/summary.json",
                "latest_live_run_analysis": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513/step5d_bridge_analysis.json",
                "latest_stop_reason": "dashboard_program_stopped",
                "latest_terminal_stop_reason": "tp_normal_stop_reason_1",
                "latest_analysis_classification": "stage25_fix_validation_success",
                "latest_evidence_classification": "v27_10s_step5b_live_step5d_shadow_fix_validation_success_not_60s_reproduction",
                "v28_live_run_status": "pending_explicit_60s_live_bridge_run",
                "previous_failed_live_run_analysis": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_040900/step5d_bridge_analysis.json",
                "previous_retained_evidence_index": "current_stage.json#evidence",
            }
        )
    return row


def upsert_stage(table: dict[str, Any], row: dict[str, Any], after_id: str | None = None) -> None:
    stages = table.setdefault("stages", [])
    for idx, existing in enumerate(stages):
        if existing.get("id") == row.get("id"):
            stages[idx] = row
            return
    insert_at = len(stages)
    if after_id:
        for idx, existing in enumerate(stages):
            if existing.get("id") == after_id:
                insert_at = idx + 1
                break
    stages.insert(insert_at, row)


def update_bridge_startup_policy(table: dict[str, Any], program: str) -> None:
    startup = table.setdefault("bridge_startup_policy", {})
    observed = startup.setdefault("observed_timing", {})
    observed["current_step5d_tp_play_wait_max_s"] = STEP5D_OPERATOR_PLAY_WAIT_S


def archived_controller_target(value: str) -> str:
    target_path = PurePosixPath(value)
    if target_path.parent.name == "step5d":
        return str(target_path)
    return str(target_path.parent / "step5d" / target_path.name)


def normalize_retained_stage_metadata(table: dict[str, Any], current_program: str) -> None:
    for row in table.get("stages", []):
        program = row.get("id")
        if not isinstance(program, str):
            continue
        if program == current_program or not program.startswith(STEP5D_PACKAGE_PREFIXES):
            continue
        delivery = row.get("local_delivery_evidence")
        if not (isinstance(delivery, dict) and delivery.get("archived_to_step5d_dir") is True):
            continue
        if delivery:
            delivery["local_program_dir"] = str(STEP5D_ARCHIVE_DIR)
            delivery["local_triplet"] = str(STEP5D_ARCHIVE_DIR / f"{program}.{{script,txt,urp}}")
            delivery["archived_to_step5d_dir"] = True
            target = delivery.get("controller_target")
            if isinstance(target, str) and "step5d_strict_rnn" in target:
                delivery["controller_target"] = archived_controller_target(target)
        package_delivery = row.get("package_delivery")
        if isinstance(package_delivery, dict) and package_delivery:
            package_delivery["local_program_dir"] = str(STEP5D_ARCHIVE_DIR)
            package_delivery["local_triplet"] = str(STEP5D_ARCHIVE_DIR / f"{program}.{{script,txt,urp}}")
            target = package_delivery.get("controller_target")
            if isinstance(target, str) and "step5d_strict_rnn" in target:
                package_delivery["controller_target"] = archived_controller_target(target)
        operator = row.get("operator_lifecycle")
        if isinstance(operator, dict):
            expected = operator.get("expected_program")
            if isinstance(expected, str) and "step5d_strict_rnn" in expected:
                operator["expected_program"] = archived_controller_target(expected)
        binding = row.get("current_binding")
        if isinstance(binding, dict):
            target = binding.get("controller_target")
            if isinstance(target, str) and "step5d_strict_rnn" in target:
                binding["controller_target"] = archived_controller_target(target)


def freeze_v29_fallback_for_v30(table: dict[str, Any]) -> None:
    """Retire the current pointer without rewriting or relocating frozen v29 evidence."""

    row = find_stage(table, STEP5D_ABLATION_V29)
    if row is None:
        fail("v29 frozen fallback row is missing before v30 promotion")
    row["active"] = False
    row["blocked"] = True
    row["complete"] = False
    row["completion_target"] = False
    row["block_reason"] = (
        "Frozen v29 fallback retained byte-for-byte as historical package/readback evidence; "
        "v30 is current only after its independent P0 v8, timing, readback, and Review v2 gates."
    )
    binding = row.setdefault("current_binding", {})
    binding["is_current"] = False
    binding["flow_claim_status"] = "frozen_fallback_not_current"
    lifecycle = row.setdefault("lifecycle", {})
    lifecycle["current_candidate"] = False
    lifecycle["retained_evidence"] = True
    lifecycle["state"] = "frozen_v29_fallback_superseded_by_v30_current_pointer"
    lifecycle["claim_status"] = "frozen_fallback_not_live_accepted_not_reproduction_complete"
    lifecycle["superseded_by"] = STEP5D_ABLATION_V30


def build_v30_current_stage_row(
    base_row: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Promote pointer/readback state while preserving the frozen v30 control contract."""

    row = copy.deepcopy(base_row)
    program = STEP5D_ABLATION_V30
    sha = manifest["sha256"]["local"]
    validation = manifest["validation"]
    controller_target = f"{manifest['target_dir']}/{program}.urp"
    readback_manifest = manifest["manifest_path"]
    prior_delivery = base_row.get("package_delivery") or {}
    row.update(
        {
            "id": program,
            "active": True,
            "blocked": False,
            "complete": False,
            "completion_target": True,
            "bridge": False,
            "block_reason": (
                "v30 is the current controller-readback-verified package after frozen P0 v8, "
                "60-second timing/safe-hold, and Review v2 2+1 acceptance; bridge/contact still "
                "require explicit live authorization."
            ),
            "success_condition": (
                "Current package acceptance is not live acceptance or reproduction completion; "
                "a separately authorized contact run remains required."
            ),
        }
    )
    row["local_delivery_evidence"] = {
        "program_basename": program,
        "local_program_dir": str(STEP5D_CURRENT_DIR),
        "local_triplet": f"{STEP5D_CURRENT_DIR}/{program}.{{script,txt,urp}}",
        "controller_target": controller_target,
        "controller_dir": manifest["target_dir"],
        "local_package_validated": True,
        "controller_readback_verified": True,
        "controller_readback": readback_manifest,
        "local_controller_readback_sha_match": True,
        "sha256": sha,
        "stamp": validation.get("stamp"),
        "installation_relative_path": validation.get("installation_relative_path"),
        "delivery_mode": manifest.get("delivery_mode", "full_upload_readback"),
    }
    row["package_delivery"] = {
        "status": "controller_readback_verified_current",
        "program_basename": program,
        "local_program_dir": str(STEP5D_CURRENT_DIR),
        "local_triplet": f"{STEP5D_CURRENT_DIR}/{program}.{{script,txt,urp}}",
        "controller_target": controller_target,
        "controller_dir": manifest["target_dir"],
        "controller_uploaded": True,
        "controller_readback_verified": True,
        "controller_readback_manifest": readback_manifest,
        "delivery_mode": manifest.get("delivery_mode", "full_upload_readback"),
        "semantic_fingerprint": prior_delivery.get("semantic_fingerprint"),
        "delivery_preparation_allowed_before_p0_v8": True,
        "sha256": sha,
    }
    row["current_binding"] = {
        "source": "config/current_stage.json",
        "is_current": True,
        "stage_id": program,
        "program": program,
        "controller_target": controller_target,
        "controller_readback_status": "verified_current",
        "controller_readback_manifest": readback_manifest,
        "flow_claim_status": "package_current_live_not_authorized_not_reproduction_complete",
    }
    contact_policy = row.setdefault("contact_policy", {})
    contact_policy.update(
        {
            "bridge_required": True,
            "tp_role": "strict_rnn_layout524_executor_and_guard_only",
            "reference_owner": "v30_control_contract",
            "live_authorization": "explicit_live_contact_authorization_required",
            "default_stage25_control_mode": "speedj_rnn_live",
            "dls_shadow_only": True,
            "dls_fallback_allowed": False,
        }
    )
    operator = row.setdefault("operator_lifecycle", {})
    operator["expected_program"] = controller_target
    operator["live_readiness_state"] = "current_package_ready_pending_explicit_live_authorization"
    p0_gate = row.setdefault("p0_v8_gate", {})
    p0_gate["passed"] = True
    acceptance = row.setdefault("acceptance", {})
    acceptance["strict_rnn_no_contact_p0_passed"] = True
    acceptance["p0_v8_passed"] = True
    review = row.setdefault("review_v2", {})
    review["status"] = "accepted"
    review["evidence_frozen"] = True
    promotion_gate = row.setdefault("promotion_gate", {})
    promotion_gate.update(
        {
            "current_promotion_allowed": True,
            "bridge_start_allowed": False,
            "contact_run_allowed": False,
        }
    )
    offline = row.setdefault("offline_acceptance", {})
    offline["status"] = "v30_offline_ready"
    lifecycle = row.setdefault("lifecycle", {})
    lifecycle.update(
        {
            "state": "current_v30_awaiting_explicit_live_authorization",
            "current_candidate": True,
            "retained_evidence": False,
            "claim_status": "package_current_live_not_accepted_not_reproduction_complete",
            "supersedes": STEP5D_ABLATION_V29,
        }
    )
    row["claim_boundary"] = {
        "package_accepted": True,
        "live_accepted": False,
        "reproduction_complete": False,
    }
    return row


def update_v30_current_stage(
    current: dict[str, Any],
    manifest: dict[str, Any],
    promotion_evidence: dict[str, Any],
) -> dict[str, Any]:
    payload = copy.deepcopy(current)
    program = STEP5D_ABLATION_V30
    target_dir = manifest["target_dir"]
    sha = manifest["sha256"]["local"]
    payload.update(
        {
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "current_step": "Step5d",
            "current_stage_id": program,
            "program": program,
            "controller_target": f"{target_dir}/{program}.urp",
            "controller_script": f"{target_dir}/{program}.script",
            "local_triplet": f"{STEP5D_CURRENT_DIR}/{program}",
            "delivery_manifest": manifest["manifest_path"],
            "controller_readback_manifest": manifest["manifest_path"],
            "status": "v30_current_package_accepted_awaiting_explicit_live_authorization",
            "sha256": sha,
            "v30_promotion_evidence": promotion_evidence,
            "liveprep_status": {
                "state": "awaiting_live_authorization",
                "package_accepted": True,
                "live_accepted": False,
                "reproduction_complete": False,
            },
            "live_run_status": {"state": "not_started", "accepted": False},
            "reproduction_status": {"state": "incomplete", "complete": False},
        }
    )
    payload["bridge_profile"] = {
        "step4e_version": program,
        "stage25_control_mode": "speedj_rnn_live",
        "joint_layout_code": 524.0,
        "runtime_profile": {
            "backend": "cupy",
            "inner_iterations": 1024,
            "epsilon": 0.01,
            "sigr_exponent_r": 0.8,
            "qdot_cap_rad_s": 0.05,
        },
        "control_contract": (
            "Step5dObservation->StrictRnnControlPolicy->ControlCandidate->"
            "SafetyEnvelope->RegisterCommand"
        ),
        "normal_contract": "n_reaction = -n_approach in one canonical command frame",
        "dls_shadow_only": True,
        "dls_runtime_fallback_allowed": False,
        "stage25_success_target_s": 60.0,
        "stage25_runtime_limit_s": 65.0,
    }
    trigger = payload.setdefault("bridge_trigger", {})
    trigger.update(
        {
            "bridge_has_started": False,
            "live_motion_authorized": False,
            "blocked_reason": (
                "v30 is current and package-accepted; bridge/contact remains blocked until a new "
                "explicit live/contact authorization."
            ),
            "required_before_live": [
                "operator outside UR reach/cage boundary",
                "TP program opened on controller-readback-verified v30 package",
                "v30 package/readback and frozen evidence fingerprint remain current",
                "P0 v8 final continuous 60 second artifact remains accepted",
                "Review v2 current composite 2+1 remains accepted",
                "explicit live/contact authorization for v30 speedj_rnn_live",
            ],
        }
    )
    evidence = payload.setdefault("evidence", {})
    evidence.update(
        {
            "v30_local_package_validated": True,
            "v30_controller_readback_verified": True,
            "v30_controller_readback_manifest": manifest["manifest_path"],
            "v30_controller_target": f"{target_dir}/{program}.urp",
            "v30_local_triplet": f"{STEP5D_CURRENT_DIR}/{program}",
            "v30_sha256": sha,
            "v30_review_v2_composite_fingerprint": promotion_evidence["review_v2"][
                "composite_fingerprint"
            ],
            "sha256": sha,
        }
    )
    payload["strict_rnn_status"] = {
        "reason": (
            "v30 package/readback, P0 v8, timing/safe-hold, and Review v2 are accepted; "
            "live/contact run and reproduction remain incomplete."
        )
    }
    payload["notes"] = [
        "v30 current promotion did not authorize bridge start, TP Play, contact, or robot motion.",
        "DLS remains shadow-only and cannot be a strict-RNN runtime fallback.",
        "package accepted != live accepted != reproduction complete.",
    ]
    return payload


def v27_fix_validation_analysis() -> dict[str, Any]:
    return {
        "run_dir": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513",
        "summary": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513/summary.json",
        "analysis": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513/step5d_bridge_analysis.json",
        "bridge_csv": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513/bridge_rtde_500hz.csv",
        "result": "successful_10s_fix_validation_not_60s_reproduction",
        "analysis_classification": "stage25_fix_validation_success",
        "fix_validation_status": "passed_10s_stage25_window",
        "reproduction_status": "pending_60s_step5b_equivalent_run",
        "stage25_duration_s": 10.215999410953373,
        "stage25_rows": 5109,
        "stage25_consumption_ratio": 0.9982384028185555,
        "stage25_row_rate_hz": 500.0979145047954,
        "terminal_tp_stop_reason": 1,
        "live_control_source": "step5b_speedl_live_step5d_shadow",
        "angular_cmd_norm_max_rad_s": 0.0,
        "shadow_raw_angular_cmd_norm_max_rad_s": 0.015000000048523384,
        "normal_load_min_n": 10.0455088,
        "normal_load_max_n": 14.4362819,
        "force_norm_max_n": 14.4401489,
    }


def normalize_v27_fix_validation_metadata(root: Path, table: dict[str, Any], current_program: str) -> None:
    if current_program == STEP5D_ABLATION_V27:
        return
    row = find_stage(table, STEP5D_ABLATION_V27)
    if row is None:
        return
    evidence = live_attempt_evidence(root, STEP5D_ABLATION_V27)
    analysis = v27_fix_validation_analysis()
    row["active"] = False
    row["complete"] = True
    row["completion_target"] = False
    row["block_reason"] = (
        "Retained v27 successful 10 s fix-validation evidence. Controller read-back was verified "
        "and the 045513 live run validated the Step5b-live / Step5d-shadow runtime boundary; "
        "v28 supersedes it for the 60 s full-run package."
    )
    row["success_condition"] = (
        "Retained v27 10 s fix-validation evidence only; not current and not a completed 60 s reproduction claim."
    )
    row["live_run_evidence"] = evidence
    cadence = row.setdefault("cadence", {})
    cadence["motion"] = "retained_10s_fix_validation_success_not_60s_reproduction"
    lifecycle = row.setdefault("lifecycle", {})
    lifecycle.update(
        {
            "state": "retained_v27_10s_fix_validation_success_superseded_by_v28",
            "current_candidate": False,
            "retained_evidence": True,
            "claim_status": "successful_10s_fix_validation_not_60s_reproduction_claim",
            "superseded_by": current_program,
        }
    )
    refs = row.setdefault("evidence_refs", {})
    refs.update(
        {
            "latest_live_run_status": "v27_fix_validation_success_not_60s_reproduction",
            "latest_live_run_summary": analysis["summary"],
            "latest_live_run_analysis": analysis["analysis"],
            "latest_stop_reason": "dashboard_program_stopped",
            "latest_terminal_stop_reason": "tp_normal_stop_reason_1",
            "latest_analysis_classification": "stage25_fix_validation_success",
            "latest_evidence_classification": "v27_10s_step5b_live_step5d_shadow_fix_validation_success",
            "previous_failed_live_run_analysis": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_040900/step5d_bridge_analysis.json",
            "next_current_stage": current_program,
        }
    )
    refs.pop("latest_control_oscillation_trigger", None)
    local_analysis = row.setdefault("local_analysis_evidence", {})
    local_analysis["v27_20260706_045513_step5b_live_step5d_shadow_fix_validation"] = analysis
    row["notes"] = [
        "v27 is retained as successful 10 s fix-validation evidence for the Step5b-live / Step5d-shadow runtime boundary.",
        "v27 is no longer current; v28 is the current 60 s full-run package.",
        "v27 is not a completed 60 s Step5d reproduction claim.",
    ]


def normalize_retained_local_triplet_evidence(evidence: dict[str, Any], current_label: str) -> None:
    current_key = f"{current_label}_local_triplet"
    for key, value in list(evidence.items()):
        if key == current_key or not key.endswith("_local_triplet"):
            continue
        if not isinstance(value, str) or "step5d_strict_rnn" not in value:
            continue
        evidence[key] = str(STEP5D_ARCHIVE_DIR / Path(value).name)


def normalize_retained_controller_target_evidence(evidence: dict[str, Any], current_label: str) -> None:
    current_key = f"{current_label}_controller_target"
    for key, value in list(evidence.items()):
        if key == current_key or not key.endswith("_controller_target"):
            continue
        if not isinstance(value, str) or "step5d_strict_rnn" not in value:
            continue
        target_path = PurePosixPath(value)
        if target_path.parent.name == "step5d":
            continue
        evidence[key] = archived_controller_target(value)


def update_current_stage(
    root: Path,
    current: dict[str, Any],
    program: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    payload = copy.deepcopy(current)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    validation = manifest["validation"]
    sha = manifest["sha256"]["local"]
    target_dir = manifest["target_dir"]
    label = version_label(program)
    is_ablation = program in STEP5D_ABLATION_PROGRAMS
    step5b_speedl_live = program in STEP5D_STEP5B_SPEEDL_LIVE_PROGRAMS
    stage25_default_mode = "speedj_rnn_live" if program == STEP5D_ABLATION_V29 else "speedl_cartesian_oracle"
    stage25_target_s = stage25_success_target_s(program)
    stage25_limit_s = stage25_runtime_limit_s(program)
    hard_raw_guard_n, hard_force_guard_n, hard_torque_guard_nm = sensor_hard_guards(program)
    entry_gate = preload_gate(program)
    payload.update(
        {
            "updated_at": now,
            "current_step": "Step5d",
            "current_stage_id": program,
            "program": program,
            "controller_target": f"{target_dir}/{program}.urp",
            "controller_script": f"{target_dir}/{program}.script",
            "local_triplet": f"{STEP5D_CURRENT_DIR}/{program}",
            "delivery_manifest": manifest["manifest_path"],
            "controller_readback_manifest": manifest["manifest_path"],
            "status": f"{program}_controller_readback_verified_pending_live_bridge_run_not_reproduction_claim",
            "sha256": sha,
        }
    )
    bridge = payload.setdefault("bridge_profile", {})
    bridge.update(
        {
            "step4e_version": program,
            "stage25_3_preload_gate": (
                (
                    f"filtered {entry_gate['filtered_normal_load_min_n']:g}-{entry_gate['filtered_normal_load_max_n']:g}N, "
                    f"raw sanity {entry_gate['raw_normal_load_min_n']:g}-{entry_gate['raw_normal_load_max_n']:g}N, "
                    f"force_norm <={entry_gate['force_norm_max_n']:g}N, "
                    f"bridge param-valid code {entry_gate['param_valid_code']:g} for {entry_gate['required_s']:.3f}s"
                )
                if is_ablation
                else (
                    "filtered 7.5-14N, raw sanity 7-15N, force_norm <=25N, "
                    "bridge param-valid code 521.0 for 0.100s"
                )
            ),
        }
    )
    if is_ablation:
        bridge["stage25_95_register_clear_barrier"] = (
            "TP writes stage 25.95 after preload; bridge writes zero command/cmd_valid=0 and a layout tag "
            "that is not 521/523/524 until TP observes registers 37..47 clear before Stage25.0 consumption"
        )
        bridge["stage25_control_mode"] = stage25_default_mode
        bridge["stage25_layout_contract"] = {
            "cartesian_layout_tag": 523.0,
            "cartesian_registers": "37..42 vx/vy/vz/wx/wy/wz, TP executes speedl",
            "joint_layout_tag": 524.0,
            "joint_registers": "37..42 qd0..qd5, TP executes speedj",
        }
        bridge["cage_primary_policy"] = (
            "In speedl_cartesian_oracle, low-load re-press is bounded: <2N for 0.100s hard-stops, "
            "<5N for 0.500s stops, while cage margin, force norm, and actual speed remain hard stops."
        )
        bridge.pop("stage25_0_diagnostic_window_s", None)
        bridge["stage25_0_success_target_s"] = stage25_target_s
        bridge["sensor_hard_guards"] = {
            "raw_normal_n": hard_raw_guard_n,
            "force_norm_n": hard_force_guard_n,
            "torque_norm_nm": hard_torque_guard_nm,
        }
        bridge["stage25_success_target_s"] = stage25_target_s
        bridge["stage25_runtime_limit_s"] = stage25_limit_s
        if step5b_speedl_live:
            if program == STEP5D_ABLATION_V29:
                bridge["stage25_speedj_live_source"] = (
                    "strict RNN qdot from feasibility-scaled xdot_c is sent to TP speedj on layout 524; "
                    "speedl_cartesian_oracle and speedj_dls_oracle are explicit debug/fallback modes"
                )
                bridge["stage25_rnn_evidence_policy"] = (
                    "RNN accepted rows require layout 524, cmd_valid=1, TP echo consumption, and "
                    "_step5d_rnn_accepted=1; non-safety evidence rejection records diagnostics and "
                    "uses bounded hold without treating the row as accepted evidence."
                )
                bridge.pop("stage25_speedl_live_source", None)
                bridge.pop("stage25_speedl_orientation_policy", None)
            else:
                bridge["stage25_speedl_live_source"] = (
                    "Step5b speedl live vx/vy/vz; live wx/wy/wz forced to zero; Step5d paper/RNN outputs shadow-only"
                )
                bridge["stage25_speedl_orientation_policy"] = (
                    f"{label} bridge runtime uses Step5b speedl vx/vy/vz as the live command source "
                    "in speedl_cartesian_oracle; Step5d paper/RNN vx/vy/vz and wx/wy/wz are "
                    "shadow diagnostics, and live wx/wy/wz are forced to 0 for all Stage25.0."
                )
            bridge["stage25_cadence_consumption_instrumentation"] = {
                "max_row_gap_s": 0.020,
                "tp_consumed_echo_register": 47,
                "bridge_csv_fields": [
                    "_step5d_stage25_echo_consumed",
                    "_step5d_stage25_row_gap_s",
                    "_bridge_loop_rtde_send_s",
                ],
            }
        for stale_key in (
            "stage25_95_qdot_clear_barrier",
            "stage25_post_rnn_normal_guard",
            "stage25_low_load_policy",
            "stage25_post_rnn_tracking_guard",
            "trusted_force_summary",
            "step5d_reacquire_predicted_tcp_speed_cap_m_s",
        ):
            bridge.pop(stale_key, None)
    elif label == "v22":
        bridge["stage25_95_qdot_clear_barrier"] = (
            "TP writes stage 25.95 after preload; bridge writes zero qdot/cmd_valid=0 and a non-521 "
            "layout tag until TP observes 37..47 clear before Stage25.0 speedj consumption"
        )
    elif label == "v23" or label == "v24":
        bridge["stage25_95_qdot_clear_barrier"] = (
            "TP writes stage 25.95 after preload; bridge writes zero qdot/cmd_valid=0 and a non-521 "
            "layout tag until TP observes 37..47 clear and qdot registers 37..42 are near zero before Stage25.0 speedj consumption"
        )
        bridge["stage25_post_rnn_normal_guard"] = (
            "Bridge preserves the RNN as object under test, then holds/stops any over-target post-RNN command "
            "whose predicted or actual TCP motion presses into the surface."
        )
        if label == "v24":
            bridge["cage_primary_policy"] = (
                "Low-load/no-contact inside the TCP cage freezes path_time, writes zero qdot, "
                "and stops after 0.050 s instead of executing active_reacquire_solver qdot; "
                "the broad AABB cage remains a diagnostic boundary."
            )
            bridge["sensor_hard_guards"] = {
                "raw_normal_n": 25.0,
                "force_norm_n": 25.0,
                "torque_norm_nm": 4.0,
            }
            bridge.pop("step5d_reacquire_predicted_tcp_speed_cap_m_s", None)
            bridge["stage25_low_load_policy"] = (
                "Low-load/no-contact no longer executes active_reacquire_solver qdot; bridge freezes path_time, "
                "writes zero qdot, and stops after 0.050 s if contact is not recovered."
            )
            bridge["stage25_post_rnn_tracking_guard"] = (
                "Bridge holds/stops if outer_xdot_limited projects into the surface while J(q)qdot projects away from it."
            )
            bridge["trusted_force_summary"] = (
                "v24 summary force stats use baseline-ready trusted samples; raw_all_* stats retain startup artifacts."
            )
    else:
        bridge.pop("stage25_95_qdot_clear_barrier", None)
        bridge.pop("stage25_95_register_clear_barrier", None)
        bridge.pop("stage25_post_rnn_normal_guard", None)
        bridge.pop("stage25_low_load_policy", None)
        bridge.pop("stage25_post_rnn_tracking_guard", None)
        bridge.pop("trusted_force_summary", None)
        bridge.pop("stage25_control_mode", None)
        bridge.pop("stage25_layout_contract", None)
    evidence = payload.setdefault("evidence", {})
    evidence.update(
        {
            "v20_retained_after_live_attempt": True,
            "v20_live_attempts": live_attempt_evidence(root, "step5d_strict_rnn_liveprep_v20"),
            f"{label}_stamp": validation.get("stamp"),
            f"{label}_local_package_validated": True,
            f"{label}_controller_readback_verified": True,
            f"{label}_controller_readback_dir": str(Path(manifest["manifest_path"]).parent),
            f"{label}_controller_readback_manifest": manifest["manifest_path"],
            f"{label}_controller_target": f"{target_dir}/{program}.urp",
            f"{label}_local_triplet": f"{STEP5D_CURRENT_DIR}/{program}",
            f"{label}_delivery_status": "controller read-back verified",
            f"{label}_preload_gate": preload_gate(program),
            f"{label}_sha256": sha,
            "sha256": sha,
        }
    )
    normalize_retained_local_triplet_evidence(evidence, label)
    normalize_retained_controller_target_evidence(evidence, label)
    if label == "v22":
        evidence["v21_retained_after_live_failure"] = True
        evidence["v21_live_attempts"] = live_attempt_evidence(root, "step5d_strict_rnn_liveprep_v21")
        evidence["v22_qdot_clear_barrier"] = {
            "stage": 25.95,
            "required_s": 0.006,
            "timeout_s": 1.0,
            "clears_input_float_registers": "37..47",
            "rejects_preload_layout_tag": 521.0,
            "bridge_clear_mode_code": 522.0,
        }
    if label == "v23":
        evidence["v22_retained_after_live_failure"] = True
        evidence["v22_live_attempts"] = live_attempt_evidence(root, "step5d_strict_rnn_liveprep_v22")
        evidence["v23_qdot_clear_barrier"] = {
            "stage": 25.95,
            "required_s": 0.006,
            "timeout_s": 1.0,
            "qdot_zero_tol_rad_s": 0.0005,
            "clears_input_float_registers": "37..47",
            "rejects_preload_layout_tag": 521.0,
            "bridge_clear_mode_code": 522.0,
        }
        evidence["v23_post_rnn_normal_guard"] = {
            "hold_load_n": 14.0,
            "directional_stop_load_n": 18.0,
            "hard_stop_load_n": 25.0,
            "hard_stop_force_norm_n": 25.0,
            "predicted_press_hold_m_s": 0.0005,
            "actual_press_hold_m_s": 0.0010,
            "normal_load_rate_hold_n_s": 20.0,
            "directional_stop_dwell_s": 0.004,
        }
    if label == "v24":
        evidence["v23_retained_after_live_failure"] = True
        evidence["v23_live_attempts"] = live_attempt_evidence(root, "step5d_strict_rnn_liveprep_v23")
        evidence["v24_qdot_clear_barrier"] = {
            "stage": 25.95,
            "required_s": 0.006,
            "timeout_s": 1.0,
            "qdot_zero_tol_rad_s": 0.0005,
            "clears_input_float_registers": "37..47",
            "rejects_preload_layout_tag": 521.0,
            "bridge_clear_mode_code": 522.0,
        }
        evidence["v24_low_load_policy"] = {
            "active_reacquire_solver_qdot_executed": False,
            "low_load_hold_timeout_s": 0.050,
            "hold_command": "zero_qdot",
        }
        evidence["v24_post_rnn_tracking_guard"] = {
            "outer_press_min_m_s": 0.0001,
            "unload_min_m_s": 0.0005,
            "opposed_dwell_stop_s": 0.004,
            "hard_stop_load_n": 25.0,
            "hard_stop_force_norm_n": 25.0,
        }
        evidence["v24_trusted_summary_policy"] = {
            "primary_force_stats": "baseline_ready trusted samples",
            "raw_all_force_stats": "retained for startup artifact audit",
        }
    if is_ablation:
        evidence[f"v24_retained_after_superseded_by_{label}_ablation"] = True
        evidence["v24_live_attempts"] = live_attempt_evidence(root, "step5d_strict_rnn_liveprep_v24")
        evidence[f"{label}_stage25_control_modes"] = [
            "speedl_cartesian_oracle",
            "speedj_dls_oracle",
            "speedj_rnn_live",
        ]
        evidence[f"{label}_register_clear_barrier"] = {
            "stage": 25.95,
            "required_s": 0.006,
            "timeout_s": 1.0,
            "register_zero_tol": 0.0005,
            "clears_input_float_registers": "37..47",
            "rejects_layout_tags": [521.0, 523.0, 524.0],
        }
        evidence[f"{label}_stage25_layout_tags"] = {
            "cartesian_speedl": 523.0,
            "joint_speedj": 524.0,
        }
        evidence[f"{label}_speedl_low_load_policy"] = {
            "hard_stop_below_n": 2.0,
            "hard_stop_s": 0.100,
            "soft_stop_below_n": 5.0,
            "soft_stop_s": 0.500,
        }
        if step5b_speedl_live:
            evidence["v27_20260706_045513_fix_validation_success"] = {
                "run_dir": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513",
                "summary": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513/summary.json",
                "analysis": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513/step5d_bridge_analysis.json",
                "bridge_csv": "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_045513/bridge_rtde_500hz.csv",
                "result": "successful_10s_fix_validation_not_60s_reproduction",
                "analysis_classification": "stage25_fix_validation_success",
                "fix_validation_status": "passed_10s_stage25_window",
                "reproduction_status": "pending_60s_step5b_equivalent_run",
                "stage25_duration_s": 10.215999410953373,
                "stage25_consumption_ratio": 0.9982384028185555,
                "live_control_source": "step5b_speedl_live_step5d_shadow",
                "angular_cmd_norm_max_rad_s": 0.0,
                "shadow_raw_angular_cmd_norm_max_rad_s": 0.015000000048523384,
                "normal_load_min_n": 10.0455088,
                "normal_load_max_n": 14.4362819,
                "force_norm_max_n": 14.4401489,
                "next_action": "v28 60s full-run live bridge evidence",
            }
    strict = payload.setdefault("strict_rnn_status", {})
    strict["reason"] = (
        f"{label} TP/script cage-primary diagnostic package is generated, uploaded, "
        "and controller read-back verified. Full Step5d reproduction remains incomplete "
        "until a successful live run completes."
    )
    trigger = payload.setdefault("bridge_trigger", {})
    trigger["bridge_has_started"] = False
    trigger["live_motion_authorized"] = False
    trigger["blocked_reason"] = (
        f"{label} package delivery is complete and controller read-back verified; live bridge start "
        "is outside this offline/package action and still requires an explicit live trigger."
    )
    trigger["required_before_live"] = [
        "operator outside UR reach/cage boundary",
        f"TP program opened on controller read-back {label} package",
    ]
    if program == STEP5D_ABLATION_V29:
        trigger.pop("no_contact_p0_capture", None)
        trigger["required_before_live"].extend(
            [
                "v29 package readback verified on controller",
                "6 Codex + 1 fable5 offline audit accepted, or user waives that gate",
                "explicit live/contact authorization for v29 speedj_rnn_live",
            ]
        )
    for retained in payload.get("retained_steps", []):
        if retained.get("step") == "Step5":
            retained["role"] = (
                f"{label} TP/script package generated, controller read-back verified, "
                f"and selected as current cage-primary diagnostic package; earlier Step5d packages retained as evidence"
            )
    payload["notes"] = [
        f"{program} is controller read-back verified and selected as the current Step5d TP/script cage-primary diagnostic package.",
        f"{label} keeps Stage22/24 gravity-down [pi,0,0] pre-contact search posture.",
        (
            f"{label} is an ablation package: v25/v26/v27/v28 default to speedl_cartesian_oracle; v29 defaults to speedj_rnn_live; preload is {entry_gate['filtered_normal_load_min_n']:g}-{entry_gate['filtered_normal_load_max_n']:g}N filtered with {entry_gate['raw_normal_load_min_n']:g}-{entry_gate['raw_normal_load_max_n']:g}N raw sanity."
            if is_ablation
            else
            f"{label} stops low-load/no-contact with zero qdot instead of executing active_reacquire_solver qdot, and uses Stage25.3 default preload 7.5-14N filtered with 7-15N raw sanity."
            if label == "v24"
            else f"{label} keeps v20 cage-primary active-reacquire policy and uses Stage25.3 default preload 7.5-14N filtered with 7-15N raw sanity."
        ),
        (
            f"{label} uses Stage25.95 register-clear barrier plus layout tag 523.0 for Cartesian speedl and 524.0 for joint speedj."
            if is_ablation
            else
            "v22 adds Stage25.95 qdot-clear barrier so Stage25.0 cannot consume stale Stage25.3 preload registers as qdot."
            if label == "v22"
            else "v24 keeps Stage25.95 near-zero qdot clear, lowers raw/force hard guards to 25N, and adds post-RNN tracking reversal detection."
            if label == "v24"
            else "v23 keeps Stage25.95 qdot-clear and tightens it to near-zero qdot before Stage25.0 speedj consumption."
            if label == "v23"
            else "This package has no Stage25.95 qdot-clear barrier."
        ),
        (
            f"{label} Stage25 success target is {stage25_target_s:g}s with a {stage25_limit_s:g}s runtime limit."
            if is_ablation
            else "This package keeps the retained runtime limit."
        ),
        (
            "v23 adds a post-RNN normal-direction guard so over-target commands that press into the surface hold/stop before the 100N sensor hard guard."
            if label == "v23"
            else "v24 keeps the post-RNN normal-direction guard and adds a tracking guard for outer-loop press vs J(q)qdot unload disagreement."
            if label == "v24"
            else "No v23 post-RNN normal-direction guard is active for this package."
        ),
        "This file is the single current pointer for UR/Kunwei package and bridge handoffs.",
        "TP program load/Play, robot motion, payload/TCP writes, and zero_ftsensor remain explicit live gates.",
        "Earlier Step5d live-prep packages remain retained evidence only.",
    ]
    return payload


def promote(root: Path, program: str, target_dir: str, local_dir: Path, manifest_path: Path | None) -> dict[str, Any]:
    if not program.startswith(STEP5D_PACKAGE_PREFIXES):
        fail(f"refusing non-Step5d TP package: {program}")
    manifest_path = manifest_path or latest_manifest(root, program)
    manifest = validate_manifest(root, program, target_dir, manifest_path)
    current_path = root / "config" / "current_stage.json"
    table_path = root / "config" / "step5_stage_table.json"
    current = load_json(current_path)
    table = load_json(table_path)
    target_row = find_stage(table, program)
    promotion_evidence = None
    if program == STEP5D_ABLATION_V30:
        promotion_evidence = verify_v30_evidence_freeze(
            root,
            current,
            target_row,
            expected_package_sha256=manifest["sha256"]["local"],
            expected_readback_manifest=manifest["manifest_path"],
        )
    formal_files = ensure_formal_local_triplet(root, program, local_dir, manifest["sha256"]["local"])
    previous = current.get("program") or current.get("current_stage_id")
    previous_row = find_stage(table, str(previous)) if previous else None
    base_row = copy.deepcopy(target_row or previous_row or {"stage": "Step5d", "owner": "bridge+TP"})
    if previous and previous != program:
        if program == STEP5D_ABLATION_V30 and previous == STEP5D_ABLATION_V29:
            freeze_v29_fallback_for_v30(table)
        else:
            update_previous_stage(root, table, str(previous), current, program)
    if program == STEP5D_ABLATION_V30:
        if not isinstance(promotion_evidence, dict):
            fail("v30 promotion evidence unexpectedly missing after validation")
        current_row = build_v30_current_stage_row(base_row, manifest)
        package_delivery = current_row.setdefault("package_delivery", {})
        package_delivery["controller_readback_manifest_sha256"] = sha256_file(
            root / manifest["manifest_path"]
        )
    else:
        current_row = build_current_stage_row(base_row, program, manifest)
    upsert_stage(table, current_row, after_id=str(previous) if previous else None)
    update_bridge_startup_policy(table, program)
    normalize_retained_stage_metadata(table, program)
    normalize_v27_fix_validation_metadata(root, table, program)
    if program == STEP5D_ABLATION_V30:
        assert isinstance(promotion_evidence, dict)
        new_current = update_v30_current_stage(current, manifest, promotion_evidence)
    else:
        new_current = update_current_stage(root, current, program, manifest)
    write_json(table_path, table)
    write_json(current_path, new_current)
    return {
        "ok": True,
        "program": program,
        "previous_program": previous,
        "manifest": manifest["manifest_path"],
        "formal_local_triplet": [rel(root, path) for path in formal_files.values()],
        "current_stage": rel(root, current_path),
        "stage_table": rel(root, table_path),
        "promotion_evidence": promotion_evidence,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--program", required=True)
    parser.add_argument("--target-dir", default=TARGET_DIR)
    parser.add_argument("--local-dir", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    local_dir = args.local_dir or (root / STEP5D_CURRENT_DIR)
    if not local_dir.is_absolute():
        local_dir = root / local_dir
    manifest_path = args.manifest
    if manifest_path is not None and not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    result = promote(root, args.program, args.target_dir, local_dir, manifest_path)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"promoted {result['program']} using {result['manifest']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"refusing Step5d promotion: {exc}")
        raise SystemExit(24)
