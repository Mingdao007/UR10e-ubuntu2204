#!/usr/bin/env python3
"""Step5c diagnostic DLS joint-space command solver and numeric sanity gate."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from step5_table import cycloid_reference_local, load_step5_table, step5_stage


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_PATH = (
    EXPERIMENT_ROOT.parent
    / "archive"
    / "legacy"
    / "tase-mujoco-reproduction-2026-05-23"
    / "assets"
    / "mjcf"
    / "ur10e_nominal.xml"
)
DEFAULT_SITE_NAME = "tcp_site_unverified_85mm"
DRYRUN_STAGE_ID = "step5c_speedj_dryrun_v1"
CONTACT_STAGE_ID = "step5c_joint_rnn_cycloid_v1"
STATUS_OK = 40.0
STATUS_CLIPPED = 41.0
STATUS_PROJECTED = 42.0
STATUS_INVALID = 90.0


@dataclass(frozen=True)
class JointSolverConfig:
    model_path: Path = DEFAULT_MODEL_PATH
    site_name: str = DEFAULT_SITE_NAME
    qdot_limit_rad_s: float = 0.15
    damping: float = 1e-4
    joint_limit_margin_rad: float = 0.02
    translational_weight: float = 1.0
    rotational_weight: float = 0.35


@dataclass(frozen=True)
class JointCommandResult:
    qdot: tuple[float, float, float, float, float, float]
    solver_status: float
    max_abs_qdot_rad_s: float
    clipped: bool
    projected: bool
    residual_norm: float


def _finite_vector(values: Any, length: int, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float).reshape(-1)
    if array.shape != (length,) or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be a finite length-{length} vector")
    return array


class Step5cDlsJointSolver:
    """Bounded least-squares qdot solver used only for Step5c diagnostics."""

    def __init__(self, config: JointSolverConfig = JointSolverConfig()) -> None:
        import mujoco

        self.config = config
        self._mujoco = mujoco
        self.model = mujoco.MjModel.from_xml_path(str(config.model_path))
        self.data = mujoco.MjData(self.model)
        self.site_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SITE, config.site_name)
        if self.site_id < 0:
            raise ValueError(f"site not found in MuJoCo model: {config.site_name}")
        if self.model.nq < 6 or self.model.nv < 6:
            raise ValueError(f"model must expose at least 6 qpos/qvel; got nq={self.model.nq} nv={self.model.nv}")

    def solve(self, actual_q: Any, target_twist_base: Any) -> JointCommandResult:
        q = _finite_vector(actual_q, 6, "actual_q")
        twist = _finite_vector(target_twist_base, 6, "target_twist_base")
        limit = float(self.config.qdot_limit_rad_s)
        if not math.isfinite(limit) or limit <= 0.0:
            raise ValueError("qdot_limit_rad_s must be positive")
        damping = float(self.config.damping)
        if not math.isfinite(damping) or damping < 0.0:
            raise ValueError("damping must be finite and non-negative")

        self.data.qpos[:6] = q
        self._mujoco.mj_forward(self.model, self.data)
        jacp = np.zeros((3, self.model.nv), dtype=float)
        jacr = np.zeros((3, self.model.nv), dtype=float)
        self._mujoco.mj_jacSite(
            self.model, self.data, jacp, jacr, self.site_id
        )
        jac = np.vstack([jacp[:, :6], jacr[:, :6]])
        weights = np.diag(
            [
                self.config.translational_weight,
                self.config.translational_weight,
                self.config.translational_weight,
                self.config.rotational_weight,
                self.config.rotational_weight,
                self.config.rotational_weight,
            ]
        )
        weighted_jac = weights @ jac
        weighted_twist = weights @ twist
        lhs = weighted_jac.T @ weighted_jac + damping * np.eye(6)
        rhs = weighted_jac.T @ weighted_twist
        try:
            qdot = np.linalg.solve(lhs, rhs)
        except np.linalg.LinAlgError:
            qdot = np.linalg.lstsq(lhs, rhs, rcond=None)[0]

        if not np.all(np.isfinite(qdot)):
            raise ValueError("joint solver produced non-finite qdot")

        projected = False
        margin = max(0.0, float(self.config.joint_limit_margin_rad))
        for idx in range(6):
            qpos_addr = self.model.jnt_qposadr[idx]
            if not bool(self.model.jnt_limited[idx]):
                continue
            lo, hi = self.model.jnt_range[idx]
            q_here = self.data.qpos[qpos_addr]
            if q_here <= lo + margin and qdot[idx] < 0.0:
                qdot[idx] = 0.0
                projected = True
            elif q_here >= hi - margin and qdot[idx] > 0.0:
                qdot[idx] = 0.0
                projected = True

        clipped = bool(np.max(np.abs(qdot)) > limit)
        qdot = np.clip(qdot, -limit, limit)
        residual = jac @ qdot - twist
        status = STATUS_PROJECTED if projected else STATUS_CLIPPED if clipped else STATUS_OK
        return JointCommandResult(
            qdot=tuple(float(v) for v in qdot),
            solver_status=status,
            max_abs_qdot_rad_s=float(np.max(np.abs(qdot))),
            clipped=clipped,
            projected=projected,
            residual_norm=float(np.linalg.norm(residual)),
        )


def solve_joint_velocity(
    actual_q: Any,
    target_twist_base: Any,
    *,
    qdot_limit_rad_s: float = 0.15,
    model_path: Path = DEFAULT_MODEL_PATH,
    site_name: str = DEFAULT_SITE_NAME,
    damping: float = 1e-4,
) -> JointCommandResult:
    solver = Step5cDlsJointSolver(
        JointSolverConfig(
            model_path=model_path,
            site_name=site_name,
            qdot_limit_rad_s=qdot_limit_rad_s,
            damping=damping,
        )
    )
    return solver.solve(actual_q, target_twist_base)


def _sample_twists(stage_id: str, *, path_cap: float, total_cap: float, attitude_cap: float) -> list[tuple[float, ...]]:
    table = load_step5_table()
    stage = step5_stage(stage_id, table)
    duration_s = float(stage["duration_s"])
    samples: list[tuple[float, ...]] = []
    for idx in range(0, int(duration_s * 10) + 1):
        ref = cycloid_reference_local(stage, idx * 0.1)
        vx = max(-path_cap, min(path_cap, float(ref["local_vx_m_s"])))
        vy = max(-path_cap, min(path_cap, float(ref["local_vy_m_s"])))
        linear_norm = math.hypot(vx, vy)
        if linear_norm > total_cap:
            scale = total_cap / linear_norm
            vx *= scale
            vy *= scale
        samples.append((vx, vy, 0.0, attitude_cap, 0.0, 0.0))
        samples.append((vx, vy, 0.0, 0.0, attitude_cap, 0.0))
        samples.append((vx, vy, 0.0, 0.0, 0.0, 0.0))
    return samples


def numeric_sanity(output_root: Path | None = None) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = output_root or EXPERIMENT_ROOT / "runs" / f"step5c_numeric_sanity_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    representative_q = (-1.57, -1.20, 1.80, -2.10, -1.57, 0.0)
    cases = [
        {
            "name": DRYRUN_STAGE_ID,
            "qdot_limit_rad_s": float(step5_stage(DRYRUN_STAGE_ID)["guard"]["qdot_cap_rad_s"]),
            "path_cap_m_s": 0.004,
            "total_linear_cap_m_s": 0.004,
            "normal_velocity_cap_m_s": 0.0,
            "attitude_cap_rad_s": 0.0,
        },
    ]
    reports = []
    overall_pass = True
    for case in cases:
        solver = Step5cDlsJointSolver(
            JointSolverConfig(
                qdot_limit_rad_s=case["qdot_limit_rad_s"],
                damping=1e-4,
            )
        )
        results = [
            solver.solve(representative_q, twist)
            for twist in _sample_twists(
                case["name"],
                path_cap=case["path_cap_m_s"],
                total_cap=case["total_linear_cap_m_s"],
                attitude_cap=case["attitude_cap_rad_s"],
            )
        ]
        max_qdot = max(result.max_abs_qdot_rad_s for result in results)
        projected = any(result.projected for result in results)
        clipped = any(result.clipped for result in results)
        passed = max_qdot <= case["qdot_limit_rad_s"] + 1e-12 and not projected
        overall_pass = overall_pass and passed
        reports.append(
            {
                **case,
                "samples": len(results),
                "max_abs_qdot_rad_s": max_qdot,
                "projected": projected,
                "clipped": clipped,
                "passed": passed,
                "representative_q_rad": representative_q,
            }
        )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "artifact_dir": str(out_dir),
        "model_path": str(DEFAULT_MODEL_PATH),
        "site_name": DEFAULT_SITE_NAME,
        "overall_pass": overall_pass,
        "cases": reports,
    }
    (out_dir / "step5c_numeric_sanity.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--numeric-sanity", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    if not args.numeric_sanity:
        parser.error("currently only --numeric-sanity is supported")
    payload = numeric_sanity(args.output_dir)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["overall_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
