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


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = (".script", ".txt", ".urp")
STEP5D_ARCHIVE_DIR = Path("programs/step5/step5d")
STEP5D_CURRENT_DIR = Path("programs/step5")
TARGET_DIR = "/programs/andyl/kunwei/step5"


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
    prefix = "step5d_strict_rnn_liveprep_"
    if not program.startswith(prefix):
        fail(f"program is not a Step5d liveprep id: {program}")
    return program[len(prefix):]


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
    else:
        root_cause = (
            f"The {label} TP package was controller read-back verified, but no successful "
            "Step5d reproduction completion was recorded before it was superseded."
        )
    return {
        "attempts": attempts,
        "latest_run_dir": latest.get("run_dir"),
        "latest_stop_reason": latest.get("stop_reason"),
        "result": "retained incomplete live-attempt evidence; no successful Step5d reproduction completion was recorded",
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
    contact_policy = row.setdefault("contact_policy", {})
    contact_policy["live_authorization"] = "retained_live_attempt_evidence_no_current_retry_authorization"
    contact_policy["tp_package_status"] = "retained_controller_readback_verified"
    contact_policy["controller_readback_status"] = "verified_retained"
    cadence = row.setdefault("cadence", {})
    cadence["motion"] = "retained_incomplete_live_attempt_evidence"
    row["notes"] = [
        f"{previous_label} was controller read-back verified but is retained as failed live-attempt evidence.",
        f"{previous_label} is no longer current; open {successor_label} from the Step5 root on the Teach Pendant.",
        f"{previous_label} local triplet is archived under {STEP5D_ARCHIVE_DIR}.",
    ]
    return row


def preload_gate(program: str) -> dict[str, float]:
    label = version_label(program)
    if label not in {"v21", "v22", "v23", "v24"}:
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
    if label == "v24":
        gate.update(
            {
                "recovery_normal_load_max_n": 20.0,
                "force_norm_stop_n": 25.0,
            }
        )
    return gate


def build_current_stage_row(
    base_row: dict[str, Any],
    program: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    label = version_label(program)
    row = copy.deepcopy(base_row)
    row["id"] = program
    row["active"] = True
    row["blocked"] = False
    row["complete"] = False
    row["completion_target"] = True
    row["block_reason"] = (
        f"Current {label} cage-primary TP/script diagnostic package is generated, uploaded, "
        "and controller read-back verified. It is ready for an explicit live bridge run; "
        "not a completed reproduction claim."
    )
    row["live_run_evidence"] = None
    validation = manifest["validation"]
    sha = manifest["sha256"]["local"]
    row["local_delivery_evidence"] = {
        "program_basename": program,
        "local_program_dir": str(STEP5D_CURRENT_DIR),
        "local_triplet": f"{STEP5D_CURRENT_DIR}/{program}.{{script,txt,urp}}",
        "controller_target": f"{manifest['target_dir']}/{program}.urp",
        "controller_dir": manifest["target_dir"],
        "local_package_validated": True,
        "controller_readback_verified": True,
        "controller_readback": manifest["manifest_path"],
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
    guard = row.setdefault("guard", {})
    guard.update(
        {
            "line_entry_normal_load_min_n": 7.5,
            "line_entry_normal_load_max_n": 14.0,
            "line_entry_raw_sanity_min_n": 7.0,
            "line_entry_raw_sanity_max_n": 15.0,
            "line_entry_force_norm_max_n": 25.0,
            "line_entry_required_s": 0.1,
            "line_entry_param_valid_code": 521.0,
            "line_entry_recovery_normal_load_max_n": 20.0 if label == "v24" else 40.0,
            "line_entry_force_norm_stop_n": 25.0 if label == "v24" else 100.0,
            "stage25_95_qdot_clear_required_s": 0.006 if label in {"v22", "v23", "v24"} else None,
            "stage25_95_qdot_clear_timeout_s": 1.0 if label in {"v22", "v23", "v24"} else None,
            "stage25_95_qdot_clear_zero_tol_rad_s": 0.0005 if label in {"v23", "v24"} else None,
            "stage25_post_rnn_normal_guard_hold_load_n": 14.0 if label in {"v23", "v24"} else None,
            "stage25_post_rnn_normal_guard_directional_stop_load_n": 18.0 if label in {"v23", "v24"} else None,
            "stage25_post_rnn_normal_guard_hard_stop_load_n": 25.0 if label in {"v23", "v24"} else None,
            "stage25_post_rnn_normal_guard_hard_stop_force_norm_n": 25.0 if label in {"v23", "v24"} else None,
            "stage25_low_load_hold_timeout_s": 0.050 if label == "v24" else None,
            "stage25_tracking_guard_opposed_dwell_s": 0.004 if label == "v24" else None,
            "stage25_tracking_guard_outer_press_min_m_s": 0.0001 if label == "v24" else None,
            "stage25_tracking_guard_unload_min_m_s": 0.0005 if label == "v24" else None,
            "raw_normal_guard_n": 25.0 if label == "v24" else 100.0,
            "force_norm_guard_n": 25.0 if label == "v24" else 100.0,
            "torque_norm_guard_nm": 4.0,
            "target_force_n": 12.0,
            "duration_s": 10.0,
        }
    )
    for key in (
        "stage25_95_qdot_clear_required_s",
        "stage25_95_qdot_clear_timeout_s",
        "stage25_95_qdot_clear_zero_tol_rad_s",
        "stage25_post_rnn_normal_guard_hold_load_n",
        "stage25_post_rnn_normal_guard_directional_stop_load_n",
        "stage25_post_rnn_normal_guard_hard_stop_load_n",
        "stage25_post_rnn_normal_guard_hard_stop_force_norm_n",
        "stage25_low_load_hold_timeout_s",
        "stage25_tracking_guard_opposed_dwell_s",
        "stage25_tracking_guard_outer_press_min_m_s",
        "stage25_tracking_guard_unload_min_m_s",
    ):
        if guard.get(key) is None:
            guard.pop(key, None)
    contact_policy = row.setdefault("contact_policy", {})
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
    contact_policy["stage25_contact_policy"] = stage25_policy
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
        "and awaits explicit live bridge run evidence before any reproduction claim."
    )
    row["notes"] = [
        f"{label} is controller read-back verified and selected as the current Step5d TP/script diagnostic package.",
        f"{label} keeps Stage22/24 gravity-down [pi,0,0] pre-contact search posture.",
        (
            f"{label} stops low-load/no-contact with zero qdot instead of executing active_reacquire_solver qdot."
            if label == "v24"
            else f"{label} uses the retained cage-primary active-reacquire diagnostic policy."
        ),
        (
            f"{label} uses 25N raw-normal/force-norm hard guards and post-RNN tracking reversal detection."
            if label == "v24"
            else f"{label} uses retained raw-normal/force-norm hard guards."
        ),
        "This current row is not a completed Step5d reproduction claim; explicit live bridge evidence is still pending.",
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
    analysis[f"{label}_controller_readback_manifest"] = manifest["manifest_path"]
    cadence = row.setdefault("cadence", {})
    cadence["motion"] = "pending_live_diagnostic"
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
                "filtered 7.5-14N, raw sanity 7-15N, force_norm <=25N, "
                "bridge param-valid code 521.0 for 0.100s"
            ),
        }
    )
    if label == "v22":
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
        bridge.pop("stage25_post_rnn_normal_guard", None)
        bridge.pop("stage25_low_load_policy", None)
        bridge.pop("stage25_post_rnn_tracking_guard", None)
        bridge.pop("trusted_force_summary", None)
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
    strict = payload.setdefault("strict_rnn_status", {})
    strict["reason"] = (
        f"{label} TP/script cage-primary diagnostic package is generated, uploaded, "
        "and controller read-back verified. Full Step5d reproduction remains incomplete "
        "until a successful live run completes."
    )
    trigger = payload.setdefault("bridge_trigger", {})
    trigger["bridge_has_started"] = False
    trigger["required_before_live"] = [
        "operator outside UR reach/cage boundary",
        f"TP program opened on controller read-back {label} package",
    ]
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
            f"{label} stops low-load/no-contact with zero qdot instead of executing active_reacquire_solver qdot, and uses Stage25.3 default preload 7.5-14N filtered with 7-15N raw sanity."
            if label == "v24"
            else f"{label} keeps v20 cage-primary active-reacquire policy and uses Stage25.3 default preload 7.5-14N filtered with 7-15N raw sanity."
        ),
        (
            "v22 adds Stage25.95 qdot-clear barrier so Stage25.0 cannot consume stale Stage25.3 preload registers as qdot."
            if label == "v22"
            else "v24 keeps Stage25.95 near-zero qdot clear, lowers raw/force hard guards to 25N, and adds post-RNN tracking reversal detection."
            if label == "v24"
            else "v23 keeps Stage25.95 qdot-clear and tightens it to near-zero qdot before Stage25.0 speedj consumption."
            if label == "v23"
            else "This package has no Stage25.95 qdot-clear barrier."
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
    if not program.startswith("step5d_strict_rnn_liveprep_"):
        fail(f"refusing non-Step5d liveprep program: {program}")
    manifest_path = manifest_path or latest_manifest(root, program)
    manifest = validate_manifest(root, program, target_dir, manifest_path)
    formal_files = ensure_formal_local_triplet(root, program, local_dir, manifest["sha256"]["local"])
    current_path = root / "config" / "current_stage.json"
    table_path = root / "config" / "step5_stage_table.json"
    current = load_json(current_path)
    table = load_json(table_path)
    previous = current.get("program") or current.get("current_stage_id")
    previous_row = find_stage(table, str(previous)) if previous else None
    base_row = copy.deepcopy(previous_row) if previous_row is not None else {"stage": "Step5d", "owner": "bridge+TP"}
    if previous and previous != program:
        update_previous_stage(root, table, str(previous), current, program)
    current_row = build_current_stage_row(base_row, program, manifest)
    upsert_stage(table, current_row, after_id=str(previous) if previous else None)
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
