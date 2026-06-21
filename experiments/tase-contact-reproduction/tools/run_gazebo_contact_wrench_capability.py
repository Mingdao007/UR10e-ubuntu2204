#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import build_gazebo_contact_wrench_trace as wrench_adapter
import capture_p2_gazebo_contact_pair_log as capture


CAPABILITY_SCHEMA = "ur10e_gazebo_contact_wrench_capability_manifest_v1"
VERIFIED_CONTACT_FILENAME = "capability_surface_gz_contact_pair_log_verified.json"
ADAPTER_REPORT_FILENAME = "capability_surface_gz_contact_wrench_adapter.json"
ADAPTER_TRACE_FILENAME = "capability_surface_gz_contact_wrench_trace.json"
MANIFEST_FILENAME = "capability_gate_manifest.json"
OBSERVATION_SCOPE = "non_step5b_forced_contact_capability"
DEFAULT_TOPIC = "/ur10e/contact/gazebo/capability_forced_contact/contacts"
PHYSICAL_GAZEBO_CLAIM_TIER = "physical Gazebo collision/contact physics"
BLOCKED_CLAIM_TIER = "visual_only"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _add3(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2])


def _close3(left: tuple[float, float, float], right: tuple[float, float, float], *, tolerance: float = 1e-6) -> bool:
    return math.sqrt(sum((a - b) * (a - b) for a, b in zip(left, right))) <= tolerance


def _neg3(value: tuple[float, float, float]) -> tuple[float, float, float]:
    return (-value[0], -value[1], -value[2])


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _native(row: dict[str, Any]) -> dict[str, Any] | None:
    native = row.get("native_gazebo_contact_wrench")
    return native if isinstance(native, dict) else None


def _is_surface_collision(name: str) -> bool:
    return "contact_surface" in name or "surface::collision" in name


def _is_eoat_collision(name: str) -> bool:
    return "eoat" in name and "collision" in name


def _raw_log_empty(path: Path) -> bool:
    return path.is_file() and not path.read_text(encoding="utf-8").strip()


def _pose_is_identity(element: ET.Element | None, *, tolerance: float = 1e-9) -> bool:
    if element is None:
        return False
    pose = element.find("./pose")
    if pose is None or not pose.text:
        return True
    try:
        values = [float(part) for part in pose.text.split()]
    except ValueError:
        return False
    return len(values) == 6 and all(abs(value) <= tolerance for value in values)


def _base_frame_identity_blockers(world_path: Path) -> list[str]:
    try:
        root = ET.parse(world_path).getroot()
    except Exception as exc:  # noqa: BLE001 - verifier must report, not crash.
        return [f"world_sdf_parse_error:{type(exc).__name__}"]
    base_model = root.find(".//model[@name='ur10e_base_frame']")
    if base_model is None:
        return ["missing_ur10e_base_frame_model"]
    base_link = base_model.find("./link[@name='base_link']")
    blockers: list[str] = []
    if base_link is None:
        blockers.append("missing_base_link")
    if not _pose_is_identity(base_model):
        blockers.append("base_model_pose_not_identity")
    if base_link is not None and not _pose_is_identity(base_link):
        blockers.append("base_link_pose_not_identity")
    return blockers


def _capture_summary(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"present": False, "path": str(path) if path else None}
    payload = _load_json(path)
    rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
    native_rows = [row for row in rows if isinstance(row.get("native_gazebo_contact_wrench"), dict)]
    return {
        "present": True,
        "path": str(path),
        "sha256": _sha256(path),
        "sim_transport": payload.get("sim_transport"),
        "sensor_collision_role": payload.get("sensor_collision_role"),
        "selected_contact_body_role": payload.get("selected_contact_body_role"),
        "topic": payload.get("topic"),
        "row_count": len(rows),
        "native_wrench_row_count": len(native_rows),
        "parse_issues": payload.get("parse_issues") or [],
    }


def binary_identity_precheck() -> dict[str, Any]:
    commands: dict[str, Any] = {}
    for tool in ("ign", "gz"):
        path = shutil.which(tool)
        commands[tool] = {
            "which": path,
            "realpath": os.path.realpath(path) if path else None,
        }
        if path:
            owner = subprocess.run(["dpkg", "-S", path], check=False, capture_output=True, text=True)
            commands[tool]["dpkg_owner_returncode"] = owner.returncode
            commands[tool]["dpkg_owner_stdout"] = owner.stdout.strip()
            commands[tool]["dpkg_owner_stderr"] = owner.stderr.strip()
    package_rows = subprocess.run(
        [
            "dpkg-query",
            "-W",
            "-f=${binary:Package} ${Version}\n",
            "ignition-tools",
            "gz-tools2",
            "ignition-transport11-cli",
            "libignition-msgs8",
            "libgz-msgs10",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "schema": "ur10e_gazebo_tool_binary_identity_precheck_v1",
        "generated_at": _now_iso(),
        "commands": commands,
        "package_rows_returncode": package_rows.returncode,
        "package_rows_stdout": package_rows.stdout.strip().splitlines(),
        "package_rows_stderr": package_rows.stderr.strip(),
        "distinct_ign_gz_wrappers": commands.get("ign", {}).get("realpath") != commands.get("gz", {}).get("realpath"),
    }


def _row_rejection_reasons(row: dict[str, Any], *, min_normal_load_n: float) -> list[str]:
    blockers: list[str] = []
    collision1 = str(row.get("collision1") or "")
    collision2 = str(row.get("collision2") or "")
    if not _is_surface_collision(collision1):
        blockers.append("surface_gate_collision1_not_surface")
    if not _is_eoat_collision(collision2):
        blockers.append("surface_gate_collision2_not_eoat")
    if row.get("stamp_evidence") is not True:
        blockers.append("missing_stamp_evidence")
    try:
        stamp_s = float(row.get("stamp_s"))
    except (TypeError, ValueError):
        blockers.append("missing_stamp_s")
        stamp_s = 0.0
    try:
        normal = _vec3(row.get("normal"), label="normal")
    except (TypeError, ValueError):
        blockers.append("invalid_contact_normal")
        normal = (0.0, 0.0, 1.0)
    if row.get("normal_source") != "gazebo_contact_message_normal":
        blockers.append("normal_not_from_gazebo_message")
    try:
        contact_count = int(row.get("contact_count") or 0)
    except (TypeError, ValueError):
        contact_count = 0
        blockers.append("invalid_contact_count")
    if contact_count <= 0:
        blockers.append("missing_contact_count")
    native = _native(row)
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
        blockers.append("native_body1_not_selected_for_surface_collision1")
    if native.get("selected_body_role") != "surface":
        blockers.append("native_selected_body_role_not_surface")
    if native.get("measured_contact_wrench") is not True or native.get("commanded_force") is not False:
        blockers.append("native_wrench_not_measured_contact")
    if native.get("wrench_stamp_evidence") is not True:
        blockers.append("missing_wrench_stamp_evidence")
    try:
        force = _vec3(native.get("force_n"), label="force_n")
        other_force = _vec3(native.get("other_force_n"), label="other_force_n")
        normal_load_n = _dot3(force, normal)
    except (TypeError, ValueError):
        blockers.append("invalid_native_force_vector")
    else:
        if normal_load_n < min_normal_load_n:
            blockers.append("normal_load_below_floor")
        if not _close3(force, _neg3(other_force)):
            blockers.append("native_body_pair_not_equal_and_opposite")
    try:
        if abs(float(native.get("wrench_stamp_s")) - stamp_s) > 0.02:
            blockers.append("wrench_stamp_not_contact_aligned")
    except (TypeError, ValueError):
        blockers.append("missing_wrench_stamp")
    return _dedupe(blockers)


def _transform_evidence_payload(world_path: str, *, generated_at: str) -> dict[str, Any]:
    blockers = _base_frame_identity_blockers(Path(world_path))
    return {
        "schema": "ur10e_contact_capability_transform_evidence_v1",
        "generated_at": generated_at,
        "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if not blockers else BLOCKED_CLAIM_TIER,
        "world_path": world_path,
        "source": "contact_capability_witness_sdf_ur10e_base_frame_identity",
        "from_frame": "gazebo_contact_message_native_frame",
        "native_frame_interpreted_as": "world",
        "to_frame": "base",
        "transform": {"translation_xyz_m": [0.0, 0.0, 0.0], "rpy_rad": [0.0, 0.0, 0.0]},
        "valid": not blockers,
        "blockers": blockers,
    }


def _baseline_rejection_reasons(
    gate_payload: dict[str, Any],
    baseline_payload: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if baseline_payload.get("schema") != capture.CONTACT_LOG_SCHEMA:
        blockers.append("baseline_schema_not_contact_pair_log_v1")
    if baseline_payload.get("sim_transport") != "gz":
        blockers.append("baseline_not_gz_transport")
    if baseline_payload.get("sensor_collision_role") != "surface":
        blockers.append("baseline_sensor_not_surface")
    if baseline_payload.get("selected_contact_body_role") != "surface":
        blockers.append("baseline_selected_body_role_not_surface")
    if baseline_payload.get("topic") != gate_payload.get("topic"):
        blockers.append("baseline_topic_not_same_as_positive")
    if baseline_payload.get("baseline_mode") != "no_contact_static_elevated_surface_sensor":
        blockers.append("baseline_mode_not_no_contact_static_elevated_surface_sensor")
    if int(baseline_payload.get("row_count") or 0) != 0 or baseline_payload.get("rows"):
        blockers.append("baseline_contact_rows_present")
    if baseline_payload.get("parse_issues"):
        blockers.append("baseline_parse_issues_present")
    capture_meta = baseline_payload.get("capture") if isinstance(baseline_payload.get("capture"), dict) else {}
    if capture_meta.get("allow_no_messages") is not True:
        blockers.append("baseline_capture_did_not_allow_no_messages")
    if capture_meta.get("topic_timeout_expired") is not True:
        blockers.append("baseline_topic_did_not_timeout_empty")
    if capture_meta.get("eoat_static") is not True:
        blockers.append("baseline_eoat_not_static")
    raw_path = Path(str(baseline_payload.get("raw_jsonl_path") or ""))
    if not _raw_log_empty(raw_path):
        blockers.append("baseline_raw_topic_log_not_empty")
    return _dedupe(blockers)


def build_verified_surface_contact_pair(
    *,
    surface_gz_payload: dict[str, Any],
    surface_gz_baseline_payload: dict[str, Any],
    output_dir: Path,
    generated_at: str | None = None,
    min_normal_load_n: float = 1.0,
) -> dict[str, Any]:
    generated_at = generated_at or _now_iso()
    output_dir.mkdir(parents=True, exist_ok=True)
    blockers: list[str] = []
    if surface_gz_payload.get("schema") != capture.CONTACT_LOG_SCHEMA:
        blockers.append("surface_gz_schema_not_contact_pair_log_v1")
    if surface_gz_payload.get("sim_transport") != "gz":
        blockers.append("surface_gz_not_gz_transport")
    if surface_gz_payload.get("sensor_collision_role") != "surface":
        blockers.append("surface_gz_sensor_not_surface")
    if surface_gz_payload.get("selected_contact_body_role") != "surface":
        blockers.append("surface_gz_selected_body_role_not_surface")
    if surface_gz_payload.get("parse_issues"):
        blockers.append("surface_gz_parse_issues_present")
    blockers.extend(_baseline_rejection_reasons(surface_gz_payload, surface_gz_baseline_payload))

    transform_payload = _transform_evidence_payload(str(surface_gz_payload.get("world_path") or ""), generated_at=generated_at)
    if not transform_payload.get("valid"):
        blockers.extend(transform_payload.get("blockers") or [])
    transform_path = _write_json(output_dir / "capability_surface_transform_evidence.json", transform_payload)
    baseline_path = _write_json(
        output_dir / "capability_surface_baseline_evidence.json",
        {
            "schema": "ur10e_contact_capability_surface_baseline_evidence_v1",
            "generated_at": generated_at,
            "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if not blockers else BLOCKED_CLAIM_TIER,
            "baseline_policy": "gazebo_contact_zero_no_contact_baseline",
            "baseline_contact_pair_path": surface_gz_baseline_payload.get("artifact_path"),
            "same_sensor_role_topic_transport_as_positive": not _baseline_rejection_reasons(
                surface_gz_payload,
                surface_gz_baseline_payload,
            ),
            "valid": not _baseline_rejection_reasons(surface_gz_payload, surface_gz_baseline_payload),
            "blockers": _baseline_rejection_reasons(surface_gz_payload, surface_gz_baseline_payload),
        },
    )
    source_path = _write_json(
        output_dir / "capability_surface_source_evidence.json",
        {
            "schema": "ur10e_contact_capability_source_evidence_v1",
            "generated_at": generated_at,
            "claim_tier": BLOCKED_CLAIM_TIER,
            "source_topic": surface_gz_payload.get("topic"),
            "source_schema": "gz.msgs.Contact.contact.wrench embedded in /contacts",
            "correlation_policy": "intramessage_same_contact_entry",
            "does_not_support": ["real bench/live contact", "Step5b unlock before M4"],
        },
    )

    verified_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    for index, row in enumerate(surface_gz_payload.get("rows") or []):
        if not isinstance(row, dict):
            rejected_rows.append({"row_index": index, "blockers": ["malformed_contact_row"]})
            continue
        row_blockers = _row_rejection_reasons(row, min_normal_load_n=min_normal_load_n)
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
        native["frame_policy"] = "verified_world_to_base_identity_from_contact_capability_witness_sdf"
        native["frame_transform_evidence"] = {
            "source": "contact_capability_witness_sdf_ur10e_base_frame_identity",
            "from_frame": "gazebo_contact_message_native_frame",
            "native_frame_interpreted_as": "world",
            "to_frame": "base",
            "stamp_s": native["wrench_stamp_s"],
            "artifact_path": str(transform_path),
        }
        native["status"] = "valid"
        native["baseline_policy"] = "gazebo_contact_zero_no_contact_baseline"
        native["baseline_evidence"] = {
            "source": "surface_sensor_no_contact_static_elevated_eoat_gz_topic_empty_log",
            "artifact_path": str(baseline_path),
        }
        native["source_evidence"] = {
            "source": "capability_surface_gz_contacts_topic",
            "artifact_path": str(source_path),
        }
        native["wrench_aggregation_policy"] = "raw_components_preserved_for_adapter_total_wrench_verification"
        verified_rows.append(verified_row)

    if not verified_rows:
        blockers.append("no_verified_surface_contact_wrench_rows")
    verified_path: str | None = None
    verified_payload: dict[str, Any] | None = None
    if verified_rows and not blockers:
        verified_contact_path = output_dir / VERIFIED_CONTACT_FILENAME
        verified_payload = {
            **{key: value for key, value in surface_gz_payload.items() if key != "rows"},
            "schema": capture.CONTACT_LOG_SCHEMA,
            "generated_at": generated_at,
            "mode": "offline_verified_surface_gz_contact_wrench_capability",
            "source": "gazebo_contact_sensor_topic_verified_surface_reaction",
            "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER,
            "target_claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER,
            "allowed_claim": "NON-Step5b forced-contact surface-side Gazebo contact wrench capability only",
            "forbidden_claim": "real bench/live contact; Step5b unlock before M4; simulated_ft upgrade",
            "row_count": len(verified_rows),
            "rows": verified_rows,
            "verification": {
                "surface_gate": True,
                "sensor_collision_role": "surface",
                "selected_contact_body_role": "surface",
                "correlation_policy": "intramessage_same_contact_entry",
                "transform_evidence_path": str(transform_path),
                "baseline_evidence_path": str(baseline_path),
                "source_evidence_path": str(source_path),
                "rejected_rows": rejected_rows,
            },
        }
        verified_payload["artifact_path"] = str(verified_contact_path)
        _write_json(verified_contact_path, verified_payload)
        verified_path = str(verified_contact_path)
    return {
        "schema": "ur10e_contact_capability_surface_verification_v1",
        "generated_at": generated_at,
        "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if verified_path else BLOCKED_CLAIM_TIER,
        "target_claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER,
        "verified_contact_pair_path": verified_path,
        "verified_payload": verified_payload,
        "verified_row_count": len(verified_rows) if verified_path else 0,
        "candidate_row_count": len(surface_gz_payload.get("rows") or []),
        "rejected_rows": rejected_rows,
        "blockers": _dedupe(blockers),
        "outputs": {
            "transform_evidence_path": str(transform_path),
            "baseline_evidence_path": str(baseline_path),
            "source_evidence_path": str(source_path),
        },
    }


def declared_delta_table(*, topic: str) -> list[dict[str, Any]]:
    return [
        {
            "dimension": "transport",
            "step5b_current": "ign topic / ign gazebo",
            "capability_surface_gz": "gz topic / gz sim",
            "capability_surface_ign_control": "ign topic / ign gazebo",
            "classification": "delta_under_test",
        },
        {
            "dimension": "sensor_role_collision",
            "step5b_current": "surface collision sensor",
            "capability_surface_gz": "surface collision sensor",
            "classification": "identical_role",
        },
        {
            "dimension": "source_topic",
            "step5b_current": "/ur10e/contact/gazebo/step5b/contacts",
            "capability_surface_gz": topic,
            "classification": "same_schema_different_topic_namespace",
        },
        {
            "dimension": "message_payload",
            "step5b_current": "Contact messages without captured wrench[] in last evidence run",
            "capability_surface_gz": "requires Contact messages with embedded wrench[]",
            "classification": "capability_gap_under_test",
        },
        {
            "dimension": "step5b_apply_policy",
            "step5b_current": "locked",
            "capability_surface_gz": "applies_to_step5b=false_until_M4",
            "classification": "planned_edit_not_authorized",
        },
    ]


def build_manifest(
    *,
    output_dir: Path,
    binary_identity: dict[str, Any],
    surface_gz_path: Path | None,
    surface_ign_path: Path | None,
    surface_gz_baseline_path: Path | None,
    eoat_gz_support_path: Path | None,
    verification: dict[str, Any],
    adapter_report_path: Path | None,
    topic: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or _now_iso()
    adapter: dict[str, Any] = _load_json(adapter_report_path) if adapter_report_path and adapter_report_path.is_file() else {}
    blockers: list[str] = []
    if not binary_identity.get("distinct_ign_gz_wrappers"):
        blockers.append("binary_identity_ign_gz_not_distinct_or_missing")
    if "/wrench" in topic:
        blockers.append("source_topic_must_be_contacts_not_wrench")
    if not surface_ign_path or not surface_ign_path.is_file():
        blockers.append("surface_ign_control_missing")
    if verification.get("verified_row_count", 0) <= 0:
        blockers.append("surface_gz_verified_native_wrench_rows_zero")
    if not adapter:
        blockers.append("adapter_report_missing")
    if adapter.get("source_topic") != topic:
        blockers.append("adapter_source_topic_not_contacts_topic")
    if adapter.get("trace_written") is not True:
        blockers.append("adapter_trace_not_written")
    if adapter.get("total_contact_wrench_proven") is not True:
        blockers.append("adapter_total_contact_wrench_not_proven")
    if adapter.get("wrench_aggregation_policy") != wrench_adapter.TOTAL_CONTACT_WRENCH_POLICY:
        blockers.append("adapter_wrench_policy_not_total_contact_wrench")
    if int(adapter.get("total_contact_wrench_row_count") or 0) <= 0:
        blockers.append("adapter_total_contact_wrench_rows_zero")
    if int(adapter.get("verified_native_wrench_row_count") or 0) <= 0:
        blockers.append("adapter_verified_native_wrench_rows_zero")
    blockers.extend(str(item) for item in verification.get("blockers") or [])
    gate_pass = not _dedupe(blockers)
    manifest = {
        "schema": CAPABILITY_SCHEMA,
        "generated_at": generated_at,
        "mode": "offline_no_live_non_step5b_forced_contact_capability_gate",
        "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if gate_pass else BLOCKED_CLAIM_TIER,
        "target_claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER,
        "allowed_claim": "NON-Step5b Gazebo contact wrench capability only" if gate_pass else "visual_only blocked/not_proven",
        "forbidden_claim": "real bench/live contact; Step5b unlock before M4; simulated_ft upgrade",
        "gate_pass": gate_pass,
        "applies_to_step5b": "false_until_M4",
        "step5b_attempt_spent": False,
        "source_topic": topic,
        "contact_wrench_correlation": "intramessage_same_contact_entry",
        "simulated_ft_used": False,
        "binary_identity_precheck": binary_identity,
        "captures": {
            "surface_gz": _capture_summary(surface_gz_path),
            "surface_ign_control": _capture_summary(surface_ign_path),
            "surface_gz_baseline": _capture_summary(surface_gz_baseline_path),
            "eoat_gz_supporting_control": _capture_summary(eoat_gz_support_path),
        },
        "verification": {
            "path": str(output_dir / "capability_surface_verification.json"),
            "verified_contact_pair_path": verification.get("verified_contact_pair_path"),
            "verified_row_count": verification.get("verified_row_count"),
            "candidate_row_count": verification.get("candidate_row_count"),
            "blockers": verification.get("blockers") or [],
        },
        "adapter": {
            "path": str(adapter_report_path) if adapter_report_path else None,
            "sha256": _sha256(adapter_report_path),
            "source_topic": adapter.get("source_topic"),
            "trace_written": adapter.get("trace_written"),
            "trace_path": adapter.get("wrench_trace_path"),
            "trace_sha256": _sha256(Path(str(adapter.get("wrench_trace_path")))) if adapter.get("wrench_trace_path") else None,
            "verified_native_wrench_row_count": adapter.get("verified_native_wrench_row_count"),
            "total_contact_wrench_row_count": adapter.get("total_contact_wrench_row_count"),
            "total_contact_wrench_proven": adapter.get("total_contact_wrench_proven"),
            "wrench_aggregation_policy": adapter.get("wrench_aggregation_policy"),
            "force_source": adapter.get("force_source"),
            "blockers": adapter.get("blockers") or [],
            "total_contact_wrench_blockers": adapter.get("total_contact_wrench_blockers") or [],
        },
        "declared_delta_table": declared_delta_table(topic=topic),
        "blockers": _dedupe(blockers),
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
    }
    _write_json(output_dir / MANIFEST_FILENAME, manifest)
    return manifest


def run_capability(
    output_dir: Path,
    *,
    topic: str = DEFAULT_TOPIC,
    timeout_s: float = 15.0,
    max_messages: int = 2,
    min_normal_load_n: float = 1.0,
    skip_captures: bool = False,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = _now_iso()
    binary_identity = binary_identity_precheck()
    _write_json(output_dir / "binary_identity_precheck.json", binary_identity)
    surface_gz_path = output_dir / "surface_gz" / "p2_gazebo_contact_pair_log.json"
    surface_ign_path = output_dir / "surface_ign_control" / "p2_gazebo_contact_pair_log.json"
    baseline_path = output_dir / "surface_gz_baseline" / "p2_gazebo_contact_pair_log.json"
    eoat_gz_path = output_dir / "eoat_gz_supporting_control" / "p2_gazebo_contact_pair_log.json"
    if not skip_captures:
        surface_gz_path = capture.capture_contact_pair_log(
            output_dir / "surface_gz",
            topic=topic,
            transport="gz",
            sensor_collision_role="surface",
            selected_contact_body_role="surface",
            timeout_s=timeout_s,
            max_messages=max_messages,
            observation_scope=OBSERVATION_SCOPE,
        )
        surface_ign_path = capture.capture_contact_pair_log(
            output_dir / "surface_ign_control",
            topic=topic,
            transport="ignition",
            sensor_collision_role="surface",
            selected_contact_body_role="surface",
            timeout_s=timeout_s,
            max_messages=max_messages,
            observation_scope=OBSERVATION_SCOPE,
        )
        baseline_path = capture.capture_contact_pair_log(
            output_dir / "surface_gz_baseline",
            topic=topic,
            transport="gz",
            sensor_collision_role="surface",
            selected_contact_body_role="surface",
            eoat_pose_z=0.2,
            eoat_static=True,
            allow_no_messages=True,
            baseline_mode="no_contact_static_elevated_surface_sensor",
            timeout_s=timeout_s,
            max_messages=1,
            observation_scope=OBSERVATION_SCOPE,
        )
        eoat_gz_path = capture.capture_contact_pair_log(
            output_dir / "eoat_gz_supporting_control",
            topic=topic,
            transport="gz",
            sensor_collision_role="eoat",
            selected_contact_body_role="eoat",
            timeout_s=timeout_s,
            max_messages=1,
            observation_scope=OBSERVATION_SCOPE,
        )
    surface_gz_payload = {**_load_json(surface_gz_path), "artifact_path": str(surface_gz_path)}
    baseline_payload = {**_load_json(baseline_path), "artifact_path": str(baseline_path)}
    verification = build_verified_surface_contact_pair(
        surface_gz_payload=surface_gz_payload,
        surface_gz_baseline_payload=baseline_payload,
        output_dir=output_dir / "verified",
        generated_at=generated_at,
        min_normal_load_n=min_normal_load_n,
    )
    verification_path = _write_json(output_dir / "capability_surface_verification.json", verification)
    verified_payload = verification.get("verified_payload")
    adapter_report_path: Path | None = None
    if isinstance(verified_payload, dict):
        adapter_report_path = wrench_adapter.write_wrench_trace_or_report(
            output_dir / "adapter",
            contact_pair_path=Path(str(verification["verified_contact_pair_path"])),
            generated_at=generated_at,
            source_topic=topic,
            report_filename=ADAPTER_REPORT_FILENAME,
            trace_filename=ADAPTER_TRACE_FILENAME,
            observation_scope=OBSERVATION_SCOPE,
        )
    manifest = build_manifest(
        output_dir=output_dir,
        binary_identity=binary_identity,
        surface_gz_path=surface_gz_path,
        surface_ign_path=surface_ign_path,
        surface_gz_baseline_path=baseline_path,
        eoat_gz_support_path=eoat_gz_path,
        verification={**verification, "path": str(verification_path)},
        adapter_report_path=adapter_report_path,
        topic=topic,
        generated_at=generated_at,
    )
    return Path(str(_write_json(output_dir / MANIFEST_FILENAME, manifest)))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a NON-Step5b Gazebo contact wrench capability gate.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--max-messages", type=int, default=2)
    parser.add_argument("--min-normal-load-n", type=float, default=1.0)
    parser.add_argument("--skip-captures", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = run_capability(
        args.output_dir,
        topic=args.topic,
        timeout_s=args.timeout_s,
        max_messages=args.max_messages,
        min_normal_load_n=args.min_normal_load_n,
        skip_captures=args.skip_captures,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
