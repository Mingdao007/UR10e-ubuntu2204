#!/usr/bin/env python3
"""Offline full-chain sanity for Step5d strict TASE RNN.

This script proves the local data path only:

    recorded pose/q -> paper outer loop xdot_c -> calibrated J(q)
    -> strict RNN qdot -> registers 37..47

It is not a live-contact gate and does not open the bridge or controller.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pinocchio as pin

import step5c_calibrated_kinematics_audit as kin
from kunwei_rtde_bridge import step5c_joint_register_values
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rnn_target_state_from_outer_loop,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE_CSV = kin.DEFAULT_BRIDGE_CSV
DEFAULT_ALPHA_S_INV = 1.0
DEFAULT_QDOT_LIMIT_RAD_S = 0.30
DEFAULT_SAMPLE_LIMIT = 400
SYNTHETIC_FORCE_TCP_N = (0.0, 0.0, 1.0)
STEP5_STEP6_FORCE_SIGN_CONVENTION = "step5_step6_positive_normal_load"


@dataclass(frozen=True)
class TcpJacobian:
    matrix: np.ndarray
    tcp_lever_base: np.ndarray


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def finite_array(values: Any, shape: tuple[int, ...], name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.shape != shape or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be finite shape {shape}")
    return array


def skew(vector: np.ndarray) -> np.ndarray:
    x, y, z = vector
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=float)


def tcp_jacobian_base(
    model_bundle: kin.CalibratedModel,
    q: np.ndarray,
    tcp_offset_tool0: np.ndarray,
) -> TcpJacobian:
    model = model_bundle.model
    data = model_bundle.data
    pin.forwardKinematics(model, data, q)
    pin.computeJointJacobians(model, data, q)
    pin.updateFramePlacements(model, data)
    tool_jac_world_aligned = pin.getFrameJacobian(
        model,
        data,
        model_bundle.tool0_frame_id,
        pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
    )
    base_world_rotation = data.oMf[model_bundle.base_frame_id].rotation.T
    tool_linear_base_jac = base_world_rotation @ tool_jac_world_aligned[:3, :]
    tool_angular_base_jac = base_world_rotation @ tool_jac_world_aligned[3:, :]
    tool0_base = data.oMf[model_bundle.base_frame_id].inverse() * data.oMf[model_bundle.tool0_frame_id]
    tcp_lever_base = tool0_base.rotation @ tcp_offset_tool0
    tcp_linear_base_jac = tool_linear_base_jac - skew(tcp_lever_base) @ tool_angular_base_jac
    return TcpJacobian(np.vstack((tcp_linear_base_jac, tool_angular_base_jac)), tcp_lever_base)


def omega_bounds(
    q: np.ndarray,
    q_min: np.ndarray,
    q_max: np.ndarray,
    *,
    alpha_s_inv: float,
    qdot_limit_rad_s: float,
) -> tuple[np.ndarray, np.ndarray]:
    lower = np.maximum(alpha_s_inv * (q_min - q), -qdot_limit_rad_s)
    upper = np.minimum(qdot_limit_rad_s, alpha_s_inv * (q_max - q))
    if np.any(lower > upper):
        raise ValueError("omega bounds are inverted")
    return lower, upper


def verified_truth_file() -> Path:
    payload = {
        "strict_rnn_enabled": True,
        "pending_pdf_verify": [],
        "sections": {},
        "offline_sanity_only": True,
    }
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False)
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)


def load_rows(csv_path: Path, *, sample_limit: int) -> pd.DataFrame:
    required = (
        [f"ur_actual_q_{idx}" for idx in range(6)]
        + [f"ur_actual_qd_{idx}" for idx in range(6)]
        + [f"ur_actual_TCP_pose_{idx}" for idx in range(6)]
        + [f"ur_actual_TCP_speed_{idx}" for idx in range(6)]
        + ["_step4e_desired_x_m", "_step4e_desired_y_m", "_step4e_desired_vx_m_s", "_step4e_desired_vy_m_s"]
    )
    df = pd.read_csv(csv_path)
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise RuntimeError(f"bridge CSV missing required full-chain columns: {missing}")
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=required).copy()
    if "step4e_cmd_valid" in df.columns:
        df = df[df["step4e_cmd_valid"].fillna(0.0).astype(float) > 0.5]
    if df.empty:
        raise RuntimeError("bridge CSV has no usable rows")
    if sample_limit > 0 and len(df) > sample_limit:
        indexes = np.linspace(0, len(df) - 1, sample_limit).round().astype(int)
        df = df.iloc[indexes].copy()
    return df.reset_index(drop=True)


def row_vector(row: pd.Series, prefix: str, length: int) -> np.ndarray:
    return np.array([row[f"{prefix}_{idx}"] for idx in range(length)], dtype=float)


def run_sanity(
    *,
    bridge_csv: Path = DEFAULT_BRIDGE_CSV,
    output_dir: Path | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
    alpha_s_inv: float = DEFAULT_ALPHA_S_INV,
    qdot_limit_rad_s: float = DEFAULT_QDOT_LIMIT_RAD_S,
    r: float = 0.2,
    epsilon: float = 0.022,
    dt_s: float = 0.002,
    use_recorded_position_error: bool = False,
) -> dict[str, Any]:
    if not bridge_csv.exists():
        raise FileNotFoundError(f"bridge CSV not found: {bridge_csv}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = output_dir or EXPERIMENT_ROOT / "runs" / f"step5d_numeric_sanity_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    model_bundle = kin.build_calibrated_model()
    audit_rows = kin.finite_run_rows(bridge_csv)
    tcp_offset = kin.infer_tcp_offset(model_bundle, audit_rows)["mean"]
    rows = load_rows(bridge_csv, sample_limit=sample_limit)
    truth_path = verified_truth_file()
    try:
        solver = StrictTaseRnnSolver(
            StrictRnnConfig(
                paper_truth_path=truth_path,
                qdot_limit_rad_s=qdot_limit_rad_s,
                epsilon=epsilon,
                sigr_exponent_r=r,
            )
        )
        outer_config = Step5dOuterLoopConfig(
            kp=4.0,
            ko=5.0,
            kf=1.0,
            Md_scalar=12.0,
            Bd_scalar=550.0,
            force_target_n=5.0,
            delay_T_s=dt_s,
            force_sign_convention=STEP5_STEP6_FORCE_SIGN_CONVENTION,
        )
        outer_state = Step5dOuterLoopState()
        records: list[dict[str, float]] = []
        q_min = model_bundle.model.lowerPositionLimit
        q_max = model_bundle.model.upperPositionLimit
        for idx, row in rows.iterrows():
            q = row_vector(row, "ur_actual_q", 6)
            qd = row_vector(row, "ur_actual_qd", 6)
            pose = row_vector(row, "ur_actual_TCP_pose", 6)
            speed = row_vector(row, "ur_actual_TCP_speed", 6)
            jac = tcp_jacobian_base(model_bundle, q, tcp_offset)
            lower, upper = omega_bounds(
                q,
                q_min,
                q_max,
                alpha_s_inv=alpha_s_inv,
                qdot_limit_rad_s=qdot_limit_rad_s,
            )
            if use_recorded_position_error:
                x_pd = (
                    float(row["_step4e_desired_x_m"]),
                    float(row["_step4e_desired_y_m"]),
                    float(pose[2]),
                )
            else:
                x_pd = (float(pose[0]), float(pose[1]), float(pose[2]))
            xdot_pd = (
                float(row["_step4e_desired_vx_m_s"]),
                float(row["_step4e_desired_vy_m_s"]),
                0.0,
            )
            outer_output = compute_step5d_outer_loop(
                outer_config,
                outer_state,
                Step5dOuterLoopInputs(
                    tcp_pose_base=tuple(float(v) for v in pose),  # type: ignore[arg-type]
                    tcp_speed_base=tuple(float(v) for v in speed),  # type: ignore[arg-type]
                    force_tcp_n=SYNTHETIC_FORCE_TCP_N,
                    x_pd_base=x_pd,
                    xdot_pd_base=xdot_pd,
                    dt_s=dt_s,
                    cmd_valid=True,
                ),
            )
            outer_state = outer_output.next_state
            target_state = rnn_target_state_from_outer_loop(
                outer_output,
                J=jac.matrix,
                omega_minus=lower,
                omega_plus=upper,
                dt_s=dt_s,
                epsilon=epsilon,
                r=r,
            )
            result = solver.solve(actual_q=q, actual_qd=qd, target_state=target_state)
            registers = step5c_joint_register_values(
                result.qdot,
                cmd_valid=1.0 if outer_output.cmd_valid else 0.0,
                path_time_s=float(row.get("_step4e_path_time_s", idx * dt_s)),
                force_error_n=float(outer_output.diagnostics.get("e_f", 0.0)),
                pose_or_orientation_error=float(np.linalg.norm(outer_output.diagnostics.get("e_o", (0.0, 0.0, 0.0)))),
                solver_status=result.solver_status,
            )
            qdot = np.asarray(result.qdot, dtype=float)
            residual = jac.matrix @ qdot - np.asarray(outer_output.xdot_c, dtype=float)
            record = {
                "sample_index": float(idx),
                "source_write_index": float(row.get("write_index", idx)),
                "cmd_valid": float(registers["step4e_cmd_valid"]),
                "path_time_s": float(registers["step4e_progress_m"]),
                "qdot_max_abs_rad_s": float(np.max(np.abs(qdot))),
                "constraint_residual_norm": float(np.linalg.norm(residual)),
                "outer_xdot_norm": float(np.linalg.norm(np.asarray(outer_output.xdot_c, dtype=float))),
                "active_bounds_count": float(sum(bool(v) for v in result.diagnostics["active_bounds_mask"])),
            }
            carrier_names = [
                "step4e_cmd_vx_m_s",
                "step4e_cmd_vy_m_s",
                "step4e_cmd_vz_m_s",
                "step4e_cmd_wx_rad_s",
                "step4e_cmd_wy_rad_s",
                "step4e_cmd_wz_rad_s",
            ]
            for joint_idx in range(6):
                record[f"qd{joint_idx}_rad_s"] = float(qdot[joint_idx])
                record[f"register_carrier_{37 + joint_idx}"] = float(registers[carrier_names[joint_idx]])
                record[f"xdot_c_{joint_idx}"] = float(outer_output.xdot_c[joint_idx])
                record[f"theta_dot_state_{joint_idx}"] = float(result.diagnostics["theta_dot_state"][joint_idx])
                record[f"lambda_state_{joint_idx}"] = float(result.diagnostics["lambda_state"][joint_idx])
                record[f"omega_minus_{joint_idx}"] = float(lower[joint_idx])
                record[f"omega_plus_{joint_idx}"] = float(upper[joint_idx])
            records.append(record)
    finally:
        truth_path.unlink(missing_ok=True)

    residuals_path = out_dir / "step5d_full_chain_residuals.csv"
    with residuals_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
    residual_norms = np.array([record["constraint_residual_norm"] for record in records], dtype=float)
    qdot_max = np.array([record["qdot_max_abs_rad_s"] for record in records], dtype=float)
    xdot_norms = np.array([record["outer_xdot_norm"] for record in records], dtype=float)
    register_order_pass = True
    for record in records:
        for joint_idx in range(6):
            if not math.isclose(record[f"qd{joint_idx}_rad_s"], record[f"register_carrier_{37 + joint_idx}"], rel_tol=0.0, abs_tol=1e-12):
                register_order_pass = False
    gates = {
        "finite_outputs_pass": bool(np.all(np.isfinite(residual_norms)) and np.all(np.isfinite(qdot_max))),
        "qdot_within_nominal_limit_pass": bool(float(np.max(qdot_max)) <= qdot_limit_rad_s + 1e-9),
        "register_order_pass": register_order_pass,
        "nonzero_outer_xdot_seen": bool(float(np.max(xdot_norms)) > 0.0),
    }
    overall_pass = all(gates.values())
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "overall_pass": overall_pass,
        "artifact_dir": str(out_dir),
        "inputs": {
            "bridge_csv": str(bridge_csv),
            "calibration_yaml": str(kin.DEFAULT_CALIBRATION_YAML),
            "calibration_hash": model_bundle.calibration_hash,
            "sample_limit": sample_limit,
        },
        "outputs": {
            "summary_json": str(out_dir / "step5d_numeric_sanity.json"),
            "residuals_csv": str(residuals_path),
        },
        "assumptions": {
            "alpha_s_inv": alpha_s_inv,
            "qdot_limit_rad_s": qdot_limit_rad_s,
            "epsilon": epsilon,
            "sigr_exponent_r": r,
            "T_s": dt_s,
            "force_input": "synthetic_no_contact_unit_normal",
            "force_target_n": 5.0,
            "force_sign_convention": STEP5_STEP6_FORCE_SIGN_CONVENTION,
            "force_sign_evidence": "retained Step5/Step6 bridge convention: target_force_n=5.0, normal_axis=fz, normal_sign=1.0, step4e_normal_command_sign=1.0",
            "contact_evidence": "not_claimed",
            "position_error_mode": "recorded" if use_recorded_position_error else "zeroed_for_structural_sanity",
        },
        "metrics": {
            "samples": len(records),
            "qdot_max_abs_rad_s": float(np.max(qdot_max)),
            "qdot_within_nominal_limit": bool(float(np.max(qdot_max)) <= qdot_limit_rad_s + 1e-9),
            "constraint_residual_norm_max": float(np.max(residual_norms)),
            "constraint_residual_norm_rms": float(np.sqrt(np.mean(residual_norms**2))),
            "outer_xdot_norm_max": float(np.max(xdot_norms)),
        },
        "gates": gates,
        "safety_boundary": [
            "offline analysis only",
            "no bridge start",
            "no controller upload",
            "no TP play",
            "no robot motion",
        ],
    }
    write_json(out_dir / "step5d_numeric_sanity.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-csv", type=Path, default=DEFAULT_BRIDGE_CSV)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--sample-limit", type=int, default=DEFAULT_SAMPLE_LIMIT)
    parser.add_argument("--alpha-s-inv", type=float, default=DEFAULT_ALPHA_S_INV)
    parser.add_argument("--qdot-limit-rad-s", type=float, default=DEFAULT_QDOT_LIMIT_RAD_S)
    parser.add_argument("--r", type=float, default=0.2)
    parser.add_argument("--epsilon", type=float, default=0.022)
    parser.add_argument("--dt-s", type=float, default=0.002)
    parser.add_argument("--use-recorded-position-error", action="store_true")
    args = parser.parse_args()
    summary = run_sanity(
        bridge_csv=args.bridge_csv,
        output_dir=args.output_dir,
        sample_limit=args.sample_limit,
        alpha_s_inv=args.alpha_s_inv,
        qdot_limit_rad_s=args.qdot_limit_rad_s,
        r=args.r,
        epsilon=args.epsilon,
        dt_s=args.dt_s,
        use_recorded_position_error=args.use_recorded_position_error,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["overall_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
