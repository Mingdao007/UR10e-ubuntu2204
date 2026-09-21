"""Transport-free yield-recovery adapter for the mature contact_command_provider contract.

The adapter returns CalibratedCommand through the existing sole writer seam.
It owns no device, socket, or input writer.  The yield-recovery route remains
inactive and unqualified.
"""
from __future__ import annotations

import math
import copy

import numpy as np

from contact_benchmark_protocol import disturbance, ENTRY_DURATION_S
from contact_yield_protocol import PATH_SEAM_CONTINUATION_S, PERIOD_S
from yield_contact_runtime import PRE_PATH_LATE_CYCLE_MAX_S
from contact_benchmark_provider import ContactReadinessObserver, ContactForceObservation
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from step5d_autotune_v4_r004.calibrated_runtime import CalibratedCommand


class YieldContactProvider:
    def __init__(self, *, runtime, model_hashes=None, native_binding=None, scenario="nominal", amplitude_n=0.0):
        disturbance(scenario, 0.0, amplitude_n=amplitude_n)
        if native_binding is not None:
            binding_hashes = dict(native_binding.model_hashes)
            if model_hashes is not None and dict(model_hashes) != binding_hashes:
                raise ValueError("provider model hashes differ from native binding")
            model_hashes = binding_hashes
        elif model_hashes is None:
            raise ValueError("model hashes or native binding required")
        self.runtime = runtime
        self.native_binding = native_binding
        self.model_hashes = dict(model_hashes)
        self.solver_profile = runtime.solver_profile
        self.lifecycle_observer = ContactReadinessObserver(runtime.controller.settings.filter_tau_s)
        self.scenario = scenario
        self.amplitude_n = amplitude_n
        self.last_result = None
        self.command_history = None
        self.last_pause = None

    def formal_reference(self, time_s):
        """World reference for evidence joined to an actually consumed packet."""
        ref = self.runtime.controller.task.reference(time_s)
        return {'position_m': self.runtime.anchor + self.runtime.basis @ np.asarray(ref['position_m']),
                'velocity_m_s': self.runtime.basis @ np.asarray(ref['velocity_m_s'])}

    def snapshot(self):
        return {"runtime": self.runtime.snapshot(), "last_result": copy.deepcopy(self.last_result),
                "command_history": copy.deepcopy(self.command_history),
                "last_pause": copy.deepcopy(self.last_pause),
                "readiness": {"filtered_normal_n": self.lifecycle_observer.filtered_normal_n,
                    "last_log": None if self.lifecycle_observer.last_log is None else {
                        "filtered_normal_n": self.lifecycle_observer.last_log.filtered_normal_n,
                        "actual_dt_s": self.lifecycle_observer.last_log.actual_dt_s}}}

    def restore(self, state):
        readiness = state["readiness"]
        filtered = readiness["filtered_normal_n"]
        if filtered is not None and not math.isfinite(float(filtered)):
            raise ValueError("nonfinite readiness state")
        log = readiness["last_log"]
        if log is not None:
            if (not math.isfinite(float(log["filtered_normal_n"]))
                or not 0 < float(log["actual_dt_s"]) <= .004):
                raise ValueError("invalid readiness log")
            log = ContactForceObservation(float(log["filtered_normal_n"]), float(log["actual_dt_s"]))
        self.runtime.restore(state["runtime"])
        self.lifecycle_observer.filtered_normal_n = filtered
        self.lifecycle_observer.last_log = log
        self.last_pause = copy.deepcopy(state["last_pause"])
        self.last_result = copy.deepcopy(state["last_result"])
        self.command_history = copy.deepcopy(state.get("command_history"))

    def bind_command_history(self, previous_qdot, slew_rad_s2):
        qdot = tuple(float(x) for x in previous_qdot)
        slew = float(slew_rad_s2)
        if len(qdot) != 6 or not all(math.isfinite(x) for x in qdot) or not math.isfinite(slew) or slew <= 0:
            raise ValueError("invalid host command history")
        self.command_history = {"previous_qdot": qdot, "slew_rad_s2": slew}

    @property
    def command_normal_base(self):
        return tuple(float(v) for v in self.runtime.controller.estimator.normal)

    @staticmethod
    def execution_phase(execution_time_s):
        elapsed = float(execution_time_s)
        if not math.isfinite(elapsed) or not 0 <= elapsed <= (
            ENTRY_DURATION_S + PERIOD_S + PATH_SEAM_CONTINUATION_S
        ):
            raise ValueError("execution clock outside entry plus full PATH")
        if elapsed < ENTRY_DURATION_S:
            return "entry", elapsed
        return "path", elapsed - ENTRY_DURATION_S

    def execution_path_errors(self, *, actual_tcp_pose, path_time_s, motion_kp):
        phase, clock = self.execution_phase(path_time_s)
        return self.path_errors(actual_tcp_pose=actual_tcp_pose, path_time_s=clock,
                                motion_kp=motion_kp, phase=phase)

    def execution_command(self, **kwargs):
        """Mature state-25 elapsed clock includes entry; formal time does not."""
        args = dict(kwargs)
        execution_time_s = None
        if args["mode"] == "path":
            execution_time_s = float(args["path_time_s"])
            phase, clock = self.execution_phase(execution_time_s)
            args["mode"] = phase
            if phase == "entry":
                args["path_time_s"] = None
                args["entry_time_s"] = clock
            else:
                args["path_time_s"] = clock
        command = self.command(**args)
        self.last_result["execution_time_s"] = execution_time_s
        self.last_result["entry_duration_s"] = ENTRY_DURATION_S
        self.last_result["formal_duration_s"] = PERIOD_S
        return command

    def path_errors(self, *, actual_tcp_pose, path_time_s, motion_kp, phase="path"):
        if not math.isfinite(motion_kp) or motion_kp <= 0:
            raise ValueError("invalid motion Kp")
        pose = np.asarray(actual_tcp_pose, dtype=float)
        if phase == "entry":
            local = self.runtime.basis.T @ (pose[:3] - self.runtime.anchor)
            reference = self.runtime.controller.task.entry_reference(path_time_s)
            task_error = local - np.asarray(reference["position_m"])
        elif phase == "path":
            task_error = self.runtime.path_error_task_m(pose[:3], path_time_s)
        else:
            raise ValueError("invalid reference phase")
        omega = self.runtime.orientation_error_rad(rotvec_to_matrix(pose[3:]))
        return tuple(float(-value) for value in task_error[:2]), omega

    def _observation(
        self,
        output,
        sensor,
        monotonic_s,
        actual_dt_s,
        *,
        max_dt_s=.004,
        strict_dt_upper=False,
    ):
        maximum = float(max_dt_s)
        within_upper = actual_dt_s < maximum if strict_dt_upper else actual_dt_s <= maximum
        if not math.isfinite(actual_dt_s) or not math.isfinite(maximum) or not 0 < actual_dt_s or not within_upper:
            bracket = '(0,{:.0f}ms)'.format(maximum * 1000.0) if strict_dt_upper else '(0,{:.0f}ms]'.format(maximum * 1000.0)
            raise ValueError(f"actual dt outside {bracket}")
        if self.runtime.last_sample_s is None and not math.isclose(
            actual_dt_s, self.runtime.controller.dt_s, rel_tol=0, abs_tol=1e-12
        ):
            raise ValueError("first sample interval must match configured controller dt")
        if sensor.stop_request or sensor.observed_at_s is None:
            raise ValueError("timestamped sensor observation required")
        if not output.safety_normal:
            raise ValueError("robot not Safety NORMAL")
        if self.runtime.last_sample_s is not None and not math.isclose(
            monotonic_s - self.runtime.last_sample_s, actual_dt_s, rel_tol=0, abs_tol=1e-7
        ):
            raise ValueError("owner/kernel clock discontinuity; stationary seam needs explicit binding")
        received = getattr(output, "received_monotonic_s", None)
        if received is None or not math.isfinite(received):
            raise ValueError("robot requires timestamped monotonic receive evidence")
        robot = {
            "observed_at_s": received,
            "timestamp": output.timestamp,
            "safety_mode": output.safety_mode,
            "tcp_offset": output.tcp_offset_m_rad,
            "payload": output.payload_kg,
            "payload_cog": output.payload_cog_m,
            "actual_q": output.q_rad,
            "actual_qd": output.qd_rad_s,
            "actual_TCP_pose": output.tcp_pose_m_rad,
            "actual_TCP_speed": output.tcp_speed_m_s_rad_s,
        }
        return robot

    def pause(self, *, output, sensor, monotonic_s, actual_dt_s, reason):
        robot = self._observation(output, sensor, monotonic_s, actual_dt_s)
        self.last_pause = self.runtime.pause(
            robot=robot,
            wrench_tcp=sensor.wrench,
            guard_wrench_tcp=sensor.raw_wrench,
            sensor_observed_at_s=sensor.observed_at_s,
            sample_time_s=monotonic_s,
            reason=reason,
        )
        return self.last_pause

    def hold_pre_path_late_cycle(self, *, output, sensor, monotonic_s, actual_dt_s, reason):
        """Pass a measured late pre-PATH tick to the evidence-only runtime seam."""
        robot = self._observation(
            output,
            sensor,
            monotonic_s,
            actual_dt_s,
            max_dt_s=PRE_PATH_LATE_CYCLE_MAX_S,
            strict_dt_upper=True,
        )
        self.last_pause = self.runtime.hold_pre_path_late_cycle(
            robot=robot,
            wrench_tcp=sensor.wrench,
            guard_wrench_tcp=sensor.raw_wrench,
            sensor_observed_at_s=sensor.observed_at_s,
            sample_time_s=monotonic_s,
            reason=reason,
        )
        filtered = self.lifecycle_observer.filtered_normal_n
        if filtered is not None:
            self.last_pause["filtered_normal_n"] = float(filtered)
        return self.last_pause

    def command(
        self,
        *,
        output,
        sensor,
        monotonic_s,
        actual_dt_s,
        mode,
        path_time_s=None,
        internal_setpoint_n,
        entry_time_s=None,
    ):
        if mode not in ("baseline", "entry", "path"):
            raise ValueError("invalid contact phase")
        if mode == "entry":
            if entry_time_s is not None and path_time_s is not None and not math.isclose(
                entry_time_s, path_time_s, rel_tol=0.0, abs_tol=1e-12
            ):
                raise ValueError("entry clocks disagree")
            entry_clock = entry_time_s if entry_time_s is not None else path_time_s
            if entry_clock is None:
                raise ValueError("entry clock is required")
            runtime_path_time_s = None
            runtime_entry_time_s = entry_clock
        else:
            if entry_time_s is not None:
                raise ValueError("entry clock is valid only for entry phase")
            if mode == "path" and path_time_s is None:
                raise ValueError("formal PATH clock is required")
            runtime_path_time_s = path_time_s if mode == "path" else None
            runtime_entry_time_s = None
        robot = self._observation(output, sensor, monotonic_s, actual_dt_s)
        result = self.runtime.step(
            robot=robot,
            wrench_tcp=sensor.wrench,
            guard_wrench_tcp=sensor.raw_wrench,
            sensor_observed_at_s=sensor.observed_at_s,
            sample_time_s=monotonic_s,
            phase=mode,
            path_time_s=runtime_path_time_s,
            entry_time_s=runtime_entry_time_s,
            force_reference_n=internal_setpoint_n,
            command_history=self.command_history,
            injection_task_n=(
                disturbance(self.scenario, path_time_s, amplitude_n=self.amplitude_n)
                if mode == "path"
                else (0.0, 0.0, 0.0)
            ),
        )
        result["filtered_normal_n"] = float(result["signed_normal_load_n"])
        self.last_result = result
        result_entry_time_s = result.get("entry_time_s")
        if mode == "path":
            command_path_time_s = path_time_s
        elif mode == "entry":
            command_path_time_s = 0.0 if result_entry_time_s is None else float(result_entry_time_s)
        else:
            command_path_time_s = 0.0 if path_time_s is None else path_time_s
        return CalibratedCommand(
            qdot=result["qdot_rad_s"],
            jacobian_6x6=result["jacobian_6x6"],
            observed_model_hashes=self.model_hashes,
            tangential_error_m=tuple(result["path_error_task_m"][:2]),
            orientation_error_rad=self.runtime.orientation_error_rad(
                rotvec_to_matrix(np.asarray(output.tcp_pose_m_rad[3:]))
            ),
            path_time_s=command_path_time_s,
            solver_status=40.0,
        )
