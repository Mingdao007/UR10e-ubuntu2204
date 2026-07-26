#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
SRC_PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(SRC_PACKAGE) not in sys.path:
    sys.path.insert(0, str(SRC_PACKAGE))

from ur10e_example_controllers import ur10e_gazebo_matrix_runner as gazebo  # noqa: E402


EXPERIMENT = WORKSPACE / "experiments" / "tase-contact-reproduction"
EOAT_ARCHIVE = (
    WORKSPACE
    / "experiments"
    / "archive"
    / "onrobot"
    / "onrobot_hex_e_v2_3010007655"
    / "eoat_design"
    / "eoat_print_archive"
    / "v13_ksm8n_receiver_5p3mm_side_window_85mm"
)
V13_VERIFICATION = EOAT_ARCHIVE / "verification.json"
WORLD_PATH = WORKSPACE / "src" / "ur10e_example_controllers" / "worlds" / "step5_table_world.sdf"

VISUAL_ROLE_BY_NAME = {
    gazebo.EOAT_REAL_MESH_VISUAL_NAME: "installed_ksm8n_ball_transfer_tool_mesh",
}

CONTACT_SURFACE_IDS = (
    "step5_contact_surface",
    "step6_contact_surface",
    "step7_large_platform_contact_surface",
    "step8_large_platform_contact_surface",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_xyz(text: str | None) -> list[float]:
    return [float(value) for value in (text or "0 0 0").split()]


def _geometry_payload(geometry: ET.Element | None) -> dict[str, Any]:
    if geometry is None or not list(geometry):
        return {"kind": None}
    child = list(geometry)[0]
    payload: dict[str, Any] = {"kind": child.tag}
    payload.update({key: _coerce_value(value) for key, value in child.attrib.items()})
    for nested in child:
        if nested.text and nested.text.strip():
            payload[nested.tag] = _coerce_value(nested.text)
    return payload


def _coerce_value(value: str) -> float | str | list[float | str]:
    values = value.split()
    if len(values) > 1:
        return [_coerce_number(item) for item in values]
    return _coerce_number(value)


def _coerce_number(value: str) -> float | str:
    try:
        return float(value)
    except ValueError:
        return value


def _visual_proxy_parts(robot_description: str, audit: dict[str, Any]) -> list[dict[str, Any]]:
    root = ET.fromstring(robot_description)
    eoat = root.find(f"./link[@name='{gazebo.EOAT_VISUAL_LINK}']")
    if eoat is None:
        return []

    parts: list[dict[str, Any]] = []
    for visual in eoat.findall("visual"):
        name = visual.attrib.get("name", "")
        origin = visual.find("origin")
        geometry = _geometry_payload(visual.find("geometry"))
        mesh_installed = (
            name == gazebo.EOAT_REAL_MESH_VISUAL_NAME
            and geometry.get("filename") == gazebo.EOAT_REAL_MESH_URI
            and geometry.get("scale") == [0.001, 0.001, 0.001]
        )
        parts.append(
            {
                "id": name,
                "role": VISUAL_ROLE_BY_NAME.get(name, "viewer_affordance"),
                "source_type": "local_stl_mesh_installed_in_urdf" if mesh_installed else "generated_urdf_visual_proxy",
                "source_path": "ur10e_example_controllers.ur10e_gazebo_matrix_runner:add_real_aligned_eoat_visual_stack",
                "source_asset": gazebo.EOAT_REAL_MESH_SOURCE_ASSET if mesh_installed else None,
                "unit": "m",
                "scale_to_m": 0.001 if mesh_installed else 1.0,
                "axis_convention": "URDF link-local xyz/rpy; mesh source dimensions audited as mm and scaled to m",
                "transform": {
                    "parent_frame": "tool0",
                    "child_frame": gazebo.EOAT_VISUAL_LINK,
                    "xyz_m": _parse_xyz(origin.attrib.get("xyz") if origin is not None else None),
                    "rpy_rad": _parse_xyz(origin.attrib.get("rpy") if origin is not None else None),
                    "status": "installed_mesh_visual_transform_only",
                },
                "geometry": geometry,
                "raw_bbox_mm": gazebo.EOAT_REAL_MESH_RAW_BBOX_MM if mesh_installed else None,
                "scaled_bbox_m": gazebo.EOAT_REAL_MESH_SCALED_BBOX_M if mesh_installed else None,
                "approximation_status": (
                    "actual_local_stl_primary_visual_with_simplified_collision_primitives"
                    if mesh_installed
                    else "legacy_visual_proxy_not_acceptance_evidence"
                ),
                "claim_tier": "visual_only",
                "collision_body_instantiated": False,
                "collision_name": None,
                "physics_relevant_candidate": False,
                "known_limit": (
                    "Installed visual mesh only; simplified EOAT collision primitives are tracked separately and do not prove contact physics."
                    if mesh_installed
                    else "Legacy viewer affordance only; not accepted as current observer visual foundation."
                ),
            }
        )

    parts.append(
        {
            "id": "current_eoat_visual_stack_summary",
            "role": "installed_eoat_mesh_visual_summary",
            "source_type": "local_stl_mesh_installed_in_urdf",
            "source_path": "ur10e_example_controllers.ur10e_gazebo_matrix_runner:build_model_composition_audit",
            "unit": "m",
            "scale_to_m": 0.001,
            "axis_convention": "URDF tool0 child fixed joint",
            "transform": audit.get("eoat_joint_origin"),
            "geometry": {
                "kind": "installed_mesh_visual",
                "visual_count": audit.get("eoat_visual_count"),
                "mesh_uri": audit.get("eoat_primary_visual_mesh_uri"),
            },
            "approximation_status": audit.get("eoat_visual_proxy_policy"),
            "claim_tier": "visual_only",
            "collision_body_instantiated": bool(audit.get("eoat_collision_count")),
            "collision_count": audit.get("eoat_collision_count"),
            "collision_names": audit.get("present_eoat_collisions"),
            "physics_relevant_candidate": False,
            "known_limit": "Summary row; installed visual mesh plus simplified collision bodies do not prove Gazebo contact physics.",
        }
    )
    return parts


def _source_files(stem: str) -> dict[str, str]:
    return {
        "step": str(EOAT_ARCHIVE / f"{stem}.step"),
        "stl": str(EOAT_ARCHIVE / f"{stem}.stl"),
    }


def _cad_archive_parts() -> list[dict[str, Any]]:
    verification = _load_json(V13_VERIFICATION)
    version = verification["version"]
    prefix = "ur5e_ksm8n_ball_transfer_tool_v13"
    outputs = (
        ("v13_ksm8n_receiver_body", "printed_adapter_receiver_body", f"{prefix}_body"),
        ("v13_ksm8n_ksm_placeholder", "purchased_ksm8n_placeholder", f"{prefix}_ksm_placeholder"),
        ("v13_ksm8n_receiver_assembly", "printable_body_assembly_preview", f"{prefix}_assembly"),
        ("v13_ksm8n_receiver_fitcheck", "receiver_fitcheck_coupon", f"{prefix}_receiver_fitcheck"),
        ("v13_ksm8n_flange_fitcheck", "flange_fitcheck_coupon", f"{prefix}_flange_fitcheck"),
    )
    parts: list[dict[str, Any]] = []
    for part_id, role, stem in outputs:
        parts.append(
            {
                "id": part_id,
                "role": role,
                "source_type": "local_cad_archive",
                "source_path": str(EOAT_ARCHIVE),
                "source_files": _source_files(stem),
                "version": version,
                "unit": "mm",
                "scale_to_m": 0.001,
                "axis_convention": "CAD +Z from flange face toward KSM-8N contact point",
                "transform": {
                    "parent_frame": "tool0",
                    "child_frame": part_id,
                    "status": "not_installed_in_gazebo_urdf_pending_ur10e_transform_audit",
                },
                "geometry": {
                    "kind": "cad_mesh_candidate",
                    "concept": verification.get("concept"),
                },
                "bounding_box_mm": verification.get("bounding_boxes", {}),
                "contact_point_from_flange_face_mm": verification.get("parameters", {}).get(
                    "contact_point_from_flange_face_mm"
                ),
                "approximation_status": "local CAD candidate not yet installed into Gazebo URDF",
                "claim_tier": "visual_only",
                "collision_body_instantiated": False,
                "physics_relevant_candidate": part_id
                in {"v13_ksm8n_receiver_body", "v13_ksm8n_ksm_placeholder", "v13_ksm8n_receiver_assembly"},
                "known_limit": "UR5e-design archive candidate; UR10e/Gazebo collision transform and inertial data are not proven.",
            }
        )
    return parts


def _surface_material(collision: ET.Element) -> dict[str, Any]:
    return {
        "contact": {
            "kp": float(collision.findtext("./surface/contact/ode/kp") or 0.0),
            "kd": float(collision.findtext("./surface/contact/ode/kd") or 0.0),
        },
        "friction": {
            "mu": float(collision.findtext("./surface/friction/ode/mu") or 0.0),
            "mu2": float(collision.findtext("./surface/friction/ode/mu2") or 0.0),
        },
    }


def _contact_surface_candidates() -> list[dict[str, Any]]:
    root = ET.parse(WORLD_PATH).getroot()
    surfaces: list[dict[str, Any]] = []
    for surface_id in CONTACT_SURFACE_IDS:
        model = root.find(f"./world/model[@name='{surface_id}']")
        if model is None:
            continue
        pose = _parse_xyz(model.findtext("pose"))
        link = model.find("./link")
        collision = model.find("./link/collision")
        visual = model.find(f"./link/visual[@name='{gazebo.CONTACT_SURFACE_REAL_MESH_VISUAL_NAME}']")
        mesh = visual.find("./geometry/mesh") if visual is not None else None
        geometry = _geometry_payload(collision.find("geometry") if collision is not None else None)
        size_m = geometry.get("size", [0.0, 0.0, 0.0])
        if not isinstance(size_m, list):
            size_m = [0.0, 0.0, 0.0]
        surfaces.append(
            {
                "id": surface_id,
                "role": "contact_surface",
                "source_type": "sdf_world_contact_surface",
                "source_path": str(WORLD_PATH),
                "static": (model.findtext("static") or "").strip().lower() == "true",
                "unit": "m",
                "scale_to_m": 1.0,
                "axis_convention": "Gazebo world XYZ; surface normal +Z, reaction_normal +Z, approach_normal -Z",
                "pose_m_rpy": pose,
                "top_z_m": pose[2] + 0.5 * float(size_m[2]),
                "link_name": link.attrib.get("name") if link is not None else None,
                "collision": {
                    "name": collision.attrib.get("name") if collision is not None else None,
                    "geometry": {"kind": geometry.get("kind"), "size_m": size_m},
                },
                "visual_mesh": {
                    "name": visual.attrib.get("name") if visual is not None else None,
                    "uri": mesh.findtext("uri") if mesh is not None else None,
                    "scale": mesh.findtext("scale") if mesh is not None else None,
                    "pose_xyz_rpy": visual.findtext("pose") if visual is not None else None,
                    "source_asset": gazebo.CONTACT_SURFACE_REAL_MESH_SOURCE_ASSET,
                    "raw_bbox_mm": gazebo.CONTACT_SURFACE_REAL_MESH_RAW_BBOX_MM,
                    "oriented_bbox_m": gazebo.CONTACT_SURFACE_REAL_MESH_ORIENTED_BBOX_M,
                    "actual_mesh_visual_present": bool(
                        mesh is not None
                        and mesh.findtext("uri") == gazebo.CONTACT_SURFACE_REAL_MESH_URI
                        and mesh.findtext("scale") == "0.001 0.001 0.001"
                    ),
                },
                "material": _surface_material(collision) if collision is not None else {},
                "claim_tier": "visual_only",
                "collision_body_instantiated": collision is not None,
                "physics_relevant_candidate": collision is not None,
                "known_limit": "Surface collision exists, but no EOAT collision/contact pair log or wrench/contact correlation exists yet.",
            }
        )
    return surfaces


def _collision_candidates(surfaces: list[dict[str, Any]], audit: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        {
            "id": "current_eoat_simplified_collision_stack",
            "role": "intended_future_eoat_contact_body",
            "source_type": "generated_urdf_simplified_collision_primitives",
            "source_path": "ur10e_example_controllers.ur10e_gazebo_matrix_runner",
            "claim_tier": "visual_only",
            "collision_body_instantiated": bool(audit.get("eoat_collision_count")),
            "collision_count": int(audit.get("eoat_collision_count") or 0),
            "collision_names": audit.get("present_eoat_collisions"),
            "contact_collision_names": audit.get("present_eoat_contact_collisions"),
            "physics_relevant_candidate": True,
            "status": (
                "eoat_collision_body_present_contact_pair_unproven"
                if audit.get("eoat_collision_count")
                else "blocked_currently_visual_only_no_collision"
            ),
            "future_required_log": "EOAT contact body name, timestamp, contact position, normal, and count.",
        }
    ]
    for surface in surfaces:
        candidates.append(
            {
                "id": f"{surface['id']}_collision",
                "role": "intended_future_surface_contact_body",
                "source_type": "sdf_world_contact_surface",
                "source_path": surface["source_path"],
                "claim_tier": "visual_only",
                "collision_body_instantiated": surface["collision_body_instantiated"],
                "collision_name": surface["collision"]["name"],
                "physics_relevant_candidate": True,
                "status": "surface_collision_present_but_contact_pair_unproven",
                "future_required_log": "Surface contact pair evidence correlated with EOAT collision and canonical wrench/contact state.",
            }
        )
    return candidates


def _inertial_provenance(root: ET.Element) -> list[dict[str, Any]]:
    eoat = root.find(f"./link[@name='{gazebo.EOAT_VISUAL_LINK}']")
    inertial = eoat.find("inertial") if eoat is not None else None
    current = {
        "id": "current_eoat_mesh_visual_link_inertial",
        "source_type": "local_stl_mesh_visual_link_with_placeholder_inertial",
        "source_path": "ur10e_example_controllers.ur10e_gazebo_matrix_runner:_append_visual_proxy_inertial",
        "status": "approximate_visual_link_inertial_not_physics_acceptance",
        "mass_kg": float(inertial.find("mass").attrib["value"]) if inertial is not None and inertial.find("mass") is not None else None,
        "cog_xyz_m": _parse_xyz(inertial.find("origin").attrib.get("xyz")) if inertial is not None and inertial.find("origin") is not None else None,
        "inertia_kg_m2": inertial.find("inertia").attrib if inertial is not None and inertial.find("inertia") is not None else None,
        "provenance": "hard-coded placeholder for visual mesh link; not measured EOAT mass/COG/inertia",
        "claim_tier": "visual_only",
    }
    return [
        current,
        {
            "id": "v13_cad_mass_cog_inertia",
            "source_type": "local_cad_archive",
            "source_path": str(V13_VERIFICATION),
            "status": "missing_material_density_mass_cog_inertia",
            "mass_kg": None,
            "cog_xyz_m": None,
            "inertia_kg_m2": None,
            "provenance": "CAD bounding boxes exist; material, purchased KSM-8N mass, fasteners, and assembled inertia are not proven.",
            "claim_tier": "visual_only",
        },
        {
            "id": "contact_surfaces_static_world_inertial",
            "source_type": "sdf_world_contact_surface",
            "source_path": str(WORLD_PATH),
            "status": "static_surface_models_no_dynamic_inertia_claim",
            "mass_kg": None,
            "cog_xyz_m": None,
            "inertia_kg_m2": None,
            "provenance": "SDF surfaces are static collision boxes with material parameters; no dynamic inertial claim needed for this inventory.",
            "claim_tier": "visual_only",
        },
    ]


def build_inventory(*, generated_at: str | None = None) -> dict[str, Any]:
    robot_description = gazebo.generate_sim_robot_description()
    root = ET.fromstring(robot_description)
    model_audit = gazebo.build_model_composition_audit(robot_description)
    surfaces = _contact_surface_candidates()
    eoat_collision_count = int(model_audit.get("eoat_collision_count") or 0)
    force_contact_physics_proven = bool(model_audit.get("force_contact_physics_proven"))
    known_blockers = [
        "force_contact_physics_proven=false",
        "no_eoat_contact_pair_log_evidence",
        "no_wrench_contact_correlation",
        "eoat_collision_primitives_simplified_no_contact_pair_log",
        "full_force_sensor_tool_stack_mesh_incomplete",
        "mass_cog_inertia_exact_provenance_missing",
    ]
    if eoat_collision_count == 0:
        known_blockers.insert(0, "eoat_collision_count=0")
    return {
        "schema": "ur10e_gazebo_p2_eoat_collision_inventory_v1",
        "generated_at": generated_at or _now_iso(),
        "goal_lineage": "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md",
        "mode": "offline_gazebo_rviz_no_live_inventory",
        "claim_tier": "visual_only",
        "evidence_scope": {
            "scope": "collision_inventory_only",
            "collision_inventory_evaluated": True,
            "runtime_contact_pair_log_evidence_evaluated": False,
            "wrench_contact_correlation_evaluated": False,
            "contact_pair_log_evidence_authority": "build_p2_contact_correlation_audit.py",
            "force_contact_physics_authority": "build_p2_contact_correlation_audit.py",
            "scope_note": (
                "This artifact proves intended EOAT/surface collision bodies and mesh/source provenance only; "
                "it does not consume runtime Gazebo contact logs or wrench traces."
            ),
        },
        "allowed_claim": "visual_only EOAT/CAD/collision/contact-surface inventory and provenance only",
        "forbidden_claim": (
            "physical Gazebo collision/contact physics, simulated_ft from this P2 artifact alone, "
            "or real bench/live contact"
        ),
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
        "current_eoat_collision_count": eoat_collision_count,
        "force_contact_source": model_audit.get("force_contact_source"),
        "force_contact_physics_proven": force_contact_physics_proven,
        "claim_boundary": {
            "visual_only": "Supported by generated URDF/SDF/model/archive inventory only.",
            "virtual/software force-loop": "Not upgraded by this artifact.",
            "simulated_ft": "P1 evidence is separate; this P2 inventory does not add simulated_ft evidence.",
            "physical Gazebo collision/contact physics": "blocked/not proven until EOAT collision body, contact pair logs, and wrench/contact correlation exist.",
            "real bench/live contact": "not authorized.",
        },
        "eoat_parts": _visual_proxy_parts(robot_description, model_audit) + _cad_archive_parts(),
        "collision_candidates": _collision_candidates(surfaces, model_audit),
        "inertial_provenance": _inertial_provenance(root),
        "contact_surface_candidates": surfaces,
        "physical_gazebo_contact_gate": {
            "gate_scope": "inventory_only_no_runtime_contact_log",
            "eoat_collision_count": eoat_collision_count,
            "eoat_collision_body_audit_passed": eoat_collision_count > 0,
            "collision_count_proven_scope": "model_inventory_only_not_runtime_contact",
            "collision_count_proven": False,
            "contact_pair_log_evidence_scope": "not_evaluated_by_inventory",
            "contact_pair_log_evidence": False,
            "wrench_contact_correlation_scope": "not_evaluated_by_inventory",
            "wrench_contact_correlation": False,
            "force_contact_physics_proven": force_contact_physics_proven,
            "status": "blocked_not_proven",
        },
        "intended_future_contact_pair": {
            "eoat_body": "future_eoat_contact_pad_or_v13_ksm_ball_collision",
            "surface_body": "step5_contact_surface::surface::collision",
            "required_before_physical_claim": [
                "EOAT collision body instantiated",
                "Gazebo contact pair/log evidence with timestamps, names, positions, normals, and counts",
                "canonical wrench/contact-state correlation evidence",
            ],
        },
        "known_blockers": known_blockers,
    }


def write_inventory(output_dir: Path, *, generated_at: str | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "p2_eoat_collision_inventory.json"
    payload = build_inventory(generated_at=generated_at)
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build fail-closed P2 EOAT/collision inventory artifact.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args(argv)
    path = write_inventory(args.output_dir, generated_at=args.generated_at)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
