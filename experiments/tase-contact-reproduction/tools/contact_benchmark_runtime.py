"""Measured robot/sensor observations -> six-law command, without transport.

The physical owner must supply fresh acquisition timestamps and a contact
anchor. This module neither reads devices nor authorizes publishing commands.
It intentionally bypasses the historical PID outer loop and post-QP rescaling.
"""
from __future__ import annotations

import math
import time
import numpy as np

from contact_benchmark_kernel import ContactKernel, KernelDeadlineError
from contact_qp import QpSolverProfile
from contact_benchmark_outer import finite
from step5c_calibrated_kinematics_audit import build_calibrated_model, rotvec_to_matrix
from step5d_autotune_v4_r004.calibrated_runtime import tcp_jacobian_base


class ContactRuntime:
    def __init__(self, *, law, qp_library, anchor_m, task_basis, target_rotation,
                 deadline_s=.0015):
        if deadline_s is not None and (not math.isfinite(deadline_s) or deadline_s<=0):
            raise ValueError('runtime deadline must be positive')
        self.deadline_s=deadline_s
        self.solver_profile=QpSolverProfile(qp_library)
        self.model=build_calibrated_model()
        self.kernel=ContactKernel(law=law,qp_library=qp_library,anchor_m=anchor_m,
            task_basis=task_basis,target_rotation=target_rotation,
            raw_force_limit_n=20.,raw_torque_limit_nm=2.,
            qp_deadline_s=.001 if deadline_s is not None else None,
            kernel_deadline_s=deadline_s)
        self.last_sample_s=None
        self.last_controller_timestamp=None
        self.last_sensor_timestamp=None
        self.paused_s=0.

    def snapshot(self):
        return {'kernel':self.kernel.snapshot(),'last_sample_s':self.last_sample_s,
                'last_controller_timestamp':self.last_controller_timestamp,
                'last_sensor_timestamp':self.last_sensor_timestamp,'paused_s':self.paused_s}

    def _validate_observation(self,robot,wrench_tcp,sensor_observed_at_s,sample_time_s):
        now=float(sample_time_s);sensor_t=float(sensor_observed_at_s)
        robot_t=float(robot['observed_at_s']);controller_t=float(robot['timestamp'])
        if not all(math.isfinite(t) for t in (now,sensor_t,robot_t,controller_t)):
            raise ValueError('nonfinite acquisition clock')
        age=max(now-sensor_t,now-robot_t)
        if min(now-sensor_t,now-robot_t)<0 or age>.020:
            raise ValueError('robot/sensor observation older than 20ms or from future')
        if self.last_controller_timestamp is not None and controller_t<self.last_controller_timestamp:
            raise ValueError('controller timestamp regressed')
        if self.last_sensor_timestamp is not None and sensor_t<self.last_sensor_timestamp:
            raise ValueError('sensor timestamp regressed')
        dt=.002 if self.last_sample_s is None else now-self.last_sample_s
        if not 0<dt<=.004:raise ValueError('writer sample interval outside (0,4ms]')
        if 'safety_status_bits' in robot:
            normal=robot['safety_status_bits'] in (1,2049)
        else:
            normal=robot.get('safety_mode') in (1,'NORMAL')
        if not normal:raise ValueError('robot not Safety NORMAL')
        tcp=finite(robot['tcp_offset'],(6,),'active TCP')
        cog=finite(robot['payload_cog'],(3,),'active CoG')
        if (not np.allclose(tcp,[0,0,.0874,0,0,0],rtol=0,atol=1e-9)
                or not np.allclose(cog,[.0011,.0031,.0163],rtol=0,atol=1e-6)
                or not math.isclose(float(robot['payload']),.413,rel_tol=0,abs_tol=1e-6)):
            raise ValueError('active tool binding changed')
        q=finite(robot['actual_q'],(6,),'joint position')
        qd=finite(robot['actual_qd'],(6,),'joint velocity')
        pose=finite(robot['actual_TCP_pose'],(6,),'TCP pose')
        wrench=finite(wrench_tcp,(6,),'raw TCP wrench')
        if np.max(np.abs(qd))>.06:raise ValueError('observed joint speed exceeds envelope')
        # Limit all possible commands for a maximum 4ms hold at joint limits.
        lower=np.maximum((self.model.model.lowerPositionLimit-q)/.004,-.05)
        upper=np.minimum((self.model.model.upperPositionLimit-q)/.004,.05)
        if np.any(q<self.model.model.lowerPositionLimit) or np.any(q>self.model.model.upperPositionLimit):
            raise ValueError('observed joint position exceeds limit')
        if np.linalg.norm(wrench[:3])>=20. or np.linalg.norm(wrench[3:])>=2.:
            raise ValueError('raw sensor guard rejected observation')
        return dict(now=now,sensor_t=sensor_t,controller_t=controller_t,age=age,dt=dt,
                    tcp=tcp,q=q,qd=qd,pose=pose,wrench=wrench,lower=lower,upper=upper)

    def pause(self, *, robot,wrench_tcp,sensor_observed_at_s,sample_time_s,reason):
        """Freeze complete controller state only during the observed pre-PATH seam."""
        started=time.perf_counter()
        obs=self._validate_observation(robot,wrench_tcp,sensor_observed_at_s,sample_time_s)
        if self.kernel.outer.phase=='path':raise ValueError('cannot pause an active PATH controller')
        speed=finite(robot['actual_TCP_speed'],(6,),'TCP speed')
        if np.linalg.norm(speed[:3])>.0005 or np.linalg.norm(speed[3:])>.005 or np.max(np.abs(obs['qd']))>.001:
            raise ValueError('freeze-carry seam requires stationary robot')
        if not isinstance(reason,str) or not reason:raise ValueError('pause reason required')
        elapsed=time.perf_counter()-started
        if self.deadline_s is not None and elapsed>self.deadline_s:
            raise KernelDeadlineError('pause observation deadline exceeded')
        self.last_sample_s=obs['now'];self.last_controller_timestamp=obs['controller_t'];self.last_sensor_timestamp=obs['sensor_t']
        self.paused_s+=obs['dt']
        return {'policy':'pre_path_stationary_freeze_carry','reason':reason,
                'paused_s':self.paused_s,'sample_time_s':obs['now'],'observation_age_s':obs['age']}

    def step(self, *, robot, wrench_tcp, sensor_observed_at_s, sample_time_s,
             phase, path_time_s=None, force_reference_n=None,
             injection_task_n=(0.,0.,0.)):
        """Consume an owner-supplied RTDE row and untampered, baseline-subtracted wrench.

        robot uses RTDE field names plus observed_at_s (host monotonic receive
        time). wrench_tcp is environment-on-tool, before filtering/injection.
        Sensor and robot acquisition clocks must share the host monotonic clock.
        """
        started=time.perf_counter()
        obs=self._validate_observation(robot,wrench_tcp,sensor_observed_at_s,sample_time_s)
        now,sensor_t,controller_t,age,dt,tcp,q,pose,wrench,lower,upper=(obs[k] for k in
            ('now','sensor_t','controller_t','age','dt','tcp','q','pose','wrench','lower','upper'))
        R=rotvec_to_matrix(pose[3:])
        before=self.kernel.snapshot()
        try:
            J=tcp_jacobian_base(self.model,q,tcp[:3])
            result=self.kernel.step(jacobian=J,
                joint_velocity_lower=lower,joint_velocity_upper=upper,
                time_s=now,dt_s=dt,phase=phase,path_time_s=path_time_s,
                force_reference_n=force_reference_n,position_m=pose[:3],rotation=R,
                raw_force_base_n=R@wrench[:3],raw_torque_base_nm=R@wrench[3:],
                injection_task_n=injection_task_n,state_age_s=age)
            elapsed=time.perf_counter()-started
            if self.deadline_s is not None and elapsed>self.deadline_s:
                raise KernelDeadlineError(f'observation-to-command deadline exceeded: {elapsed:.6f}s')
        except Exception:
            self.kernel.restore(before)
            raise
        self.last_sample_s=now;self.last_controller_timestamp=controller_t;self.last_sensor_timestamp=sensor_t
        return {**result,'pre_path_paused_s':self.paused_s,'runtime_wall_s':elapsed,'observation_age_s':age,
                'raw_wrench_tcp':tuple(wrench),'jacobian_6x6':tuple(tuple(row) for row in J),'jacobian_calibration_hash':self.model.calibration_hash,
                'claim_scope':'measured-observation command proposal; physical owner admission and transport required'}
