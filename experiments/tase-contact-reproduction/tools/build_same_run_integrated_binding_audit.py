#!/usr/bin/env python3
"""Build a fail-closed same-run integrated binding audit for P6.

This offline gate checks whether retained P6 evidence is actually from one
coherent run. It reads JSON manifests only; it does not launch Gazebo, RViz,
ROS, or any live bench surface.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
RUNS = EXPERIMENT_ROOT / "runs"
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"
DEFAULT_INTEGRATED_DEMO_MANIFEST = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0912_p6_pdf_truth_binding_bundle"
    / "p6_integrated_demo_manifest.json"
)

REQUIRED_SOURCE_ARTIFACTS = {
    "p6_manifest": "integrated_demo_manifest",
    "p3_visual_rviz_audit": "p3_visual_rviz_audit",
    "stage_simulated_ft_manifest": "stage_simulated_ft_manifest",
    "step_status_rnn_audit": "step_status_rnn_audit",
    "p2_contact_correlation_audit": "p2_contact_correlation_audit",
    "tcp_distance_evidence": "tcp_distance_evidence",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path | str | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(WORKSPACE.resolve()))
    except (OSError, ValueError):
        return str(path)


def workspace_path(path: str | None) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return WORKSPACE / candidate


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_id_for_path(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        relative = path.resolve().relative_to(RUNS.resolve())
    except (OSError, ValueError):
        return None
    return relative.parts[0] if relative.parts else None


def classify_artifact(surface: str, path_value: str | None, *, target_run_id: str | None) -> dict[str, Any]:
    path = workspace_path(path_value)
    exists = bool(path and path.is_file())
    run_id = run_id_for_path(path) if exists else None
    return {
        "surface": surface,
        "path": rel(path_value),
        "exists": exists,
        "run_id": run_id,
        "same_run_as_manifest": bool(run_id and target_run_id and run_id == target_run_id),
        "sha256": sha256_file(path) if exists else None,
    }


def manifest_source_artifacts(manifest: dict[str, Any]) -> dict[str, Any]:
    source_artifacts = manifest.get("source_artifacts")
    return source_artifacts if isinstance(source_artifacts, dict) else {}


def build_audit(
    *,
    generated_at: str | None = None,
    integrated_demo_manifest_path: Path | None = DEFAULT_INTEGRATED_DEMO_MANIFEST,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().isoformat(timespec="seconds")
    manifest_path = integrated_demo_manifest_path
    manifest_exists = bool(manifest_path and manifest_path.is_file())
    validation_issues: list[str] = []
    manifest: dict[str, Any] = {}
    if manifest_exists:
        try:
            manifest = load_json(manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            validation_issues.append(f"integrated_demo_manifest:unreadable:{type(exc).__name__}")
            manifest = {}
    else:
        validation_issues.append("integrated_demo_manifest:missing")

    if manifest.get("schema") != "ur10e_p6_integrated_demo_manifest_v1":
        validation_issues.append("integrated_demo_manifest.schema:unsupported_or_missing")
    if manifest.get("goal_lineage") != GOAL_LINEAGE:
        validation_issues.append("integrated_demo_manifest.goal_lineage:mismatch_or_missing")

    target_run_id = run_id_for_path(manifest_path) if manifest_path and manifest_exists else None
    source_artifacts = manifest_source_artifacts(manifest)
    artifact_rows: list[dict[str, Any]] = [
        classify_artifact(
            "p6_manifest",
            str(manifest_path) if manifest_path is not None else None,
            target_run_id=target_run_id,
        )
    ]
    for surface, manifest_key in REQUIRED_SOURCE_ARTIFACTS.items():
        if surface == "p6_manifest":
            continue
        artifact_rows.append(
            classify_artifact(
                surface,
                source_artifacts.get(manifest_key),
                target_run_id=target_run_id,
            )
        )

    missing_surfaces = [row["surface"] for row in artifact_rows if not row["exists"]]
    cross_run_surfaces = [
        row["surface"]
        for row in artifact_rows
        if row["exists"] and row["surface"] != "p6_manifest" and not row["same_run_as_manifest"]
    ]
    manifest_binding = manifest.get("same_run_binding") if isinstance(manifest.get("same_run_binding"), dict) else {}
    manifest_claims_same_run = all(
        manifest_binding.get(field) is True
        for field in (
            "visual_rviz_simulated_ft_same_run",
            "visual_rviz_physical_gazebo_contact_same_run",
            "step_rnn_physical_gazebo_contact_same_run",
        )
    )
    same_run_integrated_demo_proven = bool(
        manifest_exists
        and target_run_id
        and not validation_issues
        and not missing_surfaces
        and not cross_run_surfaces
        and manifest_claims_same_run
    )
    if missing_surfaces:
        validation_issues.append("required_source_artifacts:missing:" + ",".join(sorted(missing_surfaces)))
    if cross_run_surfaces:
        validation_issues.append("required_source_artifacts:cross_run:" + ",".join(sorted(cross_run_surfaces)))
    if not manifest_claims_same_run:
        validation_issues.append("manifest.same_run_binding:not_all_true")

    return {
        "schema": "ur10e_same_run_integrated_binding_audit_v1",
        "generated_at": generated,
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_report_level_same_run_integrated_binding",
        "claim_tier": "visual_only",
        "live_authorization": {
            "robot_motion_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "urscript_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
            "real_bench_live_contact_authorized": False,
        },
        "integrated_demo_manifest": rel(manifest_path),
        "target_run_id": target_run_id,
        "artifact_rows": artifact_rows,
        "missing_surfaces": sorted(missing_surfaces),
        "cross_run_surfaces": sorted(cross_run_surfaces),
        "manifest_same_run_binding": {
            "binding_status": manifest_binding.get("binding_status"),
            "visual_rviz_simulated_ft_same_run": manifest_binding.get("visual_rviz_simulated_ft_same_run"),
            "visual_rviz_physical_gazebo_contact_same_run": manifest_binding.get("visual_rviz_physical_gazebo_contact_same_run"),
            "step_rnn_physical_gazebo_contact_same_run": manifest_binding.get("step_rnn_physical_gazebo_contact_same_run"),
        },
        "same_run_integrated_demo_proven": same_run_integrated_demo_proven,
        "binding_status": "same_run_integrated_demo_proven"
        if same_run_integrated_demo_proven
        else "cross_run_evidence_only",
        "validation_issues": validation_issues,
        "blocker": None
        if same_run_integrated_demo_proven
        else "No same-run P6 manifest binds visual, RViz, simulated FT, Step/RNN, and Gazebo contact physics evidence.",
        "forbidden_claim": "full UR10e reproduction acceptance; real bench/live contact; physical Gazebo contact outside explicitly proven same-run evidence",
    }


def write_audit(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    integrated_demo_manifest_path: Path | None = DEFAULT_INTEGRATED_DEMO_MANIFEST,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "same_run_integrated_binding_audit.json"
    payload = build_audit(
        generated_at=generated_at,
        integrated_demo_manifest_path=integrated_demo_manifest_path,
    )
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--integrated-demo-manifest", type=Path, default=DEFAULT_INTEGRATED_DEMO_MANIFEST)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        write_audit(
            args.output_dir,
            generated_at=args.generated_at,
            integrated_demo_manifest_path=args.integrated_demo_manifest,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
