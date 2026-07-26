#!/usr/bin/env python3
"""Build an honest P6 integrated-demo evidence bundle scaffold.

This tool is offline only. It bundles retained visual/RViz evidence,
per-stage simulated FT logs, the Step/RNN status audit, and the standalone P2
physical Gazebo witness into one manifest. It also writes source-backed SVG/CSV
plots where data exists and explicit unsupported plot artifacts where data is
missing. It never claims same-run integrated contact physics.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PACKAGE_ROOT = WORKSPACE / "src" / "ur10e_example_controllers"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from ur10e_example_controllers import step56_simulation_matrix as step56  # noqa: E402
from ur10e_example_controllers import step5b_simulation_mvp as step5b_mvp  # noqa: E402

RUNS = EXPERIMENT_ROOT / "runs"
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"

DEFAULT_STAGE_SIM_FT_MANIFEST = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0818_p1_sim_ft_hard_floor"
    / "per_stage_simulated_ft_pack"
    / "step_simulated_ft_evidence_manifest.json"
)
DEFAULT_P3_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_055357_p3_merged_32row_visual_rviz_audit"
    / "p3_visual_rviz_evidence_audit.json"
)
DEFAULT_P2_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0708_p2_gz_sim8_physical_contact_gate"
    / "p2_contact_correlation_audit.json"
)
DEFAULT_STEP_STATUS_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0910_step_status_pdf_truth_binding"
    / "step_status_rnn_audit.json"
)

CLAIM_TIERS = [
    "visual_only",
    "virtual/software force-loop",
    "simulated_ft",
    "physical Gazebo collision/contact physics",
    "real bench/live contact",
]
CONTACT_STAGE_IDS = ["step5b", "step5d", "step6b", "step7", "step8"]
CONCURRENT_OBSERVATION_REQUIRED_SURFACES = [
    "p3_visual_rviz_audit",
    "stage_simulated_ft_manifest",
    "step_status_rnn_audit",
    "p2_contact_correlation_audit",
    "tcp_distance_evidence",
]
SURFACE_NORMAL_BASE = [0.0, 0.0, 1.0]
CONTACT_SURFACE_TOP_Z_M = float(step5b_mvp.CONTACT_SURFACE_Z_M)


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


def sha256_file(path: Path | str | None) -> str | None:
    candidate = path if isinstance(path, Path) else workspace_path(path)
    if candidate is None or not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def p2_claim_tier_row(p2_payload: dict[str, Any]) -> dict[str, str]:
    gate = (
        p2_payload.get("physical_gazebo_contact_gate")
        if isinstance(p2_payload.get("physical_gazebo_contact_gate"), dict)
        else {}
    )
    force_contact_physics_proven = bool(gate.get("force_contact_physics_proven"))
    p2_claim_tier = str(p2_payload.get("claim_tier") or "visual_only")
    if p2_claim_tier != "physical Gazebo collision/contact physics" or not force_contact_physics_proven:
        return {
            "evidence_surface": "P2 contact-correlation audit",
            "current_status": (
                "downgraded; physical Gazebo contact physics not proven "
                "because force_contact_physics_proven=false or wrench/contact correlation is missing"
            ),
            "claim_tier": "visual_only",
        }
    return {
        "evidence_surface": "Standalone P2 physical witness",
        "current_status": (
            "EOAT collision evidence, contact pair/log evidence, contact normal/surface relation, "
            "and wrench/contact correlation exist for the standalone P2 witness only; not stage-specific integrated contact physics"
        ),
        "claim_tier": "physical Gazebo collision/contact physics",
    }


def rows_for_stage(log_path: Path) -> list[dict[str, Any]]:
    payload = load_json(log_path)
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    rows = trace.get("rows") if isinstance(trace.get("rows"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def load_stage_rows(stage_manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    stages = stage_manifest.get("stages") if isinstance(stage_manifest.get("stages"), dict) else {}
    stage_rows: dict[str, list[dict[str, Any]]] = {}
    for stage_id in CONTACT_STAGE_IDS:
        summary = stages.get(stage_id)
        if not isinstance(summary, dict):
            stage_rows[stage_id] = []
            continue
        path = workspace_path(str(summary.get("log_path") or ""))
        stage_rows[stage_id] = rows_for_stage(path) if path and path.is_file() else []
    return stage_rows


def write_combined_csv(path: Path, stage_rows: dict[str, list[dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "stage_id",
        "t_s",
        "frame_id",
        "source",
        "status",
        "claim_tier",
        "contact_state",
        "force_norm_n",
        "normal_load_n",
        "latency_s",
        "stale_after_s",
        "baseline_policy",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for stage_id, rows in stage_rows.items():
            for row in rows:
                header = row.get("header") if isinstance(row.get("header"), dict) else {}
                writer.writerow(
                    {
                        "stage_id": stage_id,
                        "t_s": row.get("t_s", header.get("stamp_s")),
                        "frame_id": header.get("frame_id"),
                        "source": row.get("source"),
                        "status": row.get("status"),
                        "claim_tier": row.get("claim_tier"),
                        "contact_state": row.get("contact_state"),
                        "force_norm_n": row.get("force_norm_n"),
                        "normal_load_n": row.get("normal_load_n"),
                        "latency_s": row.get("latency_s"),
                        "stale_after_s": row.get("stale_after_s"),
                        "baseline_policy": row.get("baseline_policy"),
                    }
                )


def stage_metric_series(stage_rows: dict[str, list[dict[str, Any]]], field: str) -> dict[str, list[tuple[float, float]]]:
    series: dict[str, list[tuple[float, float]]] = {}
    for stage_id, rows in stage_rows.items():
        points: list[tuple[float, float]] = []
        for row in rows:
            header = row.get("header") if isinstance(row.get("header"), dict) else {}
            if row.get(field) is None:
                continue
            points.append((float(row.get("t_s", header.get("stamp_s", 0.0))), float(row[field])))
        series[stage_id] = points
    return series


def contact_state_series(stage_rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[tuple[float, float]]]:
    series: dict[str, list[tuple[float, float]]] = {}
    for stage_id, rows in stage_rows.items():
        points: list[tuple[float, float]] = []
        for row in rows:
            header = row.get("header") if isinstance(row.get("header"), dict) else {}
            value = 1.0 if row.get("contact_state") == "contact" else 0.0
            points.append((float(row.get("t_s", header.get("stamp_s", 0.0))), value))
        series[stage_id] = points
    return series


def threshold_crossing_series(stage_rows: dict[str, list[dict[str, Any]]], *, threshold_n: float = 1.0) -> dict[str, list[tuple[float, float]]]:
    normal_load = stage_metric_series(stage_rows, "normal_load_n")
    return {
        stage_id: [(t, 1.0 if load >= threshold_n else 0.0) for t, load in points]
        for stage_id, points in normal_load.items()
    }


def latency_margin_series(stage_rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[tuple[float, float]]]:
    series: dict[str, list[tuple[float, float]]] = {}
    for stage_id, rows in stage_rows.items():
        points: list[tuple[float, float]] = []
        for row in rows:
            header = row.get("header") if isinstance(row.get("header"), dict) else {}
            latency = float(row.get("latency_s") or 0.0)
            stale_after = float(row.get("stale_after_s") or 0.0)
            points.append((float(row.get("t_s", header.get("stamp_s", 0.0))), stale_after - latency))
        series[stage_id] = points
    return series


def _planned_source_rows(artifact: dict[str, Any], stage_id: str) -> tuple[str, list[dict[str, Any]]]:
    if stage_id == "step5b":
        contact_phase = artifact.get("contact_phase") if isinstance(artifact.get("contact_phase"), dict) else {}
        rows = contact_phase.get("rows") if isinstance(contact_phase.get("rows"), list) else []
        return "step5b.contact_phase.rows", [row for row in rows if isinstance(row, dict)]
    trajectory = artifact.get("trajectory") if isinstance(artifact.get("trajectory"), dict) else {}
    rows = trajectory.get("rows") if isinstance(trajectory.get("rows"), list) else []
    return "trajectory.rows", [row for row in rows if isinstance(row, dict)]


def planned_tcp_distance_rows() -> dict[str, list[dict[str, Any]]]:
    planned: dict[str, list[dict[str, Any]]] = {}
    for stage_id in CONTACT_STAGE_IDS:
        artifact = step56.build_stage_artifact(stage_id)
        source_field, rows = _planned_source_rows(artifact, stage_id)
        stage = artifact.get("stage") if isinstance(artifact.get("stage"), dict) else {}
        stage_rows: list[dict[str, Any]] = []
        for sequence, row in enumerate(rows):
            if stage_id == "step5b":
                tcp_x = float(row["tcp_x_m"])
                tcp_y = float(row["tcp_y_m"])
                tcp_z = float(row["tcp_z_m"])
                z_source = "step5b_contact_phase_tcp_z_m"
            else:
                base_xy = row.get("base_xy_m")
                if not isinstance(base_xy, list) or len(base_xy) != 2:
                    continue
                tcp_x = float(base_xy[0])
                tcp_y = float(base_xy[1])
                raw_z = row.get("base_z_m")
                if raw_z is None:
                    tcp_z = CONTACT_SURFACE_TOP_Z_M
                    z_source = "nominal_contact_plane_assumption_for_contact_stage_without_vertical_trajectory"
                else:
                    tcp_z = float(raw_z)
                    z_source = "trajectory.base_z_m"
            surface_point = [tcp_x, tcp_y, CONTACT_SURFACE_TOP_Z_M]
            distance_m = tcp_z - CONTACT_SURFACE_TOP_Z_M
            stage_rows.append(
                {
                    "stage_id": stage_id,
                    "sequence": sequence,
                    "t_s": float(row["t_s"]),
                    "frame_id": "base",
                    "source": "step56.build_stage_artifact_planned_path_geometry",
                    "source_field": source_field,
                    "source_stage_id": stage.get("source_stage_id"),
                    "claim_tier": "visual_only",
                    "support_scope": "planned_path_geometry_only_not_observed_tcp_pose_not_physical_contact",
                    "tcp_position_base_m": [tcp_x, tcp_y, tcp_z],
                    "contact_surface_position_base_m": surface_point,
                    "surface_normal_base": list(SURFACE_NORMAL_BASE),
                    "distance_to_surface_m": distance_m,
                    "abs_distance_to_surface_m": abs(distance_m),
                    "tcp_z_source": z_source,
                    "forbidden_claim": "same-run integrated demo; physical Gazebo collision/contact physics; real bench/live contact",
                }
            )
        planned[stage_id] = stage_rows
    return planned


def write_tcp_distance_csv(path: Path, planned_rows: dict[str, list[dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "stage_id",
        "sequence",
        "t_s",
        "frame_id",
        "source",
        "source_field",
        "source_stage_id",
        "claim_tier",
        "support_scope",
        "tcp_x_m",
        "tcp_y_m",
        "tcp_z_m",
        "surface_x_m",
        "surface_y_m",
        "surface_z_m",
        "surface_normal_x",
        "surface_normal_y",
        "surface_normal_z",
        "distance_to_surface_m",
        "abs_distance_to_surface_m",
        "tcp_z_source",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for rows in planned_rows.values():
            for row in rows:
                tcp = row["tcp_position_base_m"]
                surface = row["contact_surface_position_base_m"]
                normal = row["surface_normal_base"]
                writer.writerow(
                    {
                        "stage_id": row["stage_id"],
                        "sequence": row["sequence"],
                        "t_s": row["t_s"],
                        "frame_id": row["frame_id"],
                        "source": row["source"],
                        "source_field": row["source_field"],
                        "source_stage_id": row["source_stage_id"],
                        "claim_tier": row["claim_tier"],
                        "support_scope": row["support_scope"],
                        "tcp_x_m": tcp[0],
                        "tcp_y_m": tcp[1],
                        "tcp_z_m": tcp[2],
                        "surface_x_m": surface[0],
                        "surface_y_m": surface[1],
                        "surface_z_m": surface[2],
                        "surface_normal_x": normal[0],
                        "surface_normal_y": normal[1],
                        "surface_normal_z": normal[2],
                        "distance_to_surface_m": row["distance_to_surface_m"],
                        "abs_distance_to_surface_m": row["abs_distance_to_surface_m"],
                        "tcp_z_source": row["tcp_z_source"],
                    }
                )


def planned_tcp_distance_series(planned_rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[tuple[float, float]]]:
    return {
        stage_id: [
            (float(row["t_s"]), float(row["distance_to_surface_m"]))
            for row in rows
        ]
        for stage_id, rows in planned_rows.items()
    }


def downsample(points: list[tuple[float, float]], *, max_points: int = 240) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    return points[::step]


def write_svg_plot(
    path: Path,
    *,
    title: str,
    y_label: str,
    frame_label: str,
    claim_tier: str,
    series: dict[str, list[tuple[float, float]]],
    y_min: float | None = None,
    y_max: float | None = None,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 960, 540
    left, right, top, bottom = 78, 24, 64, 72
    all_points = [point for points in series.values() for point in points]
    x_values = [point[0] for point in all_points] or [0.0, 1.0]
    y_values = [point[1] for point in all_points] or [0.0, 1.0]
    x0, x1 = min(x_values), max(x_values)
    if abs(x1 - x0) < 1e-9:
        x1 = x0 + 1.0
    if y_min is None:
        y_min = min(y_values)
    if y_max is None:
        y_max = max(y_values)
    if abs(y_max - y_min) < 1e-9:
        y_max = y_min + 1.0
    colors = ["#1f77b4", "#2ca02c", "#d62728", "#9467bd", "#ff7f0e"]

    def sx(x: float) -> float:
        return left + (x - x0) / (x1 - x0) * (width - left - right)

    def sy(y: float) -> float:
        return height - bottom - (y - y_min) / (y_max - y_min) * (height - top - bottom)

    lines: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="32" font-family="monospace" font-size="18" fill="#111">{title}</text>',
        f'<text x="{left}" y="52" font-family="monospace" font-size="12" fill="#444">frame={frame_label} | claim_tier={claim_tier}</text>',
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#222"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#222"/>',
        f'<text x="{width/2-60:.1f}" y="{height-24}" font-family="monospace" font-size="12">time (s)</text>',
        f'<text x="14" y="{height/2:.1f}" font-family="monospace" font-size="12" transform="rotate(-90 14 {height/2:.1f})">{y_label}</text>',
        f'<text x="{left}" y="{height-bottom+18}" font-family="monospace" font-size="11" fill="#444">{x0:.2f}</text>',
        f'<text x="{width-right-44}" y="{height-bottom+18}" font-family="monospace" font-size="11" fill="#444">{x1:.2f}</text>',
        f'<text x="24" y="{sy(y_max)+4:.1f}" font-family="monospace" font-size="11" fill="#444">{y_max:.2f}</text>',
        f'<text x="24" y="{sy(y_min)+4:.1f}" font-family="monospace" font-size="11" fill="#444">{y_min:.2f}</text>',
    ]
    for index, (stage_id, points) in enumerate(series.items()):
        sampled = downsample(points)
        if not sampled:
            continue
        color = colors[index % len(colors)]
        coords = " ".join(f"{sx(x):.2f},{sy(y):.2f}" for x, y in sampled)
        lines.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{coords}"/>')
        legend_y = 82 + index * 18
        lines.append(f'<rect x="{width-190}" y="{legend_y-10}" width="14" height="3" fill="{color}"/>')
        lines.append(f'<text x="{width-170}" y="{legend_y-6}" font-family="monospace" font-size="11" fill="#222">{stage_id}</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "path": rel(path),
        "unit_labels": ["s", y_label],
        "frame_label": frame_label,
        "claim_tier": claim_tier,
        "supported": True,
        "format": "svg",
    }


def write_unsupported_svg(
    path: Path,
    *,
    title: str,
    y_label: str,
    frame_label: str,
    claim_tier: str,
    reason: str,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 960, 300
    path.write_text(
        "\n".join(
            [
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
                '<rect width="100%" height="100%" fill="#ffffff"/>',
                f'<text x="40" y="48" font-family="monospace" font-size="18" fill="#111">{title}</text>',
                f'<text x="40" y="82" font-family="monospace" font-size="13" fill="#444">frame={frame_label} | claim_tier={claim_tier}</text>',
                f'<text x="40" y="128" font-family="monospace" font-size="14" fill="#9a3412">unsupported: {reason}</text>',
                f'<text x="40" y="166" font-family="monospace" font-size="12" fill="#444">unit labels: s, {y_label}</text>',
                "</svg>",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "path": rel(path),
        "unit_labels": ["s", y_label],
        "frame_label": frame_label,
        "claim_tier": claim_tier,
        "supported": False,
        "unsupported_reason": reason,
        "format": "svg",
    }


def build_tcp_distance_evidence(
    *,
    stage_rows: dict[str, list[dict[str, Any]]],
    planned_rows: dict[str, list[dict[str, Any]]],
    planned_csv_path: Path,
    p3_payload: dict[str, Any],
    p2_payload: dict[str, Any],
) -> dict[str, Any]:
    """Audit whether current retained inputs can support TCP distance over time."""

    required_time_series_fields = [
        "t_s",
        "tcp_position_base_m",
        "contact_surface_position_base_m",
        "surface_normal_base",
        "distance_to_surface_m",
        "frame_id",
        "source",
    ]
    stage_checks: list[dict[str, Any]] = []
    for stage_id, rows in stage_rows.items():
        rows_with_distance = 0
        rows_with_tcp_position = 0
        rows_with_surface_geometry = 0
        for row in rows:
            if row.get("distance_to_surface_m") is not None:
                rows_with_distance += 1
            if row.get("tcp_position_base_m") is not None:
                rows_with_tcp_position += 1
            if row.get("contact_surface_position_base_m") is not None and row.get("surface_normal_base") is not None:
                rows_with_surface_geometry += 1
        stage_checks.append(
            {
                "stage_id": stage_id,
                "row_count": len(rows),
                "rows_with_distance_to_surface_m": rows_with_distance,
                "rows_with_tcp_position_base_m": rows_with_tcp_position,
                "rows_with_contact_surface_geometry": rows_with_surface_geometry,
                "supports_tcp_distance_time_series": bool(
                    rows
                    and rows_with_distance == len(rows)
                    and rows_with_tcp_position == len(rows)
                    and rows_with_surface_geometry == len(rows)
                ),
            }
        )

    planned_stage_checks: list[dict[str, Any]] = []
    planned_row_count = 0
    for stage_id, rows in planned_rows.items():
        rows_with_distance = sum(1 for row in rows if row.get("distance_to_surface_m") is not None)
        rows_with_tcp_position = sum(1 for row in rows if row.get("tcp_position_base_m") is not None)
        rows_with_surface_geometry = sum(
            1
            for row in rows
            if row.get("contact_surface_position_base_m") is not None
            and row.get("surface_normal_base") is not None
        )
        planned_row_count += len(rows)
        planned_stage_checks.append(
            {
                "stage_id": stage_id,
                "row_count": len(rows),
                "rows_with_distance_to_surface_m": rows_with_distance,
                "rows_with_tcp_position_base_m": rows_with_tcp_position,
                "rows_with_contact_surface_geometry": rows_with_surface_geometry,
                "supports_tcp_distance_time_series": bool(
                    rows
                    and rows_with_distance == len(rows)
                    and rows_with_tcp_position == len(rows)
                    and rows_with_surface_geometry == len(rows)
                ),
            }
        )
    planned_support = bool(planned_stage_checks) and all(
        check["supports_tcp_distance_time_series"] for check in planned_stage_checks
    )

    rviz_manifest = p3_payload.get("p3_requirement_status", {}).get("rviz_debug_evidence", {})
    p2_gate = p2_payload.get("physical_gazebo_contact_gate", {}) if isinstance(p2_payload.get("physical_gazebo_contact_gate"), dict) else {}
    p2_contact = p2_payload.get("contact_pair_log_evidence", {}) if isinstance(p2_payload.get("contact_pair_log_evidence"), dict) else {}
    return {
        "schema": "ur10e_p6_tcp_distance_evidence_audit_v1",
        "claim_tier": "visual_only",
        "tcp_distance_time_series_supported": planned_support,
        "status": "supported_planned_path_geometry_only_not_same_run" if planned_support else "not_supported_missing_same_run_tcp_distance_time_series",
        "support_scope": "planned_path_geometry_only_not_observed_tcp_pose_not_physical_contact",
        "planned_tcp_distance_csv": rel(planned_csv_path),
        "planned_tcp_distance_row_count": planned_row_count,
        "required_time_series_fields": required_time_series_fields,
        "candidate_source_audit": [
            {
                "source": "step56.build_stage_artifact planned trajectory/contact_phase",
                "claim_tier": "visual_only",
                "stage_checks": planned_stage_checks,
                "supports_p6_tcp_distance": planned_support,
                "reason": "source-backed planned path geometry provides t_s, base-frame TCP position, contact-surface projection, surface normal, and distance fields; not observed TCP pose or Gazebo contact physics",
            },
            {
                "source": "per_stage_simulated_ft_logs",
                "claim_tier": "simulated_ft",
                "stage_checks": stage_checks,
                "supports_p6_tcp_distance": False,
                "reason": "canonical simulated_ft rows provide wrench/contact/status fields but no TCP position and contact-surface geometry time series",
            },
            {
                "source": "p3_static_rviz_debug_scene",
                "claim_tier": "visual_only",
                "frames_evidenced": bool(rviz_manifest.get("all_required_items_evidenced")),
                "supports_p6_tcp_distance": False,
                "reason": "static RViz frames and markers are observer evidence only, not a time series of TCP distance to surface",
            },
            {
                "source": "standalone_p2_gazebo_contact_witness",
                "claim_tier": "physical Gazebo collision/contact physics" if p2_gate.get("force_contact_physics_proven") else "visual_only",
                "contact_pair_log_evidence": bool(p2_contact.get("present")),
                "force_contact_physics_proven": bool(p2_gate.get("force_contact_physics_proven")),
                "supports_p6_tcp_distance": False,
                "reason": "standalone P2 contact pair/wrench evidence has contact position/depth but no per-stage P6 TCP pose time series",
            },
        ],
        "blocked_claim": "same-run integrated demo readiness; per-stage physical Gazebo contact physics; observed TCP pose; real bench/live contact",
        "downgrade_rule": "Do not infer observed TCP distance from screenshots, static RViz markers, normal_load, contact_state, standalone contact depth, or planned path geometry.",
    }


def write_source_evidence(
    output_dir: Path,
    *,
    p3_payload: dict[str, Any],
    p2_payload: dict[str, Any],
    step_payload: dict[str, Any],
    stage_manifest: dict[str, Any],
    stage_rows: dict[str, list[dict[str, Any]]],
    planned_rows: dict[str, list[dict[str, Any]]],
    planned_tcp_distance_csv: Path,
) -> dict[str, str]:
    evidence_dir = output_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    platform_path = evidence_dir / "platform_trajectory_evidence.json"
    platform_path.write_text(
        json.dumps(
            {
                "claim_tier": "visual_only",
                "source": "Step/RNN status matrix and P3 observer rows",
                "stage_ids": step_payload.get("audit_coverage", {}).get("stage_rows"),
                "step_status_matrix": step_payload.get("step_status_matrix", []),
                "forbidden_claim": "strict RNN final acceptance; physical Gazebo collision/contact physics; real bench/live contact",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    eoat_path = evidence_dir / "eoat_tooling_evidence.json"
    eoat_path.write_text(
        json.dumps(
            {
                "claim_tier": "visual_only",
                "gazebo_observer_criteria_counts": p3_payload.get("gazebo_observer_evidence", {}).get("criteria_counts", {}),
                "rviz_required_items": p3_payload.get("p3_requirement_status", {}).get("rviz_debug_evidence", {}).get("evidenced_items", {}),
                "forbidden_claim": "physical Gazebo collision/contact physics; real bench/live contact",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    surface_path = evidence_dir / "contact_surface_evidence.json"
    surface_path.write_text(
        json.dumps(
            {
                "claim_tier": "visual_only",
                "p3_surface_path_visible": p3_payload.get("gazebo_observer_evidence", {}).get("criteria_counts", {}).get("surface_path_visible", {}),
                "standalone_p2_physical_gate": p2_payload.get("physical_gazebo_contact_gate", {}),
                "p2_scope": "standalone P2 witness only; not per-stage integrated contact surface proof",
                "forbidden_claim": "per-stage physical Gazebo collision/contact physics; per-stage total contact wrench; real bench/live contact",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    sim_summary_path = evidence_dir / "simulated_ft_bundle_summary.json"
    sim_summary_path.write_text(
        json.dumps(
            {
                "claim_tier": stage_manifest.get("claim_tier"),
                "stage_count": stage_manifest.get("stage_count"),
                "valid_stage_count": stage_manifest.get("valid_stage_count"),
                "stages": stage_manifest.get("stages", {}),
                "forbidden_claim": "physical Gazebo collision/contact physics; real bench/live contact",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    tcp_distance_path = evidence_dir / "tcp_distance_evidence.json"
    tcp_distance_path.write_text(
        json.dumps(
            build_tcp_distance_evidence(
                stage_rows=stage_rows,
                planned_rows=planned_rows,
                planned_csv_path=planned_tcp_distance_csv,
                p3_payload=p3_payload,
                p2_payload=p2_payload,
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "platform_trajectory_evidence": rel(platform_path),
        "eoat_tooling_evidence": rel(eoat_path),
        "contact_surface_evidence": rel(surface_path),
        "simulated_ft_summary": rel(sim_summary_path),
        "tcp_distance_evidence": rel(tcp_distance_path),
    }


def negative_concurrent_observation() -> dict[str, Any]:
    return {
        "observation_id": None,
        "same_run_concurrent_observation_explicit": False,
        "concurrent_observation_proven": False,
        "time_window": {
            "start": None,
            "end": None,
            "clock_source": None,
        },
        "surfaces": [],
        "missing_surfaces": CONCURRENT_OBSERVATION_REQUIRED_SURFACES,
        "required_surfaces": CONCURRENT_OBSERVATION_REQUIRED_SURFACES,
        "blockers": [
            "cross_run_retained_evidence_bundle:not_concurrent_observation",
            "same_run_observation_id:missing",
            "same_run_time_window:missing",
        ],
        "forbidden_claim": "same-run integrated demo; same-run dual-sensor observation; full UR10e reproduction acceptance",
    }


def concurrent_observation_from_path(path: Path | None) -> dict[str, Any]:
    if path is None:
        return negative_concurrent_observation()
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        observation = negative_concurrent_observation()
        observation["source_path"] = rel(path)
        observation["blockers"] = [
            *observation["blockers"],
            f"concurrent_observation_source:unreadable:{type(exc).__name__}",
        ]
        return observation
    observation = dict(payload)
    observation["source_path"] = rel(path)
    observation.setdefault("concurrent_observation_proven", False)
    observation.setdefault("required_surfaces", CONCURRENT_OBSERVATION_REQUIRED_SURFACES)
    observation.setdefault(
        "forbidden_claim",
        "same-run integrated demo unless same-run audit validates this observation and all source artifacts",
    )
    return observation


def write_bundle(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    stage_sim_ft_manifest_path: Path = DEFAULT_STAGE_SIM_FT_MANIFEST,
    p3_audit_path: Path = DEFAULT_P3_AUDIT,
    p2_audit_path: Path = DEFAULT_P2_AUDIT,
    step_status_audit_path: Path = DEFAULT_STEP_STATUS_AUDIT,
    concurrent_observation_path: Path | None = None,
) -> Path:
    generated = generated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    data_dir = output_dir / "data"

    stage_manifest = load_json(stage_sim_ft_manifest_path)
    p3_payload = load_json(p3_audit_path)
    p2_payload = load_json(p2_audit_path)
    step_payload = load_json(step_status_audit_path)
    stage_rows = load_stage_rows(stage_manifest)
    planned_rows = planned_tcp_distance_rows()

    combined_csv = data_dir / "p6_per_stage_simulated_ft_combined.csv"
    write_combined_csv(combined_csv, stage_rows)
    planned_tcp_distance_csv = data_dir / "p6_planned_tcp_distance_to_surface.csv"
    write_tcp_distance_csv(planned_tcp_distance_csv, planned_rows)
    source_evidence = write_source_evidence(
        output_dir,
        p3_payload=p3_payload,
        p2_payload=p2_payload,
        step_payload=step_payload,
        stage_manifest=stage_manifest,
        stage_rows=stage_rows,
        planned_rows=planned_rows,
        planned_tcp_distance_csv=planned_tcp_distance_csv,
    )

    plots = {
        "wrench_vs_time": write_svg_plot(
            plots_dir / "wrench_vs_time.svg",
            title="P6 per-stage simulated_ft normal load vs time",
            y_label="normal_load_n (N)",
            frame_label="base",
            claim_tier="simulated_ft",
            series=stage_metric_series(stage_rows, "normal_load_n"),
            y_min=0.0,
            y_max=6.0,
        ),
        "contact_state_vs_time": write_svg_plot(
            plots_dir / "contact_state_vs_time.svg",
            title="P6 per-stage simulated_ft contact state vs time",
            y_label="contact_state (0/1)",
            frame_label="base",
            claim_tier="simulated_ft",
            series=contact_state_series(stage_rows),
            y_min=0.0,
            y_max=1.2,
        ),
        "force_threshold_crossing": write_svg_plot(
            plots_dir / "force_threshold_crossing.svg",
            title="P6 per-stage simulated_ft force threshold crossing",
            y_label="normal_load >= 1N (0/1)",
            frame_label="base",
            claim_tier="simulated_ft",
            series=threshold_crossing_series(stage_rows),
            y_min=0.0,
            y_max=1.2,
        ),
        "latency_staleness": write_svg_plot(
            plots_dir / "latency_staleness.svg",
            title="P6 per-stage simulated_ft staleness margin vs time",
            y_label="stale_after_s - latency_s (s)",
            frame_label="base",
            claim_tier="simulated_ft",
            series=latency_margin_series(stage_rows),
            y_min=0.0,
            y_max=0.12,
        ),
        "tcp_distance_to_surface_vs_time": write_svg_plot(
            plots_dir / "tcp_distance_to_surface_vs_time.svg",
            title="P6 planned TCP distance to surface vs time",
            y_label="distance_to_surface_m",
            frame_label="base",
            claim_tier="visual_only",
            series=planned_tcp_distance_series(planned_rows),
            y_min=-0.002,
            y_max=0.02,
        ),
        "gravity_residual": write_unsupported_svg(
            plots_dir / "gravity_residual.unsupported.svg",
            title="P6 gravity residual vs time",
            y_label="gravity_residual_n",
            frame_label="base",
            claim_tier="visual_only",
            reason="no gravity residual source exists in the retained P6 evidence inputs",
        ),
    }

    gazebo = p3_payload.get("gazebo_observer_evidence", {}) if isinstance(p3_payload.get("gazebo_observer_evidence"), dict) else {}
    rviz = (
        p3_payload.get("p3_requirement_status", {}).get("rviz_debug_evidence", {})
        if isinstance(p3_payload.get("p3_requirement_status"), dict)
        else {}
    )
    simulated_ft_artifacts = [
        str(summary.get("log_path"))
        for stage_id, summary in sorted((stage_manifest.get("stages") or {}).items())
        if stage_id in CONTACT_STAGE_IDS and isinstance(summary, dict) and summary.get("log_path")
    ]
    source_artifacts = {
        "stage_simulated_ft_manifest": rel(stage_sim_ft_manifest_path),
        "p3_visual_rviz_audit": rel(p3_audit_path),
        "p2_contact_correlation_audit": rel(p2_audit_path),
        "step_status_rnn_audit": rel(step_status_audit_path),
        "combined_simulated_ft_csv": rel(combined_csv),
        "planned_tcp_distance_csv": rel(planned_tcp_distance_csv),
        **source_evidence,
    }
    manifest = {
        "schema": "ur10e_p6_integrated_demo_manifest_v1",
        "generated_at": generated,
        "goal_lineage": GOAL_LINEAGE,
        "run_root": str(output_dir),
        "fail_closed": True,
        "mode": "offline_cross_run_p6_evidence_bundle_not_full_acceptance",
        "claim_tier": "visual_only",
        "status": "partial_cross_run_bundle_planned_tcp_distance_supported_not_full_acceptance",
        "same_run_integrated_demo_proven": False,
        "same_run_binding": {
            "visual_rviz_simulated_ft_same_run": False,
            "visual_rviz_physical_gazebo_contact_same_run": False,
            "step_rnn_physical_gazebo_contact_same_run": False,
            "binding_status": "cross_run_evidence_only",
        },
        "concurrent_observation": concurrent_observation_from_path(concurrent_observation_path),
        "platform_trajectory_evidence": source_evidence["platform_trajectory_evidence"],
        "eoat_tooling_evidence": source_evidence["eoat_tooling_evidence"],
        "contact_surface_evidence": source_evidence["contact_surface_evidence"],
        "tcp_distance_evidence": source_evidence["tcp_distance_evidence"],
        "simulated_ft_artifacts": simulated_ft_artifacts,
        "step_rnn_pipeline_artifact": rel(step_status_audit_path),
        "gazebo_gui_evidence_paths": [gazebo.get("contact_sheet_path")] if gazebo.get("contact_sheet_path") else [],
        "rviz_evidence_paths": rviz.get("screenshot_paths", []),
        "plots": plots,
        "source_artifacts": source_artifacts,
        "source_artifact_sha256": {key: sha256_file(value) for key, value in source_artifacts.items()},
        "current_claim_tier_table": [
            {
                "evidence_surface": "P6 bundle",
                "current_status": "cross-run evidence bundle; not same-run integrated demo",
                "claim_tier": "visual_only",
            },
            {
                "evidence_surface": "Per-stage simulated FT plots",
                "current_status": "supported by canonical simulated_ft logs with stamp, frame_id, source, status, baseline, and log evidence",
                "claim_tier": "simulated_ft",
            },
            p2_claim_tier_row(p2_payload),
        ],
        "forbidden_claim": (
            "P6 integrated demo readiness; full UR10e reproduction acceptance; per-stage physical Gazebo contact; "
            "per-stage/full-goal total contact wrench unless same-run stage-specific evidence exists; real bench/live contact"
        ),
    }
    manifest_path = output_dir / "p6_integrated_demo_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--stage-sim-ft-manifest", type=Path, default=DEFAULT_STAGE_SIM_FT_MANIFEST)
    parser.add_argument("--p3-audit-path", type=Path, default=DEFAULT_P3_AUDIT)
    parser.add_argument("--p2-audit-path", type=Path, default=DEFAULT_P2_AUDIT)
    parser.add_argument("--step-status-audit-path", type=Path, default=DEFAULT_STEP_STATUS_AUDIT)
    parser.add_argument("--concurrent-observation", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_bundle(
        args.output_dir,
        generated_at=args.generated_at,
        stage_sim_ft_manifest_path=args.stage_sim_ft_manifest,
        p3_audit_path=args.p3_audit_path,
        p2_audit_path=args.p2_audit_path,
        step_status_audit_path=args.step_status_audit_path,
        concurrent_observation_path=args.concurrent_observation,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
