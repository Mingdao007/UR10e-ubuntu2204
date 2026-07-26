#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
SRC_PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(SRC_PACKAGE) not in sys.path:
    sys.path.insert(0, str(SRC_PACKAGE))

from ur10e_example_controllers import canonical_wrench_contract as contract  # noqa: E402


REPORT_SCHEMA = "ur10e_gazebo_contact_wrench_adapter_report_v1"
REPORT_FILENAME = "p2_gazebo_contact_wrench_adapter_report.json"
TRACE_FILENAME = "p2_gazebo_contact_wrench_trace.json"
DEFAULT_SOURCE_TOPIC = "/ur10e/contact/gazebo/p2_contact_witness/wrench"
BLOCKED_CLAIM_TIER = "visual_only"
PHYSICAL_GAZEBO_CLAIM_TIER = "physical Gazebo collision/contact physics"
ALLOWED_NATIVE_WRENCH_SOURCE = "gazebo_contact_message_wrench"
ALLOWED_NATIVE_WRENCH_SCHEMAS = {
    "ignition.msgs.Contact.contact.wrench",
    "gz.msgs.Contact.contact.wrench",
}
REQUIRED_NATIVE_FIELDS = (
    "native_gazebo_contact_wrench.force_n",
    "native_gazebo_contact_wrench.source=gazebo_contact_message_wrench",
    "native_gazebo_contact_wrench.source_schema in ignition.msgs.Contact.contact.wrench,gz.msgs.Contact.contact.wrench",
    "native_gazebo_contact_wrench.force_source_class=gazebo_contact",
    "native_gazebo_contact_wrench.measured_contact_wrench=true",
    "native_gazebo_contact_wrench.commanded_force=false",
    "native_gazebo_contact_wrench.frame_id=base",
    "native_gazebo_contact_wrench.frame_transform_evidence.source/from_frame/to_frame/stamp_s/artifact_path",
    "native_gazebo_contact_wrench.status=valid",
    "native_gazebo_contact_wrench.wrench_stamp_s",
    "native_gazebo_contact_wrench.wrench_stamp_evidence=true",
    "stamp_evidence=true",
    "stamp_s",
    "normal",
    "contact_count",
)
SINGLE_POINT_WRENCH_POLICY = "single_native_contact_point_wrench_sample_no_total_contact_wrench_claim"
TOTAL_CONTACT_WRENCH_POLICY = "total_contact_wrench"
TOTAL_CONTACT_FRAME_POLICIES = {
    "pretransformed_to_base",
    "verified_world_to_base_identity_from_p2_witness_sdf",
    "verified_world_to_base_identity_from_contact_capability_witness_sdf",
    "verified_world_to_base_identity_from_step5b_transport_probe_sdf",
}
STANDALONE_P2_OBSERVATION_SCOPE = "standalone_p2_contact_witness"
EPS = 1e-9


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def _sub3(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2])


def _norm3(value: tuple[float, float, float]) -> float:
    return math.sqrt(_dot3(value, value))


def _close3(left: tuple[float, float, float], right: tuple[float, float, float], *, tolerance: float = 1e-6) -> bool:
    return _norm3(_sub3(left, right)) <= tolerance


def _raw_native_wrench(row: dict[str, Any]) -> dict[str, Any] | None:
    native = row.get("native_gazebo_contact_wrench")
    return native if isinstance(native, dict) else None


def _verified_native_wrench(
    row: dict[str, Any],
) -> tuple[tuple[float, float, float], tuple[float, float, float], dict[str, Any]] | None:
    native = _raw_native_wrench(row)
    if native is None or _native_wrench_blockers(row):
        return None
    try:
        force = _vec3(native["force_n"], label="force_n")
        torque = _vec3(native.get("torque_nm") or [0.0, 0.0, 0.0], label="torque_nm")
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (*force, *torque)):
        return None
    return force, torque, native


def _contact_count(row: dict[str, Any]) -> int:
    try:
        return int(row.get("contact_count") or 0)
    except (TypeError, ValueError):
        return 0


def _native_wrench_blockers(row: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if "native_wrench" in row or "force_n" in row:
        blockers.append("legacy_force_field_contamination")
    native = _raw_native_wrench(row)
    if native is None:
        if "native_wrench" in row or "force_n" in row:
            blockers.append("missing_verified_gazebo_contact_wrench_provenance")
            return _dedupe(blockers)
        blockers.append("missing_native_gazebo_wrench_or_force_vector")
        return _dedupe(blockers)
    if (
        native.get("source") != ALLOWED_NATIVE_WRENCH_SOURCE
        or native.get("source_schema") not in ALLOWED_NATIVE_WRENCH_SCHEMAS
        or native.get("force_source_class") != contract.SOURCE_GAZEBO_CONTACT
    ):
        blockers.append("missing_verified_gazebo_contact_wrench_provenance")
    if native.get("measured_contact_wrench") is not True or native.get("commanded_force") is not False:
        blockers.append("commanded_or_unverified_wrench_force")
    selected_role = str(native.get("selected_body_role") or "")
    expected_body, expected_collision = _expected_wrench_side(row, selected_body_role=selected_role)
    if expected_body is None:
        blockers.append("no_matching_eoat_surface_contact_pair")
    if (
        selected_role not in {"eoat", "surface"}
        or native.get("selected_body") != expected_body
        or native.get("selected_body_collision") != expected_collision
    ):
        blockers.append("missing_eoat_wrench_body_selection" if selected_role != "surface" else "missing_surface_wrench_body_selection")
        blockers.append("wrench_body_collision_mismatch")
    if native.get("frame_id") != "base" or not _valid_transform_evidence(native):
        blockers.append("missing_base_frame_transform_evidence")
    if native.get("status") != "valid":
        blockers.append("native_wrench_status_not_valid")
    if native.get("baseline_policy") != "gazebo_contact_zero_no_contact_baseline":
        blockers.append("missing_valid_gazebo_contact_baseline")
    try:
        stamp_s = float(row["stamp_s"])
        wrench_stamp_s = float(native["wrench_stamp_s"])
    except (KeyError, TypeError, ValueError):
        blockers.append("missing_timestamped_native_wrench")
    else:
        if row.get("stamp_evidence") is not True or native.get("wrench_stamp_evidence") is not True:
            blockers.append("missing_timestamped_native_wrench")
        if abs(stamp_s - wrench_stamp_s) > 0.02:
            blockers.append("wrench_stamp_not_contact_aligned")
    try:
        _vec3(native["force_n"], label="force_n")
        _vec3(native.get("torque_nm") or [0.0, 0.0, 0.0], label="torque_nm")
        reaction_normal = _vec3(row.get("normal") or [0.0, 0.0, 1.0], label="normal")
        force = _vec3(native["force_n"], label="force_n")
    except (KeyError, TypeError, ValueError):
        blockers.append("invalid_native_wrench_vector")
    else:
        if _dot3(force, reaction_normal) <= 0.0:
            blockers.append("no_positive_normal_load_from_native_gazebo_wrench")
    if _contact_count(row) <= 0:
        blockers.append("missing_contact_count")
    return _dedupe(blockers)


def _expected_eoat_wrench_side(row: dict[str, Any]) -> tuple[str | None, str | None]:
    return _expected_wrench_side(row, selected_body_role="eoat")


def _expected_wrench_side(row: dict[str, Any], *, selected_body_role: str) -> tuple[str | None, str | None]:
    collision1 = str(row.get("collision1") or "")
    collision2 = str(row.get("collision2") or "")
    if selected_body_role == "eoat":
        if _is_eoat_collision(collision1) and _is_surface_collision(collision2):
            return "body_1_wrench", "collision1"
        if _is_eoat_collision(collision2) and _is_surface_collision(collision1):
            return "body_2_wrench", "collision2"
    if selected_body_role == "surface":
        if _is_surface_collision(collision1) and _is_eoat_collision(collision2):
            return "body_1_wrench", "collision1"
        if _is_surface_collision(collision2) and _is_eoat_collision(collision1):
            return "body_2_wrench", "collision2"
    return None, None


def _is_eoat_collision(name: str) -> bool:
    return "eoat" in name and "collision" in name


def _is_surface_collision(name: str) -> bool:
    return "contact_surface" in name or "surface::collision" in name


def _valid_transform_evidence(native: dict[str, Any]) -> bool:
    evidence = native.get("frame_transform_evidence")
    if not isinstance(evidence, dict):
        return False
    if not evidence.get("source") or not evidence.get("artifact_path"):
        return False
    if evidence.get("from_frame") != "gazebo_contact_message_native_frame":
        return False
    if evidence.get("to_frame") != "base":
        return False
    try:
        transform_stamp_s = float(evidence["stamp_s"])
        wrench_stamp_s = float(native["wrench_stamp_s"])
    except (KeyError, TypeError, ValueError):
        return False
    return abs(transform_stamp_s - wrench_stamp_s) <= 0.02


def _total_contact_frame_policy_valid(native: dict[str, Any]) -> bool:
    if native.get("frame_policy") in TOTAL_CONTACT_FRAME_POLICIES:
        return True
    evidence = native.get("frame_transform_evidence")
    return (
        isinstance(evidence, dict)
        and evidence.get("source") == "p2_witness_sdf_ur10e_base_frame_identity"
        and evidence.get("native_frame_interpreted_as") == "world"
        and evidence.get("to_frame") == "base"
    ) or (
        isinstance(evidence, dict)
        and evidence.get("source") == "contact_capability_witness_sdf_ur10e_base_frame_identity"
        and evidence.get("native_frame_interpreted_as") == "world"
        and evidence.get("to_frame") == "base"
    ) or (
        isinstance(evidence, dict)
        and evidence.get("source") == "step5b_transport_probe_sdf_ur10e_base_frame_identity"
        and evidence.get("native_frame_interpreted_as") == "world"
        and evidence.get("to_frame") == "base"
    )


def _raw_contact_wrenches(row: dict[str, Any]) -> list[Any]:
    wrenches = row.get("raw_gazebo_contact_wrenches")
    return wrenches if isinstance(wrenches, list) else []


def _raw_wrench_body_payload(wrench: dict[str, Any], body: str) -> tuple[dict[str, Any] | None, str | None]:
    aliases = {
        "body_1_wrench": ("body_1_wrench", "body1Wrench"),
        "body_2_wrench": ("body_2_wrench", "body2Wrench"),
    }[body]
    for field in aliases:
        payload = wrench.get(field)
        if isinstance(payload, dict):
            return payload, field
    return None, None


def _other_body(body: str) -> str:
    return "body_2_wrench" if body == "body_1_wrench" else "body_1_wrench"


def _total_contact_wrench_evidence(row: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    verified = _verified_native_wrench(row)
    if verified is None:
        return None, ["verified_native_wrench_required_for_total_contact_wrench"]
    native_force, _native_torque, native = verified
    raw_wrenches = _raw_contact_wrenches(row)
    if not raw_wrenches:
        return None, ["missing_raw_gazebo_contact_wrenches"]
    if not _total_contact_frame_policy_valid(native):
        return None, ["total_contact_wrench_requires_base_identity_or_pretransformed_frame_policy"]

    blockers: list[str] = []
    try:
        native_raw_count = int(native.get("raw_wrench_count"))
    except (TypeError, ValueError):
        native_raw_count = -1
        blockers.append("native_raw_wrench_count_missing")
    if native_raw_count != len(raw_wrenches):
        blockers.append("native_raw_wrench_count_mismatch")
    contact_count = _contact_count(row)
    if contact_count != len(raw_wrenches):
        blockers.append("raw_wrench_count_contact_count_mismatch")

    selected_body = str(native.get("selected_body") or "")
    if selected_body not in {"body_1_wrench", "body_2_wrench"}:
        blockers.append("total_contact_wrench_missing_selected_body")
        selected_body = "body_1_wrench"
    other_body = _other_body(selected_body)

    reaction_normal = _vec3(row.get("normal") or [0.0, 0.0, 1.0], label="normal")
    force_total = (0.0, 0.0, 0.0)
    torque_total = (0.0, 0.0, 0.0)
    component_loads: list[float] = []
    component_count = 0
    for index, raw_wrench in enumerate(raw_wrenches):
        if not isinstance(raw_wrench, dict):
            blockers.append(f"raw_wrench_{index}:malformed")
            continue
        selected_payload, _selected_field = _raw_wrench_body_payload(raw_wrench, selected_body)
        other_payload, _other_field = _raw_wrench_body_payload(raw_wrench, other_body)
        if selected_payload is None:
            blockers.append(f"raw_wrench_{index}:missing_selected_body_wrench")
            continue
        if other_payload is None:
            blockers.append(f"raw_wrench_{index}:missing_other_body_wrench")
            continue
        try:
            selected_force = _vec3(selected_payload.get("force"), label=f"raw_wrench_{index}.selected.force")
            selected_torque = _vec3(
                selected_payload.get("torque") or [0.0, 0.0, 0.0],
                label=f"raw_wrench_{index}.selected.torque",
            )
            other_force = _vec3(other_payload.get("force"), label=f"raw_wrench_{index}.other.force")
        except (TypeError, ValueError):
            blockers.append(f"raw_wrench_{index}:invalid_wrench_vector")
            continue
        if not all(math.isfinite(value) for value in (*selected_force, *selected_torque, *other_force)):
            blockers.append(f"raw_wrench_{index}:nonfinite_wrench_vector")
            continue
        if not _close3(_add3(selected_force, other_force), (0.0, 0.0, 0.0), tolerance=1e-6):
            blockers.append(f"raw_wrench_{index}:body_force_pair_not_balanced")
        component_load = _dot3(selected_force, reaction_normal)
        if component_load <= EPS:
            blockers.append(f"raw_wrench_{index}:nonpositive_normal_load")
        component_loads.append(component_load)
        force_total = _add3(force_total, selected_force)
        torque_total = _add3(torque_total, selected_torque)
        component_count += 1

    try:
        raw_index = int(native.get("raw_wrench_index"))
    except (TypeError, ValueError):
        raw_index = -1
        blockers.append("native_raw_wrench_index_missing")
    if 0 <= raw_index < len(raw_wrenches):
        selected_payload, _selected_field = _raw_wrench_body_payload(raw_wrenches[raw_index], selected_body)
        if selected_payload is None:
            blockers.append("native_raw_wrench_index_selected_body_missing")
        else:
            try:
                indexed_force = _vec3(selected_payload.get("force"), label="native_raw_wrench_index.force")
            except (TypeError, ValueError):
                blockers.append("native_raw_wrench_index_force_invalid")
            else:
                if not _close3(indexed_force, native_force, tolerance=1e-6):
                    blockers.append("native_raw_wrench_index_force_mismatch")
    else:
        blockers.append("native_raw_wrench_index_out_of_range")

    if component_count != len(raw_wrenches):
        blockers.append("raw_wrench_component_count_incomplete")
    total_normal_load = _dot3(force_total, reaction_normal)
    if total_normal_load <= EPS:
        blockers.append("total_contact_wrench_normal_load_not_positive")
    if blockers:
        return None, _dedupe(blockers)
    return (
        {
            "component_count": component_count,
            "force_n": force_total,
            "torque_nm": torque_total,
            "normal_load_n": total_normal_load,
            "component_normal_loads_n": component_loads,
            "selected_body": selected_body,
            "frame_policy": native.get("frame_policy"),
            "frame_transform_evidence": native.get("frame_transform_evidence"),
        },
        [],
    )


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _blocker_summary(blockers: list[str]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for blocker in blockers:
        summary[blocker] = summary.get(blocker, 0) + 1
    return dict(sorted(summary.items()))


def _row_diagnostic(row: dict[str, Any], *, sequence: int) -> dict[str, Any]:
    native = _raw_native_wrench(row)
    blockers = _native_wrench_blockers(row)
    expected_body, expected_collision = _expected_eoat_wrench_side(row)
    return {
        "sequence": sequence,
        "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if not blockers else BLOCKED_CLAIM_TIER,
        "stamp_s": row.get("stamp_s"),
        "stamp_evidence": row.get("stamp_evidence") is True,
        "collision1": row.get("collision1"),
        "collision2": row.get("collision2"),
        "expected_eoat_wrench_body": expected_body,
        "expected_eoat_wrench_collision": expected_collision,
        "contact_count": _contact_count(row),
        "normal_source": row.get("normal_source"),
        "has_depth_m": row.get("depth_m") is not None,
        "legacy_force_field_present": "native_wrench" in row or "force_n" in row,
        "native_wrench_present": native is not None,
        "native_wrench_source": native.get("source") if native else None,
        "native_wrench_source_schema": native.get("source_schema") if native else None,
        "native_wrench_force_source_class": native.get("force_source_class") if native else None,
        "native_wrench_frame_id": native.get("frame_id") if native else None,
        "native_wrench_status": native.get("status") if native else None,
        "native_wrench_baseline_policy": native.get("baseline_policy") if native else None,
        "native_wrench_stamp_s": native.get("wrench_stamp_s") if native else None,
        "native_wrench_stamp_evidence": native.get("wrench_stamp_evidence") is True if native else False,
        "frame_transform_evidence_type": type(native.get("frame_transform_evidence")).__name__ if native else None,
        "verified_native_wrench": _verified_native_wrench(row) is not None,
        "blockers": blockers,
    }


def _malformed_row_diagnostic(row: Any, *, sequence: int) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "claim_tier": BLOCKED_CLAIM_TIER,
        "native_wrench_present": False,
        "verified_native_wrench": False,
        "blockers": ["malformed_contact_pair_row"],
        "row_type": type(row).__name__,
    }


def _evidence_contract() -> dict[str, Any]:
    return {
        "required_native_fields": list(REQUIRED_NATIVE_FIELDS),
        "accepted_native_wrench_source": ALLOWED_NATIVE_WRENCH_SOURCE,
        "accepted_native_wrench_schemas": sorted(ALLOWED_NATIVE_WRENCH_SCHEMAS),
        "accepted_force_source_class": contract.SOURCE_GAZEBO_CONTACT,
        "accepted_frame_id": "base",
        "accepted_status": "valid",
        "accepted_baseline_policy": "gazebo_contact_zero_no_contact_baseline",
        "required_transform_evidence_fields": [
            "source",
            "from_frame=gazebo_contact_message_native_frame",
            "to_frame=base",
            "stamp_s",
            "artifact_path",
        ],
        "forbidden_force_sources": [
            "simulated_ft",
            "virtual/software force-loop",
            "inferred force from contact position/normal/depth",
            "legacy row-level force_n/native_wrench fields without Gazebo provenance",
            "real bench/live contact",
        ],
        "next_capture_requirements": [
            "Gazebo contact row contains native_gazebo_contact_wrench",
            "native wrench selected body is the EOAT collision side, or an explicit surface reaction side for a surface-sensor capability gate",
            "native wrench is transformed to base with timestamped transform evidence",
            "native wrench status is valid and baseline policy is gazebo_contact_zero_no_contact_baseline",
            "normal_load_n = dot(force_base, reaction_normal) is positive",
        ],
    }


def _forbidden_claim(total_contact_wrench_proven: bool, observation_scope: str | None) -> str:
    claims = [
        "real bench/live contact",
        "simulated_ft",
        "inferred force from contact position/normal/depth",
    ]
    if not total_contact_wrench_proven:
        claims.append("total contact wrench across all Gazebo contact points")
    if observation_scope == STANDALONE_P2_OBSERVATION_SCOPE:
        claims.extend(
            [
                "per-stage physical Gazebo contact upgrade",
                "same-run dual-sensor/integrated binding upgrade",
            ]
        )
    return "; ".join(claims)


def _sample_from_row(
    row: dict[str, Any],
    *,
    sequence: int,
    total_wrench_evidence: dict[str, Any] | None = None,
) -> contract.CanonicalWrenchSample | None:
    native = _verified_native_wrench(row)
    if native is None or _contact_count(row) <= 0:
        return None
    force, torque, native_payload = native
    quality = "gazebo_contact_native_wrench"
    reaction_normal = _vec3(row.get("normal") or [0.0, 0.0, 1.0], label="normal")
    total_contact = total_wrench_evidence is not None
    if total_contact:
        force = total_wrench_evidence["force_n"]
        torque = total_wrench_evidence["torque_nm"]
        quality = "gazebo_contact_total_native_wrench"
    if _dot3(force, reaction_normal) <= 0.0:
        return None
    flags = [
        "gazebo_contact_wrench_adapter",
        str(native_payload["source"]),
        str(native_payload["source_schema"]),
        "frame_transform_evidence_provided",
        str(row.get("normal_source") or "normal_source_unspecified"),
    ]
    if total_contact:
        flags.extend(
            [
                "total_contact_wrench",
                f"total_contact_wrench_component_count={total_wrench_evidence['component_count']}",
            ]
        )
    else:
        flags.extend(["single_contact_point_wrench_sample", "total_contact_wrench_not_proven"])
    if native_payload.get("raw_wrench_count") is not None:
        flags.append(f"raw_gazebo_contact_wrench_count={native_payload['raw_wrench_count']}")
    if native_payload.get("raw_wrench_index") is not None:
        flags.append(f"raw_gazebo_contact_wrench_index={native_payload['raw_wrench_index']}")
    return contract.CanonicalWrenchSample(
        stamp_s=float(native_payload["wrench_stamp_s"]),
        frame_id="base",
        force_n=force,
        torque_nm=torque,
        source=contract.SOURCE_GAZEBO_CONTACT,
        valid=True,
        quality=quality,
        status="valid",
        baseline_policy=str(native_payload["baseline_policy"]),
        latency_s=0.0,
        stale_after_s=0.1,
        diagnostic_flags=tuple(flags),
        sequence=sequence,
        contact_state="contact",
        reaction_normal=reaction_normal,
        approach_normal=(-reaction_normal[0], -reaction_normal[1], -reaction_normal[2]),
    )


def build_wrench_trace_or_report(
    contact_pair_payload: dict[str, Any],
    *,
    generated_at: str | None = None,
    source_topic: str = DEFAULT_SOURCE_TOPIC,
    stage_id: str | None = None,
    observation_id: str | None = None,
    time_window: dict[str, Any] | None = None,
    observation_scope: str | None = None,
) -> dict[str, Any]:
    rows = contact_pair_payload.get("rows") or []
    native_wrench_row_count = sum(1 for row in rows if isinstance(row, dict) and _raw_native_wrench(row) is not None)
    verified_native_wrench_row_count = sum(
        1 for row in rows if isinstance(row, dict) and _verified_native_wrench(row) is not None
    )
    blockers: list[str] = []
    row_diagnostics: list[dict[str, Any]] = []
    samples: list[contract.CanonicalWrenchSample] = []
    total_contact_wrench_row_count = 0
    total_contact_wrench_blockers: list[str] = []
    if contact_pair_payload.get("parse_issues"):
        blockers.append("contact_pair_parse_issues_present")
    for sequence, row in enumerate(rows):
        if not isinstance(row, dict):
            blockers.append("malformed_contact_pair_row")
            row_diagnostics.append(_malformed_row_diagnostic(row, sequence=sequence))
            continue
        row_diagnostics.append(_row_diagnostic(row, sequence=sequence))
        row_blockers = _native_wrench_blockers(row)
        blockers.extend(row_blockers)
        if not row_blockers:
            total_evidence, total_blockers = _total_contact_wrench_evidence(row)
            if total_blockers:
                total_contact_wrench_blockers.extend(total_blockers)
            if total_evidence is not None:
                total_contact_wrench_row_count += 1
            row_diagnostics[-1]["total_contact_wrench_proven"] = total_evidence is not None
            row_diagnostics[-1]["total_contact_wrench_component_count"] = (
                total_evidence.get("component_count") if total_evidence else 0
            )
            row_diagnostics[-1]["total_contact_wrench_blockers"] = total_blockers
            sample = _sample_from_row(row, sequence=sequence, total_wrench_evidence=total_evidence)
            if sample is None:
                blockers.append("verified_native_wrench_sample_build_failed")
            else:
                samples.append(sample)
    blocker_summary = _blocker_summary(blockers)
    blockers = _dedupe(blockers)
    if not rows:
        blockers = ["missing_contact_pair_rows"]
        blocker_summary = _blocker_summary(blockers)
    trace = contract.trace_payload(samples, source_topic=source_topic) if samples and not blockers else None
    total_contact_wrench_proven = bool(
        trace
        and samples
        and total_contact_wrench_row_count == len(samples)
        and not _dedupe(total_contact_wrench_blockers)
    )
    wrench_policy = TOTAL_CONTACT_WRENCH_POLICY if total_contact_wrench_proven else SINGLE_POINT_WRENCH_POLICY
    effective_observation_scope = (
        observation_scope if observation_scope is not None else contact_pair_payload.get("observation_scope")
    )
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": generated_at or _now_iso(),
        "mode": "offline_no_live_gazebo_contact_wrench_adapter",
        "goal_lineage": "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md",
        "stage_id": stage_id,
        "observation_id": observation_id,
        "time_window": time_window,
        "observation_scope": effective_observation_scope,
        "trace_written": bool(trace),
        "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if trace else BLOCKED_CLAIM_TIER,
        "target_claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER,
        "allowed_claim": (
            "physical Gazebo collision/contact physics with native gazebo_contact total contact wrench correlation"
            if trace and total_contact_wrench_proven
            else "physical Gazebo collision/contact physics with native gazebo_contact single contact-point wrench correlation"
            if trace
            else "visual_only blocked/not_proven; contact pair evidence cannot be upgraded into force evidence"
        ),
        "forbidden_claim": _forbidden_claim(total_contact_wrench_proven, effective_observation_scope),
        "wrench_aggregation_policy": wrench_policy,
        "total_contact_wrench_proven": total_contact_wrench_proven,
        "total_contact_wrench_row_count": total_contact_wrench_row_count,
        "total_contact_wrench_blockers": _dedupe(total_contact_wrench_blockers),
        "force_source": contract.SOURCE_GAZEBO_CONTACT if trace else None,
        "native_wrench_source_class": contract.SOURCE_GAZEBO_CONTACT if native_wrench_row_count > 0 else None,
        "source_topic": source_topic,
        "source_contact_pair_log_schema": contact_pair_payload.get("schema"),
        "source_contact_pair_row_count": len(rows),
        "native_wrench_row_count": native_wrench_row_count,
        "verified_native_wrench_row_count": verified_native_wrench_row_count,
        "required_native_fields": list(REQUIRED_NATIVE_FIELDS),
        "evidence_contract": _evidence_contract(),
        "row_diagnostics": row_diagnostics,
        "blocker_summary": blocker_summary,
        "blockers": blockers,
        "claim_boundary_gate": {
            "visual_only_inputs_do_not_prove_force": True,
            "contact_pair_only_does_not_prove_wrench": True,
            "simulated_ft_is_not_physical_gazebo_contact": True,
            "real_bench_live_contact_authorized": False,
            "total_contact_wrench_proven": total_contact_wrench_proven,
        },
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
        "wrench_trace": trace,
    }


def write_wrench_trace_or_report(
    output_dir: Path,
    *,
    contact_pair_path: Path,
    generated_at: str | None = None,
    source_topic: str = DEFAULT_SOURCE_TOPIC,
    report_filename: str = REPORT_FILENAME,
    trace_filename: str = TRACE_FILENAME,
    stage_id: str | None = None,
    observation_id: str | None = None,
    time_window: dict[str, Any] | None = None,
    observation_scope: str | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    contact_pair_payload = _load_json(contact_pair_path)
    report = build_wrench_trace_or_report(
        contact_pair_payload,
        generated_at=generated_at,
        source_topic=source_topic,
        stage_id=stage_id,
        observation_id=observation_id,
        time_window=time_window,
        observation_scope=observation_scope,
    )
    report["inputs"] = {"contact_pair_path": str(contact_pair_path)}
    if isinstance(report.get("wrench_trace"), dict):
        trace_path = _output_child(output_dir, trace_filename)
        trace_path.write_text(json.dumps(report["wrench_trace"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["wrench_trace_path"] = str(trace_path)
    else:
        report["wrench_trace_path"] = None
    report_path = _output_child(output_dir, report_filename)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report_path


def _output_child(output_dir: Path, filename: str) -> Path:
    child = Path(filename)
    if child.is_absolute() or len(child.parts) != 1:
        raise ValueError(f"output filename must be a simple filename: {filename}")
    return output_dir / child


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--contact-pair", required=True, type=Path)
    parser.add_argument("--source-topic", default=DEFAULT_SOURCE_TOPIC)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--report-filename", default=REPORT_FILENAME)
    parser.add_argument("--trace-filename", default=TRACE_FILENAME)
    parser.add_argument("--stage-id", default=None)
    parser.add_argument("--observation-id", default=None)
    parser.add_argument("--time-window-start", default=None)
    parser.add_argument("--time-window-end", default=None)
    parser.add_argument("--clock-source", default=None)
    parser.add_argument("--observation-scope", default=None)
    args = parser.parse_args()
    time_window = None
    if args.time_window_start or args.time_window_end or args.clock_source:
        time_window = {
            "start": args.time_window_start,
            "end": args.time_window_end,
            "clock_source": args.clock_source,
        }

    report_path = write_wrench_trace_or_report(
        args.output_dir,
        contact_pair_path=args.contact_pair,
        generated_at=args.generated_at,
        source_topic=args.source_topic,
        report_filename=args.report_filename,
        trace_filename=args.trace_filename,
        stage_id=args.stage_id,
        observation_id=args.observation_id,
        time_window=time_window,
        observation_scope=args.observation_scope,
    )
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
