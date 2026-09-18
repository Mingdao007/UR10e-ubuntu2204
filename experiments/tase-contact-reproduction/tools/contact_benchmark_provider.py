"""Explicit adapter for the mature qualification control's contact-only branch."""
import math
import numpy as np
import pinocchio as pin
from contact_benchmark_protocol import disturbance
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from step5d_autotune_v4_r004.calibrated_runtime import CalibratedCommand


class ContactCommandProvider:
    def __init__(self, *, runtime, model_hashes, scenario='nominal', amplitude_n=0.):
        # Validate the scenario once, outside the writer tick.
        disturbance(scenario,0.,amplitude_n=amplitude_n)
        self.runtime=runtime;self.model_hashes=dict(model_hashes)
        self.solver_profile=runtime.solver_profile
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
        if not sensor.sensor_fresh or sensor.stop_request or sensor.observed_at_s is None:
            raise ValueError('fresh timestamped sensor observation required')
        if not output.safety_normal:raise ValueError('robot not Safety NORMAL')
        if self.runtime.last_sample_s is not None and not math.isclose(
                monotonic_s-self.runtime.last_sample_s,actual_dt_s,rel_tol=0,abs_tol=1e-7):
            raise ValueError('owner/kernel clock discontinuity; stationary seam needs explicit binding')
        robot={'observed_at_s':output.observed_at_s,'timestamp':output.timestamp,
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
                path_time_s, internal_setpoint_n):
        robot=self._observation(output,sensor,monotonic_s,actual_dt_s)
        result=self.runtime.step(robot=robot,wrench_tcp=sensor.wrench,
            sensor_observed_at_s=sensor.observed_at_s,sample_time_s=monotonic_s,
            phase=mode,path_time_s=path_time_s if mode=='path' else None,
            force_reference_n=internal_setpoint_n,
            injection_task_n=disturbance(self.scenario,path_time_s,amplitude_n=self.amplitude_n) if mode=='path' else (0.,0.,0.))
        result['filtered_normal_n']=float(np.dot(result['filtered_force_base_n'],self.runtime.kernel.outer.reaction))
        self.last_result=result
        return CalibratedCommand(qdot=result['qdot_rad_s'],jacobian_6x6=result['jacobian_6x6'],
            observed_model_hashes=self.model_hashes,tangential_error_m=tuple(result['path_error_task_m'][:2]),
            orientation_error_rad=tuple(pin.log3(self.runtime.kernel.outer.target@rotvec_to_matrix(np.asarray(output.tcp_pose_m_rad[3:])).T)),path_time_s=path_time_s,
            solver_status=40.) # Legacy packet ABI only; explicit profile identifies QP.
