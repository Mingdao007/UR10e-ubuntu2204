#!/usr/bin/env python3
"""Build fail-closed same-run Gazebo v2 evidence and observer review.

The runtime capture adapter must normalize Gazebo transport messages into:

* ``native_contact.jsonl`` rows with run_id, sim_time_s, source, topic,
  contact_collision, surface_collision, and native_wrench (six values).
* ``native_ft.jsonl`` rows with run_id, sim_time_s, source, topic,
  sensor_joint, and native_wrench (six values).
* ``tick_trace.jsonl`` rows conforming to ur10e_gazebo_v2_tick_v1.

This builder never synthesizes force from surface penetration or kinematics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from ur10e_example_controllers.ur10e_gazebo_v2 import (  # noqa: E402
    EOAT_CONTACT_COLLISION,
    EOAT_FIXED_JOINT,
    EOAT_GEOMETRY_FIDELITY,
    NATIVE_CONTACT_TOPIC,
    NATIVE_FT_TOPIC,
    backend_spec,
    validate_tick_record,
)


SCHEMA = "ur10e_gazebo_v2_same_run_evidence_v1"
OBSERVER_SCHEMA = "ur10e_gazebo_v2_observer_review_v1"
REQUIRED_VIEWS = ("wide", "oblique", "close", "contact")
REQUIRED_MANUAL_CHECKS = (
    "ur10e_arm_visible",
    "eoat_chain_attached_to_arm_visible",
    "contact_surface_visible",
    "contact_relationship_visible",
)
CORRELATION_TOLERANCE_S = 0.004
EXPECTED_CONTROL_PERIOD_S = 0.002


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    if not path.is_file():
        return rows, [f"missing:{path.name}"]
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"{path.name}:line_{index}:json:{exc.msg}")
            continue
        if not isinstance(value, dict):
            issues.append(f"{path.name}:line_{index}:not_object")
            continue
        rows.append(value)
    return rows, issues


def write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _vector6(value: Any) -> list[float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)) or len(value) != 6:
        return None
    if not all(_finite(item) for item in value):
        return None
    return [float(item) for item in value]


def _force_norm(wrench: Sequence[float]) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in wrench[:3]))


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def validate_contact_rows(rows: Sequence[Mapping[str, Any]], *, run_id: str) -> list[str]:
    issues: list[str] = []
    if not rows:
        return ["native_contact_rows_zero"]
    for index, row in enumerate(rows):
        prefix = f"native_contact[{index}]"
        if row.get("run_id") != run_id:
            issues.append(f"{prefix}:run_id_mismatch")
        if row.get("source") != "gazebo_native_contact":
            issues.append(f"{prefix}:source_not_native_contact")
        if row.get("topic") != NATIVE_CONTACT_TOPIC:
            issues.append(f"{prefix}:topic_mismatch")
        if EOAT_CONTACT_COLLISION not in str(row.get("contact_collision") or ""):
            issues.append(f"{prefix}:attached_eoat_collision_missing")
        if "step5_contact_surface" not in str(row.get("surface_collision") or ""):
            issues.append(f"{prefix}:surface_collision_missing")
        if not _finite(row.get("sim_time_s")):
            issues.append(f"{prefix}:sim_time_invalid")
        wrench = _vector6(row.get("native_wrench"))
        if wrench is None:
            issues.append(f"{prefix}:native_wrench_invalid")
        elif _force_norm(wrench) <= 0.0:
            issues.append(f"{prefix}:native_wrench_zero")
        if "virtual_surface" in json.dumps(row, sort_keys=True).lower():
            issues.append(f"{prefix}:virtual_surface_forbidden")
    return issues


def validate_ft_rows(rows: Sequence[Mapping[str, Any]], *, run_id: str) -> list[str]:
    issues: list[str] = []
    if not rows:
        return ["native_ft_rows_zero"]
    for index, row in enumerate(rows):
        prefix = f"native_ft[{index}]"
        if row.get("run_id") != run_id:
            issues.append(f"{prefix}:run_id_mismatch")
        if row.get("source") != "gazebo_native_ft":
            issues.append(f"{prefix}:source_not_native_ft")
        if row.get("topic") != NATIVE_FT_TOPIC:
            issues.append(f"{prefix}:topic_mismatch")
        if row.get("sensor_joint") != EOAT_FIXED_JOINT:
            issues.append(f"{prefix}:attached_ft_joint_mismatch")
        if row.get("frame") != "child":
            issues.append(f"{prefix}:ft_frame_not_child")
        if row.get("measure_direction") != "child_to_parent":
            issues.append(f"{prefix}:ft_measure_direction_mismatch")
        if not _finite(row.get("sim_time_s")):
            issues.append(f"{prefix}:sim_time_invalid")
        wrench = _vector6(row.get("native_wrench"))
        if wrench is None:
            issues.append(f"{prefix}:native_wrench_invalid")
        if "virtual_surface" in json.dumps(row, sort_keys=True).lower():
            issues.append(f"{prefix}:virtual_surface_forbidden")
    return issues


def contact_ft_correlation(
    contacts: Sequence[Mapping[str, Any]],
    ft_rows: Sequence[Mapping[str, Any]],
    *,
    tolerance_s: float = CORRELATION_TOLERANCE_S,
) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    for contact in contacts:
        if not _finite(contact.get("sim_time_s")):
            continue
        contact_wrench = _vector6(contact.get("native_wrench"))
        if contact_wrench is None:
            continue
        stamp = float(contact["sim_time_s"])
        candidates = [row for row in ft_rows if _finite(row.get("sim_time_s"))]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda row: abs(float(row["sim_time_s"]) - stamp))
        ft_wrench = _vector6(nearest.get("native_wrench"))
        if ft_wrench is None:
            continue
        delta = abs(float(nearest["sim_time_s"]) - stamp)
        if delta > tolerance_s:
            continue
        contact_norm = _force_norm(contact_wrench)
        ft_norm = _force_norm(ft_wrench)
        if contact_norm <= 0.0 or ft_norm <= 0.0:
            continue
        pairs.append(
            {
                "contact_sim_time_s": stamp,
                "ft_sim_time_s": float(nearest["sim_time_s"]),
                "delta_s": delta,
                "contact_force_norm_n": contact_norm,
                "ft_force_norm_n": ft_norm,
                "norm_ratio": ft_norm / contact_norm,
            }
        )
    ratios = [row["norm_ratio"] for row in pairs]
    ratio_spread = None
    if ratios:
        mean = sum(ratios) / len(ratios)
        ratio_spread = max(abs(value - mean) for value in ratios) / max(abs(mean), 1e-12)
    passed = len(pairs) >= 2 and ratio_spread is not None and ratio_spread <= 0.25
    return {
        "pass": passed,
        "pair_count": len(pairs),
        "tolerance_s": tolerance_s,
        "basis": "same-run nearest sim-time pair plus rotation-invariant force-norm ratio consistency",
        "norm_ratio_relative_spread": ratio_spread,
        "pairs": pairs,
        "blockers": [] if passed else ["native_contact_ft_correlation_not_proven"],
    }


def validate_tick_rows(rows: Sequence[Mapping[str, Any]], *, run_id: str, backend: str) -> list[str]:
    issues: list[str] = []
    if not rows:
        return ["tick_rows_zero"]
    previous_sequence: int | None = None
    previous_time: float | None = None
    for index, row in enumerate(rows):
        for issue in validate_tick_record(row):
            issues.append(f"tick[{index}]:{issue}")
        if row.get("run_id") != run_id:
            issues.append(f"tick[{index}]:run_id_mismatch")
        if row.get("backend") != backend:
            issues.append(f"tick[{index}]:backend_mismatch")
        sequence = row.get("sequence")
        stamp = row.get("sim_time_s")
        if isinstance(sequence, int) and previous_sequence is not None and sequence != previous_sequence + 1:
            issues.append(f"tick[{index}]:sequence_gap")
        if _finite(stamp) and previous_time is not None:
            period = float(stamp) - previous_time
            if abs(period - EXPECTED_CONTROL_PERIOD_S) > 5e-5:
                issues.append(f"tick[{index}]:period_not_500hz")
        if isinstance(sequence, int):
            previous_sequence = sequence
        if _finite(stamp):
            previous_time = float(stamp)
    return issues


def validate_tf_lineage(payload: Mapping[str, Any], *, run_id: str) -> list[str]:
    issues: list[str] = []
    if payload.get("run_id") != run_id:
        issues.append("tf_lineage:run_id_mismatch")
    if payload.get("runtime_base_link_pose_present") is not True:
        issues.append("tf_lineage:runtime_base_link_pose_missing")
    if payload.get("pose_info_parse_issues"):
        issues.append("tf_lineage:pose_info_parse_issues_present")
    frames = payload.get("frames") if isinstance(payload.get("frames"), Mapping) else {}
    expected_parents = {
        "gazebo_world": None,
        "base_link": "gazebo_world",
        "base": "base_link",
        "base_link_inertia": "base_link",
        "shoulder_link": "base_link_inertia",
        "upper_arm_link": "shoulder_link",
        "forearm_link": "upper_arm_link",
        "wrist_1_link": "forearm_link",
        "wrist_2_link": "wrist_1_link",
        "wrist_3_link": "wrist_2_link",
        "flange": "wrist_3_link",
        "tool0": "flange",
        "real_aligned_eoat_visual_stack": "tool0",
        "active_tcp": "tool0",
    }
    for frame, parent in expected_parents.items():
        row = frames.get(frame) if isinstance(frames, Mapping) else None
        if not isinstance(row, Mapping):
            issues.append(f"tf_lineage:missing:{frame}")
        elif row.get("parent") != parent:
            issues.append(f"tf_lineage:parent_mismatch:{frame}")
        elif not str(row.get("source") or "") or str(row.get("source") or "").startswith("missing_"):
            issues.append(f"tf_lineage:source_missing:{frame}")
    base_row = frames.get("base") if isinstance(frames, Mapping) else None
    rpy = base_row.get("rpy_rad") if isinstance(base_row, Mapping) else None
    try:
        base_rotation_valid = isinstance(rpy, Sequence) and len(rpy) == 3 and abs(float(rpy[2]) - math.pi) <= 1e-9
    except (TypeError, ValueError):
        base_rotation_valid = False
    if not base_rotation_valid:
        issues.append("tf_lineage:base_to_base_link_rotation_mismatch")
    return issues


def _artifact_rows(run_dir: Path, names: Sequence[str]) -> list[dict[str, Any]]:
    rows = []
    for name in names:
        path = run_dir / name
        rows.append(
            {
                "path": str(path),
                "exists": path.is_file(),
                "size": path.stat().st_size if path.is_file() else None,
                "sha256": sha256_file(path) if path.is_file() else None,
            }
        )
    return rows


def camera_png_metadata(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"valid": False, "width": None, "height": None, "blockers": []}
    try:
        data = path.read_bytes()
    except OSError:
        result["blockers"].append("image_unreadable")
        return result
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        result["blockers"].append("image_not_png")
        return result
    offset = 8
    saw_idat = False
    saw_iend = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_start = offset + 8
        chunk_end = chunk_start + length
        crc_end = chunk_end + 4
        if crc_end > len(data):
            result["blockers"].append("png_chunk_truncated")
            break
        chunk = data[chunk_start:chunk_end]
        expected_crc = struct.unpack(">I", data[chunk_end:crc_end])[0]
        if zlib.crc32(chunk_type + chunk) & 0xFFFFFFFF != expected_crc:
            result["blockers"].append("png_crc_invalid")
            break
        if chunk_type == b"IHDR":
            if length != 13:
                result["blockers"].append("png_ihdr_invalid")
                break
            result["width"], result["height"] = struct.unpack(">II", chunk[:8])
        elif chunk_type == b"IDAT":
            saw_idat = True
        elif chunk_type == b"IEND":
            saw_iend = True
            break
        offset = crc_end
    if result["width"] is None or result["height"] is None:
        result["blockers"].append("png_dimensions_missing")
    elif result["width"] < 640 or result["height"] < 360:
        result["blockers"].append("png_resolution_below_640x360")
    if not saw_idat:
        result["blockers"].append("png_idat_missing")
    if not saw_iend:
        result["blockers"].append("png_iend_missing")
    result["blockers"] = _dedupe(result["blockers"])
    result["valid"] = not result["blockers"]
    return result


def build_observer_review(
    run_dir: Path,
    *,
    run_id: str,
    native_same_run_pass: bool,
    tf_lineage_pass: bool,
    sim_time_window: tuple[float, float] | None,
) -> dict[str, Any]:
    camera_manifest_path = run_dir / "camera_manifest.json"
    checks_path = run_dir / "observer_manual_checks.json"
    camera_manifest = load_json(camera_manifest_path) if camera_manifest_path.is_file() else {}
    checks = load_json(checks_path) if checks_path.is_file() else {}
    blockers: list[str] = []
    if camera_manifest.get("run_id") != run_id:
        blockers.append("camera_manifest_run_id_mismatch")
    views = camera_manifest.get("views") if isinstance(camera_manifest.get("views"), Mapping) else {}
    view_rows: dict[str, Any] = {}
    for name in REQUIRED_VIEWS:
        row = views.get(name) if isinstance(views, Mapping) else None
        path_value = row.get("path") if isinstance(row, Mapping) else None
        path = Path(str(path_value)) if path_value else None
        if path is not None and not path.is_absolute():
            path = run_dir / path
        image = camera_png_metadata(path) if path and path.is_file() else {"valid": False, "width": None, "height": None, "blockers": ["image_missing"]}
        sim_time_s = row.get("sim_time_s") if isinstance(row, Mapping) else None
        time_valid = bool(
            sim_time_window is not None
            and _finite(sim_time_s)
            and sim_time_window[0] <= float(sim_time_s) <= sim_time_window[1]
        )
        valid = bool(image["valid"] and time_valid)
        view_rows[name] = {
            "topic": f"/ur10e/gazebo_v2/camera/{name}",
            "path": str(path) if path else None,
            "exists": valid,
            "width": image["width"],
            "height": image["height"],
            "image_blockers": image["blockers"],
            "sim_time_s": sim_time_s,
            "same_tick_window": time_valid,
            "sha256": sha256_file(path) if valid and path is not None else None,
        }
        if not valid:
            blockers.append(f"observer_view_missing:{name}")
    if checks.get("run_id") != run_id:
        blockers.append("observer_manual_checks_run_id_mismatch")
    if not str(checks.get("reviewer") or ""):
        blockers.append("observer_reviewer_missing")
    if not str(checks.get("reviewed_at") or ""):
        blockers.append("observer_reviewed_at_missing")
    manual = checks.get("checks") if isinstance(checks.get("checks"), Mapping) else {}
    for name in REQUIRED_MANUAL_CHECKS:
        if manual.get(name) is not True:
            blockers.append(f"observer_manual_check_not_true:{name}")
    if not native_same_run_pass:
        blockers.append("native_contact_ft_same_run_not_passed")
    if not tf_lineage_pass:
        blockers.append("tf_lineage_not_passed")
    blockers = _dedupe(blockers)
    return {
        "schema": OBSERVER_SCHEMA,
        "run_id": run_id,
        "viewer_level_pass": not blockers,
        "claim_tier": "offline_native_gazebo_observer_evidence" if not blockers else "visual_only",
        "views": view_rows,
        "sim_time_window": list(sim_time_window) if sim_time_window is not None else None,
        "reviewer": checks.get("reviewer"),
        "reviewed_at": checks.get("reviewed_at"),
        "manual_checks": {name: manual.get(name) is True for name in REQUIRED_MANUAL_CHECKS},
        "native_contact_ft_same_run_pass": native_same_run_pass,
        "tf_lineage_pass": tf_lineage_pass,
        "blockers": blockers,
        "forbidden_claims": ["real bench/live contact", "live acceptance", "reproduction completion"],
    }


def build_evidence(run_dir: Path) -> tuple[Path, Path]:
    manifest_path = run_dir / "capture_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing capture manifest: {manifest_path}")
    manifest = load_json(manifest_path)
    run_id = str(manifest.get("run_id") or "")
    backend = str(manifest.get("backend") or "")
    backend_row = backend_spec(backend)
    contacts, contact_parse_issues = load_jsonl(run_dir / "native_contact.jsonl")
    ft_rows, ft_parse_issues = load_jsonl(run_dir / "native_ft.jsonl")
    ticks, tick_parse_issues = load_jsonl(run_dir / "tick_trace.jsonl")
    tf_path = run_dir / "tf_lineage.json"
    tf_payload = load_json(tf_path) if tf_path.is_file() else {}

    blockers: list[str] = []
    if not run_id:
        blockers.append("capture_manifest_run_id_missing")
    if manifest.get("engine_family") != "Gazebo Fortress":
        blockers.append("capture_manifest_engine_not_fortress")
    if manifest.get("engine_major") != 6:
        blockers.append("capture_manifest_engine_major_not_6")
    if manifest.get("ros2_control_plugin") != "libign_ros2_control-system.so":
        blockers.append("capture_manifest_ros2_control_plugin_mismatch")
    if manifest.get("simultaneous_backend_active") is not False:
        blockers.append("backend_exclusivity_not_proven")
    blockers.extend(contact_parse_issues + ft_parse_issues + tick_parse_issues)
    blockers.extend(validate_contact_rows(contacts, run_id=run_id))
    blockers.extend(validate_ft_rows(ft_rows, run_id=run_id))
    blockers.extend(validate_tick_rows(ticks, run_id=run_id, backend=backend))
    tf_issues = validate_tf_lineage(tf_payload, run_id=run_id)
    blockers.extend(tf_issues)
    correlation = contact_ft_correlation(contacts, ft_rows)
    blockers.extend(correlation["blockers"])
    blockers = _dedupe(blockers)
    native_same_run_pass = not blockers

    finite_tick_times = [float(row["sim_time_s"]) for row in ticks if _finite(row.get("sim_time_s"))]
    sim_time_window = (min(finite_tick_times), max(finite_tick_times)) if finite_tick_times else None

    observer = build_observer_review(
        run_dir,
        run_id=run_id,
        native_same_run_pass=native_same_run_pass,
        tf_lineage_pass=not tf_issues,
        sim_time_window=sim_time_window,
    )
    observer_path = write_json(run_dir / "observer_review.json", observer)
    artifacts = _artifact_rows(
        run_dir,
        (
            "capture_manifest.json",
            "native_contact.jsonl",
            "native_ft.jsonl",
            "tick_trace.jsonl",
            "tf_lineage.json",
            "camera_manifest.json",
            "observer_manual_checks.json",
            "observer_review.json",
        ),
    )
    payload = {
        "schema": SCHEMA,
        "mode": "offline_gazebo_fortress_same_run_evidence",
        "run_id": run_id,
        "backend": backend,
        "backend_fidelity": backend_row.fidelity,
        "engine": {
            "family": manifest.get("engine_family"),
            "major": manifest.get("engine_major"),
            "ros2_control_plugin": manifest.get("ros2_control_plugin"),
        },
        "rates_hz": {"physics": 2000, "controller": 500},
        "geometry": {
            "fidelity": EOAT_GEOMETRY_FIDELITY,
            "current_bench_cad_hash_bound": False,
            "mass_cog_inertia_calibrated": False,
            "active_tcp_offset_z_m": 0.12209917288991741,
        },
        "counts": {"ticks": len(ticks), "native_contact": len(contacts), "native_ft": len(ft_rows)},
        "native_contact_ft_same_run_pass": native_same_run_pass,
        "correlation": correlation,
        "tf_lineage_pass": not tf_issues,
        "tick_schema_pass": not validate_tick_rows(ticks, run_id=run_id, backend=backend),
        "observer_review_path": str(observer_path),
        "observer_review_pass": observer["viewer_level_pass"],
        "artifacts": artifacts,
        "blockers": blockers,
        "claim_boundary": {
            "allowed": "offline native Gazebo contact/FT evidence only" if native_same_run_pass else "tooling_only",
            "real_robot_motion": False,
            "live_acceptance": False,
            "package_acceptance": False,
            "reproduction_complete": False,
            "true_ur_torque_control": False,
            "virtual_surface_force_used": False,
            "current_bench_geometry_equivalence": False,
            "p0_simulator_physics_acceptance": False,
        },
        "authorization": {
            "live_motion_authorized": False,
            "bridge_start_authorized": False,
            "controller_upload_authorized": False,
            "tp_play_authorized": False,
            "zero_ftsensor_authorized": False,
        },
    }
    evidence_path = write_json(run_dir / "gazebo_v2_evidence.json", payload)
    return evidence_path, observer_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build fail-closed UR10e Gazebo v2 same-run evidence.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--strict", action="store_true", help="Return nonzero unless native and observer gates pass.")
    args = parser.parse_args()
    evidence_path, observer_path = build_evidence(args.run_dir.resolve())
    evidence = load_json(evidence_path)
    observer = load_json(observer_path)
    print(json.dumps({"evidence": str(evidence_path), "observer_review": str(observer_path)}, sort_keys=True))
    if args.strict and not (evidence["native_contact_ft_same_run_pass"] and observer["viewer_level_pass"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
