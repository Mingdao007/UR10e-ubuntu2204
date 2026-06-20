#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERIFICATION_SCHEMA = "ur10e_p2_gz_contact_wrench_evidence_verification_v1"
VERIFIED_CONTACT_LOG_SCHEMA = "ur10e_gazebo_contact_pair_log_v1"
PHYSICAL_GAZEBO_CLAIM_TIER = "physical Gazebo collision/contact physics"
BLOCKED_CLAIM_TIER = "visual_only"
DEFAULT_MIN_NORMAL_LOAD_N = 1.0
DEFAULT_STAMP_TOLERANCE_S = 0.002
DEFAULT_FORCE_TOLERANCE_N = 1e-6

VERIFICATION_FILENAME = "p2_gz_contact_wrench_evidence_verification.json"
VERIFIED_CONTACT_FILENAME = "p2_gazebo_contact_pair_log_verified.json"
TRANSFORM_FILENAME = "p2_gazebo_contact_wrench_transform_evidence.json"
BASELINE_FILENAME = "p2_gazebo_contact_wrench_baseline_evidence.json"
SOURCE_FILENAME = "p2_gz_contact_wrench_source_evidence.json"
CROSS_CHECK_FILENAME = "p2_gz_contact_wrench_surface_eoat_cross_check.json"

SOURCE_EVIDENCE_URLS = {
    "gz_sim_physics_contact_wrench_mapping": (
        "https://github.com/gazebosim/gz-sim/blob/"
        "6011926799d6c87572085e919ea68e61a0573902/src/systems/physics/Physics.cc"
    ),
    "gz_physics_world_frame_contact_data": (
        "https://github.com/gazebosim/gz-physics/blob/"
        "8a48173082a38b591ce1d5ca2829227d87ae8505/include/gz/physics/GetContacts.hh"
    ),
    "gz_sim_extra_contact_data_test": (
        "https://github.com/gazebosim/gz-sim/blob/"
        "6011926799d6c87572085e919ea68e61a0573902/test/integration/contact_system.cc"
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _path_has_empty_text_file(path: Path) -> bool:
    return path.is_file() and not path.read_text(encoding="utf-8").strip()


def _vec3(value: Any, *, label: str) -> tuple[float, float, float]:
    if isinstance(value, dict):
        return (
            float(value.get("x") or 0.0),
            float(value.get("y") or 0.0),
            float(value.get("z") or 0.0),
        )
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three values")
    return (float(value[0]), float(value[1]), float(value[2]))


def _dot3(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _neg3(value: tuple[float, float, float]) -> tuple[float, float, float]:
    return (-value[0], -value[1], -value[2])


def _close3(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
    *,
    tolerance: float,
) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(left, right))


def _norm3(value: tuple[float, float, float]) -> float:
    return math.sqrt(sum(component * component for component in value))


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _is_eoat_collision(name: str) -> bool:
    return "eoat" in name and "collision" in name


def _is_surface_collision(name: str) -> bool:
    return "contact_surface" in name or "surface::collision" in name


def _parse_sdf(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def _model(root: ET.Element, name: str) -> ET.Element | None:
    return root.find(f".//model[@name='{name}']")


def _link(model: ET.Element | None, name: str) -> ET.Element | None:
    return model.find(f"./link[@name='{name}']") if model is not None else None


def _pose_values(element: ET.Element | None) -> tuple[float, float, float, float, float, float]:
    if element is None:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    pose = element.find("./pose")
    if pose is None or not pose.text:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    parts = [float(part) for part in pose.text.split()]
    if len(parts) != 6:
        raise ValueError("SDF pose must have six values")
    return tuple(parts)  # type: ignore[return-value]


def _pose_is_identity(pose: tuple[float, float, float, float, float, float], *, tolerance: float = 1e-9) -> bool:
    return all(abs(value) <= tolerance for value in pose)


def _model_is_static(model: ET.Element | None) -> bool:
    if model is None:
        return False
    static = model.find("./static")
    return static is not None and (static.text or "").strip().lower() == "true"


def _box_size_z(model: ET.Element | None, *, link_name: str, collision_name: str) -> float | None:
    link = _link(model, link_name)
    collision = link.find(f"./collision[@name='{collision_name}']") if link is not None else None
    size = collision.find("./geometry/box/size") if collision is not None else None
    if size is None or not size.text:
        return None
    parts = [float(part) for part in size.text.split()]
    if len(parts) != 3:
        return None
    return parts[2]


def _write_source_evidence(output_dir: Path, *, generated_at: str) -> Path:
    payload = {
        "schema": "ur10e_p2_gz_contact_wrench_source_evidence_v1",
        "generated_at": generated_at,
        "claim_tier": BLOCKED_CLAIM_TIER,
        "target_claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER,
        "source_class": "primary_gazebosim_source",
        "evidence_urls": SOURCE_EVIDENCE_URLS,
        "supports": {
            "gz_physics_contact_force_world_frame": True,
            "gz_physics_contact_normal_world_frame": True,
            "gz_sim_body2_is_equal_and_opposite_to_body1": True,
            "gz_sim_integration_test_positive_body1_z_normal": True,
        },
        "does_not_support": [
            "real bench/live contact",
            "full UR10e reproduction acceptance",
            "automatic mapping of a contact sensor message collision order to a live F/T sensor frame",
            "same-run concurrent dual-sensor observation",
            "total contact wrench across all Gazebo contact points",
        ],
    }
    return _write_json(output_dir / SOURCE_FILENAME, payload)


def _build_transform_evidence(output_dir: Path, *, world_path: Path, generated_at: str) -> tuple[Path, dict[str, Any]]:
    blockers: list[str] = []
    try:
        root = _parse_sdf(world_path)
        base_model = _model(root, "ur10e_base_frame")
        base_link = _link(base_model, "base_link")
        model_pose = _pose_values(base_model)
        link_pose = _pose_values(base_link)
    except Exception as exc:  # noqa: BLE001 - artifact should record parser failure.
        blockers.append(f"sdf_parse_error:{type(exc).__name__}")
        model_pose = None
        link_pose = None
        base_model = None
        base_link = None

    if base_model is None:
        blockers.append("missing_ur10e_base_frame_model")
    if base_link is None:
        blockers.append("missing_base_link")
    if model_pose is not None and not _pose_is_identity(model_pose):
        blockers.append("base_model_pose_not_identity")
    if link_pose is not None and not _pose_is_identity(link_pose):
        blockers.append("base_link_pose_not_identity")

    payload = {
        "schema": "ur10e_p2_gz_contact_wrench_transform_evidence_v1",
        "generated_at": generated_at,
        "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if not blockers else BLOCKED_CLAIM_TIER,
        "world_path": str(world_path),
        "from_frame": "gazebo_contact_message_native_frame",
        "native_frame_interpreted_as": "world",
        "to_frame": "base",
        "transform": {
            "translation_xyz_m": [0.0, 0.0, 0.0],
            "rpy_rad": [0.0, 0.0, 0.0],
        },
        "geometric_provenance": {
            "source": "p2_witness_sdf_ur10e_base_frame",
            "model_name": "ur10e_base_frame",
            "link_name": "base_link",
            "model_pose_xyz_rpy": list(model_pose) if model_pose is not None else None,
            "link_pose_xyz_rpy": list(link_pose) if link_pose is not None else None,
        },
        "source_evidence": {
            "world_frame_contact_data": SOURCE_EVIDENCE_URLS["gz_physics_world_frame_contact_data"],
        },
        "valid": not blockers,
        "blockers": _dedupe(blockers),
    }
    path = output_dir / TRANSFORM_FILENAME
    _write_json(path, payload)
    return path, payload


def _build_baseline_evidence(
    output_dir: Path,
    *,
    baseline_payload: dict[str, Any],
    generated_at: str,
) -> tuple[Path, dict[str, Any]]:
    blockers: list[str] = []
    world_path = Path(str(baseline_payload.get("world_path") or ""))
    raw_jsonl_path = Path(str(baseline_payload.get("raw_jsonl_path") or ""))

    if baseline_payload.get("schema") != VERIFIED_CONTACT_LOG_SCHEMA:
        blockers.append("baseline_schema_not_contact_pair_log_v1")
    if baseline_payload.get("sim_transport") != "gz":
        blockers.append("baseline_not_gz_transport")
    if baseline_payload.get("sensor_collision_role") != "eoat":
        blockers.append("baseline_sensor_not_eoat")
    if baseline_payload.get("baseline_mode") != "no_contact_static_elevated_eoat":
        blockers.append("baseline_mode_not_no_contact_static_elevated_eoat")
    if int(baseline_payload.get("row_count") or 0) != 0 or baseline_payload.get("rows"):
        blockers.append("baseline_contact_rows_present")
    if baseline_payload.get("parse_issues"):
        blockers.append("baseline_parse_issues_present")

    capture = baseline_payload.get("capture") if isinstance(baseline_payload.get("capture"), dict) else {}
    if capture.get("allow_no_messages") is not True:
        blockers.append("baseline_capture_did_not_allow_no_messages")
    if capture.get("topic_timeout_expired") is not True:
        blockers.append("baseline_topic_did_not_timeout_empty")
    if capture.get("eoat_static") is not True:
        blockers.append("baseline_eoat_not_static")

    raw_topic_log_empty = _path_has_empty_text_file(raw_jsonl_path)
    if not raw_topic_log_empty:
        blockers.append("baseline_raw_topic_log_not_empty")

    separation_m = None
    try:
        root = _parse_sdf(world_path)
        surface = _model(root, "step5_contact_surface")
        eoat = _model(root, "real_aligned_eoat_visual_stack")
        surface_pose = _pose_values(surface)
        eoat_pose = _pose_values(eoat)
        surface_size_z = _box_size_z(surface, link_name="surface", collision_name="collision")
        eoat_size_z = _box_size_z(eoat, link_name="eoat_contact_pad_link", collision_name="eoat_contact_pad_collision")
        if not _model_is_static(eoat):
            blockers.append("baseline_eoat_model_not_static_in_sdf")
        if surface_size_z is None or eoat_size_z is None:
            blockers.append("baseline_missing_collision_box_geometry")
        else:
            surface_top_z = surface_pose[2] + surface_size_z / 2.0
            eoat_bottom_z = eoat_pose[2] - eoat_size_z / 2.0
            separation_m = eoat_bottom_z - surface_top_z
            if separation_m <= 0.02:
                blockers.append("baseline_eoat_surface_separation_too_small")
    except Exception as exc:  # noqa: BLE001 - artifact should record parser failure.
        blockers.append(f"baseline_sdf_parse_error:{type(exc).__name__}")

    payload = {
        "schema": "ur10e_p2_gz_contact_wrench_baseline_evidence_v1",
        "generated_at": generated_at,
        "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if not blockers else BLOCKED_CLAIM_TIER,
        "baseline_policy": "gazebo_contact_zero_no_contact_baseline",
        "baseline_contact_pair_path": baseline_payload.get("artifact_path"),
        "world_path": str(world_path),
        "raw_jsonl_path": str(raw_jsonl_path),
        "row_count": baseline_payload.get("row_count"),
        "raw_topic_log_empty": raw_topic_log_empty,
        "independent_no_contact_reference": True,
        "geometry": {
            "eoat_static": capture.get("eoat_static"),
            "eoat_pose_z": capture.get("eoat_pose_z"),
            "surface_to_eoat_gap_m": separation_m,
        },
        "valid": not blockers,
        "blockers": _dedupe(blockers),
    }
    path = output_dir / BASELINE_FILENAME
    _write_json(path, payload)
    return path, payload


def _native(row: dict[str, Any]) -> dict[str, Any] | None:
    value = row.get("native_gazebo_contact_wrench")
    return value if isinstance(value, dict) else None


def _row_stamp(row: dict[str, Any]) -> float | None:
    try:
        return float(row["stamp_s"])
    except (KeyError, TypeError, ValueError):
        return None


def _find_surface_match(
    eoat_row: dict[str, Any],
    surface_rows: list[dict[str, Any]],
    *,
    stamp_tolerance_s: float,
) -> dict[str, Any] | None:
    stamp = _row_stamp(eoat_row)
    if stamp is None:
        return None
    candidates = [row for row in surface_rows if _row_stamp(row) is not None]
    if not candidates:
        return None
    best = min(candidates, key=lambda row: abs(float(row["stamp_s"]) - stamp))
    return best if abs(float(best["stamp_s"]) - stamp) <= stamp_tolerance_s else None


def _row_rejection_reasons(
    row: dict[str, Any],
    *,
    min_normal_load_n: float,
) -> list[str]:
    blockers: list[str] = []
    native = _native(row)
    if not isinstance(row, dict):
        return ["malformed_contact_row"]
    if not _is_eoat_collision(str(row.get("collision1") or "")):
        blockers.append("collision1_not_eoat")
    if not _is_surface_collision(str(row.get("collision2") or "")):
        blockers.append("collision2_not_surface")
    if row.get("stamp_evidence") is not True or _row_stamp(row) is None:
        blockers.append("missing_stamp_evidence")
    try:
        normal = _vec3(row.get("normal"), label="normal")
    except (TypeError, ValueError):
        blockers.append("invalid_contact_normal")
        normal = (0.0, 0.0, 1.0)
    if row.get("normal_source") != "gazebo_contact_message_normal":
        blockers.append("normal_not_from_gazebo_message")
    try:
        if int(row.get("contact_count") or 0) <= 0:
            blockers.append("missing_contact_count")
    except (TypeError, ValueError):
        blockers.append("invalid_contact_count")
    if native is None:
        blockers.append("missing_native_gazebo_contact_wrench")
        return blockers
    if native.get("source") != "gazebo_contact_message_wrench":
        blockers.append("native_wrench_source_not_gazebo_contact_message")
    if native.get("source_schema") != "gz.msgs.Contact.contact.wrench":
        blockers.append("native_wrench_schema_not_gz")
    if native.get("force_source_class") != "gazebo_contact":
        blockers.append("native_force_source_class_not_gazebo_contact")
    if native.get("selected_body") != "body_1_wrench" or native.get("selected_body_collision") != "collision1":
        blockers.append("native_body1_not_selected_for_eoat_collision1")
    if native.get("selected_body_role") != "eoat":
        blockers.append("native_selected_body_role_not_eoat")
    if native.get("measured_contact_wrench") is not True or native.get("commanded_force") is not False:
        blockers.append("native_wrench_not_measured_contact")
    if native.get("wrench_stamp_evidence") is not True:
        blockers.append("missing_wrench_stamp_evidence")
    try:
        force = _vec3(native.get("force_n"), label="force_n")
        other = _vec3(native.get("other_force_n"), label="other_force_n")
        normal_load_n = _dot3(force, normal)
    except (TypeError, ValueError):
        blockers.append("invalid_native_force_vector")
    else:
        if normal_load_n < min_normal_load_n:
            blockers.append("normal_load_below_floor")
        if not _close3(force, _neg3(other), tolerance=DEFAULT_FORCE_TOLERANCE_N):
            blockers.append("native_body2_not_equal_and_opposite")
    try:
        if abs(float(native.get("wrench_stamp_s")) - float(row.get("stamp_s"))) > 0.02:
            blockers.append("wrench_stamp_not_contact_aligned")
    except (TypeError, ValueError):
        blockers.append("missing_wrench_stamp")
    return _dedupe(blockers)


def _cross_check_row(
    eoat_row: dict[str, Any],
    surface_row: dict[str, Any] | None,
    *,
    stamp_tolerance_s: float,
    force_tolerance_n: float,
) -> dict[str, Any]:
    if surface_row is None:
        return {
            "valid": False,
            "blockers": ["missing_surface_sensor_match"],
        }
    blockers: list[str] = []
    surface_native = _native(surface_row)
    eoat_native = _native(eoat_row)
    if surface_native is None or eoat_native is None:
        blockers.append("missing_native_wrench_for_cross_check")
    if not _is_surface_collision(str(surface_row.get("collision1") or "")):
        blockers.append("surface_log_collision1_not_surface")
    if not _is_eoat_collision(str(surface_row.get("collision2") or "")):
        blockers.append("surface_log_collision2_not_eoat")
    try:
        stamp_delta_s = abs(float(surface_row["stamp_s"]) - float(eoat_row["stamp_s"]))
    except (KeyError, TypeError, ValueError):
        stamp_delta_s = None
        blockers.append("missing_cross_check_stamp")
    else:
        if stamp_delta_s > stamp_tolerance_s:
            blockers.append("cross_check_stamp_delta_too_large")
    if surface_native is not None and eoat_native is not None:
        try:
            eoat_force = _vec3(eoat_native.get("force_n"), label="eoat_force")
            eoat_other = _vec3(eoat_native.get("other_force_n"), label="eoat_other_force")
            surface_selected = _vec3(surface_native.get("force_n"), label="surface_selected_force")
            surface_other = _vec3(surface_native.get("other_force_n"), label="surface_other_force")
        except (TypeError, ValueError):
            blockers.append("invalid_cross_check_force_vector")
            eoat_force = eoat_other = surface_selected = surface_other = (0.0, 0.0, 0.0)
        else:
            if not _close3(eoat_force, surface_other, tolerance=force_tolerance_n):
                blockers.append("eoat_body1_not_matching_surface_body1")
            if not _close3(eoat_force, _neg3(surface_selected), tolerance=force_tolerance_n):
                blockers.append("eoat_body1_not_opposite_surface_body2")
            if not _close3(eoat_force, _neg3(eoat_other), tolerance=force_tolerance_n):
                blockers.append("eoat_body2_not_equal_and_opposite")
    return {
        "valid": not blockers,
        "blockers": _dedupe(blockers),
        "stamp_delta_s": stamp_delta_s,
        "eoat_stamp_s": eoat_row.get("stamp_s"),
        "surface_stamp_s": surface_row.get("stamp_s"),
        "eoat_force_n": eoat_native.get("force_n") if eoat_native else None,
        "surface_selected_force_n": surface_native.get("force_n") if surface_native else None,
        "surface_other_force_n": surface_native.get("other_force_n") if surface_native else None,
        "eoat_force_magnitude_n": _norm3(_vec3(eoat_native.get("force_n"), label="eoat_force")) if eoat_native else None,
    }


def build_verified_contact_pair_or_report(
    *,
    eoat_payload: dict[str, Any],
    surface_payload: dict[str, Any],
    baseline_payload: dict[str, Any],
    output_dir: Path,
    generated_at: str | None = None,
    min_normal_load_n: float = DEFAULT_MIN_NORMAL_LOAD_N,
    stamp_tolerance_s: float = DEFAULT_STAMP_TOLERANCE_S,
    force_tolerance_n: float = DEFAULT_FORCE_TOLERANCE_N,
) -> dict[str, Any]:
    generated_at = generated_at or _now_iso()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = _write_source_evidence(output_dir, generated_at=generated_at)
    world_path = Path(str(eoat_payload.get("world_path") or ""))
    transform_path, transform = _build_transform_evidence(output_dir, world_path=world_path, generated_at=generated_at)
    baseline_path, baseline = _build_baseline_evidence(output_dir, baseline_payload=baseline_payload, generated_at=generated_at)

    blockers: list[str] = []
    if eoat_payload.get("schema") != VERIFIED_CONTACT_LOG_SCHEMA:
        blockers.append("eoat_log_schema_not_contact_pair_log_v1")
    if eoat_payload.get("sim_transport") != "gz":
        blockers.append("eoat_log_not_gz_transport")
    if eoat_payload.get("sensor_collision_role") != "eoat":
        blockers.append("eoat_log_sensor_collision_role_not_eoat")
    if eoat_payload.get("parse_issues"):
        blockers.append("eoat_log_parse_issues_present")
    if surface_payload.get("schema") != VERIFIED_CONTACT_LOG_SCHEMA:
        blockers.append("surface_log_schema_not_contact_pair_log_v1")
    if surface_payload.get("sim_transport") != "gz":
        blockers.append("surface_log_not_gz_transport")
    if surface_payload.get("sensor_collision_role") != "surface":
        blockers.append("surface_log_sensor_collision_role_not_surface")
    if surface_payload.get("parse_issues"):
        blockers.append("surface_log_parse_issues_present")
    if not transform.get("valid"):
        blockers.extend(transform.get("blockers") or [])
    if not baseline.get("valid"):
        blockers.extend(baseline.get("blockers") or [])

    surface_rows = [row for row in surface_payload.get("rows") or [] if isinstance(row, dict)]
    verified_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    cross_checks: list[dict[str, Any]] = []
    for index, row in enumerate(eoat_payload.get("rows") or []):
        if not isinstance(row, dict):
            rejected_rows.append({"row_index": index, "blockers": ["malformed_contact_row"]})
            continue
        row_blockers = _row_rejection_reasons(row, min_normal_load_n=min_normal_load_n)
        surface_match = _find_surface_match(row, surface_rows, stamp_tolerance_s=stamp_tolerance_s)
        cross_check = _cross_check_row(
            row,
            surface_match,
            stamp_tolerance_s=stamp_tolerance_s,
            force_tolerance_n=force_tolerance_n,
        )
        cross_check["row_index"] = index
        cross_checks.append(cross_check)
        if not cross_check["valid"]:
            row_blockers.extend(cross_check["blockers"])
        row_blockers = _dedupe(row_blockers)
        if row_blockers:
            rejected_rows.append(
                {
                    "row_index": index,
                    "stamp_s": row.get("stamp_s"),
                    "normal_load_n": (_native(row) or {}).get("selected_force_dot_contact_normal_n"),
                    "blockers": row_blockers,
                }
            )
            continue
        verified_row = copy.deepcopy(row)
        native = verified_row["native_gazebo_contact_wrench"]
        native["frame_id"] = "base"
        native["frame_policy"] = "verified_world_to_base_identity_from_p2_witness_sdf"
        native["frame_transform_evidence"] = {
            "source": "p2_witness_sdf_ur10e_base_frame_identity",
            "from_frame": "gazebo_contact_message_native_frame",
            "native_frame_interpreted_as": "world",
            "to_frame": "base",
            "stamp_s": native["wrench_stamp_s"],
            "artifact_path": str(transform_path),
        }
        native["status"] = "valid"
        native["baseline_policy"] = "gazebo_contact_zero_no_contact_baseline"
        native["baseline_evidence"] = {
            "source": "no_contact_static_elevated_eoat_gz_topic_empty_log",
            "artifact_path": str(baseline_path),
        }
        native["source_evidence"] = {
            "source": "primary_gazebosim_source_urls",
            "artifact_path": str(source_path),
        }
        native["wrench_aggregation_policy"] = "single_contact_point_wrench_no_total_contact_wrench_claim"
        verified_rows.append(verified_row)

    if not verified_rows:
        blockers.append("no_verified_eoat_contact_wrench_rows")

    cross_check_payload = {
        "schema": "ur10e_p2_gz_contact_wrench_surface_eoat_cross_check_v1",
        "generated_at": generated_at,
        "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if verified_rows and not blockers else BLOCKED_CLAIM_TIER,
        "comparison_scope": "cross_run_deterministic_repeatability",
        "same_run_concurrent_observation": False,
        "allowed_claim": "cross-run repeatable force sign/body-side comparison only",
        "forbidden_claim": "same-run concurrent dual-sensor observation; total contact wrench",
        "eoat_contact_pair_path": eoat_payload.get("artifact_path"),
        "surface_contact_pair_path": surface_payload.get("artifact_path"),
        "eoat_generated_at": eoat_payload.get("generated_at"),
        "surface_generated_at": surface_payload.get("generated_at"),
        "stamp_tolerance_s": stamp_tolerance_s,
        "force_tolerance_n": force_tolerance_n,
        "checks": cross_checks,
        "valid": bool(verified_rows) and not any(not check["valid"] for check in cross_checks if check.get("row_index") in {r.get("row_index") for r in rejected_rows}),
    }
    cross_check_path = _write_json(output_dir / CROSS_CHECK_FILENAME, cross_check_payload)

    verified_path: str | None = None
    if verified_rows and not blockers:
        verified_payload = {
            **{key: value for key, value in eoat_payload.items() if key != "rows"},
            "schema": VERIFIED_CONTACT_LOG_SCHEMA,
            "generated_at": generated_at,
            "mode": "offline_verified_gz_contact_wrench_evidence",
            "source": "gazebo_contact_sensor_topic_verified",
            "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER,
            "target_claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER,
            "allowed_claim": (
                "standalone P2 witness physical Gazebo collision/contact physics with one native "
                "Gazebo contact-point wrench correlation only"
            ),
            "forbidden_claim": (
                "real bench/live contact; full UR10e reproduction acceptance; simulated_ft upgrade; "
                "same-run concurrent dual-sensor observation; total contact wrench"
            ),
            "row_count": len(verified_rows),
            "rows": verified_rows,
            "verification": {
                "schema": VERIFICATION_SCHEMA,
                "min_normal_load_n": min_normal_load_n,
                "transform_evidence_path": str(transform_path),
                "baseline_evidence_path": str(baseline_path),
                "source_evidence_path": str(source_path),
                "cross_check_path": str(cross_check_path),
                "rejected_rows": rejected_rows,
            },
        }
        verified_contact_path = output_dir / VERIFIED_CONTACT_FILENAME
        verified_payload["artifact_path"] = str(verified_contact_path)
        _write_json(verified_contact_path, verified_payload)
        verified_path = str(verified_contact_path)

    report = {
        "schema": VERIFICATION_SCHEMA,
        "generated_at": generated_at,
        "mode": "offline_no_live_p2_gz_contact_wrench_verification",
        "goal_lineage": "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md",
        "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if verified_path else BLOCKED_CLAIM_TIER,
        "target_claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER,
        "allowed_claim": (
            "standalone P2 witness physical Gazebo collision/contact physics with one native contact-point wrench"
            if verified_path
            else "visual_only blocked/not_proven"
        ),
        "forbidden_claim": (
            "real bench/live contact; full UR10e reproduction acceptance; simulated_ft upgrade; "
            "same-run concurrent dual-sensor observation; total contact wrench"
        ),
        "inputs": {
            "eoat_contact_pair_path": eoat_payload.get("artifact_path"),
            "surface_contact_pair_path": surface_payload.get("artifact_path"),
            "baseline_contact_pair_path": baseline_payload.get("artifact_path"),
        },
        "outputs": {
            "verified_contact_pair_path": verified_path,
            "transform_evidence_path": str(transform_path),
            "baseline_evidence_path": str(baseline_path),
            "source_evidence_path": str(source_path),
            "cross_check_path": str(cross_check_path),
        },
        "normal_load_floor_n": min_normal_load_n,
        "verified_row_count": len(verified_rows) if verified_path else 0,
        "candidate_row_count": len(eoat_payload.get("rows") or []),
        "rejected_rows": rejected_rows,
        "blockers": _dedupe(blockers),
        "claim_boundary_gate": {
            "visual_only_inputs_do_not_prove_force": True,
            "virtual_software_force_loop_not_physical_gazebo_contact": True,
            "simulated_ft_not_physical_gazebo_contact": True,
            "real_bench_live_contact_authorized": False,
            "physical_gazebo_requires_eoat_collision_contact_pair_wrench_correlation": True,
            "surface_eoat_cross_check_is_cross_run_repeatability_not_concurrent_observation": True,
            "total_contact_wrench_proven": False,
        },
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
    }
    report_path = output_dir / VERIFICATION_FILENAME
    report["artifact_path"] = str(report_path)
    _write_json(report_path, report)
    return report


def write_verified_contact_pair_or_report(
    output_dir: Path,
    *,
    eoat_contact_pair_path: Path,
    surface_contact_pair_path: Path,
    baseline_contact_pair_path: Path,
    generated_at: str | None = None,
    min_normal_load_n: float = DEFAULT_MIN_NORMAL_LOAD_N,
) -> Path:
    report = build_verified_contact_pair_or_report(
        eoat_payload={**_load_json(eoat_contact_pair_path), "artifact_path": str(eoat_contact_pair_path)},
        surface_payload={**_load_json(surface_contact_pair_path), "artifact_path": str(surface_contact_pair_path)},
        baseline_payload={**_load_json(baseline_contact_pair_path), "artifact_path": str(baseline_contact_pair_path)},
        output_dir=output_dir,
        generated_at=generated_at,
        min_normal_load_n=min_normal_load_n,
    )
    return Path(str(report["artifact_path"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify P2 GZ contact wrench evidence fail-closed.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--eoat-contact-pair", required=True, type=Path)
    parser.add_argument("--surface-contact-pair", required=True, type=Path)
    parser.add_argument("--baseline-contact-pair", required=True, type=Path)
    parser.add_argument("--min-normal-load-n", type=float, default=DEFAULT_MIN_NORMAL_LOAD_N)
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args(argv)

    path = write_verified_contact_pair_or_report(
        args.output_dir,
        eoat_contact_pair_path=args.eoat_contact_pair,
        surface_contact_pair_path=args.surface_contact_pair,
        baseline_contact_pair_path=args.baseline_contact_pair,
        generated_at=args.generated_at,
        min_normal_load_n=args.min_normal_load_n,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
