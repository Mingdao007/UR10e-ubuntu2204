#!/usr/bin/env python3
"""Build a fail-closed RViz debug evidence pack for P3.

The pack is offline-only. It records a source-backed RViz config and manifest
for required debug surfaces, but it does not claim an observed RViz render or
any stronger force/contact tier.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PACKAGE_ROOT = WORKSPACE / "src" / "ur10e_example_controllers"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from ur10e_example_controllers import canonical_wrench_contract as wrench_contract  # noqa: E402
from ur10e_example_controllers import step56_simulation_matrix as step56  # noqa: E402


SCHEMA = "ur10e_p3_rviz_debug_evidence_pack_v1"
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"
RVIZ_CONFIG_NAME = "ur10e_p3_debug.rviz"
MANIFEST_NAME = "rviz_debug_manifest.json"

REQUIRED_ITEMS = [
    "TF tree",
    "robot model",
    "EOAT/tool frames",
    "TCP/contact_tip/contact_surface frames",
    "wrench/contact vectors or markers",
    "trajectory/path markers",
    "frame and claim-tier labels",
]

RVIZ_MARKER_TOPICS = {
    "frame_markers": "/ur10e/rviz/contact_frame_markers",
    "wrench_vector": "/ur10e/rviz/wrench_vector",
    "contact_state_markers": "/ur10e/rviz/contact_state_markers",
    "trajectory_path": "/ur10e/rviz/trajectory_path",
    "claim_tier_labels": "/ur10e/rviz/claim_tier_labels",
}


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKSPACE.resolve()))
    except ValueError:
        return str(path)


def rviz_config_text() -> str:
    fixed_frame = "base"
    marker_array_class = "rviz_default_plugins/MarkerArray"
    return f"""# Evidence topic: /tf
# Evidence topic: /tf_static
# Evidence topic: /joint_states
# Evidence topic: /ur10e/contact/canonical_wrench
# Evidence topic: /ur10e/contact/simulated_ft/wrench
# Evidence topic: /ur10e/contact/simulated_ft/status
# Evidence topic: /ur10e/contact/contact_state
Panels:
  - Class: rviz_common/Displays
    Name: Displays
  - Class: rviz_common/Selection
    Name: Selection
Visualization Manager:
  Class: ""
  Displays:
    - Class: rviz_default_plugins/TF
      Enabled: true
      Name: TF tree
      Frames:
        world:
          Value: true
        base:
          Value: true
        base_link:
          Value: true
        tool0:
          Value: true
        flange:
          Value: true
        ft_sensor:
          Value: true
        tcp:
          Value: true
        contact_tip:
          Value: true
        contact_surface:
          Value: true
        surface_normal:
          Value: true
    - Class: rviz_default_plugins/RobotModel
      Description Topic:
        Value: /robot_description
      Enabled: true
      Name: Robot model
      TF Prefix: ""
    - Class: {marker_array_class}
      Enabled: true
      Name: EOAT tool frame markers
      Topic:
        Value: {RVIZ_MARKER_TOPICS["frame_markers"]}
    - Class: {marker_array_class}
      Enabled: true
      Name: Wrench and contact vector markers
      Topic:
        Value: {RVIZ_MARKER_TOPICS["wrench_vector"]}
    - Class: {marker_array_class}
      Enabled: true
      Name: Contact state markers
      Topic:
        Value: {RVIZ_MARKER_TOPICS["contact_state_markers"]}
    - Class: rviz_default_plugins/Path
      Enabled: true
      Name: Step trajectory path
      Topic:
        Value: {RVIZ_MARKER_TOPICS["trajectory_path"]}
    - Class: {marker_array_class}
      Enabled: true
      Name: Claim-tier and frame labels
      Topic:
        Value: {RVIZ_MARKER_TOPICS["claim_tier_labels"]}
  Enabled: true
  Global Options:
    Fixed Frame: {fixed_frame}
  Name: root
  Tools:
    - Class: rviz_default_plugins/Interact
    - Class: rviz_default_plugins/MoveCamera
  Value: true
"""


def build_manifest(
    *,
    generated_at: str | None = None,
    rviz_config_name: str = RVIZ_CONFIG_NAME,
    rviz_config_sha256: str | None = None,
) -> dict[str, Any]:
    contract = wrench_contract.canonical_contract_spec()
    frames = list(contract["required_frames"])
    topics = list(
        dict.fromkeys(
            [
                *contract["required_input_topics"],
                "/robot_description",
                contract["canonical_wrench_topic"],
                contract["simulated_ft_wrench_topic"],
                contract["simulated_ft_status_topic"],
                contract["contact_state_topic"],
                contract["controller_status_topic"],
                *RVIZ_MARKER_TOPICS.values(),
            ]
        )
    )
    source_paths = {
        "canonical_wrench_contract_module": rel(Path(wrench_contract.__file__)),
        "step56_simulation_matrix": rel(Path(step56.__file__)),
        "calibrated_urdf": rel(step56.CALIBRATED_URDF),
        "world": rel(step56.WORLD_PATH),
        "launch": rel(step56.LAUNCH_PATH),
    }
    evidenced_items = {
        "TF tree": {
            "evidenced": True,
            "evidence_type": "rviz_config_tf_display_plus_contract_frames",
            "topics": ["/tf", "/tf_static"],
            "frames": frames,
        },
        "robot model": {
            "evidenced": True,
            "evidence_type": "rviz_robot_model_display_plus_urdf_source",
            "topics": ["/robot_description"],
            "source_paths": [source_paths["calibrated_urdf"]],
        },
        "EOAT/tool frames": {
            "evidenced": True,
            "evidence_type": "rviz_tf_and_marker_config",
            "topics": [RVIZ_MARKER_TOPICS["frame_markers"]],
            "frames": ["tool0", "flange", "ft_sensor"],
        },
        "TCP/contact_tip/contact_surface frames": {
            "evidenced": True,
            "evidence_type": "rviz_tf_and_marker_config",
            "topics": [RVIZ_MARKER_TOPICS["frame_markers"]],
            "frames": ["tcp", "contact_tip", "contact_surface", "surface_normal"],
        },
        "wrench/contact vectors or markers": {
            "evidenced": True,
            "evidence_type": "rviz_marker_config_plus_canonical_wrench_topics",
            "topics": [
                contract["canonical_wrench_topic"],
                contract["simulated_ft_wrench_topic"],
                contract["contact_state_topic"],
                RVIZ_MARKER_TOPICS["wrench_vector"],
                RVIZ_MARKER_TOPICS["contact_state_markers"],
            ],
        },
        "trajectory/path markers": {
            "evidenced": True,
            "evidence_type": "rviz_path_display_plus_step56_path_source",
            "topics": [RVIZ_MARKER_TOPICS["trajectory_path"]],
            "source_paths": [source_paths["step56_simulation_matrix"]],
        },
        "frame and claim-tier labels": {
            "evidenced": True,
            "evidence_type": "rviz_text_marker_config_plus_manifest_claim_boundary",
            "topics": [RVIZ_MARKER_TOPICS["claim_tier_labels"]],
            "claim_tier": "visual_only",
        },
    }
    return {
        "schema": SCHEMA,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_no_live_rviz_debug_evidence_pack",
        "claim_tier": "visual_only",
        "evidence_mode": "rviz_config_manifest_only_not_rendered",
        "rviz_config_path": rviz_config_name,
        "rviz_config_sha256": rviz_config_sha256,
        "required_items": REQUIRED_ITEMS,
        "evidenced_items": evidenced_items,
        "all_required_items_evidenced": not missing_required_items({"evidenced_items": evidenced_items}),
        "rendered_screenshot_evidence": {
            "present": False,
            "status": "not_rendered_no_screenshot",
            "claim_tier": "visual_only",
        },
        "full_rviz_render_acceptance_allowed": False,
        "frames": frames,
        "topics": topics,
        "marker_topics": RVIZ_MARKER_TOPICS,
        "source_paths": source_paths,
        "wrench_frame_policy": contract["consumer_interface"]["wrench_frame_policy"],
        "force_frame_semantics": contract["force_frame_semantics"],
        "forbidden_claim": "physical Gazebo collision/contact physics; simulated_ft; real bench/live contact",
    }


def missing_required_items(payload: dict[str, Any]) -> list[str]:
    evidenced = payload.get("evidenced_items", {})
    missing: list[str] = []
    for item in REQUIRED_ITEMS:
        row = evidenced.get(item, {})
        if not isinstance(row, dict) or not bool(row.get("evidenced")):
            missing.append(item)
    return missing


def write_pack(output_dir: Path, *, generated_at: str | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    rviz_path = output_dir / RVIZ_CONFIG_NAME
    config = rviz_config_text()
    rviz_path.write_text(config, encoding="utf-8")
    sha256 = hashlib.sha256(config.encode("utf-8")).hexdigest()
    manifest = build_manifest(
        generated_at=generated_at,
        rviz_config_name=RVIZ_CONFIG_NAME,
        rviz_config_sha256=sha256,
    )
    manifest_path = output_dir / MANIFEST_NAME
    manifest["artifact_path"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_pack(args.output_dir, generated_at=args.generated_at)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
