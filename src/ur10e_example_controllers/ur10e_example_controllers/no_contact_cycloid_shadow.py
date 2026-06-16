from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .guarded_contact_recovery_shadow import RUNS_ROOT, WORKSPACE_ROOT, _json_safe, _simple_yaml


DEFAULT_CONFIG = WORKSPACE_ROOT / "src" / "ur10e_example_controllers" / "config" / "no_contact_cycloid.yaml"

TRACE_FIELDS = [
    "row_index",
    "t_rel_s",
    "would_state",
    "phase_rad",
    "desired_x_m",
    "desired_y_m",
    "desired_z_m",
    "desired_vx_m_s",
    "desired_vy_m_s",
    "desired_vz_m_s",
    "reference_speed_m_s",
    "normal_load_n",
    "force_norm_n",
    "cmd_valid_shadow",
    "cmd_enabled",
    "would_command_twist_x",
    "would_command_twist_y",
    "would_command_twist_z",
    "would_command_twist_rx",
    "would_command_twist_ry",
    "would_command_twist_rz",
]


def load_no_contact_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    if not path.is_absolute():
        path = WORKSPACE_ROOT / path
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        raw = _simple_yaml(path.read_text(encoding="utf-8"))
    return raw


def run_no_contact_shadow(
    *,
    config_path: Path = DEFAULT_CONFIG,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    if not config_path.is_absolute():
        config_path = WORKSPACE_ROOT / config_path
    raw_config = load_no_contact_config(config_path)
    if bool(raw_config.get("enable_motion", False)):
        raise RuntimeError("Step5a remote shadow refuses enable_motion=true; live air motion needs the live gate")
    if bool(raw_config.get("live_air_motion_authorized", False)):
        raise RuntimeError("Step5a remote shadow refuses live_air_motion_authorized=true")

    created_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = output_dir or RUNS_ROOT / f"step5a_ros2_remote_no_contact_shadow_{created_at}"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = list(_iter_reference_rows(raw_config))
    trace_path = out_dir / "shadow_trace.csv"
    with trace_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRACE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_safe(row))

    speeds = [float(row["reference_speed_m_s"]) for row in rows]
    phases = [float(row["phase_rad"]) for row in rows]
    enabled_rows = [row for row in rows if bool(row["cmd_enabled"])]
    cmd_valid_rows = [row for row in rows if bool(row["cmd_valid_shadow"])]
    contact_policy = dict(raw_config.get("contact_policy", {}))
    no_contact_policy = (
        contact_policy.get("force_control") is False
        and contact_policy.get("contact_search") is False
        and contact_policy.get("zero_ftsensor") is False
        and contact_policy.get("tcp_payload_write") is False
        and contact_policy.get("external_control_urcap") is False
    )
    velocity_cap = float(raw_config["velocity_cap_m_s"])
    summary = {
        "analysis_created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "no_contact_cycloid_shadow_no_live_robot_action",
        "role": raw_config.get("role", "no_contact_air_motion_gate_before_step5b_contact"),
        "stage_id": raw_config.get("stage_id", "step5a_ros2_remote_no_contact_v1"),
        "config_path": str(config_path),
        "artifact_dir": str(out_dir),
        "trace_path": str(trace_path),
        "summary_path": str(out_dir / "summary.json"),
        "enable_motion": False,
        "live_air_motion_authorized": False,
        "tp_action_required": "none_no_tp_play_no_program_load",
        "external_control_urcap_required": False,
        "requires_5a0_driver_readiness": bool(raw_config.get("requires_5a0_driver_readiness", True)),
        "force_source": raw_config.get("force_source", "kunwei_kwr75b_tcp_pre_motion_data_gate"),
        "kunwei_data_gate_required": bool(raw_config.get("kunwei_data_gate_required", True)),
        "kunwei_bridge_mode": "direct_tcp_no_legacy_bridge",
        "ur_internal_force_delta_gate": bool(raw_config.get("ur_internal_force_delta_gate", True)),
        "rows_replayed": len(rows),
        "cmd_valid_shadow_rows": len(cmd_valid_rows),
        "cmd_enabled_any": bool(enabled_rows),
        "normal_load_n": {"min": 0.0, "mean": 0.0, "max": 0.0},
        "force_norm_n": {"min": 0.0, "mean": 0.0, "max": 0.0},
        "duration_s": float(raw_config["duration_s"]),
        "fixed_base_z_m": float(raw_config["fixed_base_z_m"]),
        "final_phase_rad": max(phases) if phases else math.nan,
        "max_reference_speed_m_s": max(speeds) if speeds else math.nan,
        "velocity_cap_m_s": velocity_cap,
        "trace_fields": TRACE_FIELDS,
        "next_live_gate": "explicit_no_contact_air_motion_gate_required_before_any_trajectory_publication",
        "step5b_status": "blocked_until_5a0_driver_readiness_and_step5a_no_contact_pass",
        "contact_policy": contact_policy,
        "acceptance": {
            "default_no_motion": not enabled_rows and len(rows) > 0,
            "no_contact_policy": no_contact_policy,
            "reference_speed_within_cap": max(speeds) <= velocity_cap + 1e-12 if speeds else False,
            "geometry_complete": math.isclose(max(phases), float(raw_config["final_phase_rad"]), rel_tol=0.0, abs_tol=1e-9)
            if phases
            else False,
            "requires_5a0_driver_readiness": bool(raw_config.get("requires_5a0_driver_readiness", True)),
            "artifact_schema_complete": True,
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(_json_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the UR10e no-contact cycloid shadow.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    summary = run_no_contact_shadow(config_path=args.config, output_dir=args.output_dir)
    print(json.dumps(_json_safe(summary), indent=2, sort_keys=True))
    return 0 if all(summary["acceptance"].values()) else 2


def _iter_reference_rows(raw_config: dict[str, Any]) -> list[dict[str, Any]]:
    duration = float(raw_config["duration_s"])
    dt = float(raw_config["sample_period_s"])
    amplitude = float(raw_config["amplitude_m"])
    omega = float(raw_config["omega_rad_s"])
    final_phase = float(raw_config["final_phase_rad"])
    fixed_z = float(raw_config["fixed_base_z_m"])
    rows: list[dict[str, Any]] = []
    sample_count = int(math.floor(duration / dt)) + 1
    for row_index in range(sample_count + 1):
        t_rel_s = min(duration, row_index * dt)
        phase = min(final_phase, omega * t_rel_s)
        vx = amplitude * omega * (1.0 - math.cos(phase))
        vy = amplitude * omega * math.sin(phase)
        speed = math.hypot(vx, vy)
        cmd_valid = t_rel_s >= float(raw_config.get("fast_after_s", 0.1))
        rows.append(
            {
                "row_index": row_index,
                "t_rel_s": t_rel_s,
                "would_state": "NO_CONTACT_AIR_MOTION_CANDIDATE" if cmd_valid else "DRIVER_READY_NO_MOTION",
                "phase_rad": phase,
                "desired_x_m": amplitude * (phase - math.sin(phase)),
                "desired_y_m": amplitude * (1.0 - math.cos(phase)),
                "desired_z_m": fixed_z,
                "desired_vx_m_s": vx,
                "desired_vy_m_s": vy,
                "desired_vz_m_s": 0.0,
                "reference_speed_m_s": speed,
                "normal_load_n": 0.0,
                "force_norm_n": 0.0,
                "cmd_valid_shadow": cmd_valid,
                "cmd_enabled": False,
                "would_command_twist_x": vx,
                "would_command_twist_y": vy,
                "would_command_twist_z": 0.0,
                "would_command_twist_rx": 0.0,
                "would_command_twist_ry": 0.0,
                "would_command_twist_rz": 0.0,
            }
        )
        if t_rel_s >= duration:
            break
    return rows


def _csv_safe(row: dict[str, Any]) -> dict[str, Any]:
    return {field: _json_safe(row.get(field, "")) for field in TRACE_FIELDS}


if __name__ == "__main__":
    raise SystemExit(main())
