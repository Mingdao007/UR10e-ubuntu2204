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
    ACTIVE_TCP_LINK,
    ACTIVE_TCP_OFFSET_TOOL0_M,
    EOAT_CONTACT_COLLISION,
    EOAT_FIXED_JOINT,
    EOAT_GEOMETRY_FIDELITY,
    NATIVE_CONTACT_TOPIC,
    NATIVE_FT_TOPIC,
    audit_robot_description,
    backend_spec,
    validate_tick_record,
)
from ur10e_gazebo_v2_runtime_adapter import (  # noqa: E402
    RUNTIME_SCHEMA,
    validate_runtime_manifest,
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
TICK_SCHEMA_PATH = EXPERIMENT_ROOT / "config" / "schemas" / "ur10e_gazebo_v2_tick_v1.schema.json"
REQUIRED_BINDING_IDS = frozenset(
    {
        "world",
        "velocity_controller",
        "effort_surrogate_controller",
        "initial_positions",
        "gazebo_v2_code",
        "gazebo_matrix_code",
        "launch",
        "lane_contract",
        "tick_schema",
        "eoat_visual_proxy",
        "calibration",
        "ur_xacro",
    }
)
RUNTIME_IMPLEMENTATION_BLOCKERS = frozenset(
    {
        "production_step5d_adapter_runtime_node_not_implemented",
        "concurrent_camera_capture_not_implemented",
    }
)


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


def _vector3(value: Any) -> list[float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)) or len(value) != 3:
        return None
    if not all(_finite(item) for item in value):
        return None
    return [float(item) for item in value]


def _unit3(value: Any) -> list[float] | None:
    vector = _vector3(value)
    if vector is None:
        return None
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 1e-12:
        return None
    return [item / norm for item in vector]


def _dot3(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(float(a) * float(b) for a, b in zip(left[:3], right[:3]))


def _force_norm(wrench: Sequence[float]) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in wrench[:3]))


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _finite(value)
    if expected == "boolean":
        return isinstance(value, bool)
    return False


def _resolve_local_ref(root: Mapping[str, Any], ref: str) -> Mapping[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    value: Any = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, Mapping) or token not in value:
            return None
        value = value[token]
    return value if isinstance(value, Mapping) else None


def _json_schema_issues(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any], path: str) -> list[str]:
    issues: list[str] = []
    ref = schema.get("$ref")
    if isinstance(ref, str):
        resolved = _resolve_local_ref(root, ref)
        if resolved is None:
            return [f"{path}:unresolved_ref:{ref}"]
        return _json_schema_issues(value, resolved, root, path)
    expected = schema.get("type")
    if isinstance(expected, str) and not _schema_type_matches(value, expected):
        return [f"{path}:type_not_{expected}"]
    if "const" in schema and value != schema["const"]:
        issues.append(f"{path}:const_mismatch")
    enum = schema.get("enum")
    if isinstance(enum, Sequence) and value not in enum:
        issues.append(f"{path}:enum_mismatch")
    if isinstance(value, str) and isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
        issues.append(f"{path}:min_length")
    if _finite(value) and _finite(schema.get("minimum")) and float(value) < float(schema["minimum"]):
        issues.append(f"{path}:minimum")
    if isinstance(value, Mapping):
        required = schema.get("required") if isinstance(schema.get("required"), Sequence) else ()
        for key in required:
            if key not in value:
                issues.append(f"{path}.{key}:required")
        if isinstance(schema.get("minProperties"), int) and len(value) < schema["minProperties"]:
            issues.append(f"{path}:min_properties")
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    issues.append(f"{path}.{key}:additional_property")
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, Mapping):
                issues.extend(_json_schema_issues(value[key], child_schema, root, f"{path}.{key}"))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            issues.append(f"{path}:min_items")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            issues.append(f"{path}:max_items")
        child_schema = schema.get("items")
        if isinstance(child_schema, Mapping):
            for index, item in enumerate(value):
                issues.extend(_json_schema_issues(item, child_schema, root, f"{path}[{index}]"))
    return issues


def validate_tick_json_schema(
    record: Mapping[str, Any],
    *,
    schema_path: Path = TICK_SCHEMA_PATH,
) -> list[str]:
    schema = load_json(schema_path)
    return _json_schema_issues(record, schema, schema, "$")


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
    tick_window: tuple[float, float] | None = None,
) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    issues: list[str] = []
    ordered_contacts = sorted(
        (row for row in contacts if _finite(row.get("sim_time_s"))),
        key=lambda row: float(row["sim_time_s"]),
    )
    ordered_ft = sorted(
        (row for row in ft_rows if _finite(row.get("sim_time_s"))),
        key=lambda row: float(row["sim_time_s"]),
    )
    last_ft_index = -1
    for contact_index, contact in enumerate(ordered_contacts):
        if not _finite(contact.get("sim_time_s")):
            continue
        stamp = float(contact["sim_time_s"])
        if tick_window is None or not (tick_window[0] <= stamp <= tick_window[1]):
            issues.append(f"contact[{contact_index}]:outside_tick_window")
            continue
        candidates = [
            (index, row)
            for index, row in enumerate(ordered_ft)
            if index > last_ft_index
            and tick_window[0] <= float(row["sim_time_s"]) <= tick_window[1]
            and abs(float(row["sim_time_s"]) - stamp) <= tolerance_s
        ]
        if not candidates:
            issues.append(f"contact[{contact_index}]:unique_ft_pair_missing")
            continue
        nearest_index, nearest = min(candidates, key=lambda item: abs(float(item[1]["sim_time_s"]) - stamp))
        last_ft_index = nearest_index
        contact_frame = str(contact.get("comparison_frame") or "")
        ft_frame = str(nearest.get("comparison_frame") or "")
        if not contact_frame or contact_frame != ft_frame:
            issues.append(f"contact[{contact_index}]:comparison_frame_missing_or_mismatch")
            continue
        if (
            contact.get("comparison_convention") != "force_on_eoat_along_reaction_normal"
            or nearest.get("comparison_convention") != "force_on_eoat_along_reaction_normal"
        ):
            issues.append(f"contact[{contact_index}]:comparison_convention_mismatch")
            continue
        contact_wrench = _vector6(contact.get("comparison_wrench_on_eoat"))
        ft_wrench = _vector6(nearest.get("comparison_wrench_on_eoat"))
        contact_normal = _unit3(contact.get("reaction_normal"))
        ft_normal = _unit3(nearest.get("reaction_normal"))
        if contact_wrench is None or ft_wrench is None:
            issues.append(f"contact[{contact_index}]:same_frame_wrench_missing")
            continue
        if contact_normal is None or ft_normal is None:
            issues.append(f"contact[{contact_index}]:reaction_normal_missing")
            continue
        normal_alignment = _dot3(contact_normal, ft_normal)
        if normal_alignment < 1.0 - 1e-6:
            issues.append(f"contact[{contact_index}]:reaction_normal_mismatch")
            continue
        delta = abs(float(nearest["sim_time_s"]) - stamp)
        contact_projection = _dot3(contact_wrench, contact_normal)
        ft_projection = _dot3(ft_wrench, contact_normal)
        if contact_projection <= 0.0 or ft_projection <= 0.0:
            issues.append(f"contact[{contact_index}]:reaction_normal_sign_mismatch")
            continue
        contact_force_norm = _force_norm(contact_wrench)
        ft_force_norm = _force_norm(ft_wrench)
        if contact_force_norm <= 0.0 or ft_force_norm <= 0.0:
            issues.append(f"contact[{contact_index}]:comparison_force_zero")
            continue
        force_direction_cosine = _dot3(contact_wrench, ft_wrench) / (contact_force_norm * ft_force_norm)
        if force_direction_cosine < 0.8:
            issues.append(f"contact[{contact_index}]:force_direction_mismatch")
            continue
        pairs.append(
            {
                "contact_sim_time_s": stamp,
                "ft_sim_time_s": float(nearest["sim_time_s"]),
                "delta_s": delta,
                "comparison_frame": contact_frame,
                "reaction_normal": contact_normal,
                "contact_normal_force_n": contact_projection,
                "ft_normal_force_n": ft_projection,
                "normal_force_ratio": ft_projection / contact_projection,
                "force_direction_cosine": force_direction_cosine,
            }
        )
    for index, row in enumerate(ordered_ft):
        stamp = float(row["sim_time_s"])
        if tick_window is None or not (tick_window[0] <= stamp <= tick_window[1]):
            issues.append(f"ft[{index}]:outside_tick_window")
    ratios = [row["normal_force_ratio"] for row in pairs]
    ratio_spread = None
    if ratios:
        mean = sum(ratios) / len(ratios)
        ratio_spread = max(abs(value - mean) for value in ratios) / max(abs(mean), 1e-12)
    if len(pairs) < 2:
        issues.append("unique_same_frame_pair_count_below_2")
    if ratio_spread is None or ratio_spread > 0.25:
        issues.append("normal_force_ratio_spread_exceeds_0p25")
    issues = _dedupe(issues)
    passed = not issues
    return {
        "pass": passed,
        "pair_count": len(pairs),
        "tolerance_s": tolerance_s,
        "basis": "unique monotonic same-tick-window pairs; common frame; force-on-EOAT convention; reaction-normal sign and vector-direction checks",
        "normal_force_ratio_relative_spread": ratio_spread,
        "pairs": pairs,
        "issues": issues,
        "blockers": [] if passed else ["native_contact_ft_correlation_not_proven"],
    }


def validate_tick_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    backend: str,
    schema_path: Path = TICK_SCHEMA_PATH,
) -> list[str]:
    issues: list[str] = []
    if not rows:
        return ["tick_rows_zero"]
    previous_sequence: int | None = None
    previous_time: float | None = None
    for index, row in enumerate(rows):
        for issue in validate_tick_json_schema(row, schema_path=schema_path):
            issues.append(f"tick[{index}]:json_schema:{issue}")
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


def validate_tf_lineage(
    payload: Mapping[str, Any],
    *,
    run_id: str,
    generated_urdf_sha256: str,
) -> list[str]:
    issues: list[str] = []
    if payload.get("run_id") != run_id:
        issues.append("tf_lineage:run_id_mismatch")
    if payload.get("schema") != "ur10e_gazebo_v2_runtime_lineage_v2":
        issues.append("tf_lineage:schema_mismatch")
    if payload.get("generated_urdf_sha256") != generated_urdf_sha256:
        issues.append("tf_lineage:generated_urdf_hash_mismatch")
    if payload.get("pose_info_parse_issues"):
        issues.append("tf_lineage:pose_info_parse_issues_present")
    expected = {
        "base_link",
        "base",
        "base_link_inertia",
        "shoulder_link",
        "upper_arm_link",
        "forearm_link",
        "wrist_1_link",
        "wrist_2_link",
        "wrist_3_link",
        "flange",
        "tool0",
        "real_aligned_eoat_visual_stack",
        ACTIVE_TCP_LINK,
    }
    declared = payload.get("required_runtime_entities")
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes, bytearray)) or set(declared) != expected:
        issues.append("tf_lineage:required_runtime_entity_set_mismatch")
    entities = payload.get("runtime_entities") if isinstance(payload.get("runtime_entities"), Mapping) else {}
    for frame in sorted(expected):
        row = entities.get(frame) if isinstance(entities, Mapping) else None
        if not isinstance(row, Mapping):
            issues.append(f"tf_lineage:missing:{frame}")
        elif row.get("present") is not True or not row.get("matches") or row.get("source") != "gazebo_pose_info":
            issues.append(f"tf_lineage:runtime_entity_not_observed:{frame}")
    if payload.get("all_required_runtime_entities_present") is not True:
        issues.append("tf_lineage:all_required_runtime_entities_not_present")
    if payload.get("static_parentage_source") != "manifest_bound_generated_urdf_not_runtime_pose_inference":
        issues.append("tf_lineage:static_parentage_provenance_mismatch")
    return issues


def _resolve_artifact_path(run_dir: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if relative.is_absolute():
        return None
    resolved = (run_dir / relative).resolve()
    try:
        resolved.relative_to(run_dir.resolve())
    except ValueError:
        return None
    return resolved


def _validate_bound_file(run_dir: Path, row: Mapping[str, Any], *, prefix: str) -> tuple[list[str], Path | None]:
    issues: list[str] = []
    path = _resolve_artifact_path(run_dir, row.get("artifact_path"))
    if path is None:
        return [f"{prefix}:artifact_path_invalid"], None
    if not path.is_file():
        return [f"{prefix}:artifact_missing"], path
    if row.get("size") != path.stat().st_size:
        issues.append(f"{prefix}:size_mismatch")
    if row.get("sha256") != sha256_file(path):
        issues.append(f"{prefix}:sha256_mismatch")
    return issues, path


def validate_model_bindings(
    run_dir: Path,
    payload: Mapping[str, Any],
    *,
    backend: str,
) -> tuple[list[str], dict[str, Any]]:
    issues: list[str] = []
    if payload.get("schema") != "ur10e_gazebo_v2_model_bindings_v1":
        issues.append("model_bindings:schema_mismatch")
    if payload.get("backend") != backend:
        issues.append("model_bindings:backend_mismatch")
    files = payload.get("files") if isinstance(payload.get("files"), Mapping) else {}
    missing = REQUIRED_BINDING_IDS - set(files)
    for source_id in sorted(missing):
        issues.append(f"model_bindings:missing:{source_id}")
    verified_rows: dict[str, Mapping[str, Any]] = {}
    for source_id, row in files.items():
        if not isinstance(row, Mapping):
            issues.append(f"model_bindings:{source_id}:row_invalid")
            continue
        if row.get("source_id") != source_id:
            issues.append(f"model_bindings:{source_id}:source_id_mismatch")
        row_issues, _ = _validate_bound_file(run_dir, row, prefix=f"model_bindings:{source_id}")
        issues.extend(row_issues)
        verified_rows[str(source_id)] = row
    generated = payload.get("generated_urdf") if isinstance(payload.get("generated_urdf"), Mapping) else {}
    if generated.get("source_id") != "generated_urdf":
        issues.append("model_bindings:generated_urdf:source_id_mismatch")
    generated_issues, generated_path = _validate_bound_file(
        run_dir,
        generated,
        prefix="model_bindings:generated_urdf",
    )
    issues.extend(generated_issues)
    if generated_path is not None and generated_path.is_file():
        try:
            description = generated_path.read_text(encoding="utf-8")
            audit = audit_robot_description(description, backend=backend)
        except (OSError, ValueError) as exc:
            audit = {"pass": False, "blockers": [f"generated_urdf_audit_exception:{type(exc).__name__}"]}
        if not audit.get("pass"):
            issues.extend(f"model_bindings:generated_urdf:{value}" for value in audit.get("blockers", []))
    else:
        audit = {"pass": False, "blockers": ["generated_urdf_missing"]}
    material_rows = dict(verified_rows)
    if generated:
        material_rows["generated_urdf"] = generated
    material = "\n".join(
        f"{source_id}:{row.get('sha256')}" for source_id, row in sorted(material_rows.items())
    )
    composite = hashlib.sha256(material.encode("utf-8")).hexdigest()
    if payload.get("composite_sha256") != composite:
        issues.append("model_bindings:composite_sha256_mismatch")
    return issues, {
        "composite_sha256": composite,
        "generated_urdf_sha256": generated.get("sha256"),
        "world_sha256": files.get("world", {}).get("sha256") if isinstance(files.get("world"), Mapping) else None,
        "eoat_visual_proxy_sha256": files.get("eoat_visual_proxy", {}).get("sha256")
        if isinstance(files.get("eoat_visual_proxy"), Mapping)
        else None,
        "calibration_sha256": files.get("calibration", {}).get("sha256")
        if isinstance(files.get("calibration"), Mapping)
        else None,
        "ur_xacro_sha256": files.get("ur_xacro", {}).get("sha256")
        if isinstance(files.get("ur_xacro"), Mapping)
        else None,
        "controller_sha256": files.get(
            "velocity_controller" if backend == "velocity" else "effort_surrogate_controller", {}
        ).get("sha256")
        if isinstance(
            files.get("velocity_controller" if backend == "velocity" else "effort_surrogate_controller"),
            Mapping,
        )
        else None,
        "code_sha256": files.get("gazebo_v2_code", {}).get("sha256")
        if isinstance(files.get("gazebo_v2_code"), Mapping)
        else None,
        "audit": audit,
    }


def validate_capture_artifacts(run_dir: Path, rows: Any) -> list[str]:
    issues: list[str] = []
    if not isinstance(rows, Mapping):
        return ["capture_artifacts:not_object"]
    required = {
        "native_contact.raw.jsonl",
        "native_ft.raw.jsonl",
        "pose_info.raw.jsonl",
        "native_contact.jsonl",
        "native_ft.jsonl",
        "tf_lineage.json",
        "tick_trace.jsonl",
    }
    for name in sorted(required - set(rows)):
        issues.append(f"capture_artifacts:missing:{name}")
    for name, row in rows.items():
        if not isinstance(name, str) or not isinstance(row, Mapping):
            issues.append("capture_artifacts:row_invalid")
            continue
        path = _resolve_artifact_path(run_dir, name)
        if path is None or not path.is_file():
            issues.append(f"capture_artifacts:{name}:missing_or_invalid_path")
            continue
        if row.get("size") != path.stat().st_size:
            issues.append(f"capture_artifacts:{name}:size_mismatch")
        if row.get("sha256") != sha256_file(path):
            issues.append(f"capture_artifacts:{name}:sha256_mismatch")
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
    idat_payloads: list[bytes] = []
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
            idat_payloads.append(chunk)
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
    if idat_payloads:
        try:
            decoded = zlib.decompress(b"".join(idat_payloads))
        except zlib.error:
            decoded = b""
            result["blockers"].append("png_idat_decompression_failed")
        if decoded:
            nonzero_fraction = sum(value != 0 for value in decoded) / len(decoded)
            result["decoded_nonzero_fraction"] = nonzero_fraction
            result["decoded_byte_diversity"] = len(set(decoded))
            if nonzero_fraction < 0.001 or len(set(decoded)) < 4:
                result["blockers"].append("png_content_near_blank")
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
    camera_manifest_sha256 = sha256_file(camera_manifest_path) if camera_manifest_path.is_file() else None
    views = camera_manifest.get("views") if isinstance(camera_manifest.get("views"), Mapping) else {}
    view_rows: dict[str, Any] = {}
    for name in REQUIRED_VIEWS:
        row = views.get(name) if isinstance(views, Mapping) else None
        path_value = row.get("path") if isinstance(row, Mapping) else None
        path = _resolve_artifact_path(run_dir, path_value)
        image = camera_png_metadata(path) if path and path.is_file() else {"valid": False, "width": None, "height": None, "blockers": ["image_missing"]}
        topic_valid = isinstance(row, Mapping) and row.get("topic") == f"/ur10e/gazebo_v2/camera/{name}"
        declared_sha = row.get("sha256") if isinstance(row, Mapping) else None
        actual_sha = sha256_file(path) if path and path.is_file() else None
        hash_valid = bool(actual_sha and declared_sha == actual_sha)
        sim_time_s = row.get("sim_time_s") if isinstance(row, Mapping) else None
        time_valid = bool(
            sim_time_window is not None
            and _finite(sim_time_s)
            and sim_time_window[0] <= float(sim_time_s) <= sim_time_window[1]
        )
        valid = bool(image["valid"] and time_valid and topic_valid and hash_valid)
        view_rows[name] = {
            "topic": f"/ur10e/gazebo_v2/camera/{name}",
            "path": str(path) if path else None,
            "exists": valid,
            "width": image["width"],
            "height": image["height"],
            "image_blockers": image["blockers"],
            "sim_time_s": sim_time_s,
            "same_tick_window": time_valid,
            "topic_bound": topic_valid,
            "hash_bound": hash_valid,
            "sha256": actual_sha if valid else None,
        }
        if not valid:
            blockers.append(f"observer_view_missing:{name}")
    if checks.get("run_id") != run_id:
        blockers.append("observer_manual_checks_run_id_mismatch")
    if not str(checks.get("reviewer") or ""):
        blockers.append("observer_reviewer_missing")
    if not str(checks.get("reviewed_at") or ""):
        blockers.append("observer_reviewed_at_missing")
    if checks.get("review_lane") != "independent_observer":
        blockers.append("observer_review_lane_not_independent")
    if checks.get("camera_manifest_sha256") != camera_manifest_sha256:
        blockers.append("observer_camera_manifest_hash_mismatch")
    declared_view_hashes = checks.get("view_sha256") if isinstance(checks.get("view_sha256"), Mapping) else {}
    for name, row in view_rows.items():
        if declared_view_hashes.get(name) != row.get("sha256") or row.get("sha256") is None:
            blockers.append(f"observer_view_hash_mismatch:{name}")
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
        "review_lane": checks.get("review_lane"),
        "camera_manifest_sha256": camera_manifest_sha256,
        "manual_checks": {name: manual.get(name) is True for name in REQUIRED_MANUAL_CHECKS},
        "native_contact_ft_same_run_pass": native_same_run_pass,
        "tf_lineage_pass": tf_lineage_pass,
        "blockers": blockers,
        "forbidden_claims": [
            "Gazebo production-runtime acceptance",
            "current bench geometry equivalence",
            "real bench/live contact",
            "live acceptance",
            "reproduction completion",
        ],
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
    runtime_manifest_path = run_dir / "runtime_manifest.json"
    runtime_manifest = load_json(runtime_manifest_path) if runtime_manifest_path.is_file() else {}

    blockers: list[str] = []
    if manifest.get("schema") != "ur10e_gazebo_v2_capture_manifest_v1":
        blockers.append("capture_manifest_schema_mismatch")
    if not run_id:
        blockers.append("capture_manifest_run_id_missing")
    if manifest.get("engine_family") != "Gazebo Fortress":
        blockers.append("capture_manifest_engine_not_fortress")
    if manifest.get("engine_major") != 6:
        blockers.append("capture_manifest_engine_major_not_6")
    if manifest.get("world_name") != "ur10e_gazebo_v2_fortress" or manifest.get("world_pose_topic_present") is not True:
        blockers.append("capture_manifest_runtime_world_identity_not_proven")
    if manifest.get("ros2_control_plugin") != "libign_ros2_control-system.so":
        blockers.append("capture_manifest_ros2_control_plugin_mismatch")
    if manifest.get("backend_fidelity") != backend_row.fidelity:
        blockers.append("capture_manifest_backend_fidelity_mismatch")
    abi = manifest.get("abi_preflight") if isinstance(manifest.get("abi_preflight"), Mapping) else {}
    version = abi.get("version") if isinstance(abi.get("version"), Sequence) else None
    plugin_paths = abi.get("plugin_paths") if isinstance(abi.get("plugin_paths"), Sequence) else ()
    plugin_names = {Path(str(path)).name for path in plugin_paths}
    if (
        abi.get("pass") is not True
        or not version
        or version[0] != 6
        or "libign_ros2_control-system.so" not in plugin_names
        or "libgz_ros2_control-system.so" in plugin_names
        or not str(abi.get("spawn_package_prefix") or "")
    ):
        blockers.append("capture_manifest_abi_preflight_not_passed")
    controller = manifest.get("controller_inventory") if isinstance(manifest.get("controller_inventory"), Mapping) else {}
    if (
        controller.get("pass") is not True
        or controller.get("selected") != backend_row.controller_name
        or controller.get("selected_active") is not True
        or controller.get("joint_state_broadcaster_active") is not True
        or controller.get("other_active") is not False
    ):
        blockers.append("capture_manifest_controller_inventory_not_passed")
    if manifest.get("simultaneous_backend_active") is not False:
        blockers.append("backend_exclusivity_not_proven")
    normalization = manifest.get("normalization_issues") if isinstance(manifest.get("normalization_issues"), Mapping) else {}
    if normalization.get("contact") != [] or normalization.get("ft") != []:
        blockers.append("capture_manifest_normalization_issues_present")
    model_payload = manifest.get("model_bindings") if isinstance(manifest.get("model_bindings"), Mapping) else {}
    model_issues, model_hashes = validate_model_bindings(run_dir, model_payload, backend=backend)
    blockers.extend(model_issues)
    blockers.extend(validate_capture_artifacts(run_dir, manifest.get("artifacts")))
    blockers.extend(contact_parse_issues + ft_parse_issues + tick_parse_issues)
    blockers.extend(validate_contact_rows(contacts, run_id=run_id))
    blockers.extend(validate_ft_rows(ft_rows, run_id=run_id))
    model_files = model_payload.get("files") if isinstance(model_payload.get("files"), Mapping) else {}
    tick_schema_row = model_files.get("tick_schema") if isinstance(model_files.get("tick_schema"), Mapping) else {}
    bound_tick_schema_path = _resolve_artifact_path(run_dir, tick_schema_row.get("artifact_path"))
    tick_schema_path = bound_tick_schema_path if bound_tick_schema_path and bound_tick_schema_path.is_file() else TICK_SCHEMA_PATH
    tick_issues = validate_tick_rows(ticks, run_id=run_id, backend=backend, schema_path=tick_schema_path)
    blockers.extend(tick_issues)
    generated_urdf_sha256 = str(model_hashes.get("generated_urdf_sha256") or "")
    tf_issues = validate_tf_lineage(
        tf_payload,
        run_id=run_id,
        generated_urdf_sha256=generated_urdf_sha256,
    )
    blockers.extend(tf_issues)
    finite_tick_times = [float(row["sim_time_s"]) for row in ticks if _finite(row.get("sim_time_s"))]
    sim_time_window = (min(finite_tick_times), max(finite_tick_times)) if finite_tick_times else None
    correlation = contact_ft_correlation(contacts, ft_rows, tick_window=sim_time_window)
    blockers.extend(correlation["blockers"])
    blockers = _dedupe(blockers)
    native_same_run_pass = not blockers
    raw_runtime_blockers = manifest.get("runtime_blockers")
    runtime_blockers = (
        [str(value) for value in raw_runtime_blockers if str(value)]
        if isinstance(raw_runtime_blockers, Sequence) and not isinstance(raw_runtime_blockers, (str, bytes, bytearray))
        else ["capture_manifest_runtime_blockers_invalid"]
    )
    external_runtime = manifest.get("external_runtime_artifacts") is True
    runtime_issues: list[str]
    if external_runtime:
        runtime_issues = validate_runtime_manifest(run_dir, runtime_manifest, run_id=run_id, backend=backend)
        runtime_blockers.extend(runtime_issues)
        if manifest.get("runtime_manifest_path") != "runtime_manifest.json":
            runtime_blockers.append("capture_manifest_runtime_path_mismatch")
        if manifest.get("gazebo_runtime_pass") is not True:
            runtime_blockers.append("capture_manifest_runtime_candidate_not_passed")
    else:
        runtime_issues = ["runtime_manifest:not_external_coordinated_runtime"]
        for required in sorted(RUNTIME_IMPLEMENTATION_BLOCKERS):
            if required not in runtime_blockers:
                runtime_blockers.append(f"required_runtime_blocker_not_declared:{required}")
        if manifest.get("gazebo_runtime_pass") is not False:
            runtime_blockers.append("capture_manifest_legacy_runtime_pass_must_be_false")
    runtime_blockers = _dedupe(runtime_blockers)
    gazebo_runtime_pass = bool(external_runtime and not runtime_blockers and native_same_run_pass)

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
            "version": abi.get("version"),
            "ros2_control_plugin": manifest.get("ros2_control_plugin"),
            "ros2_control_plugin_paths": abi.get("plugin_paths"),
            "ros_gz_sim_prefix": abi.get("spawn_package_prefix"),
        },
        "model_bindings": model_hashes,
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
        "tick_schema_pass": not tick_issues,
        "observer_review_path": str(observer_path),
        "observer_review_pass": observer["viewer_level_pass"],
        "runtime_manifest_path": str(runtime_manifest_path),
        "runtime_manifest_schema": runtime_manifest.get("schema"),
        "runtime_manifest_pass": not runtime_issues and runtime_manifest.get("schema") == RUNTIME_SCHEMA,
        "artifacts": artifacts,
        "blockers": blockers,
        "runtime_blockers": runtime_blockers,
        "gazebo_runtime_pass": gazebo_runtime_pass,
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
            "gazebo_production_runtime_acceptance": False,
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
    if args.strict and not (
        evidence["native_contact_ft_same_run_pass"]
        and observer["viewer_level_pass"]
        and evidence["gazebo_runtime_pass"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
