#!/usr/bin/env python3
"""Offline calibrated-kinematics audit for quarantined Step5c runs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pinocchio as pin
import xacro
import yaml


if TYPE_CHECKING:
    import pandas as pd


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]


def find_workspace_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "src" / "ur10e_bringup" / "config" / "ur10e_calibration.yaml").exists():
            return candidate
    return start.parents[3]


WS_ROOT = find_workspace_root(EXPERIMENT_ROOT)
DEFAULT_CALIBRATION_YAML = WS_ROOT / "src" / "ur10e_bringup" / "config" / "ur10e_calibration.yaml"
DEFAULT_XACRO_PATH = Path("/opt/ros/humble/share/ur_description/urdf/ur.urdf.xacro")
DEFAULT_BRIDGE_CSV = (
    EXPERIMENT_ROOT
    / "runs"
    / "bridge_step4e_line_outerloop_step5c_speedj_dryrun_v1_20260613_001228"
    / "bridge_rtde_500hz.csv"
)
DEFAULT_METADATA_PATH = DEFAULT_BRIDGE_CSV.parent / "metadata.json"
DEFAULT_SUMMARY_PATH = DEFAULT_BRIDGE_CSV.parent / "summary.json"
EXPECTED_CALIBRATION_HASH = "calib_7367377276742883610"
OFFSET_STD_LIMIT_M = 0.00025
OFFSET_NORM_MIN_M = 0.1218
OFFSET_NORM_MAX_M = 0.1224
SPEED_VECTOR_RMS_LIMIT = 1e-5
ROTATION_ERROR_LIMIT_RAD = 1e-4
REQUIRED_FRAMES = ("base", "tool0", "flange")


@dataclass(frozen=True)
class CalibratedModel:
    model: pin.Model
    data: pin.Data
    urdf_text: str
    calibration_hash: str
    base_frame_id: int
    tool0_frame_id: int
    flange_frame_id: int


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_calibration_hash(path: Path) -> str:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return str(payload["kinematics"]["hash"])
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"calibration YAML has no kinematics.hash: {path}") from exc


def generate_calibrated_urdf(
    calibration_yaml: Path = DEFAULT_CALIBRATION_YAML,
    xacro_path: Path = DEFAULT_XACRO_PATH,
) -> tuple[str, str]:
    if not calibration_yaml.exists():
        raise FileNotFoundError(f"calibration YAML not found: {calibration_yaml}")
    if not xacro_path.exists():
        raise FileNotFoundError(f"UR xacro not found: {xacro_path}")
    calibration_hash = load_calibration_hash(calibration_yaml)
    doc = xacro.process_file(
        str(xacro_path),
        mappings={
            "name": "ur",
            "ur_type": "ur10e",
            "robot_ip": "192.168.1.18",
            "kinematics_params": str(calibration_yaml),
        },
    )
    return doc.toxml(), calibration_hash


def build_calibrated_model(
    calibration_yaml: Path = DEFAULT_CALIBRATION_YAML,
    xacro_path: Path = DEFAULT_XACRO_PATH,
) -> CalibratedModel:
    urdf_text, calibration_hash = generate_calibrated_urdf(calibration_yaml, xacro_path)
    with tempfile.NamedTemporaryFile("w", suffix=".urdf", encoding="utf-8", delete=False) as handle:
        handle.write(urdf_text)
        temp_urdf = Path(handle.name)
    try:
        model = pin.buildModelFromUrdf(str(temp_urdf))
    finally:
        temp_urdf.unlink(missing_ok=True)
    frames = {frame.name for frame in model.frames}
    missing = sorted(set(REQUIRED_FRAMES) - frames)
    if missing:
        raise RuntimeError(f"calibrated URDF missing required frames: {missing}")
    if model.nq != 6 or model.nv != 6:
        raise RuntimeError(f"expected nq=6,nv=6, got nq={model.nq},nv={model.nv}")
    return CalibratedModel(
        model=model,
        data=model.createData(),
        urdf_text=urdf_text,
        calibration_hash=calibration_hash,
        base_frame_id=model.getFrameId("base"),
        tool0_frame_id=model.getFrameId("tool0"),
        flange_frame_id=model.getFrameId("flange"),
    )


def rotvec_to_matrix(rotvec: np.ndarray) -> np.ndarray:
    theta = float(np.linalg.norm(rotvec))
    if theta < 1e-12:
        return np.eye(3)
    axis = rotvec / theta
    skew = np.array(
        [
            [0.0, -axis[2], axis[1]],
            [axis[2], 0.0, -axis[0]],
            [-axis[1], axis[0], 0.0],
        ],
        dtype=float,
    )
    return np.eye(3) + math.sin(theta) * skew + (1.0 - math.cos(theta)) * (skew @ skew)


def rotation_angle(matrix: np.ndarray) -> float:
    cos_angle = float((np.trace(matrix) - 1.0) * 0.5)
    return math.acos(max(-1.0, min(1.0, cos_angle)))


def finite_run_rows(csv_path: Path) -> pd.DataFrame:
    import pandas as pd

    cols = (
        [f"ur_actual_q_{idx}" for idx in range(6)]
        + [f"ur_actual_qd_{idx}" for idx in range(6)]
        + [f"ur_actual_TCP_pose_{idx}" for idx in range(6)]
        + [f"ur_actual_TCP_speed_{idx}" for idx in range(6)]
    )
    df = pd.read_csv(csv_path)
    missing = [col for col in cols if col not in df.columns]
    if missing:
        raise RuntimeError(f"bridge CSV missing required columns: {missing}")
    keep_cols = ["write_index", "t_monotonic_s", "_step4e_path_time_s", "step4e_cmd_valid"] + cols
    present_keep_cols = [col for col in keep_cols if col in df.columns]
    out = df[present_keep_cols].replace([np.inf, -np.inf], np.nan).dropna(subset=cols).copy()
    if out.empty:
        raise RuntimeError(f"bridge CSV has no finite RTDE kinematics rows: {csv_path}")
    return out


def base_to_tool0(model_bundle: CalibratedModel, q: np.ndarray) -> pin.SE3:
    pin.forwardKinematics(model_bundle.model, model_bundle.data, q)
    pin.updateFramePlacements(model_bundle.model, model_bundle.data)
    base = model_bundle.data.oMf[model_bundle.base_frame_id]
    tool0 = model_bundle.data.oMf[model_bundle.tool0_frame_id]
    return base.inverse() * tool0


def infer_tcp_offset(model_bundle: CalibratedModel, rows: pd.DataFrame) -> dict[str, Any]:
    offsets = []
    rotation_errors = []
    for _, row in rows.iterrows():
        q = np.array([row[f"ur_actual_q_{idx}"] for idx in range(6)], dtype=float)
        tcp_pos = np.array([row[f"ur_actual_TCP_pose_{idx}"] for idx in range(3)], dtype=float)
        tcp_rot = rotvec_to_matrix(np.array([row[f"ur_actual_TCP_pose_{idx}"] for idx in range(3, 6)], dtype=float))
        placement = base_to_tool0(model_bundle, q)
        offsets.append(placement.rotation.T @ (tcp_pos - placement.translation))
        rotation_errors.append(rotation_angle(placement.rotation.T @ tcp_rot))

    offset_array = np.vstack(offsets)
    mean = offset_array.mean(axis=0)
    deviations = offset_array - mean
    deviation_norm = np.linalg.norm(deviations, axis=1)
    return {
        "offset_array": offset_array,
        "mean": mean,
        "std": offset_array.std(axis=0),
        "max_abs_std_m": float(np.max(offset_array.std(axis=0))),
        "norm_m": float(np.linalg.norm(mean)),
        "max_deviation_norm_m": float(np.max(deviation_norm)),
        "rotation_error_mean_rad": float(np.mean(rotation_errors)),
        "rotation_error_max_rad": float(np.max(rotation_errors)),
    }


def predict_tcp_speed(
    model_bundle: CalibratedModel,
    q: np.ndarray,
    qd: np.ndarray,
    tcp_offset_tool0: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    model = model_bundle.model
    data = model_bundle.data
    pin.forwardKinematics(model, data, q, qd)
    pin.computeJointJacobians(model, data, q)
    pin.updateFramePlacements(model, data)
    tool_jac_world_aligned = pin.getFrameJacobian(
        model,
        data,
        model_bundle.tool0_frame_id,
        pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
    )
    tool_twist_world = tool_jac_world_aligned @ qd
    base_world_rotation = data.oMf[model_bundle.base_frame_id].rotation.T
    tool_linear_base = base_world_rotation @ tool_twist_world[:3]
    tool_angular_base = base_world_rotation @ tool_twist_world[3:]
    tool0_base = data.oMf[model_bundle.base_frame_id].inverse() * data.oMf[model_bundle.tool0_frame_id]
    tcp_lever_base = tool0_base.rotation @ tcp_offset_tool0
    tcp_linear_base = tool_linear_base + np.cross(tool_angular_base, tcp_lever_base)
    return np.r_[tcp_linear_base, tool_angular_base], tool0_base.translation.copy(), tcp_lever_base


def compute_residuals(
    model_bundle: CalibratedModel,
    rows: pd.DataFrame,
    tcp_offset_tool0: np.ndarray,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    import pandas as pd

    records: list[dict[str, float]] = []
    residuals = []
    for _, row in rows.iterrows():
        q = np.array([row[f"ur_actual_q_{idx}"] for idx in range(6)], dtype=float)
        qd = np.array([row[f"ur_actual_qd_{idx}"] for idx in range(6)], dtype=float)
        actual_pose = np.array([row[f"ur_actual_TCP_pose_{idx}"] for idx in range(6)], dtype=float)
        actual_speed = np.array([row[f"ur_actual_TCP_speed_{idx}"] for idx in range(6)], dtype=float)
        predicted_speed, tool0_pos, tcp_lever_base = predict_tcp_speed(model_bundle, q, qd, tcp_offset_tool0)
        speed_residual = predicted_speed - actual_speed
        residuals.append(speed_residual)
        placement = base_to_tool0(model_bundle, q)
        inferred_offset = placement.rotation.T @ (actual_pose[:3] - placement.translation)
        record: dict[str, float] = {
            "write_index": float(row.get("write_index", np.nan)),
            "t_monotonic_s": float(row.get("t_monotonic_s", np.nan)),
            "path_time_s": float(row.get("_step4e_path_time_s", np.nan)),
            "cmd_valid": float(row.get("step4e_cmd_valid", np.nan)),
            "linear_residual_norm_m_s": float(np.linalg.norm(speed_residual[:3])),
            "angular_residual_norm_rad_s": float(np.linalg.norm(speed_residual[3:])),
        }
        for idx in range(3):
            record[f"fk_tool0_base_{idx}_m"] = float(tool0_pos[idx])
            record[f"tcp_lever_base_{idx}_m"] = float(tcp_lever_base[idx])
            record[f"inferred_tcp_offset_tool0_{idx}_m"] = float(inferred_offset[idx])
        for idx in range(6):
            record[f"predicted_tcp_speed_{idx}"] = float(predicted_speed[idx])
            record[f"actual_tcp_speed_{idx}"] = float(actual_speed[idx])
            record[f"tcp_speed_residual_{idx}"] = float(speed_residual[idx])
        records.append(record)

    residual_array = np.vstack(residuals)
    linear = residual_array[:, :3]
    angular = residual_array[:, 3:]
    metrics = {
        "samples": int(len(rows)),
        "linear_component_rms_m_s": float(np.sqrt(np.mean(linear**2))),
        "angular_component_rms_rad_s": float(np.sqrt(np.mean(angular**2))),
        "linear_vector_rms_m_s": float(np.sqrt(np.mean(np.sum(linear**2, axis=1)))),
        "angular_vector_rms_rad_s": float(np.sqrt(np.mean(np.sum(angular**2, axis=1)))),
        "linear_vector_max_m_s": float(np.max(np.linalg.norm(linear, axis=1))),
        "angular_vector_max_rad_s": float(np.max(np.linalg.norm(angular, axis=1))),
    }
    return pd.DataFrame.from_records(records), metrics


def old_mujoco_failure_summary(metadata_path: Path = DEFAULT_METADATA_PATH, summary_path: Path = DEFAULT_SUMMARY_PATH) -> dict[str, Any]:
    metadata = load_json(metadata_path) if metadata_path.exists() else {}
    run_summary = load_json(summary_path) if summary_path.exists() else {}
    args = metadata.get("args", {})
    return {
        "run_dir": str(metadata_path.parent),
        "step5c_stage_id": args.get("step4e_version"),
        "old_joint_model": args.get("step5c_joint_model"),
        "old_joint_site": args.get("step5c_joint_site"),
        "old_solver": "bounded MuJoCo nominal DLS least-squares",
        "known_failure": "2026-06-13 live dry-run moved in the wrong XY/Z direction; nominal MuJoCo Jacobian/frame mapping is quarantined.",
        "stop_reason": run_summary.get("stop_reason"),
        "bridge_samples": run_summary.get("bridge_writes"),
        "rtde_rate_hz": run_summary.get("rtde_output_timing", {}).get("rate_hz"),
        "qdot_limit_rad_s": args.get("step5c_qdot_limit_rad_s"),
        "path_cap_m_s": args.get("step4e_motion_limit_m_s"),
        "total_linear_cap_m_s": args.get("step4e_total_linear_limit_m_s"),
        "normal_velocity_cap_m_s": args.get("step4e_normal_velocity_limit_m_s"),
        "attitude_cap_rad_s": args.get("step4e_angular_limit_rad_s"),
    }


def write_plots(output_dir: Path, residuals: pd.DataFrame) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    paths: list[str] = []
    time = residuals["t_monotonic_s"].to_numpy(dtype=float)
    time = time - np.nanmin(time)

    fig, ax = plt.subplots(figsize=(8, 4))
    for idx, label in enumerate(("x", "y", "z")):
        ax.plot(time, residuals[f"inferred_tcp_offset_tool0_{idx}_m"].to_numpy(dtype=float), label=label)
    ax.set_xlabel("run time (s)")
    ax.set_ylabel("inferred TCP offset in tool0 (m)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    path = output_dir / "fk_inferred_tcp_offset_tool0.png"
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    paths.append(str(path))

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(time, residuals["linear_residual_norm_m_s"].to_numpy(dtype=float), label="linear")
    ax.plot(time, residuals["angular_residual_norm_rad_s"].to_numpy(dtype=float), label="angular")
    ax.set_xlabel("run time (s)")
    ax.set_ylabel("speed residual norm")
    ax.legend()
    ax.grid(True, alpha=0.3)
    path = output_dir / "jacobian_tcp_speed_residual_norms.png"
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    paths.append(str(path))
    return paths


def audit(
    bridge_csv: Path = DEFAULT_BRIDGE_CSV,
    calibration_yaml: Path = DEFAULT_CALIBRATION_YAML,
    xacro_path: Path = DEFAULT_XACRO_PATH,
    output_dir: Path | None = None,
    *,
    make_plots: bool = True,
) -> dict[str, Any]:
    if not bridge_csv.exists():
        raise FileNotFoundError(f"bridge CSV not found: {bridge_csv}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = output_dir or EXPERIMENT_ROOT / "runs" / f"step5c_calibrated_kinematics_audit_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    model_bundle = build_calibrated_model(calibration_yaml, xacro_path)
    rows = finite_run_rows(bridge_csv)
    tcp_offset = infer_tcp_offset(model_bundle, rows)
    residuals_df, speed_metrics = compute_residuals(model_bundle, rows, tcp_offset["mean"])

    residuals_path = out_dir / "step5c_calibrated_kinematics_residuals.csv"
    residuals_df.to_csv(residuals_path, index=False, quoting=csv.QUOTE_MINIMAL)
    urdf_path = out_dir / "calibrated_ur10e.urdf"
    urdf_path.write_text(model_bundle.urdf_text, encoding="utf-8")
    plot_paths = write_plots(out_dir, residuals_df) if make_plots else []

    gates = {
        "calibration_hash_expected": EXPECTED_CALIBRATION_HASH,
        "calibration_hash_match": model_bundle.calibration_hash == EXPECTED_CALIBRATION_HASH,
        "model_shape_pass": model_bundle.model.nq == 6 and model_bundle.model.nv == 6,
        "required_frames_pass": all(model_bundle.model.existFrame(name) for name in REQUIRED_FRAMES),
        "tcp_offset_norm_pass": OFFSET_NORM_MIN_M <= tcp_offset["norm_m"] <= OFFSET_NORM_MAX_M,
        "fk_offset_stability_pass": tcp_offset["max_abs_std_m"] < OFFSET_STD_LIMIT_M,
        "tcp_rotation_alignment_pass": tcp_offset["rotation_error_max_rad"] < ROTATION_ERROR_LIMIT_RAD,
        "jacobian_linear_speed_pass": speed_metrics["linear_vector_rms_m_s"] < SPEED_VECTOR_RMS_LIMIT,
        "jacobian_angular_speed_pass": speed_metrics["angular_vector_rms_rad_s"] < SPEED_VECTOR_RMS_LIMIT,
    }
    overall_pass = all(bool(value) for key, value in gates.items() if key.endswith("_pass") or key.endswith("_match"))
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "overall_pass": overall_pass,
        "artifact_dir": str(out_dir),
        "inputs": {
            "bridge_csv": str(bridge_csv),
            "calibration_yaml": str(calibration_yaml),
            "xacro_path": str(xacro_path),
        },
        "outputs": {
            "summary_json": str(out_dir / "step5c_calibrated_kinematics_summary.json"),
            "residuals_csv": str(residuals_path),
            "calibrated_urdf": str(urdf_path),
            "plots": plot_paths,
        },
        "calibrated_model": {
            "calibration_hash": model_bundle.calibration_hash,
            "nq": model_bundle.model.nq,
            "nv": model_bundle.model.nv,
            "required_frames": list(REQUIRED_FRAMES),
        },
        "inferred_tcp_offset_tool0": {
            "mean_xyz_m": [float(v) for v in tcp_offset["mean"]],
            "std_xyz_m": [float(v) for v in tcp_offset["std"]],
            "norm_m": tcp_offset["norm_m"],
            "max_deviation_norm_m": tcp_offset["max_deviation_norm_m"],
            "rotation_error_mean_rad": tcp_offset["rotation_error_mean_rad"],
            "rotation_error_max_rad": tcp_offset["rotation_error_max_rad"],
        },
        "jacobian_tcp_speed_residual": speed_metrics,
        "old_mujoco_nominal_failure": old_mujoco_failure_summary(DEFAULT_METADATA_PATH, DEFAULT_SUMMARY_PATH),
        "gates": gates,
        "thresholds": {
            "tcp_offset_norm_min_m": OFFSET_NORM_MIN_M,
            "tcp_offset_norm_max_m": OFFSET_NORM_MAX_M,
            "fk_offset_std_limit_m": OFFSET_STD_LIMIT_M,
            "tcp_rotation_error_limit_rad": ROTATION_ERROR_LIMIT_RAD,
            "speed_vector_rms_limit": SPEED_VECTOR_RMS_LIMIT,
        },
        "safety_boundary": [
            "offline analysis only",
            "no controller upload",
            "no bridge start",
            "no TP play",
            "no robot motion",
        ],
    }
    summary_path = out_dir / "step5c_calibrated_kinematics_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-csv", type=Path, default=DEFAULT_BRIDGE_CSV)
    parser.add_argument("--calibration-yaml", type=Path, default=DEFAULT_CALIBRATION_YAML)
    parser.add_argument("--xacro-path", type=Path, default=DEFAULT_XACRO_PATH)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--skip-plots", action="store_true")
    args = parser.parse_args()
    summary = audit(
        bridge_csv=args.bridge_csv,
        calibration_yaml=args.calibration_yaml,
        xacro_path=args.xacro_path,
        output_dir=args.output_dir,
        make_plots=not args.skip_plots,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["overall_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
