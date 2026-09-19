"""Measured robot/sensor observations -> yield-recovery command, without transport.

The physical owner must supply fresh host-monotonic receive timestamps.  This
module neither reads devices nor publishes commands.  It reuses the mature
tool/kinematics/freshness/raw-force admission and drives YieldController
through the whole baseline/entry/pause/PATH lifecycle.
"""
from __future__ import annotations

import math
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from contact_benchmark_kernel import KernelDeadlineError
from contact_benchmark_outer import finite
from contact_benchmark_protocol import ENTRY_DURATION_S, SensorFreshnessTracker
from contact_benchmark_runtime import validate_measured_observation
from contact_qp import QpSolverProfile
from contact_yield_controller import YieldController, YieldControllerError, YieldSettings
from contact_yield_math import so3_log, transported_roll_anchor, require_rotation
from contact_yield_protocol import CLAIM_SCOPE, law_seed_parameters
from step5c_calibrated_kinematics_audit import build_calibrated_model, rotvec_to_matrix
from step5d_autotune_v4_r004.calibrated_runtime import tcp_jacobian_base


def _geometric_latency(exc: BaseException) -> bool:
    message = str(exc)
    return (
        "geometric latency" in message
        or message == "latency uncertainty exceeds safety tightening"
    )


class YieldContactRuntime:
    def __init__(
        self,
        *,
        method: str,
        qp_library: Path | str,
        anchor_m,
        task_basis,
        approach_inward_base,
        deadline_s: float | None = 0.0015,
        settings: YieldSettings | None = None,
        law_parameters: Mapping[str, Any] | None = None,
        dt_s: float = 0.002,
        build_root: Path | str | None = None,
    ) -> None:
        if deadline_s is not None and (not math.isfinite(deadline_s) or deadline_s <= 0):
            raise ValueError("runtime deadline must be positive")
        self.deadline_s = deadline_s
        self.solver_profile = QpSolverProfile(qp_library)
        self.model = build_calibrated_model()
        self.anchor = finite(anchor_m, (3,), "contact anchor")
        self.basis = require_rotation(task_basis, "task basis").copy()
        if abs(float(np.linalg.det(self.basis)) - 1.0) > 1e-6:
            raise ValueError("task basis must be a right-handed rotation")
        self.controller = YieldController(
            method=method,
            qp_library=qp_library,
            approach_inward_base=approach_inward_base,
            settings=settings or YieldSettings(),
            law_parameters=law_parameters or law_seed_parameters(method),
            dt_s=dt_s,
            qp_deadline_s=0.001 if deadline_s is not None else None,
            build_root=build_root,
            allow_pre_path_force_ramp=True,
        )
        self.identity = hashlib.sha256(json.dumps({
            "schema": "yield-contact-runtime-v1",
            "controller": self.controller.identity,
            "anchor_m": self.anchor.tolist(), "basis": self.basis.tolist(),
            "calibration": self.model.calibration_hash,
            "entry_duration_s": ENTRY_DURATION_S,
            "entry_boundary_policy": "continuous_sample_hold_v2",
        }, sort_keys=True).encode()).hexdigest()
        self.last_sample_s = None
        self.last_controller_timestamp = None
        self.last_sensor_timestamp = None
        self.paused_s = 0.0
        self.phase = None
        self.last_entry_time = None
        self.freshness = SensorFreshnessTracker()

    def freshness_summary(self):
        return self.freshness.as_dict()

    def snapshot(self):
        return {
            "identity": self.identity,
            "controller": self.controller.snapshot(),
            "last_sample_s": self.last_sample_s,
            "last_controller_timestamp": self.last_controller_timestamp,
            "last_sensor_timestamp": self.last_sensor_timestamp,
            "paused_s": self.paused_s,
            "phase": self.phase,
            "last_entry_time_s": self.last_entry_time,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        if state.get("identity") != self.identity:
            raise ValueError("runtime snapshot identity differs")
        self.controller.restore(state["controller"])
        self.last_sample_s = state["last_sample_s"]
        self.last_controller_timestamp = state["last_controller_timestamp"]
        self.last_sensor_timestamp = state["last_sensor_timestamp"]
        self.paused_s = state["paused_s"]
        self.phase = state["phase"]
        self.last_entry_time = state["last_entry_time_s"]

    def close(self) -> None:
        self.controller.close()

    def __enter__(self) -> "YieldContactRuntime":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _validate_observation(self, robot, wrench_tcp, sensor_observed_at_s, sample_time_s):
        return validate_measured_observation(
            robot=robot,
            wrench_tcp=wrench_tcp,
            sensor_observed_at_s=sensor_observed_at_s,
            sample_time_s=sample_time_s,
            last_sample_s=self.last_sample_s,
            last_controller_timestamp=self.last_controller_timestamp,
            last_sensor_timestamp=self.last_sensor_timestamp,
            freshness=self.freshness,
            model=self.model,
        )

    def _commit_clocks(self, obs) -> None:
        self.last_sample_s = obs["now"]
        self.last_controller_timestamp = obs["controller_t"]
        self.last_sensor_timestamp = obs["sensor_t"]

    def orientation_error_rad(self, rotation) -> tuple[float, float, float]:
        """SO(3) log vs the evolving estimated-normal/roll reference."""
        matrix = np.asarray(rotation, dtype=float)
        roll = self.controller.roll_anchor
        if roll is None:
            return (0.0, 0.0, 0.0)
        desired = transported_roll_anchor(roll, self.controller.estimator.normal)
        return tuple(float(value) for value in so3_log(desired @ matrix.T))

    def path_error_task_m(self, position_m, path_time_s) -> np.ndarray:
        local = self.basis.T @ (finite(position_m, (3,), "TCP position") - self.anchor)
        ref = np.asarray(self.controller.task.reference(path_time_s)["position_m"], dtype=float)
        return local - ref

    def pause(self, *, robot, wrench_tcp, sensor_observed_at_s, sample_time_s, reason):
        """Freeze complete controller memory only during the observed pre-PATH seam."""
        started = time.perf_counter()
        obs = self._validate_observation(robot, wrench_tcp, sensor_observed_at_s, sample_time_s)
        if self.phase == "path" or self.controller.last_path_time_s is not None:
            raise ValueError("cannot pause an active PATH controller")
        speed = finite(robot["actual_TCP_speed"], (6,), "TCP speed")
        if np.linalg.norm(speed[:3]) > 0.0005 or np.linalg.norm(speed[3:]) > 0.005 or np.max(np.abs(obs["qd"])) > 0.001:
            raise ValueError("freeze-carry seam requires stationary robot")
        if not isinstance(reason, str) or not reason:
            raise ValueError("pause reason required")
        before = self.snapshot()
        try:
            self.controller.hold_pre_path_clock(time_s=obs["now"], dt_s=obs["dt"])
            elapsed = time.perf_counter() - started
            if self.deadline_s is not None and elapsed > self.deadline_s:
                raise KernelDeadlineError("pause observation deadline exceeded")
        except Exception:
            self.restore(before)
            raise
        self._commit_clocks(obs)
        self.paused_s += obs["dt"]
        return {
            "policy": "pre_path_stationary_freeze_carry",
            "reason": reason,
            "paused_s": self.paused_s,
            "sample_time_s": obs["now"],
            "actual_dt_s": obs["dt"],
            "actual_tcp_speed_m_s_rad_s": tuple(float(value) for value in speed),
            "observation_age_s": obs["age"],
            "age_band": obs["age_band"],
            "freshness": self.freshness_summary(),
        }

    def _reference(self, *, phase, path_time_s, entry_time_s, force_reference_n):
        if phase not in ("baseline", "entry", "path"):
            raise ValueError("invalid phase transition; no return from path to baseline")
        if self.phase == "entry" and phase == "baseline":
            raise ValueError("entry cannot return to baseline")
        if phase == "path" and self.phase not in ("entry", "path"):
            raise ValueError("formal PATH requires completed entry")
        if self.phase == "path" and phase != "path":
            raise ValueError("invalid phase transition; no return from path to baseline")
        if phase == "entry" and self.phase not in ("baseline", "entry"):
            raise ValueError("entry requires baseline phase")
        target_force = self.controller.settings.target_force_n if force_reference_n is None else float(force_reference_n)
        if not math.isfinite(target_force):
            raise ValueError("contact reference must be finite")
        if phase == "path":
            if entry_time_s is not None:
                raise ValueError("PATH has no entry clock")
            if abs(target_force - self.controller.settings.target_force_n) > 1e-12:
                raise ValueError("PATH requires 5 N reference")
            if path_time_s is None:
                raise ValueError("formal PATH clock is required")
            path_t = float(path_time_s)
            if self.phase != "path" and path_t < 0:
                raise ValueError("formal PATH clock must be nonnegative")
            reference = self.controller.task.reference(path_t)
            entry_t = self.last_entry_time
        elif phase == "entry":
            if path_time_s is not None and entry_time_s is not None:
                if not math.isclose(path_time_s, entry_time_s, rel_tol=0.0, abs_tol=1e-12):
                    raise ValueError("entry clocks disagree")
            entry_t = entry_time_s if entry_time_s is not None else path_time_s
            if entry_t is None:
                raise ValueError("entry clock is required")
            if not math.isfinite(entry_t) or not 0 <= entry_t < ENTRY_DURATION_S:
                raise ValueError("entry clock outside one-second entry")
            if self.phase == "baseline" and entry_t > 1e-10:
                raise ValueError("entry clock must start at zero")
            if self.last_entry_time is not None and entry_t <= self.last_entry_time:
                raise ValueError("entry clock must advance")
            path_t = None
            reference = self.controller.task.entry_reference(entry_t)
        else:
            if path_time_s is not None or entry_time_s is not None:
                raise ValueError("baseline has no path clock")
            path_t = None
            entry_t = None
            reference = {"position_m": (0.0, 0.0, 0.0), "velocity_m_s": (0.0, 0.0, 0.0)}
        return {
            "phase": phase,
            "path_time_s": path_t,
            "entry_time_s": entry_t,
            "force_n": target_force,
            "position_m": tuple(self.anchor + self.basis @ np.asarray(reference["position_m"], dtype=float)),
            "velocity_m_s": tuple(self.basis @ np.asarray(reference["velocity_m_s"], dtype=float)),
        }

    def step(
        self,
        *,
        robot,
        wrench_tcp,
        sensor_observed_at_s,
        sample_time_s,
        phase,
        path_time_s=None,
        entry_time_s=None,
        force_reference_n=None,
        injection_task_n=(0.0, 0.0, 0.0),
    ):
        """Consume an owner-supplied RTDE row and untampered, baseline-subtracted wrench.

        robot uses RTDE field names plus observed_at_s (host monotonic receive
        time). wrench_tcp is environment-on-tool, before filtering/injection.
        """
        started = time.perf_counter()
        obs = self._validate_observation(robot, wrench_tcp, sensor_observed_at_s, sample_time_s)
        now, age, dt, tcp, q, pose, wrench, lower, upper = (
            obs[key] for key in ("now", "age", "dt", "tcp", "q", "pose", "wrench", "lower", "upper")
        )
        speed = finite(robot["actual_TCP_speed"], (6,), "TCP speed")
        injection = finite(injection_task_n, (3,), "software disturbance")
        if phase != "path" and np.any(injection):
            raise ValueError("disturbance requires formal PATH phase")
        rotation = rotvec_to_matrix(pose[3:])
        jacobian = tcp_jacobian_base(self.model, q, tcp[:3])
        if phase == "path" and self.phase == "entry":
            # The last entry sample is normally at 0.998 s. Its 2 ms command
            # hold completes the one-second entry; do not add a duplicate
            # endpoint command at 1.000 s before PATH time zero.
            if (self.last_entry_time is None or path_time_s is None
                or not 0 <= float(path_time_s) < dt + 1e-10
                or not math.isclose(self.last_entry_time + dt,
                    ENTRY_DURATION_S + float(path_time_s), rel_tol=0.0, abs_tol=1e-10)):
                raise ValueError("formal PATH requires completed entry with continuous clock")
        if phase == "entry" and self.phase == "entry":
            entry_clock = entry_time_s if entry_time_s is not None else path_time_s
            if entry_clock is None or not math.isclose(
                float(entry_clock) - self.last_entry_time, dt, rel_tol=0.0, abs_tol=1e-10
            ):
                raise ValueError("entry clock must advance by actual dt")
        reference = self._reference(
            phase=phase,
            path_time_s=path_time_s,
            entry_time_s=entry_time_s,
            force_reference_n=force_reference_n,
        )
        observation = {
            "time_s": now,
            "state_age_s": age,
            "position_m": tuple(pose[:3]),
            "rotation": rotation,
            "raw_force_base_n": tuple(rotation @ wrench[:3]),
            "raw_torque_base_nm": tuple(rotation @ wrench[3:]),
            "software_injection_base_n": tuple(self.basis @ injection),
            "jacobian": jacobian,
            "joint_velocity_lower": lower,
            "joint_velocity_upper": upper,
            "linear_velocity_base_m_s": tuple(speed[:3]),
        }
        before = self.snapshot()
        try:
            result = self.controller.step(observation, reference, dt)
            elapsed = time.perf_counter() - started
            if self.deadline_s is not None and elapsed > self.deadline_s:
                raise KernelDeadlineError(f"observation-to-command deadline exceeded: {elapsed:.6f}s")
        except Exception as exc:
            if _geometric_latency(exc):
                self.freshness.geometric_latency_reject()
            self.restore(before)
            raise
        self._commit_clocks(obs)
        self.phase = phase
        if phase == "entry":
            self.last_entry_time = reference["entry_time_s"]
        path_error_task = self.basis.T @ np.asarray(result["path_error_base_m"], dtype=float)
        return {
            **result,
            "pre_path_paused_s": self.paused_s,
            "runtime_wall_s": elapsed,
            "actual_dt_s": dt,
            "actual_tcp_speed_m_s_rad_s": tuple(float(value) for value in speed),
            "observation_age_s": age,
            "age_band": obs["age_band"],
            "freshness": self.freshness_summary(),
            "raw_wrench_tcp": tuple(float(value) for value in wrench),
            "jacobian_6x6": tuple(tuple(float(value) for value in row) for row in jacobian),
            "jacobian_calibration_hash": self.model.calibration_hash,
            "path_error_task_m": tuple(float(value) for value in path_error_task),
            "entry_time_s": reference["entry_time_s"],
            "formal_time_s": reference["path_time_s"] if phase == "path" else None,
            "force_identity_n": float(self.controller.settings.target_force_n),
            "claim_scope": CLAIM_SCOPE,
        }
