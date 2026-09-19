"""Explicit adapter for the mature qualification control's contact-only branch."""
import math
from dataclasses import dataclass
import numpy as np
import pinocchio as pin
from contact_benchmark_protocol import disturbance
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from step5d_autotune_v4_r004.calibrated_runtime import CalibratedCommand


@dataclass(frozen=True)
class ContactForceObservation:
    filtered_normal_n: float
    actual_dt_s: float
    role: str = 'readiness_observation_only'


class ContactReadinessObserver:
    """Scalar readiness filter; has no PID, native-law or command state.

    It observes every acquisition tick, including stationary seams. The native
    controller's separate vector filter follows the explicit freeze-carry policy.
    """
    def __init__(self, tau_s):
        if not math.isfinite(tau_s) or tau_s <= 0:
            raise ValueError('invalid readiness filter time constant')
        self.tau_s=tau_s
        self.filtered_normal_n=None
        self.last_log=None

    def step(self, *, actual_dt_s, raw_normal_n, setpoint_n, mode,
             orientation_error_rad=(0.,0.,0.), tangential_error_m=(0.,0.)):
        if not math.isfinite(actual_dt_s) or not 0 < actual_dt_s <= .004:
            raise ValueError('readiness observation interval outside (0,4ms]')
        if not math.isfinite(raw_normal_n):
            raise ValueError('nonfinite readiness force')
        if self.filtered_normal_n is None:
            self.filtered_normal_n=float(raw_normal_n)
        else:
            self.filtered_normal_n += -math.expm1(-actual_dt_s/self.tau_s)*(raw_normal_n-self.filtered_normal_n)
        self.last_log=ContactForceObservation(self.filtered_normal_n,actual_dt_s)
        return self.last_log


class ContactCommandProvider:
    def __init__(self, *, runtime, model_hashes, scenario='nominal', amplitude_n=0.):
        # Validate the scenario once, outside the writer tick.
        disturbance(scenario,0.,amplitude_n=amplitude_n)
        self.runtime=runtime;self.model_hashes=dict(model_hashes)
        self.solver_profile=runtime.solver_profile
        self.lifecycle_observer=ContactReadinessObserver(runtime.kernel.outer.settings.filter_tau_s)
        self.scenario=scenario;self.amplitude_n=amplitude_n;self.last_result=None

    def path_errors(self, *, actual_tcp_pose, path_time_s, motion_kp):
        if not math.isfinite(motion_kp) or motion_kp<=0:raise ValueError('invalid motion Kp')
        outer=self.runtime.kernel.outer;ref=outer.task.reference(path_time_s)
        pose=np.asarray(actual_tcp_pose)
        local=outer.basis.T@(pose[:3]-outer.anchor)
        error=np.asarray(ref['position_m'])-local
        omega_error=pin.log3(outer.target@rotvec_to_matrix(pose[3:]).T)
        return tuple(error[:2]),tuple(omega_error)

    def _observation(self,output,sensor,monotonic_s,actual_dt_s):
        # ``sensor_fresh`` is the legacy writer flag and already uses the
        # 80-ms transport boundary.  Admission here only requires a finite
        # timestamp; ContactRuntime owns the shared age classification so a
        # 20--80 ms packet is retained as held evidence and >=80 ms is counted
        # as a stale stop rather than being rejected as an opaque flag.
        if sensor.stop_request or sensor.observed_at_s is None:
            raise ValueError('timestamped sensor observation required')
        if not output.safety_normal:raise ValueError('robot not Safety NORMAL')
        if self.runtime.last_sample_s is not None and not math.isclose(
                monotonic_s-self.runtime.last_sample_s,actual_dt_s,rel_tol=0,abs_tol=1e-7):
            raise ValueError('owner/kernel clock discontinuity; stationary seam needs explicit binding')
        received = getattr(output, 'received_monotonic_s', None)
        if received is None or not math.isfinite(received):
            raise ValueError('robot requires timestamped monotonic receive evidence')
        robot={'observed_at_s':received,'timestamp':output.timestamp,
               'safety_mode':output.safety_mode,'tcp_offset':output.tcp_offset_m_rad,
               'payload':output.payload_kg,'payload_cog':output.payload_cog_m,
               'actual_q':output.q_rad,'actual_qd':output.qd_rad_s,
               'actual_TCP_pose':output.tcp_pose_m_rad,'actual_TCP_speed':output.tcp_speed_m_s_rad_s}
        return robot

    def pause(self, *, output,sensor,monotonic_s,actual_dt_s,reason):
        robot=self._observation(output,sensor,monotonic_s,actual_dt_s)
        self.last_pause=self.runtime.pause(robot=robot,wrench_tcp=sensor.wrench,
            sensor_observed_at_s=sensor.observed_at_s,sample_time_s=monotonic_s,reason=reason)
        return self.last_pause

    def command(self, *, output, sensor, monotonic_s, actual_dt_s, mode,
                path_time_s=None, internal_setpoint_n, entry_time_s=None):
        if mode not in ('baseline', 'entry', 'path'):
            raise ValueError('invalid contact phase')
        if mode == 'entry':
            # ContactRuntime has one existing phase-local clock slot.  The
            # outer loop interprets it as the explicit entry clock only when
            # phase='entry'; callers may use the clearer entry_time_s alias.
            if entry_time_s is not None and path_time_s is not None and not math.isclose(
                    entry_time_s, path_time_s, rel_tol=0., abs_tol=1e-12):
                raise ValueError('entry clocks disagree')
            entry_clock = entry_time_s if entry_time_s is not None else path_time_s
            if entry_clock is None:
                raise ValueError('entry clock is required')
            runtime_path_time_s = entry_clock
        else:
            if entry_time_s is not None:
                raise ValueError('entry clock is valid only for entry phase')
            if mode == 'path' and path_time_s is None:
                raise ValueError('formal PATH clock is required')
            runtime_path_time_s = path_time_s if mode == 'path' else None
        robot=self._observation(output,sensor,monotonic_s,actual_dt_s)
        result=self.runtime.step(robot=robot,wrench_tcp=sensor.wrench,
            sensor_observed_at_s=sensor.observed_at_s,sample_time_s=monotonic_s,
            phase=mode,path_time_s=runtime_path_time_s,
            force_reference_n=internal_setpoint_n,
            injection_task_n=disturbance(self.scenario,path_time_s,amplitude_n=self.amplitude_n) if mode=='path' else (0.,0.,0.))
        result['filtered_normal_n']=float(np.dot(result['filtered_force_base_n'],self.runtime.kernel.outer.reaction))
        self.last_result=result
        # CalibratedCommand predates the explicit entry phase and requires a
        # float path_time_s.  Its value is retained as an ABI field; formal
        # time is authoritative in result['formal_time_s'], which is None
        # during entry and starts at zero after the one-second seam.
        result_entry_time_s = result.get('entry_time_s')
        if mode == 'path':
            command_path_time_s = path_time_s
        elif mode == 'entry':
            command_path_time_s = (0. if result_entry_time_s is None
                                   else float(result_entry_time_s))
        else:
            # Preserve the legacy baseline packet field even though its
            # runtime phase has no formal path clock.
            command_path_time_s = 0. if path_time_s is None else path_time_s
        return CalibratedCommand(qdot=result['qdot_rad_s'],jacobian_6x6=result['jacobian_6x6'],
            observed_model_hashes=self.model_hashes,tangential_error_m=tuple(result['path_error_task_m'][:2]),
            orientation_error_rad=tuple(pin.log3(self.runtime.kernel.outer.target@rotvec_to_matrix(np.asarray(output.tcp_pose_m_rad[3:])).T)),path_time_s=command_path_time_s,
            solver_status=40.) # Legacy packet ABI only; explicit profile identifies QP.
